# USL-01 真实推理证据链与来源防伪 — 修复方案（只读设计）

| 项 | 值 |
|---|---|
| 日期 | 2026-11-10 |
| 项目 | `C:\FosunAI-Trading` |
| 依据 | `US_REAL_LLM_PAPER_INDEPENDENT_AUDIT.md`（PARTIAL）、`audit_evidence/us_loop_real_llm/independent_audit.json`、源码与账本只读审计 |
| 性质 | **方案文档**。本轮未修改任何源码/测试/配置/账本/证据（见 §15 零改动证明） |
| 结论 | 4 个 P0 + 2 个 P1 缺陷均可修，但**必须先修 P0-B（端点防伪）**——新发现：上次"真实调用"实际走的是本机回环代理，`endpoint_class=remote` 在传输层不成立 |
| 不改的范围 | F-USL-02（部分成交）、F-USL-03（美股 T+0）明确排除 |

---

## 1. 现状调用链（文件:行号）

### 1.1 请求发出与响应接收

```
ai/providers.py:290  OpenAICompatibleModel.analyze(ctx)
  ├─ :296-299  messages = [system, user]            （提示词）
  ├─ :301      data = self._post(messages)          ← 唯一调用点
  └─ :303-336  解析 choices[0].message.content → TradingSignal

ai/providers.py:252  _post(messages) -> Dict
  ├─ :255      url = f"{base_url}/chat/completions"
  ├─ :257-268  body = {model, messages, temperature, max_tokens, response_format}
  ├─ :269-273  headers = {Authorization: Bearer <key>, Content-Type}
  ├─ :272      resp = requests.post(url, json=body, headers=headers, timeout=(c,r))
  ├─ :278      self.last_latency_ms = ...
  ├─ :280      if resp.status_code >= 400: raise AIModelError
  └─ :285      return resp.json()                   ← 只返回 body，**丢弃 status/headers/原始字节**
```

**关键点：`_post` 的返回值只有解析后的 dict。** 方法、路径、状态码、响应头、`id`、原始字节在函数返回时全部丢失，`analyze` 与后续链路无从获知。

### 1.2 决策落库

```
us_loop/decisions.py:412  AIGateway.record_signal(ctx, signal, decided_at, latency_ms)
  ├─ :418-436  计算 target_qty / risk_conditions
  ├─ :438-443  构造 AIDecision(provenance=self.provenance, usage=model.last_usage, ...)
  └─ :445      self.ledger.record_decision(d)

us_loop/decisions.py:250  DecisionLedger.record_decision(d)
  └─ :252-268  单条 INSERT INTO ai_decisions(...)，**无事务、无伴生证据表**
```

### 1.3 来源判定与就绪

```
us_loop/decisions.py:56   classify_endpoint(base_url)   ← urlparse().hostname 与 4 个字面量比较
us_loop/decisions.py:70   classify_provenance(model, mode)
us_loop/decisions.py:359  AIGateway.__init__ → self.provenance = classify_provenance(...)
                          （仅当 prov==real_llm 且 endpoint_class!=remote 时降级）
us_loop/runner.py:342     readiness() → ai_model_called.met = 是否存在 provenance==real_llm 的行
```

### 1.4 写入路径装配

```
us_loop/runner.py:107-161  PaperLoop.__init__
  ├─ :123  PaperBroker(state_path=settings.resolved_paper_state())
  ├─ :133-143  audit_db / audit_jsonl（sandbox 时落 data_dir）
  ├─ :149  DecisionLedger(settings.loop_db)
  ├─ :161  stops_path = settings.data_dir / "stops.json"
  └─ :165-170  _relocate(cfg, data_dir) → 只改 system.data_dir
strategy/engine.py:135     FaultRecorder(cfg.data_dir, log_dir=cfg.log_dir)   ← log_dir 未重定向
config.py:220/226          data_dir / log_dir 属性都带 mkdir(parents=True, exist_ok=True)
```

---

## 2. 缺陷与复现依据

### D1 — 传输证据从未落盘（P0-A）

