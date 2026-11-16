"""数据库增长、原子写入、WAL 与恢复一致性压力测试。"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from ai_trader.broker.paper import PaperBroker
from ai_trader.config import load_config
from ai_trader.paper_runtime.ledger import FillLedger, fills_from_orders
from ai_trader.paper_runtime.recovery import scan as recovery_scan
from ai_trader.paper_runtime.replay import SCENARIOS, build_runtime_for
from ai_trader.paper_runtime.runtime import _rotate_log
from ai_trader.paper_runtime.timebase_bridge import restore_clock

CFG = load_config()


# ---------------------------------------------------------------------- #
# 原子写入 / 损坏保护
# ---------------------------------------------------------------------- #
def test_state_write_is_atomic(tmp_path):
    b = PaperBroker(state_path=tmp_path / "s.json", initial_cash={"USD": 1000.0})
    b._save()
    assert not (tmp_path / "s.json.tmp").exists(), "临时文件必须已被 replace 掉"
    assert json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))["cash"]["USD"] == 1000.0


def test_corrupt_state_refuses_to_start_and_quarantines(tmp_path):
    p = tmp_path / "s.json"
    p.write_text('{"cash": {"USD": 10', encoding="utf-8")     # 截断
    with pytest.raises(RuntimeError, match="状态文件损坏"):
        PaperBroker(state_path=p, initial_cash={"USD": 1000.0})
    assert (tmp_path / "s.json.corrupt").exists(), "损坏文件必须被隔离保留"
    assert not p.exists()


def test_corrupt_state_never_resets_balance(tmp_path):
    """归零是最危险的"恢复"；必须拒绝启动而不是给出一个 0 元账户。"""
    p = tmp_path / "s.json"
    p.write_text("not-json-at-all", encoding="utf-8")
    with pytest.raises(RuntimeError):
        PaperBroker(state_path=p, initial_cash={"USD": 5000.0})


def test_missing_state_creates_fresh_account(tmp_path):
    b = PaperBroker(state_path=tmp_path / "new.json", initial_cash={"USD": 777.0})
    assert b._state["cash"]["USD"] == 777.0


# ---------------------------------------------------------------------- #
# WAL / 事务
# ---------------------------------------------------------------------- #
def test_wal_mode_enabled(tmp_path):
    p = tmp_path / "a.db"
    from ai_trader.audit.journal import AuditJournal
    j = AuditJournal(p, tmp_path / "a.jsonl")
    try:
        mode = j._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(mode).lower() == "wal"
    finally:
        j.close()


def test_journal_close_checkpoints(tmp_path):
    from ai_trader.audit.journal import AuditJournal
    p = tmp_path / "a.db"
    j = AuditJournal(p, tmp_path / "a.jsonl")
    j.log("t", {"x": 1})
    j.close()
    wal = p.with_name(p.name + "-wal")
    assert (not wal.exists()) or wal.stat().st_size < 128 * 1024


def test_orders_since_uses_normalized_epoch(tmp_path):
    """混用 UTC / 本地偏移时，字符串比较会把昨天的单算成今天。"""
    from ai_trader.audit.journal import AuditJournal
    j = AuditJournal(tmp_path / "a.db", tmp_path / "a.jsonl")
    try:
        j.record_order({"order_id": "o1", "ts": "2026-11-17T20:15:00-05:00",
                        "symbol": "USAAPL", "side": "buy", "qty": 1, "status": "filled"})
        # 该时刻 = 2026-11-18T01:15Z；对 11-18 00:00-05:00(=05:00Z) 而言是"昨天"
        rows = j.orders_since("2026-11-18T00:00:00-05:00")
        assert rows == [], "跨时区比较错误：昨天的订单被算进今天"
        rows2 = j.orders_since("2026-11-17T00:00:00-05:00")
        assert len(rows2) == 1
    finally:
        j.close()


# ---------------------------------------------------------------------- #
# 增长与恢复
# ---------------------------------------------------------------------- #
def test_fill_ledger_appends_only(tmp_path):
    led = FillLedger(tmp_path / "f.jsonl")
    from ai_trader.paper_runtime.ledger import Fill
    for i in range(50):
        led.post(Fill(f"F{i}", f"O{i}", "USAAPL", "buy", 1, 100.0, 0.0,
                      "USD", "2026-11-16T10:00:00-05:00"))
    assert len((tmp_path / "f.jsonl").read_text(encoding="utf-8").splitlines()) == 50


def test_recovery_after_many_fills_is_unique(tmp_path):
    led = FillLedger(tmp_path / "f.jsonl")
    orders = {}
    for i in range(30):
        orders[str(i)] = {"order_id": str(i), "status": "filled", "filled_qty": 1,
                          "avg_fill_price": 100.0, "symbol": "USAAPL", "side": "buy",
                          "commission": 0, "currency": "USD", "fill_id": f"F{i}"}
    broker = type("B", (), {"orders_snapshot": lambda self: list(orders.values()),
                            "_state": {"cash": {"USD": 100000 - 3000},
                                       "positions": {"USAAPL": {"qty": 30}}}})()
    r1 = recovery_scan(broker, led)
    r2 = recovery_scan(broker, led)
    assert r1.backfilled == 30 and r2.backfilled == 0
    assert len(led.fills()) == 30
    assert len({f.fill_id for f in led.fills()}) == 30


def test_state_and_ledger_agree_after_reload(tmp_path):
    sc = SCENARIOS["normal_day"]
    rt, clk, s, m = build_runtime_for(sc, cfg=CFG, out_dir=tmp_path / "r")
    rt.start(); rt.to_running()
    for i in range(sc.ticks):
        rt.tick(model_tick=i)
    rt.stop("done"); restore_clock()

    orders = json.loads((tmp_path / "r" / "paper_state.json").read_text(encoding="utf-8"))
    led = FillLedger(tmp_path / "r" / "paper_fills.jsonl")
    led.load()
    broker_fills = {f.fill_id for f in fills_from_orders(orders["orders"].values())}
    assert broker_fills == {f.fill_id for f in led.fills()}


def test_log_rotation_moves_files(tmp_path):
    p = tmp_path / "runtime.jsonl"
    p.write_text("x" * 2048, encoding="utf-8")
    _rotate_log(p, max_bytes=1024)
    assert not p.exists()
    assert (tmp_path / "runtime.jsonl.1").exists()


def test_log_rotation_keeps_history(tmp_path):
    p = tmp_path / "runtime.jsonl"
    for i in range(3):
        p.write_text("x" * 2048, encoding="utf-8")
        _rotate_log(p, max_bytes=1024)
    assert (tmp_path / "runtime.jsonl.1").exists()
    assert (tmp_path / "runtime.jsonl.2").exists() or (tmp_path / "runtime.jsonl.3").exists()


def test_log_rotation_noop_below_threshold(tmp_path):
    p = tmp_path / "runtime.jsonl"
    p.write_text("small", encoding="utf-8")
    _rotate_log(p, max_bytes=1024)
    assert p.exists() and p.read_text(encoding="utf-8") == "small"


def test_disk_unavailable_fails_closed(tmp_path):
    """数据目录不可用时必须 FAILED，而不是"假装在跑"。"""
    from ai_trader.paper_runtime.state import RuntimeState
    sc = SCENARIOS["mwcb"]
    rt, clk, s, m = build_runtime_for(sc, cfg=CFG, out_dir=tmp_path / "r")
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    # 让存储组件指向一个不可用的路径（父级是文件）
    rt.registry.get("storage").db_path = blocker / "sub" / "a.db"
    try:
        r = rt.start()
        assert r.ok is False
        assert rt.machine.state in (RuntimeState.FAILED, RuntimeState.STOPPED)
    finally:
        try:
            rt.stop("cleanup")
        except Exception:
            pass
        restore_clock()
