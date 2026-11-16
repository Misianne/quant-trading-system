"""US-P1 Batch 4.1.1 / A-1 定点修复测试（T24–T28）。

A-1 的问题：SDK 的 ``MarketPushAPI.connect()`` 会自己取 Session 并连到硬编码的
生产地址，完全绕过 ``feed_auth_ready()`` / ``feed_auth_headers()``。

本文件验证 **类级** 拦截（Batch 4.1.1 新增）：

* 不能拦 ``MarketPushAPI.__init__``（``SDKClient.__init__`` 会构造它）；
* 因此改为替换 ``connect/subscribe/unsubscribe/close/send_heartbeat`` 五个方法；
* 拦截器是**同步**函数：``obj.connect()`` 调用即抛，不会先造出一个没人 await
  的协程（那等于静默放行）；
* 多实例引用计数：最后一个释放者才恢复；只恢复"仍是我们的包装"的方法。

全部使用本地假类，不联网、不创建真实 Session。
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from ai_trader import config as cfg_mod  # noqa: E402
from ai_trader.broker.base import BrokerError  # noqa: E402
from ai_trader.broker.fosun_adapter import (  # noqa: E402
    FosunBroker,
    _CRED_ENV,
    _MarketPushDisabled,
    _MPA_GUARD,
)

from tests.helpers import live_cfg  # noqa: E402

PUB = "-----BEGIN PUBLIC KEY-----\nB411-A1-PUB\n-----END PUBLIC KEY-----"
PRIV = "-----BEGIN PRIVATE KEY-----\nB411-A1-PRIV\n-----END PRIVATE KEY-----"
AK = "b411-a1-api-key-1234"

#: 记录"真实方法被调到过"——拦截生效时它必须保持为空
CALLS: List[str] = []


class _FakeMarketPush:
    """等价于 SDK ``MarketPushAPI``：五个核心入口 + 若干薄包装。"""

    def __init__(self, client: Any) -> None:
        self.client = client
        self._url = "wss://openapi-push.example.invalid/msg/v1/push"

    async def connect(self) -> None:                      # pragma: no cover
        CALLS.append("connect")

    async def subscribe(self, topic: str, codes: Any) -> None:   # pragma: no cover
        CALLS.append("subscribe")

    async def unsubscribe(self, topic: str, codes: Any) -> None:  # pragma: no cover
        CALLS.append("unsubscribe")

    async def close(self) -> None:                         # pragma: no cover
        CALLS.append("close")

    async def send_heartbeat(self, hb: Any) -> None:       # pragma: no cover
        CALLS.append("send_heartbeat")

    async def subscribeQuote(self, codes: Any) -> None:    # pragma: no cover
        CALLS.append("subscribeQuote")
        await self.subscribe("quote", codes)


_ORIGINALS: Dict[str, Any] = {
    name: getattr(_FakeMarketPush, name)
    for name in ("connect", "subscribe", "unsubscribe", "close", "send_heartbeat")
}


class _FakeSDK:
    def __init__(self, **kw: Any) -> None:
        self._http_session = type("_S", (), {"request": lambda *a, **k: None})()
        self.market_push = _FakeMarketPush(self)


class _FakeSDKNoPush(_FakeSDK):
    """缺少一个入口方法的 SDK：类级安装必须 fail-closed。"""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.market_push = _PartialPush(self)


class _PartialPush(_FakeMarketPush):
    close = None          # 非 callable → 安装失败


class _Vault:
    def __init__(self, data: Dict[str, str]) -> None:
        self.data = dict(data)

    def get(self, name: str, default: Any = None) -> Any:
        return self.data.get(name, default)


VAULT = _Vault({"FSOPENAPI_SERVER_PUBLIC_KEY": PUB,
                "FSOPENAPI_CLIENT_PRIVATE_KEY": PRIV,
                "FSOPENAPI_API_KEY": AK})


@pytest.fixture(autouse=True)
def _reset_guard():
    """清空身份环境与两个进程级登记表，避免用例之间互相"继承"凭证。"""
    import os

    keys = ("FSOPENAPI_SERVER_PUBLIC_KEY", "FSOPENAPI_CLIENT_PRIVATE_KEY",
            "FSOPENAPI_API_KEY")
    saved = {k: os.environ.get(k) for k in keys}
    for k in keys:
        os.environ.pop(k, None)
    CALLS.clear()
    _MPA_GUARD.clear()
    _CRED_ENV.clear()
    cfg_mod._reset_dotenv_keys()
    try:
        yield
    finally:
        _MPA_GUARD.clear()
        _CRED_ENV.clear()
        cfg_mod._reset_dotenv_keys()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        CALLS.clear()


@pytest.fixture
def patch_sdk(monkeypatch):
    import fsopenapi

    def _install(cls=_FakeSDK):
        monkeypatch.setattr(fsopenapi, "SDKClient", cls, raising=False)

    return _install


def _cfg(tmp_path):
    return live_cfg(tmp_path, **{"fosun.base_url": "https://x.test"})


def _broker(tmp_path, **kw) -> FosunBroker:
    return FosunBroker(_cfg(tmp_path), kw.pop("vault", VAULT))


# --------------------------------------------------------------------------- #
# T24 / T25：类级拦截生效，且不触达网络
# --------------------------------------------------------------------------- #
def test_t24_class_level_guard_blocks_all_entry_points(tmp_path, patch_sdk):
    patch_sdk()
    broker = _broker(tmp_path)
    assert broker.configured is True, broker.blocked_reason

    # 另造一个实例：它没被换成哨兵，只能靠**类级**拦截挡住
    fresh = _FakeMarketPush(broker.client)
    assert not isinstance(fresh, _MarketPushDisabled)

    for name, args in (("connect", ()), ("subscribe", ("quote", ["AAPL"])),
                       ("unsubscribe", ("quote", ["AAPL"])), ("close", ()),
                       ("send_heartbeat", (1,))):
        with pytest.raises(BrokerError) as ei:
            getattr(fresh, name)(*args)          # 同步抛，不需要 await
        assert ei.value.code == "FEED_AUTH_BYPASS", (name, ei.value.code)
    # 薄包装也走不通：它内部仍要调 subscribe，await 时同样抛 FEED_AUTH_BYPASS
    with pytest.raises(BrokerError) as ei:
        asyncio.run(fresh.subscribeQuote(["AAPL"]))
    assert ei.value.code == "FEED_AUTH_BYPASS"


def test_t25_no_session_and_no_network_when_blocked(tmp_path, patch_sdk):
    patch_sdk()
    broker = _broker(tmp_path)
    assert broker.configured is True, broker.blocked_reason

    fresh = _FakeMarketPush(broker.client)
    with pytest.raises(BrokerError):
        fresh.connect()

    assert CALLS == [], f"真实方法被调到过：{CALLS}"
    assert broker._conn.connects == 0, "不得发起任何连接"


# --------------------------------------------------------------------------- #
# T26：SDK 构造不受影响
# --------------------------------------------------------------------------- #
def test_t26_sdk_construction_unaffected(tmp_path, patch_sdk):
    patch_sdk()
    broker = _broker(tmp_path)

    assert broker.client is not None
    assert broker.configured is True
    assert broker.blocked_reason == ""
    # 实例级哨兵仍在（T28 的前置）
    assert isinstance(broker.client.market_push, _MarketPushDisabled)


# --------------------------------------------------------------------------- #
# T27：释放后精确还原；多实例不提前还原
# --------------------------------------------------------------------------- #
def test_t27_release_restores_originals_only_after_last_owner(tmp_path, patch_sdk):
    patch_sdk()
    a = _broker(tmp_path)
    b = FosunBroker(_cfg(tmp_path), VAULT)
    assert a.configured and b.configured
    assert _MPA_GUARD.status()["owners"] == 2

    a.close()
    status = _MPA_GUARD.status()
    assert status["installed"] is True, "B 还在用，不能提前拆保护"
    assert status["owners"] == 1
    with pytest.raises(BrokerError):
        _FakeMarketPush(a.client).connect()

    b.close()
    assert _MPA_GUARD.status()["installed"] is False
    for name, fn in _ORIGINALS.items():
        assert getattr(_FakeMarketPush, name) is fn, f"{name} 未精确还原"

    # 还原后真实方法可以正常执行（证明我们恢复的是原实现，不是空壳）
    fresh = _FakeMarketPush(None)
    asyncio.run(fresh.connect())
    assert CALLS == ["connect"]


def test_t28_guard_detects_third_party_overwrite(tmp_path, patch_sdk):
    patch_sdk()
    broker = _broker(tmp_path)
    assert _MPA_GUARD.status()["tampered"] == []

    # 第三方把 connect 改回原实现
    _FakeMarketPush.connect = _ORIGINALS["connect"]
    try:
        assert _MPA_GUARD.status()["tampered"] == ["connect"]
        assert broker.health()["market_push_guard_detail"]["tampered"] == ["connect"]
    finally:
        _FakeMarketPush.connect = _ORIGINALS["connect"]


# --------------------------------------------------------------------------- #
# 安装失败 → fail-closed
# --------------------------------------------------------------------------- #
def test_install_failure_is_fail_closed(tmp_path, patch_sdk):
    patch_sdk(_FakeSDKNoPush)
    broker = _broker(tmp_path)

    assert broker.configured is False
    assert broker.client is None
    assert "market_push" in broker.blocked_reason or "行情推送" in broker.blocked_reason, \
        broker.blocked_reason
    assert _MPA_GUARD.status()["installed"] is False, "失败必须回滚，不留半套拦截"
    assert _FakeMarketPush.connect is _ORIGINALS["connect"]


def test_health_reports_guard_detail(tmp_path, patch_sdk):
    patch_sdk()
    broker = _broker(tmp_path)

    health = broker.health()
    assert health["market_push_guard"] is True
    detail = health["market_push_guard_detail"]
    assert detail["installed"] is True
    assert detail["class"] == "_FakeMarketPush"
    assert set(detail["methods"]) == set(_MPA_GUARD.METHODS)
    assert detail["owners"] == 1
