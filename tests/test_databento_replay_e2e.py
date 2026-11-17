"""真实样本（官方字段形状）端到端：Databento → Feed → 网关 → 账本。

这条链路必须证明的是**拒绝能力**，不只是"能用"：

1. 合格行情能进入网关并给出价格；
2. 停牌后网关拒绝出价（策略拿不到价 → 不会下单）；
3. 即使强行下单，Broker 的成交门禁也不放行；
4. 未认证的恢复事件不能解除停牌；
5. 乱序/重复数据不能覆盖有效新值；
6. 同一份样本重复运行，结果指纹完全一致。
"""
from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import pytest

from ai_trader.broker.base import OrderRequest
from ai_trader.broker.paper import PaperBroker
from ai_trader.market.clock import ExchangeClock, SessionWindow
from ai_trader.market.gateway import QuoteGateway
from ai_trader.market.providers import (
    AuthorityRouter,
    DatabentoProvider,
    FeedUnavailable,
    ProviderFeed,
    surveyed_specs,
)
from ai_trader.market.sessions import QuoteUnavailable

VER = Path(__file__).resolve().parents[1] / "verification" / "databento" / "fixtures"
SYM = json.loads((VER / "symbology.json").read_text(encoding="utf-8"))

T0 = datetime(2026, 11, 16, 14, 30, 0, tzinfo=timezone.utc)   # 09:30 ET
NOW = T0 + timedelta(seconds=13)


def ny_clock():
    return ExchangeClock(
        "us", [SessionWindow(time(9, 30), time(16, 0))],
        tz_name="America/New_York",
        pre_window=SessionWindow(time(4, 0), time(9, 30)),
        post_window=SessionWindow(time(16, 0), time(20, 0)),
        allow_extended=False)


@pytest.fixture(autouse=True)
def _pin_clock():
    from ai_trader.paper_runtime.timebase_bridge import (install_replay_clock,
                                                         restore_clock)
    install_replay_clock(NOW)
    yield
    restore_clock()


def make_feed(name, *, batch=1, max_age=60.0, extra_providers=()):
    prov = DatabentoProvider(VER / name, {"batch_size": batch, "symbology": SYM})
    router = AuthorityRouter(
        [s for s in surveyed_specs() if s.provider == "databento"],
        require_signature_for_resume=True)
    feed = ProviderFeed([prov, *extra_providers], authority_router=router,
                        max_age_seconds=max_age)
    feed.connect_all()
    feed.subscribe(["AAPL"])
    return prov, feed, router


def last_ts(feed, symbol="AAPL"):
    """样本时间线上的"现在" = 最后一笔已摄取事件的时间。

    不能写死一个常量：网关默认 10s 新鲜度，写死的时间一旦比末笔晚十几秒，
    正向用例就会莫名其妙地被判"过期"。
    """
    st = feed.state(symbol, "nasdaq")
    assert st is not None and st.updated_at is not None
    return st.updated_at


def gateway(feed):
    b = PaperBroker(state_path=Path(__import__("tempfile").mkdtemp()) / "p.json",
                    initial_cash={"USD": 100000.0})
    return QuoteGateway(b, {"us": ny_clock()}, enabled=True, provider=feed)


def trades_only_fixture(tmp_path: Path) -> Path:
    """两条成交、无停牌 —— 用于验证"合格行情能进来"这条正向路径。"""
    rows = [
        {"ts_recv": 1794839400000100000, "ts_event": 1794839400000000000,
         "rtype": 0, "publisher_id": 2, "instrument_id": 42, "symbol": "AAPL",
         "action": "T", "side": "B", "depth": 0, "price": 201_200_000_000,
         "size": 100, "flags": 128, "ts_in_delta": -100000, "sequence": 1001},
        {"ts_recv": 1794839401000100000, "ts_event": 1794839401000000000,
         "rtype": 0, "publisher_id": 2, "instrument_id": 42, "symbol": "AAPL",
         "action": "T", "side": "A", "depth": 0, "price": 201_350_000_000,
         "size": 50, "flags": 128, "ts_in_delta": -100000, "sequence": 1002},
    ]
    p = tmp_path / "trades_only.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return p


# ---------------------------------------------------------------------- #
# 正向：合格行情进入网关
# ---------------------------------------------------------------------- #
def test_qualified_quote_enters_gateway(tmp_path):
    prov = DatabentoProvider(trades_only_fixture(tmp_path), {"batch_size": 10})
    router = AuthorityRouter([s for s in surveyed_specs()
                              if s.provider == "databento"])
    feed = ProviderFeed([prov], authority_router=router, max_age_seconds=60)
    feed.connect_all(); feed.subscribe(["AAPL"]); feed.poll()
    gw = gateway(feed)
    assert gw.price("AAPL", "regular_only", market="us",
                    now=last_ts(feed)) == pytest.approx(201.35)
    snap = gw.snapshot()
    assert snap["source"] == "provider" and snap["stats"]["passed"] >= 1


def test_gateway_reports_provider_health():
    _, feed, _ = make_feed("synthetic_luld_pause.jsonl", batch=10)
    feed.poll()
    h = gateway(feed).snapshot()["provider_health"]
    assert h["providers"]["databento"]["connected"] is True


# ---------------------------------------------------------------------- #
# 停牌：网关拒绝 → 不下单 → 不成交
# ---------------------------------------------------------------------- #
def test_halt_blocks_gateway_price():
    prov, feed, _ = make_feed("synthetic_luld_pause.jsonl", batch=1)
    for _ in range(3):
        feed.poll()
    gw = gateway(feed)
    with pytest.raises((QuoteUnavailable, FeedUnavailable)):
        gw.price("AAPL", "regular_only", market="us", now=last_ts(feed))


