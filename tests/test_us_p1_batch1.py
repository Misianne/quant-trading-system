"""US-P1 第一批：交易安全补强验收测试。

对应 `US_P0_INDEPENDENT_REVIEW.md` 的 N-1 ~ N-4 与配置守卫：

* **N-1** 数据库五类局部故障不得让 monitor 静默退出；故障必须独立留痕与告警。
* **N-2** 未知单必须按方向分级，允许受限 reduce-only 退出但要核实持仓/限数量/防反向。
* **N-3** 急停哨兵读/写故障语义拆分；读失败全阻断，写失败默认阻断。
* **N-4** 订单时间戳有界；未来时间戳不得永久卡死冷却；冷却不得阻断止损。
* **配置守卫** 美股规则不得被配置绕过；模板与生产一致。

测试一律使用真实 SQLite（临时目录）、真实文件系统、真实时区库；
故障注入通过"只让某个方法抛错"的子类实现，**不 mock 掉被测逻辑本身**。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ai_trader.audit.faults import FaultRecorder
from ai_trader.audit.journal import AuditJournal
from ai_trader.broker.base import (
    AccountSummary,
    Broker,
    BrokerError,
    Candle,
    OrderResult,
    Position,
    Quote,
)
from ai_trader.config import AppConfig, build_gate, load_config
from ai_trader.guards import (
    G_BOARD_NOT_VERIFIED,
    G_CALENDAR_EXPIRED,
    G_MARKET_NOT_ENFORCED,
    G_POLICY_NOT_REJECT,
    G_RULES_DISABLED,
    G_RULES_NOT_ENFORCED,
    G_SESSION_MISSING,
    ConfigGuardError,
    assert_trading_config,
    check_trading_config,
)
from ai_trader.market.rules import MarketRuleSet
from ai_trader.risk.controller import OrderIntent, Portfolio, RiskController
from ai_trader.safety.halt import (
    HALT_FILENAME,
    KIND_CORRUPT,
    KIND_DIR_FAILED,
    HaltStore,
    HaltStoreError,
)
from ai_trader.strategy.engine import StrategyEngine

TZ_NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 4, 10, 0, tzinfo=TZ_NY)      # 美东常规时段内
POS_QTY = 10


# --------------------------------------------------------------------------- #
# 基础设施：可控券商 / 可控数据库 / 配置
# --------------------------------------------------------------------------- #
class StubBroker(Broker):
    """走真实 Broker 契约的替身（不是 Mock），价格与持仓完全可控。"""

    venue = "stub"
    is_live = True

    def __init__(self, *, price=200.0, positions=None, pos_exc=None,
                 summary_day_pnl=0.0, day_pnl_known=True):
        self.price = price
        self.positions = positions if positions is not None else [
            Position(symbol="usAAPL", market="us", qty=POS_QTY, avg_cost=200.0,
                     last_price=price, currency="USD")]
        self.pos_exc = pos_exc
        self.day_pnl = summary_day_pnl
        self.day_pnl_known = day_pnl_known
        self.places = []
        self.cancels = []

    # ---- 只读 ----
    def get_accounts(self):
        return []

    def get_summary(self, currency: str = "") -> AccountSummary:
        return AccountSummary(currency=currency or "USD", cash=1_000_000.0,
                              market_value=0.0, equity=1_000_000.0,
                              day_pnl=self.day_pnl,
                              day_pnl_known=self.day_pnl_known, source="stub")

    def get_positions(self, **kwargs):
        if self.pos_exc:
            raise self.pos_exc
        return list(self.positions)

    def get_quote(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, market="us", last=self.price,
                     prev_close=self.price, source="stub")

    def get_quotes(self, symbols):
        return {s: self.get_quote(s) for s in symbols}

    def get_klines(self, symbol: str, ktype: str = "day", num: int = 120):
        return [Candle(ts=f"d{i}", open=self.price, high=self.price * 1.01,
                       low=self.price * 0.99, close=self.price, volume=1000)
                for i in range(num)]

    # ---- 交易 ----
    def place_order(self, req) -> OrderResult:
        self.places.append({"symbol": req.symbol, "side": req.side,
                            "qty": req.qty, "client_id": req.client_id})
        return OrderResult(order_id=f"STUB-{len(self.places)}",
                           broker_order_id=f"STUB-{len(self.places)}",
                           symbol=req.symbol, market=req.market, side=req.side,
                           qty=req.qty, price=req.price, status="filled",
                           filled_qty=req.qty, avg_fill_price=req.price,
                           venue="stub", ts=NOW.isoformat(timespec="seconds"),
                           client_id=req.client_id)

    def cancel_order(self, order_id: str) -> OrderResult:
        self.cancels.append(order_id)
        return OrderResult(order_id=order_id, broker_order_id=order_id, symbol="",
                           market="", side="", qty=0.0, price=None,
                           status="cancelled", venue="stub", ts="")

    def get_order(self, order_id: str):
        return None

    def list_orders(self, limit: int = 50):
        return []

    def health(self):
        return {"venue": "stub", "connected": True}


class FlakyJournal(AuditJournal):
    """只让指定方法抛错的真实审计库（其余方法照常工作）。"""

    BROKEN: set = set()

    def _maybe(self, name: str) -> None:
        if name in self.BROKEN:
            raise sqlite3.OperationalError(f"database is temporarily unavailable ({name})")

    def log(self, *a, **kw):
        self._maybe("log")
        return super().log(*a, **kw)

    def find_orders(self, *a, **kw):
        self._maybe("find_orders")
        return super().find_orders(*a, **kw)

    def list_orders(self, *a, **kw):
        self._maybe("list_orders")
        return super().list_orders(*a, **kw)

    def orders_since(self, *a, **kw):
        self._maybe("orders_since")
        return super().orders_since(*a, **kw)

    def record_order(self, *a, **kw):
        self._maybe("record_order")
        return super().record_order(*a, **kw)

    def record_risk_decision(self, *a, **kw):
        self._maybe("record_risk_decision")
        return super().record_risk_decision(*a, **kw)

    def update_order_status(self, *a, **kw):
        self._maybe("update_order_status")
        return super().update_order_status(*a, **kw)


def _raw(**over):
    raw = json.loads(json.dumps(load_config(use_dotenv=False).raw))
    for dotted, val in over.items():
        node = raw
        parts = dotted.split(".")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = val
    return raw


def _cfg(tmp_path: Path, **over) -> AppConfig:
    raw = _raw(**over)
    raw["system"]["data_dir"] = str(tmp_path / "data")
    raw["system"]["log_dir"] = str(tmp_path / "logs")
    raw["audit"]["db_path"] = str(tmp_path / "audit.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    cfg = AppConfig(raw=raw, path=tmp_path / "config.yaml",
                    gate=build_gate(raw, data_dir=tmp_path / "data"))
    cfg.halt_store = HaltStore(cfg.data_dir)
    return cfg


def _engine(cfg, broker, journal, *, stops=None):
    risk = RiskController(cfg, cfg.gate, rules=MarketRuleSet(cfg.raw["market_rules"]))
    eng = StrategyEngine(cfg, broker, _model(), risk, journal,
                         stops_path=cfg.data_dir / "stops.json")
    if stops:
        eng.register_stops("usAAPL", stop_loss=stops, take_profit=None, qty=POS_QTY)
    return eng


class _model:
    """不会用到的模型（monitor 路径不触发 AI）。"""

    name = "stub"
    model_id = "stub-v1"
    can_trade = True
    available = True
    is_llm = False

    def analyze(self, ctx):  # pragma: no cover
        raise AssertionError("monitor 不应调用模型")

    def health(self):
        return {"name": self.name, "model_id": self.model_id}


def _journal(tmp_path: Path) -> AuditJournal:
    return AuditJournal(tmp_path / "audit.db", tmp_path / "audit.jsonl")


# --------------------------------------------------------------------------- #
# N-1：数据库五类局部故障
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("broken", ["log", "find_orders", "list_orders", "orders_since"])
def test_monitor_still_liquidates_when_db_method_fails(tmp_path, broken):
    """核心回归：任一个数据库方法坏掉，**止损都必须照常执行**。"""
    j = FlakyJournal(tmp_path / "audit.db", tmp_path / "audit.jsonl")
    j.BROKEN = {broken}
    broker = StubBroker(price=180.0)          # 180 < 止损 190 → 必须卖出
    eng = _engine(_cfg(tmp_path), broker, j, stops=190.0)

    out = eng.monitor(now=NOW)

    assert broker.places, f"{broken} 故障时止损没有执行：{out}"
    assert broker.places[0]["side"] == "sell"
    assert broker.places[0]["qty"] == POS_QTY
    # 故障必须有独立留痕（不依赖已经坏掉的审计库）
    kinds = {f["kind"] for f in eng.faults.read_all()}
    assert kinds, "数据库故障未写入独立故障通道"
    j.close()


def test_monitor_survives_all_db_read_methods_failing(tmp_path):
    """find_orders + list_orders + orders_since 同时故障（最坏情况）。"""
    j = FlakyJournal(tmp_path / "audit.db", tmp_path / "audit.jsonl")
    j.BROKEN = {"find_orders", "list_orders", "orders_since"}
    broker = StubBroker(price=180.0)
    eng = _engine(_cfg(tmp_path), broker, j, stops=190.0)

    out = eng.monitor(now=NOW)

    assert broker.places, out
    counters = eng.faults.counts
    assert counters.get("order_read_failed", 0) >= 1
    j.close()


def test_fault_channel_is_independent_and_persistent(tmp_path):
    """故障记录必须落在 SQLite 之外（faults.jsonl / FAULT.json / alerts.log）。"""
    cfg = _cfg(tmp_path)
    j = FlakyJournal(tmp_path / "audit.db", tmp_path / "audit.jsonl")
    j.BROKEN = {"log", "find_orders"}
    broker = StubBroker(price=180.0)
    eng = _engine(cfg, broker, j, stops=190.0)
    eng.monitor(now=NOW)

    faults_file = Path(eng.faults.path)
    assert faults_file.exists() and faults_file.stat().st_size > 0
    lines = [json.loads(x) for x in faults_file.read_text(encoding="utf-8").splitlines() if x]
    assert lines and {"kind", "severity", "detail"} <= set(lines[0])
    assert Path(eng.faults.sentinel_path).exists()
    assert Path(eng.faults.alert_path).exists()
    assert "ALERT" in Path(eng.faults.alert_path).read_text(encoding="utf-8")
    j.close()


def test_db_failure_is_not_treated_as_empty_positions(tmp_path):
    """数据库坏掉 ≠ 没有持仓：scan 必须整轮停手，monitor 必须用兜底并标记未核实。"""
    j = FlakyJournal(tmp_path / "audit.db", tmp_path / "audit.jsonl")
    j.BROKEN = {"find_orders", "list_orders", "orders_since"}
    broker = StubBroker(price=200.0, pos_exc=BrokerError("持仓接口 500", code="HTTP_500"))
    eng = _engine(_cfg(tmp_path), broker, j)

    rep = eng.scan(symbols=["usAAPL"], now=NOW)
    assert rep.blocked_reason and "持仓读取失败" in rep.blocked_reason
    assert not broker.places

    out = eng.monitor(now=NOW)
    assert any(o.get("kind") == "positions_unavailable" for o in out), out
    assert eng._positions_verified is False
    j.close()


def test_orders_history_unreadable_blocks_entry(tmp_path):
    """读不到订单历史时**禁止开仓**（不能假装没有历史委托）。"""
    cfg = _cfg(tmp_path)
    j = FlakyJournal(tmp_path / "audit.db", tmp_path / "audit.jsonl")
    j.BROKEN = {"list_orders"}
    risk = RiskController(cfg, cfg.gate, rules=MarketRuleSet(cfg.raw["market_rules"]))
    pf = Portfolio(currency="USD", equity=1_000_000.0, cash=1_000_000.0,
                   positions=[])
    buy = OrderIntent(symbol="usAAPL", side="buy", qty=10, price=200.0,
                      market="us", currency="USD", stop_loss=194.0)
    d = risk.evaluate(buy, pf, last_price=200.0, recent_orders=[],
                      now=NOW, journal=j, orders_known=False)
    assert not d.approved
    assert "orders_history" in d.failed_checks
    j.close()


def test_record_order_failure_is_critical_fault(tmp_path):
    """订单已提交但落库失败 → 必须写 critical 故障（供人工核对券商侧）。"""
    j = FlakyJournal(tmp_path / "audit.db", tmp_path / "audit.jsonl")
    broker = StubBroker(price=200.0)
    eng = _engine(_cfg(tmp_path), broker, j)
    j.BROKEN = {"record_order"}
    ok = eng._jrecord_order({"order_id": "X1", "symbol": "usAAPL", "side": "buy",
                             "qty": 10, "status": "filled"})
    assert ok is False
    latest = eng.faults.last
    assert latest is not None and latest.kind == "audit_write_failed"
    assert latest.severity == "critical"
    j.close()


# --------------------------------------------------------------------------- #
# N-2：未知单与减仓
# --------------------------------------------------------------------------- #
def _risk_with_orders(tmp_path, orders, *, verified=True, policy_reject=True):
    cfg = _cfg(tmp_path)
    risk = RiskController(cfg, cfg.gate, rules=MarketRuleSet(cfg.raw["market_rules"]))
    pf = Portfolio(currency="USD", equity=1_000_000.0, cash=1_000_000.0,
                   positions=[Position(symbol="usAAPL", market="us", qty=POS_QTY,
                                       avg_cost=200.0, last_price=190.0,
                                       currency="USD")],
                   positions_verified=verified)
    return cfg, risk, pf, orders


def _unknown(side="buy", qty=10):
    return {"order_id": "U1", "client_id": "cid-1", "symbol": "usAAPL",
            "side": side, "qty": qty, "status": "unknown",
            "ts": (NOW - timedelta(seconds=30)).isoformat(timespec="seconds")}


def _exit_intent(qty=POS_QTY):
    return OrderIntent(symbol="usAAPL", side="sell", qty=qty, price=190.0,
                       market="us", currency="USD", is_exit=True,
                       exit_mode="stop_loss", reason="止损")


def test_unknown_buy_allows_reduce_only_exit(tmp_path):
    """未知买单未对清时，止损（reduce-only）必须能发出去。"""
    cfg, risk, pf, _ = _risk_with_orders(tmp_path, [_unknown("buy")])
    d = risk.evaluate(_exit_intent(), pf, last_price=190.0,
                      recent_orders=[_unknown("buy")], now=NOW)
    assert d.approved, d.reasons
    chk = {c.name: c for c in d.checks}
    assert chk["unresolved_order_state"].ok
    assert "reduce-only" in chk["unresolved_order_state"].detail


def test_unknown_buy_blocks_exit_over_held_qty(tmp_path):
    """减仓数量超过已核实持仓 → 拒绝（不允许卖出超过真实持仓）。"""
    cfg, risk, pf, _ = _risk_with_orders(tmp_path, [_unknown("buy")])
    d = risk.evaluate(_exit_intent(qty=POS_QTY + 1), pf, last_price=190.0,
                      recent_orders=[_unknown("buy")], now=NOW)
    assert not d.approved
    assert "unresolved_order_state" in d.failed_checks


def test_unknown_buy_blocks_exit_when_positions_unverified(tmp_path):
    """持仓未经实时核实（降级快照）→ 不得走 reduce-only。"""
    cfg, risk, pf, _ = _risk_with_orders(tmp_path, [_unknown("buy")], verified=False)
    d = risk.evaluate(_exit_intent(), pf, last_price=190.0,
                      recent_orders=[_unknown("buy")], now=NOW)
    assert not d.approved
    assert "未经券商实时核实" in " ".join(d.reasons)


def test_unknown_sell_blocks_another_sell(tmp_path):
    """未知卖单可能已成交 → 再卖会超卖，必须阻断。"""
    cfg, risk, pf, _ = _risk_with_orders(tmp_path, [_unknown("sell")])
    d = risk.evaluate(_exit_intent(), pf, last_price=190.0,
                      recent_orders=[_unknown("sell")], now=NOW)
    assert not d.approved
    assert "超卖" in " ".join(d.reasons)


def test_unknown_order_blocks_new_entry(tmp_path):
    """未知单存在期间，买入开仓一律阻断（不允许反向/再加仓）。"""
    cfg, risk, pf, _ = _risk_with_orders(tmp_path, [_unknown("sell")])
    buy = OrderIntent(symbol="usAAPL", side="buy", qty=5, price=190.0,
                      market="us", currency="USD", stop_loss=185.0)
    d = risk.evaluate(buy, pf, last_price=190.0,
                      recent_orders=[_unknown("sell")], now=NOW)
    assert not d.approved
    assert "unresolved_order_state" in d.failed_checks


def test_reduce_only_exit_requires_holdings(tmp_path):
    """已核实持仓为 0 时不得减仓（防反向开仓）。"""
    cfg = _cfg(tmp_path)
    risk = RiskController(cfg, cfg.gate, rules=MarketRuleSet(cfg.raw["market_rules"]))
    pf = Portfolio(currency="USD", equity=1_000_000.0, cash=1_000_000.0,
                   positions=[], positions_verified=True)
    d = risk.evaluate(_exit_intent(), pf, last_price=190.0,
                      recent_orders=[_unknown("buy")], now=NOW)
    assert not d.approved
    assert "持仓为 0" in " ".join(d.reasons)


def test_reduce_only_exit_is_audited(tmp_path):
    """走 reduce-only 豁免时必须留下"退出授权"审计事件。"""
    cfg = _cfg(tmp_path)
    j = FlakyJournal(tmp_path / "audit.db", tmp_path / "audit.jsonl")
    broker = StubBroker(price=180.0)
    eng = _engine(cfg, broker, j, stops=190.0)
    # 先制造一条未知买单
    j.record_order({
        "order_id": "U1", "client_id": "cid-1", "symbol": "usAAPL",
        "side": "buy", "qty": 10, "price": 205.0, "status": "unknown",
        "ts": (NOW - timedelta(seconds=30)).isoformat(timespec="seconds"),
        "venue": "stub", "market": "us"})
    out = eng.monitor(now=NOW)
    assert broker.places, out
    events = [e["event"] for e in j.list_audit(limit=200)]
    assert "exit_authorized_reduce_only" in events
    j.close()


# --------------------------------------------------------------------------- #
# N-3：急停哨兵读写语义
# --------------------------------------------------------------------------- #
def _intent_buy():
    return OrderIntent(symbol="usAAPL", side="buy", qty=10, price=200.0,
                       market="us", currency="USD", stop_loss=194.0)


def _pf():
    return Portfolio(currency="USD", equity=1_000_000.0, cash=1_000_000.0,
                     positions=[Position(symbol="usAAPL", market="us", qty=POS_QTY,
                                         avg_cost=200.0, last_price=200.0,
                                         currency="USD")])


def test_halt_read_failure_blocks_everything(tmp_path):
    """哨兵损坏（读失败）→ 买入被拒，且分类为 corrupt。"""
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / HALT_FILENAME).write_text("{not json", encoding="utf-8")
    cfg = _cfg(tmp_path)
    assert cfg.gate.state.halt_read_failed is True
    assert cfg.gate.state.halt_error_kind == KIND_CORRUPT

    risk = RiskController(cfg, cfg.gate, rules=MarketRuleSet(cfg.raw["market_rules"]))
    d = risk.evaluate(_intent_buy(), _pf(), last_price=200.0, now=NOW)
    assert not d.approved
    assert "halt_storage_read" in d.failed_checks
    # 读失败时连减仓也阻断（不知道是否被人为停止）
    d2 = risk.evaluate(_exit_intent(), _pf(), last_price=200.0, now=NOW)
    assert not d2.approved
    assert "halt_storage_read" in d2.failed_checks


def test_halt_write_failure_blocks_by_default(tmp_path):
    """哨兵写失败 → 默认阻断，且与读失败分开标记。"""
    cfg = _cfg(tmp_path)
    bad_dir = tmp_path / "data_as_file"
    bad_dir.write_text("not a directory", encoding="utf-8")
    cfg.halt_store = HaltStore(bad_dir)
    risk = RiskController(cfg, cfg.gate, rules=MarketRuleSet(cfg.raw["market_rules"]))

    with pytest.raises(HaltStoreError) as ei:
        risk.halt("磁盘不可写", actor="pytest")
    assert ei.value.kind == KIND_DIR_FAILED

    st = risk.gate.state
    assert st.halt_write_failed is True
    assert st.halt_read_failed is False
    d = risk.evaluate(_exit_intent(), _pf(), last_price=200.0, now=NOW)
    assert not d.approved
    assert "halt_storage_write" in d.failed_checks


def test_halt_write_failure_reduce_only_channel_structure(tmp_path):
    """写失败 + 显式开启 reduce_only → 受限减仓可通过；超额仍拒绝。"""
    cfg = _cfg(tmp_path, **{"risk.halt_write_failure_exit_mode": "reduce_only"})
    cfg.gate.state.mark_halt_storage_failure(kind=KIND_DIR_FAILED, phase="write")
    risk = RiskController(cfg, cfg.gate, rules=MarketRuleSet(cfg.raw["market_rules"]))

    d = risk.evaluate(_exit_intent(), _pf(), last_price=200.0, now=NOW)
    assert d.approved, d.reasons
    assert "reduce-only" in " ".join(d.warnings)

    d2 = risk.evaluate(_exit_intent(qty=POS_QTY + 5), _pf(), last_price=200.0, now=NOW)
    assert not d2.approved

    d3 = risk.evaluate(_intent_buy(), _pf(), last_price=200.0, now=NOW)
    assert not d3.approved, "写失败时不得开新仓"


def test_halt_read_and_write_flags_do_not_leak(tmp_path):
    """读恢复后不应把写失败一起清掉（反之亦然）。"""
    store = HaltStore(tmp_path / "data")
    store.read()                                   # 文件不存在 → 正常
    assert store.read_failed is False and store.write_failed is False
    store.write_failed = True                      # 模拟先前写失败
    store.read()
    assert store.write_failed is True, "读成功不得清除写失败标记"


# --------------------------------------------------------------------------- #
# N-4：订单时间戳 / 冷却窗口
# --------------------------------------------------------------------------- #
def _order(ts, *, side="sell", status="filled", oid="O1"):
    return {"order_id": oid, "symbol": "usAAPL", "side": side, "qty": POS_QTY,
            "status": status, "ts": ts}


def test_future_timestamp_does_not_block_stop_loss(tmp_path):
    """未来时间戳（时钟回拨/时区错乱）不得永久卡死止损。"""
    cfg = _cfg(tmp_path)
    risk = RiskController(cfg, cfg.gate, rules=MarketRuleSet(cfg.raw["market_rules"]))
    future = (NOW + timedelta(days=200)).isoformat(timespec="seconds")
    d = risk.evaluate(_exit_intent(), _pf(), last_price=190.0,
                      recent_orders=[_order(future)], now=NOW)
    assert d.approved, d.reasons
    chk = {c.name: c for c in d.checks}
    assert "order_timestamp_anomaly" in chk
    assert "未来" in chk["order_timestamp_anomaly"].detail


def test_future_timestamp_does_not_block_entry_forever(tmp_path):
    """未来时间戳不计入冷却 —— 开仓不应被永久拒绝（但要告警）。"""
    cfg = _cfg(tmp_path)
    risk = RiskController(cfg, cfg.gate, rules=MarketRuleSet(cfg.raw["market_rules"]))
    future = (NOW + timedelta(days=200)).isoformat(timespec="seconds")
    buy = OrderIntent(symbol="usAAPL", side="buy", qty=5, price=200.0,
                      market="us", currency="USD", stop_loss=194.0)
    d = risk.evaluate(buy, _pf(), last_price=200.0,
                      recent_orders=[_order(future, side="buy")], now=NOW)
    chk = {c.name: c for c in d.checks}
    assert "order_timestamp_anomaly" in chk
    assert "duplicate_order" not in [c.name for c in d.checks if not c.ok]


def test_recent_timestamp_still_blocks_entry(tmp_path):
    """回归：冷却机制没有被"简单删除" —— 正常时间戳仍然拦重复开仓。"""
    cfg = _cfg(tmp_path)
    risk = RiskController(cfg, cfg.gate, rules=MarketRuleSet(cfg.raw["market_rules"]))
    recent = (NOW - timedelta(seconds=10)).isoformat(timespec="seconds")
    buy = OrderIntent(symbol="usAAPL", side="buy", qty=5, price=200.0,
                      market="us", currency="USD", stop_loss=194.0)
    d = risk.evaluate(buy, _pf(), last_price=200.0,
                      recent_orders=[_order(recent, side="buy")], now=NOW)
    assert not d.approved
    assert "duplicate_order" in d.failed_checks


def test_naive_timestamp_is_flagged_but_usable(tmp_path):
    """无时区时间戳：按本机时区解释，但要留痕。"""
    cfg = _cfg(tmp_path)
    risk = RiskController(cfg, cfg.gate, rules=MarketRuleSet(cfg.raw["market_rules"]))
    naive = (NOW - timedelta(seconds=10)).replace(tzinfo=None).isoformat(timespec="seconds")
    buy = OrderIntent(symbol="usAAPL", side="buy", qty=5, price=200.0,
                      market="us", currency="USD", stop_loss=194.0)
    d = risk.evaluate(buy, _pf(), last_price=200.0,
                      recent_orders=[_order(naive, side="buy")], now=NOW)
    chk = {c.name: c for c in d.checks}
    assert "order_timestamp_anomaly" in chk
    assert "缺少时区" in chk["order_timestamp_anomaly"].detail


def test_unparsable_timestamp_is_skipped_not_crashing(tmp_path):
    cfg = _cfg(tmp_path)
    risk = RiskController(cfg, cfg.gate, rules=MarketRuleSet(cfg.raw["market_rules"]))
    d = risk.evaluate(_exit_intent(), _pf(), last_price=190.0,
                      recent_orders=[_order("not-a-time")], now=NOW)
    assert d.approved
    chk = {c.name: c for c in d.checks}
    assert "order_timestamp_anomaly" in chk


def test_exit_not_blocked_by_time_cooldown(tmp_path):
    """减仓不受时间冷却限制（但未完成委托仍然阻断）。"""
    cfg = _cfg(tmp_path)
    risk = RiskController(cfg, cfg.gate, rules=MarketRuleSet(cfg.raw["market_rules"]))
    recent_filled = (NOW - timedelta(seconds=5)).isoformat(timespec="seconds")
    d = risk.evaluate(_exit_intent(), _pf(), last_price=190.0,
                      recent_orders=[_order(recent_filled, status="filled")], now=NOW)
    assert d.approved, d.reasons

    d2 = risk.evaluate(_exit_intent(), _pf(), last_price=190.0,
                       recent_orders=[_order(recent_filled, status="submitted")], now=NOW)
    assert not d2.approved, "未完成的同向委托仍然必须阻断（状态型检查）"


# --------------------------------------------------------------------------- #
# 配置守卫
# --------------------------------------------------------------------------- #
def test_production_config_passes_guard():
    cfg = load_config(use_dotenv=False)
    violations = check_trading_config(cfg)
    assert violations == [], [v.as_dict() for v in violations]
    assert_trading_config(cfg)          # 不抛异常


def test_production_config_is_reject_policy():
    cfg = load_config(use_dotenv=False)
    mr = cfg.raw["market_rules"]
    assert mr["unenforced_market_policy"] == "reject"
    assert "us" in {str(m).lower() for m in mr["enforce_markets"]}
    assert mr["enabled"] is True and mr["enforce"] is True


def test_template_matches_production_semantics():
    """config.template.yaml 必须与 config.yaml 的非注释内容完全一致。"""
    import yaml
    root = Path(__file__).resolve().parents[1]
    prod = yaml.safe_load((root / "config" / "config.yaml").read_text(encoding="utf-8"))
    tpl = yaml.safe_load((root / "config" / "config.template.yaml").read_text(encoding="utf-8"))
    assert prod == tpl


@pytest.mark.parametrize("over,code", [
    ({"market_rules.enabled": False}, G_RULES_DISABLED),
    ({"market_rules.enforce": False}, G_RULES_NOT_ENFORCED),
    ({"market_rules.enforce_markets": ["cn"]}, G_MARKET_NOT_ENFORCED),
    ({"market_rules.unenforced_market_policy": "warn"}, G_POLICY_NOT_REJECT),
    ({"market_rules.sessions.us": {"tz": "America/New_York", "regular": []}},
     G_SESSION_MISSING),
    # 整个 sessions.us 缺失：即使代码有内置默认值，也不允许"靠兜底"通过
    ({"market_rules.sessions": {"hk": {"tz": "Asia/Hong_Kong",
                                      "regular": [["09:30", "12:00"]]}}},
     G_SESSION_MISSING),
    ({"market_rules.calendar.markets.us.covered_until": "2026-01-31"},
     G_CALENDAR_EXPIRED),
])
def test_guard_rejects_unsafe_config(tmp_path, over, code):
    cfg = _cfg(tmp_path, **over)
    codes = {v.code for v in check_trading_config(cfg)}
    assert code in codes, codes
    with pytest.raises(ConfigGuardError):
        assert_trading_config(cfg)


def test_guard_rejects_unverified_board(tmp_path):
    cfg = _cfg(tmp_path, **{"market_rules.calendar.markets.us.status": "UNVERIFIED"})
    codes = {v.code for v in check_trading_config(cfg)}
    assert G_BOARD_NOT_VERIFIED in codes or "GUARD_CALENDAR_NOT_VERIFIED" in codes


def test_guard_blocks_scan_cli(tmp_path, monkeypatch, capsys):
    """CLI scan 在守卫失败时返回 4，且不产生任何委托。"""
    from ai_trader import cli as cli_mod

    cfg_path = tmp_path / "bad.yaml"
    raw = _raw(**{"market_rules.enabled": False})
    import yaml
    cfg_path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    rc = cli_mod.main(["--config", str(cfg_path), "scan", "--symbols", "usAAPL"])
    assert rc == cli_mod.EXIT_GUARD


def test_worker_script_runs_guard_before_trading():
    root = Path(__file__).resolve().parents[1]
    text = (root / "scripts" / "run_worker.ps1").read_text(encoding="utf-8-sig")
    assert '"guard"' in text
    assert text.index('"guard"') < text.index('"monitor"'), "守卫必须在 monitor 之前执行"
    assert "exit 4" in text


def test_reject_policy_really_rejects_unmodelled_market():
    """reject 生效后，未纳入强制校验的市场直接被规则层拒绝。"""
    cfg = load_config(use_dotenv=False)
    rs = MarketRuleSet(cfg.raw["market_rules"])
    r = rs.check_order(symbol="hk00700", side="buy", qty=100, price=300.0,
                       prev_close=300.0, now=NOW)
    assert r.applied is True
    assert any(v.code == "MARKET_NOT_ENFORCED" for v in r.violations)


def test_us_market_still_fully_enforced():
    """收紧港股不影响美股：美股仍走完整规则校验。"""
    cfg = load_config(use_dotenv=False)
    rs = MarketRuleSet(cfg.raw["market_rules"])
    ok = rs.check_order(symbol="usAAPL", side="buy", qty=10, price=200.0,
                        prev_close=200.0, now=NOW)
    assert ok.applied is True and not ok.violations
    bad = rs.check_order(symbol="usAAPL", side="buy", qty=10, price=200.003,
                         prev_close=200.0, now=NOW)
    assert any(v.code == "PRICE_TICK" for v in bad.violations)
