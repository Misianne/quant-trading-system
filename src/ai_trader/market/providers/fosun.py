"""复星财富行情适配器（离线骨架，独立实现，不依赖官方 SDK）。

三条不可让步的规则
------------------
1. **不伪造事件时间。** `quote` / `orderbook` / `min` 三个接口没有事件时间戳，
   它们**不会**被转成 `MarketEvent`。只提供带 `time_semantics` 标记的快照对象
   （`"unknown"` / `"missing"`），永远不填本地接收时间。
2. **交易所是合成的。** 券商聚合行情无法拆回单一交易所，因此 `exchange`
   恒为 ``"fosun"``，绝不允许填 `nasdaq`/`nyse`，也不允许加入
   `AuthorityRouter` 的覆盖列表。
3. **不提供任何停复牌事件。** `halt_events=False`。这不是"暂时没有"，而是
   "协议里就不存在"；上层因此**结构上**无法用它的行情去恢复交易。

能力声明里那一串 `False` 是安全闸门，不是保守措辞。
"""
from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from ...broker.base import Quote

from . import fosun_protocol as proto
from .base import MarketDataProvider, ProviderCapabilities, ProviderError, ProviderNotConfigured, ProviderUnavailable
from .models import (
    DataQuality,
    EventAuthority,
    QualityFlag,
    TradeEvent,
)
from .normalize import normalize_symbol

#: 交易所以合成标识出现。任何把它当成真实交易所的映射都是错的。
FOSUN_VENUE = "fosun"

#: 官方 quote 的价格是**放大整数**。实测：3527800 ↔ 352.78（÷1e4）。
#: 这个标度没有写在 SDK 文档里，是从真实响应反推并交叉验证得到的
#: （chgVal=-34900 ↔ -3.49，pClose=3562700 ↔ 356.27，三者自洽）。
QUOTE_PRICE_SCALE = 10_000.0
#: 涨跌幅同理：-98 ↔ -0.98%（÷100）。
QUOTE_PCT_SCALE = 100.0

#: 允许的时间戳年份区间（防御明显错误的毫秒/微秒混用）。
MIN_YEAR = 1990
MAX_YEAR = 2100


class FosunDataError(Exception):
    """单条数据不可用。``reason`` 用于分类计数。"""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"[{reason}] {detail}")
        self.reason = reason
        self.detail = detail


# ---------------------------------------------------------------------- #
# 时间与数值工具
# ---------------------------------------------------------------------- #
def ms_to_utc(value: Any) -> datetime:
    """毫秒 epoch → UTC ``datetime``。**整数运算**，不经过浮点。

    拒绝：非数字、非正数、超出合理年份、非有限值。
    缺失或非法一律抛 :class:`FosunDataError`，绝不返回"现在"。
    """
    if isinstance(value, bool) or value is None:
        raise FosunDataError("missing_time", f"时间戳缺失：{value!r}")
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise FosunDataError("missing_time", "时间戳为空串")
        if not text.lstrip("-").isdigit():
            raise FosunDataError("bad_time", f"时间戳不是整数：{value!r}")
        raw = int(text)
    elif isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise FosunDataError("bad_time", f"时间戳非有限：{value!r}")
        if isinstance(value, float) and not value.is_integer():
            raise FosunDataError("bad_time", f"毫秒时间戳应为整数：{value!r}")
        raw = int(value)
    else:
        raise FosunDataError("bad_time", f"时间戳类型不支持：{type(value).__name__}")

    if raw <= 0:
        raise FosunDataError("bad_time", f"时间戳非正：{raw}")
    # 用秒级范围做合理性检查（避免把微秒/纳秒当毫秒）
    seconds, millis = divmod(raw, 1000)
    try:
        dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise FosunDataError("bad_time", f"时间戳越界：{raw}") from None
    if not (MIN_YEAR <= dt.year <= MAX_YEAR):
        raise FosunDataError(
            "bad_time", f"时间戳年份不合理（{dt.year}），疑似单位不是毫秒")
    return dt.replace(microsecond=millis * 1000)


def optional_float(value: Any, default: float = 0.0) -> float:
    """可缺省字段（如 quote 的 open/high/low/close）。缺失给默认值，不报错。"""
    if value is None or isinstance(value, bool):
        return default
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    return num if math.isfinite(num) else default


def to_float(value: Any, *, field_name: str, allow_zero: bool = True) -> float:
    if isinstance(value, bool) or value is None:
        raise FosunDataError(f"bad_{field_name}", f"{field_name} 缺失")
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise FosunDataError(f"bad_{field_name}", f"{field_name} 非数字：{value!r}") from None
    if not math.isfinite(num):
        raise FosunDataError(f"bad_{field_name}", f"{field_name} 非有限：{value!r}")
    if num < 0 or (not allow_zero and num == 0):
        raise FosunDataError(f"bad_{field_name}", f"{field_name} 非法：{num}")
    return num


