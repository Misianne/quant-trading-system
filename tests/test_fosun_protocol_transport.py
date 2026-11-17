"""协议层与传输层测试（纯离线，无网络、无凭据）。

覆盖：签名原文、HKDF 派生、AES-GCM 往返、握手验签、令牌桶、重试策略、
重连退避、订阅恢复清单、缺口检测、监督循环。
"""
from __future__ import annotations

import asyncio
import base64
import json

import pytest

from ai_trader.market.providers import fosun_protocol as proto
from ai_trader.market.providers import fosun_transport as ft
from ai_trader.market.providers.fosun_transport import (
    GapDetector, ReconnectPolicy, RetryPolicy, SubscriptionRegistry, TokenBucket)


# ---------------------------------------------------------------------- #
# 签名
# ---------------------------------------------------------------------- #
def test_canonical_query_is_sorted_and_stable():
    assert proto.canonical_query({"b": 2, "a": 1}) == "a=1&b=2"
    assert proto.canonical_query({}) == ""
    assert proto.canonical_query(None) == ""


def test_canonical_request_shape():
    text = proto.canonical_request("get", "/api/v1/market/secu/tick",
                                   "code=usAAPL", "1700000000000", "n1", "abc")
    assert text.split("\n") == ["GET", "/api/v1/market/secu/tick",
                               "code=usAAPL", "1700000000000", "n1", "abc"]


def test_signature_is_deterministic_and_verifies():
    key = b"k" * 32
    kw = dict(method="GET", path="/api/v1/x", query="a=1",
              timestamp="1700000000000", nonce="n", body=b"{}")
    sig = proto.sign_request(key, **kw)
    assert proto.verify_request(key, sig, **kw) is True
    # 任何一项变化都必须导致验签失败
    assert proto.verify_request(key, sig, **{**kw, "method": "POST"}) is False
    assert proto.verify_request(key, sig, **{**kw, "body": b'{"x":1}'}) is False
    assert proto.verify_request(key, sig, **{**kw, "nonce": "n2"}) is False


def test_signature_changes_with_key():
    args = ("GET", "/p", "", "1", "n", None)
    a = proto.sign_request(b"a" * 32, *args)
    b = proto.sign_request(b"b" * 32, *args)
    assert a != b


def test_body_hash_of_empty_body():
    assert proto.body_sha256_hex(None) == proto.body_sha256_hex(b"")


# ---------------------------------------------------------------------- #
# 密钥派生
# ---------------------------------------------------------------------- #
def test_hkdf_is_deterministic_and_splits_two_keys():
    alice, alice_pub = proto.generate_ephemeral_keypair()
    bob, bob_pub = proto.generate_ephemeral_keypair()
    cn, sn = proto.new_nonce(), proto.new_nonce()

    mac_a, enc_a = proto.derive_session_keys(alice, bob_pub, cn, sn)
    # 对端用同一共享密钥、同一 nonce 顺序，必须得到相同的两个 key
    mac_b, enc_b = proto.derive_session_keys(bob, alice_pub, cn, sn)

    assert mac_a == mac_b and enc_a == enc_b
    assert len(mac_a) == 32 and len(enc_a) == 32
    assert mac_a != enc_a, "签名密钥与加密密钥必须不同"


def test_hkdf_depends_on_nonce_order():
    alice, alice_pub = proto.generate_ephemeral_keypair()
    bob, bob_pub = proto.generate_ephemeral_keypair()
    cn, sn = proto.new_nonce(), proto.new_nonce()
    a = proto.derive_session_keys(alice, bob_pub, cn, sn)
    b = proto.derive_session_keys(alice, bob_pub, sn, cn)
    assert a != b


# ---------------------------------------------------------------------- #
# 加解密
# ---------------------------------------------------------------------- #
def test_aes_gcm_roundtrip_and_aad_binding():
    key = b"e" * 32
    aad = proto.build_aad("S1", "1700000000000", "N1")
    iv, ct, tag = proto.encrypt_body(key, b'{"a":1}', aad)
    assert proto.decrypt_body(key, iv, ct, tag, aad) == b'{"a":1}'

    wrong = proto.build_aad("S2", "1700000000000", "N1")
    with pytest.raises(proto.FosunProtocolError):
        proto.decrypt_body(key, iv, ct, tag, wrong)


