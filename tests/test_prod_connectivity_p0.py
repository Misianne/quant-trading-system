"""生产接入 P0-1 / P0-2 / P1 修复的验收测试。

覆盖范围
--------
P0-1  空字符串环境变量不得阻止保险箱凭证注入；缺失时必须 fail-closed。
P0-2  行情推送握手必须携带 ``X-Api-Key`` / ``X-Session``；认证失败不得
      误报"已连接/已订阅"。
P1    订阅必须收到服务端有效回执才算 ``SUBSCRIBED``；拒绝/超时/权限不足
      必须进入 FAILED；``settle_updates`` 必须真正控制"数据已稳定"。

安全边界
--------
全部在本机回环完成，使用**伪造的假密钥字符串**，不接触任何真实网关、
不读取真实保险箱、不产生任何真实订单。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from ai_trader.broker.fosun_adapter import FosunBroker
from ai_trader.broker.base import BrokerNotConfigured
from ai_trader.market.feed import (
    FeedAuthError,
    FeedConfig,
    FeedState,
    FeedSubscriptionError,
    MarketFeed,
)

from tests.helpers import live_cfg

#: 2026-11-17 唯一一次受控生产 WS 连接抓到的**真实订阅回执字节**（未改动）。
#: 该回执只含 ``denied: ["usAAPL"]``，是"无推送权限"的明确拒绝。
REAL_SUBREP = (Path(__file__).resolve().parent / "fixtures" / "fosun"
               / "ws_subrep_denied_20261117.json")


def _real_denied_frame() -> Dict[str, Any]:
    return json.loads(REAL_SUBREP.read_text(encoding="utf-8"))["frame"]

# --------------------------------------------------------------------------- #
# 假凭证（不是密钥，只是用于验证"有没有被传进去"的占位字符串）
# --------------------------------------------------------------------------- #
FAKE_SERVER_PUB = "-----BEGIN PUBLIC KEY-----\nFAKE-SERVER-PUBLIC-KEY\n-----END PUBLIC KEY-----"
FAKE_CLIENT_PRIV = "-----BEGIN PRIVATE KEY-----\nFAKE-CLIENT-PRIVATE-KEY\n-----END PRIVATE KEY-----"
FAKE_API_KEY = "fake-api-key-for-tests"
API_KEY_HEADER_VALUE = "header-api-key-9f3a"
SESSION_HEADER_VALUE = "session-abc-123"


class _FakeVault:
    """只实现 ``get`` 的保险箱替身（记录调用但不落盘）。"""

    def __init__(self, data: Optional[Dict[str, str]] = None):
        self.data = dict(data or {})
        self.calls: List[str] = []

    def get(self, name: str) -> Optional[str]:
        self.calls.append(name)
        return self.data.get(name)


@pytest.fixture()
def clean_env():
    """保存/恢复进程环境，避免 FosunBroker 的注入污染其他用例。"""
    keys = (
        "FSOPENAPI_SERVER_PUBLIC_KEY", "FSOPENAPI_CLIENT_PRIVATE_KEY",
        "FSOPENAPI_API_KEY", "FSOPENAPI_BASE_URL", "SDK_TYPE", "FSOPENAPI_LANG",
    )
    saved = {k: os.environ.get(k) for k in keys}
    saved_present = {k: (k in os.environ) for k in keys}
    try:
        yield
    finally:
        for k in keys:
            if saved_present[k]:
                os.environ[k] = saved[k] or ""
            else:
                os.environ.pop(k, None)


# --------------------------------------------------------------------------- #
# A. P0-1：凭证注入
# --------------------------------------------------------------------------- #
class _CapturingSDK:
    """替身 SDKClient：构造时把 environment 里的凭证快照下来。"""

    last: Dict[str, Any] = {}

    def __init__(self, **kwargs):
        _CapturingSDK.last = {
            "server_public_key": os.environ.get("FSOPENAPI_SERVER_PUBLIC_KEY"),
            "client_private_key": os.environ.get("FSOPENAPI_CLIENT_PRIVATE_KEY"),
            "api_key": kwargs.get("api_key"),
            "base_url": kwargs.get("base_url"),
            "_http_session": None,
        }
        self._http_session = None


def _patch_sdk(monkeypatch):
    import fsopenapi

    monkeypatch.setattr(fsopenapi, "SDKClient", _CapturingSDK)


def test_empty_env_strings_do_not_block_vault_injection(tmp_path, monkeypatch, clean_env):
    """P0-1 回归：``KEY=`` 的空值必须让位给保险箱里的有效凭证。"""
    for name in ("FSOPENAPI_SERVER_PUBLIC_KEY", "FSOPENAPI_CLIENT_PRIVATE_KEY",
                 "FSOPENAPI_API_KEY", "FSOPENAPI_BASE_URL"):
        monkeypatch.setenv(name, "")          # 模拟 .env 里的空键
    _patch_sdk(monkeypatch)

    vault = _FakeVault({
        "FSOPENAPI_SERVER_PUBLIC_KEY": FAKE_SERVER_PUB,
        "FSOPENAPI_CLIENT_PRIVATE_KEY": FAKE_CLIENT_PRIV,
        "FSOPENAPI_API_KEY": FAKE_API_KEY,
        "FOSUN_BASE_URL": "https://openapi.example.test",
    })
    cfg = live_cfg(tmp_path, **{"fosun.base_url": ""})
    broker = FosunBroker(cfg, vault)

    assert broker.configured is True, broker.blocked_reason
    assert broker.blocked_reason == ""
    got = _CapturingSDK.last
    assert got["server_public_key"] == FAKE_SERVER_PUB
    assert got["client_private_key"] == FAKE_CLIENT_PRIV
    assert got["api_key"] == FAKE_API_KEY
    assert got["base_url"] == "https://openapi.example.test"
    # 来源可审计，且不含值
    assert broker.health()["credential_origin"]["FSOPENAPI_CLIENT_PRIVATE_KEY"] == "vault"


def _ephemeral_pems() -> tuple[str, str]:
    """生成一对**一次性测试密钥**（仅内存，不落盘、不联网）。

    真实 ``SDKClient`` 构造时会解析 PEM，假字符串过不了；这里生成的密钥
    只用于验证"注入路径能把值送到 SDK"，与星财富生产密钥无关。
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    priv = ec.generate_private_key(ec.SECP384R1())
    pub = ec.generate_private_key(ec.SECP384R1()).public_key()
    priv_pem = priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()
    pub_pem = pub.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return pub_pem, priv_pem


