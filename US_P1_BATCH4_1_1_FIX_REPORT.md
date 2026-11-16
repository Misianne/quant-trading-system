# US-P1 Batch 4.1.1 修复报告（定点修复 F-2 / B-1 / A-1）

- **批次**：US-P1 Batch 4.1.1（正式实施）
- **代码库**：`C:\FosunAI-Trading`（星财富单 AI 自动交易系统）
- **安全姿态**：`LIVE_TRADING=false`、`ORDER_EXECUTION_ENABLED=false`、`PAPER_TRADING=true`（终值实测，见 §6）
- **本轮性质**：按已批准的 `US_P1_BATCH4_1_1_REMEDIATION_PLAN.md` 落地代码 + 测试，**不开实盘、不连生产、不发版、不进入 Batch 4.2**

---

## 0. 结论摘要

| 编号 | 问题 | 修复位置 | 状态 | 证据 |
|---|---|---|---|---|
| SR-F2-01 | 敏感字段名变体（`session_id` / `x_api_key` / `apiKey` / 大小写）漏打码 | `log_redaction.py:127` `_is_sensitive_token` | **FIXED** | T01；探针 F2-01 前 `leaked=true` → 后 `false` |
| SR-F2-02 | 转义 JSON（`{\"api_key\": ...}`）漏打码 | `log_redaction.py:173` `_JSON_RE` | **FIXED** | T03（0–4 层转义）；探针 F2-02 |
| SR-F2-03 | `exc_info` 被 Formatter 重渲染，异常文本里的密钥直达输出 | `log_redaction.py:452` `_sanitize_record`（中和 `exc_info`，写 `exc_text`/`safe_exception`） | **FIXED** | T04/T05；探针 F2-03 |
| SR-F2-04 | 自定义 `Handler.handle` 绕过 filter | `log_redaction.py:703` `install_logger_hook`（`Logger.handle` 分发前） | **FIXED** | T06；探针 F2-04 |
| SR-F2-05 | 钩子被第三方覆盖后无法自愈 | `log_redaction.py:677/758`（按**当前属性**判幂等 + `ensure_hooks`） | **FIXED** | T08/T09 |
| SR-F2-07 | 预置 `record.message` 不参与脱敏（死分支） | `log_redaction.py` 的 `_STANDARD_ATTRS` 移除 `message` | **FIXED** | T10 |
| SR-F2-08 | handler 装不上 filter 时静默放行 | `log_redaction.py:564` 三级降级（filter → 包装 `handle` → 禁用 + 告警） | **FIXED** | T11/T12；探针 F2-08 |
| SR-B1-04 | `.env` 与显式 env 不可区分，dotenv 的 API_KEY 压过 Vault，形成混合身份 | `config.py:261` `dotenv_origin`（键名 + **值指纹**）；`fosun_adapter.py:808` 来源模型 / `:860` 同源校验 | **FIXED** | T15/T16/T17；探针 B1-04 前 `configured=true` → 后 `false` |
| SR-B1-05 | 空 Vault 实例复用其它实例注入的凭证 | `fosun_adapter.py:336` `is_operator_value`、`:808` `foreign` 判定、`:261` `acquire_many` 原子登记 | **FIXED** | T18/T19/T20/T23；探针 B1-05 前 `inherited=true, owners=2` → 后 `false, owners=0` |
| SR-A1-02 | `MarketPushAPI` 可被直接构造并连生产地址 | `fosun_adapter.py:436` `_MarketPushGuard`（五方法类级拦截） | **FIXED** | T24–T28；探针 A1-02 前 `bypass=true` → 后 `blocked=true` |

**没有 P0/P1 遗留；仍存在 3 条"不改 SDK 无法 100% 封堵"的边界（§5）。**

---

## 1. P0 现场确认与备份

