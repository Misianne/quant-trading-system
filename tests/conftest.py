"""测试夹具：所有测试都在临时目录里跑，不污染项目 data/logs。"""
from __future__ import annotations

import copy
import os
from datetime import date, datetime
from pathlib import Path

import pytest

from ai_trader.ai.base import AIModel, MarketContext, TradingSignal
from ai_trader.ai.providers import HeuristicModel
from ai_trader.audit.journal import AuditJournal
from ai_trader.broker.base import Candle, OrderRequest, Position, Quote
from ai_trader.broker.paper import PaperBroker, SyntheticQuoteSource
from ai_trader.config import AppConfig, build_gate, load_config
from ai_trader.risk.controller import RiskController
from ai_trader.safety.halt import HaltStore, RESUME_PHRASE
from ai_trader.strategy.engine import StrategyEngine

# 一个确定在交易时段内的时点（周三 10:00，港股/沪深均在盘）
WEEKDAY_1000 = datetime(2026, 11, 4, 10, 0, 0)
# 周末时点
WEEKEND_1000 = datetime(2026, 11, 7, 10, 0, 0)


def make_cfg(tmp_path: Path, **overrides) -> AppConfig:
    base = load_config(use_dotenv=False)
    raw = copy.deepcopy(base.raw)
    raw.setdefault("system", {})
    raw["system"]["data_dir"] = str(tmp_path / "data")
    raw["system"]["log_dir"] = str(tmp_path / "logs")
    raw["system"]["mode"] = "paper"
    raw.setdefault("audit", {})
    raw["audit"]["db_path"] = str(tmp_path / "audit.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    raw.setdefault("safety", {})
    raw["safety"].update({"live_trading": False, "order_execution_enabled": False,
                          "paper_trading": True, "kill_switch": False})
    # 历史多市场夹具的显式声明（US-P1 配置守卫）
    # 生产配置已改为 unenforced_market_policy="reject"（规则未建模的市场一律拒绝）。
    # 但本文件里大量既有用例用港股作为"通用标的"来验证风控机制（重复单、熔断、
    # 急停等），它们验证的不是"港股能不能交易"。为了不改变这些用例的原意，
    # 测试夹具显式声明旧的 warn 语义；生产是否 reject 由
    # tests/test_us_p1_batch1.py::test_production_config_is_reject 单独断言。
    if "market_rules.unenforced_market_policy" not in overrides:
        raw.setdefault("market_rules", {})["unenforced_market_policy"] = "warn"
    # 历史多市场夹具的显式声明（US-P1 配置守卫）
    # 生产配置已改为 unenforced_market_policy="reject"（规则未建模的市场一律拒绝）。
    # 但本文件里大量既有用例用港股作为"通用标的"来验证风控机制（重复单、熔断、
    # 急停等），它们验证的不是"港股能不能交易"。为了不改变这些用例的原意，
    # 测试夹具显式声明旧的 warn 语义；生产是否 reject 由
    # tests/test_us_p1_batch1.py::test_production_config_is_reject 单独断言。
    if "market_rules.unenforced_market_policy" not in overrides:
        raw.setdefault("market_rules", {})["unenforced_market_policy"] = "warn"
    # 深度覆盖：支持 "risk.max_positions": 3 形式
    for dotted, value in overrides.items():
        node = raw
        parts = dotted.split(".")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = value
    data_dir = Path(raw["system"]["data_dir"])
    data_dir.mkdir(parents=True, exist_ok=True)
    # 与生产一致：门禁要读急停哨兵
    cfg = AppConfig(raw=raw, path=tmp_path / "config.yaml",
                    gate=build_gate(raw, data_dir=data_dir))
    cfg.halt_store = HaltStore(cfg.data_dir)
    return cfg


@pytest.fixture
def cfg(tmp_path):
    return make_cfg(tmp_path)


@pytest.fixture
def journal(tmp_path):
    j = AuditJournal(tmp_path / "audit.db", tmp_path / "audit.jsonl")
    yield j
    j.close()


@pytest.fixture
def paper(tmp_path):
    return PaperBroker(
        state_path=tmp_path / "paper.json",
        initial_cash={"HKD": 1_000_000.0, "USD": 130_000.0, "CNY": 700_000.0},
        commission_bps=8.0, min_commission=3.0, slippage_bps=5.0,
    )


@pytest.fixture
def risk(cfg):
    return RiskController(cfg, cfg.gate)


@pytest.fixture
def engine(cfg, paper, journal, risk):
    model = HeuristicModel(min_confidence=0.60, max_position_pct=0.15)
    return StrategyEngine(cfg, paper, model, risk, journal,
                          stops_path=Path(cfg.raw["system"]["data_dir"]) / "stops.json")


class StubModel(AIModel):
    """可编排输出的假模型，用于验证风控不会放过非法信号。"""

    name = "stub"
    model_id = "stub-v1"

    def __init__(self, signal: TradingSignal):
        self.signal = signal
        self.calls = 0

    def analyze(self, ctx: MarketContext) -> TradingSignal:
        self.calls += 1
        return self.signal


def make_candles(closes, start="2026-01-01") -> list[Candle]:
    out = []
    for i, c in enumerate(closes):
        out.append(Candle(ts=f"{start}+{i}", open=c, high=c * 1.005, low=c * 0.995,
                          close=c, volume=1000))
    return out


def pos(symbol="hk00700", qty=100, avg_cost=380.0, last=385.0, currency="HKD") -> Position:
    return Position(symbol=symbol, market="hk", qty=qty, avg_cost=avg_cost,
                    last_price=last, currency=currency)


# --------------------------------------------------------------------------- #
# 确定性：把合成行情的参考日固定下来
# --------------------------------------------------------------------------- #
# 背景（US-P1 复评发现）：`SyntheticQuoteSource` 原来用 ``date.today()`` 做随机种子，
# 于是"同一条测试"在不同日历日的行情完全不同 —— 与行情无关的用例（AI 权威、
# 并发去重、行情推送门槛）会因为"今天恰好是空头排列"而随机失败。
# 这里在测试会话级别把参考日固定，使结果与本机日期无关；生产/演示仍按当天变化。
REFERENCE_DAY = date(2026, 11, 8)


@pytest.fixture(autouse=True, scope="session")
def _deterministic_quotes():
    old = SyntheticQuoteSource.reference_day
    SyntheticQuoteSource.reference_day = REFERENCE_DAY
    yield
    SyntheticQuoteSource.reference_day = old


# --------------------------------------------------------------------------- #
# 进程级凭证注册表（US-P1 Batch 4 / B-1）
# --------------------------------------------------------------------------- #
# `FosunBroker` 的身份密钥必须写进 `os.environ`（官方 SDK 只认环境变量），
# 因此多个实例共享一个**进程级**注册表。它是真实的生产状态，测试之间必须隔离，
# 否则前一个用例的"持有人"会让后一个用例（用不同假凭证）撞上 CredentialConflict
# ——那是被测代码的正确行为，不是缺陷。这里只清注册表，不碰环境变量（环境变量
# 由各用例的 monkeypatch 负责回滚）。
@pytest.fixture(autouse=True)
def _isolated_credential_registry():
    from ai_trader.broker.fosun_adapter import _CRED_ENV

    _CRED_ENV.clear()
    yield
    _CRED_ENV.clear()


@pytest.fixture(autouse=True)
def _isolate_timebase():
    """每个测试结束后强制还原全局时钟（兜底，不依赖各测试自觉）。

    回放测试会 ``timebase.install(ManualClock)``。一旦某个用例忘记还原，
    后续所有依赖"现在"的用例都会被污染 —— 表现为大面积时间相关断言失败，
    而且很难定位到真正的原因。这里把它变成结构上不可能发生。
    """
    yield
    try:
        from ai_trader.paper_runtime.timebase_bridge import active, restore_clock
        while active():
            restore_clock()
    except Exception:  # noqa: BLE001 - 清理阶段不允许影响测试结果
        pass
    try:
        from ai_trader import timebase
        timebase.restore()
    except Exception:  # noqa: BLE001
        pass