def test_real_sdk_accepts_injected_credentials(tmp_path, monkeypatch, clean_env):
    """用**真实** SDKClient 验证：空 env + vault 有值 → 构造不抛异常。"""
    for name in ("FSOPENAPI_SERVER_PUBLIC_KEY", "FSOPENAPI_CLIENT_PRIVATE_KEY",
                 "FSOPENAPI_API_KEY"):
        monkeypatch.setenv(name, "")
    pub_pem, priv_pem = _ephemeral_pems()
    vault = _FakeVault({
        "FSOPENAPI_SERVER_PUBLIC_KEY": pub_pem,
        "FSOPENAPI_CLIENT_PRIVATE_KEY": priv_pem,
        "FSOPENAPI_API_KEY": FAKE_API_KEY,
    })
    cfg = live_cfg(tmp_path, **{"fosun.base_url": "https://openapi.example.test"})
    broker = FosunBroker(cfg, vault)
    assert broker.configured is True, broker.blocked_reason
    assert os.environ["FSOPENAPI_CLIENT_PRIVATE_KEY"] == priv_pem
    # 只进进程环境，不落任何文件
    assert not list(tmp_path.rglob("*private*"))
    assert not list(tmp_path.rglob("*.pem"))


def test_valid_env_wins_over_vault(tmp_path, monkeypatch, clean_env):
    """有效环境变量优先级高于保险箱（不静默改用保险箱的旧值）。"""
    monkeypatch.setenv("FSOPENAPI_SERVER_PUBLIC_KEY", "ENV-SERVER-PUB")
    monkeypatch.setenv("FSOPENAPI_CLIENT_PRIVATE_KEY", "ENV-CLIENT-PRIV")
    monkeypatch.setenv("FSOPENAPI_API_KEY", "ENV-API-KEY")
    _patch_sdk(monkeypatch)
    vault = _FakeVault({
        "FSOPENAPI_SERVER_PUBLIC_KEY": "VAULT-SERVER-PUB",
        "FSOPENAPI_CLIENT_PRIVATE_KEY": "VAULT-CLIENT-PRIV",
        "FSOPENAPI_API_KEY": "VAULT-API-KEY",
    })
    cfg = live_cfg(tmp_path, **{"fosun.base_url": "https://openapi.example.test"})
    broker = FosunBroker(cfg, vault)
    assert broker.configured is True
    assert _CapturingSDK.last["client_private_key"] == "ENV-CLIENT-PRIV"
    assert broker.health()["credential_origin"]["FSOPENAPI_CLIENT_PRIVATE_KEY"] == "env"


