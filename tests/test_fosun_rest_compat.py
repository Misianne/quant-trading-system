"""REST 请求构造与官方源码的**字节级**对照测试。

裁判是 `tests/fosun_official_oracle.py`——一份只依据官方 `client.py` /
`crypto.py` 规则重写的参考实现，**不 import 任何生产模块**。

因此这里的断言不是"自己签自己验"，而是：
"生产代码实际发出去的字节" vs "官方规则算出来的字节"。
"""
from __future__ import annotations

import json
from urllib.parse import urlparse

import pytest

from ai_trader.market.providers import fosun as F
from ai_trader.market.providers import fosun_protocol as proto
from ai_trader.market.providers import fosun_transport as ft
try:  # pytest 以 tests/ 为 rootdir 时可直接导入
    import fosun_official_oracle as oracle
except ImportError:  # pragma: no cover
    from tests import fosun_official_oracle as oracle

API = "/api"
BASE = "https://api.example.com"
SESSION = proto.FosunSession(session_id="S-1", signing_key=b"m" * 32,
                             encryption_key=b"e" * 32, expires_at=2 ** 40)


class Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"code": 0, "data": {}}
        self.headers = {}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class CapturingHttp:
    """记录实际发出的 method/url/params/data/headers。"""

    def __init__(self, script=None):
        self.script = list(script or [])
        self.calls = []
        self.verify = True

    def request(self, method, url, **kw):
        self.calls.append({"method": method, "url": url,
                           "params": kw.get("params"), "data": kw.get("data"),
                           "headers": kw.get("headers"), "timeout": kw.get("timeout")})
        item = self.script.pop(0) if self.script else Resp()
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self):
        pass


def make(base_url=BASE, http=None, **kw):
    http = http or CapturingHttp()
    tr = ft.FosunHttpTransport(base_url, api_prefix=API,
                               session_provider=lambda: SESSION, api_key="k",
                               http_session=http, rate_per_sec=1000, burst=10,
                               sleeper=lambda s: None, **kw)
    return tr, http


def req_path_of(call, base_url=BASE):
    """从实际 URL 反推 request_path（= api_prefix + path）。"""
    return urlparse(call["url"]).path[len(urlparse(base_url).path):] \
        if urlparse(base_url).path else urlparse(call["url"]).path


def expect_signature(call, base_url=BASE, body=None):
    """按官方规则重算签名。

    ``body`` 给出时用**期望 body**（而不是实际捕获的 body）——
    这点很关键：如果用捕获值，一旦生产代码把 body 改坏（比如不发信封），
    签名仍会与"坏 body"自洽，断言就失去意义。
    """
    ts = call["headers"]["X-Timestamp"]
    nonce = call["headers"]["X-Nonce"]
    sign_path = oracle.full_sign_path(base_url, req_path_of(call, base_url))
    payload = call["data"] if body is None else body
    return oracle.sign(SESSION.signing_key, call["method"], sign_path,
                       oracle.query_string(call["params"]), ts, nonce, payload)


# ---------------------------------------------------------------------- #
# 1. GET 也必须带官方信封 body
# ---------------------------------------------------------------------- #
def test_tick_get_sends_official_envelope_body():
    tr, http = make()
    tr.request("GET", "/v1/market/secu/tick",
               params={"code": "usAAPL", "count": 50, "id": -1})

    call = http.calls[0]
    assert call["method"] == "GET"
    assert call["data"] == oracle.envelope_plain(None), \
        "GET 也必须发送 {\"encrypted\":false,\"content\":{}}"
    assert call["data"] == b'{"encrypted":false,"content":{}}'
    # query 交给 requests 编码（与官方一致），不在 URL 里手拼
    assert call["params"] == {"code": "usAAPL", "count": 50, "id": -1}
    assert "?" not in call["url"]


