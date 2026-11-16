import sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT/"src"))
from ai_trader.config import load_config
from ai_trader.paper_runtime.session import ReplaySession
cfg = load_config()
s = ReplaySession(cfg=cfg, out_dir=Path(tempfile.mkdtemp(prefix="sdbg4-")))
s.start()
for n in range(1, 12):
    row = s.run_segment()
    broker = s.rt.ctx.service("broker")
    pos = {k: v["qty"] for k, v in broker._state["positions"].items() if float(v["qty"])}
    print(f"seg{n:02d} {row['date']} kind={row['kind']:5s} fills+={row['fills_delta']} pos={pos}")
s.stop()
