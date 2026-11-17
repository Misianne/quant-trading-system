"""行情质量：重复/乱序/缺口/过期/未来/时间异常/断线/多交易所隔离。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ai_trader.market.providers import (
    AlpacaProvider,
    AuthorityRouter,
    DataQuality,
    EventAuthority,
    FeedUnavailable,
    HaltEvent,
    MassiveProvider,
    ProviderAuthoritySpec,
    ProviderError,
    ProviderFeed,
    QualityFlag,
    QuoteEvent,
    TradeEvent,
)
from ai_trader.market.providers.base import _SequenceTracker

T0 = datetime(2026, 11, 16, 14, 30, tzinfo=timezone.utc)


def make_fixture(path: Path, rows) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def alpaca_row(i: int, ts: datetime = None, sym="AAPL"):
    ts = ts or T0
    return {"T": "t", "S": sym, "x": "V", "p": 100.0 + i, "s": 1,
            "t": ts.isoformat().replace("+00:00", "Z"), "i": i}


# ---------------------------------------------------------------------- #
# 序号守卫
# ---------------------------------------------------------------------- #
def test_sequence_first_then_in_order():
    g = _SequenceTracker()
    assert g.check("AAPL", "nasdaq", 1) == "first"
    assert g.check("AAPL", "nasdaq", 2) == "in_order"


def test_sequence_duplicate():
    g = _SequenceTracker()
    g.check("AAPL", "nasdaq", 5)
    assert g.check("AAPL", "nasdaq", 5) == "duplicate"


def test_sequence_regression():
    g = _SequenceTracker()
    g.check("AAPL", "nasdaq", 5)
    assert g.check("AAPL", "nasdaq", 3) == "regression"


def test_sequence_gap():
    g = _SequenceTracker()
    g.check("AAPL", "nasdaq", 1)
    assert g.check("AAPL", "nasdaq", 4) == "gap"
    assert g.last_gap("AAPL", "nasdaq") == (1, 4)


def test_sequence_isolated_per_exchange():
    """同一 symbol 在不同交易所各有序号空间，不得互相干扰。"""
    g = _SequenceTracker()
    g.check("AAPL", "nasdaq", 100)
    assert g.check("AAPL", "nyse", 1) == "first"
    assert g.check("AAPL", "nyse", 2) == "in_order"
    assert g.check("AAPL", "nasdaq", 101) == "in_order"


def test_sequence_reset():
    g = _SequenceTracker()
    g.check("AAPL", "nasdaq", 9)
    g.reset("AAPL")
    assert g.check("AAPL", "nasdaq", 1) == "first"


# ---------------------------------------------------------------------- #
# 适配器级质量标记
# ---------------------------------------------------------------------- #
def test_duplicate_messages_flagged(tmp_path):
    rows = [alpaca_row(1), alpaca_row(2)]
    p = AlpacaProvider(make_fixture(tmp_path / "a.jsonl", rows),
                       {"duplicate_every": 1, "batch_size": 10})
    p.connect(); p.subscribe(["AAPL"])
    evs = p.poll()
    assert any(QualityFlag.DUPLICATE in e.quality.flags for e in evs)
    assert p.health.duplicate >= 1


def test_out_of_order_flagged(tmp_path):
    rows = [alpaca_row(1), alpaca_row(2), alpaca_row(3)]
    p = AlpacaProvider(make_fixture(tmp_path / "a.jsonl", rows),
                       {"swap_pairs": True, "batch_size": 10})
    p.connect(); p.subscribe(["AAPL"])
    evs = p.poll()
    assert any(e.quality.flags for e in evs)


def test_gap_flagged(tmp_path):
    rows = [alpaca_row(i) for i in range(1, 6)]
    p = AlpacaProvider(make_fixture(tmp_path / "a.jsonl", rows),
                       {"drop_indexes": [2], "batch_size": 10})
    p.connect(); p.subscribe(["AAPL"])
    evs = p.poll()
    assert any(QualityFlag.GAP in e.quality.flags for e in evs)
    assert p.health.gaps >= 1


def test_stale_event_flagged(tmp_path):
    old = datetime.now(timezone.utc) - timedelta(seconds=600)
    p = AlpacaProvider(make_fixture(tmp_path / "a.jsonl", [alpaca_row(1, old)]),
                       {"max_age_seconds": 60, "batch_size": 10,
                        "replay_clock": False})
    p.connect(); p.subscribe(["AAPL"])
    evs = p.poll()
    assert QualityFlag.STALE in evs[0].quality.flags


def test_future_event_flagged(tmp_path):
    future = datetime.now(timezone.utc) + timedelta(minutes=10)
    p = AlpacaProvider(make_fixture(tmp_path / "a.jsonl", [alpaca_row(1, future)]),
                       {"max_future_seconds": 5, "batch_size": 10,
                        "replay_clock": False})
    p.connect(); p.subscribe(["AAPL"])
    evs = p.poll()
    assert QualityFlag.FUTURE in evs[0].quality.flags


def test_naive_timestamp_flagged(tmp_path):
    row = {"T": "t", "S": "AAPL", "x": "V", "p": 100.0, "s": 1,
           "t": "2026-11-16T14:30:00"}
    p = AlpacaProvider(make_fixture(tmp_path / "a.jsonl", [row]), {"batch_size": 10})
    p.connect(); p.subscribe(["AAPL"])
    evs = p.poll()
    assert QualityFlag.NAIVE_TIME in evs[0].quality.flags


def test_provider_down_raises(tmp_path):
    p = AlpacaProvider(make_fixture(tmp_path / "a.jsonl", [alpaca_row(1)]),
                       {"down_at": [0], "batch_size": 10})
    p.connect(); p.subscribe(["AAPL"])
    with pytest.raises(ProviderError):
        p.poll()


def test_disconnected_provider_refuses_poll(tmp_path):
    p = AlpacaProvider(make_fixture(tmp_path / "a.jsonl", [alpaca_row(1)]))
    with pytest.raises(ProviderError):
        p.poll()


def test_reconnect_increments_counter(tmp_path):
    p = AlpacaProvider(make_fixture(tmp_path / "a.jsonl", [alpaca_row(1)]))
    p.connect()
    p.reconnect()
    assert p.health.reconnects == 1 and p.connected


def test_close_marks_disconnected(tmp_path):
    p = AlpacaProvider(make_fixture(tmp_path / "a.jsonl", [alpaca_row(1)]))
    p.connect(); p.close()
    assert p.connected is False and p.health.connected is False


# ---------------------------------------------------------------------- #
# Feed 层
# ---------------------------------------------------------------------- #
def test_feed_drops_unusable_events(tmp_path):
    old = T0 - timedelta(days=30)
    p = AlpacaProvider(make_fixture(tmp_path / "a.jsonl", [alpaca_row(1, old)]),
                       {"max_age_seconds": 60, "replay_clock": False})
    feed = ProviderFeed([p], max_age_seconds=60)
    feed.connect_all(); feed.subscribe(["AAPL"])
    evs = feed.poll()
    assert evs == [] and feed.dropped >= 1


def test_feed_quote_raises_when_no_data():
    feed = ProviderFeed([])
    with pytest.raises(FeedUnavailable):
        feed.quote("AAPL")


def test_feed_quote_returns_latest_trade(tmp_path):
    rows = [alpaca_row(1), alpaca_row(2)]
    p = AlpacaProvider(make_fixture(tmp_path / "a.jsonl", rows), {"batch_size": 10})
    # 样本时间是固定历史日期，与真实墙钟的"年龄"必然很大；
    # 本用例只验证"取最新一笔"，所以把新鲜度阈值放开，避免随日期漂移。
    feed = ProviderFeed([p], max_age_seconds=10 ** 9)
    feed.connect_all(); feed.subscribe(["AAPL"]); feed.poll()
    q = feed.quote("AAPL")
    assert q.last == 102.0


def test_feed_quote_refuses_when_stale(tmp_path):
    past = datetime.now(timezone.utc) - timedelta(seconds=600)
    rows = [alpaca_row(1, past)]
    p = AlpacaProvider(make_fixture(tmp_path / "a.jsonl", rows),
                       {"batch_size": 10, "replay_clock": False,
                        "max_age_seconds": 10_000})
    feed = ProviderFeed([p], max_age_seconds=60)
    feed.connect_all(); feed.subscribe(["AAPL"]); feed.poll()
    with pytest.raises(FeedUnavailable):
        feed.quote("AAPL")


def test_feed_vendor_halt_cannot_set_halted(tmp_path):
    """供应商 halt 不是官方事件：不得让 feed 进入停牌态。"""
    rom = [{"T": "t", "S": "AAPL", "x": "V", "p": 100.0, "s": 1,
            "t": T0.isoformat().replace("+00:00", "Z"), "i": 1}]
    p = AlpacaProvider(make_fixture(tmp_path / "a.jsonl", rom), {"batch_size": 10})
    feed = ProviderFeed([p])          # 无 authority router → 一律不可授权
    feed.connect_all(); feed.subscribe(["AAPL"]); feed.poll()
    st = feed.state("AAPL")
    assert st is not None and st.halted is False


def test_feed_official_halt_sets_state(tmp_path):
    """只有注册为原生且带签名的 provider 事件才允许改停牌态。"""
    rows = [{"symbol": "AAPL", "action": "H", "reason": "LUDP",
             "timestamp": int(T0.timestamp() * 1e9), "sequence": 1, "id": "1"}]
    from ai_trader.market.providers import NasdaqItchProvider
    p = NasdaqItchProvider(make_fixture(tmp_path / "n.jsonl", rows), {"batch_size": 10})
    router = AuthorityRouter(
        [ProviderAuthoritySpec("nasdaq_itch", EventAuthority.EXCHANGE_HALT,
                               exchanges=("nasdaq",), native=True, signed=True)],
        verifier=_AlwaysVerified(), require_signature_for_resume=True)
    feed = ProviderFeed([p], authority_router=router)
    feed.connect_all(); feed.subscribe(["AAPL"]); feed.poll()
    st = feed.state("AAPL")
    assert st.halted is True


class _AlwaysVerified:
    name = "test-verifier"
    def verify(self, ev, raw):
        from ai_trader.market.providers import SignatureStatus
        return SignatureStatus.VERIFIED, "test"


def test_feed_health_shape(tmp_path):
    p = AlpacaProvider(make_fixture(tmp_path / "a.jsonl", [alpaca_row(1)]))
    feed = ProviderFeed([p])
    feed.connect_all()
    h = feed.health()
    assert "providers" in h and "accepted" in h and "dropped" in h


def test_feed_unsubscribe(tmp_path):
    p = AlpacaProvider(make_fixture(tmp_path / "a.jsonl", [alpaca_row(1)]))
    feed = ProviderFeed([p])
    feed.connect_all(); feed.subscribe(["AAPL", "MSFT"])
    p.unsubscribe(["AAPL"])
    assert p.subscriptions() == ["MSFT"]
