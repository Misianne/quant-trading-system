"""策略层。

注意：:mod:`ai_trader.ai.providers` 会引用 :mod:`ai_trader.strategy.indicators`，
而 :mod:`ai_trader.strategy.engine` 又引用 AI 层 —— 为避免循环导入，
本包 ``__init__`` 只做惰性导出（PEP 562）。
"""
from .indicators import (
    atr,
    atr_pct,
    bollinger,
    compute_snapshot,
    ema,
    max_drawdown,
    momentum,
    rsi,
    sma,
    snapshot_for_prompt,
)

__all__ = [
    "StrategyEngine", "ScanReport", "SymbolOutcome",
    "sma", "ema", "rsi", "atr", "atr_pct", "bollinger", "momentum",
    "max_drawdown", "compute_snapshot", "snapshot_for_prompt",
]


def __getattr__(name: str):
    if name in ("StrategyEngine", "ScanReport", "SymbolOutcome"):
        from . import engine as _engine

        return getattr(_engine, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
