"""归一化：代码、交易所、时间戳、原始字段保留。

时间戳是本层最容易出错的地方
----------------------------
实测到的供应商时间格式至少有四种：

* 纳秒 epoch（Massive/Nasdaq ITCH 都是 ``t`` 纳秒整数）
* 毫秒 epoch（Massive 文档另一处写 "Unix MS"）
* 秒 epoch（部分 REST 接口）
* ISO8601 字符串（带/不带时区）

同一个字段在不同文档/不同接口下单位不同，**必须显式指定**，不能猜。
本模块要求调用方传入 ``unit``（``"ns" / "ms" / "s" / "iso"``），
猜错单位会把 2026 年算成 1970 年或 55000 年 —— 这种错误必须暴露而不是静默。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

#: 交易所代码归一化表（供应商写法 → 内部小写）
EXCHANGE_ALIASES: Dict[str, str] = {
    "nasdaq": "nasdaq", "xnas": "nasdaq", "xnqs": "nasdaq", "xnms": "nasdaq",
    "nyse": "nyse", "xnys": "nyse", "nys": "nyse",
    "amex": "amex", "nyseamerican": "amex", "nyse_american": "amex", "xase": "amex",
    "arca": "arca", "nysearca": "arca", "nyse_arca": "arca", "arcx": "arca",
    "bats": "cboe_bzx", "cboe": "cboe_bzx", "cboebzx": "cboe_bzx", "bzx": "cboe_bzx",
    "cboeedgx": "cboe_edgx", "edgx": "cboe_edgx",
    "cboeedga": "cboe_edga", "edga": "cboe_edga",
    "iex": "iex", "xiex": "iex",
    "finra": "finra", "trf": "finra",
    "otc": "otc",
    "": "unknown", "unknown": "unknown",
}

#: 美股常见代码前缀（本项目既有约定：usAAPL）
MARKET_PREFIXES = ("us", "hk", "cn", "sh", "sz")


def normalize_exchange(raw: Any) -> str:
    """交易所代码归一化。未知值返回原值小写（不猜成 nyse/nasdaq）。"""
    key = str(raw or "").strip().lower().replace("-", "").replace(" ", "")
    if key in EXCHANGE_ALIASES:
        return EXCHANGE_ALIASES[key]
    return key or "unknown"


def normalize_symbol(raw: Any) -> str:
    """代码归一化：去市场前缀、转大写、去掉多余空格。

    只剥**小写**前缀（``usAAPL`` → ``AAPL``），避免把 ``USB`` 这类真实代码
    误伤（``USB`` 的前两位 ``US`` 是大写，不是前缀）。
    """
    s = str(raw or "").strip()
    if not s:
        return ""
    for p in MARKET_PREFIXES:
        # 前缀必须**小写精确匹配**（"usAAPL" 剥成 "AAPL"；"USB" 不受影响，
        # 因为它的 "US" 是大写）。剥完不能为空。
        if s.startswith(p) and len(s) > len(p):
            s = s[len(p):]
            break
    return s.upper()


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_UNIT_DIV = {"ns": 1_000_000_000, "ms": 1_000, "s": 1, "us": 1_000_000}


def parse_timestamp(raw: Any, unit: str = "iso") -> Tuple[Optional[datetime], str]:
    """解析时间戳。返回 ``(utc_datetime | None, 错误说明)``。

    ``unit`` 必须显式给出；给 ``"auto"`` 只用于**已知是秒级**的整数，
    不做"按位数猜"（10 位=秒、13 位=毫秒、19 位=纳秒）这种看似聪明、
    实际会把 2001 年以前的毫秒数据判错的推断。
    """
    if raw is None or raw == "":
        return None, "时间戳为空"
    try:
        if isinstance(raw, datetime):
            dt = raw
        elif isinstance(raw, int) and not isinstance(raw, bool) or (
                isinstance(raw, str) and raw.strip().lstrip("-").isdigit()):
            # **整数用整数运算**：先 // 再取余。
            # 走 float 会丢精度 —— 1794839400123456789 ns 会被四舍五入成
            # ...123457 µs（差 1 µs），而交易所时间戳的意义恰恰在纳秒级。
            if unit not in _UNIT_DIV:
                return None, f"整数时间戳必须指定 unit（收到 {unit!r}）"
            total = int(str(raw).strip()) if isinstance(raw, str) else int(raw)
            div = int(_UNIT_DIV[unit])
            secs, rem = divmod(total, div)
            # 余数换算成微秒（datetime 的最小分辨率）：向下取整，不四舍五入
            micros = rem * 1_000_000 // div
            dt = (datetime(1970, 1, 1, tzinfo=timezone.utc)
                  + timedelta(seconds=secs, microseconds=micros))
        elif isinstance(raw, float):
            if unit not in _UNIT_DIV:
                return None, f"整数时间戳必须指定 unit（收到 {unit!r}）"
            dt = datetime.fromtimestamp(raw / _UNIT_DIV[unit], tz=timezone.utc)
        else:
            text = str(raw).strip().replace("Z", "+00:00")
            dt = datetime.fromisoformat(text)
    except (ValueError, OverflowError, OSError) as exc:
        return None, f"时间戳无法解析：{raw!r}（{exc}）"

    if dt.tzinfo is None:
        # 不做时区猜测：标记为 naive 并返回，由上层加 NAIVE_TIME 质量标记
        return dt.replace(tzinfo=timezone.utc), "时间戳缺少时区（按 UTC 占位，已标记）"
    return dt.astimezone(timezone.utc), ""


def to_market_time(dt: datetime, tz_name: str = "America/New_York") -> datetime:
    from zoneinfo import ZoneInfo
    return dt.astimezone(ZoneInfo(tz_name))


def session_phase_of(dt: datetime, *, pre=(4, 0), regular=(9, 30, 16, 0),
                     post=(16, 0, 20, 0), overnight=(20, 0, 4, 0)) -> str:
    """按美东时间判断时段（仅用于展示/质量判断；交易门禁仍走 ExchangeClock）。

    参数用 ``(时, 分)`` 或 ``(开时, 开分, 收时, 收分)``，内部统一成 (h, m) 比较，
    避免直接拿 tuple 和 int 比较这种低级错误。
    """
    local = to_market_time(dt)
    t = (local.hour, local.minute)
    reg_open = (int(regular[0]), int(regular[1]))
    reg_close = (int(regular[2]), int(regular[3]))
    pre_open = (int(pre[0]), int(pre[1]))
    post_open = (int(post[0]), int(post[1]))
    post_close = (int(post[2]), int(post[3]))

    if reg_open <= t < reg_close:
        return "regular"
    if pre_open <= t < reg_open:
        return "pre"
    if post_open <= t < post_close:
        return "post"
    return "overnight"


def keep_raw(payload: Dict[str, Any], *, max_len: int = 400) -> Dict[str, Any]:
    """保留原始字段（截断超长内容），用于审计与排障。"""
    out: Dict[str, Any] = {}
    for k, v in (payload or {}).items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v if not isinstance(v, str) or len(v) <= max_len else v[:max_len] + "…"
        else:
            out[k] = str(v)[:max_len]
    return out
