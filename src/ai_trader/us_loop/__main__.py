"""US 模拟交易闭环的命令行入口。

用法::

    python -m ai_trader.us_loop fetch   --symbols usAAPL,usNVDA,usTSLA --range 2y
    python -m ai_trader.us_loop status  --trade-date 2026-11-06
    python -m ai_trader.us_loop run     --trade-date 2026-11-06
    python -m ai_trader.us_loop evidence --trade-date 2026-11-06 --out audit_evidence/us_loop
    python -m ai_trader.us_loop fetch   # 只抓数据

所有子命令都只落 ``data/`` 下的新文件；``--sandbox DIR`` 会把全部状态移到 DIR。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import List, Optional

from ..config import PROJECT_ROOT
from .bars import BarStore
from .fetch import FetchError, fetch_and_store
from .runner import (DEFAULT_DECISION_TIME, US_SYMBOLS, LoopSettings, PaperLoop,
                     decision_moment, last_trading_day_on_or_before, restart_proof)


def _symbols(text: Optional[str]) -> List[str]:
    if not text:
        return list(US_SYMBOLS)
    return [s.strip() for s in text.split(",") if s.strip()]


def _settings(args: argparse.Namespace) -> LoopSettings:
    data_dir = PROJECT_ROOT / "data"
    bars_db = data_dir / "market_us.db"
    loop_db = data_dir / "us_loop.db"
    if args.sandbox:
        sandbox = Path(args.sandbox).resolve()
        sandbox.mkdir(parents=True, exist_ok=True)
        data_dir = sandbox
        bars_db = Path(args.bars_db).resolve() if args.bars_db else sandbox / "market_us.db"
        loop_db = sandbox / "us_loop.db"
    elif args.bars_db:
        bars_db = Path(args.bars_db).resolve()

    if args.trade_date:
        td = date.fromisoformat(args.trade_date)
    else:
        store = BarStore(bars_db)
        try:
            last = store.load(US_SYMBOLS[0], None, 1)
            td = date.fromisoformat(last[-1]["trade_date"]) if last else date.today()
        finally:
            store.close()

    at = DEFAULT_DECISION_TIME
    if args.at:
        hh, mm = args.at.split(":")
        at = dtime(int(hh), int(mm))
    return LoopSettings(
        trade_date=td, symbols=_symbols(args.symbols), data_dir=data_dir,
        bars_db=bars_db, loop_db=loop_db, mode=args.mode, sandbox=bool(args.sandbox),
        decision_time=at,
    )


def cmd_fetch(args: argparse.Namespace) -> int:
    store = BarStore(Path(args.bars_db).resolve() if args.bars_db else
                     Path(args.sandbox).resolve() / "market_us.db" if args.sandbox else
                     PROJECT_ROOT / "data" / "market_us.db")
    rc = 0
    out = []
    for sym in _symbols(args.symbols):
        try:
            meta = fetch_and_store(store, sym, range_=args.range)
            out.append(meta)
            ing = meta["ingest"]
            print(f"[ok] {sym} bars={meta['bars']} {meta['first']}..{meta['last']} "
                  f"written={ing['rows_written']} unchanged={ing['rows_unchanged']} "
                  f"rejected={ing['rows_rejected']} src={meta['source']}")
        except FetchError as exc:
            rc = 2
            print(f"[FAIL] {sym}: {exc} (code={exc.code})", file=sys.stderr)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(out, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
        print(f"meta -> {args.json_out}")
    store.close()
    return rc


def cmd_status(args: argparse.Namespace) -> int:
    loop = PaperLoop(_settings(args))
    try:
        print(json.dumps(loop.status(), ensure_ascii=False, indent=2, default=str))
    finally:
        loop.close()
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    s = _settings(args)
    loop = PaperLoop(s)
    try:
        now = decision_moment(s.trade_date, s.decision_time)
        res = loop.run(now=now)
        print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
        return 0 if res.get("run_status") == "ok" else 3
    finally:
        loop.close()


def cmd_evidence(args: argparse.Namespace) -> int:
    s = _settings(args)
    loop = PaperLoop(s)
    try:
        ev = loop.evidence()
        ev["restart"] = restart_proof(s)
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = out_dir / f"us_loop_evidence_{stamp}.json"
        path.write_text(json.dumps(ev, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8")
        print(f"evidence -> {path}")
        print(json.dumps({"readiness": ev["readiness"],
                          "restart_identical": ev["restart"]["identical"]},
                         ensure_ascii=False, indent=2))
        return 0
    finally:
        loop.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ai_trader.us_loop",
                                description="美股模拟交易闭环（真实历史行情 + 真实 LLM + Paper Broker）")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--trade-date", help="交易日 YYYY-MM-DD（默认：库中最后一根 bar 的日期）")
        sp.add_argument("--symbols", help="逗号分隔，默认 usAAPL,usNVDA,usTSLA")
        sp.add_argument("--at", help="决策时刻 HH:MM（NY 时区，默认 10:30）")
        sp.add_argument("--mode", default="replay", help="replay / realtime")
        sp.add_argument("--sandbox", help="把全部状态放到该目录（隔离实验用）")
        sp.add_argument("--bars-db", help="指定 bar 库路径")

    f = sub.add_parser("fetch", help="抓取真实美股日线（Yahoo 公开端点）")
    common(f)
    f.add_argument("--range", default="2y")
    f.add_argument("--json-out")
    f.set_defaults(func=cmd_fetch)

    st = sub.add_parser("status", help="打印闭环状态与就绪度")
    common(st)
    st.set_defaults(func=cmd_status)

    r = sub.add_parser("run", help="跑一次完整闭环（AI → 风控 → 模拟成交 → 账本）")
    common(r)
    r.set_defaults(func=cmd_run)

    ev = sub.add_parser("evidence", help="导出证据包（含重启恢复校验）")
    common(ev)
    ev.add_argument("--out", default=str(PROJECT_ROOT / "audit_evidence" / "us_loop"))
    ev.set_defaults(func=cmd_evidence)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
