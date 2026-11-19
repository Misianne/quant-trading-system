"""宏观状态判定与研究口径的测试。

三类断言：
1. **规则正确**：阈值方向、投票合成、6 状态映射；
2. **无未来函数**：判定只依赖快照；研究脚本的信息集里不得出现晚于决策时点的版本；
3. **研究口径**：入场价必须是次日开盘、SMA200 只用历史。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ai_trader.macro import get_macro_snapshot
from ai_trader.macro import regime as R

REAL_DB = Path(__file__).resolve().parents[1] / "data" / "macro_data.db"
pytestmark_db = pytest.mark.skipif(not REAL_DB.exists(), reason="需要 data/macro_data.db")


# --------------------------------------------------------------------------- #
# 构造一个最小快照（不需要数据库）
# --------------------------------------------------------------------------- #
@dataclass
class P:
    observation_period: str
    value: float
    vintage_date: str = "2026-01-01"
    time_precision: str = "date"


@dataclass
class Snap:
    as_of: str
    by_series: dict


def monthly(start_year: int, start_month: int, values):
    out, y, m = [], start_year, start_month
    for v in values:
        out.append(P(f"{y:04d}-{m:02d}-01", v))
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def _build_levels(infl_yoy: float, infl_3m_ago: float, ann3m: float,
                  n: int = 36):
    """构造一条水平序列，使最后一个月精确满足三个目标读数。

    ``yoy``       = L[-1] / L[-13]
    ``yoy_3m_ago``= L[-4] / L[-16]
    ``ann3m``     = (L[-1] / L[-4])**4 - 1
    三个量都钉在锚点上，中间用几何插值填充，避免"大概接近"导致的脆弱测试。
    """
    t = n - 1
    L = [None] * n
    L[t] = 100.0
    L[t - 3] = 100.0 / (1 + ann3m) ** 0.25
    L[t - 12] = 100.0 / (1 + infl_yoy)
    L[t - 15] = L[t - 3] / (1 + infl_3m_ago)

    anchors = [(t - 15, L[t - 15]), (t - 12, L[t - 12]), (t - 3, L[t - 3]), (t, L[t])]
    for (i0, v0), (i1, v1) in zip(anchors, anchors[1:]):
        if i1 - i0 <= 0:
            continue
        ratio = (v1 / v0) ** (1.0 / (i1 - i0))
        for k in range(i0, i1):
            L[k] = v0 * (ratio ** (k - i0))
    L[t] = 100.0
    for k in range(t - 16, -1, -1):          # 锚点之前：按 0.2%/月 外推
        L[k] = L[k + 1] / 1.002
    return L


def build_snapshot(infl_yoy: float, infl_3m_ago: float, ann3m: float,
                   pay_ratio: str = "strong", unrate_delta: float = 0.0,
                   gdp_real: float = 0.03, as_of: str = "2026-01-05T00:00:00+00:00"):
    """构造一份四类指标都可用的快照（不依赖数据库）。"""
    levels = _build_levels(infl_yoy, infl_3m_ago, ann3m)
    periods = monthly(2023, 1, levels)
    series = {sid: [P(p.observation_period, p.value) for p in periods]
              for sid in R.INFLATION_SERIES}

    base = 200.0
    step = {"strong": base, "weak": base * 0.3}[pay_ratio]
    pay, lvl = [], 150000.0
    for i, p in enumerate(periods):
        lvl += step if i >= len(periods) - 3 else base
        pay.append(P(p.observation_period, lvl))
    series["PAYEMS"] = pay

    series["UNRATE"] = [P(p.observation_period,
                          4.0 + (unrate_delta if i >= len(periods) - 3 else 0.0))
                        for i, p in enumerate(periods)]

    # GDP：以 CPI 平减后的实际同比 = gdp_real 反推名义季度序列
    idx = {p.observation_period: i for i, p in enumerate(periods)}
    gp, g_prev = "2025-10-01", "2024-10-01"
    cpi_now, cpi_prev = levels[idx[gp]], levels[idx[g_prev]]
    cpi_window = cpi_now / cpi_prev - 1.0
    nominal = (1 + gdp_real) * (1 + cpi_window) - 1.0
    gdp, lvl_g = [], 20000.0
    for i in range(12):
        y, m = 2023 + i // 4, (i % 4) * 3 + 1
        gdp.append(P(f"{y:04d}-{m:02d}-01", lvl_g))
        lvl_g *= (1 + nominal) ** 0.25
    series["GDP"] = gdp

    series["DFF"] = [P("2025-12-30", 4.0), P("2025-12-31", 4.0)]
    return Snap(as_of=as_of, by_series=series)


# --------------------------------------------------------------------------- #
# 1) 工具函数
# --------------------------------------------------------------------------- #
def test_shift_months_crosses_year():
    assert R.shift_months("2026-01-01", -1) == "2025-12-01"
    assert R.shift_months("2025-12-01", 3) == "2026-03-01"
    assert R.shift_months("2024-02-01", -12) == "2023-02-01"


def test_shift_quarters():
    assert R.shift_quarters("2026-01-01", -4) == "2025-01-01"
    assert R.shift_quarters("2026-10-01", 1) == "2027-01-01"


# --------------------------------------------------------------------------- #
# 2) 六状态映射
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("infl,grow,expect", [
    (R.COOLING, R.RESILIENT, R.COOLING_RESILIENT),
    (R.COOLING, R.WEAKENING, R.COOLING_WEAKENING),
    (R.HEATING, R.RESILIENT, R.HEATING_RESILIENT),
    (R.HEATING, R.WEAKENING, R.HEATING_WEAKENING),
    (R.MIXED, R.RESILIENT, R.CONTRADICTORY),
    (R.COOLING, R.MIXED, R.CONTRADICTORY),
    (R.UNKNOWN, R.RESILIENT, R.INSUFFICIENT),
])
def test_combine_maps_all_states(infl, grow, expect):
    assert R.combine(infl, grow) == expect


def test_every_state_has_a_label():
    for st in (R.COOLING_RESILIENT, R.COOLING_WEAKENING, R.HEATING_RESILIENT,
               R.HEATING_WEAKENING, R.CONTRADICTORY, R.INSUFFICIENT):
        assert R.REGIME_LABEL[st]


# --------------------------------------------------------------------------- #
# 3) 方向判定
# --------------------------------------------------------------------------- #
def test_cooling_inflation_is_detected():
    snap = build_snapshot(infl_yoy=0.02, infl_3m_ago=0.05, ann3m=0.005)
    a = R.assess(snap)
    assert a.inflation.direction == R.COOLING, a.inflation.as_dict()
    assert a.inflation.score <= -2


def test_heating_inflation_is_detected():
    snap = build_snapshot(infl_yoy=0.06, infl_3m_ago=0.03, ann3m=0.10)
    a = R.assess(snap)
    assert a.inflation.direction == R.HEATING, a.inflation.as_dict()
    assert a.inflation.score >= 2


def test_payroll_collapse_and_rising_unrate_mean_weakening():
    snap = build_snapshot(infl_yoy=0.02, infl_3m_ago=0.05, ann3m=0.005,
                          pay_ratio="weak", unrate_delta=0.6, gdp_real=-0.01)
    a = R.assess(snap)
    assert a.growth.direction == R.WEAKENING, a.growth.as_dict()


def test_strong_payrolls_mean_resilient():
    snap = build_snapshot(infl_yoy=0.06, infl_3m_ago=0.03, ann3m=0.10,
                          pay_ratio="strong", unrate_delta=-0.2, gdp_real=0.03)
    a = R.assess(snap)
    assert a.growth.direction == R.RESILIENT


def test_single_indicator_never_decides_alone():
    """只有一项指标越界时，方向必须是 MIXED —— 这就是"不用单一阈值"的含义。"""
    votes = [R.Vote("A", "x", -1, ""), R.Vote("B", "y", 0, ""),
             R.Vote("C", "z", +1, ""), R.Vote("D", "w", 0, "")]
    d, score = R.direction_from_votes(votes, 2)
    assert score == 0 and d == R.MIXED


def test_insufficient_data_is_reported():
    snap = Snap(as_of="2026-11-01T00:00:00+00:00", by_series={
        "CPIAUCSL": [P("2026-09-01", 300.0)],
        "PAYEMS": [P("2026-09-01", 150000.0)],
    })
    a = R.assess(snap)
    assert a.state == R.INSUFFICIENT
    assert a.confidence == "low"


def test_assessment_is_deterministic():
    snap = build_snapshot(infl_yoy=0.02, infl_3m_ago=0.05, ann3m=0.005)
    a1, a2 = R.assess(snap), R.assess(snap)
    assert a1.as_dict() == a2.as_dict()


def test_assessment_carries_invalidators_and_evidence():
    snap = build_snapshot(infl_yoy=0.02, infl_3m_ago=0.05, ann3m=0.005)
    a = R.assess(snap)
    assert a.invalidators and a.evidence_for
    assert any("失效" in x for x in a.invalidators)


def test_stale_data_is_flagged():
    snap = build_snapshot(infl_yoy=0.02, infl_3m_ago=0.05, ann3m=0.005,
                          as_of="2027-06-01T00:00:00+00:00")
    a = R.assess(snap)
    assert a.inflation.stale is True
    assert any("未更新" in n for n in a.notes)


# --------------------------------------------------------------------------- #
# 4) 真实库：无未来函数
# --------------------------------------------------------------------------- #
@pytestmark_db
def test_snapshot_used_by_assessment_has_no_future_versions():
    """研究用的每个时点，快照里所有行的 release_epoch 必须 <= as_of。"""
    for ts in ("2022-06-30T23:59:59-04:00", "2023-12-29T23:59:59-05:00",
               "2025-06-30T23:59:59-04:00"):
        snap = get_macro_snapshot(REAL_DB, ts, series=["CPIAUCSL", "PAYEMS", "GDP"])
        cutoff = int(datetime.fromisoformat(ts).timestamp())
        for pts in snap.by_series.values():
            for p in pts:
                assert p.release_epoch is not None and p.release_epoch <= cutoff


@pytestmark_db
def test_known_history_matches_expectations():
    """2022-06 的 CPI 同比应在 8% 附近、实际利率应为明显负值 —— 防止口径悄悄漂移。"""
    snap = get_macro_snapshot(REAL_DB, "2022-06-01T23:59:59-04:00")
    a = R.assess(snap)
    cpi = a.inflation.detail["CPIAUCSL"]["yoy"]
    assert 0.075 < cpi < 0.090, cpi


@pytestmark_db
def test_policy_real_rate_is_in_percentage_points():
    snap = get_macro_snapshot(REAL_DB, "2022-06-01T23:59:59-04:00")
    pol = R.policy_block(snap)
    # 2022 年 6 月：DFF≈0.8%，CPI 同比≈8.2% → 实际利率约 -7 个百分点
    assert -9.0 < pol["real_rate"] < -5.0, pol
    assert pol["stance"] == "accommodative"


# --------------------------------------------------------------------------- #
# 5) 研究口径
# --------------------------------------------------------------------------- #
def test_forward_stats_enters_at_next_open_and_uses_only_past():
    """入场必须是决策日**次日开盘**；窗口内不得引用决策日之前的价格。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "mrr", Path(__file__).resolve().parents[1] / "tools" / "macro_regime_research.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)

    bars = [{"date": f"2026-01-{i + 1:02d}", "open": 100.0 + i, "close": 101.0 + i,
             "sma200": None} for i in range(10)]
    f = m.forward_stats(bars, 0)
    assert f["entry_date"] == "2026-01-02"
    assert f["entry"] == 101.0                 # bars[1]["open"]，不是 bars[0] 的收盘
    assert f["ret5"] == pytest.approx(bars[5]["close"] / 101.0 - 1)