- `_post` 只返回 body（`providers.py:285`）；`ai_decisions` 27 列（`decisions.py:92-121`）无任何 `http_*` / `response_id` / `envelope` 字段。
- 证据 JSON 顶层键仅 `generated_at/settings/market_data/quote_source/model/account/positions/decisions/runs/journal_counts/readiness/restart`。
- 复现：对 18:30 那笔决策，无法从任何工件取回 `POST`、`/v1/chat/completions`、`200`、上游 `id`。

### D2 — 端点判定只看字符串（P0-B）

`classify_endpoint` 仅比对 `{127.0.0.1, localhost, ::1, 0.0.0.0}`。只读探针（纯函数，不写盘）结果：

| 输入 | 现判定 | 应为 |
|---|---|---|
| `192.168.1.5` | remote → `real_llm` | 非公网 |
| `10.0.0.7` | remote → `real_llm` | 非公网 |
| `127.0.0.1.nip.io` | remote → `real_llm` | 回环别名 |
| `127.1` | remote → `real_llm` | 回环简写 |
| `2130706433` | remote → `real_llm` | 回环整数形式 |

### D3 — 代理改变了实际连接目标（P0-B，**新发现，优先级最高**）

本机环境变量：

```
HTTP_PROXY / HTTPS_PROXY / http_proxy / https_proxy = http://127.0.0.1:54481   （loopback）
```

`requests.utils.get_environ_proxies("https://wb2.wanxingrumeng.online/v1/chat/completions")`
→ `{'https': 'http://127.0.0.1:54481', 'http': 'http://127.0.0.1:54481'}`，
且 `requests.Session().trust_env` 默认 `True`；`providers.py:272` 用的是 `requests.post`（同默认）。

**推论（强证据，非猜测）**：18:30 那次调用的 TCP 对端是 `127.0.0.1:54481`，不是任何公网地址。
因此该次运行的 `endpoint_class=remote` 在**传输层不成立**，`real_llm` 标记过强。
这不否定"上游确实产生过推理"（代理转发到真实中转），但否定"我们验证过远端对端"。

> 这也解释了为什么"审计拿不到传输证据"——现有代码既没有记录，也没有验证；记录到的只是主机名字符串。

### D4 — readiness 只看字段（P0-C）

`runner.py:345-356`：

```python
real_rows = [d for d in self.ledger.list_decisions(limit=200)
             if d.get("provenance") == "real_llm"]
checks["ai_model_called"] = {"met": bool(real_rows), ...}
```

注释声称"provenance=real_llm **且 endpoint_class=remote**"，代码只过滤前者。
配合 D2/D3，可 `met=true` 而无任何可验证的远端调用。

### D5 — 沙盒漏 `log_dir`（P1-D）

`_relocate`（`runner.py:165-170`）只改 `system.data_dir`；`FaultRecorder(cfg.data_dir, log_dir=cfg.log_dir)`（`engine.py:135`）仍指项目 `logs/`。
本次未触发故障所以无写入，但**故障即写回项目目录**。另外 `config.py:226` 的 `log_dir` 属性带 `mkdir`，仅"读一下"就会创建目录。

### D6 — 上一份报告两处不实（P1-E）

| 位置 | 报告写法 | 实际 |
|---|---|---|
| 报告 L62 | "395（含 **27** 个 reasoning tokens）" | 账本 `completion_thinking_tokens=313`（27 是 18:29 冒烟调用的值） |
| 报告 L153 | 执行层订单"reason 均带 `[EXEC-LAYER-TEST]` 前缀，账本里一眼可辨" | `orders.note=NULL`，`risk_decisions.reasons=[]`，标签**未落库** |

---

## 3. P0-A：真实 HTTP 传输证据

### 3.1 传输客户端重构（新增 `us_loop/transport.py`）

不再用 `requests.post`（无法控代理/重定向/对端）。改用标准库，**逐项可控**：

