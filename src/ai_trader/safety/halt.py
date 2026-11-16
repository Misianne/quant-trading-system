"""急停持久化（Halt Sentinel）。

为什么需要
----------
审计发现：原来的急停只改内存字段，进程崩溃 / 计划任务重启 / 机器重启后
`kill_switch` 会回到配置里的 `false` —— 人类按下的"停止"按钮自己失效了。

设计原则
--------
1. **落盘优先**：急停状态写入 `data/HALT.json`，重启后仍然生效。
2. **读取失败即停止**（fail-closed）：文件存在但读不出来 / 损坏 / 无权限，
   一律按"已停止"处理，绝不当成"未停止"。
3. **写入失败也停止**：写不进哨兵时，本进程立刻进入停止状态并抛出
   :class:`HaltStoreError`，让上层明确知道"这次急停不可靠"。
4. **解除需人工确认**：必须是精确的确认短语 + 操作者标识，不接受默认值、
   不接受空字符串、不接受环境变量隐式打开。

US-P1 N-3：读写语义拆分
-----------------------
原来所有故障都汇成同一个 ``failed`` 布尔，导致：

* "不知道是否被人为停止"（读失败）与"知道自己没停但停不下来"（写失败）
  被当成同一件事 —— 前者必须全阻断，后者在严格设计的 reduce-only 通道下
  才有讨论空间；
* 排查时无法区分 **文件损坏 / 权限不足 / 磁盘满 / 原子写失败**。

现在每次失败都带 :class:`HaltStoreError.kind`，并分别累计到
``read_failed`` / ``write_failed``。
"""
from __future__ import annotations

import errno
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

HALT_FILENAME = "HALT.json"

#: 解除急停必须逐字输入的确认短语
RESUME_PHRASE = "RESUME-TRADING"

#: 允许的急停来源
SOURCE_SENTINEL = "sentinel"        # 哨兵文件
SOURCE_CONFIG = "config"            # 配置文件 / 环境变量
SOURCE_STORAGE_ERROR = "storage_error"  # 哨兵读取异常，按停止处理
SOURCE_NONE = "none"

# --------------------------------------------------------------------------- #
# 失败类型（US-P1 N-3）
# --------------------------------------------------------------------------- #
KIND_READ_FAILED = "read_failed"        # 一般性读取失败
KIND_CORRUPT = "corrupt"                # 文件内容损坏（JSON 非法/字段非法）
KIND_PERMISSION = "permission"          # 权限不足
KIND_NO_SPACE = "no_space"              # 磁盘空间不足
KIND_ATOMIC_FAILED = "atomic_failed"    # os.replace / fsync 失败
KIND_WRITE_FAILED = "write_failed"      # 一般性写入失败
KIND_DIR_FAILED = "dir_failed"          # 目录不可创建
KIND_MISSING = "missing"                # 文件不存在（正常态，非故障）
KIND_UNKNOWN = "unknown"

READ_FAILURE_KINDS = frozenset({
    KIND_READ_FAILED, KIND_CORRUPT, KIND_PERMISSION, KIND_NO_SPACE, KIND_UNKNOWN,
})
WRITE_FAILURE_KINDS = frozenset({
    KIND_WRITE_FAILED, KIND_PERMISSION, KIND_NO_SPACE,
    KIND_ATOMIC_FAILED, KIND_DIR_FAILED, KIND_UNKNOWN,
})


def classify_os_error(exc: BaseException) -> str:
    """把 OS 层异常归类到具体故障类型（不猜：认不出来就归 unknown）。"""
    if isinstance(exc, PermissionError):
        return KIND_PERMISSION
    if isinstance(exc, OSError):
        eno = getattr(exc, "errno", None)
        if eno in (errno.EACCES, errno.EPERM):
            return KIND_PERMISSION
        if eno in (errno.ENOSPC, errno.EDQUOT):
            return KIND_NO_SPACE
        if eno in (errno.ENOENT,):
            return KIND_MISSING
    return KIND_UNKNOWN


