"""权威事件认证：A/B/C/D 分类、签名、伪造、重放、跨交易所。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from ai_trader.market.providers import (
    AuthorityRouter,
    EventAuthority,
    HaltEvent,
    LULDState,
    NoSignatureVerifier,
    ProviderAuthoritySpec,
    SignatureStatus,
)

T0 = datetime(2026, 11, 16, 14, 30, tzinfo=timezone.utc)
NOW = T0 + timedelta(seconds=1)


class Verified:
    name = "verified"

    def verify(self, ev, raw):
        return SignatureStatus.VERIFIED, "ok"


class Invalid:
    name = "invalid"

    def verify(self, ev, raw):
        return SignatureStatus.INVALID, "bad signature"


def halt(provider="nasdaq_itch", symbol="AAPL", exchange="nasdaq",
         authority=EventAuthority.EXCHANGE_HALT, at=T0, halted=True):
    return HaltEvent(provider=provider, symbol=symbol, exchange=exchange,
                     event_time=at, receive_time=at, halted=halted,
                     reason="LUDP", authority=authority)


def router(*specs, verifier=None, require_sig=True):
    return AuthorityRouter(list(specs), verifier=verifier or NoSignatureVerifier(),
                           require_signature_for_resume=require_sig)


NATIVE = ProviderAuthoritySpec("nasdaq_itch", EventAuthority.EXCHANGE_HALT,
                               exchanges=("nasdaq",), native=True, signed=True)


# ---------------------------------------------------------------------- #
def test_unregistered_provider_rejected():
    r = router(NATIVE)
    v = r.can_apply(halt(provider="ghost"), symbol="AAPL", exchange="nasdaq", now=NOW)
    assert not v.accepted and "PROVIDER_NOT_REGISTERED" in v.reason


def test_non_native_provider_rejected():
    spec = ProviderAuthoritySpec("massive", EventAuthority.LULD_OFFICIAL,
                                 exchanges=("nasdaq",), native=False,
                                 nasdaq_listed_only=True)
    r = router(spec, verifier=Verified())
    ev = LULDState(provider="massive", symbol="AAPL", exchange="nasdaq",
                   event_time=T0, receive_time=T0, upper=105, lower=95,
                   authority=EventAuthority.LULD_OFFICIAL)
    v = r.can_apply(ev, symbol="AAPL", exchange="nasdaq", now=NOW)
    assert not v.accepted and "NOT_NATIVE" in v.reason


def test_vendor_quote_cannot_drive_state():
    r = router(NATIVE, verifier=Verified())
    ev = halt(authority=EventAuthority.VENDOR_QUOTE)
    v = r.can_apply(ev, symbol="AAPL", exchange="nasdaq", now=NOW)
    assert not v.accepted and "VENDOR_EVENT" in v.reason


def test_authority_mismatch():
    r = router(NATIVE, verifier=Verified())
    ev = halt(authority=EventAuthority.LULD_OFFICIAL)
    v = r.can_apply(ev, symbol="AAPL", exchange="nasdaq", now=NOW)
    assert not v.accepted and "AUTHORITY_MISMATCH" in v.reason


def test_exchange_not_covered():
    spec = ProviderAuthoritySpec("nyse_pillar", EventAuthority.EXCHANGE_HALT,
                                 exchanges=("nyse",), native=True)
    r = router(spec, verifier=Verified())
    ev = halt(provider="nyse_pillar")      # 已注册，但只覆盖 nyse
    v = r.can_apply(ev, symbol="AAPL", exchange="nasdaq", now=NOW)
    assert not v.accepted and "EXCHANGE_NOT_COVERED" in v.reason


def test_exchange_mismatch():
    r = router(NATIVE, verifier=Verified())
    ev = halt(exchange="nyse")
    v = r.can_apply(ev, symbol="AAPL", exchange="nasdaq", now=NOW)
    assert not v.accepted and "EXCHANGE_MISMATCH" in v.reason


def test_symbol_mismatch_forged():
    """伪造：拿 MSFT 的事件去恢复 AAPL。"""
    r = router(NATIVE, verifier=Verified())
    ev = halt(symbol="MSFT")
    v = r.can_apply(ev, symbol="AAPL", exchange="nasdaq", now=NOW)
    assert not v.accepted and "SYMBOL_MISMATCH" in v.reason


def test_nasdaq_listed_only_blocks_other_exchanges():
    spec = ProviderAuthoritySpec("massive", EventAuthority.LULD_OFFICIAL,
                                 exchanges=("nasdaq", "nyse"), native=True,
                                 nasdaq_listed_only=True)
    r = router(spec, verifier=Verified())
    ev = LULDState(provider="massive", symbol="IBM", exchange="nyse",
                   event_time=T0, receive_time=T0, upper=105, lower=95,
                   authority=EventAuthority.LULD_OFFICIAL)
    v = r.can_apply(ev, symbol="IBM", exchange="nyse", now=NOW)
    assert not v.accepted and "NASDAQ_LISTED_ONLY" in v.reason


def test_future_event_rejected():
    r = router(NATIVE, verifier=Verified())
    ev = halt(at=NOW + timedelta(minutes=5))
    v = r.can_apply(ev, symbol="AAPL", exchange="nasdaq", now=NOW)
    assert not v.accepted and "FUTURE" in v.reason


def test_stale_event_rejected():
    r = router(NATIVE, verifier=Verified())
    ev = halt(at=NOW - timedelta(hours=1))
    v = r.can_apply(ev, symbol="AAPL", exchange="nasdaq", now=NOW)
    assert not v.accepted and "TOO_OLD" in v.reason


def test_replayed_resume_rejected_by_version():
    r = router(NATIVE, verifier=Verified())
    ev = halt(halted=False)
    v1 = r.can_apply(ev, symbol="AAPL", exchange="nasdaq", now=NOW, version=5)
    assert v1.accepted
    v2 = r.can_apply(ev, symbol="AAPL", exchange="nasdaq", now=NOW, version=5)
    assert not v2.accepted and "VERSION_STALE" in v2.reason


def test_unsigned_event_rejected_by_default():
    """本轮调查的所有供应商都没有消息签名 → 默认必须拒绝。"""
    r = router(NATIVE)          # 默认 NoSignatureVerifier
    v = r.can_apply(halt(), symbol="AAPL", exchange="nasdaq", now=NOW)
    assert not v.accepted and "NO_SIGNATURE" in v.reason
    assert v.signature is SignatureStatus.UNSIGNED


def test_unsigned_allowed_only_when_explicitly_configured():
    r = router(NATIVE, require_sig=False)
    v = r.can_apply(halt(), symbol="AAPL", exchange="nasdaq", now=NOW)
    assert v.accepted
    assert v.signature is SignatureStatus.UNSIGNED


def test_invalid_signature_rejected_even_if_not_required():
    r = router(NATIVE, verifier=Invalid(), require_sig=False)
    v = r.can_apply(halt(), symbol="AAPL", exchange="nasdaq", now=NOW)
    assert not v.accepted and "SIGNATURE_INVALID" in v.reason


def test_verified_signature_accepted():
    r = router(NATIVE, verifier=Verified())
    v = r.can_apply(halt(), symbol="AAPL", exchange="nasdaq", now=NOW)
    assert v.accepted and v.signature is SignatureStatus.VERIFIED


def test_audit_records_every_verdict():
    r = router(NATIVE, verifier=Verified())
    r.can_apply(halt(), symbol="AAPL", exchange="nasdaq", now=NOW)
    r.can_apply(halt(symbol="MSFT"), symbol="AAPL", exchange="nasdaq", now=NOW)
    assert len(r.audit) == 2
    assert r.audit[0].accepted is True and r.audit[1].accepted is False


def test_classify_vendor_payload_is_A():
    r = router(NATIVE)
    assert r.classify("alpaca", "T", {}) is EventAuthority.VENDOR_QUOTE
    assert r.classify("massive", "T", {}) is EventAuthority.VENDOR_QUOTE


def test_classify_native_status_is_B():
    r = router(NATIVE)
    assert r.classify("nasdaq_itch", "trading_action", {}) is EventAuthority.EXCHANGE_HALT


def test_classify_unregistered_is_A():
    r = router()
    assert r.classify("unknown_vendor", "halt", {}) is EventAuthority.VENDOR_QUOTE


def test_snapshot_exposes_policy():
    r = router(NATIVE)
    snap = r.snapshot()
    assert snap["require_signature_for_resume"] is True
    assert snap["verifier"] == "none"
    assert "nasdaq_itch" in snap["providers"]
