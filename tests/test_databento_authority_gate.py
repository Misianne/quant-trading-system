"""Databento status 事件的权威身份测试。

本轮的核心安全命题：
**Databento 的 status 是交易所原生状态，但它没有密码学签名，
  因此不能自动获得"官方恢复"身份。**

测试分两层：
* 路由层（``AuthorityRouter``）：身份、交易所覆盖、绑定、方向、签名；
* 汇聚层（``ProviderFeed``）：真实事件流经过后，状态到底有没有被改。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ai_trader.market.providers import (
    AuthorityRouter,
    DatabentoProvider,
    EventAuthority,
    HaltEvent,
    LULDState,
    NoSignatureVerifier,
    ProviderAuthoritySpec,
    ProviderFeed,
    SignatureStatus,
    surveyed_specs,
)

VER = Path(__file__).resolve().parents[1] / "verification" / "databento" / "fixtures"
SYM = json.loads((VER / "symbology.json").read_text(encoding="utf-8"))
T0 = datetime(2026, 11, 16, 14, 30, 0, tzinfo=timezone.utc)


class Verified:
    name = "stub-verified"

    def verify(self, ev, raw):
        return SignatureStatus.VERIFIED, "测试用已验证签名"


def databento_router(*, verifier=None, require_sig=True):
    spec = [s for s in surveyed_specs() if s.provider == "databento"]
    assert spec, "databento 必须出现在权威注册表里"
    return AuthorityRouter(spec, verifier=verifier or NoSignatureVerifier(),
                           require_signature_for_resume=require_sig)


def halt_event(*, halted=True, exchange="nasdaq", symbol="AAPL",
               provider="databento", authority=EventAuthority.EXCHANGE_HALT,
               at=T0):
    return HaltEvent(provider=provider, symbol=symbol, exchange=exchange,
                     event_time=at, receive_time=at, halted=halted,
                     reason="LULD_PAUSE", halt_code="PAUSE",
                     authority=authority)


# ---------------------------------------------------------------------- #
# 注册表事实
# ---------------------------------------------------------------------- #
def test_databento_is_native_but_unsigned():
    spec = [s for s in surveyed_specs() if s.provider == "databento"][0]
    assert spec.native is True, "status 来自交易所原始馈送"
    assert spec.signed is False, "供应商未提供消息签名 —— 不得标为已签名"


def test_status_event_authority_is_exchange_halt_not_signed():
    p = DatabentoProvider(VER / "synthetic_luld_pause.jsonl", {"batch_size": 10})
    p.connect(); p.subscribe(["AAPL"])
    halts = [e for e in p.poll() if isinstance(e, HaltEvent)]
    assert all(h.authority is EventAuthority.EXCHANGE_HALT for h in halts)


# ---------------------------------------------------------------------- #
# 方向不对称：停牌可生效，恢复需签名
# ---------------------------------------------------------------------- #
def test_unsigned_halt_accepted():
    r = databento_router()
    v = r.can_apply(halt_event(halted=True), symbol="AAPL",
                    exchange="nasdaq", now=T0 + timedelta(seconds=1))
    assert v.accepted, f"停牌被拒 = fail-open：{v.reason}"
    assert v.risk_increasing is False


def test_unsigned_resume_denied():
    r = databento_router()
    v = r.can_apply(halt_event(halted=False), symbol="AAPL",
                    exchange="nasdaq", now=T0 + timedelta(seconds=1))
    assert not v.accepted
    assert "NO_SIGNATURE" in v.reason
    assert v.risk_increasing is True


def test_signed_resume_accepted():
    r = databento_router(verifier=Verified())
    v = r.can_apply(halt_event(halted=False), symbol="AAPL",
                    exchange="nasdaq", now=T0 + timedelta(seconds=1))
    assert v.accepted and v.signature is SignatureStatus.VERIFIED


def test_invalid_signature_denied_even_for_halt():
    class Broken:
        name = "broken"

        def verify(self, ev, raw):
            return SignatureStatus.INVALID, "签名校验失败"

    r = databento_router(verifier=Broken())
    v = r.can_apply(halt_event(halted=True), symbol="AAPL",
                    exchange="nasdaq", now=T0 + timedelta(seconds=1))
    assert not v.accepted and "SIGNATURE_INVALID" in v.reason


# ---------------------------------------------------------------------- #
# 身份 / 覆盖 / 绑定
# ---------------------------------------------------------------------- #
def test_unregistered_provider_denied():
    r = databento_router()
    v = r.can_apply(halt_event(provider="some_other_feed"), symbol="AAPL",
                    exchange="nasdaq", now=T0)
    assert not v.accepted and "PROVIDER_NOT_REGISTERED" in v.reason


def test_vendor_quote_cannot_drive_state():
    r = databento_router()
    v = r.can_apply(halt_event(authority=EventAuthority.VENDOR_QUOTE),
                    symbol="AAPL", exchange="nasdaq", now=T0)
    assert not v.accepted and "VENDOR_EVENT_CANNOT_DRIVE_STATE" in v.reason


def test_exchange_not_covered_denied():
    """Databento 未覆盖的交易所不得被它的事件改变状态。"""
    r = databento_router()
    v = r.can_apply(halt_event(exchange="tokyo"), symbol="7203",
                    exchange="tokyo", now=T0)
    assert not v.accepted and "EXCHANGE_NOT_COVERED" in v.reason


def test_symbol_mismatch_denied():
    r = databento_router()
    v = r.can_apply(halt_event(symbol="MSFT"), symbol="AAPL",
                    exchange="nasdaq", now=T0)
    assert not v.accepted and "SYMBOL_MISMATCH" in v.reason


def test_exchange_mismatch_denied():
    r = databento_router()
    v = r.can_apply(halt_event(exchange="nyse"), symbol="AAPL",
                    exchange="nasdaq", now=T0)
    assert not v.accepted and "EXCHANGE_MISMATCH" in v.reason


def test_future_event_denied():
    r = databento_router()
    v = r.can_apply(halt_event(at=T0 + timedelta(hours=2)), symbol="AAPL",
                    exchange="nasdaq", now=T0)
    assert not v.accepted and "EVENT_IN_FUTURE" in v.reason


def test_stale_event_cannot_resume():
    """历史事件重放不得绕过时效检查。"""
    r = databento_router(verifier=Verified())
    v = r.can_apply(halt_event(halted=False, at=T0 - timedelta(days=3)),
                    symbol="AAPL", exchange="nasdaq", now=T0)
    assert not v.accepted and "EVENT_TOO_OLD" in v.reason


def test_stale_event_cannot_even_halt():
    r = databento_router()
    v = r.can_apply(halt_event(halted=True, at=T0 - timedelta(days=3)),
                    symbol="AAPL", exchange="nasdaq", now=T0)
    assert not v.accepted and "EVENT_TOO_OLD" in v.reason


def test_version_replay_denied():
    r = databento_router(verifier=Verified())
    kw = dict(symbol="AAPL", exchange="nasdaq", now=T0)
    first = r.can_apply(halt_event(halted=False), version=5, **kw)
    assert first.accepted
    replay = r.can_apply(halt_event(halted=False), version=5, **kw)
    assert not replay.accepted and "VERSION_STALE" in replay.reason


def test_luld_state_is_treated_as_risk_increasing():
    """不认识的放宽类事件一律按放大风险处理 → 未签名必拒。"""
    r = databento_router()
    ev = LULDState(provider="databento", symbol="AAPL", exchange="nasdaq",
                   event_time=T0, receive_time=T0, upper=210, lower=190,
                   paused=False, resumed=True,
                   authority=EventAuthority.EXCHANGE_HALT)
    v = r.can_apply(ev, symbol="AAPL", exchange="nasdaq", now=T0)
    assert not v.accepted and "NO_SIGNATURE" in v.reason
    assert v.risk_increasing is True


# ---------------------------------------------------------------------- #
# 汇聚层：真实事件流
# ---------------------------------------------------------------------- #
def _feed(name, *, verifier=None, batch=1):
    p = DatabentoProvider(VER / name, {"batch_size": batch, "symbology": SYM})
    r = databento_router(verifier=verifier)
    f = ProviderFeed([p], authority_router=r, max_age_seconds=10 ** 9)
    f.connect_all(); f.subscribe(["AAPL"])
    return p, f, r


def test_feed_applies_unsigned_halt():
    _, f, _ = _feed("synthetic_luld_pause.jsonl")
    for _ in range(3):
        f.poll()
    st = f.state("AAPL", "nasdaq")
    assert st.halted is True


def test_feed_does_not_let_unsigned_resume_clear_halt():
    _, f, r = _feed("synthetic_luld_pause.jsonl")
    for _ in range(6):
        f.poll()
    st = f.state("AAPL", "nasdaq")
    assert st.halted is True, "未签名恢复事件把停牌解除了"
    assert f.rejected.get("halt_not_authoritative", 0) >= 1
    reasons = [a.reason for a in r.audit if not a.accepted]
    assert any("NO_SIGNATURE" in x for x in reasons)


def test_feed_allows_resume_with_verified_signature():
    _, f, _ = _feed("synthetic_luld_pause.jsonl", verifier=Verified())
    for _ in range(6):
        f.poll()
    st = f.state("AAPL", "nasdaq")
    assert st.halted is False, "已验证签名的恢复事件应当生效"


def test_feed_halts_on_news_halt():
    _, f, _ = _feed("synthetic_news_halt.jsonl")
    for _ in range(3):
        f.poll()
    assert f.state("AAPL", "nasdaq").halted is True


def test_trades_during_halt_do_not_clear_halt():
    """停牌期间出现成交（迟到打印）不得让标的回到可交易。"""
    _, f, _ = _feed("synthetic_luld_pause.jsonl")
    for _ in range(6):
        f.poll()
    assert f.state("AAPL", "nasdaq").halted is True


def test_authority_audit_has_no_plaintext_secret():
    _, f, r = _feed("synthetic_luld_pause.jsonl")
    for _ in range(6):
        f.poll()
    blob = json.dumps([a.__dict__ for a in r.audit], ensure_ascii=False)
    assert "api_key" not in blob.lower()
