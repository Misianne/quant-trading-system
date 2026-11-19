"""宏观数据层：独立库 ``data/macro_data.db`` 的 schema、存储、入库与点时间快照。

与交易主链**完全解耦**：本包不导入 ``strategy`` / ``broker`` / ``market.feed``，
也没有任何反向依赖。它只做三件事：

1. 以 ``(series_id, observation_period, vintage_date)`` 为键保存**每个版本**，
   永不覆盖；
2. 用 ``release_epoch``（UTC 秒）表达"这一版什么时候可见"；
3. 提供 :func:`ai_trader.macro.snapshot.get_macro_snapshot`，只返回某时点
   已公开的数据，用于回测时防止未来信息泄漏。
"""
from __future__ import annotations

from .schema import SCHEMA_VERSION, ensure_schema
from .snapshot import MacroSnapshot, SnapshotPoint, get_macro_snapshot, history
from .store import (DATE_ONLY, EXACT, UNKNOWN, MacroStore, MacroStoreError,
                    WriteResult, conservative_release_ts, epoch_of,
                    exact_release_ts, now_utc)

__all__ = [
    "SCHEMA_VERSION", "ensure_schema",
    "MacroStore", "MacroStoreError", "WriteResult",
    "conservative_release_ts", "exact_release_ts", "epoch_of", "now_utc",
    "DATE_ONLY", "EXACT", "UNKNOWN",
    "MacroSnapshot", "SnapshotPoint", "get_macro_snapshot", "history",
]
