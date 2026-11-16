# V3.8 修订方案（设计稿 · 不实施）

| 项 | 值 |
|---|---|
| 版本 | **v3.8**（2026-11-11 16:25） |
| 取代 | `US_REAL_LLM_EVIDENCE_V3_7_SCHEME.md`（`a4bc2964…`，**不覆盖、不修改**） |
| 依据 | V3.7 第八轮独立只读审计（FAIL：2×P0 / 3×P1 / 5×P2 / 2×P3） |
| 性质 | 文档修订。未执行 SQL / 探针 / 迁移 / ACL 修改 / 服务操作 |
| 状态 | **V3.8 待第九轮独立审计**；未获批；未获实施授权；S0 未启动 |

**状态标签约定（全文严格遵守，不混用）**

| 标签 | 含义 |
|---|---|
| `[DESIGN]` | 本文档已完成设计描述，逻辑自洽 |
| `[STATIC]` | 已通过静态审查（本轮无第三方审查，故本文件不使用此标签） |
| `[WIN-VERIFY]` | 必须在 Windows 实机验证后才成立的结论 |
| `[SQLITE-PROBE]` | 必须在隔离 SQLite 探针中验证 |
| `[OPEN]` | 已知未解决 |

---

## 1. 修订摘要

第八轮审计的判决是：**方向对，但"可执行规格"缺失，且两个 P0 都能让双库隔离失效**。

V3.8 只解决一件事：**把"意图"变成"可由操作系统强制执行、可被第三方复算的规格"**。

| # | 变化 | 关闭 |
|---|---|---|
| 1 | auth.db 路径改为**服务内编译期常量**，列出 6 类禁用覆盖入口，定义 8 步句柄级验证状态机 + 目录/文件 guard handle 防 TOCTOU | P0-1 / R-22 |
| 2 | 目录链从 `%PROGRAMDATA%\FosunAI\` 起全部纳入，给出 **4 条 SDDL 字符串 + 4 组 icacls 命令 + 逐 ACE 语义表** | P0-2 / R-24 / P1-1 |
| 3 | 目录校验改为**读 DACL 结构比对**（GetNamedSecurityInfo + GetAce），不解析 icacls 文本 | P1-1 |
| 4 | 跨决策校验**移到 signer 服务端**：返回 verdict 前独立验证 decision_id/session/MAC/key_id/seq | P1-2 / R-25 |
| 5 | 服务账户三方案比较并选定"本地专用账户 + LSA Secret" | P1-3 |
| 6 | A 证据表补 4 个触发器 + seal 表（含自身 2 个触发器） | P2-1（部分，见 R-26） |
| 7 | allowlist 规范化规则 12 条 + 热更新策略 | P2-3 |
| 8 | 补热日志、升级迁移、备份恢复、二进制完整性（R-23） | P2-2/4/5、R-23 |
| 9 | CE-32 从"可配置"改为**失败路径** | CE-32 矛盾 |
| 10 | §26 交付状态列全 8 项未决风险 | P3-1 |

---

## 2. 基线核验

| 文件 | 字节 | 行 | SHA256 | 判定 |
|---|---|---|---|---|
| `US_REAL_LLM_EVIDENCE_V3_7_SCHEME.md` | 67,039 | 1,313 | `a4bc2964fbf91ef590f6ef1554b544e5c1910ce50f80d7aeb34b859c6c2efc46` | ✅ |
| 自指纹（去末行） | — | — | `d4063095099016051dd982c22bd7c2e1a7db852bac6eabf4863fd62200065711` | ✅ |
| V3.6 | 69,149 | 1,393 | `34c02c370f59a5d99fe4f6eb2a8f1ddd705672e55f93a68b2121db80f8ce223a` | 未变 |
| V3.5 | 62,703 | 1,200 | `3f308b17a43731d6d3ce5d0a741b2c17a34549e9cc03c9c95e13f2eb599f9534` | 未变 |
| 原修复计划 | 27,586 | 534 | `3d569f16754e389c77534cbbbfc3e294ad5d6bba67946fb7bb9f3f7fd629610a` | 未变 |
| `src/ config/ tests/ data/ logs/` | — | — | 15:40 后 0 个文件变化 | ✅ |

第七轮、第八轮审计报告均**未落盘**，本文引用其结论时仅依据对话文本，未声称读取文件。

---

## 3. V3.7 问题追踪

| 编号 | 级别 | 原问题 | V3.8 处置 | 章节 | 状态 |
|---|---|---|---|---|---|
| P0-1 / R-22 | P0 | auth.db 路径可由应用配置决定 | 编译期常量 + 6 类禁用入口 + 8 步验证 | §5 | `[DESIGN]` |
| P0-2 / R-24 | P0 | 父目录 `FosunAI\` 未纳入 ACL，可整体替换 authdb | 目录链 4 层全定义 + 禁止 DELETE_CHILD | §6 | `[DESIGN]` |
| P1-1 | P1 | ACL 只有散文断言，无 SDDL/命令 | 4 条 SDDL + icacls + 逐 ACE 表 + 结构比对脚本 | §6/§7 | `[DESIGN]` |
| P1-2 / R-25 | P1 | 跨决策复用仅应用侧检查 | signer 服务端 7 项校验 | §8 | `[DESIGN]` |
| P1-3 | P1 | svc 账户凭据管理未定义 | 三方案比较 + 选定 + 生命周期 | §9 | `[DESIGN]` |
| P2-1 | P2 | A 证据表无触发器 | 证据表 4 触发器 + seal 表 2 触发器 + 缺口视图 | §10 | `[DESIGN]`（DML 层） |
| P2-2 | P2 | 热日志/崩溃残留只读打开策略缺失 | 启动预检 + 只读降级规则 | §11 | `[DESIGN]` |
| P2-3 | P2 | allowlist 规范化未定义 | 12 条规范 + 热更新策略 | §12 | `[DESIGN]` |
| P2-4 | P2 | 升级迁移窗口未定义 | 停服/临时库/原子替换/回滚 | §13 | `[DESIGN]` |
| P2-5 | P2 | 备份位置与权限未定义 | 备份目录 ACL + 恢复授权 | §13.3 | `[DESIGN]` |
| R-23 | OPEN | 二进制完整性 | Authenticode + 证书 pin + DLL 策略 + 反降级 | §14 | `[DESIGN]`（验证属 `[WIN-VERIFY]`） |
| R-21 | P2 | 会话最终态 DB 不强制 | 保留 P2；signer 侧强校验 + 检测视图 | §15 | `[DESIGN]` |
| R-17 | OPEN | 全局序号语义 | 不变，仍 OPEN | §15.2 | `[OPEN]` |
| P3-1 | P3 | §26 未列全风险 | 列全 8 项（含自查新增 R-26） | §19 | ✅ |
| P3-2 | P3 | CE-32 自认攻击成功 | 改为失败路径 + 新增反例 | §17 | ✅ |
| **自查-1** | **P0** | 本文件初稿的 seal 表**无 UPDATE/DELETE 触发器**，`DELETE FROM ai_app_evidence_seal` 可清空封印，随后 `INSERT OR REPLACE` 即可绕过守卫完成证据替换 | 补 `trg_app_seal_no_update` / `trg_app_seal_no_delete`；并把 DDL 层不可防的事实写成 R-26 | §10 / §10.1 | 已修正（`[DESIGN]`，待审计） |
| **R-26** | **OPEN** | 写者对 `trading.db` 拥有 DDL 能力，`DROP TRIGGER` 不触发触发器 | 不声称解决；仅记录边界并以 signer 侧重算 HMAC 兜底 | §10.1 | `[OPEN]` |


---

## 4. 威胁模型与账户能力

### 4.1 主体与能力

| 主体 | SID 示例 | 读 signer 目录 | 读 auth.db | 写 auth.db | 改 DACL | 装服务 | 读 DPAPI 密钥 |
|---|---|---|---|---|---|---|---|
| 普通用户（应用） | `S-1-5-21-…-1001` | ❌ | ✅（RX） | ❌ | ❌ | ❌ | ❌ |
| 安装者（管理员） | `S-1-5-32-544` | ✅（RX） | ✅ | ❌（运行期） | ✅ | ✅ | ❌ |
| signer 服务账户 | `<SVC_SID>` | ✅ | ✅ | ✅ | ❌ | ❌ | ✅（自己的） |
| SYSTEM | `S-1-5-18` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅（可注入） |
| 内核/驱动 | — | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |

### 4.2 T1–T9 与新增 T10

| 威胁 | 读密钥 | 调签名 | 改元数据 | 改 allowlist | 写 auth.db | 伪造 committed | 签任意内容 | 造 B 等级 | 改历史 | 可发现 | 最高声明 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| T1 公开 API | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | — | 应用记录 |
| T2 任意 DML | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | 受限 | ✅ | 未认证数据 |
| T3 改 schema | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ | 锚定 | 同上 |
| T4 改文件 | ❌ | ❌ | ❌ | ❌ | ✅（若有文件权） | ✅ | ❌ | ⚠️ 需连密钥 | ✅ | 锚定 | 同上 |
| T5 应用被控 | ✅(app) | ✅ | ❌ | ❌ | ❌ | ❌ | ✅(A) | ❌ | 局部 | A 不可检测 | 仅 app_record |
| T5′ 可调 IPC | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ 但仅真实会话 | ❌ | 服务审计 | upstream_session |
| T6 控制 svc 账户 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 仅锚定 | 不可声明 |
| T7 锚定失效 | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | 可掩盖 | 锚点链 | 不可声明 |
| T8 替换服务/配置 | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ACL 审计 | 同上 |
| T9 管理员 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 仅锚定 | 不可声明 |
| **T10 目录级替换** | ❌ | ❌ | ❌ | ❌ | ⚠️ 见 §6 | ⚠️ | ❌ | ⚠️ | ✅ | 锚定 | **V3.8 必须封死** |

> T10 是第八轮审计新指出的：即使叶子目录无写权，若父目录可删子目录，仍可整体换库。§6 专门处理。

---

## 5. 路径权威（P0-1 / R-22）

### 5.1 常量定义（编译期，不可配置）

```c
// signer 服务源码内常量，无配置读取
#define FOSUN_AUTHDB_DIR   L"\\\\?\\C:\\ProgramData\\FosunAI\\authdb"
#define FOSUN_AUTHDB_FILE  L"\\\\?\\C:\\ProgramData\\FosunAI\\authdb\\auth.db"
#define FOSUN_SIGNER_DIR   L"\\\\?\\C:\\ProgramData\\FosunAI\\signer"
// 说明：C:\ProgramData 的卷标可能不是 C:，因此启动时用
// SHGetKnownFolderPath(FOLDERID_ProgramData) 取得真实根，再拼接固定子路径，
// 但仍不接受任何外部输入覆盖子路径。
```

| 规则 | 说明 |
|---|---|
| 根 | `FOLDERID_ProgramData`（系统 API 取，非环境变量） |
| 子路径 | 常量 `FosunAI\authdb\auth.db` |
| 前缀 | `\\?\`（禁用 Win32 路径规范化，防 `..` 与设备名） |

### 5.2 禁止的覆盖入口（逐个封死）

| 入口 | 处理 |
|---|---|
| 配置文件（yaml/json/ini） | 服务**不读取**任何包含路径的配置；启动仅读 allowlist 与密钥元数据 |
| 环境变量 | 不使用 `GetEnvironmentVariable` 决定路径；显式 `SetEnvironmentVariable` 无关 |
| CLI 参数 | 服务启动参数忽略（服务由 SCM 启动，argv 仅服务名） |
| 注册表 | 不读 `HKLM\SOFTWARE\FosunAI` 下的任何路径键；如存在则忽略并告警 |
| 当前工作目录 | 全路径为绝对路径，不拼接 CWD |
| 外部连接字符串 | 不通过 SQLite URI 参数传入路径；`sqlite3_open_v2` 的 filename 为常量 |

**验证**：安装脚本与启动自检都要枚举上述入口，确认无一处影响最终路径（`[WIN-VERIFY]`）。

### 5.3 启动验证状态机（8 步）

```
S0 取 ProgramData 根（SHGetKnownFolderPath）
S1 拼接常量路径 → 规范化（GetFullPathNameW 仅用于日志，不用于判定）
S2 打开目录 guard handle（FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT，
      share = READ|WRITE，**不含 DELETE**）
