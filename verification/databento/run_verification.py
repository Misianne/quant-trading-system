"""生成本轮 Databento 验证证据（离线可复现）。

产出 ``verification/databento/out/verification.json``，报告里的每个数字都
来自这个文件，避免"报告写一套、实际跑一套"。

它**不能**证明真实行情可用 —— 本轮没有凭据。它证明的是：
官方 schema 形状的样本能否被正确解析、语义判断是否与官方一致、
未签名事件能否被挡住、同一输入是否确定。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "src"))

FX = HERE / "fixtures"
OUT = HERE / "out"


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True,
                              text=True, timeout=30).stdout.strip()
    except Exception as exc:  # noqa: BLE001
        return f"<error: {exc}>"


def _digest(rows) -> str:
    blob = json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def main() -> int:
    from ai_trader.market.providers import (
        AuthorityRouter, DatabentoProvider, ProviderFeed, surveyed_specs)
    from ai_trader.market.providers import databento_schema as ds

    verify: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git": {"head": _git("rev-parse", "--short", "HEAD"),
                "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
                "dirty": bool(_git("status", "--porcelain"))},
        "credential": {
            "env_var": "DATABENTO_API_KEY",
            "present": bool(os.environ.get("DATABENTO_API_KEY")),
            "note": "仅报告存在性，不读取、不记录、不打印取值",
        },
        "schema_facts": {
            "rtype_status": ds.RTYPE_STATUS,
            "rtype_trades": ds.RTYPE_TRADES,
            "status_actions": ds.STATUS_ACTION,
            "halting_actions": sorted(ds.HALTING_ACTIONS),
            "trading_actions": sorted(ds.TRADING_ACTIONS),
            "ambiguous_actions": sorted(ds.AMBIGUOUS_ACTIONS),
            "mwcb_reasons": sorted(ds.MWCB_REASONS),
            "reason_count": len(ds.STATUS_REASON),
            "publisher_count": len(ds.PUBLISHER_VENUE),
            "price_scale": ds.PRICE_SCALE,
            "undef_price": ds.UNDEF_PRICE,
        },
        "samples": {},
        "authority": {},
        "determinism": {},
    }

    # ---- 1) 逐样本解析结果 ------------------------------------------- #
    sym = json.loads((FX / "symbology.json").read_text(encoding="utf-8"))
    for name in sorted(p.name for p in FX.glob("*.jsonl")):
        p = DatabentoProvider(FX / name, {"batch_size": 1000, "symbology": sym})
        p.connect(); p.subscribe(["AAPL"])
        evs = p.poll()
        rows = []
        for e in evs:
            d = e.as_dict()
            row = {"type": d["raw_type"], "symbol": d["symbol"],
                   "exchange": d["exchange"], "event_time": d["event_time"],
                   "receive_time": d["receive_time"],
                   "sequence": d["sequence"], "authority": d["authority"],
                   "usable": d["quality"]["usable"],
                   "flags": d["quality"]["flags"]}
            if "halted" in d:
                row.update({"halted": d["halted"], "code": d["halt_code"],
                            "reason": d["reason"], "is_trading": d["is_trading"],
                            "trading_event": d["trading_event"]})
            if "price" in d:
                row.update({"price": d["price"], "size": d["size"],
                            "side": d["side"], "price_scale": d["price_scale"]})
            rows.append(row)
        verify["samples"][name] = {
            "events": len(rows),
            "ignored_status": dict(p.ignored_status),
            "rejected": p.rejections(),
            "dropped": p.health.dropped,
            "parse_errors": p.health.parse_errors,
            "rows": rows,
        }

    # ---- 2) 权威判定演示 --------------------------------------------- #
    from ai_trader.market.providers.models import HaltEvent, EventAuthority
    T0 = datetime(2026, 11, 16, 14, 30, tzinfo=timezone.utc)
    spec = [s for s in surveyed_specs() if s.provider == "databento"][0]
    r = AuthorityRouter([spec], require_signature_for_resume=True)

    def _ev(halted: bool):
        return HaltEvent(provider="databento", symbol="AAPL", exchange="nasdaq",
                         event_time=T0, receive_time=T0, halted=halted,
                         reason="LULD_PAUSE", halt_code="PAUSE",
                         authority=EventAuthority.EXCHANGE_HALT)

    v_halt = r.can_apply(_ev(True), symbol="AAPL", exchange="nasdaq",
                         now=T0.replace(minute=31))
    v_resume = r.can_apply(_ev(False), symbol="AAPL", exchange="nasdaq",
                           now=T0.replace(minute=31))
    verify["authority"] = {
        "spec": {"provider": spec.provider, "native": spec.native,
                 "signed": spec.signed, "exchanges": list(spec.exchanges)},
        "unsigned_halt": v_halt.as_dict(),
        "unsigned_resume": v_resume.as_dict(),
        "asymmetry_holds": bool(v_halt.accepted and not v_resume.accepted),
    }

    # ---- 3) 确定性 ---------------------------------------------------- #
    def run():
        prov = DatabentoProvider(FX / "synthetic_luld_pause.jsonl",
                                 {"batch_size": 1000, "symbology": sym})
        rr = AuthorityRouter([spec], require_signature_for_resume=True)
        feed = ProviderFeed([prov], authority_router=rr, max_age_seconds=10 ** 9)
        feed.connect_all(); feed.subscribe(["AAPL"])
        evs = [e.as_dict() for e in feed.poll()]
        st = feed.state("AAPL", "nasdaq")
        return {"events": evs, "halted": st.halted, "version": st.version,
                "rejected": feed.rejected}

    digests = [_digest(run()) for _ in range(3)]
    verify["determinism"] = {"runs": len(digests),
                             "distinct": len(set(digests)),
                             "sha256": digests[0],
                             "identical": len(set(digests)) == 1}

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "verification.json").write_text(
        json.dumps(verify, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({
        "head": verify["git"]["head"],
        "credential_present": verify["credential"]["present"],
        "samples": len(verify["samples"]),
        "asymmetry_holds": verify["authority"]["asymmetry_holds"],
        "deterministic": verify["determinism"]["identical"],
        "out": str(OUT / "verification.json"),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
