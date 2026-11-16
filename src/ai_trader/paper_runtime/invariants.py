"""账户不变量：每一轮结束后必须成立，不成立就 fail-closed。

为什么单独成模块
----------------
"跑通"和"跑对"是两件事。成交链路能下单不代表账是对的：可能多扣一次手续费、
可能成交写了两遍、可能持仓与流水对不上。这里把**账务恒等式**写成可执行检查，
而不是靠人眼看日志。

六条不变量
----------
1. ``cash_matches_ledger``：现金 == 期初 + 成交流水净额（含手续费）
2. ``positions_match_fills``：持仓数量 == 流水买卖轧差
3. ``no_overfill``：订单累计成交量 ≤ 委托量
4. ``no_duplicate_fill``：``fill_id`` 全局唯一
5. ``short_policy``：卖空关闭时不得出现负持仓
6. ``settlement_consistent``：T+1 账本可用资金 ≤ 券商现金，且非负

严重度
------
``critical`` = 账已经不可信 → 必须 HALT（禁止继续交易）。
``warn`` = 可疑但账仍自洽 → 允许 DEGRADED 并记录。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from .ledger import Fill, FillLedger

TOL = 0.01          # 金额/数量容差（浮点累加）


@dataclass
class InvariantResult:
    name: str
    ok: bool
    detail: str
    severity: str = "critical"
    metrics: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail,
                "severity": self.severity, "metrics": dict(self.metrics)}


@dataclass
class InvariantReport:
    results: List[InvariantResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results if r.severity == "critical")

    @property
    def critical_failures(self) -> List[InvariantResult]:
        return [r for r in self.results if not r.ok and r.severity == "critical"]

    def as_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok,
                "failed": [r.name for r in self.results if not r.ok],
                "results": [r.as_dict() for r in self.results]}


# ---------------------------------------------------------------------- #
def _broker_orders(broker: Any) -> List[Dict[str, Any]]:
    fn = getattr(broker, "orders_snapshot", None)
    if callable(fn):
        return list(fn())
    state = getattr(broker, "_state", {}) or {}
    return list((state.get("orders") or {}).values())


def _broker_cash(broker: Any) -> Dict[str, float]:
    state = getattr(broker, "_state", {}) or {}
    return {str(k): float(v) for k, v in (state.get("cash") or {}).items()}


def _broker_positions(broker: Any) -> Dict[str, float]:
    state = getattr(broker, "_state", {}) or {}
    out: Dict[str, float] = {}
    for sym, pos in (state.get("positions") or {}).items():
        qty = float(pos.get("qty", 0.0) or 0.0)
        if abs(qty) > 1e-9:
            out[str(sym).upper()] = qty
    return out


def _opening_cash(broker: Any) -> Dict[str, float]:
    init = getattr(broker, "initial_cash", None)
    if isinstance(init, dict):
        return {str(k): float(v) for k, v in init.items()}
    state = getattr(broker, "_state", {}) or {}
    return {str(k): float(v) for k, v in (state.get("cash") or {}).items()}


# ---------------------------------------------------------------------- #
def check_all(broker: Any, ledger: FillLedger, *,
              settlement: Any = None,
              short_allowed: bool = False,
              tol: float = TOL) -> InvariantReport:
    rep = InvariantReport()
    fills = ledger.fills()
    orders = _broker_orders(broker)

    # 1) 现金 vs 流水 ------------------------------------------------ #
    cash = _broker_cash(broker)
    opening = _opening_cash(broker)
    delta = ledger.cash_delta()
    bad_cash: List[str] = []
    for cur in sorted(set(cash) | set(delta) | set(opening)):
        expect = float(opening.get(cur, 0.0)) + float(delta.get(cur, 0.0))
        actual = float(cash.get(cur, 0.0))
        if abs(expect - actual) > tol:
            bad_cash.append(f"{cur}: 期望 {expect:.4f} 实际 {actual:.4f} "
                            f"(差 {actual - expect:+.4f})")
    rep.results.append(InvariantResult(
        "cash_matches_ledger", not bad_cash,
        "现金与成交流水一致" if not bad_cash else "; ".join(bad_cash),
        "critical",
        {"cash": cash, "opening": opening, "delta": delta}))

    # 2) 持仓 vs 流水 ------------------------------------------------ #
    pos = _broker_positions(broker)
    by_sym = ledger.qty_by_symbol()
    bad_pos: List[str] = []
    for sym in sorted(set(pos) | set(by_sym)):
        expect = float(by_sym.get(sym, 0.0))
        actual = float(pos.get(sym, 0.0))
        if abs(expect - actual) > 1e-6:
            bad_pos.append(f"{sym}: 期望 {expect:g} 实际 {actual:g}")
    rep.results.append(InvariantResult(
        "positions_match_fills", not bad_pos,
        "持仓与流水一致" if not bad_pos else "; ".join(bad_pos),
        "critical", {"positions": pos, "from_fills": by_sym}))

    # 3) 不得超量成交 ------------------------------------------------ #
    over: List[str] = []
    for o in orders:
        qty = float(o.get("qty") or 0.0)
        filled = float(o.get("filled_qty") or 0.0)
        if filled > qty + 1e-6:
            over.append(f"{o.get('order_id')} 成交 {filled:g} > 委托 {qty:g}")
    rep.results.append(InvariantResult(
        "no_overfill", not over, "无超量成交" if not over else "; ".join(over),
        "critical", {"orders": len(orders)}))

    # 4) 成交 ID 唯一 ------------------------------------------------ #
    seen: Dict[str, int] = {}
    for f in fills:
        seen[f.fill_id] = seen.get(f.fill_id, 0) + 1
    dups = [fid for fid, n in seen.items() if n > 1]
    # 订单侧也要唯一：同一 fill_id 挂在两张不同订单上同样是错
    order_fill: Dict[str, str] = {}
    cross: List[str] = []
    for o in orders:
        fid = str(o.get("fill_id") or "")
        if not fid:
            continue
        oid = str(o.get("order_id") or "")
        if fid in order_fill and order_fill[fid] != oid:
            cross.append(f"{fid} 同时属于 {order_fill[fid]} 与 {oid}")
        order_fill[fid] = oid
    rep.results.append(InvariantResult(
        "no_duplicate_fill", not dups and not cross,
        "成交 ID 唯一" if not dups and not cross
        else "; ".join([f"重复 {d}" for d in dups] + cross),
        "critical", {"fills": len(fills), "unique": len(seen)}))

    # 5) 卖空政策 ---------------------------------------------------- #
    shorts = {s: q for s, q in pos.items() if q < -1e-9}
    ok_short = bool(short_allowed) or not shorts
    rep.results.append(InvariantResult(
        "short_policy", ok_short,
        "无未授权卖空" if ok_short else
        "卖空已关闭但存在负持仓：" + ", ".join(f"{s}={q:g}" for s, q in shorts.items()),
        "critical", {"shorts": shorts, "short_allowed": bool(short_allowed)}))

    # 6) 结算账本 vs 券商现金 ---------------------------------------- #
    if settlement is None:
        rep.results.append(InvariantResult(
            "settlement_consistent", True, "未接入结算账本（跳过）", "warn"))
    else:
        problems: List[str] = []
        for cur in sorted(cash):
            try:
                avail = float(settlement.available() if callable(
                    getattr(settlement, "available", None)) else 0.0)
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{cur} 可用资金不可读：{exc}")
                continue
            if avail < -tol:
                problems.append(f"{cur} 可用资金为负：{avail:.4f}")
            if avail > float(cash.get(cur, 0.0)) + tol:
                problems.append(f"{cur} 可用 {avail:.4f} > 现金 {cash.get(cur, 0.0):.4f}")
        rep.results.append(InvariantResult(
            "settlement_consistent", not problems,
            "结算可用资金自洽" if not problems else "; ".join(problems),
            "critical" if problems else "warn"))

    return rep
