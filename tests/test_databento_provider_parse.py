"""DatabentoProvider 对**官方字段形状**的解析验证。

样本来自 ``verification/databento/fixtures``，由 ``gen_fixtures.py`` 按官方
字段表逐字段构造（不是真实抓包 —— 本轮没有凭据，见该目录 README）。

重点不是"能解析出来"，而是：
* 字段名/类型与官方一致（``rtype`` 而不是 ``type``；``action`` 是整数）；
* 缺失字段（``symbol`` / ``sequence``）时行为正确；
* 价格标度不会被二次缩放；
* 语义不确定时**保守方向**处理，而不是丢掉事件。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ai_trader.market.providers import DatabentoProvider
from ai_trader.market.providers import databento_schema as ds
from ai_trader.market.providers.models import (
    HaltEvent,
    QualityFlag,
    QuoteEvent,
    TradeEvent,
)

VER = Path(__file__).resolve().parents[1] / "verification" / "databento" / "fixtures"
SYM = json.loads((VER / "symbology.json").read_text(encoding="utf-8"))


def prov(name: str, **cfg):
    base = {"batch_size": 100, "symbology": SYM}
    base.update(cfg)
    p = DatabentoProvider(VER / name, base)
    p.connect()
    p.subscribe(["AAPL"])
    return p


def events(name: str, **cfg):
    return prov(name, **cfg).poll()


def one_status(name: str = "synthetic_news_halt.jsonl"):
    return [e for e in events(name) if isinstance(e, HaltEvent)]


# ---------------------------------------------------------------------- #
# 记录类型分派
# ---------------------------------------------------------------------- #
def test_rtype_numeric_dispatch():
    """真实数据用整数 rtype：18=status、0=trades。"""
    evs = events("synthetic_luld_pause.jsonl")
    kinds = [type(e).__name__ for e in evs]
    assert kinds.count("TradeEvent") == 4
    assert kinds.count("HaltEvent") == 2


def test_legacy_string_type_still_accepted():
    """兼容只给 ``type``/``schema`` 字符串的输入（不因此拒绝旧样本）。"""
    p = DatabentoProvider(None, {"symbology": SYM})
    ev = p.parse({"type": "trades", "symbol": "AAPL", "publisher_id": 2,
                  "ts_event": 1794839400000000000, "ts_recv": 1794839400000100000,
                  "price": 200_000_000_000, "size": 1, "sequence": 5})
    assert isinstance(ev, TradeEvent)


def test_unknown_rtype_is_rejected_with_reason():
    p = DatabentoProvider(VER / "bad" / "unknown_rtype.jsonl", {"batch_size": 5})
    p.connect(); p.subscribe(["AAPL"])
    assert p.poll() == []
    assert "schema_unsupported:99" in p.rejections()
    assert p.health.dropped == 1


# ---------------------------------------------------------------------- #
# 标的解析（symbol 缺失是真实默认形态）
# ---------------------------------------------------------------------- #
def test_symbol_field_used_when_present():
    evs = events("synthetic_luld_pause.jsonl")
    assert {e.symbol for e in evs} == {"AAPL"}


def test_symbol_resolved_from_symbology_map():
    """官方记录只有 instrument_id；靠 symbology 还原。"""
    evs = events("synthetic_luld_pause_nosym.jsonl")
    assert len(evs) == 6
    assert {e.symbol for e in evs} == {"AAPL"}
    assert all(e.source_event_id == "" for e in evs)


def test_missing_symbology_fails_closed_and_is_counted():
    p = DatabentoProvider(VER / "synthetic_luld_pause_nosym.jsonl",
                          {"batch_size": 100})   # 故意不给映射
    p.connect(); p.subscribe(["AAPL"])
    assert p.poll() == []
    assert p.health.dropped == 6
    assert p.rejections().get("missing_symbology") == 6, \
        "丢弃必须按原因计数，否则故障会表现成'供应商没数据'"


def test_bad_sample_no_symbol_no_map():
    p = DatabentoProvider(VER / "bad" / "no_symbol_no_map.jsonl", {"batch_size": 5})
    p.connect(); p.subscribe(["AAPL"])
    assert p.poll() == []
    assert 1 == p.health.dropped


# ---------------------------------------------------------------------- #
# 价格标度
# ---------------------------------------------------------------------- #
def test_fixed_point_price_divided_once():
    tr = [e for e in events("synthetic_luld_pause.jsonl") if isinstance(e, TradeEvent)]
    assert tr[0].price == pytest.approx(201.20, abs=1e-9)
    assert tr[0].price_scale == "fixed_1e-9"


def test_pretty_price_not_divided_again():
    """``pretty_px=True`` 的 float 价不能再除 1e9（会差 9 个数量级）。"""
    tr = [e for e in events("synthetic_pretty.jsonl") if isinstance(e, TradeEvent)]
    assert tr[0].price == pytest.approx(201.20, abs=1e-9)
    assert tr[0].price_scale == "float"


def test_undef_price_rejected():
    p = DatabentoProvider(VER / "bad" / "undef_price.jsonl", {"batch_size": 5})
    p.connect(); p.subscribe(["AAPL"])
    assert p.poll() == []
    assert p.rejections().get("price_undefined") == 1


# ---------------------------------------------------------------------- #
# 时间
# ---------------------------------------------------------------------- #
def test_event_time_and_receive_time_are_distinct():
    """``ts_event``（撮合引擎）与 ``ts_recv``（抓包服务器）必须分开保留。"""
    tr = [e for e in events("synthetic_luld_pause.jsonl") if isinstance(e, TradeEvent)]
    e = tr[0]
    assert e.event_time == datetime(2026, 11, 16, 14, 30, 0, tzinfo=timezone.utc)
    assert (e.receive_time - e.event_time).total_seconds() == pytest.approx(0.00012,
                                                                          abs=1e-6)
    assert e.receive_time > e.event_time


def test_nanosecond_precision_preserved():
    """纳秒精度不能在转 datetime 时被抹成整秒。"""
    p = DatabentoProvider(None, {"symbology": SYM})
    ev = p.parse({"rtype": 0, "symbol": "AAPL", "publisher_id": 2,
                  "ts_event": 1794839400123456789, "ts_recv": 1794839400123456789,
                  "price": 100_000_000_000, "size": 1, "sequence": 1})
    assert ev.event_time.microsecond == 123456


def test_naive_timestamp_is_flagged_not_guessed():
    p = DatabentoProvider(VER / "bad" / "naive_ts.jsonl", {"batch_size": 5})
    p.connect(); p.subscribe(["AAPL"])
    evs = p.poll()
    assert evs and QualityFlag.NAIVE_TIME in evs[0].quality.flags


def test_missing_ts_recv_falls_back_and_is_counted():
    p = DatabentoProvider(None, {"symbology": SYM})
    ev = p.parse({"rtype": 0, "symbol": "AAPL", "publisher_id": 2,
                  "ts_event": 1794839400000000000, "price": 100_000_000_000,
                  "size": 1, "sequence": 1})
    assert ev.receive_time == ev.event_time
    assert p.rejections().get("missing_ts_recv") == 1


# ---------------------------------------------------------------------- #
# status 语义
# ---------------------------------------------------------------------- #
def test_pause_maps_to_halted():
    """action=9(PAUSE) + reason=50(LULD_PAUSE) → 停牌。"""
    halts = one_status("synthetic_luld_pause.jsonl")
    first = halts[0]
    assert first.halted is True
    assert first.halt_code == "PAUSE"
    assert first.reason == "LULD_PAUSE"
    assert first.is_trading is False
    assert first.is_quoting is True
    assert first.ssr_restricted is None, "'~' 是未知，不是 False"


def test_trading_maps_to_not_halted():
    halts = one_status("synthetic_luld_pause.jsonl")
    assert halts[1].halted is False and halts[1].halt_code == "TRADING"


def test_halt_action_maps_to_halted():
    halts = one_status("synthetic_news_halt.jsonl")
    assert halts[0].halted is True and halts[0].halt_code == "HALT"
    assert halts[0].reason == "NEWS_PENDING"


@pytest.mark.parametrize("action,code", [(8, "HALT"), (9, "PAUSE"), (10, "SUSPEND")])
def test_all_halting_actions(action, code):
    p = DatabentoProvider(None, {"symbology": SYM})
    ev = p.parse({"rtype": 18, "symbol": "AAPL", "publisher_id": 2,
                  "ts_event": 1794839400000000000, "ts_recv": 1794839400000000000,
                  "action": action, "reason": 0})
    assert isinstance(ev, HaltEvent) and ev.halted is True and ev.halt_code == code


def test_ambiguous_status_halts_but_stays_usable():
    """``NOT_AVAILABLE_FOR_TRADING`` 无法区分收盘/停牌 → 保守判不可交易。

    关键：它必须**仍可被摄取**。如果因为"语义不确定"就丢弃，
    标的会停在"可交易"的旧状态，那是 fail-open。
    """
    halts = one_status("synthetic_ambiguous_status.jsonl")
    assert halts[0].halted is True
    assert QualityFlag.AMBIGUOUS_STATUS in halts[0].quality.flags
    assert halts[0].quality.usable is True


def test_non_halt_status_does_not_create_fake_halt():
    """QUOTING / CLOSE / SSR_CHANGE 不是停牌事件。"""
    p = prov("synthetic_non_halt_status.jsonl")
    assert p.poll() == []
    assert p.ignored_status == {"QUOTING": 1, "CLOSE": 1, "SSR_CHANGE": 1}
    assert p.health.dropped == 0, "这不是数据丢弃，不该计入 dropped"


def test_unknown_action_forces_halt_instead_of_dropping():
    p = DatabentoProvider(VER / "bad" / "unknown_action.jsonl", {"batch_size": 5})
    p.connect(); p.subscribe(["AAPL"])
    evs = p.poll()
    assert len(evs) == 1, "未知 action 被丢弃 → 沿用旧状态 = 可能放行交易"
    assert isinstance(evs[0], HaltEvent) and evs[0].halted is True
    assert QualityFlag.UNKNOWN_ENUM in evs[0].quality.flags
    assert evs[0].quality.usable is True


def test_unknown_reason_does_not_suppress_known_halt():
    p = DatabentoProvider(None, {"symbology": SYM})
    ev = p.parse({"rtype": 18, "symbol": "AAPL", "publisher_id": 2,
                  "ts_event": 1794839400000000000, "ts_recv": 1794839400000000000,
                  "action": 8, "reason": 777})
    assert isinstance(ev, HaltEvent) and ev.halted is True
    assert QualityFlag.UNKNOWN_ENUM in ev.quality.flags
    assert ev.quality.usable is True


def test_mwcb_reason_is_flagged():
    halts = one_status("synthetic_mwcb_halt.jsonl")
    assert halts[0].reason == "MARKET_WIDE_HALT_LEVEL1"
    assert QualityFlag.AMBIGUOUS_STATUS in halts[0].quality.flags


# ---------------------------------------------------------------------- #
# 交易所与序号
# ---------------------------------------------------------------------- #
def test_publisher_id_maps_to_exchange():
    evs = events("synthetic_luld_pause.jsonl")
    assert {e.exchange for e in evs} == {"nasdaq"}


def test_unknown_publisher_counted_not_guessed():
    p = DatabentoProvider(None, {"symbology": SYM})
    ev = p.parse({"rtype": 0, "symbol": "AAPL", "publisher_id": 9999,
                  "ts_event": 1794839400000000000, "ts_recv": 1794839400000000000,
                  "price": 100_000_000_000, "size": 1, "sequence": 1})
    assert ev.exchange == "unknown"
    assert p.rejections().get("unknown_publisher") == 1


def test_consolidated_publisher_is_counted():
    p = DatabentoProvider(None, {"symbology": SYM})
    ev = p.parse({"rtype": 0, "symbol": "AAPL", "publisher_id": 47,
                  "ts_event": 1794839400000000000, "ts_recv": 1794839400000000000,
                  "price": 100_000_000_000, "size": 1, "sequence": 1})
    assert ev is not None
    assert p.rejections().get("consolidated_publisher") == 1


def test_status_has_no_sequence_but_trades_do():
    halts = one_status("synthetic_luld_pause.jsonl")
    tr = [e for e in events("synthetic_luld_pause.jsonl") if isinstance(e, TradeEvent)]
    assert all(h.sequence == 0 for h in halts), "status 记录本来就没有 sequence"
    assert [t.sequence for t in tr] == [1001, 1002, 1003, 1004]


def test_trade_side_preserved():
    tr = [e for e in events("synthetic_luld_pause.jsonl") if isinstance(e, TradeEvent)]
    assert [t.side for t in tr] == ["B", "A", "A", "B"]


# ---------------------------------------------------------------------- #
# 其他 schema
# ---------------------------------------------------------------------- #
def test_mbp1_reads_levels_array():
    """MBP-1 的买卖价在 ``levels[0]`` 里，不在顶层 ``bid_px``。"""
    p = DatabentoProvider(None, {"symbology": SYM})
    ev = p.parse({"rtype": 1, "symbol": "AAPL", "publisher_id": 2,
                  "ts_event": 1794839400000000000, "ts_recv": 1794839400000000000,
                  "levels": [{"bid_px": 100_100_000_000, "ask_px": 100_200_000_000,
                              "bid_sz": 3, "ask_sz": 4}],
                  "sequence": 9})
    assert isinstance(ev, QuoteEvent)
    assert ev.bid == pytest.approx(100.1) and ev.ask == pytest.approx(100.2)
    assert (ev.bid_size, ev.ask_size) == (3.0, 4.0)


def test_unsupported_schema_is_rejected_with_name():
    p = DatabentoProvider(None, {"symbology": SYM})
    assert p.parse({"rtype": 10, "symbol": "AAPL", "publisher_id": 2,
                    "ts_event": 1794839400000000000}) is None
    assert "schema_unsupported:mbp-10" in p.rejections()


# ---------------------------------------------------------------------- #
# 确定性
# ---------------------------------------------------------------------- #
def test_same_fixture_yields_identical_events():
    a = [e.as_dict() for e in events("synthetic_luld_pause.jsonl")]
    b = [e.as_dict() for e in events("synthetic_luld_pause.jsonl")]
    assert a == b


def test_price_does_not_depend_on_run_count():
    prices = []
    for _ in range(3):
        p = prov("synthetic_luld_pause.jsonl")
        prices.append([e.price for e in p.poll() if isinstance(e, TradeEvent)])
    assert prices[0] == prices[1] == prices[2] == [201.2, 201.15, 200.9, 201.0]
