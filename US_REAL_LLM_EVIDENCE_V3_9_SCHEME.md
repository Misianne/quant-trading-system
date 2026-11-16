# V3.9 修订方案（设计稿 · 不实施）

| 项 | 值 |
|---|---|
| 版本 | **v3.9**（2026-11-11 16:40） |
| 取代 | `US_REAL_LLM_EVIDENCE_V3_8_SCHEME.md`（`1c8ebce3…`，**只读基线，不覆盖、不修改**） |
| 依据 | V3.8 第九轮独立只读审计（FAIL：1×P0 路径权威、1×P0/P1 ACL 闭环、1×P1 凭据生命周期、3×P2） |
| 性质 | **文档修订**。未执行 SQL / 探针 / 迁移 / ACL 修改 / 服务操作 / 源码修改 |
| 状态 | V3.9 待第十轮独立审计；未获批；未获实施授权；S0 未启动 |

**修订项状态标签（全文只用这 4 种，不混用）**

| 标签 | 含义 |
|---|---|
| `[DESIGN]` | 本文档已完成设计描述，逻辑自洽 |
| `[STATIC_REVIEW]` | 尚待独立静态审查 |
| `[WINDOWS_TEST]` | 尚待 Windows 实机验证 |
| `[SQLITE_PROBE]` | 尚待未来隔离探针验证 |

> **标签口径声明**：以上 4 种只用于"修订项"。风险另有 `OPEN / 部分 / 闭环(设计)` 三种状态，出现在 §19 的"风险状态"列——那一列衡量的是**残余风险**，不是修订项完成度，两者不互换、不混用。

---

## 1. 修订摘要

第九轮审计的判决是：**ACL 与路径这两块"看起来写了"的东西，经不起"怎么落地"的追问**。

V3.9 只解决四件事，不扩张架构：

| # | 变化 | 关闭 |
|---|---|---|
| 1 | 路径权威**拆成三个正交问题**（路径选择 / 文件身份 / OS 保护），根路径改由系统 API 单独供给，编译期常量**只固定相对结构**，消除"固定 C:\ProgramData 又动态取根"的自相矛盾 | P0-1 |
| 2 | ACL 补齐**安装 / 升级 / 重建 / 回滚**闭环；明确 `/inheritance:r`+`/grant:r` **不是** DACL 清理，改写为**整份 DACL 替换**（`SetNamedSecurityInfoW` 或 `Set-Acl`+SDDL），并对每个对象给出 Owner/DACL/ACE/OI-CI/四类主体 | P0-2 / P1 |
| 3 | 服务账户凭据**三对象辨析**（SCM 口令 / DPAPI 密钥 / escrow），补齐口令轮换与 reset 的**顺序、回滚、fail-closed**，并明确"建了 profile ≠ DPAPI 可用" | P1 |
| 4 | 权限掩码**逐位核算**并纠正 V3.8 的错误：`0x1301BF` = Modify，**不含** `FILE_DELETE_CHILD`；据此撤销 L2 的 `FA`、把 L5 的 `M` 收窄为 `0x12019F`（可写不可删） | P2 |

外加：状态机步骤号与"8 步"标题不符（实为 S0–S8 共 9 步）→ 重写为 **S0–S12 共 13 步**；对象计数改为可人工复算的算式。

---

## 2. 基线核验

| 文件 | 字节 | 行 | SHA256 | 判定 |
|---|---|---|---|---|
| `US_REAL_LLM_EVIDENCE_V3_8_SCHEME.md` | 42,074 | 805 | `1c8ebce316e3aad3d5ce9162edb070d4e43205cf8b0cbcdbdc906f24e5a3b14f` | ✅ 与命令一致 |
| V3.8 自指纹（去末行） | — | — | `60721b66054746b308595ce35892fc3fb33c79eebb1928b8589211c686560a83` | ✅ 复算一致 |
| `US_REAL_LLM_EVIDENCE_V3_7_SCHEME.md` | — | — | `a4bc2964fbf91ef590f6ef1554b544e5c1910ce50f80d7aeb34b859c6c2efc46` | ✅ |
| `US_REAL_LLM_EVIDENCE_V3_6_SCHEME.md` | — | — | `34c02c370f59a5d99fe4f6eb2a8f1ddd705672e55f93a68b2121db80f8ce223a` | ✅ |
| `US_REAL_LLM_EVIDENCE_V3_5_SCHEME.md` | — | — | `3f308b17a43731d6d3ce5d0a741b2c17a34549e9cc03c9c95e13f2eb599f9534` | ✅ |
| `US_REAL_LLM_EVIDENCE_REMEDIATION_PLAN.md` | — | — | `3d569f16754e389c77534cbbbfc3e294ad5d6bba67946fb7bb9f3f7fd629610a` | ✅ |

| 目录 | 文件数 | 16:00 后改动 |
|---|---|---|
| `src/` | 51 | 0 |
| `config/` | 2 | 0 |
| `tests/` | 37 | 0 |
| `data/` | 6 | 0 |
| `logs/` | 68 | 0 |

未读取 `.env`、任何凭据文件或密钥材料。第九轮审计报告未落盘，本文引用其结论仅依据对话文本，**未声称读取文件**。

---

## 3. V3.8 问题追踪

| 编号 | 级别 | 问题（第九轮） | V3.9 处置 | 章节 | 状态 |
|---|---|---|---|---|---|
| P0-1a | P0 | 常量写死 `C:\ProgramData\...`，同时用 `SHGetKnownFolderPath` 取根 → 两条权威并存 | 根路径**唯一**来自 API；常量降级为**相对组件**；二者组合规则显式化 | §5.2/§5.3 | `[DESIGN]` |
| P0-1b | P0 | `GetFinalPathNameByHandleW` 结果与"常量"逐字符比较，未定义 UNC/短名/失败/缓冲 | 定义返回处理、组件边界比较、5 类拒绝 | §5.6 | `[DESIGN]` |
| P0-1c | P0 | 未区分"路径选择权威"与"文件身份验证"，把硬编码当安全 | 三关注点分离表 | §5.1 | `[DESIGN]` |
| P0-1d | P0 | 只校验最终文件，未校验祖先链；未处理预创建劫持 | 自顶向下 pin + 逐级校验；T11 | §5.7/§5.8 | `[DESIGN]` |
| P0-2a | P0 | `/inheritance:r` + `/grant:r` **不等于**清除历史显式 ACE | 改为整份 DACL 替换；明确两命令的真实语义 | §6.5/§7.2 | `[DESIGN]` |
| P0-2b | P0 | 无安装/升级/重建/回滚流程 | 三模式 + 回滚 + fail-closed 表 | §7 | `[DESIGN]` |
| P0-2c | P0 | 未定义"未知显式 ACE"处理 | 三分法判定矩阵（保留/拒绝/重建） | §7.3/§7.7 | `[DESIGN]` |
| P1-1 | P1 | ACL 校验只看 Users 是否有写权，未定义完整期望集 | 期望集 E + 允许集 X + 全等判定 | §8 | `[DESIGN]` |
| P1-2 | P1 | PowerShell 示例 `$SVC:(...)` 变量解析歧义 | 全部改 `${SVC}` | §6.5 | `[DESIGN]` |
| P1-3 | P1 | svc 账户/SCM/DPAPI 生命周期未闭环（轮换、reset、profile、SID 变化） | 三对象辨析 + 顺序 + 回滚 + 恢复流程 | §10 | `[DESIGN]` |
| P2-1 | P2 | `0x1301BF` 被说成"含 FILE_DELETE_CHILD"——**错误** | 逐位核算并纠正；据此改 L2/L5 | §9 | `[DESIGN]` |
| P2-2 | P2 | "8 步"实为 S0–S8 共 9 步 | 重写为 S0–S12 共 13 步 | §5.7 | `[DESIGN]` |
| P2-3 | P2 | 计数不可复算、§16/§21 曾冲突 | 给出算式与内联/继承/重定义三分类 | §17 | `[DESIGN]` |
| P2-4 | P2 | ACL 校验未基于句柄 | 全部改为句柄优先 `GetSecurityInfo(handle)` | §8.1 | `[DESIGN]` |
| 自查-a | P0 | L5 给 SVC `M` 却声称"备份不可被随意替换"，自相矛盾 | L5 收紧为 `0x12019F`（可写不可删） | §6.3/§9.7 | 已修正 |
| 自查-b | P0 | L2 给 SVC `FA`（含 `WRITE_DAC`）→ SVC 可改自身 DACL 放行 Users | L2 收紧为 `M` | §6.3 | 已修正 |

> `自查-a/b` 是**本轮写作中自查发现**的，不是审计结论；两项都属"V3.8 内部自相矛盾"，V3.9 已改。

---

## 4. 威胁模型

### 4.1 主体与能力（不变，仅补 T11 一行）

| 主体 | 能力 | 是否在范围内 |
|---|---|---|
| T1 远端未认证 | 无本机权限 | ✅ |
| T2 本机普通用户 | 可执行程序、可写 `%LOCALAPPDATA%`、可读 `authdb`（RX） | ✅ |
| T3 应用进程（同用户） | 可连 IPC、可写 `trading.db` | ✅ |
| T4 被控应用进程 | 可伪造任何请求字段 | ✅ |
| T5 网络中间人 | 可改 TLS 之外的字节 | ✅ |
| T6 上游供应商 | 可返回伪造 `body.id` | ✅ |
| T7 本机其他用户 | 与本机普通用户同级 | ✅ |
| T8 服务二进制替换者 | 可改文件但无管理员 | ✅（§15） |
| T9 管理员 / SYSTEM | 可夺属主、可 `SeRestorePrivilege`、可读任意文件 | ❌ **不可声明** |
| T10 目录级替换者 | 可删/换目录 | ✅（§6–§7） |
| **T11 预创建劫持者** | 在安装前置建 `%PROGRAMDATA%\FosunAI` 并自授 DACL，或把它做成 junction | ✅（§7.3） |

### 4.2 与 T11 相关的关键事实

