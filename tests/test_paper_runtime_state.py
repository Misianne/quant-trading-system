"""PaperRuntime 状态机测试：白名单、幂等、不可绕过。"""
from __future__ import annotations

import pytest

from ai_trader.paper_runtime.state import (
    IllegalTransition,
    RuntimeState,
    StateMachine,
    TRANSITIONS,
)


def fresh() -> StateMachine:
    return StateMachine(run_id="t-1")


def test_initial_state_is_stopped():
    assert fresh().state is RuntimeState.STOPPED


def test_state_is_read_only_property():
    m = fresh()
    with pytest.raises(AttributeError):
        m.state = RuntimeState.RUNNING          # type: ignore[misc]


def test_illegal_jump_stopped_to_running():
    m = fresh()
    with pytest.raises(IllegalTransition):
        m.transition(RuntimeState.RUNNING, "跳步")
    assert m.state is RuntimeState.STOPPED


def test_happy_path_transitions():
    m = fresh()
    m.to_preflight("p")
    m.to_initializing("i")
    m.to_ready("r")
    m.to_running("run")
    assert m.state is RuntimeState.RUNNING
    assert [h.to.value for h in m.history] == [
        "PREFLIGHT", "INITIALIZING", "READY", "RUNNING"]


def test_halted_cannot_go_back_to_running():
    m = fresh()
    for step in (m.to_preflight, m.to_initializing, m.to_ready, m.to_running):
        step("x")
    m.to_halted("boom")
    with pytest.raises(IllegalTransition):
        m.to_running("假装恢复")
    assert m.state is RuntimeState.HALTED


def test_failed_cannot_go_back_to_running():
    m = fresh()
    m.to_preflight("p")
    m.to_failed("bad")
    with pytest.raises(IllegalTransition):
        m.to_running("x")


def test_stop_path_from_running():
    m = fresh()
    m.to_preflight("p"); m.to_initializing("i"); m.to_ready("r"); m.to_running("x")
    m.to_stopping("s")
    m.to_stopped("done")
    assert m.state is RuntimeState.STOPPED


def test_degraded_can_return_to_running():
    m = fresh()
    m.to_preflight("p"); m.to_initializing("i"); m.to_ready("r"); m.to_running("x")
    m.to_degraded("warn")
    m.to_running("恢复")
    assert m.state is RuntimeState.RUNNING


def test_idempotent_to_running():
    m = fresh()
    m.to_preflight("p"); m.to_initializing("i"); m.to_ready("r")
    assert m.to_running("x") is True
    assert m.to_running("again") is False
    assert sum(1 for h in m.history if h.to is RuntimeState.RUNNING) == 1


def test_idempotent_to_stopped():
    m = fresh()
    assert m.to_stopped("already") is False
    assert m.state is RuntimeState.STOPPED


def test_history_records_reason_and_run_id():
    m = fresh()
    m.to_preflight("preflight passed")
    h = m.history[-1]
    assert h.reason == "preflight passed"
    assert h.run_id == "t-1"
    assert h.at


def test_trading_and_exit_flags():
    m = fresh()
    assert not m.is_trading() and not m.exits_allowed()
    m.to_preflight("p"); m.to_initializing("i"); m.to_ready("r"); m.to_running("x")
    assert m.is_trading() and m.exits_allowed()
    m.to_degraded("warn")
    assert not m.is_trading()          # 降级禁止新开仓
    assert m.exits_allowed()           # 但允许风险退出


def test_force_from_storage_only_at_construction():
    m = fresh()
    m.force_from_storage(RuntimeState.HALTED, "restore")
    assert m.state is RuntimeState.HALTED
    m2 = StateMachine(run_id="t-2")
    m2.to_preflight("p")
    with pytest.raises(RuntimeError):
        m2.force_from_storage(RuntimeState.RUNNING, "again")


def test_transition_table_is_whitelist():
    """未列出的目标必须被拒绝（不是"默认允许"）。"""
    for frm, targets in TRANSITIONS.items():
        for to in RuntimeState:
            if to in targets or to is frm:
                continue
            m = StateMachine(run_id="x")
            m._state = frm
            with pytest.raises(IllegalTransition):
                m.transition(to, "白名单外")


def test_as_dict_shape():
    m = fresh()
    m.to_preflight("p")
    d = m.as_dict()
    assert d["state"] == "PREFLIGHT"
    assert d["run_id"] == "t-1"
    assert d["trading"] is False
    assert isinstance(d["history"], list)
