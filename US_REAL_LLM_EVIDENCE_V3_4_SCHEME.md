# V3.4 修订方案（设计稿 · 不实施）

| 项 | 值 |
|---|---|
| 版本 | **v3.4**（2026-11-11 14:00） |
| 取代 | `US_REAL_LLM_EVIDENCE_V3_3_SCHEME.md`（`1c8ca415…c494a`，**不覆盖、不修改**） |
| 依据 | V3.3 第四轮独立审计（FAIL：1×P0 / 4×P1 / 9×P2 / 4×P3）；V3.2、V3.1 审计；V2 逻辑审计；原修复计划 |
| 性质 | 设计文档。未执行 SQL、未建库、未改源码/配置/测试/数据 |
| 状态 | **V3.4 待独立审计**；方案未获批；未获实施授权；S0 未启动 |

> 全文 SQL 为静态文本，未经任何 SQLite 实例执行。标注「需实测」的结论不得视为已验证。

---

## 1. 基线与版本

| 文件 | 字节 | 行 | SHA256 | 判定 |
|---|---|---|---|---|
| `US_REAL_LLM_EVIDENCE_V3_3_SCHEME.md` | 53,333 | 948 | `1c8ca415823a1550d9f9beb56a26fd1a1727b58afc881aad1ddfdb35f61c494a` | ✅ 与要求一致 |
| `US_REAL_LLM_EVIDENCE_V3_2_SCHEME.md` | 27,271 | 441 | `ef743a86…bf76a` | 未变 |
| `US_REAL_LLM_EVIDENCE_V3_INDEPENDENT_AUDIT.md` | 19,907 | 311 | `138ed16b…798cf` | 未变 |
| `US_REAL_LLM_EVIDENCE_V2_LOGIC_AUDIT.md` | 22,412 | 313 | `9e21e56f…d8ab8` | 未变 |
| `US_REAL_LLM_EVIDENCE_REMEDIATION_PLAN.md` | 27,586 | 534 | `3d569f16…610a` | 未变 |

目标文件创建前不存在；本轮仅新增本文件。

V3.3 第四轮独立审计报告**本机无正式文件**（`ls` 未见），其内容取自本轮对话中提供的审计文本；本文引用其编号（P0-1、P1-1…P1-4、P2-1…P2-9、P3-1…P3-4）时以该文本为准，并已在 §2 逐条复述问题本身，不依赖外部文件存在。

---

## 2. V3.1 / V3.2 / V3.3 遗留问题追踪

| 来源 | 编号 | 问题 | V3.4 处置 | 章节 | 状态 |
|---|---|---|---|---|---|
| V3.3 | P0-1 | `INSERT OR REPLACE` 绕过 append-only | 新增 **seal 表**（(decision_id,seq) 唯一 + evidence_id 唯一）+ BEFORE INSERT 守卫；全路径推演 | §4/§6/§9 | **已解决（附残余 R-9）** |
| V3.3 | P1-1 | migrated 可带非空指针；CE-36 不成立 | 独立 CHECK：`migrated ⇒ pointer IS NULL` | §5.1 | **已解决** |
| V3.3 | P1-2 | 迁移缺 DROP 旧表；RENAME 改写 FK | 改为 `_new` 建表 → 复制 → 校验 → **DROP 旧表** → RENAME | §12 | **已解决** |
| V3.3 | P1-3 | 失败后 `foreign_keys` 未复位 | `finally` 强制复位 + 复位失败即关闭连接 | §13 | **已解决** |
| V3.3 | P1-4 | pin 字段相等被当作"已验证" | 新增 `pin_verification_status` / `pin_verification_evidence`；`real_llm` 要求 `performed_match` | §11 | **已解决（附残余 R-10）** |
| V3.3 | P2-1 | 代理未知归类矛盾 | S-8 显式判 `proxy_used='unknown' → unknown`，置于 unverified 之前 | §10 | **已解决** |
| V3.3 | P2-2 | DNS 失败归类冲突 | 定义"已发起"= 产生 socket 级动作；DNS 失败 → `rejected` | §10 | **已解决** |
| V3.3 | P2-3 | `peer_ip` 与 class/role 不一致 | 明确 `peer_ip` 为**观测值**、不参与约束；一致性由应用保证（标注归属） | §14 | **部分（应用层）** |
| V3.3 | P2-4 | `voided_at` 与 provenance 关系 | 明确：作废行**保留原 claim**（历史事实），视图优先输出 `voided`；不加 CHECK | §7 | **已解决** |
| V3.3 | P2-5 | 迁移删触发器窗口未列风险 | 列 RK-09；缩短窗口 + 单事务 + 连接独占说明 | §12/§18 | **已解决** |
| V3.3 | P2-6 | `upstream_id_source` 自报 | 拆为 `upstream_id_source`（声明）+ 明确不可自证，入 R-1 | §11 | **残余** |
| V3.3 | P2-7 | SNI / CONNECT 边界丢失 | 恢复 §11.3 CONNECT 表 | §11.3 | **已解决** |
| V3.3 | P2-8 | "迁移行必须全部输出 historical" 错误 | 改为"仅 claim=real_llm 的迁移行输出 historical；其余原样" | §12.4 | **已解决** |
| V3.3 | P2-9 | readiness 排除 migrated 的归属未标 | §14 明确列为应用层 | §14 | **已解决** |
| V3.3 | P3-1 | TRG-4/TRG-5 命名混淆 | 全部触发器改为语义名（`trg_ev_chain_ins` 等），不再用 TRG 编号 | §6 | **已解决** |
| V3.3 | P3-2 | §2 落点引用错 | 本表重写 | §2 | **已解决** |
| V3.3 | P3-3 | CE-42 拦截层标错 | 更正为 INSERT CHECK | §15 | **已解决** |
| V3.3 | P3-4 | 缺旧→新编号映射 | 补 §20 完整映射 | §20 | **已解决** |
| V3.2 | P0-1/P0-2 等 | 见 V3.3 §2 | 沿用并加固 | 全文 | 保持 |
| V3.1 | N-1…N-13 | 见 V3.3 §2 | 沿用 | 全文 | 保持 |

---

## 3. 三轴模型（不变，重申定义）

| 轴 | 取值 | 语义 |
|---|---|---|
| `endpoint_class` | `remote`/`loopback`/`private`/`unknown` | 请求**目标**地址类别 |
| `transport_path` | `direct_verified`/`proxy_unverified`/`local`/`private`/`unknown` | 连接路径 + 对端验证程度 |
| `verdict`（决策 `provenance` / 证据 `attempt_verdict`） | `real_llm`/`real_llm_unverified`/`local_llm`/`stub_transport`/`rejected`/`unavailable`/`unknown` | 结论 |

外加世代轴 `record_origin ∈ {live, migrated}`（历史身份显式化）与**证据完整性辅助轴** `pin_verification_status`（§11）。

> `real_llm_unverified` 只出现在 verdict 轴，绝不作为 `endpoint_class`。

---

## 4. 表结构总览（3 张表）

| 表 | 作用 | 可变性 |
|---|---|---|
| `ai_decisions` | 决策声明（claim） | 仅允许 `voided_at/void_reason` 更新；禁止删除 |
| `ai_transport_evidence` | 传输证据 | **完全 append-only**（禁 UPDATE / 禁 DELETE / 禁 REPLACE） |
| `ai_evidence_seal` | 证据封印（防 REPLACE 复用键） | 仅允许 INSERT；禁 UPDATE / 禁 DELETE |

---

## 5. 完整 DDL

### 5.1 表

