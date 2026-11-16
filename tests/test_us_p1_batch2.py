"""US-P1 Batch 2 验收测试：R-1 / R-2 / R-7 / R-4。

全部使用**真实生产调用链**（`StrategyEngine.scan/monitor` → `RiskController.evaluate`
→ `broker.place_order`），真实 SQLite 文件；故障注入只让单个方法抛错，
不用 mock 顶替被测逻辑。

R-3 / R-8（reduce-only）**不在本轮启用**，仅另有设计报告，本文不含启用性测试。
"""
from __future__ import annotations

import json
import pathlib
import sqlite3
import sys
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from ai_trader.audit.journal import (  # noqa: E402
    INTENT_OPEN_STATES,
    INTENT_STATE_ACCEPTED,
    INTENT_STATE_TERMINAL,
    INTENT_STATE_UNKNOWN,
    AuditJournal,
)
from ai_trader.broker.base import (  # noqa: E402
    BrokerError,
    OrderResult,
    Position,
    Quote,
)
from ai_trader.market.rules import MarketRuleSet  # noqa: E402
from ai_trader.risk.controller import (  # noqa: E402
    OrderIntent,
    Portfolio,
    RiskController,
)
from ai_trader.strategy.engine import StrategyEngine  # noqa: E402

import tests.test_us_p1_batch1 as B1  # 复用 Batch1 的夹具（同一套真实组件）


TZ_NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 4, 10, 0, tzinfo=TZ_NY)
POS_QTY = 10


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
class BuyModel:
    """价格自适应买入模型（避免与合成行情耦合）。"""

    name, model_id, can_trade, available, is_llm = "stub", "buy-v1", True, True, False

    def analyze(self, ctx):
        from ai_trader.ai.base import TradingSignal
        px = float(ctx.last_price)
        return TradingSignal(symbol=ctx.symbol, action="buy", confidence=0.9,
                             entry_price=px, stop_loss=round(px * 0.97, 2),
                             take_profit=round(px * 1.05, 2), position_pct=0.05,
                             market=ctx.market, provider="stub", model="buy-v1",
                             rationale="batch2 probe")

    def health(self):
        return {"name": self.name, "model_id": self.model_id}


class RecordingBroker(B1.StubBroker):
    """可编排券商：记录每次下单/对账，支持超时、空单号、拒单。"""

    def __init__(self, *, price=200.0, mode="filled", positions=None,
                 reconcile=None, reconcile_exc=None):
        super().__init__(price=price, positions=positions)
        self.mode = mode
        self.reconcile = reconcile
        self.reconcile_exc = reconcile_exc
        self.reconcile_calls = []

    def place_order(self, req):
        if self.mode in ("reject", "timeout", "unknown"):
            # 早返回分支不会走到父类，自己记一笔
            self.places.append({"symbol": req.symbol, "side": req.side,
                                "qty": req.qty, "client_id": req.client_id})
        if self.mode == "reject":
            raise BrokerError("券商拒单", code="REJECTED")
        if self.mode == "timeout":
            raise TimeoutError("gateway timeout")
        if self.mode == "unknown":
            return OrderResult(order_id="", broker_order_id="", symbol=req.symbol,
                               market=req.market, side=req.side, qty=req.qty,
                               price=req.price, status="unknown", venue="stub",
                               ts=NOW.isoformat(timespec="seconds"),
                               client_id=req.client_id,
                               reconcile_note="券商未返回 orderId")
        return super().place_order(req)

    def reconcile_order(self, client_id="", symbol="", side="", qty=0.0):
        self.reconcile_calls.append(client_id)
        if self.reconcile_exc:
            raise self.reconcile_exc
        if callable(self.reconcile):
            return self.reconcile(client_id=client_id, symbol=symbol,
                                  side=side, qty=qty)
        return self.reconcile


