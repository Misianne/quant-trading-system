# V3.3 修订方案（设计稿 · 不实施）

| 项 | 值 |
|---|---|
| 版本 | **v3.3**（2026-11-10 22:40） |
| 取代 | `US_REAL_LLM_EVIDENCE_V3_2_SCHEME.md`（`ef743a86…bf76a`，**不覆盖、不修改**） |
| 依据 | V3.2 独立审计（FAIL：2×P0 / 11×P1 / 8×P2 / 5×P3）、V3.1 独立审计（4×P1 / 9×P2 / 2×P3）、V2 逻辑审计、原修复计划 |
| 性质 | **设计文档**。本轮未执行任何 SQL、未建库、未改源码/配置/测试/数据 |
| 状态 | **V3.3 待独立审计**；方案未获批；未获实施授权；S0 未启动 |

> 本文件是设计稿。文中所有 SQL 均为**静态文本**，未经任何 SQLite 实例执行。凡标注「需隔离实测」的结论，均不得当作已验证。

---

## 1. 基线核验（本轮执行前实测）

| 文件 | 字节 | 行 | SHA256 | 判定 |
|---|---|---|---|---|
| `US_REAL_LLM_EVIDENCE_V3_2_SCHEME.md` | 27,271 | 441 | `ef743a86a1c2c21b6282834b76084d7ac757e8cde30699a12c63933bef4bf76a` | ✅ 与任务书一致 |
| `US_REAL_LLM_EVIDENCE_DECISIONS_V3_1.md` | 34,533 | 645 | `8b4a0d0bb77af7051b83e555b7847f4f7c58d643223952a92ce83db9d0c992f8` | 未变 |
| `US_REAL_LLM_EVIDENCE_DECISIONS_V3.md` | 24,408 | 373 | `49fec2e970bf2f2130e4b22ceac47e65d2ed25df1ec807cae41216efb339fb36` | 未变 |
| `US_REAL_LLM_EVIDENCE_V3_INDEPENDENT_AUDIT.md` | 19,907 | 311 | `138ed16bf71861b140063de89b12150002f1416491804ab9fd3afbc39fb798cf` | 未变 |
| `US_REAL_LLM_EVIDENCE_V2_LOGIC_AUDIT.md` | 22,412 | 313 | `9e21e56f2d959af8646a0ca0225516c20f5e54f7ffac8932509589f8f8d8dab8` | 未变 |
| `US_REAL_LLM_EVIDENCE_REMEDIATION_PLAN.md` | 27,586 | 534 | `3d569f16754e389c77534cbbbfc3e294ad5d6bba67946fb7bb9f3f7fd629610a` | 未变 |

目标文件 `US_REAL_LLM_EVIDENCE_V3_3_SCHEME.md` 创建前不存在（未覆盖任何文件）。

---

## 2. 问题追踪表（V3.2 审计 2×P0 + 11×P1 + 8×P2 + 5×P3；V3.1 遗留 N-1…N-13）

| 审计项 | 级别 | 问题 | V3.3 落点 | 方式 |
|---|---|---|---|---|
| **P0-1 UPDATE 绕过** | P0 | TRG-1 仅 `BEFORE INSERT` | §6 `trg_dec_claim_ins` + `trg_dec_claim_upd`（**两个独立触发器**） | DB 强制 |
| **P0-2 FK 与插入顺序冲突** | P0 | 子先父后 + 立即 FK 必失败 | §5 双向 FK 均 `DEFERRABLE INITIALLY DEFERRED`；§8 事务顺序逐条推演 | DB 强制 |
| **P1-1 decisions DDL 缺失** | P1 | 无 CREATE TABLE，父键未声明 | §5.1 完整 DDL（`decision_id TEXT PRIMARY KEY NOT NULL`） | DB 强制 |
| **P1-2 TLS Pin** | P1 | expected 与 actual 从未比较 | §5.1 `CHECK(tls_pin_status<>'matched' OR tls_pin_expected=tls_spki_sha256)`；E-20 要求 `matched` | DB 强制 |
| **P1-3 X-02/X-03** | P1 | 仅应用层；ISO 文本减法 | §5 增 `unix_*` 整数列 + CHECK；§5.2 `trg_ev_chain_ins` 强制链**连续性**；§14 划清边界 | DB 部分 + 残余 |
| **P1-4 JSON** | P1 | TRG-4 不存在；INSERT 无元素校验 | §5.2 `trg_ev_json_ins`（`json_each` 逐元素）；UPDATE 被 `trg_ev_no_update` 整体封死 | DB 强制 |
| **P1-5 历史判定** | P1 | 用 `IS NULL` 推断历史 | §5.1 新增 `record_origin ∈ ('live','migrated')` + `migration_batch`；§7 视图按 origin 分支 | DB 强制 |
| **P1-6 NULL 三值** | P1 | `< >` 遇 NULL 落 ELSE | §7 视图逐条 `IS NULL` 显式分支 | DB 视图 |
| **P1-7 S 状态机** | P1 | 500+model 被判 unverified | §10 重排优先级，`rejected` 先于 `unverified`，且 unverified 要求 200 | 文档 + 应用 |
| **P1-8 迁移** | P1 | PRAGMA 写在事务内（no-op） | §11 明确「事务外、每连接」；失败用 ROLLBACK 而非还原备份 | 运维流程 |
| F-1 CASCADE 冲突 | P2 | 删除语义未定 | §5.3 全面禁止物理删除（两表 `BEFORE DELETE` ABORT）+ `ON DELETE RESTRICT` | DB 强制 |
| I-2 视图未纳入重建 | P2 | RENAME 破坏视图 | §11 步骤表：先 DROP VIEW 再重建 | 运维流程 |
| I-3 还原备份覆盖新数据 | P2 | — | §11.4 明确「备份只用于灾难恢复，不作为回滚」 | 运维流程 |
| J-1 哈希仅校验长度 | P2 | `'Z'*64` 可过 | §5.1 全部哈希列加 `NOT GLOB '*[^0-9a-f]*'` | DB 强制 |
| J-2 TEXT PK 可 NULL | P2 | SQLite 旧行为 | §5.1 `evidence_id TEXT PRIMARY KEY NOT NULL` | DB 强制 |
| J-3 seq 可改 | P2 | 破坏链 | §5.2 证据表**整体不可 UPDATE**（`trg_ev_no_update`） | DB 强制 |
| J-4 proxy_env 不一致 | P2 | 用 env 代理却记 absent | §5.1 I-19 CHECK | DB 强制 |
| J-5…J-9 | P3 | 编号表错位、交叉引用、PRAGMA 连接级、首行历史陈述 | §2 / §11.1 / §21 修正 | 文档 |

**V3.1 遗留项**：N-1→§3；N-2→§4/§5；N-3→§8；N-4→§5.3；N-5→§4；N-6→§5.1 `UNIQUE(decision_id,seq)`；N-7→§5.2/§15；N-8→§15.3；N-9→§11；N-10→§21；N-11→§4；N-12→§15；N-13→§13。

---

## 3. 三轴模型（完整定义，含第四维「来源世代」）

| 轴 | 取值 | 语义 | 谁观测 |
|---|---|---|---|
| `endpoint_class` | `remote` / `loopback` / `private` / `unknown` | 请求**目标**地址类别（DNS 解析 + 全地址判定） | 连接前 |
| `transport_path` | `direct_verified` / `proxy_unverified` / `local` / `private` / `unknown` | **怎么连的**、对端是否验证过 | 连接后 |
| `provenance`（决策表）= `attempt_verdict`（证据表） | `real_llm` / `real_llm_unverified` / `local_llm` / `stub_transport` / `rejected` / `unavailable` / `unknown` | 单次尝试 / 决策声明的结论 | 规则计算 |
| **`record_origin`**（新增） | `live` / `migrated` | 该决策是**本次运行产生**还是**历史迁移** | 写入方声明 |

> `record_origin` 是 V3.2 G-1 的根治手段：**历史身份由显式字段承载，不再由「指针为 NULL」推断**。