```sql
-- 连接级：必须在事务外、每连接执行
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- =====================================================================
-- 表 1：ai_decisions
-- =====================================================================
CREATE TABLE ai_decisions (
    decision_id      TEXT PRIMARY KEY NOT NULL
                     CHECK (length(decision_id) > 0
                        AND decision_id NOT GLOB '*[^0-9a-zA-Z_.:-]*'),
    run_id           TEXT,
    symbol           TEXT NOT NULL CHECK (length(symbol) > 0),
    decided_at       TEXT NOT NULL CHECK (length(decided_at) > 0),

    provenance       TEXT NOT NULL CHECK (provenance IN
                       ('real_llm','real_llm_unverified','local_llm',
                        'stub_transport','rejected','unavailable','unknown')),

    authoritative_evidence_id TEXT UNIQUE,

    record_origin    TEXT NOT NULL DEFAULT 'live'
                     CHECK (record_origin IN ('live','migrated')),
    migration_batch  TEXT,

    voided_at        TEXT,
    void_reason      TEXT,

    created_at       TEXT NOT NULL CHECK (length(created_at) > 0),

    -- M-01：迁移行必须带批次号
    CHECK (record_origin <> 'migrated' OR length(migration_batch) > 0),

    -- M-02（P1-1 修复）：迁移行不得持有权威证据指针。
    --        独立 CHECK，不与任何析取合并，INSERT/UPDATE 同样生效。
    CHECK (record_origin <> 'migrated' OR authoritative_evidence_id IS NULL),

    -- M-03：作废必须成对
    CHECK ((voided_at IS NULL AND void_reason IS NULL)
        OR (voided_at IS NOT NULL AND length(void_reason) > 0)),

    -- M-04：live 行声明 real_llm 必须带指针且未作废
    CHECK (provenance <> 'real_llm'
        OR record_origin = 'migrated'
        OR (authoritative_evidence_id IS NOT NULL AND voided_at IS NULL)),

    FOREIGN KEY (authoritative_evidence_id)
        REFERENCES ai_transport_evidence(evidence_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT
        DEFERRABLE INITIALLY DEFERRED
);

-- =====================================================================
-- 表 2：ai_transport_evidence（append-only）
-- =====================================================================
CREATE TABLE ai_transport_evidence (
    evidence_id      TEXT PRIMARY KEY NOT NULL
                     CHECK (length(evidence_id) = 64
                        AND evidence_id NOT GLOB '*[^0-9a-f]*'),
    decision_id      TEXT NOT NULL CHECK (length(decision_id) > 0),
    seq              INTEGER NOT NULL CHECK (seq >= 1),
    run_id           TEXT,
    symbol           TEXT NOT NULL CHECK (length(symbol) > 0),

    endpoint_class   TEXT NOT NULL CHECK (endpoint_class IN
                       ('remote','loopback','private','unknown')),
    transport_path   TEXT NOT NULL CHECK (transport_path IN
                       ('direct_verified','proxy_unverified','local','private','unknown')),
    attempt_verdict  TEXT NOT NULL CHECK (attempt_verdict IN
                       ('real_llm','real_llm_unverified','local_llm',
                        'stub_transport','rejected','unavailable','unknown')),

    peer_role        TEXT NOT NULL CHECK (peer_role IN ('upstream','proxy','unknown')),
    peer_class       TEXT NOT NULL CHECK (peer_class IN
                       ('remote','loopback','private','unknown')),
    peer_ip          TEXT,                       -- 观测值，不参与约束
    peer_ip_global   INTEGER NOT NULL DEFAULT 0 CHECK (peer_ip_global IN (0,1)),

    proxy_used       TEXT NOT NULL DEFAULT 'none'
                     CHECK (proxy_used IN ('none','unknown')
                         OR proxy_used LIKE 'env:%'
                         OR proxy_used LIKE 'explicit:%'),
    proxy_class      TEXT NOT NULL DEFAULT 'none' CHECK (proxy_class IN
                       ('none','loopback','private','remote','unknown')),
    proxy_env_present INTEGER NOT NULL DEFAULT 0 CHECK (proxy_env_present IN (0,1)),

    http_method      TEXT NOT NULL CHECK (http_method = 'POST'),
    request_path     TEXT NOT NULL CHECK (length(request_path) > 0),
    http_status      INTEGER NOT NULL CHECK (http_status BETWEEN 0 AND 599),
    redirect_count   INTEGER NOT NULL DEFAULT 0 CHECK (redirect_count >= 0),

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

    -- ---- TLS 观察层 ----
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

    -- ---- TLS 执行证明层（P1-4）----
    pin_verification_status TEXT NOT NULL DEFAULT 'not_performed'
                     CHECK (pin_verification_status IN
                        ('performed_match','performed_mismatch','not_performed','unsupported')),
    pin_verification_evidence TEXT NOT NULL DEFAULT ''
                     CHECK (length(pin_verification_evidence) IN (0,64)
                        AND pin_verification_evidence NOT GLOB '*[^0-9a-f]*'),

    -- ---- 时间 ----
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

    UNIQUE (decision_id, seq),
    FOREIGN KEY (decision_id) REFERENCES ai_decisions(decision_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT
        DEFERRABLE INITIALLY DEFERRED,

    -- ================= E 族：real_llm 的 21 项必要条件 =================
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
        AND pin_verification_status = 'performed_match'   -- E-21a
        AND length(pin_verification_evidence) = 64        -- E-21b
        AND unix_created_at        >= unix_received_at    -- X-03a
        AND unix_created_at - unix_received_at <= 5       -- X-03b
    )),

    -- ================= I 族：24 条一致性 =================
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
    CHECK (proxy_used NOT LIKE 'env:%' OR proxy_env_present = 1),                    -- I-20
    CHECK (pin_verification_status <> 'performed_match'
        OR (tls_pin_status = 'matched'
        AND tls_pin_expected = tls_spki_sha256
        AND length(pin_verification_evidence) = 64)),                                -- I-21
    CHECK (pin_verification_status <> 'performed_mismatch'
        OR tls_pin_status <> 'matched'),                                             -- I-22
    CHECK (pin_verification_status <> 'not_performed'
        OR tls_pin_status IN ('not_checked','not_configured')),                      -- I-23
    CHECK (attempt_verdict <> 'real_llm'
        OR pin_verification_status = 'performed_match')                              -- I-24
);

-- =====================================================================
-- 表 3：ai_evidence_seal（防 REPLACE 复用键；append-only）
-- =====================================================================
CREATE TABLE ai_evidence_seal (
    decision_id  TEXT NOT NULL CHECK (length(decision_id) > 0),
    seq          INTEGER NOT NULL CHECK (seq >= 1),
    evidence_id  TEXT NOT NULL CHECK (length(evidence_id) = 64
                   AND evidence_id NOT GLOB '*[^0-9a-f]*'),
    sealed_at    TEXT NOT NULL CHECK (length(sealed_at) > 0),
    seal_hash    TEXT NOT NULL CHECK (length(seal_hash) = 64
                   AND seal_hash NOT GLOB '*[^0-9a-f]*'),
    prev_seal    TEXT NOT NULL DEFAULT ''
                 CHECK (prev_seal = '' OR (length(prev_seal) = 64
                    AND prev_seal NOT GLOB '*[^0-9a-f]*')),
    PRIMARY KEY (decision_id, seq),
    UNIQUE (evidence_id)
);

CREATE INDEX ix_ev_decision ON ai_transport_evidence(decision_id, seq);
CREATE INDEX ix_dec_origin   ON ai_decisions(record_origin, provenance);
CREATE INDEX ix_seal_ev      ON ai_evidence_seal(evidence_id);
```

