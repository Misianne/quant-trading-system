# US Real LLM Evidence Remediation — 四项决策确认单（v2 · 已确认）

| 项 | 值 |
|---|---|
| 文档性质 | **决策确认单**。记录已确认的设计决策；**不构成源码实施授权。** |
| 版本 | v2（2026-11-10 19:24）/ 取代 v1（19:18，四项待确认，`62110b70…`） |
| 项目 | `C:\FosunAI-Trading` |
| 方案 | `US_REAL_LLM_EVIDENCE_REMEDIATION_PLAN.md`（`3d569f16…`，534 行） |
| 方案状态 | **已批准**（19:03） |
| **D1–D4** | **已由用户确认**（19:22） |
| 源码实施 | **未授权** |
| S0 | **未启动**（未备份、未 dry-run、未执行 SQL） |

> 本文件是"设计决策已定"的记录，不是"可以开工"的许可。开工仍需用户另行明确授权（闸门第 2 步）。

---

## 0. 状态总览

| 事项 | 状态 | 依据 |
|---|---|---|
| 修复方案 | ✅ 已批准 | 19:03 用户指令 |
| D1 代理策略 / 三轴模型 | ✅ **已确认**（19:22） | §1-D1 |
| D2 response_id 一票否决 | ✅ **已确认**（19:22） | §1-D2 |
| D3 IP 钉死 | ✅ **已确认**（19:22） | §1-D3 |
| D4 历史记录标注 | ✅ **已确认**（19:22） | §1-D4 |
| 源码实施 | ⛔ 未授权 | 19:22 用户明确"不构成源码实施授权" |
| S0 备份 / dry-run / SQL | ⛔ 未执行 | `data/` 无 `*.bak*`；无 SQL 文件 |
| 数据库迁移 | ⛔ 未执行 | 无 `ai_transport_evidence` 表 |

---

## 1. 已确认的决策

### D1 代理策略与三轴数据模型 — ✅ 已确认（19:22）

**用户确认原文**

> 保留 endpoint_class × provenance 两轴，不合并为单一枚举；新增独立的 transport_path 字段，取值为 direct_verified / proxy_unverified / local / private / unknown。代理路径不得通过 readiness。real_llm_unverified 作为 provenance 状态，不得与 endpoint_class 混用。

**定稿后的数据模型（三轴，互不替代）**

| 轴 | 取值 | 由什么决定 | 描述什么 |
|---|---|---|---|
| `endpoint_class` | `remote` / `loopback` / `private` / `unknown` | DNS 解析结果 + 连接后 `getpeername()` 校验 | **连到哪一类地址** |
| `transport_path` | `direct_verified` / `proxy_unverified` / `local` / `private` / `unknown` | 连接方式（直连/代理）+ 钉死校验是否通过 | **怎么连的、验证到哪一步** |
| `provenance` | `real_llm` / `real_llm_unverified` / `stub_transport` / `rejected` / `unavailable` / `unknown` | 由前两轴 + 响应证据**联合推导** | **这条决策的可信度结论** |

**硬性约束（写入时即校验，不是文档约定）**

1. `real_llm_unverified` 只出现在 `provenance`，**绝不出现在 `endpoint_class`**。
2. 代理路径（`transport_path=proxy_unverified`）**不得**通过 readiness，即使 HTTP 200、有上游 model、有 usage。
3. `endpoint_class` 不新增 `remote_via_proxy` 之类混合值（**推翻** v1 草案 §1-D1 的映射建议）。

**跨轴不变量（实现时用 CHECK 或写入前断言强制）**

```
transport_path=local            => endpoint_class=loopback
transport_path=private          => endpoint_class=private
transport_path=direct_verified  => endpoint_class=remote
transport_path=unknown          => endpoint_class=unknown
transport_path=proxy_unverified => endpoint_class in {remote, unknown}
provenance=real_llm             => transport_path=direct_verified AND endpoint_class=remote AND upstream_id != ""
provenance=real_llm_unverified  => transport_path=proxy_unverified
```

**与已批准方案的差异（实现时同步，且不修改方案文件本身）**

