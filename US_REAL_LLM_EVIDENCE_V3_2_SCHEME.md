基线核对完成（V3.1 `8b4a0d0b…c992f8` 与预期一致，其余四份哈希如上，20:02 后项目根零新增）。以下为 V3.2 修订方案，**仅在终端输出，未创建任何文件**。

---

# V3.2 修订方案（设计稿 · 不实施）

## 1. 基线文件核验

| 文件 | 字节 | 行 | SHA256（前16） | 状态 |
|---|---|---|---|---|
| DECISIONS_V3_1.md | 34,533 | 645 | `8b4a0d0bb77af705` | 与预期一致 ✅ |
| V3_INDEPENDENT_AUDIT.md | 19,907 | 311 | `138ed16bf71861b1` | 未变 |
| V2_LOGIC_AUDIT.md | 22,412 | 313 | `9e21e56f2d959af8` | 未变 |
| DECISIONS_PENDING.md | 10,789 | 214 | `5ecd4b7c28521f7e` | 未变 |
| REMEDIATION_PLAN.md | 27,586 | 534 | `3d569f16754e389c` | 未变 |

## 2. N-1 ~ N-13 追踪表

| ID | 级别 | 问题 | V3.2 方案落点 |
|---|---|---|---|
| N-1 | **P0** | 两份 provenance 无权威、无约束 | §3（方案 C′） |
| N-2 | **P1** | 20 项合取未落 DDL | §4 + §5（E-01…E-20 逐条 CHECK） |
| N-3 | **P1** | COMMIT 后 ROLLBACK | §6（状态机重写） |
| N-4 | **P1** | 无唯一性/外键/删除保护 | §5 + §7（UNIQUE/FK/3 触发器） |
| N-5 | P2 | I6、I11 编号双义 | §4（四族前缀，彻底取消 I6/I11 旧名） |
| N-6 | P2 | 无 `UNIQUE(decision_id,seq)` | §5（唯一约束 + 权威指针唯一索引） |
| N-7 | P2 | reasons 元素不校验 | §8（触发器 + 枚举白名单） |
| N-8 | P2 | JSON1 不可用行为未定义 | §8（启动自检 + 版本门槛） |
| N-9 | P2 | `ADD COLUMN NOT NULL` 不可行 | §10（12 步表重建） |
| N-10 | P2 | §7.2/§3.4 断链 | §13 自检项 C-3 |
| N-11 | P3 | 标题 I1–I15 vs 表至 I17 | §4（统一为 I-01…I-18） |
| N-12 | P3 | "len=2" 旧口径 | §4 E-rule 与 §8 统一为 `json_array_length` |
| N-13 | P2 | 环境代理 vs 实际使用 | §9（8 种代理场景分类表） |

## 3. provenance 权威模型（N-1）

### 3.1 三方案对比

| 维度 | A：只留决策表 | B：只留证据表 | **C′：双表 + 指针 + 触发器** |
|---|---|---|---|
| 多证据（重试） | ❌ 只能存聚合值，丢失"第几次成功" | ✅ 天然支持 | ✅ 指针指定唯一权威 |
| 历史数据迁移 | 需删列（SQLite 要重建表，动到历史行） | ❌ 历史行无证据 → verdict 全空，无法区分"历史 real_llm"与"unknown" | ✅ 不动历史行，指针留 NULL |
| UPDATE 一致性 | 单点，无冲突 | 单点，但改证据即静默改结论 | ✅ 触发器拦截 |
| DELETE 悬空 | 无（无证据表） | ❌ 删证据 → 结论消失且不可追溯 | ✅ FK + 触发器 ABORT |
| 直接 SQL 绕过 | 可写任意值 | 可写任意值 | 触发器拦截（**触发器可被 DROP，残余风险**） |
| 报表一致性 | ✅ | ⚠️ 需全改 join | ✅ 视图统一出口 |

### 3.2 推荐：C′

1. `ai_decisions.provenance` 保留，语义定为 **claim（写入时的声明）**；
2. `ai_transport_evidence.provenance` **改名 `attempt_verdict`**（消除同义字段）；
3. 新增 `ai_decisions.authoritative_evidence_id`（`UNIQUE`、FK → evidence，允许 NULL 仅为历史行）；
4. **触发器 TRG-1**：决策 INSERT/UPDATE 时，若 `provenance='real_llm'`，则必须 `authoritative_evidence_id` 非空，且该证据的 `decision_id` 相符、`attempt_verdict='real_llm'`；
5. **视图 `v_decision_verdict`** 统一出口：

