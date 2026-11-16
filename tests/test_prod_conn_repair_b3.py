"""US-P1 Batch 3 验收测试：路由旁路 / 凭证清理 / requestid 严格校验 / 异步关闭。

对应独立审计 `US_P1_PRODCONN_INDEPENDENT_REVIEW.md` 的 F-1 / F-2 / F-3 / F-4 / F-5 / F-6 / F-10。

全部使用真实本机回环 WebSocket 服务与真实 SQLite / 真实文件系统；
券商侧用"实现了完整 Broker 契约的假 SDK"（带调用计数器），**不用 mock 打桩**。
所有凭证均为占位假串，不含任何真实密钥。
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import os
import pathlib
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from ai_trader.broker.base import BrokerNotConfigured, OrderRequest  # noqa: E402
from ai_trader.broker.fosun_adapter import FosunBroker  # noqa: E402
from ai_trader.market.feed import (  # noqa: E402
    FeedAuthError,
    FeedConfig,
    FeedState,
    FeedSubscriptionError,
    MarketFeed,
)
from ai_trader.secrets import SecretVault  # noqa: E402

from tests.helpers import live_cfg  # noqa: E402

FAKE_PUB = "-----BEGIN PUBLIC KEY-----\nB3-FAKE-SERVER-PUB\n-----END PUBLIC KEY-----"
FAKE_PRIV = "-----BEGIN PRIVATE KEY-----\nB3-FAKE-CLIENT-PRIV\n-----END PRIVATE KEY-----"
FAKE_AK = "b3-fake-api-key"
HDR_AK = "b3-hdr-api-key-1234"
HDR_SESSION = "b3-hdr-session-abcd"


def _wait_until(fn, timeout: float = 5.0, interval: float = 0.02) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(interval)
    return False


# --------------------------------------------------------------------------- #
# 假 SDK：完整 Broker 契约 + 调用计数器
# --------------------------------------------------------------------------- #
class _Counter:
    def __init__(self) -> None:
        self.calls: List[str] = []

    def add(self, name: str) -> None:
        self.calls.append(name)

    def count(self, prefix: str = "") -> int:
        return sum(1 for c in self.calls if c.startswith(prefix))


def _make_fake_sdk(counter: _Counter, *, raise_on_init: Optional[BaseException] = None,
                   payload: Optional[Dict[str, Any]] = None,
                   raise_flag: Optional[Dict[str, Any]] = None):
    """返回一个可注入到 ``fsopenapi.SDKClient`` 的类（带调用计数）。

    ``raise_flag`` 传入可变字典时可运行时切换 ``{"raise": True/False}``，
    便于"同一进程内先失败、后成功"的场景。
    """

    class _Node:
        def __init__(self, name: str) -> None:
            self._name = name

        def __getattr__(self, attr: str):
            def _call(*a: Any, **kw: Any):
                counter.add(f"{self._name}.{attr}")
                return payload if payload is not None else {
                    "code": 0,
                    "data": {"list": [{"stockCode": "AAPL", "marketCode": "us",
                                       "qty": 1, "avgCost": 10.0, "lastPrice": 10.0,
                                       "currency": "USD", "orderId": "OID-1",
                                       "status": "submitted"}]},
                }
            return _call

    class _FakeSDK:
        def __init__(self, **kw: Any) -> None:
            counter.add("SDKClient.__init__")
            if raise_flag is not None and raise_flag.get("raise"):
                raise raise_on_init or RuntimeError("switchable boom")
            if raise_on_init is not None and raise_flag is None:
                raise raise_on_init
            self._http_session = type("_S", (), {"request": lambda *a, **k: None})()
            for name in ("trade", "portfolio", "account", "market", "session"):
                object.__setattr__(self, name, _Node(name))
            self.auth_manager = type("_A", (), {
                "api_key": FAKE_AK,
                "get_valid_session": lambda self: ("SESSION-FAKE", "sk", "ek"),
            })()

        def dump_session(self) -> Dict[str, Any]:
            counter.add("dump_session")
            # 故意返回"危险"内容：若实现仍保存它，测试可发现
            return {"sessionId": "SESSION-FAKE", "clientPrivateKey": "PRIV-PEM-LEAK"}

    return _FakeSDK


@pytest.fixture
def clean_env(monkeypatch):
    for k in ("FSOPENAPI_SERVER_PUBLIC_KEY", "FSOPENAPI_CLIENT_PRIVATE_KEY",
              "FSOPENAPI_API_KEY", "FSOPENAPI_BASE_URL", "SDK_TYPE", "FSOPENAPI_LANG"):
        monkeypatch.delenv(k, raising=False)
    yield


@pytest.fixture
def patch_sdk(monkeypatch):
    """把假 SDK 装到 fsopenapi（内存内，仅本进程）。"""
    def _install(counter, **kw):
        import fsopenapi
        monkeypatch.setattr(fsopenapi, "SDKClient", _make_fake_sdk(counter, **kw),
                            raising=False)
    return _install


class _Vault:
    def __init__(self, data: Optional[Dict[str, str]] = None,
                 exc: Optional[BaseException] = None) -> None:
        self.data = data or {}
        self.exc = exc

    def get(self, name: str, default: Optional[str] = None) -> Optional[str]:
        if self.exc:
            raise self.exc
        return self.data.get(name, default)


# =========================================================================== #
# 1-2. 路由旁路封堵（P1 / F-3）
# =========================================================================== #
ROUTING_ENTRIES = [
    ("get_accounts", lambda b: b.get_accounts()),
    ("get_summary", lambda b: b.get_summary("USD")),
    ("get_positions", lambda b: b.get_positions()),
    ("get_quote", lambda b: b.get_quote("usAAPL")),
    ("get_klines", lambda b: b.get_klines("usAAPL")),
    ("place_order", lambda b: b.place_order(OrderRequest(
        symbol="usAAPL", market="us", side="buy", qty=1, price=200.0,
        order_type="limit", currency="USD", client_id="1234567890"))),
]


@pytest.mark.parametrize("name,call", ROUTING_ENTRIES)
def test_routing_conflict_blocks_all_entries(tmp_path, monkeypatch, clean_env,
                                             patch_sdk, name, call):
    """`sdk_type=ops` + 个人接口 → 六个入口全部拒绝，且 SDK 调用次数为 0。"""
    counter = _Counter()
    patch_sdk(counter)
    monkeypatch.setenv("FSOPENAPI_SERVER_PUBLIC_KEY", FAKE_PUB)
    monkeypatch.setenv("FSOPENAPI_CLIENT_PRIVATE_KEY", FAKE_PRIV)
    monkeypatch.setenv("FSOPENAPI_API_KEY", FAKE_AK)
    cfg = live_cfg(tmp_path, **{"fosun.base_url": "https://openapi.example.test",
                                "fosun.sdk_type": "ops",
                                "fosun.apply_account_id": "APPLY1",
                                "fosun.sub_account_id": "SUB1"})
    broker = FosunBroker(cfg, _Vault({}))
    assert broker.client is not None, "构造阶段不应因路由冲突而拒绝（只在入口处拦）"
    assert broker.routing_issue(), "ops + 个人接口必须报告冲突"

    before = len(counter.calls)
    with pytest.raises(BrokerNotConfigured) as ei:
        call(broker)
    assert "配置冲突" in str(ei.value)
    assert len(counter.calls) == before, f"拒绝前不得调用 SDK：{counter.calls}"
    assert counter.count("trade.") == 0
    assert counter.count("portfolio.") == 0
    assert counter.count("account.") == 0
    assert counter.count("market.") == 0


def test_place_order_rejected_before_sdk_call(tmp_path, monkeypatch, clean_env,
                                              patch_sdk):
    """特别验证：place_order 被拒时 `trade.create_order` 调用次数为 0。"""
    counter = _Counter()
    patch_sdk(counter)
    monkeypatch.setenv("FSOPENAPI_SERVER_PUBLIC_KEY", FAKE_PUB)
    monkeypatch.setenv("FSOPENAPI_CLIENT_PRIVATE_KEY", FAKE_PRIV)
    monkeypatch.setenv("FSOPENAPI_API_KEY", FAKE_AK)
    cfg = live_cfg(tmp_path, **{"fosun.base_url": "https://openapi.example.test",
                                "fosun.sdk_type": "ops",
                                "fosun.apply_account_id": "APPLY1"})
    broker = FosunBroker(cfg, _Vault({}))
    req = OrderRequest(symbol="usAAPL", market="us", side="buy", qty=1,
                       price=200.0, order_type="limit", currency="USD",
                       client_id="1234567890")
    with pytest.raises(BrokerNotConfigured):
        broker.place_order(req)
    assert counter.count("trade.create_order") == 0
    assert "trade.create_order" not in counter.calls


def test_routing_conflict_does_not_depend_on_paper_trading(tmp_path, monkeypatch,
                                                           clean_env, patch_sdk):
    """路由保护不能依赖 PAPER_TRADING —— 门禁全开时也必须拦。"""
    counter = _Counter()
    patch_sdk(counter)
    monkeypatch.setenv("FSOPENAPI_SERVER_PUBLIC_KEY", FAKE_PUB)
    monkeypatch.setenv("FSOPENAPI_CLIENT_PRIVATE_KEY", FAKE_PRIV)
    monkeypatch.setenv("FSOPENAPI_API_KEY", FAKE_AK)
    cfg = live_cfg(tmp_path, **{"fosun.base_url": "https://openapi.example.test",
                                "fosun.sdk_type": "ops",
                                "fosun.apply_account_id": "APPLY1"})
    assert cfg.gate.can_send_real_order()[0] is True, "夹具应为实盘门禁全开"
    broker = FosunBroker(cfg, _Vault({}))
    before = len(counter.calls)
    with pytest.raises(BrokerNotConfigured):
        broker.get_positions()
    assert len(counter.calls) == before


def test_personal_sdk_type_still_works(tmp_path, monkeypatch, clean_env, patch_sdk):
    """对照组：个人配置（sdk_type 空）下入口仍可正常调用（未被误杀）。"""
    counter = _Counter()
    patch_sdk(counter)
    monkeypatch.setenv("FSOPENAPI_SERVER_PUBLIC_KEY", FAKE_PUB)
    monkeypatch.setenv("FSOPENAPI_CLIENT_PRIVATE_KEY", FAKE_PRIV)
    monkeypatch.setenv("FSOPENAPI_API_KEY", FAKE_AK)
    cfg = live_cfg(tmp_path, **{"fosun.base_url": "https://openapi.example.test",
                                "fosun.sdk_type": ""})
    broker = FosunBroker(cfg, _Vault({}))
    assert broker.routing_issue() == ""
    broker.get_positions()
    assert counter.count("portfolio.get_holdings") == 1


# =========================================================================== #
# 3. 凭证注入的所有权与清理（P2 / F-1 / F-10）
# =========================================================================== #
def test_empty_env_plus_vault_then_sdk_failure_cleans_owned(tmp_path, monkeypatch,
                                                            clean_env, patch_sdk):
    """空 env + vault 有效 + SDK 构造失败 → 本次注入的身份变量必须被清掉。"""
    counter = _Counter()
    patch_sdk(counter, raise_on_init=RuntimeError("boom-b3"))
    monkeypatch.setenv("FSOPENAPI_SERVER_PUBLIC_KEY", "")
    monkeypatch.setenv("FSOPENAPI_CLIENT_PRIVATE_KEY", "")
    monkeypatch.setenv("FSOPENAPI_API_KEY", "")
    cfg = live_cfg(tmp_path, **{"fosun.base_url": "https://openapi.example.test",
                                "fosun.sdk_type": ""})
    broker = FosunBroker(cfg, _Vault({
        "FSOPENAPI_SERVER_PUBLIC_KEY": FAKE_PUB,
        "FSOPENAPI_CLIENT_PRIVATE_KEY": FAKE_PRIV,
        "FSOPENAPI_API_KEY": FAKE_AK,
    }))
    assert broker.configured is False
    assert os.environ.get("FSOPENAPI_SERVER_PUBLIC_KEY") in (None, ""), "公钥未清理"
    assert os.environ.get("FSOPENAPI_CLIENT_PRIVATE_KEY") in (None, ""), "私钥未清理"
    assert broker._sdk_env_owned == []
    # 失败原因不得包含 PEM 正文
    assert "B3-FAKE-CLIENT-PRIV" not in str(broker.blocked_reason)
    assert FAKE_AK not in str(broker.blocked_reason)


def test_valid_env_then_sdk_failure_keeps_preexisting(tmp_path, monkeypatch,
                                                      clean_env, patch_sdk):
    """调用前已存在的有效 env 变量 → 构造失败**不得误删**。"""
    counter = _Counter()
    patch_sdk(counter, raise_on_init=RuntimeError("boom-b3b"))
    monkeypatch.setenv("FSOPENAPI_SERVER_PUBLIC_KEY", FAKE_PUB)
    monkeypatch.setenv("FSOPENAPI_CLIENT_PRIVATE_KEY", FAKE_PRIV)
    monkeypatch.setenv("FSOPENAPI_API_KEY", FAKE_AK)
    cfg = live_cfg(tmp_path, **{"fosun.base_url": "https://openapi.example.test",
                                "fosun.sdk_type": ""})
    broker = FosunBroker(cfg, _Vault({}))
    assert broker.configured is False
    assert os.environ.get("FSOPENAPI_SERVER_PUBLIC_KEY") == FAKE_PUB
    assert os.environ.get("FSOPENAPI_CLIENT_PRIVATE_KEY") == FAKE_PRIV
    assert broker._sdk_env_owned == [], "预存变量不应被登记为本实例所有"


def test_sdk_success_keeps_credentials_and_drops_prev(tmp_path, monkeypatch,
                                                      clean_env, patch_sdk):
    """构造成功：保留 SDK 运行所需凭证；不得残留"被覆盖前的旧值"。"""
    counter = _Counter()
    patch_sdk(counter)
    monkeypatch.setenv("FSOPENAPI_SERVER_PUBLIC_KEY", FAKE_PUB)
    monkeypatch.setenv("FSOPENAPI_CLIENT_PRIVATE_KEY", FAKE_PRIV)
    monkeypatch.setenv("FSOPENAPI_API_KEY", FAKE_AK)
    cfg = live_cfg(tmp_path, **{"fosun.base_url": "https://openapi.example.test",
                                "fosun.sdk_type": ""})
    broker = FosunBroker(cfg, _Vault({}))
    assert broker.configured is True
    assert os.environ.get("FSOPENAPI_SERVER_PUBLIC_KEY") == FAKE_PUB
    assert broker._sdk_env_prev == {}, "成功后必须丢弃旧值副本"
    assert "dump_session" not in counter.calls, "不应再调用 dump_session（私钥 PEM）"
    assert not hasattr(broker, "_session_dumped") or broker._session_dumped is None


def test_vault_missing_and_corrupt_fail_closed(tmp_path, monkeypatch, clean_env,
                                               patch_sdk):
    """vault 缺失 / 损坏 → 均 fail-closed，且不调用 SDK。"""
    counter = _Counter()
    patch_sdk(counter)

    # 缺失
    cfg = live_cfg(tmp_path / "a", **{"fosun.base_url": "https://x.test",
                                      "fosun.sdk_type": ""})
    b1 = FosunBroker(cfg, _Vault({}))
    assert b1.configured is False
    assert counter.count("SDKClient.__init__") == 0

    # 损坏
    t = tmp_path / "b"
    t.mkdir(parents=True, exist_ok=True)
    (t / "vault.json").write_text("{ broken json", encoding="utf-8")
    cfg2 = live_cfg(t, **{"fosun.base_url": "https://x.test", "fosun.sdk_type": ""})
    vault = SecretVault(t)
    b2 = FosunBroker(cfg2, vault)
    assert b2.configured is False
    assert vault.diagnostics()["state"] == "damaged"
    assert vault.diagnostics()["kind"] == "corrupt_json"
    # 诊断里不得出现任何条目内容
    assert "B3-FAKE" not in json.dumps(b2.health(), ensure_ascii=False)


def test_cleanup_does_not_affect_other_instance(tmp_path, monkeypatch, clean_env,
                                                patch_sdk):
    """A 构造失败做清理后，B 依赖的预存环境变量仍然完好。"""
    counter = _Counter()
    flag = {"raise": True}
    patch_sdk(counter, raise_on_init=RuntimeError("boom-b3c"), raise_flag=flag)
    monkeypatch.setenv("FSOPENAPI_SERVER_PUBLIC_KEY", FAKE_PUB)
    monkeypatch.setenv("FSOPENAPI_CLIENT_PRIVATE_KEY", FAKE_PRIV)
    monkeypatch.setenv("FSOPENAPI_API_KEY", FAKE_AK)
    cfg = live_cfg(tmp_path / "a", **{"fosun.base_url": "https://x.test",
                                      "fosun.sdk_type": ""})
    a = FosunBroker(cfg, _Vault({}))
    assert a.configured is False
    # 另一个实例（同样依赖预存 env）：构造必须仍然成功
    flag["raise"] = False
    cfg2 = live_cfg(tmp_path / "b", **{"fosun.base_url": "https://x.test",
                                       "fosun.sdk_type": ""})
    b = FosunBroker(cfg2, _Vault({}))
    assert b.client is not None and b.configured is True
    assert os.environ.get("FSOPENAPI_CLIENT_PRIVATE_KEY") == FAKE_PRIV


def test_release_credentials_is_ownership_scoped(tmp_path, monkeypatch, clean_env,
                                                 patch_sdk):
    """release_credentials 只删自己创建的变量；预存变量保持不动。"""
    counter = _Counter()
    patch_sdk(counter, raise_on_init=RuntimeError("never"))  # 强制走失败清理路径
    monkeypatch.setenv("FSOPENAPI_SERVER_PUBLIC_KEY", FAKE_PUB)   # 预存
    monkeypatch.delenv("FSOPENAPI_CLIENT_PRIVATE_KEY", raising=False)  # 将由 vault 注入
    monkeypatch.setenv("FSOPENAPI_API_KEY", FAKE_AK)
    cfg = live_cfg(tmp_path, **{"fosun.base_url": "https://x.test", "fosun.sdk_type": ""})
    b = FosunBroker(cfg, _Vault({"FSOPENAPI_CLIENT_PRIVATE_KEY": FAKE_PRIV}))
    assert b.configured is False
    assert os.environ.get("FSOPENAPI_SERVER_PUBLIC_KEY") == FAKE_PUB, "预存公钥被误删"
    assert os.environ.get("FSOPENAPI_CLIENT_PRIVATE_KEY") in (None, ""), "注入私钥未清理"


# =========================================================================== #
# 4-5. requestid 严格校验（P2 / F-2）
# =========================================================================== #
class RidServer:
    """可编排 requestid 行为的本机推送服务。

    ``mode``:
        ok          → 回正确的 requestid + 完整 subRep
        wrong_rid   → 回**不匹配**的 requestid + 完整 subRep
        missing_rid → 回无 requestid + 完整 subRep
        dup         → 先回正确回执，再重复一次
        late        → 首次订阅回正确回执；第二次订阅回**上一次**的 requestid
        partial     → 正确 rid，但只接受部分代码
        opaque      → 正确 rid，subRep 为不可解析字符串
        none        → 不回执
    """

    def __init__(self, mode: str = "ok") -> None:
        self.mode = mode
        self.port = 0
        self.headers: List[Dict[str, str]] = []
        self.subs: List[Dict[str, Any]] = []
        self.conns = 0
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._stop: Optional[asyncio.Event] = None
        self._last_rid: Optional[str] = None

    def start(self) -> "RidServer":
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
                self.subs.append({"rid": rid, "topic": topic, "codes": codes})
                await self._ack(ws, rid, topic, codes)
        except Exception:  # noqa: BLE001
            pass

    async def _ack(self, ws, rid, topic, codes) -> None:
        if self.mode == "none":
            return
        if self.mode == "wrong_rid":
            await ws.send(json.dumps({"requestid": "WRONG-RID-0000",
                                      "subRep": json.dumps({topic: codes})}))
            return
        if self.mode == "missing_rid":
            await ws.send(json.dumps({"subRep": json.dumps({topic: codes})}))
            return
        if self.mode == "partial":
            await ws.send(json.dumps({"requestid": rid,
                                      "subRep": json.dumps({topic: codes[:-1]})}))
            return
        if self.mode == "opaque":
            await ws.send(json.dumps({"requestid": rid, "subRep": "not-json{{"}))
            return
        if self.mode == "dup":
            await ws.send(json.dumps({"requestid": rid,
                                      "subRep": json.dumps({topic: codes})}))
            await ws.send(json.dumps({"requestid": rid,
                                      "subRep": json.dumps({topic: codes})}))
            return
        if self.mode == "late":
            # 第一次给正确 rid；之后给"上一个"rid（迟到回执）
            send_rid = rid if self._last_rid is None else self._last_rid
            self._last_rid = rid
            await ws.send(json.dumps({"requestid": send_rid,
                                      "subRep": json.dumps({topic: codes})}))
            return
        await ws.send(json.dumps({"requestid": rid,
                                  "subRep": json.dumps({topic: codes})}))


def _cfg(server: RidServer, **over) -> FeedConfig:
    base = dict(url=server.url(), topics=("qt",), codes=("hk00700", "usaapl"),
                heartbeat_timeout=3.0, stale_after=5.0,
                reconnect_initial=0.05, reconnect_max=0.2,
                max_reconnect_attempts=1,
                require_auth=True, require_subscribe_ack=True,
                subscribe_timeout=1.0)
    base.update(over)
    return FeedConfig(**base)


def _provider(session: str = HDR_SESSION):
    return lambda: {"X-Api-Key": HDR_AK, "X-Session": session}


def test_correct_rid_confirms_subscription():
    srv = RidServer("ok").start()
    f = MarketFeed(_cfg(srv), auth_provider=_provider())
    try:
        assert f.start(timeout=8) is True
        snap = f.snapshot()
        assert snap["state"] == FeedState.SUBSCRIBED
        assert snap["subscription"]["ack_mode"] == "subRep"
        assert snap["subscribed_codes"] == 2
    finally:
        f.stop()
        srv.stop()


@pytest.mark.parametrize("mode", ["wrong_rid", "missing_rid"])
def test_unmatched_ack_never_subscribes(mode):
    """错误 RID / 缺失 RID → 绝不进入 SUBSCRIBED（且判为 FAILED）。"""
    srv = RidServer(mode).start()
    f = MarketFeed(_cfg(srv, subscribe_timeout=0.6), auth_provider=_provider())
    try:
        assert f.start(timeout=8) is False
        snap = f.snapshot()
        assert snap["state"] == FeedState.FAILED, snap
        assert snap["subscribed_codes"] == 0
        assert snap["subscription"]["failures"] >= 1
        assert "超时" in snap["last_error"]
        # 诊断信息不得包含回执内容
        assert "not-json" not in snap["subscription"]["diagnostic"]
        assert HDR_AK not in json.dumps(snap, ensure_ascii=False)
    finally:
        f.stop()
        srv.stop()


def test_unverifiable_ack_is_ack_unverified():
    """C-1（Batch 4）：RID 命中但回执体不可解释 → ACK_UNVERIFIED，不是 SUBSCRIBED。

    Batch 3 时这里判 FAILED；Batch 4 收紧后"回了但核实不了"必须与
    "压根没回"区分开（前者有诊断价值，且不能被当成订阅成功）。
    """
    srv = RidServer("opaque").start()
    f = MarketFeed(_cfg(srv, subscribe_timeout=0.6), auth_provider=_provider())
    try:
        assert f.start(timeout=8) is False
        snap = f.snapshot()
        assert snap["state"] == FeedState.ACK_UNVERIFIED, snap["state"]
        assert snap["subscribed_codes"] == 0
        assert "not-json" not in json.dumps(snap, ensure_ascii=False)
        assert HDR_AK not in json.dumps(snap, ensure_ascii=False)
    finally:
        f.stop()
        srv.stop()


def test_wrong_rid_is_counted_as_unmatched():
    srv = RidServer("wrong_rid").start()
    f = MarketFeed(_cfg(srv, subscribe_timeout=0.5), auth_provider=_provider())
    try:
        f.start(timeout=8)
        snap = f.snapshot()
        assert snap["subscription"]["unmatched_acks"] >= 1
        assert "rid" in snap["subscription"]["diagnostic"]
    finally:
        f.stop()
        srv.stop()


def test_partial_ack_rejected_and_not_marked_subscribed():
    srv = RidServer("partial").start()
    f = MarketFeed(_cfg(srv, subscribe_timeout=1.5), auth_provider=_provider())
    try:
        assert f.start(timeout=8) is False
        snap = f.snapshot()
        assert snap["state"] == FeedState.FAILED
        assert "未被全部接受" in snap["last_error"]
        assert snap["subscribed_codes"] == 0
    finally:
        f.stop()
        srv.stop()


def test_duplicate_ack_does_not_break_subscription():
    """重复回执：第一次确认即可；第二条作为"不匹配回执"被忽略，不得报错。"""
    srv = RidServer("dup").start()
    f = MarketFeed(_cfg(srv), auth_provider=_provider())
    try:
        assert f.start(timeout=8) is True
        snap = f.snapshot()
        assert snap["state"] == FeedState.SUBSCRIBED
        # 重复回执可能被订阅阶段消费、也可能落到接收循环：
        # 两种都不允许改变状态或引发异常
        assert snap["subscribed_codes"] == 2
    finally:
        f.stop()
        srv.stop()


def test_ack_verdict_api_is_strict():
    """直接对判定函数做边界测试（不建连）：RID 不匹配时绝不返回确认。"""
    f = MarketFeed(FeedConfig(url="ws://127.0.0.1:1/x", codes=("hk00700", "usaapl"),
                              require_auth=False))
    pending = {"rid-1": ("qt", {"hk00700", "usaapl"})}
    # 错误 RID + 内容完全匹配 → 不确认（不做内容兜底）
    got, mode = f._ack_verdict({"requestid": "rid-X",
                                "subRep": json.dumps({"qt": ["hk00700", "usaapl"]})},
                               pending)
    assert got == set() and mode == ""
    # 缺失 RID → 不确认
    got, mode = f._ack_verdict({"subRep": json.dumps({"qt": ["hk00700", "usaapl"]})},
                               pending)
    assert got == set() and mode == ""
    # 正确 RID + 完整内容 → 确认
    got, mode = f._ack_verdict({"requestid": "rid-1",
                                "subRep": json.dumps({"qt": ["hk00700", "usaapl"]})},
                               pending)
    assert got == {"rid-1"} and mode == "subRep"
    # 正确 RID + 只接受部分代码 → 抛错（绝不整体确认）
    with pytest.raises(FeedSubscriptionError):
        f._ack_verdict({"requestid": "rid-1",
                        "subRep": json.dumps({"qt": ["hk00700"]})}, pending)
    # 正确 RID + 接受主题但未列代码 → 不确认（不可验证）
    got, mode = f._ack_verdict({"requestid": "rid-1",
                                "subRep": json.dumps({"qt": []})}, pending)
    assert got == set()


# =========================================================================== #
# 6. FAILED 恢复与 start() 语义（P2 / F-4）
# =========================================================================== #
def test_start_after_failed_returns_consistent_result(tmp_path):
    """状态已是 FAILED 时再次 start()：返回值必须与最终状态一致（不再 0s 假 False）。"""
    srv = RidServer("ok").start()
    f = MarketFeed(_cfg(srv), auth_provider=_provider())
    try:
        f._set_state(FeedState.FAILED)          # 模拟上一轮失败
        t0 = time.time()
        r = f.start(timeout=8)
        dt = time.time() - t0
        snap = f.snapshot()
        assert (r is True) == (snap["state"] == FeedState.SUBSCRIBED), (r, snap["state"])
        assert r is True, "服务正常时应恢复成功"
        assert dt > 0.001, "不应在 0 秒内直接返回"
    finally:
        f.stop()
        srv.stop()


def test_start_reports_false_on_real_failure(tmp_path):
    """真正失败（401）时 start() 必须返回 False 且 state=FAILED。"""
    srv = RidServer("ok").start()
    srv.stop()                                   # 端口已关 → 连接失败
    f = MarketFeed(FeedConfig(url=f"ws://127.0.0.1:1/x", codes=("hk00700",),
                              require_auth=False, max_reconnect_attempts=1,
                              reconnect_initial=0.05, reconnect_max=0.1))
    try:
        r = f.start(timeout=3)
        assert r is False
        assert f.state in (FeedState.FAILED, FeedState.RECONNECTING, FeedState.CONNECTING)
    finally:
        f.stop()


def test_start_timeout_is_bounded():
    """订阅永不回执 → start() 必须在超时上限内返回，不得无限等待。"""
    srv = RidServer("none").start()
    f = MarketFeed(_cfg(srv, subscribe_timeout=0.4), auth_provider=_provider())
    try:
        t0 = time.time()
        r = f.start(timeout=5)
        dt = time.time() - t0
        assert r is False
        assert dt < 5.0
        assert f.state == FeedState.FAILED
    finally:
        f.stop()
        srv.stop()


def test_reconnect_then_recover(tmp_path):
    """断线 → 重连 → 恢复订阅：最终必须回到 SUBSCRIBED，且 Session 不被复用。"""
    srv = RidServer("ok").start()
    sessions: List[str] = []

    def provider():
        sid = f"s-{len(sessions) + 1}"
        sessions.append(sid)
        return {"X-Api-Key": HDR_AK, "X-Session": sid}

    f = MarketFeed(_cfg(srv, heartbeat_timeout=0.4, max_reconnect_attempts=3),
                   auth_provider=provider)
    try:
        assert f.start(timeout=8) is True
        assert _wait_until(lambda: srv.conns >= 1, 3)
        # 让服务端"沉默"触发心跳超时 → 重连
        srv.mode = "ok"
        assert _wait_until(lambda: srv.conns >= 2, 8), "未触发重连"
        assert _wait_until(lambda: f.state == FeedState.SUBSCRIBED, 8)
        assert len(set(sessions)) >= 2
    finally:
        f.stop()
        srv.stop()


# =========================================================================== #
# 7. 关闭期 EOFError（P2 / F-5）
# =========================================================================== #
def test_normal_close_has_no_unhandled_traceback():
    """正常 stop() 不应把 asyncio 的 EOFError traceback 打到 stderr。"""
    srv = RidServer("ok").start()
    f = MarketFeed(_cfg(srv), auth_provider=_provider())
    buf = io.StringIO()
    try:
        assert f.start(timeout=8) is True
        with contextlib.redirect_stderr(buf):
            f.stop()
            time.sleep(0.3)
    finally:
        srv.stop()
    out = buf.getvalue()
    assert "Traceback (most recent call last)" not in out, out[:400]
    assert "Exception in callback" not in out, out[:400]


def test_dead_port_close_has_no_unhandled_traceback():
    """连不上（端口不可达）→ stop() 也不应刷未处理 traceback。"""
    f = MarketFeed(FeedConfig(url="ws://127.0.0.1:1/x", codes=("hk00700",),
                              require_auth=False, max_reconnect_attempts=1,
                              reconnect_initial=0.05, reconnect_max=0.1))
    buf = io.StringIO()
    try:
        with contextlib.redirect_stderr(buf):
            f.start(timeout=1.5)
            f.stop()
            time.sleep(0.4)
    finally:
        pass
    out = buf.getvalue()
    assert "Traceback (most recent call last)" not in out, out[:400]
    assert f.snapshot()["stats"]["shutdown_noise"] >= 0


def test_loop_exception_classifier_distinguishes_real_errors():
    """分类器：预期关闭噪声只计数；真实异常计入 errors。"""
    f = MarketFeed(FeedConfig(url="ws://127.0.0.1:1/x", codes=("hk00700",),
                              require_auth=False))
    f._closing = True
    f._on_loop_exception(None, {"exception": EOFError("stream ended"),
                                "message": "x"})
    assert f.snapshot()["stats"]["shutdown_noise"] == 1
    assert f.snapshot()["stats"]["errors"] == 0

    f._closing = False
    f._set_state(FeedState.CONNECTING)
    f._on_loop_exception(None, {"exception": ValueError("boom"),
                                "message": "real"})
    assert f.snapshot()["stats"]["errors"] >= 1
    assert "ValueError" in f.snapshot()["last_error"]


# =========================================================================== #
# 8. 泄露面：日志 / 状态 / 健康
# =========================================================================== #
def test_no_secret_in_logs_snapshot_or_health(tmp_path, monkeypatch, clean_env,
                                              patch_sdk):
    """认证值不得进入日志、snapshot、health。"""
    counter = _Counter()
    patch_sdk(counter)
    monkeypatch.setenv("FSOPENAPI_SERVER_PUBLIC_KEY", FAKE_PUB)
    monkeypatch.setenv("FSOPENAPI_CLIENT_PRIVATE_KEY", FAKE_PRIV)
    monkeypatch.setenv("FSOPENAPI_API_KEY", FAKE_AK)

    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    # 只捕获**应用侧**日志；本机测试服务（websockets.server）自己会 DEBUG 打印
    # 它收到的请求头，那不是被测进程的外泄路径。
    app_logger = logging.getLogger("ai_trader")
    old_app_level = app_logger.level
    app_logger.addHandler(handler)
    app_logger.setLevel(logging.DEBUG)
    # Batch 4（F-2）：不再需要把 feed.ws 钉在 INFO —— 脱敏过滤器保证 DEBUG
    # 也不泄露。这里故意开到 DEBUG，把"DEBUG 泄露"这条路一起测掉。
    ws_app_logger = logging.getLogger("ai_trader.market.feed.ws")
    old_ws_level = ws_app_logger.level
    ws_app_logger.setLevel(logging.DEBUG)
    try:
        cfg = live_cfg(tmp_path, **{"fosun.base_url": "https://x.test",
                                    "fosun.sdk_type": ""})
        broker = FosunBroker(cfg, _Vault({}))
        blob = json.dumps(broker.health(), ensure_ascii=False, default=str)

        srv = RidServer("ok").start()
        feed = MarketFeed(_cfg(srv), auth_provider=_provider())
        try:
            feed.start(timeout=8)
            snap = json.dumps(feed.snapshot(), ensure_ascii=False)
        finally:
            feed.stop()
            srv.stop()
    finally:
        app_logger.removeHandler(handler)
        app_logger.setLevel(old_app_level)
        ws_app_logger.setLevel(old_ws_level)

    logs = buf.getvalue()
    for token in (FAKE_PRIV, FAKE_AK, "B3-FAKE-CLIENT-PRIV", HDR_SESSION):
        assert token not in blob, f"health 泄露 {token[:6]}"
        assert token not in snap, f"snapshot 泄露 {token[:6]}"
        assert token not in logs, f"日志泄露 {token[:6]}"


def test_vault_diagnostics_has_no_content(tmp_path):
    t = tmp_path / "v"
    t.mkdir()
    (t / "vault.json").write_text("{bad", encoding="utf-8")
    v = SecretVault(t)
    v.get("anything")
    d = v.diagnostics()
    assert set(d) == {"state", "kind", "exists", "note"}
    assert d["kind"] == "corrupt_json"
    assert "bad" not in json.dumps(d)
