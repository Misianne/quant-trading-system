"""US 模拟交易闭环测试（US-P1 Batch 4.2 专项，与 4.1.4 安全修复无关）。

边界
----
* 全部在 ``tmp_path`` 沙盒里跑，不写 ``data/``、不连生产、不读真实密钥；
* 行情是**固定夹具**（``source=test-fixture``），只用于让撮合/风控可复现；
  真实行情链路由 ``market_us.db`` + Yahoo 抓取在运行时验证；
* ``StubLLM`` 是**传输层测试替身**（本地 HTTP，OpenAI 兼容协议），用于验证
  HTTP 客户端、JSON 解析、信号校验与引擎集成。**它不是 AI**，由它产生的决策
  一律 ``mode=stub-transport``，不得计入"AI 推理"验收。
"""
from __future__ import annotations

import json
import threading
import time
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from ai_trader.ai.base import AIModelError, MarketContext
from ai_trader.ai.providers import OpenAICompatibleModel
from ai_trader.broker.paper import PaperBroker
from ai_trader.us_loop.bars import BarStore
from ai_trader.us_loop.quote_source import HistoricalQuoteSource, NoMarketData
from ai_trader.us_loop.runner import LoopSettings, PaperLoop, decision_moment, restart_proof

TRADE_DATE = date(2026, 11, 6)            # 周五，非 NYSE 假日
BARS_AS_OF = TRADE_DATE - timedelta(days=1)
#: 卖出日：必须晚于买入日。原因见报告 F-USL-03 —— 规则层目前把"当日买入"
#: 无条件从可卖数量里扣掉（market/rules.py:651），T+0 市场当日回转会被误拦。
SELL_DATE = date(2026, 11, 9)


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
def _weekdays_ending(end: date, n: int) -> List[date]:
    out: List[date] = []
    cur = end
    while len(out) < n:
        if cur.weekday() < 5:
            out.append(cur)
        cur -= timedelta(days=1)
    return list(reversed(out))


def make_bars(n: int = 200, start_price: float = 100.0, step: float = 0.25,
              wiggle: float = 0.8) -> List[Dict[str, Any]]:
    rows = []
    for i, d in enumerate(_weekdays_ending(BARS_AS_OF, n)):
        c = start_price + step * i + wiggle * ((i % 5) - 2)
        o = c - step / 2
        rows.append({"trade_date": d.isoformat(), "open": round(o, 4),
                     "high": round(max(o, c) + 0.5, 4), "low": round(min(o, c) - 0.5, 4),
                     "close": round(c, 4), "volume": 1_000_000 + i})
    return rows


@pytest.fixture()
def store(tmp_path: Path) -> BarStore:
    st = BarStore(tmp_path / "market_us.db")
    for sym in ("usAAPL", "usNVDA"):
        st.ingest(sym, make_bars(), source="test-fixture")
    yield st
    st.close()


def _px(tmp_path: Path, sym: str = "usAAPL") -> float:
    st = BarStore(tmp_path / "market_us.db")
    try:
        return float(st.load(sym, BARS_AS_OF, 1)[-1]["close"])
    finally:
        st.close()


def _decision(px: float, action: str = "buy", pct: float = 0.05,
              conf: float = 0.8) -> Dict[str, Any]:
    if action == "buy":
        return {"action": "buy", "confidence": conf, "entry_price": round(px, 2),
                "stop_loss": round(px * 0.95, 2), "take_profit": round(px * 1.10, 2),
                "position_pct": pct, "horizon": "swing",
                "rationale": "夹具：趋势向上，回踩不破", "risk_conditions": ["跌破 20 日线离场"]}
    if action == "sell":
        return {"action": "sell", "confidence": round(conf, 2), "entry_price": round(px, 2),
                "stop_loss": round(px * 1.05, 2), "take_profit": round(px * 0.90, 2),
                "position_pct": 0.0, "horizon": "swing",
                "rationale": "夹具：趋势转弱，离场", "risk_conditions": ["放量下跌"]}
    return {"action": "hold", "confidence": conf, "entry_price": 0, "stop_loss": 0,
            "take_profit": 0, "position_pct": 0.0, "horizon": "swing",
            "rationale": "夹具：信号不足，观望"}


