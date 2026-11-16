"""交易所时钟（US-P0-1）。

为什么单独做一层
----------------
原来 `SESSIONS["us"]` 是**北京时区的硬编码窗口**（21:30–04:00），等于把 EDT 的
09:30–16:00 固化成了常量。一旦美东进入冬令时（EST，UTC-5），这个窗口就整体偏早 1 小时；
而且"星期几"用的是北京日期，导致周五夜盘（北京周六凌晨）被判成休市、
周日深夜（北京周一凌晨）被判成开市。

本模块把"判断交易时段"这件事收敛到**交易所当地时间**：

* 用 ``zoneinfo`` 的 ``America/New_York`` 做时区换算（tzdata 提供真实历史/未来规则）
* 所有判断（日期、星期、时段、提前收市）都基于**交易所当地日期与时间**
* 支持盘前/常规/盘后三段，以及提前收市（如 13:00 收盘）
* 传入 naive datetime 时按**本机时区**解释（与 `datetime.now()` 一致）

不做什么
--------
不做节假日表的推断 —— 节假日必须由配置提供（`TradingCalendar`），
本模块只负责"给定日期是否在节假日表内"这一判断。
"""
from __future__ import annotations
from .. import timebase

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#: 市场 → 交易所时区。找不到时区数据时整体降级为 UTC 并显式报错由调用方处理。
TZ_BY_MARKET = {
    "us": "America/New_York",
    "hk": "Asia/Hong_Kong",
    "cn": "Asia/Shanghai",
}

#: 交易阶段
PHASE_PRE = "pre"
PHASE_REGULAR = "regular"
PHASE_POST = "post"
PHASE_NIGHT = "night"
PHASE_CLOSED = "closed"
PHASE_WEEKEND = "weekend"
PHASE_HOLIDAY = "holiday"

#: 所有"可以交易"的阶段（是否真的允许由 ``allow_extended`` 决定）
TRADABLE_PHASES = (PHASE_PRE, PHASE_REGULAR, PHASE_POST, PHASE_NIGHT)

#: 内置默认时段表 —— **以交易所当地时间为准**。
#:
#: 与旧实现（``SESSIONS`` 硬编码北京时间 21:30–04:00）的根本差别：
#: 这里是"美东 09:30–16:00"，DST/冬令时由 ``zoneinfo`` 自动处理。
DEFAULT_SESSION_SPEC: Dict[str, Dict[str, Any]] = {
    "us": {
        "tz": "America/New_York",
        "regular": [["09:30", "16:00"]],
        # 盘前 04:00 起、盘后至 20:00（各券商细则不同，仅为窗口定义；
        # 是否允许在这些时段**交易**由 extended_tradable 控制）
        "pre": ["04:00", "09:30"],
        "post": ["16:00", "20:00"],
        "night": ["20:00", "04:00"],
    },
    "hk": {
        "tz": "Asia/Hong_Kong",
        "regular": [["09:30", "12:00"], ["13:00", "16:00"]],
    },
    "cn": {
        "tz": "Asia/Shanghai",
        "regular": [["09:30", "11:30"], ["13:00", "15:00"]],
    },
}


class ClockError(RuntimeError):
    """时区数据缺失等致命问题。调用方必须 fail-closed。"""


