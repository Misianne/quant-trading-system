# V3.5 修订方案（设计稿 · 不实施）

| 项 | 值 |
|---|---|
| 版本 | **v3.5**（2026-11-11 15:10） |
| 取代 | `US_REAL_LLM_EVIDENCE_V3_4_SCHEME.md`（`8d53f795…`，**不覆盖、不修改**） |
| 依据 | V3.4 第五轮独立审计（FAIL：2×P0 / 3×P1 / 4×P2 / 4×P3）；V3.3/V3.2/V3.1 审计；原修复计划 |
| 性质 | 设计文档。**未执行 SQL、未建库、未做探针、未改源码** |
| 状态 | **V3.5 待人工审核**；未获批；未获实施授权；S0 未启动 |

> 全文所有"预期/应/将"均为**设计预期**，不是实测结果。SQLite 行为未经验证的部分统一列在 §18。

---

## 1. 执行摘要

V3.4 试图用 SQLite 约束 + seal 表证明"真实模型调用"，第五轮审计判定 FAIL，两条 P0 的实质是：

1. **seal 自身可被 REPLACE** —— 防护对象可被同一类操作攻破；
2. **任意 SQL 写者可直接 INSERT 一条全新、字段自洽的假证据** —— seal 只防"改写已有"，对"新增伪造"完全不适用。

V3.5 的立场变化：**不再声称 SQLite 能独立证明真实性**，改为四层分工：

| 层 | 能力 | 归属 |
|---|---|---|
| L1 结构完整性 | 字段/约束/关系/状态合法 | **DB** |
| L2 记录完整性 | 每条证据的规范化消息 HMAC-SHA256 | **APP/OS**（密钥在 DB 之外） |
| L3 签发真实性 | 谁签的、密钥是谁的 | **APP / 独立签名服务** |
| L4 历史不可整体替换 | 定期把链摘要锚定到 DB 之外 | **EXTERNAL** |

`real_llm` 因此**降级为"声明"**；只有 `real_llm_authenticated`（HMAC 验证通过 + 锚定新鲜）才允许对外表述为"已认证的本地真实调用"——且仍不等于"上游真的跑了模型"（§5.3）。

---

## 2. 基线核验与 V3.4 审计问题追踪

### 2.1 基线（本轮实测）

| 文件 | 字节 | 行 | SHA256 | 判定 |
|---|---|---|---|---|
| `US_REAL_LLM_EVIDENCE_V3_4_SCHEME.md` | 57,322 | 1,059 | `8d53f795cf21a75229180e1b817a66b449bf80e18813417e9e73bd1b6bbbdd09` | ✅ 与要求一致 |
| `US_REAL_LLM_EVIDENCE_V3_3_SCHEME.md` | 53,333 | 948 | `1c8ca415823a1550d9f9beb56a26fd1a1727b58afc881aad1ddfdb35f61c494a` | ✅ |
| `US_REAL_LLM_EVIDENCE_V3_2_SCHEME.md` | 27,271 | 441 | `ef743a86a1c2c21b6282834b76084d7ac757e8cde30699a12c63933bef4bf76a` | ✅ |
| `US_REAL_LLM_EVIDENCE_V3_INDEPENDENT_AUDIT.md` | 19,907 | 311 | `138ed16bf71861b140063de89b12150002f1416491804ab9fd3afbc39fb798cf` | ✅ |
| `US_REAL_LLM_EVIDENCE_V2_LOGIC_AUDIT.md` | 22,412 | 313 | `9e21e56f2d959af8646a0ca0225516c20f5e54f7ffac8932509589f8f8d8dab8` | ✅ |
| `US_REAL_LLM_EVIDENCE_REMEDIATION_PLAN.md` | 27,586 | 534 | `3d569f16754e389c77534cbbbfc3e294ad5d6bba67946fb7bb9f3f7fd629610a` | ✅ |

**缺失项（如实记录）**：`US_REAL_LLM_EVIDENCE_V3_3_INDEPENDENT_AUDIT.md` 与 V3.4 审计报告**在磁盘上不存在**（`ls` 报 No such file）。V3.3/V3.4 的审计结论取自任务提供的文本，本文引用其编号时不依赖文件存在。

### 2.2 V3.4 问题追踪

| 来源 | 编号 | 问题 | V3.5 处置 | 章节 |
|---|---|---|---|---|
| V3.4 | **P0-1** | seal 可被 REPLACE，留孤儿证据 | 保留 seal 但**降级为一致性辅助**；新增 HMAC（攻击者无法伪造） | §6/§7/§11 |
| V3.4 | **P0-2** | 任意 SQL 可新增自洽假证据 | HMAC 覆盖全部证据字段 + 决策绑定；无 MAC → 永不 `real_llm_authenticated` | §6/§8 |
| V3.4 | **P1-1** | `v_seal_check` 发现不了孤儿证据 | 新增反向视图 `v_evidence_without_seal` + `v_evidence_auth_facts` | §11.2 |
| V3.4 | **P1-2** | `v_decision_verdict` 不查 seal/认证 | 视图只输出**结构性结论**，永不超过 `real_llm_unverified`；最终结论由应用 `resolve_verdict()` 合成 | §8/§11.3 |
| V3.4 | **P1-3** | 触发器 10/12 不一致 | 全量表格化，15 个逐一列出，正文/表格/统计三处一致 | §11.1 |
| V3.4 | P2-1…P2-4 | seal 无 FK、预封印 DoS、seal 绑定弱、风险披露不全 | 逐项处理（§11.4/§19） | §11.4 |
| V3.4 | P3-1…P3-4 | 计数、CE-45/46 层级、peer_ip | 修正并表 | §16/§17 |
| V3.3 | P1-1…P1-4 | migrated 指针、迁移清理、FK 复位、TLS pin | 继承 V3.4 已修部分，pin 升级为 MAC 绑定 | §10/§13 |
| V3.2 / V3.1 | 全部 | — | 继承，无回退（§21 对照） | §21 |

---

## 3. 设计目标与非目标

**目标**

1. 把"记录结构合法"与"记录已被认证"**彻底分开**，任何输出都不得混淆。
2. 让"新增伪造"（P0-2）在缺少 DB 外密钥时**必然失败**，而不是靠"没有触发器报警"来默认可信。
3. 给出密钥丢失/轮换/服务不可用时的 **fail closed** 路径。
4. 给出**外部锚定**设计，使整体替换/整库回滚可被发现。
5. 威胁模型 T1–T7 逐级说明"能声称什么、不能声称什么"。

**非目标**

1. 不声称 HMAC 能证明"上游真的是那个模型"（只能证明"某次真实 TLS 会话的响应被记录并签名"）。
2. 不声称数据库能独立完成认证（DB 无密钥）。
3. 不声称能抵御被完全控制的应用进程（架构 A）或同 OS 用户（T6）。
4. 不在本阶段执行任何验证。

---

## 4. 可信边界

| 边界 | 内容 | 谁能跨越 |
|---|---|---|
| **B1 DB 结构** | 表/CHECK/触发器/视图 | T3、T4 可改 |
| **B2 DB 数据** | 行内容 | T2 可增/改（受触发器限制） |
| **B3 密钥** | 32B 随机密钥，DPAPI 保护，DB 外 | T5（A 架构）、T6 |
| **B4 签名** | HMAC 由谁计算 | A：应用进程；B：独立签名服务 |
| **B5 网络** | TLS 会话、证书、对端 | 应用/签名服务 |
| **B6 锚定** | DB 外的追加式日志 | 外部存储/服务 |
| **B7 时间** | 单调时钟、锚定时间戳 | OS |

**关键**：`real_llm_authenticated` 需要 B1+B2（结构）+ B3+B4（认证）+ B5（传输）+ B6（锚定新鲜）同时成立。缺任一层即降级，降级路径见 §8.4。

---

## 5. HMAC 设计

### 5.1 十二问逐条回答

