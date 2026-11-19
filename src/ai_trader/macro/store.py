"""宏观数据存储层：幂等写入 + 发布时序 + 原始响应归档。

三条铁律
--------
1. **永不覆盖历史版本**：主键含 ``vintage_date``，不同版本是不同行；
   重复导入同一版本只在内容确实变化时才 UPDATE，并单独计数。
2. **发布时序必须显式**：每一行都带 ``release_timestamp`` 与 ``time_precision``。
   FRED 只给到"日期"，因此默认按**当日 23:59:59（America/New_York）**作为
   可得时点——这是**保守**选择：宁可晚一天可见，也绝不提前泄露。
3. **原始响应必须可追溯**：每次请求落盘 + 存 hash + 参数（已剔除 api_key）。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from .schema import ensure_schema

NY = ZoneInfo("America/New_York")
DATE_ONLY = "date"
EXACT = "exact"
#: 无 vintage 依据、按"市场收盘即可见"规则**推定**的发布时序。
#: 与 ``date``（有 FRED vintage 日作依据）严格区分，快照会单独告警。
INFERRED = "inferred"
UNKNOWN = "unknown"


class MacroStoreError(RuntimeError):
    pass


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def epoch_of(iso_ts: Optional[str]) -> Optional[int]:
    """ISO8601（可带偏移）→ UTC 秒。**跨时区比较必须走这个**，不能比字符串。"""
    if not iso_ts:
        return None
    dt = datetime.fromisoformat(iso_ts)
    if dt.tzinfo is None:
        raise MacroStoreError(f"时间戳缺少时区，拒绝猜测: {iso_ts!r}")
    return int(dt.astimezone(timezone.utc).timestamp())


def conservative_release_ts(release_date: str, tz: str = "America/New_York") -> str:
    """把"只知道日期"的发布时间转成**保守可得时点**。

    取该时区当日 23:59:59，夏令时偏移由 ``zoneinfo`` 自动处理
    （例如 3 月第二个周日前后会从 -05:00 变 -04:00）。
    """
    d = date.fromisoformat(release_date)
    zone = ZoneInfo(tz) if tz else NY
    dt = datetime.combine(d, time(23, 59, 59), tzinfo=zone)
    return dt.isoformat()


def exact_release_ts(release_date: str, hhmm: str, tz: str = "America/New_York") -> str:
    """已知官方时刻时使用（本轮未做官方日历核验，谨慎使用）。"""
    h, m = (int(x) for x in hhmm.split(":"))
    d = date.fromisoformat(release_date)
    dt = datetime.combine(d, time(h, m), tzinfo=ZoneInfo(tz))
    return dt.isoformat()


@dataclass
class WriteResult:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0

    def as_dict(self) -> Dict[str, int]:
        return {"inserted": self.inserted, "updated": self.updated,
                "unchanged": self.unchanged}


class MacroStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        ensure_schema(self._conn)
        self.schema_version = int(
            self._conn.execute("PRAGMA user_version").fetchone()[0])

    # ------------------------------------------------------------------ #
    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    # ------------------------------------------------------------------ #
    def upsert_series(self, row: Dict[str, Any]) -> bool:
        sid = str(row["series_id"])
        cur = self._conn.execute(
            "SELECT title, units, frequency, seasonal_adjustment, observation_start,"
            " observation_end, last_updated FROM macro_series WHERE series_id=?",
            (sid,)).fetchone()
        vals = (row.get("title"), row.get("units"), row.get("frequency"),
                row.get("seasonal_adjustment"), row.get("observation_start"),
                row.get("observation_end"), row.get("last_updated"))
        if cur is not None and tuple(cur) == vals:
            return False
        self._conn.execute(
            "INSERT INTO macro_series(series_id,title,units,frequency,seasonal_adjustment,"
            "source,metadata_updated_at,observation_start,observation_end,last_updated,"
            "timezone,release_time_hint) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(series_id) DO UPDATE SET title=excluded.title, units=excluded.units,"
            " frequency=excluded.frequency, seasonal_adjustment=excluded.seasonal_adjustment,"
            " metadata_updated_at=excluded.metadata_updated_at,"
            " observation_start=excluded.observation_start,"
            " observation_end=excluded.observation_end, last_updated=excluded.last_updated,"
            " timezone=excluded.timezone, release_time_hint=excluded.release_time_hint",
            (sid, row.get("title"), row.get("units"), row.get("frequency"),
             row.get("seasonal_adjustment"), row.get("source", "FRED"), now_utc(),
             row.get("observation_start"), row.get("observation_end"),
             row.get("last_updated"), row.get("timezone", "America/New_York"),
             row.get("release_time_hint")))
        self._conn.commit()
        return True

    # ------------------------------------------------------------------ #
    def upsert_observations(self, rows: Iterable[Dict[str, Any]]) -> WriteResult:
        res = WriteResult()
        for r in rows:
            key = (r["series_id"], r["observation_period"], r["vintage_date"])
            old = self._conn.execute(
                "SELECT value, release_timestamp, time_precision, is_initial_release"
                " FROM macro_observations WHERE series_id=? AND observation_period=?"
                " AND vintage_date=?", key).fetchone()
            payload = (r.get("realtime_start"), r.get("realtime_end"),
                       float(r["value"]) if r.get("value") is not None else None,
                       int(r.get("is_initial_release", 0)), r.get("source", "FRED"),
                       r.get("ingested_at") or now_utc(), r.get("release_date"),
                       r.get("release_timestamp"),
                       epoch_of(r.get("release_timestamp")),
                       r.get("time_precision", UNKNOWN), int(r.get("verified", 0)))
            if old is None:
                self._conn.execute(
                    "INSERT INTO macro_observations(series_id,observation_period,vintage_date,"
                    "realtime_start,realtime_end,value,is_initial_release,source,ingested_at,"
                    "release_date,release_timestamp,release_epoch,time_precision,verified) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", key[:3] + payload)
                res.inserted += 1
            else:
                same = (old["value"], old["release_timestamp"], old["time_precision"],
                        old["is_initial_release"]) == (payload[2], payload[7], payload[9], payload[3])
                if same:
                    res.unchanged += 1
                    continue
                self._conn.execute(
                    "UPDATE macro_observations SET realtime_start=?, realtime_end=?, value=?,"
                    " is_initial_release=?, source=?, ingested_at=?, release_date=?,"
                    " release_timestamp=?, release_epoch=?, time_precision=?, verified=?"
                    " WHERE series_id=? AND observation_period=? AND vintage_date=?",
                    payload + key)
                res.updated += 1
        self._conn.commit()
        return res

    # ------------------------------------------------------------------ #
    def upsert_releases(self, rows: Iterable[Dict[str, Any]]) -> WriteResult:
        res = WriteResult()
        for r in rows:
            key = (r["series_id"], r["observation_period"], r["release_date"])
            old = self._conn.execute(
                "SELECT release_timestamp, release_type, time_precision"
                " FROM macro_releases WHERE series_id=? AND observation_period=?"
                " AND release_date=?", key).fetchone()
            vals = (r.get("release_timestamp"), epoch_of(r.get("release_timestamp")),
                    r.get("timezone", "America/New_York"),
                    r.get("release_type", "initial"), r.get("source", "FRED"),
                    r.get("time_precision", DATE_ONLY), int(r.get("verified", 0)),
                    r.get("notes"))
            if old is None:
                self._conn.execute(
                    "INSERT INTO macro_releases(series_id,observation_period,release_date,"
                    "release_timestamp,release_epoch,timezone,release_type,source,"
                    "time_precision,verified,notes) VALUES(?,?,?,?,?,?,?,?,?,?,?)", key + vals)
                res.inserted += 1
            elif (old["release_timestamp"], old["release_type"], old["time_precision"]) == (vals[0], vals[3], vals[5]):
                res.unchanged += 1
            else:
                self._conn.execute(
                    "UPDATE macro_releases SET release_timestamp=?, release_epoch=?, timezone=?,"
                    " release_type=?, source=?, time_precision=?, verified=?, notes=?"
                    " WHERE series_id=? AND observation_period=? AND release_date=?", vals + key)
                res.updated += 1
        self._conn.commit()
        return res

    # ------------------------------------------------------------------ #
    def archive_raw(self, *, endpoint: str, params: Dict[str, Any], body: bytes,
                    http_status: int, raw_dir: str | Path,
                    row_count: Optional[int] = None) -> str:
        """落盘原始响应 + 记录 hash（参数里不得有 api_key）。"""
        safe_params = {k: v for k, v in params.items() if k.lower() != "api_key"}
        digest = hashlib.sha256(body).hexdigest()
        req_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{digest[:10]}"
        d = Path(raw_dir)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{req_id}.json"
        path.write_bytes(body)
        self._conn.execute(
            "INSERT OR REPLACE INTO macro_raw_responses(request_id,endpoint,request_parameters,"
            "response_hash,response_path,fetched_at,http_status,bytes,row_count)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (req_id, endpoint, json.dumps(safe_params, ensure_ascii=False, sort_keys=True),
             digest, str(path), now_utc(), int(http_status), len(body), row_count))
        self._conn.commit()
        if "api_key" in json.dumps(safe_params).lower():
            raise MacroStoreError("request_parameters 里出现 api_key，拒绝入库")
        return req_id

    # ------------------------------------------------------------------ #
    def start_ingest(self, task_id: str, series_id: str) -> None:
        self._conn.execute(
            "INSERT INTO macro_ingest_log(task_id,series_id,started_at,status)"
            " VALUES(?,?,?,'running') ON CONFLICT(task_id,series_id) DO UPDATE SET"
            " started_at=excluded.started_at, status='running', finished_at=NULL,"
            " rows_inserted=0, rows_updated=0, error_message=NULL",
            (task_id, series_id, now_utc()))
        self._conn.commit()

    def finish_ingest(self, task_id: str, series_id: str, result: WriteResult,
                      status: str = "ok", error: str = "") -> None:
        self._conn.execute(
            "UPDATE macro_ingest_log SET finished_at=?, rows_inserted=?, rows_updated=?,"
            " status=?, error_message=? WHERE task_id=? AND series_id=?",
            (now_utc(), result.inserted, result.updated, status, error or None,
             task_id, series_id))
        self._conn.commit()

    # ------------------------------------------------------------------ #
    def counts(self) -> Dict[str, int]:
        out = {}
        for t in ("macro_series", "macro_observations", "macro_releases",
                  "macro_raw_responses", "macro_ingest_log"):
            out[t] = int(self._conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
        return out
