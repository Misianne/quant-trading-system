"""启动前检查（PREFLIGHT）。**只读**，不做任何初始化。

为什么必须只读
--------------
PREFLIGHT 失败时要能干净退到 STOPPED，不能留下"部分初始化"的残缺状态。
因此这一层只做**断言**：配置是否合法、路径是否可写、时钟是否可信、
行情源是否真能给出数据、安全门禁是否确实关着实盘。

任何一项失败 → 整体 FAILED，不进入 INITIALIZING。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .. import timebase


@dataclass
class PreflightCheck:
    name: str
    ok: bool
    detail: str = ""
    critical: bool = True
    data: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail,
                "critical": self.critical, "data": dict(self.data)}


@dataclass
class PreflightReport:
    checks: List[PreflightCheck] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks if c.critical)

    @property
    def failures(self) -> List[PreflightCheck]:
        return [c for c in self.checks if not c.ok and c.critical]

    def as_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "started_at": self.started_at,
                "finished_at": self.finished_at,
                "failed": [c.name for c in self.failures],
                "checks": [c.as_dict() for c in self.checks]}


# ---------------------------------------------------------------------- #
def _check(name: str, fn: Callable[[], Any], critical: bool = True) -> PreflightCheck:
    try:
        res = fn()
    except Exception as exc:  # noqa: BLE001 - 任何异常都是"检查不通过"
        return PreflightCheck(name, False, f"{type(exc).__name__}: {exc}", critical)
    if isinstance(res, PreflightCheck):
        res.name = name
        return res
    if isinstance(res, tuple):
        ok, detail = res[0], (res[1] if len(res) > 1 else "")
        data = res[2] if len(res) > 2 else {}
        return PreflightCheck(name, bool(ok), str(detail or ""), critical, dict(data or {}))
    return PreflightCheck(name, bool(res), "", critical)


def run_preflight(*, cfg: Any, paper_cfg: Dict[str, Any], clocks: Dict[str, Any],
                  quote_source: Any, symbols: List[str],
                  db_path: Path, allow_live: bool = False,
                  explicit_paper_flag: bool = False) -> PreflightReport:
    """执行全部启动前检查。返回报告（不抛异常，由调用方判定）。"""
    rep = PreflightReport(started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))

    # 1) 显式 --paper 开关 ------------------------------------------- #
    rep.checks.append(_check("explicit_paper_flag", lambda: (
        bool(explicit_paper_flag),
        "必须以 --paper 显式启动" if not explicit_paper_flag else "已显式指定 --paper")))

    # 2) 运行器总开关 ------------------------------------------------ #
    def _enabled():
        on = bool(paper_cfg.get("enabled", False))
        if on:
            return True, "paper_runtime.enabled=true"
        if explicit_paper_flag:
            # 配置里关着，但命令行显式 --paper：这是**人工显式**启动路径，允许
            return True, ("paper_runtime.enabled=false（默认不自动启动）；"
                          "本次由 --paper 显式启动")
        return False, "paper_runtime.enabled=false 且未显式 --paper"
    rep.checks.append(_check("paper_runtime_enabled", _enabled,
                             critical=not explicit_paper_flag))

    # 3) 全局 P2 开关必须仍为 false（本轮纪律） ------------------------ #
    def _p2_global():
        p2 = (cfg.raw.get("us_phase2") or {})
        val = bool(p2.get("enabled", False))
        return (not val), (f"us_phase2.enabled={val}（必须为 false，"
                           f"Paper 用作用域内覆盖而不是改全局）")
    rep.checks.append(_check("us_phase2_global_off", _p2_global))

    # 4) 安全门禁：必须是模拟盘，且不得允许真实下单 -------------------- #
    def _safety():
        st = cfg.gate.state
        problems = []
        if not st.paper_trading:
            problems.append("paper_trading=false")
        if st.live_trading and not allow_live:
            problems.append("live_trading=true")
        if st.kill_switch:
            problems.append("kill_switch=true（哨兵/手动停止）")
        ok, reason = cfg.gate.can_send_real_order()
        if ok:
            problems.append("can_send_real_order()=True（实盘通道是开的）")
        return (not problems), ("; ".join(problems) or
                                f"模拟盘={st.paper_trading} 实盘={st.live_trading} "
                                f"kill={st.kill_switch}"), {"gate": cfg.gate.snapshot()}
    rep.checks.append(_check("safety_gate_paper_only", _safety))

    # 5) 时钟 -------------------------------------------------------- #
    def _clock():
        dt = timebase.current().now()
        if dt.tzinfo is None:
            return False, "时钟返回 naive 时间"
        if not clocks:
            return False, "未配置任何交易所时钟"
        mkt = "us"
        clock = clocks.get(mkt) or next(iter(clocks.values()))
        if not getattr(clock, "configured", False):
            return False, f"{mkt} 未配置常规交易时段（会 fail-closed）"
        info = clock.session(dt)
        return True, f"{getattr(timebase.current(), 'name', '?')} → {info.phase} " \
                     f"({info.local_now})", {"session": info.as_dict(),
                                             "clock": getattr(timebase.current(), 'name', '?')}
    rep.checks.append(_check("clock_trusted", _clock))

    # 6) 目录可写 + SQLite 可用 --------------------------------------- #
    def _storage():
        db_path.parent.mkdir(parents=True, exist_ok=True)
        probe = db_path.parent / f".preflight-{os.getpid()}.tmp"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        conn = sqlite3.connect(str(db_path), timeout=5)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE IF NOT EXISTS _preflight_probe (x INTEGER)")
            conn.execute("DELETE FROM _preflight_probe")
            conn.commit()
        finally:
            conn.close()
        free_gb = shutil.disk_usage(str(db_path.parent)).free / 1e9
        return True, f"{db_path} 可写，剩余 {free_gb:.1f} GB", {"free_gb": round(free_gb, 2)}
    rep.checks.append(_check("storage_writable", _storage))

    # 7) 行情源可用 -------------------------------------------------- #
    def _feed():
        if quote_source is None:
            return False, "未注入行情源"
        if not symbols:
            return False, "标的清单为空"
        ok_n, details = 0, []
        for sym in symbols[:5]:
            try:
                q = quote_source.quote(sym)
                px = float(q.last)
                if px <= 0:
                    details.append(f"{sym}=非正价 {px}")
                    continue
                ts = getattr(q, "ts", None)
                if ts is None:
                    details.append(f"{sym}=无时间戳")
                    continue
                ok_n += 1
            except Exception as exc:  # noqa: BLE001
                details.append(f"{sym}: {type(exc).__name__}: {exc}")
        return (ok_n > 0), (f"可用 {ok_n}/{min(len(symbols), 5)}"
                            + ("; " + "; ".join(details[:3]) if details else "")), \
               {"ok": ok_n}
    rep.checks.append(_check("quote_source_alive", _feed))

    # 8) 风控参数自洽 ------------------------------------------------- #
    def _risk():
        r = cfg.risk
        problems = []
        if float(r.get("max_position_pct", 0.15)) <= 0:
            problems.append("max_position_pct<=0")
        if float(r.get("max_gross_exposure_pct", 0.60)) <= 0:
            problems.append("max_gross_exposure_pct<=0")
        if int(r.get("max_positions", 8)) <= 0:
            problems.append("max_positions<=0")
        if not bool(r.get("require_stop_loss", True)):
            problems.append("require_stop_loss=false（禁止无止损开仓）")
        return (not problems), ("; ".join(problems) or "风控参数自洽")
    rep.checks.append(_check("risk_config_sane", _risk))

    # 9) 禁止真实券商能力声明 ---------------------------------------- #
    def _no_live_broker():
        kind = str(cfg.get("broker.kind", "") or "").lower()
        if kind in ("fosun", "fosun_live", "live"):
            return False, f"broker.kind={kind}（PaperRuntime 只允许模拟盘）"
        return True, f"broker.kind={kind or '(unset → paper)'}"
    rep.checks.append(_check("no_live_broker", _no_live_broker))

    rep.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return rep
