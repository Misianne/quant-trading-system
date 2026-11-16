"""P1-1 / P1-2：持仓读取失败必须报错；日内盈亏缺失必须拒单。

审计原文：
* 持仓解析失败静默返回 [] → 风控 fail-open
* 券商不回 dayPnl → 单日亏损熔断"安静通过"
"""
from __future__ import annotations

import pytest

from ai_trader.audit.journal import AuditJournal
from ai_trader.broker.base import BrokerError
from ai_trader.broker.fosun_adapter import FosunBroker
from ai_trader.risk.controller import OrderIntent, Portfolio, RiskController
from ai_trader.strategy.engine import StrategyEngine
from tests.conftest import WEEKDAY_1000
from tests.helpers import FixedSignalModel, ScriptedBroker, live_cfg


class _PortfolioAPI:
    def __init__(self, holdings=None, summary=None, exc=None):
        self.holdings = holdings
        self.summary = summary
        self.exc = exc

    def get_holdings(self, **kw):
        if self.exc:
            raise self.exc
        return self.holdings

    def get_assets_summary(self, **kw):
        if self.exc:
            raise self.exc
        return self.summary


def _broker(tmp_path, **kw):
    cfg = live_cfg(tmp_path)
    b = FosunBroker(cfg, None)
    b.client = type("C", (), {"portfolio": _PortfolioAPI(**kw)})()
    b.configured = True
    return b


# ------------------------------------------------------------------ 持仓
def test_positions_unknown_shape_raises_not_empty(tmp_path):
    """审计复现：结构不认识时必须抛错，而不是返回 []。"""
    b = _broker(tmp_path, holdings={"code": 0, "data": {"foo": [{"bar": 1}]}})
    with pytest.raises(BrokerError) as ei:
        b.get_positions()
    assert ei.value.code == "PARSE_MISMATCH"
    assert "空持仓" in str(ei.value)


def test_positions_empty_dict_raises(tmp_path):
    b = _broker(tmp_path, holdings={"code": 0, "data": {}})
    with pytest.raises(BrokerError):
        b.get_positions()


def test_positions_genuinely_empty_is_ok(tmp_path):
    """真正的空账户（list: []）必须正常返回空列表，不能误报。"""
    b = _broker(tmp_path, holdings={"code": 0, "data": {"list": []}})
    assert b.get_positions() == []


def test_positions_row_without_symbol_raises(tmp_path):
    b = _broker(tmp_path, holdings={"code": 0, "data": {"list": [{"foo": 1}]}})
    with pytest.raises(BrokerError) as ei:
        b.get_positions()
    assert ei.value.code == "PARSE_MISMATCH"


def test_positions_normal_parse(tmp_path):
    b = _broker(tmp_path, holdings={"code": 0, "data": {"list": [{
        "stockCode": "00700", "marketCode": "hk", "qty": "100",
        "avgCost": "350.5", "lastPrice": "360.0", "currency": "HKD",
        "stockName": "腾讯控股"}]}})
    pos = b.get_positions()
    assert len(pos) == 1
    assert pos[0].symbol == "hk00700"
    assert pos[0].qty == 100
    assert pos[0].avg_cost == 350.5


def test_scan_blocked_when_positions_read_fails(tmp_path):
    """持仓读不出来 → 整轮扫描必须停手，而不是"假装没持仓"继续下单。"""
    cfg = live_cfg(tmp_path, **{"risk.enforce_market_hours": False})
    journal = AuditJournal(tmp_path / "audit.db", tmp_path / "audit.jsonl")
    broker = ScriptedBroker(positions_exc=BrokerError("持仓接口 500", code="UPSTREAM"))
    risk = RiskController(cfg, cfg.gate)
    engine = StrategyEngine(cfg, broker, FixedSignalModel(), risk, journal,
                            stops_path=tmp_path / "stops.json")

    report = engine.scan(["hk00700"], now=WEEKDAY_1000)
    assert report.blocked_reason
    assert "持仓" in report.blocked_reason
    assert report.outcomes == []
    assert broker.calls["place"] == 0, "持仓不可读时绝不允许下单"
    assert any(e["event"] == "scan_blocked" for e in journal.list_audit(limit=20))
    journal.close()


