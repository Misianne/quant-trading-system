"""Databento 官方 schema 契约测试。

这些用例的价值在于**盯住常量**：解析逻辑一旦与官方枚举脱节，
最典型的表现是"halt 判断永远为假"，而系统看起来一切正常。
所以这里逐条对照官方文档给出的整数值，不做"大概对"的断言。

来源：https://databento.com/docs/schemas/status
      https://databento.com/docs/schemas/trades
      databento 0.90.0 common/publishers.py::Venue.from_int
"""
from __future__ import annotations

import pytest

from ai_trader.market.providers import databento_schema as ds


# ---------------------------------------------------------------------- #
# rtype
# ---------------------------------------------------------------------- #
def test_rtype_constants_match_official():
    assert ds.RTYPE_STATUS == 18, "官方字段表：status 的 rtype 恒为 18"
    assert ds.RTYPE_TRADES == 0, "官方字段表：trades 的 rtype 恒为 0"


def test_rtype_names_are_readable():
    assert ds.RTYPE_NAME[18] == "status"
    assert ds.RTYPE_NAME[0] == "trades"
    assert ds.SCHEMA_RTYPE["status"] == 18
    assert ds.SCHEMA_RTYPE["trades"] == 0


# ---------------------------------------------------------------------- #
# status.action
# ---------------------------------------------------------------------- #
def test_status_action_enum_is_complete_and_exact():
    expected = {
        0: "NONE", 1: "PRE_OPEN", 2: "PRE_CROSS", 3: "QUOTING", 4: "CROSS",
        5: "ROTATION", 6: "NEW_PRICE_INDICATION", 7: "TRADING", 8: "HALT",
        9: "PAUSE", 10: "SUSPEND", 11: "PRE_CLOSE", 12: "CLOSE",
        13: "POST_CLOSE", 14: "SSR_CHANGE", 15: "NOT_AVAILABLE_FOR_TRADING",
    }
    assert ds.STATUS_ACTION == expected


def test_halting_actions_exclude_close():
    """停止交易 = 8/9/10。

    不含 15（官方定义"已收盘或已停牌"，含收盘），也不含 12 CLOSE ——
    把收盘当停牌会造出一条永不恢复的假停牌。
    """
    assert ds.HALTING_ACTIONS == frozenset({8, 9, 10})
    assert 12 not in ds.HALTING_ACTIONS
    assert 15 not in ds.HALTING_ACTIONS


def test_trading_and_ambiguous_actions():
    assert ds.TRADING_ACTIONS == frozenset({7})
    assert ds.AMBIGUOUS_ACTIONS == frozenset({15})
    assert ds.LULD_PAUSE_REASON == 50


# ---------------------------------------------------------------------- #
# status.reason
# ---------------------------------------------------------------------- #
def test_status_reason_has_non_contiguous_values():
    """reason 的取值**不连续**（7-9、19-29 之间没有定义）。

    早期实现按"连续枚举"假设做反查，未知值会静默落到相邻项。
    """
    assert ds.STATUS_REASON[0] == "NONE"
    assert ds.STATUS_REASON[30] == "NEWS_PENDING"
    assert ds.STATUS_REASON[50] == "LULD_PAUSE"
    assert ds.STATUS_REASON[130] == "QUOTATION_NOT_AVAILABLE"
    for missing in (7, 8, 9, 19, 25, 41, 51, 119, 125):
        assert missing not in ds.STATUS_REASON, f"{missing} 不应存在于官方枚举"


def test_mwcb_reasons_are_market_wide():
    assert ds.MWCB_REASONS == frozenset({120, 121, 122, 123, 124})
    assert ds.STATUS_REASON[120] == "MARKET_WIDE_HALT_LEVEL1"
    assert ds.STATUS_REASON[124] == "MARKET_WIDE_HALT_RESUMPTION"


def test_trading_event_enum():
    assert ds.TRADING_EVENT == {0: "NONE", 1: "NO_CANCEL",
                                2: "CHANGE_TRADING_SESSION",
                                3: "IMPLIED_MATCHING_ON",
                                4: "IMPLIED_MATCHING_OFF"}


