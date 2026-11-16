import collections, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT/"src"))
from ai_trader.config import load_config
from ai_trader.paper_runtime.session import ReplaySession
cfg = load_config()
s = ReplaySession(cfg=cfg, out_dir=Path(tempfile.mkdtemp(prefix="sdbg3-")))
s.start()
reasons = collections.Counter()
detail = {}
def hook(rt):
    eng = rt.ctx.service("engine"); ls = eng.last_scan
    if ls and ls.outcomes:
        o = ls.outcomes[0]
        if o.stage in ("rejected", "error"):
            key = (o.decision or {}).get("reasons") if isinstance(o.decision, dict) else [o.detail]
            reasons[str(key)] += 1
            detail.setdefault(str(key), o.detail)
s.rt.on_tick = hook
for i in range(120):
    s.run_segment()
print("fills", len(s.rt.ledger.fills()), "orders", len(s.rt.ctx.service("broker").orders_snapshot()))
for k, v in reasons.most_common(6):
    print(v, k[:160])
s.stop()
