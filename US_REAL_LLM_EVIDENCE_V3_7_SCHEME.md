# V3.7 修订方案（设计稿 · 不实施）

| 项 | 值 |
|---|---|
| 版本 | **v3.7**（2026-11-11 15:45） |
| 取代 | `US_REAL_LLM_EVIDENCE_V3_6_SCHEME.md`（`34c02c37…`，**不覆盖、不修改**） |
| 依据 | V3.6 第七轮独立审计（FAIL：1×P0 / 4×P1 / 3×P2 / 2×P3） |
| 性质 | 设计文档。未执行 SQL、未建库、未跑探针、未改源码 |
| 状态 | **V3.7 待第八轮独立审计**；未获批；未获实施授权；S0 未启动 |

> 全文"应/预期"均为**设计预期**。Windows ACL 与 SQLite 语义**均未在实机验证**，相关条目见 §21。

---

## 1. 执行摘要

第七轮审计的结论是"**架构方向对，但信任边界没落地**"：`keys.json` 无属主/ACL、会话可直达 `committed`、E 族让 A 分支不可达、allowlist 保护触发器根本不存在。

V3.7 的核心判断是：**SQLite 不应继续承担认证边界**。理由：

1. 只要应用与认证数据在同一个可写上下文里，任何 CHECK/触发器都能被 DROP 或绕过；
2. 六轮迭代反复在"触发器写得够不够严"上打转，问题却始终出在**权限**（谁能写这条记录），而不是"约束写得好不好"。

因此 V3.7 做三件结构性的事：

| # | 变化 | 解决 |
|---|---|---|
| 1 | **认证数据搬进独立 `auth.db`，目录 ACL 只允许 signer 服务账户写**；应用只能读（或经 IPC 查询） | P0-1 / P0-2 |
| 2 | **密钥与 keyring 元数据放在 signer 专属目录**，应用 SID 无任何访问权；元数据权威来源是服务配置，不是 `keys.json` | P0-1 |
| 3 | **A/B 拆成两个物理数据库 + 两套约束**：A 的证据写在应用库（最多 `app_record_*`），B 的证据只能由服务写进 `auth.db`（才能 `upstream_session_*`） | P1-1 |

---

## 2. V3.6 问题追踪

| 编号 | 级别 | 原问题 | V3.7 处置 | 状态 |
|---|---|---|---|---|
| P0-1 | P0 | `keys.json` 无路径/ACL/属主；同用户应用可改 slot | §5（signer 专属目录 + 服务账户 DPAPI + 元数据由服务配置生成） | **CLOSED（设计层）** |
| P0-2 | P0 | 会话表无 INSERT 守卫，可直达 `committed` | §10 触发器 `trg_session_born_created` + 认证库物理隔离 | **CLOSED（设计层）** |
| P1-1 | P1 | E 族要求 `signer_service`，A 分支不可达 | §9 E-base / E-app / E-signer 三分 | **CLOSED** |
| P1-2 | P1 | allowlist 触发器缺失 | §11 补 3 个触发器 + 配置权威源在服务侧 | **CLOSED** |
| P1-3 | P1 | session INSERT 守卫缺失 | §10 | **CLOSED** |
| P1-4 | P1 | `UNIQUE(decision_id)` 阻止多次尝试 | §10 改 `UNIQUE(decision_id, seq)` | **CLOSED** |
| P2-1 | P2 | `db_verdict` 词表不一致 | §13 统一词表（含 `structurally_valid_unverified`） | **CLOSED** |
| P2-2 | P2 | 空锚点区间与 CHECK 冲突 | §12 CHECK 允许 `leaf_count=0 ∧ end=start-1` | **CLOSED** |
| P2-3 | P2 | 缺 committed 无证据的反向视图 | §15 `v_auth_gaps` 增分支 | **CLOSED** |
| P3-1 | P3 | 迁移序号连续性边界含糊 | §17 明确 1..N / N+1 起 | **CLOSED** |
| P3-2 | P3 | 触发器清单 | §15 逐表列出（auth 20 + trading 3） | **CLOSED** |

**未闭环项（诚实列出）**：R-17（SQLite 序号语义）仍 **OPEN**；R-19（Windows ACL 未实机验证）**OPEN**；R-20（安装者=管理员即 T9 失守）**OPEN**。

---

## 3. 认证库架构选择（核心决策）

### 3.1 四方案比较

| 方案 | 应用能否写认证数据 | 抵御 T2/T3（SQL/DROP TRIGGER） | 抵御 T4（改文件） | 抵御 T5（应用被控） | 复杂度 | 结论 |
|---|---|---|---|---|---|---|
| **A** 共用同一 SQLite | 能 | ❌ | ❌ | ❌ | 低 | 已证明不可行（V3.3–V3.6 六轮） |
| **B** 独立 auth.db，应用只读 | 否（OS ACL） | ✅ | ⚠️ 需备份/替换防护 | ✅（写不了） | 中 | **采用** |
| **C** 服务独持状态，应用经 IPC 查询 | 否（连读都没有） | ✅ | ✅ | ✅ | 高 | 可选强化，见 §6.3 |
| **D** 其他（远端服务） | 否 | ✅ | ✅ | ✅ | 很高 | 超出本机部署范围 |

### 3.2 采用 B（+ 可选 C）

| 库 | 属主 | 应用权限 | 内容 |
|---|---|---|---|
| `auth.db` | `svc_fosun` | **只读**（或不可见，走 IPC） | evidence / session / seal / anchor / allowlist 镜像 / 序号计数器 |
| `trading.db` | 交互用户 | 读写 | decisions / 订单 / 持仓 / 账本 |

**关键点**：`evidence` 属于 **auth.db**（服务写），不在应用库。应用库只存 `evidence_id` 字符串引用（软引用）+ 自己的 claim。**应用即使被完全控制，也写不进一条证据。**

### 3.3 为什么不是"再加几个触发器"

| 手段 | 对 T2（任意 DML） | 对 T3（可 DROP TRIGGER） |
|---|---|---|
| CHECK / 触发器 | 可拦 | ❌ 可删 |
| 独立文件 + NTFS ACL | ✅ 写不进去 | ✅ 除非提权到服务/管理员 |

ACL 是 OS 层强制，触发器只是"防手滑"。六轮的经验是：**不要在触发器里找安全感**。

---

## 4. Windows 身份模型（P0 的地基）

### 4.1 角色与 SID

| 角色 | 主体 | 典型 SID | 说明 |
|---|---|---|---|
| 安装者 | 管理员账户 | `S-1-5-21-<dom>-500` 或 Administrators `S-1-5-32-544` | 执行安装、写 ACL |
| 交互用户 / 应用 | 当前登录用户 | `S-1-5-21-<dom>-1001` | 运行 FosunAI 主程序 |
| signer 服务账户 | 专用本地账户 `.\svc_fosun` | `S-1-5-21-<dom>-1xxx` | 运行 `FosunSigner` 服务，持有密钥 |
| 服务 SID | 服务自身的 SID（sc.exe sidtype） | `S-1-5-80-…` | 用于"该服务"这一主体的 ACL 项 |
| SYSTEM | 本地系统 | `S-1-5-18` | 最高权限，仅安装/修复使用 |

### 4.2 十二问逐条回答

| # | 问题 | 回答 |
|---|---|---|
| 1 | 安装者与应用是否可能同一 SID？ | **会**。单用户机器上，安装者往往就是日常使用者 |
| 2 | 若相同，ACL 是否让应用继承安装者写权限？ | **取决于 ACL 写给了谁**。若写给"安装者 SID"，则应用**继承写权限** —— 这正是 V3.6 的隐性问题。V3.7 的 ACL **只写给 `svc_fosun` 与 SYSTEM**，不写给任何用户 SID |
| 3 | 普通应用能否改 `%PROGRAMDATA%` 中的密钥元数据？ | 默认 `%PROGRAMDATA%` 允许 Users 创建子目录/文件（继承 `CREATOR OWNER`）。**必须在安装时显式移除继承并只授 `svc_fosun`+SYSTEM**，否则应用可写 |
| 4 | 普通应用能否替换 keyring 文件？ | 若目录 ACL 正确（无 Users 写权限）→ **不能**；需 T9 |
| 5 | 普通应用能否创建同名 signer 服务？ | 创建服务需 `SERVICE_START`/管理员权限 → 普通用户**不能**；但**同名 DLL/可执行替换**需目录写权限，同 #4 |
| 6 | 普通应用能否改服务 ImagePath / 启动参数？ | `sc config` 需管理员 → **不能**；能改的是注册表 `HKLM\SYSTEM\CurrentControlSet\Services\...`（需管理员） |
| 7 | 普通应用能否冒充 signer 服务身份？ | 不能伪造 SID 令牌，但**可以创建同名命名管道抢占**（见 §7.3 管道名随机化 + 首实例创建） |
| 8 | signer 服务被普通用户控制时？ | 若攻击者拿到 `svc_fosun` 账户凭据 → 可读密钥、可签任意内容 → **B 等级失守**（T6 变体） |
| 9 | 服务账户被控制时？ | 同上；但**仍不能改 ACL/装服务**（需管理员） |
| 10 | SYSTEM 被控制时？ | 一切皆可伪造；只能靠**外部锚定**发现历史被改 |

### 4.3 ACL 规格（必须以 SID 表述）

```
%PROGRAMDATA%\FosunAI\signer\            (密钥 + keyring + 配置)
  Owner: Administrators
  Inheritance: 已禁用（不继承 %PROGRAMDATA% 的 Users 写权限）
  SYSTEM:                    FullControl
  <SVC_SID>:                 Modify        （仅服务账户，非用户）
  Administrators:            Read          （不给 Write，避免日常管理员误改）

%PROGRAMDATA%\FosunAI\authdb\            (auth.db)
  Owner: Administrators
  Inheritance: 已禁用
  SYSTEM:                    FullControl
  <SVC_SID>:                 Modify
  Users:                     Read          （只读；见 §6.2 WAL 限制）

%PROGRAMDATA%\FosunAI\logs\              (服务审计日志)
  SYSTEM / <SVC_SID>:        Modify
  Users:                     Read
```