`%PROGRAMDATA%` 默认允许 `Users` 创建子目录（`FILE_ADD_SUBDIRECTORY`）。因此**安装前**普通用户就可能已经创建了 `FosunAI\` 并把 DACL 设为"自己 FA"。安装器若不检查属主就直接 `/grant:r`，会得到一个"用户仍是属主、仍持有 `WRITE_DAC`"的目录——后续 `/grant:r` 成功但用户随时可改回。**这是 P0-2 必须解决的真实路径**，见 §7.3。

---

## 5. 路径权威（重写 P0-1）

### 5.1 三个正交问题（V3.8 混为一谈的根源）

| 问题 | 回答者 | 失败即 |
|---|---|---|
| **A. 路径选择权威**：该用哪个根、哪个子路径 | 系统 API（根）+ 编译期常量（相对结构） | 不启动 |
| **B. 文件身份验证**：句柄指向的对象，是否就是我以为的那个对象 | 句柄级 API（`GetFinalPathNameByHandleW` / `GetFileInformationByHandle`） | 不启动 |
| **C. OS 权限保护**：谁能改、谁能删 | ACL + 属主 + guard handle | 不启动 |

> **硬编码路径 ≠ 文件系统安全。** 编译期常量只能保证"我不会被配置指到别处"，**不能**保证"那个位置上的东西是可信的"。可信性由 B 与 C 提供。

### 5.2 根路径：唯一权威

```c
// 启动时调用一次，结果存入进程内只读全局；不接受任何参数
PWSTR g_root = NULL;
HRESULT hr = SHGetKnownFolderPath(FOLDERID_ProgramData, KF_FLAG_DEFAULT, NULL, &g_root);
if (FAILED(hr) || g_root == NULL) exit(71);
```

| 规则 | 说明 |
|---|---|
| 只调一次 | 启动期调用，之后所有构造都用 `g_root`；运行期不重复取（防环境变化） |
| 来源唯一 | 不读 `%PROGRAMDATA%` 环境变量、不读注册表、不读 CLI、不读配置 |
| 形式校验 | 必须形如 `<盘符>:\...`；拒绝 `\\`（UNC）、`\\.\`、`\\?\GLOBALROOT`；拒绝 `g_root` 长度 > 1024 |
| 归还 | `CoTaskMemFree` 在使用完毕后；进程退出前不释放亦可（只读全局） |
| 失败 | 退出码 **71**，不尝试回退到 `C:\ProgramData`（回退会引入第二条权威） |

> **V3.8 的矛盾点**：常量里写死 `\\?\C:\ProgramData\...`，注释又说"卷标可能不是 C:，启动时用 API 取根"。二者并存 → 出现"到底信谁"的空档。V3.9 取消硬编码根，只保留相对组件。

### 5.3 相对组件：编译期常量

```c
// 只固定结构，不含盘符、不含 ProgramData
static const wchar_t *const kAuthDbPath[] = { L"FosunAI", L"authdb", L"auth.db" };
static const wchar_t *const kSignerPath[] = { L"FosunAI", L"signer" };
static const wchar_t *const kLogsPath[]   = { L"FosunAI", L"logs" };
static const wchar_t *const kBackupPath[] = { L"FosunAI", L"backup" };

// 组合规则（唯一）：
//   final = L"\\\\?\\" + g_root + L"\\" + join(components, L"\\")
// 例：g_root = L"C:\\ProgramData"
//     final  = L"\\\\?\\C:\\ProgramData\\FosunAI\\authdb\\auth.db"
```

| 规则 | 说明 |
|---|---|
| 常量范围 | **只有** `FosunAI` / `authdb` / `auth.db` 这类相对组件；**没有**盘符、**没有** `ProgramData` |
| 组合时机 | 每次使用前由上述唯一函数组合；不允许在别处手写拼接 |
| 组件校验 | 每个组件：非空、不含 `\` / `/` / `:` / `*` / `?` / `"` / `<` / `>` / `\|`、不以 `.` 或空格结尾、不是 `.` 或 `..` |
| 组件数量 | auth.db 路径固定 **5** 段（`C:` / `ProgramData` / `FosunAI` / `authdb` / `auth.db`）；数量不符即拒绝 |
| 拒绝外部传入 | 服务**不接收**任何数据库绝对路径；IPC 协议里没有"路径"字段 |

> 关键区分：**常量固定的是"结构"，不是"位置"。** 位置由 A（系统 API）给出；结构由常量给出；二者拼接后必须再由 B 验证一次。

### 5.4 规范化规则

| 项 | 规则 |
|---|---|
| 大小写 | 比较用 `CompareStringOrdinal(..., TRUE)`（序数、忽略大小写）；**不**用 `_wcsicmp`（区域相关） |
| 分隔符 | 只接受 `\`；出现 `/` → 拒绝（不做"帮忙转换"） |
| 卷路径 | 必须 `\\?\` 前缀形式；`GetFullPathNameW` **只用于日志**，不参与判定 |
| UNC | `\\?\UNC\...`、`\\server\share` → 拒绝 |
| 设备路径 | `\\.\`、`\\?\GLOBALROOT`、`\\?\Volume{...}` → 拒绝 |
| 尾点/尾空格 | 任一组件的最后一个字符为 `.` 或 ` ` → 拒绝（Win32 会静默裁剪，属绕过面） |
| 相对段 | 含 `.` 或 `..` 组件 → 拒绝（`\\?\` 下它们不被解析，但出现即说明构造错误） |
| 短名 | 8.3 短名（如 `AUTHDB~1`）与期望组件不等 → 拒绝；**不做**长名展开后"帮忙通过" |
| 长度 | 全路径 > 32767（`UNICODE_STRING` 上限）→ 拒绝 |

### 5.5 组件边界比较（不是前缀比较）

```c
// 错误做法（V3.8 的"逐字符比较"若实现成前缀即中招）：
//   wcsnicmp(final, expected, wcslen(expected)) == 0
//   → "\\?\C:\ProgramData\FosunAI\authdb-evil\auth.db" 也通过

// 正确做法：按组件切分，段数相等 + 逐段序数比较
int same = split_compare(final, expected);   // 1 = 全等
```

| 断言 | 要求 |
|---|---|
| 段数 | `count(final) == count(expected)`（auth.db 为 5） |
| 逐段 | 每一段 `CompareStringOrdinal(final[i], expected[i], TRUE) == CSTR_EQUAL` |
| 段内空 | 任一段为空（连续 `\`）→ 拒绝 |
| 末段 | 必须恰为 `auth.db`（含扩展名，不接受 `auth.db.`） |

> CE-121 专门覆盖"`authdb-evil` 前缀绕过"。按组件比较则失败。

### 5.6 `GetFinalPathNameByHandleW` 的返回处理

| 情况 | 处理 |
|---|---|
| 返回 0 | `GetLastError()` → 记日志 → 拒绝（退出码 72） |
| 返回 > 缓冲区 | 首次用 `cch=0` 探询所需长度；若 `> 32767` 拒绝；否则按需分配重取一次；仍不足 → 拒绝 |
| 返回 `\\?\UNC\...` | 拒绝（说明该卷是网络重定向） |
| 返回 `\\?\Volume{...}\...` | 拒绝（说明未按 DOS 卷名解析） |
| 返回含 8.3 组件 | 组件比较自然失败 → 拒绝；**不**尝试展开 |
| 成功后 | 立即按 §5.5 比较；比较对象是**已 pin 的句柄**，不是路径字符串 |

```c
DWORD need = GetFinalPathNameByHandleW(h, NULL, 0, FILE_NAME_NORMALIZED | VOLUME_NAME_DOS);
if (need == 0 || need > 32767) fail(72);
wchar_t *buf = malloc(need * sizeof(wchar_t));
DWORD got = GetFinalPathNameByHandleW(h, buf, need, FILE_NAME_NORMALIZED | VOLUME_NAME_DOS);
if (got == 0 || got >= need) fail(72);
```

### 5.7 打开与验证状态机（**13 步**，S0–S12）

> V3.8 标题写"8 步"，正文编号是 S0–S8（9 步）。V3.9 重写并明确计数。

```
S0  取根            SHGetKnownFolderPath(FOLDERID_ProgramData) → g_root；失败 exit 71
S1  构造与形式校验   常量组件拼接 → 组件数/字符集/尾点/相对段校验；失败 exit 72
S2  pin L1          CreateFileW(FosunAI, BACKUP_SEMANTICS|OPEN_REPARSE_POINT,
                                share=READ|WRITE(不含 DELETE), OPEN_EXISTING, DIRECTORY)
S3  验 L1           非 reparse、是目录、Owner=S-1-5-18、DACL 结构 == E(L1)
S4  pin L2          signer\（同上）
S5  验 L2           E(L2)
S6  pin L3          authdb\（同上）
S7  验 L3           E(L3)
S8  pin 文件        auth.db（OPEN_REPARSE_POINT，share=READ|WRITE，不含 DELETE，非 DIRECTORY）
S9  验文件身份      非 reparse；nNumberOfLinks == 1；VolumeSerial == L3 句柄的 VolumeSerial；
                    Owner、DACL 结构 == E(auth.db)
S10 验最终路径      GetFinalPathNameByHandleW → §5.5 组件全等（5 段）
S11 打开 SQLite     路径仍为常量；打开成功后立即**重新**取一次文件标识，与 S9 记录比对
S12 保持并周期复核  保留 L1/L2/L3/文件 四个 guard handle 到进程退出；
                    每 60s 用**同一句柄**复核 file id 与 DACL 摘要（不重开路径）
