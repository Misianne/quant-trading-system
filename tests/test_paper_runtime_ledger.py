"""成交账本 / 不变量 / 启动恢复测试。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_trader.paper_runtime.invariants import check_all
from ai_trader.paper_runtime.ledger import Fill, FillLedger, fills_from_orders
from ai_trader.paper_runtime.recovery import scan as recovery_scan


def mk(**kw) -> Fill:
    base = dict(fill_id="F1", order_id="O1", symbol="USAAPL", side="buy",
                qty=10.0, price=100.0, fee=1.0, currency="USD", ts="2026-11-16T10:00:00-05:00")
    base.update(kw)
    return Fill(**base)


# ---------------------------------------------------------------------- #
# 账本
# ---------------------------------------------------------------------- #
def test_post_then_duplicate(tmp_path):
    led = FillLedger(tmp_path / "f.jsonl")
    r1 = led.post(mk())
    r2 = led.post(mk())
    assert r1.accepted and not r1.duplicate
    assert r2.accepted and r2.duplicate
    assert len(led.fills()) == 1


def test_duplicate_does_not_change_totals(tmp_path):
    led = FillLedger(tmp_path / "f.jsonl")
    led.post(mk())
    led.post(mk())
    assert led.cash_delta()["USD"] == pytest.approx(-1001.0)


def test_cash_delta_buy_and_sell(tmp_path):
    led = FillLedger(tmp_path / "f.jsonl")
    led.post(mk(fill_id="b", side="buy", qty=10, price=100, fee=1))
    led.post(mk(fill_id="s", side="sell", qty=4, price=110, fee=0.5))
    assert led.cash_delta()["USD"] == pytest.approx(-1000 - 1 + 440 - 0.5)


def test_qty_by_symbol_nets(tmp_path):
    led = FillLedger(tmp_path / "f.jsonl")
    led.post(mk(fill_id="b", side="buy", qty=10))
    led.post(mk(fill_id="s", side="sell", qty=3))
    assert led.qty_by_symbol()["USAAPL"] == pytest.approx(7.0)


def test_reload_from_disk(tmp_path):
    p = tmp_path / "f.jsonl"
    led = FillLedger(p)
    led.post(mk())
    led2 = FillLedger(p)
    rep = led2.load()
    assert rep.ok and rep.fills == 1
    assert led2.fills()[0].fill_id == "F1"


def test_torn_last_line_isolated(tmp_path):
    p = tmp_path / "f.jsonl"
    led = FillLedger(p)
    led.post(mk())
    with open(p, "a", encoding="utf-8") as fh:
        fh.write('{"fill_id": "F2", "order_id": "O2", "sym')   # 截断
    led2 = FillLedger(p)
    rep = led2.load()
    assert rep.ok is True
    assert rep.fills == 1
    assert rep.torn_lines == 1
    assert "隔离" in rep.errors[0]


def test_corrupt_middle_line_marks_not_ok(tmp_path):
    p = tmp_path / "f.jsonl"
    led = FillLedger(p)
    led.post(mk(fill_id="F1"))
    with open(p, "a", encoding="utf-8") as fh:
        fh.write("{broken}\n")
    led.post(mk(fill_id="F3"))
    led2 = FillLedger(p)
    rep = led2.load()
    assert rep.ok is False


def test_digest_independent_of_insertion_order(tmp_path):
    a = FillLedger(tmp_path / "a.jsonl")
    b = FillLedger(tmp_path / "b.jsonl")
    a.post(mk(fill_id="F1")); a.post(mk(fill_id="F2", qty=5))
    b.post(mk(fill_id="F2", qty=5)); b.post(mk(fill_id="F1"))
    assert a.digest() == b.digest()


@pytest.mark.parametrize("bad,reason", [
    (mk(fill_id=""), "fill_id"),
    (mk(qty=0), "数量"),
    (mk(price=0), "成交价"),
    (mk(side="hold"), "方向"),
])
def test_invalid_fill_rejected(tmp_path, bad, reason):
    led = FillLedger(tmp_path / "f.jsonl")
    r = led.post(bad)
    assert not r.accepted and reason in r.reason
    assert led.fills() == []


def test_fills_from_orders_skips_unfilled():
    orders = [
        {"order_id": "1", "status": "submitted", "filled_qty": 0, "symbol": "A", "side": "buy"},
        {"order_id": "2", "status": "filled", "filled_qty": 5, "avg_fill_price": 10,
         "symbol": "A", "side": "buy", "commission": 0, "currency": "USD"},
    ]
    fills = fills_from_orders(orders)
    assert len(fills) == 1 and fills[0].order_id == "2"


def test_fills_from_orders_uses_stored_fill_id():
    orders = [{"order_id": "9", "status": "filled", "filled_qty": 1,
               "avg_fill_price": 3, "symbol": "A", "side": "sell",
               "fill_id": "FPR00000009-1", "commission": 0}]
    assert fills_from_orders(orders)[0].fill_id == "FPR00000009-1"


# ---------------------------------------------------------------------- #
# 不变量
# ---------------------------------------------------------------------- #
class FakeBroker:
    def __init__(self, cash, positions, orders, opening=None, allow_short=False):
        self._state = {"cash": cash, "positions": positions, "orders": orders}
        self.initial_cash = opening if opening is not None else dict(cash)
        self.allow_short = allow_short

    def orders_snapshot(self):
        return list(self._state["orders"].values())


def _led_with(fills):
    import tempfile
    d = tempfile.mkdtemp()
    led = FillLedger(Path(d) / "f.jsonl")
    for f in fills:
        led.post(f)
    return led


def test_invariants_clean_account():
    led = _led_with([mk()])
    b = FakeBroker({"USD": 100000 - 1001}, {"USAAPL": {"qty": 10}}, {}, opening={"USD": 100000})
    rep = check_all(b, led)
    assert rep.ok, [r.detail for r in rep.results if not r.ok]


def test_invariant_cash_mismatch():
    led = _led_with([mk()])
    b = FakeBroker({"USD": 100000}, {"USAAPL": {"qty": 10}}, {}, opening={"USD": 100000})
    rep = check_all(b, led)
    assert not rep.ok
    assert any(r.name == "cash_matches_ledger" for r in rep.critical_failures)


def test_invariant_position_mismatch():
    led = _led_with([mk()])
    b = FakeBroker({"USD": 98999}, {"USAAPL": {"qty": 7}}, {}, opening={"USD": 100000})
    rep = check_all(b, led)
    assert any(r.name == "positions_match_fills" for r in rep.critical_failures)


def test_invariant_overfill():
    led = _led_with([])
    orders = {"1": {"order_id": "1", "qty": 5, "filled_qty": 6, "status": "filled"}}
    b = FakeBroker({"USD": 0}, {}, orders, opening={"USD": 0})
    rep = check_all(b, led)
    assert any(r.name == "no_overfill" for r in rep.critical_failures)


def test_invariant_duplicate_fill_id_across_orders():
    led = _led_with([mk()])
    orders = {
        "1": {"order_id": "1", "qty": 10, "filled_qty": 10, "fill_id": "F1", "status": "filled"},
        "2": {"order_id": "2", "qty": 10, "filled_qty": 10, "fill_id": "F1", "status": "filled"},
    }
    b = FakeBroker({"USD": 100000 - 1001}, {"USAAPL": {"qty": 10}}, orders, opening={"USD": 100000})
    rep = check_all(b, led)
    assert any(r.name == "no_duplicate_fill" for r in rep.critical_failures)


def test_invariant_short_position_rejected_when_disabled():
    led = _led_with([mk(side="sell", qty=5)])
    b = FakeBroker({"USD": 100499}, {"USAAPL": {"qty": -5}}, {}, opening={"USD": 100000})
    rep = check_all(b, led, short_allowed=False)
    assert any(r.name == "short_policy" for r in rep.critical_failures)


def test_invariant_short_allowed_passes():
    led = _led_with([mk(side="sell", qty=5)])
    b = FakeBroker({"USD": 100499}, {"USAAPL": {"qty": -5}}, {}, opening={"USD": 100000})
    rep = check_all(b, led, short_allowed=True)
    assert all(r.ok for r in rep.results if r.name == "short_policy")


def test_invariant_settlement_absent_is_warning_only():
    led = _led_with([])
    b = FakeBroker({"USD": 1.0}, {}, {}, opening={"USD": 1.0})
    rep = check_all(b, led, settlement=None)
    assert rep.ok
    assert any(r.severity == "warn" for r in rep.results)


# ---------------------------------------------------------------------- #
# 恢复扫描
# ---------------------------------------------------------------------- #
def test_recovery_backfills_missing_ledger(tmp_path):
    led = FillLedger(tmp_path / "f.jsonl")
    orders = {"1": {"order_id": "1", "status": "filled", "filled_qty": 10,
                    "avg_fill_price": 100, "symbol": "USAAPL", "side": "buy",
                    "commission": 1, "currency": "USD", "fill_id": "F1"}}
    b = FakeBroker({"USD": 98999}, {"USAAPL": {"qty": 10}}, orders, opening={"USD": 100000})
    rep = recovery_scan(b, led)
    assert rep.ok and rep.backfilled == 1
    assert len(led.fills()) == 1


def test_recovery_backfill_is_idempotent(tmp_path):
    led = FillLedger(tmp_path / "f.jsonl")
    orders = {"1": {"order_id": "1", "status": "filled", "filled_qty": 10,
                    "avg_fill_price": 100, "symbol": "USAAPL", "side": "buy",
                    "commission": 1, "currency": "USD", "fill_id": "F1"}}
    b = FakeBroker({"USD": 98999}, {"USAAPL": {"qty": 10}}, orders, opening={"USD": 100000})
    recovery_scan(b, led)
    rep2 = recovery_scan(b, led)
    assert rep2.backfilled == 0
    assert len(led.fills()) == 1


def test_recovery_orphan_fails_closed(tmp_path):
    led = FillLedger(tmp_path / "f.jsonl")
    led.post(mk(fill_id="GHOST"))
    b = FakeBroker({"USD": 100000}, {}, {}, opening={"USD": 100000})
    rep = recovery_scan(b, led)
    assert rep.ok is False
    assert rep.orphans == 1
    assert any(a.kind == "orphan_in_ledger" for a in rep.actions)


def test_recovery_no_backfill_when_disallowed(tmp_path):
    led = FillLedger(tmp_path / "f.jsonl")
    orders = {"1": {"order_id": "1", "status": "filled", "filled_qty": 1,
                    "avg_fill_price": 10, "symbol": "A", "side": "buy",
                    "commission": 0, "currency": "USD"}}
    b = FakeBroker({"USD": 0}, {"A": {"qty": 1}}, orders, opening={"USD": 0})
    rep = recovery_scan(b, led, allow_backfill=False)
    assert rep.ok is False
