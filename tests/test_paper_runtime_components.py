"""组件注册表测试：拓扑、回滚、逆序释放、幂等、健康聚合。"""
from __future__ import annotations

import pytest

from ai_trader.paper_runtime.components import (
    Component,
    ComponentRegistry,
    DependencyError,
)


class Rec(Component):
    """记录调用顺序的测试组件。"""

    def __init__(self, name, deps=(), fail_start=False, fail_stop=False,
                 healthy=True, critical=True):
        super().__init__()
        self.name = name
        self.depends_on = tuple(deps)
        self.critical = critical
        self.fail_start = fail_start
        self.fail_stop = fail_stop
        self.healthy = healthy
        self.log = []
        self.ctx_seen = None

    def _start(self, ctx):
        self.log.append(f"start:{self.name}")
        self.ctx_seen = ctx
        if self.fail_start:
            raise RuntimeError(f"{self.name} 启动失败")

    def _stop(self, ctx):
        self.log.append(f"stop:{self.name}")
        if self.fail_stop:
            raise RuntimeError(f"{self.name} 关闭失败")

    def _health(self):
        if isinstance(self.healthy, Exception):
            raise self.healthy
        return self.healthy, "ok" if self.healthy else "bad"


def test_topological_order_respects_dependencies():
    r = ComponentRegistry()
    order_log = []
    a = Rec("a")
    b = Rec("b", deps=("a",))
    c = Rec("c", deps=("b",))
    for comp in (c, b, a):
        r.register(comp)
    assert r.order() == ["a", "b", "c"]


def test_cycle_detected():
    r = ComponentRegistry()
    r.register(Rec("a", deps=("b",)))
    r.register(Rec("b", deps=("a",)))
    with pytest.raises(DependencyError):
        r.order()


def test_missing_dependency_detected():
    r = ComponentRegistry()
    r.register(Rec("a", deps=("ghost",)))
    with pytest.raises(DependencyError):
        r.order()


def test_duplicate_name_rejected():
    r = ComponentRegistry()
    r.register(Rec("a"))
    with pytest.raises(DependencyError):
        r.register(Rec("a"))


def test_start_all_in_order():
    r = ComponentRegistry()
    a, b = Rec("a"), Rec("b", deps=("a",))
    r.register(a); r.register(b)
    res = r.start_all()
    assert res.ok
    assert a.log + b.log == ["start:a", "start:b"]


def test_start_failure_rolls_back_in_reverse():
    r = ComponentRegistry()
    a, b, c = Rec("a"), Rec("b", deps=("a",)), Rec("c", deps=("b",), fail_start=True)
    for x in (a, b, c):
        r.register(x)
    res = r.start_all()
    assert not res.ok
    assert res.failed.startswith("c:")
    assert res.rolled_back == ["b", "a"]
    assert not a.started and not b.started


def test_partial_start_never_reported_ok():
    r = ComponentRegistry()
    r.register(Rec("a"))
    r.register(Rec("b", deps=("a",), fail_start=True))
    res = r.start_all()
    assert res.ok is False
    assert r.aggregate()[0] == "HALTED"


def test_stop_all_is_reverse_order():
    r = ComponentRegistry()
    a, b, c = Rec("a"), Rec("b", deps=("a",)), Rec("c", deps=("b",))
    for x in (a, b, c):
        r.register(x)
    r.start_all()
    r.stop_all()
    assert c.log[-1] == "stop:c"
    assert b.log[-1] == "stop:b"
    assert a.log[-1] == "stop:a"


def test_stop_failure_recorded_not_swallowed():
    r = ComponentRegistry()
    r.register(Rec("a", fail_stop=True))
    r.start_all()
    res = r.stop_all()
    assert res.ok is False
    assert res.errors and "关闭 a 失败" in res.errors[0]


def test_start_is_idempotent():
    r = ComponentRegistry()
    a = Rec("a")
    r.register(a)
    r.start_all()
    r.start_all()
    assert a.start_count == 1


def test_stop_is_idempotent():
    r = ComponentRegistry()
    a = Rec("a")
    r.register(a)
    r.start_all()
    r.stop_all()
    r.stop_all()
    assert a.stop_count == 1


def test_aggregate_halted_on_critical_unhealthy():
    r = ComponentRegistry()
    r.register(Rec("a", healthy=False, critical=True))
    r.start_all()
    worst, reasons = r.aggregate()
    assert worst == "HALTED"
    assert any("a:" in x for x in reasons)


def test_aggregate_degraded_on_noncritical_unhealthy():
    r = ComponentRegistry()
    r.register(Rec("a", healthy=False, critical=False))
    r.start_all()
    worst, reasons = r.aggregate()
    assert worst == "DEGRADED"


def test_unstarted_component_counts_unhealthy():
    r = ComponentRegistry()
    r.register(Rec("a"))
    worst, reasons = r.aggregate()
    assert worst == "HALTED"
    assert any("未启动" in x for x in reasons)


def test_health_exception_is_unhealthy():
    r = ComponentRegistry()
    r.register(Rec("a", healthy=RuntimeError("boom")))
    r.start_all()
    h = r.health_all()["a"]
    assert h.healthy is False
    assert "boom" in h.detail


def test_required_components_reported():
    r = ComponentRegistry()
    r.register(Rec("a"))
    missing = r.check_required(("a", "b"))
    assert missing == ["b"]


def test_context_passed_to_components():
    r = ComponentRegistry()
    a = Rec("a")
    r.register(a)
    r.start_all(ctx={"k": 1})
    assert a.ctx_seen == {"k": 1}
