"""纯函数技术指标（无第三方依赖，便于单元测试）。"""
from __future__ import annotations

from typing import List, Optional, Sequence

from ..broker.base import Candle


def sma(values: Sequence[float], period: int) -> Optional[float]:
    if period <= 0 or len(values) < period:
        return None
    return sum(values[-period:]) / period


def ema(values: Sequence[float], period: int) -> Optional[float]:
    if period <= 0 or len(values) < period:
        return None
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def rsi(closes: Sequence[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(-period, 0):
        delta = closes[i] - closes[i - 1]
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    if losses == 0:
        return 100.0
    rs = (gains / period) / (losses / period)
    return 100.0 - (100.0 / (1.0 + rs))


def atr(candles: Sequence[Candle], period: int = 14) -> Optional[float]:
    if len(candles) < period + 1:
        return None
    trs: List[float] = []
    for i in range(-period, 0):
        c, prev = candles[i], candles[i - 1]
        tr = max(c.high - c.low, abs(c.high - prev.close), abs(c.low - prev.close))
        trs.append(tr)
    return sum(trs) / period


def atr_pct(candles: Sequence[Candle], period: int = 14) -> Optional[float]:
    a = atr(candles, period)
    if a is None or not candles:
        return None
    last = candles[-1].close
    return a / last if last else None


def bollinger(closes: Sequence[float], period: int = 20, mult: float = 2.0):
    if len(closes) < period:
        return None, None, None
    window = closes[-period:]
    mid = sum(window) / period
    var = sum((x - mid) ** 2 for x in window) / period
    sd = var ** 0.5
    return mid - mult * sd, mid, mid + mult * sd


def max_drawdown(closes: Sequence[float]) -> float:
    peak, mdd = float("-inf"), 0.0
    for c in closes:
        peak = max(peak, c)
        if peak > 0:
            mdd = max(mdd, (peak - c) / peak)
    return mdd


def momentum(closes: Sequence[float], lookback: int = 20) -> Optional[float]:
    if len(closes) <= lookback:
        return None
    base = closes[-lookback - 1]
    return (closes[-1] - base) / base if base else None


def compute_snapshot(candles: Sequence[Candle]) -> dict:
    """一次性算出策略/AI 需要的全部指标。"""
    closes = [c.close for c in candles]
    return {
        "bars": len(candles),
        "last_close": closes[-1] if closes else None,
        "sma20": sma(closes, 20),
        "sma60": sma(closes, 60),
        "ema12": ema(closes, 12),
        "ema26": ema(closes, 26),
        "rsi14": rsi(closes, 14),
        "atr14": atr(candles, 14),
        "atr_pct": atr_pct(candles, 14),
        "mom20": momentum(closes, 20),
        "mdd60": max_drawdown(closes[-60:]) if len(closes) >= 2 else 0.0,
        "boll": bollinger(closes, 20, 2.0),
    }


def _r(v, nd: int = 4):
    return round(v, nd) if isinstance(v, (int, float)) else v


def snapshot_for_prompt(candles: Sequence[Candle], tail: int = 12) -> dict:
    """给 AI 的紧凑结构：指标 + 最近 K 线尾巴。"""
    snap = compute_snapshot(candles)
    lo, mid, hi = snap.get("boll") or (None, None, None)
    snap["boll_lower"], snap["boll_mid"], snap["boll_upper"] = _r(lo), _r(mid), _r(hi)
    snap.pop("boll", None)
    snap = {k: _r(v) for k, v in snap.items()}
    snap["recent"] = [
        {"t": c.ts, "o": _r(c.open), "h": _r(c.high), "l": _r(c.low),
         "c": _r(c.close), "v": _r(c.volume, 0)}
        for c in candles[-tail:]
    ]
    return snap
