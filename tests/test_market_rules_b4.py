"""B4：A 股通交易规则测试。

覆盖：
  1. T+1（当日买入不可卖 / 次日可卖）
  2. 涨跌停边界（含 0.01 元四舍五入边界）
  3. 分板块差异（主板 10% / 科创 20% / 创业 20% / 北交所 30% / 主板 ST 10%）
  4. 停牌、节假日、非交易时段
  5. 最小交易单位与数量规则（含零股卖出）
  6. 买入资金 / 卖出可用数量
  7. 规则不明确时拒绝（未知板块 / UNVERIFIED / 日历 UNKNOWN）
"""
from __future__ import annotations

from datetime import datetime

import pytest

from ai_trader.market.rules import (
    CODE_CALENDAR,
    CODE_LOT_SIZE,
    CODE_NO_PRICE_LIMIT_DATA,
    CODE_PRICE_LIMIT,
    CODE_RULE_UNKNOWN,
    CODE_RULE_UNVERIFIED,
    CODE_SELLABLE,
    CODE_SESSION,
    CODE_SUSPENDED,
    CODE_T_PLUS,
    STATUS_UNKNOWN,
    STATUS_UNVERIFIED,
    STATUS_VERIFIED,
    MarketRuleSet,
    round_price,
)
from ai_trader.risk.controller import OrderIntent, Portfolio, RiskController
from tests.conftest import WEEKDAY_1000, make_cfg


# --------------------------------------------------------------------------- #
# 构造规则集（显式、可就地覆盖）
# --------------------------------------------------------------------------- #
def _board(limit, lot, inc, t_plus=1, status=STATUS_VERIFIED, market="cn",
           sell="all_at_once"):
    return {
        "name": "test-board", "market": market, "price_limit_pct": limit,
        "lot_size": lot, "buy_increment": inc, "sell_odd_lot": sell,
        "t_plus": t_plus,
        "meta": {"status": status, "source": "unit-test",
                 "effective_from": "2026-07-06", "source_tier": "secondary",
                 "note": ""},
    }


def _ruleset(**over):
    raw = {
        "enabled": True,
        "enforce": True,
        "enforce_markets": ["cn"],
        "allow_unverified_rules": True,
        "boards": {
            "cn_main": _board(0.10, 100, 100),
            "cn_st_main": _board(0.10, 100, 100),
            "cn_star": _board(0.20, 200, 1),
            "cn_chinext": _board(0.20, 100, 100),
            "cn_bj": _board(0.30, 100, 100),
            "cn_unknown": _board(0.10, 100, 100, status=STATUS_UNKNOWN),
            "cn_unverified": _board(0.10, 100, 100, status=STATUS_UNVERIFIED),
            "hk": _board(None, 1, 1, t_plus=0, market="hk", sell="any"),
        },
        "symbol_prefixes": [
            {"prefix": "cn688", "board": "cn_star"},
            {"prefix": "cn300", "board": "cn_chinext"},
            {"prefix": "cn83", "board": "cn_bj"},
            {"prefix": "cn99", "board": "cn_unknown"},
            {"prefix": "cn98", "board": "cn_unverified"},
            {"prefix": "cn6", "board": "cn_main"},
            {"prefix": "cn0", "board": "cn_main"},
            {"prefix": "hk", "board": "hk"},
        ],
        "suspended_symbols": [],
        "calendar": {"status": STATUS_VERIFIED, "strict": True,
                     "holidays": {"cn": []}},
    }
    raw.update(over)
    return MarketRuleSet(raw)


def _check(rs, **kw):
    base = dict(symbol="cn600519", side="buy", qty=100, price=10.0,
                prev_close=10.0, position_qty=0.0, today_bought_qty=0.0,
                now=WEEKDAY_1000, is_exit=False)
    base.update(kw)
    return rs.check_order(**base)


def _codes(result):
    return [v.code for v in result.violations]


# --------------------------------------------------------------------------- #
# 1. T+1
# --------------------------------------------------------------------------- #
def test_t_plus_one_rejects_same_day_sell():
    rs = _ruleset()
    # 昨日持仓 100，今日又买 100 → 可卖 100，卖 100 可以
    r = _check(rs, side="sell", qty=100, position_qty=200, today_bought_qty=100)
    assert CODE_T_PLUS not in _codes(r) and CODE_SELLABLE not in _codes(r), r.violations

    # 今日买入 100 且无昨日持仓 → 卖 100 必须拒绝
    r2 = _check(rs, side="sell", qty=100, position_qty=100, today_bought_qty=100)
    assert CODE_T_PLUS in _codes(r2) or CODE_SELLABLE in _codes(r2), r2.violations
    assert any("T+1" in v.detail for v in r2.violations)