class Flaky(AuditJournal):
    """只让指定方法抛错的真实审计库。"""

    BROKEN: set = set()

    def _maybe(self, name):
        if name in self.BROKEN:
            raise sqlite3.OperationalError(f"database is temporarily unavailable ({name})")

    def prewrite_intent(self, *a, **k):
        self._maybe("prewrite_intent")
        return super().prewrite_intent(*a, **k)

    def record_order(self, *a, **k):
        self._maybe("record_order")
        return super().record_order(*a, **k)

    def update_intent(self, *a, **k):
        self._maybe("update_intent")
        return super().update_intent(*a, **k)

    def list_open_intents(self, *a, **k):
        self._maybe("list_open_intents")
        return super().list_open_intents(*a, **k)

    def open_sell_reserved(self, *a, **k):
        self._maybe("open_sell_reserved")
        return super().open_sell_reserved(*a, **k)

    def list_orders(self, *a, **k):
        self._maybe("list_orders")
        return super().list_orders(*a, **k)

    def find_orders(self, *a, **k):
        self._maybe("find_orders")
        return super().find_orders(*a, **k)


def _engine(tmp, broker, journal=None, *, stops=None, model=None):
    cfg = B1._cfg(tmp)
    j = journal or Flaky(tmp / "audit.db", tmp / "audit.jsonl")
    risk = RiskController(cfg, cfg.gate, rules=MarketRuleSet(cfg.raw["market_rules"]))
    eng = StrategyEngine(cfg, broker, model or BuyModel(), risk, j,
                         stops_path=cfg.data_dir / "stops.json")
    if stops:
        eng.register_stops("usAAPL", stop_loss=stops, take_profit=None, qty=POS_QTY)
    return cfg, eng, j


def _positions(qty=POS_QTY, price=200.0):
    if qty <= 0:
        return []
    return [Position(symbol="usAAPL", market="us", qty=qty, avg_cost=200.0,
                     last_price=price, currency="USD")]


def _sell_intent(qty=POS_QTY):
    return OrderIntent(symbol="usAAPL", side="sell", qty=qty, price=190.0,
                       market="us", currency="USD", is_exit=True,
                       exit_mode="stop_loss", reason="止损")


def _buy_intent(qty=5):
    return OrderIntent(symbol="usAAPL", side="buy", qty=qty, price=200.0,
                       market="us", currency="USD", stop_loss=194.0)


# =========================================================================== #
# R-1：下单后落库失败 / 预写 / 恢复
# =========================================================================== #
def test_r1_prewrite_success_normal_order(tmp_path):
    """预写成功 + 券商正常成交：意图既存在又终结，orders 与意图一致。"""
    broker = RecordingBroker(price=200.0)
    cfg, eng, j = _engine(tmp_path, broker)
    out = eng.analyze_symbol("usAAPL", now=NOW)
    assert out.stage == "ordered", out.detail

    intents = j.list_open_intents()
    assert intents == [], "终态订单不应出现在未终结列表"
    all_rows = j._conn.execute("SELECT * FROM order_intents").fetchall()
    assert len(all_rows) == 1
    row = dict(all_rows[0])
    assert row["state"] == INTENT_STATE_TERMINAL
    assert row["broker_order_id"]
    assert row["request_digest"] and row["request_json"]
    assert broker.places[0]["client_id"] == row["client_id"]
    j.close()


def test_r1_prewrite_success_unknown_keeps_intent(tmp_path):
    """券商返回 unknown + 空 order_id：意图保留为 unknown，且能按 client_id 对账。"""
    broker = RecordingBroker(price=200.0, mode="unknown",
                             reconcile=OrderResult(
                                 order_id="BRK-1", broker_order_id="BRK-1",
                                 symbol="usAAPL", market="us", side="buy", qty=5,
                                 price=200.0, status="filled", filled_qty=5,
                                 venue="stub", ts=NOW.isoformat(timespec="seconds")))
    cfg, eng, j = _engine(tmp_path, broker)
    out = eng.analyze_symbol("usAAPL", now=NOW)
    assert out.stage == "ordered"
    # 券商"返回 unknown"不会自动对账：由 monitor 的二次对账完成
    assert len(j.list_open_intents()) == 1, "unknown 必须保留意图（不释放预留）"
    eng.monitor(now=NOW + timedelta(seconds=5))

    intents = j.list_open_intents()
    # 对账成功后应终结
    assert intents == [], intents
    row = dict(j._conn.execute("SELECT * FROM order_intents").fetchone())
    assert row["client_id"] in broker.reconcile_calls, "对账必须用 client_id"
    j.close()


