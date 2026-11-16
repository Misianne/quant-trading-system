"""交易记录与审计日志。

两类记录
--------
* **业务记录**：信号 / 风控决策 / 委托 / 成交 / 权益快照 —— 存 SQLite，便于查询统计。
* **审计链**：每条事件追加哈希链（``prev_hash`` → ``hash``），JSONL 落盘 +
  SQLite 落库。任何一条被篡改，:meth:`AuditJournal.verify_chain` 都会报出行号。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id     TEXT NOT NULL UNIQUE,
    ts            TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    market        TEXT,
    action        TEXT NOT NULL,
    confidence    REAL,
    entry_price   REAL,
    stop_loss     REAL,
    take_profit   REAL,
    position_pct  REAL,
    provider      TEXT,
    model         TEXT,
    rationale     TEXT,
    payload       TEXT
);

CREATE TABLE IF NOT EXISTS risk_decisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT NOT NULL UNIQUE,
    ts          TEXT NOT NULL,
    symbol      TEXT,
    action      TEXT,
    approved    INTEGER NOT NULL,
    reasons     TEXT,
    checks      TEXT,
    requested_qty REAL,
    approved_qty  REAL
);

CREATE TABLE IF NOT EXISTS orders (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id       TEXT NOT NULL UNIQUE,
    ts             TEXT NOT NULL,
    symbol         TEXT NOT NULL,
    market         TEXT,
    side           TEXT NOT NULL,
    qty            REAL NOT NULL,
    price          REAL,
    order_type     TEXT,
    status         TEXT NOT NULL,
    filled_qty     REAL DEFAULT 0,
    avg_fill_price REAL,
    commission     REAL DEFAULT 0,
    venue          TEXT,
    broker_order_id TEXT,
    signal_id      TEXT,
    decision_id    TEXT,
    note           TEXT
);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    currency    TEXT NOT NULL,
    cash        REAL NOT NULL,
    market_value REAL NOT NULL,
    equity      REAL NOT NULL,
    day_pnl     REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS audit_log (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    event      TEXT NOT NULL,
    actor      TEXT,
    subject    TEXT,
    detail     TEXT,
    prev_hash  TEXT,
    hash       TEXT NOT NULL
);

-- 跨进程下单冷却：UNIQUE(symbol, side, bucket) 由数据库保证并发互斥
CREATE TABLE IF NOT EXISTS order_attempts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    side        TEXT NOT NULL,
    bucket      INTEGER NOT NULL,
    window_s    INTEGER NOT NULL,
    decision_id TEXT,
    UNIQUE(symbol, side, bucket)
);

-- US-P1 Batch2 R-1：下单意图预写日志（write-ahead order intent）
-- 目的：把"唯一 client_id + 请求参数 + 初始状态"在**调用券商之前**落盘，
-- 使订单在任何后续故障（超时/空 orderId/未知状态/落库失败/进程崩溃）下都
-- 能被恢复与对账；同时这张表就是"未完成卖单预留量"的唯一真相来源（R-2）。
CREATE TABLE IF NOT EXISTS order_intents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id       TEXT NOT NULL UNIQUE,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    market          TEXT,
    side            TEXT NOT NULL,
    qty             REAL NOT NULL,
    price           REAL,
    order_type      TEXT,
    currency        TEXT,
    stop_loss       REAL,
    take_profit     REAL,
    signal_id       TEXT,
    decision_id     TEXT,
    request_digest  TEXT,
    request_json    TEXT,
    state           TEXT NOT NULL,
    broker_order_id TEXT,
    broker_status   TEXT,
    last_error      TEXT,
    attempts        INTEGER DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_orders_ts ON orders(ts);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
CREATE INDEX IF NOT EXISTS idx_attempts_ts ON order_attempts(ts);
CREATE INDEX IF NOT EXISTS idx_intents_symbol ON order_intents(symbol);
CREATE INDEX IF NOT EXISTS idx_intents_state ON order_intents(state);
"""

#: 订单意图状态（**只有** terminal 会释放预留）
INTENT_STATE_INTENT = "intent"          # 已预写，尚未拿到券商结果
INTENT_STATE_ACCEPTED = "accepted"      # 券商明确受理（有 order_id / 明确状态）
INTENT_STATE_SUBMITTED = "submitted"    # 券商回报为在途
INTENT_STATE_UNKNOWN = "unknown"        # 结果不明（超时/空 orderId/异常）
INTENT_STATE_TERMINAL = "terminal"      # 已终结（成交/撤销/拒单），释放预留
INTENT_STATE_REJECTED = "rejected"      # 券商明确拒单（未进入市场），释放预留

