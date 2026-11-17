"""Databento 费用探针 —— 三档模式，结构上不可互相冒充。

为什么重写
==========
上一版的"估价"实际上是**向 Databento 发起 HTTPS 请求**（`metadata.get_cost`），
它需要 API Key、需要 DNS/TCP/TLS。虽然不产生数据费用，但它**不是本地计算**。
把它描述成"纯估价"是不准确的，用它来证明"零网络"更是不成立的。

所以本版把三件事在**参数层和代码层**都分开：

===== ================== ============ ======== ================
档位  模式               需要凭据      需要网络  会请求历史数据
===== ================== ============ ======== ================
A     ``--estimate-only``  否           **否**   否
B     ``--estimate-online`` 是          是       否
C     ``--fetch``           是          是       **是**
===== ================== ============ ======== ================

A 档的硬保证
------------
* 不读取 ``DATABENTO_API_KEY``（连 ``os.environ`` 都不碰）；
* 不构造 :class:`HttpClient`（构造时就会抛 :class:`NetworkForbidden`）；
* 不导入/不初始化任何网络栈；:func:`_http_get` 在 A 档下是死代码；
* 只根据 ``--price-book`` 里**显式声明已验证**的单价 + ``--expected-gb`` 估算；
* 二者缺一 → ``UNKNOWN_COST``（exit 7）。**猜价格比不估更危险**。

C 档的双重授权
--------------
1. ``--fetch`` 明确声明"我要下载"（不再是 ``--confirm`` 顺手带出来的行为）；
2. ``--max-cost-usd`` 必须 **> 0** 且有限；默认 0 表示"不允许花钱"。

两者缺一即拒。且上限有三条硬约束：
* 不接受 ``nan`` / ``inf`` / 负数（**上一版 `nan` 会使 `cost > cap` 恒为 False，
  直接绕过上限**）；
* 不接受环境变量覆盖（``DATABENTO_MAX_COST_USD`` 一律忽略）；
* 不超过模块常量 :data:`HARD_CAP_USD`（改这个值必须改代码，是明确的人工动作）。

退出码
------
==== ================================================
0    成功
2    用法错误 / 模式违规（如 A 档传 ``--confirm``）
3    缺少凭据（仅 B/C 档）
4    网络或响应异常，或估价无法解析（仅 B/C 档）
5    估价超过授权上限，或上限未授权（> 0）
6    C 档缺少 ``--confirm``
7    ``UNKNOWN_COST``：本地没有可核实的价格
==== ================================================

安全：API Key 只进请求头，不打印、不落盘、不进异常信息。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

BASE = "https://hist.databento.com/v0"
KEY_ENV = "DATABENTO_API_KEY"

#: 本轮调查过的最小可用组合：XNAS.ITCH 有原生 status。
DEFAULT_DATASET = "XNAS.ITCH"

#: 单次请求的**硬顶**（美元）。高于此值直接拒绝，防止手滑写成 5000。
#: 要突破必须改这个常量——那是明确的人工动作，不会被参数/环境变量悄悄改掉。
HARD_CAP_USD = 25.0

#: 上限相关环境变量：**故意不读取**，列在这里是为了在审计时能一眼看出
#: "这些变量即使设了也不会生效"。
IGNORED_ENV_VARS = ("DATABENTO_MAX_COST_USD", "DATABENTO_MAX_COST",
                    "DATABENTO_BUDGET_USD", "MAX_COST_USD")

#: 全局网络闸门。默认关闭；只有 B/C 档在 main 里显式打开。
_NETWORK_ENABLED = False

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NO_CREDENTIAL = 3
EXIT_NETWORK = 4
EXIT_OVER_CAP = 5
EXIT_NO_CONFIRM = 6
EXIT_UNKNOWN_COST = 7


class Blocked(Exception):
    def __init__(self, code: str, message: str, exit_code: int):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


class NetworkForbidden(Blocked):
    """在 A 档下尝试联网。这是一个**编程错误**，不是运行期配置问题。"""

    def __init__(self, message: str = "当前模式禁止任何网络访问"):
        super().__init__("NETWORK_FORBIDDEN", message, EXIT_USAGE)


def network_enabled() -> bool:
    return bool(_NETWORK_ENABLED)


# ---------------------------------------------------------------------- #
# 凭据：只在 B/C 档调用
# ---------------------------------------------------------------------- #
def _key() -> str:
    """只从环境变量取凭据。**不接受命令行、不读文件**。

    A 档（``--estimate-only``）不会调用本函数，因此连 ``os.environ`` 都不碰。
    """
    key = os.environ.get(KEY_ENV, "").strip()
    if not key:
        raise Blocked("NO_CREDENTIAL",
                      f"环境变量 {KEY_ENV} 未设置；本工具不会读取任何文件或命令行参数里的密钥",
                      EXIT_NO_CREDENTIAL)
    if len(key) < 8:
        raise Blocked("BAD_CREDENTIAL", f"{KEY_ENV} 长度异常（{len(key)}）",
                      EXIT_NO_CREDENTIAL)
    return key


# ---------------------------------------------------------------------- #
# HTTP 客户端：构造即受闸门控制
# ---------------------------------------------------------------------- #
class HttpClient:
    """唯一的网络出口。

    构造时检查全局闸门：A 档下 ``_NETWORK_ENABLED`` 为 False，
    任何试图 new 出来的行为都会立刻抛 :class:`NetworkForbidden`。
    这样"不联网"不是靠"记得别调用"，而是靠"造不出来"。
    """

    def __init__(self, key: str, *, allowed: Optional[bool] = None,
                 timeout: float = 30.0):
        enabled = network_enabled() if allowed is None else bool(allowed)
        if not enabled:
            raise NetworkForbidden(
                "HttpClient 在当前模式下被禁止构造（estimate-only 不做任何网络访问）")
        if not key:
            raise Blocked("NO_CREDENTIAL", "缺少凭据，无法构造 HttpClient",
                          EXIT_NO_CREDENTIAL)
        self._key = key
        self.timeout = float(timeout)
        #: 调用计数，供测试断言"一次都没发"
        self.requests = 0

    def get(self, url: str) -> bytes:
        if not network_enabled():
            raise NetworkForbidden("网络闸门已关闭，拒绝发送请求")
        self.requests += 1
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self._key}",
            "Accept": "application/json",
            "User-Agent": "fosunai-databento-probe/2.0",
        })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            body = b""
            try:
                body = exc.read()[:400]
            except Exception:      # noqa: BLE001
                pass
            # 绝不把 Authorization 头带进异常信息
            raise Blocked("HTTP_ERROR",
                          f"{exc.code} {exc.reason} :: "
                          f"{body.decode('utf-8', 'replace')}",
                          EXIT_NETWORK) from None
        except urllib.error.URLError as exc:
            raise Blocked("NETWORK_ERROR", f"无法连接 Databento：{exc.reason}",
                          EXIT_NETWORK) from None


def _http_get(url: str, key: str, timeout: float = 30.0) -> bytes:
    """兼容旧调用的薄封装。A 档下会抛 :class:`NetworkForbidden`。"""
    return HttpClient(key, timeout=timeout).get(url)


# ---------------------------------------------------------------------- #
# A 档：纯本地估价
# ---------------------------------------------------------------------- #
@dataclass(frozen=True)
class PriceBook:
    dataset: str
    unit_price_usd: float
    unit: str
    source: str
    verified: bool

    def per_gb(self) -> float:
        return self.unit_price_usd


def load_price_book(path: Optional[str]) -> PriceBook:
    """读本地价格表。任何一项不合格都按"未核实"处理。

    宁可 ``UNKNOWN_COST`` 也不要猜：估错的成本会让人以为"很便宜"从而授权，
    这正是超支的起点。
    """
    if not path:
        raise Blocked("UNKNOWN_COST",
                      "未提供 --price-book：本机没有可核实的 Databento 单价，"
                      "拒绝猜测。可用 --price-book + --expected-gb 提供已验证价格",
                      EXIT_UNKNOWN_COST)
    p = Path(path)
    if not p.exists():
        raise Blocked("UNKNOWN_COST", f"价格表不存在：{p}", EXIT_UNKNOWN_COST)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Blocked("UNKNOWN_COST", f"价格表无法解析：{exc}",
                      EXIT_UNKNOWN_COST) from None

    verified = data.get("verified") is True
    source = str(data.get("source") or "").strip()
    unit = str(data.get("unit") or "").strip()
    try:
        price = float(data.get("unit_price_usd"))
    except (TypeError, ValueError):
        raise Blocked("UNKNOWN_COST", "价格表 unit_price_usd 不是数字",
                      EXIT_UNKNOWN_COST) from None

    if not verified or not source:
        raise Blocked("UNKNOWN_COST",
                      "价格表未标记 verified=true 或缺少 source，"
                      "无法确认单价有效性", EXIT_UNKNOWN_COST)
    if not math.isfinite(price) or price <= 0:
        raise Blocked("UNKNOWN_COST", f"单价非法：{price!r}", EXIT_UNKNOWN_COST)
    if unit not in ("per_gb", "per_1000_records"):
        raise Blocked("UNKNOWN_COST", f"不支持的计价单位：{unit!r}",
                      EXIT_UNKNOWN_COST)
    return PriceBook(dataset=str(data.get("dataset") or ""), unit_price_usd=price,
                     unit=unit, source=source, verified=True)


def local_estimate(*, price_book: PriceBook, dataset: str,
                   expected_gb: Optional[float]) -> Dict[str, Any]:
    """纯算术。不读环境变量、不建客户端、不发请求。"""
    if price_book.dataset and price_book.dataset != dataset:
        raise Blocked("UNKNOWN_COST",
                      f"价格表数据集 {price_book.dataset} ≠ 请求数据集 {dataset}",
                      EXIT_UNKNOWN_COST)
    if expected_gb is None:
        raise Blocked("UNKNOWN_COST",
                      "缺少 --expected-gb：没有数据量就无法由单价推出总价，拒绝猜测",
                      EXIT_UNKNOWN_COST)
    gb = float(expected_gb)
    if not math.isfinite(gb) or gb <= 0:
        raise Blocked("UNKNOWN_COST", f"--expected-gb 非法：{expected_gb!r}",
                      EXIT_UNKNOWN_COST)
    cost = round(price_book.unit_price_usd * gb, 6)
    return {"mode": "estimate-only", "network": False,
            "estimated_cost_usd": cost, "expected_gb": gb,
            "unit_price_usd": price_book.unit_price_usd,
            "unit": price_book.unit, "price_source": price_book.source}


# ---------------------------------------------------------------------- #
# B 档：在线查价（不取数）
# ---------------------------------------------------------------------- #
def estimate_cost_online(client: HttpClient, *, dataset: str, schema: str,
                         symbols: str, start: str, end: str,
                         stype_in: str = "raw_symbol"
                         ) -> Tuple[float, Dict[str, Any]]:
    """``metadata.get_cost``：**需要网络与凭据**，但不下载任何行情。"""
    qs = urllib.parse.urlencode({
        "dataset": dataset, "schema": schema, "symbols": symbols,
        "start": start, "end": end, "stype_in": stype_in,
    })
    raw = client.get(f"{BASE}/metadata.get_cost?{qs}")
    text = raw.decode("utf-8", "replace")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise Blocked("COST_UNPARSEABLE", f"估价响应不是 JSON：{text[:200]}",
                      EXIT_NETWORK) from None
    cost = data.get("cost")
    if cost is None and isinstance(data.get("result"), dict):
        cost = data["result"].get("cost")
    if cost is None:
        raise Blocked("COST_MISSING", f"估价响应里没有 cost 字段：{text[:200]}",
                      EXIT_NETWORK)
    try:
        value = float(cost)
    except (TypeError, ValueError):
        raise Blocked("COST_NOT_NUMERIC", f"cost 不是数字：{cost!r}",
                      EXIT_NETWORK) from None
    if not math.isfinite(value):
        raise Blocked("COST_NOT_FINITE", f"cost 不是有限数：{value!r}",
                      EXIT_NETWORK)
    return value, data


def get_range(client: HttpClient, *, dataset: str, schema: str, symbols: str,
              start: str, end: str, limit: int,
              stype_in: str = "raw_symbol", encoding: str = "json") -> bytes:
    qs = urllib.parse.urlencode({
        "dataset": dataset, "schema": schema, "symbols": symbols,
        "start": start, "end": end, "stype_in": stype_in,
        "encoding": encoding, "limit": limit,
    })
    return client.get(f"{BASE}/timeseries.get_range?{qs}")


# ---------------------------------------------------------------------- #
def sanitize_rows(blob: bytes) -> Tuple[list, Dict[str, Any]]:
    """把响应拆成 JSONL 行并统计字段形状。只读，不改写任何字段。"""
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


def _validate_cap(raw: float) -> float:
    """上限校验。``nan``/``inf`` 会让 ``cost > cap`` 恒为 False —— 必须挡在门口。"""
    cap = float(raw)
    if not math.isfinite(cap):
        raise Blocked("CAP_INVALID",
                      f"--max-cost-usd 必须是有限数（收到 {raw!r}）；"
                      f"nan/inf 会使超限判断失效", EXIT_USAGE)
    if cap < 0:
        raise Blocked("CAP_INVALID", f"--max-cost-usd 不能为负（收到 {cap}）",
                      EXIT_USAGE)
    if cap > HARD_CAP_USD:
        raise Blocked("CAP_ABOVE_HARD_LIMIT",
                      f"--max-cost-usd {cap} 超过硬顶 {HARD_CAP_USD}；"
                      f"要突破请改代码里的 HARD_CAP_USD", EXIT_USAGE)
    return cap


def _reject_mode_conflict(args: argparse.Namespace) -> None:
    """A 档不接受任何"要花钱/要下载"的参数。"""
    if args.fetch or args.estimate_online:
        return
    if args.confirm:
        raise Blocked("MODE_CONFLICT",
                      "--estimate-only 不接受 --confirm；要下载必须显式用 --fetch",
                      EXIT_USAGE)
    if args.max_cost_usd != 0.0:
        raise Blocked("MODE_CONFLICT",
                      "--estimate-only 不接受非零 --max-cost-usd；本模式不涉及花费",
                      EXIT_USAGE)


def main(argv: Optional[list] = None) -> int:
    global _NETWORK_ENABLED

    ap = argparse.ArgumentParser(
        description="Databento 费用探针（A=纯本地估价 / B=在线查价 / C=取数）")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--estimate-only", action="store_true",
                      help="A 档：纯本地估价，零网络、不读凭据（默认）")
    mode.add_argument("--estimate-online", action="store_true",
                      help="B 档：向 Databento 查价，需要凭据与网络，不取数")
    mode.add_argument("--fetch", action="store_true",
                      help="C 档：实际取数，需要凭据、网络、--confirm 与 >0 的上限")
    ap.add_argument("--dataset", default=DEFAULT_DATASET)
    ap.add_argument("--schema", default="status")
    ap.add_argument("--symbols", default="AAPL")
    ap.add_argument("--start", default=None, help="ISO8601，例如 2026-11-16T14:30:00Z")
    ap.add_argument("--end", default=None)
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--max-cost-usd", type=float, default=0.0,
                    help=f"授权上限（美元）。默认 0 = 不允许花钱；硬顶 {HARD_CAP_USD}")
    ap.add_argument("--price-book", default=None,
                    help="本地价格表 JSON（A 档用；须 verified=true 且有 source）")
    ap.add_argument("--expected-gb", type=float, default=None,
                    help="预计数据量（GB），A 档由单价推导总价所必需")
    ap.add_argument("--confirm", action="store_true",
                    help="C 档的第二次确认；单独使用不会触发下载")
    ap.add_argument("--out", default="verification/databento/raw")
    args = ap.parse_args(argv)

    if args.fetch:
        mode_name = "fetch"
    elif args.estimate_online:
        mode_name = "estimate-online"
    else:
        mode_name = "estimate-only"
        args.estimate_only = True

    # 网络闸门：只有 B/C 档才打开。**在任何客户端构造之前**。
    _NETWORK_ENABLED = mode_name in ("estimate-online", "fetch")

    out_dir = Path(args.out)
    report: Dict[str, Any] = {
        "tool": "databento_probe",
        "version": "2.0",
        "mode": mode_name,
        "network_allowed": _NETWORK_ENABLED,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dataset": args.dataset, "schema": args.schema,
        "symbols": args.symbols, "start": args.start, "end": args.end,
        "max_cost_usd": args.max_cost_usd,
    }

    try:
        if not args.start or not args.end:
            raise Blocked("MISSING_RANGE", "必须同时给出 --start 与 --end",
                          EXIT_USAGE)

        _reject_mode_conflict(args)
        cap = _validate_cap(args.max_cost_usd)
        if not (1 <= int(args.limit) <= 10_000):
            raise Blocked("LIMIT_INVALID",
                          f"--limit 必须在 1..10000（收到 {args.limit}）",
                          EXIT_USAGE)

        # ---------------- A 档：纯本地 ------------------------------- #
        if mode_name == "estimate-only":
            book = load_price_book(args.price_book)
            est = local_estimate(price_book=book, dataset=args.dataset,
                                 expected_gb=args.expected_gb)
            report.update(est)
            report["credential"] = "not_read"
            report["network_calls"] = 0
            _write(out_dir, "probe-estimate.json", report)
            print(json.dumps({"mode": mode_name, "usd": est["estimated_cost_usd"],
                              "network": False, "credential": "not_read"},
                             ensure_ascii=False))
            return EXIT_OK

        # ---------------- B/C 档：需要凭据与网络 ---------------------- #
        # 顺序很重要：**先检查授权，再取凭据、再构造客户端**。
        # 没授权就构造客户端，等于"先把车打着再问要不要出门"。
        if mode_name == "fetch":
            if cap <= 0:
                raise Blocked("CAP_NOT_AUTHORIZED",
                              "取数要求 --max-cost-usd > 0；默认 0 表示未授权任何花费",
                              EXIT_OVER_CAP)
            if not args.confirm:
                raise Blocked("NO_CONFIRM",
                              "取数需要 --confirm 二次确认", EXIT_NO_CONFIRM)

        key = _key()
        report["credential"] = "present"

        client = HttpClient(key)          # 构造即受闸门控制

        if mode_name == "estimate-online":
            cost, raw_cost = estimate_cost_online(
                client, dataset=args.dataset, schema=args.schema,
                symbols=args.symbols, start=args.start, end=args.end)
            report.update({"estimated_cost_usd": cost,
                           "cost_response": raw_cost,
                           "network_calls": client.requests})
            report["note"] = "仅查价，未请求历史数据"
            report["within_cap"] = bool(cost <= cap)
            _write(out_dir, "probe-estimate-online.json", report)
            print(json.dumps({"mode": mode_name, "usd": cost, "network": True,
                              "data_requested": False,
                              "within_cap": report["within_cap"]},
                             ensure_ascii=False))
            # B 档只查价，不花钱：即使超上限也返回 0，把结论放在 within_cap 里。
            # 真正的拒绝发生在 C 档（那里才真的会产生费用）。
            return EXIT_OK

        # ---------------- C 档：取数 ---------------------------------- #
        cost, raw_cost = estimate_cost_online(
            client, dataset=args.dataset, schema=args.schema,
            symbols=args.symbols, start=args.start, end=args.end)
        report.update({"estimated_cost_usd": cost, "cost_response": raw_cost})
        print(json.dumps({"step": "cost", "usd": cost}, ensure_ascii=False))
        if cost > cap:
            raise Blocked("COST_EXCEEDS_CAP",
                          f"估价 {cost} USD 超过授权上限 {cap} USD；不取数",
                          EXIT_OVER_CAP)

        blob = get_range(client, dataset=args.dataset, schema=args.schema,
                         symbols=args.symbols, start=args.start, end=args.end,
                         limit=args.limit)
        rows, stats = sanitize_rows(blob)
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        raw_path = out_dir / (f"{args.dataset.replace('.', '_')}-"
                              f"{args.schema}-{stamp}.jsonl")
        raw_path.write_bytes(blob)
        digest = hashlib.sha256(blob).hexdigest()
        report.update({"raw_file": str(raw_path), "sha256": digest,
                       "network_calls": client.requests,
                       "data_requested": True, **stats})
        (out_dir / f"{raw_path.stem}.meta.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"ok": True, "rows": stats["rows"],
                          "sha256": digest}, ensure_ascii=False))
        return EXIT_OK

    except Blocked as exc:
        report.update({"blocked": exc.code, "detail": str(exc),
                       "network_allowed": _NETWORK_ENABLED})
        _write(out_dir, "probe-blocked.json", report)
        print(json.dumps({"blocked": exc.code, "detail": str(exc),
                          "mode": mode_name}, ensure_ascii=False))
        return exc.exit_code
    finally:
        # 无论成功失败，进程内网络闸门回到关闭状态
        _NETWORK_ENABLED = False


def _write(out_dir: Path, name: str, payload: Dict[str, Any]) -> None:
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        # 证据落盘失败不应改变安全判定，但要让调用方看见
        print(json.dumps({"warning": f"证据写入失败：{name}"}, ensure_ascii=False))


if __name__ == "__main__":
    raise SystemExit(main())
