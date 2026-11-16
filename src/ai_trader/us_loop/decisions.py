"""AI 决策层：真实模型调用 + 结构化决策记录（可审计、可复现）。

与既有代码的关系
----------------
* **不重写**模型调用：直接复用 ``ai/providers.py`` 的 ``OpenAICompatibleModel``
  （真实 HTTP POST 到 ``/chat/completions``）与 ``UnavailableModel``。
* **不降级**：没有 API Key 就返回 ``UnavailableModel``，决策记为
  ``action=blocked / provenance=unavailable``，绝不返回规则引擎的结论冒充 AI。
* **补齐审计字段**：既有 ``TradingSignal`` 已有 signal_id / ts / provider / model /
  confidence / rationale，但缺 ``input_version``（输入数据版本）、``risk_conditions``、
  ``latency_ms``、``prompt_digest`` 与 ``provenance``。这些在本模块补齐并落库。
* **不改引擎**：用 :class:`AuditedModel` 包一层，引擎照旧调 ``model.analyze(ctx)``，
  包一层即可在每次调用后落一条决策记录。

落库位置
--------
独立的 ``data/us_loop.db``，不改动 ``data/audit.db`` 的结构；订单/风控/权益仍由
既有 ``AuditJournal`` 记录，两边用 ``decision_id`` 关联。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..ai.base import AIModel, AIModelError, MarketContext, TradingSignal, UnavailableModel
from ..ai.providers import OpenAICompatibleModel

#: 输入版本标签：任何影响提示词内容的代码/字段变化都必须改这个值
PROMPT_SCHEMA_VERSION = "us-loop-ctx-v1"

#: 决策来源标记（**不可由数据库字段单独制造**，必须与端点类别一致）
#:
#: * ``real_llm``       —— 远端真实 Provider，HTTP 请求真的发出去过
#: * ``stub_transport`` —— 本机回环端点上的协议替身（测试用，**不是 AI 推理**）
#: * ``unavailable``    —— 没有可用模型，未发出任何请求
#: * ``rejected``       —— 请求发出但失败（超时/格式/拒答），决策被拒
PROVENANCE_REAL_LLM = "real_llm"
PROVENANCE_STUB = "stub_transport"
PROVENANCE_UNAVAILABLE = "unavailable"
PROVENANCE_REJECTED = "rejected"
#: 兼容旧字段值（历史行里出现过 "llm"，读取侧需要能识别）
PROVENANCE_LLM = "llm"

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def classify_endpoint(base_url: str) -> str:
    """端点类别：``loopback`` / ``remote`` / ``unknown``（只看主机名，不泄漏内容）。"""
    if not base_url:
        return "unknown"
    try:
        from urllib.parse import urlparse
        host = (urlparse(base_url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return "unknown"
    if not host:
        return "unknown"
    return "loopback" if host in _LOOPBACK_HOSTS else "remote"


def classify_provenance(model: Any, mode: str = "") -> str:
    """按"端点类别 + 运行模式"判定来源标记。

    规则（从严）：
    1. ``mode=stub-transport`` → ``stub_transport``（无论端点是什么）；
    2. 端点是回环地址 → ``stub_transport``（**本机替身永远不能成为 real_llm**）；
    3. 端点是远端且模型可用 → ``real_llm``；
    4. 其余 → ``unavailable``。
    """
    m = str(mode or "").strip().lower()
    if m in ("stub-transport", "stub_transport"):
        return PROVENANCE_STUB
    if not bool(getattr(model, "available", False)):
        return PROVENANCE_UNAVAILABLE
    ep = classify_endpoint(str(getattr(model, "base_url", "") or ""))
    if ep == "loopback":
        return PROVENANCE_STUB
    if ep == "remote":
        return PROVENANCE_REAL_LLM
    return PROVENANCE_UNAVAILABLE

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_decisions (
    decision_id     TEXT PRIMARY KEY,
    run_id          TEXT,
    signal_id       TEXT,
    symbol          TEXT NOT NULL,
    decided_at      TEXT NOT NULL,
    action          TEXT NOT NULL,
    confidence      REAL,
    target_qty      REAL,
    target_pct      REAL,
    entry_price     REAL,
    stop_loss       REAL,
    take_profit     REAL,
    rationale       TEXT,
    risk_conditions TEXT,
    input_version   TEXT,
    provider        TEXT,
    model_name      TEXT,
    model_id        TEXT,
    latency_ms      REAL,
    prompt_digest   TEXT,
    response_digest TEXT,
    usage           TEXT,
    provenance      TEXT NOT NULL,
    endpoint_class  TEXT,
    errors          TEXT,
    raw             TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_decisions_run ON ai_decisions(run_id);
CREATE INDEX IF NOT EXISTS idx_ai_decisions_symbol ON ai_decisions(symbol, decided_at);

CREATE TABLE IF NOT EXISTS loop_runs (
    run_id      TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    mode        TEXT,
    as_of       TEXT,
    provider    TEXT,
    model       TEXT,
    status      TEXT,
    summary     TEXT,
    notes       TEXT
);
"""


