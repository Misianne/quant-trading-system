"""密钥安全管理模块。

三层策略
--------
1. **不落明文**：密钥默认只存 DPAPI 加密后的密文（Windows 用户级绑定）。
2. **不写环境**：本模块从不把密钥写入 ``os.environ`` 之外的地方，
   也不会修改系统全局环境变量。
3. **可迁移**：提供口令派生的加密导出包（scrypt + AES-256-GCM），
   便于 VPS 之间搬迁；导出包离开原机器后 DPAPI 层即失效。

同时提供 ``redact`` 系列工具，保证日志/Web/审计里永不出现明文。

非 Windows 环境（如 CI）自动降级为 ``FernetOnlyVault``：
仅做口令派生加密，并在 :meth:`status` 中明确标记 ``dpapi=False``。
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import secrets as _secrets
import sys
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

VAULT_FILENAME = "vault.json"
SCRYPT_N = 2 ** 15
SCRYPT_R = 8
SCRYPT_P = 1


# --------------------------------------------------------------------------- #
# 敏感信息脱敏
# --------------------------------------------------------------------------- #

SENSITIVE_HINTS = (
    "key", "secret", "token", "password", "passphrase", "private",
    "credential", "api_key", "apikey", "pem",
)


def mask(value: Optional[str], keep: int = 4) -> str:
    """把密钥变成 ``abcd****`` 形式，供界面展示。"""
    if not value:
        return ""
    s = str(value)
    if len(s) <= keep:
        return "*" * len(s)
    return s[:keep] + "*" * max(4, len(s) - keep)


def is_sensitive_name(name: str) -> bool:
    low = name.lower()
    return any(h in low for h in SENSITIVE_HINTS)


def redact(obj, _depth: int = 0):
    """递归脱敏 dict/list，用于日志与 Web 输出。"""
    if _depth > 8:
        return "<deep>"
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if is_sensitive_name(str(k)) and isinstance(v, (str, bytes)):
                out[k] = "<redacted>" if not v else "****"
            else:
                out[k] = redact(v, _depth + 1)
        return out
    if isinstance(obj, list):
        return [redact(v, _depth + 1) for v in obj]
    if isinstance(obj, str) and len(obj) > 400:
        return obj[:400] + "...<truncated>"
    return obj


def redact_pem(text: Optional[str]) -> str:
    """把 PEM 内容压缩成指纹式摘要：不泄露任何密钥字节。"""
    if not text:
        return ""
    body = "".join(str(text).split())
    return f"<pem len={len(body)} sha8={__import__('hashlib').sha256(body.encode()).hexdigest()[:8]}>"


# --------------------------------------------------------------------------- #
# Windows DPAPI
# --------------------------------------------------------------------------- #

if sys.platform == "win32":
    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.windll.crypt32
    _kernel32 = ctypes.windll.kernel32

    def _blob(data: bytes) -> DATA_BLOB:
        buf = ctypes.create_string_buffer(data, len(data))
        return DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    def _blob_bytes(blob: DATA_BLOB) -> bytes:
        return ctypes.string_at(blob.pbData, blob.cbData)

    def _local_free(blob: DATA_BLOB) -> None:
        if blob.pbData:
            _kernel32.LocalFree(blob.pbData)

    def dpapi_encrypt(plaintext: bytes) -> bytes:
        src = _blob(plaintext)
        dst = DATA_BLOB()
        if not _crypt32.CryptProtectData(
            ctypes.byref(src), "ai_trader_vault", None, None, None, 0, ctypes.byref(dst)
        ):
            raise OSError("CryptProtectData 失败，无法加密密钥")
        try:
            return _blob_bytes(dst)
        finally:
            _local_free(dst)

    def dpapi_decrypt(ciphertext: bytes) -> bytes:
        src = _blob(ciphertext)
        dst = DATA_BLOB()
        if not _crypt32.CryptUnprotectData(
            ctypes.byref(src), None, None, None, None, 0, ctypes.byref(dst)
        ):
            raise OSError("CryptUnprotectData 失败（密文不属于当前 Windows 用户/机器）")
        try:
            return _blob_bytes(dst)
        finally:
            _local_free(dst)

    DPAPI_AVAILABLE = True
else:  # pragma: no cover - 仅非 Windows 开发/CI
    def dpapi_encrypt(plaintext: bytes) -> bytes:
        raise OSError("DPAPI 仅 Windows 可用")

    def dpapi_decrypt(ciphertext: bytes) -> bytes:
        raise OSError("DPAPI 仅 Windows 可用")

    DPAPI_AVAILABLE = False


# --------------------------------------------------------------------------- #
# 口令派生加密（跨机器迁移）
# --------------------------------------------------------------------------- #

def _derive_key(passphrase: str, salt: bytes) -> bytes:
    kdf = Scrypt(salt=salt, length=32, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    return kdf.derive(passphrase.encode("utf-8"))


def seal_with_passphrase(plaintext: bytes, passphrase: str) -> bytes:
    salt = _secrets.token_bytes(16)
    nonce = _secrets.token_bytes(12)
    key = _derive_key(passphrase, salt)
    ct = AESGCM(key).encrypt(nonce, plaintext, b"ai_trader_vault_v1")
    return b"FSTV1" + salt + nonce + ct


def open_with_passphrase(blob: bytes, passphrase: str) -> bytes:
    if not blob.startswith(b"FSTV1"):
        raise ValueError("不是有效的导出包格式")
    body = blob[5:]
    salt, nonce, ct = body[:16], body[16:28], body[28:]
    key = _derive_key(passphrase, salt)
    return AESGCM(key).decrypt(nonce, ct, b"ai_trader_vault_v1")


# --------------------------------------------------------------------------- #
# 保险箱
# --------------------------------------------------------------------------- #

@dataclass
class VaultStatus:
    path: str
    dpapi: bool
    keys: List[str]
    exists: bool

    def as_dict(self) -> Dict[str, object]:
        return {
            "path": self.path,
            "dpapi": self.dpapi,
            "exists": self.exists,
            "keys": self.keys,
            "key_count": len(self.keys),
        }


class SecretVault:
    """加密的键值存储。

    - Windows：每个值用 DPAPI 包裹（绑定当前用户 + 当前机器）。
    - 其他平台：退回口令派生加密，需要 ``VAULT_PASSPHRASE``。
    """

    def __init__(self, base_dir: Optional[Path] = None, passphrase: Optional[str] = None):
        from .config import PROJECT_ROOT

        self.path = Path(base_dir or (PROJECT_ROOT / "data")) / VAULT_FILENAME
        self.passphrase = passphrase or os.getenv("VAULT_PASSPHRASE", "")
        self._cache: Dict[str, str] = {}
        self._loaded = False
        #: 最近一次读取的问题分类（只存枚举，不存内容）：
        #: "" | missing | corrupt_json | not_object | permission | os_error | decrypt_failed
        self.last_error_kind: str = ""

    # ---- 存储层 ----
    def _read_raw(self) -> Dict[str, str]:
        """读取原始密文。**失败一律返回空**（调用方按"缺少凭证"处理，fail-closed），
        但会把问题分类记在 ``last_error_kind``，便于把
        "没配" / "文件坏了" / "没权限" 区分开（审计 F-6）。
        """
        if not self.path.exists():
            self.last_error_kind = "missing"
            return {}
        try:
            text = self.path.read_text(encoding="utf-8")
        except PermissionError:
            self.last_error_kind = "permission"
            return {}
        except OSError:
            self.last_error_kind = "os_error"
            return {}
        try:
            data = json.loads(text)
        except ValueError:
            self.last_error_kind = "corrupt_json"
            return {}
        if not isinstance(data, dict):
            self.last_error_kind = "not_object"
            return {}
        self.last_error_kind = ""
        return data

    def _write_raw(self, data: Dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)

    def _wrap(self, value: str) -> str:
        raw = value.encode("utf-8")
        if DPAPI_AVAILABLE:
            return "dpapi:" + base64.b64encode(dpapi_encrypt(raw)).decode()
        if not self.passphrase:
            raise RuntimeError(
                "非 Windows 环境需设置 VAULT_PASSPHRASE 才能保存密钥"
            )
        return "pwd:" + base64.b64encode(seal_with_passphrase(raw, self.passphrase)).decode()

    def _unwrap(self, blob: str) -> str:
        if blob.startswith("dpapi:"):
            return dpapi_decrypt(base64.b64decode(blob[6:])).decode("utf-8")
        if blob.startswith("pwd:"):
            if not self.passphrase:
                raise RuntimeError("该条目用口令加密，需提供 VAULT_PASSPHRASE")
            return open_with_passphrase(base64.b64decode(blob[4:]), self.passphrase).decode("utf-8")
        raise ValueError("未知的保险箱条目格式")

    # ---- 公开 API ----
    def set(self, name: str, value: str) -> None:
        if not name:
            raise ValueError("name 不能为空")
        data = self._read_raw()
        data[name] = self._wrap(str(value))
        self._write_raw(data)
        self._cache[name] = str(value)
        self._loaded = True

    def get(self, name: str, default: Optional[str] = None) -> Optional[str]:
        if name in self._cache:
            return self._cache[name]
        data = self._read_raw()
        if name not in data:
            return default
        try:
            val = self._unwrap(data[name])
        except (OSError, RuntimeError, ValueError):
            # 解密失败（DPAPI 换机器/口令错误/条目格式未知）同样是"读不到"
            self.last_error_kind = "decrypt_failed"
            return default
        self._cache[name] = val
        return val

    def diagnostics(self) -> Dict[str, Any]:
        """只返回"状态与分类"，**绝不**返回任何条目内容或密文。"""
        kind = self.last_error_kind
        state = "ok"
        if not self.path.exists():
            state = "absent"
        elif kind in ("corrupt_json", "not_object", "decrypt_failed"):
            state = "damaged"
        elif kind in ("permission", "os_error"):
            state = "unreadable"
        return {
            "state": state,
            "kind": kind or "",
            "exists": self.path.exists(),
            "note": ("不存在" if state == "absent" else
                     "内容损坏/格式错误/解密失败" if state == "damaged" else
                     "无权限或 IO 错误" if state == "unreadable" else "可读"),
        }

    def delete(self, name: str) -> bool:
        data = self._read_raw()
        if name not in data:
            return False
        data.pop(name, None)
        self._write_raw(data)
        self._cache.pop(name, None)
        return True

    def keys(self) -> List[str]:
        return sorted(self._read_raw().keys())

    def status(self) -> VaultStatus:
        return VaultStatus(
            path=str(self.path),
            dpapi=DPAPI_AVAILABLE,
            keys=self.keys(),
            exists=self.path.exists(),
        )

    # ---- 迁移 ----
    def export_encrypted(self, passphrase: str) -> bytes:
        """导出为口令加密包（可跨机器、跨用户恢复）。"""
        if not passphrase:
            raise ValueError("导出必须提供口令")
        payload = {
            "version": 1,
            "exported_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
            "secrets": {k: self.get(k, "") for k in self.keys()},
        }
        return seal_with_passphrase(
            json.dumps(payload, ensure_ascii=False).encode("utf-8"), passphrase
        )

    def import_encrypted(self, blob: bytes, passphrase: str, overwrite: bool = False) -> List[str]:
        payload = json.loads(open_with_passphrase(blob, passphrase).decode("utf-8"))
        imported = []
        for k, v in (payload.get("secrets") or {}).items():
            if not overwrite and self.get(k) is not None:
                continue
            self.set(k, v)
            imported.append(k)
        return imported

    # ---- 常见便捷方法（星财富 + AI） ----
    def set_fosun_credentials(self, server_public_key: str, client_private_key: str,
                              api_key: str = "", base_url: str = "") -> None:
        if server_public_key:
            self.set("FSOPENAPI_SERVER_PUBLIC_KEY", server_public_key)
        if client_private_key:
            self.set("FSOPENAPI_CLIENT_PRIVATE_KEY", client_private_key)
        if api_key:
            self.set("FSOPENAPI_API_KEY", api_key)
        if base_url:
            self.set("FOSUN_BASE_URL", base_url)

    def inject_into_environ(self, names: Optional[List[str]] = None) -> List[str]:
        """把保险箱里的指定键注入到**当前进程**环境（不写系统环境）。

        官方 SDK 只认环境变量，这是唯一让 SDK 拿到 PEM 的合规方式。
        返回实际注入的键名列表。注意：进程退出后即消失。
        """
        targets = names or [
            "FSOPENAPI_SERVER_PUBLIC_KEY",
            "FSOPENAPI_CLIENT_PRIVATE_KEY",
            "FSOPENAPI_API_KEY",
        ]
        injected = []
        for name in targets:
            val = self.get(name)
            if val:
                os.environ[name] = val
                injected.append(name)
        return injected


def default_vault() -> SecretVault:
    return SecretVault()