# ---------------------------------------------------------------------- #
# 握手签名
# ---------------------------------------------------------------------- #
def test_handshake_sign_and_verify():
    from cryptography.hazmat.primitives.asymmetric import ec
    priv = ec.generate_private_key(ec.SECP384R1())
    pub_pem = priv.public_key().public_bytes(
        encoding=__import__("cryptography.hazmat.primitives.serialization",
                            fromlist=["Encoding"]).Encoding.PEM,
        format=__import__("cryptography.hazmat.primitives.serialization",
                          fromlist=["PublicFormat"]).PublicFormat.SubjectPublicKeyInfo,
    ).decode()

    _, eph_pub = proto.generate_ephemeral_keypair()
    nonce = proto.new_nonce()
    sig = proto.sign_handshake(priv, eph_pub, nonce)

    verifier = proto.load_public_key_pem(pub_pem)
    assert proto.verify_handshake(verifier, eph_pub, nonce, sig) is True
    assert proto.verify_handshake(verifier, eph_pub, proto.new_nonce(), sig) is False


def test_bad_pem_is_rejected():
    with pytest.raises(proto.FosunAuthError):
        proto.load_private_key_pem("not a key")
    with pytest.raises(proto.FosunAuthError):
        proto.load_public_key_pem("-----BEGIN PUBLIC KEY-----\nxx\n-----END PUBLIC KEY-----")


