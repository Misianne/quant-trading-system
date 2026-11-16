"""US-P1 Batch 4.1.2 / B-1 定点修复测试（AUD2-B1-01 / AUD2-B1-02）。

第二轮独立审计点名的两条遗留缺陷：

* **AUD2-B1-01**：运维显式 export 的值与 ``.env`` **完全相同**时被判成
  ``dotenv``，于是 explicit_env 分支被跳过、Vault 越级胜出 —— 显式环境变量
  的最高优先级失效（SR-B1-04 的残留）。
* **AUD2-B1-02**：``dotenv`` 与 ``explicit_env`` 混用被判 ``mixed``，真实轮换
  场景（``.env`` 保留未变的公钥、只 export 轮换后的私钥与 API_KEY）无法启动。

修复口径
--------
1. 来源判定从"值指纹"升级为**加载时来源元数据**：键名 + 载入后是否被外部
   重写过（写入日志）+ 值指纹 + 对象标记。同值重写也能被识别。
2. 同源校验改为按**信任域**判定：``dotenv`` 与 ``explicit_env`` 同属
   ``local``（运维本地配置），只有 ``vault`` ↔ 本地配置这种**跨域**才拒绝。

约束：全部使用随机伪造凭据；不读真实 ``.env``；不连网络；不写仓库文件；
只断言来源/完整性字段与"SDK 实际读到什么"，**绝不**打印任何密钥值。
"""
from __future__ import annotations

import os
import pathlib
import sys
import uuid
from typing import Any, Dict, Optional

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from ai_trader import config as cfg_mod  # noqa: E402
from ai_trader.broker.fosun_adapter import (  # noqa: E402
    SRC_DOTENV,
    SRC_EXPLICIT_ENV,
    SRC_MISSING,
    SRC_VAULT,
    FosunBroker,
    _CRED_ENV,
)

from tests.helpers import live_cfg  # noqa: E402

ENV_KEYS = ("FSOPENAPI_SERVER_PUBLIC_KEY", "FSOPENAPI_CLIENT_PRIVATE_KEY",
            "FSOPENAPI_API_KEY")

#: 短名 → 环境变量名（断言来源时用短名）
SHORT = {"PUB_KEY": ENV_KEYS[0], "PRIV_KEY": ENV_KEYS[1], "API_KEY": ENV_KEYS[2]}


def _token(prefix: str) -> str:
    """随机伪造值（单行，能被 ``load_dotenv`` 完整解析）。"""
    return f"{prefix}-{uuid.uuid4().hex}{uuid.uuid4().hex}"


def _pem(kind: str) -> str:
    body = uuid.uuid4().hex + uuid.uuid4().hex
    return f"-----BEGIN {kind}-----\n{body}\n-----END {kind}-----"


class _Vault:
    def __init__(self, data: Optional[Dict[str, str]] = None) -> None:
        self.data = dict(data or {})

    def get(self, name: str, default: Optional[str] = None) -> Optional[str]:
        return self.data.get(name, default)


class _CapturingSDK:
    """记录 SDK 真正读到的身份（身份密钥只从进程环境读，api_key 走构造参数）。"""

    last: Dict[str, Any] = {}

    def __init__(self, **kw: Any) -> None:
        type(self).last = {
            "api_key": kw.get("api_key"),
            "server_public_key": os.environ.get(ENV_KEYS[0]),
            "client_private_key": os.environ.get(ENV_KEYS[1]),
        }
        self._http_session = type("_S", (), {"request": lambda *a, **k: None})()


@pytest.fixture(autouse=True)
def _clean_credential_env(monkeypatch):
    """清空身份变量与三个进程级登记表；测试后原样还原。"""
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


def _write_env(tmp_path, pub: str, priv: str, ak: str,
               name: str = "fake.env") -> pathlib.Path:
    p = tmp_path / name
    p.write_text(
        f"{ENV_KEYS[0]}={pub}\n{ENV_KEYS[1]}={priv}\n{ENV_KEYS[2]}={ak}\n",
        encoding="utf-8")
    return p


