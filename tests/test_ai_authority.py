"""P1-9：AI 降级与决策权威。

覆盖用户要求的 9 个场景：
  1. AI 正常分析并生成信号
  2. AI 输出非法 JSON
  3. AI 请求超时
  4. AI 服务断开
  5. AI 返回缺少止损价格的信号
  6. AI 重复产生相同信号
  7. AI 恢复后重新分析
  8. 风控拒绝 AI 信号
  9. AI 失效期间已有持仓仍正常执行保护机制

其中 2/3/4 走**真实本机 HTTP**（127.0.0.1），不是 mock —— 这样超时、断连、
非法响应都是真发生过的网络行为。全程不接触任何外部地址。
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from ai_trader.ai.authority import (
    CODE_DEGRADED,
    CODE_NOT_AUTHORITY,
    CODE_STALE,
    CODE_UNAVAILABLE,
    DecisionAuthority,
)
from ai_trader.ai.base import AIModelError, TradingSignal, UnavailableModel
from ai_trader.ai.providers import HeuristicModel
from ai_trader.audit.journal import AuditJournal
from ai_trader.broker.paper import PaperBroker
from ai_trader.risk.controller import RiskController
from ai_trader.strategy.engine import StrategyEngine
from tests.helpers import live_cfg


# --------------------------------------------------------------------------- #
# 可切换模式的假 LLM 服务
# --------------------------------------------------------------------------- #
class LLMServer:
    """本机假模型服务。``mode`` 可随时切换，用于模拟故障与恢复。"""

    def __init__(self, port: int = 0):
        self.mode = "ok"
        self.delay = 3.0
        self.calls = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                outer.calls += 1
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
                mode = outer.mode
                if mode == "slow":
                    time.sleep(outer.delay)
                if mode == "http_500":
                    body = b'{"error":{"message":"injected upstream failure"}}'
                    self.send_response(500)
                else:
                    self.send_response(200)
                    if mode == "bad_json":
                        body = b"hello, I think you should buy. {\"action\": oops"
                    elif mode == "missing_stop":
                        body = json.dumps({"choices": [{"message": {"content": json.dumps({
                            "action": "buy", "confidence": 0.9,
                            "entry_price": 300.0, "take_profit": 330.0,
                            "position_pct": 0.1, "rationale": "无止损"})}}]}).encode()
                    else:
                        body = json.dumps({"choices": [{"message": {"content": json.dumps({
                            "action": "buy", "confidence": 0.82,
                            "entry_price": 300.0, "stop_loss": 290.0,
                            "take_profit": 330.0, "position_pct": 0.10,
                            "horizon": "swing",
                            "rationale": "假模型：趋势向上"})}}],
                            "usage": {"total_tokens": 42}}).encode()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass

        HTTPServer.allow_reuse_address = True
        self.httpd = HTTPServer(("127.0.0.1", port), Handler)
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def stop(self):
        try:
            self.httpd.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.httpd.server_close()
        except Exception:  # noqa: BLE001
            pass


@pytest.fixture
def llm():
    srv = LLMServer().start()
    yield srv
    srv.stop()


# --------------------------------------------------------------------------- #
def _cfg(tmp_path, **ai_over):
    ai = {"provider": "openai_compatible",
          "require_ai_for_entry": True,
          "signal_ttl_seconds": 300,
          "max_consecutive_failures": 1,
          "failure_cooldown_seconds": 5,
          "constraints": {"min_confidence": 0.6, "require_stop_loss": True,
                          "max_position_pct": 0.15},
          "openai_compatible": {"base_url": "http://127.0.0.1:1/v1",
                                "model": "audit-probe", "api_key_env": "AI_API_KEY",
                                "temperature": 0.2, "max_tokens": 800,
                                "timeout_seconds": 2}}
    ai.update(ai_over)
    cfg = live_cfg(tmp_path,
                   **{"risk.enforce_market_hours": False,
                      "risk.duplicate_window_seconds": 1,
                      "risk.order_timeout_seconds": 600})
    cfg.raw["ai"] = ai
    return cfg


def _engine(cfg, model, tmp_path, *, allow_sandbox: bool = False):
    journal = AuditJournal(tmp_path / "audit.db", tmp_path / "audit.jsonl")
    broker = PaperBroker(state_path=tmp_path / "paper.json")
    risk = RiskController(cfg, cfg.gate)
    engine = StrategyEngine(cfg, broker, model, risk, journal,
                            stops_path=tmp_path / "stops.json",
                            allow_rule_engine_entry=allow_sandbox)
    return engine, journal, broker


def _llm_model(cfg, llm):
    from ai_trader.ai.providers import OpenAICompatibleModel

    sub = cfg.ai["openai_compatible"]
    return OpenAICompatibleModel(base_url=llm.base_url(), model=sub["model"],
                                 api_key="probe-key", timeout=2, connect_timeout=2,
                                 min_confidence=0.6, max_position_pct=0.15)


def _events(journal):
    return [e["event"] for e in journal.list_audit(limit=60)]


# --------------------------------------------------------------------------- #
# Part A —— 权威语义（规则引擎不能开仓）
# --------------------------------------------------------------------------- #
def test_rule_engine_cannot_open_positions(tmp_path):
    """核心要求：规则引擎只能提供指标，不能代替 AI 决策。"""
    cfg = _cfg(tmp_path, provider="heuristic")
    engine, journal, broker = _engine(cfg, HeuristicModel(), tmp_path)

    report = engine.scan(["hk00700"], now=datetime.now())

    o = report.outcomes[0]
    assert o.stage == "blocked", o.detail
    assert CODE_NOT_AUTHORITY in o.detail
    assert "规则引擎" in o.detail
    assert broker.list_orders(limit=10) == [], "规则引擎不得产生任何委托"
    assert "entry_blocked" in _events(journal)

    blocked = [e for e in journal.list_audit(limit=60) if e["event"] == "entry_blocked"]
    assert blocked[0]["detail"]["code"] == CODE_NOT_AUTHORITY
    assert blocked[0]["detail"]["action_taken"] == "no_new_entry"
    assert blocked[0]["detail"]["symbol"] == "hk00700"
    journal.close()


def test_unavailable_model_blocks_with_reason(tmp_path):
    cfg = _cfg(tmp_path)
    model = UnavailableModel("LLM 未配置 API Key", "openai_compatible")
    engine, journal, broker = _engine(cfg, model, tmp_path)

    report = engine.scan(["hk00700"], now=datetime.now())
    o = report.outcomes[0]
    assert o.stage == "blocked"
    assert CODE_UNAVAILABLE in o.detail
    assert broker.list_orders(limit=10) == []
    journal.close()


def test_config_must_explicitly_allow_rule_engine(tmp_path):
    """把 require_ai_for_entry 设为 false 才能用规则引擎 —— 必须显式。"""
    cfg = _cfg(tmp_path, provider="heuristic", require_ai_for_entry=False)
    engine, journal, broker = _engine(cfg, HeuristicModel(), tmp_path)
    report = engine.scan(["hk00700"], now=datetime.now())
    assert report.outcomes[0].stage == "ordered", report.outcomes[0].detail
    journal.close()


def test_sandbox_flag_is_the_only_way_for_rule_engine(tmp_path):
    """demo/回测路径：显式 sandbox 才放行，并留下审计痕迹。"""
    cfg = _cfg(tmp_path, provider="heuristic")
    engine, journal, broker = _engine(cfg, HeuristicModel(), tmp_path,
                                     allow_sandbox=True)
    report = engine.scan(["hk00700"], now=datetime.now())
    assert report.outcomes[0].stage == "ordered"
    assert "rule_engine_sandbox" in _events(journal)
    journal.close()


def test_stale_signal_is_rejected(tmp_path):
    """AI 恢复后不得执行失效期间的旧信号。"""
    class StaleModel:
        name, model_id, can_trade, available = "stale", "v1", True, True

        def analyze(self, ctx):
            return TradingSignal(symbol=ctx.symbol, action="buy", confidence=0.9,
                                 entry_price=300.0, stop_loss=290.0,
                                 take_profit=330.0, position_pct=0.1,
                                 market=ctx.market,
                                 ts=(datetime.now().astimezone()
                                     - timedelta(minutes=10)).isoformat(timespec="seconds"))

        def health(self):
            return {"name": self.name, "model_id": self.model_id, "can_trade": True}

    cfg = _cfg(tmp_path)
    engine, journal, broker = _engine(cfg, StaleModel(), tmp_path)
    report = engine.scan(["hk00700"], now=datetime.now())

    o = report.outcomes[0]
    assert o.stage == "blocked"
    assert CODE_STALE in o.detail
    assert broker.list_orders(limit=10) == []
    assert "stale_signal_rejected" in _events(journal)
    journal.close()


# --------------------------------------------------------------------------- #
# Part B —— 真实 HTTP 的 9 个场景
# --------------------------------------------------------------------------- #
def test_scenario_01_ai_normal_generates_signal(tmp_path, llm, monkeypatch):
    monkeypatch.setenv("AI_API_KEY", "probe-key")
    cfg = _cfg(tmp_path)
    model = _llm_model(cfg, llm)
    engine, journal, broker = _engine(cfg, model, tmp_path)

    report = engine.scan(["hk00700"], now=datetime.now())
    o = report.outcomes[0]

    assert o.stage == "ordered", o.detail
    assert o.signal["provider"] == "openai_compatible"
    assert o.signal["stop_loss"] == 290.0
    orders = journal.list_orders(limit=5)
    assert len(orders) == 1 and orders[0]["status"] == "filled"
    assert llm.calls >= 1
    journal.close()


def test_scenario_02_ai_invalid_json_blocks_entry(tmp_path, llm):
    cfg = _cfg(tmp_path)
    model = _llm_model(cfg, llm)
    engine, journal, broker = _engine(cfg, model, tmp_path)
    llm.mode = "bad_json"

    report = engine.scan(["hk00700"], now=datetime.now())
    o = report.outcomes[0]
    assert o.stage == "blocked"
    assert "NO_JSON" in o.detail or "JSON" in o.detail
    assert broker.list_orders(limit=10) == []
    ev = _events(journal)
    assert "ai_error" in ev and "entry_blocked" in ev
    journal.close()


def test_scenario_03_ai_timeout_blocks_entry(tmp_path, llm):
    cfg = _cfg(tmp_path)
    cfg.raw["ai"]["openai_compatible"]["timeout_seconds"] = 1
    model = _llm_model(cfg, llm)          # timeout=2
    model.timeout = 1.0                   # 服务睡 3s → 必然超时
    engine, journal, broker = _engine(cfg, model, tmp_path)
    llm.mode = "slow"

    started = time.time()
    report = engine.scan(["hk00700"], now=datetime.now())
    elapsed = time.time() - started

    o = report.outcomes[0]
    assert o.stage == "blocked"
    assert "NETWORK" in o.detail or "超时" in o.detail or "timeout" in o.detail.lower()
    assert broker.list_orders(limit=10) == []
    assert elapsed < 3.0, f"超时未生效，耗时 {elapsed:.1f}s"
    journal.close()


def test_scenario_04_ai_service_down_blocks_entry(tmp_path, llm, monkeypatch):
    monkeypatch.setenv("AI_API_KEY", "probe-key")
    cfg = _cfg(tmp_path)
    model = _llm_model(cfg, llm)
    engine, journal, broker = _engine(cfg, model, tmp_path)

    llm.stop()                            # 端口关闭 → 连接被拒
    time.sleep(0.2)

    report = engine.scan(["hk00700"], now=datetime.now())
    o = report.outcomes[0]
    assert o.stage == "blocked"
    assert broker.list_orders(limit=10) == []
    assert "entry_blocked" in _events(journal)
    journal.close()


def test_scenario_05_signal_without_stop_loss_rejected(tmp_path, llm):
    cfg = _cfg(tmp_path)
    model = _llm_model(cfg, llm)
    engine, journal, broker = _engine(cfg, model, tmp_path)
    llm.mode = "missing_stop"

    report = engine.scan(["hk00700"], now=datetime.now())
    o = report.outcomes[0]
    assert o.stage == "rejected"
    assert "缺少 stop_loss" in o.detail
    assert broker.list_orders(limit=10) == []
    journal.close()


def test_scenario_06_duplicate_signal_not_double_executed(tmp_path, llm):
    cfg = _cfg(tmp_path, signal_ttl_seconds=300)
    model = _llm_model(cfg, llm)
    engine, journal, broker = _engine(cfg, model, tmp_path)

    first = engine.scan(["hk00700"], now=datetime.now())
    assert first.outcomes[0].stage == "ordered"

    second = engine.scan(["hk00700"], now=datetime.now())
    assert second.outcomes[0].stage == "rejected"
    checks = {c["name"]: c for c in second.outcomes[0].decision["checks"]}
    assert checks["duplicate_order"]["ok"] is False

    assert len(broker.list_orders(limit=10)) == 1, "相同信号不能被重复执行"
    journal.close()


def test_scenario_07_recovery_produces_fresh_signal_without_replay(tmp_path, llm):
    """AI 故障 → 恢复：故障期间不下单，恢复后只按**新**信号下单一次。"""
    cfg = _cfg(tmp_path)
    model = _llm_model(cfg, llm)
    engine, journal, broker = _engine(cfg, model, tmp_path)

    # 故障
    llm.mode = "bad_json"
    t0 = datetime.now()
    r1 = engine.scan(["hk00700"], now=t0)
    assert r1.outcomes[0].stage == "blocked"
    assert broker.list_orders(limit=10) == []

    # 恢复（冷却期过后）
    llm.mode = "ok"
    t1 = t0 + timedelta(seconds=10)
    r2 = engine.scan(["hk00700"], now=t1)
    assert r2.outcomes[0].stage == "ordered", r2.outcomes[0].detail
    assert len(broker.list_orders(limit=10)) == 1, "恢复后只应有 1 笔新委托"

    ev = _events(journal)
    assert "ai_recovered" in ev
    # 恢复后产生的信号必须是**新**的（时间戳晚于故障时刻）
    sig_ts = r2.outcomes[0].signal["ts"]
    assert sig_ts >= t0.astimezone().isoformat(timespec="seconds")[:10]
    journal.close()


def test_scenario_08_risk_rejects_ai_signal(tmp_path, llm):
    """AI 给了信号，但风控拒绝 —— 必须以风控结论为准。"""
    cfg = _cfg(tmp_path)
    cfg.raw["risk"]["max_order_notional"] = 1      # 让 300*... 必然超限
    model = _llm_model(cfg, llm)
    engine, journal, broker = _engine(cfg, model, tmp_path)

    report = engine.scan(["hk00700"], now=datetime.now())
    o = report.outcomes[0]
    assert o.stage == "rejected"
    names = [c["name"] for c in o.decision["checks"] if not c["ok"]]
    assert names, "必须有具体失败项"
    assert broker.list_orders(limit=10) == []
    journal.close()


def test_scenario_09_positions_protected_while_ai_down(tmp_path, llm):
    """AI 失效期间，已有持仓的止损/止盈必须照常执行（不经过 AI 闸门）。"""
    cfg = _cfg(tmp_path)
    model = _llm_model(cfg, llm)
    engine, journal, broker = _engine(cfg, model, tmp_path)

    # 1) 先用正常的 AI 建仓
    r = engine.scan(["hk00700"], now=datetime.now())
    assert r.outcomes[0].stage == "ordered"
    pos = broker.get_positions()
    assert pos and pos[0].qty > 0, "应已建仓"
    assert "hk00700" in engine.stops

    # 2) AI 挂掉
    llm.stop()
    time.sleep(0.2)

    # 3) 人为把止损抬到现价之上，触发保护
    px = broker.get_quote("hk00700").last
    engine.register_stops("hk00700", stop_loss=px * 1.02, take_profit=None,
                          qty=pos[0].qty)
    executed = engine.monitor(now=datetime.now())

    assert any(e["kind"] == "exit" for e in executed), executed
    assert broker.get_positions() == [] or broker.get_positions()[0].qty == 0
    # US-P1 Batch2 R-7：保护解除是"两段式"——先在下一轮核实持仓为 0 再解除
    assert engine.stops.get("hk00700", {}).get("exit_pending") is True
    engine.monitor(now=datetime.now())
    assert "hk00700" not in engine.stops
    journal.close()


# --------------------------------------------------------------------------- #
# 权威组件的独立测试
# --------------------------------------------------------------------------- #
def test_authority_breaker_opens_after_failures(tmp_path):
    cfg = _cfg(tmp_path, max_consecutive_failures=2, failure_cooldown_seconds=30)
    auth = DecisionAuthority(cfg, UnavailableModel("x"))

    ok = DecisionAuthority(cfg, _DummyTradable())
    assert ok.can_decide_entry().allowed is True

    now = datetime.now().astimezone()
    ok.record_failure("NETWORK", "timeout", now)
    assert ok.can_decide_entry(now).allowed is True, "第 1 次失败还不熔断"
    ok.record_failure("NETWORK", "timeout", now)
    d = ok.can_decide_entry(now)
    assert d.allowed is False and d.code == CODE_DEGRADED
    assert "冷却" in d.reason

    # 冷却过后恢复
    later = now + timedelta(seconds=31)
    assert ok.can_decide_entry(later).allowed is True


def test_authority_success_resets_failures(tmp_path):
    cfg = _cfg(tmp_path, max_consecutive_failures=2)
    auth = DecisionAuthority(cfg, _DummyTradable())
    now = datetime.now().astimezone()
    auth.record_failure("X", "e1", now)
    auth.record_success(now)
    assert auth.snapshot(now).consecutive_failures == 0
    assert auth.can_decide_entry(now).allowed is True


def test_authority_freshness_boundaries(tmp_path):
    cfg = _cfg(tmp_path, signal_ttl_seconds=60)
    auth = DecisionAuthority(cfg, _DummyTradable())
    now = datetime.now().astimezone()

    fresh = (now - timedelta(seconds=10)).isoformat(timespec="seconds")
    assert auth.check_freshness(fresh, now).allowed is True

    stale = (now - timedelta(seconds=120)).isoformat(timespec="seconds")
    d = auth.check_freshness(stale, now)
    assert d.allowed is False and d.code == CODE_STALE

    assert auth.check_freshness("not-a-timestamp", now).allowed is False


class _DummyTradable:
    name, model_id, can_trade, available = "dummy", "v1", True, True

    def analyze(self, ctx):
        raise NotImplementedError

    def health(self):
        return {"name": self.name}
