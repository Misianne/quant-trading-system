"""供应商适配器：按官方文档字段形状解析。"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ai_trader.market.providers import (
    AlpacaProvider,
    DatabentoProvider,
    EventAuthority,
    HaltEvent,
    LULDState,
    MassiveProvider,
    NasdaqItchProvider,
    ProviderError,
    QuoteEvent,
    TradeEvent,
)

T0 = datetime(2026, 11, 16, 14, 30, tzinfo=timezone.utc)
FIX = Path(__file__).resolve().parent / "fixtures" / "marketdata"


# ---------------------------------------------------------------------- #
# Massive
# ---------------------------------------------------------------------- #
def test_massive_capabilities_are_honest():
    caps = MassiveProvider().capabilities()
    assert caps.sip_consolidated is True
    assert caps.luld_events is True
    assert caps.luld_events_native is False, "LULD 通道不是交易所原生消息"
    assert caps.halt_events_nasdaq_listed_only is True
    assert caps.mwcb_events is False, "未提供 MWCB 事件，不得声明"


def test_massive_luld_bands_parsed():
    p = MassiveProvider(FIX / "massive_luld.jsonl", {"batch_size": 10})
    p.connect(); p.subscribe(["AAPL"])
    evs = p.poll()
    luld = [e for e in evs if isinstance(e, LULDState)]
    assert luld and luld[0].upper == 205.50 and luld[0].lower == 195.10
    assert luld[0].exchange == "nasdaq"      # tape 3
    assert luld[0].authority is EventAuthority.LULD_OFFICIAL


def test_massive_luld_pause_and_resume_indicators():
    p = MassiveProvider(FIX / "massive_luld.jsonl", {"batch_size": 10})
    p.connect(); p.subscribe(["AAPL"])
    evs = [e for e in p.poll() if isinstance(e, LULDState)]
    assert any(e.paused for e in evs), "17 应为暂停"
    assert any(e.resumed for e in evs), "18 应为恢复"


def test_massive_trade_and_quote():
    p = MassiveProvider(FIX / "massive_luld.jsonl", {"batch_size": 10})
    p.connect(); p.subscribe(["AAPL"])
    evs = p.poll()
    tr = [e for e in evs if isinstance(e, TradeEvent)]
    qt = [e for e in evs if isinstance(e, QuoteEvent)]
    assert tr and tr[0].price == 201.20
    assert qt and qt[0].bid == 201.10 and qt[0].ask == 201.30


def test_massive_sequence_preserved():
    p = MassiveProvider(FIX / "massive_luld.jsonl", {"batch_size": 10})
    p.connect(); p.subscribe(["AAPL"])
    evs = p.poll()
    assert [e.sequence for e in evs][:3] == [1001, 1002, 1003]


def test_massive_nanosecond_timestamp():
    p = MassiveProvider(FIX / "massive_luld.jsonl", {"batch_size": 10})
    p.connect(); p.subscribe(["AAPL"])
    ev = p.poll()[0]
    assert abs((ev.event_time - T0).total_seconds()) < 1e-6


def test_massive_time_unit_is_explicit():
    """单位必须由配置给出；给错单位会得到完全不同的年份 —— 这正是要暴露的。"""
    p = MassiveProvider(FIX / "massive_luld.jsonl",
                        {"batch_size": 1, "time_unit": "ms"})
    p.connect(); p.subscribe(["AAPL"])
    evs = p.poll()
    assert (not evs) or (evs[0].event_time.year != 2026), \
        "单位给错却得到正确年份 —— 说明单位没有真正生效"


def test_massive_unknown_event_ignored(tmp_path):
    f = tmp_path / "x.jsonl"
    f.write_text(json.dumps({"ev": "ZZZ", "T": "AAPL", "t": 1}) + "\n", encoding="utf-8")
    p = MassiveProvider(f, {"batch_size": 5})
    p.connect(); p.subscribe(["AAPL"])
    assert p.poll() == []


# ---------------------------------------------------------------------- #
# Alpaca
# ---------------------------------------------------------------------- #
def test_alpaca_capabilities_no_halt_events():
    caps = AlpacaProvider().capabilities()
    assert caps.sip_consolidated is True
    assert caps.halt_events is False and caps.luld_events is False
    assert caps.mwcb_events is False


def test_alpaca_trade_parsed():
    p = AlpacaProvider(FIX / "alpaca_trades.jsonl", {"batch_size": 10})
    p.connect(); p.subscribe(["AAPL"])
    evs = p.poll()
    tr = [e for e in evs if isinstance(e, TradeEvent)]
    assert tr and tr[0].price == 201.2 and tr[0].size == 50


def test_alpaca_quote_parsed():
    p = AlpacaProvider(FIX / "alpaca_trades.jsonl", {"batch_size": 10})
    p.connect(); p.subscribe(["AAPL"])
    qt = [e for e in p.poll() if isinstance(e, QuoteEvent)]
    assert qt and qt[0].bid == 201.1 and qt[0].ask == 201.3


def test_alpaca_exchange_normalized():
    p = AlpacaProvider(FIX / "alpaca_trades.jsonl", {"batch_size": 10})
    p.connect(); p.subscribe(["AAPL"])
    assert p.poll()[0].exchange == "iex"


# ---------------------------------------------------------------------- #
# Databento
# ---------------------------------------------------------------------- #
def test_databento_declares_native_halt():
    caps = DatabentoProvider().capabilities()
    assert caps.halt_events is True and caps.halt_events_native is True
    assert caps.luld_events is False, "status schema 不等于 LULD 价格带"
    assert caps.halt_events_native and not caps.notes == ""


def test_databento_status_halt_and_resume():
    p = DatabentoProvider(FIX / "databento_status.jsonl", {"batch_size": 10})
    p.connect(); p.subscribe(["AAPL"])
    evs = [e for e in p.poll() if isinstance(e, HaltEvent)]
    assert [e.halted for e in evs] == [True, False]
    assert evs[0].authority is EventAuthority.EXCHANGE_HALT


def test_databento_trade_price_scaled():
    p = DatabentoProvider(FIX / "databento_status.jsonl", {"batch_size": 10})
    p.connect(); p.subscribe(["AAPL"])
    tr = [e for e in p.poll() if isinstance(e, TradeEvent)]
    assert tr and abs(tr[0].price - 201.2) < 1e-6


def test_databento_has_no_source_event_id():
    """官方 status/trades 记录**没有**事件 ID 字段，能力声明必须如实为 False。

    早期实现从虚构的 ``id`` 字段里取 ``source_event_id``，用它做幂等键会
    在真实数据上全部取到空串 —— 靠它去重等于没有去重。
    """
    caps = DatabentoProvider().capabilities()
    assert caps.source_event_id is False

    f = Path(__import__("tempfile").mkdtemp()) / "d.jsonl"
    f.write_text(json.dumps({
        "rtype": 18, "ts_event": int(T0.timestamp() * 1e9),
        "ts_recv": int(T0.timestamp() * 1e9), "publisher_id": 2,
        "instrument_id": 42, "symbol": "AAPL",
        "action": 8, "reason": 30, "id": "vendor-only-field",
    }) + "\n", encoding="utf-8")
    p = DatabentoProvider(f, {"batch_size": 5})
    p.connect(); p.subscribe(["AAPL"])
    ev = p.poll()[0]
    assert ev.source_event_id == "", "不得把非官方字段伪装成事件 ID"
    assert ev.halted is True


def test_databento_status_carries_no_sequence():
    """status 记录没有 sequence（官方字段表无此项），能力声明须如实。"""
    caps = DatabentoProvider().capabilities()
    assert caps.sequence_field is False
    p = DatabentoProvider(FIX / "databento_status.jsonl", {"batch_size": 10})
    p.connect(); p.subscribe(["AAPL"])
    halts = [e for e in p.poll() if isinstance(e, HaltEvent)]
    assert halts and all(e.sequence == 0 for e in halts)


# ---------------------------------------------------------------------- #
# Nasdaq ITCH
# ---------------------------------------------------------------------- #
def test_nasdaq_trading_action_parsed():
    p = NasdaqItchProvider(FIX / "nasdaq_trading_action.jsonl", {"batch_size": 10})
    p.connect(); p.subscribe(["AAPL"])
    evs = [e for e in p.poll() if isinstance(e, HaltEvent)]
    assert len(evs) == 2 and evs[0].halted is True and evs[1].halted is False
    assert evs[0].halt_code == "H"


def test_nasdaq_unknown_action_ignored(tmp_path):
    f = tmp_path / "n.jsonl"
    f.write_text(json.dumps({"symbol": "AAPL", "action": "X",
                             "timestamp": int(T0.timestamp() * 1e9)}) + "\n",
                 encoding="utf-8")
    p = NasdaqItchProvider(f, {"batch_size": 5})
    p.connect(); p.subscribe(["AAPL"])
    assert p.poll() == []


# ---------------------------------------------------------------------- #
# 通用
# ---------------------------------------------------------------------- #
def test_missing_fixture_raises():
    p = MassiveProvider("/nonexistent/file.jsonl")
    with pytest.raises(ProviderError):
        p.connect()


def test_empty_fixture_raises(tmp_path):
    f = tmp_path / "empty.jsonl"
    f.write_text("", encoding="utf-8")
    p = MassiveProvider(f)
    with pytest.raises(ProviderError):
        p.connect()


def test_bad_row_does_not_break_batch(tmp_path):
    f = tmp_path / "mix.jsonl"
    f.write_text(
        json.dumps({"ev": "T", "T": "AAPL", "p": 100, "s": 1,
                    "t": int(T0.timestamp() * 1e9), "q": 1}) + "\n" +
        json.dumps({"ev": "T", "T": "", "p": 100, "s": 1,
                    "t": int(T0.timestamp() * 1e9), "q": 2}) + "\n",
        encoding="utf-8")
    p = MassiveProvider(f, {"batch_size": 10})
    p.connect(); p.subscribe(["AAPL"])
    assert len(p.poll()) == 1