### 5.2 约束汇总（PK / FK / UNIQUE / NOT NULL）

| 表 | PK | UNIQUE | FK | 不可变字段 |
|---|---|---|---|---|
| `ai_decisions` | `decision_id` | `authoritative_evidence_id`（多 NULL 允许） | `authoritative_evidence_id → evidence(evidence_id)` DEFERRED | 除 `voided_at/void_reason` 外全部（由触发器保护） |
| `ai_transport_evidence` | `evidence_id` | `(decision_id, seq)` | `decision_id → decisions(decision_id)` DEFERRED | 全部 |
| `ai_evidence_seal` | `(decision_id, seq)` | `evidence_id` | 无（软引用） | 全部 |

---

## 6. 触发器（共 10 个，完整 SQL）

```sql
-- =====================================================================
-- 1) 决策 INSERT：声明 real_llm 必须有匹配的权威证据
-- =====================================================================
CREATE TRIGGER trg_dec_claim_ins
BEFORE INSERT ON ai_decisions
WHEN NEW.provenance = 'real_llm'
 AND NOT EXISTS (
       SELECT 1 FROM ai_transport_evidence e
        WHERE e.evidence_id     = NEW.authoritative_evidence_id
          AND e.decision_id     = NEW.decision_id
          AND e.attempt_verdict = 'real_llm')
BEGIN
  SELECT RAISE(ABORT, 'real_llm claim without matching authoritative evidence');
END;

-- =====================================================================
-- 2) 决策 UPDATE：与 INSERT 同构（P0-1 of V3.2）
-- =====================================================================
CREATE TRIGGER trg_dec_claim_upd
BEFORE UPDATE ON ai_decisions
WHEN NEW.provenance = 'real_llm'
 AND NOT EXISTS (
       SELECT 1 FROM ai_transport_evidence e
        WHERE e.evidence_id     = NEW.authoritative_evidence_id
          AND e.decision_id     = NEW.decision_id
          AND e.attempt_verdict = 'real_llm')
BEGIN
  SELECT RAISE(ABORT, 'real_llm claim without matching authoritative evidence (update)');
END;

-- =====================================================================
-- 3) 决策不可删除
-- =====================================================================
CREATE TRIGGER trg_dec_no_delete
BEFORE DELETE ON ai_decisions
BEGIN
  SELECT RAISE(ABORT, 'decisions are append-only; use voided_at instead');
END;

-- =====================================================================
-- 4) 证据 INSERT 守卫（P0-1 核心）：封印存在 → 拒绝任何再次插入
--    同时覆盖：同 (decision_id,seq)、同 evidence_id、REPLACE 后的重插
-- =====================================================================
CREATE TRIGGER trg_ev_seal_guard_ins
BEFORE INSERT ON ai_transport_evidence
WHEN EXISTS (SELECT 1 FROM ai_evidence_seal s
              WHERE (s.decision_id = NEW.decision_id AND s.seq = NEW.seq)
                 OR s.evidence_id = NEW.evidence_id)
BEGIN
  SELECT RAISE(ABORT, 'evidence slot already sealed; append with a new seq');
END;

-- =====================================================================
-- 5) 证据 AFTER INSERT：写入封印（失败则整条语句回滚）
-- =====================================================================
CREATE TRIGGER trg_ev_seal_after_ins
AFTER INSERT ON ai_transport_evidence
BEGIN
  INSERT INTO ai_evidence_seal(decision_id, seq, evidence_id, sealed_at,
                               seal_hash, prev_seal)
  VALUES (NEW.decision_id, NEW.seq, NEW.evidence_id, NEW.created_at,
          NEW.integrity_hash,
          COALESCE((SELECT s.seal_hash FROM ai_evidence_seal s
                     WHERE s.decision_id = NEW.decision_id
                       AND s.seq = NEW.seq - 1), ''));
END;

-- =====================================================================
-- 6) 证据不可 UPDATE
-- =====================================================================
CREATE TRIGGER trg_ev_no_update
BEFORE UPDATE ON ai_transport_evidence
BEGIN
  SELECT RAISE(ABORT, 'evidence is append-only; insert a new seq');
END;

-- =====================================================================
-- 7) 证据不可 DELETE
-- =====================================================================
CREATE TRIGGER trg_ev_no_delete
BEFORE DELETE ON ai_transport_evidence
BEGIN
  SELECT RAISE(ABORT, 'evidence is append-only; deletion forbidden');
END;

-- =====================================================================
-- 8) 链连续性
-- =====================================================================
CREATE TRIGGER trg_ev_chain_ins
BEFORE INSERT ON ai_transport_evidence
WHEN (NEW.seq = 1 AND NEW.prev_hash <> '')
  OR (NEW.seq > 1 AND NEW.prev_hash <> COALESCE((
        SELECT p.integrity_hash FROM ai_transport_evidence p
         WHERE p.decision_id = NEW.decision_id AND p.seq = NEW.seq - 1), '~'))
BEGIN
  SELECT RAISE(ABORT, 'integrity chain broken or seq gap');
END;

-- =====================================================================
-- 9) JSON 元素白名单（INSERT 路径）
-- =====================================================================
CREATE TRIGGER trg_ev_json_ins
BEFORE INSERT ON ai_transport_evidence
WHEN EXISTS (
      SELECT 1 FROM json_each(NEW.unverified_reasons) j
       WHERE j.type <> 'text'
          OR trim(j.value) = ''
          OR j.value NOT IN (
               'missing_upstream_id','missing_usage','envelope_digest_failed',
               'proxy_path','status_not_200','tls_verify_failed','tls_pin_mismatch',
               'pin_not_performed','model_mismatch','transport_unverified',
               'response_unparsable','other'))
BEGIN
  SELECT RAISE(ABORT, 'invalid unverified_reasons element');
END;

-- =====================================================================
-- 10) 封印表自身：禁 UPDATE / 禁 DELETE / 禁重复键插入
-- =====================================================================
CREATE TRIGGER trg_seal_no_update
BEFORE UPDATE ON ai_evidence_seal
BEGIN
  SELECT RAISE(ABORT, 'seal is append-only');
END;

CREATE TRIGGER trg_seal_no_delete
BEFORE DELETE ON ai_evidence_seal
BEGIN
  SELECT RAISE(ABORT, 'seal is append-only; deletion forbidden');
END;

CREATE TRIGGER trg_seal_guard_ins
BEFORE INSERT ON ai_evidence_seal
WHEN EXISTS (SELECT 1 FROM ai_evidence_seal s
              WHERE (s.decision_id = NEW.decision_id AND s.seq = NEW.seq)
                 OR s.evidence_id = NEW.evidence_id)
BEGIN
  SELECT RAISE(ABORT, 'seal slot already occupied');
END;
```

> 触发器总数：决策 3 个 + 证据 6 个 + 封印 3 个 = **12 个**（`trg_ev_seal_after_ins` 属证据表）。

---

## 7. 视图

