# US Real LLM Evidence — 决策确认单 V3（修订版，待审核）

| 项 | 值 |
|---|---|
| 版本 | **v3**（2026-11-10 19:40） |
| 取代 | `US_REAL_LLM_EVIDENCE_DECISIONS_PENDING.md` v2（`5ecd4b7c…`，**不覆盖、不修改**） |
| 依据 | `US_REAL_LLM_EVIDENCE_V2_LOGIC_AUDIT.md`（`9e21e56f…`）之 M-1…M-9 与 C1…C6 |
| 性质 | **设计文档修订**。不实施源码、不建库、不改数据 |
| 状态 | **V3 待用户审核**；方案已批准；四项决策已确认；源码实施未授权；S0 未启动 |

> v2 的 5 处硬矛盾（3 类场景无合法状态）在 v3 中已消除。本文件同时给出可执行的 CHECK 约束、16 项 readiness 谓词与全部反例的前后对照。

---

## 1. 修订总览（相对 v2 的 10 处实质变更）

| 编号 | 变更 | 修复的审查项 |
|---|---|---|
| V3-01 | `provenance` 增 `local_llm`（非公网真实模型） | M-4 |
| V3-02 | **删除 I7**；改由 `unverified_reasons` 非空约束 | M-1 |
| V3-03 | 放宽 I5：代理路径允许回环/私网代理 | M-2 |
| V3-04 | 明确 `endpoint_class` = **请求目标地址**；新增 `proxy_class` / `peer_class` 观测字段 | M-3 |
| V3-05 | 本地真实模型的合法组合定义 | 需求 5 |
| V3-06 | `upstream_id` **只认 `body.id`**；`x-request-id`/`cf-ray` 降级为 `network_request_id` | M-5 |
| V3-07 | 三轴 `NOT NULL` + 枚举 `CHECK IN` + 非空用 `length()>0`（禁 `!= ''`） | M-6 |
| V3-08 | readiness 固定 **16 项** R1–R16 并逐条给属性 | M-7 |
| V3-09 | `evidence_id` 必须非空且跨表相等 | M-8 |
| V3-10 | **删除 I4**（`T=unknown ⇒ E=unknown` 过强） | M-9 |

---

## 2. M-1…M-9 追踪表（一对一）

| 审查项 | 问题摘要 | V3 条款 | V3 具体做法 | 验证位置 |
|---|---|---|---|---|
| **M-1** | `real_llm_unverified ⇒ proxy_unverified` 过窄，3 类场景无解 | V3-02 | 删除该蕴含式；改为 `P=real_llm_unverified ⇒ length(unverified_reasons)>2`；`T` 可为 direct/proxy/unknown | §5 C7、§4 S2/S4/S5 |
| **M-2** | I5 排除回环代理（本机真实情况） | V3-03 | `T=proxy_unverified ⇒ E ∈ {remote,loopback,private,unknown}` | §5 C4、§4 S6 |
| **M-3** | E 未定义"目标"还是"对端" | V3-04 | E 固定为**目标地址**；新增 `proxy_class`、`peer_class` | §3.4、§4 S6 |
| **M-4** | 本地真实模型只能标 stub | V3-01 / V3-05 | 新增 `local_llm`；`(E=loopback,T=local)` 与 `(E=private,T=private)` 合法 | §5 C6、§4 S7/S8 |
| **M-5** | `upstream_id` 可用网关/CF 的 ID 顶替 | V3-06 | 只认 `body.id`；另存 `network_request_id` + `_source`，**不参与** readiness | §6、§5 C8 |
| **M-6** | 无 NOT NULL / 枚举 CHECK，NULL 绕过 | V3-07 | 三轴 `NOT NULL` + `CHECK IN (...)`；空串靠 `CHECK IN` 拒绝；`upstream_id` 用 `IS NOT NULL AND length>0` | §5 C1–C3 |
| **M-7** | readiness "11 项"无清单 | V3-08 | R1–R16 逐条列出，含来源/侧别/可伪造性 | §7 |
| **M-8** | `evidence_id` 双侧 NULL 判等为真 | V3-09 | R13 要求 `evidence_id IS NOT NULL AND = decision.evidence_id` | §5 C7、§7 R13 |
| **M-9** | I4 过强 | V3-10 | 删除 I4；改为 `T=unknown ⇒ P ≠ real_llm`（已含于 I6） | §5 C4、§4 S11 |

