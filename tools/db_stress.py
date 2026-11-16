"""数据库/文件增长压力：连续高频交易，采样文件体积与恢复一致性。"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ai_trader.config import load_config
from ai_trader.paper_runtime.session import ReplaySession
from ai_trader.paper_runtime.timebase_bridge import restore_clock


def sizes(d: Path) -> dict:
    out = {}
    for p in sorted(Path(d).rglob("*")):
        if p.is_file():
            key = p.suffix or p.name
            if p.name.endswith("-wal"):
                key = "wal"
            elif p.name.endswith("-shm"):
                key = "shm"
            out[key] = out.get(key, 0) + p.stat().st_size
    return {k: round(v / 1024.0, 1) for k, v in out.items()}   # KB


def main() -> int:
    minutes = float(sys.argv[1]) if len(sys.argv) > 1 else 2.0
    out_root = Path(tempfile.mkdtemp(prefix="dbstress-"))
    cfg = load_config()
    s = ReplaySession(cfg=cfg, out_dir=out_root)
    r = s.start()
    if not r.ok:
        print(json.dumps({"ok": False, "reason": r.reason}, ensure_ascii=False))
        return 4

    rows = []
    t0 = time.monotonic()
    next_sample = t0
    while time.monotonic() - t0 < minutes * 60:
        s.run_segment()
        now = time.monotonic()
        if now >= next_sample:
            next_sample = now + 10
            broker = s.rt.ctx.service("broker")
            rows.append({
                "t": round(now - t0, 1),
                "segments": s._segments,
                "orders": len(broker.orders_snapshot()),
                "fills": len(s.rt.ledger.fills()),
                "kb": sizes(out_root),
            })

    # 停止并观察 WAL 是否回收
    before_stop = sizes(out_root)
    s.stop("db_stress_end")
    restore_clock()
    time.sleep(0.5)
    after_stop = sizes(out_root)

    # 恢复一致性
    db = None
    for p in out_root.rglob("paper_audit.db"):
        db = p
    integrity = "missing"
    if db:
        conn = sqlite3.connect(str(db))
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            conn.close()

    fills = s.rt.ledger.fills()
    uniq = len({f.fill_id for f in fills})
    summary = {
        "ok": integrity == "ok" and uniq == len(fills),
        "minutes": minutes,
        "segments": s._segments,
        "ticks": s.rt._ticks,
        "fills": len(fills), "unique_fills": uniq,
        "kb_before_stop": before_stop, "kb_after_stop": after_stop,
        "sqlite_integrity": integrity,
        "invariants_ok": s.rt._last_invariants.ok,
        "samples": rows,
    }
    outp = ROOT / "data" / "paper_runs" / "db-stress-result.json"
    outp.parent.mkdir(parents=True, exist_ok=True)
    outp.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in
                      ("ok", "segments", "ticks", "fills", "unique_fills",
                       "sqlite_integrity", "invariants_ok", "kb_after_stop")},
                     ensure_ascii=False))
    print("out", outp)
    return 0 if summary["ok"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