#: 官方 `side` 用 B/S；我们的模型沿用 A=卖方主动 / B=买方主动 / N=未知。
_SIDE_MAP = {"B": "B", "BUY": "B", "S": "A", "SELL": "A", "A": "A", "N": "N"}


def map_side(value: Any) -> str:
    return _SIDE_MAP.get(str(value or "").strip().upper(), "")


def wire_code(symbol: str, prefix: str = "us") -> str:
    """``AAPL`` → ``usAAPL``。已带前缀则原样返回。"""
    text = str(symbol or "").strip()
    if not text:
        raise FosunDataError("bad_symbol", "空代码")
    if text.lower().startswith(prefix.lower()):
        return text
    return f"{prefix}{text}"


# ---------------------------------------------------------------------- #
# 快照对象（**不是** MarketEvent）
# ---------------------------------------------------------------------- #
@dataclass(frozen=True)
class FosunQuoteSnapshot:
    """报价快照。官方不提供事件时间，因此 ``event_time`` 恒为 None。"""

    symbol: str
    last: float
    bid: float
    ask: float
    volume: float
    open: float
    high: float
    low: float
    close: float
    receive_time: datetime
    #: ``unknown`` = 协议未定义事件时间；``missing`` = 字段缺失；
    #: ``vendor_quote_time`` = 官方给的报价时间（qtDate+qtTime，非交易所撮合时间）
    time_semantics: str = "unknown"
    event_time: Optional[datetime] = None
    #: 昨收（真实响应里的 pClose）
    prev_close: float = 0.0
    #: 官方标记该报价是否为延迟行情（真实响应里的 delay）
    delayed: bool = False
    #: 官方交易状态码（tradeStatus）
    trade_status: int = 0
    #: 分时段的子行情：``{"prepost": …, "nightQuote": …}``。
    #: **与主价格隔离**，绝不合并成一个"常规报价"。
    session_quotes: Dict[str, "FosunSessionQuote"] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {"symbol": self.symbol, "last": self.last, "bid": self.bid,
                "ask": self.ask, "volume": self.volume, "open": self.open,
                "high": self.high, "low": self.low, "close": self.close,
                "receive_time": self.receive_time.isoformat(),
                "event_time": None, "time_semantics": self.time_semantics}


#: 美股四段时段（美东）。用于把子行情按**时间**归类，而不是按未知的 type 枚举。
SESSION_WINDOWS = (("pre", (4, 0), (9, 30)),
                   ("regular", (9, 30), (16, 0)),
                   ("post", (16, 0), (20, 0)),
                   ("night", (20, 0), (4, 0)))


#: 官方子行情字段名 → 它所属的时段集合。字段名是官方的分类，比时间推断更权威。
SESSION_BUCKETS = {"prepost": ("pre", "post"), "nightQuote": ("night",)}


def classify_session(key: str, local_dt: Optional[datetime]) -> str:
    """判定子行情时段。

    规则：**官方字段名给出候选集合**，时间只用来在集合内细分；
    两者冲突（例如 ``prepost`` 的时间恰好是 20:00 夜盘边界）就标 ``ambiguous``，
    不做二选一的猜测。
    """
    if local_dt is None:
        return "unknown"
    derived = session_of(local_dt)
    allowed = SESSION_BUCKETS.get(key, ())
    if not allowed:
        return derived
    return derived if derived in allowed else "ambiguous"


def session_of(local_dt: datetime) -> str:
    """按美东时间判定时段。**只看时间**，不解释官方 ``type`` 枚举。"""
    minutes = local_dt.hour * 60 + local_dt.minute
    for name, start, end in SESSION_WINDOWS:
        a = start[0] * 60 + start[1]
        b = end[0] * 60 + end[1]
        if a < b:
            if a <= minutes < b:
                return name
        else:                      # 跨零点（night: 20:00–04:00）
            if minutes >= a or minutes < b:
                return name
    return "unknown"


@dataclass(frozen=True)
class FosunSessionQuote:
    """单个时段的子行情（``prepost`` / ``nightQuote``）。

    这些子对象**不能**与常规报价混用：它们有各自的时间戳与成交量。
    """

    symbol: str
    session: str                 # pre / regular / post / night / unknown
    price: float
    open: float
    high: float
    low: float
    prev_close: float
    volume: float
    event_time: Optional[datetime]
    time_semantics: str = "vendor_quote_time"
    #: 官方 ``type`` 字段原样保留。**不解释**——枚举含义未见官方文档。
    raw_type: Optional[int] = None
    #: 该时段内部的涨跌幅（官方已给，未自行计算）
    chg_pct: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {"symbol": self.symbol, "session": self.session,
                "price": self.price, "prev_close": self.prev_close,
                "volume": self.volume,
                "event_time": self.event_time.isoformat() if self.event_time else None,
                "time_semantics": self.time_semantics,
                "raw_type": self.raw_type, "chg_pct": self.chg_pct}


