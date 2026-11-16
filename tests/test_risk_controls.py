"""测试 5-7：风控模块逐项验证（仓位限制 / 止损 / 紧急停止 / 重复单等）。"""
from __future__ import annotations

import pytest

from ai_trader.risk.controller import (
    OrderIntent,
    Portfolio,
    RiskController,
    is_market_open,
)
from tests.conftest import WEEKDAY_1000, WEEKEND_1000, make_cfg, pos


def _buy(qty=100, price=300.0, sl=290.0, **kw):
    return OrderIntent(symbol=kw.pop("symbol", "hk00700"), side="buy", qty=qty,
                       price=price, market="hk", currency="HKD",
                       stop_loss=sl, take_profit=price * 1.06, **kw)


def _pf(equity=1_000_000.0, cash=1_000_000.0, positions=None, day_pnl=0.0,
        day_trades=0, day_orders=0, currency="HKD"):
    return Portfolio(currency=currency, equity=equity, cash=cash,
                     positions=positions or [], day_pnl=day_pnl,
                     day_trades=day_trades, day_orders=day_orders)


# ---------------------------------------------------------------- 正常放行
def test_clean_intent_is_approved(risk):
    d = risk.evaluate(_buy(), _pf(), last_price=300.0, now=WEEKDAY_1000)
    assert d.approved, d.reasons
    assert d.approved_qty == 100
    assert all(c.ok for c in d.checks)


# ---------------------------------------------------------------- 仓位限制
def test_max_position_pct_rejects_oversized_order(tmp_path):
    cfg = make_cfg(tmp_path, **{"risk.max_position_pct": 0.10,
                                "risk.max_order_notional": 1_000_000})
    risk = RiskController(cfg, cfg.gate)
    # 权益 100k，买入 300*500=150k → 占比 150% > 10%
    d = risk.evaluate(_buy(qty=500, price=300.0, sl=290.0),
                      _pf(equity=100_000.0, cash=100_000.0), last_price=300.0,
                      now=WEEKDAY_1000)
    assert not d.approved
    assert "max_position_pct" in d.failed_checks
    assert any("占比" in r for r in d.reasons)


def test_max_positions_count_limit(tmp_path):
    cfg = make_cfg(tmp_path, **{"risk.max_positions": 2, "risk.max_gross_exposure_pct": 1.0})
    risk = RiskController(cfg, cfg.gate)
    positions = [pos("hk00700", 100, 300, 300), pos("hk09988", 100, 90, 90)]
    d = risk.evaluate(_buy(symbol="usAAPL", qty=10, price=200.0, sl=190.0),
                      _pf(positions=positions), last_price=200.0, now=WEEKDAY_1000)
    assert not d.approved
    assert "max_positions" in d.failed_checks


def test_gross_exposure_limit(tmp_path):
    cfg = make_cfg(tmp_path, **{"risk.max_gross_exposure_pct": 0.20,
                                "risk.max_position_pct": 0.9,
                                "risk.max_order_notional": 1_000_000})
    risk = RiskController(cfg, cfg.gate)
    # 已有持仓市值 150k / 权益 1M = 15%，再买 100k → 25% > 20%
    d = risk.evaluate(_buy(qty=333, price=300.0, sl=290.0),
                      _pf(positions=[pos("hk00700", 500, 300, 300)]),
                      last_price=300.0, now=WEEKDAY_1000)
    assert not d.approved
    assert "max_gross_exposure" in d.failed_checks


def test_size_position_binds_on_risk_budget(tmp_path):
    cfg = make_cfg(tmp_path, **{"risk.max_order_notional": 5_000_000})
    risk = RiskController(cfg, cfg.gate)
    # 权益 1M，单笔风险 1% = 10k；每股风险 10 元 → 1000 股；再受 15% 仓位上限 → 500 股
    out = risk.size_position(equity=1_000_000, cash=1_000_000, price=300.0,
                             stop_loss=290.0, confidence=1.0, lot_size=100)
    assert out["qty"] == 500
    assert out["binding"] == "pct_limited"

    out2 = risk.size_position(equity=1_000_000, cash=1_000_000, price=300.0,
                              stop_loss=250.0, confidence=1.0, lot_size=100)
    assert out2["binding"] == "risk_limited"
    assert out2["qty"] == 200      # 10k / 50 = 200 股


