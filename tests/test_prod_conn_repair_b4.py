"""US-P1 Batch 4 验收测试：A-1 / C-1 / B-1 / D-1 / F-2。

对应 `US_P1_BATCH3_INDEPENDENT_REVIEW.md` 的结论项：

* A-1 ``build_feed_auth_provider`` 绕过门禁与路由校验 → 仍会发 SessionCreate；
* C-1 只带 requestid 的回执被当成订阅成功 → 必须 ``ACK_UNVERIFIED``；
* B-1 多实例共享 ``os.environ`` 的凭证所有权竞争；
* D-1 正常关闭被计入 ``errors`` / 写进 ``last_error``；
* F-2 DEBUG 级别泄露认证头（原来靠把 ``feed.ws`` 钉在 INFO 掩盖）。

全部使用真实本机回环 WebSocket 服务、真实 ``os.environ``、真实日志 handler；
券商侧用"实现完整契约的假 SDK + 调用计数器"，**不用 mock 打桩**。
所有凭证均为占位假串，不含任何真实密钥。
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import pathlib
import sys
import threading
import time
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from ai_trader.broker.base import BrokerNotConfigured  # noqa: E402
from ai_trader.broker.fosun_adapter import (  # noqa: E402
    FosunBroker,
    _CRED_ENV,
)
from ai_trader.market import feed as feed_mod  # noqa: E402
from ai_trader.market.feed import (  # noqa: E402
    FeedConfig,
    FeedState,
    MarketFeed,
)
from ai_trader.runtime import build_feed_auth_provider  # noqa: E402

from tests.helpers import live_cfg  # noqa: E402

FAKE_PUB = "-----BEGIN PUBLIC KEY-----\nB4-FAKE-SERVER-PUB\n-----END PUBLIC KEY-----"
FAKE_PUB2 = "-----BEGIN PUBLIC KEY-----\nB4-OTHER-SERVER-PUB\n-----END PUBLIC KEY-----"
FAKE_PRIV = "-----BEGIN PRIVATE KEY-----\nB4-FAKE-CLIENT-PRIV\n-----END PRIVATE KEY-----"
FAKE_AK = "b4-fake-api-key-0001"
HDR_AK = "b4-hdr-api-key-1234567890"
HDR_SESSION = "b4-hdr-session-abcdefgh"

ENV_KEYS = ("FSOPENAPI_SERVER_PUBLIC_KEY", "FSOPENAPI_CLIENT_PRIVATE_KEY",
            "FSOPENAPI_API_KEY", "FSOPENAPI_BASE_URL", "SDK_TYPE",
            "FSOPENAPI_LANG")


def _wait_until(fn, timeout: float = 5.0, interval: float = 0.02) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(interval)
    return False


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
@pytest.fixture
def clean_env(monkeypatch):
    for k in ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    yield


@pytest.fixture(autouse=True)
def isolated_cred_registry():
    """进程级注册表是全局单例：每个用例前后都清空，避免跨用例串味。

    ``clear()`` 只清注册表，不动 ``os.environ``；环境变量由 ``clean_env``
    或用例自己负责，二者组合保证用例之间完全独立。
    """
    _CRED_ENV.clear()
    yield
    _CRED_ENV.clear()


class _Counter:
    def __init__(self) -> None:
        self.calls: List[str] = []

    def add(self, name: str) -> None:
        self.calls.append(name)

    def count(self, prefix: str = "") -> int:
        return sum(1 for c in self.calls if c.startswith(prefix))


class _Auth:
    """假 auth_manager：``get_valid_session`` 计数（真实 SDK 里这会发 SessionCreate）。"""

    def __init__(self, counter: _Counter, api_key: str = FAKE_AK) -> None:
        self.api_key = api_key
        self._counter = counter

    def get_valid_session(self):
        self._counter.add("auth.get_valid_session")
        return ("SESSION-FAKE", "sk", "ek")


def _make_fake_sdk(counter: _Counter, *, api_key: str = FAKE_AK,
                   raise_on_init: Optional[BaseException] = None):
    class _Node:
        def __init__(self, name: str) -> None:
            self._name = name

        def __getattr__(self, attr: str):
            def _call(*a: Any, **kw: Any):
                counter.add(f"{self._name}.{attr}")
                return {"code": 0, "data": {"list": []}}
            return _call

    class _FakeSDK:
        def __init__(self, **kw: Any) -> None:
            counter.add("SDKClient.__init__")
            if raise_on_init is not None:
                raise raise_on_init
            self._http_session = type("_S", (), {"request": lambda *a, **k: None})()
            for name in ("trade", "portfolio", "account", "market", "session"):
                object.__setattr__(self, name, _Node(name))
            self.auth_manager = _Auth(counter, api_key)

        def dump_session(self) -> Dict[str, Any]:
            counter.add("dump_session")
            return {"sessionId": "SESSION-FAKE"}

    return _FakeSDK


@pytest.fixture
def patch_sdk(monkeypatch):
    def _install(counter, **kw):
        import fsopenapi
        monkeypatch.setattr(fsopenapi, "SDKClient", _make_fake_sdk(counter, **kw),
                            raising=False)
    return _install


class _Vault:
    def __init__(self, data: Optional[Dict[str, str]] = None) -> None:
        self.data = data or {}

    def get(self, name: str, default: Optional[str] = None) -> Optional[str]:
        return self.data.get(name, default)


def _set_env(monkeypatch, *, server_pub: str = FAKE_PUB) -> None:
    monkeypatch.setenv("FSOPENAPI_SERVER_PUBLIC_KEY", server_pub)
    monkeypatch.setenv("FSOPENAPI_CLIENT_PRIVATE_KEY", FAKE_PRIV)
    monkeypatch.setenv("FSOPENAPI_API_KEY", FAKE_AK)


# =========================================================================== #
# A-1：provider 创建必须先过门禁与路由（0 次 SessionCreate）
# =========================================================================== #
def test_provider_not_created_when_gate_closed(tmp_path, monkeypatch,
                                               clean_env, patch_sdk):
    """门禁关闭：不创建 provider，不构造 SDK，不取 Session。"""
    counter = _Counter()
    patch_sdk(counter)
    _set_env(monkeypatch)
    cfg = live_cfg(tmp_path, **{"safety.paper_trading": True,
                               "fosun.base_url": "https://x.test"})
    broker = FosunBroker(cfg, _Vault({}))

    provider = build_feed_auth_provider(broker)

    assert provider is None, "门禁关闭时必须不创建 provider"
    assert counter.count("SDKClient.__init__") == 0
    assert counter.count("auth.get_valid_session") == 0, "不得发起 SessionCreate"


def test_provider_not_created_on_routing_conflict(tmp_path, monkeypatch,
                                                  clean_env, patch_sdk):
    """A-1 复现：ops 前缀 + 个人接口 → provider 不创建、0 次 SessionCreate。

    旧实现直接摸 ``client.auth_manager.get_valid_session()``，
    SDK 在真实环境里会 ``POST /api/ops/v1/auth/SessionCreate``。
    """
    counter = _Counter()
    patch_sdk(counter)
    _set_env(monkeypatch)
    cfg = live_cfg(tmp_path, **{"fosun.base_url": "https://x.test",
                               "fosun.sdk_type": "ops"})
    broker = FosunBroker(cfg, _Vault({}))
    assert broker.client is not None, "构造阶段不拦（只在入口处拦）"
    assert broker.routing_issue(), "该组合必须被判定为路由冲突"

    provider = build_feed_auth_provider(broker)

    assert provider is None
    assert counter.count("auth.get_valid_session") == 0, (
        f"路由冲突下不得取 Session（实际调用：{counter.calls}）")


def test_provider_ready_returns_headers_and_rechecks_gate(tmp_path, monkeypatch,
                                                          clean_env, patch_sdk):
    """就绪时返回可用头；门禁变化后 provider 立刻拒绝（纵深防御）。"""
    counter = _Counter()
    patch_sdk(counter)
    _set_env(monkeypatch)
    cfg = live_cfg(tmp_path, **{"fosun.base_url": "https://x.test"})
    broker = FosunBroker(cfg, _Vault({}))

    provider = build_feed_auth_provider(broker)
    assert provider is not None
    headers = provider()
    assert headers["X-Api-Key"] == FAKE_AK
    assert headers["X-Session"] == "SESSION-FAKE"
    assert counter.count("auth.get_valid_session") == 1

    # 门禁切换为 KILL_SWITCH：provider 内部仍走 _require() → 立刻拒绝
    broker.cfg.gate.state.kill_switch = True
    with pytest.raises(BrokerNotConfigured):
        provider()
    assert counter.count("auth.get_valid_session") == 1, (
        "被门禁拒绝时不得再取 Session")


def test_provider_raises_when_session_unavailable(tmp_path, monkeypatch,
                                                  clean_env):
    """Session 取不到 → BrokerError（FEED_AUTH_UNAVAILABLE），绝不匿名连接。"""
    from ai_trader.broker.base import BrokerError

    counter = _Counter()

    class _Broken(_Auth):
        def get_valid_session(self):
            counter.add("auth.get_valid_session")
            raise RuntimeError("session endpoint down")

    class _SDK(_make_fake_sdk(counter)):
        def __init__(self, **kw: Any) -> None:
            super().__init__(**kw)
            self.auth_manager = _Broken(counter)

    import fsopenapi
    monkeypatch.setattr(fsopenapi, "SDKClient", _SDK, raising=False)
    _set_env(monkeypatch)
    cfg = live_cfg(tmp_path, **{"fosun.base_url": "https://x.test"})
    broker = FosunBroker(cfg, _Vault({}))

    provider = build_feed_auth_provider(broker)
    assert provider is not None
    with pytest.raises(BrokerError) as ei:
        provider()
    assert getattr(ei.value, "code", "") == "FEED_AUTH_UNAVAILABLE"
    assert "session endpoint down" not in str(ei.value), "内部细节不得外泄"


# =========================================================================== #
# B-1：进程级凭证注册表（引用计数 + 冲突检测）
# =========================================================================== #
def test_two_instances_share_credentials_release_is_refcounted(
        tmp_path, monkeypatch, clean_env, patch_sdk):
    """A 释放后 B 仍在用 → 环境变量必须还在；B 释放后才清理。"""
    counter = _Counter()
    patch_sdk(counter)
    # 凭证来自保险箱 → 进程环境**本来没有**这些变量，才能验证"归零即删除"
    vault = _Vault({"FSOPENAPI_SERVER_PUBLIC_KEY": FAKE_PUB,
                    "FSOPENAPI_CLIENT_PRIVATE_KEY": FAKE_PRIV,
                    "FSOPENAPI_API_KEY": FAKE_AK})
    cfg_a = live_cfg(tmp_path, **{"fosun.base_url": "https://x.test"})
    cfg_b = live_cfg(tmp_path, **{"fosun.base_url": "https://x.test"})
    name = "FSOPENAPI_SERVER_PUBLIC_KEY"
    assert os.environ.get(name) is None

    a = FosunBroker(cfg_a, vault)
    b = FosunBroker(cfg_b, vault)
    assert a.configured and b.configured
    assert os.environ.get(name) == FAKE_PUB
    assert _CRED_ENV.owners_of(name) == 2

    a.release_credentials()
    assert os.environ.get(name) == FAKE_PUB, (
        "B 仍持有引用，A 的释放不得清掉环境变量")
    assert _CRED_ENV.owners_of(name) == 1

    b.release_credentials()
    assert os.environ.get(name) is None, "引用归零且调用前无值 → 必须删除"
    assert _CRED_ENV.owners_of(name) == 0


def test_release_cleans_empty_preexisting_and_is_idempotent(
        tmp_path, monkeypatch, clean_env, patch_sdk):
    """调用前是"存在但为空"的变量 → 释放后应删除而不是留空串；重复释放安全。"""
    counter = _Counter()
    patch_sdk(counter)
    name = "FSOPENAPI_SERVER_PUBLIC_KEY"
    monkeypatch.setenv(name, "")          # 空串不算已配置（走保险箱）
    vault = _Vault({"FSOPENAPI_SERVER_PUBLIC_KEY": FAKE_PUB,
                    "FSOPENAPI_CLIENT_PRIVATE_KEY": FAKE_PRIV,
                    "FSOPENAPI_API_KEY": FAKE_AK})
    cfg = live_cfg(tmp_path, **{"fosun.base_url": "https://x.test"})
    broker = FosunBroker(cfg, vault)
    assert broker._cred_origin.get(name) == "vault"
    assert os.environ.get(name) == FAKE_PUB, "适配器应写入自己的值"

    broker.release_credentials()
    assert os.environ.get(name) is None, "空串不应被当成「恢复旧值」"
    broker.release_credentials()  # 幂等
    assert os.environ.get(name) is None
    _CRED_ENV.clear()
    assert os.environ.get(name) is None, "clear 不得动环境"


def test_release_restores_preexisting_value(tmp_path, monkeypatch, clean_env,
                                            patch_sdk):
    """调用前已有非空值 → 释放时写回旧值（不丢运维原有的配置）。"""
    counter = _Counter()
    patch_sdk(counter)
    name = "FSOPENAPI_SERVER_PUBLIC_KEY"
    vault = _Vault({"FSOPENAPI_SERVER_PUBLIC_KEY": FAKE_PUB,
                    "FSOPENAPI_CLIENT_PRIVATE_KEY": FAKE_PRIV,
                    "FSOPENAPI_API_KEY": FAKE_AK})
    monkeypatch.setenv(name, "PRE-EXISTING-VALUE")
    cfg = live_cfg(tmp_path, **{"fosun.base_url": "https://x.test"})

    # 直接验证注册表语义：先登记再恢复（不改动适配器对 env 的优先级）
    _CRED_ENV.acquire(name, FAKE_PUB, "owner-x")
    assert os.environ.get(name) == FAKE_PUB
    res = _CRED_ENV.release("owner-x")
    assert name in res["restored"]
    assert os.environ.get(name) == "PRE-EXISTING-VALUE"
    assert cfg is not None


def test_conflicting_credential_values_rejected_by_registry(clean_env):
    """注册表层：同一变量两种值 → CredentialConflict，且不覆盖已写入的值。"""
    from ai_trader.broker.fosun_adapter import CredentialConflict

    name = "FSOPENAPI_SERVER_PUBLIC_KEY"
    assert _CRED_ENV.acquire(name, FAKE_PUB, "owner-a") == "created"
    assert os.environ.get(name) == FAKE_PUB

    with pytest.raises(CredentialConflict):
        _CRED_ENV.acquire(name, FAKE_PUB2, "owner-b")

    assert os.environ.get(name) == FAKE_PUB, "冲突时绝不静默覆盖"
    assert _CRED_ENV.owners_of(name) == 1, "失败的 acquire 不得留下引用"


def test_second_instance_with_conflict_fails_closed(tmp_path, monkeypatch,
                                                    clean_env, patch_sdk):
    """第二个实例遇到不同凭证 → fail-closed，且不接管所有权、不清 A 的引用。"""
    counter = _Counter()
    patch_sdk(counter)
    _set_env(monkeypatch, server_pub=FAKE_PUB)
    cfg_a = live_cfg(tmp_path, **{"fosun.base_url": "https://x.test"})
    a = FosunBroker(cfg_a, _Vault({}))
    assert a.configured
    name = "FSOPENAPI_SERVER_PUBLIC_KEY"
    assert _CRED_ENV.owners_of(name) == 1

    # 模拟"外部把环境换成了另一套凭证"：B 解析到不同值 → 必须拒绝启动
    _set_env(monkeypatch, server_pub=FAKE_PUB2)
    cfg_b = live_cfg(tmp_path, **{"fosun.base_url": "https://x.test"})
    b = FosunBroker(cfg_b, _Vault({}))

    assert b.configured is False
    assert "CredentialConflict" in b.blocked_reason
    assert b.client is None
    assert _CRED_ENV.owners_of(name) == 1, "B 不得接管所有权，也不得释放 A 的引用"
    # A 自身状态未被牵连
    assert a.configured
    assert a.feed_auth_ready()[0] is True


# =========================================================================== #
# C-1 / D-1 / F-2 的本机推送服务
# =========================================================================== #
class FeedServer:
    """可编排订阅回执与关闭行为的本机 WebSocket 服务。

    ``ack``:
        ok       → 回完整代码清单
        rid_only → 只回 ``{"requestid": rid}``（无 subRep）
        partial  → 少回一个代码
        none     → 不回执
    ``close_first``:
        ""       → 不主动关闭
        "ok"     → 第一条连接 ack 后以 1000 正常关闭
        "error"  → 第一条连接 ack 后以 1011 异常关闭
    """

    def __init__(self, ack: str = "ok", close_first: str = "") -> None:
        self.ack = ack
        self.close_first = close_first
        self.port = 0
        self.conns = 0
        self.headers: List[Dict[str, str]] = []
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._stop: Optional[asyncio.Event] = None

    def start(self) -> "FeedServer":
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        assert _wait_until(lambda: self.port > 0, 5), "测试服务未启动"
        return self

    def stop(self) -> None:
        if self._loop and self._stop:
            self._loop.call_soon_threadsafe(self._stop.set)
        if self._thread:
            self._thread.join(timeout=5)

    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/msg/v1/push"

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
        async with serve(self._handler, "127.0.0.1", 0) as server:
            self.port = server.sockets[0].getsockname()[1]
            await self._stop.wait()

    async def _handler(self, ws) -> None:
        self.conns += 1
        index = self.conns
        hdrs = getattr(getattr(ws, "request", None), "headers", {}) or {}
        self.headers.append({"X-Api-Key": hdrs.get("X-Api-Key", ""),
                             "X-Session": hdrs.get("X-Session", "")})
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if msg.get("act") != "sub":
                    continue
                item = (msg.get("items") or [{}])[0]
                rid = msg.get("requestid")
                topic = item.get("topic")
                codes = list(item.get("code") or [])
                await self._ack(ws, rid, topic, codes)
                if index == 1 and self.close_first == "ok":
                    await ws.close(code=1000, reason="normal rotation")
                    return
                if index == 1 and self.close_first == "error":
                    await ws.close(code=1011, reason="internal error")
                    return
        except Exception:  # noqa: BLE001
            pass

    async def _ack(self, ws, rid, topic, codes) -> None:
        if self.ack == "none":
            return
        if self.ack == "rid_only":
            await ws.send(json.dumps({"requestid": rid}))
            return
        if self.ack == "partial":
            await ws.send(json.dumps({"requestid": rid,
                                      "subRep": json.dumps({topic: codes[:-1]})}))
            return
        await ws.send(json.dumps({"requestid": rid,
                                  "subRep": json.dumps({topic: codes})}))


def _cfg(server: FeedServer, **over) -> FeedConfig:
    base = dict(url=server.url(), topics=("qt",), codes=("hk00700", "usaapl"),
                heartbeat_timeout=3.0, stale_after=5.0,
                reconnect_initial=0.05, reconnect_max=0.2,
                max_reconnect_attempts=2,
                require_auth=True, require_subscribe_ack=True,
                subscribe_timeout=0.8)
    base.update(over)
    return FeedConfig(**base)


def _provider(session: str = HDR_SESSION):
    return lambda: {"X-Api-Key": HDR_AK, "X-Session": session}


# =========================================================================== #
# C-1：严格订阅确认
# =========================================================================== #
def test_requestid_only_ack_is_unverified_not_subscribed():
    """只带 requestid 的回执 → ACK_UNVERIFIED，绝不 SUBSCRIBED。"""
    srv = FeedServer(ack="rid_only").start()
    f = MarketFeed(_cfg(srv), auth_provider=_provider())
    try:
        assert f.start(timeout=8) is False
        snap = f.snapshot()
        assert snap["state"] == FeedState.ACK_UNVERIFIED, snap["state"]
        assert snap["subscribed_codes"] == 0
        assert snap["stats"]["ack_unverified_terminal"] == 1
        assert snap["stats"]["ack_unverifiable"] >= 1
        assert "无法核实" in snap["last_error"]
        # 回执不是"错误"：不应污染 errors
        assert snap["stats"]["errors"] == 0
    finally:
        f.stop()
        srv.stop()


def test_full_code_list_ack_subscribes():
    """可核实代码清单 → SUBSCRIBED。"""
    srv = FeedServer(ack="ok").start()
    f = MarketFeed(_cfg(srv), auth_provider=_provider())
    try:
        assert f.start(timeout=8) is True
        snap = f.snapshot()
        assert snap["state"] == FeedState.SUBSCRIBED
        assert snap["subscription"]["ack_mode"] == "subRep"
        assert snap["subscribed_codes"] == 2
        assert snap["stats"]["ack_unverified_terminal"] == 0
    finally:
        f.stop()
        srv.stop()


def test_partial_code_list_is_failed_not_unverified():
    """部分接受 → FAILED（明确拒绝），与"无法核实"区分开。"""
    srv = FeedServer(ack="partial").start()
    f = MarketFeed(_cfg(srv, subscribe_timeout=1.5), auth_provider=_provider())
    try:
        assert f.start(timeout=8) is False
        snap = f.snapshot()
        assert snap["state"] == FeedState.FAILED
        assert "未被全部接受" in snap["last_error"]
        assert snap["stats"]["ack_unverified_terminal"] == 0
    finally:
        f.stop()
        srv.stop()


def test_no_ack_at_all_is_failed_not_unverified():
    """压根没回执 → FAILED（区别于"回了但核实不了"）。"""
    srv = FeedServer(ack="none").start()
    f = MarketFeed(_cfg(srv, subscribe_timeout=0.5), auth_provider=_provider())
    try:
        assert f.start(timeout=8) is False
        snap = f.snapshot()
        assert snap["state"] == FeedState.FAILED
        assert snap["stats"]["ack_unverified_terminal"] == 0
        assert snap["stats"]["ack_unverifiable"] == 0
    finally:
        f.stop()
        srv.stop()


def test_unverified_state_does_not_report_fresh_data():
    """ACK_UNVERIFIED 不得被当成"行情已就绪"。"""
    srv = FeedServer(ack="rid_only").start()
    f = MarketFeed(_cfg(srv), auth_provider=_provider())
    try:
        f.start(timeout=8)
        ok, reason = f.is_fresh("hk00700")
        assert ok is False
        assert "ACK_UNVERIFIED" in reason, reason
        assert f.snapshot()["settled"] == []
    finally:
        f.stop()
        srv.stop()


# =========================================================================== #
# D-1：正常关闭不计错误
# =========================================================================== #
def test_normal_close_not_counted_as_error():
    """服务端 code=1000 关闭 → errors 不增、last_error 不被写脏。"""
    srv = FeedServer(ack="ok", close_first="ok").start()
    f = MarketFeed(_cfg(srv), auth_provider=_provider())
    try:
        assert f.start(timeout=8) is True
        # 第一次连接被正常关闭 → 应重连并最终回到 SUBSCRIBED
        assert _wait_until(lambda: srv.conns >= 2, 8), "未触发重连"
        assert _wait_until(lambda: f.state == FeedState.SUBSCRIBED, 8)

        snap = f.snapshot()
        assert snap["stats"]["normal_closes"] >= 1
        assert snap["stats"]["errors"] == 0, (
            f"正常关闭不得计入 errors：{snap['stats']}")
        assert snap["last_error"] == ""
    finally:
        f.stop()
        srv.stop()


def test_abnormal_close_still_counted_as_error():
    """code=1011 异常关闭 → 仍然计入 errors 且有 last_error。"""
    srv = FeedServer(ack="ok", close_first="error").start()
    f = MarketFeed(_cfg(srv), auth_provider=_provider())
    try:
        assert f.start(timeout=8) is True
        assert _wait_until(lambda: srv.conns >= 2, 8), "未触发重连"
        assert _wait_until(lambda: f.snapshot()["stats"]["errors"] >= 1, 5)
        snap = f.snapshot()
        assert snap["stats"]["errors"] >= 1
        assert "ConnectionClosed" in snap["last_error"], snap["last_error"]
        assert snap["stats"]["normal_closes"] == 0
    finally:
        f.stop()
        srv.stop()


def test_stop_on_live_connection_is_not_an_error():
    """正常 stop() 不算错误。"""
    srv = FeedServer(ack="ok").start()
    f = MarketFeed(_cfg(srv), auth_provider=_provider())
    try:
        assert f.start(timeout=8) is True
    finally:
        f.stop()
        srv.stop()
    snap = f.snapshot()
    assert snap["state"] == FeedState.STOPPED
    assert snap["stats"]["errors"] == 0, snap["stats"]
    assert snap["last_error"] == ""


def test_close_code_helper_does_not_guess():
    """close code 只从异常对象读，读不到就是 None（不按类名猜）。"""
    f = MarketFeed(FeedConfig(url="ws://127.0.0.1:1/x", codes=("x",),
                              require_auth=False))

    class _Frame:
        code = 1000

    class _Closed:
        rcvd = _Frame()

    assert f._close_code(_Closed()) == 1000
    assert f._close_code(ConnectionError("boom")) is None
    assert f._is_normal_close(ConnectionError("boom")) is False
    f._closing = True
    assert f._is_normal_close(ConnectionError("boom")) is True, "预期 shutdown 不算错误"


# =========================================================================== #
# F-2：DEBUG 也不泄露认证头
# =========================================================================== #
def test_ws_logger_no_longer_pinned_to_info():
    """脱敏必须来自过滤器，而不是把 logger 钉死在 INFO。"""
    assert feed_mod._WS_LOGGER.level == logging.NOTSET, (
        "不得再靠 setLevel(INFO) 掩盖泄露")
    # 库侧兜底 logger 也必须挂了同一个过滤器（换 logger 也不漏）
    lib_logger = logging.getLogger("websockets.client")
    assert any(isinstance(f, feed_mod._RedactionFilter)
               for f in lib_logger.filters), "websockets.client 未挂脱敏过滤器"


def test_redaction_filter_masks_arg_objects():
    """websockets 的记录是 msg+args；对象里的头值也必须被抹掉。"""
    class _Req:
        def __str__(self) -> str:
            return (f"<Request X-Api-Key: {HDR_AK} X-Session: {HDR_SESSION} "
                    f"Authorization: Bearer abc.def.ghi>")

    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.DEBUG)
    feed_mod._WS_LOGGER.addHandler(handler)
    old = feed_mod._WS_LOGGER.level
    feed_mod._WS_LOGGER.setLevel(logging.DEBUG)
    try:
        feed_mod._WS_LOGGER.debug("%s > %s", "client", _Req())
    finally:
        feed_mod._WS_LOGGER.removeHandler(handler)
        feed_mod._WS_LOGGER.setLevel(old)

    out = buf.getvalue()
    assert "X-Api-Key: ***" in out, out
    assert HDR_AK not in out
    assert HDR_SESSION not in out
    assert "abc.def.ghi" not in out


def test_registered_secret_masked_even_without_header_name():
    """即使值裸奔在日志里（无字段名），登记过也要抹掉。"""
    feed_mod.register_feed_secrets({"X-Api-Key": HDR_AK, "X-Session": HDR_SESSION})
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    feed_mod._WS_LOGGER.addHandler(handler)
    old = feed_mod._WS_LOGGER.level
    feed_mod._WS_LOGGER.setLevel(logging.DEBUG)
    try:
        feed_mod._WS_LOGGER.debug("raw token=%s", HDR_AK)
    finally:
        feed_mod._WS_LOGGER.removeHandler(handler)
        feed_mod._WS_LOGGER.setLevel(old)

    out = buf.getvalue()
    assert HDR_AK not in out, out
    assert "***" in out and "token" in out, out


def test_debug_logging_never_leaks_real_connection_headers():
    """真实建连 + 全量 DEBUG 捕获 → 输出里不得出现任何凭证。"""
    srv = FeedServer(ack="ok").start()
    feed_mod.register_feed_secrets({"X-Api-Key": HDR_AK, "X-Session": HDR_SESSION})

    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.DEBUG)
    app_logger = logging.getLogger("ai_trader")
    ws_logger = logging.getLogger("ai_trader.market.feed.ws")
    old_app, old_ws = app_logger.level, ws_logger.level
    app_logger.addHandler(handler)
    app_logger.setLevel(logging.DEBUG)
    ws_logger.setLevel(logging.DEBUG)      # 关键：DEBUG 打开，不再靠 INFO 掩盖
    f = MarketFeed(_cfg(srv), auth_provider=_provider())
    try:
        assert f.start(timeout=8) is True
        f.snapshot()
    finally:
        f.stop()
        srv.stop()
        app_logger.removeHandler(handler)
        app_logger.setLevel(old_app)
        ws_logger.setLevel(old_ws)

    out = buf.getvalue()
    assert out, "DEBUG 下应有日志输出（否则该用例无鉴别力）"
    assert HDR_AK not in out, "DEBUG 泄露 API Key"
    assert HDR_SESSION not in out, "DEBUG 泄露 Session"
    for token in (FAKE_PRIV, "B4-FAKE-CLIENT-PRIV"):
        assert token not in out


def test_redaction_is_idempotent_and_never_raises():
    once = feed_mod.redact_log_text("X-Api-Key: abcdefgh12345")
    twice = feed_mod.redact_log_text(once)
    assert once == twice == "X-Api-Key: ***"
    assert feed_mod.redact_log_text("") == ""
    assert feed_mod.redact_log_text("正常中文日志") == "正常中文日志"
