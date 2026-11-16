"""回放时钟的安装/还原桥。

为什么单独一层
--------------
``timebase.install()`` 是全局副作用。如果回放中途抛异常而没有还原，
后续所有测试与线上路径都会读到**冻结的历史时间** —— 这是最隐蔽的一类污染。
本桥用「栈 + 上下文管理器 + 进程退出兜底」把还原做成**不可能忘记**：

    with replay_clock(start):
        ...
    # 退出作用域一定还原（含异常路径）
"""
from __future__ import annotations

import atexit
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator, List

from .. import timebase

_LOCK = threading.RLock()
_STACK: List[object] = []
_ATEXIT_HOOKED = False


def _hook_atexit() -> None:
    global _ATEXIT_HOOKED
    if not _ATEXIT_HOOKED:
        atexit.register(_restore_all)
        _ATEXIT_HOOKED = True


def install_replay_clock(start: datetime) -> timebase.ManualClock:
    """安装回放时钟并压栈。返回可推进的 :class:`ManualClock`。"""
    _hook_atexit()
    clock = timebase.ManualClock(start)
    with _LOCK:
        _STACK.append(clock)
    timebase.install(clock)
    return clock


def restore_clock() -> None:
    """还原到上一个时钟（栈空则回到 SystemClock）。"""
    with _LOCK:
        if _STACK:
            _STACK.pop()
    timebase.restore()


def _restore_all() -> None:
    with _LOCK:
        while _STACK:
            _STACK.pop()
            timebase.restore()


@contextmanager
def replay_clock(start: datetime) -> Iterator[timebase.ManualClock]:
    clock = install_replay_clock(start)
    try:
        yield clock
    finally:
        restore_clock()


def active() -> int:
    with _LOCK:
        return len(_STACK)