附加观测字段（不参与不变量，只如实记录）：`peer_ip`、`proxy_env_present`、`network_request_id(_source)`、`tls_spki_sha256`、`tls_pin_expected`、`source_declared_by`、`voided_at`。

---

## 4. 规则编号体系（单一命名空间，共 50 条：E20 + I20 + X3 + S7）

| 族 | 范围 | 含义 |
|---|---|---|
| `E-01…E-20` | `real_llm` 的 20 项必要条件 | 单行 CHECK |
| `I-01…I-20` | 跨轴/跨字段一致性 | 单行 CHECK |
| `X-01…X-03` | 跨行/跨表 | 触发器 + 视图 |
| `S-1…S-7` | 状态判定优先级 | 应用规则 |

### 4.1 E 族（20 项，与 §5.1 的 E-CHECK 逐项对应）

| 编号 | 字段 | 条件 |
|---|---|---|
| E-01 | transport_path | `='direct_verified'` |
| E-02 | endpoint_class | `='remote'` |
| E-03 | peer_role | `='upstream'` |
| E-04 | peer_class | `='remote'` |
| E-05 | peer_ip_global | `=1` |
| E-06 | proxy_used | `='none'` |
| E-07 | proxy_class | `='none'` |
| E-08 | http_method | `='POST'` |
| E-09 | request_path | `LIKE '%/chat/completions'` |
| E-10 | http_status | `=200` |
| E-11 | upstream_model | `length>0` |
| E-12 | envelope_sha256 | `length=64` 且全 hex |
| E-13 | envelope_bytes | `>0` |
| E-14 | redirect_count | `=0` |
| E-15 | upstream_id | `IS NOT NULL` |
| E-16 | upstream_id | `length>0` |
| E-17 | upstream_id_source | `='body.id'` |
| E-18 | usage_prompt_tokens | `>0` |
| E-19 | tls_verified ∧ tls_hostname_verified | `=1 ∧ =1` |
| **E-20** | tls_pin_status | **`='matched'`**（V3.2 允许 `not_configured`，V3.3 依「默认启用 Pin、fail closed」收紧） |

> E-20 收紧的操作后果：**未配置 Pin 的环境无法产生 `real_llm`**。首次部署须人工采集 SPKI 并写入配置（非秘密），否则只能得到 `real_llm_unverified`。这是有意的 fail-closed。

### 4.2 I 族（20 条，全部落 CHECK）

| 编号 | 条件 |
|---|---|
| I-01 | `T='local' ⇒ E='loopback'` |
| I-02 | `T='private' ⇒ E='private'` |
| I-03 | `T='direct_verified' ⇒ E='remote'` |
| I-04 | `T='proxy_unverified' ⇒ proxy_class∈('loopback','private','remote') ∧ peer_role='proxy' ∧ peer_class=proxy_class` |
| I-05 | `T='direct_verified' ⇒ peer_role='upstream' ∧ peer_class='remote' ∧ peer_ip_global=1` |
| I-06 | `T='local' ⇒ peer_role='upstream' ∧ peer_class='loopback' ∧ peer_ip_global=0` |
| I-07 | `T='private' ⇒ peer_role='upstream' ∧ peer_class='private' ∧ peer_ip_global=0` |
| I-08 | `peer_class='remote' ⇒ peer_ip_global=1` |
| I-09 | `peer_class∈('loopback','private') ⇒ peer_ip_global=0` |
| I-10 | `proxy_used='none' ⇒ proxy_class='none'` |
| I-11 | `proxy_class='none' ⇒ proxy_used='none'` |
| I-12 | `proxy_used LIKE 'env:%' ⇒ T='proxy_unverified'` |
| I-13 | `proxy_used LIKE 'explicit:%' ⇒ T='proxy_unverified'` |
| I-14 | `T='proxy_unverified' ⇒ proxy_used LIKE 'env:%' OR proxy_used LIKE 'explicit:%'` |
| I-15 | `proxy_used='unknown' ⇒ T='unknown' ∧ P≠'real_llm'` |
| I-16 | `P='real_llm_unverified' ⇒ json_array_length(reasons)>0` |
| I-17 | `P='local_llm' ⇒ T∈('local','private') ∧ E∈('loopback','private') ∧ source_declared_by='operator'` |
| I-18 | `upstream_id IS NULL OR length(upstream_id_source)>0` |
| I-19 | `tls_pin_status<>'matched' OR (tls_pin_expected=tls_spki_sha256 ∧ length=64)` |
| I-20 | `proxy_used NOT LIKE 'env:%' OR proxy_env_present=1` |

### 4.3 X 族与 S 族

| 编号 | 内容 | 层 |
|---|---|---|
| X-01 | 决策指针非空且指向同一 `decision_id` 的证据、且 `attempt_verdict='real_llm'` | 触发器（两处） |
| X-02 | 链**连续性**：`seq=1 ⇒ prev_hash=''`；`seq>1 ⇒ prev_hash = 前一条 integrity_hash` | 触发器（DB 强制）<br/>哈希**正确性**：应用层（残余 R-2） |
| X-03 | `0 ≤ unix_created_at − unix_received_at ≤ 5` | CHECK（DB 强制） |

S 族见 §10。

---

## 5. 数据定义（完整 DDL，不执行）

### 5.1 表