def test_missing_everywhere_fails_closed(tmp_path, monkeypatch, clean_env):
    """env 与 vault 都没有 → configured=False，构造 SDK 之前就挡住。"""
    for name in ("FSOPENAPI_SERVER_PUBLIC_KEY", "FSOPENAPI_CLIENT_PRIVATE_KEY",
                 "FSOPENAPI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    _patch_sdk(monkeypatch)
    _CapturingSDK.last = {}
    broker = FosunBroker(live_cfg(tmp_path), _FakeVault({}))
    assert broker.configured is False
    assert "缺少凭证" in broker.blocked_reason
    assert _CapturingSDK.last == {}, "凭证缺失时不应构造 SDK"


def test_health_never_leaks_key_material(tmp_path, monkeypatch, clean_env):
    monkeypatch.setenv("FSOPENAPI_SERVER_PUBLIC_KEY", FAKE_SERVER_PUB)
    monkeypatch.setenv("FSOPENAPI_CLIENT_PRIVATE_KEY", FAKE_CLIENT_PRIV)
    monkeypatch.setenv("FSOPENAPI_API_KEY", FAKE_API_KEY)
    _patch_sdk(monkeypatch)
    broker = FosunBroker(
        live_cfg(tmp_path, **{"fosun.base_url": "https://openapi.example.test"}),
        _FakeVault({}))
    blob = json.dumps(broker.health(), ensure_ascii=False, default=str)
    assert "FAKE-CLIENT-PRIVATE-KEY" not in blob
    assert "FAKE-SERVER-PUBLIC-KEY" not in blob
    assert FAKE_API_KEY not in blob
    assert "BEGIN PRIVATE KEY" not in blob


# --------------------------------------------------------------------------- #
# B. 本机推送服务（可控制回执/拒绝/握手 403）
# --------------------------------------------------------------------------- #
def _wait_until(fn, timeout=8.0, interval=0.02) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if fn():
            return True
        time.sleep(interval)
    return False


class PushServer:
    """可编排的本机推送服务。

    ``ack``:
        "ok"      → 回 ``{"requestid":..., "subRep": "{\"qt\":[...]}"}``
        "opaque"  → 回 ``{"requestid":..., "topic":"sub"}``（无 subRep）
        "reject"  → 回 ``errMsg``
        "partial" → 只接受部分代码
        "none"    → 不回任何回执
    ``http_status``: 非 0 时在握手阶段直接返回该状态码。
    """

    def __init__(self, ack: str = "ok", http_status: int = 0, push: bool = False):
        self.ack = ack
        self.http_status = http_status
        self.push = push
        self.conns = 0
        self.headers: List[Dict[str, str]] = []
        self.subs: List[Dict[str, Any]] = []
        self.hb_acks = 0
        self.port = 0
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop: Optional[asyncio.Event] = None

    # ---- 生命周期 ----
    def start(self) -> "PushServer":
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        assert _wait_until(lambda: self.port > 0, 5), "测试服务未启动"
        return self

    def stop(self) -> None:
        if self._loop and self._stop:
            self._loop.call_soon_threadsafe(self._stop.set)
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._main())
        finally:
            try:
                loop.close()
            except Exception:  # noqa: BLE001
                pass

    async def _main(self) -> None:
        from websockets.asyncio.server import serve

        self._stop = asyncio.Event()
        async with serve(self._handler, "127.0.0.1", 0,
                         process_request=self._process_request) as server:
            self.port = server.sockets[0].getsockname()[1]
            await self._stop.wait()

    def _process_request(self, connection, request):
        if self.http_status:
            return connection.respond(self.http_status, "rejected")
        return None

    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/msg/v1/push"

    # ---- 协议 ----
    async def _handler(self, ws) -> None:
        self.conns += 1
        hdrs = getattr(getattr(ws, "request", None), "headers", {}) or {}
        self.headers.append({
            "X-Api-Key": hdrs.get("X-Api-Key", ""),
            "X-Session": hdrs.get("X-Session", ""),
            "X-Request-Client": hdrs.get("X-Request-Client", ""),
        })
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                act = msg.get("act")
                if act == "sub":
                    items = msg.get("items") or [{}]
                    self.subs.append({"requestid": msg.get("requestid"),
                                      "items": items})
                    await self._ack(ws, msg, items[0])
                elif act == "hb":
                    self.hb_acks += 1
        except Exception:  # noqa: BLE001
            pass

    async def _ack(self, ws, msg, item) -> None:
        rid = msg.get("requestid")
        topic = item.get("topic")
        codes = list(item.get("code") or [])
        if self.ack == "none":
            return
        if self.ack == "reject":
            await ws.send(json.dumps({"requestid": rid, "errMsg": "行情权限不足"}))
            return
        if self.ack == "partial":
            await ws.send(json.dumps({
                "requestid": rid,
                "subRep": json.dumps({topic: codes[:-1]}),
            }))
            return
        if self.ack == "opaque":
            await ws.send(json.dumps({"requestid": rid, "topic": "sub"}))
            return
        if self.ack == "real_denied":
            # 逐字节重放真实生产回执（只替换 requestid 为本轮请求的 id）
            frame = dict(_real_denied_frame())
            frame["requestid"] = rid
            await ws.send(json.dumps(frame, ensure_ascii=False))
            return
        if self.ack == "official":
            # 官方 push.proto::SubRepItem 结构（scode/fcode/invalid/denied）
            await ws.send(json.dumps({
                "requestid": rid,
                "subRep": json.dumps([{"topic": topic, "scode": codes, "fcode": [],
                                       "invalid": [], "denied": []}]),
            }))
            return
        if self.ack == "official_denied":
            await ws.send(json.dumps({
                "requestid": rid,
                "subRep": json.dumps([{"topic": topic, "scode": [], "fcode": [],
                                       "invalid": [], "denied": codes}]),
            }))
            return
        await ws.send(json.dumps({
            "requestid": rid,
            "subRep": json.dumps({topic: codes}),
        }))
        if self.push:
            now = datetime.now(timezone.utc)
            await ws.send(json.dumps({
                "topic": "qt", "t": int(now.timestamp() * 1000),
                "items": [{"code": codes[0], "msg": json.dumps({
                    "lastPrice": 300.5, "prevClose": 298.0,
                    "time": int(now.timestamp() * 1000)})}],
            }))


