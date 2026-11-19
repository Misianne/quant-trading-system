"""事件层测试：时间语义、版本回放、以及"没有预期就不许编"。

这套测试的核心不是覆盖率，而是**守住三条线**：
1. 决策时点之前不可见的内容，回放时必须拿不到；
2. 时间无法确认的事件，任何时点都拿不到（宁可不用，也不提前用）；
3. 没有一致预期时，解释结构必须写"市场预期未知"，且不允许给出预期差。
"""
from __future__ import annotations

import pytest

from ai_trader.events import (DATE_ONLY, RETRIEVED, UNKNOWN, EventError,
                              EventRecord, explain_event, sha256_text)


def _ev(**kw) -> EventRecord:
    base = dict(event_id="E1", title="FOMC 上调目标区间 25bp",
                category="monetary", trust_level="official",
                channels=["rates"], expectation=0.25, expectation_source="SPF 中位数",
                actual=0.25, actual_unit="pp")
    base.update(kw)
    return EventRecord(**base)


# --------------------------------------------------------------------------- #
# 1) 时间语义
# --------------------------------------------------------------------------- #
def test_version_available_at_prefers_source_time_over_retrieval():
    ev = _ev()
    v = ev.add_version("stmt", "https://fed/x.htm",
                       source_published_at="2026-03-18T14:00:00-04:00",
                       retrieved_at="2026-03-18T14:05:00-04:00")
    assert v.available_at == "2026-03-18T14:00:00-04:00"
    assert v.time_precision == "exact"


def test_retrieval_only_marks_precision_as_retrieved():
    ev = _ev()
    v = ev.add_version("stmt", "https://fed/x.htm",
                       retrieved_at="2026-03-18T14:05:00-04:00")
    assert v.time_precision == RETRIEVED
    assert v.available_at == "2026-03-18T14:05:00-04:00"


def test_date_only_precision():
    ev = _ev()
    v = ev.add_version("stmt", "https://fed/x.htm", source_published_at="2026-03-18")
    assert v.time_precision == DATE_ONLY


def test_unknown_time_is_never_visible():
    """时间不确定 → 任何时点都不可见（防"提前使用"）。"""
    ev = _ev()
    v = ev.add_version("传闻：某国将加征关税", "https://example/rumor")
    assert v.time_precision == UNKNOWN
    assert ev.visible_at("2030-01-01T00:00:00+00:00") is None
    assert ev.is_visible("2030-01-01T00:00:00+00:00") is False


def test_naive_timestamp_rejected():
    ev = _ev()
    ev.add_version("stmt", "u", source_published_at="2026-03-18T14:00:00")
    with pytest.raises(EventError):
        ev.visible_at("2026-03-19T00:00:00+00:00")


# --------------------------------------------------------------------------- #
# 2) 版本回放
# --------------------------------------------------------------------------- #
def test_replay_picks_the_version_visible_then():
    ev = _ev()
    ev.add_version("首次通报：维持利率不变", "u1",
                   source_published_at="2026-03-18T14:00:00-04:00")
    ev.add_version("更正：上调 25bp", "u2",
                   source_published_at="2026-03-18T16:00:00-04:00",
                   revised_at="2026-03-18T16:00:00-04:00")

    at_14 = ev.visible_at("2026-03-18T14:30:00-04:00")
    assert at_14.version == 1 and "维持利率不变" in at_14.text

    at_17 = ev.visible_at("2026-03-18T17:00:00-04:00")
    assert at_17.version == 2 and "上调" in at_17.text

    before = ev.visible_at("2026-03-18T13:59:00-04:00")
    assert before is None


def test_first_publication_time_recorded_once():
    ev = _ev()
    ev.add_version("v1", "u1", source_published_at="2026-03-18T14:00:00-04:00")
    ev.add_version("v2", "u2", source_published_at="2026-03-19T09:00:00-04:00")
    assert ev.first_publication_time == "2026-03-18T14:00:00-04:00"


def test_versions_are_not_overwritten():
    ev = _ev()
    ev.add_version("v1", "u1", source_published_at="2026-03-18T14:00:00-04:00")
    ev.add_version("v2", "u2", source_published_at="2026-03-19T09:00:00-04:00")
    assert len(ev.versions) == 2
    assert [v.version for v in ev.versions] == [1, 2]


def test_source_hash_is_stable_and_versioned():
    ev = _ev()
    a = ev.add_version("same text", "u1", source_published_at="2026-03-18T14:00:00-04:00")
    b = ev.add_version("same text", "u2", source_published_at="2026-03-19T14:00:00-04:00")
    assert a.source_hash == b.source_hash == sha256_text("same text")
    c = ev.add_version("changed", "u3", source_published_at="2026-03-20T14:00:00-04:00")
    assert c.source_hash != a.source_hash


# --------------------------------------------------------------------------- #
# 3) 十段解释：没有预期就不许编
# --------------------------------------------------------------------------- #
def _explained(**kw):
    ev = _ev(**kw)
    ev.add_version("全文……", "https://fed/x.htm",
                   source_published_at="2026-03-18T14:00:00-04:00")
    return explain_event(ev, "2026-03-19T00:00:00-04:00")


def test_explanation_has_all_ten_sections():
    doc = _explained()
    assert [s["key"] for s in doc["sections"]] == [
        "what", "evidence", "first_pub", "expectation", "surprise",
        "channels", "impact", "priced_in", "counter", "confidence"]


def test_missing_expectation_forces_unknown_wording():
    doc = _explained(expectation=None, actual=None)
    by = {s["key"]: s for s in doc["sections"]}
    assert doc["can_assess_surprise"] is False
    assert by["expectation"]["status"] == "unverifiable"
    assert "市场预期未知" in by["expectation"]["content"][0]
    assert by["surprise"]["status"] == "unverifiable"
    assert "无法计算" in by["surprise"]["content"][0]
    assert by["expectation"]["missing"]


def test_with_expectation_surprise_is_computed():
    doc = _explained(expectation=0.25, actual=0.50)
    by = {s["key"]: s for s in doc["sections"]}
    assert doc["can_assess_surprise"] is True
    assert doc["surprise"] == pytest.approx(0.25)
    assert by["surprise"]["status"] == "fact"


def test_impact_and_priced_in_are_never_facts():
    doc = _explained()
    by = {s["key"]: s for s in doc["sections"]}
    assert by["impact"]["status"] == "unverifiable"
    assert by["priced_in"]["status"] == "unverifiable"
    assert by["impact"]["missing"]


def test_event_not_visible_returns_unusable():
    ev = _ev()
    ev.add_version("v1", "u1", source_published_at="2026-03-18T14:00:00-04:00")
    doc = explain_event(ev, "2026-03-18T13:00:00-04:00")
    assert doc["usable"] is False and doc["sections"] == []


def test_channels_must_come_from_enum():
    from ai_trader.events import CHANNELS
    ev = _ev(channels=["rates", "inflation"])
    ev.add_version("t", "u", source_published_at="2026-03-18T14:00:00-04:00")
    doc = explain_event(ev, "2026-03-19T00:00:00-04:00")
    by = {s["key"]: s for s in doc["sections"]}
    assert by["channels"]["status"] == "fact"
    for line in by["channels"]["content"]:
        key = line.split("：")[0]
        assert key in CHANNELS
