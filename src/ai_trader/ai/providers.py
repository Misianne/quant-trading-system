"""模型实现：规则引擎（无密钥可用）+ OpenAI 兼容 LLM（DeepSeek 等）。"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, List, Optional

from ..strategy.indicators import compute_snapshot
from .base import AIModel, AIModelError, MarketContext, TradingSignal

# --------------------------------------------------------------------------- #
# 提示词
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT = """你是一名严格的量化交易分析员。只输出 JSON，不要输出任何解释文字、markdown 或代码块。

你必须基于给定的行情指标做判断，遵守：
1. 只在有明确统计优势时给出 buy/sell，否则给 hold。
2. 每个 buy/sell 必须给出 entry_price、stop_loss、take_profit，且方向必须自洽：
   buy: stop_loss < entry_price < take_profit
   sell: take_profit < entry_price < stop_loss
3. stop_loss 应基于波动率（ATR）而非随意数字，距离不应超过 12%。
4. position_pct 表示建议使用的权益比例，0~0.15 之间。
5. confidence 是你对该判断的把握（0~1），不要虚高。
6. 不得建议做空（系统未开通）、不得建议融资。

输出 JSON 结构：
{"action":"buy|sell|hold","confidence":0.0,"entry_price":0.0,"stop_loss":0.0,
 "take_profit":0.0,"position_pct":0.0,"horizon":"intraday|swing|position",
 "rationale":"不超过 120 字的中文理由"}
