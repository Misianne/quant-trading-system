"""LULD / MWCB / 停牌：规则闭环与三阶段（意图 / 提交 / 成交）测试。

官方依据：
* LULD Plan Amendment 18（开盘不再双倍；收市前 25 分钟仅 Tier1 及 Tier2≤$3 双倍）
* Investor.gov / NYSE MWCB FAQ（−7/−13/−20%；收市前 35 分钟截止；L3 当日关闭）
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ai_trader.broker.base import OrderRequest
from ai_trader.broker.paper import PaperBroker
from ai_trader.market.status import (
    DayBounds,
    HaltKind,
    HaltReason,
    LULDAvailability,
    LULD_LOW_ABS,
    LULD_MID_PCT,
    LULD_TIER1_PCT,
    LULD_TIER2_PCT,
    MarketState,
    MarketStatusService,
    luld_doubled,
)

NY = ZoneInfo("America/New_York")
UTC = timezone.utc


def bounds_regular(day: int = 16, close_h: int = 16, close_m: int = 0) -> DayBounds:
    """正常交易日：09:30–16:00 ET（或指定收盘时刻，用于早收市）。"""
    return DayBounds(
        open_at=datetime(2026, 11, day, 9, 30, tzinfo=NY),
        close_at=datetime(2026, 11, day, close_h, close_m, tzinfo=NY),
    )


def ny(h: int, m: int = 0, day: int = 16) -> datetime:
    return datetime(2026, 11, day, h, m, tzinfo=NY)


def feed_trades(svc: MarketStatusService, symbol: str, price: float,
                b: DayBounds, n: int = 3, at: datetime | None = None):
    """灌入若干笔常规时段成交，建立 5 分钟参考价。"""
    t = at or b.open_at + timedelta(minutes=30)
    for i in range(n):
        svc.reference.add(symbol, __import__(
            "ai_trader.market.status", fromlist=["Trade"]).Trade(
            price=price, ts=t + timedelta(seconds=i)), in_regular=True)
    return t


# ====================================================================== #
# P0-1 计时器拆分
# ====================================================================== #
def test_luld_pause_is_5_minutes_not_15():
    svc = MarketStatusService()
    assert svc.luld_pause.total_seconds() == 300
    assert svc.mwcb_halt.total_seconds() == 900


def test_luld_pause_expires_after_5_minutes():
    svc = MarketStatusService()
    b = bounds_regular()
    t = feed_trades(svc, "AAPL", 100.0, b)
    svc.update_reference("AAPL", 100.0, prior_close=100.0)
    # 越界 → Limit State（15 秒）→ 暂停
    svc.observe_trade("AAPL", 100.0, t, bounds=b, prior_close=100.0)
    svc.observe_trade("AAPL", 80.0, t + timedelta(seconds=1), bounds=b, prior_close=100.0)
    r = svc.observe_trade("AAPL", 80.0, t + timedelta(seconds=20), bounds=b, prior_close=100.0)
    assert r.state is MarketState.LULD_PAUSED
    st = svc.status("AAPL", now=t + timedelta(seconds=20))
    assert st.expected_resume_at == t + timedelta(seconds=20, minutes=5)
    # 4 分钟后仍暂停
    assert svc.status("AAPL", now=t + timedelta(minutes=4)).state is MarketState.LULD_PAUSED
    # 5 分钟后暂停解除（此后若行情不新鲜，状态会是 DATA_STALE —— 这是安全行为）
    late = svc.status("AAPL", now=t + timedelta(minutes=6))
    assert late.state is not MarketState.LULD_PAUSED
    assert any(e.state is MarketState.OPEN and e.kind is HaltKind.LULD
               for e in late.events)


def test_mwcb_pause_is_15_minutes():
    svc = MarketStatusService()
    b = bounds_regular()
    t0 = ny(10, 0)
    svc.update_index(5000.0, t0, bounds=b)
    assert svc.update_index(4650.0, t0, bounds=b) == 1
    assert svc.status("AAPL", now=t0).state is MarketState.MWCB_PAUSED
    assert svc.mwcb_status()["resume_at"] == (t0 + timedelta(minutes=15)).isoformat()
    assert svc.status("AAPL", now=t0 + timedelta(minutes=16)).state is not MarketState.MWCB_PAUSED


def test_mwcb_l3_closes_for_day_never_auto_resumes():
    svc = MarketStatusService()
    b = bounds_regular()
    t0 = ny(10, 0)
    svc.update_index(5000.0, t0, bounds=b)
    assert svc.update_index(4000.0, t0, bounds=b) == 3
    for hours in (1, 3, 6):
        assert svc.status("AAPL", now=t0 + timedelta(hours=hours)).state \
            is MarketState.MWCB_CLOSED


def test_exchange_halt_not_cleared_by_timer():
    """普通停牌即使过了预计恢复时间，也必须等恢复通知。"""
    svc = MarketStatusService()
    t0 = ny(10, 0)
    svc.halt_symbol("AAPL", HaltReason.NEWS_PENDING, ts=t0,
                    resume_at=t0 + timedelta(minutes=2), note="news")
    later = t0 + timedelta(hours=2)
    st = svc.status("AAPL", now=later)
    assert st.state is MarketState.HALTED
    assert "未收到恢复通知" in st.note
    # 收到通知才恢复
    svc.notify_resume("AAPL", ts=later)
    after = svc.status("AAPL", now=later)
    assert after.state is not MarketState.HALTED
    assert after.recovery_notice_at is not None
    assert any(e.state is MarketState.OPEN and e.kind is HaltKind.EXCHANGE
               for e in after.events)


def test_luld_timer_does_not_clear_exchange_halt():
    """LULD 的 5 分钟计时器不得把交易所停牌解除。"""
    svc = MarketStatusService()
    t0 = ny(10, 0)
    svc.halt_symbol("AAPL", HaltReason.REGULATORY, ts=t0)
    # 即便之后过了任意时间（相当于 LULD 早已到期）
    assert svc.status("AAPL", now=t0 + timedelta(minutes=6)).state is MarketState.HALTED
    assert svc.status("AAPL", now=t0 + timedelta(days=1)).state is MarketState.HALTED


def test_events_carry_version_source_and_times():
    svc = MarketStatusService()
    t0 = ny(10, 0)
    svc.halt_symbol("AAPL", HaltReason.NEWS_PENDING, ts=t0,
                    resume_at=t0 + timedelta(minutes=3), source="nasdaq")
    st = svc.status("AAPL", now=t0)
    ev = st.events[-1]
    assert ev.version >= 1 and ev.kind is HaltKind.EXCHANGE
    assert ev.triggered_at == t0 and ev.expected_resume_at is not None
    assert ev.source == "nasdaq"


def test_clock_jump_backwards_does_not_restore():
    """时钟回拨不得让停牌"提前"恢复。"""
    svc = MarketStatusService()
    t0 = ny(12, 0)
    svc.halt_symbol("AAPL", HaltReason.NEWS_PENDING, ts=t0)
    assert svc.status("AAPL", now=t0 - timedelta(hours=1)).state is MarketState.HALTED


# ====================================================================== #
# P0-2 LULD 仅常规时段 + 参考价
# ====================================================================== #
def test_luld_out_of_regular_session_returns_out_of_session():
    svc = MarketStatusService()
    b = bounds_regular()
    pre = datetime(2026, 11, 16, 8, 0, tzinfo=NY)
    r = svc.observe_trade("AAPL", 100.0, pre, bounds=b, prior_close=100.0)
    assert r.availability is LULDAvailability.OUT_OF_SESSION
    post = datetime(2026, 11, 16, 17, 0, tzinfo=NY)
    assert svc.observe_trade("AAPL", 100.0, post, bounds=b,
                             prior_close=100.0).availability is LULDAvailability.OUT_OF_SESSION


def test_luld_requires_bounds():
    svc = MarketStatusService()
    r = svc.observe_trade("AAPL", 100.0, ny(10), bounds=None, prior_close=100.0)
    assert r.availability is LULDAvailability.OUT_OF_SESSION


def test_luld_requires_prior_close():
    svc = MarketStatusService()
    b = bounds_regular()
    r = svc.observe_trade("AAPL", 100.0, ny(10), bounds=b, prior_close=None)
    assert r.availability is LULDAvailability.NO_PRIOR_CLOSE


def test_luld_without_qualifying_trades_is_unavailable():
    """没有合格成交 → NO_REFERENCE，不得用 last / prev_close 冒充。"""
    svc = MarketStatusService()
    b = bounds_regular()
    svc.reference.reset()
    r = svc.observe_trade("AAPL", 100.0, ny(10), bounds=b, prior_close=100.0)
    assert r.availability is LULDAvailability.NO_REFERENCE
    assert r.band is None


def test_luld_reference_from_5min_trades_not_last():
    svc = MarketStatusService()
    b = bounds_regular()
    t = b.open_at + timedelta(minutes=30)
    for i, px in enumerate([100.0, 102.0, 104.0]):
        svc.reference.add("AAPL", __import__(
            "ai_trader.market.status", fromlist=["Trade"]).Trade(px, t), in_regular=True)
    r = svc.observe_trade("AAPL", 150.0, t + timedelta(seconds=1), bounds=b, prior_close=100.0)
    assert r.band is not None
    assert r.band.reference_price == pytest.approx(101.0)   # 1% 阈值内不更新，且绝不含当前 150


def test_luld_limit_state_15_seconds_boundary():
    svc = MarketStatusService()
    b = bounds_regular()
    t = feed_trades(svc, "AAPL", 100.0, b)
    svc.update_reference("AAPL", 100.0, prior_close=100.0)
    svc.observe_trade("AAPL", 100.0, t, bounds=b, prior_close=100.0)
    # 越界：进入 Limit State
    r1 = svc.observe_trade("AAPL", 80.0, t + timedelta(seconds=1), bounds=b, prior_close=100.0)
    assert r1.state is MarketState.LULD_LIMIT_STATE
    # 14 秒：仍在 Limit State
    r2 = svc.observe_trade("AAPL", 80.0, t + timedelta(seconds=14), bounds=b, prior_close=100.0)
    assert r2.state is MarketState.LULD_LIMIT_STATE
    # 16 秒：进入暂停
    r3 = svc.observe_trade("AAPL", 80.0, t + timedelta(seconds=16), bounds=b, prior_close=100.0)
    assert r3.state is MarketState.LULD_PAUSED


def test_luld_limit_state_cleared_within_15s():
    svc = MarketStatusService()
    b = bounds_regular()
    t = feed_trades(svc, "AAPL", 100.0, b)
    svc.update_reference("AAPL", 100.0, prior_close=100.0)
    svc.observe_trade("AAPL", 100.0, t, bounds=b, prior_close=100.0)
    svc.observe_trade("AAPL", 80.0, t + timedelta(seconds=1), bounds=b, prior_close=100.0)
    r = svc.observe_trade("AAPL", 100.0, t + timedelta(seconds=5), bounds=b, prior_close=100.0)
    assert r.state is MarketState.OPEN


# ====================================================================== #
# P0-3 双倍带宽（Amendment 18）
# ====================================================================== #
@pytest.mark.parametrize("tier,ref,mins,expect_double", [
    (1, 100.0, 60.0, False),      # 盘中：单倍
    (1, 100.0, 10.0, True),       # 收市前 25 分钟：Tier1 双倍
    (2, 2.0, 10.0, True),         # Tier2 ≤ $3：双倍
    (2, 100.0, 10.0, False),      # Tier2 > $3：单倍
    (2, 100.0, 60.0, False),
])
def test_luld_doubled_rules(tier, ref, mins, expect_double):
    doubled, why = luld_doubled(tier, ref, mins)
    assert doubled is expect_double, why


def test_opening_window_is_never_doubled():
    """Amendment 18：开盘 9:30–9:45 不再双倍。"""
    b = bounds_regular()
    open_mins = 5.0           # 距开盘 5 分钟
    assert b.minutes_since_open(b.open_at + timedelta(minutes=5)) == pytest.approx(open_mins)
    doubled, _ = luld_doubled(1, 100.0, b.minutes_to_close(b.open_at + timedelta(minutes=5)))
    assert doubled is False


def test_early_close_uses_actual_close_time():
    """早收市 13:00 → 双倍窗口是 12:35–13:00。"""
    b = bounds_regular(close_h=13)
    assert b.minutes_to_close(datetime(2026, 11, 16, 12, 40, tzinfo=NY)) == pytest.approx(20.0)
    doubled, _ = luld_doubled(1, 100.0, b.minutes_to_close(
        datetime(2026, 11, 16, 12, 40, tzinfo=NY)))
    assert doubled is True
    doubled2, _ = luld_doubled(1, 100.0, b.minutes_to_close(
        datetime(2026, 11, 16, 12, 30, tzinfo=NY)))
    assert doubled2 is False


def test_tier2_high_price_band_stays_single_near_close():
    svc = MarketStatusService(tier2_symbols=["BIG"])
    b = bounds_regular()
    t = datetime(2026, 11, 16, 15, 45, tzinfo=NY)     # 收市前 15 分钟
    for i in range(3):
        svc.reference.add("BIG", __import__(
            "ai_trader.market.status", fromlist=["Trade"]).Trade(100.0, t), in_regular=True)
    r = svc.observe_trade("BIG", 100.0, t, bounds=b, prior_close=100.0)
    assert r.band is not None
    assert r.band.doubled is False
    assert r.band.band_pct == pytest.approx(LULD_TIER2_PCT)


def test_tier1_near_close_band_doubles():
    svc = MarketStatusService()
    b = bounds_regular()
    t = datetime(2026, 11, 16, 15, 45, tzinfo=NY)
    for i in range(3):
        svc.reference.add("AAPL", __import__(
            "ai_trader.market.status", fromlist=["Trade"]).Trade(100.0, t), in_regular=True)
    r = svc.observe_trade("AAPL", 100.0, t, bounds=b, prior_close=100.0)
    assert r.band is not None and r.band.doubled is True
    assert r.band.band_pct == pytest.approx(LULD_TIER1_PCT * 2)


# ====================================================================== #
# P0-5 MWCB 截止时间
# ====================================================================== #
def test_mwcb_l1_halts_before_cutoff():
    svc = MarketStatusService()
    b = bounds_regular()
    t0 = ny(10, 0)          # 距收市 360 分钟
    svc.update_index(5000.0, t0, bounds=b)
    assert svc.update_index(4650.0, t0, bounds=b) == 1


def test_mwcb_l1_after_cutoff_does_not_halt():
    svc = MarketStatusService()
    b = bounds_regular()
    t0 = ny(15, 30)         # 距收市 30 分钟 < 35
    svc.update_index(5000.0, t0, bounds=b)
    assert svc.update_index(4650.0, t0, bounds=b) == 0
    assert svc.status("AAPL", now=t0).state is not MarketState.MWCB_PAUSED


def test_mwcb_l3_halts_even_after_cutoff():
    svc = MarketStatusService()
    b = bounds_regular()
    t0 = ny(15, 30)
    svc.update_index(5000.0, t0, bounds=b)
    assert svc.update_index(4000.0, t0, bounds=b) == 3


def test_mwcb_early_close_cutoff_shifts():
    """早收市 13:00 → 截止时间为 12:25。"""
    svc = MarketStatusService()
    b = bounds_regular(close_h=13)
    late = ny(12, 40)       # 距收市 20 分钟 → 已过截止
    svc.update_index(5000.0, late, bounds=b)
    assert svc.update_index(4650.0, late, bounds=b) == 0
    early = ny(11, 0)       # 距收市 120 分钟 → 未过截止
    svc2 = MarketStatusService()
    svc2.update_index(5000.0, early, bounds=b)
    assert svc2.update_index(4650.0, early, bounds=b) == 1


def test_mwcb_level_triggers_once_per_day():
    svc = MarketStatusService()
    b = bounds_regular()
    t0 = ny(10, 0)
    svc.update_index(5000.0, t0, bounds=b)
    assert svc.update_index(4650.0, t0, bounds=b) == 1
    # 同日再次跌到 -7% 不再重复触发
    assert svc.update_index(4640.0, t0 + timedelta(minutes=1), bounds=b) == 0
    # 跨日重置后可再次触发
    svc.reset_day()
    b2 = bounds_regular(day=17)
    t1 = ny(10, 0, day=17)
    svc.update_index(5000.0, t1, bounds=b2)
    assert svc.update_index(4650.0, t1, bounds=b2) == 1


# ====================================================================== #
# P0-4 三阶段：意图 / 提交 / 成交
# ====================================================================== #
def test_three_stages_during_halt():
    svc = MarketStatusService()
    t0 = ny(10, 0)
    svc.halt_symbol("AAPL", HaltReason.NEWS_PENDING, ts=t0)
    # A 意图：允许（保留止损意图）
    a = svc.intent_gate("AAPL", is_exit=True, now=t0)
    assert a.allowed is True and "退出意图" in a.warning
    # B 提交：拒绝
    b = svc.submit_gate("AAPL", is_exit=True, now=t0)
    assert b.allowed is False and b.stage == "submit"
    # C 成交：拒绝
    ok, why, state = svc.fill_allowed("AAPL", now=t0)
    assert ok is False and state is MarketState.HALTED


def test_unknown_state_blocks_submit_but_keeps_exit_intent():
    svc = MarketStatusService()
    a = svc.intent_gate("MSFT", is_exit=True)
    assert a.allowed is True                       # 意图保留
    b = svc.submit_gate("MSFT", is_exit=True)
    assert b.allowed is False and "无法确认" in b.reason


def test_paper_does_not_fill_during_halt():
    svc = MarketStatusService()
    svc.halt_symbol("usAAPL", HaltReason.LULD)
    paper = _paper(tmp_path_or_none(), market_status=svc)
    r = paper.place_order(OrderRequest(symbol="usAAPL", market="us", side="buy",
                                       qty=10, order_type="market"))
    assert r.status == "submitted"
    assert r.filled_qty == 0


def test_paper_fills_after_resume():
    svc = MarketStatusService()
    svc.halt_symbol("usAAPL", HaltReason.NEWS_PENDING)
    paper = _paper(tmp_path_or_none(), market_status=svc)
    blocked = paper.place_order(OrderRequest(symbol="usAAPL", market="us", side="buy",
                                             qty=10, order_type="market"))
    assert blocked.status == "submitted"
    svc.notify_resume("usAAPL")
    svc.record_trade("usAAPL", 100.0, datetime.now(timezone.utc))
    ok = paper.place_order(OrderRequest(symbol="usAAPL", market="us", side="buy",
                                        qty=10, order_type="market"))
    assert ok.status == "filled" and ok.filled_qty == 10


def test_engine_submit_gate_is_wired():
    """Engine 源码必须调用 submit_gate（结构上不能绕过提交阶段）。"""
    import ai_trader.strategy.engine as mod
    text = Path(mod.__file__).read_text(encoding="utf-8")
    assert "submit_gate(" in text, "Engine 未接入提交闸门"


# ====================================================================== #
# 辅助
# ====================================================================== #
class _FixedQuotes:
    def __init__(self, price: float = 100.0):
        self.price = price

    def quote(self, symbol: str):
        from ai_trader.broker.base import Quote
        return Quote(symbol=symbol, market="us", last=self.price, prev_close=self.price)


def tmp_path_or_none():
    import tempfile
    return Path(tempfile.mkdtemp())


def _paper(state_dir: Path, market_status=None) -> PaperBroker:
    return PaperBroker(state_path=state_dir / "paper_halt.json",
                       initial_cash={"USD": 1_000_000.0},
                       commission_bps=0.0, min_commission=0.0, slippage_bps=0.0,
                       quote_source=_FixedQuotes(100.0), market_status=market_status)
