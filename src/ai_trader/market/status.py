"""美股市场状态：停牌 / LULD / MWCB（按官方规则重写 · 2026-11-16）

本版修正了上一轮的三处规则错误，并把「暂停计时」拆开：

| 暂停类型 | 时长 | 自动恢复 |
|---|---|---|
| LULD 交易暂停 | **5 分钟** | 到期可恢复（重新开盘） |
| MWCB L1/L2 | **15 分钟** | 到期可恢复，但**仅在收市前 35 分钟以外触发** |
| MWCB L3 | 当日剩余交易 | **永不**自动恢复 |
| 交易所自行停牌 | 未定 | **必须收到恢复通知**，不得靠本地计时器 |

LULD 只在 **9:30–16:00 ET 常规时段**计算；盘前/盘后/夜间一律 ``OUT_OF_SESSION``。
参考价必须来自**最近 5 分钟合格成交**，取不到就返回 ``UNAVAILABLE``，不得用 last/prev_close 冒充。

双倍带宽（Amendment 18）：
* 开盘 9:30–9:45：**不双倍**（Amendment 18 取消了开盘放宽）
* 收市前 25 分钟：Tier1 双倍；Tier2 且参考价 ≤ $3 双倍；Tier2 > $3 **不双倍**
* 早收市日按**当日实际收盘时间**倒推 25 分钟（不是固定 15:35）
"""
from __future__ import annotations
from .. import timebase

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple


# ====================================================================== #
# 常量与官方参数
# ====================================================================== #
class MarketState(str, Enum):
    OPEN = "open"
    LULD_LIMIT_STATE = "luld_limit_state"     # 触及带但未满 15 秒
    LULD_PAUSED = "luld_paused"
    HALTED = "halted"                          # 交易所停牌（等恢复通知）
    MWCB_PAUSED = "mwcb_paused"
    MWCB_CLOSED = "mwcb_closed"
    DATA_STALE = "data_stale"
    UNKNOWN = "unknown"


class HaltKind(str, Enum):
    LULD = "luld"
    MWCB = "mwcb"
    EXCHANGE = "exchange"                      # 交易所公告停牌


class HaltReason(str, Enum):
    LULD = "luld"
    NEWS_PENDING = "news_pending"
    REGULATORY = "regulatory"
    MWCB = "mwcb"
    OPERATIONAL = "operational"
    UNKNOWN = "unknown"


class LULDAvailability(str, Enum):
    """LULD 计算结果是否可用。**不可用绝不等价于"正常"。**"""

    AVAILABLE = "available"
    OUT_OF_SESSION = "out_of_session"          # 盘前/盘后/夜间/非交易日
    NO_REFERENCE = "no_reference"              # 没有合格成交 → 无法算参考价
    NO_PRIOR_CLOSE = "no_prior_close"          # 没有前收 → 无法定档
    DISABLED = "disabled"
    ERROR = "error"


#: MWCB 三档（S&P 500 相对前一交易日收盘）
MWCB_LEVELS: Dict[int, Dict[str, Any]] = {
    1: {"threshold_pct": 0.07, "halt_minutes": 15, "close_for_day": False},
    2: {"threshold_pct": 0.13, "halt_minutes": 15, "close_for_day": False},
    3: {"threshold_pct": 0.20, "halt_minutes": 0, "close_for_day": True},
}

#: LULD 官方带宽（LULD Plan Table 1）
LULD_TIER1_PCT = 0.05
LULD_TIER2_PCT = 0.10
LULD_MID_PCT = 0.20
LULD_LOW_ABS = 0.15
LULD_LOW_PCT = 0.75
LULD_OPEN_CLOSE_PCT = 0.10        # 已废弃：仅保留常量名兼容
LULD_MAX_DOUBLINGS = 2

#: 默认计时参数
DEFAULT_LULD_PAUSE_MINUTES = 5.0
DEFAULT_MWCB_HALT_MINUTES = 15.0
DEFAULT_MWCB_CUTOFF_MINUTES = 35.0     # 收市前 35 分钟内不再触发 L1/L2
DEFAULT_LULD_CLOSE_WINDOW_MINUTES = 25.0
DEFAULT_LIMIT_STATE_SECONDS = 15.0


class MarketStatusError(ValueError):
    pass


# ====================================================================== #
# 交易日边界（由调用方按真实日历提供）
# ====================================================================== #
@dataclass(frozen=True)
class DayBounds:
    """某一交易日的**实际**开盘/收盘时刻（交易所当地时区，带 tzinfo）。

    早收市日只需把 ``close_at`` 设成 13:00 —— 所有"收市前 N 分钟"的判定
    自然跟着变，不需要在规则里写死 15:35 / 15:25。
    """

    open_at: datetime
    close_at: datetime
    is_trading_day: bool = True
    note: str = ""

    def __post_init__(self) -> None:
        if self.open_at.tzinfo is None or self.close_at.tzinfo is None:
            raise MarketStatusError("DayBounds 必须带时区")
        if self.close_at <= self.open_at:
            raise MarketStatusError("close_at 必须晚于 open_at")

    def in_regular(self, dt: datetime) -> bool:
        if not self.is_trading_day:
            return False
        return self.open_at <= dt <= self.close_at

    def minutes_to_close(self, dt: datetime) -> float:
        return (self.close_at - dt).total_seconds() / 60.0

    def minutes_since_open(self, dt: datetime) -> float:
        return (dt - self.open_at).total_seconds() / 60.0

    def as_dict(self) -> Dict[str, Any]:
        return {"open_at": self.open_at.isoformat(), "close_at": self.close_at.isoformat(),
                "is_trading_day": self.is_trading_day, "note": self.note}


# ====================================================================== #
# 参考价：最近 5 分钟合格成交
# ====================================================================== #
@dataclass(frozen=True)
class Trade:
    price: float
    ts: datetime

    def __post_init__(self) -> None:
        if self.ts.tzinfo is None:
            raise MarketStatusError("Trade.ts 必须带时区")
        if self.price <= 0:
            raise MarketStatusError("Trade.price 必须为正")