def _feed_cfg(server: PushServer, **over) -> FeedConfig:
    base = dict(
        url=server.url(), topics=("qt",), codes=("hk00700",),
        heartbeat_timeout=3.0, stale_after=5.0,
        reconnect_initial=0.05, reconnect_max=0.2,
        max_reconnect_attempts=1,
        require_auth=True, require_subscribe_ack=True,
        subscribe_timeout=1.0,
    )
    base.update(over)
    return FeedConfig(**base)


def _provider(session: str = SESSION_HEADER_VALUE, api_key: str = API_KEY_HEADER_VALUE):
    def _p() -> Dict[str, str]:
        return {"X-Api-Key": api_key, "X-Session": session}
    return _p


# --------------------------------------------------------------------------- #
# C. P0-2：握手认证
# --------------------------------------------------------------------------- #
def test_handshake_carries_api_key_and_session():
    srv = PushServer(ack="ok").start()
    feed = MarketFeed(_feed_cfg(srv), auth_provider=_provider())
    try:
        assert feed.start(timeout=8) is True, feed.snapshot()["last_error"]
        assert feed.state == FeedState.SUBSCRIBED
        assert srv.headers, "服务端未收到任何连接"
        hdr = srv.headers[0]
        assert hdr["X-Api-Key"] == API_KEY_HEADER_VALUE
        assert hdr["X-Session"] == SESSION_HEADER_VALUE
        assert hdr["X-Request-Client"].startswith("ai-trader")
    finally:
        feed.stop()
        srv.stop()


def test_missing_auth_provider_never_connects():
    """没有认证提供者 → fail-closed：连都不连，直接 FAILED。"""
    srv = PushServer(ack="ok").start()
    feed = MarketFeed(_feed_cfg(srv))       # 故意不给 provider
    try:
        assert feed.start(timeout=5) is False
        assert feed.state == FeedState.FAILED
        assert feed.snapshot()["auth"]["failures"] >= 1
        assert "认证" in feed.snapshot()["last_error"]
        assert srv.conns == 0, "认证不可用时不允许建立连接"
    finally:
        feed.stop()
        srv.stop()


