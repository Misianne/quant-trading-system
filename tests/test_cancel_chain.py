"""P1-4 / P1-5 / P1-6：订单超时撤单与撤单完整链路。"""
from __future__ import annotations

from datetime import timedelta

import pytest

from ai_trader.audit.journal import AuditJournal
from ai_trader.broker.base import BrokerError, OrderRequest, OrderResult
from ai_trader.broker.paper import PaperBroker
from ai_trader.risk.controller import RiskController
from ai_trader.strategy.engine import StrategyEngine
from tests.conftest import WEEKDAY_1000, make_cfg
from tests.helpers import FixedSignalModel, ScriptedBroker


def _setup(tmp_path, timeout_s=600, broker=None):
    cfg = make_cfg(tmp_path, **{"risk.order_timeout_seconds": timeout_s,
                                "risk.enforce_market_hours": False})
    journal = AuditJournal(tmp_path / "audit.db", tmp_path / "audit.jsonl")
    br = broker or PaperBroker(state_path=tmp_path / "paper.json")
    risk = RiskController(cfg, cfg.gate)
    engine = StrategyEngine(cfg, br, FixedSignalModel(), risk, journal,
                            stops_path=tmp_path / "stops.json")
    return cfg, journal, br, engine


def _pending_order(paper, journal, age_seconds=700):
    q = paper.get_quote("hk00700")
    r = paper.place_order(OrderRequest(
        symbol="hk00700", market="hk", side="buy", qty=100,
        price=round(q.last * 0.5, 3), order_type="limit", currency="HKD",
        decision_id="D-1", signal_id="S-1", client_id="CID-1"))
    assert r.status == "submitted"
    ts = (WEEKDAY_1000 - timedelta(seconds=age_seconds)).astimezone().isoformat(
        timespec="seconds")
    journal.record_order({**r.as_dict(), "ts": ts, "client_id": "CID-1"})
    return r


# ---------------------------------------------------------------- 超时撤单
def test_stale_limit_order_is_cancelled(tmp_path):
    cfg, journal, paper, engine = _setup(tmp_path, timeout_s=600)
    r = _pending_order(paper, journal, age_seconds=700)

    executed = engine.monitor(now=WEEKDAY_1000)

    assert any(e["kind"] == "order_expired" for e in executed), executed
    row = journal.list_orders(limit=1)[0]
    assert row["status"] == "expired"
    assert "超时" in (row["reconcile_note"] or "")
    # 券商侧也必须真的被撤
    assert paper.get_order(r.order_id).status == "cancelled"
    events = [e["event"] for e in journal.list_audit(limit=20)]
    assert "order_cancelled" in events
    journal.close()


def test_fresh_order_is_not_cancelled(tmp_path):
    cfg, journal, paper, engine = _setup(tmp_path, timeout_s=600)
    _pending_order(paper, journal, age_seconds=10)
    executed = engine.monitor(now=WEEKDAY_1000)
    assert not any(e["kind"] == "order_expired" for e in executed)
    assert journal.list_orders(limit=1)[0]["status"] == "submitted"
    journal.close()


def test_timeout_zero_disables_expiry(tmp_path):
    cfg, journal, paper, engine = _setup(tmp_path, timeout_s=0)
    _pending_order(paper, journal, age_seconds=100000)
    executed = engine.monitor(now=WEEKDAY_1000)
    assert not any(e["kind"] == "order_expired" for e in executed)
    assert journal.list_orders(limit=1)[0]["status"] == "submitted"
    journal.close()


# ---------------------------------------------------------------- 手动撤单
def test_manual_cancel_success(tmp_path):
    cfg, journal, paper, engine = _setup(tmp_path)
    r = _pending_order(paper, journal, age_seconds=10)
    res = engine.cancel_order(r.order_id, reason="单元测试")
    assert res["ok"] is True
    assert res["status"] == "cancelled"
    assert journal.list_orders(limit=1)[0]["status"] == "cancelled"
    journal.close()


