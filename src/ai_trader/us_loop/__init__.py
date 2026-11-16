"""美股模拟交易闭环（US Loop）。

与 ``US-P1 Batch 4.1.x`` 的安全修复**完全独立**：本包只新增文件，不修改
``log_redaction.py`` / ``config.py`` / ``runtime.py`` / ``fosun_adapter.py``。

模块
----
``bars``          真实日线的 SQLite 存储 + 数据质量校验（拒绝脏数据）
``fetch``         Yahoo 公开日线抓取（无需密钥，非券商生产接口）
``quote_source``  把历史 bar 包成 ``QuoteSource``，直接插进既有 ``PaperBroker``
``decisions``     真实 LLM 调用 + 结构化决策落库（缺 Key 时明确 blocked，不降级）
``runner``        组织"行情 → AI → 风控 → 模拟成交 → 账本 → 重启恢复"的运行器
"""

from .bars import BarDataError, BarStore, IngestResult, validate_bar
from .decisions import (AIDecision, AIGateway, AuditedModel, DecisionLedger,
                        PROVENANCE_LLM, PROVENANCE_UNAVAILABLE, build_llm)
from .quote_source import HistoricalQuoteSource, NoMarketData
from .runner import (DEFAULT_DECISION_TIME, US_SYMBOLS, LoopSettings, PaperLoop,
                     decision_moment, restart_proof)

__all__ = [
    "BarStore", "BarDataError", "IngestResult", "validate_bar",
    "AIDecision", "AIGateway", "AuditedModel", "DecisionLedger",
    "PROVENANCE_LLM", "PROVENANCE_UNAVAILABLE", "build_llm",
    "HistoricalQuoteSource", "NoMarketData",
    "LoopSettings", "PaperLoop", "restart_proof", "decision_moment",
    "US_SYMBOLS", "DEFAULT_DECISION_TIME",
]
