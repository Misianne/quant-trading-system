# V3.6 修订方案（设计稿 · 不实施）

| 项 | 值 |
|---|---|
| 版本 | **v3.6**（2026-11-11 15:30） |
| 取代 | `US_REAL_LLM_EVIDENCE_V3_5_SCHEME.md`（`3f308b17…`，**不覆盖、不修改**） |
| 依据 | V3.5 第六轮独立审计（FAIL：1×P0 / 5×P1 / 8×P2 / 3×P3） |
| 性质 | 设计文档。未执行 SQL、未建库、未跑探针、未改源码 |
| 状态 | **V3.6 待独立审计**；未获批；未获实施授权；S0 未启动 |

> 全文"应/预期/将"均为**设计预期**，不是实测。SQLite 行为未验证项统一列于 §20。

---

## 1. 执行摘要

V3.5 把"结构合法"和"已认证"分开了，但第六轮审计指出：默认 A 架构仍能产出与 B **同名同值**的认证状态（P0-1），且 HMAC 只覆盖响应侧、不覆盖请求侧（P1-1），会话与全局序号缺失（P1-2/P1-3），`struct_ok` 未定义（P1-4），状态命名越权（P1-5）。

V3.6 的六条硬性变化：

| # | 变化 | 解决 |
|---|---|---|
| 1 | **认证等级由密钥槽位推导，不由字段声明**：`key_id → slot ∈ {app, signer}` 的映射在 DB 之外的 keyring 元数据里；数据库里的 `signing_architecture` 只作缓存，与推导不符即 `integrity_violation` | P0-1 |
| 2 | **两套状态命名空间**：`app_record_*` 与 `upstream_session_*`，A 永不产出后者 | P0-1 / P1-5 |
| 3 | **HMAC 覆盖请求与响应双侧共 64 个字段**，含 `request_body_digest` / `target_host` / `model_params_digest` | P1-1 |
| 4 | **会话表 + 状态机 + 服务端分配 session_id/nonce/decision_id 绑定**，一会话一证据 | P1-2 |
| 5 | **全局单调序号**（计数器表 + 事务内分配），锚定按其切片 | P1-3 |
| 6 | **`struct_ok` 12 项确定性清单** | P1-4 |

---

## 2. V3.5 审计问题追踪

| 来源 | 编号 | 原问题 | V3.6 修复 | 闭环 |
|---|---|---|---|---|
| V3.5 | **P0-1** | A 可产出与 B 同名的 `real_llm_authenticated` | §4（槽位推导）+ §9（状态命名空间）+ §13 DDL（`signing_architecture` 受 `key_slot` 约束） | ✅ |
| V3.5 | **P1-1** | MAC 未覆盖请求侧 | §5（消息 v2，52 字段，含 request_body_digest/target_host/model_params） | ✅ |
| V3.5 | **P1-2** | decision_id 归属不清、无一会话一签 | §6（会话状态机 + 服务端分配 + 唯一约束） | ✅ |
| V3.5 | **P1-3** | 锚定引用不存在的全局序号 | §9（`global_evidence_seq` 计数器分配）+ §10（Merkle 规约） | ✅ |
| V3.5 | **P1-4** | `struct_ok` 未定义 | §11（12 项清单 + 伪代码 + 失败原因枚举） | ✅ |
| V3.5 | **P1-5** | 状态名越权 | §9（`upstream_session_authenticated` 仅证明会话；显式禁止 model/inference 命名） | ✅ |
| V3.5 | P2-1 | CE-19 与 E-22 冲突 | §18 CE-19 改为"INSERT 即拒"，并新增 CE-77 覆盖原意 | ✅ |
| V3.5 | P2-2 | PS-03 不可达 | §19 删除该场景，替换为 PS-31（unknown key） | ✅ |
| V3.5 | P2-3 | `unverified_reasons` 未入 MAC | §5 纳入消息体（字段 44） | ✅ |
| V3.5 | P2-4 | `integrity_hash` 前像未定义 | §5.4 定义 | ✅ |
| V3.5 | P2-5 | IPC 缺速率/大小/allowlist 规范 | §8（结构化 IPC 规格） | ✅ |
| V3.5 | P2-6 | B 下应用可反复请求真实调用 | §6（`UNIQUE(decision_id)` + 会话一次性）+ §8（限流） | 部分（成本型 DoS 仍存，R-16） |
| V3.5 | P2-7 | 迁移后新列 DEFAULT 与 E-22 交互未明示 | §16 步骤 8 明确"历史行 MAC 恒空" | ✅ |
| V3.5 | P2-8 | 多 key 目录选择顺序未定义 | §5.1（按 key_id 精确选择，禁止"取最新"） | ✅ |
| V3.5 | P3-1 | 章节数 23 vs 建议 21 | 本文件 23 章，与建议一致 | ✅ |
| V3.5 | P3-2 | CE 表拦截层多处"—" | §18 全部填实 | ✅ |
| V3.5 | P3-3 | "B：否"对可伪造证据表述过绝对 | §17 T5/B 行改为"可请求真实调用，不可伪造内容" | ✅ |

---

## 3. 目标与非目标

**目标**

1. A/B 认证等级**结构上不可混淆**，A 无法通过任何字段组合获得 B 的状态。
2. HMAC 同时绑定**请求字节与响应字节**，且由**发送/接收方**计算摘要。
3. 会话一次性、decision 一次性、签名一次性。
4. 全局序号存在且单调，锚定可复算。
5. 判定函数（`struct_ok`、`resolve_verdict`）给出**确定性**结果，不同实现一致。
6. B 架构的网络侧约束（SSRF/DNS/代理/重定向/限额）可实施。

**非目标**

- 不证明上游模型身份、不证明推理发生、不证明输出语义正确（§9.4）。
- 不抵御 T5（A）、T6、T7。
- 不在本阶段验证任何 SQLite 行为。

---

## 4. A / B 认证等级（P0-1 核心）

### 4.1 槽位由密钥决定，不由字段决定

```
keyring 元数据（DB 之外，OS 保护）：
  key_id → { slot: "app" | "signer", created_at, owner_sid, purpose }
```

| 判定 | 规则 |
|---|---|
| 记录属于哪个等级 | `slot = keyring[key_id].slot`，**只读 keyring** |
| DB 里的 `signing_architecture` | 仅作缓存；与 slot 不符 → `integrity_violation` |
| A 能否产出 B 状态 | **不能**：A 的 key_id 映射到 `slot=app`，B 状态要求 `slot=signer` |
| A 复制 B 的 key_id | 能写进去，但 MAC 是用 A 的密钥算的 → 验证失败 |
| A 用 B 的 key 签名 | 拿不到 B 的密钥（§5.1 DPAPI 分离账户/服务） |

### 4.2 两个等级的能力与命名

| 等级 | 状态 | 证明什么 | 不证明什么 |
|---|---|---|---|
| **A**（应用内） | `app_record_authenticated` | 该记录自写入后未被改动；由本机应用密钥签发 | 真实网络会话、上游身份、模型推理、请求内容未被替换 |
| **B**（签名服务） | `upstream_session_authenticated` | 独立服务**亲自**发起并完成一次受约束的 TLS/HTTP 会话，请求与响应字节被绑定并签名 | 上游就是指定模型、模型做了推理、无缓存/网关改写、输出正确 |

**硬约束**：A 的输出**永不**进入对外"真实模型调用"统计；报表必须分列 `app_record_*` 与 `upstream_session_*`，禁止合并为一个"已认证真实调用"数字。

### 4.3 等级切换不可由应用触发

`signing_architecture` 只能由两件事决定：① 实际使用哪个 `key_id` 签名；② keyring 中该 key 的 slot。
普通应用**不得**通过修改 `provenance`、`signing_architecture`、`verification_status`、`key_id` 提升等级 —— 任何一处改动都会破坏 MAC 或与 keyring 冲突，结果只能是 `integrity_violation`。

---

## 5. HMAC 规范 v2（P1-1 核心）

### 5.1 密钥与槽位

| 项 | app 槽 | signer 槽 |
|---|---|---|
| 密钥生成 | `os.urandom(32)`，`key-init --slot app` | `key-init --slot signer`，在签名服务账户下执行 |
| 保护 | DPAPI（user scope + entropy `fosun-evidence-app-v1`） | DPAPI（**服务账户** scope + entropy `fosun-evidence-signer-v1`） |
| 存放 | `%LOCALAPPDATA%\FosunAI\evidence\app\key-<key_id>.dpapi` | `%PROGRAMDATA%\FosunAI\evidence\signer\key-<key_id>.dpapi`（ACL：仅服务账户） |
| keyring 元数据 | `keys.json`（**不含密钥**，仅 `key_id/slot/owner_sid/created_at`） | 同上 |
| 选择规则 | 按记录中的 `key_id` **精确匹配**；禁止"取最新" | 同 |

> 应用进程即便被控（T5），也只能拿到 `slot=app` 的密钥；signer 的密钥在另一个账户的 DPAPI 下，跨账户不可解（R-13 仍适用于"攻击者已控制该服务账户"）。

### 5.2 谁计算什么（P1-1 的关键）

| 动作 | A 架构 | B 架构 |
|---|---|---|
| 构造请求字节 | 应用 | **签名服务**（应用只给"endpoint_id + 结构化参数"） |
| 发送请求 | 应用 | **签名服务** |
| 计算 `request_body_digest` | 应用（可信度低） | **签名服务对自己发出的字节计算** |
| 接收响应字节 | 应用 | **签名服务** |
| 计算 `response_body_digest` | 应用 | **签名服务** |
| 计算 MAC | 应用 | **签名服务** |

B 的 IPC 中，应用提交的是**参数**而非字节；服务自己序列化、自己算摘要、自己发送。因此"应用在摘要计算前替换请求"在 B 下不成立。

### 5.3 消息体（schema v2，**64 个字段 + 1 行头**）

