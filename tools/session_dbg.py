import json, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT/"src"))
from ai_trader.config import load_config
from ai_trader.paper_runtime.session import ReplaySession
cfg = load_config()
s = ReplaySession(cfg=cfg, out_dir=Path(tempfile.mkdtemp(prefix="sdbg-")))
print("start", s.start().ok)
seg = s.run_segment()
print("seg", seg)
eng0 = s.rt.ctx.service("engine")
for t in s.rt._tick_log[:8]:
    print(t["index"], t["scan"], t["notes"][:2])
    if t["scan"].get("rejected"):
        o = eng0.last_scan.outcomes[0]
        print("   detail:", o.detail)
        d = getattr(o, "decision", None)
        if d:
            print("   reasons:", d.get("reasons") if isinstance(d, dict) else d)
eng = s.rt.ctx.service("engine")
print("last scan", eng.last_scan.summary, eng.last_scan.blocked_reason)
if eng.last_scan.outcomes:
    print("detail", eng.last_scan.outcomes[0].detail)
s.stop()