@dataclass(frozen=True)
class FosunOrderBookSnapshot:
    symbol: str
    bids: List[List[float]]
    asks: List[List[float]]
    receive_time: datetime
    time_semantics: str = "missing"
    event_time: Optional[datetime] = None


@dataclass(frozen=True)
class FosunKlineBar:
    symbol: str
    event_time: datetime          # 官方给的柱起始时间（毫秒 → UTC）
    open: float
    high: float
    low: float
    close: float
    volume: float


# ---------------------------------------------------------------------- #
# 解析器
# ---------------------------------------------------------------------- #
def parse_tick(raw: Dict[str, Any], *, symbol: str, receive_time: datetime,
               provider: str = "fosun") -> TradeEvent:
    """``{id,time,price,volume,side}`` → :class:`TradeEvent`。

    时间单位：**毫秒**（官方文档示例 `1699000000000`）。
    """
    if not isinstance(raw, dict):
        raise FosunDataError("bad_payload", "tick 不是对象")
    event_time = ms_to_utc(raw.get("time"))
    price = to_float(raw.get("price"), field_name="price", allow_zero=False)
    volume = to_float(raw.get("volume"), field_name="volume", allow_zero=True)

    tick_id = raw.get("id")
    try:
        source_id = str(int(tick_id)) if tick_id is not None else ""
    except (TypeError, ValueError):
        raise FosunDataError("bad_id", f"tick id 非整数：{tick_id!r}") from None

    return TradeEvent(
        provider=provider,
        symbol=normalize_symbol(symbol) or str(symbol).upper(),
        exchange=FOSUN_VENUE,
        event_time=event_time,
        receive_time=receive_time,
        # 官方没有协议级 sequence；tick id 只作审计字段，不冒充序号。
        sequence=0,
        source_event_id=source_id,
        authority=EventAuthority.VENDOR_QUOTE,
        quality=DataQuality(),
        raw_time=str(raw.get("time")),
        raw_type="tick",
        price=price,
        size=volume,
        side=map_side(raw.get("side")),
        price_scale="float",
    )


def _quote_event_time(raw: Dict[str, Any]) -> Tuple[Optional[datetime], str]:
    """把 ``qtDate``(YYYYMMDD) + ``qtTime``(HHMMSSmmm) 还原成 UTC。

    时区依据：常规收盘 16:00:01、夜盘 03:42 都落在美东时间，因此按
    ``America/New_York`` 解释。**这是官方报价时间，不是交易所撮合时间** ——
    语义上必须与 exchange time 区分。
    """
    date_v, time_v = raw.get("qtDate"), raw.get("qtTime")
    if date_v in (None, "", 0) or time_v in (None, "", 0):
        return None, "missing"
    try:
        ds = str(int(date_v))
        ts = f"{int(time_v):09d}"
        if len(ds) != 8 or len(ts) != 9:
            raise ValueError("bad length")
        y, mo, d = int(ds[0:4]), int(ds[4:6]), int(ds[6:8])
        hh, mi, se, ms = int(ts[0:2]), int(ts[2:4]), int(ts[4:6]), int(ts[6:9])
        if not (MIN_YEAR <= y <= MAX_YEAR):
            raise ValueError("bad year")
        local = datetime(y, mo, d, hh, mi, se, ms * 1000,
                         tzinfo=ZoneInfo("America/New_York"))
        return local.astimezone(timezone.utc), "vendor_quote_time"
    except (TypeError, ValueError):
        raise FosunDataError("bad_time",
                             f"quote 时间非法：qtDate={date_v!r} qtTime={time_v!r}") from None


def signed_float(value: Any, default: float = 0.0) -> float:
    """允许负数的数值（chgPct/chgVal）。缺失或非数字给默认值。

    为什么单独一个函数：``to_float`` 会拒绝负数（价格不能为负），
    但涨跌幅本来就可以为负。早期把 chgPct 丢给 ``to_float``，
    结果整个 nightQuote 解析失败被静默跳过。
    """
    if value is None or isinstance(value, bool):
        return default
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    return num if math.isfinite(num) else default


def _scaled(value: Any, *, field_name: str, allow_zero: bool = True) -> float:
    """放大整数 → 实际价格（÷1e4）。"""
    num = to_float(value, field_name=field_name, allow_zero=allow_zero)
    return num / QUOTE_PRICE_SCALE


