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
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

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
                 sleeper: Callable[[float], None] = time.sleep):
        self.base_url = str(base_url).rstrip("/")
        self.api_prefix = ("/" + api_prefix.strip("/")) if api_prefix else ""
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

    # ------------------------------------------------------------------ #
    def _path(self, path: str) -> str:
        return f"{self.api_prefix}{path if path.startswith('/') else '/' + path}"

    def _headers(self, session: proto.FosunSession, ts: str, nonce: str,
                 signature: str, request_id: str) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-API-Key": self._api_key,
            "X-session": session.session_id,
            "X-Request-Id": request_id,
            "X-Request-Client": "fosunai-selfhosted",
            "X-Timestamp": ts,
            "X-Nonce": nonce,
            "X-Signature": signature,
        }

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
        full_path = self._path(path)
        query = proto.canonical_query(params)
        body_bytes = (json.dumps(json_body, separators=(",", ":"), ensure_ascii=False)
                      .encode("utf-8")) if json_body is not None else None

        # 签名时间戳必须用**墙钟**（服务端要校验），不能用单调时钟。
        # self._clock 只用于本地限流与超时，两者不能混。
        ts = str(int(time.time() * 1000))
        nonce = proto.new_nonce()
        signature = proto.sign_request(session.signing_key, method, full_path,
                                      query, ts, nonce, body_bytes)
        headers = self._headers(session, ts, nonce, signature, _request_id())

        url = f"{self.base_url}{full_path}"
        if query:
            url = f"{url}?{query}"

        self.stats["requests"] += 1
        try:
            resp = self._http.request(method, url, data=body_bytes,
                                      headers=headers, timeout=self.timeout)
        except requests.Timeout:
            self.stats["timeouts"] += 1
            raise
        except requests.RequestException:
            raise

        if resp.status_code == 403:
            self.stats["permission_denied"] += 1
            raise proto.FosunPermissionError(
                "403：行情权限未开通（请检查账户是否已开通对应行情服务）")
        if resp.status_code == 401:
            raise proto.FosunAuthError("401：会话或 API Key 无效")
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