# ---------------------------------------------------------------------- #
# 令牌桶
# ---------------------------------------------------------------------- #
class _FakeTime:
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def clock(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def test_token_bucket_blocks_within_window():
    ft_ = _FakeTime()
    bucket = TokenBucket(1.0, 2, clock=ft_.clock, sleeper=ft_.sleep)
    assert bucket.acquire(timeout=0) is True
    assert bucket.acquire(timeout=0) is True
    assert bucket.acquire(timeout=0) is False, "突发用尽后应立即拒绝"


def test_token_bucket_refills_over_time():
    ft_ = _FakeTime()
    bucket = TokenBucket(1.0, 1, clock=ft_.clock, sleeper=ft_.sleep)
    assert bucket.acquire(timeout=0) is True
    assert bucket.acquire(timeout=0) is False
    ft_.t += 1.0                      # 过一秒
    assert bucket.acquire(timeout=0) is True


# ---------------------------------------------------------------------- #
# 重试策略
# ---------------------------------------------------------------------- #
def test_retry_backoff_sequence():
    rp = RetryPolicy(max_attempts=3, base_delay=0.5, max_delay=4.0)
    assert [rp.delay_for(i) for i in (1, 2, 3)] == [0.5, 1.0, 2.0]


def test_permission_and_auth_are_not_retryable():
    rp = RetryPolicy()
    assert rp.retryable(proto.FosunPermissionError("403")) is False
    assert rp.retryable(proto.FosunAuthError("401")) is False
    assert rp.retryable(proto.FosunRequestError("70001")) is False
    assert rp.retryable(proto.FosunUpstreamError("502")) is True
    assert rp.retryable(proto.FosunProtocolError("解密失败")) is False


# ---------------------------------------------------------------------- #
# 重连 / 订阅恢复 / 缺口
# ---------------------------------------------------------------------- #
def test_reconnect_policy_is_bounded():
    rp = ReconnectPolicy(base_delay=1.0, max_delay=8.0, max_attempts=4)
    assert [rp.delay_for(i) for i in (1, 2, 3, 4)] == [1.0, 2.0, 4.0, 8.0]
    assert rp.delay_for(5) is None, "超过上限必须放弃，不能无限重连"


def test_subscription_registry_replays_all_topics():
    reg = SubscriptionRegistry()
    reg.add("tk", ["usAAPL", "usMSFT"])
    reg.add("qt", ["usAAPL"])
    payloads = reg.payloads()
    assert [p["items"][0]["topic"] for p in payloads] == ["qt", "tk"]
    assert payloads[1]["items"][0]["code"] == ["usAAPL", "usMSFT"]
    reg.remove("qt")
    assert reg.topics() == ["tk"]


def test_gap_detector_flags_large_gap_only():
    g = GapDetector(max_gap_seconds=5.0)
    assert g.observe(100.0) == 0.0
    assert g.observe(103.0) == pytest.approx(3.0)
    assert g.gaps_detected == 0
    gap = g.observe(120.0)
    assert gap == pytest.approx(17.0)
    assert g.gaps_detected == 1
    assert g.max_gap_seen == pytest.approx(17.0)


def test_gap_detector_detects_stall():
    g = GapDetector(max_gap_seconds=5.0)
    g.observe(100.0)
    assert g.stalled(101.0) is False
    assert g.stalled(200.0) is True
    g.reset()
    assert g.stalled(200.0) is True, "无事件时应视为停滞"


# ---------------------------------------------------------------------- #
# 监督循环
# ---------------------------------------------------------------------- #
class _FakeWS:
    """先吐 n 条消息再断开的假 ws。"""

    def __init__(self, messages, *, close_after=True):
        self._messages = list(messages)
        self._close_after = close_after
        self.sent = []
        self.closed = False

    async def send(self, payload):
        self.sent.append(json.loads(payload))

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for m in self._messages:
            yield json.dumps(m)
        raise proto.FosunUpstreamError("模拟断开")

    async def close(self):
        self.closed = True


def test_stream_loop_reconnects_and_resubscribes():
    created = []

    async def connect():
        ws = _FakeWS([{"topic": "tk", "n": len(created)}])
        created.append(ws)
        return ws

    reg = SubscriptionRegistry()
    reg.add("tk", ["usAAPL"])

    seen = []
    stats = asyncio.run(ft.run_stream_loop(
        connect, reg, ReconnectPolicy(base_delay=0.0, max_delay=0.0, max_attempts=4),
        lambda m: seen.append(m),
        sleeper=_no_sleep, max_messages=3))

    assert len(created) == 3, "每次断开都应重连"
    assert all(ws.sent for ws in created), "每次重连都必须重放订阅"
    assert created[0].sent[0]["items"][0]["topic"] == "tk"
    assert len(seen) == 3
    assert stats["reconnects"] == 2


def test_stream_loop_gives_up_after_max_attempts():
    async def connect():
        raise proto.FosunUpstreamError("连不上")

    reg = SubscriptionRegistry()
    reg.add("tk", ["usAAPL"])
    with pytest.raises(proto.FosunUpstreamError):
        asyncio.run(ft.run_stream_loop(
            connect, reg, ReconnectPolicy(base_delay=0.0, max_delay=0.0, max_attempts=2),
            lambda m: None, sleeper=_no_sleep))


async def _no_sleep(_seconds):
    return None


# ---------------------------------------------------------------------- #
# HTTP 传输：超时 / 重试 / 缩圈
# ---------------------------------------------------------------------- #
class _Resp:
    def __init__(self, status_code=200, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"code": 0, "data": {}}
        self.text = text
        self.headers = headers or {}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeHttp:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.verify = True

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        item = self.script.pop(0) if self.script else _Resp()
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self):
        pass


SESSION = proto.FosunSession(session_id="S1", signing_key=b"m" * 32,
                             encryption_key=b"e" * 32,
                             expires_at=2 ** 40)


def _transport(script, **kw):
    return ft.FosunHttpTransport(
        "https://example.invalid", session_provider=lambda: SESSION,
        api_key="k", http_session=_FakeHttp(script),
        rate_per_sec=1000.0, burst=10,
        sleeper=lambda s: None, **kw)


def test_transport_signs_and_unwraps_data():
    http = _FakeHttp([_Resp(payload={"code": 0, "data": {"ticks": []}})])
    tr = ft.FosunHttpTransport("https://example.invalid",
                               session_provider=lambda: SESSION, api_key="k",
                               http_session=http, rate_per_sec=1000, burst=10,
                               sleeper=lambda s: None)
    data = tr.request("GET", "/v1/market/secu/tick", params={"code": "usAAPL"})
    assert data == {"ticks": []}
    method, url, kwargs = http.calls[0]
    assert method == "GET" and "code=usAAPL" in url
    assert kwargs["timeout"] == tr.timeout
    assert kwargs["headers"]["X-Signature"], "必须带签名头"
    assert "X-API-Key" in kwargs["headers"]