| 项目 | 结果 |
|---|---|
| 工作目录 | `C:\FosunAI-Trading`（存在 `pyproject.toml` / `src/` / `tests/` / `config/`） |
| 版本控制 | 无 `.git`（无法用 `git diff` 取证，改用 SHA256 快照） |
| 起始时间 | `2026-11-10 12:34:24` |
| 备份目录 | `backups/us_p1_b411_20261110-123437/`（新建，未覆盖任何既有备份） |
| 备份内容 | `src/ai_trader/{log_redaction,runtime,config}.py`、`src/ai_trader/broker/fosun_adapter.py` + `MANIFEST.sha256.json` |
| 备份校验 | 逐个 `sha256(src) == sha256(dst)`，4/4 一致 |
| 基线快照 | `audit_evidence/b411_pre_snapshot_20261110-123437.json`（61 个 `src/`+`tests/` 文件哈希） |
| 开关实测（修复前） | `live_trading=false`、`order_execution_enabled=false`、`paper_trading=true`、`kill_switch=false` |

---

## 2. 修复前 → 修复后：逐层差异

### 2.1 F-2：日志脱敏（五层防线）

修复前的结构是"**在 handler 上加 filter**"这一层，问题在于 Python `logging` 有三条它管不到的路径：
父 logger 的 filter 不处理子 logger 传播上来的记录、`Handler.handle` 被子类重写即失效、`Formatter` 会用 `exc_info` 重新渲染异常文本。

修复后改为**以最终输出文本为保护目标**的五层：

| 层 | 位置 | 作用 | 装不上时 |
|---|---|---|---|
| L0 | `register_secret()` / `_register_secret_values()` | 把已解析的凭证登记为"必须抹掉的字面量"（长度 ≥ 8，上限 64 条 LRU） | 不影响初始化 |
| L1 | `_sanitize_record()`（`RedactionFilter.filter`） | 就地脱敏 `msg+args`、`exc_info`、`exc_text`、`stack_info`、全部 extra | 异常 → 占位符 → 丢弃记录（fail-closed） |
| L2 | `install_logger_hook()`（`Logger.handle`） | **分发前**脱敏，覆盖"自定义 handler" | 装不上 → 健康位为 false 并告警 |
| L3 | `install_format_guard()`（`Handler.format` 兜底） | 对最终字符串再脱敏一次，覆盖"Formatter 自己拼内容" | 同上 |
| L4 | `install_on_handler()` 三级降级 | filter → 包装实例 `handle` → **`setLevel(CRITICAL+10)` 拒绝输出 + stderr 告警** | 最后一级恒可用 |

关键实现点（全部有单测）：

1. **标识符归一化匹配**：`_normalize_key` 去非字母数字 + 小写，`_SENSITIVE_KEYS` 命中，尾部白名单（`count/len/size/ttl/timeout/expire/name/type/mode/enabled/bytes/path/url`）先排除 —— 因此 `token_count=5`、`session_timeout=30` 不会误伤（T02）。
2. **量化上界**：标识符 `{0,63}`、值 `{1,4096}`、2 MiB 输入硬闸（超出整条丢弃 `***[oversized log dropped]***`）、256 KiB 输出截断 —— 避免 O(n²) 回溯（T14）。
3. **`exc_info` 中和**：先 `redact_exception()` 得到已脱敏文本写入 `exc_text` 与 `safe_exception`，再 `exc_info = None`，**切断**任何 Formatter 的重渲染来源（T04 用 SDK 的 `JsonFormatter` 验证 `exception` 字段消失）。
4. **`message` 参与脱敏**：`_STANDARD_ATTRS` 移除 `message`（它是 Formatter 的缓存字段，可被任意 handler 预先写入）。
5. **转义 JSON**：`_JSON_RE` 支持单/双引号 + 0–4 层 `json.dumps` 嵌套（反斜杠 0/1/3/7/15），值内**禁止**吞掉转义引号，否则会把后续业务字段一起抹掉（这是本轮实测发现并修掉的次生缺陷）。
6. **幂等与自愈**：`_wrap_method` 打 `_aitrader_redaction` 标 + 保存 `_aitrader_original`；判据是"**当前属性**是否仍是我们的包装"，因此第三方覆盖后 `ensure_hooks()` 能重装且不叠加（T08/T09）。

### 2.2 B-1：凭据来源模型

修复前：`env` 与 `dotenv` 在 `os.environ` 里无法区分 → `.env` 里的 `FSOPENAPI_API_KEY` 会当作"显式配置"，压过 Vault；同时 `elif injected_value` 让无 Vault 实例静默复用别人注入的身份。

