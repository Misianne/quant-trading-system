"""美股 T+1 结算资金账本（P2-E）。

要解决的问题
------------
原先 ``AccountSummary.buying_power`` 直接采信券商数字，系统自身没有账本：
一旦券商返回的购买力口径变化（或干脆没返回），风控就失去"钱够不够"的依据，
也无法发现"内部账本与券商不一致"。

本模块提供
----------
* 逐笔记录成交（含手续费、部分成交、撤单、失败）。
* 按**交易日历**推进 T+1 结算（不是 ``datetime + 1 day``）。
* 明确区分 现金余额 / 未结算资金 / 已结算资金 / 购买力 / 保证金。
* 与 Broker 返回的购买力对账，差异超阈值生成告警。
* 现金账户 vs 保证金账户两套购买力口径。

安全语义
--------
* 未结算的**卖出回款**在现金账户里**不计入购买力**（这是最容易出错的地方）。
* 券商购买力**不被无条件覆盖**：本账本只产出 ``available`` 与 ``alerts``，
  最终可否下单由风控按"两者取严"决定（见 :meth:`can_afford`）。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Any, Callable, Dict, Iterable, List, Optional, Protocol, Tuple


class AccountMode(str, Enum):
    CASH = "cash"
    MARGIN = "margin"


class EntryKind(str, Enum):
    BUY = "buy"
    SELL = "sell"
    FEE = "fee"
    DEPOSIT = "deposit"
    WITHDRAW = "withdraw"
    DIVIDEND = "dividend"
    ADJUSTMENT = "adjustment"


class SettlementStatus(str, Enum):
    UNSETTLED = "unsettled"
    SETTLED = "settled"
    VOID = "void"


class SettlementError(ValueError):
    pass


class TradingCalendar(Protocol):
    """只需要一个方法：判断某日是否交易日。"""

    def is_trading_day(self, day: date) -> Tuple[bool, str]:  # pragma: no cover
        ...


@dataclass
class LedgerEntry:
    entry_id: str
    kind: EntryKind
    trade_date: str                 # ISO 日期（交易日）
    settle_date: str                # ISO 日期（T+1 后的交易日）
    symbol: str
    qty: float
    price: float
    gross: float                    # 含符号：买入为负（付出），卖出为正（收到）
    fee: float
    net: float                      # gross - fee
    status: SettlementStatus = SettlementStatus.UNSETTLED
    settled_on: Optional[str] = None
    note: str = ""

    @property
    def affects_cash_immediately(self) -> bool:
        """买入的现金流出是立即的；卖出的回款要等结算。"""
        return self.kind == EntryKind.BUY and self.net < 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "entry_id": self.entry_id, "kind": self.kind.value,
            "trade_date": self.trade_date, "settle_date": self.settle_date,
            "symbol": self.symbol, "qty": self.qty, "price": self.price,
            "gross": round(self.gross, 6), "fee": round(self.fee, 6),
            "net": round(self.net, 6), "status": self.status.value,
            "settled_on": self.settled_on, "note": self.note,
        }


class _WeekdayCalendar:
    """兜底日历：只跳周末。**仅供无日历对象时使用**，会被显式标记。"""

    def is_trading_day(self, day: date) -> Tuple[bool, str]:
        if day.weekday() >= 5:
            return False, "周末"
        return True, ""

    name = "weekday-only"


class SettlementLedger:
    """美金现金/结算账本（单一币种一个实例）。"""

    def __init__(self, currency: str = "USD",
                 calendar: Optional[TradingCalendar] = None,
                 mode: AccountMode = AccountMode.CASH,
                 margin_multiplier: float = 2.0,
                 settle_lag_days: int = 1,
                 tolerance: float = 0.01,
                 opening_cash: float = 0.0):
        if settle_lag_days < 0:
            raise SettlementError("settle_lag_days 不能为负")
        self.currency = currency
        self.calendar = calendar or _WeekdayCalendar()
        self.mode = mode
        self.margin_multiplier = float(margin_multiplier)
        self.settle_lag_days = int(settle_lag_days)
        self.tolerance = float(tolerance)

        self._entries: List[LedgerEntry] = []
        self._opening = float(opening_cash)
        self._settled_cash = float(opening_cash)
        self._unsettled: float = 0.0
        self._last_settle_check: Optional[str] = None
        self.alerts: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------ #
    # 日历
    # ------------------------------------------------------------------ #
    def _is_trading_day(self, day: date) -> bool:
        try:
            ok, _ = self.calendar.is_trading_day(day)
            return bool(ok)
        except Exception:
            return day.weekday() < 5      # 日历故障 → 退化到周末判断，同时记告警

    def next_trading_day(self, day: date, lag: Optional[int] = None) -> date:
        """从 ``day`` 起向后推 ``lag`` 个**交易日**（不是自然日）。"""
        remaining = self.settle_lag_days if lag is None else int(lag)
        cur = day
        guard = 0
        while remaining > 0:
            cur = cur + timedelta(days=1)
            if self._is_trading_day(cur):
                remaining -= 1
            guard += 1
            if guard > 30:
                raise SettlementError(f"日历异常：{day} 起 30 天内未找到足够交易日")
        # 若起始日本身非交易日，结算日仍取之后的交易日
        while not self._is_trading_day(cur):
            cur = cur + timedelta(days=1)
            guard += 1
            if guard > 60:
                raise SettlementError(f"日历异常：无法为 {day} 计算结算日")
        return cur

    # ------------------------------------------------------------------ #
    # 记账
    # ------------------------------------------------------------------ #
    def record_trade(self, *, trade_date: date, side: str, symbol: str,
                     qty: float, price: float, fee: float = 0.0,
                     kind: Optional[EntryKind] = None,
                     note: str = "") -> LedgerEntry:
        side = str(side).lower()
        if side not in ("buy", "sell"):
            raise SettlementError(f"非法方向：{side}")
        qty = float(qty)
        price = float(price)
        fee = abs(float(fee))
        if qty <= 0 or price < 0:
            raise SettlementError(f"数量/价格非法：qty={qty} price={price}")

        gross = -(qty * price) if side == "buy" else (qty * price)
        net = gross - fee
        entry_kind = kind or (EntryKind.BUY if side == "buy" else EntryKind.SELL)
        settle = self.next_trading_day(trade_date)

        entry = LedgerEntry(
            entry_id=f"LE-{uuid.uuid4().hex[:10]}",
            kind=entry_kind,
            trade_date=trade_date.isoformat(),
            settle_date=settle.isoformat(),
            symbol=symbol.upper(), qty=qty, price=price,
            gross=gross, fee=fee, net=net,
            note=note,
        )

        if entry_kind == EntryKind.BUY:
            # 买入：现金立刻减少（已结算现金不够时，未结算部分也先垫着，由 can_afford 挡）
            self._settled_cash += net          # net 为负
            entry.status = SettlementStatus.SETTLED
            entry.settled_on = trade_date.isoformat()
        else:
            self._unsettled += net             # 卖出回款进未结算
            entry.status = SettlementStatus.UNSETTLED

        self._entries.append(entry)
        return entry

    def record_cash(self, *, on: date, amount: float,
                    kind: EntryKind = EntryKind.DEPOSIT, note: str = "") -> LedgerEntry:
        amount = float(amount)
        entry = LedgerEntry(
            entry_id=f"LE-{uuid.uuid4().hex[:10]}", kind=kind,
            trade_date=on.isoformat(), settle_date=on.isoformat(),
            symbol="", qty=0.0, price=0.0, gross=amount, fee=0.0, net=amount,
            status=SettlementStatus.SETTLED, settled_on=on.isoformat(), note=note,
        )
        self._settled_cash += amount
        self._entries.append(entry)
        return entry

    def void_entry(self, entry_id: str, reason: str = "") -> LedgerEntry:
        """撤单/失败：冲正一条账目。"""
        for e in self._entries:
            if e.entry_id == entry_id:
                if e.status == SettlementStatus.VOID:
                    return e
                if e.status == SettlementStatus.SETTLED:
                    self._settled_cash -= e.net
                else:
                    self._unsettled -= e.net
                e.status = SettlementStatus.VOID
                e.note = (e.note + " | " if e.note else "") + f"void: {reason}"
                return e
        raise SettlementError(f"未知账目：{entry_id}")

    # ------------------------------------------------------------------ #
    # 结算推进
    # ------------------------------------------------------------------ #
    def settle_through(self, on: date) -> List[LedgerEntry]:
        """把结算日 ≤ ``on`` 的未结算账目转入已结算。"""
        moved: List[LedgerEntry] = []
        for e in self._entries:
            if e.status != SettlementStatus.UNSETTLED:
                continue
            if e.settle_date > on.isoformat():
                continue
            self._unsettled -= e.net
            self._settled_cash += e.net
            e.status = SettlementStatus.SETTLED
            e.settled_on = on.isoformat()
            moved.append(e)
        self._last_settle_check = on.isoformat()
        return moved

    # ------------------------------------------------------------------ #
    # 余额视图
    # ------------------------------------------------------------------ #
    @property
    def settled_cash(self) -> float:
        return self._settled_cash

    @property
    def unsettled_cash(self) -> float:
        return self._unsettled

    @property
    def total_cash(self) -> float:
        return self._settled_cash + self._unsettled

    def buying_power(self) -> float:
        """可用购买力。

        * 现金账户：只用**已结算**现金。
        * 保证金账户：已结算现金 × 倍数 + 未结算（券商通常允许用未结算卖出款再买，
          但提现要等结算）。
        """
        if self.mode == AccountMode.CASH:
            return max(0.0, self._settled_cash)
        return max(0.0, self._settled_cash * self.margin_multiplier + self._unsettled)

    def cash_available_to_withdraw(self) -> float:
        return max(0.0, self._settled_cash)

    def can_afford(self, amount: float) -> Tuple[bool, str]:
        if amount <= 0:
            return True, ""
        bp = self.buying_power()
        if bp + 1e-9 >= amount:
            return True, f"内部账本购买力 {bp:.2f} >= 需要 {amount:.2f}"
        return False, (f"内部账本购买力不足：需要 {amount:.2f}，"
                       f"可用 {bp:.2f}（已结算 {self._settled_cash:.2f} + "
                       f"未结算 {self._unsettled:.2f}，mode={self.mode.value}）")

    def snapshot(self) -> Dict[str, Any]:
        return {
            "currency": self.currency,
            "mode": self.mode.value,
            "settled_cash": round(self._settled_cash, 6),
            "unsettled_cash": round(self._unsettled, 6),
            "total_cash": round(self.total_cash, 6),
            "buying_power": round(self.buying_power(), 6),
            "withdrawable": round(self.cash_available_to_withdraw(), 6),
            "entries": len(self._entries),
            "unsettled_entries": sum(1 for e in self._entries
                                     if e.status == SettlementStatus.UNSETTLED),
            "last_settle_check": self._last_settle_check,
        }

    def unresolved(self) -> List[LedgerEntry]:
        return [e for e in self._entries if e.status == SettlementStatus.UNSETTLED]

    # ------------------------------------------------------------------ #
    # 对账
    # ------------------------------------------------------------------ #
    def reconcile(self, broker_buying_power: Optional[float],
                  broker_cash: Optional[float] = None,
                  *, on: Optional[date] = None) -> Dict[str, Any]:
        """与券商数据对账。**不覆盖**券商值，只产出差异与告警。"""
        result: Dict[str, Any] = {"ok": True, "alerts": []}
        if broker_buying_power is None:
            alert = {"kind": "broker_bp_missing",
                     "message": "券商未提供购买力，无法对账（本账本继续独立计账）"}
            result["ok"] = False
            result["alerts"].append(alert)
            self.alerts.append(alert)
            return result

        internal = self.buying_power()
        delta = float(broker_buying_power) - internal
        base = max(abs(float(broker_buying_power)), abs(internal), 1.0)
        pct = abs(delta) / base
        result.update({"internal_bp": round(internal, 6),
                       "broker_bp": round(float(broker_buying_power), 6),
                       "delta": round(delta, 6), "delta_pct": round(pct, 6)})
        if pct > self.tolerance:
            alert = {"kind": "buying_power_mismatch",
                     "message": (f"购买力差异 {delta:.2f}（{pct:.2%}）超过阈值 "
                                 f"{self.tolerance:.2%}；internal={internal:.2f} "
                                 f"broker={float(broker_buying_power):.2f}"),
                     "on": (on or date.today()).isoformat()}
            result["ok"] = False
            result["alerts"].append(alert)
            self.alerts.append(alert)

        if broker_cash is not None:
            cash_delta = float(broker_cash) - self.total_cash
            result["cash_delta"] = round(cash_delta, 6)
        return result

    def effective_available(self, broker_buying_power: Optional[float]) -> Tuple[float, str]:
        """**两者取严**：内部账本与券商购买力都允许才放行。"""
        internal = self.buying_power()
        if broker_buying_power is None:
            return internal, "券商购买力未知，仅用内部账本"
        return min(internal, float(broker_buying_power)), "取内部账本与券商的较小值"
