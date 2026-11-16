"""AI 层：统一模型接口、上下文与信号结构。"""
from .authority import (
    AuthorityDecision,
    AuthorityState,
    DecisionAuthority,
)
from .base import (
    AIModel,
    AIModelError,
    MarketContext,
    TradingSignal,
    UnavailableModel,
    VALID_ACTIONS,
)
from .providers import (
    HeuristicModel,
    OpenAICompatibleModel,
    build_model,
    extract_json,
    SYSTEM_PROMPT,
    build_user_prompt,
)

__all__ = [
    "AIModel", "AIModelError", "MarketContext", "TradingSignal", "UnavailableModel",
    "VALID_ACTIONS", "DecisionAuthority", "AuthorityDecision", "AuthorityState",
    "HeuristicModel", "OpenAICompatibleModel", "build_model", "extract_json",
    "SYSTEM_PROMPT", "build_user_prompt",
]
