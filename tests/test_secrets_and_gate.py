"""测试：密钥安全管理与安全门禁。"""
from __future__ import annotations

import base64
import json
import os

import pytest

from ai_trader.config import SafetyState, build_gate, load_config
from ai_trader.secrets import (
    DPAPI_AVAILABLE,
    SecretVault,
    is_sensitive_name,
    mask,
    open_with_passphrase,
    redact,
    redact_pem,
    seal_with_passphrase,
)


# ---------------------------------------------------------------- 掩码
def test_mask_never_reveals_full_secret():
    secret = "sk-abcdefghijklmnop"
    out = mask(secret)
    assert out.startswith("sk-a")
    assert out.endswith("*")
    assert len(out) == len(secret)
    assert "abcdefghij" not in out
    assert mask("") == ""
    assert mask("ab") == "**"


@pytest.mark.parametrize("name", ["api_key", "API_KEY", "client_private_key",
                                  "web_password", "session_token", "vault_passphrase"])
def test_sensitive_name_detection(name):
    assert is_sensitive_name(name)


def test_redact_recursively_removes_secrets():
    obj = {"api_key": "abc", "nested": {"password": "p", "ok": 1},
           "list": [{"token": "t"}, {"x": "y"}]}
    out = redact(obj)
    assert out["api_key"] == "****"
    assert out["nested"]["password"] == "****"
    assert out["nested"]["ok"] == 1
    assert out["list"][0]["token"] == "****"
    assert "abc" not in json.dumps(out)


def test_redact_pem_gives_fingerprint_not_content():
    pem = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBg\n-----END PRIVATE KEY-----"
    out = redact_pem(pem)
    assert out.startswith("<pem len=")
    assert "MIIEvQIBADANBg" not in out
    assert "BEGIN PRIVATE" not in out


# ---------------------------------------------------------------- 保险箱
def test_vault_roundtrip(tmp_path):
    v = SecretVault(base_dir=tmp_path, passphrase="pw")
    v.set("FSOPENAPI_API_KEY", "secret-value-123")
    assert v.get("FSOPENAPI_API_KEY") == "secret-value-123"
    assert v.keys() == ["FSOPENAPI_API_KEY"]


def test_vault_stores_ciphertext_on_disk(tmp_path):
    v = SecretVault(base_dir=tmp_path, passphrase="pw")
    v.set("K", "PLAINTEXT-CANARY")
    raw = (tmp_path / "vault.json").read_text(encoding="utf-8")
    assert "PLAINTEXT-CANARY" not in raw
    assert any(raw.strip().startswith("{") for _ in [0])


def test_vault_delete(tmp_path):
    v = SecretVault(base_dir=tmp_path, passphrase="pw")
    v.set("A", "1")
    assert v.delete("A") is True
    assert v.get("A") is None
    assert v.delete("A") is False


def test_vault_status_reports_dpapi(tmp_path):
    v = SecretVault(base_dir=tmp_path, passphrase="pw")
    v.set("A", "1")
    st = v.status().as_dict()
    assert st["dpapi"] == DPAPI_AVAILABLE
    assert st["keys"] == ["A"]
    assert st["exists"] is True


@pytest.mark.skipif(not DPAPI_AVAILABLE, reason="仅在 Windows 上验证 DPAPI")
def test_dpapi_encrypted_value_is_user_bound_ciphertext(tmp_path):
    v = SecretVault(base_dir=tmp_path)
    v.set("K", "dpapi-secret")
    raw = json.loads((tmp_path / "vault.json").read_text(encoding="utf-8"))
    assert raw["K"].startswith("dpapi:")
    # 密文不是可读 base64 明文
    assert base64.b64encode(b"dpapi-secret").decode() not in raw["K"]
    assert v.get("K") == "dpapi-secret"


# ---------------------------------------------------------------- 迁移导出
def test_export_import_roundtrip(tmp_path):
    src = SecretVault(base_dir=tmp_path / "a", passphrase="pw")
    src.set("FSOPENAPI_API_KEY", "key-1")
    src.set("FSOPENAPI_SERVER_PUBLIC_KEY", "-----BEGIN PUBLIC KEY-----\nAA\n-----END PUBLIC KEY-----")
    blob = src.export_encrypted("migrate-pass")
    assert b"key-1" not in blob

    dst = SecretVault(base_dir=tmp_path / "b", passphrase="pw")
    imported = dst.import_encrypted(blob, "migrate-pass")
    assert set(imported) == {"FSOPENAPI_API_KEY", "FSOPENAPI_SERVER_PUBLIC_KEY"}
    assert dst.get("FSOPENAPI_API_KEY") == "key-1"


def test_export_import_wrong_passphrase_fails(tmp_path):
    src = SecretVault(base_dir=tmp_path, passphrase="pw")
    src.set("A", "1")
    blob = src.export_encrypted("right")
    dst = SecretVault(base_dir=tmp_path / "b", passphrase="pw")
    with pytest.raises(Exception):
        dst.import_encrypted(blob, "wrong")


