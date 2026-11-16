"""B5：实时行情推送测试。

分两层：
  A. ``ingest()`` 纯逻辑 —— 去重 / 乱序 / 过期 / 缺时间戳（确定性，无网络）
  B. 本机 WebSocket 服务 —— 连接 / 订阅 / 心跳 / 心跳超时 / 断线重连 / 重连后重订阅

所有网络行为只发生在 127.0.0.1，且行情源标记为 ``local-ws-test``，
**不会** 冒充真实行情。
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest

from ai_trader.audit.journal import AuditJournal
from ai_trader.broker.paper import PaperBroker
from ai_trader.market.feed import FeedConfig, FeedState, MarketFeed, parse_ts
from ai_trader.risk.controller import RiskController
from ai_trader.strategy.engine import StrategyEngine
from tests.conftest import make_cfg
from tests.helpers import FixedSignalModel


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _cfg(**over) -> FeedConfig:
    base = dict(url="ws://127.0.0.1:0/x", topics=("qt",), codes=("hk00700",),
                heartbeat_timeout=1.0, stale_after=2.0,
                reconnect_initial=0.05, reconnect_max=0.2,
                max_reconnect_attempts=0, settle_updates=1,
                # 本文件用的是"不认证、不回执"的极简本机服务；
                # 生产必填的认证/回执由 tests/test_prod_connectivity_p0.py 覆盖。
                require_auth=False, require_subscribe_ack=False)
    base.update(over)
    return FeedConfig(**base)


def _quote(symbol="hk00700", last=300.0, ts: Optional[datetime] = None,
           prev_close=298.0) -> Dict[str, Any]:
    ts = ts or datetime.now(timezone.utc)
    return {"topic": "qt", "items": [{
        "code": symbol,
        "msg": json.dumps({"lastPrice": last, "prevClose": prev_close,
                           "time": ts.isoformat()})}]}


def _wait_until(fn, timeout=6.0, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if fn():
            return True
        time.sleep(interval)
    return False


# --------------------------------------------------------------------------- #
# A. ingest 逻辑
# --------------------------------------------------------------------------- #
def test_ingest_accepts_valid_quote():
    f = MarketFeed(_cfg())
    ticks = f.ingest(_quote(last=300.0))
    assert len(ticks) == 1
    assert ticks[0].last == 300.0
    assert ticks[0].prev_close == 298.0
    assert f.tick("hk00700").last == 300.0
    assert f.snapshot()["stats"]["quotes"] == 1


def test_duplicate_message_dropped():
    f = MarketFeed(_cfg())
    ts = datetime.now(timezone.utc)
    assert len(f.ingest(_quote(ts=ts))) == 1
    assert len(f.ingest(_quote(ts=ts))) == 0        # 同一时间戳 = 重复
    assert f.snapshot()["stats"]["duplicates"] == 1


def test_out_of_order_message_dropped():
    f = MarketFeed(_cfg())
    now = datetime.now(timezone.utc)
    assert len(f.ingest(_quote(last=300.0, ts=now))) == 1
    older = now - timedelta(seconds=1)
    assert len(f.ingest(_quote(last=999.0, ts=older))) == 0
    assert f.tick("hk00700").last == 300.0, "乱序消息不得覆盖最新价"
    assert f.snapshot()["stats"]["out_of_order"] == 1


def test_stale_message_dropped():
    f = MarketFeed(_cfg(stale_after=2.0))
    old = datetime.now(timezone.utc) - timedelta(seconds=10)
    assert f.ingest(_quote(ts=old)) == []
    assert f.snapshot()["stats"]["stale_dropped"] == 1


def test_missing_timestamp_rejected():
    f = MarketFeed(_cfg())
    payload = {"topic": "qt", "items": [{"code": "hk00700",
                                         "msg": json.dumps({"lastPrice": 300.0})}]}
    assert f.ingest(payload) == []
    assert f.snapshot()["stats"]["ts_missing"] == 1


def test_bad_payload_counted():
    f = MarketFeed(_cfg())
    assert f.ingest({"topic": "qt", "items": [{"code": "x", "msg": "not json"}]}) == []
    assert f.ingest({"topic": "qt", "items": [{"code": "x",
                                               "msg": json.dumps({"lastPrice": "abc"})}]}) == []
    assert f.snapshot()["stats"]["bad_payload"] == 2


def test_heartbeat_topic_not_treated_as_quote():
    f = MarketFeed(_cfg())
    assert f.ingest({"topic": "hb", "hbValue": 1}) == []
    assert f.snapshot()["stats"]["quotes"] == 0


def test_is_fresh_requires_subscribed_state():
    f = MarketFeed(_cfg())
    f.ingest(_quote())
    ok, why = f.is_fresh("hk00700")
    assert ok is False
    assert "DISCONNECTED" in why


def test_is_fresh_after_manual_state():
    f = MarketFeed(_cfg())
    f._set_state(FeedState.SUBSCRIBED)
    f.ingest(_quote())
    ok, why = f.is_fresh("hk00700")
    assert ok is True, why

    ok2, why2 = f.is_fresh("usAAPL")
    assert ok2 is False and "未收到" in why2


def test_is_fresh_rejects_old_tick():
    f = MarketFeed(_cfg(stale_after=1.0))
    f._set_state(FeedState.SUBSCRIBED)
    f.ingest(_quote(ts=datetime.now(timezone.utc) - timedelta(seconds=5)))
    # ingest 会因 stale 丢弃 → 没有 tick
    ok, why = f.is_fresh("hk00700")
    assert ok is False


def test_parse_ts_variants():
    assert parse_ts("2026-11-08T10:00:00+08:00") is not None
    assert parse_ts("2026-11-08T02:00:00Z") is not None
    assert parse_ts(1794111136200) is not None        # 毫秒
    assert parse_ts(1794111136) is not None           # 秒
    assert parse_ts("not-a-time") is None
    assert parse_ts(None) is None


# --------------------------------------------------------------------------- #
# B. 本机 WebSocket 服务
# --------------------------------------------------------------------------- #
class WSServer:
    """极简本机推送服务，支持"正常推送 / 静默 / 主动断开"三种模式。"""

    def __init__(self, push_interval=0.05):
        self.push_interval = push_interval
        self.mode = "push"            # push | silent
        self.conns = 0
        self.subs: List[List[str]] = []
        self.hb_sent = 0
        self.hb_acks = 0
        self.port = 0
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop: Optional[asyncio.Event] = None
        self._server = None

    # ---- 生命周期 ----
    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        assert _wait_until(lambda: self.port > 0, 5), "测试服务未启动"
        return self

    def _run(self):
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self._main())

    async def _main(self):
        from websockets.asyncio.server import serve

        self._stop = asyncio.Event()
        async with serve(self._handler, "127.0.0.1", 0) as server:
            self._server = server
            self.port = server.sockets[0].getsockname()[1]
            await self._stop.wait()

    def stop(self):
        if self._loop and self._stop:
            self._loop.call_soon_threadsafe(self._stop.set)
        if self._thread:
            self._thread.join(timeout=5)

    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/msg/v1/push"

    # ---- 协议 ----
    async def _handler(self, ws):
        self.conns += 1
        pusher = asyncio.create_task(self._push_loop(ws))
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                act = msg.get("act")
                if act == "sub":
                    codes = [c for it in (msg.get("items") or [])
                             for c in (it.get("code") or [])]
                    self.subs.append(codes)
                elif act == "hb":
                    self.hb_acks += 1
        except Exception:  # noqa: BLE001
            pass
        finally:
            pusher.cancel()

    async def _push_loop(self, ws):
        n = 0
        while True:
            await asyncio.sleep(self.push_interval)
            if self.mode != "push":
                continue
            n += 1
            now = datetime.now(timezone.utc)
            # 每 10 条发一次心跳（模拟服务端 hb）
            if n % 10 == 0:
                try:
                    await ws.send(json.dumps({"topic": "hb", "hbValue": n}))
                    self.hb_sent += 1
                except Exception:  # noqa: BLE001
                    return
            payload = {"topic": "qt", "t": int(now.timestamp() * 1000),
                       "items": [{"code": "hk00700",
                                  "msg": json.dumps({"lastPrice": 300 + n * 0.01,
                                                     "prevClose": 298.0,
                                                     "time": int(now.timestamp() * 1000)})}]}
            try:
                await ws.send(json.dumps(payload))
            except Exception:  # noqa: BLE001
                return


def _feed_for(server: WSServer, **over) -> MarketFeed:
    cfg = _cfg(url=server.url(), **over)
    return MarketFeed(cfg, source_tag="local-ws-test")


def test_ws_connect_and_subscribe():
    srv = WSServer().start()
    feed = _feed_for(srv)
    try:
        assert feed.start(timeout=6) is True
        assert feed.state == FeedState.SUBSCRIBED
        assert _wait_until(lambda: srv.subs, 5), "服务端没收到订阅"
        assert any("hk00700" in set(codes) for codes in srv.subs)
        assert _wait_until(lambda: feed.tick("hk00700") is not None, 5)
        ok, why = feed.is_fresh("hk00700")
        assert ok is True, why
        snap = feed.snapshot()
        assert snap["source"] == "local-ws-test"
        assert snap["protocol"] == "unverified"
    finally:
        feed.stop()
        srv.stop()


def test_ws_heartbeat_roundtrip():
    srv = WSServer(push_interval=0.02).start()
    feed = _feed_for(srv, heartbeat_timeout=2.0)
    try:
        assert feed.start(timeout=6) is True
        assert _wait_until(lambda: feed.snapshot()["stats"]["heartbeats"] >= 1, 6)
        assert _wait_until(lambda: srv.hb_acks >= 1, 6)
    finally:
        feed.stop()
        srv.stop()


def test_ws_heartbeat_timeout_triggers_reconnect():
    srv = WSServer().start()
    feed = _feed_for(srv, heartbeat_timeout=0.4)
    try:
        assert feed.start(timeout=6) is True
        conns_before = srv.conns
        srv.mode = "silent"                 # 服务端不再发任何消息
        assert _wait_until(
            lambda: feed.snapshot()["stats"]["heartbeat_timeoutseconds"] if False else
            feed.snapshot()["stats"]["heartbeat_timeouts"] >= 1, 8), "未检测到心跳超时"
        assert _wait_until(lambda: srv.conns > conns_before, 8), "未触发重连"
        assert feed.snapshot()["reconnects"] >= 1
    finally:
        feed.stop()
        srv.stop()


def test_ws_reconnect_resubscribes():
    srv = WSServer().start()
    feed = _feed_for(srv, heartbeat_timeout=5.0)
    try:
        assert feed.start(timeout=6) is True
        assert _wait_until(lambda: len(srv.subs) >= 1, 5)
        subs_before = len(srv.subs)

        # 服务端主动断开 → 客户端应重连并重新订阅
        assert _wait_until(lambda: feed.is_connected(), 5)
        _force_drop(srv)
        assert _wait_until(lambda: len(srv.subs) > subs_before, 10), \
            f"重连后没有重新订阅（subs={srv.subs}）"
        assert feed.snapshot()["reconnects"] >= 1
    finally:
        feed.stop()
        srv.stop()


def _force_drop(srv: WSServer):
    """让服务端把当前连接全部关掉。"""
    from websockets.asyncio.server import Server

    async def _drop():
        loop = asyncio.get_running_loop()
        for ws in list(getattr(srv._server, "connections", set())):
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass

    if srv._loop:
        asyncio.run_coroutine_threadsafe(_drop(), srv._loop)


def test_ws_settle_required_after_reconnect():
    """重连后必须收到新行情才允许再次开仓（不拿旧缓存恢复）。"""
    srv = WSServer().start()
    feed = _feed_for(srv, stale_after=1.0, heartbeat_timeout=5.0)
    try:
        assert feed.start(timeout=6) is True
        assert _wait_until(lambda: feed.is_fresh("hk00700")[0], 6)

        # 人为清空 settle（等价于刚重连完成、还没收到数据）
        feed._settled.clear()
        ok, why = feed.is_fresh("hk00700")
        assert ok is False and "settle" in why

        # 收到新 tick 后恢复
        feed.ingest(_quote(last=301.0))
        assert _wait_until(lambda: feed.is_fresh("hk00700")[0], 3)
    finally:
        feed.stop()
        srv.stop()


# --------------------------------------------------------------------------- #
# C. 引擎联动：行情中断暂停开仓，但不影响止损
# --------------------------------------------------------------------------- #
def _engine(tmp_path, feed, feed_markets=("hk",)):
    cfg = make_cfg(tmp_path, **{
        "risk.enforce_market_hours": False,
        "risk.duplicate_window_seconds": 1,
        "market_feed.required_for_entry": True,
        "market_feed.enforce_markets": list(feed_markets),
    })
    journal = AuditJournal(tmp_path / "audit.db", tmp_path / "audit.jsonl")
    broker = PaperBroker(state_path=tmp_path / "paper.json")
    risk = RiskController(cfg, cfg.gate)
    engine = StrategyEngine(cfg, broker, FixedSignalModel(), risk, journal,
                            stops_path=tmp_path / "stops.json",
                            allow_rule_engine_entry=True, feed=feed)
    return cfg, engine, journal, broker


def test_engine_blocks_entry_when_feed_stale(tmp_path):
    feed = MarketFeed(_cfg(url="ws://127.0.0.1:1/x"))
    feed._set_state(FeedState.SUBSCRIBED)      # 已连上但没有任何行情
    cfg, engine, journal, broker = _engine(tmp_path, feed)

    report = engine.scan(["hk00700"], now=datetime.now())
    o = report.outcomes[0]
    assert o.stage == "blocked", o.detail
    assert "FEED_NOT_FRESH" in o.detail
    assert broker.list_orders(limit=5) == []
    assert "entry_blocked" in [e["event"] for e in journal.list_audit(limit=20)]
    journal.close()


def test_engine_allows_entry_when_feed_fresh(tmp_path):
    feed = MarketFeed(_cfg(url="ws://127.0.0.1:1/x"))
    feed._set_state(FeedState.SUBSCRIBED)
    feed.ingest(_quote(last=300.0))
    cfg, engine, journal, broker = _engine(tmp_path, feed)

    report = engine.scan(["hk00700"], now=datetime.now())
    assert report.outcomes[0].stage == "ordered", report.outcomes[0].detail
    journal.close()


def test_engine_exit_not_blocked_by_feed(tmp_path):
    """行情中断只暂停开仓；已有持仓的止损必须照常执行。"""
    feed = MarketFeed(_cfg(url="ws://127.0.0.1:1/x"))
    feed._set_state(FeedState.SUBSCRIBED)
    feed.ingest(_quote(last=300.0))
    cfg, engine, journal, broker = _engine(tmp_path, feed)
    engine.scan(["hk00700"], now=datetime.now())
    pos = broker.get_positions()
    assert pos and pos[0].qty > 0

    # 行情断掉
    feed._set_state(FeedState.DISCONNECTED)
    px = broker.get_quote("hk00700").last
    engine.register_stops("hk00700", stop_loss=px * 1.02, take_profit=None,
                          qty=pos[0].qty)
    executed = engine.monitor(now=datetime.now())
    assert any(e["kind"] == "exit" for e in executed), executed
    journal.close()


def test_feed_snapshot_marks_source_and_protocol():
    feed = MarketFeed(_cfg(url="ws://127.0.0.1:1/x"), source_tag="local-ws-test")
    snap = feed.snapshot()
    assert snap["source"] == "local-ws-test"
    assert snap["protocol"] == "unverified", "协议字段名未验证，不能宣称已核对"
    assert snap["state"] == FeedState.DISCONNECTED
