"""Provider 接口与能力声明。

任何一个数据源适配器都必须：
1. 显式声明 **能力**（有没有 SIP、有没有盘前盘后、有没有官方停复牌事件）；
2. 走统一的 ``poll()/subscribe()`` 接口，不允许把供应商原始消息直接抛给上层；
3. 自己维护 ``ProviderHealth``（连接、重连、丢弃、缺口、重复）；
4. 断开时**明确标记不可用**，而不是继续返回最后一笔价格。

能力声明是"能不能做"的唯一依据：上层（QuoteGateway / PaperRuntime）
只信 ``capabilities``，不信供应商口头承诺，也不信字段名像不像。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

from ... import timebase
from .models import (
    DataQuality,
    EventAuthority,
    HaltEvent,
    LULDState,
    MarketEvent,
    MWCBState,
    OrderBookEvent,
    ProviderHealth,
    QualityFlag,
    QuoteEvent,
    SessionEvent,
    TradeEvent,
)


class ProviderError(RuntimeError):
    def __init__(self, message: str, code: str = "PROVIDER_ERROR"):
        super().__init__(f"[{code}] {message}")
        self.code = code


class ProviderNotConfigured(ProviderError):
    def __init__(self, message: str):
        super().__init__(message, "PROVIDER_NOT_CONFIGURED")


class ProviderUnavailable(ProviderError):
    def __init__(self, message: str):
        super().__init__(message, "PROVIDER_UNAVAILABLE")


@dataclass(frozen=True)
class ProviderCapabilities:
    """供应商能力声明。**未声明 = 不支持**（fail-closed）。"""

    provider: str
    #: 是否覆盖美股全市场
    us_full_market: bool = False
    #: 是否 SIP 合并行情（CTA + UTP）
    sip_consolidated: bool = False
    #: 覆盖的交易所代码（小写）
    exchanges: Sequence[str] = ()
    #: 实时 / 延迟秒数（0 = 实时）
    realtime: bool = False
    delay_seconds: int = 0
    #: 历史数据能力与回溯年限
    historical: bool = False
    history_years: float = 0.0
    backfill: bool = False
    #: 时段覆盖
    premarket: bool = False
    regular: bool = True
    afterhours: bool = False
    overnight: bool = False
    #: 数据类型
    trades: bool = False
    quotes: bool = False
    order_book: bool = False
    order_book_depth: int = 0
    #: 官方事件能力（**必须区分是否为交易所原生**）
    halt_events: bool = False
    halt_events_native: bool = False      # True = 交易所原生消息，不是供应商推断
    halt_events_nasdaq_listed_only: bool = False
    luld_events: bool = False
    luld_events_native: bool = False
    mwcb_events: bool = False
    mwcb_events_native: bool = False
    #: 事件元数据
    exchange_field: bool = False
    sequence_field: bool = False
    source_event_id: bool = False
    #: 是否需要额外交易所授权
    requires_exchange_license: bool = False
    notes: str = ""

    def as_dict(self) -> Dict[str, Any]:
        d = {k: getattr(self, k) for k in self.__dataclass_fields__}
        d["exchanges"] = list(self.exchanges)
        return d

    def supports_authoritative(self, kind: str) -> bool:
        """是否可用于**权威**事件（B/C/D）。"""
        if kind == "halt":
            return bool(self.halt_events and self.halt_events_native)
        if kind == "luld":
            return bool(self.luld_events and self.luld_events_native)
        if kind == "mwcb":
            return bool(self.mwcb_events and self.mwcb_events_native)
        return False


class MarketDataProvider:
    """行情供应商适配器基类。

    子类实现 ``_connect`` / ``_close`` / ``_poll_raw`` / ``_subscribe_raw``，
    基类负责健康统计、序号守卫、时间归一与"断线即不可用"。
    """

    name = "base"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = dict(config or {})
        self.connected = False
        self._subs: List[str] = []
        self.health = ProviderHealth(provider=self.name, connected=False,
                                     capabilities=self.capabilities().as_dict())
        self._seq = _SequenceTracker()
        #: 合法但"不是事件"的原始记录数（例如 QUOTING 状态）。
        #: 与 ``health.dropped`` 分开：丢弃是**故障**，跳过是**正常**。
        self.skipped = 0

    # ------------------------------------------------------------------ #
    def capabilities(self) -> ProviderCapabilities:      # pragma: no cover
        return ProviderCapabilities(provider=self.name)

    # ------------------------------------------------------------------ #
    def connect(self) -> None:
        if self.connected:
            return
        try:
            self._connect()
        except Exception as exc:  # noqa: BLE001
            self.health.connected = False
            self.health.last_error = f"{type(exc).__name__}: {exc}"
            raise ProviderUnavailable(f"{self.name} 连接失败：{exc}") from exc
        self.connected = True
        self.health.connected = True
        self.health.last_error = ""

    def close(self) -> None:
        try:
            self._close()
        finally:
            self.connected = False
            self.health.connected = False

    def reconnect(self) -> None:
        """断线重连：计数 + 重连后要求补齐（由调用方触发 backfill）。"""
        self.health.reconnects += 1
        try:
            self._close()
        except Exception:  # noqa: BLE001
            pass
        self.connected = False
        self.connect()

    # ------------------------------------------------------------------ #
    def subscribe(self, symbols: Iterable[str]) -> None:
        want = [str(s).upper() for s in symbols]
        if not want:
            raise ProviderError("订阅列表为空", "EMPTY_SUBSCRIPTION")
        self._subscribe_raw(want)
        for s in want:
            if s not in self._subs:
                self._subs.append(s)
        self.health.subscribed = len(self._subs)

    def unsubscribe(self, symbols: Iterable[str]) -> None:
        drop = {str(s).upper() for s in symbols}
        self._subs = [s for s in self._subs if s not in drop]
        self.health.subscribed = len(self._subs)
        self._unsubscribe_raw(sorted(drop))

    def subscriptions(self) -> List[str]:
        return list(self._subs)

    # ------------------------------------------------------------------ #
    def poll(self) -> List[MarketEvent]:
        """拉取一批事件（已归一化、已过质量标记）。"""
        if not self.connected:
            raise ProviderUnavailable(f"{self.name} 未连接（拒绝返回任何行情）")
        raw = self._poll_raw()
        out: List[MarketEvent] = []
        for item in raw:
            skipped_before = self.skipped
            ev = self._normalize(item)
            if ev is None:
                if self.skipped != skipped_before:
                    # 明确的"合法但不产生事件"（如非停牌的 status），不算故障
                    continue
                # 被丢弃的原始消息也要计数，否则"字段名对不上"会表现成
                # "这个供应商没有数据"，是最难查的一类静默失效。
                self.health.dropped += 1
                self._note_drop(item)
                continue
            self._apply_quality(ev, now=self._quality_now(ev))
            self.health.messages += 1
            self.health.last_message_at = ev.receive_time
            out.append(ev)
        return out

    # ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #
    def _reject(self, reason: str, detail: str = "") -> None:
        """记录一次"明确拒绝"（比静默 None 有用得多）。"""
        key = str(reason or "unknown")
        self.health.rejected[key] = int(self.health.rejected.get(key, 0)) + 1
        if detail:
            self.health.last_error = f"[{key}] {detail}"

    def _note_drop(self, item: Any) -> None:
        """子类可覆写，用于按原因归类丢弃。"""
        return None

    def rejections(self) -> Dict[str, int]:
        return dict(self.health.rejected)

    def _quality_now(self, ev: MarketEvent) -> Optional[datetime]:
        """质量判定的"当前时间"。

        实时 provider 用墙钟；**录制回放**必须用事件时间线本身，
        否则历史样本会被判成"过期两年"或"来自未来"。
        """
        return None

    def _apply_quality(self, ev: MarketEvent,
                       now: Optional[datetime] = None) -> None:
        sym, ex = ev.symbol, ev.exchange
        if ev.sequence:
            verdict = self._seq.check(sym, ex, ev.sequence)
            if verdict == "duplicate":
                ev.quality.add(QualityFlag.DUPLICATE)
                self.health.duplicate += 1
            elif verdict == "regression":
                ev.quality.add(QualityFlag.SEQUENCE_REGRESSION)
                self.health.out_of_order += 1
            elif verdict == "gap":
                ev.quality.add(QualityFlag.GAP, f"序号缺口：{self._seq.last_gap(sym, ex)}")
                self.health.gaps += 1
        now = now or timebase.utcnow()
        age = (now - ev.event_time).total_seconds()
        ev.quality.latency_ms = round(age * 1000.0, 3)
        if age > float(self.config.get("max_age_seconds", 60)):
            ev.quality.add(QualityFlag.STALE, f"事件滞后 {age:.0f}s")
        if age < -float(self.config.get("max_future_seconds", 5)):
            ev.quality.add(QualityFlag.FUTURE, f"事件来自未来 {-age:.0f}s")
        if ev.event_time.tzinfo is None:
            ev.quality.add(QualityFlag.NAIVE_TIME)

    # ------------------------------------------------------------------ #
    # 子类实现
    def _connect(self) -> None:                          # pragma: no cover
        return None

    def _close(self) -> None:                            # pragma: no cover
        return None

    def _poll_raw(self) -> List[Any]:                    # pragma: no cover
        return []

    def _subscribe_raw(self, symbols: List[str]) -> None:  # pragma: no cover
        return None

    def _unsubscribe_raw(self, symbols: List[str]) -> None:  # pragma: no cover
        return None

    def _normalize(self, item: Any) -> Optional[MarketEvent]:  # pragma: no cover
        raise NotImplementedError

    # ------------------------------------------------------------------ #
    def snapshot(self) -> Dict[str, Any]:
        return {"provider": self.name, "connected": self.connected,
                "subscriptions": list(self._subs),
                "health": self.health.as_dict(),
                "capabilities": self.capabilities().as_dict()}


# ---------------------------------------------------------------------- #
class _SequenceTracker:
    """按 (symbol, exchange) 隔离的序号守卫。

    为什么必须按交易所隔离：同一 symbol 在两家交易所各有自己的序号空间，
    混在一起会把正常事件误判成"序号回退"。
    """

    def __init__(self) -> None:
        self._last: Dict[tuple, int] = {}
        self._gaps: Dict[tuple, tuple] = {}

    def check(self, symbol: str, exchange: str, seq: int) -> str:
        key = (symbol.upper(), exchange.lower())
        prev = self._last.get(key)
        if prev is None:
            self._last[key] = seq
            return "first"
        if seq == prev:
            return "duplicate"
        if seq < prev:
            return "regression"
        if seq > prev + 1:
            self._gaps[key] = (prev, seq)
            self._last[key] = seq
            return "gap"
        self._last[key] = seq
        return "in_order"

    def last_gap(self, symbol: str, exchange: str) -> tuple:
        return self._gaps.get((symbol.upper(), exchange.lower()), (0, 0))

    def reset(self, symbol: Optional[str] = None) -> None:
        if symbol is None:
            self._last.clear()
            self._gaps.clear()
        else:
            for k in list(self._last):
                if k[0] == symbol.upper():
                    self._last.pop(k, None)