S3 校验目录非 reparse point、属主与 DACL（§6.4 结构比对）
S4 打开 auth.db guard handle（FILE_FLAG_OPEN_REPARSE_POINT，
      share = READ|WRITE，**不含 DELETE**）
S5 校验文件非 reparse、nNumberOfLinks==1（非硬链接）、属主与 DACL
S6 GetFinalPathNameByHandleW 取 NT 路径，与常量逐字符比较（CompareStringOrdinal，忽略大小写）
S7 通过后放开 SQLite 打开（sqlite3_open_v2，路径仍为常量）
S8 保持 S2/S4 两个 guard handle 到进程退出
```

| 检查 | 失败行为 |
|---|---|
| 目录/文件不存在 | **不创建**，服务拒绝启动（退出码 79）；创建由安装程序完成 |
| 路径含 reparse point | 拒绝（`FILE_ATTRIBUTE_REPARSE_POINT`） |
| 硬链接（Links>1） | 拒绝 |
| 最终路径 ≠ 常量 | 拒绝 |
| DACL 与期望不符 | 拒绝 |
| 属主不符 | 拒绝 |

### 5.4 抗 TOCTOU

| 手段 | 作用 |
|---|---|
| guard handle 不带 `FILE_SHARE_DELETE` | 服务运行期间该目录/文件**不能被删除或改名**（任何人，包括管理员，需先关闭句柄或强制卸载） |
| `FILE_FLAG_OPEN_REPARSE_POINT` | 打开的是链接本身而非目标，便于识别替换 |
| 句柄验证（而非路径验证） | 判定基于已打开对象，不是字符串 |

**残余**：T9（管理员）可 `SeBackupPrivilege` + 离线替换；`[WIN-VERIFY]` 需实测 guard handle 对 rename 的阻断效果（W-7）。

### 5.5 明确不声称

> 路径硬编码 ≠ 文件系统安全。它只消除"配置覆盖"这一入口；真正的隔离由 §6 的 ACL 承担。

---

## 6. 目录链 ACL（P0-2 / R-24 / P1-1）

### 6.1 目录链

```
C:\ProgramData\                      ← 系统目录，Users 有创建子目录权
└─ FosunAI\                          ← 【L1】必须收紧：Users 仅 RX
   ├─ signer\                        ← 【L2】密钥 + keyring + allowlist + 配置；Users 无 ACE
   ├─ authdb\                        ← 【L3】auth.db；Users RX
   ├─ logs\                          ← 【L4】服务日志；Users RX
   └─ backup\                        ← 【L5】备份；Users 无 ACE（新增）