def test_incomplete_auth_rejected_before_connecting():
    srv = PushServer(ack="ok").start()
    feed = MarketFeed(_feed_cfg(srv), auth_provider=lambda: {"X-Api-Key": "", "X-Session": "s"})
    try:
        assert feed.start(timeout=5) is False
        assert feed.state == FeedState.FAILED
        assert srv.conns == 0
    finally:
        feed.stop()
        srv.stop()


def test_http_403_is_fatal_and_not_reported_as_connected(caplog):
    srv = PushServer(http_status=403).start()
    feed = MarketFeed(_feed_cfg(srv), auth_provider=_provider())
    try:
        with caplog.at_level(logging.DEBUG):
            assert feed.start(timeout=8) is False
        assert feed.state == FeedState.FAILED
        snap = feed.snapshot()
        assert snap["auth"]["failures"] >= 1
        assert "403" in snap["last_error"] or "权限" in snap["last_error"]
        assert snap["state"] != FeedState.SUBSCRIBED
        assert snap["subscribed_codes"] == 0
        # 认证值不得出现在**客户端侧**日志里。
        # 注意：本机测试服务端自身用 websockets.server 的 DEBUG 日志记录入站头，
        # 那是测试脚手架；生产不会有这个 logger。
        for rec in caplog.records:
            if rec.name.startswith("websockets.server"):
                continue
            assert API_KEY_HEADER_VALUE not in rec.getMessage(), rec.name
            assert SESSION_HEADER_VALUE not in rec.getMessage(), rec.name
    finally:
        feed.stop()
        srv.stop()


def test_snapshot_never_exposes_auth_values():
    srv = PushServer(ack="ok").start()
    feed = MarketFeed(_feed_cfg(srv), auth_provider=_provider())
    try:
        assert feed.start(timeout=8) is True
        blob = json.dumps(feed.snapshot(), ensure_ascii=False, default=str)
        assert API_KEY_HEADER_VALUE not in blob
        assert SESSION_HEADER_VALUE not in blob
    finally:
        feed.stop()
        srv.stop()


# --------------------------------------------------------------------------- #
# D. P1：订阅必须确认
# --------------------------------------------------------------------------- #
def test_subscribed_only_after_server_ack():
    srv = PushServer(ack="ok").start()
    feed = MarketFeed(_feed_cfg(srv), auth_provider=_provider())
    try:
        assert feed.start(timeout=8) is True
        snap = feed.snapshot()
        assert snap["state"] == FeedState.SUBSCRIBED
        assert snap["subscription"]["confirmed_topics"] == ["qt"]
        assert snap["subscription"]["ack_mode"] == "subRep"
        assert snap["subscribed_codes"] == 1
    finally:
        feed.stop()
        srv.stop()


def test_subscribe_preserves_code_case_and_acks_mixed_case():
    """F-WS-1：线上订阅保持调用方给出的大小写；回执比对仍大小写不敏感。

    证据：官方 ``example/push_example.py`` 订阅
    ``subscribePretMarketQuote(["usAMD","usAAPL"])``，而
    ``fsopenapi/api/market_push.py::subscribe`` 对 ``codes`` 不做任何
    lower/upper 转换 —— 代码大小写是线上语义的一部分，由服务端原样匹配。
    旧实现无条件 ``lower()`` 会把 ``usAAPL`` 发成 ``usapl``，与官方客户端的
    线上字节不一致。

    修复后必须同时满足：线上代码原样保留 **且** 服务端按原样回显时订阅仍被
    确认（比对集合走小写），否则会把"大小写不同"误判成"缺代码"。
    """
    srv = PushServer(ack="ok").start()
    feed = MarketFeed(_feed_cfg(srv, codes=("usAAPL",)), auth_provider=_provider())
    try:
        assert feed.start(timeout=8) is True, feed.snapshot()["last_error"]
        snap = feed.snapshot()
        assert snap["state"] == FeedState.SUBSCRIBED
        assert snap["subscription"]["ack_mode"] == "subRep"
        assert srv.subs, "服务端没收到订阅"
        wired = srv.subs[0]["items"][0]["code"]
        assert wired == ["usAAPL"], f"线上代码被改写：{wired}"
    finally:
        feed.stop()
        srv.stop()