def test_t_plus_one_allows_next_day_sell():
    rs = _ruleset()
    # 次日：today_bought 归零，持仓 100 → 可卖 100
    r = _check(rs, side="sell", qty=100, position_qty=100, today_bought_qty=0.0,
               price=10.0, prev_close=10.0)
    assert r.ok, r.violations


def test_t_plus_zero_market_has_no_restriction():
    rs = _ruleset()
    r = _check(rs, symbol="hk00700", side="sell", qty=100, position_qty=100,
               today_bought_qty=100, price=300.0, prev_close=300.0)
    assert r.applied is False, "hk 未纳入强制校验"


# --------------------------------------------------------------------------- #
# 2. 涨跌停
# --------------------------------------------------------------------------- #
def test_price_limit_boundary_main_board():
    rs = _ruleset()
    # 昨收 10.00 → 涨停 11.00 / 跌停 9.00
    assert _check(rs, qty=100, price=11.00, prev_close=10.0).ok
    assert _check(rs, qty=100, price=9.00, prev_close=10.0).ok
    assert CODE_PRICE_LIMIT in _codes(_check(rs, qty=100, price=11.01, prev_close=10.0))
    assert CODE_PRICE_LIMIT in _codes(_check(rs, qty=100, price=8.99, prev_close=10.0))


def test_price_limit_rounding_half_up():
    """昨收 9.05 → 涨停 9.96（9.955 四舍五入），不是 9.95。"""
    assert round_price(9.955) == 9.96
    assert round_price(10.005) == 10.01
    rs = _ruleset()
    r = _check(rs, qty=100, price=9.96, prev_close=9.05)
    assert CODE_PRICE_LIMIT not in _codes(r), r.violations
    r2 = _check(rs, qty=100, price=9.97, prev_close=9.05)
    assert CODE_PRICE_LIMIT in _codes(r2)


def test_price_limit_missing_prev_close_is_rejected():
    rs = _ruleset()
    r = _check(rs, qty=100, price=10.0, prev_close=None)
    assert CODE_NO_PRICE_LIMIT_DATA in _codes(r)
    assert any("缺少昨收价" in v.detail for v in r.violations)


# --------------------------------------------------------------------------- #
# 3. 分板块差异
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("symbol,board,limit", [
    ("cn600519", "cn_main", 0.10),
    ("cn688981", "cn_star", 0.20),
    ("cn300750", "cn_chinext", 0.20),
    ("cn830799", "cn_bj", 0.30),
])
def test_board_price_limit_differs(symbol, board, limit):
    rs = _ruleset()
    assert rs.resolve_board_id(symbol) == board
    prev = 100.0
    # 科创板最小 200 股，其余板块 100 股
    qty = 200 if board == "cn_star" else 100
    up = round_price(prev * (1 + limit))
    down = round_price(prev * (1 - limit))
    assert _check(rs, symbol=symbol, qty=qty, price=up, prev_close=prev).ok
    assert CODE_PRICE_LIMIT in _codes(
        _check(rs, symbol=symbol, qty=qty, price=up + 0.02, prev_close=prev))
    assert _check(rs, symbol=symbol, qty=qty, price=down, prev_close=prev).ok


def test_st_board_uses_ten_percent_after_20260706():
    """2026-07-06 起主板 ST 由 ±5% 调整为 ±10%（不能再用 5%）。"""
    rs = _ruleset(st_symbols={"cn600000": "cn_st_main"})
    assert rs.resolve_board_id("cn600000") == "cn_st_main"
    rule = rs.boards["cn_st_main"]
    assert rule.price_limit_pct == pytest.approx(0.10)
    assert rule.meta.effective_from == "2026-07-06"
    # 11.00 在 ±10% 内（若仍是 5%，10.50 就会超限）
    assert _check(rs, symbol="cn600000", qty=100, price=11.00, prev_close=10.0).ok


def test_star_board_lot_and_increment():
    rs = _ruleset()
    # 科创板：200 股起，1 股递增
    assert _check(rs, symbol="cn688981", qty=200, price=50.0, prev_close=50.0).ok
    assert _check(rs, symbol="cn688981", qty=201, price=50.0, prev_close=50.0).ok
    assert CODE_LOT_SIZE in _codes(
        _check(rs, symbol="cn688981", qty=150, price=50.0, prev_close=50.0))


# --------------------------------------------------------------------------- #
# 4. 停牌 / 节假日 / 时段
# --------------------------------------------------------------------------- #
def test_suspended_symbol_rejected():
    rs = _ruleset(suspended_symbols=["cn600519"])
    r = _check(rs, qty=100, price=10.0)
    assert CODE_SUSPENDED in _codes(r)
    # 卖出同样禁止
    r2 = _check(rs, side="sell", qty=100, position_qty=100, price=10.0)
    assert CODE_SUSPENDED in _codes(r2)