```

| 步 | 失败退出码 | 语义 |
|---|---|---|
| S0 | 71 | 根路径权威不可用 |
| S1 | 72 | 构造/形式不合规 |
| S2/S4/S6/S8 | 73 | 打开失败或对象类型不符 |
| S3/S5/S7/S9 | 73 / 74 | 73=reparse/硬链接/身份；74=Owner/DACL 不符 |
| S10 | 72 | 最终路径不等于期望 |
| S11 | 77 | SQLite 打开失败或身份已变 |
| S12 | 进程内 | 运行期复核失败 → 停止服务并写事件日志（不静默降级） |

> **顺序的意义**：自顶向下 pin。父目录先被 pin（不带 `FILE_SHARE_DELETE`）→ 子对象在其后被打开期间无法被删除/改名 → 消除"验证的是 A、用的是 B"。**这不等于 TOCTOU 全解**，见 §5.10。

### 5.8 文件身份（不只是路径）

```c
BY_HANDLE_FILE_INFORMATION bi;
GetFileInformationByHandle(hFile, &bi);
// 身份 = (bi.dwVolumeSerialNumber, bi.nFileIndexHigh, bi.nFileIndexLow)
```

| 断言 | 要求 |
|---|---|
| 卷一致 | `bi.dwVolumeSerialNumber == L3 句柄的 VolumeSerial`（防"同路径不同卷"） |
| 索引一致 | S9 与 S11 两次取值必须相同（防"验证后被换"） |
| 链接数 | `bi.nNumberOfLinks == 1`（硬链接拒绝） |
| 属性 | `bi.dwFileAttributes` 不含 `FILE_ATTRIBUTE_REPARSE_POINT`；含 `FILE_ATTRIBUTE_DIRECTORY` 则拒绝 |

### 5.9 reparse point / junction / symlink / 硬链接 / 目录替换

| 攻击 | 检测点 | 依据 |
|---|---|---|
| `authdb` 是 junction | S6 用 `FILE_FLAG_OPEN_REPARSE_POINT` 打开，S7 查 `FILE_ATTRIBUTE_REPARSE_POINT` | 打开的是链接本身 |
| `auth.db` 是 symlink | S8 + S9 同上 | 同上 |
| `auth.db` 是硬链接（指向别处真库） | S9 `nNumberOfLinks != 1` | 拒绝 |
| 目录被整体替换 | S2–S12 全程持有 guard handle（无 share-delete） | 替换需先删除/改名 → 被句柄阻断 |
| 安装前预创建（T11） | §7.3 属主检查 + 整份 DACL 替换 | 见 §7 |
| 卷被重定向（ProgramData 在非系统盘/网络盘） | S0 形式校验 + S10 UNC/Volume 拒绝 | 拒绝 |

### 5.10 明确不声称（TOCTOU）

> 单次 `GetFinalPathNameByHandleW` **不能**解决全部 TOCTOU。V3.9 的实际保证是：
> 1. 验证基于**已 pin 的句柄**（对象在验证期间不可能被换）；
> 2. 父目录先 pin，子对象后开 → 缩小竞态窗口到"父已 pin、子尚未 pin"的极短区间；
> 3. 该区间内要换子对象，需要对父目录有 `DELETE_CHILD`——而父目录 ACL（§6）已剥离该权限给非管理员；
> 4. 管理员仍可用 `SeRestorePrivilege` 绕过 → **R-28，OPEN，不声称解决**。

### 5.11 禁止的覆盖入口（6 类，逐个封死）

| 入口 | 处理 |
|---|---|
| 配置文件（yaml/json/ini） | 服务**不读取**任何含路径的配置；只读 allowlist 与 keyring |
| 环境变量 | 不调用 `GetEnvironmentVariable` 决定路径（`%PROGRAMDATA%` 亦不用） |
| CLI 参数 | 服务由 SCM 启动，argv 仅服务名；其余忽略 |
| 注册表 | 不读 `HKLM\SOFTWARE\FosunAI` 下任何路径键；存在即告警并忽略 |
| 当前工作目录 | 全路径绝对；不拼接 CWD |
| 连接字符串/URI | `sqlite3_open_v2` 的 filename 为常量；不使用 URI 参数、不传 `:memory:` |

### 5.12 两个"不"

> 不声称"硬编码即安全"；不声称"一次句柄校验即消除 TOCTOU"。

---

## 6. 目录链与对象清单（ACL 设计）

### 6.1 对象清单（5 目录 + 6 类文件）

| ID | 对象 | 类型 | 属主 |
|---|---|---|---|
| L1 | `%PROGRAMDATA%\FosunAI\` | 目录 | `S-1-5-18` |
| L2 | `…\FosunAI\signer\` | 目录 | `S-1-5-18` |
| L3 | `…\FosunAI\authdb\` | 目录 | `S-1-5-18` |
| L4 | `…\FosunAI\logs\` | 目录 | `S-1-5-18` |
| L5 | `…\FosunAI\backup\` | 目录 | `S-1-5-18` |
| F1 | `authdb\auth.db` | 文件 | `S-1-5-18` |
| F2 | `authdb\auth.db-journal` / `-wal` / `-shm` | 文件（运行期产生） | 创建者（SVC） |
| F3 | `signer\keyring.json`、`signer\key-<id>.dpapi` | 文件 | `S-1-5-18` |
| F4 | `signer\escrow\key-<id>.machine.dpapi` | 文件 | `S-1-5-18` |
| F5 | `logs\*.log` | 文件 | 创建者（SVC） |
| F6 | `backup\auth-*.db` / `*.sha256` / `tmp-<ts>\*` | 文件 | `S-1-5-18`（tmp 内） |

### 6.2 SDDL（目录 5 条 + 文件 4 条）

> `SVC` = 服务账户实际 SID（`*S-1-5-21-…`），安装时查询后代入；文档不硬编码。
> `SY`=`S-1-5-18`，`BA`=`S-1-5-32-544`，`BU`=`S-1-5-32-545`。

```
【L1】FosunAI\
O:SYG:SYD:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;0x1200A9;;;BU)

【L2】signer\
O:SYG:SYD:PAI(A;OICI;FA;;;SY)(A;OICI;0x1301BF;;;SVC)

【L3】authdb\
O:SYG:SYD:PAI(A;OICI;FA;;;SY)(A;OICI;0x1301BF;;;SVC)(A;OICI;0x1200A9;;;BU)(A;OICI;0x1200A9;;;BA)

【L4】logs\
O:SYG:SYD:PAI(A;OICI;FA;;;SY)(A;OICI;0x1301BF;;;SVC)(A;OICI;0x1200A9;;;BU)(A;OICI;0x1200A9;;;BA)

【L5】backup\
O:SYG:SYD:PAI(A;OICI;FA;;;SY)(A;OICI;0x12019F;;;SVC)(A;OICI;0x1200A9;;;BA)

【F1】auth.db（文件，OI/CI 必须为 0）
O:SYG:SYD:PAI(A;;FA;;;SY)(A;;0x1301BF;;;SVC)(A;;0x1200A9;;;BU)(A;;0x1200A9;;;BA)

【F3】signer\keyring.json 等（文件）
O:SYG:SYD:PAI(A;;FA;;;SY)(A;;0x1301BF;;;SVC)

【F4】escrow（文件，仅 SYSTEM）
O:SYG:SYD:PAI(A;;FA;;;SY)

【F6】backup 文件（文件）
O:SYG:SYD:PAI(A;;FA;;;SY)(A;;0x12019F;;;SVC)(A;;0x1200A9;;;BA)
```

**与 V3.8 的三处实质变化**

| 对象 | V3.8 | V3.9 | 原因 |
|---|---|---|---|
| L2 SVC | `FA`（含 `WRITE_DAC`/`WRITE_OWNER`） | **`0x1301BF`（Modify，无 DACL 权）** | SVC 不应能改自己密钥目录的 DACL（自查-b，CE-138） |
| L5 SVC | `0x1301BF`（含 `DELETE`） | **`0x12019F`（可新建/写入，不可删）** | V3.8 一边给删权一边称"备份不可被替换"（自查-a，CE-129） |
| L2 BA | `0x1200A9`（可读密钥文件） | **无 ACE** | 密文可读虽不致命，但缩小面；管理员需夺属主（留审计痕迹） |

### 6.3 逐 ACE 语义表

| 对象 | SID | 掩码 | OI | CI | 允许 | 明确不允许 |
|---|---|---|---|---|---|---|
| L1 | `SY` | `FA` (0x1F01FF) | ✓ | ✓ | 全部 | — |
| L1 | `BA` | `FA` | ✓ | ✓ | 全部（含建/删子目录） | — |
| L1 | `BU` | `0x1200A9` | ✓ | ✓ | 列出、遍历、读属性 | 建/删子目录、写、改 DACL、改属主 |
| L2 | `SY` | `FA` | ✓ | ✓ | 全部 | — |
| L2 | `SVC` | `0x1301BF` | ✓ | ✓ | 读写/新建/删除自身文件 | `FILE_DELETE_CHILD`、`WRITE_DAC`、`WRITE_OWNER` |
| L3 | `SY` | `FA` | ✓ | ✓ | 全部 | — |
| L3 | `SVC` | `0x1301BF` | ✓ | ✓ | 读写 `auth.db`、建/删 journal | `FILE_DELETE_CHILD`、改 DACL/属主 |
| L3 | `BU` | `0x1200A9` | ✓ | ✓ | 只读打开（`mode=ro`） | 写、删、改名、建文件 |
| L3 | `BA` | `0x1200A9` | ✓ | ✓ | 只读 | 运行期写/删 |
| L4 | `SVC` | `0x1301BF` | ✓ | ✓ | 写日志、删过期日志 | 改 DACL |
| L4 | `BU`/`BA` | `0x1200A9` | ✓ | ✓ | 读 | 写、删 |
| L5 | `SVC` | `0x12019F` | ✓ | ✓ | 新建备份、写、读回校验 | **删除任何文件**、`FILE_DELETE_CHILD`、改 DACL |
| L5 | `BA` | `0x1200A9` | ✓ | ✓ | 读 | 写、删 |
| F1 | 同 L3 | — | **0** | **0** | 文件上 OI/CI 无意义，必须为 0 | 继承位非 0 视为配置错误 |
| F4 | `SY` only | `FA` | 0 | 0 | SYSTEM 读写 | 任何其他主体（含 SVC） |

> 关键：**`BA` 在 L2 无 ACE**。管理员要读密钥文件必须 `SeTakeOwnershipPrivilege` 夺属主——这会产生**安全审计事件**，是刻意的"留痕"设计。

### 6.4 `icacls` 命令（`[WINDOWS_TEST]`，本轮**不执行**）

```powershell
# 变量解析：必须用 ${SVC}，禁止 $SVC:(OI)(CI)M ——
# PowerShell 会把 "$SVC:" 当作 drive-qualified 变量，导致空串或解析错误。
${SVC} = "*S-1-5-21-<域>-<RID>"      # 安装时查询所得
${Base} = "${env:ProgramData}\FosunAI"

icacls "${Base}"            /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" "*S-1-5-32-545:(OI)(CI)RX" /setowner "*S-1-5-18"
icacls "${Base}\signer"     /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "${SVC}:(OI)(CI)M" /setowner "*S-1-5-18"
icacls "${Base}\authdb"     /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "${SVC}:(OI)(CI)M" "*S-1-5-32-545:(OI)(CI)RX" "*S-1-5-32-544:(OI)(CI)RX" /setowner "*S-1-5-18"
icacls "${Base}\logs"       /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "${SVC}:(OI)(CI)M" "*S-1-5-32-545:(OI)(CI)RX" "*S-1-5-32-544:(OI)(CI)RX" /setowner "*S-1-5-18"
icacls "${Base}\backup"     /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "${SVC}:(OI)(CI)(RD,WD,AD,REA,WEA,RA,WA,RC,S)" "*S-1-5-32-544:(OI)(CI)RX" /setowner "*S-1-5-18"
```

> L5 的 `${SVC}` 用**显式具体权限**而非简写：`RD,WD,AD,REA,WEA,RA,WA,RC,S` 对应 `0x12019F`（**无 `DE`、无 `DC`、无 `X`**）。`icacls` 的简写 `M` 会带入 `DE`（删除自身），因此**不能**用于 L5。
> 上述命令**只是文档**。真正的落地以 §7.2 的"整份 DACL 替换"为准；`icacls` 仅作等价展示。

### 6.5 为什么不能只靠 `icacls`（必须写清楚）

| 命令 | 真实语义 | 能否清除历史显式 ACE |
|---|---|---|
| `/inheritance:r` | 移除**继承**来的 ACE，并置 `SE_DACL_PROTECTED` | ❌ 显式 ACE 原样保留 |
| `/grant:r` | 替换**指定 SID** 的"允许"ACE | ❌ 其他 SID 的显式 ACE 原样保留 |
| `/remove:g <SID>` | 移除某 SID 的允许 ACE | 需**逐个** SID 列出 |
| `/reset` | 用**父目录继承**重建 DACL | ❌ 且方向相反（会重新引入继承） |

> 因此："`/inheritance:r` + `/grant:r`" ≠ "干净 DACL"。V3.8 的表述必须作废。

---

## 7. ACL 安装 / 升级 / 重建 / 回滚

### 7.1 三种模式

| 模式 | 触发 | 做法 |
|---|---|---|
| M-A 全新安装 | 目标路径不存在 | **从诞生即正确**：`CreateDirectory` 时传 `SECURITY_ATTRIBUTES`（内含已构造 SD），杜绝"先建后改"的窗口 |
| M-B 升级/修复 | 路径已存在 | 预扫 → 分类 → **整份替换** → 结构复核 → 失败回滚 |
| M-C 运行期 | 服务启动 | **只读校验**，服务永不改 ACL（改 ACL 属安装器职责） |

### 7.2 整份 DACL 替换（唯一正确原语）

```c
// 首选：直接置 DACL，并保护（切断继承）
SetNamedSecurityInfoW(
    path, SE_FILE_OBJECT,
    DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
    NULL, NULL, pAcl /* 由 SDDL 解析出的完整 ACL */, NULL);
