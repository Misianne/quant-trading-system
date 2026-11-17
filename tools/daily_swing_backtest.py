#!/usr/bin/env python3
"""日线波段回测运行器：产出可复现的回测报告（JSON + 控制台摘要）。

只读 `data/market_us.db` 的 `us_bars`，不写库、不下单、不连券商。
用法::

    python tools/daily_swing_backtest.py [--out reports/daily_swing_backtest.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import yaml  # noqa: E402

from ai_trader.market import daily_gate as gate          # noqa: E402
from ai_trader.strategy import daily_swing as ds        # noqa: E402
from ai_trader.strategy.daily_backtest import DailyBacktester  # noqa: E402
from ai_trader.us_loop.bars import BarStore              # noqa: E402

SYMS = [ds.SYM_QQQ, ds.SYM_QLD, ds.SYM_QID, ds.SYM_TQQQ]

VARIANTS = [
    ("A_qqq_only",        dict(allow_qld=False, allow_qid=False)),
    ("B_plus_qld",        dict(allow_qld=True,  allow_qid=False)),
    ("C_plus_qld_qid",    dict(allow_qld=True,  allow_qid=True)),
    ("D_qqq_fee_x2",      dict(allow_qld=False, allow_qid=False, fee_per_side=2.0)),
    ("E_qqq_fee_x5",      dict(allow_qld=False, allow_qid=False, fee_per_side=5.0)),
    ("F_qqq_slip_x4",     dict(allow_qld=False, allow_qid=False, slippage_bps=20.0)),
    ("G_qqq_whole_share", dict(allow_qld=False, allow_qid=False, fractional=False)),
]


def data_quality(store: BarStore) -> dict:
    cfg = yaml.safe_load((ROOT / "config" / "config.yaml").read_text(encoding="utf-8"))
    us = cfg["market_rules"]["calendar"]["markets"]["us"]
    holidays = set(us["holidays"])
    cal = set(gate.intersect_calendar(store, SYMS))
    cov = {s: store.coverage(s) for s in SYMS}
    gaps = gate.weekday_gaps(store, SYMS)

    # 用官方节假日表校验 2026 段（config 只覆盖 2026-01-01 起）
    lo, hi = "2026-01-01", max(cal)
    bad_hol, bad_day = [], []
    from datetime import date, timedelta
    d = date.fromisoformat(lo)
    while d <= date.fromisoformat(hi):
        s = d.isoformat()
        if d.weekday() < 5:
            if s in holidays and s in cal:
                bad_hol.append(s)
            if s not in holidays and s not in cal:
                bad_day.append(s)
        d += timedelta(days=1)

    return {
        "db": str(store.path),
        "symbols": cov,
        "common_sessions": len(cal),
        "first_common": min(cal) if cal else "",
        "last_common": max(cal) if cal else "",
        "weekday_gaps_vs_common_calendar": {k: len(v) for k, v in gaps.items()},
        "official_calendar_check": {
            "window": [lo, hi],
            "holiday_dates_with_bars": bad_hol,
            "trading_dates_missing_bars": bad_day,
            "holiday_count_in_config": len(holidays),
            "covered_from": us.get("covered_from"),
            "covered_until": us.get("covered_until"),
            "source": us.get("source"),
        },
        "split_like_jumps": {s: store.detect_split_like_jumps(s, num=100000) for s in SYMS},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "market_us.db"))
    ap.add_argument("--initial-cash", type=float, default=300.0)
    ap.add_argument("--out", default=str(ROOT / "reports" / "daily_swing_backtest.json"))
    args = ap.parse_args()

    store = BarStore(args.db)
    try:
        quality = data_quality(store)
        bars = {s: store.load(s, num=100000) for s in SYMS}
        last = quality["last_common"]
        g = gate.check(store, SYMS, last).as_dict()
    finally:
        store.close()

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "strategy_version": ds.STRATEGY_VERSION,
        "initial_cash": args.initial_cash,
        "fee_per_side": 1.0,
        "slippage_bps": 5.0,
        "data_gate_at_last_session": g,
        "data_quality": quality,
        "variants": {},
    }

    print(f"{'方案':22s} {'净收益':>9s} {'回撤':>8s} {'笔数':>4s} {'胜率':>6s} "
          f"{'PF':>6s} {'每笔期望':>8s} {'费用':>6s} {'空仓':>6s}")
    for name, kw in VARIANTS:
        res = DailyBacktester(bars, initial_cash=args.initial_cash, **kw).run()
        m = res.metrics
        report["variants"][name] = {"params": kw, "metrics": m,
                                    "trades": [t.as_dict() for t in res.trades],
                                    "orders": [o.as_dict() for o in res.orders],
                                    "decisions": res.decisions,
                                    "equity_curve": res.equity_curve}
        pf = m["profit_factor"]
        pf_s = "inf" if pf == float("inf") else (f"{pf:.2f}" if pf is not None else "n/a")
        print(f"{name:22s} {m['net_return']:+8.2%} {m['max_drawdown']:8.2%} "
              f"{m['trades']:4d} {m['win_rate']:6.1%} {pf_s:>6s} "
              f"{m['expectancy_per_trade']:8.2f} {m['fees_paid']:6.0f} "
              f"{m['cash_time_ratio']:6.1%}")

    a = report["variants"]["A_qqq_only"]["metrics"]
    print(f"\n基准（QQQ 买入持有）: {a['benchmark_qqq']:+.2%}，"
          f"最大回撤 {a['benchmark_max_drawdown']:.2%}")
    print(f"数据：{quality['first_common']} .. {quality['last_common']}，"
          f"公共交易日 {quality['common_sessions']}，闸门 {g['code']}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nreport -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
