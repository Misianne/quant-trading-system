"""供应商适配器。

本轮实现的是**可离线验证**的适配器：解析各家**官方文档里给出的字段形状**，
用录制的 fixture 驱动。它们不发起任何网络连接，也不读取任何密钥。

为什么先做离线适配器
--------------------
1. 真实接入需要 API Key 与付费套餐（用户明确要求不要密钥、不要采购）；
2. 供应商字段语义（尤其是时间单位、停牌事件覆盖范围）必须先用真实样本验证，
   否则"接上了"只是把错误数据接进了风控；
3. 适配器可独立测试是接入层质量的前提。

一旦拿到 Key，只需实现 ``_connect/_poll_raw`` 的真实传输，解析逻辑不用改。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from .base import MarketDataProvider, ProviderCapabilities, ProviderError
from .models import (
    DataQuality,
    EventAuthority,
    EventType,
    HaltEvent,
    LULDState,
    MWCBState,
    MarketEvent,
    OrderBookEvent,
    QualityFlag,
    QuoteEvent,
    SessionEvent,
    SessionPhase,
    TradeEvent,
)
from .normalize import normalize_exchange, normalize_symbol, parse_timestamp


# ---------------------------------------------------------------------- #
class RecordedProvider(MarketDataProvider):
    """从 JSONL fixture 回放原始消息，支持注入重复/乱序/缺口/断线。"""

    name = "recorded"

    def __init__(self, fixture: Optional[Path | str] = None,
                 config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.fixture = Path(fixture) if fixture is not None else None
        self._records: List[Dict[str, Any]] = []
        self._cursor = 0
        self.batch_size = int(self.config.get("batch_size", 50))
        # 故障注入
        self.duplicate_every = int(self.config.get("duplicate_every", 0))
        self.swap_pairs = bool(self.config.get("swap_pairs", False))
        self.drop_indexes = set(self.config.get("drop_indexes", []) or [])
        self.down_at = set(self.config.get("down_at", []) or [])
        self._tick = 0
        #: 录制回放的事件时钟（取已投递事件的最大 event_time）
        self._replay_now: Optional[datetime] = None

    def _quality_now(self, ev: MarketEvent) -> Optional[datetime]:
        if not self.config.get("replay_clock", True):
            return None
        if self._replay_now is None or ev.event_time > self._replay_now:
            self._replay_now = ev.event_time
        return self._replay_now

    def _connect(self) -> None:
        if self.fixture is None:
            raise ProviderError("未指定 fixture（离线适配器必须提供样本文件）",
                                "FIXTURE_REQUIRED")
        if not self.fixture.exists():
            raise ProviderError(f"fixture 不存在：{self.fixture}", "FIXTURE_MISSING")
        recs: List[Dict[str, Any]] = []
        for line in self.fixture.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            recs.append(json.loads(line))
        if not recs:
            raise ProviderError("fixture 为空", "FIXTURE_EMPTY")
        self._records = recs
        self._cursor = 0
        self._tick = 0

    def _close(self) -> None:
        self._cursor = 0

    def _subscribe_raw(self, symbols: List[str]) -> None:
        return None

    def _poll_raw(self) -> List[Any]:
        if self._tick in self.down_at:
            self._tick += 1
            raise ProviderError("模拟断线", "PROVIDER_DOWN")
        out: List[Dict[str, Any]] = []
        end = min(self._cursor + self.batch_size, len(self._records))
        for idx in range(self._cursor, end):
            if idx in self.drop_indexes:
                continue
            rec = dict(self._records[idx])
            rec["_index"] = idx
            out.append(rec)
            if self.duplicate_every and idx and idx % self.duplicate_every == 0:
                out.append(dict(rec))       # 重复投递
        if self.swap_pairs:
            for i in range(0, len(out) - 1, 2):
                out[i], out[i + 1] = out[i + 1], out[i]
        self._cursor = end
        self._tick += 1
        return out

    # ------------------------------------------------------------------ #
    def _normalize(self, item: Any) -> Optional[MarketEvent]:
        try:
            ev = self.parse(item)
        except Exception:  # noqa: BLE001 - 单条坏数据不该中断整批
            return None
        if ev is None:
            return None
        # 录制回放：receive_time 跟事件时间一致，保证同一 fixture 多次运行结果确定
        if self.config.get("replay_clock", True):
            ev.receive_time = ev.event_time
        else:
            ev.receive_time = datetime.now(timezone.utc)
        if not ev.provider:
            ev.provider = self.name
        return ev

    def parse(self, item: Dict[str, Any]) -> Optional[MarketEvent]:  # pragma: no cover
        raise NotImplementedError


# ---------------------------------------------------------------------- #
# Massive（原 Polygon.io）
# ---------------------------------------------------------------------- #
class MassiveProvider(RecordedProvider):
    """字段依据：massive.com 文档 ``/docs/websocket/stocks/luld`` 与 trades/quotes。

    LULD 消息：``{"ev":"LULD","T":"MSFT","h":492.99,"l":446.04,"i":[15],"z":3,
    "t":1764086430905642800,"q":5925769}``
    * ``t`` 为**纳秒** epoch（官方 blog 示例）；文档另处写 "Unix MS" → 由配置
      ``time_unit`` 显式指定，默认 ``ns`` 并在适配器里记录该歧义。
    * ``i`` 中 17=暂停 / 18=恢复，**仅 Nasdaq 上市证券**。
    * ``z`` 磁带：1=NYSE, 2=AMEX, 3=Nasdaq。
    """

    name = "massive"

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=self.name, us_full_market=True, sip_consolidated=True,
            exchanges=("nyse", "nasdaq", "amex", "arca", "cboe_bzx"),
            realtime=True, historical=True, history_years=20, backfill=True,
            premarket=True, regular=True, afterhours=True, overnight=True,
            trades=True, quotes=True, order_book=False,
            halt_events=True, halt_events_native=False,
            halt_events_nasdaq_listed_only=True,
            luld_events=True, luld_events_native=False,
            mwcb_events=False, mwcb_events_native=False,
            exchange_field=True, sequence_field=True, source_event_id=False,
            requires_exchange_license=True,
            notes="LULD 通道含价格带与 17/18 指标；指标仅 Nasdaq 上市；非交易所原生")

    TAPE = {1: "nyse", 2: "amex", 3: "nasdaq"}

    def parse(self, item: Dict[str, Any]) -> Optional[MarketEvent]:
        ev_type = str(item.get("ev", "")).upper()
        sym = normalize_symbol(item.get("T") or item.get("sym"))
        if not sym:
            return None
        unit = str(self.config.get("time_unit", "ns"))
        ts, err = parse_timestamp(item.get("t"), unit)
        if ts is None:
            return None
        tape = item.get("z")
        exchange = self.TAPE.get(int(tape), "unknown") if tape is not None else "unknown"
        q = DataQuality()
        if err:
            q.add(QualityFlag.NAIVE_TIME, err)
        seq = int(item.get("q") or 0)

        base = dict(provider=self.name, symbol=sym, exchange=exchange,
                    event_time=ts, receive_time=ts, sequence=seq,
                    raw_time=str(item.get("t")), raw_type=ev_type, quality=q)

        if ev_type == "LULD":
            ind = item.get("i") or []
            ind_s = ",".join(str(x) for x in ind) if isinstance(ind, list) else str(ind)
            return LULDState(upper=float(item.get("h") or 0),
                             lower=float(item.get("l") or 0),
                             indicator=ind_s,
                             paused=("17" in [str(x) for x in ind]),
                             resumed=("18" in [str(x) for x in ind]),
                             authority=EventAuthority.LULD_OFFICIAL, **base)
        if ev_type in ("T", "XT"):
            return TradeEvent(price=float(item.get("p") or 0),
                              size=float(item.get("s") or 0),
                              conditions=[str(c) for c in (item.get("c") or [])],
                              authority=EventAuthority.VENDOR_QUOTE, **base)
        if ev_type in ("Q",):
            return QuoteEvent(bid=float(item.get("bp") or 0),
                              ask=float(item.get("ap") or 0),
                              bid_size=float(item.get("bs") or 0),
                              ask_size=float(item.get("as") or 0),
                              authority=EventAuthority.VENDOR_QUOTE, **base)
        return None


# ---------------------------------------------------------------------- #
# Alpaca
# ---------------------------------------------------------------------- #
class AlpacaProvider(RecordedProvider):
    """字段依据：alpaca.markets/docs/market-data 的实时流（trade/quote）。

    注意：Basic（免费）只有 IEX；Algo Trader Plus 才是 SIP 全市场。
    本适配器不声明任何官方停复牌事件能力（文档未提供）。
    """

    name = "alpaca"

    #: Alpaca 用单字母交易所代码；**不能**套用全局别名表
    #: （例如 ``N`` 在 Alpaca 是 NYSE，而通用表里 n 曾误映射到 nasdaq）
    EXCHANGES = {"A": "amex", "B": "cboe_bzx", "N": "nyse", "Q": "nasdaq",
                 "V": "iex", "P": "arca", "M": "cboe_edgx"}

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=self.name, us_full_market=True, sip_consolidated=True,
            exchanges=("iex", "nyse", "nasdaq", "amex", "arca"),
            realtime=True, historical=True, history_years=10, backfill=True,
            premarket=True, regular=True, afterhours=True, overnight=True,
            trades=True, quotes=True, order_book=False,
            halt_events=False, halt_events_native=False,
            luld_events=False, luld_events_native=False,
            mwcb_events=False, mwcb_events_native=False,
            exchange_field=True, sequence_field=False, source_event_id=False,
            requires_exchange_license=False,
            notes="SIP 仅 Algo Trader Plus；无停复牌/LULD 事件通道")

    def parse(self, item: Dict[str, Any]) -> Optional[MarketEvent]:
        t = str(item.get("T", "")).lower()
        sym = normalize_symbol(item.get("S"))
        if not sym:
            return None
        ts, err = parse_timestamp(item.get("t"), "iso")
        if ts is None:
            return None
        q = DataQuality()
        if err:
            q.add(QualityFlag.NAIVE_TIME, err)
        raw_ex = str(item.get("x") or item.get("bx") or item.get("ax") or "")
        ex = self.EXCHANGES.get(raw_ex.upper()) or normalize_exchange(raw_ex)
        base = dict(provider=self.name, symbol=sym, exchange=ex,
                    event_time=ts, receive_time=ts,
                    sequence=int(item.get("i") or 0),
                    raw_time=str(item.get("t")), raw_type=t, quality=q)
        if t == "t":
            return TradeEvent(price=float(item.get("p") or 0),
                              size=float(item.get("s") or 0),
                              authority=EventAuthority.VENDOR_QUOTE, **base)
        if t == "q":
            return QuoteEvent(bid=float(item.get("bp") or 0),
                              ask=float(item.get("ap") or 0),
                              bid_size=float(item.get("bs") or 0),
                              ask_size=float(item.get("as") or 0),
                              authority=EventAuthority.VENDOR_QUOTE, **base)
        return None


# ---------------------------------------------------------------------- #
# Databento
# ---------------------------------------------------------------------- #
class DatabentoProvider(RecordedProvider):
    """字段依据：databento.com 的 schema 列表（status / trades / mbp-1 / mbo）。

    ``status`` schema 提供交易所原生交易状态（halt/reopen），带纳秒时间戳。
    仍**没有消息签名**，所以不能作为"密码学可信"的恢复来源。
    """

    name = "databento"

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=self.name, us_full_market=True, sip_consolidated=False,
            exchanges=("nasdaq", "nyse", "arca", "amex", "cboe_bzx"),
            realtime=True, historical=True, history_years=5, backfill=True,
            premarket=True, regular=True, afterhours=True, overnight=False,
            trades=True, quotes=True, order_book=True, order_book_depth=10,
            halt_events=True, halt_events_native=True,
            luld_events=False, luld_events_native=False,
            mwcb_events=False, mwcb_events_native=False,
            exchange_field=True, sequence_field=True, source_event_id=True,
            requires_exchange_license=True,
            notes="交易所直连（XNAS.ITCH 等）；status schema 为原生状态；无签名")

    def parse(self, item: Dict[str, Any]) -> Optional[MarketEvent]:
        sym = normalize_symbol(item.get("symbol"))
        if not sym:
            return None
        ts, err = parse_timestamp(item.get("ts_event"), "ns")
        if ts is None:
            return None
        q = DataQuality()
        if err:
            q.add(QualityFlag.NAIVE_TIME, err)
        ex = normalize_exchange(item.get("publisher_id") or item.get("exchange"))
        seq = int(item.get("sequence") or 0)
        stype = str(item.get("type", "")).lower()
        base = dict(provider=self.name, symbol=sym, exchange=ex,
                    event_time=ts, receive_time=ts, sequence=seq,
                    source_event_id=str(item.get("id") or ""),
                    raw_time=str(item.get("ts_event")), raw_type=stype, quality=q)
        if stype == "status":
            # Databento status: action 如 "H"（halt）/ "T"（trading）/ "Q"（quote）
            action = str(item.get("action", "")).upper()
            return HaltEvent(halted=(action == "H"),
                             reason=str(item.get("reason") or ""),
                             halt_code=action,
                             authority=EventAuthority.EXCHANGE_HALT, **base)
        if stype == "trade":
            return TradeEvent(price=float(item.get("price") or 0) / 1e9,
                              size=float(item.get("size") or 0),
                              authority=EventAuthority.VENDOR_QUOTE, **base)
        if stype in ("mbp-1", "quote"):
            return QuoteEvent(bid=float(item.get("bid_px") or 0) / 1e9,
                              ask=float(item.get("ask_px") or 0) / 1e9,
                              bid_size=float(item.get("bid_sz") or 0),
                              ask_size=float(item.get("ask_sz") or 0),
                              authority=EventAuthority.VENDOR_QUOTE, **base)
        return None


# ---------------------------------------------------------------------- #
# 交易所原生（本阶段只能以录制样本验证）
# ---------------------------------------------------------------------- #
class NasdaqItchProvider(RecordedProvider):
    """Nasdaq TotalView Trading Action 消息（'H' halt / 'Q' quote resume / 'T' trade resume）。"""

    name = "nasdaq_itch"

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=self.name, us_full_market=False, sip_consolidated=False,
            exchanges=("nasdaq",), realtime=True, historical=True,
            history_years=5, backfill=True,
            premarket=True, regular=True, afterhours=True, overnight=False,
            trades=True, quotes=True, order_book=True, order_book_depth=10,
            halt_events=True, halt_events_native=True,
            luld_events=False, mwcb_events=False,
            exchange_field=True, sequence_field=True, source_event_id=True,
            requires_exchange_license=True,
            notes="需要 Nasdaq 直连授权；本阶段仅离线样本")

    def parse(self, item: Dict[str, Any]) -> Optional[MarketEvent]:
        sym = normalize_symbol(item.get("symbol"))
        ts, err = parse_timestamp(item.get("timestamp"), "ns")
        if not sym or ts is None:
            return None
        q = DataQuality()
        if err:
            q.add(QualityFlag.NAIVE_TIME, err)
        action = str(item.get("action", "")).upper()
        if action not in ("H", "Q", "T"):
            return None
        return HaltEvent(halted=(action == "H"),
                         reason=str(item.get("reason") or ""),
                         halt_code=action,
                         authority=EventAuthority.EXCHANGE_HALT,
                         provider=self.name, symbol=sym, exchange="nasdaq",
                         event_time=ts, receive_time=ts,
                         sequence=int(item.get("sequence") or 0),
                         source_event_id=str(item.get("id") or ""),
                         raw_time=str(item.get("timestamp")),
                         raw_type="trading_action", quality=q)