```
FOSUN-EVIDENCE:v2
01 key_id:
02 schema_version:v2
03 signing_architecture:
04 signer_identity:
05 session_id:
06 request_id:
07 decision_id:
08 seq:
09 evidence_id:
10 global_evidence_seq:
11 request_nonce:
12 request_timestamp:
13 endpoint_id:
14 target_scheme:
15 target_host:
16 target_port:
17 request_path:
18 http_method:
19 request_encoding:
20 request_body_digest:
21 request_body_bytes:
22 model_requested:
23 model_params_digest:
24 temperature_bps:
25 max_tokens:
26 stream_flag:
27 dns_resolved_digest:
28 dns_resolved_count:
29 peer_ip:
30 peer_ip_global:
31 peer_class:
32 peer_role:
33 tls_sni:
34 tls_verified:
35 tls_hostname_verified:
36 tls_cert_sha256:
37 tls_spki_sha256:
38 tls_pin_expected:
39 tls_pin_status:
40 pin_verification_status:
41 pin_verification_evidence:
42 proxy_mode:
43 proxy_endpoint_digest:
44 proxy_peer_ip:
45 proxy_env_present:
46 http_status:
47 resp_headers_digest:
48 response_body_digest:
49 response_body_bytes:
50 upstream_response_id:
51 upstream_response_id_source:
52 upstream_model:
53 usage_prompt_tokens:
54 usage_completion_tokens:
55 redirect_count:
56 redirect_chain_digest:
57 received_at:
58 unix_received_at:
59 attempt_verdict:
60 endpoint_class:
61 transport_path:
62 unverified_reasons_digest:
63 integrity_hash:
64 prev_hash:
```

> 编号 01–64 为字段行号，全部参与 HMAC；另有 1 行头 `FOSUN-EVIDENCE:v2`，合计 65 行。§5.5 给出逐类计数，合计必须等于 64。

### 5.4 `integrity_hash` 前像（P2-4 修复）

```
integrity_hash = sha256(
  "FOSUN-CHAIN:v1\n" +
  decision_id + "\n" + seq + "\n" + evidence_id + "\n" +
  response_body_digest + "\n" + request_body_digest + "\n" +
  received_at + "\n" + key_id + "\n"
)
```
该值是**行内自哈希**，与 `prev_hash` 共同构成链；`prev_hash` 必须等于同决策上一条的 `integrity_hash`。

### 5.5 覆盖计数（逐类）

| 类别 | 字段数 | 清单 |
|---|---|---|
| 身份与会话 | 10 | key_id, schema_version, signing_architecture, signer_identity, session_id, request_id, decision_id, seq |
| 请求侧 | 16 | endpoint_id, target_scheme, target_host, target_port, request_path, http_method, request_encoding, request_body_digest, request_body_bytes, model_requested, model_params_digest, temperature_bps, max_tokens |
| 传输观测 | 19 | stream_flag, dns_resolved_digest, dns_resolved_count, peer_ip, peer_ip_global, peer_class, peer_role, tls_sni, tls_verified, tls_hostname_verified, tls_cert_sha256, tls_spki_sha256 |
| TLS pin | 0（已并入传输观测） | tls_pin_expected, tls_pin_status, pin_verification_status, pin_verification_evidence, proxy_env_present |
| 代理 | 0（已并入传输观测） | proxy_mode, proxy_endpoint_digest, proxy_peer_ip |
| 响应侧 | 11 | http_status, resp_headers_digest, response_body_digest, response_body_bytes, upstream_response_id, upstream_response_id_source, upstream_model, usage_prompt_tokens, usage_completion_tokens |
| 时间 | 2 | redirect_count, redirect_chain_digest, received_at, unix_received_at, global_evidence_seq |
| 结论与链 | 6 | attempt_verdict, endpoint_class, transport_path, unverified_reasons_digest, integrity_hash |
| **合计** | **64** | 与 §5.3 的 64 个字段一一对应 |

> 64 个字段全部参与 HMAC。全文口径统一为 **"64 字段入 MAC"**，不再出现 52/60 等旧数字。

### 5.6 编码规则（沿用并补强 v1）

| 项 | 规则 |
|---|---|
| 字符集 | UTF-8，无 BOM |
| 行分隔 | LF，末尾恰好一个 `\n` |
| 字段顺序 | 严格按 §5.3 行号，不得增删 |
| NULL | `-` |
| 空串 | 空（`name:` 后直接换行） |
| 整数 | 十进制，无前导零/正号 |
| 浮点 | 禁止（温度用 bps 整数） |
| 时间 | `YYYY-MM-DDTHH:MM:SSZ` |
| 文本 | 原样 UTF-8，禁含 `\n`/`\r`；超长按 §8 上限拒绝 |
| 摘要 | SHA-256，hex 小写 |
| HMAC | HMAC-SHA256，hex 小写 |

### 5.7 两个示例

**示例 1（B 架构，全字段）**：`key_id=7f3a…`（slot=signer）、`session_id=s-20261111-0001`、`decision_id=d-0001`、`request_body_digest=11aa…`、`response_body_digest=22bb…`、`target_host=api.example.com`、`peer_ip=104.18.3.55`、`upstream_model=deepseek-v4.1-flash`。
规范化前 5 行：
```
FOSUN-EVIDENCE:v2
key_id:7f3a…
schema_version:v2
signing_architecture:signer_service
signer_identity:NT SERVICE\FosunSigner@v1
```
→ `evidence_mac = HMAC-SHA256(signer_key, bytes) = 9c4d…`。

**示例 2（A 架构，且请求 body 被替换的检测）**：`key_id=1a2b…`（slot=app），其余相同但 `request_body_digest=33cc…`。
攻击者把 `request_body_digest` 改成 `44dd…` 而保留 MAC → 复算不符 → `integrity_violation`。若攻击者用 app 密钥重签，则 `key_id` 仍是 app 槽 → 状态只能是 `app_record_authenticated`，**无法**变成 `upstream_session_authenticated`。

### 5.8 版本兼容

| 字段 | 规则 |
|---|---|
| `schema_version` | 当前 `v2`；`v1` 记录（历史）一律 `legacy_unverified`，**不升级、不补签** |
| `key_id` | 按精确匹配选密钥；未知 → `mac_unknown_key` |
| `mac_algorithm` | 仅 `hmac-sha256`；未知 → `mac_unsupported_algorithm` |
| `signing_architecture` | 缓存值，与 keyring 槽位不符 → `integrity_violation` |

---

## 6. 会话生命周期（P1-2 核心）

### 6.1 状态机

```
created → request_bound → sent → response_received → signed → committed
   ↓            ↓           ↓            ↓              ↓
rejected     failed      failed      failed      persistence_failed
   ↓
expired / abandoned
```

| 状态 | 含义 | 可终止 |
|---|---|---|
| `created` | 服务分配 session_id/nonce，绑定 decision_id | 否 |
| `request_bound` | 请求字节已由服务序列化并计算摘要 | 否 |
| `sent` | 已建立 TLS 并发出 | 否 |
| `response_received` | 已收完整响应字节 | 否 |
| `signed` | MAC 已计算（尚未落库） | 否 |
| `committed` | 证据已写入并提交 | **终态** |
| `persistence_failed` | 落库失败 | **终态**（不得重签） |
| `rejected` / `failed` / `expired` / `abandoned` | 各类失败 | 终态 |

### 6.2 一次性约束

| 约束 | 实现 |
|---|---|
| session_id 由服务生成 | 服务端 `uuid4` |
| nonce 由服务生成 | 服务端 128bit，`UNIQUE(nonce)` |
| decision_id 在**创建时**绑定 | `ai_sign_session.decision_id UNIQUE`，创建后不可改 |
| 一会话最多一份证据 | `evidence.session_id UNIQUE` + `session.evidence_id UNIQUE` |
| 一响应不能签给多个 decision | `decision_id` 已绑定，且 `response_digest` 在会话内唯一 |
| 同一 session 不得重复签名 | 状态机无 `signed → signed` 迁移 |
| 重放请求被拒 | nonce 一次性 + `request_timestamp` 窗口 ±60s |
| 服务重启后旧会话失效 | 启动时把所有非终态会话置 `abandoned` |
| DB 提交失败不得重签 | 状态置 `persistence_failed`，无出边 |

### 6.3 约束要点（DDL 唯一权威定义见 §13，此处不重复）

| 约束 | 落点 |
|---|---|
| `session_id` 主键 | `ai_sign_session.session_id` PK |
| `decision_id` 一决策一会话 | `UNIQUE(decision_id)` |
| `nonce` 一次性 | `UNIQUE(nonce)` |
| `evidence_id` 一证据一会话 | `UNIQUE(evidence_id)` |
| 状态取值域 | `CHECK (state IN (...))` 11 态 |
| `committed` 必须有证据 | `CHECK (state<>'committed' OR evidence_id IS NOT NULL)` |
| `signed` 必须有请求与响应摘要 | `CHECK (state<>'signed' OR (request_digest IS NOT NULL AND response_digest IS NOT NULL))` |
| 迁移白名单 | `ai_session_transition(from_state,to_state)` + `trg_session_transition` |

### 6.4 崩溃恢复

| 场景 | 处理 |
|---|---|
| 服务在 `sent` 崩溃 | 重启后该会话 → `abandoned`；不重发、不补签 |
| 在 `signed` 后、落库前崩溃 | → `abandoned`；该会话不产生证据（宁可缺失，不可重复） |
| 落库事务失败 | → `persistence_failed`；**禁止**回到 `signed` 重试 |
| 应用重试 | 必须新建 session（新 nonce、新 decision_id） |

---

## 7. B 架构网络安全边界（重点）

### 7.1 目标地址

| 约束 | 规则 |
|---|---|
| endpoint | 只接受 `endpoint_id`，指向 `ai_endpoint_allowlist` 中的行；**不接受 URL** |
| allowlist 管理 | 仅部署时由管理员写入；应用只读；`trg_allowlist_no_update/delete` 保护 |
| scheme/port | allowlist 固定（https/443）；禁止 http |
| path | 只允许 allowlist 中的 `path_prefix` 前缀 |
| method | 固定 `POST` |
| Content-Type | 固定 `application/json` |

### 7.2 DNS 与 IP

| 约束 | 规则 |
|---|---|
| 解析 | 服务解析 `target_host`，取全部 A/AAAA |
| 全部校验 | 每个 IP 必须 `is_global`；任一非全局 → 拒绝 |
| 禁止段 | loopback、RFC1918、link-local(169.254/16，含 169.254.169.254)、multicast、unspecified、CGNAT(100.64/10)、保留段、IPv6 ULA(fc00::/7) |
| 实际连接 | 只连解析集合中的 IP（钉死）；连接后 `getpeername()` 必须 ∈ 集合 |
| DNS rebinding | 解析 → 连接 → 复核 `getpeername()`；不一致 → 拒绝 |
| SNI | 用 `target_host`，不用 IP |
| 证书 | 以 `target_host` 校验主机名；记录 `tls_cert_sha256` 与 `tls_spki_sha256` |
| Pin | 与 allowlist 中的期望值比较，结果入 MAC |