def parse_quote(raw: Dict[str, Any], *, symbol: str,
                receive_time: datetime) -> FosunQuoteSnapshot:
    """报价快照。**不会**被转成事件。

    支持两种形状：

    * **真实形状**（官方 quote 实际返回）：``price/pClose/open/high/low/vol``
      为放大整数，另带 ``qtDate/qtTime/delay/tradeStatus``
    * **简化形状**（早期样例/测试）：``last/bid/ask/volume/...`` 已是浮点

    真实响应里**没有 bid/ask**，这两个字段恒为 0；不能拿 last 去冒充买一卖一。
    """
    if not isinstance(raw, dict):
        raise FosunDataError("bad_payload", "quote 不是对象")

    if "price" in raw and ("qtDate" in raw or "pClose" in raw):
        event_time, semantics = _quote_event_time(raw)
        return FosunQuoteSnapshot(
            symbol=normalize_symbol(symbol) or str(symbol).upper(),
            last=_scaled(raw.get("price"), field_name="price", allow_zero=False),
            bid=0.0, ask=0.0,               # 官方快照不含盘口
            volume=to_float(raw.get("vol"), field_name="volume"),
            open=_scaled(raw.get("open"), field_name="price"),
            high=_scaled(raw.get("high"), field_name="price"),
            low=_scaled(raw.get("low"), field_name="price"),
            close=_scaled(raw.get("latestClose", raw.get("pClose")), field_name="price"),
            prev_close=_scaled(raw.get("pClose"), field_name="price"),
            receive_time=receive_time,
            time_semantics=semantics,
            event_time=event_time,
            delayed=bool(raw.get("delay", False)),
            trade_status=int(raw.get("tradeStatus") or 0),
            session_quotes=_collect_session_quotes(raw, symbol),
            raw=dict(raw),
        )

    return FosunQuoteSnapshot(
        symbol=normalize_symbol(symbol) or str(symbol).upper(),
        last=to_float(raw.get("last"), field_name="price", allow_zero=True),
        bid=to_float(raw.get("bid"), field_name="price", allow_zero=True),
        ask=to_float(raw.get("ask"), field_name="price", allow_zero=True),
        volume=to_float(raw.get("volume"), field_name="volume"),
        open=optional_float(raw.get("open")),
        high=optional_float(raw.get("high")),
        low=optional_float(raw.get("low")),
        close=optional_float(raw.get("close")),
        receive_time=receive_time,
        time_semantics="unknown",
        event_time=None,
        raw=dict(raw),
    )


def parse_session_quote(raw: Dict[str, Any], *, symbol: str,
                        key: str) -> FosunSessionQuote:
    """解析 ``prepost`` / ``nightQuote`` 这类子行情。

    时段由**官方时间戳**换算后判定，而不是从 ``type`` 枚举推断
    （该枚举含义未见官方文档，标 UNKNOWN）。
    """
    if not isinstance(raw, dict):
        raise FosunDataError("bad_payload", f"{key} 不是对象")
    event_time, semantics = _quote_event_time(raw)
    local = event_time.astimezone(ZoneInfo("America/New_York")) if event_time else None
    session = classify_session(key, local)
    raw_type = raw.get("type")
    try:
        type_int = int(raw_type) if raw_type is not None else None
    except (TypeError, ValueError):
        type_int = None
    return FosunSessionQuote(
        symbol=normalize_symbol(symbol) or str(symbol).upper(),
        session=session,
        price=_scaled(raw.get("price"), field_name="price", allow_zero=False),
        open=_scaled(raw.get("open"), field_name="price"),
        high=_scaled(raw.get("high"), field_name="price"),
        low=_scaled(raw.get("low"), field_name="price"),
        prev_close=_scaled(raw.get("pClose"), field_name="price"),
        volume=to_float(raw.get("vol"), field_name="volume"),
        event_time=event_time, time_semantics=semantics,
        raw_type=type_int,
        chg_pct=signed_float(raw.get("chgPct")) / QUOTE_PCT_SCALE,
    )


def parse_orderbook(raw: Dict[str, Any], *, symbol: str,
                    receive_time: datetime) -> FosunOrderBookSnapshot:
    """盘口快照。官方无时间戳 → ``time_semantics="missing"``。"""
    if not isinstance(raw, dict):
        raise FosunDataError("bad_payload", "orderbook 不是对象")
    bids = _levels(raw.get("bid"))
    asks = _levels(raw.get("ask"))
    if not bids and not asks:
        raise FosunDataError("bad_payload", "盘口两侧都为空")
    return FosunOrderBookSnapshot(symbol=normalize_symbol(symbol) or symbol,
                                  bids=bids, asks=asks,
                                  receive_time=receive_time)


def _levels(rows: Any) -> List[List[float]]:
    out: List[List[float]] = []
    for row in (rows or []):
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        try:
            price = float(row[0])
            size = float(row[1])
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(price) and math.isfinite(size)):
            continue
        if price <= 0 or size < 0:
            continue
        out.append([price, size])
    return out