def _sources(broker: FosunBroker) -> Dict[str, str]:
    return dict(broker.health()["credential_sources"])


def _validation(broker: FosunBroker) -> Dict[str, Any]:
    return dict(broker.health()["credential_validation"])


def _vault_of(pub: str, priv: str, ak: str) -> _Vault:
    return _Vault({ENV_KEYS[0]: pub, ENV_KEYS[1]: priv, ENV_KEYS[2]: ak})


# ======================================================================== #
# AUD2-B1-01：显式 env 与 .env 同值时仍必须判 explicit_env，Vault 不得越级
# ======================================================================== #
def test_b101_untouched_dotenv_stays_dotenv(tmp_path, patch_sdk):
    """基线：没有任何人改写 → 仍然是 dotenv（不能把 dotenv 全判成 explicit）。"""
    pub, priv, ak = _token("BK1-PUB"), _token("BK1-PRIV"), _token("BK1-AK")
    assert cfg_mod.load_dotenv(_write_env(tmp_path, pub, priv, ak)) is True

    for name, value in zip(ENV_KEYS, (pub, priv, ak)):
        assert cfg_mod.dotenv_origin(name, value) is True

    broker = FosunBroker(_cfg(tmp_path), _Vault({}))
    assert broker.configured is True, broker.blocked_reason
    assert _sources(broker) == {"PUB_KEY": SRC_DOTENV, "PRIV_KEY": SRC_DOTENV,
                                "API_KEY": SRC_DOTENV}
    broker.close()


def test_b101_same_value_rewrite_is_no_longer_dotenv(tmp_path):
    """同值重写 → 来源元数据翻转（值指纹本身无法区分，靠写入日志）。"""
    pub, priv, ak = _token("BK1R-PUB"), _token("BK1R-PRIV"), _token("BK1R-AK")
    assert cfg_mod.load_dotenv(_write_env(tmp_path, pub, priv, ak)) is True
    for name, value in zip(ENV_KEYS, (pub, priv, ak)):
        assert cfg_mod.dotenv_origin(name, value) is True

    # 运维显式 export **完全相同**的值（普通 str 赋值）
    for name, value in zip(ENV_KEYS, (pub, priv, ak)):
        os.environ[name] = value

    for name, value in zip(ENV_KEYS, (pub, priv, ak)):
        assert cfg_mod.dotenv_origin(name, value) is False, "同值重写仍被判 dotenv"

    diag = cfg_mod.dotenv_source_diagnostics()
    assert diag["write_tracker_installed"] is True, diag
    assert diag["touched_keys"] == len(ENV_KEYS), diag
    # 诊断信息不得含键名或值
    assert all(not isinstance(v, str) or v.isdigit() or "BK1" not in v
               for v in diag.values())


def test_b101_same_value_explicit_env_beats_vault(tmp_path, patch_sdk):
    """审计原始复现：同值显式 export + 另一套值的 Vault → 必须取 env 值。"""
    pub_d, priv_d, ak_d = _token("BK1D-PUB"), _token("BK1D-PRIV"), _token("BK1D-AK")
    pub_v, priv_v, ak_v = _pem("PUBLIC KEY"), _pem("PRIVATE KEY"), _token("BK1V-AK")
    assert cfg_mod.load_dotenv(_write_env(tmp_path, pub_d, priv_d, ak_d)) is True
    for name, value in zip(ENV_KEYS, (pub_d, priv_d, ak_d)):
        os.environ[name] = value                      # 运维显式 export 同值

    broker = FosunBroker(_cfg(tmp_path), _vault_of(pub_v, priv_v, ak_v))

    assert broker.configured is True, broker.blocked_reason
    assert _sources(broker) == {"PUB_KEY": SRC_EXPLICIT_ENV,
                                "PRIV_KEY": SRC_EXPLICIT_ENV,
                                "API_KEY": SRC_EXPLICIT_ENV}, "Vault 越级覆盖了显式 env"
    assert _CapturingSDK.last["client_private_key"] == priv_d, "SDK 拿到了 Vault 的值"
    assert _CapturingSDK.last["api_key"] == ak_d
    validation = _validation(broker)
    assert validation["status"] == "ok"
    assert validation["domain"] == "local"
    broker.close()