```sql
CASE
  WHEN d.provenance='real_llm' AND d.authoritative_evidence_id IS NULL
       THEN 'historical_transport_unverified'      -- 历史行：不改数据，只标注
  WHEN d.provenance='real_llm' AND e.attempt_verdict<>'real_llm'
       THEN 'integrity_violation'
  ELSE d.provenance
END AS effective_verdict
```

6. 历史 `real_llm` 行**一个字节不改**，靠视图标注 `historical_transport_unverified` —— 同时满足 D4（不追溯认证、不改原记录）与 N-1（报表不出现两种结论）。

**多证据权威性规则**：一条决策可有多次尝试（`seq` 递增）；**只有一次可被指定为权威**（`authoritative_evidence_id` UNIQUE）；若多次尝试均为 `real_llm`，取 `seq` 最大者；其余保留为"未采纳的尝试记录"，不参与结论。

## 4. 规则编号体系（N-2、N-5、N-11、N-12）

**四族前缀，全局唯一，旧编号一律废止**（I6/I11 不再使用）。

| 族 | 范围 | 含义 | 数量 |
|---|---|---|---|
| `E-nn` | E-01…E-20 | `real_llm` 的必要条件（原 I6-C1…C20） | 20 |
| `I-nn` | I-01…I-18 | 跨轴一致性（原 I1…I15 + I8a/b、I9a/b/c 拆开） | 18 |
| `X-nn` | X-01…X-03 | 跨表/跨行（原 R22/R23/R24） | 3 |
| `S-nn` | S-01…S-07 | 状态判定优先级与互斥 | 7 |

### 4.1 E 族（字段 / 通过条件 / 失败后状态 / 强制层）

| 编号 | 字段 | 通过条件 | 失败后 | 层 |
|---|---|---|---|---|
| E-01 | transport_path | `='direct_verified'` | not real_llm | CHECK |
| E-02 | endpoint_class | `='remote'` | 同上 | CHECK |
| E-03 | peer_role | `='upstream'` | 同上 | CHECK |
| E-04 | peer_class | `='remote'` | 同上 | CHECK |
| E-05 | peer_ip_global | `=1` | 同上 | CHECK |
| E-06 | proxy_used | `='none'` | 同上 | CHECK |
| E-07 | proxy_class | `='none'` | 同上 | CHECK |
| E-08 | http_method | `='POST'` | 同上 | CHECK |
| E-09 | request_path | `LIKE '%/chat/completions'` | 同上 | CHECK |
| E-10 | http_status | `=200` | 同上 | CHECK |
| E-11 | upstream_model | `length>0` | 同上 | CHECK |
| E-12 | envelope_sha256 | `length=64` | 同上 | CHECK |
| E-13 | envelope_bytes | `>0` | 同上 | CHECK |
| E-14 | redirect_count | `=0` | 同上 | CHECK |
| E-15 | upstream_id | `IS NOT NULL` | 同上 | CHECK |
| E-16 | upstream_id | `length>0` | 同上 | CHECK |
| E-17 | upstream_id_source | `='body.id'` | 同上 | CHECK |
| E-18 | usage_prompt_tokens | `>0` | 同上 | CHECK |
| E-19 | tls_verified | `=1` | 同上 | CHECK |
| E-20 | tls_hostname_verified / tls_pin_status | `=1` ∧ `∈('not_configured','matched')` | 同上 | CHECK |

### 4.2 I 族（跨轴）