```sql
-- =====================================================================
-- 连接级设置：必须在任何事务之前执行，且每个连接各自执行一次
-- =====================================================================
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- =====================================================================
-- 表 1：ai_decisions（决策声明）
-- =====================================================================
CREATE TABLE ai_decisions (
    decision_id      TEXT PRIMARY KEY NOT NULL
                     CHECK (length(decision_id) > 0
                        AND decision_id NOT GLOB '*[^0-9a-zA-Z_.:-]*'),

    run_id           TEXT,
    symbol           TEXT NOT NULL CHECK (length(symbol) > 0),
    decided_at       TEXT NOT NULL CHECK (length(decided_at) > 0),

    -- 声明（claim），不是事实
    provenance       TEXT NOT NULL CHECK (provenance IN
                       ('real_llm','real_llm_unverified','local_llm',
                        'stub_transport','rejected','unavailable','unknown')),

    authoritative_evidence_id TEXT UNIQUE,       -- 多 NULL 允许；非空时必须存在

    record_origin    TEXT NOT NULL DEFAULT 'live'
                     CHECK (record_origin IN ('live','migrated')),
    migration_batch  TEXT,

    voided_at        TEXT,
    void_reason      TEXT,

    created_at       TEXT NOT NULL CHECK (length(created_at) > 0),

    -- 迁移行必须带批次号
    CHECK (record_origin <> 'migrated' OR length(migration_batch) > 0),
    -- 作废必须成对出现
    CHECK ((voided_at IS NULL AND void_reason IS NULL)
        OR (voided_at IS NOT NULL AND length(void_reason) > 0)),
    -- 声明 real_llm 必须携带权威证据指针且未作废；
    -- 迁移行例外：历史 real_llm 保留原 claim、指针为 NULL，由视图改写为
    -- historical_transport_unverified（D4：不改原值 + 独立标注）
    CHECK (provenance <> 'real_llm'
        OR record_origin = 'migrated'
        OR (authoritative_evidence_id IS NOT NULL AND voided_at IS NULL)),

    FOREIGN KEY (authoritative_evidence_id)
        REFERENCES ai_transport_evidence(evidence_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT
        DEFERRABLE INITIALLY DEFERRED
);

-- =====================================================================
-- 表 2：ai_transport_evidence（传输证据，append-only）
-- =====================================================================
CREATE TABLE ai_transport_evidence (
    evidence_id      TEXT PRIMARY KEY NOT NULL
                     CHECK (length(evidence_id) = 64
                        AND evidence_id NOT GLOB '*[^0-9a-f]*'),

    decision_id      TEXT NOT NULL CHECK (length(decision_id) > 0),
    seq              INTEGER NOT NULL CHECK (seq >= 1),
    run_id           TEXT,
    symbol           TEXT NOT NULL CHECK (length(symbol) > 0),

    -- ---- 三轴 ----
    endpoint_class   TEXT NOT NULL CHECK (endpoint_class IN
                       ('remote','loopback','private','unknown')),
    transport_path   TEXT NOT NULL CHECK (transport_path IN
                       ('direct_verified','proxy_unverified','local','private','unknown')),
    attempt_verdict  TEXT NOT NULL CHECK (attempt_verdict IN
                       ('real_llm','real_llm_unverified','local_llm',
                        'stub_transport','rejected','unavailable','unknown')),

    -- ---- 对端/代理观测 ----
    peer_role        TEXT NOT NULL CHECK (peer_role IN ('upstream','proxy','unknown')),
    peer_class       TEXT NOT NULL CHECK (peer_class IN
                       ('remote','loopback','private','unknown')),
    peer_ip          TEXT,
    peer_ip_global   INTEGER NOT NULL DEFAULT 0 CHECK (peer_ip_global IN (0,1)),

    proxy_used       TEXT NOT NULL DEFAULT 'none'
                     CHECK (proxy_used IN ('none','unknown')
                         OR proxy_used LIKE 'env:%'
                         OR proxy_used LIKE 'explicit:%'),
    proxy_class      TEXT NOT NULL DEFAULT 'none' CHECK (proxy_class IN
                       ('none','loopback','private','remote','unknown')),
    proxy_env_present INTEGER NOT NULL DEFAULT 0 CHECK (proxy_env_present IN (0,1)),

    -- ---- HTTP ----
    http_method      TEXT NOT NULL CHECK (http_method = 'POST'),
    request_path     TEXT NOT NULL CHECK (length(request_path) > 0),
    http_status      INTEGER NOT NULL CHECK (http_status BETWEEN 0 AND 599),
    redirect_count   INTEGER NOT NULL DEFAULT 0 CHECK (redirect_count >= 0),

    -- ---- 响应证据 ----
    upstream_model   TEXT NOT NULL DEFAULT '',
    upstream_id      TEXT,
    upstream_id_source TEXT NOT NULL DEFAULT ''
                     CHECK (upstream_id_source IN ('','body.id')),
    network_request_id        TEXT,
    network_request_id_source TEXT NOT NULL DEFAULT ''
                     CHECK (network_request_id_source IN ('','x-request-id','cf-ray')),
    envelope_sha256  TEXT NOT NULL DEFAULT ''
                     CHECK (length(envelope_sha256) IN (0,64)
                        AND envelope_sha256 NOT GLOB '*[^0-9a-f]*'),
    envelope_bytes   INTEGER NOT NULL DEFAULT 0 CHECK (envelope_bytes >= 0),
    usage_prompt_tokens     INTEGER NOT NULL DEFAULT 0 CHECK (usage_prompt_tokens >= 0),
    usage_completion_tokens INTEGER NOT NULL DEFAULT 0 CHECK (usage_completion_tokens >= 0),

    -- ---- TLS ----
    tls_verified          INTEGER NOT NULL DEFAULT 0 CHECK (tls_verified IN (0,1)),
    tls_hostname_verified INTEGER NOT NULL DEFAULT 0 CHECK (tls_hostname_verified IN (0,1)),
    tls_spki_sha256   TEXT NOT NULL DEFAULT ''
                      CHECK (length(tls_spki_sha256) IN (0,64)
                         AND tls_spki_sha256 NOT GLOB '*[^0-9a-f]*'),
    tls_pin_expected  TEXT NOT NULL DEFAULT ''
                      CHECK (length(tls_pin_expected) IN (0,64)
                         AND tls_pin_expected NOT GLOB '*[^0-9a-f]*'),
    tls_pin_status    TEXT NOT NULL DEFAULT 'not_checked'
                      CHECK (tls_pin_status IN
                        ('not_configured','matched','mismatched','not_checked')),

    -- ---- 时间（双表示：人类可读 + 可计算）----
    received_at       TEXT NOT NULL CHECK (length(received_at) > 0),
    unix_received_at  INTEGER NOT NULL CHECK (unix_received_at > 0),
    created_at        TEXT NOT NULL CHECK (length(created_at) > 0),
    unix_created_at   INTEGER NOT NULL CHECK (unix_created_at > 0),

    source_declared_by TEXT NOT NULL DEFAULT 'probe'
                      CHECK (source_declared_by IN ('probe','operator')),
    unverified_reasons TEXT NOT NULL DEFAULT '[]'
                      CHECK (json_valid(unverified_reasons)
                         AND json_type(unverified_reasons) = 'array'),

    integrity_hash   TEXT NOT NULL
                     CHECK (length(integrity_hash) = 64
                        AND integrity_hash NOT GLOB '*[^0-9a-f]*'),
    prev_hash        TEXT NOT NULL DEFAULT ''
                     CHECK (prev_hash = ''
                        OR (length(prev_hash) = 64
                        AND prev_hash NOT GLOB '*[^0-9a-f]*')),

    -- ---- 唯一性与引用 ----
    UNIQUE (decision_id, seq),
    FOREIGN KEY (decision_id) REFERENCES ai_decisions(decision_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT
        DEFERRABLE INITIALLY DEFERRED,

    -- ================= E 族：real_llm 的 20 项必要条件 =================
    CHECK (attempt_verdict <> 'real_llm' OR (
            transport_path          = 'direct_verified'   -- E-01
        AND endpoint_class          = 'remote'            -- E-02
        AND peer_role               = 'upstream'          -- E-03
        AND peer_class              = 'remote'            -- E-04
        AND peer_ip_global          = 1                   -- E-05
        AND proxy_used              = 'none'              -- E-06
        AND proxy_class             = 'none'              -- E-07
        AND http_method             = 'POST'              -- E-08
        AND request_path LIKE '%/chat/completions'        -- E-09
        AND http_status             = 200                 -- E-10
        AND length(upstream_model)  > 0                   -- E-11
        AND length(envelope_sha256) = 64                  -- E-12
        AND envelope_bytes          > 0                   -- E-13
        AND redirect_count          = 0                   -- E-14
        AND upstream_id IS NOT NULL                       -- E-15
        AND length(upstream_id)     > 0                   -- E-16
        AND upstream_id_source      = 'body.id'           -- E-17
        AND usage_prompt_tokens     > 0                   -- E-18
        AND tls_verified            = 1                   -- E-19a
        AND tls_hostname_verified   = 1                   -- E-19b
        AND tls_pin_status          = 'matched'           -- E-20a
        AND tls_pin_expected        = tls_spki_sha256     -- E-20b
        AND unix_created_at        >= unix_received_at    -- X-03a
        AND unix_created_at - unix_received_at <= 5       -- X-03b
    )),

    -- ================= I 族：20 条跨轴一致性 =================
    CHECK (transport_path <> 'local'  OR endpoint_class = 'loopback'),               -- I-01
    CHECK (transport_path <> 'private' OR endpoint_class = 'private'),               -- I-02
    CHECK (transport_path <> 'direct_verified' OR endpoint_class = 'remote'),        -- I-03
    CHECK (transport_path <> 'proxy_unverified' OR (proxy_class IN
             ('loopback','private','remote')
             AND peer_role = 'proxy' AND peer_class = proxy_class)),                 -- I-04
    CHECK (transport_path <> 'direct_verified' OR (peer_role = 'upstream'
             AND peer_class = 'remote' AND peer_ip_global = 1)),                     -- I-05
    CHECK (transport_path <> 'local' OR (peer_role = 'upstream'
             AND peer_class = 'loopback' AND peer_ip_global = 0)),                   -- I-06
    CHECK (transport_path <> 'private' OR (peer_role = 'upstream'
             AND peer_class = 'private' AND peer_ip_global = 0)),                    -- I-07
    CHECK (peer_class <> 'remote' OR peer_ip_global = 1),                            -- I-08
    CHECK (peer_class NOT IN ('loopback','private') OR peer_ip_global = 0),          -- I-09
    CHECK (proxy_used <> 'none' OR proxy_class = 'none'),                            -- I-10
    CHECK (proxy_class <> 'none' OR proxy_used = 'none'),                            -- I-11
    CHECK (proxy_used NOT LIKE 'env:%' OR transport_path = 'proxy_unverified'),      -- I-12
    CHECK (proxy_used NOT LIKE 'explicit:%' OR transport_path = 'proxy_unverified'), -- I-13
    CHECK (transport_path <> 'proxy_unverified'
        OR proxy_used LIKE 'env:%' OR proxy_used LIKE 'explicit:%'),                 -- I-14
    CHECK (proxy_used <> 'unknown' OR (transport_path = 'unknown'
             AND attempt_verdict <> 'real_llm')),                                    -- I-15
    CHECK (attempt_verdict <> 'real_llm_unverified'
        OR json_array_length(unverified_reasons) > 0),                               -- I-16
    CHECK (attempt_verdict <> 'local_llm' OR (transport_path IN ('local','private')
             AND endpoint_class IN ('loopback','private')
             AND source_declared_by = 'operator')),                                  -- I-17
    CHECK (upstream_id IS NULL OR length(upstream_id_source) > 0),                   -- I-18
    CHECK (tls_pin_status <> 'matched' OR (tls_pin_expected = tls_spki_sha256
             AND length(tls_pin_expected) = 64)),                                    -- I-19
    CHECK (proxy_used NOT LIKE 'env:%' OR proxy_env_present = 1)                     -- I-20
);

CREATE INDEX ix_ev_decision ON ai_transport_evidence(decision_id, seq);
CREATE INDEX ix_dec_origin   ON ai_decisions(record_origin, provenance);
```

