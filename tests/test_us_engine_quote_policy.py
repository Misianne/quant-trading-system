"""US-P2-A 收口测试：从**真实 Engine 入口**验证行情时段策略不可绕过。

这些用例不是单测 `SessionPricePolicy`，而是走
``StrategyEngine.scan()`` / ``monitor()``，证明闸门就在业务主路径上。

时间基准：2026-11-16（周一）
* 10:00 UTC = 05:00 纽约 → pre
* 17:00 UTC = 12:00 纽约 → regular
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ai_trader.broker.base import Quote
from ai_trader.strategy.engine import StrategyEngine

from test_us_p0 import FixedSignalModel, UsBroker, _ruleset, live_cfg

UTC = timezone.utc
REGULAR = datetime(2026, 11, 16, 17, 0, tzinfo=UTC)     # 12:00 NY
PRE = datetime(2026, 11, 16, 10, 0, tzinfo=UTC)         # 05:00 NY
POST = datetime(2026, 11, 16, 21, 30, tzinfo=UTC)       # 16:30 NY


class TsUsBroker(UsBroker):
    """带时间戳的美股券商替身 —— 用于验证时段判定。"""

    def __init__(self, prices=None, ts=None, **kw):
        super().__init__(prices, **kw)
        self.ts = ts

    def get_quote(self, symbol: str) -> Quote:
        q = super().get_quote(symbol)
        q.ts = self.ts if self.ts is not None else ""
        return q


def _engine2(tmp_path, broker, enabled: bool = True, now: datetime = REGULAR):
    from ai_trader.audit.journal import AuditJournal
    from ai_trader.risk.controller import RiskController

    cfg = live_cfg(tmp_path, **({"us_phase2.enabled": True} if enabled else {}))
    j = AuditJournal(tmp_path / "audit.db", tmp_path / "audit.jsonl")
    risk = RiskController(cfg, cfg.gate, rules=_ruleset())
    eng = StrategyEngine(cfg, broker, FixedSignalModel(), risk, j,
                         stops_path=tmp_path / "stops.json")
    eng.quote_gateway._now_fn = lambda: now
    return cfg, j, eng


# ---------------------------------------------------------------------- #
# 结构：Engine 已把行情收进网关
# ---------------------------------------------------------------------- #
def test_engine_has_unified_quote_gateway(tmp_path):
    broker = TsUsBroker(prices={"usAAPL": 200.0}, ts=REGULAR.isoformat())
    cfg, j, eng = _engine2(tmp_path, broker, enabled=True)
    try:
        assert eng.quote_gateway is not None
        assert eng.quote_gateway.broker is broker
        assert eng.quote_gateway.enabled is True
    finally:
        j.close()


def test_engine_source_has_no_direct_quote_bypass():
    """静态检查：引擎源码里不得再出现 ``self.broker.get_quote``。"""
    from pathlib import Path
    import ai_trader.strategy.engine as mod
    text = Path(mod.__file__).read_text(encoding="utf-8")
    assert "self.broker.get_quote" not in text, "行情读取出现旁路，必须走 quote_gateway"


# ---------------------------------------------------------------------- #
# 正向：常规时段行情可用
# ---------------------------------------------------------------------- #
def test_regular_quote_passes_engine_gate(tmp_path):
    broker = TsUsBroker(prices={"usAAPL": 200.0}, ts=REGULAR.isoformat())
    cfg, j, eng = _engine2(tmp_path, broker, enabled=True)
    try:
        out = eng.analyze_symbol("usAAPL", now=REGULAR)
        assert "行情不可用" not in (out.detail or ""), out.detail
    finally:
        j.close()


# ---------------------------------------------------------------------- #
# 负向：盘前价不得当盘中价
# ---------------------------------------------------------------------- #
def test_premarket_quote_rejected_for_regular_entry(tmp_path):
    broker = TsUsBroker(prices={"usAAPL": 200.0}, ts=PRE.isoformat())
    cfg, j, eng = _engine2(tmp_path, broker, enabled=True)
    try:
        out = eng.analyze_symbol("usAAPL", now=PRE)
        assert out.stage == "error"
        assert "行情不可用" in out.detail and "pre" in out.detail
    finally:
        j.close()


def test_postmarket_quote_rejected_for_regular_entry(tmp_path):
    broker = TsUsBroker(prices={"usAAPL": 200.0}, ts=POST.isoformat())
    cfg, j, eng = _engine2(tmp_path, broker, enabled=True, now=POST)
    try:
        out = eng.analyze_symbol("usAAPL", now=POST)
        assert out.stage == "error" and "行情不可用" in out.detail
    finally:
        j.close()


def test_scan_reports_error_and_places_no_order_on_premarket(tmp_path):
    broker = TsUsBroker(prices={"usAAPL": 200.0}, ts=PRE.isoformat())
    cfg, j, eng = _engine2(tmp_path, broker, enabled=True)
    try:
        rep = eng.scan(symbols=["usAAPL"], now=PRE)
        assert broker.calls["place"] == 0
        assert any("行情不可用" in (o.detail or "") for o in rep.outcomes), \
            [o.detail for o in rep.outcomes]
    finally:
        j.close()


# ---------------------------------------------------------------------- #
# 负向：过期 / 缺时间戳
# ---------------------------------------------------------------------- #
def test_stale_quote_rejected(tmp_path):
    stale = REGULAR - timedelta(minutes=5)
    broker = TsUsBroker(prices={"usAAPL": 200.0}, ts=stale.isoformat())
    cfg, j, eng = _engine2(tmp_path, broker, enabled=True)
    try:
        out = eng.analyze_symbol("usAAPL", now=REGULAR)
        assert out.stage == "error" and "过期" in out.detail
    finally:
        j.close()


def test_missing_timestamp_rejected(tmp_path):
    broker = TsUsBroker(prices={"usAAPL": 200.0}, ts="")
    cfg, j, eng = _engine2(tmp_path, broker, enabled=True)
    try:
        out = eng.analyze_symbol("usAAPL", now=REGULAR)
        assert out.stage == "error" and "时间戳" in out.detail
    finally:
        j.close()


def test_naive_timestamp_rejected(tmp_path):
    broker = TsUsBroker(prices={"usAAPL": 200.0}, ts="2026-11-16T17:00:00")  # 无时区
    cfg, j, eng = _engine2(tmp_path, broker, enabled=True)
    try:
        out = eng.analyze_symbol("usAAPL", now=REGULAR)
        assert out.stage == "error" and "时间戳" in out.detail
    finally:
        j.close()


# ---------------------------------------------------------------------- #
# 开关：默认关闭时行为与旧版一致（透传）
# ---------------------------------------------------------------------- #
def test_disabled_gateway_passes_premarket_through(tmp_path):
    broker = TsUsBroker(prices={"usAAPL": 200.0}, ts=PRE.isoformat())
    cfg, j, eng = _engine2(tmp_path, broker, enabled=False)
    try:
        out = eng.analyze_symbol("usAAPL", now=PRE)
        assert "行情不可用" not in (out.detail or "")
        assert eng.quote_gateway.stats.passthrough >= 1
    finally:
        j.close()


# ---------------------------------------------------------------------- #
# 风险退出：不因新策略被阻断
# ---------------------------------------------------------------------- #
def test_stale_quote_still_allows_stop_loss_via_position_mark(tmp_path):
    """行情过期 → 出场路径退回券商持仓 mark，止损仍然触发（US-P0 语义保持）。"""
    stale = REGULAR - timedelta(minutes=30)
    broker = TsUsBroker(prices={"usAAPL": 185.0}, ts=stale.isoformat())
    broker.positions = [__import__("ai_trader.broker.base", fromlist=["Position"]).Position(
        symbol="usAAPL", market="us", qty=10, avg_cost=200.0,
        last_price=185.0, currency="USD")]
    cfg, j, eng = _engine2(tmp_path, broker, enabled=True)
    try:
        eng.register_stops("usAAPL", stop_loss=190.0, take_profit=None, qty=10)
        out = eng.monitor(now=REGULAR)
        assert any(o.get("kind") == "exit" for o in out), out
    finally:
        j.close()


def test_risk_exit_use_case_accepts_premarket_quote(tmp_path):
    """出场允许扩展时段：盘前行情在 risk_exit 用途下可用。"""
    broker = TsUsBroker(prices={"usAAPL": 200.0}, ts=PRE.isoformat())
    cfg, j, eng = _engine2(tmp_path, broker, enabled=True, now=PRE)
    try:
        q = eng.quote_gateway.quote("usAAPL", "risk_exit", now=PRE, market="us")
        assert q.last == 200.0
    finally:
        j.close()
