"""US-P0-1 ~ US-P0-4 验收测试。

对应审计项：

* **US-P0-1** 用 ``zoneinfo`` 的 ``America/New_York`` 按**纽约当地时间**判断
  交易日期/星期/常规时段；覆盖 DST、冬令时、跨日、周末、边界时间。
* **US-P0-2** ``enforce_markets`` 纳入 ``us``；价格精度、下单数量、交易时段
  检查真正生效，不能通过 ``applied=False`` 绕过。
* **US-P0-3** ``get_positions`` 传入 ``use_us_pre / use_us_post / use_us_night``；
  各时段持仓全部进入风控；查询失败必须 fail-closed。
* **US-P0-4** 止损/撤单/未知单/持仓读取失败等风险处置路径不被行情或
  交易时段错误阻断；不得因查询失败假定"没有这张单/没有这个持仓"。

原则：**不 mock 时区**。测试直接使用真实 ``zoneinfo`` 与真实 IANA tzdata，
时间一律显式带时区（纽约或北京），因此结果与本机时区无关。
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from ai_trader.audit.journal import AuditJournal
from ai_trader.broker.base import BrokerError, OrderResult, Position, Quote
from ai_trader.broker.fosun_adapter import FosunBroker
from ai_trader.market import clock as clock_mod
from ai_trader.market.clock import PHASE_POST, PHASE_PRE, PHASE_WEEKEND, ClockError
from ai_trader.market.rules import (
    CODE_CALENDAR,
    CODE_LOT_SIZE,
    CODE_MARKET_NOT_ENFORCED,
    CODE_PRICE_TICK,
    CODE_RULE_UNKNOWN,
    CODE_RULE_UNVERIFIED,
    CODE_SESSION,
    CODE_T_PLUS,
    STATUS_UNKNOWN,
    STATUS_UNVERIFIED,
    STATUS_VERIFIED,
    MarketRuleSet,
)
from ai_trader.risk.controller import OrderIntent, Portfolio, RiskController
from ai_trader.strategy.engine import StrategyEngine
from tests.conftest import make_cfg
from tests.helpers import FixedSignalModel, ScriptedBroker, live_cfg

TZ_NY = ZoneInfo("America/New_York")
TZ_BJ = ZoneInfo("Asia/Shanghai")


def ny(y, m, d, H, M) -> datetime:
    return datetime(y, m, d, H, M, tzinfo=TZ_NY)


def bj(y, m, d, H, M) -> datetime:
    return datetime(y, m, d, H, M, tzinfo=TZ_BJ)


# --------------------------------------------------------------------------- #
# 规则集 / 风控构造
# --------------------------------------------------------------------------- #
def _ruleset(**over) -> MarketRuleSet:
    raw = {
        "enabled": True,
        "enforce": True,
        "enforce_markets": ["us"],
        "allow_unverified_rules": False,
        "unenforced_market_policy": "warn",
        "sessions": {
            "us": {"tz": "America/New_York",
                   "regular": [["09:30", "16:00"]],
                   "pre": ["04:00", "09:30"],
                   "post": ["16:00", "20:00"],
                   "night": ["20:00", "04:00"],
                   "extended_tradable": False},
            "cn": {"tz": "Asia/Shanghai",
                   "regular": [["09:30", "11:30"], ["13:00", "15:00"]]},
            "hk": {"tz": "Asia/Hong_Kong",
                   "regular": [["09:30", "12:00"], ["13:00", "16:00"]]},
        },
        "boards": {
            "us": {
                "name": "美股", "market": "us", "price_limit_pct": None,
                "lot_size": 1, "buy_increment": 1, "sell_odd_lot": "any",
                "t_plus": 0, "tick_size": 0.01, "tick_size_below_1": 0.0001,
                "meta": {"status": STATUS_VERIFIED, "source": "17 CFR 242.612",
                         "effective_from": "2026-01-06", "source_tier": "primary",
                         "note": "unit-test"},
            },
            "hk": {
                "name": "港股", "market": "hk", "price_limit_pct": None,
                "lot_size": 100, "buy_increment": 100, "sell_odd_lot": "any",
                "t_plus": 0,
                "meta": {"status": STATUS_VERIFIED, "source": "unit-test",
                         "effective_from": "", "source_tier": "secondary", "note": ""},
            },
            "cn_main": {
                "name": "沪深主板", "market": "cn", "price_limit_pct": 0.10,
                "lot_size": 100, "buy_increment": 100, "sell_odd_lot": "all_at_once",
                "t_plus": 1,
                "meta": {"status": STATUS_VERIFIED, "source": "unit-test",
                         "effective_from": "2026-07-06", "source_tier": "secondary",
                         "note": ""},
            },
        },
        "symbol_prefixes": [
            {"prefix": "us", "board": "us"},
            {"prefix": "hk", "board": "hk"},
            {"prefix": "cn6", "board": "cn_main"},
        ],
        "calendar": {
            "status": STATUS_VERIFIED, "strict": True,
            "markets": {"us": {"status": STATUS_VERIFIED,
                               "source": "NYSE",
                               "covered_from": "2026-01-01",
                               "covered_until": "2026-12-31",
                               "holidays": ["2026-11-26", "2026-12-25"],
                               "early_close": {"2026-11-27": "13:00",
                                               "2026-12-24": "13:00"}}},
            "holidays": {"cn": [], "hk": []},
        },
    }
    raw.update(over)
    return MarketRuleSet(raw)


def _check(rs: MarketRuleSet, **kw):
    base = dict(symbol="usAAPL", side="buy", qty=10, price=200.0,
                prev_close=200.0, position_qty=0.0, today_bought_qty=0.0,
                now=ny(2026, 11, 4, 10, 0), is_exit=False)
    base.update(kw)
    return rs.check_order(**base)


def _codes(result) -> list:
    return [v.code for v in result.violations]


# --------------------------------------------------------------------------- #
# 1. DST（夏令时）下的开盘边界
# --------------------------------------------------------------------------- #
def test_dst_regular_open_boundary():
    """2026-07-15 处于 EDT（UTC-4）。"""
    rs = _ruleset()
    assert ny(2026, 7, 15, 12, 0).utcoffset().total_seconds() == -4 * 3600

    # 09:29 → 盘前，未开市
    ok, why = rs.session_status("us", ny(2026, 7, 15, 9, 29))
    assert ok is False and PHASE_PRE in why

    # 09:30 → 开盘（边界含开盘时刻）
    ok, why = rs.session_status("us", ny(2026, 7, 15, 9, 30))
    assert ok is True, why

    # 15:59 → 仍在常规时段
    assert rs.session_status("us", ny(2026, 7, 15, 15, 59))[0] is True
    # 16:00 → 收盘（边界不含收盘时刻）
    ok, why = rs.session_status("us", ny(2026, 7, 15, 16, 0))
    assert ok is False and PHASE_POST in why


def test_dst_beijing_equivalents():
    """EDT 下纽约 09:30 = 北京 21:30；冬令时则应变成 22:30。"""
    rs = _ruleset()
    assert rs.session_status("us", bj(2026, 7, 15, 21, 29))[0] is False
    assert rs.session_status("us", bj(2026, 7, 15, 21, 30))[0] is True
    # 尾盘：纽约 15:59 = 北京次日 03:59
    assert rs.session_status("us", bj(2026, 7, 16, 3, 59))[0] is True
    assert rs.session_status("us", bj(2026, 7, 16, 4, 0))[0] is False


# --------------------------------------------------------------------------- #
# 2. 冬令时（EST）下的开盘边界
# --------------------------------------------------------------------------- #
def test_standard_time_regular_open_boundary():
    """2026-11-04 处于 EST（UTC-5）。"""
    rs = _ruleset()
    assert ny(2026, 11, 4, 12, 0).utcoffset().total_seconds() == -5 * 3600

    assert rs.session_status("us", ny(2026, 11, 4, 9, 29))[0] is False
    assert rs.session_status("us", ny(2026, 11, 4, 9, 30))[0] is True
    assert rs.session_status("us", ny(2026, 11, 4, 15, 59))[0] is True
    assert rs.session_status("us", ny(2026, 11, 4, 16, 0))[0] is False

    # 北京时间下的冬令时开盘点：22:29 未开、22:30 开
    assert rs.session_status("us", bj(2026, 11, 4, 22, 29))[0] is False
    assert rs.session_status("us", bj(2026, 11, 4, 22, 30))[0] is True


def test_dst_transition_week_does_not_drift():
    """DST 切换周前后，同一个"北京 22:30"对应的纽约时间不同，必须分别判定。"""
    rs = _ruleset()
    # 2026-10-30（DST 未结束，UTC-4）：北京 22:30 = 纽约 10:30 → 开市
    assert rs.session_status("us", bj(2026, 10, 30, 22, 30))[0] is True
    # 2026-11-06（DST 已结束，UTC-5）：北京 22:30 = 纽约 09:30 → 开市
    assert rs.session_status("us", bj(2026, 11, 6, 22, 30))[0] is True
    # 而北京 22:00：DST 内是纽约 10:00（开市），冬令时是纽约 09:00（盘前，未开市）
    assert rs.session_status("us", bj(2026, 10, 30, 22, 0))[0] is True
    assert rs.session_status("us", bj(2026, 11, 6, 22, 0))[0] is False


# --------------------------------------------------------------------------- #
# 3. 北京周六 = 纽约周五
# --------------------------------------------------------------------------- #
def test_beijing_saturday_is_new_york_friday():
    """北京周六凌晨跑的是**纽约周五**的常规时段（旧实现按北京日期判周末 → 误杀）。"""
    rs = _ruleset()
    info = rs.session_info("us", bj(2026, 11, 7, 2, 0))   # 北京周六 02:00
    assert info is not None
    assert info.local_date == "2026-11-06"                # 纽约 2026-11-06 周五
    assert info.weekday == 4
    assert info.is_regular_open is True, info.as_dict()
    assert rs.session_status("us", bj(2026, 11, 7, 2, 0))[0] is True

    # 而北京周六 10:00 = 纽约周五 21:00 → 盘后，不可开新仓
    assert rs.session_status("us", bj(2026, 11, 7, 10, 0))[0] is False


def test_new_york_saturday_is_closed():
    rs = _ruleset()
    info = rs.session_info("us", ny(2026, 11, 7, 12, 0))
    assert info.phase == PHASE_WEEKEND
    assert info.is_regular_open is False


# --------------------------------------------------------------------------- #
# 4. 北京周一 = 纽约周日
# --------------------------------------------------------------------------- #
def test_beijing_monday_is_new_york_sunday():
    """北京周一凌晨是**纽约周日傍晚** —— 必须休市（旧实现落进 21:30-04:00 窗口 → 误开）。"""
    rs = _ruleset()
    info = rs.session_info("us", bj(2026, 11, 9, 2, 0))
    assert info.local_date == "2026-11-08"    # 周日
    assert info.weekday == 6
    assert info.phase == PHASE_WEEKEND
    assert rs.session_status("us", bj(2026, 11, 9, 2, 0))[0] is False


# --------------------------------------------------------------------------- #
# 5. 提前收市日
# --------------------------------------------------------------------------- #
def test_us_early_close_day():
    rs = _ruleset()
    # 2026-11-27 感恩节次日，13:00 收盘
    assert rs.session_status("us", ny(2026, 11, 27, 12, 59))[0] is True
    assert rs.session_status("us", ny(2026, 11, 27, 13, 0))[0] is False
    info = rs.session_info("us", ny(2026, 11, 27, 12, 0))
    assert info.closes_at == "13:00"

    # 普通日同一点仍然是常规时段
    assert rs.session_status("us", ny(2026, 11, 25, 13, 0))[0] is True


def test_us_holiday_and_calendar_coverage():
    rs = _ruleset()
    # 感恩节当天（周四）休市 —— 不是周末，靠日历拦住
    assert rs.session_status("us", ny(2026, 11, 26, 10, 0))[0] is False
    v = _codes(_check(rs, now=ny(2026, 11, 26, 10, 0)))
    assert CODE_CALENDAR in v

    # 超出日历覆盖范围（2027 年）必须拒绝，而不是假定"是交易日"
    v = _codes(_check(rs, now=ny(2027, 1, 5, 10, 0)))
    assert CODE_CALENDAR in v


# --------------------------------------------------------------------------- #
# 6/7/8. 盘前 / 盘后 / 夜盘持仓
# --------------------------------------------------------------------------- #
def _portfolio_for(positions) -> Portfolio:
    return Portfolio(currency="USD", equity=1_000_000.0, cash=1_000_000.0,
                     positions=positions)


@pytest.mark.parametrize("session", ["pre", "regular", "post", "night", "unknown"])
def test_all_session_positions_enter_risk_control(session):
    """不论持仓来自哪个时段，都必须进入风控（敞口/止损都看得见）。"""
    p = Position(symbol="usAAPL", market="us", qty=10, avg_cost=200.0,
                 last_price=100.0, currency="USD", session=session)
    pf = _portfolio_for([p])
    assert pf.position_count == 1
    assert pf.position_of("usAAPL") is p
    assert pf.sellable_qty("usAAPL") == 10

    rs = _ruleset()
    risk = RiskController.__new__(RiskController)   # 只借用 scan_exits 的纯函数逻辑
    intents = RiskController.scan_exits(
        risk, [p], {"usAAPL": {"stop_loss": 150.0, "take_profit": None}},
        {"usAAPL": Quote(symbol="usAAPL", market="us", last=120.0)})
    assert len(intents) == 1 and intents[0].is_exit, intents


def test_mixed_session_positions_all_counted():
    ps = [Position(symbol=f"usS{i}", market="us", qty=5, avg_cost=10.0,
                   last_price=10.0, currency="USD", session=s)
          for i, s in enumerate(["pre", "regular", "post", "night"])]
    pf = _portfolio_for(ps)
    assert pf.position_count == 4
    assert {p.session for p in pf.positions} == {"pre", "regular", "post", "night"}


def test_session_phases_are_recognised():
    rs = _ruleset()
    assert rs.session_info("us", ny(2026, 11, 4, 5, 0)).phase == "pre"
    assert rs.session_info("us", ny(2026, 11, 4, 10, 0)).phase == "regular"
    assert rs.session_info("us", ny(2026, 11, 4, 17, 0)).phase == "post"
    assert rs.session_info("us", ny(2026, 11, 4, 22, 0)).phase == "night"
    # 跨当地午夜的夜盘
    assert rs.session_info("us", ny(2026, 11, 5, 2, 0)).phase == "night"


# --------------------------------------------------------------------------- #
# 9. 持仓查询异常 → fail-closed，但止损仍要继续评估
# --------------------------------------------------------------------------- #
class UsBroker(ScriptedBroker):
    """可控报价的美股券商替身（走真实 Broker 契约，不是 Mock）。"""

    def __init__(self, prices=None, **kw):
        super().__init__(**kw)
        self.prices = {k.lower(): v for k, v in (prices or {}).items()}

    def get_quote(self, symbol: str) -> Quote:
        px = self.prices.get(symbol.lower())
        if px is None:
            raise BrokerError(f"无 {symbol} 行情", code="EMPTY_QUOTE", retryable=True)
        return Quote(symbol=symbol, market="us", last=px, prev_close=px, source="stub")


def _engine(tmp_path, broker, **over):
    cfg = live_cfg(tmp_path, **over)
    j = AuditJournal(tmp_path / "audit.db", tmp_path / "audit.jsonl")
    risk = RiskController(cfg, cfg.gate, rules=_ruleset())
    eng = StrategyEngine(cfg, broker, FixedSignalModel(), risk, j,
                         stops_path=tmp_path / "stops.json")
    return cfg, j, risk, eng


def test_positions_query_exception_blocks_new_entries(tmp_path):
    broker = UsBroker(prices={"usAAPL": 200.0},
                      positions_exc=BrokerError("持仓接口 500", code="HTTP_500"))
    cfg, j, risk, eng = _engine(tmp_path, broker)
    try:
        rep = eng.scan(symbols=["usAAPL"], now=ny(2026, 11, 4, 10, 0))
        assert rep.blocked_reason, "持仓查询失败必须整轮停手"
        assert "持仓读取失败" in rep.blocked_reason
        assert broker.calls["place"] == 0
    finally:
        j.close()


def test_positions_query_exception_still_allows_stop_loss(tmp_path):
    """US-P0-4：持仓查不到时，止损**不能**失效。

    先用一次成功的查询建立快照；随后持仓接口开始报错，止损判定改用
    最近快照（券商回报的真实数量与最新价），而不是直接放弃处置。
    """
    broker = UsBroker(prices={"usAAPL": 180.0})
    broker.positions = [Position(symbol="usAAPL", market="us", qty=10,
                                 avg_cost=200.0, last_price=180.0,
                                 currency="USD", session="regular")]
    cfg, j, risk, eng = _engine(tmp_path, broker)
    try:
        # 第一次：正常拿到持仓并缓存（此时还没登记止损，不会触发）
        first = eng.monitor(now=ny(2026, 11, 4, 10, 0))
        assert not any(o.get("kind") == "exit" for o in first), first
        assert eng._positions_cache.get("positions")
        cached_px = eng._positions_cache["positions"][0].last_price
        assert cached_px == 180.0

        # 登记止损 190（现价 180 已破），随后两个接口同时失效
        eng.register_stops("usAAPL", stop_loss=190.0, take_profit=None, qty=10)
        broker.positions_exc = BrokerError("持仓超时", code="TIMEOUT")
        broker.prices = {}

        out = eng.monitor(now=ny(2026, 11, 4, 10, 0))
        kinds = [o.get("kind") for o in out]
        assert "positions_unavailable" in kinds, out
        assert broker.calls["place"] >= 1, out
        assert broker.last_request is not None
        assert broker.last_request.side == "sell"
        assert eng._positions_stale is True, "持仓不可信时必须标记，禁止新开仓"
    finally:
        j.close()


def test_quote_failure_uses_position_mark_for_stop(tmp_path):
    """行情接口失败 → 用券商持仓最新价判定止损（不是本地编的价格）。"""
    broker = UsBroker(prices={})   # 所有 get_quote 都抛错
    broker.positions = [Position(symbol="usAAPL", market="us", qty=10,
                                 avg_cost=200.0, last_price=185.0,
                                 currency="USD")]
    cfg, j, risk, eng = _engine(tmp_path, broker)
    try:
        eng.register_stops("usAAPL", stop_loss=190.0, take_profit=None, qty=10)
        out = eng.monitor(now=ny(2026, 11, 4, 10, 0))
        assert any(o.get("kind") == "exit" for o in out), out
    finally:
        j.close()


# --------------------------------------------------------------------------- #
# 10. 交易时段服务异常
# --------------------------------------------------------------------------- #
def test_session_service_exception_is_fail_closed(monkeypatch):
    rs = _ruleset()

    def boom(self, now=None):
        raise RuntimeError("tzdata 损坏")

    monkeypatch.setattr(clock_mod.ExchangeClock, "session", boom)
    ok, why = rs.session_status("us", ny(2026, 11, 4, 10, 0))
    assert ok is False
    assert "时段判定异常" in why

    v = _codes(_check(rs, now=ny(2026, 11, 4, 10, 0)))
    assert CODE_SESSION in v

    # 但减仓不能被时段服务异常阻断（US-P0-4）
    r = _check(rs, side="sell", qty=10, position_qty=10,
               now=ny(2026, 11, 4, 10, 0), is_exit=True)
    assert CODE_SESSION not in _codes(r)
    assert "减仓豁免" in r.note


def test_clock_data_missing_is_fail_closed(monkeypatch):
    def boom(market, spec, **kw):
        raise ClockError("时区数据不可用")

    monkeypatch.setattr("ai_trader.market.rules.build_clock", boom)
    rs = _ruleset()
    assert rs.clock_errors.get("us")
    ok, why = rs.session_status("us", ny(2026, 11, 4, 10, 0))
    assert ok is False and "时钟不可用" in why
    assert CODE_SESSION in _codes(_check(rs))


# --------------------------------------------------------------------------- #
# 11. 规则未配置 / 未强制 → 不得放行
# --------------------------------------------------------------------------- #
def test_unresolvable_symbol_rejected():
    rs = _ruleset()
    r = _check(rs, symbol="xxFOO")
    assert CODE_RULE_UNKNOWN in _codes(r)


def test_unknown_board_status_rejected():
    rs = _ruleset()
    raw = rs._raw
    raw["boards"]["us"]["meta"]["status"] = STATUS_UNKNOWN
    rs2 = MarketRuleSet(raw)
    assert CODE_RULE_UNKNOWN in _codes(_check(rs2))


def test_unverified_board_rejected_when_not_allowed():
    rs = _ruleset()
    raw = rs._raw
    raw["boards"]["us"]["meta"]["status"] = STATUS_UNVERIFIED
    rs2 = MarketRuleSet(raw)
    assert CODE_RULE_UNVERIFIED in _codes(_check(rs2))


def test_unenforced_market_cannot_bypass_rules():
    """``applied=False`` 不是"通过"：配置成 reject 时必须拒绝。"""
    rs = _ruleset()
    r = _check(rs, symbol="hk00700", qty=100, price=300.0, now=ny(2026, 11, 4, 10, 0))
    assert r.applied is False, "warn 策略下港股仍标注未建模"
    assert r.violations == []

    raw = rs._raw
    raw["unenforced_market_policy"] = "reject"
    rs2 = MarketRuleSet(raw)
    r2 = _check(rs2, symbol="hk00700", qty=100, price=300.0,
                now=ny(2026, 11, 4, 10, 0))
    assert r2.applied is True
    assert CODE_MARKET_NOT_ENFORCED in _codes(r2)


def test_us_rules_really_enforced():
    """US-P0-2：美股在 enforce_markets 内时，价格精度/数量/时段必须真生效。"""
    rs = _ruleset()
    assert rs.applies("us") is True

    # 价格精度（$0.01）——150.003 不是 0.01 的整数倍
    assert CODE_PRICE_TICK in _codes(_check(rs, price=150.003, prev_close=150.0))
    assert CODE_PRICE_TICK not in _codes(_check(rs, price=150.0, prev_close=150.0))
    # < $1 走 $0.0001 档
    assert CODE_PRICE_TICK not in _codes(_check(rs, price=0.5001, prev_close=0.50))
    assert CODE_PRICE_TICK in _codes(_check(rs, price=0.50005, prev_close=0.50))
    # 数量必须为正
    assert CODE_LOT_SIZE in _codes(_check(rs, qty=0))
    # 时段
    assert CODE_SESSION in _codes(_check(rs, now=ny(2026, 11, 4, 3, 0)))


def test_us_exit_not_blocked_by_session_or_calendar():
    """US-P0-4：止损发生在休市/节假日也必须能发出（券商侧的拒单由券商处理）。"""
    rs = _ruleset()
    r = _check(rs, side="sell", qty=10, position_qty=10,
               now=ny(2026, 12, 25, 10, 0), is_exit=True)   # 圣诞节
    assert CODE_SESSION not in _codes(r), r.violations
    assert CODE_CALENDAR not in _codes(r), r.violations
    assert r.ok


# --------------------------------------------------------------------------- #
# 12. A 股 / 港股既有行为回归
# --------------------------------------------------------------------------- #
def test_cn_t_plus_one_regression():
    rs = _ruleset(enforce_markets=["us", "cn"])
    r = _check(rs, symbol="cn600519", side="sell", qty=100, price=10.0,
               prev_close=10.0, position_qty=100, today_bought_qty=100,
               now=bj(2026, 11, 4, 10, 0))
    assert r.applied is True
    assert CODE_T_PLUS in _codes(r)


def test_cn_session_still_by_shanghai_time():
    rs = _ruleset()
    assert rs.session_status("cn", bj(2026, 11, 4, 10, 0))[0] is True
    assert rs.session_status("cn", bj(2026, 11, 4, 12, 0))[0] is False   # 午休
    assert rs.session_status("cn", bj(2026, 11, 4, 9, 0))[0] is False


def test_hk_session_still_by_hong_kong_time():
    rs = _ruleset()
    assert rs.session_status("hk", bj(2026, 11, 4, 10, 0))[0] is True
    assert rs.session_status("hk", bj(2026, 11, 4, 12, 30))[0] is False


def test_is_market_open_helper_uses_exchange_time():
    from ai_trader.risk.controller import is_market_open
    # 冬令时：北京 22:00 = 纽约 09:00 → 盘前，未开市
    assert is_market_open("us", bj(2026, 11, 4, 22, 0))[0] is False
    assert is_market_open("us", bj(2026, 11, 4, 22, 30))[0] is True
    # 夏令时：北京 21:30 = 纽约 09:30 → 开市
    assert is_market_open("us", bj(2026, 7, 15, 21, 30))[0] is True
    assert is_market_open("cn", bj(2026, 11, 4, 10, 0))[0] is True


# --------------------------------------------------------------------------- #
# US-P0-3：适配器必须把 use_us_pre / use_us_post / use_us_night 传下去
# --------------------------------------------------------------------------- #
class _HoldingsAPI:
    def __init__(self):
        self.kwargs = None

    def get_holdings(self, **kw):
        self.kwargs = dict(kw)
        return {"code": 0, "data": {"list": [
            {"stockCode": "AAPL", "marketCode": "us", "qty": 10,
             "avgCost": 200.0, "lastPrice": 210.0, "currency": "USD"},
        ]}}


def _adapter(tmp_path, **over):
    cfg = live_cfg(tmp_path, **over)
    b = FosunBroker(cfg, None)
    api = _HoldingsAPI()
    b.client = type("C", (), {"portfolio": api})()
    b.configured = True
    return b, api


def test_positions_passes_all_us_session_flags(tmp_path):
    b, api = _adapter(tmp_path)
    pos = b.get_positions()
    assert api.kwargs["use_us_pre"] is True
    assert api.kwargs["use_us_post"] is True
    assert api.kwargs["use_us_night"] is True
    assert len(pos) == 1 and pos[0].symbol.lower().startswith("us")
    assert b.health()["position_sessions"]["warning"] == ""


def test_positions_flags_can_be_overridden_and_warned(tmp_path):
    b, api = _adapter(tmp_path,
                      **{"fosun.position_sessions.use_us_night": False})
    b.get_positions()
    assert api.kwargs["use_us_night"] is False
    h = b.health()
    assert h["position_sessions"]["use_us_night"] is False
    assert h["position_sessions"]["warning"], "关掉某段持仓查询必须显式告警"


# --------------------------------------------------------------------------- #
# US-P0-4：风控层不得因降级/亏损/休市而阻断止损
# --------------------------------------------------------------------------- #
def _us_risk(tmp_path):
    cfg = make_cfg(tmp_path, **{
        "market_rules.enforce_markets": ["us"],
        "market_rules.calendar.status": STATUS_VERIFIED,
    })
    risk = RiskController(cfg, cfg.gate, rules=_ruleset())
    return risk


def _us_portfolio(**kw):
    base = dict(currency="USD", equity=1_000_000.0, cash=1_000_000.0,
                positions=[Position(symbol="usAAPL", market="us", qty=10,
                                    avg_cost=200.0, last_price=190.0,
                                    currency="USD")])
    base.update(kw)
    return Portfolio(**base)


def _us_exit():
    return OrderIntent(symbol="usAAPL", side="sell", qty=10, price=190.0,
                       market="us", currency="USD", order_type="market",
                       reason="止损触发", is_exit=True)


def test_daily_loss_does_not_block_exit(tmp_path):
    risk = _us_risk(tmp_path)
    pf = _us_portfolio(day_pnl=-500_000.0, day_pnl_known=True)
    d = risk.evaluate(_us_exit(), pf, last_price=190.0, recent_orders=[],
                      now=ny(2026, 11, 4, 10, 0))
    assert d.approved, d.reasons


def test_unknown_day_pnl_does_not_block_exit(tmp_path):
    risk = _us_risk(tmp_path)
    pf = _us_portfolio(day_pnl_known=False)
    d = risk.evaluate(_us_exit(), pf, last_price=190.0, recent_orders=[],
                      now=ny(2026, 11, 4, 10, 0))
    assert d.approved, d.reasons


def test_degraded_portfolio_allows_exit_but_blocks_entry(tmp_path):
    risk = _us_risk(tmp_path)
    pf = _us_portfolio(degraded=True, degraded_reason="持仓接口超时")
    d = risk.evaluate(_us_exit(), pf, last_price=190.0, recent_orders=[],
                      now=ny(2026, 11, 4, 10, 0))
    assert d.approved, d.reasons

    buy = OrderIntent(symbol="usNVDA", side="buy", qty=10, price=200.0,
                      market="us", currency="USD", stop_loss=190.0)
    d2 = risk.evaluate(buy, pf, last_price=200.0, recent_orders=[],
                       now=ny(2026, 11, 4, 10, 0))
    assert not d2.approved
    assert "portfolio_integrity" in d2.failed_checks


def test_exit_allowed_when_market_disabled(tmp_path):
    risk = _us_risk(tmp_path)
    risk.enforce_market_hours = True
    risk.markets = {}                     # 模拟"该市场被停用"
    d = risk.evaluate(_us_exit(), _us_portfolio(), last_price=190.0,
                      recent_orders=[], now=ny(2026, 11, 4, 10, 0))
    assert d.approved, d.reasons


def test_entry_blocked_outside_us_session(tmp_path):
    risk = _us_risk(tmp_path)
    buy = OrderIntent(symbol="usNVDA", side="buy", qty=10, price=200.0,
                      market="us", currency="USD", stop_loss=196.0)
    # 纽约 09:00 = 盘前 → 不允许开新仓
    d = risk.evaluate(buy, _us_portfolio(), last_price=200.0, recent_orders=[],
                      now=ny(2026, 11, 4, 9, 0))
    assert not d.approved
    assert "market_hours" in d.failed_checks
