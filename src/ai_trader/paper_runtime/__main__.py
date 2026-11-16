"""PaperRuntime 命令行入口。

**必须显式 ``--paper``**。不带参数直接运行只会打印用法并以非零码退出 ——
默认绝不自动启动（这是硬性纪律，不是配置项）。

用法::

    # 跑单个回放场景
    python -m ai_trader.paper_runtime --paper --replay normal_day

    # 跑全部 10 个场景（每个 1 次），输出汇总
    python -m ai_trader.paper_runtime --paper --replay all

    # 确定性验证：同一场景跑 3 次并比对指纹
    python -m ai_trader.paper_runtime --paper --replay normal_day --repeat 3

    # 持续运行（默认 30 分钟，循环回放，真实执行链路）
    python -m ai_trader.paper_runtime --paper --soak-minutes 30

    # 查看最近一次运行状态（读心跳 + 报告，不启动任何东西）
    python -m ai_trader.paper_runtime --status

退出码：0 成功；2 参数/保护检查失败；3 有场景未通过；4 运行时失败。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

if __package__ in (None, ""):        # 允许 python -m 与直接执行
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ai_trader import timebase                     # noqa: E402
from ai_trader.config import PROJECT_ROOT, load_config   # noqa: E402
from ai_trader.paper_runtime.replay import SCENARIOS, run_scenario  # noqa: E402
from ai_trader.paper_runtime.supervisor import (           # noqa: E402
    Supervisor, heartbeat_age_seconds, pid_alive, read_heartbeat)


# ---------------------------------------------------------------------- #
def _hard_guards(cfg: Any) -> List[str]:
    """启动前再次确认所有硬性保护（不依赖 preflight 的实现细节）。"""
    problems: List[str] = []
    st = cfg.gate.state
    if not st.paper_trading:
        problems.append("PAPER_TRADING 未开启")
    if st.live_trading:
        problems.append("LIVE_TRADING 已开启（禁止）")
    if st.kill_switch:
        problems.append("KILL_SWITCH 已触发")
    ok, why = cfg.gate.can_send_real_order()
    if ok:
        problems.append(f"实盘下单通道是开的：{why}")
    p2 = (cfg.raw.get("us_phase2") or {})
    if bool(p2.get("enabled", False)):
        problems.append("us_phase2.enabled=true（本轮必须为 false；Paper 用作用域覆盖）")
    if os.getenv("LIVE_TRADING", "").lower() in ("1", "true", "yes"):
        problems.append("环境变量 LIVE_TRADING 为真")
    return problems


def _run_root(cfg: Any, override: Optional[str]) -> Path:
    if override:
        return Path(override).resolve()
    raw = (cfg.raw.get("paper_runtime") or {}).get("run_root", "data/paper_runs")
    p = Path(str(raw))
    return p if p.is_absolute() else (PROJECT_ROOT / p)


# ---------------------------------------------------------------------- #
def recover_only(scenario: Any, *, cfg: Any, out_dir: Path) -> Any:
    """只启动（含恢复扫描）不交易，然后立即停止。"""
    from ai_trader.paper_runtime.replay import ReplayResult, build_runtime_for
    from ai_trader.paper_runtime.timebase_bridge import restore_clock

    res = ReplayResult(scenario=scenario.key, run_id="", ok=False)
    res.input_digest = scenario.input_digest()
    rt, clk, src, model = build_runtime_for(scenario, cfg=cfg, out_dir=out_dir)
    res.run_id = rt.run_id
    try:
        start = rt.start()
        res.ok = bool(start.ok)
        res.final_state = rt.machine.state.value
        res.invariants_ok = bool(getattr(rt._last_invariants, "ok", True))
        res.report = {"start": start.as_dict(),
                      "recovery": (rt._recovery.as_dict() if rt._recovery else None),
                      "ledger": rt.ledger.stats()}
        if not start.ok:
            res.failures.append(start.reason)
        return res
    finally:
        rt.stop("recover_only")
        restore_clock()


def cmd_status(cfg: Any, root: Path) -> int:
    # 递归查找（运行目录可能是 data/paper_runs/<批次>/run-soak-*）
    runs = sorted((p for p in root.rglob("run-*") if p.is_dir()),
                  key=lambda p: (p / "heartbeat.json").stat().st_mtime
                  if (p / "heartbeat.json").exists() else p.stat().st_mtime)
    if not runs:
        print(f"没有运行记录：{root}")
        return 0
    latest = runs[-1]
    hb = read_heartbeat(latest / "heartbeat.json")
    out: Dict[str, Any] = {"run_dir": str(latest)}
    if hb:
        alive = pid_alive(int(hb.get("pid", 0)))
        age = heartbeat_age_seconds(hb)
        out.update({"heartbeat": hb, "pid_alive": alive,
                    "heartbeat_age_s": round(age, 1),
                    "verdict": ("RUNNING" if alive and age < 60
                                else "STALLED" if alive else "DEAD")})
    rep = latest / "run_report.json"
    if rep.exists():
        try:
            out["report"] = json.loads(rep.read_text(encoding="utf-8"))
        except ValueError:
            out["report"] = "（报告损坏）"
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------------- #
def cmd_replay(cfg: Any, args: argparse.Namespace, root: Path) -> int:
    keys = list(SCENARIOS) if args.replay in ("all", "*") else [args.replay]
    bad = [k for k in keys if k not in SCENARIOS]
    if bad:
        print(f"未知场景：{bad}；可用：{sorted(SCENARIOS)}")
        return 2

    summary: List[Dict[str, Any]] = []
    failed = 0
    for key in keys:
        digests: List[str] = []
        for n in range(max(1, args.repeat)):
            if args.run_dir:
                run_dir = Path(args.run_dir).resolve() / f"{key}-{n}"
            else:
                run_dir = root / f"run-replay-{key}-{n}-{datetime.now():%Y%m%dT%H%M%S}"
            run_dir.mkdir(parents=True, exist_ok=True)
            if args.recover_only:
                res = recover_only(SCENARIOS[key], cfg=cfg, out_dir=run_dir)
            else:
                res = run_scenario(SCENARIOS[key], cfg=cfg, out_dir=run_dir)
            (run_dir / "run_report.json").write_text(
                json.dumps(res.as_dict(), ensure_ascii=False, indent=2, default=str),
                encoding="utf-8")
            digests.append(res.digest)
            if not res.ok:
                failed += 1
            summary.append({"scenario": key, "repeat": n, "ok": res.ok,
                            "digest": res.digest, "fills": res.fills,
                            "orders": res.orders, "final_state": res.final_state,
                            "failures": res.failures, "run_dir": str(run_dir)})
        if len(set(digests)) > 1:
            print(f"[DETERMINISM-FAIL] {key} 多次运行指纹不一致：{digests}")
            failed += 1
        elif len(digests) > 1:
            print(f"[deterministic] {key} ×{len(digests)} → {digests[0][:16]}")

    out_path = root / f"replay-summary-{datetime.now():%Y%m%dT%H%M%S}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"generated_at": timebase.now().isoformat(),
                                    "results": summary},
                                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"summary": str(out_path), "runs": len(summary),
                      "failed": failed}, ensure_ascii=False))
    return 0 if failed == 0 else 3


def cmd_soak(cfg: Any, args: argparse.Namespace, root: Path) -> int:
    """持续运行：循环回放场景（真实执行行情→策略→风控→订单→账户）。"""
    from ai_trader.paper_runtime.replay import build_runtime_for
    from ai_trader.paper_runtime.supervisor import Supervisor

    minutes = float(args.soak_minutes)
    # 注意：不含 feed_outage —— 它的设计结果就是 HALTED（终态），
    # 放进循环会让"持续运行"在第一轮就结束。故障恢复单独验证。
    keys = ["normal_day", "regular_session", "luld_limit_state",
            "luld_pause_resume", "mwcb", "premarket", "afterhours",
            "early_close", "holiday"]
    run_dir = root / f"run-soak-{datetime.now():%Y%m%dT%H%M%S}"
    run_dir.mkdir(parents=True, exist_ok=True)

    sc = SCENARIOS[keys[0]]
    rt, clk, src, model = build_runtime_for(
        sc, cfg=cfg, out_dir=run_dir,
        lock_root=Path(args.lock_root) if args.lock_root else root)
    start = rt.start()
    if not start.ok:
        print(json.dumps({"ok": False, "reason": start.reason}, ensure_ascii=False))
        return 4
    rt.to_running()

    def _progress() -> dict:
        return {"total_ticks": total_ticks, "cycles_done": cycles,
                "total_fills": total_fills}

    sup = Supervisor(rt, run_dir=run_dir,
                     interval_seconds=float(args.tick_interval),
                     max_consecutive_failures=3, max_auto_recoveries=3,
                     heartbeat_timeout_seconds=60.0, progress_fn=_progress)
    from datetime import timedelta

    # 运行时长必须用**单调墙钟**：回放时钟会跳跃，用它计时会瞬间跑完
    import time as _time
    deadline_mono = _time.monotonic() + minutes * 60.0
    total_ticks = 0
    total_fills = 0
    cycles_log = []

    def _expired() -> bool:
        return _time.monotonic() >= deadline_mono
    cycles = 0
    fails = 0
    started_at = datetime.now()          # 运维标识用墙钟，不用回放时间
    try:
        # 先跑一轮 supervisor 预检
        pre = sup.preflight()
        if not pre["ok"]:
            print(json.dumps({"ok": False, "preflight": pre}, ensure_ascii=False))
            return 4
        stop_flag = run_dir / "stop.request"
        while not _expired():
            if stop_flag.exists():
                print(json.dumps({"stop_requested": True,
                                  "detail": stop_flag.read_text(encoding="utf-8",
                                                                errors="replace")[:200]},
                                 ensure_ascii=False))
                break
            key = keys[cycles % len(keys)]
            sc = SCENARIOS[key]
            if cycles:
                # 换场景 = 重新装一个运行体（同一进程内顺序执行）
                rt.stop("cycle_end")
                from ai_trader.paper_runtime.timebase_bridge import restore_clock
                restore_clock()
                rt, clk, src, model = build_runtime_for(
                    sc, cfg=cfg, out_dir=run_dir / f"cycle-{cycles}")
                st = rt.start()
                if not st.ok:
                    fails += 1
                    break
                rt.to_running()
                sup = Supervisor(rt, run_dir=run_dir,
                                 interval_seconds=float(args.tick_interval),
                                 max_consecutive_failures=3,
                                 progress_fn=_progress)
            for i in range(sc.ticks):
                if _expired():
                    break
                if i in sc.day_breaks:
                    cur = timebase.now()
                    nxt = cur.date() + timedelta(days=1)
                    while nxt.weekday() >= 5:
                        nxt += timedelta(days=1)
                    clk.set(datetime(nxt.year, nxt.month, nxt.day, 9, 30,
                                     tzinfo=clk.now().tzinfo))
                elif i:
                    clk.advance(timedelta(minutes=sc.tick_minutes))
                src.set_tick(i)
                if sc.use_luld:
                    ms = rt.ctx.service("market_status")
                    now = timebase.now()
                    for sym, series in sc.price_series().items():
                        prior = float(sc.prior_close.get(sym, series[0]))
                        if i == 0:
                            ms.update_reference(sym, prior, prior_close=prior)
                        ms.observe_trade(sym, float(series[min(i, len(series) - 1)]),
                                         now, bounds=sc.bounds(), prior_close=prior)
                rt.tick()
                total_ticks += 1
                sup.step()
                if rt.machine.state.value in ("HALTED", "FAILED"):
                    break
                if args.tick_interval:
                    _time.sleep(float(args.tick_interval))
            cycles += 1
            total_fills += len(rt.ledger.fills())
            cycles_log.append({"cycle": cycles, "scenario": key,
                               "ticks": rt._ticks, "state": rt.machine.state.value,
                               "fills": len(rt.ledger.fills())})
            if rt.machine.state.value in ("HALTED", "FAILED"):
                break
    finally:
        try:
            rep = sup.report
            end_state = rt.machine.state.value
            final = {"run_id": rt.run_id, "cycles": cycles, "fails": fails,
                     "started_at": started_at.isoformat(timespec="seconds"),
                     "finished_at": datetime.now().isoformat(timespec="seconds"),
                     "duration_seconds": round(_time.monotonic() - (deadline_mono - minutes * 60.0), 1),
                     "final_state": end_state,
                     "ticks": total_ticks, "fills": total_fills,
                     "cycles_log": cycles_log,
                     "supervisor": rep.as_dict(),
                     "health": rt.health(),
                     "account": rt.ctx.service("broker").health()
                     if rt.ctx.service("broker") else {}}
            (run_dir / "run_report.json").write_text(
                json.dumps(final, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8")
            print(json.dumps({"ok": fails == 0, "run_dir": str(run_dir),
                              "cycles": cycles, "ticks": total_ticks,
                              "fills": total_fills,
                              "final_state": end_state}, ensure_ascii=False))
            safe = rt.stop("soak_end")
            from ai_trader.paper_runtime.timebase_bridge import restore_clock
            restore_clock()
            with open(run_dir / "soak_tail.json", "w", encoding="utf-8") as fh:
                json.dump({"stop": safe.as_dict()}, fh, ensure_ascii=False, indent=2)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            return 4
    return 0 if fails == 0 else 3


# ---------------------------------------------------------------------- #
def cmd_session(cfg: Any, args: argparse.Namespace, root: Path) -> int:
    """复用单运行体的长时连续会话，按 5/15/30 分钟节奏采集监控数据。"""
    import gc
    import threading
    import time as _time
    from datetime import datetime as _dt

    from ai_trader.paper_runtime.session import ReplaySession
    from ai_trader.paper_runtime.supervisor import (
        HeartbeatWriter, process_commit_mb, process_handle_count, process_rss_mb)

    minutes = float(args.session_minutes)
    run_dir = root / f"run-session-{_dt.now():%Y%m%dT%H%M%S}"
    run_dir.mkdir(parents=True, exist_ok=True)
    monitor = run_dir / "monitor.jsonl"
    sess = ReplaySession(cfg=cfg, out_dir=run_dir,
                         lock_root=Path(args.lock_root) if args.lock_root else root)
    start = sess.start()
    if not start.ok:
        print(json.dumps({"ok": False, "reason": start.reason}, ensure_ascii=False))
        return 4

    rt = sess.rt
    hb = HeartbeatWriter(run_dir / "heartbeat.json", rt.run_id)
    t0 = _time.monotonic()
    deadline = t0 + minutes * 60.0
    next5 = t0
    next15 = t0
    next30 = t0
    seg_rows: List[Dict[str, Any]] = []
    anomalies: List[str] = []

    def snap(tag: str) -> Dict[str, Any]:
        h = rt.health()
        broker = rt.ctx.service("broker")
        orders = broker.orders_snapshot() if broker else []
        inv = rt._last_invariants
        row = {
            "tag": tag,
            "wall_at": _dt.now().isoformat(timespec="seconds"),
            "elapsed_min": round((_time.monotonic() - t0) / 60.0, 2),
            "rss_mb": process_rss_mb(), "commit_mb": process_commit_mb(),
            "handles": process_handle_count(),
            "threads": threading.active_count(),
            "gc_objects": len(gc.get_objects()),
            "gc_count": list(gc.get_count()),
            "state": rt.machine.state.value, "health": h.get("aggregate"),
            "segments": sess._segments, "ticks": rt._ticks,
            "orders": len(orders), "fills": len(rt.ledger.fills()),
            "positions": len([p for p in (broker._state.get("positions") or {}).values()
                              if float(p.get("qty", 0) or 0) != 0]) if broker else 0,
            "cash_usd": round(float((broker._state.get("cash") or {}).get("USD", 0.0)), 2)
            if broker else None,
            "invariants_ok": bool(inv.ok) if inv is not None else None,
        }
        with open(monitor, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        hb.beat(row["state"], row["ticks"],
                {"segments": row["segments"], "fills": row["fills"],
                 "rss_mb": row["rss_mb"], "invariants_ok": row["invariants_ok"]})
        return row

    snap("t0")
    try:
        stop_flag = run_dir / "stop.request"
        while _time.monotonic() < deadline:
            if stop_flag.exists():
                print(json.dumps({"stop_requested": True,
                                  "detail": stop_flag.read_text(
                                      encoding="utf-8", errors="replace")[:200]},
                                 ensure_ascii=False), flush=True)
                break
            row = sess.run_segment()
            seg_rows.append(row)
            hb.beat(row["state"], rt._ticks,
                    {"segments": sess._segments, "fills": len(rt.ledger.fills()),
                     "rss_mb": process_rss_mb()})
            # 控制节奏：每段代表一个交易日，过快会产生不现实的订单量
            if args.segment_interval and _time.monotonic() < deadline:
                _time.sleep(float(args.segment_interval))
            if row["state"] in ("HALTED", "FAILED"):
                anomalies.append(f"会话在第 {row['segment']} 段进入 {row['state']}")
                break
            nowm = _time.monotonic()
            if nowm >= next30:
                next30 = nowm + 1800
                s30 = snap("30min-health")
                pre = {"ok": rt.health().get("aggregate") == "OK",
                       "components": list(rt.registry.names())}
                if not pre["ok"]:
                    anomalies.append(f"30 分钟健康检查异常：{pre}")
                with open(monitor, "a", encoding="utf-8", newline="\n") as fh:
                    fh.write(json.dumps({"tag": "30min-preflight", **pre},
                                        ensure_ascii=False) + "\n")
            if nowm >= next15:
                next15 = nowm + 900
                from ai_trader.paper_runtime.invariants import check_all
                rep = check_all(rt.ctx.service("broker"), rt.ledger,
                                settlement=rt.ctx.service("settlement"),
                                short_allowed=False)
                with open(monitor, "a", encoding="utf-8", newline="\n") as fh:
                    fh.write(json.dumps({"tag": "15min-invariants",
                                         "ok": rep.ok,
                                         "failed": [r.name for r in rep.results if not r.ok]},
                                        ensure_ascii=False) + "\n")
                if not rep.ok:
                    anomalies.append("15 分钟不变量失败：" +
                                     ",".join(r.name for r in rep.results if not r.ok))
            if nowm >= next5:
                next5 = nowm + 300
                snap("5min")
    finally:
        final = snap("before-stop")
        # 必须在 stop() **之前**取会话摘要：stop 后组件已释放，
        # broker 服务被移除、健康聚合会变成 HALTED（未启动=不健康），数字会失真。
        session_summary = sess.summary()
        stopres = sess.stop("session_end")
        final_state = rt.machine.state.value

        idle_rows = []
        if args.idle_minutes and args.idle_minutes > 0:
            idle_end = _time.monotonic() + float(args.idle_minutes) * 60.0
            while _time.monotonic() < idle_end:
                _time.sleep(min(60.0, max(1.0, idle_end - _time.monotonic())))
                idle_rows.append({"tag": "idle", "min": round(
                    (idle_end - _time.monotonic()) / 60.0, 1),
                    "rss_mb": process_rss_mb(), "commit_mb": process_commit_mb(),
                    "handles": process_handle_count(),
                    "gc_objects": len(gc.get_objects())})

        summary = {
            "ok": not anomalies and final_state in ("RUNNING", "READY", "STOPPED"),
            "run_id": rt.run_id, "run_dir": str(run_dir),
            "planned_minutes": minutes,
            "segments": sess._segments, "ticks": rt._ticks,
            "fills": len(rt.ledger.fills()),
            "final_state": final_state,
            "stop": stopres.as_dict(),
            "anomalies": anomalies,
            "first": final, "idle": idle_rows,
            "session": session_summary,
            "segments_tail": seg_rows[-10:],
        }
        (run_dir / "run_report.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
        print(json.dumps({k: summary[k] for k in
                          ("ok", "segments", "ticks", "fills", "final_state",
                           "anomalies", "run_dir")}, ensure_ascii=False))
    return 0 if not anomalies else 3


# ---------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m ai_trader.paper_runtime",
        description="本地 Paper/Replay 运行时（必须显式 --paper）")
    p.add_argument("--paper", action="store_true",
                   help="显式启用本地模拟盘（必需）")
    p.add_argument("--replay", default=None,
                   help="场景名，或 all 跑全部")
    p.add_argument("--repeat", type=int, default=1,
                   help="同一场景重复次数（用于确定性比对）")
    p.add_argument("--soak-minutes", type=float, default=None,
                   help="持续运行分钟数（循环回放，真实执行链路）")
    p.add_argument("--tick-interval", type=float, default=0.0,
                   help="每个 tick 之间的真实间隔秒数（0 = 不等待）")
    p.add_argument("--out", default=None, help="运行根目录（默认 data/paper_runs）")
    p.add_argument("--status", action="store_true",
                   help="查看最近一次运行状态（不启动任何东西）")
    p.add_argument("--config", default=None, help="配置文件路径")
    p.add_argument("--log-file", default=None,
                   help="把本进程 stdout/stderr 写到该文件（后台启动时不丢日志）")
    p.add_argument("--lock-root", default=None,
                   help="单实例锁所在目录（默认取 --out 根目录）")
    p.add_argument("--run-dir", default=None,
                   help="固定运行目录（崩溃/恢复实验用；不传则按时间戳新建）")
    p.add_argument("--session-minutes", type=float, default=None,
                   help="复用单运行体的连续会话时长（分钟），单一账户/单一库")
    p.add_argument("--segment-interval", type=float, default=3.0,
                   help="复用会话中每个交易段之间的真实间隔秒数（控制压力速率）")
    p.add_argument("--idle-minutes", type=float, default=0.0,
                   help="停止后静置观察内存的分钟数（0 = 不等待）")
    p.add_argument("--recover-only", action="store_true",
                   help="只做启动+恢复扫描，不交易（验证崩溃后状态）")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.log_file:
        lf = Path(args.log_file)
        lf.parent.mkdir(parents=True, exist_ok=True)
        _fh = open(lf, "a", encoding="utf-8", buffering=1, newline="\n")
        sys.stdout = _fh
        sys.stderr = _fh
    cfg = load_config(Path(args.config) if args.config else None)
    root = _run_root(cfg, args.out)

    if args.status:
        return cmd_status(cfg, root)

    if not args.paper:
        print("拒绝启动：必须显式指定 --paper。", file=sys.stderr)
        print("（PaperRuntime 默认不自动启动；本命令不会连接任何真实券商）",
              file=sys.stderr)
        build_parser().print_usage(sys.stderr)
        return 2

    from ai_trader.paper_runtime import fault_inject
    bad_inject = fault_inject.validate()
    if bad_inject:
        print(f"[fault-inject] {bad_inject}", file=sys.stderr)
        return 2

    problems = _hard_guards(cfg)
    if problems:
        print("硬性保护检查未通过：" + "; ".join(problems), file=sys.stderr)
        return 2

    if args.session_minutes:
        return cmd_session(cfg, args, root)
    if args.soak_minutes:
        return cmd_soak(cfg, args, root)
    if args.replay:
        return cmd_replay(cfg, args, root)

    print("用法：--paper 需要配合 --replay <场景|all> 或 --soak-minutes N",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