def test_signature_covers_the_sent_body_not_empty_string():
    """若签名覆盖的是空 body 而不是信封，这条会失败。"""
    tr, http = make()
    tr.request("GET", "/v1/market/secu/tick", params={"code": "usAAPL", "count": 50, "id": -1})
    call = http.calls[0]
    sig = call["headers"]["X-Signature"]

    assert call["data"] == oracle.envelope_plain(None)
    assert sig == expect_signature(call, body=oracle.envelope_plain(None))

    ts, nonce = call["headers"]["X-Timestamp"], call["headers"]["X-Nonce"]
    sign_path = oracle.full_sign_path(BASE, "/api/v1/market/secu/tick")
    q = oracle.query_string(call["params"])
    wrong = oracle.sign(SESSION.signing_key, "GET", sign_path, q, ts, nonce, None)
    assert wrong != sig, "空 body 的签名不应与信封 body 的签名相同"


# ---------------------------------------------------------------------- #
# 2. quote：POST + 加密信封
# ---------------------------------------------------------------------- #
def test_quote_post_is_encrypted_and_plaintext_matches_official():
    tr, http = make()
    tr.request("POST", "/v1/market/secu/quote",
               json_body={"codes": ["usAAPL"]})

    call = http.calls[0]
    assert call["method"] == "POST"

    payload = json.loads(call["data"])
    assert list(payload.keys()) == ["encrypted", "iv", "tag", "content"], \
        "加密信封的键顺序必须与官方一致"
    assert payload["encrypted"] is True, \
        "按官方实际行为（is_market_path 判定失效），行情 POST 会被加密"

    plain = oracle.decrypt_envelope(
        call["data"], enc_key=SESSION.encryption_key, session_id=SESSION.session_id,
        timestamp=call["headers"]["X-Timestamp"], nonce=call["headers"]["X-Nonce"])
    assert plain == oracle.expected_quote_body(["usAAPL"])
    assert plain == b'{"codes":["usAAPL"]}'

    assert call["headers"]["X-Signature"] == expect_signature(call)


def test_kline_post_encrypted_plaintext_matches():
    tr, http = make()
    body = {"code": "usAAPL", "ktype": "day", "num": 5}
    tr.request("POST", "/v1/market/kline", json_body=body)

    call = http.calls[0]
    plain = oracle.decrypt_envelope(
        call["data"], enc_key=SESSION.encryption_key, session_id=SESSION.session_id,
        timestamp=call["headers"]["X-Timestamp"], nonce=call["headers"]["X-Nonce"])
    assert plain == oracle.plaintext_of(body)


# ---------------------------------------------------------------------- #
# 3. full_sign_path：base_path 必须被签
# ---------------------------------------------------------------------- #
def test_base_path_is_included_in_signature():
    base = "https://api.example.com/gw"
    tr, http = make(base_url=base)
    tr.request("GET", "/v1/market/secu/tick", params={"code": "usAAPL"})

    call = http.calls[0]
    ts, nonce = call["headers"]["X-Timestamp"], call["headers"]["X-Nonce"]
    q = oracle.query_string(call["params"])
    sig = call["headers"]["X-Signature"]

    with_base = oracle.sign(SESSION.signing_key, "GET",
                            "/gw/api/v1/market/secu/tick", q, ts, nonce, call["data"])
    without_base = oracle.sign(SESSION.signing_key, "GET",
                               "/api/v1/market/secu/tick", q, ts, nonce, call["data"])

    assert sig == with_base
    assert sig != without_base, "漏掉 base_path 的签名不应通过"


def test_trailing_slash_and_no_path_are_equivalent():
    for base in ("https://api.example.com", "https://api.example.com/"):
        tr, http = make(base_url=base)
        tr.request("GET", "/v1/market/secu/tick", params={"code": "usAAPL"})
        call = http.calls[0]
        assert call["headers"]["X-Signature"] == \
            expect_signature(call, base_url=base.rstrip("/"),
                             body=oracle.envelope_plain(None))


def test_no_double_slash_in_url():
    tr, http = make(base_url="https://api.example.com/gw/")
    tr.request("GET", "/v1/market/secu/tick", params={"code": "usAAPL"})
    assert "//v1" not in http.calls[0]["url"]
    assert http.calls[0]["url"].endswith("/gw/api/v1/market/secu/tick")


