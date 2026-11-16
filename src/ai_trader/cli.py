"""命令行入口：`ai-trader <command>`。

所有命令都走 :func:`ai_trader.runtime.build_runtime`，与 Web 完全同一套装配逻辑。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, List, Optional

from .config import PROJECT_ROOT, load_config
from .guards import ConfigGuardError, assert_trading_config, guard_summary
from .runtime import Runtime, build_runtime, setup_logging
from .safety.halt import RESUME_PHRASE, HaltStore
from .secrets import DPAPI_AVAILABLE, SecretVault, mask, redact_pem


def _j(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


def _rt(args) -> Runtime:
    return build_runtime(load_config(Path(args.config) if args.config else None))


# --------------------------------------------------------------------------- #
def cmd_status(args) -> int:
    rt = _rt(args)
    print(_j(rt.status()))
    return 0


EXIT_GUARD = 4


def _guard_or_fail(cfg, *, json_out: bool = True) -> Optional[int]:
    """交易前置守卫：不通过时**拒绝启动交易流程**（退出码 4）。"""
    try:
        assert_trading_config(cfg)
    except ConfigGuardError as exc:
        print(_j({"ok": False, "error": "config_guard_violation",
                  **exc.as_dict()}), file=sys.stderr)
        return EXIT_GUARD
    return None


def cmd_guard(args) -> int:
    """只做配置守卫检查（worker 启动前的第一道闸）。"""
    cfg = load_config(Path(args.config) if args.config else None)
    summary = guard_summary(cfg)
    print(_j(summary))
    return 0 if summary["ok"] else EXIT_GUARD


def cmd_scan(args) -> int:
    _cfg = load_config(Path(args.config) if args.config else None)
    rc = _guard_or_fail(_cfg)
    if rc is not None:
        return rc
    rt = build_runtime(_cfg)
    symbols = [s.strip() for s in (args.symbols or "").split(",") if s.strip()] or None
    report = rt.engine.scan(symbols)
    print(_j(report.as_dict()))
    return 0 if not report.errors else 1


def cmd_monitor(args) -> int:
    _cfg = load_config(Path(args.config) if args.config else None)
    rc = _guard_or_fail(_cfg)
    if rc is not None:
        return rc
    rt = build_runtime(_cfg)
    print(_j({"executed": rt.engine.monitor()}))
    return 0


def cmd_kill(args) -> int:
    rt = _rt(args)
    try:
        rt.risk.halt(args.reason, actor="cli")
        persisted = True
        err = ""
    except Exception as exc:  # noqa: BLE001
        # 已写入内存停止态；哨兵写失败 → 依然停止，但要如实报告
        persisted = False
        err = str(exc)
    rt.journal.log("kill_switch_on" if persisted else "kill_switch_persist_failed",
                   {"reason": args.reason, "error": err}, actor="cli")
    print(_j({"kill_switch": True, "persisted": persisted, "error": err,
              "halt_file": str(getattr(rt.cfg.halt_store, "path", "")),
              "safety": rt.cfg.gate.snapshot()}))
    return 0 if persisted else 3


def cmd_resume(args) -> int:
    rt = _rt(args)
    try:
        rt.risk.resume(args.confirm or "", actor="cli")
    except Exception as exc:  # noqa: BLE001
        rt.journal.log("kill_switch_resume_rejected",
                       {"error": str(exc)}, actor="cli")
        print(_j({"kill_switch": True, "error": str(exc),
                  "hint": "必须提供 --confirm RESUME-TRADING"}), file=sys.stderr)
        return 2
    rt.journal.log("kill_switch_off", {"actor": "cli"}, actor="cli")
    print(_j({"kill_switch": False, "safety": rt.cfg.gate.snapshot()}))
    return 0


def cmd_cancel(args) -> int:
    """撤单（走完整链路：查单 → 门禁 → 撤单 → 回写状态）。"""
    rt = _rt(args)
    result = rt.engine.cancel_order(args.order_id, reason=args.reason)
    print(_j(result))
    return 0 if result.get("ok") else 1


def cmd_check_env(args) -> int:
    from .broker.fosun_adapter import FosunBroker

    cfg = load_config(Path(args.config) if args.config else None)
    vault = SecretVault()
    fosun = FosunBroker(cfg, vault)

    # AI 决策权威摘要（不发起任何网络请求）
    from .ai.authority import DecisionAuthority
    from .runtime import build_ai

    _model, _ai_reason = build_ai(cfg)
    _auth = DecisionAuthority(cfg, _model)
    _auth_state = _auth.snapshot()
    _auth_decision = _auth.can_decide_entry()

    out = {
        "python": sys.version.split()[0],
        "project_root": str(PROJECT_ROOT),
        "config_path": str(cfg.path),
        "config_exists": cfg.path.exists(),
        "dotenv_loaded": cfg.dotenv_loaded,
        "dpapi_available": DPAPI_AVAILABLE,
        "safety": cfg.gate.snapshot(),
        "vault": vault.status().as_dict(),
        "fosun": fosun.health(),
        "sdk_importable": _sdk_importable(),
        # US-P1 配置守卫：不通过则 worker/scan/monitor 拒绝启动
        "config_guard": guard_summary(cfg),
        "market_rules": _market_rules_summary(cfg),
        "market_feed": _market_feed_summary(cfg),
        "ai": {
            "model": f"{_model.name}/{_model.model_id}",
            "can_trade": _auth_state.can_trade,
            "available": _auth_state.available,
            "require_ai_for_entry": _auth_state.require_ai_for_entry,
            "entry_allowed": _auth_decision.allowed,
            "entry_block_reason": _auth_decision.reason,
            "code": _auth_decision.code,
            "fallback_reason": _ai_reason,
        },
        "note": "凭证缺失时 fosun.configured=false，系统不会假装已连接；"
                "AI 不可用时 entry_allowed=false（不会开仓）",
    }
    print(_j(out))
    return 0


def _sdk_importable() -> dict:
    try:
        import fsopenapi
        return {"ok": True, "path": getattr(fsopenapi, "__file__", "")}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}



def _market_rules_summary(cfg) -> dict:
    """B4/US-P0：交易规则摘要（含所有非 VERIFIED 的规则与会话时区）。"""
    from .market.rules import MarketRuleSet

    rs = MarketRuleSet(cfg.raw.get("market_rules") or {})
    unverified = rs.unverified_rules()
    sessions: dict = {}
    for mkt in sorted(rs.clocks):
        info = rs.session_info(mkt)
        sessions[mkt] = {
            "tz": rs.clocks[mkt].tz_name,
            "local_now": info.local_now if info else "",
            "phase": info.phase if info else "",
            "regular_open": info.is_regular_open if info else False,
            "closes_at": info.closes_at if info else "",
            "reason": info.reason if info else "",
        }
    return {
        "enabled": rs.enabled,
        "enforce_markets": sorted(rs.enforce_markets),
        "unenforced_market_policy": rs.unenforced_policy,
        "allow_unverified_rules": rs.allow_unverified,
        "boards": sorted(rs.boards),
        "calendar_status": rs.calendar.status,
        "calendar_strict": rs.calendar.strict,
        "calendar_markets": {k: {"status": e.status,
                                 "covered": f"{e.covered_from}~{e.covered_until}"}
                             for k, e in rs.calendar.entries.items()},
        "sessions": sessions,
        "clock_errors": dict(rs.clock_errors),
        "suspended_symbols": sorted(rs.suspended),
        "unverified_count": len(unverified),
        "unverified": unverified,
        "note": "UNKNOWN 规则一律拒绝交易；UNVERIFIED 由 allow_unverified_rules 决定；"
                "交易时段按 sessions.<mkt>.tz 的交易所当地时间判断",
    }


def _market_feed_summary(cfg) -> dict:
    """B5：实时行情推送摘要（不发起连接）。"""
    from .market.feed import FeedConfig

    raw = cfg.raw.get("market_feed") or {}
    enabled = bool(raw.get("enabled"))
    fc = FeedConfig.from_cfg(raw)
    return {
        "enabled": enabled,
        "url_configured": bool(fc.url),
        "topics": list(fc.topics),
        "codes": list(fc.codes),
        "required_for_entry": bool(raw.get("required_for_entry", True)),
        "enforce_markets": list(raw.get("enforce_markets") or []),
        "heartbeat_timeout": fc.heartbeat_timeout,
        "stale_after": fc.stale_after,
        "protocol": "unverified（真实推送字段名未核对）",
        "note": "未启用时开仓不依赖推送；启用后行情不新鲜将禁止新开仓",
    }


def cmd_sdk_audit(args) -> int:
    """反射式列出官方 SDK 暴露的接口，用于 API_STATUS.md 的证据。"""
    try:
        import fsopenapi
    except Exception as exc:  # noqa: BLE001
        print(_j({"ok": False, "error": str(exc)}))
        return 2

    inventory: dict = {"sdk_path": getattr(fsopenapi, "__file__", ""),
                       "modules": {}, "exports": sorted(getattr(fsopenapi, "__all__", []))}
    classes = {
        "account": fsopenapi.AccountAPI,
        "portfolio": fsopenapi.PortfolioAPI,
        "trade": fsopenapi.TradeAPI,
        "market": fsopenapi.MarketAPI,
        "session": fsopenapi.SessionAPI,
        "market_push": getattr(fsopenapi, "MarketPushAPI", None),
        "optmarket": getattr(fsopenapi, "OptMarketAPI", None),
        "ops_trade": getattr(fsopenapi, "OpsTradeAPI", None),
    }
    for name, cls in classes.items():
        if cls is None:
            continue
        methods = {}
        for attr in sorted(dir(cls)):
            if attr.startswith("_"):
                continue
            fn = getattr(cls, attr, None)
            if callable(fn):
                try:
                    import inspect
                    sig = str(inspect.signature(fn))
                except (TypeError, ValueError):
                    sig = "(...)"
                methods[attr] = sig
        inventory["modules"][name] = methods
    print(_j(inventory))
    return 0


# --------------------------------------------------------------------------- #
def _resolve_passphrase(args) -> str:
    """口令来源：--passphrase 优先，其次环境变量。两者都没有则返回空串。"""
    if getattr(args, "passphrase", None):
        return str(args.passphrase)
    env_name = getattr(args, "passphrase_env", "VAULT_PASSPHRASE") or "VAULT_PASSPHRASE"
    return os.getenv(env_name, "")


def cmd_vault(args) -> int:
    from .secrets import open_with_passphrase, seal_with_passphrase  # noqa: F401

    vault = SecretVault()
    if args.vault_cmd == "list":
        print(_j(vault.status().as_dict()))
        return 0
    if args.vault_cmd == "set":
        if not args.name or args.value is None:
            print("用法: ai-trader vault set --name KEY --value VALUE", file=sys.stderr)
            return 2
        vault.set(args.name, args.value)
        print(_j({"ok": True, "name": args.name, "preview": mask(args.value)}))
        return 0
    if args.vault_cmd == "get":
        val = vault.get(args.name or "")
        if val is None:
            print(_j({"ok": False, "error": "not found"}))
            return 1
        if args.show:
            print(val)
        else:
            print(_j({"name": args.name, "preview": mask(val),
                      "pem": redact_pem(val) if "PEM" in val else ""}))
        return 0
    if args.vault_cmd == "delete":
        print(_j({"deleted": vault.delete(args.name or "")}))
        return 0
    if args.vault_cmd == "export":
        passphrase = _resolve_passphrase(args)
        if not passphrase:
            print("必须提供 --passphrase，或设置环境变量（默认 VAULT_PASSPHRASE）",
                  file=sys.stderr)
            return 2
        blob = vault.export_encrypted(passphrase)
        out = Path(args.out or (PROJECT_ROOT / "data" / "vault_export.bin"))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(blob)
        print(_j({"ok": True, "path": str(out), "bytes": len(blob)}))
        return 0
    if args.vault_cmd == "import":
        passphrase = _resolve_passphrase(args)
        if not args.infile or not passphrase:
            print("用法: vault import --infile X（口令用环境变量 VAULT_PASSPHRASE 传入）",
                  file=sys.stderr)
            return 2
        blob = Path(args.infile).read_bytes()
        imported = vault.import_encrypted(blob, passphrase, overwrite=args.overwrite)
        print(_j({"ok": True, "imported": imported}))
        return 0
    print("未知 vault 子命令", file=sys.stderr)
    return 2


def cmd_demo(args) -> int:
    """离线全链路演示：信号 → 风控 → 模拟成交 → 止损触发 → 审计校验。

    整个过程在独立沙盒目录里完成，**不触碰主账户状态**，
    且强制 ``enforce_market_hours=false`` 以便任意时间可复现。
    """
    import copy
    import shutil
    import tempfile
    from datetime import datetime

    from .ai.providers import HeuristicModel
    from .audit.journal import AuditJournal
    from .broker.paper import PaperBroker
    from .config import AppConfig, build_gate
    from .risk.controller import RiskController
    from .strategy.engine import StrategyEngine

    base = load_config(use_dotenv=False)
    raw = copy.deepcopy(base.raw)
    sandbox = Path(tempfile.mkdtemp(prefix="fosunai-demo-"))
    raw.setdefault("system", {})
    raw["system"]["data_dir"] = str(sandbox)
    raw["system"]["log_dir"] = str(sandbox)
    raw.setdefault("audit", {})
    raw["audit"]["db_path"] = str(sandbox / "audit.db")
    raw["audit"]["jsonl_path"] = str(sandbox / "audit.jsonl")
    raw.setdefault("risk", {})["enforce_market_hours"] = False
    raw.setdefault("paper", {})["initial_cash"] = {"HKD": 1_000_000.0, "USD": 130_000.0,
                                                   "CNY": 700_000.0}
    cfg = AppConfig(raw=raw, path=sandbox / "config.yaml",
                    gate=build_gate(raw, data_dir=sandbox))
    cfg.halt_store = HaltStore(sandbox)      # 演示也走真实哨兵，确保急停链路一致

    journal = AuditJournal(sandbox / "audit.db", sandbox / "audit.jsonl")
    broker = PaperBroker(state_path=sandbox / "paper.json",
                         initial_cash={"HKD": 1_000_000.0, "USD": 130_000.0, "CNY": 700_000.0},
                         commission_bps=8.0, min_commission=3.0, slippage_bps=5.0)
    risk = RiskController(cfg, cfg.gate)
    # demo 是**沙盒**：显式允许规则引擎开仓，并在审计里留一条 rule_engine_sandbox。
    # 生产装配（runtime.build_runtime）永远传 False。
    engine = StrategyEngine(cfg, broker, HeuristicModel(), risk, journal,
                            stops_path=sandbox / "stops.json",
                            allow_rule_engine_entry=True)

    report: dict = {"sandbox": str(sandbox), "steps": []}
    t0 = datetime(2026, 11, 4, 10, 0, 0)      # 周三 10:00，港股盘中

    # --- 1. 扫描并下单 ---
    scan = engine.scan(["hk00700"], now=t0)
    report["steps"].append({"step": "1_scan", "summary": scan.summary,
                            "outcome": scan.outcomes[0].as_dict()})
    # --- 2. 账户 ---
    s = broker.get_summary("HKD")
    report["steps"].append({"step": "2_account", "summary": s.as_dict(),
                            "positions": [p.as_dict() for p in broker.get_positions()]})
    # --- 3. 止损触发 ---
    if broker.get_positions():
        px = broker.get_quote("hk00700").last
        engine.register_stops("hk00700", stop_loss=px * 1.02, take_profit=None, qty=100)
        executed = engine.monitor(now=t0)
        report["steps"].append({"step": "3_stop_loss", "executed": executed[:3],
                                "positions_after": [p.as_dict() for p in broker.get_positions()]})
    # --- 4. 重复下单拦截 ---
    from .risk.controller import OrderIntent
    pf = engine.build_portfolio("HKD", t0)
    dup = risk.evaluate(OrderIntent(symbol="hk00700", side="buy", qty=100, price=300.0,
                                    market="hk", currency="HKD", stop_loss=290.0),
                        pf, last_price=300.0, now=t0)
    report["steps"].append({"step": "4_duplicate_guard", "approved": dup.approved,
                            "reasons": dup.reasons})
    # --- 5. 紧急停止 ---
    risk.halt("演示用紧急停止", actor="demo")
    halted = engine.scan(["hk00700"], now=t0)
    report["steps"].append({"step": "5_kill_switch", "blocked_reason": halted.blocked_reason,
                            "orders_placed": len(halted.outcomes)})
    risk.resume(RESUME_PHRASE, actor="demo")
    # --- 6. 审计 ---
    report["audit"] = {"counts": journal.counts(), "chain": journal.verify_chain()}

    print(_j(report))
    journal.close()
    if not args.keep:
        shutil.rmtree(sandbox, ignore_errors=True)
        print(f"\n[sandbox 已清理] {sandbox}", file=sys.stderr)
    else:
        print(f"\n[sandbox 保留] {sandbox}", file=sys.stderr)
    return 0


def cmd_web(args) -> int:
    from .web.app import create_app, guard_bind

    import uvicorn

    rt = _rt(args)
    web = rt.cfg.raw.get("web") or {}
    host = args.host or str(web.get("host") or "127.0.0.1")
    port = int(args.port or web.get("port") or 8781)
    level = getattr(args, "uvicorn_log_level", None) or args.log_level or "info"
    # 只允许回环绑定（审计 P1-11：逃生口已移除）
    try:
        guard_bind(host)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 4
    print(f"启动管理台 http://{host}:{port}  ({rt.cfg.mode} / {rt.broker_kind})")
    uvicorn.run(create_app(rt), host=host, port=port, log_level=level)
    return 0


# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ai-trader", description="星财富单 AI 自动交易系统 CLI")
    p.add_argument("--config", help="配置文件路径（默认 config/config.yaml）")
    p.add_argument("--log-level", default="INFO")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="打印完整运行状态").set_defaults(func=cmd_status)
    sub.add_parser("check-env", help="检查环境/凭证/门禁状态").set_defaults(func=cmd_check_env)

    s = sub.add_parser("scan", help="执行一次全量扫描")
    s.add_argument("--symbols", help="逗号分隔，留空用观察池")
    s.set_defaults(func=cmd_scan)

    sub.add_parser("monitor", help="执行一次持仓监控（止损/止盈/挂单复核）").set_defaults(func=cmd_monitor)

    # 注意：--config 是全局参数（定义在根 parser 上），子命令不要再声明，
    # 否则子命令的默认值会覆盖父命名空间，导致 --config 被静默忽略。
    sub.add_parser("guard",
                   help="检查交易配置守卫（不通过则退出码 4）").set_defaults(func=cmd_guard)

    k = sub.add_parser("kill", help="触发紧急停止（持久化到 data/HALT.json）")
    k.add_argument("--reason", default="CLI 手动紧急停止")
    k.set_defaults(func=cmd_kill)

    r = sub.add_parser("resume", help="解除紧急停止（必须人工确认）")
    r.add_argument("--confirm", required=True,
                   help="确认短语，必须逐字为 RESUME-TRADING")
    r.set_defaults(func=cmd_resume)

    c = sub.add_parser("cancel", help="撤单")
    c.add_argument("--order-id", required=True)
    c.add_argument("--reason", default="CLI 手动撤单")
    c.set_defaults(func=cmd_cancel)
    sub.add_parser("sdk-audit", help="反射列出官方 SDK 接口清单").set_defaults(func=cmd_sdk_audit)

    d = sub.add_parser("demo", help="离线全链路演示（沙盒，不触碰主账户）")
    d.add_argument("--keep", action="store_true", help="保留沙盒目录便于查看")
    d.set_defaults(func=cmd_demo)

    w = sub.add_parser("web", help="启动 Web 管理台")
    w.add_argument("--host")
    w.add_argument("--port", type=int)
    # 与全局 --log-level 同 dest；用 SUPPRESS 避免子命令默认值覆盖全局值
    w.add_argument("--log-level", default=argparse.SUPPRESS)
    w.add_argument("--uvicorn-log-level", default=None,
                   help="uvicorn 日志级别（默认取 --log-level）")
    w.set_defaults(func=cmd_web)

    v = sub.add_parser("vault", help="密钥保险箱操作")
    vsub = v.add_subparsers(dest="vault_cmd", required=True)
    vsub.add_parser("list")
    vs = vsub.add_parser("set")
    vs.add_argument("--name", required=True)
    vs.add_argument("--value", required=True)
    vg = vsub.add_parser("get")
    vg.add_argument("name")
    vg.add_argument("--show", action="store_true", help="输出明文（谨慎）")
    vd = vsub.add_parser("delete")
    vd.add_argument("name")
    ve = vsub.add_parser("export")
    ve.add_argument("--passphrase", default=None,
                    help="口令；省略时从环境变量 VAULT_PASSPHRASE 读取（推荐，避免进程命令行泄漏）")
    ve.add_argument("--passphrase-env", default="VAULT_PASSPHRASE",
                    help="从哪个环境变量读口令，默认 VAULT_PASSPHRASE")
    ve.add_argument("--out")
    vi = vsub.add_parser("import")
    vi.add_argument("--infile", required=True)
    vi.add_argument("--passphrase", default=None,
                    help="口令；省略时从环境变量 VAULT_PASSPHRASE 读取")
    vi.add_argument("--passphrase-env", default="VAULT_PASSPHRASE")
    vi.add_argument("--overwrite", action="store_true")
    v.set_defaults(func=cmd_vault)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    setup_logging()
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
