"""可注入的时间基准。

为什么需要
----------
Engine / Risk 内部有多处 ``datetime.now()``。回放（Replay）时如果这些调用点
仍然读系统真实时间，就会出现「历史时间戳 + 现在时间」混用：

* 市场状态门禁会用**真实现在**去判断一笔**历史**成交是否该放行；
* 风控的「每分钟下单数」「重复窗口」会按真实时钟统计；
* 事件时间与系统时间的差值会时而超前、时而过期。

本模块把「现在几点」收敛成**唯一入口**：

    ai_trader.timebase.now()        # 本地时区、带 tzinfo
    ai_trader.timebase.utcnow()     # UTC

默认是 :class:`SystemClock`，行为与 ``datetime.now().astimezone()`` 完全一致
（既有 800+ 测试不受影响）。回放时由 ``PaperRuntime`` 安装 :class:`ManualClock`，
退出时 **必须** 还原。

线程安全
--------
安装/还原用模块级锁保护；``now()`` 读的是不可变引用，无锁竞争。
不做的：不提供"跨线程各自时钟"——回放是单线程推进的，多时钟只会制造歧义。
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Protocol

__all__ = [
    "Clock", "SystemClock", "ManualClock", "FrozenClock",
    "now", "utcnow", "today", "install", "restore", "current", "ClockNotInstalled",
]


class Clock(Protocol):
    def now(self) -> datetime:  # pragma: no cover - 协议
        ...


class SystemClock:
    """真实系统时钟（默认）。返回**带本地时区**的 aware datetime。"""

    name = "system"

    def now(self) -> datetime:
        return datetime.now().astimezone()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return "SystemClock()"


class ManualClock:
    """回放时钟：由调用方显式推进。

    ``advance()`` 只能向前 —— 时间倒退在真实市场里不存在，倒退一定是 bug，
    这里直接抛错而不是悄悄接受（避免"时钟倒退"被当成正常行情处理）。
    """

    name = "manual"

    def __init__(self, start: datetime):
        if start.tzinfo is None:
            raise ValueError("ManualClock 起始时间必须带时区（禁止 naive）")
        self._t = start
        self._lock = threading.Lock()

    def now(self) -> datetime:
        with self._lock:
            return self._t

    def set(self, dt: datetime) -> None:
        """显式设置（仅允许向前）。"""
        if dt.tzinfo is None:
            raise ValueError("禁止把 naive 时间设进回放时钟")
        with self._lock:
            if dt < self._t:
                raise ValueError(
                    f"回放时钟不允许倒退：{dt.isoformat()} < {self._t.isoformat()}")
            self._t = dt

    def advance(self, delta: timedelta) -> datetime:
        if delta < timedelta(0):
            raise ValueError("advance 不接受负增量（时钟倒退）")
        with self._lock:
            self._t = self._t + delta
            return self._t

    def __repr__(self) -> str:  # pragma: no cover
        return f"ManualClock({self._t.isoformat()})"


class FrozenClock:
    """固定时钟：用于"同一时刻重放 N 次"的确定性实验。"""

    name = "frozen"

    def __init__(self, at: datetime):
        if at.tzinfo is None:
            raise ValueError("FrozenClock 必须带时区")
        self._t = at

    def now(self) -> datetime:
        return self._t


class ClockNotInstalled(RuntimeError):
    """回放结束后忘记还原时抛出（fail-fast，避免污染线上路径）。"""


_LOCK = threading.RLock()
_CURRENT: Clock = SystemClock()
_IS_OVERRIDDEN = False
_PREVIOUS: Optional[Clock] = None


def current() -> Clock:
    return _CURRENT


def install(clock: Clock) -> None:
    """安装时钟。重复安装会覆盖并记录上一个（用于嵌套还原）。"""
    global _CURRENT, _IS_OVERRIDDEN, _PREVIOUS
    with _LOCK:
        _PREVIOUS = _CURRENT
        _CURRENT = clock
        _IS_OVERRIDDEN = True


def restore() -> None:
    """还原到安装前的时钟；没有安装过则回到 SystemClock。"""
    global _CURRENT, _IS_OVERRIDDEN, _PREVIOUS
    with _LOCK:
        _CURRENT = _PREVIOUS if _PREVIOUS is not None else SystemClock()
        _PREVIOUS = None
        _IS_OVERRIDDEN = False


def now() -> datetime:
    """当前本地时间（带时区）。"""
    dt = _CURRENT.now()
    if dt.tzinfo is None:  # 防御：任何时钟都不允许返回 naive
        raise ClockNotInstalled(f"{_CURRENT!r} 返回了 naive 时间")
    return dt


def utcnow() -> datetime:
    return now().astimezone(timezone.utc)


def today():
    return now().date()
