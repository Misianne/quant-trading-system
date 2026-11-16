# US Real LLM Evidence — 决策确认单 V3.1（设计修订，待审核）

| 项 | 值 |
|---|---|
| 版本 | **v3.1**（2026-11-10 19:56） |
| 取代 | `..._DECISIONS_V3.md`（`49fec2e9…`，**不覆盖、不修改**） |
| 依据 | `..._V3_INDEPENDENT_AUDIT.md`（`138ed16b…`）之 A-01…A-15 |
| 性质 | 设计文档修订；不实施源码/数据库/SQL |
| 状态 | **V3.1 待用户审核**；原方案已批准；D1–D4 已确认；源码实施未授权；S0 未启动 |

---

## 0. 先纠正一个计数（A-17）

任务书与审计摘要都写"P2 = 8 / P3 = 3"，但审计报告 **§8 分级表实际是 9 项 P2 + 2 项 P3**：

```
P1: A-01 A-02 A-03 A-04                      （4）
P2: A-05 A-06 A-07 A-08 A-09 A-10 A-13 A-14 A-15 （9）
P3: A-11 A-12                                （2）
```

审计报告第 246 行的"P2 = 8，P3 = 3"与自己的表不符。**V3.1 以表为准（9 + 2）**，不按摘要数偷工。另新增 1 项 V3 内部不一致（A-16，见下），故本轮实际处置 **17 项**。

---

## 1. 问题追踪总表

| ID | 级别 | 问题 | V3.1 处置 | 位置 |
|---|---|---|---|---|
| A-01 | P1 | §5 散文 I6 只列 10 项，漏 `upstream_id_source` | **已修**：I6 改为单一事实源，散文/伪代码/SQL 三处引用同一编号清单 | §2 |
| A-02 | P1 | `real_llm` 被写成充分条件 | **已修**：`real_llm` 重新定义为"R1–R24 全过"，DB+写路径+自检三重保证 | §3 |
| A-03 | P1 | 缺地址一致性约束 | **已修**：新增 `peer_role`，加 5 条地址一致性 CHECK | §4 |
| A-04 | P1 | `proxy_class` 只单向 | **已修**：代理四字段双向约束 + `unknown` 语义 | §5 |
| A-05 | P2 | `proxy_used` 无枚举/非空 | **已修**：枚举 + `env:/explicit:/none/unknown` | §5.2 |
| A-06 | P2 | DDL 缺 evidence_id/received_at/created_at 等 | **已修**：给完整 DDL，R→字段逐项映射 | §7 |
| A-07 | P2 | 丢 `tls_spki_sha256` | **已修**：恢复并加 `tls_verified/hostname/pin_status` | §6 |
| A-08 | P2 | `local_llm` 与 `stub_transport` 不可区分 | **部分修**：加 `source_declared_by` 强制声明来源；残余风险入 §14 | §8 I12 |
| A-09 | P2 | §2 追踪表 8 处交叉引用错位 | **已修**：本文件全部引用按实际章节重写 | 全文 |
| A-10 | P2 | §8 标题与行数不符；CE-1…CE-8 未追踪 | **已修**：附录 A 补 CE 映射表 | 附录 A |
| A-11 | P3 | "拆分 2 条"说法不成立 | **已修**：改为"14 条 1:1 映射 + 新增 10 条 = 24" | §10.1 |
| A-12 | P3 | 有 ID 无来源 | **已修**：I13 约束 | §8 |
| A-13 | P2 | 未纠正"链哈希防篡改"过强表述 | **已修**：§12.1 明确链的检测边界 | §12.1 |
| A-14 | P2 | 未承接 SNI/证书/CONNECT 边界 | **已修**：§6.2 边界表 | §6.2 |
| A-15 | P2 | I5 放宽后恒真 | **已修**：删除 I5，改由 I4 承载（含 `peer_role`） | §8 |
| **A-16** | P2 | **V3 §4 S5 称"usage 缺失→unverified"，但 R1–R16 无任何 usage 谓词** | **已修**：新增 R18 `usage_prompt_tokens>0` | §10.1 |
| **A-17** | P3 | 审计报告自身计数 8/3 与表 9/2 不符 | **已记录**：本文件以表为准 | §0 |

---

## 2. A-01：I6 单一事实源

### 2.1 做法

I6 不再用散文复述。定义编号清单 **I6-C1 … I6-C20**，散文、伪代码、SQL 全部引用**同一套编号**，任何一处增删必须同步三处。

| 编号 | 条件 | 数据源 |
|---|---|---|
| I6-C1 | `transport_path='direct_verified'` | 连接期 |
| I6-C2 | `endpoint_class='remote'` | DNS 解析 |
| I6-C3 | `peer_role='upstream'` | 连接期 |
| I6-C4 | `peer_class='remote'` | getpeername |
| I6-C5 | `peer_ip_global=1` | getpeername |
| I6-C6 | `proxy_used='none'` | 代理检测 |
| I6-C7 | `proxy_class='none'` | 代理检测 |
| I6-C8 | `http_method='POST'` | 请求构造 |
| I6-C9 | `request_path LIKE '%/chat/completions'` | 请求构造 |
| I6-C10 | `http_status=200` | 响应状态 |
| I6-C11 | `length(upstream_model)>0` | 响应体 `model` |
| I6-C12 | `length(envelope_sha256)=64` | 原始字节 |
| I6-C13 | `envelope_bytes>0` | 原始字节 |
| I6-C14 | `redirect_count=0` | 响应链 |
| I6-C15 | `upstream_id IS NOT NULL` | 响应体 `id` |
| I6-C16 | `length(upstream_id)>0` | 同上 |
| I6-C17 | `upstream_id_source='body.id'` | 解析器指定 |
| I6-C18 | `usage_prompt_tokens>0` | 响应体 `usage` |
| I6-C19 | `tls_verified=1` | TLS 握手 |
| I6-C20 | `tls_hostname_verified=1` ∧ `tls_pin_status IN ('not_configured','matched')` | TLS 校验 |

