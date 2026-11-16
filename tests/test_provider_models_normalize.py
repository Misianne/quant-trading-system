"""数据模型与归一化：字段完整性、时间单位、时区、代码/交易所归一。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ai_trader.market.providers import (
    DataQuality,
    EventAuthority,
    HaltEvent,
    LULDState,
    QualityFlag,
    QuoteEvent,
    TradeEvent,
    normalize_exchange,
    normalize_symbol,
    parse_timestamp,
    session_phase_of,
    to_market_time,
)

T0 = datetime(2026, 11, 16, 14, 30, tzinfo=timezone.utc)


# ---------------------------------------------------------------------- #
# 归一化
# ---------------------------------------------------------------------- #
@pytest.mark.parametrize("raw,expect", [
    ("usAAPL", "AAPL"), ("AAPL", "AAPL"), ("aapl", "AAPL"),
    ("USB", "USB"), ("hk00700", "00700"), ("  MSFT ", "MSFT"),
])
def test_normalize_symbol(raw, expect):
    assert normalize_symbol(raw) == expect


def test_normalize_symbol_empty():
    assert normalize_symbol("") == ""
    assert normalize_symbol(None) == ""


@pytest.mark.parametrize("raw,expect", [
    ("XNAS", "nasdaq"), ("nasdaq", "nasdaq"), ("NYSE", "nyse"),
    ("XNYS", "nyse"), ("BATS", "cboe_bzx"), ("IEX", "iex"),
    ("", "unknown"), ("WEIRD", "weird"),
])
def test_normalize_exchange(raw, expect):
    assert normalize_exchange(raw) == expect


def test_unknown_exchange_is_not_guessed():
    """未知交易所不得被猜成 nyse/nasdaq。"""
    assert normalize_exchange("some_new_venue") == "some_new_venue"


# ---------------------------------------------------------------------- #
# 时间戳
# ---------------------------------------------------------------------- #
def test_parse_iso_with_tz():
    dt, err = parse_timestamp("2026-11-16T14:30:00Z", "iso")
    assert dt == T0 and err == ""


def test_parse_nanoseconds():
    ns = int(T0.timestamp() * 1_000_000_000)
    dt, _ = parse_timestamp(ns, "ns")
    assert abs((dt - T0).total_seconds()) < 1e-6


def test_parse_milliseconds():
    ms = int(T0.timestamp() * 1000)
    dt, _ = parse_timestamp(ms, "ms")
    assert abs((dt - T0).total_seconds()) < 1


def test_parse_seconds():
    dt, _ = parse_timestamp(int(T0.timestamp()), "s")
    assert abs((dt - T0).total_seconds()) < 1


def test_integer_timestamp_requires_unit():
    """整数时间戳必须显式给单位，禁止按位数猜。"""
    dt, err = parse_timestamp(1763303400, "auto")
    assert dt is None and "unit" in err


def test_naive_timestamp_flagged():
    dt, err = parse_timestamp("2026-11-16T14:30:00", "iso")
    assert dt is not None and "时区" in err


def test_invalid_timestamp_returns_error():
    dt, err = parse_timestamp("not-a-time", "iso")
    assert dt is None and err


def test_empty_timestamp():
    assert parse_timestamp(None, "ns")[0] is None


# ---------------------------------------------------------------------- #
# 时区 / DST
# ---------------------------------------------------------------------- #
def test_utc_to_new_york_winter():
    local = to_market_time(T0)
    assert local.hour == 9 and local.minute == 30
    assert local.utcoffset() == timedelta(hours=-5)


def test_utc_to_new_york_summer():
    summer = datetime(2026, 7, 15, 14, 30, tzinfo=timezone.utc)
    local = to_market_time(summer)
    assert local.utcoffset() == timedelta(hours=-4)


def test_session_phase_regular():
    assert session_phase_of(T0) == "regular"


def test_session_phase_premarket():
    assert session_phase_of(T0.replace(hour=12)) == "pre"


def test_session_phase_afterhours():
    assert session_phase_of(T0.replace(hour=21, minute=30)) == "post"


def test_session_phase_overnight():
    assert session_phase_of(T0.replace(hour=2)) == "overnight"


# ---------------------------------------------------------------------- #
# 数据模型
# ---------------------------------------------------------------------- #
def test_event_carries_all_required_fields():
    ev = TradeEvent(provider="p", symbol="AAPL", exchange="nasdaq",
                    event_time=T0, receive_time=T0, sequence=7,
                    source_event_id="x-1", price=100.0, size=10)
    d = ev.as_dict()
    for k in ("provider", "symbol", "exchange", "event_time", "receive_time",
              "sequence", "source_event_id", "authority", "quality"):
        assert k in d
    assert d["price"] == 100.0 and d["size"] == 10


def test_quote_mid_price():
    q = QuoteEvent(provider="p", symbol="AAPL", exchange="nyse",
                   event_time=T0, receive_time=T0, bid=100.0, ask=101.0)
    assert q.mid == 100.5


def test_quote_mid_zero_when_missing():
    q = QuoteEvent(provider="p", symbol="AAPL", exchange="nyse",
                   event_time=T0, receive_time=T0)
    assert q.mid == 0.0


def test_quality_usable_by_default():
    assert DataQuality().usable is True


@pytest.mark.parametrize("flag", list(QualityFlag))
def test_blocking_flags_make_unusable(flag):
    from ai_trader.market.providers.models import BLOCKING_FLAGS
    q = DataQuality().add(flag)
    assert q.usable is (flag not in BLOCKING_FLAGS)


def test_quality_dedup_flags():
    q = DataQuality().add(QualityFlag.STALE).add(QualityFlag.STALE)
    assert q.flags.count(QualityFlag.STALE) == 1


def test_halt_and_luld_models():
    h = HaltEvent(provider="p", symbol="AAPL", exchange="nasdaq",
                  event_time=T0, receive_time=T0, halted=True, reason="LUDP")
    assert h.as_dict()["halted"] is True
    l = LULDState(provider="p", symbol="AAPL", exchange="nasdaq",
                  event_time=T0, receive_time=T0, upper=105, lower=95,
                  indicator="17", paused=True)
    assert l.as_dict()["paused"] is True


def test_authority_enum_has_four_classes():
    assert {a.value[0] for a in EventAuthority} == {"A", "B", "C", "D", "u"}