def parse_kline_row(row: Sequence[Any], *, symbol: str) -> FosunKlineBar:
    """``[ts, open, high, low, close, volume]``，ts 为**毫秒**。"""
    if not isinstance(row, (list, tuple)) or len(row) < 6:
        raise FosunDataError("bad_payload", f"K 线行长度不足：{row!r}")
    return FosunKlineBar(
        symbol=normalize_symbol(symbol) or str(symbol).upper(),
        event_time=ms_to_utc(row[0]),
        open=to_float(row[1], field_name="price"),
        high=to_float(row[2], field_name="price"),
        low=to_float(row[3], field_name="price"),
        close=to_float(row[4], field_name="price"),
        volume=to_float(row[5], field_name="volume"),
    )


def _collect_session_quotes(raw: Dict[str, Any],
                            symbol: str) -> Dict[str, FosunSessionQuote]:
    """把响应里出现的子行情挑出来解析。解析失败只跳过，不影响主报价。"""
    out: Dict[str, FosunSessionQuote] = {}
    for key in ("prepost", "nightQuote"):
        node = raw.get(key)
        if not isinstance(node, dict):
            continue
        try:
            out[key] = parse_session_quote(node, symbol=symbol, key=key)
        except FosunDataError:
            continue
    return out


def _first_quote_row(data: Any, wire: str, symbol: str) -> Optional[Dict[str, Any]]:
    """从 quote 响应里取出该标的的行。

    官方实际返回的是**数组**（``[{...}]``），早期样例是 ``{code: {...}}`` 字典。
    两种都认；认不出返回 None，由调用方 fail-closed（绝不返回空壳 Quote）。
    """
    if isinstance(data, list):
        for row in data:
            if not isinstance(row, dict):
                continue
            raw = str(row.get("rawSymbol") or "").upper()
            if raw and raw == str(symbol).upper():
                return row
        return next((r for r in data if isinstance(r, dict)), None)
    if isinstance(data, dict):
        for key in (wire, symbol, str(symbol).upper()):
            if isinstance(data.get(key), dict):
                return data[key]
        return next((v for v in data.values() if isinstance(v, dict)), None)
    return None


