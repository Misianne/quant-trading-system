"""成交账本：唯一 ID、幂等入账、崩溃后可重建。

为什么不能只靠 PaperBroker 的 JSON 状态
---------------------------------------
Broker 的 ``paper_state.json`` 是"最终快照"，崩溃时只能告诉我们最后一次成功
落盘的样子。要回答"这笔成交到底有没有入过账"，必须有一份**只追加**的流水：

* 每笔成交写入即 ``fsync``，不依赖进程正常退出；
* 主键 ``fill_id`` 唯一，重复投递直接判定 ``duplicate``，不重复改账；
* 启动时逐行重放，**只**承认完整行（末尾被截断的半行会被隔离并报告，
  绝不猜它"大概是哪笔成交"）。

不做什么
--------
不做"补造缺失成交"。账本与 Broker 快照不一致时，一律 fail-closed 交给上层
HALT，由人看报告决定，而不是自动猜一个数字补上。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("ai_trader.paper.ledger")


class LedgerError(RuntimeError):
    pass


@dataclass(frozen=True)
class Fill:
    fill_id: str
    order_id: str
    symbol: str
    side: str
    qty: float
    price: float
    fee: float
    currency: str
    ts: str
    source: str = "broker"

    @property
    def notional(self) -> float:
        return self.qty * self.price

    @property
    def cash_delta(self) -> float:
        """买 → 负（付钱）；卖 → 正（收钱）。手续费恒为负。"""
        gross = -self.notional if self.side == "buy" else self.notional
        return gross - self.fee

    def as_dict(self) -> Dict[str, Any]:
        return {"fill_id": self.fill_id, "order_id": self.order_id,
                "symbol": self.symbol, "side": self.side, "qty": self.qty,
                "price": self.price, "fee": self.fee, "currency": self.currency,
                "ts": self.ts, "source": self.source}


@dataclass
class PostResult:
    accepted: bool
    duplicate: bool = False
    reason: str = ""
    fill: Optional[Fill] = None

    def as_dict(self) -> Dict[str, Any]:
        return {"accepted": self.accepted, "duplicate": self.duplicate,
                "reason": self.reason,
                "fill_id": self.fill.fill_id if self.fill else ""}


@dataclass
class LoadReport:
    ok: bool = True
    fills: int = 0
    duplicates: int = 0
    torn_lines: int = 0
    errors: List[str] = field(default_factory=list)
    replayed_cash: Dict[str, float] = field(default_factory=dict)
    replayed_qty: Dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "fills": self.fills, "duplicates": self.duplicates,
                "torn_lines": self.torn_lines, "errors": list(self.errors),
                "replayed_cash": dict(self.replayed_cash),
                "replayed_qty": dict(self.replayed_qty)}


class FillLedger:
    """只追加的成交流水（JSONL + fsync）。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._fills: Dict[str, Fill] = {}
        self._order_index: Dict[str, List[str]] = {}
        self._lock = threading.RLock()
        self._write_errors = 0
        self._torn = 0

    # ------------------------------------------------------------------ #
    def load(self) -> LoadReport:
        rep = LoadReport()
        with self._lock:
            self._fills.clear()
            self._order_index.clear()
            if not self.path.exists():
                return rep
            raw = self.path.read_bytes()
            if not raw:
                return rep
            lines = raw.split(b"\n")
            # 末尾若是空串说明最后一行有换行结尾，正常
            for i, line in enumerate(lines):
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line.decode("utf-8"))
                except (ValueError, UnicodeDecodeError) as exc:
                    # 只容忍"最后一行"被截断；中间损坏一律视为账本不可信
                    is_tail = (i == len(lines) - 1) or all(
                        not x.strip() for x in lines[i + 1:])
                    if is_tail:
                        self._torn += 1
                        rep.torn_lines += 1
                        rep.errors.append(f"末尾不完整行已隔离（{len(line)} 字节）")
                        continue
                    rep.ok = False
                    rep.errors.append(f"第 {i + 1} 行损坏：{exc}")
                    continue
                try:
                    f = Fill(fill_id=str(obj["fill_id"]), order_id=str(obj["order_id"]),
                             symbol=str(obj["symbol"]).upper(), side=str(obj["side"]),
                             qty=float(obj["qty"]), price=float(obj["price"]),
                             fee=float(obj.get("fee", 0.0)),
                             currency=str(obj.get("currency", "USD")),
                             ts=str(obj.get("ts", "")),
                             source=str(obj.get("source", "broker")))
                except (KeyError, TypeError, ValueError) as exc:
                    rep.ok = False
                    rep.errors.append(f"第 {i + 1} 行字段缺失：{exc}")
                    continue
                if f.fill_id in self._fills:
                    rep.duplicates += 1
                    continue
                self._fills[f.fill_id] = f
                self._order_index.setdefault(f.order_id, []).append(f.fill_id)
            rep.fills = len(self._fills)
            for f in self._fills.values():
                rep.replayed_cash[f.currency] = round(
                    rep.replayed_cash.get(f.currency, 0.0) + f.cash_delta, 8)
                signed = f.qty if f.side == "buy" else -f.qty
                rep.replayed_qty[f.symbol] = round(
                    rep.replayed_qty.get(f.symbol, 0.0) + signed, 8)
        return rep

    # ------------------------------------------------------------------ #
    def post(self, fill: Fill) -> PostResult:
        """幂等入账。同一 ``fill_id`` 第二次投递 → ``duplicate=True``，不改账。"""
        if not fill.fill_id:
            return PostResult(False, reason="fill_id 为空（拒绝无主键成交）")
        if fill.qty <= 0:
            return PostResult(False, reason=f"成交数量非正：{fill.qty}")
        if fill.price <= 0:
            return PostResult(False, reason=f"成交价非正：{fill.price}")
        if fill.side not in ("buy", "sell"):
            return PostResult(False, reason=f"非法方向：{fill.side}")
        with self._lock:
            if fill.fill_id in self._fills:
                return PostResult(True, duplicate=True,
                                  reason="重复成交事件（已忽略）", fill=self._fills[fill.fill_id])
            try:
                self._append_durable(fill)
            except OSError as exc:
                self._write_errors += 1
                return PostResult(False, reason=f"账本写入失败：{exc}")
            self._fills[fill.fill_id] = fill
            self._order_index.setdefault(fill.order_id, []).append(fill.fill_id)
            return PostResult(True, fill=fill)

    def _append_durable(self, fill: Fill) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(fill.as_dict(), ensure_ascii=False, sort_keys=True) + "\n"
        with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())

    # ------------------------------------------------------------------ #
    def fills(self) -> List[Fill]:
        with self._lock:
            return sorted(self._fills.values(), key=lambda f: (f.ts, f.fill_id))

    def for_order(self, order_id: str) -> List[Fill]:
        with self._lock:
            return [self._fills[i] for i in self._order_index.get(order_id, [])]

    def cash_delta(self) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for f in self.fills():
            out[f.currency] = round(out.get(f.currency, 0.0) + f.cash_delta, 8)
        return out

    def qty_by_symbol(self) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for f in self.fills():
            signed = f.qty if f.side == "buy" else -f.qty
            out[f.symbol] = round(out.get(f.symbol, 0.0) + signed, 8)
        return out

    def fees(self) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for f in self.fills():
            out[f.currency] = round(out.get(f.currency, 0.0) + f.fee, 8)
        return out

    def digest(self) -> str:
        """内容指纹：同一组成交无论顺序如何都得到同值（用于确定性比对）。"""
        h = hashlib.sha256()
        for line in sorted(json.dumps(f.as_dict(), ensure_ascii=False, sort_keys=True)
                           for f in self.fills()):
            h.update(line.encode("utf-8"))
            h.update(b"\n")
        return h.hexdigest()

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {"fills": len(self._fills), "orders": len(self._order_index),
                    "torn_isolated": self._torn, "write_errors": self._write_errors,
                    "path": str(self.path)}