def test_b101_same_value_explicit_env_label_without_vault(tmp_path, patch_sdk):
    """无 Vault 时取值本正确，来源标签也必须是 explicit_env（审计 B1-03）。"""
    pub, priv, ak = _token("BK1N-PUB"), _token("BK1N-PRIV"), _token("BK1N-AK")
    assert cfg_mod.load_dotenv(_write_env(tmp_path, pub, priv, ak)) is True
    for name, value in zip(ENV_KEYS, (pub, priv, ak)):
        os.environ[name] = value

    broker = FosunBroker(_cfg(tmp_path), _Vault({}))

    assert broker.configured is True, broker.blocked_reason
    assert _sources(broker) == {"PUB_KEY": SRC_EXPLICIT_ENV,
                                "PRIV_KEY": SRC_EXPLICIT_ENV,
                                "API_KEY": SRC_EXPLICIT_ENV}
    assert _CapturingSDK.last["client_private_key"] == priv
    broker.close()


def test_b101_changed_value_is_explicit_env(tmp_path, patch_sdk):
    """既有语义回归：被改成**不同**值 → explicit_env。"""
    pub, priv, ak = _token("BK1C-PUB"), _token("BK1C-PRIV"), _token("BK1C-AK")
    assert cfg_mod.load_dotenv(_write_env(tmp_path, pub, priv, ak)) is True
    pub2, priv2, ak2 = _token("BK1C2-PUB"), _token("BK1C2-PRIV"), _token("BK1C2-AK")
    for name, value in zip(ENV_KEYS, (pub2, priv2, ak2)):
        os.environ[name] = value

    for name, value in zip(ENV_KEYS, (pub2, priv2, ak2)):
        assert cfg_mod.dotenv_origin(name, value) is False

    broker = FosunBroker(_cfg(tmp_path), _Vault({}))
    assert broker.configured is True, broker.blocked_reason
    assert set(_sources(broker).values()) == {SRC_EXPLICIT_ENV}
    assert _CapturingSDK.last["client_private_key"] == priv2
    broker.close()


def test_b101_inherited_process_env_is_explicit_not_dotenv(tmp_path, monkeypatch,
                                                           patch_sdk):
    """进程启动前就已存在的变量（真实 ``export``）→ explicit_env，且不被 .env 覆盖。"""
    pub, priv, ak = _token("BK1I-PUB"), _token("BK1I-PRIV"), _token("BK1I-AK")
    for name, value in zip(ENV_KEYS, (pub, priv, ak)):
        monkeypatch.setenv(name, value)

    # .env 里是同一批值：load_dotenv 只 setdefault，一个键都不写
    assert cfg_mod.load_dotenv(_write_env(tmp_path, pub, priv, ak)) is False
    assert cfg_mod.dotenv_keys() == frozenset()

    broker = FosunBroker(_cfg(tmp_path), _Vault({}))
    assert broker.configured is True, broker.blocked_reason
    assert set(_sources(broker).values()) == {SRC_EXPLICIT_ENV}
    broker.close()


def test_b101_identity_fallback_when_write_tracker_unavailable(tmp_path, monkeypatch):
    """写入日志不可用时，仍靠**对象身份**识别同值改写（不得放行明文来源误判）。"""
    pub, priv, ak = _token("BK1F-PUB"), _token("BK1F-PRIV"), _token("BK1F-AK")
    assert cfg_mod.load_dotenv(_write_env(tmp_path, pub, priv, ak)) is True

    # 模拟"包装失败"：只看对象身份，不看写入日志
    monkeypatch.setattr(cfg_mod, "_ENVIRON_TRACKER",
                        {"installed": False, "reason": "simulated", "original": None})
    cfg_mod._DOTENV_TOUCHED.clear()

    if cfg_mod._environ_storage_kind() == "str":
        # Windows：os.environ 内部保留原对象 → 未改写时仍可判 dotenv
        assert cfg_mod.dotenv_origin(ENV_KEYS[0], pub) is True
    os.environ[ENV_KEYS[0]] = pub            # 同值重写，对象被换掉
    cfg_mod._DOTENV_TOUCHED.clear()          # 排除写入日志，只考察身份判据
    assert cfg_mod.dotenv_origin(ENV_KEYS[0], pub) is False