@dataclass
class AIDecision:
    """一次决策的完整记录（含可复现所需的一切）。"""

    decision_id: str
    symbol: str
    decided_at: str
    action: str                       # buy / sell / hold / blocked
    confidence: float = 0.0
    target_qty: float = 0.0
    target_pct: float = 0.0
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    rationale: str = ""
    risk_conditions: List[str] = field(default_factory=list)
    input_version: str = ""
    provider: str = ""
    model_name: str = ""
    model_id: str = ""
    latency_ms: float = 0.0
    prompt_digest: str = ""
    response_digest: str = ""
    usage: Dict[str, Any] = field(default_factory=dict)
    provenance: str = PROVENANCE_UNAVAILABLE
    endpoint_class: str = ""
    errors: List[str] = field(default_factory=list)
    signal_id: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)
    run_id: str = ""

    @classmethod
    def new(cls, symbol: str, decided_at: str, action: str, **kw: Any) -> "AIDecision":
        return cls(decision_id=kw.pop("decision_id", "") or f"d{uuid.uuid4().hex[:20]}",
                   symbol=symbol, decided_at=decided_at, action=action, **kw)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "decision_id": self.decision_id, "run_id": self.run_id,
            "signal_id": self.signal_id, "symbol": self.symbol,
            "decided_at": self.decided_at, "action": self.action,
            "confidence": self.confidence, "target_qty": self.target_qty,
            "target_pct": self.target_pct, "entry_price": self.entry_price,
            "stop_loss": self.stop_loss, "take_profit": self.take_profit,
            "rationale": self.rationale, "risk_conditions": list(self.risk_conditions),
            "input_version": self.input_version, "provider": self.provider,
            "model_name": self.model_name, "model_id": self.model_id,
            "latency_ms": self.latency_ms, "prompt_digest": self.prompt_digest,
            "response_digest": self.response_digest, "usage": dict(self.usage),
            "provenance": self.provenance, "endpoint_class": self.endpoint_class,
            "errors": list(self.errors),
            "raw": dict(self.raw),
        }


