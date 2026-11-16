"""美股行情时段模型（P2-A）。

解决的问题
----------
`feed.py` 原先只提供"当前价"，没有"这段价格属于哪个交易时段"的概念。
后果是：盘前的一笔成交价被拿去当正常时段的参考价，止损/开仓判断全部失真。

本模块提供三件事，且**不产生任何行情数据**（没有数据源时一律判不可用）：

1. :class:`SessionQuote` —— 带交易所时间戳、时段、交易日、来源的行情帧。
2. :class:`QuoteNormalizer` —— 过期 / 重复 / 乱序 / 异常值的统一把关。
3. :class:`SessionPricePolicy` —— 按用途取价：要求"正常时段价"时，
   只有盘前价就**必须拒绝**，不允许降级冒用。

设计约束
--------
* 时间一律用**带时区的交易所时间**（``ExchangeClock.local``）；naive 输入直接拒绝。
* 交易日以**纽约当地日期**为准（跨日盘前属于下一个交易日，由 clock 决定）。
* 全部为纯内存对象，无网络、无文件写入。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .clock import (
    PHASE_CLOSED,
    PHASE_HOLIDAY,
    PHASE_NIGHT,
    PHASE_POST,
    PHASE_PRE,
    PHASE_REGULAR,
    PHASE_WEEKEND,
    TRADABLE_PHASES,
    ExchangeClock,
)


class QuoteError(ValueError):
    """行情帧不合法（结构/时区/字段）。"""


class QuoteUnavailable(RuntimeError):
    """没有可用行情 —— 明确表示"不可用"，调用方不得用旧价顶替。"""


#: 各时段允许的最大行情年龄（秒）。
#:
#: 盘前/盘后/夜盘流动性差、更新稀疏，给更宽的窗口；正常时段要求最严。
DEFAULT_MAX_AGE_SECONDS: Dict[str, float] = {
    PHASE_REGULAR: 10.0,
    PHASE_PRE: 60.0,
    PHASE_POST: 60.0,
    PHASE_NIGHT: 120.0,
}


@dataclass(frozen=True)
class SessionQuote:
    """一帧带时段语义的行情。

    ``exchange_ts`` 必须是**带 tzinfo 的 datetime**，且已换算到交易所时区。
    """

    symbol: str
    price: float
    exchange_ts: datetime
    phase: str
    trading_day: str            # ISO 日期（纽约当地）
    source: str = ""
    seq: int = 0
    prev_close: float = 0.0
    volume: float = 0.0

    def __post_init__(self) -> None:
        if not self.symbol:
            raise QuoteError("symbol 不能为空")
        if self.exchange_ts.tzinfo is None:
            raise QuoteError(
                "exchange_ts 必须是带时区的时间；naive 时间无法判断属于哪个时段")
        try:
            price = float(self.price)
        except (TypeError, ValueError):
            raise QuoteError(f"price 非法: {self.price!r}")
        if price <= 0:
            raise QuoteError(f"price 必须为正: {price}")
        if self.phase not in TRADABLE_PHASES and self.phase not in (
                PHASE_CLOSED, PHASE_WEEKEND, PHASE_HOLIDAY):
            raise QuoteError(f"未知 phase: {self.phase}")

    @property
    def is_regular(self) -> bool:
        return self.phase == PHASE_REGULAR

    @property
    def is_extended(self) -> bool:
        return self.phase in (PHASE_PRE, PHASE_POST, PHASE_NIGHT)

    def age_seconds(self, now: datetime) -> float:
        if now.tzinfo is None:
            raise QuoteError("now 必须带时区")
        return (now - self.exchange_ts).total_seconds()

    def as_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "price": self.price,
            "exchange_ts": self.exchange_ts.isoformat(),
            "phase": self.phase,
            "trading_day": self.trading_day,
            "source": self.source,
            "seq": self.seq,
        }


@dataclass
class IngestResult:
    """一帧行情的受理结论。"""

    accepted: bool
    reason: str = ""
    quote: Optional[SessionQuote] = None

    def as_dict(self) -> Dict[str, Any]:
        return {"accepted": self.accepted, "reason": self.reason,
                "quote": self.quote.as_dict() if self.quote else None}


class QuoteNormalizer:
    """行情帧的统一把关：过期 / 重复 / 乱序 / 异常跳变。

    刻意**不做**价格平滑或补值 —— 只判断"能不能用"，不能就明确拒绝。
    """

    def __init__(self, clock: ExchangeClock,
                 max_age_seconds: Optional[Dict[str, float]] = None,
                 jump_limit_pct: float = 0.35,
                 allow_jump_between_sessions: bool = True):
        self.clock = clock
        self.max_age = dict(DEFAULT_MAX_AGE_SECONDS)
        if max_age_seconds:
            self.max_age.update(max_age_seconds)
        self.jump_limit_pct = float(jump_limit_pct)
        self.allow_jump_between_sessions = bool(allow_jump_between_sessions)
        self._last: Dict[str, SessionQuote] = {}
        self._rejects: Dict[str, int] = {}

    # ------------------------------------------------------------------ #
    def ingest(self, quote: SessionQuote, now: Optional[datetime] = None) -> IngestResult:
        """受理或拒绝一帧行情。"""
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise QuoteError("now 必须带时区")

        age = quote.age_seconds(now)
        limit = self.max_age.get(quote.phase, self.max_age[PHASE_REGULAR])
        if age > limit:
            return self._reject(quote, f"行情过期：{age:.1f}s > {limit:.0f}s（phase={quote.phase}）")
        if age < -5.0:
            return self._reject(quote, f"行情时间来自未来：{-age:.1f}s")

        prev = self._last.get(quote.symbol)
        if prev is not None:
            if quote.seq and prev.seq and quote.seq < prev.seq:
                return self._reject(quote, f"乱序帧：seq {quote.seq} < {prev.seq}")
            if quote.seq and prev.seq and quote.seq == prev.seq and quote.price == prev.price:
                return self._reject(quote, "重复帧（同 seq 同价）")
            # 同一时段内的异常跳变：可能是脏数据
            if (not self.allow_jump_between_sessions
                    and prev.phase == quote.phase and prev.price > 0):
                move = abs(quote.price - prev.price) / prev.price
                if move > self.jump_limit_pct:
                    return self._reject(
                        quote, f"异常跳变：{move:.1%} > {self.jump_limit_pct:.0%}")

        self._last[quote.symbol] = quote
        return IngestResult(True, "", quote)

    def _reject(self, quote: SessionQuote, reason: str) -> IngestResult:
        self._rejects[quote.symbol] = self._rejects.get(quote.symbol, 0) + 1
        return IngestResult(False, reason)

    # ------------------------------------------------------------------ #
    def last(self, symbol: str) -> Optional[SessionQuote]:
        return self._last.get(symbol.lower()) or self._last.get(symbol)

    def stats(self) -> Dict[str, Any]:
        return {"tracked": len(self._last),
                "rejected_by_symbol": dict(self._rejects)}


class SessionPricePolicy:
    """按用途取价，并**拒绝跨时段冒用**。

    这是 P2-A 的核心约束：需要"正常时段价"时，只有盘前价就必须判不可用，
    不能用盘前价顶替 —— 那会让隔夜跳空被当成盘中波动。
    """

    #: 用途 → 允许的时段集合
    USE_CASES: Dict[str, Tuple[str, ...]] = {
        "regular_only": (PHASE_REGULAR,),
        "any_tradable": TRADABLE_PHASES,
        "risk_exit": TRADABLE_PHASES,      # 风险退出允许任何可交易时段
    }

    def __init__(self, normalizer: QuoteNormalizer,
                 max_age_seconds: Optional[Dict[str, float]] = None):
        self.normalizer = normalizer
        self.max_age = dict(DEFAULT_MAX_AGE_SECONDS)
        if max_age_seconds:
            self.max_age.update(max_age_seconds)

    def price(self, symbol: str, use_case: str = "regular_only",
              now: Optional[datetime] = None) -> SessionQuote:
        """返回可用的行情帧；不可用即抛 :class:`QuoteUnavailable`。"""
        if use_case not in self.USE_CASES:
            raise ValueError(f"未知用途: {use_case}")
        allowed = self.USE_CASES[use_case]

        q = self.normalizer.last(symbol)
        if q is None:
            raise QuoteUnavailable(f"{symbol} 无任何行情")
        if q.phase not in allowed:
            raise QuoteUnavailable(
                f"{symbol} 只有 {q.phase} 时段行情，{use_case} 要求 {'/'.join(allowed)}；"
                f"禁止跨时段冒用")
        now = now or datetime.now(timezone.utc)
        limit = self.max_age.get(q.phase, self.max_age[PHASE_REGULAR])
        if q.age_seconds(now) > limit:
            raise QuoteUnavailable(
                f"{symbol} 行情已过期（{q.age_seconds(now):.1f}s > {limit:.0f}s）")
        return q

    def try_price(self, symbol: str, use_case: str = "regular_only",
                  now: Optional[datetime] = None) -> Tuple[Optional[SessionQuote], str]:
        try:
            return self.price(symbol, use_case, now), ""
        except QuoteUnavailable as exc:
            return None, str(exc)


def phase_of(clock: ExchangeClock, ts: datetime) -> Tuple[str, str]:
    """返回 ``(phase, trading_day)``；ts 必须是带时区的交易所时间。"""
    if ts.tzinfo is None:
        raise QuoteError("ts 必须带时区")
    info = clock.session(ts)
    local = clock.local(ts)
    return info.phase, local.date().isoformat()