### 7.3 代理

| 项 | 规则 |
|---|---|
| 继承环境变量 | **禁止**（`trust_env=False` 等价语义） |
| 默认 | 直连；若部署要求代理，必须显式配置 `proxy_mode='explicit'` + allowlist 代理地址 |
| 观测 | `proxy_peer_ip` 必须记录；`proxy_mode='none'` 时该字段必须为空 |
| loopback 代理 | 视为 `proxy_mode='explicit'` 且 `transport_path='proxy_unverified'`，**不得**记 direct |
| 不可验证 | `proxy_mode='unknown'` → `upstream_session_unverified` |

### 7.4 重定向

| 项 | 规则 |
|---|---|
| 默认 | **禁止**（`allow_redirects=False`） |
| 若启用 | 逐跳重新校验 endpoint/allowlist/DNS/IP/TLS/代理，禁止跳向私网 |
| 记录 | `redirect_count` + `redirect_chain_digest` 入 MAC |
| 上限 | 3 跳 |

### 7.5 限额（建议默认值，未经实测）

| 项 | 默认 | 依据 |
|---|---|---|
| 请求体 | 256 KiB | 提示词 + 120 根 K 线约 40–80 KiB，留 3× |
| 响应体 | 1 MiB | 单次 JSON 决策响应通常 < 20 KiB |
| 响应头 | 32 KiB | 常见上限 |
| 连接超时 | 10 s | 与既有 provider 一致 |
| 读取超时 | 120 s | 推理可能较慢 |
| 重定向跳数 | 3 | — |
| 单调用者并发 | 2 | 单机研究用途 |
| 单调用者速率 | 10 次/分钟 | 防刷 |
| 全局速率 | 60 次/分钟 | 成本保护 |
| 会话 TTL | 300 s | 超时 → `expired` |

### 7.6 HTTP 安全

- 响应不得跨会话复用（`response_digest` 在会话内唯一，跨会话出现同 digest → 标记 `replay_suspected`）。
- 非 2xx 响应**不得**产生 `upstream_session_authenticated`（只能 `rejected`）。
- HTTP 200 **不**等于模型身份或推理真实（§9.4 再次强调）。
- 禁止自动重试（避免一次决策产生两次调用）。

---

## 8. IPC 安全（结构化）

| 项 | 规格 |
|---|---|
| 通道 | `\\.\pipe\FosunAI.Evidence.Signer.v1` |
| DACL | 仅 `NT SERVICE\FosunSigner`（所有者）与 `BUILTIN\Users` 中**指定 SID** 可 `FILE_READ_DATA|FILE_WRITE_DATA`；其他拒绝 |
| 调用者校验 | `GetNamedPipeClientProcessId` → `OpenProcessToken` → 校验 token 的 SID ∈ 白名单 |
| 消息上限 | 64 KiB（结构化参数，不含响应） |
| 消息格式 | `{"op":"call","endpoint_id":…,"decision_id":…,"params":{…},"client_nonce":…}` |
| nonce | 服务端生成 `session_nonce`；客户端 `client_nonce` 仅用于去重 |
| 重放 | `(client_nonce)` 在 10 min 内一次性；服务端 `session_nonce` 一次性 |
| 频率 | 每 SID 10 次/分钟，超限 `rejected(rate_limited)` |
| 模型参数 | 服务按 allowlist 中该 endpoint 的 `allowed_models` 校验 `model_requested` |
| 配置 | 服务配置只读（`%PROGRAMDATA%` ACL），应用不可写 |
| 审计 | 每次调用写 `ai_sign_session` + 独立 `signer-audit.log`（不含密钥） |
| 重启 | 非终态会话置 `abandoned`；旧 nonce 不再可用 |

---

## 9. 全局证据序号与状态命名

### 9.1 全局序号（P1-3 核心）

计数器表 `ai_seq_counter(id=1, next_seq)` 的 **DDL 唯一定义在 §13**（此处不重复）。
分配（写入事务内，`BEGIN IMMEDIATE` 串行化）：

```sql
UPDATE ai_seq_counter SET next_seq = next_seq + 1 WHERE id = 1;
SELECT next_seq FROM ai_seq_counter;      -- 作为本行 global_evidence_seq
```

| 性质 | 结论 |
|---|---|
| 单调性 | 每次 +1 |
| 空洞 | **不会**：计数器更新与插入同事务，回滚一起回滚 |
| 并发 | `BEGIN IMMEDIATE` 取写锁；第二个写者等待/重试 |
| 崩溃 | 未提交 → 全部回滚，序号不前进 |
| 历史记录 | 迁移时按 `(decision_id, seq)` 排序依次分配 1..N，`record_origin='migrated'` |
| 新旧区分 | `record_origin` + `schema_version`（历史为 `v1`） |
| 重复 | `UNIQUE(global_evidence_seq)` + 触发器校验等于计数器值 |

### 9.2 状态命名空间（P0-1 / P1-5）

| 状态 | 等级 | 条件 |
|---|---|---|
| `app_record_authenticated` | A | slot=app ∧ MAC ok ∧ struct_ok |
| `app_record_unverified` | A | slot=app 但 MAC 缺失/未知/锚定不新鲜 |
| `upstream_session_authenticated` | B | slot=signer ∧ MAC ok ∧ struct_ok ∧ 会话 committed ∧ 锚定 current ∧ session 各项匹配 |
| `upstream_session_unverified` | B | slot=signer 但锚定不新鲜/会话信息不全 |
| `legacy_unverified` | — | `record_origin='migrated'` |
| `integrity_violation` | — | MAC 不符/字段被改/槽位不符 |
| `local_llm` / `stub` / `rejected` / `unavailable` / `unknown` / `voided` | — | 同前 |

**禁用命名**：`model_identity_verified`、`inference_proven`、`real_llm_authenticated`。理由：三者都超出了可证范围（§9.4）。

### 9.3 判定顺序（固定）

```
1 voided → 2 migrated → 3 struct_ok 失败 → 4 claim≠real_llm → 5 槽位判定
6 MAC 验证 → 7 锚定 → 8 会话完整性（B 才有）→ 输出
```

### 9.4 明确不可证

| 命题 | 可证 |
|---|---|
| 上游就是 `deepseek-v4.1-flash` | ❌ |
| 模型执行了推理 | ❌ |
| 输出语义正确 | ❌ |
| 无缓存/网关改写 | ❌ |
| 该次会话真实发生（B） | ✅ |
| 请求/响应字节未被替换（B） | ✅ |
| 记录自签发未被改动 | ✅ |

---

## 10. Merkle 锚定

### 10.1 叶子与排序

| 项 | 规则 |
|---|---|
| 叶子集合 | 区间内全部证据行 |
| 排序 | 按 `global_evidence_seq` 升序（唯一、稳定） |
| 叶子编码 | `leaf_i = sha256( "FOSUN-LEAF:v1\n" + global_evidence_seq(十进制) + "\n" + evidence_mac )` |
| 空区间 | `root = sha256("FOSUN-EMPTY:v1")` |
| 奇数叶 | 复制最后一个叶子（`leaf_n = leaf_{n-1}`） |
| 内部节点 | `sha256( "FOSUN-NODE:v1\n" + left + right )`（均为 64 hex 小写） |
| 根编码 | hex 小写 64 |

### 10.2 锚点字段

| 字段 | 含义 |
|---|---|
| `anchor_start_seq` / `anchor_end_seq` | 全局序号闭区间 |
| `anchor_count` | 叶子数（= end − start + 1；空区间为 0） |
| `anchor_root` | Merkle root |
| `previous_anchor_root` | 上一条锚点的 root（链） |
| `anchor_timestamp` | 锚点生成时刻（UTC） |
| `anchor_key_id` | 锚点签名密钥（与证据密钥分开） |
| `anchor_signature` | `HMAC(anchor_key, 规范化锚点行)` |

### 10.3 复算流程

1. 读外部锚点日志最后 N 条；
2. 对每条：`SELECT global_evidence_seq, evidence_mac FROM ... WHERE seq BETWEEN start AND end ORDER BY global_evidence_seq`；
3. 按 §10.1 重建树，比较 `anchor_root`；
4. 校验 `previous_anchor_root` 与本条前一条的 `anchor_root` 相等；
5. 校验 `anchor_signature`；
6. 任一不符 → `anchor_status='mismatch'`。

---

## 11. `struct_ok`（P1-4）

### 11.1 12 项判定（全部为布尔，任一 false 即 `struct_ok=false`）

| # | 检查 | 层 |
|---|---|---|
| S-01 | `schema_version` 属于已知集合（`v1`/`v2`） | APP |
| S-02 | 所有 NOT NULL 字段非空（DB 已保证，APP 复核读回值） | DB+APP |
| S-03 | 枚举字段取值在允许集合内 | DB |
| S-04 | `unverified_reasons` 为合法 JSON 数组且元素在白名单内 | DB+APP |
| S-05 | `evidence.decision_id` 存在且 `ai_decisions.authoritative_evidence_id` 指向它 | DB+APP |
| S-06 | `evidence.decision_id = decision.decision_id` | APP |
| S-07 | seal 行存在且 `seal_hash = evidence.integrity_hash` | DB（视图）+APP |
| S-08 | `prev_hash` 等于同决策上一条的 `integrity_hash` | APP |
| S-09 | `global_evidence_seq` 唯一且与相邻行连续（允许迁移起点前不连续） | APP |
| S-10 | `record_origin='migrated'` 的行不带 MAC 且 `schema_version='v1'` | DB+APP |
| S-11 | 无孤儿证据（每条 evidence 都有 seal 与 decision） | APP（用视图） |
| S-12 | 会话完整性（B）：`session_id` 存在、`state='committed'`、`decision_id`/`evidence_id` 匹配 | APP |

### 11.2 输出契约

```
输入：一行 evidence + 其 decision + seal + session（可缺）
输出：(ok: bool, failed: list[str])   # failed 为 S-01…S-12 的子集
异常：任何读取异常 → (False, ["S-00_read_error"])
```

**不同实现必须给出相同 `failed` 集合**；顺序按 S 编号升序。

---

## 12. readiness

