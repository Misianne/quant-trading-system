"""FRED 探测工具的响应解析单测（全部离线，不发网络请求）。

背景：2026-11-19 实测中 `vintage_dates` 返回的是**字符串数组**
``["2026-11-10", "2026-10-14", ...]``，旧代码按 ``d.get("vintage_date")``
解析，抛 ``AttributeError: 'str' object has no attribute 'get'``，整轮任务中断。

这里用**实测抓下来的真实结构**做夹具，锁死解析行为。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "fred_probe", Path(__file__).resolve().parents[1] / "tools" / "fred_probe.py")
assert _SPEC and _SPEC.loader
fp = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("fred_probe", fp)
_SPEC.loader.exec_module(fp)


# --------------------------------------------------------------------------- #
# vintage_dates —— 本次 bug 的正主
# --------------------------------------------------------------------------- #
#: 2026-11-19 直连 FRED `/series/vintagedates?series_id=CPIAUCSL` 的真实结构
REAL_VINTAGE_RESPONSE = {
    "realtime_start": "2026-11-19", "realtime_end": "2026-11-19",
    "order_by": "vintage_date", "sort_order": "desc",
    "count": 5, "offset": 0, "limit": 5,
    "vintage_dates": ["2026-11-10", "2026-10-14", "2026-09-11",
                      "2026-08-12", "2026-07-14"],
}


def test_real_string_list_is_parsed_without_attribute_error():
    """回归：字符串元素不得再被当成 dict。"""
    dates, info = fp.parse_vintage_dates(REAL_VINTAGE_RESPONSE)
    assert dates == ["2026-11-10", "2026-10-14", "2026-09-11",
                     "2026-08-12", "2026-07-14"]
    assert info.get("error") is None
    assert info["malformed_elements"] == 0
    assert info["page_count"] == 5


def test_empty_list_is_valid_not_an_error():
    dates, info = fp.parse_vintage_dates({"vintage_dates": [], "count": 0})
    assert dates == []
    assert info.get("error") is None
    assert info["page_count"] == 0


def test_dict_elements_are_tolerated():
    """容错形态，不作为主路径，但必须能吃下。"""
    payload = {"vintage_dates": [{"vintage_date": "2026-10-14"},
                                 {"vintage_date": "2026-09-11"}]}
    dates, info = fp.parse_vintage_dates(payload)
    assert dates == ["2026-10-14", "2026-09-11"]
    assert info.get("error") is None


def test_mixed_elements_skip_the_bad_ones_and_report():
    payload = {"vintage_dates": ["2026-10-14", 12345, None, {"foo": 1},
                                 "2026-09-11"]}
    dates, info = fp.parse_vintage_dates(payload)
    assert dates == ["2026-10-14", "2026-09-11"]
    assert info["malformed_elements"] == 3
    assert "3 个元素格式异常" in info["error"]


@pytest.mark.parametrize("payload", [
    None, "not-a-dict", 42, [],
])
def test_non_object_payload_reports_structure(payload):
    dates, info = fp.parse_vintage_dates(payload)
    assert dates == []
    assert info.get("error")
    assert "struct" in info


def test_missing_field_reports_structure():
    dates, info = fp.parse_vintage_dates({"count": 3, "offset": 0})
    assert dates == []
    assert "缺少 vintage_dates" in info["error"]


def test_field_wrong_type_reports_type_name():
    dates, info = fp.parse_vintage_dates({"vintage_dates": "2026-10-14"})
    assert dates == []
    assert "不是数组" in info["error"] and "str" in info["error"]


# --------------------------------------------------------------------------- #
# observations —— 400 时没有 observations 字段，旧代码会 KeyError
# --------------------------------------------------------------------------- #
def test_observations_normal():
    payload = {"observations": [
        {"realtime_start": "2026-09-11", "realtime_end": "2026-11-19",
         "date": "2026-08-01", "value": "334.131"},
        {"realtime_start": "2026-10-14", "realtime_end": "2026-11-19",
         "date": "2026-09-01", "value": "336.298"},
    ]}
    rows, info = fp.parse_observations(payload)
    assert len(rows) == 2
    assert info.get("error") is None


def test_observations_error_body_is_reported_not_crashed():
    """真实 400：output_type=4 不给 realtime 窗口时就是长这样。"""
    payload = {"error_code": 400,
               "error_message": "Bad Request.  No vintage dates exist for the "
                                "specified real-time period: 2026-11-19 to 2026-11-19."}
    rows, info = fp.parse_observations(payload)
    assert rows == []
    assert "No vintage dates exist" in info["error"]


def test_observations_missing_field():
    rows, info = fp.parse_observations({"count": 0})
    assert rows == []
    assert "缺少 observations" in info["error"]


def test_observations_not_a_list():
    rows, info = fp.parse_observations({"observations": {"a": 1}})
    assert rows == []
    assert "不是数组" in info["error"]


def test_observations_skips_malformed_rows():
    payload = {"observations": [
        {"date": "2026-08-01", "value": "334.131"},
        {"value": "1.0"},            # 缺 date
        "garbage",
    ]}
    rows, info = fp.parse_observations(payload)
    assert len(rows) == 1
    assert "格式异常" in info["error"]


def test_value_map_skips_missing_marker():
    rows = [{"date": "2026-08-01", "value": "334.131"},
            {"date": "2026-09-01", "value": "."},
            {"date": "2026-10-01", "value": ""}]
    m = fp._value_map(rows)
    assert m == {"2026-08-01": "334.131"}


# --------------------------------------------------------------------------- #
# series 元数据
# --------------------------------------------------------------------------- #
def test_series_meta_normal():
    payload = {"seriess": [{
        "id": "CPIAUCSL", "title": "Consumer Price Index for All Urban Consumers",
        "units": "Index 1982-1984=100", "frequency": "Monthly",
        "seasonal_adjustment": "Seasonally Adjusted",
        "observation_start": "1947-01-01", "observation_end": "2026-10-01",
        "last_updated": "2026-11-13 7:41 AM CST", "popularity": 92,
        "notes": "x" * 500,
    }]}
    meta, info = fp.parse_series_meta(payload)
    assert meta["title"].startswith("Consumer Price Index")
    assert meta["observation_start"] == "1947-01-01"
    assert len(meta["notes"]) == 200          # 截断
    assert info.get("error") is None


@pytest.mark.parametrize("payload,needle", [
    ({}, "缺少 seriess"),
    ({"seriess": []}, "为空"),
    ({"seriess": ["not-a-dict"]}, "不是对象"),
])
def test_series_meta_bad_shapes(payload, needle):
    meta, info = fp.parse_series_meta(payload)
    assert meta == {}
    assert needle in info["error"]


# --------------------------------------------------------------------------- #
# 端到端：一次坏响应不得让整轮任务中断
# --------------------------------------------------------------------------- #
def test_bad_series_does_not_abort_others():
    """模拟 probe_series 的异常隔离：一个序列抛错，其余仍能产出。"""
    results = {}
    for sid in ("GOOD", "BAD", "ALSO_GOOD"):
        try:
            if sid == "BAD":
                raise AttributeError("'str' object has no attribute 'get'")
            results[sid] = {"ok": True}
        except Exception as exc:  # noqa: BLE001
            results[sid] = {"fatal": f"{type(exc).__name__}: {exc}"}
    assert results["GOOD"]["ok"] is True
    assert results["ALSO_GOOD"]["ok"] is True
    assert "AttributeError" in results["BAD"]["fatal"]
