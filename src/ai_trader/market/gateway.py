"""统一行情网关（US-P2-A 收口）。

为什么需要它
------------
`SessionPricePolicy` 上一轮只是"可选工具"——调用方不用就等于没有。
结果 `strategy/engine.py` 仍然直接 `broker.get_quote()`，盘前价照样能被当成
常规时段价用。本模块把**所有**行情读取收进一个闸门：

    Engine / Risk / Web / CLI  ──→  QuoteGateway  ──→  Broker.get_quote
                                        │
                                        ├─ 时区/时间戳校验（naive 直接拒）
                                        ├─ 时段识别（pre / regular / post / night）
                                        ├─ 过期 / 乱序 / 重复 / 跳变
                                        └─ 用途约束（regular_only 不接受盘前价）

行为开关
--------
``enabled=False``（默认，且本轮禁止打开）时**透传**：只做计数，不改变任何价格，
保证既有 P0/P1 行为与全部回归测试不受影响。

``enabled=True`` 时任何不合规行情都抛 :class:`QuoteUnavailable`，
由调用方按"安全降级"处理（Engine 的出场路径会退回券商持仓 mark 并写审计）。

**关键点：不论开关如何，Engine 都必须经过这里** —— 结构上不再存在旁路。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional, Tuple

from .. import timebase
from ..broker.base import Broker, BrokerError, Quote
from .clock import ExchangeClock, PHASE_REGULAR, TRADABLE_PHASES
from .sessions import (
    DEFAULT_MAX_AGE_SECONDS,
    QuoteError,
    QuoteNormalizer,
    QuoteUnavailable,
    SessionQuote,
)


def _parse_ts(raw: Any) -> Optional[datetime]:
    """把券商返回的时间戳解析成 aware datetime；失败返回 None。"""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else None
    text = str(raw).strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else None


@dataclass
class GatewayStats:
    total: int = 0
    passed: int = 0
    rejected: int = 0
    passthrough: int = 0
    reject_reasons: Dict[str, int] = None

    def __post_init__(self) -> None:
        if self.reject_reasons is None:
            self.reject_reasons = {}

    def as_dict(self) -> Dict[str, Any]:
        return {"total": self.total, "passed": self.passed,
                "rejected": self.rejected, "passthrough": self.passthrough,
                "reject_reasons": dict(self.reject_reasons)}


class QuoteGateway:
    """统一行情入口。**Engine 只允许通过它取价。**"""

    #: 用途 → 允许的时段
    USE_CASES: Dict[str, Tuple[str, ...]] = {
        "regular_only": (PHASE_REGULAR,),
        "any_tradable": TRADABLE_PHASES,
        "risk_exit": TRADABLE_PHASES,     # 退出允许扩展时段，但仍要求"新鲜"
        "display": TRADABLE_PHASES,       # 仅展示，不做交易决策
    }

    def __init__(self, broker: Broker, clocks: Dict[str, ExchangeClock], *,
                 enabled: bool = False,
                 max_age_seconds: Optional[Dict[str, float]] = None,
                 jump_limit_pct: float = 0.35,
                 now_fn=None,
                 provider: Any = None):
        self.broker = broker
        #: 可选的真实行情 provider（实现 ``quote(symbol) -> Quote``）。
        #: 为 None 时行为与既有版本完全一致（走 broker.get_quote）。
        #: 行情来源变了，但**准入校验（时段/新鲜度/跳变）一条都不少**。
        self.provider = provider
        self.clocks = dict(clocks or {})
        self.enabled = bool(enabled)
        self.stats = GatewayStats()
        #: 可注入的"当前时间"，用于测试与回放；生产走系统 UTC。
        self._now_fn = now_fn or (lambda: timebase.utcnow())
        self.max_age = dict(DEFAULT_MAX_AGE_SECONDS)
        if max_age_seconds:
            self.max_age.update(max_age_seconds)
        self.jump_limit_pct = float(jump_limit_pct)
        self._normalizer = QuoteNormalizer(clock=None, max_age_seconds=self.max_age,
                                           jump_limit_pct=self.jump_limit_pct)
        self._tags: Dict[str, SessionQuote] = {}

    # ------------------------------------------------------------------ #
    def _clock_for(self, symbol: str, market: str = "") -> Optional[ExchangeClock]:
        mkt = (market or "").lower()
        if mkt and mkt in self.clocks:
            return self.clocks[mkt]
        # 回退：唯一一个时钟（单市场部署最常见）
        if len(self.clocks) == 1:
            return next(iter(self.clocks.values()))
        return None

    def _build_session_quote(self, raw: Quote, clock: ExchangeClock) -> SessionQuote:
        dt = _parse_ts(getattr(raw, "ts", None))
        if dt is None:
            raise QuoteUnavailable(
                f"{raw.symbol} 行情时间戳缺失或非 ISO/带时区格式（ts={getattr(raw, 'ts', '')!r}）"
                f"—— 无法判定时段，拒绝使用")
        info = clock.session(dt)
        local = clock.local(dt)
        return SessionQuote(
            symbol=raw.symbol, price=float(raw.last), exchange_ts=dt,
            phase=info.phase, trading_day=local.date().isoformat(),
            source=getattr(raw, "source", "") or "", prev_close=float(getattr(raw, "prev_close", 0.0) or 0.0),
        )

    def _fetch(self, symbol: str):
        """取原始报价：优先 provider，其次 broker。"""
        if self.provider is not None:
            return self.provider.quote(symbol)
        return self.broker.get_quote(symbol)

    # ------------------------------------------------------------------ #
    def quote(self, symbol: str, use_case: str = "regular_only",
              now: Optional[datetime] = None, market: str = "") -> Quote:
        """取一笔可用于 ``use_case`` 的行情。

        不合规 → :class:`QuoteUnavailable`（调用方必须走安全降级，不得用旧价顶替）。
        """
        if use_case not in self.USE_CASES:
            raise ValueError(f"未知用途: {use_case}")
        self.stats.total += 1

        if not self.enabled:
            self.stats.passthrough += 1
            return self._fetch(symbol)

        clock = self._clock_for(symbol, market)
        if clock is None:
            self.stats.rejected += 1
            self.stats.reject_reasons["no_clock"] = self.stats.reject_reasons.get("no_clock", 0) + 1
            raise QuoteUnavailable(f"{symbol} 没有可用的交易所时钟，无法判定时段")

        try:
            raw = self._fetch(symbol)
        except BrokerError:
            raise
        except Exception as exc:
            self.stats.rejected += 1
            key = f"broker_error:{type(exc).__name__}"
            self.stats.reject_reasons[key] = self.stats.reject_reasons.get(key, 0) + 1
            raise QuoteUnavailable(f"{symbol} 取行情失败：{exc}") from exc

        try:
            sq = self._build_session_quote(raw, clock)
        except QuoteError as exc:
            self.stats.rejected += 1
            key = "invalid_quote"
            self.stats.reject_reasons[key] = self.stats.reject_reasons.get(key, 0) + 1
            raise QuoteUnavailable(f"{symbol} 行情不合规：{exc}") from exc

        now_dt = now or self._now_fn()
        ingest = self._normalizer.ingest(sq, now_dt)
        if not ingest.accepted:
            self.stats.rejected += 1
            key = ingest.reason.split("：")[0][:24]
            self.stats.reject_reasons[key] = self.stats.reject_reasons.get(key, 0) + 1
            raise QuoteUnavailable(f"{symbol} {ingest.reason}")

        allowed = self.USE_CASES[use_case]
        if sq.phase not in allowed:
            self.stats.rejected += 1
            key = "cross_session_misuse"
            self.stats.reject_reasons[key] = self.stats.reject_reasons.get(key, 0) + 1
            raise QuoteUnavailable(
                f"{symbol} 当前行情属于 {sq.phase} 时段，{use_case} 要求 "
                f"{'/'.join(allowed)}；禁止跨时段冒用")

        self._tags[symbol.upper()] = sq
        self.stats.passed += 1
        return raw

    # ------------------------------------------------------------------ #
    def price(self, symbol: str, use_case: str = "regular_only",
              now: Optional[datetime] = None, market: str = "") -> float:
        return float(self.quote(symbol, use_case, now, market).last)

    def tag(self, symbol: str) -> Optional[SessionQuote]:
        """最近一次通过校验的行情标签（用于审计/展示时段）。"""
        return self._tags.get(symbol.upper())

    def peek(self, symbol: str) -> Optional[SessionQuote]:
        return self._normalizer.last(symbol.upper())

    def snapshot(self) -> Dict[str, Any]:
        out = {"enabled": self.enabled, "stats": self.stats.as_dict(),
               "tracked": len(self._tags),
               "source": "provider" if self.provider is not None else "broker"}
        if self.provider is not None and hasattr(self.provider, "health"):
            try:
                out["provider_health"] = self.provider.health()
            except Exception as exc:  # noqa: BLE001
                out["provider_health"] = {"error": str(exc)}
        return out