| # | 问题 | 设计 |
|---|---|---|
| 1 | 密钥由谁生成 | 应用首次启动时 `os.urandom(32)`（CSPRNG）；**仅生成一次**，写入 DPAPI 包 |
| 2 | 谁持有 | 架构 A：应用进程内存；架构 B：独立签名服务进程内存。**任何情况下不落 DB/配置/日志/证据** |
| 3 | 首次初始化 | `key-init` 子命令（需人工执行一次）：生成 key → `CryptProtectData`（user scope + entropy=`b"fosun-evidence-v1"`）→ 写 `%LOCALAPPDATA%\FosunAI\evidence\key-v1.dpapi` → 打印 `key_id`（非秘密）→ 人工登记 |
| 4 | 如何读取 | 启动时 `CryptUnprotectData` 解密；失败即 fail closed（§5.2） |
| 5 | 重启后如何验证 | 复算 `key_id = sha256(key)[:16]`，与文件名/登记的 `key_id` 比对；不一致 → 拒绝启动 |
| 6 | 丢失/损坏/不可访问 | **fail closed**：拒绝签发；已有证据降级为 `mac_unknown_key`；不伪造、不回退到"无认证"模式 |
| 7 | 轮换 | 新 key 生成 `key-v2.dpapi`，`key_id` 变化；旧 key **保留只读**用于验证历史 |
| 8 | 旧证据如何验证 | 按证据行内的 `key_id` 选择对应密钥；找不到 → `mac_unknown_key` |
| 9 | 轮换是否误判历史 | 不会：验证按 `key_id` 分发，不假设"当前密钥唯一" |
| 10 | 备份/恢复 | 备份 DPAPI blob（仍绑定同一 Windows 用户+entropy）；跨机器/跨用户**不可恢复**（如实记录） |
| 11 | 防日志泄露 | 密钥只在 `KeyHandle` 对象内；`__repr__` 返回 `KeyHandle(key_id=…, secret=<redacted>)`；日志脱敏按 `secrets.is_sensitive_name` 强制 |
| 12 | 多进程/多连接 | A：每进程各自解密（DPAPI 可多次解密）；B：只有签名服务持有，客户端经命名管道请求 |

### 5.2 fail-closed 矩阵

| 情形 | 行为 |
|---|---|
| 密钥文件缺失 | 应用启动失败（退出码 78）；不提供"无认证模式" |
| DPAPI 解密失败 | 同上；记录 `key_unavailable` 到 stderr，不写证据 |
| `key_id` 不匹配 | 同上 |
| 验证期遇到未知 `key_id` | 该行 → `mac_unknown_key`，永不 `real_llm_authenticated` |
| 签名服务不可用（B） | 该轮不产生新证据；已排队请求超时丢弃 |

### 5.3 HMAC 能证明什么（必须写清）

| 命题 | 是否可证 |
|---|---|
| 这条记录自签发以来未被改动 | ✅（前提：密钥未泄露） |
| 这条记录由持有该密钥的组件签发 | ✅ |
| 签发时确实发生过一次 TLS 会话 | ✅ 仅当架构 B（签名服务自己发起的会话）；A 架构下**不能** |
| 上游真的是 `deepseek-v4.1-flash` | ❌（只能证明响应里写了这个名字，见 R-1） |
| 模型真的做了推理 | ❌ |
| 数据库历史未被整体替换 | ❌（需外部锚定，§9） |

---

## 6. 消息规范化与签名（v1）

### 6.1 编码规则

| 项 | 规则 |
|---|---|
| 字符集 | UTF-8，不带 BOM |
| 行分隔 | `\n`（LF），**末尾必须有且仅有一个 `\n`** |
| 字段分隔 | 每行 `name:value`，冒号后无空格 |
| 字段顺序 | 固定顺序，见 §6.2；缺失即视为格式错误 |
| NULL | 编码为单个 `-` |
| 空字符串 | 编码为空（`name:` 后面直接换行）——**与 NULL 严格区分** |
| 整数 | 十进制，无前导零，无正号；范围 `-2^63..2^63-1` |
| 浮点 | **禁止**；所有比率以整数 bps 或定点字符串表示 |
| 时间 | `YYYY-MM-DDTHH:MM:SSZ`（UTC，秒精度） |
| 文本字段 | 原样 UTF-8；**禁止**内含 `\n` 与 `\r`（写入前校验，含则拒绝） |
| 布尔 | `0` / `1` |
| 枚举 | 小写字面量，与 DDL 枚举一致 |
| 摘要算法 | SHA-256（hex 小写 64） |
| HMAC | HMAC-SHA256，输出 hex 小写 64 |

### 6.2 消息体（schema v1）

```
FOSUN-EVIDENCE:v1
key_id:<16hex>
schema_version:v1
decision_id:<text>
seq:<int>
evidence_id:<64hex>
attempt_verdict:<enum>
endpoint_class:<enum>
transport_path:<enum>
peer_role:<enum>
peer_class:<enum>
peer_ip:<text|-> 
peer_ip_global:<0|1>
proxy_used:<none|unknown|env:…|explicit:…>
proxy_class:<enum>
proxy_env_present:<0|1>
http_method:POST
request_path:<text>
http_status:<int>
redirect_count:<int>
upstream_model:<text>
upstream_id:<text|->
upstream_id_source:<text>
network_request_id:<text|->
network_request_id_source:<text>
envelope_sha256:<64hex|->
envelope_bytes:<int>
usage_prompt_tokens:<int>
usage_completion_tokens:<int>
tls_verified:<0|1>
tls_hostname_verified:<0|1>
tls_spki_sha256:<64hex|->
tls_pin_expected:<64hex|->
tls_pin_status:<enum>
pin_verification_status:<enum>
pin_verification_evidence:<64hex|->
received_at:<utc>
unix_received_at:<int>
created_at:<utc>
unix_created_at:<int>
source_declared_by:<enum>
integrity_hash:<64hex>
prev_hash:<64hex|->
```

### 6.3 两个输入/输出示例

**示例 A（正常、全字段）**

输入字段：`key_id=0123456789abcdef`，`decision_id=d-20261111-0001`，`seq=2`，`evidence_id=9f2c…`（64 hex），`attempt_verdict=real_llm`，`endpoint_class=remote`，`transport_path=direct_verified`，`peer_role=upstream`，`peer_class=remote`，`peer_ip=104.18.3.55`，`peer_ip_global=1`，`proxy_used=none`，`proxy_class=none`，`proxy_env_present=0`，`http_status=200`，`request_path=/v1/chat/completions`，`upstream_model=deepseek-v4.1-flash`，`upstream_id=chatcmpl-abc123`，`upstream_id_source=body.id`，`envelope_sha256=3c1f…`，`envelope_bytes=4182`，`usage_prompt_tokens=1188`，`usage_completion_tokens=395`，`tls_verified=1`，`tls_hostname_verified=1`，`tls_spki_sha256=aa…`，`tls_pin_expected=aa…`，`tls_pin_status=matched`，`pin_verification_status=performed_match`，`pin_verification_evidence=bb…`，`unix_received_at=1762833000`，`unix_created_at=1762833001`，`integrity_hash=cc…`，`prev_hash=dd…`，`network_request_id=-`，`source_declared_by=probe`

规范化字节（节选前 6 行与末 3 行）：
```
FOSUN-EVIDENCE:v1
key_id:0123456789abcdef
schema_version:v1
decision_id:d-20261111-0001
seq:2
evidence_id:9f2c…
…
integrity_hash:cc…
prev_hash:dd…
```
→ `evidence_mac = HMAC-SHA256(key, bytes)` = `7b1e…`（64 hex）。
DB 行：`key_id='0123456789abcdef'`, `mac_algorithm='hmac-sha256'`, `evidence_mac='7b1e…'`。

**示例 B（含 NULL 与空串，验证二者不混淆）**

字段：`upstream_id=NULL`（缺 id）、`network_request_id=''`（字段存在但为空）、`prev_hash=NULL`（seq=1）
```
upstream_id:-
network_request_id:
prev_hash:-
```
→ 若把 `upstream_id` 写成 `upstream_id:`（空串）或把 `network_request_id` 写成 `network_request_id:-`，MAC 都会不同；这三个值互不相等，不得互换。

### 6.4 版本与算法演进

| 字段 | 作用 | 兼容规则 |
|---|---|---|
| `schema_version` | 消息体版本（当前 `v1`） | 新增字段必须升版本；验证器必须能按版本选择解析器；未知版本 → `mac_unknown_schema` |
| `key_id` | 密钥标识（`sha256(key)[:16]`） | 验证按 key_id 分发；未知 → `mac_unknown_key` |
| `mac_algorithm` | 当前仅 `hmac-sha256` | 未知算法 → `mac_unsupported_algorithm` |
| `evidence_mac` | 签名值 | 空 → `mac_missing`；长度/hex 不符 → `mac_malformed` |

> 三者必须**同时**参与判定；不得因为 `evidence_mac` 非空就认为已认证。

---

## 7. HMAC 签发架构（A / B）

### 7.1 架构 A：应用进程内签发（默认，阶段一）

```
应用进程 → 加载密钥(DPAPI) → 发起 HTTPS → 收到响应 → 构造规范化消息 → 计算 MAC → 写库
```

