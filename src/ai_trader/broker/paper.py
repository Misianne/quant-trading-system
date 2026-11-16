"""本地模拟撮合券商（Paper Broker）。

诚实声明
--------
**默认行情源是合成数据**（``SyntheticQuoteSource``），由 ``symbol + 日期`` 做种子
生成确定性随机游走序列。它不是真实行情，只用于打通"信号→风控→下单→撮合→
记账"的完整链路。若接入真实行情源，把 ``quote_source`` 换成实现了
:class:`QuoteSource` 的对象即可（例如 :class:`ai_trader.broker.fosun_adapter.FosunBroker`
的行情部分）。

撮合规则（保守，宁可少成交）
--------------------------
* 市价单：以最新价 ± 滑点成交。
* 限价买单：仅当 ``最新价 <= 委托价`` 时成交，成交价取二者较低者。
* 限价卖单：仅当 ``最新价 >= 委托价`` 时成交，成交价取二者较高者。
* 未成交的限价单挂为 ``submitted``，由 :meth:`PaperBroker.refresh_pending` 复核。
* 手续费 = max(成交额 * bps, 最低手续费)，按币种分别记现金。
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import threading
import uuid
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import (
    AccountInfo,
    AccountSummary,
    Broker,
    BrokerNotConfigured,
    Candle,
    OrderRejected,
    OrderRequest,
    OrderResult,
    Position,
    Quote,
    now_iso,
)

CURRENCY_BY_MARKET = {"hk": "HKD", "us": "USD", "cn": "CNY", "sh": "CNY", "sz": "CNY"}


def market_of(symbol: str) -> str:
    s = str(symbol).lower()
    if s.startswith("hk"):
        return "hk"
    if s.startswith("us"):
        return "us"
    if s.startswith(("sh", "sz", "cn")):
        return "cn"
    return "hk"


def currency_of(symbol: str) -> str:
    return CURRENCY_BY_MARKET.get(market_of(symbol), "HKD")


# --------------------------------------------------------------------------- #
# 行情源
# --------------------------------------------------------------------------- #
class QuoteSource:
    """行情源接口。"""

    source_name = "abstract"

    def quote(self, symbol: str) -> Quote:
        raise NotImplementedError

    def klines(self, symbol: str, ktype: str = "day", num: int = 120) -> List[Candle]:
        raise NotImplementedError


class SyntheticQuoteSource(QuoteSource):
    """确定性合成行情。

    同一 ``symbol + 日期`` 永远得到同一条序列。**默认跟随系统日期**
    （实盘模拟需要每天有变化），但允许通过 ``reference_day`` 固定到某一天 ——
    单元测试必须这样做，否则"今天行情恰好是空头排列"之类的外部条件会让
    与行情无关的用例随机失败（本机实测：同一套测试在不同日历日结果不同）。

    注意：``reference_day`` 只影响"取哪一天的随机种子"，不改变价格模型本身。
    """

    source_name = "synthetic"

    #: 类级默认参考日；``None`` = 跟随系统日期。测试夹具会统一设为固定值。
    reference_day: Optional[date] = None

    BASE_PRICES = {
        "hk00700": 385.0, "hk09988": 92.0, "hk03690": 118.0, "hk00941": 72.0,
        "usAAPL": 228.0, "usNVDA": 142.0, "usTSLA": 265.0, "usMSFT": 420.0,
        "sh600519": 1520.0, "sz000001": 11.5, "cn600519": 1520.0,
    }

    def __init__(self, volatility: float = 0.018,
                 reference_day: Optional[date] = None):
        self.volatility = volatility
        self.reference_day = reference_day

    def _seed(self, symbol: str, day: Optional[date] = None) -> int:
        day = day or self.reference_day or self.__class__.reference_day or date.today()
        raw = f"{symbol.lower()}|{day.isoformat()}|paper-v1"
        return int(hashlib.sha256(raw.encode()).hexdigest()[:16], 16)

    def _base(self, symbol: str) -> float:
        s = symbol.lower()
        if s in self.BASE_PRICES:
            return self.BASE_PRICES[s]
        # 未知标的使用稳定哈希派生一个合理价位
        h = int(hashlib.sha256(s.encode()).hexdigest()[:8], 16)
        return 20.0 + (h % 48000) / 100.0

    def _walk(self, symbol: str, n: int) -> List[float]:
        rng = random.Random(self._seed(symbol))
        price = self._base(symbol)
        out = []
        for _ in range(n):
            drift = rng.gauss(0, self.volatility)
            price = max(0.01, price * (1 + drift))
            out.append(round(price, 4))
        return out

    def klines(self, symbol: str, ktype: str = "day", num: int = 120) -> List[Candle]:
        rng = random.Random(self._seed(symbol) ^ 0x5EED)
        closes = self._walk(symbol, num)
        candles: List[Candle] = []
        start = datetime.now() - timedelta(days=num)
        prev = closes[0]
        for i, c in enumerate(closes):
            o = prev
            hi = max(o, c) * (1 + abs(rng.gauss(0, 0.004)))
            lo = min(o, c) * (1 - abs(rng.gauss(0, 0.004)))
            vol = 1_000_000 * (1 + abs(rng.gauss(0, 0.5)))
            candles.append(Candle(
                ts=(start + timedelta(days=i)).strftime("%Y-%m-%d"),
                open=round(o, 4), high=round(hi, 4), low=round(lo, 4),
                close=round(c, 4), volume=round(vol, 0),
            ))
            prev = c
        return candles

    def quote(self, symbol: str) -> Quote:
        candles = self.klines(symbol, "day", 30)
        last = candles[-1]
        prev_close = candles[-2].close if len(candles) > 1 else last.close
        return Quote(
            symbol=symbol,
            market=market_of(symbol),
            last=last.close,
            open=last.open,
            high=last.high,
            low=last.low,
            prev_close=prev_close,
            volume=last.volume,
            ts=now_iso(),
            source=self.source_name,
        )


# --------------------------------------------------------------------------- #
# 模拟券商
# --------------------------------------------------------------------------- #
class PaperBroker(Broker):
    venue = "paper"
    is_live = False

    def __init__(
        self,
        state_path: str | Path,
        initial_cash: Optional[Dict[str, float]] = None,
        commission_bps: float = 8.0,
        min_commission: float = 3.0,
        slippage_bps: float = 5.0,
        quote_source: Optional[QuoteSource] = None,
        allow_partial_fill: bool = True,
    ):
        self.state_path = Path(state_path)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.initial_cash = dict(initial_cash or {"HKD": 1_000_000.0, "USD": 130_000.0, "CNY": 700_000.0})
        self.commission_bps = float(commission_bps)
        self.min_commission = float(min_commission)
        self.slippage_bps = float(slippage_bps)
        self.allow_partial_fill = allow_partial_fill
        self.quote_source = quote_source or SyntheticQuoteSource()
        self._lock = threading.RLock()
        self._state = self._load()
        self.connected = True

    # ---------------------------- 状态持久化 ----------------------------
    def _default_state(self) -> Dict[str, Any]:
        return {
            "created_at": now_iso(),
            "cash": dict(self.initial_cash),
            "positions": {},          # symbol -> {qty, avg_cost, market, currency, name}
            "orders": {},             # order_id -> order dict
            "realized_pnl": {},       # currency -> float
            "day_start_equity": {},   # currency -> float
            "day": date.today().isoformat(),
        }

    def _load(self) -> Dict[str, Any]:
        if self.state_path.exists():
            try:
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                base = self._default_state()
                base.update(data)
                return base
            except (ValueError, OSError):
                pass
        st = self._default_state()
        self.state_path.write_text(json.dumps(st, indent=2, ensure_ascii=False), encoding="utf-8")
        return st

    def _save(self) -> None:
        self.state_path.write_text(
            json.dumps(self._state, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
        )

    def _roll_day(self, quotes: Optional[Dict[str, Quote]] = None) -> None:
        """跨日重置日内基准（用于单日亏损统计）。"""
        today = date.today().isoformat()
        if self._state.get("day") != today:
            self._state["day"] = today
            self._state["day_start_equity"] = {
                cur: self._equity(cur, quotes) for cur in self._state["cash"]
            }
            self._save()

    # ---------------------------- 账户 ----------------------------
    def get_accounts(self) -> List[AccountInfo]:
        return [AccountInfo(
            sub_account_id="PAPER-001",
            name="本地模拟账户",
            currency=", ".join(sorted(self._state["cash"])),
            account_type="paper",
            market="hk/us/cn",
            raw={"venue": self.venue},
        )]

    def _market_value(self, currency: str, quotes: Optional[Dict[str, Quote]] = None) -> float:
        total = 0.0
        for sym, pos in self._state["positions"].items():
            if pos.get("currency") != currency or float(pos.get("qty", 0)) == 0:
                continue
            price = None
            if quotes and sym in quotes:
                price = quotes[sym].last
            else:
                try:
                    price = self.quote_source.quote(sym).last
                except Exception:
                    price = pos.get("avg_cost", 0.0)
            total += float(pos["qty"]) * float(price or pos.get("avg_cost", 0))
        return total

    def _equity(self, currency: str, quotes: Optional[Dict[str, Quote]] = None) -> float:
        return float(self._state["cash"].get(currency, 0.0)) + self._market_value(currency, quotes)

    def get_summary(self, currency: str = "") -> AccountSummary:
        with self._lock:
            self._roll_day()
            cur = currency or "HKD"
            cash = float(self._state["cash"].get(cur, 0.0))
            mv = self._market_value(cur)
            eq = cash + mv
            start = float(self._state.get("day_start_equity", {}).get(cur, eq))
            return AccountSummary(
                currency=cur, cash=round(cash, 4), market_value=round(mv, 4),
                equity=round(eq, 4), day_pnl=round(eq - start, 4),
                buying_power=round(cash, 4), source=self.venue,
            )

    def summaries(self) -> List[AccountSummary]:
        return [self.get_summary(c) for c in sorted(self._state["cash"])]

    def get_positions(self, **kwargs: Any) -> List[Position]:
        # 模拟盘没有盘前/盘后/夜盘概念，忽略 use_us_* 参数（接口对齐）
        with self._lock:
            out: List[Position] = []
            for sym, pos in self._state["positions"].items():
                qty = float(pos.get("qty", 0))
                if qty == 0:
                    continue
                try:
                    last = self.quote_source.quote(sym).last
                except Exception:
                    last = float(pos.get("avg_cost", 0))
                out.append(Position(
                    symbol=sym, market=pos.get("market", market_of(sym)), qty=qty,
                    avg_cost=float(pos.get("avg_cost", 0)), last_price=float(last),
                    currency=pos.get("currency", currency_of(sym)), name=pos.get("name", ""),
                ))
            return sorted(out, key=lambda p: p.symbol)

    # ---------------------------- 行情 ----------------------------
    def get_quote(self, symbol: str) -> Quote:
        return self.quote_source.quote(symbol)

    def get_quotes(self, symbols: List[str]) -> Dict[str, Quote]:
        return {s: self.quote_source.quote(s) for s in symbols}

    def get_klines(self, symbol: str, ktype: str = "day", num: int = 120) -> List[Candle]:
        return self.quote_source.klines(symbol, ktype, num)

    # ---------------------------- 撮合 ----------------------------
    def _commission(self, notional: float) -> float:
        return round(max(notional * self.commission_bps / 10_000.0, self.min_commission), 4)

    def _slip(self, price: float, side: str) -> float:
        adj = price * (self.slippage_bps / 10_000.0)
        return price + adj if side == "buy" else max(0.01, price - adj)

    def place_order(self, req: OrderRequest) -> OrderResult:
        with self._lock:
            if not req.symbol:
                raise OrderRejected("symbol 不能为空")
            if req.side not in ("buy", "sell"):
                raise OrderRejected(f"非法方向: {req.side}")
            if not req.qty or float(req.qty) <= 0:
                raise OrderRejected("数量必须大于 0")

            sym = req.symbol
            market = req.market or market_of(sym)
            currency = req.currency or currency_of(sym)
            qty = float(req.qty)
            quote = self.quote_source.quote(sym)
            ref_price = float(req.price) if req.price is not None else quote.last
            order_id = "P" + uuid.uuid4().hex[:14]

            otype = (req.order_type or "limit").lower()
            if otype == "market":
                exec_price = self._slip(quote.last, req.side)
                fill_qty = qty
                status = "filled"
            else:
                if req.price is None:
                    raise OrderRejected("限价单必须提供 price")
                crossed = (quote.last <= ref_price) if req.side == "buy" else (quote.last >= ref_price)
                if crossed:
                    raw = min(ref_price, quote.last) if req.side == "buy" else max(ref_price, quote.last)
                    exec_price = self._slip(raw, req.side)
                    fill_qty = qty
                    status = "filled"
                else:
                    exec_price = None
                    fill_qty = 0.0
                    status = "submitted"

            notional = (exec_price or 0.0) * fill_qty
            commission = self._commission(notional) if fill_qty else 0.0

            # 资金/持仓校验
            if req.side == "buy":
                need = notional + commission
                if status == "filled" and float(self._state["cash"].get(currency, 0.0)) < need:
                    raise OrderRejected(
                        f"{currency} 可用现金不足：需要 {need:.2f}，"
                        f"可用 {float(self._state['cash'].get(currency, 0.0)):.2f}",
                        code="INSUFFICIENT_CASH",
                    )
            else:
                held = float(self._state["positions"].get(sym, {}).get("qty", 0))
                if held < qty:
                    raise OrderRejected(
                        f"持仓不足：{sym} 持有 {held}，卖出 {qty}", code="INSUFFICIENT_POSITION"
                    )

            if status == "filled":
                self._apply_fill(sym, market, currency, req.side, fill_qty, exec_price, commission)

            order = {
                "order_id": order_id,
                "broker_order_id": "SIM-" + order_id,
                "ts": now_iso(),
                "symbol": sym,
                "market": market,
                "side": req.side,
                "qty": qty,
                "price": ref_price,
                "order_type": otype,
                "status": status,
                "filled_qty": fill_qty,
                "avg_fill_price": exec_price,
                "commission": commission,
                "currency": currency,
                "venue": self.venue,
                "signal_id": req.signal_id,
                "decision_id": req.decision_id,
                "note": req.note,
                "stop_loss": req.stop_loss,
                "take_profit": req.take_profit,
                "client_id": req.client_id or order_id,
            }
            self._state["orders"][order_id] = order
            self._save()
            return self._to_result(order)

    def _apply_fill(self, sym: str, market: str, currency: str, side: str,
                    qty: float, price: float, commission: float) -> None:
        cash = float(self._state["cash"].get(currency, 0.0))
        pos = self._state["positions"].setdefault(
            sym, {"qty": 0.0, "avg_cost": 0.0, "market": market, "currency": currency, "name": ""}
        )
        if side == "buy":
            new_qty = float(pos["qty"]) + qty
            prev_cost = float(pos["avg_cost"]) * float(pos["qty"])
            pos["avg_cost"] = round((prev_cost + price * qty + commission) / new_qty, 6)
            pos["qty"] = new_qty
            cash -= price * qty + commission
        else:
            realized = (price - float(pos["avg_cost"])) * qty - commission
            rp = self._state.setdefault("realized_pnl", {})
            rp[currency] = round(float(rp.get(currency, 0.0)) + realized, 4)
            pos["qty"] = float(pos["qty"]) - qty
            if abs(pos["qty"]) < 1e-9:
                pos["qty"] = 0.0
                pos["avg_cost"] = 0.0
            cash += price * qty - commission
        self._state["cash"][currency] = round(cash, 4)

    def _to_result(self, order: Dict[str, Any]) -> OrderResult:
        return OrderResult(
            order_id=order["order_id"], broker_order_id=order["broker_order_id"],
            symbol=order["symbol"], market=order["market"], side=order["side"],
            qty=order["qty"], price=order["price"], status=order["status"],
            filled_qty=order.get("filled_qty", 0.0),
            avg_fill_price=order.get("avg_fill_price"),
            commission=order.get("commission", 0.0), venue=self.venue,
            ts=order.get("ts", ""),
            client_id=order.get("client_id", ""),
            reconcile_note=order.get("reconcile_note", ""),
        )

    def reconcile_order(self, client_id: str, symbol: str = "",
                        side: str = "", qty: float = 0.0) -> Optional[OrderResult]:
        """模拟盘按 client_id 查单。查不到返回 None（模拟盘不存在"可能已受理"）。"""
        if not client_id:
            return None
        with self._lock:
            for order in self._state["orders"].values():
                if str(order.get("client_id", "")) == str(client_id):
                    res = self._to_result(order)
                    res.reconcile_note = "模拟盘按 client_id 命中"
                    return res
        return None

    def cancel_order(self, order_id: str) -> OrderResult:
        with self._lock:
            order = self._state["orders"].get(order_id)
            if not order:
                raise OrderRejected(f"订单不存在: {order_id}", code="ORDER_NOT_FOUND")
            if order["status"] in ("filled", "cancelled", "rejected"):
                raise OrderRejected(
                    f"订单状态 {order['status']} 不可撤销", code="INVALID_STATE"
                )
            order["status"] = "cancelled"
            self._save()
            return self._to_result(order)

    def get_order(self, order_id: str) -> Optional[OrderResult]:
        order = self._state["orders"].get(order_id)
        return self._to_result(order) if order else None

    def list_orders(self, limit: int = 50) -> List[OrderResult]:
        items = sorted(self._state["orders"].values(), key=lambda o: o["ts"], reverse=True)
        return [self._to_result(o) for o in items[:limit]]

    def refresh_pending(self) -> List[OrderResult]:
        """用最新合成价复核挂单，能成交就成交。"""
        filled: List[OrderResult] = []
        with self._lock:
            for oid, order in list(self._state["orders"].items()):
                if order["status"] != "submitted":
                    continue
                quote = self.quote_source.quote(order["symbol"])
                if order["side"] == "buy" and quote.last <= order["price"]:
                    px = self._slip(min(order["price"], quote.last), "buy")
                elif order["side"] == "sell" and quote.last >= order["price"]:
                    px = self._slip(max(order["price"], quote.last), "sell")
                else:
                    continue
                qty = order["qty"]
                notional = px * qty
                comm = self._commission(notional)
                try:
                    self._apply_fill(order["symbol"], order["market"], order["currency"],
                                     order["side"], qty, px, comm)
                except Exception:
                    continue
                order.update(status="filled", filled_qty=qty, avg_fill_price=px, commission=comm)
                filled.append(self._to_result(order))
            if filled:
                self._save()
        return filled

    def health(self) -> Dict[str, Any]:
        with self._lock:
            self._roll_day()
            return {
                "venue": self.venue,
                "connected": True,
                "is_live": False,
                "state_path": str(self.state_path),
                "quote_source": self.quote_source.source_name,
                "synthetic_prices": self.quote_source.source_name == "synthetic",
                "orders": len(self._state["orders"]),
                "positions": sum(1 for p in self._state["positions"].values() if p.get("qty")),
                "cash": dict(self._state["cash"]),
            }

    def reset(self) -> None:
        with self._lock:
            self._state = self._default_state()
            self._save()


class LiveBrokerBlocked(BrokerNotConfigured):
    """当安全门禁拒绝真实券商时抛出。"""
