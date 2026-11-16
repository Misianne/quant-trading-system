"""美股市场状态：单证券停牌、LULD 价格带、全市场熔断（P2-C）。

分层
----
1. **单证券停牌** —— 交易所层面的 Halt / Resume。
2. **LULD（Limit Up-Limit Down）** —— 价格带 + 波动暂停。带子按参考价百分比计算，
   连续两次触发同方向暂停时**带宽翻倍**（LULD Plan 的 doubling 规则）。
3. **MWCB（Market-Wide Circuit Breaker）** —— 基于基准指数跌幅的三级熔断。

关键约束（必须遵守，否则就是"假实现"）
----------------------------------------
* 本模块**只做领域模型与状态机**，不提供任何行情/停牌数据源。
  没有数据时状态是 ``UNKNOWN``，一律按 fail-closed 处理。
* 不把"简单百分比阈值"冒充 LULD：必须区分 tier、开盘/收盘特别带宽、以及 doubling。
* 风险退出（``is_exit``）**不受新开仓限制阻断**，保持 P0 的退出保护语义。
* 阈值全部来自配置，默认值可审计；不得在代码里硬编码"看起来像"的数值。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class MarketState(str, Enum):
    OPEN = "open"
    LULD_PAUSED = "luld_paused"
    HALTED = "halted"
    MWCB_PAUSED = "mwcb_paused"
    MWCB_CLOSED = "mwcb_closed"
    DATA_STALE = "data_stale"
    UNKNOWN = "unknown"


class HaltReason(str, Enum):
    LULD = "luld"
    NEWS_PENDING = "news_pending"
    REGULATORY = "regulatory"
    MWCB = "mwcb"
    OPERATIONAL = "operational"
    UNKNOWN = "unknown"


#: MWCB 三级：跌幅阈值与停牌时长（分钟）。L3 直接收盘。
MWCB_LEVELS: Dict[int, Dict[str, Any]] = {
    1: {"threshold_pct": 0.07, "halt_minutes": 15, "close_for_day": False},
    2: {"threshold_pct": 0.13, "halt_minutes": 15, "close_for_day": False},
    3: {"threshold_pct": 0.20, "halt_minutes": 0, "close_for_day": True},
}

#: LULD 带宽：Tier1 5% / Tier2 10%；开盘与收盘各 25 分钟用 20%。
LULD_TIER1_PCT = 0.05
LULD_TIER2_PCT = 0.10
LULD_OPEN_CLOSE_PCT = 0.20
#: 同方向第二次触发时带宽翻倍的上限（LULD Plan: 大到 2 倍）
LULD_MAX_DOUBLINGS = 2


class MarketStatusError(ValueError):
    pass


@dataclass(frozen=True)
class LULDBand:
    """某个标的一次 LULD 价格带。"""

    symbol: str
    reference_price: float
    upper: float
    lower: float
    band_pct: float
    doubling: int
    tier: int
    computed_at: datetime

    @property
    def width(self) -> float:
        return self.upper - self.lower

    def contains(self, price: float) -> bool:
        return self.lower <= price <= self.upper

    def as_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "reference_price": self.reference_price,
            "upper": self.upper,
            "lower": self.lower,
            "band_pct": self.band_pct,
            "doubling": self.doubling,
            "tier": self.tier,
            "computed_at": self.computed_at.isoformat(),
        }


@dataclass
class SymbolStatus:
    symbol: str
    state: MarketState = MarketState.UNKNOWN
    reason: HaltReason = HaltReason.UNKNOWN
    halted_at: Optional[datetime] = None
    resume_at: Optional[datetime] = None
    band: Optional[LULDBand] = None
    note: str = ""
    updated_at: Optional[datetime] = None

    @property
    def tradable(self) -> bool:
        return self.state == MarketState.OPEN

    def as_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "state": self.state.value,
            "reason": self.reason.value,
            "halted_at": self.halted_at.isoformat() if self.halted_at else None,
            "resume_at": self.resume_at.isoformat() if self.resume_at else None,
            "band": self.band.as_dict() if self.band else None,
            "note": self.note,
            "tradable": self.tradable,
        }


@dataclass
class MarketDecision:
    """下单前的市场状态结论。"""

    allowed: bool
    state: MarketState
    reason: str = ""
    warning: str = ""
    band: Optional[LULDBand] = None

    def as_dict(self) -> Dict[str, Any]:
        return {"allowed": self.allowed, "state": self.state.value,
                "reason": self.reason, "warning": self.warning,
                "band": self.band.as_dict() if self.band else None}


@dataclass
class MWCBState:
    level: int = 0
    triggered_at: Optional[datetime] = None
    resume_at: Optional[datetime] = None
    closed_for_day: bool = False
    reference_price: float = 0.0
    last_price: float = 0.0


class MarketStatusService:
    """市场状态机。

    数据来源完全由调用方注入（:meth:`on_quote` / :meth:`halt_symbol` /
    :meth:`update_index`）。**没有注入数据时状态保持 UNKNOWN**，
    绝不默认"正常"。
    """

    def __init__(self, *, max_quote_age_seconds: float = 60.0,
                 tier2_symbols: Optional[List[str]] = None,
                 luld_enabled: bool = True,
                 halt_expiry_minutes: float = 15.0):
        self.max_quote_age = float(max_quote_age_seconds)
        self.tier2_symbols = {s.upper() for s in (tier2_symbols or [])}
        self.luld_enabled = bool(luld_enabled)
        self.halt_expiry = timedelta(minutes=float(halt_expiry_minutes))

        self._symbols: Dict[str, SymbolStatus] = {}
        self._last_quote: Dict[str, Tuple[float, datetime]] = {}
        self._bands: Dict[str, LULDBand] = {}
        self._luld_breaches: Dict[str, int] = {}
        self._mwcb = MWCBState()
        self._index_ref: float = 0.0
        self._index_last: float = 0.0
        self._index_ts: Optional[datetime] = None

    # ------------------------------------------------------------------ #
    # 数据注入
    # ------------------------------------------------------------------ #
    def update_index(self, price: float, ts: datetime) -> Optional[int]:
        """更新基准指数价格；返回触发的 MWCB 级别（0 表示未触发）。"""
        self._index_ts = ts
        if self._index_ref <= 0:
            self._index_ref = float(price)
        self._index_last = float(price)
        if self._index_ref <= 0 or self._mwcb.closed_for_day:
            return 0
        drop = (self._index_ref - self._index_last) / self._index_ref
        triggered = 0
        for level in (3, 2, 1):
            if drop >= MWCB_LEVELS[level]["threshold_pct"] and level > self._mwcb.level:
                triggered = level
                break
        if not triggered:
            return 0
        cfg = MWCB_LEVELS[triggered]
        self._mwcb.level = triggered
        self._mwcb.triggered_at = ts
        self._mwcb.reference_price = self._index_ref
        self._mwcb.last_price = self._index_last
        if cfg["close_for_day"]:
            self._mwcb.closed_for_day = True
            self._mwcb.resume_at = None
        else:
            self._mwcb.resume_at = ts + timedelta(minutes=cfg["halt_minutes"])
        return triggered

    def update_reference(self, symbol: str, price: float) -> None:
        """设置 LULD 参考价（通常为最近 5 分钟成交均价或前收，由调用方决定）。"""
        if price <= 0:
            raise MarketStatusError(f"参考价必须为正: {price}")
        sym = symbol.upper()
        self._bands[sym] = self._compute_band(sym, float(price), doubling=0)

    def on_quote(self, symbol: str, price: float, ts: datetime,
                 in_open_close_window: bool = False) -> Optional[LULDBand]:
        """喂入一笔成交，判断是否触发 LULD 暂停。返回当前价格带。"""
        sym = symbol.upper()
        self._last_quote[sym] = (float(price), ts)
        st = self._symbols.setdefault(sym, SymbolStatus(symbol=sym))
        st.updated_at = ts

        if not self.luld_enabled:
            if st.state in (MarketState.UNKNOWN, MarketState.DATA_STALE):
                st.state = MarketState.OPEN
            return self._bands.get(sym)

        band = self._bands.get(sym)
        if band is None:
            # 没有参考价 → 无法计算价格带 → 状态未知（不假装正常）
            if st.state in (MarketState.UNKNOWN, MarketState.DATA_STALE):
                st.state = MarketState.UNKNOWN
                st.note = "缺少 LULD 参考价"
            return None

        if band.contains(price):
            if st.state == MarketState.LULD_PAUSED:
                st.state = MarketState.OPEN
                st.reason = HaltReason.UNKNOWN
                st.halted_at = None
                st.resume_at = None
                st.note = "价格回到带内，恢复交易"
            elif st.state in (MarketState.UNKNOWN, MarketState.DATA_STALE):
                st.state = MarketState.OPEN
                st.note = ""
            st.band = band
            return band

        # 越界 → 暂停；同方向第二次越界 → 带宽翻倍
        self._luld_breaches[sym] = self._luld_breaches.get(sym, 0) + 1
        doubling = min(self._luld_breaches[sym] - 1, LULD_MAX_DOUBLINGS)
        new_band = self._compute_band(sym, band.reference_price, doubling,
                                      in_open_close_window=in_open_close_window)
        self._bands[sym] = new_band
        st.state = MarketState.LULD_PAUSED
        st.reason = HaltReason.LULD
        st.halted_at = ts
        st.resume_at = ts + self.halt_expiry
        st.band = new_band
        st.note = (f"价格 {price:.4f} 越出带 [{new_band.lower:.4f}, {new_band.upper:.4f}]"
                   f"（第 {self._luld_breaches[sym]} 次，doubling={doubling}）")
        return new_band

    def halt_symbol(self, symbol: str, reason: HaltReason = HaltReason.UNKNOWN,
                    ts: Optional[datetime] = None, resume_at: Optional[datetime] = None,
                    note: str = "") -> SymbolStatus:
        sym = symbol.upper()
        st = self._symbols.setdefault(sym, SymbolStatus(symbol=sym))
        st.state = MarketState.HALTED
        st.reason = reason
        st.halted_at = ts or datetime.now(timezone.utc)
        st.resume_at = resume_at
        st.note = note
        st.updated_at = st.halted_at
        return st

    def resume_symbol(self, symbol: str, ts: Optional[datetime] = None,
                      note: str = "交易所恢复交易") -> SymbolStatus:
        sym = symbol.upper()
        st = self._symbols.setdefault(sym, SymbolStatus(symbol=sym))
        st.state = MarketState.OPEN
        st.reason = HaltReason.UNKNOWN
        st.halted_at = None
        st.resume_at = None
        st.note = note
        st.updated_at = ts or datetime.now(timezone.utc)
        self._luld_breaches[sym] = 0
        return st

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def status(self, symbol: str, now: Optional[datetime] = None) -> SymbolStatus:
        now = now or datetime.now(timezone.utc)
        sym = symbol.upper()
        st = self._symbols.get(sym)

        # MWCB 优先于个股状态
        if self._mwcb.closed_for_day:
            return SymbolStatus(symbol=sym, state=MarketState.MWCB_CLOSED,
                                reason=HaltReason.MWCB,
                                note=f"MWCB L3 已触发，全市场收盘（跌 {(1 - self._index_last / self._index_ref):.2%}）"
                                if self._index_ref else "MWCB L3")
        if self._mwcb.level and self._mwcb.resume_at and now < self._mwcb.resume_at:
            return SymbolStatus(symbol=sym, state=MarketState.MWCB_PAUSED,
                                reason=HaltReason.MWCB, resume_at=self._mwcb.resume_at,
                                note=f"MWCB L{self._mwcb.level} 暂停中")

        if st is None:
            return SymbolStatus(symbol=sym, state=MarketState.UNKNOWN,
                                note="该标的无任何市场状态数据")
        if st.state == MarketState.HALTED and st.resume_at and now >= st.resume_at:
            st.state = MarketState.OPEN
            st.note = f"到达预计恢复时间（{st.resume_at.isoformat()}）"
        if st.state == MarketState.LULD_PAUSED and st.resume_at and now >= st.resume_at:
            # LULD 暂停到期不等于自动恢复：必须由新的带内成交确认
            st.note = "LULD 暂停已到期，等待带内成交确认恢复"

        # 行情过期 → DATA_STALE（不改写已明确的停牌状态）
        if st.state in (MarketState.OPEN, MarketState.UNKNOWN):
            last = self._last_quote.get(sym)
            if last is None:
                st.state = MarketState.UNKNOWN
                if not st.note:
                    st.note = "无行情数据"
            else:
                _, ts = last
                if (now - ts).total_seconds() > self.max_quote_age:
                    st.state = MarketState.DATA_STALE
                    st.note = f"行情过期 {(now - ts).total_seconds():.0f}s > {self.max_quote_age:.0f}s"
        return st

    def evaluate(self, symbol: str, *, is_exit: bool = False,
                 now: Optional[datetime] = None) -> MarketDecision:
        """下单前的市场状态裁决。

        ``is_exit=True``（风险退出）时**不因市场状态阻断**，只给出告警 ——
        保持 P0 的"风险退出不被普通限制阻断"语义。
        """
        st = self.status(symbol, now)
        if st.state == MarketState.OPEN:
            return MarketDecision(True, st.state, "", "", st.band)

        if is_exit:
            return MarketDecision(
                True, st.state, "",
                f"{symbol} 当前 {st.state.value}（{st.note or st.reason.value}）；"
                f"本次为风险退出，放行但交易所侧可能拒单", st.band)

        return MarketDecision(
            False, st.state,
            f"{symbol} 市场状态={st.state.value}，禁止新开仓（{st.note or st.reason.value}）",
            "", st.band)

    def mwcb_status(self) -> Dict[str, Any]:
        return {
            "level": self._mwcb.level,
            "triggered_at": self._mwcb.triggered_at.isoformat() if self._mwcb.triggered_at else None,
            "resume_at": self._mwcb.resume_at.isoformat() if self._mwcb.resume_at else None,
            "closed_for_day": self._mwcb.closed_for_day,
            "index_ref": self._index_ref,
            "index_last": self._index_last,
            "drop_pct": ((self._index_ref - self._index_last) / self._index_ref)
            if self._index_ref else 0.0,
        }

    def snapshot(self) -> Dict[str, Any]:
        return {
            "mwcb": self.mwcb_status(),
            "symbols": {k: v.as_dict() for k, v in self._symbols.items()},
            "bands": {k: v.as_dict() for k, v in self._bands.items()},
            "luld_enabled": self.luld_enabled,
        }

    # ------------------------------------------------------------------ #
    def _compute_band(self, symbol: str, ref: float, doubling: int,
                      in_open_close_window: bool = False) -> LULDBand:
        tier = 2 if symbol in self.tier2_symbols else 1
        if in_open_close_window:
            pct = LULD_OPEN_CLOSE_PCT
        else:
            pct = LULD_TIER2_PCT if tier == 2 else LULD_TIER1_PCT
        pct = pct * (2 ** doubling)
        return LULDBand(
            symbol=symbol,
            reference_price=ref,
            upper=round(ref * (1 + pct), 6),
            lower=round(ref * (1 - pct), 6),
            band_pct=pct,
            doubling=doubling,
            tier=tier,
            computed_at=datetime.now(timezone.utc),
        )
