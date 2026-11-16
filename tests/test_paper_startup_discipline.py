"""启动纪律：必须 --paper、全局 P2 开关必须关、实盘通道必须关。"""
from __future__ import annotations

import copy
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
if not Path(PY).exists():
    PY = sys.executable


def cli(args, timeout=60):
    return subprocess.run([PY, "-m", "ai_trader.paper_runtime", *args],
                          cwd=str(ROOT), capture_output=True, text=True, timeout=timeout,
                          env={**__import__("os").environ,
                               "PYTHONPATH": str(ROOT / "src")})


def test_cli_refuses_without_paper_flag():
    r = cli([])
    assert r.returncode == 2
    assert "--paper" in (r.stderr or "") + (r.stdout or "")


def test_cli_refuses_replay_without_paper_flag():
    r = cli(["--replay", "normal_day"])
    assert r.returncode == 2


def test_cli_unknown_scenario_exits_2():
    r = cli(["--paper", "--replay", "nope"])
    assert r.returncode == 2
    assert "未知场景" in (r.stdout or "") + (r.stderr or "")


def test_hard_guard_rejects_global_phase2_enabled():
    """若全局 us_phase2.enabled 被打开，--paper 必须拒绝启动。"""
    import copy as _c
    from ai_trader.config import load_config
    from ai_trader.paper_runtime.__main__ import _hard_guards

    cfg = load_config()
    bad = _c.deepcopy(cfg)
    bad.raw["us_phase2"] = dict(bad.raw.get("us_phase2") or {})
    bad.raw["us_phase2"]["enabled"] = True
    problems = _hard_guards(bad)
    assert any("us_phase2" in p for p in problems)


def test_hard_guard_ok_on_current_config():
    from ai_trader.config import load_config
    from ai_trader.paper_runtime.__main__ import _hard_guards
    assert _hard_guards(load_config()) == []


def test_hard_guard_rejects_live_trading():
    import copy as _c
    from ai_trader.config import load_config
    from ai_trader.paper_runtime.__main__ import _hard_guards

    cfg = load_config()
    bad = _c.deepcopy(cfg)
    bad.gate.state.paper_trading = False
    assert any("PAPER_TRADING" in p for p in _hard_guards(bad))
