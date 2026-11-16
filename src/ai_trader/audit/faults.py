"""数据库故障隔离与外部告警（US-P1 N-1）。

为什么需要独立于 SQLite
----------------------
`monitor()` 的止损、撤单、对账、审计**全部**依赖 `AuditJournal`（SQLite）。
一旦 SQLite 局部失效（WAL 锁、磁盘满、杀软占用、文件被替换），
原来会直接抛异常 → 整个 monitor 退出 → 本轮所有风险处置都不执行，
而且**这条事实本身没能被记录下来**（因为记录也依赖同一个坏掉的库）。

本模块提供一条**完全独立的持久化通道**：

* ``data/faults.jsonl`` —— 只追加的故障流水（纯文件 IO，不用 SQLite）
* ``data/FAULT.json``   —— 当前故障哨兵（原子写，供外部监控读取）
* ``logs/alerts.log``   —— 面向人的告警行（含 ``ALERT`` 前缀，便于日志采集）

自身失败时的最后防线：写 ``stderr`` + 进程内计数器。宁可只有进程内计数，
也不能因为"记录故障的通道也坏了"而静默。

设计约束（与风控一致）
--------------------
* 这里**只记录**，不做任何交易决策；
* 任何方法都**不抛异常**（调用方多处于 except 分支，不能再被二次打断）；
* 写入使用原子替换，避免留下半个 JSON 让外部监控误读。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

#: 故障类别（与 monitor 的三条通道一一对应）
KIND_AUDIT_WRITE = "audit_write_failed"       # 审计写入失败
KIND_ORDER_READ = "order_read_failed"         # 订单查询失败
KIND_POSITION_READ = "position_read_failed"   # 持仓读取失败
KIND_HALT_STORAGE = "halt_storage_failed"     # 急停哨兵读写失败
KIND_TIMESTAMP = "order_timestamp_anomaly"    # 订单时间戳异常
KIND_CONFIG = "config_guard_violation"        # 配置守卫违规
KIND_UNKNOWN_ORDER = "unknown_order_block"    # 未知单阻断/放行事件
KIND_ORDER_INTENT = "order_intent_failed"   # 下单意图预写/更新失败（R-1）
KIND_RESERVATION = "sell_reservation_unverified"  # 卖出预留量无法核实（R-2）
KIND_STOPS = "stops_protection_anomaly"    # 止损保护异常（R-7）
KIND_OTHER = "other"

#: 严重度
SEV_WARN = "warn"
SEV_ERROR = "error"
SEV_CRITICAL = "critical"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class FaultEntry:
    ts: str
    kind: str
    severity: str
    detail: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {"ts": self.ts, "kind": self.kind,
                "severity": self.severity, "detail": self.detail}


class FaultRecorder:
    """故障记录器。所有方法都不抛异常。"""

    def __init__(self, data_dir: str | Path, log_dir: Optional[str | Path] = None,
                 filename: str = "faults.jsonl", sentinel: str = "FAULT.json"):
        self.data_dir = Path(data_dir)
        self.log_dir = Path(log_dir) if log_dir else None
        self.path = self.data_dir / filename
        self.sentinel_path = self.data_dir / sentinel
        self.alert_path = (self.log_dir / "alerts.log") if self.log_dir else (self.data_dir / "alerts.log")
        self._lock = threading.RLock()
        #: 进程内计数（即使文件写不进去也能被读出来做验收）
        self.counts: Dict[str, int] = {}
        self.last: Optional[FaultEntry] = None
        self.persist_failed = False

    # ------------------------------------------------------------------ #
    def record(self, kind: str, detail: Optional[Dict[str, Any]] = None,
               severity: str = SEV_ERROR, alert: bool = True) -> FaultEntry:
        """记录一条故障。永不抛异常。"""
        entry = FaultEntry(ts=_now_iso(), kind=kind, severity=severity,
                           detail=dict(detail or {}))
        with self._lock:
            self.counts[kind] = self.counts.get(kind, 0) + 1
            self.last = entry
            self._append_jsonl(entry)
            self._write_sentinel(entry)
            if alert:
                self._alert(entry)
        return entry

    # ------------------------------------------------------------------ #
    def _append_jsonl(self, entry: FaultEntry) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry.as_dict(), ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except Exception as exc:  # noqa: BLE001
            self.persist_failed = True
            self._stderr(f"faults.jsonl 写入失败: {type(exc).__name__}: {exc}", entry)

    def _write_sentinel(self, entry: FaultEntry) -> None:
        """原子写当前故障哨兵，供外部监控（healthcheck/告警系统）读取。"""
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.data_dir), prefix=".fault-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump({"latest": entry.as_dict(),
                               "counts": dict(self.counts)}, fh,
                              ensure_ascii=False, indent=2)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self.sentinel_path)
            finally:
                if os.path.exists(tmp):
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
        except Exception as exc:  # noqa: BLE001
            self.persist_failed = True
            self._stderr(f"FAULT.json 写入失败: {type(exc).__name__}: {exc}", entry)

    def _alert(self, entry: FaultEntry) -> None:
        """面向人的告警行。外部采集（tail/grep ALERT）即可发现。"""
        line = (f"{entry.ts} ALERT severity={entry.severity} kind={entry.kind} "
                f"detail={json.dumps(entry.detail, ensure_ascii=False)}\n")
        try:
            self.alert_path.parent.mkdir(parents=True, exist_ok=True)
            with self.alert_path.open("a", encoding="utf-8") as fh:
                fh.write(line)
        except Exception:  # noqa: BLE001
            pass
        self._stderr(line.rstrip(), entry)

    @staticmethod
    def _stderr(msg: str, entry: Optional[FaultEntry] = None) -> None:
        try:
            print(f"[FaultRecorder] {msg}", file=sys.stderr)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ #
    def read_all(self, limit: int = 500) -> List[Dict[str, Any]]:
        """读回故障流水（验收/排查用）。读不到返回空列表，不抛异常。"""
        out: List[Dict[str, Any]] = []
        try:
            if not self.path.exists():
                return out
            with self.path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
        except Exception:  # noqa: BLE001
            return out
        return out[-limit:]

    def current(self) -> Optional[Dict[str, Any]]:
        try:
            if self.sentinel_path.exists():
                return json.loads(self.sentinel_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None
        return None

    def clear(self) -> None:
        """人工确认后清除哨兵（流水保留，不清历史）。"""
        try:
            if self.sentinel_path.exists():
                self.sentinel_path.unlink()
        except OSError:
            pass

    def snapshot(self) -> Dict[str, Any]:
        return {"counts": dict(self.counts),
                "last": self.last.as_dict() if self.last else None,
                "persist_failed": self.persist_failed,
                "faults_path": str(self.path),
                "alert_path": str(self.alert_path)}
