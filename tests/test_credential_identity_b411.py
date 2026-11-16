"""US-P1 Batch 4.1.1 / B-1 定点修复测试（T15–T23）。

覆盖两条被独立审计点名的缺陷：

* **SR-B1-04**：``.env`` 被 ``load_dotenv`` 灌进 ``os.environ`` 后，三者看起来
  都是"环境变量"，于是 dotenv 的 API_KEY 会压过 Vault，形成"混合身份"；
* **SR-B1-05**：空 Vault 的实例会复用**其它实例注入**的身份，A 释放后 B 仍以
  A 的身份运行。

断言只看 ``health()`` 的**来源/完整性**字段与注册表引用计数，不读任何密钥值；
SDK 用"记录构造参数"的假实现，不联网、不写盘。
"""
from __future__ import annotations

import os
import pathlib
import sys
import threading
from typing import Any, Dict, Optional

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from ai_trader import config as cfg_mod  # noqa: E402
from ai_trader.broker.fosun_adapter import (  # noqa: E402
    CredentialConflict,
    FosunBroker,
    _CRED_ENV,
)

from tests.helpers import live_cfg  # noqa: E402

PUB = "-----BEGIN PUBLIC KEY-----\nB411-FAKE-PUB-A\n-----END PUBLIC KEY-----"
PUB2 = "-----BEGIN PUBLIC KEY-----\nB411-FAKE-PUB-B\n-----END PUBLIC KEY-----"
PRIV = "-----BEGIN PRIVATE KEY-----\nB411-FAKE-PRIV-A\n-----END PRIVATE KEY-----"
PRIV2 = "-----BEGIN PRIVATE KEY-----\nB411-FAKE-PRIV-B\n-----END PRIVATE KEY-----"
AK = "b411-fake-api-key-aaaa1111"
AK2 = "b411-fake-api-key-bbbb2222"

ENV_KEYS = ("FSOPENAPI_SERVER_PUBLIC_KEY", "FSOPENAPI_CLIENT_PRIVATE_KEY",
            "FSOPENAPI_API_KEY")


class _Vault:
    def __init__(self, data: Optional[Dict[str, str]] = None) -> None:
        self.data = dict(data or {})

    def get(self, name: str, default: Optional[str] = None) -> Optional[str]:
        return self.data.get(name, default)


class _CapturingSDK:
    """记录"SDK 真正能读到什么"：身份密钥来自进程环境，api_key 来自构造参数。"""

    last: Dict[str, Any] = {}

    def __init__(self, **kw: Any) -> None:
        type(self).last = {
            "api_key": kw.get("api_key"),
            "base_url": kw.get("base_url"),
            "server_public_key": os.environ.get("FSOPENAPI_SERVER_PUBLIC_KEY"),
            "client_private_key": os.environ.get("FSOPENAPI_CLIENT_PRIVATE_KEY"),
        }
        self._http_session = type("_S", (), {"request": lambda *a, **k: None})()


@pytest.fixture(autouse=True)
def _clean_credential_env(monkeypatch):
    """清空身份变量与三个进程级登记表，测试后还原环境。"""
    saved = {k: os.environ.get(k) for k in ENV_KEYS}
    for k in ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    _CRED_ENV.clear()
    cfg_mod._reset_dotenv_keys()
    try:
        yield
    finally:
        _CRED_ENV.clear()
        cfg_mod._reset_dotenv_keys()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@pytest.fixture
def patch_sdk(monkeypatch):
    import fsopenapi

    monkeypatch.setattr(fsopenapi, "SDKClient", _CapturingSDK, raising=False)
    _CapturingSDK.last = {}


def _cfg(tmp_path, name: str = "a"):
    return live_cfg(tmp_path / name, **{"fosun.base_url": "https://x.test"})


def _write_dotenv(tmp_path, pub: str, priv: str, ak: str) -> pathlib.Path:
    p = tmp_path / "fake.env"
    p.write_text(
        f"FSOPENAPI_SERVER_PUBLIC_KEY={pub}\n"
        f"FSOPENAPI_CLIENT_PRIVATE_KEY={priv}\n"
        f"FSOPENAPI_API_KEY={ak}\n", encoding="utf-8")
    return p