```python
class TransportResult:            # 只描述"发生了什么"，不含任何秘密
    method: str                   # 固定 "POST"
    path: str                     # "/chat/completions"（相对 base_url，不含 query）
    status: int
    received_at: str              # 收到首字节时刻（早于 JSON 解析）
    latency_ms: float
    body_bytes: bytes             # 原始响应体（用完即弃，不落库）
    envelope_sha256: str          # sha256(body_bytes)
    envelope_bytes: int
    upstream_model: str           # 响应体 "model"，缺失为 ""
    upstream_id: str              # 响应体 "id" 或 header x-request-id
    upstream_usage: dict          # 响应体 "usage" 原样
    headers_whitelist: dict       # 仅 content-type / x-request-id / cf-ray / date
    peer_ip: str                  # 实际 TCP 对端
    peer_ip_global: bool | None
    proxy_used: str               # "none" | "env:<host:port>" | "explicit:<host:port>"
    redirect_count: int
    tls_spki_sha256: str          # 叶子证书 SPKI 摘要（可选校验）
```

要点：
1. **无代理**：构造时 `proxies={}`、不读环境变量；检测到 `HTTP(S)_PROXY` 存在时记录 `proxy_env_present=True` 并在证据里写成 `proxy_used="env:..."`（**不启用**）。
2. **不跟随重定向**：`allow_redirects=False` 语义；3xx 直接判失败并记录 `Location` 的主机类别。
3. **对端可见**：连接后立即 `sock.getpeername()[0]`，连接级拿到真实对端（不是 hostname 推断）。
4. **可选 IP 钉死**：先解析出 IP，再 `HTTPSConnection(host=ip)` + `ctx.wrap_socket(sock, server_hostname=hostname)` 保留 SNI 与证书校验，彻底消除 DNS rebinding 窗口。
5. **秘密零落库**：`Authorization` 只存在于请求构造的局部变量，永不进入 `TransportResult`；白名单头也只存 4 个。

### 3.2 新表 `ai_transport_evidence`（append-only）

```sql
CREATE TABLE ai_transport_evidence (
    evidence_id     TEXT PRIMARY KEY,   -- sha256(decision_id|seq)
    decision_id     TEXT NOT NULL,
    seq             INTEGER NOT NULL,   -- 同决策的重试序号，从 1 开始
    run_id          TEXT,
    symbol          TEXT,
    http_method     TEXT NOT NULL,
    request_path    TEXT NOT NULL,
    http_status     INTEGER NOT NULL,
    upstream_model  TEXT,               -- 响应体 model（禁止用请求侧 model_id 填充）
    upstream_id     TEXT,               -- 响应体 id / x-request-id
    envelope_sha256 TEXT NOT NULL,
    envelope_bytes  INTEGER NOT NULL,
    usage_json      TEXT,
    prompt_tokens   INTEGER,
    completion_tokens INTEGER,
    latency_ms      REAL NOT NULL,
    received_at     TEXT NOT NULL,
    endpoint_class  TEXT NOT NULL,      -- remote / loopback / private / unknown
    peer_ip         TEXT,
    peer_ip_global  INTEGER,            -- 1/0/NULL
    proxy_used      TEXT NOT NULL,
    redirect_count  INTEGER NOT NULL DEFAULT 0,
    tls_spki_sha256 TEXT,
    provenance      TEXT NOT NULL,      -- real_llm / stub_transport / rejected / unknown
    integrity_hash  TEXT NOT NULL,      -- 链式：sha256(prev_integrity + 本行规范串)
    created_at      TEXT NOT NULL
);
CREATE UNIQUE INDEX uq_evidence_decision_seq ON ai_transport_evidence(decision_id, seq);
```

`ai_decisions` 增加两列（只加列，不动历史行）：`evidence_id TEXT`、`transport_verified INTEGER DEFAULT 0`。

### 3.3 同事务写入（保证"要么都有、要么都没有"）

```python
def record_decision_with_evidence(d: AIDecision, ev: TransportEvidence) -> None:
    with self._lock:
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            cur.execute("INSERT INTO ai_transport_evidence(...)", ev.as_row())
            cur.execute("INSERT INTO ai_decisions(... evidence_id, transport_verified)", ...)
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise EvidencePersistError(...)
```

