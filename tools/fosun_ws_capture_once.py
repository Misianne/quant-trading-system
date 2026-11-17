#!/usr/bin/env python3
"""一次性生产 WebSocket 首帧捕获（受控、只读、不交易）。

严格限制（由代码强制，无命令行开关可放宽）
------------------------------------------
* 只连 ``wss://openapi-push.fosunxcz.com/msg/v1/push``（硬编码，不接受覆盖）；
* 只订阅主题 ``qt``、只订阅证券代码 ``usAAPL``；
* 连接最长 ``MAX_SECONDS = 60`` 秒；
* 最多保存第一条有效行情帧，收到即发 ``unsub`` 并断开；
* 不自动重连 / 不自动重试 / 不循环订阅（只调用一次连接，无重连循环）；
* 不发起任何**数据** REST 请求（只有建立会话所必需的一次 SessionCreate 握手，
  且该握手只做 ECDH/签名，不读取任何行情、账户、资金、订单、成交）；
* 不修改任何生产密钥，只读取既有凭据。

为什么复用 ``MarketFeed`` 而不是另写一条 WebSocket 实现
------------------------------------------------------
连接、认证头、订阅报文、收包解析全部走 ``ai_trader.market.feed.MarketFeed``
（生产同一份代码）。本脚本只做两件它没做的事：

1. **旁路记录原始帧**——通过 ``connect_factory`` 注入口包一层 ``recv``，
   把服务端**原样**发来的字节记下来（不改 ``feed.py`` 的解析逻辑）；
2. **单次连接**——直接调用 ``_connect_and_serve()``，绕开 ``_run()`` 的
   重连循环，因为本轮明确禁止自动重连。

有效性判定不含任何字段语义猜测
------------------------------
一条"有效行情帧"只按**结构**判定：``topic`` 为订阅主题、``items`` 里存在
``code`` 与订阅代码（大小写不敏感）相符的条目、且该条目的消息体是
JSON 字符串或对象。**不**假设哪个字段是价格、不假设时间单位。
字段含义留给离线审计阶段按官方 ``push.proto`` / ``calc.proto`` 核对。

输出
----
``first_frame.json``：脱敏后的首帧结构 + 字段类型表 + 各阶段证据；
``run.json``：本次运行的阶段结论与计数（不含任何凭据）。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# 硬约束（不可由命令行覆盖）
# --------------------------------------------------------------------------- #
WS_URL = "wss://openapi-push.fosunxcz.com/msg/v1/push"
TOPIC = "qt"
CODE = "usAAPL"
MAX_SECONDS = 60.0
HANDSHAKE_TIMEOUT = 12.0
OUT_DIR = Path.home() / "fosunai" / "ws_capture"
ENV_FILE = Path.home() / "fosunai" / "fosun.env"

ENV_KEYS = ("FSOPENAPI_BASE_URL", "FSOPENAPI_API_KEY",
            "FSOPENAPI_CLIENT_PRIVATE_KEY", "FSOPENAPI_SERVER_PUBLIC_KEY")

#: 脱敏后仍保留原值的字段名白名单（都是公共市场数据/协议控制字段，
#: 不含账户、会话、密钥）。其余字符串一律脱敏为 ``<redacted:str:len=NN>``。
SAFE_STR_KEYS = {
    "topic", "code", "symbol", "act", "currency", "market", "name",
    "requestid", "scode", "fcode", "invalid", "denied", "type",
}
#: 数字/布尔/None 一律保留：报价数值是公开市场数据，且审计字段语义必须看原值。
MAX_ITEMS_KEPT = 1          # 只保留满足结构的条目数，避免整包堆数据


# --------------------------------------------------------------------------- #
# 环境与脱敏
# --------------------------------------------------------------------------- #
def load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def read_secret(v: str) -> str:
    v = (v or "").strip()
    return Path(v[1:]).read_text(encoding="utf-8") if v.startswith("@") else v


class Scrubber:
    """把已知敏感值从任何字符串里抹掉，并在落盘前做硬断言。"""

    def __init__(self, secrets):
        self.secrets = [s for s in secrets if s and len(s) >= 8]

    def text(self, value: str) -> str:
        out = str(value)
        for s in self.secrets:
            if s in out:
                out = out.replace(s, "***")
        return out

    def contains_secret(self, blob: str) -> bool:
        return any(s in blob for s in self.secrets)


def redact_value(value: Any, key: Optional[str], scr: Scrubber) -> Any:
    """递归脱敏：数值/布尔/None 保留原值；字符串按白名单决定是否保留。"""
    if isinstance(value, dict):
        return {str(k): redact_value(v, str(k), scr) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(v, key, scr) for v in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        cleaned = scr.text(value)
        if key in SAFE_STR_KEYS:
            return cleaned
        # 短 token（枚举/状态码之类）保留；长串/疑似凭据一律脱敏
        if len(cleaned) <= 24 and cleaned and all(32 <= ord(c) < 127 for c in cleaned):
            return cleaned
        return f"<redacted:str:len={len(cleaned)}>"
    return f"<redacted:{type(value).__name__}>"


def type_of(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def field_table(obj: Any, prefix: str = "", out: Optional[List[Dict[str, Any]]] = None,
                depth: int = 0) -> List[Dict[str, Any]]:
    """把（已脱敏的）帧展开成 ``path / 类型 / 长度 / 值预览`` 表。"""
    if out is None:
        out = []
    if depth > 6:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else str(k)
            out.append({"path": path, "type": type_of(v),
                        "len": len(v) if isinstance(v, (list, str)) else None})
            if isinstance(v, (dict, list)):
                field_table(v, path, out, depth + 1)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            path = f"{prefix}[{i}]"
            out.append({"path": path, "type": type_of(v),
                        "len": len(v) if isinstance(v, (list, str)) else None})
            if isinstance(v, (dict, list)):
                field_table(v, path, out, depth + 1)
    return out


# --------------------------------------------------------------------------- #
# 结构判定（不猜字段语义）
# --------------------------------------------------------------------------- #
def _items_of(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    items = payload.get("items")
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        return []
    return [it for it in items if isinstance(it, dict)]


def classify(payload: Any, code: str) -> Tuple[str, Optional[Dict[str, Any]]]:
    """按**结构**给一条消息分类；不解释任何字段含义。"""
    if not isinstance(payload, dict):
        return "not_object", None
    if str(payload.get("topic") or "").strip().lower() == "hb":
        return "heartbeat", None
    if payload.get("subRep") not in (None, "", [], {}) or payload.get("subrep") not in (None, "", [], {}):
        return "subscribe_reply", None
    if payload.get("errMsg") not in (None, "", {}, []):
        return "error", None
    for it in _items_of(payload):
        got = str(it.get("code") or it.get("symbol") or "").strip()
        if got.lower() != code.lower():
            continue
        body = it.get("msg", it.get("data"))
        if isinstance(body, str):
            kind = "json_string"
        elif isinstance(body, dict):
            kind = "object"
        else:
            continue
        return "quote", {"code": got, "body_kind": kind, "item": it}
    return "other", None


# --------------------------------------------------------------------------- #
# 旁路记录：包装连接对象，只加观测，不改解析
# --------------------------------------------------------------------------- #
class TappedWs:
    """``MarketFeed`` 只用到 ``recv`` / ``send`` / ``close``，其余属性透传。"""

    def __init__(self, ws, tap: "FrameTap"):
        self._ws = ws
        self._tap = tap

    async def recv(self, *a, **kw):
        raw = await self._ws.recv(*a, **kw)
        trigger = self._tap.on_frame(raw)
        if trigger:
            await self._tap.finish(self._ws)
        return raw

    async def send(self, data):
        self._tap.on_send(data)
        return await self._ws.send(data)

    async def close(self, *a, **kw):
        return await self._ws.close(*a, **kw)

    def __getattr__(self, name):
        return getattr(self._ws, name)


class FrameTap:
    def __init__(self, code: str):
        self.code = code
        self.t_connected: Optional[datetime] = None
        self._raw_ws: Any = None
        self.frames: List[Dict[str, Any]] = []      # 仅本进程内存，用于统计
        self.first_valid: Optional[Dict[str, Any]] = None
        self.first_valid_recv: Optional[datetime] = None
        self.first_ack: Optional[Dict[str, Any]] = None
        self.hb_samples: List[Dict[str, Any]] = []
        self.errors: List[str] = []
        self.sent: List[Dict[str, Any]] = []
        self.unsub_sent_at: Optional[datetime] = None
        self.unsub_payload: Optional[Dict[str, Any]] = None
        self.close_requested_at: Optional[datetime] = None
        self.done = None                            # asyncio.Event

    # ---- 工厂 ----
    def make_factory(self):
        async def _connect(url, **kwargs):
            from websockets.asyncio.client import connect
            ws = await connect(url, **kwargs)
            self.t_connected = datetime.now(timezone.utc)
            self._raw_ws = ws
            return TappedWs(ws, self)
        return _connect

    # ---- 记录 ----
    def on_send(self, data: Any) -> None:
        try:
            obj = json.loads(data) if isinstance(data, (str, bytes)) else None
        except (ValueError, TypeError):
            obj = None
        if isinstance(obj, dict):
            self.sent.append({"at": datetime.now(timezone.utc).isoformat(), "obj": obj})

    def on_frame(self, raw: Any) -> bool:
        recv_at = datetime.now(timezone.utc)
        if isinstance(raw, bytes):
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                self.frames.append({"kind": "undecodable", "recv_at": recv_at})
                return False
        else:
            text = str(raw)
        try:
            payload = json.loads(text)
        except ValueError:
            self.frames.append({"kind": "not_json", "recv_at": recv_at,
                                "raw_len": len(text)})
            return False

        kind, extra = classify(payload, self.code)
        self.frames.append({"kind": kind, "recv_at": recv_at,
                            "topic": payload.get("topic") if isinstance(payload, dict) else None})
        if kind == "heartbeat":
            if len(self.hb_samples) < 1:
                self.hb_samples.append(payload)
        elif kind == "subscribe_reply":
            if self.first_ack is None:
                self.first_ack = payload
        elif kind == "error":
            self.errors.append(str(payload.get("errMsg"))[:200])
        elif kind == "quote" and self.first_valid is None:
            self.first_valid = payload
            self.first_valid_recv = recv_at
            return True
        return False

    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for f in self.frames:
            out[f["kind"]] = out.get(f["kind"], 0) + 1
        return out

    async def finish(self, ws) -> None:
        """首帧达成：立即退订并断开（官方 act=unsub 报文）。"""
        rid = f"unsub-{uuid.uuid4().hex[:8]}"
        body = {"act": "unsub", "items": [{"topic": TOPIC, "code": [CODE]}],
                "requestid": rid}
        try:
            self.unsub_sent_at = datetime.now(timezone.utc)
            self.unsub_payload = body
            await ws.send(json.dumps(body, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            self.errors.append(f"unsub_send_failed:{type(exc).__name__}")
        self.close_requested_at = datetime.now(timezone.utc)
        try:
            await ws.close()
        except Exception as exc:  # noqa: BLE001
            self.errors.append(f"close_failed:{type(exc).__name__}")


# --------------------------------------------------------------------------- #
# 会话（复用既有生产凭据；只做签名握手，不读任何业务数据）
# --------------------------------------------------------------------------- #
class SessionHolder:
    def __init__(self, base_url: str, api_key: str, priv_pem: str, server_pem: str,
                 proto):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._proto = proto
        self._identity = proto.load_private_key_pem(priv_pem)
        self._server_pub = proto.load_public_key_pem(server_pem)
        self._session = None
        self.http_requests = 0
        self.handshake_ms: Optional[float] = None

    def get(self) -> str:
        if self._session is None:
            t0 = time.monotonic()
            self._session = self._handshake()
            self.handshake_ms = round((time.monotonic() - t0) * 1000, 1)
        return self._session

    def _handshake(self) -> str:
        import requests
        proto = self._proto
        ephemeral, eph_pub = proto.generate_ephemeral_keypair()
        nonce = proto.new_nonce()
        signature = proto.sign_handshake(self._identity, eph_pub, nonce)
        headers = {"X-Lang": "zhCn",
                   "Content-Type": "application/json",
                   "Accept": "application/json",
                   "X-API-Key": self.api_key,
                   "X-Request-Id": os.urandom(8).hex(),
                   "X-Request-Client": "fosunai-ws-capture",
                   "X-timestamp": str(int(time.time() * 1000)),
                   "X-source": "python-sdk",
                   "X-product": "sdk",
                   "X-Nonce": nonce,
                   "X-Signature": signature}
        self.http_requests += 1
        resp = requests.post(f"{self.base_url}/api/v1/auth/SessionCreate",
                             json={"apiKey": self.api_key,
                                   "clientTempPublicKey": eph_pub},
                             headers=headers, timeout=HANDSHAKE_TIMEOUT, verify=True)
        resp.raise_for_status()
        body = resp.json()
        data = body.get("content") if isinstance(body, dict) and "encrypted" in body else body
        if not isinstance(data, dict) or int(data.get("code", -1)) != 0:
            raise RuntimeError(
                f"HANDSHAKE_REJECTED code={data.get('code') if isinstance(data, dict) else None}")
        payload = data.get("data") or {}
        session_id = payload.get("sessionId")
        server_temp = payload.get("serverTempPublicKey")
        srv_sig = resp.headers.get("X-Signature")
        srv_nonce = resp.headers.get("X-Nonce")
        if not (session_id and server_temp and srv_sig and srv_nonce):
            raise RuntimeError("HANDSHAKE_INCOMPLETE")
        if not proto.verify_handshake(self._server_pub, server_temp, srv_nonce, srv_sig):
            raise RuntimeError("SERVER_SIGNATURE_INVALID")
        proto.derive_session_keys(ephemeral, server_temp, nonce, srv_nonce)
        return str(session_id)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


async def capture(holder: SessionHolder, tap: FrameTap, feed_mod, result: Dict[str, Any]) -> None:
    FeedConfig = feed_mod.FeedConfig
    MarketFeed = feed_mod.MarketFeed

    cfg = FeedConfig(
        url=WS_URL,
        topics=(TOPIC,),
        codes=(CODE,),
        # 本轮到 60 秒为止都不允许重连：直接把心跳超时放宽到窗口之外，
        # 让"谁先到"完全由脚本的总时限决定，而不是被内部看门狗打断。
        heartbeat_timeout=MAX_SECONDS + 10.0,
        stale_after=0.0,                 # 不因新鲜度丢帧（本轮只取证，不做风控判定）
        reconnect_initial=0.5,
        reconnect_max=0.5,
        max_reconnect_attempts=1,
        settle_updates=1,
        require_auth=True,
        # 回执核实在本轮由"原始帧证据"独立完成：线上字节与 require_ack=True 完全
        # 相同，只是不因为回执格式尚未在生产验证过而提前中止这次唯一的机会。
        require_subscribe_ack=False,
        subscribe_timeout=5.0,
    )

    def _auth() -> Dict[str, str]:
        return {"X-Api-Key": holder.api_key,
                "X-Session": holder.get(),
                "X-Request-Client": "fosunai-ws-capture"}

    feed = MarketFeed(cfg, connect_factory=tap.make_factory(), auth_provider=_auth)

    async def _serve() -> None:
        # 直接走单次连接；``_run()`` 的重连循环本轮禁用。
        feed._stop_evt = asyncio.Event()          # _recv_loop 依赖它
        await feed._connect_and_serve()

    t_start = datetime.now(timezone.utc)
    task = asyncio.create_task(_serve())
    try:
        await asyncio.wait({task}, timeout=MAX_SECONDS)
    finally:
        result["connect_seconds"] = round(
            (datetime.now(timezone.utc) - t_start).total_seconds(), 3)

    if not task.done():
        result["timed_out"] = True
        # 超时：仍然先退订再断开
        probe = getattr(feed, "_ws", None)
        if probe is not None:
            await tap.finish(probe)
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    else:
        result["timed_out"] = False
        exc = task.exception()
        result["serve_exception"] = (
            f"{type(exc).__name__}: {str(exc)[:200]}" if exc else None)

    result["feed"] = {
        "state": feed.state,
        "stats": dict(feed.snapshot()["stats"]),
        "ticks": feed.snapshot()["ticks"],
        "last_error": feed.snapshot()["last_error"],
    }
    result["connected"] = tap.t_connected is not None
    result["frames_by_kind"] = tap.counts()
    result["subscribe_verdict"] = subrep_verdict(tap.first_ack, CODE)


def _describe_frame(payload: Dict[str, Any], scr: "Scrubber") -> Dict[str, Any]:
    """把一条推送消息拆成"信封字段（逐个脱敏）+ 条目（code + 解析后的消息体）"。

    不整包递归脱敏的原因：条目消息体 ``msg`` 是一个 **JSON 字符串**，
    整包脱敏只会把整串替换成 ``<redacted>``，字段名就全丢了 —— 而字段名
    正是本轮的取证目标。这里显式解析后再逐字段脱敏。
    """
    envelope: Dict[str, Any] = {}
    for k, v in payload.items():
        if k in ("items", "subRep", "subrep"):
            continue
        envelope[str(k)] = {"type": type_of(v),
                            "value": redact_value(v, str(k), scr)}

    items_out: List[Dict[str, Any]] = []
    for it in _items_of(payload)[:MAX_ITEMS_KEPT]:
        node: Dict[str, Any] = {"keys": sorted(it.keys())}
        node["code"] = {"type": type_of(it.get("code")),
                        "value": redact_value(it.get("code"), "code", scr)}
        body = it.get("msg", it.get("data"))
        if isinstance(body, str):
            node["body_was"] = "json_string"
            node["body_raw_len"] = len(body)
            try:
                parsed = json.loads(body)
            except ValueError:
                parsed = None
            if isinstance(parsed, (dict, list)):
                node["body"] = redact_value(parsed, None, scr)
                node["body_type"] = type_of(parsed)
            else:
                node["body"] = "<redacted:unparsed_string>"
                node["body_type"] = "unparsed"
        elif isinstance(body, dict):
            node["body_was"] = "object"
            node["body"] = redact_value(body, None, scr)
            node["body_type"] = "object"
        else:
            node["body_was"] = type_of(body)
            node["body"] = None
            node["body_type"] = "absent"
        items_out.append(node)

    out: Dict[str, Any] = {"envelope": envelope, "items": items_out}

    rep = payload.get("subRep", payload.get("subrep"))
    if rep not in (None, "", [], {}):
        if isinstance(rep, str):
            try:
                rep = json.loads(rep)
            except ValueError:
                rep = None
        if rep is not None:
            out["sub_rep"] = redact_value(rep, None, scr)
            out["sub_rep_type"] = type_of(rep)
            out["sub_rep_table"] = field_table(redact_value(rep, None, scr), "subRep")

    table: List[Dict[str, Any]] = [{"path": k, "type": v["type"]}
                                   for k, v in envelope.items()]
    if items_out and isinstance(items_out[0].get("body"), dict):
        table.append({"path": "items[0].body", "type": "object"})
        table.extend(field_table(items_out[0]["body"], "items[0].body"))
    out["field_table"] = table
    return out


def subrep_verdict(ack: Optional[Dict[str, Any]], code: str) -> Dict[str, Any]:
    """把订阅回执翻译成**可判决**的结论，而不是"收到一条像回执的消息"。

    按官方 ``push.proto::SubRepItem`` 的四个代码桶判断：
    ``scode``=成功、``fcode``=失败、``invalid``=无效、``denied``=无权限。
    只把 ``scode`` 算作"被接受"，出现在其余三个桶里的代码一律不算。
    """
    if not isinstance(ack, dict):
        return {"verdict": "NO_ACK"}
    rep = ack.get("subRep", ack.get("subrep"))
    if rep in (None, "", [], {}):
        return {"verdict": "UNVERIFIABLE", "reason": "回执不含 subRep（无法核实代码级接受）"}
    nodes: Any = rep
    if isinstance(nodes, str):
        try:
            nodes = json.loads(nodes)
        except ValueError:
            return {"verdict": "UNVERIFIABLE", "reason": "subRep 不是合法 JSON"}
    if isinstance(nodes, dict):
        nodes = [nodes]
    if not isinstance(nodes, list):
        return {"verdict": "UNVERIFIABLE", "reason": "subRep 结构未知"}

    accepted: List[str] = []
    rejected: List[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        for c in node.get("scode") or []:
            accepted.append(str(c))
        for key in ("fcode", "invalid", "denied"):
            for c in node.get(key) or []:
                rejected.append(str(c))
    want = code.strip().lower()
    acc_l = {c.lower() for c in accepted}
    rej_l = {c.lower() for c in rejected}
    if want in acc_l and want not in rej_l:
        verdict = "ACCEPTED"
    elif want in rej_l:
        verdict = "DENIED"
    elif acc_l or rej_l:
        verdict = "NOT_ACCEPTED"
    else:
        verdict = "UNVERIFIABLE"
    return {"verdict": verdict,
            "accepted": accepted, "rejected": rejected,
            "scode_present": any(isinstance(n, dict) and "scode" in n for n in nodes)}


def main() -> int:
    if len(sys.argv) > 1:
        print("REFUSED: 本脚本不接受任何命令行参数（限制硬编码，不可放宽）")
        return 2

    load_env_file(ENV_FILE)
    missing = [k for k in ENV_KEYS if not (os.environ.get(k) or "").strip()]
    if missing:
        print("REFUSED: 缺少凭据环境键 " + ",".join(missing))
        return 2

    try:
        from fsclient import fosun_protocol as proto
    except Exception as exc:  # noqa: BLE001
        print(f"REFUSED: 无法加载既有协议实现 fsclient.fosun_protocol ({type(exc).__name__})")
        return 2
    try:
        from ai_trader.market import feed as feed_mod
    except Exception as exc:  # noqa: BLE001
        print(f"REFUSED: 无法加载既有 MarketFeed ({type(exc).__name__}: {exc})")
        return 2

    api_key = read_secret(os.environ["FSOPENAPI_API_KEY"])
    priv_pem = read_secret(os.environ["FSOPENAPI_CLIENT_PRIVATE_KEY"])
    server_pem = read_secret(os.environ["FSOPENAPI_SERVER_PUBLIC_KEY"])
    base_url = os.environ["FSOPENAPI_BASE_URL"].strip()

    # 敏感值只用于脱敏，不写入任何输出
    scr = Scrubber([api_key])
    holder = SessionHolder(base_url, api_key, priv_pem, server_pem, proto)
    tap = FrameTap(CODE)
    result: Dict[str, Any] = {"url": WS_URL, "topic": TOPIC, "code": CODE,
                              "max_seconds": MAX_SECONDS,
                              "started_at": _iso(datetime.now(timezone.utc))}

    try:
        asyncio.run(capture(holder, tap, feed_mod, result))
    except Exception as exc:  # noqa: BLE001
        result["fatal"] = f"{type(exc).__name__}: {str(exc)[:300]}"

    # 会话 id 也算敏感，加入脱敏表
    if holder._session:
        scr.secrets.append(holder._session)

    result.update({
        "handshake_ok": holder._session is not None,
        "handshake_ms": holder.handshake_ms,
        "http_requests_total": holder.http_requests,
        "http_data_requests": 0,
        "connected": tap.t_connected is not None,
        "connected_at": _iso(tap.t_connected),
        "frames_by_kind": tap.counts(),
        "sent_frames": len(tap.sent),
        "sent_objects": [redact_value(o, None, scr) for o in tap.sent],
        "unsub": {"payload": tap.unsub_payload,
                  "sent_at": _iso(tap.unsub_sent_at),
                  "close_requested_at": _iso(tap.close_requested_at)},
        "heartbeat_sample": redact_value(tap.hb_samples[0], None, scr) if tap.hb_samples else None,
        "errors": [scr.text(e) for e in tap.errors],
        "close_code": getattr(tap._raw_ws, "close_code", None),
    })

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # ---- 首帧：显式拆分"信封字段"与"条目消息体"，逐字段脱敏 ----
    frame_doc: Dict[str, Any] = {
        "capture": {
            "url": WS_URL, "topic": TOPIC, "code": CODE,
            "connected_at": _iso(tap.t_connected),
            "frame_received_at": _iso(tap.first_valid_recv),
            "handshake_ms": holder.handshake_ms,
            "http_requests_total": holder.http_requests,
            "http_data_requests": 0,
        },
        "note": ("数值型字段保留原值（公共市场数据，且审计字段语义必须看原值）；"
                 "字符串按白名单脱敏。field_table 给出每个字段的 JSON 类型。"),
    }
    if tap.first_valid is not None:
        frame_doc.update(_describe_frame(tap.first_valid, scr))
    else:
        frame_doc["frame"] = None
        frame_doc["result"] = "NO_VALID_FRAME"

    if tap.first_ack is not None:
        frame_doc["subscribe_reply"] = _describe_frame(tap.first_ack, scr)
        frame_doc["subscribe_verdict"] = subrep_verdict(tap.first_ack, CODE)
    else:
        frame_doc["subscribe_verdict"] = {"verdict": "NO_ACK"}

    blob = json.dumps(frame_doc, ensure_ascii=False, indent=2, default=str)
    if scr.contains_secret(blob):
        print("ABORT: 脱敏后仍检出凭据字面量，拒绝落盘")
        return 3
    (OUT_DIR / "first_frame.json").write_text(blob, encoding="utf-8")

    blob2 = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    if scr.contains_secret(blob2):
        print("ABORT: 脱敏后仍检出凭据字面量，拒绝落盘")
        return 3
    (OUT_DIR / "run.json").write_text(blob2, encoding="utf-8")

    # ---- 人可读摘要 ----
    verdict = result.get("subscribe_verdict") or subrep_verdict(tap.first_ack, CODE)
    result["subscribe_verdict"] = verdict
    print("STAGE_A_CONNECTED=%s" % ("OK" if tap.t_connected else "FAIL"))
    print("STAGE_B_HANDSHAKE=%s" % ("OK" if holder._session else "FAIL"))
    print("STAGE_C_ACK=%s" % verdict.get("verdict"))
    print("STAGE_D_FIRST_FRAME=%s" % ("OK" if tap.first_valid is not None else "NO_VALID_FRAME"))
    print("FRAMES_BY_KIND=%s" % json.dumps(tap.counts(), ensure_ascii=False))
    print("CONNECT_SECONDS=%s" % result.get("connect_seconds"))
    print("HTTP_REQUESTS_TOTAL=%d" % holder.http_requests)
    print("OUT=%s" % OUT_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