def canonical_digest(obj: Any) -> str:
    """稳定的输入/输出摘要（排序键、紧凑分隔符、不落原文）。"""
    raw = json.dumps(obj, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_input_payload(ctx: MarketContext, *, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """构造"喂给模型"的规范化载荷（用于计算 input_version 与回放）。"""
    payload = {
        "schema": PROMPT_SCHEMA_VERSION,
        "market_context": ctx.to_prompt_dict(),
        "bars": [
            {"ts": getattr(c, "ts", ""), "o": getattr(c, "open", None),
             "h": getattr(c, "high", None), "l": getattr(c, "low", None),
             "c": getattr(c, "close", None), "v": getattr(c, "volume", None)}
            for c in (ctx.candles or [])
        ],
        "notes": list(ctx.notes or []),
    }
    if extra:
        payload["extra"] = extra
    return payload


class DecisionLedger:
    """``data/us_loop.db`` 的读写封装（AI 决策 + 运行记录）。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            # 老库补列（只加列，不动已有数据）
            try:
                cols = {r[1] for r in self._conn.execute("PRAGMA table_info(ai_decisions)")}
                if "endpoint_class" not in cols:
                    self._conn.execute("ALTER TABLE ai_decisions ADD COLUMN endpoint_class TEXT")
            except sqlite3.Error:
                pass
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:
                pass
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    def record_decision(self, d: AIDecision) -> str:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO ai_decisions(decision_id,run_id,signal_id,symbol,"
                "decided_at,action,confidence,target_qty,target_pct,entry_price,stop_loss,"
                "take_profit,rationale,risk_conditions,input_version,provider,model_name,"
                "model_id,latency_ms,prompt_digest,response_digest,usage,provenance,"
                "endpoint_class,errors,raw,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (d.decision_id, d.run_id, d.signal_id, d.symbol, d.decided_at, d.action,
                 d.confidence, d.target_qty, d.target_pct, d.entry_price, d.stop_loss,
                 d.take_profit, d.rationale, json.dumps(d.risk_conditions, ensure_ascii=False),
                 d.input_version, d.provider, d.model_name, d.model_id, d.latency_ms,
                 d.prompt_digest, d.response_digest, json.dumps(d.usage, ensure_ascii=False),
                 d.provenance, d.endpoint_class,
                 json.dumps(d.errors, ensure_ascii=False),
                 json.dumps(d.raw, ensure_ascii=False, default=str),
                 datetime.now().astimezone().isoformat(timespec="seconds")))
            self._conn.commit()
        return d.decision_id

    def list_decisions(self, limit: int = 100, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM ai_decisions"
        args: List[Any] = []
        if symbol:
            sql += " WHERE symbol=?"
            args.append(symbol)
        sql += " ORDER BY rowid DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def list_runs(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(
                "SELECT * FROM loop_runs ORDER BY rowid DESC LIMIT ?", (int(limit),)).fetchall()]

    def start_run(self, run_id: str, mode: str, as_of: str, provider: str,
                  model: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO loop_runs(run_id,started_at,mode,as_of,provider,"
                "model,status) VALUES(?,?,?,?,?,?,?)",
                (run_id, datetime.now().astimezone().isoformat(timespec="seconds"),
                 mode, as_of, provider, model, "running"))
            self._conn.commit()

    def finish_run(self, run_id: str, status: str, summary: Dict[str, Any],
                   notes: Optional[List[str]] = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE loop_runs SET finished_at=?,status=?,summary=?,notes=? WHERE run_id=?",
                (datetime.now().astimezone().isoformat(timespec="seconds"), status,
                 json.dumps(summary, ensure_ascii=False, default=str),
                 json.dumps(notes or [], ensure_ascii=False), run_id))
            self._conn.commit()

    def counts(self) -> Dict[str, int]:
        with self._lock:
            return {
                "ai_decisions": int(self._conn.execute(
                    "SELECT COUNT(*) FROM ai_decisions").fetchone()[0]),
                "loop_runs": int(self._conn.execute(
                    "SELECT COUNT(*) FROM loop_runs").fetchone()[0]),
            }


def build_llm(cfg: Any, override: Optional[Dict[str, str]] = None) -> Tuple[AIModel, str]:
    """按配置构造**真实** LLM；缺 Key 返回 ``UnavailableModel``（不降级）。

    与 ``runtime.build_ai`` 的区别：这里**不**返回规则引擎 —— 本专项禁止用规则
    引擎冒充 AI 决策。

    ``override`` 仅供测试把端点指向本地传输层替身（``base_url``/``api_key``/
    ``model``）。走 override 时调用方**必须**在运行记录里标注
    ``mode=stub-transport``，不得把结果当成 AI 推理。
    """
    ai_cfg = getattr(cfg, "ai", None) or {}
    sub = ai_cfg.get("openai_compatible") or {}
    key_env = str(sub.get("api_key_env") or "AI_API_KEY")
    ov = dict(override or {})
    api_key = ov.get("api_key") or os.getenv(key_env, "") or os.getenv("AI_API_KEY", "")
    base_url = ov.get("base_url") or os.getenv("AI_BASE_URL") or str(sub.get("base_url") or "")
    model_name = ov.get("model") or os.getenv("AI_MODEL") or str(sub.get("model") or "")
    constraints = ai_cfg.get("constraints") or {}
    if not api_key:
        reason = (f"AI 凭据缺失：环境变量 {key_env} 为空（vault 中亦无该键），"
                  f"endpoint={base_url or '未配置'} model={model_name or '未配置'}")
        return UnavailableModel(reason, provider="openai_compatible"), reason
    if not base_url or not model_name:
        reason = f"AI 端点/模型未配置：base_url={base_url or '空'} model={model_name or '空'}"
        return UnavailableModel(reason, provider="openai_compatible"), reason
    model = OpenAICompatibleModel(
        base_url=base_url, model=model_name, api_key=api_key,
        temperature=float(sub.get("temperature", 0.2)),
        max_tokens=int(sub.get("max_tokens", 1200)),
        timeout=int(sub.get("timeout_seconds", 60)),
        min_confidence=float(constraints.get("min_confidence", 0.60)),
        max_position_pct=float(constraints.get("max_position_pct", 0.15)),
    )
    if not model.available:
        return UnavailableModel("LLM 构造后仍不可用",
                                provider="openai_compatible"), "LLM 构造后仍不可用"
    return model, ""


class AIGateway:
    """把一次模型调用变成一条 :class:`AIDecision`。"""

    def __init__(self, model: AIModel, ledger: DecisionLedger,
                 run_id: str = "", lot_size: int = 1,
                 provenance: Optional[str] = None, mode: str = ""):
        self.model = model
        self.ledger = ledger
        self.run_id = run_id
        self.lot_size = max(1, int(lot_size))
        #: 来源标记由"端点类别 + 运行模式"推出；外部即使强行指定 real_llm，
        #: 只要端点不是远端就会被降级 —— 堵死"只改字段伪造真实调用"的路径。
        self.endpoint_class = classify_endpoint(str(getattr(model, "base_url", "") or ""))
        prov = str(provenance or classify_provenance(model, mode))
        if prov == PROVENANCE_REAL_LLM and self.endpoint_class != "remote":
            prov = (PROVENANCE_STUB if self.endpoint_class == "loopback"
                    else PROVENANCE_UNAVAILABLE)
        self.provenance = prov

    # ------------------------------------------------------------------ #
    def available(self) -> bool:
        return bool(getattr(self.model, "available", True))

    def _base(self, ctx: MarketContext, extra_input: Optional[Dict[str, Any]] = None
              ) -> Dict[str, Any]:
        payload = build_input_payload(ctx, extra=extra_input)
        input_version = canonical_digest(payload)
        prompt_digest = canonical_digest({"provider": getattr(self.model, "name", ""),
                                          "model": getattr(self.model, "model_id", ""),
                                          "payload": payload})
        return dict(
            provider=str(getattr(self.model, "name", "")),
            model_name=str(getattr(self.model, "name", "")),
            model_id=str(getattr(self.model, "model_id", "")),
            input_version=input_version, prompt_digest=prompt_digest,
            run_id=self.run_id, endpoint_class=self.endpoint_class,
        )

    def record_blocked(self, ctx: MarketContext, code: str, reason: str,
                       decided_at: str, latency_ms: float = 0.0,
                       extra_input: Optional[Dict[str, Any]] = None,
                       attempted: Optional[bool] = None) -> AIDecision:
        """记录一次"没有产生可执行决策"的结果。

        ``attempted=True`` 表示请求**真的发出过**（超时/格式错/据答），来源标记为
        ``rejected``；否则（没有可用模型、根本没发请求）为 ``unavailable``。
        """
        tried = self.available() if attempted is None else bool(attempted)
        prov = PROVENANCE_REJECTED if tried else PROVENANCE_UNAVAILABLE
        d = AIDecision.new(
            ctx.symbol, decided_at, "blocked", rationale=reason,
            provenance=prov, errors=[code],
            latency_ms=latency_ms, **self._base(ctx, extra_input))
        self.ledger.record_decision(d)
        return d

    def record_signal(self, ctx: MarketContext, signal: TradingSignal,
                      decided_at: str, latency_ms: float = 0.0,
                      extra_input: Optional[Dict[str, Any]] = None) -> AIDecision:
        pct = float(signal.position_pct or 0.0)
        price = float(signal.entry_price or ctx.last_price or 0.0)
        est_qty = 0.0
        if pct > 0 and price > 0 and ctx.equity > 0:
            raw_qty = ctx.equity * pct / price
            est_qty = float(int(raw_qty // self.lot_size) * self.lot_size)

        conditions: List[str] = []
        if signal.stop_loss:
            price_ref = price or 1.0
            dist = abs(price_ref - float(signal.stop_loss)) / price_ref
            conditions.append(f"stop_loss={signal.stop_loss}（距现价 {dist:.2%}）")
        if signal.take_profit:
            conditions.append(f"take_profit={signal.take_profit}")
        if pct:
            conditions.append(f"position_pct<={pct:.2%}")
        conditions.append(f"max_position_pct<={ctx.max_position_pct:.2%}")
        for c in (signal.raw or {}).get("risk_conditions", []) or []:
            conditions.append(str(c)[:200])

        d = AIDecision.new(
            ctx.symbol, decided_at, signal.action,
            confidence=float(signal.confidence or 0.0),
            target_qty=est_qty, target_pct=pct,
            entry_price=signal.entry_price, stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            rationale=signal.rationale, risk_conditions=conditions,
            response_digest=canonical_digest(signal.raw or {}),
            usage=dict(getattr(self.model, "last_usage", {}) or {}),
            provenance=self.provenance, errors=list(signal.errors or []),
            signal_id=signal.signal_id, raw=dict(signal.raw or {}),
            latency_ms=latency_ms, **self._base(ctx, extra_input))
        self.ledger.record_decision(d)
        return d

    def decide(self, ctx: MarketContext, decided_at: str,
               extra_input: Optional[Dict[str, Any]] = None) -> AIDecision:
        """独立调用一次（不经过引擎）；引擎路径请用 :class:`AuditedModel`。"""
        if not self.available():
            return self.record_blocked(
                ctx, "AI_UNAVAILABLE",
                getattr(self.model, "reason", "") or "AI 模型不可用",
                decided_at, extra_input=extra_input)
        started = time.time()
        try:
            signal = self.model.analyze(ctx)
        except AIModelError as exc:
            return self.record_blocked(ctx, exc.code or "AI_ERROR", f"AI 调用失败：{exc}",
                                       decided_at, (time.time() - started) * 1000,
                                       extra_input)
        except Exception as exc:  # noqa: BLE001
            return self.record_blocked(ctx, "AI_EXCEPTION",
                                       f"AI 异常：{type(exc).__name__}: {exc}",
                                       decided_at, (time.time() - started) * 1000,
                                       extra_input)
        return self.record_signal(ctx, signal, decided_at,
                                  round((time.time() - started) * 1000, 1), extra_input)


class AuditedModel(AIModel):
    """在真实模型外面包一层，**每次** ``analyze`` 都落一条决策记录后原样返回信号。

    放在引擎与模型之间，是为了不改 ``strategy/engine.py`` 一行代码，同时让
    "AI 到底说什么、输入是什么版本、耗时多少"全部可审计。
    """

    def __init__(self, inner: AIModel, gateway: AIGateway,
                 clock: Optional[Callable[[], datetime]] = None):
        self.inner = inner
        self.gateway = gateway
        self._clock = clock or (lambda: datetime.now().astimezone())
        # 透传引擎/权威闸门需要的属性
        self.name = getattr(inner, "name", "audited")
        self.model_id = getattr(inner, "model_id", "audited-v0")
        self.is_llm = bool(getattr(inner, "is_llm", False))
        self.available = bool(getattr(inner, "available", True))
        self.can_trade = bool(getattr(inner, "can_trade", True))
        self.reason = getattr(inner, "reason", "")

    def _now_iso(self) -> str:
        return self._clock().astimezone().isoformat(timespec="seconds")

    def analyze(self, ctx: MarketContext) -> TradingSignal:
        started = time.time()
        try:
            signal = self.inner.analyze(ctx)
        except AIModelError as exc:
            self.gateway.record_blocked(ctx, exc.code or "AI_ERROR", str(exc),
                                        self._now_iso(),
                                        round((time.time() - started) * 1000, 1))
            raise
        except Exception as exc:  # noqa: BLE001
            self.gateway.record_blocked(ctx, "AI_EXCEPTION", repr(exc), self._now_iso(),
                                        round((time.time() - started) * 1000, 1))
            raise
        self.gateway.record_signal(ctx, signal, self._now_iso(),
                                   round((time.time() - started) * 1000, 1))
        # 决策时刻以**运行时钟**为准：回放 2026-11-06 时，信号的 ts 必须是
        # 2026-11-06 10:30（而不是墙钟当前时间），否则 AI 权威闸门的
        # check_freshness 会算出几十天的"过期"（实测 STALE_SIGNAL）。
        signal.ts = self._now_iso()
        return signal

    def health(self) -> Dict[str, Any]:
        base = self.inner.health() if hasattr(self.inner, "health") else {}
        base = dict(base)
        base["audited"] = True
        base["decisions_recorded"] = None
        return base
