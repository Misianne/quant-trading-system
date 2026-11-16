# V4.0 方案（设计稿 · 不实施）

| 项 | 值 |
|---|---|
| 版本 | **v4.0**（2026-11-11 17:35） |
| 取代 | `US_REAL_LLM_EVIDENCE_V3_9_SCHEME.md`（`1a72a1f5…`，**只读基线，不覆盖、不修改**） |
| 依据 | `US_REAL_LLM_EVIDENCE_V3_9_INDEPENDENT_AUDIT.md`（`a096f806…`，结论 **FAIL**：P1=3 / P2=5 / P3=6） |
| 性质 | **文档修订**。未执行 SQL / 探针 / 迁移 / ACL / 服务 / DPAPI / 凭据操作 |
| 范围 | 只修 3 条 P1 + 5 条 P2；**不重构**已通过的路径权威、组件比较、双库隔离、V-1…V-7 |
| 状态 | V4.0 待第十一轮独立审计；未获批；未获实施授权；S0 未启动 |

**状态标签（全文只用这 4 种）**

| 标签 | 含义 |
|---|---|
| `[DESIGN]` | 设计描述完成，逻辑自洽 |
| `[STATIC_REVIEW]` | 尚待独立静态审查 |
| `[WINDOWS_TEST]` | 尚待 Windows 实机验证 |
| `[SQLITE_PROBE]` | 尚待未来隔离探针验证 |

> 标签只用于**修订项**。风险另用 `OPEN / MITIGATED / DESIGN-CLOSED / TEST-PENDING`（§19），两者不互换。

---

## 1. 版本说明与 V3.9 基线

### 1.1 基线核验

| 文件 | 字节 | 行 | SHA256 | 自指纹 | 判定 |
|---|---|---|---|---|---|
| `US_REAL_LLM_EVIDENCE_V3_9_SCHEME.md` | 66,354 | 1,191 | `1a72a1f5…59f66` | `0a3a692e…f8c2` | ✅ 一致 |
| `US_REAL_LLM_EVIDENCE_V3_9_INDEPENDENT_AUDIT.md` | 46,069 | 654 | `a096f806…341418` | `0f735b21…31d8` | ✅ 一致 |
| V3.8 | — | — | `1c8ebce3…3b14f` | — | ✅ 未变 |
| V3.7 | — | — | `a4bc2964…c2efc46` | — | ✅ 未变 |
| V3.6 | — | — | `34c02c37…8ce223a` | — | ✅ 未变 |
| V3.5 | — | — | `3f308b17…599f9534` | — | ✅ 未变 |
| 原始修订计划 | — | — | `3d569f16…29610a` | — | ✅ 未变 |

保护目录：`src/`=51、`config/`=2、`tests/`=37、`data/`=6、`logs/`=68；17:00 后改动 **0**。

### 1.2 本轮的三条硬约束

| # | 约束 |
|---|---|
| 1 | 不为了让问题"看起来解决"而降低等级或改写审计结论 |
| 2 | 不引入无法验证的 Windows 行为断言；一律标 `[WINDOWS_TEST]` |
| 3 | 任何"设计闭环"必须不依赖未验证假设；否则记 `TEST-PENDING` 或 `OPEN` |

---

## 2. 第十轮审计问题总览

| ID | 级别 | 一句话 | 根因归类 | V4.0 处置章 |
|---|---|---|---|---|
| A-P1-01 | P1 | 把管理员 reset 当"change"，声称 master key 会被重加密 | 凭据生命周期语义错误 | §4 / §6 / §6.3 |
| A-P1-02 | P1 | 安装器与服务之间缺明文密钥通道 | 秘密传递未定义 | §7 |
| A-P1-03 | P1 | `LoadUserProfile` 对普通服务账户可行性未证 | 架构选型未做可行性比较 | §3 / §8 |
| A-P2-01 | P2 | S12 用同一句柄复核 file id 是恒真命题 | 验证目标定义错误 | §9 |
| A-P2-02 | P2 | L5 `0x12019F` 含写权 → 备份可就地改写 | 掩码与目标不一致 | §10 / §16 |
| A-P2-03 | P2 | SQLite 临时文件落在 `%TEMP%` 之外保护面 | 保护面遗漏 | §11 |
| A-P2-04 | P2 | 回滚用现场导出的 SDDL（可能是攻击者的） | 回滚可信源错误 | §12 / §13 |
| A-P2-05 | P2 | `BU:RX` → 任意用户独占打开致持久 DoS | 权限给多了 | §14 / §16 |

### 2.1 处置策略（一句话）

> **不再"修补" A-P1-01/02/03 的流程，而是换掉产生这三个问题的前提**：把 HMAC 密钥的保护从"用户作用域 DPAPI + 服务账户 profile"改为"机器作用域/纯 ACL + 服务自主生成"，使口令与 profile 两条生命周期**与密钥彻底解耦**。三条 P1 因此同时失去成立条件（§3、§7）。

---

## 3. 凭据与 DPAPI 架构比较

### 3.1 三个候选架构

| 代号 | 密钥保护方式 | 是否需要用户 profile | 是否与账户口令耦合 |
|---|---|---|---|
| **A** | DPAPI **CurrentUser**（V3.9 方案） | 是（`LoadUserProfile`） | **是**（master key 由口令派生密钥加密） |
| **B** | DPAPI **LocalMachine**（`CRYPTPROTECT_LOCAL_MACHINE`）+ 文件 ACL | 否 | 否 |
| **C** | 独立密钥保护服务 / CNG 持久化密钥 | 否 | 否 |

### 3.2 十项准则比较

| 准则 | A CurrentUser | B LocalMachine | C 独立密钥服务 / CNG |
|---|---|---|---|
| 服务启动可行性 | **未证**：需 profile 已加载；`LoadUserProfile` 的特权要求见 §8.2 | 高：无需 profile；仅需读 blob + 机器 master key（**可解性待验证**） | 中：需 KSP 支持 HMAC（见下） |
| 所需账户权限 | 普通账户 + profile 加载能力（未证） | 普通账户 + blob 读权（ACL 给定） | 普通账户 + 密钥句柄/ACL 授权 |
| 密钥保护边界 | "该用户 + 该 profile" | **"能读 blob 且能访问机器 master key 的进程"**——**不是**"仅 signer" | "被 ACL 授权的调用方"，且密钥不可导出 |
| 本地管理员威胁 | 可加载该 profile 解密（T9） | 可读机器 master key 解密（T9） | 可改 ACL / 可提权调用（T9） |
| 口令 reset 影响 | **密钥永久失效**（R-30） | **无影响** ✅ | 无影响 ✅ |
| 账户 SID 变化 | blob 与新 profile 不匹配 → 失效 | 无影响（machine 与 SID 无关）✅ | 需重建密钥对象 |
| 密钥恢复能力 | 仅靠 escrow | escrow 仅防 blob 损坏 | 依赖 KSP 导出能力（通常不可导出 → 不可恢复） |
| 历史证据兼容 | reset 后历史证据可能全部 `mac_unknown_key` | 不受口令事件影响 ✅ | 同 B |
| 部署/升级复杂度 | 高（profile、口令、escrow 三者联动） | **低**（一个 blob + 一条 ACL） | 高（P/Invoke、KSP 兼容性） |
| 需要的实机验证 | W-40…W-42 | W-43、W-44 | W-45（HMAC 支持性） |

### 3.3 C 的可行性判定（不得含糊）

| 事实 | 结论 |
|---|---|
| Microsoft 软件 KSP 的持久化密钥以**非对称**为主 | 对 HMAC 这类对称运算，`NCryptSignHash` 是否可用**未证实** |
| 若必须导出对称密钥到进程内做 `BCryptHashData` | 导出即失去"不可导出"的收益 |
| 结论 | **C 不作为主方案**；记为 `TEST-PENDING` 备选，理由不是"不好"，而是"HMAC 支持性未证" |

### 3.4 选定：**B（主）+ 安装期可用性自检 + C 备选**

