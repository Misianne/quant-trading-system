"""Databento 历史样本探针（费用门禁版）。

设计原则：**先算钱，再取数；算不出钱就不取数。**

调用顺序被硬编码成三步，不可跳过：

1. 读凭据（只从环境变量，绝不从命令行参数、文件、报告）；
2. ``metadata.get_cost`` —— 官方**免费**的估价接口，得到本次请求的美元成本；
3. 只有当 ``成本 <= --max-cost-usd``（默认 **0.0**）且显式给了 ``--confirm``
   时才真正调用 ``timeseries.get_range``。

任何一步不满足就退出，不触网取数：
* 无凭据 → exit 3
* 估价失败 → exit 4（**算不出钱就等于不许取**）
* 估价超限 → exit 5
* 缺 ``--confirm`` → exit 6

为什么默认上限是 0.0 而不是"看看多少钱再说"：本轮明确要求 **0 美元实际扣款**。
Databento 的计费是按量从额度扣，额度耗尽后会走银行卡 —— 「先取一小段应该不贵」
这种想法正是超支的来源。上限 0 意味着**只有完全免费（成本估价为 0）的请求才放行**。

安全：API Key 只出现在请求头里，不打印、不落盘、不进异常信息。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

BASE = "https://hist.databento.com/v0"
KEY_ENV = "DATABENTO_API_KEY"

#: 本轮调查过的最小可用组合：XNAS.ITCH 有原生 status，且按量计费最便宜。
DEFAULT_DATASET = "XNAS.ITCH"


class Blocked(Exception):
    def __init__(self, code: str, message: str, exit_code: int):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


def _key() -> str:
    """只从环境变量取凭据。**不接受命令行传入**。"""
    key = os.environ.get(KEY_ENV, "").strip()
    if not key:
        raise Blocked("NO_CREDENTIAL",
                      f"环境变量 {KEY_ENV} 未设置；本工具不会读取任何文件或命令行参数里的密钥",
                      3)
    if len(key) < 8:
        raise Blocked("BAD_CREDENTIAL", f"{KEY_ENV} 长度异常（{len(key)}）", 3)
    return key


def _http_get(url: str, key: str, timeout: float = 30.0) -> bytes:
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "User-Agent": "fosunai-databento-probe/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        body = b""
        try:
            body = exc.read()[:400]
        except Exception:      # noqa: BLE001
            pass
        # 绝不把 Authorization 头带进异常信息
        raise Blocked("HTTP_ERROR",
                      f"{exc.code} {exc.reason} :: {body.decode('utf-8', 'replace')}",
                      4) from None
    except urllib.error.URLError as exc:
        raise Blocked("NETWORK_ERROR", f"无法连接 Databento：{exc.reason}", 4) from None


def estimate_cost(key: str, *, dataset: str, schema: str, symbols: str,
                  start: str, end: str, stype_in: str = "raw_symbol") -> Tuple[float, Dict[str, Any]]:
    """调用官方免费估价接口，返回 ``(美元成本, 原始响应)``。"""
    qs = urllib.parse.urlencode({
        "dataset": dataset, "schema": schema, "symbols": symbols,
        "start": start, "end": end, "stype_in": stype_in,
    })
    raw = _http_get(f"{BASE}/metadata.get_cost?{qs}", key)
    text = raw.decode("utf-8", "replace")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise Blocked("COST_UNPARSEABLE", f"估价响应不是 JSON：{text[:200]}", 4) from None
    # 官方返回形如 {"cost": 0.000123} 或嵌套在 records 里；两种都认
    cost = data.get("cost")
    if cost is None and isinstance(data.get("result"), dict):
        cost = data["result"].get("cost")
    if cost is None:
        raise Blocked("COST_MISSING", f"估价响应里没有 cost 字段：{text[:200]}", 4)
    try:
        return float(cost), data
    except (TypeError, ValueError):
        raise Blocked("COST_NOT_NUMERIC", f"cost 不是数字：{cost!r}", 4) from None


def get_range(key: str, *, dataset: str, schema: str, symbols: str,
              start: str, end: str, limit: int,
              stype_in: str = "raw_symbol", encoding: str = "json") -> bytes:
    qs = urllib.parse.urlencode({
        "dataset": dataset, "schema": schema, "symbols": symbols,
        "start": start, "end": end, "stype_in": stype_in,
        "encoding": encoding, "limit": limit,
    })
    return _http_get(f"{BASE}/timeseries.get_range?{qs}", key)


def sanitize_rows(blob: bytes) -> Tuple[list, Dict[str, Any]]:
    """把响应拆成 JSONL 行，并统计字段形状。

    只做**只读统计**，不改写任何字段 —— 样本要能原样用于交叉核对。
    """
    rows = []
    for line in blob.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    keys: Dict[str, int] = {}
    for r in rows:
        for k in r:
            keys[k] = keys.get(k, 0) + 1
    return rows, {"rows": len(rows), "field_presence": keys}


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="Databento 历史样本探针（0 美元门禁）")
    ap.add_argument("--dataset", default=DEFAULT_DATASET)
    ap.add_argument("--schema", default="status")
    ap.add_argument("--symbols", default="AAPL")
    ap.add_argument("--start", required=False,
                    help="ISO8601，例如 2026-11-16T14:30:00Z")
    ap.add_argument("--end", required=False)
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--max-cost-usd", type=float, default=0.0,
                    help="允许的最大美元成本；默认 0 表示只允许完全免费的请求")
    ap.add_argument("--confirm", action="store_true",
                    help="确认真的取数（缺省只做估价）")
    ap.add_argument("--out", default="verification/databento/raw")
    args = ap.parse_args(argv)

    out_dir = Path(args.out)
    report: Dict[str, Any] = {
        "tool": "databento_probe",
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dataset": args.dataset, "schema": args.schema,
        "symbols": args.symbols, "start": args.start, "end": args.end,
        "max_cost_usd": args.max_cost_usd,
    }
    try:
        key = _key()
        report["credential"] = "present"

        if not args.start or not args.end:
            raise Blocked("MISSING_RANGE", "必须同时给出 --start 与 --end", 2)

        cost, raw_cost = estimate_cost(
            key, dataset=args.dataset, schema=args.schema, symbols=args.symbols,
            start=args.start, end=args.end)
        report["estimated_cost_usd"] = cost
        report["cost_response"] = raw_cost
        print(json.dumps({"step": "cost", "usd": cost}, ensure_ascii=False))

        if cost > args.max_cost_usd:
            raise Blocked(
                "COST_EXCEEDS_CAP",
                f"估价 {cost} USD 超过上限 {args.max_cost_usd} USD；不取数", 5)
        if not args.confirm:
            raise Blocked("NO_CONFIRM",
                          f"估价 {cost} USD 在上限内，但未给 --confirm；仅估价不取数", 6)

        blob = get_range(key, dataset=args.dataset, schema=args.schema,
                         symbols=args.symbols, start=args.start, end=args.end,
                         limit=args.limit)
        rows, stats = sanitize_rows(blob)
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        raw_path = out_dir / f"{args.dataset.replace('.', '_')}-{args.schema}-{stamp}.jsonl"
        raw_path.write_bytes(blob)
        digest = hashlib.sha256(blob).hexdigest()
        report.update({"raw_file": str(raw_path), "sha256": digest, **stats})
        (out_dir / f"{raw_path.stem}.meta.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"ok": True, "rows": stats["rows"], "sha256": digest},
                         ensure_ascii=False))
        return 0

    except Blocked as exc:
        report.update({"blocked": exc.code, "detail": str(exc)})
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "probe-blocked.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"blocked": exc.code, "detail": str(exc)},
                         ensure_ascii=False))
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