```

> V3.7 只定义了 L2–L4，**漏了 L1 与 L5**。L1 若继承 `%PROGRAMDATA%` 的 `Users:(AD)`（append data/add subdirectory），用户可对 L1 下的子目录做 `DELETE_CHILD` → 删除或改名 `authdb` → 放入自己的同名目录 → **T10 成立**。

### 6.2 SDDL（4 条 + 备份目录）

```
【L1】FosunAI\
O:SYG:SYD:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;0x1200a9;;;BU)

【L2】signer\
O:SYG:SYD:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;SVC)(A;OICI;0x1200a9;;;BA)

【L3】authdb\
O:SYG:SYD:PAI(A;OICI;FA;;;SY)(A;OICI;0x1301bf;;;SVC)(A;OICI;0x1200a9;;;BU)(A;OICI;0x1200a9;;;BA)

【L4】logs\
O:SYG:SYD:PAI(A;OICI;FA;;;SY)(A;OICI;0x1301bf;;;SVC)(A;OICI;0x1200a9;;;BU)(A;OICI;0x1200a9;;;BA)

【L5】backup\
O:SYG:SYD:PAI(A;OICI;FA;;;SY)(A;OICI;0x1301bf;;;SVC)(A;OICI;0x1200a9;;;BA)
```

> `SVC` 为占位，安装时替换为实际服务账户 SID（`*S-1-5-21-…`）。
> `0x1200a9` = `FILE_GENERIC_READ | FILE_GENERIC_EXECUTE`（目录上即"列目录 + 遍历 + 读属性"，**不含** `FILE_ADD_FILE`/`FILE_ADD_SUBDIRECTORY`/`FILE_DELETE_CHILD`/`DELETE`/`WRITE_DAC`/`WRITE_OWNER`）。
> `0x1301bf` = `Modify`（含 `DELETE`、`FILE_DELETE_CHILD`，仅授予 SVC）。
> `D:PAI` = DACL 受保护（**切断来自 `%PROGRAMDATA%` 的继承**）+ ACE 可自动继承至子对象。

### 6.3 逐 ACE 语义表

| 目录 | SID | 掩码 | 继承 | 允许的操作 | 禁止的操作 | 理由 |
|---|---|---|---|---|---|---|
| L1 | `SY` | `FA` | OI+CI | 全部 | — | 系统兜底 |
| L1 | `BA` | `FA` | OI+CI | 全部 | — | 安装/修复 |
| L1 | `BU` | `0x1200a9` | OI+CI | 列出、遍历 | **建子目录、删子目录、改 DACL、改属主** | 应用只需定位 |
| L2 | `SY` | `FA` | OI+CI | 全部 | — | — |
| L2 | `SVC` | `FA` | OI+CI | 读写密钥文件 | — | 服务独占 |
| L2 | `BA` | `0x1200a9` | OI+CI | 只读 | **写、删** | 防止管理员误改 |
| L3 | `SY` | `FA` | OI+CI | 全部 | — | — |
| L3 | `SVC` | `0x1301bf` | OI+CI | 读写、删子项 | 改 DACL | 服务需要建 journal |
| L3 | `BU` | `0x1200a9` | OI+CI | 读 auth.db | **写、删、改名、建文件** | 应用只读 |
| L3 | `BA` | `0x1200a9` | OI+CI | 只读 | 写、删 | 运行期管理员不写 |
| L4 | `SVC` | `0x1301bf` | OI+CI | 写日志 | — | — |
| L4 | `BU`/`BA` | `0x1200a9` | OI+CI | 读日志 | 写、删 | — |
| L5 | `SVC` | `0x1301bf` | OI+CI | 写备份 | — | — |
| L5 | `BA` | `0x1200a9` | OI+CI | 只读 | 写、删 | 备份不可被随意替换 |

### 6.4 安装命令（`[WIN-VERIFY]`，本轮不执行）

```powershell
$SVC = "*S-1-5-21-<dom>-<svc-rid>"     # 安装时查询，不硬编码到文档

# L1
icacls "C:\ProgramData\FosunAI" /inheritance:r `
  /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" "*S-1-5-32-545:(OI)(CI)RX" `
  /setowner "*S-1-5-18"

# L2（无 BU ACE → 用户无任何权限）
icacls "C:\ProgramData\FosunAI\signer" /inheritance:r `
  /grant:r "*S-1-5-18:(OI)(CI)F" "$SVC:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)RX" `
  /setowner "*S-1-5-18"

# L3
icacls "C:\ProgramData\FosunAI\authdb" /inheritance:r `
  /grant:r "*S-1-5-18:(OI)(CI)F" "$SVC:(OI)(CI)M" `
           "*S-1-5-32-545:(OI)(CI)RX" "*S-1-5-32-544:(OI)(CI)RX" `
  /setowner "*S-1-5-18"

# L4
icacls "C:\ProgramData\FosunAI\logs" /inheritance:r `
  /grant:r "*S-1-5-18:(OI)(CI)F" "$SVC:(OI)(CI)M" `
           "*S-1-5-32-545:(OI)(CI)RX" "*S-1-5-32-544:(OI)(CI)RX" `
  /setowner "*S-1-5-18"

# L5
icacls "C:\ProgramData\FosunAI\backup" /inheritance:r `
  /grant:r "*S-1-5-18:(OI)(CI)F" "$SVC:(OI)(CI)M" "*S-1-5-32-544:(OI)(CI)RX" `
  /setowner "*S-1-5-18"
```

> `M` = Modify；`RX` = Read&Execute；`F` = Full。

### 6.5 校验方式（不依赖文本输出）

```text
1. GetNamedSecurityInfoW(path, SE_FILE_OBJECT, OWNER_SECURITY_INFORMATION |
                         DACL_SECURITY_INFORMATION | GROUP_SECURITY_INFORMATION)
