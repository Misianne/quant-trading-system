"""真实美股日线抓取（Yahoo Finance chart 公开端点，无需密钥）。

约束
----
* 仅访问公开行情端点，不涉及任何券商生产接口；
* 抓到的数据原样交给 :class:`~ai_trader.us_loop.bars.BarStore` 校验，
  校验不过的行**不写入**，并在 ``IngestResult.rejected`` 中计数；
* 抓取失败不降级、不伪造：直接抛 :class:`FetchError`，由调用方报告缺口。

时间口径
--------
Yahoo 返回的 ``timestamp`` 是该交易日 **常规时段开盘** 的 epoch 秒。这里把它
换算到 ``America/New_York`` 的日历日，作为 ``trade_date`` —— 与券商"交易日"
口径一致，避免 UTC 换算把周五的 bar 记到周六。
"""
from __future__ import annotations

import csv
import io
import json
import time
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from .bars import BarStore, IngestResult

_NY = ZoneInfo("America/New_York")

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
SOURCE_TAG = "yahoo:chart:1d"

_UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "application/json,text/plain,*/*",
}


class FetchError(RuntimeError):
    def __init__(self, message: str, code: str = "FETCH_FAILED", detail: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.detail = detail or {}


def to_yahoo_symbol(symbol: str) -> str:
    """``usAAPL`` -> ``AAPL``；已是纯代码则原样返回。"""
    s = str(symbol or "").strip()
    low = s.lower()
    if low.startswith("us") and len(s) > 2:
        return s[2:].upper()
    return s.upper()


def _rows_from_chart(payload: Dict[str, Any], symbol: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    chart = (payload or {}).get("chart") or {}
    err = chart.get("error")
    if err:
        raise FetchError(f"Yahoo 返回错误: {err}", code="UPSTREAM_ERROR", detail={"err": err})
    results = chart.get("result") or []
    if not results:
        raise FetchError("Yahoo 响应缺少 result", code="BAD_RESPONSE")
    res = results[0]
    meta = res.get("meta") or {}
    ts = res.get("timestamp") or []
    quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    opens, highs = quote.get("open") or [], quote.get("high") or []
    lows, closes = quote.get("low") or [], quote.get("close") or []
    vols = quote.get("volume") or []

    rows: List[Dict[str, Any]] = []
    for i, t in enumerate(ts):
        try:
            o = opens[i]; h = highs[i]; l = lows[i]; c = closes[i]
            v = vols[i] if i < len(vols) else 0
        except IndexError:
            continue
        if o is None or h is None or l is None or c is None:
            continue           # Yahoo 对停牌/无成交日会给 null，跳过交给校验统计
        day = datetime.fromtimestamp(int(t), tz=_NY).date().isoformat()
        rows.append({"trade_date": day, "open": o, "high": h, "low": l,
                     "close": c, "volume": 0 if v is None else v})
    return rows, {
        "symbol": symbol,
        "yahoo_symbol": meta.get("symbol"),
        "currency": meta.get("currency"),
        "exchange": meta.get("fullExchangeName") or meta.get("exchangeName"),
        "instrument": meta.get("instrumentType"),
        "tz": meta.get("exchangeTimezoneName"),
        "bars": len(rows),
        "first": rows[0]["trade_date"] if rows else "",
        "last": rows[-1]["trade_date"] if rows else "",
    }


def fetch_chart(yahoo_symbol: str, range_: str = "2y", interval: str = "1d",
                timeout: float = 20.0, retries: int = 2) -> Dict[str, Any]:
    """GET 公开 chart 端点；失败重试后抛 :class:`FetchError`。"""
    import requests

    url = CHART_URL.format(sym=yahoo_symbol)
    params = {"range": range_, "interval": interval, "includePrePost": "false",
              "events": "div,splits"}
    last: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, params=params, headers=_UA,
                                timeout=(8.0, timeout))
            if resp.status_code == 429:
                raise FetchError("上游限流 HTTP 429", code="RATE_LIMITED")
            if resp.status_code >= 400:
                raise FetchError(f"HTTP {resp.status_code}", code=f"HTTP_{resp.status_code}")
            return resp.json()
        except FetchError as exc:
            last = exc
            if exc.code == "RATE_LIMITED" or str(exc.code).startswith("HTTP_5"):
                time.sleep(1.2 * (attempt + 1))
                continue
            raise
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.8 * (attempt + 1))
    raise FetchError(f"抓取失败（已重试 {retries} 次）: {last}", code="NETWORK",
                     detail={"error": repr(last)})


def fetch_and_store(store: BarStore, symbol: str, range_: str = "2y",
                    interval: str = "1d") -> Dict[str, Any]:
    """抓一个标的并写入存储，返回抓取元数据 + 写入结果。"""
    ys = to_yahoo_symbol(symbol)
    payload = fetch_chart(ys, range_=range_, interval=interval)
    rows, meta = _rows_from_chart(payload, symbol)
    if not rows:
        raise FetchError(f"{symbol} 未取到任何 bar", code="EMPTY_DATA")
    fetched_at = datetime.now().astimezone().isoformat(timespec="seconds")
    res: IngestResult = store.ingest(symbol, rows, source=SOURCE_TAG, fetched_at=fetched_at)
    meta.update({"fetched_at": fetched_at, "source": SOURCE_TAG,
                 "range": range_, "interval": interval,
                 "ingest": res.as_dict()})
    return meta


def ingest_csv(store: BarStore, symbol: str, path: str, source: str,
               date_col: str = "Date") -> IngestResult:
    """离线导入 CSV（测试与灾备用；来源必须显式给出，禁止留空）。"""
    text = open(path, "r", encoding="utf-8-sig").read()
    reader = csv.DictReader(io.StringIO(text))
    rows: List[Dict[str, Any]] = []
    for r in reader:
        rows.append({
            "trade_date": r.get(date_col) or r.get("date"),
            "open": r.get("Open") or r.get("open"),
            "high": r.get("High") or r.get("high"),
            "low": r.get("Low") or r.get("low"),
            "close": r.get("Close") or r.get("close"),
            "volume": r.get("Volume") or r.get("volume") or 0,
        })
    return store.ingest(symbol, rows, source=source,
                        fetched_at=datetime.now().astimezone().isoformat(timespec="seconds"))


def load_chart_json(path: str) -> Dict[str, Any]:
    """从本地保存的 chart JSON 还原（离线测试用）。"""
    return json.loads(open(path, "r", encoding="utf-8").read())