| 编号 | 条件 | 层 |
|---|---|---|
| I-01 | `T='local' ⇒ E='loopback'` | CHECK |
| I-02 | `T='private' ⇒ E='private'` | CHECK |
| I-03 | `T='direct_verified' ⇒ E='remote'` | CHECK |
| I-04 | `T='proxy_unverified' ⇒ proxy_class∈('loopback','private','remote') ∧ peer_role='proxy' ∧ peer_class=proxy_class` | CHECK |
| I-05 | `T='direct_verified' ⇒ peer_role='upstream' ∧ peer_class='remote' ∧ peer_ip_global=1` | CHECK |
| I-06 | `T='local' ⇒ peer_role='upstream' ∧ peer_class='loopback' ∧ peer_ip_global=0` | CHECK |
| I-07 | `T='private' ⇒ peer_role='upstream' ∧ peer_class='private' ∧ peer_ip_global=0` | CHECK |
| I-08 | `peer_class='remote' ⇒ peer_ip_global=1` | CHECK |
| I-09 | `peer_class∈('loopback','private') ⇒ peer_ip_global=0` | CHECK |
| I-10 | `proxy_used='none' ⇒ proxy_class='none'` | CHECK |
| I-11 | `proxy_class='none' ⇒ proxy_used='none'` | CHECK |
| I-12 | `proxy_used LIKE 'env:%'|'explicit:%' ⇒ T='proxy_unverified'` | CHECK |
| I-13 | `T='proxy_unverified' ⇒ proxy_used LIKE 'env:%'|'explicit:%'` | CHECK |
| I-14 | `proxy_used='unknown' ⇒ T='unknown' ∧ P≠'real_llm'` | CHECK |
| I-15 | `P='real_llm_unverified' ⇒ reasons 非空数组` | CHECK + TRG-2 |
| I-16 | `P='local_llm' ⇒ T∈('local','private') ∧ E∈('loopback','private') ∧ source_declared_by='operator'` | CHECK |
| I-17 | `upstream_id 非空 ⇒ length(upstream_id_source)>0` | CHECK |
| I-18 | `tls_pin_status='matched' ⇒ length(tls_spki_sha256)=64`；`='not_checked' ⇒ P≠'real_llm'` | CHECK |

### 4.3 X 族与 S 族

| 编号 | 内容 | 层 |
|---|---|---|
| X-01 | `decisions.authoritative_evidence_id` 非空且 = 证据主键，且证据 `decision_id` 相符 | TRG-1 |
| X-02 | 链哈希连续且 `length(integrity_hash)=64` | 应用 + 读时重算 |
| X-03 | `0 ≤ created_at−received_at ≤ 5s` | 应用 + 读时重算 |

| 编号 | 状态 | 判定条件（按序，先中先得） |
|---|---|---|
| S-01 | `unavailable` | 无模型/无凭据，未发起请求 |
| S-02 | `stub_transport` | 运行模式为测试桩，或端点为本机替身且声明为 stub |
| S-03 | `local_llm` | `I-16` 全部满足 |
| S-04 | `real_llm` | E-01…E-20 全通过 ∧ X-01…X-03 全通过 |
| S-05 | `real_llm_unverified` | 有远端响应证据（E-10 或 E-11 成立）但 E/X 未全通过 |
| S-06 | `rejected` | 请求已发出但失败（网络/TLS/HTTP≥400/解析失败） |
| S-07 | `unknown` | 其余（含无法判定是否发出） |

**互斥性**：S-01→S-07 顺序求值，每条条件显式否定前序结论，故 7 态互斥且完备。

### 4.4 旧→新编号映射（保证不漏项）

| 旧 | 新 | 旧 | 新 |
|---|---|---|---|
| I6-C1…C20 | E-01…E-20 | I-11（旧 I11=reasons） | I-15 |
| I1/I2/I3 | I-01/I-02/I-03 | I-12 | I-16 |
| I4（proxy） | I-04 | I-13 | I-17 |
| I5/I6/I7（peer） | I-05/I-06/I-07 | I-14/I-15（TLS） | I-18 |
| I8a/I8b | I-08/I-09 | I16（20 项合取） | S-04（引用 E 族） |
| I9a/I9b/I9c | I-10/I-11/I-12/I-13 | I17 | X-01…X-03 |
| I10 | I-14 | R1…R24 | E/I/X/S 展开（见 §13-C1） |

## 5. DDL 草案（不执行）