- 入参 `d.provenance` **不被信任**：函数内按 `ev` 重新推导（见 §3.4），入参只作参考。
- 任一步失败 → 回滚 → 抛 `EvidencePersistError` → 上层该标的记为 `blocked`，**不会留下 real_llm 行**（满足要求 4）。
- 崩溃恢复：SQLite WAL + 单事务原子性；启动时 `verify_evidence_chain()` 检查
  （a）`provenance='real_llm'` 但无 `evidence_id`；（b）evidence 链哈希断点；（c）`created_at < received_at`。
  任一命中 → 该行在读取侧被降级为 `integrity_violation`，readiness 直接 false，并写 `faults`（**只读校验，不改历史行**）。

### 3.4 失败语义（要求 4）

| 场景 | evidence | decision.provenance | 是否可 `real_llm` |
|---|---|---|---|
| 连接/超时/DNS 失败 | 建议仍写一条 `status=0` 的失败证据 | `rejected` | 否 |
| HTTP ≥ 400 | 写入（含 status、信封摘要） | `rejected` | 否 |
| 3xx 重定向 | 写入（`redirect_count=1`） | `rejected` | 否 |
| 响应非 JSON | 写入（信封摘要照存） | `rejected` | 否 |
| JSON 但缺 `choices` | 写入 | `rejected` | 否 |
| 解析成功但 `upstream_model` 为空 | 写入 | `rejected`（无法证明上游来源） | 否 |
| 证据写库失败 | 无 | 角色上不存在（事务回滚） | 否 |
| 全链路成功 + 对端 global + 无代理/重定向 | 写入 | `real_llm` | 是 |

### 3.5 防"事后补写"（要求 5）

1. 决策与证据**同事务**，物理上不可分离；
2. `received_at` 在解析前取，`created_at` 在提交时取，强制 `0 <= created_at - received_at <= 5000ms`，否则标 `late_write`（readiness 不认）；
3. 链式 `integrity_hash` 防止插入/删改中间行；
4. 证据表只 `INSERT`，代码里不提供 `UPDATE/DELETE` 接口（迁移脚本也不提供）。

### 3.6 不把自写字段当第三方证明（要求 6）

证据表在文档与 UI 上一律标注 **"应用自记录（B 级）"**；`readiness` 的措辞改为
"本机可复核的真实调用"，**不**使用"独立第三方证明"。A 级需要上游对账导出或签名，见 §11。

---

## 4. P0-B：端点来源防伪（fail-closed）

### 4.1 判定算法（替换 `decisions.py:56-68`）

```python
def classify_endpoint(base_url, *, resolver=getaddrinfo, timeout=5.0) -> EndpointVerdict:
    u = urlparse(base_url)
    if u.scheme not in ("https", "http"): return unknown("scheme")
    host = (u.hostname or "").lower().rstrip(".")
    if not host: return unknown("empty_host")
    # 1) 字面量 IP（含特殊写法）
    ip = parse_ip_literal(host)      # inet_aton 兼容 127.1 / 2130706433 / 0x7f000001 / 0177.0.0.1
    if ip is not None:
        return verdict_for([ip], scheme=u.scheme, via="literal")
    # 2) 域名 → 全部地址
    infos = resolver(host, u.port or (443 if u.scheme=="https" else 80),
                     type=SOCK_STREAM)
    ips = dedup([i[4][0] for i in infos])
    if not ips: return unknown("dns_empty")
    return verdict_for(ips, scheme=u.scheme, via="dns")
```

`verdict_for` 规则（**任一不满足即 fail-closed**）：

| 条件 | 结果 |
|---|---|
| scheme == "http" 且任一 IP 非 loopback | `unknown`（明文出网一律拒） |
| 任一 IP `not ipaddress.ip_address(ip).is_global` | `unknown`（含 loopback / private / link-local / unspecified / multicast / reserved / CGNAT 100.64/10） |
| 全部 IP `is_global` | `remote`，并记录 `resolved_ips` 与 `dns_digest` |
| 解析异常 / 超时 | `unknown` |

