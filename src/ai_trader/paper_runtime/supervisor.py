"""Paper Supervisor：心跳、健康巡检、熔断、安全停止。

职责边界（**不越权**）
----------------------
Supervisor **不做交易决策**，也不修改风控参数。它只能：
* 观察（健康、延迟、订单异常、账户不变量、资源占用）
* 请求停机（``HALTED`` 由 PaperRuntime 自己执行，风控仍然是唯一门禁）

熔断规则
--------
* 关键故障（组件 critical 不健康 / 不变量失败 / 持续无行情）→ 立即 HALT，**不重启**。
* 非关键故障 → 允许 ``max_auto_recoveries`` 次恢复；每次恢复都要走
  :meth:`PaperRuntime._revalidate`（完整重新校验），不是"进程还在"就继续。
* 连续失败达 ``max_consecutive_failures`` → **熔断**，此后不再自动恢复，
  只能人工 ``stop`` 并重新 ``start``。熔断状态不会被任何自动逻辑清除。

进程失联检测
------------
心跳写到 ``<run_dir>/heartbeat.json``（含 ``run_id`` / ``at`` / ``state`` / ``ticks``）。
外部脚本（``paper_status.ps1``）读该文件判断：
* PID 不在 → ``DEAD``
* PID 在但心跳超过 ``heartbeat_timeout_seconds`` 未更新 → ``STALLED``
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .. import timebase

logger = logging.getLogger("ai_trader.paper.supervisor")


def wallnow() -> datetime:
    """**墙钟**（真实时间）。

    心跳与"进程是否失联"必须用真实时间：回放时钟是跳跃推进的，
    用它算年龄会得出负数或几十年，外部监控据此判断会全错。
    """
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------- #
# 资源指标（零依赖）
# ---------------------------------------------------------------------- #
def process_rss_mb() -> float:
    """当前进程常驻内存（MB）。Windows 用 K32GetProcessMemoryInfo，其它平台用 resource。

    必须显式声明 ``restype/argtypes``：不声明时 ctypes 会把 64 位 HANDLE 截断，
    调用返回 0（看起来"读不到内存"，实际是签名问题）。
    """
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD),
                            ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t),
                            ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t),
                            ("PeakPagefileUsage", ctypes.c_size_t)]

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            k32.GetCurrentProcess.argtypes = []
            fn = k32.K32GetProcessMemoryInfo
            fn.restype = wintypes.BOOL
            fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
                           wintypes.DWORD]
            counters = PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(counters)
            if fn(k32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
                return round(counters.WorkingSetSize / (1024 * 1024), 2)
        except Exception:  # noqa: BLE001 - 取不到不伪造
            return -1.0
        return -1.0
    try:
        import resource
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return round(rss / 1024.0, 2)      # Linux 单位 KB
    except Exception:  # noqa: BLE001
        return -1.0


class CpuMeter:
    """进程 CPU 使用率（用 process_time 差值，不依赖 psutil）。"""

    def __init__(self) -> None:
        self._t0 = time.monotonic()
        self._c0 = time.process_time()
        self.last_pct = 0.0

    def sample(self) -> float:
        now, cpu = time.monotonic(), time.process_time()
        dt = now - self._t0
        if dt <= 0:
            return self.last_pct
        pct = (cpu - self._c0) / dt * 100.0
        self._t0, self._c0 = now, cpu
        self.last_pct = round(max(0.0, pct), 2)
        return self.last_pct


# ---------------------------------------------------------------------- #
# 心跳
# ---------------------------------------------------------------------- #
@dataclass
class Heartbeat:
    run_id: str
    pid: int
    state: str
    ticks: int
    at: str
    seq: int = 0
    extra: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {"run_id": self.run_id, "pid": self.pid, "state": self.state,
                "ticks": self.ticks, "at": self.at, "seq": self.seq,
                "extra": dict(self.extra)}


class HeartbeatWriter:
    """原子写心跳文件（临时文件 + replace），外部可安全读取。"""

    def __init__(self, path: Path, run_id: str):
        self.path = Path(path)
        self.run_id = run_id
        self.seq = 0
        self.write_failures = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def beat(self, state: str, ticks: int, extra: Optional[Dict[str, Any]] = None) -> Heartbeat:
        self.seq += 1
        hb = Heartbeat(run_id=self.run_id, pid=os.getpid(), state=state,
                       ticks=int(ticks),
                       at=wallnow().isoformat(timespec="seconds"),
                       seq=self.seq, extra=dict(extra or {}))
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(hb.as_dict(), ensure_ascii=False), encoding="utf-8")
        # Windows：若外部（状态脚本/编辑器）正打开着 heartbeat.json，
        # os.replace 会抛 PermissionError。心跳是**诊断信息**，
        # 绝不能因为"有人在看状态"就把交易进程搞崩 —— 重试后放弃本次写入。
        last: Optional[BaseException] = None
        for attempt in range(4):
            try:
                os.replace(tmp, self.path)
                return hb
            except PermissionError as exc:      # 被占用
                last = exc
                time.sleep(0.02 * (attempt + 1))
            except OSError as exc:               # 其它磁盘问题
                last = exc
                break
        self.write_failures += 1
        if self.write_failures == 1:
            logger.warning("心跳写入被占用，已跳过本次（不会中断交易）：%s", last)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return hb


def read_heartbeat(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def heartbeat_age_seconds(hb: Dict[str, Any], *, now: Optional[datetime] = None) -> float:
    """心跳年龄（秒）。时间戳不可解析 → 返回 inf（视为失联）。"""
    try:
        at = datetime.fromisoformat(str(hb.get("at")))
        if at.tzinfo is None:
            at = at.astimezone()
    except (ValueError, TypeError):
        return float("inf")
    now = now or wallnow()
    return max(0.0, (now - at).total_seconds())


def pid_alive(pid: int) -> bool:
    """零依赖判断 PID 是否存在（Windows 用 OpenProcess）。"""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:  # noqa: BLE001
            return False
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------- #
# 熔断
# ---------------------------------------------------------------------- #
@dataclass
class CircuitBreaker:
    max_consecutive_failures: int = 3
    consecutive_failures: int = 0
    opened: bool = False
    opened_at: str = ""
    reason: str = ""
    trips: int = 0

    def record_failure(self, reason: str) -> bool:
        self.consecutive_failures += 1
        if not self.opened and self.consecutive_failures >= self.max_consecutive_failures:
            self.opened = True
            self.opened_at = wallnow().isoformat(timespec="seconds")
            self.reason = reason
            self.trips += 1
            return True
        return False

    def record_success(self) -> None:
        # 注意：**不清除 opened**。熔断只能人工复位。
        self.consecutive_failures = 0

    def reset_manually(self) -> None:
        self.opened = False
        self.consecutive_failures = 0
        self.reason = ""
        self.opened_at = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"opened": self.opened, "reason": self.reason,
                "opened_at": self.opened_at,
                "consecutive_failures": self.consecutive_failures,
                "max_consecutive_failures": self.max_consecutive_failures,
                "trips": self.trips}


# ---------------------------------------------------------------------- #
@dataclass
class SupervisorReport:
    run_id: str
    started_at: str = ""
    finished_at: str = ""
    iterations: int = 0
    halts: int = 0
    recoveries: int = 0
    breaker_trips: int = 0
    peak_rss_mb: float = 0.0
    avg_cpu_pct: float = 0.0
    last_state: str = ""
    events: List[Dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"run_id": self.run_id, "started_at": self.started_at,
                "finished_at": self.finished_at, "iterations": self.iterations,
                "halts": self.halts, "recoveries": self.recoveries,
                "breaker_trips": self.breaker_trips,
                "peak_rss_mb": self.peak_rss_mb, "avg_cpu_pct": self.avg_cpu_pct,
                "last_state": self.last_state, "events": self.events}


class Supervisor:
    """把运行时"跑起来并盯住它"。"""

    def __init__(self, runtime: Any, *, run_dir: Path,
                 interval_seconds: float = 5.0,
                 max_consecutive_failures: int = 3,
                 max_auto_recoveries: int = 3,
                 heartbeat_timeout_seconds: float = 30.0,
                 max_quote_lag_seconds: float = 60.0,
                 progress_fn: Optional[Callable[[], Dict[str, Any]]] = None):
        self.rt = runtime
        self.run_dir = Path(run_dir)
        self.interval = float(interval_seconds)
        self.heartbeat_timeout = float(heartbeat_timeout_seconds)
        self.max_quote_lag = float(max_quote_lag_seconds)
        #: 外部监控需要的累计进度（tick 每轮会重置，只有累计值能证明"在动"）
        self._progress_fn = progress_fn
        self.breaker = CircuitBreaker(max_consecutive_failures)
        self.max_auto_recoveries = int(max_auto_recoveries)
        self.recoveries = 0
        self.hb = HeartbeatWriter(self.run_dir / "heartbeat.json", runtime.run_id)
        self.cpu = CpuMeter()
        self.report = SupervisorReport(run_id=runtime.run_id,
                                       started_at=wallnow().isoformat(timespec="seconds"))
        self._stop = threading.Event()
        self._last_beat: Optional[datetime] = None
        self._cpu_samples: List[float] = []

    # ------------------------------------------------------------------ #
    def preflight(self) -> Dict[str, Any]:
        """启动检查：运行时必须已 READY/RUNNING，且四组件齐全。"""
        from .runtime import REQUIRED_COMPONENTS
        h = self.rt.health()
        missing = [c for c in REQUIRED_COMPONENTS
                   if c not in (h.get("components") or {})]
        ok = (not missing) and h.get("state") in ("READY", "RUNNING", "DEGRADED")
        return {"ok": ok, "state": h.get("state"), "missing": missing,
                "aggregate": h.get("aggregate")}

    def beat(self) -> None:
        h = self.rt.health()
        self._last_beat = wallnow()
        extra = {
            "aggregate": h.get("aggregate"),
            "can_open_new": h.get("can_open_new"),
            "rss_mb": process_rss_mb(),
            "cpu_pct": self.cpu.last_pct,
        }
        if self._progress_fn is not None:
            try:
                extra.update(self._progress_fn() or {})
            except Exception as exc:  # noqa: BLE001 - 进度统计不影响运行
                extra["progress_error"] = f"{type(exc).__name__}: {exc}"
        self.hb.beat(h.get("state", ""), h.get("ticks", 0), extra)

    # ------------------------------------------------------------------ #
    def health_scan(self) -> Dict[str, Any]:
        """一次完整巡检。返回需要采取的动作。"""
        h = self.rt.health()
        problems: List[str] = []
        critical_problems: List[str] = []

        # 1) 组件健康
        for name, comp in (h.get("components") or {}).items():
            if not comp.get("healthy"):
                msg = f"{name}: {comp.get('detail')}"
                (critical_problems if comp.get("critical") else problems).append(msg)

        # 2) 行情延迟
        broker = getattr(self.rt.ctx, "broker", None)
        feed = self.rt.registry.get("feed")
        if feed is not None:
            fh = feed.health()
            if not fh.healthy:
                critical_problems.append(f"feed: {fh.detail}")

        # 3) 订单异常（未完成且超时的委托）
        stuck = 0
        if broker is not None:
            try:
                for o in broker.orders_snapshot():
                    if str(o.get("status")) in ("unknown", "pending"):
                        stuck += 1
            except Exception as exc:  # noqa: BLE001
                critical_problems.append(f"订单读取失败：{exc}")
        if stuck:
            problems.append(f"存在 {stuck} 笔状态未知/待确认委托")

        # 4) 风险状态
        risk = getattr(self.rt.ctx, "risk", None)
        if risk is not None and getattr(risk, "halt_reason", ""):
            critical_problems.append(f"风控停止：{risk.halt_reason}")

        # 5) 账户不变量
        inv = getattr(self.rt, "_last_invariants", None)
        if inv is not None and not inv.ok:
            critical_problems.append(
                "账户不变量失败：" + ", ".join(r.name for r in inv.critical_failures))

        # 6) 心跳自身
        if self._last_beat is not None:
            age = (wallnow() - self._last_beat).total_seconds()
            if age > self.heartbeat_timeout:
                critical_problems.append(f"心跳停滞 {age:.0f}s")

        # 7) 资源
        rss = process_rss_mb()
        if rss > 0:
            self.report.peak_rss_mb = max(self.report.peak_rss_mb, rss)
        cpu = self.cpu.sample()
        self._cpu_samples.append(cpu)

        action = "none"
        if critical_problems:
            action = "halt"
        elif problems:
            action = "degrade"
        return {"action": action, "critical": critical_problems,
                "warnings": problems, "rss_mb": rss, "cpu_pct": cpu,
                "state": h.get("state")}

    # ------------------------------------------------------------------ #
    def step(self) -> Dict[str, Any]:
        """一次监督周期：巡检 → 决定动作 → 记录。"""
        self.report.iterations += 1
        scan = self.health_scan()
        action = scan["action"]

        if self.breaker.opened:
            # 熔断后只观察、不重启；如果运行时还在交易就要求停
            if self.rt.machine.is_trading():
                self.rt._halt("熔断已触发，禁止继续交易")
                action = "halt_breaker"
            self._event("breaker_blocking", {"reason": self.breaker.reason})
            self.beat()
            return {"action": action, "breaker": True}

        if action == "halt":
            reason = "; ".join(scan["critical"]) or "巡检发现关键故障"
            self.rt._halt(reason)
            self.report.halts += 1
            self._event("halt", {"reason": reason})
            if self.breaker.record_failure(reason):
                self.report.breaker_trips += 1
                self._event("breaker_opened", {"reason": reason})
        elif action == "degrade":
            self.rt._degraded("; ".join(scan["warnings"]))
            self._event("degrade", {"warnings": scan["warnings"]})
        else:
            self.breaker.record_success()
            # 已经在降级但巡检无问题：runtime.supervise() 会走完整重新校验
            if self.rt.machine.state.value == "DEGRADED":
                if self.recoveries < self.max_auto_recoveries:
                    res = self.rt.supervise()
                    if res.get("action") == "recovered":
                        self.recoveries += 1
                        self.report.recoveries += 1
                        self._event("recovered", {"count": self.recoveries})
                else:
                    self._event("recovery_budget_exhausted",
                                {"max": self.max_auto_recoveries})
        self.beat()
        self.report.last_state = self.rt.machine.state.value
        return {"action": action, "scan": scan}

    # ------------------------------------------------------------------ #
    def run(self, *, minutes: Optional[float] = None,
            iterations: Optional[int] = None) -> Dict[str, Any]:
        """主循环。``minutes`` 与 ``iterations`` 二选一。"""
        pre = self.preflight()
        self._event("supervisor_start", pre)
        if not pre["ok"]:
            self.report.finished_at = wallnow().isoformat(timespec="seconds")
            self._event("preflight_failed", pre)
            return {"ok": False, "reason": "启动检查未通过", "preflight": pre,
                    "report": self.report.as_dict()}

        self.beat()
        t0 = time.monotonic()
        n = 0
        while not self._stop.is_set():
            if iterations is not None and n >= iterations:
                break
            if minutes is not None and (time.monotonic() - t0) >= minutes * 60:
                break
            try:
                self.rt.tick()
            except Exception as exc:  # noqa: BLE001
                self._event("tick_error", {"error": f"{type(exc).__name__}: {exc}"})
                if self.breaker.record_failure(str(exc)):
                    self.report.breaker_trips += 1
                    self._event("breaker_opened", {"reason": str(exc)})
                self.rt._halt(f"tick 异常：{exc}")
            self.step()
            n += 1
            if self.rt.machine.state.value in ("HALTED", "FAILED"):
                self._event("halted_terminal", {"state": self.rt.machine.state.value})
                break
            if self._stop.wait(self.interval):
                break

        self.report.finished_at = wallnow().isoformat(timespec="seconds")
        self.report.avg_cpu_pct = (round(sum(self._cpu_samples) / len(self._cpu_samples), 2)
                                   if self._cpu_samples else 0.0)
        self.report.last_state = self.rt.machine.state.value
        self._event("supervisor_end", {"iterations": self.report.iterations,
                                       "state": self.report.last_state})
        return {"ok": True, "report": self.report.as_dict()}

    def request_stop(self, reason: str = "supervisor stop") -> None:
        self._stop.set()
        self._event("stop_requested", {"reason": reason})

    # ------------------------------------------------------------------ #
    def _event(self, kind: str, detail: Dict[str, Any]) -> None:
        self.report.events.append({
            "kind": kind, "at": wallnow().isoformat(timespec="seconds"),
            **detail})
        if len(self.report.events) > 2000:
            self.report.events = self.report.events[-2000:]

    def status(self) -> Dict[str, Any]:
        hb = read_heartbeat(self.hb.path)
        return {"run_id": self.rt.run_id, "pid": os.getpid(),
                "state": self.rt.machine.state.value,
                "breaker": self.breaker.as_dict(),
                "recoveries": self.recoveries,
                "heartbeat": hb,
                "heartbeat_age_s": (heartbeat_age_seconds(hb) if hb else None),
                "pid_alive": pid_alive(int((hb or {}).get("pid", 0))),
                "report": self.report.as_dict()}