# ---------------------------------------------------------------------- #
# Provider
# ---------------------------------------------------------------------- #
class FosunProvider(MarketDataProvider):
    """复星财富行情源。

    只实现**逐笔成交**这一条可用管道：它是六个接口中唯一同时具备
    "事件时间"与"增量游标"的。其余接口要么没有时间戳（quote/orderbook/min），
    要么我们没有对应事件模型（kline）。
    """

    name = "fosun"

    def __init__(self, config: Optional[Dict[str, Any]] = None, *,
                 transport: Any = None,
                 session_provider: Optional[Callable[[], proto.FosunSession]] = None,
                 clock: Callable[[], datetime] = None):
        super().__init__(config)
        self._transport = transport
        self._session_provider = session_provider
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.prefix = str(self.config.get("symbol_prefix", "us"))
        self.tick_count = int(self.config.get("tick_count", 50))
        self.strict_gap_check = bool(self.config.get("strict_gap_check", False))
        self.max_gap_seconds = float(self.config.get("max_gap_seconds", 5.0))
        #: 已处理过的 tick id（有界），用于去重与乱序判定
        self._seen: Dict[str, Dict[str, int]] = {}
        self._max_id: Dict[str, int] = {}
        self._seq_window = int(self.config.get("id_window", 5000))
        self.permission_denied = False
        self.counters: Dict[str, int] = {
            "ticks": 0, "duplicates": 0, "out_of_order": 0, "gaps": 0,
            "rejected": 0,
        }

    # ------------------------------------------------------------------ #
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=self.name,
            us_full_market=False,        # 覆盖范围未知
            sip_consolidated=False,      # 券商聚合 ≠ SIP，且无证明
            exchanges=(FOSUN_VENUE,),    # 合成交易所
            realtime=False,              # 未实测，不得声明为实时
            delay_seconds=0,
            historical=True,             # tick 支持 ts 回溯（年限未知）
            history_years=0.0,           # 不写猜测值
            backfill=False,
            premarket=False, regular=True, afterhours=False, overnight=False,
            trades=True,
            quotes=False,                # quote 无事件时间 → 不产出事件
            order_book=False,            # orderbook 无事件时间
            order_book_depth=int(self.config.get("orderbook_depth", 5)),
            halt_events=False,           # 协议中不存在
            halt_events_native=False,
            luld_events=False,
            mwcb_events=False,
            exchange_field=False,        # 无交易所字段
            sequence_field=False,        # 无协议级序号
            source_event_id=False,       # tick id 不是事件 ID
            requires_exchange_license=True,
            notes=("复星财富 OpenAPI：券商聚合行情，非交易所直连；"
                   "exchange 为合成 fosun；quote/orderbook 无事件时间不产出；"
                   "无停牌/LULD/MWCB；行情响应无签名；"
                   "行情权限需账户单独开通"))

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def _connect(self) -> None:
        if self._transport is None:
            self._transport = self._build_transport_from_env()
        if self._session_provider is not None:
            # 注入式会话：立即取一次，验证可用
            self._session_provider()

    def _build_transport_from_env(self):
        """从环境变量构造真实传输。**只读环境变量**，不搜盘、不读文件。"""
        from .fosun_transport import FosunHttpTransport

        base_url = os.environ.get("FSOPENAPI_BASE_URL", "").strip()
        api_key = os.environ.get("FSOPENAPI_API_KEY", "").strip()
        priv_pem = os.environ.get("FSOPENAPI_CLIENT_PRIVATE_KEY", "").strip()
        pub_pem = os.environ.get("FSOPENAPI_SERVER_PUBLIC_KEY", "").strip()
        # 先拦截"连到测试环境"这件事。文档示例里的默认值是 openapi-sit，
        # 照抄会把测试行情当成真实行情 —— 这比报错危险得多。
        host = urlparse(base_url).hostname or ""
        if "sit" in host.lower() and not self.config.get("allow_test_endpoint", False):
            raise ProviderNotConfigured(
                f"base_url 指向测试环境（{host}）：测试行情不代表真实市场。"
                f"请改用开放平台下发的生产地址；确需测试请显式设置 "
                f"allow_test_endpoint=True")

        missing = [name for name, val in (
            ("FSOPENAPI_BASE_URL", base_url), ("FSOPENAPI_API_KEY", api_key),
            ("FSOPENAPI_CLIENT_PRIVATE_KEY", priv_pem),
            ("FSOPENAPI_SERVER_PUBLIC_KEY", pub_pem)) if not val]
        if missing:
            raise ProviderNotConfigured(
                "缺少环境变量：" + ", ".join(missing) +
                "（本适配器不会从命令行、配置文件或磁盘搜索凭据）")

        holder = _SessionHolder(base_url, api_key, priv_pem, pub_pem,
                                timeout=float(self.config.get("timeout", 3.0)),
                                clock=self._clock,
                                lang=str(self.config.get("lang", "zhCn")),
                                client_source=str(self.config.get(
                                    "client_source", "python-sdk")),
                                client_product=str(self.config.get(
                                    "client_product", "sdk")),
                                client_id=str(self.config.get(
                                    "client_id", "fosunai-selfhosted")))
        self._session_provider = holder.get
        return FosunHttpTransport(
            base_url, api_prefix=str(self.config.get("api_prefix", "/api")),
            session_provider=holder.get, api_key=api_key,
            timeout=float(self.config.get("timeout", 3.0)),
            rate_per_sec=float(self.config.get("rate_per_sec", 2.0)),
            burst=int(self.config.get("burst", 4)),
            lang=str(self.config.get("lang", "zhCn")),
            client_id=str(self.config.get("client_id", "fosunai-selfhosted")))

    def _close(self) -> None:
        if self._transport is not None and hasattr(self._transport, "close"):
            try:
                self._transport.close()
            except Exception:  # noqa: BLE001
                pass

    def _subscribe_raw(self, symbols: List[str]) -> None:
        for s in symbols:
            self._seen.setdefault(normalize_symbol(s) or s, {})
            self._max_id.setdefault(normalize_symbol(s) or s, -1)

    # ------------------------------------------------------------------ #
    # 拉取
    # ------------------------------------------------------------------ #
    def _poll_raw(self) -> List[Any]:
        if self.permission_denied:
            raise ProviderUnavailable("行情权限未开通，已停止轮询（fail-closed）")
        if self._transport is None:
            raise ProviderUnavailable("传输未初始化")

        out: List[Any] = []
        now = self._clock()
        for symbol in list(self._subs):
            code = wire_code(symbol, self.prefix)
            try:
                data = self._transport.request(
                    "GET", "/v1/market/secu/tick",
                    params={"code": code, "count": self.tick_count, "id": -1})
            except proto.FosunPermissionError as exc:
                self.permission_denied = True
                self.health.last_error = str(exc)
                raise ProviderUnavailable(str(exc)) from None
            except proto.FosunProtocolError as exc:
                self.health.last_error = str(exc)
                raise ProviderUnavailable(f"行情拉取失败：{exc}") from None
            except Exception as exc:  # noqa: BLE001 - 网络层异常一律转成不可用
                self.health.last_error = f"{type(exc).__name__}: {exc}"
                raise ProviderUnavailable(
                    f"行情拉取异常：{type(exc).__name__}: {exc}") from None

            ticks = data.get("ticks") if isinstance(data, dict) else None
            if not isinstance(ticks, list):
                self.health.last_error = "响应缺少 ticks 数组"
                raise ProviderUnavailable("响应缺少 ticks 数组")
            for row in ticks:
                if isinstance(row, dict):
                    out.append({"_symbol": symbol, "_receive": now, **row})
        return out

    # ------------------------------------------------------------------ #
    # 归一到事件
    # ------------------------------------------------------------------ #
    def _normalize(self, item: Any) -> Optional[TradeEvent]:
        symbol = item.get("_symbol") if isinstance(item, dict) else None
        if not symbol:
            self._reject("bad_payload", "缺少 _symbol")
            return None
        receive = item.get("_receive") or self._clock()
        payload = {k: v for k, v in item.items()
                   if not str(k).startswith("_")}

        try:
            ev = parse_tick(payload, symbol=symbol, receive_time=receive)
        except FosunDataError as exc:
            self.counters["rejected"] += 1
            self._reject(exc.reason, exc.detail)
            return None

        key = ev.symbol
        verdict = self._check_tick_id(key, payload.get("id"))
        if verdict == "duplicate":
            self.counters["duplicates"] += 1
            ev.quality.add(QualityFlag.DUPLICATE, "同一 tick id 重复投递")
            # 带标记返回而不是静默丢弃：让上层/测试能看到"为什么被拦"。
            # feed 会因 DUPLICATE ∈ BLOCKING_FLAGS 拒绝它。
            return ev
        if verdict == "out_of_order":
            self.counters["out_of_order"] += 1
            ev.quality.add(QualityFlag.OUT_OF_ORDER, "tick id 回退")
            return ev
        if verdict == "gap" and self.strict_gap_check:
            self.counters["gaps"] += 1
            ev.quality.add(QualityFlag.GAP, "tick id 跳号")

        self.counters["ticks"] += 1
        return ev

    def _check_tick_id(self, symbol: str, raw_id: Any) -> str:
        """返回 ``ok`` / ``duplicate`` / ``out_of_order`` / ``gap``。

        不把 tick id 当协议序号：只用于**本地**去重与乱序检测。
        """
        try:
            tid = int(raw_id)
        except (TypeError, ValueError):
            return "ok"
        seen = self._seen.setdefault(symbol, {})
        last = self._max_id.get(symbol, -1)

        if tid in seen:
            return "duplicate"
        if tid < last:
            return "out_of_order"
        gap = tid > (last + 1) and last >= 0

        seen[tid] = 1
        if len(seen) > self._seq_window:
            for k in sorted(seen)[: len(seen) - self._seq_window]:
                seen.pop(k, None)
        self._max_id[symbol] = max(last, tid)
        return "gap" if gap else "ok"

    # ------------------------------------------------------------------ #
    # 只读快照（不进事件管道）
    # ------------------------------------------------------------------ #
    def snapshot_quote(self, symbol: str) -> FosunQuoteSnapshot:
        """取报价快照。**仅供展示/审计**，不得用于行情门禁。"""
        if self._transport is None:
            raise ProviderUnavailable("传输未初始化")
        # 官方是 POST + JSON body，且 codes 是**数组**：
        #   client.post("/v1/market/secu/quote", data={"codes": list(codes)})
        # 用 GET + query 会同时错在方法、参数位置与值类型三处。
        data = self._transport.request(
            "POST", "/v1/market/secu/quote",
            json_body={"codes": [wire_code(symbol, self.prefix)]})
        row = _first_quote_row(data, wire_code(symbol, self.prefix), symbol)
        if row is None:
            raise ProviderUnavailable("报价响应缺少该代码的数据")
        return parse_quote(row, symbol=symbol, receive_time=self._clock())

    # ------------------------------------------------------------------ #
    def quote(self, symbol: str) -> Quote:
        """给 QuoteGateway 用的 ``broker.base.Quote``。

        这是本项目**唯一**允许把官方 quote 快照送进网关的入口。
        时间戳取官方 ``qtDate+qtTime``（语义 = 供应商报价时间，不是交易所撮合时间），
        因此网关的时段判定会按该时刻计算——快照过期时会被正常拒绝。
        """
        snap = self.snapshot_quote(symbol)
        return Quote(
            symbol=snap.symbol, market=self.config.get("market", "us"),
            last=snap.last, open=snap.open, high=snap.high, low=snap.low,
            prev_close=snap.prev_close, volume=snap.volume,
            ts=snap.event_time.isoformat() if snap.event_time else "",
            source="fosun")

    def snapshot_orderbook(self, symbol: str, count: int = 5) -> FosunOrderBookSnapshot:
        if self._transport is None:
            raise ProviderUnavailable("传输未初始化")
        data = self._transport.request(
            "GET", "/v1/market/secu/orderbook",
            params={"code": wire_code(symbol, self.prefix), "count": count})
        return parse_orderbook(data or {}, symbol=symbol, receive_time=self._clock())

    # ------------------------------------------------------------------ #
    def snapshot(self) -> Dict[str, Any]:
        base = super().snapshot()
        base["fosun"] = {"permission_denied": self.permission_denied,
                         "counters": dict(self.counters),
                         "subscribed": list(self._subs)}
        return base