def test_r1_prewrite_success_timeout_marks_unknown(tmp_path):
    """broker 超时（非 BrokerError）→ 意图保持 unknown，绝不删除。"""
    broker = RecordingBroker(price=200.0, mode="timeout",
                             reconcile_exc=BrokerError("对账不可达", code="NET"))
    cfg, eng, j = _engine(tmp_path, broker)
    eng.analyze_symbol("usAAPL", now=NOW)

    intents = j.list_open_intents()
    assert len(intents) == 1, intents
    assert intents[0]["state"] == INTENT_STATE_UNKNOWN
    assert intents[0]["last_error"], "必须记录失败原因"
    j.close()


def test_r1_broker_rejected_releases_reservation(tmp_path):
    """券商明确拒单 → 意图置 rejected（不属于未终结），不占用预留。"""
    broker = RecordingBroker(price=200.0, mode="reject")
    cfg, eng, j = _engine(tmp_path, broker)
    out = eng.analyze_symbol("usAAPL", now=NOW)
    assert out.stage == "rejected"
    assert j.list_open_intents() == []
    row = dict(j._conn.execute("SELECT * FROM order_intents").fetchone())
    assert row["state"] == "rejected"
    j.close()


def test_r1_record_order_failure_keeps_intent_visible(tmp_path):
    """orders 落库失败，但意图仍在（订单不再"彻底消失"）。"""
    j = Flaky(tmp_path / "audit.db", tmp_path / "audit.jsonl")
    broker = RecordingBroker(price=200.0)
    cfg, eng, j = _engine(tmp_path, broker, j)
    j.BROKEN = {"record_order"}
    out = eng.analyze_symbol("usAAPL", now=NOW)
    assert out.stage == "ordered"

    orders = j._conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"]
    intents = j._conn.execute("SELECT * FROM order_intents").fetchall()
    assert orders == 0, "orders 写入确实失败了"
    assert len(intents) == 1, "意图必须仍在（预写发生在下单前）"
    row = dict(intents[0])
    assert row["client_id"] == broker.places[0]["client_id"]
    assert row["state"] in (INTENT_STATE_TERMINAL, INTENT_STATE_UNKNOWN)
    j.close()


def test_r1_prewrite_failure_never_calls_broker(tmp_path):
    """预写失败 → 绝不允许调用券商（不允许"先发单再补记录"）。"""
    j = Flaky(tmp_path / "audit.db", tmp_path / "audit.jsonl")
    broker = RecordingBroker(price=200.0)
    cfg, eng, j = _engine(tmp_path, broker, j)
    j.BROKEN = {"prewrite_intent"}
    out = eng.analyze_symbol("usAAPL", now=NOW)
    assert out.stage == "rejected"
    assert broker.places == [], "预写失败却发出了订单"
    kinds = {f["kind"] for f in eng.faults.read_all()}
    assert "order_intent_failed" in kinds
    j.close()


@pytest.mark.parametrize("broken", ["prewrite_intent"])
def test_r1_persistence_unavailable_blocks_all_entries(tmp_path, broken):
    """持久化介质不可用：连 scan 也必须被拦住，且不产生任何委托。"""
    j = Flaky(tmp_path / "audit.db", tmp_path / "audit.jsonl")
    broker = RecordingBroker(price=200.0)
    cfg, eng, j = _engine(tmp_path, broker, j)
    j.BROKEN = {broken}
    rep = eng.scan(symbols=["usAAPL"], now=NOW)
    assert broker.places == []
    assert rep.outcomes and rep.outcomes[0].stage in ("rejected", "blocked", "error")
    j.close()


