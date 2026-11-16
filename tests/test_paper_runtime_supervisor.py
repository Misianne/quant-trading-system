"""Supervisor 测试：心跳、熔断、恢复预算、失去联系判定。"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ai_trader import timebase
from ai_trader.paper_runtime.state import RuntimeState, StateMachine
from ai_trader.paper_runtime.supervisor import (
    CircuitBreaker,
    CpuMeter,
    HeartbeatWriter,
    Supervisor,
    heartbeat_age_seconds,
    pid_alive,
    process_rss_mb,
    read_heartbeat,
)


class FakeMachine:
    def __init__(self, state=RuntimeState.RUNNING):
        self._s = state
        self.is_trading = lambda: self._s is RuntimeState.RUNNING

    @property
    def state(self):
        return self._s


class FakeRuntime:
    run_id = "run-test"

    def __init__(self, healthy=True, state=RuntimeState.RUNNING):
        self.machine = FakeMachine(state)
        self.halt_reasons = []
        self.degrade_reasons = []
        self.ticks = 0
        self.ctx = type("C", (), {"broker": None, "risk": None})()
        self.registry = type("R", (), {"get": lambda self_, n: None})()
        self._last_invariants = None
        self.supervise_called = 0
        self.supervise_result = {"action": "recovered"}
        self._healthy = healthy

    def health(self):
        return {"run_id": self.run_id, "state": self.machine.state.value,
                "aggregate": "OK" if self._healthy else "HALTED",
                "components": {
                    "market_status": {"healthy": self._healthy, "critical": True,
                                      "detail": "" if self._healthy else "坏了"},
                    "short_guard": {"healthy": True, "critical": True, "detail": ""},
                    "settlement": {"healthy": True, "critical": True, "detail": ""},
                    "caps": {"healthy": True, "critical": True, "detail": ""}},
                "ticks": self.ticks}

    def _halt(self, reason):
        self.halt_reasons.append(reason)
        self.machine._s = RuntimeState.HALTED

    def _degraded(self, reason):
        self.degrade_reasons.append(reason)
        self.machine._s = RuntimeState.DEGRADED

    def tick(self):
        self.ticks += 1

    def supervise(self):
        self.supervise_called += 1
        return self.supervise_result


# ---------------------------------------------------------------------- #
def test_heartbeat_write_and_read(tmp_path):
    w = HeartbeatWriter(tmp_path / "hb.json", "run-1")
    hb = w.beat("RUNNING", 5, {"x": 1})
    assert hb.seq == 1 and hb.pid == os.getpid()
    raw = read_heartbeat(tmp_path / "hb.json")
    assert raw["run_id"] == "run-1" and raw["ticks"] == 5
    assert raw["extra"] == {"x": 1}


def test_heartbeat_seq_increments(tmp_path):
    w = HeartbeatWriter(tmp_path / "hb.json", "r")
    w.beat("A", 1)
    w.beat("B", 2)
    assert read_heartbeat(tmp_path / "hb.json")["seq"] == 2


def test_heartbeat_age_parsing(tmp_path):
    with_clock = timebase.FrozenClock(datetime(2026, 11, 16, 10, 0, tzinfo=timezone.utc))
    hb = {"at": "2026-11-16T10:00:00+00:00"}
    assert heartbeat_age_seconds(hb, now=with_clock.now()) == 0.0
    hb2 = {"at": "2026-11-16T09:59:00+00:00"}
    assert heartbeat_age_seconds(hb2, now=with_clock.now()) == 60.0


def test_heartbeat_age_invalid_is_inf():
    assert heartbeat_age_seconds({"at": "not-a-time"}) == float("inf")
    assert heartbeat_age_seconds({}) == float("inf")


def test_pid_alive_self():
    assert pid_alive(os.getpid()) is True


def test_pid_alive_bogus():
    assert pid_alive(999999) is False
    assert pid_alive(0) is False


def test_rss_and_cpu_are_real_numbers():
    assert process_rss_mb() > 0
    m = CpuMeter()
    time.sleep(0.01)
    pct = m.sample()
    assert pct >= 0.0


def test_breaker_opens_after_threshold():
    b = CircuitBreaker(max_consecutive_failures=3)
    assert b.record_failure("e1") is False
    assert b.record_failure("e2") is False
    assert b.record_failure("e3") is True
    assert b.opened and b.trips == 1


def test_breaker_not_cleared_by_success():
    b = CircuitBreaker(max_consecutive_failures=2)
    b.record_failure("e1"); b.record_failure("e2")
    assert b.opened
    b.record_success()
    assert b.opened, "熔断不允许被自动清除"


def test_breaker_manual_reset():
    b = CircuitBreaker(max_consecutive_failures=1)
    b.record_failure("e")
    b.reset_manually()
    assert not b.opened and b.consecutive_failures == 0


def test_supervisor_preflight_ok(tmp_path):
    rt = FakeRuntime()
    sup = Supervisor(rt, run_dir=tmp_path)
    assert sup.preflight()["ok"] is True


def test_supervisor_preflight_rejects_stopped(tmp_path):
    rt = FakeRuntime(state=RuntimeState.STOPPED)
    sup = Supervisor(rt, run_dir=tmp_path)
    assert sup.preflight()["ok"] is False


def test_supervisor_halts_on_critical(tmp_path):
    rt = FakeRuntime(healthy=False)
    sup = Supervisor(rt, run_dir=tmp_path)
    res = sup.step()
    assert res["action"] == "halt"
    assert rt.machine.state is RuntimeState.HALTED


def test_supervisor_breaker_stops_restart(tmp_path):
    rt = FakeRuntime(healthy=False)
    sup = Supervisor(rt, run_dir=tmp_path, max_consecutive_failures=2)
    sup.step()
    sup.step()
    assert sup.breaker.opened
    rt.machine._s = RuntimeState.RUNNING      # 假装"恢复"了
    res = sup.step()
    assert res["action"] == "halt_breaker"
    assert rt.machine.state is RuntimeState.HALTED


def test_supervisor_degrade_on_noncritical(tmp_path):
    rt = FakeRuntime()
    rt.health = lambda: {
        "run_id": "r", "state": rt.machine.state.value, "aggregate": "DEGRADED",
        "components": {"market_status": {"healthy": False, "critical": False,
                                         "detail": "行情慢"},
                       "short_guard": {"healthy": True, "critical": True},
                       "settlement": {"healthy": True, "critical": True},
                       "caps": {"healthy": True, "critical": True}},
        "ticks": 0}
    sup = Supervisor(rt, run_dir=tmp_path)
    res = sup.step()
    assert res["action"] == "degrade"
    assert rt.machine.state is RuntimeState.DEGRADED


def test_supervisor_recovery_requires_revalidate(tmp_path):
    rt = FakeRuntime(state=RuntimeState.DEGRADED)
    sup = Supervisor(rt, run_dir=tmp_path)
    sup.step()
    assert rt.supervise_called == 1
    assert sup.recoveries == 1


def test_supervisor_recovery_budget_exhausted(tmp_path):
    rt = FakeRuntime(state=RuntimeState.DEGRADED)
    sup = Supervisor(rt, run_dir=tmp_path, max_auto_recoveries=1)
    rt.machine._s = RuntimeState.DEGRADED
    sup.recoveries = 1
    sup.step()
    assert rt.supervise_called == 0
    assert any(e["kind"] == "recovery_budget_exhausted" for e in sup.report.events)


def test_supervisor_run_iterations(tmp_path):
    rt = FakeRuntime()
    sup = Supervisor(rt, run_dir=tmp_path, interval_seconds=0.0)
    out = sup.run(iterations=3)
    assert out["ok"] and out["report"]["iterations"] == 3
    assert rt.ticks == 3


def test_supervisor_writes_heartbeat(tmp_path):
    rt = FakeRuntime()
    sup = Supervisor(rt, run_dir=tmp_path, interval_seconds=0.0)
    sup.run(iterations=1)
    hb = read_heartbeat(tmp_path / "heartbeat.json")
    assert hb and hb["run_id"] == "run-test"
    assert hb["ticks"] == 1


def test_supervisor_status_shape(tmp_path):
    rt = FakeRuntime()
    sup = Supervisor(rt, run_dir=tmp_path)
    sup.beat()
    st = sup.status()
    assert st["pid_alive"] is True
    assert st["state"] == "RUNNING"
    assert st["breaker"]["opened"] is False


def test_supervisor_stop_request_ends_loop(tmp_path):
    rt = FakeRuntime()
    sup = Supervisor(rt, run_dir=tmp_path, interval_seconds=0.0)
    sup.request_stop("test")
    out = sup.run(minutes=10)
    assert out["ok"] is True
    assert out["report"]["iterations"] == 0


def test_heartbeat_survives_locked_file(tmp_path):
    """外部正打开心跳文件时，写入不得抛异常（Windows 上 os.replace 会被拒）。"""
    import sys
    w = HeartbeatWriter(tmp_path / "hb.json", "r")
    w.beat("RUNNING", 1)
    if sys.platform != "win32":
        pytest.skip("Windows 专属：文件共享语义")
    with open(tmp_path / "hb.json", "rb") as fh:      # 持有句柄，阻塞 replace
        hb = w.beat("RUNNING", 2)
        assert hb.seq == 2
    assert w.write_failures >= 1
    # 释放后应恢复写入
    w.beat("RUNNING", 3)
    assert read_heartbeat(tmp_path / "hb.json")["seq"] == 3
