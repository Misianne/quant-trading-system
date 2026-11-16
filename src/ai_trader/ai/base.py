"""统一 AI 模型接口。

任何模型（LLM、规则引擎、未来的 RL 策略）只要实现 :class:`AIModel`
就能被策略引擎调用，实现"模型可替换"。

关键约束
--------
* 模型只能输出**建议**（:class:`TradingSignal`），没有任何执行权。
* 信号必须自带止损；没有止损的一律被视为非法信号。
* 校验失败不抛异常到上层流程，而是返回 ``valid=False`` + ``errors``，
  由策略引擎交给风控模块判定 —— 保证"AI 不能绕过风控"。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional

VALID_ACTIONS = ("buy", "sell", "hold")


class AIModelError(Exception):
    """模型调用/解析失败。"""

    def __init__(self, message: str, code: str = "AI_ERROR", detail: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.detail = detail or {}


@dataclass
class MarketContext:
    """喂给模型的全部上下文（也可序列化后作为审计证据）。"""

    symbol: str
    market: str
    currency: str
    last_price: float
    candles: List[Any] = field(default_factory=list)
    indicators: Dict[str, Any] = field(default_factory=dict)
    position: Optional[Dict[str, Any]] = None
    equity: float = 0.0
    cash: float = 0.0
    max_position_pct: float = 0.15
    risk_budget: float = 0.0
    notes: List[str] = field(default_factory=list)

    def to_prompt_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "market": self.market,
            "currency": self.currency,
            "last_price": self.last_price,
            "position": self.position or {"qty": 0},
            "account": {
                "equity": round(self.equity, 2),
                "cash": round(self.cash, 2),
                "max_position_pct": self.max_position_pct,
                "risk_budget": round(self.risk_budget, 2),
            },
            "indicators": self.indicators,
        }


@dataclass
class TradingSignal:
    symbol: str
    action: str
    confidence: float
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    position_pct: float = 0.0
    horizon: str = "swing"
    rationale: str = ""
    market: str = ""
    provider: str = ""
    model: str = ""
    signal_id: str = ""
    ts: str = ""
    valid: bool = True
    errors: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.signal_id:
            self.signal_id = uuid.uuid4().hex[:16]
        if not self.ts:
            self.ts = datetime.now().astimezone().isoformat(timespec="seconds")
        self.action = str(self.action or "hold").strip().lower()
        try:
            self.confidence = float(self.confidence)
        except (TypeError, ValueError):
            self.confidence = 0.0
        for attr in ("entry_price", "stop_loss", "take_profit", "position_pct"):
            val = getattr(self, attr)
            if val is not None:
                try:
                    setattr(self, attr, float(val))
                except (TypeError, ValueError):
                    setattr(self, attr, None)

    # ------------------------------------------------------------------ #
    def validate(self, min_confidence: float = 0.60, require_stop_loss: bool = True) -> List[str]:
        """结构性校验。返回错误列表（空 = 通过）。"""
        errs: List[str] = []
        if self.action not in VALID_ACTIONS:
            errs.append(f"非法 action: {self.action}")
        if not (0.0 <= self.confidence <= 1.0):
            errs.append(f"confidence 越界: {self.confidence}")
        if self.action in ("buy", "sell") and self.confidence < min_confidence:
            errs.append(f"confidence {self.confidence:.2f} 低于阈值 {min_confidence:.2f}")
        if self.action in ("buy", "sell"):
            if self.entry_price is None or self.entry_price <= 0:
                errs.append("缺少有效 entry_price")
            if require_stop_loss and (self.stop_loss is None or self.stop_loss <= 0):
                errs.append("缺少 stop_loss（策略强制要求止损）")
            if self.action == "buy" and self.stop_loss and self.entry_price:
                if self.stop_loss >= self.entry_price:
                    errs.append("买入信号的 stop_loss 必须低于 entry_price")
                if self.take_profit and self.take_profit <= self.entry_price:
                    errs.append("买入信号的 take_profit 必须高于 entry_price")
            if self.action == "sell" and self.stop_loss and self.entry_price:
                if self.stop_loss <= self.entry_price:
                    errs.append("卖出信号的 stop_loss 必须高于 entry_price")
            if not (0.0 <= self.position_pct <= 1.0):
                errs.append(f"position_pct 越界: {self.position_pct}")
        self.errors = errs
        self.valid = not errs
        return errs

    def as_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("raw", None)
        return d

    @classmethod
    def hold(cls, symbol: str, reason: str, provider: str = "", model: str = "",
             market: str = "") -> "TradingSignal":
        return cls(symbol=symbol, action="hold", confidence=0.0, rationale=reason,
                   provider=provider, model=model, market=market)


class AIModel:
    """模型基类。子类必须实现 ``analyze``。"""

    name: str = "base"
    model_id: str = "base-v0"
    is_llm: bool = False
    available: bool = True
    #: 是否具备"开仓决策权"。
    #: 只有真正的大模型才允许为 True；规则引擎只能是 False ——
    #: 它可以算指标、给辅助判断，但不能代替 AI 决定开仓（审计 P1-9）。
    can_trade: bool = True

    def analyze(self, ctx: MarketContext) -> TradingSignal:
        raise NotImplementedError

    def health(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "model_id": self.model_id,
            "is_llm": self.is_llm,
            "available": self.available,
            "can_trade": self.can_trade,
            "status": "ready" if self.available else "unavailable",
        }


class UnavailableModel(AIModel):
    """占位模型：AI 配置了但不可用（无 Key / 构建失败）。

    它的存在就是为了让"降级"变成**显式的不可用**，而不是偷偷换成规则引擎。
    ``analyze`` 一律抛 ``AIModelError(AI_UNAVAILABLE)``，因此不可能产生任何信号。
    """

    name = "unavailable"
    model_id = "unavailable-v0"
    is_llm = True
    available = False
    can_trade = False

    def __init__(self, reason: str = "", provider: str = ""):
        self.reason = reason
        self.provider = provider

    def analyze(self, ctx: MarketContext) -> TradingSignal:
        raise AIModelError(
            f"AI 模型不可用，禁止产生开仓信号：{self.reason or '未配置'}"
            f"（provider={self.provider or 'unknown'}）",
            code="AI_UNAVAILABLE", detail={"reason": self.reason},
        )

    def health(self) -> Dict[str, Any]:
        return {
            "name": self.name, "model_id": self.model_id, "is_llm": True,
            "available": False, "can_trade": False, "status": "unavailable",
            "reason": self.reason, "provider": self.provider,
        }