#: 仍占用"未完成卖单预留量"的状态集合（unknown 也在内 —— 绝不自动释放）
INTENT_OPEN_STATES = (
    INTENT_STATE_INTENT, INTENT_STATE_ACCEPTED,
    INTENT_STATE_SUBMITTED, INTENT_STATE_UNKNOWN,
)

#: 需要"确认后才允许再开仓"的状态（未终结）
INTENT_UNRESOLVED_STATES = (
    INTENT_STATE_INTENT, INTENT_STATE_ACCEPTED,
    INTENT_STATE_SUBMITTED, INTENT_STATE_UNKNOWN,
)

#: 需要增量补列（不删表、不改历史数据）
_ORDER_COLUMN_MIGRATIONS = (
    ("client_id", "TEXT"),
    ("reconcile_note", "TEXT"),
    #: 归一化后的时间戳（epoch 秒）。
    #: 为什么需要：``ts`` 列里可能混有不同时区偏移（-05:00 / +08:00 / +00:00），
    #: 用字符串比较 ``ts >= ?`` 会把"昨天 20:15-05:00"（= 今天 01:15Z）算成今天，
    #: 导致 T+1 的"当日买入"统计错误、可卖数量被错误清零。
    ("ts_epoch", "REAL"),
)


def utcnow_iso() -> str:
    """统一时间基准（回放时跟随回放时钟，而不是系统真实时间）。"""
    try:
        from .. import timebase
        return timebase.now().astimezone().isoformat(timespec="seconds")
    except Exception:  # noqa: BLE001 - 极早期导入失败时退回系统时间
        return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _epoch(iso: Any) -> float:
    """把 ISO 时间串转成 epoch 秒；无法解析时返回 0（保守：不匹配任何查询）。"""
    if isinstance(iso, (int, float)):
        return float(iso)
    text = str(iso or "").strip()
    if not text:
        return 0.0
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.timestamp()


