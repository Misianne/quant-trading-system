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
    #: ``unknown`` = 协议未定义事件时间；``exchange`` = 有交易所时间
    time_semantics: str = "unknown"
    event_time: Optional[datetime] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {"symbol": self.symbol, "last": self.last, "bid": self.bid,
                "ask": self.ask, "volume": self.volume, "open": self.open,
                "high": self.high, "low": self.low, "close": self.close,
                "receive_time": self.receive_time.isoformat(),
                "event_time": None, "time_semantics": self.time_semantics}


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


def parse_quote(raw: Dict[str, Any], *, symbol: str,
                receive_time: datetime) -> FosunQuoteSnapshot:
    """报价快照。**不会**被转成事件。"""
    if not isinstance(raw, dict):
        raise FosunDataError("bad_payload", "quote 不是对象")
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
                                clock=self._clock)
        self._session_provider = holder.get
        return FosunHttpTransport(
            base_url, api_prefix=str(self.config.get("api_prefix", "/api")),
            session_provider=holder.get, api_key=api_key,
            timeout=float(self.config.get("timeout", 3.0)),
            rate_per_sec=float(self.config.get("rate_per_sec", 2.0)),
            burst=int(self.config.get("burst", 4)))

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
        data = self._transport.request(
            "GET", "/v1/market/secu/quote",
            params={"codes": wire_code(symbol, self.prefix)})
        row = (data or {}).get(wire_code(symbol, self.prefix)) or (data or {}).get(symbol)
        if not isinstance(row, dict):
            raise ProviderUnavailable("报价响应缺少该代码的数据")
        return parse_quote(row, symbol=symbol, receive_time=self._clock())

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
                 clock: Callable[[], datetime] = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._identity = proto.load_private_key_pem(private_pem)
        self._server_pub = proto.load_public_key_pem(server_pem)
        self.timeout = float(timeout)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
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
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "X-API-Key": self.api_key, "X-Nonce": nonce,
                   "X-Timestamp": str(int(time.time() * 1000)),
                   "X-Signature": signature,
                   "X-Request-Client": "fosunai-selfhosted"}
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
