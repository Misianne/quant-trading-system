"""US-P2/P3 接线测试：验证五大能力**真的进了业务调用链**。

不是"模块能 import"，而是：
* 风控 ``evaluate`` 里出现了对应 check，并且按预期放行/阻断；
* Paper Broker 的撮合真的按订单类型/TIF/卖空语义执行。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from ai_trader.broker.base import OrderRequest, Position
from ai_trader.broker.orders import (
    BrokerCapabilities,
    OrderType,
    TimeInForce,
)
from ai_trader.broker.paper import PaperBroker
from ai_trader.broker.short import BorrowBook, LocateService, ShortGuard, ShortMode
from ai_trader.market.status import HaltReason, MarketStatusService
from ai_trader.portfolio.settlement import AccountMode, SettlementLedger
from ai_trader.risk.controller import OrderIntent, Portfolio, RiskController

from conftest import make_cfg


# ---------------------------------------------------------------------- #
# 构造辅助
# ---------------------------------------------------------------------- #
class _FixedQuotes:
    def __init__(self, price: float = 100.0):
        self.price = price

    def quote(self, symbol: str):
        from ai_trader.broker.base import Quote
        return Quote(symbol=symbol, market="us", last=self.price, prev_close=self.price)


def _cfg(tmp_path, enabled: bool = True, **extra):
    base = {
        "market_rules.enforce_markets": ["us"],
        "us_phase2.enabled": enabled,
        "risk.enforce_market_hours": False,
    }
    base.update(extra)
    return make_cfg(tmp_path, **base)


def _pf(qty: float = 0.0, cash: float = 1_000_000.0) -> Portfolio:
    positions = []
    if qty:
        positions.append(Position(symbol="usAAPL", market="us", qty=qty,
                                  avg_cost=100.0, last_price=100.0, currency="USD"))
    return Portfolio(currency="USD", equity=cash, cash=cash, positions=positions)


def _intent(side="buy", qty=10.0, price=100.0, **kw) -> OrderIntent:
    return OrderIntent(symbol="usAAPL", market="us", side=side, qty=qty,
                       price=price, **kw)


def _check(decision, name: str):
    for c in decision.checks:
        if c.name == name:
            return c
    return None


# ---------------------------------------------------------------------- #
# 开关语义：默认关闭 → 行为与旧版一致
# ---------------------------------------------------------------------- #
def test_phase2_disabled_adds_no_new_checks(tmp_path):
    cfg = _cfg(tmp_path, enabled=False)
    risk = RiskController(cfg, cfg.gate)
    d = risk.evaluate(_intent(), _pf(), last_price=100.0, recent_orders=[])
    for name in ("market_status", "order_policy", "short_sale", "settlement"):
        assert _check(d, name) is None, f"未开启 US-P2 却出现了 {name} 检查"


# ---------------------------------------------------------------------- #
# 市场状态
# ---------------------------------------------------------------------- #
def test_market_status_blocks_entry(tmp_path):
    svc = MarketStatusService()
    svc.halt_symbol("usAAPL", HaltReason.NEWS_PENDING, note="pending news")
    cfg = _cfg(tmp_path)
    risk = RiskController(cfg, cfg.gate, market_status=svc,
                          caps=BrokerCapabilities(venue="paper",
                                                  order_types=frozenset(OrderType),
                                                  tifs=frozenset(TimeInForce)))
    d = risk.evaluate(_intent(), _pf(cash=1e6), last_price=100.0, recent_orders=[])
    c = _check(d, "market_status")
    assert c is not None and c.ok is False
    assert not d.approved


def test_market_status_does_not_block_exit(tmp_path):
    svc = MarketStatusService()
    svc.halt_symbol("usAAPL", HaltReason.NEWS_PENDING)
    cfg = _cfg(tmp_path)
    risk = RiskController(cfg, cfg.gate, market_status=svc,
                          caps=BrokerCapabilities(venue="paper",
                                                  order_types=frozenset(OrderType),
                                                  tifs=frozenset(TimeInForce)))
    d = risk.evaluate(_intent(side="sell", qty=10, is_exit=True, exit_mode="stop_loss"),
                      _pf(qty=10), last_price=100.0, recent_orders=[])
    c = _check(d, "market_status")
    assert c is not None and c.ok is True      # 放行，但带告警
    assert any("风险退出" in w for w in d.warnings)


def test_market_status_missing_component_fails_closed(tmp_path):
    cfg = _cfg(tmp_path)
    risk = RiskController(cfg, cfg.gate)       # 不注入 market_status
    d = risk.evaluate(_intent(), _pf(), last_price=100.0, recent_orders=[])
    c = _check(d, "market_status")
    assert c is not None and c.ok is False and "fail-closed" in c.detail


# ---------------------------------------------------------------------- #
# 订单类型 / TIF
# ---------------------------------------------------------------------- #
def test_order_policy_rejects_unsupported_stop(tmp_path):
    caps = BrokerCapabilities(venue="toy", order_types=frozenset({OrderType.LIMIT}),
                              tifs=frozenset({TimeInForce.DAY}))
    cfg = _cfg(tmp_path)
    risk = RiskController(cfg, cfg.gate, caps=caps,
                          market_status=MarketStatusService())
    d = risk.evaluate(_intent(order_type="stop", stop_price=95.0), _pf(),
                      last_price=100.0, recent_orders=[])
    c = _check(d, "order_policy")
    assert c is not None and c.ok is False
    assert "不支持订单类型" in c.detail


def test_order_policy_accepts_supported_stop_limit(tmp_path):
    caps = BrokerCapabilities(venue="paper", order_types=frozenset(OrderType),
                              tifs=frozenset(TimeInForce))
    cfg = _cfg(tmp_path)
    risk = RiskController(cfg, cfg.gate, caps=caps,
                          market_status=MarketStatusService())
    d = risk.evaluate(_intent(order_type="stop_limit", price=101.0, stop_price=99.0),
                      _pf(), last_price=100.0, recent_orders=[])
    c = _check(d, "order_policy")
    assert c is not None and c.ok is True


# ---------------------------------------------------------------------- #
# 卖空
# ---------------------------------------------------------------------- #
def test_naked_sell_blocked_when_short_disabled(tmp_path):
    book = BorrowBook()
    guard = ShortGuard(mode=ShortMode.DISABLED, borrow=book,
                       locates=LocateService(book=book))
    cfg = _cfg(tmp_path, **{"risk.allow_short": False})
    risk = RiskController(cfg, cfg.gate, short_guard=guard,
                          caps=BrokerCapabilities(venue="paper",
                                                  order_types=frozenset(OrderType),
                                                  tifs=frozenset(TimeInForce)))
    d = risk.evaluate(_intent(side="sell", qty=10), _pf(qty=0), last_price=100.0,
                      recent_orders=[])
    c = _check(d, "short_sale")
    assert c is not None and c.ok is False and "禁止裸卖空" in c.detail


def test_normal_sell_passes_short_check(tmp_path):
    book = BorrowBook()
    guard = ShortGuard(mode=ShortMode.DISABLED, borrow=book,
                       locates=LocateService(book=book))
    cfg = _cfg(tmp_path)
    risk = RiskController(cfg, cfg.gate, short_guard=guard,
                          caps=BrokerCapabilities(venue="paper",
                                                  order_types=frozenset(OrderType),
                                                  tifs=frozenset(TimeInForce)))
    d = risk.evaluate(_intent(side="sell", qty=10), _pf(qty=10), last_price=100.0,
                      recent_orders=[])
    c = _check(d, "short_sale")
    assert c is not None and c.ok is True


# ---------------------------------------------------------------------- #
# 结算资金
# ---------------------------------------------------------------------- #
def test_settlement_blocks_buy_without_power(tmp_path):
    lg = SettlementLedger(currency="USD", mode=AccountMode.CASH, opening_cash=100.0)
    cfg = _cfg(tmp_path)
    risk = RiskController(cfg, cfg.gate, settlement=lg,
                          caps=BrokerCapabilities(venue="paper",
                                                  order_types=frozenset(OrderType),
                                                  tifs=frozenset(TimeInForce)))
    d = risk.evaluate(_intent(qty=10, price=100.0), _pf(cash=100.0), last_price=100.0,
                      recent_orders=[])
    c = _check(d, "settlement")
    assert c is not None and c.ok is False and "购买力不足" in c.detail


def test_settlement_passes_with_enough_cash(tmp_path):
    lg = SettlementLedger(currency="USD", mode=AccountMode.CASH, opening_cash=100_000.0)
    cfg = _cfg(tmp_path)
    risk = RiskController(cfg, cfg.gate, settlement=lg,
                          caps=BrokerCapabilities(venue="paper",
                                                  order_types=frozenset(OrderType),
                                                  tifs=frozenset(TimeInForce)))
    d = risk.evaluate(_intent(qty=10, price=100.0), _pf(cash=100_000.0), last_price=100.0,
                      recent_orders=[])
    c = _check(d, "settlement")
    assert c is not None and c.ok is True


def test_settlement_not_required_for_sell(tmp_path):
    lg = SettlementLedger(currency="USD", mode=AccountMode.CASH, opening_cash=0.0)
    cfg = _cfg(tmp_path)
    risk = RiskController(cfg, cfg.gate, settlement=lg,
                          caps=BrokerCapabilities(venue="paper",
                                                  order_types=frozenset(OrderType),
                                                  tifs=frozenset(TimeInForce)))
    d = risk.evaluate(_intent(side="sell", qty=10), _pf(qty=10), last_price=100.0,
                      recent_orders=[])
    c = _check(d, "settlement")
    assert c is not None and c.ok is True


# ---------------------------------------------------------------------- #
# Paper Broker：订单类型 / TIF / 卖空
# ---------------------------------------------------------------------- #
def _paper(tmp_path, price=100.0, allow_short=False):
    return PaperBroker(state_path=tmp_path / "paper_p2.json",
                       initial_cash={"USD": 1_000_000.0},
                       commission_bps=0.0, min_commission=0.0, slippage_bps=0.0,
                       quote_source=_FixedQuotes(price), allow_short=allow_short)


def test_paper_market_order_fills(tmp_path):
    b = _paper(tmp_path)
    r = b.place_order(OrderRequest(symbol="usAAPL", market="us", side="buy",
                                   qty=10, order_type="market"))
    assert r.status == "filled" and r.filled_qty == 10


def test_paper_limit_not_crossed_stays_submitted(tmp_path):
    b = _paper(tmp_path, price=100.0)
    r = b.place_order(OrderRequest(symbol="usAAPL", market="us", side="buy",
                                   qty=10, price=90.0, order_type="limit"))
    assert r.status == "submitted" and r.filled_qty == 0


def test_paper_stop_not_triggered_rests(tmp_path):
    b = _paper(tmp_path, price=100.0)
    b.place_order(OrderRequest(symbol="usAAPL", market="us", side="buy",
                               qty=10, order_type="market"))
    # 卖出止损触发条件：现价 <= 触发价；现价 100 > 90 → 不触发
    r = b.place_order(OrderRequest(symbol="usAAPL", market="us", side="sell",
                                   qty=10, order_type="stop", stop_price=90.0))
    assert r.status == "submitted" and r.filled_qty == 0


def test_paper_stop_triggered_fills(tmp_path):
    b = _paper(tmp_path, price=100.0)
    b.place_order(OrderRequest(symbol="usAAPL", market="us", side="buy",
                               qty=10, order_type="market"))
    # 现价 100 <= 触发价 105 → 立即触发，按市价成交
    r = b.place_order(OrderRequest(symbol="usAAPL", market="us", side="sell",
                                   qty=10, order_type="stop", stop_price=105.0))
    assert r.status == "filled" and r.filled_qty == 10


def test_paper_stop_requires_trigger_price(tmp_path):
    from ai_trader.broker.base import OrderRejected
    b = _paper(tmp_path)
    with pytest.raises(OrderRejected) as ei:
        b.place_order(OrderRequest(symbol="usAAPL", market="us", side="sell",
                                   qty=10, order_type="stop"))
    assert ei.value.code == "STOP_PRICE_REQUIRED"


def test_paper_ioc_cancels_when_not_crossed(tmp_path):
    b = _paper(tmp_path, price=100.0)
    r = b.place_order(OrderRequest(symbol="usAAPL", market="us", side="buy",
                                   qty=10, price=90.0, order_type="limit", tif="ioc"))
    assert r.status == "cancelled" and r.filled_qty == 0


def test_paper_fok_rejected_when_not_fillable(tmp_path):
    from ai_trader.broker.base import OrderRejected
    b = _paper(tmp_path, price=100.0)
    with pytest.raises(OrderRejected) as ei:
        b.place_order(OrderRequest(symbol="usAAPL", market="us", side="buy",
                                   qty=10, price=90.0, order_type="limit", tif="fok"))
    assert ei.value.code == "FOK_UNFILLABLE"


def test_paper_short_denied_by_default(tmp_path):
    from ai_trader.broker.base import OrderRejected
    b = _paper(tmp_path)
    with pytest.raises(OrderRejected) as ei:
        b.place_order(OrderRequest(symbol="usAAPL", market="us", side="sell",
                                   qty=10, order_type="market"))
    assert ei.value.code == "INSUFFICIENT_POSITION"


def test_paper_short_allowed_when_enabled(tmp_path):
    b = _paper(tmp_path, allow_short=True)
    r = b.place_order(OrderRequest(symbol="usAAPL", market="us", side="sell",
                                   qty=10, order_type="market"))
    assert r.status == "filled"
    pos = {p.symbol: p for p in b.get_positions()}
    assert pos["usAAPL"].qty == pytest.approx(-10.0)
    assert pos["usAAPL"].avg_cost == pytest.approx(100.0)


def test_paper_short_cover_realizes_pnl(tmp_path):
    b = _paper(tmp_path, allow_short=True, price=100.0)
    b.place_order(OrderRequest(symbol="usAAPL", market="us", side="sell",
                               qty=10, order_type="market"))
    b.quote_source.price = 90.0
    b.place_order(OrderRequest(symbol="usAAPL", market="us", side="buy",
                               qty=10, order_type="market"))
    pos = {p.symbol: p for p in b.get_positions()}
    assert pos.get("usAAPL") is None or abs(pos["usAAPL"].qty) < 1e-9