### 5.2 触发器（7 个，全部给出完整 SQL）

```sql
-- ---------------------------------------------------------------------
-- TRG-1a：决策 INSERT —— 声明 real_llm 必须有匹配的权威证据
-- ---------------------------------------------------------------------
CREATE TRIGGER trg_dec_claim_ins
BEFORE INSERT ON ai_decisions
WHEN NEW.provenance = 'real_llm'
 AND NOT EXISTS (
       SELECT 1 FROM ai_transport_evidence e
        WHERE e.evidence_id    = NEW.authoritative_evidence_id
          AND e.decision_id    = NEW.decision_id
          AND e.attempt_verdict = 'real_llm')
BEGIN
  SELECT RAISE(ABORT, 'real_llm claim without matching authoritative evidence');
END;

-- ---------------------------------------------------------------------
-- TRG-1b：决策 UPDATE —— 与 INSERT 完全同构（P0-1 的修复点）
-- ---------------------------------------------------------------------
CREATE TRIGGER trg_dec_claim_upd
BEFORE UPDATE ON ai_decisions
WHEN NEW.provenance = 'real_llm'
 AND NOT EXISTS (
       SELECT 1 FROM ai_transport_evidence e
        WHERE e.evidence_id    = NEW.authoritative_evidence_id
          AND e.decision_id    = NEW.decision_id
          AND e.attempt_verdict = 'real_llm')
BEGIN
  SELECT RAISE(ABORT, 'real_llm claim without matching authoritative evidence (update)');
END;

-- ---------------------------------------------------------------------
-- TRG-1c：决策不可物理删除（证据保留策略）
-- ---------------------------------------------------------------------
CREATE TRIGGER trg_dec_no_delete
BEFORE DELETE ON ai_decisions
BEGIN
  SELECT RAISE(ABORT, 'decisions are append-only; use voided_at instead of delete');
END;

-- ---------------------------------------------------------------------
-- TRG-2：证据整行不可 UPDATE（append-only；新尝试另起 seq）
--        该触发器同时封死"改 seq / 改 verdict / 改 JSON"的全部路径
-- ---------------------------------------------------------------------
CREATE TRIGGER trg_ev_no_update
BEFORE UPDATE ON ai_transport_evidence
BEGIN
  SELECT RAISE(ABORT, 'evidence is append-only; insert a new seq instead of update');
END;

-- ---------------------------------------------------------------------
-- TRG-3：证据不可物理删除
-- ---------------------------------------------------------------------
CREATE TRIGGER trg_ev_no_delete
BEFORE DELETE ON ai_transport_evidence
BEGIN
  SELECT RAISE(ABORT, 'evidence is append-only; deletion is forbidden');
END;

-- ---------------------------------------------------------------------
-- TRG-4：链连续性（X-02 的数据库部分）
--        seq=1 必须 prev_hash=''；seq>1 必须等于前一条的 integrity_hash
-- ---------------------------------------------------------------------
CREATE TRIGGER trg_ev_chain_ins
BEFORE INSERT ON ai_transport_evidence
WHEN (NEW.seq = 1 AND NEW.prev_hash <> '')
  OR (NEW.seq > 1 AND NEW.prev_hash <> COALESCE((
        SELECT p.integrity_hash FROM ai_transport_evidence p
         WHERE p.decision_id = NEW.decision_id AND p.seq = NEW.seq - 1), '~'))
BEGIN
  SELECT RAISE(ABORT, 'integrity chain broken or seq gap');
END;

-- ---------------------------------------------------------------------
-- TRG-5（JSON）：unverified_reasons 元素级校验（INSERT 路径）
--   注：V3.2 §8 三处引用的「TRG-4」从未定义；V3.3 不再使用该名称，
--   JSON 校验一律由本触发器（trg_ev_json_ins）承担。
--        -- 根类型与空数组由列 CHECK + I-16 负责
--        -- 元素必须是白名单内的非空字符串
-- ---------------------------------------------------------------------
CREATE TRIGGER trg_ev_json_ins
BEFORE INSERT ON ai_transport_evidence
WHEN EXISTS (
      SELECT 1 FROM json_each(NEW.unverified_reasons) j
       WHERE j.type <> 'text'
          OR trim(j.value) = ''
          OR j.value NOT IN (
               'missing_upstream_id','missing_usage','envelope_digest_failed',
               'proxy_path','status_not_200','tls_verify_failed','tls_pin_mismatch',
               'model_mismatch','transport_unverified','response_unparsable','other'))
BEGIN
  SELECT RAISE(ABORT, 'invalid unverified_reasons element');
END;
```

> **关于 UPDATE 路径的 JSON 校验**：`trg_ev_no_update` 使证据表**不可能被 UPDATE**，因此「更新后 JSON 变非法」这条路径在结构上不存在。这不是遗漏，而是比"逐个字段校验 UPDATE"更强的保证。反例 CE-27/CE-28 验证该结论。

### 5.3 删除策略

| 对象 | 物理删除 | 逻辑作废 |
|---|---|---|
| `ai_decisions` | **禁止**（TRG-1c） | `UPDATE voided_at/void_reason`（允许；视图据此判 `voided`） |
| `ai_transport_evidence` | **禁止**（TRG-3） | 不可作废（证据永远保留） |

FK 用 `ON DELETE RESTRICT` 作为第二道防线；因此**不存在** CASCADE 与保护触发器互相冲突的问题（V3.2 F-1 的修复）。

---

## 6. 视图

```sql
-- ---------------------------------------------------------------------
-- v_decision_verdict：唯一对外结论出口
-- 所有 NULL 情形显式处理，不依赖 "<>" 的三值结果
-- ---------------------------------------------------------------------
CREATE VIEW v_decision_verdict AS
SELECT
    d.decision_id,
    d.symbol,
    d.record_origin,
    d.migration_batch,
    d.provenance            AS claimed_verdict,
    d.authoritative_evidence_id,
    e.evidence_id           AS resolved_evidence_id,
    e.attempt_verdict       AS evidence_verdict,
    e.decision_id           AS evidence_decision_id,
    d.voided_at,
    CASE
        WHEN d.voided_at IS NOT NULL                     THEN 'voided'
        WHEN d.provenance <> 'real_llm'                  THEN d.provenance
        WHEN d.record_origin = 'migrated'                THEN 'historical_transport_unverified'
        WHEN d.authoritative_evidence_id IS NULL         THEN 'integrity_violation'
        WHEN e.evidence_id IS NULL                       THEN 'integrity_violation'
        WHEN e.decision_id IS NULL
          OR e.decision_id <> d.decision_id              THEN 'integrity_violation'
        WHEN e.attempt_verdict IS NULL                   THEN 'integrity_violation'
        WHEN e.attempt_verdict <> 'real_llm'             THEN 'integrity_violation'
        ELSE 'real_llm'
    END                     AS effective_verdict
FROM ai_decisions d
LEFT JOIN ai_transport_evidence e
       ON e.evidence_id = d.authoritative_evidence_id;

-- ---------------------------------------------------------------------
-- v_evidence_seq：每条决策的证据序列（审计用）
-- ---------------------------------------------------------------------
CREATE VIEW v_evidence_seq AS
SELECT decision_id,
       seq,
       evidence_id,
       attempt_verdict,
       endpoint_class,
       transport_path,
       http_status,
       received_at,
       integrity_hash,
       prev_hash
  FROM ai_transport_evidence;
```

