"""把真实历史日线包装成 ``QuoteSource``，直接插进现有 ``PaperBroker``。

为什么要这一层
--------------
``PaperBroker`` 只依赖 ``QuoteSource`` 协议（``quote`` / ``klines``），
见 ``broker/paper.py:57-70``。因此**不需要改一行 PaperBroker 代码**，
就能把默认的 ``SyntheticQuoteSource``（随机游走）换成真实历史数据。

防未来函数
----------
``as_of`` 是硬上界：``klines`` 只返回 ``trade_date <= as_of`` 的 bar，
``quote`` 只取该上界内最后一根。回放 2026-11-06 的决策时，读不到 11-09 的行情。

诚实标注
--------
``Quote.source`` 会带出真实来源（例如 ``hist:yahoo:chart:1d``）与最后 bar 日期，
一路传到 ``engine._build_context`` 的 ``notes`` 与日志里 —— 任何一条模拟成交都能
追溯到"用的是哪天的真实收盘价"。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from ..broker.base import BrokerError, Candle, Quote
from ..broker.paper import QuoteSource, currency_of, market_of
from .bars import BarStore

_NY = ZoneInfo("America/New_York")


class NoMarketData(BrokerError):
    """该标的在 ``as_of`` 之前没有任何可用 bar（fail-closed，不猜价格）。"""

    def __init__(self, message: str, code: str = "NO_MARKET_DATA"):
        super().__init__(message)
        self.code = code


class HistoricalQuoteSource(QuoteSource):
    """真实历史日线行情源。

    Parameters
    ----------
    store:
        已写入真实 bar 的 :class:`BarStore`。
    as_of:
        决策/撮合的"当日"。所有读取都被截断到这一天（含）。
    source_label:
        写进 ``Quote.source`` 的人类可读标签，默认 ``hist:yahoo:chart:1d``。
    """

    source_name = "historical"

    def __init__(self, store: BarStore, as_of: Optional[date | str] = None,
                 source_label: str = "hist:yahoo:chart:1d"):
        self.store = store
        if as_of is not None and not isinstance(as_of, date):
            self.as_of = date.fromisoformat(str(as_of)[:10])
        else:
            self.as_of = as_of  # type: ignore[assignment]
        self.source_label = source_label
        self.calls = 0
        self.last_symbols: List[str] = []

    # ------------------------------------------------------------------ #
    def _as_of_date(self, override: Optional[date | str] = None) -> Optional[date]:
        if override is None:
            return self.as_of
        if isinstance(override, date) and not isinstance(override, datetime):
            return override
        return date.fromisoformat(str(override)[:10])

    def klines(self, symbol: str, ktype: str = "day", num: int = 120,
               as_of: Optional[date | str] = None) -> List[Candle]:
        if str(ktype).lower() not in ("day", "d", "1d"):
            raise NoMarketData(f"历史源只提供日线，收到 ktype={ktype!r}", code="KTYPE_UNSUPPORTED")
        self.calls += 1
        bars = self.store.candles(symbol, self._as_of_date(as_of), num)
        if not bars:
            raise NoMarketData(
                f"{symbol} 在 {self._as_of_date(as_of) or '最新'} 之前没有历史 bar"
                f"（存储 {self.store.path}）")
        return bars

    def quote(self, symbol: str, as_of: Optional[date | str] = None) -> Quote:
        candles = self.klines(symbol, "day", 2, as_of=as_of)
        last = candles[-1]
        prev_close = candles[-2].close if len(candles) > 1 else last.close
        d = date.fromisoformat(str(last.ts)[:10])
        # 收盘时刻用 NY 时区表示，带正确 DST 偏移
        ts = datetime(d.year, d.month, d.day, 16, 0, tzinfo=_NY).isoformat()
        return Quote(
            symbol=symbol,
            market=market_of(symbol),
            last=float(last.close),
            open=float(last.open),
            high=float(last.high),
            low=float(last.low),
            prev_close=float(prev_close),
            volume=float(last.volume or 0),
            ts=ts,
            source=f"{self.source_label}@{last.ts}",
        )

    # ------------------------------------------------------------------ #
    def evidence(self, symbols: List[str]) -> Dict[str, Any]:
        """给审计用的只读快照：每个标的最后被使用的 bar。"""
        out: Dict[str, Any] = {"as_of": self.as_of.isoformat() if self.as_of else None,
                               "source": self.source_label, "symbols": {}}
        for s in symbols:
            bars = self.store.load(s, self.as_of, 1)
            if not bars:
                out["symbols"][s] = {"bars_available": False}
                continue
            b = bars[-1]
            out["symbols"][s] = {
                "last_bar": b["trade_date"], "close": b["close"],
                "source": b["source"], "digest": b["digest"],
                "bars_in_store": self.store.coverage(s)["bars"],
            }
        return out
