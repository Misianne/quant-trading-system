"""测试：信号 → 风控 → 下单 → 记账 全链路，以及止损止盈与紧急停止。"""
from __future__ import annotations

import pytest

from ai_trader.ai.base import AIModel, AIModelError, TradingSignal
from ai_trader.ai.providers import HeuristicModel
from ai_trader.broker.base import Quote
from ai_trader.strategy.engine import StrategyEngine
from tests.conftest import WEEKDAY_1000, StubModel, make_cfg


def _engine(cfg, paper, journal, risk, model):
    """本文件测的是"信号→风控→下单"的机械流程，因此显式开启沙盒模式
    （允许规则引擎开仓）。P1-9 的 AI 权威语义由 tests/test_ai_authority.py 覆盖。
    """
    from pathlib import Path
    return StrategyEngine(cfg, paper, model, risk, journal,
                          stops_path=Path(cfg.raw["system"]["data_dir"]) / "stops.json",
                          allow_rule_engine_entry=True)


def _buy_signal(symbol="hk00700", price=None, sl_ratio=0.95, conf=0.85):
    return TradingSignal(symbol=symbol, action="buy", confidence=conf,
                         entry_price=price, stop_loss=None if price is None else price * sl_ratio,
                         take_profit=None if price is None else price * 1.08,
                         position_pct=0.10, rationale="stub 买入", market="hk")


def test_scan_buy_creates_order_and_position(cfg, paper, journal, risk):
    quote = paper.get_quote("hk00700")
    model = StubModel(_buy_signal(price=quote.last))
    eng = _engine(cfg, paper, journal, risk, model)
    report = eng.scan(["hk00700"], now=WEEKDAY_1000)

    o = report.outcomes[0]
    assert o.stage == "ordered", o.detail
    assert o.order["side"] == "buy"
    assert o.order["status"] in ("filled", "submitted")

    orders = journal.list_orders(limit=5)
    assert len(orders) == 1
    assert orders[0]["signal_id"] == o.signal["signal_id"]
    assert orders[0]["decision_id"] == o.decision["decision_id"]

    if o.order["status"] == "filled":
        assert paper.get_positions()[0].qty > 0
    # 止损已登记，重启后仍保护
    assert "hk00700" in eng.stops
    assert eng.stops["hk00700"]["stop_loss"] == pytest.approx(quote.last * 0.95)


def test_illegal_signal_without_stop_loss_is_blocked(cfg, paper, journal, risk):
    quote = paper.get_quote("hk00700")
    bad = TradingSignal(symbol="hk00700", action="buy", confidence=0.99,
                        entry_price=quote.last, stop_loss=None, take_profit=None,
                        position_pct=0.5, market="hk")
    eng = _engine(cfg, paper, journal, risk, StubModel(bad))
    report = eng.scan(["hk00700"], now=WEEKDAY_1000)

    assert report.outcomes[0].stage == "rejected"
    assert "缺少 stop_loss" in report.outcomes[0].detail
    assert journal.list_orders() == []
    assert paper.get_positions() == []


def test_low_confidence_signal_is_blocked(cfg, paper, journal, risk):
    quote = paper.get_quote("hk00700")
    weak = TradingSignal(symbol="hk00700", action="buy", confidence=0.30,
                         entry_price=quote.last, stop_loss=quote.last * 0.95,
                         take_profit=quote.last * 1.08, position_pct=0.1, market="hk")
    eng = _engine(cfg, paper, journal, risk, StubModel(weak))
    report = eng.scan(["hk00700"], now=WEEKDAY_1000)
    assert report.outcomes[0].stage == "rejected"
    assert "confidence" in report.outcomes[0].detail
    assert journal.list_orders() == []


def test_hold_signal_produces_no_order(cfg, paper, journal, risk):
    eng = _engine(cfg, paper, journal, risk,
                  StubModel(TradingSignal.hold("hk00700", "观望", "stub", "v1", "hk")))
    report = eng.scan(["hk00700"], now=WEEKDAY_1000)
    assert report.outcomes[0].stage == "hold"
    assert journal.list_orders() == []


