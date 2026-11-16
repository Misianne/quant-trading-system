"""P2 五大能力的领域模型测试。

覆盖：
* 行情时段（sessions）
* 订单类型 / TIF / 状态机（orders）
* 市场状态 / LULD / MWCB（status）
* 卖空 / 借券 / SSR（short）
* T+1 结算账本（settlement）
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from ai_trader.broker.base import OrderRejected, OrderRequest
from ai_trader.broker.orders import (
    LIVE_DEFAULT_CAPABILITIES,
    PAPER_CAPABILITIES,
    BrokerCapabilities,
    OrderLifecycle,
    OrderState,
    OrderType,
    OrderValidationError,
    TimeInForce,
    apply_tif,
    parse_order_type,
    parse_tif,
    validate_order,
)
from ai_trader.broker.short import (
    BorrowBook,
    LocateService,
    SSRRegistry,
    ShortGuard,
    ShortMode,
    ShortPosition,
)
from ai_trader.market.sessions import (
    QuoteError,
    QuoteNormalizer,
    QuoteUnavailable,
    SessionPricePolicy,
    SessionQuote,
)
from ai_trader.market.status import (
    HaltReason,
    LULD_TIER1_PCT,
    MarketState,
    MarketStatusService,
)
from ai_trader.portfolio.settlement import (
    AccountMode,
    EntryKind,
    SettlementLedger,
    SettlementStatus,
)


# ====================================================================== #
# 行情时段
# ====================================================================== #
class _Cal:
    """可控交易日历：2026-11-26 为感恩节休市。"""

    def __init__(self, closed=()):
        self.closed = set(closed)

    def is_trading_day(self, day: date):
        if day.isoformat() in self.closed:
            return False, "holiday"
        if day.weekday() >= 5:
            return False, "weekend"
        return True, ""


NY = timezone(timedelta(hours=-5))   # 简化：测试只关心时间戳一致性


def _q(price=100.0, minutes_ago=0.0, phase="regular", seq=1, symbol="AAPL"):
    return SessionQuote(symbol=symbol, price=price,
                        exchange_ts=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
                        phase=phase, trading_day="2026-11-16", seq=seq)


def test_quote_rejects_naive_timestamp():
    with pytest.raises(QuoteError):
        SessionQuote(symbol="AAPL", price=100.0, exchange_ts=datetime(2026, 11, 16, 9, 30),
                     phase="regular", trading_day="2026-11-16")


def test_normalizer_accepts_fresh_regular_quote():
    n = QuoteNormalizer(clock=None)  # clock 未用于 ingest
    r = n.ingest(_q())
    assert r.accepted and r.quote.price == 100.0


def test_normalizer_rejects_stale_quote():
    n = QuoteNormalizer(clock=None)
    r = n.ingest(_q(minutes_ago=5))
    assert not r.accepted and "过期" in r.reason


def test_normalizer_rejects_out_of_order():
    n = QuoteNormalizer(clock=None)
    n.ingest(_q(price=100, seq=10))
    r = n.ingest(_q(price=101, seq=5))
    assert not r.accepted and "乱序" in r.reason


def test_normalizer_rejects_duplicate():
    n = QuoteNormalizer(clock=None)
    n.ingest(_q(price=100, seq=10))
    r = n.ingest(_q(price=100, seq=10))
    assert not r.accepted and "重复" in r.reason


def test_price_policy_refuses_premarket_as_regular():
    n = QuoteNormalizer(clock=None)
    n.ingest(_q(phase="pre"))
    policy = SessionPricePolicy(n)
    with pytest.raises(QuoteUnavailable) as ei:
        policy.price("AAPL", "regular_only")
    assert "禁止跨时段冒用" in str(ei.value)
    # 风险退出允许任何可交易时段
    assert policy.price("AAPL", "risk_exit").phase == "pre"


def test_price_policy_raises_when_no_quote():
    policy = SessionPricePolicy(QuoteNormalizer(clock=None))
    with pytest.raises(QuoteUnavailable) as ei:
        policy.price("MSFT")
    assert "无任何行情" in str(ei.value)


# ====================================================================== #
# 订单类型与 TIF
# ====================================================================== #
def test_parse_aliases():
    assert parse_order_type("STOP-LIMIT") is OrderType.STOP_LIMIT
    assert parse_order_type("mkt") is OrderType.MARKET
    assert parse_tif("GTC") is TimeInForce.GTC
    with pytest.raises(OrderValidationError):
        parse_order_type("iceberg")
    with pytest.raises(OrderValidationError):
        parse_tif("GTD")


def test_validate_rejects_unsupported_type_instead_of_downgrade():
    caps = BrokerCapabilities(venue="toy", order_types=frozenset({OrderType.LIMIT}),
                              tifs=frozenset({TimeInForce.DAY}))
    req = OrderRequest(symbol="AAPL", market="us", side="buy", qty=1,
                       order_type="stop", stop_price=90.0)
    with pytest.raises(OrderValidationError) as ei:
        validate_order(req, caps)
    assert ei.value.code == "ORDER_TYPE_UNSUPPORTED"


def test_validate_requires_stop_price():
    req = OrderRequest(symbol="AAPL", market="us", side="sell", qty=1,
                       order_type="stop", price=100.0)
    with pytest.raises(OrderValidationError) as ei:
        validate_order(req, PAPER_CAPABILITIES)
    assert "触发价" in str(ei.value)


def test_validate_stop_limit_requires_both_prices():
    ok = OrderRequest(symbol="AAPL", market="us", side="buy", qty=1,
                      order_type="stop_limit", price=105.0, stop_price=104.0)
    v = validate_order(ok, PAPER_CAPABILITIES)
    assert v.price == 105.0 and v.stop_price == 104.0


def test_auction_tif_requires_window():
    req = OrderRequest(symbol="AAPL", market="us", side="buy", qty=1,
                       order_type="limit", price=100.0, tif="opg")
    with pytest.raises(OrderValidationError) as ei:
        validate_order(req, PAPER_CAPABILITIES, in_auction_window=False)
    assert ei.value.code == "NOT_AUCTION_WINDOW"
    assert validate_order(req, PAPER_CAPABILITIES, in_auction_window=True).tif is TimeInForce.OPG


def test_live_default_capabilities_are_minimal():
    assert OrderType.STOP not in LIVE_DEFAULT_CAPABILITIES.order_types
    assert TimeInForce.GTC not in LIVE_DEFAULT_CAPABILITIES.tifs
    assert LIVE_DEFAULT_CAPABILITIES.supports_short is False


@pytest.mark.parametrize("tif,fillable,expect_fill,expect_cancel,expect_rest", [
    (TimeInForce.DAY, 40, 40, False, True),
    (TimeInForce.GTC, 40, 40, False, True),
    (TimeInForce.IOC, 40, 40, True, False),
    (TimeInForce.FOK, 40, 0, True, False),
    (TimeInForce.FOK, 100, 100, False, False),
    (TimeInForce.CLS, 30, 30, True, False),
])
def test_apply_tif_semantics(tif, fillable, expect_fill, expect_cancel, expect_rest):
    out = apply_tif(tif, 100, fillable)
    assert out.fill_qty == expect_fill
    assert out.cancel_remainder is expect_cancel
    assert out.rest_remainder is expect_rest


def test_lifecycle_happy_path_and_illegal_transition():
    lc = OrderLifecycle(order_id="O1", client_id="C1", symbol="AAPL", side="buy", qty=100)
    lc.transition(OrderState.SUBMITTED)
    lc.transition(OrderState.PARTIAL, filled_qty=40, avg_price=100.0)
    lc.transition(OrderState.FILLED, filled_qty=100, avg_price=100.5)
    assert lc.state is OrderState.FILLED and lc.is_terminal
    with pytest.raises(OrderValidationError):
        lc.transition(OrderState.CANCELED)


def test_lifecycle_rejects_overfill_and_regression():
    lc = OrderLifecycle(order_id="O2", client_id="C2", symbol="AAPL", side="buy", qty=10)
    lc.transition(OrderState.SUBMITTED)
    lc.transition(OrderState.PARTIAL, filled_qty=5)
    with pytest.raises(OrderValidationError):
        lc.transition(OrderState.PARTIAL, filled_qty=4)
    with pytest.raises(OrderValidationError):
        lc.transition(OrderState.FILLED, filled_qty=11)


def test_lifecycle_partial_as_filled_is_rejected():
    lc = OrderLifecycle(order_id="O3", client_id="C3", symbol="AAPL", side="buy", qty=10)
    lc.transition(OrderState.SUBMITTED)
    with pytest.raises(OrderValidationError):
        lc.transition(OrderState.FILLED, filled_qty=6)


# ====================================================================== #
# 市场状态
# ====================================================================== #
def test_unknown_without_data():
    svc = MarketStatusService()
    st = svc.status("AAPL")
    assert st.state is MarketState.UNKNOWN
    d = svc.evaluate("AAPL")
    assert d.allowed is False


def test_halt_blocks_entry_but_not_exit():
    svc = MarketStatusService()
    svc.update_reference("AAPL", 100.0)
    svc.halt_symbol("AAPL", HaltReason.NEWS_PENDING, note="news")
    assert svc.evaluate("AAPL").allowed is False
    exit_d = svc.evaluate("AAPL", is_exit=True)
    assert exit_d.allowed is True and "风险退出" in exit_d.warning


def test_luld_trigger_and_recover():
    svc = MarketStatusService()
    svc.update_reference("AAPL", 100.0)
    svc.on_quote("AAPL", 100.2, datetime.now(timezone.utc))
    assert svc.status("AAPL").state is MarketState.OPEN
    # 跌破 5% 下轨：首次越界只暂停，带宽不变（doubling=0）
    svc.on_quote("AAPL", 94.0, datetime.now(timezone.utc))
    st = svc.status("AAPL")
    assert st.state is MarketState.LULD_PAUSED
    assert st.band is not None and st.band.doubling == 0
    assert st.band.band_pct == pytest.approx(LULD_TIER1_PCT)
    # 回到带内
    svc.on_quote("AAPL", 99.0, datetime.now(timezone.utc))
    assert svc.status("AAPL").state is MarketState.OPEN


def test_luld_band_doubles_on_repeat_breach():
    svc = MarketStatusService()
    svc.update_reference("AAPL", 100.0)
    svc.on_quote("AAPL", 100.0, datetime.now(timezone.utc))
    b1 = svc.on_quote("AAPL", 90.0, datetime.now(timezone.utc))
    b2 = svc.on_quote("AAPL", 80.0, datetime.now(timezone.utc))
    assert b1 is not None and b2 is not None
    assert b2.doubling == b1.doubling + 1
    assert b2.band_pct > b1.band_pct
    assert b1.band_pct == pytest.approx(LULD_TIER1_PCT)


def test_stale_quote_becomes_data_stale():
    svc = MarketStatusService(max_quote_age_seconds=1)
    svc.update_reference("AAPL", 100.0)
    svc.on_quote("AAPL", 100.0, datetime.now(timezone.utc))
    st = svc.status("AAPL", now=datetime.now(timezone.utc) + timedelta(seconds=5))
    assert st.state is MarketState.DATA_STALE


def test_mwcb_levels_and_l3_close():
    svc = MarketStatusService()
    now = datetime.now(timezone.utc)
    svc.update_index(5000.0, now)
    assert svc.update_index(4650.0, now) == 1          # -7%
    assert svc.mwcb_status()["level"] == 1
    assert svc.status("AAPL", now=now).state is MarketState.MWCB_PAUSED
    # 跳到 -20%
    assert svc.update_index(4000.0, now) == 3
    assert svc.status("AAPL", now=now).state is MarketState.MWCB_CLOSED
    assert svc.evaluate("AAPL", is_exit=True).allowed is True


# ====================================================================== #
# 卖空
# ====================================================================== #
def _short_req(qty=100.0, price=100.0, side="sell"):
    return OrderRequest(symbol="AAPL", market="us", side=side, qty=qty, price=price)


def test_short_disabled_by_default():
    g = ShortGuard(mode=ShortMode.DISABLED)
    d = g.check(_short_req(), held_qty=0, caps=PAPER_CAPABILITIES)
    assert d.allowed is False and d.is_short is True
    assert "禁止裸卖空" in d.reason


def test_normal_sell_is_not_short():
    g = ShortGuard(mode=ShortMode.PAPER_ONLY)
    d = g.check(_short_req(qty=100), held_qty=100, caps=PAPER_CAPABILITIES)
    assert d.allowed and not d.is_short


def test_short_requires_borrow():
    book = BorrowBook()
    g = ShortGuard(mode=ShortMode.PAPER_ONLY, borrow=book,
                   locates=LocateService(auto_grant=True, book=book))
    d = g.check(_short_req(), held_qty=0, caps=PAPER_CAPABILITIES)
    assert d.allowed is False and "可借数量不足" in d.reason


def test_short_success_with_borrow_and_locate():
    book = BorrowBook()
    book.set_inventory("AAPL", 500)
    g = ShortGuard(mode=ShortMode.PAPER_ONLY, borrow=book,
                   locates=LocateService(auto_grant=True, book=book))
    d = g.check(_short_req(qty=100), held_qty=0, caps=PAPER_CAPABILITIES)
    assert d.allowed and d.is_short and d.locate is not None
    assert book.available("AAPL") == 400      # locate 已预留


def test_short_exceeds_borrow():
    book = BorrowBook()
    book.set_inventory("AAPL", 50)
    g = ShortGuard(mode=ShortMode.PAPER_ONLY, borrow=book,
                   locates=LocateService(auto_grant=True, book=book))
    d = g.check(_short_req(qty=100), held_qty=0, caps=PAPER_CAPABILITIES)
    assert d.allowed is False


def test_ssr_blocks_short_at_or_below_reference():
    book = BorrowBook()
    book.set_inventory("AAPL", 1000)
    ssr = SSRRegistry()
    ssr.observe("AAPL", 89.0, prior_close=100.0)     # 触发 -11%
    assert ssr.is_active("AAPL")
    g = ShortGuard(mode=ShortMode.PAPER_ONLY, borrow=book, ssr=ssr,
                   locates=LocateService(auto_grant=True, book=book))
    d = g.check(_short_req(price=95.0), held_qty=0, caps=PAPER_CAPABILITIES,
                price=95.0, prior_close=100.0)
    assert d.allowed is False and "SSR" in d.reason
    ok = g.check(_short_req(price=101.0), held_qty=0, caps=PAPER_CAPABILITIES,
                 price=101.0, prior_close=100.0)
    assert ok.allowed and ok.ssr_active


def test_live_short_requires_operator_approval():
    book = BorrowBook()
    book.set_inventory("AAPL", 1000)
    caps = BrokerCapabilities(venue="live", order_types=frozenset(OrderType),
                              tifs=frozenset(TimeInForce), supports_short=True,
                              supports_locate=True, is_live=True)
    g = ShortGuard(mode=ShortMode.ENABLED, borrow=book,
                   locates=LocateService(auto_grant=True, book=book),
                   operator_approved=False)
    d = g.check(_short_req(), held_qty=0, caps=caps, is_live=True)
    assert d.allowed is False and "人工批准" in d.reason


def test_broker_without_short_support_rejected():
    book = BorrowBook()
    book.set_inventory("AAPL", 1000)
    caps = BrokerCapabilities(venue="nostop", order_types=frozenset(OrderType),
                              tifs=frozenset(TimeInForce), supports_short=False,
                              supports_locate=False)
    g = ShortGuard(mode=ShortMode.PAPER_ONLY, borrow=book,
                   locates=LocateService(auto_grant=True, book=book))
    d = g.check(_short_req(), held_qty=0, caps=caps)
    assert d.allowed is False and "未声明支持卖空" in d.reason


def test_short_position_cover_pnl():
    p = ShortPosition(symbol="AAPL", qty=-100, avg_price=100.0)
    pnl = p.cover(40, 90.0)
    assert pnl == pytest.approx(400.0)
    assert p.qty == pytest.approx(-60)
    p.cover(60, 110.0)
    assert p.qty == 0.0


# ====================================================================== #
# T+1 结算
# ====================================================================== #
def _ledger(mode=AccountMode.CASH, cash=10000.0, closed=("2026-11-26",)):
    return SettlementLedger(currency="USD", calendar=_Cal(closed), mode=mode,
                            opening_cash=cash)


def test_t1_uses_trading_calendar_not_plus_one_day():
    lg = _ledger()
    # 2026-11-25 买入 → 结算日应跳过 11-26 感恩节 → 11-27
    e = lg.record_trade(trade_date=date(2026, 11, 25), side="buy", symbol="AAPL",
                        qty=1, price=100.0)
    assert e.settle_date == "2026-11-27"


def test_friday_trade_settles_next_monday():
    lg = _ledger()
    e = lg.record_trade(trade_date=date(2026, 11, 13), side="buy",   # 周五
                        symbol="AAPL", qty=1, price=100.0)
    assert e.settle_date == "2026-11-16"


def test_cash_account_excludes_unsettled_proceeds():
    lg = _ledger(cash=1000.0)
    lg.record_trade(trade_date=date(2026, 11, 16), side="sell", symbol="AAPL",
                    qty=10, price=100.0, fee=1.0)          # 回款 999 未结算
    assert lg.unsettled_cash == pytest.approx(999.0)
    assert lg.buying_power() == pytest.approx(1000.0)      # 现金账户不认未结算
    ok, why = lg.can_afford(1500.0)
    assert ok is False and "不足" in why


def test_margin_account_counts_unsettled():
    lg = _ledger(mode=AccountMode.MARGIN, cash=1000.0)
    lg.record_trade(trade_date=date(2026, 11, 16), side="sell", symbol="AAPL",
                    qty=10, price=100.0, fee=1.0)
    assert lg.buying_power() == pytest.approx(1000.0 * 2.0 + 999.0)


def test_settlement_moves_unsettled_to_settled():
    lg = _ledger()
    e = lg.record_trade(trade_date=date(2026, 11, 16), side="sell", symbol="AAPL",
                        qty=10, price=100.0, fee=0.0)
    moved_before = lg.settle_through(date(2026, 11, 16))
    assert moved_before == []
    moved = lg.settle_through(date(2026, 11, 17))
    assert len(moved) == 1 and moved[0].entry_id == e.entry_id
    assert lg.unsettled_cash == 0.0
    assert lg.settled_cash == pytest.approx(11000.0)


def test_partial_fill_and_void():
    lg = _ledger()
    e = lg.record_trade(trade_date=date(2026, 11, 16), side="buy", symbol="AAPL",
                        qty=10, price=100.0, fee=1.0)
    assert lg.settled_cash == pytest.approx(10000.0 - 1001.0)
    lg.void_entry(e.entry_id, "cancelled")
    assert lg.settled_cash == pytest.approx(10000.0)


def test_reconcile_mismatch_generates_alert():
    lg = _ledger(cash=1000.0)
    r = lg.reconcile(broker_buying_power=900.0)
    assert r["ok"] is False
    kinds = [a["kind"] for a in r["alerts"]]
    assert "buying_power_mismatch" in kinds


def test_reconcile_missing_broker_bp_is_alert():
    lg = _ledger(cash=1000.0)
    r = lg.reconcile(broker_buying_power=None)
    assert r["ok"] is False
    assert r["alerts"][0]["kind"] == "broker_bp_missing"


def test_effective_available_takes_stricter():
    lg = _ledger(cash=1000.0)
    val, why = lg.effective_available(500.0)
    assert val == pytest.approx(500.0)
    val2, _ = lg.effective_available(5000.0)
    assert val2 == pytest.approx(1000.0)
