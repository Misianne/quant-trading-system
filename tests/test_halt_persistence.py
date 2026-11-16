"""P0-2：急停持久化测试。

覆盖：
* 急停落盘 → 重启后仍然停止
* 解除需要精确确认短语
* 哨兵损坏 → 按已停止处理（fail-closed）
* 哨兵写不进去 → 仍然停止 + 拒绝交易
* 重启后的 scan 被阻断
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from ai_trader.audit.journal import AuditJournal
from ai_trader.broker.paper import PaperBroker
from ai_trader.config import AppConfig, build_gate
from ai_trader.risk.controller import OrderIntent, Portfolio, RiskController
from ai_trader.safety.halt import (
    HALT_FILENAME,
    RESUME_PHRASE,
    HaltStore,
    HaltStoreError,
)
from ai_trader.strategy.engine import StrategyEngine
from tests.conftest import WEEKDAY_1000, make_cfg
from tests.helpers import FixedSignalModel


def _intent(qty=100, price=300.0):
    return OrderIntent(symbol="hk00700", side="buy", qty=qty, price=price,
                       market="hk", currency="HKD", stop_loss=290.0)


def _pf():
    return Portfolio(currency="HKD", equity=1_000_000.0, cash=1_000_000.0)


# ---------------------------------------------------------------- 基础读写
def test_halt_creates_sentinel_file(tmp_path):
    store = HaltStore(tmp_path)
    assert store.exists() is False
    st = store.halt("测试急停", actor="pytest")
    assert st.halted is True
    assert store.exists() is True
    raw = json.loads((tmp_path / HALT_FILENAME).read_text(encoding="utf-8"))
    assert raw["halted"] is True
    assert raw["reason"] == "测试急停"
    assert raw["actor"] == "pytest"


def test_halt_survives_restart(tmp_path):
    """核心回归：进程重启后必须仍然是停止状态。"""
    store = HaltStore(tmp_path / "data")     # 与 cfg.data_dir 一致
    store.halt("崩溃前按下的急停", actor="ops")

    # 模拟重启：重新从磁盘构建配置与风控
    cfg = make_cfg(tmp_path)
    assert cfg.gate.state.kill_switch is True
    assert cfg.gate.state.halt_source == "sentinel"
    assert "崩溃前按下的急停" in cfg.gate.state.halt_reason

    risk = RiskController(cfg, cfg.gate)
    assert risk.gate.state.kill_switch is True

    d = risk.evaluate(_intent(), _pf(), last_price=300.0, now=WEEKDAY_1000)
    assert d.approved is False
    assert "kill_switch" in d.failed_checks


def test_scan_blocked_after_restart(tmp_path):
    HaltStore(tmp_path / "data").halt("重启前急停", actor="ops")
    cfg = make_cfg(tmp_path, **{"risk.enforce_market_hours": False})
    journal = AuditJournal(tmp_path / "audit.db", tmp_path / "audit.jsonl")
    broker = PaperBroker(state_path=tmp_path / "paper.json")
    risk = RiskController(cfg, cfg.gate)
    engine = StrategyEngine(cfg, broker, FixedSignalModel(), risk, journal,
                            stops_path=tmp_path / "stops.json")
    report = engine.scan(["hk00700"], now=WEEKDAY_1000)
    assert report.blocked_reason
    assert report.outcomes == []
    assert broker.get_positions() == []
    journal.close()


# ---------------------------------------------------------------- 解除
def test_resume_requires_exact_phrase(tmp_path):
    store = HaltStore(tmp_path)
    store.halt("x", actor="ops")
    for bad in ("", "resume-trading", "RESUME", "RESUME-TRADING ", None):
        with pytest.raises(HaltStoreError):
            store.resume(bad)          # type: ignore[arg-type]
        assert store.exists() is True, f"错误短语 {bad!r} 竟然解除了急停"


def test_resume_with_phrase_clears_sentinel(tmp_path):
    store = HaltStore(tmp_path)
    store.halt("x", actor="ops")
    st = store.resume(RESUME_PHRASE, actor="pytest")
    assert st.halted is False
    assert store.exists() is False
    assert (tmp_path / "HALT.resumed.json").exists()


def test_risk_resume_requires_confirmation(tmp_path):
    cfg = make_cfg(tmp_path)
    risk = RiskController(cfg, cfg.gate)
    risk.halt("测试", actor="pytest")
    with pytest.raises(HaltStoreError):
        risk.resume("wrong")
    assert cfg.gate.state.kill_switch is True
    risk.resume(RESUME_PHRASE, actor="pytest")
    assert cfg.gate.state.kill_switch is False
    assert risk.evaluate(_intent(), _pf(), last_price=300.0,
                         now=WEEKDAY_1000).approved, "解除后应恢复"


# ---------------------------------------------------------------- 异常场景
def test_corrupt_sentinel_is_fail_closed(tmp_path):
    """哨兵损坏 → 必须当成"已停止"，而不是"没停过"。"""
    data = tmp_path / "data"
    data.mkdir(parents=True)
    (data / HALT_FILENAME).write_text("{ 这不是 json", encoding="utf-8")

    cfg = make_cfg(tmp_path)
    assert cfg.gate.state.kill_switch is True
    assert cfg.gate.state.halt_store_failed is True
    assert cfg.gate.state.halt_source == "storage_error"

    risk = RiskController(cfg, cfg.gate)
    d = risk.evaluate(_intent(), _pf(), last_price=300.0, now=WEEKDAY_1000)
    assert d.approved is False
    assert "halt_storage" in d.failed_checks or "kill_switch" in d.failed_checks


def test_sentinel_missing_halted_field_is_fail_closed(tmp_path):
    data = tmp_path / "data"
    data.mkdir(parents=True)
    (data / HALT_FILENAME).write_text(json.dumps({"reason": "no halted key"}),
                                      encoding="utf-8")
    cfg = make_cfg(tmp_path)
    assert cfg.gate.state.kill_switch is True
    assert cfg.gate.state.halt_store_failed is True


def test_write_failure_keeps_halted_and_blocks_trading(tmp_path):
    """哨兵写不进去时：内存停止 + 拒绝交易 + 明确抛错。"""
    cfg = make_cfg(tmp_path)
    # 让 data_dir 变成一个"文件"，mkdir 必然失败
    bad_dir = tmp_path / "data_as_file"
    bad_dir.write_text("not a directory", encoding="utf-8")
    broken = HaltStore(bad_dir)
    cfg.halt_store = broken
    risk = RiskController(cfg, cfg.gate)

    with pytest.raises(HaltStoreError):
        risk.halt("磁盘不可写", actor="pytest")

    assert risk.gate.state.kill_switch is True, "写失败也必须处于停止态"
    assert risk.gate.state.halt_store_failed is True
    # US-P1 N-3：写失败单独打标（读失败才是 halt_read_failed）
    assert risk.gate.state.halt_write_failed is True
    assert risk.gate.state.halt_read_failed is False
    d = risk.evaluate(_intent(), _pf(), last_price=300.0, now=WEEKDAY_1000)
    assert d.approved is False
    # 检查项按读写拆分：写失败 → halt_storage_write（默认阻断，未开 reduce-only）
    assert "halt_storage_write" in d.failed_checks
    assert "halt_storage_read" not in d.failed_checks


def test_gate_refuses_broker_connect_when_storage_failed(tmp_path):
    data = tmp_path / "data"
    data.mkdir(parents=True)
    (data / HALT_FILENAME).write_text("broken", encoding="utf-8")
    cfg = make_cfg(tmp_path)
    ok, reason = cfg.gate.can_connect_broker()
    assert ok is False
    assert "哨兵" in reason or "KILL_SWITCH" in reason


def test_config_kill_switch_also_marks_source(tmp_path):
    cfg = make_cfg(tmp_path, **{"safety.kill_switch": True})
    assert cfg.gate.state.kill_switch is True
    assert cfg.gate.state.halt_source in ("config", "sentinel")


def test_halt_resume_is_auditable_via_cli_status(tmp_path, capsys, monkeypatch):
    """CLI 的 kill 必须回显持久化结果与哨兵路径。"""
    from ai_trader import cli

    cfg = make_cfg(tmp_path)
    monkeypatch.setattr(cli, "_rt", lambda args: __import__(
        "ai_trader.runtime", fromlist=["build_runtime"]).build_runtime(cfg))
    rc = cli.main(["kill", "--reason", "CLI 测试急停"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["persisted"] is True
    assert out["kill_switch"] is True
    assert out["halt_file"].endswith(HALT_FILENAME)
    assert (Path(out["halt_file"])).exists()
