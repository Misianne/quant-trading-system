"""单运行体、多场景连续会话（Runtime 复用）。

为什么需要
----------
上一版长跑每轮 `build_runtime_for()` 新建一整套对象（Runtime / Engine / Broker /
Journal / DB 文件）。即使没有泄漏，也会产生两个副作用：

1. 每轮新建一个 SQLite 库文件 → 磁盘与工作集随轮次增长；
2. 账户无法跨场景累积（每次都是 10 万现金的"新户"），
   验证不到"长时间运行后账本/持仓/订单历史仍自洽"。

本模块复用**同一个** PaperRuntime / 账户 / 账本 / 审计库，把一个交易日作为一个
"段"（segment）循环推进：只替换行情序列与剧本，其余对象保持不变。

生命周期
--------
只构造一次 → `start()` 一次 → 每个 segment 只做「推进时钟 + 换数据 + 跑 tick」
→ 结束时 `stop()` 一次。任何对象都不需要重复创建，因此也不存在"靠重启掩盖泄漏"。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..market.clock import ExchangeClock, SessionWindow
from .model import ScriptStep, ScriptedModel
from .replay import ReplayQuoteSource, _path
from .runtime import PaperRuntime

NY = __import__("zoneinfo").ZoneInfo("America/New_York")


@dataclass
class Segment:
    """一个交易日的行情与剧本。"""

    index: int
    kind: str                       # buy / sell / luld / mwcb / quiet
    trading_date: date
    prices: Dict[str, List[float]]
    prior_close: Dict[str, float]
    ticks: int
    script: List[ScriptStep] = field(default_factory=list)
    use_luld: bool = False
    index_path: Optional[List[float]] = None
    expect_fill: bool = True

    def prices_of(self, sym: str) -> List[float]:
        return self.prices[sym.upper()]


def build_segment(i: int, day: date, base: float = 180.0,
                  mode: Optional[str] = None) -> Segment:
    """确定性生成第 i 段（不随机：全部由 i 推导，可复现）。"""
    # 温和锯齿：±0.6% 内波动，避免价格漂移导致现金/持仓失控
    drift = ((i % 9) - 4) * 0.0015
    ticks = 8
    # 美股最小报价单位 0.01：价格必须落在 tick 上，否则会被风控以 PRICE_TICK 拒绝
    path = [round(p, 2) for p in _path(
        round(base * (1 + drift * 0.1), 2),
        [(k * 0.08) + (0.05 if i % 2 else -0.05) for k in range(ticks)])]
    sym = "usAAPL"
    # 按**交易日**决定买/卖（T+1：当日买入当日不可卖，所以同日多段必须同向）
    kind = mode or ("buy" if i % 2 == 1 else "sell")
    script: List[ScriptStep] = []

    if i % 23 == 0:
        kind = "luld"
        path = [round(p, 2) for p in
                _path(100.0, [0, .2, .5, 1.0, 12.0] + [12.0] * (ticks - 5))]
        script = [ScriptStep(4, sym, "buy", position_pct=0.02,
                             rationale="LULD 越界，不该成交")]
    elif i % 17 == 0:
        kind = "mwcb"
        script = [ScriptStep(2, sym, "buy", position_pct=0.02,
                             rationale="熔断期，不该成交")]
    elif kind == "buy":
        script = [ScriptStep(3, sym, "buy", position_pct=0.02,
                             stop_loss_pct=0.05, take_profit_pct=0.03,
                             rationale=f"seg{i} 建仓")]
    else:
        script = [ScriptStep(3, sym, "sell", position_pct=0.5,
                             stop_loss_pct=0.05, rationale=f"seg{i} 减仓")]

    return Segment(index=i, kind=kind, trading_date=day,
                   prices={sym.upper(): path},
                   prior_close={sym.upper(): path[0]},
                   ticks=ticks, script=script,
                   use_luld=(kind == "luld"),
                   index_path=([4500.0, 4100.0] + [4080.0] * (ticks - 2)
                               if kind == "mwcb" else None),
                   expect_fill=(kind not in ("luld", "mwcb")))


class ReplaySession:
    """复用单个 PaperRuntime 的连续回放会话。"""

    def __init__(self, *, cfg: Any, out_dir: Path, journal: Any = None,
                 start_date: date = date(2026, 11, 16),
                 symbols: Sequence[str] = ("usAAPL",),
                 lock_root: Optional[Path] = None):
        self.cfg = cfg
        self.out_dir = Path(out_dir)
        self.symbols = [s.upper() for s in symbols]
        self._start_date = start_date
        self._day = start_date
        self._cursor = datetime(start_date.year, start_date.month, start_date.day,
                                9, 30, tzinfo=NY)
        self._segments = 0
        self._day_index = 0

        holidays = []
        try:
            from datetime import date as _d
            raw = (((cfg.raw.get("market_rules") or {}).get("calendar") or {})
                   .get("markets") or {}).get("us") or {}
            for x in (raw.get("holidays") or []):
                try:
                    holidays.append(_d.fromisoformat(str(x)))
                except ValueError:
                    continue
        except Exception:  # noqa: BLE001 - 配置缺失不该阻断会话
            holidays = []
        clock = ExchangeClock(
            "us", [SessionWindow(dtime(9, 30), dtime(16, 0))],
            tz_name="America/New_York",
            pre_window=SessionWindow(dtime(4, 0), dtime(9, 30)),
            post_window=SessionWindow(dtime(16, 0), dtime(20, 0)),
            night_window=SessionWindow(dtime(20, 0), dtime(4, 0)),
            holidays=holidays, early_close={}, allow_extended=False)
        self.clock = clock

        seg0 = build_segment(1, start_date)
        self.source = ReplayQuoteSource(prices=seg0.prices,
                                        prev_close=seg0.prior_close)
        self.model = ScriptedModel(seg0.script)
        paper_cfg = {
            "enabled": bool((cfg.raw.get("paper_runtime") or {}).get("enabled", False)),
            "broker": {"initial_cash": {"USD": 1_000_000.0},
                       "commission_bps": 1.0, "min_commission": 0.0,
                       "slippage_bps": 1.0},
            "settlement": {"currency": "USD", "mode": "cash"},
            "short": {"mode": "disabled"},
            "feed": {"max_quote_age_seconds": 300.0, "max_idle_seconds": 300.0},
            "replay": {"scenario": "session", "reuse": True},
        }
        from .timebase_bridge import install_replay_clock
        self.replay_clock = install_replay_clock(
            datetime(start_date.year, start_date.month, start_date.day,
                     9, 25, tzinfo=NY))
        self.rt = PaperRuntime(
            cfg=cfg, paper_cfg=paper_cfg, clocks={"us": clock},
            quote_source=self.source, model=self.model, journal=journal,
            symbols=self.symbols, run_dir=self.out_dir,
            clock=self.replay_clock, explicit_paper=True,
            lock_root=lock_root if lock_root is not None else self.out_dir)
        self.market_status = None

    # ------------------------------------------------------------------ #
    def start(self):
        res = self.rt.start()
        if res.ok:
            self.rt.to_running()
            self.market_status = self.rt.ctx.service("market_status")
        return res

    #: 每个"段"代表的日内时长（分钟）
    SEGMENT_MINUTES = 45

    def _advance_cursor(self) -> datetime:
        """推进到下一段起点：同日内 +45 分钟；收盘后跳到下一交易日 09:30。"""
        cur = self._cursor + timedelta(minutes=self.SEGMENT_MINUTES)
        if cur.time() >= dtime(15, 30):
            d = self._day + timedelta(days=1)
            while True:
                ok, _why = self.clock.is_trading_day(d)
                if ok:
                    break
                d += timedelta(days=1)
            self._day = d
            cur = datetime(d.year, d.month, d.day, 9, 30, tzinfo=NY)
        self._cursor = cur
        return cur

    def run_segment(self) -> Dict[str, Any]:
        """推进一个交易日。返回该段的摘要。"""
        if self.market_status is None:
            raise RuntimeError("会话未启动")
        cursor = self._advance_cursor()
        day = cursor.date()
        new_day = (cursor.hour == 9 and cursor.minute == 30)
        if new_day:
            self._day_index += 1
        mode = "buy" if self._day_index % 2 == 1 else "sell"
        seg = build_segment(self._segments + 1, day, mode=mode)
        self._segments += 1

        # 1) 时钟推进到本段起点（只向前）
        self.replay_clock.set(cursor)
        # 每个交易日的**第一段**：交易所隔夜重置，清掉上一日的停牌/暂停状态
        if new_day:
            for sym in self.symbols:
                try:
                    self.market_status.notify_resume(sym, cursor, source="session",
                                                     note="新的交易日开始，交易所重置")
                except Exception:  # noqa: BLE001
                    pass
        # 2) 换行情与剧本（同一对象，不新建）
        self.source.load(prices=seg.prices, prev_close=seg.prior_close,
                         down_ticks=())
        self.model.load(seg.script)
        # 3) LULD 场景需要重置参考价
        if seg.use_luld:
            for sym, series in seg.prices.items():
                self.market_status.update_reference(sym, series[0], prior_close=series[0],
                                                    note=f"session-seg{seg.index}")
        # 4) 跑 tick
        from .. import timebase
        fills_before = len(self.rt.ledger.fills())
        for i in range(seg.ticks):
            if i:
                self.replay_clock.advance(timedelta(minutes=3))
            self.source.set_tick(i)
            now = timebase.now()
            if seg.use_luld:
                for sym, series in seg.prices.items():
                    self.market_status.observe_trade(
                        sym, float(series[min(i, len(series) - 1)]), now,
                        bounds=__import__("ai_trader.market.status", fromlist=["DayBounds"]).DayBounds(
                            open_at=datetime(day.year, day.month, day.day, 9, 30, tzinfo=NY),
                            close_at=datetime(day.year, day.month, day.day, 16, 0, tzinfo=NY)),
                        prior_close=series[0])
            if seg.index_path:
                self.market_status.update_index(
                    float(seg.index_path[min(i, len(seg.index_path) - 1)]), now,
                    bounds=__import__("ai_trader.market.status", fromlist=["DayBounds"]).DayBounds(
                        open_at=datetime(day.year, day.month, day.day, 9, 30, tzinfo=NY),
                        close_at=datetime(day.year, day.month, day.day, 16, 0, tzinfo=NY)))
            self.rt.tick(model_tick=i)

        fills_after = len(self.rt.ledger.fills())
        return {"segment": seg.index, "date": day.isoformat(), "kind": seg.kind,
                "ticks": seg.ticks, "fills_delta": fills_after - fills_before,
                "state": self.rt.machine.state.value,
                "orders": len(self.rt.ctx.service("broker").orders_snapshot()),
                "cash": dict(self.rt.ctx.service("broker")._state.get("cash", {}))}

    def stop(self, reason: str = "session_stop"):
        from .timebase_bridge import restore_clock
        out = self.rt.stop(reason)
        restore_clock()
        return out

    def summary(self) -> Dict[str, Any]:
        broker = self.rt.ctx.service("broker")
        return {"run_id": self.rt.run_id, "segments": self._segments,
                "ticks": self.rt._ticks,
                "fills": len(self.rt.ledger.fills()),
                "state": self.rt.machine.state.value,
                "orders": len(broker.orders_snapshot()) if broker else 0,
                "cash": dict(broker._state.get("cash", {})) if broker else {},
                "health": self.rt.health()["aggregate"],
                "invariants": self.rt._last_invariants.as_dict()}