def test_seal_open_with_passphrase():
    blob = seal_with_passphrase(b"hello", "pw")
    assert blob.startswith(b"FSTV1")
    assert open_with_passphrase(blob, "pw") == b"hello"
    with pytest.raises(Exception):
        open_with_passphrase(blob, "nope")


def test_inject_into_environ_only_affects_process(tmp_path, monkeypatch):
    v = SecretVault(base_dir=tmp_path, passphrase="pw")
    v.set("FSOPENAPI_API_KEY", "k1")
    v.set("FSOPENAPI_SERVER_PUBLIC_KEY", "pem1")
    monkeypatch.delenv("FSOPENAPI_API_KEY", raising=False)
    monkeypatch.delenv("FSOPENAPI_SERVER_PUBLIC_KEY", raising=False)
    injected = v.inject_into_environ()
    assert "FSOPENAPI_API_KEY" in injected
    assert os.environ["FSOPENAPI_API_KEY"] == "k1"
    # 未请求的键不会被注入
    assert "FSOPENAPI_CLIENT_PRIVATE_KEY" not in injected


# ---------------------------------------------------------------- 安全门禁
def test_default_config_is_safe():
    cfg = load_config(use_dotenv=False)
    s = cfg.gate.state
    assert s.paper_trading is True
    assert s.live_trading is False
    assert s.order_execution_enabled is False
    assert cfg.gate.can_connect_broker()[0] is False
    assert cfg.gate.can_send_real_order()[0] is False


def test_live_requires_ack_phrase():
    gate = build_gate({
        "safety": {"live_trading": True, "paper_trading": False,
                   "order_execution_enabled": True, "kill_switch": False,
                   "live_ack_phrase": "", "required_live_ack": "I UNDERSTAND REAL MONEY IS AT RISK"}
    })
    assert gate.can_connect_broker()[0] is False
    assert "LIVE_ACK_PHRASE" in gate.can_connect_broker()[1]


def test_live_with_ack_allows_connect_but_order_needs_second_switch():
    gate = build_gate({
        "safety": {"live_trading": True, "paper_trading": False,
                   "order_execution_enabled": False, "kill_switch": False,
                   "live_ack_phrase": "I UNDERSTAND REAL MONEY IS AT RISK"}
    })
    assert gate.can_connect_broker()[0] is True
    assert gate.can_send_real_order()[0] is False
    assert "ORDER_EXECUTION_ENABLED" in gate.can_send_real_order()[1]


def test_full_live_requires_all_three():
    gate = build_gate({
        "safety": {"live_trading": True, "paper_trading": False,
                   "order_execution_enabled": True, "kill_switch": False,
                   "live_ack_phrase": "I UNDERSTAND REAL MONEY IS AT RISK"}
    })
    assert gate.can_send_real_order()[0] is True


def test_kill_switch_overrides_everything():
    gate = build_gate({
        "safety": {"live_trading": True, "paper_trading": False,
                   "order_execution_enabled": True, "kill_switch": True,
                   "live_ack_phrase": "I UNDERSTAND REAL MONEY IS AT RISK"}
    })
    assert gate.can_connect_broker()[0] is False
    assert gate.can_send_real_order()[0] is False


def test_paper_and_live_conflict_resolves_to_paper():
    gate = build_gate({
        "safety": {"live_trading": True, "paper_trading": True,
                   "order_execution_enabled": True, "kill_switch": False,
                   "live_ack_phrase": "I UNDERSTAND REAL MONEY IS AT RISK"}
    })
    assert gate.state.paper_trading is True
    assert gate.state.live_trading is False
    assert gate.state.order_execution_enabled is False


def test_env_override_cannot_silently_enable_live(monkeypatch):
    """即使环境变量写 true，只要还缺确认短语，仍不可真实下单。"""
    monkeypatch.setenv("LIVE_TRADING", "true")
    monkeypatch.setenv("ORDER_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("PAPER_TRADING", "false")
    monkeypatch.delenv("LIVE_ACK_PHRASE", raising=False)
    cfg = load_config(use_dotenv=False)
    assert cfg.gate.can_send_real_order()[0] is False


# ---------------------------------------------------------------- 口令来源（P1-10）
def test_passphrase_from_env_not_command_line(monkeypatch):
    """口令应可只通过环境变量传入，避免出现在进程命令行。"""
    from ai_trader import cli

    class Args:
        passphrase = None
        passphrase_env = "VAULT_PASSPHRASE"

    monkeypatch.setenv("VAULT_PASSPHRASE", "from-env-123456")
    assert cli._resolve_passphrase(Args()) == "from-env-123456"

    Args.passphrase = "from-argv"
    assert cli._resolve_passphrase(Args()) == "from-argv", "显式参数优先"


def test_passphrase_absent_everywhere_is_empty(monkeypatch):
    from ai_trader import cli

    class Args:
        passphrase = None
        passphrase_env = "VAULT_PASSPHRASE"

    monkeypatch.delenv("VAULT_PASSPHRASE", raising=False)
    assert cli._resolve_passphrase(Args()) == ""