---

## 3. 三轴定义（定稿）

### 3.1 `endpoint_class` — **请求目标地址**的性质

| 值 | 定义 |
|---|---|
| `remote` | base_url 主机解析出的**全部**地址均为公网全局地址 |
| `loopback` | 目标即本机（127.0.0.0/8、::1、`localhost`、`127.1`、`2130706433` 等归一化后） |
| `private` | 目标为私网/链路本地/CGNAT（10/8、172.16/12、192.168/16、169.254/16、100.64/10、fc00::/7） |
| `unknown` | 无法解析、解析为空、多地址含非公网、pin 失败等（fail-closed 落点） |

**明确**：E 描述"你要访问谁"，**不是**"实际连到了谁"。后者由 `peer_class` 记录。

### 3.2 `transport_path` — 连接路径与对端验证程度

| 值 | 定义 | 是否验证过对端 |
|---|---|---|
| `direct_verified` | 直连；`getpeername()` 命中解析集合且为公网；无代理、无重定向 | ✅ |
| `proxy_unverified` | 经代理（环境变量或显式）：代理不参与上游对端校验 | ❌（设计如此） |
| `local` | 目标即本机/本地服务（多为回环），无需公网对端校验 | 不适用 |
| `private` | 目标为私网服务 | 不适用 |
| `unknown` | 路径无法判定（TLS 失败、连接中断、钉死集合不一致等） | ❌ |

### 3.3 `provenance` — 决策来源与证据结论（7 值）

| 值 | 含义 | 可否通过 readiness |
|---|---|---|
| `real_llm` | 远端真实模型 + 全链证据通过 | ✅（唯一） |
| `real_llm_unverified` | 远端真实模型，但证据链不完整/传输未验证（须带 reasons） | ❌ |
| `local_llm` | **非公网真实模型**（loopback 或私网，自建/本地部署） | ❌ |
| `stub_transport` | 已知替身（测试桩、mock） | ❌ |
| `rejected` | 请求失败/被拒/响应非法 | ❌ |
| `unavailable` | 无模型（未配置、无凭据） | ❌ |
| `unknown` | 无法判定（DNS/TLS/路径全失） | ❌ |

### 3.4 三个观测字段（不参与跨轴不变量，只用于如实记录）

| 字段 | 取值 | 说明 |
|---|---|---|
| `proxy_class` | `none` / `loopback` / `private` / `remote` / `unknown` | 代理地址类别；`none` 表示未使用代理 |
| `peer_class` | `loopback` / `private` / `remote` / `unknown` | 实际 TCP 对端类别；取不到时为 `unknown` |
| `peer_ip_global` | `0/1` | 对端是否为公网；`0` 时不得 `real_llm` |
| `proxy_env_present` | `0/1` | 环境里是否存在 `HTTP(S)_PROXY`。**存在≠使用**：本机有此变量但 `TransportClient` 不读它，故可为 1 而 `proxy_used=none` |

这样"目标 remote + 代理 loopback + 对端 loopback"可以被**同时、如实**记录，而不是被迫二选一（M-3 的正解）。

---

## 4. 三轴合法状态真值表（11 个场景）

> `R` = readiness 结果（全部谓词见 §7）。

| # | 场景 | `endpoint_class` | `transport_path` | `provenance` | reasons | 观测字段 | R |
|---|---|---|---|---|---|---|---|
| S1 | 直连公网，全部证据通过 | `remote` | `direct_verified` | `real_llm` | — | proxy=none, peer=remote, global=1 | ✅ **TRUE** |
| S2 | 直连公网，缺 `response_id` | `remote` | `direct_verified` | `real_llm_unverified` | `missing_upstream_id` | 同上 | ❌ FALSE |
| S3 | 直连公网，HTTP 500 | `remote` | `direct_verified` | `rejected` | `status_not_200` | 同上 | ❌ FALSE |
| S4 | 直连公网，响应摘要校验失败 | `remote` | `direct_verified` | `real_llm_unverified` | `envelope_digest_failed` | 同上 | ❌ FALSE |
| S5 | 直连公网，usage 缺失 | `remote` | `direct_verified` | `real_llm_unverified` | `missing_usage` | 同上 | ❌ FALSE |
| S6 | 本机代理转发公网 | `remote` | `proxy_unverified` | `real_llm_unverified` | `proxy_path` | proxy=loopback, peer=loopback | ❌ FALSE |
| S7 | 本地真实模型（ollama 等） | `loopback` | `local` | `local_llm` | — | proxy=none, peer=loopback | ❌ FALSE |
| S8 | 私网模型服务 | `private` | `private` | `local_llm` | — | proxy=none, peer=private | ❌ FALSE |
| S9 | 未知目标地址（解析为空） | `unknown` | `unknown` | `unknown` | `dns_empty` | peer=unknown | ❌ FALSE |
| S10 | DNS 解析异常（超时/抛错） | `unknown` | `unknown` | `unknown` | `dns_failure` | peer=unknown | ❌ FALSE |
| S11 | TLS 验证失败 | `remote` | `unknown` | `rejected` | `tls_verify_failed` | peer=unknown | ❌ FALSE |

