"""10 个端到端回放场景 + 确定性验证。"""
from __future__ import annotations

from pathlib import Path

import pytest

from ai_trader.config import load_config
from ai_trader.paper_runtime.replay import SCENARIOS, run_scenario

CFG = load_config()


def run(key: str, tmp_path: Path):
    return run_scenario(SCENARIOS[key], cfg=CFG, out_dir=tmp_path / key)


@pytest.mark.parametrize("key", sorted(SCENARIOS))
def test_scenario_passes(key, tmp_path):
    res = run(key, tmp_path)
    assert res.ok, f"{key} 失败：{res.failures}"
    assert res.invariants_ok
    assert res.final_state in ("RUNNING", "HALTED", "READY")
    assert res.digest


@pytest.mark.parametrize("key", sorted(SCENARIOS))
def test_scenario_has_evidence(key, tmp_path):
    """每个场景必须留下可审计证据（时钟轨迹、状态、账户快照）。"""
    res = run(key, tmp_path)
    rep = res.report
    assert rep.get("clock_trace"), "缺少时钟轨迹"
    assert rep.get("phases"), "缺少时段记录"
    assert rep.get("snapshot"), "缺少运行快照"
    assert "invariants" in rep["snapshot"]
    assert rep["snapshot"]["run_id"]


def test_normal_day_produces_two_fills(tmp_path):
    res = run("normal_day", tmp_path)
    assert res.fills == 2
    sides = [f["side"] for f in res.report["fills"]]
    assert sides == ["buy", "sell"]


def test_premarket_blocks_entry(tmp_path):
    res = run("premarket", tmp_path)
    assert res.fills == 0 and res.orders == 0
    assert "pre" in res.phases_seen


def test_afterhours_blocks_entry(tmp_path):
    res = run("afterhours", tmp_path)
    assert res.fills == 0 and res.orders == 0
    assert "post" in res.phases_seen


def test_holiday_blocks_everything(tmp_path):
    res = run("holiday", tmp_path)
    assert res.fills == 0
    assert "holiday" in res.phases_seen


def test_early_close_regular_then_closed(tmp_path):
    res = run("early_close", tmp_path)
    assert "regular" in res.phases_seen


def test_luld_limit_state_blocks_out_of_band(tmp_path):
    res = run("luld_limit_state", tmp_path)
    assert res.fills == 0
    assert any("luld" in s for s in res.report["market_states"])


def test_luld_pause_blocks_fills(tmp_path):
    res = run("luld_pause_resume", tmp_path)
    assert res.fills == 0
    assert "luld_paused" in res.report["market_states"]


def test_mwcb_pauses_market(tmp_path):
    res = run("mwcb", tmp_path)
    assert res.fills == 0
    assert "mwcb_paused" in res.report["market_states"]


def test_feed_outage_halts(tmp_path):
    res = run("feed_outage", tmp_path)
    assert res.final_state == "HALTED"
    assert res.fills >= 1          # 中断前的那笔已经成交并入了账


@pytest.mark.parametrize("key", ["normal_day", "regular_session", "feed_outage"])
def test_scenario_is_deterministic_across_three_runs(key, tmp_path):
    """同一数据集 + 同一配置，3 次运行的指纹必须完全一致。"""
    digests = [run(key, tmp_path / f"r{n}").digest for n in range(3)]
    assert len(set(digests)) == 1, f"{key} 指纹漂移：{digests}"


def test_scenario_input_digest_is_stable():
    a = SCENARIOS["normal_day"].input_digest()
    b = SCENARIOS["normal_day"].input_digest()
    assert a == b and len(a) == 16


def test_scenario_input_digest_differs_between_scenarios():
    assert SCENARIOS["normal_day"].input_digest() != SCENARIOS["holiday"].input_digest()


def test_account_state_after_normal_day(tmp_path):
    res = run("normal_day", tmp_path)
    cash = res.report["snapshot"]["account"]["cash"]
    # 手续费 0、滑点 0：买 44 股 @180.36 卖 44 股 @更高的价 → 现金应高于 9 万
    assert float(cash["USD"]) > 99_000
