"""回放用确定性信号模型。

为什么不用真实 LLM
------------------
回放验收要求「同一数据集 + 同一配置，结果必须确定性一致」。真实 LLM 做不到
（温度、版本、网络都在变），而且本轮明确禁止使用生产 API 密钥。

诚实声明
--------
本模型**不产生任何投资判断**。它是"剧本执行器"：由场景文件显式给出
「第 N 个 tick、某标的、买/卖、数量比例、止损」，它只负责把剧本翻译成
引擎能消费的 :class:`TradingSignal`。因此回放验证的是**执行链路**
（风控 → 门禁 → 撮合 → 入账 → 不变量），不是策略盈利能力。
这一点在报告里必须写清楚，不能拿回放收益当业绩。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from ..ai.base import AIModel, MarketContext, TradingSignal


@dataclass
class ScriptStep:
    """剧本中的一步。``tick`` 从 0 开始计数（每次 ``strip`` 推进一格）。"""

    tick: int
    symbol: str
    action: str = "buy"          # buy / sell / hold
    position_pct: float = 0.10
    stop_loss_pct: float = 0.05  # 相对当前价的止损距离
    take_profit_pct: float = 0.10
    confidence: float = 0.80
    rationale: str = ""
    #: 剧本显式给出的信号 ID（保证回放可复现；不由 uuid 生成）
    signal_id: str = ""

    def to_signal(self, price: float, ts: str) -> TradingSignal:
        # 卖出（含平仓）的止损必须**高于**入场价，买入的止损必须**低于**入场价
        # —— 与引擎的结构校验保持一致，否则信号会被判非法。
        is_sell = str(self.action).lower() == "sell"
        if is_sell:
            stop = round(price * (1 + self.stop_loss_pct), 4) if self.stop_loss_pct else None
            take = round(price * (1 - self.take_profit_pct), 4) if self.take_profit_pct else None
        else:
            stop = round(price * (1 - self.stop_loss_pct), 4) if self.stop_loss_pct else None
            take = round(price * (1 + self.take_profit_pct), 4) if self.take_profit_pct else None
        return TradingSignal(
            symbol=self.symbol.lower(), action=self.action,
            confidence=self.confidence, entry_price=price,
            stop_loss=stop, take_profit=take,
            position_pct=self.position_pct, horizon="intraday",
            rationale=self.rationale or f"replay-script@{self.tick}",
            provider="replay", model="scripted",
            signal_id=self.signal_id or f"REPLAY-{self.tick}-{self.symbol.upper()}-{self.action}",
            ts=ts,
        )


class ScriptedModel(AIModel):
    """按 tick 回放剧本的信号源。确定性：无随机、无网络、无时间依赖。"""

    name = "replay"
    model_id = "scripted-v1"
    is_llm = False
    available = True
    can_trade = True

    def __init__(self, steps: Sequence[ScriptStep] = (), *, default_action: str = "hold"):
        self.steps: List[ScriptStep] = list(steps)
        self.default_action = default_action
        self.tick = 0
        self.calls = 0
        self.emitted: List[str] = []
        self._by_key: Dict[tuple, ScriptStep] = {
            (s.tick, s.symbol.upper()): s for s in self.steps}

    # ------------------------------------------------------------------ #
    def set_tick(self, tick: int) -> None:
        self.tick = int(tick)

    def advance(self) -> int:
        self.tick += 1
        return self.tick

    def _ts(self) -> str:
        from .. import timebase
        return timebase.now().isoformat(timespec="seconds")

    def analyze(self, ctx: MarketContext) -> TradingSignal:
        self.calls += 1
        sym = str(ctx.symbol).upper()
        step = self._by_key.get((self.tick, sym))
        if step is None:
            return TradingSignal(
                symbol=sym.lower(), action=self.default_action, confidence=0.0,
                entry_price=float(ctx.last_price),
                rationale=f"no script for tick={self.tick}",
                provider="replay", model="scripted",
                signal_id=f"REPLAY-{self.tick}-{sym}-HOLD", ts=self._ts())
        sig = step.to_signal(float(ctx.last_price), self._ts())
        self.emitted.append(sig.signal_id)
        return sig

    def health(self) -> Dict[str, Any]:
        return {"name": self.name, "model": self.model_id, "available": True,
                "is_llm": False, "can_trade": True, "calls": self.calls,
                "tick": self.tick, "script_steps": len(self.steps)}

    def describe(self) -> Dict[str, Any]:
        return {"model": f"{self.name}/{self.model_id}", "steps": len(self.steps),
                "ticks": sorted({s.tick for s in self.steps})}
