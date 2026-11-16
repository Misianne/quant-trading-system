"""单实例锁：同一账户/同一运行根目录只允许一个 PaperRuntime。

为什么必须做
------------
两个进程同时控制同一个模拟账户，会出现「两边都读到 10 万现金，各自买入」——
账本立刻不一致。文件锁不是可选项，是正确性前提。

实现
----
* ``O_CREAT | O_EXCL`` 原子创建锁文件（Windows/POSIX 都成立）；
* 锁文件内写 pid / run_id / 启动时间；
* 已存在时：读 pid，**进程还活着 → 拒绝**；进程已死 → 判定为残留锁，
  删除后重试一次（并记录 `stale_recovered`）；
* 释放时只删自己持有的锁（比对 pid），避免误删别人的。
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from .. import timebase


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.restype = wintypes.HANDLE
            k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE,
                                               ctypes.POINTER(wintypes.DWORD)]
            h = k32.OpenProcess(0x1000, False, int(pid))
            if not h:
                return False
            try:
                code = wintypes.DWORD(0)
                k32.GetExitCodeProcess(h, ctypes.byref(code))
                return int(code.value) == 259      # STILL_ACTIVE
            finally:
                k32.CloseHandle(h)
        except Exception:  # noqa: BLE001
            return False
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


@dataclass
class LockInfo:
    acquired: bool
    reason: str = ""
    holder: Dict[str, Any] = field(default_factory=dict)
    stale_recovered: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {"acquired": self.acquired, "reason": self.reason,
                "holder": dict(self.holder),
                "stale_recovered": self.stale_recovered}


class InstanceLock:
    """基于文件系统的单实例约束。"""

    def __init__(self, path: Path, *, run_id: str = ""):
        self.path = Path(path)
        self.run_id = run_id
        self.held = False
        self.pid = os.getpid()

    # ------------------------------------------------------------------ #
    @staticmethod
    def read(path: Path) -> Dict[str, Any]:
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def acquire(self) -> LockInfo:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in (0, 1):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                holder = self.read(self.path)
                pid = int(holder.get("pid", 0) or 0)
                if pid and _pid_alive(pid):
                    return LockInfo(False, f"已有运行实例持有锁（pid={pid}）", holder)
                # 残留锁（进程已退出）→ 清理后重试一次
                try:
                    self.path.unlink()
                except OSError:
                    pass
                if attempt == 1:
                    return LockInfo(False, "锁文件无法清理", holder, stale_recovered=True)
                continue
            payload = {"pid": self.pid, "run_id": self.run_id,
                       "at": timebase.now().isoformat(timespec="seconds")}
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
                fh.flush()
                os.fsync(fh.fileno())
            self.held = True
            return LockInfo(True, "ok", payload, stale_recovered=(attempt == 1))
        return LockInfo(False, "未知失败")

    def release(self) -> bool:
        if not self.held:
            return False
        holder = self.read(self.path)
        # 只删自己的锁
        if int(holder.get("pid", 0) or 0) not in (0, self.pid):
            return False
        try:
            self.path.unlink()
        except OSError:
            return False
        self.held = False
        return True

    def holder(self) -> Dict[str, Any]:
        return self.read(self.path)
