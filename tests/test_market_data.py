"""测试 3：行情数据结构与计算正确性。"""
from __future__ import annotations

from datetime import date

import pytest

from ai_trader.broker.base import Candle, Quote
from ai_trader.broker.paper import SyntheticQuoteSource, currency_of, market_of
from ai_trader.strategy.indicators import (
    atr,
    atr_pct,
    bollinger,
    compute_snapshot,
    ema,
    momentum,
    rsi,
    sma,
    snapshot_for_prompt,
)
from tests.conftest import make_candles


# ---------------------------------------------------------------- 标的解析
@pytest.mark.parametrize("symbol,market,currency", [
    ("hk00700", "hk", "HKD"),
    ("usAAPL", "us", "USD"),
    ("sh600519", "cn", "CNY"),
    ("sz000001", "cn", "CNY"),
    ("cn600519", "cn", "CNY"),
])
def test_symbol_market_currency_mapping(symbol, market, currency):
    assert market_of(symbol) == market
    assert currency_of(symbol) == currency


# ---------------------------------------------------------------- 结构
def test_quote_dataclass_shape(paper):
    q = paper.get_quote("hk00700")
    assert isinstance(q, Quote)
    assert q.symbol == "hk00700"
    assert q.market == "hk"
    assert q.last > 0
    d = q.as_dict()
    for k in ("symbol", "market", "last", "open", "high", "low", "prev_close", "volume", "source"):
        assert k in d
    assert d["source"] == "synthetic"     # 诚实标注：非真实行情


def test_kline_structure_and_ordering(paper):
    candles = paper.get_klines("hk00700", "day", 60)
    assert len(candles) == 60
    assert all(isinstance(c, Candle) for c in candles)
    for c in candles:
        assert c.low <= c.open <= c.high
        assert c.low <= c.close <= c.high
        assert c.volume >= 0
    # 时间戳单调递增
    ts = [c.ts for c in candles]
    assert ts == sorted(ts)


def test_synthetic_source_is_deterministic():
    a = SyntheticQuoteSource().quote("usAAPL")
    b = SyntheticQuoteSource().quote("usAAPL")
    assert a.last == b.last
    assert a.open == b.open
    c = SyntheticQuoteSource().quote("usNVDA")
    assert c.last != a.last


def test_multi_symbol_quotes(paper):
    qs = paper.get_quotes(["hk00700", "usAAPL", "sh600519"])
    assert set(qs) == {"hk00700", "usAAPL", "sh600519"}
    assert all(v.last > 0 for v in qs.values())


# ---------------------------------------------------------------- 指标
def test_sma_and_ema():
    vals = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert sma(vals, 3) == pytest.approx(9.0)
    assert sma(vals, 20) is None
    assert ema(vals, 3) is not None


def test_rsi_bounds():
    up = [100 + i for i in range(40)]
    down = [100 - i * 0.5 for i in range(40)]
    assert rsi(up) == pytest.approx(100.0)
    assert rsi(down) < 5
    assert rsi([1, 2, 3]) is None


def test_atr_positive_and_pct():
    candles = make_candles([100 + i for i in range(30)])
    a = atr(candles, 14)
    assert a is not None and a > 0
    p = atr_pct(candles, 14)
    assert p is not None and 0 < p < 1


def test_bollinger_ordering():
    closes = [100 + (i % 7) for i in range(40)]
    lo, mid, hi = bollinger(closes, 20, 2.0)
    assert lo < mid < hi


def test_momentum_sign():
    assert momentum([100 + i for i in range(30)], 20) > 0
    assert momentum([100 - i for i in range(30)], 20) < 0


def test_snapshot_contains_all_fields(paper):
    candles = paper.get_klines("hk00700", "day", 120)
    snap = compute_snapshot(candles)
    for key in ("bars", "last_close", "sma20", "sma60", "rsi14", "atr14",
                "atr_pct", "mom20", "mdd60"):
        assert key in snap
    prompt_snap = snapshot_for_prompt(candles, tail=5)
    assert len(prompt_snap["recent"]) == 5
    assert {"t", "o", "h", "l", "c", "v"} <= set(prompt_snap["recent"][0])
