#!/usr/bin/env python3
"""抓取并入库美股日线（复用既有 fetch_chart + BarStore，不另建数据层）。

为什么需要这个工具
------------------
`us_loop/fetch.py::fetch_and_store` 只返回抓取元数据，**丢掉了** Yahoo 的
`events.splits`。而未复权的 OHLC 序列一旦跨越拆股，就会出现"一夜 -50%"的假跳空，
直接喂给策略会得到完全错误的信号。本工具在**不修改** `fetch.py` / `bars.py`
的前提下：

1. 复用 `fetch_chart()` 取原始 payload；
2. 复用 `BarStore.ingest()` 写入（含全部既有校验与留痕）；
3. **额外**把 `events.splits` 解析出来落盘，供回测窗口选择使用；
4. 复核该标的的疑似拆股跳空，确认没有漏网。

口径
----
写入的是**未复权**原始 OHLCV（BarStore 的既定契约）。拆股不做静默调整 ——
只做显式标记，由回测侧决定"把窗口截断到拆股之后"。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_trader.us_loop.bars import BarStore          # noqa: E402
from ai_trader.us_loop.fetch import (               # noqa: E402
    SOURCE_TAG, _rows_from_chart, fetch_chart, to_yahoo_symbol,
)

DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "market_us.db"


def splits_of(payload: dict) -> list[dict]:
    """从 chart payload 里取拆股事件（官方返回值，不做推断）。"""
    results = ((payload or {}).get("chart") or {}).get("result") or []
    if not results:
        return []
    events = results[0].get("events") or {}
    out = []
    for key, node in (events.get("splits") or {}).items():
        epoch = node.get("date")
        day = (datetime.fromtimestamp(int(epoch), tz=timezone.utc).date().isoformat()
               if epoch else "")
        out.append({
            "date": day,
            "numerator": node.get("numerator"),
            "denominator": node.get("denominator"),
            "splitRatio": node.get("splitRatio"),
            "key": str(key),
        })
    out.sort(key=lambda r: r["date"])
    return out


def ingest_one(store: BarStore, symbol: str, range_: str) -> dict:
    ys = to_yahoo_symbol(symbol)
    payload = fetch_chart(ys, range_=range_, interval="1d")
    rows, meta = _rows_from_chart(payload, symbol)
    if not rows:
        raise RuntimeError(f"{symbol}: Yahoo 未返回任何 bar")
    fetched_at = datetime.now().astimezone().isoformat(timespec="seconds")
    res = store.ingest(symbol, rows, source=SOURCE_TAG, fetched_at=fetched_at)
    meta.update({
        "rows_from_upstream": len(rows),
        "splits": splits_of(payload),
        "ingest": res.as_dict(),
        "fetched_at": fetched_at,
    })
    return meta


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--symbols", default="usQQQ,usQLD,usQID,usTQQQ")
    ap.add_argument("--range", default="5y")
    ap.add_argument("--out", default="data/daily_fetch_report.json")
    args = ap.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    store = BarStore(args.db)
    report = {"db": args.db, "range": args.range, "source": SOURCE_TAG,
              "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
              "symbols": {}}
    try:
        for sym in symbols:
            try:
                report["symbols"][sym] = ingest_one(store, sym, args.range)
                print(f"OK   {sym}: {report['symbols'][sym]['ingest']}")
                print(f"     splits={report['symbols'][sym]['splits']}")
            except Exception as exc:  # noqa: BLE001
                report["symbols"][sym] = {"error": f"{type(exc).__name__}: {exc}"}
                print(f"FAIL {sym}: {type(exc).__name__}: {exc}")
        report["quality"] = store.quality_report(symbols)
    finally:
        store.close()

    report["finished_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nreport -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
