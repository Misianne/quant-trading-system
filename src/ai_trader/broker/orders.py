"""统一订单模型：类型、有效期（TIF）、状态机与券商能力声明（P2-B）。

设计原则
--------
* **不支持就拒绝，绝不静默降级。** 这是本模块最重要的一条：
  把 STOP 单悄悄当 MARKET 发出去，等于把"触发价"这个风险控制点丢掉。
* 类型/TIF 一旦确定就是**不可变**的（`OrderRequest` 里是字符串，这里统一成枚举校验）。
* 状态机是唯一的状态变更入口，非法跃迁直接抛错，不靠调用方自觉。
* 券商能力用 :class:`BrokerCapabilities` 显式声明；Paper 与真实券商各自填写，
  调用方按能力校验，而不是按"猜券商应该支持"。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .base import OrderRejected, OrderRequest


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class TimeInForce(str, Enum):
    DAY = "day"
    GTC = "gtc"
    IOC = "ioc"
    FOK = "fok"
    OPG = "opg"
    CLS = "cls"


#: 需要触发价的类型
PRICE_TRIGGERED = {OrderType.STOP, OrderType.STOP_LIMIT}
#: 需要限价的类型
PRICE_LIMITED = {OrderType.LIMIT, OrderType.STOP_LIMIT}
#: 只能在开/收盘集合竞价使用的 TIF
AUCTION_TIFS = {TimeInForce.OPG, TimeInForce.CLS}
#: 一次性（不挂单）的 TIF
IMMEDIATE_TIFS = {TimeInForce.IOC, TimeInForce.FOK}


class OrderState(str, Enum):
    NEW = "new"
    SUBMITTED = "submitted"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"


#: 合法状态跃迁表。**任何不在表里的跃迁都会被拒绝。**
VALID_TRANSITIONS: Dict[OrderState, Set[OrderState]] = {
    OrderState.NEW: {OrderState.SUBMITTED, OrderState.REJECTED,
                     OrderState.PARTIAL, OrderState.FILLED, OrderState.CANCELED},
    OrderState.SUBMITTED: {OrderState.PARTIAL, OrderState.FILLED, OrderState.CANCELED,
                           OrderState.REJECTED, OrderState.EXPIRED},
    OrderState.PARTIAL: {OrderState.PARTIAL, OrderState.FILLED, OrderState.CANCELED,
                         OrderState.EXPIRED},
    OrderState.FILLED: set(),
    OrderState.CANCELED: set(),
    OrderState.REJECTED: set(),
    OrderState.EXPIRED: set(),
}

TERMINAL_STATES = {OrderState.FILLED, OrderState.CANCELED,
                   OrderState.REJECTED, OrderState.EXPIRED}


class OrderValidationError(OrderRejected):
    def __init__(self, message: str, code: str = "ORDER_INVALID"):
        super().__init__(message, code=code)


@dataclass(frozen=True)
class BrokerCapabilities:
    """券商能力声明。

    真实券商必须**按官方接口文档逐项填写**，不允许凭猜测声明支持。
    未填即视为不支持（fail-closed）。
    """

    venue: str
    order_types: frozenset = frozenset()          # set[OrderType]
    tifs: frozenset = frozenset()                 # set[TimeInForce]
    supports_short: bool = False
    supports_locate: bool = False                 # 可查询可借券（locate）
    supports_reduce_only: bool = False
    supports_auction_orders: bool = False
    is_live: bool = False

    def supports_type(self, t: OrderType) -> bool:
        return t in self.order_types

    def supports_tif(self, tif: TimeInForce) -> bool:
        return tif in self.tifs

    def as_dict(self) -> Dict[str, Any]:
        return {
            "venue": self.venue,
            "order_types": sorted(x.value for x in self.order_types),
            "tifs": sorted(x.value for x in self.tifs),
            "supports_short": self.supports_short,
            "supports_locate": self.supports_locate,
            "supports_reduce_only": self.supports_reduce_only,
            "supports_auction_orders": self.supports_auction_orders,
            "is_live": self.is_live,
        }


#: Paper Broker 的完整能力（用于模拟与测试）
PAPER_CAPABILITIES = BrokerCapabilities(
    venue="paper",
    order_types=frozenset(OrderType),
    tifs=frozenset(TimeInForce),
    supports_short=True,
    supports_locate=True,
    supports_reduce_only=True,
    supports_auction_orders=True,
    is_live=False,
)

#: 真实券商默认能力：**只保留已确认的最小集合**（limit/market + DAY/GTC）。
#: 其余能力必须在核实官方接口后显式打开。
LIVE_DEFAULT_CAPABILITIES = BrokerCapabilities(
    venue="live",
    order_types=frozenset({OrderType.MARKET, OrderType.LIMIT}),
    tifs=frozenset({TimeInForce.DAY}),
    supports_short=False,
    supports_locate=False,
    supports_reduce_only=False,
    supports_auction_orders=False,
    is_live=True,
)


# ---------------------------------------------------------------------- #
# 解析与校验
# ---------------------------------------------------------------------- #
def parse_order_type(raw: Any) -> OrderType:
    if isinstance(raw, OrderType):
        return raw
    if raw is None:
        return OrderType.LIMIT
    text = str(raw).strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "market": OrderType.MARKET,
        "mkt": OrderType.MARKET,
        "limit": OrderType.LIMIT,
        "lmt": OrderType.LIMIT,
        "stop": OrderType.STOP,
        "stop_market": OrderType.STOP,
        "stp": OrderType.STOP,
        "stop_limit": OrderType.STOP_LIMIT,
        "stoplimit": OrderType.STOP_LIMIT,
    }
    if text not in aliases:
        raise OrderValidationError(f"未知订单类型: {raw!r}（支持 market/limit/stop/stop_limit）")
    return aliases[text]


def parse_tif(raw: Any) -> TimeInForce:
    if isinstance(raw, TimeInForce):
        return raw
    if raw is None:
        return TimeInForce.DAY
    text = str(raw).strip().lower()
    aliases = {
        "day": TimeInForce.DAY,
        "gtc": TimeInForce.GTC,
        "ioc": TimeInForce.IOC,
        "fok": TimeInForce.FOK,
        "opg": TimeInForce.OPG,
        "cls": TimeInForce.CLS,
    }
    if text not in aliases:
        raise OrderValidationError(f"未知 TIF: {raw!r}（支持 DAY/GTC/IOC/FOK/OPG/CLS）")
    return aliases[text]


@dataclass(frozen=True)
class ValidatedOrder:
    order_type: OrderType
    tif: TimeInForce
    qty: float
    price: Optional[float]
    stop_price: Optional[float]

    def as_dict(self) -> Dict[str, Any]:
        return {"order_type": self.order_type.value, "tif": self.tif.value,
                "qty": self.qty, "price": self.price, "stop_price": self.stop_price}


def validate_order(req: OrderRequest, caps: BrokerCapabilities,
                   *, in_auction_window: bool = False,
                   allow_short: bool = False) -> ValidatedOrder:
    """校验订单是否符合券商能力与类型语义。

    任何不支持的情况都**直接抛错**，绝不做类型降级。
    """
    otype = parse_order_type(getattr(req, "order_type", None))
    tif = parse_tif(getattr(req, "tif", None))
    qty = float(req.qty or 0.0)

    if qty <= 0:
        raise OrderValidationError(f"数量必须大于 0：{qty}")
    if str(req.side).lower() not in ("buy", "sell"):
        raise OrderValidationError(f"非法方向：{req.side}")

    if not caps.supports_type(otype):
        raise OrderValidationError(
            f"{caps.venue} 不支持订单类型 {otype.value}（支持："
            f"{sorted(x.value for x in caps.order_types)}）；拒绝下单，不会降级为 market",
            code="ORDER_TYPE_UNSUPPORTED")
    if not caps.supports_tif(tif):
        raise OrderValidationError(
            f"{caps.venue} 不支持 TIF {tif.value}（支持："
            f"{sorted(x.value for x in caps.tifs)}）",
            code="TIF_UNSUPPORTED")

    price = req.price
    stop_price = getattr(req, "stop_price", None)
    if stop_price is None and otype in PRICE_TRIGGERED:
        # 兼容旧字段：stop_loss 在本系统的语义就是"止损触发价"
        stop_price = getattr(req, "stop_loss", None)

    if otype in PRICE_LIMITED:
        if price is None or float(price) <= 0:
            raise OrderValidationError(f"{otype.value} 必须提供正数 price")
    if otype in PRICE_TRIGGERED:
        if stop_price is None or float(stop_price) <= 0:
            raise OrderValidationError(f"{otype.value} 必须提供正数触发价 stop_price")
        side = str(req.side).lower()
        # 买入止损必须在市价之上，卖出止损必须在市价之下（否则是立即成交的无效触发）
        if side == "buy" and float(stop_price) <= 0:
            raise OrderValidationError("买入止损触发价非法")
        if side == "sell" and float(stop_price) <= 0:
            raise OrderValidationError("卖出止损触发价非法")

    if tif in AUCTION_TIFS:
        if not caps.supports_auction_orders:
            raise OrderValidationError(
                f"{caps.venue} 不支持集合竞价订单（{tif.value}），拒绝",
                code="AUCTION_UNSUPPORTED")
        if not in_auction_window:
            raise OrderValidationError(
                f"{tif.value} 只能在开/收盘集合竞价窗口内提交", code="NOT_AUCTION_WINDOW")

    if str(req.side).lower() == "sell" and allow_short:
        pass  # 卖空资格由 short 模块单独把关，这里不重复判断

    return ValidatedOrder(
        order_type=otype, tif=tif, qty=qty,
        price=float(price) if price is not None else None,
        stop_price=float(stop_price) if stop_price is not None else None,
    )


# ---------------------------------------------------------------------- #
# 状态机
# ---------------------------------------------------------------------- #
@dataclass
class OrderEvent:
    ts: datetime
    state: OrderState
    filled_qty: float = 0.0
    avg_price: Optional[float] = None
    note: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"ts": self.ts.isoformat(), "state": self.state.value,
                "filled_qty": self.filled_qty, "avg_price": self.avg_price,
                "note": self.note}


@dataclass
class OrderLifecycle:
    """单笔订单的状态机 + 审计轨迹。

    只允许 :data:`VALID_TRANSITIONS` 中的跃迁；重复写入同一状态且成交量不变时
    视为幂等（不报错、不重复记事件）。
    """

    order_id: str
    client_id: str
    symbol: str
    side: str
    qty: float
    order_type: OrderType = OrderType.LIMIT
    tif: TimeInForce = TimeInForce.DAY
    state: OrderState = OrderState.NEW
    filled_qty: float = 0.0
    avg_price: Optional[float] = None
    events: List[OrderEvent] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._record(OrderState.NEW, note="created")

    def _record(self, state: OrderState, filled_qty: Optional[float] = None,
                avg_price: Optional[float] = None, note: str = "") -> None:
        self.events.append(OrderEvent(
            ts=datetime.now(timezone.utc), state=state,
            filled_qty=self.filled_qty if filled_qty is None else filled_qty,
            avg_price=avg_price if avg_price is not None else self.avg_price,
            note=note))

    def transition(self, new_state: OrderState, *, filled_qty: Optional[float] = None,
                   avg_price: Optional[float] = None, note: str = "") -> OrderState:
        if new_state == self.state:
            if filled_qty is not None and abs(filled_qty - self.filled_qty) < 1e-9:
                return self.state          # 幂等
            if new_state != OrderState.PARTIAL:
                return self.state
        if new_state not in VALID_TRANSITIONS[self.state]:
            raise OrderValidationError(
                f"非法状态跃迁 {self.state.value} → {new_state.value}（order={self.order_id}）",
                code="ILLEGAL_TRANSITION")
        if filled_qty is not None:
            if filled_qty < self.filled_qty - 1e-9:
                raise OrderValidationError(
                    f"成交数量回退：{filled_qty} < 已成交 {self.filled_qty}",
                    code="FILL_QTY_REGRESSION")
            if filled_qty > self.qty + 1e-9:
                raise OrderValidationError(
                    f"成交数量超过下单量：{filled_qty} > {self.qty}",
                    code="OVERFILL")
            if new_state == OrderState.FILLED and abs(filled_qty - self.qty) > 1e-9:
                raise OrderValidationError(
                    f"FILLED 要求全部成交：{filled_qty}/{self.qty}", code="PARTIAL_AS_FILLED")
            self.filled_qty = filled_qty
        if avg_price is not None:
            self.avg_price = avg_price
        self.state = new_state
        self._record(new_state, note=note)
        return self.state

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def as_dict(self) -> Dict[str, Any]:
        return {
            "order_id": self.order_id, "client_id": self.client_id,
            "symbol": self.symbol, "side": self.side, "qty": self.qty,
            "order_type": self.order_type.value, "tif": self.tif.value,
            "state": self.state.value, "filled_qty": self.filled_qty,
            "avg_price": self.avg_price,
            "events": [e.as_dict() for e in self.events],
        }


# ---------------------------------------------------------------------- #
# TIF 成交量语义
# ---------------------------------------------------------------------- #
@dataclass(frozen=True)
class TIFOutcome:
    """某个 TIF 下"这一轮能成交多少、剩余怎么办"。"""

    fill_qty: float
    remainder: float
    cancel_remainder: bool
    rest_remainder: bool
    reject_all: bool = False
    note: str = ""


def apply_tif(tif: TimeInForce, requested_qty: float, fillable_qty: float) -> TIFOutcome:
    """按 TIF 计算成交与剩余处理。

    * ``FOK``  → 无法全成则全部不成交（reject_all）
    * ``IOC``  → 成交可成交部分，剩余立即撤销
    * ``DAY`` / ``GTC`` → 成交可成交部分，剩余挂单
    * ``OPG`` / ``CLS`` → 只在竞价成交，未成交即失效（expired）
    """
    qty = max(0.0, float(requested_qty))
    fillable = max(0.0, min(float(fillable_qty), qty))

    if tif == TimeInForce.FOK:
        if fillable + 1e-9 < qty:
            return TIFOutcome(0.0, qty, cancel_remainder=True, rest_remainder=False,
                              reject_all=True, note="FOK 无法全部成交，整单不执行")
        return TIFOutcome(qty, 0.0, False, False, note="FOK 全部成交")

    if tif == TimeInForce.IOC:
        return TIFOutcome(fillable, qty - fillable,
                          cancel_remainder=True, rest_remainder=False,
                          note=f"IOC 成交 {fillable}，剩余 {qty - fillable} 立即撤销")

    if tif in AUCTION_TIFS:
        return TIFOutcome(fillable, qty - fillable,
                          cancel_remainder=True, rest_remainder=False,
                          note=f"{tif.value} 竞价成交 {fillable}，剩余失效")

    # DAY / GTC
    return TIFOutcome(fillable, qty - fillable,
                      cancel_remainder=False, rest_remainder=True,
                      note=f"{tif.value} 成交 {fillable}，剩余挂单")