修复后：

1. **来源判定**（`_classify_source`，优先级严格递减）
   `explicit_env` > `vault` > `dotenv` > `injected`（仅**本实例**自己注入）> `missing`。
   - `dotenv` 与 `explicit_env` 用 `config.dotenv_origin(name, value)` 区分：键名由本进程 `load_dotenv` 写入，**且当前值指纹与当时一致**。只按键名判断会把"后来被 export 覆盖"误判成 dotenv。
   - 由**其它实例**注入的值（`is_ours` 为真但不是自己、也不是运维原有）→ `foreign` → 不采纳。
   - 运维先 export、我们只是又写了一遍同值（`preexisting == 当前值`）→ `is_operator_value` 为真 → 仍算 `explicit_env`，不误杀（T20）。
2. **同源校验**（`_validate_source_model`）
   三要素出现 ≥2 种来源 → `mixed` → 抛 `CredentialSourceMix`（code `CREDENTIAL_SOURCE_MIX`）；任一缺失 → `incomplete` → 抛 `CredentialIncomplete`（code `CREDENTIAL_INCOMPLETE`）。两者都在**构造 SDK 之前**，`configured=False` + `blocked_reason` 写明 code，面板可读。
3. **原子登记**（`acquire_many`）
   三要素在**一次持锁**内先校验全部指纹再统一写入：要么整套归同一 owner，要么一个字节都不写（等价回滚，无补偿逻辑）。并发下不会出现"A 的 PUB + B 的 PRIV"（T23）。
4. **释放**（`release`，沿用并加固）
   只减自己的引用；归零时才动环境，且**先核对当前值仍是我们写入的那个**；调用前有值则恢复、无值才删除。重复调用幂等（T22）。
5. **可审计输出**（`health()`）
   `credential_origin`（历史字段，键为环境变量名）、`credential_sources`（`PUB_KEY/PRIV_KEY/API_KEY` → 来源）、`credential_mixed`、`credential_validation{status,detail,complete,mixed,fingerprints}`。指纹是 `sha256(prefix|name|value)` 的**前 12 位十六进制**，不含可逆信息。
6. **新增 `close()`**：释放凭证引用 + 释放类级行情推送拦截，幂等。

### 2.3 A-1：行情推送入口

修复前只把 `client.market_push` 换成哨兵 —— 只保护"当前这个 client"，另造一个 `MarketPushAPI(client)` 就能绕过并连 `wss://openapi-push.fosunxcz.com`。

修复后改为**类级 + 实例级**双层：

- **类级**（`_MarketPushGuard`）：替换 `connect / subscribe / unsubscribe / close / send_heartbeat` 五个方法。`subscribeQuote/subscribeMin/...` 全是 `subscribe` 的薄包装，拦 5 个即封死整族入口。
- **不拦 `__init__`**：`SDKClient.__init__` 会执行 `self.market_push = MarketPushAPI(self)`（`sdk_src/fsopenapi/__init__.py:40`），拦构造等于连 `SDKClient(...)` 都建不出来。
- **同步抛**：拦截器是普通函数，`obj.connect()` 调用即抛 `FEED_AUTH_BYPASS`；不用 `async def`，避免"创建了协程但没人 await"的静默旁路。
- **fail-closed**：方法缺失 / 类不可写 / 回读校验不一致 → 全量回滚 + 返回 False → 调用方拒绝完成初始化（`test_install_failure_is_fail_closed`）。
- **引用计数与精确恢复**：多实例各自登记 owner，最后一个释放者才恢复；只恢复"仍是我们的包装"的方法，第三方改过的跳过并记入 `tampered`（T27/T28）。
- **可观测**：`health()["market_push_guard"]`（bool）+ `market_push_guard_detail{installed,class,methods,owners,tampered}`。

---

## 3. 修改清单（前后 SHA256 / 增删行）