| 决策 | 内容 |
|---|---|
| 主方案 | DPAPI **LocalMachine** 保护 HMAC 密钥 blob；blob 文件 ACL 为 `SY:FA + SVC:R` |
| 为什么不是 A | A 把"密钥可用性"绑在"账户口令 + profile"上，任何一次管理员 reset 都可能**不可逆**地摧毁验签能力（R-30）；且 profile 加载可行性未证（R-32）。B 把两条生命周期彻底解耦 |
| 为什么不是 C | HMAC 支持性未证（§3.3） |
| **必须明示的边界** | **DPAPI LocalMachine 不等于"只有 signer 能解密"**。它的实际保护边界是：① blob 文件 ACL 允许谁读；② 谁能访问机器 master key（`%WINDIR%\System32\Microsoft\Protect\S-1-5-18\`，其 ACL 由系统管理，**`[WINDOWS_TEST]` 需实测非管理员可否读取**）。任一条件被满足即可解密 |
| 降级路径 | 若安装期自检发现 SVC **无法**解开 machine-scope blob → 自动切换到 **B′：纯 ACL 保护（明文 blob）**，并在 keyring 记 `protection="acl_only"` |
| 对降级的态度 | **不隐藏**：`protection` 字段对外可读（`health` 接口）；`acl_only` 意味着"blob 被拷出即等于密钥泄露"，必须写入运维手册与事件日志 |

### 3.5 关于"密钥保护等级"与 verdict 的关系（防止用加密层冒充真实性）

| 规则 | 理由 |
|---|---|
| `protection` 取值 **不参与** `real_llm_authenticated` 的判定 | B 等级讲的是"证据来自真实传输"，与"密钥静态保护强度"是两件事；绑定会导致"换个存储方式就能提级/降级"的错觉 |
| `protection` 只出现在 `health` 与 `readiness` 的**诊断字段** | 可观测，但不改变证据语义 |
| 若 `protection` 缺失或非法 | `health=degraded`，服务仍可运行（因为不影响证据语义），但必须告警 |

### 3.6 五组决策汇总

| # | 决策 | 选择 | 理由 | 备选 | 验证 |
|---|---|---|---|---|---|
| D-1 | 密钥保护 | **B：LocalMachine + ACL** | 与口令/profile 解耦；部署最简 | C（HMAC 支持性未证）、A（R-30/R-32） | W-43/W-44 |
| D-2 | 谁来处理密钥 | **signer 服务自主** | 安装器永不接触明文（§7） | 安装器处理（需 IPC 传秘密，风险高） | W-46 |
| D-3 | 轮换策略 | **账户口令轮换** 与 **密钥轮换（新 key_id）** 两条独立生命周期 | 解耦后互不影响 | 新账户+新 key_id（仅账户被怀疑时） | W-47 |
| D-4 | 备份形态 | **封存备份**（写→校验→SYSTEM 封存→auth.db 记录） | 让篡改可检测 | 普通文件（不可检测） | W-48/W-49 |
| D-5 | auth.db 读取 | **取消直读，改经 signer 受控查询** | 同时消灭 A-P2-05 的 DoS 面 | 保留 `BU:RX`（DoS 不可消除） | W-50 |

---

## 4. 正常密码变更状态机

### 4.1 先厘清事实（不假设，只陈述可核验的语义）

| # | 事实 | 状态 |
|---|---|---|
| F-1 | `NetUserChangePassword` 需提供**旧口令**，属于"用户知情的变更" | `[WINDOWS_TEST]` 语义确认 |
| F-2 | 管理员路径 `NetUserSetInfo(USER_INFO_1003)` 不校验旧口令，属于**设置/重置** | `[WINDOWS_TEST]` 语义确认 |
| F-3 | 口令**变更**流程会触发用新口令重新加密该用户的 DPAPI master key | `[WINDOWS_TEST]` |
| F-4 | 口令**重置**不会重新加密 master key，旧 master key 随之不可解 | `[WINDOWS_TEST]` |
| F-5 | 本架构（B/B′）下，C1（账户口令）与 C2（HMAC 密钥 blob）**无耦合** | `[DESIGN]`（由 §3.4 保证） |

> **V3.9 的错误**：把 F-2 的调用写成 "change 语义"，并据此在 §10.9 断言 "master key 被重新加密"。V4.0 **不再做任何"调用 X 就会自动重加密"的断言**；F-3/F-4 本身也标 `[WINDOWS_TEST]`。

### 4.2 变更状态机（本架构下的推荐路径）

| 步 | 前置条件 | 动作 | 成功条件 | 失败状态 | 恢复动作 | 历史证据影响 |
|---|---|---|---|---|---|---|
| P0 | 服务处于 `STOPPED` | 检查 `maintenance` 标记未置位 | 无标记 | `E_MAINT` | 清除标记或等待安装完成 | 无 |
| P1 | — | 以 SVC 之外的管理上下文调用 `NetUserChangePassword(NULL, L".\svc_fosun", old, new)` | 返回 `NERR_Success` | `E_PWD_CHANGE` | 重试或改走 §5 的 reset 流程 | 无（密钥不依赖口令） |
| P2 | P1 成功 | `ChangeServiceConfig(SERVICE_NO_CHANGE, …, lpPassword=new)` | 返回成功 | `E_SCM_SYNC` | **立即回滚 P1**（用 new→old 反向变更） | 无 |
| P3 | P2 成功 | 启动服务 | 服务 S0–S12 通过 | `E_START` | 见 §13 恢复入口 | 无 |
| P4 | P3 成功 | 服务写 `logs\cred-rotation.log`（不含任何口令） | 记录成对出现 | — | — | 无 |

| 关键点 | 说明 |
|---|---|
| 旧口令从哪来 | **不由本方案保存**。要么操作者在变更时提供；要么改走 §5 的 reset（并承担其后果）。**不为了"省事"而在任何位置持久化旧口令** |
| 是否需要 escrow | **不需要**（B/B′ 下密钥与口令无耦合）。escrow 只剩"防 blob 损坏"的作用（§6.4） |
| 顺序 | 必须先改账户、再改 SCM；反序会出现"SCM 持新口令、账户仍旧口令"→ 服务 1069 |
| 回滚 | P2 失败必须回滚 P1，否则账户与 SCM 不一致 |

---

## 5. 管理员密码重置状态机

### 5.1 事实与后果

| 项 | A 架构（CurrentUser，**未选**） | B / B′ 架构（**已选**） |
|---|---|---|
| 旧 master key | **不可解**（F-4） | 不适用（不使用用户 master key） |
| HMAC 密钥 blob | 不可解 → 验签能力**永久失效** | **不受影响** ✅ |
| 历史证据 | 全部 `mac_unknown_key` | 仍可正常验证 ✅ |
| 服务能否启动 | 不能（fail-closed，退出码 75） | 能（与口令无关，仅需 SCM 同步新口令） |

> 这张表是本轮最重要的结论：**A-P1-01 的破坏力来自架构 A 本身，不是来自某个函数调用写错**。V4.0 换架构后，reset 从"灾难事件"降级为"运维事件"。

### 5.2 重置状态机

| 步 | 前置条件 | 动作 | 成功条件 | 失败状态 | 恢复动作 | 历史证据影响 |
|---|---|---|---|---|---|---|
| R0 | 服务 `STOPPED` | 记录事件（谁、何时、为何） | 事件已落 `logs\cred-reset.log` | `E_LOG` | 修日志路径后重试 | 无 |
| R1 | — | `NetUserSetInfo(..., USER_INFO_1003, newpwd)` | 成功 | `E_RESET` | 中止 | 无（B/B′） |
| R2 | R1 成功 | `ChangeServiceConfig(lpPassword=newpwd)` | 成功 | `E_SCM_SYNC` | **不得回滚 R1**（reset 已发生，回滚无收益且会再触发一次 reset）；改为把服务置 `DEGRADED_DISABLED` 并告警 | 无 |
| R3 | R2 成功 | 启动服务 | S0–S12 通过 | `E_START` | §13 恢复入口 | 无 |
| R4 | R3 成功 | 服务自检：读 blob → 解封 → 对固定向量算 MAC | 与期望值一致 | `E_KEY_UNUSABLE` | 走 §6.4 escrow 恢复 | 无 |

### 5.3 若部署方**坚持**使用 A 架构（明确不支持，但给出 fail-closed）

| 情形 | 行为 |
|---|---|
| reset 后 master key 不可解 | 服务**拒绝启动**（退出码 75），**禁止**静默改用 machine scope 或静默生成新密钥 |
| 是否允许"生成新 key_id 继续跑" | 允许，但必须**显式**由管理员执行 `key-init --new-id`，且：① 旧 `key_id` 永久标 `unusable`；② 所有依赖旧 key 的证据在 `resolve` 中返回 `upstream_session_unverified`；③ **不得**把新 key 用于验证旧证据；④ UI 与报告中必须显示"历史证据不可认证" |
| 禁止事项 | 禁止"悄悄换密钥后照常输出 `real_llm_authenticated`" |

---

## 6. 密钥轮换与恢复状态机

### 6.1 三个对象的关系（必须写死，避免再混）

```
C1 账户口令        → 只影响 SCM 能否登录；不影响 C2
C2 HMAC 密钥 blob  → 只受"blob ACL + 机器 master key"影响；不受口令事件影响
C3 escrow 副本     → 仅用于 C2 损坏/丢失时重建；同一保护边界内的第二副本

