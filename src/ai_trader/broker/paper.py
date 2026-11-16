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
import os
import random
import threading
import uuid
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import timebase
from ..paper_runtime import fault_inject
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
        start = timebase.now() - timedelta(days=num)
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
        allow_short: bool = False,
        settlement: Any = None,
        market_status: Any = None,
        deterministic_ids: bool = False,
    ):
        self.state_path = Path(state_path)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.initial_cash = dict(initial_cash or {"HKD": 1_000_000.0, "USD": 130_000.0, "CNY": 700_000.0})
        self.commission_bps = float(commission_bps)
        self.min_commission = float(min_commission)
        self.slippage_bps = float(slippage_bps)
        self.allow_partial_fill = allow_partial_fill
        #: US-P2-D：模拟盘是否允许卖空。默认 False，保持"禁止裸卖空"的安全语义。
        self.allow_short = bool(allow_short)
        #: US-P2-E：可选的 T+1 结算账本。成交后自动入账，形成"撮合 → 账本"闭环。
        self.settlement = settlement
        #: US-P2-C 阶段 C：市场状态。暂停期间**不得产生成交**。
        self.market_status = market_status
        self.quote_source = quote_source or SyntheticQuoteSource()
        #: 回放模式：订单号用"种子 + 递增序号"而不是 uuid，
        #: 这样同一场景重复运行的成交/订单/账本指纹完全一致。
        self.deterministic_ids = bool(deterministic_ids)
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
            "day": timebase.now().date().isoformat(),
            "order_seq": 0,
        }

    def _load(self) -> Dict[str, Any]:
        if self.state_path.exists():
            try:
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                base = self._default_state()
                base.update(data)
                return base
            except (ValueError, OSError) as exc:
                # **绝不**在状态文件损坏时回落到默认账户 —— 那等于把余额清零。
                # fail-closed：拒绝启动，保留现场供人工核对/从备份恢复。
                bad = self.state_path.with_name(
                    self.state_path.name + ".corrupt")
                try:
                    if not bad.exists():
                        self.state_path.replace(bad)
                except OSError:
                    pass
                raise RuntimeError(
                    f"账户状态文件损坏，拒绝启动（已隔离为 {bad.name}）："
                    f"{type(exc).__name__}: {exc}") from exc
        st = self._default_state()
        self.state_path.write_text(json.dumps(st, indent=2, ensure_ascii=False), encoding="utf-8")
        return st

    def _save(self) -> None:
        """原子落盘：临时文件 → fsync → ``os.replace``。

        为什么不能直接 ``write_text``：写到一半崩溃会留下**截断的 JSON**，
        下次启动 ``_load`` 解析失败后走 ``_default_state`` —— 账户直接"归零"，
        这是最危险的一种崩溃恢复行为（比拒绝启动还坏）。
        """
        tmp = self.state_path.with_name(self.state_path.name + ".tmp")
        payload = json.dumps(self._state, indent=2, ensure_ascii=False, default=str)
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.state_path)
        # 目录项也要落盘，否则某些文件系统上 rename 可能丢失
        try:
            dir_fd = os.open(str(self.state_path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except (OSError, AttributeError):   # Windows 不支持目录 fsync
            pass

    def _new_order_id(self) -> str:
        """生成订单号。回放模式确定性，生产模式保持 uuid 随机。"""
        if not self.deterministic_ids:
            return "P" + uuid.uuid4().hex[:14]
        seq = int(self._state.get("order_seq", 0)) + 1
        self._state["order_seq"] = seq
        return f"PR{seq:08d}"

    def _roll_day(self, quotes: Optional[Dict[str, Quote]] = None) -> None:
        """跨日重置日内基准（用于单日亏损统计）。"""
        today = timebase.now().date().isoformat()
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

            fault_inject.check("before_order")
            sym = req.symbol
            market = req.market or market_of(sym)
            currency = req.currency or currency_of(sym)
            qty = float(req.qty)
            quote = self.quote_source.quote(sym)
            ref_price = float(req.price) if req.price is not None else quote.last
            order_id = self._new_order_id()

            # ---- US-P2-B：订单类型 / TIF（统一枚举，禁止静默降级）----
            from .orders import OrderType, TimeInForce, apply_tif, parse_order_type, parse_tif
            otype_enum = parse_order_type(req.order_type)
            tif_enum = parse_tif(req.tif)
            otype = otype_enum.value

            exec_price: Optional[float] = None
            fill_qty = 0.0
            status = "submitted"
            triggered = True

            if otype_enum in (OrderType.STOP, OrderType.STOP_LIMIT):
                trigger = getattr(req, "stop_price", None)
                if trigger is None:
                    trigger = req.stop_loss
                if trigger is None or float(trigger) <= 0:
                    raise OrderRejected(f"{otype} 必须提供触发价 stop_price",
                                        code="STOP_PRICE_REQUIRED")
                triggered = (quote.last >= float(trigger)) if req.side == "buy" \
                    else (quote.last <= float(trigger))

            if not triggered:
                # 未触发 → 挂单，不成交（不是"静默当市价单"）
                status = "submitted"
            elif otype_enum is OrderType.STOP_LIMIT:
                if req.price is None:
                    raise OrderRejected("stop_limit 必须提供 price",
                                        code="LIMIT_PRICE_REQUIRED")
                limit = float(req.price)
                crossed = (quote.last <= limit) if req.side == "buy" else (quote.last >= limit)
                if crossed:
                    raw = min(limit, quote.last) if req.side == "buy" else max(limit, quote.last)
                    exec_price = self._slip(raw, req.side)
                    fill_qty, status = qty, "filled"
            elif otype_enum is OrderType.STOP:
                # 触发后按市价成交
                exec_price = self._slip(quote.last, req.side)
                fill_qty, status = qty, "filled"
            elif otype_enum is OrderType.MARKET:
                exec_price = self._slip(quote.last, req.side)
                fill_qty, status = qty, "filled"
            else:  # LIMIT
                if req.price is None:
                    raise OrderRejected("限价单必须提供 price")
                crossed = (quote.last <= ref_price) if req.side == "buy" else (quote.last >= ref_price)
                if crossed:
                    raw = min(ref_price, quote.last) if req.side == "buy" else max(ref_price, quote.last)
                    exec_price = self._slip(raw, req.side)
                    fill_qty = qty
                    status = "filled"

            # TIF 语义：IOC 立即撤销剩余；FOK 不能全成则整单拒绝；DAY/GTC 剩余挂单
            tif_out = apply_tif(tif_enum, qty, fill_qty)
            if tif_out.reject_all:
                raise OrderRejected(tif_out.note or "FOK 无法全部成交",
                                    code="FOK_UNFILLABLE")
            if tif_out.cancel_remainder and tif_out.fill_qty < qty:
                # IOC/OPG/CLS：部分成交 + 剩余撤销；完全未成交则整单取消
                if tif_out.fill_qty <= 0:
                    status = "cancelled"
                fill_qty = tif_out.fill_qty

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
                if held < qty and not getattr(self, "allow_short", False):
                    raise OrderRejected(
                        f"持仓不足：{sym} 持有 {held}，卖出 {qty}", code="INSUFFICIENT_POSITION"
                    )
                if held < qty and getattr(self, "allow_short", False):
                    # US-P2-D：模拟盘卖空。真实盘由 ShortGuard + 人工批准另行把关。
                    self._record_short_borrow(sym, qty - held, currency)

            # ---- US-P2-C 阶段 C：成交闸门 ----
            # 市场暂停期间**不得伪造成交**；订单保留为 submitted，等恢复后再处理。
            market_note = ""
            if status == "filled" and self.market_status is not None:
                try:
                    # 传入**成交价**：Limit State 期间只有带内价格才允许成交
                    _ok, _why, _mstate = self.market_status.fill_allowed(
                        sym, price=float(exec_price or ref_price))
                except Exception as exc:      # 状态不可查 → 同样不成交（fail-closed）
                    _ok, _why, _mstate = False, f"市场状态不可查：{exc}", None
                if not _ok:
                    status = "submitted"
                    fill_qty = 0.0
                    exec_price = None
                    market_note = _why

            if status == "filled":
                fault_inject.check("before_fill_write")
                self._apply_fill(sym, market, currency, req.side, fill_qty, exec_price, commission)
                fault_inject.check("after_position")
                # US-P2-E：成交即入账（买卖都记），账本按交易日历推进 T+1。
                if self.settlement is not None and fill_qty > 0 and exec_price:
                    try:
                        self.settlement.record_trade(
                            trade_date=timebase.now().date(), side=req.side, symbol=sym,
                            qty=fill_qty, price=float(exec_price), fee=float(commission),
                            note=f"paper:{order_id}",
                        )
                        fault_inject.check("after_settlement")
                    except Exception as exc:      # 账本故障不得吞掉成交，但必须留痕
                        self._state.setdefault("settlement_errors", []).append(
                            {"order_id": order_id, "error": str(exc)})

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
                "fill_id": (f"F{order_id}-1" if fill_qty > 0 else ""),
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
                "market_note": market_note,
            }
            self._state["orders"][order_id] = order
            self._save()
            if status == "filled":
                fault_inject.check("after_fill_write")
            else:
                fault_inject.check("after_order_create")
            return self._to_result(order)

    def _record_short_borrow(self, sym: str, qty: float, currency: str) -> None:
        """在模拟盘登记一笔借券占用（供后续回补/风控查看）。"""
        book = self._state.setdefault("short_borrow", {})
        item = book.setdefault(sym, {"qty": 0.0, "currency": currency})
        item["qty"] = round(float(item.get("qty", 0.0)) + float(qty), 6)

    def _apply_fill(self, sym: str, market: str, currency: str, side: str,
                    qty: float, price: float, commission: float) -> None:
        cash = float(self._state["cash"].get(currency, 0.0))
        pos = self._state["positions"].setdefault(
            sym, {"qty": 0.0, "avg_cost": 0.0, "market": market, "currency": currency, "name": ""}
        )
        held = float(pos["qty"])
        if side == "buy":
            if held < 0:
                # 买回平空：已实现盈亏按开仓均价与买回价之差计算
                cover_qty = min(qty, -held)
                realized = (float(pos["avg_cost"]) - price) * cover_qty - commission
                rp = self._state.setdefault("realized_pnl", {})
                rp[currency] = round(float(rp.get(currency, 0.0)) + realized, 4)
                remaining = qty - cover_qty
                pos["qty"] = round(held + cover_qty, 6)
                if abs(pos["qty"]) < 1e-9:
                    pos["qty"] = 0.0
                    pos["avg_cost"] = 0.0
                if remaining > 0:
                    pos["avg_cost"] = price
                    pos["qty"] = round(pos["qty"] + remaining, 6)
            else:
                new_qty = held + qty
                prev_cost = float(pos["avg_cost"]) * held
                pos["avg_cost"] = round((prev_cost + price * qty + commission) / new_qty, 6)
                pos["qty"] = new_qty
            cash -= price * qty + commission
        else:
            if held > 0:
                close_qty = min(qty, held)
                realized = (price - float(pos["avg_cost"])) * close_qty - commission
                rp = self._state.setdefault("realized_pnl", {})
                rp[currency] = round(float(rp.get(currency, 0.0)) + realized, 4)
                short_qty = qty - close_qty
                pos["qty"] = round(held - close_qty, 6)
                if short_qty > 0:
                    # 反手做空：以本次卖出价作为空头开仓均价
                    pos["avg_cost"] = price
                    pos["qty"] = round(pos["qty"] - short_qty, 6)
                elif abs(pos["qty"]) < 1e-9:
                    pos["qty"] = 0.0
                    pos["avg_cost"] = 0.0
            else:
                # 已有空头：加空，按加权均价
                total = -held + qty
                pos["avg_cost"] = round((float(pos["avg_cost"]) * (-held) + price * qty) / total, 6)
                pos["qty"] = round(held - qty, 6)
                rp = self._state.setdefault("realized_pnl", {})
                rp[currency] = round(float(rp.get(currency, 0.0)) - commission, 4)
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

    def orders_snapshot(self) -> List[Dict[str, Any]]:
        """订单记录的快照（深拷贝）。

        给对账/不变量检查用：调用方拿到的是副本，改它不会污染券商状态。
        """
        with self._lock:
            return [json.loads(json.dumps(o, default=str))
                    for o in self._state.get("orders", {}).values()]

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
