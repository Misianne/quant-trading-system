"""官方请求构造的**独立参考实现**（oracle），仅用于对照测试。

为什么需要这个文件
------------------
之前那批测试是"自己生成密钥、自己签、自己验"——即使双方都用同一个错误算法
也会全绿。它们证明内部一致，**不证明**与官方兼容。

本模块的作用是：把官方 `client.py::_request` / `crypto.py::sign` 的构造规则
用另一份代码重写一遍，测试时比较"生产代码实际发出去的字节"与"oracle 按官方
规则算出的字节"。

硬性约束
--------
* **不得 import 生产模块**（`fosun_protocol` / `fosun_transport` / `fosun`）。
  一旦 import，Oracle 就退化成"用实现验证实现"，失去意义。
* 只用标准库 + `cryptography`（AES-GCM 无法用标准库替代）。
* 这里的每个函数都对应官方源码里的一段，注释标出出处。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# 官方常量（client.py / auth.py / crypto.py）
LANG_DEFAULT = "zhCn"
OFFICIAL_REQUEST_CLIENT = "openapi-python-sdk"
OFFICIAL_SOURCE = "python-sdk"
OFFICIAL_PRODUCT = "sdk"
HKDF_INFO = b"session-derivation"


# ---------------------------------------------------------------------- #
# 路径
# ---------------------------------------------------------------------- #
def request_path(api_prefix: str, path: str) -> str:
    """官方：``f"{self.api_prefix}{path if path.startswith('/') else '/' + path}"``"""
    return f"{api_prefix}{path if path.startswith('/') else '/' + path}"


def base_path_of(base_url: str) -> str:
    """官方：``self.base_path = urlparse(self.base_url).path.rstrip('/')``"""
    return urlparse(base_url).path.rstrip("/")


def full_sign_path(base_url: str, req_path: str) -> str:
    """官方：``f"{self.base_path}{request_path}"``"""
    return f"{base_path_of(base_url)}{req_path}"


# ---------------------------------------------------------------------- #
# 序列化与查询串
# ---------------------------------------------------------------------- #
def dumps(obj: Any) -> bytes:
    """官方：``json.dumps(obj, separators=(',', ':'))`` —— **默认** ensure_ascii。"""
    return json.dumps(obj, separators=(",", ":")).encode("utf-8")


def query_string(params: Optional[dict]) -> str:
    """官方：``"&".join(f"{k}={params[k]}" for k in sorted(params.keys()))``"""
    if not params:
        return ""
    return "&".join(f"{k}={params[k]}" for k in sorted(params.keys()))


# ---------------------------------------------------------------------- #
# 请求体
# ---------------------------------------------------------------------- #
def is_market_path(req_path: str) -> bool:
    """官方：``request_path == "/market/" or "/optmarket/" in request_path``

    保留原样（包括它对 ``/api/v1/market/...`` 恒为 False 这个事实）。
    """
    return req_path == "/market/" or "/optmarket/" in req_path


def should_encrypt(req_path: str, data: Optional[dict]) -> bool:
    """官方：``is_encrypted = data is not None and not is_market_path``"""
    return data is not None and not is_market_path(req_path)


def plaintext_of(data: Any) -> bytes:
    """加密前的明文：官方 ``json.dumps(data, separators=(',', ':'))``。"""
    return dumps(data)


def envelope_plain(data: Optional[dict]) -> bytes:
    """未加密信封：``{"encrypted": False, "content": data or {}}``"""
    return dumps({"encrypted": False, "content": data if data is not None else {}})


def aad(session_id: str, timestamp: str, nonce: str) -> bytes:
    """官方 ``build_response_aad``"""
    return f"X-session:{session_id}|X-timestamp:{timestamp}|X-nonce:{nonce}".encode("utf-8")


def envelope_encrypted(data: dict, *, enc_key: bytes, session_id: str,
                       timestamp: str, nonce: str, iv: bytes) -> bytes:
    """加密信封（IV 由调用方给定，便于构造确定性期望值）。

    官方顺序：``{"encrypted": True, "iv": ..., "tag": ..., "content": ...}``
    """
    sealed = AESGCM(enc_key).encrypt(iv, plaintext_of(data),
                                     aad(session_id, timestamp, nonce))
    return dumps({
        "encrypted": True,
        "iv": base64.b64encode(iv).decode("ascii"),
        "tag": base64.b64encode(sealed[-16:]).decode("ascii"),
        "content": base64.b64encode(sealed[:-16]).decode("ascii"),
    })


def decrypt_envelope(body: bytes, *, enc_key: bytes, session_id: str,
                     timestamp: str, nonce: str) -> bytes:
    """解开加密信封，返回**明文**（用于比对生产代码实际发送的内容）。"""
    payload = json.loads(body)
    if not payload.get("encrypted"):
        raise AssertionError("期望加密信封，实际不是")
    return AESGCM(enc_key).decrypt(
        base64.b64decode(payload["iv"]),
        base64.b64decode(payload["content"]) + base64.b64decode(payload["tag"]),
        aad(session_id, timestamp, nonce))


# ---------------------------------------------------------------------- #
# 签名
# ---------------------------------------------------------------------- #
def canonical(method: str, path: str, query: str, timestamp: str,
              nonce: str, body: Optional[bytes]) -> str:
    """官方 ``crypto.py::sign`` 的拼串：
    ``f"{method.upper()}\n{path}\n{query}\n{timestamp}\n{nonce}\n{body_sha_hex}"``
    """
    body_sha = hashlib.sha256(body if body else b"").hexdigest()
    return "\n".join([method.upper(), path, query, timestamp, nonce, body_sha])


def sign(mac_key: bytes, method: str, path: str, query: str, timestamp: str,
         nonce: str, body: Optional[bytes]) -> str:
    text = canonical(method, path, query, timestamp, nonce, body)
    return base64.b64encode(
        hmac.new(mac_key, text.encode("utf-8"), hashlib.sha256).digest()
    ).decode("ascii")


# ---------------------------------------------------------------------- #
# 请求头
# ---------------------------------------------------------------------- #
def business_headers(*, lang: str, api_key: str, session_id: str,
                     request_id: str, timestamp: str, nonce: str,
                     signature: str, request_client: str) -> Dict[str, str]:
    """官方业务请求头（注意：**没有** X-source / X-product / Accept）。"""
    return {
        "X-Lang": lang,
        "Content-Type": "application/json",
        "X-API-Key": api_key,
        "X-session": session_id,
        "X-Request-Id": request_id,
        "X-Request-Client": request_client,
        "X-Timestamp": timestamp,
        "X-Nonce": nonce,
        "X-Signature": signature,
    }


def handshake_headers(*, lang: str, api_key: str, request_id: str,
                      timestamp: str, nonce: str, signature: str,
                      client_id: str) -> Dict[str, str]:
    """官方握手头（包含 X-source / X-product，且时间戳头是 ``X-timestamp``）。"""
    return {
        "X-Lang": lang,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-API-Key": api_key,
        "X-Request-Id": request_id,
        "X-Request-Client": client_id,
        "X-timestamp": timestamp,
        "X-source": OFFICIAL_SOURCE,
        "X-product": OFFICIAL_PRODUCT,
        "X-Nonce": nonce,
        "X-Signature": signature,
    }


# ---------------------------------------------------------------------- #
# 期望值
# ---------------------------------------------------------------------- #
def expected_query_for_tick(code: str, count: int, id_value: int = -1) -> str:
    return query_string({"code": code, "count": count, "id": id_value})


def expected_quote_body(codes) -> bytes:
    """quote 的明文 body：``{"codes": [...]}``"""
    return plaintext_of({"codes": list(codes)})