def test_transport_retries_upstream_5xx_then_succeeds():
    http = _FakeHttp([_Resp(status_code=502), _Resp(payload={"code": 0, "data": {"ok": 1}})])
    tr = ft.FosunHttpTransport("https://x.invalid", session_provider=lambda: SESSION,
                               api_key="k", http_session=http, rate_per_sec=1000,
                               burst=10, sleeper=lambda s: None,
                               retry=RetryPolicy(max_attempts=3, base_delay=0))
    assert tr.request("GET", "/p") == {"ok": 1}
    assert tr.stats["retries"] == 1


def test_transport_does_not_retry_permission_error():
    http = _FakeHttp([_Resp(status_code=403), _Resp(payload={"code": 0, "data": {}})])
    tr = ft.FosunHttpTransport("https://x.invalid", session_provider=lambda: SESSION,
                               api_key="k", http_session=http, rate_per_sec=1000,
                               burst=10, sleeper=lambda s: None,
                               retry=RetryPolicy(max_attempts=3, base_delay=0))
    with pytest.raises(proto.FosunPermissionError):
        tr.request("GET", "/p")
    assert len(http.calls) == 1, "权限错误不得重试"


def test_transport_raises_on_timeout_without_infinite_loop():
    import requests
    http = _FakeHttp([requests.Timeout("timeout")] * 5)
    tr = ft.FosunHttpTransport("https://x.invalid", session_provider=lambda: SESSION,
                               api_key="k", http_session=http, rate_per_sec=1000,
                               burst=10, sleeper=lambda s: None,
                               retry=RetryPolicy(max_attempts=2, base_delay=0))
    with pytest.raises(requests.Timeout):
        tr.request("GET", "/p")
    assert len(http.calls) == 2
    assert tr.stats["timeouts"] == 2


def test_transport_maps_business_codes():
    for code, exc_type in ((70001, proto.FosunRequestError),
                           (70002, proto.FosunUpstreamError),
                           (70003, proto.FosunPermissionError)):
        http = _FakeHttp([_Resp(payload={"code": code, "message": "m"})])
        tr = ft.FosunHttpTransport("https://x.invalid", session_provider=lambda: SESSION,
                                   api_key="k", http_session=http, rate_per_sec=1000,
                                   burst=10, sleeper=lambda s: None,
                                   retry=RetryPolicy(max_attempts=1))
        with pytest.raises(exc_type):
            tr.request("GET", "/p")


def test_transport_decrypts_encrypted_response():
    aad_holder = {}

    class _Session(proto.FosunSession):
        pass

    http = None

    def _make(iv, ct, tag):
        return _Resp(payload={"encrypted": True, "iv": iv, "content": ct, "tag": tag})

    # 先用一次正常调用抓取 timestamp/nonce，无法预知 → 用固定 session 直接构造
    key = SESSION.encryption_key
    # 构造响应时 AAD 依赖请求里的 ts/nonce，所以让 fake http 在 request 时动态生成
    class _DynamicHttp(_FakeHttp):
        def request(self, method, url, **kwargs):
            headers = kwargs["headers"]
            aad = proto.build_aad(SESSION.session_id, headers["X-Timestamp"],
                                  headers["X-Nonce"])
            iv, ct, tag = proto.encrypt_body(key, b'{"code":0,"data":{"x":1}}', aad)
            self.calls.append((method, url, kwargs))
            return _make(iv, ct, tag)

    tr = ft.FosunHttpTransport("https://x.invalid", session_provider=lambda: SESSION,
                               api_key="k", http_session=_DynamicHttp([]),
                               rate_per_sec=1000, burst=10, sleeper=lambda s: None)
    assert tr.request("GET", "/p") == {"x": 1}


def test_transport_rejects_undecryptable_body():
    http = _FakeHttp([_Resp(payload={"encrypted": True, "iv": "AA==",
                                     "content": "AA==", "tag": "AA=="})])
    tr = ft.FosunHttpTransport("https://x.invalid", session_provider=lambda: SESSION,
                               api_key="k", http_session=http, rate_per_sec=1000,
                               burst=10, sleeper=lambda s: None,
                               retry=RetryPolicy(max_attempts=1))
    with pytest.raises(proto.FosunProtocolError):
        tr.request("GET", "/p")
