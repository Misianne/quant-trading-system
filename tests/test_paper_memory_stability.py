"""内存稳定性回归：锁死 ctypes 类型泄漏与工作集/提交内存行为。

背景：`process_rss_mb()` 原来在函数内定义 `ctypes.Structure` 子类并每次
`WinDLL` 加载，18,535 次心跳后 RSS 涨 275 MB。这类泄漏**不会**被
`tracemalloc` 发现（是原生内存），所以必须用"类型对象数量 + 提交内存"来测。
"""
from __future__ import annotations

import ctypes
import gc
import sys

import pytest

from ai_trader.paper_runtime.supervisor import (
    CpuMeter,
    process_commit_mb,
    process_handle_count,
    process_rss_mb,
)


def struct_types() -> int:
    return sum(1 for o in gc.get_objects()
               if isinstance(o, type) and issubclass(o, ctypes.Structure))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 专属")
def test_rss_read_does_not_create_ctypes_types():
    """1000 次取 RSS 不得新增 Structure 类型（原泄漏点）。"""
    process_rss_mb()
    gc.collect()
    before = struct_types()
    for _ in range(1000):
        process_rss_mb()
    gc.collect()
    after = struct_types()
    assert after - before <= 1, f"ctypes 类型对象增长 {after - before}（应为 0）"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 专属")
def test_commit_read_is_stable():
    process_commit_mb()
    gc.collect()
    before = struct_types()
    for _ in range(500):
        process_commit_mb()
    gc.collect()
    assert struct_types() - before <= 1


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 专属")
def test_handle_count_does_not_leak_handles():
    process_handle_count()
    before = process_handle_count()
    for _ in range(500):
        process_handle_count()
    after = process_handle_count()
    assert after - before <= 5, f"句柄增长 {after - before}"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 专属")
def test_rss_does_not_grow_linearly_with_reads():
    """连续读 3000 次，RSS 增量必须很小（< 8MB）。"""
    for _ in range(50):
        process_rss_mb()
    gc.collect()
    before = process_rss_mb()
    for _ in range(3000):
        process_rss_mb()
    gc.collect()
    after = process_rss_mb()
    assert after - before < 8.0, f"RSS 增长 {after - before:.1f} MB"


def test_rss_returns_positive():
    assert process_rss_mb() > 0


def test_commit_positive_or_unsupported():
    v = process_commit_mb()
    assert v > 0 or v == -1.0


def test_cpu_meter_monotonic_samples():
    m = CpuMeter()
    a = m.sample()
    b = m.sample()
    assert a >= 0 and b >= 0


def test_struct_type_count_stable_across_cpu_sampling():
    before = struct_types()
    for _ in range(200):
        CpuMeter().sample()
    gc.collect()
    assert struct_types() - before <= 1
