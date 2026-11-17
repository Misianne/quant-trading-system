"""WS 主题登记表、来源合并策略、WS→网关桥 的测试（离线，无网络）。

既有的 `MarketFeed`（握手/订阅/心跳/重连/去重）已有 90 项测试覆盖，
本文件只覆盖本轮新增的两层：主题证据表 + 网关桥与合并策略。
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ai_trader.broker.paper import PaperBroker
from ai_trader.market.clock import ExchangeClock, SessionWindow
from ai_trader.market.gateway import QuoteGateway
from ai_trader.market.providers import fosun_gateway_bridge as B
from ai_trader.market.providers import fosun_topics as T
from ai_trader.market.sessions import QuoteUnavailable

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 17, 15, 0, tzinfo=timezone.utc)   # 10:00 ET（常规盘）


class FakeTick:
    def __init__(self, symbol="AAPL", last=352.78, prev_close=356.27, ts=None):
        self.symbol = symbol
        self.last = last
        self.prev_close = prev_close
        self.ts = ts if ts is not None else NOW - timedelta(seconds=1)
        self.received_at = NOW
        self.source = "fosun-ws"
        self.seq = 1


class FakeFeed:
    def __init__(self, ticks=None, fresh=True, detail="ok"):
        self._ticks = dict(ticks or {})
        self._fresh = fresh
        self._detail = detail

    def tick(self, symbol):
        return self._ticks.get(str(symbol).upper())

    def is_fresh(self, symbol, now=None):
        return self._fresh, (self._detail if self._fresh else "stale")


def bridge(feed, **kw):
    return B.WsQuoteBridge(feed, now_fn=lambda: NOW, **kw)


def ny_clock():
    return ExchangeClock("us", [SessionWindow(time(9, 30), time(16, 0))],
                         tz_name="America/New_York",
                         pre_window=SessionWindow(time(4, 0), time(9, 30)),
                         post_window=SessionWindow(time(16, 0), time(20, 0)),
                         allow_extended=False)


# ---------------------------------------------------------------------- #
# 主题登记表：证据优先，不做语义猜测
# ---------------------------------------------------------------------- #
def test_topic_registry_quotes_official_comments():
    for name, spec in T.TOPICS.items():
        assert spec.evidence in (T.SDK_COMMENT, T.UNKNOWN)
        assert spec.quote, f"{name} 必须带官方原文出处"
        assert spec.as_dict()["topic"] == name


def test_night_topics_are_registered():
    for t in ("qt_nt", "min_nt", "tk_nt"):
        assert T.TOPICS[t].sessions == ("night",)
    assert set(T.topics_for_session("night")) >= {"qt_nt", "min_nt", "tk_nt", "bq"}


def test_pre_post_topics_are_registered():
    for t in ("pqt", "pmin", "ptk"):
        assert T.TOPICS[t].sessions == ("pre", "post")
    assert set(T.topics_for_session("pre")) >= {"pqt", "pmin", "ptk", "ob"}
    assert set(T.topics_for_session("post")) >= {"pqt", "pmin", "ptk", "ob"}


def test_unclassified_topics_can_be_included_but_stay_unclassified():
    base = set(T.topics_for_session("regular"))
    assert "qt" not in base, "qt 的注释没提时段，不能默认它覆盖常规盘"
    wider = set(T.topics_for_session("regular", include_unclassified=True))
    assert "qt" in wider and "tk" in wider


def test_unknown_topic_is_rejected():
    with pytest.raises(ValueError, match="未登记"):
        T.assert_known("qt_xx")
    assert T.assert_known("qt").name == "qt"


def test_heartbeat_is_not_a_business_topic():
    assert T.HEARTBEAT_TOPIC == "hb"
    assert "hb" not in T.TOPICS


def test_all_payload_schemas_are_still_unknown():
    """从未收到过真实推送样本 → 所有主题的消息结构都必须是 UNKNOWN。

    这是刻意的诚实标记：一旦有人偷偷"按 REST 字段猜" WS 结构，
    这条测试会失败。
    """
    assert set(T.unknown_payload_topics()) == set(T.TOPICS)
    assert T.BOUNDARY_EVIDENCE == T.UNKNOWN


# ---------------------------------------------------------------------- #
# 合并策略：REST 延迟不得覆盖 WS 实时
# ---------------------------------------------------------------------- #
def mark(ts, delayed, source):
    return B.QuoteMark(ts=ts, delayed=delayed, source=source)


def test_no_previous_always_accepts():
    assert B.should_replace(None, mark(NOW, False, "ws")) is True


def test_newer_ws_replaces_older_rest():
    old = mark(NOW - timedelta(seconds=5), True, B.SOURCE_REST)
    new = mark(NOW, False, B.SOURCE_WS)
    assert B.should_replace(old, new) is True
    assert B.merge_reason(old, new) == "newer"


def test_older_rest_cannot_overwrite_newer_ws():
    cur = mark(NOW, False, B.SOURCE_WS)
    inc = mark(NOW - timedelta(seconds=3), True, B.SOURCE_REST)
    assert B.should_replace(cur, inc) is False
    assert B.merge_reason(cur, inc) == "older_than_current"


def test_delayed_cannot_overwrite_realtime_at_same_timestamp():
    cur = mark(NOW, False, B.SOURCE_WS)
    inc = mark(NOW, True, B.SOURCE_REST)
    assert B.should_replace(cur, inc) is False
    assert B.merge_reason(cur, inc) == "same_timestamp_keep_current"


def test_realtime_can_upgrade_delayed_at_same_timestamp():
    cur = mark(NOW, True, B.SOURCE_REST)
    inc = mark(NOW, False, B.SOURCE_WS)
    assert B.should_replace(cur, inc) is True
    assert B.merge_reason(cur, inc) == "realtime_upgrade"


def test_incoming_without_timestamp_is_rejected():
    cur = mark(NOW, False, B.SOURCE_WS)
    inc = mark(None, False, B.SOURCE_REST)
    assert B.should_replace(cur, inc) is False
    assert B.merge_reason(cur, inc) == "incoming_without_timestamp"


def test_previous_without_timestamp_is_replaceable():
    cur = mark(None, True, B.SOURCE_REST)
    inc = mark(NOW, False, B.SOURCE_WS)
    assert B.should_replace(cur, inc) is True


# ---------------------------------------------------------------------- #
# WS → QuoteGateway 桥
# ---------------------------------------------------------------------- #
def test_bridge_returns_quote_without_fabricating_book():
    q = bridge(FakeFeed({"AAPL": FakeTick()})).quote("AAPL")
    assert q.last == pytest.approx(352.78)
    assert q.prev_close == pytest.approx(356.27)
    assert q.source == B.SOURCE_WS
    assert q.ts, "必须带官方行情时间戳"
    # QuoteTick 没有盘口，这里就不能编
    assert q.open == 0.0 and q.high == 0.0 and q.low == 0.0 and q.volume == 0.0


def test_bridge_raises_without_tick():
    with pytest.raises(B.WsQuoteUnavailable, match="无 WS 行情"):
        bridge(FakeFeed({})).quote("AAPL")


def test_bridge_raises_when_feed_not_fresh():
    feed = FakeFeed({"AAPL": FakeTick()}, fresh=False)
    with pytest.raises(B.WsQuoteUnavailable, match="不可用"):
        bridge(feed).quote("AAPL")


def test_bridge_raises_when_stale_by_max_age():
    old = FakeTick(ts=NOW - timedelta(seconds=30))
    with pytest.raises(B.WsQuoteUnavailable, match="过期"):
        bridge(FakeFeed({"AAPL": old}), max_age_seconds=5).quote("AAPL")


def test_bridge_rejects_invalid_price():
    with pytest.raises(B.WsQuoteUnavailable, match="价格无效"):
        bridge(FakeFeed({"AAPL": FakeTick(last=0.0)})).quote("AAPL")


def test_bridge_without_feed():
    with pytest.raises(B.WsQuoteUnavailable, match="未注入"):
        bridge(None).quote("AAPL")


def test_bridge_feeds_quote_gateway_end_to_end():
    """WS 报价真的能进现有 QuoteGateway（不改 gateway 一行）。"""
    b = bridge(FakeFeed({"AAPL": FakeTick()}))
    broker = PaperBroker(state_path=Path(__import__("tempfile").mkdtemp()) / "p.json",
                         initial_cash={"USD": 100000.0})
    gw = QuoteGateway(broker, {"us": ny_clock()}, enabled=True, provider=b)
    # 行情 ts = 14:59:59Z = 09:59:59 ET，落在常规时段
    px = gw.price("AAPL", "regular_only", market="us", now=NOW)
    assert px == pytest.approx(352.78)
    assert gw.snapshot()["source"] == "provider"


def test_gateway_rejects_when_ws_quote_stale():
    old = FakeTick(ts=NOW - timedelta(minutes=10))
    b = bridge(FakeFeed({"AAPL": old}))
    broker = PaperBroker(state_path=Path(__import__("tempfile").mkdtemp()) / "p.json",
                         initial_cash={"USD": 100000.0})
    gw = QuoteGateway(broker, {"us": ny_clock()}, enabled=True, provider=b)
    with pytest.raises((QuoteUnavailable, B.WsQuoteUnavailable)):
        gw.price("AAPL", "regular_only", market="us", now=NOW)


def test_bridge_mark_is_realtime():
    m = bridge(FakeFeed({"AAPL": FakeTick()})).mark("AAPL")
    assert m is not None and m.delayed is False and m.source == B.SOURCE_WS
    assert bridge(FakeFeed({})).mark("AAPL") is None
