"""P0-1：订单状态完整性测试。

覆盖审计要求的三个特殊场景：
* **空订单响应** → 必须 UNKNOWN，不得 submitted
* **网络超时但券商实际已受理** → 对账查到 → 按真实状态落库，不重试下单
* 对账查不清 → 保持 unknown 并封锁该标的
"""
from __future__ import annotations

import pytest

from ai_trader.audit.journal import AuditJournal
from ai_trader.broker.base import (
    BrokerError,
    OrderRequest,
    OrderResult,
    OrderStateUnknown,
    derive_client_id,
)
from ai_trader.broker.fosun_adapter import FosunBroker
from ai_trader.risk.controller import OrderIntent, Portfolio, RiskController
from ai_trader.strategy.engine import StrategyEngine
from tests.conftest import WEEKDAY_1000
from tests.helpers import FixedSignalModel, ScriptedBroker, live_cfg


# ------------------------------------------------------------------ 适配器层
class _Trade:
    def __init__(self, resp=None, exc=None):
        self.resp = resp
        self.exc = exc
        self.calls = 0

    def create_order(self, **kw):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.resp


class _Client:
    def __init__(self, trade):
        self.trade = trade


def _broker(tmp_path, resp=None, exc=None):
    cfg = live_cfg(tmp_path)
    b = FosunBroker(cfg, None)
    trade = _Trade(resp, exc)
    b.client = _Client(trade)
    b.configured = True
    return b, trade


def _req(**kw):
    base = dict(symbol="hk00700", market="hk", side="buy", qty=100,
                price=300.0, currency="HKD", decision_id="D-1", signal_id="S-1")
    base.update(kw)
    return OrderRequest(**base)


def test_empty_payload_raises_unknown_state(tmp_path):
    """空订单响应 → 抛 OrderStateUnknown，不得返回 submitted。"""
    b, trade = _broker(tmp_path, resp={"code": 0, "data": {}})
    with pytest.raises(OrderStateUnknown) as ei:
        b.place_order(_req())
    assert ei.value.code == "EMPTY_ORDER_RESPONSE"
    assert ei.value.client_id
    assert trade.calls == 1


def test_missing_order_id_returns_unknown_not_submitted(tmp_path):
    """有响应体但没 orderId → 状态必须是 unknown，且不得编造券商单号。"""
    b, trade = _broker(tmp_path, resp={"code": 0, "data": {"status": "submitted"}})
    r = b.place_order(_req())
    assert r.status == "unknown", "没有 orderId 时不能被当成 submitted"
    assert r.broker_order_id == ""
    assert r.order_id.startswith("UNCONFIRMED-")
    assert not r.order_id.startswith("FS-"), "旧版会编造 FS-HHMMSS，必须已删除"
    assert r.client_id
    assert "orderId" in r.reconcile_note


def test_missing_status_with_order_id_is_unknown(tmp_path):
    b, _ = _broker(tmp_path, resp={"code": 0, "data": {"orderId": "BRK-9"}})
    r = b.place_order(_req())
    assert r.broker_order_id == "BRK-9"
    assert r.status == "unknown", "缺 status 字段不能假定 submitted"


def test_network_error_raises_unknown_with_client_id(tmp_path):
    import requests

    b, trade = _broker(tmp_path, exc=requests.exceptions.ReadTimeout("read timed out"))
    with pytest.raises(OrderStateUnknown) as ei:
        b.place_order(_req())
    assert ei.value.code == "ORDER_STATE_UNKNOWN"
    assert ei.value.client_id
    assert "禁止重试" in str(ei.value)
    assert trade.calls == 1, "下单绝不能自动重试"