```python
def resolve_verdict(row, decision, keyring, anchor, session=None):
    if decision.voided_at: return "voided"
    if decision.record_origin == "migrated": return "legacy_unverified"
    ok, failed = struct_ok(row, decision, session)
    if not ok: return "integrity_violation"
    if decision.provenance != "real_llm": return decision.provenance

    slot = keyring.slot_of(row.key_id)          # 唯一等级来源
    if slot is None: return "app_record_unverified"     # 未知 key
    if row.signing_architecture != ("app_in_process" if slot=="app" else "signer_service"):
        return "integrity_violation"            # 缓存与槽位不符

    mac = verify_mac(row, keyring)
    if mac in ("missing","malformed","mismatch"): return "integrity_violation"
    if mac in ("unknown_key","unknown_schema","unsupported_algorithm"):
        return ("app_record_unverified" if slot=="app" else "upstream_session_unverified")

    if slot == "app":
        return "app_record_authenticated"        # 锚定不参与 A 的等级
    # slot == signer
    if session is None or session.state != "committed": return "upstream_session_unverified"
    if anchor.status != "current": return "upstream_session_unverified" if anchor.status!="mismatch" else "integrity_violation"
    return "upstream_session_authenticated"
```

| 规则 | 说明 |
|---|---|
| A 与锚定 | A 的等级不依赖锚定（它本就不声称会话） |
| B 与锚定 | B 必须 `anchor_status='current'` |
| 同 evidence 缓存的验证结果 | 只能进程内缓存，键为 `(evidence_id, evidence_mac, key_id, schema_version)` |
| 任一变化 | 缓存失效 |

---

## 13. 完整 DDL

```sql
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- 1) 允许的 endpoint
CREATE TABLE ai_endpoint_allowlist (
    endpoint_id   TEXT PRIMARY KEY NOT NULL CHECK (length(endpoint_id) > 0),
    scheme        TEXT NOT NULL CHECK (scheme = 'https'),
    host          TEXT NOT NULL CHECK (length(host) > 0),
    port          INTEGER NOT NULL CHECK (port BETWEEN 1 AND 65535),
    path_prefix   TEXT NOT NULL CHECK (length(path_prefix) > 0),
    allowed_models TEXT NOT NULL DEFAULT '[]'
                   CHECK (json_valid(allowed_models) AND json_type(allowed_models)='array'),
    pin_expected  TEXT NOT NULL DEFAULT ''
                  CHECK (length(pin_expected) IN (0,64)
                     AND pin_expected NOT GLOB '*[^0-9a-f]*'),
    enabled       INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1))
);

-- 2) 会话
CREATE TABLE ai_sign_session (
    session_id      TEXT PRIMARY KEY NOT NULL CHECK (length(session_id) > 0),
    decision_id     TEXT NOT NULL UNIQUE,
    signer_identity TEXT NOT NULL CHECK (length(signer_identity) > 0),
    key_id          TEXT NOT NULL CHECK (length(key_id) = 16
                      AND key_id NOT GLOB '*[^0-9a-f]*'),
    nonce           TEXT NOT NULL UNIQUE CHECK (length(nonce) = 32
                      AND nonce NOT GLOB '*[^0-9a-f]*'),
    state           TEXT NOT NULL CHECK (state IN
        ('created','request_bound','sent','response_received','signed','committed',
         'persistence_failed','rejected','failed','expired','abandoned')),
    created_at      TEXT NOT NULL CHECK (length(created_at) > 0),
    expires_at      TEXT NOT NULL CHECK (length(expires_at) > 0),
    request_digest  TEXT,
    response_digest TEXT,
    evidence_id     TEXT UNIQUE,
    closed_reason   TEXT,
    CHECK (state <> 'committed' OR evidence_id IS NOT NULL),
    CHECK (state <> 'signed' OR (request_digest IS NOT NULL AND response_digest IS NOT NULL))
);

CREATE TABLE ai_session_transition (
    from_state TEXT NOT NULL,
    to_state   TEXT NOT NULL,
    PRIMARY KEY (from_state, to_state)
);

-- 3) 序号计数器
CREATE TABLE ai_seq_counter (
    id       INTEGER PRIMARY KEY CHECK (id = 1),
    next_seq INTEGER NOT NULL CHECK (next_seq >= 1)
);
INSERT INTO ai_seq_counter(id, next_seq) VALUES (1, 1);

-- 4) 决策
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
        ON DELETE RESTRICT ON UPDATE RESTRICT DEFERRABLE INITIALLY DEFERRED
);

-- 5) 证据
CREATE TABLE ai_transport_evidence (
    evidence_id     TEXT PRIMARY KEY NOT NULL
                    CHECK (length(evidence_id)=64 AND evidence_id NOT GLOB '*[^0-9a-f]*'),
    decision_id     TEXT NOT NULL CHECK (length(decision_id) > 0),
    seq             INTEGER NOT NULL CHECK (seq >= 1),
    global_evidence_seq INTEGER NOT NULL UNIQUE CHECK (global_evidence_seq >= 1),

    -- 身份与会话
    session_id      TEXT,
    request_id      TEXT,
    signing_architecture TEXT NOT NULL CHECK (signing_architecture IN
                      ('app_in_process','signer_service')),
    signer_identity TEXT NOT NULL DEFAULT '',
    key_id          TEXT NOT NULL CHECK (length(key_id) IN (0,16)
                      AND key_id NOT GLOB '*[^0-9a-f]*'),

    -- 请求侧
    endpoint_id     TEXT,
    target_scheme   TEXT CHECK (target_scheme IN ('https')),
    target_host     TEXT CHECK (target_host IS NULL OR length(target_host) > 0),
    target_port     INTEGER CHECK (target_port IS NULL OR target_port BETWEEN 1 AND 65535),
    request_path    TEXT NOT NULL CHECK (length(request_path) > 0),
    http_method     TEXT NOT NULL CHECK (http_method = 'POST'),
    request_encoding TEXT NOT NULL DEFAULT 'utf-8' CHECK (request_encoding = 'utf-8'),
    request_body_digest TEXT NOT NULL DEFAULT ''
                    CHECK (request_body_digest='' OR (length(request_body_digest)=64
                       AND request_body_digest NOT GLOB '*[^0-9a-f]*')),
    request_body_bytes INTEGER NOT NULL DEFAULT 0 CHECK (request_body_bytes >= 0),
    model_requested TEXT NOT NULL DEFAULT '',
    model_params_digest TEXT NOT NULL DEFAULT ''
                    CHECK (model_params_digest='' OR (length(model_params_digest)=64
                       AND model_params_digest NOT GLOB '*[^0-9a-f]*')),
    temperature_bps INTEGER NOT NULL DEFAULT 0 CHECK (temperature_bps BETWEEN 0 AND 20000),
    max_tokens      INTEGER NOT NULL DEFAULT 0 CHECK (max_tokens >= 0),
    stream_flag     INTEGER NOT NULL DEFAULT 0 CHECK (stream_flag IN (0,1)),
    request_nonce   TEXT,
    request_timestamp TEXT,

    -- 传输观测
    endpoint_class  TEXT NOT NULL CHECK (endpoint_class IN
                      ('remote','loopback','private','unknown')),
    transport_path  TEXT NOT NULL CHECK (transport_path IN
                      ('direct_verified','proxy_unverified','local','private','unknown')),
    dns_resolved_digest TEXT NOT NULL DEFAULT ''
                    CHECK (dns_resolved_digest='' OR (length(dns_resolved_digest)=64
                       AND dns_resolved_digest NOT GLOB '*[^0-9a-f]*')),
    dns_resolved_count INTEGER NOT NULL DEFAULT 0 CHECK (dns_resolved_count >= 0),
    peer_ip         TEXT,
    peer_class      TEXT NOT NULL CHECK (peer_class IN ('remote','loopback','private','unknown')),
    peer_role       TEXT NOT NULL CHECK (peer_role IN ('upstream','proxy','unknown')),
    peer_ip_global  INTEGER NOT NULL DEFAULT 0 CHECK (peer_ip_global IN (0,1)),
    tls_sni         TEXT,
    tls_verified    INTEGER NOT NULL DEFAULT 0 CHECK (tls_verified IN (0,1)),
    tls_hostname_verified INTEGER NOT NULL DEFAULT 0 CHECK (tls_hostname_verified IN (0,1)),
    tls_cert_sha256 TEXT NOT NULL DEFAULT ''
                    CHECK (tls_cert_sha256='' OR (length(tls_cert_sha256)=64
                       AND tls_cert_sha256 NOT GLOB '*[^0-9a-f]*')),
    tls_spki_sha256 TEXT NOT NULL DEFAULT ''
                    CHECK (tls_spki_sha256='' OR (length(tls_spki_sha256)=64
                       AND tls_spki_sha256 NOT GLOB '*[^0-9a-f]*')),
    tls_pin_expected TEXT NOT NULL DEFAULT ''
                    CHECK (tls_pin_expected='' OR (length(tls_pin_expected)=64
                       AND tls_pin_expected NOT GLOB '*[^0-9a-f]*')),
    tls_pin_status  TEXT NOT NULL DEFAULT 'not_checked'
                    CHECK (tls_pin_status IN ('not_configured','matched','mismatched','not_checked')),
    pin_verification_status TEXT NOT NULL DEFAULT 'not_performed'
                    CHECK (pin_verification_status IN
                      ('performed_match','performed_mismatch','not_performed','unsupported')),
    pin_verification_evidence TEXT NOT NULL DEFAULT ''
                    CHECK (pin_verification_evidence='' OR (length(pin_verification_evidence)=64
                       AND pin_verification_evidence NOT GLOB '*[^0-9a-f]*')),
    proxy_mode      TEXT NOT NULL DEFAULT 'none'
                    CHECK (proxy_mode IN ('none','env','explicit','unknown')),
    proxy_endpoint_digest TEXT NOT NULL DEFAULT ''
                    CHECK (proxy_endpoint_digest='' OR (length(proxy_endpoint_digest)=64
                       AND proxy_endpoint_digest NOT GLOB '*[^0-9a-f]*')),
    proxy_peer_ip   TEXT,
    proxy_env_present INTEGER NOT NULL DEFAULT 0 CHECK (proxy_env_present IN (0,1)),

    -- 响应侧
    http_status     INTEGER NOT NULL CHECK (http_status BETWEEN 0 AND 599),
    resp_headers_digest TEXT NOT NULL DEFAULT ''
                    CHECK (resp_headers_digest='' OR (length(resp_headers_digest)=64
                       AND resp_headers_digest NOT GLOB '*[^0-9a-f]*')),
    response_body_digest TEXT NOT NULL DEFAULT ''
                    CHECK (response_body_digest='' OR (length(response_body_digest)=64
                       AND response_body_digest NOT GLOB '*[^0-9a-f]*')),
    response_body_bytes INTEGER NOT NULL DEFAULT 0 CHECK (response_body_bytes >= 0),
    upstream_response_id TEXT,
    upstream_response_id_source TEXT NOT NULL DEFAULT ''
                    CHECK (upstream_response_id_source IN ('','body.id')),
    upstream_model  TEXT NOT NULL DEFAULT '',
    usage_prompt_tokens INTEGER NOT NULL DEFAULT 0 CHECK (usage_prompt_tokens >= 0),
    usage_completion_tokens INTEGER NOT NULL DEFAULT 0 CHECK (usage_completion_tokens >= 0),
    redirect_count  INTEGER NOT NULL DEFAULT 0 CHECK (redirect_count BETWEEN 0 AND 3),
    redirect_chain_digest TEXT NOT NULL DEFAULT ''
                    CHECK (redirect_chain_digest='' OR (length(redirect_chain_digest)=64
                       AND redirect_chain_digest NOT GLOB '*[^0-9a-f]*')),

    -- 时间
    received_at     TEXT NOT NULL CHECK (length(received_at) > 0),
    unix_received_at INTEGER NOT NULL CHECK (unix_received_at > 0),
    created_at      TEXT NOT NULL CHECK (length(created_at) > 0),
    unix_created_at INTEGER NOT NULL CHECK (unix_created_at > 0),

    -- 结论
    attempt_verdict TEXT NOT NULL CHECK (attempt_verdict IN
                      ('real_llm','real_llm_unverified','local_llm',
                       'stub_transport','rejected','unavailable','unknown')),
    unverified_reasons TEXT NOT NULL DEFAULT '[]'
                    CHECK (json_valid(unverified_reasons) AND json_type(unverified_reasons)='array'),
    source_declared_by TEXT NOT NULL DEFAULT 'probe'
                    CHECK (source_declared_by IN ('probe','operator')),

    -- 认证
    schema_version  TEXT NOT NULL DEFAULT 'v2' CHECK (schema_version IN ('v1','v2')),
    mac_algorithm   TEXT NOT NULL DEFAULT 'hmac-sha256' CHECK (mac_algorithm = 'hmac-sha256'),
    evidence_mac    TEXT NOT NULL DEFAULT ''
                    CHECK (evidence_mac='' OR (length(evidence_mac)=64
                       AND evidence_mac NOT GLOB '*[^0-9a-f]*')),
    mac_signed_at   TEXT NOT NULL DEFAULT '',
    integrity_hash  TEXT NOT NULL CHECK (length(integrity_hash)=64
                       AND integrity_hash NOT GLOB '*[^0-9a-f]*'),
    prev_hash       TEXT NOT NULL DEFAULT ''
                    CHECK (prev_hash='' OR (length(prev_hash)=64
                       AND prev_hash NOT GLOB '*[^0-9a-f]*')),

    UNIQUE (decision_id, seq),
    FOREIGN KEY (decision_id) REFERENCES ai_decisions(decision_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT DEFERRABLE INITIALLY DEFERRED,

    -- ===== E 族：real_llm 必要条件（28 项）=====
    CHECK (attempt_verdict <> 'real_llm' OR (
            transport_path='direct_verified' AND endpoint_class='remote'      -- E-01/02
        AND peer_role='upstream' AND peer_class='remote' AND peer_ip_global=1 -- E-03..05
        AND dns_resolved_count > 0                                            -- E-06
        AND length(dns_resolved_digest)=64                                    -- E-07
        AND tls_verified=1 AND tls_hostname_verified=1                        -- E-08/09
        AND length(tls_cert_sha256)=64 AND length(tls_spki_sha256)=64         -- E-10/11
        AND tls_pin_status='matched' AND tls_pin_expected=tls_spki_sha256     -- E-12/13
        AND pin_verification_status='performed_match'                         -- E-14
        AND length(pin_verification_evidence)=64                              -- E-15
        AND proxy_mode='none' AND proxy_peer_ip IS NULL                       -- E-16/17
        AND http_method='POST' AND request_path LIKE '%/chat/completions'     -- E-18/19
        AND length(request_body_digest)=64 AND request_body_bytes>0           -- E-20/21
        AND length(model_params_digest)=64 AND length(model_requested)>0      -- E-22/23
        AND endpoint_id IS NOT NULL AND target_host IS NOT NULL               -- E-24/25
        AND http_status=200                                                   -- E-26
        AND length(response_body_digest)=64 AND response_body_bytes>0         -- E-27/28
        AND upstream_response_id IS NOT NULL
        AND length(upstream_response_id)>0
        AND upstream_response_id_source='body.id'
        AND length(upstream_model)>0
        AND usage_prompt_tokens>0
        AND redirect_count=0
        AND signing_architecture='signer_service'
        AND length(session_id)>0 AND length(signer_identity)>0
        AND length(key_id)=16 AND length(evidence_mac)=64
        AND schema_version='v2'
        AND unix_created_at>=unix_received_at
        AND unix_created_at-unix_received_at<=5
    )),

    -- ===== I 族：一致性（18 条）=====
    CHECK (transport_path<>'local' OR endpoint_class='loopback'),
    CHECK (transport_path<>'private' OR endpoint_class='private'),
    CHECK (transport_path<>'direct_verified' OR endpoint_class='remote'),
    CHECK (transport_path<>'proxy_unverified' OR peer_role='proxy'),
    CHECK (transport_path<>'direct_verified' OR peer_role='upstream'),
    CHECK (peer_class<>'remote' OR peer_ip_global=1),
    CHECK (peer_class NOT IN ('loopback','private') OR peer_ip_global=0),
    CHECK (proxy_mode='none' OR proxy_peer_ip IS NOT NULL
           OR proxy_mode='unknown'),
    CHECK (proxy_mode<>'none' OR proxy_peer_ip IS NULL),
    CHECK (proxy_mode<>'env' OR proxy_env_present=1),
    CHECK (proxy_mode<>'env' OR transport_path<>'direct_verified'),
    CHECK (transport_path<>'proxy_unverified' OR proxy_mode IN ('env','explicit','unknown')),
    CHECK (pin_verification_status<>'performed_match'
           OR (tls_pin_status='matched' AND tls_pin_expected=tls_spki_sha256)),
    CHECK (attempt_verdict<>'real_llm_unverified'
           OR json_array_length(unverified_reasons)>0),
    CHECK (attempt_verdict<>'local_llm' OR (transport_path IN ('local','private')
           AND endpoint_class IN ('loopback','private'))),
    CHECK (upstream_response_id IS NULL OR length(upstream_response_id_source)>0),
    CHECK (length(evidence_mac)=0 OR length(key_id)=16),
    CHECK (signing_architecture<>'signer_service' OR length(signer_identity)>0)
);

CREATE INDEX ix_ev_decision ON ai_transport_evidence(decision_id, seq);
CREATE INDEX ix_ev_global   ON ai_transport_evidence(global_evidence_seq);

-- 6) 封印（仅一致性辅助）
CREATE TABLE ai_evidence_seal (
    decision_id TEXT NOT NULL,
    seq         INTEGER NOT NULL CHECK (seq>=1),
    evidence_id TEXT NOT NULL CHECK (length(evidence_id)=64),
    sealed_at   TEXT NOT NULL,
    seal_hash   TEXT NOT NULL CHECK (length(seal_hash)=64),
    PRIMARY KEY (decision_id, seq),
    UNIQUE (evidence_id)
);

-- 7) 锚点（本地镜像；判定读外部日志）
CREATE TABLE ai_anchor (
    anchor_id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_seq INTEGER NOT NULL CHECK (start_seq>=1),
    end_seq   INTEGER NOT NULL CHECK (end_seq>=start_seq),
    leaf_count INTEGER NOT NULL CHECK (leaf_count>=1),
    anchor_root TEXT NOT NULL CHECK (length(anchor_root)=64),
    prev_anchor_root TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    anchor_key_id TEXT NOT NULL,
    anchor_signature TEXT NOT NULL,
    external_ref TEXT NOT NULL DEFAULT '',
    CHECK (leaf_count = end_seq - start_seq + 1)
);
```

