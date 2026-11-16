"""美股日线存储与质量校验（US 闭环专项，独立于 US-P1 安全修复）。

设计原则
--------
1. **只存真实数据**：本模块不接受"随机游走"或任何合成序列。写入方必须提供
   ``source`` 字段（例如 ``yahoo:chart:1d``），空值直接拒绝。
2. **无未来函数**：读取接口一律带 ``as_of`` 上界，只返回 ``trade_date <= as_of``
   的 bar。回放某一天时，不可能读到该日之后的数据。
3. **可审计**：每次写入都记录 ``fetched_at`` 与每行 ``digest``，便于事后核对
   同一根 bar 是否被改写。
4. **失败可见**：不合法行不静默丢弃，返回 ``IngestResult`` 明细（拒绝原因计数）。

价格口径
--------
存储的是**未复权**原始 OHLCV（与券商当日成交价一致）。拆股/分红不做调整，
由 :func:`detect_split_like_jumps` 显式标记，避免"看起来正常"的错误信号。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..broker.base import Candle

_SCHEMA = """
CREATE TABLE IF NOT EXISTS us_bars (
    symbol      TEXT NOT NULL,
    trade_date  TEXT NOT NULL,          -- YYYY-MM-DD，America/New_York 交易日
    open        REAL NOT NULL,
    high        REAL NOT NULL,
    low         REAL NOT NULL,
    close       REAL NOT NULL,
    volume      REAL NOT NULL,
    source      TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    digest      TEXT NOT NULL,
    PRIMARY KEY (symbol, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_us_bars_symbol_date ON us_bars(symbol, trade_date);

CREATE TABLE IF NOT EXISTS us_bar_ingest_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    source      TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    rows_in     INTEGER NOT NULL,
    rows_written INTEGER NOT NULL,
    rows_rejected INTEGER NOT NULL,
    detail      TEXT
);
"""

#: 价格合理性上界（一分钱到 1e6 美元）。超出即判为脏数据，防"0 价成交"。
_MIN_PRICE = 1e-4
_MAX_PRICE = 1e6
#: 单日涨跌幅绝对值超过该阈值 → 标记为疑似拆股（未复权序列的正常表现）
_SPLIT_LIKE_RET = 0.35


class BarDataError(RuntimeError):
    """行情存储不可用/数据不合法。"""


@dataclass
class IngestResult:
    symbol: str
    source: str
    rows_in: int = 0
    rows_written: int = 0
    rows_unchanged: int = 0
    rejected: Dict[str, int] = field(default_factory=dict)
    duplicates_dropped: int = 0
    first_date: str = ""
    last_date: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "source": self.source,
            "rows_in": self.rows_in,
            "rows_written": self.rows_written,
            "rows_unchanged": self.rows_unchanged,
            "rows_rejected": int(sum(self.rejected.values())),
            "rejected_reasons": dict(sorted(self.rejected.items())),
            "duplicates_dropped": self.duplicates_dropped,
            "first_date": self.first_date,
            "last_date": self.last_date,
        }


def normalize_symbol(symbol: Any) -> str:
    """标的规范化：去空白 + 小写。

    ``strategy/engine.py:1187`` 会把标的转小写后再使用，存储侧若保留大小写，
    查询就会静默取不到数据（实测：``usAAPL`` 写入、``usaapl`` 读取 → NoMarketData）。
    """
    return str(symbol or "").strip().lower()


def _digest(symbol: str, d: str, o: float, h: float, l: float, c: float, v: float) -> str:
    raw = f"{symbol}|{d}|{o:.6f}|{h:.6f}|{l:.6f}|{c:.6f}|{v:.4f}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _as_date(value: Any) -> Optional[date]:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        s = value.strip()
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
            try:
                return datetime.strptime(s, fmt).date()
            except ValueError:
                continue
    return None


def validate_bar(raw: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
    """校验单根 bar。返回 ``(clean, "")`` 或 ``(None, 拒绝原因)``。"""
    d = _as_date(raw.get("trade_date") or raw.get("date") or raw.get("ts"))
    if d is None:
        return None, "bad_date"
    try:
        o = float(raw["open"]); h = float(raw["high"])
        l = float(raw["low"]); c = float(raw["close"])
        v = float(raw.get("volume") or 0.0)
    except (KeyError, TypeError, ValueError):
        return None, "bad_number"
    for name, val in (("open", o), ("high", h), ("low", l), ("close", c)):
        if not (val == val) or val in (float("inf"), float("-inf")):
            return None, f"nan_or_inf_{name}"
        if val < _MIN_PRICE or val > _MAX_PRICE:
            return None, f"price_out_of_range_{name}"
    if v != v or v < 0:
        return None, "bad_volume"
    if h < max(o, c) - 1e-9:
        return None, "high_below_body"
    if l > min(o, c) + 1e-9:
        return None, "low_above_body"
    if h < l:
        return None, "high_below_low"
    return {
        "trade_date": d.isoformat(), "open": o, "high": h, "low": l,
        "close": c, "volume": v,
    }, ""


class BarStore:
    """美股日线的 SQLite 存储。线程安全（单连接 + RLock）。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:
                pass
            self._conn.commit()

    # ------------------------------------------------------------------ #
    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    def ingest(self, symbol: str, rows: Iterable[Dict[str, Any]], source: str,
               fetched_at: Optional[str] = None) -> IngestResult:
        """写入一批 bar（幂等：同一 (symbol,date) 同内容不重写，不同内容覆盖并留痕）。"""
        sym = normalize_symbol(symbol)
        if not sym:
            raise BarDataError("symbol 不能为空")
        if not str(source or "").strip():
            # 真实数据必须有可追溯来源；否则无法区分真实与合成
            raise BarDataError("必须提供 source（禁止写入无来源的行情）")
        now = fetched_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
        res = IngestResult(symbol=sym, source=str(source))
        seen: Dict[str, Dict[str, Any]] = {}
        for raw in rows:
            res.rows_in += 1
            clean, why = validate_bar(raw)
            if clean is None:
                res.rejected[why] = res.rejected.get(why, 0) + 1
                continue
            key = clean["trade_date"]
            if key in seen:
                res.duplicates_dropped += 1
            seen[key] = clean          # 同批重复：保留最后一条
        if not seen:
            self._log_ingest(res, now)
            return res

        dates = sorted(seen)
        res.first_date, res.last_date = dates[0], dates[-1]
        with self._lock:
            cur = self._conn.cursor()
            for d in dates:
                b = seen[d]
                dig = _digest(sym, d, b["open"], b["high"], b["low"], b["close"], b["volume"])
                old = cur.execute(
                    "SELECT digest FROM us_bars WHERE symbol=? AND trade_date=?",
                    (sym, d)).fetchone()
                if old is not None and old["digest"] == dig:
                    res.rows_unchanged += 1
                    continue
                cur.execute(
                    "INSERT INTO us_bars(symbol,trade_date,open,high,low,close,volume,"
                    "source,fetched_at,digest) VALUES(?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(symbol,trade_date) DO UPDATE SET "
                    "open=excluded.open, high=excluded.high, low=excluded.low, "
                    "close=excluded.close, volume=excluded.volume, "
                    "source=excluded.source, fetched_at=excluded.fetched_at, "
                    "digest=excluded.digest",
                    (sym, d, b["open"], b["high"], b["low"], b["close"],
                     b["volume"], str(source), now, dig))
                res.rows_written += 1
            self._conn.commit()
        self._log_ingest(res, now)
        return res

    def _log_ingest(self, res: IngestResult, fetched_at: str) -> None:
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO us_bar_ingest_log(symbol,source,fetched_at,rows_in,"
                    "rows_written,rows_rejected,detail) VALUES(?,?,?,?,?,?,?)",
                    (res.symbol, res.source, fetched_at, res.rows_in,
                     res.rows_written, int(sum(res.rejected.values())),
                     json.dumps(res.as_dict(), ensure_ascii=False)))
                self._conn.commit()
        except sqlite3.Error:
            pass        # 日志失败不影响数据本体

    # ------------------------------------------------------------------ #
    def load(self, symbol: str, as_of: Optional[date | str] = None,
             num: int = 120) -> List[Dict[str, Any]]:
        """读取 ``trade_date <= as_of`` 的最近 ``num`` 根，按日期升序。"""
        sym = normalize_symbol(symbol)
        sql = ("SELECT symbol,trade_date,open,high,low,close,volume,source,fetched_at,digest "
               "FROM us_bars WHERE symbol=?")
        args: List[Any] = [sym]
        if as_of is not None:
            d = _as_date(as_of)
            if d is None:
                raise BarDataError(f"as_of 非法: {as_of!r}")
            sql += " AND trade_date<=?"
            args.append(d.isoformat())
        sql += " ORDER BY trade_date DESC LIMIT ?"
        args.append(int(max(1, num)))
        with self._lock:
            rows = [dict(r) for r in self._conn.execute(sql, args).fetchall()]
        rows.reverse()
        return rows

    def candles(self, symbol: str, as_of: Optional[date | str] = None,
                num: int = 120) -> List[Candle]:
        out: List[Candle] = []
        for r in self.load(symbol, as_of, num):
            out.append(Candle(ts=r["trade_date"], open=r["open"], high=r["high"],
                              low=r["low"], close=r["close"], volume=r["volume"]))
        return out

    def coverage(self, symbol: str) -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) n, MIN(trade_date) f, MAX(trade_date) l, "
                "COUNT(DISTINCT source) ns, MIN(source) s "
                "FROM us_bars WHERE symbol=?", (normalize_symbol(symbol),)).fetchone()
        if not row or not row["n"]:
            return {"symbol": symbol, "bars": 0, "first": "", "last": "",
                    "sources": [], "weekday_gaps": 0, "gap_examples": []}
        gaps = self._weekday_gaps(symbol, row["f"], row["l"])
        return {
            "symbol": symbol, "bars": int(row["n"]),
            "first": row["f"], "last": row["l"],
            "sources": [r["source"] for r in self._sources(symbol)],
            "weekday_gaps": len(gaps), "gap_examples": gaps[:10],
        }

    def _sources(self, symbol: str) -> List[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT DISTINCT source FROM us_bars WHERE symbol=? ORDER BY source",
                (normalize_symbol(symbol),)).fetchall()

    def _weekday_gaps(self, symbol: str, first: str, last: str) -> List[str]:
        """工作日缺口（可能是节假日，也可能真的缺数据 —— 不自动判错，只报告）。"""
        with self._lock:
            have = {r["trade_date"] for r in self._conn.execute(
                "SELECT trade_date FROM us_bars WHERE symbol=? AND trade_date BETWEEN ? AND ?",
                (normalize_symbol(symbol), first, last))}
        d0, d1 = date.fromisoformat(first), date.fromisoformat(last)
        out: List[str] = []
        cur = d0
        while cur <= d1:
            if cur.weekday() < 5 and cur.isoformat() not in have:
                out.append(cur.isoformat())
            cur += timedelta(days=1)
        return out

    # ------------------------------------------------------------------ #
    def detect_split_like_jumps(self, symbol: str, as_of: Optional[date | str] = None,
                                num: int = 120, threshold: float = _SPLIT_LIKE_RET,
                                ) -> List[Dict[str, Any]]:
        """标记未复权序列里的疑似拆股/异常跳空（|日收益| >= threshold）。"""
        bars = self.load(symbol, as_of, num)
        out: List[Dict[str, Any]] = []
        for prev, cur in zip(bars, bars[1:]):
            pc = float(prev["close"])
            if pc <= 0:
                continue
            ret = float(cur["close"]) / pc - 1.0
            if abs(ret) >= threshold:
                out.append({"date": cur["trade_date"], "ret": round(ret, 6),
                            "prev_close": pc, "close": cur["close"],
                            "ratio": round(pc / float(cur["close"]), 4)})
        return out

    def quality_report(self, symbols: Sequence[str]) -> Dict[str, Any]:
        return {
            "db": str(self.path),
            "symbols": {s: self.coverage(s) for s in symbols},
            "split_like_jumps": {s: self.detect_split_like_jumps(s) for s in symbols},
        }