| 文件 | 行数（前→后） | 增/删 | SHA256（前） | SHA256（后） |
|---|---|---|---|---|
| `src/ai_trader/log_redaction.py` | 543 → 767 | +354 / -130 | `a4733d97f20de17bea1dc7c3d3b656ee2b68bf15d26a8a25482bbc52af1820f0` | `bef31deaafdd7c149a34c876022100617da07e8851d1fcbbb18cd6006412fb34` |
| `src/ai_trader/runtime.py` | 222 → 238 | +23 / -7 | `769ccec94d74f28138923eca5620ed6cf7ee093bc1a361e6420742f5aff044a7` | `a3ad630ca87df45d332c2dff10bc9b630f61bb7f47cdc269cff543b7739616fd` |
| `src/ai_trader/config.py` | 361 → 411 | +50 / -0 | `72713d4d5b3c5d990fed753a11d840a0d94809c69a13b3a1c1b2c12a1a4cda98` | `3ee1bc0fee23ad4a0b90928cc2303b44ff4779acc93b2cb2b4145c1dc019362d` |
| `src/ai_trader/broker/fosun_adapter.py` | 1433 → 1924 | +594 / -103 | `c399f6b24e46ad10d4e900d69ed9c747a3253f9e552fba7157225403edb73162` | `1cf22d39343fe21bf178a3c1e718f085d59b12bfebf972fa56ea5d1dfa2a10b9` |

新增文件（3 个测试 + 1 个探针）：

| 文件 | 用途 |
|---|---|
| `tests/test_log_redaction_b411.py` | T01–T14（F-2） |
| `tests/test_credential_identity_b411.py` | T15–T23（B-1） |
| `tests/test_market_push_guard_b411.py` | T24–T28 + 2 个补充用例（A-1） |
| `audit_evidence/us_p1_b411_probe.py` | 修复前/后同一脚本的安全探针（证据生成器） |

`runtime.py` 的改动：`setup_logging()` 依次安装四个钩子 → `ensure_hooks()` 自愈 → 若有 handler 被禁用则 `logger.error` 告警；`Runtime.status()` 增加 `"log_redaction": redaction_health()`。

`config.py` 的改动：新增 `_DOTENV_FP`（键名 → 值 SHA256，不存明文）、`dotenv_origin()`；`load_dotenv()` 在写入时记录指纹；`_reset_dotenv_keys()` 同时清两张表。

---

## 4. 未修改文件与保护边界核对

以 P0 的 61 文件快照为基线重新计算 SHA256：

- **变更文件：4 个**（上表），**其余 57 个 `src/`+`tests/` 文件哈希全部一致**。
- `tests/helpers.py`：在快照内，哈希未变 → 未修改。
- `.env`（`c914b6c3b58158a9…`，mtime 2026-11-08 11:57）、`config/config.yaml`（`fe744191bc9fbeea…`，mtime 2026-11-09 12:38）、`sdk_src/fsopenapi/__init__.py`（`66dc4bb682731bac…`，mtime 2026-11-08 11:36）、`requirements.txt`（`9138fc16a7de48d9…`，mtime 2026-11-09 17:11）：mtime 全部早于本轮开工时间（2026-11-10 12:34），**未修改**。
- 未新增/删除任何依赖；未触碰数据库、密钥、备份目录以外的新增文件；未执行 `git reset/checkout/clean`（仓库无 `.git`）。

---

## 5. 未覆盖风险与已知边界（如实记录）

| # | 边界 | 为什么无法覆盖 | 现状态 |
|---|---|---|---|
| 1 | 第三方 handler **同时**重写 `handle()` 与 `format()`，且记录**不经过任何 logger** 直接被驱动 | `Logger.handle`（L2）与 `Handler.format`（L3）都被子类重写绕过，记录从未进入 logging 分发链 | 写成显式用例 T07（断言"确实泄露"）；项目内 handler 全由 `setup_logging` 创建，实际不可达 |
| 2 | `json.dumps` 嵌套 ≥ 5 层（反斜杠 ≥ 31 个） | `_JSON_RE` 上界取 15（覆盖到 4 层），再放宽会让回溯成本上升、且无真实场景 | 记录在案；此类文本属人为构造 |
| 3 | 记录在进入 `logging` **之前**就被写进文件/网络（如 SDK 自己的 `print`、自建 logger） | 不改 SDK 源码的前提下无法接管 | 本轮已封堵 SDK 的 `logger.*`；`print` 类输出属 SDK 行为，另行审计 |
| 4 | `api_key` 不写入进程环境（按设计） | SDK 用构造参数接收，写进环境只会把密钥扩散到子进程，且让两个实例仅因 api_key 不同就冲突 | 冲突检测以 `PUB/PRIV` 为判据；跨身份串用在 `acquire_many` 阶段即被拒绝 |
| 5 | `credential_validation.fingerprints` 暴露 48 bit 摘要 | 需求要求"脱敏指纹"用于人工比对 | 只对高熵密钥安全；低熵口令场景需人工判断 |

