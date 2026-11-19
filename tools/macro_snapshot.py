#!/usr/bin/env python3
"""查询某一历史时点的宏观快照（命令行入口，只读）。

    python tools/macro_snapshot.py --as-of 2023-04-28T00:00:00-04:00
    python tools/macro_snapshot.py --as-of 2026-10-16T00:00:00Z --series CPIAUCSL,GDP
    python tools/macro_snapshot.py --as-of 2023-04-28T00:00:00-04:00 --json

它调用的是 :func:`ai_trader.macro.snapshot.get_macro_snapshot` —— 与回测用的是
**同一个函数**，不存在"命令行一套、回测另一套"的风险。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ai_trader.macro.snapshot import get_macro_snapshot  # noqa: E402
from ai_trader.macro.store import MacroStore, MacroStoreError  # noqa: E402

DEFAULT_DB = ROOT / "data" / "macro_data.db"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--as-of", required=True,
                    help="ISO8601，必须带时区偏移（如 2023-04-28T00:00:00-04:00）")
    ap.add_argument("--series", default="", help="逗号分隔；空=全部")
    ap.add_argument("--json", action="store_true", help="输出完整 JSON")
    ap.add_argument("--limit", type=int, default=3, help="每条序列显示最近几个观测期")
    args = ap.parse_args()

    series = [s.strip() for s in args.series.split(",") if s.strip()] or None
    try:
        snap = get_macro_snapshot(args.db, args.as_of, series=series)
    except MacroStoreError as exc:
        print(f"ERROR: {exc}")
        return 2

    if args.json:
        print(json.dumps(snap.as_dict(), ensure_ascii=False, indent=2))
        return 0

    print(f"as_of = {snap.as_of}  (epoch {snap.as_of_epoch})")
    print(f"{'序列':16s} {'观测期':12s} {'值':>12s} {'版本日':12s} {'精度':7s} 初值")
    for sid in sorted(snap.by_series):
        pts = snap.by_series[sid][-args.limit:]
        for p in pts:
            print(f"{sid:16s} {p.observation_period:12s} {p.value:>12.4f} "
                  f"{p.vintage_date:12s} {p.time_precision:7s} "
                  f"{'是' if p.is_initial_release else ''}")
    if snap.missing_series:
        print(f"\n该时点无可见数据的序列: {', '.join(snap.missing_series)}")
    if snap.precision_warnings:
        print(f"时间精度提示: {len(snap.precision_warnings)} 条"
              f"（发布时刻只精确到日，按当日 23:59:59 保守处理）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
