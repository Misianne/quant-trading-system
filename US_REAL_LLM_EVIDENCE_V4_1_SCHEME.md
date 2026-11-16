# V4.1 方案（设计稿 · 不实施）

| 项 | 值 |
|---|---|
| 版本 | **v4.1**（2026-11-11 18:05） |
| 取代 | `US_REAL_LLM_EVIDENCE_V4_0_SCHEME.md`（`adba0a05…`，**只读基线，不覆盖、不修改**） |
| 依据 | `US_REAL_LLM_EVIDENCE_V4_0_INDEPENDENT_AUDIT.md`（`48cf5d84…`，结论 **FAIL**：P1=3 / P2=6 / P3=7） |
| 性质 | **文档设计**。仅允许读 Markdown 与写本文件；未执行代码 / SQL / 数据库 / ACL / 服务 / 密钥操作 |
| 范围 | 只修 3 条 P1 + 6 条 P2 + 7 条 P3；**不重构**已通过的路径权威、组件比较、双库隔离、V-1…V-7 |
| 状态 | V4.1 待第十二轮独立审计；未获批；未获实施授权；S0 未启动 |

**状态标签（全文只用这 4 种）**

| 标签 | 含义 |
|---|---|
| `[DESIGN]` | 设计描述完成，逻辑自洽 |
| `[STATIC_REVIEW]` | 尚待独立静态审查 |
| `[WINDOWS_TEST]` | 尚待 Windows 实机验证 |
| `[SQLITE_PROBE]` | 尚待未来隔离探针验证 |

> 标签只用于**修订项**；风险另用 `OPEN / MITIGATED / TEST-PENDING / DESIGN-CLOSED`（§10），两者不混用。

---

## 1. 版本与基线

| 文件 | 字节 | 行 | SHA256 | 自指纹 | 判定 |
|---|---|---|---|---|---|
| V4.0 | 67,510 | 1,049 | `adba0a05…c3066` | `4c50d0da…853c9` | ✅ 一致 |
| V4.0 独立审计 | 40,216 | 543 | `48cf5d84…74c44` | `3df23d1f…73696` | ✅ 一致 |
| V3.9 | — | — | `1a72a1f5…59f66` | — | ✅ 未变 |
| V3.9 审计 | — | — | `a096f806…341418` | — | ✅ 未变 |
| V3.8 | — | — | `1c8ebce3…3b14f` | — | ✅ 未变 |
| V3.7 / V3.6 / V3.5 / 原计划 | — | — | 与 V4.0 §1.1 同 | — | ✅ 未变 |

保护目录：`src/`=51、`config/`=2、`tests/`=37、`data/`=6、`logs/`=68；18:00 后改动 **0**。

---

## 2. 第十一轮问题 → V4.1 处置总览

| 审计 ID | 级别 | 问题一句话 | 根因 | 处置章 | 处置性质 |
|---|---|---|---|---|---|
| A11-P1-01 | P1 | `ai_backup_seal` 放在 SVC 可写的 `auth.db`，"可信来源"可被 SVC 伪造 | 换载体没换信任根 | §3 | **挪走载体**（移出可写库） |
| A11-P1-02 | P1 | `policy-<ver>.json` 无法自证 Authenticode，而它正是结构校验的期望集来源 | 同上 | §4 | **取消"从文件读 policy"这条路径** |
| A11-P1-03 | P1 | §3.4 `SVC:R` 与 §16.3 `SVC:0x1301BF` 矛盾 | 掩码未单点定义 | §5 | **定死单值 + 轮换不覆盖** |
| A11-P2-01 | P2 | 自检失败自动降级 `acl_only` | fail-soft 与 fail-closed 混用 | §6 | 改显式审批 |
| A11-P2-02 | P2 | B4 先算哈希后收紧 DACL（TOCTOU） | 步骤顺序 | §3.3 | 重排顺序 |
| A11-P2-03 | P2 | S12a 失败只靠 exit 81，SCM 重启抹掉检测 | 检测结果不持久 | §7 | 持久化 `drift.flag` |
| A11-P2-04 | P2 | policy 无版本下限，可合法降级 ACL | 无单调性 | §4.3 | 复用 `MinBuild` |
| A11-P2-05 | P2 | `key-purge` 的"无引用"未定义检查法 | 定义缺失 | §8.1 | 补算法与失败行为 |
| A11-P2-06 | P2 | B4 未要求停服，`auth.db` 双写者 | 备份路径误触认证库 | §3 | **B4 不再写 `auth.db`** |
| A11-P3-01…07 | P3 | 风险表双值/过期标记/构建裁剪/IPC 未重述/key_id 随机性/注释语义/readiness 暴露 | 一致性 | §8 / §10 | 逐条修 |

### 2.1 一句话总结本轮思路

> 三条 P1 的共性是"**换了载体，但信任根没换**"。V4.1 的做法不是再换一次载体，而是**把信任根从"文件"移回"已签名的二进制"**：
> - 备份封存 → **SYSTEM 独占目录**的文件存储（SVC 连读都不行）；
> - policy → **编译期常量**（随已签名二进制），不存在"读文件"的入口；
> - 密钥文件 ACL → **单点定义**，且轮换**永不覆盖**旧文件。

---

## 3. 备份封存重构（A11-P1-01 / P2-02 / P2-06）

### 3.1 结构性变更：`ai_backup_seal` 表被**删除**