key_id      → 一个 C2 blob 的标识；1 个 key_id ↔ 1 份 HMAC 密钥
master key  → DPAPI 的机器主密钥（B 架构下），与 key_id 无一一对应
历史证据    → 记录 key_id；验证时按 key_id 找 blob；blob 不在 → mac_unknown_key
```

| 断言 | 说明 |
|---|---|
| 一个 `key_id` 只对应一个密钥 | keyring 中 `key_id` 唯一 |
| 删除 blob **不会**让历史证据变"有效" | 只会让它们变 `mac_unknown_key` |
| **禁止**用新密钥验证旧证据 | cryptographically 不成立，且会被伪装成"仍然有效" |

### 6.2 触发方式：**服务自主**（无远程触发、无安装器参与）

| 触发条件 | 动作 | 备注 |
|---|---|---|
| 首次启动且 keyring 为空 | 生成 `key_id` + 32 字节密钥 → 解封为 blob → 写 keyring | 全流程在服务进程内 |
| 轮换周期到（`rotation_days`，默认 90） | 同上，新增一个 active key | 策略值来自 L2 的非秘密策略文件 |
| 管理员需要轮换 | 在**服务本机**以 SYSTEM 运行 `fosun-signer.exe key-rotate`（命令行参数**不含秘密**） | 服务自己生成，安装器只发"做一次"的指令 |
| 怀疑泄露 | `key-rotate --reason=compromise` → 旧 key 立即 `retired`，**只验不签** | 需人工确认吊销时点 |

> 关键：**任何"秘密"都不跨越进程边界**。安装器的角色退化为"创建账户 + 建目录 + 置 ACL + 启动服务"。

### 6.3 轮换序列（旧 key 的保留与退役顺序）

| 步 | 动作 | 此时可用性 |
|---|---|---|
| K1 | 生成新 `key_id`，写 `key-<new>.dpapi` + escrow 副本 | 旧 key 仍 active |
| K2 | keyring 追加：新 key `state=active`；旧 key `state=retiring` | 新旧并存，**签用新、验用新+旧** |
| K3 | `rotation_window`（默认 7 天）内持续双验 | — |
| K4 | 窗口结束 → 旧 key `state=retired`：**只验不签** | 新证据只可能来自新 key |
| K5 | 证据保留期结束 → `state=deletable`；管理员显式 `key-purge` 才删除 | 删除后旧证据 → `mac_unknown_key` |

| 禁止顺序 | 后果 |
|---|---|
| 先删旧再发新 | 窗口期内已签证据无法验证 |
| 先改 keyring 再写 blob | 服务可能引用不存在的 key |
| 并发两个轮换 | 两个 `active` → 判定歧义（keyring 写入需互斥锁 + 单实例） |

### 6.4 escrow 的产生 / 验证 / 保管 / 恢复 / 销毁

| 环节 | 设计 |
|---|---|
| 产生 | 与 C2 同时生成：同一密钥，分别用 `CRYPTPROTECT_LOCAL_MACHINE`（与 C2 相同保护）解封为两份 blob；**不是**"两个不同密钥" |
| 位置 | `signer\escrow\key-<id>.machine.dpapi`；DACL `SY:FA`（**SVC 无任何权限**） |
| 验证 | 安装期自检 + 每次服务启动：以 SYSTEM 侧工具（或安装器 `--verify-escrow`）解封并比对派生指纹（HMAC(key, 固定向量) 前 8 字节），**不比对明文** |
| 保管 | 与 C2 同目录链保护；**不允许**导出到网络盘、U 盘、日志 |
| 恢复 | C2 损坏 → SYSTEM 解 escrow → **在服务内**重新解封为 C2（通过 `key-restore` 子命令，明文不跨进程） |
| 销毁 | `key-purge --id=<id>` 同时删 C2 与 C3；删除前要求 `state=deletable` 且无引用 |
| 安全增益的诚实说明 | C3 **不增加**对管理员的抵抗力（管理员本就能解 C2）；它只把"blob 损坏 = 永久失能"变为"可恢复"。**因此 R-27 由"缓和"改为"部分闭环，仍有 operator 依赖"** |

### 6.5 恢复失败 = 明确的终态（禁止静默补新）

| 情形 | 行为 |
|---|---|
| C2 与 C3 都不可解 | `key_state=lost`；服务**可以启动**但只提供 `unavailable` / `upstream_session_unverified`，**绝不**输出 `real_llm_authenticated` |
| 是否自动生成新 key | **否**。必须管理员显式 `key-init --new-id --reason=loss`，且 UI/日志标注"历史证据不可认证" |
| 恢复失败是否影响 A 证据 | 不直接影响；但 signer 无法给出 B 等级 → 应用侧最高只能拿 `app_record_unverified` |

---

## 7. 安装器与 signer 服务秘密处理

### 7.1 三个候选方案 × 十二项准则

| 准则 | A 双向认证命名管道传秘密 | **B 服务自主处理（选定）** | C 新账户 + 新 key_id |
|---|---|---|---|
| 身份认证 | 服务端 `ImpersonateNamedPipeClient` + `GetTokenInformation(TokenUser)` 要求 `S-1-5-18`；客户端验证服务进程 SID=`S-1-5-18` + 映像签名 | **不需要**（无秘密传递） | 需账户创建流程（管理员） |
| 访问控制 | 管道 SDDL：`D:(A;;GA;;;SY)(A;;GA;;;<SVC>)`，`PIPE_REJECT_REMOTE_CLIENTS` | 无管道 | 账户 + SCM |
| 消息完整性 | HMAC over (cmd‖nonce‖seq) 或依赖本地管道 + SID 认证 | 无消息 | — |
| 防重放 | 一次性 nonce + 单调 `seq`（内存表，重启清零并要求重连） | 无 | — |
| 进程身份绑定 | 客户端 PID → `OpenProcess` → 查映像路径 + Authenticode + 签名者 pin | 无 | — |
| 明文存在时间 | 安装器内存 + 管道缓冲区（**双份**） | **仅服务内存** | 无明文 |
| 内存清理 | `SecureZeroMemory` 后仍可能被换页/转储；**无法证明已清除** | 同上（单进程） | 无 |
| 崩溃/重启 | 管道断开 → 明文丢失 → 需重来（可能留下半写 blob） | 服务崩溃 → 未提交的 key 不进入 keyring（先写 blob 再写 keyring，失败则孤儿 blob 由 GC 清理） | 重来成本高 |
| 审计日志 | 记录命令、SID、PID、结果（**不含秘密**） | 记录 `key-init/rotate` 的触发源与结果 | 记录账户变更 |
| 失败回滚 | 需两阶段（blob 落盘后才写 keyring） | 同上，天然两阶段 | 删账户 |
| 攻击面 | **新增**：本地任意进程可尝试连管道；解析器风险；伪造 SYSTEM 令牌需已提权 | **最小**（无新端点） | 账户创建接口（管理员） |
| 历史证据连续性 | 好（同 key_id 重保护） | 好 | 差（新 key_id，旧证据仍可验但需保留旧 key） |

### 7.2 选定 B，并给出理由

| 理由 | 说明 |
|---|---|
| ① 消灭秘密传递 | 没有传递就没有"传输出错"这一整类问题；A-P1-02 的成立条件消失 |
| ② 安装器降权 | 安装器不再需要接触任何密钥材料，只做文件系统/账户/服务配置 |
| ③ 与 D-3 一致 | 密钥生命周期由持有密钥的一方负责，职责单一 |
| ④ 攻击面更小 | 不新增本地 IPC 端点，不引入新的解析器 |
| 代价 | 首次密钥生成必须在"服务已能启动"之后；因此**安装完成 ≠ 可用**，需要一次 `key-init`（可由服务在首次启动时自动完成） |

### 7.3 若未来确需人工触发（受限设计，非当前路径）

> 仅当出现"服务无法自启动但需轮换"的运维场景才启用；默认**不启用**。

| 要求 | 落地 |
|---|---|
| 管道创建 | `CreateNamedPipeW(L"\\\\.\\pipe\\fosun-signer-ctl", PIPE_ACCESS_DUPLEX, PIPE_TYPE_MESSAGE|PIPE_READMODE_MESSAGE|PIPE_REJECT_REMOTE_CLIENTS, 1, …)` |
| 管道 ACL | `SECURITY_ATTRIBUTES` 携带 SDDL `D:(A;;GA;;;SY)(A;;GA;;;<SVC>)` |
| 服务端验客户端 | `ImpersonateNamedPipeClient` → `OpenThreadToken` → `GetTokenInformation(TokenUser)` → 必须 `S-1-5-18`；否则断开 |
| 客户端验服务端 | 客户端先 `GetNamedPipeServerProcessId` → `OpenProcess` → 校验映像路径 + Authenticode + 签名者 pin |
| 消息格式 | `{v, cmd, nonce(32B), seq, args_digest}`；服务维护"已用 nonce"内存集合（上限 N，FIFO） |
| 禁止 | 消息体**不含**任何密钥/口令；命令仅 `rotate|status|verify-escrow` |
| 超时 | 连接 5s、单请求 10s；超时即断开并计数 |
| 洪泛防护 | 单实例 + 全局并发 1 + 每 SID 每分钟 5 次 |

### 7.4 明令禁止的做法

| 禁止 | 原因 |
|---|---|
| 把密钥写入磁盘临时文件 | 落盘即扩大保护面 |
| 通过命令行参数传密钥 | 进程命令行对其他进程可见（`NtQueryInformationProcess`/WMI） |
| 通过环境变量传密钥 | 子进程继承、可被 dump |
| 写入普通日志/配置 | 同上 |
| 把"本机 IPC"默认视为安全通道 | 本地任意进程都能尝试连接；必须显式认证 |

---

## 8. 服务账户配置文件生命周期

### 8.1 本架构下的结论：**不需要加载 profile**

| 事实 | 推论 |
|---|---|
| B/B′ 使用 DPAPI **LocalMachine** 或纯 ACL，不使用用户 master key | 无需 `%APPDATA%\Microsoft\Protect\<SID>` |
| 因此不需要 `LoadUserProfile` | A-P1-03 的成立条件消失 |
| 仍需要 `UnloadUserProfile` 吗 | **不需要**（从未加载）。若实现中出于其他原因加载了，必须在停止时卸载 |

### 8.2 对 `LoadUserProfile` 的客观记录（保留，不删，供将来参考）

| # | 调查项 | V4.0 结论 | 状态 |
|---|---|---|---|
| 1 | 实际调用要求 | 公开文档记载需要备份/还原类特权；**未实测，不作断言** | `[WINDOWS_TEST]` W-40 |
| 2 | 特权与账户权限关系 | 普通账户默认不具备备份/还原特权（待确认） | `[WINDOWS_TEST]` |
| 3 | SCM 登录是否自动加载 profile | **不自动**（服务 token 不携带 profile）——此点为设计前提，需实测确认 | `[WINDOWS_TEST]` W-41 |
| 4 | `CreateProfile` vs `LoadUserProfile` | 前者在磁盘/注册表建立 profile 骨架；后者把 hive 挂到 `HKEY_USERS\<SID>`；**两者都不能保证 master key 已生成** | `[WINDOWS_TEST]` |
| 5 | 服务启动时 profile 可用性 | 本架构不依赖 | — |
| 6 | DPAPI CurrentUser 的用户上下文要求 | 进程 token 的 SID + 已加载 profile + 存在的 master key，三者缺一不可 | `[WINDOWS_TEST]` |
| 7 | profile 损坏/丢失/未加载 | 本架构不受影响 | — |
| 8 | reset / SID 变化 / 账户重建 | 本架构**不受影响**（密钥与账户解耦）✅ | `[DESIGN]` |
| 9 | `UnloadUserProfile` 条件 | 仅当曾 `LoadUserProfile` 成功时才需卸载；否则调用会失败 | `[DESIGN]` |
| 10 | 并发实例 / profile 句柄未释放 | 服务设为**单实例**（命名互斥体 `Global\FosunSignerSingle`），从根上避免两份 profile 句柄 | `[DESIGN]` |

> **不作断言原则**：本节不写"普通账户一定不能调用 `LoadUserProfile`"，只写"未证；因此不依赖它"。

### 8.3 单实例约束（新增，解决第 10 项）

| 机制 | 内容 |
|---|---|
| 互斥体 | 启动首步 `CreateMutexW(NULL, TRUE, L"Global\\FosunSignerSingle")`；`ERROR_ALREADY_EXISTS` → 退出码 **80**（多实例） |
| 为什么用 `Global\` | 防止不同会话各起一个 |
| 与 SCM 的关系 | 服务本身单实例；额外实例只能是手工启动，直接拒绝 |

---

## 9. S12 文件身份验证修订

### 9.1 两个必须分开的概念

| 概念 | 定义 | 能否用"同一句柄"验证 |
|---|---|---|
| **句柄对象身份** | 该 `HANDLE` 指向的内核对象（由 `VolumeSerial + FileIndex` 标识），句柄开着就不会变 | ✅ 能，但**恒真**，无检测价值 |
| **路径解析结果** | 此刻把 `…\auth.db` 这个字符串解析到哪个对象 | ❌ 不能。必须**重新按路径打开**才能得到 |

> V3.9 的错误：用前者去证明后者。**同一句柄重复读 file id 永远返回同一值，与路径是否被重定向无关。**

### 9.2 修订：S12 拆成 S12a / S12b，目标各自明确

| 步 | 名称 | 动作 | 能检测什么 | 不能检测什么 |
|---|---|---|---|---|
| **S12a** | 路径解析复核（每 30s） | **重新** `CreateFileW(常量路径, OPEN_REPARSE_POINT, share=READ\|WRITE)` → `GetFileInformationByHandle` → 与 S9 记录的 `(VolumeSerial, FileIndex)` 比对；再 `GetSecurityInfo(新句柄)` 比对 DACL 摘要；随即关闭该临时句柄 | ① 路径被重定向；② 文件被替换；③ 目录被整体替换 | 两次检查之间的窗口内发生又撤销的替换 |
| **S12b** | 对象属性复核（每 60s） | 用 **S8 的原 guard 句柄** `GetSecurityInfo` 比对 DACL 摘要 | 该对象的 DACL 是否被就地修改 | 路径重定向（**明确不用于此**） |

| 规则 | 内容 |
|---|---|
| S12a 失败 | 立即停止对外服务：所有 `resolve` 返回 `unavailable`；写事件日志；**不自动修复**；退出码 **81**（路径漂移） |
| S12a 的临时句柄 | 必须 `CLOSE` 掉；**不能**用它替代 S8 的 guard 句柄（否则 guard 语义被稀释） |
| S12b 失败 | 同 S12a（DACL 被改 = 保护边界变了） |
| 频率 | S12a 30s、S12b 60s；两者都失败即停服 |
| 开销 | 每次 1 次 open + 1 次查询，可忽略 |

### 9.3 数据库已打开后路径被替换怎么办

| 问题 | 答案 |
|---|---|
| 已建立的 SQLite 连接会"跟着"换文件吗 | **不会**。SQLite 在 `open` 时已持有文件句柄，该句柄绑定原对象；替换路径不会改变既有连接的读写目标 |
| 那还有什么风险 | ① **新**连接会解析到新对象；② 备份/恢复流程若按路径重新打开，会操作到错误对象；③ 进程重启后按路径打开 → 得到替换后的对象 |
| 因此规则 | ① **只要 S12a 失败就停服**（不给"新连接"机会）；② 任何"重开数据库"的动作（升级、恢复、迁移）前必须重跑 S11（含 S12a 的比对）；③ 服务重启一律重跑 S0–S12 |

### 9.4 SQLite 各文件的对象身份关系

| 文件 | 生命周期 | 能否长期 pin | 身份如何保证 |
|---|---|---|---|
| `auth.db` | 长期 | ✅ guard handle | VolumeSerial+FileIndex + DACL 双检 |
| `auth.db-journal` | 每事务创建/删除 | ❌ | 依赖 **L3 目录 ACL**（不可被他人创建/替换）+ `journal_mode=DELETE` |
| `auth.db-wal` / `-shm` | 本方案不产生 | — | 出现即拒绝启动（V3.9 §12 保留） |
| 临时文件 | 查询期间 | ❌ | §11 重定向到 L6 |

> 明确：`-journal` **无法**用句柄身份保护（它每次都是新对象）；其安全性完全由"L3 目录内只有 SVC 能创建文件"承担。

### 9.5 无法消除的竞态

| 竞态 | 残余 |
|---|---|
| S12a 两次检查之间的窗口（≤30s） | 检测延迟，不是预防；预防靠 guard handle + 目录 ACL |
| 管理员 `SeRestorePrivilege` + POSIX rename | R-28，OPEN，不声称解决 |
| 服务停止期间被替换 | 下次启动 S0–S12 全量复核会发现（若替换者未同时伪造 ACL 与路径） |

---

## 10. 备份写入、验证与封存

### 10.1 五阶段

| 阶段 | 主体 | 动作 | 权限要求 |
|---|---|---|---|
| B1 创建 | SVC | 在 `backup\` 建 `auth-<ts>.db.part` | 需 L5 的 `ADD_FILE` + `WRITE_DATA` |
| B2 写入 | SVC | 写数据 → `FlushFileBuffers` | 同上 |
| B3 校验 | SVC | `PRAGMA integrity_check` + 逐表 digest；与源库比对 | 需 `READ_DATA` |
| B4 封存 | **SYSTEM**（安装器 `--seal-backups` 或计划任务） | ① 计算 `sha256(file)`；② `SetNamedSecurityInfoW` 把该文件 DACL 改为 `SY:FA + SVC:R + BA:R`；③ 写入 `auth.db` 的 `ai_backup_seal`（append-only）；④ 改名为 `auth-<ts>.db` | 需 `WRITE_DAC`（SYSTEM 有） |
| B5 恢复 | 管理员 + 服务 | 停服 → 校验 `.sha256` **与** `ai_backup_seal` 记录一致 → 替换 → 重跑 S0–S12 | — |

### 10.2 关键规则

| 规则 | 内容 |
|---|---|
| `.sha256` 的定位 | **只是校验和，不是签名**。SVC 能改它，因此**不得**把它当可信来源。可信来源是 `auth.db` 中由 signer 写入的 `ai_backup_seal` 记录 |
| 封存后 SVC 的权限 | 只读（`0x1200A9`）。**不能**写、不能删、不能改名、不能改 DACL |
| 未封存的 `.part` | 允许 SVC 写/删（清理失败残留）；**不参与恢复**，恢复流程只认 `ai_backup_seal` 中有记录的封存文件 |
| SVC 能否篡改已封存备份 | **不能**（§16 掩码复算） |
| 如果 SVC 在 B4 之前篡改 | `.part` 阶段可篡改，但 B3 的 digest 由 SVC 自己算 → **同一主体**，无法防。因此 B4 的 `seal_hash` 必须由 SYSTEM **重新计算**文件 sha256 后写入 `auth.db`（不复用 SVC 的结果） |
| 残余 | 若 SYSTEM 组件长期不可用 → 备份停留在 `.part`，`auth.db` 无 seal 记录 → `readiness` 报 `backup_unsealed` 告警，**不阻塞**服务运行（备份不是认证路径） |

### 10.3 `ai_backup_seal` DDL（V4.0 新增，auth.db）

```sql
CREATE TABLE ai_backup_seal (
    backup_name   TEXT PRIMARY KEY NOT NULL
                  CHECK (length(backup_name) > 0 AND backup_name NOT GLOB '*[/\\]*'),
    file_sha256   TEXT NOT NULL CHECK (length(file_sha256) = 64
                    AND file_sha256 NOT GLOB '*[^0-9a-f]*'),
    file_bytes    INTEGER NOT NULL CHECK (file_bytes > 0),
    sealed_at     TEXT NOT NULL CHECK (length(sealed_at) > 0),
    sealer        TEXT NOT NULL CHECK (sealer = 'SYSTEM')
);