# ---------------------------------------------------------------------- #
# 从 Broker 订单派生成交（唯一键由 Broker 提供，不在本层猜）
# ---------------------------------------------------------------------- #
def fills_from_orders(orders: Iterable[Dict[str, Any]]) -> List[Fill]:
    """把 PaperBroker 的订单记录转换成 :class:`Fill`。

    只承认 ``status == "filled"`` 且 ``filled_qty > 0`` 的订单。
    唯一键用 Broker 落盘的 ``fill_id``；老数据没有该字段时用
    ``order_id`` 派生（**确定性**，不含随机/时间成分）。
    """
    out: List[Fill] = []
    for o in orders:
        if str(o.get("status")) != "filled":
            continue
        qty = float(o.get("filled_qty") or 0.0)
        if qty <= 0:
            continue
        fid = str(o.get("fill_id") or "") or f"F{o.get('order_id')}-1"
        out.append(Fill(
            fill_id=fid,
            order_id=str(o.get("order_id")),
            symbol=str(o.get("symbol", "")).upper(),
            side=str(o.get("side", "")),
            qty=qty,
            price=float(o.get("avg_fill_price") or o.get("price") or 0.0),
            fee=float(o.get("commission") or 0.0),
            currency=str(o.get("currency") or "USD"),
            ts=str(o.get("ts") or ""),
        ))
    return out