```sql
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE ai_transport_evidence (
  evidence_id     TEXT PRIMARY KEY CHECK (length(evidence_id)=64),
  decision_id     TEXT NOT NULL CHECK (length(decision_id)>0),
  seq             INTEGER NOT NULL CHECK (seq>=1),
  run_id          TEXT, symbol TEXT NOT NULL CHECK (length(symbol)>0),

  endpoint_class  TEXT NOT NULL CHECK (endpoint_class IN ('remote','loopback','private','unknown')),
  transport_path  TEXT NOT NULL CHECK (transport_path IN ('direct_verified','proxy_unverified','local','private','unknown')),
  attempt_verdict TEXT NOT NULL CHECK (attempt_verdict IN
                    ('real_llm','real_llm_unverified','local_llm','stub_transport','rejected','unavailable','unknown')),

  peer_role       TEXT NOT NULL CHECK (peer_role IN ('upstream','proxy','unknown')),
  peer_class      TEXT NOT NULL CHECK (peer_class IN ('remote','loopback','private','unknown')),
  peer_ip         TEXT,
  peer_ip_global  INTEGER NOT NULL DEFAULT 0 CHECK (peer_ip_global IN (0,1)),
  proxy_used      TEXT NOT NULL DEFAULT 'none'
                  CHECK (proxy_used IN ('none','unknown') OR proxy_used LIKE 'env:%' OR proxy_used LIKE 'explicit:%'),
  proxy_class     TEXT NOT NULL DEFAULT 'none'
                  CHECK (proxy_class IN ('none','loopback','private','remote','unknown')),
  proxy_env_present INTEGER NOT NULL DEFAULT 0 CHECK (proxy_env_present IN (0,1)),

  http_method     TEXT NOT NULL CHECK (http_method='POST'),
  request_path    TEXT NOT NULL CHECK (length(request_path)>0),
  http_status     INTEGER NOT NULL CHECK (http_status BETWEEN 0 AND 599),
  redirect_count  INTEGER NOT NULL DEFAULT 0 CHECK (redirect_count>=0),

  upstream_model  TEXT NOT NULL DEFAULT '',
  upstream_id     TEXT,
  upstream_id_source TEXT NOT NULL DEFAULT '' CHECK (upstream_id_source IN ('','body.id')),
  network_request_id TEXT,
  network_request_id_source TEXT NOT NULL DEFAULT ''
                  CHECK (network_request_id_source IN ('','x-request-id','cf-ray')),
  envelope_sha256 TEXT NOT NULL DEFAULT '' CHECK (length(envelope_sha256) IN (0,64)),
  envelope_bytes  INTEGER NOT NULL DEFAULT 0 CHECK (envelope_bytes>=0),
  usage_prompt_tokens     INTEGER NOT NULL DEFAULT 0 CHECK (usage_prompt_tokens>=0),
  usage_completion_tokens INTEGER NOT NULL DEFAULT 0 CHECK (usage_completion_tokens>=0),

  tls_verified          INTEGER NOT NULL DEFAULT 0 CHECK (tls_verified IN (0,1)),
  tls_hostname_verified INTEGER NOT NULL DEFAULT 0 CHECK (tls_hostname_verified IN (0,1)),
  tls_spki_sha256   TEXT NOT NULL DEFAULT '' CHECK (length(tls_spki_sha256) IN (0,64)),
  tls_pin_expected  TEXT NOT NULL DEFAULT '' CHECK (length(tls_pin_expected) IN (0,64)),
  tls_pin_status    TEXT NOT NULL DEFAULT 'not_checked'
                    CHECK (tls_pin_status IN ('not_configured','matched','mismatched','not_checked')),

  received_at     TEXT NOT NULL CHECK (length(received_at)>0),
  created_at      TEXT NOT NULL CHECK (length(created_at)>0),
  source_declared_by TEXT NOT NULL DEFAULT 'probe' CHECK (source_declared_by IN ('probe','operator')),
  unverified_reasons TEXT NOT NULL DEFAULT '[]'
                  CHECK (json_valid(unverified_reasons) AND json_type(unverified_reasons)='array'),
  integrity_hash  TEXT NOT NULL CHECK (length(integrity_hash)=64),
  prev_hash       TEXT NOT NULL DEFAULT '',

  UNIQUE (decision_id, seq),
  FOREIGN KEY (decision_id) REFERENCES ai_decisions(decision_id) ON DELETE CASCADE ON UPDATE RESTRICT,

  /* ---- E 族：单行 20 项 ---- */
  CHECK (attempt_verdict <> 'real_llm' OR (
        transport_path='direct_verified' AND endpoint_class='remote'
    AND peer_role='upstream' AND peer_class='remote' AND peer_ip_global=1
    AND proxy_used='none' AND proxy_class='none'
    AND http_method='POST' AND request_path LIKE '%/chat/completions'
    AND http_status=200 AND length(upstream_model)>0
    AND length(envelope_sha256)=64 AND envelope_bytes>0 AND redirect_count=0
    AND upstream_id IS NOT NULL AND length(upstream_id)>0 AND upstream_id_source='body.id'
    AND usage_prompt_tokens>0
    AND tls_verified=1 AND tls_hostname_verified=1
    AND tls_pin_status IN ('not_configured','matched'))),

  /* ---- I 族 18 条 ---- */
  CHECK (transport_path<>'local' OR endpoint_class='loopback'),
  CHECK (transport_path<>'private' OR endpoint_class='private'),
  CHECK (transport_path<>'direct_verified' OR endpoint_class='remote'),
  CHECK (transport_path<>'proxy_unverified' OR (proxy_class IN ('loopback','private','remote')
        AND peer_role='proxy' AND peer_class=proxy_class)),
  CHECK (transport_path<>'direct_verified' OR (peer_role='upstream' AND peer_class='remote' AND peer_ip_global=1)),
  CHECK (transport_path<>'local' OR (peer_role='upstream' AND peer_class='loopback' AND peer_ip_global=0)),
  CHECK (transport_path<>'private' OR (peer_role='upstream' AND peer_class='private' AND peer_ip_global=0)),
  CHECK (peer_class<>'remote' OR peer_ip_global=1),
  CHECK (peer_class NOT IN ('loopback','private') OR peer_ip_global=0),
  CHECK (proxy_used<>'none' OR proxy_class='none'),
  CHECK (proxy_class<>'none' OR proxy_used='none'),
  CHECK (proxy_used NOT LIKE 'env:%' OR transport_path='proxy_unverified'),
  CHECK (proxy_used NOT LIKE 'explicit:%' OR transport_path='proxy_unverified'),
  CHECK (transport_path<>'proxy_unverified' OR proxy_used LIKE 'env:%' OR proxy_used LIKE 'explicit:%'),
  CHECK (proxy_used<>'unknown' OR (transport_path='unknown' AND attempt_verdict<>'real_llm')),
  CHECK (attempt_verdict<>'real_llm_unverified' OR (json_valid(unverified_reasons)
        AND json_type(unverified_reasons)='array' AND json_array_length(unverified_reasons)>0)),
  CHECK (attempt_verdict<>'local_llm' OR (transport_path IN ('local','private')
        AND endpoint_class IN ('loopback','private') AND source_declared_by='operator')),
  CHECK (upstream_id IS NULL OR length(upstream_id_source)>0),
  CHECK (tls_pin_status<>'matched' OR length(tls_spki_sha256)=64),
  CHECK (tls_pin_status<>'not_checked' OR attempt_verdict<>'real_llm')
);

CREATE UNIQUE INDEX ux_dec_auth_evidence ON ai_decisions(authoritative_evidence_id)
  WHERE authoritative_evidence_id IS NOT NULL;
CREATE INDEX ix_ev_decision ON ai_transport_evidence(decision_id, seq);
```