# ---------------------------------------------------------------------- #
# publisher → venue
# ---------------------------------------------------------------------- #
def test_publisher_map_core_us_venues():
    assert ds.PUBLISHER_VENUE[1] == "glbx"
    assert ds.PUBLISHER_VENUE[2] == "nasdaq"
    assert ds.PUBLISHER_VENUE[9] == "nyse"
    assert ds.PUBLISHER_VENUE[30] == "opra"
    assert len(ds.PUBLISHER_VENUE) == 72, "官方 Venue.from_int 共 72 项"


def test_consolidated_publishers_are_flagged():
    """DBEQ/EQUS/CTA/UTP 是合并发布者，不能用来判"某个交易所停牌"。"""
    assert ds.CONSOLIDATED_PUBLISHERS == frozenset({40, 47, 59, 60})
    for pid in (40, 47, 59, 60):
        venue, consolidated = ds.venue_of(pid)
        assert consolidated is True


def test_unknown_publisher_is_not_guessed():
    venue, consolidated = ds.venue_of(9999)
    assert venue == "unknown" and consolidated is False
    assert ds.venue_of("XNAS")[0] == "unknown", "字符串 publisher 不得被猜成 nasdaq"
    assert ds.venue_of(None)[0] == "unknown"


# ---------------------------------------------------------------------- #
# 价格与三态
# ---------------------------------------------------------------------- #
def test_price_scale_is_1e9_and_undef_sentinel():
    assert ds.PRICE_SCALE == 1_000_000_000.0
    assert ds.UNDEF_PRICE == 9_223_372_036_854_775_807


def test_price_int_is_fixed_point():
    assert ds.price_to_float(201_200_000_000) == (201.2, "fixed_1e-9")
    assert ds.price_to_float(1) == (1e-9, "fixed_1e-9")


def test_price_float_is_already_scaled():
    """``pretty_px=True`` 的 JSON 里价格已经是美元价。

    再除一次 1e9 会得到 2e-7 —— 静默差 9 个数量级，比报错危险得多。
    """
    assert ds.price_to_float(201.2) == (201.2, "float")


def test_price_undef_sentinel_is_not_a_price():
    assert ds.price_to_float(ds.UNDEF_PRICE) == (0.0, "undef")
    assert ds.price_to_float(None) == (0.0, "invalid")
    assert ds.price_to_float(float("nan")) == (0.0, "invalid")


def test_tristate_does_not_treat_unknown_as_false():
    assert ds.TRI_STATE["Y"] is True
    assert ds.TRI_STATE["N"] is False
    assert "~" not in ds.TRI_STATE, "官方 '~' 表示未知，不能映射成 False"


# ---------------------------------------------------------------------- #
# 归一函数
# ---------------------------------------------------------------------- #
@pytest.mark.parametrize("raw,expected", [
    ("H", 8), ("T", 7), ("P", 9), ("S", 10), ("Q", 3),
    ("HALT", 8), ("TRADING", 7),
    (8, 8), (9, 9), (7, 7),
    (77, None), ("ZZ", None), (None, None), (True, None),
])
def test_status_action_int_normalization(raw, expected):
    assert ds.status_action_int(raw) == expected


def test_legacy_char_map_is_deliberately_small():
    """单字符映射只收录**唯一可确定**的五个。

    PRE_CROSS / CROSS / CLOSE 都像 ``C``，猜错会把"收盘"当"暂停"，
    宁可不认，交给 fail-closed 处理。
    """
    assert set(ds._LEGACY_CHAR_ACTION) == {"T", "H", "P", "S", "Q"}
    assert ds.status_action_code("C")[1] is False


def test_status_reason_code_unknown_is_marked():
    assert ds.status_reason_code(50) == ("LULD_PAUSE", True)
    assert ds.status_reason_code("LUDP")[1] is False
    name, ok = ds.status_reason_code(77)
    assert ok is False and "77" in name