def _sources(broker: FosunBroker) -> Dict[str, str]:
    return dict(broker.health()["credential_sources"])


# --------------------------------------------------------------------------- #
# T15：dotenv 不再压过 Vault
# --------------------------------------------------------------------------- #
def test_t15_dotenv_does_not_outrank_vault(tmp_path, monkeypatch, patch_sdk):
    env_file = _write_dotenv(tmp_path, "DOTENV-PUB-0001", "DOTENV-PRIV-0001",
                             "dotenv-ak-0001")
    assert cfg_mod.load_dotenv(env_file) is True
    assert "FSOPENAPI_API_KEY" in cfg_mod.dotenv_keys()

    vault = _Vault({"FSOPENAPI_SERVER_PUBLIC_KEY": PUB,
                    "FSOPENAPI_CLIENT_PRIVATE_KEY": PRIV,
                    "FSOPENAPI_API_KEY": AK})
    broker = FosunBroker(_cfg(tmp_path), vault)

    assert broker.configured is True, broker.blocked_reason
    assert _CapturingSDK.last["client_private_key"] == PRIV, "Vault 必须压过 dotenv"
    assert _CapturingSDK.last["api_key"] == AK
    assert _sources(broker) == {"PUB_KEY": "vault", "PRIV_KEY": "vault",
                                "API_KEY": "vault"}
    assert broker.health()["credential_validation"]["status"] == "ok"


# --------------------------------------------------------------------------- #
# T16 / T17：混合来源 fail-closed
# --------------------------------------------------------------------------- #
def test_t16_mixed_sources_fail_closed(tmp_path, monkeypatch, patch_sdk):
    monkeypatch.setenv("FSOPENAPI_SERVER_PUBLIC_KEY", PUB)   # 只有公钥来自环境
    vault = _Vault({"FSOPENAPI_CLIENT_PRIVATE_KEY": PRIV,
                    "FSOPENAPI_API_KEY": AK})

    broker = FosunBroker(_cfg(tmp_path), vault)

    assert broker.configured is False
    assert broker.client is None
    assert "CREDENTIAL_SOURCE_MIX" in broker.blocked_reason, broker.blocked_reason
    assert _CapturingSDK.last == {}, "混合身份绝不能构造 SDK"


def test_t17_health_exposes_sources_and_mixed_flag(tmp_path, monkeypatch, patch_sdk):
    monkeypatch.setenv("FSOPENAPI_SERVER_PUBLIC_KEY", PUB)
    broker = FosunBroker(_cfg(tmp_path), _Vault({
        "FSOPENAPI_CLIENT_PRIVATE_KEY": PRIV, "FSOPENAPI_API_KEY": AK}))

    health = broker.health()
    assert health["credential_mixed"] is True
    assert health["credential_validation"]["mixed"] is True
    assert health["credential_validation"]["complete"] is True
    assert health["credential_sources"]["PUB_KEY"] == "explicit_env"
    assert health["credential_sources"]["PRIV_KEY"] == "vault"
    # 只暴露脱敏指纹：12 位十六进制，且不等于任何原始值
    for short, fp in health["credential_validation"]["fingerprints"].items():
        assert fp == "" or (fp.startswith("sha256:") and len(fp) == 19), (short, fp)
        for secret in (PUB, PRIV, AK):
            assert secret not in fp


# --------------------------------------------------------------------------- #
# T18 / T19：空 Vault 不得继承
# --------------------------------------------------------------------------- #
def test_t18_clean_env_empty_vault_fails_closed(tmp_path, patch_sdk):
    broker = FosunBroker(_cfg(tmp_path), _Vault({}))

    assert broker.configured is False
    assert "CREDENTIAL_INCOMPLETE" in broker.blocked_reason, broker.blocked_reason
    assert _CRED_ENV.snapshot() == {}, "未注入就不该有任何登记"
    assert _CapturingSDK.last == {}


