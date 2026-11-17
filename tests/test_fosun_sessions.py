"""分时段实时性判定测试（确定性，无任何网络请求）。

重点：结论只依赖**官方对象本身**，不依赖我们对时段边界的假设。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from ai_trader.market.providers import fosun_sessions as S

NY = ZoneInfo("America/New_York")
UTC = timezone.utc


def ny(y, m, d, hh, mm, ss=0, us=0):
    return datetime(y, m, d, hh, mm, ss, us, tzinfo=NY)


def utc(y, m, d, hh, mm, ss=0, us=0):
    return datetime(y, m, d, hh, mm, ss, us, tzinfo=UTC)


# ---------------------------------------------------------------------- #
# 1. 四时段识别
# ---------------------------------------------------------------------- #
@pytest.mark.parametrize("hh,mm,expected", [
    (3, 59, "night"), (4, 0, "pre"), (9, 29, "pre"),
    (9, 30, "regular"), (15, 59, "regular"),
    (16, 0, "post"), (19, 59, "post"),
    (20, 0, "night"), (23, 59, "night"), (0, 0, "night"),
])
def test_phase_recognition(hh, mm, expected):
    assert S.DEFAULT_SPEC.phase_of(ny(2026, 11, 17, hh, mm)) == expected


def test_spec_source_is_labelled_unverified():
    """边界不是官方文档给的，必须自报家门。"""
    assert S.DEFAULT_SPEC.source.startswith("UNVERIFIED")


# ---------------------------------------------------------------------- #
# 2. 跨午夜
# ---------------------------------------------------------------------- #
def test_night_crosses_midnight():
    assert S.DEFAULT_SPEC.phase_of(ny(2026, 11, 17, 23, 30)) == "night"
    assert S.DEFAULT_SPEC.phase_of(ny(2026, 11, 18, 0, 30)) == "night"
    assert S.DEFAULT_SPEC.phase_of(ny(2026, 11, 18, 3, 59)) == "night"
    assert S.DEFAULT_SPEC.phase_of(ny(2026, 11, 18, 4, 0)) == "pre"


# ---------------------------------------------------------------------- #
# 3. 夏令时 / 冬令时
# ---------------------------------------------------------------------- #
def test_dst_spring_forward_boundary():
    """2026-03-08 美东进入夏令时（EST→EDT）。同一墙钟时刻的 UTC 偏移要变。"""
    before = S.phase_at(utc(2026, 3, 6, 14, 30))     # EST: 09:30 NY
    after = S.phase_at(utc(2026, 3, 9, 13, 30))      # EDT: 09:30 NY
    assert before == "regular" and after == "regular"
    # 用 UTC 同一时刻反证偏移确实变了
    assert S.to_new_york(utc(2026, 3, 6, 14, 30)).hour == 9
    assert S.to_new_york(utc(2026, 3, 9, 14, 30)).hour == 10


def test_dst_fall_back_boundary():
    """2026-11-01 美东退出夏令时（EDT→EST）。"""
    before = S.phase_at(utc(2026, 10, 30, 13, 30))   # EDT: 09:30 NY
    after = S.phase_at(utc(2026, 11, 3, 14, 30))     # EST: 09:30 NY
    assert before == "regular" and after == "regular"
    assert S.to_new_york(utc(2026, 10, 30, 13, 30)).hour == 9
    assert S.to_new_york(utc(2026, 11, 3, 14, 30)).hour == 9


def test_naive_datetime_is_rejected():
    with pytest.raises(ValueError):
        S.to_new_york(datetime(2026, 11, 17, 9, 30))


# ---------------------------------------------------------------------- #
# 4. 节假日 / 非交易日
# ---------------------------------------------------------------------- #
def test_weekend_is_not_trading_day():
    assert S.is_trading_day(date(2026, 11, 14)) is False     # 周六
    assert S.is_trading_day(date(2026, 11, 15)) is False


def test_holiday_is_not_trading_day():
    assert S.is_trading_day(date(2026, 11, 26), {date(2026, 11, 26)}) is False


def test_holiday_source_must_be_reported():
    """未提供日历时必须自报"只判了周末"，不能假装知道节假日。"""
    rep = S.summarize("regular", [], trading_day=True)
    assert rep.holiday_source == "weekday_only"


def test_non_trading_day_forces_no_session_data():
    ev = S.evaluate_object("main", present=True, price=352.78,
                           event_time=utc(2026, 11, 26, 15, 0), delay=False,
                           now=utc(2026, 11, 26, 15, 0, 1), continuity=True)
    rep = S.summarize("regular", [ev], trading_day=False)
    assert rep.verdict == S.NO_SESSION_DATA


# ---------------------------------------------------------------------- #
# 5. prepost / nightQuote 独立
# ---------------------------------------------------------------------- #
def test_sub_quotes_are_evaluated_independently():
    now = utc(2026, 11, 17, 9, 0)
    good = S.evaluate_object("nightQuote", present=True, price=351.48,
                             event_time=now - timedelta(seconds=2), delay=False,
                             now=now, continuity=True)
    bad = S.evaluate_object("prepost", present=True, price=353.79,
                            event_time=now - timedelta(seconds=2), delay=True,
                            now=now, continuity=True)
    assert good.verdict == S.REALTIME_CONFIRMED
    assert bad.verdict == S.DELAYED, "子行情各自判定，不互相影响"


def test_main_realtime_does_not_imply_night_realtime():
    now = utc(2026, 11, 17, 6, 0)
    main = S.evaluate_object("main", present=True, price=352.78,
                             event_time=now - timedelta(seconds=1), delay=False,
                             now=now, continuity=True)
    night = S.evaluate_object("nightQuote", present=False)
    rep = S.summarize("night", [main, night])
    assert main.verdict == S.REALTIME_CONFIRMED
    assert rep.verdict == S.NO_SESSION_DATA, "夜盘只认 nightQuote"


# ---------------------------------------------------------------------- #
# 6/7. delay=true / 时间戳过期
# ---------------------------------------------------------------------- #
def test_delay_true_never_passes():
    now = utc(2026, 11, 17, 15, 0)
    ev = S.evaluate_object("main", present=True, price=352.78,
                           event_time=now - timedelta(seconds=1), delay=True,
                           now=now, continuity=True)
    assert ev.verdict == S.DELAYED
    assert S.summarize("regular", [ev]).verdict == S.DELAYED


def test_delay_false_but_stale_does_not_pass():
    now = utc(2026, 11, 17, 15, 0)
    ev = S.evaluate_object("main", present=True, price=352.78,
                           event_time=now - timedelta(minutes=30), delay=False,
                           now=now, continuity=True)
    assert ev.verdict == S.INSUFFICIENT_EVIDENCE
    assert "超过阈值" in ev.reason


def test_delay_flag_missing_is_insufficient_not_false():
    now = utc(2026, 11, 17, 15, 0)
    ev = S.evaluate_object("main", present=True, price=352.78,
                           event_time=now - timedelta(seconds=1), delay=None,
                           now=now, continuity=True)
    assert ev.verdict == S.INSUFFICIENT_EVIDENCE
    assert "缺少 delay" in ev.reason


# ---------------------------------------------------------------------- #
# 连续性：delay=false 不单独等于实时
# ---------------------------------------------------------------------- #
def test_single_sample_is_insufficient():
    now = utc(2026, 11, 17, 15, 0)
    ev = S.evaluate_object("main", present=True, price=352.78,
                           event_time=now - timedelta(seconds=1), delay=False,
                           now=now, continuity=None)
    assert ev.verdict == S.INSUFFICIENT_EVIDENCE
    assert "单次采样" in ev.reason


def test_timestamp_not_advancing_is_insufficient():
    now = utc(2026, 11, 17, 15, 0)
    ev = S.evaluate_object("main", present=True, price=352.78,
                           event_time=now - timedelta(seconds=1), delay=False,
                           now=now, continuity=False)
    assert ev.verdict == S.INSUFFICIENT_EVIDENCE
    assert "未推进" in ev.reason


def test_all_conditions_met_confirms():
    now = utc(2026, 11, 17, 15, 0)
    ev = S.evaluate_object("main", present=True, price=352.78,
                           event_time=now - timedelta(seconds=1), delay=False,
                           now=now, continuity=True)
    assert ev.verdict == S.REALTIME_CONFIRMED


# ---------------------------------------------------------------------- #
# 8. 未知 type 不得被猜测
# ---------------------------------------------------------------------- #
def test_unknown_type_is_preserved_but_not_interpreted():
    now = utc(2026, 11, 17, 1, 0)
    ev = S.evaluate_object("prepost", present=True, price=353.79,
                           event_time=now - timedelta(seconds=1), delay=False,
                           raw_type=99, now=now, continuity=True)
    assert ev.raw_type == 99
    assert ev.session in ("pre", "post", "ambiguous"), "时段只能由时间/字段名定"
    assert "type" not in ev.reason.lower()


# ---------------------------------------------------------------------- #
# 9. 边界冲突 → ambiguous
# ---------------------------------------------------------------------- #
def test_prepost_at_2000_is_ambiguous():
    """真实 fixture 里 prepost.qtTime 正好 20:00:00.000（盘后收盘/夜盘开盘边界）。"""
    t = utc(2026, 11, 17, 1, 0)      # = 2026-11-16 20:00 EST
    assert S.classify_object_session("prepost", t) == "ambiguous"


def test_nightquote_at_0342_is_night():
    t = utc(2026, 11, 17, 8, 42, 9, 181000)
    assert S.classify_object_session("nightQuote", t) == "night"


def test_ambiguous_primary_blocks_confirmation():
    now = utc(2026, 11, 17, 1, 0, 2)
    ev = S.evaluate_object("prepost", present=True, price=353.79,
                           event_time=utc(2026, 11, 17, 1, 0), delay=False,
                           now=now, continuity=True)
    rep = S.summarize("post", [ev])
    assert rep.verdict == S.AMBIGUOUS


# ---------------------------------------------------------------------- #
# 10. 子行情不覆盖主报价（在 summarize 层不串用）
# ---------------------------------------------------------------------- #
def test_summarize_uses_primary_object_only():
    now = utc(2026, 11, 17, 6, 0)
    main = S.evaluate_object("main", present=True, price=352.78,
                             event_time=now - timedelta(seconds=1), delay=False,
                             now=now, continuity=True)
    night = S.evaluate_object("nightQuote", present=True, price=351.48,
                              event_time=now - timedelta(seconds=1), delay=True,
                              now=now, continuity=True)
    # 夜盘时段的结论必须是 nightQuote 的 DELAYED，而不是主报价的 CONFIRMED
    assert S.summarize("night", [main, night]).verdict == S.DELAYED


# ---------------------------------------------------------------------- #
# 11. API 无该时段数据
# ---------------------------------------------------------------------- #
def test_missing_object_reports_no_session_data():
    ev = S.evaluate_object("nightQuote", present=False)
    assert ev.verdict == S.NO_SESSION_DATA
    assert S.summarize("night", [ev]).verdict == S.NO_SESSION_DATA


# ---------------------------------------------------------------------- #
# 12. 请求预算
# ---------------------------------------------------------------------- #
def test_request_budget_caps_at_two():
    b = S.RequestBudget(max_requests=2, min_interval_seconds=5)
    t0 = utc(2026, 11, 17, 15, 0)
    b.acquire(t0)
    with pytest.raises(RuntimeError, match="间隔"):
        b.acquire(t0 + timedelta(seconds=1))
    b.acquire(t0 + timedelta(seconds=6))
    assert b.used == 2 and b.remaining == 0
    with pytest.raises(RuntimeError, match="预算"):
        b.acquire(t0 + timedelta(seconds=20))


# ---------------------------------------------------------------------- #
# 额外：报告结构
# ---------------------------------------------------------------------- #
def test_report_is_serializable():
    now = utc(2026, 11, 17, 15, 0)
    ev = S.evaluate_object("main", present=True, price=1.0,
                           event_time=now, delay=False, now=now, continuity=True)
    d = S.summarize("regular", [ev]).as_dict()
    import json
    json.dumps(d)          # 不抛异常即可
    assert d["verdict"] == S.REALTIME_CONFIRMED
    assert d["objects"][0]["key"] == "main"