def _canonical(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


@dataclass
class AuditEntry:
    seq: int
    ts: str
    event: str
    actor: str
    subject: str
    detail: Dict[str, Any]
    prev_hash: str
    hash: str


class AuditJournal:
    """线程安全的记录器。一个实例对应一个 SQLite 文件。"""

    def __init__(self, db_path: str | Path, jsonl_path: str | Path | None = None,
                 hash_chain: bool = True):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = Path(jsonl_path) if jsonl_path else None
        if self.jsonl_path:
            self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        self.hash_chain = hash_chain
        self._lock = threading.RLock()
        self._conn = self._open_and_init()

    def _open_and_init(self):
        """建立连接并初始化 schema。

        并发场景（多个 worker 同时启动、或多进程首次建库）下，DDL 会互相打断
        —— Windows 上实测会抛 ``attempt to write a readonly database``。
        这里做有限次重连重试：DDL 是幂等的，重试安全。
        """
        last_exc: Optional[BaseException] = None
        for attempt in range(6):
            try:
                conn = sqlite3.connect(str(self.db_path), check_same_thread=False,
                                       timeout=15.0, isolation_level=None)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA busy_timeout = 15000")
                try:
                    conn.execute("PRAGMA journal_mode = WAL")
                except sqlite3.OperationalError:
                    pass          # 另一个连接正在建库，WAL 稍后再设
                conn.executescript(SCHEMA)
                self._migrate_columns_on(conn)
                conn.commit()
                return conn
            except sqlite3.OperationalError as exc:
                last_exc = exc
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(0.05 * (attempt + 1))
        raise last_exc if last_exc else RuntimeError("初始化审计库失败")

    # ---- 写操作容错 ---------------------------------------------------- #
    #: 可恢复的 SQLite 错误特征（并发/锁冲突/WAL 切换竞态）
    _RECOVERABLE = ("readonly", "locked", "busy", "unable to open")

    @classmethod
    def _recoverable(cls, exc: BaseException) -> bool:
        msg = str(exc).lower()
        return any(k in msg for k in cls._RECOVERABLE)

    def _reconnect_locked(self) -> None:
        """在当前锁内重建连接（不回放 DDL，库已存在）。"""
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False,
                               timeout=15.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 15000")
        self._conn = conn

    def _retry_write(self, fn, attempts: int = 6):
        """执行一次写操作；遇到锁/WAL 竞态时重连并重试。

        背景（本机实测）：多个连接**同时**首次打开同一库时，有的连接建在
        ``delete`` 日志模式、有的切到 ``WAL``，前者之后写入会返回
        ``attempt to write a readonly database``。重连即可自愈。
        """
        last: Optional[BaseException] = None
        for attempt in range(attempts):
            with self._lock:
                try:
                    result = fn(self._conn)
                    self._conn.commit()
                    return result
                except sqlite3.OperationalError as exc:
                    last = exc
                    if not self._recoverable(exc) or attempt == attempts - 1:
                        raise
                    self._reconnect_locked()
            time.sleep(0.05 * (attempt + 1))
        raise last if last else RuntimeError("写操作失败")

    def _migrate_columns(self) -> None:
        self._migrate_columns_on(self._conn)

    @staticmethod
    def _migrate_columns_on(conn) -> None:
        """增量补列，保留全部历史数据（不删表、不重建）。"""
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(orders)")}
        added = []
        for name, ddl in _ORDER_COLUMN_MIGRATIONS:
            if name not in existing:
                conn.execute(f"ALTER TABLE orders ADD COLUMN {name} {ddl}")
                added.append(name)
        # ts_epoch 回填：老库里的行按 ts 解析一次（只做一次，之后新写入自带值）
        if "ts_epoch" in added:
            rows = conn.execute(
                "SELECT order_id, ts FROM orders WHERE ts_epoch IS NULL").fetchall()
            for r in rows:
                conn.execute("UPDATE orders SET ts_epoch = ? WHERE order_id = ?",
                             (_epoch(r["ts"]), r["order_id"]))

    # ------------------------------------------------------------------ #
    # 低层
    # ------------------------------------------------------------------ #
    def _last_hash(self) -> str:
        row = self._conn.execute(
            "SELECT hash FROM audit_log ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        return row["hash"] if row else "GENESIS"

    def _hash(self, ts: str, event: str, actor: str, subject: str,
              detail_json: str, prev: str) -> str:
        return hashlib.sha256(
            f"{prev}|{ts}|{event}|{actor}|{subject}|{detail_json}".encode("utf-8")
        ).hexdigest()

    def log(self, event: str, detail: Optional[Dict[str, Any]] = None,
            actor: str = "system", subject: str = "") -> str:
        """追加一条审计事件，返回其 hash。"""
        from ..secrets import redact

        ts = utcnow_iso()
        payload = redact(detail or {})
        detail_json = _canonical(payload)
        def _append(conn):
            # 关键：读 prev 与 写新行必须在同一个**写事务**内，
            # 否则两个进程会读到同一个 prev，哈希链立刻断（本机实测 seq=14 断链）。
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
                prev = (row["hash"] if row else "GENESIS") if self.hash_chain else ""
                digest = self._hash(ts, event, actor, subject, detail_json, prev)
                conn.execute(
                    "INSERT INTO audit_log (ts, event, actor, subject, detail, prev_hash, hash)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (ts, event, actor, subject, detail_json, prev, digest),
                )
                conn.commit()
                return prev, digest
            except BaseException:
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
                raise

        prev, h = self._retry_write(_append)
        if self.jsonl_path:
            with self.jsonl_path.open("a", encoding="utf-8") as fh:
                fh.write(_canonical({
                    "ts": ts, "event": event, "actor": actor,
                    "subject": subject, "detail": payload,
                    "prev_hash": prev, "hash": h,
                }) + "\n")
        return h

    # ------------------------------------------------------------------ #
    # 业务记录
    # ------------------------------------------------------------------ #
    def record_signal(self, signal: Dict[str, Any]) -> str:
        sid = signal.get("signal_id") or uuid.uuid4().hex[:16]
        if True:
            self._retry_write(lambda c: c.execute(
                "INSERT OR REPLACE INTO signals (signal_id, ts, symbol, market, action,"
                " confidence, entry_price, stop_loss, take_profit, position_pct, provider,"
                " model, rationale, payload) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    sid, signal.get("ts") or utcnow_iso(), signal.get("symbol"),
                    signal.get("market"), signal.get("action"),
                    signal.get("confidence"), signal.get("entry_price"),
                    signal.get("stop_loss"), signal.get("take_profit"),
                    signal.get("position_pct"), signal.get("provider"),
                    signal.get("model"), signal.get("rationale"),
                    _canonical(signal),
                ),
            ))
        self.log("signal", {"signal_id": sid, "symbol": signal.get("symbol"),
                            "action": signal.get("action"),
                            "confidence": signal.get("confidence")}, subject=signal.get("symbol", ""))
        return sid

    def record_risk_decision(self, decision: Dict[str, Any]) -> str:
        did = decision.get("decision_id") or uuid.uuid4().hex[:16]
        if True:
            self._retry_write(lambda c: c.execute(
                "INSERT OR REPLACE INTO risk_decisions (decision_id, ts, symbol, action,"
                " approved, reasons, checks, requested_qty, approved_qty)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    did, decision.get("ts") or utcnow_iso(), decision.get("symbol"),
                    decision.get("action"), 1 if decision.get("approved") else 0,
                    _canonical(decision.get("reasons", [])),
                    _canonical(decision.get("checks", {})),
                    decision.get("requested_qty"), decision.get("approved_qty"),
                ),
            ))
        self.log("risk_decision", {"decision_id": did,
                                   "approved": bool(decision.get("approved")),
                                   "reasons": decision.get("reasons", [])},
                 subject=decision.get("symbol", ""))
        return did

    def record_order(self, order: Dict[str, Any]) -> str:
        oid = order.get("order_id") or uuid.uuid4().hex[:16]
        _ts = order.get("ts") or utcnow_iso()
        if True:
            self._retry_write(lambda c: c.execute(
                "INSERT OR REPLACE INTO orders (order_id, ts, symbol, market, side, qty,"
                " price, order_type, status, filled_qty, avg_fill_price, commission,"
                " venue, broker_order_id, signal_id, decision_id, note, client_id,"
                " reconcile_note, ts_epoch)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    oid, _ts, order.get("symbol"),
                    order.get("market"), order.get("side"), order.get("qty"),
                    order.get("price"), order.get("order_type"), order.get("status"),
                    order.get("filled_qty", 0), order.get("avg_fill_price"),
                    order.get("commission", 0), order.get("venue"),
                    order.get("broker_order_id"), order.get("signal_id"),
                    order.get("decision_id"), order.get("note"),
                    order.get("client_id"), order.get("reconcile_note"),
                    _epoch(_ts),
                ),
            ))
        self.log("order", order, subject=order.get("symbol", ""))
        return oid

    # ------------------------------------------------------------------ #
    # 订单状态核对 / 超时 / 跨进程冷却
    # ------------------------------------------------------------------ #
    def update_order_status(self, order_id: str, status: str,
                            **fields: Any) -> bool:
        """更新订单状态（核对流程与超时撤单使用）。保留原记录，不删行。"""
        sets = ["status = ?"]
        args: List[Any] = [status]
        for k in ("broker_order_id", "filled_qty", "avg_fill_price", "commission",
                  "reconcile_note", "client_id"):
            if k in fields:
                sets.append(f"{k} = ?")
                args.append(fields[k])
        args.append(order_id)
        cur = self._retry_write(lambda c: c.execute(
            f"UPDATE orders SET {', '.join(sets)} WHERE order_id = ?", args))
        changed = cur.rowcount > 0
        if changed:
            self.log("order_status_update",
                     {"order_id": order_id, "status": status, **fields},
                     subject=str(fields.get("symbol", "")))
        return changed

    def find_orders(self, statuses: Optional[List[str]] = None,
                    symbol: Optional[str] = None,
                    older_than: Optional[str] = None,
                    limit: int = 200) -> List[Dict[str, Any]]:
        """按状态/标的/时间筛选订单（ts 为 ISO 字符串，可直接比较）。"""
        sql = "SELECT * FROM orders WHERE 1=1"
        args: List[Any] = []
        if statuses:
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            args.extend(statuses)
        if symbol:
            sql += " AND lower(symbol) = ?"
            args.append(symbol.lower())
        if older_than:
            sql += " AND ts <= ?"
            args.append(older_than)
        sql += " ORDER BY id ASC LIMIT ?"
        args.append(limit)
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def reserve_order_attempt(self, symbol: str, side: str, window_seconds: int,
                              decision_id: str = "", now: Optional[Any] = None) -> bool:
        """原子占用一个下单冷却槽位。

        返回 ``True`` = 占位成功；``False`` = 窗口内已有同标的同方向委托
        （由 ``UNIQUE(symbol, side, bucket)`` 保证，**跨进程**同样生效）。
        其它数据库异常向上抛出，由调用方按"拒绝"处理。
        """
        now = now or datetime.now(timezone.utc).astimezone()
        window = max(1, int(window_seconds))
        bucket = int(now.timestamp()) // window
        try:
            self._retry_write(lambda c: c.execute(
                "INSERT INTO order_attempts (ts, symbol, side, bucket, window_s, decision_id)"
                " VALUES (?,?,?,?,?,?)",
                (now.astimezone().isoformat(timespec="seconds"),
                 symbol.lower(), side, bucket, window, decision_id),
            ))
            return True
        except sqlite3.IntegrityError:
            return False

    def release_order_attempt(self, decision_id: str) -> bool:
        """下单最终失败时释放槽位，避免无谓地阻塞 300 秒。"""
        if not decision_id:
            return False
        cur = self._retry_write(lambda c: c.execute(
            "DELETE FROM order_attempts WHERE decision_id = ?", (decision_id,)))
        return cur.rowcount > 0

    def record_equity(self, currency: str, cash: float, market_value: float,
                      equity: float, day_pnl: float = 0.0) -> None:
        if True:
            self._retry_write(lambda c: c.execute(
                "INSERT INTO equity_snapshots (ts, currency, cash, market_value, equity, day_pnl)"
                " VALUES (?,?,?,?,?,?)",
                (utcnow_iso(), currency, cash, market_value, equity, day_pnl),
            ))

    # ------------------------------------------------------------------ #
    # 下单意图预写日志（US-P1 Batch2 R-1 / R-2）
    # ------------------------------------------------------------------ #
    def prewrite_intent(self, intent: Dict[str, Any],
                        sell_capacity: Optional[float] = None
                        ) -> Tuple[bool, str]:
        """在下单**之前**持久化意图；同时对卖出做原子预留。

        返回 ``(ok, reason)``：

        * ``(True, "ok")``            —— 预写成功，可以进入券商调用阶段
        * ``(False, "duplicate:...")`` —— 同一 client_id 已存在 → **必须阻断**，
          否则两个 worker 会对同一决策各发一次单
        * ``(False, "capacity:...")``  —— 卖出预留超额（并发/多笔未完成卖单合并计算）
        * 异常时**向上抛出**：调用方必须视为"不可下单"

        ``sell_capacity`` 非空时，在**同一个写事务**内做
        ``SUM(未完成卖单) + 本单 <= 已核实持仓`` 的检查，
        由 SQLite 的 ``BEGIN IMMEDIATE`` 保证两个 worker 不会同时抢到同一份额度。
        """
        cid = str(intent.get("client_id") or "")
        if not cid:
            raise ValueError("prewrite_intent 需要 client_id")
        sym = str(intent.get("symbol") or "")
        side = str(intent.get("side") or "").lower()
        qty = float(intent.get("qty") or 0)
        now = intent.get("created_at") or utcnow_iso()
        req_json = _canonical(intent.get("request") or {})
        digest = hashlib.sha256(req_json.encode("utf-8")).hexdigest()

        def _do(conn):
            conn.execute("BEGIN IMMEDIATE")
            try:
                if side == "sell" and sell_capacity is not None:
                    placeholders = ",".join("?" * len(INTENT_OPEN_STATES))
                    row = conn.execute(
                        f"SELECT COALESCE(SUM(qty),0) AS s FROM order_intents"
                        f" WHERE lower(symbol)=? AND side='sell' AND state IN ({placeholders})",
                        (sym.lower(), *INTENT_OPEN_STATES)).fetchone()
                    reserved = float(row["s"] or 0.0) if row else 0.0
                    capacity = float(sell_capacity)
                    if reserved + qty > capacity + 1e-9:
                        conn.rollback()
                        return (False,
                                f"capacity: 未完成卖单已预留 {reserved:g} + 本单 {qty:g}"
                                f" > 已核实持仓 {capacity:g}")
                conn.execute(
                    "INSERT INTO order_intents (client_id, created_at, updated_at, symbol,"
                    " market, side, qty, price, order_type, currency, stop_loss, take_profit,"
                    " signal_id, decision_id, request_digest, request_json, state, attempts)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                    (cid, now, now, sym, intent.get("market"), side, qty,
                     intent.get("price"), intent.get("order_type"),
                     intent.get("currency"), intent.get("stop_loss"),
                     intent.get("take_profit"), intent.get("signal_id"),
                     intent.get("decision_id"), digest, req_json,
                     INTENT_STATE_INTENT))
                conn.commit()
                return (True, "ok")
            except sqlite3.IntegrityError:
                # client_id 已存在 → 同一决策被重复提交（多 worker / 重启重试）
                # **必须阻断**：否则会对同一 client_id 发两次单。
                conn.rollback()
                try:
                    row = conn.execute(
                        "SELECT state FROM order_intents WHERE client_id = ?",
                        (cid,)).fetchone()
                    prev = str(row["state"]) if row else "?"
                except Exception:  # noqa: BLE001
                    prev = "?"
                return (False,
                        f"duplicate: client_id {cid} 已存在（state={prev}），"
                        f"同一决策不得重复发单")
            except BaseException:
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
                raise

        return self._retry_write(_do)

    def update_intent(self, client_id: str, state: str, *,
                      broker_order_id: Optional[str] = None,
                      broker_status: Optional[str] = None,
                      last_error: Optional[str] = None,
                      bump_attempt: bool = False) -> bool:
        """更新意图状态。**永不删除行**（终态只是改 state）。"""
        if not client_id:
            return False
        sets = ["state = ?", "updated_at = ?"]
        args: List[Any] = [state, utcnow_iso()]
        if broker_order_id is not None:
            sets.append("broker_order_id = ?")
            args.append(broker_order_id)
        if broker_status is not None:
            sets.append("broker_status = ?")
            args.append(broker_status)
        if last_error is not None:
            sets.append("last_error = ?")
            args.append(str(last_error)[:500])
        if bump_attempt:
            sets.append("attempts = attempts + 1")
        args.append(client_id)
        cur = self._retry_write(lambda c: c.execute(
            f"UPDATE order_intents SET {', '.join(sets)} WHERE client_id = ?", args))
        return cur.rowcount > 0

    def sync_order_status_for_client(self, client_id: str, status: str,
                                     **fields: Any) -> int:
        """把 ``orders`` 表里同一 client_id 的行同步到意图的最新状态。

        意图表是权威，但 ``orders`` 行若长期停留在 ``unknown``，
        会让后续"未完成委托"判定一直命中 —— 必须保持一致。
        """
        if not client_id:
            return 0
        sets = ["status = ?"]
        args: List[Any] = [status]
        for k, v in fields.items():
            if k in ("broker_order_id", "filled_qty", "avg_fill_price", "reconcile_note"):
                sets.append(f"{k} = ?")
                args.append(v)
        args.append(client_id)
        cur = self._retry_write(lambda c: c.execute(
            f"UPDATE orders SET {', '.join(sets)} WHERE client_id = ?", args))
        return cur.rowcount

    def sync_order_status_for_client(self, client_id: str, status: str,
                                     **fields: Any) -> int:
        """把 ``orders`` 表里同一 client_id 的行同步到意图的最新状态。

        意图表是权威，但 ``orders`` 行若长期停留在 ``unknown``，
        会让后续"未完成委托"判定一直命中 —— 必须保持一致。
        """
        if not client_id:
            return 0
        sets = ["status = ?"]
        args: List[Any] = [status]
        for k, v in fields.items():
            if k in ("broker_order_id", "filled_qty", "avg_fill_price", "reconcile_note"):
                sets.append(f"{k} = ?")
                args.append(v)
        args.append(client_id)
        cur = self._retry_write(lambda c: c.execute(
            f"UPDATE orders SET {', '.join(sets)} WHERE client_id = ?", args))
        return cur.rowcount

    def get_intent(self, client_id: str) -> Optional[Dict[str, Any]]:
        if not client_id:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM order_intents WHERE client_id = ?", (client_id,)).fetchone()
        return dict(row) if row else None

    def list_open_intents(self, symbol: Optional[str] = None,
                          limit: int = 200) -> List[Dict[str, Any]]:
        """所有未终结的意图（intent/accepted/submitted/unknown）。"""
        placeholders = ",".join("?" * len(INTENT_OPEN_STATES))
        sql = (f"SELECT * FROM order_intents WHERE state IN ({placeholders})")
        args: List[Any] = list(INTENT_OPEN_STATES)
        if symbol:
            sql += " AND lower(symbol) = ?"
            args.append(symbol.lower())
        sql += " ORDER BY id ASC LIMIT ?"
        args.append(limit)
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def open_sell_reserved(self, symbol: Optional[str] = None
                           ) -> Tuple[Dict[str, float], bool]:
        """未完成卖单预留量：``{symbol: qty}``，以及"是否读成功"。

        ``ok=False`` 表示**读不出来** —— 调用方必须按"预留量未知"处理（禁止再卖），
        绝不能当作 0。
        """
        placeholders = ",".join("?" * len(INTENT_OPEN_STATES))
        sql = ("SELECT lower(symbol) AS sym, COALESCE(SUM(qty),0) AS s"
               f" FROM order_intents WHERE side='sell' AND state IN ({placeholders})")
        args: List[Any] = list(INTENT_OPEN_STATES)
        if symbol:
            sql += " AND lower(symbol) = ?"
            args.append(symbol.lower())
        sql += " GROUP BY lower(symbol)"
        try:
            with self._lock:
                rows = self._conn.execute(sql, args).fetchall()
            return {str(r["sym"]): float(r["s"] or 0.0) for r in rows}, True
        except sqlite3.Error:
            return {}, False

    def recover_open_intents(self) -> List[Dict[str, Any]]:
        """重启恢复：把所有未终结意图提升为 ``unknown`` 并返回。

        **不删除任何行**；``intent``（预写后未拿到结果）与 ``accepted/submitted``
        （拿到过受理但未终结）在重启后都**不能**假定成功或失败，
        统一标为 ``unknown``，迫使对账后才允许再交易。
        """
        out: List[Dict[str, Any]] = []
        for row in self.list_open_intents():
            cid = str(row.get("client_id") or "")
            prev = str(row.get("state") or "")
            try:
                self.update_intent(cid, INTENT_STATE_UNKNOWN,
                                   last_error=f"进程重启恢复：原状态 {prev}，结果未知")
            except Exception:  # noqa: BLE001
                pass
            row["state"] = INTENT_STATE_UNKNOWN
            row["previous_state"] = prev
            out.append(row)
        return out

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def list_orders(self, limit: int = 50, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM orders"
        args: List[Any] = []
        if symbol:
            sql += " WHERE symbol = ?"
            args.append(symbol)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def list_signals(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(
                "SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]

    def list_risk_decisions(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(
                "SELECT * FROM risk_decisions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]

    def list_audit(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM audit_log ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["detail"] = json.loads(d["detail"] or "{}")
            except ValueError:
                pass
            out.append(d)
        return out

    def counts(self) -> Dict[str, int]:
        with self._lock:
            return {
                t: self._conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
                for t in ("signals", "risk_decisions", "orders", "audit_log", "equity_snapshots")
            }

    def orders_since(self, iso_ts: str) -> List[Dict[str, Any]]:
        """返回 ``ts >= iso_ts`` 的订单。

        **按归一化 epoch 比较**，不做字符串比较 —— 混用时区偏移时字符串比较
        会把不同日期的订单算进"今天"（T+1 可卖数量因此被错误清零）。
        """
        since = _epoch(iso_ts)
        with self._lock:
            return [dict(r) for r in self._conn.execute(
                "SELECT * FROM orders WHERE ts_epoch IS NOT NULL AND ts_epoch >= ?"
                " ORDER BY id ASC", (since,)).fetchall()]

    # ------------------------------------------------------------------ #
    # 审计链校验
    # ------------------------------------------------------------------ #
    def verify_chain(self) -> Dict[str, Any]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, ts, event, actor, subject, detail, prev_hash, hash"
                " FROM audit_log ORDER BY seq ASC"
            ).fetchall()
        prev = "GENESIS"
        for r in rows:
            expected = self._hash(r["ts"], r["event"], r["actor"] or "",
                                  r["subject"] or "", r["detail"], prev if self.hash_chain else "")
            if self.hash_chain and (r["prev_hash"] != prev or r["hash"] != expected):
                return {"ok": False, "broken_at_seq": r["seq"], "total": len(rows)}
            prev = r["hash"]
        return {"ok": True, "broken_at_seq": None, "total": len(rows)}

    def close(self) -> None:
        with self._lock:
            self._conn.close()
