"""Runtime 复用会话：单账户连续多段，不重建对象。"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from ai_trader.config import load_config
from ai_trader.paper_runtime.replay import ReplayQuoteSource
from ai_trader.paper_runtime.session import ReplaySession, build_segment

CFG = load_config()


def make(tmp_path: Path) -> ReplaySession:
    return ReplaySession(cfg=CFG, out_dir=tmp_path)


def test_build_segment_is_deterministic():
    a = build_segment(7, date(2026, 11, 20))
    b = build_segment(7, date(2026, 11, 20))
    assert a.prices == b.prices and a.script[0].action == b.script[0].action


def test_build_segment_prices_on_tick_grid():
    for i in range(1, 40):
        seg = build_segment(i, date(2026, 11, 20))
        for series in seg.prices.values():
            for px in series:
                assert round(px, 2) == pytest.approx(px), "价格必须是 0.01 的整数倍"


def test_session_start_creates_one_runtime(tmp_path):
    s = make(tmp_path)
    r = s.start()
    assert r.ok
    assert s.rt is not None
    s.stop()


def test_session_reuses_same_runtime_object(tmp_path):
    s = make(tmp_path)
    s.start()
    rt_before = id(s.rt)
    for _ in range(5):
        s.run_segment()
    assert id(s.rt) == rt_before
    s.stop()


def test_session_segments_advance_clock_forward(tmp_path):
    s = make(tmp_path)
    s.start()
    dates = []
    for _ in range(10):
        s.run_segment()
        dates.append(s.rt._clock.now())
    assert dates == sorted(dates), "回放时钟只能向前"
    s.stop()


def test_session_skips_weekends(tmp_path):
    s = make(tmp_path)
    s.start()
    days = set()
    for _ in range(40):
        s.run_segment()
        days.add(s.rt._clock.now().date())
    assert all(d.weekday() < 5 for d in days), f"出现周末：{[d for d in days if d.weekday()>=5]}"
    s.stop()


def test_session_account_is_continuous(tmp_path):
    s = make(tmp_path)
    s.start()
    for _ in range(10):
        s.run_segment()
    broker = s.rt.ctx.service("broker")
    assert len(broker.orders_snapshot()) > 0
    assert s.rt._ticks > 0
    s.stop()


def test_session_fills_accumulate_in_one_ledger(tmp_path):
    s = make(tmp_path)
    s.start()
    for _ in range(14):
        s.run_segment()
    n = len(s.rt.ledger.fills())
    assert n >= 1
    assert s.rt.ledger.stats()["fills"] == n
    s.stop()


def test_session_source_reload_in_place():
    src = ReplayQuoteSource(prices={"USAAPL": [10.0]})
    obj = id(src)
    src.load(prices={"USAAPL": [20.0, 21.0]}, prev_close={"USAAPL": 20.0})
    assert id(src) == obj
    assert src.prices["USAAPL"] == [20.0, 21.0]
    src.set_tick(1)
    assert src._price("USAAPL") == 21.0


def test_session_model_reload_in_place():
    from ai_trader.paper_runtime.model import ScriptStep, ScriptedModel
    m = ScriptedModel([ScriptStep(1, "usAAPL", "buy")])
    obj = id(m)
    m.load([ScriptStep(2, "usAAPL", "sell")])
    assert id(m) == obj
    assert (2, "USAAPL") in m._by_key


def test_session_stop_releases_lock(tmp_path):
    s = make(tmp_path)
    s.start()
    lock = tmp_path / "paper_runtime.lock"
    assert lock.exists()
    s.stop()
    assert not lock.exists()


def test_session_invariants_hold_after_many_segments(tmp_path):
    s = make(tmp_path)
    s.start()
    for _ in range(20):
        s.run_segment()
    assert s.rt._last_invariants.ok, [r.name for r in s.rt._last_invariants.critical_failures]
    s.stop()


def test_session_summary_shape(tmp_path):
    s = make(tmp_path)
    s.start()
    s.run_segment()
    d = s.summary()
    assert {"run_id", "segments", "ticks", "fills", "state", "cash", "health"} <= set(d)
    s.stop()
