"""LULD / MWCB 规则边界测试（对齐官方 LULD Plan Table 1 与投资者教育材料）。

官方参数（前收价决定档位）：
    | 前收价        | Tier1 常规 | Tier1 双倍 | Tier2 常规 | Tier2 双倍 |
    | > $3.00       | 5%        | 10%       | 10%       | 20%       |
    | $0.75 ~ $3.00 | 20%       | 40%       | 20%       | 40%       |
    | < $0.75       | min($0.15, 75%) | min($0.30, 150%) | 同左 | 同左 |

MWCB：
    L1 -7% / L2 -13%：**15:25 之前**触发才停市 15 分钟；15:25 及以后不停市
    L3 -20%：任何时间触发 → 当日剩余时间停市
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ai_trader.market.status import (
    LULD_LOW_ABS,
    LULD_MID_PCT,
    LULD_TIER1_PCT,
    LULD_TIER2_PCT,
    MarketState,
    MarketStatusService,
    luld_band_width,
)

UTC = timezone.utc
T0 = datetime(2026, 11, 16, 17, 0, tzinfo=UTC)


# ---------------------------------------------------------------------- #
# 带宽表
# ---------------------------------------------------------------------- #
@pytest.mark.parametrize("price,tier,doubled,expect", [
    (100.0, 1, False, 0.05),
    (100.0, 1, True, 0.10),
    (100.0, 2, False, 0.10),
    (100.0, 2, True, 0.20),
    (3.01, 1, False, 0.05),
    (2.00, 1, False, 0.20),
    (2.00, 2, False, 0.20),
    (2.00, 1, True, 0.40),
])
def test_band_width_pct_tiers(price, tier, doubled, expect):
    width, unit = luld_band_width(price, tier=tier, doubled=doubled)
    assert unit == "pct"
    assert width == pytest.approx(expect)


@pytest.mark.parametrize("price,expect_normal", [
    (0.50, 0.15),     # min(0.15, 37.5%) → 0.15
    (0.10, 0.075),    # min(0.15, 7.5%)  → 0.075
])
def test_band_width_low_price_uses_absolute(price, expect_normal):
    width, unit = luld_band_width(price, tier=1, doubled=False)
    assert unit == "abs"
    assert width == pytest.approx(expect_normal)
    assert width <= LULD_LOW_ABS + 1e-9
    doubled_width, _ = luld_band_width(price, tier=1, doubled=True)
    assert doubled_width == pytest.approx(min(LULD_LOW_ABS * 2, price * 1.5))


def test_band_width_at_075_uses_mid_bucket():
    width, unit = luld_band_width(0.75, tier=1, doubled=False)
    assert unit == "pct" and width == pytest.approx(LULD_MID_PCT)


def test_band_width_rejects_bad_price():
    from ai_trader.market.status import MarketStatusError
    with pytest.raises(MarketStatusError):
        luld_band_width(0.0)


# ---------------------------------------------------------------------- #
# 服务层：按前收价选档
# ---------------------------------------------------------------------- #
def test_service_uses_prior_close_for_bucket():
    svc = MarketStatusService()
    # 参考价 100，但前收 2.00 → 应走 20% 档，而不是 5%
    svc.update_reference("AAPL", 100.0, prior_close=2.00)
    band = svc._bands["AAPL"]
    assert band.band_pct == pytest.approx(LULD_MID_PCT)
    assert band.upper == pytest.approx(120.0)
    assert band.lower == pytest.approx(80.0)


def test_service_tier2_band_doubles_near_close():
    svc = MarketStatusService()
    svc.update_reference("SMALL", 50.0, prior_close=50.0, doubled=True)
    band = svc._bands["SMALL"]
    # 默认 tier1；用 tier2 列表再测
    assert band.band_pct == pytest.approx(LULD_TIER1_PCT * 2)

    svc2 = MarketStatusService(tier2_symbols=["SMALL"])
    svc2.update_reference("SMALL", 50.0, prior_close=50.0, doubled=True)
    assert svc2._bands["SMALL"].band_pct == pytest.approx(LULD_TIER2_PCT * 2)


def test_repeat_breach_stacks_with_doubled_window():
    svc = MarketStatusService(tier2_symbols=["AAPL"])
    svc.update_reference("AAPL", 100.0, prior_close=100.0, doubled=True)
    b0 = svc._bands["AAPL"]
    assert b0.band_pct == pytest.approx(0.20)          # tier2 + doubled
    # 第一次越界：只暂停，带宽不变（doubling=0）
    b1 = svc.on_quote("AAPL", 79.0, T0, doubled=True, prior_close=100.0)
    assert b1 is not None and b1.doubling == 0
    assert b1.band_pct == pytest.approx(0.20)
    # 第二次越界：带宽翻倍（20% → 40%）
    b2 = svc.on_quote("AAPL", 69.0, T0, doubled=True, prior_close=100.0)
    assert b2 is not None and b2.doubling == 1
    assert b2.band_pct == pytest.approx(0.40)


def test_low_price_band_is_absolute_dollars():
    svc = MarketStatusService()
    svc.update_reference("PENNY", 0.50, prior_close=0.50)
    band = svc._bands["PENNY"]
    assert band.band_abs == pytest.approx(0.15)
    assert band.upper == pytest.approx(0.65)
    assert band.lower == pytest.approx(0.35)


# ---------------------------------------------------------------------- #
# MWCB 15:25 规则
# ---------------------------------------------------------------------- #
def test_mwcb_l1_before_325_halts():
    svc = MarketStatusService()
    svc.update_index(5000.0, T0)
    assert svc.update_index(4650.0, T0) == 1
    assert svc.status("AAPL", now=T0).state is MarketState.MWCB_PAUSED


def test_mwcb_l1_after_325_does_not_halt():
    svc = MarketStatusService()
    svc.update_index(5000.0, T0)
    assert svc.update_index(4650.0, T0, at_or_after_325=True) == 0
    assert svc.status("AAPL", now=T0).state is not MarketState.MWCB_PAUSED


def test_mwcb_l3_closes_even_after_325():
    svc = MarketStatusService()
    svc.update_index(5000.0, T0)
    assert svc.update_index(4000.0, T0, at_or_after_325=True) == 3
    assert svc.status("AAPL", now=T0).state is MarketState.MWCB_CLOSED


def test_mwcb_l2_after_325_does_not_halt():
    svc = MarketStatusService()
    svc.update_index(5000.0, T0)
    assert svc.update_index(4300.0, T0, at_or_after_325=True) == 0   # -14%
    assert svc.mwcb_status()["level"] == 2      # 记录级别但不暂停
    assert svc.status("AAPL", now=T0).state is not MarketState.MWCB_PAUSED


def test_mwcb_exit_still_allowed():
    svc = MarketStatusService()
    svc.update_index(5000.0, T0)
    svc.update_index(4000.0, T0)
    assert svc.evaluate("AAPL", is_exit=True).allowed is True
    assert svc.evaluate("AAPL", is_exit=False).allowed is False
