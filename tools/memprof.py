"""PaperRuntime 内存剖析：区分「真泄漏 / 缓存增长 / 重复创建 / 分配器保留」。

用法::

    python tools/memprof.py --cycles 400 --every 100

每 ``--every`` 轮输出一行指标，并在首尾各取一次 tracemalloc 快照，
结束时打印**增量最大的分配点**（按文件:行号归并）。

指标口径
--------
* RSS / Commit：Windows `K32GetProcessMemoryInfo` 的 WorkingSetSize / PagefileUsage
* Python 当前/峰值：``tracemalloc.get_traced_memory()``（只统计 Python 分配）
* GC：``gc.get_count()``（三代阈值计数）与各代对象数
* 句柄：``GetProcessHandleCount``
* SQLite 连接：GC 中统计 ``sqlite3.Connection`` 实例
* 实例计数：GC 中按类名统计（Runtime/Engine/Broker/Journal/Quote/Signal…）
"""
from __future__ import annotations

import argparse
import ctypes
import gc
import json
import os
import sqlite3
import sys
import threading
import tracemalloc
from ctypes import wintypes
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from ai_trader.config import load_config                     # noqa: E402
from ai_trader.paper_runtime.replay import SCENARIOS, build_runtime_for  # noqa: E402
from ai_trader.paper_runtime.timebase_bridge import restore_clock       # noqa: E402
from ai_trader import timebase                              # noqa: E402

WATCH = ("PaperRuntime", "StrategyEngine", "PaperBroker", "AuditJournal",
         "MarketStatusService", "SettlementLedger", "ShortGuard", "Quote",
         "Fill", "TradingSignal", "MarketContext", "LuldLifecycle",
         "SymbolStatus", "ComponentRegistry")


# ---------------------------------------------------------------------- #
class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t)]


def mem() -> dict:
    out = {"rss_mb": -1.0, "commit_mb": -1.0, "peak_rss_mb": -1.0}
    if sys.platform == "win32":
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.GetCurrentProcess.argtypes = []
        fn = k32.K32GetProcessMemoryInfo
        fn.restype = wintypes.BOOL
        fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
                       wintypes.DWORD]
        c = PROCESS_MEMORY_COUNTERS()
        c.cb = ctypes.sizeof(c)
        if fn(k32.GetCurrentProcess(), ctypes.byref(c), c.cb):
            out = {"rss_mb": round(c.WorkingSetSize / 1048576, 2),
                   "commit_mb": round(c.PagefileUsage / 1048576, 2),
                   "peak_rss_mb": round(c.PeakWorkingSetSize / 1048576, 2)}
    return out


def handle_count() -> int:
    if sys.platform != "win32":
        return -1
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.GetCurrentProcess.argtypes = []
    k32.GetProcessHandleCount.restype = wintypes.BOOL
    k32.GetProcessHandleCount.argtypes = [wintypes.HANDLE,
                                          ctypes.POINTER(wintypes.DWORD)]
    n = wintypes.DWORD(0)
    k32.GetProcessHandleCount(k32.GetCurrentProcess(), ctypes.byref(n))
    return int(n.value)


def count_objects() -> dict:
    """按类名统计 GC 中的实例数。"""
    counts = {n: 0 for n in WATCH}
    conns = 0
    for o in gc.get_objects():
        t = type(o).__name__
        if t in counts:
            counts[t] += 1
        elif t == "Connection" and isinstance(o, sqlite3.Connection):
            conns += 1
    counts["sqlite3.Connection"] = conns
    return counts


def sample(tag: str) -> dict:
    cur, peak = tracemalloc.get_traced_memory() if tracemalloc.is_tracing() else (0, 0)
    d = {"tag": tag, "rss_mb": None, "commit_mb": None,
         "py_current_mb": round(cur / 1048576, 3),
         "py_peak_mb": round(peak / 1048576, 3),
         "gc_count": list(gc.get_count()),
         "gc_objects_gen": [len(gc.get_objects(generation=g)) for g in (0, 1, 2)],
         "threads": threading.active_count(),
         "handles": handle_count(),
         "objects": count_objects()}
    d.update(mem())
    return d


# ---------------------------------------------------------------------- #
def run_cycle(cfg, key: str, root: Path, cycle: int) -> None:
    sc = SCENARIOS[key]
    out = root / f"c{cycle:05d}"
    rt, clk, src, model = build_runtime_for(sc, cfg=cfg, out_dir=out)
    try:
        st = rt.start()
        if not st.ok:
            raise RuntimeError(f"start failed: {st.reason}")
        rt.to_running()
        for i in range(sc.ticks):
            if i in sc.day_breaks:
                cur = timebase.now()
                nxt = cur.date() + timedelta(days=1)
                while nxt.weekday() >= 5:
                    nxt += timedelta(days=1)
                from datetime import datetime as _dt
                clk.set(_dt(nxt.year, nxt.month, nxt.day, 9, 30, tzinfo=cur.tzinfo))
            elif i:
                clk.advance(timedelta(minutes=sc.tick_minutes))
            src.set_tick(i)
            if sc.use_luld:
                ms = rt.ctx.service("market_status")
                now = timebase.now()
                for sym, series in sc.price_series().items():
                    prior = float(sc.prior_close.get(sym, series[0]))
                    if i == 0:
                        ms.update_reference(sym, prior, prior_close=prior)
                    ms.observe_trade(sym, float(series[min(i, len(series) - 1)]),
                                     now, bounds=sc.bounds(), prior_close=prior)
            rt.tick()
    finally:
        rt.stop("memprof")
        restore_clock()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycles", type=int, default=400)
    ap.add_argument("--every", type=int, default=100)
    ap.add_argument("--out", default=None)
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()

    cfg = load_config()
    root = Path(args.out or (Path(os.environ.get("TEMP", ".")) / "memprof"))
    root.mkdir(parents=True, exist_ok=True)
    keys = ["normal_day", "regular_session", "luld_pause_resume", "mwcb",
            "premarket", "holiday"]

    tracemalloc.start(25)
    gc.collect()
    base = sample("cycle-0")
    rows = [base]
    snaps = [(0, tracemalloc.take_snapshot())]
    print(json.dumps(base, ensure_ascii=False))

    for c in range(1, args.cycles + 1):
        run_cycle(cfg, keys[(c - 1) % len(keys)], root, c)
        if c % args.every == 0:
            s = sample(f"cycle-{c}")
            rows.append(s)
            snaps.append((c, tracemalloc.take_snapshot()))
            print(json.dumps(s, ensure_ascii=False), flush=True)
            gc.collect()

    # 末次统计（GC 后）
    final = sample("final-after-gc")
    rows.append(final)
    print(json.dumps(final, ensure_ascii=False))

    print("\n=== tracemalloc top diff (first → last) ===")
    if len(snaps) >= 2:
        a, b = snaps[0][1], snaps[-1][1]
        diff = b.compare_to(a, "lineno")
        for stat in diff[:args.top]:
            print(f"{stat.size_diff/1048576:+8.2f} MB  {stat.count_diff:+7d}  {stat.traceback}")
        print("\n=== tracemalloc top diff by filename ===")
        diff2 = b.compare_to(a, "filename")
        for stat in diff2[:args.top]:
            print(f"{stat.size_diff/1048576:+8.2f} MB  {stat.count_diff:+7d}  {stat.traceback}")

    if args.out or True:
        p = root / "memprof-result.json"
        p.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nresult -> {p}")
    tracemalloc.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