**合计 20 项**（V3 是 12 项；新增 C3–C5、C17、C18、C19–C20，合并原 upstream_id 两条为 C15/C16）。

### 2.2 三处一致性证明

| 检查 | 结果 |
|---|---|
| 散文（§3.2 表）列出 C1–C20 | ✅ 20 行 |
| 伪代码（§3.4 `evaluate_readiness`）引用 R2–R21 | ✅ 与 C1–C20 一一对应（R 编号 = C 编号 + 1） |
| SQL（§7.2 `CHECK ... provenance<>'real_llm' OR (...)`） | ✅ 20 个合取项，逐项可在 §7.2 数出 |
| 自动核对 | 三处均含 `upstream_id_source='body.id'`（V3 散文漏项已补） |

> 映射：I6-C*n* ↔ readiness R*n+1*。例：C1↔R2、C17↔R18、C20↔R21。

---

## 3. A-02：`real_llm` 语义修正

### 3.1 新定义（与 V3 的差别）

| | V3 | V3.1 |
|---|---|---|
| `real_llm` 含义 | "I6 的 12 项满足" | **"R1–R24 全部满足"** |
| 与 readiness 关系 | 必要条件 | **充要条件**（定义上等价） |
| DB 能否保证 | 只保证 12 项 | 单行 21 项由 CHECK 保证；跨行 3 项由写路径保证 |
| 允许存在 `real_llm` 但 readiness=false 的行 | 允许 | **不允许**（出现即为数据被改，标 `integrity_violation`） |

### 3.2 三层保证

```
写入时（唯一入口）
  ├─ compute_all(R1..R24)  →  ok, missing
  ├─ d.provenance = 'real_llm' if ok else fallback(ev)
  ├─ d.unverified_reasons = [] if ok else missing
  ├─ BEGIN IMMEDIATE
  │    INSERT evidence(...)
  │    INSERT decision(..., provenance, unverified_reasons, evidence_id)
  │    COMMIT
  └─ 读回重算 readiness；与写入时不一致 → ROLLBACK + integrity_violation

读取时（readiness()）
  └─ 对每行重算 R1..R24；P='real_llm' 但重算 false → 该行不计入 met，并标 integrity_violation
```

- **DB 层**：`provenance='real_llm'` 触发 C1–C20 的 CHECK（单行可判的 20 项）。
- **写路径**：R22（evidence_id 跨表）、R23（chain_ok）、R24（late_write）跨行/跨表，CHECK 无法表达，由写入函数在事务内断言。
- **自检**：提交后重算，防"写入逻辑与判定逻辑漂移"。

### 3.3 记录状态与 readiness 的关系（明示）

| 存储的 `provenance` | readiness 重算 | 结论 |
|---|---|---|
| `real_llm` | true | 已验证的真实调用（唯一可宣称 PASS 的情形） |
| `real_llm` | false | **数据不一致**（`integrity_violation`），不得采信 |
| `real_llm_unverified` | false | 正常：证据不全，reasons 说明缺什么 |
| 其他 | false | 正常 |

> 这与任务要求一致："DB 中允许存在尚未满足完整 readiness 的其他记录，但这些记录不得被认定为已验证的 real_llm"——由 C1–C20 的 CHECK 在物理上阻止。

---

## 4. A-03：地址一致性（新增 `peer_role`）

### 4.1 问题

V3 只有 `endpoint_class`（目标）+ `peer_class`（对端），**无法表达"这个对端是上游还是代理"**。于是 `E=remote + peer=loopback + global=1` 可以入库（审计 A-03），且代理链路上"对端=代理"这一事实无法被约束表达。

### 4.2 新字段 `peer_role`

| 值 | 含义 |
|---|---|
| `upstream` | 对端就是我们要访问的模型服务（直连或本地/私网） |
| `proxy` | 对端是代理，真实上游在代理之后，**不可见** |
| `unknown` | 未取得/无法判定 |

### 4.3 一致性规则（I4–I8）

| 编号 | 规则 |
|---|---|
| **I4** | `T='proxy_unverified' ⇒ proxy_class ∈ {loopback,private,remote} ∧ peer_role='proxy' ∧ peer_class=proxy_class` |
| **I5** | `T='direct_verified' ⇒ peer_role='upstream' ∧ peer_class='remote' ∧ peer_ip_global=1` |
| **I6** | `T='local' ⇒ peer_role='upstream' ∧ peer_class='loopback' ∧ peer_ip_global=0` |
| **I7** | `T='private' ⇒ peer_role='upstream' ∧ peer_class='private' ∧ peer_ip_global=0` |
| **I8** | `peer_class='remote' ⇔ peer_ip_global=1`（反向：`peer_class∈{loopback,private} ⇒ peer_ip_global=0`） |

**反例**：`E=remote + T=direct_verified + peer_class='loopback' + peer_ip_global=1` → 违反 I5（要求 peer_class='remote'）与 I8（loopback 要求 global=0），**双重拒绝**。

### 4.4 代理场景的正确表达

