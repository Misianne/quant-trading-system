"""复星 OpenAPI 的外围可靠性层。

官方 SDK 缺三件事：**超时、重试、限流**；WebSocket 也**没有重连**。
本模块用独立实现补齐，不修改、不复制、不依赖 SDK。

设计原则
--------
* 一个请求挂死，绝不能拖住 PaperRuntime 的 tick 循环 → 默认 3 秒超时；
* 权限错误、签名错误、数据质量错误**不重试**（重试只会掩盖问题）；
* 只对幂等且可恢复的失败重试：连接错误、超时、5xx、业务码 70002；
* 限流默认**保守**（官方未公布配额，见审计报告），宁可慢也不要触发风控；
* 日志只记元数据，**永不打印密钥、签名、nonce、完整报文**。
"""
from __future__ import annotations

import asyncio
import json
import math
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

import requests

from . import fosun_protocol as proto

#: 默认单次请求超时（秒）。官方未给建议值，取 3 秒 —— 超过这个时间，
#: 对"每秒级"的行情轮询已经失去意义。
DEFAULT_TIMEOUT = 3.0

#: 默认限流：每秒 2 次、突发 4 次。官方未公布配额，故取保守值。
DEFAULT_RATE = 2.0
DEFAULT_BURST = 4


# ---------------------------------------------------------------------- #
# 令牌桶
# ---------------------------------------------------------------------- #
class TokenBucket:
    """线程安全的令牌桶。

    ``acquire`` 最多等待 ``timeout`` 秒；超时返回 False，由调用方决定
    是"跳过本轮"还是"报错"。**不阻塞到天荒地老**是这里的全部意义。
    """

    def __init__(self, rate_per_sec: float, burst: int,
                 *, clock: Callable[[], float] = time.monotonic,
                 sleeper: Callable[[float], None] = time.sleep):
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec 必须 > 0")
        self.rate = float(rate_per_sec)
        self.capacity = max(1, int(burst))
        self._tokens = float(self.capacity)
        self._clock = clock
        self._sleep = sleeper
        self._last = clock()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._last)
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
        self._last = now

    def acquire(self, tokens: float = 1.0, timeout: float = 2.0) -> bool:
        deadline = self._clock() + max(0.0, timeout)
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return True
                missing = tokens - self._tokens
                wait = missing / self.rate
            now = self._clock()
            if now >= deadline:
                return False
            self._sleep(min(wait, deadline - now))


# ---------------------------------------------------------------------- #
# 重试策略
# ---------------------------------------------------------------------- #
@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 4.0

    def delay_for(self, attempt: int) -> float:
        """指数退避：0.5, 1, 2, 4…（attempt 从 1 开始）。"""
        raw = self.base_delay * (2 ** max(0, attempt - 1))
        return min(self.max_delay, raw)

    @staticmethod
    def retryable(exc: BaseException) -> bool:
        """只有"网络抖动/上游临时故障"值得重试。

        权限、参数、签名、解密错误**一律不重试** —— 那些重试一万次也一样，
        只会拖慢失败、掩盖真正的配置问题。
        """
        if isinstance(exc, (proto.FosunPermissionError, proto.FosunAuthError,
                            proto.FosunRequestError)):
            return False
        if isinstance(exc, proto.FosunUpstreamError):
            return True
        if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
            return True
        return False