| 维度 | 评价 |
|---|---|
| 部署复杂度 | 低（单进程） |
| 密钥暴露面 | 应用进程内存 |
| 抵御 T1/T2 | ✅ T2 无密钥，无法伪造 MAC |
| 抵御 T3 | ✅ 改结构也伪造不出 MAC（无密钥） |
| 抵御 T4 | ✅ 整库替换后 MAC 对不上（新数据无密钥） |
| **抵御 T5（应用被控）** | ❌ **不能**。攻击者可在进程内拿到密钥，为任意内容签名 |
| 恢复能力 | 好（无外部依赖） |

**结论**：A 能挡住"只动数据库"的攻击，**挡不住被控的应用进程**。对外表述必须限定为"数据库层面未被伪造"。

### 7.2 架构 B：独立签名服务（推荐用于"真实调用"声明）

```
应用 → (命名管道) → 签名服务 → 自行发起 TLS/HTTP → 用自己观测的数据构造消息 → 签名 → 返回
```

**关键设计**：签名服务**不接受任意 blob 签名**，只接受"请调用某个 allowlist 端点"的请求，并**自己**完成 HTTP 与解析，因此 MAC 只能对应"签名服务亲自建立的一次会话"。

| 维度 | 评价 |
|---|---|
| 部署复杂度 | 中（额外进程 + 管道 ACL + 服务账号） |
| 密钥暴露面 | 仅签名服务进程 |
| 抵御 T1–T4 | ✅ |
| **抵御 T5（应用被控）** | ✅ **部分**：应用仍可请求"再调一次"，但**每次 MAC 都对应真实会话**；无法为编造内容签名 |
| 残余 | 应用可耗尽配额（DoS/成本）；可指定 allowlist 内的其他端点 |
| 恢复能力 | 服务不可用 → 无法签发（fail closed） |

**IPC 要求（B 必做）**：

| 项 | 设计 |
|---|---|
| 传输 | Windows 命名管道 `\\.\pipe\fosun-evidence-signer`，仅 `BUILTIN\Users` 中指定 SID 可连接（ACL） |
| 身份 | 服务端用 `GetNamedPipeClientProcessId` + 令牌校验调用者 SID |
| 请求格式 | `{"nonce":"<128bit hex>","target":"<allowlist id>","payload_digest":"<64hex>","requested_at":"<utc>"}` |
| 请求绑定 | 服务端只信自己发起的连接；`payload_digest` 必须等于服务端实际发出的 body 摘要，否则拒绝 |
| 重放 | nonce 一次性（内存 LRU 保留 10 min）+ `requested_at` 窗口 ±60s |
| 审计 | 每次签发写一条 `sign_audit`（不含密钥），拒绝也写 |
| 服务失效 | 客户端超时 3s → 本轮不产生证据；不降级为 A |

**不得宣称**：B 在应用被完全控制时仍能保证"内容是应用想要的"。B 保证的是"MAC 必然对应签名服务发起的一次真实会话"。

### 7.3 架构选择

| 场景 | 建议 |
|---|---|
| 本地研究 / 单机 | A（并接受"T5 不可防"） |
| 要对外声称"真实调用" | **B**（A 不足以支撑该声明） |
| 本阶段 | 设计两者，**默认 A**，把 B 列为"若要对外声明的前置条件" |

---

## 8. 验证与 readiness

### 8.1 三层判定（严格分离）

| 层 | 判定者 | 输入 | 输出 |
|---|---|---|---|
| **S1 结构** | DB + 应用 | 行内字段 | `struct_ok`（布尔）+ 违反项清单 |
| **S2 认证** | 应用（持密钥） | 规范化消息 + `evidence_mac` + `key_id` | `mac_status ∈ {ok, missing, malformed, mismatch, unknown_key, unknown_schema, unsupported_algorithm, key_unavailable}` |
| **S3 锚定** | 应用（读外部锚点） | 锚点文件/服务 + 本地链尾 | `anchor_status ∈ {current, stale, missing, mismatch, unavailable}` |

**最终 verdict 由 S1+S2+S3 合成**，DB 视图**永不输出** `real_llm_authenticated`。

### 8.2 状态定义

| 状态 | 条件 | 可否对外称"已认证" |
|---|---|---|
| `real_llm_authenticated` | struct_ok ∧ mac_status=ok ∧ anchor_status=current ∧ claim=real_llm | ✅（限定为"本地已认证"） |
| `real_llm_unverified` | claim=real_llm 但 mac_status∈{missing,unknown_key,…} 或 anchor∈{stale,missing} | ❌ |
| `integrity_violation` | mac_status∈{mismatch,malformed} 或 claim 与证据矛盾 | ❌（且需告警） |
| `legacy_unverified` | `record_origin='migrated'` | ❌（永不追溯） |
| `local_llm` / `stub_transport` / `rejected` / `unavailable` / `unknown` / `voided` | 同 V3.4 语义 | ❌ |

### 8.3 `resolve_verdict()` 伪代码

```python
def resolve_verdict(row, keyring, anchor_state) -> str:
    if row.voided_at:                                  return "voided"
    if row.record_origin == "migrated":                return "legacy_unverified"
    if not struct_ok(row):                             return "integrity_violation"
    if row.provenance != "real_llm":                   return row.provenance

    mac = verify_mac(row, keyring)      # 见 §8.4
    if mac in ("missing", "unknown_key", "unknown_schema", "unsupported_algorithm"):
        return "real_llm_unverified"
    if mac in ("mismatch", "malformed"):               return "integrity_violation"

    if anchor_state.status == "current":               return "real_llm_authenticated"
    if anchor_state.status == "mismatch":              return "integrity_violation"
    return "real_llm_unverified"        # stale / missing / unavailable
```

### 8.4 `verify_mac()` 的失效条件（任一即失败）

| 条件 | 结果 |
|---|---|
| `key_id` 查不到对应密钥 | `unknown_key` |
| `schema_version` 未知 | `unknown_schema` |
| `mac_algorithm` 非 `hmac-sha256` | `unsupported_algorithm` |
| `evidence_mac` 为空 | `missing` |
| `evidence_mac` 非 64 hex | `malformed` |
| 复算值 ≠ 存储值 | `mismatch` |
| 密钥不可用（DPAPI 失败） | `key_unavailable`（全局 fail closed） |
| 行内任一签名覆盖字段被改 | `mismatch`（字段参与消息体） |

### 8.5 认证结果不得缓存进 DB

`mac_status` **不落库**。理由：落库后它本身可被 T2 改写。每次读取时由应用重算。若为性能需要缓存，只能缓存在进程内，并以 `(evidence_id, evidence_mac, key_id, schema_version)` 为键；任一项变化即失效。

---

## 9. 外部锚定

### 9.1 目标

发现 **整库替换 / 整库回滚 / 删除尾部记录** —— 这些 HMAC 单独无法发现（攻击者可把旧库连 MAC 一起搬回）。

### 9.2 设计

| 项 | 设计 |
|---|---|
| 内容 | `(anchor_seq, from_evidence_seq, to_evidence_seq, merkle_root, created_at, prev_anchor_hash, host_id)` |
| 计算 | `merkle_root` 由区间内所有 `evidence_mac`（按 `(decision_id,seq)` 排序）构造 Merkle 树得到 |
| 频率 | 每 5 分钟或每 100 条证据，取先到者 |
| 存储 | **DB 外**：`%PROGRAMDATA%\FosunAI\anchor\anchor-<yyyyMM>.log`（追加写，ACL 仅服务账号可写）；可选远端对象存储/只写 API |
| DB 内镜像 | `ai_anchor_mirror` 表（仅便于本地查询，**不参与判定**） |
| 判定 | 应用读外部日志最后 N 条，与本地区间重算的 `merkle_root` 比对；同时验证 `prev_anchor_hash` 链 |
| 断网 | 本地写 `pending`，恢复到 `stale`；联网后补锚 |
| 丢失 | `anchor_status=missing` → 所有行降级 `real_llm_unverified` |
| 时钟 | 锚点用锚定服务时间；本地时间仅作参考；时间倒退 → `mismatch` |

### 9.3 锚定失败对 readiness 的影响

| anchor_status | real_llm_authenticated | 说明 |
|---|---|---|
| current | ✅ | 新鲜且在容差内 |
| stale | ❌ → unverified | 超时未锚定 |
| missing | ❌ → unverified | 锚点文件缺失 |
| mismatch | ❌ → integrity_violation | 本地链与锚点不符（**强告警**） |
| unavailable | ❌ → unverified | 服务/网络不可用 |

---

## 10. 数据库 DDL（完整）