def test_ack_accepts_official_subrep_item_shape():
    """F-WS-2：官方 ``push.proto::SubRepItem`` 结构必须能被确认。

    官方回执是 ``[{topic, scode(成功), fcode(失败), invalid(无效), denied(无权限)}]``，
    不是 ``{topic: [codes]}``。旧实现只认后者 → 真实回执恒为
    ``ACK_UNVERIFIED``，生产永远进不了 ``SUBSCRIBED``。
    """
    srv = PushServer(ack="official").start()
    feed = MarketFeed(_feed_cfg(srv, codes=("usAAPL",)), auth_provider=_provider())
    try:
        assert feed.start(timeout=8) is True, feed.snapshot()["last_error"]
        snap = feed.snapshot()
        assert snap["state"] == FeedState.SUBSCRIBED
        assert snap["subscription"]["ack_mode"] == "subRep"
        assert snap["subscribed_codes"] == 1
    finally:
        feed.stop()
        srv.stop()


def test_ack_official_shape_with_denied_code_fails_closed():
    """官方结构里 ``denied`` 非空 → 该代码未被接受，绝不报订阅成功。"""
    srv = PushServer(ack="official_denied").start()
    feed = MarketFeed(_feed_cfg(srv, codes=("usAAPL",)), auth_provider=_provider())
    try:
        assert feed.start(timeout=8) is False
        assert feed.state == FeedState.FAILED
        assert feed.snapshot()["subscribed_codes"] == 0
    finally:
        feed.stop()
        srv.stop()


# --------------------------------------------------------------------------- #
# F-WS-2：用**真实生产回执字节**验证门禁（2026-11-17 唯一一次受控 WS 连接）
# --------------------------------------------------------------------------- #
def test_real_production_subrep_denied_parses_to_empty_accept_set():
    """真实回执只有 ``denied`` 没有 ``scode`` → 接受集合必须是空集（不是 None）。

    None 会被上层当成"不可核实"（ACK_UNVERIFIED），空集才是"一条也没接受"，
    进而按"缺代码"明确失败 —— 两者都不许变成 SUBSCRIBED，但诊断不同。
    """
    frame = _real_denied_frame()
    parsed = MarketFeed._parse_subrep(frame["subRep"])
    assert parsed == {"qt": set()}
    # 线上也可能是同内容的 JSON 字符串，解析器必须两种都吃
    assert MarketFeed._parse_subrep(json.dumps(frame["subRep"])) == {"qt": set()}


def test_real_production_subrep_denied_is_rejected_not_subscribed():
    """真实拒绝回执 + requestid 命中 → 必须明确失败，绝不确认订阅。"""
    frame = _real_denied_frame()
    feed = MarketFeed(FeedConfig(url="ws://127.0.0.1:0/x", codes=("usAAPL",),
                                 require_auth=False, require_subscribe_ack=False))
    pending = {frame["requestid"]: ("qt", {"usapl"})}
    with pytest.raises(FeedSubscriptionError) as ei:
        feed._ack_verdict(frame, pending)
    assert "未被全部接受" in str(ei.value)
    assert feed.snapshot()["subscribed_codes"] == 0


def test_real_production_subrep_envelope_time_is_microseconds():
    """回执 ``time`` 是 UNIX **微秒**（官方 push.proto 明确标注 us）。

    1794911830383098 us 必须解出 2026-11-17T10:37:10.383098Z，且晚于本端
    建连时刻 2026-11-17T10:37:10.357609Z —— 两条独立时钟上的次序仍然成立。
    """
    frame = _real_denied_frame()
    got = datetime.fromtimestamp(frame["time"] / 1_000_000, tz=timezone.utc)
    assert got.isoformat() == "2026-11-17T10:37:10.383098+00:00"
    connected = datetime.fromisoformat("2026-11-17T10:37:10.357609+00:00")
    assert 0 <= (got - connected).total_seconds() < 1.0


def test_real_production_denied_ack_makes_feed_fail_closed():
    """端到端重放真实拒绝回执：必须 FAILED，且诊断明确指出"未被全部接受"。

    这是本轮最重要的一条：生产确实回了回执（不是超时），回执里
    ``denied: ["usAAPL"]`` —— 门禁必须把它判成失败，绝不能因为
    "requestid 对上了" 就进 ``SUBSCRIBED``。
    """
    srv = PushServer(ack="real_denied").start()
    feed = MarketFeed(_feed_cfg(srv, codes=("usAAPL",)), auth_provider=_provider())
    try:
        assert feed.start(timeout=8) is False
        snap = feed.snapshot()
        assert snap["state"] == FeedState.FAILED
        assert "未被全部接受" in snap["last_error"], snap["last_error"]
        assert snap["subscribed_codes"] == 0
    finally:
        feed.stop()
        srv.stop()


