"""PaperRuntime：把四个 P2 组件 + 券商 + 风控 + 引擎装进一个可启动/可停止/
可监控的运行体。

生命周期（严格执行，不允许跳步）
--------------------------------
``STOPPED → PREFLIGHT → INITIALIZING → READY → RUNNING``
``RUNNING → DEGRADED / HALTED``，``任一状态 → STOPPING → STOPPED``

几条不可协商的规则
------------------
1. **不允许半启动**：组件初始化失败 → 逆序回滚 → FAILED。
2. **DEGRADED 不扩大权限**：非关键组件失效时仍可运行，但**禁止新开仓**，
   只允许风险退出（见 :meth:`can_open_new`）。
3. **HALTED 不自动恢复**：关键组件恢复后也必须走完整 ``_revalidate()``，
   并且当前实现要求先 STOPPING→STOPPED 再重新 start —— "进程还活着"
   不是"已恢复"的证据。
4. **幂等**：重复 ``start()`` / ``stop()`` 不会重复初始化或重复订阅。
5. **时间来自 timebase**：回放时全部与回放时钟一致，不混用系统真实时间。
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .. import timebase
from .components import Component, ComponentHealth, ComponentRegistry, RegistryResult
from .components_impl import (
    BrokerComponent,
    CapsComponent,
    ClockComponent,
    EngineComponent,
    FeedComponent,
    MarketStatusComponent,
    PaperContext,
    RiskComponent,
    SettlementComponent,
    ShortGuardComponent,
    StorageComponent,
)
from .invariants import InvariantReport, check_all
from .ledger import FillLedger, fills_from_orders
from .preflight import PreflightReport, run_preflight
from .state import IllegalTransition, RuntimeState, StateMachine

logger = logging.getLogger("ai_trader.paper.runtime")

#: 四个必需组件（缺一不可）
REQUIRED_COMPONENTS = ("market_status", "short_guard", "settlement", "caps")


@dataclass
class TickResult:
    index: int
    at: str
    quotes: int = 0
    scan_outcomes: Dict[str, int] = field(default_factory=dict)
    new_fills: int = 0
    duplicate_fills: int = 0
    orders: int = 0
    invariants_ok: bool = True
    health: str = "OK"
    notes: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"index": self.index, "at": self.at, "quotes": self.quotes,
                "scan": dict(self.scan_outcomes), "new_fills": self.new_fills,
                "duplicate_fills": self.duplicate_fills, "orders": self.orders,
                "invariants_ok": self.invariants_ok, "health": self.health,
                "notes": list(self.notes)}


@dataclass
class RunReport:
    run_id: str
    started_at: str = ""
    finished_at: str = ""
    ticks: int = 0
    orders: int = 0
    fills: int = 0
    duplicates: int = 0
    final_state: str = ""
    halted_reason: str = ""
    invariants: Dict[str, Any] = field(default_factory=dict)
    tick_log: List[Dict[str, Any]] = field(default_factory=list)
    digest: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"run_id": self.run_id, "started_at": self.started_at,
                "finished_at": self.finished_at, "ticks": self.ticks,
                "orders": self.orders, "fills": self.fills,
                "duplicates": self.duplicates, "final_state": self.final_state,
                "halted_reason": self.halted_reason,
                "invariants": self.invariants, "digest": self.digest,
                "tick_log": self.tick_log}


@dataclass
class StartResult:
    ok: bool
    state: str
    already_started: bool = False
    reason: str = ""
    preflight: Optional[Dict[str, Any]] = None
    components: Optional[Dict[str, Any]] = None

    def as_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "state": self.state,
                "already_started": self.already_started, "reason": self.reason,
                "preflight": self.preflight, "components": self.components}


@dataclass
class StopResult:
    ok: bool
    state: str
    already_stopped: bool = False
    release: Optional[Dict[str, Any]] = None
    errors: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "state": self.state,
                "already_stopped": self.already_stopped,
                "release": self.release, "errors": list(self.errors)}


# ---------------------------------------------------------------------- #
class PaperRuntime:
    """本地 Paper/Replay 运行体。"""

    def __init__(self, *, cfg: Any, paper_cfg: Dict[str, Any], clocks: Dict[str, Any],
                 quote_source: Any, model: Any, journal: Any,
                 symbols: List[str], run_dir: Path, clock: Any,
                 explicit_paper: bool = False, allow_live: bool = False,
                 on_tick: Optional[Callable[["PaperRuntime"], None]] = None):
        self.cfg = cfg
        self.paper_cfg = dict(paper_cfg or {})
        self.clocks = dict(clocks or {})
        self.quote_source = quote_source
        self.model = model
        self.journal = journal
        self.symbols = [str(s) for s in (symbols or [])]
        self.run_dir = Path(run_dir)
        self.explicit_paper = bool(explicit_paper)
        self.allow_live = bool(allow_live)
        self.on_tick = on_tick

        self.run_id = self._make_run_id()
        self.machine = StateMachine(run_id=self.run_id)
        self.registry = ComponentRegistry()
        self.ctx = PaperContext(cfg=cfg, clocks=self.clocks, symbols=self.symbols,
                                clock=clock, journal=journal, run_id=self.run_id)
        self._clock = clock
        self._started_once = False
        self._stop_errors: List[str] = []
        self._ticks = 0
        self._halted_reason = ""
        self._degraded_reason = ""
        self._last_invariants: InvariantReport = InvariantReport()
        self._recovery: Any = None
        self._tick_log: List[Dict[str, Any]] = []
        self._lock = __import__("threading").RLock()

        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = self.run_dir / "paper_audit.db"
        self._state_path = self.run_dir / "paper_state.json"
        self._ledger_path = self.run_dir / "paper_fills.jsonl"
        self._stops_path = self.run_dir / "stops.json"
        self.ledger = FillLedger(self._ledger_path)
        # 审计库：未注入时在本运行目录内自建（**不**复用线上 audit.db）
        # 记录所有权：自己开的连接必须自己关，否则长跑会持续泄漏句柄
        self._owns_journal = journal is None
        if self.journal is None:
            from ..audit.journal import AuditJournal
            self.journal = AuditJournal(
                self._db_path, self.run_dir / "audit.jsonl", hash_chain=True)
            self.ctx.journal = self.journal
        self._build_registry()

    # ------------------------------------------------------------------ #
    def _make_run_id(self) -> str:
        """运行 ID 用**墙钟**生成。

        回放时钟停在历史日期上，用它做 ID 会让"哪次运行更新"变得不可判断，
        还会让不同批次出现同名目录。ID 属于运维标识，不属于业务时间。
        """
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        return f"paper-{stamp}-{uuid.uuid4().hex[:8]}"

    def _phase2_scope(self) -> Dict[str, Any]:
        """**作用域内**启用 P2 检查 —— 不改全局 ``us_phase2.enabled``。"""
        base = dict(self.cfg.raw.get("us_phase2") or {})
        scope = dict(base)
        scope["enabled"] = True
        checks = dict(base.get("checks") or {})
        checks.update({"market_status": True, "order_policy": True,
                       "short": True, "settlement": True})
        scope["checks"] = checks
        scope["_scope"] = "paper_runtime"
        return scope

    def _build_registry(self) -> None:
        p = self.paper_cfg
        broker_cfg = dict(p.get("broker") or {})
        cash = broker_cfg.get("initial_cash") or {"USD": 100_000.0}
        self.registry.register(ClockComponent())
        self.registry.register(StorageComponent(self._db_path,
                                                self.run_dir / "audit.jsonl"))
        self.registry.register(CapsComponent())
        self.registry.register(ShortGuardComponent(
            mode=_short_mode(p), require_operator_approval_live=True))
        self.registry.register(SettlementComponent(
            currency=str((p.get("settlement") or {}).get("currency", "USD")),
            opening_cash=float(cash.get("USD", 0.0))))
        self.registry.register(MarketStatusComponent(
            max_quote_age_seconds=float((p.get("feed") or {}).get(
                "max_quote_age_seconds", 60))))
        self.registry.register(FeedComponent(
            self.quote_source,
            max_idle_seconds=float((p.get("feed") or {}).get("max_idle_seconds", 120))))
        self.registry.register(BrokerComponent(
            self.cfg, self._state_path, initial_cash={k: float(v) for k, v in cash.items()},
            commission_bps=float(broker_cfg.get("commission_bps", 8.0)),
            min_commission=float(broker_cfg.get("min_commission", 3.0)),
            slippage_bps=float(broker_cfg.get("slippage_bps", 5.0)),
            allow_short=False,
            deterministic_ids=bool(self.paper_cfg.get("replay"))))
        self.registry.register(RiskComponent(self.cfg, self._phase2_scope()))
        self.registry.register(EngineComponent(
            self.cfg, self.model, self.journal, self._stops_path,
            self._phase2_scope()))

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def start(self) -> StartResult:
        with self._lock:
            st = self.machine.state
            if st in (RuntimeState.READY, RuntimeState.RUNNING):
                return StartResult(True, st.value, already_started=True,
                                   reason="已启动（幂等）")
            if st in (RuntimeState.HALTED, RuntimeState.FAILED, RuntimeState.DEGRADED):
                return StartResult(False, st.value,
                                   reason=f"{st.value} 不允许直接重启，必须先 stop() 再 start()")
            if st is RuntimeState.INITIALIZING:
                return StartResult(False, st.value, reason="正在初始化（拒绝并发启动）")

            # ---- PREFLIGHT ------------------------------------------- #
            self.machine.to_preflight("start()")
            pre = run_preflight(
                cfg=self.cfg, paper_cfg=self.paper_cfg, clocks=self.clocks,
                quote_source=self.quote_source, symbols=self.symbols,
                db_path=self._db_path, allow_live=self.allow_live,
                explicit_paper_flag=self.explicit_paper)
            if not pre.ok:
                reason = "启动前检查未通过：" + "; ".join(
                    f"{c.name}({c.detail})" for c in pre.failures)
                self.machine.to_failed(reason)
                self._audit("preflight_failed", pre.as_dict())
                return StartResult(False, self.machine.state.value, reason=reason,
                                   preflight=pre.as_dict())

            # ---- INITIALIZING ---------------------------------------- #
            self.machine.to_initializing("preflight passed")
            missing = self.registry.check_required(REQUIRED_COMPONENTS)
            if missing:
                reason = f"缺少必需组件：{missing}"
                self.machine.to_failed(reason)
                return StartResult(False, self.machine.state.value, reason=reason,
                                   preflight=pre.as_dict())

            res: RegistryResult = self.registry.start_all(self.ctx)
            if not res.ok:
                self.machine.to_failed(f"组件初始化失败：{res.failed}")
                self._audit("init_failed", res.as_dict())
                return StartResult(False, self.machine.state.value,
                                   reason=f"组件初始化失败：{res.failed}",
                                   preflight=pre.as_dict(), components=res.as_dict())

            # ---- 健康门槛：全部关键组件必须健康 ---------------------- #
            agg, reasons = self.registry.aggregate()
            if agg == "HALTED":
                self.registry.stop_all(self.ctx)
                self.machine.to_failed("初始化后健康检查未通过：" + "; ".join(reasons))
                return StartResult(False, self.machine.state.value,
                                   reason="初始化后健康检查未通过：" + "; ".join(reasons),
                                   preflight=pre.as_dict(),
                                   components=self.registry.as_dict())

            # ---- 一致性扫描（崩溃恢复的关键一步，必须先于任何交易）---- #
            from .recovery import scan as recovery_scan
            load_rep = self.ledger.load()
            self._recovery = recovery_scan(self.ctx.service("broker"), self.ledger)
            self._audit("recovery_scan", {
                "ledger": load_rep.as_dict(),
                "recovery": self._recovery.as_dict()})
            if not self._recovery.ok:
                reason = ("启动恢复扫描发现不一致：" +
                          "; ".join(a.detail for a in self._recovery.actions
                                    if a.kind in ("orphan_in_ledger",
                                                  "missing_in_ledger",
                                                  "backfill_failed")))
                self.registry.stop_all(self.ctx)
                self.machine.to_failed(reason)
                return StartResult(False, self.machine.state.value, reason=reason,
                                   preflight=pre.as_dict(),
                                   components=self.registry.as_dict())

            # ---- 账务不变量（启动即校验，不等到第一个 tick）---- #
            _b = self.ctx.service("broker")
            if _b is not None:
                self._last_invariants = check_all(
                    _b, self.ledger,
                    settlement=self.ctx.service("settlement"),
                    short_allowed=bool(getattr(_b, "allow_short", False)))
                self._audit("startup_invariants", self._last_invariants.as_dict())
                if not self._last_invariants.ok:
                    reason = ("启动不变量校验失败：" + "; ".join(
                        f"{r.name}({r.detail})"
                        for r in self._last_invariants.critical_failures))
                    self.registry.stop_all(self.ctx)
                    self.machine.to_failed(reason)
                    return StartResult(False, self.machine.state.value,
                                       reason=reason, preflight=pre.as_dict(),
                                       components=self.registry.as_dict())

            self.machine.to_ready("all components healthy")
            self._started_once = True
            self._audit("runtime_ready", {
                "run_id": self.run_id, "components": res.started,
                "health": agg, "symbols": self.symbols,
                "scope": "paper_runtime"})
            return StartResult(True, self.machine.state.value,
                               preflight=pre.as_dict(),
                               components=self.registry.as_dict())

    # ------------------------------------------------------------------ #
    def to_running(self) -> bool:
        changed = self.machine.to_running("run()")
        if changed:
            self._audit("runtime_running", {"run_id": self.run_id})
        return changed

    def can_open_new(self) -> Tuple[bool, str]:
        """是否允许**新开仓**。DEGRADED 明确禁止（不扩大权限）。"""
        st = self.machine.state
        if st is RuntimeState.RUNNING:
            return True, ""
        if st is RuntimeState.DEGRADED:
            return False, f"运行降级：{self._degraded_reason}"
        return False, f"运行状态 {st.value} 不允许新开仓"

    def exits_allowed(self) -> Tuple[bool, str]:
        if self.machine.exits_allowed():
            return True, ""
        return False, f"运行状态 {self.machine.state.value} 不允许任何交易"

    # ------------------------------------------------------------------ #
    def tick(self, *, index: Optional[int] = None) -> TickResult:
        """执行一个运行周期：行情 → 策略 → 风控 → 订单 → 入账 → 不变量。"""
        if self.machine.state not in (RuntimeState.RUNNING, RuntimeState.DEGRADED):
            raise RuntimeError(f"tick 只在 RUNNING/DEGRADED 下允许，当前 {self.machine.state.value}")
        if self.model is not None and hasattr(self.model, "set_tick"):
            self.model.set_tick(self._ticks)
        now = timebase.now()
        tr = TickResult(index=self._ticks, at=now.isoformat(timespec="seconds"))

        # 1) 行情推进：把当前 tick 的价格写入市场状态（新鲜度）
        ms = self.ctx.service("market_status")
        feed = self.registry.get("feed")
        for sym in self.symbols:
            try:
                q = self.quote_source.quote(sym)
                ts = getattr(q, "ts", None)
                if isinstance(ts, str):
                    ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if ts is None:
                    raise ValueError("行情缺少时间戳")
                if ms is not None:
                    ms.record_trade(sym, float(q.last), ts)
                tr.quotes += 1
                if isinstance(feed, FeedComponent):
                    feed.mark_ok()
            except Exception as exc:  # noqa: BLE001
                tr.notes.append(f"quote {sym}: {type(exc).__name__}: {exc}")
                if isinstance(feed, FeedComponent):
                    feed.mark_error(f"{sym}: {exc}")

        # 2) 健康闸门：任何关键组件失效 → HALT，本轮不再交易
        agg, reasons = self.registry.aggregate()
        tr.health = agg
        if agg == "HALTED":
            self._halt("关键组件失效：" + "; ".join(reasons))
            tr.notes.append("HALTED：" + "; ".join(reasons))
            self._ticks += 1
            self._finish_tick(tr)
            return tr
        if agg == "DEGRADED" and self.machine.state is RuntimeState.RUNNING:
            self._degraded("非关键组件失效：" + "; ".join(reasons))

        # 3) 策略与订单（只在允许新开仓时跑 scan；退出永远跑）
        engine = self.ctx.service("engine")
        if engine is not None:
            allow, why = self.can_open_new()
            if allow:
                try:
                    rep = engine.scan(self.symbols, now=now)
                    tr.scan_outcomes = dict(rep.summary)
                except Exception as exc:  # noqa: BLE001
                    tr.notes.append(f"scan 失败：{type(exc).__name__}: {exc}")
                    self._halt(f"策略扫描异常：{exc}")
            else:
                tr.notes.append(f"跳过 scan：{why}")
            try:
                engine.monitor(now=now)
            except Exception as exc:  # noqa: BLE001
                tr.notes.append(f"monitor 失败：{type(exc).__name__}: {exc}")
                self._halt(f"持仓监控异常：{exc}")

        # 4) 成交入账（幂等）+ 不变量
        self._ingest_fills(tr)
        self._ticks += 1
        self._finish_tick(tr)
        return tr

    def _finish_tick(self, tr: TickResult) -> None:
        self._tick_log.append(tr.as_dict())
        if len(self._tick_log) > 5000:
            self._tick_log = self._tick_log[-5000:]
        if self.on_tick is not None:
            try:
                self.on_tick(self)
            except Exception as exc:  # noqa: BLE001
                logger.error("on_tick 回调异常：%s", exc)

    def _ingest_fills(self, tr: TickResult) -> None:
        broker = self.ctx.service("broker")
        if broker is None:
            return
        orders = broker.orders_snapshot()
        tr.orders = len(orders)
        for fill in fills_from_orders(orders):
            res = self.ledger.post(fill)
            if res.accepted and not res.duplicate:
                tr.new_fills += 1
            elif res.duplicate:
                tr.duplicate_fills += 1
            elif not res.accepted:
                tr.notes.append(f"入账失败 {fill.fill_id}: {res.reason}")
        settlement = self.ctx.service("settlement")
        self._last_invariants = check_all(
            broker, self.ledger, settlement=settlement,
            short_allowed=bool(getattr(broker, "allow_short", False)))
        tr.invariants_ok = self._last_invariants.ok
        if not tr.invariants_ok:
            failed = "; ".join(f"{r.name}({r.detail})"
                               for r in self._last_invariants.critical_failures)
            tr.notes.append("不变量失败：" + failed)
            self._halt("账户不变量失败：" + failed)

    # ------------------------------------------------------------------ #
    def _halt(self, reason: str) -> None:
        if self.machine.state is RuntimeState.HALTED:
            return
        self._halted_reason = reason
        try:
            self.machine.to_halted(reason)
        except IllegalTransition:
            self.machine.to_stopping(reason)
        self._audit("runtime_halted", {"reason": reason})

    def _degraded(self, reason: str) -> None:
        self._degraded_reason = reason
        try:
            self.machine.to_degraded(reason)
        except IllegalTransition:
            return
        self._audit("runtime_degraded", {"reason": reason})

    # ------------------------------------------------------------------ #
    def supervise(self) -> Dict[str, Any]:
        """健康巡检：只在 DEGRADED→RUNNING 时做**完整重新校验**。"""
        agg, reasons = self.registry.aggregate()
        st = self.machine.state
        action = "none"
        if agg == "HALTED" and st is not RuntimeState.HALTED:
            self._halt("巡检：关键组件失效：" + "; ".join(reasons))
            action = "halt"
        elif st is RuntimeState.DEGRADED:
            if agg == "OK":
                ok, why = self._revalidate()
                if ok:
                    self.machine.to_running("巡检恢复：重新校验通过")
                    action = "recovered"
                else:
                    action = f"still_degraded:{why}"
        elif agg == "DEGRADED" and st is RuntimeState.RUNNING:
            self._degraded("巡检：非关键组件失效：" + "; ".join(reasons))
            action = "degrade"
        return {"state": self.machine.state.value, "aggregate": agg,
                "reasons": reasons, "action": action}

    def _revalidate(self) -> Tuple[bool, str]:
        """恢复前的**完整**校验：不是"进程还活着"就放行。"""
        agg, reasons = self.registry.aggregate()
        if agg != "OK":
            return False, "组件仍不健康：" + "; ".join(reasons)
        pre = run_preflight(
            cfg=self.cfg, paper_cfg=self.paper_cfg, clocks=self.clocks,
            quote_source=self.quote_source, symbols=self.symbols,
            db_path=self._db_path, allow_live=self.allow_live,
            explicit_paper_flag=self.explicit_paper)
        if not pre.ok:
            return False, "重新预检未通过：" + "; ".join(c.name for c in pre.failures)
        broker = self.ctx.service("broker")
        if broker is not None:
            rep = check_all(broker, self.ledger,
                            settlement=self.ctx.service("settlement"),
                            short_allowed=bool(getattr(broker, "allow_short", False)))
            if not rep.ok:
                return False, "恢复校验时不变量失败：" + "; ".join(
                    r.name for r in rep.critical_failures)
        self._degraded_reason = ""
        return True, ""

    # ------------------------------------------------------------------ #
    def run_ticks(self, ticks: int, *, fail_on_halt: bool = False) -> RunReport:
        return self._run(max_ticks=int(ticks), max_seconds=None,
                         fail_on_halt=fail_on_halt)

    def run_for(self, seconds: float, *, tick_interval: float = 1.0,
                fail_on_halt: bool = False) -> RunReport:
        return self._run(max_ticks=None, max_seconds=float(seconds),
                         tick_interval=tick_interval, fail_on_halt=fail_on_halt)

    def _run(self, *, max_ticks: Optional[int], max_seconds: Optional[float],
             tick_interval: float = 0.0, fail_on_halt: bool = False) -> RunReport:
        started = timebase.now()
        rep = RunReport(run_id=self.run_id, started_at=started.isoformat(timespec="seconds"))
        self.to_running()
        t0 = time.monotonic()
        n = 0
        while True:
            if max_ticks is not None and n >= max_ticks:
                break
            if max_seconds is not None and (time.monotonic() - t0) >= max_seconds:
                break
            tr = self.tick()
            n += 1
            if self.machine.state is RuntimeState.HALTED:
                if fail_on_halt:
                    rep.halted_reason = self._halted_reason
                    break
                break
            if tick_interval:
                time.sleep(tick_interval)
        rep.finished_at = timebase.now().isoformat(timespec="seconds")
        rep.ticks = n
        rep.orders = int(self._last_tick_orders())
        rep.fills = len(self.ledger.fills())
        rep.duplicates = sum(t.get("duplicate_fills", 0) for t in self._tick_log)
        rep.final_state = self.machine.state.value
        rep.halted_reason = rep.halted_reason or self._halted_reason
        rep.invariants = self._last_invariants.as_dict()
        rep.tick_log = list(self._tick_log[-50:])
        rep.digest = self.digest()
        self._audit("run_finished", rep.as_dict() | {"tick_log": "..."})
        return rep

    def _last_tick_orders(self) -> int:
        return int(self._tick_log[-1]["orders"]) if self._tick_log else 0

    # ------------------------------------------------------------------ #
    def digest(self) -> str:
        """运行指纹：成交账本 + 账户快照。用于"同场景多次运行结果一致"比对。"""
        import hashlib
        broker = self.ctx.service("broker")
        payload = {
            "fills": self.ledger.digest(),
            "orders": [],
            "cash": {},
        }
        if broker is not None:
            payload["cash"] = {k: round(float(v), 6)
                               for k, v in (getattr(broker, "_state", {}) or {})
                               .get("cash", {}).items()}
            for o in broker.orders_snapshot():
                payload["orders"].append({
                    "id": o.get("order_id"), "sym": o.get("symbol"),
                    "side": o.get("side"), "qty": o.get("qty"),
                    "status": o.get("status"), "filled": o.get("filled_qty"),
                    "px": o.get("avg_fill_price")})
        blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------ #
    def stop(self, reason: str = "stop()") -> StopResult:
        with self._lock:
            st = self.machine.state
            if st is RuntimeState.STOPPED:
                return StopResult(True, st.value, already_stopped=True)
            if st is RuntimeState.STOPPING:
                # 已经有人在关，返回当前状态而不是并发再关一次
                return StopResult(True, st.value, already_stopped=True,
                                  errors=list(self._stop_errors))
            if st is RuntimeState.FAILED:
                # FAILED 可以直接落 STOPPED（组件可能已经回滚过）
                self.machine.to_stopping(reason)
            else:
                try:
                    self.machine.to_stopping(reason)
                except IllegalTransition as exc:
                    self._audit("stop_illegal", {"error": str(exc)})
                    return StopResult(False, st.value, errors=[str(exc)])

            res = self.registry.stop_all(self.ctx)
            self._stop_errors = list(res.errors)
            self.machine.to_stopped("released")
            # 审计必须**先写**，再关连接 —— 否则最后一条停止记录写不进去
            self._audit("runtime_stopped", {
                "ok": res.ok, "errors": res.errors, "run_id": self.run_id,
                "release_order": list(reversed(self.registry.order()))})
            # 释放自建审计库连接（注入的 journal 由调用方负责关闭）
            if getattr(self, "_owns_journal", False) and self.journal is not None:
                try:
                    self.journal.close()
                except Exception as exc:  # noqa: BLE001 - 关闭失败要记录，不假装成功
                    self._stop_errors.append(
                        f"关闭审计库失败：{type(exc).__name__}: {exc}")
            return StopResult(res.ok, self.machine.state.value,
                              release=res.as_dict(), errors=list(res.errors))

    # ------------------------------------------------------------------ #
    def health(self) -> Dict[str, Any]:
        agg, reasons = self.registry.aggregate()
        return {"run_id": self.run_id, "state": self.machine.state.value,
                "aggregate": agg, "reasons": reasons,
                "components": {n: h.as_dict() for n, h in self.registry.health_all().items()},
                "halted_reason": self._halted_reason,
                "degraded_reason": self._degraded_reason,
                "ticks": self._ticks,
                "can_open_new": self.can_open_new()[0],
                "exits_allowed": self.exits_allowed()[0]}

    def snapshot(self) -> Dict[str, Any]:
        broker = self.ctx.service("broker")
        led = self.ledger.stats()
        return {"run_id": self.run_id, "state": self.machine.state.value,
                "machine": self.machine.as_dict(), "health": self.health(),
                "ledger": led, "invariants": self._last_invariants.as_dict(),
                "account": (broker.health() if broker is not None else {}),
                "ticks": self._ticks}

    # ------------------------------------------------------------------ #
    def _audit(self, event: str, detail: Dict[str, Any]) -> None:
        """写审计：DB 不可用时退化为 JSONL，绝不只留在内存。"""
        payload = {"event": event, "run_id": self.run_id,
                   "at": timebase.now().isoformat(timespec="seconds"),
                   "detail": detail}
        journal = self.journal
        if journal is not None:
            try:
                journal.log(f"paper.{event}", detail)
                return
            except Exception as exc:  # noqa: BLE001
                payload["detail"] = dict(detail) | {"_journal_error": str(exc)}
        try:
            with open(self.run_dir / "runtime.jsonl", "a", encoding="utf-8",
                      newline="\n") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        except OSError:
            logger.error("审计写入失败：%s", event)


def _short_mode(paper_cfg: Dict[str, Any]):
    from ..broker.short import ShortMode
    raw = str((paper_cfg.get("short") or {}).get("mode", "disabled")).lower()
    try:
        mode = ShortMode(raw)
    except ValueError:
        mode = ShortMode.DISABLED
    # 不在构造期抛错：把"配置违规"留到组件初始化阶段 → 整体 FAILED（可审计）
    return mode