2. 检查 SecurityDescriptorControl 含 SE_DACL_PROTECTED（等价 D:P*）
3. 取 DACL → GetAclInformation(AclSizeInformation) → 逐 GetAce
4. 对每个 ACE：
     ACE_HEADER.AceType ∈ {ACCESS_ALLOWED_ACE_TYPE, ACCESS_DENIED_ACE_TYPE}
     取 AceSize 与 Mask（ACCESS_ALLOWED_ACE.Header.AceFlags/Mask）
     SidStart 取 SID → ConvertSidToStringSidW → 与期望 SID 比较
     与期望集合做**完全匹配**（数量、类型、掩码、继承标志、SID 全等）
5. 任何一项不符 → 拒绝启动；不解析 icacls 文本、不做字符串匹配
```

| 断言 | 期望 |
|---|---|
| DACL 受保护 | `SE_DACL_PROTECTED` 置位 |
| L1 BU 掩码 | 恰为 `0x1200a9`，且 `AceFlags` 含 `OI|CI` |
| L1 BU 是否含 `DELETE_CHILD` | **否**（`0x1200a9 & 0x40 == 0`） |
| L1 BU 是否含 `WRITE_DAC` | **否**（`& 0x40000 == 0`） |
| L1 BU 是否含 `WRITE_OWNER` | **否**（`& 0x80000 == 0`） |
| 是否有 `CREATOR OWNER` ACE | **无**（D:PAI 已切断继承） |
| 属主 | `S-1-5-18` |

> 以上断言实现为安装器与服务的共用校验模块；**本轮未执行，`[WIN-VERIFY]`**。

---

## 7. 目录删除/替换分析（T10）

| 攻击 | 需要的权限 | V3.8 是否阻断 | 依据 |
|---|---|---|---|
| 删除 `authdb\` 目录 | 对 L1 有 `FILE_DELETE_CHILD` | ✅ 阻断 | L1 BU 掩码 `0x1200a9` 不含 `0x40` |
| 改名 `authdb\` | 同上 + `DELETE` | ✅ | 同上 |
| 删除 `auth.db` | 对 L3 有 `FILE_DELETE_CHILD` | ✅ | L3 BU 不含 `0x40` |
| 改名 `auth.db` | `DELETE` on file | ✅ | 文件继承 L3 的 RX |
| 新建同名目录 | 对 L1 有 `FILE_ADD_SUBDIRECTORY` | ✅ | `0x1200a9` 不含 `0x4` |
| 新建 `auth.db-wal` | 对 L3 有 `FILE_ADD_FILE` | ✅ | `0x1200a9` 不含 `0x2` |
| 修改 L3 DACL | `WRITE_DAC` | ✅ | 应用无，属主为 SYSTEM |
| 取得属主 | `SeTakeOwnershipPrivilege` | ⚠️ 管理员可 | T9，设计接受 |
| 服务运行中替换（rename） | `DELETE` + `FILE_SHARE_DELETE` | ✅ | guard handle 不含 share-delete（`[WIN-VERIFY]` W-7） |
| 服务停止后替换 | 需上面的删除/新建权 | ✅ | ACL |
| 通过 junction 重定向 | 需在可写目录建 junction + 替换目标 | ✅ | `FILE_FLAG_OPEN_REPARSE_POINT` + `FILE_ATTRIBUTE_REPARSE_POINT` 检查 |

---

## 8. signer 服务端证据验证（P1-2 / R-25）

`resolve` 请求返回前，服务**独立**执行 7 项校验，任一失败即拒绝返回 B 等级：

| # | 校验 | 失败返回 |
|---|---|---|
| V-1 | `evidence.decision_id == request.decision_id` | `integrity_violation` |
| V-2 | `evidence.session_id` 存在且 `session.decision_id == request.decision_id` | 同上 |
| V-3 | `session.seq == evidence.seq` | 同上 |
| V-4 | `session.state == 'committed'` 且 `session.evidence_id == evidence.evidence_id` | `upstream_session_unverified` |
| V-5 | MAC 复算（服务持密钥）通过 | `integrity_violation`（不符）/ `unverified`（未知 key） |
| V-6 | `evidence.key_id` 与 `session.key_id` 一致，且 keyring 中该 key 的 `slot='signer'`、`owner_sid` 与当前服务身份一致 | `integrity_violation` |
| V-7 | `evidence.request_nonce` 与 `session.nonce` 一致；该 nonce 未被其他 session 使用 | 同上 |

```text
resolve(decision_id):
  row ← SELECT * FROM evidence WHERE decision_id=? ORDER BY seq DESC LIMIT 1
  if row is None: return {verdict:"unavailable"}
  s ← SELECT * FROM session WHERE session_id=row.session_id
  for check in [V1..V7]:
      if not check(row, s, request): return {verdict: fail_code}
  mac ← verify_hmac(row, keyring)
  if mac != ok: return {verdict:"integrity_violation" or "upstream_session_unverified"}
  anchor ← anchor_status(row.global_evidence_seq)
  return {verdict: anchor=="current" ? "upstream_session_authenticated"
                                     : "upstream_session_unverified"}
```

> 应用侧的 S-06 仍保留，但**只是双保险**；安全边界在服务端。

---

## 9. 服务账户与凭据（P1-3）

### 9.1 三方案比较

| 维度 | A 本地专用账户 `svc_fosun` + LSA Secret | B gMSA | C 虚拟服务账户 `NT SERVICE\FosunSigner` |
|---|---|---|---|
| 可用性（单机非域） | ✅ | ❌ 需域 | ✅ |
| 有用户配置文件（DPAPI 用户 scope） | ✅（需首次登录建 profile） | ✅ | ❌（虚拟账户无 profile） |
| 密码管理 | 安装器生成 32 字节随机，存 LSA Secret | AD 托管 | 无密码 |
| 普通用户可重置 | ❌（需管理员） | ❌ | — |
| DPAPI 用户 scope 可用 | ✅ | ✅ | ❌ → 只能 CNG/machine scope（machine scope 任意本地用户可解，**不可接受**） |
| 卸载/迁移 | 删账户 + 删 LSA Secret | 出域即失效 | 自动 |
| 结论 | **选定 A** | 不适用 | 备选（配 CNG 才可用） |

### 9.2 选定方案：A + DPAPI 用户 scope

| 项 | 设计 |
|---|---|
| 账户 | `.\svc_fosun`，`user` 类型，`PasswordNeverExpires=1`，`不能交互登录`（deny interactive logon） |
| 密码 | 安装器 `CryptGenRandom(32)` → base64 → **只写 LSA Secret** `FosunSignerPwd`（`LsaStorePrivateData`），不落文件 |
| 登录权限 | `SeServiceLogonRight` 仅授予该账户；`SeDenyInteractiveLogonRight` / `SeDenyRemoteInteractiveLogonRight` |
| Profile | 安装时以该账户运行一次 `CreateProfile`，使 DPAPI 用户 scope 可用 |
| 密钥位置 | `%PROGRAMDATA%\FosunAI\signer\key-<id>.dpapi`（DPAPI user scope + entropy `fosun-signer-v1`） |
| 轮换 | 服务内生成新 key → 写 keyring → 旧 key 保留只读 |
| 恢复 | 密码丢失 = 无法解密 = 历史不可认证（R-15） |
| 卸载 | 停服务 → 删密钥文件 → 删 LSA Secret → 删账户（保留 auth.db 供审计） |

**明确风险**：管理员可重置该账户密码并以其身份登录（T9），此时密钥可解 → B 等级失守。**不可声明"抗管理员"**。

---

## 10. A 证据保护（P2-1）

`trading.db` 新增 seal 表与 **6 个触发器**（证据表 4 + seal 表 2）：

```sql
CREATE TABLE ai_app_evidence_seal (
    decision_id TEXT NOT NULL,
    seq         INTEGER NOT NULL CHECK (seq >= 1),
    evidence_id TEXT NOT NULL CHECK (length(evidence_id) = 64),
    seal_hash   TEXT NOT NULL CHECK (length(seal_hash) = 64),
    PRIMARY KEY (decision_id, seq),
    UNIQUE (evidence_id)
);

