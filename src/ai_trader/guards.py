"""交易配置守卫（US-P1 配置守卫）。

为什么需要
----------
`market_rules` 有三个"总闸"：

* ``enabled: false``   → 整个规则层不生效
* ``enforce: false``   → 同上
* ``enforce_markets`` 不含目标市场 → 该市场走 ``applied=False``（"未建模"）

任何一个被改错，**代码路径完全不变，测试也全绿**，但美股会瞬间失去
价格精度 / 数量 / 时段 / 日历校验。这类"配置级旁路"必须在**启动时**拦住，
而不是等出事以后再从审计里翻。

设计原则
--------
1. **只读**：本模块不修改任何配置，只判定"这份配置能不能用来交易"。
2. **可解释**：每条违规都带 ``code`` / ``field`` / ``detail``，便于运维定位。
3. **默认最严**：目标市场默认 ``("us",)``；找不到配置 = 违规，不是放行。
4. **与市场无关的地方不检查**：不给 A 股/港股加新约束（历史行为保留）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .market.rules import (
    STATUS_UNVERIFIED,
    STATUS_VERIFIED,
    MarketRuleSet,
)

# ---- 违规码 ----
G_RULES_DISABLED = "GUARD_RULES_DISABLED"
G_RULES_NOT_ENFORCED = "GUARD_RULES_NOT_ENFORCED"
G_MARKET_NOT_ENFORCED = "GUARD_MARKET_NOT_ENFORCED"
G_POLICY_NOT_REJECT = "GUARD_UNENFORCED_POLICY_NOT_REJECT"
G_SESSION_MISSING = "GUARD_SESSION_MISSING"
G_CLOCK_UNAVAILABLE = "GUARD_CLOCK_UNAVAILABLE"
G_BOARD_MISSING = "GUARD_BOARD_MISSING"
G_BOARD_NOT_VERIFIED = "GUARD_BOARD_NOT_VERIFIED"
G_CALENDAR_MISSING = "GUARD_CALENDAR_MISSING"
G_CALENDAR_NOT_VERIFIED = "GUARD_CALENDAR_NOT_VERIFIED"
G_CALENDAR_EXPIRED = "GUARD_CALENDAR_EXPIRED"
G_POSITION_SESSIONS = "GUARD_POSITION_SESSIONS_INCOMPLETE"
G_TICK_MISSING = "GUARD_TICK_SIZE_MISSING"
G_SAFETY_CONFLICT = "GUARD_SAFETY_CONFLICT"

#: 默认必须强制校验的市场（本项目当前只交易美股）
DEFAULT_REQUIRED_MARKETS: Tuple[str, ...] = ("us",)


@dataclass(frozen=True)
class GuardViolation:
    code: str
    field: str
    detail: str

    def as_dict(self) -> Dict[str, str]:
        return {"code": self.code, "field": self.field, "detail": self.detail}

    def __str__(self) -> str:  # pragma: no cover - 仅用于日志
        return f"[{self.code}] {self.field}: {self.detail}"


class ConfigGuardError(RuntimeError):
    """配置不满足交易前置条件。调用方必须**拒绝启动交易流程**。"""

    def __init__(self, violations: Sequence[GuardViolation]):
        self.violations = list(violations)
        super().__init__(
            "交易配置守卫未通过：" +
            "；".join(f"[{v.code}] {v.field}（{v.detail}）" for v in self.violations)
        )

    def as_dict(self) -> Dict[str, Any]:
        return {"ok": False, "count": len(self.violations),
                "violations": [v.as_dict() for v in self.violations]}


def _required_markets(raw: Dict[str, Any],
                      override: Optional[Iterable[str]]) -> Tuple[str, ...]:
    if override is not None:
        return tuple(str(m).lower() for m in override)
    mr = raw.get("market_rules") or {}
    configured = mr.get("required_markets")
    if isinstance(configured, (list, tuple)) and configured:
        return tuple(str(m).lower() for m in configured)
    return DEFAULT_REQUIRED_MARKETS


def check_trading_config(cfg: Any,
                         required_markets: Optional[Iterable[str]] = None,
                         today: Optional[date] = None) -> List[GuardViolation]:
    """检查一份 ``AppConfig`` 能否安全用于交易。返回违规列表（空 = 通过）。"""
    raw = getattr(cfg, "raw", {}) or {}
    mr = raw.get("market_rules") or {}
    out: List[GuardViolation] = []
    required = _required_markets(raw, required_markets)

    # ---- 1. 总闸 ----
    if not bool(mr.get("enabled", True)):
        out.append(GuardViolation(
            G_RULES_DISABLED, "market_rules.enabled",
            "规则层被显式关闭（enabled=false）—— 美股将失去价格精度/数量/时段/日历校验"))
    if not bool(mr.get("enforce", True)):
        out.append(GuardViolation(
            G_RULES_NOT_ENFORCED, "market_rules.enforce",
            "规则层被显式关闭（enforce=false）—— 同上"))

    # ---- 2. 强制校验市场 ----
    enforce = {str(m).lower() for m in (mr.get("enforce_markets") or [])}
    for mkt in required:
        if mkt not in enforce:
            out.append(GuardViolation(
                G_MARKET_NOT_ENFORCED, "market_rules.enforce_markets",
                f"目标市场 {mkt!r} 不在强制校验列表 {sorted(enforce)} 中"
                f" → 该市场会以 applied=False 通过规则层"))

    # ---- 3. 未建模市场的处置策略 ----
    policy = str(mr.get("unenforced_market_policy") or "").lower()
    if policy != "reject":
        out.append(GuardViolation(
            G_POLICY_NOT_REJECT, "market_rules.unenforced_market_policy",
            f"当前为 {policy or '(未设置)'!r}，应为 'reject'"
            f"（否则未建模市场会被放行）"))

    # ---- 4. 时段 / 时钟 / 板块 / 日历（用真实规则集，不重复实现） ----
    try:
        rs = MarketRuleSet(mr)
    except Exception as exc:  # noqa: BLE001
        out.append(GuardViolation(
            G_CLOCK_UNAVAILABLE, "market_rules",
            f"规则集无法构建：{type(exc).__name__}: {exc}"))
        return out

    configured_sessions = mr.get("sessions") or {}
    for mkt in required:
        # 必须**显式**配置时段。`MarketRuleSet` 在缺省时会回落到代码里的
        # DEFAULT_SESSIONS —— 那份默认值本身是对的，但"靠代码兜底"意味着
        # 配置漂移无法被发现，所以启动守卫要求配置里写清楚。
        if not isinstance(configured_sessions.get(mkt), (dict, list)):
            out.append(GuardViolation(
                G_SESSION_MISSING, f"market_rules.sessions.{mkt}",
                f"未显式配置 {mkt!r} 的交易时段（当前依赖代码内置默认值）"))
        clock = rs.clocks.get(mkt)
        if clock is None:
            out.append(GuardViolation(
                G_CLOCK_UNAVAILABLE, f"market_rules.sessions.{mkt}",
                f"未配置或无法加载该市场的交易所时钟"
                f"（clock_errors={rs.clock_errors.get(mkt, '无')}）"))
        elif not clock.configured:
            out.append(GuardViolation(
                G_SESSION_MISSING, f"market_rules.sessions.{mkt}.regular",
                "常规交易时段未配置（会导致该市场恒定拒绝或口径不明）"))

        boards = [b for b in rs.boards.values() if b.market == mkt]
        if not boards:
            out.append(GuardViolation(
                G_BOARD_MISSING, f"market_rules.boards.<{mkt}>",
                f"没有为市场 {mkt!r} 定义任何板块规则 —— 所有标的都会 RULE_UNKNOWN"))
        for b in boards:
            if b.meta.status == STATUS_UNVERIFIED and not rs.allow_unverified:
                # UNVERIFIED + allow_unverified_rules=false 会恒定拒绝，属"不可交易"
                out.append(GuardViolation(
                    G_BOARD_NOT_VERIFIED, f"market_rules.boards.{b.board}.meta.status",
                    "规则状态为 UNVERIFIED 且 allow_unverified_rules=false，"
                    "该板块无法交易"))
            if b.tick_size is None:
                out.append(GuardViolation(
                    G_TICK_MISSING, f"market_rules.boards.{b.board}.tick_size",
                    "未配置最小报价单位（tick_size），价格精度校验不会生效"))

        entry = rs.calendar.entries.get(mkt)
        if entry is None:
            out.append(GuardViolation(
                G_CALENDAR_MISSING, f"market_rules.calendar.markets.{mkt}",
                f"未配置该市场的交易日历 —— strict 模式下会被恒定拒绝"))
        else:
            if entry.status != STATUS_VERIFIED:
                out.append(GuardViolation(
                    G_CALENDAR_NOT_VERIFIED, f"market_rules.calendar.markets.{mkt}.status",
                    f"日历状态为 {entry.status}，应录入官方休市日并标 VERIFIED"))
            if entry.covered_until:
                try:
                    until = date.fromisoformat(entry.covered_until)
                except ValueError:
                    out.append(GuardViolation(
                        G_CALENDAR_EXPIRED, f"market_rules.calendar.markets.{mkt}.covered_until",
                        f"覆盖截止日无法解析：{entry.covered_until!r}"))
                else:
                    if (today or date.today()) > until:
                        out.append(GuardViolation(
                            G_CALENDAR_EXPIRED,
                            f"market_rules.calendar.markets.{mkt}.covered_until",
                            f"日历只覆盖到 {entry.covered_until}，已过期 → 必须录入新一年休市日"))

    # ---- 5. 持仓时段开关（风险可见性） ----
    ps = (raw.get("fosun") or {}).get("position_sessions") or {}
    for mkt in required:
        if mkt != "us":
            continue
        for key in ("use_us_pre", "use_us_post", "use_us_night"):
            if ps.get(key) is False:
                out.append(GuardViolation(
                    G_POSITION_SESSIONS, f"fosun.position_sessions.{key}",
                    "被显式关闭 → 该时段的持仓不会返回，将脱离风控"))

    # ---- 6. 安全开关自洽 ----
    s = raw.get("safety") or {}
    if bool(s.get("paper_trading", True)) and bool(s.get("live_trading", False)):
        out.append(GuardViolation(
            G_SAFETY_CONFLICT, "safety.paper_trading",
            "paper_trading 与 live_trading 同时为真"))
    return out


def assert_trading_config(cfg: Any,
                          required_markets: Optional[Iterable[str]] = None,
                          today: Optional[date] = None) -> None:
    """不通过则抛 :class:`ConfigGuardError`（worker/CLI 启动时调用）。"""
    violations = check_trading_config(cfg, required_markets=required_markets, today=today)
    if violations:
        raise ConfigGuardError(violations)


def guard_summary(cfg: Any,
                  required_markets: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """给 ``check-env`` 用的结构化摘要（不抛异常）。"""
    required = _required_markets(getattr(cfg, "raw", {}) or {}, required_markets)
    violations = check_trading_config(cfg, required_markets=required_markets)
    return {
        "ok": not violations,
        "required_markets": list(required),
        "count": len(violations),
        "violations": [v.as_dict() for v in violations],
        "note": "worker/scan/monitor 启动前必须通过；不通过则拒绝启动交易流程",
    }