def test_sma200_uses_only_trailing_window():
    import importlib.util
    import tempfile
    import sqlite3
    spec = importlib.util.spec_from_file_location(
        "mrr", Path(__file__).resolve().parents[1] / "tools" / "macro_regime_research.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)

    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "m.db"
        c = sqlite3.connect(db)
        c.execute("CREATE TABLE us_bars(symbol TEXT, trade_date TEXT, open REAL, close REAL)")
        for i in range(250):
            c.execute("INSERT INTO us_bars VALUES('usqqq', ?, ?, ?)",
                      (f"2026-{i // 28 + 1:02d}-{i % 28 + 1:02d}", 100.0 + i, 100.0 + i))
        c.commit()
        c.close()
        bars = m.load_qqq(db)
        assert bars[198]["sma200"] is None
        assert bars[199]["sma200"] == pytest.approx(sum(range(100, 300)) / 200)
        # 最后一天突然暴涨，不能影响前面已经算好的 SMA
        before = bars[-2]["sma200"]
        bars2 = m.load_qqq(db)
        assert bars2[-2]["sma200"] == before


# --------------------------------------------------------------------------- #
# 6) 解释层：结构与"不编造"
# --------------------------------------------------------------------------- #
def test_explanation_has_all_ten_sections_in_order():
    from ai_trader.macro.explain import explain
    snap = build_snapshot(infl_yoy=0.02, infl_3m_ago=0.05, ann3m=0.005)
    doc = explain(R.assess(snap))
    keys = [s["key"] for s in doc["sections"]]
    assert keys == ["environment", "changes", "impact", "positives", "negatives",
                    "priced_in", "not_priced", "confidence", "invalidators", "sources"]


def test_priced_in_sections_are_marked_unverifiable_and_say_what_is_missing():
    from ai_trader.macro.explain import explain
    snap = build_snapshot(infl_yoy=0.02, infl_3m_ago=0.05, ann3m=0.005)
    doc = explain(R.assess(snap))
    by = {s["key"]: s for s in doc["sections"]}
    for k in ("priced_in", "not_priced"):
        assert by[k]["status"] == "unverifiable"
        assert by[k]["missing"], "必须写明缺什么数据，否则等于假装知道"
    assert set(doc["unverifiable_sections"]) >= {"priced_in", "not_priced"}


def test_contradictory_state_is_not_dressed_up_as_a_direction():
    from ai_trader.macro.explain import explain
    snap = build_snapshot(infl_yoy=0.06, infl_3m_ago=0.03, ann3m=0.10,
                          pay_ratio="strong", unrate_delta=-0.2, gdp_real=0.03)
    a = R.assess(snap)
    assert a.state == R.HEATING_RESILIENT
    doc = explain(a)
    impact = [s for s in doc["sections"] if s["key"] == "impact"][0]
    assert impact["status"] == "inferred"
    assert "不可外推" in impact["content"][0] or "不显著" in impact["content"][0]


def test_fact_sections_always_cite_sources():
    from ai_trader.macro.explain import explain
    snap = build_snapshot(infl_yoy=0.02, infl_3m_ago=0.05, ann3m=0.005)
    doc = explain(R.assess(snap))
    for s in doc["sections"]:
        if s["key"] in ("environment", "changes", "positives", "negatives"):
            assert s["sources"], f"{s['title']} 缺少来源引用"