class HaltStoreError(RuntimeError):
    """哨兵读写失败。上层必须按"已停止"处理。

    ``kind`` 给出**具体**故障类型；``phase`` 区分 read / write。
    """

    def __init__(self, message: str, kind: str = KIND_UNKNOWN,
                 phase: str = "read"):
        super().__init__(message)
        self.kind = kind
        self.phase = phase

    def as_dict(self) -> Dict[str, str]:
        return {"kind": self.kind, "phase": self.phase, "message": str(self)}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class HaltState:
    halted: bool = False
    reason: str = ""
    at: str = ""
    actor: str = ""
    source: str = SOURCE_NONE
    detail: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "halted": self.halted,
            "reason": self.reason,
            "at": self.at,
            "actor": self.actor,
            "source": self.source,
            "detail": self.detail,
        }

    @classmethod
    def none(cls) -> "HaltState":
        return cls(halted=False, source=SOURCE_NONE)


class HaltStore:
    """哨兵文件的读写。

    文件格式::

        {"halted": true, "reason": "...", "at": "2026-...", "actor": "web"}

    读取语义：

    * 文件不存在 → 未停止
    * 文件存在且 ``halted=true`` → 停止
    * 文件存在但 JSON 非法 / 字段缺失 / 无读权限 → **停止**（storage_error）
    * 文件存在且 ``halted=false`` → 未停止（允许人工写回 false，但推荐删除文件）
    """

    def __init__(self, data_dir: str | Path, filename: str = HALT_FILENAME):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / filename
        #: 最近一次读写失败的原因（sticky，用于让风控持续拒绝）
        self.last_error: str = ""
        self.failed: bool = False
        #: US-P1 N-3：读写分别累计（互不覆盖），用于风险决策分级
        self.read_failed: bool = False
        self.write_failed: bool = False
        self.last_kind: str = ""
        self.last_phase: str = ""

    # ------------------------------------------------------------------ #
    def exists(self) -> bool:
        return self.path.exists()

    def _fail(self, message: str, kind: str, phase: str) -> "HaltStoreError":
        self.failed = True
        self.last_error = message
        self.last_kind = kind
        self.last_phase = phase
        if phase == "read":
            self.read_failed = True
        else:
            self.write_failed = True
        return HaltStoreError(message, kind=kind, phase=phase)

    def read(self) -> HaltState:
        """严格读取。文件不存在返回未停止；其余异常抛 :class:`HaltStoreError`。"""
        if not self.path.exists():
            self.read_failed = False
            self.last_kind = KIND_MISSING
            self.last_phase = "read"
            if not self.write_failed:
                self.failed = False
                self.last_error = ""
            return HaltState.none()
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise self._fail(
                f"急停哨兵读取失败: {type(exc).__name__}: {exc}",
                classify_os_error(exc), "read") from exc
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise self._fail(
                f"急停哨兵内容损坏（JSON 无法解析）: {type(exc).__name__}: {exc}",
                KIND_CORRUPT, "read") from exc
        if not isinstance(data, dict):
            raise self._fail(
                f"急停哨兵格式非法（应为 JSON 对象）: {raw[:80]!r}", KIND_CORRUPT, "read")
        if "halted" not in data:
            raise self._fail("急停哨兵缺少 halted 字段", KIND_CORRUPT, "read")
        halted = data.get("halted")
        if not isinstance(halted, bool):
            raise self._fail(
                f"急停哨兵 halted 字段非布尔值: {halted!r}", KIND_CORRUPT, "read")
        # 读到合法内容 → 读取侧恢复正常（写入侧的粘性状态由 write 自己维护）
        self.read_failed = False
        self.last_kind = ""
        self.last_phase = "read"
        if not self.write_failed:
            self.failed = False
            self.last_error = ""
        return HaltState(
            halted=halted,
            reason=str(data.get("reason") or ""),
            at=str(data.get("at") or ""),
            actor=str(data.get("actor") or ""),
            source=SOURCE_SENTINEL,
        )

    def safe_read(self) -> HaltState:
        """宽松读取：任何异常都转成"已停止"。风控与启动流程应该用这个。"""
        try:
            return self.read()
        except HaltStoreError as exc:
            return HaltState(
                halted=True,
                reason="急停哨兵不可读，按已停止处理（fail-closed）",
                at="",
                actor="system",
                source=SOURCE_STORAGE_ERROR,
                detail=str(exc),
            )

    # ------------------------------------------------------------------ #
    def halt(self, reason: str, actor: str = "system") -> HaltState:
        """写入急停。失败抛 :class:`HaltStoreError`（调用方必须保持停止）。"""
        payload = {
            "halted": True,
            "reason": str(reason or "未说明原因"),
            "at": _now(),
            "actor": str(actor or "system"),
        }
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # 目录都建不出来 → 停止态无法持久化，调用方必须保持停止
            raise self._fail(
                f"急停哨兵目录创建失败: {type(exc).__name__}: {exc}",
                KIND_DIR_FAILED, "write") from exc
        try:
            tmp_fd, tmp_name = tempfile.mkstemp(dir=str(self.data_dir),
                                                prefix=".halt-", suffix=".tmp")
        except OSError as exc:
            raise self._fail(
                f"急停哨兵临时文件创建失败: {type(exc).__name__}: {exc}",
                classify_os_error(exc), "write") from exc
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self.path)
        except OSError as exc:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            kind = classify_os_error(exc)
            # fsync/replace 失败的分类型：空间不足优先，其次原子替换
            if kind == KIND_UNKNOWN:
                kind = KIND_ATOMIC_FAILED if isinstance(exc, OSError) else KIND_WRITE_FAILED
            raise self._fail(
                f"急停哨兵写入失败: {type(exc).__name__}: {exc}", kind, "write") from exc
        self.write_failed = False
        self.failed = False
        self.last_error = ""
        self.last_kind = ""
        self.last_phase = "write"
        return HaltState(halted=True, reason=payload["reason"], at=payload["at"],
                         actor=payload["actor"], source=SOURCE_SENTINEL)

    def resume(self, confirmation: str, actor: str = "human") -> HaltState:
        """解除急停。必须提供精确确认短语。

        返回解除后的状态；失败抛 :class:`HaltStoreError`，调用方必须保持停止。
        """
        if not confirmation or confirmation != RESUME_PHRASE:
            raise HaltStoreError(
                f"解除急停需要精确确认短语 {RESUME_PHRASE!r}，收到 {confirmation!r}",
                kind=KIND_WRITE_FAILED, phase="write")
        try:
            if self.path.exists():
                self.path.unlink()
        except OSError as exc:
            raise self._fail(
                f"急停哨兵删除失败: {type(exc).__name__}: {exc}",
                classify_os_error(exc), "write") from exc
        # 写一条解除记录，供审计追溯（不是哨兵本身，失败不影响解除结果）
        try:
            (self.data_dir / "HALT.resumed.json").write_text(
                json.dumps({"resumed_at": _now(), "actor": actor}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass
        self.failed = False
        self.read_failed = False
        self.write_failed = False
        self.last_error = ""
        self.last_kind = ""
        return HaltState.none()

    # ------------------------------------------------------------------ #
    def classify(self) -> Dict[str, Any]:
        """当前哨兵状态的结构化描述（供 health()/check-env/风控分级使用）。"""
        return {
            "exists": self.path.exists(),
            "failed": self.failed,
            "read_failed": self.read_failed,
            "write_failed": self.write_failed,
            "last_kind": self.last_kind,
            "last_phase": self.last_phase,
            "last_error": self.last_error,
            "path": str(self.path),
        }
