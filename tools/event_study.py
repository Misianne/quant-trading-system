#!/usr/bin/env python3
"""事件研究：FOMC 决议 / CPI 发布 / 非农发布 前后的 QQQ 表现（只读）。

**能算什么、不能算什么**，先说清楚：

* 能算：事件日的实际反应（收盘/开盘缺口）、以及事件后 1/5/20 个交易日的走势；
* 不能算：**surprise（预期差）**。本机可得的免费源里没有「发布前市场一致预期」，
  所以任何"超预期 → 涨跌"的结论都无从检验。本脚本因此**只报告反应，不报告预期差**，
  更不会用"实际值减上月值"之类的东西冒充 surprise。

事件来源
--------
* **FOMC**：``federalreserve.gov/monetarypolicy/fomccalendars.htm`` 里每条会议记录
  都带 ``/newsevents/pressreleases/monetaryYYYYMMDDa.htm`` 链接，日期即决议日；
  决议幅度由 FRED ``DFEDTARU`` / ``DFEDTARL`` 的目标区间变化给出（决议**次一日**生效，
  脚本里做了 -1 天对齐与交叉校验）。
* **CPI / 非农**：直接取 ``macro_data.db`` 的 ``macro_releases`` 中
  ``release_type='initial'`` 的记录 —— 那是 FRED vintage 日，即首次发布时间。

输出
----
``reports/market_research/event_study.json`` + 终端摘要。
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sqlite3
import statistics
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

MARKET_DB = ROOT / "data" / "market_us.db"
MACRO_DB = ROOT / "data" / "macro_data.db"
CACHE = ROOT / "reports" / "market_research" / "fomc_decisions.json"
OUT = ROOT / "reports" / "market_research" / "event_study.json"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"}
HORIZONS = (1, 5, 20)


# --------------------------------------------------------------------------- #
# FOMC：日期 + 决议幅度
# --------------------------------------------------------------------------- #
def fetch_fomc_dates() -> list[str]:
    html = urllib.request.urlopen(urllib.request.Request(
        "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
        headers=UA), timeout=45).read().decode("utf-8", "replace")
    raw = sorted({m for m in re.findall(r"monetary(\d{8})a\.htm", html)})
    return [f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in raw]


def fetch_target_range() -> dict[str, float]:
    """FRED 目标区间上限（日频）。用于给每个决议日配幅度。"""
    from ai_trader.macro.ingest import FredClient, _json, load_key
    key, _ = load_key()
    cli = FredClient(key, min_interval=0.5)
    out = {}
    for sid in ("DFEDTARU", "DFEDTARL"):
        st, body, _ = cli.get("series/observations", series_id=sid,
                              observation_start="2021-01-01")
        obs = _json(body).get("observations", [])
        out[sid] = {o["date"]: o["value"] for o in obs if o.get("value") != "."}
    return out


def build_fomc_events(dates: list[str], ranges: dict) -> list[dict]:
    """用目标区间在「决议前 1 日」与「决议后 3 日」的差，还原决议幅度。

    为什么不是决议当天：FRED 的 DFEDTARU/L 记录的是**生效日**，而新的目标区间
    通常在决议**次一日**生效（实测 2022-03-16 决议 → 2022-03-17 生效）。
    取 +3 天是为了跨过周末/假日，同时不会跨到下一次会议（两次会议至少间隔两周）。
    """
    up, low = ranges["DFEDTARU"], ranges["DFEDTARL"]

    def at(d: dict, day: str):
        ks = [k for k in d if k <= day]
        return float(d[max(ks)]) if ks else None

    ev = []
    for d in dates:
        prev_day = (date.fromisoformat(d) - timedelta(days=1)).isoformat()
        after_day = (date.fromisoformat(d) + timedelta(days=3)).isoformat()
        before_u, after_u = at(up, prev_day), at(up, after_day)
        before_l, after_l = at(low, prev_day), at(low, after_day)
        change = None
        if None not in (before_u, after_u):
            change = round((after_u - before_u) * 100, 1)      # bp
        # 生效日：+1..+3 天内第一次出现新值的日期
        eff = None
        for k in range(1, 4):
            probe = (date.fromisoformat(d) + timedelta(days=k)).isoformat()
            v = up.get(probe)
            if v is not None and after_u is not None and float(v) == after_u:
                eff = probe
                break
        ev.append({"type": "FOMC", "date": d,
                   "target_upper_before": before_u, "target_upper_after": after_u,
                   "target_lower_before": before_l, "target_lower_after": after_l,
                   "change_bp": change, "effective_date": eff,
                   "source": f"https://www.federalreserve.gov/newsevents/"
                             f"pressreleases/monetary{d.replace('-', '')}a.htm"})
    return ev


# --------------------------------------------------------------------------- #
# 宏观发布事件
# --------------------------------------------------------------------------- #
def build_macro_events(db: Path) -> list[dict]:
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    ev = []
    for sid, label in (("CPIAUCSL", "CPI"), ("PAYEMS", "NFP")):
        rows = c.execute(
            "SELECT observation_period, release_date, release_timestamp, time_precision"
            " FROM macro_releases WHERE series_id=? AND release_type='initial'"
            " AND observation_period >= '2021-01-01' ORDER BY release_date",
            (sid,)).fetchall()
        seen = set()
        for period, rdate, rts, prec in rows:
            if rdate in seen:
                continue
            seen.add(rdate)
            ev.append({"type": label, "date": rdate, "observation_period": period,
                       "release_timestamp": rts, "time_precision": prec,
                       "source": f"FRED vintage date ({sid})"})
    c.close()
    return ev


# --------------------------------------------------------------------------- #
# 行情
# --------------------------------------------------------------------------- #
def load_qqq(db: Path):
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = c.execute("SELECT trade_date, open, close FROM us_bars"
                     " WHERE symbol='usqqq' ORDER BY trade_date").fetchall()
    c.close()
    return [{"date": r[0], "open": float(r[1]), "close": float(r[2])} for r in rows]


def index_on_or_after(bars, d: str):
    for i, b in enumerate(bars):
        if b["date"] >= d:
            return i
    return None


def reaction(bars, i):
    """事件日：收盘涨跌、开盘缺口、以及事件后 1/5/20 日。"""
    b = bars[i]
    prev_close = bars[i - 1]["close"] if i > 0 else None
    res = {"session": b["date"],
           "ret_cc": (b["close"] / prev_close - 1.0) if prev_close else None,
           "gap": (b["open"] / prev_close - 1.0) if prev_close else None,
           "intraday": b["close"] / b["open"] - 1.0 if b["open"] else None}
    for h in HORIZONS:
        j = i + h
        res[f"fwd{h}"] = (bars[j]["close"] / b["close"] - 1.0) if j < len(bars) else None
    return res


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #
def boot_ci(vals, n=10000, seed=42):
    if len(vals) < 2:
        return (None, None)
    rng = random.Random(seed)
    means = sorted(sum(rng.choice(vals) for _ in range(len(vals))) / len(vals)
                   for _ in range(n))
    return means[int(0.025 * n)], means[int(0.975 * n) - 1]


def perm_p(all_v, g):
    if len(g) < 2 or len(g) >= len(all_v):
        return None
    rng = random.Random(7)
    obs = abs(statistics.fmean(g) - statistics.fmean(all_v))
    pool, cnt, k = list(all_v), 0, len(g)
    for _ in range(10000):
        rng.shuffle(pool)
        if abs(statistics.fmean(pool[:k]) - statistics.fmean(pool)) >= obs:
            cnt += 1
    return cnt / 10000


def summ(vals):
    v = [x for x in vals if x is not None]
    if not v:
        return None
    lo, hi = boot_ci(v)
    return {"n": len(v), "mean": round(statistics.fmean(v), 6),
            "median": round(statistics.median(v), 6),
            "sd": round(statistics.pstdev(v), 6) if len(v) > 1 else None,
            "min": round(min(v), 6), "max": round(max(v), 6),
            "win_rate": round(sum(1 for x in v if x > 0) / len(v), 4),
            "ci95": [None if lo is None else round(lo, 6),
                     None if hi is None else round(hi, 6)]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh-fomc", action="store_true",
                    help="重新从美联储官网抓取 FOMC 决议日（否则用缓存）")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    CACHE.parent.mkdir(parents=True, exist_ok=True)
    if args.refresh_fomc or not CACHE.exists():
        dates = fetch_fomc_dates()
        ranges = fetch_target_range()
        fomc = build_fomc_events(dates, ranges)
        CACHE.write_text(json.dumps(
            {"fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "source": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
             "target_range_source": "FRED DFEDTARU / DFEDTARL",
             "events": fomc}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"FOMC 决议日已抓取并缓存：{len(fomc)} 条 → {CACHE}")
    fomc = json.loads(CACHE.read_text(encoding="utf-8"))["events"]

    bars = load_qqq(MARKET_DB)
    first, last = bars[0]["date"], bars[-1]["date"]
    fomc = [e for e in fomc if first <= e["date"] <= last]
    macro_ev = [e for e in build_macro_events(MACRO_DB) if first <= e["date"] <= last]
    events = fomc + macro_ev

    # 事件日 → 会话下标；同一天多个事件合并
    by_day: dict[str, list] = {}
    for e in events:
        i = index_on_or_after(bars, e["date"])
        if i is None or i == 0:
            continue
        e = dict(e, session=bars[i]["date"], session_index=i,
                 same_day_as_requested=(bars[i]["date"] == e["date"]))
        by_day.setdefault(bars[i]["date"], []).append(e)

    # 展开成带 session_index 的事件列表（后续一律用它，避免两套结构）
    events = [e for es in by_day.values() for e in es]

    all_fwd = {h: [] for h in HORIZONS}
    session_rows = []
    for i, b in enumerate(bars):
        r = reaction(bars, i)
        for h in HORIZONS:
            if r[f"fwd{h}"] is not None:
                all_fwd[h].append(r[f"fwd{h}"])
        session_rows.append(r)

    # 按事件类型统计
    per_type = {}
    for t in ("FOMC", "CPI", "NFP"):
        sel = [e for e in events if e["type"] == t]
        idxs = sorted({e["session_index"] for e in sel})
        rs = [session_rows[i] for i in idxs]
        per_type[t] = {
            "n_events": len(sel), "n_sessions": len(idxs),
            "ret_cc": summ([r["ret_cc"] for r in rs]),
            "abs_ret_cc": summ([abs(r["ret_cc"]) for r in rs if r["ret_cc"] is not None]),
            "gap": summ([r["gap"] for r in rs]),
        }
        for h in HORIZONS:
            per_type[t][f"fwd{h}"] = summ([r[f"fwd{h}"] for r in rs])
        per_type[t]["fwd_h_p"] = {
            f"fwd{h}": perm_p(all_fwd[h], [r[f"fwd{h}"] for r in rs
                                           if r[f"fwd{h}"] is not None])
            for h in HORIZONS}

    # 非事件日基准：绝对涨跌幅
    event_idx = {i for i in range(len(bars))
                 if bars[i]["date"] in by_day}
    non_event_abs = [abs(session_rows[i]["ret_cc"]) for i in range(1, len(bars))
                     if i not in event_idx and session_rows[i]["ret_cc"] is not None]
    event_abs = [abs(session_rows[i]["ret_cc"]) for i in sorted(event_idx)
                 if session_rows[i]["ret_cc"] is not None]

    # 方向延续性：事件日涨 → 之后是否延续
    cont = {}
    for t in ("FOMC", "CPI", "NFP"):
        idxs = sorted({e["session_index"] for e in events if e["type"] == t})
        up = [session_rows[i]["fwd5"] for i in idxs
              if session_rows[i]["ret_cc"] is not None and session_rows[i]["ret_cc"] > 0
              and session_rows[i]["fwd5"] is not None]
        dn = [session_rows[i]["fwd5"] for i in idxs
              if session_rows[i]["ret_cc"] is not None and session_rows[i]["ret_cc"] <= 0
              and session_rows[i]["fwd5"] is not None]
        all5 = [session_rows[i]["fwd5"] for i in idxs
                if session_rows[i]["fwd5"] is not None]
        cont[t] = {"n_up": len(up), "fwd5_after_up": summ(up),
                   "n_down": len(dn), "fwd5_after_down": summ(dn),
                   "p_after_up_vs_all": perm_p(all5, up),
                   "p_after_down_vs_all": perm_p(all5, dn)}

    # FOMC 按决议方向分组
    fomc_by_dir = {}
    for e in [x for x in events if x["type"] == "FOMC"]:
        if e.get("change_bp") is None:
            continue
        k = ("hike" if e["change_bp"] > 0 else
             "cut" if e["change_bp"] < 0 else "hold")
        fomc_by_dir.setdefault(k, []).append(session_rows[e["session_index"]])

    fomc_dir_stats = {k: {"n": len(v),
                          "ret_cc": summ([r["ret_cc"] for r in v]),
                          "fwd5": summ([r["fwd5"] for r in v]),
                          "fwd20": summ([r["fwd20"] for r in v])}
                      for k, v in fomc_by_dir.items()}

    overlap = {d: [e["type"] for e in es] for d, es in by_day.items() if len(es) > 1}

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "window": {"first": first, "last": last},
        "caveat": ("本机没有「发布前市场一致预期」数据，因此**无法计算 surprise**。"
                   "以下全部是**实际反应**，不是预期差反应。"),
        "counts": {"fomc": len(fomc), "cpi": len([e for e in macro_ev if e["type"] == "CPI"]),
                   "nfp": len([e for e in macro_ev if e["type"] == "NFP"]),
                   "overlap_days": len(overlap)},
        "per_type": per_type,
        "event_vs_non_event_abs": {
            "event": summ(event_abs), "non_event": summ(non_event_abs),
            "p": perm_p(non_event_abs + event_abs, event_abs)},
        "continuation": cont,
        "fomc_by_direction": fomc_dir_stats,
        "overlap_detail": overlap,
        "events": [{k: v for k, v in e.items() if k != "session_index"}
                   for e in events],
    }
    p = Path(args.out)
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # ---- 终端摘要 ----
    print(f"窗口 {first} .. {last} | FOMC {out['counts']['fomc']} 次、"
          f"CPI {out['counts']['cpi']} 次、非农 {out['counts']['nfp']} 次、"
          f"同日多事件 {out['counts']['overlap_days']} 天")
    print("\n注意：没有一致预期数据，下面全是「实际反应」，不是「预期差反应」。")
    print(f"\n{'事件':6s} {'n':>3s} {'当日均值':>9s} {'当日中位':>9s} {'|当日|':>8s} "
          f"{'1日':>8s} {'5日':>8s} {'20日':>8s}")
    for t in ("FOMC", "CPI", "NFP"):
        d = per_type[t]
        print(f"{t:6s} {d['n_sessions']:3d} {d['ret_cc']['mean']:+8.2%} "
              f"{d['ret_cc']['median']:+8.2%} {d['abs_ret_cc']['mean']:8.2%} "
              f"{d['fwd1']['mean']:+8.2%} {d['fwd5']['mean']:+8.2%} "
              f"{d['fwd20']['mean']:+8.2%}")
    print(f"\n事件日 |涨跌| 均值 {out['event_vs_non_event_abs']['event']['mean']:.2%} "
          f"vs 非事件日 {out['event_vs_non_event_abs']['non_event']['mean']:.2%} "
          f"(p={out['event_vs_non_event_abs']['p']})")
    print("\nFOMC 按决议方向：")
    for k, v in fomc_dir_stats.items():
        if v["n"]:
            print(f"  {k:5s} n={v['n']:2d}  当日 {v['ret_cc']['mean']:+.2%}  "
                  f"5日 {v['fwd5']['mean']:+.2%}  20日 {v['fwd20']['mean']:+.2%}")
    print("\n方向延续性（事件日后 5 日）：")
    for t, d in cont.items():
        a = d["fwd5_after_up"]["mean"] if d["fwd5_after_up"] else None
        b = d["fwd5_after_down"]["mean"] if d["fwd5_after_down"] else None
        print(f"  {t:5s} 当日涨(n={d['n_up']}) 之后 {a:+.2%} | "
              f"当日跌(n={d['n_down']}) 之后 {b:+.2%}"
              if a is not None and b is not None else f"  {t}: 样本不足")
    print(f"\nEVIDENCE={p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