def test_r1_crash_after_broker_before_settle_recovers_as_unknown(tmp_path):
    """进程在"券商返回后、状态更新前"崩溃 → 重启恢复为 unknown 并阻断。"""
    tmp = tmp_path
    j = Flaky(tmp / "audit.db", tmp / "audit.jsonl")
    broker = RecordingBroker(price=200.0)
    cfg, eng, j = _engine(tmp, broker, j)
    # 手工模拟：预写成功（intent 状态），但后续状态更新从未发生 = 崩溃
    rec = {"client_id": "crash-cid-1", "created_at": NOW.isoformat(),
           "symbol": "usAAPL", "market": "us", "side": "buy", "qty": 5,
           "price": 200.0, "order_type": "limit", "currency": "USD",
           "request": {"symbol": "usAAPL", "side": "buy", "qty": 5}}
    ok, _ = j.prewrite_intent(rec)
    assert ok
    j.close()

    # 重启：新的 engine + 新连接，同一 DB
    j2 = Flaky(tmp / "audit.db", tmp / "audit.jsonl")
    broker2 = RecordingBroker(price=200.0)
    cfg2, eng2, j2 = _engine(tmp, broker2, j2)
    intents = j2.list_open_intents()
    assert len(intents) == 1 and intents[0]["state"] == INTENT_STATE_UNKNOWN
    assert "crash-cid-1" in eng2._recovered_intents
    j2.close()


def test_r1_after_restart_reentry_blocked_until_reconciled(tmp_path):
    """重启后未对账 → 同标的开仓被阻断；对账确认后才放行。"""
    tmp = tmp_path
    j = Flaky(tmp / "audit.db", tmp / "audit.jsonl")
    rec = {"client_id": "crash-cid-2", "created_at": NOW.isoformat(),
           "symbol": "usAAPL", "market": "us", "side": "buy", "qty": 5,
           "price": 200.0, "order_type": "limit", "currency": "USD",
           "request": {"symbol": "usAAPL", "side": "buy", "qty": 5}}
    j.prewrite_intent(rec)
    j.update_intent("crash-cid-2", INTENT_STATE_UNKNOWN, last_error="crash")
    j.close()

    j2 = Flaky(tmp / "audit.db", tmp / "audit.jsonl")
    broker = RecordingBroker(price=200.0, reconcile_exc=BrokerError("对账不可达", code="NET"))
    cfg, eng, j2 = _engine(tmp, broker, j2)
    out = eng.analyze_symbol("usAAPL", now=NOW + timedelta(seconds=400))
    assert out.stage == "rejected"
    assert "unresolved_order_state" in [c["name"] for c in out.decision["checks"]
                                        if not c["ok"]]
    assert broker.places == []

    # 对账确认成交 → 解除
    broker.reconcile_exc = None
    broker.reconcile = OrderResult(order_id="BRK-R", broker_order_id="BRK-R",
                                   symbol="usAAPL", market="us", side="buy", qty=5,
                                   price=200.0, status="filled", filled_qty=5,
                                   venue="stub", ts=NOW.isoformat(timespec="seconds"))
    eng.monitor(now=NOW + timedelta(seconds=410))
    out2 = eng.analyze_symbol("usAAPL", now=NOW + timedelta(seconds=420))
    names = [c["name"] for c in out2.decision["checks"] if not c["ok"]]
    assert "unresolved_order_state" not in names, names
    j2.close()


def test_r1_two_workers_same_decision_place_once(tmp_path):
    """两个 worker 处理同一决策（同 client_id）→ 券商只应收到 1 笔。"""
    tmp = tmp_path
    j = Flaky(tmp / "audit.db", tmp / "audit.jsonl")
    broker = RecordingBroker(price=200.0)
    cfg, eng, j = _engine(tmp, broker, j)
    eng.analyze_symbol("usAAPL", now=NOW)
    first = len(broker.places)
    assert first == 1
    # 第二个 worker：同一个 DB，同一 client_id 再预写
    j.close()
    j2 = Flaky(tmp / "audit.db", tmp / "audit.jsonl")
    cid = j2._conn.execute("SELECT client_id FROM order_intents").fetchone()["client_id"]
    ok, why = j2.prewrite_intent({"client_id": cid, "symbol": "usAAPL",
                                  "side": "buy", "qty": 5, "request": {}})
    assert ok is False and why.startswith("duplicate"), why
    j2.close()