def load_tz(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:  # pragma: no cover - 取决于环境
        raise ClockError(
            f"时区数据不可用: {name!r}（Windows 需安装 tzdata 包：pip install tzdata）"
        ) from exc


@dataclass(frozen=True)
class SessionWindow:
    """交易所当地时间的时段窗口（含端点语义：open 含、close 不含）。"""

    open: time
    close: time

    def contains(self, t: time) -> bool:
        return self.open <= t < self.close

    def as_list(self) -> List[str]:
        return [self.open.strftime("%H:%M"), self.close.strftime("%H:%M")]


@dataclass
class SessionInfo:
    market: str
    tz_name: str
    local_now: str
    local_date: str
    weekday: int
    phase: str
    is_trading_day: bool
    is_regular_open: bool
    is_extended_open: bool
    closes_at: str
    reason: str

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def tradable(self) -> bool:
        """当前阶段是否处于某个"可以做交易"的窗口（不管是否允许开新仓）。"""
        return self.phase in TRADABLE_PHASES


def _parse_hm(value: Any) -> Optional[time]:
    if isinstance(value, time):
        return value
    if isinstance(value, str):
        s = value.strip()
        if ":" in s:
            parts = s.split(":")
            try:
                return time(int(parts[0]), int(parts[1]))
            except (ValueError, IndexError):
                return None
    if isinstance(value, (tuple, list)) and len(value) >= 2:
        try:
            return time(int(value[0]), int(value[1]))
        except (TypeError, ValueError):
            return None
    return None


def _coerce_window(raw: Any) -> Optional[SessionWindow]:
    """支持三种写法：((9,30),(11,30)) / ["09:30","11:30"] / {"open":"09:30","close":"11:30"}"""
    if raw is None:
        return None
    if isinstance(raw, dict):
        o, c = _parse_hm(raw.get("open") or raw.get("start")), _parse_hm(raw.get("close") or raw.get("end"))
    elif isinstance(raw, (tuple, list)) and len(raw) == 2:
        o, c = _parse_hm(raw[0]), _parse_hm(raw[1])
    else:
        return None
    if o is None or c is None:
        return None
    return SessionWindow(open=o, close=c)


class ExchangeClock:
    """某个市场的交易所时钟。线程内无状态，可复用。"""

    def __init__(
        self,
        market: str,
        windows: Sequence[SessionWindow],
        *,
        tz_name: Optional[str] = None,
        pre_window: Optional[SessionWindow] = None,
        post_window: Optional[SessionWindow] = None,
        night_window: Optional[SessionWindow] = None,
        holidays: Optional[Iterable[date]] = None,
        early_close: Optional[Dict[date, time]] = None,
        allow_extended: bool = False,
    ):
        self.market = str(market or "").lower()
        self.tz_name = tz_name or TZ_BY_MARKET.get(self.market, "UTC")
        self.tz = load_tz(self.tz_name)
        #: 是否真的配置了常规窗口。空 = 配置缺失 → 上层必须 fail-closed，
        #: 绝不能悄悄用默认 09:30–16:00 顶替（那是 fail-open）。
        self.configured = bool(list(windows))
        self.windows = list(windows) or [SessionWindow(time(9, 30), time(16, 0))]
        self.pre_window = pre_window
        self.post_window = post_window
        self.night_window = night_window
        self.holidays = set(holidays or ())
        self.early_close = dict(early_close or {})
        self.allow_extended = bool(allow_extended)

    # ------------------------------------------------------------------ #
    # 时区换算
    # ------------------------------------------------------------------ #
    def local(self, now: Optional[datetime] = None) -> datetime:
        """把任意 datetime 转成交易所当地时间。

        naive 输入按**本机时区**解释（与 ``datetime.now()`` 的语义一致）——
        调用方（引擎/测试）几乎都传 naive 的"本机时间"。
        """
        dt = now or timebase.now()
        if dt.tzinfo is None:
            dt = dt.astimezone()          # 视为本机时区
        return dt.astimezone(self.tz)

    def local_date(self, now: Optional[datetime] = None) -> date:
        return self.local(now).date()

    # ------------------------------------------------------------------ #
    # 交易日 / 收盘时间
    # ------------------------------------------------------------------ #
    def is_trading_day(self, day: date) -> Tuple[bool, str]:
        if day.weekday() >= 5:
            return False, f"{day.isoformat()} 为当地周末"
        if day in self.holidays:
            return False, f"{day.isoformat()} 在当地节假日表内"
        return True, f"{day.isoformat()} 为交易日"

    def close_time(self, day: date) -> Optional[time]:
        """当日收盘时间（提前收市日返回提前后的时间）。"""
        if self.early_close and day in self.early_close:
            return self.early_close[day]
        return self.windows[-1].close if self.windows else None

    # ------------------------------------------------------------------ #
    # 阶段判定
    # ------------------------------------------------------------------ #
    def session(self, now: Optional[datetime] = None) -> SessionInfo:
        loc = self.local(now)
        day = loc.date()
        t = loc.time().replace(microsecond=0, tzinfo=None)

        if not self.configured:
            return SessionInfo(
                market=self.market, tz_name=self.tz_name,
                local_now=loc.isoformat(timespec="seconds"),
                local_date=day.isoformat(), weekday=day.weekday(),
                phase=PHASE_CLOSED, is_trading_day=False,
                is_regular_open=False, is_extended_open=False,
                closes_at="", reason="未配置常规交易时段（fail-closed）",
            )

        trading_day, day_reason = self.is_trading_day(day)
        closes_at = self.close_time(day) if trading_day else None

        if not trading_day:
            phase = PHASE_WEEKEND if day.weekday() >= 5 else PHASE_HOLIDAY
            return SessionInfo(
                market=self.market, tz_name=self.tz_name,
                local_now=loc.isoformat(timespec="seconds"),
                local_date=day.isoformat(), weekday=day.weekday(),
                phase=phase, is_trading_day=False,
                is_regular_open=False, is_extended_open=False,
                closes_at="", reason=day_reason,
            )

        phase = PHASE_CLOSED
        reason = "不在任何交易时段"

        # 盘前（早于第一个常规窗口开盘）
        first_open = self.windows[0].open
        if self.pre_window and self.pre_window.open <= t < min(self.pre_window.close, first_open):
            phase, reason = PHASE_PRE, f"盘前 {self.pre_window.open:%H:%M}-{first_open:%H:%M}"

        if phase == PHASE_CLOSED:
            for w in self.windows:
                end = w.close
                # 常规窗口的"收盘"要吃到提前收市
                if w is self.windows[-1] and closes_at:
                    end = closes_at
                if w.open <= t < end:
                    phase = PHASE_REGULAR
                    reason = f"常规时段 {w.open:%H:%M}-{end:%H:%M}"
                    break

        if phase == PHASE_CLOSED and self.post_window and closes_at:
            if closes_at <= t < self.post_window.close:
                phase = PHASE_POST
                reason = f"盘后 {closes_at:%H:%M}-{self.post_window.close:%H:%M}"

        # 夜盘：可能跨午夜（例如 20:00–04:00），跨午夜时用"或"判断
        if phase == PHASE_CLOSED and self.night_window:
            nw = self.night_window
            if nw.open < nw.close:
                in_night = nw.open <= t < nw.close
            else:
                in_night = t >= nw.open or t < nw.close
            if in_night:
                phase = PHASE_NIGHT
                reason = f"夜盘 {nw.open:%H:%M}-{nw.close:%H:%M}" + (
                    "（跨当地午夜）" if nw.open >= nw.close else "")

        is_regular = phase == PHASE_REGULAR
        is_extended = phase in TRADABLE_PHASES if self.allow_extended else is_regular

        return SessionInfo(
            market=self.market, tz_name=self.tz_name,
            local_now=loc.isoformat(timespec="seconds"),
            local_date=day.isoformat(), weekday=day.weekday(),
            phase=phase, is_trading_day=True,
            is_regular_open=is_regular, is_extended_open=is_extended,
            closes_at=closes_at.strftime("%H:%M") if closes_at else "",
            reason=reason,
        )

    def is_open(self, now: Optional[datetime] = None) -> Tuple[bool, str]:
        info = self.session(now)
        if self.allow_extended:
            return info.is_extended_open, f"{info.phase} · {info.reason}"
        return info.is_regular_open, f"{info.phase} · {info.reason}"

    def describe(self) -> Dict[str, Any]:
        return {
            "market": self.market,
            "tz": self.tz_name,
            "regular": [w.as_list() for w in self.windows],
            "pre": self.pre_window.as_list() if self.pre_window else None,
            "post": self.post_window.as_list() if self.post_window else None,
            "night": self.night_window.as_list() if self.night_window else None,
            "early_close": sorted(d.isoformat() for d in self.early_close),
            "allow_extended": self.allow_extended,
            "holiday_count": len(self.holidays),
        }


# --------------------------------------------------------------------------- #
# 配置解析
# --------------------------------------------------------------------------- #
def build_clock(market: str, spec: Any, *,
                holidays: Optional[Iterable[date]] = None,
                early_close: Optional[Dict[date, time]] = None) -> ExchangeClock:
    """把配置里的一段 ``sessions.<market>`` 解析成 :class:`ExchangeClock`。

    支持两种写法（后者是旧格式，保持兼容）：

    * 新：``{tz: America/New_York, regular: [["09:30","16:00"]],
      pre: ["04:00","09:30"], post: [...], night: [...], allow_extended: false}``
    * 旧：``[((9,30),(11,30)), ((13,0),(15,0))]`` —— 仅常规窗口，
      时区取 ``TZ_BY_MARKET`` 中该市场的交易所时区
    """
    mkt = str(market or "").lower()
    default_tz = TZ_BY_MARKET.get(mkt, "UTC")

    if isinstance(spec, dict):
        tz_name = str(spec.get("tz") or default_tz)
        regular_raw = spec.get("regular") or spec.get("windows")
        windows = [w for w in (_coerce_window(x) for x in (regular_raw or [])) if w]
        pre = _coerce_window(spec.get("pre"))
        post = _coerce_window(spec.get("post"))
        night = _coerce_window(spec.get("night"))
        allow_ext = bool(spec.get("allow_extended", spec.get("extended_tradable", False)))
        ec = dict(early_close or {})
        for d, hm in (spec.get("early_close") or {}).items():
            try:
                t = _parse_hm(hm)
                if t:
                    ec[date.fromisoformat(str(d))] = t
            except ValueError:
                continue
    else:
        tz_name = default_tz
        windows = [w for w in (_coerce_window(x) for x in (spec or [])) if w]
        pre = post = night = None
        allow_ext = False
        ec = dict(early_close or {})

    if not windows:
        # 没有常规窗口 = 配置错误。这里仍造一个空窗口时钟，由上层 fail-closed。
        windows = []
    return ExchangeClock(
        mkt, windows, tz_name=tz_name, pre_window=pre, post_window=post,
        night_window=night, holidays=holidays, early_close=ec,
        allow_extended=allow_ext,
    )
