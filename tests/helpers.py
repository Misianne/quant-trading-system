"""测试辅助：可编排的券商/模型替身，用于精确模拟异常场景。

这些替身一律实现真实的 :class:`Broker` 契约（而不是 Mock），
这样被测代码走的是真实调用路径，只是对端行为可控。
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ai_trader.broker.base import (
    AccountSummary,
    Broker,
    BrokerError,
    Candle,
    OrderRequest,
    OrderResult,
    OrderStateUnknown,
    Position,
    Quote,
)
from ai_trader.config import AppConfig, build_gate, load_config
from ai_trader.safety.halt import HaltStore


def live_cfg(tmp: Path, **overrides) -> AppConfig:
    """构造"具备真实下单资格"的内存配置（仅用于走代码分支，不联网）。"""
    raw = copy.deepcopy(load_config(use_dotenv=False).raw)
    raw.setdefault("system", {})
    raw["system"]["data_dir"] = str(tmp / "data")
    raw["system"]["log_dir"] = str(tmp / "logs")
    raw.setdefault("audit", {})
    raw["audit"]["db_path"] = str(tmp / "audit.db")
    raw["audit"]["jsonl_path"] = str(tmp / "audit.jsonl")
    raw.setdefault("safety", {})
    raw["safety"].update({
        "live_trading": True, "paper_trading": False,
        "order_execution_enabled": True, "kill_switch": False,
        "live_ack_phrase": raw["safety"]["required_live_ack"],
    })
    raw.setdefault("fosun", {})
    raw["fosun"]["sub_account_id"] = "SUB1"
    raw["fosun"]["apply_account_id"] = "APPLY1"
    # US-P1 Batch3：`_require()` 现在统一校验生产路由冲突。
    # 生产 config.yaml 里 `sdk_type: "ops"` 与"本适配器只调个人接口"是**已知冲突**
    # （独立审计 F-9，等待星财富确认账户类型，本轮不允许擅自改配置）。
    # 因此凡是验证个人接口行为的夹具，默认声明 `sdk_type: ""`；
    # 需要验证冲突行为的用例显式覆盖成 "ops"（见 test_prod_connectivity_p0）。
    if "fosun.sdk_type" not in overrides:
        raw["fosun"]["sdk_type"] = ""
    # 显式声明历史多市场夹具沿用 warn 语义（生产为 reject）
    if "market_rules.unenforced_market_policy" not in overrides:
        raw.setdefault("market_rules", {})["unenforced_market_policy"] = "warn"
    for dotted, value in overrides.items():
        node = raw
        parts = dotted.split(".")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = value
    data_dir = Path(raw["system"]["data_dir"])
    data_dir.mkdir(parents=True, exist_ok=True)
    cfg = AppConfig(raw=raw, path=tmp / "cfg.yaml",
                    gate=build_gate(raw, data_dir=data_dir))
    cfg.halt_store = HaltStore(cfg.data_dir)
    return cfg


class ScriptedBroker(Broker):
    """按脚本响应异常的券商替身。

    ``place_behaviour``:
        "ok"        → 返回正常 OrderResult
        "no_id"     → 返回"有响应体但无 orderId"的 unknown
        "raise"     → 抛 place_exc
    """

    venue = "scripted"
    is_live = True

    def __init__(self, place_behaviour: str = "ok", place_exc: Optional[BaseException] = None,
                 reconcile_result: Any = "none", reconcile_exc: Optional[BaseException] = None,
                 positions: Optional[List[Position]] = None,
                 positions_exc: Optional[BaseException] = None,
                 cancel_exc: Optional[BaseException] = None):
        self.place_behaviour = place_behaviour
        self.place_exc = place_exc
        self.reconcile_result = reconcile_result
        self.reconcile_exc = reconcile_exc
        self.positions = positions or []
        self.positions_exc = positions_exc
        self.cancel_exc = cancel_exc
        self.calls = {"place": 0, "cancel": 0, "reconcile": 0, "positions": 0}
        self.last_request: Optional[OrderRequest] = None
        self.reconcile_calls: List[Dict[str, Any]] = []
        self.connected = True

    # ---- 只读 ----
    def get_accounts(self):
        return []

    def get_summary(self, currency: str = "") -> AccountSummary:
        return AccountSummary(currency=currency or "HKD", cash=1_000_000.0,
                              market_value=0.0, equity=1_000_000.0, day_pnl=0.0,
                              day_pnl_known=True, source=self.venue)

    def get_positions(self, **kwargs) -> List[Position]:
        self.calls["positions"] += 1
        self.position_kwargs = dict(kwargs)
        if self.positions_exc:
            raise self.positions_exc
        return list(self.positions)

    def get_quote(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, market="hk", last=300.0, prev_close=298.0,
                     source="scripted")

    def get_quotes(self, symbols: List[str]) -> Dict[str, Quote]:
        return {s: self.get_quote(s) for s in symbols}

    def get_klines(self, symbol: str, ktype: str = "day", num: int = 120) -> List[Candle]:
        return [Candle(ts=f"d{i}", open=100 + i, high=101 + i, low=99 + i,
                       close=100 + i, volume=1000) for i in range(num)]

    # ---- 交易 ----
    def place_order(self, req: OrderRequest) -> OrderResult:
        self.calls["place"] += 1
        self.last_request = req
        if self.place_behaviour == "raise":
            exc = self.place_exc or BrokerError("模拟网络中断", code="NETWORK")
            if isinstance(exc, OrderStateUnknown):
                raise exc
            raise OrderStateUnknown(
                f"下单结果未知: {type(exc).__name__}: {exc}",
                client_id=req.client_id or "0", symbol=req.symbol, side=req.side,
                qty=req.qty, price=req.price, cause=repr(exc))
        if self.place_behaviour == "no_id":
            return OrderResult(order_id=f"UNCONFIRMED-{req.client_id}",
                               broker_order_id="", symbol=req.symbol, market=req.market,
                               side=req.side, qty=req.qty, price=req.price,
                               status="unknown", venue=self.venue, ts="2026-01-01T00:00:00+08:00",
                               client_id=req.client_id,
                               reconcile_note="券商未返回 orderId")
        return OrderResult(order_id="BRK-1", broker_order_id="BRK-1",
                           symbol=req.symbol, market=req.market, side=req.side,
                           qty=req.qty, price=req.price, status="filled",
                           filled_qty=req.qty, avg_fill_price=req.price,
                           venue=self.venue, ts="2026-01-01T00:00:00+08:00",
                           client_id=req.client_id)

    def cancel_order(self, order_id: str) -> OrderResult:
        self.calls["cancel"] += 1
        if self.cancel_exc:
            raise self.cancel_exc
        return OrderResult(order_id=order_id, broker_order_id=order_id, symbol="",
                           market="", side="", qty=0.0, price=None,
                           status="cancelled", venue=self.venue)

    def get_order(self, order_id: str):
        return None

    def list_orders(self, limit: int = 50) -> List[OrderResult]:
        if isinstance(self.reconcile_result, list):
            return self.reconcile_result
        return []

    def reconcile_order(self, client_id: str, symbol: str = "", side: str = "",
                        qty: float = 0.0):
        self.calls["reconcile"] += 1
        self.reconcile_calls.append({"client_id": client_id, "symbol": symbol,
                                     "side": side, "qty": qty})
        if self.reconcile_exc:
            raise self.reconcile_exc
        if self.reconcile_result == "none":
            return None
        if isinstance(self.reconcile_result, OrderResult):
            return self.reconcile_result
        return OrderResult(order_id="BRK-FOUND", broker_order_id="BRK-FOUND",
                           symbol=symbol or "hk00700", market="hk", side=side or "buy",
                           qty=qty or 100, price=300.0, status="filled",
                           filled_qty=qty or 100, avg_fill_price=300.0,
                           venue=self.venue, ts="2026-01-01T00:00:01+08:00",
                           client_id=client_id, reconcile_note="对账命中")

    def health(self) -> Dict[str, Any]:
        return {"venue": self.venue, "connected": True, "is_live": True}


@dataclass
class FixedSignalModel:
    """固定输出的模型，用于驱动引擎下单路径。"""

    action: str = "buy"
    confidence: float = 0.85
    entry: float = 300.0
    stop_loss: float = 288.0
    take_profit: float = 330.0
    position_pct: float = 0.10
    name: str = "fixed"
    model_id: str = "fixed-v1"
    is_llm: bool = False
    available: bool = True

    def analyze(self, ctx):
        from ai_trader.ai.base import TradingSignal

        return TradingSignal(symbol=ctx.symbol, action=self.action,
                             confidence=self.confidence, entry_price=self.entry,
                             stop_loss=self.stop_loss, take_profit=self.take_profit,
                             position_pct=self.position_pct, market=ctx.market,
                             provider=self.name, model=self.model_id,
                             rationale="fixed signal")

    def health(self):
        return {"name": self.name, "model_id": self.model_id, "is_llm": False,
                "available": True, "status": "ready"}