```

```powershell
# 等价 PowerShell（注意：这是"整份替换"，不是 grant）
${acl} = New-Object System.Security.AccessControl.DirectorySecurity
${acl}.SetSecurityDescriptorSddlForm(${sddl})
${acl}.SetAccessRuleProtection($true, $false)   # 保护 + 不保留继承
Set-Acl -Path ${path} -AclObject ${acl}
```

| 步 | 动作 | 失败 |
|---|---|---|
| 1 | `GetNamedSecurityInfoW` 导出当前 SDDL → 写 `backup\acl-<对象>-<ts>.sddl` | 退出码 74 |
| 2 | 若属主 ≠ `S-1-5-18`：`SeTakeOwnershipPrivilege` 夺属主 → 再置属主为 `SY` | 退出码 74（中止安装） |
| 3 | 整份替换 DACL（上文 API） | 退出码 74 |
| 4 | 立刻用 §8 算法结构复核（同进程、刚写入的句柄） | 退出码 74 + 回滚 |
| 5 | 逐对象串行；任一步失败 → 回滚已完成的对象（用第 1 步导出的 SDDL） | 见 §7.5 |

> 顺序必须 L1 → L5，因为 **L1 的 DACL 必须在子目录创建/校验前就位**；否则子目录会继承到"用户可写"的旧 ACL。

### 7.3 未知显式 ACE 处理（P0-2c）

对每个对象，把现有 DACL 的每个 ACE 归入三类：

| 类 | 判定 | 动作 |
|---|---|---|
| **KEEP** | `(type, flags, mask, sid)` 与期望集 E **完全相等** | 保留（替换后仍会重建为同值） |
| **ALLOWED-EXTRA** | 属系统托管且不在 E 中（本方案取 **空集**，见下） | 允许存在，不报错 |
| **UNKNOWN** | 其余一切（含 `BU:F`、`Everyone:*`、`CREATOR OWNER`、任何 DENY） | **必须移除**；替换后复核仍存在 → 拒绝启动 |

| 决定 | 理由 |
|---|---|
| ALLOWED-EXTRA = ∅ | 一旦允许"额外 ACE"，就回到"只检查 Users 有没有写权"的老问题（P1-1）。宁可误拒（fail-closed），不放行未知项 |
| `CREATOR OWNER`(S-1-3-0) 一律视为 UNKNOWN | 它会让"当前属主"动态获得权限 → 属主一变即提权（CE-135） |
| 任何 `DENY` ACE 一律视为 UNKNOWN | 残留 DENY 可能让服务自己读不到（CE-126），必须显式重建而非"凑合" |
| `S-1-5-11`(Authenticated Users) | 视为 UNKNOWN；若需只读，用 `BU` 显式表达 |

> **T11 预创建劫持的具体处置**：若 `L1` 已存在且属主不是 `S-1-5-18`（典型：普通用户预建并自授 `FA`），安装器**不得**直接 `/grant:r`（那会留下用户仍是属主、仍持 `WRITE_DAC` 的目录）。必须走 §7.2 步 2 夺属主 → 步 3 整份替换 → 步 4 复核。夺属主失败 → **中止安装**，不降级。

### 7.4 属主与权限不足

| 场景 | 处理 |
|---|---|
| 属主是用户，安装器是 SYSTEM | `SeTakeOwnershipPrivilege` + `SeRestorePrivilege` 夺属主 → 重建 |
| 属主是 SYSTEM，但安装器无 `WRITE_DAC` | 不应发生（SYSTEM 有 `FA`）；若发生 → 中止并报错 |
| 目录被其他进程句柄占用（无法置 DACL） | `SetNamedSecurityInfoW` 返回 `ERROR_SHARING_VIOLATION` → 中止，提示先停服务 |
| L1 是 reparse point | **中止安装**（不做"删除重建"，避免误删用户数据；由管理员人工处置） |
| 磁盘只读 / ACL 不可写 | 中止 |

### 7.5 回滚

| 阶段 | 回滚动作 |
|---|---|
| 步 1（导出）后失败 | 用已导出的 `.sddl` 逐个还原 → 复核 → 若还原也失败：**保持服务停止**并写事件日志 |
| 步 3 部分对象已改 | 逆序（L5→L1）还原 |
| 步 4 复核失败 | 视为"替换未生效"，还原并中止 |
| 回滚本身失败 | **不启动服务**；安装器退出码 74；管理员介入（文档给出人工恢复步骤） |

> 原则：**ACL 不完整时，宁可服务不启动，也不带病运行。**

### 7.6 fail-closed 规则表

| 条件 | 服务行为 |
|---|---|
| 任一对象缺失 | 不创建，退出 **79** |
| 任一对象 Owner ≠ `SY` | 退出 **74** |
| 任一对象 DACL 未保护（无 `SE_DACL_PROTECTED`） | 退出 **74** |
| 任一对象存在 UNKNOWN ACE | 退出 **74** |
| 期望集 E 中任一 ACE 缺失 | 退出 **74** |
| ACL 复核 API 调用失败 | 退出 **74**（不"假设通过"） |

### 7.7 三类判定汇总（保留 / 拒绝 / 重建）

| 项 | 保留 | 拒绝 | 由安装器重建 |
|---|---|---|---|
| 属主 `SY` | ✅ | — | — |
| 属主 其他 | — | ✅ | 夺属主后重建 |
| `SY:FA` | ✅ | — | — |
| `BU:0x1200A9` | ✅ | — | — |
| `BU:FA` | — | ✅ | 移除 |
| `SVC:M`（L2/L3/L4） | ✅ | — | — |
| `SVC:FA` | — | ✅ | 收窄为 `M` |
| `Everyone:*` | — | ✅ | 移除 |
| `CREATOR OWNER` | — | ✅ | 移除 |
| 任意 `DENY` | — | ✅ | 移除 |
| `SE_DACL_PROTECTED` 未置位 | — | ✅ | 置位 |

### 7.8 继承语义定义（三者不得混用）

| 概念 | 含义 | 本方案用法 |
|---|---|---|
| **继承 ACE**（inherited） | `ACE_HEADER.AceFlags & INHERITED_ACE`，来自父对象 | 全部切断（`PAI` + 不保留继承），**不允许存在** |
| **显式 ACE**（explicit） | 直接写在对象 DACL 上 | 唯一允许的 ACE 来源 |
| **对象继承**（OI） | `OBJECT_INHERIT_ACE`，传递给**文件**子对象 | 仅目录上的目录级 ACE 置 OI；文件 ACE 的 OI 必须为 0 |
| **容器继承**（CI） | `CONTAINER_INHERIT_ACE`，传递给**子目录** | 同上 |

> 复核时 `AceFlags` 必须**逐位**比对：`OI|CI` 缺失或多出（例如只有 `IO`）都算不符（CE-136）。

---

## 8. 基于 SID 的 DACL 结构校验

### 8.1 句柄优先

```c
// 优先：对已 pin 的句柄取安全信息
GetSecurityInfo(hObject, SE_FILE_OBJECT,
                OWNER_SECURITY_INFORMATION | GROUP_SECURITY_INFORMATION |
                DACL_SECURITY_INFORMATION,
                &pOwner, &pGroup, &pDacl, NULL, &pSD);
// 兜底（仅当无句柄可用）：GetNamedSecurityInfoW(path, ...)
```

> 路径版会在 `GetNamedSecurityInfoW` 内部**重新解析路径**，与"我正在用的对象"可能不同。因此校验流程内**只用句柄版**；路径版仅用于安装器对"尚未打开"的对象。

### 8.2 期望集 E / 允许集 X / 判定

```
E(obj) = { (ALLOW, flags, mask, sid) ... }        // §6.3 表逐行
X(obj) = ∅                                        // §7.3

verify(obj):
  SD ← GetSecurityInfo(handle)
  if Owner(SD) != S-1-5-18:                      return FAIL_OWNER
  if !(Control(SD) & SE_DACL_PROTECTED):         return FAIL_UNPROTECTED
  if DACL(SD) == NULL:                           return FAIL_NULL_DACL   # NULL DACL = 人人全权
  matched ← ∅
  for ace in DACL(SD):
      k ← (ace.Type, ace.Flags, ace.Mask, sid_of(ace))
      if k ∈ E: matched ← matched ∪ {k}
      elif k ∈ X: continue
      else:                                      return FAIL_UNKNOWN_ACE
  if matched != E:                               return FAIL_MISSING_ACE
  return OK