```sql
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

    CHECK (record_origin <> 'migrated' OR length(migration_batch) > 0),
    CHECK (record_origin <> 'migrated' OR authoritative_evidence_id IS NULL),
    CHECK ((voided_at IS NULL AND void_reason IS NULL)
        OR (voided_at IS NOT NULL AND length(void_reason) > 0)),
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
    peer_ip          TEXT,
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
    pin_verification_status TEXT NOT NULL DEFAULT 'not_performed'
                     CHECK (pin_verification_status IN
                        ('performed_match','performed_mismatch','not_performed','unsupported')),
    pin_verification_evidence TEXT NOT NULL DEFAULT ''
                     CHECK (length(pin_verification_evidence) IN (0,64)
                        AND pin_verification_evidence NOT GLOB '*[^0-9a-f]*'),

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

    -- ===== 认证层（L2/L3）=====
    schema_version   TEXT NOT NULL DEFAULT 'v1' CHECK (schema_version IN ('v1')),
    key_id           TEXT NOT NULL DEFAULT ''
                     CHECK (key_id = '' OR (length(key_id) = 16
                        AND key_id NOT GLOB '*[^0-9a-f]*')),
    mac_algorithm    TEXT NOT NULL DEFAULT 'hmac-sha256'
                     CHECK (mac_algorithm IN ('hmac-sha256')),
    evidence_mac     TEXT NOT NULL DEFAULT ''
                     CHECK (evidence_mac = '' OR (length(evidence_mac) = 64
                        AND evidence_mac NOT GLOB '*[^0-9a-f]*')),
    mac_signed_at    TEXT NOT NULL DEFAULT '',

    UNIQUE (decision_id, seq),
    FOREIGN KEY (decision_id) REFERENCES ai_decisions(decision_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT
        DEFERRABLE INITIALLY DEFERRED,

    -- ===== E 族：real_llm 的 23 项必要条件 =====
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
        AND length(evidence_mac)    = 64                  -- E-22a
        AND length(key_id)          = 16                  -- E-22b
        AND schema_version          = 'v1'                -- E-23
        AND unix_created_at        >= unix_received_at    -- X-03a
        AND unix_created_at - unix_received_at <= 5       -- X-03b
    )),

    -- ===== I 族：24 条一致性 =====
    CHECK (transport_path <> 'local'  OR endpoint_class = 'loopback'),
    CHECK (transport_path <> 'private' OR endpoint_class = 'private'),
    CHECK (transport_path <> 'direct_verified' OR endpoint_class = 'remote'),
    CHECK (transport_path <> 'proxy_unverified' OR (proxy_class IN
             ('loopback','private','remote')
             AND peer_role = 'proxy' AND peer_class = proxy_class)),
    CHECK (transport_path <> 'direct_verified' OR (peer_role = 'upstream'
             AND peer_class = 'remote' AND peer_ip_global = 1)),
    CHECK (transport_path <> 'local' OR (peer_role = 'upstream'
             AND peer_class = 'loopback' AND peer_ip_global = 0)),
    CHECK (transport_path <> 'private' OR (peer_role = 'upstream'
             AND peer_class = 'private' AND peer_ip_global = 0)),
    CHECK (peer_class <> 'remote' OR peer_ip_global = 1),
    CHECK (peer_class NOT IN ('loopback','private') OR peer_ip_global = 0),
    CHECK (proxy_used <> 'none' OR proxy_class = 'none'),
    CHECK (proxy_class <> 'none' OR proxy_used = 'none'),
    CHECK (proxy_used NOT LIKE 'env:%' OR transport_path = 'proxy_unverified'),
    CHECK (proxy_used NOT LIKE 'explicit:%' OR transport_path = 'proxy_unverified'),
    CHECK (transport_path <> 'proxy_unverified'
        OR proxy_used LIKE 'env:%' OR proxy_used LIKE 'explicit:%'),
    CHECK (proxy_used <> 'unknown' OR (transport_path = 'unknown'
             AND attempt_verdict <> 'real_llm')),
    CHECK (attempt_verdict <> 'real_llm_unverified'
        OR json_array_length(unverified_reasons) > 0),
    CHECK (attempt_verdict <> 'local_llm' OR (transport_path IN ('local','private')
             AND endpoint_class IN ('loopback','private')
             AND source_declared_by = 'operator')),
    CHECK (upstream_id IS NULL OR length(upstream_id_source) > 0),
    CHECK (tls_pin_status <> 'matched' OR (tls_pin_expected = tls_spki_sha256
             AND length(tls_pin_expected) = 64)),
    CHECK (proxy_used NOT LIKE 'env:%' OR proxy_env_present = 1),
    CHECK (pin_verification_status <> 'performed_match'
        OR (tls_pin_status = 'matched'
        AND tls_pin_expected = tls_spki_sha256
        AND length(pin_verification_evidence) = 64)),
    CHECK (pin_verification_status <> 'performed_mismatch'
        OR tls_pin_status <> 'matched'),
    CHECK (pin_verification_status <> 'not_performed'
        OR tls_pin_status IN ('not_checked','not_configured')),
    CHECK (length(evidence_mac) = 0 OR length(evidence_mac) = 64)
);

-- =====================================================================
-- 表 3：ai_evidence_seal（仅一致性辅助，不是真实性证明）
-- =====================================================================
CREATE TABLE ai_evidence_seal (
    decision_id  TEXT NOT NULL CHECK (length(decision_id) > 0),
    seq          INTEGER NOT NULL CHECK (seq >= 1),
    evidence_id  TEXT NOT NULL CHECK (length(evidence_id) = 64
                   AND evidence_id NOT GLOB '*[^0-9a-f]*'),
    sealed_at    TEXT NOT NULL CHECK (length(sealed_at) > 0),
    seal_hash    TEXT NOT NULL CHECK (length(seal_hash) = 64
                   AND seal_hash NOT GLOB '*[^0-9a-f]*'),
    PRIMARY KEY (decision_id, seq),
    UNIQUE (evidence_id)
);

-- =====================================================================
-- 表 4：ai_anchor_mirror（本地镜像，仅便于查询；判定用外部日志）
-- =====================================================================
CREATE TABLE ai_anchor_mirror (
    anchor_seq       INTEGER PRIMARY KEY CHECK (anchor_seq >= 1),
    from_evidence_seq INTEGER NOT NULL CHECK (from_evidence_seq >= 0),
    to_evidence_seq   INTEGER NOT NULL CHECK (to_evidence_seq >= from_evidence_seq),
    merkle_root      TEXT NOT NULL CHECK (length(merkle_root) = 64
                       AND merkle_root NOT GLOB '*[^0-9a-f]*'),
    created_at       TEXT NOT NULL CHECK (length(created_at) > 0),
    prev_anchor_hash TEXT NOT NULL DEFAULT ''
                     CHECK (prev_anchor_hash = '' OR (length(prev_anchor_hash) = 64
                        AND prev_anchor_hash NOT GLOB '*[^0-9a-f]*')),
    external_ref     TEXT NOT NULL DEFAULT ''
);

CREATE INDEX ix_ev_decision ON ai_transport_evidence(decision_id, seq);
CREATE INDEX ix_dec_origin   ON ai_decisions(record_origin, provenance);
CREATE INDEX ix_seal_ev      ON ai_evidence_seal(evidence_id);
CREATE INDEX ix_ev_key       ON ai_transport_evidence(key_id);
```

---

## 11. 触发器与视图

### 11.1 触发器全表（**15 个**，正文/表格/统计三处一致）

| # | 名称 | 事件 | 对象 | 用途 |
|---|---|---|---|---|
| 1 | `trg_dec_claim_ins` | BEFORE INSERT | ai_decisions | claim=real_llm 必须有匹配证据 |
| 2 | `trg_dec_claim_upd` | BEFORE UPDATE | ai_decisions | 同上（UPDATE 路径） |
| 3 | `trg_dec_no_delete` | BEFORE DELETE | ai_decisions | 禁物理删除 |
| 4 | `trg_ev_seal_guard_ins` | BEFORE INSERT | ai_transport_evidence | 已封印的槽位/ID 不得再插 |
| 5 | `trg_ev_seal_after_ins` | AFTER INSERT | ai_transport_evidence | 自动写入 seal |
| 6 | `trg_ev_no_update` | BEFORE UPDATE | ai_transport_evidence | 证据不可改 |
| 7 | `trg_ev_no_delete` | BEFORE DELETE | ai_transport_evidence | 证据不可删 |
| 8 | `trg_ev_chain_ins` | BEFORE INSERT | ai_transport_evidence | 链连续性 |
| 9 | `trg_ev_json_ins` | BEFORE INSERT | ai_transport_evidence | reasons 元素白名单 |
| 10 | `trg_ev_mac_shape_ins` | BEFORE INSERT | ai_transport_evidence | MAC 三字段要么全空、要么全齐 |
| 11 | `trg_seal_no_update` | BEFORE UPDATE | ai_evidence_seal | 封不可改 |
| 12 | `trg_seal_no_delete` | BEFORE DELETE | ai_evidence_seal | 封不可删 |
| 13 | `trg_seal_guard_ins` | BEFORE INSERT | ai_evidence_seal | 封不得重复占用 |
| 14 | `trg_anchor_no_update` | BEFORE UPDATE | ai_anchor_mirror | 锚点镜像不可改 |
| 15 | `trg_anchor_no_delete` | BEFORE DELETE | ai_anchor_mirror | 锚点镜像不可删 |

