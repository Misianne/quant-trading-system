"""P1-3 / P1-7 / P1-8：HTTP 超时、断线重连、行情加密兜底、SDK 未修改。

注意：本文件所有网络行为都发生在本机回环地址，不接触任何真实网关。
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from ai_trader.broker.base import BrokerError, OrderRequest, OrderStateUnknown
from ai_trader.broker.fosun_adapter import FosunBroker
from tests.helpers import live_cfg

ROOT = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------ 替身
class _Resp:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class _TradeAPI:
    def __init__(self, exc=None, payload=None):
        self.exc = exc
        self.payload = payload or {"code": 0, "data": {"orderId": "B1", "status": "submitted"}}
        self.calls = 0

    def create_order(self, **kw):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.payload

    def list_orders(self, **kw):
        return {"code": 0, "data": {"list": []}}


class _MarketAPI:
    def __init__(self, quote_exc=None, quote_rows=None, min_rows=None):
        self.quote_exc = quote_exc
        self.quote_rows = quote_rows if quote_rows is not None else [
            {"lastPrice": "300.5", "prevClose": "298"}]
        self.min_rows = min_rows if min_rows is not None else [
            {"lastPrice": "299.9", "prevClose": "298"}]
        self.calls = {"quote": 0, "min": 0}

    def quote(self, codes, fields=None):
        self.calls["quote"] += 1
        if self.quote_exc:
            raise self.quote_exc
        return {"code": 0, "data": {"list": self.quote_rows}}

    def min(self, code, count=5):
        self.calls["min"] += 1
        return {"code": 0, "data": {"list": self.min_rows}}


class _Session:
    def __init__(self):
        self.creates = 0

    def create_session(self):
        self.creates += 1
        return {"sessionId": "s"}


class _Client:
    def __init__(self, trade=None, market=None):
        self.trade = trade or _TradeAPI()
        self.market = market or _MarketAPI()
        self.session = _Session()


def _broker(tmp_path, **fosun_over):
    cfg = live_cfg(tmp_path, **{f"fosun.{k}": v for k, v in fosun_over.items()})
    b = FosunBroker(cfg, None)
    b.configured = True
    return b


def _req(**kw):
    base = dict(symbol="hk00700", market="hk", side="buy", qty=100, price=300.0,
                currency="HKD", decision_id="D1", signal_id="S1")
    base.update(kw)
    return OrderRequest(**base)


# ------------------------------------------------------------------ 超时注入
class _FakeSession:
    def __init__(self):
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, "kwargs": kwargs})
        return "ok"


def test_http_timeout_injected_into_sdk_session(tmp_path):
    b = _broker(tmp_path, http_connect_timeout=3, http_read_timeout=7)
    sess = _FakeSession()
    b.client = type("C", (), {"_http_session": sess})()
    b._install_http_timeout()

    assert sess.request("POST", "http://x/v1/trade/OrderCreate") == "ok"
    kw = sess.calls[-1]["kwargs"]
    assert kw["timeout"] == (3.0, 7.0), "SDK 没设 timeout，必须由适配器注入"


def test_http_timeout_not_installed_twice(tmp_path):
    b = _broker(tmp_path)
    sess = _FakeSession()
    b.client = type("C", (), {"_http_session": sess})()
    b._install_http_timeout()
    first = sess.request
    b._install_http_timeout()
    assert sess.request is first, "重复安装会不断包裹，应该幂等"


@pytest.mark.network
def test_timeout_actually_fires_on_real_socket(tmp_path):
    """真回环服务故意慢响应：注入的超时必须真的中断，而不是摆设。"""

    class Slow(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            time.sleep(3)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

    srv = HTTPServer(("127.0.0.1", 0), Slow)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    import requests

    b = _broker(tmp_path, http_connect_timeout=1, http_read_timeout=1)
    real = requests.Session()
    b.client = type("C", (), {"_http_session": real})()
    b._install_http_timeout()

    started = time.time()
    with pytest.raises(requests.exceptions.ReadTimeout):
        real.request("GET", f"http://127.0.0.1:{port}/slow")
    elapsed = time.time() - started
    assert elapsed < 2.5, f"超时未生效，实际耗时 {elapsed:.1f}s"
    srv.shutdown()


# ------------------------------------------------------------------ 只读重试 / 重连
def test_read_retries_on_network_error(tmp_path):
    import requests

    b = _broker(tmp_path)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.exceptions.ConnectionError("conn reset")
        return "ok"

    assert b._read(flaky) == "ok"
    assert calls["n"] == 2
    assert b._conn.retries == 1


def test_read_does_not_retry_beyond_limit(tmp_path):
    import requests

    b = _broker(tmp_path, read_retry_attempts=2)
    calls = {"n": 0}

    def always_fail():
        calls["n"] += 1
        raise requests.exceptions.ConnectionError("down")

    with pytest.raises(Exception):
        b._read(always_fail)
    assert calls["n"] == 2


def test_auth_error_triggers_reconnect_then_retry(tmp_path):
    from fsopenapi import AuthenticationError

    b = _broker(tmp_path)
    b.client = _Client()
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise AuthenticationError(code=401, message="session expired")
        return "ok"

    assert b._read(flaky) == "ok"
    assert calls["n"] == 2
    assert b.client.session.creates == 1, "鉴权失败应重建一次会话"
    assert b._conn.connects == 1


def test_place_order_never_retried(tmp_path):
    import requests

    b = _broker(tmp_path)
    trade = _TradeAPI(exc=requests.exceptions.ReadTimeout("read timeout"))
    b.client = _Client(trade=trade)

    with pytest.raises(OrderStateUnknown):
        b.place_order(_req())
    assert trade.calls == 1, "下单绝不能重试（可能变成两笔单）"


def test_reconcile_passes_client_id(tmp_path):
    b = _broker(tmp_path)
    captured = {}

    class _TradeWithList(_TradeAPI):
        def list_orders(self, **kw):
            captured.update(kw)
            return {"code": 0, "data": {"list": [{
                "orderId": "BRK-7", "stockCode": "00700", "direction": "1",
                "quantity": "100", "price": "300", "status": "filled",
                "clientId": kw.get("client_id")}]}}

    b.client = _Client(trade=_TradeWithList())
    found = b.reconcile_order(client_id="123456", symbol="hk00700",
                              side="buy", qty=100)
    assert captured.get("client_id") == 123456
    assert found is not None and found.broker_order_id == "BRK-7"


def test_reconcile_returns_none_when_not_found(tmp_path):
    """查询成功但确实没有这笔单 → 返回 None（与"查不清"必须区分）。"""
    b = _broker(tmp_path)
    b.client = _Client(trade=_TradeAPI())      # list_orders 返回空列表
    assert b.reconcile_order(client_id="9", symbol="hk00700", side="buy") is None


def test_reconcile_raises_when_unable_to_query(tmp_path):
    b = _broker(tmp_path)

    class _Broken(_TradeAPI):
        def list_orders(self, **kw):
            raise BrokerError("网关 503", code="UPSTREAM")

    b.client = _Client(trade=_Broken())
    with pytest.raises(BrokerError):
        b.reconcile_order(client_id="9", symbol="hk00700")


# ------------------------------------------------------------------ 行情加密兜底
def test_quote_falls_back_to_min_on_decrypt_error(tmp_path):
    from fsopenapi import APIError

    b = _broker(tmp_path)
    market = _MarketAPI(quote_exc=APIError(code=-1, message="Response decryption failed"))
    b.client = _Client(market=market)

    q = b.get_quote("hk00700")
    assert q.last == 299.9
    assert q.source == "fosun_market_min_fallback"
    assert b._quote_via_min is True
    assert "decryption" in b._market_fallback_reason

    # 之后不再尝试 POST（避免每次都等一次失败）
    q2 = b.get_quote("hk00700")
    assert market.calls["quote"] == 1
    assert market.calls["min"] == 2
    assert q2.last == 299.9


def test_quote_no_fallback_on_auth_error(tmp_path):
    from fsopenapi import AuthenticationError

    b = _broker(tmp_path)
    market = _MarketAPI(quote_exc=AuthenticationError(code=401, message="bad session"))
    b.client = _Client(market=market)
    with pytest.raises(AuthenticationError):
        b.get_quote("hk00700")
    assert b._quote_via_min is False
    assert market.calls["min"] == 0


def test_quote_workaround_can_be_disabled(tmp_path):
    from fsopenapi import APIError

    b = _broker(tmp_path, market_encryption_workaround="off")
    b.client = _Client(market=_MarketAPI(
        quote_exc=APIError(code=-1, message="Response decryption failed")))
    with pytest.raises(APIError):
        b.get_quote("hk00700")


def test_quote_normal_path_does_not_touch_min(tmp_path):
    b = _broker(tmp_path)
    market = _MarketAPI()
    b.client = _Client(market=market)
    q = b.get_quote("hk00700")
    assert q.last == 300.5 and q.source == "fosun"
    assert market.calls["min"] == 0


def test_health_reports_no_sdk_modification(tmp_path):
    b = _broker(tmp_path)
    h = b.health()
    assert h["sdk_modified"] is False
    assert h["market_encryption_fallback"] is False
    assert h["http_timeout"]["read"] == 30.0


# ------------------------------------------------------------------ SDK 未被修改
def test_sdk_source_matches_audited_baseline():
    """证明"没有改第三方 SDK 后冒充官方兼容"：逐文件比对 sha256。"""
    baseline_path = ROOT / "audit_evidence" / "sdk_baseline.json"
    if not baseline_path.exists():
        pytest.skip("缺少 sdk_baseline.json")
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    sdk_root = ROOT / "sdk_src"

    drift = []
    for rel, expected in baseline["files"].items():
        p = sdk_root / rel
        if not p.exists():
            drift.append(f"缺失: {rel}")
            continue
        actual = hashlib.sha256(p.read_bytes()).hexdigest()
        if actual != expected:
            drift.append(f"被修改: {rel}")
    assert not drift, (
        "上游 SDK 与审计基线不一致：" + ", ".join(drift) +
        "。若为官方升级，请重新生成 audit_evidence/sdk_baseline.json 并在 FIX_REPORT 说明。"
    )


def test_client_still_contains_unpatched_market_branch():
    """把"未打补丁"这件事钉死：上游那行 `== "/market/"` 必须保持原样。

    我们通过"官方 GET 端点兜底"绕行，而不是偷偷改 SDK。
    """
    src = (ROOT / "sdk_src" / "fsopenapi" / "client.py").read_text(encoding="utf-8")
    assert 'is_market_path = request_path == "/market/" or "/optmarket/" in request_path' in src