def test_holiday_rejected():
    rs = _ruleset(calendar={"status": STATUS_VERIFIED, "strict": True,
                            "holidays": {"cn": ["2026-11-04"]}})
    r = _check(rs, qty=100, price=10.0)
    assert CODE_CALENDAR in _codes(r)
    assert any("节假日" in v.detail for v in r.violations)


def test_weekend_rejected():
    rs = _ruleset(calendar={"status": STATUS_VERIFIED, "strict": True,
                            "holidays": {"cn": []}})
    saturday = datetime(2026, 11, 7, 10, 0, 0)
    r = _check(rs, qty=100, price=10.0, now=saturday)
    assert CODE_CALENDAR in _codes(r)
    assert any("周末" in v.detail for v in r.violations)


def test_out_of_session_rejected():
    rs = _ruleset(calendar={"status": STATUS_VERIFIED, "strict": True,
                            "holidays": {"cn": []}})
    lunch = datetime(2026, 11, 4, 12, 0, 0)
    r = _check(rs, qty=100, price=10.0, now=lunch)
    assert CODE_SESSION in _codes(r)


def test_calendar_unknown_strict_blocks_trading():
    rs = _ruleset(calendar={"status": STATUS_UNKNOWN, "strict": True,
                            "holidays": {"cn": []}})
    r = _check(rs, qty=100, price=10.0)
    assert CODE_CALENDAR in _codes(r)
    assert any("UNKNOWN" in v.detail for v in r.violations)


def test_calendar_unknown_non_strict_allows_weekday():
    rs = _ruleset(calendar={"status": STATUS_UNKNOWN, "strict": False,
                            "holidays": {"cn": []}})
    r = _check(rs, qty=100, price=10.0)
    assert CODE_CALENDAR not in _codes(r)


# --------------------------------------------------------------------------- #
# 5. 数量规则
# --------------------------------------------------------------------------- #
def test_buy_quantity_must_be_multiple_of_lot():
    rs = _ruleset(calendar={"status": STATUS_VERIFIED, "strict": True,
                            "holidays": {"cn": []}})
    assert CODE_LOT_SIZE in _codes(_check(rs, qty=150, price=10.0))
    assert _check(rs, qty=200, price=10.0).ok
    assert CODE_LOT_SIZE in _codes(_check(rs, qty=0, price=10.0))


def test_sell_odd_lot_must_be_all_at_once():
    rs = _ruleset(calendar={"status": STATUS_VERIFIED, "strict": True,
                            "holidays": {"cn": []}})
    # 持仓 150（零股）：卖 150 = 全部 → 允许；卖 50 → 拒绝
    assert _check(rs, side="sell", qty=150, position_qty=150,
                  price=10.0, prev_close=10.0).ok
    assert CODE_LOT_SIZE in _codes(
        _check(rs, side="sell", qty=50, position_qty=150,
               price=10.0, prev_close=10.0))


# --------------------------------------------------------------------------- #
# 6. 可用数量
# --------------------------------------------------------------------------- #
def test_sell_exceeding_position_rejected():
    rs = _ruleset(calendar={"status": STATUS_VERIFIED, "strict": True,
                            "holidays": {"cn": []}})
    r = _check(rs, side="sell", qty=200, position_qty=100, price=10.0)
    assert CODE_SELLABLE in _codes(r)


def test_sellable_ignores_today_bought():
    rs = _ruleset(calendar={"status": STATUS_VERIFIED, "strict": True,
                            "holidays": {"cn": []}})
    r = _check(rs, side="sell", qty=100, position_qty=300, today_bought_qty=200,
               price=10.0)
    assert r.ok, r.violations     # 可卖 100，卖 100 正好
    r2 = _check(rs, side="sell", qty=200, position_qty=300, today_bought_qty=200,
                price=10.0)
    assert CODE_SELLABLE in _codes(r2) or CODE_T_PLUS in _codes(r2)


# --------------------------------------------------------------------------- #
# 7. 规则不明确 → 拒绝
# --------------------------------------------------------------------------- #
def test_unknown_symbol_rejected():
    rs = _ruleset()
    r = _check(rs, symbol="xx123456", qty=100, price=10.0)
    assert CODE_RULE_UNKNOWN in _codes(r)
    assert any("无法解析" in v.detail for v in r.violations)


def test_board_with_unknown_status_rejected():
    rs = _ruleset()
    r = _check(rs, symbol="cn990001", qty=100, price=10.0)
    assert CODE_RULE_UNKNOWN in _codes(r)
    assert any("UNKNOWN" in v.detail for v in r.violations)


def test_unverified_rule_rejected_by_default():
    rs = _ruleset(allow_unverified_rules=False)
    r = _check(rs, symbol="cn980001", qty=100, price=10.0)
    assert CODE_RULE_UNVERIFIED in _codes(r)

    rs2 = _ruleset(allow_unverified_rules=True)
    r2 = _check(rs2, symbol="cn980001", qty=100, price=10.0)
    assert CODE_RULE_UNVERIFIED not in _codes(r2)