```sql
-- 1
CREATE TRIGGER trg_dec_claim_ins
BEFORE INSERT ON ai_decisions
WHEN NEW.provenance = 'real_llm'
 AND NOT EXISTS (SELECT 1 FROM ai_transport_evidence e
                  WHERE e.evidence_id = NEW.authoritative_evidence_id
                    AND e.decision_id = NEW.decision_id
                    AND e.attempt_verdict = 'real_llm')
BEGIN SELECT RAISE(ABORT, 'real_llm claim without matching evidence'); END;

-- 2
CREATE TRIGGER trg_dec_claim_upd
BEFORE UPDATE ON ai_decisions
WHEN NEW.provenance = 'real_llm'
 AND NOT EXISTS (SELECT 1 FROM ai_transport_evidence e
                  WHERE e.evidence_id = NEW.authoritative_evidence_id
                    AND e.decision_id = NEW.decision_id
                    AND e.attempt_verdict = 'real_llm')
BEGIN SELECT RAISE(ABORT, 'real_llm claim without matching evidence (update)'); END;

-- 3
CREATE TRIGGER trg_dec_no_delete
BEFORE DELETE ON ai_decisions
BEGIN SELECT RAISE(ABORT, 'decisions are append-only'); END;

-- 4
CREATE TRIGGER trg_ev_seal_guard_ins
BEFORE INSERT ON ai_transport_evidence
WHEN EXISTS (SELECT 1 FROM ai_evidence_seal s
              WHERE (s.decision_id = NEW.decision_id AND s.seq = NEW.seq)
                 OR s.evidence_id = NEW.evidence_id)
BEGIN SELECT RAISE(ABORT, 'evidence slot already sealed'); END;

-- 5
CREATE TRIGGER trg_ev_seal_after_ins
AFTER INSERT ON ai_transport_evidence
BEGIN
  INSERT INTO ai_evidence_seal(decision_id, seq, evidence_id, sealed_at, seal_hash)
  VALUES (NEW.decision_id, NEW.seq, NEW.evidence_id, NEW.created_at, NEW.integrity_hash);
END;

-- 6
CREATE TRIGGER trg_ev_no_update
BEFORE UPDATE ON ai_transport_evidence
BEGIN SELECT RAISE(ABORT, 'evidence is append-only'); END;

-- 7
CREATE TRIGGER trg_ev_no_delete
BEFORE DELETE ON ai_transport_evidence
BEGIN SELECT RAISE(ABORT, 'evidence is append-only'); END;

-- 8
CREATE TRIGGER trg_ev_chain_ins
BEFORE INSERT ON ai_transport_evidence
WHEN (NEW.seq = 1 AND NEW.prev_hash <> '')
  OR (NEW.seq > 1 AND NEW.prev_hash <> COALESCE((
        SELECT p.integrity_hash FROM ai_transport_evidence p
         WHERE p.decision_id = NEW.decision_id AND p.seq = NEW.seq - 1), '~'))
BEGIN SELECT RAISE(ABORT, 'chain broken or seq gap'); END;

-- 9
CREATE TRIGGER trg_ev_json_ins
BEFORE INSERT ON ai_transport_evidence
WHEN EXISTS (SELECT 1 FROM json_each(NEW.unverified_reasons) j
              WHERE j.type <> 'text'
                 OR trim(j.value) = ''
                 OR j.value NOT IN (
                    'missing_upstream_id','missing_usage','envelope_digest_failed',
                    'proxy_path','status_not_200','tls_verify_failed','tls_pin_mismatch',
                    'pin_not_performed','mac_missing','mac_invalid','anchor_stale',
                    'model_mismatch','transport_unverified','response_unparsable','other'))
BEGIN SELECT RAISE(ABORT, 'invalid unverified_reasons element'); END;

-- 10：MAC 三字段必须同进同退（要么全空=未签名，要么齐全）
CREATE TRIGGER trg_ev_mac_shape_ins
BEFORE INSERT ON ai_transport_evidence
WHEN (length(NEW.evidence_mac) = 0
      AND (length(NEW.key_id) > 0 OR length(NEW.mac_signed_at) > 0))
   OR (length(NEW.evidence_mac) > 0
      AND (length(NEW.key_id) = 0 OR length(NEW.mac_signed_at) = 0))
BEGIN SELECT RAISE(ABORT, 'mac fields must be all-empty or all-present'); END;

-- 11
CREATE TRIGGER trg_seal_no_update
BEFORE UPDATE ON ai_evidence_seal
BEGIN SELECT RAISE(ABORT, 'seal is append-only'); END;

-- 12
CREATE TRIGGER trg_seal_no_delete
BEFORE DELETE ON ai_evidence_seal
BEGIN SELECT RAISE(ABORT, 'seal is append-only'); END;

-- 13
CREATE TRIGGER trg_seal_guard_ins
BEFORE INSERT ON ai_evidence_seal
WHEN EXISTS (SELECT 1 FROM ai_evidence_seal s
              WHERE (s.decision_id = NEW.decision_id AND s.seq = NEW.seq)
                 OR s.evidence_id = NEW.evidence_id)
BEGIN SELECT RAISE(ABORT, 'seal slot already occupied'); END;

-- 14
CREATE TRIGGER trg_anchor_no_update
BEFORE UPDATE ON ai_anchor_mirror
BEGIN SELECT RAISE(ABORT, 'anchor mirror is append-only'); END;

-- 15
CREATE TRIGGER trg_anchor_no_delete
BEFORE DELETE ON ai_anchor_mirror
BEGIN SELECT RAISE(ABORT, 'anchor mirror is append-only'); END;
```

### 11.2 视图（5 个）

```sql
-- V1：证据事实（不做认证判定）
CREATE VIEW v_evidence_facts AS
SELECT e.*,
       (length(e.evidence_mac) = 64) AS mac_present,
       (length(e.key_id) = 16)       AS key_id_present,
       (SELECT COUNT(*) FROM ai_evidence_seal s
         WHERE s.decision_id = e.decision_id AND s.seq = e.seq) AS seal_rows
  FROM ai_transport_evidence e;

-- V2：以 evidence 为驱动的反向完整性（P1-1 修复）
CREATE VIEW v_evidence_without_seal AS
SELECT e.decision_id, e.seq, e.evidence_id, e.attempt_verdict,
       'missing_seal' AS defect
  FROM ai_transport_evidence e
 WHERE NOT EXISTS (SELECT 1 FROM ai_evidence_seal s
                    WHERE s.decision_id = e.decision_id AND s.seq = e.seq)
UNION ALL
SELECT s.decision_id, s.seq, s.evidence_id, NULL,
       CASE
         WHEN e.evidence_id IS NULL            THEN 'missing_evidence'
         WHEN e.evidence_id <> s.evidence_id    THEN 'id_mismatch'
         WHEN e.integrity_hash <> s.seal_hash   THEN 'hash_mismatch'
       END
  FROM ai_evidence_seal s
  LEFT JOIN ai_transport_evidence e
         ON e.decision_id = s.decision_id AND e.seq = s.seq
 WHERE e.evidence_id IS NULL
    OR e.evidence_id <> s.evidence_id
    OR e.integrity_hash <> s.seal_hash;

-- V3：决策与证据的结构性事实（**永不输出 authenticated**）
CREATE VIEW v_decision_struct AS
SELECT d.decision_id, d.symbol, d.record_origin, d.provenance AS claimed_verdict,
       d.authoritative_evidence_id, d.voided_at,
       e.evidence_id AS resolved_evidence_id,
       e.attempt_verdict AS evidence_verdict,
       e.evidence_mac, e.key_id, e.schema_version, e.mac_algorithm,
       (SELECT COUNT(*) FROM ai_evidence_seal s
         WHERE s.decision_id = e.decision_id AND s.seq = e.seq) AS seal_rows,
       CASE
         WHEN d.voided_at IS NOT NULL                          THEN 'voided'
         WHEN d.record_origin = 'migrated'                     THEN 'legacy_unverified'
         WHEN d.provenance <> 'real_llm'                       THEN d.provenance
         WHEN d.authoritative_evidence_id IS NULL              THEN 'integrity_violation'
         WHEN e.evidence_id IS NULL                            THEN 'integrity_violation'
         WHEN e.decision_id <> d.decision_id                   THEN 'integrity_violation'
         WHEN e.attempt_verdict IS NULL                        THEN 'integrity_violation'
         WHEN e.attempt_verdict <> 'real_llm'                  THEN 'integrity_violation'
         WHEN e.evidence_mac IS NULL OR length(e.evidence_mac) = 0
                                                              THEN 'real_llm_unverified'
         ELSE 'real_llm_unverified'   -- DB 无法验证 MAC，最高只能到未认证
       END AS db_verdict
  FROM ai_decisions d
  LEFT JOIN ai_transport_evidence e
         ON e.evidence_id = d.authoritative_evidence_id;

-- V4：锚点镜像（仅查询）
CREATE VIEW v_anchor_tail AS
SELECT anchor_seq, from_evidence_seq, to_evidence_seq, merkle_root,
       created_at, prev_anchor_hash, external_ref
  FROM ai_anchor_mirror
 ORDER BY anchor_seq DESC
 LIMIT 20;

-- V5：认证缺口汇总（供审计）
CREATE VIEW v_auth_gaps AS
SELECT 'missing_mac' AS gap, COUNT(*) AS n
  FROM ai_transport_evidence WHERE length(evidence_mac) = 0
UNION ALL
SELECT 'unknown_key_id', COUNT(*) FROM ai_transport_evidence
 WHERE length(evidence_mac) > 0 AND length(key_id) = 0
UNION ALL
SELECT 'evidence_without_seal', COUNT(*) FROM v_evidence_without_seal
 WHERE defect = 'missing_seal';
```

