"""宏观数据库 schema 定义与建表/迁移。

设计前提
--------
* 这是**独立库** ``data/macro_data.db``，与 ``market_us.db`` / ``audit.db``
  没有任何外键或 ATTACH 关系，绝不改动行情库。
* 表结构按"点时间"（point-time）设计：同一观测期间允许多个版本，
  版本由 ``vintage_date`` 区分，**永不覆盖**。
* 用 ``PRAGMA user_version`` 记录 schema 版本；迁移只做增量 ``ADD COLUMN``，
  不做破坏性重建。

为什么不用 ``executescript``
---------------------------
``sqlite3.executescript`` 会**先隐式 COMMIT** 再执行，于是"BEGIN ... 失败回滚"
根本不成立：一旦第 3 条 DDL 语法错误，前 2 张表已经落盘，留下半套 schema。
这里改成**逐语句 execute**，全部包在同一个显式事务里，任一条失败即整体回滚。
"""
from __future__ import annotations

import sqlite3
from typing import Iterable, List, Optional, Sequence

SCHEMA_VERSION = 1


#: 每一条都是独立的 DDL 语句（顺序有意义：先表后索引）。
DDL_STATEMENTS: List[str] = [
    """
    CREATE TABLE IF NOT EXISTS macro_series (
        series_id           TEXT PRIMARY KEY,
        title               TEXT,
        units               TEXT,
        frequency           TEXT,
        seasonal_adjustment TEXT,
        source              TEXT NOT NULL,
        metadata_updated_at TEXT NOT NULL,
        observation_start   TEXT,
        observation_end     TEXT,
        last_updated        TEXT,
        timezone            TEXT NOT NULL DEFAULT 'America/New_York',
        release_time_hint   TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS macro_observations (
        series_id          TEXT NOT NULL,
        observation_period TEXT NOT NULL,
        vintage_date       TEXT NOT NULL,
        realtime_start     TEXT,
        realtime_end       TEXT,
        value              REAL,
        is_initial_release INTEGER NOT NULL DEFAULT 0,
        source             TEXT NOT NULL,
        ingested_at        TEXT NOT NULL,
        release_date       TEXT,
        release_timestamp  TEXT,
        release_epoch      INTEGER,
        time_precision     TEXT NOT NULL DEFAULT 'unknown',
        verified           INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (series_id, observation_period, vintage_date)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_macro_obs_release
        ON macro_observations(series_id, release_epoch)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_macro_obs_period
        ON macro_observations(series_id, observation_period)
    """,
    """
    CREATE TABLE IF NOT EXISTS macro_releases (
        series_id          TEXT NOT NULL,
        observation_period TEXT NOT NULL,
        release_date       TEXT NOT NULL,
        release_timestamp  TEXT,
        release_epoch      INTEGER,
        timezone           TEXT NOT NULL DEFAULT 'America/New_York',
        release_type       TEXT NOT NULL,
        source             TEXT NOT NULL,
        time_precision     TEXT NOT NULL DEFAULT 'date',
        verified           INTEGER NOT NULL DEFAULT 0,
        notes              TEXT,
        PRIMARY KEY (series_id, observation_period, release_date)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_macro_releases_epoch
        ON macro_releases(series_id, release_epoch)
    """,
    """
    CREATE TABLE IF NOT EXISTS macro_raw_responses (
        request_id         TEXT PRIMARY KEY,
        endpoint           TEXT NOT NULL,
        request_parameters TEXT NOT NULL,
        response_hash      TEXT NOT NULL,
        response_path      TEXT NOT NULL,
        fetched_at         TEXT NOT NULL,
        http_status        INTEGER NOT NULL,
        bytes              INTEGER,
        row_count          INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS macro_ingest_log (
        task_id       TEXT NOT NULL,
        series_id     TEXT NOT NULL,
        started_at    TEXT NOT NULL,
        finished_at   TEXT,
        rows_inserted INTEGER NOT NULL DEFAULT 0,
        rows_updated  INTEGER NOT NULL DEFAULT 0,
        status        TEXT NOT NULL,
        error_message TEXT,
        PRIMARY KEY (task_id, series_id)
    )
    """,
]


class SchemaError(RuntimeError):
    pass


def ensure_schema(conn: sqlite3.Connection,
                  statements: Optional[Sequence[str]] = None) -> int:
    """建表（幂等）。返回迁移后的 schema 版本。

    整个迁移在一个事务里；任一条语句失败则**全部回滚**，不留半套表。
    ``statements`` 仅用于测试注入非法 DDL，生产调用不传。
    """
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if current == SCHEMA_VERSION:
        return current
    if current > SCHEMA_VERSION:
        raise SchemaError(
            f"数据库 schema 版本 {current} 高于代码支持的 {SCHEMA_VERSION}，拒绝降级运行")

    stmts: Iterable[str] = statements if statements is not None else DDL_STATEMENTS
    prev_isolation = conn.isolation_level
    try:
        conn.isolation_level = None            # 手动控制事务
        conn.execute("BEGIN IMMEDIATE")
        for stmt in stmts:
            conn.execute(stmt)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.execute("COMMIT")
    except Exception as exc:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise SchemaError(f"schema 迁移失败并已回滚: {type(exc).__name__}: {exc}") from exc
    finally:
        conn.isolation_level = prev_isolation
    return SCHEMA_VERSION


def integrity_check(conn: sqlite3.Connection) -> str:
    """``PRAGMA integrity_check`` 的原文（正常为 ``ok``）。"""
    return str(conn.execute("PRAGMA integrity_check").fetchone()[0])


def table_names(conn: sqlite3.Connection) -> List[str]:
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
