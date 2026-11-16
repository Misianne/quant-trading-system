"""100 次启停循环：真实进程，不是模拟。

每个循环：
  1. 启动 `--paper --session-minutes N`（真实 PaperRuntime）
  2. 等待锁文件 + 心跳出现（证明进入 READY/RUNNING）
  3. 跑至少一个交易段（心跳 ticks 增长）
  4. 写 stop.request 请求**优雅**停止
  5. 等待进程退出（超时则失败，不掩盖）
  6. 校验：退出码 0、锁文件已删除、进程已消失

输出 ``cycles.jsonl``（逐轮）与 ``summary.json``。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
if not Path(PY).exists():
    PY = sys.executable


def env() -> dict:
    e = dict(os.environ)
    e["PYTHONPATH"] = str(ROOT / "src")
    e.pop("PAPER_CRASH_AT", None)
    return e


def wait_for(pred, timeout: float, interval: float = 0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(interval)
    return None


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
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
            return int(code.value) == 259
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def run_cycle(i: int, root: Path) -> dict:
    cdir = root / f"c{i:03d}"
    cdir.mkdir(parents=True, exist_ok=True)
    out = cdir / "stdout.log"
    t0 = time.monotonic()
    rec = {"cycle": i, "ok": False, "started": False, "ready": False,
           "ticks_seen": 0, "stopped_gracefully": False, "exit_code": None,
           "lock_released": False, "pid_gone": False, "elapsed_s": 0.0,
           "error": ""}
    with open(out, "w", encoding="utf-8") as fh:
        p = subprocess.Popen(
            [PY, "-u", "-m", "ai_trader.paper_runtime", "--paper",
             "--session-minutes", "5", "--segment-interval", "0.05",
             "--out", str(cdir)],
            cwd=str(ROOT), env=env(), stdout=fh, stderr=subprocess.STDOUT)
    rec["pid"] = p.pid
    rec["started"] = True

    lock = cdir / "paper_runtime.lock"
    hb = None
    session_dirs = list(cdir.glob("run-session-*"))

    def ready():
        nonlocal hb, session_dirs
        if not session_dirs:
            session_dirs = list(cdir.glob("run-session-*"))
        if session_dirs:
            hb = session_dirs[-1] / "heartbeat.json"
            return hb.exists() and lock.exists()
        return False

    if not wait_for(ready, timeout=30):
        rec["error"] = "未在 30s 内进入 READY（无心跳/锁）"
        p.kill()
        p.wait(timeout=20)
        rec["exit_code"] = p.returncode
        rec["elapsed_s"] = round(time.monotonic() - t0, 2)
        return rec
    rec["ready"] = True

    # 等 ticks 增长（证明真的在跑，不是空转）
    def progressed():
        try:
            d = json.loads(hb.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        rec["ticks_seen"] = max(rec["ticks_seen"], int(d.get("ticks", 0) or 0))
        return rec["ticks_seen"] > 0

    wait_for(progressed, timeout=10)

    # 请求优雅停止
    sd = session_dirs[-1] if session_dirs else None
    if sd is not None:
        (sd / "stop.request").write_text(f"cycle={i}\n", encoding="utf-8")
    rec["stopped_gracefully"] = bool(sd)

    try:
        p.wait(timeout=40)
    except subprocess.TimeoutExpired:
        rec["error"] = "优雅停止超时（40s）"
        p.kill()
        p.wait(timeout=10)
    rec["exit_code"] = p.returncode
    rec["lock_released"] = not lock.exists()
    # Windows 上进程对象在被完全回收前，OpenProcess 仍可能成功几百毫秒
    rec["pid_gone"] = bool(wait_for(lambda: not pid_alive(p.pid), timeout=5.0,
                                    interval=0.05))
    rec["elapsed_s"] = round(time.monotonic() - t0, 2)
    rec["ok"] = (rec["exit_code"] == 0 and rec["lock_released"]
                 and rec["pid_gone"] and rec["ticks_seen"] > 0
                 and not rec["error"])
    return rec


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    root = Path(sys.argv[2]) if len(sys.argv) > 2 else (ROOT / "data" / "paper_runs" / "startstop100")
    root.mkdir(parents=True, exist_ok=True)
    out = root / "cycles.jsonl"
    rows = []
    t0 = time.monotonic()
    for i in range(1, n + 1):
        r = run_cycle(i, root)
        rows.append(r)
        with open(out, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        if i % 10 == 0 or not r["ok"]:
            print(json.dumps({"cycle": i, "ok": r["ok"], "ms": r["elapsed_s"],
                              "ticks": r["ticks_seen"], "err": r["error"]},
                             ensure_ascii=False), flush=True)
    ok = sum(1 for r in rows if r["ok"])
    summary = {"cycles": len(rows), "ok": ok, "failed": len(rows) - ok,
               "elapsed_s": round(time.monotonic() - t0, 1),
               "max_cycle_s": max((r["elapsed_s"] for r in rows), default=0),
               "avg_cycle_s": round(sum(r["elapsed_s"] for r in rows) / max(1, len(rows)), 2),
               "failures": [r for r in rows if not r["ok"]][:20]}
    (root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("cycles", "ok", "failed", "elapsed_s")},
                     ensure_ascii=False))
    return 0 if ok == len(rows) else 3


if __name__ == "__main__":
    raise SystemExit(main())