def test_no_order_and_no_fill_during_halt():
    """策略拿不到价 → 不产生订单；账本必须为空。"""
    prov, feed, _ = make_feed("synthetic_luld_pause.jsonl", batch=1)
    for _ in range(3):
        feed.poll()
    gw = gateway(feed)
    broker = PaperBroker(state_path=Path(__import__("tempfile").mkdtemp()) / "p.json",
                         initial_cash={"USD": 100000.0}, commission_bps=0,
                         min_commission=0, slippage_bps=0, quote_source=feed)

    submitted = []
    try:
        px = gw.price("AAPL", "regular_only", market="us", now=last_ts(feed))
    except (QuoteUnavailable, FeedUnavailable):
        px = None
    if px:
        submitted.append(broker.place_order(OrderRequest(
            symbol="usAAPL", market="us", side="buy", qty=1,
            price=px, order_type="limit")))
    assert submitted == [], "停牌期间不应产生订单"
    assert broker.orders_snapshot() == []


def test_broker_gate_blocks_fill_even_if_forced():
    """即使绕过网关强行下单，Broker 的成交门禁仍须拦住。

    这里用 MarketStatusService 直接表达交易所停牌事实 —— 与 provider 层是
    两个独立防线，任何一层单独失效都不应该导致成交。
    """
    from ai_trader.market.status import MarketStatusService
    ms = MarketStatusService()
    ms.record_trade("usAAPL", 100.0, datetime.now(timezone.utc))
    ms.halt_symbol("usAAPL", ts=datetime.now(timezone.utc))
    b = PaperBroker(state_path=Path(__import__("tempfile").mkdtemp()) / "p.json",
                    initial_cash={"USD": 100000.0}, commission_bps=0,
                    min_commission=0, slippage_bps=0, market_status=ms)
    r = b.place_order(OrderRequest(symbol="usAAPL", market="us", side="buy",
                                   qty=1, price=100.0, order_type="market"))
    assert r.status != "filled"


def test_unsigned_resume_keeps_halt_and_gateway_still_refuses():
    prov, feed, _ = make_feed("synthetic_luld_pause.jsonl", batch=1)
    for _ in range(6):          # 含 14:35 的 TRADING 事件
        feed.poll()
    assert feed.state("AAPL", "nasdaq").halted is True
    with pytest.raises((QuoteUnavailable, FeedUnavailable)):
        gateway(feed).price("AAPL", "regular_only", market="us",
                            now=last_ts(feed))


# ---------------------------------------------------------------------- #
# 数据质量：乱序 / 重复 / 过期
# ---------------------------------------------------------------------- #
def test_out_of_order_cannot_overwrite_newer():
    from ai_trader.market.providers.models import TradeEvent
    feed = ProviderFeed([])
    new = TradeEvent(provider="databento", symbol="AAPL", exchange="nasdaq",
                     event_time=NOW, receive_time=NOW, price=201.35, size=1,
                     sequence=1002)
    old = TradeEvent(provider="databento", symbol="AAPL", exchange="nasdaq",
                     event_time=NOW - timedelta(minutes=5), receive_time=NOW,
                     price=199.00, size=1, sequence=1001)
    feed._ingest(new)
    feed._ingest(old)
    assert feed.state("AAPL", "nasdaq").price() == pytest.approx(201.35)


def test_duplicate_messages_are_flagged_not_double_counted():
    p = DatabentoProvider(VER / "synthetic_luld_pause.jsonl",
                          {"batch_size": 100, "symbology": SYM,
                           "duplicate_every": 1})
    p.connect(); p.subscribe(["AAPL"])
    evs = p.poll()
    dups = [e for e in evs if "duplicate" in [f.value for f in e.quality.flags]]
    assert dups, "重复投递未被标记"
    assert p.health.duplicate >= 1


def test_stale_feed_fails_closed():
    prov, feed, _ = make_feed("synthetic_luld_pause.jsonl", batch=10, max_age=1.0)
    feed.poll()
    with pytest.raises((FeedUnavailable, QuoteUnavailable)):
        gateway(feed).price("AAPL", "regular_only", market="us", now=NOW)


def test_swapped_order_is_rejected_by_feed():
    p = DatabentoProvider(VER / "synthetic_luld_pause.jsonl",
                          {"batch_size": 100, "symbology": SYM,
                           "swap_pairs": True})
    router = AuthorityRouter([s for s in surveyed_specs()
                              if s.provider == "databento"])
    feed = ProviderFeed([p], authority_router=router, max_age_seconds=10 ** 9)
    feed.connect_all(); feed.subscribe(["AAPL"]); feed.poll()
    assert feed.rejected.get("late_event", 0) >= 1, "乱序事件必须被拒绝"


# ---------------------------------------------------------------------- #
# 确定性
# ---------------------------------------------------------------------- #
def _pipeline_digest():
    prov = DatabentoProvider(VER / "synthetic_luld_pause.jsonl",
                             {"batch_size": 100, "symbology": SYM})
    router = AuthorityRouter([s for s in surveyed_specs()
                              if s.provider == "databento"])
    feed = ProviderFeed([prov], authority_router=router, max_age_seconds=10 ** 9)
    feed.connect_all(); feed.subscribe(["AAPL"])
    evs = [e.as_dict() for e in feed.poll()]
    st = feed.state("AAPL", "nasdaq")
    return json.dumps({"events": evs,
                       "halted": st.halted if st else None,
                       "version": st.version if st else 0,
                       "rejected": feed.rejected,
                       "health": feed.health()["providers"]["databento"]},
                      ensure_ascii=False, sort_keys=True, default=str)


def test_same_sample_produces_identical_digest():
    digests = [_pipeline_digest() for _ in range(3)]
    assert len(set(digests)) == 1, "同一份样本必须产生完全一致的结果"