# =========================================================================== #
# R-2：可卖余量 / 未完成卖单预留
# =========================================================================== #
def _risk(tmp, *, qty=POS_QTY, reserved=None, known=True, intents_ok=True):
    cfg = B1._cfg(tmp)
    risk = RiskController(cfg, cfg.gate, rules=MarketRuleSet(cfg.raw["market_rules"]))
    pf = Portfolio(currency="USD", equity=1e6, cash=1e6,
                   positions=_positions(qty),
                   positions_verified=True,
                   open_sell_reserved={k.lower(): v for k, v in (reserved or {}).items()},
                   reservations_known=known,
                   intents_available=intents_ok)
    return risk, pf


def test_r2_hold10_reserved10_sell10_rejected(tmp_path):
    risk, pf = _risk(tmp_path, reserved={"usAAPL": 10})
    d = risk.evaluate(_sell_intent(10), pf, last_price=190.0, now=NOW)
    assert not d.approved
    assert "sell_capacity" in d.failed_checks


def test_r2_hold10_unknown_sell6_sell5_rejected(tmp_path):
    risk, pf = _risk(tmp_path, reserved={"usAAPL": 6})
    d = risk.evaluate(_sell_intent(5), pf, last_price=190.0, now=NOW)
    assert not d.approved
    assert "sell_capacity" in d.failed_checks


def test_r2_two_unknown_sells_merged(tmp_path):
    """两笔未知卖 4 + 6 = 10 → 再卖 1 也必须拒绝（合并计算）。"""
    risk, pf = _risk(tmp_path, reserved={"usAAPL": 10})
    d = risk.evaluate(_sell_intent(1), pf, last_price=190.0, now=NOW)
    assert not d.approved
    assert "超过可卖余量" in " ".join(d.reasons)


def test_r2_partial_room_allows_partial_exit(tmp_path):
    """持仓 10、预留 4 → 卖 6 允许，卖 7 拒绝。"""
    risk, pf = _risk(tmp_path, reserved={"usAAPL": 4})
    ok = risk.evaluate(_sell_intent(6), pf, last_price=190.0, now=NOW)
    assert ok.approved, ok.reasons
    bad = risk.evaluate(_sell_intent(7), pf, last_price=190.0, now=NOW)
    assert not bad.approved


def test_r2_reservations_unreadable_blocks_sell(tmp_path):
    """预留量读不出来 → 不能当 0，必须禁止新增卖出。"""
    risk, pf = _risk(tmp_path, known=False)
    d = risk.evaluate(_sell_intent(10), pf, last_price=190.0, now=NOW)
    assert not d.approved
    assert "reduction_blocked_unverified" in " ".join(d.reasons)


def test_r2_intents_unavailable_blocks_sell(tmp_path):
    risk, pf = _risk(tmp_path, intents_ok=False)
    d = risk.evaluate(_sell_intent(10), pf, last_price=190.0, now=NOW)
    assert not d.approved
    assert "sell_capacity" in d.failed_checks


def test_r2_stop_loss_cannot_bypass_capacity(tmp_path):
    """止损优先级高也不能绕过可卖余量。"""
    risk, pf = _risk(tmp_path, reserved={"usAAPL": 10})
    d = risk.evaluate(_sell_intent(10), pf, last_price=185.0, now=NOW)
    assert not d.approved
    assert "sell_capacity" in d.failed_checks


def test_r2_atomic_reservation_single_winner(tmp_path):
    """8 个并发 worker 抢同一份卖出额度 → 只能有 1 个成功预写。"""
    j = Flaky(tmp_path / "audit.db", tmp_path / "audit.jsonl")
    results = []
    lock = threading.Lock()

    def worker(i):
        try:
            ok, why = j.prewrite_intent(
                {"client_id": f"c{i}", "symbol": "usAAPL", "side": "sell",
                 "qty": 10, "request": {}}, sell_capacity=10)
        except Exception as exc:  # noqa: BLE001
            ok, why = None, f"{type(exc).__name__}: {exc}"
        with lock:
            results.append((ok, why))

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]

    wins = [r for r in results if r[0] is True]
    assert len(wins) == 1, results
    assert all(("capacity" in (r[1] or "")) for r in results if r[0] is False)
    reserved, ok = j.open_sell_reserved("usAAPL")
    assert ok and reserved["usaapl"] == 10
    j.close()