---

## 14. 触发器与视图

### 14.1 触发器（**19 个**，逐一列表）

| # | 名称 | 事件 / 对象 | 作用 |
|---|---|---|---|
| 1 | `trg_dec_claim_ins` | BI / decisions | claim=real_llm 必须有证据 |
| 2 | `trg_dec_claim_upd` | BU / decisions | 同上（UPDATE） |
| 3 | `trg_dec_no_delete` | BD / decisions | 禁删 |
| 4 | `trg_ev_seal_guard_ins` | BI / evidence | 槽位已封印则拒 |
| 5 | `trg_ev_seal_after_ins` | AI / evidence | 写封印 |
| 6 | `trg_ev_no_update` | BU / evidence | 证据不可改 |
| 7 | `trg_ev_no_delete` | BD / evidence | 证据不可删 |
| 8 | `trg_ev_chain_ins` | BI / evidence | 链连续 |
| 9 | `trg_ev_json_ins` | BI / evidence | reasons 白名单 |
| 10 | `trg_ev_mac_shape_ins` | BI / evidence | MAC 字段同进同退 |
| 11 | `trg_ev_global_seq_ins` | BI / evidence | 全局序号必须等于计数器 |
| 12 | `trg_ev_session_ins` | BI / evidence | B 行必须有 **signed** 会话（committed 在插入后置） |
| 13 | `trg_seal_no_update` | BU / seal | 禁改 |
| 14 | `trg_seal_no_delete` | BD / seal | 禁删 |
| 15 | `trg_seal_guard_ins` | BI / seal | 禁重复占位 |
| 16 | `trg_session_transition` | BU / session | 只允许白名单迁移 |
| 17 | `trg_session_no_delete` | BD / session | 会话不可删 |
| 18 | `trg_anchor_no_update` | BU / anchor | 锚点不可改 |
| 19 | `trg_anchor_no_delete` | BD / anchor | 锚点不可删 |