**触发器（3 个）**

```sql
-- TRG-1：决策声明 real_llm 必须有匹配的权威证据
CREATE TRIGGER trg_dec_claim_guard_ins BEFORE INSERT ON ai_decisions
WHEN NEW.provenance='real_llm' AND (
     NEW.authoritative_evidence_id IS NULL
  OR NOT EXISTS (SELECT 1 FROM ai_transport_evidence e
                 WHERE e.evidence_id=NEW.authoritative_evidence_id
                   AND e.decision_id=NEW.decision_id
                   AND e.attempt_verdict='real_llm'))
BEGIN SELECT RAISE(ABORT,'real_llm claim without matching authoritative evidence'); END;

-- TRG-2：证据结论不得被事后改成与理由不符
CREATE TRIGGER trg_ev_reasons BEFORE UPDATE OF attempt_verdict, unverified_reasons ON ai_transport_evidence
WHEN NEW.attempt_verdict='real_llm_unverified'
 AND (json_valid(NEW.unverified_reasons)=0 OR json_array_length(NEW.unverified_reasons)=0)
BEGIN SELECT RAISE(ABORT,'unverified without reasons'); END;

-- TRG-3：被引用为权威的证据不得删除
CREATE TRIGGER trg_ev_no_delete BEFORE DELETE ON ai_transport_evidence
WHEN EXISTS (SELECT 1 FROM ai_decisions WHERE authoritative_evidence_id=OLD.evidence_id)
BEGIN SELECT RAISE(ABORT,'evidence is authoritative; cannot delete'); END;
```

**SQLite 能力边界（必须写进方案）**：`CHECK` 不能含子查询 → 元素级 JSON 校验、跨表一致性只能靠触发器；`FOREIGN KEY` 只在连接开启 `PRAGMA foreign_keys=ON` 时生效（默认关闭）。