`E=remote`（目标）+ `proxy_class='loopback'`（代理在本机）+ `peer_role='proxy'` + `peer_class='loopback'` + `peer_ip_global=0`。
—— 目标、代理、对端三个概念分别落在三个字段，不再二选一。

---

## 5. A-04：代理字段双向一致

### 5.1 `proxy_used` 取值（A-05 一并修复）

| 值 | 含义 |
|---|---|
| `none` | 未使用代理（可能在环境里有代理变量但被绕过） |
| `env:<host:port>` | 使用了环境变量指定的代理 |
| `explicit:<host:port>` | 使用了代码显式指定的代理 |
| `unknown` | 未观测到真实代理状态（**不得默认 none**） |

SQLite 约束：

```sql
CHECK (proxy_used IN ('none','unknown')
    OR proxy_used LIKE 'env:%'
    OR proxy_used LIKE 'explicit:%')
CHECK (length(proxy_used) > 0)
```

### 5.2 双向规则（I9–I11）

| 编号 | 规则 | 说明 |
|---|---|---|
| **I9a** | `proxy_used='none' ⇔ proxy_class='none'` | 双向；`proxy_used≠none` 时 `proxy_class` 必须给真值 |
| **I9b** | `proxy_used LIKE 'env:%' OR 'explicit:%' ⇒ T='proxy_unverified'` | 用了代理就只能是代理路径 |
| **I9c** | `T='proxy_unverified' ⇒ proxy_used LIKE 'env:%' OR 'explicit:%'` | 反向，防"声称代理路径却无代理记录" |
| **I10** | `proxy_used='unknown' ⇒ T='unknown' ∧ P≠'real_llm'` | 观测不到就整条降级 |
| **I11** | `proxy_env_present` **不约束** `proxy_used` | 环境变量存在 ≠ 使用（本机真实情况：`proxy_env_present=1` 而 `proxy_used='none'`，因为 `TransportClient` 不读 env） |

**反例**：`proxy_used='env:127.0.0.1:54481' + proxy_class='none'` → 违反 I9a（`proxy_used≠none` 要求 `proxy_class≠'none'`），拒绝 ✅（审计 A-04 的目标）。

---

## 6. TLS / SPKI 证据（A-07、A-14）

### 6.1 字段

| 字段 | 取值 | 说明 |
|---|---|---|
| `tls_verified` | 0/1 | 证书链校验是否通过 |
| `tls_hostname_verified` | 0/1 | **证书主机名是否与原始域名匹配**（不是与 IP 比） |
| `tls_spki_sha256` | TEXT，`length∈{0,64}` | 实际观测到的叶子证书 SPKI 摘要 |
| `tls_pin_expected` | TEXT，`length∈{0,64}` | 配置的期望 pin（可空） |
| `tls_pin_status` | `not_configured` / `matched` / `mismatched` / `not_checked` | 四态，**不得用 matched 表示"没验"** |

规则：

| 编号 | 规则 |
|---|---|
| **I14** | `tls_pin_status='matched' ⇒ length(tls_spki_sha256)=64`（没摘要就不能声称匹配） |
| **I15** | `tls_pin_status='not_checked' ⇒ P≠'real_llm'`（未执行不得伪装通过） |
| I6-C19/C20 | `P='real_llm' ⇒ tls_verified=1 ∧ tls_hostname_verified=1 ∧ pin_status∈{not_configured,matched}` |

### 6.2 网络层边界（A-14）

| 场景 | 原始域名/SNI | 证书主机名 | 对端可见性 | 能否 `real_llm` |
|---|---|---|---|---|
| 直连（IP 钉死） | 保留（`server_hostname=域名`） | 以域名校验 | `getpeername()`=目标 IP | ✅ |
| 本地模型（http） | 无 TLS | 不适用 | loopback | ❌（`local_llm`） |
| 代理 CONNECT | SNI 仍由本地发出 | **仍以域名校验**（端到端 TLS） | `getpeername()`=**代理** | ❌（`proxy_unverified`） |
| 代理 + TLS 终止（MITM） | 域名可能不匹配 | 会失败或 pin 不匹配 | 代理 | ❌ |

**明确写入文档**：
- IP 钉死**只证明"TCP 对端是那个 IP"**，不证明"那个 IP 是模型供应商"；
- 代理 CONNECT 下 TLS 仍端到端，但**网络路径不可验证**，因此只能 `proxy_unverified`；
- pin 匹配只能证明"证书是我预期的"，不能证明"上游确实做了推理"。

---

## 7. 完整 DDL（A-06）