```sql
CREATE TRIGGER trg_dec_claim_ins BEFORE INSERT ON ai_decisions
WHEN NEW.provenance='real_llm' AND NOT EXISTS (
  SELECT 1 FROM ai_transport_evidence e
   WHERE e.evidence_id=NEW.authoritative_evidence_id
     AND e.decision_id=NEW.decision_id AND e.attempt_verdict='real_llm')
BEGIN SELECT RAISE(ABORT,'claim without evidence'); END;

CREATE TRIGGER trg_dec_claim_upd BEFORE UPDATE ON ai_decisions
WHEN NEW.provenance='real_llm' AND NOT EXISTS (
  SELECT 1 FROM ai_transport_evidence e
   WHERE e.evidence_id=NEW.authoritative_evidence_id
     AND e.decision_id=NEW.decision_id AND e.attempt_verdict='real_llm')
BEGIN SELECT RAISE(ABORT,'claim without evidence (update)'); END;

CREATE TRIGGER trg_dec_no_delete BEFORE DELETE ON ai_decisions
BEGIN SELECT RAISE(ABORT,'decisions append-only'); END;

CREATE TRIGGER trg_ev_seal_guard_ins BEFORE INSERT ON ai_transport_evidence
WHEN EXISTS (SELECT 1 FROM ai_evidence_seal s
              WHERE (s.decision_id=NEW.decision_id AND s.seq=NEW.seq)
                 OR s.evidence_id=NEW.evidence_id)
BEGIN SELECT RAISE(ABORT,'slot already sealed'); END;

CREATE TRIGGER trg_ev_seal_after_ins AFTER INSERT ON ai_transport_evidence
BEGIN
  INSERT INTO ai_evidence_seal(decision_id,seq,evidence_id,sealed_at,seal_hash)
  VALUES (NEW.decision_id,NEW.seq,NEW.evidence_id,NEW.created_at,NEW.integrity_hash);
END;

CREATE TRIGGER trg_ev_no_update BEFORE UPDATE ON ai_transport_evidence
BEGIN SELECT RAISE(ABORT,'evidence append-only'); END;

CREATE TRIGGER trg_ev_no_delete BEFORE DELETE ON ai_transport_evidence
BEGIN SELECT RAISE(ABORT,'evidence append-only'); END;

CREATE TRIGGER trg_ev_chain_ins BEFORE INSERT ON ai_transport_evidence
WHEN (NEW.seq=1 AND NEW.prev_hash<>'')
  OR (NEW.seq>1 AND NEW.prev_hash<>COALESCE((
       SELECT p.integrity_hash FROM ai_transport_evidence p
        WHERE p.decision_id=NEW.decision_id AND p.seq=NEW.seq-1),'~'))
BEGIN SELECT RAISE(ABORT,'chain broken'); END;

CREATE TRIGGER trg_ev_json_ins BEFORE INSERT ON ai_transport_evidence
WHEN EXISTS (SELECT 1 FROM json_each(NEW.unverified_reasons) j
              WHERE j.type<>'text' OR trim(j.value)='' OR j.value NOT IN (
                'missing_upstream_id','missing_usage','envelope_digest_failed',
                'proxy_path','status_not_200','tls_verify_failed','tls_pin_mismatch',
                'pin_not_performed','mac_missing','mac_invalid','anchor_stale',
                'model_mismatch','transport_unverified','response_unparsable',
                'session_incomplete','other'))
BEGIN SELECT RAISE(ABORT,'invalid reason'); END;

CREATE TRIGGER trg_ev_mac_shape_ins BEFORE INSERT ON ai_transport_evidence
WHEN (length(NEW.evidence_mac)=0 AND (length(NEW.key_id)>0 OR length(NEW.mac_signed_at)>0))
  OR (length(NEW.evidence_mac)>0 AND (length(NEW.key_id)=0 OR length(NEW.mac_signed_at)=0))
BEGIN SELECT RAISE(ABORT,'mac fields all-or-none'); END;

CREATE TRIGGER trg_ev_global_seq_ins BEFORE INSERT ON ai_transport_evidence
WHEN NEW.global_evidence_seq <> (SELECT next_seq FROM ai_seq_counter WHERE id=1)
  OR NEW.global_evidence_seq <= COALESCE((SELECT MAX(global_evidence_seq)
       FROM ai_transport_evidence), 0)
BEGIN SELECT RAISE(ABORT,'global seq out of order'); END;

CREATE TRIGGER trg_ev_session_ins BEFORE INSERT ON ai_transport_evidence
WHEN NEW.signing_architecture='signer_service'
 AND NOT EXISTS (SELECT 1 FROM ai_sign_session s
                  WHERE s.session_id=NEW.session_id
                    AND s.decision_id=NEW.decision_id
                    AND s.state='signed')          -- 插入时只要求 signed；
                                                    -- committed 在证据落库后才置（见 §15 步骤⑨）
BEGIN SELECT RAISE(ABORT,'signer evidence requires signed session'); END;

CREATE TRIGGER trg_seal_no_update BEFORE UPDATE ON ai_evidence_seal
BEGIN SELECT RAISE(ABORT,'seal append-only'); END;
CREATE TRIGGER trg_seal_no_delete BEFORE DELETE ON ai_evidence_seal
BEGIN SELECT RAISE(ABORT,'seal append-only'); END;
CREATE TRIGGER trg_seal_guard_ins BEFORE INSERT ON ai_evidence_seal
WHEN EXISTS (SELECT 1 FROM ai_evidence_seal s
              WHERE (s.decision_id=NEW.decision_id AND s.seq=NEW.seq)
                 OR s.evidence_id=NEW.evidence_id)
BEGIN SELECT RAISE(ABORT,'seal slot occupied'); END;

CREATE TRIGGER trg_session_transition BEFORE UPDATE OF state ON ai_sign_session
WHEN NOT EXISTS (SELECT 1 FROM ai_session_transition t
                  WHERE t.from_state=OLD.state AND t.to_state=NEW.state)
BEGIN SELECT RAISE(ABORT,'illegal session transition'); END;

CREATE TRIGGER trg_session_no_delete BEFORE DELETE ON ai_sign_session
BEGIN SELECT RAISE(ABORT,'sessions append-only'); END;

CREATE TRIGGER trg_anchor_no_update BEFORE UPDATE ON ai_anchor
BEGIN SELECT RAISE(ABORT,'anchor append-only'); END;
CREATE TRIGGER trg_anchor_no_delete BEFORE DELETE ON ai_anchor
BEGIN SELECT RAISE(ABORT,'anchor append-only'); END;
```

### 14.2 视图（**6 个**）

```sql
CREATE VIEW v_decision_struct AS
SELECT d.decision_id, d.symbol, d.record_origin, d.provenance AS claimed_verdict,
       d.authoritative_evidence_id, d.voided_at,
       e.evidence_id AS resolved_evidence_id, e.attempt_verdict,
       e.signing_architecture, e.key_id, e.schema_version, e.session_id,
       e.evidence_mac, e.global_evidence_seq,
       (SELECT COUNT(*) FROM ai_evidence_seal s
         WHERE s.decision_id=e.decision_id AND s.seq=e.seq) AS seal_rows,
       CASE
         WHEN d.voided_at IS NOT NULL THEN 'voided'
         WHEN d.record_origin='migrated' THEN 'legacy_unverified'
         WHEN d.provenance<>'real_llm' THEN d.provenance
         WHEN d.authoritative_evidence_id IS NULL THEN 'integrity_violation'
         WHEN e.evidence_id IS NULL THEN 'integrity_violation'
         WHEN e.decision_id<>d.decision_id THEN 'integrity_violation'
         WHEN e.attempt_verdict<>'real_llm' THEN 'integrity_violation'
         ELSE 'unverified_by_db'      -- DB 永不输出 authenticated
       END AS db_verdict
  FROM ai_decisions d
  LEFT JOIN ai_transport_evidence e ON e.evidence_id=d.authoritative_evidence_id;

CREATE VIEW v_evidence_without_seal AS
SELECT e.decision_id, e.seq, e.evidence_id, 'missing_seal' AS defect
  FROM ai_transport_evidence e
 WHERE NOT EXISTS (SELECT 1 FROM ai_evidence_seal s
                    WHERE s.decision_id=e.decision_id AND s.seq=e.seq)
UNION ALL
SELECT s.decision_id, s.seq, s.evidence_id,
       CASE WHEN e.evidence_id IS NULL THEN 'missing_evidence'
            WHEN e.evidence_id<>s.evidence_id THEN 'id_mismatch'
            ELSE 'hash_mismatch' END
  FROM ai_evidence_seal s
  LEFT JOIN ai_transport_evidence e
         ON e.decision_id=s.decision_id AND e.seq=s.seq
 WHERE e.evidence_id IS NULL OR e.evidence_id<>s.evidence_id
    OR e.integrity_hash<>s.seal_hash;

CREATE VIEW v_session_state AS
SELECT s.session_id, s.decision_id, s.state, s.signer_identity, s.key_id,
       s.created_at, s.expires_at, s.evidence_id, s.closed_reason,
       (SELECT COUNT(*) FROM ai_transport_evidence e WHERE e.session_id=s.session_id) AS evidence_rows
  FROM ai_sign_session s;

CREATE VIEW v_anchor_tail AS
SELECT anchor_id, start_seq, end_seq, leaf_count, anchor_root,
       prev_anchor_root, created_at, anchor_key_id, external_ref
  FROM ai_anchor ORDER BY anchor_id DESC LIMIT 20;

CREATE VIEW v_auth_gaps AS
SELECT 'missing_mac' AS gap, COUNT(*) AS n FROM ai_transport_evidence
 WHERE length(evidence_mac)=0
UNION ALL SELECT 'evidence_without_seal', COUNT(*) FROM v_evidence_without_seal
 WHERE defect='missing_seal'
UNION ALL SELECT 'signer_without_session', COUNT(*) FROM ai_transport_evidence
 WHERE signing_architecture='signer_service' AND session_id IS NULL;

CREATE VIEW v_evidence_facts AS
SELECT evidence_id, decision_id, seq, global_evidence_seq,
       signing_architecture, key_id, session_id, attempt_verdict,
       length(evidence_mac)=64 AS mac_present,
       length(key_id)=16 AS key_present
  FROM ai_transport_evidence;
```

---

## 15. 事务与崩溃恢复