**分支语义（逐条对应 P1-6）**：

| 情形 | effective_verdict | 理由 |
|---|---|---|
| 已作废 | `voided` | 逻辑删除优先于一切 |
| claim ≠ real_llm | 原 claim | 非真实声明，无需校验 |
| 迁移行 + claim=real_llm | `historical_transport_unverified` | D4：不追溯认证 |
| live + 指针 NULL | `integrity_violation` | **新记录不得靠 NULL 伪装历史** |
| LEFT JOIN 无匹配（悬空指针） | `integrity_violation` | 显式判 `e.evidence_id IS NULL` |
| 证据 decision_id 不符 | `integrity_violation` | 跨表归属 |
| verdict 为 NULL | `integrity_violation` | 显式判 NULL |
| verdict ≠ real_llm | `integrity_violation` | claim 与证据矛盾 |

> 注：视图第二分支 `d.provenance <> 'real_llm'` 之所以安全，是因为 `provenance` 列 `NOT NULL`（§5.1），不存在 NULL 落入该比较的情况。

---

## 7. 事务写入（伪代码 + 顺序推演）

```python
def record_attempt(conn, decision_id, symbol, claim, evidence_row) -> None:
    """唯一写入入口。所有校验在 COMMIT 之前完成。"""
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    ev = finalize_envelope(evidence_row)          # 计算 sha256 / unix 时间 / pin 比对
    conn.execute("BEGIN IMMEDIATE")
    try:
        # ① 先写证据（父记录尚不存在 —— 依赖 DEFERRABLE INITIALLY DEFERRED）
        conn.execute(INSERT_EVIDENCE, ev)
        # ② 再写决策（FK 指向刚插入的证据，同样延迟到 COMMIT 校验）
        conn.execute(INSERT_DECISION, dict(decision_id=decision_id, symbol=symbol,
                                           provenance=claim,
                                           authoritative_evidence_id=ev.evidence_id
                                             if claim == "real_llm" else None))
        # ③ 事务内重算并断言（X-01 由触发器已保证，这里复核跨行一致性）
        ok, missing = evaluate_readiness(conn, decision_id, ev.evidence_id)
        if claim == "real_llm" and not ok:
            raise InconsistentClaim(missing)
        if claim != "real_llm" and ok:
            raise InconsistentClaim(["claim understated"])
        # ④ 链尾复核（TRG-4 已保证连续性，这里复核整链可读）
        verify_chain(conn, decision_id)
        # ⑤ 提交前唯一出口
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")                  # 只在事务内
        raise
```

**顺序推演（P0-2 验收）**：

| 步骤 | 若无 DEFERRABLE | 有 DEFERRABLE（本方案） |
|---|---|---|
| ① 插证据（父不存在） | `FOREIGN KEY constraint failed` | ✅ 通过（延迟） |
| ② 插决策（指向证据） | 父子均存在，通过 | ✅ 通过 |
| ③ 事务内校验 | — | ✅ 可读到未提交数据 |
| ⑤ COMMIT | — | ✅ 此刻校验两个 FK，均满足 |

**明确废止**：任何 "COMMIT 后重算 → ROLLBACK" 的写法。提交后只允许**只读**审计（`v_decision_verdict` 重算），发现异常只能标记 `integrity_violation` + 告警，**不得回滚**。

---

## 8. 失败与异常处理

| 情形 | 处理 | 结果 |
|---|---|---|
| ① 插入证据失败（CHECK/触发器） | ROLLBACK | 无任何行 |
| ② 插入决策失败（TRG-1a/FK） | ROLLBACK | 无任何行 |
| ③ 事务内断言失败 | ROLLBACK | 无任何行 |
| COMMIT 失败（磁盘/IO） | 驱动报错；不重试写半条 | 无任何行（SQLite 原子性） |
| 进程被 kill（COMMIT 前） | 下次打开自动回滚未提交事务 | 无任何行 |
| 进程被 kill（COMMIT 后） | 已提交，正常 | 完整一行 |
| 读时发现 claim≠evidence | 视图输出 `integrity_violation`；可选告警 | 数据不被改写 |

---

## 9. 状态机（S-1…S-7，互斥且完备）

**求值顺序固定，先中先得；每条均显式否定前序条件。**

| 序 | 状态 | 判定条件（含对前序的否定） |
|---|---|---|
| 1 | `unavailable` | 无凭据/无模型配置，**且未发起任何请求**（`http_status=0 且 envelope_bytes=0 且 upstream_model=''`） |
| 2 | `stub_transport` | 非 1，且运行模式为测试桩（`source_declared_by='operator'` 且 stub 模式标记为真） |
| 3 | `local_llm` | 非 1–2，且 `I-17` 全部满足（本地/私网真实模型，由运维声明） |
| 4 | `rejected` | 非 1–3，且 **已发起**，且满足任一：无响应（status=0）／`http_status≠200`／`upstream_model=''`（响应不可解析） |
| 5 | `real_llm` | 非 1–4，且 `E-01…E-20` ∧ `X-01…X-03` 全通过 |
| 6 | `real_llm_unverified` | 非 1–5，且 `http_status=200 ∧ upstream_model≠''` ∧ E/X 至少一项未通过 |
| 7 | `unknown` | 其余（含无法判定是否发起） |

**关键判定（对应 P1-7 的逐条要求）**：

| 输入 | 状态 | 说明 |
|---|---|---|
| HTTP 500 + `upstream_model` 非空 | **`rejected`** | 命中序 4（status≠200），**不会**落到 unverified ✅ |
| HTTP 200 + 缺 `upstream_id` | `real_llm_unverified` | 序 5 失败（E-15/16）、序 6 命中 |
| HTTP 200 + `upstream_model` 空 | `rejected` | 序 4「响应不可解析」 |
| 请求已发出但无响应（超时） | `rejected` | 序 4「无响应」 |
| DNS / TLS 握手失败 | `rejected` | 序 4「无响应」 |
| 本地模型（loopback, local） | `local_llm` | 序 3；永不 `real_llm` |
| 测试桩 | `stub_transport` | 序 2 |
| 回环代理且无法验证上游 | `real_llm_unverified` | 序 6（E-01/E-06 失败） |
| 代理状态不可观测 | `unknown` | `proxy_used='unknown'` → I-15 强制 T=unknown，序 7 |

> 三轴保持独立：`endpoint_class` 只描述目标地址类别，`transport_path` 只描述连接路径，`attempt_verdict` 只描述结论。`real_llm_unverified` **只出现在 verdict 轴**，不作为 `endpoint_class` 取值。

---

## 10. 历史数据迁移（不执行）

### 10.1 前置与顺序（P1-8 修复点）

