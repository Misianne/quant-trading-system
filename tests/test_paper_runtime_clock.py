"""时钟与时段测试：可注入时钟、倒退/超前、DST、早收市、节假日、回放时间戳。"""
from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ai_trader import timebase
from ai_trader.market.clock import ExchangeClock, SessionWindow
from ai_trader.paper_runtime.replay import ReplayQuoteSource
from ai_trader.paper_runtime.timebase_bridge import (
    active,
    install_replay_clock,
    replay_clock,
    restore_clock,
)

NY = ZoneInfo("America/New_York")
WINTER = datetime(2026, 11, 16, 10, 0, tzinfo=NY)
SUMMER = datetime(2026, 7, 15, 10, 0, tzinfo=NY)


def test_manual_clock_rejects_naive():
    with pytest.raises(ValueError):
        timebase.ManualClock(datetime(2026, 11, 16, 10, 0))


def test_manual_clock_rejects_backwards():
    c = timebase.ManualClock(WINTER)
    with pytest.raises(ValueError):
        c.set(WINTER - timedelta(minutes=1))


def test_manual_clock_rejects_negative_advance():
    c = timebase.ManualClock(WINTER)
    with pytest.raises(ValueError):
        c.advance(timedelta(seconds=-1))


def test_frozen_clock_requires_tz():
    with pytest.raises(ValueError):
        timebase.FrozenClock(datetime(2026, 11, 16, 10, 0))


def test_install_and_restore():
    before = timebase.current()
    c = timebase.ManualClock(WINTER)
    timebase.install(c)
    assert timebase.now() == WINTER
    timebase.restore()
    assert timebase.current() is before or timebase.current().name == "system"


def test_bridge_restores_even_on_exception():
    assert active() == 0
    with pytest.raises(RuntimeError):
        with replay_clock(WINTER):
            assert active() == 1
            raise RuntimeError("boom")
    assert active() == 0
    assert timebase.current().name == "system"


def test_bridge_nested():
    with replay_clock(WINTER):
        with replay_clock(WINTER + timedelta(hours=1)):
            assert timebase.now() == WINTER + timedelta(hours=1)
        assert timebase.now() == WINTER
    assert active() == 0


def test_install_then_restore_direct():
    c = install_replay_clock(WINTER)
    c.advance(timedelta(minutes=5))
    assert timebase.now() == WINTER + timedelta(minutes=5)
    restore_clock()
    assert active() == 0


def _clock(holidays=(), early=None):
    return ExchangeClock(
        "us", [SessionWindow(dtime(9, 30), dtime(16, 0))],
        tz_name="America/New_York",
        pre_window=SessionWindow(dtime(4, 0), dtime(9, 30)),
        post_window=SessionWindow(dtime(16, 0), dtime(20, 0)),
        holidays=holidays, early_close=early or {})


def test_winter_and_summer_both_regular_at_10am():
    clk = _clock()
    assert clk.session(WINTER).phase == "regular"
    assert clk.session(SUMMER).phase == "regular"


def test_dst_boundary_offsets_differ():
    clk = _clock()
    # 同为当地 10:00，UTC 偏移在冬夏令时不同（-5 vs -4）
    assert clk.local(WINTER).utcoffset().total_seconds() == -5 * 3600
    assert clk.local(SUMMER).utcoffset().total_seconds() == -4 * 3600


def test_dst_transition_night_is_not_regular():
    clk = _clock()
    # 2026-11-01 凌晨 1:30（EDT→EST 当日）不属于常规时段
    weird = datetime(2026, 11, 1, 1, 30, tzinfo=NY)
    assert clk.session(weird).phase != "regular"


def test_premarket_phase():
    clk = _clock()
    assert clk.session(datetime(2026, 11, 16, 8, 0, tzinfo=NY)).phase == "pre"


def test_afterhours_phase():
    clk = _clock()
    assert clk.session(datetime(2026, 11, 16, 17, 0, tzinfo=NY)).phase == "post"


def test_holiday_phase():
    clk = _clock(holidays=[date(2026, 11, 26)])
    info = clk.session(datetime(2026, 11, 26, 11, 0, tzinfo=NY))
    assert info.phase == "holiday" and not info.is_trading_day


def test_weekend_phase():
    clk = _clock()
    assert clk.session(datetime(2026, 11, 21, 11, 0, tzinfo=NY)).phase == "weekend"


def test_early_close_shifts_end_of_regular():
    clk = _clock(early={date(2026, 11, 27): dtime(13, 0)})
    assert clk.session(datetime(2026, 11, 27, 12, 30, tzinfo=NY)).phase == "regular"
    assert clk.session(datetime(2026, 11, 27, 13, 30, tzinfo=NY)).phase != "regular"
    assert clk.session(datetime(2026, 11, 27, 12, 0, tzinfo=NY)).closes_at == "13:00"


def test_replay_quote_timestamp_follows_clock():
    src = ReplayQuoteSource(prices={"USAAPL": [100.0, 101.0]})
    with replay_clock(WINTER):
        q = src.quote("USAAPL")
        assert q.ts.startswith("2026-11-16T10:00:00-05:00")
        src.set_tick(1)
        q2 = src.quote("USAAPL")
        assert q2.last == 101.0
        assert q2.ts.startswith("2026-11-16T10:00:00")


def test_replay_quote_down_tick_raises():
    from ai_trader.paper_runtime.replay import ReplayFeedDown
    src = ReplayQuoteSource(prices={"USAAPL": [100.0]}, down_ticks=(0,))
    with replay_clock(WINTER):
        with pytest.raises(ReplayFeedDown):
            src.quote("USAAPL")


def test_replay_klines_deterministic():
    src = ReplayQuoteSource(prices={"USAAPL": [100.0]})
    with replay_clock(WINTER):
        a = [c.close for c in src.klines("USAAPL", num=20)]
        b = [c.close for c in src.klines("USAAPL", num=20)]
    assert a == b and len(a) == 20
