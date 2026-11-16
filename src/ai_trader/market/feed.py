"""实时行情推送（B5）—— 在官方 SDK 之上补齐可靠性。

为什么不能直接用 SDK 的 ``MarketPushAPI``
-----------------------------------------
审计 `sdk_src/fsopenapi/api/market_push.py` 后确认它只有：
``connect`` / ``subscribe`` / ``unsubscribe`` / ``send_heartbeat`` / ``close``。

**没有**：断线重连、心跳超时检测、重连后恢复订阅、消息去重、乱序过滤、
时间戳校验、连接状态机。``self._subscriptions`` 虽然注释写着"断线后用于恢复"，
但从未被写入或读取（死字段）。

因此本模块自己实现一层可靠传输，协议格式对齐 SDK 的 JSON 模式：

    订阅： {"act": "sub", "items": [{"topic": "qt", "code": ["cn600519"]}], "requestid": "..."}
    心跳： 收到 {"topic": "hb", "hbValue": X} 后回 {"act": "hb", "hbValue": X}
    推送： {"topic": "qt", "items": [{"code": "...", "msg": "{...}"}], "t": 毫秒}

线程模型
--------
``MarketFeed`` 在后台线程里跑 asyncio 事件循环，对外暴露**线程安全**的
``is_fresh()`` / ``snapshot()``，方便同步的风控与引擎调用。

诚实标注
--------
* 真实推送消息的字段名**未验证**（无凭证），因此解析器是"候选字段名"式的宽松解析，
  并在 ``snapshot()`` 里返回 ``protocol="unverified"``。
* 本地测试用的行情一律 ``source="local-ws-test"``，绝不冒充真实行情。

认证（P0-2）
------------
官方推送服务要求握手时带 ``X-Api-Key`` 与 ``X-Session``（见
``sdk_src/fsopenapi/api/market_push.py``）。本模块**不再**匿名连接：

* 认证信息由 ``auth_provider`` 在**每次建连时**现取（Session 会过期/轮换，
  不能在构造时缓存）；
* 取不到或不完整 → :class:`FeedAuthError`，直接 FAILED，**绝不**以匿名身份
  建立连接并对外宣称"行情已连上"；
* 认证头**永不**进入日志、异常文本或 ``snapshot()``。

订阅确认（P1）
--------------
发出 ``act=sub`` 后必须收到服务端有效回执才进入 ``SUBSCRIBED``；
拒绝、权限不足、超时一律 :class:`FeedSubscriptionError` → FAILED。
回执的真实字段格式未经生产验证，因此判定逻辑只接受"可解释为确认"的证据，
其余一律视为未确认（fail-closed），不会把"发出去了"当成"订阅成功"。
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from ..log_redaction import RedactionFilter, redact_text, register_secret

#: websockets 库在 DEBUG 级别会把**完整握手头**（含 X-Api-Key / X-Session）
#: 打进日志（``logger.debug("%s > %s", side, request)``，格式化发生在 handler 里）。
#: F-2：不再靠"把 logger 钉死在 INFO"来回避泄露（那是掩盖而不是修复，
#: 换个 logger/换处调用就又漏了）；改为**在记录进入 handler 之前脱敏**，
#: 因此 DEBUG 级别下也不可能看到凭证。
logger = logging.getLogger("ai_trader.market.feed")
_WS_LOGGER = logging.getLogger("ai_trader.market.feed.ws")

#: 兼容导出：脱敏规则与实现已统一到 :mod:`ai_trader.log_redaction`（F-2），
#: 这里不再保留第二份规则表；旧名字继续可用，外部调用与既有测试不受影响。
redact_log_text = redact_text
_RedactionFilter = RedactionFilter
_REDACTION = RedactionFilter()

#: logger 级 filter 只是纵深防御：真正覆盖**子 logger** 的是 handler 级 filter
#: （父 logger 的 filter 不会处理子 logger 传播上来的记录）。
for _target in (logger, _WS_LOGGER):
    _target.addFilter(_REDACTION)


def register_feed_secrets(headers: Dict[str, Any]) -> None:
    """把一次建连实际使用的认证值登记进脱敏表（不给调用方任何回读能力）。"""
    register_secret(headers.get("X-Api-Key"))
    register_secret(headers.get("X-Session"))


try:  # websockets 是运行期依赖，但导入失败不应连累模块可用性
    from websockets.exceptions import ConnectionClosedOK as _ConnectionClosedOK
except Exception:  # pragma: no cover - 老版本/无 websockets
    _ConnectionClosedOK = None  # type: ignore[assignment]

# 兜底：万一某版本/某调用路径没接受 ``logger=`` 参数，库会退回自己的
# ``websockets.client`` logger。同一个脱敏实现也挂上去，保证"换 logger
# 也不漏"（这正是 F-2 要修的病根 —— 只钉一个 logger 的级别不算修复）。
if _ConnectionClosedOK is not None:  # 有 websockets 才挂，避免无谓副作用
    logging.getLogger("websockets.client").addFilter(_REDACTION)


class FeedAuthError(RuntimeError):
    """推送认证不可用或被服务端拒绝（致命，不重试）。"""

    code = "FEED_AUTH"


class FeedSubscriptionError(RuntimeError):
    """订阅被拒绝 / 未确认 / 超时（致命，不重试）。"""

    code = "FEED_SUBSCRIBE"


class FeedAckUnverified(FeedSubscriptionError):
    """收到回执，但**无法核实实际订阅了哪些代码**。

    独立审计 C-1：官方回执格式尚未验证。凡是"能关联到请求但无法证明代码级
    接受"的回执，一律归到这里 —— 状态机进入 ``ACK_UNVERIFIED``，绝不
    ``SUBSCRIBED``，也绝不用订阅请求内容反推服务端成功。
    """

    code = "FEED_ACK_UNVERIFIED"



# --------------------------------------------------------------------------- #
# 连接状态
# --------------------------------------------------------------------------- #
class FeedState:
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    SUBSCRIBED = "SUBSCRIBED"
    RECONNECTING = "RECONNECTING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"
    #: 收到回执但**无法核实实际订阅代码**（缺 subRep / 空 / 不可解析 /
    #: 仅 RID 不匹配）。明确区别于 FAILED：网络正常但协议不可确认。
    ACK_UNVERIFIED = "ACK_UNVERIFIED"


@dataclass
class QuoteTick:
    symbol: str
    last: float
    prev_close: Optional[float]
    ts: Optional[datetime]
    received_at: datetime
    source: str
    seq: int = 0

    @property
    def age_seconds(self) -> float:
        ref = self.ts or self.received_at
        return max(0.0, (datetime.now(timezone.utc) - ref.astimezone(timezone.utc)).total_seconds())

    def as_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol, "last": self.last, "prev_close": self.prev_close,
            "ts": self.ts.isoformat() if self.ts else "",
            "received_at": self.received_at.isoformat(),
            "source": self.source, "seq": self.seq,
            "age_seconds": round(self.age_seconds, 3),
        }


# --------------------------------------------------------------------------- #
# 时间戳解析（容错）
# --------------------------------------------------------------------------- #
def parse_ts(value: Any) -> Optional[datetime]:
    """把各种时间戳写法转成 aware datetime。解析不出来返回 None（不猜）。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.astimezone()
    if isinstance(value, (int, float)):
        v = float(value)
        if v > 1e12:        # 毫秒
            v /= 1000.0
        try:
            return datetime.fromtimestamp(v, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    s = str(value).strip()
    if not s:
        return None
    if s.isdigit():
        return parse_ts(int(s))
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.astimezone()
    except ValueError:
        return None


def _first(d: Dict[str, Any], keys) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


_LAST_KEYS = ("lastPrice", "last", "price", "curPrice", "close", "now")
_PREV_KEYS = ("prevClose", "preClose", "prevClosePrice", "yesterdayClose")
_TS_KEYS = ("time", "ts", "t", "timestamp", "updateTime")


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
@dataclass
class FeedConfig:
    url: str = ""
    topics: Tuple[str, ...] = ("qt",)
    codes: Tuple[str, ...] = ()
    heartbeat_timeout: float = 10.0     # 多久收不到任何消息就判定连接已死
    stale_after: float = 5.0            # 单条行情多久算过期
    reconnect_initial: float = 0.2
    reconnect_max: float = 5.0
    max_reconnect_attempts: int = 0     # 0 = 无限
    settle_updates: int = 1             # 重连后需收到几条新行情才认为"已恢复"
    state_path: Optional[str] = None    # 可选：落盘连接状态
    #: 是否要求握手认证（生产必须 True；仅本机回环测试可关）
    require_auth: bool = True
    #: 是否要求服务端订阅回执（生产必须 True；关掉只是为了兼容协议探针）
    require_subscribe_ack: bool = True
    #: 等待订阅回执的秒数
    subscribe_timeout: float = 5.0

    @classmethod
    def from_cfg(cls, raw: Optional[Dict[str, Any]]) -> "FeedConfig":
        raw = raw or {}
        return cls(
            url=str(raw.get("url") or ""),
            topics=tuple(raw.get("topics") or ("qt",)),
            codes=tuple(str(c).lower() for c in (raw.get("codes") or ())),
            heartbeat_timeout=float(raw.get("heartbeat_timeout_seconds", 10)),
            stale_after=float(raw.get("stale_after_seconds", 5)),
            reconnect_initial=float(raw.get("reconnect_initial_seconds", 0.2)),
            reconnect_max=float(raw.get("reconnect_max_seconds", 5)),
            max_reconnect_attempts=int(raw.get("max_reconnect_attempts", 0) or 0),
            settle_updates=int(raw.get("settle_updates", 1) or 1),
            require_auth=bool(raw.get("require_auth", True)),
            require_subscribe_ack=bool(raw.get("require_subscribe_ack", True)),
            subscribe_timeout=float(raw.get("subscribe_timeout_seconds", 5) or 5),
        )


# --------------------------------------------------------------------------- #
# 主体
# --------------------------------------------------------------------------- #
class MarketFeed:
    """带重连 / 心跳超时 / 去重 / 排序 / 时间戳校验的行情推送客户端。"""

    source_tag = "fosun-push"

    def __init__(self, cfg: FeedConfig,
                 connect_factory: Optional[Callable[..., Any]] = None,
                 source_tag: Optional[str] = None,
                 auth_provider: Optional[Callable[[], Dict[str, str]]] = None):
        self.cfg = cfg
        self._connect_factory = connect_factory
        self._auth_provider = auth_provider
        #: 注入的工厂可能不是 websockets 的 connect，需要探测一次参数名
        self._header_kwarg_cache: Dict[int, Optional[str]] = {}
        self.source_tag = source_tag or self.source_tag
        self._lock = threading.RLock()
        self._state = FeedState.DISCONNECTED
        self._state_since = datetime.now(timezone.utc)
        self._ticks: Dict[str, QuoteTick] = {}
        self._last_ts: Dict[str, datetime] = {}
        self._seq = 0
        self._settled: set = set()      # 重连后已确认"数据新鲜"的标的
        #: settle_updates 计数：每个标的已收到的连续有效行情条数
        self._settle_count: Dict[str, int] = {}
        self._reconnects = 0
        self._subscribed = 0
        self._confirmed_topics: List[str] = []
        self._sub_ack_mode = ""
        self._stats = {
            "messages": 0, "quotes": 0, "duplicates": 0, "out_of_order": 0,
            "stale_dropped": 0, "bad_payload": 0, "ts_missing": 0,
            "heartbeats": 0, "heartbeat_timeouts": 0, "errors": 0,
            "auth_failures": 0, "subscribe_failures": 0,
            # P2：订阅回执关联诊断（只记计数与不含内容的一句话）
            "ack_unmatched": 0, "ack_unverifiable": 0,
            "ack_unverified_terminal": 0,
            # 关闭期 asyncio 传输层噪声（不计入 errors）
            "shutdown_noise": 0,
            # D-1：服务端正常关闭（code 1000/1001）与预期 shutdown，不计入 errors
            "normal_closes": 0,
        }
        self._last_unmatched_ack = ""
        self._last_unverified_ack = ""
        #: 是否收到过任何"像回执"的消息（用于区分 FAILED 与 ACK_UNVERIFIED）
        self._ack_seen = False
        self._last_error = ""
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_evt: Optional[asyncio.Event] = None
        self._ws: Any = None
        self._closing = False

    # ------------------------------------------------------------------ #
    # 生命周期（同步 API）
    # ------------------------------------------------------------------ #
    def start(self, timeout: float = 10.0) -> bool:
        """启动后台线程并等待**本次启动**的首次订阅结果。

        P2（审计 F-4）：旧实现在状态已是 FAILED 时立刻 break → 0.00s 返回
        False，而新线程随后又变成 SUBSCRIBED（返回值与事实相反）。
        现在启动前**同步**把状态置为 CONNECTING，再等待本次尝试的终态，
        因此返回值与 ``state`` 的语义一致；且始终有明确超时。
        """
        if self._thread and self._thread.is_alive():
            # 已在运行：如实返回当前是否已订阅，不假报成功
            return self.state == FeedState.SUBSCRIBED
        self._closing = False
        with self._lock:
            # 关键：先落地 CONNECTING，避免沿用上一轮的 FAILED/STOPPED 终态
            self._state = FeedState.CONNECTING
            self._state_since = datetime.now(timezone.utc)
        self._thread = threading.Thread(target=self._thread_main, name="market-feed",
                                        daemon=True)
        self._thread.start()
        deadline = time.time() + max(0.05, float(timeout))
        while time.time() < deadline:
            st = self.state
            if st == FeedState.SUBSCRIBED:
                return True
            if st in (FeedState.FAILED, FeedState.STOPPED,
                      FeedState.ACK_UNVERIFIED):
                # C-1：ACK_UNVERIFIED 也是**本次尝试的终态**，必须立刻返回，
                # 不能傻等到超时再把 last_error 覆盖成"等待超时"（那会抹掉
                # "回执无法核实"这个更准确的诊断）。
                return False
            time.sleep(0.02)
        with self._lock:
            self._last_error = (f"等待首次订阅超时（{timeout:.1f}s）；"
                                f"后台仍在重试，state={self._state}")
        logger.warning("feed.start 超时未就绪（%.1fs）", timeout)
        return False

    def stop(self) -> None:
        self._closing = True
        loop, evt = self._loop, self._stop_evt
        if loop and evt:
            def _shutdown():
                evt.set()
                ws = self._ws
                if ws is not None:
                    asyncio.ensure_future(self._safe_close(ws))
            try:
                loop.call_soon_threadsafe(_shutdown)
            except RuntimeError:
                pass
        if self._thread:
            self._thread.join(timeout=5)
        self._set_state(FeedState.STOPPED)

    @staticmethod
    def _close_code(exc: BaseException) -> Optional[int]:
        """从关闭异常里取 close code（取不到返回 None，**不猜**）。

        先读 ``rcvd``/``sent`` 帧：websockets ≥13 已把 ``ConnectionClosed.code``
        标为 deprecated，直接用它会在每次关闭时刷 DeprecationWarning。
        """
        for attr in ("rcvd", "sent"):
            frame = getattr(exc, attr, None)
            frame_code = getattr(frame, "code", None)
            if isinstance(frame_code, int):
                return frame_code
        code = getattr(exc, "code", None)
        return code if isinstance(code, int) else None

    def _is_normal_close(self, exc: BaseException) -> bool:
        """D-1：是否为**正常关闭**而非网络故障。

        判定依据是 close code 与关闭意图，不看异常类名（测试替身/不同
        websockets 版本抛的类不一样）：

        * ``ConnectionClosedOK``（1000/1001）→ 正常；
        * 任意异常但其 close code == 1000 → 正常；
        * 本端正在 ``stop()``（``_closing``）且没有可判定的 code → 预期关闭。

        心跳超时（``ConnectionError``，无 code，``_closing`` 为假）**不是**正常关闭。
        """
        if _ConnectionClosedOK is not None and isinstance(exc, _ConnectionClosedOK):
            return True
        code = self._close_code(exc)
        if code == 1000:
            return True
        if self._closing and code in (None, 1000, 1001):
            return True
        return False

    def _on_loop_exception(self, loop, context) -> None:
        """事件循环未处理异常的分类器。

        P2（审计 F-5）：正常关闭时，底层传输在 ``loop.close()`` 之后回调
        ``connection_lost`` → ``websockets.streams.feed_eof()`` 抛
        ``EOFError: stream ended``，asyncio 会把它当"未处理异常"打印 traceback。
        这里把**预期的关闭噪声**与**真实传输故障**分开：

        * 预期关闭（``_closing`` 或已 STOPPED/FAILED）的 EOFError/CancelledError
          → 计入 ``shutdown_noise``，不打印；
        * 其它异常 → 计入 ``errors``，只打一行（含类型），**不打印 traceback**，
          也不包含任何消息负载（避免把服务端原文/凭证带进日志）。
        """
        exc = context.get("exception")
        msg = str(context.get("message") or "")
        name = type(exc).__name__ if exc else "unknown"
        expected = bool(self._closing) or self.state in (FeedState.STOPPED,
                                                         FeedState.FAILED,
                                                         FeedState.ACK_UNVERIFIED)
        if expected and (isinstance(exc, (EOFError, asyncio.CancelledError,
                                          ConnectionError)) or "stream ended" in msg):
            with self._lock:
                self._stats["shutdown_noise"] += 1
            return
        if isinstance(exc, EOFError) and "stream ended" in str(exc):
            # 传输层在拆除时的已知噪声：不是连接错误本身（连接错误已在
            # _connect_and_serve 里抛出并进入重试），单独计数，不刷 traceback。
            with self._lock:
                self._stats["shutdown_noise"] += 1
            return
        with self._lock:
            self._stats["errors"] += 1
            self._last_error = f"事件循环异常: {name}: {msg}"[:200]
        logger.warning("feed 事件循环异常（%s）: %s", name, msg[:120])

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        loop.set_exception_handler(self._on_loop_exception)
        try:
            loop.run_until_complete(self._run())
        except asyncio.CancelledError:
            # D-1：预期 shutdown 的取消不是错误，不写 last_error
            with self._lock:
                self._stats["normal_closes"] += 1
            self._set_state(FeedState.STOPPED)
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._last_error = f"{type(exc).__name__}: {exc}"
                self._stats["errors"] += 1
            self._set_state(FeedState.FAILED)
        finally:
            # 先让关闭回调跑完，再关 loop —— 减少 transport 回调打在已关闭 loop 上
            try:
                loop.run_until_complete(asyncio.sleep(0))
            except Exception:  # noqa: BLE001
                pass
            try:
                loop.close()
            except Exception:  # noqa: BLE001
                pass

    def _set_state(self, state: str) -> None:
        with self._lock:
            self._state = state
            self._state_since = datetime.now(timezone.utc)

    # ------------------------------------------------------------------ #
    # 查询（线程安全）
    # ------------------------------------------------------------------ #
    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def is_connected(self) -> bool:
        return self.state in (FeedState.CONNECTED, FeedState.SUBSCRIBED)

    def tick(self, symbol: str) -> Optional[QuoteTick]:
        with self._lock:
            return self._ticks.get(str(symbol).lower())

    def is_fresh(self, symbol: str, now: Optional[datetime] = None) -> Tuple[bool, str]:
        """某标的当前行情是否可用于开仓决策。

        三个条件同时满足：
          1. 连接状态是 SUBSCRIBED
          2. 该标的在重连后已"settled"（收到过足够多的新行情）
          3. 最新行情的时间戳在 ``stale_after`` 内
        """
        sym = str(symbol).lower()
        # 归一化时区：调用方（引擎）经常传 naive datetime
        if now is None:
            now = datetime.now(timezone.utc)
        elif now.tzinfo is None:
            now = now.astimezone(timezone.utc)
        else:
            now = now.astimezone(timezone.utc)
        with self._lock:
            if self._state != FeedState.SUBSCRIBED:
                return False, f"行情连接状态 {self._state}"
            t = self._ticks.get(sym)
            if t is None:
                return False, f"{symbol} 未收到任何行情"
            if t.ts is None:
                return False, f"{symbol} 行情缺少时间戳，无法确认新鲜度"
            if sym not in self._settled:
                return False, f"{symbol} 重连后尚未完成数据校验（settle）"
            age = (now - t.ts.astimezone(timezone.utc)).total_seconds()
            if age > self.cfg.stale_after:
                return False, f"{symbol} 行情已过期（{age:.1f}s > {self.cfg.stale_after:.1f}s）"
            return True, f"{symbol} 行情新鲜（{age:.1f}s）"

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "state": self._state,
                "state_since": self._state_since.isoformat(),
                "url": self.cfg.url,
                "topics": list(self.cfg.topics),
                "codes": list(self.cfg.codes),
                "source": self.source_tag,
                "protocol": "unverified",
                "subscribed_codes": self._subscribed,
                "reconnects": self._reconnects,
                "settled": sorted(self._settled),
                # P0-2：认证状态只报"有没有"，不报任何头的值
                "auth": {
                    "required": bool(self.cfg.require_auth),
                    "provider_configured": self._auth_provider is not None,
                    "failures": self._stats.get("auth_failures", 0),
                },
                # P1：订阅必须"已确认"才计数
                "subscription": {
                    "confirmed_topics": list(self._confirmed_topics),
                    "require_ack": bool(self.cfg.require_subscribe_ack),
                    "ack_mode": self._sub_ack_mode,
                    "timeout_seconds": self.cfg.subscribe_timeout,
                    "failures": self._stats.get("subscribe_failures", 0),
                    # P2：回执关联诊断（计数 + 不含内容的说明）
                    "unmatched_acks": self._stats.get("ack_unmatched", 0),
                    "unverifiable_acks": self._stats.get("ack_unverifiable", 0),
                    "diagnostic": (self._last_unmatched_ack
                                   or self._last_unverified_ack or ""),
                },
                "settle": {
                    "required_updates": max(1, int(self.cfg.settle_updates or 1)),
                    "counts": dict(self._settle_count),
                },
                "ticks": {k: v.as_dict() for k, v in self._ticks.items()},
                "stats": dict(self._stats),
                "last_error": self._last_error,
                "config": {
                    "heartbeat_timeout": self.cfg.heartbeat_timeout,
                    "stale_after": self.cfg.stale_after,
                    "settle_updates": self.cfg.settle_updates,
                },
            }

    # ------------------------------------------------------------------ #
    # 消息处理（同步，便于单测直接调用）
    # ------------------------------------------------------------------ #
    def ingest(self, payload: Dict[str, Any],
               received_at: Optional[datetime] = None) -> List[QuoteTick]:
        """处理一条推送消息，返回本次被**接受**的 tick。

        去重/排序规则：同一标的，时间戳必须严格大于上一次；否则计入
        ``duplicates``（相同）或 ``out_of_order``（更旧），并丢弃。
        """
        received_at = received_at or datetime.now(timezone.utc)
        accepted: List[QuoteTick] = []
        if not isinstance(payload, dict):
            with self._lock:
                self._stats["bad_payload"] += 1
            return accepted
        with self._lock:
            self._stats["messages"] += 1

        topic = payload.get("topic")
        if topic == "hb":
            return accepted                      # 心跳由接收循环单独处理

        env_ts = parse_ts(payload.get("t") or payload.get("time"))
        items = payload.get("items") or []
        if isinstance(items, dict):
            items = [items]

        for item in items:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or item.get("symbol") or "").strip().lower()
            if not code:
                continue
            raw = item.get("msg", item.get("data", item))
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except ValueError:
                    with self._lock:
                        self._stats["bad_payload"] += 1
                    continue
            if not isinstance(raw, dict):
                with self._lock:
                    self._stats["bad_payload"] += 1
                continue

            last = _first(raw, _LAST_KEYS)
            try:
                last_f = float(last)
            except (TypeError, ValueError):
                with self._lock:
                    self._stats["bad_payload"] += 1
                continue
            prev = _first(raw, _PREV_KEYS)
            try:
                prev_f = float(prev) if prev is not None else None
            except (TypeError, ValueError):
                prev_f = None

            ts = parse_ts(_first(raw, _TS_KEYS)) or env_ts
            if ts is None:
                with self._lock:
                    self._stats["ts_missing"] += 1
                # 缺时间戳 → 不进入 ticks（无法做新鲜度判断），但记一笔
                continue

            with self._lock:
                self._seq += 1
                seq = self._seq
                prev_ts = self._last_ts.get(code)
                if prev_ts is not None:
                    if ts == prev_ts:
                        self._stats["duplicates"] += 1
                        continue
                    if ts < prev_ts:
                        self._stats["out_of_order"] += 1
                        continue
                age = (received_at - ts.astimezone(timezone.utc)).total_seconds()
                if self.cfg.stale_after > 0 and age > self.cfg.stale_after:
                    self._stats["stale_dropped"] += 1
                    continue
                tick = QuoteTick(symbol=code, last=last_f, prev_close=prev_f, ts=ts,
                                 received_at=received_at, source=self.source_tag, seq=seq)
                self._ticks[code] = tick
                self._last_ts[code] = ts
                self._stats["quotes"] += 1
                # P1：settle_updates 必须真正控制"数据是否已稳定"
                need = max(1, int(self.cfg.settle_updates or 1))
                seen = self._settle_count.get(code, 0) + 1
                self._settle_count[code] = seen
                if seen >= need:
                    self._settled.add(code)
                accepted.append(tick)
        return accepted

    # ------------------------------------------------------------------ #
    # asyncio 部分
    # ------------------------------------------------------------------ #
    async def _run(self) -> None:
        self._stop_evt = asyncio.Event()
        attempt = 0
        backoff = self.cfg.reconnect_initial
        while not self._closing:
            try:
                self._set_state(FeedState.CONNECTING)
                await self._connect_and_serve()
                attempt = 0
                backoff = self.cfg.reconnect_initial
            except asyncio.CancelledError:
                raise
            except FeedAuthError as exc:
                # 认证问题重试也不会好：直接 FAILED，绝不继续假装在连
                with self._lock:
                    self._last_error = f"{type(exc).__name__}: {exc}"
                    self._stats["errors"] += 1
                    self._stats["auth_failures"] += 1
                self._set_state(FeedState.FAILED)
                return
            except FeedAckUnverified as exc:
                # C-1：协议回执无法核实代码级订阅 → 明确 ACK_UNVERIFIED（非 FAILED）
                with self._lock:
                    self._last_error = f"{type(exc).__name__}: {exc}"
                    self._stats["subscribe_failures"] += 1
                    self._stats["ack_unverified_terminal"] = (
                        self._stats.get("ack_unverified_terminal", 0) + 1)
                self._set_state(FeedState.ACK_UNVERIFIED)
                return
            except FeedSubscriptionError as exc:
                # 订阅被拒 / 权限不足 / 无回执：明确失败，不进入无限重连
                with self._lock:
                    self._last_error = f"{type(exc).__name__}: {exc}"
                    self._stats["errors"] += 1
                    self._stats["subscribe_failures"] += 1
                self._set_state(FeedState.FAILED)
                return
            except Exception as exc:  # noqa: BLE001
                # D-1：正常关闭（服务端 code 1000/1001、或本端 stop()）不是错误：
                # 不计 errors、不写 last_error。真实网络异常仍走下面的错误路径。
                if self._is_normal_close(exc):
                    with self._lock:
                        self._stats["normal_closes"] += 1
                    if self._closing:
                        break
                    # 服务端正常轮换（如发布重启）：仍需重连，但不算故障
                    attempt += 1
                    if (self.cfg.max_reconnect_attempts
                            and attempt > self.cfg.max_reconnect_attempts):
                        self._set_state(FeedState.FAILED)
                        return
                    self._reconnects += 1
                    self._set_state(FeedState.RECONNECTING)
                    with self._lock:
                        self._settled.clear()
                        self._settle_count.clear()
                    await asyncio.sleep(backoff)
                    backoff = min(self.cfg.reconnect_max, backoff * 2)
                    continue
                with self._lock:
                    self._last_error = f"{type(exc).__name__}: {exc}"
                    self._stats["errors"] += 1
                if self._closing:
                    break
                attempt += 1
                if self.cfg.max_reconnect_attempts and attempt > self.cfg.max_reconnect_attempts:
                    self._set_state(FeedState.FAILED)
                    return
                self._reconnects += 1
                self._set_state(FeedState.RECONNECTING)
                # 重连后必须重新 settle，避免用旧缓存直接恢复交易
                with self._lock:
                    self._settled.clear()
                    self._settle_count.clear()
                await asyncio.sleep(backoff)
                backoff = min(self.cfg.reconnect_max, backoff * 2)
        self._set_state(FeedState.STOPPED)

    # ------------------------------------------------------------------ #
    # 认证（P0-2）
    # ------------------------------------------------------------------ #
    @staticmethod
    def _header_kwarg_name(factory: Callable[..., Any]) -> Optional[str]:
        """探测连接工厂接受的头参数名，**不猜**。

        websockets 17.x 是 ``additional_headers``；旧版本曾是 ``extra_headers``。
        注入的测试替身若接受 ``**kwargs``，按当前官方 SDK 的写法用
        ``additional_headers``。两者都不支持时返回 ``None``（调用方 fail-closed）。
        """
        try:
            params = inspect.signature(factory).parameters
        except (TypeError, ValueError):  # 内建/扩展函数拿不到签名
            return "additional_headers"
        if "additional_headers" in params:
            return "additional_headers"
        if "extra_headers" in params:
            return "extra_headers"
        if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
            return "additional_headers"
        return None

    def _headers_kwarg_for(self, factory: Callable[..., Any]) -> Optional[str]:
        key = id(factory)
        if key not in self._header_kwarg_cache:
            self._header_kwarg_cache[key] = self._header_kwarg_name(factory)
        return self._header_kwarg_cache[key]

    def _build_auth_headers(self) -> Dict[str, str]:
        """现取一次认证头。**值绝不外泄**（不记日志、不进异常文本）。"""
        if not self.cfg.require_auth:
            return {}
        if self._auth_provider is None:
            raise FeedAuthError(
                "未配置推送认证提供者，拒绝以匿名身份连接行情服务（fail-closed）")
        try:
            raw = self._auth_provider() or {}
        except Exception as exc:  # noqa: BLE001
            raise FeedAuthError(
                f"获取推送认证信息失败：{type(exc).__name__}") from exc
        api_key = str(raw.get("X-Api-Key") or "").strip()
        session = str(raw.get("X-Session") or "").strip()
        if not api_key or not session:
            raise FeedAuthError(
                "推送认证信息不完整（缺少 X-Api-Key 或 X-Session），拒绝连接")
        headers = {
            "X-Api-Key": api_key,
            "X-Session": session,
            "X-Request-Client": str(raw.get("X-Request-Client") or "ai-trader-feed"),
        }
        # F-2：登记实际值，任何日志里出现该字面量都会被抹成 ***
        register_feed_secrets(headers)
        return headers

    @staticmethod
    def _is_auth_rejection(exc: BaseException) -> bool:
        """握手阶段是否属于"认证/权限"类拒绝（重试无意义）。"""
        status = getattr(exc, "status_code", None)
        if isinstance(status, int) and status in (400, 401, 403, 409):
            return True
        msg = str(exc)
        return bool(re.search(r"\b(400|401|403|409)\b", msg))

    @staticmethod
    def _auth_reject_hint(exc: BaseException) -> str:
        status = getattr(exc, "status_code", None)
        if not isinstance(status, int):
            m = re.search(r"\b(400|401|403|409)\b", str(exc))
            status = int(m.group(1)) if m else 0
        return {
            400: "请求参数缺失（X-Api-Key / X-Session 必填）",
            401: "鉴权失败（X-Api-Key 或 X-Session 无效/过期）",
            403: "行情权限验证失败（账户未开通该行情服务）",
            409: "该账号重复连接过多",
        }.get(int(status or 0), "认证握手被拒绝")

    # ------------------------------------------------------------------ #
    # 连接与订阅
    # ------------------------------------------------------------------ #
    async def _connect_and_serve(self) -> None:
        factory = self._connect_factory
        if factory is None:
            try:
                from websockets.asyncio.client import connect as factory  # websockets>=13
            except ImportError:  # pragma: no cover
                import websockets
                factory = websockets.connect

        # 认证信息每次都现取：Session 会过期，重连必须用新的
        headers = await asyncio.to_thread(self._build_auth_headers)
        kwargs: Dict[str, Any] = {}
        if headers:
            kwarg = self._headers_kwarg_for(factory)
            if kwarg is None:
                raise FeedAuthError(
                    "当前 WebSocket 客户端不接受认证头参数，拒绝匿名连接（fail-closed）")
            kwargs[kwarg] = headers
            # 只对真正的 websockets.connect 生效：让库日志走**带脱敏过滤器**
            # 的 logger（DEBUG 也安全，见模块顶部 _RedactionFilter）
            try:
                if "logger" in inspect.signature(factory).parameters:
                    kwargs.setdefault("logger", _WS_LOGGER)
            except (TypeError, ValueError):
                pass
        try:
            ws = await factory(self.cfg.url, **kwargs)
        except FeedAuthError:
            raise
        except Exception as exc:  # noqa: BLE001
            if self._is_auth_rejection(exc):
                raise FeedAuthError(
                    f"行情推送握手被拒绝：{self._auth_reject_hint(exc)}") from exc
            raise

        self._ws = ws
        try:
            self._set_state(FeedState.CONNECTED)
            # 新连接一律重新 settle
            with self._lock:
                self._settled.clear()
                self._settle_count.clear()
                self._confirmed_topics = []
                self._sub_ack_mode = ""
                self._ack_seen = False
            await self._subscribe_and_confirm(ws)
            self._set_state(FeedState.SUBSCRIBED)
            await self._recv_loop(ws)
        finally:
            self._ws = None
            await self._safe_close(ws)

    @staticmethod
    async def _safe_close(ws) -> None:
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass

    async def _send(self, ws, obj: Dict[str, Any]) -> None:
        await ws.send(json.dumps(obj, ensure_ascii=False))

    @staticmethod
    def _decode_message(raw: Any) -> Optional[Dict[str, Any]]:
        """bytes/str → dict；解不出来返回 None（计入 bad_payload）。"""
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                return None
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _error_text(payload: Dict[str, Any]) -> str:
        """提取服务端错误信息（不含任何本地凭证）。"""
        for key in ("errMsg", "errmsg", "errorMsg", "error", "err"):
            val = payload.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()[:200]
        for key in ("code", "errCode", "errorCode"):
            val = payload.get(key)
            if isinstance(val, bool):
                continue
            if isinstance(val, int) and val != 0:
                return f"{key}={val}"
            if isinstance(val, str) and val.strip().isdigit() and int(val.strip()) != 0:
                return f"{key}={val.strip()}"
        return ""

    @staticmethod
    def _parse_subrep(rep: Any) -> Optional[Dict[str, Optional[Set[str]]]]:
        """把 ``subRep`` 归一化成 ``{topic: {已接受代码} | None}``。

        无法理解的结构返回 ``None``（调用方按"未确认"处理）。
        """
        if isinstance(rep, str):
            s = rep.strip()
            if not s:
                return None
            try:
                rep = json.loads(s)
            except ValueError:
                return None
        if isinstance(rep, dict):
            # 形如 {"items":[{"topic":..,"code":[..]}]}
            if isinstance(rep.get("items"), list):
                rep = rep["items"]
            else:
                out: Dict[str, Optional[Set[str]]] = {}
                for topic, codes in rep.items():
                    if isinstance(codes, dict):
                        codes = codes.get("code") or codes.get("codes") or []
                    if isinstance(codes, str):
                        codes = [codes]
                    if codes is None or codes == []:
                        out[str(topic)] = None
                    elif isinstance(codes, (list, tuple)):
                        out[str(topic)] = {str(c).strip().lower() for c in codes if str(c).strip()}
                    else:
                        out[str(topic)] = None
                return out or None
        if isinstance(rep, list):
            out2: Dict[str, Optional[Set[str]]] = {}
            for node in rep:
                if not isinstance(node, dict):
                    continue
                topic = node.get("topic")
                if topic is None:
                    continue
                codes = node.get("code") or node.get("codes") or []
                if isinstance(codes, str):
                    codes = [codes]
                out2[str(topic)] = ({str(c).strip().lower() for c in codes if str(c).strip()}
                                    if isinstance(codes, (list, tuple)) and codes else None)
            return out2 or None
        return None

    def _timeout_error(self, timeout: float, remaining: int) -> Exception:
        """超时归类：从未收到回执 → FAILED；收到过但不可核实 → ACK_UNVERIFIED。"""
        base = (f"订阅确认超时：{timeout:.1f}s 内未收到有效回执"
                f"（仍有 {remaining} 个主题未确认）")
        if self._ack_seen:
            return FeedAckUnverified(
                base + "；已收到回执但无法核实实际订阅代码"
                + self._ack_diag_suffix())
        return FeedSubscriptionError(base + self._ack_diag_suffix())

    def _ack_diag_suffix(self) -> str:
        """超时诊断后缀：只含计数与固定措辞，**不含**任何回执内容或凭证。"""
        with self._lock:
            bits = []
            if self._stats.get("ack_unmatched"):
                bits.append(f"不匹配回执 {self._stats['ack_unmatched']} 条")
            if self._stats.get("ack_unverifiable"):
                bits.append(f"不可解释回执 {self._stats['ack_unverifiable']} 条")
            detail = self._last_unmatched_ack or self._last_unverified_ack
        if not bits:
            return ""
        return "；回执诊断：" + "、".join(bits) + (f"（{detail}）" if detail else "")

    def _ack_verdict(self, payload: Dict[str, Any],
                     pending: Dict[str, Tuple[str, Set[str]]]
                     ) -> Tuple[Set[str], str]:
        """判断一条消息能否作为**当前待确认请求**的订阅确认。

        P2（审计 F-2）：只接受 requestid **严格命中** pending 的回执。

        * 错误 RID / 缺失 RID / 迟到回执（已被确认过）→ 一律不确认；
          记录一条非敏感诊断（``ack_unmatched``），不做内容兜底；
        * 命中 RID 但回执体无法解释 → 视为"不可可靠关联"，**不确认**
          （继续等待 → 超时 FAILED 并给出可诊断原因），不猜测匹配规则；
        * 只按该 RID 自己的 topic 校验；部分接受直接抛
          :class:`FeedSubscriptionError`，绝不把全部订阅标成功。
        """
        err = self._error_text(payload)
        if err:
            raise FeedSubscriptionError(f"订阅被服务端拒绝：{err}")

        rid = str(payload.get("requestid") or "").strip()
        rep = payload.get("subRep", payload.get("subrep"))
        has_rep = rep not in (None, "", {}, [])
        act = str(payload.get("act") or payload.get("topic") or "").strip().lower()
        looks_like_ack = has_rep or act in ("sub", "subrep", "ack", "suback")
        # C-1：只有 rid **严格命中**本次待确认请求，才算"服务端回了这一单的执"。
        # 命中但没有可核实代码清单 → 超时后归为 ACK_UNVERIFIED；
        # rid 不匹配/缺失 → 根本没收到本请求的回执 → 仍是 FAILED。
        if rid and rid in pending:
            self._ack_seen = True

        # ---- 关键：RID 不匹配 → 绝不确认 ----
        if not rid or rid not in pending:
            if looks_like_ack:
                with self._lock:
                    self._stats["ack_unmatched"] = self._stats.get("ack_unmatched", 0) + 1
                    self._last_unmatched_ack = (
                        f"收到无法关联的回执（rid={'缺失' if not rid else '不匹配'}，"
                        f"has_subRep={has_rep}，act={act or '-'}）")
            return set(), ""

        topic, codes = pending[rid]

        if not has_rep:
            # C-1：RID 匹配只能证明"服务端收到了这个请求"，**不能**证明
            # 任何代码被接受。缺少 subRep / 空 subRep 一律不确认。
            with self._lock:
                self._stats["ack_unverifiable"] = self._stats.get("ack_unverifiable", 0) + 1
                self._last_unverified_ack = (
                    f"回执缺少可核实的代码清单（rid={rid}，act={act or '-'}），"
                    f"已按不可确认处理")
            return set(), ""

        rep_map = self._parse_subrep(rep)
        if rep_map is None:
            # 回执体不可解释 → 无法确认"哪些代码被接受" → 不确认（fail-closed）
            with self._lock:
                self._stats["ack_unverifiable"] = self._stats.get("ack_unverifiable", 0) + 1
                self._last_unverified_ack = (
                    f"回执结构不可解释（rid={rid}），已按不可确认处理")
            return set(), ""

        if topic not in rep_map:
            raise FeedSubscriptionError(
                f"订阅未被服务端确认：回执未包含主题 {topic}")
        got = rep_map.get(topic)
        if got is None:
            # 主题被接受但未列出代码：无法证明"全部代码都被接受" → 不确认
            with self._lock:
                self._stats["ack_unverifiable"] = self._stats.get("ack_unverifiable", 0) + 1
                self._last_unverified_ack = (
                    f"回执未列出代码清单（rid={rid}, topic={topic}），已按不可确认处理")
            return set(), ""
        lack = codes - got
        if lack:
            raise FeedSubscriptionError(
                f"订阅未被全部接受（{topic} 缺 {len(lack)} 个代码）")
        return {rid}, "subRep"

    async def _subscribe_and_confirm(self, ws) -> None:
        """发送订阅并在收到**服务端确认**后才返回。"""
        codes = [str(c).lower() for c in self.cfg.codes]
        if not codes:
            with self._lock:
                self._subscribed = 0
                self._confirmed_topics = []
            return

        pending: Dict[str, Tuple[str, Set[str]]] = {}
        for topic in self.cfg.topics:
            rid = f"sub-{uuid.uuid4().hex[:8]}"
            pending[rid] = (str(topic), set(codes))
            await self._send(ws, {"act": "sub",
                                  "items": [{"topic": topic, "code": codes}],
                                  "requestid": rid})

        if not self.cfg.require_subscribe_ack:
            with self._lock:
                self._subscribed = len(codes)
                self._confirmed_topics = [str(t) for t in self.cfg.topics]
                self._sub_ack_mode = "disabled"
            return

        loop = asyncio.get_running_loop()
        timeout = max(0.1, float(self.cfg.subscribe_timeout or 5.0))
        deadline = loop.time() + timeout
        ack_mode = ""
        while pending:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise self._timeout_error(timeout, len(pending))
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                raise self._timeout_error(timeout, len(pending)) from None

            payload = self._decode_message(raw)
            if payload is None:
                with self._lock:
                    self._stats["bad_payload"] += 1
                continue
            if payload.get("topic") == "hb":
                with self._lock:
                    self._stats["heartbeats"] += 1
                await self._send(ws, {"act": "hb", "hbValue": payload.get("hbValue")})
                continue

            confirmed, mode = self._ack_verdict(payload, pending)
            if confirmed:
                for req_id in confirmed:
                    pending.pop(req_id, None)
                ack_mode = mode or ack_mode
                continue
            # 不是回执的消息：可能是先到的行情快照，正常消费但不算确认
            self.ingest(payload)

        with self._lock:
            self._subscribed = len(codes)
            self._confirmed_topics = [str(t) for t in self.cfg.topics]
            self._sub_ack_mode = ack_mode or "subRep"

    async def _recv_loop(self, ws) -> None:
        """收消息 + 心跳超时看门狗。

        - 收到 ``{"topic":"hb","hbValue":x}`` → 立即回 ``{"act":"hb","hbValue":x}``
        - ``heartbeat_timeout`` 内没有任何消息 → 抛异常触发重连
        """
        assert self._stop_evt is not None
        while not self._closing:
            if self._stop_evt.is_set():
                return
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=self.cfg.heartbeat_timeout)
            except asyncio.TimeoutError:
                # D-1：预期 shutdown 期间的心跳超时不是错误，不写 last_error
                with self._lock:
                    if self._closing:
                        self._stats["shutdown_noise"] += 1
                    else:
                        self._stats["heartbeat_timeouts"] += 1
                        self._last_error = (
                            f"心跳超时：{self.cfg.heartbeat_timeout:.1f}s "
                            f"未收到任何消息")
                raise ConnectionError("heartbeat timeout")
            if isinstance(msg, bytes):
                try:
                    msg = msg.decode("utf-8")
                except UnicodeDecodeError:
                    with self._lock:
                        self._stats["bad_payload"] += 1
                    continue
            payload = self._decode_message(msg)
            if payload is None:
                with self._lock:
                    self._stats["bad_payload"] += 1
                continue
            if payload.get("topic") == "hb":
                with self._lock:
                    self._stats["heartbeats"] += 1
                await self._send(ws, {"act": "hb", "hbValue": payload.get("hbValue")})
                continue
            self.ingest(payload)