def test_auth_error_before_send_is_safe_to_retry(tmp_path):
    """鉴权失败发生在发出委托之前 → 明确告知可安全重试，而不是 unknown。"""
    from fsopenapi import AuthenticationError

    b, trade = _broker(tmp_path, exc=AuthenticationError(code=401, message="session invalid"))
    with pytest.raises(BrokerError) as ei:
        b.place_order(_req())
    assert not isinstance(ei.value, OrderStateUnknown)
    assert "未发出委托" in str(ei.value)
    assert trade.calls == 1


def test_client_id_is_stable_for_same_decision(tmp_path):
    b, _ = _broker(tmp_path, resp={"code": 0, "data": {"orderId": "B1"}})
    r1 = b.place_order(_req())
    r2 = b.place_order(_req())
    assert r1.client_id == r2.client_id, "同一决策必须得到同一 clientId"

    r3 = b.place_order(_req(decision_id="D-2"))
    assert r3.client_id != r1.client_id


def test_derive_client_id_is_deterministic():
    a = derive_client_id("D1", "S1", "hk00700", "buy")
    b = derive_client_id("D1", "S1", "hk00700", "buy")
    c = derive_client_id("D2", "S1", "hk00700", "buy")
    assert a == b and a != c
    assert a.isdigit() and int(a) < 2_000_000_000


# ------------------------------------------------------------------ 引擎层
def _engine(tmp_path, broker, model=None):
    cfg = live_cfg(tmp_path, **{"risk.enforce_market_hours": False})
    journal = AuditJournal(tmp_path / "audit.db", tmp_path / "audit.jsonl")
    risk = RiskController(cfg, cfg.gate)
    engine = StrategyEngine(cfg, broker, model or FixedSignalModel(), risk, journal,
                            stops_path=tmp_path / "stops.json")
    return engine, journal, risk


def test_engine_reconciles_when_broker_already_accepted(tmp_path):
    """网络超时但券商实际已受理 → 对账查到真单，按真实状态落库，不重试下单。"""
    found = OrderResult(order_id="BRK-REAL", broker_order_id="BRK-REAL",
                        symbol="hk00700", market="hk", side="buy", qty=100,
                        price=300.0, status="filled", filled_qty=100,
                        avg_fill_price=300.0, venue="scripted",
                        ts="2026-01-01T00:00:00+08:00", reconcile_note="对账命中")
    broker = ScriptedBroker(place_behaviour="raise", reconcile_result=found)
    engine, journal, _ = _engine(tmp_path, broker)

    report = engine.scan(["hk00700"], now=WEEKDAY_1000)
    o = report.outcomes[0]

    assert o.stage == "ordered", o.detail
    assert broker.calls["place"] == 1, "绝不能重试下单"
    assert broker.calls["reconcile"] == 1
    orders = journal.list_orders(limit=5)
    assert orders[0]["broker_order_id"] == "BRK-REAL"
    assert orders[0]["status"] == "filled"
    assert orders[0]["client_id"]
    events = [e["event"] for e in journal.list_audit(limit=40)]
    assert "order_state_unknown" in events
    assert "order_reconciled" in events


def test_engine_marks_not_accepted_when_broker_has_no_order(tmp_path):
    """对账明确查不到 → not_accepted，且仍然只下过一次单。"""
    broker = ScriptedBroker(place_behaviour="raise", reconcile_result="none")
    engine, journal, _ = _engine(tmp_path, broker)

    report = engine.scan(["hk00700"], now=WEEKDAY_1000)
    assert report.outcomes[0].stage in ("ordered", "rejected")
    assert broker.calls["place"] == 1

    orders = journal.list_orders(limit=5)
    assert orders[0]["status"] == "not_accepted"
    assert "未受理" in (orders[0]["reconcile_note"] or "")
    events = [e["event"] for e in journal.list_audit(limit=40)]
    assert "order_not_accepted" in events