# ======================================================================== #
# AUD2-B1-02：dotenv + explicit 混用（真实轮换）必须能启动
# ======================================================================== #
def _rotate(tmp_path, monkeypatch):
    """``.env`` 保留未变的公钥，只 export 轮换后的私钥与 API_KEY。"""
    pub, priv_old, ak_old = _token("BK2-PUB"), _token("BK2-OLD-PRIV"), _token("BK2-OLD-AK")
    priv_new, ak_new = _token("BK2-NEW-PRIV"), _token("BK2-NEW-AK")
    assert cfg_mod.load_dotenv(_write_env(tmp_path, pub, priv_old, ak_old)) is True
    monkeypatch.setenv(ENV_KEYS[1], priv_new)
    monkeypatch.setenv(ENV_KEYS[2], ak_new)
    return pub, priv_new, ak_new


def test_b102_rotation_scenario_starts(tmp_path, monkeypatch, patch_sdk):
    """审计原始复现：轮换场景曾被判 mixed 而拒绝启动。"""
    pub, priv_new, ak_new = _rotate(tmp_path, monkeypatch)

    broker = FosunBroker(_cfg(tmp_path), _Vault({}))

    assert broker.configured is True, broker.blocked_reason
    assert broker.client is not None
    broker.close()


def test_b102_rotation_sources_and_domain_fields(tmp_path, monkeypatch, patch_sdk):
    """域内混用：不阻止启动，但必须如实留痕，不得悄悄当成"同源"。"""
    _rotate(tmp_path, monkeypatch)

    broker = FosunBroker(_cfg(tmp_path), _Vault({}))
    validation = _validation(broker)

    assert _sources(broker) == {"PUB_KEY": SRC_DOTENV,
                                "PRIV_KEY": SRC_EXPLICIT_ENV,
                                "API_KEY": SRC_EXPLICIT_ENV}
    assert validation["status"] == "ok"
    assert validation["mixed"] is False, "域内混用不应判成跨域混合"
    assert validation["mixed_within_domain"] is True
    assert validation["domain"] == "local"
    assert validation["identity_consistent"] is True
    assert validation["complete"] is True
    assert "本地配置" in validation["detail"]
    broker.close()


def test_b102_rotation_sdk_gets_rotated_identity(tmp_path, monkeypatch, patch_sdk):
    """混用放行不等于"随便取一个值"：三要素必须都按各自来源正确取值。"""
    pub, priv_new, ak_new = _rotate(tmp_path, monkeypatch)

    broker = FosunBroker(_cfg(tmp_path), _Vault({}))

    assert broker.configured is True, broker.blocked_reason
    assert _CapturingSDK.last["server_public_key"] == pub, "公钥应从 .env 取"
    assert _CapturingSDK.last["client_private_key"] == priv_new, "私钥应取轮换后的值"
    assert _CapturingSDK.last["api_key"] == ak_new
    broker.close()


def test_b102_cross_domain_explicit_plus_vault_fails_closed(tmp_path, monkeypatch,
                                                            patch_sdk):
    """跨域（显式 env + Vault）仍然 fail-closed —— 同源校验没有被削弱。"""
    pub_a = _pem("PUBLIC KEY")
    monkeypatch.setenv(ENV_KEYS[0], pub_a)
    vault = _Vault({ENV_KEYS[1]: _pem("PRIVATE KEY"), ENV_KEYS[2]: _token("BK2V-AK")})

    broker = FosunBroker(_cfg(tmp_path), vault)

    assert broker.configured is False
    assert broker.client is None
    assert "CREDENTIAL_SOURCE_MIX" in broker.blocked_reason, broker.blocked_reason
    assert _CapturingSDK.last == {}, "混合身份绝不能构造 SDK"
    validation = _validation(broker)
    assert validation["mixed"] is True
    assert validation["domain"] == "mixed"
    assert validation["identity_consistent"] is False