# ---------------------------------------------------------------------- #
# HTTP 传输
# ---------------------------------------------------------------------- #
class FosunHttpTransport:
    """带超时/限流/重试/签名的 JSON 传输。

    ``session_provider`` 是一个无参可调用对象，返回 :class:`proto.FosunSession`。
    它负责"会话过期则重建"，本类不关心握手细节。
    """

    def __init__(self, base_url: str, *,
                 api_prefix: str = "/api",
                 session_provider: Callable[[], proto.FosunSession],
                 api_key: str = "",
                 timeout: float = DEFAULT_TIMEOUT,
                 rate_per_sec: float = DEFAULT_RATE,
                 burst: int = DEFAULT_BURST,
                 retry: RetryPolicy = RetryPolicy(),
                 http_session: Optional[requests.Session] = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleeper: Callable[[float], None] = time.sleep,
                 lang: str = "zhCn",
                 client_id: str = "fosunai-selfhosted",
                 encrypt_request: bool = True):
        self.base_url = str(base_url).rstrip("/")
        self.api_prefix = ("/" + api_prefix.strip("/")) if api_prefix else ""
        #: base_url 自带的路径前缀。官方把它一起签进 full_sign_path
        #: （`base_path = urlparse(base_url).path.rstrip('/')`）。
        #: 少了它，只要网关部署在子路径下，签名就必然对不上。
        self._base_path = urlparse(self.base_url).path.rstrip("/")
        #: 官方每个请求都带 X-Lang，默认 zhCn。
        self.lang = str(lang or "zhCn")
        #: 客户端标识。官方用 "openapi-python-sdk"；本项目不冒充官方 SDK，
        #: 用自己的标识。该头**不参与签名**，不影响协议兼容性。
        self.client_id = str(client_id or "fosunai-selfhosted")
        #: 是否对请求体加密。默认 True —— 复刻官方客户端的**实际行为**
        #: （其 is_market_path 判定对 /api/v1/market/... 恒为假，行情 POST 也会加密）。
        #: 置 False 时发送明文信封，用于服务端行为与 README 一致时的兼容/诊断。
        self.encrypt_request = bool(encrypt_request)
        self._session_provider = session_provider
        self._api_key = api_key
        self.timeout = float(timeout)
        self.retry = retry
        self.bucket = TokenBucket(rate_per_sec, burst, clock=clock, sleeper=sleeper)
        self._clock = clock
        self._sleep = sleeper
        self._http = http_session or requests.Session()
        self._http.verify = True
        #: 统计（供健康检查/报告）
        self.stats: Dict[str, int] = {"requests": 0, "retries": 0,
                                      "timeouts": 0, "throttled": 0,
                                      "permission_denied": 0}
        #: 最近一次响应的 HTTP 状态码（None = 未收到响应）。
        #: 单独记，是因为告警/报告需要"到底返回了 200 还是 403"，
        #: 而异常路径上这个信息很容易丢。
        self.last_status: Optional[int] = None
        #: 最近一次响应是否走了 AES-GCM 解密分支
        self.last_response_encrypted: bool = False

    # ------------------------------------------------------------------ #
    def _path(self, path: str) -> str:
        """请求路径（用于拼 URL）：``api_prefix + path``。"""
        return f"{self.api_prefix}{path if path.startswith('/') else '/' + path}"

    def _sign_path(self, request_path: str) -> str:
        """参与签名的完整路径：``base_path + request_path``。

        与官方 ``full_sign_path = f"{self.base_path}{request_path}"`` 对齐。
        """
        return f"{self._base_path}{request_path}"

    def _headers(self, session: proto.FosunSession, ts: str, nonce: str,
                 signature: str, request_id: str) -> Dict[str, str]:
        """业务请求头，逐项对齐官方 ``client.py::_request``。

        官方业务请求**不含** ``X-source`` / ``X-product``（那两个只在握手时发），
        也不含 ``Accept``。这里同样不发，避免"多一个头"造成字节级差异。
        """
        return {
            "X-Lang": self.lang,
            "Content-Type": "application/json",
            "X-API-Key": self._api_key,
            "X-session": session.session_id,
            "X-Request-Id": request_id,
            "X-Request-Client": self.client_id,
            "X-Timestamp": ts,
            "X-Nonce": nonce,
            "X-Signature": signature,
        }

    # ------------------------------------------------------------------ #
    @staticmethod
    def _dumps(obj: Any) -> bytes:
        """官方序列化规则：紧凑分隔符，**不关闭** ensure_ascii。

        ``json.dumps(obj, separators=(',', ':'))`` 默认 ``ensure_ascii=True``，
        非 ASCII 会被转义成 ``\\uXXXX``。要字节级一致就必须保持默认，
        不能图省事加 ``ensure_ascii=False``。
        """
        return json.dumps(obj, separators=(",", ":")).encode("utf-8")

    def _build_body(self, session: proto.FosunSession, request_path: str,
                    data: Optional[dict], ts: str, nonce: str) -> bytes:
        """构造最终发送的 body 字节（官方信封规则）。

        官方原文：
            is_market_path = request_path == "/market/" or "/optmarket/" in request_path
            is_encrypted = data is not None and not is_market_path
            req_payload = {"encrypted": False, "content": data if data is not None else {}}

        注意 ``request_path == "/market/"`` 对 ``/api/v1/market/...`` **永远为假**
        （见独立审计 §3.4），因此行情 POST 实际会被加密。我们按**实际行为**
        复现，而不是按 README 的描述。
        """
        is_market_path = (request_path == "/market/"
                          or "/optmarket/" in request_path)
        # 官方恒加密；``encrypt_request=False`` 是给"服务端行为与 README 一致"
        # 的场景留的兼容/诊断开关，默认保持 True 以复刻官方。
        is_encrypted = bool(data is not None and not is_market_path
                            and self.encrypt_request)

        if is_encrypted:
            plaintext = self._dumps(data)
            aad = proto.build_aad(session.session_id, ts, nonce)
            iv_b64, ct_b64, tag_b64 = proto.encrypt_body(
                session.encryption_key, plaintext, aad)
            payload = {"encrypted": True, "iv": iv_b64,
                       "tag": tag_b64, "content": ct_b64}
        else:
            payload = {"encrypted": False,
                       "content": data if data is not None else {}}

        return self._dumps(payload)

    # ------------------------------------------------------------------ #
    def request(self, method: str, path: str, *,
                params: Optional[dict] = None,
                json_body: Optional[dict] = None,
                idempotent: bool = True) -> Dict[str, Any]:
        """发一次请求，返回**已解包**的业务 data。

        抛错语义：
        * ``FosunPermissionError`` 权限（403 / 70003）—— 不重试
        * ``FosunAuthError``       会话失效（401）—— 由 session_provider 重建后重试一次
        * ``FosunUpstreamError``   上游 5xx / 70002 —— 可重试
        * ``FosunProtocolError``   解析/解密失败 —— 不重试
        """
        last_exc: Optional[BaseException] = None
        for attempt in range(1, max(1, self.retry.max_attempts) + 1):
            if not self.bucket.acquire(timeout=self.timeout):
                self.stats["throttled"] += 1
                raise proto.FosunProtocolError("本地限流：等待令牌超时")
            try:
                return self._attempt(method, path, params, json_body)
            except BaseException as exc:  # noqa: BLE001
                last_exc = exc
                if not (idempotent and self.retry.retryable(exc)):
                    raise
                if attempt >= self.retry.max_attempts:
                    raise
                self.stats["retries"] += 1
                self._sleep(self.retry.delay_for(attempt))
        raise last_exc if last_exc else proto.FosunProtocolError("未知失败")

    # ------------------------------------------------------------------ #
    def _attempt(self, method: str, path: str, params: Optional[dict],
                 json_body: Optional[dict]) -> Dict[str, Any]:
        session = self._session_provider()
        request_path = self._path(path)
        sign_path = self._sign_path(request_path)
        query = proto.canonical_query(params)

        # 签名时间戳必须是**墙钟**（服务端要校验），不能与限流用的单调时钟混用。
        ts = str(int(time.time() * 1000))
        # 官方 nonce = str(uuid.uuid4().hex)（32 位十六进制）。
        nonce = uuid.uuid4().hex

        # 先定 ts/nonce，再加密（AAD 依赖它们），最后对**最终 body 字节**签名。
        # 顺序错了会签到一个与实际发送不同的 body 上。
        body_bytes = self._build_body(session, request_path, json_body, ts, nonce)

        signature = proto.sign_request(session.signing_key, method, sign_path,
                                      query, ts, nonce, body_bytes)
        headers = self._headers(session, ts, nonce, signature, str(uuid.uuid4()))

        url = f"{self.base_url}{request_path}"

        self.stats["requests"] += 1
        try:
            # params 交给 requests 编码（与官方一致：签名用原始串，发送用编码串）。
            resp = self._http.request(method, url, data=body_bytes, params=params,
                                      headers=headers, timeout=self.timeout)
            self.last_status = getattr(resp, "status_code", None)
        except requests.Timeout:
            self.stats["timeouts"] += 1
            raise
        except requests.RequestException:
            raise

        if resp.status_code in (401, 403):
            code, msg = _peek_error(resp)
            if resp.status_code == 403:
                self.stats["permission_denied"] += 1
                raise proto.FosunPermissionError(
                    f"403：{msg or '行情权限未开通'}" + (f"（code={code}）" if code else ""))
            raise proto.FosunAuthError(
                f"401：{msg or '会话或 API Key 无效'}" + (f"（code={code}）" if code else ""))
        if resp.status_code >= 500:
            raise proto.FosunUpstreamError(f"HTTP {resp.status_code}")

        return self._decode(resp, session, ts, nonce)

    # ------------------------------------------------------------------ #
    def _decode(self, resp, session: proto.FosunSession, ts: str,
                nonce: str) -> Dict[str, Any]:
        try:
            payload = resp.json()
        except ValueError:
            raise proto.FosunProtocolError("响应不是 JSON") from None

        data: Any = payload
        self.last_response_encrypted = bool(
            isinstance(payload, dict) and payload.get("encrypted"))
        if isinstance(payload, dict) and "encrypted" in payload:
            if payload.get("encrypted"):
                plain = proto.decrypt_body(
                    session.encryption_key, payload.get("iv", ""),
                    payload.get("content", ""), payload.get("tag", ""),
                    proto.build_aad(session.session_id, ts, nonce))
                try:
                    data = json.loads(plain)
                except ValueError:
                    raise proto.FosunProtocolError("解密后不是 JSON") from None
            else:
                data = payload.get("content")

        if not isinstance(data, dict):
            raise proto.FosunProtocolError("响应结构不可识别")

        code = data.get("code")
        if code is None:
            raise proto.FosunProtocolError("响应缺少 code 字段")
        try:
            code_i = int(code)
        except (TypeError, ValueError):
            raise proto.FosunProtocolError(f"code 非整数：{code!r}") from None
        if code_i != 0:
            message = str(data.get("message") or "")
            request_id = str(data.get("requestId") or "")
            if code_i == 70003:
                self.stats["permission_denied"] += 1
                raise proto.FosunPermissionError(f"70003 权限错误：{message}")
            if code_i == 70001:
                raise proto.FosunRequestError(f"70001 参数错误：{message}")
            if code_i == 70002:
                raise proto.FosunUpstreamError(f"70002 下游服务错误：{message}")
            raise proto.FosunAPIError(code_i, message, request_id)
        return data.get("data") or {}

    def close(self) -> None:
        try:
            self._http.close()
        except Exception:  # noqa: BLE001
            pass


