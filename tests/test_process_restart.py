"""跨进程重启测试（真实子进程，不是"同进程重建对象"）。

这是审计要求里最重要的一条：
**急停之后重启，系统必须仍然是停止的。**

做法：用 `subprocess` 起真实的 `python -m ai_trader.cli`，每次都是全新解释器进程，
共享同一个 data 目录。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _env() -> dict:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONUTF8"] = "1"
    return env


def _run(cfg_path: Path, *args: str, timeout: int = 90):
    cmd = [sys.executable, "-m", "ai_trader.cli", "--config", str(cfg_path), *args]
    return subprocess.run(cmd, cwd=str(ROOT), env=_env(), capture_output=True,
                          text=True, timeout=timeout)


def _write_cfg(tmp_path: Path) -> Path:
    """用一份临时 YAML 指向临时 data 目录，绝不碰项目真实数据。

    US-P1：改为**基于生产配置**构造再覆盖路径。
    原因：`scan` 现在有配置守卫前置检查，手写的最小配置会因为缺少
    `market_rules` 而被拒绝（这正是守卫要拦的情况）。
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(ROOT / "src"))
    from ai_trader.config import load_config

    cfg = json.loads(json.dumps(load_config(use_dotenv=False).raw))
    cfg.update({
        "system": {"name": "audit-restart", "mode": "paper",
                   "data_dir": str(data_dir), "log_dir": str(tmp_path / "logs")},
        "safety": {"live_trading": False, "order_execution_enabled": False,
                   "paper_trading": True, "kill_switch": False,
                   "live_ack_phrase": "",
                   "required_live_ack": "I UNDERSTAND REAL MONEY IS AT RISK"},
        "markets": {"hk": {"enabled": True, "currency": "HKD", "lot_default": 100},
                    "us": {"enabled": True, "currency": "USD", "lot_default": 1}},
        "universe": [{"code": "hk00700", "name": "腾讯控股", "market": "hk"}],
        "ai": {"provider": "heuristic",
               "constraints": {"min_confidence": 0.6, "require_stop_loss": True,
                               "max_position_pct": 0.15}},
        "risk": {"max_positions": 8, "max_position_pct": 0.15,
                 "max_order_notional": 50000, "min_order_notional": 100,
                 "risk_per_trade_pct": 0.01, "max_daily_loss_pct": 0.03,
                 "duplicate_window_seconds": 300, "order_timeout_seconds": 600,
                 "enforce_market_hours": False},
        "paper": {"initial_cash": {"HKD": 1000000.0}},
        "audit": {"db_path": str(tmp_path / "audit.db"),
                  "jsonl_path": str(tmp_path / "audit.jsonl"),
                  "hash_chain": True},
        "fosun": {"base_url": "", "sdk_type": "ops"},
        "strategy": {"kline_ktype": "day", "kline_num": 120,
                     "trend_filter": {"enabled": True, "fast_ma": 20,
                                      "slow_ma": 60, "min_atr_pct": 0.005}},
        "web": {"host": "127.0.0.1", "port": 0},
    })
    # 不覆盖 market_rules：走**生产口径**，顺带验证"守卫放行时跨进程行为不变"
    path = tmp_path / "config.yaml"
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return path


def test_halt_survives_real_process_restart(tmp_path):
    cfg = _write_cfg(tmp_path)
    data = tmp_path / "data"

    # 1) 全新进程：触发急停
    killed = _run(cfg, "kill", "--reason", "跨进程测试急停")
    assert killed.returncode == 0, killed.stderr
    payload = json.loads(killed.stdout)
    assert payload["kill_switch"] is True
    assert payload["persisted"] is True
    assert (data / "HALT.json").exists(), "哨兵文件必须落盘"

    # 2) 又一个全新进程：必须仍然停止
    status = _run(cfg, "status")
    assert status.returncode == 0, status.stderr
    st = json.loads(status.stdout)
    assert st["safety"]["kill_switch"] is True
    assert st["safety"]["halt_source"] == "sentinel"
    assert "跨进程测试急停" in st["safety"]["halt_reason"]

    # 3) 全新进程里执行扫描：必须被阻断，且不产生任何委托
    scan = _run(cfg, "scan")
    assert scan.returncode == 0, scan.stderr
    rep = json.loads(scan.stdout)
    assert rep["blocked_reason"], "重启后扫描仍然必须被阻断"
    assert rep["outcomes"] == []

    # 4) 错误确认短语：拒绝
    bad = _run(cfg, "resume", "--confirm", "resume-trading")
    assert bad.returncode == 2
    still = json.loads(_run(cfg, "status").stdout)
    assert still["safety"]["kill_switch"] is True

    # 5) 正确短语：解除
    good = _run(cfg, "resume", "--confirm", "RESUME-TRADING")
    assert good.returncode == 0, good.stderr
    after = json.loads(_run(cfg, "status").stdout)
    assert after["safety"]["kill_switch"] is False
    assert not (data / "HALT.json").exists()


def test_corrupt_sentinel_fails_closed_across_processes(tmp_path):
    cfg = _write_cfg(tmp_path)
    (tmp_path / "data" / "HALT.json").write_text("not-json", encoding="utf-8")

    status = _run(cfg, "status")
    st = json.loads(status.stdout)
    assert st["safety"]["kill_switch"] is True
    assert st["safety"]["halt_store_failed"] is True

    scan = _run(cfg, "scan")
    rep = json.loads(scan.stdout)
    assert rep["blocked_reason"]


def test_paper_state_persists_across_processes(tmp_path):
    """回归：正常路径下重启不应丢数据（急停以外的东西照常持久化）。"""
    cfg = _write_cfg(tmp_path)
    first = _run(cfg, "demo")            # demo 用独立沙盒，不应影响主 data
    assert first.returncode == 0

    status = _run(cfg, "status")
    st = json.loads(status.stdout)
    assert st["broker"]["kind"] == "paper"
    assert st["audit"]["chain"]["ok"] is True