```

| 检查 | 说明 |
|---|---|
| `SE_DACL_PROTECTED` | 等价 SDDL 的 `P`；未置位说明继承未断 |
| `DACL == NULL` | 极易被忽略：**NULL DACL 表示所有人全权**，必须显式拒绝 |
| ACE 计数 | `|DACL| == |E| + |X|`；数量不等必然有 UNKNOWN 或缺失 |
| 类型 | 只接受 `ACCESS_ALLOWED_ACE_TYPE`；出现 `ACCESS_DENIED_ACE_TYPE` 或对象 ACE 类型 → UNKNOWN |
| 掩码 | **按位全等**，不做"含某位即可"的宽松判断 |
| SID | `ConvertSidToStringSidW` 后与期望字符串比较（或 `EqualSid` 直接比二进制） |
| `AceFlags` | 目录：`OI|CI` 必须同时置位；文件：必须为 0；出现 `INHERITED_ACE` 即失败 |

### 8.3 不允许的额外项（黑名单，与 §7.3 同源）

| 项 | 结论 |
|---|---|
| `CREATOR OWNER` (S-1-3-0) | 拒绝 |
| `OWNER RIGHTS` (S-1-3-4) | 拒绝（本方案不使用 owner-rights 语义） |
| `Everyone` (S-1-1-0) | 拒绝 |
| `Authenticated Users` (S-1-5-11) | 拒绝 |
| `ANONYMOUS LOGON` (S-1-5-7) | 拒绝 |
| 任意 `DENY` | 拒绝 |
| `ACE` 带 `INHERIT_ONLY_ACE` | 拒绝（本方案不用仅继承 ACE） |

### 8.4 失败码与退出码映射

| 失败码 | 退出码 | 事件日志 |
|---|---|---|
| `FAIL_OWNER` | 74 | ACL/owner mismatch |
| `FAIL_UNPROTECTED` | 74 | DACL not protected |
| `FAIL_NULL_DACL` | 74 | Null DACL |
| `FAIL_UNKNOWN_ACE` | 74 | Unexpected ACE |
| `FAIL_MISSING_ACE` | 74 | Missing expected ACE |
| `FAIL_API` | 74 | Security API failure |

---

## 9. 权限掩码逐位核算（P2）

### 9.1 位表

| 十六进制 | 十进制 | 常量 | 文件语义 | 目录语义 |
|---|---|---|---|---|
| `0x00000001` | 1 | `FILE_READ_DATA` / `FILE_LIST_DIRECTORY` | 读数据 | 列目录 |
| `0x00000002` | 2 | `FILE_WRITE_DATA` / `FILE_ADD_FILE` | 写数据 | 建文件 |
| `0x00000004` | 4 | `FILE_APPEND_DATA` / `FILE_ADD_SUBDIRECTORY` | 追加 | 建子目录 |
| `0x00000008` | 8 | `FILE_READ_EA` | 读扩展属性 | 同 |
| `0x00000010` | 16 | `FILE_WRITE_EA` | 写扩展属性 | 同 |
| `0x00000020` | 32 | `FILE_EXECUTE` / `FILE_TRAVERSE` | 执行 | 穿越 |
| `0x00000040` | 64 | `FILE_DELETE_CHILD` | （文件上无意义） | **删子项** |
| `0x00000080` | 128 | `FILE_READ_ATTRIBUTES` | 读属性 | 同 |
| `0x00000100` | 256 | `FILE_WRITE_ATTRIBUTES` | 写属性 | 同 |
| `0x00010000` | 65536 | `DELETE` | **删自身** | **删自身** |
| `0x00020000` | 131072 | `READ_CONTROL` | 读 DACL/属主 | 同 |
| `0x00040000` | 262144 | `WRITE_DAC` | 改 DACL | 同 |
| `0x00080000` | 524288 | `WRITE_OWNER` | 改属主 | 同 |
| `0x00100000` | 1048576 | `SYNCHRONIZE` | 等待句柄 | 同 |

通用映射：`FILE_GENERIC_READ = 0x00120089`、`FILE_GENERIC_WRITE = 0x00120116`、`FILE_GENERIC_EXECUTE = 0x001200A0`、`FILE_ALL_ACCESS = 0x001F01FF`。

### 9.2 `0x1200A9`（只读+执行）

```
0x001200A9 = 0x00100000 SYNCHRONIZE
           + 0x00020000 READ_CONTROL
           + 0x000000A9
0x000000A9 = 0x80 (READ_ATTRIBUTES) + 0x20 (EXECUTE/TRAVERSE) + 0x08 (READ_EA) + 0x01 (READ_DATA/LIST)
```

| 含 | 不含 |
|---|---|
| 读、列、遍历、读属性、读 EA、`READ_CONTROL`、`SYNCHRONIZE` | `DELETE`(0x10000)、`FILE_DELETE_CHILD`(0x40)、`WRITE_DAC`(0x40000)、`WRITE_OWNER`(0x80000)、写类 |

→ 等价于 `FILE_GENERIC_READ | FILE_EXECUTE`。**V3.8 此处描述正确**。

### 9.3 `0x1301BF`（Modify）—— **V3.8 描述错误，此处纠正**

```
0x001301BF = 0x00100000 SYNCHRONIZE
           + 0x00020000 READ_CONTROL
           + 0x00010000 DELETE
           + 0x000001BF
0x000001BF = 0x100 WRITE_ATTRIBUTES
           + 0x080 READ_ATTRIBUTES
           + 0x020 EXECUTE/TRAVERSE
           + 0x010 WRITE_EA
           + 0x008 READ_EA
           + 0x004 APPEND_DATA / ADD_SUBDIRECTORY
           + 0x002 WRITE_DATA / ADD_FILE
           + 0x001 READ_DATA / LIST
```

| 结论 | 值 |
|---|---|
| 是否含 `DELETE`(0x10000) | **✅ 含** |
| 是否含 `FILE_DELETE_CHILD`(0x40) | **❌ 不含**（`0x1BF & 0x40 == 0`） |
| 是否含 `WRITE_DAC`/`WRITE_OWNER` | ❌ 不含 |
| 等价 | `FILE_GENERIC_READ(0x120089) OR FILE_GENERIC_WRITE(0x120116) OR FILE_GENERIC_EXECUTE(0x1200A0) OR DELETE(0x10000)` |

> **V3.8 §6.2 注写"`0x1301bf` = Modify（含 `DELETE`、`FILE_DELETE_CHILD`）"是错的。** `Modify` 从不包含 `FILE_DELETE_CHILD`；它包含的是 `DELETE`（删自身）。二者是**不同权限**：`DELETE` 作用于对象自身，`FILE_DELETE_CHILD` 作用于父目录内的子对象。

### 9.4 `DELETE` 与 `FILE_DELETE_CHILD` 的区别（必须分清）

| 权限 | 谁持有 | 作用对象 | 效果 |
|---|---|---|---|
| `DELETE` (0x10000) | 文件/目录**自身**的 DACL | 自身 | 允许删除**这个**对象 |
| `FILE_DELETE_CHILD` (0x40) | **父目录**的 DACL | 父目录内的子项 | 允许删除父目录里的**任意**子项（配合子项的 `DELETE` 或"无 DACL 时"） |

> 删除一个文件 `X` 于目录 `D`：需要 `D` 上的 `FILE_DELETE_CHILD`，**或** `X` 自身的 `DELETE`。
> 因此 L3 给 SVC `Modify`（含 `DELETE`，不含 `FILE_DELETE_CHILD`）→ SVC 能删**已有 DACL 的** `auth.db`/journal，但**不能**删别人刚放进去的、SVC 无 `DELETE` 的文件。这正好是我们要的：够用且不多给。

### 9.5 `0x12019F`（L5 专用：可写不可删）

```
0x0012019F = FILE_GENERIC_READ(0x120089) OR FILE_GENERIC_WRITE(0x120116)
           = 0x00100000 + 0x00020000 + 0x0000019F
0x0000019F = 0x100 W 属性 + 0x080 R 属性 + 0x010 W EA + 0x008 R EA
           + 0x004 ADD_SUBDIRECTORY + 0x002 ADD_FILE + 0x001 LIST
