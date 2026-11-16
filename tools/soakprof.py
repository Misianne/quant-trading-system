"""复现 cmd_soak 的真实循环（每轮新建 Runtime + Supervisor），观察 RSS 是否线性。"""
import ctypes, gc, json, os, sys, tracemalloc
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import importlib.util
spec = importlib.util.spec_from_file_location("mp", ROOT / "tools" / "memprof.py")
mp = importlib.util.module_from_spec(spec); spec.loader.exec_module(mp)

from ai_trader.config import load_config
from ai_trader.paper_runtime.replay import SCENARIOS, build_runtime_for
from ai_trader.paper_runtime.supervisor import Supervisor
from ai_trader.paper_runtime.timebase_bridge import restore_clock
from ai_trader import timebase

cfg = load_config()
root = Path(os.environ.get("TEMP", ".")) / "soakprof"
root.mkdir(parents=True, exist_ok=True)
keys = ["normal_day", "regular_session", "luld_pause_resume", "mwcb", "premarket", "holiday"]
N = int(sys.argv[1]) if len(sys.argv) > 1 else 300
tracemalloc.start(15)
print(json.dumps(mp.sample("start"), ensure_ascii=False))
total_ticks = total_fills = 0
for c in range(1, N + 1):
    sc = SCENARIOS[keys[(c - 1) % len(keys)]]
    rt, clk, src, model = build_runtime_for(sc, cfg=cfg, out_dir=root / f"c{c:05d}")
    st = rt.start(); rt.to_running()
    sup = Supervisor(rt, run_dir=root / f"c{c:05d}", interval_seconds=0.0,
                     progress_fn=lambda: {"total_ticks": total_ticks})
    for i in range(sc.ticks):
        if i in sc.day_breaks:
            cur = timebase.now(); nxt = cur.date() + timedelta(days=1)
            while nxt.weekday() >= 5: nxt += timedelta(days=1)
            from datetime import datetime as _dt
            clk.set(_dt(nxt.year, nxt.month, nxt.day, 9, 30, tzinfo=cur.tzinfo))
        elif i:
            clk.advance(timedelta(minutes=sc.tick_minutes))
        src.set_tick(i)
        rt.tick(); total_ticks += 1
        sup.step()
    total_fills += len(rt.ledger.fills())
    rt.stop("prof"); restore_clock()
    if c % 50 == 0:
        print(json.dumps(mp.sample(f"cycle-{c}"), ensure_ascii=False), flush=True)
    if c == 50:
        globals()["SNAP_A"] = tracemalloc.take_snapshot()

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.GetCurrentProcess.restype = ctypes.c_void_p
k32.SetProcessWorkingSetSize.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_size_t]
k32.SetProcessWorkingSetSize(k32.GetCurrentProcess(), ctypes.c_size_t(-1).value, ctypes.c_size_t(-1).value)
SNAP_B = tracemalloc.take_snapshot()
print(json.dumps({**mp.sample("after-trim"), "ticks": total_ticks, "fills": total_fills}, ensure_ascii=False))
a = globals().get("SNAP_A")
if a is not None:
    print("=== top diff by lineno ===")
    for st in SNAP_B.compare_to(a, "lineno")[:12]:
        print(f"{st.size_diff/1048576:+8.2f} MB {st.count_diff:+7d}  {st.traceback}")
    print("=== top diff by filename ===")
    for st in SNAP_B.compare_to(a, "filename")[:12]:
        print(f"{st.size_diff/1048576:+8.2f} MB {st.count_diff:+7d}  {st.traceback}")
tracemalloc.stop()