# --------------------------------------------------------------------------- #
# 传输层替身（本地 HTTP；不是 AI）
# --------------------------------------------------------------------------- #
class _Handler(BaseHTTPRequestHandler):
    payload: Dict[str, Any] = {}
    delay: float = 0.0
    raw = ""

    def log_message(self, *a):  # noqa: A003
        return

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0) or 0)
        self.rfile.read(length)
        if _Handler.delay:
            time.sleep(_Handler.delay)
        body = _Handler.raw or json.dumps({
            "choices": [{"message": {"content": json.dumps(_Handler.payload)}}],
            "usage": {"prompt_tokens": 123, "completion_tokens": 45},
        })
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class StubLLM:
    """本地 OpenAI 兼容端点（测试替身）。"""

    def __init__(self):
        self.httpd = HTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        _Handler.raw = ""
        _Handler.delay = 0.0


def _make_loop(tmp_path: Path, stub: Optional[StubLLM], symbols=("usAAPL",),
               cash: float = 130000.0) -> PaperLoop:
    s = LoopSettings(
        trade_date=TRADE_DATE, symbols=list(symbols), data_dir=tmp_path / "data",
        bars_db=tmp_path / "market_us.db", loop_db=tmp_path / "us_loop.db",
        mode="stub-transport" if stub else "replay", sandbox=True,
    )
    llm = ({"base_url": stub.base_url, "api_key": "test-key-not-real", "model": "stub-1"}
           if stub else None)
    loop = PaperLoop(s, llm_override=llm)
    if cash != 130000.0:
        loop.broker._state["cash"]["USD"] = cash
        loop.broker._save()
    return loop


def _seed_position(loop: PaperLoop, sym: str, qty: float, avg_cost: float) -> None:
    """预置一笔"往日买入"的持仓（模拟重启后的既有仓位）。

    不能在同一个沙盒里"先买后卖"：规则层 ``market/rules.py:651`` 把当日买入
    无条件从可卖数量里扣掉（T+0 市场也被扣），且账本 ts 用真实时间、回放
    时钟用交易日，两者混在一起会让 ``orders_since`` 判断失真。
    """
    # PaperBroker 的持仓字典是大小写敏感的，而引擎把所有标的转小写后再查询，
    # 因此预置时必须用小写键，否则会出现"风控看得到、券商看不到"的持仓不足。
    loop.broker._state["positions"][sym.lower()] = {
        "qty": float(qty), "avg_cost": float(avg_cost), "market": "us",
        "currency": "USD", "name": "",
    }
    loop.broker._save()


# --------------------------------------------------------------------------- #
# 行情层（真实数据链路的存储/校验/防未来函数）
# --------------------------------------------------------------------------- #
def test_bars_validation_rejects_dirty_rows(store: BarStore):
    bad = [
        {"trade_date": "2026-11-05", "open": 0, "high": 1, "low": 1, "close": 1, "volume": 1},
        {"trade_date": "2026-11-05", "open": 10, "high": 9, "low": 8, "close": 10, "volume": 1},
        {"trade_date": "not-a-date", "open": 1, "high": 1, "low": 1, "close": 1},
        {"trade_date": "2026-11-05", "open": 10, "high": 11, "low": 12, "close": 10},
        {"trade_date": "2026-11-05", "open": 10, "high": 11, "low": 9, "close": 10, "volume": -1},
    ]
    res = store.ingest("usAAPL", bad, source="test-fixture")
    assert res.rows_written == 0
    assert sum(res.rejected.values()) == len(bad)
    for reason in ("price_out_of_range_open", "high_below_body", "bad_date",
                   "low_above_body", "bad_volume"):
        assert reason in res.rejected, res.rejected


def test_bars_require_source(store: BarStore):
    with pytest.raises(Exception):
        store.ingest("usAAPL", make_bars(3), source="")


def test_bars_are_idempotent(store: BarStore):
    before = store.coverage("usAAPL")["bars"]
    res = store.ingest("usAAPL", make_bars(), source="test-fixture")
    assert res.rows_written == 0 and res.rows_unchanged == before


def test_no_lookahead(store: BarStore):
    src = HistoricalQuoteSource(store, as_of=BARS_AS_OF)
    bars = src.klines("usAAPL", "day", 500)
    assert bars and max(b.ts for b in bars) <= BARS_AS_OF.isoformat()
    with pytest.raises(NoMarketData):
        HistoricalQuoteSource(store, as_of=date(2026, 1, 5)).klines("usAAPL", "day", 5)


def test_quote_source_reports_its_source(store: BarStore):
    src = HistoricalQuoteSource(store, as_of=BARS_AS_OF)
    q = src.quote("usAAPL")
    assert q.last > 0 and q.source.startswith("hist:yahoo")
    assert "test-fixture" in json.dumps(store.coverage("usAAPL")["sources"])