```

| 含 | 不含 |
|---|---|
| 列、读、写、建文件、建子目录、读写属性/EA | **`DELETE`、`FILE_DELETE_CHILD`、`WRITE_DAC`、`WRITE_OWNER`、`EXECUTE`** |

→ SVC 能写备份，**不能删备份**；过期清理由 SYSTEM（安装器/计划任务）执行。

### 9.6 目录 vs 文件语义差异

| 掩码位 | 在文件上 | 在目录上 |
|---|---|---|
| `0x1` | 读数据 | **列目录** |
| `0x2` | 写数据 | **建文件** |
| `0x4` | 追加 | **建子目录** |
| `0x20` | 执行 | **穿越（traverse）** |
| `0x40` | 无意义（忽略） | **删子项** |

> 同一掩码在文件与目录上语义不同 → §6.3 表必须分对象写，不能"一套掩码走天下"。

### 9.7 由核算得出的三处设计结论

| 结论 | 影响 |
|---|---|
| 不需要给 SVC `FILE_DELETE_CHILD` | 删除自身文件靠 `DELETE` 即可；因此全链**不出现** `0x40` → T10 面更小 |
| L5 不能用 `M` | `M` 含 `DELETE` → SVC 可删备份；改 `0x12019F` |
| L2 不能用 `FA` | `FA` 含 `WRITE_DAC`/`WRITE_OWNER` → SVC 可改自身 DACL；改 `M` |

---

## 10. 服务账户、SCM 凭据与 DPAPI 生命周期（P1）

### 10.1 三个凭据对象（V3.8 混为一谈）

| # | 对象 | 存在哪 | 谁写 | 谁读 | 用途 |
|---|---|---|---|---|---|
| C1 | **服务账户口令** | SCM 内部（LSA secret `_SC_<服务名>`，命名待 `[WINDOWS_TEST]` 确认） | 安装器（管理员）经 `ChangeServiceConfig` | SCM 启动服务时 | 让 SCM 能以此账户登录 |
| C2 | **HMAC 主密钥** | `signer\key-<id>.dpapi`（DPAPI **user scope**，CurrentUser=SVC，entropy 常量） | SVC | SVC | 签/验证据 MAC |
| C3 | **密钥 escrow** | `signer\escrow\key-<id>.machine.dpapi`（DPAPI **machine scope**，DACL 仅 `SY`） | SVC | **仅 SYSTEM** | 口令 reset / profile 损坏后的恢复 |

| 混淆点 | 澄清 |
|---|---|
| "LSA Secret 存密钥" | ❌ C1 是**口令**的 SCM 副本，不是 HMAC 密钥；V3.8 §9.2 的 `FosunSignerPwd` 属于 C1 的重复副本，**V3.9 取消**（见 §10.3） |
| "SCM 有口令就等于服务能解密密钥" | ❌ SCM 用口令**登录**；密钥解密还需要 **profile 加载 + DPAPI 用户 master key** |
| "建了 profile 就能用 DPAPI" | ❌ 见 §10.7 |

### 10.2 账户与登录权限

| 项 | 设计 |
|---|---|
| 账户 | `.\svc_fosun`（本地账户，非域） |
| 口令策略 | 32 字节随机（`BCryptGenRandom`），base64 传递；`PasswordNeverExpires=1` |
| 登录权 | `SeServiceLogonRight` **仅**授予该账户 |
| 拒绝登录 | `SeDenyInteractiveLogonRight`、`SeDenyRemoteInteractiveLogonRight`、`SeDenyNetworkLogonRight` |
| 组成员 | **不加入** `Administrators`；**无需**加入 `Users`（本方案 ACL 全部按 SID 精确授予，不依赖组） |
| 配置文件 | 安装期 `CreateProfile` 建立；运行期由服务 `LoadUserProfile`（§10.7） |

### 10.3 口令生成 / 交付 / 轮换 / 恢复

**生成与交付**

| 步 | 动作 | 注意 |
|---|---|---|
| 1 | `BCryptGenRandom(NULL, buf, 32, BCRYPT_USE_SYSTEM_PREFERRED_RNG)` | 不落盘明文 |
| 2 | base64 后直接作为 `CreateServiceW` 的 `lpPassword` | 只存在于内存 |
| 3 | `NetUserSetInfo(..., USER_INFO_1003, ...)` 设同一口令 | 与 SCM 一致 |
| 4 | **不写**自定义 LSA Secret | 取消 V3.8 的 `FosunSignerPwd`：与 C1 重复，且多一处可被读取的副本 |
| 5 | 口令字符串用 `SecureZeroMemory` 清除 | — |

**轮换（"change" 语义，非 "reset"）**

| 顺序 | 动作 | 失败回滚 |
|---|---|---|
| 1 | 停服务（`sc stop`，等待 `STOPPED`） | — |
| 2 | 以 SYSTEM 用 **C3 escrow** 解出密钥明文（仅内存） | 解不出 → 中止（不继续改口令，否则密钥永久丢失） |
| 3 | `NetUserSetInfo` 设新口令 | — |
| 4 | `ChangeServiceConfig` 更新 SCM 口令 | **回滚步 3**（恢复旧口令），否则 SCM 与账户不一致 |
| 5 | 启动服务；服务 `LoadUserProfile` 后用新 profile 的 master key 重新保护密钥 → 写 `key-<id>.dpapi.new` → `MoveFileEx(REPLACE_EXISTING)` → 更新 keyring 的 `reprotected_at` | 失败 → 停服 + 安装器回滚步 3/4 |
| 6 | 启动后自检（解开 + 固定向量 MAC 比对） | 失败 → 停服，报事件日志 |

> **顺序不可颠倒**：先改 SCM 后改账户 → SCM 持新口令、账户仍旧口令 → 服务无法登录（CE-133）。

**恢复（"reset" 语义：管理员不知道旧口令）**

| 步 | 动作 | 后果 |
|---|---|---|
| 1 | 停服务 | — |
| 2 | 管理员 `NetUserSetInfo` 直接设新口令（**旧 master key 失效**） | C2 **不可解** |
| 3 | 以 SYSTEM 用 C3 解出密钥 → 在**新 profile** 下重新 `CryptProtectData` 覆盖 C2 | C2 恢复可用 |
| 4 | 若 C3 也损坏 → **密钥不可恢复**：历史证据永久 `mac_unknown_key`，只能新开 key_id | 记入 R-27 |

> 关键诚实点：**DPAPI user scope 的 master key 与口令强耦合**。"reset" 会打断这条链；escrow 是唯一恢复路径。C3 的存在使得"管理员可恢复"，这与 T9 边界一致，**不额外扩大信任集**（管理员本来就能加载该用户 profile 解密）。

### 10.4 SCM 与账户口令同步

| 事实 | 结论 |
|---|---|
| SCM 保存的是**上一次** `CreateService`/`ChangeServiceConfig` 传入的口令 | 改账户口令必须同步改 SCM，否则下次启动失败（错误 `1069`） |
| 同步失败无自动回滚 | 由安装器显式回滚（§10.3 步 4） |
| 服务运行中改口令 | SCM 不感知；下次启动才用新口令 → **必须停服后改** |

### 10.5 LSA Secret 的用途与访问主体

| 项 | 结论 |
|---|---|
| 本方案是否使用自定义 LSA Secret | **不使用**（§10.3 步 4） |
| 为什么要专门说明 | V3.8 把"存口令到 LSA Secret"写成设计，实际是**冗余副本**且访问主体不可控 |
| LSA Secret 的默认访问主体 | 仅 `SYSTEM`（`HKLM\SECURITY` 受保护）——非管理员既读不到也改不了 |
| 若将来确需（如无人值守修复） | 只能以 **C3 escrow**（DPAPI machine + 仅 `SY` 可读的文件 ACL）替代，不要动 LSA secret ACL |

### 10.6 普通用户能做什么（能力矩阵）

| 动作 | 普通用户 | 管理员 | 依据 |
|---|---|---|---|
| 读 C2/C3 密文文件 | ❌ | ✅（L2 无 BA ACE，需夺属主） | §6.2 |
| 读 LSA secret | ❌ | ❌（需 SYSTEM） | `HKLM\SECURITY` |
| 重置 `svc_fosun` 口令 | ❌ | ✅ | 账户管理权 |
| 改服务配置（`SERVICE_CHANGE_CONFIG`） | ❌ | ✅ | SCM |
| 替换 `keyring.json` | ❌ | ❌（需先夺属主） | L2 ACL |
| 改 `auth.db` | ❌（仅 `RX`） | ❌（`RX`，运行期） | L3 ACL |
| 删除 `authdb` 目录 | ❌（L1 无 `FILE_DELETE_CHILD`） | ✅（L1 `FA`） | §6.3 / T9 |

### 10.7 配置文件生命周期（V3.8 最大漏洞）

| 阶段 | 动作 | 说明 |
|---|---|---|
| 安装 | `CreateProfile(szSid, szUser, ...)` | 建立 `%SystemDrive%\Users\svc_fosun` 与注册表 hive 骨架 |
| 服务启动前 | SCM 以该账户**登录**（产生 token） | **登录 ≠ 加载 profile**：服务默认运行在"无加载 profile"状态 |
| 服务启动时 | 服务**必须**自行 `LoadUserProfile`（构造 `PROFILEINFO`，`dwFlags=PI_NOUI`） | 只有加载后 `HKEY_USERS\<SID>` 与 `%APPDATA%` 才可用 |
| 解密前 | 检查 `LoadUserProfile` 返回值 + 探测 `%APPDATA%\Microsoft\Protect\<SID>` 是否存在 | 不存在 → **fail-closed**（退出码 75），**不得**静默改用 machine scope |
| 服务停止 | `UnloadUserProfile` | 避免 hive 泄漏 |
| 异常 | 见 §10.9 | — |

> **明确写入**：创建了用户配置文件 **不等于** DPAPI CurrentUser 可用；SCM 登录 **不等于** profile 已加载。二者都必须显式完成。

### 10.8 DPAPI CurrentUser 的前置条件（清单）

| # | 条件 | 不满足的后果 |
|---|---|---|
| 1 | 账户存在且有 profile 目录 | `CryptUnprotectData` 失败 |
| 2 | 服务已 `LoadUserProfile` 成功 | 同上（`ERROR_FILE_NOT_FOUND` 类） |
| 3 | `%APPDATA%\Microsoft\Protect\<SID>` 下有 master key | 同上 |
| 4 | 口令未被 **reset**（master key 未失效） | 解密失败 `NTE_BAD_KEY_STATE` 一类 |
| 5 | entropy 与加密时一致 | 解密失败 |
| 6 | 进程 token 的 SID 与加密时一致（账户 SID 未变） | 解密失败 |

> 第 4/6 条对应 R-27；第 3/4 条在安装期用 **凭据自检**（§10.12）验证，不靠事后猜测。

### 10.9 异常场景矩阵

| 场景 | 现象 | 处置 |
|---|---|---|
| 服务重启 | profile 未加载 | 每次启动都 `LoadUserProfile`（不依赖"上次已加载"） |
| 系统重启 | profile 目录仍在，hive 未加载 | 同上 |
| 口令 **change** 轮换 | master key 被重新加密 | 按 §10.3 顺序执行，成功后可用 |
| 口令 **reset** | master key 失效 | 用 C3 恢复（§10.3 恢复流程） |
| 账户被删重建（SID 变） | C2 的 ACL 与 DPAPI 上下文全变 | **密钥作废**；必须重新 `key-init` 生成新 `key_id`；旧证据 → `mac_unknown_key` |
| profile 目录损坏 | `LoadUserProfile` 失败 | fail-closed，退出码 75；用 C3 重建（重装 profile + 重新保护） |
| escrow 损坏 | 无法恢复 | R-27 落地：新 `key_id`，旧证据永久不可验 |
| 磁盘满 | 写 `.new` 失败 | 保留旧 C2，回滚口令，停服 |

### 10.10 用户 scope vs 机器 scope

| 维度 | user scope（C2） | machine scope（C3） |
|---|---|---|
| 解密主体 | 该账户（需 profile） | **机器上具备读权限者**（本方案靠文件 ACL 限制为 `SY`） |
| 抗"同机其他用户" | ✅（他人无法解密） | ⚠️ 若文件 ACL 失误则失守 |
| 依赖口令 | ✅（reset 即失效） | ❌ |
| 依赖 profile | ✅ | ❌ |
| 本方案用途 | **主密钥** | **仅 escrow（恢复）**，DACL 只有 `SY` |
| 结论 | 日常运行 | 只做恢复，不参与日常签名 |

> 不声称 C3 "弱于"C2 就不可用：C3 的读取面被 ACL 压到 `SY`，而 `SY` 已在 T9 之内，**未扩大信任集**。

### 10.11 密钥轮换的先后与失效

| 步 | 动作 | 失效顺序 |
|---|---|---|
| 1 | 生成新 `key_id`，写 `key-<new>.dpapi` + escrow | 旧 key 仍有效 |
| 2 | keyring 追加新 key，`state: active`；旧 key 置 `state: retiring` | 新旧并存 |
| 3 | 服务开始用**新 key** 签；验签**同时接受**新旧（窗口期内） | — |
| 4 | 窗口期结束（可配置，默认 7 天）→ 旧 key 置 `retired`：**只验不签** | 旧 key 停止签发 |
| 5 | 证据保留期结束 → 旧 key 可删（需管理员显式命令） | 删除后旧证据 `mac_unknown_key` |

> 不允许"先删旧 key 再发新 key"：那会让窗口期内的证据无法验签。

### 10.12 安装期凭据自检（替代"事后猜"）

| 步 | 动作 | 失败 |
|---|---|---|
| 1 | 安装器生成临时随机密钥 → 以 SVC 上下文运行 **一次性 helper**（`CreateProcessAsUser` 或计划任务） | 退出码 75 |
| 2 | helper `LoadUserProfile` → `CryptProtectData` → 写临时 blob → `CryptUnprotectData` → 比对 | 退出码 75 |
| 3 | helper 对固定测试向量算 HMAC-SHA256 并与安装器独立计算值比对 | 退出码 75 |
| 4 | helper 退出、删临时 blob、`UnloadUserProfile` | — |
| 5 | 全部通过 → 才继续安装 | — |

> 自检通过 = "在本机的这套账户/profile/DPAPI 组合下，SVC 确实能加解密与算 MAC"。**这不等于**"设计已被验证"，只是把不可靠的 Windows 行为在安装期变成可观测的失败。

---

## 11. A 证据保护（沿用 V3.8，边界不变）

表与 6 个触发器与 V3.8 完全一致（`ai_app_evidence_seal` + 证据表 4 + seal 表 2），此处不重复贴 DDL，仅复述边界。

| 事实 | 结论 |
|---|---|
| 写者对 `trading.db` 有 DML 权限 | 触发器对 DML 是硬约束（`RAISE(ABORT)`） |
| 写者对同库有 **DDL** 权限（`DROP TRIGGER` 不触发任何触发器） | SQLite 无法自保 → **R-26 保持 `OPEN`** |
| seal 的真实价值 | **不**是"应用侧不可篡改"，而是"篡改留结构缺口 + 真伪最终由 signer 重算 MAC 判定" |
| 最终边界 | `auth.db`（signer 独占写）+ 锚定链 |

> **R-26 不得被删除、弱化或改写成"已解决"。** 任何声称"seal 可抵御拥有 `trading.db` DDL 权限的攻击者"的表述都是错的。

---

## 12. 热日志与崩溃残留（沿用）

| 场景 | 策略 |
|---|---|
| 启动发现 `auth.db-journal` | 服务以读写打开让 SQLite 回滚；**不**给应用写权 |
| 应用 `mode=ro` 遇热 journal | 返回 `SQLITE_BUSY` / `SQLITE_READONLY_ROLLBACK`；**禁止**重试为读写（CE-140） |
| 出现 `-wal` / `-shm` | 本方案 `journal_mode=DELETE`，出现即拒绝启动（退出码 78） |
| 目录存在未知文件 | 记 `logs\anomaly.log`，不删除（SVC 在 L3 无 `FILE_DELETE_CHILD`，也删不掉他人文件） |

---

## 13. allowlist 规范化（沿用 N-1…N-12）

规则与 V3.8 一致，不重复。仅补一条与 §5 的联动：

| # | 规则 |
|---|---|
| N-13 | allowlist 文件位于 `signer\`（L2），由 SVC 读；应用不可读、不可写；文件路径同样由 §5.3 常量构造，不来自配置 |

---

## 14. 升级、迁移、备份（与 §7 合并）

| 步 | 动作 | ACL 相关 |
|---|---|---|
| 1 | 停服务（等 `STOPPED`） | — |
| 2 | 校验新包 Authenticode（§15） | — |
| 3 | 备份 `auth.db` → `backup\` | 写 `.sha256` |
| 4 | `backup\tmp-<ts>\` 建临时库并迁移 | 该目录 DACL **仅 `SY`**，SVC 无权限 |
| 5 | 校验行数/哈希/FK/`integrity_check` | — |
| 6 | 同卷 `MoveFileEx(REPLACE_EXISTING)` | 替换后**重跑 §7.2** 重建 F1 ACL |
| 7 | ACL 重建（§7.2）+ 结构复核（§8） | 失败 → 回滚 ACL，服务不启动 |
| 8 | 启动服务，服务自检 S0–S12 | 任一失败 → 退出码见 §5.7 |

| 失败点 | 回滚 |
|---|---|
| 2 / 4 / 5 | 删临时库，原库未动 |
| 6 | `MoveFileEx` 原子；原库仍在 |
| 7 | 用 §7.2 步 1 导出的 SDDL 还原；服务保持停止 |

备份保留策略不变；**新增**：备份文件的删除只允许 `SYSTEM`（因 L5 的 SVC 掩码为 `0x12019F`，无 `DELETE`）。

---

## 15. 二进制完整性（R-23，不因本轮关闭）

| 层 | 措施 | 状态 |
|---|---|---|
| 签名 | 全部 EXE/DLL/MSI Authenticode + SHA-256 | `[DESIGN]` |
| 启动校验 | `WinVerifyTrust` 校验自身与同目录 DLL | `[WINDOWS_TEST]` |
| 签名者 pin | 编译期写入证书公钥 SHA-256，运行期比对 | `[WINDOWS_TEST]` |
| DLL 搜索 | `SetDefaultDllDirectories(SYSTEM32|APPLICATION_DIR)`、`SetDllDirectoryW(L"")` | `[DESIGN]` |
| 反降级 | `HKLM\SOFTWARE\FosunAI\MinBuild` + 启动校验自身 ≥ min_build | `[DESIGN]` |

> **R-23 在 T9 下仍 `OPEN`**：管理员可替换二进制。本轮修订**不改变**该结论。

---

## 16. R-21 与 R-17（不变）

| 风险 | 结论 |
|---|---|
| R-21 会话最终态 | 触发器收紧为"出生即 `created`"、证据插入要求 `signed`；"插入后未置 committed"仍靠代码 → **`OPEN`（P2）**，保留服务端校验 + `v_session_gaps` |
| R-17 全局序号 | **保持 `OPEN`**，不对 SQLite 的 read-your-writes / 触发器子查询可见性做任何已验证声明 |

---

## 17. 结构与对象清单（计数可复算）

| 库 | 表 | 触发器 | 视图 |
|---|---|---|---|
| `auth.db` | 7 | 20 | 5 |
| `trading.db` | 4 | 9 | 2 |
| **合计** | **11** | **29** | **7** |

**复算式（人工可验）**

```
表      ：V3.7 10 + V3.9 新增 1（ai_app_evidence_seal）                     = 11
触发器  ：V3.7 23 + V3.9 新增 6（app_ev 4 + app_seal 2）                    = 29
          其中 trg_session_born_created / trg_ev_session_ins 为"重定义"，不增计数