**逐场景不变量自检**（这是 v2 出问题的地方，逐条验）：

| # | I1 | I2 | I3 | I5 | I6 | I7′ | I8 | 结论 |
|---|---|---|---|---|---|---|---|---|
| S1 | — | — | ✅ | — | ✅ 全项满足 | — | — | 合法 |
| S2 | — | — | ✅ | — | 不适用（P≠real_llm） | ✅ reasons 非空 | — | 合法（v2 无解） |
| S3 | — | — | ✅ | — | 不适用 | 不适用 | — | 合法 |
| S4 | — | — | ✅ | — | 不适用 | ✅ | — | 合法（v2 无解） |
| S5 | — | — | ✅ | — | 不适用 | ✅ | — | 合法（v2 无解） |
| S6 | — | — | — | ✅ remote 在集合内 | 不适用 | ✅ | — | 合法（v2 无解） |
| S7 | ✅ loopback | — | — | — | 不适用 | — | ✅ local+loopback | 合法（v2 无值） |
| S8 | — | ✅ private | — | — | 不适用 | — | ✅ private+private | 合法（v2 无值） |
| S9 | — | — | — | — | 不适用 | — | — | 合法 |
| S10 | — | — | — | — | 不适用 | — | — | 合法 |
| S11 | — | — | — | — | 不适用 | 不适用 | — | 合法（v2 违反 I4） |

**结果：11/11 场景均有唯一合法取值**（v2 时 3 个无解、1 个无值、1 个被误判非法）。

---

## 5. 跨轴不变量（v3 定稿：定义 9 条，其中 I4 已删除 → 生效 8 条 + I9 跨表断言）

| # | 规则 | 强制层 | v2 对照 |
|---|---|---|---|
| **I1** | `T='local' ⇒ E='loopback'` | DB CHECK | 保留 |
| **I2** | `T='private' ⇒ E='private'` | DB CHECK | 保留 |
| **I3** | `T='direct_verified' ⇒ E='remote'` | DB CHECK | 保留 |
| **I4** | ~~`T='unknown' ⇒ E='unknown'`~~ | — | **删除**（M-9）；改由 I6 保证 `T=unknown` 时不可能 `real_llm` |
| **I5** | `T='proxy_unverified' ⇒ E ∈ {remote,loopback,private,unknown}` | DB CHECK | 放宽（M-2） |
| **I6** | `P='real_llm' ⇒ T='direct_verified' ∧ E='remote' ∧ upstream_id 非空 ∧ http_status=200 ∧ length(envelope_sha256)=64 ∧ envelope_bytes>0 ∧ upstream_model 非空 ∧ peer_ip_global=1 ∧ proxy_used='none' ∧ redirect_count=0` | DB CHECK（**12 个合取项**） | 加强（原仅 3 项，M-1/C5） |
| **I7′** | `P='real_llm_unverified' ⇒ length(unverified_reasons)>2` | DB CHECK | 取代 I7（M-1） |
| **I8** | `P='local_llm' ⇒ T ∈ {local,private} ∧ E ∈ {loopback,private}` | DB CHECK | 新增（M-4） |
| **I9** | `P='real_llm' ⇒ evidence_id 非空 ∧ = ai_decisions.evidence_id ∧ chain_ok=1 ∧ late_write=0` | **写入断言 + readiness**（跨表，CHECK 做不到） | 新增（M-8） |

> 计数口径：DB CHECK 承载 8 条（I1/I2/I3/I5/I4′/I6/I7′/I8）+ 3 条代理一致性；I9 由写入断言与 readiness R13–R15 承担。