def test_t19_empty_vault_does_not_inherit_other_instance(tmp_path, patch_sdk):
    """A（有 Vault）注入后，B（空 Vault）**不得**顶用 A 的身份。"""
    a = FosunBroker(_cfg(tmp_path, "a"), _Vault({
        "FSOPENAPI_SERVER_PUBLIC_KEY": PUB,
        "FSOPENAPI_CLIENT_PRIVATE_KEY": PRIV,
        "FSOPENAPI_API_KEY": AK}))
    assert a.configured is True, a.blocked_reason
    assert _CRED_ENV.owners_of("FSOPENAPI_CLIENT_PRIVATE_KEY") == 1

    b = FosunBroker(_cfg(tmp_path, "b"), _Vault({}))

    assert b.configured is False, "空 Vault 实例继承了 A 的身份"
    assert "CREDENTIAL_INCOMPLETE" in b.blocked_reason, b.blocked_reason
    assert b.client is None
    assert _CRED_ENV.owners_of("FSOPENAPI_CLIENT_PRIVATE_KEY") == 1, "B 不得接管引用"


def test_t19b_same_identity_may_share_via_dotenv(tmp_path, patch_sdk):
    """两个实例读同一份 ``.env`` → 同一身份，允许共享（引用计数=2）。"""
    env_file = _write_dotenv(tmp_path, PUB, PRIV, AK)
    assert cfg_mod.load_dotenv(env_file) is True

    a = FosunBroker(_cfg(tmp_path, "a"), _Vault({}))
    b = FosunBroker(_cfg(tmp_path, "b"), _Vault({}))

    assert a.configured is True, a.blocked_reason
    assert b.configured is True, b.blocked_reason
    assert _sources(b) == {"PUB_KEY": "dotenv", "PRIV_KEY": "dotenv",
                           "API_KEY": "dotenv"}
    assert _CRED_ENV.owners_of("FSOPENAPI_CLIENT_PRIVATE_KEY") == 2


def test_t20_operator_exported_value_is_not_mistaken_for_injection(tmp_path,
                                                                   monkeypatch,
                                                                   patch_sdk):
    """运维先 export、A 再注入同值 → B 仍可把该值当作自己的 ``explicit_env``。"""
    for name, value in zip(ENV_KEYS, (PUB, PRIV, AK)):
        monkeypatch.setenv(name, value)

    a = FosunBroker(_cfg(tmp_path, "a"), _Vault({}))
    assert a.configured is True, a.blocked_reason

    b = FosunBroker(_cfg(tmp_path, "b"), _Vault({}))
    assert b.configured is True, b.blocked_reason
    assert _sources(b) == {"PUB_KEY": "explicit_env", "PRIV_KEY": "explicit_env",
                           "API_KEY": "explicit_env"}
    assert _CRED_ENV.owners_of("FSOPENAPI_SERVER_PUBLIC_KEY") == 2


# --------------------------------------------------------------------------- #
# T21 / T22：冲突与释放
# --------------------------------------------------------------------------- #
def test_t21_two_vaults_conflict_and_keep_env(tmp_path, patch_sdk):
    a = FosunBroker(_cfg(tmp_path, "a"), _Vault({
        "FSOPENAPI_SERVER_PUBLIC_KEY": PUB,
        "FSOPENAPI_CLIENT_PRIVATE_KEY": PRIV,
        "FSOPENAPI_API_KEY": AK}))
    assert a.configured is True

    b = FosunBroker(_cfg(tmp_path, "b"), _Vault({
        "FSOPENAPI_SERVER_PUBLIC_KEY": PUB2,
        "FSOPENAPI_CLIENT_PRIVATE_KEY": PRIV2,
        "FSOPENAPI_API_KEY": AK2}))

    assert b.configured is False
    assert "CredentialConflict" in b.blocked_reason, b.blocked_reason
    assert os.environ["FSOPENAPI_SERVER_PUBLIC_KEY"] == PUB, "不得静默覆盖"
    assert os.environ["FSOPENAPI_CLIENT_PRIVATE_KEY"] == PRIV
    assert _CRED_ENV.owners_of("FSOPENAPI_SERVER_PUBLIC_KEY") == 1
    assert a.configured is True