```sql
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
        WHEN d.voided_at IS NOT NULL                            THEN 'voided'
        WHEN d.provenance <> 'real_llm'                         THEN d.provenance
        WHEN d.record_origin = 'migrated'                       THEN 'historical_transport_unverified'
        WHEN d.authoritative_evidence_id IS NULL                THEN 'integrity_violation'
        WHEN e.evidence_id IS NULL                              THEN 'integrity_violation'
        WHEN e.decision_id IS NULL
          OR e.decision_id <> d.decision_id                     THEN 'integrity_violation'
        WHEN e.attempt_verdict IS NULL                          THEN 'integrity_violation'
        WHEN e.attempt_verdict <> 'real_llm'                    THEN 'integrity_violation'
        ELSE 'real_llm'
    END                     AS effective_verdict
FROM ai_decisions d
LEFT JOIN ai_transport_evidence e
       ON e.evidence_id = d.authoritative_evidence_id;

CREATE VIEW v_evidence_seq AS
SELECT decision_id, seq, evidence_id, attempt_verdict,
       endpoint_class, transport_path, http_status,
       pin_verification_status, received_at, integrity_hash, prev_hash
  FROM ai_transport_evidence;

-- 封印与证据的对照（完整性审计）
CREATE VIEW v_seal_check AS
SELECT s.decision_id, s.seq, s.evidence_id, s.seal_hash,
       e.evidence_id  AS live_evidence_id,
       e.integrity_hash AS live_integrity_hash,
       CASE
         WHEN e.evidence_id IS NULL                       THEN 'missing_evidence'
         WHEN e.evidence_id <> s.evidence_id               THEN 'id_mismatch'
         WHEN e.integrity_hash <> s.seal_hash              THEN 'hash_mismatch'
         ELSE 'ok'
       END AS seal_status
  FROM ai_evidence_seal s
  LEFT JOIN ai_transport_evidence e
         ON e.decision_id = s.decision_id AND e.seq = s.seq;
```

**视图语义（含 P2-4、P2-8 修正）**：

| 情形 | effective_verdict |
|---|---|
| 作废 | `voided`（claim 原值仍保留在 `claimed_verdict`，作为历史事实） |
| claim ≠ real_llm | 原 claim（含 `rejected`/`local_llm`/`stub_transport`/`unavailable`/`unknown`/`real_llm_unverified`） |
| migrated + real_llm | `historical_transport_unverified` |
| live + 指针 NULL | `integrity_violation` |
| LEFT JOIN 无匹配 | `integrity_violation` |
| 证据 decision_id 不符 | `integrity_violation` |
| verdict NULL | `integrity_violation` |
| verdict ≠ real_llm | `integrity_violation` |

> migrated 且 claim 非 real_llm 的行**原样输出其 claim**，不会被统一改写成 historical（V3.3 P2-8 的修正）。

---

## 8. 事务写入顺序（唯一合法）

```python
def record_attempt(conn, decision_id, symbol, claim, evidence_row) -> None:
    assert_fk_on(conn)                      # 见 §13
    ev = finalize_envelope(evidence_row)    # sha256 / unix / pin 比对结果

    conn.execute("BEGIN IMMEDIATE")
    try:
        # ① 证据（父不存在 → 依赖 DEFERRABLE INITIALLY DEFERRED）
        conn.execute(INSERT_EVIDENCE, ev)
        # ② 决策（FK 指向刚插入的证据；AFTER INSERT 已写入封印）
        conn.execute(INSERT_DECISION, dict(
            decision_id=decision_id, symbol=symbol, provenance=claim,
            authoritative_evidence_id=(ev.evidence_id if claim == "real_llm" else None)))
        # ③ 事务内复核（提交前）
        ok, missing = evaluate_readiness(conn, decision_id, ev.evidence_id)
        if claim == "real_llm" and not ok:
            raise InconsistentClaim(missing)
        if claim != "real_llm" and ok:
            raise InconsistentClaim(["claim understated"])
        verify_chain(conn, decision_id)
        verify_seal(conn, decision_id)
        conn.execute("COMMIT")              # ⑥ 唯一提交点
    except BaseException:
        conn.execute("ROLLBACK")            # 只在事务内有效
        raise
    # ⑦ 提交后：只读审计，禁止回滚
```

**顺序约束**：`BEGIN` → 证据 → 决策 → 校验 → `COMMIT`。任何一步失败 → `ROLLBACK`，不产生残留行。

---

## 9. 全路径写入分析（P0 核心）

### 9.1 SQLite REPLACE 语义（前提事实）

- `INSERT OR REPLACE` 在遇到约束冲突时，**先删除冲突行，再插入新行**。
- 这次删除**不触发 `BEFORE DELETE` 触发器**，除非连接开启了 `recursive_triggers`。
- `BEFORE INSERT` 触发器**会**触发。
- `RAISE(ABORT)` 会**回退当前语句已做的全部改动**（包括 REPLACE 内部的删除）。
- 以上为 SQLite 文档语义，**仍需隔离实测**（§17-1、§17-2）。

### 9.2 逐路径推演

| 路径 | 触发顺序 | 是否被拦 | 拦截点 | 结果 |
|---|---|---|---|---|
| `INSERT`（新 seq） | BEFORE INSERT(guard/chain/json) → AFTER INSERT(seal) | 正常放行 | — | ✅ 成功，写入封印 |
| `INSERT`（已存在的 seq 或 id） | guard 先于约束检查 | **拦** | `trg_ev_seal_guard_ins` | ABORT；无变更 |
| `INSERT OR REPLACE`（同 id） | 删除旧行（无 DELETE 触发） → BEFORE INSERT(guard) 发现封印存在 → ABORT | **拦** | guard | ABORT；语句回退，旧行恢复（需实测 §17-2） |
| `INSERT OR REPLACE`（同 seq、新 id） | 同上（guard 同时看 seq 与 id） | **拦** | guard | 同上 |
| `INSERT OR IGNORE` | 冲突 → 忽略，不插入 | 无变更 | 约束 | ✅ 无副作用 |
| `INSERT ... ON CONFLICT DO UPDATE`（UPSERT） | 冲突 → 走 UPDATE 分支 → BEFORE UPDATE | **拦** | `trg_ev_no_update` | ABORT |
| `UPDATE` 任意列 | BEFORE UPDATE | **拦** | `trg_ev_no_update` | ABORT |
| `UPDATE OR REPLACE` | 先 UPDATE 触发 | **拦** | `trg_ev_no_update` | ABORT |
| `DELETE` | BEFORE DELETE | **拦** | `trg_ev_no_delete` | ABORT |
| 外键 `ON DELETE CASCADE` | 不存在（用 RESTRICT） | — | FK | 父删除本身被 `trg_dec_no_delete` 拦 |
| 决策 `INSERT OR REPLACE` | 删旧决策（无 DELETE 触发）→ BEFORE INSERT 校验 claim | 部分 | `trg_dec_claim_ins` | 若 claim=real_llm 且证据在位 → 允许（相当于重写决策行）；**残余 R-11** |
| 封印 `INSERT OR REPLACE` | 删旧封（无 DELETE 触发）→ guard 发现同键不存在 → 通过 | **未拦** | — | **残余 R-9**（见 §9.4） |

### 9.3 为什么 seal 能拦住 REPLACE（机制说明）

REPLACE 只删除**与本次插入冲突的那张表**的行。`ai_evidence_seal` 与证据表是不同的表，REPLACE 证据不会删除封印行。因此当 REPLACE 试图重新占用同一 `(decision_id, seq)` 或同一 `evidence_id` 时，`trg_ev_seal_guard_ins` 看到封印仍在 → `RAISE(ABORT)` → 整条语句（含内部删除）回退。

这不是"只检查现有 ID"：它同时检查 **seq 槽位**与 **evidence_id**，且封印由 `AFTER INSERT` 自动生成，正常写入路径无法跳过。

### 9.4 数据库层能保证什么、不能保证什么（不得夸大）