`is_global` 已覆盖任务书列的 7 类，无需逐条手写；`127.1`/`2130706433` 通过 `inet_aton` 归一化后再判。

### 4.2 连接期校验（防 rebinding / 重定向 / 代理）

| 风险 | 对策 |
|---|---|
| DNS rebinding（解析后换 IP） | 解析一次 → **钉死该 IP 连接**；连接后再取 `getpeername()` 与解析集合比对，不一致 → `unknown` |
| 多 A/AAAA | 全部必须 global；连接用的那个 IP 必须 ∈ 解析集合 |
| 重定向 | 不跟随；3xx → `rejected`，并记录 `Location` 主机类别 |
| 代理 | 不读环境变量；`proxy_used` 记录是否存在 `*_PROXY`；若必须用代理，需显式开关且代理地址本身通过同一套判定（回环/私网 → `unknown`） |
| TLS 中间人 | 默认 `ssl` 校验 + 可选 SPKI pin（`tls_spki_sha256`），pin 不匹配 → `unknown` |

**对无法确定的端点：`endpoint_class=unknown`，`provenance` 最高 `real_llm_unverified`，不满足 readiness。**

### 4.3 与 provenance 的关系

```
endpoint=remote + 证据完整 + 传输校验通过 → real_llm
endpoint=remote + 任一校验缺失        → real_llm_unverified（不满足 readiness）
endpoint=loopback                      → stub_transport
endpoint=private/unknown               → unknown
```

`classify_provenance` 的签名保留，但**只作为"候选值"**；最终值由 §3.3 按证据重算。

---

## 5. P0-C：readiness 修复

```python
def _ai_model_called(self) -> dict:
    rows = self.ledger.list_decisions_with_evidence(limit=200)
    good = [r for r in rows if
            r["provenance"] == "real_llm"
        and r["endpoint_class"] == "remote"
        and r["http_method"] == "POST"
        and r["request_path"].endswith("/chat/completions")
        and r["http_status"] == 200
        and bool(r["upstream_model"])                 # 必须有上游来源
        and bool(r["envelope_sha256"]) and r["envelope_bytes"] > 0
        and r["peer_ip_global"] is True
        and r["proxy_used"] == "none"
        and (r["redirect_count"] or 0) == 0
        and r["evidence_id"] and r["evidence_id"] == r["decision_evidence_id"]
        and r["chain_ok"] and not r["late_write"]]
    return {"met": bool(good), "detail": {...逐项计数...}}
```

反例测试（全部必须 `met=False`）：
1. 手工 INSERT 一条 `provenance='real_llm'`、无 evidence；
2. 有 evidence 但 `endpoint_class='loopback'`；
3. `http_status=500`；
4. `upstream_model=''`；
5. `peer_ip_global=0/None`；
6. `proxy_used='env:127.0.0.1:54481'`；
7. `redirect_count=1`；
8. `evidence_id` 与 decision 不一致；
9. 链哈希断点；
10. `late_write=True`。

---

## 6. P1-D：沙盒完整隔离

### 6.1 统一 sandbox root

```python
@dataclass
class LoopSettings:
    sandbox_root: Optional[Path] = None
    # 派生：data_dir = root/"data"; log_dir = root/"logs"; loop_db = root/"data/us_loop.db"
    #       paper_state = root/"data/paper_state.json"; bars_db = root/"data/market_us.db"(副本)
    #       stops = root/"data/stops.json"; audit = root/"data/audit.db"
```

启动时断言：**每个派生路径 `.resolve()` 必须位于 `root` 之下**，否则 `SandboxEscape` 直接退出（不"尽力而为"）。

`_relocate` 同时写 `system.data_dir` 与 `system.log_dir`，并重建 `gate`/`halt_store`。

### 6.2 写守卫（WriteGuard）