**分层原则**：
- DB CHECK 保证"单行自不自相矛盾"；
- 写入断言保证"跨表关联成立"；
- readiness 保证"这条总结论是否可对外宣称"。
三层缺一不可，且**不互相替代**。

---

## 6. `upstream_id` 规则（M-5 定稿）

| 项 | 规则 |
|---|---|
| 唯一来源 | 响应体 `id`（`choices` 同级字段）。**仅此一个** |
| 禁止来源 | `x-request-id`（中转自签）、`cf-ray`（CF 边缘）、`decision_id`/`signal_id`/`run_id`（本地生成） |
| 次级 ID 去处 | 写入 `network_request_id` + `network_request_id_source`，**仅供排查，不参与 readiness** |
| 类型 | 必须是 `str`；非 str（数字/对象/数组）→ 视为缺失 |
| 规范化 | 仅 `strip()`；**不得**改大小写、截断、重排 |
| 空/缺/异常 | 一律 `NULL`（不用 `""`）；`""` 视为非法输入直接拒写 |
| 长度/字符 | `1 ≤ len ≤ 200`；`^[A-Za-z0-9_\-:.]+$` |
| 本地冒充防线 | `upstream_id` 列**只允许**由响应解析函数写入（单一写入路径）；写入前断言 `upstream_id ∉ {decision_id, signal_id, run_id}` |
| 历史数据 | 老行无该列 → `NULL`；readiness 用 `IS NOT NULL AND length>0`，NULL 必然不过（C2 的修法） |

> 诚实说明：以上能阻止"明显冒充"，但**无法**从值本身证明它一定来自上游（一个长得像的字符串仍可能被塞进来）。真正的防线是"单一写入路径 + 同事务 + 链哈希 + 上游对账"，属 §10 的残余风险。

---

## 7. SQLite CHECK 约束设计（完整）

### 7.1 列级约束

```sql
endpoint_class     TEXT    NOT NULL CHECK (endpoint_class IN ('remote','loopback','private','unknown')),
transport_path     TEXT    NOT NULL CHECK (transport_path IN ('direct_verified','proxy_unverified','local','private','unknown')),
provenance         TEXT    NOT NULL CHECK (provenance IN ('real_llm','real_llm_unverified','local_llm','stub_transport','rejected','unavailable','unknown')),

unverified_reasons TEXT    NOT NULL DEFAULT '[]',
http_method        TEXT    NOT NULL CHECK (http_method = 'POST'),
request_path       TEXT    NOT NULL CHECK (length(request_path) > 0),
http_status        INTEGER NOT NULL CHECK (http_status BETWEEN 0 AND 599),
upstream_model     TEXT    NOT NULL DEFAULT '',
upstream_id        TEXT,                                    -- 允许 NULL；P=real_llm 时由 I6 禁止
upstream_id_source TEXT    NOT NULL DEFAULT '' CHECK (upstream_id_source IN ('','body.id')),
network_request_id TEXT,                                   -- 次级 ID，不参与 readiness
network_request_id_source TEXT NOT NULL DEFAULT '' CHECK (network_request_id_source IN ('','x-request-id','cf-ray')),
envelope_sha256    TEXT    NOT NULL DEFAULT '',
envelope_bytes     INTEGER NOT NULL DEFAULT 0 CHECK (envelope_bytes >= 0),
peer_ip_global     INTEGER NOT NULL DEFAULT 0 CHECK (peer_ip_global IN (0,1)),
peer_class         TEXT    NOT NULL DEFAULT 'unknown' CHECK (peer_class IN ('remote','loopback','private','unknown')),
proxy_class        TEXT    NOT NULL DEFAULT 'none'    CHECK (proxy_class IN ('none','loopback','private','remote','unknown')),
proxy_used         TEXT    NOT NULL DEFAULT 'none',
proxy_env_present  INTEGER NOT NULL DEFAULT 0 CHECK (proxy_env_present IN (0,1)),
redirect_count     INTEGER NOT NULL DEFAULT 0 CHECK (redirect_count >= 0),
integrity_hash     TEXT    NOT NULL CHECK (length(integrity_hash) = 64),
```

### 7.2 表级约束（8 条跨轴不变量 + 3 条代理一致性；共 11 条 CHECK）