| 项 | V4.0 | V4.1 | 理由 |
|---|---|---|---|
| 封存记录载体 | `auth.db` 的 `ai_backup_seal` 表 | **`%PROGRAMDATA%\FosunAI\seals\backup-seals.jsonl`（L7，`SY:FA` 独占）** | `auth.db` 给 SVC `Modify`，SQLite 无法区分"谁在写"，触发器只能拦语句形状、拦不住 `INSERT`；`sealer='SYSTEM'` 只是自填字符串 |
| SVC 能否读 | 能（`ai_transport_evidence` 同级） | **不能**（L7 无 SVC ACE） | 减少暴露面 |
| SVC 能否写 | **能**（致命） | **不能** | 这是 P1-01 的根因 |
| `auth.db` 表数 | 8 | **7**（回到 V3.9 数量） | 删除该表 |
| 触发器数 | 31 | **29** | 删除 2 个 seal 触发器 |

> **明确**：V4.1 不再声称"`auth.db` 里的记录是可信来源"。可信来源改为**SVC 无法访问的目录**里的哈希链文件。这是一个**位置变化**，不是话术变化。

### 3.2 L7 封存存储的格式与完整性

```
%PROGRAMDATA%\FosunAI\seals\backup-seals.jsonl
{ "seq":1, "name":"auth-20261111-120000.db", "sha256":"<64hex>",
  "bytes":12345678, "sealed_at":"2026-11-11T12:00:03Z",
  "sealer":"SYSTEM", "prev_hash":"<64hex 或 GENESIS>" }
```

| 规则 | 内容 |
|---|---|
| 写入者 | **仅 SYSTEM**（安装器 `--seal-backups` 或 SYSTEM 计划任务）；SVC 无任何权限 |
| ACL | L7 目录 `SY:FA`；文件 `SY:FA`。**不继承**（`D:PAI`） |
| 完整性 | 逐行哈希链：`prev_hash = sha256(上一行原始字节)`；首行 `prev_hash="GENESIS"` |
| 追加方式 | `FILE_APPEND_DATA` 单写者；写完 `FlushFileBuffers` |
| 读取者 | 恢复流程（SYSTEM）；服务**不需要**读 |
| 局限（必须披露） | SYSTEM 可整体重写整条链。本机制只防**非 SYSTEM**主体伪造；对 SYSTEM 级攻击者无效（R-43） |

### 3.3 B4 步骤重排（修 A11-P2-02）

**V4.0 的错误顺序**：① 算哈希 → ② 收紧 DACL → ③ 写记录 → ④ 改名。
问题：①→② 之间文件仍可写，SVC 可改写内容，导致"记录哈希 ≠ 最终文件"。

**V4.1 的正确顺序**：

