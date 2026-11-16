import sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT/"src"))
from ai_trader.config import load_config
from ai_trader.paper_runtime.session import ReplaySession
cfg = load_config()
s = ReplaySession(cfg=cfg, out_dir=Path(tempfile.mkdtemp(prefix="sdbg2-")))
print("start", s.start().ok)
seen = []
def hook(rt):
    eng = rt.ctx.service("engine")
    ls = eng.last_scan
    if ls and ls.outcomes:
        o = ls.outcomes[0]
        seen.append((rt._ticks - 1, o.stage, o.detail, (o.decision or {}).get("reasons") if isinstance(o.decision, dict) else None))
s.rt.on_tick = hook
s.run_segment()
for row in seen:
    print(row)
s.stop()
