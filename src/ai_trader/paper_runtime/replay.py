"""确定性回放：行情源、10 个验收场景、驱动器。

确定性来自三条硬约束
--------------------
1. **价格是显式列表**，不是随机游走；没有 ``random``、没有网络。
2. **时间是回放时钟**（:class:`ai_trader.timebase.ManualClock`），
   行情时间戳、订单时间戳、审计时间戳全部由它派生。
3. **信号是剧本**（:class:`ScriptedModel`），不调 LLM。

因此「同一场景 + 同一配置」重复运行必须得到同一个 :attr:`ReplayResult.digest`。
digest 覆盖：成交账本指纹、订单明细、期末现金。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from ..broker.base import Candle, Quote
from ..market.clock import ExchangeClock, SessionWindow
from ..market.status import DayBounds, MarketState
from .model import ScriptStep, ScriptedModel

NY = ZoneInfo("America/New_York")


class ReplayFeedDown(RuntimeError):
    """模拟行情中断（被行情组件捕获 → 不健康 → HALT/DEGRADED）。"""


# ---------------------------------------------------------------------- #
# 行情源
# ---------------------------------------------------------------------- #
@dataclass
class ReplayQuoteSource:
    """按 tick 取价。时间戳一律取**当前回放时钟**，不读系统时间。"""

    prices: Dict[str, List[float]]
    prev_close: Dict[str, float] = field(default_factory=dict)
    down_ticks: Tuple[int, ...] = ()
    market: str = "us"
    source_name: str = "replay"
    tick: int = 0
    calls: int = 0
    failures: int = 0
    _base_ts: Optional[datetime] = None

    # ------------------------------------------------------------------ #
    def set_tick(self, tick: int) -> None:
        self.tick = int(tick)

    def load(self, *, prices: Dict[str, List[float]],
             prev_close: Optional[Dict[str, float]] = None,
             down_ticks: Sequence[int] = ()) -> None:
        """原地换一组行情（供 Runtime 复用；不新建对象）。"""
        self.prices = {str(k).upper(): list(v) for k, v in prices.items()}
        self.prev_close = {str(k).upper(): float(v)
                           for k, v in (prev_close or {}).items()}
        self.down_ticks = tuple(int(x) for x in down_ticks)
        self.tick = 0
        self.calls = 0
        self.failures = 0

    def _now(self) -> datetime:
        from .. import timebase
        return timebase.now()

    def _price(self, symbol: str) -> float:
        key = symbol.upper()
        series = self.prices.get(key) or self.prices.get(symbol) or []
        if not series:
            raise ReplayFeedDown(f"{symbol} 无回放数据")
        idx = min(self.tick, len(series) - 1)
        return float(series[idx])

    def quote(self, symbol: str) -> Quote:
        self.calls += 1
        if self.tick in self.down_ticks:
            self.failures += 1
            raise ReplayFeedDown(f"回放行情中断（tick={self.tick}）")
        px = self._price(symbol)
        ts = self._now().isoformat(timespec="seconds")
        key = symbol.upper()
        return Quote(symbol=symbol, market=self.market, last=px,
                     open=px, high=px, low=px,
                     prev_close=float(self.prev_close.get(key, px)),
                     volume=1000.0, ts=ts, source=self.source_name)

    def klines(self, symbol: str, ktype: str = "day", num: int = 120) -> List[Candle]:
        """生成确定性日线（只用于技术指标/预筛，不参与撮合定价）。"""
        key = symbol.upper()
        series = self.prices.get(key) or self.prices.get(symbol) or [1.0]
        px = float(series[min(self.tick, len(series) - 1)])
        out: List[Candle] = []
        day0 = self._now().date()
        for i in range(int(num)):
            d = day0 - timedelta(days=int(num) - i)
            # 用位置做确定性偏移，避免随机
            drift = 1.0 + ((i % 7) - 3) * 0.0015
            close = round(px * drift, 4)
            out.append(Candle(ts=d.isoformat(), open=close, high=close * 1.004,
                              low=close * 0.996, close=close, volume=1000.0 + i))
        return out

    def as_dict(self) -> Dict[str, Any]:
        return {"source": self.source_name, "tick": self.tick, "calls": self.calls,
                "failures": self.failures, "symbols": sorted(self.prices),
                "down_ticks": list(self.down_ticks)}


# ---------------------------------------------------------------------- #
# 场景
# ---------------------------------------------------------------------- #
@dataclass
class ReplayScenario:
    key: str
    title: str
    trading_date: date
    start_hm: Tuple[int, int]
    tick_minutes: int
    ticks: int
    prices: Dict[str, List[float]]
    prior_close: Dict[str, float]
    script: List[ScriptStep] = field(default_factory=list)
    early_close_hm: Optional[Tuple[int, int]] = None
    is_holiday: bool = False
    down_ticks: Tuple[int, ...] = ()
    use_luld: bool = False
    index_path: Optional[List[float]] = None
    #: 在这些 tick 上把时钟跳到**下一个工作日 09:30**（用于跨日 T+1 场景）
    day_breaks: Tuple[int, ...] = ()
    expect: Dict[str, Any] = field(default_factory=dict)
    notes: str = ""

    # ------------------------------------------------------------------ #
    def start_at(self) -> datetime:
        return datetime(self.trading_date.year, self.trading_date.month,
                        self.trading_date.day, self.start_hm[0], self.start_hm[1],
                        tzinfo=NY)

    def bounds(self) -> DayBounds:
        close_hm = self.early_close_hm or (16, 0)
        return DayBounds(
            open_at=datetime(self.trading_date.year, self.trading_date.month,
                             self.trading_date.day, 9, 30, tzinfo=NY),
            close_at=datetime(self.trading_date.year, self.trading_date.month,
                              self.trading_date.day, close_hm[0], close_hm[1],
                              tzinfo=NY),
            is_trading_day=not self.is_holiday)

    def clock(self) -> ExchangeClock:
        early = {}
        if self.early_close_hm:
            early[self.trading_date] = dtime(*self.early_close_hm)
        return ExchangeClock(
            "us", [SessionWindow(dtime(9, 30), dtime(16, 0))],
            tz_name="America/New_York",
            pre_window=SessionWindow(dtime(4, 0), dtime(9, 30)),
            post_window=SessionWindow(dtime(16, 0), dtime(20, 0)),
            night_window=SessionWindow(dtime(20, 0), dtime(4, 0)),
            holidays=([self.trading_date] if self.is_holiday else []),
            early_close=early, allow_extended=False)

    def price_series(self) -> Dict[str, List[float]]:
        return {k.upper(): list(v) for k, v in self.prices.items()}

    def input_digest(self) -> str:
        import hashlib
        blob = json.dumps({
            "key": self.key, "date": self.trading_date.isoformat(),
            "start": self.start_hm, "tick_min": self.tick_minutes,
            "ticks": self.ticks, "prices": self.price_series(),
            "prior": self.prior_close, "down": list(self.down_ticks),
            "early": self.early_close_hm, "holiday": self.is_holiday,
            "script": [s.__dict__ for s in self.script],
            "index": self.index_path,
        }, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _path(base: float, pcts: Sequence[float]) -> List[float]:
    return [round(base * (1 + p / 100.0), 4) for p in pcts]


#: 10 个端到端场景。价格路径全部显式，便于断言与复现。
SCENARIOS: Dict[str, ReplayScenario] = {}


def _add(s: ReplayScenario) -> None:
    SCENARIOS[s.key] = s


_add(ReplayScenario(
    key="normal_day", title="正常交易日（常规时段建仓 + 止盈退出）",
    trading_date=date(2026, 11, 16), start_hm=(9, 40), tick_minutes=5, ticks=12,
    prices={"usAAPL": _path(180.0, [0, .1, .2, .3, .55, .9, 1.4, 2.0, 2.6, 3.0, 3.2, 3.4])},
    prior_close={"usAAPL": 180.0},
    script=[ScriptStep(2, "usAAPL", "buy", position_pct=0.10, stop_loss_pct=0.05,
                       take_profit_pct=0.02, rationale="正常日建仓"),
            ScriptStep(8, "usAAPL", "sell", position_pct=0.10, stop_loss_pct=0.05,
                       rationale="次日止盈退出")],
    day_breaks=(6,),
    expect={"min_fills": 2, "final_state": "RUNNING", "phase": "regular"},
    notes="验证『信号→风控→下单→成交→入账→不变量』全链；"
          "卖出安排在**次日**，因为 T+1 禁止当日买入当日卖出"))

_add(ReplayScenario(
    key="premarket", title="盘前行情（04:00-09:30，不允许常规开仓）",
    trading_date=date(2026, 11, 16), start_hm=(8, 0), tick_minutes=10, ticks=6,
    prices={"usAAPL": _path(180.0, [0, .2, .4, .3, .5, .6])},
    prior_close={"usAAPL": 180.0},
    script=[ScriptStep(1, "usAAPL", "buy", position_pct=0.10, rationale="盘前不该成交")],
    expect={"max_fills": 0, "phase": "pre"},
    notes="行情网关应拒绝 pre 时段的 regular_only 用途"))

_add(ReplayScenario(
    key="regular_session", title="常规交易时段（09:30-16:00 连续多笔）",
    trading_date=date(2026, 11, 16), start_hm=(10, 0), tick_minutes=3, ticks=10,
    prices={"usNVDA": _path(420.0, [0, -.1, -.2, -.1, .1, .3, .5, .4, .6, .8])},
    prior_close={"usNVDA": 420.0},
    script=[ScriptStep(1, "usNVDA", "buy", position_pct=0.12, take_profit_pct=0.05,
                       rationale="常规时段建仓"),
            ScriptStep(6, "usNVDA", "sell", position_pct=0.12, rationale="次日退出")],
    day_breaks=(4,),
    expect={"min_fills": 2, "phase": "regular"},
    notes="同一时段多次 tick，验证状态与账本连续；卖出跨日以满足 T+1"))

_add(ReplayScenario(
    key="afterhours", title="盘后行情（16:00-20:00，不允许常规开仓）",
    trading_date=date(2026, 11, 16), start_hm=(17, 0), tick_minutes=10, ticks=5,
    prices={"usAAPL": _path(180.0, [0, .3, .5, .6, .7])},
    prior_close={"usAAPL": 180.0},
    script=[ScriptStep(1, "usAAPL", "buy", position_pct=0.10, rationale="盘后不该成交")],
    expect={"max_fills": 0, "phase": "post"},
    notes="盘后价不得冒充常规价"))

_add(ReplayScenario(
    key="early_close", title="早收市（13:00 收盘）",
    trading_date=date(2026, 11, 27), start_hm=(12, 40), tick_minutes=3, ticks=8,
    prices={"usAAPL": _path(181.0, [0, .2, .4, .6, .5, .7, .9, 1.0])},
    prior_close={"usAAPL": 181.0},
    early_close_hm=(13, 0),
    script=[ScriptStep(1, "usAAPL", "buy", position_pct=0.10, take_profit_pct=0.03,
                       rationale="早收市前建仓")],
    expect={"phase": "regular", "closes_at": "13:00"},
    notes="收盘时间必须按当日实际 13:00 判定，不能写死 16:00"))

_add(ReplayScenario(
    key="holiday", title="节假日（非交易日）",
    trading_date=date(2026, 11, 26), start_hm=(11, 0), tick_minutes=5, ticks=4,
    prices={"usAAPL": _path(180.0, [0, .1, .2, .3])},
    prior_close={"usAAPL": 180.0},
    is_holiday=True,
    script=[ScriptStep(0, "usAAPL", "buy", position_pct=0.10, rationale="节假日不该成交")],
    expect={"max_fills": 0, "phase": "holiday"},
    notes="节假日任何下单都应被拒（无时段可交易）"))

_add(ReplayScenario(
    key="luld_limit_state", title="LULD Limit State（带外价禁止成交）",
    trading_date=date(2026, 11, 16), start_hm=(10, 0), tick_minutes=1, ticks=14,
    prices={"usAAPL": _path(100.0, [0, .2, .5, .8, 1.2, 3.0, 14.0, 14.5, 15.0,
                                  15.2, 15.4, 15.6, 15.8, 16.0])},
    prior_close={"usAAPL": 100.0},
    use_luld=True,
    script=[ScriptStep(6, "usAAPL", "buy", position_pct=0.10,
                       stop_loss_pct=0.05, rationale="越界后试图成交")],
    expect={"max_fills": 0, "state_contains": "luld_limit_state"},
    notes="+14% 远超 Tier1 5% 带宽 → 进入 Limit State；带外成交必须被拒"))

_add(ReplayScenario(
    key="luld_pause_resume", title="LULD 暂停（持续越界 → 交易暂停，不恢复）",
    trading_date=date(2026, 11, 16), start_hm=(10, 0), tick_minutes=1, ticks=20,
    prices={"usAAPL": _path(100.0, [0, .3, .6, 1.0, 6.0] + [6.0] * 15)},
    prior_close={"usAAPL": 100.0},
    use_luld=True,
    script=[ScriptStep(4, "usAAPL", "buy", position_pct=0.10,
                       rationale="暂停期试图成交"),
            ScriptStep(10, "usAAPL", "buy", position_pct=0.10,
                       rationale="暂停持续中再试")],
    expect={"max_fills": 0, "state_contains": "luld_paused"},
    notes="越界持续 >15s → LULD_PAUSED；暂停期间不得伪造成交"))

_add(ReplayScenario(
    key="mwcb", title="MWCB 熔断（指数 -8% → L1 暂停 15 分钟）",
    trading_date=date(2026, 11, 16), start_hm=(12, 0), tick_minutes=5, ticks=6,
    prices={"usAAPL": _path(100.0, [0, -.5, -1.0, -1.5, -2.0, -2.5])},
    prior_close={"usAAPL": 100.0},
    index_path=[4500.0, 4100.0, 4080.0, 4070.0, 4060.0, 4050.0],
    script=[ScriptStep(2, "usAAPL", "buy", position_pct=0.10, rationale="熔断期试图成交")],
    expect={"max_fills": 0, "state_contains": "mwcb_paused"},
    notes="指数跌幅 ≥7% 触发 L1，全市场暂停 15 分钟"))

_add(ReplayScenario(
    key="feed_outage", title="行情中断及恢复（组件失效 → HALT，不自动恢复）",
    trading_date=date(2026, 11, 16), start_hm=(10, 0), tick_minutes=2, ticks=10,
    prices={"usAAPL": _path(180.0, [0, .2, .4, .6, .8, 1.0, 1.2, 1.4, 1.6, 1.8])},
    prior_close={"usAAPL": 180.0},
    down_ticks=(3, 4, 5, 6, 7, 8, 9),
    script=[ScriptStep(1, "usAAPL", "buy", position_pct=0.10, rationale="中断前建仓")],
    expect={"halted": True, "state_contains": "halted"},
    notes="行情组件持续不健康 → 关键故障 → HALT（不允许无限重启）"))


# ---------------------------------------------------------------------- #
# 驱动器
# ---------------------------------------------------------------------- #
@dataclass
class ReplayResult:
    scenario: str
    run_id: str
    ok: bool
    digest: str = ""
    input_digest: str = ""
    ticks: int = 0
    fills: int = 0
    orders: int = 0
    final_state: str = ""
    states_seen: List[str] = field(default_factory=list)
    phases_seen: List[str] = field(default_factory=list)
    invariants_ok: bool = True
    failures: List[str] = field(default_factory=list)
    report: Dict[str, Any] = field(default_factory=dict)
    clock_trace: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"scenario": self.scenario, "run_id": self.run_id, "ok": self.ok,
                "digest": self.digest, "input_digest": self.input_digest,
                "ticks": self.ticks, "fills": self.fills, "orders": self.orders,
                "final_state": self.final_state,
                "states_seen": self.states_seen, "phases_seen": self.phases_seen,
                "invariants_ok": self.invariants_ok, "failures": self.failures,
                "clock_trace": self.clock_trace, "report": self.report}


def build_runtime_for(scenario: ReplayScenario, *, cfg: Any, out_dir: Path,
                      journal: Any = None, explicit_paper: bool = True,
                      lock_root: Optional[Path] = None):
    """按场景构造一个**全新**的 PaperRuntime（运行目录独立 → 起点状态一致）。"""
    from .. import timebase
    from ..market.status import MarketStatusService
    from .runtime import PaperRuntime
    from .timebase_bridge import install_replay_clock

    clock = scenario.clock()
    start = scenario.start_at()
    replay_clock = install_replay_clock(start)

    src = ReplayQuoteSource(prices=scenario.price_series(),
                            prev_close=scenario.prior_close,
                            down_ticks=tuple(scenario.down_ticks))
    model = ScriptedModel(scenario.script)

    paper_cfg = {
        "enabled": bool((cfg.raw.get("paper_runtime") or {}).get("enabled", False)),
        "broker": {"initial_cash": {"USD": 100_000.0},
                   "commission_bps": 0.0, "min_commission": 0.0, "slippage_bps": 0.0},
        "settlement": {"currency": "USD", "mode": "cash"},
        "short": {"mode": "disabled"},
        "feed": {"max_quote_age_seconds": 300.0, "max_idle_seconds": 300.0},
        "replay": {"scenario": scenario.key, "tick_minutes": scenario.tick_minutes},
    }
    rt = PaperRuntime(cfg=cfg, paper_cfg=paper_cfg, clocks={"us": clock},
                      quote_source=src, model=model, journal=journal,
                      symbols=sorted(scenario.price_series()), run_dir=out_dir,
                      clock=replay_clock, explicit_paper=explicit_paper,
                      lock_root=lock_root)
    return rt, replay_clock, src, model


def run_scenario(scenario: ReplayScenario, *, cfg: Any, out_dir: Path,
                 journal: Any = None) -> ReplayResult:
    """跑一个场景：逐 tick 推进回放时钟，注入 LULD/MWCB 事件，收集证据。"""
    from .. import timebase

    res = ReplayResult(scenario=scenario.key, run_id="", ok=False)
    res.input_digest = scenario.input_digest()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rt, replay_clock, src, model = build_runtime_for(
        scenario, cfg=cfg, out_dir=out_dir, journal=journal)
    res.run_id = rt.run_id
    try:
        start = rt.start()
        if not start.ok:
            res.failures.append(f"start 失败：{start.reason}")
            res.report = {"start": start.as_dict()}
            return res
        rt.to_running()

        ms = rt.ctx.service("market_status")
        bounds = scenario.bounds()
        states: List[str] = []
        market_states: List[str] = []
        phases: List[str] = []
        trace: List[str] = []
        for i in range(scenario.ticks):
            # 1) 时钟推进（支持跨日：用于验证 T+1 结算与日内重置）
            if i in scenario.day_breaks:
                cur = timebase.now()
                nxt = cur.date() + timedelta(days=1)
                while nxt.weekday() >= 5:
                    nxt += timedelta(days=1)
                replay_clock.set(datetime(nxt.year, nxt.month, nxt.day, 9, 30, tzinfo=NY))
            elif i:
                replay_clock.advance(timedelta(minutes=scenario.tick_minutes))
            src.set_tick(i)
            now = timebase.now()
            trace.append(now.isoformat(timespec="seconds"))

            # 2) 事件注入：LULD 必须**先有参考价**，再走 observe_trade 的 15 秒判定
            if ms is not None and scenario.use_luld:
                for sym, series in scenario.price_series().items():
                    prior = float(scenario.prior_close.get(sym)
                                  or scenario.prior_close.get(sym.lower()) or series[0])
                    if i == 0:
                        ms.update_reference(sym, prior, prior_close=prior,
                                            note=f"replay:{scenario.key}")
                    px = float(series[min(i, len(series) - 1)])
                    ms.observe_trade(sym, px, now, bounds=bounds, prior_close=prior)
            # 3) 事件注入：MWCB
            if ms is not None and scenario.index_path and i < len(scenario.index_path):
                ms.update_index(float(scenario.index_path[i]), now, bounds=bounds)

            tr = rt.tick()
            states.append(rt.machine.state.value)
            if ms is not None:
                for sym in scenario.price_series():
                    market_states.append(ms.status(sym, now).state.value)
            phases.append(scenario.clock().session(now).phase)
            if rt.machine.state.value in ("HALTED", "FAILED"):
                break

        # 4) 汇总
        broker = rt.ctx.service("broker")
        orders = broker.orders_snapshot() if broker is not None else []
        res.ticks = rt._ticks
        res.orders = len(orders)
        res.fills = len(rt.ledger.fills())
        res.final_state = rt.machine.state.value
        res.states_seen = sorted(set(states + [h.to.value for h in rt.machine.history]))
        res.phases_seen = sorted(set(phases))
        res.invariants_ok = rt._last_invariants.ok
        res.digest = rt.digest()
        res.clock_trace = trace
        res.report = {
            "scenario": scenario.title,
            "input_digest": res.input_digest,
            "start": start.as_dict(),
            "health": rt.health(),
            "snapshot": rt.snapshot(),
            "orders": [{k: o.get(k) for k in
                        ("order_id", "symbol", "side", "qty", "status",
                         "filled_qty", "avg_fill_price", "market_note")}
                       for o in orders],
            "fills": [f.as_dict() for f in rt.ledger.fills()],
            "clock_trace": trace,
            "phases": phases,
            "market_states": sorted(set(market_states)),
            "scenario_notes": scenario.notes,
        }
        # 5) 断言
        exp = scenario.expect or {}
        if "min_fills" in exp and res.fills < int(exp["min_fills"]):
            res.failures.append(f"成交数 {res.fills} < 期望 {exp['min_fills']}")
        if "max_fills" in exp and res.fills > int(exp["max_fills"]):
            res.failures.append(f"成交数 {res.fills} > 允许 {exp['max_fills']}")
        if "final_state" in exp and res.final_state != exp["final_state"]:
            res.failures.append(f"终态 {res.final_state} ≠ 期望 {exp['final_state']}")
        if "halted" in exp and bool(exp["halted"]) != (res.final_state == "HALTED"):
            res.failures.append(f"是否 HALT 与期望不符（实际 {res.final_state}）")
        if "state_contains" in exp:
            want = str(exp["state_contains"]).lower()
            pool = [x.lower() for x in res.states_seen] + \
                   [x.lower() for x in (res.report.get("market_states") or [])]
            if not any(want in x for x in pool):
                res.failures.append(
                    f"未出现预期状态 {want}；运行状态={res.states_seen} "
                    f"市场状态={res.report.get('market_states')}")
        if "phase" in exp:
            if exp["phase"] not in res.phases_seen:
                res.failures.append(f"未出现预期时段 {exp['phase']}；实际 {res.phases_seen}")
        if "closes_at" in exp:
            sess = scenario.clock().session(timebase.now())
            if sess.closes_at != exp["closes_at"]:
                res.failures.append(f"收盘时间 {sess.closes_at} ≠ {exp['closes_at']}")
        if not res.invariants_ok:
            res.failures.append("账户不变量未通过")
        res.ok = not res.failures
        return res
    finally:
        rt.stop("scenario_end")
        from .timebase_bridge import restore_clock
        restore_clock()
