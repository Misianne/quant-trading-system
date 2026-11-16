"""美股卖空领域模型：借券、Locate、SSR、空头核算（P2-D）。

安全基线（不可违背）
--------------------
1. **默认禁止卖空。** 原有"卖出必须有持仓（禁止裸卖空）"的规则保留为默认模式；
   卖空必须由配置显式开启，且真实券商还必须同时满足"能力声明 + 人工批准"。
2. **没有 locate 不能卖空。** 拿不到可借券就不许下卖空单，不是"先下单再说"。
3. **SSR 生效时的价格约束必须真实执行**，不是打个标记。
4. 本模块只做领域判定与账务，不提供任何借券数据源；没有数据即判不可用。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from .base import OrderRejected, OrderRequest


class ShortError(OrderRejected):
    def __init__(self, message: str, code: str = "SHORT_REJECTED"):
        super().__init__(message, code=code)


class ShortMode(str, Enum):
    """卖空模式。默认 ``DISABLED``。"""

    DISABLED = "disabled"          # 禁止一切卖空（原安全模式）
    PAPER_ONLY = "paper_only"      # 仅在模拟盘允许
    ENABLED = "enabled"            # 允许（真实盘还须人工批准）


@dataclass
class BorrowInventory:
    """某标的的可借券库存。"""

    symbol: str
    available_qty: float
    fee_rate: float = 0.0          # 年化借券费率
    updated_at: Optional[datetime] = None
    source: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"symbol": self.symbol, "available_qty": self.available_qty,
                "fee_rate": self.fee_rate, "source": self.source}


@dataclass
class Locate:
    """一次借券确认（locate）。有有效期，过期作废。"""

    locate_id: str
    symbol: str
    qty: float
    obtained_at: datetime
    expires_at: datetime

    def valid(self, now: Optional[datetime] = None) -> bool:
        now = now or datetime.now(timezone.utc)
        return now < self.expires_at

    def as_dict(self) -> Dict[str, Any]:
        return {"locate_id": self.locate_id, "symbol": self.symbol, "qty": self.qty,
                "obtained_at": self.obtained_at.isoformat(),
                "expires_at": self.expires_at.isoformat()}


class BorrowBook:
    """借券库存 + 预留。没有库存数据的标的返回 0（= 不可卖空）。"""

    def __init__(self) -> None:
        self._inv: Dict[str, BorrowInventory] = {}
        self._reserved: Dict[str, float] = {}

    def set_inventory(self, symbol: str, qty: float, fee_rate: float = 0.0,
                      source: str = "", now: Optional[datetime] = None) -> BorrowInventory:
        sym = symbol.upper()
        inv = BorrowInventory(symbol=sym, available_qty=float(qty), fee_rate=float(fee_rate),
                              updated_at=now or datetime.now(timezone.utc), source=source)
        self._inv[sym] = inv
        return inv

    def inventory(self, symbol: str) -> Optional[BorrowInventory]:
        return self._inv.get(symbol.upper())

    def available(self, symbol: str) -> float:
        sym = symbol.upper()
        inv = self._inv.get(sym)
        if inv is None:
            return 0.0
        return max(0.0, inv.available_qty - self._reserved.get(sym, 0.0))

    def reserve(self, symbol: str, qty: float) -> bool:
        sym = symbol.upper()
        if qty <= 0:
            return True
        if self.available(sym) + 1e-9 < qty:
            return False
        self._reserved[sym] = self._reserved.get(sym, 0.0) + float(qty)
        return True

    def release(self, symbol: str, qty: float) -> None:
        sym = symbol.upper()
        left = self._reserved.get(sym, 0.0) - float(qty)
        if left <= 1e-9:
            self._reserved.pop(sym, None)
        else:
            self._reserved[sym] = left

    def snapshot(self) -> Dict[str, Any]:
        return {s: {**inv.as_dict(), "reserved": self._reserved.get(s, 0.0),
                    "free": self.available(s)}
                for s, inv in self._inv.items()}


class LocateService:
    """Locate 申请与登记。真实券商必须由适配器实现；模拟盘本地发放。"""

    def __init__(self, default_ttl_minutes: float = 15.0, auto_grant: bool = False,
                 book: Optional[BorrowBook] = None):
        self.ttl = timedelta(minutes=float(default_ttl_minutes))
        self.auto_grant = bool(auto_grant)
        self.book = book or BorrowBook()
        self._locates: Dict[str, Locate] = {}
        self._seq = 0

    def request(self, symbol: str, qty: float, *,
                now: Optional[datetime] = None) -> Locate:
        now = now or datetime.now(timezone.utc)
        sym = symbol.upper()
        if qty <= 0:
            raise ShortError(f"locate 数量非法：{qty}", code="LOCATE_QTY_INVALID")
        if not self.auto_grant:
            raise ShortError(
                f"{sym} 无法取得 locate（当前未接入借券数据源）—— 拒绝卖空",
                code="LOCATE_UNAVAILABLE")
        if not self.book.reserve(sym, qty):
            raise ShortError(
                f"{sym} 可借数量不足：需要 {qty}，可借 {self.book.available(sym)}",
                code="BORROW_INSUFFICIENT")
        self._seq += 1
        loc = Locate(locate_id=f"LOC-{self._seq:06d}", symbol=sym, qty=float(qty),
                     obtained_at=now, expires_at=now + self.ttl)
        self._locates[loc.locate_id] = loc
        return loc

    def get(self, locate_id: str) -> Optional[Locate]:
        return self._locates.get(locate_id)

    def consume(self, locate_id: str, qty: float,
                now: Optional[datetime] = None) -> None:
        loc = self._locates.get(locate_id)
        if loc is None:
            raise ShortError(f"未知 locate：{locate_id}", code="LOCATE_UNKNOWN")
        if not loc.valid(now):
            raise ShortError(f"locate 已过期：{locate_id}", code="LOCATE_EXPIRED")
        if qty > loc.qty + 1e-9:
            raise ShortError(
                f"下单量 {qty} 超过 locate 数量 {loc.qty}", code="LOCATE_QTY_EXCEEDED")
        self.book.release(loc.symbol, qty)


@dataclass
class SSRState:
    """Reg SHO Rule 201 —— 卖空限制（Alternative Uptick Rule）。

    触发条件：某标的当日价格较**前收**下跌 ≥ 10%。
    触发后当日剩余时间 + 次日：卖空只能在**高于当前全国最优买价**时成交
    （本实现用 ``price > prior_close`` 作为可审计的保守判据）。
    """

    symbol: str
    prior_close: float
    active: bool = False
    triggered_at: Optional[datetime] = None
    trigger_price: float = 0.0
    trigger_day: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {"symbol": self.symbol, "prior_close": self.prior_close,
                "active": self.active, "trigger_price": self.trigger_price,
                "trigger_day": self.trigger_day}


class SSRRegistry:
    """按标的维护 SSR 状态。阈值默认 10%（Rule 201）。"""

    def __init__(self, drop_threshold: float = 0.10):
        self.drop_threshold = float(drop_threshold)
        self._states: Dict[str, SSRState] = {}

    def observe(self, symbol: str, price: float, prior_close: float,
                now: Optional[datetime] = None) -> SSRState:
        now = now or datetime.now(timezone.utc)
        sym = symbol.upper()
        st = self._states.get(sym)
        if st is None or abs(st.prior_close - prior_close) > 1e-9:
            st = SSRState(symbol=sym, prior_close=float(prior_close))
            self._states[sym] = st
        if prior_close <= 0:
            return st
        drop = (prior_close - price) / prior_close
        if drop >= self.drop_threshold and not st.active:
            st.active = True
            st.triggered_at = now
            st.trigger_price = float(price)
            st.trigger_day = now.date().isoformat()
        return st

    def is_active(self, symbol: str) -> bool:
        st = self._states.get(symbol.upper())
        return bool(st and st.active)

    def state(self, symbol: str) -> Optional[SSRState]:
        return self._states.get(symbol.upper())

    def reset_day(self, day: Optional[date] = None) -> None:
        """跨日重置（SSR 只影响触发当日剩余时间，简化实现：次日重置）。"""
        day = day or datetime.now(timezone.utc).date()
        for st in self._states.values():
            if st.trigger_day and st.trigger_day != day.isoformat():
                st.active = False
                st.trigger_day = None


@dataclass
class ShortDecision:
    allowed: bool
    is_short: bool
    short_qty: float
    reason: str = ""
    warning: str = ""
    locate: Optional[Locate] = None
    ssr_active: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {"allowed": self.allowed, "is_short": self.is_short,
                "short_qty": self.short_qty, "reason": self.reason,
                "warning": self.warning, "ssr_active": self.ssr_active,
                "locate": self.locate.as_dict() if self.locate else None}


class ShortGuard:
    """卖空下单前的统一裁决。

    典型调用：``guard.check(req, held_qty=..., price=..., caps=...)``
    """

    def __init__(self, mode: ShortMode = ShortMode.DISABLED,
                 borrow: Optional[BorrowBook] = None,
                 locates: Optional[LocateService] = None,
                 ssr: Optional[SSRRegistry] = None,
                 require_operator_approval_live: bool = True,
                 operator_approved: bool = False):
        self.mode = mode
        self.borrow = borrow or BorrowBook()
        self.locates = locates or LocateService(book=self.borrow)
        self.ssr = ssr or SSRRegistry()
        self.require_operator_approval_live = bool(require_operator_approval_live)
        self.operator_approved = bool(operator_approved)

    # ------------------------------------------------------------------ #
    def check(self, req: OrderRequest, *, held_qty: float, caps,
              price: Optional[float] = None, prior_close: Optional[float] = None,
              is_live: bool = False, now: Optional[datetime] = None) -> ShortDecision:
        now = now or datetime.now(timezone.utc)
        side = str(req.side).lower()
        qty = float(req.qty or 0.0)
        sym = req.symbol.upper()

        # 买单不涉及卖空
        if side == "buy":
            return ShortDecision(True, False, 0.0, "")

        held = max(0.0, float(held_qty))
        if qty <= held + 1e-9:
            return ShortDecision(True, False, 0.0, "")

        short_qty = qty - held

        if self.mode == ShortMode.DISABLED:
            return ShortDecision(
                False, True, short_qty,
                f"{sym} 卖出 {qty} 超过持仓 {held}，需要卖空 {short_qty}；"
                f"卖空模式=disabled（默认安全模式：禁止裸卖空）",
            )
        if is_live and self.mode != ShortMode.ENABLED:
            return ShortDecision(
                False, True, short_qty,
                f"{sym} 真实盘卖空未开启（mode={self.mode.value}）")
        if is_live and self.require_operator_approval_live and not self.operator_approved:
            return ShortDecision(
                False, True, short_qty,
                f"{sym} 真实盘卖空需要人工批准，当前未批准")

        if not getattr(caps, "supports_short", False):
            return ShortDecision(
                False, True, short_qty,
                f"{caps.venue} 未声明支持卖空（Broker 能力不足），拒绝")

        # 借券可用性
        if not getattr(caps, "supports_locate", False):
            return ShortDecision(
                False, True, short_qty,
                f"{caps.venue} 不支持借券查询（locate），按规则不得开放卖空")

        avail = self.borrow.available(sym)
        if avail + 1e-9 < short_qty:
            return ShortDecision(
                False, True, short_qty,
                f"{sym} 可借数量不足：需要 {short_qty}，可借 {avail}")

        # SSR 价格约束
        ssr_active = self.ssr.is_active(sym)
        if ssr_active:
            ref = prior_close if (prior_close and prior_close > 0) else (
                self.ssr.state(sym).prior_close if self.ssr.state(sym) else 0.0)
            if ref > 0 and price is not None and price <= ref:
                return ShortDecision(
                    False, True, short_qty,
                    f"{sym} SSR 生效中：卖空价格 {price} 必须高于参考价 {ref}",
                    ssr_active=True)

        # 取 locate
        try:
            loc = self.locates.request(sym, short_qty, now=now)
        except ShortError as exc:
            return ShortDecision(False, True, short_qty, str(exc), ssr_active=ssr_active)

        return ShortDecision(True, True, short_qty,
                             f"{sym} 允许卖空 {short_qty}（locate={loc.locate_id}）",
                             warning="SSR 生效中" if ssr_active else "",
                             locate=loc, ssr_active=ssr_active)


# ---------------------------------------------------------------------- #
# 空头持仓核算
# ---------------------------------------------------------------------- #
@dataclass
class ShortPosition:
    """空头仓位（qty 为负数表示做空）。"""

    symbol: str
    qty: float                    # < 0
    avg_price: float              # 开仓均价
    opened_at: Optional[datetime] = None
    borrow_fee: float = 0.0

    @property
    def is_short(self) -> bool:
        return self.qty < 0

    def cover(self, qty: float, price: float) -> float:
        """买回平空，返回已实现盈亏。"""
        qty = min(abs(self.qty), float(qty))
        pnl = (self.avg_price - float(price)) * qty
        self.qty += qty
        if abs(self.qty) < 1e-9:
            self.qty = 0.0
        return pnl

    def add(self, qty: float, price: float) -> None:
        """加空：按加权平均更新开仓价。"""
        total = abs(self.qty) + float(qty)
        if total <= 0:
            return
        self.avg_price = (self.avg_price * abs(self.qty) + price * qty) / total
        self.qty -= float(qty)

    def as_dict(self) -> Dict[str, Any]:
        return {"symbol": self.symbol, "qty": self.qty,
                "avg_price": self.avg_price, "is_short": self.is_short,
                "borrow_fee": self.borrow_fee}