def test_b102_cross_domain_dotenv_plus_vault_fails_closed(tmp_path, patch_sdk):
    """跨域（dotenv + Vault）同样 fail-closed：Vault 只覆盖自己有的要素会撕裂身份。"""
    pub, priv, ak = _token("BK2X-PUB"), _token("BK2X-PRIV"), _token("BK2X-AK")
    assert cfg_mod.load_dotenv(_write_env(tmp_path, pub, priv, ak)) is True
    vault = _Vault({ENV_KEYS[1]: _pem("PRIVATE KEY"), ENV_KEYS[2]: _token("BK2Y-AK")})

    broker = FosunBroker(_cfg(tmp_path), vault)

    assert broker.configured is False
    assert "CREDENTIAL_SOURCE_MIX" in broker.blocked_reason, broker.blocked_reason
    assert _CapturingSDK.last == {}
    assert _validation(broker)["domain"] == "mixed"


def test_b102_incomplete_still_fail_closed(tmp_path, patch_sdk):
    """域内混用只在"三要素齐全"时才放行；缺一个仍必须拒绝。"""
    pub = _token("BK2M-PUB")
    p = tmp_path / "partial.env"
    p.write_text(f"{ENV_KEYS[0]}={pub}\n", encoding="utf-8")
    assert cfg_mod.load_dotenv(p) is True

    broker = FosunBroker(_cfg(tmp_path), _Vault({}))

    assert broker.configured is False
    assert "CREDENTIAL_INCOMPLETE" in broker.blocked_reason, broker.blocked_reason
    validation = _validation(broker)
    assert validation["complete"] is False
    assert validation["identity_consistent"] is False


def test_b102_all_dotenv_is_not_mixed_within_domain(tmp_path, patch_sdk):
    pub, priv, ak = _token("BK2D-PUB"), _token("BK2D-PRIV"), _token("BK2D-AK")
    assert cfg_mod.load_dotenv(_write_env(tmp_path, pub, priv, ak)) is True

    broker = FosunBroker(_cfg(tmp_path), _Vault({}))
    validation = _validation(broker)

    assert broker.configured is True, broker.blocked_reason
    assert validation["mixed"] is False
    assert validation["mixed_within_domain"] is False
    assert validation["domain"] == "local"
    assert validation["detail"] == ""
    broker.close()


def test_b102_all_explicit_is_local_and_consistent(tmp_path, monkeypatch, patch_sdk):
    for name in ENV_KEYS:
        monkeypatch.setenv(name, _token("BK2E"))
    # 公钥/私钥用 PEM 形态更贴近真实（api_key 仍是随机串）
    monkeypatch.setenv(ENV_KEYS[0], _pem("PUBLIC KEY"))
    monkeypatch.setenv(ENV_KEYS[1], _pem("PRIVATE KEY"))

    broker = FosunBroker(_cfg(tmp_path), _Vault({}))
    validation = _validation(broker)

    assert broker.configured is True, broker.blocked_reason
    assert validation["status"] == "ok"
    assert validation["domain"] == "local"
    assert validation["identity_consistent"] is True
    assert validation["mixed"] is False
    assert validation["mixed_within_domain"] is False
    assert broker.health()["credential_mixed"] is False
    broker.close()


def test_b102_identity_consistent_flag_tracks_status(tmp_path, monkeypatch, patch_sdk):
    """"启动成功就忽略身份一致性"是禁止的：标志必须与真实判定一致。"""
    ok_broker = FosunBroker(_cfg(tmp_path, "ok1"), _Vault({
        ENV_KEYS[0]: _pem("PUBLIC KEY"),
        ENV_KEYS[1]: _pem("PRIVATE KEY"),
        ENV_KEYS[2]: _token("BK2F-AK")}))
    assert ok_broker.configured is True, ok_broker.blocked_reason
    assert _validation(ok_broker)["identity_consistent"] is True
    ok_broker.close()

    monkeypatch.setenv(ENV_KEYS[0], _pem("PUBLIC KEY"))
    bad_broker = FosunBroker(_cfg(tmp_path, "bad1"), _Vault({
        ENV_KEYS[1]: _pem("PRIVATE KEY"), ENV_KEYS[2]: _token("BK2G-AK")}))
    assert bad_broker.configured is False
    assert _validation(bad_broker)["identity_consistent"] is False


