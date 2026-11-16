"""进程级故障注入：真的杀进程，再验证重启后账还对不对。

要点：这些用例**不是**测 Python 异常，而是 ``os._exit(9)`` ——
不执行 finally、不 flush 缓冲、不做业务清理，等价于断电。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
if not Path(PY).exists():                       # 兼容非 Windows CI
    PY = sys.executable

SCENARIO = "normal_day"


def _env(crash_at: str = "") -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    env.pop("PAPER_CRASH_AT", None)
    if crash_at:
        env["PAPER_CRASH_AT"] = crash_at
    return env


def run_cli(args, *, crash_at="", timeout=180):
    return subprocess.run(
        [PY, "-m", "ai_trader.paper_runtime", *args],
        cwd=str(ROOT), env=_env(crash_at), capture_output=True, text=True,
        timeout=timeout)


def replay(run_dir: Path, *, crash_at="", recover_only=False):
    args = ["--paper", "--replay", SCENARIO, "--run-dir", str(run_dir)]
    if recover_only:
        args.append("--recover-only")
    return run_cli(args, crash_at=crash_at)


def report_of(run_dir: Path) -> dict:
    p = Path(run_dir) / f"{SCENARIO}-0" / "run_report.json"
    return json.loads(p.read_text(encoding="utf-8"))


def state_of(run_dir: Path) -> dict:
    p = Path(run_dir) / f"{SCENARIO}-0" / "paper_state.json"
    return json.loads(p.read_text(encoding="utf-8"))


def ledger_lines(run_dir: Path) -> int:
    p = Path(run_dir) / f"{SCENARIO}-0" / "paper_fills.jsonl"
    if not p.exists():
        return 0
    return len([x for x in p.read_text(encoding="utf-8").splitlines() if x.strip()])


# ---------------------------------------------------------------------- #
def test_crash_before_order_leaves_no_order(tmp_path):
    r = replay(tmp_path, crash_at="before_order")
    assert r.returncode == 9, r.stderr[-500:]
    st = state_of(tmp_path)
    assert st["orders"] == {}
    assert st["positions"] == {}

    rec = replay(tmp_path, recover_only=True)
    assert rec.returncode == 0
    rep = report_of(tmp_path)["report"]
    assert rep["recovery"]["ok"] is True
    assert rep["recovery"]["backfilled"] == 0


def test_crash_after_risk_leaves_no_ghost_order(tmp_path):
    r = replay(tmp_path, crash_at="after_risk")
    assert r.returncode == 9
    st = state_of(tmp_path)
    assert st["orders"] == {}, "风控通过但未下单，不得出现幽灵订单"
    assert st["cash"]["USD"] == pytest.approx(100000.0)


def test_crash_after_fill_write_then_recover(tmp_path):
    r = replay(tmp_path, crash_at="after_fill_write")
    assert r.returncode == 9
    st = state_of(tmp_path)
    fills_in_state = [o for o in st["orders"].values() if o["status"] == "filled"]
    assert len(fills_in_state) == 1, "成交应已落盘"
    assert ledger_lines(tmp_path) == 0, "账本此时还没写（这就是崩溃窗口）"

    rec = replay(tmp_path, recover_only=True)
    assert rec.returncode == 0, rec.stderr[-800:]
    rep = report_of(tmp_path)["report"]
    assert rep["recovery"]["backfilled"] == 1
    assert rep["recovery"]["orphans"] == 0
    assert rep["recovery"]["ok"] is True
    assert ledger_lines(tmp_path) == 1


def test_recovery_is_idempotent(tmp_path):
    replay(tmp_path, crash_at="after_fill_write")
    replay(tmp_path, recover_only=True)
    first = ledger_lines(tmp_path)
    replay(tmp_path, recover_only=True)
    assert ledger_lines(tmp_path) == first == 1
    rep = report_of(tmp_path)["report"]
    assert rep["recovery"]["backfilled"] == 0


def test_crash_before_fill_write_loses_nothing(tmp_path):
    r = replay(tmp_path, crash_at="before_fill_write")
    assert r.returncode == 9
    st = state_of(tmp_path)
    assert all(o["status"] != "filled" for o in st["orders"].values())
    rec = replay(tmp_path, recover_only=True)
    assert rec.returncode == 0
    assert report_of(tmp_path)["report"]["recovery"]["backfilled"] == 0


def test_crash_after_order_create_on_unfilled_order(tmp_path):
    """挂单（未成交）落盘后崩溃：订单在、成交不在，恢复后不得凭空补成交。"""
    probe = tmp_path / "probe_order.py"
    probe.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "from ai_trader.broker.paper import PaperBroker\n"
        "from ai_trader.broker.base import OrderRequest\n"
        "b = PaperBroker(state_path=Path(sys.argv[1]) / 'p.json',\n"
        "                initial_cash={'USD': 100000.0}, commission_bps=0,\n"
        "                min_commission=0, slippage_bps=0)\n"
        "b.update_quote('usAAPL', 100.0) if hasattr(b, 'update_quote') else None\n"
        "r = b.place_order(OrderRequest(symbol='usAAPL', market='us', side='buy',\n"
        "                              qty=1, price=1.0, order_type='limit'))\n"
        "print('status', r.status)\n", encoding="utf-8")
    env = _env("after_order_create")
    p2 = subprocess.run([PY, str(probe), str(tmp_path)], cwd=str(ROOT), env=env,
                        capture_output=True, text=True, timeout=120)
    assert p2.returncode == 9, p2.stderr[-400:]
    st = json.loads((tmp_path / "p.json").read_text(encoding="utf-8"))
    assert len(st["orders"]) == 1
    assert all(o["status"] != "filled" for o in st["orders"].values())


def test_filled_path_does_not_fire_order_create_hook(tmp_path):
    """成交单走 after_fill_write，不会被 after_order_create 误伤。"""
    r = replay(tmp_path, crash_at="after_order_create")
    assert r.returncode == 0, "normal_day 全是成交单，不应触发挂单注入点"


def test_orphan_ledger_fails_closed(tmp_path):
    """账本里有、券商状态没有 → 无法判定，必须拒绝进入 READY。"""
    replay(tmp_path, recover_only=True)
    led = Path(tmp_path) / f"{SCENARIO}-0" / "paper_fills.jsonl"
    ghost = {"fill_id": "FGHOST", "order_id": "OGHOST", "symbol": "USAAPL",
             "side": "buy", "qty": 1.0, "price": 100.0, "fee": 0.0,
             "currency": "USD", "ts": "2026-11-16T10:00:00-05:00", "source": "broker"}
    led.write_text(json.dumps(ghost) + "\n", encoding="utf-8")
    r = replay(tmp_path, recover_only=True)
    assert r.returncode == 3, r.stdout[-400:]
    rep = report_of(tmp_path)["report"]
    assert rep["recovery"]["ok"] is False
    assert rep["recovery"]["orphans"] == 1


def test_torn_ledger_tail_tolerated(tmp_path):
    replay(tmp_path, crash_at="after_fill_write")
    replay(tmp_path, recover_only=True)
    led = Path(tmp_path) / f"{SCENARIO}-0" / "paper_fills.jsonl"
    with open(led, "a", encoding="utf-8") as fh:
        fh.write('{"fill_id": "FT", "order_id": "OT"')
    r = replay(tmp_path, recover_only=True)
    assert r.returncode == 0
    rep = report_of(tmp_path)["report"]
    assert rep["ledger"]["torn_isolated"] == 1
    assert rep["recovery"]["ok"] is True


def test_unknown_crash_point_reports_and_exits(tmp_path):
    r = replay(tmp_path, crash_at="not-a-real-point")
    assert r.returncode == 2
    assert "未知注入点" in r.stderr


def test_no_crash_when_env_absent(tmp_path):
    r = replay(tmp_path)
    assert r.returncode == 0, r.stderr[-500:]
    assert report_of(tmp_path)["ok"] is True


def test_crash_points_are_whitelisted():
    from ai_trader.paper_runtime.fault_inject import POINTS
    assert set(POINTS) == {
        "before_order", "after_order_create", "after_risk", "before_fill_write",
        "after_fill_write", "after_position", "after_settlement"}