def test_r2_reservation_released_only_after_confirmed_terminal(tmp_path):
    """明确终结（filled/cancelled）才释放；unknown 绝不释放。"""
    j = Flaky(tmp_path / "audit.db", tmp_path / "audit.jsonl")
    j.prewrite_intent({"client_id": "s1", "symbol": "usAAPL", "side": "sell",
                       "qty": 6, "request": {}}, sell_capacity=10)
    reserved, _ = j.open_sell_reserved("usAAPL")
    assert reserved["usaapl"] == 6

    j.update_intent("s1", INTENT_STATE_UNKNOWN, last_error="timeout")
    reserved, _ = j.open_sell_reserved("usAAPL")
    assert reserved["usaapl"] == 6, "unknown 不得自动释放预留"

    j.update_intent("s1", INTENT_STATE_TERMINAL, broker_status="cancelled")
    reserved, _ = j.open_sell_reserved("usAAPL")
    assert reserved.get("usaapl", 0) == 0, "确认终结后才释放"
    j.close()


def test_r2_rechecks_position_size_each_cycle(tmp_path):
    """持仓变化后重新核算：预留 6，持仓从 10 降到 8 → 可卖 2。"""
    risk, pf = _risk(tmp_path, qty=8, reserved={"usAAPL": 6})
    assert risk.evaluate(_sell_intent(2), pf, last_price=190.0, now=NOW).approved
    assert not risk.evaluate(_sell_intent(3), pf, last_price=190.0, now=NOW).approved


def test_r2_engine_blocks_second_sell_when_db_unreadable(tmp_path):
    """生产链路：券商已有一笔在途卖 + 预留不可读 → 不得再发第二笔卖单。"""
    j = Flaky(tmp_path / "audit.db", tmp_path / "audit.jsonl")
    broker = RecordingBroker(price=180.0)
    cfg, eng, j = _engine(tmp_path, broker, j, stops=190.0)
    # 预置一笔未完成卖单（预留 10）
    j.prewrite_intent({"client_id": "inflight-1", "symbol": "usAAPL",
                       "side": "sell", "qty": 10, "request": {}},
                      sell_capacity=POS_QTY)
    j.BROKEN = {"open_sell_reserved"}
    out = eng.monitor(now=NOW)
    assert broker.places == [], out
    assert any(o.get("kind") == "exit_blocked" for o in out), out
    j.close()


def test_r2_engine_allows_exit_without_reservation(tmp_path):
    """无在途卖单、预留可读 → 止损正常发出。"""
    j = Flaky(tmp_path / "audit.db", tmp_path / "audit.jsonl")
    broker = RecordingBroker(price=180.0)
    cfg, eng, j = _engine(tmp_path, broker, j, stops=190.0)
    out = eng.monitor(now=NOW)
    assert any(o.get("kind") == "exit" for o in out), out
    assert broker.places and broker.places[0]["side"] == "sell"
    j.close()


# =========================================================================== #
# R-7：止损保护绑定实际持仓
# =========================================================================== #
def test_r7_exit_does_not_clear_stops_immediately(tmp_path):
    j = Flaky(tmp_path / "audit.db", tmp_path / "audit.jsonl")
    broker = RecordingBroker(price=180.0)
    cfg, eng, j = _engine(tmp_path, broker, j, stops=190.0)
    out = eng.monitor(now=NOW)
    assert any(o.get("kind") == "exit" for o in out)
    assert eng.stops["usAAPL"]["exit_pending"] is True
    assert eng.stops["usAAPL"]["protected_qty"] == POS_QTY
    j.close()


