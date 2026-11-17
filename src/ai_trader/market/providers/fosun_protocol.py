"""复星财富 OpenAPI 的**协议实现**（独立重写，不含 SDK 源码）。

为什么单独一层
--------------
官方 SDK 仓库**没有许可证**（见 `US_FOSUN_OFFICIAL_OPENAPI_AUDIT.md` §2），
因此不能把它的代码复制进本项目、也不把它作为项目依赖。但"协议"本身不是
著作权客体：我们按官方文档与公开可见的协议约定独立实现最小必要部分。

本模块只做四件事：

1. 会话密钥派生（ECDH → HKDF-SHA256）
2. 握手签名/验签（ECDSA P-384 + SHA-256）
3. 请求签名（HMAC-SHA256）
4. 报文体加解密（AES-256-GCM）

**边界声明**：这些密码学操作保护的是"请求身份与完整性"。官方**不对行情
响应签名**，所以本模块不提供、也不应提供"行情数据真实性证明"。任何把这里
的签名当成"交易所事件认证"的用法都是错的。

不明确的地方一律标 UNKNOWN，不猜：

* 查询串在签名里的确切编码形式（我们按 RFC3986 编码，并保证与实际发送一致）
* 服务端是否对 `code != 0` 的响应也做加密
* 时间戳时区（毫秒 epoch，默认 UTC；未做本地时区推断）
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
from dataclasses import dataclass
from typing import Optional, Tuple
from urllib.parse import quote, urlencode

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

#: 握手与密钥交换使用的曲线。官方为 secp384r1（P-384）。
CURVE = ec.SECP384R1()

#: HKDF 的 info 标签。协议约定值，改动会导致双方派生出的密钥不同。
HKDF_INFO = b"session-derivation"

#: 一次派生的字节数：前 32 字节做 MAC key，后 32 字节做 AES key。
DERIVE_LEN = 64

#: 会话提前多少秒视为过期（避免请求在途时失效）。
SESSION_EXPIRY_SKEW = 60.0


class FosunProtocolError(Exception):
    """协议层错误基类。"""


class FosunAuthError(FosunProtocolError):
    """握手失败、身份密钥缺失或无效。"""


class FosunPermissionError(FosunProtocolError):
    """业务码 70003 / HTTP 403：行情权限未开通。"""


class FosunRequestError(FosunProtocolError):
    """业务码 70001：参数错误。"""


class FosunUpstreamError(FosunProtocolError):
    """业务码 70002：下游服务错误（可重试）。"""


class FosunAPIError(FosunProtocolError):
    """其它业务错误。"""

    def __init__(self, code: int, message: str, request_id: str = ""):
        super().__init__(f"[{code}] {message}")
        self.code = int(code)
        self.message = message
        self.request_id = request_id


# ---------------------------------------------------------------------- #
# 会话
# ---------------------------------------------------------------------- #
@dataclass(frozen=True)
class FosunSession:
    """一次 ECDH 握手的结果。"""

    session_id: str
    signing_key: bytes          # HMAC key（32B）
    encryption_key: bytes       # AES-GCM key（32B）
    expires_at: float           # epoch 秒

    def is_valid(self, now: float, skew: float = SESSION_EXPIRY_SKEW) -> bool:
        return bool(self.session_id) and now < (self.expires_at - skew)

    def as_public_dict(self) -> dict:
        """可安全记录的摘要——**不含任何密钥材料**。"""
        return {"session_id": self.session_id,
                "expires_at": self.expires_at,
                "has_signing_key": bool(self.signing_key),
                "has_encryption_key": bool(self.encryption_key)}


# ---------------------------------------------------------------------- #
# 身份密钥
# ---------------------------------------------------------------------- #
def load_private_key_pem(pem: str):
    if not pem or "PRIVATE KEY" not in pem:
        raise FosunAuthError("客户端私钥不是 PEM PRIVATE KEY")
    try:
        return serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
    except Exception as exc:  # noqa: BLE001
        raise FosunAuthError(f"客户端私钥无法解析：{type(exc).__name__}") from None


def load_public_key_pem(pem: str):
    if not pem or "PUBLIC KEY" not in pem:
        raise FosunAuthError("服务端公钥不是 PEM PUBLIC KEY")
    try:
        return serialization.load_pem_public_key(pem.encode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise FosunAuthError(f"服务端公钥无法解析：{type(exc).__name__}") from None


# ---------------------------------------------------------------------- #
# 临时密钥对与握手签名
# ---------------------------------------------------------------------- #
def generate_ephemeral_keypair() -> Tuple[object, str]:
    """生成临时 ECDH 密钥对，返回 ``(私钥, base64(X9.62 未压缩点))``。"""
    priv = ec.generate_private_key(CURVE)
    raw = priv.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint)
    return priv, base64.b64encode(raw).decode("ascii")


def handshake_payload(ephemeral_pub_b64: str, nonce_b64: str) -> bytes:
    """握手被签名的字节串：临时公钥原始字节 || nonce 原始字节。

    这是协议约定。**不含时间戳、不含 apiKey** —— 后两者由请求头携带。
    """
    return base64.b64decode(ephemeral_pub_b64) + base64.b64decode(nonce_b64)


def sign_handshake(identity_private_key, ephemeral_pub_b64: str,
                   nonce_b64: str) -> str:
    """客户端用长期身份私钥签 ECDSA-P384/SHA256。"""
    try:
        sig = identity_private_key.sign(handshake_payload(ephemeral_pub_b64, nonce_b64),
                                        ec.ECDSA(hashes.SHA256()))
    except Exception as exc:  # noqa: BLE001
        raise FosunAuthError(f"握手签名失败：{type(exc).__name__}") from None
    return base64.b64encode(sig).decode("ascii")


def verify_handshake(server_public_key, ephemeral_pub_b64: str,
                     nonce_b64: str, signature_b64: str) -> bool:
    """验证服务端握手签名。失败返回 False，不抛异常（由调用方决定语义）。"""
    try:
        server_public_key.verify(base64.b64decode(signature_b64),
                                 handshake_payload(ephemeral_pub_b64, nonce_b64),
                                 ec.ECDSA(hashes.SHA256()))
        return True
    except Exception:  # noqa: BLE001
        return False


def new_nonce() -> str:
    """32 字节随机 nonce，base64 编码。"""
    return base64.b64encode(os.urandom(32)).decode("ascii")


# ---------------------------------------------------------------------- #
# 密钥派生
# ---------------------------------------------------------------------- #
def derive_session_keys(ephemeral_private_key, server_pub_b64: str,
                        client_nonce_b64: str, server_nonce_b64: str
                        ) -> Tuple[bytes, bytes]:
    """ECDH → HKDF-SHA256(64B) → ``(signing_key, encryption_key)``。

    salt 为 ``client_nonce || server_nonce``，info 为协议常量。
    """
    try:
        point = base64.b64decode(server_pub_b64)
        server_pub = ec.EllipticCurvePublicKey.from_encoded_point(CURVE, point)
        shared = ephemeral_private_key.exchange(ec.ECDH(), server_pub)
    except Exception as exc:  # noqa: BLE001
        raise FosunAuthError(f"密钥协商失败：{type(exc).__name__}") from None

    salt = base64.b64decode(client_nonce_b64) + base64.b64decode(server_nonce_b64)
    material = HKDF(algorithm=hashes.SHA256(), length=DERIVE_LEN,
                    salt=salt, info=HKDF_INFO).derive(shared)
    return material[:32], material[32:]


# ---------------------------------------------------------------------- #
# 请求签名
# ---------------------------------------------------------------------- #
def canonical_query(params: Optional[dict]) -> str:
    """构造参与签名的查询串。

    排序 + RFC3986 编码。当前协议参数都是纯 ASCII 代码或整数，编码前后
    完全一致；将来若出现需要转义的取值，**发送与签名必须使用同一个串**，
    否则会出现"签名对但服务端算出来不一样"的诡异问题。
    """
    if not params:
        return ""
    items = sorted((str(k), str(v)) for k, v in params.items())
    return urlencode(items, quote_via=quote)


def body_sha256_hex(body: Optional[bytes]) -> str:
    return hashlib.sha256(body or b"").hexdigest()


def canonical_request(method: str, path: str, query: str,
                      timestamp: str, nonce: str, body_hex: str) -> str:
    """签名原文，六段以 ``\\n`` 连接。

    顺序：方法、完整路径、查询串、时间戳、nonce、请求体 SHA256。
    """
    return "\n".join([method.upper(), path, query, timestamp, nonce, body_hex])


def sign_request(signing_key: bytes, method: str, path: str, query: str,
                 timestamp: str, nonce: str, body: Optional[bytes]) -> str:
    text = canonical_request(method, path, query, timestamp, nonce,
                             body_sha256_hex(body))
    mac = hmac.new(signing_key, text.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(mac).decode("ascii")


def verify_request(signing_key: bytes, signature_b64: str, method: str,
                   path: str, query: str, timestamp: str, nonce: str,
                   body: Optional[bytes]) -> bool:
    """本地自校验。仅用于测试与排错——生产签名由服务端校验。"""
    expected = sign_request(signing_key, method, path, query, timestamp, nonce, body)
    return hmac.compare_digest(expected, signature_b64)


# ---------------------------------------------------------------------- #
# 报文加解密
# ---------------------------------------------------------------------- #
def build_aad(session_id: str, timestamp: str, nonce: str) -> bytes:
    return f"X-session:{session_id}|X-timestamp:{timestamp}|X-nonce:{nonce}".encode("utf-8")


def encrypt_body(key: bytes, plaintext: bytes, aad: bytes) -> Tuple[str, str, str]:
    iv = os.urandom(12)
    sealed = AESGCM(key).encrypt(iv, plaintext, aad)
    return (base64.b64encode(iv).decode("ascii"),
            base64.b64encode(sealed[:-16]).decode("ascii"),
            base64.b64encode(sealed[-16:]).decode("ascii"))


def decrypt_body(key: bytes, iv_b64: str, ciphertext_b64: str,
                 tag_b64: str, aad: bytes) -> bytes:
    try:
        return AESGCM(key).decrypt(base64.b64decode(iv_b64),
                                   base64.b64decode(ciphertext_b64)
                                   + base64.b64decode(tag_b64), aad)
    except Exception as exc:  # noqa: BLE001
        raise FosunProtocolError(f"报文解密失败：{type(exc).__name__}") from None
