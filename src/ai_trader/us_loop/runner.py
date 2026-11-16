"""美股模拟交易闭环的运行器。

复用清单（不重复造轮子）
------------------------
============================  ==========================================
能力                          复用的现有实现
============================  ==========================================
行情协议                      ``broker/paper.py:57-70 QuoteSource``
模拟撮合/手续费/滑点/持仓      ``broker/paper.py:169 PaperBroker``
决策闸门（AI 权威）            ``ai/authority.py:118 DecisionAuthority``
信号校验/定仓/下单意图/对账    ``strategy/engine.py:571 analyze_symbol``
独立风控（10+ 项检查）         ``risk/controller.py:364 evaluate``
账本（信号/风控/订单/权益）    ``audit/journal.py:192 AuditJournal``
交易时段/日历                  ``market/clock.py`` + ``market/rules.py``
============================  ==========================================

本模块只做三件事：
1. 把真实历史 bar 注入 ``PaperBroker``（:class:`HistoricalQuoteSource`）；
2. 在引擎与模型之间插入 ``AuditedModel``，让每次决策落库；
3. 组织一次"按交易日回放"的运行，并输出可审计证据。

不改 ``strategy/engine.py``、``risk/controller.py``、``broker/paper.py`` 一行。
"""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict as _dc_asdict
from dataclasses import dataclass, field, is_dataclass
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from ..audit.journal import AuditJournal
from ..broker.base import BrokerError
from ..broker.paper import PaperBroker, currency_of, market_of
from ..config import PROJECT_ROOT, AppConfig, build_gate, load_config
from ..risk.controller import OrderIntent, RiskController
from ..safety.halt import HaltStore
from ..strategy.engine import StrategyEngine
from .bars import BarStore
from .decisions import AuditedModel, AIGateway, DecisionLedger, build_llm
from .quote_source import HistoricalQuoteSource

NY = ZoneInfo("America/New_York")
DEFAULT_DECISION_TIME = dtime(10, 30)      # 常规时段内，避开开盘 15 分钟
US_SYMBOLS = ("usAAPL", "usNVDA", "usTSLA")
#: 决策用到的最后一根 bar = 交易日的前一个日历日（历史上就是上一交易日）
BAR_LAG_DAYS = 1


def _asdict(obj: Any) -> Dict[str, Any]:
    """统一把 dataclass / 带 as_dict 的对象 / 普通对象转成 dict。"""
    if hasattr(obj, "as_dict"):
        return obj.as_dict()
    if is_dataclass(obj):
        return _dc_asdict(obj)
    return dict(getattr(obj, "__dict__", {}) or {})


def _orders(broker: Any, limit: int = 200) -> List[Dict[str, Any]]:
    fn = getattr(broker, "list_orders", None)
    if not callable(fn):
        return []
    try:
        rows = fn(limit=limit) or []
    except TypeError:
        rows = fn() or []
    return sorted([_asdict(r) for r in rows], key=lambda x: str(x.get("order_id", "")))


@dataclass
class LoopSettings:
    """一次闭环运行的全部落点（默认全部在项目内，sandbox 时整体外移）。"""

    trade_date: date
    symbols: List[str] = field(default_factory=lambda: list(US_SYMBOLS))
    data_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "data")
    bars_db: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "market_us.db")
    loop_db: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "us_loop.db")
    paper_state: Optional[Path] = None           # None → data_dir/paper_state.json
    audit_db: Optional[Path] = None              # None → cfg.audit.db_path
    audit_jsonl: Optional[Path] = None
    mode: str = "replay"                         # replay | realtime
    sandbox: bool = False
    decision_time: dtime = DEFAULT_DECISION_TIME

    def resolved_paper_state(self) -> Path:
        return Path(self.paper_state) if self.paper_state else self.data_dir / "paper_state.json"


def last_trading_day_on_or_before(d: date, store: BarStore, symbol: str) -> Optional[date]:
    bars = store.load(symbol, d, 1)
    return date.fromisoformat(bars[-1]["trade_date"]) if bars else None