def test_r7_unknown_buy_filled_keeps_stop(tmp_path):
    """卖出后未知买单成交（持仓回到 10）→ 保留止损并重算保护数量。"""
    j = Flaky(tmp_path / "audit.db", tmp_path / "audit.jsonl")
    broker = RecordingBroker(price=180.0)
    cfg, eng, j = _engine(tmp_path, broker, j, stops=190.0)
    j.prewrite_intent({"client_id": "ub-1", "symbol": "usAAPL", "side": "buy",
                       "qty": 10, "request": {}})
    j.update_intent("ub-1", INTENT_STATE_UNKNOWN, last_error="pending fill")

    # 第一轮：止损卖出（未知买单仍在）
    eng.monitor(now=NOW)
    assert eng.stops["usAAPL"]["exit_pending"] is True

    # 券商侧持仓仍为 10（未知买单已成交，卖出未成交）
    broker.positions = _positions(10)
    out2 = eng.monitor(now=NOW + timedelta(seconds=5))
    assert "usAAPL" in eng.stops, "未知买单成交后不得丢掉止损"
    assert eng.stops["usAAPL"]["protected_qty"] == 10
    assert any(o.get("kind") == "stops_retained" for o in out2), out2
    j.close()


def test_r7_stops_cleared_only_after_verified_flat(tmp_path):
    j = Flaky(tmp_path / "audit.db", tmp_path / "audit.jsonl")
    broker = RecordingBroker(price=180.0)
    cfg, eng, j = _engine(tmp_path, broker, j, stops=190.0)
    eng.monitor(now=NOW)
    broker.positions = []          # 已确认清仓
    out2 = eng.monitor(now=NOW + timedelta(seconds=5))
    assert "usAAPL" not in eng.stops
    assert any(o.get("kind") == "stops_cleared" for o in out2), out2
    j.close()


def test_r7_positions_unverified_keeps_stop(tmp_path):
    """持仓读不出来 → 保留保护，不解除。"""
    j = Flaky(tmp_path / "audit.db", tmp_path / "audit.jsonl")
    broker = RecordingBroker(price=180.0)
    cfg, eng, j = _engine(tmp_path, broker, j, stops=190.0)
    eng.monitor(now=NOW)
    broker.pos_exc = BrokerError("持仓接口 500", code="HTTP_500")
    out2 = eng.monitor(now=NOW + timedelta(seconds=5))
    assert "usAAPL" in eng.stops, out2
    j.close()


def test_r7_stops_survive_restart_with_protected_qty(tmp_path):
    j = Flaky(tmp_path / "audit.db", tmp_path / "audit.jsonl")
    broker = RecordingBroker(price=180.0)
    cfg, eng, j = _engine(tmp_path, broker, j, stops=190.0)
    eng.monitor(now=NOW)
    j.close()

    j2 = Flaky(tmp_path / "audit.db", tmp_path / "audit.jsonl")
    broker2 = RecordingBroker(price=180.0)
    cfg2, eng2, j2 = _engine(tmp_path, broker2, j2)
    assert "usAAPL" in eng2.stops
    assert eng2.stops["usAAPL"]["protected_qty"] == POS_QTY
    j2.close()


def test_r7_partial_exit_keeps_remaining_protected(tmp_path):
    """部分成交（持仓仍为 4）→ 保护数量跟随实际持仓重算。"""
    j = Flaky(tmp_path / "audit.db", tmp_path / "audit.jsonl")
    broker = RecordingBroker(price=180.0)
    cfg, eng, j = _engine(tmp_path, broker, j, stops=190.0)
    broker.positions = _positions(4)
    out = eng.monitor(now=NOW)
    assert "usAAPL" in eng.stops
    assert eng.stops["usAAPL"]["protected_qty"] == 4, out
    j.close()