# ------------------------------------------------------------------ 日内盈亏
def _summary_broker(tmp_path, row):
    return _broker(tmp_path, summary={"code": 0, "data": {"list": [row]}})


def test_day_pnl_missing_marks_unknown(tmp_path):
    b = _summary_broker(tmp_path, {"currency": "HKD", "cash": "1000000",
                                   "marketValue": "0", "totalAsset": "1000000"})
    s = b.get_summary("HKD")
    assert s.day_pnl_known is False, "券商没给 dayPnl 时不能假装是 0"
    assert s.equity == 1_000_000.0


def test_day_pnl_present_marks_known(tmp_path):
    b = _summary_broker(tmp_path, {"currency": "HKD", "cash": "900000",
                                   "marketValue": "100000", "totalAsset": "1000000",
                                   "dayPnl": "-20000"})
    s = b.get_summary("HKD")
    assert s.day_pnl_known is True
    assert s.day_pnl == -20_000.0


def test_daily_loss_check_fails_when_pnl_unknown(tmp_path):
    cfg = live_cfg(tmp_path)
    risk = RiskController(cfg, cfg.gate)
    pf = Portfolio(currency="HKD", equity=1_000_000.0, cash=1_000_000.0,
                   day_pnl=0.0, day_pnl_known=False)
    d = risk.evaluate(OrderIntent(symbol="hk00700", side="buy", qty=100, price=300.0,
                                  market="hk", currency="HKD", stop_loss=290.0),
                      pf, last_price=300.0, now=WEEKDAY_1000, reserve_attempt=False)
    assert d.approved is False
    assert "daily_loss_limit" in d.failed_checks
    assert any("无法判定" in r or "不可用" in r for r in d.reasons)


def test_daily_loss_check_passes_when_pnl_known_and_small(tmp_path):
    cfg = live_cfg(tmp_path)
    risk = RiskController(cfg, cfg.gate)
    pf = Portfolio(currency="HKD", equity=1_000_000.0, cash=1_000_000.0,
                   day_pnl=-5_000.0, day_pnl_known=True)
    d = risk.evaluate(OrderIntent(symbol="hk00700", side="buy", qty=100, price=300.0,
                                  market="hk", currency="HKD", stop_loss=290.0),
                      pf, last_price=300.0, now=WEEKDAY_1000, reserve_attempt=False)
    assert "daily_loss_limit" not in d.failed_checks, d.failed_checks


def test_engine_propagates_unknown_day_pnl(tmp_path):
    """适配器 → 引擎 → 风控 全链路：dayPnl 缺失必须导致拒单。"""
    cfg = live_cfg(tmp_path, **{"risk.enforce_market_hours": False})
    journal = AuditJournal(tmp_path / "audit.db", tmp_path / "audit.jsonl")
    risk = RiskController(cfg, cfg.gate)
    broker = ScriptedBroker()          # day_pnl_known=True 的对照
    engine = StrategyEngine(cfg, broker, FixedSignalModel(), risk, journal,
                            stops_path=tmp_path / "stops.json")
    ok = engine.scan(["hk00700"], now=WEEKDAY_1000)
    assert ok.outcomes[0].stage == "ordered"

    # 换成"不知道日内盈亏"的券商
    broker2 = ScriptedBroker()
    broker2.get_summary = lambda currency="": __import__(
        "ai_trader.broker.base", fromlist=["AccountSummary"]).AccountSummary(
        currency=currency or "HKD", cash=1_000_000.0, market_value=0.0,
        equity=1_000_000.0, day_pnl=0.0, day_pnl_known=False)
    engine2 = StrategyEngine(cfg, broker2, FixedSignalModel(), risk, journal,
                             stops_path=tmp_path / "stops2.json")
    rep = engine2.scan(["hk09988"], now=WEEKDAY_1000)
    assert rep.outcomes[0].stage == "rejected"
    assert "daily_loss_limit" in rep.outcomes[0].decision["checks"] or any(
        c["name"] == "daily_loss_limit" for c in rep.outcomes[0].decision["checks"])
    journal.close()
