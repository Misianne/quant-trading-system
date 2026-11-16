"""真实执行 Windows 启停脚本（不是语法解析）。

覆盖：项目外目录 / 中文目录 / 含空格目录 / CMD 调用 / PowerShell 调用 /
重复启动 / 无进程时停止 / 运行中查状态 / 异常退出后识别 / 启动失败的错误码。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = []


def rec(name, ok, detail):
    RESULTS.append({"test": name, "ok": bool(ok), "detail": str(detail)[:400]})
    print(("PASS " if ok else "FAIL ") + name + " :: " + str(detail)[:200], flush=True)


def _dec(b: bytes) -> str:
    """Windows 控制台输出可能是 GBK，也可能被写成 UTF-8。"""
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("latin-1", errors="replace")


class R:
    def __init__(self, cp):
        self.returncode = cp.returncode
        self.stdout = _dec(cp.stdout or b"")
        self.stderr = _dec(cp.stderr or b"")


def _run_to_file(cmd, cwd, timeout):
    """把子进程输出写文件而不是管道。

    后台启动的 Paper 进程会继承 stdout 句柄；如果用管道，``communicate()``
    会一直等到**所有**持有者关闭管道（也就是后台进程退出）—— 表现就是卡死。
    写文件没有 EOF 等待问题。
    """
    tmp = Path(tempfile.mkdtemp(prefix="cmdout-"))
    out, err = tmp / "o.txt", tmp / "e.txt"
    with open(out, "wb") as fo, open(err, "wb") as fe:
        cp = subprocess.run(cmd, cwd=str(cwd), stdout=fo, stderr=fe, timeout=timeout)
    fake = type("CP", (), {"returncode": cp.returncode,
                           "stdout": out.read_bytes(),
                           "stderr": err.read_bytes()})
    return R(fake())


def run_cmd(args, cwd, timeout=90):
    return _run_to_file(["cmd", "/c", *args], cwd, timeout)


def run_ps(args, cwd, timeout=90):
    return _run_to_file(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", *args],
        cwd, timeout)


def read_pid(root: Path) -> int:
    p = root / "data" / "paper_runs" / "supervisor.pid"
    try:
        return int(p.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return 0


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    h = k32.OpenProcess(0x1000, False, int(pid))
    if not h:
        return False
    try:
        code = wintypes.DWORD(0)
        k32.GetExitCodeProcess(h, ctypes.byref(code))
        return int(code.value) == 259
    finally:
        k32.CloseHandle(h)


def wait(pred, timeout, interval=0.2):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        time.sleep(interval)
    return None


def main() -> int:
    outside = Path(tempfile.mkdtemp(prefix="outside-"))
    cn = Path(tempfile.gettempdir()) / "星财富 测试 目录"
    cn.mkdir(parents=True, exist_ok=True)

    start_cmd = str(ROOT / "paper_start.cmd")
    stop_cmd = str(ROOT / "paper_stop.cmd")
    status_cmd = str(ROOT / "paper_status.cmd")
    start_ps = str(ROOT / "scripts" / "paper_start.ps1")
    stop_ps = str(ROOT / "scripts" / "paper_stop.ps1")

    # 0) 无进程时停止：应返回 0 且不报错
    r = run_cmd([stop_cmd], outside)
    rec("stop-when-idle", r.returncode == 0, f"rc={r.returncode} out={r.stdout.strip()[:120]}")

    # 1) 从项目外目录用 CMD 启动
    r = run_cmd([start_cmd, "-Minutes", "5"], outside)
    ok_start = r.returncode == 0
    pid = 0
    if ok_start:
        pid = wait(lambda: read_pid(ROOT) or 0, timeout=30) or 0
    rec("start-from-outside-cmd", ok_start and pid > 0,
        f"rc={r.returncode} pid={pid} out={r.stdout.strip()[:150]}")

    # 2) 运行中查状态（项目外 + 中文含空格目录）
    r = run_cmd([status_cmd], cn)
    ok_status = r.returncode == 0 and "RUNNING" in r.stdout
    rec("status-running-cn-space-cwd", ok_status,
        f"rc={r.returncode} head={r.stdout.strip()[:160]}")

    # 3) 重复启动必须被拒绝（非 0 且有提示）
    r = run_cmd([start_cmd, "-Minutes", "5"], outside)
    rec("duplicate-start-blocked", r.returncode != 0,
        f"rc={r.returncode} out={r.stdout.strip()[:160]}")

    # 4) 通过 PowerShell 直接调用 stop（覆盖“普通 PowerShell”路径）
    r = run_ps([stop_ps, "-TimeoutSeconds", "30"], cn)
    gone = wait(lambda: (not pid_alive(pid)) if pid else False, timeout=45)
    rec("stop-via-powershell", r.returncode == 0 and bool(gone),
        f"rc={r.returncode} pid_alive={pid_alive(pid)} out={r.stdout.strip()[:150]}")

    # 5) 停止后 PID 文件应清理、状态应识别为 DEAD/NO_RUNS
    r = run_cmd([status_cmd], outside)
    verdict_line = [ln for ln in r.stdout.splitlines() if '"verdict"' in ln]
    rec("status-after-stop", r.returncode in (0, 6) and
        any(("DEAD" in x or "NO_RUNS" in x) for x in verdict_line),
        f"rc={r.returncode} {verdict_line[:1]}")

    # 6) 再从中文/空格目录用 CMD 启动一次，验证 cwd 不影响
    r = run_cmd([start_cmd, "-Minutes", "5"], cn)
    pid2 = wait(lambda: read_pid(ROOT) or 0, timeout=30) or 0 if r.returncode == 0 else 0
    rec("start-from-cn-space-cwd", r.returncode == 0 and pid2 > 0,
        f"rc={r.returncode} pid={pid2}")

    # 7) 异常退出后状态识别：强杀进程，status 应报 STALLED 或 DEAD（不能报 RUNNING）
    if pid2:
        subprocess.run(["taskkill", "/F", "/PID", str(pid2)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wait(lambda: not pid_alive(pid2), timeout=15)
        r = run_cmd([status_cmd], outside)
        txt = r.stdout
        ok_abn = any(x in txt for x in ("DEAD", "STALLED", "NO_RUNS"))
        rec("status-after-abnormal-exit", ok_abn, f"verdict={[l for l in txt.splitlines() if 'verdict' in l]}")

    # 8) 启动失败：非法场景名 → 非 0 错误码 + 明确信息
    r = run_cmd([start_cmd, "-Replay", "no_such_scenario"], outside)
    rec("start-invalid-arg-fails", r.returncode != 0,
        f"rc={r.returncode} out={(r.stdout + r.stderr).strip()[:200]}")

    # 清理：确保没有残留
    run_cmd([stop_cmd, "-Force"], outside)

    summary = {"results": RESULTS, "passed": sum(1 for x in RESULTS if x["ok"]),
               "total": len(RESULTS)}
    outp = ROOT / "data" / "paper_runs" / "cmd-script-verification.json"
    outp.parent.mkdir(parents=True, exist_ok=True)
    outp.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": summary["passed"], "total": summary["total"],
                      "out": str(outp)}, ensure_ascii=False))
    return 0 if summary["passed"] == summary["total"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