> **`db_verdict` 的取值域刻意不含 `real_llm_authenticated`**：SQLite 拿不到密钥，任何"已认证"都必须由应用在验证后生成（§8.3）。

### 11.3 为什么视图不做认证判定（P1-2 的边界说明）

| 事实 | 后果 |
|---|---|
| 视图无法调用 HMAC | 视图只能输出结构性结论 |
| 视图可被 T3 改写 | 应用**不得**把视图输出当作最终结论 |
| `db_verdict` 最高为 `real_llm_unverified` | 任何"已认证"必须由 `resolve_verdict()`（§8.3）产生 |
| 若视图被改写成输出 `real_llm_authenticated` | 应用侧校验器应拒绝：终判不读视图该列，只读原始行 + 自行验证 |

---

## 12. 事务与写入流程

```
预检：PRAGMA foreign_keys=1 且 密钥可用 且 签名服务可用（B）
BEGIN IMMEDIATE
  ① 构造证据（含全部字段）
  ② 计算规范化消息 → HMAC → 写入 evidence_mac/key_id/mac_signed_at/schema_version
  ③ INSERT evidence        （触发器：guard/chain/json/mac_shape）
  ④ INSERT decision        （触发器：claim）
  ⑤ 事务内复核：struct_ok、链连续、seal 一致、MAC 自校验（用同一密钥复算）
  ⑥ 更新锚点镜像（如需，另事务或提交后异步）
COMMIT
提交后：只读审计；锚定异步进行；发现异常只标记，不回滚
```

| 失败点 | 处理 |
|---|---|
| 密钥不可用 | 不进入事务，直接 fail closed |
| 签名失败 | 回滚，不写半条 |
| 证据 INSERT 失败 | 回滚 |
| 决策 INSERT 失败 | 回滚 |
| 事务内 MAC 复算不一致 | 回滚（编译期错误级别） |
| COMMIT 失败 | 不重试写半条 |

---

## 13. 历史迁移

| 步 | 动作 |
|---|---|
| 0 | 只读预检：`integrity_check`、`foreign_key_check`、行数、`COUNT(DISTINCT decision_id,seq)` |
| 1 | 备份 `.bak-<ts>`，记录前后 SHA256 |
| 2 | **事务外** `PRAGMA foreign_keys=OFF` 并断言读回 0 |
| 3 | `BEGIN IMMEDIATE` |
| 4 | 删视图（5 个） |
| 5 | 删触发器（15 个） |
| 6 | 建 4 张 `_new` 表（FK 文本写最终名） |
| 7 | 复制 decisions：`record_origin='migrated'`、`migration_batch`、指针 NULL、claim 原样 |
| 8 | 复制 evidence：**`evidence_mac=''`、`key_id=''`、`mac_signed_at=''`、`schema_version='v1'`** —— 历史行永不带 MAC |
| 9 | 由 `_new` 重建 seal |
| 10 | 校验：行数、内容哈希（排除 MAC 三列）、FK、完整性 |
| 11 | `DROP` 旧表（先子后父） |
| 12 | `RENAME` `_new` → 最终名 |
| 13 | 建索引 → 触发器 → 视图 |
| 14 | `foreign_key_check` / `integrity_check` |
| 15 | 抽样：migrated 行 `db_verdict` 必须为 `legacy_unverified`；`v_auth_gaps.missing_mac` 必须等于迁移行数 |
| 16 | `COMMIT` |
| 17 | `finally`：`PRAGMA foreign_keys=ON` 并读回断言 |

**历史语义**：迁移行 `record_origin='migrated'`、`evidence_mac=''` → 应用 `resolve_verdict()` 首先命中 `legacy_unverified`，**永不认证**；也不允许事后补签（补签会改变 MAC 覆盖字段，且 `trg_ev_no_update` 禁止 UPDATE）。

---

## 14. 回滚

| 层 | 手段 |
|---|---|
| 迁移失败 | `ROLLBACK`（DDL 事务化）；不还原备份 |
| 迁移成功但认证异常 | 不回滚；标记 `legacy_unverified` + 告警；必要时人工决策 |
| 代码回退 | 保留 V3.4 代码分支；DB 向前兼容（新列有 DEFAULT） |
| 密钥丢失 | **不可恢复**；历史证据降级 `mac_unknown_key`；需重新采集并接受历史不可认证 |
| 锚点丢失 | 重建锚点会丢失历史锚定能力；旧锚点日志需保留只读 |

---

## 15. 威胁模型 T1–T7

| 等级 | 能力 | 能做 | 不能做 | 可伪造证据 | 可生成有效 MAC | 可绕过 readiness | 可篡改历史 | 可检测性 | 允许声称 |
|---|---|---|---|---|---|---|---|---|---|
| **T1** 仅调 API | 提交参数 | 让应用记录一条自己给的字段 | 改库、拿密钥 | 否（受 E 族约束） | 否 | 否 | 否 | — | "应用记录的调用" |
| **T2** 任意 DML | INSERT/REPLACE/UPDATE/DELETE | 新增自洽假证据、REPLACE seal | 改结构、拿密钥 | **是（新增）** | **否** | 否（MAC 不过） | 受限（触发器） | MAC 校验可发现 | "未被认证的数据" |
| **T3** 改结构 | DROP TRIGGER/改 CHECK | 绕过所有 DB 约束 | 拿密钥 | 是 | 否 | 否（MAC 不过） | 是（可改历史） | MAC + 锚定可发现 | 同上 |
| **T4** 改文件 | 整库替换/回滚 | 换整库 | 拿密钥（DPAPI 绑定用户） | 是 | 否（新数据无 MAC） | 否 | 是 | 锚定 mismatch 可发现 | 同上 |
| **T5** 应用被控 | 进程内任意代码 | 读内存密钥（A）、签任意内容（A） | 改 OS 其他用户 | **A：是 / B：否** | **A：是 / B：仅真实会话** | A：是 / B：否 | 取决于 DB 权限 | A 无法检测；B 靠 signer 审计 | A：不可声称；B："已认证的真实会话" |
| **T6** OS 用户被控 | 同用户全权 | 解 DPAPI、读密钥 | 跨用户 | 是 | 是 | 是 | 是 | 仅外部锚定 | 不可声称 |
| **T7** 锚定服务被控 | 伪造/拒绝锚点 | 让锚定失效或伪造 | 拿密钥 | 否 | 否 | 可使降级（DoS） | 否（但可掩盖） | 锚点链/单调性 | 不可声称"已被外部见证" |