安装脚本须断言：`icacls` 输出中 **不含** `BUILTIN\Users:(W)` 或 `(M)`、**不含** `CREATOR OWNER` 继承项。断言失败 → 安装中止。

### 4.4 需要 Windows 实机验证的项

| # | 待验证 |
|---|---|
| W-1 | `%PROGRAMDATA%` 子目录默认继承是否真允许 Users 建文件 |
| W-2 | 禁用继承后 `svc_fosun` 是否仍能写（服务账户需要 "Log on as a service"） |
| W-3 | `sc.exe sidtype FosunSigner unrestricted` 是否成功（需要服务 SID 用于 ACL） |
| W-4 | 只读打开 WAL 库是否需要 `-shm` 写权限（见 §6.2） |
| W-5 | 命名管道首实例创建能否阻止同名抢占 |

---

## 5. 密钥与元数据（P0-1）

### 5.1 三方案比较

| 维度 | A：DPAPI + 用户目录 | **B：DPAPI + 服务账户 + 专属 ACL** | C：CNG/KSP 持久化密钥 |
|---|---|---|---|
| 密钥读取权 | 同用户任意进程 | 仅 `svc_fosun` | 仅获授权的调用方（可配 ACL） |
| 签名权 | 同用户任意进程 | 仅服务 | 仅服务 |
| 元数据修改权 | 同用户（**致命**） | 仅服务 | 仅服务 |
| 同用户攻击 | ❌ 全失守 | ✅ 隔离 | ✅ |
| 跨账户攻击 | ✅ 隔离 | ✅ | ✅ |
| 服务账户被控 | — | ❌ 失守 | ❌ 失守 |
| 密钥轮换 | 手工 | 服务内建 | KSP 支持 |
| 备份/迁移 | 可导出（连带风险） | 可导出（受 ACL） | 需 `certutil`/API |
| 实现复杂度 | 低 | 中 | 高（P/Invoke） |

**选择 B**：比 A 真正隔离，比 C 少一层 P/Invoke 风险；C 作为后续强化（§5.4）。

### 5.2 keyring 的权威来源

```
%PROGRAMDATA%\FosunAI\signer\keyring.json     ← 服务写，应用不可读
{
  "schema": "fosun-keyring-v1",
  "keys": [
    {"key_id":"…16hex…","slot":"signer","created_at":"…","owner_sid":"<SVC_SID>",
     "purpose":"evidence","dpapi_blob":"key-<key_id>.dpapi"}
  ]
}
```

| 规则 | 说明 |
|---|---|
| 谁写 | **只有 signer 服务**（`key-init` 由服务自身执行，不接受客户端指令） |
| 应用可读？ | **不可**（目录 ACL 无 Users 权限） |
| `slot` 由谁定 | 由服务在 `key-init --slot signer` 时写入，**不接受运行时参数** |
| 应用能否伪造 slot | 不能写文件；即使写了也读不到密钥 → MAC 失败 |
| 轮换 | 服务内新增 key 文件 + keyring 追加，旧 key 保留只读 |
| 丢失 | 无法恢复 → 历史证据永久 `mac_unknown_key` |

### 5.3 为什么"元数据不在 DB 里"很重要

V3.6 把 slot 放在 `keys.json`（未定 ACL）。V3.7 把它放进 **仅服务可读写的目录**，且**应用连读都不需要** —— 应用只提交 `key_id`（不可信），由服务在验签时查自己的 keyring。

### 5.4 CNG/KSP 强化（可选）

把密钥改为 `NCryptOpenStorageProvider(MS_KEY_STORAGE_PROVIDER)` + `NCRYPT_MACHINE_KEY_FLAG`，密钥永不导出；签名走 `NCryptSignHash`。ACL 用 `NCryptSetProperty(NCRYPT_ACCESS_DESCRIPTION)` 限定只允许 `svc_fosun`。收益：密钥不可导出；代价：实现复杂度 + 需实机验证。**本期不选，列为 P3 增强。**

---

## 6. 认证数据库边界（P0-2）

### 6.1 两张库

| 项 | `auth.db` | `trading.db` |
|---|---|---|
| 位置 | `%PROGRAMDATA%\FosunAI\authdb\` | 应用数据目录 |
| 写者 | **仅 signer 服务** | 应用 |
| 读者 | 服务；应用只读 | 应用 |
| 表 | allowlist / session / transition / seq_counter / evidence / seal / anchor | decisions / orders / positions / runs |
| journal | **DELETE**（见 §6.2） | WAL |

### 6.2 WAL 的坑（必须写清）

SQLite 只读连接在 **WAL 模式**下仍需要写 `-shm` 文件；若目录只给 Read，会打开失败或退化为只读快照异常。
因此 `auth.db` **不使用 WAL**，改用 `journal_mode=DELETE`（写者少、读多），应用以 `file:auth.db?mode=ro` 打开。
此结论**需实测确认**（§21 W-4）。

### 6.3 应用如何拿到结论

| 方式 | 说明 | 采用 |
|---|---|---|
| 直读 `auth.db`（ro） | 应用自己算 MAC —— 但应用没有 signer 密钥，**算不了** | 仅用于展示原始字段 |
| **IPC 查询 `resolve`** | 服务持密钥计算，返回 `{evidence_id, verdict, grade, reasons}` | **主路径** |
| 服务导出签名结论文件 | 离线审计用 | 辅助 |

**结论**：应用**不能**自行判定 B 等级；`upstream_session_authenticated` 只能由服务返回。

### 6.4 应用被控（T5）能做什么

| 能力 | 结果 |
|---|---|
| 改 `trading.db` 里的 `claim` | 只能改自己的声明；B 结论由服务给 |
| 直写 `auth.db` | **无写权限**（ACL 拒绝） |
| 删除/替换 `auth.db` | 无目录写权限 |
| 调 IPC 请求新调用 | 可以，但每次都是**真实会话**（§7 限流 + 会话绑定） |
| 伪造 `upstream_session_authenticated` | **不能**（拿不到 signer 密钥，也写不了 auth.db） |

---

## 7. IPC 安全

### 7.1 通道与 ACL

| 项 | 规格 |
|---|---|
| 管道名 | `\\.\pipe\FosunAI.Signer.<随机后缀>`，后缀由服务启动时生成并写入 `%PROGRAMDATA%\FosunAI\signer\pipe.txt`（Users 只读） |
| 首实例 | `FILE_FLAG_FIRST_PIPE_INSTANCE`，防同名抢占 |
| DACL | `SYSTEM:F`、`<SVC_SID>:F`、`Users:RW`（仅连接受限操作）；拒绝 `ANONYMOUS` |
| 调用者校验 | `GetNamedPipeClientProcessId` → `OpenProcessToken` → `GetTokenInformation(TokenUser)`；要求 SID == 交互用户 SID（配置白名单），且进程映像路径 ∈ 允许列表 |
| 消息上限 | 64 KiB |
| 并发 | 每 SID 2 个连接；全局 8 |

### 7.2 请求/响应

```json
// 请求
{"op":"call","endpoint_id":"deepseek-primary","decision_id":"d-0001",
 "seq":1,"params":{"model":"deepseek-v4.1-flash","temperature_bps":200,
 "max_tokens":1200,"messages_digest":"<64hex>","messages_bytes":8743},
 "client_nonce":"<32hex>"}
// 响应（成功）
{"ok":true,"session_id":"s-…","evidence_id":"<64hex>","verdict":"upstream_session_unverified",
 "grade":"signer","reasons":["anchor_stale"],"latency_ms":8478}
```

| 规则 | 说明 |
|---|---|
| nonce | **服务生成** `session_nonce`；`client_nonce` 仅用于幂等去重（10 min LRU） |
| decision_id + seq | 客户端提交，但在**创建会话时固化**，之后不可改 |
| 参数 | 服务校验 `model ∈ allowlist.allowed_models`、`messages_bytes ≤ 上限` |
| 摘要 | 服务自己序列化 messages、自己算 `messages_digest`，**不信任客户端** |
| 拒绝 | `{"ok":false,"code":"rate_limited|model_not_allowed|endpoint_disabled|…"}` |

### 7.3 应用能否要求服务签任意内容

不能：服务只接受"参数 + endpoint_id"，**自己构造请求体、自己发送、自己收响应**，签名对象是它观测到的一切。客户端提交的 `messages_digest` 只用于一致性校验，不进入签名值。

---

## 8. HMAC 规范（沿用 v2，64 字段）

字段清单、编码规则、示例与 V3.6 §5 相同，**此处不重复**（V3.6 的 64 字段与 §5.5 分类已通过第六轮审计的 P1-1 复核）。V3.7 的改动仅在"谁来算"：

| 字段组 | A 架构 | B 架构 |
|---|---|---|
| 身份/会话 | 应用填 | **服务填**（session_id/nonce/signer_identity） |
| 请求侧 16 项 | 应用填 | **服务填**（它自己序列化与发送） |
| 传输观测 | 应用填 | **服务填** |
| 响应侧 11 项 | 应用填 | **服务填**（它自己接收字节） |
| 结论/链 | 应用填 | 服务填 |

> 因此 A 的 MAC 只证明"应用声明的一致性"，B 的 MAC 证明"服务观测的一致性"。这就是两套命名空间的物理依据。

---

## 9. A / B 规则拆分（P1-1）

### 9.1 三条约束

| 约束 | 适用 | 内容 |
|---|---|---|
| **E-base** | `attempt_verdict='real_llm'` | 结构性与响应侧：http_status=200、response_body_digest 64、upstream_response_id 非空且 source='body.id'、upstream_model 非空、usage>0、redirect_count=0、evidence_mac 64、key_id 16、schema_version='v2'、时间窗 |
| **E-app** | 且 `signing_architecture='app_in_process'` | 允许 `request_body_digest` 可空（A 无服务观测）、`dns_resolved_count` 可 0、`tls_cert_sha256` 可空；**不要求 session** |
| **E-signer** | 且 `signing_architecture='signer_service'` | 追加：`transport_path='direct_verified'`、`peer_class='remote'`、`peer_ip_global=1`、`dns_resolved_count>0`、`length(request_body_digest)=64`、`length(request_body_bytes)>0`、`length(model_params_digest)=64`、`endpoint_id/target_host/tls_sni` 非空、`tls_verified=1`、`tls_hostname_verified=1`、`length(tls_cert_sha256)=64`、`tls_pin_status='matched'` 且 `expected=spki`、`pin_verification_status='performed_match'`、`proxy_mode='none'`、`length(session_id)>0`、`length(signer_identity)>0` |

**关键差异**：A 可以在 `trading.db` 里声明 real_llm 并拿到 `app_record_authenticated`；B 才能在 `auth.db` 里存在，且必须满足 E-signer 全部条件。

### 9.2 为什么 DB 不能决定等级

`signing_architecture` 是**存储值**，理论上可被写。但：

- A 的记录在 `trading.db`（应用可写）→ 服务从不承认其 B 等级；
- B 的记录在 `auth.db`（服务独占写）→ 应用改不了；
- 因此**等级由"记录在哪个库"这一物理事实决定**，而不是由字段值决定。

字段仅用于审计对账：若 `auth.db` 中出现 `signing_architecture='app_in_process'`，服务启动自检报 `integrity_violation` 并拒绝服务。

---

## 10. 会话与多次尝试（P1-3、P1-4）

### 10.1 状态机（11 态，INSERT 只能生于 created）

```
created → request_bound → sent → response_received → signed → committed
   └────────┴──────────────┴─────────────┴────────────┴────→ rejected / failed / expired / abandoned / persistence_failed
