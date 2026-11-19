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
def _obs_map(payload) -> dict[str, str]:
    out = {}
    for o in (payload or {}).get("observations", []) if isinstance(payload, dict) else []:
        v = o.get("value")
        if v not in (".", "", None):
            out[o["date"]] = v
    return out


def probe_series(cli: Fred, sid: str, label: str, years: int) -> dict:
    start = (date.today() - timedelta(days=int(365.25 * years))).isoformat()
    rec: dict = {"series_id": sid, "label": label, "window_start": start}

    st, meta, safe = cli.get("series", series_id=sid)
    rec["meta_http"] = st
    if st == 200 and isinstance(meta, dict):
        s = (meta.get("seriess") or [{}])[0]
        rec["meta"] = {k: s.get(k) for k in
                       ("title", "units", "frequency", "seasonal_adjustment",
                        "observation_start", "observation_end", "last_updated",
                        "popularity", "notes")}
        rec["meta"]["notes"] = (rec["meta"].get("notes") or "")[:200]

    st, first, _ = cli.get("series/observations", series_id=sid,
                           sort_order="asc", limit=1)
    rec["earliest"] = (first.get("observations") or [{}])[0] if st == 200 else None

    st, last, _ = cli.get("series/observations", series_id=sid,
                          sort_order="desc", limit=1)
    rec["latest"] = (last.get("observations") or [{}])[0] if st == 200 else None

    st, vd, _ = cli.get("series/vintagedates", series_id=sid,
                        limit=10000, sort_order="desc")
    dates = [d.get("vintage_date") for d in (vd.get("vintage_dates") or [])] if st == 200 else []
    rec["vintage"] = {"http": st, "count": len(dates),
                      "newest": dates[:3], "oldest": dates[-3:] if dates else []}

    st_ini, ini, _ = cli.get("series/observations", series_id=sid,
                             output_type=4, observation_start=start)
    st_cur, cur, _ = cli.get("series/observations", series_id=sid,
                             observation_start=start)
    ini_map, cur_map = _obs_map(ini), _obs_map(cur)
    rec["first_release_http"], rec["current_http"] = st_ini, st_cur

    diffs = []
    for d, v_ini in ini_map.items():
        v_cur = cur_map.get(d)
        if v_cur is None:
            continue
        try:
            a, b = float(v_ini), float(v_cur)
        except ValueError:
            continue
        if abs(a - b) > 1e-9:
            diffs.append({"date": d, "first_release": v_ini, "current": v_cur,
                          "delta": round(b - a, 6),
                          "pct": round((b / a - 1) * 100, 4) if a else None})
    diffs.sort(key=lambda x: abs(x["delta"]), reverse=True)
    rec["revision"] = {
        "compared_points": len(set(ini_map) & set(cur_map)),
        "revised_points": len(diffs),
        "top": diffs[:5],
    }
    return rec


def point_in_time(cli: Fred, sid: str, obs_date: str, vintages: list[str]) -> dict:
    """验证「历史时点还原」：同一个观测日，在不同 vintage 下取到的值。"""
    out = {"series_id": sid, "observation_date": obs_date, "snapshots": []}
    for v in vintages:
        st, body, _ = cli.get("series/observations", series_id=sid,
                              observation_start=obs_date, observation_end=obs_date,
                              realtime_start=v, realtime_end=v)
        val = None
        if st == 200:
            ob = _obs_map(body)
            val = ob.get(obs_date)
        out["snapshots"].append({"realtime": v, "http": st, "value": val})
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

    print(f"KEY_SOURCE={source}  len={len(key)}  format=OK(32 lower-alnum)  fp={fp(key)}")
    cli = Fred(key, min_interval=args.min_interval)
    report: dict = {"probed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "key_source": source, "key_fingerprint": fp(key),
                    "base": BASE, "series": {}, "events": None}

    print(f"\n{'序列':16s} {'覆盖起始':11s} {'最早观测':11s} {'最新观测':11s} {'vintage数':>8s} {'近窗修订':>8s}")
    for sid, label, years in SERIES:
        r = probe_series(cli, sid, label, years)
        report["series"][sid] = r
        m = r.get("meta") or {}
        e = (r.get("earliest") or {}).get("date", "-")
        l = (r.get("latest") or {})
        print(f"{sid:16s} {str(m.get('observation_start')):11s} {e:11s} "
              f"{str(l.get('date')):11s} {r['vintage']['count']:8d} "
              f"{r['revision']['revised_points']:8d}")

    # ---- 挑修订最大的三处做点时间还原 ----
    print("\n[点时间还原] 选修订幅度最大的 3 个点，比较不同 vintage 下的取值")
    picks = []
    for sid, r in report["series"].items():
        for d in r["revision"]["top"][:1]:
            picks.append((sid, d))
    picks.sort(key=lambda x: abs(float(x[1]["delta"])), reverse=True)
    pits = []
    for sid, d in picks[:3]:
        vintages = [v for v in (report["series"][sid]["vintage"]["newest"] or [])][:1]
        # 用「首次修订发生日」和「今天」两个快照对照
        vd = report["series"][sid]["vintage"]
        cand = []
        if vd.get("oldest"):
            cand.append(vd["oldest"][-1])
        cand.append(date.today().isoformat())
        pit = point_in_time(cli, sid, d["date"], cand)
        pit["first_release_value"] = d["first_release"]
        pit["current_value"] = d["current"]
        pits.append(pit)
        print(f"  {sid} {d['date']}: 首次={d['first_release']} 当前={d['current']} "
              f"→ " + ", ".join(f"{s['realtime']}={s['value']}" for s in pit["snapshots"]))
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
      f"**密钥来源**：`{rep['key_source']}`（指纹 `{rep['key_fingerprint']}`，未落盘 key 本身）\n")
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
    A("同一条观测，在不同 `realtime` 快照下取到的值：\n")
    A("| 序列 | 观测日 | 首次公布值 | 历史快照取值 | 当前值 | 结论 |")
    A("|---|---|---:|---|---:|---|")
    for pit in rep.get("point_in_time", []):
        snaps = pit.get("snapshots") or []
        hist = "; ".join(f"{s['realtime']}={s['value']}" for s in snaps[:-1]) or "-"
        A(f"| `{pit['series_id']}` | {pit['observation_date']} | "
          f"{pit.get('first_release_value')} | {hist} | {pit.get('current_value')} | "
          f"{'历史值与当前值不同 → 回测若用当前值即引入未来信息' if hist and hist.split('=')[-1] != pit.get('current_value') else '见快照'} |")
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
    A("## 五、未验证\n")
    A("- FRED 的**硬性速率上限**（官方未公布具体数值，只建议 ≤120 次/分钟）")
    A("- 各序列的**逐条授权条款**（是否有第三方再分发限制）")
    A("- `output_type=2`（全量 vintage 矩阵）未跑——体积过大，本轮用 `4` vs 默认值的差分替代")
    A("- BEA / 纽约联储等原始发布方未纳入本轮")
    A("")
    return "\n".join(L)


if __name__ == "__main__":
    sys.exit(main())