def test_size_position_is_capped_by_single_order_notional(risk):
    # 默认 max_order_notional=50000 → 300 元股票最多 166 股，取整到 100
    out = risk.size_position(equity=1_000_000, cash=1_000_000, price=300.0,
                             stop_loss=290.0, confidence=1.0, lot_size=100)
    assert out["binding"] == "max_notional"
    assert out["qty"] == 100


def test_size_position_rounds_down_to_lot(risk):
    out = risk.size_position(equity=1_000_000, cash=1_000_000, price=300.0,
                             stop_loss=295.0, confidence=1.0, lot_size=100)
    assert out["qty"] % 100 == 0


def test_size_position_scales_with_confidence(risk):
    a = risk.size_position(equity=1_000_000, cash=1_000_000, price=300.0,
                           stop_loss=290.0, confidence=1.0, lot_size=100)["qty"]
    b = risk.size_position(equity=1_000_000, cash=1_000_000, price=300.0,
                           stop_loss=290.0, confidence=0.5, lot_size=100)["qty"]
    assert b < a


# ---------------------------------------------------------------- 单笔风险 / 止损
def test_buy_without_stop_loss_is_rejected(risk):
    intent = _buy(sl=None)
    d = risk.evaluate(intent, _pf(), last_price=300.0, now=WEEKDAY_1000)
    assert not d.approved
    assert "per_trade_risk" in d.failed_checks
    assert any("止损" in r for r in d.reasons)


def test_stop_loss_on_wrong_side_is_rejected(risk):
    d = risk.evaluate(_buy(sl=310.0), _pf(), last_price=300.0, now=WEEKDAY_1000)
    assert not d.approved
    assert "stop_side" in d.failed_checks


def test_stop_distance_too_wide_is_rejected(risk):
    # 300 → 250 元，距离 16.7% > 12%
    d = risk.evaluate(_buy(sl=250.0), _pf(), last_price=300.0, now=WEEKDAY_1000)
    assert not d.approved
    assert "max_stop_distance" in d.failed_checks


def test_per_trade_risk_budget_enforced(risk):
    # 每股风险 90 元 × 100 股 = 9000 < 10000 预算，放行
    ok = risk.evaluate(_buy(qty=100, price=300.0, sl=210.0), _pf(), last_price=300.0,
                       now=WEEKDAY_1000)
    assert "max_stop_distance" in ok.failed_checks  # 距离 30% 过宽，先被拦
    # 每股风险 10 元 × 2000 股 = 20000 > 10000 预算
    bad = risk.evaluate(_buy(qty=2000, price=300.0, sl=290.0),
                        _pf(equity=1_000_000.0), last_price=300.0, now=WEEKDAY_1000)
    assert not bad.approved
    assert "per_trade_risk" in bad.failed_checks


# ---------------------------------------------------------------- 单日亏损
def test_daily_loss_limit_blocks_new_entries(risk):
    d = risk.evaluate(_buy(), _pf(day_pnl=-35_000.0), last_price=300.0, now=WEEKDAY_1000)
    assert not d.approved
    assert "daily_loss_limit" in d.failed_checks


def test_daily_loss_within_limit_passes(risk):
    d = risk.evaluate(_buy(), _pf(day_pnl=-5_000.0), last_price=300.0, now=WEEKDAY_1000)
    assert d.approved, d.reasons


def test_daily_order_count_limit(tmp_path):
    cfg = make_cfg(tmp_path, **{"risk.max_daily_orders": 1})
    risk = RiskController(cfg, cfg.gate)
    d = risk.evaluate(_buy(), _pf(day_orders=1), last_price=300.0, now=WEEKDAY_1000)
    assert not d.approved
    assert "daily_order_count" in d.failed_checks


