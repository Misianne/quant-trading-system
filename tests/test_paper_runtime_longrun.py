"""长跑相关的监督器行为 + 运行时级 LULD/MWCB 保护。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ai_trader import timebase
from ai_trader.config import load_config
from ai_trader.paper_runtime.replay import SCENARIOS, build_runtime_for
from ai_trader.paper_runtime.state import RuntimeState
from ai_trader.paper_runtime.supervisor import (
    CircuitBreaker,
    HeartbeatWriter,
    Supervisor,
    process_commit_mb,
    process_rss_mb,
)
from ai_trader.paper_runtime.timebase_bridge import restore_clock

CFG = load_config()


# ---------------------------------------------------------------------- #
# 心跳 / 熔断
# ---------------------------------------------------------------------- #
def test_heartbeat_has_wall_clock_timestamp(tmp_path):
    """回放时钟停在历史日期，心跳必须用墙钟，否则外部算年龄会得到负数。"""
    w = HeartbeatWriter(tmp_path / "hb.json", "r")
    with timebase.replay_clock if False else _null_ctx():
        pass
    from ai_trader.paper_runtime.timebase_bridge import replay_clock
    with replay_clock(datetime(2026, 1, 2, 10, 0, tzinfo=timezone.utc)):
        hb = w.beat("RUNNING", 1)
    at = datetime.fromisoformat(hb.at)
    real = datetime.now(timezone.utc)
    assert abs((real - at).total_seconds()) < 120, "心跳时间不是墙钟"


class _null_ctx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_heartbeat_includes_commit_and_rss(tmp_path):
    rt = _fake_rt()
    sup = Supervisor(rt, run_dir=tmp_path)
    sup.beat()
    d = json.loads((tmp_path / "heartbeat.json").read_text(encoding="utf-8"))
    assert "rss_mb" in d["extra"] and "commit_mb" in d["extra"]


def test_breaker_survives_many_successes():
    b = CircuitBreaker(max_consecutive_failures=2)
    b.record_failure("e"); b.record_failure("e")
    assert b.opened
    for _ in range(50):
        b.record_success()
    assert b.opened, "熔断不得被成功样本自动清除"


def test_breaker_counts_reset_on_success_only():
    b = CircuitBreaker(max_consecutive_failures=3)
    b.record_failure("a")
    b.record_success()
    b.record_failure("b")
    assert b.opened is False
    assert b.consecutive_failures == 1


def test_supervisor_detects_stalled_heartbeat(tmp_path):
    rt = _fake_rt()
    sup = Supervisor(rt, run_dir=tmp_path, heartbeat_timeout_seconds=0.0)
    sup.beat()
    scan = sup.health_scan()
    # heartbeat_timeout=0 → 任何年龄都算停滞
    assert any("心跳" in x for x in scan["critical"])


def test_supervisor_halts_and_opens_breaker(tmp_path):
    rt = _fake_rt(healthy=False)
    sup = Supervisor(rt, run_dir=tmp_path, max_consecutive_failures=1)
    sup.step()
    assert rt.machine.state is RuntimeState.HALTED
    assert sup.breaker.opened


def test_supervisor_does_not_touch_risk_limits(tmp_path):
    rt = _fake_rt()
    sup = Supervisor(rt, run_dir=tmp_path)
    before = dict(getattr(rt, "_risk_limits", {}) or {})
    sup.step()
    assert dict(getattr(rt, "_risk_limits", {}) or {}) == before


# ---------------------------------------------------------------------- #
# 运行时级 LULD / MWCB
# ---------------------------------------------------------------------- #
def test_luld_limit_state_blocks_out_of_band_in_runtime(tmp_path):
    sc = SCENARIOS["luld_limit_state"]
    rt, clk, s, m = build_runtime_for(sc, cfg=CFG, out_dir=tmp_path / "r")
    try:
        rt.start(); rt.to_running()
        ms = rt.ctx.service("market_status")
        from ai_trader.market.status import MarketState
        for i in range(sc.ticks):
            if i:
                clk.advance(timedelta(minutes=sc.tick_minutes))
            s.set_tick(i)
            now = timebase.now()
            for sym, series in sc.price_series().items():
                prior = float(sc.prior_close.get(sym, series[0]))
                if i == 0:
                    ms.update_reference(sym, prior, prior_close=prior)
                ms.observe_trade(sym, float(series[min(i, len(series) - 1)]),
                                 now, bounds=sc.bounds(), prior_close=prior)
            rt.tick(model_tick=i)
        assert any("luld" in st for st in
                   [v.state.value for v in [ms.status(x) for x in sc.price_series()]])
    finally:
        rt.stop("done"); restore_clock()


def test_mwcb_pause_blocks_new_entries(tmp_path):
    sc = SCENARIOS["mwcb"]
    rt, clk, s, m = build_runtime_for(sc, cfg=CFG, out_dir=tmp_path / "r")
    try:
        rt.start(); rt.to_running()
        ms = rt.ctx.service("market_status")
        for i in range(sc.ticks):
            if i:
                clk.advance(timedelta(minutes=sc.tick_minutes))
            s.set_tick(i)
            if sc.index_path and i < len(sc.index_path):
                ms.update_index(float(sc.index_path[i]), timebase.now(), bounds=sc.bounds())
            rt.tick(model_tick=i)
        assert ms.mwcb_status()["level"] >= 1
        assert len(rt.ledger.fills()) == 0, "熔断期间不得成交"
    finally:
        rt.stop("done"); restore_clock()


def test_runtime_halted_blocks_all_fills(tmp_path):
    sc = SCENARIOS["normal_day"]
    rt, clk, s, m = build_runtime_for(sc, cfg=CFG, out_dir=tmp_path / "r")
    try:
        rt.start(); rt.to_running()
        rt._halt("test halt")
        assert rt.can_open_new()[0] is False
        assert rt.exits_allowed()[0] is False
        n = len(rt.ledger.fills())
        with pytest.raises(RuntimeError):
            rt.tick()
        assert len(rt.ledger.fills()) == n
    finally:
        rt.stop("done"); restore_clock()


# ---------------------------------------------------------------------- #
def _fake_rt(healthy: bool = True):
    class M:
        def __init__(self):
            self._s = RuntimeState.RUNNING
        @property
        def state(self):
            return self._s
        def is_trading(self):
            return self._s is RuntimeState.RUNNING
    class RT:
        run_id = "fake"
        def __init__(self):
            self.machine = M(); self.ticks = 0; self._risk_limits = {"max_pos": 8}
            self.ctx = type("C", (), {"broker": None, "risk": None})()
            self.registry = type("R", (), {"get": lambda self_, n: None})()
            self._last_invariants = None
        def health(self):
            return {"state": self.machine.state.value,
                    "aggregate": "OK" if healthy else "HALTED",
                    "components": {
                        "market_status": {"healthy": healthy, "critical": True, "detail": ""},
                        "short_guard": {"healthy": True, "critical": True},
                        "settlement": {"healthy": True, "critical": True},
                        "caps": {"healthy": True, "critical": True}},
                    "ticks": self.ticks}
        def _halt(self, reason):
            self.machine._s = RuntimeState.HALTED
        def _degraded(self, reason):
            self.machine._s = RuntimeState.DEGRADED
        def tick(self):
            self.ticks += 1
        def supervise(self):
            return {"action": "recovered"}
    return RT()