class RollingReferencePrice:
    """官方参考价：最近 5 分钟**合格成交**的算术平均；移动 ≥1% 才更新。

    取不到任何合格成交时返回 ``None`` —— 调用方必须据此判 ``NO_REFERENCE``，
    **不得**退回 last / prev_close。
    """

    def __init__(self, window_minutes: float = 5.0, update_threshold_pct: float = 0.01,
                 min_trades: int = 1):
        self.window = timedelta(minutes=float(window_minutes))
        self.update_threshold = float(update_threshold_pct)
        self.min_trades = int(min_trades)
        self._trades: Dict[str, List[Trade]] = {}
        self._ref: Dict[str, float] = {}

    def add(self, symbol: str, trade: Trade, *, in_regular: bool) -> Optional[float]:
        if not in_regular:
            return self._ref.get(symbol.upper())   # 常规时段外不计入参考价
        sym = symbol.upper()
        bucket = self._trades.setdefault(sym, [])
        bucket.append(trade)
        cutoff = trade.ts - self.window
        self._trades[sym] = [t for t in bucket if t.ts >= cutoff]
        return self.current(sym)

    def current(self, symbol: str) -> Optional[float]:
        sym = symbol.upper()
        trades = self._trades.get(sym) or []
        if len(trades) < self.min_trades:
            return None
        avg = sum(t.price for t in trades) / len(trades)
        prev = self._ref.get(sym)
        if prev is None or abs(avg - prev) / prev >= self.update_threshold:
            self._ref[sym] = avg
            return avg
        return prev

    def sample_count(self, symbol: str) -> int:
        return len(self._trades.get(symbol.upper()) or [])

    def seed(self, symbol: str, price: float) -> None:
        """开盘首个参考价（开盘价或开盘报价中点），由调用方提供。"""
        if price <= 0:
            raise MarketStatusError("参考价必须为正")
        self._ref[symbol.upper()] = float(price)

    def reset(self, symbol: Optional[str] = None) -> None:
        if symbol is None:
            self._trades.clear()
            self._ref.clear()
        else:
            self._trades.pop(symbol.upper(), None)
            self._ref.pop(symbol.upper(), None)


# ====================================================================== #
# 价格带
# ====================================================================== #
def luld_band_width(prior_close: float, *, tier: int = 1,
                    doubled: bool = False) -> Tuple[float, str]:
    """返回 ``(宽度, 单位)``；单位 ``"pct"`` 为比例，``"abs"`` 为绝对美元。"""
    p = float(prior_close)
    if p <= 0:
        raise MarketStatusError(f"前收价必须为正: {prior_close}")
    factor = 2.0 if doubled else 1.0
    if p > 3.0:
        base = LULD_TIER1_PCT if tier == 1 else LULD_TIER2_PCT
        return base * factor, "pct"
    if p >= 0.75:
        return LULD_MID_PCT * factor, "pct"
    return min(LULD_LOW_ABS * factor, p * LULD_LOW_PCT * factor), "abs"


def luld_doubled(tier: int, reference_price: float, minutes_to_close: float,
                 close_window_minutes: float = DEFAULT_LULD_CLOSE_WINDOW_MINUTES
                 ) -> Tuple[bool, str]:
    """是否适用**双倍**带宽（Amendment 18）。

    * 开盘 9:30–9:45：**一律不双倍**（Amendment 18 已取消开盘放宽）
    * 收市前 ``close_window_minutes`` 分钟内：
        - Tier1 → 双倍
        - Tier2 且参考价 ≤ $3.00 → 双倍
        - Tier2 且参考价 > $3.00 → **单倍**
    """
    if minutes_to_close > close_window_minutes:
        return False, "非收市窗口"
    if tier == 1:
        return True, "收市前窗口：Tier1 双倍"
    if reference_price <= 3.0:
        return True, "收市前窗口：Tier2 且 ≤ $3.00 双倍"
    return False, "收市前窗口：Tier2 且 > $3.00 保持单倍"


@dataclass(frozen=True)
class LULDBand:
    symbol: str
    reference_price: float
    upper: float
    lower: float
    band_pct: float
    doubling: int
    tier: int
    computed_at: datetime
    band_abs: Optional[float] = None
    doubled: bool = False
    reason: str = ""

    @property
    def width(self) -> float:
        return self.upper - self.lower

    def contains(self, price: float) -> bool:
        return self.lower <= price <= self.upper

    def as_dict(self) -> Dict[str, Any]:
        return {"symbol": self.symbol, "reference_price": self.reference_price,
                "upper": self.upper, "lower": self.lower, "band_pct": self.band_pct,
                "doubling": self.doubling, "tier": self.tier, "doubled": self.doubled,
                "band_abs": self.band_abs, "reason": self.reason,
                "computed_at": self.computed_at.isoformat()}


# ====================================================================== #
# 事件与状态
# ====================================================================== #
@dataclass
class StatusEvent:
    """状态变更事件（可审计）。``version`` 单调递增，用于丢弃乱序事件。"""

    version: int
    kind: HaltKind
    state: MarketState
    triggered_at: datetime
    source: str = ""
    expected_resume_at: Optional[datetime] = None
    recovery_notice_at: Optional[datetime] = None
    note: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"version": self.version, "kind": self.kind.value,
                "state": self.state.value,
                "triggered_at": self.triggered_at.isoformat(),
                "source": self.source,
                "expected_resume_at": self.expected_resume_at.isoformat()
                if self.expected_resume_at else None,
                "recovery_notice_at": self.recovery_notice_at.isoformat()
                if self.recovery_notice_at else None,
                "note": self.note}