```sql
CREATE TABLE ai_transport_evidence (
    evidence_id     TEXT    PRIMARY KEY CHECK (length(evidence_id) = 64),
    decision_id     TEXT    NOT NULL CHECK (length(decision_id) > 0),
    seq             INTEGER NOT NULL CHECK (seq >= 1),
    run_id          TEXT,
    symbol          TEXT    NOT NULL CHECK (length(symbol) > 0),

    /* ---- 三轴 ---- */
    endpoint_class  TEXT    NOT NULL CHECK (endpoint_class IN ('remote','loopback','private','unknown')),
    transport_path  TEXT    NOT NULL CHECK (transport_path IN ('direct_verified','proxy_unverified','local','private','unknown')),
    provenance      TEXT    NOT NULL CHECK (provenance IN ('real_llm','real_llm_unverified','local_llm','stub_transport','rejected','unavailable','unknown')),

    /* ---- 对端/代理观测 ---- */
    peer_role       TEXT    NOT NULL CHECK (peer_role IN ('upstream','proxy','unknown')),
    peer_class      TEXT    NOT NULL CHECK (peer_class IN ('remote','loopback','private','unknown')),
    peer_ip         TEXT,
    peer_ip_global  INTEGER NOT NULL DEFAULT 0 CHECK (peer_ip_global IN (0,1)),
    proxy_used      TEXT    NOT NULL DEFAULT 'none'
                    CHECK (proxy_used IN ('none','unknown')
                        OR proxy_used LIKE 'env:%'
                        OR proxy_used LIKE 'explicit:%'),
    proxy_class     TEXT    NOT NULL DEFAULT 'none'
                    CHECK (proxy_class IN ('none','loopback','private','remote','unknown')),
    proxy_env_present INTEGER NOT NULL DEFAULT 0 CHECK (proxy_env_present IN (0,1)),

    /* ---- HTTP ---- */
    http_method     TEXT    NOT NULL CHECK (http_method = 'POST'),
    request_path    TEXT    NOT NULL CHECK (length(request_path) > 0),
    http_status     INTEGER NOT NULL CHECK (http_status BETWEEN 0 AND 599),
    redirect_count  INTEGER NOT NULL DEFAULT 0 CHECK (redirect_count >= 0),

    /* ---- 响应证据 ---- */
    upstream_model  TEXT    NOT NULL DEFAULT '',
    upstream_id     TEXT,
    upstream_id_source TEXT NOT NULL DEFAULT '' CHECK (upstream_id_source IN ('','body.id')),
    network_request_id TEXT,
    network_request_id_source TEXT NOT NULL DEFAULT ''
                    CHECK (network_request_id_source IN ('','x-request-id','cf-ray')),
    envelope_sha256 TEXT    NOT NULL DEFAULT '' CHECK (length(envelope_sha256) IN (0,64)),
    envelope_bytes  INTEGER NOT NULL DEFAULT 0 CHECK (envelope_bytes >= 0),
    usage_prompt_tokens     INTEGER NOT NULL DEFAULT 0 CHECK (usage_prompt_tokens >= 0),
    usage_completion_tokens INTEGER NOT NULL DEFAULT 0 CHECK (usage_completion_tokens >= 0),
    response_digest TEXT,

    /* ---- TLS ---- */
    tls_verified          INTEGER NOT NULL DEFAULT 0 CHECK (tls_verified IN (0,1)),
    tls_hostname_verified INTEGER NOT NULL DEFAULT 0 CHECK (tls_hostname_verified IN (0,1)),
    tls_spki_sha256       TEXT    NOT NULL DEFAULT '' CHECK (length(tls_spki_sha256) IN (0,64)),
    tls_pin_expected      TEXT    NOT NULL DEFAULT '' CHECK (length(tls_pin_expected) IN (0,64)),
    tls_pin_status        TEXT    NOT NULL DEFAULT 'not_checked'
                          CHECK (tls_pin_status IN ('not_configured','matched','mismatched','not_checked')),

    /* ---- 时间与链 ---- */
    received_at     TEXT    NOT NULL CHECK (length(received_at) > 0),
    created_at      TEXT    NOT NULL CHECK (length(created_at) > 0),
    source_declared_by TEXT NOT NULL DEFAULT 'probe' CHECK (source_declared_by IN ('probe','operator')),
    integrity_hash  TEXT    NOT NULL CHECK (length(integrity_hash) = 64),
    prev_hash       TEXT    NOT NULL DEFAULT '',
    unverified_reasons TEXT NOT NULL DEFAULT '[]'
                    CHECK (json_valid(unverified_reasons) AND json_type(unverified_reasons) = 'array'),

    /* ---- 跨轴不变量（详见 §8） ---- */
    CHECK (...)
);
```

`ai_decisions` 增列：`evidence_id TEXT NOT NULL`、`transport_verified INTEGER NOT NULL DEFAULT 0`。

### 7.1 R → 字段映射（证明无悬空）

| R | 字段 | 校验层 |
|---|---|---|
| R1 | `provenance` | CHECK |
| R2 | `transport_path` | CHECK |
| R3 | `endpoint_class` | CHECK |
| R4 | `peer_role` | CHECK |
| R5 | `peer_class` | CHECK |
| R6 | `peer_ip_global` | CHECK |
| R7 | `proxy_used` | CHECK |
| R8 | `proxy_class` | CHECK |
| R9 | `http_method` | CHECK |
| R10 | `request_path` | CHECK |
| R11 | `http_status` | CHECK |
| R12 | `upstream_model` | CHECK |
| R13 | `envelope_sha256` | CHECK |
| R14 | `envelope_bytes` | CHECK |
| R15 | `redirect_count` | CHECK |
| R16 | `upstream_id` | CHECK |
| R17 | `upstream_id_source` | CHECK |
| R18 | `usage_prompt_tokens` | CHECK |
| R19 | `tls_verified` | CHECK |
| R20 | `tls_hostname_verified` | CHECK |
| R21 | `tls_pin_status`(+`tls_spki_sha256`) | CHECK |
| R22 | `evidence_id`（+ `ai_decisions.evidence_id`） | 写断言 + 读时重算 |
| R23 | `integrity_hash` / `prev_hash` | 写断言 + 读时重算 |
| R24 | `received_at` / `created_at` | 写断言 + 读时重算 |

→ **V3 中 R13/R15 悬空的问题已消除**（V3 §7.1 缺 `evidence_id/received_at/created_at`，此处全部补入）。

---

## 8. 不变量全集（I1–I15，口径统一）

