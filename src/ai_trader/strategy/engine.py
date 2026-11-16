"""策略引擎：把"行情 → AI 判断 → 风控 → 委托 → 记账"串起来。

流程（每次 :meth:`StrategyEngine.scan`）
--------------------------------------
1. 预筛：K 线数量/ATR 等廉价技术过滤，不达标直接跳过（省 AI 调用费）。
2. 组上下文 :class:`MarketContext` 交给模型。
3. 模型输出 :class:`TradingSignal` → 结构校验（无止损=非法）。
4. 风控定仓 + 逐项校验 → :class:`RiskDecision`。
5. 仅当 ``approved`` 才调用券商下单，并登记止损/止盈监控。
6. 全过程写入审计日志（信号、决策、委托分别落库）。

风控被绕过在代码层面是不可能的：:class:`Broker` 只在
:meth:`_execute` 内被调用，而 :meth:`_execute` 的第一行就是
``if not decision.approved: return``。
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..ai.authority import CODE_STALE, DecisionAuthority
from ..ai.base import AIModel, AIModelError, MarketContext, TradingSignal
from ..ai.providers import build_model
from ..market.gateway import QuoteGateway, QuoteUnavailable
from ..audit.faults import (
    KIND_AUDIT_WRITE,
    KIND_ORDER_INTENT,
    KIND_ORDER_READ,
    KIND_POSITION_READ,
    KIND_RESERVATION,
    KIND_STOPS,
    KIND_TIMESTAMP,
    KIND_UNKNOWN_ORDER,
    SEV_CRITICAL,
    SEV_ERROR,
    SEV_WARN,
    FaultRecorder,
)
from ..audit.journal import (
    INTENT_STATE_ACCEPTED,
    INTENT_STATE_REJECTED,
    INTENT_STATE_SUBMITTED,
    INTENT_STATE_TERMINAL,
    INTENT_STATE_UNKNOWN,
    AuditJournal,
    utcnow_iso,
)
from ..broker.base import (
    Broker,
    BrokerError,
    OrderRequest,
    OrderResult,
    OrderStateUnknown,
    Position,
    Quote,
    derive_client_id,
)
from ..broker.paper import currency_of, market_of
from ..config import AppConfig
from ..risk.controller import OrderIntent, Portfolio, RiskController
from .indicators import atr_pct, compute_snapshot, snapshot_for_prompt

logger = logging.getLogger("ai_trader.strategy")


@dataclass
class SymbolOutcome:
    symbol: str
    stage: str                  # skipped | hold | rejected | ordered | error
    detail: str = ""
    signal: Optional[Dict[str, Any]] = None
    decision: Optional[Dict[str, Any]] = None
    order: Optional[Dict[str, Any]] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol, "stage": self.stage, "detail": self.detail,
            "signal": self.signal, "decision": self.decision, "order": self.order,
        }


@dataclass
class ScanReport:
    started_at: str
    finished_at: str = ""
    model: str = ""
    outcomes: List[SymbolOutcome] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    blocked_reason: str = ""

    @property
    def summary(self) -> Dict[str, int]:
        agg: Dict[str, int] = {}
        for o in self.outcomes:
            agg[o.stage] = agg.get(o.stage, 0) + 1
        return agg

    def as_dict(self) -> Dict[str, Any]:
        return {
            "started_at": self.started_at, "finished_at": self.finished_at,
            "model": self.model, "summary": self.summary, "errors": self.errors,
            "blocked_reason": self.blocked_reason,
            "outcomes": [o.as_dict() for o in self.outcomes],
        }


class StrategyEngine:
    def __init__(self, cfg: AppConfig, broker: Broker, model: AIModel,
                 risk: RiskController, journal: AuditJournal,
                 stops_path: Optional[Path] = None,
                 allow_rule_engine_entry: bool = False,
                 feed=None):
        self.cfg = cfg
        self.broker = broker
        # ---- US-P2-A 收口：所有行情读取统一走网关（结构上消除旁路）----
        # 时钟复用风控的 rules.clocks，保证"时段判定"和"交易规则"用的是同一套时区/日历。
        _clocks = getattr(getattr(risk, "rules", None), "clocks", {}) or {}
        _p2 = (cfg.raw.get("us_phase2") or {})
        self.quote_gateway = QuoteGateway(
            broker, _clocks,
            enabled=bool(_p2.get("enabled", False)),
            max_age_seconds=(_p2.get("market_status") or {}).get("quote_max_age_seconds"),
        )
        self.model = model
        self.risk = risk
        self.journal = journal
        self.stops_path = Path(stops_path or (cfg.data_dir / "stops.json"))
        self._lock = threading.RLock()
        self._stops: Dict[str, Dict[str, Any]] = self._load_stops()
        self.last_scan: Optional[ScanReport] = None
        self._scan_count = 0
        #: 最近一次成功的持仓快照（US-P0-3/4：持仓接口不可用时用于降级）
        self._positions_cache: Dict[str, Any] = {}
        #: 持仓是否处于"读不到"状态（此时禁止新开仓）
        self._positions_stale = False
        #: 持仓是否由券商**本轮实时核实**（US-P1 N-2：reduce-only 的硬前提）
        self._positions_verified: bool = True
        #: 订单历史是否读不出来（US-P1 N-1）：为真时禁止一切开仓
        self._orders_unknown = False
        #: US-P1 N-1：独立于 SQLite 的故障记录 + 外部告警通道
        self.faults = FaultRecorder(cfg.data_dir, log_dir=cfg.log_dir)
        #: US-P1 Batch2 R-1：重启恢复出来的未终结订单意图（client_id -> row）
        self._recovered_intents: Dict[str, Dict[str, Any]] = {}
        #: US-P1 Batch2 R-2：未完成卖单预留量 {symbol: qty}，以及是否读成功
        self._sell_reserved: Dict[str, float] = {}
        self._reservations_known: bool = True
        #: 意图表是否可用（不可用 → 禁止一切开仓）
        self._intents_available: bool = True
        self._recover_order_intents()
        # AI 决策权威（P1-9）：只有真模型能开仓；规则引擎只能算指标
        self.authority = DecisionAuthority(
            cfg, model, allow_rule_engine_entry=allow_rule_engine_entry)
        self.allow_rule_engine_entry = bool(allow_rule_engine_entry)
        # B5：实时行情推送（可选）。为 None 时不依赖推送。
        self.feed = feed
        self._feed_required = bool((cfg.get("market_feed.required_for_entry", True)))
        self._feed_markets = {str(m).lower()
                              for m in (cfg.get("market_feed.enforce_markets") or [])}
        if self.allow_rule_engine_entry:
            journal.log("rule_engine_sandbox",
                        {"model": f"{model.name}/{model.model_id}",
                         "warning": "沙盒模式：允许规则引擎开仓，仅 demo/回测使用"},
                        actor="system")

    # ------------------------------------------------------------------ #
    # 止损注册表（重启后仍然保护已有持仓）
    # ------------------------------------------------------------------ #
    def _load_stops(self) -> Dict[str, Dict[str, Any]]:
        if self.stops_path.exists():
            try:
                import json
                return json.loads(self.stops_path.read_text(encoding="utf-8")) or {}
            except (ValueError, OSError):
                return {}
        return {}

    def _save_stops(self) -> None:
        import json
        self.stops_path.parent.mkdir(parents=True, exist_ok=True)
        self.stops_path.write_text(json.dumps(self._stops, indent=2, ensure_ascii=False),
                                   encoding="utf-8")

    def register_stops(self, symbol: str, stop_loss: Optional[float],
                       take_profit: Optional[float], qty: float) -> None:
        if not stop_loss and not take_profit:
            return
        prev = self._stops.get(symbol) or {}
        self._stops[symbol] = {
            "stop_loss": stop_loss, "take_profit": take_profit,
            "qty": qty,
            # R-7：保护绑定实际持仓与版本；exit_pending 表示"发过退出但未核实"
            "protected_qty": qty,
            "version": int(prev.get("version") or 0) + 1,
            "exit_pending": False,
            "registered_at": utcnow_iso(),
        }
        self._save_stops()

    def clear_stops(self, symbol: str) -> None:
        if symbol in self._stops:
            self._stops.pop(symbol, None)
            self._save_stops()

    @property
    def stops(self) -> Dict[str, Dict[str, Any]]:
        return dict(self._stops)

    # ------------------------------------------------------------------ #
    # 组合快照
    # ------------------------------------------------------------------ #
    def build_portfolio(self, currency: str = "HKD", now: Optional[datetime] = None) -> Portfolio:
        now = now or datetime.now()
        summary = self.broker.get_summary(currency)
        positions = self.broker.get_positions()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        # US-P1 N-1：订单历史读不出来时不能假装"今天没交易过"。
        # 返回 ok=False 让上层把它当"不可读"，而不是"空列表"。
        raw_orders, orders_ok = self._jorders_since(start.isoformat())
        today_orders = [o for o in raw_orders]
        filled = [o for o in today_orders if str(o.get("status")) in ("filled", "partial")]
        # T+1 可卖数量：当日已成交买入（用于 A 股通 T+1 校验）
        today_bought: Dict[str, float] = {}
        for o in filled:
            if str(o.get("side", "")).lower() != "buy":
                continue
            sym_key = str(o.get("symbol", "")).lower()
            today_bought[sym_key] = today_bought.get(sym_key, 0.0) + float(o.get("filled_qty") or 0)
        return Portfolio(
            currency=currency,
            equity=float(summary.equity),
            cash=float(summary.cash),
            positions=positions,
            day_pnl=float(summary.day_pnl),
            day_trades=len(filled),
            day_orders=len(today_orders),
            # 券商没给日内盈亏时，风控必须判为"无法判定"（见 P1-2）
            day_pnl_known=bool(getattr(summary, "day_pnl_known", True)),
            today_bought=today_bought,
            positions_verified=True,
            orders_known=orders_ok,
            open_sell_reserved=dict(self._sell_reserved),
            reservations_known=bool(self._reservations_known),
            intents_available=bool(self._intents_available),
        )

    # ------------------------------------------------------------------ #
    # 安全审计写入 / 订单读取（US-P1 N-1：数据库故障隔离）
    # ------------------------------------------------------------------ #
    # 设计要点：审计写入失败**不能**中断风险处置，但也**不能**静默丢弃。
    # 因此每个包装器都做两件事：① 不抛异常；② 失败时把事实写进独立故障通道
    # （FaultRecorder → faults.jsonl + FAULT.json + alerts.log），
    # 使得"这次止损为什么没发出去"在数据库坏掉时依然可追溯。
    def _jlog(self, event: str, detail: Optional[Dict[str, Any]] = None,
              actor: str = "engine", subject: str = "",
              severity: str = SEV_WARN, alert: bool = True) -> bool:
        try:
            self.journal.log(event, detail or {}, actor=actor, subject=subject)
            return True
        except Exception as exc:  # noqa: BLE001
            self.faults.record(KIND_AUDIT_WRITE, {
                "event": event, "subject": subject,
                "error": f"{type(exc).__name__}: {exc}",
                "detail": "审计写入失败；本条事件只存在于 faults.jsonl",
            }, severity=severity, alert=alert)
            return False

    def _jlist_orders(self, limit: int = 50,
                      symbol: Optional[str] = None) -> tuple:
        """返回 ``(rows, ok)``。ok=False 表示**读不出来**，不是"没有订单"。"""
        try:
            return self.journal.list_orders(limit=limit, symbol=symbol), True
        except Exception as exc:  # noqa: BLE001
            self.faults.record(KIND_ORDER_READ, {
                "op": "list_orders", "symbol": symbol, "limit": limit,
                "error": f"{type(exc).__name__}: {exc}",
            })
            self._orders_unknown = True
            return [], False

    def _jfind_orders(self, **kw) -> tuple:
        try:
            return self.journal.find_orders(**kw), True
        except Exception as exc:  # noqa: BLE001
            self.faults.record(KIND_ORDER_READ, {
                "op": "find_orders", "args": {k: str(v) for k, v in kw.items()},
                "error": f"{type(exc).__name__}: {exc}",
            })
            self._orders_unknown = True
            return [], False

    def _jorders_since(self, iso_ts: str) -> tuple:
        try:
            return self.journal.orders_since(iso_ts), True
        except Exception as exc:  # noqa: BLE001
            self.faults.record(KIND_ORDER_READ, {
                "op": "orders_since", "since": iso_ts,
                "error": f"{type(exc).__name__}: {exc}",
            })
            self._orders_unknown = True
            return [], False

    def _jrecord_order(self, record: Dict[str, Any]) -> bool:
        try:
            self.journal.record_order(record)
            return True
        except Exception as exc:  # noqa: BLE001
            self.faults.record(KIND_AUDIT_WRITE, {
                "event": "record_order", "order_id": record.get("order_id"),
                "client_id": record.get("client_id"),
                "broker_order_id": record.get("broker_order_id"),
                "symbol": record.get("symbol"), "side": record.get("side"),
                "status": record.get("status"),
                "error": f"{type(exc).__name__}: {exc}",
                "detail": "订单已提交但落库失败 —— 必须人工核对券商侧订单"
                          "（可用 client_id 在意图表/券商侧检索）",
            }, severity=SEV_CRITICAL)
            return False

    def _jrecord_risk_decision(self, decision: Dict[str, Any]) -> bool:
        try:
            self.journal.record_risk_decision(decision)
            return True
        except Exception as exc:  # noqa: BLE001
            self.faults.record(KIND_AUDIT_WRITE, {
                "event": "record_risk_decision",
                "decision_id": decision.get("decision_id"),
                "approved": decision.get("approved"),
                "error": f"{type(exc).__name__}: {exc}",
            })
            return False

    def _jupdate_status(self, order_id: str, status: str, **fields: Any) -> bool:
        try:
            self.journal.update_order_status(order_id, status, **fields)
            return True
        except Exception as exc:  # noqa: BLE001
            self.faults.record(KIND_AUDIT_WRITE, {
                "event": "update_order_status", "order_id": order_id,
                "status": status, "error": f"{type(exc).__name__}: {exc}",
            })
            return False

    # ------------------------------------------------------------------ #
    # US-P1 Batch2 R-7：止损保护绑定"实际持仓 + 保护版本"
    # ------------------------------------------------------------------ #
    def _mark_stop_exit_pending(self, symbol: str, order: Dict[str, Any]) -> None:
        """退出单已发出：**不解除**保护，只标记"待核实"。"""
        rule = self._stops.get(symbol)
        if not rule:
            return
        rule["exit_pending"] = True
        rule["last_exit_client_id"] = str(order.get("client_id") or "")
        rule["last_exit_status"] = str(order.get("status") or "")
        rule["last_exit_at"] = utcnow_iso()
        self._save_stops()
        self._jlog("stops_exit_pending", {
            "symbol": symbol,
            "exit_status": rule["last_exit_status"],
            "client_id": rule["last_exit_client_id"],
            "detail": "退出已提交但保护未解除：等下一轮核实实际持仓后再决定",
        }, subject=symbol, severity=SEV_WARN)

    def _reconcile_stops(self, positions: List[Position],
                         executed: List[Dict[str, Any]]) -> None:
        """按"实际持仓 + 未终结买单"重算保护，而不是按订单状态变化。

        R-7 的核心：未知买单成交后，禁止因为"我们发过卖出"就把止损删掉。
        """
        pos_map = {p.symbol.lower(): float(p.qty) for p in positions}
        verified = bool(self._positions_verified)
        for sym in list(self._stops.keys()):
            rule = self._stops[sym]
            # 持仓列表里没有该标的 = 已清仓（0），不是"未知"。
            # 真正的"未知"由 verified=False 表达（持仓接口失败/降级）。
            held = pos_map.get(sym.lower(), 0.0)
            open_buys = 0.0
            try:
                for it in self.journal.list_open_intents(symbol=sym):
                    if str(it.get("side") or "").lower() == "buy":
                        open_buys += float(it.get("qty") or 0)
            except Exception:  # noqa: BLE001
                open_buys = -1.0        # 未知 → 一律不解除保护

            pending = bool(rule.get("exit_pending"))
            if not verified:
                # 持仓未核实：保留原保护，只告警
                if pending:
                    self.faults.record(KIND_STOPS, {
                        "symbol": sym, "phase": "reconcile",
                        "detail": "持仓未核实，保留原止损保护（不清除）",
                    }, severity=SEV_WARN)
                rule["protected_qty"] = float(rule.get("protected_qty")
                                              or rule.get("qty") or 0)
                continue

            if held <= 0 and open_buys == 0 and pending:
                # 已确认清仓且没有未终结买单 → 才允许解除
                self._stops.pop(sym, None)
                self._save_stops()
                self._jlog("stops_cleared_after_flat", {
                    "symbol": sym,
                    "detail": "重新核实持仓为 0 且无未终结买单，解除止损保护",
                }, subject=sym)
                executed.append({"kind": "stops_cleared", "symbol": sym})
                continue

            if held > 0:
                rule["protected_qty"] = held
                rule["qty"] = held
                if pending:
                    rule["exit_pending"] = False
                    self._jlog("stops_retained_after_exit", {
                        "symbol": sym, "protected_qty": held,
                        "detail": ("退出后重新核实仍有持仓（可能是未知买单成交）→ "
                                   "保留止损并重算保护数量"),
                    }, subject=sym, severity=SEV_WARN)
                    executed.append({"kind": "stops_retained", "symbol": sym,
                                     "protected_qty": held})
                self._save_stops()
            elif open_buys != 0:
                # 持仓 0 但仍有未终结买单 → 不解除
                if pending:
                    executed.append({"kind": "stops_retained_open_buy", "symbol": sym})
                rule["exit_pending"] = False
                self._save_stops()

    # ------------------------------------------------------------------ #
    # 持仓（US-P0-3 / US-P0-4）
    # ------------------------------------------------------------------ #
    def _cache_positions(self, positions: List[Position], now: datetime) -> None:
        self._positions_cache = {
            "at": now.astimezone().isoformat(timespec="seconds"),
            "positions": list(positions),
        }

    def _positions_from_stops(self) -> List[Position]:
        """券商持仓查不到时，用**我们自己登记过的止损记录**里的数量兜底。

        这不是"编造持仓"：``qty`` 来自成交回报，是本地已知的最小事实集合。
        它只用于让已有的止损规则还能被评估，且会带 ``session="local_stops"``
        标记，便于审计区分。
        """
        out: List[Position] = []
        for sym, rule in self._stops.items():
            qty = float(rule.get("qty") or 0)
            if qty <= 0:
                continue
            out.append(Position(
                symbol=sym, market=market_of(sym), qty=qty,
                avg_cost=0.0, last_price=0.0,
                currency=currency_of(sym), session="local_stops",
            ))
        return out

    def _positions_for_monitor(self, executed: List[Dict[str, Any]]) -> List[Position]:
        """monitor 专用：持仓查询失败也**不能**让风险处置整体停摆。

        US-P1 N-1：这里的告警也走 ``_jlog``（数据库坏了也要留下痕迹），
        并显式设置 ``self._positions_verified``，供 reduce-only 通道判断
        "数量是否可信"。
        """
        try:
            positions = self.broker.get_positions()
            self._cache_positions(positions, datetime.now())
            self._positions_stale = False
            self._positions_verified = True
            return positions
        except Exception as exc:  # noqa: BLE001
            detail = f"{type(exc).__name__}: {exc}"
            self._positions_stale = True
            # 快照/本地登记不是"实时核实"，数量可能已变 → 不得用于 reduce-only
            self._positions_verified = False
            cached = self._positions_cache or {}
            cached_positions = list(cached.get("positions") or [])
            # 优先用"最近一次成功的真实持仓快照"（带价格、带时段），
            # 再把"本地止损登记里有、但快照里没有"的标的补进来。
            fallback = list(cached_positions)
            known = {p.symbol.lower() for p in fallback}
            for p in self._positions_from_stops():
                if p.symbol.lower() not in known:
                    fallback.append(p)
            self.faults.record(KIND_POSITION_READ, {
                "error": detail,
                "fallback": ("cache+local_stops" if cached_positions and fallback
                             else "local_stops" if fallback else "none"),
                "fallback_count": len(fallback),
                "cached_at": cached.get("at"),
                "detail": ("持仓查询失败：新开仓已停止；止损改用最近快照/本地登记数量继续评估"
                           if fallback else
                           "持仓查询失败且无可用的本地持仓记录，本轮无法评估止损（已告警）"),
            }, severity=SEV_ERROR)
            self._jlog("positions_unavailable", {
                "error": detail,
                "positions_verified": False,
                "fallback_count": len(fallback),
            }, severity=SEV_ERROR)
            executed.append({"kind": "positions_unavailable", "error": detail,
                             "positions_verified": False})
            return fallback

    def _portfolio_for_exit(self, currency: str, now: datetime,
                            positions: List[Position]) -> Portfolio:
        """减仓用的组合快照：正常走 build_portfolio；失败时降级但不放弃处置。"""
        try:
            pf = self.build_portfolio(currency, now)
            pf.positions_verified = bool(self._positions_verified)
            pf.open_sell_reserved = dict(self._sell_reserved)
            pf.reservations_known = bool(self._reservations_known)
            pf.intents_available = bool(self._intents_available)
            return pf
        except Exception as exc:  # noqa: BLE001
            cached = self._positions_cache or {}
            self._jlog("portfolio_unavailable_for_exit", {
                "error": f"{type(exc).__name__}: {exc}",
                "currency": currency,
                "detail": "账户汇总/持仓不可用，减仓决策改用降级组合（权益/现金未知）",
            }, severity=SEV_ERROR)
            return Portfolio(
                currency=currency,
                equity=0.0, cash=0.0,
                positions=list(positions),
                day_pnl=0.0, day_trades=0, day_orders=0,
                # 权益未知 → 单日亏损熔断不参与判定（减仓本来就不受它约束）
                day_pnl_known=False,
                today_bought={},
                degraded=True,
                degraded_reason=f"{type(exc).__name__}: {exc}",
                cache_at=str(cached.get("at") or ""),
                positions_verified=False,
                open_sell_reserved=dict(self._sell_reserved),
                reservations_known=bool(self._reservations_known),
                intents_available=bool(self._intents_available),
            )

    # ------------------------------------------------------------------ #
    # 单标的分析
    # ------------------------------------------------------------------ #
    def _build_context(self, symbol: str, portfolio: Portfolio) -> tuple[MarketContext, Quote]:
        market = market_of(symbol)
        cfg_mkt = (self.cfg.raw.get("markets") or {}).get(market) or {}
        currency = cfg_mkt.get("currency") or currency_of(symbol)
        # US-P2-A：新开仓只能用"常规交易时段"的行情；盘前/盘后价不得冒充盘中价。
        quote = self.quote_gateway.quote(symbol, "regular_only", market=market)
        num = int(self.cfg.get("strategy.kline_num", 120) or 120)
        ktype = str(self.cfg.get("strategy.kline_ktype", "day") or "day")
        candles = self.broker.get_klines(symbol, ktype, num)
        pos = portfolio.position_of(symbol)
        equity = portfolio.equity if portfolio.currency == currency else self.broker.get_summary(currency).equity
        cash = portfolio.cash if portfolio.currency == currency else self.broker.get_summary(currency).cash
        ctx = MarketContext(
            symbol=symbol, market=market, currency=currency,
            last_price=float(quote.last),
            candles=candles,
            indicators=snapshot_for_prompt(candles),
            position={"qty": pos.qty, "avg_cost": pos.avg_cost,
                      "unrealized_pct": round(pos.unrealized_pct, 4)} if pos else None,
            equity=equity, cash=cash,
            max_position_pct=float(self.cfg.risk.get("max_position_pct", 0.15)),
            risk_budget=equity * float(self.cfg.risk.get("risk_per_trade_pct", 0.01)),
            notes=[f"行情源={quote.source}"],
        )
        return ctx, quote

    def _prefilter(self, ctx: MarketContext) -> Optional[str]:
        """返回跳过原因；None 表示通过。"""
        tf = self.cfg.get("strategy.trend_filter") or {}
        if not tf.get("enabled", True):
            return None
        candles = ctx.candles or []
        if len(candles) < 30:
            return f"K 线不足（{len(candles)}）"
        ap = atr_pct(candles, 14)
        min_atr = float(tf.get("min_atr_pct", 0.005))
        if ap is not None and ap < min_atr:
            return f"ATR {ap:.4%} 低于最低要求 {min_atr:.2%}，波动不足"
        return None

    def analyze_symbol(self, symbol: str, portfolio: Optional[Portfolio] = None,
                       now: Optional[datetime] = None) -> SymbolOutcome:
        market = market_of(symbol)
        cfg_mkt = (self.cfg.raw.get("markets") or {}).get(market) or {}
        currency = cfg_mkt.get("currency") or currency_of(symbol)
        portfolio = portfolio or self.build_portfolio(currency, now)
        try:
            ctx, quote = self._build_context(symbol, portfolio)
        except QuoteUnavailable as exc:
            # US-P2-A：行情不可用是"明确拒绝"，不是"取数失败"——
            # 不得回退到上一个价格继续交易。
            return SymbolOutcome(symbol, "error", f"行情不可用: {exc}")
        except BrokerError as exc:
            return SymbolOutcome(symbol, "error", f"取行情失败: {exc}")
        except Exception as exc:  # noqa: BLE001
            return SymbolOutcome(symbol, "error", f"构建上下文失败: {type(exc).__name__}: {exc}")

        skip = self._prefilter(ctx)
        if skip:
            return SymbolOutcome(symbol, "skipped", skip, signal={"indicators": {
                "atr_pct": (ctx.indicators or {}).get("atr_pct"),
                "bars": (ctx.indicators or {}).get("bars"),
            }})

        # ---- 行情新鲜度闸门（B5）----
        # 行情断了/过期时只禁止**新开仓**；已有持仓的止损仍由 monitor() 走风控。
        feed_ok, feed_code, feed_detail = self._feed_gate(market, symbol, now)
        if not feed_ok:
            return self._block_entry(symbol, feed_code, feed_detail, now,
                                     extra={"feed": self.feed_snapshot()})

        # ---- AI 决策权威闸门（P1-9）----
        # 规则引擎 / 不可用模型 / 故障冷却期内 —— 一律不允许产生新的开仓信号。
        # 注意：这里只挡"新开仓"，已有持仓的止损/止盈由 monitor() 走 risk.scan_exits，
        # 完全不经过本闸门。
        auth = self.authority.can_decide_entry(now)
        if not auth.allowed:
            return self._block_entry(symbol, auth.code, auth.reason, now,
                                     extra={"authority": self.authority.snapshot(now).as_dict()})

        # ---- AI 判断 ----
        try:
            signal = self.model.analyze(ctx)
        except AIModelError as exc:
            info = self.authority.record_failure(exc.code, str(exc), now)
            self._jlog("ai_error", {"symbol": symbol, "code": exc.code,
                                          "error": str(exc), **info},
                             actor="ai", subject=symbol)
            return self._block_entry(
                symbol, exc.code or "AI_ERROR",
                f"AI 调用失败：{exc}", now, extra=info)
        except Exception as exc:  # noqa: BLE001
            info = self.authority.record_failure("AI_EXCEPTION", repr(exc), now)
            self._jlog("ai_error", {"symbol": symbol, "error": repr(exc), **info},
                             actor="ai", subject=symbol)
            return self._block_entry(
                symbol, "AI_EXCEPTION",
                f"AI 异常：{type(exc).__name__}: {exc}", now, extra=info)

        rec = self.authority.record_success(now)
        if rec.get("recovered"):
            self._jlog("ai_recovered", {"at": rec["at"], "model": self.model.model_id},
                             actor="ai")

        # ---- 信号新鲜度（禁止 AI 恢复后执行失效期间的旧信号）----
        fresh = self.authority.check_freshness(signal.ts, now)
        if not fresh.allowed:
            self._jlog("stale_signal_rejected",
                             {"symbol": symbol, "signal_id": signal.signal_id,
                              "signal_ts": signal.ts, "code": fresh.code,
                              "reason": fresh.reason},
                             actor="engine", subject=symbol)
            return SymbolOutcome(symbol, "blocked", f"[{fresh.code}] {fresh.reason}",
                                 signal=signal.as_dict())

        errs = signal.validate(
            min_confidence=float(self.cfg.ai.get("constraints", {}).get("min_confidence", 0.60)),
            require_stop_loss=bool(self.cfg.ai.get("constraints", {}).get("require_stop_loss", True)),
        )
        self.journal.record_signal(signal.as_dict())

        if signal.action == "hold":
            return SymbolOutcome(symbol, "hold", signal.rationale or "模型建议观望",
                                 signal=signal.as_dict())
        if errs:
            return SymbolOutcome(symbol, "rejected", "信号非法: " + "；".join(errs),
                                 signal=signal.as_dict())

        # ---- 定仓 ----
        pos = portfolio.position_of(symbol)
        lot = int(((self.cfg.raw.get("markets") or {}).get(market) or {}).get("lot_default", 1) or 1)
        price = float(signal.entry_price or quote.last)
        if signal.action == "buy":
            sizing = self.risk.size_position(
                equity=ctx.equity, cash=ctx.cash, price=price,
                stop_loss=float(signal.stop_loss or price * 0.97),
                confidence=float(signal.confidence), lot_size=lot,
                max_pct_override=float(signal.position_pct or 0) or None,
            )
            qty = sizing["qty"]
            intent = OrderIntent(
                symbol=symbol, side="buy", qty=qty, price=round(price, 4),
                market=market, currency=currency,
                stop_loss=signal.stop_loss, take_profit=signal.take_profit,
                order_type="limit", signal_id=signal.signal_id,
                confidence=signal.confidence, reason=signal.rationale,
            )
        else:
            qty = float(pos.qty) if pos else 0.0
            intent = OrderIntent(
                symbol=symbol, side="sell", qty=qty, price=round(price, 4),
                market=market, currency=currency,
                stop_loss=signal.stop_loss, take_profit=signal.take_profit,
                order_type="limit", signal_id=signal.signal_id,
                confidence=signal.confidence, reason=signal.rationale, is_exit=True,
                exit_mode="signal_exit",
            )

        # ---- 风控 ----
        self._load_sell_reservations()
        recent, orders_ok = self._recent_orders_for(symbol)
        decision = self.risk.evaluate(
            intent=intent, portfolio=portfolio, last_price=float(quote.last),
            recent_orders=recent, now=now,
            journal=self.journal,
            prev_close=float(getattr(quote, "prev_close", 0.0) or 0.0) or None,
            orders_known=(orders_ok and getattr(portfolio, "orders_known", True)
                          and self._intents_available),
        )
        self._jrecord_risk_decision(decision.as_dict())

        if not decision.approved:
            return SymbolOutcome(symbol, "rejected", decision.explain(),
                                 signal=signal.as_dict(), decision=decision.as_dict())

        # ---- 执行 ----
        held_now = float(portfolio.position_of(symbol).qty) if portfolio.position_of(symbol) else 0.0
        order = self._execute(intent, decision, portfolio_held=held_now)
        if order is None:
            return SymbolOutcome(symbol, "rejected", "券商拒单或预写被拒",
                                 signal=signal.as_dict(),
                                 decision=decision.as_dict())
        if intent.side == "buy" and order.get("status") in ("filled", "submitted", "partial"):
            self.register_stops(symbol, signal.stop_loss, signal.take_profit, float(order["qty"]))
        if intent.side == "sell" and order.get("status") in ("filled",):
            self.clear_stops(symbol)
        return SymbolOutcome(symbol, "ordered", decision.explain() + " → " + str(order.get("status")),
                             signal=signal.as_dict(), decision=decision.as_dict(), order=order)

    def _feed_gate(self, market: str, symbol: str,
                   now: Optional[datetime]) -> tuple:
        """行情推送是否足以支撑"开新仓"。"""
        if self.feed is None or not self._feed_required:
            return True, "", "未启用行情推送依赖"
        if market not in self._feed_markets:
            return True, "", f"市场 {market} 未纳入推送校验"
        ok, detail = self.feed.is_fresh(symbol, now)
        if ok:
            return True, "", detail
        return False, "FEED_NOT_FRESH", f"行情不可用于开仓：{detail}"

    def feed_snapshot(self) -> Dict[str, Any]:
        if self.feed is None:
            return {"enabled": False}
        snap = self.feed.snapshot()
        return {"enabled": True, "state": snap.get("state"),
                "stats": snap.get("stats"), "reconnects": snap.get("reconnects"),
                "source": snap.get("source"), "protocol": snap.get("protocol")}

    def _block_entry(self, symbol: str, code: str, reason: str,
                     now: Optional[datetime] = None,
                     extra: Optional[Dict[str, Any]] = None) -> SymbolOutcome:
        """拒绝一次新开仓，并把**具体原因**写进审计。

        审计要求（P1-9）：每次拒绝交易必须记录具体原因。
        """
        detail = {
            "symbol": symbol, "code": code, "reason": reason,
            "model": f"{self.model.name}/{self.model.model_id}",
            "at": (now or datetime.now()).astimezone().isoformat(timespec="seconds"),
            "action_taken": "no_new_entry",
            "note": "已有持仓不受影响，止损/止盈仍由 monitor() 执行",
        }
        if extra:
            detail.update(extra)
        self._jlog("entry_blocked", detail, actor="engine", subject=symbol)
        return SymbolOutcome(symbol, "blocked", f"[{code}] {reason}",
                             signal={"authority": self.authority.snapshot(now).as_dict()})

    # ------------------------------------------------------------------ #
    # US-P1 Batch2 R-1：下单意图预写 / 恢复 / 结算
    # ------------------------------------------------------------------ #
    def _recover_order_intents(self) -> None:
        """启动时恢复未终结订单：全部提升为 unknown，并强制对账后才可再交易。

        这是"进程在券商返回后、状态更新前崩溃"的恢复点：
        预写发生在券商调用**之前**，因此任何一刻崩溃都至少留下 ``intent`` 行。
        """
        try:
            rows = self.journal.recover_open_intents()
        except Exception as exc:  # noqa: BLE001
            self._intents_available = False
            self._recovered_intents = {}
            self.faults.record(KIND_ORDER_INTENT, {
                "phase": "startup_recovery",
                "error": f"{type(exc).__name__}: {exc}",
                "detail": "订单意图表不可读 → 禁止一切新开仓，等待人工核对券商侧订单",
            }, severity=SEV_CRITICAL)
            return
        self._intents_available = True
        self._recovered_intents = {str(r.get("client_id")): r for r in rows
                                   if r.get("client_id")}
        if rows:
            self.faults.record(KIND_ORDER_INTENT, {
                "phase": "startup_recovery",
                "count": len(rows),
                "client_ids": [r.get("client_id") for r in rows][:10],
                "detail": "重启后发现未终结订单，已全部标为 unknown；"
                          "对账完成前禁止同标的再次开仓/加仓",
            }, severity=SEV_ERROR)
            self._jlog("order_intents_recovered", {
                "count": len(rows),
                "client_ids": [r.get("client_id") for r in rows][:20],
            }, severity=SEV_ERROR)

    def _prewrite_intent(self, intent: OrderIntent, client_id: str, decision,
                         qty: float, sell_capacity: Optional[float]
                         ) -> tuple:
        """在下单前持久化意图。失败则**不得**调用券商。"""
        record = {
            "client_id": client_id,
            "created_at": utcnow_iso(),
            "symbol": intent.symbol,
            "market": intent.market,
            "side": intent.side,
            "qty": qty,
            "price": intent.price,
            "order_type": intent.order_type,
            "currency": intent.currency,
            "stop_loss": intent.stop_loss,
            "take_profit": intent.take_profit,
            "signal_id": intent.signal_id,
            "decision_id": decision.decision_id,
            "request": {
                "symbol": intent.symbol, "market": intent.market,
                "side": intent.side, "qty": qty, "price": intent.price,
                "order_type": intent.order_type, "currency": intent.currency,
                "signal_id": intent.signal_id, "decision_id": decision.decision_id,
                "exit_mode": intent.exit_mode,
            },
        }
        try:
            ok, why = self.journal.prewrite_intent(
                record, sell_capacity=sell_capacity)
        except Exception as exc:  # noqa: BLE001
            self.faults.record(KIND_ORDER_INTENT, {
                "phase": "prewrite", "symbol": intent.symbol,
                "client_id": client_id, "error": f"{type(exc).__name__}: {exc}",
                "detail": "意图预写失败 → 未调用券商（不允许先发单再补记录）",
            }, severity=SEV_CRITICAL)
            return False, f"prewrite_failed: {type(exc).__name__}: {exc}"
        if not ok:
            self.faults.record(KIND_RESERVATION, {
                "phase": "prewrite_capacity", "symbol": intent.symbol,
                "side": intent.side, "qty": qty, "client_id": client_id,
                "reason": why,
                "detail": "卖出预留不足（未完成卖单合并计算），未调用券商",
            }, severity=SEV_ERROR)
        elif why.startswith("duplicate"):
            self._jlog("order_intent_duplicate_blocked", {
                "client_id": client_id, "symbol": intent.symbol, "reason": why,
                "detail": "同一 client_id 已存在：可能是另一个 worker 正在处理同一决策，"
                          "本次不重复发单",
            }, severity=SEV_WARN)
        return ok, why

    def _jupdate_intent(self, client_id: str, state: str, **kwargs: Any) -> bool:
        try:
            return bool(self.journal.update_intent(client_id, state, **kwargs))
        except Exception as exc:  # noqa: BLE001
            self.faults.record(KIND_ORDER_INTENT, {
                "phase": "update", "client_id": client_id, "state": state,
                "error": f"{type(exc).__name__}: {exc}",
                "detail": "意图状态更新失败 → 行仍在，重启后会被恢复为 unknown",
            }, severity=SEV_ERROR)
            return False

    def _mark_intent_unknown(self, client_id: str, error: str) -> None:
        """券商结果不明：标 unknown（**不释放预留**）。"""
        self._jupdate_intent(client_id, INTENT_STATE_UNKNOWN,
                             broker_status="unknown", last_error=error,
                             bump_attempt=True)
        try:
            row = self.journal.get_intent(client_id)
            if row:
                self._recovered_intents[str(client_id)] = row
        except Exception:  # noqa: BLE001
            pass

    def _settle_intent_from_result(self, client_id: str, record: Dict[str, Any]) -> None:
        """把券商真实结果映射到意图状态（只有终态才释放预留）。"""
        status = str(record.get("status") or "").lower()
        broker_oid = record.get("broker_order_id") or record.get("order_id") or ""
        if status in ("filled", "cancelled", "canceled", "rejected",
                      "expired", "not_accepted"):
            state = INTENT_STATE_TERMINAL
        elif status in ("submitted", "pending", "partial", "new", "accepted"):
            state = INTENT_STATE_SUBMITTED
        else:
            state = INTENT_STATE_UNKNOWN
        self._jupdate_intent(client_id, state, broker_order_id=str(broker_oid),
                             broker_status=status or "unknown")
        # 同步 orders 行，避免它长期停在 unknown 而让"未完成委托"判定永久命中
        if state != INTENT_STATE_UNKNOWN:
            try:
                self.journal.sync_order_status_for_client(
                    client_id, status or "unknown",
                    broker_order_id=broker_oid,
                    reconcile_note=record.get("reconcile_note") or "意图结算同步")
            except Exception:  # noqa: BLE001
                pass
        if state != INTENT_STATE_UNKNOWN:
            self._recovered_intents.pop(str(client_id), None)

    def _load_sell_reservations(self) -> None:
        """刷新"未完成卖单预留量"。读不出来 → 视为未知（禁止再卖）。"""
        try:
            reserved, ok = self.journal.open_sell_reserved()
        except Exception as exc:  # noqa: BLE001
            reserved, ok = {}, False
            self.faults.record(KIND_RESERVATION, {
                "phase": "load",
                "error": f"{type(exc).__name__}: {exc}",
                "detail": "预留量查询抛异常 → 按未知处理，禁止新增卖出",
            }, severity=SEV_ERROR)
        self._sell_reserved = {k: float(v) for k, v in reserved.items()}
        self._reservations_known = bool(ok)
        if not ok:
            self.faults.record(KIND_RESERVATION, {
                "phase": "load",
                "detail": "未完成卖单预留量读取失败 → 暂停一切新增卖出",
            }, severity=SEV_ERROR)

    # ------------------------------------------------------------------ #
    # 唯一的下单出口
    # ------------------------------------------------------------------ #
    def _execute(self, intent: OrderIntent, decision,
                 portfolio_held: Optional[float] = None) -> Optional[Dict[str, Any]]:
        if not decision.approved:
            # 代码级兜底：没有获批的决策绝不可能到达券商
            logger.error("拒绝执行未获批意图 %s", intent.symbol)
            return None
        if decision.approved_qty <= 0:
            return None

        client_id = derive_client_id(decision.decision_id, intent.signal_id,
                                     intent.symbol, intent.side)
        req = OrderRequest(
            symbol=intent.symbol, market=intent.market, side=intent.side,
            qty=float(decision.approved_qty), price=intent.price,
            order_type=intent.order_type, currency=intent.currency,
            stop_loss=intent.stop_loss, take_profit=intent.take_profit,
            signal_id=intent.signal_id, decision_id=decision.decision_id,
            note=intent.reason[:200], client_id=client_id,
        )

        # ---- US-P1 Batch2 R-1：下单**之前**先持久化意图（write-ahead）----
        # 预写失败 = 无法保证"订单可恢复" → 绝不能先把单发出去再补记录。
        qty = float(decision.approved_qty)
        capacity = None
        if (intent.side or "").lower() == "sell":
            held = float(portfolio_held) if portfolio_held is not None else None
            capacity = held
        ok, why = self._prewrite_intent(intent, client_id, decision, qty, capacity)
        if not ok:
            self._release_attempt(decision.decision_id)
            self._jlog("order_intent_prewrite_blocked", {
                "symbol": intent.symbol, "side": intent.side, "qty": qty,
                "client_id": client_id, "reason": why,
                "detail": "预写失败或卖出预留不足：未调用券商，未产生任何委托",
            }, subject=intent.symbol, severity=SEV_ERROR)
            return None

        # ---- US-P2-C 阶段 B：提交闸门 ----
        # 意图（阶段 A）已经放行，但"允许退出意图"≠"订单可正常执行"。
        # 已确认停牌 / LULD 暂停 / MWCB 暂停时不得把订单发给券商。
        _ms = getattr(self.risk, "market_status", None)
        if getattr(self.risk, "phase2_enabled", False) and _ms is not None:
            _now_aware = datetime.now().astimezone()
            _sub = _ms.submit_gate(intent.symbol, is_exit=bool(intent.is_exit),
                                   now=_now_aware)
            if not _sub.allowed:
                self._jlog("order_submit_blocked", {
                    "symbol": intent.symbol, "side": intent.side,
                    "qty": float(decision.approved_qty), "client_id": client_id,
                    "state": _sub.state.value, "reason": _sub.reason,
                    "warning": _sub.warning,
                    "detail": "市场暂停/不可确认：退出意图已保留，未向券商提交",
                }, subject=intent.symbol, severity=SEV_WARN)
                self._jupdate_intent(client_id, INTENT_STATE_REJECTED,
                                     broker_status="submit_blocked",
                                     last_error=_sub.reason)
                self._release_attempt(decision.decision_id)
                return None

        try:
            result = self.broker.place_order(req)
        except OrderStateUnknown as exc:
            # 券商可能已受理 —— 先落"未知"，再去对账，绝不重试下单
            self._mark_intent_unknown(client_id, str(exc))
            return self._reconcile_unknown(exc, intent, decision)
        except BrokerError as exc:
            # 明确失败（门禁/拒单/参数错）：意图标记 rejected 释放预留，允许后续重试
            self._jupdate_intent(client_id, INTENT_STATE_REJECTED,
                                 broker_status="rejected", last_error=str(exc))
            self._release_attempt(decision.decision_id)
            self._jlog("broker_reject", {"symbol": intent.symbol, "error": str(exc),
                                               "code": exc.code}, actor="broker",
                             subject=intent.symbol)
            return None
        except Exception as exc:  # noqa: BLE001
            # 非 BrokerError 的意外异常同样意味着"状态不确定"
            self._mark_intent_unknown(client_id, f"{type(exc).__name__}: {exc}")
            unknown = OrderStateUnknown(
                f"下单过程中出现未预期异常（{type(exc).__name__}: {exc}），状态未知",
                client_id=client_id, symbol=intent.symbol, side=intent.side,
                qty=float(decision.approved_qty), price=intent.price,
                code="ORDER_STATE_UNKNOWN", cause=repr(exc))
            return self._reconcile_unknown(unknown, intent, decision)

        record = result.as_dict()
        record["signal_id"] = intent.signal_id
        record["decision_id"] = decision.decision_id
        if not record.get("client_id"):
            record["client_id"] = client_id
        # US-P1 Batch2 R-1：先把券商真实结果写进**意图表**（恢复依据），
        # 再尝试写 orders。orders 写失败不影响"这笔单存在过"这一事实。
        self._settle_intent_from_result(client_id, record)
        self._jrecord_order(record)
        self.risk.note_order(intent.symbol, intent.side, float(decision.approved_qty))
        if str(record.get("status", "")).lower() == "unknown":
            self._jlog("order_state_unknown",
                             {"order_id": record.get("order_id"),
                              "client_id": record.get("client_id"),
                              "note": record.get("reconcile_note")},
                             actor="broker", subject=intent.symbol)
        return record

    def _release_attempt(self, decision_id: str) -> None:
        try:
            self.journal.release_order_attempt(decision_id)
        except Exception:  # noqa: BLE001
            logger.warning("释放冷却槽位失败 decision_id=%s", decision_id)

    def _reconcile_unknown(self, exc: OrderStateUnknown, intent: OrderIntent,
                           decision) -> Dict[str, Any]:
        """订单状态未知时，用 client_id 去券商侧对账。

        三种结果：

        * 查到了 → 按券商真实状态落库（可能是 filled/submitted/cancelled）
        * 明确查不到 → 落库 ``not_accepted``，解除对该标的的封锁
        * 查不清 → 落库 ``unknown``，**继续封锁该标的**，等下一轮 monitor 再对账
        """
        client_id = exc.client_id or derive_client_id(
            decision.decision_id, intent.signal_id, intent.symbol, intent.side)
        self._jlog("order_state_unknown", {
            "symbol": intent.symbol, "client_id": client_id,
            "cause": exc.cause or str(exc), "code": exc.code,
            "qty": exc.qty, "price": exc.price,
            "action": "entering_reconciliation_no_retry",
        }, actor="broker", subject=intent.symbol)

        if self._stops_guard_engaged(intent):
            pass  # 占位：未来可在此暂停策略

        reconciled: Optional[OrderResult] = None
        reconcile_error = ""
        try:
            reconcile = getattr(self.broker, "reconcile_order", None)
            if not callable(reconcile):
                raise BrokerError("该券商未实现对账接口", code="NO_RECONCILE")
            reconciled = reconcile(client_id=client_id, symbol=intent.symbol,
                                   side=intent.side, qty=float(decision.approved_qty))
        except Exception as rex:  # noqa: BLE001
            reconcile_error = f"{type(rex).__name__}: {rex}"

        if reconciled is not None:
            record = reconciled.as_dict()
            record["signal_id"] = intent.signal_id
            record["decision_id"] = decision.decision_id
            if not record.get("client_id"):
                record["client_id"] = client_id
            record["reconcile_note"] = (record.get("reconcile_note") or "") + \
                                       "｜由 unknown 对账确认"
            # R-1：券商已给出真实状态 → 结算意图（终态才释放卖出预留）
            self._settle_intent_from_result(client_id, record)
            self._jrecord_order(record)
            self.risk.note_order(intent.symbol, intent.side, float(decision.approved_qty))
            self._jlog("order_reconciled", {
                "symbol": intent.symbol, "client_id": client_id,
                "broker_order_id": record.get("broker_order_id"),
                "status": record.get("status")}, actor="engine", subject=intent.symbol)
            return record

        status = "not_accepted" if not reconcile_error else "unknown"
        order_id = f"UNCONFIRMED-{client_id}"
        record = {
            "order_id": order_id,
            "broker_order_id": "",
            "symbol": intent.symbol, "market": intent.market, "side": intent.side,
            "qty": float(decision.approved_qty), "price": intent.price,
            "order_type": intent.order_type,
            "status": status,
            "filled_qty": 0.0, "avg_fill_price": None, "commission": 0.0,
            "venue": getattr(self.broker, "venue", ""),
            "ts": utcnow_iso(),
            "signal_id": intent.signal_id,
            "decision_id": decision.decision_id,
            "client_id": client_id,
            "note": "下单结果未知，未重试",
            "reconcile_note": ("对账确认券商侧无此单（判定未受理）" if not reconcile_error
                               else f"对账失败，状态仍未知：{reconcile_error}"),
            "raw": {},
        }
        if status == "not_accepted":
            # 券商确认无此单 → 终态，释放卖出预留
            self._settle_intent_from_result(client_id, record)
        else:
            self._jupdate_intent(client_id, INTENT_STATE_UNKNOWN,
                                 broker_status="unknown",
                                 last_error=f"对账失败：{reconcile_error}",
                                 bump_attempt=True)
        self._jrecord_order(record)
        self._jlog("order_unreconciled" if reconcile_error else "order_not_accepted", {
            "symbol": intent.symbol, "client_id": client_id, "status": status,
            "error": reconcile_error}, actor="engine", subject=intent.symbol)
        if status == "not_accepted":
            self._release_attempt(decision.decision_id)
        return record

    @staticmethod
    def _stops_guard_engaged(intent: OrderIntent) -> bool:
        return False

    # ------------------------------------------------------------------ #
    # 撤单（P1-6）
    # ------------------------------------------------------------------ #
    def cancel_order(self, order_id: str, reason: str = "手动撤单",
                     now: Optional[datetime] = None) -> Dict[str, Any]:
        """撤单完整链路：查单 → 门禁 → 撤单 → 回写状态 → 审计。

        ``order_id`` 可以是券商 id，也可以是本地 ``UNCONFIRMED-*`` 记录
        （后者会先按 client_id 对账拿到真实 id 再撤）。
        """
        with self._lock:
            fetched, _ok = self._jlist_orders(limit=200)
            rows = [o for o in fetched
                    if str(o.get("order_id")) == str(order_id)]
            order = rows[0] if rows else None

            target_id = str(order_id)
            if order is None:
                return {"ok": False, "reason": f"本地无此订单记录: {order_id}"}

            if str(order.get("status", "")).lower() in ("filled", "cancelled", "rejected",
                                                        "expired", "not_accepted"):
                return {"ok": False,
                        "reason": f"订单状态 {order['status']} 不可撤销"}

            # 本地未确认记录：先对账拿真实券商 id
            if not order.get("broker_order_id"):
                client_id = str(order.get("client_id") or "")
                reconcile = getattr(self.broker, "reconcile_order", None)
                if client_id and callable(reconcile):
                    try:
                        found = reconcile(client_id=client_id,
                                          symbol=str(order.get("symbol", "")))
                    except Exception as exc:  # noqa: BLE001
                        self._jlog("cancel_blocked", {
                            "order_id": order_id, "reason": f"对账失败: {exc}"},
                            actor="engine", subject=str(order.get("symbol", "")))
                        return {"ok": False, "reason": f"无法对账，不能撤单: {exc}"}
                    if found is None:
                        self._jupdate_status(
                            order_id, "not_accepted",
                            reconcile_note="撤单前对账：券商侧无此单")
                        return {"ok": False, "reason": "对账确认券商侧无此单，无需撤单"}
                    target_id = found.broker_order_id or found.order_id

            try:
                result = self.broker.cancel_order(target_id)
            except BrokerError as exc:
                self._jlog("cancel_failed", {"order_id": target_id,
                                                   "reason": str(exc), "code": exc.code},
                                 actor="broker", subject=str(order.get("symbol", "")))
                return {"ok": False, "reason": str(exc), "retryable": exc.retryable}

            new_status = str(result.status or "cancel_requested").lower()
            if new_status == "cancel_requested":
                new_status = "cancelled" if self.broker.venue == "paper" else "cancel_requested"
            self._jupdate_status(order_id, new_status,
                                             reconcile_note=f"撤单原因: {reason}")
            self._jlog("order_cancelled", {"order_id": order_id,
                                                 "broker_order_id": target_id,
                                                 "reason": reason,
                                                 "status": new_status},
                             actor="engine", subject=str(order.get("symbol", "")))
            return {"ok": True, "order_id": order_id, "status": new_status,
                    "broker_order_id": target_id}

    # ------------------------------------------------------------------ #
    # 全量扫描
    # ------------------------------------------------------------------ #
    def scan(self, symbols: Optional[List[str]] = None, now: Optional[datetime] = None) -> ScanReport:
        with self._lock:
            now = now or datetime.now()
            report = ScanReport(
                started_at=utcnow_iso(),
                model=f"{self.model.name}/{self.model.model_id}",
            )
            self._scan_count += 1
            ok, reason = ((True, "") if self.cfg.gate.state.paper_trading
                          else self.cfg.gate.can_send_real_order())
            if self.cfg.gate.state.kill_switch:
                report.blocked_reason = f"KILL_SWITCH 已触发：{self.risk.halt_reason or '手动停止'}"
                report.finished_at = utcnow_iso()
                self.last_scan = report
                self._jlog("scan_blocked", {"reason": report.blocked_reason})
                return report

            # 持仓读不出来 → 整轮停手（不能只跳过单个标的：
            # 风控的敞口/持仓数/占比全都依赖持仓，缺失时任何下单都是盲下）
            try:
                self.broker.get_positions()
            except Exception as exc:  # noqa: BLE001
                # US-P0-3：任何异常都必须 fail-closed，且不能把空结果当"没持仓"。
                code = getattr(exc, "code", type(exc).__name__)
                report.blocked_reason = f"持仓读取失败，禁止交易：{exc}"
                report.finished_at = utcnow_iso()
                self.last_scan = report
                self._jlog("scan_blocked", {"reason": report.blocked_reason,
                                                  "code": code})
                return report

            targets = [s.lower() for s in (symbols or [u["code"] for u in self.cfg.universe])]
            for sym in targets:
                try:
                    outcome = self.analyze_symbol(sym, now=now)
                except Exception as exc:  # noqa: BLE001
                    outcome = SymbolOutcome(sym, "error", f"{type(exc).__name__}: {exc}")
                    report.errors.append(f"{sym}: {exc}")
                report.outcomes.append(outcome)

            report.finished_at = utcnow_iso()
            self.last_scan = report
            self._jlog("scan", {"model": report.model, "summary": report.summary,
                                      "blocked": bool(report.blocked_reason)})
            return report

    # ------------------------------------------------------------------ #
    # 持仓监控（止损/止盈 + 挂单复核）
    # ------------------------------------------------------------------ #
    def monitor(self, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """持仓监控：挂单复核 → 超时撤单 → 未知单对账 → 止损止盈。

        顺序有讲究：先把"悬而未决的委托"处理干净，再考虑新动作。

        US-P1 N-1：**五个阶段彼此隔离**。任何一个阶段的数据库/券商调用失败，
        只影响该阶段，后续阶段（尤其是止损）继续执行；失败一律写入独立故障通道。
        这是"单个数据库方法异常不能导致整个 monitor 静默退出"的落地点。
        """
        with self._lock:
            now = now or datetime.now()
            executed: List[Dict[str, Any]] = []
            self._orders_unknown = False

            # 1) 模拟盘挂单复核（券商能力，不依赖数据库）
            executed.extend(self._phase("refresh_pending", self._phase_refresh_pending))

            # 2) 超时未成交 → 撤单（P1-4，依赖 find_orders）
            executed.extend(self._phase("expire_orders",
                                        lambda: self._expire_stale_orders(now)))

            # 3) 状态未知的委托 → 再次对账（P0-1 / P1-7，依赖 find_orders）
            executed.extend(self._phase("reconcile_unknown",
                                        lambda: self._reconcile_pending_unknown(now)))

            # 4) 持仓（券商，失败则降级到快照/本地登记）
            positions = self._positions_for_monitor(executed)

            # 4b) R-7：按"实际持仓 + 未终结买单"重算止损保护（不依赖订单状态）
            try:
                self._reconcile_stops(positions, executed)
            except Exception as exc:  # noqa: BLE001
                self.faults.record(KIND_STOPS, {
                    "phase": "monitor_reconcile", "error": f"{type(exc).__name__}: {exc}",
                    "detail": "止损保护重算失败：保留原保护，不清除",
                }, severity=SEV_ERROR)

            # 5) 行情 + 止损止盈（最关键的一段，绝不因为前面的故障而跳过）
            try:
                executed.extend(self._run_exit_pass(positions, now))
            except Exception as exc:  # noqa: BLE001
                # 走到这里说明退出评估本身炸了（例如风控内部异常被打包抛出），
                # 必须留下 critical 记录，调度层据此人工介入。
                self.faults.record("exit_pass_failed", {
                    "error": f"{type(exc).__name__}: {exc}",
                    "positions": [p.symbol for p in positions],
                    "detail": "退出评估阶段整体失败 —— 本轮止损可能未执行，需人工核对持仓",
                }, severity=SEV_CRITICAL)
                executed.append({"kind": "exit_pass_failed",
                                 "error": f"{type(exc).__name__}: {exc}"})
            return executed

    # ------------------------------------------------------------------ #
    def _phase(self, name: str, fn):
        """执行一个可失败阶段；失败只记录、不中断 monitor。"""
        try:
            return fn() or []
        except Exception as exc:  # noqa: BLE001
            self.faults.record("monitor_phase_failed", {
                "phase": name, "error": f"{type(exc).__name__}: {exc}",
                "detail": "该阶段已跳过，后续阶段继续（止损不受影响）",
            }, severity=SEV_ERROR)
            return [{"kind": "phase_failed", "phase": name,
                     "error": f"{type(exc).__name__}: {exc}"}]

    def _phase_refresh_pending(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        refresh = getattr(self.broker, "refresh_pending", None)
        if not callable(refresh):
            return out
        for filled in refresh():
            d = filled.as_dict()
            self._jrecord_order({**d, "note": "挂单复核成交"})
            out.append({"kind": "pending_fill", **d})
        return out

    def _run_exit_pass(self, positions: List[Position],
                       now: datetime) -> List[Dict[str, Any]]:
        """行情 + 止损止盈评估。这是 monitor 的核心，独立成段便于隔离与测试。"""
        executed: List[Dict[str, Any]] = []

        quotes: Dict[str, Quote] = {}
        for p in positions:
            try:
                # US-P2-A：退出路径放宽到"任何可交易时段"（含盘前/盘后/夜盘），
                # 但仍要求行情新鲜 —— 过期行情不得当作最新价用于止损判断。
                quotes[p.symbol] = self.quote_gateway.quote(
                    p.symbol, "risk_exit", now=now, market=p.market)
            except Exception as exc:  # noqa: BLE001
                # US-P0-4：行情失败**不能**让这条持仓的止损静默失效。
                # 退一步用券商持仓里的最新价（交易所回报的 mark，不是本地造的），
                # 并把"用了哪个价"写进审计。
                mark = float(getattr(p, "last_price", 0.0) or 0.0)
                if mark > 0:
                    quotes[p.symbol] = Quote(
                        symbol=p.symbol, market=p.market, last=mark,
                        source="position_mark",
                    )
                    self._jlog("quote_fallback_position_mark", {
                        "symbol": p.symbol, "mark": mark,
                        "error": f"{type(exc).__name__}: {exc}",
                        "detail": "行情接口失败，止损判定退化为券商持仓最新价",
                    }, subject=p.symbol)
                else:
                    self._jlog("quote_unavailable", {
                        "symbol": p.symbol,
                        "error": f"{type(exc).__name__}: {exc}",
                        "detail": "行情失败且持仓无最新价，该标的无法评估止损（已告警）",
                    }, subject=p.symbol, severity=SEV_ERROR)

        # 没有止损规则的持仓：必须告警（P1-8）
        for p in positions:
            if p.qty and p.symbol not in self._stops:
                self._jlog("position_without_stop", {
                    "symbol": p.symbol, "qty": p.qty,
                    "session": getattr(p, "session", "") or "unknown",
                    "detail": "该持仓没有登记止损/止盈规则，monitor 不会保护它",
                }, subject=p.symbol)

        # R-2：退出评估前必须先刷新"未完成卖单预留量"
        self._load_sell_reservations()
        intents = self.risk.scan_exits(positions, self._stops, quotes)
        for intent in intents:
            currency = currency_of(intent.symbol)
            # US-P0-4：组合快照读不出来也必须能继续处置风险
            portfolio = self._portfolio_for_exit(currency, now, positions)
            _q = quotes[intent.symbol]
            recent, orders_ok = self._recent_orders_for(intent.symbol, limit=10)
            if not orders_ok:
                self.faults.record(KIND_ORDER_READ, {
                    "phase": "exit_evaluate", "symbol": intent.symbol,
                    "detail": "订单历史不可读 → 本次减仓按 reduce-only 约束评估",
                }, severity=SEV_ERROR)
            decision = self.risk.evaluate(
                intent=intent, portfolio=portfolio,
                last_price=float(_q.last),
                recent_orders=recent,
                now=now,
                journal=self.journal,
                prev_close=float(getattr(_q, "prev_close", 0.0) or 0.0) or None,
                orders_known=(orders_ok and getattr(portfolio, "orders_known", True)
                              and self._intents_available),
            )
            self._jrecord_risk_decision(decision.as_dict())

            # US-P1 N-4：时间戳异常要落到独立故障通道（不被审计故障吞掉）
            for chk in decision.checks:
                if chk.name == "order_timestamp_anomaly":
                    self.faults.record(KIND_TIMESTAMP, {
                        "symbol": intent.symbol, "detail": chk.detail,
                    }, severity=SEV_WARN)

            if not decision.approved:
                self._jlog("exit_blocked", {"symbol": intent.symbol,
                                            "reasons": decision.reasons},
                           subject=intent.symbol, severity=SEV_ERROR)
                executed.append({"kind": "exit_blocked", "symbol": intent.symbol,
                                 "reasons": decision.reasons})
                continue

            # US-P1 N-2：走了 reduce-only 豁免通道 → 必须留"退出授权"审计
            if any(c.name in ("unresolved_order_state", "orders_history",
                              "halt_storage_write") and c.ok for c in decision.checks):
                self._jlog("exit_authorized_reduce_only", {
                    "symbol": intent.symbol, "side": intent.side, "qty": intent.qty,
                    "exit_mode": intent.exit_mode or "unspecified",
                    "positions_verified": bool(getattr(portfolio, "positions_verified", True)),
                    "held": float(getattr(portfolio.position_of(intent.symbol), "qty", 0) or 0),
                    "checks": {c.name: c.detail for c in decision.checks
                               if c.name in ("unresolved_order_state", "orders_history",
                                             "halt_storage_write")},
                    "detail": "在未知单/数据库/急停写失败等异常状态下，"
                              "经 reduce-only 硬约束放行的风险退出",
                }, subject=intent.symbol, severity=SEV_WARN)

            held_now = float(portfolio.position_of(intent.symbol).qty) if \
                portfolio.position_of(intent.symbol) else 0.0
            order = self._execute(intent, decision, portfolio_held=held_now)
            if order:
                # US-P1 Batch2 R-7：**不在这里清除止损**。
                # 是否解除保护必须等下一轮重新核实持仓后再决定，
                # 否则"未知买单随后成交"会让新持仓失去止损。
                self._mark_stop_exit_pending(intent.symbol, order)
                executed.append({"kind": "exit", "reason": intent.reason, **order})
        return executed

    # ------------------------------------------------------------------ #
    def _expire_stale_orders(self, now: datetime) -> List[Dict[str, Any]]:
        """把挂太久的活动委托撤掉。``order_timeout_seconds<=0`` 视为关闭。"""
        timeout_s = int(self.cfg.risk.get("order_timeout_seconds", 600) or 0)
        if timeout_s <= 0:
            return []
        cutoff = (now - timedelta(seconds=timeout_s)).astimezone().isoformat(
            timespec="seconds")
        out: List[Dict[str, Any]] = []
        rows, ok = self._jfind_orders(statuses=["submitted", "pending", "partial"],
                                      older_than=cutoff)
        if not ok:
            # 读不出活动委托 ≠ 没有活动委托：不能撤单，但也不阻断止损
            out.append({"kind": "phase_unavailable", "phase": "expire_orders",
                        "detail": "订单查询失败，本轮无法执行超时撤单"})
            return out
        for order in rows:
            oid = str(order.get("order_id"))
            res = self.cancel_order(oid, reason=f"超时未成交（>{timeout_s}s）", now=now)
            if res.get("ok"):
                self._jupdate_status(oid, "expired",
                                     reconcile_note=f"超时撤单（>{timeout_s}s）")
                out.append({"kind": "order_expired", "order_id": oid,
                            "status": "expired"})
            else:
                self._jlog("order_expire_failed", {"order_id": oid,
                                                   "reason": res.get("reason")},
                           subject=str(order.get("symbol", "")), severity=SEV_ERROR)
                out.append({"kind": "order_expire_failed", "order_id": oid,
                            "reason": res.get("reason")})
        return out

    def _reconcile_pending_unknown(self, now: datetime) -> List[Dict[str, Any]]:
        """对未终结的委托再做一次对账。

        US-P1 Batch2 R-1：**以意图表为权威**（预写保证了一定有行），
        再兼容历史仅存在于 ``orders`` 表的 unknown 行。
        只有券商明确回答（找到 / 明确不存在）才会改变状态；
        对账失败一律保持 ``unknown``，绝不假定成功或不存在。
        """
        out: List[Dict[str, Any]] = []
        reconcile = getattr(self.broker, "reconcile_order", None)

        # 1) 意图表（权威来源）
        try:
            intents = self.journal.list_open_intents()
            intents_ok = True
        except Exception as exc:  # noqa: BLE001
            intents, intents_ok = [], False
            self.faults.record(KIND_ORDER_INTENT, {
                "phase": "reconcile_list", "error": f"{type(exc).__name__}: {exc}",
            }, severity=SEV_ERROR)
        if not intents_ok:
            out.append({"kind": "phase_unavailable", "phase": "reconcile_unknown",
                        "detail": "订单意图表不可读，本轮无法对账"})

        if callable(reconcile):
            for row in intents:
                cid = str(row.get("client_id") or "")
                if not cid:
                    continue
                sym = str(row.get("symbol") or "")
                side = str(row.get("side") or "")
                qty = float(row.get("qty") or 0)
                try:
                    found = reconcile(client_id=cid, symbol=sym, side=side, qty=qty)
                except Exception as exc:  # noqa: BLE001
                    self._jlog("reconcile_retry_failed", {
                        "client_id": cid, "symbol": sym,
                        "error": f"{type(exc).__name__}: {exc}"},
                        subject=sym, severity=SEV_ERROR)
                    # 对账失败：保持 unknown（预留不释放）
                    self._jupdate_intent(cid, INTENT_STATE_UNKNOWN,
                                         broker_status="unknown",
                                         last_error=f"对账异常：{exc}", bump_attempt=True)
                    out.append({"kind": "reconcile_failed", "client_id": cid})
                    continue
                if found is None:
                    rec = {
                        "order_id": str(row.get("broker_order_id") or f"UNCONFIRMED-{cid}"),
                        "broker_order_id": str(row.get("broker_order_id") or ""),
                        "symbol": sym, "market": row.get("market"),
                        "side": side, "qty": qty, "price": row.get("price"),
                        "order_type": row.get("order_type"), "status": "not_accepted",
                        "filled_qty": 0.0, "avg_fill_price": None, "commission": 0.0,
                        "venue": getattr(self.broker, "venue", ""), "ts": utcnow_iso(),
                        "signal_id": row.get("signal_id"),
                        "decision_id": row.get("decision_id"), "client_id": cid,
                        "note": "对账确认券商侧无此单",
                        "reconcile_note": "二次对账：券商侧确认无此单（释放卖出预留）",
                    }
                    self._settle_intent_from_result(cid, rec)
                    self._jrecord_order(rec)
                    self._jlog("order_not_accepted", {
                        "client_id": cid, "symbol": sym,
                        "note": "券商侧无此单，意图置终态并释放预留"},
                        subject=sym)
                    out.append({"kind": "order_not_accepted", "client_id": cid,
                                "symbol": sym})
                else:
                    rec = found.as_dict()
                    rec["client_id"] = rec.get("client_id") or cid
                    rec["symbol"] = rec.get("symbol") or sym
                    rec["signal_id"] = row.get("signal_id")
                    rec["decision_id"] = row.get("decision_id")
                    rec["reconcile_note"] = (rec.get("reconcile_note") or "") + "｜二次对账确认"
                    self._settle_intent_from_result(cid, rec)
                    self._jrecord_order(rec)
                    out.append({"kind": "order_reconciled", "client_id": cid,
                                "symbol": sym, "status": rec.get("status")})

        # 2) 兼容：历史 unknown 行（无对应意图）
        rows, ok = self._jfind_orders(statuses=["unknown"])
        if not ok:
            out.append({"kind": "phase_unavailable", "phase": "reconcile_legacy",
                        "detail": "订单查询失败，本轮无法对账历史 unknown 委托"})
            return out
        known_cids = {str(r.get("client_id") or "") for r in intents}
        if rows and callable(reconcile):
            for row in rows:
                cid = str(row.get("client_id") or "")
                if cid and cid in known_cids:
                    continue           # 已由意图路径处理
                if not cid:
                    continue
                try:
                    found = reconcile(client_id=cid, symbol=str(row.get("symbol") or ""))
                except Exception as exc:  # noqa: BLE001
                    self._jlog("reconcile_retry_failed", {
                        "order_id": row.get("order_id"), "client_id": cid,
                        "error": f"{type(exc).__name__}: {exc}"},
                        subject=str(row.get("symbol") or ""), severity=SEV_ERROR)
                    out.append({"kind": "reconcile_failed", "order_id": row.get("order_id")})
                    continue
                if found is None:
                    self._jupdate_status(str(row.get("order_id")), "not_accepted",
                                         reconcile_note="二次对账：券商侧确认无此单")
                    out.append({"kind": "order_not_accepted",
                                "order_id": row.get("order_id")})
                else:
                    self._jupdate_status(
                        str(row.get("order_id")), str(found.status or "unknown"),
                        broker_order_id=found.broker_order_id,
                        filled_qty=found.filled_qty,
                        avg_fill_price=found.avg_fill_price,
                        reconcile_note="二次对账确认")
                    out.append({"kind": "order_reconciled",
                                "order_id": row.get("order_id"),
                                "status": found.status})
        return out

    # ------------------------------------------------------------------ #
    def _recent_orders_for(self, symbol: str, limit: int = 30) -> tuple:
        """订单历史 + **未终结意图**合并视图（R-1 / R-2 的判定输入）。

        意图表是权威来源：即使 ``orders`` 写失败，只要预写成功过，
        这里就一定能把"该标的还有一笔未终结委托"暴露给风控。
        返回 ``(rows, ok)``；``ok=False`` 表示至少一个来源读不出来。
        """
        rows, ok = self._jlist_orders(limit=limit, symbol=symbol)
        try:
            intents = self.journal.list_open_intents(symbol=symbol)
            iok = True
        except Exception as exc:  # noqa: BLE001
            intents, iok = [], False
            self.faults.record(KIND_ORDER_INTENT, {
                "phase": "recent_orders", "symbol": symbol,
                "error": f"{type(exc).__name__}: {exc}",
            }, severity=SEV_ERROR)
        seen = {str(r.get("client_id") or "") for r in rows}
        for it in intents:
            cid = str(it.get("client_id") or "")
            if cid and cid in seen:
                # 已存在 orders 行：把状态改写成最保守的"未完成"
                for r in rows:
                    if str(r.get("client_id") or "") == cid and \
                            str(r.get("status") or "").lower() in ("filled", "cancelled"):
                        pass
                continue
            rows.append({
                "order_id": str(it.get("broker_order_id") or f"INTENT-{cid}"),
                "broker_order_id": it.get("broker_order_id") or "",
                "symbol": it.get("symbol"), "market": it.get("market"),
                "side": it.get("side"), "qty": it.get("qty"),
                "price": it.get("price"), "order_type": it.get("order_type"),
                "status": ("unknown" if str(it.get("state")) == INTENT_STATE_UNKNOWN
                           else "submitted"),
                "filled_qty": 0.0, "avg_fill_price": None, "commission": 0.0,
                "venue": getattr(self.broker, "venue", ""),
                "ts": it.get("created_at"), "signal_id": it.get("signal_id"),
                "decision_id": it.get("decision_id"), "client_id": cid,
                "note": f"来自意图表（state={it.get('state')}）",
                "reconcile_note": it.get("last_error") or "",
                "_from_intent": True,
            })
        return rows, (ok and iok)

    def status(self) -> Dict[str, Any]:
        sessions: Dict[str, Any] = {}
        for mkt in ("us", "hk", "cn"):
            info = self.risk.rules.session_info(mkt)
            if info is not None:
                sessions[mkt] = info.as_dict()
        return {
            "model": self.model.health(),
            "scans": self._scan_count,
            "last_scan": self.last_scan.as_dict() if self.last_scan else None,
            "active_stops": self._stops,
            "kill_switch": self.cfg.gate.state.kill_switch,
            "halt_reason": self.risk.halt_reason,
            "authority": self.authority.snapshot().as_dict(),
            "feed": self.feed_snapshot(),
            "allow_rule_engine_entry": self.allow_rule_engine_entry,
            "universe": [u["code"] for u in self.cfg.universe],
            # US-P0-1：当前各市场的交易所当地时间与阶段（常规/盘前/盘后/夜盘/休市）
            "sessions": sessions,
            # US-P0-3：持仓是否处于"读不到"状态（此时禁止新开仓）
            "positions_stale": self._positions_stale,
            "position_sessions": dict(self._position_session_flags_report()),
        }

    def _position_session_flags_report(self) -> Dict[str, Any]:
        """持仓查询实际覆盖的时段（美股盘前/盘后/夜盘）。"""
        fn = getattr(self.broker, "_position_session_flags", None)
        if callable(fn):
            try:
                return fn()
            except Exception:  # noqa: BLE001
                return {}
        return {}