| 方案原文 | 定稿后 |
|---|---|
| §1-D1 建议把 `remote_via_proxy` 加进 `endpoint_class` | `endpoint_class` 保持 4 值不变 |
| §4.3 只用 endpoint_class × provenance 两轴 | 增加第三轴 `transport_path` |
| §3.2 `ai_transport_evidence` 无 transport_path 列 | 新增 `transport_path TEXT NOT NULL`（含 5 值 CHECK） |
| §5 readiness 十项合取 | 增加 `transport_path='direct_verified'`，共 11 项 |

处理方式：**方案文件保持 `3d569f16…` 不动**，本确认单作为其"已确认修订"，实施时以本单为准。

---

### D2 response_id — ✅ 已确认（19:22）

**用户确认原文**

> 上游 response_id 缺失即否决 readiness，不允许本地生成 ID 冒充上游 ID。

**定稿后的规则**

1. `upstream_id` 仅接受上游产出物，按优先级取，并记录来源字段名 `upstream_id_source`：
   `body.id` → `header.x-request-id` → `header.cf-ray`；三者皆无 → `upstream_id=""`。
2. `upstream_id=""` ⇒ **readiness 直接 false**（一票否决）。
3. 本地标识（`decision_id`、`signal_id`、`run_id`）**禁止**写入 `upstream_id` 列。
4. 实现兜底：写入前断言 `upstream_id` 不等于同行 `decision_id` / `signal_id`，否则拒写并记 `integrity_violation`。
5. **不提供**"本地补一个 UUID 顶上"的降级路径——这正是最容易做成假证据的地方。

---

### D3 IP 钉死 — ✅ 已确认（19:22）

**用户确认原文**

> 默认开启 IP 钉死，允许经过验证的公网多 IP 集合；必须保留原始域名 TLS SNI 和证书主机名校验，验证失败时 fail closed。

**定稿后的规则**

1. 默认开启，且**不提供**运行期关闭开关（排障只能用独立调试构建，不得进入验收路径）。
2. 可信集合：解析结果**全部** `ipaddress.ip_address(x).is_global` 为真，且"连接前解析"与"连接后复核"两次集合一致。
3. 连接对象为集合内 IP；`ssl.wrap_socket(server_hostname=<原域名>)` 保留 SNI；证书主机名校验仍以**原域名**进行（不得改成校验 IP）。
4. fail closed 条件（任一命中 → `endpoint_class=unknown`、`transport_path=unknown`）：
   - `getaddrinfo` 超时、为空、抛异常；
   - 集合含回环 / 私网 / 链路本地 / unspecified / multicast / reserved / CGNAT(100.64/10)；
   - 两次解析集合不一致（rebinding 迹象）；
   - `getpeername()` 不可得，或对端 IP ∉ 可信集合；
   - 证书主机名不匹配，或 SPKI pin 不符（若启用）。
5. 多 IP 场景允许"集合内任一个"作实际对端，但必须把实际对端与集合摘要（`resolved_ips_digest`）一并写入证据。

---

### D4 历史 real_llm 记录 — ✅ 已确认（19:22）

**用户确认原文**

> 历史 real_llm 记录标记为传输未验证；不追溯认证，不修改原始记录和原始证据，只能通过独立审计说明补充状态。

**定稿后的做法**

1. **零数据改动**：历史 `ai_decisions` 行一字节不动；迁移时 `transport_verified` 默认 `0`，历史行 `transport_path` 留 `NULL`。
2. **零文件改动**：以下三个文件保持原哈希，不改一字：
   - `US_REAL_LLM_PAPER_ACCEPTANCE_REPORT.md` — `3fb7fa6361bacb13223e34a99c4e97c8428201e123d1ee78f9a2a4035c82eb73`
   - `us_loop_evidence_20261110-183109.json` — `23fde3aba2cea913ee1ac8fb072010287873e520d7cce33e491731d95827704e`
   - `independent_audit.json` — `eade6cfbde79cffc7f182737de21a7bd742a50a39f3d9d5a16ecaaf7af498127`
3. **不追溯认证**：历史行不得升级为 `direct_verified` / `real_llm`。
4. **标注载体**：仅通过**新增的独立审计说明**补状态。建议文件：
   `audit_evidence/us_loop_real_llm/historical_real_llm_transport_unverified.md`
   内容 = 历史 `real_llm` 行清单（`decision_id`、`decided_at`、`model_id`、当前 `provenance`）+ 统一结论"传输未验证" + 指向审计依据。

