#!/usr/bin/env python3
"""宏观状态 × QQQ 前瞻收益：可复现的历史研究（只读，不产生交易信号）。

方法（全部写死，便于复现）
--------------------------
1. **决策时点**：每个自然月的**最后一个交易日** 23:59:59（America/New_York）。
2. **信息集**：``get_macro_snapshot(as_of=该时刻)`` —— 只含当时已公开的版本，
   未来修订值不可能出现在里面。
3. **入场**：决策日**次一交易日的开盘价**（不用决策日收盘价假装成交）。
4. **前瞻**：5 / 20 / 60 个交易日，按收盘价计；并统计 60 日窗口内相对入场价的
   最大不利偏移（MAE）。
5. **统计**：均值、中位数、标准差、最小/最大、胜率、bootstrap 95% CI，
   以及"该状态 vs 其余样本"的置换检验 p 值。
6. **增量检验**：与**价格趋势基准**（收盘价是否在 200 日均线上方，只用历史数据）
   对比 —— 若宏观状态在价格趋势之外没有额外区分力，就不能说它有增量价值。
7. **稳健性**：换决策日（月中）、换阈值，看结论符号是否稳定。

**本脚本不输出任何买卖信号**，只输出条件分布与显著性。
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ai_trader.macro import get_macro_snapshot              # noqa: E402
from ai_trader.macro import regime as R                     # noqa: E402

NY = ZoneInfo("America/New_York")
MACRO_DB = ROOT / "data" / "macro_data.db"
MARKET_DB = ROOT / "data" / "market_us.db"
HORIZONS = (5, 20, 60)
PRIMARY = ["CPIAUCSL", "CPILFESL", "PCEPI", "PCEPILFE",
           "PAYEMS", "UNRATE", "GDP", "DFF"]


# --------------------------------------------------------------------------- #
# 数据
# --------------------------------------------------------------------------- #
def load_qqq(db: Path):
    import sqlite3
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = c.execute(
        "SELECT trade_date, open, close FROM us_bars WHERE symbol='usqqq'"
        " ORDER BY trade_date").fetchall()
    c.close()
    bars = [{"date": r[0], "open": float(r[1]), "close": float(r[2])} for r in rows]
    closes = [b["close"] for b in bars]
    for i, b in enumerate(bars):
        b["sma200"] = (statistics.fmean(closes[i - 199:i + 1]) if i >= 199 else None)
    return bars


def selection_indices(bars, mode: str):
    """决策日下标。mode = month_end | month_mid。"""
    if mode == "month_mid":
        out, seen = [], set()
        for i, b in enumerate(bars):
            key = b["date"][:7]
            if key not in seen and b["date"][8:] >= "15":
                out.append(i)
                seen.add(key)
        return out
    out = []
    for i in range(len(bars)):
        if i + 1 == len(bars) or bars[i + 1]["date"][:7] != bars[i]["date"][:7]:
            out.append(i)
    return out


def forward_stats(bars, i: int):
    if i + 1 >= len(bars):
        return None
    entry = bars[i + 1]["open"]
    if entry <= 0:
        return None
    res = {"entry_date": bars[i + 1]["date"], "entry": entry}
    for h in HORIZONS:
        j = i + h
        res[f"ret{h}"] = (bars[j]["close"] / entry - 1.0) if j < len(bars) else None
    j60 = min(i + 60, len(bars) - 1)
    window = [bars[k]["close"] for k in range(i + 1, j60 + 1)]
    res["max_dd_60"] = (min(window) / entry - 1.0) if window else None
    return res


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #
def bootstrap_ci(values, n_boot: int = 10000, alpha: float = 0.05, seed: int = 42):
    if len(values) < 2:
        return (None, None)
    rng = random.Random(seed)
    n = len(values)
    means = sorted(sum(rng.choice(values) for _ in range(n)) / n
                   for _ in range(n_boot))
    return (means[int(alpha / 2 * n_boot)],
            means[int((1 - alpha / 2) * n_boot) - 1])


def permutation_p(all_values, group_values, n_perm: int = 10000, seed: int = 7):
    if len(group_values) < 2 or len(group_values) >= len(all_values):
        return None
    rng = random.Random(seed)
    obs = statistics.fmean(group_values) - statistics.fmean(all_values)
    k = len(group_values)
    pool = list(all_values)
    cnt = 0
    for _ in range(n_perm):
        rng.shuffle(pool)
        if abs(statistics.fmean(pool[:k]) - statistics.fmean(pool)) >= abs(obs):
            cnt += 1
    return cnt / n_perm


def spearman(xs, ys):
    """秩相关（不引入 scipy）。"""
    n = len(xs)
    if n < 3:
        return None

    def rank(v):
        order = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    rx, ry = rank(xs), rank(ys)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return num / den if den else None


def summarise(values):
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    lo, hi = bootstrap_ci(vals)
    return {"n": len(vals),
            "mean": round(statistics.fmean(vals), 6),
            "median": round(statistics.median(vals), 6),
            "sd": round(statistics.pstdev(vals), 6) if len(vals) > 1 else None,
            "min": round(min(vals), 6), "max": round(max(vals), 6),
            "win_rate": round(sum(1 for v in vals if v > 0) / len(vals), 4),
            "ci95_mean": [None if lo is None else round(lo, 6),
                          None if hi is None else round(hi, 6)]}


def fmt(v, pct=True, width=8):
    if v is None:
        return "-".rjust(width)
    return (f"{v:+.2%}" if pct else f"{v:+.3f}").rjust(width)


# --------------------------------------------------------------------------- #
def build_records(macro_db: str, bars, mode: str):
    recs = []
    for i in selection_indices(bars, mode):
        d = bars[i]["date"]
        as_of = datetime.combine(date.fromisoformat(d), time(23, 59, 59),
                                 tzinfo=NY).isoformat()
        snap = get_macro_snapshot(macro_db, as_of, series=PRIMARY)
        a = R.assess(snap)
        fwd = forward_stats(bars, i)
        if not fwd:
            continue
        recs.append({
            "decision_date": d, "as_of": as_of, "state": a.state, "label": a.label,
            "confidence": a.confidence,
            "infl_dir": a.inflation.direction, "infl_score": a.inflation.score,
            "grow_dir": a.growth.direction, "grow_score": a.growth.score,
            "macro_score": a.growth.score - a.inflation.score,
            "infl_raw": a.inflation.detail,
            "policy_stance": a.policy.get("stance"),
            "real_rate": a.policy.get("real_rate"),
            "cpi_yoy": (a.inflation.detail.get("CPIAUCSL") or {}).get("yoy"),
            "unrate_3m": (a.growth.detail.get("UNRATE") or {}).get("delta_3m"),
            "versions": {k: a.versions.get(k) for k in ("CPIAUCSL", "PAYEMS", "GDP")},
            "above_sma200": (None if bars[i].get("sma200") is None
                             else bars[i]["close"] > bars[i]["sma200"]),
            "forward": fwd,
        })
    return recs


def block_stats(recs, key_fn):
    groups = {}
    for r in recs:
        groups.setdefault(key_fn(r), []).append(r)
    out = {}
    allv = {h: [r["forward"][f"ret{h}"] for r in recs
                if r["forward"][f"ret{h}"] is not None] for h in HORIZONS}
    for g, rs in groups.items():
        g = str(g)
        out[g] = {"n": len(rs), "dates": [r["decision_date"] for r in rs],
                  "max_dd_60": summarise([r["forward"]["max_dd_60"] for r in rs])}
        for h in HORIZONS:
            vals = [r["forward"][f"ret{h}"] for r in rs
                    if r["forward"][f"ret{h}"] is not None]
            s = summarise(vals)
            if s:
                s["p_vs_rest"] = permutation_p(allv[h], vals)
            out[g][f"ret{h}"] = s
    return out


def print_table(title, stats, baseline, label_map=None):
    print(f"\n### {title}")
    print(f"{'分组':28s} {'n':>3s} {'5d均值':>8s} {'20d均值':>9s} {'20d中位':>9s} "
          f"{'60d均值':>9s} {'胜率':>6s} {'60dMAE':>8s} {'p20':>6s} {'p60':>6s}")
    for g, s in sorted(stats.items(), key=lambda kv: -kv[1]["n"]):
        name = (label_map or {}).get(g, g)
        r5, r20, r60 = s.get("ret5"), s.get("ret20"), s.get("ret60")
        if not (r5 and r20 and r60):
            continue
        p20 = r20.get("p_vs_rest")
        p60 = r60.get("p_vs_rest")
        print(f"{name:28s} {s['n']:3d} {fmt(r5['mean'])} {fmt(r20['mean'], width=9)} "
              f"{fmt(r20['median'], width=9)} {fmt(r60['mean'], width=9)} "
              f"{r20['win_rate']:6.1%} "
              f"{fmt((s.get('max_dd_60') or {}).get('mean'))} "
              f"{(f'{p20:.3f}' if p20 is not None else '-').rjust(6)} "
              f"{(f'{p60:.3f}' if p60 is not None else '-').rjust(6)}")
    b5, b20, b60 = baseline.get("ret5"), baseline.get("ret20"), baseline.get("ret60")
    if b20:
        print(f"{'全样本(基准)':28s} {b20['n']:3d} {fmt(b5['mean'])} "
              f"{fmt(b20['mean'], width=9)} {fmt(b20['median'], width=9)} "
              f"{fmt(b60['mean'], width=9)} {b20['win_rate']:6.1%}")


def episodes(recs):
    """把连续同状态的月份合成"事件段"，用于检验结论是否只由某一段驱动。"""
    out, cur = [], []
    for r in recs:
        if cur and r["state"] != cur[-1]["state"]:
            out.append(cur)
            cur = []
        cur.append(r)
    if cur:
        out.append(cur)
    return [{"state": e[0]["state"], "start": e[0]["decision_date"],
             "end": e[-1]["decision_date"], "n": len(e)} for e in out]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--macro-db", default=str(MACRO_DB))
    ap.add_argument("--market-db", default=str(MARKET_DB))
    ap.add_argument("--out", default=str(ROOT / "reports" / "macro_research"
                                         / "regime_history.json"))
    args = ap.parse_args()

    bars = load_qqq(Path(args.market_db))
    recs = build_records(args.macro_db, bars, "month_end")
    print(f"决策点 {len(recs)}，样本 {recs[0]['decision_date']} .. {recs[-1]['decision_date']}")

    state_stats = block_stats(recs, lambda r: r["state"])
    baseline = {f"ret{h}": summarise([r["forward"][f"ret{h}"] for r in recs
                                      if r["forward"][f"ret{h}"] is not None])
                for h in HORIZONS}
    print_table("按宏观状态", state_stats, baseline, R.REGIME_LABEL)

    trend_stats = block_stats(
        recs, lambda r: ("above" if r["above_sma200"] else "below")
        if r["above_sma200"] is not None else "unknown")
    print_table("按价格趋势（QQQ 收盘 vs SMA200，仅用历史数据）", trend_stats, baseline,
                {"above": "收盘 > SMA200", "below": "收盘 < SMA200",
                 "unknown": "SMA200 不可用(样本前段)"})

    incr = {}
    for tr, flag in (("above", True), ("below", False)):
        sub = [r for r in recs if r["above_sma200"] is flag]
        if len(sub) < 6:
            continue
        incr[tr] = {"n": len(sub), "states": {}}
        pool = {h: [r["forward"][f"ret{h}"] for r in sub
                    if r["forward"][f"ret{h}"] is not None] for h in HORIZONS}
        for st in sorted({r["state"] for r in sub}):
            vals = [r["forward"]["ret20"] for r in sub
                    if r["state"] == st and r["forward"]["ret20"] is not None]
            v60 = [r["forward"]["ret60"] for r in sub
                   if r["state"] == st and r["forward"]["ret60"] is not None]
            incr[tr]["states"][st] = {
                "n": len(vals), "mean20": summarise(vals),
                # **同组内**置换检验：这是"在价格趋势之上是否还有增量"的直接检验
                "p20_vs_group": permutation_p(pool[20], vals),
                "p60_vs_group": permutation_p(pool[60], v60),
            }
    print("\n### 增量检验：价格趋势内部的宏观状态")
    for tr, d in incr.items():
        parts = []
        for st, v in d["states"].items():
            m = v["mean20"]["mean"] if v["mean20"] else None
            pv = v.get("p20_vs_group")
            parts.append(f"{R.REGIME_LABEL[st]} n={v['n']} 20d={fmt(m, width=1)}"
                         f"(p={pv:.3f})" if pv is not None else
                         f"{R.REGIME_LABEL[st]} n={v['n']} 20d={fmt(m, width=1)}")
        print(f"  {tr} (n={d['n']}): " + "; ".join(parts))

    corr = {}
    for h in HORIZONS:
        pairs = [(r["macro_score"], r["forward"][f"ret{h}"]) for r in recs
                 if r["forward"][f"ret{h}"] is not None]
        corr[f"ret{h}"] = spearman([p[0] for p in pairs], [p[1] for p in pairs])
    print("\n### 宏观得分（增长票数 − 通胀票数）与前瞻收益的 Spearman 秩相关")
    for k, v in corr.items():
        print(f"  {k}: {v:+.3f}" if v is not None else f"  {k}: n/a")

    print("\n### 稳健性 1：换决策日（月末 vs 月中）")
    alt = build_records(args.macro_db, bars, "month_mid")
    alt_stats = block_stats(alt, lambda r: r["state"])
    for st in (R.COOLING_RESILIENT, R.HEATING_RESILIENT):
        a = (state_stats.get(st) or {}).get("ret20")
        b = (alt_stats.get(st) or {}).get("ret20")
        print(f"  {R.REGIME_LABEL[st]:26s} 月末 n={a['n'] if a else 0} "
              f"{fmt(a['mean'] if a else None, width=7)} | "
              f"月中 n={b['n'] if b else 0} {fmt(b['mean'] if b else None, width=7)}")

    sens = []
    orig = (R.YOY_DELTA_EPS, R.INFL_VOTE_MIN)
    try:
        for eps in (0.05, 0.10, 0.20):
            for vmin in (1, 2):
                R.YOY_DELTA_EPS, R.INFL_VOTE_MIN = eps, vmin
                ss = block_stats(recs, lambda r: r["state"])
                cool = (ss.get(R.COOLING_RESILIENT) or {}).get("ret20")
                heat = (ss.get(R.HEATING_RESILIENT) or {}).get("ret20")
                sens.append({
                    "yoy_delta_eps": eps, "infl_vote_min": vmin,
                    "n_cooling": cool["n"] if cool else 0,
                    "cooling_20d": cool["mean"] if cool else None,
                    "n_heating": heat["n"] if heat else 0,
                    "heating_20d": heat["mean"] if heat else None,
                    "spread": (cool["mean"] - heat["mean"]) if (cool and heat) else None,
                })
    finally:
        R.YOY_DELTA_EPS, R.INFL_VOTE_MIN = orig
    print("\n### 稳健性 2：事件段结构与「剔除单段」检验")
    eps_ = episodes(recs)
    for e in eps_:
        print(f"  {e['start']} .. {e['end']}  {R.REGIME_LABEL[e['state']]:22s} n={e['n']}")
    print("\n  剔除每一段后，该状态 20d 均值是否仍与总体不同：")
    loo = []
    for target in (R.COOLING_RESILIENT, R.HEATING_RESILIENT):
        sub = [r for r in recs if r["state"] == target and r["forward"]["ret20"] is not None]
        if len(sub) < 3:
            continue
        base_mean = statistics.fmean([r["forward"]["ret20"] for r in sub])
        for e in eps_:
            if e["state"] != target:
                continue
            keep = [r for r in sub if not (e["start"] <= r["decision_date"] <= e["end"])]
            if len(keep) < 3:
                loo.append({"state": target, "dropped": f"{e['start']}..{e['end']}",
                            "n_left": len(keep), "mean20": None})
                continue
            loo.append({"state": target, "dropped": f"{e['start']}..{e['end']}",
                        "n_left": len(keep),
                        "mean20": statistics.fmean([r["forward"]["ret20"] for r in keep])})
            print(f"    {R.REGIME_LABEL[target]:20s} 全样本 n={len(sub)} "
                  f"{base_mean:+.2%} → 剔除 {e['start']}..{e['end']} 后 "
                  f"n={len(keep)} {loo[-1]['mean20']:+.2%}")

    print("\n### 稳健性 3：阈值敏感性（20 日，按新阈值**重算**状态）")
    sens = []
    for eps in (0.10, 0.50, 1.00, 2.00):
        for vmin in (1, 2, 3):
            re_stated = []
            for r in recs:
                votes = R.inflation_votes(r["infl_raw"], eps=eps, gap_eps=R.ANN3M_GAP_EPS)
                idir, iscore = R.direction_from_votes(votes, vmin)
                st = R.combine(idir, r["grow_dir"])
                re_stated.append((st, r["forward"]["ret20"], r))
            cool = [v for st, v, _ in re_stated
                    if st == R.COOLING_RESILIENT and v is not None]
            heat = [v for st, v, _ in re_stated
                    if st == R.HEATING_RESILIENT and v is not None]
            sens.append({
                "yoy_delta_eps": eps, "infl_vote_min": vmin,
                "n_cooling": len(cool), "cooling_20d": statistics.fmean(cool) if cool else None,
                "n_heating": len(heat), "heating_20d": statistics.fmean(heat) if heat else None,
                "spread": (statistics.fmean(cool) - statistics.fmean(heat))
                if (len(cool) >= 2 and len(heat) >= 2) else None,
            })
    print(f"  {'eps':>5s} {'vmin':>5s} {'n冷/热':>10s} {'冷':>8s} {'热':>8s} {'差':>8s}")
    for s_ in sens:
        print(f"  {s_['yoy_delta_eps']:5.2f} {s_['infl_vote_min']:5d} "
              f"{s_['n_cooling']:4d}/{s_['n_heating']:<4d} "
              f"{fmt(s_['cooling_20d'])} {fmt(s_['heating_20d'])} {fmt(s_['spread'])}")

    # 剔除 2022 年上半年的"单一事件"重算相关性
    no22 = [r for r in recs if not ("2021-11" <= r["decision_date"][:7] <= "2022-06")]
    corr_no22 = {}
    for h in HORIZONS:
        pairs = [(r["macro_score"], r["forward"][f"ret{h}"]) for r in no22
                 if r["forward"][f"ret{h}"] is not None]
        corr_no22[f"ret{h}"] = spearman([x[0] for x in pairs], [x[1] for x in pairs])
    print("\n### 秩相关：剔除 2021-11..2022-06 之后")
    for k, v in corr_no22.items():
        print(f"  {k}: {v:+.3f}" if v is not None else f"  {k}: n/a")

    switches = sum(1 for a, b in zip(recs, recs[1:]) if a["state"] != b["state"])
    out = {"generated_at": datetime.now(NY).isoformat(timespec="seconds"),
           "macro_db": args.macro_db, "market_db": args.market_db,
           "decision_rule": "每月最后一个交易日 23:59:59 ET；次日开盘入场；"
                            "5/20/60 个交易日后收盘计",
           "n_records": len(recs),
           "sample": {"first": recs[0]["decision_date"], "last": recs[-1]["decision_date"]},
           "baseline": baseline, "states": state_stats, "trend": trend_stats,
           "incremental": incr, "spearman": corr, "sensitivity": sens,
           "persistence": round(1 - switches / max(1, len(recs) - 1), 4),
           "episodes": eps_, "leave_one_episode_out": loo,
           "spearman_ex_2022h1": corr_no22,
           "records": recs}
    p = Path(args.out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nEVIDENCE={p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