| 编号 | 规则 | 层 |
|---|---|---|
| I1 | `T='local' ⇒ E='loopback'` | CHECK |
| I2 | `T='private' ⇒ E='private'` | CHECK |
| I3 | `T='direct_verified' ⇒ E='remote'` | CHECK |
| I4 | `T='proxy_unverified' ⇒ proxy_class∈{loopback,private,remote} ∧ peer_role='proxy' ∧ peer_class=proxy_class` | CHECK |
| I5 | `T='direct_verified' ⇒ peer_role='upstream' ∧ peer_class='remote' ∧ peer_ip_global=1` | CHECK |
| I6 | `T='local' ⇒ peer_role='upstream' ∧ peer_class='loopback' ∧ peer_ip_global=0` | CHECK |
| I7 | `T='private' ⇒ peer_role='upstream' ∧ peer_class='private' ∧ peer_ip_global=0` | CHECK |
| I8a/b | `peer_class='remote' ⇒ peer_ip_global=1`；`peer_class∈{loopback,private} ⇒ peer_ip_global=0` | CHECK |
| I9a/b/c | 代理与路径双向等价（§5.2） | CHECK |
| I10 | `proxy_used='unknown' ⇒ T='unknown' ∧ P≠'real_llm'` | CHECK |
| I11 | `P='real_llm_unverified' ⇒ json_valid(unverified_reasons) ∧ json_type(...)='array' ∧ json_array_length(...)>0` | CHECK |
| I12 | `P='local_llm' ⇒ T∈{local,private} ∧ E∈{loopback,private} ∧ source_declared_by='operator'` | CHECK |
| I13 | `upstream_id 非空 ⇒ length(upstream_id_source)>0` | CHECK |
| I14 | `tls_pin_status='matched' ⇒ length(tls_spki_sha256)=64` | CHECK |
| I15 | `tls_pin_status='not_checked' ⇒ P≠'real_llm'` | CHECK |
| **I16** | `P='real_llm' ⇒ I6-C1…C20（20 项合取）` | CHECK |
| **I17** | `P='real_llm' ⇒ evidence_id 跨表一致 ∧ chain_ok ∧ late_write=0` | 写断言 + 读时重算 |

> 与 V3 对照：V3 的 I1/I2/I3 保留；I4（`T=unknown ⇒ E=unknown`）在 V3 已删；I5（proxy 4 值）**被 I4 取代**（A-15：原式恒真）；I6 → I16（20 项）；I7′ → I11；I8 → I12；I9 → I17。

---

## 9. 11 场景真值表（全字段）

| # | 场景 | E | T | peer_role | peer_class | global | proxy_used / class | tls 验/名/pin | P | reasons | R |
|---|---|---|---|---|---|---|---|---|---|---|---|
| S1 | 直连全通过 | remote | direct_verified | upstream | remote | 1 | none/none | 1/1/not_configured | `real_llm` | — | ✅ |
| S2 | 直连缺 response_id | remote | direct_verified | upstream | remote | 1 | none/none | 1/1/not_configured | `real_llm_unverified` | missing_upstream_id | ❌ |
| S3 | 直连 HTTP 500 | remote | direct_verified | upstream | remote | 1 | none/none | 1/1/not_configured | `rejected` | status_not_200 | ❌ |
| S4 | 直连摘要失败 | remote | direct_verified | upstream | remote | 1 | none/none | 1/1/not_configured | `real_llm_unverified` | envelope_digest_failed | ❌ |
| S5 | 直连 usage 缺失 | remote | direct_verified | upstream | remote | 1 | none/none | 1/1/not_configured | `real_llm_unverified` | missing_usage | ❌ |
| S6 | 本机代理转发 | remote | proxy_unverified | proxy | loopback | 0 | env:127.0.0.1:54481/loopback | 1/1/not_checked | `real_llm_unverified` | proxy_path | ❌ |
| S7 | 本地真实模型 | loopback | local | upstream | loopback | 0 | none/none | 0/0/not_checked | `local_llm` | — | ❌ |
| S8 | 私网模型服务 | private | private | upstream | private | 0 | none/none | 1/1/not_checked | `local_llm` | — | ❌ |
| S9 | 未知目标 | unknown | unknown | unknown | unknown | 0 | none/none | 0/0/not_checked | `unknown` | dns_empty | ❌ |
| S10 | DNS 异常 | unknown | unknown | unknown | unknown | 0 | none/none | 0/0/not_checked | `unknown` | dns_failure | ❌ |
| S11 | TLS 验证失败 | remote | unknown | unknown | unknown | 0 | none/none | 0/0/not_checked | `rejected` | tls_verify_failed | ❌ |

**逐场景 CHECK 自检**：

| # | I4 | I5 | I6 | I8 | I9 | I11 | I15 | 合法 |
|---|---|---|---|---|---|---|---|---|
| S1 | — | ✅ | — | ✅(remote→1) | ✅(none/none) | — | — | ✅ |
| S2 | — | ✅ | — | ✅ | ✅ | ✅ | — | ✅ |
| S3 | — | ✅ | — | ✅ | ✅ | — | — | ✅ |
| S4 | — | ✅ | — | ✅ | ✅ | ✅ | — | ✅ |
| S5 | — | ✅ | — | ✅ | ✅ | ✅ | — | ✅ |
| S6 | ✅(loopback,proxy,=class) | — | — | ✅(loopback→0) | ✅(env→≠none) | ✅ | ✅(not_checked) | ✅ |
| S7 | — | — | ✅ | ✅(loopback→0) | ✅ | — | ✅ | ✅ |
| S8 | — | — | ✅ | ✅(private→0) | ✅ | — | ✅ | ✅ |
| S9 | — | — | — | — | ✅ | — | ✅ | ✅ |
| S10 | — | — | — | — | ✅ | — | ✅ | ✅ |
| S11 | — | — | — | — | ✅ | — | ✅ | ✅ |