CREATE TRIGGER trg_backup_seal_no_update
BEFORE UPDATE ON ai_backup_seal
BEGIN SELECT RAISE(ABORT,'backup seal append-only'); END;

CREATE TRIGGER trg_backup_seal_no_delete
BEFORE DELETE ON ai_backup_seal
BEGIN SELECT RAISE(ABORT,'backup seal append-only'); END;
```

> 与 R-26 一致：**写者若有 DDL 权限仍可 `DROP TRIGGER`**，本表不因此获得"不可篡改"地位。它只是让"备份被换"这件事在**诚实写者**假设下必须留下记录。

### 10.4 失败恢复

| 失败点 | 状态 | 恢复 |
|---|---|---|
| B1/B2 中断 | `.part` 残留 | 下次启动 GC 删除超过 24h 的 `.part`（SVC 可删） |
| B3 校验失败 | `.part` 保留供诊断 | 不封存，告警 |
| B4 部分完成（改名后、写 seal 前） | 文件已封存但无 seal 记录 | `--seal-backups` 幂等重跑：文件已存在且无记录 → 计算并补写记录（`backup_name` 主键防重复） |
| B4 seal 写入失败 | 文件已改名、无记录 | 同上；期间该备份不可用于恢复 |

---

## 11. SQLite 临时文件保护

### 11.1 临时文件可能出现在哪里

| 触发 | 位置决定顺序 |
|---|---|
| 排序 / 临时表 / `VACUUM` / 大结果集溢出 | ① `sqlite3_config(SQLITE_CONFIG_TEMP_DIR)` 设置的目录；② 环境变量 `SQLITE_TMPDIR`；③ `TMPDIR` / `TEMP` / `TMP`；④ Windows `GetTempPath()`（通常 `%LOCALAPPDATA%\Temp` 或 `%TEMP%`） |
| 文件名 | `etilqs_<随机>`（Windows 上可能带 `.tmp` 后缀变体） |
| 内容 | 中间排序页、临时表页——**可能包含证据字段的明文或摘要** |

> 结论：**只设 `PRAGMA temp_store=MEMORY` 不足以保护所有临时文件**（它可能被 `SQLITE_TEMPFILE` 之外的路径绕过；且内存压力下 SQLite 可自行落盘）。

### 11.2 设计：L6 专用临时目录 + 进程级配置 + 启动验证

| 项 | 内容 |
|---|---|
| 目录 | `%PROGRAMDATA%\FosunAI\signer\tmp\`（**L6，新增**） |
| Owner | `S-1-5-18` |
| DACL | `SY:FA + SVC:M`（SVC 必须能建/删临时文件）；**无 BU、无 BA** |
| 配置时机 | `sqlite3_config(SQLITE_CONFIG_TEMP_DIR, L"...\\signer\\tmp")` **必须**在 `sqlite3_initialize()` 之前调用 |
| 失败处理 | `sqlite3_config` 返回非 0 → **拒绝启动**（退出码 **82**）；**禁止**回退默认临时目录 |
| 环境变量 | 即使 `SQLITE_TMPDIR`/`TEMP` 被恶意设置也无效（`sqlite3_config` 优先级更高）——但**必须实测确认**（W-51） |
| `temp_store` | 设 `MODE_FILE`（**不选 MEMORY**）：避免大排序 OOM；文件落在 L6 |
| 内存上限 | `PRAGMA cache_size`、`PRAGMA mmap_size` 设上限，防止内存压力导致不可控行为 |
| 启动验证 | 打开 DB 后：建临时表 → 强制溢出（低 `cache_size`）→ 检查 L6 出现 `etilqs_*` → `DROP` → 确认清理。失败 → 拒绝启动（82） |
| 崩溃残留 | 进程崩溃留下的 `etilqs_*` 由下次启动清理（SVC 在 L6 有 `DELETE`） |
| 每连接 vs 进程级 | `sqlite3_config` 是**进程级**（全局），优于逐连接 `PRAGMA`；`PRAGMA temp_store_directory` 已弃用，不使用 |

### 11.3 不声称

> 不声称"设置 L6 + `temp_store` 就能保证临时文件永不出现在别处"。真实保证是：**只要 `sqlite3_config` 成功且未被第三方代码重置**，临时文件落在 L6；若链接了别的 SQLite 副本或调用 `sqlite3_shutdown`/重新初始化，保证失效。因此启动自检（§11.2 末行）是必需的，不是可选项。

### 11.4 需要实机验证

| # | 项 |
|---|---|
| W-51 | `sqlite3_config(TEMP_DIR)` 是否确实优先于 `SQLITE_TMPDIR`/`TEMP` |
| W-52 | 自检能否稳定触发溢出并在 L6 观察到文件 |
| W-53 | L6 的 `SVC:M` 是否足以创建/删除 `etilqs_*` |

---

## 12. 安装器逐对象状态判定

### 12.1 四态定义

| 态 | 判定条件 | 处置 |
|---|---|---|
| **ABSENT** | 路径不存在 | 用 `SECURITY_ATTRIBUTES` + 目标 SDDL **创建**（从诞生即正确） |
| **COMPLIANT** | 存在 + 类型正确 + 非 reparse + Owner=`SY` + DACL 结构等于期望集 E（§16） | **no-op**（不重写，避免无谓的 DACL 变更窗口） |
| **CONTAMINATED** | 存在 + 类型正确 + 非 reparse + （Owner≠SY **或** 存在 UNKNOWN ACE） | **隔离后重建**：改名到 `<name>.quarantine-<ts>` → 新建 → 记录事务日志。**不原地改**（原地改会保留原属主/原句柄语义） |
| **UNKNOWN** | 存在但不可判定：reparse point / 类型不符（该是目录却是文件）/ 无法读取安全信息 | **停止安装**，不删除、不改名，输出诊断并要求人工处置 |

### 12.2 判定顺序（每个对象独立执行）

```
for obj in [L1,L2,L3,L4,L5,L6,F1,F3,F4]:
    st = probe(obj)
    match st:
      ABSENT        -> create_with_sd(obj)          # 失败 → 转 CONTAMINATED 分支
      COMPLIANT     -> log(noop)
      CONTAMINATED  -> quarantine(obj); create_with_sd(obj)
      UNKNOWN       -> abort_install(E_OBJ_UNKNOWN)  # 不自动处理