---

## 2. 由此定稿的实现清单（仅记录，不执行）

| 编号 | 变更点 | 关键点 |
|---|---|---|
| I-1 | 新表 `ai_transport_evidence` | 增加 `transport_path`、`upstream_id_source`、`resolved_ips_digest` |
| I-2 | `classify_endpoint` → 返回 `(endpoint_class, transport_path, resolved_ips, digest)` | 单函数出两轴，避免两次判定不一致 |
| I-3 | `TransportClient` | 不读代理环境变量、不跟随重定向、IP 钉死 + SNI、`getpeername()` 校验 |
| I-4 | 同事务写 decision + evidence | 失败回滚，不留 `real_llm` 行 |
| I-5 | readiness 11 项合取 | 含 `transport_path=direct_verified`、`upstream_id != ""` |
| I-6 | 迁移脚本 | 只加表/加列；历史行 `transport_verified=0` |
| I-7 | 历史标注文档 | 独立新文件，零数据改动 |

---

## 3. 实施授权闸门（顺序不变）

```
1. 用户最终确认 D1-D4                    ✅ 已完成（19:22）
2. 用户另行明确授权开始实施               ⛔ 未发生
3. S0：备份 us_loop.db + 前后 SHA256      ⛔ 未执行
4. 迁移 dry-run：只打印 SQL               ⛔ 未执行
5. 提交 SQL + 影响 + 回滚方案待审         ⛔ 未执行
6. 用户确认 SQL 无误 → 才可进入 S1        ⛔ 未到达
```

**当前位置：闸门 1/6 已完成，停在 2 之前。**

---

## 4. 本次操作记录（19:22–19:24）

| 操作 | 结果 |
|---|---|
| 只读核验 v1 确认单 | `62110b70…` 存在、可读、219 行 |
| 只读核验方案文件 | `3d569f16…` / 27,586 B / 534 行，未变 |
| 检查 S0 痕迹 | `data/` 下无 `*.bak*` / `*.backup*` |
| 更新本确认单 v1 → v2 | 四项状态改为「已确认」，补三轴模型与跨轴不变量 |

**未执行**：未修改方案文件；未修改 `src/`、`tests/`、`config/`；未访问 `.env`、密钥、凭据；未访问 VPS / 券商 / 真实交易接口；未修改 `data/`、`logs/` 或任何 SQLite；未创建备份；未 dry-run；未生成或执行 SQL；未发起模型请求；未触碰 Git。

---

## 5. 零改动证明

| 校验项 | 结果 |
|---|---|
| 方案文件 | `3d569f16754e389c77534cbbbfc3e294ad5d6bba67946fb7bb9f3f7fd629610a`（未变） |
| 基线 86 文件（src/tests/config/.env/data/既有证据） | **86/86 OK** |
| `data/` 最新文件 mtime | `audit.db` 17:38、`us_loop.db` 17:34、`paper_state.json` 17:32（均早于本轮） |
| `logs/` 新文件 | 无 |
| `ai_transport_evidence` 表 | 不存在 |
| 本轮新增/变更文件 | 仅本确认单（v2） |

---

## 6. 变更记录

| 版本 | 时间 | 变更 | 整文件 SHA256 |
|---|---|---|---|
| v1 | 2026-11-10 19:18 | 四项标「待用户最终确认」 | `62110b70fbcc82f1bfa9d8bb0c8c66b1e4fd7aaba2f57e0b1d616c36507a491f` |
| v2 | 2026-11-10 19:24 | 四项改「已确认（19:22）」；落实三轴模型、跨轴不变量、与方案的差异 | 见末行自指纹；整文件哈希随更新变化 |

---

## 7. 最终状态声明

```
四项决定：已由用户确认（2026-11-10 19:22）；
方案：已批准；
实施：未授权；
S0：未启动；
闸门：1/6。
```

---

> 本文件 SHA256（本行之前全部字节，可用 `head -n -1 <本文件> | sha256sum` 复算）：`b9e2cb0564d15afa49b592f3948439ccee13b0ca24c1d75e2fe7c8249b73a58a`
