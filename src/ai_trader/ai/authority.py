"""AI 决策权威（Decision Authority）。

为什么需要这一层
----------------
审计 P1-9 指出：配置了 LLM 但缺 Key 时，系统会**静默降级为规则引擎并继续下单**。
但本系统的定位是「单 AI 自动交易」—— 规则引擎不能代替 AI 做开仓决策。

本模块把「谁有权决定开仓」这件事从"约定"变成"结构"：

* 只有 ``model.can_trade=True``（即真正的大模型）才可能拿到开仓许可；
* AI 超时 / 断线 / 返回非法内容 / API 错误 → 立刻进入 ``degraded`` 冷却期，
  冷却期内**禁止一切新开仓**；
* 信号必须"新鲜"（``signal_ttl_seconds``），防止 AI 恢复后把失效期间的旧信号拿来执行；
* 每次拒绝都返回**具体的错误码 + 中文原因**，供审计落库。

明确不做的事
------------
* **不影响已有持仓的保护**：止损/止盈/风控退出由 ``RiskController.scan_exits`` 驱动，
  完全不经过本模块。AI 挂掉时，持仓照样会被止损。
* **不做信号排队**：引擎是同步的"分析→执行"，从不缓存信号。配合新鲜度校验，
  结构上就不可能出现"恢复后补发旧单"。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

# ---- 拒绝码（用于审计检索与告警） ----
CODE_NOT_AUTHORITY = "AI_NOT_DECISION_AUTHORITY"   # 规则引擎/DisabledModel，无开仓权
CODE_UNAVAILABLE = "AI_UNAVAILABLE"                # 模型不可用（无 Key / 构建失败）
CODE_DEGRADED = "AI_DEGRADED"                      # 冷却期内
CODE_STALE = "STALE_SIGNAL"                        # 信号过期
CODE_DISABLED_BY_CONFIG = "AI_ENTRY_DISABLED_BY_CONFIG"


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """统一时区：naive 视为本地时间，避免 offset-naive 与 offset-aware 相减报错。"""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.astimezone()


def _now_iso(now: Optional[datetime] = None) -> str:
    return (_aware(now) or datetime.now().astimezone()).isoformat(timespec="seconds")


@dataclass
class AuthorityDecision:
    allowed: bool
    code: str = ""
    reason: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"allowed": self.allowed, "code": self.code, "reason": self.reason}


@dataclass
class AuthorityState:
    """对外展示的权威状态（Web / status 用）。"""

    model_name: str = ""
    model_id: str = ""
    can_trade: bool = False
    require_ai_for_entry: bool = True
    available: bool = True
    degraded: bool = False
    degraded_until: str = ""
    consecutive_failures: int = 0
    last_success_at: str = ""
    last_failure_at: str = ""
    last_code: str = ""
    last_reason: str = ""
    signal_ttl_seconds: int = 300
    cooldown_seconds: int = 120

    def as_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


class DecisionAuthority:
    """判断「现在能不能让 AI 决定一笔新开仓」。"""

    CODE_SANDBOX = "RULE_ENGINE_SANDBOX"

    def __init__(self, cfg, model, allow_rule_engine_entry: bool = False):
        self.cfg = cfg
        self.model = model
        #: 仅供 demo / 回测沙盒显式开启；生产装配永远为 False。
        self.allow_rule_engine_entry = bool(allow_rule_engine_entry)
        ai = (cfg.ai or {}) if hasattr(cfg, "ai") else {}
        self.require_ai_for_entry = bool(ai.get("require_ai_for_entry", True))
        self.signal_ttl = int(ai.get("signal_ttl_seconds", 300) or 0)
        self.cooldown = int(ai.get("failure_cooldown_seconds", 120) or 0)
        self.max_failures = int(ai.get("max_consecutive_failures", 3) or 1)
        self._failures = 0
        self._degraded_until: Optional[datetime] = None
        self._last_success: Optional[datetime] = None
        self._last_failure: Optional[datetime] = None
        self._last_code = ""
        self._last_reason = ""
        self._ever_succeeded = False

    # ------------------------------------------------------------------ #
    # 模型属性
    # ------------------------------------------------------------------ #
    @property
    def can_trade(self) -> bool:
        return bool(getattr(self.model, "can_trade", True))

    @property
    def model_available(self) -> bool:
        return bool(getattr(self.model, "available", True))

    # ------------------------------------------------------------------ #
    # 资格判定
    # ------------------------------------------------------------------ #
    def can_decide_entry(self, now: Optional[datetime] = None) -> AuthorityDecision:
        """是否允许本模型产生**新的开仓**信号。"""
        now = _aware(now) or datetime.now().astimezone()

        if not self.require_ai_for_entry:
            return AuthorityDecision(
                True, CODE_DISABLED_BY_CONFIG,
                "配置 require_ai_for_entry=false：已显式允许非 AI 决策开仓（不推荐）")

        if self.allow_rule_engine_entry and not self.can_trade:
            return AuthorityDecision(
                True, self.CODE_SANDBOX,
                "沙盒模式：显式允许规则引擎开仓（仅 demo/回测，生产装配不会开启）")

        # 顺序有讲究：先判"能不能用"，再判"有没有决策权"。
        # 这样 UnavailableModel（LLM 但缺 Key）报 AI_UNAVAILABLE，
        # 而规则引擎报 AI_NOT_DECISION_AUTHORITY，两者的运维处置不同。
        if not self.model_available:
            return AuthorityDecision(
                False, CODE_UNAVAILABLE,
                f"AI 模型不可用：{self._last_reason or '未就绪'}")

        if not self.can_trade:
            return AuthorityDecision(
                False, CODE_NOT_AUTHORITY,
                f"模型 {getattr(self.model, 'name', '?')}/"
                f"{getattr(self.model, 'model_id', '?')} 不具备开仓决策权"
                f"（规则引擎只能提供指标与辅助判断，不能代替 AI）")

        if self._degraded_until and now < self._degraded_until:
            left = int((self._degraded_until - now).total_seconds())
            return AuthorityDecision(
                False, CODE_DEGRADED,
                f"AI 处于故障冷却期（剩余 {left}s，最后错误 {self._last_code}："
                f"{self._last_reason}）")

        return AuthorityDecision(True, "", "AI 可决策")

    # ------------------------------------------------------------------ #
    # 成败记录（驱动熔断）
    # ------------------------------------------------------------------ #
    def record_success(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        now = _aware(now) or datetime.now().astimezone()
        recovered = self._failures > 0 or self._degraded_until is not None
        self._failures = 0
        self._degraded_until = None
        self._last_success = now
        self._last_code = ""
        self._last_reason = ""
        self._ever_succeeded = True
        return {"recovered": recovered, "at": _now_iso(now)}

    def record_failure(self, code: str, detail: str,
                       now: Optional[datetime] = None) -> Dict[str, Any]:
        now = _aware(now) or datetime.now().astimezone()
        self._failures += 1
        self._last_failure = now
        self._last_code = str(code or "AI_ERROR")
        self._last_reason = str(detail or "")[:300]
        entered_degraded = False
        if self._failures >= self.max_failures and self.cooldown > 0:
            self._degraded_until = now + timedelta(seconds=self.cooldown)
            entered_degraded = True
        return {
            "failures": self._failures,
            "degraded": entered_degraded,
            "degraded_until": _now_iso(self._degraded_until) if self._degraded_until else "",
            "code": self._last_code,
        }

    # ------------------------------------------------------------------ #
    # 信号新鲜度
    # ------------------------------------------------------------------ #
    @staticmethod
    def parse_ts(ts: str) -> Optional[datetime]:
        if not ts:
            return None
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.astimezone()
        return dt

    def check_freshness(self, signal_ts: str,
                        now: Optional[datetime] = None) -> AuthorityDecision:
        """信号必须在 TTL 内。防止"AI 恢复后执行失效期间的旧信号"。"""
        if self.signal_ttl <= 0:
            return AuthorityDecision(True, "", "未启用信号有效期检查")
        now = _aware(now) or datetime.now().astimezone()
        ts = self.parse_ts(signal_ts)
        ts = _aware(ts)
        if ts is None:
            return AuthorityDecision(False, CODE_STALE, f"信号时间戳无法解析: {signal_ts!r}")
        age = (now - ts).total_seconds()
        if age > self.signal_ttl:
            return AuthorityDecision(
                False, CODE_STALE,
                f"信号已过期（生成于 {signal_ts}，距今 {age:.0f}s > 上限 {self.signal_ttl}s）")
        return AuthorityDecision(True, "", f"信号新鲜（{age:.0f}s）")

    # ------------------------------------------------------------------ #
    def snapshot(self, now: Optional[datetime] = None) -> AuthorityState:
        now = _aware(now) or datetime.now().astimezone()
        degraded = bool(self._degraded_until and now < self._degraded_until)
        return AuthorityState(
            model_name=str(getattr(self.model, "name", "")),
            model_id=str(getattr(self.model, "model_id", "")),
            can_trade=self.can_trade,
            require_ai_for_entry=self.require_ai_for_entry,
            available=self.model_available,
            degraded=degraded,
            degraded_until=_now_iso(self._degraded_until) if degraded else "",
            consecutive_failures=self._failures,
            last_success_at=_now_iso(self._last_success) if self._last_success else "",
            last_failure_at=_now_iso(self._last_failure) if self._last_failure else "",
            last_code=self._last_code,
            last_reason=self._last_reason,
            signal_ttl_seconds=self.signal_ttl,
            cooldown_seconds=self.cooldown,
        )
