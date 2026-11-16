"""测试 4：模拟买卖撮合与账务。"""
from __future__ import annotations

import pytest

from ai_trader.broker.base import OrderRejected, OrderRequest


def _buy(paper, symbol="hk00700", qty=100, price=None, otype="market"):
    q = paper.get_quote(symbol)
    return paper.place_order(OrderRequest(
        symbol=symbol, market=q.market, side="buy", qty=qty,
        price=price if price is not None else q.last,
        order_type=otype, currency="HKD" if q.market == "hk" else "USD",
    ))


def test_initial_cash_and_equity(paper):
    s = paper.get_summary("HKD")
    assert s.cash == pytest.approx(1_000_000.0)
    assert s.equity == pytest.approx(1_000_000.0)
    assert paper.get_positions() == []


def test_market_buy_creates_position_and_deducts_cash(paper):
    before = paper.get_summary("HKD").cash
    r = _buy(paper, qty=100)
    assert r.status == "filled"
    assert r.filled_qty == 100
    assert r.avg_fill_price > 0
    assert r.commission >= 3.0

    holdings = paper.get_positions()
    assert len(holdings) == 1
    assert holdings[0].qty == 100

    after = paper.get_summary("HKD").cash
    spent = before - after
    assert spent == pytest.approx(r.avg_fill_price * 100 + r.commission, rel=1e-6)


def test_sell_reduces_position_and_realizes_pnl(paper):
    _buy(paper, qty=200)
    pos = paper.get_positions()[0]
    q = paper.get_quote("hk00700")
    r = paper.place_order(OrderRequest(symbol="hk00700", market="hk", side="sell",
                                       qty=200, price=q.last, order_type="market",
                                       currency="HKD"))
    assert r.status == "filled"
    assert paper.get_positions() == []

    rp = paper._state["realized_pnl"].get("HKD", 0.0)
    expected = (r.avg_fill_price - pos.avg_cost) * 200 - r.commission
    assert rp == pytest.approx(expected, rel=1e-6)


def test_sell_without_position_is_rejected(paper):
    with pytest.raises(OrderRejected) as ei:
        paper.place_order(OrderRequest(symbol="hk00700", market="hk", side="sell",
                                       qty=100, price=300.0, order_type="market",
                                       currency="HKD"))
    assert ei.value.code == "INSUFFICIENT_POSITION"


def test_buy_with_insufficient_cash_is_rejected(paper):
    q = paper.get_quote("hk00700")
    with pytest.raises(OrderRejected) as ei:
        _buy(paper, qty=1_000_000, price=q.last)
    assert ei.value.code == "INSUFFICIENT_CASH"


def test_limit_buy_below_market_stays_pending_then_can_cancel(paper):
    q = paper.get_quote("hk00700")
    r = paper.place_order(OrderRequest(
        symbol="hk00700", market="hk", side="buy", qty=100,
        price=round(q.last * 0.5, 3), order_type="limit", currency="HKD",
    ))
    assert r.status == "submitted"
    assert r.filled_qty == 0
    assert paper.get_positions() == []

    cancelled = paper.cancel_order(r.order_id)
    assert cancelled.status == "cancelled"
    with pytest.raises(OrderRejected):
        paper.cancel_order(r.order_id)          # 不可重复撤单


def test_limit_buy_above_market_fills_immediately(paper):
    q = paper.get_quote("hk00700")
    r = paper.place_order(OrderRequest(
        symbol="hk00700", market="hk", side="buy", qty=100,
        price=round(q.last * 1.02, 3), order_type="limit", currency="HKD",
    ))
    assert r.status == "filled"
    assert r.avg_fill_price <= q.last * 1.02


def test_commission_floor_applied(paper):
    q = paper.get_quote("hk00700")
    r = paper.place_order(OrderRequest(
        symbol="hk00700", market="hk", side="buy", qty=1,
        price=q.last, order_type="market", currency="HKD",
    ))
    assert r.commission == pytest.approx(3.0)


def test_pending_order_refresh_fills_when_crossed(paper):
    q = paper.get_quote("hk00700")
    r = paper.place_order(OrderRequest(
        symbol="hk00700", market="hk", side="buy", qty=100,
        price=round(q.last * 0.5, 3), order_type="limit", currency="HKD",
    ))
    assert r.status == "submitted"
    # 人工把委托价改到市价之上，模拟行情穿越
    paper._state["orders"][r.order_id]["price"] = round(q.last * 1.5, 3)
    filled = paper.refresh_pending()
    assert len(filled) == 1
    assert filled[0].status == "filled"
    assert paper.get_positions()[0].qty == 100


def test_multi_currency_isolation(paper):
    _buy(paper, "hk00700", 100)
    _buy(paper, "usAAPL", 10)
    hkd = paper.get_summary("HKD")
    usd = paper.get_summary("USD")
    assert hkd.cash < 1_000_000.0
    assert usd.cash < 130_000.0
    symbols = {p.symbol for p in paper.get_positions()}
    assert symbols == {"hk00700", "usAAPL"}


def test_state_persists_across_instances(tmp_path):
    from ai_trader.broker.paper import PaperBroker

    path = tmp_path / "p.json"
    b1 = PaperBroker(state_path=path)
    q = b1.get_quote("hk00700")
    b1.place_order(OrderRequest(symbol="hk00700", market="hk", side="buy", qty=100,
                                price=q.last, order_type="market", currency="HKD"))
    cash_after = b1.get_summary("HKD").cash

    b2 = PaperBroker(state_path=path)
    assert b2.get_summary("HKD").cash == pytest.approx(cash_after)
    assert b2.get_positions()[0].qty == 100


def test_reset_restores_initial_state(paper):
    _buy(paper, qty=100)
    paper.reset()
    assert paper.get_positions() == []
    assert paper.get_summary("HKD").cash == pytest.approx(1_000_000.0)
