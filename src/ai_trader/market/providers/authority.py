"""权威事件认证：谁能改交易状态，以及凭什么。

四类事件必须严格区分
--------------------
========  ============================  ==========================
类别      含义                          能否驱动交易状态机
========  ============================  ==========================
A         普通行情（含供应商 halt 标记）  不能
B         交易所官方停复牌消息            能（且仅对该交易所）
C         LULD 官方价格带/状态            能（且仅对覆盖的标的）
D         MWCB 官方全市场状态             能（全市场）
========  ============================  ==========================

**供应商的 ``halt: true`` 永远只是 A 类。** 它可能来自供应商自己的推断、
可能是延迟的、可能只覆盖部分交易所。把它当 B 类会把"未经核验的字段"
伪装成交易所官方事件 —— 这正是本项目明令禁止的。

签名问题（必须诚实）
--------------------
本轮调查的所有自助供应商（Massive / Alpaca / Databento）**都没有**提供
对 halt/resume 消息的密码学签名。因此：

* ``NoSignatureVerifier`` 是默认实现，任何事件都会被标为 ``UNSIGNED``；
* 默认策略 ``require_signature_for_resume=True`` 时，**未签名事件不得恢复交易**；
* 想放开必须显式注入一个真正的验证器（例如交易所授权的 mTLS + 消息签名），
  并在配置里写明来源。

这条策略让"接入真实行情"和"能自动恢复交易"彻底解耦：
前者可以推进，后者仍然是 BLOCKED。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Protocol, Tuple

from .models import EventAuthority, MarketEvent


class SignatureStatus(str, Enum):
    VERIFIED = "verified"
    UNSIGNED = "unsigned"
    INVALID = "invalid"
    UNSUPPORTED = "unsupported"


class EventSignatureVerifier(Protocol):
    def verify(self, event: MarketEvent, raw: Dict[str, Any]) -> Tuple[SignatureStatus, str]:
        ...


class NoSignatureVerifier:
    """默认：供应商不提供签名 → 一律 UNSIGNED，绝不当成已验证。"""

    name = "none"

    def verify(self, event: MarketEvent,
               raw: Dict[str, Any]) -> Tuple[SignatureStatus, str]:
        return SignatureStatus.UNSIGNED, "供应商未提供消息签名（本轮调查结论）"


@dataclass(frozen=True)
class ProviderAuthoritySpec:
    """一个 provider 被允许声明哪些权威能力。"""

    provider: str
    authority: EventAuthority
    exchanges: Tuple[str, ...] = ()
    #: True 表示该 provider 的事件来自交易所原生消息（不是聚合/推断）
    native: bool = False
    #: 只覆盖 Nasdaq 上市证券的停复牌（Massive 的 17/18 指标就是这种）
    nasdaq_listed_only: bool = False
    #: 事件是否有可校验的签名
    signed: bool = False
    notes: str = ""

    def covers(self, exchange: str) -> bool:
        return not self.exchanges or exchange.lower() in {e.lower() for e in self.exchanges}


@dataclass
class AuthorityVerdict:
    accepted: bool
    reason: str
    authority: EventAuthority = EventAuthority.UNKNOWN
    signature: SignatureStatus = SignatureStatus.UNSIGNED

    def as_dict(self) -> Dict[str, Any]:
        return {"accepted": self.accepted, "reason": self.reason,
                "authority": self.authority.value,
                "signature": self.signature.value}


@dataclass
class AuthorityAuditRecord:
    at: str
    provider: str
    symbol: str
    exchange: str
    authority: str
    accepted: bool
    reason: str
    signature: str


class AuthorityRouter:
    """把"这条事件能不能改状态"收敛成一个可测试的判定。"""

    def __init__(self, specs: Iterable[ProviderAuthoritySpec] = (),
                 verifier: Optional[EventSignatureVerifier] = None, *,
                 require_signature_for_resume: bool = True,
                 max_event_age: timedelta = timedelta(minutes=5),
                 clock_skew: timedelta = timedelta(seconds=30)):
        self.specs: Dict[str, ProviderAuthoritySpec] = {s.provider: s for s in specs}
        self.verifier = verifier or NoSignatureVerifier()
        self.require_signature_for_resume = bool(require_signature_for_resume)
        self.max_event_age = max_event_age
        self.clock_skew = clock_skew
        self.audit: List[AuthorityAuditRecord] = []
        self._applied: Dict[Tuple[str, str], int] = {}

    # ------------------------------------------------------------------ #
    def register(self, spec: ProviderAuthoritySpec) -> None:
        self.specs[spec.provider] = spec

    def classify(self, provider: str, raw_type: str,
                 payload: Dict[str, Any]) -> EventAuthority:
        """把供应商原始类型映射为事件类别。**默认 A，绝不自动升格。**"""
        spec = self.specs.get(provider)
        rt = str(raw_type or "").lower()
        if spec is None or not spec.native:
            return EventAuthority.VENDOR_QUOTE
        if rt in ("halt", "trading_action", "security_status", "status"):
            return EventAuthority.EXCHANGE_HALT
        if rt in ("luld", "limit_up_limit_down", "price_band"):
            return EventAuthority.LULD_OFFICIAL
        if rt in ("mwcb", "circuit_breaker", "market_wide_circuit_breaker"):
            return EventAuthority.MWCB_OFFICIAL
        return EventAuthority.VENDOR_QUOTE

    # ------------------------------------------------------------------ #
    def can_apply(self, ev: MarketEvent, *, symbol: str, exchange: str,
                  now: Optional[datetime] = None,
                  version: int = 0) -> AuthorityVerdict:
        """判定一条事件能否改变交易状态。"""
        now = now or datetime.now(timezone.utc)
        spec = self.specs.get(ev.provider)

        if spec is None:
            return self._deny(ev, "PROVIDER_NOT_REGISTERED",
                              "provider 未在权威注册表中")
        if not spec.native:
            return self._deny(ev, "NOT_NATIVE",
                              f"{ev.provider} 的事件不是交易所原生消息")
        if ev.authority is EventAuthority.VENDOR_QUOTE:
            return self._deny(ev, "VENDOR_EVENT_CANNOT_DRIVE_STATE",
                              "普通行情（A 类）不得驱动交易状态")
        if ev.authority is not spec.authority:
            return self._deny(ev, "AUTHORITY_MISMATCH",
                              f"事件类型 {ev.authority.value} 与 provider 声明 "
                              f"{spec.authority.value} 不符")
        if not spec.covers(exchange):
            return self._deny(ev, "EXCHANGE_NOT_COVERED",
                              f"{ev.provider} 未覆盖 {exchange}")
        if ev.exchange.lower() != exchange.lower():
            return self._deny(ev, "EXCHANGE_MISMATCH",
                              f"事件交易所 {ev.exchange} ≠ 期望 {exchange}")
        if ev.symbol.upper() != symbol.upper():
            return self._deny(ev, "SYMBOL_MISMATCH",
                              f"事件代码 {ev.symbol} ≠ 期望 {symbol}")
        if spec.nasdaq_listed_only and exchange.lower() != "nasdaq":
            return self._deny(ev, "NASDAQ_LISTED_ONLY",
                              f"{ev.provider} 的停复牌只覆盖 Nasdaq 上市证券")
        if ev.event_time > now + self.clock_skew:
            return self._deny(ev, "EVENT_IN_FUTURE", "事件时间晚于当前时间")
        if now - ev.event_time > self.max_event_age:
            return self._deny(ev, "EVENT_TOO_OLD", "事件已过期，不得用于恢复")
        if version:
            last = self._applied.get((symbol.upper(), exchange.lower()), 0)
            if version <= last:
                return self._deny(ev, "VERSION_STALE",
                                  f"version {version} ≤ 已应用 {last}")

        status, why = self.verifier.verify(ev, {})
        if status is SignatureStatus.INVALID:
            return self._deny(ev, "SIGNATURE_INVALID", why,
                              signature=status)
        if self.require_signature_for_resume and status is not SignatureStatus.VERIFIED:
            return self._deny(ev, "NO_SIGNATURE",
                              f"未通过签名验证（{status.value}）：{why}",
                              signature=status)

        if version:
            self._applied[(symbol.upper(), exchange.lower())] = version
        self._record(ev, True, "ACCEPTED", status)
        return AuthorityVerdict(True, "", ev.authority, status)

    # ------------------------------------------------------------------ #
    def _deny(self, ev: MarketEvent, code: str, message: str,
              *, signature: SignatureStatus = SignatureStatus.UNSIGNED
              ) -> AuthorityVerdict:
        self._record(ev, False, f"[{code}] {message}", signature)
        return AuthorityVerdict(False, f"[{code}] {message}", ev.authority, signature)

    def _record(self, ev: MarketEvent, ok: bool, reason: str,
                sig: SignatureStatus) -> None:
        self.audit.append(AuthorityAuditRecord(
            at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            provider=ev.provider, symbol=ev.symbol, exchange=ev.exchange,
            authority=ev.authority.value, accepted=ok, reason=reason,
            signature=sig.value))
        if len(self.audit) > 2000:
            self.audit = self.audit[-2000:]

    def snapshot(self) -> Dict[str, Any]:
        return {"providers": {k: {"authority": v.authority.value,
                                  "native": v.native,
                                  "signed": v.signed,
                                  "exchanges": list(v.exchanges)}
                              for k, v in self.specs.items()},
                "require_signature_for_resume": self.require_signature_for_resume,
                "verifier": getattr(self.verifier, "name", "custom"),
                "audit_tail": [a.__dict__ for a in self.audit[-10:]]}


# ---------------------------------------------------------------------- #
# 预置规格：**只声明已核实的事实**，不声明"可能有"的能力
# ---------------------------------------------------------------------- #
def surveyed_specs() -> List[ProviderAuthoritySpec]:
    """本轮调查得到的 provider 权威能力（均为未签名）。"""
    return [
        # Massive 的 LULD 通道：价格带来自 SIP；17/18 指标只覆盖 Nasdaq 上市证券
        ProviderAuthoritySpec(
            provider="massive", authority=EventAuthority.LULD_OFFICIAL,
            exchanges=("nasdaq",), native=False,
            nasdaq_listed_only=True, signed=False,
            notes="LULD 通道含价格带；halt/resume 指标仅 Nasdaq 上市；无签名"),
        # 交易所原生馈送（需自行直连/授权），供应商不自助提供
        ProviderAuthoritySpec(
            provider="nasdaq_itch", authority=EventAuthority.EXCHANGE_HALT,
            exchanges=("nasdaq",), native=True, signed=False,
            notes="Nasdaq TotalView Trading Action 消息；需交易所授权"),
        ProviderAuthoritySpec(
            provider="nyse_pillar", authority=EventAuthority.EXCHANGE_HALT,
            exchanges=("nyse", "arca", "amex"), native=True, signed=False,
            notes="NYSE Pillar Security Status 消息；需交易所授权"),
        ProviderAuthoritySpec(
            provider="databento", authority=EventAuthority.EXCHANGE_HALT,
            exchanges=("nasdaq", "nyse", "arca", "amex", "cboe_bzx"),
            native=True, signed=False,
            notes="status schema 来自交易所原始馈送；无消息签名"),
    ]