def decision_moment(trade_date: date, at: dtime = DEFAULT_DECISION_TIME) -> datetime:
    """交易日的决策时刻（NY 时区，常规时段内）。"""
    return datetime(trade_date.year, trade_date.month, trade_date.day,
                    at.hour, at.minute, tzinfo=NY)


class PaperLoop:
    """一条可重入的模拟交易闭环。"""

    def __init__(self, settings: LoopSettings, cfg: Optional[AppConfig] = None,
                 llm_override: Optional[Dict[str, str]] = None):
        self.settings = settings
        self.llm_override = dict(llm_override or {})
        cfg = cfg or load_config()
        if settings.sandbox:
            self._relocate(cfg, settings.data_dir)
        self.cfg = cfg

        self.store = BarStore(settings.bars_db)
        self.bars_as_of = settings.trade_date - timedelta(days=BAR_LAG_DAYS)
        self.source = HistoricalQuoteSource(self.store, as_of=self.bars_as_of,
                                            source_label="hist:yahoo:chart:1d")
        p = cfg.paper
        cash_cfg = p.get("initial_cash") or {}
        self.broker = PaperBroker(
            state_path=settings.resolved_paper_state(),
            initial_cash={k: float(v) for k, v in cash_cfg.items()} or None,
            commission_bps=float(p.get("commission_bps", 8.0)),
            min_commission=float(p.get("min_commission", 3.0)),
            slippage_bps=float(p.get("slippage_bps", 5.0)),
            allow_partial_fill=bool(p.get("allow_partial_fill", True)),
            quote_source=self.source,
        )
        # 沙盒一律用沙盒目录内的账本，绝不写项目 data/audit.db
        if settings.audit_db:
            audit_db = Path(settings.audit_db)
        elif settings.sandbox:
            audit_db = settings.data_dir / "audit.db"
        else:
            audit_db = PROJECT_ROOT / str(cfg.get("audit.db_path", "data/audit.db"))
        if settings.audit_jsonl:
            audit_jsonl = Path(settings.audit_jsonl)
        elif settings.sandbox:
            audit_jsonl = settings.data_dir / "audit.jsonl"
        else:
            audit_jsonl = PROJECT_ROOT / str(cfg.get("audit.jsonl_path", "logs/audit.jsonl"))
        self.journal = AuditJournal(
            audit_db, audit_jsonl,
            hash_chain=bool(cfg.get("audit.hash_chain", True)),
        )
        self.risk = RiskController(cfg, cfg.gate)
        self.ledger = DecisionLedger(settings.loop_db)
        self.model, self.model_block_reason = build_llm(cfg, override=self.llm_override)
        self.gateway = AIGateway(
            self.model, self.ledger, run_id="",
            lot_size=int(((cfg.raw.get("markets") or {}).get("us") or {})
                         .get("lot_default", 1) or 1),
            mode=settings.mode)
        self.audited = AuditedModel(self.model, self.gateway,
                                    clock=lambda: self._clock_now)
        self._clock_now: datetime = decision_moment(settings.trade_date, settings.decision_time)
        self.engine = StrategyEngine(
            cfg, self.broker, self.audited, self.risk, self.journal,
            stops_path=settings.data_dir / "stops.json", feed=None)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _relocate(cfg: AppConfig, data_dir: Path) -> None:
        """把 ``data_dir`` 指到沙盒（绝对路径会覆盖 PROJECT_ROOT 前缀）。"""
        cfg.raw.setdefault("system", {})["data_dir"] = str(data_dir)
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        cfg.gate = build_gate(cfg.raw, data_dir=Path(data_dir))
        cfg.halt_store = HaltStore(Path(data_dir))

    def close(self) -> None:
        for obj in (self.journal, self.ledger, self.store):
            try:
                obj.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ #
    def run(self, now: Optional[datetime] = None, run_id: Optional[str] = None) -> Dict[str, Any]:
        """跑一次扫描：行情 → AI → 风控 → 模拟成交 → 账本。"""
        now = now or decision_moment(self.settings.trade_date, self.settings.decision_time)
        self._clock_now = now
        rid = run_id or f"run{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        self.gateway.run_id = rid
        provider = str(getattr(self.model, "name", ""))
        model_id = str(getattr(self.model, "model_id", ""))
        self.ledger.start_run(rid, self.settings.mode,
                              self.settings.trade_date.isoformat(), provider, model_id)

        notes: List[str] = []
        blocked_rows: List[Dict[str, Any]] = []
        if not self.gateway.available():
            # 模型不可用：如实记录"每个标的都被阻断"，让账本里留下证据
            notes.append(f"AI 阻断：{self.model_block_reason}")
            blocked_rows = self._record_blocked_all(now, rid)

        report = self.engine.scan(self.settings.symbols, now=now)

        # 权益快照（每轮一次）
        summary = self.broker.get_summary("USD")
        try:
            self.journal.record_equity("USD", summary.cash, summary.market_value,
                                       summary.equity, summary.day_pnl)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"权益快照写入失败：{exc}")

        outcomes = [o.as_dict() if hasattr(o, "as_dict") else dict(o.__dict__)
                    for o in (report.outcomes or [])]
        by_status: Dict[str, int] = {}
        for o in outcomes:
            st = o.get("stage", "?")
            by_status[st] = by_status.get(st, 0) + 1

        result = {
            "run_id": rid,
            "mode": self.settings.mode,
            "trade_date": self.settings.trade_date.isoformat(),
            "bars_as_of": self.bars_as_of.isoformat(),
            "decided_at": now.isoformat(),
            "model": f"{provider}/{model_id}",
            "model_available": self.gateway.available(),
            "provenance": self.gateway.provenance,
            "endpoint_class": self.gateway.endpoint_class,
            "model_block_reason": self.model_block_reason,
            "scan_blocked_reason": report.blocked_reason,
            "outcomes": outcomes,
            "by_status": by_status,
            "decisions_recorded": len(blocked_rows) + len([
                o for o in outcomes if o.get("signal") and o.get("signal", {}).get("signal_id")]),
            "account": _asdict(summary),
            "notes": notes,
        }
        status = "blocked" if (not self.gateway.available() or report.blocked_reason) else "ok"
        self.ledger.finish_run(rid, status, result, notes)
        result["run_status"] = status
        return result

    def _record_blocked_all(self, now: datetime, rid: str) -> List[Dict[str, Any]]:
        """模型不可用时，为每个标的落一条 blocked 决策（含真实 input_version）。"""
        out: List[Dict[str, Any]] = []
        cur = "USD"
        portfolio = self.engine.build_portfolio(cur, now)
        for sym in self.settings.symbols:
            try:
                ctx, _quote = self.engine._build_context(sym, portfolio)  # noqa: SLF001
            except Exception as exc:  # noqa: BLE001
                out.append({"symbol": sym, "error": f"{type(exc).__name__}: {exc}"})
                continue
            d = self.gateway.record_blocked(
                ctx, "AI_UNAVAILABLE",
                self.model_block_reason or "AI 模型不可用",
                now.astimezone().isoformat(timespec="seconds"))
            out.append(d.as_dict())
        return out

    # ------------------------------------------------------------------ #
    def execute_intent(self, symbol: str, side: str, qty: float, price: float,
                       reason: str, now: Optional[datetime] = None,
                       tag: str = "EXEC-LAYER-TEST",
                       stop_loss: Optional[float] = None,
                       take_profit: Optional[float] = None) -> Dict[str, Any]:
        """**执行层**链路：意图 → 独立风控 → Paper Broker → 账本。

        这条路径**不经过 AI**，用于验证"风控 + 撮合 + 记账 + 持久化"是否正确。
        每一笔的 ``reason`` 都带 ``[tag]`` 前缀，账本里一眼可辨，绝不会被误读成
        AI 决策。AI 决策路径请用 :meth:`run`。

        买入必须带止损（风控 ``per_trade_risk`` 的硬要求），未显式给出时按
        现价 -5% 取，与 ``risk.max_stop_distance_pct=0.12`` 相容。
        """
        now = now or self._clock_now
        symbol = str(symbol).strip().lower()   # 与 engine.scan 的规范化保持一致
        market = market_of(symbol)
        currency = currency_of(symbol)
        portfolio = self.engine.build_portfolio(currency, now)
        p = float(price)
        if side == "buy":
            stop_loss = float(stop_loss) if stop_loss is not None else round(p * 0.95, 4)
            take_profit = float(take_profit) if take_profit is not None else round(p * 1.10, 4)
        else:
            stop_loss, take_profit = stop_loss, take_profit
        intent = OrderIntent(
            symbol=symbol, side=side, qty=float(qty), price=round(p, 4),
            market=market, currency=currency, order_type="limit",
            stop_loss=stop_loss, take_profit=take_profit,
            signal_id="", confidence=0.0, reason=f"[{tag}] {reason}",
            is_exit=(side == "sell"), exit_mode="manual" if side == "sell" else "",
        )
        recent, orders_ok = self.engine._recent_orders_for(symbol)  # noqa: SLF001
        pos = portfolio.position_of(symbol)
        decision = self.risk.evaluate(
            intent=intent, portfolio=portfolio, last_price=float(price),
            recent_orders=recent, now=now, journal=self.journal,
            prev_close=None, orders_known=orders_ok)
        try:
            self.engine._jrecord_risk_decision(decision.as_dict())  # noqa: SLF001
        except Exception:  # noqa: BLE001
            pass
        order = None
        broker_error = ""
        if decision.approved:
            try:
                order = self.engine._execute(  # noqa: SLF001
                    intent, decision, portfolio_held=float(pos.qty) if pos else 0.0)
            except BrokerError as exc:
                broker_error = f"{type(exc).__name__}({getattr(exc, 'code', '')}): {exc}"
        return {"intent": intent.as_dict(), "decision": decision.as_dict(),
                "approved": bool(decision.approved), "order": order,
                "broker_error": broker_error}

    # ------------------------------------------------------------------ #
    def status(self) -> Dict[str, Any]:
        return {
            "trade_date": self.settings.trade_date.isoformat(),
            "bars_as_of": self.bars_as_of.isoformat(),
            "sandbox": self.settings.sandbox,
            "paths": {
                "bars_db": str(self.store.path),
                "loop_db": str(self.ledger.path),
                "paper_state": str(self.broker.state_path),
                "audit_db": str(self.journal.db_path),
            },
            "model": self.model.health() if hasattr(self.model, "health") else {},
            "model_block_reason": self.model_block_reason,
            "authority": self.engine.authority.snapshot(self._clock_now).as_dict()
            if hasattr(self.engine.authority.snapshot(self._clock_now), "as_dict")
            else {},
            "account_usd": _asdict(self.broker.get_summary("USD")),
            "positions": [_asdict(p) for p in self.broker.get_positions()],
            "journal_counts": self.journal.counts() if hasattr(self.journal, "counts") else {},
            "ledger_counts": self.ledger.counts(),
            "gate": {
                "paper_trading": self.cfg.gate.state.paper_trading,
                "live_trading": self.cfg.gate.state.live_trading,
                "order_execution_enabled": self.cfg.gate.state.order_execution_enabled,
                "kill_switch": self.cfg.gate.state.kill_switch,
            },
            "readiness": self.readiness(),
        }

    def readiness(self) -> Dict[str, Any]:
        """逐条对照"闭环完成条件"，给 NOT-MET 明确原因（不猜、不粉饰）。"""
        checks: Dict[str, Any] = {}
        real_rows = [d for d in self.ledger.list_decisions(limit=200)
                     if d.get("provenance") == "real_llm"]
        checks["ai_model_called"] = {
            "met": bool(real_rows),
            "detail": {
                "provenance": self.gateway.provenance,
                "endpoint_class": self.gateway.endpoint_class,
                "real_llm_rows": len(real_rows),
                "note": ("只有 provenance=real_llm 且 endpoint_class=remote 的记录才算"
                         "真实模型调用；loopback 替身一律 stub_transport"),
            },
        }
        bars = {s: self.store.coverage(s) for s in self.settings.symbols}
        checks["real_historical_bars"] = {
            "met": all(v["bars"] > 0 for v in bars.values()),
            "detail": bars,
        }
        decisions = self.ledger.list_decisions(limit=200)
        real_decisions = [d for d in decisions
                          if d.get("provenance") == "real_llm" and d.get("action") != "blocked"]
        checks["ai_decision_produced"] = {
            "met": bool(real_decisions),
            "detail": f"provenance=real_llm 的有效决策 {len(real_decisions)} 条"
                      f"（其余来源不计入）",
        }
        checks["risk_gate_passed"] = {
            "met": None,
            "detail": "需在 AI 可用后由 engine 调用 risk.evaluate 才有意义",
        }
        checks["paper_filled"] = {
            "met": self.broker.get_summary("USD").market_value > 0,
            "detail": _asdict(self.broker.get_summary("USD")),
        }
        return checks

    # ------------------------------------------------------------------ #
    def evidence(self) -> Dict[str, Any]:
        """一次性导出审计证据（不包含任何密钥）。"""
        return {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "settings": {
                "trade_date": self.settings.trade_date.isoformat(),
                "bars_as_of": self.bars_as_of.isoformat(),
                "symbols": list(self.settings.symbols),
                "mode": self.settings.mode,
                "sandbox": self.settings.sandbox,
                "decision_time": self.settings.decision_time.strftime("%H:%M"),
                "paths": {
                    "bars_db": str(self.store.path),
                    "loop_db": str(self.ledger.path),
                    "paper_state": str(self.broker.state_path),
                    "audit_db": str(self.journal.db_path),
                },
            },
            "market_data": self.store.quality_report(self.settings.symbols),
            "quote_source": self.source.evidence(self.settings.symbols),
            "model": self.model.health() if hasattr(self.model, "health") else {},
            "account": _asdict(self.broker.get_summary("USD")),
            "positions": [_asdict(p) for p in self.broker.get_positions()],
            "decisions": self.ledger.list_decisions(limit=50),
            "runs": self.ledger.list_runs(limit=10),
            "journal_counts": self.journal.counts() if hasattr(self.journal, "counts") else {},
            "readiness": self.readiness(),
        }


