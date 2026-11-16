"""交易规则引擎（B4）—— 分板块 / 分状态 / 分市场，全部配置化。

设计原则
--------
1. **不写死涨跌幅**：每个板块一条规则，带 ``source`` / ``effective_from`` / ``status``。
   规则会变（例如 2026-07-06 沪深主板 ST 股涨跌幅由 ±5% 调整为 ±10%），
   写死在代码里迟早出错。
2. **不猜**：符号 → 板块解析不出来，或规则状态为 ``UNKNOWN``，一律拒绝交易，
   错误码 ``RULE_UNKNOWN``；状态为 ``UNVERIFIED`` 时由 ``allow_unverified`` 决定。
3. **可审计**：每次校验都返回结构化 :class:`RuleViolation`（code / detail / rule_id），
   由风控落库。
4. **只做 A 股通的硬规则**：T+1、涨跌幅、最小单位、停牌、交易日历。
   港股/美股的做市、LULD 等未建模，显式标注 ``not_modeled``，不做假校验。

规则来源现状（见 B4_MARKET_RULES_REPORT.md 第 2 节）
--------------------------------------------------
价格限制类规则依据 2026-04-24 发布、2026-07-06 实施的沪深北《交易规则(2026 年修订)》，
来源为央视网 / 中国经济网 / 人民财经网 2026-07-06 的报道（**二级来源**），
未取得交易所原文，因此 ``source_tier="secondary"``。
节假日表未内置（无法离线核实），``calendar.status=UNKNOWN``。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, time
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .. import timebase
from .clock import (
    ClockError,
    ExchangeClock,
    PHASE_HOLIDAY,
    PHASE_WEEKEND,
    build_clock,
)

# --------------------------------------------------------------------------- #
# 状态
# --------------------------------------------------------------------------- #
STATUS_VERIFIED = "VERIFIED"        # 已核对（来源可追溯）
STATUS_UNVERIFIED = "UNVERIFIED"    # 有依据但未与一手文档核对
STATUS_UNKNOWN = "UNKNOWN"          # 不知道 —— 必须拒绝

TIER_PRIMARY = "primary"
TIER_SECONDARY = "secondary"
TIER_NONE = "none"

# 违规码
CODE_RULE_UNKNOWN = "RULE_UNKNOWN"
CODE_RULE_UNVERIFIED = "RULE_UNVERIFIED"
CODE_NO_PRICE_LIMIT_DATA = "NO_PRICE_LIMIT_DATA"
CODE_PRICE_LIMIT = "PRICE_LIMIT"
CODE_LOT_SIZE = "LOT_SIZE"
CODE_T_PLUS = "T_PLUS_VIOLATION"
CODE_SUSPENDED = "SUSPENDED"
CODE_CALENDAR = "NOT_TRADING_DAY"
CODE_SESSION = "OUT_OF_SESSION"
CODE_SELLABLE = "INSUFFICIENT_SELLABLE"
CODE_PRICE_TICK = "PRICE_TICK"
CODE_MARKET_NOT_ENFORCED = "MARKET_NOT_ENFORCED"


def round_price(value: float, digits: int = 2) -> float:
    """交易所报价取整：四舍五入到 0.01 元（A 股最小价格变动）。

    用 Decimal 避免二进制浮点在 .005 边界上的错误（例如 10.005 → 10.01）。
    """
    q = Decimal(1).scaleb(-digits)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def _hm_of(text: str) -> Optional[Tuple[int, int]]:
    """``"09:30"`` → ``(9, 30)``。解析失败返回 ``None``（不猜）。"""
    if not text or ":" not in text:
        return None
    try:
        h, m = text.split(":")[:2]
        hh, mm = int(h), int(m)
    except (TypeError, ValueError):
        return None
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return hh, mm


@dataclass(frozen=True)
class RuleMeta:
    status: str = STATUS_UNKNOWN
    source: str = ""
    effective_from: str = ""
    source_tier: str = TIER_NONE
    note: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status, "source": self.source,
            "effective_from": self.effective_from,
            "source_tier": self.source_tier, "note": self.note,
        }

    @property
    def usable(self) -> bool:
        return self.status in (STATUS_VERIFIED, STATUS_UNVERIFIED)


@dataclass(frozen=True)
class BoardRule:
    """一个"板块"的硬规则。"""

    board: str
    name: str
    market: str                 # cn / hk / us
    #: None = 不做涨跌幅校验（该市场未建模价格限制）
    price_limit_pct: Optional[float]
    lot_size: int
    buy_increment: int
    #: 卖出零股规则：all_at_once = 不足一手的必须一次性全部卖出
    sell_odd_lot: str
    #: 0 = T+0，1 = T+1（当日买入不可卖）
    t_plus: int
    meta: RuleMeta
    not_modeled: Tuple[str, ...] = ()
    #: 最小报价单位（US-P0-2）。``None`` = 不校验价格精度。
    #: 美股依据 17 CFR 242.612：≥$1 为 $0.01，<$1 为 $0.0001，
    #: 另有逐股判定（TWAS ≤ $0.015）的 $0.005 档。
    tick_size: Optional[float] = None
    #: 价格 < $1 时适用的最小报价单位（美股为 $0.0001）
    tick_size_below_1: Optional[float] = None
    #: 可适用 $0.005 档的标的（需 TWAS 数据，默认空 = 不启用）
    tick_size_half_cent: Tuple[str, ...] = ()

    def tick_for(self, symbol: str, price: float) -> Optional[float]:
        """该标的在该价位档上的最小报价单位。"""
        s = str(symbol or "").lower()
        if price < 1.0 and self.tick_size_below_1:
            return self.tick_size_below_1
        if self.tick_size_half_cent and s in {x.lower() for x in self.tick_size_half_cent}:
            return 0.005
        return self.tick_size

    def as_dict(self) -> Dict[str, Any]:
        return {
            "board": self.board, "name": self.name, "market": self.market,
            "price_limit_pct": self.price_limit_pct,
            "lot_size": self.lot_size, "buy_increment": self.buy_increment,
            "sell_odd_lot": self.sell_odd_lot, "t_plus": self.t_plus,
            "tick_size": self.tick_size,
            "tick_size_below_1": self.tick_size_below_1,
            "tick_size_half_cent": list(self.tick_size_half_cent),
            "not_modeled": list(self.not_modeled),
            "meta": self.meta.as_dict(),
        }


@dataclass
class RuleViolation:
    code: str
    detail: str
    rule_id: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "detail": self.detail, "rule_id": self.rule_id}


@dataclass
class RuleCheckResult:
    """一次规则校验的完整结果。"""

    applied: bool = False          # 本市场是否启用了强制校验
    rule_id: str = ""
    violations: List[RuleViolation] = field(default_factory=list)
    note: str = ""

    @property
    def ok(self) -> bool:
        return not self.violations

    def as_dict(self) -> Dict[str, Any]:
        return {"applied": self.applied, "rule_id": self.rule_id, "ok": self.ok,
                "note": self.note,
                "violations": [v.as_dict() for v in self.violations]}


# --------------------------------------------------------------------------- #
# 交易日历
# --------------------------------------------------------------------------- #
@dataclass
class CalendarEntry:
    """单个市场的日历条目。

    ``covered_until`` 是 US-P0 新增的诚实性字段：节假日表只覆盖到某一天时，
    超出覆盖范围的日期**必须拒绝**，而不是假定"没登记就是交易日"。
    """

    status: str = STATUS_UNKNOWN
    source: str = ""
    effective_from: str = ""
    covered_from: str = ""
    covered_until: str = ""
    holidays: set = field(default_factory=set)
    #: 提前收市日 → 当日收盘时间（当地时间）
    early_close: Dict[date, Any] = field(default_factory=dict)

    def covers(self, day: date) -> Tuple[bool, str]:
        try:
            if self.covered_from and day < date.fromisoformat(self.covered_from):
                return False, f"{day} 早于日历覆盖起点 {self.covered_from}"
            if self.covered_until and day > date.fromisoformat(self.covered_until):
                return False, (f"{day} 超出日历覆盖范围（最后覆盖 {self.covered_until}）")
        except ValueError:
            return False, f"日历覆盖范围字段非法（{self.covered_from}~{self.covered_until}）"
        return True, ""

    def meta(self) -> RuleMeta:
        return RuleMeta(status=self.status, source=self.source,
                        effective_from=self.effective_from,
                        source_tier=TIER_SECONDARY if self.source else TIER_NONE,
                        note=f"覆盖 {self.covered_from or '?'}~{self.covered_until or '?'}；"
                             f"{len(self.holidays)} 个休市日")


@dataclass
class TradingCalendar:
    """交易日历。

    ``holidays`` 由配置提供（ISO 日期字符串）。**不内置任何节假日表** ——
    离线无法核实，编造一张表比没有表更危险。

    US-P0 起支持**分市场**日历：每个市场可以有自己的状态、来源与覆盖区间。
    未单独配置的市场回落到顶层 ``default``。
    """

    status: str = STATUS_UNKNOWN
    source: str = ""
    effective_from: str = ""
    strict: bool = False
    holidays: Dict[str, set] = field(default_factory=dict)   # market -> set[date]
    entries: Dict[str, CalendarEntry] = field(default_factory=dict)
    default: Optional[CalendarEntry] = None

    def _entry(self, market: str) -> CalendarEntry:
        mkt = (market or "").lower()
        if mkt in self.entries:
            return self.entries[mkt]
        # 未单独配置该市场：用顶层默认值 + 该市场的 holidays 合成，
        # 保证 `calendar.holidays.cn` 这类老写法继续生效。
        base = self.default
        if base is not None:
            return replace(base, holidays=self.holidays.get(mkt, base.holidays))
        return CalendarEntry(status=self.status, source=self.source,
                             effective_from=self.effective_from,
                             holidays=self.holidays.get(mkt, set()))

    def meta(self) -> RuleMeta:
        return RuleMeta(status=self.status, source=self.source,
                        effective_from=self.effective_from,
                        source_tier=TIER_SECONDARY if self.source else TIER_NONE,
                        note="节假日表由配置提供；未内置任何表")

    def is_trading_day(self, market: str, day: date) -> Tuple[bool, str, str]:
        """返回 ``(是否交易日, 原因, 状态)``。``day`` 必须是**交易所当地日期**。"""
        if day.weekday() >= 5:
            return False, f"{day.isoformat()} 是周末（结构上确定休市）", STATUS_VERIFIED
        e = self._entry(market)
        covered, why = e.covers(day)
        if not covered:
            return False, why, STATUS_UNKNOWN
        if e.holidays and day in e.holidays:
            return False, f"{day.isoformat()} 在 {market} 节假日表内", e.status
        if self.strict and e.status != STATUS_VERIFIED:
            return (False,
                    f"{market} 节假日表状态为 {e.status}，strict 模式拒绝交易",
                    e.status)
        return True, f"{day.isoformat()} 非周末、不在节假日表", e.status


# --------------------------------------------------------------------------- #
# 规则集
# --------------------------------------------------------------------------- #
# US-P0-1：默认时段表改为**交易所当地时间**（见 market/clock.py）。
# 旧实现是硬编码北京时间窗口，"us": 21:30–04:00 —— 冬令时会整体偏早 1 小时。
DEFAULT_SESSIONS = {
    "cn": [((9, 30), (11, 30)), ((13, 0), (15, 0))],
    "hk": [((9, 30), (12, 0)), ((13, 0), (16, 0))],
    # 美股：EDT/EST 下的 09:30–16:00（由 zoneinfo 换算，不再手写北京窗口）
    "us": [((9, 30), (16, 0))],
}


class MarketRuleSet:
    """从配置加载的规则集，提供"这笔单合不合规"的判定。"""

    def __init__(self, raw: Optional[Dict[str, Any]] = None):
        raw = raw or {}
        self.enabled = bool(raw.get("enabled", True))
        self.allow_unverified = bool(raw.get("allow_unverified_rules", False))
        self.enforce = bool(raw.get("enforce", True))
        self.enforce_markets = {str(m).lower()
                                for m in (raw.get("enforce_markets") or [])}
        # US-P0-2：市场未纳入 enforce_markets 时怎么办。
        #   warn   = 记录"未建模"并放行（保留历史市场行为）
        #   reject = 拒绝（fail-closed；只交易美股时推荐）
        self.unenforced_policy = str(
            raw.get("unenforced_market_policy") or "warn").lower()
        self.st_symbols = {str(k).lower(): str(v)
                           for k, v in (raw.get("st_symbols") or {}).items()}
        self.boards: Dict[str, BoardRule] = {}
        self._prefixes: List[Tuple[str, str]] = []
        self.suspended: set = {str(s).lower() for s in (raw.get("suspended_symbols") or [])}
        self.sessions: Dict[str, List] = {}
        #: market -> ExchangeClock（US-P0-1：按交易所当地时间判断）
        self.clocks: Dict[str, ExchangeClock] = {}
        self.clock_errors: Dict[str, str] = {}
        self.calendar = TradingCalendar()
        self._raw = raw
        self._load_boards(raw.get("boards") or {})
        self._load_prefixes(raw.get("symbol_prefixes") or [])
        self._load_sessions(raw.get("sessions") or DEFAULT_SESSIONS)
        self._load_calendar(raw.get("calendar") or {})
        self._build_clocks()

    # ------------------------------------------------------------------ #
    # 加载
    # ------------------------------------------------------------------ #
    def _load_boards(self, boards: Dict[str, Any]) -> None:
        for board_id, spec in boards.items():
            if not isinstance(spec, dict):
                continue
            meta = spec.get("meta") or {}
            self.boards[board_id] = BoardRule(
                board=board_id,
                name=str(spec.get("name") or board_id),
                market=str(spec.get("market") or "cn").lower(),
                price_limit_pct=(None if spec.get("price_limit_pct") in (None, "", "null")
                                 else float(spec["price_limit_pct"])),
                lot_size=int(spec.get("lot_size") or 1),
                buy_increment=int(spec.get("buy_increment") or spec.get("lot_size") or 1),
                sell_odd_lot=str(spec.get("sell_odd_lot") or "all_at_once"),
                t_plus=int(spec.get("t_plus") or 0),
                meta=RuleMeta(
                    status=str(meta.get("status") or STATUS_UNKNOWN).upper(),
                    source=str(meta.get("source") or ""),
                    effective_from=str(meta.get("effective_from") or ""),
                    source_tier=str(meta.get("source_tier") or TIER_NONE),
                    note=str(meta.get("note") or ""),
                ),
                not_modeled=tuple(spec.get("not_modeled") or ()),
                tick_size=(None if spec.get("tick_size") in (None, "", "null")
                           else float(spec["tick_size"])),
                tick_size_below_1=(None if spec.get("tick_size_below_1") in (None, "", "null")
                                   else float(spec["tick_size_below_1"])),
                tick_size_half_cent=tuple(
                    str(x).lower() for x in (spec.get("tick_size_half_cent") or ())),
            )

    def _load_prefixes(self, items: Sequence[Dict[str, Any]]) -> None:
        # 按配置顺序匹配（长的/特殊的写前面），不做自动"最长匹配"以免隐式行为
        for item in items:
            if not isinstance(item, dict) or not item.get("prefix"):
                continue
            self._prefixes.append((str(item["prefix"]).lower(), str(item.get("board") or "")))

    def _load_sessions(self, sessions: Dict[str, Any]) -> None:
        """保存原始时段配置，并同步一份便于展示的列表形式。"""
        self._session_specs: Dict[str, Any] = {}
        for mkt, spec in (sessions or {}).items():
            key = str(mkt).lower()
            self._session_specs[key] = spec
            # describe() 用的扁平列表（只反映常规窗口）
            raw_windows = spec.get("regular") if isinstance(spec, dict) else spec
            parsed = []
            for w in raw_windows or []:
                try:
                    o, c = w[0], w[1]
                    po = ((int(o[0]), int(o[1])) if isinstance(o, (tuple, list))
                          else _hm_of(str(o)))
                    pc = ((int(c[0]), int(c[1])) if isinstance(c, (tuple, list))
                          else _hm_of(str(c)))
                    if po and pc:
                        parsed.append((po, pc))
                except (TypeError, IndexError, ValueError):
                    continue
            self.sessions[key] = parsed

    def _build_clocks(self) -> None:
        """把时段 + 日历合成为 ``ExchangeClock``。

        时区数据缺失（``ClockError``）时**不抛异常中断整个程序**，
        而是记录错误并让该市场的 ``session_status`` 恒定拒绝（fail-closed）。
        """
        for mkt, spec in getattr(self, "_session_specs", {}).items():
            e = self.calendar._entry(mkt)
            try:
                self.clocks[mkt] = build_clock(
                    mkt, spec, holidays=e.holidays, early_close=e.early_close)
            except ClockError as exc:
                self.clock_errors[mkt] = str(exc)
        # 配置里没有显式 sessions 时，仍为已定义板块的市场建立时钟
        for mkt in {b.market for b in self.boards.values()}:
            if mkt in self.clocks or mkt in self.clock_errors:
                continue
            if mkt not in DEFAULT_SESSIONS:
                continue
            e = self.calendar._entry(mkt)
            try:
                self.clocks[mkt] = build_clock(
                    mkt, DEFAULT_SESSIONS[mkt], holidays=e.holidays,
                    early_close=e.early_close)
            except ClockError as exc:
                self.clock_errors[mkt] = str(exc)

    def _load_calendar(self, cal: Dict[str, Any]) -> None:
        def _days(items: Any) -> set:
            parsed = set()
            for d in items or []:
                try:
                    parsed.add(date.fromisoformat(str(d)))
                except ValueError:
                    continue
            return parsed

        def _early(items: Any) -> Dict[date, Any]:
            out: Dict[date, Any] = {}
            for d, hm in (items or {}).items():
                hm_pair = _hm_of(str(hm))
                if not hm_pair:
                    continue
                try:
                    out[date.fromisoformat(str(d))] = time(hm_pair[0], hm_pair[1])
                except ValueError:
                    continue
            return out

        holidays: Dict[str, set] = {}
        for mkt, days in (cal.get("holidays") or {}).items():
            holidays[str(mkt).lower()] = _days(days)

        top = str(cal.get("status") or STATUS_UNKNOWN).upper()
        default = CalendarEntry(
            status=top,
            source=str(cal.get("source") or ""),
            effective_from=str(cal.get("effective_from") or ""),
            covered_from=str(cal.get("covered_from") or ""),
            covered_until=str(cal.get("covered_until") or ""),
            holidays=holidays.get("*", set()),
            early_close=_early(cal.get("early_close")),
        )

        entries: Dict[str, CalendarEntry] = {}
        for mkt, spec in (cal.get("markets") or {}).items():
            if not isinstance(spec, dict):
                continue
            key = str(mkt).lower()
            entries[key] = CalendarEntry(
                status=str(spec.get("status") or top).upper(),
                source=str(spec.get("source") or default.source),
                effective_from=str(spec.get("effective_from") or default.effective_from),
                covered_from=str(spec.get("covered_from") or default.covered_from),
                covered_until=str(spec.get("covered_until") or default.covered_until),
                holidays=_days(spec.get("holidays")) or holidays.get(key, set()),
                early_close=_early(spec.get("early_close")),
            )

        self.calendar = TradingCalendar(
            status=top,
            source=str(cal.get("source") or ""),
            effective_from=str(cal.get("effective_from") or ""),
            strict=bool(cal.get("strict", False)),
            holidays=holidays,
            entries=entries,
            default=default,
        )

    # ------------------------------------------------------------------ #
    # 解析
    # ------------------------------------------------------------------ #
    def resolve_symbol(self, symbol: str) -> Tuple[Optional[BoardRule], str]:
        """返回 ``(BoardRule 或 None, 原因)``。解析不出来即 None —— 不猜。"""
        s = str(symbol or "").strip().lower()
        if not s:
            return None, "标的为空"
        # ST/*ST 等特殊状态优先：系统无法从代码判断，必须人工登记
        if s in self.st_symbols:
            board_id = self.st_symbols[s]
            rule = self.boards.get(board_id)
            if rule is None:
                return None, f"ST 登记指向未定义板块 {board_id!r}"
            return rule, f"按 st_symbols 登记命中板块 {board_id}"
        for prefix, board_id in self._prefixes:
            if s.startswith(prefix):
                rule = self.boards.get(board_id)
                if rule is None:
                    return None, f"前缀 {prefix!r} 指向未定义板块 {board_id!r}"
                return rule, f"按前缀 {prefix!r} 命中板块 {board_id}"
        return None, f"无法把 {symbol!r} 解析到任何板块（未配置匹配前缀）"

    def resolve_board_id(self, symbol: str) -> Optional[str]:
        rule, _ = self.resolve_symbol(symbol)
        return rule.board if rule else None

    # ------------------------------------------------------------------ #
    # 时段
    # ------------------------------------------------------------------ #
    def session_info(self, market: str, now: Optional[datetime] = None):
        """返回 :class:`SessionInfo`；无法判定时返回 ``None``。"""
        clock = self.clocks.get((market or "").lower())
        if clock is None:
            return None
        return clock.session(now)

    def session_status(self, market: str, now: Optional[datetime] = None,
                       extended: bool = False) -> Tuple[bool, str]:
        """按**交易所当地时间**判断是否可交易。

        US-P0-1：不再使用"北京时间近似窗口"。``now`` 传 naive 时按本机时区解释，
        传 aware 时按其时区换算到交易所时区（DST/冬令时由 tzdata 处理）。

        ``extended=True`` 时把盘前/盘后/夜盘也算作"可交易"（用于减仓判断）。
        时区数据缺失、时段未配置时一律返回 ``False``（fail-closed）。
        """
        mkt = (market or "").lower()
        err = self.clock_errors.get(mkt)
        if err:
            return False, f"{mkt} 交易所时钟不可用（{err}），拒绝交易"
        clock = self.clocks.get(mkt)
        if clock is None:
            return False, f"{mkt} 未配置交易时段（拒绝而非放行）"
        if not clock.configured:
            return False, f"{mkt} 未配置常规交易时段（拒绝而非放行）"
        try:
            info = clock.session(now)
        except Exception as exc:  # noqa: BLE001 - 时钟异常同样 fail-closed
            return False, f"{mkt} 时段判定异常（{type(exc).__name__}: {exc}），拒绝交易"
        ok = info.is_extended_open if extended else info.is_regular_open
        return ok, f"{mkt}/{clock.tz_name} {info.local_now} {info.phase} · {info.reason}"

    def local_date(self, market: str, now: Optional[datetime] = None) -> Optional[date]:
        """交易所当地日期。日历判断必须用它，不能用本机日期。"""
        clock = self.clocks.get((market or "").lower())
        if clock is None:
            return None
        return clock.local_date(now)

    # ------------------------------------------------------------------ #
    # 涨跌停
    # ------------------------------------------------------------------ #
    def price_limits(self, rule: BoardRule,
                     prev_close: Optional[float]) -> Optional[Tuple[float, float]]:
        """返回 ``(跌停价, 涨停价)``；``None`` 表示该板块不做涨跌幅校验。"""
        if rule.price_limit_pct is None:
            return None
        if not prev_close or prev_close <= 0:
            return None
        up = round_price(prev_close * (1 + rule.price_limit_pct))
        down = round_price(prev_close * (1 - rule.price_limit_pct))
        return down, up

    # ------------------------------------------------------------------ #
    # 主校验
    # ------------------------------------------------------------------ #
    def validate_order(
        self,
        *,
        symbol: str,
        side: str,
        qty: float,
        price: Optional[float],
        prev_close: Optional[float] = None,
        position_qty: float = 0.0,
        today_bought_qty: float = 0.0,
        now: Optional[datetime] = None,
        is_exit: bool = False,
        notes: Optional[List[str]] = None,
    ) -> List[RuleViolation]:
        """校验一笔委托。返回违规列表（空 = 通过）。

        任何一个检查失败都会**独立**记录，便于一次给出全部原因。
        ``notes`` 是输出参数：装"不阻断但必须留痕"的说明（例如减仓豁免时段）。
        """
        if not self.enabled or not self.enforce:
            return []
        v: List[RuleViolation] = []
        if notes is None:
            notes = []
        now = now or timebase.now()

        # ---- 1. 板块 / 规则可用性 ----
        rule, why = self.resolve_symbol(symbol)
        if rule is None:
            v.append(RuleViolation(CODE_RULE_UNKNOWN, f"无法解析标的规则：{why}"))
            return v
        if rule.meta.status == STATUS_UNKNOWN:
            v.append(RuleViolation(
                CODE_RULE_UNKNOWN,
                f"板块 {rule.board} 的规则状态为 UNKNOWN（{rule.meta.note or '无说明'}）",
                rule.board))
            return v
        if rule.meta.status == STATUS_UNVERIFIED and not self.allow_unverified:
            v.append(RuleViolation(
                CODE_RULE_UNVERIFIED,
                f"板块 {rule.board} 的规则未经核对（{rule.meta.source or '无来源'}）；"
                f"allow_unverified_rules=false 时拒绝交易",
                rule.board))
            return v

        # ---- 2. 交易时段（US-P0-1：交易所当地时间；US-P0-4：减仓不阻断）----
        ok, msg = self.session_status(rule.market, now, extended=bool(is_exit))
        if not ok:
            if is_exit:
                # 风险处置（止损/止盈/未知单清理）不允许被"时段判定"挡住：
                # 券商侧若真的不可交易会自行拒单，这里只留痕不拦截。
                notes.append(f"减仓豁免时段检查：{msg}")
            else:
                v.append(RuleViolation(CODE_SESSION, msg, rule.board))

        # ---- 3. 交易日历（用交易所当地日期，不能用本机日期）----
        local_day = self.local_date(rule.market, now)
        if local_day is None:
            if not is_exit:
                v.append(RuleViolation(
                    CODE_CALENDAR,
                    f"{rule.market} 无法确定交易所当地日期（时钟缺失），拒绝交易",
                    "calendar"))
        else:
            day_ok, day_msg, _ = self.calendar.is_trading_day(rule.market, local_day)
            if not day_ok:
                if is_exit:
                    notes.append(f"减仓豁免日历检查：{day_msg}")
                else:
                    v.append(RuleViolation(CODE_CALENDAR, day_msg, "calendar"))

        # ---- 4. 停牌 ----
        if str(symbol).lower() in self.suspended:
            v.append(RuleViolation(CODE_SUSPENDED,
                                   f"{symbol} 在停牌名单内，禁止任何方向委托", rule.board))

        # ---- 5. 数量 / 最小单位 ----
        q = float(qty or 0)
        if q <= 0:
            v.append(RuleViolation(CODE_LOT_SIZE, f"数量必须为正：{q}"))
        elif side == "buy":
            if q < rule.lot_size:
                v.append(RuleViolation(
                    CODE_LOT_SIZE,
                    f"买入数量 {q:g} 小于最小单位 {rule.lot_size}", rule.board))
            elif rule.buy_increment > 1 and int(q) % rule.buy_increment != 0:
                v.append(RuleViolation(
                    CODE_LOT_SIZE,
                    f"买入数量 {q:g} 不是 {rule.buy_increment} 的整数倍", rule.board))
        else:
            sellable = max(0.0, float(position_qty) - float(today_bought_qty))
            if q > sellable + 1e-9:
                v.append(RuleViolation(
                    CODE_SELLABLE,
                    f"可卖数量不足：持仓 {position_qty:g} − 当日买入 {today_bought_qty:g}"
                    f"（T+{rule.t_plus}）= {sellable:g} < 卖出 {q:g}", rule.board))
            if rule.sell_odd_lot == "all_at_once" and q < rule.lot_size:
                # 零股必须一次性全部卖出
                if abs(q - sellable) > 1e-9:
                    v.append(RuleViolation(
                        CODE_LOT_SIZE,
                        f"零股（{q:g} < {rule.lot_size}）必须一次性全部卖出，"
                        f"当前可卖 {sellable:g}", rule.board))

        # ---- 6. T+1 ----
        if side == "sell" and rule.t_plus >= 1 and today_bought_qty > 0:
            if q > max(0.0, float(position_qty) - float(today_bought_qty)) + 1e-9:
                v.append(RuleViolation(
                    CODE_T_PLUS,
                    f"T+{rule.t_plus}：当日买入 {today_bought_qty:g} 股不可卖出"
                    f"（持仓 {position_qty:g}）", rule.board))

        # ---- 7. 涨跌停 ----
        if price is not None and price > 0:
            limits = self.price_limits(rule, prev_close)
            if rule.price_limit_pct is not None and limits is None and not is_exit:
                v.append(RuleViolation(
                    CODE_NO_PRICE_LIMIT_DATA,
                    f"板块 {rule.board} 有 ±{rule.price_limit_pct:.0%} 涨跌幅限制，"
                    f"但缺少昨收价，无法校验委托价", rule.board))
            elif limits is not None:
                down, up = limits
                if price > up + 1e-9:
                    v.append(RuleViolation(
                        CODE_PRICE_LIMIT,
                        f"委托价 {price:g} 超过涨停 {up:g}（昨收 {prev_close:g} "
                        f"× {1 + rule.price_limit_pct:.2f}）", rule.board))
                elif price < down - 1e-9:
                    v.append(RuleViolation(
                        CODE_PRICE_LIMIT,
                        f"委托价 {price:g} 低于跌停 {down:g}（昨收 {prev_close:g} "
                        f"× {1 - rule.price_limit_pct:.2f}）", rule.board))

        # ---- 8. 最小报价单位（US-P0-2：价格精度）----
        if price is not None and price > 0:
            tick = rule.tick_for(symbol, float(price))
            if tick:
                try:
                    # 用 Decimal 判整倍数，避开浮点尾差（10.005 / 0.005 = 2001.0000…）
                    steps = (Decimal(str(price)) / Decimal(str(tick)))
                    bad = abs(steps - steps.to_integral_value()) > Decimal("1e-6")
                except (ArithmeticError, ValueError):
                    bad = True
                if bad:
                    v.append(RuleViolation(
                        CODE_PRICE_TICK,
                        f"委托价 {price:g} 不是最小报价单位 {tick:g} 的整数倍"
                        f"（板块 {rule.board}）", rule.board))
        return v

    # ------------------------------------------------------------------ #
    def applies(self, market: str) -> bool:
        return str(market or "").lower() in self.enforce_markets

    def check_order(self, **kwargs) -> RuleCheckResult:
        """带"是否适用"语义的校验入口（风控用这个）。

        市场不在 ``enforce_markets`` 内时 ``applied=False`` 且无违规 ——
        这明确表示"该市场规则未建模"，而不是"校验通过"。
        """
        symbol = kwargs.get("symbol", "")
        rule, why = self.resolve_symbol(symbol)
        if rule is None:
            return RuleCheckResult(
                applied=self.enabled and self.enforce,
                violations=[RuleViolation(CODE_RULE_UNKNOWN, f"无法解析标的规则：{why}")])
        if not self.enabled or not self.enforce:
            return RuleCheckResult(applied=False, rule_id=rule.board,
                                   note="market_rules 未启用")
        if not self.applies(rule.market):
            note = (f"市场 {rule.market} 未纳入强制校验"
                    f"（enforce_markets={sorted(self.enforce_markets)}）；"
                    f"未建模项：{list(rule.not_modeled) or '无'}")
            if self.unenforced_policy == "reject":
                # US-P0-2：不允许用 applied=False 悄悄绕过规则层
                return RuleCheckResult(
                    applied=True, rule_id=rule.board,
                    violations=[RuleViolation(CODE_MARKET_NOT_ENFORCED, note, rule.board)],
                    note=note)
            return RuleCheckResult(applied=False, rule_id=rule.board, note=note)
        notes: List[str] = []
        violations = self.validate_order(notes=notes, **kwargs)
        return RuleCheckResult(applied=True, rule_id=rule.board,
                               violations=violations,
                               note="；".join(notes))

    def describe(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "enforce": self.enforce,
            "enforce_markets": sorted(self.enforce_markets),
            "unenforced_market_policy": self.unenforced_policy,
            "allow_unverified_rules": self.allow_unverified,
            "st_symbols": dict(self.st_symbols),
            "boards": {k: b.as_dict() for k, b in self.boards.items()},
            "prefixes": [{"prefix": p, "board": b} for p, b in self._prefixes],
            "suspended": sorted(self.suspended),
            "sessions": {k: [[list(a), list(b)] for a, b in v]
                         for k, v in self.sessions.items()},
            "clocks": {k: c.describe() for k, c in self.clocks.items()},
            "clock_errors": dict(self.clock_errors),
            "calendar": {**self.calendar.meta().as_dict(),
                         "strict": self.calendar.strict,
                         "markets": {k: {**e.meta().as_dict(),
                                         "covered_from": e.covered_from,
                                         "covered_until": e.covered_until,
                                         "early_close": sorted(d.isoformat()
                                                               for d in e.early_close)}
                                     for k, e in self.calendar.entries.items()},
                         "holiday_counts": {k: len(v) for k, v in self.calendar.holidays.items()}},
        }

    def unverified_rules(self) -> List[Dict[str, Any]]:
        """列出所有非 VERIFIED 的规则（供报告与 check-env 用）。"""
        out = []
        for b in self.boards.values():
            if b.meta.status != STATUS_VERIFIED:
                out.append({"board": b.board, "status": b.meta.status,
                            "source": b.meta.source,
                            "effective_from": b.meta.effective_from,
                            "note": b.meta.note})
        if self.calendar.status != STATUS_VERIFIED:
            out.append({"board": "calendar", "status": self.calendar.status,
                        "source": self.calendar.source, "effective_from": "",
                        "note": "节假日表未内置，需从交易所公告录入"})
        for mkt, e in self.calendar.entries.items():
            if e.status != STATUS_VERIFIED:
                out.append({"board": f"calendar.{mkt}", "status": e.status,
                            "source": e.source, "effective_from": e.effective_from,
                            "note": e.meta().note})
        return out