| 能力 | 结论 |
|---|---|
| 阻止普通 `INSERT OR REPLACE` 覆盖证据 | ✅ 由 seal + guard 保证（前提：封印表未被篡改） |
| 阻止 `UPSERT` / `UPDATE` / `DELETE` | ✅ 触发器 ABORT |
| 阻止封印表被 REPLACE | ❌ **不能**（REPLACE 封印行不触发 DELETE 触发器，guard 在删除后看不到旧行）→ **R-9** |
| 阻止有任意 SQL 权限者 `DROP TRIGGER`、直接改写 | ❌ **不能** → **R-3** |
| 证明封印内容真实（哈希正确） | ❌ 不能（SQLite 无 sha256）→ **R-2** |

**因此**：`real_llm` 的可信度上限是"**未被篡改的本地证据链**"，不是"第三方可证明"。任何对外表述必须写明这一点。

### 9.5 设计方向评估（A–E，按任务书要求）

| 方向 | 评估 | 采纳 |
|---|---|---|
| **A. 不可复用主键/唯一约束** | PK + `UNIQUE(decision_id,seq)` 只能防重复，**不能防 REPLACE**（REPLACE 正是先删后插）。必须叠加 seal | 部分采纳（seal） |
| **B. 受控写入路径** | SQLite 无 SQL 级 ACL；只能靠"应用不使用 REPLACE"+ 触发器兜底。**属于应用层**，不写作 DB 保证 | 采纳为约定（§14） |
| **C. 连接/组件隔离** | SQLite 无表级权限；只能靠**文件级权限 + 独立写入进程**。属**操作系统/部署层**，非数据库权限系统 | 采纳为部署要求（§14） |
| **D. 针对 REPLACE 的专项防护** | seal 表 + guard 触发器（§6 第 4/5/10 项），并给出全路径推演（§9.2） | **采纳（核心）** |
| **E. 无法可靠阻止时降级为残余风险** | 封印表自身可被 REPLACE → 只要攻击者能动封印表，链即不可信 → 必须外部锚定 + 限制可信度声明 | **采纳（R-9）** |

---

## 10. 状态机（S-1…S-7，重排优先级）

| 序 | 状态 | 条件（含对前序的否定） |
|---|---|---|
| 1 | `unavailable` | 无凭据/无模型配置，且**未产生任何 socket 级动作**（`http_status=0 ∧ envelope_bytes=0 ∧ upstream_model=''` 且应用记为"未尝试"） |
| 2 | `stub_transport` | 非 1，且运行于测试桩模式 |
| 3 | `local_llm` | 非 1–2，且 I-17 全部满足 |
| 4 | `unknown` | 非 1–3，且 `proxy_used='unknown'`（代理状态不可观测）**或** 无法判定是否发起 |
| 5 | `rejected` | 非 1–4，且**已发起**（发生 DNS/TCP/TLS/HTTP 任一动作），且满足任一：无响应（status=0）／status≠200／`upstream_model=''` |
| 6 | `real_llm` | 非 1–5，且 E-01…E-21 ∧ X-01…X-03 全通过 |
| 7 | `real_llm_unverified` | 其余（已 200 且有 model，但 E/X 未全过） |

**"已发起"的判定（P2-2 修复）**：只要发生了 DNS 查询、TCP 连接、TLS 握手、HTTP 请求中的任一动作，即为"已发起"。DNS 失败 → 已发起 → `rejected`（不再是 `unknown`）。

**关键判定**：

| 输入 | 状态 |
|---|---|
| HTTP 500 + model 非空 | `rejected`（序 5，status≠200） |
| HTTP 200 + 缺 upstream_id | `real_llm_unverified`（序 7） |
| HTTP 200 + model 空 | `rejected`（序 5） |
| DNS 失败 | `rejected`（序 5，已发起） |
| TLS 握手失败 | `rejected`（序 5） |
| 代理不可观测 | `unknown`（序 4，先于 unverified） |
| 本地/私网模型 | `local_llm`（序 3） |
| 测试桩 | `stub_transport`（序 2） |
| 未发起 | `unavailable`（序 1） |

---

## 11. TLS Pin 与 CONNECT 代理证据模型（P1-4）

### 11.1 三层分离

| 层 | 字段 | 能证明什么 | 归属 |
|---|---|---|---|
| **观察** | `tls_verified`、`tls_hostname_verified`、`tls_spki_sha256`、`tls_pin_status` | 本次握手观察到什么 | 应用写入 |
| **执行证明** | `pin_verification_status`、`pin_verification_evidence` | 在证书验证回调中**确实执行了比对**，并把比对输入摘要落库 | 应用写入（**不可由 DB 证明**） |
| **约束** | E-21、I-21…I-24 | 三者不得互相矛盾 | DB CHECK |

### 11.2 语义（不得互相冒充）

| `pin_verification_status` | 含义 | 可否 `real_llm` |
|---|---|---|
| `performed_match` | 在 TLS 校验路径中比对过且一致，`pin_verification_evidence` 为 64 hex 摘要 | ✅（仍需 E-20） |
| `performed_mismatch` | 比对过，不一致 | ❌ |
| `not_performed` | 未执行比对 | ❌ |
| `unsupported` | 运行时不支持（如无证书链访问能力） | ❌ |

**明确写入**：`tls_pin_expected = tls_spki_sha256` **只表示两列相等**，不表示校验发生过。`real_llm` 要求 `performed_match`，而 `performed_match` 本身是**应用声明**——数据库无法验证。因此 `real_llm` 的 pin 部分可信度上限是"应用自证"，列入 R-10。

**`pin_verification_evidence` 的构造建议**（应用层）：对 `(域名, 叶子证书 DER 的 sha256, 期望 pin, 比对结果, 握手时间戳)` 取 sha256，便于事后与抓包/证书日志对照。数据库只校验长度与 hex。

### 11.3 代理 CONNECT 边界（P2-7）

| 场景 | TLS 是否端到端 | 证书主机名校验 | peer 可见 | pin 可验证 | 可否 real_llm |
|---|---|---|---|---|---|
| 直连 | 是 | 对原域名 | 上游 IP | 是 | ✅（全 E 满足时） |
| 代理 CONNECT 隧道 | **是**（隧道内端到端） | 对原域名 | **代理 IP** | 是（可验证原站证书） | ❌（`proxy_unverified`） |
| 代理 + TLS 终止（MITM） | 否 | 对中间证书 | 代理 IP | 否（pin 不符） | ❌ |
| 本地模型（http） | 无 TLS | 不适用 | loopback | 不适用 | ❌（`local_llm`） |

结论：**CONNECT 下 TLS 仍端到端，但网络路径不可验证**，所以只能 `proxy_unverified`；pin 通过也不能提升路径可信度。

---

## 12. 历史数据迁移与清理（P1-2 修复）

### 12.1 步骤（严格顺序）