def test_engine_keeps_unknown_when_reconcile_fails(tmp_path):
    """对账本身失败 → 保持 unknown（不能乐观地认为没受理）。"""
    broker = ScriptedBroker(place_behaviour="raise",
                            reconcile_exc=BrokerError("网关不可达", code="NETWORK"))
    engine, journal, risk = _engine(tmp_path, broker)

    report = engine.scan(["hk00700"], now=WEEKDAY_1000)
    assert report.outcomes[0].stage in ("ordered", "rejected")
    orders = journal.list_orders(limit=5)
    assert orders[0]["status"] == "unknown"
    assert "对账失败" in (orders[0]["reconcile_note"] or "")


def test_unresolved_order_blocks_further_orders_on_symbol(tmp_path):
    """存在 unknown 委托时，该标的必须被封锁（禁止盲目重试下单）。"""
    risk = RiskController(live_cfg(tmp_path), live_cfg(tmp_path).gate)
    portfolio = Portfolio(currency="HKD", equity=1_000_000.0, cash=1_000_000.0)
    recent = [{"symbol": "hk00700", "side": "buy", "status": "unknown",
               "order_id": "UNCONFIRMED-123", "client_id": "123"}]
    d = risk.evaluate(OrderIntent(symbol="hk00700", side="buy", qty=100, price=300.0,
                                  market="hk", currency="HKD", stop_loss=290.0),
                      portfolio, last_price=300.0, recent_orders=recent,
                      now=WEEKDAY_1000, reserve_attempt=False)
    assert d.approved is False
    assert "unresolved_order_state" in d.failed_checks
    assert any("对账" in r for r in d.reasons)


def test_not_accepted_order_does_not_block(tmp_path):
    cfg = live_cfg(tmp_path)
    risk = RiskController(cfg, cfg.gate)
    portfolio = Portfolio(currency="HKD", equity=1_000_000.0, cash=1_000_000.0)
    recent = [{"symbol": "hk00700", "side": "buy", "status": "not_accepted",
               "order_id": "UNCONFIRMED-1"}]
    d = risk.evaluate(OrderIntent(symbol="hk00700", side="buy", qty=100, price=300.0,
                                  market="hk", currency="HKD", stop_loss=290.0),
                      portfolio, last_price=300.0, recent_orders=recent,
                      now=WEEKDAY_1000, reserve_attempt=False)
    assert "unresolved_order_state" not in d.failed_checks


def test_monitor_reconciles_unknown_and_unblocks(tmp_path):
    """第二次对账成功 → 订单状态更新，标的解除封锁。"""
    broker = ScriptedBroker(place_behaviour="raise",
                            reconcile_exc=BrokerError("暂时不可达", code="NETWORK"))
    engine, journal, _ = _engine(tmp_path, broker)
    engine.scan(["hk00700"], now=WEEKDAY_1000)
    assert journal.list_orders(limit=1)[0]["status"] == "unknown"

    # 网关恢复
    broker.reconcile_exc = None
    broker.reconcile_result = OrderResult(
        order_id="BRK-LATE", broker_order_id="BRK-LATE", symbol="hk00700",
        market="hk", side="buy", qty=100, price=300.0, status="filled",
        filled_qty=100, venue="scripted", ts="2026-01-01T00:00:02+08:00")
    executed = engine.monitor(now=WEEKDAY_1000)
    assert any(e["kind"] == "order_reconciled" for e in executed), executed
    assert journal.list_orders(limit=1)[0]["status"] == "filled"


def test_monitor_marks_not_accepted_on_second_pass(tmp_path):
    broker = ScriptedBroker(place_behaviour="raise",
                            reconcile_exc=BrokerError("暂时不可达", code="NETWORK"))
    engine, journal, _ = _engine(tmp_path, broker)
    engine.scan(["hk00700"], now=WEEKDAY_1000)

    broker.reconcile_exc = None
    broker.reconcile_result = "none"
    executed = engine.monitor(now=WEEKDAY_1000)
    assert any(e["kind"] == "order_not_accepted" for e in executed), executed
    assert journal.list_orders(limit=1)[0]["status"] == "not_accepted"