# --------------------------------------------------------------------------- #
# 闭环：AI → 风控 → 撮合 → 账本
# --------------------------------------------------------------------------- #
def test_ai_buy_decision_fills_and_persists(tmp_path, store):
    with StubLLM() as stub:
        _Handler.payload = _decision(_px(tmp_path))
        loop = _make_loop(tmp_path, stub)
        try:
            res = loop.run()
            assert res["run_status"] == "ok", res
            assert res["by_status"].get("ordered") == 1, res["outcomes"]
            d = loop.ledger.list_decisions()[0]
            # 端点在本机回环 → 必须标 stub_transport，绝不能冒充 real_llm
            assert d["action"] == "buy"
            assert d["provenance"] == "stub_transport", d["provenance"]
            assert d["endpoint_class"] == "loopback"
            for field in ("decision_id", "input_version", "prompt_digest", "model_id",
                          "latency_ms", "risk_conditions", "target_pct"):
                assert d[field] not in (None, "", "[]"), field
            assert json.loads(d["usage"])["prompt_tokens"] == 123
            pos = loop.broker.get_positions()
            assert len(pos) == 1 and pos[0].qty > 0
            assert loop.broker.get_summary("USD").cash < 130000.0
            counts = loop.journal.counts()
            assert counts["orders"] == 1 and counts["equity_snapshots"] == 1
        finally:
            loop.close()


def test_ai_sell_decision_closes_position(tmp_path, store):
    with StubLLM() as stub:
        px = _px(tmp_path)
        loop = _make_loop(tmp_path, stub)
        _seed_position(loop, "usAAPL", 10, round(px * 0.98, 2))
        cash_before = loop.broker.get_summary("USD").cash
        _Handler.payload = _decision(px, "sell")
        res = loop.run(now=decision_moment(SELL_DATE))
        assert res["by_status"].get("ordered") == 1, res["outcomes"]
        assert loop.broker.get_positions() == []
        assert loop.broker.get_summary("USD").cash > cash_before
        assert loop.ledger.list_decisions()[0]["action"] == "sell"
        loop.close()


def test_ai_hold_creates_no_order(tmp_path, store):
    with StubLLM() as stub:
        _Handler.payload = _decision(_px(tmp_path), "hold")
        loop = _make_loop(tmp_path, stub)
        res = loop.run()
        assert res["by_status"].get("hold") == 1, res["outcomes"]
        assert loop.journal.counts()["orders"] == 0
        assert loop.ledger.list_decisions()[0]["action"] == "hold"
        loop.close()


def test_ai_unavailable_blocks_entry(tmp_path, store):
    loop = _make_loop(tmp_path, None)          # 无 Key → UnavailableModel，不降级
    res = loop.run()
    assert res["run_status"] == "blocked"
    assert res["by_status"].get("blocked") == 1
    d = loop.ledger.list_decisions()[0]
    assert d["action"] == "blocked" and d["provenance"] == "unavailable"
    assert "AI_UNAVAILABLE" in d["errors"]
    assert loop.journal.counts()["orders"] == 0
    assert loop.readiness()["ai_model_called"]["met"] is False
    loop.close()


def test_ai_bad_json_is_blocked_not_guessed(tmp_path, store):
    with StubLLM() as stub:
        _Handler.raw = json.dumps({"choices": [{"message": {"content": "我觉得可以买"}}]})
        loop = _make_loop(tmp_path, stub)
        res = loop.run()
        assert res["by_status"].get("blocked") == 1, res["outcomes"]
        assert loop.journal.counts()["orders"] == 0
        loop.close()


def test_ai_timeout_is_blocked(tmp_path, store):
    with StubLLM() as stub:
        _Handler.delay = 2.0
        model = OpenAICompatibleModel(base_url=stub.base_url, model="stub-1",
                                      api_key="test-key-not-real", timeout=1)
        ctx = MarketContext(symbol="usaapl", market="us", currency="USD", last_price=100.0)
        with pytest.raises(AIModelError) as ei:
            model.analyze(ctx)
        assert ei.value.code in ("NETWORK", "TIMEOUT") or "timed out" in str(ei.value).lower()


def test_risk_rejects_second_entry_same_symbol(tmp_path, store):
    with StubLLM() as stub:
        _Handler.payload = _decision(_px(tmp_path), pct=0.15)
        loop = _make_loop(tmp_path, stub)
        r1 = loop.run()
        assert r1["by_status"].get("ordered") == 1, r1["outcomes"]
        r2 = loop.run()
        assert r2["by_status"].get("rejected") == 1, r2["outcomes"]
        assert loop.journal.counts()["orders"] == 1
        loop.close()