| 步 | 动作 | 说明 |
|---|---|---|
| 0 | 只读预检：`integrity_check`、`foreign_key_check`、各表行数、`COUNT(DISTINCT decision_id,seq)` | 异常即终止 |
| 1 | 备份 `us_loop.db` → `.bak-<ts>`，记录前后 SHA256 | 仅灾难恢复 |
| 2 | **事务外**：`PRAGMA foreign_keys = OFF`，并断言读回为 0 | 事务内设置无效 |
| 3 | `BEGIN IMMEDIATE`（独占写锁） | 防并发写 |
| 4 | `DROP VIEW IF EXISTS v_decision_verdict; v_evidence_seq; v_seal_check;` | 先删视图 |
| 5 | `DROP TRIGGER IF EXISTS`（12 个，逐个列出） | 先删触发器 |
| 6 | `CREATE TABLE ai_decisions_new / ai_transport_evidence_new / ai_evidence_seal_new`（按 §5.1 结构，表名加 `_new`） | 新表 FK 文本仍写**最终名** |
| 7 | 复制 `ai_decisions` → `ai_decisions_new`：逐行 `record_origin='migrated'`、`migration_batch='<ts>'`、`authoritative_evidence_id=NULL`（P1-1 强制） | 历史 claim **原样保留**；注意 `trg_dec_claim_ins` 会阻止 migrated+real_llm 的插入，故本步必须在步骤 5 删触发器之后执行（窗口风险见 RK-09） |
| 8 | 复制 `ai_transport_evidence` → `_new` | 原样复制 |
| 9 | 由 `_new` 证据生成 `ai_evidence_seal_new`（每 `(decision_id,seq)` 一行，`seal_hash=integrity_hash`，`prev_seal` 取前一条 `seal_hash`） | 封印链从迁移起点重建 |
| 10 | **校验**：三表行数（旧 vs 新）、按主键排序的内容哈希、`foreign_key_check`（新表，FK 仍 OFF 但该 pragma 独立生效） | 任一不符 → ROLLBACK |
| 11 | `DROP TABLE ai_transport_evidence; DROP TABLE ai_evidence_seal; DROP TABLE ai_decisions;`（顺序：先子表后父表） | FK OFF 下允许；**V3.3 遗漏的步骤** |
| 12 | `ALTER TABLE ai_decisions_new RENAME TO ai_decisions;`（evidence、seal 同理，顺序：父表先改名） | 此时无任何对象引用 `_new` 名，RENAME 不改写他人引用 |
| 13 | 建索引（3 个） | |
| 14 | 建触发器（12 个，按 §6 顺序） | |
| 15 | 建视图（3 个） | |
| 16 | `PRAGMA foreign_key_check;`（0 行）、`PRAGMA integrity_check;`（ok） | |
| 17 | 抽样：`v_decision_verdict` 对 migrated+real_llm 必须全为 `historical_transport_unverified`；对 migrated+其他 claim 必须原样输出 | P2-8 修正 |
| 18 | `COMMIT` | 唯一提交点 |

### 12.2 为何不先 RENAME 旧表

SQLite ≥3.25 在 `ALTER TABLE ... RENAME` 时会**改写其他对象中对旧名的引用**。若先改名旧表，另一表的外键会被改写为指向 `_old`，随后删除旧表会产生悬空引用。
本方案改为**建 `_new` → 复制 → 删旧 → 改名 `_new`**：改名时无任何对象引用 `_new` 名，且 FK 文本写的是最终名，改名后自然指向新表。
需实测确认 RENAME 行为（§17-5）。

### 12.3 失败处理

- 任一步失败 → `ROLLBACK` → 数据库回到迁移前状态（含触发器、视图、表结构）。
- **不自动还原备份**（会覆盖迁移期间其他数据）。
- `finally` 中强制 `PRAGMA foreign_keys=ON` 并读回断言（§13）。

### 12.4 迁移后的视图预期（P2-8 修正）

| migrated 行的 claim | 期望 effective_verdict |
|---|---|
| `real_llm` | `historical_transport_unverified` |
| `rejected` / `local_llm` / `stub_transport` / `unavailable` / `unknown` / `real_llm_unverified` | **原样输出**（不统一改写） |
| 任意 + `voided_at` 非空 | `voided` |

---

## 13. 失败回滚与连接恢复（P1-3 修复）

```python
def run_migration(conn):
    fk_on = lambda: conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    if not fk_on():
        raise MigrationAbort("迁移前 foreign_keys 未开启，拒绝执行")

    conn.execute("PRAGMA foreign_keys = OFF")          # 事务外
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 0:
        raise MigrationAbort("无法关闭 foreign_keys，拒绝执行")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            ...  # §12 步骤 4–17
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        # 无论成功/失败/异常，都必须恢复；恢复失败则丢弃连接
        restored = False
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            restored = conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        except Exception:
            restored = False
        if not restored:
            conn.close()                                # 禁止继续用该连接写入
            raise MigrationAbort("foreign_keys 无法恢复，连接已关闭")
```

**规则**：
1. `PRAGMA foreign_keys` 只在**事务外**生效；事务内设置是 no-op（需实测 §17-3）。
2. 每个连接**独立**设置；连接池必须为新连接重设（§14）。
3. 失败路径与成功路径**共用** `finally` 复位。
4. 复位失败 → 关闭连接，**不允许**带着 FK OFF 继续写。
5. 写入函数开头断言 `foreign_keys=1`（§8 第一行），双保险。

---

## 14. 数据库保证 vs 应用层保证

| 项 | 层级 | 说明 |
|---|---|---|
| 列 CHECK、I 族、E 族、PK/FK/UNIQUE | **DB** | 需 `foreign_keys=ON` 才生效 |
| 12 个触发器的 WHEN 判定 | **DB** | 但可被 `DROP TRIGGER` 绕过 |
| seal 防 REPLACE | **DB**（有条件） | 前提：封印表未被篡改（R-9） |
| `pin_verification_status` 的真实性 | **应用** | DB 只能校验其与观察字段一致 |
| `evaluate_readiness()` 全量重算、排除 migrated | **应用** | §10.2/§14 明示，DB 无对应约束 |
| `peer_ip` 与 `peer_class` 一致 | **应用** | DB 无法解析 IP |
| `upstream_id_source` 真实性 | **应用** | 仅声明 |
| 不在代码里使用 `INSERT OR REPLACE` | **应用约定** | DB 已兜底，但不足以对抗恶意写者 |
| 每连接 `PRAGMA foreign_keys=ON` / 连接池初始化 | **应用/运维** | 漏设则 FK 全失效 |
| 文件级权限、独立写入进程、日志外部锚定 | **操作系统/部署** | SQLite 无表级 ACL |

---

## 15. 反例（54 条，静态推演）