def test_no_ack_times_out_and_never_subscribes():
    srv = PushServer(ack="none").start()
    feed = MarketFeed(_feed_cfg(srv, subscribe_timeout=0.5), auth_provider=_provider())
    try:
        assert feed.start(timeout=8) is False
        assert feed.state == FeedState.FAILED
        snap = feed.snapshot()
        assert snap["subscription"]["failures"] >= 1
        assert "超时" in snap["last_error"]
        assert snap["state"] != FeedState.SUBSCRIBED
        assert snap["subscribed_codes"] == 0
    finally:
        feed.stop()
        srv.stop()


def test_subscription_rejected_is_fatal():
    srv = PushServer(ack="reject").start()
    feed = MarketFeed(_feed_cfg(srv), auth_provider=_provider())
    try:
        assert feed.start(timeout=8) is False
        assert feed.state == FeedState.FAILED
        assert "拒绝" in feed.snapshot()["last_error"]
        assert feed.snapshot()["subscription"]["failures"] >= 1
    finally:
        feed.stop()
        srv.stop()


def test_partial_subscription_is_rejected():
    """只接受部分代码 = 未被全部接受 → 不能进 SUBSCRIBED。"""
    srv = PushServer(ack="partial").start()
    feed = MarketFeed(_feed_cfg(srv, codes=("hk00700", "usaapl")), auth_provider=_provider())
    try:
        assert feed.start(timeout=8) is False
        assert feed.state == FeedState.FAILED
        assert "未被全部接受" in feed.snapshot()["last_error"]
    finally:
        feed.stop()
        srv.stop()


def test_opaque_ack_with_matching_requestid_is_unverified():
    """C-1（Batch 4 收紧）：无 subRep 但 requestid 命中 → ACK_UNVERIFIED。

    Batch 3 之前这里被当作"确认"（ack_mode=requestid）。但 requestid 只能证明
    "服务端收到了这个请求"，证明不了任何代码被接受；只带 requestid 的回执
    必须归为"无法核实"，绝不进入 SUBSCRIBED。
    """
    srv = PushServer(ack="opaque").start()
    feed = MarketFeed(_feed_cfg(srv), auth_provider=_provider())
    try:
        assert feed.start(timeout=8) is False
        snap = feed.snapshot()
        assert snap["state"] == FeedState.ACK_UNVERIFIED
        assert snap["subscribed_codes"] == 0
    finally:
        feed.stop()
        srv.stop()


# --------------------------------------------------------------------------- #
# E. P1：settle_updates 真正生效
# --------------------------------------------------------------------------- #
def _quote(symbol: str, last: float, ts: datetime) -> Dict[str, Any]:
    return {"topic": "qt", "items": [{
        "code": symbol,
        "msg": json.dumps({"lastPrice": last, "prevClose": 298.0,
                           "time": ts.isoformat()})}]}


def test_settle_updates_requires_configured_number_of_ticks():
    f = MarketFeed(FeedConfig(url="ws://127.0.0.1:1/x", codes=("hk00700",),
                              settle_updates=3, require_auth=False))
    f._set_state(FeedState.SUBSCRIBED)      # 只验证 settle 逻辑，不建连
    base = datetime.now(timezone.utc)
    # 第 1、2 条：接受但不 settle
    for i in (0, 1):
        f.ingest(_quote("hk00700", 300 + i, base.replace(microsecond=i * 1000)))
    assert f.snapshot()["settled"] == []
    ok, why = f.is_fresh("hk00700")
    assert ok is False and "settle" in why
    # 第 3 条：达到阈值
    f.ingest(_quote("hk00700", 302, base.replace(microsecond=3000)))
    assert f.snapshot()["settled"] == ["hk00700"]
    assert f.snapshot()["settle"]["counts"]["hk00700"] == 3
    assert f.is_fresh("hk00700")[0] is True