def test_duplicate_order_blocked(tmp_path, store):
    with StubLLM() as stub:
        _Handler.payload = _decision(_px(tmp_path), pct=0.02)
        loop = _make_loop(tmp_path, stub)
        loop.run()
        n1 = loop.journal.counts()["orders"]
        loop.run()
        assert loop.journal.counts()["orders"] == n1
        loop.close()


def test_insufficient_cash_never_orders(tmp_path, store):
    with StubLLM() as stub:
        _Handler.payload = _decision(_px(tmp_path), pct=0.15)
        loop = _make_loop(tmp_path, stub, cash=500.0)
        res = loop.run()
        assert res["by_status"].get("ordered") is None
        assert loop.journal.counts()["orders"] == 0
        loop.close()


def test_kill_switch_blocks_scan(tmp_path, store):
    with StubLLM() as stub:
        _Handler.payload = _decision(_px(tmp_path))
        loop = _make_loop(tmp_path, stub)
        loop.cfg.gate.state.kill_switch = True
        res = loop.run()
        assert res["by_status"] == {}
        assert "KILL_SWITCH" in (res["scan_blocked_reason"] or "")
        assert loop.journal.counts()["orders"] == 0
        loop.close()


def test_paper_broker_isolation(tmp_path, store):
    loop = _make_loop(tmp_path, None)
    b = loop.broker
    assert isinstance(b, PaperBroker)
    assert b.venue == "paper" and b.is_live is False
    assert not hasattr(b, "client"), "模拟券商不得持有真实 SDK client"
    g = loop.cfg.gate.state
    assert (g.live_trading, g.order_execution_enabled, g.paper_trading) == (False, False, True)
    loop.close()


def test_restart_recovery(tmp_path, store):
    with StubLLM() as stub:
        _Handler.payload = _decision(_px(tmp_path))
        loop = _make_loop(tmp_path, stub)
        loop.run()
        cash = loop.broker.get_summary("USD").cash
        pos = [(p.symbol, p.qty) for p in loop.broker.get_positions()]
        loop.close()

        s = LoopSettings(trade_date=TRADE_DATE, symbols=["usAAPL"],
                         data_dir=tmp_path / "data", bars_db=tmp_path / "market_us.db",
                         loop_db=tmp_path / "us_loop.db", sandbox=True)
        proof = restart_proof(s)
        assert proof["identical"] is True, proof["diff"]
        assert proof["after"]["account"]["cash"] == pytest.approx(cash, abs=1e-6)
        assert [(p["symbol"], p["qty"]) for p in proof["after"]["positions"]] == pos


def test_idempotent_rerun(tmp_path, store):
    with StubLLM() as stub:
        _Handler.payload = _decision(_px(tmp_path))
        loop = _make_loop(tmp_path, stub)
        loop.run()
        cash = loop.broker.get_summary("USD").cash
        orders = loop.journal.counts()["orders"]
        loop.run()
        loop.run()
        assert loop.journal.counts()["orders"] == orders
        assert loop.broker.get_summary("USD").cash == pytest.approx(cash, abs=1e-6)
        loop.close()


# --------------------------------------------------------------------------- #
# 执行层（不经 AI）：风控 + 撮合 + 记账
# --------------------------------------------------------------------------- #
def test_exec_layer_buy_fills(tmp_path, store):
    loop = _make_loop(tmp_path, None)
    px = _px(tmp_path)
    r = loop.execute_intent("usAAPL", "buy", 10, px, "执行层买入")
    assert r["approved"] and r["order"], r
    assert r["order"]["status"] == "filled" and r["order"]["commission"] > 0
    assert loop.broker.get_positions()[0].qty == 10
    assert loop.broker.get_summary("USD").cash < 130000.0      # 手续费+滑点
    loop.close()


def test_exec_layer_sell_closes_seeded_position(tmp_path, store):
    loop = _make_loop(tmp_path, None)
    px = _px(tmp_path)
    _seed_position(loop, "usAAPL", 10, round(px * 0.98, 2))
    cash_before = loop.broker.get_summary("USD").cash
    r = loop.execute_intent("usAAPL", "sell", 10, px, "执行层卖出",
                            now=decision_moment(SELL_DATE))
    assert r["approved"] and r["order"], r
    assert loop.broker.get_positions() == []
    assert loop.broker.get_summary("USD").cash > cash_before
    assert loop.journal.counts()["orders"] == 1
    loop.close()


