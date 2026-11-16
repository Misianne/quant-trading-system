"""LULD 恢复事件的身份验证层（Authority）。

为什么必须单独一层
------------------
上一版 `on_official_resume(ts, version, source="exchange")` 只要**传一个字符串**
就能把标的重置为可交易。这是**权限漏洞**：任何拿到 lifecycle 对象的代码都能"宣布"恢复。

本模块把「谁是权威、能对谁、说什么」变成显式校验：

    恢复事件 ──> ResumeAuthority.verify() ──> 通过 → LuldLifecycle 才允许 REOPENED
                                        └→ 拒绝 → 保持暂停 + 记录原因

校验项（缺一不可）
------------------
1. provider 身份（必须在注册表里，且 `can_authorize_resume=True`）
2. provider 是否有权对该 exchange 签发
3. **测试身份不得出现在生产模式**（反之亦然）
4. symbol / exchange / trading_date 三者完全匹配
5. `pause_event_id`、`auction_id` 必须与当前暂停/竞价匹配
6. `status` 必须是 `"resumed"`
7. `event_version` 必须**大于**已应用的恢复版本（旧事件不覆盖新状态）
8. 时间戳不得来自未来（允许时钟偏差）也不得过于陈旧

没有真实认证机制时的边界
------------------------
本模块**不声称**做了密码学签名验证。它做的是**身份与绑定校验**：
provider 必须显式注册、必须声明可授权的交易所，且测试与生产身份物理隔离。
真实数据源接入时需要在此基础上补签名/证书校验（标注 UNVERIFIED）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional

from .luld import normalize_symbol


class ProviderKind(str, Enum):
    EXCHANGE_FEED = "exchange_feed"      # 真实交易所/官方证券信息处理器
    TEST = "test"                         # Replay / Paper 测试适配器
    UNKNOWN = "unknown"


#: 状态串必须是这个值才算"恢复"
RESUME_STATUS = "resumed"


class ResumeRejected(RuntimeError):
    """恢复事件被拒。``code`` 是可审计的拒绝原因。"""

    def __init__(self, code: str, message: str):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ProviderSpec:
    provider_id: str
    kind: ProviderKind
    exchanges: frozenset
    can_authorize_resume: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {"provider_id": self.provider_id, "kind": self.kind.value,
                "exchanges": sorted(self.exchanges),
                "can_authorize_resume": self.can_authorize_resume}


@dataclass(frozen=True)
class ResumeEvent:
    """一条"官方恢复"声明。所有字段都必须可追溯。"""

    provider: str
    exchange: str
    symbol: str
    trading_date: str                 # ISO 日期
    pause_event_id: str
    auction_id: str
    event_version: int
    event_ts: datetime
    status: str = RESUME_STATUS
    note: str = ""

    def __post_init__(self) -> None:
        if self.event_ts.tzinfo is None:
            raise ValueError("ResumeEvent.event_ts 必须带时区")

    def as_dict(self) -> Dict[str, Any]:
        return {"provider": self.provider, "exchange": self.exchange,
                "symbol": self.symbol, "trading_date": self.trading_date,
                "pause_event_id": self.pause_event_id,
                "auction_id": self.auction_id,
                "event_version": self.event_version,
                "event_ts": self.event_ts.isoformat(),
                "status": self.status, "note": self.note}


@dataclass
class AuthorityAudit:
    ts: datetime
    provider: str
    symbol: str
    exchange: str
    accepted: bool
    code: str = ""
    message: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"ts": self.ts.isoformat(), "provider": self.provider,
                "symbol": self.symbol, "exchange": self.exchange,
                "accepted": self.accepted, "code": self.code,
                "message": self.message}


class ResumeAuthority:
    """恢复事件的准入控制。

    ``mode`` 决定是否允许测试身份：
    * ``"production"``：只接受 ``EXCHANGE_FEED`` 类型的 provider
    * ``"test"``：额外接受 ``TEST`` 类型的 provider（Replay/Paper 专用）
    """

    def __init__(self, providers: Iterable[ProviderSpec], *,
                 mode: str = "production",
                 clock_skew: timedelta = timedelta(minutes=2),
                 max_event_age: timedelta = timedelta(hours=2)):
        if mode not in ("production", "test"):
            raise ValueError(f"未知模式: {mode}")
        self.mode = mode
        self.providers: Dict[str, ProviderSpec] = {p.provider_id: p for p in providers}
        self.clock_skew = clock_skew
        self.max_event_age = max_event_age
        self.audit: List[AuthorityAudit] = []
        #: (symbol, exchange) → 最后应用的恢复事件版本
        self._last_resume_version: Dict[tuple, int] = {}

    # ------------------------------------------------------------------ #
    def _record(self, ev: ResumeEvent, accepted: bool, code: str = "",
                message: str = "", now: Optional[datetime] = None) -> None:
        self.audit.append(AuthorityAudit(
            ts=now or datetime.now(timezone.utc), provider=ev.provider,
            symbol=normalize_symbol(ev.symbol), exchange=ev.exchange,
            accepted=accepted, code=code, message=message))

    def _deny(self, ev: ResumeEvent, code: str, message: str,
              now: Optional[datetime] = None) -> None:
        self._record(ev, False, code, message, now)
        raise ResumeRejected(code, message)

    def applied_version(self, symbol: str, exchange: str) -> int:
        return self._last_resume_version.get(
            (normalize_symbol(symbol), exchange.lower()), 0)

    # ------------------------------------------------------------------ #
    def verify(self, ev: ResumeEvent, *, symbol: str, exchange: str,
               trading_date: str, pause_event_id: str,
               auction_id: str, now: Optional[datetime] = None) -> None:
        """校验一条恢复事件；通过返回 None，否则抛 :class:`ResumeRejected`。"""
        now = now or datetime.now(timezone.utc)

        # 1) provider 身份
        spec = self.providers.get(ev.provider)
        if spec is None:
            self._deny(ev, "PROVIDER_UNKNOWN", f"未知 provider: {ev.provider}", now)
        if not spec.can_authorize_resume:
            self._deny(ev, "PROVIDER_NOT_AUTHORIZED",
                       f"{ev.provider} 无权签发恢复事件", now)
        # 2) 测试/生产身份隔离
        if spec.kind is ProviderKind.TEST and self.mode != "test":
            self._deny(ev, "TEST_PROVIDER_IN_PRODUCTION",
                       f"{ev.provider} 是测试身份，不得在生产模式签发恢复", now)
        if spec.kind is ProviderKind.UNKNOWN:
            self._deny(ev, "PROVIDER_KIND_UNKNOWN", f"{ev.provider} 类型未知", now)
        # 3) provider ↔ exchange 授权
        ex = str(exchange).lower()
        if ex not in {e.lower() for e in spec.exchanges}:
            self._deny(ev, "PROVIDER_EXCHANGE_MISMATCH",
                       f"{ev.provider} 未授权 {exchange} 交易所", now)
        if str(ev.exchange).lower() != ex:
            self._deny(ev, "EXCHANGE_MISMATCH",
                       f"事件交易所 {ev.exchange} ≠ 期望 {exchange}", now)
        # 4) symbol / trading_date 绑定
        if normalize_symbol(ev.symbol) != normalize_symbol(symbol):
            self._deny(ev, "SYMBOL_MISMATCH",
                       f"事件证券 {ev.symbol} ≠ 期望 {symbol}", now)
        if str(ev.trading_date) != str(trading_date):
            self._deny(ev, "TRADING_DATE_MISMATCH",
                       f"事件交易日 {ev.trading_date} ≠ {trading_date}", now)
        # 5) 与当前暂停/竞价绑定
        if ev.pause_event_id != pause_event_id:
            self._deny(ev, "PAUSE_ID_MISMATCH",
                       f"pause_event_id {ev.pause_event_id} ≠ {pause_event_id}", now)
        if auction_id and ev.auction_id != auction_id:
            self._deny(ev, "AUCTION_ID_MISMATCH",
                       f"auction_id {ev.auction_id} ≠ {auction_id}", now)
        # 6) 状态语义
        if str(ev.status).lower() != RESUME_STATUS:
            self._deny(ev, "STATUS_INVALID",
                       f"status={ev.status!r}，必须是 {RESUME_STATUS!r}", now)
        # 7) 版本单调
        last = self._last_resume_version.get((normalize_symbol(symbol), ex), 0)
        if ev.event_version <= last:
            self._deny(ev, "VERSION_STALE",
                       f"event_version {ev.event_version} ≤ 已应用 {last}", now)
        # 8) 时间合理性
        if ev.event_ts > now + self.clock_skew:
            self._deny(ev, "TIMESTAMP_FUTURE",
                       f"事件时间 {ev.event_ts.isoformat()} 晚于当前可信时间", now)
        if now - ev.event_ts > self.max_event_age:
            self._deny(ev, "TIMESTAMP_STALE",
                       f"事件过旧：{ev.event_ts.isoformat()}", now)

        self._record(ev, True, "ACCEPTED", "", now)
        self._last_resume_version[(normalize_symbol(symbol), ex)] = ev.event_version

    def as_dict(self) -> Dict[str, Any]:
        return {"mode": self.mode,
                "providers": {k: v.as_dict() for k, v in self.providers.items()},
                "audit_tail": [a.as_dict() for a in self.audit[-10:]]}


# ====================================================================== #
# 预置身份（**不含任何真实凭据**，只是身份与权限声明）
# ====================================================================== #
def production_authority() -> ResumeAuthority:
    """生产模式：只认声明为 EXCHANGE_FEED 且显式授权的 provider。

    注意：这里**没有**接入真实交易所身份。真实接入时必须补签名/证书校验，
    当前状态标注 **UNVERIFIED**。
    """
    return ResumeAuthority([
        # 真实交易所 provider 需要在实际接入时注册；此处保持为空，
        # 意味着生产模式下**任何**恢复事件都会被拒 —— 这是刻意的 fail-closed。
    ], mode="production")


def test_authority(*, exchanges: Iterable[str] = ("nasdaq", "nyse"),
                   mode: str = "test") -> ResumeAuthority:
    """Replay/Paper 专用：显式测试身份，名字带 ``test.`` 前缀。"""
    ex = frozenset(e.lower() for e in exchanges)
    return ResumeAuthority([
        ProviderSpec(provider_id="test.exchange-sim", kind=ProviderKind.TEST,
                     exchanges=ex, can_authorize_resume=True),
        ProviderSpec(provider_id="test.provider-feed", kind=ProviderKind.TEST,
                     exchanges=ex, can_authorize_resume=True),
        ProviderSpec(provider_id="test.readonly", kind=ProviderKind.TEST,
                     exchanges=ex, can_authorize_resume=False),
    ], mode=mode)


def make_resume_event(lifecycle: Any, *, provider: str = "test.exchange-sim",
                      event_version: Optional[int] = None,
                      ts: Optional[datetime] = None,
                      status: str = RESUME_STATUS,
                      exchange: Optional[str] = None,
                      symbol: Optional[str] = None,
                      trading_date: Optional[str] = None,
                      pause_event_id: Optional[str] = None,
                      auction_id: Optional[str] = None) -> ResumeEvent:
    """按 lifecycle 当前状态生成一条恢复事件（默认字段全部取自实际状态）。

    这正是攻击测试的入口：把任意一个字段改错，就应该被拒。
    """
    pause = lifecycle.pause
    auction = lifecycle.auction
    return ResumeEvent(
        provider=provider,
        exchange=exchange or (lifecycle.exchange.value if lifecycle.exchange else "unknown"),
        symbol=symbol or lifecycle.symbol,
        trading_date=trading_date or (getattr(pause, "trading_date", "") if pause else ""),
        pause_event_id=pause_event_id if pause_event_id is not None
        else (getattr(pause, "pause_event_id", "") if pause else ""),
        auction_id=auction_id if auction_id is not None else (auction.auction_id or ""),
        event_version=event_version if event_version is not None else (lifecycle.version + 1),
        event_ts=ts or datetime.now(timezone.utc),
        status=status,
    )
