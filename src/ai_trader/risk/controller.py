"""独立风控模块 —— 系统的唯一放行闸门。

设计契约
--------
1. **AI 与策略引擎无权下单**，它们只能产出 :class:`OrderIntent`。
2. 只有 :meth:`RiskController.evaluate` 返回 ``approved=True`` 的意图才能进入券商层。
3. 风控的任何异常都按"拒绝"处理（fail-closed），绝不 fail-open。
4. 每个检查项都留下可审计的结构化结果（name / ok / detail）。
5. 紧急停止（kill switch）一旦置位，任何方向的新开仓与加仓全部拒绝。

检查项一览（顺序即执行顺序，命中即短路，但全部结果都会记录）
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from ..broker.base import Position, Quote
from ..config import AppConfig, SafetyGate
from ..market.clock import ClockError, build_clock
from ..market.rules import DEFAULT_SESSIONS, MarketRuleSet
from ..safety.halt import HaltStoreError


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class Portfolio:
    """风控看到的账户快照（券商层聚合后的只读视图）。"""

    currency: str = "HKD"
    equity: float = 0.0
    cash: float = 0.0
    positions: List[Position] = field(default_factory=list)
    day_pnl: float = 0.0
    day_trades: int = 0
    day_orders: int = 0
    #: 券商是否真的提供了日内盈亏。False 时单日亏损熔断必须判为"无法判定"。
    day_pnl_known: bool = True
    #: symbol -> 当日已成交买入数量（用于 T+1 可卖数量计算）
    today_bought: Dict[str, float] = field(default_factory=dict)
    #: 降级快照标记（US-P0-4）：账户汇总/持仓查询失败时为 True。
    #: 降级只允许"减仓"，绝不允许新开仓。
    degraded: bool = False
    degraded_reason: str = ""
    cache_at: str = ""
    #: US-P1 N-2：本轮持仓是否由券商**实时**核实。
    #: 为 False 时（降级/快照兜底）任何 reduce-only 退出通道都不得启用 ——
    #: 因为"退出数量不超过真实持仓"这个前提本身不成立。
    positions_verified: bool = True
    #: US-P1 N-1：本轮订单历史是否可读。False = 数据库故障，不能当作"没有历史委托"。
    orders_known: bool = True
    #: US-P1 Batch2 R-2：未完成卖单占用的预留量（symbol -> qty）。
    #: 可卖余量 = 已核实持仓 − 该值；**unknown 状态也计入**（绝不自动释放）。
    open_sell_reserved: Dict[str, float] = field(default_factory=dict)
    #: 预留量是否读成功。False = 读不出来 → 禁止任何新增卖出（不能当 0）。
    reservations_known: bool = True
    #: 订单意图表是否可用。False = 预写通道不可用 → 禁止开仓。
    intents_available: bool = True

    def sellable_qty(self, symbol: str) -> float:
        """可卖数量 = 持仓 − 当日买入（T+1）。T+0 市场由规则层跳过此限制。"""
        pos = self.position_of(symbol)
        held = float(pos.qty) if pos else 0.0
        return max(0.0, held - float(self.today_bought.get(symbol.lower(), 0.0)))

    @property
    def market_value(self) -> float:
        """只统计报告币种下的持仓市值（本版不做 FX 折算，见 REPORT.md 已知限制）。"""
        return sum(p.market_value for p in self.positions
                   if (p.currency or self.currency) == self.currency)

    @property
    def position_count(self) -> int:
        """账户级持仓标的数（跨币种），用于 max_positions 判定。"""
        return len([p for p in self.positions if p.qty != 0])

    def reserved_sell_qty(self, symbol: str) -> float:
        """该标的未完成卖单占用的数量（未知状态也计入）。"""
        return float(self.open_sell_reserved.get((symbol or "").lower(), 0.0))

    def sellable_now(self, symbol: str) -> float:
        """当前可卖余量 = 持仓 − 未完成卖单预留。可能为负（数据不一致时）。"""
        pos = self.position_of(symbol)
        held = float(pos.qty) if pos else 0.0
        return held - self.reserved_sell_qty(symbol)

    def position_of(self, symbol: str) -> Optional[Position]:
        for p in self.positions:
            if p.symbol.lower() == symbol.lower():
                return p
        return None

    @property
    def gross_exposure_pct(self) -> float:
        return (self.market_value / self.equity) if self.equity else 0.0


@dataclass
class OrderIntent:
    """"想做什么"—— 尚未获得任何执行权限。"""

    symbol: str
    side: str                       # buy | sell
    qty: float
    price: Optional[float] = None
    market: str = ""
    currency: str = ""
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    order_type: str = "limit"
    signal_id: str = ""
    confidence: float = 0.0
    reason: str = ""
    is_exit: bool = False           # 止损/止盈等减仓动作
    #: US-P1 N-2：减仓动作的来源分类，用于"未知单期间"的 reduce-only 判定。
    #: 取值：stop_loss / take_profit / signal_exit / manual / ""（未分类）
    exit_mode: str = ""

    @property
    def notional(self) -> float:
        return float(self.qty or 0) * float(self.price or 0)

    @property
    def is_reduce_only(self) -> bool:
        """是不是"只减仓"的意图：卖出且已标记为退出。"""
        return bool(self.is_exit) and (self.side or "").lower() == "sell"

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass
class RiskDecision:
    approved: bool
    intent: OrderIntent
    checks: List[CheckResult] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    approved_qty: float = 0.0
    requested_qty: float = 0.0
    decision_id: str = ""
    ts: str = ""

    def __post_init__(self) -> None:
        if not self.decision_id:
            self.decision_id = uuid.uuid4().hex[:16]
        if not self.ts:
            self.ts = datetime.now().astimezone().isoformat(timespec="seconds")
        if not self.requested_qty:
            self.requested_qty = float(self.intent.qty or 0)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "ts": self.ts,
            "symbol": self.intent.symbol,
            "action": self.intent.side,
            "approved": self.approved,
            "approved_qty": self.approved_qty,
            "requested_qty": self.requested_qty,
            "reasons": self.reasons,
            "warnings": self.warnings,
            "checks": [c.as_dict() for c in self.checks],
        }

    @property
    def failed_checks(self) -> List[str]:
        return [c.name for c in self.checks if not c.ok]

    def explain(self) -> str:
        if self.approved:
            return f"放行 {self.intent.side} {self.intent.symbol} × {self.approved_qty:g}"
        return "拒绝：" + "；".join(self.reasons or self.failed_checks)


# --------------------------------------------------------------------------- #
# 交易时段
# --------------------------------------------------------------------------- #
# US-P0-1：时段判定统一交给 market.clock.ExchangeClock（交易所当地时间 + zoneinfo）。
# 旧实现把美股写成"北京时间 21:30–04:00"，等价于把 EDT 的 09:30–16:00 固化成常量：
#   * 冬令时（EST）整体偏早 1 小时 → 盘前被当成开市、尾盘 1 小时被当成休市
#   * 星期几用北京日期 → 周五夜盘（北京周六）判休市、周日深夜（北京周一）判开市
def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """把 naive datetime 视为本地时间并转 aware。

    审计发现的真实崩溃点：调用方传 naive ``now``、而订单/信号时间戳是 aware
    ISO 字符串时，``ts >= cutoff`` 会抛 TypeError，风控按 fail-closed 变成
    "内部异常 → 拒绝一切交易"。这里统一时区，避免误封锁。
    """
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.astimezone()


#: 兼容旧引用：``SESSIONS`` 现在是**交易所当地时间**的窗口，而非北京窗口。
SESSIONS = {k: [tuple(map(tuple, w)) for w in v]
            for k, v in DEFAULT_SESSIONS.items()}


def is_market_open(market: str, now: Optional[datetime] = None) -> Tuple[bool, str]:
    """某个市场当前是否处于**常规交易时段**（按交易所当地时间判断）。

    时区数据缺失、市场未配置时段时返回 ``False``（fail-closed），
    绝不因为"不知道"而放行。
    """
    mkt = (market or "").lower()
    spec = DEFAULT_SESSIONS.get(mkt)
    if not spec:
        return False, f"{mkt} 未配置交易时段，拒绝交易（fail-closed）"
    try:
        clock = build_clock(mkt, spec)
    except ClockError as exc:
        return False, f"{mkt} 交易所时钟不可用（{exc}），拒绝交易"
    try:
        return clock.is_open(now)
    except Exception as exc:  # noqa: BLE001
        return False, f"{mkt} 时段判定异常（{type(exc).__name__}: {exc}），拒绝交易"


# --------------------------------------------------------------------------- #
# 风控主体
# --------------------------------------------------------------------------- #
class RiskController:
    """独立风控。策略引擎每次下单前必须调用 :meth:`evaluate`。"""

    def __init__(self, cfg: AppConfig, gate: Optional[SafetyGate] = None,
                 rules: Optional[MarketRuleSet] = None):
        self.cfg = cfg
        self.gate = gate or cfg.gate
        # B4：交易规则（板块/T+1/涨跌停/最小单位/停牌/日历）
        self.rules = rules if rules is not None else MarketRuleSet(
            cfg.raw.get("market_rules") or {})
        r = cfg.risk
        self.max_gross = float(r.get("max_gross_exposure_pct", 0.60))
        self.max_positions = int(r.get("max_positions", 8))
        self.max_position_pct = float(r.get("max_position_pct", 0.15))
        self.max_order_notional = float(r.get("max_order_notional", 50000))
        self.min_order_notional = float(r.get("min_order_notional", 100))
        self.risk_per_trade_pct = float(r.get("risk_per_trade_pct", 0.01))
        self.max_stop_distance_pct = float(r.get("max_stop_distance_pct", 0.12))
        self.max_daily_loss_pct = float(r.get("max_daily_loss_pct", 0.03))
        self.max_daily_trades = int(r.get("max_daily_trades", 20))
        self.max_daily_orders = int(r.get("max_daily_orders", 40))
        self.duplicate_window = int(r.get("duplicate_window_seconds", 300))
        self.max_orders_per_minute = int(r.get("max_orders_per_minute", 5))
        self.max_price_deviation_pct = float(r.get("max_price_deviation_pct", 0.05))
        self.enforce_market_hours = bool(r.get("enforce_market_hours", True))
        self.allow_short = bool(r.get("allow_short", False))
        self.allow_margin = bool(r.get("allow_margin", False))
        self.markets = (cfg.raw.get("markets") or {})
        self.universe_codes = {u["code"].lower() for u in cfg.universe}
        # 运行期状态
        self._recent_orders: List[Tuple[datetime, str, str, float]] = []
        self.halt_reason: str = ""
        self.halt_store_error: str = ""
        # 启动时从哨兵同步：重启后默认保持停止
        self.refresh_halt_state()

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #
    def _check(self, checks: List[CheckResult], name: str, ok: bool, detail: str = "") -> bool:
        checks.append(CheckResult(name=name, ok=ok, detail=detail))
        return ok

    def _market_enabled(self, market: str) -> bool:
        cfg = self.markets.get((market or "").lower()) or {}
        return bool(cfg.get("enabled", False))

    def recent_order_count(self, seconds: int = 60, now: Optional[datetime] = None) -> int:
        now = _aware(now) or datetime.now().astimezone()
        cutoff = now - timedelta(seconds=seconds)
        self._recent_orders = [r for r in self._recent_orders if r[0] >= cutoff]
        return len(self._recent_orders)

    def note_order(self, symbol: str, side: str, qty: float, now: Optional[datetime] = None) -> None:
        """下单成功后登记，用于频率与重复检测。"""
        self._recent_orders.append((_aware(now) or datetime.now().astimezone(),
                                    symbol.lower(), side, float(qty)))

    def halt(self, reason: str, actor: str = "system") -> None:
        """触发急停。

        顺序很重要：先让**当前进程**进入停止态，再尝试落盘。
        落盘失败时保持停止并把 ``halt_store_failed`` 置位 —— 宁可不可恢复，
        也不能出现"以为停了其实没停"。
        """
        self.halt_reason = reason
        self.gate.state.kill_switch = True
        self.gate.state.halt_reason = reason
        self.gate.state.halt_source = "runtime"
        store = self._halt_store()
        if store is None:
            self.halt_store_error = "未配置急停哨兵路径，急停仅在本进程有效"
            return
        try:
            st = store.halt(reason, actor=actor)
            self.gate.state.halt_at = st.at
            self.gate.state.halt_source = "sentinel"
            self.halt_store_error = ""
        except HaltStoreError as exc:
            self.halt_store_error = str(exc)
            # US-P1 N-3：写失败单独打标（读失败会在 evaluate 里全阻断）
            self.gate.state.mark_halt_storage_failure(
                kind=getattr(exc, "kind", ""), phase="write")
            self.gate.state.halt_reason = f"{reason}（持久化失败：{exc}）"
            raise

    def resume(self, confirmation: str, actor: str = "human") -> None:
        """解除急停。必须提供精确确认短语，否则抛 :class:`HaltStoreError`。"""
        store = self._halt_store()
        if store is None:
            raise HaltStoreError("未配置急停哨兵路径，无法确认解除")
        store.resume(confirmation, actor=actor)
        self.halt_reason = ""
        self.halt_store_error = ""
        self.gate.state.kill_switch = False
        self.gate.state.halt_source = "none"
        self.gate.state.halt_reason = ""
        self.gate.state.halt_at = ""
        self.gate.state.clear_halt_storage_failure()

    def _halt_store(self):
        store = getattr(self.cfg, "halt_store", None)
        return store

    def refresh_halt_state(self) -> None:
        """从哨兵重新同步停止态（Web/CLI 在长驻进程里定期调用）。"""
        store = self._halt_store()
        if store is None:
            return
        st = store.safe_read()
        if st.halted:
            self.gate.state.kill_switch = True
            self.gate.state.halt_source = st.source
            self.gate.state.halt_reason = st.reason
            self.gate.state.halt_at = st.at
            self.halt_reason = st.reason
        if store.failed:
            # US-P1 N-3：把具体故障类型带上门禁
            self.gate.state.mark_halt_storage_failure(
                kind=store.last_kind, phase=store.last_phase or "read")
        elif not self.gate.state.halt_write_failed:
            # 读侧恢复：仅当写侧也正常时才清除聚合标志
            self.gate.state.clear_halt_storage_failure()

    # ------------------------------------------------------------------ #
    # 核心：评估一个交易意图
    # ------------------------------------------------------------------ #
    def evaluate(
        self,
        intent: OrderIntent,
        portfolio: Portfolio,
        last_price: float,
        recent_orders: Optional[List[Dict[str, Any]]] = None,
        now: Optional[datetime] = None,
        journal: Any = None,
        reserve_attempt: bool = True,
        prev_close: Optional[float] = None,
        orders_known: bool = True,
    ) -> RiskDecision:
        """返回放行/拒绝决定。任何异常 → 拒绝。

        ``journal`` 非空时会使用**落库**的冷却记录做跨进程重复单校验，
        并在放行前原子地占用一个冷却槽位（数据库唯一约束保证并发安全）。

        ``orders_known=False`` 表示**订单历史读不出来**（数据库故障）。
        这时不能假装"没有历史委托"：开仓一律拒绝；减仓在满足 reduce-only
        硬约束时放行，并在 decision 上留痕。
        """
        checks: List[CheckResult] = []
        reasons: List[str] = []
        warnings: List[str] = []
        now = _aware(now) or datetime.now().astimezone()
        recent_orders = recent_orders or []

        try:
            decision = self._evaluate_inner(intent, portfolio, last_price, recent_orders,
                                            now, checks, reasons, warnings,
                                            prev_close=prev_close,
                                            orders_known=orders_known)
            if decision.approved and reserve_attempt and journal is not None:
                self._reserve_or_block(decision, intent, journal, now, checks, reasons)
            return decision
        except Exception as exc:  # fail-closed
            return RiskDecision(
                approved=False, intent=intent, checks=checks + [
                    CheckResult("risk_engine_exception", False, f"{type(exc).__name__}: {exc}")
                ],
                reasons=[f"风控内部异常，按拒绝处理: {type(exc).__name__}: {exc}"],
                warnings=warnings, approved_qty=0.0,
            )

    def _evaluate_inner(self, intent, portfolio, last_price, recent_orders,
                        now, checks, reasons, warnings,
                        prev_close: Optional[float] = None,
                        orders_known: bool = True) -> RiskDecision:
        sym = (intent.symbol or "").strip()
        side = (intent.side or "").strip().lower()
        qty = float(intent.qty or 0)
        market = (intent.market or "").lower()
        # 持仓与参考价在一次评估内只解析一次：规则校验、金额检查、卖出检查共用
        pos = portfolio.position_of(sym)
        price = float(intent.price or last_price or 0)
        notional = qty * price

        # ---------- 0. 基础合法性 ----------
        if not self._check(checks, "intent_valid", bool(sym) and side in ("buy", "sell") and qty > 0,
                           f"symbol={sym} side={side} qty={qty}"):
            reasons.append("交易意图非法（缺少标的/方向错误/数量非正）")

        # ---------- 1. 紧急停止 ----------
        if not self._check(checks, "kill_switch", not self.gate.state.kill_switch,
                           "KILL_SWITCH 未触发" if not self.gate.state.kill_switch
                           else f"KILL_SWITCH 已触发：{self.halt_reason or '手动紧急停止'}"):
            reasons.append(f"紧急停止生效：{self.halt_reason or '手动紧急停止'}")

        # ---------- 1b. 急停存储可用性（US-P1 N-3：读写分级） ----------
        # 读失败 = "不知道是否被人为停止" → 一切阻断，没有例外。
        # 写失败 = "知道自己没停，但停不下来" → 默认也阻断；只有在显式开启且
        #          满足 reduce-only 硬约束时才讨论放行（本轮默认关闭）。
        st = self.gate.state
        if st.halt_read_failed or (st.halt_store_failed and not st.halt_write_failed):
            kind = st.halt_error_kind or "unknown"
            self._check(checks, "halt_storage_read", False,
                        f"急停哨兵读取失败（{kind}）：无法确认是否已被人工停止")
            reasons.append(
                f"急停哨兵读取失败（{kind}，{self.halt_store_error or '未知'}），"
                f"无法确认停止态，拒绝一切交易")
        elif st.halt_write_failed:
            ok, why = self._halt_write_failure_exit(intent, portfolio, now)
            self._check(checks, "halt_storage_write", ok,
                        f"急停哨兵写入失败（{st.halt_error_kind or 'unknown'}）：{why}")
            if not ok:
                reasons.append(
                    f"急停哨兵写入失败（{st.halt_error_kind or '未知'}），{why}")
            else:
                warnings.append(f"急停哨兵写入失败，但本次为受限减仓：{why}")
        else:
            self._check(checks, "halt_storage", True, "急停哨兵可用")

        # ---------- 1c. 订单历史可见性（US-P1 N-1） ----------
        # 数据库读不出历史时，绝不能假装"没有历史委托"。
        if orders_known:
            self._check(checks, "orders_history", True, "订单历史可读")
        elif intent.is_reduce_only:
            self._check(checks, "orders_history", True,
                        "订单历史不可读；本次为减仓，按 reduce-only 硬约束放行（已告警）")
            warnings.append("订单历史不可读：已禁止一切新开仓，仅放行 reduce-only 减仓")
        else:
            self._check(checks, "orders_history", False,
                        "订单历史不可读（数据库故障），无法判断是否存在未完成/未知委托")
            reasons.append("订单历史不可读，禁止开仓（避免在看不到已有委托时重复下单）")

        # ---------- 2. 交易权限 ----------
        permitted = True
        perm_detail = ""
        if not intent.is_exit and getattr(portfolio, "degraded", False):
            # US-P0-4：降级组合（权益/现金未知）只能用于减仓，不能开新仓
            self._check(checks, "portfolio_integrity", False,
                        f"账户快照不完整（{portfolio.degraded_reason or '未知原因'}），"
                        f"禁止新开仓")
            reasons.append(f"账户快照不完整，禁止新开仓：{portfolio.degraded_reason}")
        else:
            self._check(checks, "portfolio_integrity", True,
                        "账户快照完整" if not getattr(portfolio, "degraded", False)
                        else "账户快照不完整，但本次为减仓动作，放行")
        if not intent.is_exit:
            # 新开仓/加仓需要完整的下单权限；减仓/止损在模拟盘由 paper 权限覆盖
            if self.gate.state.paper_trading:
                perm_detail = "PAPER_TRADING=true，模拟盘交易已启用"
            else:
                ok, why = self.gate.can_send_real_order()
                permitted = ok
                perm_detail = why
        else:
            perm_detail = "减仓/止损动作，允许在紧急状态下执行"
        if not self._check(checks, "trading_permission", permitted, perm_detail):
            reasons.append(f"无交易权限：{perm_detail}")

        # ---------- 3. 市场开关 ----------
        # US-P0-4：市场被停用只应拦"新开仓"。已有持仓必须在任何配置状态下都能平掉，
        # 否则一次配置改动就会让持仓失去止损能力（配置错误不该变成资金风险）。
        if self._market_enabled(market):
            self._check(checks, "market_enabled", True, f"market={market} 已启用")
        elif intent.is_exit:
            self._check(checks, "market_enabled", True,
                        f"market={market} 未启用，但减仓动作放行")
            warnings.append(f"市场 {market} 未启用，本次为减仓动作，已放行")
        else:
            self._check(checks, "market_enabled", False, f"market={market} 未启用")
            reasons.append(f"市场 {market or '(空)'} 未启用")

        # ---------- 3b. 交易规则（B4：板块/T+1/涨跌停/最小单位/停牌/日历）----------
        rule_check = None
        try:
            rule_check = self.rules.check_order(
                symbol=sym, side=side, qty=qty, price=(price if price > 0 else None),
                prev_close=prev_close,
                position_qty=float(pos.qty if pos else 0.0),
                today_bought_qty=float(portfolio.today_bought.get(sym.lower(), 0.0)),
                now=now, is_exit=bool(intent.is_exit),
            )
        except Exception as exc:  # 规则层异常 → fail-closed
            checks.append(CheckResult("market_rules", False,
                                      f"规则校验异常：{type(exc).__name__}: {exc}"))
            reasons.append(f"交易规则校验异常，拒绝：{type(exc).__name__}: {exc}")

        if rule_check is not None:
            if rule_check.applied:
                if rule_check.violations:
                    for v in rule_check.violations:
                        checks.append(CheckResult(f"rule:{v.code}", False, v.detail))
                        reasons.append(f"[{v.code}] {v.detail}")
                else:
                    detail = (f"板块 {rule_check.rule_id} 规则通过"
                              f"（T+1/涨跌停/最小单位/停牌/日历）")
                    if rule_check.note:
                        detail += f"；备注：{rule_check.note}"
                    checks.append(CheckResult("market_rules", True, detail))
            else:
                # 未纳入强制校验的市场：显式记录"未建模"，不伪装成"已校验"
                checks.append(CheckResult("market_rules", True,
                                          f"未强制校验：{rule_check.note}"))

        # ---------- 4. 标的白名单 ----------
        in_universe = sym.lower() in self.universe_codes
        if not self._check(checks, "symbol_allowlist", in_universe,
                           f"{sym} {'在' if in_universe else '不在'}观察池（{len(self.universe_codes)} 个）"):
            reasons.append(f"{sym} 不在允许交易的观察池内")

        # ---------- 5. 交易时段（按交易所当地时间；US-P0-1） ----------
        if self.enforce_market_hours and not intent.is_exit:
            is_open, why = self.rules.session_status(market, now)
            if not self._check(checks, "market_hours", is_open, why):
                reasons.append(f"不在交易时段：{why}")
        else:
            self._check(checks, "market_hours", True, "未强制时段检查" if not self.enforce_market_hours
                        else "减仓动作豁免时段检查")

        # ---------- 6. 数量与名义金额 ----------
        if not self._check(checks, "price_available", price > 0, f"参考价={price}"):
            reasons.append("无法取得有效参考价格")
        # US-P1 N-2：最小金额是"过滤碎单"的规则，不能用来阻止清掉一个很小的持仓。
        if intent.is_exit:
            self._check(checks, "min_notional", True,
                        f"减仓动作不适用最小下单金额（名义 {notional:.2f}）")
        elif not self._check(checks, "min_notional", notional >= self.min_order_notional,
                             f"名义金额 {notional:.2f} >= 下限 {self.min_order_notional:.2f}"):
            reasons.append(f"委托金额 {notional:.2f} 低于最小下单金额 {self.min_order_notional:.2f}")
        if side == "buy" and not self._check(
            checks, "max_notional", notional <= self.max_order_notional,
            f"名义金额 {notional:.2f} <= 上限 {self.max_order_notional:.2f}"):
            reasons.append(f"委托金额 {notional:.2f} 超过单笔上限 {self.max_order_notional:.2f}")

        # ---------- 7. 卖出必须有持仓（禁止裸卖空） ----------
        held = float(pos.qty) if pos else 0.0
        if side == "sell":
            ok = held >= qty - 1e-9
            if not self._check(checks, "no_naked_short", ok,
                               f"持有 {held:g}，卖出 {qty:g}；allow_short={self.allow_short}"):
                reasons.append(f"卖出数量 {qty:g} 超过持仓 {held:g}（未开通做空）")

            # ---------- 7b. 可卖余量（US-P1 Batch2 R-2，核心） ----------
            # 可卖余量 = 已核实持仓 − 所有未完成卖单预留（含 unknown）。
            # 预留量读不出来时**不能当 0**：直接禁止新增卖出。
            reserved = float(getattr(portfolio, "reserved_sell_qty")(sym)
                             if hasattr(portfolio, "reserved_sell_qty")
                             else portfolio.open_sell_reserved.get(sym.lower(), 0.0))
            known = bool(getattr(portfolio, "reservations_known", True))
            intents_ok = bool(getattr(portfolio, "intents_available", True))
            if not known or not intents_ok:
                self._check(checks, "sell_capacity", False,
                            "未完成卖单预留量不可读（数据库/意图表故障）→ "
                            "无法确认可卖余量，禁止新增卖出")
                reasons.append(
                    "reduction_blocked_unverified：未完成卖单预留量不可读，"
                    "无法确认可卖余量，禁止新增卖出（含止损）")
            else:
                available = held - reserved
                ok2 = qty <= available + 1e-9
                if not self._check(
                        checks, "sell_capacity", ok2,
                        f"持仓 {held:g} − 未完成卖单预留 {reserved:g} = "
                        f"可卖 {available:g}，卖出 {qty:g}"):
                    reasons.append(
                        f"reduction_blocked_unverified：卖出 {qty:g} 超过可卖余量 "
                        f"{available:g}（持仓 {held:g} − 未完成卖单 {reserved:g}）")

        # ---------- 8. 持仓数量上限 ----------
        if side == "buy" and held <= 0:
            n = portfolio.position_count
            if not self._check(checks, "max_positions", n < self.max_positions,
                               f"当前持仓 {n} / 上限 {self.max_positions}"):
                reasons.append(f"持仓标的数已达上限 {self.max_positions}")

        # ---------- 9. 单标的仓位占比 ----------
        if side == "buy" and portfolio.equity > 0:
            after = (pos.market_value if pos else 0.0) + notional
            pct = after / portfolio.equity
            if not self._check(checks, "max_position_pct", pct <= self.max_position_pct + 1e-9,
                               f"建仓后该标的占比 {pct:.2%} / 上限 {self.max_position_pct:.2%}"):
                reasons.append(f"建仓后 {sym} 占比 {pct:.2%} 超过上限 {self.max_position_pct:.2%}")

        # ---------- 10. 总敞口 ----------
        if side == "buy" and portfolio.equity > 0:
            after_exposure = (portfolio.market_value + notional) / portfolio.equity
            if not self._check(checks, "max_gross_exposure",
                               after_exposure <= self.max_gross + 1e-9,
                               f"总敞口 {after_exposure:.2%} / 上限 {self.max_gross:.2%}"):
                reasons.append(f"总敞口 {after_exposure:.2%} 超过上限 {self.max_gross:.2%}")

        # ---------- 11. 单笔风险（依赖止损） ----------
        if side == "buy":
            sl = intent.stop_loss
            if sl is None or sl <= 0:
                self._check(checks, "per_trade_risk", False, "缺少止损价，无法计算单笔风险")
                reasons.append("买入缺少止损价，违反单笔风险限额规则")
            else:
                dist_pct = (price - sl) / price if price else 1.0
                if not self._check(checks, "max_stop_distance",
                                   dist_pct <= self.max_stop_distance_pct + 1e-9,
                                   f"止损距离 {dist_pct:.2%} / 上限 {self.max_stop_distance_pct:.2%}"):
                    reasons.append(f"止损距离 {dist_pct:.2%} 过宽，超过 {self.max_stop_distance_pct:.2%}")
                if sl >= price:
                    self._check(checks, "stop_side", False, f"止损价 {sl} 不低于入场价 {price}")
                    reasons.append("止损价方向错误（买入止损必须低于入场价）")
                else:
                    self._check(checks, "stop_side", True, f"止损 {sl} < 入场 {price}")
                    risk_amt = (price - sl) * qty
                    budget = portfolio.equity * self.risk_per_trade_pct if portfolio.equity else 0
                    ok = budget > 0 and risk_amt <= budget + 1e-9
                    if not self._check(checks, "per_trade_risk", ok,
                                       f"本单最大亏损 {risk_amt:.2f} / 单笔预算 {budget:.2f}"
                                       f"（权益 {portfolio.equity:.2f} × {self.risk_per_trade_pct:.2%}）"):
                        reasons.append(
                            f"单笔风险超限：最大亏损 {risk_amt:.2f} > 预算 {budget:.2f}"
                        )

        # ---------- 12. 单日亏损 ----------
        # US-P0-4：单日亏损熔断只能拦"新开仓"。已经亏损时更需要能止损离场 ——
        # 把减仓也一起拦掉等于"亏损到线后不许跑"，方向完全反了。
        if intent.is_exit:
            self._check(checks, "daily_loss_limit", True,
                        "减仓动作不适用单日亏损熔断（越亏越必须允许离场）")
        elif portfolio.equity > 0:
            if not getattr(portfolio, "day_pnl_known", True):
                # 拿不到真实日内盈亏时不放行：无法判断是否已触及熔断，
                # "不知道"必须等价于"不允许"（fail-closed）。
                self._check(checks, "daily_loss_limit", False,
                            "无法获取真实日内盈亏（券商未返回 dayPnl），"
                            "无法判定单日亏损熔断 → 拒绝新开仓")
                reasons.append("日内盈亏不可用，单日亏损熔断无法判定，拒绝新开仓")
            else:
                loss_pct = -portfolio.day_pnl / portfolio.equity
                if not self._check(checks, "daily_loss_limit",
                                   loss_pct < self.max_daily_loss_pct,
                                   f"当日盈亏 {portfolio.day_pnl:+.2f}（{loss_pct:+.2%}）/ 上限 {self.max_daily_loss_pct:.2%}"):
                    reasons.append(
                        f"触及单日亏损上限 {self.max_daily_loss_pct:.2%}，今日禁止新开仓"
                    )

        # ---------- 13. 单日频次 ----------
        if not intent.is_exit:
            if not self._check(checks, "daily_trade_count",
                               portfolio.day_trades < self.max_daily_trades,
                               f"今日成交 {portfolio.day_trades} / 上限 {self.max_daily_trades}"):
                reasons.append(f"今日成交笔数已达上限 {self.max_daily_trades}")
            if not self._check(checks, "daily_order_count",
                               portfolio.day_orders < self.max_daily_orders,
                               f"今日委托 {portfolio.day_orders} / 上限 {self.max_daily_orders}"):
                reasons.append(f"今日委托笔数已达上限 {self.max_daily_orders}")

        # ---------- 14. 状态未知的委托（必须先对账；US-P1 N-2 方向分级） ----------
        # 券商返回空 orderId / 网络中断时，订单可能已受理也可能没受理。
        # 原实现"该标的有未知单 → 禁止一切方向"，会把**止损也锁死**；
        # 现在按方向分级：
        #   * 未知单是 buy，本次是 sell  → 允许 reduce-only（数量≤已核实持仓）
        #   * 未知单是 sell，本次是 sell → 仍然阻断（可能重复卖出 → 超卖）
        #   * 任何其它组合 → 阻断
        unresolved_orders = self._unresolved_orders(sym, recent_orders)
        if not unresolved_orders:
            self._check(checks, "unresolved_order_state", True, "无状态未知的委托")
        else:
            ok, why = self._unresolved_reduce_only(intent, portfolio,
                                                   unresolved_orders, now)
            self._check(checks, "unresolved_order_state", ok,
                        why if ok else
                        f"{self._unresolved_reason(sym, unresolved_orders)}；{why}")
            if not ok:
                reasons.append(f"{self._unresolved_reason(sym, unresolved_orders)}"
                               f"（{why}）")
            else:
                warnings.append(why)

        # ---------- 14b. 重复订单 / 冷却（US-P1 N-4：时间戳有界 + 减仓不适用时间冷却） ----------
        dup = self._duplicate_check_detail(sym, side, qty, price, recent_orders,
                                           now, is_exit=bool(intent.is_exit))
        if dup.get("anomaly"):
            checks.append(CheckResult("order_timestamp_anomaly", True,
                                      dup["anomaly"]))
            warnings.append(f"订单时间戳异常（已告警、不计入冷却）：{dup['anomaly']}")
        if not self._check(checks, "duplicate_order", dup.get("reason") is None,
                           dup.get("reason") or "无重复/冷却冲突"):
            reasons.append(dup.get("reason") or "重复订单被拦截")

        # ---------- 15. 频率限制 ----------
        # US-P1 N-2：频率限制防的是"刷单"，不是"救命"。减仓只告警不拦截。
        rpm = self.recent_order_count(60, now)
        if intent.is_exit:
            self._check(checks, "rate_limit", True,
                        f"减仓动作不适用频率限制（近 60 秒 {rpm} 笔）")
        elif not self._check(checks, "rate_limit", rpm < self.max_orders_per_minute,
                             f"近 60 秒 {rpm} 笔 / 上限 {self.max_orders_per_minute}"):
            reasons.append(f"下单频率超限（{rpm} 笔/分钟 > {self.max_orders_per_minute}）")

        # ---------- 16. 价格合理性 ----------
        if last_price > 0 and price > 0:
            dev = abs(price - last_price) / last_price
            if not self._check(checks, "price_deviation",
                               dev <= self.max_price_deviation_pct + 1e-9,
                               f"委托价偏离 {dev:.2%} / 上限 {self.max_price_deviation_pct:.2%}"):
                reasons.append(f"委托价偏离最新价 {dev:.2%}，疑似错价")

        # ---------- 17. 现金充足 ----------
        if side == "buy":
            if not self._check(checks, "cash_sufficient", notional <= portfolio.cash + 1e-9,
                               f"需要 {notional:.2f} / 可用 {portfolio.cash:.2f}"):
                reasons.append(f"现金不足：需要 {notional:.2f}，可用 {portfolio.cash:.2f}")

        # ---------- 18. 融资/做空开关 ----------
        if not self._check(checks, "margin_disabled", not self.allow_margin,
                           "融资交易已关闭" if not self.allow_margin else "融资交易已开启"):
            warnings.append("融资交易状态与默认策略不一致，请人工确认")

        # ---------- 汇总 ----------
        approved = not reasons
        approved_qty = qty if approved else 0.0
        if approved and qty <= 0:
            approved, approved_qty = False, 0.0
            reasons.append("获批数量为 0")

        if approved and self.gate.state.kill_switch:
            approved, approved_qty = False, 0.0
            reasons.append("兜底校验：KILL_SWITCH 为真，拒绝放行")

        return RiskDecision(
            approved=approved, intent=intent, checks=checks, reasons=reasons,
            warnings=warnings, approved_qty=approved_qty, requested_qty=qty,
        )

    # ------------------------------------------------------------------ #
    # US-P1 N-2：未知单 / reduce-only 通道
    # ------------------------------------------------------------------ #
    def _unresolved_orders(self, symbol: str,
                           recent_orders: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """该标的所有"状态未知"的委托（可能是已受理但未确认）。"""
        sym_l = (symbol or "").lower()
        out: List[Dict[str, Any]] = []
        for od in recent_orders or []:
            if str(od.get("symbol", "")).lower() != sym_l:
                continue
            if str(od.get("status", "")).lower() in ("unknown", "pending_reconcile"):
                out.append(od)
        return out

    def _find_unresolved(self, symbol: str,
                         recent_orders: List[Dict[str, Any]]) -> Optional[str]:
        """兼容旧接口：返回第一条未知单的说明（无则 None）。"""
        found = self._unresolved_orders(symbol, recent_orders)
        return self._unresolved_reason(symbol, found) if found else None

    def _unresolved_reason(self, symbol: str,
                           orders: List[Dict[str, Any]]) -> str:
        ids = ", ".join(
            f"{o.get('order_id') or '?'}(clientId={o.get('client_id') or '?'},"
            f"{str(o.get('side') or '?')})" for o in orders[:3])
        return (f"{symbol} 存在状态未知的委托（{ids}），必须先完成对账才能继续交易")

    def _reduce_only_gate(self, intent: OrderIntent, portfolio: "Portfolio",
                          *, why_prefix: str) -> Tuple[bool, str]:
        """reduce-only 硬约束。**四条同时满足**才算通过。

        1. 方向必须是卖出，且已标记为退出（不允许借道反向开仓）
        2. 持仓必须**本轮已由券商实时核实**（快照兜底不够）
        3. 存在真实持仓且数量 > 0
        4. 委托数量 ≤ 已核实持仓（不允许卖出超过真实持仓）

        说明：官方 SDK 的 ``create_order`` **没有** reduce-only / close 原语
        （见 API_STATUS.md），因此券商侧无法二次保证；本约束是**客户端**约束，
        已在报告中明确标注为"部分未解决"。
        """
        if not intent.is_reduce_only:
            return False, f"{why_prefix}：本次不是「卖出且标记为退出」的减仓意图，不得放行"
        if not bool(getattr(portfolio, "positions_verified", True)):
            return False, f"{why_prefix}：本轮持仓未经券商实时核实（降级快照），数量可能失真，不得放行"
        pos = portfolio.position_of(intent.symbol)
        held = float(pos.qty) if pos else 0.0
        if held <= 0:
            return False, f"{why_prefix}：已核实持仓为 0，无法减仓（也不允许开反向仓）"
        qty = float(intent.qty or 0)
        if qty <= 0:
            return False, f"{why_prefix}：减仓数量非法（{qty}）"
        if not bool(getattr(portfolio, "reservations_known", True)) or \
                not bool(getattr(portfolio, "intents_available", True)):
            return False, (f"{why_prefix}：未完成卖单预留量不可读，"
                           f"无法确认可卖余量，禁止减仓")
        reserved = portfolio.reserved_sell_qty(intent.symbol)
        available = held - reserved
        if qty > available + 1e-9:
            return False, (f"{why_prefix}：减仓数量 {qty:g} 超过可卖余量 {available:g}"
                           f"（持仓 {held:g} − 未完成卖单预留 {reserved:g}），不得放行")
        return True, (f"{why_prefix}：按 reduce-only 放行卖 {qty:g} ≤ 可卖余量 {available:g}"
                      f"（持仓 {held:g} − 预留 {reserved:g}；"
                      f"券商侧无 reduce-only 原语，属客户端约束）")

    def _unresolved_reduce_only(self, intent: OrderIntent, portfolio: "Portfolio",
                                unresolved: List[Dict[str, Any]],
                                now: Optional[datetime] = None) -> Tuple[bool, str]:
        """未知单存在时，判断本次意图能否走 reduce-only 通道。

        关键方向规则：
        * 未知单是 **buy**，本次是 **sell(退出)** → 允许（持仓已核实 + 数量受限）
        * 未知单是 **sell**，本次是 **sell**      → 阻断（可能重复卖出 → 超卖）
        * 其它组合（含任何买入）                  → 阻断
        """
        if not intent.is_reduce_only:
            return False, "未知单存在期间只允许 reduce-only 减仓，其它方向一律阻断"
        sides = {str(o.get("side") or "").lower() for o in unresolved}
        if sides & {"sell"}:
            return False, ("存在状态未知的 sell 委托，可能已成交；再卖会造成超卖，"
                           "必须先对账")
        if not sides <= {"buy"}:
            return False, f"未知单方向不明（{sorted(sides)}），无法判断持仓影响，阻断"
        return self._reduce_only_gate(
            intent, portfolio, why_prefix="未知买单未对清期间的风险退出")

    def _halt_write_failure_exit(self, intent: OrderIntent, portfolio: "Portfolio",
                                 now: Optional[datetime] = None) -> Tuple[bool, str]:
        """急停哨兵**写失败**时的处置。

        读失败一律全阻断（见 evaluate 1b），这里只处理写失败。

        默认 ``risk.halt_write_failure_exit_mode = "block"`` → 全阻断。
        只有显式配置为 ``"reduce_only"`` 且满足 reduce-only 硬约束时才放行。
        **本轮不开启**，开关保留给人工决策后使用。
        """
        mode = str(self.cfg.risk.get("halt_write_failure_exit_mode", "block") or "block").lower()
        if mode != "reduce_only":
            return False, ("急停哨兵写入失败 → 默认阻断一切交易"
                           "（如需放行须显式配置 halt_write_failure_exit_mode=reduce_only 并人工审批）")
        return self._reduce_only_gate(
            intent, portfolio, why_prefix="急停哨兵写入失败时的受限减仓")

    # ------------------------------------------------------------------ #
    def _reserve_or_block(self, decision: "RiskDecision", intent: OrderIntent,
                          journal: Any, now: datetime, checks: List[CheckResult],
                          reasons: List[str]) -> None:
        """原子占用冷却槽位；并发/跨进程冲突时把已通过的决策改为拒绝。

        US-P1 N-4：时间冷却只约束"开仓"。减仓走 reduce-only 通道，
        由 ``duplicate_order`` 的**状态型**检查（是否存在未完成/未知委托）兜底，
        不因时钟偏差或系统时间回拨而阻断止损。
        """
        if intent.is_exit:
            return
        try:
            ok = journal.reserve_order_attempt(
                symbol=intent.symbol, side=intent.side,
                window_seconds=self.duplicate_window,
                decision_id=decision.decision_id, now=now,
            )
        except Exception as exc:  # 落库失败 → 无法保证去重 → 拒绝
            checks.append(CheckResult("duplicate_order", False,
                                      f"冷却记录写入失败：{type(exc).__name__}: {exc}"))
            reasons.append("无法写入跨进程冷却记录，拒绝下单以免重复")
            decision.approved = False
            decision.approved_qty = 0.0
            decision.reasons = list(reasons)
            return
        if not ok:
            detail = (f"{intent.symbol} 在 {self.duplicate_window}s 冷却窗口内已有 {intent.side} 委托"
                      f"（跨进程记录命中），判定为重复下单")
            checks.append(CheckResult("duplicate_order", False, detail))
            reasons.append(detail)
            decision.approved = False
            decision.approved_qty = 0.0
            decision.reasons = list(reasons)

    # ------------------------------------------------------------------ #
    # US-P1 N-4：订单时间戳体检
    # ------------------------------------------------------------------ #
    def _parse_order_ts(self, raw: Any) -> Tuple[Optional[datetime], str]:
        """解析订单时间戳。返回 ``(aware_dt, 问题说明)``；问题说明为空表示正常。"""
        text = str(raw or "").strip()
        if not text:
            return None, "时间戳为空"
        try:
            ts = datetime.fromisoformat(text)
        except ValueError:
            return None, f"时间戳无法解析：{text!r}"
        if ts.tzinfo is None:
            # 无时区 → 按本机时区解释（与全系统一致），但要留痕
            ts = ts.astimezone()
            return ts, f"时间戳缺少时区（按本机时区解释）：{text}"
        return ts, ""

    def _duplicate_check_detail(self, sym: str, side: str, qty: float, price: float,
                                recent_orders: List[Dict[str, Any]], now: datetime,
                                is_exit: bool = False) -> Dict[str, Any]:
        """重复/冷却检查（US-P1 N-4 重写）。

        与旧实现的差别：

        1. 时间窗口**有上下界**：``cutoff <= ts <= now + skew``。
           旧实现只判 ``ts >= cutoff``，一条未来时间戳的记录会**永久**卡住该标的。
        2. 未来时间戳进入**明确的异常状态**（``anomaly`` 字段），不计入冷却，
           由引擎写故障流水并告警。
        3. 减仓**不适用**时间冷却（时钟不可靠时不能因此阻断止损），
           但仍受"存在未完成/未知同向委托"的状态型检查约束。
        """
        sym_l = (sym or "").lower()
        side = (side or "").lower()
        now = _aware(now) or datetime.now().astimezone()
        skew = timedelta(seconds=int(self.cfg.risk.get("max_clock_skew_seconds", 300) or 300))
        cutoff = now - timedelta(seconds=self.duplicate_window)
        upper = now + skew
        anomalies: List[str] = []
        result: Dict[str, Any] = {"reason": None, "anomaly": "", "anomalies": []}

        # 1) 进程内冷却（仅开仓）
        if not is_exit:
            for ts, s, sd, q in self._recent_orders:
                if s != sym_l or sd != side:
                    continue
                if ts > upper:
                    anomalies.append(f"进程内记录时间戳在未来：{ts.isoformat()}")
                    continue
                if ts >= cutoff:
                    result["reason"] = (f"{sym} 在 {self.duplicate_window}s 内已有 "
                                        f"{side} 委托（{ts.strftime('%H:%M:%S')}），判定为重复下单")
                    break

        # 2) 落库订单：未完成态永远阻断（与时钟无关）；成交态只在冷却窗口内阻断且仅限开仓
        for od in recent_orders or []:
            o_sym = str(od.get("symbol", "")).lower()
            o_side = str(od.get("side", "")).lower()
            o_status = str(od.get("status", "")).lower()
            if o_sym != sym_l or o_side != side:
                continue
            if o_status in ("submitted", "pending", "partial", "new", "unknown",
                            "pending_reconcile"):
                result["reason"] = (f"{sym} 已有未完成/状态未知的 {side} 委托"
                                    f"（{od.get('order_id')}），不得重复提交")
                break
            ts_raw = od.get("ts", "")
            ts, problem = self._parse_order_ts(ts_raw)
            if problem:
                anomalies.append(f"{od.get('order_id') or '?'}: {problem}")
                if not is_exit:
                    # US-P1 Batch2 R-4：时间戳不可确认 ⇒ 不能视为"没有冷却记录"。
                    # 开仓方向 fail-closed；减仓仍放行（受预留/幂等约束）。
                    result["reason"] = (
                        f"{sym} 存在时间戳异常的 {side} 委托"
                        f"（{od.get('order_id') or '?'}: {problem}），"
                        f"无法确认冷却状态，拒绝开仓")
                    result["anomaly"] = "；".join(anomalies[:3])
                    result["anomalies"] = anomalies
                    return result
                continue
            if ts is None:
                continue
            if ts > upper:
                delta = (ts - now).total_seconds()
                anomalies.append(
                    f"{od.get('order_id') or '?'} 时间戳在未来 {delta:.0f}s（{ts_raw}），"
                    f"已排除出冷却窗口，疑似系统时钟回拨/券商时间戳异常")
                continue
            if ts < cutoff:
                continue
            if is_exit:
                # 减仓：时间冷却不作为阻断理由（只记录，交给人看）
                continue
            result["reason"] = (f"{sym} 在冷却窗口内已成交 {side}（{ts_raw}），判定为重复")

        if anomalies:
            result["anomaly"] = "；".join(anomalies[:3])
            result["anomalies"] = anomalies
        return result

    def _duplicate_check(self, sym: str, side: str, qty: float, price: float,
                         recent_orders: List[Dict[str, Any]], now: datetime) -> Optional[str]:
        """兼容旧接口（返回拒绝理由 / None）。"""
        return self._duplicate_check_detail(sym, side, qty, price,
                                            recent_orders, now).get("reason")

    # ------------------------------------------------------------------ #
    # 仓位建议（给策略引擎用，仍然要过 evaluate）
    # ------------------------------------------------------------------ #
    def size_position(
        self,
        equity: float,
        cash: float,
        price: float,
        stop_loss: float,
        confidence: float = 1.0,
        lot_size: int = 1,
        max_pct_override: Optional[float] = None,
    ) -> Dict[str, Any]:
        """按风险预算与仓位上限取较小者，并向下取整到整手。"""
        out = {"qty": 0.0, "risk_limited": 0.0, "pct_limited": 0.0, "cash_limited": 0.0,
               "binding": "", "detail": ""}
        if equity <= 0 or price <= 0:
            out["detail"] = "权益或价格无效"
            return out
        risk_per_share = price - stop_loss
        if risk_per_share <= 0:
            out["detail"] = "止损价不低于入场价，无法定仓"
            return out

        budget = equity * self.risk_per_trade_pct
        risk_qty = budget / risk_per_share
        max_pct = max_pct_override if max_pct_override is not None else self.max_position_pct
        pct_qty = (equity * max_pct) / price
        cash_qty = cash / price
        notional_qty = self.max_order_notional / price

        candidates = {
            "risk_limited": risk_qty,
            "pct_limited": pct_qty,
            "cash_limited": cash_qty,
            "max_notional": notional_qty,
        }
        binding = min(candidates, key=candidates.get)
        raw_qty = candidates[binding]
        # 置信度不足以 1 时按比例收缩（不低于 0）
        raw_qty *= max(0.0, min(1.0, confidence))
        lot = max(1, int(lot_size or 1))
        qty = float(int(raw_qty // lot) * lot)
        out.update(qty=qty, binding=binding, **{k: round(v, 4) for k, v in candidates.items()})
        out["detail"] = (f"binding={binding} 原量 {raw_qty:.2f} → 取整 {qty:g}"
                         f"（手数 {lot}）")
        return out

    # ------------------------------------------------------------------ #
    # 止损 / 止盈扫描
    # ------------------------------------------------------------------ #
    def scan_exits(
        self,
        positions: List[Position],
        stops: Dict[str, Dict[str, Optional[float]]],
        quotes: Dict[str, Quote],
    ) -> List[OrderIntent]:
        """返回应当触发的减仓意图（全部标记 is_exit=True，可绕过时段/频次限制）。"""
        intents: List[OrderIntent] = []
        for pos in positions:
            if pos.qty <= 0:
                continue
            q = quotes.get(pos.symbol)
            if not q:
                continue
            rule = stops.get(pos.symbol) or {}
            sl, tp = rule.get("stop_loss"), rule.get("take_profit")
            if sl and q.last <= sl:
                intents.append(OrderIntent(
                    symbol=pos.symbol, side="sell", qty=pos.qty, price=q.last,
                    market=pos.market, currency=pos.currency, order_type="market",
                    reason=f"止损触发：最新 {q.last} <= 止损 {sl}", is_exit=True,
                    exit_mode="stop_loss",
                    signal_id="", confidence=1.0,
                ))
            elif tp and q.last >= tp:
                intents.append(OrderIntent(
                    symbol=pos.symbol, side="sell", qty=pos.qty, price=q.last,
                    market=pos.market, currency=pos.currency, order_type="market",
                    reason=f"止盈触发：最新 {q.last} >= 目标 {tp}", is_exit=True,
                    exit_mode="take_profit",
                    confidence=1.0,
                ))
        return intents

    # ------------------------------------------------------------------ #
    def describe_limits(self) -> Dict[str, Any]:
        return {
            "kill_switch": self.gate.state.kill_switch,
            "halt_reason": self.halt_reason,
            "max_gross_exposure_pct": self.max_gross,
            "max_positions": self.max_positions,
            "max_position_pct": self.max_position_pct,
            "max_order_notional": self.max_order_notional,
            "min_order_notional": self.min_order_notional,
            "risk_per_trade_pct": self.risk_per_trade_pct,
            "max_stop_distance_pct": self.max_stop_distance_pct,
            "max_daily_loss_pct": self.max_daily_loss_pct,
            "max_daily_trades": self.max_daily_trades,
            "max_daily_orders": self.max_daily_orders,
            "duplicate_window_seconds": self.duplicate_window,
            "max_orders_per_minute": self.max_orders_per_minute,
            "max_price_deviation_pct": self.max_price_deviation_pct,
            "enforce_market_hours": self.enforce_market_hours,
            "allow_short": self.allow_short,
            "allow_margin": self.allow_margin,
            "universe_size": len(self.universe_codes),
        }
