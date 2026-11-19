#!/usr/bin/env python3
"""Twelve Data 连通性与日线数据验证（只读，不写数据库、不改策略）。

密钥来源（按优先级，**绝不回显**）
--------------------------------
1. 进程环境变量 ``TWELVE_DATA_API_KEY``
2. Windows 用户环境变量（注册表 ``HKCU\\Environment``）

为什么要读注册表
----------------
用 ``setx`` / ``[Environment]::SetEnvironmentVariable(...,'User')`` 写入的用户变量，
**不会**出现在已经启动的父进程（编辑器/终端）的环境块里。若只读 ``os.environ``，
新开的子进程照样读不到，会误判成"没配置"。读注册表可以绕开这个继承问题。

安全约定
--------
* 密钥只在本进程内存里使用，不写文件、不进日志、不进异常文本；
* 落盘前对整份 JSON 做一次「密钥字面量」硬断言，命中即拒绝写盘；
* 保存的 URL 一律把 ``apikey=`` 之后的参数替换成 ``***``。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://api.twelvedata.com"
KEY_ENV = "TWELVE_DATA_API_KEY"
DEFAULT_SYMBOLS = ("QQQ", "QLD", "QID", "TQQQ")


# --------------------------------------------------------------------------- #
# 取密钥
# --------------------------------------------------------------------------- #
def load_key() -> tuple[str, str]:
    """返回 ``(key, source)``；取不到返回 ``("", "missing")``。"""
    v = (os.environ.get(KEY_ENV) or "").strip()
    if v:
        return v, "process_env"
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                v2, _ = winreg.QueryValueEx(k, KEY_ENV)
            v2 = str(v2 or "").strip()
            if v2:
                return v2, "user_env(registry)"
        except OSError:
            pass
    return "", "missing"


def redact_url(url: str) -> str:
    """把 URL 里的 apikey 抹掉，便于安全落盘。"""
    parts = urllib.parse.urlsplit(url)
    q = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    q = [(k, ("***" if k.lower() in ("apikey", "api_key", "access_key") else v)) for k, v in q]
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path,
                                    urllib.parse.urlencode(q), ""))


# --------------------------------------------------------------------------- #
# 请求
# --------------------------------------------------------------------------- #
class CreditScheduler:
    """严格按 credits 调度：任意 60 秒窗口内最多 max_credits 个。

    免费 Basic 是 8 credits/分钟。并发请求会绕过本地计数（服务端也可能
    同时计数），所以这里**只允许串行**，并且每次真正等待都把原因记下来。
    """

    def __init__(self, max_credits: int = 8, window: float = 60.0, margin: float = 1.2):
        self.max_credits = int(max_credits)
        self.window = float(window)
        self.margin = float(margin)
        self._events: list[tuple[float, int]] = []
        self.waited_total = 0.0

    def _prune(self, now: float) -> None:
        cut = now - self.window
        self._events = [(t, c) for (t, c) in self._events if t > cut]

    def used(self) -> int:
        self._prune(time.monotonic())
        return sum(c for _, c in self._events)

    def acquire(self, cost: int = 1) -> float:
        """阻塞到本窗口还能容纳 ``cost`` 个 credit；返回实际等待秒数。"""
        waited = 0.0
        while True:
            now = time.monotonic()
            self._prune(now)
            if self.used() + cost <= self.max_credits:
                self._events.append((now, cost))
                self.waited_total += waited
                return waited
            oldest = min(t for t, _ in self._events)
            sleep_for = max(0.05, (oldest + self.window + self.margin) - now)
            time.sleep(sleep_for)
            waited += sleep_for

    def record_extra(self, cost: int = 1) -> None:
        """服务端统计与本地不一致时（如 429 仍计一次），补记一笔。"""
        self._events.append((time.monotonic(), cost))


class Client:
    """串行 HTTP 客户端：本地 credits 调度 + 429 退避（有上限，不无限重试）。"""

    def __init__(self, key: str, timeout: float = 25.0,
                 scheduler: CreditScheduler | None = None,
                 max_retries: int = 3):
        self._key = key
        self._timeout = timeout
        self.scheduler = scheduler or CreditScheduler()
        self.max_retries = int(max_retries)
        self.calls = 0
        self.retry_429 = 0
        #: 每次请求的审计记录（不含密钥）
        self.events: list[dict] = []

    def get(self, path: str, cost: int = 1, **params) -> tuple[int, dict | list | str, str]:
        params = {k: v for k, v in params.items() if v is not None}
        params["apikey"] = self._key
        url = f"{BASE}/{path}?{urllib.parse.urlencode(params)}"
        safe = redact_url(url)

        attempt = 0
        while True:
            attempt += 1
            waited = self.scheduler.acquire(cost)
            self.calls += 1
            t0 = time.monotonic()
            status, obj, retry_after = self._once(url, safe)
            elapsed = round(time.monotonic() - t0, 3)
            rec = {"path": path, "attempt": attempt, "credits": cost,
                   "http": status, "schedule_wait_s": round(waited, 2),
                   "elapsed_s": elapsed,
                   "used_in_window": self.scheduler.used()}
            if status == 429:
                self.retry_429 += 1
                self.scheduler.record_extra(cost)     # 服务端把 429 也计了一次
                back = retry_after or self._hint_wait(obj) or self.scheduler.window - 5
                rec.update({"outcome": "RATE_LIMITED", "backoff_s": round(back, 1),
                            "hint": str((obj or {}).get("message", ""))[:120]})
                self.events.append(rec)
                if attempt > self.max_retries:
                    return status, obj, safe
                time.sleep(max(0.5, back))
                continue
            rec["outcome"] = "OK" if status == 200 else f"HTTP_{status}"
            self.events.append(rec)
            return status, obj, safe

    def _once(self, url: str, safe: str):
        req = urllib.request.Request(url, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"),
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as r:
                body = r.read().decode("utf-8", "replace")
                status = r.status
                retry_after = r.headers.get("Retry-After")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            status = e.code
            retry_after = e.headers.get("Retry-After")
        except Exception as exc:  # noqa: BLE001
            return 0, {"exception": f"{type(exc).__name__}: {exc}"}, None
        try:
            obj = json.loads(body)
        except ValueError:
            obj = body[:500]
        try:
            ra = float(retry_after) if retry_after else None
        except (TypeError, ValueError):
            ra = None
        return status, obj, ra

    @staticmethod
    def _hint_wait(obj) -> float | None:
        """从 429 的 message 里读"等待下一分钟"这类提示（读不到就返回 None）。"""
        msg = str((obj or {}).get("message", "")).lower()
        for kw in ("wait for the next minute", "next minute", "per minute"):
            if kw in msg:
                return 60.0
        return None

    def credit_summary(self) -> dict:
        total = sum(e["credits"] for e in self.events)
        return {"requests": len(self.events), "credits_charged_locally": total,
                "rate_limited_attempts": self.retry_429,
                "schedule_wait_total_s": round(self.scheduler.waited_total, 1),
                "max_credits_per_minute": self.scheduler.max_credits}


def _is_error(obj) -> bool:
    return isinstance(obj, dict) and (str(obj.get("status")) == "error" or "code" in obj and "message" in obj)


def _brief(obj) -> str:
    if isinstance(obj, dict):
        if _is_error(obj):
            return f"ERR {obj.get('code')}: {str(obj.get('message'))[:70]}"
        if obj.get("status") == "ok":
            n = len(obj.get("values") or [])
            meta = obj.get("meta") or {}
            return f"ok bars={n} {meta.get('symbol','')}/{meta.get('exchange','')}/{meta.get('type','')}"
        return "ok " + str(list(obj)[:6])
    return str(obj)[:80]


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    ap.add_argument("--bars", type=int, default=30)
    ap.add_argument("--out", default="")
    ap.add_argument("--db", default=str(ROOT / "data" / "market_us.db"),
                    help="只读，用于比对；不会写入")
    ap.add_argument("--skip-quote", action="store_true", help="跳过 quote（元数据已足时省额度）")
    ap.add_argument("--skip-ndx", action="store_true", help="跳过 NDX 探测（本阶段不需要）")
    ap.add_argument("--sample-depth", action="store_true",
                    help="跑 1995/2005/2015 抽样（默认关闭，改用 --history-years）")
    ap.add_argument("--history-years", type=float, default=0.0,
                    help="额外取最近 N 年的日线（单次请求，验证历史深度）")
    ap.add_argument("--min-interval", type=float, default=0.0,
                    help="请求之间的最小间隔秒（默认交给 credits 调度器）")
    ap.add_argument("--max-credits", type=int, default=8,
                    help="每分钟最多 credits（免费 Basic = 8）")
    ap.add_argument("--max-retries", type=int, default=3, help="429 最大重试次数")
    args = ap.parse_args()

    key, source = load_key()
    if not key:
        print(f"BLOCKED: 未找到 {KEY_ENV}")
        print("  请先运行 scripts/set_twelve_data_key.ps1 完成安全配置（不回显、不进聊天记录）")
        return 2

    print(f"KEY_SOURCE={source}  length={len(key)}  sha256_12={_fp(key)}")
    cli = Client(key, scheduler=CreditScheduler(max_credits=args.max_credits),
                 max_retries=args.max_retries)
    report: dict = {
        "probed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "key_source": source,
        "key_fingerprint": _fp(key),
        "base": BASE,
        "note": "只读验证；未写入数据库；URL 中的 apikey 已脱敏。",
        "results": {},
    }

    # 1) 账户/额度
    st, usage, safe = cli.get("api_usage")
    report["results"]["api_usage"] = {"http": st, "url": safe, "response": usage}
    print(f"\n[1] api_usage  http={st}  {json.dumps(usage, ensure_ascii=False)[:160]}")

    # 2) 四个 ETF 的 quote（确认标的身份）
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    print("\n[2] quote（标的身份）" + ("  [SKIPPED]" if args.skip_quote else ""))
    for s in (() if args.skip_quote else symbols):
        st, obj, safe = cli.get("quote", symbol=s)
        report["results"][f"quote.{s}"] = {"http": st, "url": safe, "response": obj}
        if isinstance(obj, dict) and not _is_error(obj):
            print(f"    {s:5s} http={st}  name={obj.get('name')}  exch={obj.get('exchange')}  "
                  f"type={obj.get('type')}  ccy={obj.get('currency')}  close={obj.get('close')}")
        else:
            print(f"    {s:5s} http={st}  {_brief(obj)}")
        time.sleep(0.4)

    # 3) 日线 OHLCV
    print(f"\n[3] time_series 1day × {args.bars}")
    ok_symbols = []
    for s in symbols:
        st, obj, safe = cli.get("time_series", symbol=s, interval="1day",
                                outputsize=args.bars, timezone="America/New_York")
        report["results"][f"timeseries.{s}"] = {"http": st, "url": safe, "response": obj}
        if isinstance(obj, dict) and obj.get("status") == "ok" and obj.get("values"):
            vals = obj["values"]
            ok_symbols.append(s)
            print(f"    {s:5s} ok  bars={len(vals):3d}  {vals[-1]['datetime']} .. {vals[0]['datetime']}"
                  f"  tz={obj['meta'].get('exchange_timezone')}")
        else:
            print(f"    {s:5s} http={st}  {_brief(obj)}")
        time.sleep(0.4)

    # 4) NDX 指数（符号变体探测）
    print("\n[4] NDX 指数可用性探测" + ("  [SKIPPED]" if args.skip_ndx else ""))
    for sym, exch in (() if args.skip_ndx else
                      (("NDX", None), ("NDX", "INDEX"), ("NDX:INDX", None), ("^NDX", None))):
        st, obj, safe = cli.get("time_series", symbol=sym, exchange=exch,
                                interval="1day", outputsize="3")
        tag = f"{sym}" + (f"@{exch}" if exch else "")
        report["results"][f"ndx.{tag}"] = {"http": st, "url": safe, "response": obj}
        print(f"    {tag:12s} http={st}  {_brief(obj)}")
        time.sleep(0.4)

    # 5) 历史深度
    print("\n[5] 历史深度抽样" + ("" if args.sample_depth else "  [SKIPPED]"))
    for yr in ((1995, 2005, 2015) if args.sample_depth else ()):
        st, obj, safe = cli.get("time_series", symbol="QQQ", interval="1day",
                                start_date=f"{yr}-01-01", end_date=f"{yr}-01-20")
        report["results"][f"depth.{yr}"] = {"http": st, "url": safe, "response": obj}
        if isinstance(obj, dict) and obj.get("status") == "ok" and obj.get("values"):
            v = obj["values"]
            print(f"    {yr}: bars={len(v):3d}  {v[-1]['datetime']} .. {v[0]['datetime']}")
        else:
            print(f"    {yr}: http={st}  {_brief(obj)}")
        time.sleep(0.4)

    # 5b) 最近 N 年（单次请求，用于验证 5 年历史是否可一次取全）
    if args.history_years > 0:
        end = datetime.now(timezone.utc).date()
        start = end.replace(year=end.year - int(args.history_years))
        print(f"\n[5b] 最近 {args.history_years:g} 年（{start} .. {end}，单次请求）")
        for sym in symbols:
            st, obj, safe = cli.get("time_series", symbol=sym, interval="1day",
                                    start_date=start.isoformat(), end_date=end.isoformat(),
                                    outputsize="5000")
            report["results"][f"history.{sym}"] = {"http": st, "url": safe, "response": obj}
            if isinstance(obj, dict) and obj.get("status") == "ok" and obj.get("values"):
                v = obj["values"]
                span = ""
                try:
                    d0 = datetime.strptime(v[-1]["datetime"], "%Y-%m-%d")
                    d1 = datetime.strptime(v[0]["datetime"], "%Y-%m-%d")
                    span = f"  跨度 {(d1 - d0).days / 365.25:.2f} 年"
                except ValueError:
                    pass
                print(f"    {sym:5s} ok bars={len(v):5d}  {v[-1]['datetime']} .. {v[0]['datetime']}{span}")
            else:
                print(f"    {sym:5s} http={st}  {_brief(obj)}")

    # 5c) 请求审计
    report["request_log"] = cli.events
    report["credit_summary"] = cli.credit_summary()

    # 6) 与本地 us_bars 比对（只读）
    report["comparison"] = _compare_local(report, args.db)
    print("\n[6] 与本机 us_bars（Yahoo 口径）比对")
    for sym, c in (report["comparison"] or {}).items():
        if not isinstance(c, dict) or "overlap" not in c:
            continue
        print(f"    {sym:5s} 重叠 {c['overlap']:3d} 天  "
              f"收盘最大偏差 {c['max_abs_bp']} bp  均值 {c['mean_abs_bp']} bp"
              + (f"  本地缺: {c['missing_locally']}" if c["missing_locally"] else ""))

    # 7) 落盘（先做密钥断言）
    blob = json.dumps(report, ensure_ascii=False, indent=2)
    if key in blob:
        print("\nABORT: 脱敏后仍检出密钥字面量，拒绝写盘")
        return 3
    out = Path(args.out) if args.out else (ROOT / "reports" / "marketdata_probe"
                                           / f"td_verified_{datetime.now().strftime('%Y%m%d-%H%M%S')}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(blob, encoding="utf-8")

    print(f"\nCREDITS={json.dumps(cli.credit_summary(), ensure_ascii=False)}")
    print(f"API_CALLS={cli.calls}")
    print(f"OK_SYMBOLS={ok_symbols}")
    print(f"EVIDENCE={out}")
    return 0 if ok_symbols else 1


def _fp(key: str) -> str:
    import hashlib
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def _compare_local(report: dict, db_path: str) -> dict:
    """把 Twelve Data 的最近 N 根与本机 us_bars 对比（只读）。"""
    try:
        sys.path.insert(0, str(ROOT / "src"))
        from ai_trader.us_loop.bars import BarStore
        if not Path(db_path).is_file():
            return {"skipped": f"db 不存在: {db_path}"}
        st = BarStore(db_path)
    except Exception as exc:  # noqa: BLE001
        return {"skipped": f"{type(exc).__name__}: {exc}"}

    out: dict = {}
    try:
        mapping = {"QQQ": "usqqq", "QLD": "usqld", "QID": "usqid", "TQQQ": "ustqqq"}
        for td_sym, local_sym in mapping.items():
            r = report["results"].get(f"timeseries.{td_sym}") or {}
            obj = r.get("response")
            if not isinstance(obj, dict) or obj.get("status") != "ok":
                continue
            local = {row["trade_date"]: row["close"] for row in st.load(local_sym, num=100000)}
            rows, devs, vol_mismatch = [], [], 0
            for v in obj["values"]:
                d = v["datetime"]
                if d not in local:
                    rows.append({"date": d, "td": v["close"], "local": None, "bp": None})
                    continue
                a, b = float(v["close"]), float(local[d])
                bp = (a / b - 1) * 1e4 if b else 0.0
                devs.append(abs(bp))
                rows.append({"date": d, "td": a, "local": b, "bp": round(bp, 4)})
            out[td_sym] = {
                "local_symbol": local_sym,
                "overlap": len(devs),
                "max_abs_bp": round(max(devs), 4) if devs else None,
                "mean_abs_bp": round(sum(devs) / len(devs), 4) if devs else None,
                "missing_locally": [r["date"] for r in rows if r["local"] is None],
                "rows": rows,
            }
        return out
    finally:
        st.close()


if __name__ == "__main__":
    sys.exit(main())