| 步 | 动作 | 关键点 |
|---|---|---|
| 0 | 只读预检：`PRAGMA integrity_check`、`PRAGMA foreign_key_check`、行数、各表 `COUNT(DISTINCT)` | 任何异常即终止 |
| 1 | 备份 `us_loop.db` → `us_loop.db.bak-<ts>`；记录**前后** SHA256 | 备份仅用于**灾难恢复**，不作为回滚手段 |
| 2 | **在事务之外**、连接建立后立刻执行 `PRAGMA foreign_keys = OFF` | 该 PRAGMA 在事务内是 no-op（V3.2 I-1 的根因） |
| 3 | `BEGIN IMMEDIATE` | 全程单事务 |
| 4 | `DROP VIEW IF EXISTS v_decision_verdict; DROP VIEW IF EXISTS v_evidence_seq;` | 先删视图，避免 RENAME 改写引用 |
| 5 | `DROP TRIGGER IF EXISTS`（7 个） | 先删触发器，避免重建期间误拦 |
| 6 | `ALTER TABLE ai_decisions RENAME TO ai_decisions_old;`（证据表同理） | |
| 7 | 按 §5.1 建新表（新表名不带 `_old`） | |
| 8 | `INSERT INTO ... SELECT ...` 复制，并**逐行设置** `record_origin='migrated'`、`migration_batch='<ts>'` | 历史 `real_llm` 的 `provenance` **原样保留**（不改原值）；指针置 NULL；原值另抄 `migration_conflicts` |
| 9 | 建索引 → 建触发器 → 建视图（顺序：表 → 索引 → 触发器 → 视图） | 视图最后建，确保引用已存在 |
| 10 | `PRAGMA foreign_key_check;`（应 0 行） | 事务内可执行 |
| 11 | `PRAGMA integrity_check;`（应 `ok`） | |
| 12 | 行数与内容哈希比对（见 §10.2） | 不一致 → ROLLBACK |
| 13 | `COMMIT` | |
| 14 | `PRAGMA foreign_keys = ON;` 再跑一次 `foreign_key_check` | 每连接一次 |

### 10.2 历史行处理规则

| 情形 | 处理 |
|---|---|
| 历史 `provenance='real_llm'` 且无证据 | **保留原 claim**、`record_origin='migrated'`、指针 NULL；视图输出 `historical_transport_unverified`。原值另抄一份到 `migration_conflicts` 以便对照 |
| 历史有证据但链路不完整 | 同上（保留 claim、origin=migrated、指针可指向已有证据但不参与 readiness） |
| 多条证据 | 取 `seq` 最大且 `attempt_verdict='real_llm'` 者为 `authoritative_evidence_id`；否则 NULL |
| 缺 `evidence_id` | 允许 NULL（迁移行） |
| 旧字段 NULL | 新列用 DEFAULT；不强制回填 |
| 重复 `(decision_id, seq)` | 预检终止，需人工裁决 |
| FK 无法满足 | 复制顺序：先 decisions 后 evidence（FK 已 OFF），事后 `foreign_key_check` 兜底 |
| 迁移中断 | ROLLBACK；数据库回到迁移前状态（**备份不会被自动覆盖**） |

> **readiness 对迁移行一律 false**：`evaluate_readiness` 第一行即 `if record_origin='migrated': return False, ['migrated_record']`，不因证据齐全而追溯认证（D4）。

> **失败恢复不使用备份还原**：备份是灾难兜底。常规失败一律 `ROLLBACK`。若必须用备份，需人工确认"迁移开始后无新数据"，否则会覆盖迁移期间产生的记录 —— 该风险写入 §12 R-6。

### 10.3 迁移前后校验

| 校验 | 迁移前 | 迁移后 |
|---|---|---|
| 行数 | 每表 `COUNT(*)` 记录 | 必须相等（或按规则增减并说明） |
| 内容哈希 | 按 `decision_id` 排序的 `sha256(concat(字段))` 聚合 | 同法比对；**允许**的差异仅限 `record_origin`/`provenance` 改写项，逐条列在 `migration_conflicts` |
| FK | `foreign_key_check` | 必须 0 行 |
| 完整性 | `integrity_check` | 必须 `ok` |
| 视图 | 记录旧视图输出快照 | 新视图对迁移行必须全部输出 `historical_transport_unverified` |

---

## 11. TLS / 代理 / 网络 / 响应规则

### 11.1 TLS 九要素（逐项可审计）

| # | 要素 | 字段 | 强制 |
|---|---|---|---|
| 1 | 证书链验证 | `tls_verified` | E-19 |
| 2 | **hostname 验证**（与原始域名比，非与 IP） | `tls_hostname_verified` | E-19 |
| 3 | SPKI 是否配置 | `length(tls_pin_expected)=64` | E-20b（间接） |
| 4 | SPKI 是否匹配 | `tls_pin_expected = tls_spki_sha256` | **I-19 + E-20b（真正的比较）** |
| 5 | TCP peer 是否上游 | `peer_role='upstream'` + `peer_class` | I-05 |
| 6 | 是否经代理 | `proxy_used` / `proxy_class` | I-12…I-15 |
| 7 | 上游 response_id | `upstream_id` + `upstream_id_source='body.id'` | E-15…E-17 |
| 8 | HTTP 状态 | `http_status` | E-10 |
| 9 | usage 是否存在 | `usage_prompt_tokens` | E-18 |

**`tls_pin_status` 四态语义（不可互相冒充）**：

| 值 | 含义 | 能否 real_llm |
|---|---|---|
| `not_configured` | 未配置期望 pin | ❌（E-20a 要求 matched） |
| `not_checked` | 配置了但本次未校验 | ❌（E-20a） |
| `mismatched` | 校验过，不一致 | ❌（E-20a；且 I-19 允许不匹配存在但不得标 matched） |
| `matched` | 校验过且一致 | ✅（且 I-19 强制 `expected=actual`） |

> `matched` **不再由应用自由填写**：I-19 把"标 matched"与"两个值相等"绑定，写 `matched` 但不相等会被 CHECK 直接拒绝。

### 11.2 代理分类（8 场景，全字段给出）

| 场景 | proxy_used | proxy_class | peer_role | transport_path | 可否 real_llm |
|---|---|---|---|---|---|
| 无代理变量，直连 | `none` | `none` | upstream | `direct_verified` | ✅ |
| 有环境变量但本次直连 | `none` | `none` | upstream | `direct_verified` | ✅（`proxy_env_present=1` 仅记录） |
| 环境代理，本机回环 | `env:127.0.0.1:p` | `loopback` | proxy | `proxy_unverified` | ❌ |
| 显式代理，私网 | `explicit:10.x:p` | `private` | proxy | `proxy_unverified` | ❌ |
| 显式代理，公网 | `explicit:1.2.3.4:p` | `remote` | proxy | `proxy_unverified` | ❌ |
| 代理状态不可观测 | `unknown` | `unknown` | unknown | `unknown` | ❌（I-15） |
| 本地模型（无 TLS） | `none` | `none` | upstream | `local` | ❌ |
| 私网模型 | `none` | `none` | upstream | `private` | ❌ |

---

## 12. X-02 / X-03 的保证边界（不夸大）

| 项 | 数据库能保证 | 数据库不能保证 | 归属 |
|---|---|---|---|
| X-02 链**连续性** | `prev_hash` 必须等于前一条 `integrity_hash`（TRG-4） | 无法验证 `integrity_hash` 是否由正确算法算出（SQLite 无 sha256） | DB（连续性）+ 应用（正确性） |
| X-02 链**防篡改** | 阻止 UPDATE/DELETE（TRG-2/3） | 有写权限者可 `DROP TRIGGER` 后重写整链 | **残余 R-3** |
| X-03 时间窗 | `unix_created_at − unix_received_at ∈ [0,5]` | 无法证明 `unix_received_at` 是真实接收时刻 | DB（区间）+ 残余 R-7 |
| X-03 实现方式 | 用 INTEGER 秒，**不对 ISO 文本做减法** | — | DB |

> 若实现者改用 `created_at`/`received_at` 文本相减，将得到字符串/数值强转的垃圾结果 —— 本方案**明确禁止**，并提供 `unix_*` 列作为唯一计算来源。

---

## 13. JSON 元素校验（P1-4）

