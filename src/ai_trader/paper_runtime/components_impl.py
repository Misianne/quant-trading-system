"""四个 Paper 必需组件的具体实现。

每个组件都遵循同一契约：
* ``_start(ctx)``：构造真正的服务对象并做一次自检，失败抛错（→ 整体 FAILED）
* ``_stop(ctx)``：释放资源（取消订阅 / 关连接 / 落盘），失败会记录
* ``_health()``：运行期判定，返回 ``(ok, detail)``

**健康 ≠ 进程活着**。这里的每一项都检查"该组件是否真的还能履行职能"：
行情组件查新鲜度、账本查余额一致、存储查可用性与完整性、市场状态查时钟可信。
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .. import timebase
from ..broker.orders import PAPER_CAPABILITIES
from ..broker.short import BorrowBook, LocateService, ShortGuard, ShortMode, SSRRegistry
from ..market.clock import ExchangeClock
from ..market.status import MarketStatusService
from ..portfolio.settlement import AccountMode, SettlementLedger
from .components import Component, ComponentHealth

logger = logging.getLogger("ai_trader.paper.impl")


# ---------------------------------------------------------------------- #
# 上下文
# ---------------------------------------------------------------------- #
@dataclass
class PaperContext:
    """组件共享上下文。字段在初始化过程中被逐步填充。"""

    cfg: Any
    clocks: Dict[str, ExchangeClock] = field(default_factory=dict)
    symbols: List[str] = field(default_factory=list)
    clock: Any = None
    broker: Any = None
    journal: Any = None
    risk: Any = None
    engine: Any = None
    quote_source: Any = None
    #: 组件产物
    services: Dict[str, Any] = field(default_factory=dict)
    #: 本次运行唯一 ID
    run_id: str = ""

    def service(self, name: str) -> Any:
        return self.services.get(name)


class _CalendarAdapter:
    """把 :class:`ExchangeClock` 适配成结算账本需要的日历协议。"""

    def __init__(self, clock: ExchangeClock):
        self._clock = clock
        self.name = f"exchange-clock:{clock.market}:{clock.tz_name}"

    def is_trading_day(self, day: date) -> Tuple[bool, str]:
        return self._clock.is_trading_day(day)


# ---------------------------------------------------------------------- #
# 1. 时钟
# ---------------------------------------------------------------------- #
class ClockComponent(Component):
    name = "clock"
    critical = True

    #: 允许的时钟超前量（超过即视为时钟不可信）
    MAX_FUTURE_SKEW = timedelta(seconds=90)

    def __init__(self) -> None:
        super().__init__()
        self._last: Optional[datetime] = None
        self._readings = 0

    def _start(self, ctx: PaperContext) -> None:
        if ctx.clock is None:
            raise RuntimeError("未安装时钟（PaperRuntime 必须显式注入）")
        dt = ctx.clock.now()
        if dt.tzinfo is None:
            raise RuntimeError("时钟返回 naive 时间，无法判定交易时段")
        self._last = dt
        self._readings = 1

    def _health(self):
        dt = timebase.current().now()
        if dt.tzinfo is None:
            return False, "时钟返回 naive 时间"
        # 与"真实系统时间"对比只对 SystemClock 有意义；Manual/Frozen 跳过
        if getattr(timebase.current(), "name", "") == "system":
            real = datetime.now().astimezone()
            if dt - real > self.MAX_FUTURE_SKEW:
                return False, f"系统时钟超前真实时间 {dt - real}"
        self._readings += 1
        self._last = dt
        return True, f"readings={self._readings}"


# ---------------------------------------------------------------------- #
# 2. 存储（SQLite）
# ---------------------------------------------------------------------- #
class StorageComponent(Component):
    name = "storage"
    critical = True
    depends_on = ("clock",)

    def __init__(self, db_path: Path, jsonl_path: Optional[Path] = None) -> None:
        super().__init__()
        self.db_path = Path(db_path)
        self.jsonl_path = Path(jsonl_path) if jsonl_path else None
        self._integrity = ""

    def _start(self, ctx: PaperContext) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if self.jsonl_path:
            self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("CREATE TABLE IF NOT EXISTS _paper_runtime_probe (id INTEGER PRIMARY KEY)")
            conn.commit()
            row = conn.execute("PRAGMA integrity_check").fetchone()
            self._integrity = str(row[0]) if row else "unknown"
            if self._integrity.lower() != "ok":
                raise RuntimeError(f"SQLite integrity_check={self._integrity}")
        finally:
            conn.close()

    def _health(self):
        if not self.db_path.exists():
            return False, f"数据库不存在：{self.db_path}"
        try:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=5)
            try:
                conn.execute("SELECT 1").fetchone()
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            return False, f"数据库不可读：{type(exc).__name__}: {exc}"
        wal = self.db_path.with_name(self.db_path.name + "-wal")
        wal_mb = (wal.stat().st_size / 1e6) if wal.exists() else 0.0
        return True, f"integrity={self._integrity} wal={wal_mb:.2f}MB"


# ---------------------------------------------------------------------- #
# 3. 券商能力
# ---------------------------------------------------------------------- #
class CapsComponent(Component):
    name = "caps"
    critical = True
    depends_on = ("clock",)

    def __init__(self, capabilities: Any = None) -> None:
        super().__init__()
        self.capabilities = capabilities or PAPER_CAPABILITIES

    def _start(self, ctx: PaperContext) -> None:
        caps = self.capabilities
        if not getattr(caps, "venue", ""):
            raise RuntimeError("券商能力缺少 venue 声明")
        if not caps.order_types:
            raise RuntimeError("券商能力未声明任何订单类型（不允许空能力声明）")
        if getattr(caps, "is_live", False):
            raise RuntimeError("PaperRuntime 拒绝真实券商能力声明（is_live=True）")
        ctx.services["caps"] = caps

    def _health(self):
        return True, f"venue={self.capabilities.venue} types={len(self.capabilities.order_types)}"


# ---------------------------------------------------------------------- #
# 4. 卖空 / 借券 / SSR
# ---------------------------------------------------------------------- #
class ShortGuardComponent(Component):
    name = "short_guard"
    critical = True
    depends_on = ("clock", "caps")

    def __init__(self, mode: ShortMode = ShortMode.DISABLED,
                 require_operator_approval_live: bool = True) -> None:
        super().__init__()
        self.mode = mode
        self.require_operator_approval_live = bool(require_operator_approval_live)
        self.guard: Optional[ShortGuard] = None

    def _start(self, ctx: PaperContext) -> None:
        # 本阶段明确禁止开启卖空：配置写错也不能悄悄生效，直接失败
        if self.mode is not ShortMode.DISABLED:
            raise RuntimeError(
                f"PaperRuntime 本阶段只允许 short.mode=disabled，收到 {self.mode.value!r}")
        book = BorrowBook()
        self.guard = ShortGuard(
            mode=self.mode, borrow=book,
            locates=LocateService(book=book), ssr=SSRRegistry(),
            require_operator_approval_live=self.require_operator_approval_live,
            operator_approved=False)
        ctx.services["short_guard"] = self.guard

    def _health(self):
        if self.guard is None:
            return False, "未初始化"
        if self.mode is not ShortMode.DISABLED and self.guard.borrow is None:
            return False, "卖空模式已开启但借券簿缺失"
        return True, f"mode={self.mode.value}"

    def _stop(self, ctx: PaperContext) -> None:
        self.guard = None
        ctx.services.pop("short_guard", None)


# ---------------------------------------------------------------------- #
# 5. T+1 结算
# ---------------------------------------------------------------------- #
class SettlementComponent(Component):
    name = "settlement"
    critical = True
    depends_on = ("clock",)

    def __init__(self, currency: str = "USD", mode: AccountMode = AccountMode.CASH,
                 opening_cash: float = 0.0, margin_multiplier: float = 2.0) -> None:
        super().__init__()
        self.currency = currency
        self.mode = mode
        self.opening_cash = float(opening_cash)
        self.margin_multiplier = float(margin_multiplier)
        self.ledger: Optional[SettlementLedger] = None

    def _start(self, ctx: PaperContext) -> None:
        clock = ctx.clocks.get("us") or (next(iter(ctx.clocks.values())) if ctx.clocks else None)
        if clock is None:
            raise RuntimeError("缺少美东交易所时钟，无法按交易日做 T+1 结算")
        self.ledger = SettlementLedger(
            currency=self.currency, calendar=_CalendarAdapter(clock),
            mode=self.mode, margin_multiplier=self.margin_multiplier,
            opening_cash=self.opening_cash)
        ctx.services["settlement"] = self.ledger

    def _health(self):
        if self.ledger is None:
            return False, "未初始化"
        try:
            # 结算推进失败（例如日历不可用）视为不健康
            self.ledger.settle_through(timebase.now().date())
        except Exception as exc:  # noqa: BLE001
            return False, f"结算推进失败：{type(exc).__name__}: {exc}"
        return True, f"{self.currency}/{self.mode.value}"

    def _stop(self, ctx: PaperContext) -> None:
        self.ledger = None
        ctx.services.pop("settlement", None)


# ---------------------------------------------------------------------- #
# 6. 市场状态（LULD / MWCB / 停牌）
# ---------------------------------------------------------------------- #
class MarketStatusComponent(Component):
    name = "market_status"
    critical = True
    depends_on = ("clock", "caps")

    def __init__(self, max_quote_age_seconds: float = 60.0,
                 luld_enabled: bool = True) -> None:
        super().__init__()
        self.max_quote_age = float(max_quote_age_seconds)
        self.luld_enabled = bool(luld_enabled)
        self.service: Optional[MarketStatusService] = None

    def _start(self, ctx: PaperContext) -> None:
        self.service = MarketStatusService(
            max_quote_age_seconds=self.max_quote_age,
            luld_enabled=self.luld_enabled)
        ctx.services["market_status"] = self.service

    def _health(self):
        if self.service is None:
            return False, "未初始化"
        snap = self.service.snapshot()
        mwcb = snap.get("mwcb") or {}
        if mwcb.get("closed_for_day"):
            return False, "MWCB L3 已触发（当日关闭）"
        return True, f"symbols={len(snap.get('symbols') or {})}"

    def _stop(self, ctx: PaperContext) -> None:
        self.service = None
        ctx.services.pop("market_status", None)


# ---------------------------------------------------------------------- #
# 7. 行情源
# ---------------------------------------------------------------------- #
class FeedComponent(Component):
    """行情组件：健康判定依据「最近一次成功取价距今多久」。

    组件本身不"订阅"（本地 Replay 是拉取式），但**必须**能证明它在动：
    超过 ``max_idle`` 没有成功取价即视为失效（→ HALTED，禁止新成交）。
    """

    name = "feed"
    critical = True
    depends_on = ("clock",)

    def __init__(self, quote_source: Any, max_idle_seconds: float = 120.0) -> None:
        super().__init__()
        self.quote_source = quote_source
        self.max_idle = float(max_idle_seconds)
        self.last_ok: Optional[datetime] = None
        self.last_error = ""
        self.fetches = 0
        self.failures = 0

    def _start(self, ctx: PaperContext) -> None:
        if self.quote_source is None:
            raise RuntimeError("缺少行情源")
        ctx.services["quote_source"] = self.quote_source
        ctx.quote_source = self.quote_source
        self.mark_ok()

    def mark_ok(self) -> None:
        self.last_ok = timebase.current().now()
        self.fetches += 1

    def mark_error(self, err: str) -> None:
        self.failures += 1
        self.last_error = err

    def _health(self):
        if self.last_ok is None:
            return False, "从未成功取价"
        age = (timebase.current().now() - self.last_ok).total_seconds()
        if age > self.max_idle:
            return False, f"行情已 {age:.0f}s 未更新 > {self.max_idle:.0f}s"
        return True, f"age={age:.1f}s fetches={self.fetches} fail={self.failures}"

    def _stop(self, ctx: PaperContext) -> None:
        ctx.services.pop("quote_source", None)


# ---------------------------------------------------------------------- #
# 8. 模拟券商
# ---------------------------------------------------------------------- #
class BrokerComponent(Component):
    name = "broker"
    critical = True
    depends_on = ("feed", "market_status", "settlement", "caps")

    def __init__(self, cfg: Any, state_path: Path,
                 initial_cash: Optional[Dict[str, float]] = None,
                 commission_bps: float = 8.0, min_commission: float = 3.0,
                 slippage_bps: float = 5.0,
                 allow_short: bool = False,
                 deterministic_ids: bool = False) -> None:
        super().__init__()
        self.cfg = cfg
        self.state_path = Path(state_path)
        self.initial_cash = initial_cash
        self.commission_bps = float(commission_bps)
        self.min_commission = float(min_commission)
        self.slippage_bps = float(slippage_bps)
        self.allow_short = bool(allow_short)
        self.deterministic_ids = bool(deterministic_ids)
        self.broker: Any = None

    def _start(self, ctx: PaperContext) -> None:
        from ..broker.paper import PaperBroker
        src = ctx.service("quote_source")
        if src is None:
            raise RuntimeError("缺少行情源（feed 未就绪）")
        if self.allow_short:
            raise RuntimeError("PaperRuntime 本阶段禁止开启卖空（allow_short 必须 False）")
        self.broker = PaperBroker(
            state_path=self.state_path,
            initial_cash=self.initial_cash,
            commission_bps=self.commission_bps,
            min_commission=self.min_commission,
            slippage_bps=self.slippage_bps,
            quote_source=src,
            allow_short=self.allow_short,
            settlement=ctx.service("settlement"),
            market_status=ctx.service("market_status"),
            deterministic_ids=self.deterministic_ids,
        )
        ctx.broker = self.broker
        ctx.services["broker"] = self.broker

    def _health(self):
        if self.broker is None:
            return False, "未初始化"
        try:
            h = self.broker.health()
        except Exception as exc:  # noqa: BLE001
            return False, f"券商健康检查失败：{type(exc).__name__}: {exc}"
        if not h.get("connected"):
            return False, "券商未连接"
        if h.get("is_live"):
            return False, "券商声明为真实盘（PaperRuntime 不允许）"
        return True, f"orders={h.get('orders')} positions={h.get('positions')}"

    def _stop(self, ctx: PaperContext) -> None:
        # 落盘由 PaperBroker 每次写操作保证；这里只解除引用
        self.broker = None
        ctx.services.pop("broker", None)


# ---------------------------------------------------------------------- #
# 9. 风控
# ---------------------------------------------------------------------- #
class RiskComponent(Component):
    name = "risk"
    critical = True
    depends_on = ("market_status", "short_guard", "settlement", "caps")

    def __init__(self, cfg: Any, phase2_override: Dict[str, Any]) -> None:
        super().__init__()
        self.cfg = cfg
        self.phase2_override = dict(phase2_override)
        self.risk: Any = None

    def _start(self, ctx: PaperContext) -> None:
        from ..risk.controller import RiskController
        self.risk = RiskController(
            self.cfg, self.cfg.gate,
            market_status=ctx.service("market_status"),
            short_guard=ctx.service("short_guard"),
            settlement=ctx.service("settlement"),
            caps=ctx.service("caps"),
            phase2_override=self.phase2_override,
        )
        ctx.risk = self.risk
        ctx.services["risk"] = self.risk

    def _health(self):
        if self.risk is None:
            return False, "未初始化"
        if not getattr(self.risk, "phase2_enabled", False):
            return False, "P2 检查未启用（作用域覆盖失效）"
        if self.risk.halt_reason:
            return False, f"风控处于停止状态：{self.risk.halt_reason}"
        return True, f"halt=0 max_pos={self.risk.max_positions}"

    def _stop(self, ctx: PaperContext) -> None:
        self.risk = None
        ctx.services.pop("risk", None)


# ---------------------------------------------------------------------- #
# 10. 策略引擎
# ---------------------------------------------------------------------- #
class EngineComponent(Component):
    name = "engine"
    critical = True
    depends_on = ("broker", "risk")

    def __init__(self, cfg: Any, model: Any, journal: Any,
                 stops_path: Path, phase2_override: Dict[str, Any]) -> None:
        super().__init__()
        self.cfg = cfg
        self.model = model
        self.journal = journal
        self.stops_path = Path(stops_path)
        self.phase2_override = dict(phase2_override)
        self.engine: Any = None

    def _start(self, ctx: PaperContext) -> None:
        from ..strategy.engine import StrategyEngine
        self.engine = StrategyEngine(
            self.cfg, ctx.service("broker"), self.model, ctx.service("risk"),
            self.journal, stops_path=self.stops_path,
            allow_rule_engine_entry=False,
            phase2_override=self.phase2_override,
        )
        ctx.engine = self.engine
        ctx.services["engine"] = self.engine

    def _health(self):
        if self.engine is None:
            return False, "未初始化"
        gw = getattr(self.engine, "quote_gateway", None)
        if gw is None:
            return False, "行情网关缺失（结构旁路）"
        if not getattr(gw, "enabled", False):
            return False, "行情网关未启用（时段校验会被绕过）"
        return True, f"scans={getattr(self.engine, '_scan_count', 0)} gateway=on"

    def _stop(self, ctx: PaperContext) -> None:
        try:
            self.engine._save_stops()          # 止损状态落盘，避免重启丢失
        except Exception as exc:               # noqa: BLE001 - 记录但不掩盖
            self.last_error = f"止损落盘失败：{type(exc).__name__}: {exc}"
        self.engine = None
        ctx.services.pop("engine", None)