| # | 构造 | 预期 | 拦截层 |
|---|---|---|---|
| CE-01 | 三轴任一 NULL | 拒 | NOT NULL |
| CE-02 | 三轴空串 | 拒 | CHECK IN |
| CE-03 | 非法枚举 | 拒 | CHECK IN |
| CE-04 | `upstream_id=NULL` + real_llm | 拒 | E-15 |
| CE-05 | `upstream_id=''` + real_llm | 拒 | E-16 |
| CE-06 | `http_status=NULL` | 拒 | NOT NULL |
| CE-07 | `http_status=500` + real_llm | 拒 | E-10 |
| CE-08 | `evidence_id=NULL` | 拒 | PK NOT NULL |
| CE-09 | `env:` 代理 + `direct_verified` | 拒 | I-12 |
| CE-10 | `E='remote'+peer_class='loopback'+global=1` | 拒 | I-05/I-08 |
| CE-11 | `proxy_class='none'` 但 `proxy_used='env:…'` | 拒 | I-11 |
| CE-12 | `proxy_used=''` | 拒 | 枚举 |
| CE-13 | `reasons='[]'` + unverified | 拒 | I-16 |
| CE-14 | `reasons='[ ]'` + unverified | 拒 | I-16 |
| CE-15 | 多字段同时 NULL | 拒 | NOT NULL |
| CE-16 | `tls_verified=0` + real_llm | 拒 | E-19a |
| CE-17 | `tls_pin_status='not_checked'` + real_llm | 拒 | E-20a |
| CE-18 | `tls_pin_status='not_configured'` + real_llm | 拒 | E-20a |
| CE-19 | `matched` 但 `expected≠spki` | 拒 | I-19 |
| CE-20 | `matched` 但 `spki=''` | 拒 | I-19 长度 |
| CE-21 | `usage_prompt_tokens=0` + real_llm | 拒 | E-18 |
| CE-22 | `proxy_used='unknown'` + `direct_verified` | 拒 | I-15 |
| CE-23 | `local_llm` + `source_declared_by='probe'` | 拒 | I-17 |
| CE-24 | `upstream_id` 非空 + `source=''` | 拒 | I-18 |
| CE-25 | `proxy_unverified` + `peer_role='upstream'` | 拒 | I-04 |
| CE-26 | `env:` 代理 + `proxy_env_present=0` | 拒 | I-20 |
| CE-27 | `UPDATE ai_decisions SET provenance='real_llm'`（无证据） | 拒 | `trg_dec_claim_upd` |
| CE-28 | `UPDATE ai_transport_evidence SET attempt_verdict='real_llm'` | 拒 | `trg_ev_no_update` |
| CE-29 | `UPDATE ai_transport_evidence SET seq=99` | 拒 | `trg_ev_no_update` |
| CE-30 | `DELETE FROM ai_transport_evidence` | 拒 | `trg_ev_no_delete` |
| CE-31 | `DELETE FROM ai_decisions` | 拒 | `trg_dec_no_delete` |
| CE-32 | `prev_hash` 与前一条不符 | 拒 | `trg_ev_chain_ins` |
| CE-33 | `seq=2` 但缺 `seq=1` | 拒 | `trg_ev_chain_ins`（`COALESCE(...,'~')`） |
| CE-34 | 时间倒流或差值 >5 + real_llm | 拒 | X-03 |
| CE-35 | 哈希列 64 个 `Z` | 拒 | `NOT GLOB '*[^0-9a-f]*'` |
| CE-36 | `migrated` + `real_llm` + **指针非空** | 拒 | **M-02**（V3.3 P1-1 修复点） |
| CE-37 | `migrated` 但 `migration_batch` 空 | 拒 | M-01 |
| CE-38 | `voided_at` 有值但 `void_reason` 空 | 拒 | M-03 |
| CE-39 | `reasons='[{"a":1}]'` | 拒 | `trg_ev_json_ins` |
| CE-40 | `reasons='["made_up_reason"]'` | 拒 | `trg_ev_json_ins` |
| CE-41 | 悬空指针（FK 关闭时） | 视图 `integrity_violation` | 视图 NULL 分支 |
| CE-42 | live + `real_llm` + 指针 NULL | 拒 | M-04（INSERT 即拒） |
| CE-43 | **`INSERT OR REPLACE` 同 `evidence_id`** | **拒** | `trg_ev_seal_guard_ins` |
| CE-44 | **`INSERT OR REPLACE` 同 `(decision_id,seq)`、新 id** | **拒** | `trg_ev_seal_guard_ins` |
| CE-45 | `INSERT OR IGNORE` 重复 | 无变更 | 约束（语句忽略） |
| CE-46 | `INSERT ... ON CONFLICT DO UPDATE` | 拒 | `trg_ev_no_update` |
| CE-47 | `UPDATE OR REPLACE` | 拒 | `trg_ev_no_update` |
| CE-48 | 删除决策触发级联 | 拒 | `trg_dec_no_delete`（无 CASCADE） |
| CE-49 | `pin_verification_status='performed_mismatch'` + real_llm | 拒 | E-21a |
| CE-50 | `performed_match` 但 `tls_pin_status='not_checked'` | 拒 | I-21 |
| CE-51 | `performed_match` 但 `expected≠spki` | 拒 | I-21 |
| CE-52 | `performed_match` 但 evidence 长度≠64 | 拒 | E-21b / I-21 |
| CE-53 | 先 `INSERT OR REPLACE` 封印行、再 REPLACE 证据 | **通过（未拦）** | **R-9 残余**（DB 无法拦截） |
| CE-54 | `DROP TRIGGER` 后任意改写 | **通过（未拦）** | **R-3 残余** |

> 统计：**52 条被拒/无变更，2 条（CE-53/CE-54）明确为残余风险**。

---

## 16. 正向场景（23 条）

| # | 场景 | 期望 | 说明 |
|---|---|---|---|
| PS-01 | 直连 + 全 E + 全 X + pin performed_match | `real_llm` | 唯一可达路径 |
| PS-02 | 缺 `upstream_id` | `real_llm_unverified` | E-15 |
| PS-03 | HTTP 500 + model 非空 | `rejected` | 序 5 |
| PS-04 | 信封摘要缺失 | `real_llm_unverified` | E-12 |
| PS-05 | usage=0 | `real_llm_unverified` | E-18 |
| PS-06 | 回环代理 | `real_llm_unverified` | 序 7 |
| PS-07 | 本地模型 | `local_llm` | I-17 |
| PS-08 | 私网模型 | `local_llm` | I-17 |
| PS-09 | DNS 解析失败 | `rejected` | 序 5（**P2-2 修正**） |
| PS-10 | TLS 证书失败 | `rejected` | 序 5 |
| PS-11 | 测试桩 | `stub_transport` | 序 2 |
| PS-12 | 无凭据未发起 | `unavailable` | 序 1 |
| PS-13 | migrated + real_llm | `historical_transport_unverified` | 视图 |
| PS-14 | 多证据 seq1=rejected, seq2=real_llm | `real_llm` | 指针指 seq2 |
| PS-15 | 逻辑作废 | `voided` | 视图首分支 |
| PS-16 | 有环境代理但直连成功 | `real_llm` | `proxy_env_present=1` 不影响 |
| PS-17 | pin 未配置 | `real_llm_unverified` | E-20a |
| PS-18 | 首次 `INSERT`（新 seq） | 成功 + 自动封印 | `trg_ev_seal_after_ins` |
| PS-19 | 连续 seq=1,2,3 链完整 | 全部成功 | chain + seal |
| PS-20 | migrated + `rejected` claim | `rejected`（原样） | P2-8 修正 |
| PS-21 | 代理 CONNECT + pin performed_match | `real_llm_unverified` | 路径不可验证 |
| PS-22 | `INSERT OR IGNORE` 首次插入 | 成功 | 无冲突 |
| PS-23 | 作废后再读 | `voided`，claim 仍为原值 | claim 保留 |

---

## 17. 未验证的 SQLite 语义（全部需隔离实测）

| # | 待测 | 影响 |
|---|---|---|
| 1 | `INSERT OR REPLACE` 是否真的不触发 DELETE 触发器 | P0 防护前提 |
| 2 | `BEFORE INSERT` 中 `RAISE(ABORT)` 是否回退 REPLACE 已执行的删除 | CE-43/44 成立性 |
| 3 | `PRAGMA foreign_keys` 在事务内是否 no-op | §12 步骤 2、§13 |
| 4 | 循环 FK + 双 `DEFERRABLE INITIALLY DEFERRED` 能否建表并提交 | §8 顺序 |
| 5 | `ALTER TABLE RENAME` 是否改写他表 FK 文本 | §12.2 |
| 6 | `AFTER INSERT` 触发器中插入另一表失败，是否回滚整条语句 | 封印原子性 |
| 7 | `json_type()` 对非法 JSON 是报错还是 NULL；是否中止语句 | CE-39/40 |
| 8 | `NOT GLOB '*[^0-9a-f]*'` 对空串与 64-hex 的行为 | 哈希校验 |
| 9 | `COALESCE(...,'~')` 跳号检测是否可靠 | CE-33 |
| 10 | `foreign_key_check` 在事务内是否可见未提交数据 | §12 步骤 10/16 |
| 11 | `RAISE(ABORT)` 在 `BEFORE INSERT` 里对多行语句的语义（逐行 vs 整语句） | 批量插入 |
| 12 | `INSERT OR REPLACE` 对 seal 表是否也跳过 DELETE 触发器（决定 R-9 严重度） | CE-53 |
| 13 | TEXT PK 在显式 NOT NULL 下是否仍可能为 NULL | CE-08 |
| 14 | 同一事件多个 BEFORE 触发器的执行顺序 | 多个 insert 触发器 |