CREATE TRIGGER trg_app_ev_no_update
BEFORE UPDATE ON ai_app_evidence
BEGIN SELECT RAISE(ABORT,'app evidence append-only'); END;

CREATE TRIGGER trg_app_ev_no_delete
BEFORE DELETE ON ai_app_evidence
BEGIN SELECT RAISE(ABORT,'app evidence append-only'); END;

CREATE TRIGGER trg_app_ev_seal_after_ins
AFTER INSERT ON ai_app_evidence
BEGIN
  INSERT INTO ai_app_evidence_seal(decision_id,seq,evidence_id,seal_hash)
  VALUES (NEW.decision_id,NEW.seq,NEW.evidence_id,NEW.evidence_mac);
END;

CREATE TRIGGER trg_app_ev_seal_guard_ins
BEFORE INSERT ON ai_app_evidence
WHEN EXISTS (SELECT 1 FROM ai_app_evidence_seal s
              WHERE (s.decision_id=NEW.decision_id AND s.seq=NEW.seq)
                 OR s.evidence_id=NEW.evidence_id)
BEGIN SELECT RAISE(ABORT,'app evidence slot sealed'); END;

CREATE TRIGGER trg_app_seal_no_update
BEFORE UPDATE ON ai_app_evidence_seal
BEGIN SELECT RAISE(ABORT,'app seal append-only'); END;

CREATE TRIGGER trg_app_seal_no_delete
BEFORE DELETE ON ai_app_evidence_seal
BEGIN SELECT RAISE(ABORT,'app seal append-only'); END;
```

| 路径 | 行为 |
|---|---|
| `INSERT OR REPLACE` | 冲突删除不触发 DELETE 触发器，但 BEFORE INSERT 的 seal 守卫拦住（`[SQLITE-PROBE]` S-5/S-6 验证） |
| UPSERT | 走 BEFORE UPDATE → 拒绝 |
| DELETE | 拒绝 |
| MAC 失败 | 降级为 `app_record_unverified`，**绝不会升级** |
| 证据被删（若绕过） | `v_app_evidence_gaps` 可见；`app_record_*` 不再可能 |

### 10.1 本机制的边界（**必须明示，不得夸大**）

| 事实 | 结论 |
|---|---|
| 写者（应用账户）对 `trading.db` 有 `INSERT`/`UPDATE`/`DELETE` 权限 | 触发器对 **DML** 是硬约束（`RAISE(ABORT)`） |
| 写者对同库拥有**结构修改能力**（`DROP TRIGGER` / `DROP TABLE` 不触发任何触发器） | SQLite **无法**阻止写者用 DDL 拆除本机制 → 状态 **`[OPEN]`**，记为 R-26 |
| 因此 seal 的真实价值 | **不**是"应用侧不可篡改"，而是"篡改必然留下结构缺口，且真伪最终由 signer 侧重算 HMAC 判定" |
| 最终边界 | `auth.db` 中的 `ai_transport_evidence`（signer 独占写）+ 锚定链；`trading.db` 的 seal 只是**降低静默替换的成功率**，不是认证来源 |

> R-26（**`[OPEN]`**）：A 证据的 DML 保护依赖触发器，DDL 保护不在 SQLite 能力范围内。缓解只能来自文件级（`trading.db` 属主 + 应用账户权限最小化）与 signer 侧重算校验；本轮**不声称**已解决。

---

## 11. 热日志与崩溃残留（P2-2）

| 场景 | 策略 |
|---|---|
| 服务启动时发现 `auth.db-journal` 存在 | 服务以**读写**打开并让 SQLite 自行回滚（服务有写权）；**绝不为了提高权限给应用写权** |
| 应用以 `mode=ro` 打开时遇 journal | 返回 `SQLITE_BUSY`/`SQLITE_READONLY_ROLLBACK`；应用**不得**重试为读写；界面显示"认证库不可用" |
| `auth.db-wal` / `-shm` | 不存在（`journal_mode=DELETE`），若被意外创建 → 服务启动告警并拒绝启动（防注入） |
| 目录中存在未知文件 | 记入 `logs\anomaly.log`，不删除 |

---

## 12. allowlist 规范化（P2-3）

| # | 规则 |
|---|---|
| N-1 | JSON 解析器必须拒绝**重复键**（不是后值覆盖） |
| N-2 | `host` 先做 IDN → A-label（`IdnToAscii`，`IDN_ALLOW_UNASSIGNED=0`） |
| N-3 | 统一小写（`LCMapStringEx` 序数小写，非区域相关） |
| N-4 | 去掉**唯一一个**尾点（`example.com.` → `example.com`）；多于一个尾点拒绝 |
| N-5 | `port` 必须显式；缺省拒绝（不默认 443） |
| N-6 | `path_prefix` 去尾斜杠后比较；必须以 `/` 开头 |
| N-7 | 拒绝任何控制字符（`< 0x20`）、前后空白、零宽字符（U+200B–U+200D、U+FEFF） |
| N-8 | 拒绝非 NFC 规范化形式 |
| N-9 | `scheme` 必须 `https` |
| N-10 | 比较规则：`host` 序数不区分大小写；`path` 序数区分大小写；`port` 整数相等 |
| N-11 | 热更新：**不接受**运行期文件变化；仅在服务重启时加载；文件 mtime/hash 变化记录告警但不生效 |
| N-12 | 文件与 DB 镜像不一致 → 服务拒绝启动（fail-closed），不"以文件为准"静默覆盖 |

> SQLite 的 `GLOB` 只做兜底，**不是**合法性验证（N-2/N-4 全是它做不到的）。

---

## 13. 升级、迁移、备份（P2-4 / P2-5）

### 13.1 升级

| 步 | 动作 | 权限 |
|---|---|---|
| 1 | 停止服务（`sc stop`） | 管理员 |
| 2 | 校验新包 Authenticode（§14） | 安装器 |
| 3 | 备份 auth.db 到 `backup\`（§13.3） | 安装器（以 SYSTEM 运行） |
| 4 | 在 `backup\tmp-<ts>\` 建临时库并迁移 | 安装器 |
| 5 | 校验行数/哈希/FK/完整性 | 安装器 |
| 6 | 原子替换：`MoveFileEx(..., MOVEFILE_REPLACE_EXISTING)`（同卷） | 安装器 |
| 7 | 重建 ACL（§6.4） | 安装器 |
| 8 | 启动服务，服务自检（§5.3） | SCM |

| 失败点 | 回滚 |
|---|---|
| 2 失败 | 中止，服务不启动 |
| 4–5 失败 | 删除临时库；原库未动 |
| 6 失败 | 原库仍在（MoveFileEx 原子）；报警 |
| 7 失败 | ACL 错误 → 服务启动失败（fail-closed），安装器回滚 ACL |

### 13.2 迁移（schema 变更）

沿用 V3.7 §17 的步骤，另加：迁移期间**服务必须停止**（否则两个写者）；迁移由安装器以 SYSTEM 执行；完成后恢复 ACL 再启动。

### 13.3 备份

| 项 | 设计 |
|---|---|
| 位置 | `%PROGRAMDATA%\FosunAI\backup\`（L5） |
| 命名 | `auth-<yyyyMMdd-HHmmss>.db` + `.sha256` |
| 权限 | SYSTEM F、SVC M、BA RX、**Users 无** |
| 完整性 | 备份后立即 `PRAGMA integrity_check` + 全表哈希写入同名 `.sha256` |
| 恢复授权 | 仅管理员显式命令；恢复前必须停服并再次校验 `.sha256` |
| 保留 | 最近 30 份 + 每月 1 份，超期归档到 `archive\`（同 ACL） |
| 清理 | 由服务按策略执行（服务有 M 权），不手工删 |

---

## 14. 二进制完整性（R-23）

| 层 | 措施 |
|---|---|
| 签名 | 所有交付二进制（EXE/DLL/MSI）Authenticode 签名，SHA-256 |
| 启动校验 | 服务启动时 `WinVerifyTrust(WINTRUST_ACTION_GENERIC_VERIFY_V2)` 校验自身 EXE + 同目录全部 DLL |
| 签名者 pin | 校验通过后再 `CryptQueryObject` 取签名证书，比对**编译期写入的证书公钥 SHA-256**；不匹配拒绝启动 |
| DLL 搜索 | `SetDefaultDllDirectories(LOAD_LIBRARY_SEARCH_SYSTEM32 \| LOAD_LIBRARY_SEARCH_APPLICATION_DIR)`；禁用 CWD 与 PATH 搜索；`SetDllDirectoryW(L"")` |
| side-loading | 应用目录内**不允许**存在未签名 DLL；启动时枚举目录，发现即拒 |
| 更新包 | MSI 必须签名；安装器校验后再安装；安装器自身也必须签名（鸡生蛋问题：由 Windows Installer 的服务信任链承担） |
| 反降级 | 安装包内 `min_build` 写入 `HKLM\SOFTWARE\FosunAI\MinBuild`（管理员写）；安装器拒绝低于该版本；服务启动也校验自身 ≥ min_build |
| 升级中断 | MSI 事务回滚；服务启动校验失败 → 拒绝启动并写事件日志 |

**四层区分（不得混为一谈）**：

| 层 | 本方案是否具备 |
|---|---|
| 文件存在签名 | ✅ 要求 |
| Windows 能验证签名 | ✅ 标准 API |
| **服务实际在启动时验证** | ✅ 设计（`[WIN-VERIFY]`） |
| **只允许授权发布版本启动** | ⚠️ 依赖 pin + min_build；管理员仍可替换（T9） |

→ R-23 在 T8 下 `[DESIGN]` 闭合；在 T9 下 `[OPEN]`。

---

## 15. R-21 与 R-17

### 15.1 R-21（会话最终态）

| 项 | 结论 |
|---|---|
| 谁能 INSERT | 仅 signer |
| 能否直插 `committed` | V3.7 允许 `signed\|committed`；V3.8 收紧为**只允许 `signed`**（`trg_session_born_created` 改为 `state='created'`，证据插入要求 `signed`，插入后置 `committed`） |
| 检测 | `v_session_gaps`（committed 无 evidence）+ 服务启动全表扫描 |
| 严重性 | **P2**（仅 signer 可写 + 可检测 + 不产生 verdict） |
| 残余 | "插入后未置 committed"仍靠代码；`[OPEN]` |

```sql
-- V3.8 收紧版
CREATE TRIGGER trg_session_born_created
BEFORE INSERT ON ai_sign_session
WHEN NEW.state <> 'created'
BEGIN SELECT RAISE(ABORT,'session must be born in created'); END;

