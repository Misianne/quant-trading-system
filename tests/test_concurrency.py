"""P1-5：并发下单（两个 worker 同时提交相同订单）。

审计场景："两个 worker 同时提交相同订单"。
这里用两种并发形态验证：

1. **同进程多线程、各自独立连接** —— 逼近两个进程同时写同一 SQLite
2. **同进程多线程、共享同一 journal** —— 逼近线程池 / Web 与 worker 并发
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

import pytest

from ai_trader.audit.journal import AuditJournal
from ai_trader.broker.paper import PaperBroker
from ai_trader.risk.controller import OrderIntent, Portfolio, RiskController
from ai_trader.strategy.engine import StrategyEngine
from tests.conftest import WEEKDAY_1000, make_cfg
from tests.helpers import FixedSignalModel


def _intent(symbol="hk00700", side="buy", qty=100):
    return OrderIntent(symbol=symbol, side=side, qty=qty, price=300.0,
                       market="hk", currency="HKD", stop_loss=290.0)


def _pf():
    return Portfolio(currency="HKD", equity=1_000_000.0, cash=1_000_000.0)


# ---------------------------------------------------------------- 数据库层
def test_same_slot_only_one_wins_across_connections(tmp_path):
    db = tmp_path / "audit.db"
    j1 = AuditJournal(db, None)
    j2 = AuditJournal(db, None)
    barrier = threading.Barrier(2)
    results: list[bool] = []
    lock = threading.Lock()

    def worker(journal, decision_id):
        barrier.wait(timeout=5)
        ok = journal.reserve_order_attempt("hk00700", "buy", 300, decision_id,
                                           now=WEEKDAY_1000)
        with lock:
            results.append(ok)

    t1 = threading.Thread(target=worker, args=(j1, "D-A"))
    t2 = threading.Thread(target=worker, args=(j2, "D-B"))
    t1.start(); t2.start(); t1.join(10); t2.join(10)

    assert sorted(results) == [False, True], results
    j1.close(); j2.close()


def test_different_side_does_not_conflict(tmp_path):
    j = AuditJournal(tmp_path / "a.db", None)
    assert j.reserve_order_attempt("hk00700", "buy", 300, "D1", WEEKDAY_1000) is True
    assert j.reserve_order_attempt("hk00700", "sell", 300, "D2", WEEKDAY_1000) is True
    j.close()


def test_slot_released_after_window(tmp_path):
    j = AuditJournal(tmp_path / "a.db", None)
    assert j.reserve_order_attempt("hk00700", "buy", 300, "D1", WEEKDAY_1000) is True
    later = WEEKDAY_1000 + timedelta(seconds=301)
    assert j.reserve_order_attempt("hk00700", "buy", 300, "D2", later) is True
    j.close()


def test_release_allows_immediate_retry(tmp_path):
    j = AuditJournal(tmp_path / "a.db", None)
    assert j.reserve_order_attempt("hk00700", "buy", 300, "D1", WEEKDAY_1000) is True
    assert j.reserve_order_attempt("hk00700", "buy", 300, "D2", WEEKDAY_1000) is False
    assert j.release_order_attempt("D1") is True
    assert j.reserve_order_attempt("hk00700", "buy", 300, "D3", WEEKDAY_1000) is True
    j.close()


# ---------------------------------------------------------------- 风控层
def test_two_risk_controllers_one_approval(tmp_path):
    cfg = make_cfg(tmp_path, **{"risk.enforce_market_hours": False})
    j1 = AuditJournal(tmp_path / "audit.db", None)
    j2 = AuditJournal(tmp_path / "audit.db", None)
    r1 = RiskController(cfg, cfg.gate)
    r2 = RiskController(cfg, cfg.gate)
    barrier = threading.Barrier(2)
    approved: list[bool] = []
    lock = threading.Lock()

    def run(risk, journal):
        barrier.wait(timeout=5)
        d = risk.evaluate(_intent(), _pf(), last_price=300.0, now=WEEKDAY_1000,
                          journal=journal)
        with lock:
            approved.append(d.approved)

    t1 = threading.Thread(target=run, args=(r1, j1))
    t2 = threading.Thread(target=run, args=(r2, j2))
    t1.start(); t2.start(); t1.join(10); t2.join(10)

    assert approved.count(True) == 1, f"应恰好一单通过，实际 {approved}"
    assert approved.count(False) == 1
    j1.close(); j2.close()


# ---------------------------------------------------------------- 引擎层
def test_two_workers_place_only_one_order(tmp_path):
    """两个 worker 各自持有券商与风控，共享同一审计库 —— 只能成交一笔。"""
    cfg = make_cfg(tmp_path, **{"risk.enforce_market_hours": False})
    journal_a = AuditJournal(tmp_path / "audit.db", tmp_path / "audit.jsonl")
    journal_b = AuditJournal(tmp_path / "audit.db", tmp_path / "audit.jsonl")

    paper_a = PaperBroker(state_path=tmp_path / "paper_a.json")
    paper_b = PaperBroker(state_path=tmp_path / "paper_b.json")

    eng_a = StrategyEngine(cfg, paper_a, FixedSignalModel(), RiskController(cfg, cfg.gate),
                           journal_a, stops_path=tmp_path / "stops_a.json")
    eng_b = StrategyEngine(cfg, paper_b, FixedSignalModel(), RiskController(cfg, cfg.gate),
                           journal_b, stops_path=tmp_path / "stops_b.json")

    barrier = threading.Barrier(2)
    reports = {}

    def run(name, engine):
        barrier.wait(timeout=5)
        try:
            reports[name] = engine.scan(["hk00700"], now=WEEKDAY_1000)
        except Exception as exc:  # noqa: BLE001
            reports[name] = exc

    t1 = threading.Thread(target=run, args=("a", eng_a))
    t2 = threading.Thread(target=run, args=("b", eng_b))
    t1.start(); t2.start(); t1.join(20); t2.join(20)

    orders_placed = (len(paper_a.list_orders(limit=10))
                     + len(paper_b.list_orders(limit=10)))
    assert orders_placed == 1, f"并发下只应产生 1 笔委托，实际 {orders_placed}"

    stages = [r.outcomes[0].stage for r in reports.values()
              if hasattr(r, "outcomes")]
    assert stages.count("ordered") == 1, stages
    assert stages.count("rejected") == 1, stages

    journal_a.close(); journal_b.close()


def test_sequential_duplicate_after_success_is_blocked(tmp_path):
    """回归：单 worker 连续两次扫描同一标的，第二次必须被冷却拦住。"""
    cfg = make_cfg(tmp_path, **{"risk.enforce_market_hours": False})
    journal = AuditJournal(tmp_path / "audit.db", tmp_path / "audit.jsonl")
    paper = PaperBroker(state_path=tmp_path / "paper.json")
    engine = StrategyEngine(cfg, paper, FixedSignalModel(), RiskController(cfg, cfg.gate),
                            journal, stops_path=tmp_path / "stops.json")

    first = engine.scan(["hk00700"], now=WEEKDAY_1000)
    assert first.outcomes[0].stage == "ordered"
    second = engine.scan(["hk00700"], now=WEEKDAY_1000)
    assert second.outcomes[0].stage == "rejected"
    assert any(c["name"] == "duplicate_order" for c in second.outcomes[0].decision["checks"])
    assert len(paper.list_orders(limit=10)) == 1
    journal.close()
