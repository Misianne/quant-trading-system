"""汇总 2 小时会话的监控数据，生成报告所需的表格。"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if root is None:
        print("usage: summarize_session.py <run-session-dir>")
        return 2
    rows = []
    for line in (root / "monitor.jsonl").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    snaps = [r for r in rows if r.get("tag") in ("t0", "5min", "30min-health", "before-stop")]
    print("| 时间点 | RSS (MB) | Commit (MB) | Python GC 对象 | 句柄 | 线程 | 段 | 成交 |")
    print("|---|---|---|---|---|---|---|---|")
    seen = set()
    for r in snaps:
        if "rss_mb" not in r:
            continue
        m = round(float(r.get("elapsed_min", 0)))
        bucket = m - (m % 15)
        if bucket in seen and r.get("tag") == "5min":
            continue
        seen.add(bucket)
        print(f"| {m} min | {r['rss_mb']:.1f} | {r['commit_mb']:.1f} | "
              f"{r['gc_objects']} | {r['handles']} | {r['threads']} | "
              f"{r['segments']} | {r['fills']} |")
    rep = json.loads((root / "run_report.json").read_text(encoding="utf-8"))
    print()
    print("final:", json.dumps({k: rep[k] for k in
                                ("ok", "segments", "ticks", "fills", "final_state",
                                 "anomalies")}, ensure_ascii=False))
    print("idle:", json.dumps(rep.get("idle"), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
