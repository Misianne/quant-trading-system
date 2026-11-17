"""复星行情：分时段实时性判定（纯标准库，无 Provider 依赖）。

设计前提：**官方没有公布四个时段的边界时间**。

我们查过的官方资料（SDK 源码、README、`fw-trading` 的 onboarding/keygen/skill 文档）
只说明美股"行情级别 L1（盘前/盘中/盘后）"、推送主题有 `pqt/pmin/ptk`（盘前盘后）
与 `qt_nt/min_nt/tk_nt`（夜盘），**从未给出分界时刻**。

因此本模块把"时钟判定"和"结论判定"严格分开：

* ``phase_of()`` 依赖一组**边界假设**，其来源被显式标注为
  ``SPEC_SOURCE_UNVERIFIED``。它只用于"当前应该期待哪个官方对象"，
  **不作为实时性证据**。
* 实时性结论只依据**官方返回的对象本身**：该对象是否存在、价格、官方时间戳、
  ``delay`` 标记、以及两次采样之间时间戳是否推进。

这样即使时钟假设错了，也不会误判成"实时"——最坏是多标一次
``NO_SESSION_DATA`` 或 ``AMBIGUOUS``。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------- #
# 结论常量（与任务约定的六个取值一一对应）
# ---------------------------------------------------------------------- #
REALTIME_CONFIRMED = "REALTIME_CONFIRMED"
DELAYED = "DELAYED"
NO_SESSION_DATA = "NO_SESSION_DATA"
INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
AMBIGUOUS = "AMBIGUOUS"
API_ERROR = "API_ERROR"

VERDICTS = (REALTIME_CONFIRMED, DELAYED, NO_SESSION_DATA,
            INSUFFICIENT_EVIDENCE, AMBIGUOUS, API_ERROR)

#: 官方子对象名 → 它可能属于的时段。字段名是厂商自己的分类，比时间更权威。
OBJECT_BUCKETS: Dict[str, Tuple[str, ...]] = {
    "prepost": ("pre", "post"),
    "nightQuote": ("night",),
}

#: 当前时段应期待哪个官方对象。这个映射**不涉及边界时刻**，只涉及"哪个对象对应该时段"。
PRIMARY_OBJECT: Dict[str, str] = {
    "night": "nightQuote",
    "pre": "prepost",
    "regular": "main",
    "post": "prepost",
}

#: 边界假设的来源标注。看到这个字符串就说明"边界不是官方给的"。
SPEC_SOURCE_UNVERIFIED = (
    "UNVERIFIED：官方资料未公布四时段边界；此处仅用于选择应期待哪个官方对象，"
    "不参与实时性判定")


# ---------------------------------------------------------------------- #
# 时间边界（可配置；默认值明确标注为未验证）
# ---------------------------------------------------------------------- #
@dataclass(frozen=True)
class SessionSpec:
    """时段边界。默认值来自美股通行惯例，**不是**官方文档。"""

    pre_start: time = time(4, 0)
    regular_start: time = time(9, 30)
    post_start: time = time(16, 0)
    night_start: time = time(20, 0)
    source: str = SPEC_SOURCE_UNVERIFIED

    def phase_of(self, ny_dt: datetime) -> str:
        """按美东墙钟判时段。``night`` 跨午夜。"""
        t = ny_dt.timetz().replace(tzinfo=None)
        if self.regular_start <= t < self.post_start:
            return "regular"
        if self.pre_start <= t < self.regular_start:
            return "pre"
        if self.post_start <= t < self.night_start:
            return "post"
        return "night"

    def boundaries_dict(self) -> Dict[str, str]:
        return {"pre_start": self.pre_start.strftime("%H:%M"),
                "regular_start": self.regular_start.strftime("%H:%M"),
                "post_start": self.post_start.strftime("%H:%M"),
                "night_start": self.night_start.strftime("%H:%M")}


DEFAULT_SPEC = SessionSpec()


# ---------------------------------------------------------------------- #
# 交易日 / 夏令时
# ---------------------------------------------------------------------- #
def is_trading_day(d: date, holidays: Optional[Iterable[date]] = None) -> bool:
    """周末与（给定的）节假日不是交易日。

    ``holidays`` 为 None 时**只判周末**——这必须在输出里如实标注，
    不能默认"今天一定开市"。
    """
    if d.weekday() >= 5:
        return False
    if holidays and d in set(holidays):
        return False
    return True


def to_new_york(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("naive datetime 不接受：必须先带时区")
    return dt.astimezone(NY)


def phase_at(dt_utc: datetime, spec: SessionSpec = DEFAULT_SPEC) -> str:
    return spec.phase_of(to_new_york(dt_utc))


# ---------------------------------------------------------------------- #
# 单个官方对象的证据
# ---------------------------------------------------------------------- #
@dataclass
class ObjectEvidence:
    key: str                        # main / prepost / nightQuote
    present: bool
    price: Optional[float] = None
    event_time: Optional[datetime] = None
    delay: Optional[bool] = None
    raw_type: Optional[int] = None
    session: str = "unknown"
    verdict: str = INSUFFICIENT_EVIDENCE
    reason: str = ""
    skew_seconds: Optional[float] = None

    def as_dict(self) -> Dict[str, object]:
        return {"key": self.key, "present": self.present, "price": self.price,
                "event_time": self.event_time.isoformat() if self.event_time else None,
                "delay": self.delay, "raw_type": self.raw_type,
                "session": self.session, "verdict": self.verdict,
                "reason": self.reason, "skew_seconds": self.skew_seconds}


def classify_object_session(key: str, event_time: Optional[datetime],
                            spec: SessionSpec = DEFAULT_SPEC) -> str:
    """对象所属时段：字段名给候选集合，时间只在集合内细分，冲突即 ambiguous。"""
    if event_time is None:
        return "unknown"
    derived = phase_at(event_time, spec)
    allowed = OBJECT_BUCKETS.get(key)
    if not allowed:
        return derived
    return derived if derived in allowed else "ambiguous"


def evaluate_object(key: str, *, present: bool,
                    price: Optional[float] = None,
                    event_time: Optional[datetime] = None,
                    delay: Optional[bool] = None,
                    raw_type: Optional[int] = None,
                    now: Optional[datetime] = None,
                    continuity: Optional[bool] = None,
                    max_skew_seconds: float = 60.0,
                    spec: SessionSpec = DEFAULT_SPEC) -> ObjectEvidence:
    """判定单个对象是否"实时"。

    必要条件（缺一即不通过）：
      1. 对象存在且有价格
      2. 有官方时间戳
      3. 有明确的 ``delay`` 标记（缺失 → 证据不足，**不默认 False**）
      4. ``delay`` 为 False
      5. 时间戳与当前时间偏差在阈值内
      6. 两次采样之间时间戳**推进过**（连续性证据）

    第 6 条是刻意的：``delay=false`` 单独不足以证明实时，必须看到它在动。
    单次采样（``continuity=None``）只能给 INSUFFICIENT_EVIDENCE。
    """
    ev = ObjectEvidence(key=key, present=bool(present), price=price,
                        event_time=event_time, delay=delay, raw_type=raw_type)
    ev.session = classify_object_session(key, event_time, spec) if present else "unknown"

    if not present:
        ev.verdict, ev.reason = NO_SESSION_DATA, "官方响应中不存在该对象"
        return ev
    if price is None or not isinstance(price, (int, float)) or float(price) <= 0:
        ev.verdict, ev.reason = INSUFFICIENT_EVIDENCE, "缺少有效价格"
        return ev
    if event_time is None:
        ev.verdict, ev.reason = INSUFFICIENT_EVIDENCE, "缺少官方时间戳"
        return ev
    if delay is None:
        ev.verdict, ev.reason = INSUFFICIENT_EVIDENCE, "缺少 delay 标记"
        return ev
    if now is None:
        ev.verdict, ev.reason = INSUFFICIENT_EVIDENCE, "缺少判定基准时间"
        return ev

    ev.skew_seconds = round((now - event_time).total_seconds(), 3)
    if delay is True:
        ev.verdict, ev.reason = DELAYED, "官方标记 delay=true"
        return ev
    if abs(ev.skew_seconds) > float(max_skew_seconds):
        ev.verdict = INSUFFICIENT_EVIDENCE
        ev.reason = f"时间戳偏差 {ev.skew_seconds:.0f}s 超过阈值 {max_skew_seconds:.0f}s"
        return ev
    if continuity is None:
        ev.verdict = INSUFFICIENT_EVIDENCE
        ev.reason = "仅单次采样，无连续更新证据（delay=false 不单独等于实时）"
        return ev
    if continuity is False:
        ev.verdict = INSUFFICIENT_EVIDENCE
        ev.reason = "两次采样之间时间戳未推进"
        return ev
    ev.verdict, ev.reason = REALTIME_CONFIRMED, "delay=false + 时间戳新鲜 + 已推进"
    return ev


# ---------------------------------------------------------------------- #
# 整体判定
# ---------------------------------------------------------------------- #
@dataclass
class SessionReport:
    phase: str
    verdict: str
    reason: str
    trading_day: Optional[bool] = None
    holiday_source: str = "weekday_only"
    objects: List[ObjectEvidence] = field(default_factory=list)

    def as_dict(self) -> Dict[str, object]:
        return {"phase": self.phase, "verdict": self.verdict,
                "reason": self.reason, "trading_day": self.trading_day,
                "holiday_source": self.holiday_source,
                "objects": [o.as_dict() for o in self.objects]}


def summarize(phase: str, objects: Sequence[ObjectEvidence], *,
              trading_day: Optional[bool] = None,
              holiday_source: str = "weekday_only") -> SessionReport:
    """以**该时段对应的官方对象**为准给结论。

    不用主报价的实时性去证明夜盘实时——每个时段只认自己的对象。
    """
    by_key = {o.key: o for o in objects}
    primary_key = PRIMARY_OBJECT.get(phase)
    if primary_key is None:
        return SessionReport(phase=phase, verdict=AMBIGUOUS,
                             reason=f"未知时段 {phase!r}",
                             trading_day=trading_day,
                             holiday_source=holiday_source, objects=list(objects))

    primary = by_key.get(primary_key)
    if primary is None:
        return SessionReport(phase=phase, verdict=INSUFFICIENT_EVIDENCE,
                             reason=f"缺少 {primary_key} 的证据",
                             trading_day=trading_day,
                             holiday_source=holiday_source, objects=list(objects))

    if trading_day is False:
        return SessionReport(phase=phase, verdict=NO_SESSION_DATA,
                             reason="非交易日",
                             trading_day=False,
                             holiday_source=holiday_source, objects=list(objects))

    if primary.session == "ambiguous":
        return SessionReport(phase=phase, verdict=AMBIGUOUS,
                             reason=f"{primary_key} 的时间与字段名冲突，时段无法确定",
                             trading_day=trading_day,
                             holiday_source=holiday_source, objects=list(objects))

    return SessionReport(phase=phase, verdict=primary.verdict,
                         reason=f"以 {primary_key} 为准：{primary.reason}",
                         trading_day=trading_day,
                         holiday_source=holiday_source, objects=list(objects))


# ---------------------------------------------------------------------- #
# 请求预算（防止越界请求）
# ---------------------------------------------------------------------- #
class RequestBudget:
    """请求次数上限。超过即抛错，不给"再试一次"的机会。"""

    def __init__(self, max_requests: int = 2, min_interval_seconds: float = 5.0):
        self.max_requests = int(max_requests)
        self.min_interval = float(min_interval_seconds)
        self._used = 0
        self._last_at: Optional[datetime] = None

    @property
    def used(self) -> int:
        return self._used

    @property
    def remaining(self) -> int:
        return max(0, self.max_requests - self._used)

    def acquire(self, now: datetime) -> None:
        if self._used >= self.max_requests:
            raise RuntimeError(f"请求预算已用尽（上限 {self.max_requests} 次）")
        if self._last_at is not None:
            gap = (now - self._last_at).total_seconds()
            if gap < self.min_interval:
                raise RuntimeError(
                    f"两次请求间隔 {gap:.1f}s 小于下限 {self.min_interval:.0f}s")
        self._used += 1
        self._last_at = now
