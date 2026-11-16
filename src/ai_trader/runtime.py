"""运行时装配：把配置、密钥、券商、模型、风控、审计拼成一个可用的系统对象。

所有入口（Web、CLI 脚本、测试）都走 :func:`build_runtime`，保证初始化路径唯一。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from .ai.base import AIModel, UnavailableModel
from .ai.providers import build_model
from .audit.journal import AuditJournal
from .broker.base import Broker
from .broker.fosun_adapter import FosunBroker
from .broker.paper import PaperBroker
from .market.feed import FeedConfig, MarketFeed
from .config import AppConfig, PROJECT_ROOT, load_config
from .log_redaction import (ensure_hooks, install_format_guard,
                            install_global_hook, install_logger_hook,
                            install_on_handlers, redaction_health)
from .risk.controller import RiskController
from .secrets import SecretVault, default_vault
from .strategy.engine import StrategyEngine

logger = logging.getLogger("ai_trader.runtime")


@dataclass
class Runtime:
    cfg: AppConfig
    vault: SecretVault
    journal: AuditJournal
    broker: Broker
    model: AIModel
    risk: RiskController
    engine: StrategyEngine
    broker_kind: str
    ai_fallback_reason: str = ""

    def status(self) -> Dict[str, Any]:
        gate = self.cfg.gate.snapshot()
        b_health = self.broker.health()
        a_health = self.model.health()
        chain = self.journal.verify_chain()
        return {
            "version": __import__("ai_trader").__version__,
            "mode": self.cfg.mode,
            "safety": gate,
            "broker": {"kind": self.broker_kind, **b_health},
            "ai": a_health,
            "ai_fallback_reason": self.ai_fallback_reason,
            "authority": self.engine.authority.snapshot().as_dict(),
            "market_rules": self.risk.rules.describe(),
            "feed": self.engine.feed_snapshot(),
            "risk": self.risk.describe_limits(),
            "audit": {"counts": self.journal.counts(), "chain": chain,
                      "jsonl": str(self.cfg.get("audit.jsonl_path", ""))},
            "log_redaction": redaction_health(),
            "vault": self.vault.status().as_dict(),
            "engine": {
                "scans": self.engine._scan_count,
                "active_stops": len(self.engine.stops),
            },
        }


def build_journal(cfg: AppConfig) -> AuditJournal:
    db = PROJECT_ROOT / str(cfg.get("audit.db_path", "data/audit.db"))
    jsonl = PROJECT_ROOT / str(cfg.get("audit.jsonl_path", "logs/audit.jsonl"))
    return AuditJournal(db, jsonl, hash_chain=bool(cfg.get("audit.hash_chain", True)))


def build_broker(cfg: AppConfig, vault: SecretVault) -> tuple[Broker, str]:
    """按安全开关选择券商。

    ``PAPER_TRADING=true`` → 永远走模拟撮合，即使配了真实凭证也不碰真账。
    """
    if cfg.gate.state.paper_trading:
        p = cfg.paper
        cash_cfg = p.get("initial_cash") or {}
        broker = PaperBroker(
            state_path=cfg.data_dir / "paper_state.json",
            initial_cash={k: float(v) for k, v in cash_cfg.items()} or None,
            commission_bps=float(p.get("commission_bps", 8.0)),
            min_commission=float(p.get("min_commission", 3.0)),
            slippage_bps=float(p.get("slippage_bps", 5.0)),
            allow_partial_fill=bool(p.get("allow_partial_fill", True)),
        )
        return broker, "paper"

    fosun = FosunBroker(cfg, vault)
    return fosun, "fosun_live" if fosun.configured else "fosun_unconfigured"


def build_feed_auth_provider(broker: Any) -> Optional[Any]:
    """从券商适配器构造行情推送的认证提供者。

    P0-2：官方推送要求握手带 ``X-Api-Key`` + ``X-Session``，二者都挂在
    SDK 的 ``auth_manager`` 上，且 Session 会过期。因此这里返回一个**每次
    建连都现取**的闭包，而不是把凭证缓存到 ``FeedConfig`` 里：

    * 不在配置/日志/快照中保存任何认证值；
    * Session 过期时由 ``get_valid_session()`` 负责刷新；
    * 拿不到就抛错，让 ``MarketFeed`` fail-closed（绝不匿名连接）。
    """
    # A-1：provider 创建前必须走**与业务接口相同**的门禁 + 路由校验。
    # 冲突/未配置时直接不创建 provider —— 不会触达 auth_manager，
    # 因此不会发出 SessionCreate 请求。
    ready_fn = getattr(broker, "feed_auth_ready", None)
    if callable(ready_fn):
        ready, reason = ready_fn()
    else:  # 兼容没有该方法的 Broker 实现（如模拟盘）
        ready, reason = bool(getattr(broker, "client", None)), "该券商未实现 feed_auth_ready"
    if not ready:
        logger.error("行情推送认证不可用（%s）：不创建认证 provider，"
                     "也不会发起任何 SessionCreate/HTTP 请求", reason)
        return None

    def _provider() -> Dict[str, str]:
        # 每次建连现取；内部会再次经过 _require()（纵深防御）
        headers_fn = getattr(broker, "feed_auth_headers", None)
        if not callable(headers_fn):
            raise RuntimeError("券商未实现 feed_auth_headers（fail-closed）")
        return headers_fn()

    return _provider


def build_feed(cfg: AppConfig, broker: Any = None):
    """构建实时行情推送（未启用/未配置地址时返回 None）。"""
    raw = cfg.raw.get("market_feed") or {}
    if not raw.get("enabled"):
        return None
    feed_cfg = FeedConfig.from_cfg(raw)
    if not feed_cfg.codes:
        feed_cfg.codes = tuple(str(u["code"]).lower() for u in cfg.universe)
    if not feed_cfg.url:
        logger.warning("market_feed.enabled=true 但未配置 url，推送未启动")
        return None
    provider = build_feed_auth_provider(broker) if broker is not None else None
    if feed_cfg.require_auth and provider is None:
        logger.error("行情推送要求认证，但当前不具备认证条件（门禁未通过 / "
                     "路由冲突 / 凭证缺失 / 无 auth_manager）：推送将以 "
                     "fail-closed 方式启动（不会匿名连接）")
    return MarketFeed(feed_cfg, auth_provider=provider)


def build_ai(cfg: AppConfig) -> tuple[AIModel, str]:
    """构建模型。

    审计 P1-9 修复后的语义：**不再降级为规则引擎去交易**。

    * LLM 可用 → 返回 LLM（``can_trade=True``）
    * LLM 缺 Key / 构建失败 → 返回 :class:`UnavailableModel`（``can_trade=False``，
      ``analyze`` 必抛错）→ 引擎不可能产生开仓信号
    * provider=heuristic → 返回规则引擎但 ``can_trade=False``，只能算指标，
      **不能开仓**（除非配置显式 `require_ai_for_entry=false`）

    两种情况都会把原因写进 ``ai_fallback_reason``，Web 首页与 ``status`` 可见。
    """
    provider = str(cfg.ai.get("provider", "heuristic")).lower()
    try:
        model = build_model(cfg)
    except Exception as exc:  # noqa: BLE001
        reason = f"模型构建失败：{type(exc).__name__}: {exc}"
        logger.error("%s —— 返回不可用模型，禁止开仓", reason)
        return UnavailableModel(reason, provider), reason

    if getattr(model, "is_llm", False) and not getattr(model, "available", False):
        reason = ("LLM 未配置 API Key（未测试）：不降级、不交易；"
                  "只允许已有持仓按风控退出")
        logger.error(reason)
        return UnavailableModel(reason, provider), reason

    if not getattr(model, "can_trade", True):
        reason = (f"provider={provider}：规则引擎仅提供指标与辅助判断，"
                  f"不作为开仓决策者（require_ai_for_entry=true）")
        logger.warning(reason)
        return model, reason

    return model, ""


def build_runtime(cfg: Optional[AppConfig] = None, vault: Optional[SecretVault] = None,
                  config_path: Optional[Path] = None) -> Runtime:
    cfg = cfg or load_config(config_path)
    vault = vault or default_vault()
    journal = build_journal(cfg)
    broker, kind = build_broker(cfg, vault)
    model, fallback_reason = build_ai(cfg)
    risk = RiskController(cfg, cfg.gate)
    feed = build_feed(cfg, broker)
    engine = StrategyEngine(cfg, broker, model, risk, journal,
                            stops_path=cfg.data_dir / "stops.json",
                            allow_rule_engine_entry=False,
                            feed=feed)
    journal.log("runtime_start", {
        "mode": cfg.mode, "broker": kind, "model": f"{model.name}/{model.model_id}",
        "ai_fallback": fallback_reason, "safety": cfg.gate.snapshot(),
    })
    return Runtime(cfg=cfg, vault=vault, journal=journal, broker=broker, model=model,
                   risk=risk, engine=engine, broker_kind=kind,
                   ai_fallback_reason=fallback_reason)


def setup_logging(level: str = "INFO") -> None:
    """配置根日志并安装统一脱敏（F-2 / Batch 4.1.1）。

    脱敏必须在**日志配置完成后**安装，且分多层落点：

    * ``install_on_handlers``：给已存在的 handler 挂 filter（覆盖存量）；
    * ``install_global_hook``：``Handler.handle`` —— 覆盖之后创建的 handler；
    * ``install_logger_hook``：``Logger.handle`` —— 覆盖重写 handle 的自定义
      handler（记录在分发前就已脱敏）；
    * ``install_format_guard``：``Handler.format`` —— 最终文本兜底，覆盖
      自己重新渲染内容的 Formatter。

    重复调用是幂等的；被第三方覆盖的钩子会由 ``ensure_hooks()`` 自动修复，
    修复结果与"被禁用/包装的 handler 数量"可从 :func:`redaction_health` 读到。
    不改变日志级别与输出格式。
    """
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    install_on_handlers()
    install_global_hook()
    install_logger_hook()
    install_format_guard()
    health = ensure_hooks()
    blocked = int(health.get("blocked_handlers", 0) or 0)
    if blocked:
        logger.error("日志脱敏不可用：已禁用 %d 个 handler（拒绝明文输出，"
                     "详见 redaction_health）", blocked)
    # Batch 4.1.2 / AUD2-F2-01、AUD2-F2-02：健康位必须如实反映
    # "是否真的能拒绝输出"与"钩子是否仍是可信包装"，不能只看函数没抛异常。
    if not health.get("healthy", True):
        logger.error("日志脱敏健康检查未通过：unsecured_handlers=%d, "
                     "hooks(handler/logger/format)=%s/%s/%s, foreign_hooks=%s"
                     "（详见 redaction_health，禁止当作已受保护）",
                     int(health.get("unsecured_handlers", 0) or 0),
                     bool(health.get("handler_hook_trusted")),
                     bool(health.get("logger_hook_trusted")),
                     bool(health.get("format_hook_trusted")),
                     sorted((health.get("foreign_hooks") or {}).keys()))
