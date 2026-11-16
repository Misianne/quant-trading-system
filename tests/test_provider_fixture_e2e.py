"""录制样本端到端：fixture → ProviderFeed → QuoteGateway → 风控/撮合。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ai_trader import timebase
from ai_trader.broker.base import OrderRequest
from ai_trader.broker.paper import PaperBroker
from ai_trader.config import load_config
from ai_trader.market.clock import ExchangeClock, SessionWindow
from ai_trader.market.gateway import QuoteGateway
from ai_trader.market.providers import (
    AlpacaProvider,
    AuthorityRouter,
    EventAuthority,
    FeedUnavailable,
    MassiveProvider,
    NasdaqItchProvider,
    NoSignatureVerifier,
    ProviderAuthoritySpec,
    ProviderFeed,
    SignatureStatus,
)
from ai_trader.market.sessions import QuoteUnavailable

FIX = Path(__file__).resolve().parent / "fixtures" / "marketdata"
CFG = load_config()


class Verified:
    name = "verified"

    def verify(self, ev, raw):
        return SignatureStatus.VERIFIED, "ok"


def ny_clock():
    return ExchangeClock("us", [SessionWindow(__import__("datetime").time(9, 30),
                                              __import__("datetime").time(16, 0))],
                         tz_name="America/New_York",
                         pre_window=SessionWindow(__import__("datetime").time(4, 0),
                                                  __import__("datetime").time(9, 30)),
                         post_window=SessionWindow(__import__("datetime").time(16, 0),
                                                   __import__("datetime").time(20, 0)),
                         allow_extended=False)


def gateway_with(feed):
    broker = PaperBroker(state_path=Path(__import__("tempfile").mkdtemp()) / "p.json",
                         initial_cash={"USD": 100000.0})
    return QuoteGateway(broker, {"us": ny_clock()}, enabled=True, provider=feed)


# ---------------------------------------------------------------------- #
def test_fixture_quote_flows_through_gateway():
    p = AlpacaProvider(FIX / "alpaca_trades.jsonl", {"batch_size": 10})
    feed = ProviderFeed([p])
    feed.connect_all(); feed.subscribe(["AAPL"]); feed.poll()
    gw = gateway_with(feed)
    # fixture 时间是 2026-11-16 14:30Z = 09:30 ET，属于常规时段
    # fixture 覆盖 14:30:00Z–14:30:12Z，取最后一笔之后的时间作为现在
    px = gw.price("AAPL", "regular_only", market="us",
                  now=datetime(2026, 11, 16, 14, 30, 12, tzinfo=timezone.utc))
    assert px == pytest.approx(201.2)


def test_fixture_replay_is_deterministic():
    def run():
        p = MassiveProvider(FIX / "massive_luld.jsonl", {"batch_size": 10})
        feed = ProviderFeed([p])
        feed.connect_all(); feed.subscribe(["AAPL"])
        evs = feed.poll()
        return [e.as_dict() for e in evs]
    assert run() == run()


def test_stale_fixture_blocks_gateway():
    p = AlpacaProvider(FIX / "alpaca_trades.jsonl", {"batch_size": 10})
    feed = ProviderFeed([p], max_age_seconds=1)
    feed.connect_all(); feed.subscribe(["AAPL"]); feed.poll()
    gw = gateway_with(feed)
    with pytest.raises((QuoteUnavailable, FeedUnavailable)):
        gw.price("AAPL", "regular_only", market="us",
                 now=datetime(2026, 11, 16, 15, 30, tzinfo=timezone.utc))


def test_vendor_halt_does_not_block_trading():
    """供应商没有官方停牌事件 → 不能因为"感觉危险"就自行停牌，也不能假装有。"""
    p = AlpacaProvider(FIX / "alpaca_trades.jsonl", {"batch_size": 10})
    feed = ProviderFeed([p])
    feed.connect_all(); feed.subscribe(["AAPL"]); feed.poll()
    assert feed.state("AAPL").halted is False


def test_official_halt_blocks_quote():
    rows = [{"symbol": "AAPL", "action": "H", "reason": "LUDP",
             "timestamp": int(datetime(2026, 11, 16, 14, 30,
                                       tzinfo=timezone.utc).timestamp() * 1e9),
             "sequence": 1, "id": "1"}]
    f = Path(__import__("tempfile").mkdtemp()) / "n.jsonl"
    f.write_text(json.dumps(rows[0]) + "\n", encoding="utf-8")
    p = NasdaqItchProvider(f, {"batch_size": 5})
    router = AuthorityRouter(
        [ProviderAuthoritySpec("nasdaq_itch", EventAuthority.EXCHANGE_HALT,
                               exchanges=("nasdaq",), native=True, signed=True)],
        verifier=Verified())
    feed = ProviderFeed([p], authority_router=router)
    feed.connect_all(); feed.subscribe(["AAPL"]); feed.poll()
    with pytest.raises(FeedUnavailable, match="停牌"):
        feed.quote("AAPL")


def test_unsigned_official_halt_is_ignored():
    """没有签名 → 即使 provider 是交易所原生，也不得改状态。"""
    rows = [{"symbol": "AAPL", "action": "H", "reason": "LUDP",
             "timestamp": int(datetime(2026, 11, 16, 14, 30,
                                       tzinfo=timezone.utc).timestamp() * 1e9),
             "sequence": 1, "id": "1"}]
    f = Path(__import__("tempfile").mkdtemp()) / "n.jsonl"
    f.write_text(json.dumps(rows[0]) + "\n", encoding="utf-8")
    p = NasdaqItchProvider(f, {"batch_size": 5})
    router = AuthorityRouter(
        [ProviderAuthoritySpec("nasdaq_itch", EventAuthority.EXCHANGE_HALT,
                               exchanges=("nasdaq",), native=True, signed=True)],
        verifier=NoSignatureVerifier())
    feed = ProviderFeed([p], authority_router=router)
    feed.connect_all(); feed.subscribe(["AAPL"]); feed.poll()
    assert feed.state("AAPL").halted is False
    assert feed.rejected.get("halt_not_authoritative", 0) >= 1


def test_paper_broker_blocks_fill_during_official_halt():
    """停牌期间 PaperBroker 不得成交（阶段 C 门禁）。"""
    from ai_trader.market.status import MarketStatusService
    ms = MarketStatusService()
    now = datetime.now(timezone.utc)
    ms.record_trade("usAAPL", 100.0, now)
    ms.halt_symbol("usAAPL", ts=now)
    b = PaperBroker(state_path=Path(__import__("tempfile").mkdtemp()) / "p.json",
                    initial_cash={"USD": 100000.0},
                    commission_bps=0, min_commission=0, slippage_bps=0,
                    market_status=ms)
    b._state["cash"]["USD"] = 100000.0
    b.update_quote("AAPL", 100.0) if hasattr(b, "update_quote") else None
    r = b.place_order(OrderRequest(symbol="usAAPL", market="us", side="buy",
                                   qty=1, price=100.0, order_type="market"))
    assert r.status != "filled"


def test_gateway_snapshot_reports_provider():
    p = AlpacaProvider(FIX / "alpaca_trades.jsonl", {"batch_size": 10})
    feed = ProviderFeed([p])
    feed.connect_all(); feed.subscribe(["AAPL"]); feed.poll()
    snap = gateway_with(feed).snapshot()
    assert snap["source"] == "provider"
    assert "provider_health" in snap


def test_feed_isolated_per_exchange_state():
    """同一 symbol 在两个交易所的状态必须分开。"""
    from ai_trader.market.providers.models import QuoteEvent
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    feed = ProviderFeed([])
    e1 = QuoteEvent(provider="x", symbol="AAPL", exchange="nasdaq",
                    event_time=now, receive_time=now, bid=100, ask=101)
    e2 = QuoteEvent(provider="x", symbol="AAPL", exchange="nyse",
                    event_time=now, receive_time=now, bid=200, ask=201)
    feed._ingest(e1); feed._ingest(e2)
    assert feed.state("AAPL", "nasdaq").price() == 100.5
    assert feed.state("AAPL", "nyse").price() == 200.5


def test_feed_does_not_let_old_overwrite_new():
    from ai_trader.market.providers.models import TradeEvent
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    feed = ProviderFeed([])
    new = TradeEvent(provider="x", symbol="AAPL", exchange="nasdaq",
                     event_time=now, receive_time=now, price=200.0, size=1)
    old = TradeEvent(provider="x", symbol="AAPL", exchange="nasdaq",
                     event_time=now - timedelta(minutes=5), receive_time=now,
                     price=100.0, size=1)
    feed._ingest(new)
    feed._ingest(old)          # 迟到事件
    assert feed.state("AAPL", "nasdaq").price() == 200.0, "旧数据覆盖了新数据"


def test_gateway_rejects_cross_session_use():
    """盘后样本不得当作常规时段价使用。"""
    rows = [{"T": "t", "S": "AAPL", "x": "V", "p": 100.0, "s": 1,
             "t": "2026-11-16T22:30:00Z", "i": 1}]   # 17:30 ET
    f = Path(__import__("tempfile").mkdtemp()) / "a.jsonl"
    f.write_text(json.dumps(rows[0]) + "\n", encoding="utf-8")
    p = AlpacaProvider(f, {"batch_size": 5})
    feed = ProviderFeed([p])
    feed.connect_all(); feed.subscribe(["AAPL"]); feed.poll()
    gw = gateway_with(feed)
    with pytest.raises(QuoteUnavailable):
        gw.price("AAPL", "regular_only", market="us",
                 now=datetime(2026, 11, 16, 22, 30, tzinfo=timezone.utc))


def test_fixture_data_does_not_touch_real_accounts(tmp_path):
    """所有测试使用独立临时库；项目 data/ 下不应新增文件。"""
    before = len(list(Path("data").glob("*.db")))
    p = AlpacaProvider(FIX / "alpaca_trades.jsonl", {"batch_size": 10})
    feed = ProviderFeed([p])
    feed.connect_all(); feed.subscribe(["AAPL"]); feed.poll()
    after = len(list(Path("data").glob("*.db")))
    assert after == before