| 步 | 动作 | 此时文件状态 | 为什么放这里 |
|---|---|---|---|
| B1 | SVC 在 `backup\` 写 `auth-<ts>.db.part` | SVC 可写（L5 `0x1201BF`） | 写入阶段 |
| B2 | `FlushFileBuffers` | 同上 | 落盘 |
| B3 | SVC 自检 `integrity_check` + 逐表 digest | 同上 | **仅供参考**，不作为可信依据 |
| **B4-1** | SYSTEM `MoveFileEx` 改名为 `auth-<ts>.db` | 仍可写 | 先定名，后续哈希针对最终路径 |
| **B4-2** | SYSTEM `SetNamedSecurityInfoW` 把该文件 DACL 改为 `SY:FA + SVC:0x1200A9 + BA:0x1200A9` | **SVC 转为只读** | **先收紧** |
| **B4-3** | SYSTEM 重新打开该文件（只读）→ 计算 `sha256` + `bytes` | 只读，无法被 SVC 改 | 收紧后计算，消除 TOCTOU |
| **B4-4** | SYSTEM 追加一行到 L7 `backup-seals.jsonl`（含 `prev_hash`） | — | 记录与字节一致 |
| **B4-5** | SYSTEM 回读刚写入的行，校验 `prev_hash` 链与 `sha256` | — | 自检 |

| 断言 | 说明 |
|---|---|
| B4-2 之后 SVC 不能再改 | 掩码不含 `WRITE_DATA`（`0x1200A9` 只有读+遍历） |
| B4-3 之后 SYSTEM 也不再改 | 单写者、写后即记 |
| 若 B4-4 失败 | 文件已封存但无记录 → 该备份**不可用于恢复**（`readiness` 标 `backup_unsealed`，不阻断服务） |
| 幂等 | 重跑时若 `name` 已存在且 `sha256` 相同 → no-op；`sha256` 不同 → **拒绝**并告警（不覆盖历史行） |

### 3.4 B4 不再写 `auth.db`（修 A11-P2-06）

| 项 | V4.0 | V4.1 |
|---|---|---|
| B4 是否打开 `auth.db` | 是（写 seal 表） | **否** |
| 是否与运行中的服务构成双写者 | 是 | **否**（只写 L5 文件 + L7 追加） |
| 是否仍需停服 | **否**（可在线封存） | — |

> 这是"删除表"带来的**附带收益**：备份路径彻底脱离认证库，A11-P2-06 自然消失。**B1–B3 仍在服务运行时执行**，此时 SVC 与 SYSTEM 不共享任何文件。

### 3.5 恢复流程（B5）与"SYSTEM 重算 ≠ 内容认证"（修信任边界 2）

| 步 | 动作 |
|---|---|
| R-1 | 停服 |
| R-2 | SYSTEM 校验 L7 整条哈希链（`prev_hash` 连续、末行完整） |
| R-3 | 取目标 `name` 的记录：`sha256`/`bytes` |
| R-4 | 断言目标文件当前 DACL 为 `SY:FA + SVC:R + BA:R`（防"封存后被放宽再改"） |
| R-5 | 重算 `sha256` 与 `bytes`，与 R-3 比对 |
| R-6 | 一致 → 替换 `auth.db` → 重跑 S0–S12；不一致 → **拒绝恢复**并告警 |

**必须写清的边界（V4.0 未写）**：

> SYSTEM 重算 `sha256` 只证明"**记录的哈希与文件字节一致**"（防事后替换），**不证明**文件内容在语义上是正确、未被污染的数据库。
> 内容的语义可信只能来自：① 备份当时 B3 的 `integrity_check`；② 与源库的页级比对；③ 而这两者的输入都在 SVC 可控范围内。
> 因此：**备份是"可检测被替换"的，不是"内容被证明正确"的。** 任何"恢复后即等同原始历史"的说法都是错的。

### 3.6 失败与降级

| 情形 | 行为 |
|---|---|
| SYSTEM 封存组件不可用 | 备份停在 `.part` 或"已改名无记录"；`readiness=backup_unsealed`；**不影响** B 等级判定（备份不在认证路径） |
| L7 链校验失败 | 拒绝一切恢复；告警；人工介入 |
| `sha256` 不匹配 | 拒绝恢复该备份，其余备份不受影响 |

---

## 4. policy 信任根重构（A11-P1-02 / P2-04）

### 4.1 取消"从文件读 policy"这条路径

| 项 | V4.0 | V4.1 |
|---|---|---|
| policy 来源 | `policy-<version>.json` 文件（号称 SHA256 + Authenticode） | **编译期常量，编入已签名的安装器与服务二进制** |
| 是否存在"读 policy 文件"的代码路径 | 是 | **否**（不存在该入口，因此不存在"被替换"的攻击面） |
| 人可读副本 | policy 文件 | `US_REAL_LLM_EVIDENCE_V4_1_SCHEME.md` §9 的权限矩阵（**文档**，不参与运行） |
| 期望集 E 的来源 | policy 文件 | **二进制常量 `kExpectedAcl[]`** |

> 为什么这是唯一干净的解：Authenticode 签的是 PE/COFF（或 catalog），**独立 JSON 无法自证签名**。只要期望集来自"运行时可被替换的文件"，§8 的结构校验就可能为攻击者的 ACL 背书。把期望集放进**已签名二进制**，信任根与执行体合一。

### 4.2 二进制的验证链（复用 V3.9 §15，不新增机制）

| 层 | 机制 |
|---|---|
| 签名 | 安装器与 signer 均 Authenticode + SHA-256 |
| 启动校验 | `WinVerifyTrust` + 编译期写入的**证书公钥 SHA-256 pin** |
| DLL | `SetDefaultDllDirectories(SYSTEM32|APPLICATION_DIR)`；目录内禁止未签名 DLL |

| 断言 | 说明 |
|---|---|
| 改 policy 必须改二进制 | 改二进制会破坏签名 → 启动/安装被拒 |
| 因此"替换 policy 文件"攻击**不存在** | 无该入口 |

### 4.3 防回滚（修 A11-P2-04）

| 机制 | 内容 |
|---|---|
| 单调下限 | 复用 V3.9 §15 的 `HKLM\SOFTWARE\FosunAI\MinBuild`（管理员写），**不新增** `MinPolicyEpoch` |
| 为什么可复用 | policy 已是二进制常量 → "policy 版本" ≡ "构建号" |
| 安装器行为 | 拒绝安装 `build < MinBuild` 的包（含"用旧包 `--repair`"） |
| 服务行为 | 启动校验自身 `build >= MinBuild` |
| 残余 | 管理员可改 `MinBuild` 以允许降级 → T9（R-28 同域），不声称解决 |

### 4.4 安装中断与隔离（继承 V4.0 §13，仅改来源）

| 项 | V4.1 |
|---|---|
| 回滚源 | **二进制常量**（即 §4.1 的 `kExpectedAcl[]`） |
| 现场 SDDL | 仅作证据写 `install-log`，**永不作为回滚目标**（V4.0 §13.1 保留） |
| 事务日志 | `install-log\install-<ts>.jsonl`，`SY:FA`，哈希链（V4.0 §13.2 保留） |
| 隔离态 | deny-all（`SY:FA`），服务 exit 83（V4.0 §13.5 保留） |

---

## 5. 密钥文件 ACL 与轮换（A11-P1-03）

### 5.1 单点定义（消除 §3.4 与 §16.3 的矛盾）

| 项 | 值 | 说明 |
|---|---|---|
| F3 掩码 | **`0x12019F`** | `READ_DATA|WRITE_DATA|APPEND_DATA|READ_EA|WRITE_EA|READ_ATTRIBUTES|WRITE_ATTRIBUTES|READ_CONTROL|SYNCHRONIZE` |
| 为什么不是 `SVC:R`（V4.0 §3.4） | 服务必须能写新 blob 与 keyring | 只读则轮换不可实现 |
| 为什么不是 `0x1301BF`（V4.0 §16.3） | 含 `DELETE` → SVC 可删 `key-<id>.dpapi` 与 `keyring`，**`key_id` 元数据丢失即历史不可验** | 不可逆 |
| 逐位核对 | `0x12019F = 0x100000(S) + 0x20000(RC) + 0x19F`；`0x19F = 0x100+0x80+0x10+0x08+0x04+0x02+0x01` | 无 `0x10000(DELETE)`、无 `0x40(DC)`、无 `0x40000/0x80000`、无 `0x20(X)` |

> **F3 在 §5.1 与 §9.2 中必须出现完全相同的字符串**；V4.1 加一条自检："文档内 `0x12019F` 只以本值描述 F3，不得出现 `SVC:R` 或 `SVC:0x1301BF` 描述 F3"。

### 5.2 轮换**永不覆盖**旧 blob

| 规则 | 内容 |
|---|---|
| 新 key | 新 `key_id` → **新文件名** `key-<new>.dpapi`；**不使用** `MoveFileEx(REPLACE_EXISTING)` |
| 为什么 | 覆盖需要目标文件的 `DELETE`；F3 无 `DELETE`，因此"覆盖"在权限上就不可行——这正好与"不覆盖"的设计一致 |
| 旧 key 删除 | 仅 SYSTEM（`key-purge` 以 SYSTEM 身份运行）；SVC 无 `DELETE` |
| 孤儿 blob | 崩溃产生的未登记 blob → 启动时列出并写 `logs` 告警；**不自动删除**（删除需 SYSTEM） |

### 5.3 keyring 的原子写（不使用 `REPLACE_EXISTING`）

| 项 | 设计 |
|---|---|
| 形式 | **两份轮替**：`keyring-a.json` / `keyring-b.json`，各含 `{epoch, keys[]}` |
| 写入 | 总是写**较旧**的那份：`OPEN_EXISTING` + `SetEndOfFile` + 写入 + `FlushFileBuffers` |
| 读取 | 取 `epoch` 较大且 JSON 合法者；两份都非法 → **拒绝启动**（退出码 76，密钥不可用） |
| 为什么不用 rename | rename 覆盖需 `DELETE`，而 F3 无 `DELETE` |
| 崩溃 | 最多丢失"最新一次写"，旧份仍可读 → 不会出现"两份都坏" |
| 单调性 | `epoch` 只增不减；读取时若发现回退 → 拒绝启动 |

### 5.4 `key_id` 格式（修 A11-P3-05）

| 规则 | 内容 |
|---|---|
| 形式 | 16 位小写 hex（64 bit） |
| 来源 | **CSPRNG**（`BCryptGenRandom`），**禁止**顺序编号或时间戳派生 |
| 唯一性 | 生成后必须与 keyring 内既有 `key_id` 比较；冲突则重生成（上限 8 次，超限拒绝启动） |

---

## 6. 降级审批（A11-P2-01）

| 项 | V4.0 | V4.1 |
|---|---|---|
| 触发 | 安装期自检失败 → **自动**切 `acl_only` | 自检失败 → **中止安装**（exit 75） |
| 显式路径 | 无 | 仅当管理员传 `--allow-acl-only` 才降级 |
| 记录 | keyring `protection` | keyring `protection` + `install-log` 一条 `ACL_ONLY_APPROVED`（含操作者 SID 与时间） |
| 可见性 | `health.protection` | `health.protection` + `readiness=degraded_acl_only`（**只读展示，不阻断**服务） |
| 与 verdict 的关系 | 不参与 | 不参与（V4.0 §3.5 保留） |
| 原因 | — | 与 §11.2「`sqlite3_config` 失败即 exit 82、禁止回退」保持**同一种 fail-closed 哲学** |

> 明示：`acl_only` 下 blob 被拷出即等价于密钥泄露；该状态必须由人批准，不能由一次自检结果代替人决策。

---

## 7. 漂移持久化（A11-P2-03）

### 7.1 问题复述

V4.0 §9.2 让 S12a/S12b 失败时以 **exit 81** 退出。若 SCM 配置了失败重启：进程退出 → 立即拉起 → 新进程重跑 S0–S12，对**已被替换且 ACL 已伪造**的对象全部通过 → 服务正常运行在攻击者的库上，日志只留一次 81。

### 7.2 修订：持久化 `drift.flag` + 启动前置检查

| 项 | 内容 |
|---|---|
| 位置 | `signer\drift.flag`（L2，SVC 可写） |
| 内容 | `{ts, kind:"path_drift"|"dacl_drift", old_fileid, new_fileid, event_id}` |
| 写入时机 | S12a 或 S12b 失败，**在退出之前** |
| 启动第 0 步 | S0 之前检查该文件：存在 → **立即 exit 81**，不进入 S0 |
| 清除 | **仅管理员**：`fosun-signer.exe clear-drift --confirm`（写事件日志 + 新增一条 L4 记录） |
| 与 SCM 的关系 | 明确"重启不等于恢复" |
| 事件日志 | 用固定 EventID（例如 4101/4102），便于集中告警 |

### 7.3 诚实的局限（新增 R-42）

> `drift.flag` 位于 L2，SVC 有 `Modify` → **SVC 可以删除它**。
> 因此该标志的真实作用是"**防止 SCM 重启把检测结果静默抹掉**"，**不是**"抵抗 SVC 或 T9 掩盖"。
> 若需抵抗 SVC，必须把 flag 放到 SVC 不可写的目录（L7），但服务就写不了它 —— 这是能力与可信度的固有权衡。V4.1 **选择可用性**（服务能写），并**如实标记 R-42 OPEN**。

### 7.4 备选（不选，仅记录）

| 备选 | 为什么不用 |
|---|---|
| flag 写 L7（`SY:FA`） | 服务无写权 → 无法记录；除非新增一个 SYSTEM 常驻组件（扩大攻击面，R-39） |
| 只用事件日志、不用 flag | 事件日志可被清除/回滚，且 SCM 重启后不阻断 → 回到原问题 |

---

## 8. 其余 P2/P3 处置

### 8.1 `key-purge` 的引用检查（A11-P2-05）

```
purge(key_id):
  1. assert keyring[key_id].state == 'deletable'
  2. n ← SELECT COUNT(*) FROM ai_transport_evidence WHERE key_id = ?
  3. if query_failed:            return REFUSE("cannot verify references")
  4. if n > 0:                   return REFUSE("referenced by N evidence rows")
  5. append L7 行 {action:"purge", key_id, n=0, ts}      # SYSTEM 侧审计
  6. delete key-<id>.dpapi 与 escrow 副本                # SYSTEM 权限
  7. keyring 标记 state='purged'（保留条目，不删行）