CREATE TRIGGER trg_ev_session_ins
BEFORE INSERT ON ai_transport_evidence
WHEN NOT EXISTS (SELECT 1 FROM ai_sign_session s
                  WHERE s.session_id=NEW.session_id AND s.state='signed'
                    AND s.decision_id=NEW.decision_id)
BEGIN SELECT RAISE(ABORT,'evidence requires signed session'); END;
```

### 15.2 R-17（全局序号）

**保持 OPEN。** 本文档不对 SQLite 的 read-your-writes / 触发器子查询可见性做任何已验证声明；相关 15 项仍需 `[SQLITE-PROBE]`（见 §20）。

---

## 16. 结构与对象清单（V3.8）

| 库 | 表 | 触发器 | 视图 |
|---|---|---|---|
| `auth.db` | 7（allowlist / session / transition / seq_counter / evidence / seal / anchor） | 20 | 5 |
| `trading.db` | 4（decisions / app_evidence / **app_evidence_seal** / legacy_evidence） | **9**（原 3 + app_ev 4 + app_seal 2） | 2（v_decision_struct、**v_app_evidence_gaps**） |
| 合计 | **11** | **29** | **7** |

> 计数口径：`auth.db` 7 / 20 / 5 与 `trading.db` 原 3 个触发器均**继承 V3.7 §14–§15**，本文件**不重抄**；V3.8 正文内联的是 **1 表 + 8 触发器 + 1 视图**（8 = 新增 6 + 重定义 2）。上表是**合并后总量**，可复算：`10+1=11`、`23+4+2=29`、`6+1=7`。重定义的两个为 `trg_session_born_created`、`trg_ev_session_ins`，**不增加计数**。


```sql
CREATE VIEW v_app_evidence_gaps AS
SELECT e.decision_id, e.seq, e.evidence_id, 'missing_seal' AS defect
  FROM ai_app_evidence e
 WHERE NOT EXISTS (SELECT 1 FROM ai_app_evidence_seal s
                    WHERE s.decision_id=e.decision_id AND s.seq=e.seq);
