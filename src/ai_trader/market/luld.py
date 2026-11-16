"""LULD 完整生命周期（参考价 → 价格带 → Limit State → 暂停 → 重新开盘竞价 → 恢复）

为什么单独成模块
----------------
上一版把「暂停 5 分钟到期」直接当成「恢复交易」，这在真实规则下是**错的**：
5 分钟只是**最短**暂停时间，之后必须走**重新开盘竞价**，并且必须收到
**权威的交易所恢复状态**才能恢复连续交易。本模块把这条链路显式建模。

官方依据
--------
* LULD Plan（含 Amendment 12 / Amendment 18）
* Nasdaq Rule 4120(c)(10)（Initial / Extended Display Only Period、Auction Collar）
* NYSE Amendment 12 说明（Auction Reference Price = 触发暂停时的价格带）
* SR-NASDAQ-2019-057 / SR-NASDAQ-2026-064（19b-4 文本）

三家共同点与差异（详见 ``NasdaqAuctionPolicy`` / ``NyseAuctionPolicy``）
* 共同：5 分钟一段；每次延长把**失衡方向**的边界再放宽 5%（≤$3 为 $0.15）；对侧不动；
  收市前 10 分钟内不恢复连续交易，转为收盘竞价。
* 差异：Nasdaq 的 Auction Reference Price 取「触发暂停时的价格带那一侧」（Limit Down → Lower Band），
  先有 5 分钟 Display Only；NYSE 的 auction reference price 同样是"最后处于 limit state 的价格带"，
  且收盘竞价沿用最后一次发布的 collar。两者字段与流程**不完全相同**，故拆成两个 Policy。

未核实项统一标 ``UNVERIFIED``，不允许用猜测填参数。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


# ====================================================================== #
# 枚举
# ====================================================================== #
class LuldPhase(str, Enum):
    """完整生命周期。**只有 ``OPEN`` / ``REOPENED`` 允许连续交易。**"""

    OPEN = "open"
    REFERENCE_PRICE = "reference_price"
    PRICE_BAND = "price_band"
    LIMIT_STATE = "limit_state"
    TRADING_PAUSE = "trading_pause"
    REOPENING_AUCTION = "reopening_auction"
    AUCTION_EXTENDED = "auction_extended"
    AUCTION_IMBALANCE = "auction_imbalance"
    RESUME_PENDING = "resume_pending"
    REOPENED = "reopened"
    AUCTION_CANCELLED = "auction_cancelled"
    MARKET_CLOSED = "market_closed"
    DATA_UNAVAILABLE = "data_unavailable"
    INVALID_EVENT = "invalid_event"


TRADABLE_PHASES = {LuldPhase.OPEN, LuldPhase.LIMIT_STATE, LuldPhase.REOPENED}

#: 交易所枚举（决定竞价策略）
class Exchange(str, Enum):
    NASDAQ = "nasdaq"
    NYSE = "nyse"
    UNKNOWN = "unknown"


class ImbalanceSide(str, Enum):
    BUY = "buy"
    SELL = "sell"
    NONE = "none"


class AuctionStatus(str, Enum):
    NOT_STARTED = "not_started"
    WAITING_FOR_AUCTION = "waiting_for_auction"
    INITIAL_DISPLAY_ONLY = "initial_display_only"
    AUCTION_IN_PROGRESS = "auction_in_progress"
    AUCTION_EXTENDED = "auction_extended"
    IMBALANCE = "imbalance"
    RESUME_PENDING = "resume_pending"
    REOPENED = "reopened"
    AUCTION_CANCELLED = "auction_cancelled"
    DATA_UNAVAILABLE = "data_unavailable"


# ====================================================================== #
# 官方参数
# ====================================================================== #
MIN_PAUSE = timedelta(minutes=5)           # 最短暂停时间
EXTENSION = timedelta(minutes=5)           # 每次延长
LIMIT_STATE_WINDOW = timedelta(seconds=15)
COLLAR_PCT = 0.05                          # 每次放宽 5%
COLLAR_LOW_ABS = 0.15                      # ≤ $3 用 $0.15
COLLAR_LOW_PRICE = 3.0
#: 收市前 10 分钟内进入/仍在暂停 → 不恢复连续交易，转收盘竞价（Nasdaq 3:50 / NYSE 3:50）
CLOSE_CUTOFF_MINUTES = 10.0


class LuldError(ValueError):
    pass


def normalize_symbol(symbol: str) -> str:
    """统一符号：剥掉**小写市场前缀**（``usAAPL`` → ``AAPL``）并大写。

    只在"前缀确实是小写市场码、其余为大写"时剥离 —— 否则 ``USB``、``CNC`` 这类
    真实代码会被误伤。
    """
    raw = str(symbol or "").strip()
    if not raw:
        return ""
    if len(raw) > 2 and raw[:2].lower() in ("us", "hk", "cn") \
            and raw[:2].islower() and not any(c.islower() for c in raw[2:]):
        return raw[2:].upper()
    return raw.upper()


# ====================================================================== #
# 状态对象
# ====================================================================== #
@dataclass
class ReferencePriceState:
    symbol: str
    tier: int = 1
    previous_close: float = 0.0
    reference_price: Optional[float] = None
    reference_window_start: Optional[datetime] = None
    reference_window_end: Optional[datetime] = None
    last_eligible_trade_at: Optional[datetime] = None
    effective_at: Optional[datetime] = None
    source: str = ""
    version: int = 0
    #: "ok" | "no_eligible_trades" | "no_previous_close" | "out_of_session"
    quality: str = "no_eligible_trades"

    @property
    def usable(self) -> bool:
        return self.reference_price is not None and self.quality == "ok"

    def as_dict(self) -> Dict[str, Any]:
        return {"symbol": self.symbol, "tier": self.tier,
                "previous_close": self.previous_close,
                "reference_price": self.reference_price,
                "window": [self.reference_window_start.isoformat()
                           if self.reference_window_start else None,
                           self.reference_window_end.isoformat()
                           if self.reference_window_end else None],
                "effective_at": self.effective_at.isoformat() if self.effective_at else None,
                "source": self.source, "version": self.version, "quality": self.quality}


@dataclass
class PriceBandState:
    symbol: str
    reference_price: float
    upper_band: float
    lower_band: float
    tier: int
    price_bucket: str
    band_width: float
    effective_at: datetime
    expires_at: Optional[datetime]
    session: str
    source: str
    version: int
    doubled: bool = False

    def contains(self, price: float) -> bool:
        return self.lower_band <= price <= self.upper_band

    def side_hit(self, price: float) -> Optional[str]:
        """返回 ``"upper"`` / ``"lower"`` / None。"""
        if price >= self.upper_band:
            return "upper"
        if price <= self.lower_band:
            return "lower"
        return None

    def as_dict(self) -> Dict[str, Any]:
        return {"symbol": self.symbol, "reference_price": self.reference_price,
                "upper_band": self.upper_band, "lower_band": self.lower_band,
                "tier": self.tier, "price_bucket": self.price_bucket,
                "band_width": self.band_width, "doubled": self.doubled,
                "effective_at": self.effective_at.isoformat(),
                "expires_at": self.expires_at.isoformat() if self.expires_at else None,
                "session": self.session, "source": self.source, "version": self.version}


@dataclass
class LimitStateRecord:
    entered_at: datetime
    direction: str                       # upper | lower
    band: PriceBandState
    observed_until: Optional[datetime] = None
    returned_inside: bool = False
    triggered_pause: bool = False
    version: int = 0
    source: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"entered_at": self.entered_at.isoformat(), "direction": self.direction,
                "returned_inside": self.returned_inside,
                "triggered_pause": self.triggered_pause, "version": self.version}


@dataclass
class TradingPause:
    pause_started_at: datetime
    minimum_pause_until: datetime
    pause_reason: str
    reference_price: float
    band: PriceBandState
    source: str = ""
    version: int = 0
    auction_status: AuctionStatus = AuctionStatus.WAITING_FOR_AUCTION
    #: 与恢复事件绑定的唯一 ID（跨证券/跨交易所/跨交易日不可复用）
    pause_event_id: str = ""
    trading_date: str = ""

    @property
    def minimum_elapsed(self) -> bool:
        return False    # 需传入 now，见 LuldLifecycle

    def elapsed(self, now: datetime) -> bool:
        return now >= self.minimum_pause_until

    def as_dict(self) -> Dict[str, Any]:
        return {"pause_started_at": self.pause_started_at.isoformat(),
                "minimum_pause_until": self.minimum_pause_until.isoformat(),
                "pause_reason": self.pause_reason,
                "reference_price": self.reference_price,
                "auction_status": self.auction_status.value,
                "version": self.version}


@dataclass
class AuctionState:
    symbol: str
    exchange: Exchange = Exchange.UNKNOWN
    auction_id: str = ""
    auction_started_at: Optional[datetime] = None
    indicative_price: Optional[float] = None
    collar_lower: Optional[float] = None
    collar_upper: Optional[float] = None
    paired_quantity: float = 0.0
    imbalance_quantity: float = 0.0
    imbalance_side: ImbalanceSide = ImbalanceSide.NONE
    extension_count: int = 0
    last_update_at: Optional[datetime] = None
    official_resume_at: Optional[datetime] = None
    source: str = ""
    version: int = 0
    status: AuctionStatus = AuctionStatus.NOT_STARTED
    #: 竞价参考价（Nasdaq: 触发暂停那一侧的价格带；NYSE: 同义）
    auction_reference_price: float = 0.0
    trading_date: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"symbol": self.symbol, "exchange": self.exchange.value,
                "auction_id": self.auction_id,
                "status": self.status.value,
                "indicative_price": self.indicative_price,
                "collars": [self.collar_lower, self.collar_upper],
                "imbalance": {"qty": self.imbalance_quantity,
                              "side": self.imbalance_side.value},
                "paired_quantity": self.paired_quantity,
                "extension_count": self.extension_count,
                "official_resume_at": self.official_resume_at.isoformat()
                if self.official_resume_at else None,
                "version": self.version, "source": self.source}


# ====================================================================== #
# 竞价策略（Nasdaq / NYSE 分开）
# ====================================================================== #
class AuctionPolicy:
    """竞价边界计算。子类必须实现 ``build_collars`` 与 ``widen``。"""

    exchange = Exchange.UNKNOWN
    #: 该交易所字段是否已核实
    verified = False
    notes = ""

    def auction_reference_price(self, band: "PriceBandState", direction: str) -> float:
        """触发暂停那一侧的价格带价（Limit Down → 下轨；Limit Up → 上轨）。"""
        return band.lower_band if direction == "lower" else band.upper_band

    def collar_step(self, arp: float) -> float:
        """每次放宽的金额：5%，或 ARP ≤ $3 时的 $0.15。"""
        if arp <= COLLAR_LOW_PRICE:
            return COLLAR_LOW_ABS
        return arp * COLLAR_PCT

    def build_collars(self, band: "PriceBandState", direction: str
                      ) -> Tuple[float, float, float]:
        """返回 ``(arp, lower_collar, upper_collar)``。"""
        raise NotImplementedError

    def widen(self, arp: float, lower: float, upper: float,
              side: ImbalanceSide) -> Tuple[float, float]:
        raise NotImplementedError


class NasdaqAuctionPolicy(AuctionPolicy):
    """Nasdaq Rule 4120(c)(10)。

    * Limit Down：ARP = Lower Band；下边界 = ARP − 5%（≤$3 为 $0.15）；上边界 = **Upper Band**
    * Limit Up  ：ARP = Upper Band；上边界 = ARP + 5%（≤$3 为 $0.15）；下边界 = **Lower Band**
    * 每次延长：只放宽**失衡方向**那一侧，步长 = 初始 ARP 的 5%（或 $0.15）
    """

    exchange = Exchange.NASDAQ
    verified = True
    notes = "SR-NASDAQ-2019-057 / Rule 4120(c)(10)"

    def build_collars(self, band, direction):
        arp = self.auction_reference_price(band, direction)
        step = self.collar_step(arp)
        if direction == "lower":
            return arp, arp - step, band.upper_band
        return arp, band.lower_band, arp + step

    def widen(self, arp, lower, upper, side):
        step = self.collar_step(arp)
        if side == ImbalanceSide.SELL:
            return lower - step, upper
        if side == ImbalanceSide.BUY:
            return lower, upper + step
        return lower, upper


class NyseAuctionPolicy(AuctionPolicy):
    """NYSE Amendment 12。

    与 Nasdaq 的**差异**（已核实）：
    * NYSE 的 auction reference price 同样是"触发暂停时的价格带"，但 collar 的下/上界
      与 Nasdaq 同构（5% 或 $0.15，另一侧取对侧价格带）。
    * 差异主要在**收盘竞价沿用最后一次 collar**，以及 3:50 后直接并入收盘竞价。
    * 若既未恢复、又无 Regulatory Halt，其他交易所也不会先恢复 —— 本实现以
      ``official_resume`` 作为唯一恢复入口来体现这一点。
    """

    exchange = Exchange.NYSE
    verified = True
    notes = "NYSE Amendment 12 / LULD_12 说明"

    def build_collars(self, band, direction):
        arp = self.auction_reference_price(band, direction)
        step = self.collar_step(arp)
        if direction == "lower":
            return arp, arp - step, band.upper_band
        return arp, band.lower_band, arp + step

    def widen(self, arp, lower, upper, side):
        step = self.collar_step(arp)
        if side == ImbalanceSide.SELL:
            return lower - step, upper
        if side == ImbalanceSide.BUY:
            return lower, upper + step
        return lower, upper


class UnknownExchangePolicy(AuctionPolicy):
    """未核实交易所：不产生任何竞价结论（禁止猜测）。"""

    exchange = Exchange.UNKNOWN
    verified = False
    notes = "UNVERIFIED：交易所未识别，不推断竞价参数"

    def build_collars(self, band, direction):
        raise LuldError("UNVERIFIED：未知交易所无法计算竞价边界")

    def widen(self, arp, lower, upper, side):
        raise LuldError("UNVERIFIED：未知交易所无法放宽竞价边界")


POLICIES = {
    Exchange.NASDAQ: NasdaqAuctionPolicy(),
    Exchange.NYSE: NyseAuctionPolicy(),
}


def policy_for(exchange: Exchange) -> AuctionPolicy:
    return POLICIES.get(exchange, UnknownExchangePolicy())


# ====================================================================== #
# 生命周期
# ====================================================================== #
@dataclass
class LuldEvent:
    ts: datetime
    kind: str
    phase: LuldPhase
    version: int
    note: str = ""
    source: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"ts": self.ts.isoformat(), "kind": self.kind,
                "phase": self.phase.value, "version": self.version,
                "note": self.note, "source": self.source}


class LuldLifecycle:
    """单个标的的 LULD 生命周期。

    状态迁移只由：成交、竞价更新、官方恢复 三类输入驱动；**没有任何"计时器到期 → OPEN"的路径**。
    """

    def __init__(self, symbol: str, *, exchange: Exchange = Exchange.UNKNOWN,
                 tier: int = 1):
        self.symbol = normalize_symbol(symbol)
        self.exchange = exchange
        self.tier = tier
        self.policy = policy_for(exchange)
        self.phase = LuldPhase.OPEN
        self.version = 0
        self.events: List[LuldEvent] = []

        self.reference = ReferencePriceState(symbol=self.symbol, tier=tier)
        self.band: Optional[PriceBandState] = None
        self.limit: Optional[LimitStateRecord] = None
        self.pause: Optional[TradingPause] = None
        self.auction = AuctionState(symbol=self.symbol, exchange=exchange)

    # ------------------------------------------------------------------ #
    def _bump(self, kind: str, phase: LuldPhase, ts: datetime, *,
              note: str = "", source: str = "") -> None:
        self.version += 1
        self.phase = phase
        self.events.append(LuldEvent(ts=ts, kind=kind, phase=phase,
                                     version=self.version, note=note, source=source))

    @property
    def tradable(self) -> bool:
        return self.phase in TRADABLE_PHASES

    # ------------------------------------------------------------------ #
    # 参考价 + 价格带
    # ------------------------------------------------------------------ #
    def set_reference(self, price: float, ts: datetime, *, source: str = "",
                      previous_close: Optional[float] = None) -> None:
        if price <= 0:
            raise LuldError("参考价必须为正")
        self.reference.reference_price = float(price)
        self.reference.effective_at = ts
        self.reference.source = source
        self.reference.quality = "ok"
        self.reference.version += 1
        if previous_close is not None:
            self.reference.previous_close = float(previous_close)
        self._bump("reference_price", LuldPhase.REFERENCE_PRICE, ts, source=source)

    def set_band(self, band: PriceBandState) -> None:
        self.band = band
        self._bump("price_band", LuldPhase.PRICE_BAND, band.effective_at,
                   note=f"[{band.lower_band:.4f},{band.upper_band:.4f}]")

    # ------------------------------------------------------------------ #
    # 成交
    # ------------------------------------------------------------------ #
    def on_trade(self, price: float, ts: datetime, *, in_regular: bool,
                 minutes_to_close: Optional[float] = None) -> LuldPhase:
        """喂入一笔成交，推进生命周期。返回当前 phase。"""
        if not in_regular:
            self._bump("market_closed", LuldPhase.MARKET_CLOSED, ts,
                       note="非常规时段：LULD 不适用")
            return self.phase
        if self.band is None:
            self._bump("data_unavailable", LuldPhase.DATA_UNAVAILABLE, ts,
                       note="无价格带")
            return self.phase

        # 暂停/竞价阶段：连续成交不参与状态推进
        if self.phase in (LuldPhase.TRADING_PAUSE, LuldPhase.REOPENING_AUCTION,
                          LuldPhase.AUCTION_EXTENDED, LuldPhase.AUCTION_IMBALANCE,
                          LuldPhase.RESUME_PENDING):
            return self.phase

        if self.band.contains(price):
            if self.phase is LuldPhase.LIMIT_STATE and self.limit is not None:
                self.limit.returned_inside = True
                self.limit.observed_until = ts
                self._bump("limit_state_cleared", LuldPhase.OPEN, ts,
                           note="15 秒内回到带内")
            return self.phase

        side = "upper" if price > self.band.upper_band else "lower"

        if self.limit is None:
            self.limit = LimitStateRecord(entered_at=ts, direction=side,
                                          band=self.band, version=self.version + 1)
            self._bump("limit_state_enter", LuldPhase.LIMIT_STATE, ts,
                       note=f"触及 {side} 带")
            return self.phase

        if self.limit.direction != side:
            # 方向翻转：重新计时（更保守）
            self.limit = LimitStateRecord(entered_at=ts, direction=side,
                                          band=self.band, version=self.version + 1)
            self._bump("limit_state_flip", LuldPhase.LIMIT_STATE, ts,
                       note=f"方向翻转到 {side}，重新计时")
            return self.phase

        elapsed = (ts - self.limit.entered_at).total_seconds()
        if elapsed < LIMIT_STATE_WINDOW.total_seconds():
            return self.phase

        # ≥15 秒 → 交易暂停（最短 5 分钟）
        if minutes_to_close is not None and minutes_to_close <= CLOSE_CUTOFF_MINUTES:
            # 收市前 10 分钟内不恢复连续交易 → 转收盘竞价
            self.pause = TradingPause(pause_started_at=ts,
                                      minimum_pause_until=ts + MIN_PAUSE,
                                      pause_reason=f"luld_{side}",
                                      reference_price=self.limit.band.reference_price,
                                      band=self.limit.band,
                                      version=self.version + 1,
                                      auction_status=AuctionStatus.AUCTION_CANCELLED,
                                      pause_event_id=f"{self.exchange.value}-{self.symbol}-"
                                                     f"{ts.isoformat()}-close-cutoff",
                                      trading_date=ts.date().isoformat())
            self._bump("trading_pause_close_cutoff", LuldPhase.TRADING_PAUSE, ts,
                       note="收市前 10 分钟内进入暂停：不恢复连续交易，转收盘竞价")
            return self.phase

        self.limit.triggered_pause = True
        self.pause = TradingPause(pause_started_at=ts,
                                  minimum_pause_until=ts + MIN_PAUSE,
                                  pause_reason=f"luld_{side}",
                                  reference_price=self.limit.band.reference_price,
                                  band=self.limit.band,
                                  version=self.version + 1,
                                  pause_event_id=f"{self.exchange.value}-{self.symbol}-"
                                                 f"{ts.isoformat()}-luld",
                                  trading_date=ts.date().isoformat())
        self.auction.trading_date = ts.date().isoformat()
        self.auction.status = AuctionStatus.WAITING_FOR_AUCTION
        self.auction.auction_started_at = ts
        self._bump("trading_pause_start", LuldPhase.TRADING_PAUSE, ts,
                   note=f"Limit State 超过 15 秒 → 暂停（最短 5 分钟）")
        return self.phase

    # ------------------------------------------------------------------ #
    # 竞价
    # ------------------------------------------------------------------ #
    def start_auction(self, ts: datetime, *, source: str = "") -> LuldPhase:
        """进入重新开盘竞价（5 分钟 Display Only Period）。"""
        if self.phase is not LuldPhase.TRADING_PAUSE or self.pause is None:
            self._bump("invalid_event", LuldPhase.INVALID_EVENT, ts,
                       note="非暂停状态无法启动竞价")
            return self.phase
        if not self.pause.elapsed(ts):
            # 反例 1：未满 5 分钟就收到竞价 → 记录但不启动
            self.events.append(LuldEvent(ts=ts, kind="early_auction_ignored",
                                         phase=self.phase, version=self.version,
                                         note="最短暂停期未满，竞价请求被忽略"))
            return self.phase
        direction = "lower" if "lower" in (self.pause.pause_reason or "") else "upper"
        try:
            arp, lo, hi = self.policy.build_collars(self.pause.band, direction)
        except LuldError as exc:
            self._bump("data_unavailable", LuldPhase.DATA_UNAVAILABLE, ts, note=str(exc))
            return self.phase
        self.auction = AuctionState(
            symbol=self.symbol, exchange=self.exchange,
            auction_id=f"{self.exchange.value}-{self.symbol}-{self.version+1}",
            auction_started_at=ts, collar_lower=lo, collar_upper=hi,
            auction_reference_price=arp, source=source, version=0,
            status=AuctionStatus.INITIAL_DISPLAY_ONLY, last_update_at=ts)
        self.pause.auction_status = AuctionStatus.INITIAL_DISPLAY_ONLY
        self._bump("auction_start", LuldPhase.REOPENING_AUCTION, ts, source=source,
                   note=f"ARP={arp:.4f} collar=[{lo:.4f},{hi:.4f}]")
        return self.phase

    def on_auction_update(self, ts: datetime, *, version: int,
                          indicative_price: Optional[float] = None,
                          paired_qty: float = 0.0,
                          imbalance_qty: float = 0.0,
                          imbalance_side: ImbalanceSide = ImbalanceSide.NONE,
                          source: str = "") -> LuldPhase:
        """竞价更新（指示价 / 未配对量 / 失衡方向）。"""
        if self.phase not in (LuldPhase.REOPENING_AUCTION, LuldPhase.AUCTION_EXTENDED,
                              LuldPhase.AUCTION_IMBALANCE):
            self._bump("invalid_event", LuldPhase.INVALID_EVENT, ts,
                       note=f"当前 {self.phase.value} 不接受竞价更新")
            return self.phase
        if version <= self.auction.version:
            # 反例 6/7：旧版本或重复事件不得回退状态
            self.events.append(LuldEvent(ts=ts, kind="stale_auction_update",
                                         phase=self.phase, version=self.version,
                                         note=f"version {version} ≤ {self.auction.version}，忽略"))
            return self.phase

        self.auction.version = version
        self.auction.last_update_at = ts
        self.auction.indicative_price = indicative_price
        self.auction.paired_quantity = float(paired_qty)
        self.auction.imbalance_quantity = float(imbalance_qty)
        self.auction.imbalance_side = imbalance_side
        self.auction.source = source

        # 指示价越界或存在残余失衡 → 延长
        out_of_collar = (indicative_price is not None and
                         (indicative_price < self.auction.collar_lower or
                          indicative_price > self.auction.collar_upper))
        if imbalance_side is not ImbalanceSide.NONE or out_of_collar:
            if out_of_collar:
                side = (ImbalanceSide.SELL if indicative_price < self.auction.collar_lower
                        else ImbalanceSide.BUY)
                self.auction.imbalance_side = side
            lo, hi = self.policy.widen(self.auction.auction_reference_price,
                                       self.auction.collar_lower,
                                       self.auction.collar_upper,
                                       self.auction.imbalance_side)
            self.auction.collar_lower, self.auction.collar_upper = lo, hi
            self.auction.extension_count += 1
            self.auction.status = AuctionStatus.AUCTION_EXTENDED
            self.pause.auction_status = AuctionStatus.AUCTION_EXTENDED
            self._bump("auction_extended", LuldPhase.AUCTION_EXTENDED, ts, source=source,
                       note=(f"第 {self.auction.extension_count} 次延长；"
                             f"collar=[{lo:.4f},{hi:.4f}]"))
            return self.phase

        # 无失衡 → 等待官方恢复（**绝不自行 REOPENED**）
        self.auction.status = AuctionStatus.RESUME_PENDING
        self.pause.auction_status = AuctionStatus.RESUME_PENDING
        self._bump("auction_balanced", LuldPhase.RESUME_PENDING, ts, source=source,
                   note="竞价已平衡，等待交易所官方恢复状态")
        return self.phase

    def cancel_auction(self, ts: datetime, *, source: str = "") -> LuldPhase:
        self.auction.status = AuctionStatus.AUCTION_CANCELLED
        if self.pause:
            self.pause.auction_status = AuctionStatus.AUCTION_CANCELLED
        self._bump("auction_cancelled", LuldPhase.AUCTION_CANCELLED, ts, source=source,
                   note="竞价取消：保持不可交易")
        return self.phase

    # ------------------------------------------------------------------ #
    # 官方恢复
    # ------------------------------------------------------------------ #
    def on_official_resume(self, event: Any, authority: Any, *,
                           now: Optional[datetime] = None) -> LuldPhase:
        """**唯一**的恢复入口：必须先通过 :class:`ResumeAuthority` 校验。

        校验失败 → 保持当前（不可交易）状态，并记录拒绝原因（可审计）。
        """
        from .luld_authority import ResumeRejected

        ts = event.event_ts
        if self.phase is not LuldPhase.RESUME_PENDING:
            # 非待恢复状态收到恢复事件：**只记录、不改状态**（避免杂散事件污染状态机）
            self.events.append(LuldEvent(
                ts=ts, kind="resume_ignored", phase=self.phase,
                version=self.version,
                note=f"{self.phase.value} 不接受恢复事件",
                source=getattr(event, "provider", "")))
            return self.phase

        pause = self.pause
        auction_id = self.auction.auction_id
        try:
            authority.verify(
                event,
                symbol=self.symbol,
                exchange=self.exchange.value,
                trading_date=getattr(pause, "trading_date", "") if pause else "",
                pause_event_id=getattr(pause, "pause_event_id", "") if pause else "",
                auction_id=auction_id,
                now=now,
            )
        except ResumeRejected as exc:
            self.events.append(LuldEvent(
                ts=ts, kind="resume_rejected", phase=self.phase,
                version=self.version,
                note=f"[{exc.code}] {exc.message}",
                source=getattr(event, "provider", "")))
            return self.phase

        # 通过校验 → 应用恢复
        self.auction.status = AuctionStatus.REOPENED
        self.auction.official_resume_at = ts
        self.pause = None
        self.limit = None
        self._bump("official_resume", LuldPhase.REOPENED, ts,
                   source=getattr(event, "provider", ""),
                   note="权威恢复事件通过校验，恢复连续交易")
        return self.phase

    def data_unavailable(self, ts: datetime, note: str) -> LuldPhase:
        self._bump("data_unavailable", LuldPhase.DATA_UNAVAILABLE, ts, note=note)
        return self.phase

    def snapshot(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol, "exchange": self.exchange.value,
            "tier": self.tier, "phase": self.phase.value,
            "tradable": self.tradable, "version": self.version,
            "reference": self.reference.as_dict(),
            "band": self.band.as_dict() if self.band else None,
            "limit": self.limit.as_dict() if self.limit else None,
            "pause": self.pause.as_dict() if self.pause else None,
            "auction": self.auction.as_dict(),
            "events": [e.as_dict() for e in self.events[-8:]],
        }
