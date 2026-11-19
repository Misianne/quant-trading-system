#!/usr/bin/env python3
"""从已入库的 ``data/macro_data.db`` 生成**真实历史回放证据**（不联网）。

它回答一个问题：**在某个过去时点做决策，当时到底能看到的数字是多少？**

做法：对每条序列挑修订幅度最大的观测，输出
1. 完整 vintage 链（每个版本的值 + 生效日）；
2. 在"首次发布前 1 秒 / 首次发布当刻 / 最新版本之后"三个时点分别调用
   ``get_macro_snapshot``，把取到的值并排列出；
3. 断言：任何时点都不会出现更晚才发布的版本。

输出 ``reports/marketdata_probe/macro_replay_evidence.json`` 与终端表格。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ai_trader.macro.snapshot import get_macro_snapshot, history  # noqa: E402
from ai_trader.macro.store import MacroStore, epoch_of  # noqa: E402

DB = ROOT / "data" / "macro_data.db"
OUT = ROOT / "reports" / "marketdata_probe" / "macro_replay_evidence.json"

SERIES = ["CPIAUCSL", "CPILFESL", "PCEPI", "PCEPILFE", "PAYEMS", "UNRATE",
          "CES0500000003", "GDP", "DFF", "DGS2", "DGS10", "VIXCLS"]

LABEL = {
    "CPIAUCSL": "CPI", "CPILFESL": "核心 CPI", "PCEPI": "PCE",
    "PCEPILFE": "核心 PCE", "PAYEMS": "非农就业", "UNRATE": "失业率",
    "CES0500000003": "平均时薪", "GDP": "GDP", "DFF": "联邦基金有效利率",
    "DGS2": "2 年期国债", "DGS10": "10 年期国债", "VIXCLS": "VIX",
}


def iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


def pick_observation(conn, sid: str):
    """挑"版本数最多"的观测期（修订最多 → 最能暴露未来函数）。"""
    row = conn.execute(
        "SELECT observation_period, COUNT(*) n FROM macro_observations"
        " WHERE series_id=? GROUP BY observation_period HAVING n>=2"
        " ORDER BY n DESC, observation_period DESC LIMIT 1", (sid,)).fetchone()
    return row[0] if row else None


def visible_at(store, sid: str, period: str, ts: str):
    snap = get_macro_snapshot(store, ts, series=[sid])
    for p in snap.by_series.get(sid) or []:
        if p.observation_period == period:
            return p
    return None


def main() -> int:
    if not DB.exists():
        print(f"BLOCKED: {DB} 不存在，先跑 tools/macro_ingest.py")
        return 2
    store = MacroStore(DB)
    out = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "db": str(DB), "series": {}}
    try:
        for sid in SERIES:
            period = pick_observation(store.conn, sid)
            if period is None:
                # 无修订的序列（如国债收益率）也要记录：说明"0 修订"是比对过的
                n = store.conn.execute(
                    "SELECT COUNT(*) FROM macro_observations WHERE series_id=?",
                    (sid,)).fetchone()[0]
                out["series"][sid] = {"label": LABEL[sid], "versions": 1,
                                      "note": "该序列在 FRED 中只有一个版本（市场数据不修订）",
                                      "observations": n}
                print(f"{LABEL[sid]:12s} 单版本（{n} 条观测），无修订可比")
                continue

            chain = history(store, sid, period)
            first_epoch = epoch_of(chain[0]["release_timestamp"])
            last_epoch = epoch_of(chain[-1]["release_timestamp"])

            probes = {
                "before_first_release": iso(first_epoch - 1),
                "at_first_release": chain[0]["release_timestamp"],
                "at_latest_release": chain[-1]["release_timestamp"],
                "after_everything": iso(last_epoch + 86400),
            }
            seen = {}
            for tag, ts in probes.items():
                p = visible_at(store, sid, period, ts)
                seen[tag] = {"as_of": ts,
                             "value": None if p is None else p.value,
                             "vintage_date": None if p is None else p.vintage_date,
                             "release_timestamp": None if p is None else p.release_timestamp,
                             "time_precision": None if p is None else p.time_precision}

            lo = min(c["value"] for c in chain)
            hi = max(c["value"] for c in chain)
            out["series"][sid] = {
                "label": LABEL[sid], "observation_period": period,
                "versions": len(chain),
                "chain": [{"vintage_date": c["vintage_date"], "value": c["value"],
                           "release_timestamp": c["release_timestamp"],
                           "is_initial_release": bool(c["is_initial_release"])}
                          for c in chain],
                "min_value": lo, "max_value": hi,
                "spread_pct": round((hi / lo - 1) * 100, 4) if lo else None,
                "probes": seen,
                "leak_free": (seen["before_first_release"]["value"] is None
                              and seen["at_first_release"]["value"] == chain[0]["value"]
                              and seen["after_everything"]["value"] == chain[-1]["value"]),
            }

            print(f"\n{LABEL[sid]:12s} 观测期 {period}  共 {len(chain)} 个版本  "
                  f"区间 {lo} ~ {hi}（{out['series'][sid]['spread_pct']:+.2f}%）")
            for c in chain:
                print(f"    {c['vintage_date']}  {c['value']:>12}   "
                      f"{'初值' if c['is_initial_release'] else '修订'}")
            print(f"    回放: 首次发布前={seen['before_first_release']['value']}  "
                  f"当刻={seen['at_first_release']['value']}  "
                  f"最新后={seen['after_everything']['value']}  "
                  f"→ {'无泄漏 ✅' if out['series'][sid]['leak_free'] else '!! 泄漏 !!'}")

        leaks = [k for k, v in out["series"].items() if v.get("leak_free") is False]
        out["summary"] = {"series_checked": len(SERIES),
                          "series_with_revisions": sum(
                              1 for v in out["series"].values() if v.get("versions", 1) > 1),
                          "leaks": leaks}
        print(f"\n汇总：{out['summary']}")
    finally:
        store.close()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"EVIDENCE={OUT}")
    return 0 if not out["summary"]["leaks"] else 4


if __name__ == "__main__":
    raise SystemExit(main())
