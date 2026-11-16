"""LULD 恢复权限 / 成交门禁 / 多交易所隔离 / 事件版本 —— 攻击性测试。

每条用例都走**实际系统入口**（MarketStatusService / LuldLifecycle / PaperBroker），
不是只测孤立辅助函数。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from ai_trader.broker.base import OrderRequest
from ai_trader.broker.paper import PaperBroker
from ai_trader.market.luld import (
    Exchange,
    ImbalanceSide,
    LuldLifecycle,
    LuldPhase,
    PriceBandState,
)
from ai_trader.market.luld_authority import (
    ProviderKind,
    ProviderSpec,
    ResumeAuthority,
    ResumeEvent,
    ResumeRejected,
    make_resume_event,
    production_authority,
    test_authority as make_test_authority,
)
from ai_trader.market.status import (
    DayBounds,
    HaltReason,
    MarketState,
    MarketStatusService,
    Trade,
)

NY = ZoneInfo("America/New_York")
T0 = datetime(2026, 11, 16, 10, 0, tzinfo=NY)
UTC = timezone.utc


# ---------------------------------------------------------------------- #
# 构造
# ---------------------------------------------------------------------- #
def band(ref=100.0, pct=0.05):
    return PriceBandState(symbol="AAPL", reference_price=ref,
                          upper_band=ref * (1 + pct), lower_band=ref * (1 - pct),
                          tier=1, price_bucket=">3", band_width=pct,
                          effective_at=T0, expires_at=None, session="regular",
                          source="test", version=1)


def lc_paused(exchange=Exchange.NASDAQ):
    lc = LuldLifecycle("AAPL", exchange=exchange)
    lc.set_reference(100.0, T0, previous_close=100.0)
    lc.set_band(band())
    lc.on_trade(80.0, T0 + timedelta(seconds=1), in_regular=True)
    t = T0 + timedelta(seconds=17)
    lc.on_trade(80.0, t, in_regular=True)
    assert lc.phase is LuldPhase.TRADING_PAUSE
    return lc, t


def lc_ready_to_resume(exchange=Exchange.NASDAQ):
    lc, t = lc_paused(exchange)
    lc.start_auction(t + timedelta(minutes=5, seconds=1))
    lc.on_auction_update(t + timedelta(minutes=6), version=2, indicative_price=100.0)
    assert lc.phase is LuldPhase.RESUME_PENDING
    return lc, t


BOUNDS = DayBounds(open_at=datetime(2026, 11, 16, 9, 30, tzinfo=NY),
                   close_at=datetime(2026, 11, 16, 16, 0, tzinfo=NY))


def svc_in_limit_state():
    """走**官方入口** observe_trade（含常规时段 + 5 分钟参考价），进入 Limit State。"""
    svc = MarketStatusService()
    svc.reference.add("AAPL", Trade(100.0, T0), in_regular=True)
    svc.observe_trade("AAPL", 100.0, T0 + timedelta(seconds=1),
                      bounds=BOUNDS, prior_close=100.0)
    svc.observe_trade("AAPL", 80.0, T0 + timedelta(seconds=2),
                      bounds=BOUNDS, prior_close=100.0)
    return svc


def svc_in_pause():
    svc = svc_in_limit_state()
    svc.observe_trade("AAPL", 80.0, T0 + timedelta(seconds=20),
                      bounds=BOUNDS, prior_close=100.0)
    return svc


# ====================================================================== #
# A. 成交门禁（P0-1）
# ====================================================================== #
def test_limit_state_allows_in_band_fill():
    svc = svc_in_limit_state()
    st = svc.status("AAPL", now=T0 + timedelta(seconds=2))
    assert st.state is MarketState.LULD_LIMIT_STATE
    ok, why, _ = svc.fill_allowed("AAPL", now=T0 + timedelta(seconds=2), price=100.0)
    assert ok is True, why


def test_limit_state_rejects_out_of_band_fill():
    svc = svc_in_limit_state()
    ok, why, _ = svc.fill_allowed("AAPL", now=T0 + timedelta(seconds=2), price=80.0)
    assert ok is False and "越出带" in why


def test_limit_state_submit_requires_price_in_band():
    svc = svc_in_limit_state()
    assert svc.submit_gate("AAPL", now=T0 + timedelta(seconds=2), price=101.0).allowed is True
    bad = svc.submit_gate("AAPL", now=T0 + timedelta(seconds=2), price=80.0)
    assert bad.allowed is False and "越出带" in bad.reason
    no_price = svc.submit_gate("AAPL", now=T0 + timedelta(seconds=2))
    assert no_price.allowed is False and "缺少委托价" in no_price.reason


def test_pause_rejects_fill_and_submit():
    svc = svc_in_pause()
    ok, _, state = svc.fill_allowed("AAPL", now=T0 + timedelta(seconds=21), price=80.0)
    assert ok is False and state is MarketState.LULD_PAUSED
    assert svc.submit_gate("AAPL", now=T0 + timedelta(seconds=21), price=80.0).allowed is False


def test_auction_period_rejects_continuous_fill():
    """竞价期间普通连续交易不得成交（生命周期不可交易）。"""
    svc = MarketStatusService()
    svc.update_reference("AAPL", 100.0, prior_close=100.0)
    lc = svc.ensure_lifecycle("AAPL", exchange=Exchange.NASDAQ)
    lc.set_band(band())
    # 人工把生命周期推进到竞价
    lc.on_trade(80.0, T0 + timedelta(seconds=1), in_regular=True)
    t = T0 + timedelta(seconds=17)
    lc.on_trade(80.0, t, in_regular=True)
    lc.start_auction(t + timedelta(minutes=5, seconds=1))
    # 服务层状态仍是 LULD_PAUSED；但即使状态被置 OPEN，也不得成交
    svc.record_trade("AAPL", 100.0, T0 + timedelta(minutes=6))
    st = svc.status("AAPL", now=T0 + timedelta(minutes=6))
    if st.state is MarketState.OPEN:
        ok, why, _ = svc.fill_allowed("AAPL", now=T0 + timedelta(minutes=6), price=100.0)
        assert ok is False and "LULD 生命周期" in why
    else:
        assert svc.fill_allowed("AAPL", now=T0 + timedelta(minutes=6), price=100.0)[0] is False


def test_resume_pending_rejects_fill():
    lc, t = lc_ready_to_resume()
    assert lc.tradable is False
    assert lc.phase is LuldPhase.RESUME_PENDING


def test_stale_quote_blocks_fill_even_if_state_open():
    svc = MarketStatusService(max_quote_age_seconds=1)
    svc.update_reference("AAPL", 100.0, prior_close=100.0)
    svc.on_quote("AAPL", 100.0, T0)
    ok, why, state = svc.fill_allowed("AAPL", now=T0 + timedelta(seconds=30), price=100.0)
    assert ok is False and state is MarketState.DATA_STALE


def test_exit_intent_allowed_but_fill_denied_during_pause():
    """止损意图 ≠ 成交许可。"""
    svc = svc_in_pause()
    intent = svc.intent_gate("AAPL", is_exit=True, now=T0 + timedelta(seconds=21))
    assert intent.allowed is True and "退出意图" in intent.warning
    ok, _, _ = svc.fill_allowed("AAPL", now=T0 + timedelta(seconds=21), price=80.0)
    assert ok is False


# ====================================================================== #
# B. 恢复权限（P0-3）
# ====================================================================== #
def test_no_authority_cannot_resume():
    """只给时间戳和版本号，不再能恢复 —— 必须传权威事件。"""
    lc, _ = lc_ready_to_resume()
    with pytest.raises(TypeError):
        lc.on_official_resume(T0, version=999)   # 旧签名，已被移除


def test_production_authority_rejects_everything_without_registered_provider():
    lc, _ = lc_ready_to_resume()
    auth = production_authority()             # 未注册任何真实 provider
    ev = make_resume_event(lc, provider="test.exchange-sim",
                           ts=T0 + timedelta(minutes=7))
    assert lc.on_official_resume(ev, auth, now=T0 + timedelta(minutes=7)) \
        is LuldPhase.RESUME_PENDING
    assert any(e.kind == "resume_rejected" for e in lc.events)


def test_test_provider_rejected_in_production_mode():
    auth = make_test_authority(mode="production")
    lc, _ = lc_ready_to_resume()
    ev = make_resume_event(lc, ts=T0 + timedelta(minutes=7))
    lc.on_official_resume(ev, auth, now=T0 + timedelta(minutes=7))
    assert any("[TEST_PROVIDER_IN_PRODUCTION]" in e.note for e in lc.events)


def test_unauthorized_provider_rejected():
    auth = make_test_authority()
    lc, _ = lc_ready_to_resume()
    ev = make_resume_event(lc, provider="test.readonly", ts=T0 + timedelta(minutes=7))
    lc.on_official_resume(ev, auth, now=T0 + timedelta(minutes=7))
    assert any("[PROVIDER_NOT_AUTHORIZED]" in e.note for e in lc.events)


def test_unknown_provider_rejected():
    auth = make_test_authority()
    lc, _ = lc_ready_to_resume()
    ev = make_resume_event(lc, provider="someone.else", ts=T0 + timedelta(minutes=7))
    lc.on_official_resume(ev, auth, now=T0 + timedelta(minutes=7))
    assert any("[PROVIDER_UNKNOWN]" in e.note for e in lc.events)


@pytest.mark.parametrize("field,value,code", [
    ("symbol", "MSFT", "SYMBOL_MISMATCH"),
    ("exchange", "nyse", "EXCHANGE_MISMATCH"),
    ("trading_date", "2026-11-17", "TRADING_DATE_MISMATCH"),
    ("pause_event_id", "forged-pause", "PAUSE_ID_MISMATCH"),
    ("auction_id", "forged-auction", "AUCTION_ID_MISMATCH"),
    ("status", "pending", "STATUS_INVALID"),
])
def test_forged_resume_fields_rejected(field, value, code):
    auth = make_test_authority()
    lc, _ = lc_ready_to_resume()
    kwargs = {field: value}
    ev = make_resume_event(lc, provider="test.exchange-sim",
                           ts=T0 + timedelta(minutes=7), **kwargs)
    phase = lc.on_official_resume(ev, auth, now=T0 + timedelta(minutes=7))
    assert phase is LuldPhase.RESUME_PENDING, f"{field} 伪造竟然通过了"
    assert any(f"[{code}]" in e.note for e in lc.events), [e.note for e in lc.events]


def test_stale_resume_version_rejected():
    auth = make_test_authority()
    lc, _ = lc_ready_to_resume()
    t = T0 + timedelta(minutes=7)
    ok_ev = make_resume_event(lc, ts=t, event_version=10)
    assert lc.on_official_resume(ok_ev, auth, now=t) is LuldPhase.REOPENED
    # 同一标的再来一个旧版本（此时 phase 已 REOPENED，只记录不改状态）
    lc2, _ = lc_ready_to_resume()
    lc2._auth = auth
    old = make_resume_event(lc2, ts=t, event_version=10)     # 与已应用同版本
    assert lc2.on_official_resume(old, auth, now=t) is LuldPhase.RESUME_PENDING
    assert any("[VERSION_STALE]" in e.note for e in lc2.events)


def test_duplicate_resume_is_idempotent():
    auth = make_test_authority()
    lc, _ = lc_ready_to_resume()
    t = T0 + timedelta(minutes=7)
    ev = make_resume_event(lc, ts=t, event_version=20)
    assert lc.on_official_resume(ev, auth, now=t) is LuldPhase.REOPENED
    ver = lc.version
    for _ in range(5):
        lc.on_official_resume(ev, auth, now=t)          # 重复投递
    assert lc.phase is LuldPhase.REOPENED and lc.version == ver


def test_future_and_stale_timestamps_rejected():
    auth = make_test_authority()
    lc, _ = lc_ready_to_resume()
    now = T0 + timedelta(minutes=7)
    fut = make_resume_event(lc, ts=now + timedelta(hours=2), event_version=30)
    lc.on_official_resume(fut, auth, now=now)
    assert any("[TIMESTAMP_FUTURE]" in e.note for e in lc.events)

    lc2, _ = lc_ready_to_resume()
    old = make_resume_event(lc2, ts=now - timedelta(days=1), event_version=30)
    lc2.on_official_resume(old, auth, now=now)
    assert any("[TIMESTAMP_STALE]" in e.note for e in lc2.events)


def test_local_timer_cannot_resume():
    """本地计时器/连续成交都不能把状态推到可交易。"""
    lc, t = lc_paused()
    for mins in (5, 10, 100):
        lc.on_trade(100.0, t + timedelta(minutes=mins), in_regular=True)
    assert lc.phase is LuldPhase.TRADING_PAUSE and lc.tradable is False


def test_indicative_price_cannot_imply_resume():
    lc, t = lc_paused()
    lc.start_auction(t + timedelta(minutes=5, seconds=1))
    lc.on_auction_update(t + timedelta(minutes=6), version=2, indicative_price=100.0)
    assert lc.phase is LuldPhase.RESUME_PENDING
    assert lc.tradable is False


# ====================================================================== #
# C. Paper 不能被绕过
# ====================================================================== #
class _Q:
    def quote(self, symbol):
        from ai_trader.broker.base import Quote
        return Quote(symbol=symbol, market="us", last=100.0, prev_close=100.0)


def _paper(tmp_path, ms):
    return PaperBroker(state_path=tmp_path / "p.json", initial_cash={"USD": 1_000_000.0, "HKD": 1_000_000.0},
                       commission_bps=0.0, min_commission=0.0, slippage_bps=0.0,
                       quote_source=_Q(), market_status=ms)


def test_paper_cannot_bypass_gate_directly(tmp_path):
    """直接对 PaperBroker 下单（不经 Engine）也必须被门禁拦住。"""
    svc = svc_in_pause()
    b = _paper(tmp_path, svc)
    r = b.place_order(OrderRequest(symbol="usAAPL", market="us", side="buy",
                                   qty=10, order_type="market"))
    assert r.status == "submitted" and r.filled_qty == 0


def test_paper_limit_state_in_band_fills_out_of_band_blocked(tmp_path):
    svc = svc_in_limit_state()
    # 命中带内价（撮合价 100）→ 允许
    b = _paper(tmp_path, svc)
    r = b.place_order(OrderRequest(symbol="AAPL", market="us", side="buy",
                                   qty=10, price=101.0, order_type="limit"))
    # 限价 101 ≥ 现价 100 → 成交价 min(101,100)=100，带内
    assert r.status == "filled"


# ====================================================================== #
# D. 多交易所隔离（P0-4）
# ====================================================================== #
def test_two_exchanges_have_independent_state():
    svc = MarketStatusService()
    svc.halt_symbol("AAPL", exchange="nasdaq", note="nasdaq halt")
    svc.halt_symbol("AAPL", exchange="nyse", note="nyse halt")
    svc.notify_resume("AAPL", exchange="nyse")
    svc.record_trade("AAPL", 100.0, datetime.now(UTC), exchange="nyse")
    assert svc.status("AAPL", exchange="nasdaq").state is MarketState.HALTED
    nyse = svc.status("AAPL", exchange="nyse")
    assert nyse.state is not MarketState.HALTED


def test_unknown_exchange_write_does_not_clear_venue_halt():
    """``"*"`` 仍是独立桶：在默认桶登记成交**不得**抹掉 nasdaq 的停牌。

    （读取方向见下一条：未知场所查询必须保守，两者不矛盾 ——
    写入按桶隔离，读取按最严格聚合。）
    """
    svc = MarketStatusService()
    svc.halt_symbol("AAPL", exchange="nasdaq")
    svc.record_trade("AAPL", 100.0, datetime.now(UTC))
    assert svc.status("AAPL", exchange="nasdaq").state is MarketState.HALTED


def test_resume_one_exchange_does_not_touch_another():
    svc = MarketStatusService()
    svc.halt_symbol("MSFT", exchange="nasdaq")
    svc.halt_symbol("MSFT", exchange="nyse")
    svc.notify_resume("MSFT", exchange="nasdaq")
    svc.record_trade("MSFT", 300.0, datetime.now(UTC), exchange="nasdaq")
    ok_nas, _, _ = svc.fill_allowed("MSFT", exchange="nasdaq", price=300.0)
    ok_nyse, _, state = svc.fill_allowed("MSFT", exchange="nyse", price=300.0)
    assert ok_nyse is False and state is MarketState.HALTED


def test_unknown_exchange_query_is_conservative():
    """``exchange="*"`` 表示**场所未知** → 任一场所停牌都必须生效。

    旧实现把 ``"*"`` 当成独立桶，导致「停牌记在 nasdaq，用默认参数查询得到 OPEN」
    —— 而 PaperBroker 的成交门禁恰好不传 exchange，等于整道门被绕过。
    这里锁死安全方向：未知场所 = 最保守。
    """
    svc = MarketStatusService()
    svc.halt_symbol("AAPL", exchange="nasdaq")
    svc.record_trade("AAPL", 100.0, datetime.now(UTC))
    assert svc.status("AAPL").state is MarketState.HALTED
    ok, why, state = svc.fill_allowed("AAPL", price=100.0)
    assert ok is False and state is MarketState.HALTED


def test_symbol_level_halt_blocks_every_exchange():
    """不指定场所的停牌是**标的级事实**，对具体场所查询同样生效。"""
    svc = MarketStatusService()
    svc.record_trade("AAPL", 100.0, datetime.now(UTC))
    svc.halt_symbol("AAPL", HaltReason.NEWS_PENDING, ts=datetime.now(UTC))
    assert svc.status("AAPL", exchange="nasdaq").state is MarketState.HALTED
    assert svc.status("AAPL", exchange="nyse").state is MarketState.HALTED
    ok, _, _ = svc.fill_allowed("AAPL", price=100.0, exchange="nasdaq")
    assert ok is False


def test_broker_fill_gate_cannot_bypass_venue_halt(tmp_path):
    """真实链路：PaperBroker 不传 exchange，仍必须被 nasdaq 停牌拦住。"""
    svc = MarketStatusService()
    svc.record_trade("AAPL", 100.0, datetime.now(UTC))
    svc.halt_symbol("AAPL", exchange="nasdaq")
    b = _paper(tmp_path, svc)
    r = b.place_order(OrderRequest(symbol="AAPL", market="us", side="buy",
                                   qty=10, price=100.0, order_type="limit"))
    assert r.status != "filled", "交易所已停牌却成交 = 门禁被绕过"


def test_aggregation_only_tightens_never_loosens():
    """聚合只能更严：Limit State 记在 ``"*"`` 桶时，具体场所查询仍需带内价格。

    走公开入口 ``observe_trade``（越界成交 → Limit State），不直接改内部字段。
    """
    svc = MarketStatusService()
    b = DayBounds(open_at=T0.replace(hour=9, minute=30),
                  close_at=T0.replace(hour=16, minute=0))
    t = T0 + timedelta(minutes=30)
    for px in (100.0, 101.0, 100.5):
        svc.reference.add("AAPL", Trade(px, t - timedelta(seconds=3)), in_regular=True)
    svc.observe_trade("AAPL", 100.0, t - timedelta(seconds=2), bounds=b, prior_close=100.0)
    r = svc.observe_trade("AAPL", 80.0, t, bounds=b, prior_close=100.0)
    assert r.state is MarketState.LULD_LIMIT_STATE

    st = svc.status("AAPL", exchange="nasdaq")
    assert st.state is MarketState.LULD_LIMIT_STATE
    assert st.tradable is False
    ok, why, _ = svc.fill_allowed("AAPL", exchange="nasdaq", price=80.0)
    assert ok is False and "越出带" in why


def test_fused_result_does_not_pollute_stored_state():
    """聚合返回副本：拿到结果后修改不得写回存储桶。"""
    svc = MarketStatusService()
    svc.halt_symbol("AAPL", exchange="nasdaq")
    fused = svc.status("AAPL")
    assert fused.state is MarketState.HALTED
    fused.state = MarketState.OPEN
    assert svc.status("AAPL").state is MarketState.HALTED


def test_limit_state_bucket_cannot_outlive_lifecycle_pause():
    """状态桶停在 Limit State、生命周期已进入暂停 → 以生命周期为准，拒绝成交。

    防的是"两套状态不同步时挑宽松的那套用"。
    """
    svc = MarketStatusService()
    b = DayBounds(open_at=T0.replace(hour=9, minute=30),
                  close_at=T0.replace(hour=16, minute=0))
    t = T0 + timedelta(minutes=30)
    for px in (100.0, 101.0, 100.5):
        svc.reference.add("AAPL", Trade(px, t - timedelta(seconds=3)), in_regular=True)
    svc.observe_trade("AAPL", 100.0, t - timedelta(seconds=2), bounds=b, prior_close=100.0)
    assert svc.observe_trade("AAPL", 80.0, t, bounds=b,
                             prior_close=100.0).state is MarketState.LULD_LIMIT_STATE

    lc = svc.ensure_lifecycle("AAPL", exchange=Exchange.NASDAQ)
    lc.start_auction(t + timedelta(seconds=1), source="test")
    assert lc.tradable is False

    ok, why, _ = svc.fill_allowed("AAPL", price=100.0)
    assert ok is False and "生命周期" in why


def test_authority_rejects_wrong_exchange_provider():
    auth = ResumeAuthority([
        ProviderSpec("test.nyse-only", ProviderKind.TEST, frozenset({"nyse"}),
                     can_authorize_resume=True),
    ], mode="test")
    lc, _ = lc_ready_to_resume(Exchange.NASDAQ)
    ev = make_resume_event(lc, provider="test.nyse-only", ts=T0 + timedelta(minutes=7))
    lc.on_official_resume(ev, auth, now=T0 + timedelta(minutes=7))
    assert any("[PROVIDER_EXCHANGE_MISMATCH]" in e.note for e in lc.events)