def test_b102_validate_source_model_matrix():
    """判定矩阵（单元级）：域内 → ok；跨域 → mixed；缺要素 → incomplete。"""
    keys = ENV_KEYS
    cases = [
        ((SRC_DOTENV, SRC_DOTENV, SRC_DOTENV), "ok", False),
        ((SRC_DOTENV, SRC_EXPLICIT_ENV, SRC_EXPLICIT_ENV), "ok", False),
        ((SRC_EXPLICIT_ENV, SRC_EXPLICIT_ENV, SRC_EXPLICIT_ENV), "ok", False),
        ((SRC_VAULT, SRC_VAULT, SRC_VAULT), "ok", False),
        ((SRC_DOTENV, SRC_VAULT, SRC_VAULT), "mixed", True),
        ((SRC_EXPLICIT_ENV, SRC_VAULT, SRC_MISSING), "mixed", True),
        ((SRC_DOTENV, SRC_MISSING, SRC_MISSING), "incomplete", False),
    ]
    for values, expect_status, expect_mixed in cases:
        sources = dict(zip(keys, values))
        status, detail, missing, mixed = FosunBroker._validate_source_model(sources)
        assert status == expect_status, (values, status, detail)
        assert mixed is expect_mixed, (values, mixed)
        if expect_status == "incomplete":
            assert len(missing) == 2
        if expect_status == "mixed":
            assert "跨信任域" in detail

    assert FosunBroker._source_domain(dict(zip(keys, (SRC_DOTENV, SRC_DOTENV,
                                                     SRC_EXPLICIT_ENV)))) == "local"
    assert FosunBroker._source_domain(dict(zip(keys, (SRC_DOTENV, SRC_VAULT,
                                                     SRC_VAULT)))) == "mixed"
    assert FosunBroker._source_domain(dict(zip(keys, (SRC_MISSING, SRC_MISSING,
                                                     SRC_MISSING)))) == "none"


# ======================================================================== #
# T17 / T20 原始意图恢复（不得以重定义冒充 PASS，不得开放不安全回退）
# ======================================================================== #
def test_t17_original_intent_policy_warn_is_blocked_not_bypassed(tmp_path, monkeypatch,
                                                                 patch_sdk):
    """T17 原始意图（`REMEDIATION_PLAN.md:1041`）：``policy=warn`` 时
    ``credential_mixed=true``。

    判定：**BLOCKED（等待决策）** + 可见性一半 PASS。

    * "混合状态如实可见"：已实现，本用例验证，并验证**任何配置都掩盖不了它**；
    * "warn 降级放行"：**故意不实现**。它等于提供一个绕过同源校验的开关，会让
      "一半来自 Vault、一半来自本地"的混合身份成功初始化，与 fail-closed 直接
      冲突。按本轮"不得为通过而开放不安全回退"的要求，本用例**反向钉死**该行为：
      即使配置里写了 warn，跨域混合仍然拒绝初始化。

    因此 T17 不计 PASS：可见性 PASS、原计划的 warn 语义 BLOCKED，等运维决策。
    """
    monkeypatch.setenv(ENV_KEYS[0], _pem("PUBLIC KEY"))
    cfg = live_cfg(tmp_path / "t17", **{
        "fosun.base_url": "https://x.test",
        # 伪造"有人已经加了 warn 开关"：适配器不认这两个键，绝不能因此放行
        "fosun.credential_source_policy": "warn",
        "fosun.credential_mixed_policy": "warn",
    })
    vault = _Vault({ENV_KEYS[1]: _pem("PRIVATE KEY"), ENV_KEYS[2]: _token("T17-AK")})

    broker = FosunBroker(cfg, vault)

    assert broker.configured is False, "warn 语义不得放行混合身份"
    assert broker.client is None
    assert "CREDENTIAL_SOURCE_MIX" in broker.blocked_reason, broker.blocked_reason
    health = broker.health()
    assert health["credential_mixed"] is True, "混合状态必须如实可见"
    assert health["credential_validation"]["mixed"] is True
    assert health["credential_validation"]["domain"] == "mixed"

    # 放行通道根本不存在：构造参数里没有任何 policy/allow/force 之类的口子
    import inspect

    params = set(inspect.signature(FosunBroker.__init__).parameters)
    risky = {p for p in params
             if any(k in p.lower() for k in ("policy", "allow", "force", "ignore"))}
    assert not risky, f"出现可疑的放行参数：{risky}"