```

| 规则 | 内容 |
|---|---|
| 默认 `--dry-run` | 必须显式 `--force` 才真删 |
| 查询失败 | **拒绝删除**（fail-closed） |
| 保留条目 | keyring 保留 `state='purged'` 而非删行 → 事后仍可解释 `mac_unknown_key` 的原因 |
| 谁执行 | SYSTEM（SVC 无 `DELETE`） |

### 8.2 SQLite 构建裁剪（A11-P3-03）

| 检查 | 动作 |
|---|---|
| `sqlite3_compileoption_used("SQLITE_OMIT_TEMPDB")` | 为真 → **中止安装**（明确不支持该构建） |
| `SQLITE_TEMP_STORE` 值 | `3`（强制内存）→ 记录 `temp=memory`，**跳过** `TEMP_DIR` 配置与 exit 82；`0/1/2` → 必须配置成功 |
| 版本与编译选项 | 启动时记录 `sqlite3_libversion()` + `compileoption_get` 摘要，与构建清单比对；不一致 → 告警（不阻断） |

> 这消除了"恒 exit 82 → 服务永不可用"与"全内存构建被误阻断"两种情形。

### 8.3 IPC 鉴权重述（A11-P3-04）

| 项 | 内容 |
|---|---|
| 客户端认证 | 服务端 `ImpersonateNamedPipeClient` → `GetTokenInformation(TokenUser)` → SID 必须在白名单（应用账户 SID 枚举，来自 L2 的 `clients.json`，`SVC:M`） |
| 请求绑定 | `decision_id` 必须与 `session`/`evidence` 一致（V-1…V-7 已有） |
| 防重放 | 每次请求 `nonce`，服务端保留最近 N=4096 已用 nonce（FIFO）；重复 → 拒 |
| 限流 | 每 SID 10s ≤ 20 次；并发 ≤ 4 |
| 拒绝原因 | 统一返回 `denied`，不区分"SID 不在白名单"与"nonce 重复"（防探测） |

### 8.4 其余

| ID | 处置 |
|---|---|
| A11-P3-01 | 风险表改为**单值状态**；R-25 → `TEST-PENDING`；R-36 → `DESIGN-CLOSED`（依据已换为二进制常量）；R-33 的"阻断实施=是"改为**否**（措辞已在 §9 改完）；R-29 关闭条件改为 **W-54/W-55** |
| A11-P3-02 | `Global\` 互斥体是否需 `SeCreateGlobalPrivilege` → W-56（保留），并在文档标注"若不可用则退化为 `Local\` + 服务名唯一性检查" |
| A11-P3-06 | 删除 V4.0 §19 末尾语义不明的注释句，改为明确的三条 DESIGN-CLOSED 依据 |
| A11-P3-07 | `readiness` 增 `backup_unsealed` / `backup_seal_chain_broken` 两个诊断项（只读展示，不阻断） |

---

## 9. 权限矩阵与计数

### 9.1 位表（供逐位核对）

| 位 | 值 | 文件 | 目录 |
|---|---|---|---|
| `0x1` | 1 | READ_DATA | LIST |
| `0x2` | 2 | WRITE_DATA | ADD_FILE |
| `0x4` | 4 | APPEND_DATA | ADD_SUBDIR |
| `0x8` | 8 | READ_EA | 同 |
| `0x10` | 16 | WRITE_EA | 同 |
| `0x20` | 32 | EXECUTE | TRAVERSE |
| `0x40` | 64 | — | **DELETE_CHILD** |
| `0x80` | 128 | READ_ATTR | 同 |
| `0x100` | 256 | WRITE_ATTR | 同 |
| `0x10000` | 65536 | **DELETE** | 同 |
| `0x20000` | 131072 | READ_CONTROL | 同 |
| `0x40000` / `0x80000` | — | WRITE_DAC / WRITE_OWNER | 同 |
| `0x100000` | 1048576 | SYNCHRONIZE | 同 |

### 9.2 目标 ACL（V4.1 全量）

| 对象 | 类型 | Owner | ACE（SID:mask，继承） |
|---|---|---|---|
| L1 `FosunAI\` | 目录 | `SY` | `SY:FA(OI,CI)` `BA:FA(OI,CI)` `SVC:0x1200A9(OI,CI)` |
| L2 `signer\` | 目录 | `SY` | `SY:FA(OI,CI)` `SVC:0x1301BF(OI,CI)` |
| L3 `authdb\` | 目录 | `SY` | `SY:FA(OI,CI)` `SVC:0x1301BF(OI,CI)` |
| L4 `logs\` | 目录 | `SY` | `SY:FA(OI,CI)` `SVC:0x1301BF(OI,CI)` `BA:0x1200A9(OI,CI)` |
| L5 `backup\` | 目录 | `SY` | `SY:FA(OI,CI)` `SVC:0x1201BF(OI,CI)` `BA:0x1200A9(OI,CI)` |
| L6 `signer\tmp\` | 目录 | `SY` | `SY:FA(OI,CI)` `SVC:0x1301BF(OI,CI)` |
| **L7 `seals\`** | 目录 | `SY` | **`SY:FA(OI,CI)`（仅此一条）** |
| F1 `auth.db` | 文件 | `SY` | `SY:FA` `SVC:0x1301BF` |
| **F3** `key-<id>.dpapi` / `keyring-a|b.json` | 文件 | `SY` | **`SY:FA` `SVC:0x12019F`** |
| F4 `escrow\key-<id>.machine.dpapi` | 文件 | `SY` | `SY:FA` |
| F6 已封存备份 | 文件 | `SY` | `SY:FA` `SVC:0x1200A9` `BA:0x1200A9` |
| F7 `install-log\*.jsonl` | 文件 | `SY` | `SY:FA` |
| **F9** `seals\backup-seals.jsonl` | 文件 | `SY` | **`SY:FA`** |
| F8 `logs\*.log` | 文件 | 创建者 | 继承 L4 |

**掩码逐位核算**

| 掩码 | 拆解 | 含 | 不含 |
|---|---|---|---|
| `0x1200A9` | `0x100000+0x20000+0x80+0x20+0x08+0x01` | 读/列/遍历 | DELETE、DC、WDAC、WOWNER、写 |
| `0x1301BF` | `0x100000+0x20000+0x10000+0x1BF` | Modify（含 DELETE） | **DC**、WDAC、WOWNER |
| `0x1201BF` | `0x1301BF − 0x10000` | 写/读/遍历 | **DELETE**、DC、WDAC、WOWNER |
| **`0x12019F`** | `0x100000+0x20000+0x19F`（`0x19F=0x100+0x80+0x10+0x08+0x04+0x02+0x01`） | **F3** 读写+属性 | **DELETE**、DC、WDAC、WOWNER、X |

### 9.3 对象计数（删除表后回退）

| 库 | 表 | 触发器 | 视图 |
|---|---|---|---|
| `auth.db` | 7 | 20 | 5 |
| `trading.db` | 4 | 9 | 2 |
| **合计** | **11** | **29** | **7** |

```
表    ：V3.7 10 + V3.8 1 + V4.0 1 − V4.1 删 1   = 11
触发器：V3.7 23 + V3.8 净增 6 + V4.0 增 2 − V4.1 删 2 = 29
视图  ：V3.7 6 + V3.8 1                           = 7
```

| 项 | 本文件内联 | 合计 |
|---|---|---|
| 表 / 触发器 / 视图 | **0 / 0 / 0** | 11 / 29 / 7 |

> V4.1 **不内联任何 DDL**，因此这些数字无法由本文件 grep 得到；它们是"历史内联 − 本轮删除"的推导结果。**这是刻意的**，并在 §11 CE-232 中作为可复算项列出。

---

## 10. 风险状态总表（R-17 ～ R-43，单值）

| ID | 风险 | 状态 | 关闭条件 | 依据 | 阻断实施 |
|---|---|---|---|---|---|
| R-17 | SQLite 全局序号语义 | OPEN | S 探针通过 | V3.7 §12 | 否 |
| R-19 | Windows ACL 未实机验证 | OPEN | W-1…W-28 + W-43…W-60 | §12 | 是 |
| R-21 | 会话最终态 DB 不强制 | OPEN | 有 DB 约束或接受 P2 | V3.9 §16 | 否 |
| R-22 | auth.db 路径权威 | TEST-PENDING | W-13…W-16 | V3.9 §5 | 是 |
| R-23 | 二进制完整性 | OPEN（T9 下） | 不可关闭 | V3.9 §15 | 否 |
| R-24 | 父目录 ACL | TEST-PENDING | W-17/W-18 | §9 | 是 |
| R-25 | 跨决策证据绑定 | **TEST-PENDING**（V4.1 下调） | 静态审查 + V-6 实测 | V3.9 §8 | 否 |
| R-26 | trading.db DDL 拆 seal | OPEN | 不可关闭 | V3.8 §10.1 | 否 |
| R-27 | DPAPI 与口令 reset 耦合 | MITIGATED | 不采用 A 架构 | §6 | 否 |
| R-28 | 管理员 `SeRestorePrivilege` | OPEN | 不可关闭 | §7.3 | 否 |
| R-29 | 安装中断致半套 ACL | MITIGATED | **W-54/W-55** 通过 | §8.4 | 是（未验证前） |
| R-30 | reset 致 master key 不可恢复 | MITIGATED | B/B′ 下不适用 | §6.1 | 否 |
| R-31 | 安装器与服务间秘密传递 | DESIGN-CLOSED | 无传递路径 | §5（V4.0 §7 保留） | 否 |
| R-32 | 服务 profile 加载不可行 | MITIGATED | 不依赖 profile | V4.0 §8 | 否 |
| R-33 | S12 句柄身份复核误判 | MITIGATED | S12a/b + W-32 | §7 | **否**（V4.1 更新） |
| R-34 | 备份被 SVC 就地改写 | MITIGATED | W-48 通过 | §3.3 | 否 |
| R-35 | 临时文件出保护面 | TEST-PENDING | W-51…W-53 + S-16/S-17 | §8.2 | 是 |
| R-36 | 用污染 SDDL 回滚 | **DESIGN-CLOSED** | 回滚源=二进制常量 | §4 | 否 |
| R-37 | 普通用户独占打开 DoS | MITIGATED | 无 BU/BA 直读 | V4.0 §14 | 否 |
| R-38 | LocalMachine 边界被误解 | MITIGATED | §6 披露 + `health.protection` | §6 | 否 |
| R-39 | SYSTEM 封存组件攻击面 | OPEN | 最小化实现 + 审计 | §3 | 否 |
| R-40 | 大小写比较前提 | OPEN（低） | 补目录属性检查 | V4.0 §9.2 | 否 |
| R-41 | HMAC 64 字段未内联 | OPEN | 后续版本内联 | §8.4 | 否 |
| **R-42** | **`drift.flag` 可被 SVC 删除** | **OPEN** | 需"服务可写"与"抗 SVC"二者兼得，当前不可 | §7.3 | 否 |
| **R-43** | **SYSTEM 可整体改写 L7 哈希链** | **OPEN** | 不可关闭（与 T9 同域） | §3.2 | 否 |

**统计**：共 **25** 个活跃风险号。

| 状态 | 数量 | 编号 |
|---|---|---|
| `OPEN` | **11** | R-17, R-19, R-21, R-23, R-26, R-28, R-39, R-40, R-41, R-42, R-43 |
| `MITIGATED` | **8** | R-27, R-29, R-30, R-32, R-33, R-34, R-37, R-38 |
| `TEST-PENDING` | **4** | R-22, R-24, R-25, R-35 |
| `DESIGN-CLOSED` | **2** | R-31, R-36 |

`DESIGN-CLOSED` 仅 2 项，均满足"设计完整且不依赖未验证假设"：R-31（确实无秘密传递路径，可静态验证）、R-36（期望集来自二进制常量，无"被替换的文件"这一输入）。

---

## 11. 新增反例与正例

> 编号续 V4.0 与第十一轮审计：CE 从 **CE-211** 起，PS 从 **PS-101** 起。

### 11.1 负面反例 CE-211 ～ CE-232（22 条）

| ID | 前提 | 动作 | 预期 | V4.1 行为 | 依据 | 判定 |
|---|---|---|---|---|---|---|
| CE-211 | T3/T4（SVC） | `INSERT` 伪造封存记录 | 拒 | `auth.db` 已无该表；L7 无 SVC ACE | §3.1 | PASS |
| CE-212 | T3/T4 | 读 L7 `backup-seals.jsonl` | 拒 | L7 `SY:FA` 独占 | §9.2 | PASS |
| CE-213 | T3/T4 | 写/改 L7 文件 | 拒 | 同上 | §9.2 | PASS |
| CE-214 | 管理员 | 重写整条 L7 链 | 不可防 | 已披露 R-43 | §3.2 | PASS（已披露） |
| CE-215 | 分发者 | 替换 `policy-<ver>.json` | 拒 | 无"读 policy 文件"路径 | §4.1 | PASS |
| CE-216 | 管理员 | 用旧版已签名包 `--repair` | 拒 | `build < MinBuild` 拒绝 | §4.3 | PASS |
| CE-217 | 管理员 | 改 `HKLM\...\MinBuild` 后装旧包 | 不可防 | T9（R-28 同域） | §4.3 | UNCERTAIN |
| CE-218 | 实现者 | 照 §5.1 设 F3=`0x12019F` | 可行 | 服务可写新 blob/keyring，不可删 | §5.1 | PASS |
| CE-219 | SVC | 删 `key-<id>.dpapi` | 拒 | 无 `DELETE` | §5.1 | PASS |
| CE-220 | 实现者 | 用 `REPLACE_EXISTING` 覆盖旧 blob | 应避免 | 新 `key_id` → 新文件名；且无 `DELETE` | §5.2 | PASS |
| CE-221 | 实现者 | 用 rename 写 keyring | 会失败 | 改用 a/b 轮替 + `SetEndOfFile` | §5.3 | PASS |
| CE-222 | 崩溃 | 写 keyring-a 时崩溃 | 可恢复 | 读 epoch 较大的合法份 | §5.3 | PASS |
| CE-223 | 攻击者扰动 | 让自检失败触发降级 | 应阻断 | 无 `--allow-acl-only` 则 exit 75 | §6 | PASS |
| CE-224 | 运维 | 显式批准 `acl_only` 后 blob 被拷出 | 已披露风险 | `degraded_acl_only` 可见 + 记录操作者 | §6 | PASS |
| CE-225 | T3/T4 | B4-2 之前改写 `.db` | 拒 | 哈希在 B4-3（收紧后）计算 | §3.3 | PASS |
| CE-226 | T3/T4 | B4-4 之后改写备份 | 拒 | `SVC:0x1200A9` 只读 | §3.3 | PASS |
| CE-227 | 运维 | 服务运行时执行 B4 | 无冲突 | B4 不再碰 `auth.db` | §3.4 | PASS |
| CE-228 | T9 | S12a 失败 → exit 81 → SCM 重启 | 应持续拒绝 | `drift.flag` 前置检查 → 仍 81 | §7.2 | PASS |
| CE-229 | SVC | 删除 `drift.flag` 后重启 | 不可防 | R-42 OPEN（已披露） | §7.3 | UNCERTAIN |
| CE-230 | 构建 | `SQLITE_OMIT_TEMPDB` | 应明确中止 | 安装期检测 → 中止 | §8.2 | PASS |
| CE-231 | 构建 | `TEMP_STORE=3`（全内存） | 不应误阻断 | 记录 `temp=memory`，跳过 82 | §8.2 | PASS |
| CE-232 | 实现者 | 用旧规格掩码描述 F3（`SVC:R` 或 `0x1301BF`） | 应被文档自检挡住 | §5.1 声明"F3 只以 `0x12019F` 描述" | §5.1 | PASS |

**统计**：PASS 20 / FAIL 0 / UNCERTAIN 2。

### 11.2 正面场景 PS-101 ～ PS-110（10 条）

| ID | 场景 | 预期 | V4.1 行为 | 依据 | 判定 |
|---|---|---|---|---|---|
| PS-101 | 全新安装 7 目录 + 4 文件全部 ABSENT → 创建 | 通过 | 四态判定 + 带 SD 创建 | V4.0 §12 | PASS |
| PS-102 | B1–B4 全流程：写→改名→收紧→哈希→追加 L7 | 通过 | §3.3 顺序 | §3.3 | PASS |
| PS-103 | 恢复：L7 链校验 + DACL 断言 + 重算哈希一致 | 通过 | §3.5 R-1…R-6 | §3.5 | PASS |
| PS-104 | 轮换：新 `key_id` 新文件，旧 blob 保留 | 通过 | §5.2 | §5.2 | PASS |
| PS-105 | keyring a/b 轮替写入与崩溃恢复 | 通过 | §5.3 | §5.3 | PASS |
| PS-106 | 自检通过（machine-scope 可用）→ 不降级 | 通过 | 默认 `--require-dpapi` | §6 | PASS |
| PS-107 | 显式 `--allow-acl-only` → 记录 + 可见 | 通过 | §6 | §6 | PASS |
| PS-108 | 计数 11/29/7 与推导式一致 | 通过 | §9.3 | §9.3 | PASS |
| PS-109 | 无 BU/BA 直读时应用经 IPC 取数（含 nonce/限流） | 通过 | §8.3 | §8.3 | PASS |
| PS-110 | V3.9 §15 的 13 条保留项 + V4.0 的 6 条修复全部未弱化 | 通过 | §12 | §12 | PASS |

**统计**：PASS 10 / FAIL 0 / UNCERTAIN 0。

**累计**：反例 210 + 22 = **232**；正例 100 + 10 = **110**。

---

## 12. 保留项与验证计划

### 12.1 不得弱化的既有设计（逐条确认）

| # | 项 | V4.1 |
|---|---|---|
| 1 | 根路径唯一来自 `SHGetKnownFolderPath` | 保留 |
| 2 | 相对组件固定 | 保留 |
| 3 | 组件边界比较 | 保留 |
| 4 | UNC/设备/短名拒绝 | 保留 |
| 5 | 祖先逐级检查（扩至 L6/L7） | 保留 + 扩 |
| 6 | 句柄式安全描述符校验 | 保留 |
| 7 | A/B 证据物理隔离 | 保留 |
| 8 | signer 端 V-1…V-7 | 保留 |
| 9 | 历史 `real_llm` 仍标 transport unverified | 保留 |
| 10 | `real_llm_authenticated` 只由 signer 证据支持 | 保留 |
| 11 | R-26 继续 OPEN | 保留 |
| 12 | R-17/19/21/22/23/24/25 不自动关闭 | 保留（R-25 反向下调为 TEST-PENDING） |
| 13 | V4.0 的 6 项修复（架构解耦/无传递/S12 拆分/四态/取消 BU 直读/临时目录） | 全部保留 |

### 12.2 新增/续用验证项

| # | 项 | 状态 |
|---|---|---|
| W-48 | `0x12019F` 下可写不可删；封存后 `0x1200A9` 只读 | `[WINDOWS_TEST]` |
| W-54/W-55 | `CreateDirectory` 遇 `ERROR_ALREADY_EXISTS`；隔离重建 | `[WINDOWS_TEST]`（现为 R-29 关闭条件） |
| W-56 | `Global\` 互斥体对普通服务账户可用性 | `[WINDOWS_TEST]` |
| W-59 | `MinBuild` 防降级链路 | `[WINDOWS_TEST]` |
| W-60 | `drift.flag` 能否阻断 SCM 重启 | `[WINDOWS_TEST]` |
| W-61 | L7 `SY:FA` 下 SYSTEM 追加 + 回读 | `[WINDOWS_TEST]` |
| W-62 | keyring a/b 轮替在断电/崩溃下的可读性 | `[WINDOWS_TEST]` |
| S-24 | `SELECT COUNT(*) ... WHERE key_id=?` 在 10 万行下的性能 | `[SQLITE_PROBE]` |
| S-25 | `auth.db` 在服务运行中由 SYSTEM 只读打开的可行性（B5 前置） | `[SQLITE_PROBE]` |

> W-1…W-53、S-1…S-23 继续有效。

---

## 13. 残余风险与实施前置条件

### 13.1 不可消除的残余

| # | 残余 | 说明 |
|---|---|---|
| 1 | T9 强制替换（`SeRestorePrivilege` / POSIX rename） | R-28 |
| 2 | T9/SYSTEM 可整体改写 L7 链 | R-43 |
| 3 | SVC 可删除 `drift.flag` | R-42 |
| 4 | SQLite 写者可 `DROP TRIGGER` | R-26 |
| 5 | 备份内容是 SVC 写的，"字节一致"≠"语义正确" | §3.5 |
| 6 | S12a ≤30s 检测延迟 | §7 |
| 7 | 内存密钥无法证明清零 | — |

### 13.2 实施前置条件（缺一不可）

| # | 条件 | 状态 |
|---|---|---|
| 1 | 本方案通过第十二轮独立审计 | ❌ |
| 2 | W-43/W-46/W-48/W-50/W-51/W-59/W-60/W-61 通过 | ❌ |
| 3 | W-13…W-28 通过 | ❌ |
| 4 | S-1…S-25 通过 | ❌ |
| 5 | 二进制常量化的期望集 E 与 §9.2 逐条一致（代码级核对） | ❌ |
| 6 | SYSTEM 封存组件最小化实现 + 审计 | ❌ |

### 13.3 明确未做

```
未执行任何代码；未创建/修改源码、配置、测试、依赖、数据、日志；
未执行 SQL；未创建/打开 SQLite 库；未运行任何探针；
未执行 ACL 命令；未创建/修改/启动/停止服务；
未调用 DPAPI / 凭据 / 密钥相关 API；未生成或读取任何密钥；
未访问 VPS / 生产 API / 真实账户；未发起模型调用；未触碰 Git；
未创建 V4.2；未开展第十二轮审计。
```

---

## 附录 A 章节行号索引

| 章 | 起始行 |
|---|---|
| 1. 版本与基线 | 25 |
| 2. 第十一轮问题 → V4.1 处置总览 | 40 |
| 3. 备份封存重构（A11-P1-01 / P2-02 / P2-06） | 64 |
| 4. policy 信任根重构（A11-P1-02 / P2-04） | 158 |
| 5. 密钥文件 ACL 与轮换（A11-P1-03） | 205 |
| 6. 降级审批（A11-P2-01） | 248 |
| 7. 漂移持久化（A11-P2-03） | 263 |
| 8. 其余 P2/P3 处置 | 296 |
| 9. 权限矩阵与计数 | 349 |
| 10. 风险状态总表（R-17 ～ R-43，单值） | 419 |
| 11. 新增反例与正例 | 462 |
| 12. 保留项与验证计划 | 516 |
| 13. 残余风险与实施前置条件 | 554 |
| 附录 A 章节行号索引 | 592 |

---

> 本文件 SHA256（本行之前全部字节，可用 `head -n -1 <本文件> | sha256sum` 复算）：`8cde2bdce174daab8ee10efe5ce3d4ff903eeccb4e516bf52184d517472c9b47`