视图    ：V3.7 6 + V3.9 新增 1（v_app_evidence_gaps）                        = 7
trading.db 触发器 = 3（dec_* 继承）+ 4（app_ev）+ 2（app_seal）              = 9
auth.db   触发器 = 29 − 9                                                   = 20
```

| 项 | 本文件内联 | V3.8 内联 | V3.7 内联 | 合计 |
|---|---|---|---|---|
| 表 | **0** | 1 | 10 | 11 |
| 触发器 | **0** | 8（含 2 重定义） | 23 | 29 |
| 视图 | **0** | 1 | 6 | 7 |

> 本文件 §11 明确"不重复贴 DDL"，因此 **V3.9 内联对象为 0**。上表合计来自 `V3.7 内联 + V3.8 净新增`，不是本文件的 grep 结果——这是刻意的，避免"文字声明与正文 DDL 数量对不上"。

> **V3.8 曾有 §16 与 §21 计数冲突（26 vs 实际 29）**，根因是把 4 个 app_ev 触发器写成 3 个。V3.9 以"新增 + 继承 − 重定义"三段式给出算式，任何一段变动都必须同步三处（§17 / §21 / §19）。

---

## 18. 反例与正例（增量）

### 18.1 新增反例（**28 条**，CE-113…CE-140）

| # | 域 | 攻击/错误 | 预期 | 拦截层 | 状态 |
|---|---|---|---|---|---|
| CE-113 | 安装 | 用户预建 `FosunAI\` 并自授 `FA` | 中止/夺属主 | §7.3 | `[WINDOWS_TEST]` |
| CE-114 | 路径 | L1 是 junction 指向 `C:\Users\Public\x` | 拒 | §5.9 | `[WINDOWS_TEST]` |
| CE-115 | 路径 | `authdb\` 是 junction | 拒 | §5.7 S6/S7 | `[WINDOWS_TEST]` |
| CE-116 | 路径 | `auth.db` 是硬链接（`Links=2`） | 拒 | §5.8 | `[WINDOWS_TEST]` |
| CE-117 | 路径 | 最终路径返回 UNC | 拒 | §5.6 | `[WINDOWS_TEST]` |
| CE-118 | 路径 | 最终路径含 8.3 短名 | 拒 | §5.4/§5.5 | `[WINDOWS_TEST]` |
| CE-119 | 路径 | 组件数不符（多一级） | 拒 | §5.5 | `[WINDOWS_TEST]` |
| CE-120 | 路径 | `GetFinalPathNameByHandleW` 返回 0 | 拒 | §5.6 | `[WINDOWS_TEST]` |
| CE-121 | 路径 | 前缀比较被 `authdb-evil\auth.db` 骗过 | 拒（按组件比较） | §5.5 | `[DESIGN]` |
| CE-122 | 路径 | `auth.db.` / `AUTH.DB` 变体 | 拒 | §5.4 | `[DESIGN]` |
| CE-123 | 竞态 | S9 与 S11 之间替换文件 | 拒（身份比对） | §5.7 S11 | `[WINDOWS_TEST]` |
| CE-124 | 竞态 | 运行期 rename `authdb` | 失败（guard handle） | §5.7 S12 | `[WINDOWS_TEST]` |
| CE-125 | ACL | `/grant:r` 后仍残留 `BU:F` 显式 ACE | 拒 | §7.3 | `[WINDOWS_TEST]` |
| CE-126 | ACL | 残留显式 `DENY` 致服务读不到 | 拒（视为 UNKNOWN） | §8.3 | `[WINDOWS_TEST]` |
| CE-127 | ACL | 属主是 Users，`/grant:r` 成功但属主仍可改回 | 拒（先夺属主） | §7.4 | `[WINDOWS_TEST]` |
| CE-128 | 安装 | 中断在 L3 与 L4 之间（半套 ACL） | 服务不启动 | §7.5/§7.6 | `[DESIGN]` |
| CE-129 | ACL | L5 给 SVC `M` → SVC 删备份 | 拒（`0x12019F`） | §6.2 | `[DESIGN]` |
| CE-130 | ACL | escrow 文件 ACL 错配致 SVC 可读 | 拒（仅 `SY`） | §6.2 F4 | `[WINDOWS_TEST]` |
| CE-131 | 凭据 | 未 `LoadUserProfile` → DPAPI 失败 → 静默降级 machine scope | 拒（退出码 75） | §10.7 | `[WINDOWS_TEST]` |
| CE-132 | 凭据 | 口令 reset 后无 escrow → 密钥永久丢失 | 拒启动 + R-27 | §10.3 | `[WINDOWS_TEST]` |
| CE-133 | 凭据 | 先改 SCM 后改账户 → 服务无法登录（1069） | 拒（顺序固定） | §10.3 | `[WINDOWS_TEST]` |
| CE-134 | 凭据 | 账户重建（SID 变）但 keyring 仍旧 `owner_sid` | 拒（V-6） | §8/§10.9 | `[DESIGN]` |
| CE-135 | ACL | DACL 含 `CREATOR OWNER` | 拒 | §8.3 | `[WINDOWS_TEST]` |
| CE-136 | ACL | ACE 继承标志被改为 `IO`-only | 拒（逐位比对） | §7.8 | `[WINDOWS_TEST]` |
| CE-137 | 设计 | 误以为 `0x1301BF` 含 `FILE_DELETE_CHILD` → 以为能删子项 | 纠正（不含） | §9.3 | `[DESIGN]` |
| CE-138 | ACL | 给 SVC `FA` 于 L2 → SVC 改 DACL 放行 Users | 拒（`M`） | §6.2 | `[DESIGN]` |
| CE-139 | 路径 | `FOLDERID_ProgramData` 返回非系统盘/网络盘 | 拒（UNC/Volume 检查） | §5.6 | `[WINDOWS_TEST]` |
| CE-140 | 运行时 | 应用 `mode=ro` 遇热 journal 后重试为读写 | 拒绝（不提权） | §12 | `[DESIGN]` |

### 18.2 新增正例（**15 条**，PS-50…PS-64）

| # | 场景 | 预期 | 状态 |
|---|---|---|---|
| PS-50 | 全新安装：目录从创建即带正确 SDDL，无"先松后紧"窗口 | 通过 | `[WINDOWS_TEST]` |
| PS-51 | 升级：预扫 0 个 UNKNOWN，直接复核通过 | 通过 | `[WINDOWS_TEST]` |
| PS-52 | 升级：发现 1 个 UNKNOWN，备份 SDDL → 整份替换 → 复核通过 | 通过 | `[WINDOWS_TEST]` |
| PS-53 | 属主是 Users，夺属主后重建成功 | 通过 | `[WINDOWS_TEST]` |
| PS-54 | L1–L5 全链结构比对通过，服务启动 | 通过 | `[WINDOWS_TEST]` |
| PS-55 | `auth.db` 非 reparse、`Links=1`、路径 5 段全等 | 通过 | `[WINDOWS_TEST]` |
| PS-56 | 运行 24h 周期复核 file id 不变 | 通过 | `[WINDOWS_TEST]` |
| PS-57 | 清除旧显式 ACE 后 `BU` 仅剩 `0x1200A9` | 通过 | `[WINDOWS_TEST]` |
| PS-58 | SVC 在 L5 只能新建、不能删；删除由 SYSTEM 执行 | 通过 | `[DESIGN]` |
| PS-59 | 口令 change 轮换 → blob 重新保护 → `key_id` 不变 | 通过 | `[WINDOWS_TEST]` |
| PS-60 | 口令 reset → escrow 恢复 → `key_id` 不变 | 通过 | `[WINDOWS_TEST]` |
| PS-61 | `LoadUserProfile` 成功 → DPAPI 解开 | 通过 | `[WINDOWS_TEST]` |
| PS-62 | 安装期凭据自检（SVC 上下文解封 + 固定向量 MAC）通过 | 通过 | `[WINDOWS_TEST]` |
| PS-63 | `backup\tmp-<ts>\` 仅 `SY` 可访问，SVC 被拒 | 通过 | `[WINDOWS_TEST]` |
| PS-64 | 回滚：ACL 还原为备份 SDDL，服务保持停止 | 通过 | `[WINDOWS_TEST]` |

**累计**：反例 112 + 28 = **140**；正例 49 + 15 = **64**。

---

## 19. 风险与修订项状态总表

### 19.1 修订项（4 标签）

| 修订项 | 设计 | 静态审查 | Windows | SQLite |
|---|---|---|---|---|
| 根路径唯一权威（§5.2） | ✅ | `[STATIC_REVIEW]` | `[WINDOWS_TEST]` W-13 | — |
| 相对组件常量（§5.3） | ✅ | `[STATIC_REVIEW]` | — | — |
| 规范化规则（§5.4） | ✅ | `[STATIC_REVIEW]` | — | — |
| 组件边界比较（§5.5） | ✅ | `[STATIC_REVIEW]` | W-14 | — |
| 13 步状态机（§5.7） | ✅ | `[STATIC_REVIEW]` | W-15 | — |
| 文件身份（§5.8） | ✅ | `[STATIC_REVIEW]` | W-16 | — |
| 目录链 ACL（§6） | ✅ | `[STATIC_REVIEW]` | W-17 | — |
| 安装/升级/重建/回滚（§7） | ✅ | `[STATIC_REVIEW]` | W-18 | — |
| DACL 结构校验（§8） | ✅ | `[STATIC_REVIEW]` | W-19 | — |
| 掩码核算（§9） | ✅ | `[STATIC_REVIEW]` | — | — |
| 凭据生命周期（§10） | ✅ | `[STATIC_REVIEW]` | W-20…W-23 | — |
| A 证据 seal（§11） | ✅ | `[STATIC_REVIEW]` | — | `[SQLITE_PROBE]` S-5/S-6/S-13…S-15 |

### 19.2 风险状态（**11 项活跃**，不得视为已解决）

```
R-17  SQLite 全局序号语义                 OPEN
R-19  Windows ACL 未实机验证              OPEN
R-21  会话最终态 DB 不强制                OPEN（P2）
R-22  auth.db 路径权威                    设计修订完成，待独立审查 + 实机验证
R-23  signer 二进制完整性                 设计完成，T9 下 OPEN
R-24  父目录 ACL                          设计修订完成，待实机验证
R-25  跨决策证据绑定                      设计完成，保留 signer 端 V-1…V-7
R-26  trading.db 的 DDL 拆解 seal          OPEN（不得弱化）
R-27  DPAPI user scope 与口令 reset 耦合    OPEN（escrow 缓解，不消除）
R-28  管理员 SeRestorePrivilege 强制替换    OPEN（T9，不声称解决）
R-29  安装器中断导致半套 ACL               部分（回滚流程 + fail-closed，窗口仍在）
```

---

## 20. 待验证清单

### 20.1 Windows（`[WINDOWS_TEST]`）

| # | 项 |
|---|---|
| W-13 | `SHGetKnownFolderPath(FOLDERID_ProgramData)` 在非 C: 系统盘上的返回值形式 |
| W-14 | `GetFinalPathNameByHandleW` 对长名/短名/UNC/Volume 的实际返回 |
| W-15 | 自顶向下 pin + `FILE_FLAG_OPEN_REPARSE_POINT` 对 junction 的识别与阻断 |
| W-16 | `nNumberOfLinks` 与 `dwVolumeSerialNumber` 的实测值 |
| W-17 | `SetNamedSecurityInfoW` 整份替换后 `SE_DACL_PROTECTED` 是否置位 |
| W-18 | 安装器以 SYSTEM 夺属主 + 重建在"用户预建目录"上的实际行为 |
| W-19 | `GetSecurityInfo(handle)` 在已 pin 句柄上的可用性 |
| W-20 | SCM 是否将服务口令存为 `_SC_<服务名>` LSA secret（命名确认） |
| W-21 | `CreateProfile` + `LoadUserProfile` 对 `%APPDATA%\Microsoft\Protect` 的实际效果 |
| W-22 | 服务在无加载 profile 时 `CryptUnprotectData(CurrentUser)` 的失败码 |
| W-23 | 口令 reset 后旧 DPAPI blob 的失败码；escrow 恢复是否成功 |
| W-24 | `0x12019F` 下 SVC 能否建文件但不能删（`icacls` 语义确认） |
| W-25 | L2 无 `BA` ACE 时管理员夺属主是否成功（审计事件是否产生） |
| W-26 | guard handle 对 rename 的阻断（V3.8 W-7 复测） |
| W-27 | `MoveFileEx(REPLACE_EXISTING)` 后 ACL 是否被继承覆盖（需重跑 §7.2） |
| W-28 | 安装期凭据自检在干净机器上的通过率与失败信息 |

> V3.8 的 W-1…W-12 继续有效，未作废。

### 20.2 SQLite（`[SQLITE_PROBE]`，本轮**不执行**）

S-1…S-15 沿用 V3.8（含 S-13/S-14/S-15：seal 表删除/更新被拒、两种情形下 `INSERT OR REPLACE` 终态）。V3.9 **未新增** SQL 层面设计，故不新增探针。

---

## 21. V3.8 → V3.9 差异映射

| V3.8 位置 | V3.8 内容 | V3.9 处理 | 章节 |
|---|---|---|---|
| §5.1 | 硬编码 `C:\ProgramData\...` + 注释"用 API 取根" | 常量降级为**相对组件**；根唯一起源于 API | §5.2/§5.3 |
| §5.3 | 标题"8 步"，正文 S0–S8（9 步） | 重写为 S0–S12（**13 步**），补身份/路径/SQLite 复核 | §5.7 |
| §5.3 S6 | "逐字符比较" | 改**组件边界比较** + 返回处理 | §5.5/§5.6 |
| §5.4 | 只谈 guard handle | 补祖先链 pin 顺序、文件身份、运行期复核、不声称 TOCTOU 已解 | §5.7–§5.10 |
| §6.2 | L2 `SVC:FA`，L5 `SVC:M`，L2 `BA:RX` | L2→`M`，L5→`0x12019F`，L2 无 `BA` | §6.2 |
| §6.2 注 | `0x1301BF` "含 `FILE_DELETE_CHILD`" | **纠错**：不含 | §9.3 |
| §6.4 | `$SVC:(OI)(CI)M` | 全部 `${SVC}` | §6.4 |
| §6.4 | 用 `/inheritance:r`+`/grant:r` 当清理 | 明确其真实语义 + 整份替换原语 | §6.5/§7.2 |
| 无 | 无安装/升级/重建/回滚 | 新增 §7 全章 | §7 |
| §6.5 | 文本化断言 | SID 结构比对 + E/X 集合 + 黑名单 + 失败码 | §8 |
| §9.1/9.2 | 三方案比较 + LSA Secret 存口令 | 三对象辨析；取消自定义 LSA Secret；补轮换/reset/escrow | §10 |
| 无 | 无 profile 生命周期 | `CreateProfile`/`LoadUserProfile`/`UnloadProfile` + 异常矩阵 | §10.7–§10.9 |
| §10.1 | R-26 已披露 | **原样保留**，并加"不得弱化"约束 | §11 |
| §17 | 无 T11 | 新增 T11 预创建劫持 | §4.2 |
| §16/§21 | 计数曾冲突 | 三段式算式 | §17 |

---

## 22. 自检表（**不构成审计**）

| # | 检查 | 结果 |
|---|---|---|
| C-01 | 路径权威是否只剩一个来源 | ✅ §5.2（API） |
| C-02 | 编译期常量是否只固定相对结构 | ✅ §5.3 |
| C-03 | 是否用组件边界比较而非前缀 | ✅ §5.5，CE-121 |
| C-04 | 是否定义 `GetFinalPathNameByHandleW` 失败行为 | ✅ §5.6 |
| C-05 | 是否检查祖先链 | ✅ §5.7 S2–S7 |
| C-06 | 是否承认 TOCTOU 未全解 | ✅ §5.10、R-28 |
| C-07 | 是否把 `/grant:r` 说成清理 | ✅ 明确否定，§6.5 |
| C-08 | 是否给出整份替换原语 | ✅ §7.2 |
| C-09 | 是否定义未知 ACE 处置 | ✅ §7.3 |
| C-10 | 是否有回滚与 fail-closed | ✅ §7.5/§7.6 |
| C-11 | 是否基于句柄校验 | ✅ §8.1 |
| C-12 | 是否区分 DELETE 与 FILE_DELETE_CHILD | ✅ §9.4 |
| C-13 | 掩码是否逐位可复算 | ✅ §9.2–§9.5 |
| C-14 | 是否把"建 profile"等同"DPAPI 可用" | ✅ 明确否定，§10.7 |
| C-15 | 是否定义口令 reset 的后果与恢复 | ✅ §10.3、R-27 |
| C-16 | 是否保留 R-26 且未弱化 | ✅ §11/§19.2 |
| C-17 | 计数是否可复算 | ✅ §17 算式 |
| C-18 | 是否宣称实测 | ✅ 无 |
| C-19 | 是否留下真实密码/密钥 | ✅ 无（仅占位 SID/路径） |
| C-20 | 是否混用状态标签 | ✅ 修订项仅 4 种，风险另表 |

---

## 23. 交付状态

```
V3.9 已生成，待第十轮独立审计；
未执行 SQL / 探针 / 迁移 / ACL 修改 / 服务操作 / 源码修改；
方案未获批；未获实施授权；S0 未启动；
V3.10 未创建。
```

---

> 本文件 SHA256（本行之前全部字节，可用 `head -n -1 <本文件> | sha256sum` 复算）：`0a3a692ead0c79aa948c6e56f0950a4d4244bdc62c2437072160848cdf11f8c2`