def test_cancel_filled_order_rejected(tmp_path):
    cfg, journal, paper, engine = _setup(tmp_path)
    journal.record_order({"order_id": "O-F", "symbol": "hk00700", "side": "buy",
                          "qty": 100, "price": 300.0, "status": "filled",
                          "venue": "paper"})
    res = engine.cancel_order("O-F")
    assert res["ok"] is False
    assert "不可撤销" in res["reason"]
    journal.close()


def test_cancel_unknown_order_id(tmp_path):
    cfg, journal, paper, engine = _setup(tmp_path)
    res = engine.cancel_order("NO-SUCH")
    assert res["ok"] is False
    assert "无此订单" in res["reason"]
    journal.close()


def test_cancel_calls_broker_exactly_once(tmp_path):
    broker = ScriptedBroker()
    cfg, journal, _, engine = _setup(tmp_path, broker=broker)
    journal.record_order({"order_id": "O-1", "symbol": "hk00700", "side": "buy",
                          "qty": 100, "price": 300.0, "status": "submitted",
                          "broker_order_id": "BRK-1", "client_id": "CID-1",
                          "venue": "scripted"})
    res = engine.cancel_order("O-1")
    assert res["ok"] is True
    assert broker.calls["cancel"] == 1
    journal.close()


def test_cancel_unknown_order_reconciles_first(tmp_path):
    """本地未确认的委托：必须先对账拿真实单号；查不到就不发撤单。"""
    broker = ScriptedBroker(reconcile_result="none")
    cfg, journal, _, engine = _setup(tmp_path, broker=broker)
    journal.record_order({"order_id": "UNCONFIRMED-9", "symbol": "hk00700",
                          "side": "buy", "qty": 100, "price": 300.0,
                          "status": "unknown", "broker_order_id": "",
                          "client_id": "9", "venue": "scripted"})
    res = engine.cancel_order("UNCONFIRMED-9")
    assert res["ok"] is False
    assert "无需撤单" in res["reason"]
    assert broker.calls["reconcile"] == 1
    assert broker.calls["cancel"] == 0, "对账确认无此单时不该发撤单请求"
    assert journal.list_orders(limit=1)[0]["status"] == "not_accepted"
    journal.close()


def test_cancel_broker_error_is_reported_not_swallowed(tmp_path):
    broker = ScriptedBroker(cancel_exc=BrokerError("网关 502", code="UPSTREAM",
                                                   retryable=True))
    cfg, journal, _, engine = _setup(tmp_path, broker=broker)
    journal.record_order({"order_id": "O-2", "symbol": "hk00700", "side": "buy",
                          "qty": 100, "price": 300.0, "status": "submitted",
                          "broker_order_id": "BRK-2", "venue": "scripted"})
    res = engine.cancel_order("O-2")
    assert res["ok"] is False
    assert res["retryable"] is True
    assert journal.list_orders(limit=1)[0]["status"] == "submitted", "撤单失败不得改状态"
    events = [e["event"] for e in journal.list_audit(limit=20)]
    assert "cancel_failed" in events
    journal.close()


# ---------------------------------------------------------------- Web / CLI
def test_web_cancel_endpoint(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from ai_trader.runtime import build_runtime
    from ai_trader.web.app import create_app

    monkeypatch.delenv("WEB_ADMIN_PASSWORD", raising=False)
    cfg = make_cfg(tmp_path, **{"risk.order_timeout_seconds": 600,
                                "risk.enforce_market_hours": False})
    rt = build_runtime(cfg)
    client = TestClient(create_app(rt))

    q = rt.broker.get_quote("hk00700")
    r = rt.broker.place_order(OrderRequest(
        symbol="hk00700", market="hk", side="buy", qty=100,
        price=round(q.last * 0.5, 3), order_type="limit", currency="HKD"))
    rt.journal.record_order({**r.as_dict(), "client_id": "CID-W", "ts": "2026-11-04T10:00:00+08:00"})

    resp = client.post(f"/api/orders/{r.order_id}/cancel", data={"reason": "web 测试"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    again = client.post(f"/api/orders/{r.order_id}/cancel", data={"reason": "重复"})
    assert again.status_code == 409