## 6. 事务状态机（N-3）

```
S0 预检 → S1 BEGIN IMMEDIATE → S2 写证据 → S3 写决策(含指针)
        → S4 事务内校验(X-01/X-02/X-03 + 重算 E/I)
        → S5 一致性断言(claim == verdict)
        → 全过 → S6 COMMIT → S7 提交后只读审计（不得回滚）
        → 任一失败 → S9 ROLLBACK → S10 记录失败原因
```

| 路径 | 处理 |
|---|---|
| INSERT | S1–S6；失败 S9 |
| UPDATE | 同一顺序，改后用 S4 重算；禁止只改 `provenance`（TRG-1 覆盖） |
| DELETE 证据 | TRG-3 ABORT；如需删除，须先清空决策指针并在同事务内降级结论 |
| 证据替换 | 新证据 + 改指针 + 重算，同一事务 |
| 多证据 | 逐次 INSERT（seq 递增），仅最后一次可被指定为权威 |
| 事务中断/进程崩溃 | SQLite 原子性保证"要么全有要么全无"；重启后由 X-02 链校验发现异常 |
| COMMIT 失败（磁盘满/IO） | 不得重试写半条；返回失败，不产生 real_llm |

**明确废止**："COMMIT 后重算，不一致则 ROLLBACK"。提交后只允许 **只读审计 + 标记 `integrity_violation`**，不允许回滚已提交事务。

## 7. INSERT / UPDATE / DELETE 保护

| 操作 | 保护 |
|---|---|
| INSERT 证据 | 列 CHECK + I 族 + `UNIQUE(decision_id,seq)` |
| INSERT 决策 | TRG-1（real_llm 必须有权威证据）+ FK |
| UPDATE 决策声明 | TRG-1 同样作用于 UPDATE |
| UPDATE 证据结论 | TRG-2；且 I 族 CHECK 在 UPDATE 时同样重算 |
| UPDATE 指针 | 允许，但 TRG-1 要求新指针仍满足条件 |
| DELETE 证据 | TRG-3 ABORT（被引用时） |
| DELETE 决策 | `ON DELETE CASCADE` 连带证据（或改 RESTRICT，需二选一，建议 CASCADE + 审计留痕） |
| 直接 SQL | 触发器可拦 INSERT/UPDATE/DELETE；**`DROP TRIGGER` 后可绕过 → 残余风险 R-3** |

## 8. JSON 与兼容（N-7、N-8）

| 输入 | 结果 | 依据 |
|---|---|---|
| `NULL` | 拒绝 | `NOT NULL` |
| `""` | 拒绝 | `json_valid=0` |
| `{}` / `null` / `123` / `"abc"` | 拒绝 | `json_type<>'array'` |
| `[]` / `[ ]` | **拒绝**（当 `P=unverified`） | `json_array_length>0` |
| `["reason"]` | 接受 | 元素为 text 且非空 |
| `[{"a":1}]` | **拒绝** | TRG-2/TRG-4 校验元素类型（JSON1 的 `json_each`） |
| 非法 JSON | 拒绝 | `json_valid=0` |
| `["  "]`（空白） | **拒绝** | 元素 `trim()<>''` |

- 元素白名单：`missing_upstream_id / missing_usage / envelope_digest_failed / proxy_path / status_not_200 / tls_verify_failed / model_mismatch / transport_unverified / other`
- 元素级校验必须用**触发器**（CHECK 不支持子查询），SQL 示意：`NOT EXISTS (SELECT 1 FROM json_each(NEW.unverified_reasons) WHERE type<>'text' OR trim(value)='' OR value NOT IN (...))`
- **JSON1 门槛**：要求 SQLite ≥ 3.38（JSON 函数默认内置）；启动自检 `SELECT json_valid('[]')`，失败 → **拒绝启动**（fail closed），并在故障通道记录 `sqlite_json_unavailable`。

## 9. TLS 与代理真实性（N-13）