```

### 10.2 会话键

| 约束 | 说明 |
|---|---|
| `UNIQUE(decision_id, seq)` | 同一决策可多次尝试（seq=1,2,3），每次一个会话 |
| `UNIQUE(session_id)` | 主键 |
| `UNIQUE(nonce)` | 服务生成，一次性 |
| `UNIQUE(evidence_id)` | 一会话至多一份证据 |
| `UNIQUE(response_digest)` | **同一响应不得跨会话复用**（NULL 允许多行） |

### 10.3 INSERT 守卫

- `BEFORE INSERT`：`NEW.state <> 'created'` → ABORT；
- 出生时 `request_digest/response_digest/evidence_id` 必须为 NULL；
- `expires_at > created_at`；
- `signer_identity` 必须等于服务自身标识（由服务写入，弱校验）。

### 10.4 多次尝试的语义

| 场景 | 结果 |
|---|---|
| seq=1 rejected | 会话 `failed`，无证据 |
| seq=2 timeout | 会话 `abandoned`，无证据 |
| seq=3 成功 | 会话 `committed`，一份证据 |
| 决策的 `authoritative_evidence_id` | 指向 seq=3 |
| B 等级 | 只看那一条证据，不看失败尝试 |

---

## 11. allowlist（P1-2）

### 11.1 权威源

```
%PROGRAMDATA%\FosunAI\signer\allowlist.json    ← 服务读，应用不可写
{
  "schema":"fosun-allowlist-v1",
  "entries":[{"endpoint_id":"deepseek-primary","scheme":"https","host":"api.example.com",
              "port":443,"path_prefix":"/v1/chat/completions",
              "allowed_models":["deepseek-v4.1-flash"],
              "pin_expected":"<64hex>","enabled":true}]
}
```

| 规则 | 说明 |
|---|---|
| 谁能改 | 仅安装/升级流程（管理员）写文件；服务只读 |
| 应用能否改 | 无目录写权限 → 不能 |
| 数据库镜像 | `auth.db.ai_endpoint_allowlist` 由服务启动时同步，**应用只读** |
| 三道防线 | ① 文件 ACL；② DB 触发器 no-update/no-delete/guard-ins；③ 服务启动校验文件与镜像一致 |

### 11.2 补的 3 个触发器

`trg_allowlist_no_update`、`trg_allowlist_no_delete`、`trg_allowlist_guard_ins`（见 §15）。

---

## 12. 全局序号与锚定（P1-3、P2-2）

### 12.1 序号

`auth.db.ai_seq_counter(id=1, next_seq)`。分配在服务的事务内：`UPDATE ... SET next_seq=next_seq+1; SELECT next_seq;` 然后插入。

| 性质 | 结论 |
|---|---|
| 单调 | 每次 +1 |
| 空洞 | 回滚同事务 → 无空洞 |
| 并发 | `BEGIN IMMEDIATE` 串行化 |
| 迁移 | 历史行 1..N（按 `(decision_id,seq)` 排序），计数器初始化为 N+1 |
| 新旧边界 | `record_origin` + `schema_version`（历史 v1 无 MAC） |

**R-17 保持 OPEN**：以上均为设计推导，未验证。

### 12.2 锚点

| 字段 | 说明 |
|---|---|
| `start_seq` / `end_seq` | 全局序号闭区间；**允许空区间** `end = start-1` |
| `leaf_count` | 空区间为 0 |
| `root` | 空区间 = `sha256("FOSUN-EMPTY:v1")` |
| `prev_root` / `anchor_sig` | 链 + 密钥签名（锚点密钥与证据密钥分开） |

DDL：

```sql
CHECK ( (leaf_count = 0 AND end_seq = start_seq - 1)
     OR (leaf_count = end_seq - start_seq + 1) )
```

叶子/节点编码、奇偶处理沿用 V3.6 §10.1（`sha256("FOSUN-LEAF:v1\n"+seq+"\n"+mac)`、双写末叶、`sha256("FOSUN-NODE:v1\n"+l+r)`）。

---

## 13. `struct_ok` 与 readiness

### 13.1 struct_ok（14 项，替换 V3.6 的 12 项）

| # | 检查 | 层 |
|---|---|---|
| S-01 | `schema_version ∈ {v1,v2}` | APP |
| S-02 | 所有 NOT NULL 非空（读回复核） | DB+APP |
| S-03 | 枚举合法 | DB |
| S-04 | `unverified_reasons` 合法数组且元素白名单 | DB+APP |
| S-05 | 决策指向的 `evidence_id` 在 `auth.db` 中存在 | APP（跨库） |
| S-06 | 证据 `decision_id` 与决策一致 | APP |
| S-07 | seal 存在且 `seal_hash = evidence.integrity_hash` | DB+APP |
| S-08 | `prev_hash` 等于同决策上一条 `integrity_hash` | APP |
| S-09 | `global_evidence_seq` 唯一；迁移段 1..N 连续、在线段从 N+1 起连续 | APP |
| S-10 | migrated 行：`schema_version='v1'`、无 MAC、无 session | DB+APP |
| S-11 | 无孤儿证据（evidence 无 seal / seal 无 evidence） | APP（视图） |
| S-12 | B 行：`session_id` 存在、`state='committed'`、`decision_id`/`evidence_id` 双向匹配 | APP |
| **S-13** | B 行所在库为 `auth.db`（由连接串决定，非字段） | APP |
| **S-14** | 会话 `seq` 与证据 `seq` 相等 | APP |

**输入**：`(evidence, decision, seal, session, db_role)`；**输出**：`(ok, failed[list])`；异常 → `(False, ["S-00_read_error"])`。

### 13.2 词表（P2-1）

| 状态 | 谁产生 | 含义 |
|---|---|---|
| `app_record_authenticated` | APP | A 库记录，MAC ok，结构 ok |
| `app_record_unverified` | APP | A 库，MAC 缺失/未知 |
| `upstream_session_authenticated` | **SIGNER** | B 库，MAC ok + 结构 ok + 锚定 current + 会话 committed |
| `upstream_session_unverified` | SIGNER | B 库但锚定 stale / 会话信息不全 |
| `structurally_valid_unverified` | APP/DB | 结构 ok 但无法认证（**替换 V3.6 的 `unverified_by_db`**） |
| `legacy_unverified` / `integrity_violation` / `local_llm` / `stub_transport` / `rejected` / `unavailable` / `unknown` / `voided` | 各自 | — |

**禁用**：`real_llm_authenticated`、`model_identity_verified`、`inference_proven`。

### 13.3 readiness

```
if voided → voided
if migrated → legacy_unverified
if !struct_ok → integrity_violation
if claim != real_llm → claim
if source_db == trading.db  → app_record_*   (A)
if source_db == auth.db     → signer IPC 查询 → upstream_session_*
     ├ mac_missing/unknown_key → upstream_session_unverified
     ├ mac_mismatch → integrity_violation
     ├ session not committed → upstream_session_unverified
     └ anchor mismatch → integrity_violation；stale → unverified
```

---

## 14. 完整 DDL

### 14.1 `auth.db`（服务写，应用只读）

```sql
PRAGMA journal_mode = DELETE;      -- 不用 WAL，见 §6.2
PRAGMA foreign_keys = ON;

CREATE TABLE ai_endpoint_allowlist (
    endpoint_id    TEXT PRIMARY KEY NOT NULL CHECK (length(endpoint_id) > 0),
    scheme         TEXT NOT NULL CHECK (scheme = 'https'),
    host           TEXT NOT NULL CHECK (length(host) > 0
                     AND host NOT GLOB '*[^a-zA-Z0-9.\-]*'),
    port           INTEGER NOT NULL CHECK (port BETWEEN 1 AND 65535),
    path_prefix    TEXT NOT NULL CHECK (length(path_prefix) > 0),
    allowed_models TEXT NOT NULL DEFAULT '[]'
                   CHECK (json_valid(allowed_models) AND json_type(allowed_models)='array'),
    pin_expected   TEXT NOT NULL DEFAULT ''
                   CHECK (pin_expected='' OR (length(pin_expected)=64
                      AND pin_expected NOT GLOB '*[^0-9a-f]*')),
    enabled        INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1))
);

CREATE TABLE ai_sign_session (
    session_id      TEXT PRIMARY KEY NOT NULL CHECK (length(session_id) > 0),
    decision_id     TEXT NOT NULL CHECK (length(decision_id) > 0),
    seq             INTEGER NOT NULL CHECK (seq >= 1),
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
    response_digest TEXT UNIQUE,
    evidence_id     TEXT UNIQUE,
    closed_reason   TEXT,
    UNIQUE (decision_id, seq),
    CHECK (state <> 'committed' OR evidence_id IS NOT NULL),
    CHECK (state <> 'signed' OR (request_digest IS NOT NULL AND response_digest IS NOT NULL)),
    CHECK (state <> 'created' OR (request_digest IS NULL AND response_digest IS NULL
                                  AND evidence_id IS NULL))
);

