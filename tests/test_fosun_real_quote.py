"""真实生产行情 fixture 的解析与网关接入测试（离线，无网络）。

fixture 来源：2026-11-17 通过官方生产 API（`POST /api/v1/market/secu/quote`）
实际取得的一次 `usAAPL` 响应，原样保存、未做任何字段改写。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ai_trader.broker.base import Quote
from ai_trader.broker.paper import PaperBroker
from ai_trader.market.clock import ExchangeClock, SessionWindow
from ai_trader.market.gateway import QuoteGateway
from ai_trader.market.providers import fosun as F
from ai_trader.market.providers.fosun_protocol import FosunSession
from ai_trader.market.sessions import QuoteUnavailable
from datetime import time as dtime

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "fosun" / "quote_usAAPL_real_20261116.json"
RAW = json.loads(FIXTURE.read_text(encoding="utf-8"))
SESSION = FosunSession(session_id="S1", signing_key=b"m" * 32,
                       encryption_key=b"e" * 32, expires_at=2 ** 40)


class FakeTransport:
    """返回**真实 fixture** 的传输替身。"""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def request(self, method, path, *, params=None, json_body=None, idempotent=True):
        self.calls.append((method, path, json_body))
        return self.payload

    def close(self):
        pass


def make_provider(payload=None):
    prov = F.FosunProvider({"max_age_seconds": 10 ** 9},
                           transport=FakeTransport(payload if payload is not None else [RAW]),
                           session_provider=lambda: SESSION)
    prov.connect()
    return prov


def ny_clock():
    return ExchangeClock("us", [SessionWindow(dtime(9, 30), dtime(16, 0))],
                         tz_name="America/New_York",
                         pre_window=SessionWindow(dtime(4, 0), dtime(9, 30)),
                         post_window=SessionWindow(dtime(16, 0), dtime(20, 0)),
                         allow_extended=False)


# ---------------------------------------------------------------------- #
# 解析：放大整数 / 时间
# ---------------------------------------------------------------------- #
def test_real_quote_price_is_scaled_by_1e4():
    snap = F.parse_quote(RAW, symbol="usAAPL", receive_time=datetime.now(timezone.utc))
    assert snap.last == pytest.approx(352.78)
    assert snap.prev_close == pytest.approx(356.27)
    assert snap.open == pytest.approx(357.125)
    assert snap.high == pytest.approx(357.125)
    assert snap.low == pytest.approx(351.31)
    assert snap.volume == pytest.approx(50292161.0)


def test_scale_is_cross_checked_by_official_fields():
    """用官方自己的 chgVal/chgPct 反证 ÷1e4 是对的。

    3527800/1e4=352.78, pClose 3562700/1e4=356.27 → 差 -3.49
    官方 chgVal = -34900 → ÷1e4 = -3.49 ✓
    官方 chgPct = -98 → ÷100 = -0.98% ≈ -3.49/356.27 ✓
    """
    snap = F.parse_quote(RAW, symbol="usAAPL", receive_time=datetime.now(timezone.utc))
    assert (snap.last - snap.prev_close) == pytest.approx(RAW["chgVal"] / F.QUOTE_PRICE_SCALE)
    pct = (snap.last - snap.prev_close) / snap.prev_close * 100
    assert pct == pytest.approx(RAW["chgPct"] / F.QUOTE_PCT_SCALE, abs=0.02)


def test_quote_time_is_converted_from_new_york_to_utc():
    snap = F.parse_quote(RAW, symbol="usAAPL", receive_time=datetime.now(timezone.utc))
    # qtDate=20261116 qtTime=160001579 → 2026-11-16 16:00:01.579 America/New_York = 21:00:01.579Z
    assert snap.event_time == datetime(2026, 11, 16, 21, 0, 1, 579000, tzinfo=timezone.utc)
    assert snap.time_semantics == "vendor_quote_time", \
        "必须标明这是供应商报价时间，不是交易所撮合时间"


def test_real_quote_marks_delay_flag():
    snap = F.parse_quote(RAW, symbol="usAAPL", receive_time=datetime.now(timezone.utc))
    assert snap.delayed is True, "官方返回 delay=true，必须如实带出"
    assert snap.trade_status == 0


def test_real_quote_has_no_book_and_we_do_not_fabricate():
    snap = F.parse_quote(RAW, symbol="usAAPL", receive_time=datetime.now(timezone.utc))
    assert "bid" not in RAW and "ask" not in RAW
    assert snap.bid == 0.0 and snap.ask == 0.0, "没有盘口就不能编一个出来"


def test_missing_qt_time_marks_missing_semantics():
    raw = {k: v for k, v in RAW.items() if k not in ("qtDate", "qtTime")}
    snap = F.parse_quote(raw, symbol="usAAPL", receive_time=datetime.now(timezone.utc))
    assert snap.event_time is None
    assert snap.time_semantics == "missing"


def test_bad_qt_time_raises():
    raw = dict(RAW, qtTime="not-a-time")
    with pytest.raises(F.FosunDataError):
        F.parse_quote(raw, symbol="usAAPL", receive_time=datetime.now(timezone.utc))


# ---------------------------------------------------------------------- #
# Provider.quote() → 网关
# ---------------------------------------------------------------------- #
def test_provider_quote_builds_broker_quote():
    prov = make_provider()
    q = prov.quote("AAPL")
    assert isinstance(q, Quote)
    assert q.last == pytest.approx(352.78)
    assert q.prev_close == pytest.approx(356.27)
    assert q.ts == "2026-11-16T21:00:01.579000+00:00"
    assert q.source == "fosun"


def test_snapshot_quote_handles_list_response():
    prov = make_provider()
    snap = prov.snapshot_quote("AAPL")
    assert snap.last == pytest.approx(352.78)


def test_snapshot_quote_picks_matching_symbol_from_list():
    other = dict(RAW, rawSymbol="MSFT", price=1000000)
    prov = make_provider([other, RAW])
    assert prov.snapshot_quote("AAPL").last == pytest.approx(352.78)


def test_snapshot_quote_fails_closed_on_empty_list():
    prov = make_provider([])
    from ai_trader.market.providers.base import ProviderUnavailable
    with pytest.raises(ProviderUnavailable):
        prov.snapshot_quote("AAPL")


# ---------------------------------------------------------------------- #
# 网关：真实时间戳参与时段判定
# ---------------------------------------------------------------------- #
def gateway_for(provider):
    broker = PaperBroker(state_path=Path(__import__("tempfile").mkdtemp()) / "p.json",
                         initial_cash={"USD": 100000.0})
    return QuoteGateway(broker, {"us": ny_clock()}, enabled=True, provider=provider)


def test_gateway_accepts_fresh_quote_for_display():
    prov = make_provider()
    gw = gateway_for(prov)
    now = datetime(2026, 11, 16, 21, 0, 2, tzinfo=timezone.utc)   # 报价后 1 秒
    assert gw.price("AAPL", "display", market="us", now=now) == pytest.approx(352.78)


def test_gateway_rejects_same_quote_for_regular_session():
    """16:00:01 ET 属于盘后，不能当作常规时段价使用。"""
    prov = make_provider()
    gw = gateway_for(prov)
    now = datetime(2026, 11, 16, 21, 0, 2, tzinfo=timezone.utc)
    with pytest.raises(QuoteUnavailable):
        gw.price("AAPL", "regular_only", market="us", now=now)


def test_gateway_rejects_stale_quote():
    prov = make_provider()
    gw = gateway_for(prov)
    now = datetime(2026, 11, 17, 21, 0, 2, tzinfo=timezone.utc)   # 一天之后
    with pytest.raises(QuoteUnavailable):
        gw.price("AAPL", "display", market="us", now=now)
