"""在"不可预期时刻"强杀：覆盖 WAL 写入中、心跳更新中等窗口。

固定注入点只能覆盖已知位置；真实崩溃可能落在任意写入中间。
这里用定时强杀模拟，再用恢复扫描验证账务仍然自洽。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
if not Path(PY).exists():
    PY = sys.executable


def env() -> dict:
    e = dict(os.environ)
    e["PYTHONPATH"] = str(ROOT / "src")
    e.pop("PAPER_CRASH_AT", None)
    return e


def run_and_kill(tmp_path: Path, after_seconds: float) -> subprocess.Popen:
    p = subprocess.Popen(
        [PY, "-u", "-m", "ai_trader.paper_runtime", "--paper",
         "--session-minutes", "5", "--segment-interval", "0.02",
         "--out", str(tmp_path), "--lock-root", str(tmp_path)],
        cwd=str(ROOT), env=env(), stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL)
    time.sleep(after_seconds)
    p.kill()
    p.wait(timeout=30)
    return p


def recover(tmp_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PY, "-m", "ai_trader.paper_runtime", "--paper", "--replay", "normal_day",
         "--run-dir", str(tmp_path / "recover"), "--recover-only"],
        cwd=str(ROOT), env=env(), capture_output=True, text=True, timeout=180)


@pytest.mark.parametrize("delay", [0.7, 1.4, 2.3])
def test_kill_at_arbitrary_moment_then_recover(tmp_path, delay):
    run_and_kill(tmp_path, delay)
    # 现场必须留下可读的 JSON（不能是半截文件）
    states = list(tmp_path.glob("run-session-*/paper_state.json"))
    for s in states:
        json.loads(s.read_text(encoding="utf-8"))
    r = recover(tmp_path)
    assert r.returncode == 0, r.stdout[-400:]


def test_kill_leaves_no_duplicate_fills(tmp_path):
    """等到**确实产生成交**后再强杀，确保这条断言不是空跑。"""
    p = subprocess.Popen(
        [PY, "-u", "-m", "ai_trader.paper_runtime", "--paper",
         "--session-minutes", "5", "--segment-interval", "0.02",
         "--out", str(tmp_path), "--lock-root", str(tmp_path)],
        cwd=str(ROOT), env=env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    led = None
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            cands = list(tmp_path.glob("run-session-*/paper_fills.jsonl"))
            if cands and cands[0].exists() and cands[0].stat().st_size > 0:
                led = cands[0]
                break
            time.sleep(0.05)
        assert led is not None, "30s 内未产生成交，无法验证重复性"
    finally:
        p.kill()
        p.wait(timeout=30)
    ids = [json.loads(x)["fill_id"]
           for x in led.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert ids, "账本为空"
    assert len(ids) == len(set(ids)), "账本出现重复 fill_id"


def test_kill_then_restart_can_start(tmp_path):
    run_and_kill(tmp_path, 1.0)
    r = subprocess.run(
        [PY, "-m", "ai_trader.paper_runtime", "--paper", "--replay", "mwcb",
         "--run-dir", str(tmp_path / "next")],
        cwd=str(ROOT), env=env(), capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, (r.stdout + r.stderr)[-400:]
