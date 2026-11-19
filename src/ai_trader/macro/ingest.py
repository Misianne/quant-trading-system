"""FRED → ``data/macro_data.db`` 的幂等入库。

取数口径（实测选定，不是猜的）
------------------------------
FRED 的四种 ``output_type`` 在**长历史**上的体积差异极大，本模块的选择依据是
2026-11-19 的实测：

===========  ==================================  ==========  =========
output_type  含义                                实测体积      实测行数
===========  ==================================  ==========  =========
1            按 realtime 期间返回观测             324 KB      3,364
2            按 vintage 返回全部观测               —           未用
3            按 vintage 返回**新增/修订**观测      116 KB        958
4            仅首次公布                           小           仅初值
===========  ==================================  ==========  =========

但 ``output_type=1`` 有一个致命行为：**分片窗口会把整条历史重发一遍**。
实测 VIXCLS 取 ``2026-01-05..2026-01-20`` 这 12 个 vintage，返回了 **9,406 行**
（1990 年以来每一天在该时点的取值）。日频序列有 4,000+ 个 vintage，逐片重发
整条历史会把请求量和库体积放大两个数量级。

因此本模块用 ``output_type=3``：它只返回**该 vintage 相对前一版发生变化**的观测，
是重建点时间序列的最小充分表示（同样的 12 个 vintage 只有 12 行 / 850 字节）。

返回是**宽表**：``{"date": "2026-01-05", "VIXCLS_20260105": "14.9"}``，
列名形如 ``<SERIES>_<YYYYMMDD>``，其中日期就是该版本的 vintage 日。
``"."`` 表示"该 vintage 未改动此观测"，必须跳过而不是当成 0。

分片
----
FRED 单个 realtime 窗口内的 vintage 数上限是 **2000**（实测超限返回
``There are 3975 vintage dates in the specified real-time period``）。因此按
``CHUNK=1500`` 切片，每片内列数受控。

发布时序
--------
``vintage_date`` 是**该版本在 FRED 生效的日期**。FRED 不提供时刻，所以
``release_timestamp`` 取当日 23:59:59（America/New_York）——**保守到宁可晚一天
可见，也绝不提前泄露**，并把 ``time_precision='date'``、``verified=0`` 如实写出。
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .store import (DATE_ONLY, INFERRED, MacroStore, WriteResult,
                    conservative_release_ts, now_utc)

BASE = "https://api.stlouisfed.org/fred"
#: 实测：日频序列 1000 / 1500 列的窗口稳定 504（网关超时），600 列稳定 200。
#: 因此默认 600，并在遇到 5xx 时**二分递归**而不是直接失败。
CHUNK = 600
VINTAGE_CAP = 2000
MIN_SPLIT = 25

#: 列名后缀：``<SERIES>_<YYYYMMDD>``
_COL_RE = re.compile(r"_(\d{8})$")


class FetchError(RuntimeError):
    pass


@dataclass
class SeriesTask:
    series_id: str
    timezone: str = "America/New_York"
    release_time_hint: Optional[str] = None
    #: ``vintages`` = 用 FRED vintage 矩阵重建版本链（有真实生效日）
    #: ``market_daily`` = 日频市场序列，取全历史、发布时序**推定**
    mode: str = "vintages"
    #: market_daily 模式下，观测日到"可见"之间要推后几个工作日（0 或 1）
    lag_weekdays: int = 0


class FredClient:
    def __init__(self, key: str, min_interval: float = 0.55, max_retries: int = 3):
        self._key = key
        self.min_interval = float(min_interval)
        self.max_retries = int(max_retries)
        self._last = 0.0
        self.calls = 0
        self.retry_429 = 0

    def get(self, path: str, **params) -> Tuple[int, bytes, Dict[str, Any]]:
        params = {k: v for k, v in params.items() if v is not None}
        params["api_key"] = self._key
        params.setdefault("file_type", "json")
        safe = {k: v for k, v in params.items() if k != "api_key"}
        url = f"{BASE}/{path}?{urllib.parse.urlencode(params)}"
        status, body = 0, b""
        for attempt in range(1, self.max_retries + 2):
            gap = time.monotonic() - self._last
            if gap < self.min_interval:
                time.sleep(self.min_interval - gap)
            self._last = time.monotonic()
            self.calls += 1
            status, body = self._once(url)
            if status == 429 and attempt <= self.max_retries:
                self.retry_429 += 1
                time.sleep(20.0)
                continue
            return status, body, safe
        return status, body, safe

    def _once(self, url: str) -> Tuple[int, bytes]:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0",
            "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except Exception as exc:  # noqa: BLE001
            raise FetchError(f"{type(exc).__name__}: {exc}") from exc


def _json(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return {}


# --------------------------------------------------------------------------- #
# 取数
# --------------------------------------------------------------------------- #
def fetch_series_meta(cli: FredClient, sid: str, store: MacroStore,
                      raw_dir: Path) -> Dict[str, Any]:
    st, body, params = cli.get("series", series_id=sid)
    payload = _json(body)
    rows = payload.get("seriess") if isinstance(payload, dict) else None
    store.archive_raw(endpoint="/series", params=params, body=body,
                      http_status=st, raw_dir=raw_dir,
                      row_count=len(rows or []) if isinstance(rows, list) else None)
    if st != 200 or not isinstance(rows, list) or not rows:
        raise FetchError(f"/series HTTP {st} 或结构异常: {str(payload)[:160]}")
    s = rows[0]
    return {"series_id": sid, "title": s.get("title"), "units": s.get("units"),
            "frequency": s.get("frequency"),
            "seasonal_adjustment": s.get("seasonal_adjustment"),
            "observation_start": s.get("observation_start"),
            "observation_end": s.get("observation_end"),
            "last_updated": s.get("last_updated"), "source": "FRED"}


def fetch_vintage_dates(cli: FredClient, sid: str, store: MacroStore,
                        raw_dir: Path) -> List[str]:
    st, body, params = cli.get("series/vintagedates", series_id=sid,
                               limit=10000, sort_order="asc")
    payload = _json(body)
    store.archive_raw(endpoint="/series/vintagedates", params=params, body=body,
                      http_status=st, raw_dir=raw_dir)
    if st != 200 or not isinstance(payload, dict):
        raise FetchError(f"/series/vintagedates HTTP {st}")
    raw = payload.get("vintage_dates")
    if not isinstance(raw, list):
        raise FetchError("vintage_dates 不是数组")
    out = [x for x in raw if isinstance(x, str)]
    if len(out) != len(raw):
        raise FetchError(f"vintage_dates 有 {len(raw) - len(out)} 个非字符串元素")
    return out


def plan_windows(vintages: Sequence[str], chunk: int = CHUNK) -> List[List[str]]:
    """按 vintage 数量切片，保证每片的列数受控。

    返回**vintage 分组**（不是 (start,end) 对），因为 5xx 二分重试需要知道
    窗口内到底有哪些 vintage，否则无法安全地砍半。
    """
    if not vintages:
        return []
    if len(vintages) <= chunk:
        return [list(vintages)]
    return [list(vintages[i:i + chunk]) for i in range(0, len(vintages), chunk)]


def parse_wide_row(row: Dict[str, Any], sid: str) -> List[Tuple[str, str, float]]:
    """把 ``{"date": ..., "SID_YYYYMMDD": "12.3"}`` 拆成 ``[(date, vintage, value)]``。

    ``"."`` / 空串 = 该 vintage 未改动此观测 → **跳过**（不是 0）。
    列名后缀解析失败 → 抛错，不静默丢弃（否则会悄悄少掉一整个版本）。
    """
    d = row.get("date")
    if not isinstance(d, str) or not d:
        return []
    out: List[Tuple[str, str, float]] = []
    for key, val in row.items():
        if key == "date":
            continue
        m = _COL_RE.search(str(key))
        if not m:
            raise FetchError(f"无法从列名解析 vintage: {key!r}（series={sid}）")
        ymd = m.group(1)
        vintage = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}"
        if val in (".", "", None):
            continue
        try:
            out.append((d, vintage, float(val)))
        except (TypeError, ValueError):
            continue
    return out


def fetch_chunk(cli: FredClient, sid: str, start: str, end: str,
                store: MacroStore, raw_dir: Path,
                vintages: Optional[Sequence[str]] = None) -> List[Tuple[str, str, float]]:
    """取一个 vintage 窗口的「新增/修订」观测（``output_type=3``）。

    5xx（实测 1000 列以上会 504）不是数据错误，而是网关超时：此时按
    ``vintages`` 二分后递归请求，直到能返回或切到 ``MIN_SPLIT`` 以下。
    """
    st, body, params = cli.get("series/observations", series_id=sid, output_type=3,
                               realtime_start=start, realtime_end=end)
    payload = _json(body)
    obs = payload.get("observations") if isinstance(payload, dict) else None
    store.archive_raw(endpoint="/series/observations", params=params, body=body,
                      http_status=st, raw_dir=raw_dir,
                      row_count=len(obs or []) if isinstance(obs, list) else None)
    if st >= 500 and vintages and len(vintages) > MIN_SPLIT:
        mid = len(vintages) // 2
        head = fetch_chunk(cli, sid, vintages[0], vintages[mid - 1], store, raw_dir,
                           vintages[:mid])
        tail = fetch_chunk(cli, sid, vintages[mid], vintages[-1], store, raw_dir,
                           vintages[mid:])
        return head + tail
    if st != 200 or not isinstance(obs, list):
        msg = payload.get("error_message") if isinstance(payload, dict) else str(payload)[:160]
        raise FetchError(f"/series/observations HTTP {st}: {msg} "
                         f"(window {start}..{end}, vintages={len(vintages or [])})")
    points: List[Tuple[str, str, float]] = []
    for row in obs:
        if isinstance(row, dict):
            points.extend(parse_wide_row(row, sid))
    return points


# --------------------------------------------------------------------------- #
# 规范化
# --------------------------------------------------------------------------- #
def canonicalize(points: Iterable[Tuple[str, str, float]]
                 ) -> Dict[str, List[Tuple[str, float]]]:
    """按观测期间聚合，去重并按 vintage 排序，**消除同值连续版本**。

    为什么必须消重：分片请求时，FRED 可能把"窗口起点时仍然有效的老值"也放进
    第一列，于是同一个值会在两个相邻 vintage 上各出现一次。同值连续版本对
    快照结果没有影响（值一样），但会在 ``macro_observations`` 里制造伪修订，
    让"修订次数"这个指标失真。
    """
    per: Dict[str, Dict[str, float]] = {}
    for d, v, val in points:
        per.setdefault(d, {})[v] = val
    out: Dict[str, List[Tuple[str, float]]] = {}
    for d, by_v in per.items():
        seq: List[Tuple[str, float]] = []
        for v in sorted(by_v):
            if seq and seq[-1][1] == by_v[v]:
                continue                      # 同值连续 → 不是新版本
            seq.append((v, by_v[v]))
        out[d] = seq
    return out


def build_rows(sid: str, points: Iterable[Tuple[str, str, float]], tz: str,
               ingested_at: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """转成 ``macro_observations`` 与 ``macro_releases`` 的行。

    ``is_initial_release`` 的含义被严格限定为「**本库能取到的最早版本**」。
    FRED 的 vintage 历史并非从序列诞生开始（例如 CPIAUCSL 的 vintage 只回溯到
    1972-07-21），所以对更早的观测，这个标记**不等于真实首次公布**；真实首次
    公布需要 ``output_type=4`` 或官方发布日历，本轮未做，故 ``verified=0``。
    """
    canon = canonicalize(points)
    rows: List[Dict[str, Any]] = []
    rel: List[Dict[str, Any]] = []
    for d in sorted(canon):
        seq = canon[d]
        for i, (vintage, value) in enumerate(seq):
            is_init = 1 if i == 0 else 0
            rts = conservative_release_ts(vintage, tz)
            rows.append({
                "series_id": sid, "observation_period": d, "vintage_date": vintage,
                "realtime_start": vintage, "realtime_end": seq[i + 1][0] if i + 1 < len(seq) else None,
                "value": value, "is_initial_release": is_init,
                "source": "FRED", "ingested_at": ingested_at,
                "release_date": vintage, "release_timestamp": rts,
                "time_precision": DATE_ONLY, "verified": 0,
            })
            rel.append({
                "series_id": sid, "observation_period": d, "release_date": vintage,
                "release_timestamp": rts, "timezone": tz,
                "release_type": "initial" if is_init else "revision",
                "source": "FRED", "time_precision": DATE_ONLY, "verified": 0,
                "notes": ("FRED vintage date；时刻未核验，按当日 23:59:59 保守处理"
                          + ("（最早可得版本，非必然的真实首次公布）" if is_init else "")),
            })
    return rows, rel


# --------------------------------------------------------------------------- #
# 日频市场序列：vintage 矩阵不可用时的口径
# --------------------------------------------------------------------------- #
def fetch_market_daily(cli: FredClient, sid: str, store: MacroStore,
                       raw_dir: Path) -> List[Dict[str, Any]]:
    """一次取回全历史观测（默认口径，不带 ``output_type``）。

    为什么这些序列不用 vintage 矩阵：实测 ``DFF`` 在 **50 列**的窗口上就稳定
    504（网关 60 秒超时），``DGS2`` / ``DGS10`` 同理；而它们的修订数为 0
    （见 ``reports/marketdata_probe/fred_live_validation.md``：498~729 个比对
    点全部无修订）。为拿到**完整历史**（vintage 模式对 ``VIXCLS`` 只覆盖了
    约 4,000 / 9,200 个交易日），这里改用"一条观测一个版本"的口径。
    """
    st, body, params = cli.get("series/observations", series_id=sid)
    payload = _json(body)
    obs = payload.get("observations") if isinstance(payload, dict) else None
    store.archive_raw(endpoint="/series/observations", params=params, body=body,
                      http_status=st, raw_dir=raw_dir,
                      row_count=len(obs or []) if isinstance(obs, list) else None)
    if st != 200 or not isinstance(obs, list):
        msg = payload.get("error_message") if isinstance(payload, dict) else str(payload)[:160]
        raise FetchError(f"/series/observations HTTP {st}: {msg}")
    return [o for o in obs if isinstance(o, dict) and isinstance(o.get("date"), str)]


def _next_weekday(d: str, n: int) -> str:
    """推后 n 个工作日（只跳周末；节假日未建模，方向仍是"更晚"）。"""
    from datetime import date as _date, timedelta as _td
    cur = _date.fromisoformat(d)
    while n > 0:
        cur += _td(days=1)
        if cur.weekday() < 5:
            n -= 1
    return cur.isoformat()


def build_market_rows(sid: str, obs: List[Dict[str, Any]], tz: str,
                      ingested_at: str, lag_weekdays: int
                      ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """日频市场序列 → 行。``time_precision='inferred'``，**不冒充**实测时刻。

    规则（写进 ``notes``，可审计）：
    * 观测日 D 的收盘价/收益率，按"D 日收盘后可见"处理（``release_timestamp``
      取 D 日 23:59:59 本地时间）；
    * ``DFF``（有效联邦基金利率）由纽约联储在**次一工作日**发布，故推后 1 个
      工作日，宁可晚半天也不提前。
    """
    rows: List[Dict[str, Any]] = []
    rel: List[Dict[str, Any]] = []
    for o in obs:
        d = o["date"]
        v = o.get("value")
        if v in (".", "", None):
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        rdate = _next_weekday(d, lag_weekdays) if lag_weekdays else d
        rts = conservative_release_ts(rdate, tz)
        rows.append({
            "series_id": sid, "observation_period": d, "vintage_date": rdate,
            "realtime_start": rdate, "realtime_end": None,
            "value": fv, "is_initial_release": 1,
            "source": "FRED", "ingested_at": ingested_at,
            "release_date": rdate, "release_timestamp": rts,
            "time_precision": INFERRED, "verified": 0,
        })
        rel.append({
            "series_id": sid, "observation_period": d, "release_date": rdate,
            "release_timestamp": rts, "timezone": tz, "release_type": "initial",
            "source": "FRED", "time_precision": INFERRED, "verified": 0,
            "notes": ("日频市场序列：发布时刻无 FRED vintage 依据，按"
                      f"“观测日 + {lag_weekdays} 个工作日收盘后可见”推定，"
                      "仅到日粒度，未用官方日历核验"),
        })
    return rows, rel


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def ingest_series(cli: FredClient, store: MacroStore, task: SeriesTask,
                  task_id: str, raw_dir: Path,
                  max_windows: Optional[int] = None) -> Dict[str, Any]:
    sid = task.series_id
    store.start_ingest(task_id, sid)
    try:
        meta = fetch_series_meta(cli, sid, store, raw_dir)
        meta.update({"timezone": task.timezone,
                     "release_time_hint": task.release_time_hint})
        store.upsert_series(meta)

        ingested_at = now_utc()
        if task.mode == "market_daily":
            obs = fetch_market_daily(cli, sid, store, raw_dir)
            obs_rows, rel_rows = build_market_rows(
                sid, obs, task.timezone, ingested_at, task.lag_weekdays)
            r1 = store.upsert_observations(obs_rows)
            r2 = store.upsert_releases(rel_rows)
            store.finish_ingest(task_id, sid,
                                WriteResult(r1.inserted, r1.updated, r1.unchanged), "ok")
            return {"series_id": sid, "status": "ok", "mode": task.mode,
                    "vintages": 1, "windows": 1, "parsed_points": len(obs),
                    "observation_periods": len({r["observation_period"] for r in obs_rows}),
                    "observations": r1.as_dict(), "releases": r2.as_dict()}

        vintages = fetch_vintage_dates(cli, sid, store, raw_dir)
        windows = plan_windows(vintages)
        if max_windows is not None:
            windows = windows[:max_windows]
        points: List[Tuple[str, str, float]] = []
        for grp in windows:
            points.extend(fetch_chunk(cli, sid, grp[0], grp[-1], store, raw_dir, grp))

        ingested_at = now_utc()
        obs_rows, rel_rows = build_rows(sid, points, task.timezone, ingested_at)
        r1 = store.upsert_observations(obs_rows)
        r2 = store.upsert_releases(rel_rows)
        result = WriteResult(inserted=r1.inserted, updated=r1.updated,
                             unchanged=r1.unchanged)
        store.finish_ingest(task_id, sid, result, "ok")
        return {"series_id": sid, "status": "ok", "mode": task.mode,
                "vintages": len(vintages),
                "windows": len(windows), "parsed_points": len(points),
                "observation_periods": len({p[0] for p in points}),
                "observations": r1.as_dict(), "releases": r2.as_dict()}
    except Exception as exc:  # noqa: BLE001
        store.finish_ingest(task_id, sid, WriteResult(), "failed",
                            f"{type(exc).__name__}: {exc}")
        return {"series_id": sid, "status": "failed",
                "error": f"{type(exc).__name__}: {exc}"}


def load_key() -> Tuple[str, str]:
    """取 FRED key：进程环境优先，其次 Windows 用户环境变量（注册表）。"""
    import os
    env = (os.environ.get("FRED_API_KEY") or "").strip()
    if env:
        return env, "process_env"
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                v, _ = winreg.QueryValueEx(k, "FRED_API_KEY")
            v = str(v or "").strip()
            if v:
                return v, "user_env(registry)"
        except OSError:
            pass
    return "", "missing"
