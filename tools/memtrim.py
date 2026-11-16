"""验证 RSS 增长是否为「工作集缓存」而非活对象：跑 N 轮后 EmptyWorkingSet 再测。"""
import ctypes, json, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
code = (ROOT / "tools" / "memprof.py").read_text(encoding="utf-8")

# 复用 memprof 的 run_cycle/sample，但结束前 trim
sys.path.insert(0, str(ROOT / "src"))
sys.argv = ["memprof", "--cycles", "0"]
import importlib.util
spec = importlib.util.spec_from_file_location("mp", ROOT / "tools" / "memprof.py")
mp = importlib.util.module_from_spec(spec); spec.loader.exec_module(mp)

import tracemalloc
from ai_trader.config import load_config
cfg = load_config()
root = Path(__import__("os").environ.get("TEMP", ".")) / "memtrim"
root.mkdir(parents=True, exist_ok=True)
keys = ["normal_day", "regular_session", "luld_pause_resume", "mwcb"]
tracemalloc.start(10)
print(json.dumps(mp.sample("start"), ensure_ascii=False))
for c in range(1, 201):
    mp.run_cycle(cfg, keys[(c - 1) % len(keys)], root, c)
    if c % 100 == 0:
        print(json.dumps(mp.sample(f"cycle-{c}"), ensure_ascii=False), flush=True)

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.GetCurrentProcess.restype = ctypes.c_void_p
k32.SetProcessWorkingSetSize.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_size_t]
ok = k32.SetProcessWorkingSetSize(k32.GetCurrentProcess(), ctypes.c_size_t(-1).value, ctypes.c_size_t(-1).value)
print(json.dumps({"trim_ok": bool(ok), **mp.sample("after-trim")}, ensure_ascii=False))
tracemalloc.stop()
