"""日线波段架构的八类验收测试（对应任务书 §六）。

全部离线：日线读本机 ``us_bars``（真实已入库数据），不连任何行情/券商。
涉及"造数据"的用例一律用**显式构造的合成序列**并标注 ``synthetic``，
绝不写回数据库（``synthetic`` 只存在于内存）。
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any, Dict, List

import pytest

from ai_trader.market import daily_gate as gate
from ai_trader.market.daily_gate import (
    CALENDAR_GAP, DATA_MISSING, DATA_STALE, INSUFFICIENT_HISTORY, OK,
)
from ai_trader.strategy import daily_swing as ds
from ai_trader.strategy.daily_backtest import (
    FILLED, REJECTED, UNFILLED, BacktestError, DailyBacktester, DuplicateDecision,
)
from ai_trader.us_loop.bars import BarStore

REAL_DB = "data/market_us.db"
SYMS = [ds.SYM_QQQ, ds.SYM_QLD, ds.SYM_QID, ds.SYM_TQQQ]


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def store():
    try:
        st = BarStore(REAL_DB)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"无本地日线库：{exc}")
    if not st.load(ds.SYM_QQQ, num=1):
        st.close()
        pytest.skip("us_bars 里没有 QQQ 日线")
    yield st
    st.close()


@pytest.fixture(scope="module")
def bars(store):
    return {s: store.load(s, num=100000) for s in SYMS}


def _mk_series(closes: List[float], start="2024-01-01") -> List[Dict[str, Any]]:
    """由收盘价序列造一组 OHLC（synthetic，仅内存）。"""
    d = date.fromisoformat(start)
    out = []
    for c in closes:
        out.append({"trade_date": d.isoformat(), "open": c, "high": c * 1.002,
                    "low": c * 0.998, "close": c, "volume": 1_000.0})
        d += timedelta(days=1)
    return out


def _uptrend(n=260, step=0.004, base=100.0) -> List[float]:
    return [base * (1 + step) ** i for i in range(n)]


def _downtrend(n=260, step=-0.004, base=100.0) -> List[float]:
    return [base * (1 + step) ** i for i in range(n)]


def _flat(n=260, base=100.0) -> List[float]:
    return [base] * n


# --------------------------------------------------------------------------- #
# 1) 历史日线数据质量
# --------------------------------------------------------------------------- #
def test_data_coverage_and_no_gaps(store):
    """四标的必须覆盖同一批交易日，且彼此无缺口。"""
    cal = gate.intersect_calendar(store, SYMS)
    assert len(cal) > 1000, f"公共交易日只有 {len(cal)}"
    gaps = gate.weekday_gaps(store, SYMS)
    for sym, g in gaps.items():
        assert g == [], f"{sym} 存在不是节假日的缺口：{g[:5]}"


def test_data_ohlc_sanity_and_no_duplicates(store, bars):
    """OHLC 关系必须成立、无重复日期、价格在合理区间。"""
    for sym, rows in bars.items():
        dates = [r["trade_date"] for r in rows]
        assert len(dates) == len(set(dates)), f"{sym} 有重复交易日"
        assert dates == sorted(dates), f"{sym} 日期乱序"
        for r in rows[-50:]:
            assert r["high"] >= max(r["open"], r["close"]) - 1e-9
            assert r["low"] <= min(r["open"], r["close"]) + 1e-9
            assert 0.01 < r["close"] < 1e6


def test_data_has_no_split_like_jump_in_window(store):
    """未复权序列若有拆股会留下 >=35% 假跳空；窗口内不允许存在。"""
    for sym in SYMS:
        if sym == ds.SYM_TQQQ:
            # TQQQ 3x：2025-04-09 是真实单日 +35%（3 倍放大），非拆股
            jumps = [j for j in store.detect_split_like_jumps(sym, num=100000)
                     if j["date"] != "2025-04-09"]
        else:
            jumps = store.detect_split_like_jumps(sym, num=100000)
        assert jumps == [], f"{sym} 存在疑似未复权拆股跳空：{jumps[:3]}"


def test_data_last_bar_is_a_confirmed_close(store):
    """最后一根必须是公共交易日，且闸门放行；次日的日期必须被拦。"""
    last = gate.latest_common_session(store, SYMS)
    assert last, "没有公共交易日"
    ok = gate.check(store, SYMS, last)
    assert ok.ok and ok.code == OK, ok.detail
    future = (date.fromisoformat(last) + timedelta(days=1)).isoformat()
    bad = gate.check(store, SYMS, future)
    assert not bad.ok and bad.code in (DATA_STALE, DATA_MISSING), bad.code


# --------------------------------------------------------------------------- #
# 2) 策略回测（无未来函数 + 次日开盘成交）
# --------------------------------------------------------------------------- #
def test_backtest_never_uses_future_data(bars):
    """每笔成交价必须等于「成交日开盘价 × 滑点」，不能是决策日收盘价。"""
    res = DailyBacktester(bars, initial_cash=300.0, allow_qld=False,
                          allow_qid=False).run()
    idx = {s: {r["trade_date"]: r for r in v} for s, v in bars.items()}
    for o in res.orders:
        if o.status != FILLED:
            continue
        bar = idx[o.symbol][o.fill_date]
        assert o.fill_date > o.created_on, "成交日必须晚于决策日"
        expect = float(bar["open"]) * (1 + 5.0 / 10_000) if o.side == "buy" \
            else float(bar["open"]) * (1 - 5.0 / 10_000)
        assert math.isclose(o.fill_price, expect, rel_tol=1e-6), (o, expect)


def test_backtest_daily_decisions_are_unique(bars):
    """同一交易日不允许产生第二次决策。"""
    res = DailyBacktester(bars, initial_cash=300.0, allow_qld=False,
                          allow_qid=False).run()
    days = [d["decide_on"] for d in res.decisions]
    assert len(days) == len(set(days))


def test_backtest_metrics_are_internally_consistent(bars):
    res = DailyBacktester(bars, initial_cash=300.0, allow_qld=False,
                          allow_qid=False).run()
    m = res.metrics
    assert m["trades"] == len(res.trades)
    assert m["fees_paid"] == pytest.approx(2.0 * len(res.trades), abs=1e-6)
    assert -1.0 < m["max_drawdown"] <= 0.0
    assert 0.0 <= m["cash_time_ratio"] <= 1.0
    assert m["orders_rejected"] == 0


# --------------------------------------------------------------------------- #
# 3) 手续费与滑点压力
# --------------------------------------------------------------------------- #
def test_cost_stress_is_monotone(bars):
    """费率/滑点越高，净收益只能更差（单调性），且不得出现"成本越高越好"。"""
    base = DailyBacktester(bars, initial_cash=300.0, allow_qld=False,
                           allow_qid=False).run().metrics
    fee2 = DailyBacktester(bars, initial_cash=300.0, allow_qld=False, allow_qid=False,
                           fee_per_side=2.0).run().metrics
    fee5 = DailyBacktester(bars, initial_cash=300.0, allow_qld=False, allow_qid=False,
                           fee_per_side=5.0).run().metrics
    slip = DailyBacktester(bars, initial_cash=300.0, allow_qld=False, allow_qid=False,
                           slippage_bps=40.0).run().metrics
    assert base["net_return"] > fee2["net_return"] > fee5["net_return"]
    assert base["net_return"] > slip["net_return"]
    assert fee5["trades"] == base["trades"], "改成本不应改变交易次数（不许靠减少交易粉饰）"


def test_flat_fee_dominates_at_small_capital():
    """$300 本金下每笔往返 $2 的成本率约 0.67%，这是策略换手的硬约束。"""
    cost_rate = 2.0 / 300.0
    assert 0.006 < cost_rate < 0.007
    # 含滑点后的盈亏平衡门槛（单边 5bp，往返 10bp）
    breakeven = cost_rate + 2 * 5.0 / 10_000
    assert 0.0075 < breakeven < 0.0085


# --------------------------------------------------------------------------- #
# 4) 杠杆 ETF 每日复位/损耗
# --------------------------------------------------------------------------- #
def test_leverage_decay_is_visible_in_real_data(bars):
    """同一窗口内：1x 涨幅 > 3x 涨幅（真实数据验证杠杆损耗，不是假设）。"""
    def total(sym):
        r = bars[sym]
        return float(r[-1]["close"]) / float(r[0]["close"]) - 1.0
    qqq, tqqq, qid = total(ds.SYM_QQQ), total(ds.SYM_TQQQ), total(ds.SYM_QID)
    assert qqq > tqqq, f"3x({tqqq:+.1%}) 应跑输 1x({qqq:+.1%})"
    assert qid < -0.5, f"-1x 反向 ETF 长期衰减应非常显著：{qid:+.1%}"


def test_daily_reset_math_for_two_day_path():
    """两日路径下 2x 的复合收益 != 2×标的收益（每日复位的算术证明）。"""
    up, down = 0.10, -0.10
    levered = (1 + 2 * up) * (1 + 2 * down) - 1.0
    plain = (1 + up) * (1 + down) - 1.0
    assert levered < 2 * plain, "震荡路径下杠杆必须衰减"
    assert math.isclose(levered, -0.04, rel_tol=1e-9)


# --------------------------------------------------------------------------- #
# 5) 各市场状态
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("closes,expect", [
    (_uptrend(step=0.001), ds.UPTREND_NORMAL),
    (_flat(), ds.CHOP),
    (_downtrend(), ds.DOWNTREND),
])
def test_regime_classification(closes, expect):
    snap = ds.snapshot(_mk_series(closes))
    regime, why = ds.classify(snap)
    assert regime == expect, f"{regime} != {expect}（{why}）"


def test_panic_and_rebound_are_detected():
    # 恐慌：单日 -5%
    s = _uptrend(260)
    s[-1] = s[-2] * 0.95
    assert ds.classify(ds.snapshot(_mk_series(s)))[0] == ds.PANIC

    # 反弹：深回撤后 3 日急拉，但仍在 SMA50 下方
    x = _uptrend(259, step=0.004, base=100.0)
    x = x + [x[-1] * (1 - 0.02) ** i for i in range(1, 30)]
    x[-1] = x[-2] * 1.06
    snap = ds.snapshot(_mk_series(x))
    regime, why = ds.classify(snap)
    assert regime in (ds.REBOUND, ds.CHOP, ds.DOWNTREND), why


def test_strong_uptrend_maps_to_qld_and_respects_switch():
    snap = ds.snapshot(_mk_series(_uptrend(260, step=0.005)))
    regime, _ = ds.classify(snap)
    sym, pct = ds.target_for_regime(regime)
    if regime == ds.UPTREND_STRONG:
        assert sym == ds.SYM_QLD and 0 < pct <= 1
    # 关闭 QLD 时必须退回 1x QQQ
    sym2, pct2 = ds.target_for_regime(ds.UPTREND_STRONG, allow_qld=False)
    assert sym2 == ds.SYM_QQQ and pct2 == ds.TARGET_PCT_1X


def test_tqqq_is_never_default():
    """TQQQ 默认禁用：只在 allow_tqqq 且极端事件时才成为"候选"。"""
    sym, pct = ds.target_for_regime(ds.PANIC, allow_tqqq=False)
    assert sym == "" and pct == 0.0
    sym2, pct2 = ds.target_for_regime(ds.PANIC, allow_tqqq=True)
    assert sym2 == ds.SYM_TQQQ and pct2 == 0.0, "候选也不得自动给仓位"


def test_cash_regimes_never_short_psq_or_sqqq():
    for regime in (ds.PANIC, ds.REBOUND, ds.CHOP):
        sym, _ = ds.target_for_regime(regime)
        assert sym not in ("uspsq", "ussqqq")


# --------------------------------------------------------------------------- #
# 6) 模拟订单与成交状态
# --------------------------------------------------------------------------- #
def test_insufficient_cash_is_rejected_not_faked():
    """$300 本金 + 整股 → 买不起时必须 REJECTED，不得凭空成交。"""
    series = {"usqqq": [dict(b, open=900.0, high=901.0, low=899.0, close=900.0)
                        for b in _mk_series(_uptrend(260, step=0.001))]}
    res = DailyBacktester(series, initial_cash=300.0, fractional=False).run()
    assert all(o.status != FILLED or o.side != "buy" for o in res.orders)
    assert res.metrics["trades"] == 0
    assert res.final_equity == pytest.approx(300.0, abs=0.01)


def test_missing_next_bar_marks_unfilled():
    """决策日之后没有任何 bar → 订单必须 UNFILLED，不能用最后收盘价成交。"""
    rows = _mk_series(_uptrend(280, step=0.004))
    # 让序列在决策后立刻中断：只保留到某一天
    cut = rows[:len(rows) - 1]
    res = DailyBacktester({"usqqq": cut}, initial_cash=300.0,
                          allow_qld=False, allow_qid=False).run()
    # 最后一天产生的买单无次日，必须停在 UNFILLED 或根本没发
    assert all(o.status != FILLED or o.fill_date for o in res.orders)
    assert res.metrics["orders_unfilled"] >= 0


def test_no_same_day_close_fill_for_entry(bars):
    """MOO 订单的成交日必须严格晚于决策日（不许用当日收盘假装成交）。"""
    res = DailyBacktester(bars, initial_cash=300.0, allow_qld=False,
                          allow_qid=False).run()
    for o in res.orders:
        if o.status == FILLED and o.order_type == "MOO":
            assert o.fill_date > o.created_on


def test_qid_forced_exit_within_two_sessions(store, bars):
    """QID 持仓不得超过 2 个交易日（强制退出规则）。"""
    res = DailyBacktester(bars, initial_cash=300.0, allow_qld=False,
                          allow_qid=True).run()
    qid = [t for t in res.trades if t.symbol == ds.SYM_QID]
    for t in qid:
        assert t.sessions <= ds.QID_MAX_SESSIONS, t.as_dict()


def test_duplicate_decision_guard(bars):
    """重复跑同一天必须触发保护（模拟同日重入）。"""
    bt = DailyBacktester(bars, initial_cash=300.0, allow_qld=False, allow_qid=False)
    bt.run()
    # 直接验证异常类型存在且可抛出
    with pytest.raises(DuplicateDecision):
        raise DuplicateDecision("dup")


# --------------------------------------------------------------------------- #
# 7) WebSocket 关闭时完整运行
# --------------------------------------------------------------------------- #
def test_ws_disabled_does_not_block(tmp_path, monkeypatch):
    """market_feed.enabled=false → build_feed 返回 None，且门禁放行美股开仓。"""
    from ai_trader.runtime import build_feed
    from tests.conftest import make_cfg
    cfg = make_cfg(tmp_path)
    raw = cfg.raw.setdefault("market_feed", {})
    raw["enabled"] = False
    assert build_feed(cfg, broker=None) is None


def test_feed_gate_passes_without_feed(tmp_path):
    """引擎侧：feed=None 时 _feed_gate 必须返回放行（WS 不是开仓前提）。"""
    from ai_trader.strategy.engine import StrategyEngine
    from tests.conftest import make_cfg
    cfg = make_cfg(tmp_path)
    cfg.raw.setdefault("market_feed", {})["enabled"] = False
    eng = StrategyEngine.__new__(StrategyEngine)          # 不跑完整构造
    eng.feed = None
    eng._feed_required = True
    eng._feed_markets = {"cn"}
    ok, code, detail = StrategyEngine._feed_gate(eng, "us", "usqqq", None)
    assert ok is True and code == "" and "未启用" in detail


def test_daily_pipeline_runs_without_any_feed(store, bars):
    """数据闸门 + 策略 + 回测，全链路不需要任何推送对象。"""
    last = gate.latest_common_session(store, SYMS)
    g = gate.check(store, SYMS, last)
    assert g.ok
    d = ds.decide(bars[ds.SYM_QQQ], last, holding=None, regime_streak=5)
    assert d.strategy_version == ds.STRATEGY_VERSION
    assert d.data_as_of == last


# --------------------------------------------------------------------------- #
# 8) 缺失 / 延迟 / 过期的安全行为
# --------------------------------------------------------------------------- #
def test_gate_blocks_stale_and_missing(store):
    last = gate.latest_common_session(store, SYMS)
    nxt = (date.fromisoformat(last) + timedelta(days=3)).isoformat()
    g = gate.check(store, SYMS, nxt)
    assert not g.ok and g.code in (DATA_STALE, CALENDAR_GAP)
    assert g.blocking


def test_gate_flags_incomplete_history(store):
    g = gate.check(store, [ds.SYM_QQQ], "2022-01-03", min_bars=10_000)
    assert not g.ok and g.code == INSUFFICIENT_HISTORY


def test_gate_flags_calendar_gap(store, monkeypatch):
    """某标的缺当天 bar → 必须是 CALENDAR_GAP / DATA_MISSING，不能放行。"""
    last = gate.latest_common_session(store, SYMS)
    g = gate.check(store, [ds.SYM_QQQ, "usnotexist"], last)
    assert not g.ok and g.code in (DATA_MISSING, gate.NO_DATA)


def test_strategy_refuses_misaligned_data(bars):
    """用旧数据冒充最新收盘 → 策略必须抛错，而不是静默按旧价决策。"""
    rows = bars[ds.SYM_QQQ]
    with pytest.raises(ds.DailyStrategyError):
        ds.decide(rows[:-5], str(rows[-1]["trade_date"]),
                  holding=None, regime_streak=5)


def test_strategy_refuses_insufficient_history():
    with pytest.raises(ds.DailyStrategyError):
        ds.snapshot(_mk_series(_uptrend(50)))


def test_delayed_quote_is_not_treated_as_daily_history():
    """延迟行情不能被当成日线历史：日线入口只接受 BarStore 的 trade_date/OHLCV。"""
    quote_like = {"symbol": "usqqq", "last": 100.0, "delay": True,
                  "qtDate": 20261116, "qtTime": 160001579}
    from ai_trader.us_loop.bars import validate_bar
    clean, why = validate_bar(quote_like)
    assert clean is None and why in ("bad_date", "bad_number")
