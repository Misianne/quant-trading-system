"""测试：审计日志与哈希链防篡改。"""
from __future__ import annotations

import json

import pytest

from ai_trader.audit.journal import AuditJournal, utcnow_iso


def test_audit_chain_is_valid_on_fresh_journal(journal):
    journal.log("test_event", {"a": 1})
    journal.log("test_event", {"a": 2})
    res = journal.verify_chain()
    assert res["ok"] is True
    assert res["total"] == 2


def test_audit_chain_detects_tampering(journal):
    for i in range(5):
        journal.log("evt", {"i": i})
    assert journal.verify_chain()["ok"] is True

    # 直接改库里的 detail，模拟篡改
    journal._conn.execute(
        "UPDATE audit_log SET detail = ? WHERE seq = 3",
        ('{"i":999}',),
    )
    journal._conn.commit()

    res = journal.verify_chain()
    assert res["ok"] is False
    assert res["broken_at_seq"] == 3


def test_audit_jsonl_is_appended(tmp_path):
    p = tmp_path / "a.jsonl"
    j = AuditJournal(tmp_path / "a.db", p)
    j.log("one", {"x": 1})
    j.log("two", {"x": 2})
    lines = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert [x["event"] for x in lines] == ["one", "two"]
    assert lines[1]["prev_hash"] == lines[0]["hash"]
    j.close()


def test_audit_redacts_sensitive_fields(journal):
    journal.log("creds", {
        "api_key": "sk-super-secret-value",
        "password": "hunter2",
        "server_public_key": "-----BEGIN PUBLIC KEY-----xxxx",
        "symbol": "hk00700",
        "qty": 100,
    })
    row = journal.list_audit(limit=1)[0]
    detail = row["detail"]
    assert detail["api_key"] == "****"
    assert detail["password"] == "****"
    assert detail["server_public_key"] == "****"
    assert detail["symbol"] == "hk00700"
    assert detail["qty"] == 100
    blob = json.dumps(detail, ensure_ascii=False)
    assert "sk-super-secret-value" not in blob
    assert "hunter2" not in blob


def test_record_signal_and_order(journal):
    sid = journal.record_signal({"symbol": "hk00700", "action": "buy", "confidence": 0.8,
                                 "entry_price": 300.0, "stop_loss": 290.0})
    assert sid
    journal.record_order({"order_id": "O1", "symbol": "hk00700", "side": "buy",
                          "qty": 100, "price": 300.0, "status": "filled",
                          "filled_qty": 100, "avg_fill_price": 300.1, "commission": 3.0,
                          "signal_id": sid})
    orders = journal.list_orders()
    assert orders[0]["order_id"] == "O1"
    counts = journal.counts()
    assert counts["signals"] == 1 and counts["orders"] == 1


def test_record_risk_decision_stores_reasons(journal):
    journal.record_risk_decision({
        "decision_id": "D1", "symbol": "hk00700", "action": "buy",
        "approved": False, "reasons": ["仓位超限"], "requested_qty": 100,
        "approved_qty": 0,
    })
    d = journal.list_risk_decisions()[0]
    assert d["approved"] == 0
    assert json.loads(d["reasons"]) == ["仓位超限"]


def test_orders_since_filters_by_time(journal):
    journal.record_order({"order_id": "OLD", "symbol": "hk00700", "side": "buy",
                          "qty": 1, "price": 1.0, "status": "filled",
                          "ts": "2000-01-01T00:00:00+08:00"})
    journal.record_order({"order_id": "NEW", "symbol": "hk00700", "side": "buy",
                          "qty": 1, "price": 1.0, "status": "filled"})
    recent = journal.orders_since("2020-01-01T00:00:00+08:00")
    assert [o["order_id"] for o in recent] == ["NEW"]


def test_equity_snapshot(journal):
    journal.record_equity("HKD", 900_000, 120_000, 1_020_000, 20_000)
    assert journal.counts()["equity_snapshots"] == 1