---

## 18. 风险表

| ID | 级别 | 风险 | 处置 |
|---|---|---|---|
| RK-01 | P0 | 有任意 SQL 权限者 `DROP TRIGGER` / 改写任意表 | 无法库内解决 → R-3；外部锚定 + 文件权限 |
| RK-02 | P0 | 应用内存伪造 envelope 后写入 | 无法库内解决 → R-1；上游对账 |
| RK-03 | P1 | 封印表本身可被 REPLACE | 见 R-9；外部锚定 |
| RK-04 | P1 | 触发器/CHECK 行为未实测 | §17；未实测不得称"已强制" |
| RK-05 | P1 | `PRAGMA foreign_keys` 漏设 → FK 全失效 | §13 断言 + 连接池初始化 |
| RK-06 | P1 | 迁移窗口内触发器被删 | RK-09；独占连接 + 单事务 |
| RK-07 | P2 | pin 证明为应用自证 | R-10 |
| RK-08 | P2 | 证据表无限增长 | 归档策略（不删） |
| RK-09 | P2 | 迁移期间触发器缺失窗口 | 独占写锁 + 事后校验 |
| RK-10 | P3 | `record_origin` 可自报 migrated | 只会降级，无收益 |

---

## 19. 残余风险与不可证明事项

| # | 事项 | 说明 |
|---|---|---|
| R-1 | `upstream_id` 值是否真来自上游 | 应用声明；需上游对账 |
| R-2 | `integrity_hash` 是否按正确算法计算 | SQLite 无 sha256 |
| R-3 | 任意 SQL 权限者可整体重写 | 威胁模型边界 |
| R-4 | `local_llm` 是否真是模型 | 运维声明 |
| R-5 | 代理路径下上游身份 | 设计上限 |
| R-6 | 备份还原覆盖新数据 | 故不作回滚手段 |
| R-7 | `unix_received_at` 真实性 | 应用写入 |
| R-8 | 模型名非空 ≠ 真推理 | E-11 必要非充分 |
| **R-9** | **封印表可被 REPLACE** | `BEFORE INSERT` 守卫在 REPLACE 删除后失效；需外部锚定 |
| **R-10** | **`pin_verification_status` 为应用自证** | DB 无法验证握手期比对确实发生 |
| **R-11** | **决策表可被 `INSERT OR REPLACE` 重写** | 仅当 claim=real_llm 且证据在位时通过；旧行的 `created_at` 等被替换，属可检测（需外部锚定） |

---

## 20. 旧编号映射（V3.1 I6-C / I / R → V3.4）

| 旧编号（V3.1/V3.2） | V3.4 编号 | 说明 |
|---|---|---|
| I6-C1…C20 | E-01…E-20 | 一一对应（C1↔E-01 … C20↔E-20） |
| I1/I2/I3 | I-01/I-02/I-03 | 原样 |
| I4（旧 proxy 4 值） | I-04 | 收紧为 3 值 + peer 约束 |
| I5/I6/I7（peer） | I-05/I-06/I-07 | 原样 |
| I8a/I8b | I-08/I-09 | 原样 |
| I9a/I9b/I9c | I-10/I-11/I-12/I-13 | 拆为 4 条 |
| I10 | I-14 | 原样 |
| I11（reasons） | I-16 | 原样 |
| I12 | I-17 | 原样 |
| I13 | I-18 | 原样 |
| I14/I15（TLS） | I-19 | 合并为 1 条 |
| I16（20 项合取） | E 族整体（S-6 引用） | 由 CHECK 表达 |
| I17（跨表） | 视图 + `trg_dec_claim_ins/upd` | 触发器化 |
| R1（provenance） | S-6 | 结论由状态机统一判定 |
| R2（transport_path） | E-01 | |
| R3（endpoint_class） | E-02 | |
| R4（peer_role） | E-03 | |
| R5（peer_class） | E-04 | |
| R6（peer_ip_global） | E-05 | |
| R7（proxy_used） | E-06 | |
| R8（proxy_class） | E-07 | |
| R9（http_method） | E-08 | |
| R10（request_path） | E-09 | |
| R11（http_status） | E-10 | |
| R12（upstream_model） | E-11 | |
| R13（envelope_sha256） | E-12 | |
| R14（envelope_bytes） | E-13 | |
| R15（redirect_count） | E-14 | |
| R16（upstream_id 非空） | E-15 + E-16 | 拆为 NOT NULL 与长度两条 |
| R17（upstream_id_source） | E-17 | |
| R18（usage_prompt_tokens） | E-18 | |
| R19（tls_verified） | E-19a | |
| R20（tls_hostname_verified） | E-19b | |
| R21（tls_pin_status） | E-20a + E-20b | 状态与值比较拆开 |
| R22（evidence_id 跨表） | X-01（触发器）+ 视图 | |
| R23（chain_ok） | X-02（`trg_ev_chain_ins` + 封印链） | |
| R24（late_write） | X-03（`unix_*` CHECK） | |
| — | **E-21a/E-21b** | 新增：pin 执行证明 |
| — | **I-20…I-24** | 新增：proxy_env 一致、pin 三态一致、real_llm 要求 performed |
| — | **M-01…M-04** | 新增：决策表 4 条 CHECK（含 P1-1） |
| — | **X-01…X-03** | 沿用（X-01 触发器化，X-02 链连续性，X-03 时间窗） |

---

## 21. 自检表（不构成审计）

| # | 检查 | 结果 |
|---|---|---|
| C-01 | P0-1：是否有针对 REPLACE 的专门机制（非仅 EXISTS 检查） | ✅ seal 表 + 双键守卫（§6-4/5/10） |
| C-02 | P0-1：是否推演 INSERT/REPLACE/IGNORE/UPSERT/UPDATE/DELETE/CASCADE | ✅ §9.2 十行表 |
| C-03 | P0-1：是否夸大 DB 能力 | ✅ §9.4 明确"封印可被 REPLACE"→R-9 |
| C-04 | P1-1：migrated 指针 NULL 是否独立 CHECK | ✅ M-02 |
| C-05 | P1-2：迁移是否含 DROP 旧表 | ✅ §12 步骤 11 |
| C-06 | P1-3：失败路径是否复位 FK | ✅ §13 finally |
| C-07 | P1-4：pin 是否有独立执行证明字段 | ✅ §11 + E-21/I-21…24 |
| C-08 | P2-1/P2-2 状态机矛盾 | ✅ §10 重排 |
| C-09 | P2-7 CONNECT 边界 | ✅ §11.3 |
| C-10 | P2-8 migrated 其他 claim | ✅ §12.4 + PS-20 |
| C-11 | 反例数 | ✅ 54（≥50） |
| C-12 | 正例数 | ✅ 23（≥20） |
| C-13 | 是否含"略/同前" | ✅ 无 |
| C-14 | 是否宣称已验证 | ✅ 全文标注需实测 |

---

## 22. 交付状态

```
V3.4 设计文件已生成，待独立审计；
方案未获批；
未获实施授权；
未执行任何 SQL；
未创建数据库；
S0 未启动。
```

本文件自检表不构成审计结论，也不代表设计通过。

---

> 本文件 SHA256（本行之前全部字节，可用 `head -n -1 <本文件> | sha256sum` 复算）：`6119859d7c5f1aa4950ed2189826144d8f54b144d99df16d199c757707366cf4`