# ---------------------------------------------------------------------- #
# 4. query 排序与签名
# ---------------------------------------------------------------------- #
def test_query_is_sorted_in_signature():
    tr, http = make()
    tr.request("GET", "/v1/market/secu/tick", params={"b": 2, "a": 1})
    call = http.calls[0]
    assert oracle.query_string(call["params"]) == "a=1&b=2"
    assert call["headers"]["X-Signature"] == \
        expect_signature(call, body=oracle.envelope_plain(None))


def test_query_not_mixed_into_path():
    tr, http = make()
    tr.request("GET", "/v1/market/secu/tick", params={"code": "usAAPL"})
    call = http.calls[0]
    sig_path = oracle.full_sign_path(BASE, req_path_of(call))
    assert "code=" not in sig_path


# ---------------------------------------------------------------------- #
# 5. 请求头
# ---------------------------------------------------------------------- #
def test_headers_match_official_set():
    tr, http = make()
    tr.request("GET", "/v1/market/secu/tick", params={"code": "usAAPL"})
    h = http.calls[0]["headers"]

    expected = oracle.business_headers(
        lang="zhCn", api_key="k", session_id=SESSION.session_id,
        request_id=h["X-Request-Id"], timestamp=h["X-Timestamp"],
        nonce=h["X-Nonce"], signature=h["X-Signature"],
        request_client=h["X-Request-Client"])

    assert set(h) == set(expected), "业务请求头集合必须与官方一致"
    assert "X-source" not in h and "X-product" not in h, "这两个只在握手时发"
    assert "Accept" not in h
    assert h["X-Lang"] == "zhCn"
    assert h["Content-Type"] == "application/json"


def test_request_client_is_our_own_identity():
    tr, http = make()
    tr.request("GET", "/v1/market/secu/tick", params={"code": "usAAPL"})
    # 官方是 openapi-python-sdk；本项目不冒充，用自己的标识（不参与签名）
    assert http.calls[0]["headers"]["X-Request-Client"] == "fosunai-selfhosted"


def test_handshake_headers_include_source_and_product():
    hs = oracle.handshake_headers(
        lang="zhCn", api_key="k", request_id="r", timestamp="1",
        nonce="n", signature="s", client_id="fosunai-selfhosted")
    assert hs["X-source"] == "python-sdk" and hs["X-product"] == "sdk"
    assert "X-timestamp" in hs, "官方握手用的是小写 t 的 X-timestamp"


# ---------------------------------------------------------------------- #
# 6. 序列化
# ---------------------------------------------------------------------- #
def test_json_serialization_is_byte_identical_to_official():
    tr, _ = make()
    cases = [
        {"encrypted": False, "content": {}},
        {"codes": ["usAAPL", "usMSFT"]},
        {"a": 1, "b": [1, 2], "c": {"d": None}},
        {"中文": "值"},
    ]
    for obj in cases:
        assert tr._dumps(obj) == oracle.dumps(obj), f"序列化不一致：{obj}"


def test_non_ascii_is_escaped_not_raw():
    tr, _ = make()
    got = tr._dumps({"a": "中文"})
    assert b"\\u4e2d\\u6587" in got
    assert "中文".encode("utf-8") not in got


# ---------------------------------------------------------------------- #
# 7. 重试：必须重新签名
# ---------------------------------------------------------------------- #
def test_retry_regenerates_timestamp_nonce_and_signature():
    http = CapturingHttp([Resp(502), Resp(payload={"code": 0, "data": {"ok": 1}})])
    tr, _ = make(http=http, retry=ft.RetryPolicy(max_attempts=2, base_delay=0))

    assert tr.request("GET", "/v1/market/secu/tick", params={"code": "usAAPL"}) == {"ok": 1}
    first, second = http.calls[0], http.calls[1]

    assert first["headers"]["X-Nonce"] != second["headers"]["X-Nonce"]
    assert first["headers"]["X-Signature"] != second["headers"]["X-Signature"]
    # 每一次签名都必须与当次的时间戳/nonce/**期望 body** 自洽
    env = oracle.envelope_plain(None)
    assert first["data"] == env and second["data"] == env
    assert first["headers"]["X-Signature"] == expect_signature(first, body=env)
    assert second["headers"]["X-Signature"] == expect_signature(second, body=env)


