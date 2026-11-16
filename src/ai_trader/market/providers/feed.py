"""ProviderFeed：把多个供应商的事件流汇聚成"每标的最新状态"。

职责
----
1. 订阅/轮询多个 provider；
2. 按 ``(symbol, exchange)`` 维护最新状态（**多交易所天然隔离**）；
3. 给上层（QuoteGateway）提供与 ``Broker.get_quote`` 同形的报价；
4. 数据不可用/过期/停牌时**拒绝出价**（fail-closed），不返回最后一笔。

不做什么
--------
* 不做交易决策；
* 不把供应商事件升格为官方事件（由 :mod:`authority` 判定）；
* 不在断线时"沿用上一笔价格"——那是最危险的静默降级。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ... import timebase
from ...broker.base import Quote
from .base import MarketDataProvider, ProviderError, ProviderUnavailable
from .models import (
    DataQuality,
    EventAuthority,
    EventType,
    HaltEvent,
    LULDState,
    MWCBState,
    MarketEvent,
    OrderBookEvent,
    ProviderHealth,
    QualityFlag,
    QuoteEvent,
    SessionEvent,
    TradeEvent,
)


class FeedUnavailable(ProviderUnavailable):
    def __init__(self, message: str):
        super().__init__(message)


@dataclass
class SymbolState:
    symbol: str
    exchange: str = "unknown"
    last_trade: Optional[TradeEvent] = None
    last_quote: Optional[QuoteEvent] = None
    last_book: Optional[OrderBookEvent] = None
    session: Optional[SessionEvent] = None
    halt: Optional[HaltEvent] = None
    luld: Optional[LULDState] = None
    #: **事件时间**（市场发生变化的那一刻）——新鲜度必须按它算
    updated_at: Optional[datetime] = None
    #: 本地收到的时间（用于统计延迟，不用于新鲜度）
    received_at: Optional[datetime] = None
    version: int = 0

    @property
    def halted(self) -> bool:
        return bool(self.halt and self.halt.halted)

    def price(self) -> Optional[float]:
        if self.last_quote and self.last_quote.mid > 0:
            return self.last_quote.mid
        if self.last_trade:
            return float(self.last_trade.price)
        return None

    def as_dict(self) -> Dict[str, Any]:
        return {"symbol": self.symbol, "exchange": self.exchange,
                "price": self.price(), "halted": self.halted,
                "halt_reason": self.halt.reason if self.halt else "",
                "luld": self.luld.as_dict() if self.luld else None,
                "updated_at": self.updated_at.isoformat() if self.updated_at else None,
                "received_at": self.received_at.isoformat() if self.received_at else None,
                "version": self.version}


class ProviderFeed:
    """多 provider 汇聚。"""

    def __init__(self, providers: Iterable[MarketDataProvider], *,
                 max_age_seconds: float = 60.0,
                 authority_router: Any = None):
        self.providers: Dict[str, MarketDataProvider] = {p.name: p for p in providers}
        self.max_age = float(max_age_seconds)
        self.authority = authority_router
        self._states: Dict[Tuple[str, str], SymbolState] = {}
        self._mwcb: Optional[MWCBState] = None
        self._events: List[MarketEvent] = []
        self.dropped = 0
        self.accepted = 0
        self.rejected: Dict[str, int] = {}

    # ------------------------------------------------------------------ #
    def connect_all(self) -> None:
        for p in self.providers.values():
            p.connect()

    def close_all(self) -> None:
        for p in self.providers.values():
            try:
                p.close()
            except Exception:  # noqa: BLE001
                pass

    def subscribe(self, symbols: Iterable[str]) -> None:
        for p in self.providers.values():
            p.subscribe(symbols)

    # ------------------------------------------------------------------ #
    def poll(self) -> List[MarketEvent]:
        """拉取所有 provider 的事件并更新状态。"""
        out: List[MarketEvent] = []
        for p in self.providers.values():
            try:
                batch = p.poll()
            except Exception as exc:  # noqa: BLE001
                self._count_reject("provider_error")
                continue
            for ev in batch:
                if not ev.quality.usable:
                    self.dropped += 1
                    self._count_reject(",".join(f.value for f in ev.quality.flags))
                    continue
                self._ingest(ev)
                out.append(ev)
        self._events.extend(out)
        if len(self._events) > 2000:
            self._events = self._events[-2000:]
        return out

    def _count_reject(self, key: str) -> None:
        self.rejected[key] = self.rejected.get(key, 0) + 1

    def _ingest(self, ev: MarketEvent) -> None:
        if isinstance(ev, MWCBState):
            self._mwcb = ev
        key = (ev.symbol.upper(), ev.exchange.lower())
        st = self._states.get(key) or SymbolState(symbol=ev.symbol.upper(),
                                                  exchange=ev.exchange.lower())
        # **旧数据不得覆盖新数据**：事件时间早于已记录的最新时间 → 丢弃并计数
        if st.updated_at is not None and ev.event_time < st.updated_at:
            ev.quality.add(QualityFlag.OUT_OF_ORDER,
                           f"迟到事件 {ev.event_time.isoformat()} < "
                           f"{st.updated_at.isoformat()}")
            self.dropped += 1
            self._count_reject("late_event")
            return
        if isinstance(ev, TradeEvent):
            st.last_trade = ev
        elif isinstance(ev, QuoteEvent):
            st.last_quote = ev
        elif isinstance(ev, OrderBookEvent):
            st.last_book = ev
        elif isinstance(ev, SessionEvent):
            st.session = ev
        elif isinstance(ev, HaltEvent):
            # 只有官方事件才允许改停牌状态
            if self._can_authorize(ev, "halt"):
                st.halt = ev
            else:
                self._count_reject("halt_not_authoritative")
        elif isinstance(ev, LULDState):
            if self._can_authorize(ev, "luld"):
                st.luld = ev
            else:
                self._count_reject("luld_not_authoritative")
        # 关键：用 event_time 而不是 receive_time 判新鲜度。
        # 否则供应商把两小时前的数据"现在推过来"，就会被当成最新的。
        st.updated_at = ev.event_time
        st.received_at = ev.receive_time
        st.version += 1
        self._states[key] = st
        self.accepted += 1

    def _can_authorize(self, ev: MarketEvent, kind: str) -> bool:
        if self.authority is None:
            return False
        # 用 receive_time 作为"判定时刻"：回放时它就是事件时间线上的现在，
        # 否则历史样本会被判成"来自未来"。
        verdict = self.authority.can_apply(ev, symbol=ev.symbol,
                                          exchange=ev.exchange,
                                          now=ev.receive_time)
        return bool(verdict.accepted)

    # ------------------------------------------------------------------ #
    def state(self, symbol: str, exchange: str = "*") -> Optional[SymbolState]:
        sym = symbol.upper()
        if exchange and exchange != "*":
            return self._states.get((sym, exchange.lower()))
        candidates = [v for (s, _e), v in self._states.items() if s == sym]
        if not candidates:
            return None
        return max(candidates, key=lambda v: v.version)

    def states(self) -> List[SymbolState]:
        return list(self._states.values())

    def mwcb(self) -> Optional[MWCBState]:
        return self._mwcb

    # ------------------------------------------------------------------ #
    def quote(self, symbol: str, *, exchange: str = "*",
              max_age: Optional[float] = None) -> Quote:
        """给 QuoteGateway/Broker 用的报价。不可用直接抛错。"""
        st = self.state(symbol, exchange)
        if st is None:
            raise FeedUnavailable(f"{symbol} 无任何 provider 数据")
        age_limit = self.max_age if max_age is None else float(max_age)
        if st.updated_at is None:
            raise FeedUnavailable(f"{symbol} 无更新时间")
        age = (timebase.utcnow() - st.updated_at).total_seconds()
        if age > age_limit:
            raise FeedUnavailable(f"{symbol} 行情过期 {age:.0f}s > {age_limit:.0f}s")
        if st.halted:
            raise FeedUnavailable(
                f"{symbol} 处于停牌（{st.halt.reason or '未说明'}），拒绝出价")
        px = st.price()
        if not px or px <= 0:
            raise FeedUnavailable(f"{symbol} 无有效价格")
        prev = float(st.last_trade.price) if st.last_trade else px
        return Quote(symbol=symbol, market="us", last=float(px),
                     open=float(px), high=float(px), low=float(px),
                     prev_close=prev, volume=0.0,
                     ts=st.updated_at.astimezone(timezone.utc).isoformat(),
                     source=st.last_quote.provider if st.last_quote else "provider")

    # ------------------------------------------------------------------ #
    def health(self) -> Dict[str, Any]:
        hs = {name: p.health.as_dict() for name, p in self.providers.items()}
        connected = any(h["connected"] for h in hs.values())
        return {"providers": hs, "connected": connected,
                "accepted": self.accepted, "dropped": self.dropped,
                "rejected": dict(self.rejected),
                "symbols": len(self._states),
                "mwcb": self._mwcb.as_dict() if self._mwcb else None}

    def snapshot(self) -> Dict[str, Any]:
        return {"health": self.health(),
                "states": {f"{s}|{e}": v.as_dict() for (s, e), v in self._states.items()}}