def test_unfilled_limit_order_stays_submitted(tmp_path, store):
    loop = _make_loop(tmp_path, None)
    px = _px(tmp_path)
    r = loop.execute_intent("usAAPL", "buy", 5, round(px * 0.99, 2), "挂略低买单")
    assert r["approved"] and r["order"], r
    assert r["order"]["status"] == "submitted"
    assert r["order"]["filled_qty"] == 0
    assert loop.broker.get_positions() == []
    loop.close()


def test_zero_qty_intent_is_rejected(tmp_path, store):
    loop = _make_loop(tmp_path, None)
    r = loop.execute_intent("usAAPL", "buy", 0, _px(tmp_path), "零数量")
    assert r["approved"] is False or r["broker_error"]
    assert loop.journal.counts()["orders"] == 0
    loop.close()


def test_missing_market_data_fails_closed(tmp_path):
    st = BarStore(tmp_path / "empty.db")
    st.ingest("usNVDA", make_bars(5), source="test-fixture")
    st.close()
    s = LoopSettings(trade_date=TRADE_DATE, symbols=["usAAPL"], data_dir=tmp_path / "d",
                     bars_db=tmp_path / "empty.db", loop_db=tmp_path / "l.db", sandbox=True)
    loop = PaperLoop(s)
    res = loop.run()
    assert res["by_status"].get("error") == 1, res["outcomes"]
    assert "没有历史 bar" in res["outcomes"][0]["detail"]
    assert loop.journal.counts()["orders"] == 0
    loop.close()


def test_partial_fill_gap_is_explicit(tmp_path, store):
    """诚实记录缺口：PaperBroker 只保存 allow_partial_fill，从不使用。"""
    src = Path("src/ai_trader/broker/paper.py").read_text(encoding="utf-8")
    uses = [ln.strip() for ln in src.splitlines()
            if "allow_partial_fill" in ln
            and not ln.strip().startswith(("self.allow_partial_fill", "allow_partial_fill:"))]
    assert uses == [], f"若已实现请更新本用例：{uses}"
    loop = _make_loop(tmp_path, None)
    px = _px(tmp_path)
    r = loop.execute_intent("usAAPL", "buy", 10, px, "部分成交能力检查")
    assert r["order"]["filled_qty"] in (0.0, 10.0)
    loop.close()


# --------------------------------------------------------------------------- #
# 来源标记：真实模型 vs 传输层替身（不可仅凭字段伪造）
# --------------------------------------------------------------------------- #
def test_provenance_loopback_is_never_real(tmp_path, store):
    from ai_trader.ai.providers import OpenAICompatibleModel
    from ai_trader.us_loop.decisions import (AIGateway, classify_endpoint,
                                             classify_provenance)
    loop_model = OpenAICompatibleModel(base_url="http://127.0.0.1:9/v1",
                                       model="m", api_key="k")
    remote_model = OpenAICompatibleModel(base_url="https://api.deepseek.com/v1",
                                          model="deepseek-chat", api_key="k")
    assert classify_endpoint(loop_model.base_url) == "loopback"
    assert classify_endpoint(remote_model.base_url) == "remote"
    assert classify_provenance(loop_model, "replay") == "stub_transport"
    assert classify_provenance(remote_model, "replay") == "real_llm"
    assert classify_provenance(remote_model, "stub-transport") == "stub_transport"
    # 强行指定 real_llm 也会被端点类别压回 stub
    assert AIGateway(loop_model, None, provenance="real_llm").provenance == "stub_transport"


def test_loopback_run_reports_stub_and_not_ready(tmp_path, store):
    with StubLLM() as stub:
        _Handler.payload = _decision(_px(tmp_path))
        loop = _make_loop(tmp_path, stub)         # mode=stub-transport + loopback
        res = loop.run()
        assert res["provenance"] == "stub_transport"
        assert res["endpoint_class"] == "loopback"
        rd = loop.readiness()
        assert rd["ai_model_called"]["met"] is False, "回环替身不得计为真实模型调用"
        assert rd["ai_decision_produced"]["met"] is False
        loop.close()


def test_failed_request_marked_rejected_not_unavailable(tmp_path, store):
    with StubLLM() as stub:
        _Handler.raw = json.dumps({"choices": [{"message": {"content": "not-json"}}]})
        loop = _make_loop(tmp_path, stub)
        loop.run()
        d = loop.ledger.list_decisions()[0]
        assert d["provenance"] == "rejected", d["provenance"]
        assert d["endpoint_class"] == "loopback"
        loop.close()