@dataclass
class SymbolStatus:
    symbol: str
    state: MarketState = MarketState.UNKNOWN
    reason: HaltReason = HaltReason.UNKNOWN
    kind: Optional[HaltKind] = None
    halted_at: Optional[datetime] = None
    expected_resume_at: Optional[datetime] = None
    recovery_notice_at: Optional[datetime] = None
    band: Optional[LULDBand] = None
    note: str = ""
    updated_at: Optional[datetime] = None
    version: int = 0
    events: List[StatusEvent] = field(default_factory=list)
    #: LULD Limit State 起始时刻（15 秒判定用）
    limit_state_since: Optional[datetime] = None
    #: 该状态属于哪个交易场所（多交易所隔离）
    exchange: str = "*"

    @property
    def tradable(self) -> bool:
        return self.state == MarketState.OPEN

    def as_dict(self) -> Dict[str, Any]:
        return {"symbol": self.symbol, "state": self.state.value,
                "reason": self.reason.value,
                "kind": self.kind.value if self.kind else None,
                "halted_at": self.halted_at.isoformat() if self.halted_at else None,
                "expected_resume_at": self.expected_resume_at.isoformat()
                if self.expected_resume_at else None,
                "recovery_notice_at": self.recovery_notice_at.isoformat()
                if self.recovery_notice_at else None,
                "band": self.band.as_dict() if self.band else None,
                "note": self.note, "version": self.version,
                "tradable": self.tradable,
                "events": [e.as_dict() for e in self.events[-5:]]}


@dataclass
class MarketDecision:
    allowed: bool
    state: MarketState
    reason: str = ""
    warning: str = ""
    band: Optional[LULDBand] = None
    stage: str = "intent"

    def as_dict(self) -> Dict[str, Any]:
        return {"allowed": self.allowed, "state": self.state.value,
                "reason": self.reason, "warning": self.warning, "stage": self.stage,
                "band": self.band.as_dict() if self.band else None}


@dataclass
class LULDResult:
    """一次 LULD 计算的结果 —— 明确区分「不可用」与「未触发」。"""

    availability: LULDAvailability
    band: Optional[LULDBand] = None
    state: MarketState = MarketState.UNKNOWN
    note: str = ""

    @property
    def band_available(self) -> bool:
        return self.availability == LULDAvailability.AVAILABLE

    def as_dict(self) -> Dict[str, Any]:
        return {"availability": self.availability.value,
                "band": self.band.as_dict() if self.band else None,
                "state": self.state.value, "note": self.note}


@dataclass
class MWCBState:
    level: int = 0
    triggered_at: Optional[datetime] = None
    resume_at: Optional[datetime] = None
    closed_for_day: bool = False
    reference_price: float = 0.0
    last_price: float = 0.0
    note: str = ""
    triggered_levels_today: List[int] = field(default_factory=list)


