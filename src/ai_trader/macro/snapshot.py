"""历史时点快照接口 ``get_macro_snapshot(as_of_timestamp)``。

它是整个宏观库的**唯一读入口**，负责防止未来数据泄漏：

1. 只返回 ``release_epoch <= as_of`` 的行 —— 用 UTC 秒比较，**不比字符串**
   （不同偏移的 ISO 串按字典序比较会错，夏令时切换时必踩）；
2. 同一 ``(series, observation_period)`` 有多个版本时，只取**该时点已公开的最新版本**
   （``release_epoch`` 最大；同刻并列时取 ``vintage_date`` 更大者）；
3. 尚未发布的观测**直接不出现**（不是返回 None、不是返回当前值）；
4. ``time_precision != 'exact'`` 的行走 ``precision_warnings``，不伪装成精确时刻；
5. 纯函数：给定 ``as_of`` 可重复回放，结果与"什么时候查"无关；
6. 只读，永不 UPDATE/DELETE，历史初值不会被当前修订值覆盖。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from .store import MacroStore, MacroStoreError

#: 时间精度 → 给调用方的告警文案（**不伪装**成精确时刻）
_PRECISION_NOTE = {
    "date": "有 FRED vintage 日作依据，但只精确到日，已按当日 23:59:59 保守处理",
    "inferred": "无 vintage 依据，按“观测日（或次一工作日）收盘后可见”推定，"
                "仅日粒度，未经官方日历核验",
    "unknown": "发布时间未知，不可当作任何精确时刻使用",
}


@dataclass(frozen=True)
class SnapshotPoint:
    series_id: str
    observation_period: str
    value: Optional[float]
    vintage_date: str
    release_timestamp: Optional[str]
    release_epoch: Optional[int]
    time_precision: str
    verified: int
    is_initial_release: int
    age_seconds: Optional[int]

    def as_dict(self) -> Dict[str, Any]:
        return {"series_id": self.series_id,
                "observation_period": self.observation_period,
                "value": self.value, "vintage_date": self.vintage_date,
                "release_timestamp": self.release_timestamp,
                "time_precision": self.time_precision,
                "verified": bool(self.verified),
                "is_initial_release": bool(self.is_initial_release),
                "age_seconds": self.age_seconds}


@dataclass
class MacroSnapshot:
    as_of: str
    as_of_epoch: int
    points: List[SnapshotPoint] = field(default_factory=list)
    by_series: Dict[str, List[SnapshotPoint]] = field(default_factory=dict)
    missing_series: List[str] = field(default_factory=list)
    precision_warnings: List[Dict[str, Any]] = field(default_factory=list)
    #: 聚合用：按序列累计精度不足的观测数（不对外暴露，最终折叠进 precision_warnings）
    _precision_index: Dict[str, Dict[str, Any]] = field(default_factory=dict, repr=False)

    def latest(self, series_id: str) -> Optional[SnapshotPoint]:
        pts = self.by_series.get(series_id) or []
        return pts[-1] if pts else None

    def value(self, series_id: str) -> Optional[float]:
        p = self.latest(series_id)
        return p.value if p else None

    def as_dict(self) -> Dict[str, Any]:
        return {"as_of": self.as_of, "as_of_epoch": self.as_of_epoch,
                "series": {k: [p.as_dict() for p in v] for k, v in self.by_series.items()},
                "missing_series": self.missing_series,
                "precision_warnings": self.precision_warnings}


def _parse_as_of(as_of_timestamp: str) -> datetime:
    try:
        dt = datetime.fromisoformat(str(as_of_timestamp).replace("Z", "+00:00"))
    except ValueError as exc:
        raise MacroStoreError(f"as_of_timestamp 不是合法 ISO8601: {as_of_timestamp!r}") from exc
    if dt.tzinfo is None:
        raise MacroStoreError(
            f"as_of_timestamp 缺少时区偏移，拒绝猜测: {as_of_timestamp!r}")
    return dt.astimezone(timezone.utc)


def get_macro_snapshot(target, as_of_timestamp: str,
                       series: Optional[Sequence[str]] = None) -> MacroSnapshot:
    """按历史时点取快照。``target`` 可以是 MacroStore 或 db 路径。"""
    owns = False
    if isinstance(target, MacroStore):
        store = target
    else:
        store = MacroStore(target)
        owns = True
    try:
        dt = _parse_as_of(as_of_timestamp)
        as_of_epoch = int(dt.timestamp())
        rows = _query(store, as_of_epoch, series)
        snap = MacroSnapshot(as_of=dt.isoformat(), as_of_epoch=as_of_epoch)
        for r in rows:
            p = SnapshotPoint(
                series_id=r["series_id"],
                observation_period=r["observation_period"],
                value=r["value"], vintage_date=r["vintage_date"],
                release_timestamp=r["release_timestamp"],
                release_epoch=r["release_epoch"],
                time_precision=r["time_precision"] or "unknown",
                verified=int(r["verified"] or 0),
                is_initial_release=int(r["is_initial_release"] or 0),
                age_seconds=(as_of_epoch - r["release_epoch"])
                if r["release_epoch"] is not None else None)
            snap.points.append(p)
            snap.by_series.setdefault(p.series_id, []).append(p)
            if p.time_precision != "exact":
                _prec = snap._precision_index.setdefault(p.series_id, {
                    "series_id": p.series_id,
                    "time_precision": p.time_precision,
                    "points": 0,
                    "note": _PRECISION_NOTE.get(
                        p.time_precision, "发布时间精度未知，不可当作精确时刻"),
                })
                _prec["points"] += 1
        for pts in snap.by_series.values():
            pts.sort(key=lambda x: x.observation_period)
        snap.precision_warnings = sorted(snap._precision_index.values(),
                                         key=lambda d: d["series_id"])
        asked = list(series) if series else sorted(
            r["series_id"] for r in store.conn.execute(
                "SELECT DISTINCT series_id FROM macro_series"))
        snap.missing_series = [s for s in asked if s not in snap.by_series]
        return snap
    finally:
        if owns:
            store.close()


def _query(store: MacroStore, as_of_epoch: int,
           series: Optional[Sequence[str]]) -> List[Any]:
    sql = [
        "SELECT series_id, observation_period, value, vintage_date,",
        " release_timestamp, release_epoch, time_precision, verified,",
        " is_initial_release FROM (",
        "  SELECT *, ROW_NUMBER() OVER (",
        "    PARTITION BY series_id, observation_period",
        "    ORDER BY release_epoch DESC, vintage_date DESC) AS rn",
        "  FROM macro_observations",
        "  WHERE release_epoch IS NOT NULL AND release_epoch <= ?",
    ]
    args: List[Any] = [as_of_epoch]
    if series:
        sql.append(f"   AND series_id IN ({','.join('?' for _ in series)})")
        args.extend(series)
    sql.append(" ) WHERE rn = 1 ORDER BY series_id, observation_period")
    return store.conn.execute(" ".join(sql), args).fetchall()


def history(store: MacroStore, series_id: str, observation_period: str) -> List[Dict[str, Any]]:
    """某条观测的完整 vintage 链（审计用）。"""
    rows = store.conn.execute(
        "SELECT vintage_date, value, release_date, release_timestamp, time_precision,"
        " is_initial_release, verified FROM macro_observations"
        " WHERE series_id=? AND observation_period=? ORDER BY release_epoch",
        (series_id, observation_period)).fetchall()
    return [dict(r) for r in rows]
