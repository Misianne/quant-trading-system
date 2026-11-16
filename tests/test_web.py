"""测试：Web 管理界面。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ai_trader.runtime import build_runtime
from ai_trader.web.app import create_app


@pytest.fixture
def client(cfg, monkeypatch):
    monkeypatch.delenv("WEB_ADMIN_PASSWORD", raising=False)
    # US-P1：/api/scan 前置守卫要求 unenforced_market_policy=reject（生产即如此）。
    # 这里把夹具切到生产口径，验证"守卫放行时扫描可用"；防绕过另有专项用例。
    cfg.raw.setdefault("market_rules", {})["unenforced_market_policy"] = "reject"
    rt = build_runtime(cfg)
    return TestClient(create_app(rt))


def test_dashboard_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert "FosunAI-Trading" in body
    assert "紧急停止" in body
    assert "风控决策" in body
    assert "审计日志" in body
    # 安全开关必须展示在页面上
    assert "真实下单" in body


def test_status_api(client):
    r = client.get("/api/status")
    assert r.status_code == 200
    data = r.json()
    assert data["broker"]["kind"] == "paper"
    assert data["safety"]["paper_trading"] is True
    assert data["safety"]["real_order_authorized"] is False
    assert data["audit"]["chain"]["ok"] is True
    assert data["ai"]["model_id"]


def test_positions_api(client):
    r = client.get("/api/positions")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_market_api_returns_quote_and_klines(client):
    r = client.get("/api/market/hk00700")
    assert r.status_code == 200
    d = r.json()
    assert d["quote"]["symbol"] == "hk00700"
    assert d["quote"]["last"] > 0
    assert len(d["klines"]) == 60
    assert {"ts", "open", "high", "low", "close"} <= set(d["klines"][0])


def test_scan_endpoint(client):
    r = client.post("/api/scan", data={"symbols": "hk00700"})
    assert r.status_code == 200
    d = r.json()
    assert d["model"].startswith("heuristic")
    assert len(d["outcomes"]) == 1


def test_monitor_endpoint(client):
    r = client.post("/api/monitor")
    assert r.status_code == 200
    assert "executed" in r.json()


def test_kill_and_resume_cycle(client):
    r = client.post("/api/kill", data={"reason": "单元测试"})
    assert r.json()["kill_switch"] is True
    assert r.json()["safety"]["kill_switch"] is True

    status = client.get("/api/status").json()
    assert status["risk"]["kill_switch"] is True

    # 解除急停必须带确认短语，空值必须被拒
    denied = client.post("/api/resume", data={"confirm": ""})
    assert denied.status_code == 400
    assert client.get("/api/status").json()["safety"]["kill_switch"] is True

    r2 = client.post("/api/resume", data={"confirm": "RESUME-TRADING"})
    assert r2.json()["kill_switch"] is False


def test_paper_reset_requires_confirmation(client):
    r = client.post("/api/paper/reset", data={"confirm": "no"})
    assert r.status_code == 400
    r2 = client.post("/api/paper/reset", data={"confirm": "RESET"})
    assert r2.status_code == 200


def test_audit_endpoint_verifies_chain(client):
    r = client.get("/api/audit?verify=true")
    d = r.json()
    assert d["chain"]["ok"] is True
    assert len(d["entries"]) > 0


def test_login_required_when_password_set(cfg, monkeypatch):
    monkeypatch.setenv("WEB_ADMIN_PASSWORD", "s3cret")
    rt = build_runtime(cfg)
    c = TestClient(create_app(rt))
    r = c.get("/", follow_redirects=False)
    assert r.status_code in (303, 307)
    assert "/login" in r.headers.get("location", "")

    assert c.get("/api/status").status_code == 401

    bad = c.post("/login", data={"password": "wrong"})
    assert bad.status_code == 401

    ok = c.post("/login", data={"password": "s3cret"}, follow_redirects=False)
    assert ok.status_code == 303
    assert c.get("/api/status").status_code == 200


def test_no_bypass_order_endpoint_exists(client):
    """Web 层不得存在"绕过风控直接下单"的接口。"""
    paths = {r.path for r in client.app.routes}
    assert "/api/order" not in paths
    assert "/api/buy" not in paths
    assert "/api/sell" not in paths


# ---------------------------------------------------------------- 绑定守卫（P1-11）
def test_bind_guard_allows_only_loopback(monkeypatch):
    """P1-11 收尾：只允许回环，且不再有 allow_insecure 逃生口。"""
    import inspect

    from ai_trader.web.app import guard_bind, is_loopback

    monkeypatch.delenv("WEB_ADMIN_PASSWORD", raising=False)
    assert is_loopback("127.0.0.1") is True
    assert is_loopback("localhost") is True
    assert is_loopback("::1") is True
    assert is_loopback("0.0.0.0") is False

    guard_bind("127.0.0.1")                     # 回环直接放行
    guard_bind("localhost")

    # 逃生口必须已移除
    assert "allow_insecure" not in inspect.signature(guard_bind).parameters

    with pytest.raises(SystemExit) as ei:
        guard_bind("0.0.0.0")
    assert "只能监听回环地址" in str(ei.value)


def test_bind_guard_refuses_non_loopback_even_with_password(monkeypatch):
    """有口令也不行 —— 公网访问必须走独立的反向代理方案。"""
    from ai_trader.web.app import guard_bind

    monkeypatch.setenv("WEB_ADMIN_PASSWORD", "s3cret")
    with pytest.raises(SystemExit):
        guard_bind("0.0.0.0")
    with pytest.raises(SystemExit):
        guard_bind("192.168.1.10")


def test_cli_has_no_insecure_bind_flag():
    from ai_trader import cli

    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["web", "--host", "0.0.0.0", "--allow-insecure-bind"])


def test_web_cmd_exit_code_when_bind_refused(tmp_path, monkeypatch):
    """CLI 层也要把这个拒绝变成可判别的退出码（4）。"""
    from ai_trader import cli
    from ai_trader.runtime import build_runtime
    from tests.conftest import make_cfg

    monkeypatch.delenv("WEB_ADMIN_PASSWORD", raising=False)
    cfg = make_cfg(tmp_path)
    monkeypatch.setattr(cli, "_rt", lambda args: build_runtime(cfg))
    assert cli.main(["web", "--host", "0.0.0.0", "--port", "8799"]) == 4