| 输入 | 结果 | 由谁拦截 |
|---|---|---|
| `NULL` | 拒绝 | `NOT NULL` |
| `""` | 拒绝 | `json_valid=0` |
| `{}` | 拒绝 | `json_type<>'array'` |
| `null` | 拒绝 | `json_type='null'` |
| `123` | 拒绝 | `json_type='integer'` |
| `"abc"` | 拒绝 | `json_type='text'` |
| `[]` / `[ ]` | 拒绝（当 `unverified`） | I-16 `json_array_length>0` |
| `[{"a":1}]` | 拒绝 | **TRG-5** 元素 `type<>'text'` |
| `[""]` / `["  "]` | 拒绝 | **TRG-5** `trim(value)=''` |
| `["made_up_reason"]` | 拒绝 | **TRG-5** 白名单 |
| `["missing_usage","proxy_path"]` | 接受 | 白名单内非空字符串 |
| 非法 JSON | 拒绝 | `json_valid=0`（`json_type` 会报错，语句中止） |

**覆盖路径**：INSERT 由 TRG-5 + 列 CHECK + I-16 三重覆盖；UPDATE 由 `trg_ev_no_update` 整体禁止（结构上不可能出现更新后的非法 JSON）。

**JSON1 缺失**：启动自检 `SELECT json_valid('[]')`；失败 → **拒绝启动**，记录 `sqlite_json_unavailable`。要求 SQLite ≥ 3.38（JSON 函数默认内置）。若 DDL 在无 JSON1 的环境执行，`CREATE TABLE` 会直接失败（fail closed，但需实测确认错误形态）。

---

## 14. 反例（42 条，全部为静态推演）

| # | 构造 | 预期 | 拦截层 |
|---|---|---|---|
| CE-01 | 三轴任一 NULL | 拒 | NOT NULL |
| CE-02 | 三轴空串 | 拒 | CHECK IN |
| CE-03 | 非法枚举 | 拒 | CHECK IN |
| CE-04 | `upstream_id=NULL` + real_llm | 拒 | E-15 |
| CE-05 | `upstream_id=''` + real_llm | 拒 | E-16 |
| CE-06 | `http_status=NULL` | 拒 | NOT NULL |
| CE-07 | `http_status=500` + real_llm | 拒 | E-10 |
| CE-08 | `evidence_id=NULL` | 拒 | `PRIMARY KEY NOT NULL`（显式，堵 SQLite 旧行为） |
| CE-09 | `proxy_used='env:…' + T='direct_verified'` | 拒 | I-12 |
| CE-10 | `E='remote' + peer_class='loopback' + global=1` | 拒 | I-05/I-08 |
| CE-11 | `proxy_class='none'` 但 `proxy_used='env:…'` | 拒 | I-11 |
| CE-12 | `proxy_used=''` | 拒 | 枚举 CHECK |
| CE-13 | `reasons='[]'` + unverified | 拒 | I-16 |
| CE-14 | `reasons='[ ]'` + unverified | 拒 | I-16 |
| CE-15 | 多字段同时 NULL | 拒 | NOT NULL |
| CE-16 | `tls_verified=0` + real_llm | 拒 | E-19 |
| CE-17 | `pin_status='not_checked'` + real_llm | 拒 | E-20a |
| CE-18 | `pin_status='not_configured'` + real_llm | 拒 | E-20a（V3.3 收紧点） |
| CE-19 | `pin='matched'` 但 `expected≠spki` | 拒 | **I-19**（真正的比较） |
| CE-20 | `pin='matched'` 但 `spki=''` | 拒 | I-19 长度 |
| CE-21 | `usage_prompt_tokens=0` + real_llm | 拒 | E-18 |
| CE-22 | `proxy_used='unknown'` + `T='direct_verified'` | 拒 | I-15 |
| CE-23 | `local_llm` + `source_declared_by='probe'` | 拒 | I-17 |
| CE-24 | `upstream_id` 非空 + `source=''` | 拒 | I-18 |
| CE-25 | `T='proxy_unverified'` + `peer_role='upstream'` | 拒 | I-04 |
| CE-26 | `proxy_used='env:…'` 但 `proxy_env_present=0` | 拒 | I-20 |
| CE-27 | `UPDATE ai_decisions SET provenance='real_llm'`（无证据） | 拒 | **TRG-1b**（P0-1 修复点） |
| CE-28 | `UPDATE ai_transport_evidence SET attempt_verdict='real_llm'` | 拒 | TRG-2（append-only） |
| CE-29 | `UPDATE ai_transport_evidence SET seq=99` | 拒 | TRG-2 |
| CE-30 | `DELETE FROM ai_transport_evidence WHERE …` | 拒 | TRG-3 |
| CE-31 | `DELETE FROM ai_decisions WHERE …` | 拒 | TRG-1c |
| CE-32 | `prev_hash` 与前一条不符 | 拒 | TRG-4 |
| CE-33 | `seq=2` 但 `seq=1` 不存在（跳号） | 拒 | TRG-4（`COALESCE(...,'~')` 制造不匹配） |
| CE-34 | `unix_created_at < unix_received_at` 或差值 >5 + real_llm | 拒 | X-03 CHECK |
| CE-35 | 哈希列填 64 个 `Z` | 拒 | `NOT GLOB '*[^0-9a-f]*'` |
| CE-36 | `record_origin='migrated'` + `provenance='real_llm'` + **指针非空** | 拒 | 决策表 CHECK（迁移行必须指针 NULL） |
| CE-37 | `migrated` 但 `migration_batch` 为空 | 拒 | 决策表 CHECK |
| CE-38 | `voided_at` 有值但 `void_reason` 空 | 拒 | 决策表 CHECK |
| CE-39 | `reasons='[{"a":1}]'`（INSERT） | 拒 | TRG-5 |
| CE-40 | `reasons='["made_up_reason"]'`（INSERT） | 拒 | TRG-5 |
| CE-41 | 指针指向不存在证据（绕过 FK 关闭时） | 视图输出 `integrity_violation` | 视图 NULL 分支 |
| CE-42 | live 行 `provenance='real_llm'` + 指针 NULL | 视图输出 `integrity_violation`（**不是 historical**） | 视图 origin 分支 |

> 共 42 条（超出要求的 30 条）。CE-08 依赖「TEXT PK 默认允许 NULL」这一 SQLite 旧行为，属**需隔离实测**项；本方案用显式 `NOT NULL` 消除该风险。

---

## 15. 正向场景（17 条）

| # | 场景 | 期望 verdict | 说明 |
|---|---|---|---|
| PS-01 | 直连 + 全 E + 全 X + pin matched | `real_llm` | 唯一可宣称真实的路径 |
| PS-02 | 同上但缺 `upstream_id` | `real_llm_unverified` | E-15 失败 |
| PS-03 | 同上但 HTTP 500（model 非空） | `rejected` | 序 4 先于序 6 |
| PS-04 | 同上但信封摘要缺失 | `real_llm_unverified` | E-12 |
| PS-05 | 同上但 usage=0 | `real_llm_unverified` | E-18 |
| PS-06 | 回环代理 | `real_llm_unverified` | 三轴齐全、peer_role=proxy |
| PS-07 | 本地模型（loopback/local） | `local_llm` | I-17 |
| PS-08 | 私网模型 | `local_llm` | I-17 |
| PS-09 | DNS 解析为空 | `unknown` | 序 7 |
| PS-10 | TLS 证书失败 | `rejected` | 序 4 |
| PS-11 | 测试桩 | `stub_transport` | 序 2 |
| PS-12 | 无凭据未发起 | `unavailable` | 序 1 |
| PS-13 | 历史迁移行（原 real_llm） | `historical_transport_unverified` | 视图 origin 分支 |
| PS-14 | 多证据：seq1=rejected, seq2=real_llm，指针指 seq2 | `real_llm` | 权威指针唯一 |
| PS-15 | 逻辑作废（voided_at 非空） | `voided` | 视图首分支 |
| PS-16 | 有环境代理但直连成功 | `real_llm`（若 pin matched） | `proxy_env_present=1` 不影响 |
| PS-17 | pin 未配置 | `real_llm_unverified` | E-20a 失败（fail closed） |

---

## 16. 风险清单