```
预检：FK=1、keyring 可读、签名服务可用（B）、allowlist 非空
BEGIN IMMEDIATE
  ① UPDATE ai_seq_counter SET next_seq=next_seq+1
  ② SELECT next_seq  →  gseq
  ③ 会话：created → request_bound（B）
  ④ 发请求（B 由服务）→ sent → response_received
  ⑤ 计算 MAC、字段自检 → signed
  ⑥ INSERT evidence(global_evidence_seq=gseq, session_id=…)
  ⑦ INSERT decision(claim, pointer)
  ⑧ 事务内复核：struct_ok / 链 / seal / MAC 复算 / 会话匹配
  ⑨ 会话 → committed
COMMIT
提交后：只读审计 + 异步锚定
```

| 失败点 | 状态 | 处理 |
|---|---|---|
| ①–② 失败 | — | 回滚，序号不前进 |
| ④ 网络失败 | `failed` | 回滚；不写证据 |
| ⑤ MAC 失败 | `failed` | 回滚 |
| ⑥/⑦ 约束失败 | `failed` | 回滚 |
| ⑧ 复核不一致 | `failed` | 回滚 |
| COMMIT 失败 | `persistence_failed` | 不重签；序号随回滚复原 |
| 提交后崩溃 | `committed` | 正常；锚定异步补 |

**幂等**：同一 `decision_id` 第二次写入必然被 `UNIQUE(decision_id, seq)` 或 `session_id UNIQUE` 拒绝。

---

## 16. 历史迁移

| 步 | 动作 |
|---|---|
| 0 | 预检：`integrity_check`、`foreign_key_check`、行数 |
| 1 | 备份 + SHA256 |
| 2 | 事务外 `PRAGMA foreign_keys=OFF`（读回 0） |
| 3 | `BEGIN IMMEDIATE` |
| 4 | 删视图（6）/触发器（19） |
| 5 | 建 `_new` 表（含会话/计数器/allowlist） |
| 6 | 复制 decisions（`record_origin='migrated'`、指针 NULL） |
| 7 | 复制 evidence 按 `(decision_id, seq)` 排序，依次赋 `global_evidence_seq=1..N` |
| 8 | 历史行设 `evidence_mac=''`、`key_id=''`、`mac_signed_at=''`、`schema_version='v1'`、`signing_architecture='app_in_process'`（历史不可能是 B） |
| 9 | 重建 seal |
| 10 | 校验：行数、内容哈希（排除 MAC 三列与 global_seq 的分配差异需可解释） |
| 11 | `DROP` 旧表（先子后父） |
| 12 | `RENAME` `_new` → 最终名 |
| 13 | 建索引 / 触发器 / 视图 |
| 14 | `foreign_key_check` / `integrity_check` |
| 15 | 抽样：全部 migrated 行 `db_verdict='legacy_unverified'` |
| 16 | `COMMIT` |
| 17 | `finally`：`PRAGMA foreign_keys=ON` + 读回断言 |

**历史语义**：v1 记录永不补签、永不进入 `upstream_session_authenticated`；`global_evidence_seq` 按排序一次分配，之后不再变化。

---

## 17. 威胁模型（A/B 分列）

| 攻击者 | 读密钥 | 调签名服务 | 造合法 MAC | 造 app_record_authenticated | 造 upstream_session_authenticated | 改请求 | 重放响应 | 改历史 | 绕过 readiness | 可发现 | 最高声明 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| T1 公开接口 | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | — | 应用记录 |
| T2 任意 DML | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | 受限 | ❌ | MAC | 未认证数据 |
| T3 改结构 | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ | ❌ | MAC+锚定 | 同上 |
| T4 改文件 | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ | ❌ | 锚定 | 同上 |
| **T5 应用被控** | ✅(app) | ✅ | ✅(app) | **✅** | **❌**（无 signer 密钥） | ✅(A) | ⚠️ | 视权限 | A 可 | A 不可检测 | **仅 app_record** |
| **T5′ 可调服务** | ❌ | ✅ | ❌ | ✅ | ⚠️ 只能请求真实调用，内容不可伪造 | ❌ | ❌（会话一次性） | ❌ | 否 | 服务审计 | upstream_session（真实会话） |
| T6 OS 用户被控 | ✅ | ✅ | ✅ | ✅ | ✅（若控制服务账户） | ✅ | ✅ | ✅ | ✅ | 仅外部锚定 | 不可声明 |
| T7 锚定失效 | ❌ | ❌ | ❌ | ✅ | ❌（B 需 anchor current） | ❌ | ❌ | 可掩盖 | 部分 | 锚点链 | 不可声明"已锚定" |

---

## 18. 反例（**78 条**，全部设计预期）

| # | T | 操作 | 预期 | 拦截层 | readiness |
|---|---|---|---|---|---|
| CE-01 | T1 | claim=real_llm 无证据 | 拒 | trg_dec_claim_ins | — |
| CE-02 | T1 | 重复 `(decision_id,seq)` | 拒 | UNIQUE | — |
| CE-03 | T1 | 重复 `decision_id` 建会话 | 拒 | session UNIQUE | — |
| CE-04 | T1 | migrated+real_llm+指针 | 拒 | CHECK | — |
| CE-05 | T1 | 非白名单 reason | 拒 | trg_ev_json_ins | — |
| CE-06 | T1 | 请求体 257 KiB | 拒 | 服务限额 | — |
| CE-07 | T1 | 响应体 1.01 MiB | 拒 | 服务限额 | — |
| CE-08 | T1 | 非 allowlist endpoint_id | 拒 | allowlist | — |
| CE-09 | T1 | target_host 与 endpoint 不符 | 拒 | 服务校验 | — |
| CE-10 | T1 | 请求 method 非 POST | 拒 | CHECK | — |
| CE-11 | T1 | 请求 path 越出前缀 | 拒 | 服务校验 | — |
| CE-12 | T1 | model 不在 allowed_models | 拒 | 服务校验 | — |
| CE-13 | T1 | 会话 TTL 过期后调用 | 拒 | 状态 expired | — |
| CE-14 | T1 | 超频率调用 | 拒 | 限流 | — |
| CE-15 | T1 | stream=true（未授权） | 拒 | 服务校验 | — |
| CE-16 | T2 | INSERT OR REPLACE 证据同 id | 拒 | seal guard | — |
| CE-17 | T2 | REPLACE 换 seq 新 id | 拒 | seal guard | — |
| CE-18 | T2 | UPDATE 证据 | 拒 | trg_ev_no_update | — |
| CE-19 | T2 | DELETE 证据 | 拒 | trg_ev_no_delete | — |
| CE-20 | T2 | UPSERT DO UPDATE | 拒 | BEFORE INSERT guard | — |
| CE-21 | T2 | **新增自洽假证据但 MAC 为空** | **INSERT 即拒**（E 族要求 64 hex） | CHECK | — |
| CE-22 | T2 | 新增假证据 + 随机 MAC | 插入成功 | — | integrity_violation |
| CE-23 | T2 | 复制他人 MAC | 插入成功 | — | integrity_violation |
| CE-24 | T2 | 跨 decision 复制 | 同上 | — | integrity_violation |
| CE-25 | T2 | 跨 seq 复制 | 同上 | — | integrity_violation |
| CE-26 | T2 | 替换 key_id 为 app 槽 | 插入成功 | — | 等级降为 app_record_* |
| CE-27 | T2 | 篡改 `signing_architecture` | 插入成功 | — | 与 keyring 槽不符 → integrity_violation |
| CE-28 | T2 | 全局序号跳号 | 拒 | trg_ev_global_seq_ins | — |
| CE-29 | T2 | 全局序号重复 | 拒 | UNIQUE | — |
| CE-30 | T2 | 无会话的 signer 证据 | 拒 | trg_ev_session_ins | — |
| CE-31 | T2 | 会话未 committed 就插证据 | 拒 | trg_ev_session_ins | — |
| CE-32 | T2 | 一会话两证据 | 拒 | evidence.session_id UNIQUE | — |
| CE-33 | T2 | 一 decision 两会话 | 拒 | session.decision_id UNIQUE | — |
| CE-34 | T2 | 会话状态跳跃（created→signed） | 拒 | trg_session_transition | — |
| CE-35 | T2 | 终态会话再改 | 拒 | 无出边 | — |
| CE-36 | T2 | 删除会话 | 拒 | trg_session_no_delete | — |
| CE-37 | T2 | MAC 三字段半空 | 拒 | trg_ev_mac_shape_ins | — |
| CE-38 | T2 | 空 MAC + key_id | 拒 | 同上 | — |
| CE-39 | T3 | DROP TRIGGER 改证据 | 成功改库 | — | MAC 不符 → integrity_violation |
| CE-40 | T3 | 改 CHECK 后插假证据 | 成功 | — | MAC 无法伪造 |
| CE-41 | T3 | 改 keyring 元数据 | 取决于文件 ACL | OS | 槽位改变 → 等级重算 |
| CE-42 | T3 | 伪造视图输出 authenticated | 视图可改 | — | 应用终判不读视图（§9.3） |
| CE-43 | T3 | 删除 `v_auth_gaps` | 视图缺失 | — | 应用拒绝启动 |
| CE-44 | T4 | 整库替换 | 成功 | — | MAC/锚点 mismatch |
| CE-45 | T4 | 整库回滚到旧备份 | 成功 | — | 锚点链断裂 |
| CE-46 | T4 | 删锚点日志 | 成功 | — | anchor missing → unverified |
| CE-47 | T4 | 改锚点日志 | 取决于 ACL | — | 锚点签名/链校验失败 |
| CE-48 | T4 | 替换 app 密钥文件 | 成功 | DPAPI | 解密失败 → fail closed |
| CE-49 | T4 | 替换 signer 密钥文件 | 需服务账户 | DPAPI | 同上 |
| CE-50 | T4 | 拷他人机器密钥 | 成功 | DPAPI 用户绑定 | 解密失败 |
| CE-51 | T4 | 时间回拨 | 成功 | — | 锚点时间倒退 → mismatch |
| CE-52 | T5 | A：伪造内容并用 app 密钥签名 | **成功** | — | **只能是 app_record_authenticated** |
| CE-53 | T5 | A：把 `signing_architecture` 写成 signer_service | 写入成功但 MAC 用 app 密钥 | — | 槽位不符 → integrity_violation |
| CE-54 | T5 | A：复制 B 的 key_id | 写入成功 | — | MAC 不符 → integrity_violation |
| CE-55 | T5 | A：重放 B 的 MAC | 写入成功 | — | 字段不同 → MAC 不符 |
| CE-56 | T5 | A：导出 app 密钥 | 同用户可 | — | 仍只能 app 等级 |
| CE-57 | T5 | A：内存 dump signer 密钥 | **不可**（跨账户 DPAPI） | DPAPI | — |
| CE-58 | T5′ | 请求服务签任意 blob | 拒 | 服务只接受参数 | — |
| CE-59 | T5′ | 请求非 allowlist 端点 | 拒 | allowlist | — |
| CE-60 | T5′ | 重放同一 client_nonce | 拒 | nonce 一次性 | — |
| CE-61 | T5′ | 用旧 session 再签 | 拒 | 终态无出边 | — |
| CE-62 | T5′ | 服务崩溃后恢复旧会话 | 拒 | 启动置 abandoned | — |
| CE-63 | T5′ | DB 提交失败后重签 | 拒 | persistence_failed | — |
| CE-64 | T5′ | 伪造调用者 SID 连管道 | 拒 | 管道 ACL + token 校验 | — |
| CE-65 | T5′ | 高频调用刷成本 | 限流 | 速率限制 | — |
| CE-66 | SSRF | endpoint 解析到 127.0.0.1 | 拒 | IP 全量校验 | — |
| CE-67 | SSRF | 解析到 10.0.0.0/8 | 拒 | 同上 | — |
| CE-68 | SSRF | 解析到 169.254.169.254 | 拒 | 同上 | — |
| CE-69 | SSRF | 解析到 ::1 / fc00::/7 | 拒 | 同上 | — |
| CE-70 | SSRF | DNS rebinding（二次解析不同） | 拒 | 连接后 peer 复核 | — |
| CE-71 | SSRF | 302 跳转到私网 | 拒 | 禁止自动重定向 | — |
| CE-72 | SSRF | 继承 `HTTPS_PROXY=127.0.0.1:p` | 拒/降级 | 禁止继承 env | — |
| CE-73 | 链 | `prev_hash` 断链 | 拒 | trg_ev_chain_ins | — |
| CE-74 | 链 | 全局序号回滚后空洞 | 拒绝（序号与计数器不符） | trg_ev_global_seq_ins | — |
| CE-75 | 锚定 | Merkle root 不符 | 拒 | 复算 | anchor mismatch |
| CE-76 | 锚定 | 奇数叶处理不同实现 | 复算不一致即判 mismatch | 规约刚性 | — |
| CE-77 | A/B | A 生成的记录被报表计入 B 统计 | 拒 | 报表按 slot 分列 | — |
| CE-78 | 结构 | `unverified_reasons` 为 `[ ]` + unverified | 拒 | I 族 | — |