**结论**：只有在 **B 架构 + T1–T4 未越界 + 锚定 current** 时，系统才可声称"已认证的本地真实会话"。T5(A)/T6/T7 下必须降级。

---

## 16. 反例矩阵（64 条，全部为**设计预期**）

| # | T | 操作 | 预期 | 拦截层 | readiness |
|---|---|---|---|---|---|
| CE-01 | T1 | API claim=real_llm 无证据 | 拒 | `trg_dec_claim_ins` | — |
| CE-02 | T1 | 缺 upstream_id 却 claim real_llm | 拒 | E-15 | — |
| CE-03 | T1 | pin_verification=not_performed + real_llm | 拒 | E-21a | — |
| CE-04 | T1 | 重复 seq | 拒 | UNIQUE(decision_id,seq) | — |
| CE-05 | T1 | migrated + real_llm + 指针非空 | 拒 | M-02 | — |
| CE-06 | T1 | unverified + 空 reasons | 拒 | I-16 | — |
| CE-07 | T1 | proxy unknown + direct_verified | 拒 | I-15 | — |
| CE-08 | T1 | 500 + real_llm | 拒 | E-10 | — |
| CE-09 | T2 | REPLACE evidence 同 id | 拒 | guard | — |
| CE-10 | T2 | REPLACE evidence 同 seq 新 id | 拒 | guard | — |
| CE-11 | T2 | UPDATE evidence | 拒 | `trg_ev_no_update` | — |
| CE-12 | T2 | DELETE evidence | 拒 | `trg_ev_no_delete` | — |
| CE-13 | T2 | UPSERT DO UPDATE | 拒 | guard（BEFORE INSERT 先触发） | — |
| CE-14 | T2 | INSERT OR IGNORE 已占槽位 | 拒（ABORT，非静默忽略） | guard | — |
| CE-15 | T2 | REPLACE seal 同键 | **成功** | 无 | 取决于 MAC |
| CE-16 | T2 | 先 REPLACE seal 再 REPLACE evidence | **成功改证据** | 无（R-9） | MAC 不匹配 → integrity_violation |
| CE-17 | T2 | DELETE seal | 拒 | `trg_seal_no_delete` | — |
| CE-18 | T2 | UPDATE seal | 拒 | `trg_seal_no_update` | — |
| CE-19 | T2 | **新增自洽假证据（无 MAC）** | 插入成功 | — | `real_llm_unverified` |
| CE-20 | T2 | **新增假证据 + 随机 64hex MAC** | 插入成功 | — | `integrity_violation`（MAC 不符） |
| CE-21 | T2 | 复制他人有效 MAC 到新证据 | 插入成功 | 无（DB 不验 MAC） | `integrity_violation` |
| CE-22 | T2 | 跨决策复制 MAC | 同上 | 无 | `integrity_violation`（decision_id 入消息） |
| CE-23 | T2 | 跨 seq 复制 MAC | 同上 | 无 | `integrity_violation` |
| CE-24 | T2 | 改 key_id 为未知值 | 插入成功 | 无 | `real_llm_unverified`（unknown_key） |
| CE-25 | T2 | 改任一签名字段但保留 MAC | 插入成功 | 无 | `integrity_violation` |
| CE-26 | T2 | 删除决策保留证据 | 拒 | `trg_dec_no_delete` | — |
| CE-27 | T2 | 清空指针 + claim real_llm | 拒 | M-04 | — |
| CE-28 | T2 | 预占 seal 制造 DoS | 成功占位 | 无（P2-2） | 后续写入失败 |
| CE-29 | T2 | 孤儿证据（decision 不存在） | COMMIT 失败 | 延迟 FK | — |
| CE-30 | T2 | 空 evidence_mac + key_id 非空 | 拒 | `trg_ev_mac_shape_ins` | — |
| CE-31 | T3 | DROP 触发器后 UPDATE 证据 | 成功改库 | 无 | `integrity_violation`（MAC 不符） |
| CE-32 | T3 | DROP 触发器后伪造 + 随机 MAC | 成功 | 无 | `integrity_violation` |
| CE-33 | T3 | 重建表并复制旧 MAC | 成功 | 无 | `integrity_violation` |
| CE-34 | T3 | 删除 `v_decision_struct` | 视图缺失 | — | 应用拒绝启动（fail closed） |
| CE-35 | T3 | 伪造视图输出 authenticated | 视图可被改 | 无 | 应用**不得**信任视图终判（§11.3） |
| CE-36 | T3 | FK OFF + 悬空指针 | 成功 | 无 | `integrity_violation`（证据缺失） |
| CE-37 | T3 | 整库重建、无旧 MAC | 成功 | 无 | `mac_missing` → unverified |
| CE-38 | T3 | 改 schema_version 为 v9 | 被 CHECK 拒 | CHECK | — |
| CE-39 | T3 | 改 mac_algorithm 为 md5 | 被 CHECK 拒 | CHECK | — |
| CE-40 | T3 | 篡改锚点镜像 | 拒 | `trg_anchor_no_update` | — |
| CE-41 | T4 | 整库替换为伪造库 | 成功替换 | 无 | MAC 不符 + 锚点 mismatch |
| CE-42 | T4 | 整库替换 + 复制旧 MAC | 成功 | 无 | 锚点 mismatch（§9） |
| CE-43 | T4 | 回滚到旧备份 | 成功 | 无 | 锚点链断裂 → mismatch |
| CE-44 | T4 | 删除外部锚点日志 | 成功 | 无 | `anchor missing` → unverified |
| CE-45 | T4 | 篡改外部锚点日志 | 取决于 ACL | 无 | 锚点链校验失败 |
| CE-46 | T4 | 替换 DPAPI 密钥文件 | 成功替换 | 无 | 解密失败 → 全库 fail closed |
| CE-47 | T4 | 把他人机器的密钥文件拷来 | 成功拷贝 | DPAPI 用户绑定 | 解密失败 → fail closed |
| CE-48 | T4 | 系统时间回拨 | 成功 | 无 | 锚点时间倒退 → mismatch |
| CE-49 | T5 | A 架构：应用伪造上下文并签名 | **成功**，MAC 有效 | 无 | `real_llm_authenticated`（**误报**） |
| CE-50 | T5 | A 架构：内存 dump 取密钥 | 成功 | 无 | 可签任意内容 |
| CE-51 | T5 | B 架构：请求签任意 blob | 拒 | signer 拒绝（只签自己发起的会话） | — |
| CE-52 | T5 | B 架构：请求非 allowlist 端点 | 拒 | signer allowlist | — |
| CE-53 | T5 | B 架构：重放同一请求 | 拒 | nonce + 时间窗 | — |
| CE-54 | T5 | B 架构：伪造客户端连管道 | 拒 | 管道 ACL + 进程令牌 | — |
| CE-55 | T5 | B 架构：服务不可用 | 不产生证据 | 超时 3s | `auth_service_unavailable` → unverified |
| CE-56 | T6 | 同用户解 DPAPI | **成功取密钥** | 无 | 一切可伪造 |
| CE-57 | T6 | 提权到 SYSTEM | 成功取密钥 | 无 | 一切可伪造 |
| CE-58 | T6 | 替换 DPAPI master key | 失败或需旧密码 | DPAPI | 解密失败 → fail closed |
| CE-59 | T6 | 改外部锚点 ACL 后写入 | 取决于配置 | OS ACL | 锚定不可信 |
| CE-60 | T7 | 锚定服务返回假 root | 成功 | 无 | 本地比对失败 → mismatch |
| CE-61 | T7 | 锚定服务拒绝服务 | 成功 | 无 | `stale/unavailable` → unverified |
| CE-62 | T7 | 重放旧锚点记录 | 成功 | 无 | 单调性检查失败 |
| CE-63 | T7 | 长期断网 | 本地 pending | 无 | `stale` → unverified |
| CE-64 | 通用 | reasons 元素为对象/空白 | 拒 | `trg_ev_json_ins` | — |

> 计数：**64 条**（要求 ≥60）。全部为设计预期，未执行。

---

## 17. 正向场景（26 条）

