"""券商抽象层。

统一的数据结构与接口，让策略/风控/Web 完全不感知底层是模拟撮合还是真实券商。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional


class BrokerError(Exception):
    """券商层统一异常。"""

    def __init__(self, message: str, code: str = "BROKER_ERROR", retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class BrokerNotConfigured(BrokerError):
    """缺少凭证/未授权，明确表示"未连接"。"""

    def __init__(self, message: str):
        super().__init__(message, code="NOT_CONFIGURED", retryable=False)


class OrderRejected(BrokerError):
    def __init__(self, message: str, code: str = "ORDER_REJECTED"):
        super().__init__(message, code=code, retryable=False)


class OrderStateUnknown(BrokerError):
    """下单结果未知 —— 券商可能已受理，也可能没有。

    出现本异常的典型场景：

    * 券商响应里没有 ``orderId``（结构变化 / 空 data）
    * 请求超时、连接中断、读到一半断线

    处理原则（**禁止盲目重试下单**）：

    1. 绝不假定 ``submitted`` 或 ``filled``；
    2. 必须带 ``client_id`` 去券商侧对账（:meth:`Broker.reconcile_order`）；
    3. 对账未完成前，该标的禁止再下单（风控 ``unresolved_order_state``）。
    """

    def __init__(self, message: str, client_id: str = "", symbol: str = "",
                 side: str = "", qty: float = 0.0, price: Optional[float] = None,
                 code: str = "ORDER_STATE_UNKNOWN", cause: str = ""):
        super().__init__(message, code=code, retryable=False)
        self.client_id = client_id
        self.symbol = symbol
        self.side = side
        self.qty = qty
        self.price = price
        self.cause = cause

    def as_dict(self) -> Dict[str, Any]:
        return {
            "message": str(self),
            "code": self.code,
            "client_id": self.client_id,
            "symbol": self.symbol,
            "side": self.side,
            "qty": self.qty,
            "price": self.price,
            "cause": self.cause,
        }


@dataclass
class Quote:
    symbol: str
    market: str
    last: float
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    prev_close: float = 0.0
    volume: float = 0.0
    ts: str = ""
    source: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Candle:
    ts: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class Position:
    symbol: str
    market: str
    qty: float
    avg_cost: float
    last_price: float = 0.0
    currency: str = ""
    name: str = ""
    #: 该持仓来自哪个交易时段（regular/pre/post/night）。
    #: 券商未提供时为空字符串 —— 不猜。空值同样必须进入风控，
    #: 不能因为"不知道时段"就把持仓排除在止损之外（US-P0-3）。
    session: str = ""

    @property
    def market_value(self) -> float:
        return self.qty * (self.last_price or self.avg_cost)

    @property
    def unrealized_pnl(self) -> float:
        return (self.last_price - self.avg_cost) * self.qty if self.last_price else 0.0

    @property
    def unrealized_pct(self) -> float:
        if not self.avg_cost:
            return 0.0
        return (self.last_price - self.avg_cost) / self.avg_cost if self.last_price else 0.0

    def as_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["market_value"] = round(self.market_value, 4)
        d["unrealized_pnl"] = round(self.unrealized_pnl, 4)
        d["unrealized_pct"] = round(self.unrealized_pct, 6)
        return d


@dataclass
class AccountSummary:
    currency: str
    cash: float
    market_value: float
    equity: float
    day_pnl: float = 0.0
    buying_power: float = 0.0
    source: str = ""
    #: 券商是否真的返回了日内盈亏。False → 单日亏损熔断必须判为"无法判定"。
    day_pnl_known: bool = True

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class OrderRequest:
    symbol: str
    market: str
    side: str                     # buy | sell
    qty: float
    price: Optional[float] = None
    order_type: str = "limit"     # limit | market | stop | stop_limit
    currency: str = ""
    tif: str = "day"              # day | gtc | ioc | fok | opg | cls
    #: 止损/止盈触发价。``stop`` / ``stop_limit`` 单必填（P2-B）。
    stop_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    signal_id: str = ""
    decision_id: str = ""
    note: str = ""
    #: 幂等/对账键。为空时由券商适配器按 decision_id 稳定派生。
    client_id: str = ""


@dataclass
class OrderResult:
    order_id: str
    broker_order_id: str
    symbol: str
    market: str
    side: str
    qty: float
    price: Optional[float]
    #: submitted | filled | partial | cancelled | rejected | expired | **unknown**
    #:
    #: ``unknown`` 表示"券商是否受理未知"。**任何代码都不得把 unknown 当作
    #: submitted 或 filled 处理**，必须先走 :meth:`Broker.reconcile_order`。
    status: str
    filled_qty: float = 0.0
    avg_fill_price: Optional[float] = None
    commission: float = 0.0
    venue: str = ""
    ts: str = ""
    #: 幂等/对账键：下单前生成，重试与核对都以它为准
    client_id: str = ""
    #: 对账说明（为什么是 unknown、查到了什么）
    reconcile_note: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_unknown(self) -> bool:
        return str(self.status).lower() in ("unknown", "pending_reconcile")

    def as_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("raw", None)
        return d


@dataclass
class AccountInfo:
    sub_account_id: str
    name: str = ""
    currency: str = ""
    account_type: str = ""
    market: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


class Broker:
    """所有券商实现的接口契约（鸭子类型 + 显式基类，便于类型检查）。"""

    venue: str = "abstract"
    is_live: bool = False
    connected: bool = False

    # 账户
    def get_accounts(self) -> List[AccountInfo]:
        raise NotImplementedError

    def get_summary(self, currency: str = "") -> AccountSummary:
        raise NotImplementedError

    def get_positions(self, **kwargs: Any) -> List[Position]:
        """查询全部持仓。

        实现层可支持 ``use_us_pre`` / ``use_us_post`` / ``use_us_night``
        （美股盘前/盘后/夜盘持仓）。**任何时段的持仓都必须返回** ——
        漏掉一段就等于漏掉一段风险敞口（US-P0-3）。
        """
        raise NotImplementedError

    # 行情
    def get_quote(self, symbol: str) -> Quote:
        raise NotImplementedError

    def get_quotes(self, symbols: List[str]) -> Dict[str, Quote]:
        raise NotImplementedError

    def get_klines(self, symbol: str, ktype: str = "day", num: int = 120) -> List[Candle]:
        raise NotImplementedError

    # 交易
    def place_order(self, req: OrderRequest) -> OrderResult:
        raise NotImplementedError

    def cancel_order(self, order_id: str) -> OrderResult:
        raise NotImplementedError

    def get_order(self, order_id: str) -> Optional[OrderResult]:
        raise NotImplementedError

    def list_orders(self, limit: int = 50) -> List[OrderResult]:
        raise NotImplementedError

    def reconcile_order(self, client_id: str, symbol: str = "",
                        side: str = "", qty: float = 0.0) -> Optional[OrderResult]:
        """用 ``client_id`` 去券商侧查这笔单到底有没有受理。

        返回：

        * :class:`OrderResult` —— 查到了，带着券商的真实 orderId 与状态
        * ``None`` —— 明确查不到（判定为"未受理"）

        实现方应在**无法判断**时抛 :class:`BrokerError`，而不是返回 ``None``：
        "查不到" 与 "查不清" 必须区分，否则会把已受理的单当成未受理去重下。
        """
        raise NotImplementedError

    def health(self) -> Dict[str, Any]:
        raise NotImplementedError


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def derive_client_id(decision_id: str, signal_id: str = "", symbol: str = "",
                     side: str = "") -> str:
    """稳定派生 ``clientId``（0 < id < 2,000,000,000）。

    同一次决策永远得到同一个值 —— 这是"重试不会变成两笔单"的前提，
    也是 :meth:`Broker.reconcile_order` 能查回来的依据。
    """
    import hashlib

    seed = f"{decision_id}|{signal_id}|{symbol}|{side}"
    h = int(hashlib.sha256(seed.encode()).hexdigest()[:12], 16)
    return str(h % 2_000_000_000)