def test_t22_release_is_refcounted_and_scoped(tmp_path, patch_sdk):
    vault = _Vault({"FSOPENAPI_SERVER_PUBLIC_KEY": PUB,
                    "FSOPENAPI_CLIENT_PRIVATE_KEY": PRIV,
                    "FSOPENAPI_API_KEY": AK})
    a = FosunBroker(_cfg(tmp_path, "a"), vault)
    b = FosunBroker(_cfg(tmp_path, "b"), vault)
    assert a.configured and b.configured
    assert _CRED_ENV.owners_of("FSOPENAPI_CLIENT_PRIVATE_KEY") == 2

    a.close()
    assert os.environ["FSOPENAPI_CLIENT_PRIVATE_KEY"] == PRIV, "B 仍持有，不得清理"
    assert _CRED_ENV.owners_of("FSOPENAPI_CLIENT_PRIVATE_KEY") == 1
    assert b.configured is True

    b.close()
    assert os.environ.get("FSOPENAPI_CLIENT_PRIVATE_KEY") is None, "归零后必须清理"
    assert _CRED_ENV.snapshot() == {}
    # 幂等：再次关闭不抛异常、不误删
    assert b.close()["credentials"]["popped"] == []


# --------------------------------------------------------------------------- #
# T23：并发初始化不得写出混合身份
# --------------------------------------------------------------------------- #
def test_t23_concurrent_init_never_mixes_identities(tmp_path, monkeypatch, patch_sdk):
    barrier = threading.Barrier(2, timeout=10)
    original = FosunBroker._inject_sdk_env

    def _sync_inject(self, creds):
        barrier.wait()
        return original(self, creds)

    monkeypatch.setattr(FosunBroker, "_inject_sdk_env", _sync_inject)

    vault_a = _Vault({"FSOPENAPI_SERVER_PUBLIC_KEY": PUB,
                      "FSOPENAPI_CLIENT_PRIVATE_KEY": PRIV,
                      "FSOPENAPI_API_KEY": AK})
    vault_b = _Vault({"FSOPENAPI_SERVER_PUBLIC_KEY": PUB2,
                      "FSOPENAPI_CLIENT_PRIVATE_KEY": PRIV2,
                      "FSOPENAPI_API_KEY": AK2})

    results: Dict[str, Any] = {}

    def _build(tag: str, vault: _Vault) -> None:
        try:
            results[tag] = FosunBroker(_cfg(tmp_path, tag), vault)
        except CredentialConflict as exc:          # pragma: no cover - 构造期不外抛
            results[tag] = exc

    t1 = threading.Thread(target=_build, args=("a", vault_a), daemon=True)
    t2 = threading.Thread(target=_build, args=("b", vault_b), daemon=True)
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    brokers = [v for v in results.values() if isinstance(v, FosunBroker)]
    assert len(brokers) == 2, results
    ok = [b for b in brokers if b.configured]
    bad = [b for b in brokers if not b.configured]
    assert len(ok) == 1, [(b.configured, b.blocked_reason) for b in brokers]
    assert len(bad) == 1
    assert "CredentialConflict" in bad[0].blocked_reason, bad[0].blocked_reason

    # 环境里必须是**一整套**同一身份，不能 A 的 PUB 配 B 的 PRIV
    # （API_KEY 按设计不进环境：SDK 用构造参数接收，见 _SDK_ENV_MAP 注释）
    env = {name: os.environ.get(name) for name in ENV_KEYS[:2]}
    pairs = [
        {"FSOPENAPI_SERVER_PUBLIC_KEY": PUB,
         "FSOPENAPI_CLIENT_PRIVATE_KEY": PRIV},
        {"FSOPENAPI_SERVER_PUBLIC_KEY": PUB2,
         "FSOPENAPI_CLIENT_PRIVATE_KEY": PRIV2},
    ]
    assert env in pairs, env
    for name in ENV_KEYS[:2]:
        assert _CRED_ENV.owners_of(name) == 1