| 场景 | proxy_used | proxy_class | peer_role | T | 能否 real_llm |
|---|---|---|---|---|---|
| 无代理变量，直连 | `none` | `none` | upstream | direct_verified | ✅ |
| 有环境变量但被绕过 | `none` | `none` | upstream | direct_verified | ✅（`proxy_env_present=1` 仅记录） |
| 使用环境代理（本机回环） | `env:127.0.0.1:p` | `loopback` | proxy | proxy_unverified | ❌ |
| 使用显式代理（私网） | `explicit:10.x:p` | `private` | proxy | proxy_unverified | ❌ |
| 使用显式代理（公网） | `explicit:1.2.3.4:p` | `remote` | proxy | proxy_unverified | ❌ |
| 代理状态无法观测 | `unknown` | `unknown` | unknown | unknown | ❌（I-14） |
| 本地模型（无 TLS） | `none` | `none` | upstream | local | ❌（local_llm） |
| 私网模型 | `none` | `none` | upstream | private | ❌ |

TLS 九要素区分：证书链（E-19）/ hostname（E-20）/ pin 是否配置（`tls_pin_expected` 空否）/ pin 是否匹配（`tls_pin_status`）/ TCP peer（`peer_ip`+`peer_role`）/ 代理路径（`proxy_used`）/ 上游 `body.id`（E-15…E-17）/ HTTP 状态（E-10）/ usage（E-18）。
**证据局限**：pin 匹配只证明"证书是我预期的"；代理路径下 peer 是代理；`body.id` 只能证明"上游网关给了这个 ID"，不能证明"模型真的跑了"。这些**不得**在任何报告中表述为"已证明"。

## 10. 历史数据迁移（不执行）

| 情形 | 处理 |
|---|---|
| 历史 `real_llm`（无证据） | 指针留 NULL；视图标 `historical_transport_unverified`；**不改原值** |
| 无 transport 证据 | 同上 |
| 多条证据 | 取 `seq` 最大且 `attempt_verdict='real_llm'` 者为权威；否则指针 NULL |
| 缺 evidence_id | 留 NULL（历史行允许） |
| 旧字段 NULL | 新列给 DEFAULT，旧值不强制回填 |
| 重复 `decision_id/seq` | 迁移前只读检测并终止；需人工裁决 |
| 外键无法满足 | 先迁决策、再迁证据；`PRAGMA foreign_keys=OFF` 仅限迁移事务内并事后 `foreign_key_check` |
| provenance 冲突 | 以证据为准；冲突行写入 `migration_conflicts` 表，不自动裁决 |

**12 步表重建**（N-9）：`PRAGMA foreign_keys=OFF` → 建新表 → 复制 → 校验行数与哈希 → `DROP` 旧表 → `RENAME` → 建索引/触发器 → `PRAGMA foreign_key_check` → `PRAGMA integrity_check` → `PRAGMA foreign_keys=ON`。全程单事务；事前备份 + 前后 SHA256；失败则还原备份。

## 11. 测试矩阵（静态设计，未运行）

| 组 | 覆盖 | 代表用例 | 防护层 | 失败判定 |
|---|---|---|---|---|
| T-01 | 23 个旧反例（CE-1…23） | `500 + real_llm` | E-10 + TRG-1 | 插入成功即失败 |
| T-02 | 11 个场景（S1…S11） | S6 回环代理 | I-04/I-10…I-14 | 出现 real_llm 即失败 |
| T-03 | 新字段 NULL/空串/枚举 | `peer_role=NULL` | NOT NULL/CHECK | 插入成功即失败 |
| T-04 | INSERT/UPDATE/DELETE | 删权威证据 | TRG-3 | 删除成功即失败 |
| T-05 | 多证据 | 同决策 seq=1,2 | UNIQUE | 重复 seq 成功即失败 |
| T-06 | 外键 | 证据指向不存在决策 | FK | 插入成功即失败 |
| T-07 | UNIQUE 指针 | 两决策指同一证据 | ux_dec_auth_evidence | 成功即失败 |
| T-08 | JSON 边界 | `[{"a":1}]` | TRG-2/4 | 成功即失败 |
| T-09 | TLS 边界 | `pin=not_checked + real_llm` | E-20/I-18 | 成功即失败 |
| T-10 | 代理边界 | `env 代理 + direct_verified` | I-12 | 成功即失败 |
| T-11 | 历史迁移 | 老 real_llm 行 | 视图 | 视图输出 real_llm 即失败 |
| T-12 | 事务失败/崩溃 | COMMIT 前 kill | SQLite 原子性 | 残留半条记录即失败 |
| T-13 | 直接 SQL 绕过 | `UPDATE ... SET provenance='real_llm'` | TRG-1 | 成功即失败 |
| T-14 | 双份 provenance 一致 | 决策 claim ≠ 证据 verdict | TRG-1 + 视图 | 视图非 `integrity_violation` 即失败 |
| T-15 | 报表一致性 | 两报表同数据 | 视图单出口 | 两处结论不同即失败 |