"""


def build_user_prompt(ctx: MarketContext) -> str:
    payload = ctx.to_prompt_dict()
    return (
        "请分析以下标的并给出交易决策。\n"
        f"```json\n{json.dumps(payload, ensure_ascii=False, default=str)}\n```\n"
        "记住：只输出一个 JSON 对象。"
    )


# --------------------------------------------------------------------------- #
# 规则引擎（保证无 API Key 时系统仍能端到端跑通）
# --------------------------------------------------------------------------- #
class HeuristicModel(AIModel):
    """确定性的技术分析模型。

    它**不是**大语言模型，而是可审计的规则引擎：均线趋势 + RSI + 动量 + ATR 定仓。
    作用是在没有 AI 密钥时仍能验证"信号→风控→下单"链路，并作为 LLM 的对照基线。
    """

    name = "heuristic"
    model_id = "heuristic-trend-v1"
    is_llm = False
    #: 规则引擎没有开仓决策权（审计 P1-9）：它只提供指标与辅助判断。
    #: 引擎在 ``require_ai_for_entry=true``（默认）时会拒绝它的开仓信号。
    can_trade = False

    def __init__(self, min_confidence: float = 0.60, max_position_pct: float = 0.15,
                 trend_fast: int = 20, trend_slow: int = 60):
        self.min_confidence = min_confidence
        self.max_position_pct = max_position_pct
        self.trend_fast = trend_fast
        self.trend_slow = trend_slow

    def analyze(self, ctx: MarketContext) -> TradingSignal:
        candles = ctx.candles or []
        if len(candles) < 30:
            return TradingSignal.hold(
                ctx.symbol, f"K 线数量不足（{len(candles)} < 30），跳过",
                self.name, self.model_id, ctx.market,
            )

        snap = compute_snapshot(candles)
        closes = [c.close for c in candles]
        last = float(ctx.last_price or closes[-1])
        sma_f, sma_s = snap.get("sma20"), snap.get("sma60")
        rsi = snap.get("rsi14")
        mom = snap.get("mom20")
        atr = snap.get("atr14") or (last * 0.02)
        atr_p = snap.get("atr_pct") or 0.02

        pos_qty = float((ctx.position or {}).get("qty", 0) or 0)

        reasons: List[str] = []
        score = 0.0

        # 1) 趋势
        if sma_f and sma_s:
            if sma_f > sma_s:
                score += 0.30
                reasons.append(f"MA{self.trend_fast}({sma_f:.2f}) > MA{self.trend_slow}({sma_s:.2f}) 多头排列")
            else:
                score -= 0.30
                reasons.append(f"MA{self.trend_fast}({sma_f:.2f}) < MA{self.trend_slow}({sma_s:.2f}) 空头排列")

        # 2) 价格与均线位置
        if sma_f:
            dev = (last - sma_f) / sma_f
            if -0.02 <= dev <= 0.03 and (sma_f or 0) > (sma_s or 0):
                score += 0.20
                reasons.append(f"价格贴近 MA{self.trend_fast}（偏离 {dev*100:+.2f}%），回踩买点")
            elif dev > 0.08:
                score -= 0.15
                reasons.append(f"价格偏离 MA{self.trend_fast} 达 {dev*100:+.2f}%，追高风险")

        # 3) RSI
        if rsi is not None:
            if rsi < 35:
                score += 0.15
                reasons.append(f"RSI {rsi:.1f} 超卖")
            elif rsi > 72:
                score -= 0.25
                reasons.append(f"RSI {rsi:.1f} 超买")
            else:
                reasons.append(f"RSI {rsi:.1f} 中性")

        # 4) 动量
        if mom is not None:
            if mom > 0.03:
                score += 0.15
                reasons.append(f"20 日动量 {mom*100:+.2f}%")
            elif mom < -0.03:
                score -= 0.15
                reasons.append(f"20 日动量 {mom*100:+.2f}%")

        # 5) 波动率过滤：太死或太疯都不做
        if atr_p < 0.005:
            return TradingSignal.hold(
                ctx.symbol, f"ATR {atr_p*100:.2f}% 过低，缺乏交易价值",
                self.name, self.model_id, ctx.market,
            )
        if atr_p > 0.09:
            return TradingSignal.hold(
                ctx.symbol, f"ATR {atr_p*100:.2f}% 过高，波动失控风险",
                self.name, self.model_id, ctx.market,
            )

        confidence = min(0.95, max(0.0, 0.5 + score))
        action = "hold"
        if score >= 0.35 and pos_qty == 0:
            action = "buy"
        elif score <= -0.25 and pos_qty > 0:
            action = "sell"

        if action == "hold":
            if pos_qty > 0 and score <= -0.15:
                reasons.append("持仓中出现转弱，但未达止损条件，继续观察")
            return TradingSignal(
                symbol=ctx.symbol, action="hold", confidence=round(confidence, 3),
                rationale="；".join(reasons)[:400],
                market=ctx.market, provider=self.name, model=self.model_id,
                valid=confidence >= self.min_confidence or True,
            )

        stop_dist = max(2.0 * atr, last * 0.015)
        if action == "buy":
            entry = last
            stop = round(max(0.01, entry - stop_dist), 4)
            target = round(entry + 3.0 * atr, 4)
            position_pct = min(
                self.max_position_pct,
                max(0.02, round(confidence * self.max_position_pct, 4)),
            )
            # 用风险预算反推上限：单笔最大亏损 = 权益 * risk_budget%
            if ctx.risk_budget and ctx.equity:
                risk_per_share = entry - stop
                max_qty_by_risk = ctx.risk_budget / risk_per_share if risk_per_share > 0 else 0
                max_pct_by_risk = (max_qty_by_risk * entry) / ctx.equity if ctx.equity else 0
                position_pct = min(position_pct, max(0.0, round(max_pct_by_risk, 4)))
        else:
            entry = last
            stop = round(entry + stop_dist, 4)
            target = round(max(0.01, entry - 3.0 * atr), 4)
            position_pct = min(1.0, max(0.0, pos_qty and 1.0 or 0.0))

        sig = TradingSignal(
            symbol=ctx.symbol, action=action, confidence=round(confidence, 3),
            entry_price=entry, stop_loss=stop, take_profit=target,
            position_pct=position_pct,
            horizon="swing", rationale="；".join(reasons)[:400],
            market=ctx.market, provider=self.name, model=self.model_id,
        )
        sig.validate(min_confidence=self.min_confidence)
        return sig

    def health(self) -> Dict[str, Any]:
        h = super().health()
        h.update({"status": "ready", "note": "规则引擎，非 LLM；确定性可复现"})
        return h


# --------------------------------------------------------------------------- #
# OpenAI 兼容 LLM
# --------------------------------------------------------------------------- #
_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def extract_json(text: str) -> Dict[str, Any]:
    """从模型输出里抠出第一个 JSON 对象。"""
    if not text:
        raise AIModelError("模型返回空内容", code="EMPTY_RESPONSE")
    text = text.strip()
    m = _JSON_BLOCK.search(text)
    if m:
        text = m.group(1)
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise AIModelError(f"模型输出不含 JSON: {text[:200]}", code="NO_JSON")
        text = text[start:end + 1]
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AIModelError(f"JSON 解析失败: {exc}", code="BAD_JSON",
                           detail={"snippet": text[:300]}) from exc
    if not isinstance(obj, dict):
        raise AIModelError("模型输出的 JSON 不是对象", code="BAD_SHAPE")
    return obj


class OpenAICompatibleModel(AIModel):
    """任意 OpenAI Chat Completions 兼容端点（DeepSeek / OpenAI / 自建网关）。"""

    name = "openai_compatible"
    is_llm = True

    def __init__(self, base_url: str, model: str, api_key: str,
                 temperature: float = 0.2, max_tokens: int = 1200,
                 timeout: int = 60, min_confidence: float = 0.60, max_position_pct: float = 0.15,
                 connect_timeout: float = 10.0):
        self.base_url = (base_url or "").rstrip("/")
        self.model_id = model or "unknown"
        self.api_key = api_key or ""
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = float(timeout) if timeout and timeout > 0 else 60.0
        self.connect_timeout = float(connect_timeout) if connect_timeout and connect_timeout > 0 else 10.0
        self.min_confidence = min_confidence
        self.max_position_pct = max_position_pct
        self.available = bool(self.api_key and self.base_url)
        self.last_status = "untested" if self.available else "untested_no_api_key"
        self.last_error = ""
        self.last_latency_ms: Optional[float] = None
        self.calls = 0
        self.failures = 0
        self.last_usage: Dict[str, Any] = {}

    def _post(self, messages: List[Dict[str, str]]) -> Dict[str, Any]:
        import requests

        url = f"{self.base_url}/chat/completions"
        body = {
            "model": self.model_id,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        # 超时必须成对设置：只给 read 不管 connect，DNS/TCP 阶段仍会无限挂起
        connect_to = getattr(self, "connect_timeout", None) or min(10.0, float(self.timeout))
        read_to = float(self.timeout) if self.timeout and self.timeout > 0 else 60.0
        started = time.time()
        try:
            resp = requests.post(url, json=body, headers=headers,
                                 timeout=(connect_to, read_to))
        except Exception as exc:  # noqa: BLE001
            self.last_latency_ms = (time.time() - started) * 1000
            raise AIModelError(f"请求失败: {type(exc).__name__}: {exc}",
                               code="NETWORK") from exc
        self.last_latency_ms = round((time.time() - started) * 1000, 1)
        if resp.status_code >= 400:
            snippet = resp.text[:300]
            raise AIModelError(
                f"HTTP {resp.status_code}: {snippet}",
                code=f"HTTP_{resp.status_code}", detail={"status": resp.status_code},
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise AIModelError("响应不是合法 JSON", code="BAD_RESPONSE") from exc

    def analyze(self, ctx: MarketContext) -> TradingSignal:
        if not self.available:
            raise AIModelError(
                "未配置 AI_API_KEY，模型不可用（状态：未测试）", code="NO_API_KEY"
            )
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(ctx)},
        ]
        self.calls += 1
        try:
            data = self._post(messages)
            choices = data.get("choices") or []
            if not choices:
                raise AIModelError("响应中无 choices", code="BAD_RESPONSE",
                                   detail={"keys": list(data.keys())})
            content = (choices[0].get("message") or {}).get("content") or ""
            self.last_usage = data.get("usage") or {}
            obj = extract_json(content)
        except AIModelError as exc:
            self.failures += 1
            self.last_status = "error"
            self.last_error = str(exc)
            raise

        self.last_status = "ok"
        self.last_error = ""
        sig = TradingSignal(
            symbol=ctx.symbol,
            action=obj.get("action", "hold"),
            confidence=obj.get("confidence", 0.0),
            entry_price=obj.get("entry_price"),
            stop_loss=obj.get("stop_loss"),
            take_profit=obj.get("take_profit"),
            position_pct=obj.get("position_pct", 0.0) or 0.0,
            horizon=str(obj.get("horizon", "swing")),
            rationale=str(obj.get("rationale", ""))[:500],
            market=ctx.market, provider=self.name, model=self.model_id,
            raw=obj,
        )
        # 硬约束：模型无权决定超过配置上限的仓位
        if sig.position_pct and sig.position_pct > self.max_position_pct:
            sig.rationale += f"（仓位建议 {sig.position_pct:.0%} 被截断至 {self.max_position_pct:.0%}）"
            sig.position_pct = self.max_position_pct
        sig.validate(min_confidence=self.min_confidence)
        return sig

    def health(self) -> Dict[str, Any]:
        first = ""
        if self.available:
            first = f"{self.base_url}/chat/completions"
        return {
            "name": self.name,
            "model_id": self.model_id,
            "is_llm": True,
            "available": self.available,
            "status": self.last_status,
            "endpoint": first,
            "calls": self.calls,
            "failures": self.failures,
            "last_latency_ms": self.last_latency_ms,
            "last_error": self.last_error,
            "last_usage": self.last_usage,
            "api_key_present": bool(self.api_key),
        }


# --------------------------------------------------------------------------- #
# 工厂
# --------------------------------------------------------------------------- #
def build_model(cfg, force_provider: Optional[str] = None) -> AIModel:
    """按配置构建模型。provider 不可用时**不静默替换**，由调用方决定是否降级。"""
    ai_cfg = cfg.ai or {}
    provider = (force_provider or ai_cfg.get("provider") or "heuristic").strip().lower()
    constraints = ai_cfg.get("constraints") or {}
    min_conf = float(constraints.get("min_confidence", 0.60))
    max_pos = float(constraints.get("max_position_pct", 0.15))

    if provider in ("heuristic", "rules", "rule"):
        trend = (cfg.get("strategy.trend_filter") or {})
        return HeuristicModel(
            min_confidence=min_conf, max_position_pct=max_pos,
            trend_fast=int(trend.get("fast_ma", 20)), trend_slow=int(trend.get("slow_ma", 60)),
        )

    if provider in ("openai_compatible", "openai", "llm", "deepseek"):
        sub = ai_cfg.get("openai_compatible") or {}
        key_env = str(sub.get("api_key_env") or "AI_API_KEY")
        api_key = os.getenv(key_env, "") or os.getenv("AI_API_KEY", "")
        base_url = os.getenv("AI_BASE_URL") or str(sub.get("base_url") or "")
        model_name = os.getenv("AI_MODEL") or str(sub.get("model") or "")
        return OpenAICompatibleModel(
            base_url=base_url, model=model_name, api_key=api_key,
            temperature=float(sub.get("temperature", 0.2)),
            max_tokens=int(sub.get("max_tokens", 1200)),
            timeout=int(sub.get("timeout_seconds", 60)),
            min_confidence=min_conf, max_position_pct=max_pos,
        )

    raise ValueError(f"未知的 AI provider: {provider}")