CREATE TABLE ai_session_transition (
    from_state TEXT NOT NULL,
    to_state   TEXT NOT NULL,
    PRIMARY KEY (from_state, to_state)
);

CREATE TABLE ai_seq_counter (
    id       INTEGER PRIMARY KEY CHECK (id = 1),
    next_seq INTEGER NOT NULL CHECK (next_seq >= 1)
);

CREATE TABLE ai_transport_evidence (
    evidence_id     TEXT PRIMARY KEY NOT NULL
                    CHECK (length(evidence_id)=64 AND evidence_id NOT GLOB '*[^0-9a-f]*'),
    decision_id     TEXT NOT NULL CHECK (length(decision_id) > 0),
    seq             INTEGER NOT NULL CHECK (seq >= 1),
    global_evidence_seq INTEGER NOT NULL UNIQUE CHECK (global_evidence_seq >= 1),
    session_id      TEXT NOT NULL CHECK (length(session_id) > 0),
    request_id      TEXT,
    signing_architecture TEXT NOT NULL CHECK (signing_architecture = 'signer_service'),
    signer_identity TEXT NOT NULL CHECK (length(signer_identity) > 0),
    key_id          TEXT NOT NULL CHECK (length(key_id) = 16
                      AND key_id NOT GLOB '*[^0-9a-f]*'),
    endpoint_id     TEXT NOT NULL CHECK (length(endpoint_id) > 0),
    target_scheme   TEXT NOT NULL CHECK (target_scheme='https'),
    target_host     TEXT NOT NULL CHECK (length(target_host) > 0),
    target_port     INTEGER NOT NULL CHECK (target_port BETWEEN 1 AND 65535),
    request_path    TEXT NOT NULL CHECK (length(request_path) > 0),
    http_method     TEXT NOT NULL CHECK (http_method='POST'),
    request_encoding TEXT NOT NULL DEFAULT 'utf-8' CHECK (request_encoding='utf-8'),
    request_body_digest TEXT NOT NULL CHECK (length(request_body_digest)=64
                      AND request_body_digest NOT GLOB '*[^0-9a-f]*'),
    request_body_bytes INTEGER NOT NULL CHECK (request_body_bytes > 0),
    model_requested TEXT NOT NULL CHECK (length(model_requested) > 0),
    model_params_digest TEXT NOT NULL CHECK (length(model_params_digest)=64
                      AND model_params_digest NOT GLOB '*[^0-9a-f]*'),
    temperature_bps INTEGER NOT NULL DEFAULT 0 CHECK (temperature_bps BETWEEN 0 AND 20000),
    max_tokens      INTEGER NOT NULL DEFAULT 0 CHECK (max_tokens >= 0),
    stream_flag     INTEGER NOT NULL DEFAULT 0 CHECK (stream_flag IN (0,1)),
    request_nonce   TEXT NOT NULL CHECK (length(request_nonce)=32),
    request_timestamp TEXT NOT NULL CHECK (length(request_timestamp) > 0),
    endpoint_class  TEXT NOT NULL CHECK (endpoint_class IN
                      ('remote','loopback','private','unknown')),
    transport_path  TEXT NOT NULL CHECK (transport_path IN
                      ('direct_verified','proxy_unverified','local','private','unknown')),
    dns_resolved_digest TEXT NOT NULL CHECK (length(dns_resolved_digest)=64),
    dns_resolved_count INTEGER NOT NULL CHECK (dns_resolved_count > 0),
    peer_ip         TEXT NOT NULL CHECK (length(peer_ip) > 0),
    peer_class      TEXT NOT NULL CHECK (peer_class IN ('remote','loopback','private','unknown')),
    peer_role       TEXT NOT NULL CHECK (peer_role IN ('upstream','proxy','unknown')),
    peer_ip_global  INTEGER NOT NULL CHECK (peer_ip_global IN (0,1)),
    tls_sni         TEXT NOT NULL CHECK (length(tls_sni) > 0),
    tls_verified    INTEGER NOT NULL CHECK (tls_verified IN (0,1)),
    tls_hostname_verified INTEGER NOT NULL CHECK (tls_hostname_verified IN (0,1)),
    tls_cert_sha256 TEXT NOT NULL CHECK (length(tls_cert_sha256)=64),
    tls_spki_sha256 TEXT NOT NULL CHECK (length(tls_spki_sha256)=64),
    tls_pin_expected TEXT NOT NULL CHECK (length(tls_pin_expected)=64),
    tls_pin_status  TEXT NOT NULL CHECK (tls_pin_status IN
                      ('not_configured','matched','mismatched','not_checked')),
    pin_verification_status TEXT NOT NULL CHECK (pin_verification_status IN
                      ('performed_match','performed_mismatch','not_performed','unsupported')),
    pin_verification_evidence TEXT NOT NULL CHECK (length(pin_verification_evidence)=64),
    proxy_mode      TEXT NOT NULL DEFAULT 'none'
                    CHECK (proxy_mode IN ('none','env','explicit','unknown')),
    proxy_endpoint_digest TEXT NOT NULL DEFAULT ''
                    CHECK (proxy_endpoint_digest='' OR (length(proxy_endpoint_digest)=64
                      AND proxy_endpoint_digest NOT GLOB '*[^0-9a-f]*')),
    proxy_peer_ip   TEXT,
    proxy_env_present INTEGER NOT NULL DEFAULT 0 CHECK (proxy_env_present IN (0,1)),
    http_status     INTEGER NOT NULL CHECK (http_status BETWEEN 0 AND 599),
    resp_headers_digest TEXT NOT NULL CHECK (length(resp_headers_digest)=64),
    response_body_digest TEXT NOT NULL CHECK (length(response_body_digest)=64),
    response_body_bytes INTEGER NOT NULL CHECK (response_body_bytes > 0),
    upstream_response_id TEXT NOT NULL CHECK (length(upstream_response_id) > 0),
    upstream_response_id_source TEXT NOT NULL CHECK (upstream_response_id_source='body.id'),
    upstream_model  TEXT NOT NULL CHECK (length(upstream_model) > 0),
    usage_prompt_tokens INTEGER NOT NULL CHECK (usage_prompt_tokens > 0),
    usage_completion_tokens INTEGER NOT NULL CHECK (usage_completion_tokens >= 0),
    redirect_count  INTEGER NOT NULL DEFAULT 0 CHECK (redirect_count BETWEEN 0 AND 3),
    redirect_chain_digest TEXT NOT NULL DEFAULT ''
                    CHECK (redirect_chain_digest='' OR (length(redirect_chain_digest)=64
                      AND redirect_chain_digest NOT GLOB '*[^0-9a-f]*')),
    received_at     TEXT NOT NULL CHECK (length(received_at) > 0),
    unix_received_at INTEGER NOT NULL CHECK (unix_received_at > 0),
    created_at      TEXT NOT NULL CHECK (length(created_at) > 0),
    unix_created_at INTEGER NOT NULL CHECK (unix_created_at > 0),
    attempt_verdict TEXT NOT NULL CHECK (attempt_verdict IN
                      ('real_llm','real_llm_unverified','local_llm',
                       'stub_transport','rejected','unavailable','unknown')),
    unverified_reasons TEXT NOT NULL DEFAULT '[]'
                    CHECK (json_valid(unverified_reasons) AND json_type(unverified_reasons)='array'),
    schema_version  TEXT NOT NULL DEFAULT 'v2' CHECK (schema_version='v2'),
    mac_algorithm   TEXT NOT NULL DEFAULT 'hmac-sha256' CHECK (mac_algorithm='hmac-sha256'),
    evidence_mac    TEXT NOT NULL CHECK (length(evidence_mac)=64
                      AND evidence_mac NOT GLOB '*[^0-9a-f]*'),
    mac_signed_at   TEXT NOT NULL CHECK (length(mac_signed_at) > 0),
    integrity_hash  TEXT NOT NULL CHECK (length(integrity_hash)=64
                      AND integrity_hash NOT GLOB '*[^0-9a-f]*'),
    prev_hash       TEXT NOT NULL DEFAULT ''
                    CHECK (prev_hash='' OR (length(prev_hash)=64
                      AND prev_hash NOT GLOB '*[^0-9a-f]*')),
    UNIQUE (decision_id, seq),
    FOREIGN KEY (session_id) REFERENCES ai_sign_session(session_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT DEFERRABLE INITIALLY DEFERRED,

    /* E-signer：B 的完整条件 */
    CHECK (attempt_verdict <> 'real_llm' OR (
            http_status = 200
        AND transport_path='direct_verified' AND endpoint_class='remote'
        AND peer_role='upstream' AND peer_class='remote' AND peer_ip_global=1
        AND dns_resolved_count > 0 AND length(dns_resolved_digest)=64
        AND tls_verified=1 AND tls_hostname_verified=1
        AND length(tls_cert_sha256)=64 AND length(tls_spki_sha256)=64
        AND tls_pin_status='matched' AND tls_pin_expected=tls_spki_sha256
        AND pin_verification_status='performed_match'
        AND length(pin_verification_evidence)=64
        AND proxy_mode='none' AND proxy_peer_ip IS NULL
        AND length(request_body_digest)=64 AND request_body_bytes > 0
        AND length(model_params_digest)=64 AND length(model_requested) > 0
        AND length(endpoint_id) > 0 AND length(target_host) > 0 AND length(tls_sni) > 0
        AND length(response_body_digest)=64 AND response_body_bytes > 0
        AND length(upstream_response_id) > 0 AND upstream_response_id_source='body.id'
        AND length(upstream_model) > 0 AND usage_prompt_tokens > 0
        AND redirect_count = 0
        AND length(session_id) > 0 AND length(signer_identity) > 0
        AND length(evidence_mac)=64 AND schema_version='v2'
        AND unix_created_at >= unix_received_at
        AND unix_created_at - unix_received_at <= 5
    )),
    CHECK (transport_path<>'direct_verified' OR endpoint_class='remote'),
    CHECK (transport_path<>'direct_verified' OR peer_role='upstream'),
    CHECK (peer_class<>'remote' OR peer_ip_global=1),
    CHECK (proxy_mode<>'none' OR proxy_peer_ip IS NULL),
    CHECK (proxy_mode<>'env' OR proxy_env_present=1),
    CHECK (attempt_verdict<>'real_llm_unverified' OR json_array_length(unverified_reasons)>0),
    CHECK (attempt_verdict<>'local_llm' OR transport_path IN ('local','private'))
);

CREATE TABLE ai_evidence_seal (
    decision_id TEXT NOT NULL,
    seq         INTEGER NOT NULL CHECK (seq >= 1),
    evidence_id TEXT NOT NULL CHECK (length(evidence_id)=64),
    sealed_at   TEXT NOT NULL,
    seal_hash   TEXT NOT NULL CHECK (length(seal_hash)=64),
    PRIMARY KEY (decision_id, seq),
    UNIQUE (evidence_id)
);

CREATE TABLE ai_anchor (
    anchor_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    start_seq   INTEGER NOT NULL CHECK (start_seq >= 1),
    end_seq     INTEGER NOT NULL CHECK (end_seq >= 0),
    leaf_count  INTEGER NOT NULL CHECK (leaf_count >= 0),
    anchor_root TEXT NOT NULL CHECK (length(anchor_root)=64),
    prev_anchor_root TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    anchor_key_id TEXT NOT NULL,
    anchor_signature TEXT NOT NULL,
    external_ref TEXT NOT NULL DEFAULT '',
    CHECK ((leaf_count = 0 AND end_seq = start_seq - 1)
        OR (leaf_count = end_seq - start_seq + 1))
);

CREATE INDEX ix_ev_session ON ai_transport_evidence(session_id);
CREATE INDEX ix_ev_global  ON ai_transport_evidence(global_evidence_seq);
```

### 14.2 `trading.db`（应用写）

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE ai_decisions (
    decision_id   TEXT PRIMARY KEY NOT NULL
                  CHECK (length(decision_id) > 0
                     AND decision_id NOT GLOB '*[^0-9a-zA-Z_.:-]*'),
    run_id        TEXT,
    symbol        TEXT NOT NULL CHECK (length(symbol) > 0),
    decided_at    TEXT NOT NULL CHECK (length(decided_at) > 0),
    provenance    TEXT NOT NULL CHECK (provenance IN
                    ('real_llm','real_llm_unverified','local_llm',
                     'stub_transport','rejected','unavailable','unknown')),
    evidence_id   TEXT,                       -- 软引用 auth.db
    record_origin TEXT NOT NULL DEFAULT 'live'
                  CHECK (record_origin IN ('live','migrated')),
    voided_at     TEXT,
    void_reason   TEXT,
    created_at    TEXT NOT NULL CHECK (length(created_at) > 0),
    CHECK ((voided_at IS NULL AND void_reason IS NULL)
        OR (voided_at IS NOT NULL AND length(void_reason) > 0)),
    CHECK (provenance <> 'real_llm' OR record_origin='migrated' OR evidence_id IS NOT NULL)
);

CREATE INDEX ix_dec_evidence ON ai_decisions(evidence_id);

-- A 架构的证据（应用写；MAC 用 app 密钥；最多 app_record_*）
CREATE TABLE ai_app_evidence (
    evidence_id    TEXT PRIMARY KEY NOT NULL
                   CHECK (length(evidence_id)=64 AND evidence_id NOT GLOB '*[^0-9a-f]*'),
    decision_id    TEXT NOT NULL CHECK (length(decision_id) > 0),
    seq            INTEGER NOT NULL CHECK (seq >= 1),
    signing_architecture TEXT NOT NULL CHECK (signing_architecture='app_in_process'),
    key_id         TEXT NOT NULL CHECK (length(key_id)=16
                     AND key_id NOT GLOB '*[^0-9a-f]*'),
    http_status    INTEGER NOT NULL CHECK (http_status BETWEEN 0 AND 599),
    request_path   TEXT NOT NULL CHECK (length(request_path) > 0),
    request_body_digest TEXT NOT NULL DEFAULT ''
                   CHECK (request_body_digest='' OR length(request_body_digest)=64),
    response_body_digest TEXT NOT NULL CHECK (length(response_body_digest)=64),
    response_body_bytes  INTEGER NOT NULL CHECK (response_body_bytes > 0),
    upstream_response_id TEXT NOT NULL CHECK (length(upstream_response_id) > 0),
    upstream_response_id_source TEXT NOT NULL CHECK (upstream_response_id_source='body.id'),
    upstream_model TEXT NOT NULL CHECK (length(upstream_model) > 0),
    usage_prompt_tokens INTEGER NOT NULL CHECK (usage_prompt_tokens > 0),
    attempt_verdict TEXT NOT NULL CHECK (attempt_verdict IN
                     ('real_llm','real_llm_unverified','rejected','unknown')),
    unverified_reasons TEXT NOT NULL DEFAULT '[]'
                     CHECK (json_valid(unverified_reasons) AND json_type(unverified_reasons)='array'),
    schema_version TEXT NOT NULL DEFAULT 'v2' CHECK (schema_version='v2'),
    evidence_mac   TEXT NOT NULL CHECK (length(evidence_mac)=64
                     AND evidence_mac NOT GLOB '*[^0-9a-f]*'),
    created_at     TEXT NOT NULL CHECK (length(created_at) > 0),
    UNIQUE (decision_id, seq),

    /* E-base + E-app：A 的 real_llm 约束（不含 signer 网络/会话项） */
    CHECK (attempt_verdict <> 'real_llm' OR (
            http_status = 200
        AND length(response_body_digest)=64 AND response_body_bytes > 0
        AND length(upstream_response_id) > 0 AND upstream_response_id_source='body.id'
        AND length(upstream_model) > 0 AND usage_prompt_tokens > 0
        AND length(evidence_mac)=64 AND length(key_id)=16
        AND schema_version='v2' AND signing_architecture='app_in_process'
    )),
    CHECK (attempt_verdict <> 'real_llm_unverified'
        OR json_array_length(unverified_reasons) > 0)
);

-- 历史证据（v1）：宽表，宽松约束，永不参与认证
CREATE TABLE ai_legacy_evidence (
    evidence_id    TEXT PRIMARY KEY,
    decision_id    TEXT NOT NULL,
    seq            INTEGER NOT NULL,
    raw_json       TEXT NOT NULL,
    migrated_at    TEXT NOT NULL,
    source_schema  TEXT NOT NULL DEFAULT 'v1'
);
```

---

## 15. 触发器与视图

### 15.1 `auth.db` 触发器（**20 个**）

| # | 名称 | 事件/对象 | 作用 |
|---|---|---|---|
| 1 | trg_ev_seal_guard_ins | BI/evidence | 槽位已封则拒 |
| 2 | trg_ev_seal_after_ins | AI/evidence | 写封印 |
| 3 | trg_ev_no_update | BU/evidence | 证据不可改 |
| 4 | trg_ev_no_delete | BD/evidence | 证据不可删 |
| 5 | trg_ev_chain_ins | BI/evidence | 链连续 |
| 6 | trg_ev_json_ins | BI/evidence | reasons 白名单 |
| 7 | trg_ev_session_ins | BI/evidence | 会话必须 committed |
| 8 | trg_ev_global_seq_ins | BI/evidence | 序号等于计数器 |
| 9 | trg_ev_seq_match_ins | BI/evidence | 会话 seq 与证据 seq 相等 |
| 10 | trg_session_born_created | BI/session | **只能生于 created** |
| 11 | trg_session_transition | BU/session | 白名单迁移 |
| 12 | trg_session_no_delete | BD/session | 会话不可删 |
| 13 | trg_seal_no_update | BU/seal | 禁改 |
| 14 | trg_seal_no_delete | BD/seal | 禁删 |
| 15 | trg_seal_guard_ins | BI/seal | 禁重复占位 |
| 16 | trg_allowlist_no_update | BU/allowlist | 禁改 |
| 17 | trg_allowlist_no_delete | BD/allowlist | 禁删 |
| 18 | trg_allowlist_guard_ins | BI/allowlist | 禁重复 endpoint_id |
| 19 | trg_anchor_no_update | BU/anchor | 锚点不可改 |
| 20 | trg_anchor_no_delete | BD/anchor | 锚点不可删 |

`trading.db` 触发器（**3 个**）：`trg_dec_claim_ins`、`trg_dec_claim_upd`（声明 real_llm 必须有 evidence_id 且该 id 非空——**不校验存在性，跨库无法校验**，存在性由 APP 查询 auth.db 保证）、`trg_dec_no_delete`。

```sql
-- auth.db：会话只能生于 created
CREATE TRIGGER trg_session_born_created
BEFORE INSERT ON ai_sign_session
WHEN NEW.state <> 'created'
BEGIN SELECT RAISE(ABORT,'session must be born in created'); END;

CREATE TRIGGER trg_session_transition
BEFORE UPDATE OF state ON ai_sign_session
WHEN NOT EXISTS (SELECT 1 FROM ai_session_transition t
                  WHERE t.from_state=OLD.state AND t.to_state=NEW.state)
BEGIN SELECT RAISE(ABORT,'illegal session transition'); END;

CREATE TRIGGER trg_session_no_delete
BEFORE DELETE ON ai_sign_session
BEGIN SELECT RAISE(ABORT,'sessions append-only'); END;

CREATE TRIGGER trg_ev_session_ins
BEFORE INSERT ON ai_transport_evidence
WHEN NOT EXISTS (SELECT 1 FROM ai_sign_session s
                  WHERE s.session_id=NEW.session_id
                    AND s.state IN ('signed','committed')   -- 见 R-21：committed 在证据落库后才置
                    AND s.decision_id=NEW.decision_id)
BEGIN SELECT RAISE(ABORT,'evidence requires signed/committed session'); END;

CREATE TRIGGER trg_ev_seq_match_ins
BEFORE INSERT ON ai_transport_evidence
WHEN NEW.seq <> (SELECT s.seq FROM ai_sign_session s WHERE s.session_id=NEW.session_id)
BEGIN SELECT RAISE(ABORT,'evidence seq must equal session seq'); END;

CREATE TRIGGER trg_ev_global_seq_ins
BEFORE INSERT ON ai_transport_evidence
WHEN NEW.global_evidence_seq <> (SELECT next_seq FROM ai_seq_counter WHERE id=1)
  OR NEW.global_evidence_seq <= COALESCE((SELECT MAX(global_evidence_seq)
       FROM ai_transport_evidence),0)
BEGIN SELECT RAISE(ABORT,'global seq out of order'); END;

CREATE TRIGGER trg_allowlist_no_update
BEFORE UPDATE ON ai_endpoint_allowlist
BEGIN SELECT RAISE(ABORT,'allowlist is read-only mirror'); END;

CREATE TRIGGER trg_allowlist_no_delete
BEFORE DELETE ON ai_endpoint_allowlist
BEGIN SELECT RAISE(ABORT,'allowlist is read-only mirror'); END;

CREATE TRIGGER trg_allowlist_guard_ins
BEFORE INSERT ON ai_endpoint_allowlist
WHEN EXISTS (SELECT 1 FROM ai_endpoint_allowlist a WHERE a.endpoint_id=NEW.endpoint_id)
BEGIN SELECT RAISE(ABORT,'allowlist endpoint exists'); END;
```

```sql
CREATE TRIGGER trg_ev_seal_guard_ins
BEFORE INSERT ON ai_transport_evidence
WHEN EXISTS (SELECT 1 FROM ai_evidence_seal s
              WHERE (s.decision_id=NEW.decision_id AND s.seq=NEW.seq)
                 OR s.evidence_id=NEW.evidence_id)
BEGIN SELECT RAISE(ABORT,'slot already sealed'); END;

CREATE TRIGGER trg_ev_seal_after_ins
AFTER INSERT ON ai_transport_evidence
BEGIN
  INSERT INTO ai_evidence_seal(decision_id,seq,evidence_id,sealed_at,seal_hash)
  VALUES (NEW.decision_id,NEW.seq,NEW.evidence_id,NEW.created_at,NEW.integrity_hash);
END;

CREATE TRIGGER trg_ev_no_update
BEFORE UPDATE ON ai_transport_evidence
BEGIN SELECT RAISE(ABORT,'evidence append-only'); END;

CREATE TRIGGER trg_ev_no_delete
BEFORE DELETE ON ai_transport_evidence
BEGIN SELECT RAISE(ABORT,'evidence append-only'); END;

CREATE TRIGGER trg_ev_chain_ins
BEFORE INSERT ON ai_transport_evidence
WHEN (NEW.seq=1 AND NEW.prev_hash<>'')
  OR (NEW.seq>1 AND NEW.prev_hash<>COALESCE((
       SELECT p.integrity_hash FROM ai_transport_evidence p
        WHERE p.decision_id=NEW.decision_id AND p.seq=NEW.seq-1),'~'))
BEGIN SELECT RAISE(ABORT,'chain broken'); END;

CREATE TRIGGER trg_ev_json_ins
BEFORE INSERT ON ai_transport_evidence
WHEN EXISTS (SELECT 1 FROM json_each(NEW.unverified_reasons) j
              WHERE j.type<>'text' OR trim(j.value)='' OR j.value NOT IN (
                'missing_upstream_id','missing_usage','envelope_digest_failed',
                'proxy_path','status_not_200','tls_verify_failed','tls_pin_mismatch',
                'pin_not_performed','mac_missing','mac_invalid','anchor_stale',
                'model_mismatch','transport_unverified','response_unparsable',
                'session_incomplete','other'))
BEGIN SELECT RAISE(ABORT,'invalid reason'); END;

CREATE TRIGGER trg_seal_no_update
BEFORE UPDATE ON ai_evidence_seal
BEGIN SELECT RAISE(ABORT,'seal append-only'); END;

CREATE TRIGGER trg_seal_no_delete
BEFORE DELETE ON ai_evidence_seal
BEGIN SELECT RAISE(ABORT,'seal append-only'); END;

CREATE TRIGGER trg_seal_guard_ins
BEFORE INSERT ON ai_evidence_seal
WHEN EXISTS (SELECT 1 FROM ai_evidence_seal s
              WHERE (s.decision_id=NEW.decision_id AND s.seq=NEW.seq)
                 OR s.evidence_id=NEW.evidence_id)
BEGIN SELECT RAISE(ABORT,'seal slot occupied'); END;

CREATE TRIGGER trg_anchor_no_update
BEFORE UPDATE ON ai_anchor
BEGIN SELECT RAISE(ABORT,'anchor append-only'); END;

CREATE TRIGGER trg_anchor_no_delete
BEFORE DELETE ON ai_anchor
BEGIN SELECT RAISE(ABORT,'anchor append-only'); END;
```

**`trading.db` 触发器（3 个）**

```sql
CREATE TRIGGER trg_dec_claim_ins
BEFORE INSERT ON ai_decisions
WHEN NEW.provenance='real_llm' AND NEW.record_origin<>'migrated'
 AND (NEW.evidence_id IS NULL OR length(NEW.evidence_id)=0)
BEGIN SELECT RAISE(ABORT,'real_llm claim requires evidence_id'); END;

CREATE TRIGGER trg_dec_claim_upd
BEFORE UPDATE ON ai_decisions
WHEN NEW.provenance='real_llm' AND NEW.record_origin<>'migrated'
 AND (NEW.evidence_id IS NULL OR length(NEW.evidence_id)=0)
BEGIN SELECT RAISE(ABORT,'real_llm claim requires evidence_id (update)'); END;

CREATE TRIGGER trg_dec_no_delete
BEFORE DELETE ON ai_decisions
BEGIN SELECT RAISE(ABORT,'decisions append-only'); END;
```

### 15.2 视图（**6 个**）

```sql
-- auth.db
CREATE VIEW v_evidence_facts AS
SELECT evidence_id, decision_id, seq, global_evidence_seq, session_id,
       attempt_verdict, key_id, signing_architecture,
       length(evidence_mac)=64 AS mac_present
  FROM ai_transport_evidence;

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

CREATE VIEW v_session_gaps AS            -- P2-3 修复
SELECT s.session_id, s.decision_id, s.seq, s.state, 'committed_without_evidence' AS defect
  FROM ai_sign_session s
 WHERE s.state='committed' AND s.evidence_id IS NULL
UNION ALL
SELECT s.session_id, s.decision_id, s.seq, s.state, 'evidence_not_committed'
  FROM ai_sign_session s
  JOIN ai_transport_evidence e ON e.evidence_id=s.evidence_id
 WHERE s.state<>'committed'
UNION ALL
SELECT s.session_id, s.decision_id, s.seq, s.state, 'decision_mismatch'
  FROM ai_sign_session s
  JOIN ai_transport_evidence e ON e.evidence_id=s.evidence_id
 WHERE e.decision_id<>s.decision_id;

CREATE VIEW v_anchor_tail AS
SELECT anchor_id,start_seq,end_seq,leaf_count,anchor_root,prev_anchor_root,
       created_at,anchor_key_id,external_ref
  FROM ai_anchor ORDER BY anchor_id DESC LIMIT 20;

CREATE VIEW v_auth_gaps AS
SELECT 'missing_seal' AS gap, COUNT(*) AS n FROM v_evidence_without_seal
 WHERE defect='missing_seal'
UNION ALL SELECT 'committed_without_evidence', COUNT(*) FROM v_session_gaps
 WHERE defect='committed_without_evidence'
UNION ALL SELECT 'evidence_not_committed', COUNT(*) FROM v_session_gaps
 WHERE defect='evidence_not_committed';

-- trading.db
CREATE VIEW v_decision_struct AS
SELECT decision_id, symbol, record_origin, provenance AS claimed_verdict,
       evidence_id, voided_at,
       CASE
         WHEN voided_at IS NOT NULL THEN 'voided'
         WHEN record_origin='migrated' THEN 'legacy_unverified'
         WHEN provenance<>'real_llm' THEN provenance
         WHEN evidence_id IS NULL THEN 'integrity_violation'
         ELSE 'structurally_valid_unverified'
       END AS db_verdict
  FROM ai_decisions;
```

---

## 16. 事务与崩溃恢复

```
应用：BEGIN (trading) → 准备 claim
应用 → IPC(call) → 服务：
   BEGIN IMMEDIATE (auth)
     ① 建会话 created（绑定 decision_id/seq/nonce）
     ② 解析+校验 allowlist → 序列化请求 → 计算 request_digest
     ③ created→request_bound→sent
     ④ 收响应 → 计算 response_digest → response_received
     ⑤ 计算 MAC → signed
     ⑥ UPDATE seq_counter；SELECT gseq
     ⑦ INSERT evidence（触发器校验 session committed？—— 否，见下）
     ⑧ UPDATE session → committed（写 evidence_id）
     ⑨ 事务内复核：struct_ok / 链 / seal / MAC / seq 匹配
   COMMIT (auth)
服务 → IPC 返回 verdict
应用：写 decisions（claim + evidence_id）→ COMMIT (trading)
```

**顺序澄清（R-21）**：`committed` 要求 `evidence_id` 非空，而证据插入又需要在会话之后 —— 若触发器要求 `committed` 会形成循环。
V3.7 的取舍：`trg_ev_session_ins` 接受 `state IN ('signed','committed')`；证据落库后由同一事务把会话置 `committed`。
**DB 无法强制"插入之后必须置 committed"** —— 这一条只能由服务代码保证，列入 R-21。这是明确的**安全降级**，不是闭环。

| 失败点 | 处理 |
|---|---|
| ②–⑤ 失败 | 会话 → failed/abandoned，ROLLBACK |
| ⑦ 约束失败 | ROLLBACK，会话随事务回滚（等于未发生） |
| COMMIT 失败 | 会话不存在（回滚）；应用未收到 verdict |
| 应用侧 trading 提交失败 | auth 已有证据，形成"有证据无决策"的孤儿 → `v_decision_struct` 查不到，审计可见 |

---

## 17. 历史迁移

| 步 | 动作 |
|---|---|
| 0 | 预检两库：`integrity_check`、行数 |
| 1 | 备份两库 + SHA256 |
| 2 | 事务外 `PRAGMA foreign_keys=OFF`（auth 库） |
| 3 | `BEGIN IMMEDIATE`（auth） |
| 4 | 建新表 / 迁移旧 evidence（V3.6 的 auth 数据若有） |
| 5 | 历史证据：`schema_version='v1'`、`evidence_mac=''`、`session_id=''`（**但 V3.7 的 evidence 表 `session_id NOT NULL`**）→ 需为历史行放宽：`CHECK (schema_version='v2' OR session_id='')` |
| 6 | 全局序号：按 `(decision_id,seq)` 排序赋 1..N；计数器置 N+1 |
| 7 | 重建 seal |
| 8 | 校验行数/内容哈希/FK/完整性 |
| 9 | COMMIT；finally 恢复 FK |

**历史行与 V3.7 约束的冲突（必须显式处理）**：V3.7 的 evidence 表对多数列设了 `NOT NULL`（如 `signer_identity`、`tls_sni`）。历史行没有这些值。
→ 迁移时必须**把历史证据留在 `trading.db` 的 `ai_legacy_evidence` 表**（宽表、宽松约束），而不是硬塞进 `auth.db` 的 v2 表。
→ `auth.db` 只装 v2 证据。这是 V3.7 对 P3-1 的答案：**历史与在线物理分表**，序号只在在线段连续。

---

## 18. 威胁模型 T1–T9

| 威胁 | 读密钥 | 调签名 | 改元数据 | 改 allowlist | 写 auth.db | 伪造 committed 会话 | 签任意内容 | 造 upstream_session_authenticated | 改历史 | 可发现 | 最高声明 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| T1 公开 API | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | — | 应用记录 |
| T2 任意 DML | ❌ | ❌ | ❌ | ❌ | ❌（ACL） | ❌ | ❌ | ❌ | 受限 | 可 | 未认证数据 |
| T3 改 schema | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ | MAC+锚定 | 同上 |
| T4 改文件 | ❌ | ❌ | ❌ | ❌ | ✅（文件级） | ✅ | ❌ | ⚠️ 需同时改密钥/锚定 | ✅ | 锚定 | 同上 |
| T5 应用被控 | ✅(app) | ✅ | ❌ | ❌ | ❌ | ❌ | ✅(A) | **❌** | 局部 | A 不可检测 | **仅 app_record** |
| T5′ 可调 IPC | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ | ❌（只能请求真实调用） | ✅ 但仅真实会话 | ❌ | 服务审计 | upstream_session（真实会话） |
| T6 控制 svc_fosun | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | **✅** | ✅ | 仅外部锚定 | 不可声明 |
| T7 锚定失效 | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | 可掩盖 | 锚点链 | 不可声明"已锚定" |
| **T8 替换服务配置/二进制** | ❌（需写目录） | ❌ | ❌ | ❌（需写目录） | ❌ | ❌ | ❌ | ❌ | ❌ | ACL 审计 | 同上 |
| **T9 管理员/安装者** | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 仅外部锚定 | 不可声明 |

**结论**：B 的可信上限 = "T4 以下 + T6 未发生"。T6/T9 一律不可声明。

---

## 19. 反例（**85 条**，全部设计预期）

| # | 威胁 | 操作 | 预期 | 拦截层 | readiness |
|---|---|---|---|---|---|
| CE-01 | T1 | claim=real_llm 无 evidence_id | 拒 | trading CHECK | — |
| CE-02 | T1 | 重复 `(decision_id,seq)` 会话 | 拒 | session UNIQUE | — |
| CE-03 | T1 | 已存在 endpoint_id 的 allowlist 插入 | 拒 | trg_allowlist_guard_ins | — |
| CE-04 | T1 | model 不在 allowed_models | 拒 | 服务校验 | — |
| CE-05 | T1 | 非 https scheme | 拒 | CHECK | — |
| CE-06 | T1 | host 含非法字符 | 拒 | CHECK GLOB | — |
| CE-07 | T1 | 请求体超 256 KiB | 拒 | 服务限额 | — |
| CE-08 | T1 | 响应体超 1 MiB | 拒 | 服务限额 | — |
| CE-09 | T1 | 超频率 | 拒 | 限流 | — |
| CE-10 | T1 | 会话 TTL 过期 | 拒 | 状态 expired | — |
| CE-11 | T2 | 应用直接 `INSERT auth.db.evidence` | 拒 | **OS ACL** | — |
| CE-12 | T2 | 应用 `UPDATE auth.db.evidence` | 拒 | ACL + trg_ev_no_update | — |
| CE-13 | T2 | 应用 `DELETE auth.db.evidence` | 拒 | ACL + trg_ev_no_delete | — |
| CE-14 | T2 | 应用 `INSERT auth.db.session(state='committed')` | 拒 | ACL | — |
| CE-15 | T2 | 应用 `UPDATE allowlist` | 拒 | ACL + trg_allowlist_no_update | — |
| CE-16 | T2 | 应用 `DELETE allowlist` | 拒 | ACL + trg_allowlist_no_delete | — |
| CE-17 | T2 | 应用 `UPDATE seq_counter` | 拒 | ACL | — |
| CE-18 | T2 | 应用 `DELETE anchor` | 拒 | ACL + trg_anchor_no_delete | — |
| CE-19 | T2 | 应用改 `trading.db.provenance='real_llm'`（无 evidence） | 拒 | trading CHECK | — |
| CE-20 | T2 | 应用改 `evidence_id` 指向伪造串 | 允许（软引用） | — | 查 auth 库不存在 → integrity_violation |
| CE-21 | T2 | `INSERT OR REPLACE` 证据（若误授写权） | 拒 | seal guard | — |
| CE-22 | T2 | UPSERT 证据 | 拒 | BEFORE INSERT guard | — |
| CE-23 | T2 | 同一响应跨会话复用 | 拒 | `UNIQUE(response_digest)` | — |
| CE-24 | T2 | 跨 decision 复用会话 | 拒 | `UNIQUE(decision_id,seq)` + trg_ev_seq_match | — |
| CE-25 | T2 | 证据 seq 与会话 seq 不等 | 拒 | trg_ev_seq_match_ins | — |
| CE-26 | T2 | 全局序号跳号 | 拒 | trg_ev_global_seq_ins | — |
| CE-27 | T2 | 覆盖 `evidence_mac` | 拒 | ACL | — |
| CE-28 | T2 | 空 reasons + unverified | 拒 | CHECK | — |
| CE-29 | T3 | DROP 触发器后改证据（若已获写权） | 成功改库 | — | MAC 不符 → integrity_violation |
| CE-30 | T3 | 重建 auth.db 表结构 | 成功（需 ACL 突破） | — | MAC 无法伪造 |
| CE-31 | T3 | 伪造 `v_decision_struct` 输出 | 视图可改 | — | 应用终判走服务 IPC |
| CE-32 | T3 | 把 auth.db 指向伪库（改连接串） | 可配置 | — | 服务端应固化路径（未强制，R-22） |
| CE-33 | T4 | 整库替换 auth.db | 成功（需文件写权） | — | 锚定 mismatch |
| CE-34 | T4 | 回滚 auth.db 到旧备份 | 成功 | — | 锚点链断裂 |
| CE-35 | T4 | 删除锚点日志 | 成功 | — | anchor missing → unverified |
| CE-36 | T4 | 改锚点日志 | 取决于 ACL | — | 锚点签名失败 |
| CE-37 | T4 | 替换 signer 密钥文件 | 需服务账户 | DPAPI | 解密失败 → fail closed |
| CE-38 | T4 | 拷他人机器密钥 | 成功 | DPAPI 绑定 | 解密失败 |
| CE-39 | T4 | 时间回拨 | 成功 | — | 锚点时间倒退 |
| CE-40 | T5 | A：用 app 密钥签伪造内容 | 成功 | — | 仅 app_record_authenticated |
| CE-41 | T5 | A：往 trading.db 写 `signing_architecture='signer_service'` | 允许 | — | 服务不承认（库不同） |
| CE-42 | T5 | A：复制 B 的 key_id | 允许 | — | MAC 不符 |
| CE-43 | T5 | A：重放 B 的 MAC | 允许 | — | 字段不同 → 不符 |
| CE-44 | T5 | A：写 auth.db 造 B 证据 | 拒 | ACL | — |
| CE-45 | T5 | A：读 keyring.json | 拒 | 目录 ACL | — |
| CE-46 | T5 | A：读 signer DPAPI blob | 拒 | 账户隔离 | — |
| CE-47 | T5 | A：改 keys.json slot | 拒（文件不可写） | 目录 ACL | — |
| CE-48 | T5′ | 请求服务签任意 blob | 拒 | 只接受参数 | — |
| CE-49 | T5′ | 请求非 allowlist 端点 | 拒 | allowlist | — |
| CE-50 | T5′ | 重放 client_nonce | 拒 | LRU 去重 | — |
| CE-51 | T5′ | 用终态会话再签 | 拒 | 无出边 | — |
| CE-52 | T5′ | 服务崩溃后恢复旧会话 | 拒 | 启动置 abandoned | — |
| CE-53 | T5′ | DB 提交失败后重签 | 拒 | persistence_failed | — |
| CE-54 | T5′ | 伪造调用者 SID | 拒 | 管道 ACL + token | — |
| CE-55 | T5′ | 同名管道抢占 | 拒 | FIRST_PIPE_INSTANCE（**需实测**） | — |
| CE-56 | T5′ | 高频刷成本 | 限流 | 速率限制 | — |
| CE-57 | SSRF | endpoint 解析到 127.0.0.1 | 拒 | IP 全量校验 | — |
| CE-58 | SSRF | 解析到 10/8 | 拒 | 同上 | — |
| CE-59 | SSRF | 解析到 169.254.169.254 | 拒 | 同上 | — |
| CE-60 | SSRF | 解析到 ::1 / fc00::/7 | 拒 | 同上 | — |
| CE-61 | SSRF | DNS rebinding | 拒 | 连接后 peer 复核 | — |
| CE-62 | SSRF | 302 跳私网 | 拒 | 禁止自动重定向 | — |
| CE-63 | SSRF | 继承 `HTTPS_PROXY` | 拒/降级 | 禁止继承 env | — |
| CE-64 | SSRF | 代理为本机回环 | 降级 | proxy_mode=explicit → proxy_unverified | — |
| CE-65 | T8 | 普通用户改 `%PROGRAMDATA%\FosunAI\signer` | 拒 | NTFS ACL | — |
| CE-66 | T8 | 普通用户替换允许目录下的 signer.exe | 拒 | 目录 ACL | — |
| CE-67 | T8 | 普通用户 `sc config` 改 ImagePath | 拒 | 需管理员 | — |
| CE-68 | T8 | 普通用户创建同名服务 | 拒 | 需管理员 | — |
| CE-69 | T8 | 普通用户改 allowlist.json | 拒 | 目录 ACL | — |
| CE-70 | T9 | 管理员改 ACL | 成功 | — | 仅锚定可发现 |
| CE-71 | T9 | 管理员替换 signer 二进制 | 成功 | — | 需代码签名校验（未实现，R-23） |
| CE-72 | 链 | prev_hash 断链 | 拒 | trg_ev_chain_ins | — |
| CE-73 | 链 | 序号回滚后空洞 | 拒 | 序号触发器 | — |
| CE-74 | 锚定 | root 不符 | 拒 | 复算 | mismatch |
| CE-75 | 锚定 | 空区间 leaf_count=0 | 允许 | CHECK 新表达式 | — |
| CE-76 | 锚定 | 奇数叶 | 复算一致 | 规约 | — |
| CE-77 | 迁移 | 历史证据塞进 v2 表 | 拒 | NOT NULL 约束 | — |
| CE-78 | 迁移 | 历史行补签 | 拒 | 无写权限 + 无密钥 | — |
| CE-79 | 迁移 | 序号从 1 重新开始 | 拒 | 计数器 N+1 | — |
| CE-80 | 会话 | committed 无 evidence | 拒 | CHECK | — |
| CE-81 | 会话 | created 带 evidence_id | 拒 | CHECK | — |
| CE-82 | 会话 | 状态跳跃 | 拒 | trg_session_transition | — |
| CE-83 | 会话 | 多尝试 seq 冲突 | 拒 | UNIQUE(decision_id,seq) | — |
| CE-84 | 结构 | `unverified_reasons='[ ]'` + unverified | 拒 | CHECK | — |
| CE-85 | 结构 | 决策指向不存在 evidence | 允许 | 软引用 | integrity_violation |

---

## 20. 正向场景（**35 条**）

| # | 场景 | 期望 |
|---|---|---|
| PS-01 | B 全条件 + 锚定 current | `upstream_session_authenticated` |
| PS-02 | B 但锚定 stale | `upstream_session_unverified` |
| PS-03 | A 全条件 | `app_record_authenticated` |
| PS-04 | A 但 MAC 缺失 | `app_record_unverified` |
| PS-05 | A 但未知 key | `app_record_unverified` |
| PS-06 | 历史 v1 行（legacy 表） | `legacy_unverified` |
| PS-07 | 历史行尝试补签 | 拒 |
| PS-08 | 多尝试：1 rejected / 2 timeout / 3 成功 | 决策指向 3，B 等级 |
| PS-09 | 作废 | `voided` |
| PS-10 | 回环代理 | `upstream_session_unverified` |
| PS-11 | 本地模型 | `local_llm` |
| PS-12 | 私网模型 | `local_llm` |
| PS-13 | 测试桩 | `stub_transport` |
| PS-14 | 无凭据 | `unavailable` |
| PS-15 | DNS 失败 | `rejected` |
| PS-16 | HTTP 500 | `rejected` |
| PS-17 | 200 缺 response_id | `upstream_session_unverified` |
| PS-18 | pin mismatched | `rejected` |
| PS-19 | 会话正常走完 | committed |
| PS-20 | 服务重启 | 旧会话 abandoned |
| PS-21 | 密钥轮换后旧证据 | 旧 key 验证通过 |
| PS-22 | 新证据用新 key | 通过 |
| PS-23 | 锚定恢复 | 等级回升 |
| PS-24 | 并发两决策 | 序号连续 |
| PS-25 | 事务回滚 | 序号不前进 |
| PS-26 | 迁移中断 | 回滚 |
| PS-27 | 单叶区间 | 复算一致 |
| PS-28 | 三叶奇数 | 复算一致 |
| PS-29 | 空区间锚点 | leaf_count=0 通过 |
| PS-30 | `v_auth_gaps` 与迁移行数一致 | 一致 |
| PS-31 | 报表分列 A/B | 不合并 |
| PS-32 | unknown key 的 B 记录 | unverified |
| PS-33 | allowlist 镜像与文件一致 | 服务启动校验通过 |
| PS-34 | 应用只读打开 auth.db | 成功（DELETE journal） |
| PS-35 | 安装脚本 ACL 断言 | 无 Users 写权限项 |

---

## 21. 未验证事项（Windows + SQLite）

| # | 类别 | 待验证 |
|---|---|---|
| W-1…W-5 | Windows | §4.4 五项（ProgramData 继承、服务账户写权、服务 SID、只读 WAL、管道首实例） |
| W-6 | Windows | `icacls` 断言脚本在中文系统输出格式下是否可解析 |
| S-1 | SQLite | 同事务 read-your-writes |
| S-2 | SQLite | 触发器子查询能否读同事务已更新的计数器（**R-17**） |
| S-3 | SQLite | `BEGIN IMMEDIATE` 多连接串行化 |
| S-4 | SQLite | ROLLBACK 后计数器恢复 |
| S-5 | SQLite | `INSERT OR REPLACE` 是否触发 DELETE 触发器 |
| S-6 | SQLite | BEFORE INSERT `RAISE(ABORT)` 是否回滚 REPLACE 的删除 |
| S-7 | SQLite | 只读连接打开 DELETE-journal 库的行为 |
| S-8 | SQLite | 触发器递归设置 |
| S-9 | SQLite | WAL 并发（trading.db） |
| S-10 | SQLite | 崩溃恢复语义 |
| S-11 | SQLite | 跨库 ATTACH 的只读约束 |
| S-12 | SQLite | `UNIQUE(response_digest)` 对 NULL 的多行允许 |

---

## 22. 残余风险

| # | 风险 | 状态 |
|---|---|---|
| R-17 | 全局序号语义未验证 | OPEN |
| R-19 | Windows ACL 未实机验证 | OPEN |
| **R-21** | **会话最终态未被 DB 强制**（committed 与证据插入的循环依赖，靠事务顺序保证） | OPEN |
| **R-22** | auth.db 路径由应用配置决定（改配置可指向伪库） | OPEN（建议服务硬编码） |
| **R-23** | 未校验 signer 二进制签名（T8/T9 可替换） | OPEN |
| R-12 | A 架构应用被控可签任意内容 | 设计接受 |
| R-13 | 服务账户被控即失守 | 设计接受 |
| R-16 | T5′ 可反复请求真实调用 | 限流缓解 |
| R-1 | 上游 response_id 真实性 | 上游 |
| R-18 | 模型身份不可证 | 语义 |

---

## 23. 实施前置条件

| # | 条件 |
|---|---|
| 1 | 创建 `svc_fosun` 本地账户 + 授予 "Log on as a service" |
| 2 | 安装脚本写好 3 个目录的 ACL 并断言 |
| 3 | `key-init` 在服务账户下执行（DPAPI 用户 scope） |
| 4 | 生成 allowlist.json 并同步镜像 |
| 5 | §21 的 W-1…W-6、S-1…S-12 全部实机/探针验证 |
| 6 | 迁移脚本在副本演练 |

---

## 24. 与 V3.6 的差异

| 项 | V3.6 | V3.7 |
|---|---|---|
| 认证数据位置 | 同一库 | **独立 auth.db，应用只读** |
| 等级依据 | keyring slot 字段 | **库归属（物理）** |
| 密钥元数据 | `keys.json` 位置未定 | `%PROGRAMDATA%\...\signer\`，ACL 仅服务账户 |
| 会话 INSERT | 无守卫 | `trg_session_born_created` |
| 会话唯一键 | `UNIQUE(decision_id)` | `UNIQUE(decision_id, seq)` |
| E 族 | 单一，A 不可达 | **E-signer 独立，A 走 trading.db** |
| allowlist | 无触发器 | 3 个触发器 + 文件权威源 |
| 空锚点 | DDL 不可表达 | CHECK 支持 |
| 词表 | `unverified_by_db` 未登记 | `structurally_valid_unverified` |
| 触发器/表/视图 | 19 / 8 / 6 | **23（auth 20 + trading 3）/ 10（auth 7 + trading 3）/ 6** |
| CE / PS | 78 / 32 | **85 / 35** |
| 威胁模型 | T1–T9 | T1–T9（补 T8 细节） |

---

## 25. 自检表（不构成审计）

| # | 检查 | 结果 |
|---|---|---|
| C-01 | 是否把"触发器"当安全边界 | ✅ 明确"LSASS 不在触发器里"，ACL 才是 |
| C-02 | 安装者与应用同 SID 是否处理 | ✅ §4.2 第 2 问 + ACL 只给服务 SID |
| C-03 | keyring 是否由应用可写 | ✅ 否 |
| C-04 | 会话能否直达 committed | ✅ 不能（INSERT 守卫） |
| C-05 | A 分支是否可达 | ✅ 可达（trading.db） |
| C-06 | allowlist 是否有 3 个触发器 | ✅ |
| C-07 | 多尝试是否支持 | ✅ `UNIQUE(decision_id,seq)` |
| C-08 | 是否有"引用不重写" | ✅ 无；23 个触发器全部给出完整 SQL |
| C-09 | 是否宣称实测 | ✅ 全文标注设计预期 |
| C-10 | 反例/正例 | ✅ 85 / 35 |

---

## 26. 交付状态

```
V3.7 设计文件已生成，待第八轮独立审计；
方案未获批；未获实施授权；
未执行 SQL；未创建数据库；未运行探针；未跑迁移；
S0 未启动。
```

---

> 本文件 SHA256（本行之前全部字节，可用 `head -n -1 <本文件> | sha256sum` 复算）：`d4063095099016051dd982c22bd7c2e1a7db852bac6eabf4863fd62200065711`
