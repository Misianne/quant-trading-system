"""单实例锁与资源释放测试。"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from ai_trader.config import load_config
from ai_trader.paper_runtime.lock import InstanceLock
from ai_trader.paper_runtime.replay import SCENARIOS, build_runtime_for
from ai_trader.paper_runtime.timebase_bridge import restore_clock

CFG = load_config()


# ---------------------------------------------------------------------- #
# 锁
# ---------------------------------------------------------------------- #
def test_lock_acquire_and_release(tmp_path):
    lock = InstanceLock(tmp_path / "x.lock", run_id="r1")
    info = lock.acquire()
    assert info.acquired and lock.held
    assert (tmp_path / "x.lock").exists()
    assert lock.release() is True
    assert not (tmp_path / "x.lock").exists()


def test_lock_blocks_second_holder(tmp_path):
    a = InstanceLock(tmp_path / "x.lock", run_id="a")
    b = InstanceLock(tmp_path / "x.lock", run_id="b")
    assert a.acquire().acquired
    info = b.acquire()
    assert info.acquired is False
    assert "已有运行实例" in info.reason
    a.release()


def test_lock_recovers_stale_file(tmp_path):
    p = tmp_path / "x.lock"
    p.write_text('{"pid": 999999, "run_id": "dead"}', encoding="utf-8")
    lock = InstanceLock(p, run_id="new")
    info = lock.acquire()
    assert info.acquired and info.stale_recovered
    lock.release()


def test_lock_does_not_release_someone_elses(tmp_path):
    p = tmp_path / "x.lock"
    p.write_text('{"pid": %d, "run_id": "other"}' % (os.getpid() + 1), encoding="utf-8")
    lock = InstanceLock(p, run_id="mine")
    assert lock.release() is False
    assert p.exists()


def test_lock_release_without_acquire_is_false(tmp_path):
    assert InstanceLock(tmp_path / "x.lock").release() is False


def test_two_runtimes_same_root_conflict(tmp_path):
    sc = SCENARIOS["mwcb"]
    rt1, clk1, s1, m1 = build_runtime_for(sc, cfg=CFG, out_dir=tmp_path / "a",
                                          lock_root=tmp_path)
    rt2, clk2, s2, m2 = build_runtime_for(sc, cfg=CFG, out_dir=tmp_path / "b",
                                          lock_root=tmp_path)
    try:
        assert rt1.start().ok
        r2 = rt2.start()
        assert r2.ok is False
        assert "单实例锁" in r2.reason
        assert rt2.machine.state.value == "FAILED"
    finally:
        rt2.stop("cleanup")
        rt1.stop("cleanup")
        restore_clock()


def test_lock_released_after_normal_stop(tmp_path):
    sc = SCENARIOS["mwcb"]
    rt, clk, s, m = build_runtime_for(sc, cfg=CFG, out_dir=tmp_path / "r",
                                      lock_root=tmp_path)
    rt.start()
    assert (tmp_path / "paper_runtime.lock").exists()
    rt.stop("done")
    assert not (tmp_path / "paper_runtime.lock").exists()
    restore_clock()


def test_lock_released_when_preflight_fails(tmp_path):
    sc = SCENARIOS["mwcb"]
    rt, clk, s, m = build_runtime_for(sc, cfg=CFG, out_dir=tmp_path / "r",
                                      lock_root=tmp_path, explicit_paper=False)
    rt.paper_cfg["enabled"] = False
    r = rt.start()
    assert r.ok is False
    assert not (tmp_path / "paper_runtime.lock").exists(), "失败路径必须释放锁"
    restore_clock()


# ---------------------------------------------------------------------- #
# 资源释放
# ---------------------------------------------------------------------- #
def test_stop_closes_owned_journal(tmp_path):
    sc = SCENARIOS["mwcb"]
    rt, clk, s, m = build_runtime_for(sc, cfg=CFG, out_dir=tmp_path / "r")
    rt.start()
    rt.stop("done")
    with pytest.raises(Exception):
        rt.journal.counts()          # 连接已关闭
    restore_clock()


def test_stop_is_idempotent(tmp_path):
    sc = SCENARIOS["mwcb"]
    rt, clk, s, m = build_runtime_for(sc, cfg=CFG, out_dir=tmp_path / "r")
    rt.start()
    a = rt.stop("1")
    b = rt.stop("2")
    assert a.ok and b.already_stopped
    restore_clock()


def test_start_is_idempotent(tmp_path):
    sc = SCENARIOS["mwcb"]
    rt, clk, s, m = build_runtime_for(sc, cfg=CFG, out_dir=tmp_path / "r")
    a = rt.start()
    b = rt.start()
    assert a.ok and b.already_started
    rt.stop("done")
    restore_clock()


def test_components_released_in_reverse(tmp_path):
    sc = SCENARIOS["mwcb"]
    rt, clk, s, m = build_runtime_for(sc, cfg=CFG, out_dir=tmp_path / "r")
    rt.start()
    order = list(reversed(rt.registry.order()))
    res = rt.stop("done")
    assert res.release["errors"] == []
    assert order[0] == "engine"
    restore_clock()


def test_no_thread_leak_after_stop(tmp_path):
    import threading
    before = threading.active_count()
    for i in range(3):
        sc = SCENARIOS["mwcb"]
        rt, clk, s, m = build_runtime_for(sc, cfg=CFG, out_dir=tmp_path / f"r{i}")
        rt.start(); rt.to_running()
        rt.tick()
        rt.stop("done")
        restore_clock()
    assert threading.active_count() <= before + 1


def test_sqlite_connections_released(tmp_path):
    import gc
    sc = SCENARIOS["mwcb"]
    rt, clk, s, m = build_runtime_for(sc, cfg=CFG, out_dir=tmp_path / "r")
    rt.start()
    rt.stop("done")
    restore_clock()
    del rt
    gc.collect()
    conns = [o for o in gc.get_objects()
             if isinstance(o, sqlite3.Connection)]
    assert len(conns) <= 2, f"仍有 {len(conns)} 个 SQLite 连接未释放"


def test_repeated_cycles_do_not_grow_handles(tmp_path):
    from ai_trader.paper_runtime.supervisor import process_handle_count
    base = process_handle_count()
    for i in range(5):
        sc = SCENARIOS["mwcb"]
        rt, clk, s, m = build_runtime_for(sc, cfg=CFG, out_dir=tmp_path / f"c{i}")
        rt.start(); rt.to_running(); rt.tick(); rt.stop("done")
        restore_clock()
    after = process_handle_count()
    assert after - base <= 10, f"句柄增长 {after - base}"


def test_run_dir_keeps_state_and_ledger(tmp_path):
    sc = SCENARIOS["normal_day"]
    rt, clk, s, m = build_runtime_for(sc, cfg=CFG, out_dir=tmp_path / "r")
    rt.start(); rt.to_running()
    for i in range(sc.ticks):
        rt.tick(model_tick=i)
    rt.stop("done")
    restore_clock()
    assert (tmp_path / "r" / "paper_state.json").exists()
    assert (tmp_path / "r" / "paper_fills.jsonl").exists()
    assert (tmp_path / "r" / "paper_audit.db").exists()


def test_ledger_survives_restart(tmp_path):
    sc = SCENARIOS["normal_day"]
    rt, clk, s, m = build_runtime_for(sc, cfg=CFG, out_dir=tmp_path / "r")
    rt.start(); rt.to_running()
    for i in range(sc.ticks):
        rt.tick(model_tick=i)
    n = len(rt.ledger.fills())
    rt.stop("done"); restore_clock()

    rt2, clk2, s2, m2 = build_runtime_for(sc, cfg=CFG, out_dir=tmp_path / "r")
    r2 = rt2.start()
    assert r2.ok
    assert len(rt2.ledger.fills()) == n
    rt2.stop("done"); restore_clock()
