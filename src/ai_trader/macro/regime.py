"""宏观状态判定（研究层，**不产生任何交易信号**）。

设计原则
--------
1. **只看某一时点已公开的数据**：所有输入来自 ``MacroSnapshot``，而它已经按
   ``release_epoch <= as_of`` 过滤。本模块不直接读数据库表。
2. **多指标投票，不用单一阈值**：通胀由 CPI / 核心 CPI / PCE / 核心 PCE 四项投票，
   增长由非农动量 / 失业率变化 / Sahm 规则 / GDP 同比四项投票。单条指标越界不会
   单独改变结论。
3. **信号分歧本身就是结论**：当同一块内指标互相矛盾（例如 CPI 在降、核心 PCE 在升），
   返回 ``MIXED``，映射到"信号相互矛盾"状态，而不是强行归边。
4. **可解释**：每次判定都带支持证据、反对证据、数据版本、置信度与失效条件。

阈值全部集中在文件顶部，并标注"先验设定、未做拟合"，便于做敏感性分析。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# 状态
# --------------------------------------------------------------------------- #
COOLING_RESILIENT = "COOLING_RESILIENT"        # 通胀回落、经济保持韧性
COOLING_WEAKENING = "COOLING_WEAKENING"        # 通胀回落、经济明显恶化
HEATING_RESILIENT = "HEATING_RESILIENT"        # 通胀反弹、经济保持韧性
HEATING_WEAKENING = "HEATING_WEAKENING"        # 通胀反弹、经济明显恶化
CONTRADICTORY = "CONTRADICTORY"                # 增长与通胀信号相互矛盾
INSUFFICIENT = "INSUFFICIENT"                  # 数据不足

REGIME_LABEL = {
    COOLING_RESILIENT: "通胀回落、经济保持韧性",
    COOLING_WEAKENING: "通胀回落、经济明显恶化",
    HEATING_RESILIENT: "通胀反弹、经济保持韧性",
    HEATING_WEAKENING: "通胀反弹、经济明显恶化",
    CONTRADICTORY: "经济增长与通胀信号相互矛盾",
    INSUFFICIENT: "数据不足，无法判断",
}

# 通胀方向
COOLING, HEATING, FLAT, MIXED, UNKNOWN = "cooling", "heating", "flat", "mixed", "unknown"
# 增长方向
RESILIENT, WEAKENING = "resilient", "weakening"

# --------------------------------------------------------------------------- #
# 参数（**先验设定，未对收益做任何拟合**）
# --------------------------------------------------------------------------- #
#: 同比变化多少个"百分点"才算有方向（0.10pp）
YOY_DELTA_EPS = 0.10
#: 3 个月折年率与同比相差多少个百分点才算确认（0.20pp）
ANN3M_GAP_EPS = 0.20
#: 通胀块：投票绝对值 >= 该值判定方向
INFL_VOTE_MIN = 2
#: 增长块：投票绝对值 >= 该值判定方向
GROWTH_VOTE_MIN = 2
#: 非农 3 月均值 / 12 月均值 的比值阈值
PAYROLL_RESILIENT_RATIO = 0.75
PAYROLL_WEAKENING_RATIO = 0.45
#: 失业率 3 个月上升幅度（百分点）
UNRATE_RISE_WEAKENING = 0.30
UNRATE_RISE_RESILIENT = 0.10
#: Sahm 规则阈值（3 月均值 - 12 月最低）
SAHM_TRIGGER = 0.50
#: GDP 同比
GDP_YOY_RESILIENT = 0.015
GDP_YOY_WEAKENING = 0.000
#: 数据新鲜度：最新一期与 as_of 的最大允许滞后（月）
FRESH_MONTHS = 2

INFLATION_SERIES = ("CPIAUCSL", "CPILFESL", "PCEPI", "PCEPILFE")
LABOR_SERIES = ("PAYEMS", "UNRATE")
GROWTH_SERIES = ("GDP",)


class RegimeError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def shift_months(period: str, n: int) -> str:
    """``2026-10-01`` 平移 n 个月，返回月初字符串（不依赖 dateutil）。"""
    y, m = int(period[:4]), int(period[5:7])
    total = y * 12 + (m - 1) + n
    return f"{total // 12:04d}-{total % 12 + 1:02d}-01"


def shift_quarters(period: str, n: int) -> str:
    y, q = int(period[:4]), (int(period[5:7]) - 1) // 3
    total = y * 4 + q + n
    return f"{total // 4:04d}-{total % 4 * 3 + 1:02d}-01"


def _levels(snap, sid: str) -> Dict[str, float]:
    """快照里某序列的 ``{期间: 值}``。快照已按 as_of 过滤，这里不再做时间判断。"""
    pts = snap.by_series.get(sid) or []
    return {p.observation_period: p.value for p in pts if p.value is not None}


def _latest(snap, sid: str):
    pts = snap.by_series.get(sid) or []
    return pts[-1] if pts else None


def _yoy(levels: Dict[str, float], period: str, months: int = 12) -> Optional[float]:
    prev = levels.get(shift_months(period, -months))
    cur = levels.get(period)
    if prev is None or cur is None or prev <= 0:
        return None
    return cur / prev - 1.0


def _ann3m(levels: Dict[str, float], period: str) -> Optional[float]:
    """3 个月折年率（月频）。用 (L_t / L_{t-3})^4 - 1，比看单月环比稳。"""
    prev = levels.get(shift_months(period, -3))
    cur = levels.get(period)
    if prev is None or cur is None or prev <= 0:
        return None
    return (cur / prev) ** 4 - 1.0


def _latest_period(levels: Dict[str, float]) -> Optional[str]:
    return max(levels) if levels else None


# --------------------------------------------------------------------------- #
# 结果结构
# --------------------------------------------------------------------------- #
@dataclass
class Vote:
    name: str
    value: str                    # 人类可读的取值摘要
    vote: int                     # +1 该块朝某方向 / -1 反向 / 0 中性
    note: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"indicator": self.name, "value": self.value,
                "vote": self.vote, "note": self.note}


@dataclass
class Block:
    direction: str
    votes: List[Vote] = field(default_factory=list)
    score: int = 0
    detail: Dict[str, Any] = field(default_factory=dict)
    stale: bool = False

    @property
    def total(self) -> int:
        return self.score

    def as_dict(self) -> Dict[str, Any]:
        return {"direction": self.direction, "score": self.score,
                "stale": self.stale, "votes": [v.as_dict() for v in self.votes],
                "detail": self.detail}


@dataclass
class Assessment:
    state: str
    as_of: str
    inflation: Block
    growth: Block
    policy: Dict[str, Any]
    confidence: str
    evidence_for: List[str] = field(default_factory=list)
    evidence_against: List[str] = field(default_factory=list)
    data_dates: Dict[str, str] = field(default_factory=dict)
    versions: Dict[str, str] = field(default_factory=dict)
    invalidators: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return REGIME_LABEL[self.state]

    def as_dict(self) -> Dict[str, Any]:
        return {"state": self.state, "label": self.label, "as_of": self.as_of,
                "confidence": self.confidence,
                "inflation": self.inflation.as_dict(),
                "growth": self.growth.as_dict(),
                "policy": self.policy,
                "evidence_for": self.evidence_for,
                "evidence_against": self.evidence_against,
                "data_dates": self.data_dates, "versions": self.versions,
                "invalidators": self.invalidators, "notes": self.notes}


# --------------------------------------------------------------------------- #
# 三个块
# --------------------------------------------------------------------------- #
def inflation_votes(detail: Dict[str, Any], eps: float = YOY_DELTA_EPS,
                    gap_eps: float = ANN3M_GAP_EPS) -> List[Vote]:
    """由四个序列的原始读数生成投票。**纯函数**，便于研究层换阈值重算。"""
    votes: List[Vote] = []
    for sid in INFLATION_SERIES:
        d = detail.get(sid)
        if not d:
            votes.append(Vote(sid, "数据不足", 0, "该序列在该时点不可用"))
            continue
        y, y3, a3 = d.get("yoy"), d.get("yoy_3m_ago"), d.get("ann3m")
        if y is None or y3 is None or a3 is None:
            votes.append(Vote(sid, "数据不足", 0, "缺 13 个月历史"))
            continue
        delta = (y - y3) * 100.0          # 百分点
        gap = (a3 - y) * 100.0            # 百分点（3m 折年 相对 同比）
        if delta <= -eps and gap <= -gap_eps:
            v, why = -1, ["同比下行", "3m 折年低于同比"]
        elif delta >= eps and gap >= gap_eps:
            v, why = +1, ["同比上行", "3m 折年高于同比"]
        else:
            v, why = 0, [f"同比变化 {delta:+.2f}pp / 3m 折年与同比差 {gap:+.2f}pp 未同向确认"]
        votes.append(Vote(sid, f"同比 {y:.2%}，3m 变化 {delta:+.2f}pp，"
                               f"3m 折年 {a3:.2%}", v, "；".join(why)))
    return votes


def direction_from_votes(votes: List[Vote], vote_min: int = INFL_VOTE_MIN) -> Tuple[str, int]:
    score = sum(v.vote for v in votes)
    have_data = sum(1 for v in votes if not v.value.startswith("数据不足"))
    if have_data < 2:
        return UNKNOWN, score
    if score >= vote_min:
        return HEATING, score
    if score <= -vote_min:
        return COOLING, score
    return MIXED, score


def inflation_block(snap) -> Block:
    """通胀方向：4 项同比的 3 个月变化 + 3 个月折年率与同比的差。"""
    detail: Dict[str, Any] = {}
    periods = []
    for sid in INFLATION_SERIES:
        lv = _levels(snap, sid)
        p = _latest_period(lv)
        if p is None:
            continue
        periods.append(p)
        detail[sid] = {"period": p, "yoy": _yoy(lv, p),
                       "yoy_3m_ago": _yoy(lv, shift_months(p, -3)),
                       "ann3m": _ann3m(lv, p)}

    votes = inflation_votes(detail)
    direction, score = direction_from_votes(votes)

    stale = False
    if periods:
        newest = max(periods)
        # 月度序列：最新一期距 as_of 超过 FRESH_MONTHS 个月即视为陈旧
        as_of_month = snap.as_of[:7] + "-01"
        stale = shift_months(newest, FRESH_MONTHS) < as_of_month
    return Block(direction, votes, score, detail, stale)


def growth_block(snap) -> Block:
    """增长方向：非农动量、失业率 3 月变化、Sahm 规则、GDP 同比。"""
    votes: List[Vote] = []
    detail: Dict[str, Any] = {}

    # --- 非农 ---
    pay = _levels(snap, "PAYEMS")
    pp = _latest_period(pay)
    if pp is not None:
        mos = [shift_months(pp, -i) for i in range(0, 13)]
        ch = [pay[mos[i]] - pay[mos[i + 1]]
              for i in range(12) if mos[i] in pay and mos[i + 1] in pay]
        avg3 = sum(ch[:3]) / 3 if len(ch) >= 3 else None
        avg12 = sum(ch[:12]) / 12 if len(ch) >= 12 else None
        ratio = (avg3 / avg12) if (avg3 is not None and avg12) else None
        detail["PAYEMS"] = {"period": pp, "avg3": avg3, "avg12": avg12, "ratio": ratio}
        if ratio is None:
            votes.append(Vote("PAYEMS", "数据不足", 0, "缺 13 个月历史"))
        elif ratio >= PAYROLL_RESILIENT_RATIO:
            votes.append(Vote("PAYEMS", f"3m 均值 {avg3:,.0f}k / 12m 均值 {avg12:,.0f}k"
                                       f" = {ratio:.2f}", +1, "就业动能未明显衰减"))
        elif ratio <= PAYROLL_WEAKENING_RATIO:
            votes.append(Vote("PAYEMS", f"3m 均值 {avg3:,.0f}k / 12m 均值 {avg12:,.0f}k"
                                       f" = {ratio:.2f}", -1, "就业动能明显衰减"))
        else:
            votes.append(Vote("PAYEMS", f"比值 {ratio:.2f}", 0, "处于中间区间"))

    # --- 失业率 ---
    une = _levels(snap, "UNRATE")
    up = _latest_period(une)
    if up is not None:
        p3 = shift_months(up, -3)
        d3 = (une[up] - une[p3]) if p3 in une else None
        base = [une[shift_months(up, -i)] for i in range(0, 12)
                if shift_months(up, -i) in une]
        ma3 = (sum([une[shift_months(up, -i)] for i in range(3)
                    if shift_months(up, -i) in une]) / 3) if len(base) >= 3 else None
        sahm = (ma3 - min(base)) if (ma3 is not None and base) else None
        detail["UNRATE"] = {"period": up, "delta_3m": d3, "sahm": sahm}
        if d3 is None:
            votes.append(Vote("UNRATE", "数据不足", 0, "缺 4 个月历史"))
        elif d3 >= UNRATE_RISE_WEAKENING:
            votes.append(Vote("UNRATE", f"{une[p3]:.1f}% → {une[up]:.1f}%"
                                       f"（3m {d3:+.1f}pp）", -1, "失业率明显上行"))
        elif d3 <= UNRATE_RISE_RESILIENT:
            votes.append(Vote("UNRATE", f"{une[p3]:.1f}% → {une[up]:.1f}%"
                                       f"（3m {d3:+.1f}pp）", +1, "失业率未恶化"))
        else:
            votes.append(Vote("UNRATE", f"3m {d3:+.1f}pp", 0, "小幅上行，未达阈值"))
        if sahm is not None and sahm >= SAHM_TRIGGER:
            votes.append(Vote("SAHM", f"3m 均值高于 12m 低点 {sahm:.2f}pp", -1,
                              "Sahm 规则触发（衰退经验信号）"))

    # --- GDP（名义值用 CPI 平减，得到实际增长代理）---
    gdp = _levels(snap, "GDP")
    gp = _latest_period(gdp)
    if gp is not None:
        cpi = _levels(snap, "CPIAUCSL")
        g_prev = shift_quarters(gp, -4)
        nominal = (gdp[gp] / gdp[g_prev] - 1.0) if g_prev in gdp else None
        # 平减：同一窗口的名义增长 ÷ 同一窗口的 CPI 涨幅
        lead_m = gp[:7] + "-01"
        cpi_window = _yoy(cpi, lead_m) if lead_m in cpi else None
        real = None
        if nominal is not None and cpi_window is not None and (1 + cpi_window) > 0:
            real = (1 + nominal) / (1 + cpi_window) - 1.0
        detail["GDP"] = {"period": gp, "nominal_yoy": nominal,
                         "cpi_yoy": cpi_window, "real_yoy_proxy": real}
        if real is None:
            votes.append(Vote("GDP", "数据不足", 0, "缺 5 个季度或对应月度 CPI"))
        elif real >= GDP_YOY_RESILIENT:
            votes.append(Vote("GDP", f"实际同比(CPI 平减) {real:.2%}", +1,
                              "实际产出仍在扩张"))
        elif real <= GDP_YOY_WEAKENING:
            votes.append(Vote("GDP", f"实际同比(CPI 平减) {real:.2%}", -1,
                              "实际产出同比停滞或转负"))
        else:
            votes.append(Vote("GDP", f"实际同比(CPI 平减) {real:.2%}", 0, "低速扩张"))

    score = sum(v.vote for v in votes)
    if score >= GROWTH_VOTE_MIN:
        direction = RESILIENT
    elif score <= -GROWTH_VOTE_MIN:
        direction = WEAKENING
    else:
        # 全中性时是"没有结论"，不是"矛盾"
        direction = MIXED if any(v.vote != 0 for v in votes) else UNKNOWN
    return Block(direction, votes, score, detail, stale=False)


def policy_block(snap) -> Dict[str, Any]:
    """政策环境：名义利率水平、3 个月变化、实际利率（DFF - CPI 同比）。"""
    dff = _levels(snap, "DFF")
    if not dff:
        return {"available": False}
    latest = max(dff)
    lvl = dff[latest]
    # 3 个月前（按日历日 90 天近似；日频序列用日期比较，不构造月）
    from datetime import date, timedelta
    target = (date.fromisoformat(latest) - timedelta(days=91)).isoformat()
    past = [p for p in dff if p <= target]
    chg = (lvl - dff[max(past)]) if past else None

    cpi = _levels(snap, "CPIAUCSL")
    cp = _latest_period(cpi)
    cpi_yoy = _yoy(cpi, cp) if cp else None
    # 实际利率按**百分点**算：名义利率(%) − CPI 同比(%)
    real = (lvl - cpi_yoy * 100.0) if cpi_yoy is not None else None

    stance = "unknown"
    if real is not None:
        if real >= 1.0:
            stance = "restrictive"
        elif real <= -1.0:
            stance = "accommodative"
        else:
            stance = "neutral"
    return {"available": True, "date": latest, "level": lvl, "change_3m": chg,
            "cpi_yoy": cpi_yoy, "real_rate": real, "stance": stance}


# --------------------------------------------------------------------------- #
# 主判定
# --------------------------------------------------------------------------- #
def combine(infl_dir: str, grow_dir: str) -> str:
    """两块方向 → 6 个状态之一。单独抽出来，便于研究层用不同阈值重算。"""
    if infl_dir in (COOLING, HEATING) and grow_dir in (RESILIENT, WEAKENING):
        if infl_dir == COOLING:
            return COOLING_RESILIENT if grow_dir == RESILIENT else COOLING_WEAKENING
        return HEATING_RESILIENT if grow_dir == RESILIENT else HEATING_WEAKENING
    if infl_dir == UNKNOWN and grow_dir == UNKNOWN:
        return INSUFFICIENT
    if infl_dir == UNKNOWN or grow_dir == UNKNOWN:
        return INSUFFICIENT
    return CONTRADICTORY          # 任一为 MIXED/FLAT


def assess(snap) -> Assessment:
    """把一份时点快照映射到 6 个状态之一。"""
    infl = inflation_block(snap)
    grow = growth_block(snap)
    state = combine(infl.direction, grow.direction)

    # --- 置信度：由两块的投票强度 + 数据新鲜度决定 ---
    strength = min(abs(infl.score), abs(grow.score))
    if state == INSUFFICIENT:
        conf = "low"
    elif strength >= 3 and not infl.stale:
        conf = "high"
    elif strength >= INFL_VOTE_MIN and not infl.stale:
        conf = "medium"
    else:
        conf = "low"

    # --- 证据 ---
    for_bits: List[str] = []
    against: List[str] = []
    for v in infl.votes:
        (for_bits if (v.vote < 0 and infl.direction == COOLING)
         or (v.vote > 0 and infl.direction == HEATING) else against).append(
            f"[通胀] {v.name}: {v.value} — {v.note}")
    for v in grow.votes:
        (for_bits if (v.vote > 0 and grow.direction == RESILIENT)
         or (v.vote < 0 and grow.direction == WEAKENING) else against).append(
            f"[增长] {v.name}: {v.value} — {v.note}")

    # --- 数据日期与版本 ---
    dates: Dict[str, str] = {}
    versions: Dict[str, str] = {}
    for sid, d in infl.detail.items():
        lp = _latest(snap, sid)
        if lp:
            dates[sid] = lp.observation_period
            versions[sid] = f"{lp.vintage_date}（{lp.time_precision}）"
    for sid in ("PAYEMS", "UNRATE", "GDP"):
        lp = _latest(snap, sid)
        if lp:
            dates[sid] = lp.observation_period
            versions[sid] = f"{lp.vintage_date}（{lp.time_precision}）"

    pol = policy_block(snap)

    # --- 失效条件 ---
    inv: List[str] = []
    if infl.direction == COOLING:
        inv.append("核心 CPI 同比连续两个月回升，或 3 个月折年率回到同比之上 → 判定通胀回落失效")
    elif infl.direction == HEATING:
        inv.append("核心 CPI 同比连续两个月回落且 3 个月折年率低于同比 → 判定通胀反弹失效")
    if grow.direction == RESILIENT:
        inv.append("非农 3 个月均值跌破 12 个月均值的 0.45 倍，或失业率 3 个月上行 ≥0.3pp，"
                   "或 Sahm 规则触发 → 判定经济韧性失效")
    elif grow.direction == WEAKENING:
        inv.append("非农 3 个月均值回到 12 个月均值的 0.75 倍以上且失业率连续两个月回落 → "
                   "判定经济恶化失效")
    inv.append("数据版本被修订：若同一期的首次公布值与当前值方向相反，"
               "该期证据需重新评估（本判定只使用决策时点可见的版本）")

    notes: List[str] = []
    if infl.stale:
        notes.append("通胀数据超过 2 个月未更新，判定可靠性下降")
    if state == CONTRADICTORY:
        notes.append("两块信号方向不一致或块内指标互相矛盾，本状态不指向任何单一结论")

    return Assessment(state=state, as_of=snap.as_of, inflation=infl, growth=grow,
                      policy=pol, confidence=conf,
                      evidence_for=for_bits, evidence_against=against,
                      data_dates=dates, versions=versions,
                      invalidators=inv, notes=notes)