```sql
CHECK (transport_path <> 'local'                  OR endpoint_class = 'loopback'),
CHECK (transport_path <> 'private'                OR endpoint_class = 'private'),
CHECK (transport_path <> 'direct_verified'        OR endpoint_class = 'remote'),
CHECK (transport_path <> 'proxy_unverified'       OR endpoint_class IN ('remote','loopback','private','unknown')),
CHECK (transport_path <> 'unknown'                OR provenance <> 'real_llm'),

CHECK (provenance <> 'real_llm_unverified' OR length(unverified_reasons) > 2),
CHECK (provenance <> 'local_llm' OR (transport_path IN ('local','private') AND endpoint_class IN ('loopback','private'))),

CHECK (provenance <> 'real_llm' OR (
        transport_path      = 'direct_verified'
    AND endpoint_class      = 'remote'
    AND upstream_id IS NOT NULL AND length(upstream_id) > 0
    AND upstream_id_source  = 'body.id'
    AND http_status         = 200
    AND length(envelope_sha256) = 64
    AND envelope_bytes      > 0
    AND length(upstream_model) > 0
    AND peer_ip_global      = 1
    AND proxy_used          = 'none'
    AND redirect_count      = 0
)),

-- 代理一致性：只要用了代理，路径必须是 proxy_unverified；反之亦然
CHECK (proxy_used = 'none' OR transport_path = 'proxy_unverified'),
CHECK (transport_path <> 'proxy_unverified' OR proxy_used <> 'none'),
CHECK (proxy_used <> 'none' OR proxy_class = 'none')
```

### 7.3 三值逻辑检查（逐条）

| 风险 | 是否仍存在 | 说明 |
|---|---|---|
| 三轴为 NULL 使蕴含式恒 NULL | **已消除** | 三轴 `NOT NULL`；`CHECK` 只在 NULL 时"放过"，`NOT NULL` 才是防线 |
| `!= ''` 对 NULL 求值为 NULL | **已消除** | 全部改 `IS NOT NULL AND length(x) > 0`；空串由 `CHECK IN` 或 `length>0` 拒绝 |
| 枚举外值（含空串） | **已消除** | `CHECK IN` |
| `http_status` NULL | **已消除** | `NOT NULL` |
| `peer_ip_global` 存成 2 或 '1' | **已消除** | `CHECK IN (0,1)` + INTEGER 亲和性 |
| `unverified_reasons` 为 `'[]'` 但 P=unverified | **已消除** | `length('[]') = 2`，约束要求 `> 2` |
| `proxy_used='none'` 但 `proxy_class='loopback'` | **已消除** | 末条 CHECK |
| `T='proxy_unverified'` 但 `proxy_used='none'` | **已消除** | 新增两条代理一致性 CHECK（双向） |
| **`evidence_id` 跨表一致** | **CHECK 做不到** | 交由写入断言 + readiness R13 |
| **`upstream_id` 值是否为"真上游"** | **无法在 DB 层证明** | 单一写入路径 + 链哈希 + 上游对账（§10 残余风险） |

---

## 8. 反例前后对照（C1–C6 + 用户要求的 9 项）

| 编号 | 反例（尝试写入的行） | v2 结果 | v3 结果 | 由谁拦住 |
|---|---|---|---|---|
| **C1** | `T=NULL` + `P='real_llm'` | ❌ 通过（NULL 使 I6 为 NULL） | ✅ 拒绝 | `transport_path NOT NULL` |
| **C2** | `upstream_id=NULL` + `P='real_llm'` | ❌ 通过（`!= ''` 为 NULL） | ✅ 拒绝 | I6 的 `IS NOT NULL AND length>0` |
| **C3** | `E=NULL` + `T='direct_verified'` | ❌ 通过 | ✅ 拒绝 | `endpoint_class NOT NULL` |
| **C4** | `T=''`（空串） | ❌ 通过（不在任何比较分支） | ✅ 拒绝 | `CHECK IN` |
| **C5** | `P='real_llm'` + `http_status=500` | ❌ 通过（I6 无 status） | ✅ 拒绝 | I6 增加 `http_status=200` |
| **C6** | `P='real_llm_unverified'` + `T='direct_verified'` | ❌ 被 I7 误拒（合法状态被判非法） | ✅ 允许 | I7 删除 + I7′ |
| C7 | `evidence_id` 两侧 NULL | ❌ 判等为真→通过 | ✅ 拒绝 | 写入断言 + R13（`IS NOT NULL`） |
| C8 | `upstream_id` 填 `x-request-id` 的值 | ❌ 视为合法 | ⚠️ **部分** | `upstream_id_source` 限 `body.id` + 单一写入路径；值本身仍无法证明 |
| C9 | `P='real_llm'` + `T='proxy_unverified'` | ❌ 被 I6 挡（本来就能挡） | ✅ 拒绝 | I6（三轴矛盾） |
| C10 | `P='local_llm'` + `T='direct_verified'` | —（v2 无该值） | ✅ 拒绝 | I8 |
| C11 | `proxy_used='none'` + `proxy_class='loopback'` | — | ✅ 拒绝 | 末条 CHECK |
| C12 | `unverified_reasons='[]'` + `P='real_llm_unverified'` | — | ✅ 拒绝 | I7′ 的 `length>2` |