| ID | 级别 | 风险 | 处置 |
|---|---|---|---|
| RK-01 | P0 | 有 DB 写权限者可 `DROP TRIGGER`/`DROP CHECK` 后写假证据 | **无法在库内解决**，残余 R-3；缓解：外部锚定（每日链尾入独立日志）+ 文件级权限 |
| RK-02 | P0 | 应用可在内存里伪造 envelope 后调用写入 | 同上；`body.id` 与上游对账是唯一出路（R-1） |
| RK-03 | P1 | 触发器与 CHECK 在真实 SQLite 上的行为未实测 | §18 实测清单；未实测前不得宣称"已强制" |
| RK-04 | P1 | `DEFERRABLE` 需 `PRAGMA foreign_keys=ON` 且每连接设置 | §11 明确；漏设则 FK 完全不生效（需实测确认） |
| RK-05 | P1 | 迁移期间 `foreign_keys=OFF` 有窗口期 | 单事务 + 事后 `foreign_key_check`；R-6 |
| RK-06 | P2 | pin 强制导致无 pin 环境永不 real_llm | 有意 fail closed；运维须首次采集 SPKI |
| RK-07 | P2 | 证据 append-only 导致表无限增长 | 需保留策略（按年归档到冷库，不删） |
| RK-08 | P3 | `record_origin` 可由写方声明为 `migrated` | 只会降低可信度（→historical），无收益，不构成风险 |

---

## 17. SQLite 隔离实测清单（本轮未执行）

| # | 待测 | 为何必须实测 |
|---|---|---|
| 1 | TEXT PRIMARY KEY 是否允许 NULL（CE-08） | 旧行为与显式 NOT NULL 的交互 |
| 2 | `PRAGMA foreign_keys` 在事务内是否 no-op | 决定迁移步骤 2 的位置是否正确 |
| 3 | 循环 FK + `DEFERRABLE INITIALLY DEFERRED` 能否建表并通过事务 | P0-2 的核心假设 |
| 4 | `json_type()` 对非法 JSON 是报错还是返回 NULL；报错是否中止语句 | 决定 CE 中"非法 JSON"的拦截层 |
| 5 | 触发器 `WHEN ... NOT EXISTS(SELECT ...)` 在插入顺序下的可见性 | TRG-1 是否真能看到未提交的证据行 |
| 6 | 多个 `BEFORE UPDATE` 触发器的执行顺序 | 本方案只有一个，仍需确认 abort 语义 |
| 7 | `DROP TABLE/RENAME` 对视图/触发器引用的改写行为 | 迁移步骤 4–6 |
| 8 | `foreign_key_check` 在事务内是否可见未提交数据 | 迁移步骤 10 |
| 9 | `NOT GLOB '*[^0-9a-f]*'` 对空串与 64 位 hex 的行为 | 哈希校验 |
| 10 | `COALESCE(...,'~')` 制造不匹配的技巧是否可靠 | TRG-4 跳号检测 |

> 以上 10 项**全部未验证**。任何"数据库强制"的表述，在实测前只能理解为"按 SQLite 文档语义应当如此"。

---

## 18. 残余风险与不可证明事项

| # | 事项 | 为什么不可证明 |
|---|---|---|
| R-1 | `upstream_id` 的**值**是否真来自上游 | 值可被伪造；只能靠单一写入路径 + 上游对账 |
| R-2 | `integrity_hash` 是否按正确算法计算 | SQLite 无 sha256，DB 只能校验链**连续性** |
| R-3 | 有写权限者可整体重写 | 触发器可被删除；需外部锚定 |
| R-4 | `local_llm` 是否真是模型 | 仅运维声明（`source_declared_by='operator'`） |
| R-5 | 代理路径下上游身份 | peer 只能是代理，设计上限即 `proxy_unverified` |
| R-6 | 迁移用备份还原会覆盖新数据 | 故备份不作为回滚手段 |
| R-7 | `unix_received_at` 是否为真实接收时刻 | 由应用写入，无法外部验证 |
| R-8 | 模型名非空 ≠ 真推理 | E-11 是必要条件，不是充分条件 |

---

## 19. 自检表（**不能代替独立审计**）

| # | 检查 | 结果 |
|---|---|---|
| C-01 | P0-1：INSERT 与 UPDATE 是否各有一个独立触发器 | ✅ TRG-1a / TRG-1b |
| C-02 | P0-2：FK 是否 DEFERRABLE，顺序是否推演 | ✅ §5.1 + §7 顺序表 |
| C-03 | P1-1：`ai_decisions` 是否有完整 CREATE TABLE | ✅ §5.1 |
| C-04 | P1-1：指针是否真 FK（非索引冒充） | ✅ 表内 `FOREIGN KEY` |
| C-05 | P1-2：pin 是否真比较 | ✅ I-19 + E-20b |
| C-06 | P1-3：是否区分 DB/应用保证 | ✅ §12 |
| C-07 | P1-3：是否避免 ISO 文本减法 | ✅ `unix_*` |
| C-08 | P1-4：TRG-4 是否已删除或真定义 | ✅ 定义为 TRG-5（JSON），旧名废弃 |
| C-09 | P1-4：INSERT/UPDATE 是否都覆盖 | ✅ INSERT 由 TRG-5；UPDATE 被 TRG-2 整体禁止 |
| C-10 | P1-5：是否用显式 origin 而非 NULL 推断 | ✅ `record_origin` |
| C-11 | P1-6：视图是否显式处理 NULL | ✅ 逐分支 `IS NULL` |
| C-12 | P1-7：500+model 是否 rejected | ✅ 序 4 |
| C-13 | P1-8：PRAGMA 是否在事务外 | ✅ §10 步骤 2 |
| C-14 | 哈希字段是否校验 hex | ✅ `NOT GLOB '*[^0-9a-f]*'` |
| C-15 | 是否还有 `CHECK (...)` 占位 | ✅ 无 |
| C-16 | 反例数 | ✅ 42（要求 ≥30） |
| C-17 | 正向场景数 | ✅ 17（要求 ≥15） |
| C-18 | 是否宣称已验证 | ✅ 全文标注"静态推演 / 需实测" |
| C-19 | 是否修改既有文件 | ✅ 仅新建本文件 |

---

## 20. 与 V3.2 的差异一览

| 项 | V3.2 | V3.3 |
|---|---|---|
| 决策表 DDL | 缺失 | 完整，含 `decision_id TEXT PRIMARY KEY NOT NULL` |
| UPDATE 保护 | 声称 TRG-1 覆盖，实际只有 INSERT | TRG-1a + TRG-1b 两个独立触发器 |
| FK | 立即约束，顺序冲突 | 双向 `DEFERRABLE INITIALLY DEFERRED` |
| 指针 FK | 仅 UNIQUE INDEX | 真 `FOREIGN KEY` + `UNIQUE` |
| pin | 无比较 | I-19 强制 `expected=actual`；E-20 要求 matched |
| 时间 | ISO 文本 | `unix_*` 整数 + CHECK |
| 链 | 仅应用层 | TRG-4 强制连续性（正确性仍属应用） |
| JSON 元素 | 引用不存在的 TRG-4 | TRG-5 真正定义 |
| 历史判定 | 指针 NULL 推断 | `record_origin` 显式字段 |
| 视图 NULL | 依赖 `<>` 三值 | 逐分支显式 |
| 删除 | CASCADE 与触发器冲突 | 全面禁止物理删除 + RESTRICT |
| 哈希校验 | 仅长度 | 长度 + hex |
| 状态机 | 500 被判 unverified | `rejected` 优先 |

---

## 21. 交付状态

```
V3.3 设计文件已生成，待独立审计；
方案未获批；
未获实施授权；
未执行任何 SQL；
未创建数据库；
S0 未启动。
```

本文件的自检表（§19）**不构成审计结论**，也不代表设计通过。

---

> 本文件 SHA256（本行之前全部字节，可用 `head -n -1 <本文件> | sha256sum` 复算）：`e5901301dfc9e277dabf0358af5f761965032636a3ee7e19177bd5224dfb2e2b`