def _peek_error(resp) -> tuple:
    """从失败响应里取 ``(业务码, 脱敏消息)``，消息截断到 120 字符。

    401/403 光有一句固定话术没法排障；服务端通常会在 body 里给 code/message。
    这里只取这两个字段，不落整包 body。
    """
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return None, ""
    if not isinstance(body, dict):
        return None, ""
    data = body.get("content") if "encrypted" in body else body
    if not isinstance(data, dict):
        return None, ""
    code = data.get("code")
    msg = str(data.get("message") or "")[:120]
    return code, msg


def _request_id() -> str:
    import uuid
    return str(uuid.uuid4())


# ---------------------------------------------------------------------- #
# WebSocket 重连三件套
# ---------------------------------------------------------------------- #
@dataclass(frozen=True)
class ReconnectPolicy:
    """重连退避。**必须有限次**，否则会把权限错误变成无限重试。"""

    base_delay: float = 1.0
    max_delay: float = 30.0
    max_attempts: int = 10

    def delay_for(self, attempt: int) -> Optional[float]:
        if attempt > self.max_attempts:
            return None
        raw = self.base_delay * (2 ** max(0, attempt - 1))
        return min(self.max_delay, raw)


class SubscriptionRegistry:
    """订阅清单。重连后据此重放订阅。

    为什么自己维护：SDK 内部也有一份，但**没有任何代码使用它**；
    我们不能依赖未使用的私有字段。
    """

    def __init__(self):
        self._topics: Dict[str, Tuple[str, ...]] = {}
        self._lock = threading.Lock()

    def add(self, topic: str, codes: Sequence[str]) -> None:
        with self._lock:
            self._topics[str(topic)] = tuple(str(c) for c in codes)

    def remove(self, topic: str) -> None:
        with self._lock:
            self._topics.pop(str(topic), None)

    def payloads(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [{"act": "sub",
                     "items": [{"topic": t, "code": list(c)}],
                     "requestid": f"resub-{t}"}
                    for t, c in sorted(self._topics.items())]

    def topics(self) -> List[str]:
        with self._lock:
            return sorted(self._topics)

    def __len__(self) -> int:
        return len(self._topics)


class GapDetector:
    """断线期间的数据缺口。

    只看"事件时间轴的断档"，不看消息条数——条数受快照窗口影响，不可靠。
    """

    def __init__(self, max_gap_seconds: float = 5.0):
        self.max_gap = float(max_gap_seconds)
        self.last_event_time: Optional[float] = None
        self.gaps_detected = 0
        self.max_gap_seen = 0.0

    def observe(self, event_epoch: float, now_epoch: Optional[float] = None) -> float:
        """返回本次观测到的缺口秒数（0 表示连续）。"""
        if event_epoch <= 0 or not math.isfinite(event_epoch):
            return 0.0
        if self.last_event_time is None:
            self.last_event_time = event_epoch
            return 0.0
        gap = event_epoch - self.last_event_time
        if gap > self.max_gap:
            self.gaps_detected += 1
            self.max_gap_seen = max(self.max_gap_seen, gap)
        self.last_event_time = max(self.last_event_time, event_epoch)
        return max(0.0, gap)

    def stalled(self, now_epoch: float, threshold: Optional[float] = None) -> bool:
        if self.last_event_time is None:
            return True
        limit = self.max_gap if threshold is None else float(threshold)
        return (now_epoch - self.last_event_time) > limit

    def reset(self) -> None:
        """重连后调用：基线重置，避免把"断线时长"误判成"数据缺口"。"""
        self.last_event_time = None


async def run_stream_loop(connect: Callable[[], Any],
                           registry: SubscriptionRegistry,
                           policy: ReconnectPolicy,
                           on_message: Callable[[dict], None],
                           *,
                           sleeper: Callable[[float], Any] = asyncio.sleep,
                           max_messages: Optional[int] = None,
                           on_reconnect: Optional[Callable[[int], None]] = None
                           ) -> Dict[str, Any]:
    """极简 WS 监督循环：连接 → 订阅 → 收消息 → 断开 → 退避 → 重连。

    ``connect`` 返回一个异步可迭代的 ws 对象（具备 ``send`` / ``close``）。
    ``on_message`` 收到已解析的 dict。

    返回统计。达到 ``max_attempts`` 仍失败则抛 ``FosunUpstreamError`` ——
    **不允许无限重连**，否则权限错误会变成静默的死循环。
    """
    stats = {"connects": 0, "reconnects": 0, "messages": 0, "failures": 0}
    for attempt in range(1, policy.max_attempts + 1):
        try:
            ws = await connect()
            stats["connects"] += 1
            for payload in registry.payloads():
                await ws.send(json.dumps(payload, separators=(",", ":")))
            async for raw in ws:
                try:
                    msg = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
                except (ValueError, TypeError):
                    continue
                stats["messages"] += 1
                on_message(msg)
                if max_messages and stats["messages"] >= max_messages:
                    await ws.close()
                    return stats
            # 正常关闭 → 也走重连
            raise proto.FosunUpstreamError("WebSocket 连接关闭")
        except Exception:  # noqa: BLE001
            stats["failures"] += 1
            delay = policy.delay_for(attempt)
            if delay is None:
                raise proto.FosunUpstreamError(
                    f"WebSocket 重连 {policy.max_attempts} 次仍失败，停止")
            stats["reconnects"] += 1
            if on_reconnect is not None:
                on_reconnect(attempt)
            await sleeper(delay)
    # 循环走完仍没有成功连接 = 重连耗尽。**必须抛错**，
    # 不能静默返回 stats —— 那会让上层以为"跑完了"，实际一条数据都没收到。
    raise proto.FosunUpstreamError(
        f"WebSocket 在 {policy.max_attempts} 次尝试后仍未建立稳定连接")
