"""一次性 WS 首帧捕获脚本的离线测试（全程 127.0.0.1，不出网）。

覆盖三件必须成立的事：

1. **结构判定不猜语义**：``classify`` 只按 topic / code / 消息体形态分类；
2. **旁路记录 + 首帧即退订断开**：走真实 ``MarketFeed._connect_and_serve``，
   由注入的 connect_factory 记录原始帧，首帧达成后发 ``act=unsub`` 并关闭；
3. **脱敏不放行**：数值保留、凭据字面量必须被抹掉，且字段名与类型不丢。

本文件里的行情数值是**合成**的（用于验证结构与脱敏），不冒充真实行情。
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "fosun_ws_capture_once",
    Path(__file__).resolve().parents[1] / "tools" / "fosun_ws_capture_once.py")
assert _SPEC and _SPEC.loader
cap = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("fosun_ws_capture_once", cap)
_SPEC.loader.exec_module(cap)


# --------------------------------------------------------------------------- #
# 合成消息体（字段名取自官方 calc.proto::DynamicQuote，数值为合成值）
# --------------------------------------------------------------------------- #
def _synthetic_quote_body(price: int = 3_527_800, pclose: int = 3_562_700) -> Dict[str, Any]:
    return {
        "qtTime": 160001579, "qtDate": 20261116, "power": 4, "tradeStatus": 0,
        "price": price, "chgVal": -34_900, "chgPct": -98, "pClose": pclose,
        "open": 3_560_000, "high": 3_570_000, "low": 3_520_000,
        "vol": 50_292_161, "turnover": 17_812_000_000,
        "week52High": 4_000_000, "week52Low": 2_500_000,
        "stockQuote": {"amp": 141, "tor": 33},
        "calcSendtime": 1_763_331_601_000_000, "avg": 3_540_000,
    }


def _quote_frame(code: str = "usAAPL", **over) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    body = _synthetic_quote_body()
    body.update(over)
    return {"topic": "qt", "t": int(now.timestamp() * 1000),
            "time": int(now.timestamp() * 1_000_000), "id": 1, "cache": 0,
            "items": [{"code": code, "msg": json.dumps(body)}]}


# --------------------------------------------------------------------------- #
# 结构判定
# --------------------------------------------------------------------------- #
def test_classify_quote_by_structure_only():
    kind, extra = cap.classify(_quote_frame(), "usAAPL")
    assert kind == "quote"
    assert extra["body_kind"] == "json_string"
    assert extra["code"] == "usAAPL"


def test_classify_code_match_is_case_insensitive():
    kind, _ = cap.classify(_quote_frame(code="usaapl"), "usAAPL")
    assert kind == "quote"


def test_classify_heartbeat_ack_error():
    assert cap.classify({"topic": "hb", "hbValue": 7}, "usAAPL")[0] == "heartbeat"
    assert cap.classify({"requestid": "r1", "subRep": "[{\"topic\":\"qt\"}]"},
                        "usAAPL")[0] == "subscribe_reply"
    assert cap.classify({"errMsg": {"code": 4001}}, "usAAPL")[0] == "error"
    assert cap.classify({"topic": "qt", "items": []}, "usAAPL")[0] == "other"
    assert cap.classify("not-a-dict", "usAAPL")[0] == "not_object"


def test_classify_ignores_other_symbols():
    frame = _quote_frame(code="usMSFT")
    assert cap.classify(frame, "usAAPL")[0] == "other"


# --------------------------------------------------------------------------- #
# 脱敏
# --------------------------------------------------------------------------- #
def test_describe_frame_keeps_numbers_and_field_names():
    scr = cap.Scrubber(["NOT-A-REAL-KEY-0123456789"])
    doc = cap._describe_frame(_quote_frame(), scr)
    body = doc["items"][0]["body"]
    assert body["price"] == 3_527_800, "数值必须保留原值用于审计字段语义"
    assert body["pClose"] == 3_562_700
    assert body["qtDate"] == 20261116
    assert doc["items"][0]["body_was"] == "json_string"
    paths = {row["path"] for row in doc["field_table"]}
    assert "items[0].body.price" in paths
    assert "items[0].body.qtTime" in paths
    types = {row["path"]: row["type"] for row in doc["field_table"]}
    assert types["items[0].body.price"] == "int"
    assert types["items[0].body.stockQuote"] == "object"
    assert types["topic"] == "string"


def test_describe_frame_scrubs_secret_literals():
    secret = "SUPERSECRETKEY0123456789"
    scr = cap.Scrubber([secret])
    frame = _quote_frame()
    frame["someEcho"] = f"token={secret}"
    frame["items"][0]["msg"] = json.dumps(_synthetic_quote_body())
    doc = cap._describe_frame(frame, scr)
    blob = json.dumps(doc, ensure_ascii=False)
    assert secret not in blob
    assert not scr.contains_secret(blob)


def test_scrubber_ignores_short_values():
    # 过短的字符串不登记，避免把正常数据抹成 ***
    scr = cap.Scrubber(["ab", ""])
    assert scr.text("abc") == "abc"


def test_field_table_marks_long_strings_redacted():
    scr = cap.Scrubber([])
    doc = cap._describe_frame(
        {"topic": "qt", "items": [{"code": "usAAPL", "msg": json.dumps(
            {"note": "x" * 80})}]}, scr)
    assert doc["items"][0]["body"]["note"] == "<redacted:str:len=80>"


# --------------------------------------------------------------------------- #
# 端到端：本机回环 WS 服务 + 真实 MarketFeed
# --------------------------------------------------------------------------- #
class _LoopServer:
    """本机回环推送服务：官方结构 ACK → 一条行情帧 → 等 unsub。"""

    def __init__(self) -> None:
        self.port = 0
        self.subs: List[Dict[str, Any]] = []
        self.unsubs: List[Dict[str, Any]] = []
        self.conns = 0
        self.closed = False
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop: Optional[asyncio.Event] = None

    def start(self) -> "_LoopServer":
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        deadline = time.time() + 5
        while time.time() < deadline and not self.port:
            time.sleep(0.02)
        assert self.port, "测试服务未启动"
        return self

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._main())
        finally:
            loop.close()

    async def _main(self) -> None:
        from websockets.asyncio.server import serve
        self._stop = asyncio.Event()
        async with serve(self._handler, "127.0.0.1", 0) as server:
            self.port = server.sockets[0].getsockname()[1]
            await self._stop.wait()

    def stop(self) -> None:
        if self._loop and self._stop:
            self._loop.call_soon_threadsafe(self._stop.set)
        if self._thread:
            self._thread.join(timeout=5)

    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/msg/v1/push"

    async def _handler(self, ws) -> None:
        self.conns += 1
        try:
            async for raw in ws:
                msg = json.loads(raw)
                act = msg.get("act")
                if act == "sub":
                    self.subs.append(msg)
                    item = (msg.get("items") or [{}])[0]
                    await ws.send(json.dumps({
                        "requestid": msg.get("requestid"),
                        "subRep": json.dumps([{
                            "topic": item.get("topic"),
                            "scode": item.get("code") or [],
                            "fcode": [], "invalid": [], "denied": [],
                        }]),
                    }))
                    await ws.send(json.dumps(_quote_frame()))
                elif act == "unsub":
                    self.unsubs.append(msg)
        except Exception:  # noqa: BLE001
            pass
        finally:
            self.closed = True


class _FakeHolder:
    api_key = "test-api-key-value"
    http_requests = 0
    handshake_ms = 0.0

    def get(self) -> str:
        return "test-session-value"


def test_end_to_end_capture_sends_unsub_and_closes(monkeypatch):
    srv = _LoopServer().start()
    monkeypatch.setattr(cap, "WS_URL", srv.url())
    monkeypatch.setattr(cap, "MAX_SECONDS", 8.0)

    tap = cap.FrameTap(cap.CODE)
    result: Dict[str, Any] = {}
    try:
        asyncio.run(cap.capture(_FakeHolder(), tap, _feed_mod(), result))
    finally:
        srv.stop()

    assert result["connected"] is True
    assert tap.first_valid is not None, result
    assert tap.first_ack is not None, result
    assert result["timed_out"] is False
    kinds = result["frames_by_kind"]
    assert kinds.get("subscribe_reply") == 1
    assert kinds.get("quote") == 1
    assert result["subscribe_verdict"]["verdict"] == "ACCEPTED"
    # 首帧达成 → 必须发过 unsub 且报文中代码保持原样大小写
    assert srv.unsubs, "首帧后没有退订"
    assert srv.unsubs[0]["items"][0]["code"] == [cap.CODE]
    # 订阅报文同理（F-WS-1）
    assert srv.subs[0]["items"][0]["code"] == [cap.CODE]
    assert result["connect_seconds"] < 8.0


def _feed_mod():
    from ai_trader.market import feed as feed_mod
    return feed_mod


def test_end_to_end_no_valid_frame_reports_timeout(monkeypatch):
    """服务端只回 ACK、不推行情 → 到点必须 NO_VALID_FRAME，且不得重连。"""
    class _Silent(_LoopServer):
        async def _handler(self, ws) -> None:
            self.conns += 1
            try:
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("act") == "sub":
                        self.subs.append(msg)
                        await ws.send(json.dumps({"requestid": msg.get("requestid")}))
            except Exception:  # noqa: BLE001
                pass
            finally:
                self.closed = True

    srv = _Silent().start()
    monkeypatch.setattr(cap, "WS_URL", srv.url())
    monkeypatch.setattr(cap, "MAX_SECONDS", 1.5)
    tap = cap.FrameTap(cap.CODE)
    result: Dict[str, Any] = {}
    try:
        asyncio.run(cap.capture(_FakeHolder(), tap, _feed_mod(), result))
    finally:
        srv.stop()

    assert tap.first_valid is None
    assert result["timed_out"] is True
    assert srv.conns == 1, f"禁止重连，实际连接 {srv.conns} 次"


def test_classify_does_not_accept_body_less_item():
    frame = {"topic": "qt", "items": [{"code": "usAAPL"}]}
    assert cap.classify(frame, "usAAPL")[0] == "other"


# --------------------------------------------------------------------------- #
# 阶段 C：回执判决（用真实生产回执字节）
# --------------------------------------------------------------------------- #
def _real_ack() -> Dict[str, Any]:
    p = (Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "fosun"
         / "ws_subrep_denied_20261117.json")
    return json.loads(p.read_text(encoding="utf-8"))["frame"]


def test_subrep_verdict_denied_from_real_production_reply():
    v = cap.subrep_verdict(_real_ack(), "usAAPL")
    assert v["verdict"] == "DENIED"
    assert v["rejected"] == ["usAAPL"]
    assert v["scode_present"] is False, "真实回执只有 denied，没有 scode"


def test_subrep_verdict_buckets():
    assert cap.subrep_verdict(
        {"subRep": [{"topic": "qt", "scode": ["usAAPL"]}]}, "usAAPL")["verdict"] == "ACCEPTED"
    assert cap.subrep_verdict({"requestid": "x"}, "usAAPL")["verdict"] == "UNVERIFIABLE"
    assert cap.subrep_verdict(None, "usAAPL")["verdict"] == "NO_ACK"
    # 同时出现在成功与拒绝桶：以拒绝为准，绝不报成功
    both = {"subRep": [{"topic": "qt", "scode": ["usAAPL"], "denied": ["usAAPL"]}]}
    assert cap.subrep_verdict(both, "usAAPL")["verdict"] == "DENIED"
    # 有清单但目标代码不在任何桶里
    other = {"subRep": [{"topic": "qt", "scode": ["usMSFT"]}]}
    assert cap.subrep_verdict(other, "usAAPL")["verdict"] == "NOT_ACCEPTED"
    # 字符串形态（JSON 传输层可能把 subRep 作为字符串下发）
    assert cap.subrep_verdict(
        {"subRep": json.dumps([{"topic": "qt", "invalid": ["usAAPL"]}])},
        "usAAPL")["verdict"] == "DENIED"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
