import json, os, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from ai_trader.config import load_config
from ai_trader.paper_runtime.session import ReplaySession

cfg = load_config()
out = Path(tempfile.mkdtemp(prefix="sess-"))
s = ReplaySession(cfg=cfg, out_dir=out)
r = s.start()
print("start", r.ok, r.reason)
kinds = []
for i in range(30):
    seg = s.run_segment()
    kinds.append((seg["kind"], seg["fills_delta"], seg["state"]))
print("segments:", kinds[:10])
print("summary:", json.dumps(s.summary(), ensure_ascii=False)[:400])
print("stop:", s.stop().ok)
