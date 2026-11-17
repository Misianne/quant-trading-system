"""日线波段策略（V1 主策略）——只吃**已确认收盘**的日线，不依赖任何实时行情。

边界
----
* 输入只有日线 OHLCV；不读 WS 推送、不读延迟快照。
* 无未来函数：指标只用 ``bars[:i]``；决策在 D 日收盘后产生，成交在 D+1 开盘
  （由执行层负责，策略本身不产生"按收盘价成交"）。
* 可解释：每个决策带 ``reason`` 与指标快照，可事后复算。

状态（全部为规则，非拟合）
--------------------------
``PANIC``          单日 <= -3.5% 或 5 日 <= -7%          → 现金
``REBOUND``        距 60 日高回撤 >= 8% 且 3 日 >= +4%
                   且仍在 SMA50 下方                     → 现金
``DOWNTREND``      close < SMA50 且 SMA50 斜率 < 0
                   且 close < SMA200 且连续确认           → 现金 / QID
``UPTREND_STRONG`` close > SMA20 > SMA50、20 日斜率 > 0、
                   60 日 >= +8%、波动未过热               → QLD(2x)
``UPTREND_NORMAL`` close > SMA50 且 SMA50 斜率 >= 0       → QQQ(1x)
``CHOP``           其余                                   → 现金

``TQQQ``(3x) 默认禁用。5 年实测（2021-11-17..2026-11-16）：QQQ +66.4%、
QLD +67.7%、TQQQ **+25.5%** —— 3x 跑输 1x，这是"不得默认启用"的数据依据。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

STRATEGY_VERSION = "daily_swing.v1"

SYM_QQQ = "usqqq"
SYM_QLD = "usqld"
SYM_QID = "usqid"
SYM_TQQQ = "ustqqq"

PANIC = "PANIC"
REBOUND = "REBOUND"
DOWNTREND = "DOWNTREND"
UPTREND_STRONG = "UPTREND_STRONG"
UPTREND_NORMAL = "UPTREND_NORMAL"
CHOP = "CHOP"
ALL_REGIMES = (PANIC, REBOUND, DOWNTREND, UPTREND_STRONG, UPTREND_NORMAL, CHOP)

BUY = "buy"
SELL = "sell"
HOLD = "hold"
CASH = "cash"

SMA_FAST, SMA_SLOW, SMA_LONG, SLOPE_LOOKBACK, DD_WINDOW = 20, 50, 200, 10, 60
MIN_HISTORY = SMA_LONG + SLOPE_LOOKBACK

PANIC_1D, PANIC_5D = -0.035, -0.070
REBOUND_DD, REBOUND_3D = -0.08, 0.040
STRONG_60D, STRONG_VOL_CAP = 0.08, 0.28
DOWN_CONFIRM_DAYS = 10

TARGET_PCT_1X, TARGET_PCT_2X, TARGET_PCT_QID = 1.00, 0.95, 0.90
QID_TP = 0.05
QID_MAX_SESSIONS = 2
#: 状态需连续维持多少个交易日才允许**开仓**（防单日翻转导致的来回打脸）
MIN_ENTRY_STREAK = 2
#: 平仓后同一标的的冷却交易日数（$300 本金下一次往返成本约 0.67%，
#: 冷却期是成本约束，不是择时优化）
REENTRY_COOLDOWN = 5


class DailyStrategyError(RuntimeError):
    """数据不足或参数非法。"""


def _r(v: Optional[float], nd: int = 6) -> Optional[float]:
    return None if v is None else round(float(v), nd)


@dataclass(frozen=True)
class TrendSnapshot:
    as_of: str
    close: float
    sma_fast: Optional[float]
    sma_slow: Optional[float]
    sma_long: Optional[float]
    slope_slow: Optional[float]
    ret_1d: float
    ret_3d: float
    ret_5d: float
    ret_20d: float
    ret_60d: float
    dd_from_high: float
    atr_pct: float
    vol_20d: float

    def as_dict(self) -> Dict[str, Any]:
        return {"as_of": self.as_of, "close": round(self.close, 4),
                "sma20": _r(self.sma_fast), "sma50": _r(self.sma_slow),
                "sma200": _r(self.sma_long), "slope50": _r(self.slope_slow),
                "ret_1d": _r(self.ret_1d), "ret_3d": _r(self.ret_3d),
                "ret_5d": _r(self.ret_5d), "ret_20d": _r(self.ret_20d),
                "ret_60d": _r(self.ret_60d),
                "dd_from_high": _r(self.dd_from_high),
                "atr_pct": _r(self.atr_pct), "vol_20d": _r(self.vol_20d)}


@dataclass
class Decision:
    action: str
    symbol: str
    reason: str
    regime: str
    decide_on: str
    data_as_of: str
    strategy_version: str = STRATEGY_VERSION
    target_pct: float = 0.0
    snap: Optional[TrendSnapshot] = None
    notes: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"action": self.action, "symbol": self.symbol,
                "reason": self.reason, "regime": self.regime,
                "decide_on": self.decide_on, "data_as_of": self.data_as_of,
                "strategy_version": self.strategy_version,
                "target_pct": self.target_pct,
                "snapshot": self.snap.as_dict() if self.snap else None,
                "notes": list(self.notes)}


def sma(values: Sequence[float], n: int) -> Optional[float]:
    if n <= 0 or len(values) < n:
        return None
    return sum(values[-n:]) / float(n)


def _ret(closes: Sequence[float], n: int) -> float:
    if len(closes) <= n or closes[-1 - n] == 0:
        return 0.0
    return closes[-1] / closes[-1 - n] - 1.0


def atr_pct(bars: Sequence[Dict[str, Any]], n: int = 14) -> float:
    if len(bars) < 2:
        return 0.0
    window = bars[-(n + 1):]
    trs = []
    for prev, cur in zip(window, window[1:]):
        hi, lo, pc = float(cur["high"]), float(cur["low"]), float(prev["close"])
        trs.append(max(hi - lo, abs(hi - pc), abs(lo - pc)))
    last_close = float(bars[-1]["close"])
    if not trs or last_close <= 0:
        return 0.0
    return sum(trs) / len(trs) / last_close


def realized_vol(closes: Sequence[float], n: int = 20) -> float:
    if len(closes) < n + 1:
        return 0.0
    rets = [closes[i] / closes[i - 1] - 1.0 for i in range(len(closes) - n, len(closes))]
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / max(1, len(rets) - 1)
    return (var ** 0.5) * (252 ** 0.5)


def snapshot(bars: Sequence[Dict[str, Any]]) -> TrendSnapshot:
    """计算以 ``bars[-1]`` 收盘为准的指标快照。"""
    if len(bars) < MIN_HISTORY:
        raise DailyStrategyError(
            f"历史不足：需要 >= {MIN_HISTORY} 根日线，实际 {len(bars)}")
    closes = [float(b["close"]) for b in bars]
    close = closes[-1]
    s_fast, s_slow, s_long = sma(closes, SMA_FAST), sma(closes, SMA_SLOW), sma(closes, SMA_LONG)

    slope_slow = None
    if len(closes) >= SMA_SLOW + SLOPE_LOOKBACK and s_slow:
        past = sum(closes[-(SMA_SLOW + SLOPE_LOOKBACK):-SLOPE_LOOKBACK]) / SMA_SLOW
        if past > 0:
            slope_slow = s_slow / past - 1.0

    recent_high = max(closes[-DD_WINDOW:]) if len(closes) >= DD_WINDOW else max(closes)
    dd = (close / recent_high - 1.0) if recent_high > 0 else 0.0
    return TrendSnapshot(
        as_of=str(bars[-1]["trade_date"]), close=close,
        sma_fast=s_fast, sma_slow=s_slow, sma_long=s_long, slope_slow=slope_slow,
        ret_1d=_ret(closes, 1), ret_3d=_ret(closes, 3), ret_5d=_ret(closes, 5),
        ret_20d=_ret(closes, 20), ret_60d=_ret(closes, 60),
        dd_from_high=dd, atr_pct=atr_pct(bars), vol_20d=realized_vol(closes))


def classify(snap: TrendSnapshot) -> Tuple[str, str]:
    """快照 → ``(regime, reason)``。纯规则。"""
    if snap.sma_slow is None or snap.sma_long is None:
        return CHOP, "均线不可用（历史不足）"

    if snap.ret_1d <= PANIC_1D:
        return PANIC, f"单日 {snap.ret_1d:+.2%} <= {PANIC_1D:+.1%}"
    if snap.ret_5d <= PANIC_5D:
        return PANIC, f"5 日 {snap.ret_5d:+.2%} <= {PANIC_5D:+.1%}"

    # 用相对容差判定"在均线上方"：完全横盘（close==SMA）视为无趋势，不算上涨
    eps = max(1e-9, snap.sma_slow * 1e-9)
    above_slow = snap.close > snap.sma_slow + eps
    below_slow = not above_slow
    below_long = snap.close < snap.sma_long

    # 反弹：深回撤后短期急拉，但趋势仍未修复 → 不追
    if snap.dd_from_high <= REBOUND_DD and snap.ret_3d >= REBOUND_3D and below_slow:
        return REBOUND, (f"回撤 {snap.dd_from_high:+.2%} 且 3 日 {snap.ret_3d:+.2%}"
                         f"，仍在 SMA50 下方")

    slope_neg = snap.slope_slow is not None and snap.slope_slow < 0
    if below_slow and below_long and slope_neg:
        return DOWNTREND, (f"close<SMA50 且 <SMA200，SMA50 斜率 "
                           f"{snap.slope_slow:+.2%}")

    if above_slow and snap.slope_slow is not None and snap.slope_slow > 0:
        strong = (snap.sma_fast is not None and snap.close > snap.sma_fast
                  and snap.sma_fast > snap.sma_slow
                  and snap.ret_20d > 0
                  and snap.ret_60d >= STRONG_60D
                  and snap.vol_20d <= STRONG_VOL_CAP)
        if strong:
            return UPTREND_STRONG, (f"close>SMA20>SMA50，60 日 {snap.ret_60d:+.2%}，"
                                    f"年化波动 {snap.vol_20d:.1%}")
        return UPTREND_NORMAL, (f"close>SMA50 且 SMA50 斜率 {snap.slope_slow:+.2%}"
                                f"（未达 2x 门槛）")

    return CHOP, "均线关系不明（无趋势）"


def target_for_regime(regime: str, allow_tqqq: bool = False,
                      allow_qld: bool = True) -> Tuple[str, float]:
    """状态 → ``(目标标的, 目标仓位比例)``。空仓返回 ``("", 0.0)``。

    ``allow_qld=False`` 时强势上涨仍用 1x QQQ（用于验证"2x 升级是否值得"）。
    """
    if regime == UPTREND_NORMAL:
        return SYM_QQQ, TARGET_PCT_1X
    if regime == UPTREND_STRONG:
        return (SYM_QLD, TARGET_PCT_2X) if allow_qld else (SYM_QQQ, TARGET_PCT_1X)
    if regime == DOWNTREND:
        return SYM_QID, TARGET_PCT_QID
    if regime == PANIC and allow_tqqq:
        # 极端事件候选：默认关闭，且只给"候选"，需人工确认后才可下单
        return SYM_TQQQ, 0.0
    return "", 0.0


def decide(qqq_bars: Sequence[Dict[str, Any]], decide_on: str,
           holding: Optional[str] = None, allow_tqqq: bool = False,
           allow_qid: bool = True, regime_streak: int = 0,
           sessions_since_exit: Optional[int] = None,
           allow_qld: bool = True) -> Decision:
    """在 ``decide_on`` 收盘后产生决策。

    ``regime_streak``：当前状态已连续维持的交易日数（含当日）。
    ``sessions_since_exit``：该标的距上次平仓的交易日数（未平过为 ``None``）。

    QID 的退出由 :func:`evaluate_exit` 单独判定；本函数只在**空仓**时考虑开仓。
    """
    snap = snapshot(qqq_bars)
    if snap.as_of != decide_on:
        raise DailyStrategyError(
            f"数据未对齐：快照 as_of={snap.as_of}，决策日={decide_on}"
            f"（禁止用旧数据冒充最新收盘）")
    regime, why = classify(snap)

    if regime == PANIC and allow_tqqq:
        return Decision(BUY, SYM_TQQQ, f"{why}（极端事件候选，需人工确认）",
                        regime, decide_on, snap.as_of, target_pct=0.0,
                        snap=snap, notes=["TQQQ_CANDIDATE_ONLY", "REQUIRES_MANUAL_APPROVAL"])

    symbol, pct = target_for_regime(regime, allow_tqqq=False, allow_qld=allow_qld)

    if symbol == SYM_QID and not allow_qid:
        return Decision(CASH, "", f"{why}；但 QID 被禁用 → 空仓",
                        regime, decide_on, snap.as_of, snap=snap)

    if holding and symbol == "":
        return Decision(SELL, holding, f"{why} → 退出到现金",
                        regime, decide_on, snap.as_of, snap=snap)
    if holding and symbol and holding == symbol:
        return Decision(HOLD, holding, f"{why} → 继续持有",
                        regime, decide_on, snap.as_of, snap=snap, target_pct=pct)
    if holding and symbol and holding != symbol:
        return Decision(SELL, holding, f"{why} → 换仓（先卖后买）",
                        regime, decide_on, snap.as_of, snap=snap, notes=[f"next={symbol}"])

    # ---- 以下是"空仓 → 开仓"的闸门 ----
    if not holding and symbol:
        if symbol == SYM_QID and regime_streak < DOWN_CONFIRM_DAYS:
            return Decision(CASH, "",
                            f"{why}；但下跌仅持续 {regime_streak} 日 "
                            f"< {DOWN_CONFIRM_DAYS} → 不评估 QID",
                            regime, decide_on, snap.as_of, snap=snap)
        if regime_streak < MIN_ENTRY_STREAK:
            return Decision(CASH, "",
                            f"{why}；状态仅持续 {regime_streak} 日 "
                            f"< 入场确认 {MIN_ENTRY_STREAK} → 不开仓",
                            regime, decide_on, snap.as_of, snap=snap)
        if sessions_since_exit is not None and sessions_since_exit < REENTRY_COOLDOWN:
            return Decision(CASH, "",
                            f"{why}；距上次平仓仅 {sessions_since_exit} 日 "
                            f"< 冷却 {REENTRY_COOLDOWN} → 不开仓",
                            regime, decide_on, snap.as_of, snap=snap)
        return Decision(BUY, symbol, why, regime, decide_on, snap.as_of,
                        target_pct=pct, snap=snap)
    return Decision(CASH, "", f"{why} → 空仓", regime, decide_on, snap.as_of, snap=snap)


@dataclass
class QidPosition:
    symbol: str
    qty: float
    avg_price: float
    entry_date: str
    sessions_held: int = 0
    taken_profit: bool = False


def evaluate_exit(pos: QidPosition, bars: Sequence[Dict[str, Any]],
                  today: str, rebounded: bool = False) -> Optional[str]:
    """QID 退出判定。返回退出原因字符串，或 ``None`` 表示继续持有。

    四条规则（与既定规则一一对应）：
    1. 相对**实际成交均价**上涨 >= 5% → 止盈；
    2. 趋势失效 / 反弹识别 → 立即退出；
    3. 最迟第二个交易日收盘前强制退出（``sessions_held >= 2``）；
    4. 止盈后当天进入观察态，不再开新仓（由调用方保证，见 ``taken_profit``）。
    """
    if not bars:
        return None
    last = bars[-1]
    if str(last["trade_date"]) != today:
        raise DailyStrategyError(f"退出判定数据未对齐：{last['trade_date']} != {today}")
    close = float(last["close"])
    if pos.avg_price <= 0:
        return None
    gain = close / pos.avg_price - 1.0
    if gain >= QID_TP:
        return f"止盈：收盘 {close:.2f} 相对均价 {pos.avg_price:.2f} {gain:+.2%} >= {QID_TP:+.0%}"
    if rebounded:
        return "趋势失效/反弹识别 → 立即退出"
    if pos.sessions_held >= QID_MAX_SESSIONS:
        return f"强制退出：已持有 {pos.sessions_held} 个交易日（上限 {QID_MAX_SESSIONS}）"
    return None