```python
class WriteGuard:
    def __init__(self, roots): self._snap = snapshot(roots)   # path -> (size, mtime_ns)
    def __enter__(...): ...
    def __exit__(...):
        drift = diff(self._snap, snapshot(self._roots))
        if drift: raise SandboxEscape(f"沙盒外写入: {drift}")
```

包住 `PaperLoop.run()` / `evidence()` / `restart_proof()`；
快照根 = `PROJECT_ROOT/data` + `PROJECT_ROOT/logs`（只比对，不写）。
命中差异 → 该次 run 标记 `failed`，并把差异写进证据 JSON 的 `sandbox_violations`。

### 6.3 bars 库策略

默认**复制**到沙盒（`shutil.copy2` + sha256 记录），不直接打开项目 `market_us.db`（避免 rw 打开产生 `-wal/-shm`）。
`--bars-db` 显式指向项目文件时，改为只读 URI 打开（`file:...?mode=ro`）并告警。

---

## 7. P1-E：报告勘误（不静默改原文）

新增 `US_REAL_LLM_PAPER_ACCEPTANCE_REPORT_ERRATA.md`，逐条列出：

| 原文位置 | 原文 | 更正 | 依据 |
|---|---|---|---|
| L62 | "395（含 27 个 reasoning tokens）" | `completion_thinking_tokens=313` | `ai_decisions.usage` |
| L153 | "两笔 reason 均带 `[EXEC-LAYER-TEST]` 前缀" | `orders.note=NULL`；标签仅存在于未持久化的 `intent.reason` | `audit.db.orders` |
| §7.2 | 执行层 BUY/SELL 与真实模型混述 | 明确：真实模型轮 = HOLD / 0 单；执行层 = 独立沙盒的 2 笔测试 | 两套 `audit.db` |
| §5 | "model_id 回显" | 账本 `model_id` 是请求侧名称；上游回显未落盘 | `decisions.py:389` |

原则：**原报告与原始证据文件保持字节不变**，勘误单独成文并互相引用。

---

## 8. 数据库迁移与备份

1. 迁移前：`copy2 us_loop.db -> us_loop.db.bak-<YYYYmmdd-HHMMSS>`，记录两侧 sha256 到 `migration_log.json`。
2. 迁移用 `PRAGMA user_version` 版本号（0 → 1）：
   - `CREATE TABLE ai_transport_evidence`
   - `ALTER TABLE ai_decisions ADD COLUMN evidence_id TEXT`
   - `ALTER TABLE ai_decisions ADD COLUMN transport_verified INTEGER DEFAULT 0`
   - 每步 `PRAGMA table_info` 守卫（沿用 `decisions.py:230-236` 的写法），可重复执行。
3. 历史行处理：`transport_verified` 保持 `0`；readiness **不认**历史行（不允许追溯认证）。
4. 迁移失败：捕获异常 → 回滚 DDL（SQLite DDL 可事务化）→ 保留 bak 文件 → 报错退出，不进入运行。

---

## 9. 失败回滚

| 层 | 回滚手段 |
|---|---|
| 代码 | 改动集中在 3 个文件 + 2 个新文件；`cp` 备份即可整体回退（备份含 sha256） |
| 数据库 | `DROP TABLE ai_transport_evidence`；`ALTER TABLE ... DROP COLUMN`（SQLite ≥3.35 支持）；或直接还原 `us_loop.db.bak-*` |
| 运行时 | 单事务保证"决策+证据"原子；任何异常 → 该标的 `blocked`，不下单 |
| 配置开关 | `USL_EVIDENCE=off` 时**不是**回到旧行为，而是直接拒跑（避免"关掉证据继续跑"的诱惑） |

---

## 10. 测试方案（20 例）

新增 `tests/test_usl01_evidence_chain.py`（**不改 `tests/helpers.py`、不删旧用例、不放宽断言**）。