# ====================================================================== #
# 服务
# ====================================================================== #
class MarketStatusService:
    """市场状态机。所有输入由调用方注入；无数据即 ``UNKNOWN``（不假装正常）。"""

    def __init__(self, *, max_quote_age_seconds: float = 60.0,
                 tier2_symbols: Optional[Sequence[str]] = None,
                 luld_enabled: bool = True,
                 luld_pause_minutes: float = DEFAULT_LULD_PAUSE_MINUTES,
                 mwcb_halt_minutes: float = DEFAULT_MWCB_HALT_MINUTES,
                 mwcb_cutoff_minutes: float = DEFAULT_MWCB_CUTOFF_MINUTES,
                 luld_close_window_minutes: float = DEFAULT_LULD_CLOSE_WINDOW_MINUTES,
                 limit_state_seconds: float = DEFAULT_LIMIT_STATE_SECONDS,
                 reference_price: Optional[RollingReferencePrice] = None,
                 # 兼容旧参数名：仅作为 LULD 暂停时长的别名
                 halt_expiry_minutes: Optional[float] = None):
        self.max_quote_age = float(max_quote_age_seconds)
        self.tier2_symbols = {s.upper() for s in (tier2_symbols or [])}
        self.luld_enabled = bool(luld_enabled)
        # P0-1：三个计时器彻底分开
        self.luld_pause = timedelta(minutes=float(
            halt_expiry_minutes if halt_expiry_minutes is not None else luld_pause_minutes))
        self.mwcb_halt = timedelta(minutes=float(mwcb_halt_minutes))
        self.mwcb_cutoff_minutes = float(mwcb_cutoff_minutes)
        self.luld_close_window = float(luld_close_window_minutes)
        self.limit_state_window = timedelta(seconds=float(limit_state_seconds))
        self.reference = reference_price or RollingReferencePrice()

        self._symbols: Dict[str, SymbolStatus] = {}
        self._last_trade: Dict[str, Tuple[float, datetime]] = {}
        self._bands: Dict[str, LULDBand] = {}
        self._breaches: Dict[str, int] = {}
        #: 真正**进入暂停**的次数（决定重新开盘带宽翻倍级数）
        self._pauses: Dict[str, int] = {}
        self._version = 0
        #: US-LULD 完整生命周期（可选接线；接上后 5 分钟到期 ≠ 恢复）
        self.lifecycles: Dict[str, Any] = {}
        self._mwcb = MWCBState()
        self._index_ref: float = 0.0
        self._index_last: float = 0.0
        self._index_ts: Optional[datetime] = None
        self._pending_resume_notices: Dict[str, datetime] = {}

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    @staticmethod
    def _key(symbol: str, exchange: str = "*") -> str:
        """状态键 = symbol|exchange。

        不同交易所**必须**各自维护状态：一家恢复不得影响另一家。
        ``"*"`` 是"未指定交易所"的显式桶（兼容旧的单所调用）。
        """
        return f"{str(symbol).upper()}|{str(exchange or '*').lower()}"

    def _st(self, symbol: str, exchange: str = "*") -> SymbolStatus:
        k = self._key(symbol, exchange)
        st = self._symbols.get(k)
        if st is None:
            st = SymbolStatus(symbol=str(symbol).upper(), exchange=str(exchange).lower())
            self._symbols[k] = st
        return st

    #: 由**显式事件**写入的状态（停牌 / 暂停 / 竞价 / Limit State）。
    #: 只有这些状态参与跨交易所桶聚合 —— 行情新鲜度是标的级（合并行情），不参与。
    EVENT_STATES: Tuple[MarketState, ...] = (
        MarketState.LULD_LIMIT_STATE,
        MarketState.LULD_PAUSED,
        MarketState.HALTED,
        MarketState.MWCB_PAUSED,
        MarketState.MWCB_CLOSED,
    )

    #: 状态严格度（越大越严）。用于"场所未知"时必须取最保守值。
    SEVERITY: Dict[MarketState, int] = {
        MarketState.OPEN: 0,
        MarketState.LULD_LIMIT_STATE: 1,
        MarketState.UNKNOWN: 2,
        MarketState.DATA_STALE: 3,
        MarketState.LULD_PAUSED: 4,
        MarketState.HALTED: 5,
        MarketState.MWCB_PAUSED: 6,
        MarketState.MWCB_CLOSED: 7,
    }

    def _bucket_keys(self, symbol: str, exchange: str) -> List[str]:
        """返回该查询应当参考的状态桶。

        * ``exchange="*"``（**场所未知**）→ 该标的**所有**场所桶
          理由：未知场所时无法排除任何一家交易所的停牌，必须保守。
        * ``exchange="nasdaq"`` → 仅 ``sym|nasdaq`` + ``sym|*``
          理由：``"*"`` 桶是**标的级**事实（不区分场所的停牌），对所有场所生效；
          但**不得**参考其它具体场所（否则隔离失效）。
        """
        sym = str(symbol).upper()
        ex = str(exchange or "*").lower()
        prefix = f"{sym}|"
        if ex == "*":
            return [k for k in list(self._symbols) if k.startswith(prefix)]
        keys = [self._key(sym, ex)]
        star = self._key(sym, "*")
        if star != keys[0]:
            keys.append(star)
        return keys

    def _strictest_event(self, symbol: str, exchange: str,
                         exclude_key: str) -> Optional[SymbolStatus]:
        """找出比本单位更严格的**显式事件**状态；没有则返回 ``None``。"""
        worst: Optional[SymbolStatus] = None
        for k in self._bucket_keys(symbol, exchange):
            if k == exclude_key:
                continue
            cand = self._symbols.get(k)
            if cand is None or cand.state not in self.EVENT_STATES:
                continue
            if worst is None or self.SEVERITY[cand.state] > self.SEVERITY[worst.state]:
                worst = cand
        return worst

    def _fuse(self, st: SymbolStatus, symbol: str, exchange: str) -> SymbolStatus:
        """把更严格的跨桶事件状态叠加到查询结果上（返回副本，不改存储对象）。

        **安全方向**：聚合只会让结果更严，绝不会把已停牌的标的"放宽"成可交易。
        """
        worst = self._strictest_event(symbol, exchange, self._key(symbol, exchange))
        if worst is None or self.SEVERITY[worst.state] <= self.SEVERITY[st.state]:
            return st
        out = replace(st)
        out.state = worst.state
        out.reason = worst.reason
        out.kind = worst.kind
        out.halted_at = worst.halted_at
        out.expected_resume_at = worst.expected_resume_at
        out.recovery_notice_at = worst.recovery_notice_at
        out.limit_state_since = worst.limit_state_since
        if out.band is None and worst.band is not None:
            out.band = worst.band
        src = worst.exchange or "*"
        out.note = (f"{worst.state.value} 来自场所 {src}（查询场所 "
                    f"{str(exchange or '*').lower()}）；{worst.note or ''}").strip()
        return out

    def _emit(self, st: SymbolStatus, kind: HaltKind, state: MarketState,
              ts: datetime, *, source: str = "", note: str = "",
              expected_resume_at: Optional[datetime] = None,
              recovery_notice_at: Optional[datetime] = None) -> None:
        self._version += 1
        st.version = self._version
        ev = StatusEvent(version=self._version, kind=kind, state=state,
                         triggered_at=ts, source=source,
                         expected_resume_at=expected_resume_at,
                         recovery_notice_at=recovery_notice_at, note=note)
        st.events.append(ev)
        st.state = state
        st.kind = kind
        st.updated_at = ts
        st.note = note
        st.expected_resume_at = expected_resume_at
        if recovery_notice_at is not None:
            st.recovery_notice_at = recovery_notice_at

    def _tier(self, symbol: str) -> int:
        return 2 if symbol.upper() in self.tier2_symbols else 1

    # ------------------------------------------------------------------ #
    # MWCB
    # ------------------------------------------------------------------ #
    def update_index(self, price: float, ts: datetime, *,
                     bounds: Optional[DayBounds] = None,
                     at_or_after_325: Optional[bool] = None) -> int:
        """更新基准指数；返回触发的级别（0 = 未触发）。

        L1/L2：**收市前 35 分钟以外**才停市 15 分钟；到/过该截止时间不停市。
        L3：任何时间 → 当日剩余时间停市。
        ``at_or_after_325`` 为兼容旧接口的显式开关（等价于"已进入截止窗口"）。
        """
        self._index_ts = ts
        if self._index_ref <= 0:
            self._index_ref = float(price)
        self._index_last = float(price)
        if self._index_ref <= 0 or self._mwcb.closed_for_day:
            return 0

        # 是否已进入"不再触发 L1/L2"的窗口
        if bounds is not None:
            try:
                mins = bounds.minutes_to_close(ts)
                in_cutoff = mins <= self.mwcb_cutoff_minutes
            except Exception:
                in_cutoff = True          # 边界算不出来 → 保守：不触发
        elif at_or_after_325 is not None:
            in_cutoff = bool(at_or_after_325)
        else:
            in_cutoff = True              # 无任何时间信息 → 不擅自停市

        drop = (self._index_ref - self._index_last) / self._index_ref
        level = 0
        for lv in (3, 2, 1):
            if drop >= MWCB_LEVELS[lv]["threshold_pct"] and lv > self._mwcb.level:
                level = lv
                break
        if not level:
            return 0

        # 每级每日只触发一次
        if level in self._mwcb.triggered_levels_today:
            return 0

        cfg = MWCB_LEVELS[level]
        if level in (1, 2) and in_cutoff:
            self._mwcb.level = max(self._mwcb.level, level)
            self._mwcb.triggered_at = ts
            self._mwcb.reference_price = self._index_ref
            self._mwcb.last_price = self._index_last
            self._mwcb.note = f"L{level} 在收市前 {self.mwcb_cutoff_minutes:.0f} 分钟内触发，不停市"
            self._mwcb.triggered_levels_today.append(level)
            return 0

        self._mwcb.level = level
        self._mwcb.triggered_at = ts
        self._mwcb.reference_price = self._index_ref
        self._mwcb.last_price = self._index_last
        self._mwcb.triggered_levels_today.append(level)
        if cfg["close_for_day"]:
            self._mwcb.closed_for_day = True
            self._mwcb.resume_at = None
            self._mwcb.note = "L3：当日剩余交易关闭（不会自动恢复）"
        else:
            self._mwcb.resume_at = ts + self.mwcb_halt
            self._mwcb.note = f"L{level}：暂停 {cfg['halt_minutes']} 分钟"
        return level

    def mwcb_status(self) -> Dict[str, Any]:
        return {"level": self._mwcb.level,
                "triggered_at": self._mwcb.triggered_at.isoformat()
                if self._mwcb.triggered_at else None,
                "resume_at": self._mwcb.resume_at.isoformat()
                if self._mwcb.resume_at else None,
                "closed_for_day": self._mwcb.closed_for_day,
                "index_ref": self._index_ref, "index_last": self._index_last,
                "drop_pct": ((self._index_ref - self._index_last) / self._index_ref)
                if self._index_ref else 0.0,
                "triggered_levels_today": list(self._mwcb.triggered_levels_today),
                "note": self._mwcb.note}

    def reset_day(self) -> None:
        """跨交易日重置（MWCB 的 L1/L2 允许次日重新触发）。"""
        self._mwcb = MWCBState()
        self._index_ref = 0.0
        self._index_last = 0.0
        self._index_ts = None
        self._breaches.clear()

    # ------------------------------------------------------------------ #
    # LULD
    # ------------------------------------------------------------------ #
    def observe_trade(self, symbol: str, price: float, ts: datetime, *,
                      bounds: Optional[DayBounds],
                      prior_close: Optional[float] = None,
                      source: str = "",
                      exchange: str = "*") -> LULDResult:
        """官方 LULD 计算入口。

        要求 ``bounds``（真实交易日开收盘）与 ``prior_close``；缺任何一项都返回
        对应的 ``UNAVAILABLE`` 原因，**不退回 last/prev_close 硬算**。
        """
        sym = symbol.upper()
        self._last_trade[sym] = (float(price), ts)

        if not self.luld_enabled:
            return LULDResult(LULDAvailability.DISABLED, state=MarketState.OPEN,
                              note="LULD 已禁用")
        if bounds is None:
            return LULDResult(LULDAvailability.OUT_OF_SESSION,
                              state=MarketState.UNKNOWN,
                              note="缺少交易日边界，无法判断是否常规时段")
        if not bounds.in_regular(ts):
            return LULDResult(LULDAvailability.OUT_OF_SESSION,
                              state=self._st(sym, exchange).state
                              if self._st(sym, exchange).state != MarketState.UNKNOWN
                              else MarketState.OPEN,
                              note=f"{ts.isoformat()} 不在常规交易时段（LULD 不适用）")
        if prior_close is None or prior_close <= 0:
            return LULDResult(LULDAvailability.NO_PRIOR_CLOSE, state=MarketState.UNKNOWN,
                              note="缺少前收价，无法确定价格档位")

        # 官方：参考价取**此前** 5 分钟成交的均值，**不含当前这笔**。
        # 因此先算参考价，再把当前成交纳入窗口。
        ref = self.reference.current(sym)
        if ref is None:
            self.reference.add(sym, Trade(price=float(price), ts=ts), in_regular=True)
            return LULDResult(LULDAvailability.NO_REFERENCE, state=MarketState.UNKNOWN,
                              note=f"此前 5 分钟无合格成交（样本 "
                                   f"{self.reference.sample_count(sym)}）")

        tier = self._tier(sym)
        doubled, why = luld_doubled(tier, ref, bounds.minutes_to_close(ts),
                                    self.luld_close_window)
        # 关键：翻倍级数只能由**已发生的暂停**决定。
        # 若用「Limit State 进入次数」当级数，带宽会在下一笔成交时自动变宽，
        # 使价格"回到带内"而自我解除 Limit State —— 永远进不了暂停。
        doubling = min(self._pauses.get(sym, 0), LULD_MAX_DOUBLINGS)
        band = self._compute_band(sym, ref, doubling, doubled=doubled,
                                  prior_close=prior_close, reason=why)
        self._bands[sym] = band

        st = self._st(sym, exchange)
        st.band = band
        inside = band.contains(float(price))

        # 官方：**只有带内成交**才进入参考价样本。
        # 若把越界价也算进 5 分钟均值，参考价会被拉向越界方向、带宽随之变宽，
        # 下一笔就越界变"带内"，Limit State 会自我解除 —— 暂停永远触发不了。
        if inside:
            self.reference.add(sym, Trade(price=float(price), ts=ts), in_regular=True)

        # 已在暂停中：只看是否到期
        if st.state == MarketState.LULD_PAUSED:
            return LULDResult(LULDAvailability.AVAILABLE, band=band, state=st.state,
                              note="LULD 暂停中")

        if inside:
            if st.state == MarketState.LULD_LIMIT_STATE:
                st.limit_state_since = None
                self._emit(st, HaltKind.LULD, MarketState.OPEN, ts, source=source,
                           note="价格回到带内（15 秒内），未进入暂停")
            elif st.state in (MarketState.UNKNOWN, MarketState.DATA_STALE):
                self._emit(st, HaltKind.LULD, MarketState.OPEN, ts, source=source,
                           note="价格在带内")
            return LULDResult(LULDAvailability.AVAILABLE, band=band, state=st.state)

        # 越界
        if st.limit_state_since is None:
            st.limit_state_since = ts
            self._emit(st, HaltKind.LULD, MarketState.LULD_LIMIT_STATE, ts, source=source,
                       note=f"触及价格带 [{band.lower:.4f},{band.upper:.4f}]，进入 Limit State")
            return LULDResult(LULDAvailability.AVAILABLE, band=band,
                              state=MarketState.LULD_LIMIT_STATE,
                              note="Limit State 开始计时（15 秒）")

        elapsed = (ts - st.limit_state_since).total_seconds()
        if elapsed < self.limit_state_window.total_seconds():
            return LULDResult(LULDAvailability.AVAILABLE, band=band,
                              state=MarketState.LULD_LIMIT_STATE,
                              note=f"Limit State 已持续 {elapsed:.1f}s < 15s")

        # 超过 15 秒 → 交易暂停（5 分钟）
        self._breaches[sym] = self._breaches.get(sym, 0) + 1
        self._pauses[sym] = self._pauses.get(sym, 0) + 1
        st.limit_state_since = None
        self._emit(st, HaltKind.LULD, MarketState.LULD_PAUSED, ts, source=source,
                   expected_resume_at=ts + self.luld_pause,
                   note=(f"Limit State 超过 {self.limit_state_window.total_seconds():.0f}s，"
                         f"暂停 {self.luld_pause.total_seconds()/60:.0f} 分钟"))
        return LULDResult(LULDAvailability.AVAILABLE, band=band,
                          state=MarketState.LULD_PAUSED, note="已进入 LULD 交易暂停")

    def update_reference(self, symbol: str, price: float, *,
                         prior_close: Optional[float] = None,
                         doubled: bool = False,
                         note: str = "manual") -> None:
        """手动/回放模式：直接设定参考价与是否双倍（**不用于 Engine 主路径**）。"""
        if price <= 0:
            raise MarketStatusError(f"参考价必须为正: {price}")
        sym = symbol.upper()
        self.reference.seed(sym, float(price))
        self._bands[sym] = self._compute_band(
            sym, float(price), min(self._breaches.get(sym, 0), LULD_MAX_DOUBLINGS),
            doubled=doubled, prior_close=prior_close, reason=f"manual:{note}")

    def on_quote(self, symbol: str, price: float, ts: datetime,
                 *, doubled: bool = False, prior_close: Optional[float] = None,
                 in_open_close_window: Optional[bool] = None,
                 exchange: str = "*"
                 ) -> Optional[LULDBand]:
        """回放/测试用的简化入口（**假定调用方已确认在常规时段**）。

        生产路径请用 :meth:`observe_trade`（会强制校验常规时段与 5 分钟参考价）。
        """
        if in_open_close_window is not None and in_open_close_window:
            doubled = True
        sym = symbol.upper()
        self._last_trade[sym] = (float(price), ts)
        st = self._st(sym, exchange)
        st.updated_at = ts

        band = self._bands.get(sym)
        if band is None:
            st.state = MarketState.UNKNOWN
            st.note = "缺少 LULD 参考价"
            return None

        if band.contains(price):
            if st.state in (MarketState.LULD_PAUSED, MarketState.LULD_LIMIT_STATE):
                self._emit(st, HaltKind.LULD, MarketState.OPEN, ts,
                           note="价格回到带内")
            elif st.state in (MarketState.UNKNOWN, MarketState.DATA_STALE):
                self._emit(st, HaltKind.LULD, MarketState.OPEN, ts, note="价格在带内")
            st.band = band
            return band

        # 越界 → 直接暂停（简化模式不做 15 秒判定）
        self._breaches[sym] = self._breaches.get(sym, 0) + 1
        doubling = min(self._breaches[sym] - 1, LULD_MAX_DOUBLINGS)
        new_band = self._compute_band(sym, band.reference_price, doubling,
                                      doubled=doubled, prior_close=prior_close,
                                      reason="replay")
        self._bands[sym] = new_band
        self._emit(st, HaltKind.LULD, MarketState.LULD_PAUSED, ts,
                   expected_resume_at=ts + self.luld_pause,
                   note=f"越出带 [{new_band.lower:.4f},{new_band.upper:.4f}]")
        st.band = new_band
        return new_band

    # ------------------------------------------------------------------ #
    # 交易所停牌
    # ------------------------------------------------------------------ #
    def halt_symbol(self, symbol: str, reason: HaltReason = HaltReason.UNKNOWN,
                    ts: Optional[datetime] = None,
                    resume_at: Optional[datetime] = None,
                    note: str = "", source: str = "exchange",
                    kind: HaltKind = HaltKind.EXCHANGE,
                    exchange: str = "*") -> SymbolStatus:
        """记录一次停牌。

        **交易所停牌不会自动恢复**：``resume_at`` 只是"预计恢复"，仅用于展示；
        必须在收到恢复通知后调用 :meth:`notify_resume`。
        """
        sym = symbol.upper()
        st = self._st(sym, exchange)
        now = ts or timebase.utcnow()
        self._emit(st, kind, MarketState.HALTED, now, source=source,
                   expected_resume_at=resume_at,
                   note=note or f"停牌（{reason.value}）")
        st.reason = reason
        st.halted_at = now
        return st

    def notify_resume(self, symbol: str, ts: Optional[datetime] = None,
                      source: str = "exchange", note: str = "收到恢复通知",
                      exchange: str = "*") -> SymbolStatus:
        """收到真实恢复通知 → 解除停牌（**唯一**解除交易所停牌的方式）。"""
        sym = symbol.upper()
        st = self._st(sym, exchange)
        now = ts or timebase.utcnow()
        self._emit(st, HaltKind.EXCHANGE, MarketState.OPEN, now, source=source,
                   recovery_notice_at=now, note=note)
        st.reason = HaltReason.UNKNOWN
        st.halted_at = None
        st.expected_resume_at = None
        st.limit_state_since = None
        self._breaches[sym] = 0
        return st

    def record_trade(self, symbol: str, price: float, ts: datetime,
                     *, exchange: str = "*") -> None:
        """仅登记"最近成交时间/价格"（用于新鲜度判定），不参与 LULD 计算。

        数据源在没有完整 LULD 输入（无 bounds / 无前收）时应调用它。
        """
        self._last_trade[symbol.upper()] = (float(price), ts)
        st = self._st(symbol, exchange)
        st.updated_at = ts

    #: 旧接口别名（语义同 notify_resume）
    def resume_symbol(self, symbol: str, ts: Optional[datetime] = None,
                      note: str = "交易所恢复交易") -> SymbolStatus:
        return self.notify_resume(symbol, ts, note=note)

    # ------------------------------------------------------------------ #
    # 查询与三阶段裁决
    # ------------------------------------------------------------------ #
    def ensure_lifecycle(self, symbol: str, *, exchange: Any = None,
                         tier: Optional[int] = None):
        """为标的创建/取回 LULD 生命周期对象（接线用）。"""
        from .luld import Exchange as _Ex, LuldLifecycle

        sym = symbol.upper()
        lc = self.lifecycles.get(sym)
        if lc is None:
            ex = exchange if isinstance(exchange, _Ex) else _Ex.UNKNOWN
            lc = LuldLifecycle(sym, exchange=ex,
                               tier=tier if tier is not None else self._tier(sym))
            self.lifecycles[sym] = lc
        return lc

    def lifecycle_phase(self, symbol: str) -> Optional[str]:
        lc = self.lifecycles.get(symbol.upper())
        return lc.phase.value if lc is not None else None

    def status(self, symbol: str, now: Optional[datetime] = None, *,
               exchange: str = "*") -> SymbolStatus:
        """查询状态。**先解析本单位，再叠加更严格的跨场所事件状态**。"""
        now = now or timebase.utcnow()
        st = self._status_local(symbol, now, exchange=exchange)
        return self._fuse(st, symbol, exchange)

    def _status_local(self, symbol: str, now: datetime, *,
                      exchange: str = "*") -> SymbolStatus:
        sym = symbol.upper()
        st = self._st(sym, exchange)

        # 1) MWCB L3：当日关闭，永不自动恢复
        if self._mwcb.closed_for_day:
            out = SymbolStatus(symbol=sym, state=MarketState.MWCB_CLOSED,
                               kind=HaltKind.MWCB, reason=HaltReason.MWCB,
                               note=self._mwcb.note)
            return out

        # 2) MWCB L1/L2 暂停期内
        if self._mwcb.level in (1, 2) and self._mwcb.resume_at and now < self._mwcb.resume_at:
            out = SymbolStatus(symbol=sym, state=MarketState.MWCB_PAUSED,
                               kind=HaltKind.MWCB, reason=HaltReason.MWCB,
                               expected_resume_at=self._mwcb.resume_at,
                               note=self._mwcb.note)
            return out

        # 3) 交易所停牌：**只认恢复通知**，过期时间不作数
        if st.state == MarketState.HALTED:
            if st.expected_resume_at and now >= st.expected_resume_at and \
                    st.recovery_notice_at is None:
                st.note = (f"预计恢复时间已过（{st.expected_resume_at.isoformat()}）"
                           f"但未收到恢复通知 —— 保持停牌")
            return st

        # 4) LULD 暂停：**5 分钟到期不等于恢复**
        #    到期后必须进入重新开盘竞价；无竞价/官方恢复数据时保持不可交易。
        if st.state == MarketState.LULD_PAUSED and st.expected_resume_at \
                and now >= st.expected_resume_at:
            lc = self.lifecycles.get(sym)
            if lc is not None:
                try:
                    lc.start_auction(now, source="gateway")
                except Exception as exc:      # 竞价启动失败 → 保持不可交易
                    st.note = f"最短暂停已满，但竞价无法启动：{exc}"
                    return st
                if st.state == MarketState.LULD_PAUSED:
                    st.note = "最短暂停已满，进入重新开盘竞价（不可连续交易）"
                return st
            # 未接线生命周期 → 保守：也不自动恢复
            st.note = "5 分钟最短暂停已满；无竞价/官方恢复数据，保持不可交易"
            return st

        # 5) 行情陈旧
        if st.state in (MarketState.OPEN, MarketState.UNKNOWN):
            last = self._last_trade.get(sym)
            if last is None:
                st.state = MarketState.UNKNOWN
                if not st.note:
                    st.note = "无行情数据"
            else:
                _, ts = last
                if (now - ts).total_seconds() > self.max_quote_age:
                    st.state = MarketState.DATA_STALE
                    st.note = (f"行情过期 {(now - ts).total_seconds():.0f}s > "
                               f"{self.max_quote_age:.0f}s")
                elif st.state == MarketState.UNKNOWN:
                    # 有**新鲜**行情且无停牌/暂停 → 正常可交易
                    st.state = MarketState.OPEN
                    st.note = ""
        return st

    #: 允许**连续交易**的市场状态（LIMIT_STATE 也允许，但受价格带约束）
    CONTINUOUS_OK = (MarketState.OPEN,)

    def _lifecycle_ok(self, symbol: str, exchange: str) -> Tuple[bool, str]:
        lc = self.lifecycles.get(str(symbol).upper())
        if lc is None:
            return True, ""
        if not lc.tradable:
            return False, f"LULD 生命周期={lc.phase.value}"
        return True, ""

    def intent_gate(self, symbol: str, *, is_exit: bool = False,
                    now: Optional[datetime] = None,
                    exchange: str = "*") -> MarketDecision:
        """**阶段 A：意图**。风险退出不因市场状态被否掉，只带告警。"""
        st = self.status(symbol, now, exchange=exchange)
        if st.state == MarketState.OPEN:
            return MarketDecision(True, st.state, "", "", st.band, stage="intent")
        if st.state == MarketState.LULD_LIMIT_STATE:
            return MarketDecision(True, st.state, "", "标的处于 Limit State："
                                  "仅允许价格带内成交", st.band, stage="intent")
        if is_exit:
            return MarketDecision(
                True, st.state, "",
                f"{symbol} 当前 {st.state.value}（{st.note or st.reason.value}）；"
                f"保留退出意图，但订单可能无法成交", st.band, stage="intent")
        return MarketDecision(False, st.state,
                              f"{symbol} 市场状态={st.state.value}，禁止新开仓"
                              f"（{st.note or st.reason.value}）", "", st.band, stage="intent")

    def submit_gate(self, symbol: str, *, is_exit: bool = False,
                    now: Optional[datetime] = None,
                    exchange: str = "*",
                    price: Optional[float] = None) -> MarketDecision:
        """**阶段 B：提交**。

        * ``OPEN`` → 允许
        * ``LULD_LIMIT_STATE`` → 允许提交，但必须是**带内价格**（带外直接拒）
        * 暂停/竞价/待恢复 → 拒绝（退出亦然）
        """
        st = self.status(symbol, now, exchange=exchange)
        if st.state == MarketState.OPEN:
            ok, why = self._lifecycle_ok(symbol, exchange)
            if not ok:
                return MarketDecision(False, st.state,
                                      f"{symbol} {why}，不可提交连续交易订单",
                                      "", st.band, stage="submit")
            return MarketDecision(True, st.state, "", "", st.band, stage="submit")

        if st.state == MarketState.LULD_LIMIT_STATE:
            band = st.band
            if price is None:
                return MarketDecision(False, st.state,
                                      f"{symbol} Limit State：缺少委托价，无法校验价格带",
                                      "", band, stage="submit")
            ok, why = self._lifecycle_ok(symbol, exchange)
            if not ok:
                return MarketDecision(False, st.state,
                                      f"{symbol} {why}，不可提交（Limit State 状态桶与"
                                      f"生命周期不一致时以生命周期为准）",
                                      "", band, stage="submit")
            if band is not None and not band.contains(float(price)):
                return MarketDecision(
                    False, st.state,
                    f"{symbol} Limit State：价格 {price} 越出带 "
                    f"[{band.lower:.4f},{band.upper:.4f}]，拒绝",
                    "", band, stage="submit")
            return MarketDecision(True, st.state, "",
                                  "Limit State：带内价格允许提交", band, stage="submit")

        if st.state in (MarketState.DATA_STALE, MarketState.UNKNOWN):
            reason = f"{symbol} 市场状态无法确认（{st.state.value}），不可路由订单"
        else:
            reason = f"{symbol} 处于 {st.state.value}，交易所不接受常规订单"
        if is_exit:
            return MarketDecision(False, st.state, reason,
                                  "退出意图已保留；待恢复后重新提交", st.band, stage="submit")
        return MarketDecision(False, st.state, reason, "", st.band, stage="submit")

    def fill_allowed(self, symbol: str, now: Optional[datetime] = None, *,
                     price: Optional[float] = None,
                     exchange: str = "*") -> Tuple[bool, str, MarketState]:
        """**阶段 C：成交**（Paper Broker 自己再验一次，不依赖 Engine 事先检查）。

        * ``OPEN`` → 还需行情新鲜 + 生命周期可交易
        * ``LULD_LIMIT_STATE`` → 只允许**带内价格**成交
        * 其它（暂停/竞价/待恢复/未知/过期）→ 一律不允许
        """
        st = self.status(symbol, now, exchange=exchange)

        if st.state == MarketState.OPEN:
            ok, why = self._lifecycle_ok(symbol, exchange)
            if not ok:
                return False, f"{symbol} {why}，不允许连续交易成交", st.state
            return True, "", st.state

        if st.state == MarketState.LULD_LIMIT_STATE:
            band = st.band
            if band is None:
                return False, f"{symbol} Limit State 但无价格带，拒绝成交", st.state
            if price is None:
                return False, f"{symbol} Limit State：缺少成交价，无法校验价格带", st.state
            ok, why = self._lifecycle_ok(symbol, exchange)
            if not ok:
                return False, (f"{symbol} {why}，Limit State 状态桶与生命周期不一致，"
                               f"以生命周期为准，拒绝成交"), st.state
            if not band.contains(float(price)):
                return False, (f"{symbol} Limit State：成交价 {price} 越出带 "
                               f"[{band.lower:.4f},{band.upper:.4f}]，拒绝成交"), st.state
            return True, "", st.state

        return False, f"{symbol} 处于 {st.state.value}，不允许成交（{st.note or '—'}）", st.state

    #: 兼容旧名
    def evaluate(self, symbol: str, *, is_exit: bool = False,
                 now: Optional[datetime] = None) -> MarketDecision:
        return self.intent_gate(symbol, is_exit=is_exit, now=now)

    def snapshot(self) -> Dict[str, Any]:
        return {"mwcb": self.mwcb_status(),
                "symbols": {k: v.as_dict() for k, v in self._symbols.items()},
                "bands": {k: v.as_dict() for k, v in self._bands.items()},
                "luld_enabled": self.luld_enabled,
                "timers": {"luld_pause_minutes": self.luld_pause.total_seconds() / 60,
                           "mwcb_halt_minutes": self.mwcb_halt.total_seconds() / 60,
                           "mwcb_cutoff_minutes": self.mwcb_cutoff_minutes,
                           "luld_close_window_minutes": self.luld_close_window,
                           "limit_state_seconds": self.limit_state_window.total_seconds()}}

    # ------------------------------------------------------------------ #
    def _compute_band(self, symbol: str, ref: float, doubling: int, *,
                      doubled: bool = False, prior_close: Optional[float] = None,
                      reason: str = "") -> LULDBand:
        tier = self._tier(symbol)
        bucket = float(prior_close) if (prior_close and prior_close > 0) else float(ref)
        width, unit = luld_band_width(bucket, tier=tier, doubled=doubled)
        scale = 2 ** min(int(doubling), LULD_MAX_DOUBLINGS)
        if unit == "pct":
            pct = width * scale
            upper = round(ref * (1 + pct), 6)
            lower = round(max(0.0, ref * (1 - pct)), 6)
            band_abs = None
        else:
            band_abs = width * scale
            pct = (band_abs / ref) if ref > 0 else 0.0
            upper = round(ref + band_abs, 6)
            lower = round(max(0.0, ref - band_abs), 6)
        return LULDBand(symbol=symbol.upper(), reference_price=ref, upper=upper,
                        lower=lower, band_pct=pct,
                        doubling=min(int(doubling), LULD_MAX_DOUBLINGS),
                        tier=tier, computed_at=timebase.utcnow(),
                        band_abs=band_abs, doubled=bool(doubled), reason=reason)
