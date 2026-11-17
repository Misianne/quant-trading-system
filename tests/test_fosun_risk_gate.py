"""风控门禁与网关集成测试（离线）。

这一组是"一票否决"性质的：
* 复星行情必须经过 QuoteGateway，不能直连 PaperBroker；
* 复星行情**永远不能**解除停牌 / LULD / MWCB；
* 停牌期间 PaperBroker 不得成交。
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import pytest

from ai_trader.broker.base import OrderRequest
from ai_trader.broker.paper import PaperBroker
from ai_trader.market.clock import ExchangeClock, SessionWindow
from ai_trader.market.gateway import QuoteGateway
from ai_trader.market.providers import fosun as F
from ai_trader.market.providers.authority import AuthorityRouter, surveyed_specs
from ai_trader.market.providers.feed import FeedUnavailable, ProviderFeed
from ai_trader.market.providers.fosun_protocol import FosunSession
from ai_trader.market.providers.models import (
    EventAuthority, HaltEvent, LULDState, MWCBState)
from ai_trader.market.sessions import QuoteUnavailable

MS = 1794839400000                      # 2026-11-16T14:30:00Z
UTC = datetime(2026, 11, 16, 14, 30, tzinfo=timezone.utc)
SESSION = FosunSession(session_id="S1", signing_key=b"m" * 32,
                       encryption_key=b"e" * 32, expires_at=2 ** 40)


def tick(**over):
    row = {"id": 1, "time": MS, "price": 201.2, "volume": 100, "side": "B"}
    row.update(over)
    return row


class FakeTransport:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def request(self, method, path, *, params=None, json_body=None, idempotent=True):
        self.calls.append((method, path, params))
        item = self.payloads.pop(0) if self.payloads else {"ticks": []}
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self):
        pass


def ny_clock():
    return ExchangeClock(
        "us", [SessionWindow(time(9, 30), time(16, 0))],
        tz_name="America/New_York",
        pre_window=SessionWindow(time(4, 0), time(9, 30)),
        post_window=SessionWindow(time(16, 0), time(20, 0)),
        allow_extended=False)


def build(payloads, *, feed_max_age=10 ** 9, **cfg):
    base = {"max_age_seconds": 10 ** 9}
    base.update(cfg)
    prov = F.FosunProvider(base, transport=FakeTransport(payloads),
                           session_provider=lambda: SESSION)
    feed = ProviderFeed([prov], max_age_seconds=feed_max_age)
    feed.connect_all()
    feed.subscribe(["AAPL"])
    return prov, feed


def gw_for(feed):
    broker = PaperBroker(state_path=Path(__import__("tempfile").mkdtemp()) / "p.json",
                         initial_cash={"USD": 100000.0})
    return QuoteGateway(broker, {"us": ny_clock()}, enabled=True, provider=feed)


# ---------------------------------------------------------------------- #
# 网关
# ---------------------------------------------------------------------- #
def test_quote_flows_through_gateway():
    _, feed = build([{"ticks": [tick(price=201.2)]}])
    feed.poll()
    gw = gw_for(feed)
    px = gw.price("AAPL", "regular_only", market="us", now=UTC + timedelta(seconds=1))
    assert px == pytest.approx(201.2)
    assert gw.snapshot()["source"] == "provider"


def test_gateway_refuses_when_data_stale():
    _, feed = build([{"ticks": [tick()]}], feed_max_age=1.0)
    feed.poll()
    gw = gw_for(feed)
    with pytest.raises((QuoteUnavailable, FeedUnavailable)):
        gw.price("AAPL", "regular_only", market="us", now=UTC + timedelta(minutes=5))


def test_gateway_refuses_after_permission_loss():
    from ai_trader.market.providers.fosun_protocol import FosunPermissionError
    _, feed = build([FosunPermissionError("403")])
    feed.poll()                      # feed 会吞掉并计数
    assert feed.rejected.get("provider_error", 0) >= 1
    with pytest.raises((QuoteUnavailable, FeedUnavailable)):
        gw_for(feed).price("AAPL", "regular_only", market="us", now=UTC)


def test_provider_does_not_touch_broker_directly():
    """Provider 只能产出事件；不得持有或调用 PaperBroker。"""
    prov, _ = build([{"ticks": [tick()]}])
    assert not hasattr(prov, "broker")
    assert not hasattr(prov, "place_order")


# ---------------------------------------------------------------------- #
# 停牌 / LULD / MWCB：复星一律无授权
# ---------------------------------------------------------------------- #
def test_fosun_is_not_in_authority_registry():
    providers = {s.provider for s in surveyed_specs()}
    assert "fosun" not in providers, "复星不得登记为可授权来源"


def test_fosun_halt_event_is_denied():
    router = AuthorityRouter(surveyed_specs(), require_signature_for_resume=True)
    ev = HaltEvent(provider="fosun", symbol="AAPL", exchange="fosun",
                   event_time=UTC, receive_time=UTC, halted=True,
                   authority=EventAuthority.EXCHANGE_HALT)
    v = router.can_apply(ev, symbol="AAPL", exchange="fosun", now=UTC)
    assert not v.accepted
    assert "PROVIDER_NOT_REGISTERED" in v.reason


def test_fosun_luld_state_is_denied():
    router = AuthorityRouter(surveyed_specs(), require_signature_for_resume=True)
    ev = LULDState(provider="fosun", symbol="AAPL", exchange="fosun",
                   event_time=UTC, receive_time=UTC, upper=210, lower=190,
                   resumed=True, authority=EventAuthority.EXCHANGE_HALT)
    v = router.can_apply(ev, symbol="AAPL", exchange="fosun", now=UTC)
    assert not v.accepted and "PROVIDER_NOT_REGISTERED" in v.reason


def test_fosun_mwcb_state_is_denied():
    router = AuthorityRouter(surveyed_specs(), require_signature_for_resume=True)
    ev = MWCBState(provider="fosun", symbol="US500", exchange="fosun",
                   event_time=UTC, receive_time=UTC, level=1, triggered=True,
                   closed_for_day=False,
                   authority=EventAuthority.EXCHANGE_HALT)
    v = router.can_apply(ev, symbol="US500", exchange="fosun", now=UTC)
    assert not v.accepted and "PROVIDER_NOT_REGISTERED" in v.reason


def test_capabilities_block_authoritative_use():
    caps = F.FosunProvider().capabilities()
    for kind in ("halt", "luld", "mwcb"):
        assert caps.supports_authoritative(kind) is False, \
            f"复星不应被声明为 {kind} 的权威来源"


def test_feed_never_applies_halt_from_fosun_quote():
    """即使构造一条 fosun 停牌事件，feed 也必须拒绝写入状态。"""
    _, feed = build([{"ticks": [tick()]}])
    feed.poll()
    ev = HaltEvent(provider="fosun", symbol="AAPL", exchange="fosun",
                   event_time=UTC, receive_time=UTC, halted=True,
                   authority=EventAuthority.EXCHANGE_HALT)
    feed._ingest(ev)
    assert feed.state("AAPL", "fosun").halted is False
    assert feed.rejected.get("halt_not_authoritative", 0) >= 1


# ---------------------------------------------------------------------- #
# PaperBroker 成交门禁
# ---------------------------------------------------------------------- #
def test_paper_broker_blocks_fill_when_market_halted():
    from ai_trader.market.status import MarketStatusService

    ms = MarketStatusService()
    now = datetime.now(timezone.utc)
    ms.record_trade("usAAPL", 100.0, now)
    ms.halt_symbol("usAAPL", ts=now)
    broker = PaperBroker(state_path=Path(__import__("tempfile").mkdtemp()) / "p.json",
                         initial_cash={"USD": 100000.0}, commission_bps=0,
                         min_commission=0, slippage_bps=0, market_status=ms)
    r = broker.place_order(OrderRequest(symbol="usAAPL", market="us", side="buy",
                                       qty=1, price=100.0, order_type="market"))
    assert r.status != "filled"