| # | 场景 | 期望最终状态 | 依赖 |
|---|---|---|---|
| PS-01 | 直连 + 全 E + MAC 有效 + 锚定 current | `real_llm_authenticated` | A/B 均可 |
| PS-02 | 同上但欠 anchor（stale） | `real_llm_unverified` | 锚定 |
| PS-03 | 同上但 MAC 缺失 | `real_llm_unverified` | — |
| PS-04 | 同上但 key_id 未知（轮换后旧 key 未加载） | `real_llm_unverified` | 密钥 |
| PS-05 | 密钥轮换后：旧证据用旧 key 验证 | `real_llm_authenticated` | 多 key |
| PS-06 | 密钥轮换后：新证据用新 key | `real_llm_authenticated` | 多 key |
| PS-07 | migrated 历史行 | `legacy_unverified` | — |
| PS-08 | migrated 行尝试补签 | 拒（UPDATE 被封） | — |
| PS-09 | 多证据 seq1=rejected, seq2=real_llm，指针指 seq2 | `real_llm_authenticated` | — |
| PS-10 | 作废（voided） | `voided` | — |
| PS-11 | 回环代理 | `real_llm_unverified` | — |
| PS-12 | 本地模型 | `local_llm` | — |
| PS-13 | 私网模型 | `local_llm` | — |
| PS-14 | 测试桩 | `stub_transport` | — |
| PS-15 | 无凭据未发起 | `unavailable` | — |
| PS-16 | DNS 失败 | `rejected` | — |
| PS-17 | HTTP 500 + model 非空 | `rejected` | — |
| PS-18 | 200 缺 upstream_id | `real_llm_unverified` | — |
| PS-19 | pin mismatched | `rejected`（E-20a） | — |
| PS-20 | B 架构正常签发 | `real_llm_authenticated` | signer |
| PS-21 | B 架构服务重启后恢复 | 继续可签 | signer |
| PS-22 | 锚定恢复（stale → current） | 状态回升至 authenticated | 锚定 |
| PS-23 | 重启后重新验证全部证据 | 结论与重启前一致 | 密钥 |
| PS-24 | 并发两进程写不同 decision | 均成功（WAL + IMMEDIATE 重试） | DB |
| PS-25 | 迁移中断后 ROLLBACK | 库回到迁移前 | DB |
| PS-26 | `v_auth_gaps` 显示 missing_mac=迁移行数 | 一致 | — |

> 计数：**26 条**（要求 ≥25）。全部为设计预期。

---

## 18. SQLite 未验证语义（本轮一项都未验证）

| # | 待测 | 影响 |
|---|---|---|
| 1 | `INSERT OR REPLACE` 是否跳过 DELETE 触发器（`recursive_triggers` 默认值） | R-9 严重度 |
| 2 | BEFORE INSERT 中 `RAISE(ABORT)` 是否回滚 REPLACE 已做的删除 | CE-09/10 |
| 3 | 循环 FK + 双 `DEFERRABLE INITIALLY DEFERRED` 能否建表并提交 | §12 |
| 4 | `AFTER INSERT` 内 INSERT 失败是否回滚外层插入 | 封印原子性 |
| 5 | `PRAGMA foreign_keys` 在事务内是否 no-op | §13 步骤 2 |
| 6 | `ALTER TABLE RENAME` 是否改写他表 FK 文本 | §13 步骤 12 |
| 7 | `json_type()` 非法 JSON 是报错还是 NULL | CE-64 |
| 8 | 多 BEFORE INSERT 触发器执行顺序 | 若 guard 晚于 CHECK 则行为不同 |
| 9 | `LEFT JOIN` 在 `UNION ALL` 视图中的 NULL 行为 | §11.2 V2 |
| 10 | TEXT PK + 显式 NOT NULL 的 NULL 语义 | CE-08 类 |
| 11 | `INSERT OR IGNORE` 遇 `RAISE(ABORT)` 的实际行为 | CE-14 |
| 12 | `foreign_key_check` 是否可见未提交数据 | §13 步骤 10 |
| 13 | WAL 下两进程并发写的行为与重试语义 | PS-24 |
| 14 | `v_evidence_without_seal` 在 UNION ALL 下的类型亲和性 | 视图可用性 |

---

## 19. 残余风险

| # | 风险 | 归属 | 披露 |
|---|---|---|---|
| R-1 | `upstream_id` 值真实性 | 上游对账 | 永不可证 |
| R-2 | `integrity_hash` 算法正确性 | 应用 | DB 无 sha256 |
| R-3 | 任意 SQL 权限者改结构 | DB 边界 | MAC + 锚定可发现 |
| R-4 | `local_llm` 是否真是模型 | 运维声明 | 不认证 |
| R-5 | 代理路径上上游身份 | 设计上限 | 只能 unverified |
| R-6 | 备份还原覆盖新数据 | 运维 | 备份不作回滚 |
| R-7 | `unix_received_at` 真实性 | 应用 | 无法外部验证 |
| R-8 | 模型名非空 ≠ 真推理 | 语义 | 必要非充分 |
| **R-9** | seal 可被 REPLACE；证据可被替换后 MAC 不符 | DB | **可检测但不可阻止** |
| **R-10** | `pin_verification_status` 为应用声明 | 应用 | DB 无法验证握手 |
| **R-11** | 决策行可被 REPLACE 重写（claim 仍受触发器约束） | DB | 需锚定才能发现 |
| **R-12** | **A 架构下应用被控可签任意内容** | 架构 | 唯一对策是 B |
| **R-13** | **DPAPI 绑定 Windows 用户**：同用户被控即失守 | OS | T6 无法防 |
| **R-14** | 锚点日志本地可被删/改（取决于 ACL） | 部署 | 需远端只写存储 |
| **R-15** | 密钥丢失 = 历史永久不可认证 | 运维 | 不可恢复 |

---

## 20. 实施前置条件

| # | 条件 | 不满足时的替代 |
|---|---|---|
| 1 | 确定架构 A 或 B（B 需额外交付签名服务） | 只能声称"数据库未被伪造" |
| 2 | 完成 `key-init` 并登记 key_id | 无法签发，系统只读 |
| 3 | DPAPI 可用（Windows 用户配置完整） | fail closed |
| 4 | 锚点目录存在且 ACL 正确 | 所有结论停留 unverified |
| 5 | 14 项 SQLite 语义探针全部实跑 | 不得声称"数据库强制" |
| 6 | 迁移脚本在独立副本上演练通过 | 不得对生产库执行 |

---

## 21. 版本差异与交叉引用

| 项 | V3.4 | V3.5 |
|---|---|---|
| 真实性声明 | 试图用 DB 证明 | **明确 DB 不能证明**，改为 MAC + 锚定 |
| 新增证据伪造（P0-2） | 无法阻止也不易发现 | 无 MAC → 永不 authenticated |
| seal 定位 | 被当作防伪造 | 降级为一致性辅助（§11） |
| 触发器 | 12 个，标题写 10 | 15 个，表格逐一列出 |
| 视图 | 3 个 | 5 个（含反向孤儿视图、结构视图） |
| 最终结论 | 视图直接给 real_llm | 视图最高 unverified，终判在应用 |
| 威胁模型 | T1–T4 | T1–T7 |
| 反例/正例 | 54 / 23 | 64 / 26 |
| 历史数据 | legacy 标注 | 永不补签（§13） |
| 时间 | `unix_*` | 沿用，并入 MAC 消息 |

---

## 22. 自检表（不构成审计）

| # | 检查 | 结果 |
|---|---|---|
| C-01 | 是否声称 SQLite 能独立证明真实性 | ✅ 否（§1/§5.3） |
| C-02 | P0-1 seal 是否降级 | ✅ §11 标题与 §9 表均写明 |
| C-03 | P0-2 是否被 MAC 覆盖 | ✅ E-22 + §8.4 |
| C-04 | P1-1 反向视图是否存在 | ✅ `v_evidence_without_seal` |
| C-05 | P1-2 视图是否不再输出 real_llm | ✅ `db_verdict` 最高 unverified |
| C-06 | P1-3 触发器数量 | ✅ 15，表 + 统计 + 正文一致 |
| C-07 | HMAC 12 问是否逐条回答 | ✅ §5.1 |
| C-08 | 规范化是否给出示例 | ✅ §6.3 两例 |
| C-09 | A/B 架构边界 | ✅ §7，A 不抵御 T5 |
| C-10 | 锚定是否独立于 DB | ✅ §9 |
| C-11 | 反例数量 | ✅ 64 |
| C-12 | 正例数量 | ✅ 26 |
| C-13 | 是否有"略/同前" | ✅ 无 |
| C-14 | 是否宣称已实测 | ✅ 全文标注设计预期 |

---

## 23. 交付状态

```
V3.5 设计文件已生成，待人工审核；
方案未获批；
未获实施授权；
未执行 SQL；未创建数据库；未运行探针；未跑迁移；
S0 未启动。
```

本文件自检表不构成审计结论。

---

> 本文件 SHA256（本行之前全部字节，可用 `head -n -1 <本文件> | sha256sum` 复算）：`b72480720f71c2ad555ac32a9037385d41ffebdc8ee152b48c80d6ac201e48c9`