def test_ai_failure_is_recorded_not_crashing(cfg, paper, journal, risk):
    class Boom(AIModel):
        name, model_id = "boom", "v0"

        def analyze(self, ctx):
            raise AIModelError("模拟模型崩溃", code="BOOM")

    eng = _engine(cfg, paper, journal, risk, Boom())
    report = eng.scan(["hk00700"], now=WEEKDAY_1000)
    # P1-9 之后的语义：AI 失败 = 拒绝开仓（blocked），而不再是"error 后继续"
    assert report.outcomes[0].stage == "blocked"
    assert "BOOM" in report.outcomes[0].detail
    assert journal.list_orders() == []
    events = [e["event"] for e in journal.list_audit(limit=30)]
    assert "ai_error" in events
    assert "entry_blocked" in events


def test_kill_switch_blocks_entire_scan(cfg, paper, journal, risk):
    quote = paper.get_quote("hk00700")
    eng = _engine(cfg, paper, journal, risk, StubModel(_buy_signal(price=quote.last)))
    risk.halt("测试停止")
    report = eng.scan(["hk00700"], now=WEEKDAY_1000)
    assert report.blocked_reason
    assert report.outcomes == []
    assert journal.list_orders() == []


def test_stop_loss_monitor_sells_position(cfg, paper, journal, risk):
    quote = paper.get_quote("hk00700")
    eng = _engine(cfg, paper, journal, risk, StubModel(_buy_signal(price=quote.last)))
    eng.scan(["hk00700"], now=WEEKDAY_1000)
    if not paper.get_positions():
        pytest.skip("首单为挂单未成交，跳过止损触发验证")

    # 把止损抬到现价之上，强制触发
    eng.register_stops("hk00700", stop_loss=quote.last * 1.05, take_profit=None, qty=100)
    executed = eng.monitor(now=WEEKDAY_1000)
    assert any(e["kind"] == "exit" for e in executed), executed
    assert paper.get_positions() == [] or paper.get_positions()[0].qty == 0
    # US-P1 Batch2 R-7：退出后**不立即解除**保护，先标记待核实
    #（避免"未知买单随后成交"导致新持仓失去止损）
    assert eng.stops.get("hk00700", {}).get("exit_pending") is True, eng.stops
    # 下一轮重新核实持仓为 0 且无未终结买单 → 才解除
    executed2 = eng.monitor(now=WEEKDAY_1000)
    assert any(e["kind"] == "stops_cleared" for e in executed2), executed2
    assert "hk00700" not in eng.stops


def test_take_profit_monitor_sells_position(cfg, paper, journal, risk):
    quote = paper.get_quote("hk00700")
    eng = _engine(cfg, paper, journal, risk, StubModel(_buy_signal(price=quote.last)))
    eng.scan(["hk00700"], now=WEEKDAY_1000)
    if not paper.get_positions():
        pytest.skip("首单为挂单未成交，跳过止盈触发验证")

    eng.register_stops("hk00700", stop_loss=quote.last * 0.5,
                       take_profit=quote.last * 0.95, qty=100)
    executed = eng.monitor(now=WEEKDAY_1000)
    assert any(e["kind"] == "exit" for e in executed), executed
    assert any("止盈" in e.get("reason", "") for e in executed if e["kind"] == "exit")


def test_heuristic_engine_end_to_end_on_synthetic_data(cfg, paper, journal, risk):
    """用真实规则模型跑一遍：只要求不崩、结果可解释。"""
    eng = _engine(cfg, paper, journal, risk, HeuristicModel())
    report = eng.scan(now=WEEKDAY_1000)
    assert report.finished_at
    assert len(report.outcomes) == len(cfg.universe)
    for o in report.outcomes:
        assert o.stage in ("skipped", "hold", "rejected", "ordered", "error")
        if o.stage == "ordered":
            assert o.decision["approved"] is True


def test_scan_respects_symbol_subset(cfg, paper, journal, risk):
    eng = _engine(cfg, paper, journal, risk, StubModel(TradingSignal.hold("x", "h")))
    report = eng.scan(["usAAPL"], now=WEEKDAY_1000)
    assert len(report.outcomes) == 1
    assert report.outcomes[0].symbol == "usaapl"   # 引擎统一小写归一
