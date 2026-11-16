"""启动恢复扫描。

原则
----
1. **只承认记录在案的事实**：券商状态文件里的成交、成交账本里的流水。
2. **绝不猜**：不会"大概补一笔"，不会把缺失数量摊平。
3. **宁可停机**：无法判定 → fail-closed，交给人看报告。

两类不一致
----------
``MISSING_IN_LEDGER``（可自动修复）
    券商状态里已有这笔成交（已落盘），但账本没有 —— 典型的"成交写盘后、
    账本追加前"崩溃窗口。这是**已记录的事实**，补入账本不算猜测。

``ORPHAN_IN_LEDGER``（不可自动修复）
    账本里有这笔成交，券商状态里没有。可能是账本写入后崩溃、也可能是状态文件
    被回滚。两种情况都无法确定"钱到底扣没扣"，因此 HALT。

其余：账本与券商都有 → 幂等跳过；都没有 → 无事。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .ledger import Fill, FillLedger, fills_from_orders


@dataclass
class RecoveryAction:
    kind: str                 # backfill_ledger / orphan / ok
    fill_id: str
    detail: str
    auto_fixed: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "fill_id": self.fill_id,
                "detail": self.detail, "auto_fixed": self.auto_fixed}


@dataclass
class RecoveryReport:
    ok: bool = True
    fills_in_ledger: int = 0
    fills_in_broker: int = 0
    backfilled: int = 0
    orphans: int = 0
    actions: List[RecoveryAction] = field(default_factory=list)
    checked_at: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "fills_in_ledger": self.fills_in_ledger,
                "fills_in_broker": self.fills_in_broker,
                "backfilled": self.backfilled, "orphans": self.orphans,
                "checked_at": self.checked_at,
                "actions": [a.as_dict() for a in self.actions]}


def scan(broker: Any, ledger: FillLedger, *,
         allow_backfill: bool = True) -> RecoveryReport:
    """启动时的一致性扫描。必须在**任何**交易之前调用。"""
    rep = RecoveryReport(checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    orders = broker.orders_snapshot() if broker is not None else []
    broker_fills = fills_from_orders(orders)
    rep.fills_in_broker = len(broker_fills)
    rep.fills_in_ledger = len(ledger.fills())

    in_ledger = {f.fill_id for f in ledger.fills()}
    in_broker = {f.fill_id for f in broker_fills}

    # 1) 券商有、账本没有 → 补入（已落盘的事实）
    for f in broker_fills:
        if f.fill_id in in_ledger:
            continue
        if not allow_backfill:
            rep.ok = False
            rep.actions.append(RecoveryAction(
                "missing_in_ledger", f.fill_id,
                f"{f.order_id} 已成交但账本缺失（未允许补账）"))
            continue
        res = ledger.post(f)
        if res.accepted:
            rep.backfilled += 1
            rep.actions.append(RecoveryAction(
                "backfill_ledger", f.fill_id,
                f"{f.order_id} {f.symbol} {f.side} {f.qty:g}@{f.price:g} 补入账本",
                auto_fixed=True))
        else:
            rep.ok = False
            rep.actions.append(RecoveryAction(
                "backfill_failed", f.fill_id, f"补账失败：{res.reason}"))

    # 2) 账本有、券商没有 → 无法判定，停
    for fid in in_ledger - in_broker:
        rep.orphans += 1
        rep.ok = False
        rep.actions.append(RecoveryAction(
            "orphan_in_ledger", fid,
            "账本中存在但券商状态缺失 —— 无法确定资金是否已扣，fail-closed"))

    return rep