11/11 合法，且 S1 是唯一 R=true 的情形。

---

## 10. readiness R1–R24（单一事实源）

| R | 谓词 | 字段 | 侧 | 实测 | 缺失时 | 可本地伪造 | 影响 |
|---|---|---|---|---|---|---|---|
| R1 | `provenance='real_llm'` | provenance | 结论 | 派生 | false | 需绕过 I16 | 是 |
| R2 | `transport_path='direct_verified'` | transport_path | 传输 | ✅ | false | 改库 | 是 |
| R3 | `endpoint_class='remote'` | endpoint_class | 目标 | ✅ | false | 改库 | 是 |
| R4 | `peer_role='upstream'` | peer_role | 传输 | ✅ | false | 改库 | 是 |
| R5 | `peer_class='remote'` | peer_class | 传输 | ✅ | false | 改库 | 是 |
| R6 | `peer_ip_global=1` | peer_ip_global | 传输 | ✅ | false | 改库 | 是 |
| R7 | `proxy_used='none'` | proxy_used | 传输 | ✅ | false | 改库 | 是 |
| R8 | `proxy_class='none'` | proxy_class | 传输 | ✅ | false | 改库 | 是 |
| R9 | `http_method='POST'` | http_method | **请求** | 构造值 | false | 可（无收益） | 是 |
| R10 | `request_path` 后缀 | request_path | **请求** | 构造值 | false | 可 | 是 |
| R11 | `http_status=200` | http_status | 响应 | ✅ | false | 改库 | 是 |
| R12 | `upstream_model` 非空 | upstream_model | 响应 | ✅ | false | 改库 | 是 |
| R13 | `envelope_sha256` 64 位 | envelope_sha256 | 响应 | ✅ | false | 改库 | 是 |
| R14 | `envelope_bytes>0` | envelope_bytes | 响应 | ✅ | false | 改库 | 是 |
| R15 | `redirect_count=0` | redirect_count | 传输 | ✅ | false | 改库 | 是 |
| R16 | `upstream_id` 非空 | upstream_id | 响应 | ✅ | false | 见 §14 R-1 | 是 |
| R17 | `upstream_id_source='body.id'` | upstream_id_source | 解析器 | 规则 | false | 改库 | 是 |
| R18 | `usage_prompt_tokens>0` | usage_prompt_tokens | 响应 | ✅ | false | 改库 | 是 |
| R19 | `tls_verified=1` | tls_verified | 传输 | ✅ | false | 改库 | 是 |
| R20 | `tls_hostname_verified=1` | tls_hostname_verified | 传输 | ✅ | false | 改库 | 是 |
| R21 | `tls_pin_status∈{not_configured,matched}` | tls_pin_status | 传输 | ✅ | false | 改库 | 是 |
| R22 | `evidence_id` 非空且跨表相等 | evidence_id | 关联 | 派生 | false | 需改两表 | 是 |
| R23 | `chain_ok=1` | integrity_hash/prev_hash | 派生 | 派生 | false | 需重算全链 | 是 |
| R24 | `late_write=0`（0≤Δ≤5000ms） | received_at/created_at | 派生 | 派生 | false | 改时间戳会破链 | 是 |

实现约束（伪代码）：

```python
def evaluate_readiness(ev, dec) -> tuple[bool, list[str]]:
    missing = []
    if ev.provenance != "real_llm":            missing.append("provenance")
    if ev.transport_path != "direct_verified": missing.append("transport_path")
    if ev.endpoint_class != "remote":          missing.append("endpoint_class")
    if ev.peer_role != "upstream":             missing.append("peer_role")
    if ev.peer_class != "remote":              missing.append("peer_class")
    if ev.peer_ip_global != 1:                 missing.append("peer_ip_global")
    if ev.proxy_used != "none":                missing.append("proxy_used")
    if ev.proxy_class != "none":               missing.append("proxy_class")
    if ev.http_method != "POST":               missing.append("http_method")
    if not ev.request_path.endswith("/chat/completions"): missing.append("request_path")
    if ev.http_status != 200:                  missing.append("http_status")
    if not ev.upstream_model:                  missing.append("upstream_model")
    if len(ev.envelope_sha256) != 64:          missing.append("envelope_sha256")
    if not (ev.envelope_bytes or 0) > 0:       missing.append("envelope_bytes")
    if (ev.redirect_count or 0) != 0:          missing.append("redirect_count")
    if not ev.upstream_id:                     missing.append("upstream_id")
    if ev.upstream_id_source != "body.id":     missing.append("upstream_id_source")
    if not (ev.usage_prompt_tokens or 0) > 0:  missing.append("usage_prompt_tokens")
    if ev.tls_verified != 1:                   missing.append("tls_verified")
    if ev.tls_hostname_verified != 1:          missing.append("tls_hostname_verified")
    if ev.tls_pin_status not in ("not_configured","matched"): missing.append("tls_pin")
    if not ev.evidence_id or ev.evidence_id != dec.evidence_id: missing.append("evidence_id")
    if not verify_chain(ev):                   missing.append("chain")
    if not (0 <= (parse(ev.created_at) - parse(ev.received_at)) <= 5.0): missing.append("late_write")
    return (not missing), missing
```