```

| 规则 | 内容 |
|---|---|
| 不直接覆盖未知对象 | 未知可能是"用户数据"或"攻击者布局"，覆盖会破坏证据、也可能掩盖攻击 |
| `ERROR_ALREADY_EXISTS` | `CreateDirectory`/`CreateFile` 返回该错误时**必须**转入 `probe` 重判为 COMPLIANT/CONTAMINATED/UNKNOWN，**不得忽略返回值**（A-P2-04 的直接修复点） |
| 创建即正确 | 用 `SECURITY_ATTRIBUTES.lpSecurityDescriptor` 传目标 SD，不使用"先建默认 ACL 再改" |
| 顺序 | L1 → L2 → L3 → L4 → L5 → L6 → F1/F3/F4；父先于子 |
| 每对象独立 | 不存在"整套 M-A / 整套 M-B"的全局模式；**逐个对象判定**（修复 A-P2-04 的"混合状态未定义"） |

### 12.3 四种安装场景的入口

| 场景 | 特征 | 处理 |
|---|---|---|
| 全新安装 | 全部 ABSENT | 逐对象创建 |
| 完整旧版升级 | 全部 COMPLIANT 或 CONTAMINATED（V3.9 的旧 ACL） | 逐对象重建 |
| 部分升级（上次中断） | 混合态 | 逐对象判定——**这正是 V3.9 缺失的分支** |
| 未知状态 | 事务日志链断裂 / 对象 UNKNOWN | **停止**，进入 §13.5 隔离态 |

---

## 13. 安全回滚与中断恢复

### 13.1 回滚源：版本化安全策略，不是现场 SDDL

| 项 | 内容 |
|---|---|
| 回滚源 | 安装包内嵌的 **`policy-<version>.json`**（含每对象的目标 SDDL、掩码、期望 ACE 集），带 **SHA256 + Authenticode** |
| 现场 SDDL 的用途 | **仅作证据**：写入 `install-log\observed-<ts>.sddl`，供事后分析；**永不作为回滚目标** |
| 为什么 | 现场 SDDL 在 T11 场景下就是攻击者的 ACL；用它会"把攻击者装回去"（A-P2-04 根因） |
| 版本校验 | 回滚前校验 `policy` 的 SHA256 与安装包清单一致；不一致 → 不执行回滚，进入隔离态 |

### 13.2 安装事务日志（完整性保护）

| 项 | 内容 |
|---|---|
| 位置 | `%PROGRAMDATA%\FosunAI\install-log\install-<ts>.jsonl` |
| 目录 ACL | `SY:FA` **仅此一条**（安装器 SYSTEM 写；SVC、BU、BA 均无权限） |
| 记录 | 每条：`{seq, obj, from_state, to_state, action, result, ts, prev_hash}`；`prev_hash = sha256(前一条原始字节)` |
| 完整性 | 哈希链；最后一条为 `{result:"COMMIT"}` 或 `{result:"ABORT"}` |
| 校验 | 恢复入口先验证哈希链与首条 `genesis`；断裂 → 拒绝自动恢复（E_LOG_CHAIN） |
| 局限 | 安装器本身是 SYSTEM，**它能改写整条链**。本机制的作用是"防止非 SYSTEM 主体伪造历史"，不是"防 SYSTEM"。**必须明示**，不得声称不可篡改 |

### 13.3 中断状态与服务状态

| 阶段 | 服务状态 | 是否允许启动 |
|---|---|---|
| 安装开始 | 强制 `STOPPED`；写 `maintenance.flag` | — |
| 任一步骤进行中 | `STOPPED` | **否**：服务启动首步检查 `maintenance.flag`，存在即退出码 **83** |
| 全部对象 COMPLIANT + 日志 `COMMIT` | 允许启动 | 是 |
| 日志 `ABORT` 且回滚成功 | 允许启动（回到旧版本状态） | 是，但 `readiness` 标 `install_aborted` |
| 回滚失败 | 进入 §13.5 隔离态 | **否** |

### 13.4 回滚执行顺序

| 步 | 动作 |
|---|---|
| 1 | 停服务（已停） |
| 2 | 校验 `policy` 版本与哈希 |
| 3 | 逆序（F4/F3/F1 → L6 → L5 → L4 → L3 → L2 → L1）对**已改动的对象**重放 `policy` 中的目标 SDDL（不是现场值） |
| 4 | 各对象结构复核；任一失败 → 隔离态 |
| 5 | 写 `ABORT` + 回滚结果；删 `maintenance.flag` |

> 注意：回滚是"**恢复到 policy 定义的合规态**"，不是"恢复到安装前状态"。安装前状态可能是污染态，不应恢复。

### 13.5 隔离态（恢复失败后的终态）

| 项 | 内容 |
|---|---|
| 触发 | 回滚失败 / 日志链断裂 / 对象 UNKNOWN 且需继续 |
| 动作 | 对所有 `FosunAI` 子树设 **deny-all**：`O:SYG:SYD:PAI(A;;FA;;;SY)`（仅 SYSTEM） |
| 服务 | 禁止启动（退出码 83） |
| 退出条件 | 管理员人工处置后运行 `installer --repair`（重跑逐对象判定）；**不得**由服务自动解除 |
| 记录 | `install-log` 追加 `ISOLATED` 记录 |

### 13.6 普通用户能否趁中断抢占

| 窗口 | 是否可利用 |
|---|---|
| 安装前（L1 未建） | ✅ 可预建 → 被 §12 判为 CONTAMINATED → 隔离重建 |
| L1 已建、L2 未建 | L1 已为 `SY:FA + BA:FA + SVC:RX`，**BU 无任何权限** → 用户无法在 L1 下建 `signer\` ✅ |
| 安装中断后 | L1 仍受保护 → 同样不可建 ✅ |
| 例外 | 管理员（T9） |

---

## 14. auth.db 可用性与拒绝服务防护

### 14.1 问题分析

| 概念 | 说明 |
|---|---|
| 读取 | `FILE_READ_DATA` — 允许读内容 |
| 遍历 | `FILE_TRAVERSE` — 允许穿过目录 |
| 打开句柄 | 任何具备"读/遍历"的主体都能 `CreateFileW` 成功 |
| 共享模式 | 由**打开者**指定。攻击者用 `dwShareMode = 0`（独占）即可阻止他人再打开 |

> 因此 **`BU:RX` 本身就足以造成持久 DoS**：任意本地用户对 `authdb\` 或 `auth.db` 做一次独占打开，服务 S6/S8 的 `CreateFileW` 就返回 `ERROR_SHARING_VIOLATION`，服务启动失败；用户只要保持句柄不关，故障可持续。

### 14.2 处置：**取消普通用户对认证库的一切直接访问**

| 对象 | V3.9 | V4.0 | 理由 |
|---|---|---|---|
| L3 `authdb\` | `BU:RX`、`BA:RX` | **无 BU、无 BA**（仅 SY + SVC） | 消灭独占打开 DoS；同时消灭"任何用户可读全部证据" |
| F1 `auth.db` | `BU:RX`、`BA:RX` | **无 BU、无 BA** | 同上 |
| L1 `FosunAI\` | `BU:RX` | **无 BU**（仅 SY + BA + SVC:RX） | 用户不再需要定位数据库 |

### 14.3 应用如何拿到证据：经 signer 受控查询

| 项 | 内容 |
|---|---|
| 接口 | 复用 V3.7 的 `resolve(decision_id)` IPC；新增只读 `query` 子集（按 `decision_id`/`session_id` 取证据摘要） |
| 鉴权 | 客户端 SID 白名单（应用账户）+ 请求绑定 `decision_id` + 每次请求 `nonce` |
| 限流 | 每 SID 每 10s ≤ 20 次；并发 ≤ 4；超限返回 `throttled`（**不影响**已建立的会话） |
| 数据面 | 只返回判定与摘要字段，**不返回原始 `response_body`** |
| 管理员诊断 | 需 `SeTakeOwnershipPrivilege` 夺属主后读 → 留审计痕迹；或经同一 IPC 以 SYSTEM 身份查询 |

### 14.4 启动失败与占用处理（不得无限重试）

| 情形 | 行为 |
|---|---|
| `ERROR_SHARING_VIOLATION` | 重试 **3 次**、间隔 2s/4s/8s；仍失败 → 退出码 **73**，事件日志写明"对象被占用"（区分于 ACL 失败 74） |
| `ERROR_ACCESS_DENIED` | **不重试**，退出码 **74**（ACL/属主问题，重试无意义） |
| 其他 | 退出码 73，记录 `GetLastError` |
| 禁止 | 无限重试、忙等、在被占用时降级为"以读写打开" |

### 14.5 日志路径的可用性

| 项 | 内容 |
|---|---|
| L4 `logs\` | 保留 `BU:RX`（只读诊断价值）；`SVC:M`、`BA:RX` |
| 日志被独占 | 打开日志失败**不得**导致服务退出；降级为"仅事件日志 + 内存计数"，`health=degraded` |
| 理由 | 日志不是认证路径，不能让它成为 DoS 放大器 |

---

## 15. A/B 证据架构继承（不得弱化）

| # | 已通过设计 | V4.0 处置 |
|---|---|---|
| 1 | ProgramData 根唯一来自 `SHGetKnownFolderPath` | **原样保留** |
| 2 | 相对组件固定，不接受外部覆盖 | **原样保留** |
| 3 | 组件边界比较（非前缀） | **原样保留** |
| 4 | UNC/设备/短名/尾点拒绝 | **原样保留** |
| 5 | 祖先目录逐级检查 | **原样保留**，并扩到 L6 |
| 6 | 基于句柄的安全描述符校验 | **原样保留**；S12 拆分为 a/b |
| 7 | A/B 证据物理隔离 | **原样保留** |
| 8 | signer 端 V-1…V-7 | **原样保留** |
| 9 | 历史 `real_llm` 仍标 transport unverified | **原样保留** |
| 10 | `real_llm_authenticated` 只由 signer 证据支持 | **原样保留** |
| 11 | R-26（DDL 拆 seal）继续 OPEN | **原样保留，未弱化** |
| 12 | R-17/R-19/R-21/R-22/R-23/R-24/R-25 不自动关闭 | **原样保留** |

> 唯一与证据链相关的新增是 `ai_backup_seal`（§10.3），它**不参与** verdict 判定，只服务备份可检测性。

---

## 16. 权限矩阵与掩码复算

### 16.1 位表（独立列出，供逐位核对）

| 位 | 值 | 文件语义 | 目录语义 |
|---|---|---|---|
| `0x1` | 1 | READ_DATA | LIST_DIRECTORY |
| `0x2` | 2 | WRITE_DATA | ADD_FILE |
| `0x4` | 4 | APPEND_DATA | ADD_SUBDIRECTORY |
| `0x8` | 8 | READ_EA | 同 |
| `0x10` | 16 | WRITE_EA | 同 |
| `0x20` | 32 | EXECUTE | TRAVERSE |
| `0x40` | 64 | —（忽略） | **DELETE_CHILD** |
| `0x80` | 128 | READ_ATTRIBUTES | 同 |
| `0x100` | 256 | WRITE_ATTRIBUTES | 同 |
| `0x10000` | 65536 | **DELETE（自身）** | 同 |
| `0x20000` | 131072 | READ_CONTROL | 同 |
| `0x40000` | 262144 | WRITE_DAC | 同 |
| `0x80000` | 524288 | WRITE_OWNER | 同 |
| `0x100000` | 1048576 | SYNCHRONIZE | 同 |

通用：`GENERIC_READ=0x120089`、`GENERIC_WRITE=0x120116`、`GENERIC_EXECUTE=0x1200A0`、`ALL=0x1F01FF`。

### 16.2 本次用到的四个掩码（逐位）

| 掩码 | 拆解 | 含 | 不含 |
|---|---|---|---|
| `0x1200A9` | `0x100000 + 0x20000 + 0xA9(0x80+0x20+0x08+0x01)` | 读/列/遍历/读属性/读EA/RC/S | DELETE、DC、WDAC、WOWNER、写 |
| `0x1301BF` | `0x100000 + 0x20000 + 0x10000 + 0x1BF(0x100+0x80+0x20+0x10+0x08+0x04+0x02+0x01)` | **Modify**：读写/建/删自身/遍历 | **无 DC**、无 WDAC、无 WOWNER |
| `0x12019F` | `0x100000 + 0x20000 + 0x19F(0x100+0x80+0x10+0x08+0x04+0x02+0x01)` | 写/建/读 | DELETE、DC、WDAC、**无 X** |
| **`0x1201BF`**（新） | `0x1301BF − 0x10000` = `0x100000+0x20000+0x1BF` | 写/建/读/**遍历** | **DELETE、DC、WDAC、WOWNER** |

> **为什么 L5 用 `0x1201BF` 而不是 V3.9 的 `0x12019F`**：`0x12019F` 缺 `FILE_TRAVERSE(0x20)`，SVC 只能靠 `SeChangeNotifyPrivilege`（Bypass traverse checking）穿过该目录——依赖一个可被策略关闭的特权不稳健。`0x1201BF` 显式授予遍历，语义更明确。
> 二者都**不含 `DELETE`**，因此"不能删备份"的结论不变；**但两者都含 `WRITE_DATA`，所以仍能就地覆盖** —— 这正是 A-P2-02 的真实修法在 §10（封存时把文件 DACL 改为 `SVC:R`），而不是继续在目录掩码上做文章。

### 16.3 目标 ACL 表（V4.0）

| 对象 | 类型 | Owner | ACE 列表（SID:mask, 继承） |
|---|---|---|---|
| L1 `FosunAI\` | 目录 | `SY` | `SY:FA(OI,CI)` `BA:FA(OI,CI)` `SVC:0x1200A9(OI,CI)` |
| L2 `signer\` | 目录 | `SY` | `SY:FA(OI,CI)` `SVC:0x1301BF(OI,CI)` |
| L3 `authdb\` | 目录 | `SY` | `SY:FA(OI,CI)` `SVC:0x1301BF(OI,CI)` |
| L4 `logs\` | 目录 | `SY` | `SY:FA(OI,CI)` `SVC:0x1301BF(OI,CI)` `BA:0x1200A9(OI,CI)` |
| L5 `backup\` | 目录 | `SY` | `SY:FA(OI,CI)` `SVC:0x1201BF(OI,CI)` `BA:0x1200A9(OI,CI)` |
| L6 `signer\tmp\` | 目录 | `SY` | `SY:FA(OI,CI)` `SVC:0x1301BF(OI,CI)` |
| F1 `auth.db` | 文件 | `SY` | `SY:FA` `SVC:0x1301BF`（OI/CI = 0） |
| F3 `key-<id>.dpapi` / `keyring.json` | 文件 | `SY` | `SY:FA` `SVC:0x1301BF` |
| F4 `escrow\key-<id>.machine.dpapi` | 文件 | `SY` | `SY:FA`（**仅此一条**） |
| F6 已封存备份 `auth-<ts>.db` | 文件 | `SY` | `SY:FA` `SVC:0x1200A9` `BA:0x1200A9` |
| F7 `install-log\*.jsonl` | 文件 | `SY` | `SY:FA`（**仅此一条**） |
| F8 日志 `logs\*.log` | 文件 | 创建者 | 继承 L4；服务打开时显式 `share=READ|WRITE|DELETE` |

**去掉的权限（相对 V3.9）**

| 对象 | V3.9 | V4.0 | 影响 |
|---|---|---|---|
| L1 `BU:RX` | 有 | **删** | 用户不能再遍历 `FosunAI`；应用改走 IPC |
| L3 `BU:RX` / `BA:RX` | 有 | **删** | 用户/管理员不能直读 `auth.db`；消灭 DoS 与"全量证据可读" |
| L2 `BA` | 无 | 无（保持） | — |
| L4 `BU:RX` | 有 | **删**（保留 `BA:RX`） | 日志仅管理员可读；日志故障不致命 |

### 16.4 SDDL（V4.0 全量）

```
【L1】O:SYG:SYD:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;0x1200A9;;;SVC)
【L2】O:SYG:SYD:PAI(A;OICI;FA;;;SY)(A;OICI;0x1301BF;;;SVC)
【L3】O:SYG:SYD:PAI(A;OICI;FA;;;SY)(A;OICI;0x1301BF;;;SVC)
【L4】O:SYG:SYD:PAI(A;OICI;FA;;;SY)(A;OICI;0x1301BF;;;SVC)(A;OICI;0x1200A9;;;BA)
【L5】O:SYG:SYD:PAI(A;OICI;FA;;;SY)(A;OICI;0x1201BF;;;SVC)(A;OICI;0x1200A9;;;BA)
【L6】O:SYG:SYD:PAI(A;OICI;FA;;;SY)(A;OICI;0x1301BF;;;SVC)
【F1】O:SYG:SYD:PAI(A;;FA;;;SY)(A;;0x1301BF;;;SVC)
【F3】O:SYG:SYD:PAI(A;;FA;;;SY)(A;;0x1301BF;;;SVC)
【F4】O:SYG:SYD:PAI(A;;FA;;;SY)
【F6】O:SYG:SYD:PAI(A;;FA;;;SY)(A;;0x1200A9;;;SVC)(A;;0x1200A9;;;BA)
【F7】O:SYG:SYD:PAI(A;;FA;;;SY)
```

### 16.5 icacls 等价（文档，不执行；`${SVC}` 防解析歧义）

```powershell
${SVC} = "*S-1-5-21-<域>-<RID>"
${B}   = "${env:ProgramData}\FosunAI"
icacls "${B}"           /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" "${SVC}:(OI)(CI)RX" /setowner "*S-1-5-18"
icacls "${B}\signer"    /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "${SVC}:(OI)(CI)M"  /setowner "*S-1-5-18"
icacls "${B}\signer\tmp" /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "${SVC}:(OI)(CI)M" /setowner "*S-1-5-18"
icacls "${B}\authdb"    /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "${SVC}:(OI)(CI)M"  /setowner "*S-1-5-18"
icacls "${B}\logs"      /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "${SVC}:(OI)(CI)M"  "*S-1-5-32-544:(OI)(CI)RX" /setowner "*S-1-5-18"
icacls "${B}\backup"    /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "${SVC}:(OI)(CI)(RD,WD,AD,REA,WEA,X,RA,WA,RC,S)" "*S-1-5-32-544:(OI)(CI)RX" /setowner "*S-1-5-18"
```

> `RD,WD,AD,REA,WEA,X,RA,WA,RC,S` = `0x1+0x2+0x4+0x8+0x10+0x20+0x80+0x100+0x20000+0x100000` = **`0x1201BF`** ✅（含 `X`，无 `DE`/`DC`）。
> `icacls` 的 `/inheritance:r`+`/grant:r` **仍然不是 DACL 清理**（V3.9 §6.5 结论保留）；落地以整份替换为准。

### 16.6 对象计数（可复算）

| 库 | 表 | 触发器 | 视图 |
|---|---|---|---|
| `auth.db` | 8 | 22 | 5 |
| `trading.db` | 4 | 9 | 2 |
| **合计** | **12** | **31** | **7** |

```
表    ：V3.7 10 + V3.8 1 + V4.0 1（ai_backup_seal）           = 12
触发器：V3.7 23 + V3.8 净新增 6 + V4.0 新增 2（backup_seal）  = 31
视图  ：V3.7 6 + V3.8 1                                       = 7
auth.db 触发器 = 31 − 9（trading.db）                         = 22
```

| 项 | 本文件内联 | 历史内联 | 合计 |
|---|---|---|---|
| 表 | **1** | 11 | 12 |
| 触发器 | **2** | 29（含 2 重定义） | 31 |
| 视图 | **0** | 7 | 7 |

---

## 17. 新增反例与正例

> 编号续 V3.9 与第十轮审计之后：CE 从 **CE-161** 起，PS 从 **PS-77** 起。判定只有 PASS / FAIL / UNCERTAIN。

### 17.1 新增反例 CE-161 ～ CE-185（25 条）

| ID | 前提 | 动作 | 预期 | V4.0 行为 | 依据 | 判定 |
|---|---|---|---|---|---|---|
| CE-161 | 运维 | 口令 change（知旧口令） | 密钥仍可用 | change 与密钥解耦 | §4.2 | PASS |
| CE-162 | 管理员 | reset（若选 A 架构） | 拒启动 | 退出码 75，禁止静默换 key | §5.3 | PASS |
| CE-163 | 管理员 | reset（B/B′ 架构） | 密钥可用 | 密钥与口令无关 | §5.1 | PASS |
| CE-164 | 实现者 | 让安装器接收密钥明文 | 无此入口 | 设计无该路径 | §7.2 | PASS |
| CE-165 | 本地任意进程 | 连控制管道要求 rotate | 拒 | `ImpersonateNamedPipeClient` 非 SY 即断 | §7.3 | PASS |
| CE-166 | 攻击者 | 重放旧 rotate 命令 | 拒 | 一次性 nonce + seq | §7.3 | PASS |
| CE-167 | 部署 | SVC 调 `LoadUserProfile` | 不需要 | 本架构不调用 | §8.1 | PASS |
| CE-168 | 实现者 | 用同句柄复核检测路径重定向 | 应失败 | 已明确"不用于此" | §9.1 | PASS |
| CE-169 | T9 | 路径被重定向 | 停服 | S12a 重开路径比对不一致 → 81 | §9.2 | PASS |
| CE-170 | SVC 被控 | 覆盖已封存备份 | 拒 | 封存后 `SVC:0x1200A9`（无写） | §10.2/§16.3 | PASS |
| CE-171 | SVC | 覆盖未封存 `.part` | 允许（不可用于恢复） | 恢复只认 `ai_backup_seal` | §10.2 | PASS |
| CE-172 | T2 | 改 `.sha256` 试图伪造 | 无效 | 不作为可信来源 | §10.2 | PASS |
| CE-173 | T2 | 诱导临时文件落 `%TEMP%` | 拒 | `sqlite3_config(TEMP_DIR)` 指向 L6 | §11.2 | PASS（待 W-51） |
| CE-174 | 环境 | `sqlite3_config` 失败 | 拒启动 | 退出码 82，不回退 | §11.2 | PASS |
| CE-175 | 负载 | `temp_store=MEMORY` OOM | 不 OOM | 选 `MODE_FILE` + 上限 | §11.2 | PASS |
| CE-176 | T2 | 预建 L1 并自授 `FA` | 隔离重建 | CONTAMINATED → quarantine + 新建 | §12.1 | PASS |
| CE-177 | T2 | 预建 L1 为 junction | 停止安装 | UNKNOWN → abort | §12.1 | PASS |
| CE-178 | 运维 | 回滚用现场 SDDL | 应禁用 | 回滚源改为 `policy-<ver>` | §13.1 | PASS |
| CE-179 | 攻击者 | 删/改 `policy` 文件 | 拒 | 回滚前校验 SHA256+签名 | §13.1 | PASS |
| CE-180 | 混合态 | L1 新 / L2 旧 | 不启动 | 逐对象判定，未全 COMPLIANT 即 83 | §12.2/§13.3 | PASS |
| CE-181 | T2 | 独占打开 `auth.db` | 拒 | 无 BU/BA ACE → `ERROR_ACCESS_DENIED` | §14.2 | PASS |
| CE-182 | T2 | 独占打开 `authdb\` 目录 | 拒 | 同上 | §14.2 | PASS |
| CE-183 | T2 | 直接读 `auth.db` 取 MAC | 拒 | 同上；改经 IPC | §14.3 | PASS |
| CE-184 | 安装器 | `CreateDirectory` 返回 `ERROR_ALREADY_EXISTS` 被忽略 | 应重判 | 必须转 probe 重判 | §12.2 | PASS |
| CE-185 | T2 | 篡改 `install-log` 以掩盖污染 | 拒自动恢复 | 哈希链断裂 → E_LOG_CHAIN | §13.2 | PASS（SYSTEM 仍可改，见 §13.2 局限） |

**统计**：PASS 25 / FAIL 0 / UNCERTAIN 0。

### 17.2 新增正例 PS-77 ～ PS-88（12 条）

| ID | 场景 | 预期 | V4.0 行为 | 依据 | 判定 |
|---|---|---|---|---|---|
| PS-77 | 全新安装：6 目录 + 3 文件全部 ABSENT → 创建 | 通过 | 带 SD 创建 | §12.2 | PASS |
| PS-78 | 升级：全部 COMPLIANT → no-op | 通过 | 不重写 DACL | §12.1 | PASS |
| PS-79 | 单对象 CONTAMINATED → 隔离重建 | 通过 | 改名 + 新建 | §12.1 | PASS |
| PS-80 | 首次启动：服务自生成 key + 写 keyring（含 `protection`） | 通过 | 无外部输入 | §6.2 | PASS |
| PS-81 | 定期轮换：新 key active、旧 key retiring → retired | 通过 | K1–K4 | §6.3 | PASS |
| PS-82 | 备份：写 → 校验 → SYSTEM 封存 → auth.db 记录 | 通过 | B1–B4 | §10.1 | PASS |
| PS-83 | 恢复：`.sha256` 与 `ai_backup_seal` 一致才允许 | 通过 | B5 | §10.1 | PASS |
| PS-84 | 临时目录自检：观察到 L6 出现 `etilqs_*` 并清理 | 通过 | §11.2 末行 | §11.2 | PASS（待 W-52） |
| PS-85 | 应用经 IPC 只读查询成功（无 `auth.db` 直接访问） | 通过 | `query` 子集 | §14.3 | PASS |
| PS-86 | 启动失败分类：占用→73 / ACL→74 / 多实例→80 / 临时目录→82 / 维护中→83 | 通过 | 退出码表 | §14.4 | PASS |
| PS-87 | 计数复算：12 / 31 / 7 与算式一致 | 通过 | §16.6 | §16.6 | PASS |
| PS-88 | V3.9 十三条已通过设计全部保留 | 通过 | §15 | §15 | PASS |

**统计**：PASS 12 / FAIL 0 / UNCERTAIN 0。

**累计**：反例 160 + 25 = **185**；正例 76 + 12 = **88**。

---

## 18. V3.9 问题逐项修复映射

### 18.1 三条 P1

| 审计 ID | V3.9 位置 | 根因 | V4.0 修复 | 修复章节 | 状态 |
|---|---|---|---|---|---|
| A-P1-01 | §10.3 标题「change 语义」、行 766 `NetUserSetInfo`、§10.9「master key 被重新加密」 | 把管理员 reset 当作用户变更，并据此断言重加密 | ① 删除"调用即重加密"的断言；② F-1…F-5 全部标 `[WINDOWS_TEST]`；③ **换架构**，使密钥与口令解耦（reset 不再是灾难）；④ 给出 change / reset 两套独立状态机 | §4 / §5 / §3.4 | `[DESIGN]` |
| A-P1-02 | §10 轮换步 2/步 5 | 明文需跨进程但无通道 | 改为**服务自主生成并处理密钥**，安装器只发无秘密指令；秘密**不跨进程** | §7 | `[DESIGN]` |
| A-P1-03 | §10.2/§10.7 | 依赖 `LoadUserProfile` 但可行性未证 | 改为 DPAPI LocalMachine / 纯 ACL，**不需要 profile**；`LoadUserProfile` 仅作客观记录并标 `[WINDOWS_TEST]` | §3.4 / §8 | `[DESIGN]` |

### 18.2 五条 P2

| 审计 ID | V3.9 位置 | 根因 | V4.0 修复 | 修复章节 | 状态 |
|---|---|---|---|---|---|
| A-P2-01 | §5.7 行 241–242 | 用同一句柄证明路径解析 | 拆 S12a（**重新按路径打开**比对 file id + DACL）/ S12b（同句柄只查 DACL）；明确 S12b 不检测重定向 | §9 | `[DESIGN]` |
| A-P2-02 | §6.2 L5 `0x12019F` | 目录掩码含写权 → 可就地覆盖 | ① 改为 **封存式**：写完后由 SYSTEM 把**文件**DACL 改为 `SVC:R`；② `auth.db` 记 `ai_backup_seal`（SYSTEM 重算 sha256）；③ 明示 `.sha256` 非签名 | §10 / §16.2 | `[DESIGN]` |
| A-P2-03 | §12 | 只覆盖 journal/wal/shm | 新增 **L6** 临时目录 + `sqlite3_config(TEMP_DIR)` + 启动自检 + fail-closed | §11 / §16.3 | `[DESIGN]` + `[SQLITE_PROBE]` |
| A-P2-04 | §7.2 步 1/5、§7.5 | 回滚源是现场 SDDL；混合态未定义 | ① 回滚源改为**版本化 `policy`**；② 逐对象四态判定；③ `ERROR_ALREADY_EXISTS` 必须重判；④ 新增隔离态 | §12 / §13 | `[DESIGN]` |
| A-P2-05 | §6.3 L3 `BU:RX` | 读权 = 可独占打开 = DoS | 删除 BU/BA 对 L1/L3 的一切权限；应用改经 signer 受控查询；启动失败分类 + 有限重试 | §14 / §16.3 | `[DESIGN]` |

### 18.3 对审计 P3 的处置（不强制，但一并处理）

| 审计 ID | 处置 |
|---|---|
| A-P3-01（大小写前提） | §9.2 保留忽略大小写比较；补注"依赖目录大小写不敏感；若置 case-sensitive 须改严格比较" → 记入 R-40 说明（不新开风险号，附于 §19 备注） |
| A-P3-02（Group/SACL/运行期文件） | §16.3 明确校验范围仅 L1–L6/F1/F3/F4/F6/F7；F8 不校验（继承 + 动态） |
| A-P3-03（`/setowner "*SID"`） | 保留 `icacls` 仅作"等价展示"；落地用 `SetNamedSecurityInfoW`；W-34 保留 |
| A-P3-04（对象清单/顺序） | §12.2 明确 L1→L2→L3→L4→L5→L6→F1/F3/F4 顺序，并把 F 类纳入 |
| A-P3-05（SQLite 句柄绑定） | §9.1 明确"不能绑定 SQLite 句柄"，保证来自路径常量 + guard handle + S12a |
| A-P3-06（HMAC 字段） | 本轮不重述；**R-41** 记录"64 字段清单需在后续版本内联以便独立复算" |

---

## 19. 风险状态总表

| ID | 风险 | 状态 | 关闭条件 | 证据 | 责任组件 | 阻断实施 |
|---|---|---|---|---|---|---|
| R-17 | SQLite 全局序号语义 | **OPEN** | S 探针全通过 | V3.7 §12 | 存储层 | 否（但影响锚定） |
| R-19 | Windows ACL 未实机验证 | **OPEN** | W-1…W-28 + W-43…W-50 通过 | V3.9 §20 | 安装器/服务 | 是（实施前必须做） |
| R-21 | 会话最终态 DB 不强制 | **OPEN** | 有 DB 级约束或接受 P2 | V3.9 §16 | signer | 否 |
| R-22 | auth.db 路径权威 | **TEST-PENDING** | W-13…W-16 通过 | V3.9 §5、V4.0 §9 | 服务 | 是 |
| R-23 | signer 二进制完整性 | **OPEN**（T9 下） | 不可关闭（T9 残留） | V3.9 §15 | 构建/安装 | 否 |
| R-24 | 父目录 ACL | **TEST-PENDING** | W-17/W-18 通过 | V4.0 §16 | 安装器 | 是 |
| R-25 | 跨决策证据绑定 | **DESIGN-CLOSED**（设计层） | 静态审查 + 实机 | V3.9 §8（V-1…V-7） | signer | 否 |
| R-26 | trading.db DDL 拆 seal | **OPEN** | 不可关闭（SQLite 能力边界） | V3.8 §10.1、V3.9 §11 | 应用+存储 | 否（已披露） |
| R-27 | DPAPI 与口令 reset 耦合 | **MITIGATED**（架构解耦）+ **OPEN**（A 架构下仍存在） | 不采用 A 架构即闭环；采用则 OPEN | §3.4 / §6.4 | 部署选型 | 否 |
| R-28 | 管理员 `SeRestorePrivilege` 替换 | **OPEN** | 不可关闭 | §9.5 | — | 否 |
| R-29 | 安装器中断致半套 ACL | **MITIGATED** | W-35/W-48 通过 → 可转 DESIGN-CLOSED | §12 / §13 | 安装器 | 是（未验证前） |
| **R-30** | 口令 reset 致 master key 不可恢复 | **MITIGATED**（B/B′ 下不适用）/ **OPEN**（A 下） | 不用 A 即闭环 | §5.1 | 部署选型 | 否 |
| **R-31** | 安装器与服务间秘密传递 | **DESIGN-CLOSED** | 无秘密传递路径（设计验证：§7.2） | §7 | 安装器+服务 | 否 |
| **R-32** | 服务 profile 加载不可行 | **MITIGATED** | 不依赖 profile | §8.1 | 服务 | 否 |
| **R-33** | S12 句柄身份复核误判 | **MITIGATED** | S12 拆分 + W-32 通过 → DESIGN-CLOSED | §9 | 服务 | 是（措辞未改前） |
| **R-34** | 备份被服务账户就地改写 | **MITIGATED**（封存后只读）/ **OPEN**（B4 之前） | W-48/W-49 通过 | §10 | 安装器(SYSTEM) | 否 |
| **R-35** | SQLite 临时文件落入不受保护目录 | **TEST-PENDING** | W-51…W-53 + S-16/S-17 通过 | §11 | 服务 | 是（未验证前） |
| **R-36** | 安装器用污染 SDDL 回滚 | **DESIGN-CLOSED** | 回滚源改为 `policy`（无现场值路径） | §13.1 | 安装器 | 否 |
| **R-37** | 普通用户独占打开致 DoS | **MITIGATED** | 已删 BU/BA 直读；IPC 限流 | §14 | ACL + signer | 否 |
| **R-38** | DPAPI LocalMachine 保护边界被误解为"仅 signer" | **MITIGATED**（文档强制披露） | §3.4 边界表 + `health.protection` 可见 | §3.4 | 文档+服务 | 否 |
| **R-39** | SYSTEM 封存组件的攻击面 | **OPEN** | 需给出最小化实现与审计 | §10.1 B4 | 安装器/计划任务 | 否 |
| **R-40** | 忽略大小写比较依赖 case-insensitive 目录 | **OPEN**（低） | 补前提说明 + 目录属性检查 | §9.2 | 服务 | 否 |
| **R-41** | HMAC 64 字段清单未内联，审计不可独立复算 | **OPEN** | 后续版本内联清单 | §18.3 | 文档 | 否 |

**统计**：共 **23** 个活跃风险号（R-17…R-41，未列 R-18/R-20 等历史已并入项）。状态分布：`OPEN` **9**、`MITIGATED` **8**、`TEST-PENDING` **3**、`DESIGN-CLOSED` **3**。

> 只有"设计完整且不依赖未验证假设"才给 `DESIGN-CLOSED`：R-25（V-1…V-7 逻辑闭合）、R-31（无传递路径）、R-36（无现场值路径）、R-41 之外的文档项。R-29/R-33 需实机后才能升级为 `DESIGN-CLOSED`。

---

## 20. Windows 实机验证计划

| # | 项 | 依赖 | 失败后果 |
|---|---|---|---|
| W-40 | 非管理员账户调 `LoadUserProfile` 的实际返回码 | §8.2 | 仅影响 A 架构结论，本架构不受影响 |
| W-41 | SCM 登录是否加载 profile | §8.2 | 同上 |
| W-42 | 口令 change vs reset 对 master key 的实际影响 | §4.1 F-3/F-4 | 影响 A 架构说明；本架构不受影响 |
| W-43 | 非管理员能否解 `CRYPTPROTECT_LOCAL_MACHINE` blob | §3.4 | 失败 → 降级 B′（acl_only） |
| W-44 | 机器 master key 目录对普通账户的可读性 | §3.4 | 影响边界描述 |
| W-45 | MS 软件 KSP 是否支持 HMAC（C 架构） | §3.3 | C 永久不选 |
| W-46 | 服务自生成密钥全流程（无外部输入） | §7.2 | 阻断 |
| W-47 | 口令轮换与密钥轮换互不影响 | §6.3 | 阻断 |
| W-48 | `SetNamedSecurityInfoW` 对**文件**改 DACL 后 SVC 是否确实只读 | §10.1 | 阻断（备份仍可改） |
| W-49 | SYSTEM 封存 + `auth.db` 记录幂等重跑 | §10.4 | 备份不可用 |
| W-50 | 无 BU/BA ACE 时应用经 IPC 取数正常 | §14.3 | 阻断（应用拿不到证据） |
| W-51 | `sqlite3_config(TEMP_DIR)` 是否优先于 `SQLITE_TMPDIR`/`TEMP` | §11.2 | 阻断 |
| W-52 | 启动自检能触发溢出并在 L6 观察 | §11.2 | 阻断 |
| W-53 | L6 `SVC:M` 足以建/删 `etilqs_*` | §11.2 | 阻断 |
| W-54 | `CreateDirectory` + `SECURITY_ATTRIBUTES` 在 `ERROR_ALREADY_EXISTS` 下的行为 | §12.2 | 安装器误判 |
| W-55 | 隔离重建（rename → create）在占用/长路径下的行为 | §12.1 | 安装器 |
| W-56 | 单实例互斥体 `Global\` 在多会话下的行为 | §8.3 | 多实例 |
| W-57 | 命名管道 `PIPE_REJECT_REMOTE_CLIENTS` + SDDL 的实际效果 | §7.3 | 仅影响可选路径 |
| W-58 | `GetNamedPipeClientProcessId` + 映像签名校验可行性 | §7.3 | 同上 |

> V3.9 的 W-13…W-28 继续有效；V3.8 的 W-1…W-12 继续有效。

---

## 21. SQLite 隔离探针计划（本轮未执行）

| # | 项 | 关联 |
|---|---|---|
| S-16 | `temp_store=MODE_FILE` + `TEMP_DIR` 下临时文件位置 | R-35 |
| S-17 | `sqlite3_config` 后再设 `SQLITE_TMPDIR` 是否被忽略 | R-35 |
| S-18 | `ai_backup_seal` 的 UPDATE/DELETE 是否被触发器拒绝 | §10.3 |
| S-19 | 幂等重跑 B4（同名 `backup_name`）是否被主键拒绝 | §10.4 |
| S-20 | `DROP TRIGGER` 后 seal 失效（复现 R-26） | R-26 |

> S-1…S-15 继续有效。**本轮未运行任何 SQL / 探针。**

---

## 22. 残余风险与实施前置条件

### 22.1 不可消除的残余风险

| # | 残余 | 为什么不可消除 |
|---|---|---|
| 1 | T9 管理员 `SeRestorePrivilege` + POSIX rename | 内核允许持权者绕过 ACL |
| 2 | T9 管理员可解 machine-scope blob（读机器 master key 或夺属主） | 密钥必须对某主体可用 |
| 3 | SQLite 写者可 `DROP TRIGGER`（R-26） | SQLite 无禁止 DDL 能力 |
| 4 | B4 之前 SVC 可篡改 `.part` | 同一主体既写又校验 |
| 5 | SYSTEM 封存组件本身可被 SYSTEM 级攻击者滥用 | 与 T9 同域 |
| 6 | S12a 的检测延迟窗口（≤30s） | 周期检测的固有性质 |
| 7 | 内存中密钥无法证明被清零 | 换页/转储/崩溃 dump |

### 22.2 实施前置条件（缺一不可）

| # | 条件 | 当前状态 |
|---|---|---|
| 1 | 本方案通过第十一轮独立审计 | ❌ 未开始 |
| 2 | W-43/W-46/W-48/W-50/W-51 全部通过 | ❌ 未执行 |
| 3 | W-13…W-28（V3.9 遗留）通过 | ❌ 未执行 |
| 4 | 隔离探针 S-1…S-20 通过 | ❌ 未执行 |
| 5 | `policy-<version>` 与安装包签名链打通 | ❌ 未设计实现 |
| 6 | 服务单实例 + 退出码表落地 | ❌ 未实现 |
| 7 | 备份封存组件（SYSTEM）最小化实现与审计 | ❌ 未实现 |

### 22.3 明确未做

```
未执行 SQL；未创建/打开 SQLite 库；未运行 S-1…S-20；
未执行 ACL 命令；未创建/修改/启动/停止服务；
未调用 LoadUserProfile / DPAPI / 任何凭据 API；
未生成、读取或传递任何密钥或口令；
未修改源码/配置/测试/依赖/数据/日志；
未访问 VPS / 生产 API / 真实账户；未发起模型调用；未触碰 Git；
未创建 V4.1；未开展第十一轮审计。
```

---

## 附录 A 章节行号索引

| 章 | 起始行 |
|---|---|
| 1. 版本说明与 V3.9 基线 | 25 |
| 2. 第十轮审计问题总览 | 51 |
| 3. 凭据与 DPAPI 架构比较 | 70 |
| 4. 正常密码变更状态机 | 134 |
| 5. 管理员密码重置状态机 | 167 |
| 6. 密钥轮换与恢复状态机 | 200 |
| 7. 安装器与 signer 服务秘密处理 | 269 |
| 8. 服务账户配置文件生命周期 | 325 |
| 9. S12 文件身份验证修订 | 362 |
| 10. 备份写入、验证与封存 | 417 |
| 11. SQLite 临时文件保护 | 475 |
| 12. 安装器逐对象状态判定 | 517 |
| 13. 安全回滚与中断恢复 | 559 |
| 14. auth.db 可用性与拒绝服务防护 | 624 |
| 15. A/B 证据架构继承（不得弱化） | 674 |
| 16. 权限矩阵与掩码复算 | 695 |
| 17. 新增反例与正例 | 811 |
| 18. V3.9 问题逐项修复映射 | 870 |
| 19. 风险状态总表 | 903 |
| 20. Windows 实机验证计划 | 937 |
| 21. SQLite 隔离探针计划（本轮未执行） | 965 |
| 22. 残余风险与实施前置条件 | 979 |
| 附录 A 章节行号索引 | 1019 |

---

> 本文件 SHA256（本行之前全部字节，可用 `head -n -1 <本文件> | sha256sum` 复算）：`4c50d0dac2402eb1953221e60f3ef0a09b685469a3b0059ed6a90006664253c9`
