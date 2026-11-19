#!/usr/bin/env python3
"""把 FRED 的 12 条宏观序列幂等导入 ``data/macro_data.db``。

用法
----
    python tools/macro_ingest.py                 # 全量 12 条
    python tools/macro_ingest.py --dry-run       # 只取数不写库（评估体积）
    python tools/macro_ingest.py --series GDP,CPIAUCSL
    python tools/macro_ingest.py --max-windows 1 # 每条只取第一片（限流探测）

安全
----
* key 从环境变量 / 注册表读取，**不打印、不落盘、不入库**
  （``MacroStore.archive_raw`` 会剔除 ``api_key`` 参数，并在写库后复查）。
* 原始响应按请求逐份归档到 ``data/macro_raw/<task_id>/``。
* 重复运行幂等：同一 ``(series, period, vintage)`` 已存在且内容相同 → 计为 unchanged。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ai_trader.macro.ingest import (FredClient, SeriesTask, ingest_series,  # noqa: E402
                                    load_key)
from ai_trader.macro.store import MacroStore  # noqa: E402

DEFAULT_DB = ROOT / "data" / "macro_data.db"
RAW_ROOT = ROOT / "data" / "macro_raw"

#: 12 条已验证序列。``mode`` 依据实测：
#:   * ``vintages`` —— FRED vintage 矩阵可用，能拿到**真实生效日**（8 条统计序列）
#:   * ``market_daily`` —— 日频市场序列，vintage 矩阵在 50 列以上稳定 504，
#:     且实测 0 修订；改取全历史 + 推定发布时序（见 ingest.build_market_rows）
SERIES = [
    SeriesTask("CPIAUCSL", "America/New_York", "08:30"),
    SeriesTask("CPILFESL", "America/New_York", "08:30"),
    SeriesTask("PCEPI", "America/New_York", "08:30"),
    SeriesTask("PCEPILFE", "America/New_York", "08:30"),
    SeriesTask("PAYEMS", "America/New_York", "08:30"),
    SeriesTask("UNRATE", "America/New_York", "08:30"),
    SeriesTask("CES0500000003", "America/New_York", "08:30"),
    SeriesTask("GDP", "America/New_York", "08:30"),
    # DFF 由纽约联储在次一工作日发布 → 推后 1 个工作日，绝不提前
    SeriesTask("DFF", "America/New_York", None, mode="market_daily", lag_weekdays=1),
    SeriesTask("DGS2", "America/New_York", None, mode="market_daily"),
    SeriesTask("DGS10", "America/New_York", None, mode="market_daily"),
    SeriesTask("VIXCLS", "America/New_York", None, mode="market_daily"),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--series", default="", help="逗号分隔；空=全部 12 条")
    ap.add_argument("--min-interval", type=float, default=0.6)
    ap.add_argument("--max-windows", type=int, default=None,
                    help="每条序列最多取几片（评估用）")
    ap.add_argument("--dry-run", action="store_true", help="只取数、不写库")
    ap.add_argument("--report", default="", help="把结果写一份 JSON")
    args = ap.parse_args()

    key, src = load_key()
    if not key:
        print("BLOCKED: 未找到 FRED_API_KEY（环境变量与用户环境变量都没有）")
        return 2
    if len(key) != 32:
        print(f"BLOCKED: FRED_API_KEY 长度 {len(key)} 非法（应为 32）")
        return 2

    wanted = {s.strip() for s in args.series.split(",") if s.strip()} or None
    tasks = [t for t in SERIES if (wanted is None or t.series_id in wanted)]

    task_id = ("dryrun-" if args.dry_run else "ingest-") + \
        datetime.now().strftime("%Y%m%dT%H%M%S")
    raw_dir = RAW_ROOT / task_id
    cli = FredClient(key, min_interval=args.min_interval)

    print(f"KEY_SOURCE={src}  len={len(key)}  format=OK（不输出 key）")
    print(f"TASK={task_id}  DB={args.db}  SERIES={len(tasks)}")
    print(f"{'序列':16s} {'状态':8s} {'vintage':>8s} {'片':>3s} {'观测期':>7s} "
          f"{'新增':>6s} {'更新':>5s} {'未变':>6s}  备注")

    results = []
    if args.dry_run:
        # 只评估体积：取数但不开库
        from ai_trader.macro.ingest import (build_market_rows, build_rows,
                                            fetch_chunk, fetch_market_daily,
                                            fetch_series_meta, fetch_vintage_dates,
                                            plan_windows)
        class _NullStore:
            def archive_raw(self, **kw):
                return "noop"
            def start_ingest(self, *a, **k):
                pass
            def finish_ingest(self, *a, **k):
                pass
            def upsert_series(self, *a, **k):
                return False
        for t in tasks:
            try:
                meta = fetch_series_meta(cli, t.series_id, _NullStore(), raw_dir)
                if t.mode == "market_daily":
                    obs = fetch_market_daily(cli, t.series_id, _NullStore(), raw_dir)
                    rows, rel = build_market_rows(
                        t.series_id, obs, t.timezone,
                        datetime.now().astimezone().isoformat(timespec="seconds"),
                        t.lag_weekdays)
                    r = {"series_id": t.series_id, "status": "dry-run",
                         "title": meta.get("title"), "vintages": 1,
                         "windows": 1, "parsed_points": len(obs),
                         "observation_periods": len({x["observation_period"] for x in rows}),
                         "would_write_obs": len(rows), "would_write_releases": len(rel)}
                    results.append(r)
                    _print_row(r)
                    continue
                vint = fetch_vintage_dates(cli, t.series_id, _NullStore(), raw_dir)
                wins = plan_windows(vint)
                if args.max_windows:
                    wins = wins[:args.max_windows]
                pts = []
                for grp in wins:
                    pts.extend(fetch_chunk(cli, t.series_id, grp[0], grp[-1],
                                           _NullStore(), raw_dir, grp))
                rows, rel = build_rows(t.series_id, pts, t.timezone,
                                       datetime.now().astimezone().isoformat(timespec="seconds"))
                r = {"series_id": t.series_id, "status": "dry-run",
                     "title": meta.get("title"), "vintages": len(vint),
                     "windows": len(wins), "parsed_points": len(pts),
                     "observation_periods": len({p[0] for p in pts}),
                     "would_write_obs": len(rows), "would_write_releases": len(rel)}
            except Exception as exc:  # noqa: BLE001
                r = {"series_id": t.series_id, "status": "failed",
                     "error": f"{type(exc).__name__}: {exc}"}
            results.append(r)
            _print_row(r)
    else:
        store = MacroStore(args.db)
        try:
            for t in tasks:
                r = ingest_series(cli, store, t, task_id, raw_dir,
                                 max_windows=args.max_windows)
                r["title"] = (store.conn.execute(
                    "SELECT title FROM macro_series WHERE series_id=?",
                    (t.series_id,)).fetchone() or [None])[0]
                results.append(r)
                _print_row(r)
            print("\n=== 库内计数 ===")
            print(json.dumps(store.counts(), ensure_ascii=False))
        finally:
            store.close()

    print(f"\nREQUESTS={cli.calls}  RATE_LIMITED={cli.retry_429}")
    print(f"RAW_DIR={raw_dir}")
    if args.report:
        p = Path(args.report)
        p.parent.mkdir(parents=True, exist_ok=True)
        blob = json.dumps({"task_id": task_id, "key_source": src,
                           "requests": cli.calls, "rate_limited": cli.retry_429,
                           "results": results}, ensure_ascii=False, indent=2)
        if key in blob:
            print("ABORT: 结果中检出 key 字面量，拒绝写盘")
            return 3
        p.write_text(blob + "\n", encoding="utf-8")
        print(f"REPORT={p}")
    return 0


def _print_row(r: dict) -> None:
    if r.get("status") == "failed":
        print(f"{r['series_id']:16s} {'FAILED':8s} {'-':>8s} {'-':>3s} {'-':>7s} "
              f"{'-':>6s} {'-':>5s} {'-':>6s}  {str(r.get('error'))[:60]}")
        return
    o = r.get("observations") or {}
    print(f"{r['series_id']:16s} {r['status']:8s} {r['vintages']:8d} "
          f"{r['windows']:3d} {r['observation_periods']:7d} "
          f"{o.get('inserted', r.get('would_write_obs', 0)):6d} "
          f"{o.get('updated', 0):5d} {o.get('unchanged', 0):6d}  "
          f"{str(r.get('title') or '')[:34]}")


if __name__ == "__main__":
    raise SystemExit(main())