```

（其余 DDL 与 V3.7 §14 相同，仅 `ai_sign_session` 的 CHECK 与两个触发器按 §15.1 收紧；**V3.7 原文不动**。）

---

## 17. 反例与正例（增量）

### 17.1 新增反例（27 条，CE-86…CE-112）

| # | 威胁 | 操作 | 预期 | 拦截层 | 状态 |
|---|---|---|---|---|---|
| CE-86 | T10 | 对 `FosunAI\` 建子目录 | 拒 | L1 ACL 无 `FILE_ADD_SUBDIRECTORY` | `[WIN-VERIFY]` |
| CE-87 | T10 | 删除 `authdb\` | 拒 | L1 无 `DELETE_CHILD` | `[WIN-VERIFY]` |
| CE-88 | T10 | 改名 `authdb\` | 拒 | 同上 + 无 `DELETE` | `[WIN-VERIFY]` |
| CE-89 | T10 | 删除 `auth.db` | 拒 | L3 无 `DELETE_CHILD` | `[WIN-VERIFY]` |
| CE-90 | T10 | 新建 `auth.db-wal` | 拒 | L3 无 `FILE_ADD_FILE` | `[WIN-VERIFY]` |
| CE-91 | T10 | 改 L3 DACL | 拒 | 无 `WRITE_DAC`，属主 SYSTEM | `[WIN-VERIFY]` |
| CE-92 | T10 | 服务运行中 rename `auth.db` | 拒 | guard handle 无 share-delete | `[WIN-VERIFY]` |
| CE-93 | T10 | 用 junction 把 `authdb` 指向别处 | 拒 | `OPEN_REPARSE_POINT` + 属性检查 | `[WIN-VERIFY]` |
| CE-94 | T10 | 硬链接替换 `auth.db` | 拒 | `nNumberOfLinks>1` | `[WIN-VERIFY]` |
| CE-95 | T10 | 改配置让服务打开伪库 | 拒 | §5.2 禁 6 入口 | `[DESIGN]` |
| CE-96 | T10 | 设 `FOSUN_AUTHDB` 环境变量 | 拒 | 不读环境变量 | `[DESIGN]` |
| CE-97 | T10 | 改注册表路径键 | 拒 | 不读注册表 | `[DESIGN]` |
| CE-98 | T10 | 在 CWD 放同名 auth.db | 拒 | 绝对路径，不拼 CWD | `[DESIGN]` |
| CE-99 | T5′ | 请求 verdict 时把 evidence 挂到别的 decision | 拒 | V-1/V-2 | `[DESIGN]` |
| CE-100 | T5′ | session.decision_id 与 evidence 不符 | 拒 | V-2 | `[DESIGN]` |
| CE-101 | T5′ | seq 不一致 | 拒 | V-3 | `[DESIGN]` |
| CE-102 | T5′ | 会话未 committed 却要 verdict | 降级 | V-4 | `[DESIGN]` |
| CE-103 | T5′ | key_id 与 session.key_id 不符 | 拒 | V-6 | `[DESIGN]` |
| CE-104 | T5′ | 复用同一 nonce 建两会话 | 拒 | `UNIQUE(nonce)` + V-7 | `[DESIGN]` |
| CE-105 | A | 删除 `ai_app_evidence` 行 | 拒 | `trg_app_ev_no_delete` | `[DESIGN]` |
| CE-106 | A | UPDATE A 证据 | 拒 | `trg_app_ev_no_update` | `[DESIGN]` |
| CE-107 | A | `INSERT OR REPLACE` A 证据 | 拒 | seal 守卫 | `[SQLITE-PROBE]` |
| CE-108 | 配置 | allowlist 重复 JSON 键 | 拒 | N-1 | `[DESIGN]` |
| CE-109 | 配置 | host 用 IDN 变体冒充 | 拒 | N-2/N-4 | `[DESIGN]` |
| CE-110 | 配置 | 运行期替换 allowlist 文件 | 不生效 | N-11 | `[DESIGN]` |
| CE-111 | 升级 | 安装未签名 MSI | 拒 | §14 | `[WIN-VERIFY]` |
| CE-112 | A | 先 `DELETE FROM ai_app_evidence_seal` 清封印，再 `INSERT OR REPLACE` 替换证据 | 拒 | `trg_app_seal_no_delete`（**自查-1 补入**） | `[SQLITE-PROBE]` S-13/S-15 |

**CE-32 修正**：V3.7 的 CE-32 把"改连接串指向伪库"记为"可配置（即成功）"。V3.8 改为 **CE-32′：预期=拒绝**，拦截层=§5.2 + §5.3，状态 `[DESIGN]`。原文的"可配置"结论作废。

### 17.2 新增正例（14 条，PS-36…PS-49）

| # | 场景 | 期望 | 状态 |
|---|---|---|---|
| PS-36 | L1–L5 ACL 全部按 §6.4 设置 | 服务启动通过 | `[WIN-VERIFY]` |
| PS-37 | 应用以 ro 打开 auth.db | 成功读取 | `[WIN-VERIFY]` |
| PS-38 | 应用尝试写 auth.db | `SQLITE_READONLY` | `[WIN-VERIFY]` |
| PS-39 | 服务以 guard handle 持有 auth.db | 管理员 rename 失败 | `[WIN-VERIFY]` |
| PS-40 | svc 账户登录并解密 DPAPI 密钥 | 成功 | `[WIN-VERIFY]` |
| PS-41 | 普通用户读取密钥文件 | 拒绝 | `[WIN-VERIFY]` |
| PS-42 | 签名正常的 EXE 启动 | 通过 WinVerifyTrust | `[WIN-VERIFY]` |
| PS-43 | 篡改一字节的 EXE 启动 | 拒绝 | `[WIN-VERIFY]` |
| PS-44 | 正常 allowlist（含 IDN 域名） | 规范化后匹配 | `[DESIGN]` |
| PS-45 | verdict 查询：evidence 属于该 decision | 返回 B 等级 | `[DESIGN]` |
| PS-46 | 备份 → 恢复（校验 .sha256 通过） | 数据一致 | `[WIN-VERIFY]` |
| PS-47 | 升级中断在临时库阶段 | 原库未动 | `[WIN-VERIFY]` |
| PS-48 | 反降级：安装低于 min_build 的包 | 拒绝 | `[WIN-VERIFY]` |
| PS-49 | A 证据 MAC 失败 | `app_record_unverified`（不升级） | `[DESIGN]` |

**累计**：反例 85 + 27 = **112**；正例 35 + 14 = **49**。

---

## 18. 各项状态矩阵

| 修复项 | 设计 | Windows 实机 | SQLite 探针 | 静态审查 |
|---|---|---|---|---|
| 路径常量与 6 入口封禁 | ✅ | 待验 | — | 待第九轮 |
| 8 步验证状态机 | ✅ | 待验（W-7/8/9） | — | 待 |
| 目录链 ACL + SDDL | ✅ | 待验（W-1…W-6） | — | 待 |
| ACL 结构比对 | ✅ | 待验 | — | 待 |
| signer 端 V-1…V-7 | ✅ | — | — | 待 |
| svc 账户 + LSA Secret | ✅ | 待验 | — | 待 |
| A 证据触发器（4 + seal 2） | ✅ | — | 待验（S-5/S-6/S-13） | 待 |
| allowlist N-1…N-12 | ✅ | 部分待验 | — | 待 |
| 热日志策略 | ✅ | 待验 | 待验（S-7） | 待 |
| 升级/备份 | ✅ | 待验 | — | 待 |
| 二进制完整性 | ✅ | 待验（W-10） | — | 待 |
| R-21 收紧 | ✅ | — | 待验 | 待 |
| R-17 | ❌ 未解决 | — | 待验 | — |

---

## 19. §26 交付状态（完整）

```
V3.8 设计文件已生成，待第九轮独立审计；
方案未获批；未获实施授权；

