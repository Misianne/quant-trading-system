"""事件层：时间语义、版本回放与十段式解释。

与交易链路完全解耦：本包不导入 strategy / broker / risk，也不被它们导入。
它只回答两个问题——
1. 这条信息在某个历史时点**是否已经公开**（:meth:`EventRecord.visible_at`）；
2. 在没有一致预期的情况下，**哪些话不能说**（:func:`explain_event`）。
"""
from __future__ import annotations

from .model import (CHANNELS, DATE_ONLY, EXACT, RETRIEVED, TRUST_LEVELS, UNKNOWN,
                    EventError, EventRecord, EventVersion, explain_event, sha256_text)

__all__ = ["EventRecord", "EventVersion", "EventError", "explain_event",
           "sha256_text", "CHANNELS", "TRUST_LEVELS",
           "EXACT", "DATE_ONLY", "RETRIEVED", "UNKNOWN"]