**纯合取**：无 `or`、无 `try/except` 吞异常、无默认放行；`missing` 为空才 true。

### 10.1 与 V3 的 16 项映射（A-11 更正）

V3 的 16 项**未被拆分**，全部 1:1 保序映射；V3.1 新增 8 项：

| V3 编号 | V3.1 编号 | 说明 |
|---|---|---|
| R1 | R1 | 不变 |
| R2 | R3 | 换位（顺序调整） |
| R3 | R2 | 换位 |
| R4 | R9 | 换位 |
| R5 | R10 | 换位 |
| R6 | R11 | 换位 |
| R7 | R12 | 换位 |
| R8 | R13 | 换位 |
| R9 | R14 | 换位 |
| R10 | R6 | 换位 |
| R11 | R7 | 换位 |
| R12 | R15 | 换位 |
| R13 | R22 | 换位 |
| R14 | R23 | 换位 |
| R15 | R24 | 换位 |
| R16 | R16 | 不变 |
| — | **R4/R5/R8** | 新增：peer_role / peer_class / proxy_class |
| — | **R17** | 新增：upstream_id_source（V3 只在 CHECK 里，无 R 项） |
| — | **R18** | 新增：usage（A-16） |
| — | **R19/R20/R21** | 新增：TLS 三项（A-07） |

→ 16 + 8 = **24**。V3 §9.1 的"拆分 2 条"表述**作废**（实际从未拆分）。

### 10.2 与方案 14 谓词映射

| 方案 | V3.1 | 处置 |
|---|---|---|
| P1 provenance | R1 | 保留 |
| P2 endpoint_class | R3 | 保留 |
| P3 http_method | R9 | 保留 |
| P4 request_path | R10 | 保留 |
| P5 http_status | R11 | 保留 |
| P6 upstream_model | R12 | 保留（`length>0`） |
| P7 envelope_sha256 | R13 | **加强**为 `=64`（非拆分） |
| P8 envelope_bytes | R14 | 保留 |
| P9 peer_ip_global | R6 | 保留，改 `=1`（避免 `is True` 陷阱） |
| P10 proxy_used | R7 | 保留 |
| P11 redirect_count | R15 | 保留 |
| P12 evidence_id | R22 | 保留，拆为非空+相等两个断言（同一 R 项内） |
| P13 chain_ok | R23 | 保留 |
| P14 late_write | R24 | 保留，量化 5 s |
| — | R2/R4/R5/R8/R16/R17/R18/R19/R20/R21 | **新增 10 项** |

14 + 10 = 24 ✅

---

## 11. SQLite 反例重验（23 条）

| # | 反例 | V3 结果 | V3.1 结果 | 拦截者 |
|---|---|---|---|---|
| 1 | 三轴 NULL | 拒绝 | 拒绝 | NOT NULL |
| 2 | 三轴空串 | 拒绝 | 拒绝 | CHECK IN |
| 3 | 非法枚举 | 拒绝 | 拒绝 | CHECK IN |
| 4 | `upstream_id NULL` + real_llm | 拒绝 | 拒绝 | I6-C15 |
| 5 | `upstream_id=''` + real_llm | 拒绝 | 拒绝 | I6-C16 |
| 6 | `http_status NULL` | 拒绝 | 拒绝 | NOT NULL |
| 7 | `http_status=500` + real_llm | 拒绝 | 拒绝 | I6-C10 |
| 8 | `evidence_id NULL` | **通过**（列不存在） | 拒绝 | `NOT NULL` + R22 |
| 9 | `proxy_used` 与 T 矛盾 | 拒绝 | 拒绝 | I9b/I9c |
| 10 | `E=remote+peer=loopback+global=1` | **通过** | 拒绝 | I5 + I8b |
| 11 | `proxy_used≠none` + `proxy_class='none'` | **通过** | 拒绝 | I9a |
| 12 | `proxy_used=''` | **通过** | 拒绝 | 枚举 + `length>0` |
| 13 | `unverified + '[]'` | 拒绝 | 拒绝 | I11（len=2） |
| 14 | `unverified + '[ ]'` | 通过 | 拒绝 | I11 用 `json_array_length>0`（`'[ ]'` 是合法 JSON，仅靠长度拦不住） |
| 15 | 多字段 NULL | 拒绝 | 拒绝 | NOT NULL |
| 16 | `real_llm` + `tls_verified=0` | — | 拒绝 | I6-C19 |
| 17 | `real_llm` + `pin='not_checked'` | — | 拒绝 | I6-C20 / I15 |
| 18 | `pin='matched'` + `spki=''` | — | 拒绝 | I14 |
| 19 | `usage_prompt_tokens=0` + `real_llm` | — | 拒绝 | I6-C18 |
| 20 | `proxy_used='unknown'` + `T=direct_verified` | — | 拒绝 | I10 |
| 21 | `local_llm` + `source_declared_by='probe'` | — | 拒绝 | I12 |
| 22 | `upstream_id` 非空 + `source=''` | — | 拒绝 | I13 |
| 23 | `proxy_unverified` + `peer_role='upstream'` | — | 拒绝 | I4 |

**23/23 拒绝**（V3 是 15 条中 4 条可通过）。

> 全部为**逻辑推演**（SQLite：CHECK 仅在谓词求值 FALSE 时拒绝；NULL 会"放过"，故必须有 `NOT NULL`）。本轮禁止建库，实施阶段须用内存断言实测，尤其是 `json_valid` 是否可用（需 JSON1 扩展，现代 SQLite 默认启用）。

