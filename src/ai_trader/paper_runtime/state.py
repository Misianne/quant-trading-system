"""PaperRuntime 状态机。

设计约束（来自验收要求）
------------------------
1. 状态**不可直接赋值** —— ``state`` 是只读属性，唯一修改入口是 :meth:`transition`。
2. 合法迁移表是**白名单**，未列出的一律拒绝（不是"默认允许"）。
3. 每次迁移都记录 ``from / to / reason / at / run_id``，可审计。
4. ``HALTED`` 是**吸收态**之一：不允许自动回到 ``RUNNING``；
   必须先 ``STOPPING → STOPPED``，再重新走完整 ``PREFLIGHT``。
   （否则"进程还活着"就会被当成"已恢复"，这正是要防的。）
5. 迁移方法本身**幂等**：重复 ``to_running()`` 在已 RUNNING 时是 no-op。

状态图::

    STOPPED ──> PREFLIGHT ──> INITIALIZING ──> READY ──> RUNNING
                    │              │             │          │  ▲
                    │              ▼             │          ▼  │
                    └──────────> FAILED          │       DEGRADED
                                   │             │          │
                                   │             ▼          ▼
                                   └──────> STOPPING <── HALTED
                                                 │
                                                 ▼
                                              STOPPED
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class RuntimeState(str, Enum):
    STOPPED = "STOPPED"
    PREFLIGHT = "PREFLIGHT"
    INITIALIZING = "INITIALIZING"
    READY = "READY"
    RUNNING = "RUNNING"
    DEGRADED = "DEGRADED"
    HALTED = "HALTED"
    FAILED = "FAILED"
    STOPPING = "STOPPING"


#: 白名单迁移。**不在表里 = 非法**。
TRANSITIONS: Dict[RuntimeState, Tuple[RuntimeState, ...]] = {
    RuntimeState.STOPPED: (RuntimeState.PREFLIGHT,),
    RuntimeState.PREFLIGHT: (RuntimeState.INITIALIZING, RuntimeState.FAILED,
                             RuntimeState.STOPPED),
    RuntimeState.INITIALIZING: (RuntimeState.READY, RuntimeState.FAILED,
                                RuntimeState.STOPPING),
    RuntimeState.READY: (RuntimeState.RUNNING, RuntimeState.STOPPING,
                         RuntimeState.HALTED, RuntimeState.FAILED),
    RuntimeState.RUNNING: (RuntimeState.DEGRADED, RuntimeState.HALTED,
                           RuntimeState.STOPPING),
    RuntimeState.DEGRADED: (RuntimeState.RUNNING, RuntimeState.HALTED,
                            RuntimeState.STOPPING),
    # HALTED / FAILED 只能走向关闭 —— 不允许直接回 RUNNING
    RuntimeState.HALTED: (RuntimeState.STOPPING,),
    RuntimeState.FAILED: (RuntimeState.STOPPING, RuntimeState.STOPPED),
    RuntimeState.STOPPING: (RuntimeState.STOPPED,),
}

#: 这些状态下**允许产生新成交**。别处一律视为"禁止新开仓"。
TRADING_STATES = (RuntimeState.RUNNING,)

#: 这些状态下允许**风险退出**（退出意图永不因运行状态丢失）
EXIT_ALLOWED_STATES = (RuntimeState.RUNNING, RuntimeState.DEGRADED)


class IllegalTransition(RuntimeError):
    def __init__(self, frm: RuntimeState, to: RuntimeState, reason: str = ""):
        super().__init__(
            f"非法状态迁移 {frm.value} → {to.value}"
            + (f"（{reason}）" if reason else "")
            + f"；允许的目标：{[s.value for s in TRANSITIONS.get(frm, ())]}")
        self.frm = frm
        self.to = to
        self.reason = reason


@dataclass(frozen=True)
class Transition:
    frm: RuntimeState
    to: RuntimeState
    reason: str
    at: str
    run_id: str

    def as_dict(self) -> Dict[str, Any]:
        return {"from": self.frm.value, "to": self.to.value,
                "reason": self.reason, "at": self.at, "run_id": self.run_id}


@dataclass
class StateMachine:
    """单次运行的运行状态。不可直接改 ``state``。"""

    run_id: str = ""
    _state: RuntimeState = field(default=RuntimeState.STOPPED, repr=False)
    _history: List[Transition] = field(default_factory=list, repr=False)
    _lock: Any = field(default_factory=threading.RLock, repr=False)

    # ------------------------------------------------------------------ #
    @property
    def state(self) -> RuntimeState:
        return self._state

    @property
    def history(self) -> List[Transition]:
        with self._lock:
            return list(self._history)

    def can_go(self, to: RuntimeState) -> bool:
        return to in TRANSITIONS.get(self._state, ())

    def allowed_targets(self) -> Tuple[RuntimeState, ...]:
        return TRANSITIONS.get(self._state, ())

    def is_trading(self) -> bool:
        return self._state in TRADING_STATES

    def exits_allowed(self) -> bool:
        return self._state in EXIT_ALLOWED_STATES

    # ------------------------------------------------------------------ #
    def transition(self, to: RuntimeState, reason: str = "",
                   *, allow_same: bool = True) -> bool:
        """执行迁移。返回 True 表示发生了迁移，False 表示同态 no-op。

        非法迁移抛 :class:`IllegalTransition` —— 绝不静默纠正。
        """
        with self._lock:
            if to is self._state:
                if not allow_same:
                    raise IllegalTransition(self._state, to, "目标与当前相同")
                return False
            if to not in TRANSITIONS.get(self._state, ()):
                raise IllegalTransition(self._state, to, reason)
            rec = Transition(frm=self._state, to=to, reason=reason,
                             at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                             run_id=self.run_id)
            self._history.append(rec)
            self._state = to
            return True

    def force_from_storage(self, state: RuntimeState, reason: str) -> None:
        """**仅**用于从持久化快照恢复初始态（构造期，history 为空）。

        这不是"绕过状态机"的口子：只允许在 ``history`` 为空且当前为 STOPPED 时调用，
        且必须写明理由；此后所有迁移仍走白名单。
        """
        with self._lock:
            if self._history:
                raise RuntimeError("force_from_storage 只允许在构造期调用")
            if self._state is not RuntimeState.STOPPED:
                raise RuntimeError("force_from_storage 只允许从 STOPPED 起步")
            self._history.append(Transition(
                frm=RuntimeState.STOPPED, to=state, reason=f"[restore] {reason}",
                at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                run_id=self.run_id))
            self._state = state

    def as_dict(self) -> Dict[str, Any]:
        return {"state": self._state.value, "run_id": self.run_id,
                "trading": self.is_trading(), "exits_allowed": self.exits_allowed(),
                "history": [h.as_dict() for h in self.history]}

    # 便捷方法（幂等） -------------------------------------------------- #
    def to_preflight(self, reason: str = "") -> bool:
        return self.transition(RuntimeState.PREFLIGHT, reason)

    def to_initializing(self, reason: str = "") -> bool:
        return self.transition(RuntimeState.INITIALIZING, reason)

    def to_ready(self, reason: str = "") -> bool:
        if self._state is RuntimeState.READY:
            return False
        return self.transition(RuntimeState.READY, reason)

    def to_running(self, reason: str = "") -> bool:
        if self._state is RuntimeState.RUNNING:
            return False
        return self.transition(RuntimeState.RUNNING, reason)

    def to_degraded(self, reason: str = "") -> bool:
        if self._state is RuntimeState.DEGRADED:
            return False
        return self.transition(RuntimeState.DEGRADED, reason)

    def to_halted(self, reason: str = "") -> bool:
        if self._state is RuntimeState.HALTED:
            return False
        return self.transition(RuntimeState.HALTED, reason)

    def to_failed(self, reason: str = "") -> bool:
        if self._state is RuntimeState.FAILED:
            return False
        return self.transition(RuntimeState.FAILED, reason)

    def to_stopping(self, reason: str = "") -> bool:
        if self._state is RuntimeState.STOPPING:
            return False
        return self.transition(RuntimeState.STOPPING, reason)

    def to_stopped(self, reason: str = "") -> bool:
        if self._state is RuntimeState.STOPPED:
            return False
        return self.transition(RuntimeState.STOPPED, reason)