def test_unverified_rules_listed_for_report():
    rs = _ruleset()
    items = rs.unverified_rules()
    assert any(i["board"] == "cn_unknown" for i in items)


# --------------------------------------------------------------------------- #
# 8. 风控集成
# --------------------------------------------------------------------------- #
def _risk(tmp_path, **mr_over):
    over = {"risk.enforce_market_hours": False,
            "market_rules.enforce_markets": ["cn"],
            "market_rules.allow_unverified_rules": True,
            "market_rules.calendar.status": STATUS_VERIFIED,
            "market_rules.calendar.strict": True,
            "market_rules.calendar.holidays.cn": [],
            "market_rules.boards": {
                "cn_main": _board(0.10, 100, 100),
                "hk": _board(None, 1, 1, t_plus=0, market="hk", sell="any"),
            },
            "market_rules.symbol_prefixes": [
                {"prefix": "cn6", "board": "cn_main"},
                {"prefix": "hk", "board": "hk"},
            ],
            "risk.max_order_notional": 10_000_000,
            "risk.max_position_pct": 1.0,
            "risk.max_gross_exposure_pct": 1.0,
            }
    over.update(mr_over)
    cfg = make_cfg(tmp_path, **over)
    return RiskController(cfg, cfg.gate)


def _pf(qty=0.0, today=0.0, equity=1_000_000.0):
    from ai_trader.broker.base import Position

    positions = []
    if qty:
        positions.append(Position(symbol="cn600519", market="cn", qty=qty,
                                  avg_cost=10.0, last_price=10.0, currency="CNY"))
    return Portfolio(currency="CNY", equity=equity, cash=equity,
                     positions=positions,
                     today_bought={"cn600519": today} if today else {})


def test_risk_rejects_t_plus_one_sell(tmp_path):
    risk = _risk(tmp_path)
    intent = OrderIntent(symbol="cn600519", side="sell", qty=100, price=10.0,
                         market="cn", currency="CNY")
    d = risk.evaluate(intent, _pf(qty=100, today=100), last_price=10.0,
                      now=WEEKDAY_1000, prev_close=10.0, reserve_attempt=False)
    assert d.approved is False
    names = [c.name for c in d.checks if not c.ok]
    assert any(n.startswith("rule:") for n in names), names


def test_risk_allows_next_day_sell(tmp_path):
    risk = _risk(tmp_path)
    intent = OrderIntent(symbol="cn600519", side="sell", qty=100, price=10.0,
                         market="cn", currency="CNY")
    d = risk.evaluate(intent, _pf(qty=100, today=0), last_price=10.0,
                      now=WEEKDAY_1000, prev_close=10.0, reserve_attempt=False)
    rule_fails = [c for c in d.checks if not c.ok and c.name.startswith("rule:")]
    assert not rule_fails, [c.as_dict() for c in rule_fails]


def test_risk_rejects_bad_lot(tmp_path):
    risk = _risk(tmp_path)
    intent = OrderIntent(symbol="cn600519", side="buy", qty=150, price=10.0,
                         market="cn", currency="CNY", stop_loss=9.5)
    d = risk.evaluate(intent, _pf(), last_price=10.0, now=WEEKDAY_1000,
                      prev_close=10.0, reserve_attempt=False)
    assert any(c.name == "rule:LOT_SIZE" for c in d.checks if not c.ok)


def test_risk_records_unverified_market_note(tmp_path):
    """hk 未纳入强制校验时，必须留下"未校验"的痕迹而不是假装通过。"""
    risk = _risk(tmp_path)
    intent = OrderIntent(symbol="hk00700", side="buy", qty=100, price=300.0,
                         market="hk", currency="HKD", stop_loss=290.0)
    d = risk.evaluate(intent, _pf(), last_price=300.0, now=WEEKDAY_1000,
                      prev_close=298.0, reserve_attempt=False)
    mr = [c for c in d.checks if c.name == "market_rules"]
    assert mr and "未强制校验" in mr[0].detail


# --------------------------------------------------------------------------- #
# 9. 原有实现的 fail-open 修正
# --------------------------------------------------------------------------- #
def test_market_hours_unknown_market_is_fail_closed():
    """B4 审计修正：未配置时段的市场必须拒绝，而不是"默认放行"。"""
    from ai_trader.risk.controller import is_market_open

    ok, why = is_market_open("xx", WEEKDAY_1000)
    assert ok is False
    assert "fail-closed" in why


def test_market_rules_session_missing_is_fail_closed():
    rs = _ruleset()
    ok, why = rs.session_status("xx", WEEKDAY_1000)
    assert ok is False
    assert "未配置交易时段" in why