class _SessionHolder:
    """缓存会话，过期前重建。用完不落盘（私钥不写文件）。"""

    def __init__(self, base_url: str, api_key: str, private_pem: str,
                 server_pem: str, *, timeout: float = 3.0,
                 clock: Callable[[], datetime] = None,
                 lang: str = "zhCn",
                 client_source: str = "python-sdk",
                 client_product: str = "sdk",
                 client_id: str = "fosunai-selfhosted"):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._identity = proto.load_private_key_pem(private_pem)
        self._server_pub = proto.load_public_key_pem(server_pem)
        self.timeout = float(timeout)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.lang = str(lang or "zhCn")
        # X-source / X-product 是协议常量（官方固定 python-sdk / sdk）。
        # 它们不参与签名、不承担认证；填官方值是为了让网关按已知客户端类型处理。
        # X-Request-Client 保留本项目真实标识，不冒充官方 SDK。
        self.client_source = str(client_source or "python-sdk")
        self.client_product = str(client_product or "sdk")
        self.client_id = str(client_id or "fosunai-selfhosted")
        self._session: Optional[proto.FosunSession] = None

    def get(self) -> proto.FosunSession:
        now_epoch = self._clock().timestamp()
        if self._session is not None and self._session.is_valid(now_epoch):
            return self._session
        self._session = self._handshake()
        return self._session

    def _handshake(self) -> proto.FosunSession:
        import requests as _rq
        ephemeral, eph_pub = proto.generate_ephemeral_keypair()
        nonce = proto.new_nonce()
        signature = proto.sign_handshake(self._identity, eph_pub, nonce)

        url = f"{self.base_url}/api/v1/auth/SessionCreate"
        # 逐项对齐官方 auth.py::create_session 的握手头。
        # 注意官方握手用的是 **X-timestamp**（小写 t），业务请求用 X-Timestamp；
        # HTTP 头本应大小写不敏感，但这里照抄以免踩到大小写敏感的中间件。
        headers = {"X-Lang": self.lang,
                   "Content-Type": "application/json",
                   "Accept": "application/json",
                   "X-API-Key": self.api_key,
                   "X-Request-Id": str(__import__("uuid").uuid4()),
                   "X-Request-Client": self.client_id,
                   "X-timestamp": str(int(time.time() * 1000)),
                   "X-source": self.client_source,
                   "X-product": self.client_product,
                   "X-Nonce": nonce,
                   "X-Signature": signature}
        try:
            resp = _rq.post(url, json={"apiKey": self.api_key,
                                       "clientTempPublicKey": eph_pub},
                            headers=headers, timeout=self.timeout)
        except _rq.RequestException as exc:
            raise ProviderUnavailable(f"握手请求失败：{type(exc).__name__}") from None

        if resp.status_code in (401, 403):
            raise proto.FosunPermissionError(f"握手被拒：HTTP {resp.status_code}")
        if resp.status_code >= 500:
            raise proto.FosunUpstreamError(f"握手 HTTP {resp.status_code}")

        try:
            body = resp.json()
        except ValueError:
            raise proto.FosunProtocolError("握手响应不是 JSON") from None
        data = body.get("content") if "encrypted" in body else body
        if not isinstance(data, dict):
            raise proto.FosunProtocolError("握手响应结构不可识别")
        if int(data.get("code", -1)) != 0:
            raise proto.FosunAuthError(f"握手失败：{data.get('message')}")

        payload = data.get("data") or {}
        session_id = str(payload.get("sessionId") or "")
        server_pub = str(payload.get("serverTempPublicKey") or "")
        expires_at = float(payload.get("expiresAt") or 0)
        server_sig = resp.headers.get("X-Signature", "")
        server_nonce = resp.headers.get("X-Nonce", "")
        if not (session_id and server_pub and server_sig and server_nonce):
            raise proto.FosunAuthError("握手响应缺少 sessionId/临时公钥/签名/nonce")
        if not proto.verify_handshake(self._server_pub, server_pub, server_nonce,
                                      server_sig):
            raise proto.FosunAuthError("服务端握手签名校验失败")

        mac, enc = proto.derive_session_keys(ephemeral, server_pub, nonce, server_nonce)
        return proto.FosunSession(session_id=session_id, signing_key=mac,
                                  encryption_key=enc, expires_at=expires_at)