# ---------------------------------------------------------------- 重复订单
def test_duplicate_order_in_cooldown_is_blocked(risk):
    risk.note_order("hk00700", "buy", 100, now=WEEKDAY_1000)
    d = risk.evaluate(_buy(), _pf(), last_price=300.0,
                      now=WEEKDAY_1000.replace(second=30))
    assert not d.approved
    assert "duplicate_order" in d.failed_checks


def test_duplicate_order_after_cooldown_is_allowed(risk):
    risk.note_order("hk00700", "buy", 100, now=WEEKDAY_1000)
    later = WEEKDAY_1000.replace(minute=50)
    d = risk.evaluate(_buy(), _pf(), last_price=300.0, now=later)
    assert "duplicate_order" not in d.failed_checks, d.reasons


def test_pending_same_side_order_blocks_new_one(risk):
    recent = [{"symbol": "hk00700", "side": "buy", "status": "submitted",
               "order_id": "P123"}]
    d = risk.evaluate(_buy(), _pf(), last_price=300.0, recent_orders=recent,
                      now=WEEKDAY_1000)
    assert not d.approved
    assert "duplicate_order" in d.failed_checks


# ---------------------------------------------------------------- 频率
def test_rate_limit(tmp_path):
    cfg = make_cfg(tmp_path, **{"risk.max_orders_per_minute": 2})
    risk = RiskController(cfg, cfg.gate)
    for _ in range(2):
        risk.note_order("hk00700", "buy", 100, now=WEEKDAY_1000)
    d = risk.evaluate(_buy(), _pf(), last_price=300.0, now=WEEKDAY_1000)
    assert not d.approved
    assert "rate_limit" in d.failed_checks


# ---------------------------------------------------------------- 价格 / 现金 / 时段
def test_price_deviation_rejected(risk):
    d = risk.evaluate(_buy(price=340.0, sl=330.0), _pf(), last_price=300.0,
                      now=WEEKDAY_1000)
    assert not d.approved
    assert "price_deviation" in d.failed_checks


def test_insufficient_cash_rejected(risk):
    d = risk.evaluate(_buy(qty=100, price=300.0), _pf(cash=1_000.0),
                      last_price=300.0, now=WEEKDAY_1000)
    assert not d.approved
    assert "cash_sufficient" in d.failed_checks


def test_naked_short_rejected(risk):
    intent = OrderIntent(symbol="hk00700", side="sell", qty=100, price=300.0,
                         market="hk", currency="HKD")
    d = risk.evaluate(intent, _pf(), last_price=300.0, now=WEEKDAY_1000)
    assert not d.approved
    assert "no_naked_short" in d.failed_checks


def test_sell_with_position_allowed_even_on_weekend(risk):
    intent = OrderIntent(symbol="hk00700", side="sell", qty=100, price=300.0,
                         market="hk", currency="HKD", is_exit=True)
    d = risk.evaluate(intent, _pf(positions=[pos(qty=100, last=300.0)]),
                      last_price=300.0, now=WEEKEND_1000)
    assert d.approved, d.reasons


def test_market_hours_block_on_weekend(risk):
    d = risk.evaluate(_buy(), _pf(), last_price=300.0, now=WEEKEND_1000)
    assert not d.approved
    assert "market_hours" in d.failed_checks


def test_market_hours_helper():
    assert is_market_open("hk", WEEKDAY_1000)[0] is True
    assert is_market_open("hk", WEEKDAY_1000.replace(hour=12, minute=30))[0] is False
    # US-P0-1 修正：美股时段按**纽约当地时间**判断，不再是"北京 21:30 开盘"。
    # 2026-11-04 处于 EST（UTC-5）：
    #   北京 22:00 = 纽约 09:00 → 盘前，未开市（旧实现错判为开市）
    #   北京 22:30 = 纽约 09:30 → 常规时段开盘
    assert is_market_open("us", WEEKDAY_1000.replace(hour=22, minute=0))[0] is False
    assert is_market_open("us", WEEKDAY_1000.replace(hour=22, minute=30))[0] is True
    assert is_market_open("hk", WEEKEND_1000)[0] is False