**结论**：12 类反例中，11 类由 DB 层彻底拒绝；1 类（C8）只能在"来源字段 + 写入路径"层面限制，属**残余风险**，必须由 §10 明示，不得宣称已解决。

---

## 9. readiness 清单（R1–R16，逐项属性）

> 唯一实现必须是**纯合取**：无 `or`、无 `try/except` 吞异常、无默认放行。

| # | 谓词 | 数据来源 | 请求侧/响应侧 | 来自实际传输观测 | 缺失时 | 本地可否伪造 | 影响 readiness |
|---|---|---|---|---|---|---|---|
| R1 | `provenance = 'real_llm'` | 写入时由证据推导 | 结论 | 派生 | 缺失→false | 需同时伪造 R2–R12 | 是 |
| R2 | `endpoint_class = 'remote'` | DNS 解析 + 对端校验 | 目标侧 | ✅ | 缺失→false | 伪造字段可，但需绕过 NOT NULL/枚举 | 是 |
| R3 | `transport_path = 'direct_verified'` | 连接后校验 | 传输侧 | ✅ | 同上 | 同上 | 是 |
| R4 | `http_method = 'POST'` | 请求对象 | **请求侧** | 部分（我方构造） | false | 可（但无意义） | 是 |
| R5 | `request_path` 以 `/chat/completions` 结尾 | 请求 URL | 请求侧 | 部分 | false | 可 | 是 |
| R6 | `http_status = 200` | 响应状态行 | 响应侧 | ✅ | false | 需改库 | 是 |
| R7 | `length(upstream_model) > 0` | 响应体 `model` | 响应侧 | ✅ | false | 需改库 | 是 |
| R8 | `length(envelope_sha256) = 64` | 响应原始字节 sha256 | 响应侧 | ✅ | false | 需改库 | 是 |
| R9 | `envelope_bytes > 0` | 同上 | 响应侧 | ✅ | false | 需改库 | 是 |
| R10 | `peer_ip_global = 1` | `getpeername()` | 传输侧 | ✅ | false（默认 0） | 需改库 | 是 |
| R11 | `proxy_used = 'none'` | 代理检测 | 传输侧 | ✅ | false | 需改库 | 是 |
| R12 | `redirect_count = 0` | 响应状态 | 传输侧 | ✅ | false（默认 0，但 NULL 已被 NOT NULL 挡） | 需改库 | 是 |
| R13 | `evidence_id IS NOT NULL AND = decision.evidence_id` | 两表关联 | 关联 | 派生 | false | 需同时改两表 | 是 |
| R14 | `chain_ok = 1` | 链哈希校验 | 派生 | 派生 | false | 需重算全链 | 是 |
| R15 | `late_write = 0`（`0 ≤ created_at-received_at ≤ 5000ms`） | 两个时间戳 | 派生 | 派生 | false | 可（改时间戳会破坏链） | 是 |
| R16 | `upstream_id IS NOT NULL AND length > 0` | 响应体 `id` | 响应侧 | ✅ | false | 见 C8 残余风险 | 是 |

### 9.1 与方案原文 14 谓词的对应（需求 10）

