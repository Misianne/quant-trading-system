"""统一行情数据模型（Provider 层）。

设计原则
--------
1. **所有事件都带来源五元组**：``provider`` / ``symbol`` / ``exchange`` /
   ``event_time`` / ``receive_time``，外加 ``sequence`` 与 ``source_event_id``。
   少了任何一个都无法做乱序/重复/缺口判定。
2. **内部一律 UTC**，同时保留 ``raw_time``（供应商原始字段）与 ``raw_type``。
   不允许"猜时区"：naive 时间直接标记为 ``TIME_NAIVE`` 并降级。
3. **质量状态随数据走**（:class:`DataQuality`），不是旁路日志。
   下游（QuoteGateway / PaperRuntime）据此决定是否可用。
4. **事件分类**（:class:`EventAuthority`）区分：
   A 普通行情 / B 交易所官方停复牌 / C LULD 官方状态 / D MWCB 官方状态。
   供应商给的 ``halt=true`` 只能算 A，不能自动升格为 B。

不做的事
--------
* 不把供应商字段"翻译"成官方事件；
* 不为缺失的交易所补造事件；
* 不在模型层做交易决策。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------- #
# 枚举
# ---------------------------------------------------------------------- #
class EventType(str, Enum):
    QUOTE = "quote"
    TRADE = "trade"
    ORDER_BOOK = "order_book"
    SESSION = "session"
    HALT = "halt"
    LULD = "luld"
    MWCB = "mwcb"
    HEARTBEAT = "heartbeat"


class EventAuthority(str, Enum):
    """事件权威级别。**只有 B/C/D 才可能驱动交易状态机。**"""

    VENDOR_QUOTE = "A_vendor_quote"          # 普通行情（含供应商自带的 halt 标记）
    EXCHANGE_HALT = "B_exchange_halt"        # 交易所官方停复牌消息
    LULD_OFFICIAL = "C_luld_official"        # LULD 官方价格带/状态
    MWCB_OFFICIAL = "D_mwcb_official"        # 全市场熔断官方状态
    UNKNOWN = "unknown"


class QualityFlag(str, Enum):
    OK = "ok"
    DUPLICATE = "duplicate"
    OUT_OF_ORDER = "out_of_order"
    GAP = "gap"
    STALE = "stale"
    FUTURE = "future"
    NAIVE_TIME = "naive_time"
    UNKNOWN_SYMBOL = "unknown_symbol"
    EXCHANGE_MISMATCH = "exchange_mismatch"
    SEQUENCE_REGRESSION = "sequence_regression"
    PROVIDER_DOWN = "provider_down"
    NO_AUTHORITY = "no_authority"


class SessionPhase(str, Enum):
    PRE = "pre"
    REGULAR = "regular"
    POST = "post"
    OVERNIGHT = "overnight"
    CLOSED = "closed"


#: 哪些质量标记意味着"这条数据不能用于交易决策"
BLOCKING_FLAGS = frozenset({
    QualityFlag.DUPLICATE, QualityFlag.OUT_OF_ORDER, QualityFlag.GAP,
    QualityFlag.STALE, QualityFlag.FUTURE, QualityFlag.NAIVE_TIME,
    QualityFlag.UNKNOWN_SYMBOL, QualityFlag.EXCHANGE_MISMATCH,
    QualityFlag.SEQUENCE_REGRESSION, QualityFlag.PROVIDER_DOWN,
})


@dataclass
class DataQuality:
    flags: List[QualityFlag] = field(default_factory=list)
    note: str = ""
    latency_ms: Optional[float] = None

    @property
    def usable(self) -> bool:
        return not any(f in BLOCKING_FLAGS for f in self.flags)

    def add(self, flag: QualityFlag, note: str = "") -> "DataQuality":
        if flag not in self.flags:
            self.flags.append(flag)
        if note:
            self.note = (self.note + "; " + note).strip("; ")
        return self

    def as_dict(self) -> Dict[str, Any]:
        return {"flags": [f.value for f in self.flags], "usable": self.usable,
                "note": self.note, "latency_ms": self.latency_ms}


# ---------------------------------------------------------------------- #
# 基础事件
# ---------------------------------------------------------------------- #
@dataclass
class MarketEvent:
    """所有事件的公共字段。"""

    provider: str
    symbol: str
    exchange: str                 # 交易所（小写）；未知用 "unknown"，不得猜
    event_time: datetime          # UTC
    receive_time: datetime        # UTC
    sequence: int = 0             # 供应商序号（可能不连续）
    source_event_id: str = ""     # 供应商事件 ID（有则记录）
    authority: EventAuthority = EventAuthority.UNKNOWN
    quality: DataQuality = field(default_factory=DataQuality)
    raw_time: str = ""            # 原始时间字段（不做改写）
    raw_type: str = ""            # 原始消息类型

    def as_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider, "symbol": self.symbol,
            "exchange": self.exchange,
            "event_time": self.event_time.isoformat(),
            "receive_time": self.receive_time.isoformat(),
            "sequence": self.sequence, "source_event_id": self.source_event_id,
            "authority": self.authority.value, "quality": self.quality.as_dict(),
            "raw_time": self.raw_time, "raw_type": self.raw_type,
        }


@dataclass
class QuoteEvent(MarketEvent):
    bid: float = 0.0
    ask: float = 0.0
    bid_size: float = 0.0
    ask_size: float = 0.0

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return round((self.bid + self.ask) / 2.0, 6)
        return 0.0

    def as_dict(self) -> Dict[str, Any]:
        d = super().as_dict()
        d.update({"bid": self.bid, "ask": self.ask,
                  "bid_size": self.bid_size, "ask_size": self.ask_size})
        return d


@dataclass
class TradeEvent(MarketEvent):
    price: float = 0.0
    size: float = 0.0
    conditions: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        d = super().as_dict()
        d.update({"price": self.price, "size": self.size,
                  "conditions": list(self.conditions)})
        return d


@dataclass
class OrderBookEvent(MarketEvent):
    bids: List[List[float]] = field(default_factory=list)   # [[price, size], ...]
    asks: List[List[float]] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        d = super().as_dict()
        d.update({"bids": [list(b) for b in self.bids],
                  "asks": [list(a) for a in self.asks]})
        return d


@dataclass
class SessionEvent(MarketEvent):
    phase: SessionPhase = SessionPhase.CLOSED

    def as_dict(self) -> Dict[str, Any]:
        d = super().as_dict()
        d["phase"] = self.phase.value
        return d


@dataclass
class HaltEvent(MarketEvent):
    halted: bool = True
    reason: str = ""
    halt_code: str = ""
    expected_resume: Optional[datetime] = None

    def as_dict(self) -> Dict[str, Any]:
        d = super().as_dict()
        d.update({"halted": self.halted, "reason": self.reason,
                  "halt_code": self.halt_code,
                  "expected_resume": self.expected_resume.isoformat()
                  if self.expected_resume else None})
        return d


@dataclass
class LULDState(MarketEvent):
    upper: float = 0.0
    lower: float = 0.0
    indicator: str = ""
    paused: bool = False
    resumed: bool = False

    def as_dict(self) -> Dict[str, Any]:
        d = super().as_dict()
        d.update({"upper": self.upper, "lower": self.lower,
                  "indicator": self.indicator, "paused": self.paused,
                  "resumed": self.resumed})
        return d


@dataclass
class MWCBState(MarketEvent):
    level: int = 0
    triggered: bool = False
    closed_for_day: bool = False

    def as_dict(self) -> Dict[str, Any]:
        d = super().as_dict()
        d.update({"level": self.level, "triggered": self.triggered,
                  "closed_for_day": self.closed_for_day})
        return d


@dataclass
class ProviderHealth:
    provider: str
    connected: bool
    last_message_at: Optional[datetime] = None
    last_error: str = ""
    reconnects: int = 0
    messages: int = 0
    dropped: int = 0
    gaps: int = 0
    duplicate: int = 0
    out_of_order: int = 0
    subscribed: int = 0
    capabilities: Dict[str, Any] = field(default_factory=dict)

    @property
    def healthy(self) -> bool:
        return self.connected and not self.last_error

    def as_dict(self) -> Dict[str, Any]:
        return {"provider": self.provider, "connected": self.connected,
                "healthy": self.healthy,
                "last_message_at": self.last_message_at.isoformat()
                if self.last_message_at else None,
                "last_error": self.last_error, "reconnects": self.reconnects,
                "messages": self.messages, "dropped": self.dropped,
                "gaps": self.gaps, "duplicate": self.duplicate,
                "out_of_order": self.out_of_order,
                "subscribed": self.subscribed,
                "capabilities": dict(self.capabilities)}