| # | 场景 | 构造方式 | 断言 |
|---|---|---|---|
| 1 | 公网真实端点 | 门控：`USL_REAL_ENDPOINT=1` + 环境凭据，默认 `pytest.skip` | `status=200`、`upstream_model` 非空、`peer_ip_global=1`、`proxy_used=none` |
| 2 | 本地 HTTP stub | 127.0.0.1 stub | `endpoint=loopback`、`provenance=stub_transport`、readiness=False |
| 3 | 私网 IP | stub 绑 `0.0.0.0`，URL 用 `http://10.0.0.7:port` | `endpoint=unknown/private`、无 real_llm |
| 4 | IPv4 特殊写法 | `127.1`、`2130706433`、`0x7f000001`、`0177.0.0.1` | 全部 `loopback/unknown`，**不得 remote** |
| 5 | IPv6 loopback | `[::1]`、`[::ffff:127.0.0.1]` | `loopback` |
| 6 | DNS 解析到私网 | 注入 resolver → `192.168.1.5` | `unknown` |
| 7 | 多地址 DNS | 注入 resolver → `[8.8.8.8, 10.0.0.1]` | `unknown`（任一非 global 即拒） |
| 8 | redirect 到私网 | stub 返回 302 → `http://10.0.0.7/` | `rejected`、`redirect_count=1`、无 real_llm |
| 9 | proxy 改变目标 | 设 `HTTPS_PROXY=http://127.0.0.1:9` | `proxy_used=env:...`、无 real_llm |
| 10 | 上游 500 | stub 返 500 | `rejected`、evidence 存在、readiness=False |
| 11 | 无效 JSON | stub 返 `not-json` | `rejected`、信封摘要仍记录 |
| 12 | 缺 response_id | 200 但无 `id` 无 `x-request-id` | `upstream_id=""`，记录 `response_id_missing`，按策略降级 |
| 13 | 缺 usage | 200 但无 `usage` | `usage_json=NULL`，readiness 要求 token>0 → False |
| 14 | 数据库写入失败 | 只读打开 loop_db / 或注入 `sqlite3.OperationalError` | 抛 `EvidencePersistError`、`ai_decisions` 无新增行 |
| 15 | 进程中途崩溃 | 事务内 `os._exit(1)` 子进程 | 重启后无"孤证"、无"孤决策" |
| 16 | 伪造 provenance | 直接传 `provenance='real_llm'` 给 gateway（loopback 端点） | 被降级、readiness=False |
| 17 | 伪造 endpoint_class | 手工 INSERT decision+evidence，endpoint 写 remote 但 `peer_ip_global=0` | readiness=False |
| 18 | readiness 反例 | §5 的 10 种构造 | 全部 `met=False` |
| 19 | sandbox 日志隔离 | 沙盒内触发一次故障（写 faults） | 项目 `logs/` 无新文件；沙盒 `logs/` 有 |
| 20 | 重启恢复与重复执行 | 跑两次 + `restart_proof` | 状态一致、无重复订单、evidence 链连续 |

**验收标准**：20 例全绿；既有 611 项不回退；新增断言不得放宽既有断言。

---

## 11. 证据可信度分级

| 级别 | 定义 | 当前 | 改后 |
|---|---|---|---|
| **A** | 第三方可独立验证（上游对账导出 / 可验证签名 / 公开可查的计费凭证） | 无 | 仍无（需上游配合） |
| **B** | 上游响应中的不可自造标识（响应 `id`、`model`）+ 原始信封摘要 + 传输对端验证 | 无 | **可达**（P0-A+P0-B 完成） |
| **C** | 本应用自记录 + 完整性链 + 同事务 + 写时戳 | 部分（有字段无链） | 有 |
| **D** | 仅应用自写字段，无任何交叉约束（现状 `provenance`） | **当前即 D** | 不再单独使用 |

结论写死：**B 级是"本机可复核"，不是"第三方证明"**；报告措辞必须区分。

---

## 12. 风险与未解决边界