| 方案编号 | 谓词 | V3 处理 | 原因 |
|---|---|---|---|
| P1 | `provenance='real_llm'` | → R1 保留 | 结论入口 |
| P2 | `endpoint_class='remote'` | → R2 保留 | — |
| P3 | `http_method='POST'` | → R4 保留 | — |
| P4 | `request_path` 后缀 | → R5 保留 | — |
| P5 | `http_status=200` | → R6 保留 | — |
| P6 | `bool(upstream_model)` | → R7 保留（改 `length>0`） | 避免空白串 |
| P7 | `bool(envelope_sha256)` | → R8 **拆分并加强**为 `length=64` | 空串/半截哈希都要挡 |
| P8 | `envelope_bytes>0` | → R9 保留 | — |
| P9 | `peer_ip_global is True` | → R10 保留，**改 `= 1`** | `1 is True` 在 Python 为 False（假阴性陷阱） |
| P10 | `proxy_used='none'` | → R11 保留 | — |
| P11 | `redirect_count=0` | → R12 保留 | — |
| P12 | `evidence_id` 真值 + 跨表相等 | → R13 **拆分**为"非空"+"相等"两个断言 | NULL==NULL 绕过（M-8） |
| P13 | `chain_ok` | → R14 保留 | — |
| P14 | `not late_write` | → R15 保留（量化 5s） | 给出可判定阈值 |
| — | — | **+R3** `transport_path='direct_verified'` | 三轴模型新增（V2 引入） |
| — | — | **+R16** `upstream_id` 非空 | D2 一票否决 |

**14 条 → 16 条**：拆分 2 条（P7、P12 各拆为 1 条但加强）、新增 2 条（R3、R16），净 +2。V2 所称"11 项"**无对应依据，作废**。

---

## 10. 与原批准方案的差异

| 方案原文 | V3 定稿 | 影响 |
|---|---|---|
| `ai_transport_evidence` 无 `transport_path` | 新增（NOT NULL + 5 值枚举） | 表结构 +1 列 |
| 无 `unverified_reasons` | 新增（NOT NULL，默认 `'[]'`） | 表结构 +1 列 |
| 无 `peer_class` / `proxy_class` | 新增（NOT NULL） | 表结构 +2 列 |
| 无 `network_request_id(_source)` | 新增 | 表结构 +2 列 |
| `provenance` 6 值 | 7 值（+`local_llm`） | 枚举变更 |
| I6（方案未命名，仅 3 项） | 10 项合取 | 收紧 |
| readiness 14 谓词 | 16 谓词 | 扩项 |
| `upstream_id` 来源未规定 | 只认 `body.id` | 收紧 |
| 方案 §4.1 `verdict_for` 返回 E 单值 | 返回 E + T + 观测字段 | 接口变更 |

> 方案文件 `3d569f16…` **保持不变**；本文件为其"已确认修订"，实施以 V3 为准。

---

## 11. 未解决问题与残余风险

| # | 风险 | 性质 | 处置 |
|---|---|---|---|
| R-1 | `upstream_id` 的值无法被 DB 层证明"真的来自上游"（C8） | 架构性 | 单一写入路径 + 链哈希 + 上游对账导出；**报告措辞不得称"已证明"** |
| R-2 | 本机代理为回环（`127.0.0.1:54481`），直连验证实际未发生 | 环境性 | V3 下该场景只能是 `proxy_unverified` → 永不通过 readiness，**这是正确结果** |
| R-3 | 中转若不返回 `body.id`，永远不过 readiness | 业务性 | 接受；不得用 `cf-ray` 绕过 |
| R-4 | 本地/私网真实模型永远不过 readiness | 设计选择 | 接受（`local_llm` 明示"非公网"） |
| R-5 | SQLite CHECK 对 NULL 的具体行为需实测验证 | 待验证 | 本轮禁止建库，标记"语义推演"；实施阶段须补一条内存断言 |
| R-6 | 多 IP CDN 与钉死的偶发 fail-closed | 可用性 | 允许"集合内任一"，集合摘要入库 |
| R-7 | 历史行 `transport_path` 为 NULL，永不通过 | 预期 | 与 D4 一致（不追溯认证） |

---

## 12. 只读证明

本轮仅新建本文件；未修改 v2 确认单（`5ecd4b7c…`）、原方案（`3d569f16…`）、审查报告（`9e21e56f…`）、`src/`、`tests/`、`config/`、`.env`、`data/`、`logs/`；未读/写 SQLite；未建库；未执行 SQL；未运行模型请求；未执行 S0/S1；未触碰 Git。

---

## 13. 最终状态

```
V3 文档待用户审核；
方案已批准；
四项决策已确认；
源码实施未授权；
S0 未启动。
```

---

> 本文件 SHA256（本行之前全部字节，可用 `head -n -1 <本文件> | sha256sum` 复算）：`062efa5e21e4a4637c4ccb014983a7d528a59113f7cb35ffcccd260d2dc0926c`