def test_t20_original_intent_reuse_switch_absent_but_stricter(tmp_path, monkeypatch,
                                                              patch_sdk):
    """T20 原始意图（`REMEDIATION_PLAN.md:1044`）：
    "显式开关开启且三要素齐全才复用 + 告警"。

    判定：**PARTIAL / 与实现冲突（实现更严格）**，差异在报告中列出，等决策。

    实现现状：**不存在**任何"复用他人注入身份"的开关，复用被无条件禁止。
    原断言中"开关开启 → 可以复用"这条路径在实现里不存在，且**不能**为了对齐
    计划而新增（新增即重新打开 SR-B1-05：A 释放后 B 会以 A 的身份继续跑）。

    本用例保留原始意图的**安全实质**，不降低任何既有断言：
    1. 环境里"看起来有"PUB/PRIV（其实是 A 注入的）时，B 仍不得把它们当自己的；
    2. B 自己提供了 API_KEY（三要素里有一项是自己配的）也不行；
    3. 不存在能打开复用的开关 / 参数 / 全局常量；
    4. 运维**自己 export** 的值不受影响（4.1.1 已覆盖的真实需求，这里只做附加
       确认，不替代 `test_t20_operator_exported_value_is_not_mistaken_for_injection`）。
    """
    import inspect

    import ai_trader.broker.fosun_adapter as fa

    pub, priv, ak = _pem("PUBLIC KEY"), _pem("PRIVATE KEY"), _token("T20-AK")
    a = FosunBroker(_cfg(tmp_path, "a"), _vault_of(pub, priv, ak))
    assert a.configured is True, a.blocked_reason
    assert _CRED_ENV.owners_of(ENV_KEYS[1]) == 1

    # B 自己额外配了 API_KEY：环境里三要素"都有值"，但 PUB/PRIV 是 A 注入的
    monkeypatch.setenv(ENV_KEYS[2], _token("T20-B-AK"))
    b = FosunBroker(_cfg(tmp_path, "b"), _Vault({}))

    assert b.configured is False, "B 复用了 A 注入的身份（SR-B1-05 回归）"
    assert "CREDENTIAL_INCOMPLETE" in b.blocked_reason, b.blocked_reason
    assert b.client is None
    assert _CRED_ENV.owners_of(ENV_KEYS[1]) == 1, "B 不得接管引用"

    # 复用通道不存在：无参数、无全局开关
    params = set(inspect.signature(FosunBroker.__init__).parameters)
    assert not {p for p in params
                if any(k in p.lower() for k in ("reuse", "inherit", "share", "adopt"))}
    assert not [n for n in dir(fa)
                if any(k in n.upper() for k in ("REUSE", "INHERIT", "ADOPT"))]

    a.close()

    # 运维自己 export 的值仍按 explicit_env 处理（不误杀）
    for name, value in zip(ENV_KEYS, (_pem("PUBLIC KEY"), _pem("PRIVATE KEY"),
                                      _token("T20-C-AK"))):
        monkeypatch.setenv(name, value)
    c = FosunBroker(_cfg(tmp_path, "c"), _Vault({}))
    assert c.configured is True, c.blocked_reason
    assert set(_sources(c).values()) == {SRC_EXPLICIT_ENV}
    c.close()