def test_min_notional_rejects_dust(risk):
    d = risk.evaluate(_buy(qty=1, price=5.0, sl=4.9), _pf(), last_price=5.0,
                      now=WEEKDAY_1000)
    assert not d.approved
    assert "min_notional" in d.failed_checks


def test_symbol_outside_universe_rejected(risk):
    intent = OrderIntent(symbol="usZZZZ", side="buy", qty=10, price=100.0,
                         market="us", currency="USD", stop_loss=95.0)
    d = risk.evaluate(intent, _pf(), last_price=100.0, now=WEEKDAY_1000)
    assert not d.approved
    assert "symbol_allowlist" in d.failed_checks


# ---------------------------------------------------------------- 紧急停止
def test_kill_switch_blocks_everything(risk):
    risk.halt("测试紧急停止")
    d = risk.evaluate(_buy(), _pf(), last_price=300.0, now=WEEKDAY_1000)
    assert not d.approved
    assert "kill_switch" in d.failed_checks
    assert risk.gate.state.kill_switch is True


def test_kill_switch_still_allows_exit(risk):
    """止损/减仓在紧急停止下仍然允许（降风险动作不应被锁死）。"""
    risk.halt("测试紧急停止")
    intent = OrderIntent(symbol="hk00700", side="sell", qty=100, price=300.0,
                         market="hk", currency="HKD", is_exit=True)
    d = risk.evaluate(intent, _pf(positions=[pos(qty=100, last=300.0)]),
                      last_price=300.0, now=WEEKDAY_1000)
    # kill_switch 检查会对所有意图生效（保守），但 trading_permission 放行减仓
    assert "trading_permission" not in d.failed_checks


def test_resume_clears_halt(risk):
    from ai_trader.safety.halt import RESUME_PHRASE

    risk.halt("x")
    risk.resume(RESUME_PHRASE)
    assert risk.gate.state.kill_switch is False
    d = risk.evaluate(_buy(), _pf(), last_price=300.0, now=WEEKDAY_1000)
    assert d.approved, d.reasons


# ---------------------------------------------------------------- 止损止盈扫描
def test_scan_exits_triggers_stop_loss(risk, paper):
    positions = [pos(qty=100, avg_cost=380.0, last=385.0)]
    quote = paper.get_quote("hk00700")
    stops = {"hk00700": {"stop_loss": quote.last * 1.05, "take_profit": None}}
    intents = risk.scan_exits(positions, stops, {"hk00700": quote})
    assert len(intents) == 1
    assert intents[0].side == "sell"
    assert intents[0].qty == 100
    assert "止损" in intents[0].reason
    assert intents[0].is_exit is True


def test_scan_exits_triggers_take_profit(risk, paper):
    positions = [pos(qty=100, avg_cost=380.0, last=385.0)]
    quote = paper.get_quote("hk00700")
    stops = {"hk00700": {"stop_loss": quote.last * 0.5, "take_profit": quote.last * 0.95}}
    intents = risk.scan_exits(positions, stops, {"hk00700": quote})
    assert len(intents) == 1
    assert "止盈" in intents[0].reason


def test_scan_exits_no_trigger_inside_band(risk, paper):
    positions = [pos(qty=100)]
    quote = paper.get_quote("hk00700")
    stops = {"hk00700": {"stop_loss": quote.last * 0.8, "take_profit": quote.last * 1.2}}
    assert risk.scan_exits(positions, stops, {"hk00700": quote}) == []


def test_fail_closed_on_internal_error(risk):
    """风控内部异常必须视为拒绝，而不是放行。"""
    class Boom:
        @property
        def position_count(self):
            raise RuntimeError("模拟内部故障")
        currency = "HKD"
        equity = 1e6
        cash = 1e6
        day_pnl = 0
        day_trades = 0
        day_orders = 0
        positions = []
        market_value = 0.0

        def position_of(self, symbol):
            raise RuntimeError("模拟内部故障")

    d = risk.evaluate(_buy(), Boom(), last_price=300.0, now=WEEKDAY_1000)
    assert not d.approved
    assert any("内部异常" in r for r in d.reasons)