def restart_proof(settings: LoopSettings, cfg: Optional[AppConfig] = None) -> Dict[str, Any]:
    """重启恢复证据：新建一套实例读同一批文件，比较状态是否一致。"""
    a = PaperLoop(settings, cfg)
    before = {
        "account": _asdict(a.broker.get_summary("USD")),
        "positions": sorted([_asdict(p) for p in a.broker.get_positions()],
                            key=lambda x: x["symbol"]),
        "orders": _orders(a.broker),
        "journal": a.journal.counts() if hasattr(a.journal, "counts") else {},
        "ledger": a.ledger.counts(),
    }
    a.close()

    b = PaperLoop(settings, cfg)
    after = {
        "account": _asdict(b.broker.get_summary("USD")),
        "positions": sorted([_asdict(p) for p in b.broker.get_positions()],
                            key=lambda x: x["symbol"]),
        "orders": _orders(b.broker),
        "journal": b.journal.counts() if hasattr(b.journal, "counts") else {},
        "ledger": b.ledger.counts(),
    }
    b.close()

    # audit_log 是追加写日志，构造实例本身就会写（迁移/恢复），不参与"业务状态一致"
    for side in (before, after):
        if isinstance(side.get("journal"), dict):
            side["journal"] = {k: v for k, v in side["journal"].items() if k != "audit_log"}

    def _eq(x: Any, y: Any) -> bool:
        return json.dumps(x, sort_keys=True, default=str) == json.dumps(y, sort_keys=True, default=str)

    diff = {k: not _eq(before[k], after[k]) for k in before}
    return {"before": before, "after": after, "diff": diff,
            "identical": not any(diff.values())}