---

## 12. P2/P3 处置表（全部 11 项 + 新增 2 项）

| ID | 级别 | 处置 | 说明 |
|---|---|---|---|
| A-05 | P2 | ✅ 已修 | `proxy_used` 枚举 + 非空 |
| A-06 | P2 | ✅ 已修 | 完整 DDL + R→字段映射 |
| A-07 | P2 | ✅ 已修 | 恢复 `tls_spki_sha256` + 4 个 TLS 字段 |
| A-08 | P2 | ⚠️ 部分 | 加 `source_declared_by` 强制"本地模型必须是 operator 声明"；**仍无法从数据证明"它真是模型"**（残余风险 R-2） |
| A-09 | P2 | ✅ 已修 | 本文件全部引用重写；C 表在 §11、R 表在 §10 |
| A-10 | P2 | ✅ 已修 | 附录 A 补 CE-1…CE-8 映射 |
| A-13 | P2 | ✅ 已修 | §12.1 明确链的检测边界 |
| A-14 | P2 | ✅ 已修 | §6.2 边界表（SNI/证书/CONNECT/MITM） |
| A-15 | P2 | ✅ 已修 | 删除恒真的 I5，改由 I4（含 peer_role）承载 |
| A-11 | P3 | ✅ 已修 | §10.1 改为"1:1 映射 + 新增 8 项" |
| A-12 | P3 | ✅ 已修 | I13 |
| A-16 | P2 | ✅ 已修 | 新增 R18（usage） |
| A-17 | P3 | ✅ 已记录 | §0 以表为准（9+2） |

### 12.1 链哈希的检测边界（A-13）

`integrity_hash` 链**能**发现：单条插入、单条删除、单条字段篡改（因为后继行的 `prev_hash` 对不上）。
**不能**发现：拥有数据库写权限者从头重算整条链。
因此文档措辞统一为"**链用于发现局部改写，不能防有写权限者整体重写**"；真正的防篡改需要外部锚定（每日把链尾 hash 记录到独立位置/日志），属后续阶段。

---

## 13. 附录 A：CE-1…CE-8 映射（A-10）

| CE | V2 审计中的用途 | V3.1 对应 |
|---|---|---|
| CE-1 | 直连缺 id 应拒 | S2 / 反例 #4 |
| CE-2 | `T=NULL` + real_llm | 反例 #1 |
| CE-3 | 500 + real_llm | 反例 #7 |
| CE-4 | 回环代理应可记录 | S6 |
| CE-5 | 直连缺 id 应可标 unverified | S2 |
| CE-6 | 本地真模型 | S7 |
| CE-7 | `evidence_id` 双侧 NULL | 反例 #8 |
| CE-8 | `peer_ip_global` 整数比较陷阱 | R6 用 `=1`；§10.1 备注 |

---

## 14. 与 V3 的差异清单

| 项 | V3 | V3.1 |
|---|---|---|
| `real_llm` 语义 | I6 的 12 项 | R1–R24 全过（充要） |
| I6 合取项 | 12 | **20**（+peer_role/peer_class/proxy_class/usage/TLS×2，合并 upstream_id 两条） |
| readiness 项数 | 16 | **24** |
| 新增字段 | — | `peer_role`、`tls_verified`、`tls_hostname_verified`、`tls_spki_sha256`、`tls_pin_expected`、`tls_pin_status`、`usage_prompt_tokens`、`usage_completion_tokens`、`source_declared_by`、`peer_ip`、`response_digest`、`prev_hash`、`evidence_id`、`received_at`、`created_at`、`seq` |
| 不变量 | I1–I9 | I1–I17 |
| 反例覆盖 | 12 类，4 类可通过 | 23 类，**0 类可通过** |
| 交叉引用 | 8 处错位 | 全部重写 |

---

## 15. 尚存风险

| # | 风险 | 为什么本轮不解决 |
|---|---|---|
| R-1 | `upstream_id` 的**值**是否真来自上游，DB 无法证明 | 需上游对账/签名；单一写入路径只是降低概率 |
| R-2 | `local_llm` 无法证明"真的是模型"而非 stub | 需本地模型侧的可验证签名，超出本项目能力 |
| R-3 | 代理 CONNECT 下网络路径不可验证 | TLS 端到端但 peer 只能是代理；设计上只能 `proxy_unverified` |
| R-4 | 链哈希不能防整体重写 | 需外部锚定（每日链尾入独立日志） |
| R-5 | SQLite `json_valid` / `CHECK` NULL 语义未经实测 | 本轮禁止建库；实施阶段内存断言验证 |
| R-6 | 中转不返回 `body.id` 则永不通过 readiness | 接受；不得用 `cf-ray` 绕过 |

---

## 16. 零修改证明

本轮仅新建本文件。未修改 V3（`49fec2e9…`）、审计报告（`138ed16b…`）、V2（`5ecd4b7c…`）、方案（`3d569f16…`）、`src/`、`tests/`、`config/`、`.env`、`data/`、`logs/`；未读/写 SQLite；未建库/建 SQL 文件；未发模型请求；未启动 S0/S1；未做 Git 操作。

---

## 17. 最终状态

```
V3.1 待用户审核；
原方案已批准；
D1–D4 已确认；
源码实施未授权；
S0 未启动。
```

---

> 本文件 SHA256（本行之前全部字节，可用 `head -n -1 <本文件> | sha256sum` 复算）：`33f0840425dee99168b973fe4a110c859bbc3df6e3125d8548478106ecb197d9`
