"""LULD 完整生命周期 + 重新开盘竞价：确定性 Replay 测试。

覆盖：参考价 → 价格带 → Limit State(15s) → Trading Pause(≥5min) →
      Reopening Auction（Nasdaq / NYSE）→ 官方恢复 → 连续交易。

所有时间由测试注入，不依赖真实时钟。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from ai_trader.market.luld import (
    CLOSE_CUTOFF_MINUTES,
    Exchange,
    ImbalanceSide,
    LuldError,
    LuldLifecycle,
    LuldPhase,
    PriceBandState,
    UnknownExchangePolicy,
    normalize_symbol,
    policy_for,
)
from ai_trader.market.status import MarketState, MarketStatusService

NY = ZoneInfo("America/New_York")
T0 = datetime(2026, 11, 16, 10, 0, tzinfo=NY)


def band(ref: float = 100.0, pct: float = 0.05,
         eff: datetime = T0) -> PriceBandState:
    return PriceBandState(symbol="AAPL", reference_price=ref,
                          upper_band=ref * (1 + pct), lower_band=ref * (1 - pct),
                          tier=1, price_bucket=">3", band_width=pct,
                          effective_at=eff, expires_at=None, session="regular",
                          source="test", version=1)


def lc_ready(exchange: Exchange = Exchange.NASDAQ, ref: float = 100.0) -> LuldLifecycle:
    lc = LuldLifecycle("AAPL", exchange=exchange)
    lc.set_reference(ref, T0, previous_close=ref)
    lc.set_band(band(ref))
    return lc


def to_pause(lc: LuldLifecycle, *, direction: str = "lower") -> datetime:
    """推进到 TRADING_PAUSE，返回暂停开始时刻。"""
    px = 80.0 if direction == "lower" else 120.0
    lc.on_trade(px, T0 + timedelta(seconds=1), in_regular=True)
    t_pause = T0 + timedelta(seconds=17)
    lc.on_trade(px, t_pause, in_regular=True)
    assert lc.phase is LuldPhase.TRADING_PAUSE, lc.phase
    return t_pause


# ====================================================================== #
# 正常路径
# ====================================================================== #
def test_full_happy_path_nasdaq():
    lc = lc_ready()
    t = to_pause(lc)
    # 5 分钟内不能开竞价
    assert lc.start_auction(t + timedelta(minutes=1)) is LuldPhase.TRADING_PAUSE
    # 满 5 分钟 → 竞价
    assert lc.start_auction(t + timedelta(minutes=5, seconds=1)) is LuldPhase.REOPENING_AUCTION
    # 竞价平衡 → 等待官方恢复（不是 REOPENED）
    assert lc.on_auction_update(t + timedelta(minutes=6), version=2,
                                indicative_price=100.0) is LuldPhase.RESUME_PENDING
    assert lc.tradable is False
    # 官方恢复 → REOPENED
    assert lc.on_official_resume(t + timedelta(minutes=7), version=99,
                                 source="exchange") is LuldPhase.REOPENED
    assert lc.tradable is True


def test_full_happy_path_nyse():
    lc = lc_ready(Exchange.NYSE)
    t = to_pause(lc)
    assert lc.start_auction(t + timedelta(minutes=5, seconds=1)) is LuldPhase.REOPENING_AUCTION
    assert lc.on_auction_update(t + timedelta(minutes=6), version=2,
                                indicative_price=100.0) is LuldPhase.RESUME_PENDING
    assert lc.on_official_resume(t + timedelta(minutes=7), version=50) is LuldPhase.REOPENED


def test_limit_state_before_pause_is_not_tradable_break():
    lc = lc_ready()
    lc.on_trade(80.0, T0 + timedelta(seconds=1), in_regular=True)
    assert lc.phase is LuldPhase.LIMIT_STATE
    assert lc.tradable is True      # Limit State 期间仍可交易（官方语义）
    lc.on_trade(100.0, T0 + timedelta(seconds=10), in_regular=True)
    assert lc.phase is LuldPhase.OPEN


def test_fifteen_second_boundary():
    lc = lc_ready()
    lc.on_trade(80.0, T0 + timedelta(seconds=1), in_regular=True)
    assert lc.on_trade(80.0, T0 + timedelta(seconds=15), in_regular=True) is LuldPhase.LIMIT_STATE
    assert lc.on_trade(80.0, T0 + timedelta(seconds=17), in_regular=True) is LuldPhase.TRADING_PAUSE


# ====================================================================== #
# 反例 1-2：竞价时点
# ====================================================================== #
def test_counter_early_auction_before_5min_is_ignored():
    """反例 1：4 分 59 秒就收到竞价数据 —— 不得启动竞价。"""
    lc = lc_ready()
    t = to_pause(lc)
    assert lc.start_auction(t + timedelta(seconds=299)) is LuldPhase.TRADING_PAUSE
    assert any(e.kind == "early_auction_ignored" for e in lc.events)


def test_counter_pause_expired_without_auction_stays_paused():
    """反例 2：满 5 分钟但没有竞价数据 —— 保持不可交易。"""
    lc = lc_ready()
    t = to_pause(lc)
    for mins in (5, 10, 60):
        lc.on_trade(100.0, t + timedelta(minutes=mins), in_regular=True)   # 连续成交不推进
        assert lc.phase is LuldPhase.TRADING_PAUSE
        assert lc.tradable is False


# ====================================================================== #
# 反例 3-4：竞价边界与延长
# ====================================================================== #
def test_auction_collars_follow_nasdaq_rule():
    lc = lc_ready()
    t = to_pause(lc)                      # Limit Down → ARP = lower band 95
    lc.start_auction(t + timedelta(minutes=5, seconds=1))
    assert lc.auction.auction_reference_price == pytest.approx(95.0)
    assert lc.auction.collar_lower == pytest.approx(95.0 - 4.75)
    assert lc.auction.collar_upper == pytest.approx(105.0)   # 对侧保持上带


def test_counter_indicative_price_outside_collar_extends():
    """反例 3：指示价超出保护区间 → 延长并放宽对应侧。"""
    lc = lc_ready()
    t = to_pause(lc)
    lc.start_auction(t + timedelta(minutes=5, seconds=1))
    lo_before = lc.auction.collar_lower
    assert lc.on_auction_update(
        t + timedelta(minutes=6), version=2, indicative_price=80.0,
        imbalance_side=ImbalanceSide.SELL) is LuldPhase.AUCTION_EXTENDED
    assert lc.auction.collar_lower < lo_before           # 下侧放宽
    assert lc.auction.collar_upper == pytest.approx(105.0)   # 上侧不动


def test_counter_repeated_extensions():
    """反例 4：反复延长 —— 每次只放宽失衡侧，extension_count 递增。"""
    lc = lc_ready()
    t = to_pause(lc)
    lc.start_auction(t + timedelta(minutes=5, seconds=1))
    lowers = [lc.auction.collar_lower]
    for i in range(3):
        lc.on_auction_update(t + timedelta(minutes=6 + i), version=2 + i,
                             indicative_price=50.0, imbalance_side=ImbalanceSide.SELL)
        lowers.append(lc.auction.collar_lower)
    assert lc.auction.extension_count == 3
    assert lowers == sorted(lowers, reverse=True)        # 单调放宽
    assert lc.tradable is False


# ====================================================================== #
# 反例 5-7：事件版本 / 重复 / 乱序
# ====================================================================== #
def test_counter_stale_resume_ignored():
    """反例 6：先恢复，再收到旧版本暂停/恢复事件 → 不回退。"""
    lc = lc_ready()
    t = to_pause(lc)
    lc.start_auction(t + timedelta(minutes=5, seconds=1))
    lc.on_auction_update(t + timedelta(minutes=6), version=2, indicative_price=100.0)
    lc.on_official_resume(t + timedelta(minutes=7), version=100)
    ver = lc.version
    lc.on_official_resume(t + timedelta(minutes=8), version=1)     # 旧版本
    assert lc.phase is LuldPhase.REOPENED and lc.version == ver


def test_counter_duplicate_auction_update_ten_times():
    """反例 7：同一事件重复 10 次 —— 只生效一次。"""
    lc = lc_ready()
    t = to_pause(lc)
    lc.start_auction(t + timedelta(minutes=5, seconds=1))
    for _ in range(10):
        lc.on_auction_update(t + timedelta(minutes=6), version=2,
                             indicative_price=80.0, imbalance_side=ImbalanceSide.SELL)
    assert lc.auction.extension_count == 1


def test_counter_new_pause_after_resume_is_honoured():
    """反例 5：恢复后又出现更高版本的新暂停 → 必须重新进入暂停。"""
    lc = lc_ready()
    t = to_pause(lc)
    lc.start_auction(t + timedelta(minutes=5, seconds=1))
    lc.on_auction_update(t + timedelta(minutes=6), version=2, indicative_price=100.0)
    lc.on_official_resume(t + timedelta(minutes=7), version=100)
    assert lc.phase is LuldPhase.REOPENED
    # 新一次 Limit State → 暂停
    lc.on_trade(80.0, t + timedelta(minutes=8), in_regular=True)
    lc.on_trade(80.0, t + timedelta(minutes=8, seconds=20), in_regular=True)
    assert lc.phase is LuldPhase.TRADING_PAUSE


# ====================================================================== #
# 反例 8 / 14：数据不可用
# ====================================================================== #
def test_counter_data_unavailable_never_resumes():
    """反例 8：数据源中断期间时钟继续前进 —— 不得自行恢复。"""
    lc = lc_ready()
    t = to_pause(lc)
    lc.data_unavailable(t + timedelta(minutes=1), "feed down")
    assert lc.phase is LuldPhase.DATA_UNAVAILABLE
    assert lc.tradable is False
    lc.on_official_resume(t + timedelta(hours=1), version=999)
    assert lc.phase is LuldPhase.INVALID_EVENT     # 非 RESUME_PENDING 不接受恢复


def test_counter_unknown_auction_never_reopened():
    """反例 14：竞价状态未知 → 永远不判 REOPENED。"""
    lc = lc_ready()
    t = to_pause(lc)
    lc.start_auction(t + timedelta(minutes=5, seconds=1))
    for mins in (6, 10, 30):
        assert lc.on_trade(100.0, t + timedelta(minutes=mins), in_regular=True) \
            is LuldPhase.REOPENING_AUCTION
    assert lc.phase is not LuldPhase.REOPENED


# ====================================================================== #
# 反例 13：早收市 / 收盘前暂停
# ====================================================================== #
def test_counter_close_cutoff_no_continuous_resume():
    """反例 13：收市前 10 分钟内进入暂停 → 转收盘竞价，不恢复连续交易。"""
    lc = lc_ready()
    lc.on_trade(80.0, T0 + timedelta(seconds=1), in_regular=True,
                minutes_to_close=CLOSE_CUTOFF_MINUTES - 1)
    lc.on_trade(80.0, T0 + timedelta(seconds=20), in_regular=True,
                minutes_to_close=CLOSE_CUTOFF_MINUTES - 2)
    assert lc.phase is LuldPhase.TRADING_PAUSE
    assert lc.pause.auction_status.value == "auction_cancelled"
    assert lc.tradable is False


# ====================================================================== #
# 反例 11 / 12：符号与多交易所
# ====================================================================== #
@pytest.mark.parametrize("raw,expected", [
    ("usAAPL", "AAPL"), ("AAPL", "AAPL"), ("USB", "USB"),
    ("hk00700", "00700"), ("CNC", "CNC"), ("", ""),
])
def test_symbol_normalization(raw, expected):
    """反例 11：AAPL 与 usAAPL 必须映射到同一标的；USB/CNC 不能被误剥。"""
    assert normalize_symbol(raw) == expected


def test_counter_two_exchanges_independent():
    """反例 12：一个交易所恢复，另一个仍暂停。"""
    a = lc_ready(Exchange.NASDAQ)
    b = lc_ready(Exchange.NYSE)
    ta, tb = to_pause(a), to_pause(b)
    a.start_auction(ta + timedelta(minutes=5, seconds=1))
    a.on_auction_update(ta + timedelta(minutes=6), version=2, indicative_price=100.0)
    a.on_official_resume(ta + timedelta(minutes=7), version=100)
    assert a.tradable is True
    assert b.phase is LuldPhase.TRADING_PAUSE and b.tradable is False


# ====================================================================== #
# 未知交易所 / 规则未核实
# ====================================================================== #
def test_unknown_exchange_policy_refuses_to_guess():
    with pytest.raises(LuldError):
        UnknownExchangePolicy().build_collars(band(), "lower")
    assert policy_for(Exchange.UNKNOWN).verified is False


def test_known_exchange_policies_verified():
    assert policy_for(Exchange.NASDAQ).verified is True
    assert policy_for(Exchange.NYSE).verified is True


# ====================================================================== #
# 与 MarketStatusService 的接线（反例 9 / 10）
# ====================================================================== #
def test_service_pause_expiry_does_not_become_open():
    """接线后：5 分钟到期不再是 OPEN，而是等待竞价/恢复。"""
    svc = MarketStatusService()
    b = __import__("ai_trader.market.status", fromlist=["DayBounds"]).DayBounds(
        open_at=datetime(2026, 11, 16, 9, 30, tzinfo=NY),
        close_at=datetime(2026, 11, 16, 16, 0, tzinfo=NY))
    svc.update_reference("AAPL", 100.0, prior_close=100.0)
    lc = svc.ensure_lifecycle("AAPL", exchange=Exchange.NASDAQ)
    lc.set_reference(100.0, T0, previous_close=100.0)
    lc.set_band(band())
    svc.on_quote("AAPL", 100.0, T0)
    svc.on_quote("AAPL", 80.0, T0 + timedelta(seconds=2))
    st = svc.status("AAPL", now=T0 + timedelta(seconds=30))
    assert st.state is MarketState.LULD_PAUSED
    # 5 分钟后：仍不可交易
    later = svc.status("AAPL", now=T0 + timedelta(minutes=6))
    assert later.state is MarketState.LULD_PAUSED
    assert "竞价" in later.note or "恢复" in later.note
    ok, why, _ = svc.fill_allowed("AAPL", now=T0 + timedelta(minutes=6))
    assert ok is False


def test_service_fill_blocked_while_auction_pending():
    """反例 9：暂停期间遗留止损单 → Paper 不成交（fill_allowed=False）。"""
    svc = MarketStatusService()
    svc.update_reference("AAPL", 100.0, prior_close=100.0)
    lc = svc.ensure_lifecycle("AAPL", exchange=Exchange.NASDAQ)
    lc.set_band(band())
    svc.on_quote("AAPL", 100.0, T0)
    svc.on_quote("AAPL", 80.0, T0 + timedelta(seconds=2))
    ok, why, state = svc.fill_allowed("AAPL", now=T0 + timedelta(seconds=30))
    assert ok is False and state is MarketState.LULD_PAUSED