# ---------------------------------------------------------------------- #
# 8. 加密细节
# ---------------------------------------------------------------------- #
def test_encrypted_envelope_uses_12_byte_iv_and_16_byte_tag():
    import base64
    tr, http = make()
    tr.request("POST", "/v1/market/secu/quote", json_body={"codes": ["usAAPL"]})
    payload = json.loads(http.calls[0]["data"])
    assert len(base64.b64decode(payload["iv"])) == 12
    assert len(base64.b64decode(payload["tag"])) == 16


def test_aad_binds_session_timestamp_nonce():
    tr, http = make()
    tr.request("POST", "/v1/market/secu/quote", json_body={"codes": ["usAAPL"]})
    call = http.calls[0]
    with pytest.raises(Exception):
        oracle.decrypt_envelope(call["data"], enc_key=SESSION.encryption_key,
                                session_id="WRONG",
                                timestamp=call["headers"]["X-Timestamp"],
                                nonce=call["headers"]["X-Nonce"])


def test_get_requests_are_not_encrypted():
    tr, http = make()
    tr.request("GET", "/v1/market/secu/orderbook", params={"code": "usAAPL", "count": 5})
    payload = json.loads(http.calls[0]["data"])
    assert payload == {"encrypted": False, "content": {}}


# ---------------------------------------------------------------------- #
# 9. 安全边界
# ---------------------------------------------------------------------- #
def test_tls_verification_is_forced_on():
    tr, http = make()
    assert http.verify is True, "传入的 session 也必须被强制开启校验"


def test_no_secrets_leaked_to_output(tmp_path, monkeypatch, capsys, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    tr, http = make()
    tr.request("GET", "/v1/market/secu/tick", params={"code": "usAAPL"})
    captured = capsys.readouterr()
    blob = captured.out + captured.err + caplog.text
    assert SESSION.signing_key.hex() not in blob
    assert "X-Signature" not in blob
    assert "Bearer" not in blob


# ---------------------------------------------------------------------- #
# 10. Provider 走 POST（F1 端到端）
# ---------------------------------------------------------------------- #
def test_provider_snapshot_quote_uses_post():
    http = CapturingHttp([Resp(payload={"code": 0, "data": {
        "usAAPL": {"last": 201.2, "bid": 201.1, "ask": 201.3, "volume": 10}}})])
    tr, _ = make(http=http)
    prov = F.FosunProvider({"max_age_seconds": 10 ** 9}, transport=tr,
                           session_provider=lambda: SESSION)
    prov.connect()
    snap = prov.snapshot_quote("AAPL")

    call = http.calls[0]
    assert call["method"] == "POST"
    assert req_path_of(call) == "/api/v1/market/secu/quote"
    plain = oracle.decrypt_envelope(
        call["data"], enc_key=SESSION.encryption_key, session_id=SESSION.session_id,
        timestamp=call["headers"]["X-Timestamp"], nonce=call["headers"]["X-Nonce"])
    assert plain == b'{"codes":["usAAPL"]}'
    assert snap.last == 201.2 and snap.time_semantics == "unknown"


def test_provider_tick_uses_get_with_query():
    http = CapturingHttp([Resp(payload={"code": 0, "data": {"ticks": [
        {"id": 1, "time": 1794839400000, "price": 201.2, "volume": 100, "side": "B"}]}})])
    tr, _ = make(http=http)
    prov = F.FosunProvider({"max_age_seconds": 10 ** 9}, transport=tr,
                           session_provider=lambda: SESSION)
    prov.connect(); prov.subscribe(["AAPL"])
    evs = prov.poll()

    call = http.calls[0]
    assert call["method"] == "GET"
    assert req_path_of(call) == "/api/v1/market/secu/tick"
    assert call["params"]["code"] == "usAAPL"
    assert len(evs) == 1 and evs[0].exchange == "fosun"


def test_get_without_params_still_sends_empty_envelope():
    """无任何参数时，body 仍是 ``{"encrypted":false,"content":{}}``，不是 None。"""
    tr, http = make()
    tr.request("GET", "/v1/market/ping")
    call = http.calls[0]
    assert call["params"] is None
    assert call["data"] == b'{"encrypted":false,"content":{}}'
    assert call["headers"]["X-Signature"] == expect_signature(call)