---

## 19. 正向场景（**32 条**）

| # | 场景 | 期望 |
|---|---|---|
| PS-01 | B 全条件 + 锚定 current | `upstream_session_authenticated` |
| PS-02 | B 但锚定 stale | `upstream_session_unverified` |
| PS-03 | A 全条件 | `app_record_authenticated` |
| PS-04 | A 但 MAC 缺失 | `app_record_unverified` |
| PS-05 | A 但未知 key | `app_record_unverified` |
| PS-06 | 历史 v1 行 | `legacy_unverified` |
| PS-07 | 历史行尝试补签 | 拒（UPDATE 封） |
| PS-08 | 多证据 seq1 rejected / seq2 real_llm | seq2 按等级输出 |
| PS-09 | 作废 | `voided` |
| PS-10 | 回环代理（B） | `upstream_session_unverified` |
| PS-11 | 本地模型 | `local_llm` |
| PS-12 | 私网模型 | `local_llm` |
| PS-13 | 测试桩 | `stub` |
| PS-14 | 无凭据 | `unavailable` |
| PS-15 | DNS 失败 | `rejected` |
| PS-16 | HTTP 500 | `rejected` |
| PS-17 | 200 缺 upstream_id | `upstream_session_unverified` |
| PS-18 | pin mismatched | `rejected` |
| PS-19 | 会话正常走完全流程 | committed |
| PS-20 | 服务重启后旧会话 | abandoned |
| PS-21 | 密钥轮换后旧证据 | 用旧 key 验证通过 |
| PS-22 | 密钥轮换后新证据 | 新 key 验证通过 |
| PS-23 | 锚定恢复 stale→current | 等级回升 |
| PS-24 | 并发两进程写不同 decision | 序号连续无洞 |
| PS-25 | 写事务回滚 | 序号不前进 |
| PS-26 | 迁移中断 | 回滚到迁移前 |
| PS-27 | Merkle 单叶区间 | 复算一致 |
| PS-28 | Merkle 三叶（奇数） | 复算一致 |
| PS-29 | `v_auth_gaps` 与迁移行数一致 | 一致 |
| PS-30 | REPORT 分列 A/B 统计 | 不合并 |
| PS-31 | unknown key 的 B 记录 | `upstream_session_unverified` |
| PS-32 | 迁移行 global_seq 连续 | 通过 struct_ok |

---

## 20. SQLite 未验证语义

| # | 待测 |
|---|---|
| 1 | `INSERT OR REPLACE` 是否跳过 DELETE 触发器 |
| 2 | BEFORE INSERT 中 `RAISE(ABORT)` 是否回滚 REPLACE 的删除 |
| 3 | 循环 FK + 双 DEFERRABLE 的建表与提交 |
| 4 | AFTER INSERT 内 INSERT 失败是否回滚外层 |
| 5 | `PRAGMA foreign_keys` 事务内是否 no-op |
| 6 | `ALTER TABLE RENAME` 是否改写他表 FK |
| 7 | `json_type()` 对非法 JSON 的行为 |
| 8 | 多 BEFORE 触发器执行顺序 |
| 9 | `UPDATE ... SET next_seq=next_seq+1` 在并发下的可见性 |
| 10 | `trg_ev_global_seq_ins` 中子查询读到的计数器值是否为同一事务内更新后的值（**关键**） |
| 11 | `UNION ALL` 视图的类型亲和性 |
| 12 | TEXT PK + NOT NULL 的 NULL 语义 |
| 13 | `INSERT OR IGNORE` 遇 `RAISE(ABORT)` |
| 14 | WAL 下两进程并发写与重试 |
| 15 | `AUTOINCREMENT` 与手工 `anchor_id` 的交互 |

> **第 10 项是 V3.6 的关键假设**：若触发器读不到同事务内已更新的计数器值，全局序号机制不成立，必须改为应用侧分配 + 事后校验。**未验证前不得声称序号机制可用。**

---

## 21. 残余风险

| # | 风险 | 层 |
|---|---|---|
| R-1 | `upstream_response_id` 值真实性 | 上游 |
| R-2 | `integrity_hash` 算法正确性 | APP |
| R-3 | 任意 SQL 权限者可改结构 | DB |
| R-4 | `local_llm` 是否真模型 | 声明 |
| R-5 | 代理路径上游身份 | 设计上限 |
| R-6 | 备份还原覆盖新数据 | 运维 |
| R-7 | `unix_received_at` 真实性 | APP |
| R-8 | 模型名≠推理 | 语义 |
| R-9 | seal 可被 REPLACE | DB |
| R-10 | `pin_verification_status` 为声明 | APP |
| R-11 | 决策行可被 REPLACE | DB |
| R-12 | A 架构：应用被控可签任意内容 | 架构 |
| R-13 | DPAPI 绑定用户/账户：同账户被控即失守 | OS |
| R-14 | 锚点日志本地可改（取决于 ACL） | 部署 |
| R-15 | 密钥丢失 = 历史永久不可认证 | 运维 |
| **R-16** | T5′ 可反复请求真实调用（成本 DoS），限流只是缓解 | APP |
| **R-17** | 全局序号依赖 §20-10 未验证行为 | DB |
| **R-18** | 上游模型身份永不可证 | 上游 |

---

## 22. 实施前置条件

| # | 条件 |
|---|---|
| 1 | 明确本期只做 A 还是 A+B（做 B 才可对外称 session） |
| 2 | 完成两次 `key-init`（app / signer），登记 key_id 与 slot |
| 3 | 建 endpoint allowlist（含 pin 与 allowed_models） |
| 4 | 锚点目录 ACL 就绪 |
| 5 | §20 的 15 项语义探针全部实跑，尤其第 10 项 |
| 6 | 迁移脚本在独立副本演练通过 |

---

## 23. 版本差异与交叉引用

| 项 | V3.5 | V3.6 |
|---|---|---|
| 认证等级来源 | 字段声明 | **keyring 槽位推导** |
| 状态名 | 单一 `real_llm_authenticated` | `app_record_*` / `upstream_session_*` 两套 |
| MAC 覆盖 | 42 字段（无请求侧） | **64 字段（含请求侧 16 项）** |
| 会话 | 无 | 表 + 状态机 + 一次性 |
| 全局序号 | 不存在 | 计数器分配 |
| 锚点 | 引用不存在的序号 | 按 `global_evidence_seq` + Merkle 规约 |
| `struct_ok` | 未定义 | 12 项清单 |
| 触发器 / 视图 | 15 / 5 | 19 / 6 |
| CE / PS | 64 / 26 | 78 / 32 |
| B 网络约束 | 概念 | SSRF/DNS/代理/重定向/限额/IPC 全表 |

---

## 24. 自检表（不构成审计）

| # | 检查 | 结果 |
|---|---|---|
| C-01 | A/B 是否结构隔离 | ✅ 槽位推导 + 双命名空间 |
| C-02 | A 能否产出 B 状态 | ✅ 不能（无 signer 密钥） |
| C-03 | MAC 是否含请求侧 | ✅ 13 项 |
| C-04 | 会话是否一次性 | ✅ UNIQUE + 状态机 |
| C-05 | 全局序号是否真实存在 | ✅ 计数器表 |
| C-06 | struct_ok 是否可执行 | ✅ 12 项 |
| C-07 | 状态命名是否越权 | ✅ 无 model/inference 字样 |
| C-08 | 反例 / 正例数 | ✅ 78 / 32 |
| C-09 | 是否宣称实测 | ✅ 全文标注设计预期 |
| C-10 | 是否有"略/同前" | ✅ 无 |

---

## 25. 交付状态

```
V3.6 设计文件已生成，待独立审计；
方案未获批；未获实施授权；
未执行 SQL；未创建数据库；未运行探针；未跑迁移；
S0 未启动。
```

---

> 本文件 SHA256（本行之前全部字节，可用 `head -n -1 <本文件> | sha256sum` 复算）：`bde98420e4424e78567af14934a8cd38247430bf0b7e046fc0c2cd888ffc957e`