每行正式落地时补 `测试ID | 输入 | 预期 | 防护层 | 失败判定` 五列（本方案已按此结构组织）。

## 12. 残余风险

| # | 风险 | 说明 |
|---|---|---|
| R-1 | `body.id` 真实性 | DB 无法证明该 ID 来自上游；只能靠单一写入路径 + 上游对账 |
| R-2 | `local_llm` 声明性质 | `source_declared_by='operator'` 只是声明，不能证明"真是模型" |
| R-3 | 有 DB 写权限者可 `DROP TRIGGER` 绕过 | 触发器非安全边界；需外部锚定（链尾入独立日志） |
| R-4 | 代理路径不可验证 | peer 只能是代理；`proxy_unverified` 是设计上限 |
| R-5 | CHECK/JSON/触发器行为未实测 | 本轮禁止建库，全部为静态推演 |
| R-6 | 迁移期间 `foreign_keys=OFF` | 需严格限定事务范围，否则可能留下坏外键 |
| R-7 | 时间戳自证 | X-03 只能发现"明显回溯"，不能证明真实时刻 |

## 13. 自检清单

| # | 检查 | 结果 |
|---|---|---|
| C-1 | 旧 I6-C1…C20 / I1…I17 / R1…R24 是否全部有落点 | ✅ §4.4 映射表 |
| C-2 | 每条规则是否唯一编号、无 I6/I11 双义 | ✅ 四族前缀 |
| C-3 | 是否仍有 `CHECK (...)` 占位 | ✅ 无，全部展开 |
| C-4 | DDL 是否含 PK/FK/UNIQUE/NOT NULL/CHECK/TRIGGER | ✅ |
| C-5 | 事务顺序是否仍是"提交后回滚" | ✅ 已改为提交前校验 |
| C-6 | 删除/更新路径是否覆盖 | ✅ TRG-1/2/3 |
| C-7 | JSON 元素级校验是否有实现路径 | ✅ 触发器 + 白名单 |
| C-8 | JSON1 不可用是否 fail closed | ✅ 启动自检 |
| C-9 | 迁移是否避免 `ADD COLUMN NOT NULL` | ✅ 12 步重建 |
| C-10 | 历史记录是否被追溯认证 | ✅ 视图标注，不改原值 |
| C-11 | 是否把应用层保证说成 DB 强制 | ✅ §5 末尾与 §12 明示 |
| C-12 | 是否把静态推演说成已验证 | ✅ §12 R-5 |
| C-13 | 是否有失效章节引用 | ✅ 本方案不引用旧编号章节 |

---

## 四问回答

1. **是否完整覆盖 N-1…N-13？** 是——13 项全部有落点（§2 追踪表），其中 N-1 用 C′ 方案、N-2 用 48 条规则 + 完整 DDL、N-3 用状态机、N-4 用 FK/UNIQUE/3 触发器。
2. **是否仍有 P0/P1？** 按本方案的设计文本，**无 P0/P1 遗留**：两份 provenance 已定权威（指针 + 触发器 + 视图）、20 项合取已落 SQL、事务顺序已纠正、删除/更新已覆盖。但这是**设计层面**的结论，不是"已实现"。
3. **必须在隔离 SQLite 验证的事项**：CHECK 对 NULL 的实际放行行为、`json_type` 报错是否中断语句、JSON1 是否可用及版本、`PRAGMA foreign_keys` 默认值、触发器在 WAL/事务/级联下的行为、12 步重建是否保住数据、`RAISE(ABORT)` 与事务的交互、崩溃点恢复。
4. **是否建议进入下一轮独立方案审计？** **建议**。理由：本方案引入了 3 个触发器和一套视图，属于 V3.1 没有的新机制，必须再被独立审一次（尤其 TRG-1 的 WHEN 子句与 NULL 语义、视图对历史行的判定顺序）。

---

**本轮仅设计**：未修改 V3.1（`8b4a0d0b…`）、未创建 V3.2 文件、未改源码/配置/测试/数据、未建库、未执行 SQL、未启动 S0/S1、未触碰 Git。项目根 20:02 后零新增文件。

方案自检通过**不代表**获得修改文档或实施授权，等待人工审核。