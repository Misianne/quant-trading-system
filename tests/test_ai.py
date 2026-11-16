"""测试 2：AI 模型接口与调用。

无密钥时，LLM 调用测试必须 **skip 并明确标记未测试**，不得伪造成功。
"""
from __future__ import annotations

import os

import pytest

from ai_trader.ai.base import AIModelError, MarketContext, TradingSignal
from ai_trader.ai.providers import (
    HeuristicModel,
    OpenAICompatibleModel,
    build_model,
    extract_json,
)
from tests.conftest import make_candles


def _ctx(closes, symbol="hk00700"):
    return MarketContext(symbol=symbol, market="hk", currency="HKD",
                         last_price=closes[-1], candles=make_candles(closes),
                         equity=1_000_000.0, cash=1_000_000.0, risk_budget=10_000.0)


# ---------------------------------------------------------------- 规则引擎
UPTREND_PAUSE = ([100 + i * 1.0 for i in range(106)]
                 + [206 + 0.6 * ((-1) ** i) for i in range(14)])


def test_heuristic_model_returns_valid_buy_on_uptrend():
    """上升趋势 + 高位横盘（未超买、贴 MA20）应给出合法买入信号。"""
    sig = HeuristicModel().analyze(_ctx(UPTREND_PAUSE))
    assert sig.action == "buy"
    assert sig.stop_loss < sig.entry_price < sig.take_profit
    assert 0 < sig.position_pct <= 0.15
    assert sig.validate() == [], sig.errors


def test_heuristic_model_holds_on_overbought_straight_rally():
    """单边直线上涨（RSI=100 且远离均线）应主动放弃 —— 不追高。"""
    closes = [100 + i * 0.9 for i in range(120)]
    sig = HeuristicModel().analyze(_ctx(closes))
    assert sig.action == "hold"
    assert "超买" in sig.rationale


def test_heuristic_model_holds_when_history_too_short():
    sig = HeuristicModel().analyze(_ctx([100.0] * 10))
    assert sig.action == "hold"
    assert "K 线数量不足" in sig.rationale


def test_heuristic_model_holds_on_flat_market():
    closes = [100.0 + (0.01 if i % 2 else -0.01) for i in range(150)]
    sig = HeuristicModel().analyze(_ctx(closes))
    assert sig.action == "hold"


# ---------------------------------------------------------------- 信号校验
@pytest.mark.parametrize("signal,expect", [
    (TradingSignal(symbol="hk00700", action="buy", confidence=0.9, entry_price=100.0,
                   stop_loss=None, take_profit=110.0), "缺少 stop_loss"),
    (TradingSignal(symbol="hk00700", action="buy", confidence=0.9, entry_price=100.0,
                   stop_loss=105.0, take_profit=110.0), "stop_loss 必须低于 entry_price"),
    (TradingSignal(symbol="hk00700", action="buy", confidence=0.9, entry_price=100.0,
                   stop_loss=95.0, take_profit=90.0), "take_profit 必须高于 entry_price"),
    (TradingSignal(symbol="hk00700", action="short", confidence=0.9), "非法 action"),
    (TradingSignal(symbol="hk00700", action="buy", confidence=0.2, entry_price=100.0,
                   stop_loss=95.0, take_profit=110.0), "低于阈值"),
])
def test_signal_validation_catches_illegal_output(signal, expect):
    errors = signal.validate()
    assert any(expect in e for e in errors), errors
    assert signal.valid is False


def test_signal_validation_accepts_well_formed_buy():
    sig = TradingSignal(symbol="hk00700", action="buy", confidence=0.8, entry_price=100.0,
                        stop_loss=95.0, take_profit=112.0, position_pct=0.1)
    assert sig.validate() == []
    assert sig.valid


# ---------------------------------------------------------------- JSON 解析
@pytest.mark.parametrize("raw", [
    '{"action":"buy","confidence":0.8,"entry_price":100,"stop_loss":95,"take_profit":110}',
    '```json\n{"action":"buy","confidence":0.8}\n```',
    '好的，我的判断是：{"action":"hold","confidence":0.0} 以上。',
])
def test_extract_json_tolerates_fences_and_prose(raw):
    obj = extract_json(raw)
    assert "action" in obj


def test_extract_json_rejects_non_json():
    with pytest.raises(AIModelError):
        extract_json("我认为应该买入")


# ---------------------------------------------------------------- LLM 未配置
def test_llm_without_key_is_marked_unavailable_and_untested(cfg):
    model = build_model(cfg, force_provider="openai_compatible")
    assert isinstance(model, OpenAICompatibleModel)
    assert model.available is False
    assert model.last_status == "untested_no_api_key"
    health = model.health()
    assert health["status"] == "untested_no_api_key"
    assert health["api_key_present"] is False
    with pytest.raises(AIModelError) as ei:
        model.analyze(_ctx([100 + i for i in range(60)]))
    assert ei.value.code == "NO_API_KEY"


def test_build_model_provider_dispatch(cfg):
    assert isinstance(build_model(cfg, force_provider="heuristic"), HeuristicModel)
    assert isinstance(build_model(cfg, force_provider="openai_compatible"), OpenAICompatibleModel)
    with pytest.raises(ValueError):
        build_model(cfg, force_provider="no-such-provider")


# ---------------------------------------------------------------- 真实调用
@pytest.mark.network
def test_llm_real_call_if_key_present(cfg, monkeypatch):
    key = os.getenv("AI_API_KEY", "")
    if not key:
        pytest.skip("未配置 AI_API_KEY —— LLM 真实调用【未测试】")
    monkeypatch.setenv("AI_API_KEY", key)
    model = build_model(cfg, force_provider="openai_compatible")
    sig = model.analyze(_ctx([100 + i * 0.8 for i in range(80)]))
    assert sig.action in ("buy", "sell", "hold")
    assert model.health()["status"] == "ok"
