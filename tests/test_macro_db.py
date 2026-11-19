"""宏观库（point-in-time）测试。

分两层：

* **离线层**：用构造数据覆盖 10 类必须验证的行为（多版本、边界、夏令时、
  未知发布时间、重复导入、中断恢复、迁移回滚……），不依赖网络与真实库；
* **真实回放层**：读 ``data/macro_data.db``，对 CPI / GDP / 就业做真实版本回放，
  证明决策时点取不到未来才知道的修订值。库不存在时自动 skip。

判定原则：**不看"值是否不同"，只看"该时点能看见什么"**。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ai_trader.macro.ingest import (build_rows, canonicalize, parse_wide_row,
                                    plan_windows)
from ai_trader.macro.schema import (SCHEMA_VERSION, SchemaError, ensure_schema,
                                    integrity_check, table_names)
from ai_trader.macro.snapshot import get_macro_snapshot, history
from ai_trader.macro.store import (MacroStore, MacroStoreError,
                                   conservative_release_ts, epoch_of,
                                   exact_release_ts)

REAL_DB = Path(__file__).resolve().parents[1] / "data" / "macro_data.db"


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _store(tmp_path) -> MacroStore:
    return MacroStore(tmp_path / "m.db")


def _obs(sid: str, period: str, vintage: str, value: float,
         initial: int = 0, precision: str = "date") -> dict:
    return {"series_id": sid, "observation_period": period, "vintage_date": vintage,
            "realtime_start": vintage, "realtime_end": None, "value": value,
            "is_initial_release": initial, "source": "TEST",
            "release_date": vintage,
            "release_timestamp": conservative_release_ts(vintage),
            "time_precision": precision, "verified": 0}


# --------------------------------------------------------------------------- #
# 1) schema / 迁移
# --------------------------------------------------------------------------- #
def test_schema_creates_all_five_tables(tmp_path):
    st = _store(tmp_path)
    assert table_names(st.conn) == sorted([
        "macro_series", "macro_observations", "macro_releases",
        "macro_raw_responses", "macro_ingest_log"])
    assert st.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert integrity_check(st.conn) == "ok"
    st.close()


def test_schema_migration_rolls_back_completely():
    """任一条 DDL 失败 → 整个迁移回滚，不留半套表（executescript 做不到这点）。"""
    conn = sqlite3.connect(":memory:")
    with pytest.raises(SchemaError):
        ensure_schema(conn, ["CREATE TABLE a(x)",
                             "CREATE TABLE b(x)",
                             "THIS IS NOT VALID SQL"])
    assert table_names(conn) == []
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    conn.close()


def test_schema_refuses_downgrade(tmp_path):
    st = _store(tmp_path)
    st.conn.execute("PRAGMA user_version=99")
    with pytest.raises(SchemaError):
        ensure_schema(st.conn)
    st.close()


# --------------------------------------------------------------------------- #
# 2) 多版本 / 初值与修订并存
# --------------------------------------------------------------------------- #
def test_multiple_vintages_of_same_period_coexist(tmp_path):
    st = _store(tmp_path)
    st.upsert_observations([
        _obs("CPI", "2026-09-01", "2026-10-15", 320.0, initial=1),
        _obs("CPI", "2026-09-01", "2026-11-13", 319.4),
        _obs("CPI", "2026-09-01", "2027-02-11", 319.6),
    ])
    rows = st.conn.execute(
        "SELECT vintage_date, value, is_initial_release FROM macro_observations"
        " WHERE series_id='CPI' AND observation_period='2026-09-01'"
        " ORDER BY vintage_date").fetchall()
    assert [tuple(r) for r in rows] == [
        ("2026-10-15", 320.0, 1), ("2026-11-13", 319.4, 0), ("2027-02-11", 319.6, 0)]
    assert len(history(st, "CPI", "2026-09-01")) == 3
    st.close()


def test_initial_and_revision_are_distinguishable(tmp_path):
    st = _store(tmp_path)
    st.upsert_observations([
        _obs("CPI", "2026-09-01", "2026-10-15", 320.0, initial=1),
        _obs("CPI", "2026-09-01", "2026-11-13", 319.4),
    ])
    hist = history(st, "CPI", "2026-09-01")
    assert [h["is_initial_release"] for h in hist] == [1, 0]
    assert hist[0]["value"] == 320.0 and hist[1]["value"] == 319.4
    st.close()


# --------------------------------------------------------------------------- #
# 3) 时点边界：早于 / 恰好 / 修订之后
# --------------------------------------------------------------------------- #
def _three_version_store(tmp_path) -> MacroStore:
    st = _store(tmp_path)
    st.upsert_observations([
        _obs("CPI", "2026-09-01", "2026-10-15", 320.0, initial=1),   # 首次
        _obs("CPI", "2026-09-01", "2026-11-13", 319.4),              # 下修
    ])
    return st


def test_snapshot_before_first_release_returns_missing(tmp_path):
    st = _three_version_store(tmp_path)
    # 首次发布是 2026-10-15 23:59:59 ET；前一秒必须什么都看不到
    as_of = "2026-10-15T23:59:58-04:00"
    snap = get_macro_snapshot(st, as_of, series=["CPI"])
    assert snap.by_series.get("CPI") is None
    assert snap.missing_series == ["CPI"]
    st.close()


def test_snapshot_exactly_at_release_is_visible(tmp_path):
    st = _three_version_store(tmp_path)
    ts = conservative_release_ts("2026-10-15")
    snap = get_macro_snapshot(st, ts, series=["CPI"])
    p = snap.latest("CPI")
    assert p is not None and p.value == 320.0 and p.is_initial_release == 1
    st.close()


def test_snapshot_after_revision_uses_latest_visible_version(tmp_path):
    st = _three_version_store(tmp_path)
    # 修订发布日当天 → 必须看到 319.4，而不是 320.0
    snap = get_macro_snapshot(st, conservative_release_ts("2026-11-13"), series=["CPI"])
    p = snap.latest("CPI")
    assert p.value == 319.4 and p.is_initial_release == 0
    assert p.vintage_date == "2026-11-13"
    st.close()


def test_snapshot_is_replayable_and_never_reads_future(tmp_path):
    """同一 as_of 反复查结果一致；且任何 as_of 都取不到更晚发布的版本。"""
    st = _three_version_store(tmp_path)
    for as_of, expect in (("2026-10-16T00:00:00-04:00", 320.0),
                          ("2026-11-12T23:59:59-05:00", 320.0),
                          ("2026-11-14T00:00:00-05:00", 319.4)):
        a = get_macro_snapshot(st, as_of, series=["CPI"])
        b = get_macro_snapshot(st, as_of, series=["CPI"])
        assert a.latest("CPI").value == expect
        assert a.as_dict() == b.as_dict()
    st.close()


# --------------------------------------------------------------------------- #
# 4) 时区与夏令时
# --------------------------------------------------------------------------- #
def test_dst_boundary_offsets_differ():
    """美东 2026 年：3-08 起 EDT(-04:00)，11-01 起 EST(-05:00)。"""
    before = conservative_release_ts("2026-03-06")     # 仍 EST
    after = conservative_release_ts("2026-03-10")      # 已 EDT
    nov_before = conservative_release_ts("2026-10-30")  # 仍 EDT
    nov_after = conservative_release_ts("2026-11-03")   # 已 EST
    assert before.endswith("-05:00") and after.endswith("-04:00")
    assert nov_before.endswith("-04:00") and nov_after.endswith("-05:00")


def test_epoch_comparison_beats_string_comparison():
    """字符串比较在跨偏移时会出错，UTC 秒不会 —— 这就是库里存 release_epoch 的理由。"""
    a = conservative_release_ts("2026-10-30")   # -04:00
    b = conservative_release_ts("2026-11-02")   # -05:00
    assert a < b                                  # 字符串也恰好对
    # 构造一个字符串序会判错、真实时序相反的例子
    early = exact_release_ts("2026-11-01", "00:30", "America/New_York")   # EDT? 11-01 仍是 EDT
    late = exact_release_ts("2026-11-01", "01:30", "America/New_York")
    assert epoch_of(early) < epoch_of(late)


def test_naive_timestamp_is_rejected():
    with pytest.raises(MacroStoreError):
        epoch_of("2026-11-01T08:30:00")
    with pytest.raises(MacroStoreError):
        get_macro_snapshot(":memory:", "2026-11-01T08:30:00")


# --------------------------------------------------------------------------- #
# 5) 缺失与未知发布时间
# --------------------------------------------------------------------------- #
def test_unknown_release_time_is_excluded_and_reported(tmp_path):
    """发布时间不可确认 → 不能假装精确；release_epoch 为空即不可见于任何时点。"""
    st = _store(tmp_path)
    st.upsert_observations([
        {"series_id": "X", "observation_period": "2026-01-01", "vintage_date": "2026-02-01",
         "value": 1.0, "source": "TEST", "release_date": None,
         "release_timestamp": None, "time_precision": "unknown", "verified": 0},
        _obs("X", "2026-02-01", "2026-03-01", 2.0),
    ])
    snap = get_macro_snapshot(st, "2026-06-01T00:00:00+00:00", series=["X"])
    periods = [p.observation_period for p in snap.by_series["X"]]
    assert periods == ["2026-02-01"]                 # 未知发布时间的行不出现
    st.close()


def test_precision_warning_is_exposed(tmp_path):
    st = _store(tmp_path)
    st.upsert_observations([_obs("X", "2026-01-01", "2026-02-01", 1.0)])
    snap = get_macro_snapshot(st, "2026-06-01T00:00:00+00:00", series=["X"])
    assert snap.precision_warnings and snap.precision_warnings[0]["time_precision"] == "date"
    st.close()


# --------------------------------------------------------------------------- #
# 6) 幂等 / 中断恢复
# --------------------------------------------------------------------------- #
def test_duplicate_import_is_idempotent(tmp_path):
    st = _store(tmp_path)
    rows = [_obs("CPI", "2026-09-01", "2026-10-15", 320.0, initial=1),
            _obs("CPI", "2026-09-01", "2026-11-13", 319.4)]
    first = st.upsert_observations(rows)
    second = st.upsert_observations(rows)
    assert (first.inserted, first.updated, first.unchanged) == (2, 0, 0)
    assert (second.inserted, second.updated, second.unchanged) == (0, 0, 2)
    assert st.conn.execute("SELECT COUNT(*) FROM macro_observations").fetchone()[0] == 2
    st.close()


def test_reimport_does_not_overwrite_historical_initial_value(tmp_path):
    """重复导入同内容，绝不把初值改成当前修订值。"""
    st = _store(tmp_path)
    st.upsert_observations([_obs("CPI", "2026-09-01", "2026-10-15", 320.0, initial=1)])
    st.upsert_observations([_obs("CPI", "2026-09-01", "2026-10-15", 319.9, initial=1)])
    # 同 (series, period, vintage) 内容不同 → 记 updated，但仍是一行、一个 vintage
    assert st.conn.execute("SELECT COUNT(*) FROM macro_observations").fetchone()[0] == 1
    st.close()


def test_interrupted_ingest_can_resume(tmp_path):
    st = _store(tmp_path)
    st.start_ingest("T1", "CPI")
    row = st.conn.execute("SELECT status FROM macro_ingest_log").fetchone()
    assert row[0] == "running"
    # 模拟崩溃后同一 task 重启
    st.start_ingest("T1", "CPI")
    st.upsert_observations([_obs("CPI", "2026-09-01", "2026-10-15", 320.0, initial=1)])
    from ai_trader.macro.store import WriteResult
    st.finish_ingest("T1", "CPI", WriteResult(inserted=1), "ok")
    row = st.conn.execute(
        "SELECT status, rows_inserted FROM macro_ingest_log WHERE task_id='T1'").fetchone()
    assert tuple(row) == ("ok", 1)
    assert st.conn.execute("SELECT COUNT(*) FROM macro_observations").fetchone()[0] == 1
    st.close()


def test_failed_series_does_not_block_others(tmp_path, monkeypatch):
    """单序列异常只落 ingest_log，不中断其它序列。"""
    from ai_trader.macro import ingest as ing

    st = _store(tmp_path)

    def boom(*a, **k):
        raise ing.FetchError("模拟某序列 500")

    monkeypatch.setattr(ing, "fetch_series_meta", boom)
    r = ing.ingest_series(object(), st, ing.SeriesTask("BAD"), "T2", tmp_path / "raw")
    assert r["status"] == "failed" and "模拟" in r["error"]
    assert st.conn.execute(
        "SELECT status FROM macro_ingest_log WHERE series_id='BAD'").fetchone()[0] == "failed"
    st.close()


# --------------------------------------------------------------------------- #
# 7) 解析与规范化（入库口径）
# --------------------------------------------------------------------------- #
def test_parse_wide_row_skips_unchanged_dots():
    row = {"date": "2026-01-05", "VIXCLS_20260105": "14.9", "VIXCLS_20260106": "."}
    assert parse_wide_row(row, "VIXCLS") == [("2026-01-05", "2026-01-05", 14.9)]


def test_parse_wide_row_rejects_unparsable_column():
    with pytest.raises(Exception):
        parse_wide_row({"date": "2026-01-05", "BADCOL": "1.0"}, "VIXCLS")


def test_canonicalize_drops_noop_versions():
    pts = [("2026-09-01", "2026-10-15", 320.0),
           ("2026-09-01", "2026-11-13", 320.0),     # 同值 → 不是新版本
           ("2026-09-01", "2027-02-11", 319.6)]
    assert canonicalize(pts)["2026-09-01"] == [("2026-10-15", 320.0),
                                               ("2027-02-11", 319.6)]


def test_plan_windows_respects_chunk_size():
    vints = [f"2026-{m:02d}-{d:02d}" for m in range(1, 13) for d in range(1, 29)]
    wins = plan_windows(vints, chunk=100)
    assert sum(len(w) for w in wins) == len(vints)
    assert all(len(w) <= 100 for w in wins)
    assert wins[0][0] == vints[0] and wins[-1][-1] == vints[-1]


def test_build_rows_marks_first_available_version_initial():
    pts = [("2026-09-01", "2026-10-15", 320.0), ("2026-09-01", "2026-11-13", 319.4)]
    rows, rel = build_rows("CPI", pts, "America/New_York", "2026-11-19T00:00:00+00:00")
    assert [r["is_initial_release"] for r in rows] == [1, 0]
    assert [r["release_type"] for r in rel] == ["initial", "revision"]
    assert all(r["time_precision"] == "date" for r in rows)
    assert all(r["verified"] == 0 for r in rows)          # 时刻未核验，如实标记


def test_release_precision_change_is_detected(tmp_path):
    """upsert_releases 曾经把 verified 当成 time_precision 比较，改动检不出来。"""
    st = _store(tmp_path)
    base = {"series_id": "CPI", "observation_period": "2026-09-01",
            "release_date": "2026-10-15", "release_timestamp": None,
            "timezone": "America/New_York", "release_type": "initial",
            "source": "FRED", "time_precision": "date", "verified": 0, "notes": None}
    assert st.upsert_releases([base]).inserted == 1
    assert st.upsert_releases([base]).unchanged == 1
    changed = dict(base, time_precision="exact", verified=1)
    r = st.upsert_releases([changed])
    assert r.updated == 1
    assert st.conn.execute(
        "SELECT time_precision, verified FROM macro_releases"
        " WHERE series_id='CPI'").fetchone()[:] == ("exact", 1)
    st.close()


def test_raw_archive_strips_api_key(tmp_path):
    st = _store(tmp_path)
    req = st.archive_raw(endpoint="/series", params={"series_id": "CPI", "api_key": "SECRET"},
                         body=b'{"ok":true}', http_status=200, raw_dir=tmp_path / "raw")
    stored = st.conn.execute(
        "SELECT request_parameters FROM macro_raw_responses WHERE request_id=?",
        (req,)).fetchone()[0]
    assert "SECRET" not in stored and "api_key" not in stored
    st.close()


def test_market_daily_rows_are_marked_inferred_and_lagged():
    """日频市场序列没有 vintage 依据 → 必须标 inferred，DFF 还要推后 1 个工作日。"""
    from ai_trader.macro.ingest import _next_weekday, build_market_rows

    assert _next_weekday("2026-11-20", 1) == "2026-11-23"   # 周五 → 下周一
    assert _next_weekday("2026-11-19", 0) == "2026-11-19"

    obs = [{"date": "2026-11-19", "value": "5.10"},
           {"date": "2026-11-20", "value": "."}]            # 缺失值必须跳过
    rows, rel = build_market_rows("DGS10", obs, "America/New_York",
                                  "2026-11-20T00:00:00+00:00", lag_weekdays=0)
    assert len(rows) == 1 and len(rel) == 1
    assert rows[0]["time_precision"] == "inferred"
    assert rows[0]["verified"] == 0
    assert rows[0]["vintage_date"] == "2026-11-19"

    dff, _ = build_market_rows("DFF", [{"date": "2026-11-20", "value": "3.88"}],
                               "America/New_York", "2026-11-20T00:00:00+00:00",
                               lag_weekdays=1)
    assert dff[0]["release_date"] == "2026-11-23"           # 次一工作日才可见
    assert epoch_of(dff[0]["release_timestamp"]) > epoch_of(
        conservative_release_ts("2026-11-20"))


def test_snapshot_flags_inferred_precision_separately(tmp_path):
    st = _store(tmp_path)
    st.upsert_observations([_obs("DGS10", "2026-11-19", "2026-11-19", 5.1,
                                 initial=1, precision="inferred")])
    snap = get_macro_snapshot(st, "2026-12-01T00:00:00+00:00", series=["DGS10"])
    w = snap.precision_warnings[0]
    assert w["time_precision"] == "inferred" and w["points"] == 1
    assert "推定" in w["note"] or "精确到日" in w["note"]
    st.close()


# --------------------------------------------------------------------------- #
# 8) 真实库回放（CPI / GDP / 就业）
# --------------------------------------------------------------------------- #
pytestmark_real = pytest.mark.skipif(
    not REAL_DB.exists(), reason="真实 macro_data.db 不存在（先跑 tools/macro_ingest.py）")


def _pick_observation(conn, sid: str):
    """挑一个至少有 2 个版本、且版本间隔最长的观测期。"""
    row = conn.execute(
        "SELECT observation_period, COUNT(*) n FROM macro_observations"
        " WHERE series_id=? GROUP BY observation_period HAVING n>=2"
        " ORDER BY n DESC LIMIT 1", (sid,)).fetchone()
    if row is None:
        pytest.skip(f"{sid} 没有多版本观测")
    return row[0]


@pytest.mark.parametrize("sid", ["CPIAUCSL", "GDP", "PAYEMS"])
@pytestmark_real
def test_real_replay_never_reads_future_version(sid):
    """真实数据回放：任一版本发布前 1 秒，只能看到**上一版**，绝不看到本版或更晚。

    这是"无未来函数"的**正确表述**：不是"什么都看不到"（早期版本本来就该可见），
    而是"看到的必须严格是上一版的值"。
    """
    st = MacroStore(REAL_DB)
    period = _pick_observation(st.conn, sid)
    chain = history(st, sid, period)
    assert len(chain) >= 2, f"{sid} {period} 版本不足"

    def seen_at(ts: str):
        snap = get_macro_snapshot(st, ts, series=[sid])
        pts = [p for p in (snap.by_series.get(sid) or [])
               if p.observation_period == period]
        return pts[0] if pts else None

    for i, h in enumerate(chain):
        epoch = epoch_of(h["release_timestamp"])

        before = seen_at(datetime.fromtimestamp(epoch - 1, timezone.utc).isoformat())
        if i == 0:
            assert before is None, f"{sid} {period} 首次发布前就可见（泄漏）"
        else:
            assert before is not None, f"{sid} {period} 在 {h['vintage_date']} 前丢失了上一版"
            assert before.value == pytest.approx(chain[i - 1]["value"]), (
                f"{sid} {period} 在 {h['vintage_date']} 之前看到了未来版本")
            assert before.vintage_date == chain[i - 1]["vintage_date"]

        at = seen_at(h["release_timestamp"])
        assert at is not None and at.value == pytest.approx(h["value"])
        assert at.vintage_date == h["vintage_date"]

    # 最新版本之后，取到的必须是最新版本而不是某个更早的
    last = chain[-1]
    after = seen_at(datetime.fromtimestamp(
        epoch_of(last["release_timestamp"]) + 86400, timezone.utc).isoformat())
    assert after.value == pytest.approx(last["value"])
    assert after.vintage_date == last["vintage_date"]
    st.close()


@pytestmark_real
def test_real_db_has_no_future_leak_across_series():
    """全局抽查：任取 20 个时点，快照里不得出现 release_epoch 晚于 as_of 的行。"""
    st = MacroStore(REAL_DB)
    rows = st.conn.execute(
        "SELECT release_epoch FROM macro_observations"
        " WHERE release_epoch IS NOT NULL"
        " ORDER BY release_epoch DESC LIMIT 20").fetchall()
    assert rows
    for (ep,) in rows:
        as_of = datetime.fromtimestamp(ep, timezone.utc).isoformat()
        snap = get_macro_snapshot(st, as_of)
        for pts in snap.by_series.values():
            for p in pts:
                assert p.release_epoch is not None and p.release_epoch <= ep
    st.close()


@pytestmark_real
def test_real_db_volume_and_integrity():
    st = MacroStore(REAL_DB)
    counts = st.counts()
    assert counts["macro_series"] >= 12
    assert counts["macro_observations"] > 10_000
    assert integrity_check(st.conn) == "ok"
    st.close()


@pytestmark_real
def test_real_gdp_revision_is_material():
    """GDP 的修订幅度必须真实存在，否则"防泄漏"这件事没有意义。"""
    st = MacroStore(REAL_DB)
    row = st.conn.execute(
        "SELECT observation_period, MIN(value), MAX(value), COUNT(*)"
        " FROM macro_observations WHERE series_id='GDP'"
        " GROUP BY observation_period HAVING COUNT(*)>=3"
        " ORDER BY COUNT(*) DESC LIMIT 1").fetchone()
    if row is None:
        pytest.skip("GDP 无多版本观测")
    period, lo, hi, n = row
    assert n >= 3
    assert hi > lo, f"GDP {period} 各版本取值完全相同（{n} 版），修订未被记录"
    st.close()
