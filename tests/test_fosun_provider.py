"""FosunProvider 解析与数据质量测试（离线）。

重点不是"能解析"，而是**拒绝得对不对**：
没有时间戳不伪造、价格非法不放过、重复乱序要看得见。
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest

from ai_trader.market.providers import fosun as F
from ai_trader.market.providers.base import ProviderUnavailable
from ai_trader.market.providers.fosun_protocol import FosunProtocolError
from ai_trader.market.providers.fosun_protocol import (
    FosunPermissionError, FosunSession)
from ai_trader.market.providers.models import QualityFlag, TradeEvent

# 2026-11-16T14:30:00Z，与既有样本时间线一致
MS = 1794839400000
UTC = datetime(2026, 11, 16, 14, 30, tzinfo=timezone.utc)
SESSION = FosunSession(session_id="S1", signing_key=b"m" * 32,
                       encryption_key=b"e" * 32, expires_at=2 ** 40)


def tick(**over):
    row = {"id": 1, "time": MS, "price": 201.2, "volume": 100, "side": "B"}
    row.update(over)
    return row


class FakeTransport:
    def __init__(self, payloads=None):
        self.payloads = list(payloads or [])
        self.calls = []

    def request(self, method, path, *, params=None, json_body=None, idempotent=True):
        self.calls.append((method, path, params))
        item = self.payloads.pop(0) if self.payloads else {"ticks": []}
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self):
        pass


def make_provider(payloads=None, **cfg):
    base = {"max_age_seconds": 10 ** 9, "tick_count": 50}
    base.update(cfg)
    p = F.FosunProvider(base, transport=FakeTransport(payloads),
                        session_provider=lambda: SESSION)
    p.connect()
    p.subscribe(["AAPL"])
    return p


# ---------------------------------------------------------------------- #
# 时间
# ---------------------------------------------------------------------- #
def test_tick_time_is_milliseconds_utc():
    p = make_provider([{"ticks": [tick(time=MS)]}])
    evs = p.poll()
    assert len(evs) == 1
    assert evs[0].event_time == UTC
    assert evs[0].raw_time == str(MS)


def test_kline_time_is_milliseconds():
    bar = F.parse_kline_row([MS, 201.0, 202.0, 200.0, 201.5, 1000], symbol="AAPL")
    assert bar.event_time == UTC
    assert bar.volume == 1000
    assert (bar.open, bar.high, bar.low, bar.close) == (201.0, 202.0, 200.0, 201.5)


@pytest.mark.parametrize("bad", [
    None, "", "abc", 0, -5, 1.5,
    MS // 1000,          # 秒级时间戳（会被年份检查拦下）
    MS * 1000,           # 纳秒级
    float("inf"), float("nan"),
])
def test_invalid_timestamp_rejected(bad):
    with pytest.raises(F.FosunDataError):
        F.ms_to_utc(bad)


def test_seconds_timestamp_is_caught_not_shifted():
    """把秒当毫秒会得到 1970 年 —— 必须被年份检查拦下，而不是静默变成 1970。"""
    with pytest.raises(F.FosunDataError) as ei:
        F.ms_to_utc(1794839400)
    assert ei.value.reason == "bad_time"


def test_tick_without_time_is_rejected_not_faked():
    p = make_provider([{"ticks": [{"id": 1, "price": 201.2, "volume": 1}]}])
    assert p.poll() == []
    assert p.rejections().get("missing_time") == 1
    assert p.counters["rejected"] == 1


# ---------------------------------------------------------------------- #
# 报价：没有事件时间，必须显式标 unknown
# ---------------------------------------------------------------------- #
def test_quote_snapshot_has_no_event_time():
    p = make_provider([{"usAAPL": {"last": 201.2, "bid": 201.1, "ask": 201.3,
                                   "volume": 1000, "open": 200.0, "high": 202.0,
                                   "low": 199.5, "close": 201.0}}])
    snap = p.snapshot_quote("AAPL")
    assert snap.event_time is None
    assert snap.time_semantics == "unknown"
    assert snap.last == 201.2 and snap.bid == 201.1 and snap.ask == 201.3
    assert snap.receive_time.tzinfo is not None, "接收时间要有，但不能当事件时间"
    assert "event_time" in snap.as_dict() and snap.as_dict()["event_time"] is None


def test_quote_without_ohlc_does_not_crash():
    p = make_provider([{"usAAPL": {"last": 201.2, "bid": 201.1, "ask": 201.3,
                                   "volume": 10}}])
    snap = p.snapshot_quote("AAPL")
    assert snap.open == 0.0 and snap.high == 0.0


def test_orderbook_marks_missing_timestamp():
    p = make_provider([{"bid": [[201.1, 10]], "ask": [[201.3, 20]]}])
    ob = p.snapshot_orderbook("AAPL")
    assert ob.time_semantics == "missing"
    assert ob.event_time is None
    assert ob.bids == [[201.1, 10.0]] and ob.asks == [[201.3, 20.0]]


def test_orderbook_drops_invalid_levels():
    p = make_provider([{"bid": [[201.1, 10], [-1, 5], ["x", 1], [201.0, -2]],
                        "ask": [[201.3, 20]]}])
    ob = p.snapshot_orderbook("AAPL")
    assert ob.bids == [[201.1, 10.0]], "负价/非数字/负量的档位必须被剔除"


# ---------------------------------------------------------------------- #
# 价格与成交量
# ---------------------------------------------------------------------- #
@pytest.mark.parametrize("field,value,reason", [
    ("price", 0, "bad_price"),
    ("price", -1, "bad_price"),
    ("price", None, "bad_price"),
    ("price", "abc", "bad_price"),
    ("price", float("nan"), "bad_price"),
    ("volume", -1, "bad_volume"),
    ("volume", None, "bad_volume"),
    ("volume", "x", "bad_volume"),
])
def test_invalid_price_or_volume_rejected(field, value, reason):
    p = make_provider([{"ticks": [tick(**{field: value})]}])
    assert p.poll() == []
    assert p.rejections().get(reason) == 1


def test_zero_volume_is_allowed():
    p = make_provider([{"ticks": [tick(volume=0)]}])
    evs = p.poll()
    assert len(evs) == 1 and evs[0].size == 0.0


# ---------------------------------------------------------------------- #
# 映射
# ---------------------------------------------------------------------- #
@pytest.mark.parametrize("raw,expected", [
    ("B", "B"), ("S", "A"), ("b", "B"), ("sell", "A"), ("N", "N"),
    ("", ""), (None, ""), ("?", ""),
])
def test_side_mapping(raw, expected):
    assert F.map_side(raw) == expected


def test_symbol_and_exchange_are_normalized():
    p = make_provider([{"ticks": [tick()]}])
    ev = p.poll()[0]
    assert ev.symbol == "AAPL"
    assert ev.exchange == "fosun", "券商聚合行情必须用合成交易所，不得映射到 nasdaq"


def test_wire_code_prefix():
    assert F.wire_code("AAPL") == "usAAPL"
    assert F.wire_code("usAAPL") == "usAAPL"
    with pytest.raises(F.FosunDataError):
        F.wire_code("")


def test_tick_id_is_not_treated_as_protocol_sequence():
    p = make_provider([{"ticks": [tick(id=42)]}])
    ev = p.poll()[0]
    assert ev.sequence == 0, "tick id 不是协议序号，不能冒充 sequence"
    assert ev.source_event_id == "42"


# ---------------------------------------------------------------------- #
# 能力声明
# ---------------------------------------------------------------------- #
def test_capabilities_are_conservative():
    caps = F.FosunProvider().capabilities()
    assert caps.halt_events is False and caps.halt_events_native is False
    assert caps.luld_events is False and caps.mwcb_events is False
    assert caps.quotes is False, "quote 无事件时间，不能声明可用报价"
    assert caps.order_book is False
    assert caps.trades is True
    assert caps.exchange_field is False and caps.sequence_field is False
    assert caps.exchanges == ("fosun",)
    assert caps.realtime is False, "未实测前不得声明实时"


# ---------------------------------------------------------------------- #
# 重复 / 乱序 / 缺口
# ---------------------------------------------------------------------- #
def test_duplicate_tick_is_flagged():
    p = make_provider([{"ticks": [tick(id=10)]}, {"ticks": [tick(id=10)]}])
    p.poll()
    evs = p.poll()
    assert len(evs) == 1
    assert QualityFlag.DUPLICATE in evs[0].quality.flags
    assert evs[0].quality.usable is False
    assert p.counters["duplicates"] == 1


def test_out_of_order_tick_is_flagged():
    p = make_provider([{"ticks": [tick(id=100)]}, {"ticks": [tick(id=50)]}])
    p.poll()
    evs = p.poll()
    assert QualityFlag.OUT_OF_ORDER in evs[0].quality.flags
    assert evs[0].quality.usable is False


def test_overlapping_window_is_treated_as_duplicate_not_error():
    """轮询必然返回重叠窗口，不能被当成异常。"""
    p = make_provider([{"ticks": [tick(id=1), tick(id=2)]},
                       {"ticks": [tick(id=2), tick(id=3)]}])
    first = p.poll()
    second = p.poll()
    assert len(first) == 2
    assert len(second) == 2
    assert QualityFlag.DUPLICATE in second[0].quality.flags   # id=2 重复
    assert QualityFlag.OK in second[1].quality.flags or not second[1].quality.flags


def test_gap_flagged_only_in_strict_mode():
    p = make_provider([{"ticks": [tick(id=10)]}, {"ticks": [tick(id=500)]}])
    p.poll()
    evs = p.poll()
    assert QualityFlag.GAP not in evs[0].quality.flags

    q = make_provider([{"ticks": [tick(id=10)]}, {"ticks": [tick(id=500)]}],
                      strict_gap_check=True)
    q.poll()
    evs = q.poll()
    assert QualityFlag.GAP in evs[0].quality.flags
    assert q.counters["gaps"] == 1


# ---------------------------------------------------------------------- #
# 过期 / 断线 / 权限
# ---------------------------------------------------------------------- #
def test_stale_event_is_flagged():
    p = make_provider([{"ticks": [tick()]}], max_age_seconds=1)
    evs = p.poll()
    assert QualityFlag.STALE in evs[0].quality.flags
    assert evs[0].quality.usable is False


def test_disconnect_raises_and_recovers():
    p = make_provider([ProviderUnavailable("boom"), {"ticks": [tick()]}])
    with pytest.raises(ProviderUnavailable):
        p.poll()
    evs = p.poll()
    assert len(evs) == 1, "恢复后应能继续"


def test_permission_denied_stops_polling():
    p = make_provider([FosunPermissionError("403")])
    with pytest.raises(ProviderUnavailable):
        p.poll()
    assert p.permission_denied is True
    # 再次 poll 不应再发请求（fail-closed，避免无意义重试）
    calls_before = len(p._transport.calls)
    with pytest.raises(ProviderUnavailable):
        p.poll()
    assert len(p._transport.calls) == calls_before


def test_protocol_error_becomes_unavailable():
    p = make_provider([FosunProtocolError("报文解密失败：InvalidTag")])
    with pytest.raises(ProviderUnavailable):
        p.poll()
    assert "解密" in p.health.last_error


def test_malformed_ticks_payload():
    p = make_provider([{"nope": []}])
    with pytest.raises(ProviderUnavailable):
        p.poll()


# ---------------------------------------------------------------------- #
# 不应产出的事件
# ---------------------------------------------------------------------- #
def test_provider_never_emits_halt_events():
    from ai_trader.market.providers.models import HaltEvent
    p = make_provider([{"ticks": [tick()]}])
    assert not any(isinstance(e, HaltEvent) for e in p.poll())
    assert not hasattr(p, "halt_symbol")


# ---------------------------------------------------------------------- #
# 端点防误连
# ---------------------------------------------------------------------- #
def _pem_pair():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    priv = ec.generate_private_key(ec.SECP384R1())
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()).decode()
    pub_pem = priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return priv_pem, pub_pem


def test_sit_endpoint_is_rejected(monkeypatch):
    priv, pub = _pem_pair()
    monkeypatch.setenv("FSOPENAPI_BASE_URL", "https://openapi-sit.example.com")
    monkeypatch.setenv("FSOPENAPI_API_KEY", "dummy")
    monkeypatch.setenv("FSOPENAPI_CLIENT_PRIVATE_KEY", priv)
    monkeypatch.setenv("FSOPENAPI_SERVER_PUBLIC_KEY", pub)
    p = F.FosunProvider({})
    with pytest.raises(Exception) as ei:
        p._build_transport_from_env()
    assert "测试环境" in str(ei.value)


def test_sit_can_be_allowed_explicitly(monkeypatch):
    priv, pub = _pem_pair()
    monkeypatch.setenv("FSOPENAPI_BASE_URL", "https://openapi-sit.example.com")
    monkeypatch.setenv("FSOPENAPI_API_KEY", "dummy")
    monkeypatch.setenv("FSOPENAPI_CLIENT_PRIVATE_KEY", priv)
    monkeypatch.setenv("FSOPENAPI_SERVER_PUBLIC_KEY", pub)
    tr = F.FosunProvider({"allow_test_endpoint": True})._build_transport_from_env()
    assert tr is not None
