"""Web 管理界面（FastAPI）。

只提供"看"和"有限操作"：
* 看：状态、账户、持仓、委托、信号、风控决策、审计链。
* 操作：触发扫描 / 持仓监控 / 挂单复核 / 紧急停止 / 恢复 / 重置模拟盘。

**不提供**任何"绕过风控直接下单"的入口 —— 即使手动下单也必须走
:meth:`RiskController.evaluate`。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..config import PROJECT_ROOT
from ..guards import ConfigGuardError, assert_trading_config
from ..runtime import Runtime, build_runtime, setup_logging
from ..safety.halt import RESUME_PHRASE
from ..secrets import mask

TEMPLATE_DIR = Path(__file__).parent / "templates"
COOKIE_NAME = "aitrader_session"


def _sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


class Auth:
    """极简 HMAC 签名 Cookie 鉴权。未设置口令时仅允许本机访问。"""

    def __init__(self, password: str, secret: str):
        self.password = password
        self.secret = secret or "insecure-dev-secret"

    @property
    def enabled(self) -> bool:
        return bool(self.password)

    def issue(self) -> str:
        ts = str(int(time.time()))
        return f"{ts}.{_sign(self.secret, ts)}"

    def verify(self, token: Optional[str]) -> bool:
        if not self.enabled:
            return True
        if not token or "." not in token:
            return False
        ts, sig = token.split(".", 1)
        if _sign(self.secret, ts) != sig:
            return False
        return (time.time() - int(ts)) < 12 * 3600


def create_app(rt: Optional[Runtime] = None) -> FastAPI:
    setup_logging()
    runtime = rt or build_runtime()
    cfg = runtime.cfg
    admin_password = os.getenv("WEB_ADMIN_PASSWORD", "") or ""
    auth = Auth(admin_password, os.getenv("WEB_SESSION_SECRET", ""))
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

    def _from_json(value: Any) -> Any:
        if isinstance(value, str):
            try:
                return json.loads(value)
            except ValueError:
                return value
        return value if value is not None else []

    templates.env.filters["from_json"] = _from_json

    app = FastAPI(title="FosunAI-Trading 管理台", version="0.1.0",
                  docs_url="/api/docs", redoc_url=None)

    # ------------------------------------------------------------------ #
    # 鉴权
    # ------------------------------------------------------------------ #
    def check(request: Request) -> None:
        if auth.verify(request.cookies.get(COOKIE_NAME)):
            return
        raise HTTPException(status_code=401, detail="需要登录")

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        return templates.TemplateResponse(request, "login.html", {"error": ""})

    @app.post("/login")
    def login(request: Request, password: str = Form("")):
        if auth.password and hmac.compare_digest(password, auth.password):
            resp = RedirectResponse("/", status_code=303)
            resp.set_cookie(COOKIE_NAME, auth.issue(), httponly=True, samesite="lax",
                            max_age=12 * 3600)
            return resp
        return templates.TemplateResponse(
            request, "login.html", {"error": "口令错误"}, status_code=401
        )

    @app.get("/logout")
    def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE_NAME)
        return resp

    # ------------------------------------------------------------------ #
    # 页面
    # ------------------------------------------------------------------ #
    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        if auth.verify(request.cookies.get(COOKIE_NAME)) is False:
            return RedirectResponse("/login", status_code=303)
        status = runtime.status()
        try:
            summaries = (runtime.broker.summaries() if hasattr(runtime.broker, "summaries")
                         else [runtime.broker.get_summary()])
        except Exception as exc:  # noqa: BLE001
            summaries = []
            status["broker"]["summaries_error"] = str(exc)
        try:
            positions = [p.as_dict() for p in runtime.broker.get_positions()]
        except Exception:  # noqa: BLE001
            positions = []
        ctx = {
            "request": request,
            "status": status,
            "summaries": [s.as_dict() for s in summaries],
            "positions": positions,
            "orders": runtime.journal.list_orders(limit=30),
            "signals": runtime.journal.list_signals(limit=20),
            "decisions": runtime.journal.list_risk_decisions(limit=20),
            "audit": runtime.journal.list_audit(limit=40),
            "stops": runtime.engine.stops,
            "last_scan": runtime.engine.last_scan.as_dict() if runtime.engine.last_scan else None,
            "mask": mask,
            "universe": [u["code"] for u in cfg.universe],
        }
        return templates.TemplateResponse(request, "dashboard.html", ctx)

    # ------------------------------------------------------------------ #
    # JSON API
    # ------------------------------------------------------------------ #
    @app.get("/api/status")
    def api_status(request: Request):
        check(request)
        return JSONResponse(runtime.status())

    @app.get("/api/positions")
    def api_positions(request: Request):
        check(request)
        return JSONResponse([p.as_dict() for p in runtime.broker.get_positions()])

    @app.get("/api/orders")
    def api_orders(request: Request, limit: int = 50):
        check(request)
        return JSONResponse(runtime.journal.list_orders(limit=limit))

    @app.get("/api/signals")
    def api_signals(request: Request, limit: int = 50):
        check(request)
        return JSONResponse(runtime.journal.list_signals(limit=limit))

    @app.get("/api/risk")
    def api_risk(request: Request, limit: int = 50):
        check(request)
        return JSONResponse({
            "limits": runtime.risk.describe_limits(),
            "decisions": runtime.journal.list_risk_decisions(limit=limit),
        })

    @app.get("/api/audit")
    def api_audit(request: Request, limit: int = 100, verify: bool = False):
        check(request)
        out: Dict[str, Any] = {"entries": runtime.journal.list_audit(limit=limit)}
        if verify:
            out["chain"] = runtime.journal.verify_chain()
        return JSONResponse(out)

    @app.get("/api/market/{symbol}")
    def api_market(request: Request, symbol: str):
        check(request)
        q = runtime.broker.get_quote(symbol)
        kl = runtime.broker.get_klines(symbol, "day", 60)
        return JSONResponse({"quote": q.as_dict(),
                             "klines": [c.__dict__ for c in kl]})

    # ------------------------------------------------------------------ #
    # 操作
    # ------------------------------------------------------------------ #
    def _guard() -> None:
        """交易前置守卫（US-P1）：配置不满足条件时拒绝触发交易流程。"""
        try:
            assert_trading_config(runtime.cfg)
        except ConfigGuardError as exc:
            raise HTTPException(status_code=409, detail={
                "error": "config_guard_violation",
                "violations": [v.as_dict() for v in exc.violations],
                "hint": "运行 `ai-trader guard` 查看详情；修好配置后再试",
            }) from exc

    @app.post("/api/scan")
    def api_scan(request: Request, symbols: str = Form("")):
        check(request)
        _guard()
        targets = [s.strip() for s in symbols.split(",") if s.strip()] or None
        report = runtime.engine.scan(targets)
        return JSONResponse(report.as_dict())

    @app.post("/api/monitor")
    def api_monitor(request: Request):
        check(request)
        # 减仓不受守卫限制：配置错误不该让持仓失去保护。
        # 守卫只在"要不要开新仓"的 scan 路径上强拦。
        return JSONResponse({"executed": runtime.engine.monitor()})

    @app.post("/api/kill")
    def api_kill(request: Request, reason: str = Form("手动紧急停止（Web）")):
        check(request)
        try:
            runtime.risk.halt(reason, actor="web")
        except Exception as exc:  # noqa: BLE001
            runtime.journal.log("kill_switch_persist_failed",
                                {"reason": reason, "error": str(exc)}, actor="web")
            return JSONResponse(
                {"kill_switch": True, "reason": reason, "persisted": False,
                 "error": f"急停已生效但持久化失败：{exc}",
                 "safety": cfg.gate.snapshot()}, status_code=500)
        runtime.journal.log("kill_switch_on", {"reason": reason}, actor="web")
        return JSONResponse({"kill_switch": True, "reason": reason, "persisted": True,
                             "safety": cfg.gate.snapshot()})

    @app.post("/api/resume")
    def api_resume(request: Request, confirm: str = Form("")):
        check(request)
        # 解除急停必须人工逐字确认，不接受空值/默认值
        if confirm != RESUME_PHRASE:
            runtime.journal.log("kill_switch_resume_rejected",
                                {"confirm_received": bool(confirm)}, actor="web")
            raise HTTPException(
                400, f"解除急停需要 confirm={RESUME_PHRASE}（收到 {confirm!r}）")
        try:
            runtime.risk.resume(confirm, actor="web")
        except Exception as exc:  # noqa: BLE001
            runtime.journal.log("kill_switch_resume_failed", {"error": str(exc)},
                                actor="web")
            raise HTTPException(500, f"解除急停失败（仍处于停止状态）：{exc}")
        runtime.journal.log("kill_switch_off", {"actor": "web"}, actor="web")
        return JSONResponse({"kill_switch": False, "safety": cfg.gate.snapshot()})

    @app.post("/api/orders/{order_id}/cancel")
    def api_cancel_order(request: Request, order_id: str,
                         reason: str = Form("Web 手动撤单")):
        check(request)
        result = runtime.engine.cancel_order(order_id, reason=reason)
        if not result.get("ok"):
            return JSONResponse(result, status_code=409)
        return JSONResponse(result)

    @app.post("/api/paper/reset")
    def api_reset(request: Request, confirm: str = Form("")):
        check(request)
        if confirm != "RESET":
            raise HTTPException(400, "需要 confirm=RESET 才能重置模拟盘")
        if runtime.broker_kind != "paper":
            raise HTTPException(409, "当前不是模拟盘，拒绝重置")
        runtime.broker.reset()
        runtime.journal.log("paper_reset", {}, actor="web")
        return JSONResponse({"ok": True})

    @app.exception_handler(HTTPException)
    def http_exc(request: Request, exc: HTTPException):
        if request.url.path.startswith("/api"):
            return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
        if exc.status_code == 401:
            return RedirectResponse("/login", status_code=303)
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    return app


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", ""})


def is_loopback(host: str) -> bool:
    return str(host).strip().lower() in LOOPBACK_HOSTS


def guard_bind(host: str) -> None:
    """**只允许**绑定回环地址。

    审计 P1-11 收尾：不再提供 ``--allow-insecure-bind`` 逃生口。
    理由：管理台的权限等于"能按急停、能撤单、能触发扫描"，把它暴露到非回环地址
    就必须另做一套经过认证的反向代理方案（TLS + 鉴权 + 访问控制），
    而不是靠一个命令行开关"自担风险"。

    需要公网访问时：保持本服务监听 127.0.0.1，由 Caddy/Nginx/IIS 做 TLS 与鉴权后反代。
    （见 DEPLOYMENT.md 的"对外暴露"章节）
    """
    if is_loopback(host):
        return
    raise SystemExit(
        f"拒绝启动：管理台只能监听回环地址，收到 {host!r}。\n"
        f"管理台拥有急停/撤单/扫描权限，暴露到非回环地址必须另有认证方案。\n"
        f"如需公网访问，请保持 127.0.0.1 监听，并在前面加一层带鉴权的反向代理"
        f"（DEPLOYMENT.md '对外暴露'章节）。"
    )


def main() -> None:  # pragma: no cover - 入口脚本
    import uvicorn

    runtime = build_runtime()
    web = runtime.cfg.raw.get("web") or {}
    host = str(os.getenv("WEB_HOST") or web.get("host") or "127.0.0.1")
    port = int(os.getenv("WEB_PORT") or web.get("port") or 8781)
    guard_bind(host)
    print("=" * 68)
    print(f"FosunAI-Trading 管理台  http://{host}:{port}")
    print(f"模式: {runtime.cfg.mode} | 券商: {runtime.broker_kind} | "
          f"AI: {runtime.model.name}/{runtime.model.model_id}")
    print(f"安全开关: {json.dumps(runtime.cfg.gate.snapshot(), ensure_ascii=False)}")
    print("=" * 68)
    uvicorn.run(create_app(runtime), host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
