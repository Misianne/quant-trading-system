"""把既有 WebSocket 行情（`MarketFeed`）接到 QuoteGateway，并定义来源优先级。

为什么是"桥"而不是"新引擎"
--------------------------
项目里已经有完整的 WS 实现：``ai_trader.market.feed.MarketFeed``
（认证头、订阅回执校验、心跳、重连、去重、乱序、过期丢弃都在里面）。
`broker/fosun_adapter.py` 还装了守卫，禁止使用官方 SDK 自带的 push 客户端，
所以**WS 路径只有一条**。

但 `MarketFeed` 目前只被引擎当作"新鲜度闸门"（`engine._feed_gate`），
它的 ``tick()`` 没有被任何价格通道消费——也就是说 **WS 行情没有进入
QuoteGateway**。本模块补的就是这一环，并且**不改** `feed.py` / `gateway.py`：

* :class:`WsQuoteBridge` 只暴露 ``quote(symbol)``，形状与
  `FosunProvider.quote()` 一致，因此可以直接喂给
  ``QuoteGateway(..., provider=bridge)``。
* :func:`should_replace` 定义"新旧/延迟"的取舍，防止 REST 延迟报价
  覆盖已经确认更新的 WS 实时推送。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

from ...broker.base import Quote

#: 本模块的行情来源标签
SOURCE_WS = "fosun-ws"
SOURCE_REST = "fosun-rest"


class WsQuoteUnavailable(RuntimeError):
    """WS 侧无法提供可用报价（未连接/无数据/过期）。"""


# ---------------------------------------------------------------------- #
# 来源优先级
# ---------------------------------------------------------------------- #
@dataclass(frozen=True)
class QuoteMark:
    """一条报价的"新旧与延迟"标记，用于比较。"""

    ts: Optional[datetime]
    delayed: bool
    source: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"ts": self.ts.isoformat() if self.ts else None,
                "delayed": self.delayed, "source": self.source}


def should_replace(current: Optional[QuoteMark],
                   incoming: QuoteMark) -> bool:
    """``incoming`` 是否应替换 ``current``。

    规则（宁可保留旧的，也不让错误的方向覆盖）：

    1. 没有旧值 → 接受
    2. 新值没有时间戳 → **拒绝**（无法判断新旧）
    3. 新值更旧 → 拒绝
    4. 时间戳相同：
       - 旧的是延迟、新的是实时 → 接受（升级）
       - 其余情况 → 拒绝（避免抖动，也避免延迟覆盖实时）
    5. 新值更新 → 接受
    """
    if current is None:
        return True
    if incoming.ts is None:
        return False
    if current.ts is None:
        return True
    if incoming.ts < current.ts:
        return False
    if incoming.ts == current.ts:
        return bool(current.delayed and not incoming.delayed)
    return True


def merge_reason(current: Optional[QuoteMark], incoming: QuoteMark) -> str:
    """给审计用的可读理由。"""
    if current is None:
        return "no_previous"
    if incoming.ts is None:
        return "incoming_without_timestamp"
    if current.ts is None:
        return "previous_without_timestamp"
    if incoming.ts < current.ts:
        return "older_than_current"
    if incoming.ts == current.ts:
        return "realtime_upgrade" if (current.delayed and not incoming.delayed) \
            else "same_timestamp_keep_current"
    return "newer"


# ---------------------------------------------------------------------- #
# WS → QuoteGateway 桥
# ---------------------------------------------------------------------- #
class WsQuoteBridge:
    """把 ``MarketFeed`` 的 QuoteTick 转成 ``broker.base.Quote``。

    只依赖鸭子类型（``tick()`` / ``is_fresh()``），因此测试可以用假 feed，
    也不会与 `feed.py` 产生循环依赖。
    """

    def __init__(self, feed: Any, *, market: str = "us",
                 max_age_seconds: Optional[float] = None,
                 now_fn=None):
        self.feed = feed
        self.market = market
        self.max_age_seconds = max_age_seconds
        self._now_fn = now_fn or (lambda: datetime.now().astimezone())

    # ------------------------------------------------------------------ #
    def quote(self, symbol: str) -> Quote:
        """给 QuoteGateway 用。不可用时抛 :class:`WsQuoteUnavailable`。"""
        if self.feed is None:
            raise WsQuoteUnavailable("WS feed 未注入")

        tick = None
        getter = getattr(self.feed, "tick", None)
        if callable(getter):
            tick = getter(symbol)
        if tick is None:
            raise WsQuoteUnavailable(f"{symbol} 无 WS 行情")

        now = self._now_fn()
        checker = getattr(self.feed, "is_fresh", None)
        if callable(checker):
            try:
                fresh, detail = checker(symbol, now)
            except TypeError:               # 老签名不接受 now
                fresh, detail = checker(symbol)
            if not fresh:
                raise WsQuoteUnavailable(f"{symbol} WS 行情不可用：{detail}")

        if self.max_age_seconds is not None:
            ts = getattr(tick, "ts", None)
            if ts is None:
                raise WsQuoteUnavailable(f"{symbol} WS 行情缺少时间戳")
            age = (now - ts).total_seconds()
            if age > float(self.max_age_seconds):
                raise WsQuoteUnavailable(
                    f"{symbol} WS 行情过期 {age:.0f}s > {self.max_age_seconds:.0f}s")

        last = float(getattr(tick, "last", 0.0) or 0.0)
        if last <= 0:
            raise WsQuoteUnavailable(f"{symbol} WS 行情价格无效：{last}")

        ts = getattr(tick, "ts", None)
        return Quote(
            symbol=str(symbol).upper(),
            market=self.market,
            last=last,
            # QuoteTick 只有 last/prev_close —— 没有 bid/ask，
            # 因此这里**不填**买一卖一，绝不拿 last 冒充。
            open=0.0, high=0.0, low=0.0,
            prev_close=float(getattr(tick, "prev_close", 0.0) or 0.0),
            volume=0.0,
            ts=ts.isoformat() if ts else "",
            source=SOURCE_WS,
        )

    # ------------------------------------------------------------------ #
    def mark(self, symbol: str) -> Optional[QuoteMark]:
        """当前 WS 报价的 Freshness 标记（给合并策略用）。"""
        getter = getattr(self.feed, "tick", None)
        tick = getter(symbol) if callable(getter) else None
        if tick is None:
            return None
        return QuoteMark(ts=getattr(tick, "ts", None), delayed=False,
                         source=SOURCE_WS)