---

## 6. 安全边界声明（终值实测）

| 项 | 结果 |
|---|---|
| `live_trading` | **false** |
| `order_execution_enabled` | **false** |
| `paper_trading` | **true** |
| `kill_switch` | false |
| `can_connect_broker()` | **false** |
| `can_send_real_order()` | **false** |
| 是否触达生产网络 | **否**（全部 SDK 调用被替换为本地假实现；`websockets` 未被调用） |
| 是否创建真实 Session | **否**（`get_valid_session` 计数 0） |
| 是否执行真实订单 | **否** |
| 是否访问 VPS `203.0.113.10` | **否** |
| 是否修改 SDK 源码 | **否**（`sdk_src` 哈希未变） |
| 是否修改 `.env` / `config.yaml` | **否**（mtime 早于开工） |
| 是否在输出中出现真实密钥 | **否**（全部为 `*-FAKE-*` 伪造串） |

---

## 7. 与批准计划的偏离（逐条说明，不隐瞒）

1. **T17 的 `policy=warn` 未实现**：计划里写了"`policy=warn` 时 `credential_mixed=true`"。本轮判断**不应提供任何可绕过同源校验的开关**（与"禁止跨来源混合"直接冲突），因此只暴露 `credential_mixed` / `credential_validation.mixed` 字段，不提供 warn 降级。T17 按"部分通过"记。
2. **T20 重新定义**：计划原意是"显式开关开启且三要素齐全才复用注入凭证"。该开关会让 SR-B1-05 重新打开，**不实现**。改为验证"运维 export 的同值不会被误判为注入"（`is_operator_value`），这条是真实需求。
3. **新增 `acquire_many`**（计划未列）：实现"三要素作为一个完整集合"时发现逐个 `acquire` 在并发下会写出半套身份，故补原子批量登记。
4. **`_JSON_RE` 扩到单引号 + 4 层转义**（计划只要求 1–2 层）：实现 1–2 层时发现"值里吞掉转义引号会把后续字段一起抹掉"，顺带修正；单引号是 Python `dict` repr 的常见形态。
5. **`_SDK_ENV_MAP` 保持两项**（计划曾考虑把 `FSOPENAPI_API_KEY` 纳入注入清单）：实测 SDK 用构造参数接收 api_key，写入环境无收益且有扩散风险；三要素完整性由 `_SDK_CRED_FIELDS` 单独负责。

---

## 8. 交付物

| 文件 | 说明 |
|---|---|
| `US_P1_BATCH4_1_1_FIX_REPORT.md` | 本文件 |
| `US_P1_BATCH4_1_1_TEST_REPORT.md` | 测试报告（T01–T29 实测） |
| `US_P1_BATCH4_1_1_FILE_MANIFEST.sha256` | 交付物 + 源码 + 测试 + 证据的哈希清单 |
| `audit_evidence/us_p1_b41_1_test_results.json` | 测试结果（命令、退出码、逐用例状态） |
| `audit_evidence/us_p1_b41_1_pytest.log` | 定点测试详细日志（`-v`） |
| `audit_evidence/us_p1_b41_1_full.log` | 全量回归日志 |
| `audit_evidence/us_p1_b41_1_security_probes.json` | 修复前/后对照探针结果 |
| `audit_evidence/us_p1_b411_probe.py` | 探针脚本（可复跑） |
| `audit_evidence/b411_pre_snapshot_20261110-123437.json` | P0 基线快照（61 文件） |

**本批次到此停止**：不开始 Batch 4.2、不发版、不开实盘、不改生产配置。
