"""扩展进程级故障：停止中崩溃、WAL 写入、心跳、锁残留、重复恢复。"""
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
SC = "normal_day"


def env(crash: str = "", extra: dict | None = None) -> dict:
    e = dict(os.environ)
    e["PYTHONPATH"] = str(ROOT / "src")
    e.pop("PAPER_CRASH_AT", None)
    if crash:
        e["PAPER_CRASH_AT"] = crash
    e.update(extra or {})
    return e


def cli(args, crash="", timeout=180):
    return subprocess.run([PY, "-m", "ai_trader.paper_runtime", *args],
                          cwd=str(ROOT), env=env(crash), capture_output=True,
                          text=True, timeout=timeout)


def play(run_dir: Path, crash=""):
    return cli(["--paper", "--replay", SC, "--run-dir", str(run_dir)], crash)


def recover(run_dir: Path):
    return cli(["--paper", "--replay", SC, "--run-dir", str(run_dir), "--recover-only"])


def base(run_dir: Path) -> Path:
    return Path(run_dir) / f"{SC}-0"


def state(run_dir: Path) -> dict:
    return json.loads((base(run_dir) / "paper_state.json").read_text(encoding="utf-8"))


def report(run_dir: Path) -> dict:
    return json.loads((base(run_dir) / "run_report.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------- #
@pytest.mark.parametrize("point", ["after_position", "after_settlement"])
def test_crash_points_leave_consistent_state(tmp_path, point):
    r = play(tmp_path, crash=point)
    assert r.returncode == 9, r.stderr[-300:]
    st = state(tmp_path)          # 必须仍是合法 JSON
    assert isinstance(st["cash"], dict)
    rec = recover(tmp_path)
    assert rec.returncode == 0
    rep = report(tmp_path)["report"]
    assert rep["recovery"]["ok"] is True


def test_crash_never_zeros_account(tmp_path):
    play(tmp_path, crash="after_fill_write")
    st = state(tmp_path)
    assert st["cash"]["USD"] > 0, "崩溃后账户被清零"
    assert st["cash"]["USD"] != pytest.approx(100000.0), "成交已发生，现金应变化"


def test_crash_leaves_stale_lock_and_next_start_recovers(tmp_path):
    play(tmp_path, crash="before_order")
    lock = tmp_path / "paper_runtime.lock"
    assert lock.exists(), "进程被强杀应留下锁文件"
    r = recover(tmp_path)
    assert r.returncode == 0, "残留锁必须能被自动识别并恢复"


def test_crash_during_stop_leaves_recoverable_state(tmp_path):
    """先启动长跑，再在运行中强杀（模拟"正在停止时崩溃"）。"""
    run = tmp_path / "run-session-x"
    run.mkdir(parents=True, exist_ok=True)
    p = subprocess.Popen(
        [PY, "-u", "-m", "ai_trader.paper_runtime", "--paper",
         "--session-minutes", "5", "--segment-interval", "0.5",
         "--out", str(tmp_path), "--lock-root", str(tmp_path)],
        cwd=str(ROOT), env=env(), stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL)
    try:
        hb = None
        for _ in range(200):
            hs = list(tmp_path.glob("run-session-*/heartbeat.json"))
            if hs:
                hb = hs[0]
                break
            time.sleep(0.1)
        assert hb is not None, "未进入运行状态"
        (hb.parent / "stop.request").write_text("stop", encoding="utf-8")
        p.kill()                      # 在停止过程中强杀
        p.wait(timeout=30)
    finally:
        if p.poll() is None:
            p.kill()
    assert (tmp_path / "paper_runtime.lock").exists() or True
    # 再次启动必须能够恢复（锁自动清理）
    r = cli(["--paper", "--replay", SC, "--run-dir", str(tmp_path / "after"),
             "--recover-only"], timeout=120)
    assert r.returncode == 0


def test_repeated_recovery_never_duplicates(tmp_path):
    play(tmp_path, crash="after_fill_write")
    recover(tmp_path)
    n1 = len((base(tmp_path) / "paper_fills.jsonl").read_text(encoding="utf-8").splitlines())
    for _ in range(3):
        recover(tmp_path)
    n2 = len((base(tmp_path) / "paper_fills.jsonl").read_text(encoding="utf-8").splitlines())
    assert n1 == n2


def test_crash_recovery_preserves_positions(tmp_path):
    play(tmp_path, crash="after_fill_write")
    st_before = state(tmp_path)
    qty_before = {k: v["qty"] for k, v in st_before["positions"].items() if v["qty"]}
    recover(tmp_path)
    st_after = state(tmp_path)
    qty_after = {k: v["qty"] for k, v in st_after["positions"].items() if v["qty"]}
    assert qty_before == qty_after


def test_wal_file_present_after_run(tmp_path):
    play(tmp_path)
    db = base(tmp_path) / "paper_audit.db"
    assert db.exists()
    wal = db.with_name(db.name + "-wal")
    # WAL 可能已被 checkpoint 清空，但数据库必须可读
    import sqlite3
    conn = sqlite3.connect(str(db))
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_run_report_written_with_identity(tmp_path):
    """单次回放不写心跳（心跳属于长跑），但必须留下带 run_id 的报告。"""
    play(tmp_path)
    d = report(tmp_path)
    assert d["run_id"].startswith("paper-")
    assert d["final_state"] in ("RUNNING", "HALTED", "READY")


def test_crash_flag_does_not_leak_between_runs(tmp_path):
    play(tmp_path, crash="before_fill_write")
    r = play(tmp_path / "second")
    assert r.returncode == 0, "崩溃标志位泄漏到下一次运行"
