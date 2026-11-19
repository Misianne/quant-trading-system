#!/usr/bin/env python3
"""FRED 实盘验证：12 条序列的元数据、深度、vintage 与修订还原。

只读：不改数据库、不写策略、不下单。密钥只在本进程内存中使用，
落盘前对整份 JSON 做「密钥字面量」硬断言，命中即拒绝写盘；URL 里的
``api_key`` 一律替换成 ``***``。

为什么每序列要打 6 个请求
--------------------------
1. ``/series``                       元数据（单位、频率、覆盖区间、季调）
2. ``observations?sort_order=asc&limit=1``   最早观测（= 历史深度）
3. ``observations?sort_order=desc&limit=1``  最新观测（= 当前修订值）
4. ``/series/vintagedates``          官方 vintage 日期清单（修订发生日）
5. ``observations?output_type=4``    首次公布值（Initial Release Only）
6. ``observations``（默认）          当前值 —— 与 5 对比即为修订
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://api.stlouisfed.org/fred"
KEY_ENV = "FRED_API_KEY"

SERIES = [
    ("CPIAUCSL", "CPI 全项目（季调）", 3),
    ("CPILFESL", "核心 CPI（季调）", 3),
    ("PCEPI", "PCE 价格指数（季调）", 3),
    ("PCEPILFE", "核心 PCE（季调）", 3),
    ("PAYEMS", "非农就业（季调，千人）", 3),
    ("UNRATE", "失业率（季调）", 3),
    ("CES0500000003", "平均时薪（季调）", 3),
    ("GDP", "GDP（名义，季调年化）", 4),
    ("DFF", "联邦基金有效利率（日）", 2),
    ("DGS2", "2 年期国债收益率（日）", 2),
    ("DGS10", "10 年期国债收益率（日）", 2),
    ("VIXCLS", "VIX 收盘（日）", 2),
]


# --------------------------------------------------------------------------- #
def load_key() -> tuple[str, str]:
    v = (os.environ.get(KEY_ENV) or "").strip()
    if v:
        return v, "process_env"
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                v2, _ = winreg.QueryValueEx(k, KEY_ENV)
            v2 = str(v2 or "").strip()
            if v2:
                return v2, "user_env(registry)"
        except OSError:
            pass
    return "", "missing"


def fp(key: str) -> str:
    import hashlib
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def redact(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    q = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
    q = [(k, "***" if k.lower() == "api_key" else v) for k, v in q]
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path,
                                    urllib.parse.urlencode(q), ""))


class Fred:
    """串行 + 节流 + 429 退避。FRED 建议不超过 120 次/分钟。"""

    def __init__(self, key: str, min_interval: float = 0.55, max_retries: int = 3):
        self._key = key
        self.min_interval = float(min_interval)
        self.max_retries = int(max_retries)
        self._last = 0.0
        self.calls = 0
        self.retry_429 = 0
        self.events: list[dict] = []

    def get(self, path: str, **params) -> tuple[int, object, str]:
        params = {k: v for k, v in params.items() if v is not None}
        params["api_key"] = self._key
        params.setdefault("file_type", "json")
        url = f"{BASE}/{path}?{urllib.parse.urlencode(params)}"
        safe = redact(url)
        attempt = 0
        while True:
            attempt += 1
            gap = time.monotonic() - self._last
            if gap < self.min_interval:
                time.sleep(self.min_interval - gap)
            self._last = time.monotonic()
            self.calls += 1
            status, body = self._once(url)
            rec = {"path": path, "attempt": attempt, "http": status}
            if status == 429:
                self.retry_429 += 1
                back = 20.0
                rec["outcome"] = "RATE_LIMITED"
                self.events.append(rec)
                if attempt > self.max_retries:
                    return status, {"error": "rate limited"}, safe
                time.sleep(back)
                continue
            rec["outcome"] = "OK" if status == 200 else f"HTTP_{status}"
            self.events.append(rec)
            return status, body, safe

    def _once(self, url: str):
        req = urllib.request.Request(url, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"),
            "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                return r.status, json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, raw[:300]
        except Exception as exc:  # noqa: BLE001
            return 0, {"exception": f"{type(exc).__name__}: {exc}"}


# --------------------------------------------------------------------------- #
def _struct_summary(payload) -> dict:
    """异常响应的结构摘要（写进报告，便于定位）。"""
    if isinstance(payload, dict):
        return {"type": "dict", "keys": list(payload)[:12],
                "error": payload.get("error_message") or payload.get("error")}
    return {"type": type(payload).__name__, "preview": str(payload)[:160]}


def parse_vintage_dates(payload) -> tuple[list[str], dict]:
    """vintage_dates 解析。

    **实测结构**（2026-11-19 直连 FRED）::

        {"vintage_dates": ["2026-11-10", "2026-10-14", "2026-09-11"], ...}

    元素是**字符串**日期，不是对象。旧代码 ``d.get("vintage_date")`` 因此抛
    ``AttributeError: 'str' object has no attribute 'get'``。

    另兼容 ``[{"vintage_date": "..."}]`` 的字典形态（只作容错，主路径按实测结构）。
    """
    if not isinstance(payload, dict):
        return [], {"error": "响应不是对象", "struct": _struct_summary(payload)}
    raw = payload.get("vintage_dates")
    if raw is None:
        return [], {"error": "缺少 vintage_dates 字段", "struct": _struct_summary(payload)}
    if not isinstance(raw, list):
        return [], {"error": f"vintage_dates 不是数组（{type(raw).__name__}）",
                    "struct": _struct_summary(payload)}
    out, bad = [], 0
    for item in raw:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict) and isinstance(item.get("vintage_date"), str):
            out.append(item["vintage_date"])
        else:
            bad += 1
    info: dict = {"page_count": payload.get("count"), "malformed_elements": bad}
    if bad:
        info["error"] = f"{bad} 个元素格式异常，已跳过"
    return out, info


def parse_series_meta(payload) -> tuple[dict, dict]:
    """实测结构：``{"seriess": [ {...} ]}``。"""
    if not isinstance(payload, dict) or not isinstance(payload.get("seriess"), list):
        return {}, {"error": "缺少 seriess 数组", "struct": _struct_summary(payload)}
    rows = payload["seriess"]
    if not rows:
        return {}, {"error": "seriess 为空"}
    s = rows[0]
    if not isinstance(s, dict):
        return {}, {"error": "seriess[0] 不是对象", "struct": _struct_summary(s)}
    keep = ("title", "units", "frequency", "seasonal_adjustment",
            "observation_start", "observation_end", "last_updated", "popularity")
    meta = {k: s.get(k) for k in keep}
    meta["notes"] = str(s.get("notes") or "")[:200]
    return meta, {}


def parse_observations(payload) -> tuple[list[dict], dict]:
    """实测结构：``{"observations": [{"realtime_start","realtime_end","date","value"}]}``。

    400 时返回 ``{"error_code":400,"error_message":"..."}`` —— 没有 observations，
    旧代码直接下标就 KeyError。
    """
    if not isinstance(payload, dict):
        return [], {"error": "响应不是对象", "struct": _struct_summary(payload)}
    if "observations" not in payload:
        return [], {"error": payload.get("error_message") or "缺少 observations 字段",
                    "struct": _struct_summary(payload)}
    rows = payload["observations"]
    if not isinstance(rows, list):
        return [], {"error": "observations 不是数组", "struct": _struct_summary(payload)}
    out = [r for r in rows if isinstance(r, dict) and isinstance(r.get("date"), str)]
    if len(out) != len(rows):
        return out, {"error": f"{len(rows) - len(out)} 条观测格式异常，已跳过"}
    return out, {}


def _value_map(rows) -> dict[str, str]:
    out = {}
    for o in rows:
        v = o.get("value")
        if v in (".", "", None):
            continue
        out[o["date"]] = v
    return out


def probe_series(cli: "Fred", sid: str, label: str, years: int) -> dict:
    start = (date.today() - timedelta(days=int(365.25 * years))).isoformat()
    today = date.today().isoformat()
    rec: dict = {"series_id": sid, "label": label, "window_start": start, "errors": []}

    def note(step: str, info: dict) -> None:
        if info and info.get("error"):
            rec["errors"].append({"step": step, **info})

    st, body, _ = cli.get("series", series_id=sid)
    rec["meta_http"] = st
    meta, info = parse_series_meta(body)
    note("meta", info)
    rec["meta"] = meta

    st, body, _ = cli.get("series/observations", series_id=sid, sort_order="asc", limit=1)
    rows, info = parse_observations(body)
    note("earliest", info)
    rec["earliest"] = rows[0] if rows else None

    st, body, _ = cli.get("series/observations", series_id=sid, sort_order="desc", limit=1)
    rows, info = parse_observations(body)
    note("latest", info)
    rec["latest"] = rows[0] if rows else None

    st, body, _ = cli.get("series/vintagedates", series_id=sid, limit=10000, sort_order="desc")
    vdates, info = parse_vintage_dates(body)
    note("vintagedates", info)
    rec["vintage"] = {"http": st, "count": len(vdates),
                      "newest": vdates[:3], "oldest": vdates[-3:], **info}

    # 实测两处硬约束：
    #  1) output_type=4 不给 realtime 窗口会 400（"No vintage dates exist ..."）；
    #  2) 窗口内的 vintage 数超过 **2000** 也会 400
    #     （"There are 5167 vintage dates ... maximum ... (2000)"，日频序列必踩）。
    # 因此把 realtime_start 收到「观测窗口起点前 120 天」：
    # 既覆盖每一次首次发布，又把 vintage 数量压到上限以内（日频 2 年 ≈ 600）。
    rt_start = (date.fromisoformat(start) - timedelta(days=120)).isoformat()
    wide = {"realtime_start": rt_start, "realtime_end": today}
    st, body, _ = cli.get("series/observations", series_id=sid, output_type=4,
                          observation_start=start, **wide)
    ini_rows, info = parse_observations(body)
    note("first_release", info)
    rec["first_release_http"] = st

    st, body, _ = cli.get("series/observations", series_id=sid, observation_start=start)
    cur_rows, info = parse_observations(body)
    note("current", info)
    rec["current_http"] = st

    ini_map, cur_map = _value_map(ini_rows), _value_map(cur_rows)
    diffs = []
    for d, a in ini_map.items():
        b = cur_map.get(d)
        if b is None:
            continue
        try:
            fa, fb = float(a), float(b)
        except ValueError:
            continue
        if abs(fa - fb) > 1e-9:
            diffs.append({"date": d, "first_release": a, "current": b,
                          "delta": round(fb - fa, 6),
                          "pct": round((fb / fa - 1) * 100, 4) if fa else None})
    diffs.sort(key=lambda x: abs(x["delta"]), reverse=True)
    rec["revision"] = {"compared_points": len(set(ini_map) & set(cur_map)),
                       "revised_points": len(diffs), "top": diffs[:5]}
    return rec


def pit_check(cli: "Fred", sid: str, obs_date: str, current_value) -> dict:
    """点时间还原：验证 realtime 窗口与**真实发布日**的关系。

    不是"首次值 != 当前值"就算通过。这里做三件事：
      1. 取该观测的完整 vintage 链（output_type=1 + 宽 realtime），最小
         ``realtime_start`` 即**实际发布日**；
      2. 把 ``realtime_end`` 设成发布日前一天 → 该观测**必须不可见**；
      3. 把 ``realtime_end`` 设成发布日当天 → 必须可见且**等于首次公布值**。
    三条同时成立，才算"历史时点可正确还原"。
    """
    wide_start = "1776-07-04"
    today = date.today().isoformat()
    out: dict = {"series_id": sid, "observation_date": obs_date,
                 "current_value": current_value, "errors": []}

    st, body, _ = cli.get("series/observations", series_id=sid,
                          observation_start=obs_date, observation_end=obs_date,
                          output_type=1, realtime_start=wide_start, realtime_end=today)
    rows, info = parse_observations(body)
    if info.get("error"):
        out["errors"].append({"step": "chain", **info})
    chain = sorted([r for r in rows if r.get("date") == obs_date],
                   key=lambda r: str(r.get("realtime_start")))
    out["http_chain"] = st
    out["chain"] = [{"realtime_start": r.get("realtime_start"), "value": r.get("value")}
                    for r in chain]
    if not chain:
        out["verdict"] = "FAIL: 取不到该观测的 vintage 链"
        return out
    pub = str(chain[0]["realtime_start"])
    first_val = chain[0].get("value")
    before = (date.fromisoformat(pub) - timedelta(days=1)).isoformat()

    st_b, body_b, _ = cli.get("series/observations", series_id=sid,
                              observation_start=obs_date, observation_end=obs_date,
                              output_type=1, realtime_start=wide_start, realtime_end=before)
    rows_b, _ = parse_observations(body_b)
    seen_before = [r.get("value") for r in rows_b if r.get("date") == obs_date]

    st_a, body_a, _ = cli.get("series/observations", series_id=sid,
                              observation_start=obs_date, observation_end=obs_date,
                              output_type=1, realtime_start=wide_start, realtime_end=pub)
    rows_a, _ = parse_observations(body_a)
    seen_at = [r.get("value") for r in rows_a if r.get("date") == obs_date]

    out.update({"publication_date": pub, "first_release_value": first_val,
                "visible_day_before": seen_before,
                "visible_on_publication_date": seen_at,
                "http_before": st_b, "http_at": st_a,
                "probe_before": f"realtime_end={before}",
                "probe_at": f"realtime_end={pub}"})
    ok_before = (len(seen_before) == 0)
    ok_at = bool(seen_at) and str(seen_at[0]) == str(first_val)
    out["verdict"] = ("PASS: 发布日前不可见，发布日当天=首次公布值"
                      if (ok_before and ok_at) else
                      f"FAIL: before_visible={seen_before} at={seen_at} first={first_val}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    ap.add_argument("--min-interval", type=float, default=0.55)
    ap.add_argument("--md", default=str(ROOT / "reports" / "marketdata_probe"
                                        / "fred_live_validation.md"))
    args = ap.parse_args()

    key, source = load_key()
    if not key:
        print(f"BLOCKED: 未找到 {KEY_ENV}")
        print("  请先运行 scripts/set_fred_key.ps1（掩码输入，不进聊天记录）")
        return 2
    if not (len(key) == 32 and key.isalnum() and key == key.lower()):
        print(f"BLOCKED: {KEY_ENV} 格式不合法（长度 {len(key)}，FRED 要求 32 位小写字母数字）")
        return 2

    print(f"KEY_SOURCE={source}  len={len(key)}  format=OK(32 lower-alnum)"
          "  （按要求不输出 key 与指纹）")
    cli = Fred(key, min_interval=args.min_interval)
    report: dict = {"probed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "key_source": source, "base": BASE,
                    "series": {}, "errors": [], "events": None}

    hdr = (f"{'序列':16s} {'覆盖起始':11s} {'最早观测':11s} {'最新观测':11s} "
           f"{'vintage':>8s} {'修订点':>6s}  状态")
    print("\n" + hdr)
    for sid, label, years in SERIES:
        try:
            r = probe_series(cli, sid, label, years)
        except Exception as exc:                      # noqa: BLE001
            # 单序列失败不影响其它序列
            r = {"series_id": sid, "label": label,
                 "fatal": f"{type(exc).__name__}: {exc}", "errors": [],
                 "revision": {"revised_points": -1}, "vintage": {"count": -1}}
            report["errors"].append({"series_id": sid, "fatal": r["fatal"]})
        report["series"][sid] = r
        if r.get("fatal"):
            print(f"{sid:16s} {'-':11s} {'-':11s} {'-':11s} {'-':>8s} {'-':>6s}  FATAL {r['fatal'][:40]}")
            continue
        m = r.get("meta") or {}
        e = (r.get("earliest") or {}).get("date", "-")
        l = r.get("latest") or {}
        st_txt = "OK" if not r["errors"] else f"{len(r['errors'])} 项结构异常"
        print(f"{sid:16s} {str(m.get('observation_start')):11s} {e:11s} "
              f"{str(l.get('date')):11s} {r['vintage']['count']:8d} "
              f"{r['revision']['revised_points']:6d}  {st_txt}")

    # ---- 挑修订最大的三处，做「发布日边界」验证 ----
    print("\n[点时间还原] 选修订幅度最大的 3 个点，验证 realtime 窗口与真实发布日")
    picks = []
    for sid, r in report["series"].items():
        if r.get("fatal"):
            continue
        for d in (r.get("revision") or {}).get("top", [])[:1]:
            picks.append((sid, d))
    picks.sort(key=lambda x: abs(float(x[1]["delta"])), reverse=True)
    pits = []
    for sid, d in picks[:3]:
        try:
            pit = pit_check(cli, sid, d["date"], d["current"])
        except Exception as exc:                      # noqa: BLE001
            pit = {"series_id": sid, "observation_date": d["date"],
                   "verdict": f"ERROR {type(exc).__name__}: {exc}"}
        pit["largest_revision"] = d
        pits.append(pit)
        print(f"  {sid} {d['date']} 发布日={pit.get('publication_date')} "
              f"前一日可见={pit.get('visible_day_before')} "
              f"发布日可见={pit.get('visible_on_publication_date')} → {pit.get('verdict')}")
    report["point_in_time"] = pits

    report["events"] = cli.events
    blob = json.dumps(report, ensure_ascii=False, indent=2)
    if key in blob:
        print("\nABORT: 检出密钥字面量，拒绝写盘")
        return 3
    out = Path(args.out) if args.out else (
        ROOT / "reports" / "marketdata_probe" /
        f"fred_live_{datetime.now().strftime('%Y%m%d-%H%M%S')}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(blob, encoding="utf-8")

    md = Path(args.md)
    md.parent.mkdir(parents=True, exist_ok=True)
    md_txt = render_md(report, cli)
    if key in md_txt:
        print("ABORT: 报告检出密钥字面量，拒绝写盘")
        return 3
    md.write_text(md_txt, encoding="utf-8")

    print(f"\nREQUESTS={cli.calls}  RATE_LIMITED={cli.retry_429}")
    print(f"EVIDENCE={out}")
    print(f"REPORT={md}")
    return 0


def render_md(rep: dict, cli: "Fred") -> str:
    L = []
    A = L.append
    A("# FRED 实测验证报告（12 条宏观序列 + 点时间还原）\n")
    A(f"**执行时间**：{rep['probed_at']}　**端点**：`{rep['base']}`　"
      f"**密钥来源**：`{rep['key_source']}`（未落盘 key 本身，也不输出指纹）\n")
    A(f"**请求数**：{cli.calls}　**429 次数**：{cli.retry_429}\n")
    A("> 全部为官方 API 实测；`output_type=4` 取首次公布值，与当前值逐点比对得到修订。\n")

    A("## 一、序列总览\n")
    A("| 序列 | 含义 | 频率 | 覆盖起始 | 最早观测 | 最新观测 | 最新值 | vintage 数 | 窗口内修订点 |")
    A("|---|---|---|---|---|---|---|---:|---:|")
    for sid, r in rep["series"].items():
        m = r.get("meta") or {}
        e = (r.get("earliest") or {}).get("date", "-")
        l = r.get("latest") or {}
        A(f"| `{sid}` | {r['label']} | {m.get('frequency', '-')} | "
          f"{m.get('observation_start', '-')} | {e} | {l.get('date', '-')} | "
          f"{l.get('value', '-')} | {r['vintage']['count']} | {r['revision']['revised_points']} |")
    A("")

    A(f"## 二、修订实例（窗口内首次公布 vs 当前）\n")
    A("窗口：月/季序列最近 3 年、日序列最近 2 年。完整数据见同目录 JSON。\n")
    any_rev = False
    for sid, r in rep["series"].items():
        top = r["revision"].get("top") or []
        if not top:
            continue
        any_rev = True
        A(f"### `{sid}` — {r['label']}")
        A("")
        A("| 观测日 | 首次公布 | 当前修订值 | 变化 | 变化率 |")
        A("|---|---:|---:|---:|---:|")
        for d in top[:3]:
            A(f"| {d['date']} | {d['first_release']} | {d['current']} | "
              f"{d['delta']:+} | {d['pct']:+}% |")
        A("")
    if not any_rev:
        A("_窗口内未检测到修订。_\n")

    A("## 三、点时间还原验证\n")
    A("判定标准不是「首次值 != 当前值」，而是三条同时成立：")
    A("① 该观测在**发布日前一天**用 `realtime_end` 取不到；")
    A("② 在**发布日当天**取得到；")
    A("③ 取到的值等于**首次公布值**。\n")
    A("| 序列 | 观测日 | 实际发布日 | 发布日前可见? | 发布日可见值 | 首次公布值 | 当前值 | 判定 |")
    A("|---|---|---|---|---|---:|---:|---|")
    for pit in rep.get("point_in_time", []):
        before = pit.get("visible_day_before")
        before_txt = "不可见 ✅" if before == [] else f"可见 {before}"
        at = pit.get("visible_on_publication_date") or []
        A(f"| `{pit.get('series_id')}` | {pit.get('observation_date')} | "
          f"{pit.get('publication_date', '-')} | {before_txt} | "
          f"{at[0] if at else '-'} | {pit.get('first_release_value', '-')} | "
          f"{pit.get('current_value', '-')} | {pit.get('verdict', '-')} |")
    A("")
    A("### vintage 链（同一观测在各发布版本下的取值）\n")
    for pit in rep.get("point_in_time", []):
        chain = pit.get("chain") or []
        if not chain:
            continue
        A(f"- `{pit.get('series_id')}` {pit.get('observation_date')}: " +
          " → ".join(f"{c.get('realtime_start')}={c.get('value')}" for c in chain))
    A("")

    errs = [e for e in (rep.get("errors") or [])]
    if errs:
        A("## 三之二、结构异常记录\n")
        A("| 序列 | 步骤 | 错误 | 响应摘要 |")
        A("|---|---|---|---|")
        for e in errs:
            A(f"| `{e.get('series_id','-')}` | {e.get('step','fatal')} | "
              f"{e.get('error') or e.get('fatal')} | {str(e.get('struct',''))[:90]} |")
        A("")
    per_series_err = [(sid, x) for sid, r in rep["series"].items()
                      for x in (r.get("errors") or [])]
    if per_series_err:
        A("### 单序列内的解析异常\n")
        for sid, x in per_series_err:
            A(f"- `{sid}` / {x.get('step')}: {x.get('error')} — `{str(x.get('struct',''))[:100]}`")
        A("")

    A("## 四、评价\n")
    A("| 维度 | 结论 |")
    A("|---|---|")
    A("| 数据质量 | 官方一手或官方转载，单位/季调/频率齐备；`output_type=4` 提供首次公布值 |")
    A("| 历史深度 | 见 §一「覆盖起始」列，最早观测普遍回溯数十年 |")
    A("| 更新频率 | 元数据 `frequency` 字段（月/季/日），`last_updated` 给出最近更新时间 |")
    A("| 授权 | 多数序列公共领域；**逐序列再分发限制未核实** |")
    A("| 费用 | 免费（需注册 key） |")
    A("| 接口稳定性 | 串行 + 0.55s 节流，本轮 429 次数见页首 |")
    A("")
    A("## 四之二、接口硬约束（本轮实测踩到的 400）\n")
    A("| # | 约束 | 触发条件 | 错误原文/表现 |")
    A("|---|---|---|---|")
    A("| 1 | `vintage_dates` 是**字符串数组** | 按 `d.get('vintage_date')` 解析 | "
      "`AttributeError: 'str' object has no attribute 'get'`（整轮中断） |")
    A("| 2 | `output_type=4` **必须**给 realtime 窗口 | 只传 `observation_start` | "
      "400 `No vintage dates exist for the specified real-time period: <today> to <today>` |")
    A("| 3 | 窗口内 vintage 数 **≤2000** | 日频序列用 1776→今天 | "
      "400 `There are 5167 vintage dates ... exceeds the maximum ... (2000)` |")
    A("| 4 | 400 响应**没有** `observations` 字段 | 直接 `body['observations']` | `KeyError` |")
    A("")
    A("对策：`realtime_start` 取「观测窗口起点前 120 天」——既覆盖每次首次发布，"
      "又把 vintage 数压到上限内（日频 2 年约 600）。\n")

    A("## 五、未验证\n")
    A("- FRED 的**硬性速率上限**（官方未公布具体数值，只建议 ≤120 次/分钟）")
    A("- 各序列的**逐条授权条款**（是否有第三方再分发限制）")
    A("- `output_type=2`（全量 vintage 矩阵）未跑——体积过大，本轮用 `4` vs 默认值的差分替代")
    A("- BEA / 纽约联储等原始发布方未纳入本轮")
    A("")
    return "\n".join(L)


if __name__ == "__main__":
    sys.exit(main())