| 风险 | 说明 | 处置 |
|---|---|---|
| 强制无代理 | 若用户所处网络必须走代理，真实调用会 fail-closed | 提供 `--allow-proxy` 显式开关，但结果只能是 `real_llm_unverified` |
| 对端 IP 可被合法 CDN 共享 | `is_global` 通过不代表"就是该 Provider" | 可选 SPKI pin；文档标注剩余风险 |
| 上游模型名与请求名不一致（别名） | `cn:deepseek-v4.1-flash` → `deepseek-v4.1-flash` | 记录两者 + `model_alias_matched` 标志，不强制相等 |
| 历史行不可追溯 | 迁移前的 `real_llm` 行永远 `transport_verified=0` | readiness 不认；如需重证只能重跑 |
| TLS pin 轮换 | pin 更新会中断调用 | pin 存配置（非秘密），变更需人工 |
| 性能 | 见 §13 | 可接受 |
| 未覆盖 | F-USL-02 / F-USL-03；上游签名；跨机器时钟一致性 | 明示不在本轮 |

---

## 13. 性能影响评估

| 项 | 增量 | 依据 |
|---|---|---|
| 多一次 DNS 解析 | 0~30 ms（冷） / ≈0（OS 缓存） | 与 8.5 s 推理相比可忽略 |
| IP 钉死 | 0（复用已有解析结果） | — |
| 信封 sha256 | 4 KB ≈ 0.03 ms | 单次 hash |
| 事务多一次 INSERT | 0.2~0.5 ms（WAL） | 同事务 |
| 链哈希 | <0.05 ms | 拼接 + sha256 |
| **合计** | **p50 +1~3 ms，占单次调用 <0.05%** | — |

不影响指标计算/撮合路径（本改动只在 AI 调用的边界）。

---

## 14. 实施顺序与停止条件

| 阶段 | 内容 | 停止条件（任一命中即停，不进下一阶段） |
|---|---|---|
| **S0** | 备份 `us_loop.db` + 记录 sha256；建迁移脚本（dry-run 只打印 SQL） | 备份失败 / dry-run 与预期表结构不符 |
| **S1** | P0-B `transport.py` 的端点判定 + 单测（#3–#7） | 任一"私网/别名/多地址"用例被判 remote |
| **S2** | P0-A `TransportClient` + 证据表 + 同事务（#10–#15） | 出现"有决策无证据"或"evidence 写失败仍留 real_llm 行" |
| **S3** | 接线 `providers._post` → 返回 `(payload, TransportResult)`；`AIGateway` 落证据 | 既有 611 项任一失败 |
| **S4** | P0-C readiness 重写 + 反例（#16–#18） | 任一反例 `met=True` |
| **S5** | P1-D 沙盒 root + WriteGuard（#19） | 快照比对发现项目 `data/`/`logs/` 被写 |
| **S6** | P1-E 勘误文档 + 重跑一次门控真实调用（#1，需人工授权与额度） | 真实端点未通过 `peer_ip_global`/代理校验 |
| **S7** | 全量回归 + 出具新验收报告 | 611 项回退 / 证据链断点 |

每阶段产出：改动文件 + 新增测试 + 该阶段停止条件的执行结果。

---

## 15. 只读证明

本轮仅读取源码/账本/环境变量，未写任何项目文件。基线快照（86 个文件，含 `src/` `tests/` `config/` `.env` `data/*.db` 与既有报告/证据）：
`%TEMP%\usl01_baseline.sha256`；结束时逐项复核（见交付说明）。

---

## 16. 待人工决策的 4 个点

1. **代理策略**：强制无代理（fail-closed，最安全）还是允许显式代理（打 `unverified` 标）？
2. **`response_id` 缺失**是否一票否决 readiness？（建议：否决，因为它是上游不可自造标识里最便宜的一个）
3. **IP 钉死**是否默认开启？（建议开启；对 CDN 多 IP 需允许"钉死集合中的任一个"）
4. **历史 `real_llm` 行**是否需要在报告里逐条标注"未验证传输"？（建议标注）

---

> 本文件 SHA256（本行之前全部字节，可用 `head -n -1 <本文件> | sha256sum` 复算）：`493f361a50a9a3d2a10fd56d5af08c94eb976c8255cb99931a9f06bc9277a074`