def test_settle_updates_default_one_settles_immediately():
    f = MarketFeed(FeedConfig(url="ws://127.0.0.1:1/x", codes=("hk00700",),
                              require_auth=False))
    f._set_state(FeedState.SUBSCRIBED)
    f.ingest(_quote("hk00700", 300.0, datetime.now(timezone.utc)))
    assert f.snapshot()["settled"] == ["hk00700"]


# --------------------------------------------------------------------------- #
# G. 生产路径（/api vs /api/ops）
# --------------------------------------------------------------------------- #
def test_ops_sdk_type_is_reported_as_routing_conflict(tmp_path, monkeypatch, clean_env):
    """个人接口 + SDK_TYPE=ops → 必须报冲突并拒绝建连，不得静默发请求。"""
    monkeypatch.setenv("FSOPENAPI_SERVER_PUBLIC_KEY", FAKE_SERVER_PUB)
    monkeypatch.setenv("FSOPENAPI_CLIENT_PRIVATE_KEY", FAKE_CLIENT_PRIV)
    monkeypatch.setenv("FSOPENAPI_API_KEY", FAKE_API_KEY)
    _patch_sdk(monkeypatch)
    cfg = live_cfg(tmp_path, **{"fosun.base_url": "https://openapi.example.test",
                                "fosun.sdk_type": "ops"})
    broker = FosunBroker(cfg, _FakeVault({}))
    health = broker.health()
    assert health["api_prefix"] == "/api/ops"
    assert "配置冲突" in health["routing_issue"]
    with pytest.raises(BrokerNotConfigured) as ei:
        broker.connect()
    assert "配置冲突" in str(ei.value)


def test_personal_sdk_type_has_no_routing_conflict(tmp_path, monkeypatch, clean_env):
    monkeypatch.setenv("FSOPENAPI_SERVER_PUBLIC_KEY", FAKE_SERVER_PUB)
    monkeypatch.setenv("FSOPENAPI_CLIENT_PRIVATE_KEY", FAKE_CLIENT_PRIV)
    monkeypatch.setenv("FSOPENAPI_API_KEY", FAKE_API_KEY)
    _patch_sdk(monkeypatch)
    cfg = live_cfg(tmp_path, **{"fosun.base_url": "https://openapi.example.test",
                                "fosun.sdk_type": ""})
    broker = FosunBroker(cfg, _FakeVault({}))
    health = broker.health()
    assert health["api_prefix"] == "/api"
    assert health["routing_issue"] == ""


# --------------------------------------------------------------------------- #
# H. 配置默认值必须偏安全
# --------------------------------------------------------------------------- #
def test_feed_config_defaults_require_auth_and_ack():
    cfg = FeedConfig.from_cfg({"url": "wss://openapi-push.example/msg/v1/push"})
    assert cfg.require_auth is True
    assert cfg.require_subscribe_ack is True
    assert cfg.subscribe_timeout > 0


# --------------------------------------------------------------------------- #
# F. 重连：重新取 Session + 重新订阅 + 重置 settle
# --------------------------------------------------------------------------- #
def test_reconnect_refreshes_session_and_resubscribes():
    srv = PushServer(ack="ok").start()
    sessions: List[str] = []

    def provider() -> Dict[str, str]:
        # 每次建连都给一个新 Session，用来证明"没有缓存旧 Session"
        sid = f"session-{len(sessions) + 1}"
        sessions.append(sid)
        return {"X-Api-Key": API_KEY_HEADER_VALUE, "X-Session": sid}

    feed = MarketFeed(_feed_cfg(srv, heartbeat_timeout=0.5,
                                max_reconnect_attempts=3), auth_provider=provider)
    try:
        assert feed.start(timeout=8) is True
        assert len(srv.subs) >= 1
        # 服务端不再发任何消息 → 心跳超时 → 重连
        srv.ack = "none"
        assert _wait_until(lambda: srv.conns >= 2, 8), "未触发重连"
        assert _wait_until(lambda: len(sessions) >= 2, 8), "重连未重新获取 Session"
        assert len(set(sessions)) >= 2, "Session 被缓存复用"
        # 订阅报文到达服务端晚于 provider 调用（异步），这里必须有界等待，
        # 否则是"客户端刚取完 Session"就断言，属于测试自身竞态。
        assert _wait_until(lambda: len(srv.subs) >= 2, 5), "重连后没有重新订阅"
        assert srv.headers[-1]["X-Session"] == sessions[-1]
    finally:
        feed.stop()
        srv.stop()