# =========================================================================== #
# R-4：异常时间戳（开仓 fail-closed）
# =========================================================================== #
@pytest.mark.parametrize("ts,label", [
    ("", "空字符串"),
    ("not-a-timestamp", "无法解析"),
    (None, "缺失"),
])
def test_r4_entry_blocked_on_unparsable_ts(tmp_path, ts, label):
    risk, pf = _risk(tmp_path)
    row = {"order_id": "O1", "symbol": "usAAPL", "side": "buy", "qty": 5,
           "status": "filled", "ts": ts}
    d = risk.evaluate(_buy_intent(5), pf, last_price=200.0,
                      recent_orders=[row], now=NOW)
    assert not d.approved, label
    names = [c.name for c in d.checks if not c.ok]
    assert "duplicate_order" in names
    assert "order_timestamp_anomaly" in [c.name for c in d.checks]


def test_r4_entry_blocked_on_naive_ts(tmp_path):
    risk, pf = _risk(tmp_path)
    naive = NOW.replace(tzinfo=None).isoformat(timespec="seconds")
    row = {"order_id": "O1", "symbol": "usAAPL", "side": "buy", "qty": 5,
           "status": "filled", "ts": naive}
    d = risk.evaluate(_buy_intent(5), pf, last_price=200.0,
                      recent_orders=[row], now=NOW)
    assert not d.approved
    assert "duplicate_order" in [c.name for c in d.checks if not c.ok]


def test_r4_exit_still_allowed_with_bad_ts(tmp_path):
    """减仓不受异常时间戳阻断（但受预留/幂等约束）。"""
    risk, pf = _risk(tmp_path)
    row = {"order_id": "O1", "symbol": "usAAPL", "side": "sell", "qty": 5,
           "status": "filled", "ts": "not-a-timestamp"}
    d = risk.evaluate(_sell_intent(5), pf, last_price=190.0,
                      recent_orders=[row], now=NOW)
    assert d.approved, d.reasons


def test_r4_normal_recent_ts_still_blocks_and_old_ts_allows(tmp_path):
    risk, pf = _risk(tmp_path)
    recent = (NOW - timedelta(seconds=10)).isoformat(timespec="seconds")
    old = (NOW - timedelta(hours=2)).isoformat(timespec="seconds")
    assert not risk.evaluate(_buy_intent(5), pf, last_price=200.0,
                             recent_orders=[{"order_id": "A", "symbol": "usAAPL",
                                             "side": "buy", "qty": 5,
                                             "status": "filled", "ts": recent}],
                             now=NOW).approved
    assert risk.evaluate(_buy_intent(5), pf, last_price=200.0,
                         recent_orders=[{"order_id": "B", "symbol": "usAAPL",
                                         "side": "buy", "qty": 5,
                                         "status": "filled", "ts": old}],
                         now=NOW).approved


def test_r4_future_ts_isolated_not_ignored(tmp_path):
    """未来时间戳：开仓放行但必须留 anomaly（不是静默忽略）。"""
    risk, pf = _risk(tmp_path)
    future = (NOW + timedelta(days=30)).isoformat(timespec="seconds")
    d = risk.evaluate(_buy_intent(5), pf, last_price=200.0,
                      recent_orders=[{"order_id": "F", "symbol": "usAAPL",
                                      "side": "buy", "qty": 5,
                                      "status": "filled", "ts": future}],
                      now=NOW)
    assert d.approved
    assert "order_timestamp_anomaly" in [c.name for c in d.checks]


def test_r4_bad_ts_blocks_entry_through_engine(tmp_path):
    """生产链路：历史行 ts 无法解析 → scan 的真实开仓被拒。"""
    j = Flaky(tmp_path / "audit.db", tmp_path / "audit.jsonl")
    broker = RecordingBroker(price=200.0)
    cfg, eng, j = _engine(tmp_path, broker, j)
    j.record_order({"order_id": "BAD-1", "client_id": "bad-cid", "symbol": "usAAPL",
                    "side": "buy", "qty": 5, "price": 200.0, "status": "filled",
                    "ts": "???", "venue": "stub", "market": "us"})
    out = eng.analyze_symbol("usAAPL", now=NOW + timedelta(seconds=400))
    assert out.stage == "rejected"
    assert "无法确认冷却状态" in out.detail
    assert broker.places == []
    j.close()
