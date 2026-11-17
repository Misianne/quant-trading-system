"""日线数据闸门 —— 取代"实时推送新鲜度"作为开仓前置条件。

为什么需要它
------------
WebSocket 解除强制依赖之后，主链路只剩**日线**。如果日线缺失、过期或与
交易日历不一致，系统就会拿旧收盘价冒充最新价去开新仓 —— 这是比"没有实时行情"
更危险的状态。本模块把这件事做成**可判定的闸门**，而不是散落在各处的 if。

判定口径（全部为硬条件，任一不满足即 ``ok=False``）
----------------------------------------------------
1. **对齐**：决策日 D 必须等于该标的**最后一根已入库 bar 的日期**；
   D 有任何标的缺 bar → ``DATA_MISSING``（不允许向前取旧值顶替）。
2. **充分**：每标的至少 ``min_bars`` 根（默认取策略所需 210 根）。
3. **新鲜**：入库的最后一根不得早于 D → 否则 ``DATA_STALE``。
4. **日历一致**：D 必须出现在"所有标的日期交集"里 → 否则 ``CALENDAR_GAP``
   （说明有标的当天没数据，或数据源把非交易日当成了交易日）。
5. **价格合规**：D 当日 OHLC 必须通过 ``validate_bar``（非 NaN、high>=body、
   low<=body 等）→ 否则 ``BAD_BAR``。

闸门**只读**，不写库、不改数据、不修补缺口。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..us_loop.bars import BarStore, normalize_symbol, validate_bar

OK = "OK"
DATA_MISSING = "DATA_MISSING"
DATA_STALE = "DATA_STALE"
CALENDAR_GAP = "CALENDAR_GAP"
INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
BAD_BAR = "BAD_BAR"
NO_DATA = "NO_DATA"

#: 策略侧需要的最少历史（与 daily_swing.MIN_HISTORY 对齐）
DEFAULT_MIN_BARS = 210


@dataclass
class GateResult:
    ok: bool
    code: str
    detail: str
    decision_date: str
    as_of: Dict[str, str] = field(default_factory=dict)   # symbol -> 最后一根日期
    bars: Dict[str, int] = field(default_factory=dict)
    blocking: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "code": self.code, "detail": self.detail,
                "decision_date": self.decision_date, "as_of": dict(self.as_of),
                "bars": dict(self.bars), "blocking": list(self.blocking)}

    def __bool__(self) -> bool:      # 允许 `if gate:` 直接判断
        return self.ok


def _is_weekday(day: str) -> bool:
    try:
        return date.fromisoformat(day).weekday() < 5
    except ValueError:
        return False


def intersect_calendar(store: BarStore, symbols: Sequence[str]) -> List[str]:
    """所有标的都有 bar 的交易日（= 可用于决策的日历）。"""
    sets = []
    for s in symbols:
        rows = store.load(s, num=100000)
        sets.append({str(r["trade_date"]) for r in rows})
    if not sets:
        return []
    common = set.intersection(*sets)
    return sorted(common)


def check(store: BarStore, symbols: Sequence[str], decision_date: str,
          min_bars: int = DEFAULT_MIN_BARS) -> GateResult:
    """在 ``decision_date`` 收盘后判断能否据此决策/开仓。"""
    syms = [normalize_symbol(s) for s in symbols if normalize_symbol(s)]
    if not syms:
        return GateResult(False, NO_DATA, "未提供任何标的", decision_date)

    as_of: Dict[str, str] = {}
    counts: Dict[str, int] = {}
    blocking: List[str] = []
    for s in syms:
        rows = store.load(s, num=100000)
        counts[s] = len(rows)
        if not rows:
            as_of[s] = ""
            blocking.append(f"{s}: 无任何日线")
            continue
        as_of[s] = str(rows[-1]["trade_date"])
        if len(rows) < min_bars:
            blocking.append(f"{s}: 历史 {len(rows)} < {min_bars}")
        if as_of[s] < decision_date:
            blocking.append(f"{s}: 最后一根 {as_of[s]} < 决策日 {decision_date}（数据过期）")
        elif as_of[s] > decision_date:
            blocking.append(f"{s}: 最后一根 {as_of[s]} > 决策日 {decision_date}"
                            f"（决策日尚未收盘，出现未来数据）")
        # 当日 bar 合规性
        today = [r for r in rows if str(r["trade_date"]) == decision_date]
        if today:
            clean, why = validate_bar(today[0])
            if clean is None:
                blocking.append(f"{s}: {decision_date} 的 bar 不合法（{why}）")
        else:
            blocking.append(f"{s}: 缺 {decision_date} 的 bar")

    cal = intersect_calendar(store, syms)
    if decision_date not in cal:
        blocking.append(f"{decision_date} 不在所有标的的公共交易日里（日历缺口）")

    if not blocking:
        return GateResult(True, OK, "日线完备且与决策日对齐", decision_date,
                          as_of, counts)

    # 归因：优先报最严重的一类
    joined = " | ".join(blocking)
    if any("无任何日线" in b for b in blocking):
        code = NO_DATA
    elif any("数据过期" in b for b in blocking):
        code = DATA_STALE
    elif any("日历缺口" in b for b in blocking):
        code = CALENDAR_GAP
    elif any("历史" in b and "<" in b for b in blocking):
        code = INSUFFICIENT_HISTORY
    elif any("不合法" in b for b in blocking):
        code = BAD_BAR
    else:
        code = DATA_MISSING
    return GateResult(False, code, joined, decision_date, as_of, counts, blocking)


def latest_common_session(store: BarStore, symbols: Sequence[str]) -> Optional[str]:
    """所有标的都有的最后一个交易日（用于确定"最近一次可用收盘"）。"""
    cal = intersect_calendar(store, symbols)
    return cal[-1] if cal else None


def weekday_gaps(store: BarStore, symbols: Sequence[str]) -> Dict[str, List[str]]:
    """各标的相对"公共日历"的工作日缺口（缺数据 vs 节假日的直接证据）。

    公共日历本身已剔除所有标的都没有的日子（周末/节假日）；因此
    ``A 有、B 没有`` 的那些日期就是 **B 的数据缺口**，而不是节假日。
    """
    cal = set(intersect_calendar(store, symbols))
    out: Dict[str, List[str]] = {}
    for s in symbols:
        sym = normalize_symbol(s)
        rows = store.load(sym, num=100000)
        have = {str(r["trade_date"]) for r in rows}
        if not have:
            out[sym] = []
            continue
        lo, hi = min(have), max(have)
        gaps = sorted(d for d in cal if lo <= d <= hi and d not in have)
        out[sym] = gaps
    return out
