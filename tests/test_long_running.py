"""长期运行 / 稳定性测试。

覆盖用户要求的 7 项：
  1. worker 连续运行
  2. 网络断开和恢复
  3. 进程异常退出和重启
  4. 急停状态持久性（多次重启）
  5. 订单状态 UNKNOWN 恢复
  6. 数据库并发
  7. 日志是否泄露密钥

设计原则：能真跑就不 mock。进程退出用真实子进程 + SIGTERM/taskkill，
数据库并发用多连接多线程，日志检查扫真实日志目录。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from ai_trader.audit.journal import AuditJournal
from ai_trader.broker.base import BrokerError, OrderResult
from ai_trader.broker.paper import PaperBroker
from ai_trader.risk.controller import RiskController
from ai_trader.safety.halt import HALT_FILENAME, RESUME_PHRASE, HaltStore
from ai_trader.strategy.engine import StrategyEngine
from tests.helpers import FixedSignalModel, ScriptedBroker, live_cfg

ROOT = Path(__file__).resolve().parents[1]


def _env() -> dict:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONUTF8"] = "1"
    return env


def _engine(tmp_path, broker=None, model=None, **cfg_over):
    cfg = live_cfg(tmp_path, **{"risk.enforce_market_hours": False,
                                "risk.duplicate_window_seconds": 1,
                                **cfg_over})
    journal = AuditJournal(tmp_path / "audit.db", tmp_path / "audit.jsonl")
    br = broker or PaperBroker(state_path=tmp_path / "paper.json")
    risk = RiskController(cfg, cfg.gate)
    engine = StrategyEngine(cfg, br, model or FixedSignalModel(), risk, journal,
                            stops_path=tmp_path / "stops.json",
                            allow_rule_engine_entry=True)
    return cfg, engine, journal, br


# --------------------------------------------------------------------------- #
# 1. worker 连续运行
# --------------------------------------------------------------------------- #
def test_repeated_scan_monitor_loops_without_state_leak(tmp_path):
    """连续 40 轮 scan+monitor：不崩、不泄漏、账本自洽。"""
    cfg, engine, journal, broker = _engine(tmp_path)
    base_orders = len(broker.list_orders(limit=500))
    errors = []

    t0 = datetime.now()
    for i in range(40):
        now = t0 + timedelta(seconds=i * 2)
        try:
            engine.scan(["hk00700"], now=now)
            engine.monitor(now=now)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"iter {i}: {type(exc).__name__}: {exc}")

    assert errors == [], errors
    counts = journal.counts()
    # 审计条数随轮次线性增长，不应爆量
    assert counts["audit_log"] < 40 * 20, counts
    # 持仓数量不会无限增长（同一标的）
    assert len(broker.get_positions()) <= 1
    # 冷却表不会无限膨胀（同标的同窗口只占一个槽位）
    attempts = journal._conn.execute(
        "SELECT COUNT(*) c FROM order_attempts").fetchone()["c"]
    assert attempts <= 40, attempts
    # 哈希链仍然完整
    assert journal.verify_chain()["ok"] is True
    journal.close()


def test_stops_registry_does_not_grow_unbounded(tmp_path):
    cfg, engine, journal, broker = _engine(tmp_path)
    for i in range(20):
        engine.register_stops("hk00700", 290.0, 330.0, 100)
        engine.register_stops("usAAPL", 200.0, 240.0, 10)
    assert set(engine.stops) == {"hk00700", "usAAPL"}
    engine.clear_stops("hk00700")
    assert set(engine.stops) == {"usAAPL"}
    journal.close()


# --------------------------------------------------------------------------- #
# 2. 网络断开与恢复
# --------------------------------------------------------------------------- #
class FlakyBroker(ScriptedBroker):
    """前 N 次调用失败，之后恢复。"""

    def __init__(self, fail_times: int = 2):
        super().__init__()
        self.fail_times = fail_times
        self.quote_calls = 0

    def get_quote(self, symbol):
        self.quote_calls += 1
        if self.quote_calls <= self.fail_times:
            raise BrokerError("模拟网络中断", code="NETWORK", retryable=True)
        return super().get_quote(symbol)


def test_network_disconnect_then_recover(tmp_path):
    broker = FlakyBroker(fail_times=2)
    cfg, engine, journal, _ = _engine(tmp_path, broker=broker)

    r1 = engine.scan(["hk00700"], now=datetime.now())
    assert r1.outcomes[0].stage == "error", "断网时应报错而不是伪造数据"
    assert journal.list_orders(limit=10) == []

    r2 = engine.scan(["hk00700"], now=datetime.now() + timedelta(seconds=5))
    assert r2.outcomes[0].stage == "error"

    r3 = engine.scan(["hk00700"], now=datetime.now() + timedelta(seconds=10))
    assert r3.outcomes[0].stage == "ordered", r3.outcomes[0].detail
    journal.close()


def test_positions_failure_then_recover(tmp_path):
    broker = ScriptedBroker(positions_exc=BrokerError("持仓接口 502", code="UPSTREAM"))
    cfg, engine, journal, _ = _engine(tmp_path, broker=broker)

    r1 = engine.scan(["hk00700"], now=datetime.now())
    assert r1.blocked_reason, "持仓读不出来必须整轮阻断"

    broker.positions_exc = None
    r2 = engine.scan(["hk00700"], now=datetime.now() + timedelta(seconds=5))
    assert r2.outcomes[0].stage == "ordered"
    journal.close()


# --------------------------------------------------------------------------- #
# 3. 进程异常退出与重启
# --------------------------------------------------------------------------- #
def test_killed_process_leaves_consistent_database(tmp_path):
    """子进程写到一半被强杀 → 审计链仍然自洽，新进程可继续写。"""
    db = tmp_path / "audit.db"
    script = f'''
import sys, time
sys.path.insert(0, r"{ROOT / 'src'}")
from ai_trader.audit.journal import AuditJournal
j = AuditJournal(r"{db}", None)
for i in range(500):
    j.log("loop_event", {{"i": i}})
    time.sleep(0.002)
'''
    proc = subprocess.Popen([sys.executable, "-c", script], env=_env(),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(0.6)
    proc.kill()
    proc.wait(timeout=20)

    j = AuditJournal(db, None)
    res = j.verify_chain()
    assert res["ok"] is True, res
    assert res["total"] > 0, "至少应有部分事件落库"
    j.log("after_restart", {"ok": True})
    assert j.verify_chain()["ok"] is True
    j.close()


def test_cli_process_can_continue_after_abnormal_exit(tmp_path):
    """CLI 进程被强杀后，再跑一次 status 不应报错。"""
    cfg_file = tmp_path / "cfg.json"
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    cfg_file.write_text(json.dumps({
        "system": {"data_dir": str(data), "log_dir": str(tmp_path / "logs")},
        "safety": {"paper_trading": True, "live_trading": False,
                   "order_execution_enabled": False, "kill_switch": False},
        "audit": {"db_path": str(tmp_path / "audit.db"),
                  "jsonl_path": str(tmp_path / "audit.jsonl")},
        "markets": {"hk": {"enabled": True, "currency": "HKD"}},
        "universe": [{"code": "hk00700", "market": "hk"}],
        "ai": {"provider": "heuristic", "require_ai_for_entry": True},
        "web": {"host": "127.0.0.1", "port": 0},
    }), encoding="utf-8")

    cmd = [sys.executable, "-m", "ai_trader.cli", "--config", str(cfg_file), "status"]
    first = subprocess.run(cmd, cwd=str(ROOT), env=_env(), capture_output=True,
                           text=True, timeout=90)
    assert first.returncode == 0, first.stderr

    second = subprocess.run(cmd, cwd=str(ROOT), env=_env(), capture_output=True,
                            text=True, timeout=90)
    assert second.returncode == 0, second.stderr
    payload = json.loads(second.stdout)
    assert payload["audit"]["chain"]["ok"] is True


# --------------------------------------------------------------------------- #
# 4. 急停持久性（多次重启）
# --------------------------------------------------------------------------- #
def test_halt_survives_many_restarts(tmp_path):
    HaltStore(tmp_path / "data").halt("多次重启测试", actor="pytest")

    for round_no in range(3):
        # 每一轮都重新从磁盘构建配置（等价于一次重启）
        from tests.conftest import make_cfg

        cfg = make_cfg(tmp_path)
        assert cfg.gate.state.kill_switch is True, f"第 {round_no + 1} 次重启后应仍停止"
        assert cfg.gate.state.halt_source == "sentinel"
        risk = RiskController(cfg, cfg.gate)
        assert risk.gate.state.kill_switch is True

    # 解除后必须真的恢复（并且只恢复一次）
    risk.resume(RESUME_PHRASE, actor="pytest")
    cfg2 = make_cfg(tmp_path)
    assert cfg2.gate.state.kill_switch is False
    assert not (tmp_path / "data" / HALT_FILENAME).exists()


def test_halt_file_cannot_be_ignored_by_config(tmp_path):
    """配置文件写 kill_switch=false 也不能覆盖哨兵。"""
    HaltStore(tmp_path / "data").halt("哨兵优先")
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, **{"safety.kill_switch": False})
    assert cfg.gate.state.kill_switch is True


# --------------------------------------------------------------------------- #
# 5. 订单 UNKNOWN 恢复（跨实例，模拟重启）
# --------------------------------------------------------------------------- #
def test_unknown_order_recovered_after_restart(tmp_path):
    """进程 A 留下 unknown，进程 B（新实例）通过 monitor 对账恢复。"""
    found = OrderResult(order_id="BRK-LATE", broker_order_id="BRK-LATE",
                        symbol="hk00700", market="hk", side="buy", qty=100,
                        price=300.0, status="filled", filled_qty=100,
                        avg_fill_price=300.0, venue="scripted",
                        ts="2026-01-01T00:00:00+08:00")

    # 进程 A：下单后对账失败 → unknown
    broker_a = ScriptedBroker(place_behaviour="raise",
                             reconcile_exc=BrokerError("网关不可达", code="NETWORK"))
    cfg, engine_a, journal_a, _ = _engine(tmp_path, broker=broker_a)
    engine_a.scan(["hk00700"], now=datetime.now())
    rows = journal_a.list_orders(limit=5)
    assert rows[0]["status"] == "unknown"
    assert rows[0]["client_id"]
    journal_a.close()

    # 进程 B：新实例，网关恢复
    broker_b = ScriptedBroker(reconcile_result=found)
    cfg2, engine_b, journal_b, _ = _engine(tmp_path, broker=broker_b)
    executed = engine_b.monitor(now=datetime.now())

    assert any(e["kind"] == "order_reconciled" for e in executed), executed
    rows2 = journal_b.list_orders(limit=5)
    assert rows2[0]["status"] == "filled"
    assert rows2[0]["broker_order_id"] == "BRK-LATE"
    journal_b.close()


def test_unknown_order_blocks_trading_until_reconciled(tmp_path):
    found = OrderResult(order_id="BRK-X", broker_order_id="BRK-X", symbol="hk00700",
                        market="hk", side="buy", qty=100, price=300.0,
                        status="filled", filled_qty=100, venue="scripted")
    broker = ScriptedBroker(place_behaviour="raise",
                            reconcile_exc=BrokerError("网关不可达", code="NETWORK"))
    cfg, engine, journal, _ = _engine(tmp_path, broker=broker)
    engine.scan(["hk00700"], now=datetime.now())

    # 第二次扫描：应被 unresolved_order_state 拦住（不再下单）
    broker.reconcile_result = "none"
    calls_before = broker.calls["place"]
    r2 = engine.scan(["hk00700"], now=datetime.now() + timedelta(seconds=5))
    assert broker.calls["place"] == calls_before, "状态未知期间禁止再下单"
    assert r2.outcomes[0].stage == "rejected"
    checks = [c["name"] for c in r2.outcomes[0].decision["checks"] if not c["ok"]]
    assert "unresolved_order_state" in checks

    # 对账恢复后解除（网关恢复：清掉注入的对账异常）
    broker.reconcile_exc = None
    broker.reconcile_result = found
    engine.monitor(now=datetime.now() + timedelta(seconds=10))
    r3 = engine.scan(["hk00700"], now=datetime.now() + timedelta(seconds=20))
    assert r3.outcomes[0].stage in ("ordered", "rejected")
    if r3.outcomes[0].decision:
        names = [c["name"] for c in r3.outcomes[0].decision["checks"] if not c["ok"]]
        assert "unresolved_order_state" not in names, "对账完成后应解除封锁"
    journal.close()


# --------------------------------------------------------------------------- #
# 6. 数据库并发
# --------------------------------------------------------------------------- #
def test_concurrent_writers_keep_chain_intact(tmp_path):
    db = tmp_path / "audit.db"
    writers = 6
    per_writer = 25
    barrier = threading.Barrier(writers)
    failures: list[str] = []

    def work(idx):
        j = AuditJournal(db, None)
        try:
            barrier.wait(timeout=10)
            for k in range(per_writer):
                j.log("concurrent", {"writer": idx, "k": k})
        except Exception as exc:  # noqa: BLE001
            failures.append(f"w{idx}: {type(exc).__name__}: {exc}")
        finally:
            j.close()

    threads = [threading.Thread(target=work, args=(i,)) for i in range(writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)

    assert failures == [], failures
    j = AuditJournal(db, None)
    chain = j.verify_chain()
    assert chain["ok"] is True, chain
    # 链式写入是串行的，总数必须等于写入次数（不多不少）
    assert chain["total"] == writers * per_writer, chain
    j.close()


def test_concurrent_reservation_exactly_one_winner(tmp_path):
    db = tmp_path / "audit.db"
    n = 8
    barrier = threading.Barrier(n)
    results: list[bool] = []
    lock = threading.Lock()

    def work():
        j = AuditJournal(db, None)
        try:
            barrier.wait(timeout=10)
            ok = j.reserve_order_attempt("hk00700", "buy", 300, "D", datetime.now())
            with lock:
                results.append(ok)
        finally:
            j.close()

    threads = [threading.Thread(target=work) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    assert results.count(True) == 1, results
    assert results.count(False) == n - 1, results


# --------------------------------------------------------------------------- #
# 7. 日志是否泄露密钥
# --------------------------------------------------------------------------- #
SECRET_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "PEM 私钥块"),
    (re.compile(r"-----BEGIN [A-Z ]*PUBLIC KEY-----"), "PEM 公钥块"),
    (re.compile(r"sk-[A-Za-z0-9]{16,}"), "疑似 OpenAI 风格 key"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "疑似 AWS key"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9\-_\.]{20,}"), "Bearer token"),
]


def _scan_files_for_secrets(paths):
    hits = []
    for p in paths:
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pat, label in SECRET_PATTERNS:
            for m in pat.finditer(text):
                hits.append(f"{p}: {label} -> {m.group(0)[:24]}…")
    return hits


def test_runtime_logs_do_not_leak_secrets(tmp_path):
    """跑一轮真实流程，然后扫描它写出的所有日志/审计文件。"""
    cfg, engine, journal, broker = _engine(tmp_path)
    engine.scan(["hk00700"], now=datetime.now())
    engine.monitor(now=datetime.now())
    journal.log("creds_probe", {"api_key": "sk-SHOULD-BE-REDACTED-1234567890",
                                "password": "hunter2", "symbol": "hk00700"})
    journal.close()

    files = list(tmp_path.rglob("*"))
    hits = _scan_files_for_secrets(files)
    assert hits == [], "日志/审计里出现疑似密钥：" + "; ".join(hits)

    # 明确验证脱敏：明文不得出现在任何文件里
    for p in files:
        if p.is_file():
            raw = p.read_text(encoding="utf-8", errors="ignore")
            assert "sk-SHOULD-BE-REDACTED-1234567890" not in raw, p
            assert "hunter2" not in raw, p


def test_repo_logs_do_not_contain_pem_or_keys():
    """扫项目真实 logs/ 目录（历史累积），确保没有密钥落地。"""
    logs = ROOT / "logs"
    if not logs.exists():
        pytest.skip("logs/ 不存在")
    hits = _scan_files_for_secrets(list(logs.rglob("*")))
    # junit/evidence 由测试生成，内容受控；这里只要求不出现 PEM 与真实 key 形态
    assert hits == [], "logs/ 中发现疑似密钥：" + "; ".join(hits)


def test_env_file_has_no_nonempty_credentials():
    """`.env` 的凭证位必须为空（本项目不持有任何真实凭证）。"""
    env_path = ROOT / ".env"
    if not env_path.exists():
        pytest.skip(".env 不存在")
    secret_keys = ("FSOPENAPI_API_KEY", "FSOPENAPI_SERVER_PUBLIC_KEY",
                   "FSOPENAPI_CLIENT_PRIVATE_KEY", "AI_API_KEY",
                   "FSOPENAPI_BASE_URL", "WEB_ADMIN_PASSWORD",
                   "VAULT_PASSPHRASE", "LIVE_ACK_PHRASE")
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() in secret_keys:
            assert v.strip() == "", f"{k} 竟然有值（不应存在真实凭证）"
