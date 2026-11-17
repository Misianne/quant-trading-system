"""日线波段回测引擎（$300 本金 / $1 买入 + $1 卖出 / 含滑点）。

执行口径（关键，不许含糊）
--------------------------
* **决策在 D 日收盘后**产生，只用 ``trade_date <= D`` 的 bar；
* **QQQ/QLD 的买卖在 D+1 开盘**成交（``MOO``）——绝不用 D 日收盘价假装成交；
* **QID 的退出**按既定规则是"收盘前必须完成"，因此用当日收盘成交（``MOC``）：
  止盈、趋势失效、第二交易日强制退出，三类都在收盘判定并收盘成交；
* 手续费 **每边 1 美元**（与金额无关），滑点按 ``slippage_bps`` 单边扣除；
* 现金不足则订单被拒（``REJECTED``），下一交易日数据缺失则 ``UNFILLED``；
* 同一交易日**只允许产生一次决策**（``DuplicateDecision``）。

不使用未来数据：成交价取 D+1 的 ``open``；若 D+1 不存在（回测结束）则该订单
保持 ``UNFILLED`` 并计入统计，不允许"按最后一天收盘价收尾"。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import daily_swing as ds

FEE_PER_SIDE = 1.0
SLIPPAGE_BPS = 5.0

FILLED = "FILLED"
REJECTED = "REJECTED"
UNFILLED = "UNFILLED"


class BacktestError(RuntimeError):
    pass


class DuplicateDecision(BacktestError):
    pass


@dataclass
class Order:
    order_id: int
    symbol: str
    side: str                 # buy / sell
    qty: float
    created_on: str           # 决策日
    order_type: str           # MOO / MOC
    reason: str
    regime: str
    status: str = "PENDING"
    fill_date: str = ""
    fill_price: float = 0.0
    fee: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {"order_id": self.order_id, "symbol": self.symbol, "side": self.side,
                "qty": round(self.qty, 6), "created_on": self.created_on,
                "order_type": self.order_type, "reason": self.reason,
                "regime": self.regime, "status": self.status,
                "fill_date": self.fill_date,
                "fill_price": round(self.fill_price, 4), "fee": self.fee}


@dataclass
class Trade:
    symbol: str
    entry_date: str
    entry_price: float
    qty: float
    exit_date: str = ""
    exit_price: float = 0.0
    buy_fee: float = 0.0
    sell_fee: float = 0.0
    regime: str = ""
    entry_reason: str = ""
    exit_reason: str = ""
    gross_pnl: float = 0.0
    net_pnl: float = 0.0
    sessions: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {"symbol": self.symbol, "entry_date": self.entry_date,
                "entry_price": round(self.entry_price, 4), "qty": round(self.qty, 6),
                "exit_date": self.exit_date, "exit_price": round(self.exit_price, 4),
                "buy_fee": round(self.buy_fee, 4), "sell_fee": round(self.sell_fee, 4),
                "regime": self.regime, "entry_reason": self.entry_reason,
                "exit_reason": self.exit_reason,
                "gross_pnl": round(self.gross_pnl, 4),
                "net_pnl": round(self.net_pnl, 4), "sessions": self.sessions}


@dataclass
class BacktestResult:
    initial_cash: float
    final_equity: float
    equity_curve: List[Dict[str, Any]]
    trades: List[Trade]
    orders: List[Order]
    decisions: List[Dict[str, Any]]
    metrics: Dict[str, Any]
    sessions: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {"initial_cash": self.initial_cash,
                "final_equity": round(self.final_equity, 2),
                "sessions": self.sessions,
                "metrics": self.metrics,
                "trades": [t.as_dict() for t in self.trades],
                "orders": [o.as_dict() for o in self.orders],
                "decisions": self.decisions,
                "equity_curve": self.equity_curve}


def _slip(price: float, side: str, bps: float) -> float:
    adj = price * bps / 10_000.0
    return price + adj if side == "buy" else max(0.01, price - adj)


def _round_qty(qty: float, fractional: bool) -> float:
    return qty if fractional else float(int(qty))


class DailyBacktester:
    def __init__(self, bars: Dict[str, List[Dict[str, Any]]], *,
                 initial_cash: float = 300.0,
                 fee_per_side: float = FEE_PER_SIDE,
                 slippage_bps: float = SLIPPAGE_BPS,
                 fractional: bool = True,
                 allow_qid: bool = True,
                 allow_qld: bool = True,
                 allow_tqqq: bool = False,
                 strategy_version: str = ds.STRATEGY_VERSION):
        self.bars = {k: list(v) for k, v in bars.items()}
        self.initial_cash = float(initial_cash)
        self.fee_per_side = float(fee_per_side)
        self.slippage_bps = float(slippage_bps)
        self.fractional = bool(fractional)
        self.allow_qid = bool(allow_qid)
        self.allow_qld = bool(allow_qld)
        self.allow_tqqq = bool(allow_tqqq)
        self.strategy_version = strategy_version

    # ------------------------------------------------------------------ #
    def _calendar(self) -> List[str]:
        """交易日索引 = 所有标的日期的并集（QQQ 为主，缺失则整体报错）。"""
        qqq = self.bars.get(ds.SYM_QQQ) or []
        if not qqq:
            raise BacktestError("缺少 QQQ 日线，无法决策")
        return [str(b["trade_date"]) for b in qqq]

    def _bar_on(self, symbol: str, day: str) -> Optional[Dict[str, Any]]:
        for b in self.bars.get(symbol) or []:
            if str(b["trade_date"]) == day:
                return b
        return None

    def _bars_until(self, symbol: str, day: str) -> List[Dict[str, Any]]:
        return [b for b in (self.bars.get(symbol) or []) if str(b["trade_date"]) <= day]

    def _next_day(self, day: str) -> Optional[str]:
        cal = self._calendar()
        if day not in cal:
            return None
        i = cal.index(day)
        return cal[i + 1] if i + 1 < len(cal) else None

    # ------------------------------------------------------------------ #
    def run(self, start: Optional[str] = None) -> BacktestResult:
        cal = self._calendar()
        qqq = self.bars[ds.SYM_QQQ]
        n = len(qqq)

        # ---- 预计算每个交易日的状态与连续天数（避免循环里重复算 200 根均线）----
        regimes: List[Optional[str]] = [None] * n
        for i in range(ds.MIN_HISTORY - 1, n):
            try:
                regimes[i] = ds.classify(ds.snapshot(qqq[:i + 1]))[0]
            except ds.DailyStrategyError:
                regimes[i] = None
        streaks: List[int] = [0] * n
        for i in range(1, n):
            streaks[i] = (streaks[i - 1] + 1) if (regimes[i] and regimes[i] == regimes[i - 1]) else 1

        first_ok = max(0, ds.MIN_HISTORY - 1)
        if start:
            idx = [i for i, b in enumerate(qqq) if str(b["trade_date"]) >= start]
            if not idx:
                raise BacktestError(f"start={start} 超出数据范围")
            first_ok = max(first_ok, idx[0])

        cash = self.initial_cash
        pos: Optional[ds.QidPosition] = None
        holder: Optional[str] = None
        pending: Optional[Order] = None
        seen_days: set = set()
        orders: List[Order] = []
        trades: List[Trade] = []
        decisions: List[Dict[str, Any]] = []
        curve: List[Dict[str, Any]] = []
        open_trade: Optional[Trade] = None
        open_idx: Optional[int] = None
        last_exit_idx: Dict[str, int] = {}
        taken_profit_day: Optional[str] = None
        oid = 0

        def close_of(symbol: str, day: str) -> Optional[float]:
            """取该标的当日收盘；没有当日 bar 就返回 None（绝不拿旧价顶替）。"""
            b = self._bar_on(symbol, day)
            return float(b["close"]) if b else None

        for i in range(first_ok, n):
            day = str(qqq[i]["trade_date"])

            # ---------- 1) 执行昨日挂单（MOO = 今日开盘成交）----------
            if pending is not None and pending.status == "PENDING":
                bar = self._bar_on(pending.symbol, day) if pending.order_type == "MOO" else None
                if pending.order_type != "MOO":
                    pending = None
                elif bar is None:
                    pending.status = UNFILLED
                    pending.reason += " | 次日缺该标的 bar"
                    pending = None
                else:
                    px = _slip(float(bar["open"]), pending.side, self.slippage_bps)
                    if pending.side == "buy":
                        need = px * pending.qty + self.fee_per_side
                        if need > cash + 1e-9:
                            pending.status = REJECTED
                            pending.reason += f" | 现金不足 need={need:.2f} cash={cash:.2f}"
                        else:
                            cash -= need
                            pending.status, pending.fill_date = FILLED, day
                            pending.fill_price, pending.fee = px, self.fee_per_side
                            open_trade = Trade(symbol=pending.symbol, entry_date=day,
                                               entry_price=px, qty=pending.qty,
                                               buy_fee=self.fee_per_side,
                                               regime=pending.regime,
                                               entry_reason=pending.reason)
                            open_idx = i
                            holder = pending.symbol
                            pos = ds.QidPosition(symbol=pending.symbol, qty=pending.qty,
                                                 avg_price=px, entry_date=day)
                    else:
                        cash += px * pending.qty - self.fee_per_side
                        pending.status, pending.fill_date = FILLED, day
                        pending.fill_price, pending.fee = px, self.fee_per_side
                        if open_trade is not None:
                            open_trade.exit_date, open_trade.exit_price = day, px
                            open_trade.sell_fee = self.fee_per_side
                            open_trade.gross_pnl = (px - open_trade.entry_price) * open_trade.qty
                            open_trade.net_pnl = (open_trade.gross_pnl - open_trade.buy_fee
                                                  - open_trade.sell_fee)
                            open_trade.exit_reason = pending.reason
                            open_trade.sessions = (i - open_idx) if open_idx is not None else 0
                            trades.append(open_trade)
                            last_exit_idx[open_trade.symbol] = i
                            open_trade, open_idx = None, None
                        holder, pos = None, None
                    pending = None

            # ---------- 2) 重复决策保护 ----------
            if day in seen_days:
                raise DuplicateDecision(f"{day} 已产生过决策")
            seen_days.add(day)

            hist = qqq[:i + 1]
            if regimes[i] is None:
                curve.append({"date": day, "equity": round(cash, 2), "cash": round(cash, 2),
                              "holder": holder, "note": "history_short"})
                continue

            # ---------- 3) QID 持仓：收盘判定 + 收盘成交 ----------
            if holder == ds.SYM_QID and pos is not None:
                pos.sessions_held += 1
                qid_hist = self._bars_until(ds.SYM_QID, day)
                if not qid_hist or str(qid_hist[-1]["trade_date"]) != day:
                    raise BacktestError(f"{day} 缺 QID 当日 bar，禁止用旧价格判退出")
                why = ds.evaluate_exit(pos, qid_hist, day,
                                       rebounded=(regimes[i] != ds.DOWNTREND))
                if why:
                    px = _slip(float(qid_hist[-1]["close"]), "sell", self.slippage_bps)
                    cash += px * pos.qty - self.fee_per_side
                    oid += 1
                    orders.append(Order(oid, ds.SYM_QID, "sell", pos.qty, day, "MOC",
                                        why, regimes[i] or "", status=FILLED, fill_date=day,
                                        fill_price=px, fee=self.fee_per_side))
                    if open_trade is not None:
                        open_trade.exit_date, open_trade.exit_price = day, px
                        open_trade.sell_fee = self.fee_per_side
                        open_trade.gross_pnl = (px - open_trade.entry_price) * open_trade.qty
                        open_trade.net_pnl = (open_trade.gross_pnl - open_trade.buy_fee
                                              - open_trade.sell_fee)
                        open_trade.exit_reason = why
                        open_trade.sessions = (i - open_idx) if open_idx is not None else 0
                        trades.append(open_trade)
                        last_exit_idx[ds.SYM_QID] = i
                        open_trade, open_idx = None, None
                    if "止盈" in why:
                        taken_profit_day = day
                    holder, pos = None, None
                    decisions.append({"decide_on": day, "action": "sell", "symbol": ds.SYM_QID,
                                      "reason": why, "regime": regimes[i],
                                      "regime_streak": streaks[i],
                                      "data_as_of": day,
                                      "strategy_version": self.strategy_version,
                                      "order_type": "MOC"})
                closed = close_of(holder, day) if holder else None
                equity = cash + (pos.qty * closed if (pos is not None and closed) else 0.0)
                curve.append({"date": day, "equity": round(equity, 2),
                              "cash": round(cash, 2), "holder": holder})
                continue

            if pos is not None:
                pos.sessions_held += 1

            # ---------- 4) 常规决策（收盘后产生，次日开盘执行）----------
            can_open = (taken_profit_day != day)
            target_guess = ds.target_for_regime(regimes[i] or ds.CHOP, allow_qld=self.allow_qld)[0]
            since_exit = (i - last_exit_idx[target_guess]) if target_guess in last_exit_idx else None
            d = ds.decide(hist, day, holding=holder, allow_tqqq=self.allow_tqqq,
                          allow_qid=self.allow_qid, regime_streak=streaks[i],
                          sessions_since_exit=since_exit, allow_qld=self.allow_qld)
            decisions.append(d.as_dict())

            if d.action == ds.BUY and can_open:
                nd = self._next_day(day)
                bar_next = self._bar_on(d.symbol, nd) if nd else None
                px_ref = float(bar_next["open"]) if bar_next else float(hist[-1]["close"])
                px = _slip(px_ref, "buy", self.slippage_bps)
                qty = _round_qty((cash * d.target_pct - self.fee_per_side) / px, self.fractional)
                oid += 1
                if qty <= 0:
                    orders.append(Order(oid, d.symbol, "buy", 0.0, day, "MOO",
                                        d.reason + " | 资金不足一手", d.regime, status=REJECTED))
                else:
                    orders.append(Order(oid, d.symbol, "buy", qty, day, "MOO", d.reason, d.regime))
                    pending = orders[-1]
            elif d.action == ds.SELL and holder is not None:
                oid += 1
                orders.append(Order(oid, holder, "sell", pos.qty if pos else 0.0, day, "MOO",
                                    d.reason, d.regime))
                pending = orders[-1]

            mark = close_of(holder, day) if holder else None
            equity = cash + (pos.qty * mark if (pos is not None and mark) else 0.0)
            if holder and mark is None:
                raise BacktestError(f"{day} 缺 {holder} 当日 bar，禁止用旧价估值")
            curve.append({"date": day, "equity": round(equity, 2),
                          "cash": round(cash, 2), "holder": holder})

        final_equity = curve[-1]["equity"] if curve else self.initial_cash
        metrics = _metrics(self.initial_cash, final_equity, curve, trades, orders,
                           open_trade, self.bars)
        return BacktestResult(initial_cash=self.initial_cash, final_equity=final_equity,
                              equity_curve=curve, trades=trades, orders=orders,
                              decisions=decisions, metrics=metrics, sessions=len(curve))

def _metrics(initial: float, final: float, curve: List[Dict[str, Any]],
             trades: List[Trade], orders: List[Order],
             open_trade: Optional[Trade], bars: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    equities = [float(c["equity"]) for c in curve] or [initial]
    peak, mdd = -1e18, 0.0
    for e in equities:
        peak = max(peak, e)
        if peak > 0:
            mdd = min(mdd, e / peak - 1.0)

    wins = [t for t in trades if t.net_pnl > 0]
    losses = [t for t in trades if t.net_pnl <= 0]
    gp = sum(t.net_pnl for t in wins)
    gl = -sum(t.net_pnl for t in losses)
    n = len(trades)
    cash_days = sum(1 for c in curve if c.get("holder") in (None, ""))
    by_regime: Dict[str, Dict[str, Any]] = {}
    for t in trades:
        r = by_regime.setdefault(t.regime or "UNKNOWN",
                                 {"trades": 0, "net": 0.0, "wins": 0})
        r["trades"] += 1
        r["net"] += t.net_pnl
        if t.net_pnl > 0:
            r["wins"] += 1
    for r in by_regime.values():
        r["net"] = round(r["net"], 2)
        r["win_rate"] = round(r["wins"] / r["trades"], 4) if r["trades"] else 0.0

    # 基准：同期 QQQ 买入持有（不含费用）
    qqq = bars.get(ds.SYM_QQQ) or []
    bench = 0.0
    bench_mdd = 0.0
    if curve and qqq:
        start = curve[0]["date"]
        end = curve[-1]["date"]
        s = [b for b in qqq if str(b["trade_date"]) >= start]
        e = [b for b in qqq if str(b["trade_date"]) <= end]
        if s and e and float(s[0]["open"]) > 0:
            bench = float(e[-1]["close"]) / float(s[0]["open"]) - 1.0
        peak = -1e18
        for b in s:
            px = float(b["close"])
            peak = max(peak, px)
            if peak > 0:
                bench_mdd = min(bench_mdd, px / peak - 1.0)

    return {
        "net_return": round(final / initial - 1.0, 6) if initial else 0.0,
        "final_equity": round(final, 2),
        "max_drawdown": round(mdd, 6),
        "trades": n,
        "orders": len(orders),
        "orders_rejected": sum(1 for o in orders if o.status == REJECTED),
        "orders_unfilled": sum(1 for o in orders if o.status == UNFILLED),
        "win_rate": round(len(wins) / n, 4) if n else 0.0,
        "profit_factor": round(gp / gl, 4) if gl > 0 else (None if gp == 0 else float("inf")),
        "expectancy_per_trade": round(sum(t.net_pnl for t in trades) / n, 4) if n else 0.0,
        "cash_time_ratio": round(cash_days / len(curve), 4) if curve else 0.0,
        "fees_paid": round(sum(t.buy_fee + t.sell_fee for t in trades), 2),
        "by_regime": by_regime,
        "benchmark_qqq": round(bench, 6),
        "benchmark_max_drawdown": round(bench_mdd, 6),
        "open_position_at_end": open_trade.as_dict() if open_trade else None,
    }