未决风险（8 项，全部不得视为已解决）：
  R-17  SQLite 全局序号语义           [OPEN]
  R-19  Windows ACL 未实机验证        [OPEN]
  R-21  会话最终态 DB 不强制          [OPEN, P2]
  R-22  auth.db 路径权威              [设计闭环, 待实机验证]
  R-23  signer 二进制完整性           [设计闭环, T9 下 OPEN]
  R-24  父目录 ACL                    [设计闭环, 待实机验证]
  R-25  跨决策复用                    [设计闭环, 待审查]
  R-26  trading.db 的 DDL 拆解 seal   [OPEN, 无 SQLite 层解]

未执行：SQL / 探针 / 迁移 / ACL 修改 / 服务操作 / 源码修改；
S0 未启动。
```

---

## 20. 待验证清单

### 20.1 Windows（`[WIN-VERIFY]`）

| # | 项 |
|---|---|
| W-1 | `%PROGRAMDATA%` 子目录默认继承 ACE（是否含 `AD`） |
| W-2 | `/inheritance:r` 后 `svc_fosun` 仍可写（需 Log on as service） |
| W-3 | `sc.exe sidtype FosunSigner unrestricted` 取服务 SID |
| W-4 | 只读打开 DELETE-journal 库是否需要目录写权 |
| W-5 | 命名管道 `FIRST_PIPE_INSTANCE` 防抢占 |
| W-6 | `icacls` 输出在中文系统下的解析（本方案**不依赖**文本，但仍需验证安装器日志可读） |
| W-7 | guard handle（无 share-delete）能否阻止 rename/delete |
| W-8 | `GetFinalPathNameByHandleW` 返回 `\\?\C:\ProgramData\...` 的确切形式 |
| W-9 | `FILE_FLAG_OPEN_REPARSE_POINT` 对 junction 的识别 |
| W-10 | WinVerifyTrust + 证书 pin 的实际行为（含离线吊销检查） |
| W-11 | 本地账户 DPAPI 用户 scope 在 `CreateProfile` 后可用 |
| W-12 | 服务以 `svc_fosun` 启动时对 `%PROGRAMDATA%\FosunAI\authdb` 的写权 |

### 20.2 SQLite（`[SQLITE-PROBE]`）

| # | 项 |
|---|---|
| S-1 | 同事务 read-your-writes |
| S-2 | 触发器子查询能否读同事务更新的计数器（**R-17 关键**） |
| S-3 | `BEGIN IMMEDIATE` 多连接串行化 |
| S-4 | ROLLBACK 后计数器恢复 |
| S-5 | `INSERT OR REPLACE` 是否触发 DELETE 触发器 |
| S-6 | BEFORE INSERT `RAISE(ABORT)` 是否回滚 REPLACE 的删除 |
| S-7 | ro 连接遇热 journal 的错误码 |
| S-8 | `UNIQUE(response_digest)` 对 NULL 的多行允许 |
| S-9 | 多 BEFORE 触发器执行顺序 |
| S-10 | 崩溃恢复语义 |
| S-11 | 跨库 ATTACH 的只读约束 |
| S-12 | `journal_mode=DELETE` 下并发读 |
| S-13 | `DELETE FROM ai_app_evidence_seal` 是否被 `trg_app_seal_no_delete` 拒绝（自查-1 验证点） |
| S-14 | `UPDATE ai_app_evidence_seal` 是否被 `trg_app_seal_no_update` 拒绝 |
| S-15 | seal 行存在 / 不存在两种情形下 `INSERT OR REPLACE INTO ai_app_evidence` 的最终状态 |

---

## 21. V3.7 → V3.8 差异

| 项 | V3.7 | V3.8 |
|---|---|---|
| 路径权威 | 建议硬编码 | **常量 + 6 入口封禁 + 8 步句柄验证** |
| 目录链 | L2–L4 | **L1–L5 + 逐 ACE 表 + SDDL + icacls** |
| ACL 校验 | 文本断言 | **结构比对（GetAce）** |
| 跨决策校验 | 应用侧 S-06 | **服务端 V-1…V-7** |
| 服务账户 | 一句话 | **三方案比较 + LSA Secret + profile** |
| A 证据 | 无保护 | **3 触发器 + seal** |
| allowlist | 粗规则 | **N-1…N-12** |
| 热日志 | 无策略 | §11 |
| 升级/备份 | 未定义 | §13 |
| 二进制 | OPEN | §14（T9 仍 OPEN） |
| 会话出生态 | `signed\|committed` | **仅 `created`** |
| 触发器/表/视图 | 23 / 10 / 6（全部内联） | **29 / 11 / 7**（内联 6 / 1 / 1，余继承 V3.7） |
| CE / PS | 85 / 35 | **112 / 49**（含自查新增 CE-112） |

---

## 22. 自检表（不构成审计）

| # | 检查 | 结果 |
|---|---|---|
| C-01 | 是否给出 SDDL 与 icacls | ✅ §6.2/§6.4 |
| C-02 | 是否覆盖 L1 父目录 | ✅ |
| C-03 | 是否做结构比对而非文本匹配 | ✅ §6.5 |
| C-04 | 是否处理 TOCTOU | ✅ §5.4 |
| C-05 | 是否把决策校验移到服务端 | ✅ §8 |
| C-06 | 账户方案是否比较并选定 | ✅ §9 |
| C-07 | CE-32 矛盾是否修正 | ✅ §17.1 |
| C-08 | §26 是否列全全部风险 | ✅ §19（8 项，含 R-26） |
| C-09 | 是否混用状态标签 | ✅ 全文只用 5 种 |
| C-10 | 是否宣称实测 | ✅ 无 |
| C-11 | 对象计数是否可复算 | ✅ §16 给出算式 `10+1 / 23+4+2 / 6+1`，与正文 grep 一致 |
| C-12 | seal 表自身是否受保护 | ✅ §10 补 2 触发器；DDL 层缺口记 R-26 且不宣称解决 |
| C-13 | 是否发现并公开自己的 P0 | ✅ §3 自查-1 |

---

## 23. 交付状态

```
V3.8 已生成，待第九轮独立审计；
未执行 SQL / 探针 / 迁移 / ACL 修改 / 服务操作；
方案未获批；未获实施授权；S0 未启动。
```

---

> 本文件 SHA256（本行之前全部字节，可用 `head -n -1 <本文件> | sha256sum` 复算）：`60721b66054746b308595ce35892fc3fb33c79eebb1928b8589211c686560a83`
