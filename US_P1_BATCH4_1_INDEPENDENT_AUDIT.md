# US-P1 Batch 4.1 独立只读审计报告

- 审计时间：2026-11-09 21:37–21:52（GMT+8）
- 工作目录：`C:\FosunAI-Trading`
- 审计对象：Batch 4.1（B-1 凭据隔离 / A-1 SDK 旁路 / F-2 统一日志脱敏）
- 审计方式：只读；未修改源码、测试、配置、`.env`、SDK、数据库；未连接生产 API、未启动真实行情、未下单
- 审计产出：本报告 + `audit_evidence/us_p1_b41_independent_audit.json`

> **独立性声明（必读）**
> 本轮审计由**同一执行代理**在只读约束下完成，而 F-2 的代码正是该代理在上一轮实现的。
> 因此这是一次**自查式审计**，不等同于第三方独立审计；结论对“实现思路的一致性”较强，
> 对“实现者盲区”的独立性有限。建议人工复核本报告列出的 P2 项，尤其是 F2-03 / F2-04。

---

## 0. 总评

| 项目 | 结论 | 摘要 |
|---|---|---|
| B-1 凭据身份与隔离 | **PARTIAL** | 核心冲突检测在代码与手工探针中成立；但双 Vault 场景无自动化测试，成功实例无释放生命周期，无 Vault 实例会静默继承他实例身份 |
| A-1 SDK 旁路 | **PARTIAL** | provider 门禁/路由校验有测试；`market_push` 哨兵代码正确且手工探针通过，但**没有任何自动化测试**；直接 import SDK 类仍可绕过 |
| F-2 日志脱敏 | **PARTIAL** | handler 边界、fail-closed、限额、幂等均已落地；但存在规则缺口、`exc_info` 重渲染绕过、自定义 handler 绕过，测试覆盖被报告夸大 |
| C-1 RID-only ACK | **PASS** | 代码未改；`ACK_UNVERIFIED` 终态与测试均成立 |
| D-1 正常关闭 | **PASS** | 代码未改；正常关闭不计 errors 的测试成立 |
| 测试证据 | **PARTIAL** | 41/41、517/1 均独立复跑通过；但全量无 JUnit、部分声明与实物不符、fake SDK 限制 |
| 交易安全边界 | **PASS** | `false / false / true` 未变；证据日志无生产域名 |

**最终总评：PARTIAL。**

**P0/P1：无。** 未发现当前生产调用图（`setup_logging` + `build_runtime` + 单 `FosunBroker` + `MarketFeed`）
中可直接触发的凭证泄露或认证绕过。**P2：12 项**，见 §6；其中 F2-03、F2-04、F2-01、B1-02、B1-03
应在 Batch 4.2 开始前修复或书面接受。

**是否允许进入下一阶段：允许（有条件）。** 无 P0/P1，不触发“必须阻止”；但 P2 项未处置前，
不得把 F-2 描述为“统一、无死角”的脱敏边界。

---

## 1. 现场与备份核验

1. 工作目录为 `C:\FosunAI-Trading`；**不是 Git 仓库**（`git status` 返回 `not a git repository`），
   因此变更对比以备份目录为准。
2. 备份 `backups/us_p1_b41_20261109-203830/` 存在，含：
   - `MANIFEST.sha256.json`（phase=`pre-fix (US-P1 Batch 4.1)`）
   - `src/ai_trader/broker/fosun_adapter.py`（mtime 17:53，4.1 修改前版本）
   - `src/ai_trader/market/feed.py`（18:16）
   - `src/ai_trader/runtime.py`（18:14）
   - `tests/conftest.py`（18:05）
   - `US_P1_BATCH4_FIX_REPORT.md`（18:20）
3. 备份创建于 20:38；adapter 当前 mtime 为 20:47（B-1/A-1 4.1 修改），feed/runtime 为 21:20（F-2）。
   时间线与“备份=修改前”一致。

### 1.1 文件哈希 / 编码 / BOM / EOL

| 文件 | 大小 | SHA256 | EOL | BOM | 相对备份 |
|---|---:|---|---|---|---|
| `src/ai_trader/broker/fosun_adapter.py` | 69860 | `c399f6b2…b73162` | CRLF | 无 | **changed +161/−27**（B-1 指纹注册表、A-1 哨兵、F-2 登记） |
| `src/ai_trader/market/feed.py` | 53540 | `fe073db4…f24a8b` | CRLF | 无 | **changed +13/−75**（仅头部脱敏区，C-1/D-1 未动） |
| `src/ai_trader/runtime.py` | 9847 | `769ccec9…f044a7` | CRLF | 无 | **changed +10/−0**（`setup_logging` 安装） |
| `src/ai_trader/log_redaction.py` | 22619 | `a4733d97…1820f0` | CRLF | 无 | **新增 543 行** |
| `tests/conftest.py` | 7047 | `0e4d26cf…c46050` | LF | 无 | **未变** |
| `tests/test_log_redaction_b41.py` | 11467 | `1166c12c…6c34da` | CRLF | 无 | **新增 304 行 / 18 例** |
| `tests/test_prod_conn_repair_b41.py` | — | — | — | — | **不存在**（用户原文为“如存在”，非缺陷） |
| `US_P1_BATCH4_FIX_REPORT.md` | 21158 | `c09c02ea…4f593` | LF | 无 | 变更（新增 Batch 4.1 取代说明） |
| `US_P1_BATCH4_1_FIX_REPORT.md` | 12429 | `cf580601…c10ed` | **CRLF** | 无 | 新增（199 行；与其它 `.md` 的 LF 不一致） |

**保护边界文件未修改**（mtime 均早于 20:38）：`sdk_src/fsopenapi/client.py`、`.../api/market_push.py`
（2026-11-08 11:36）、`config/config.yaml`（11-09 12:38）、`.env`（11-08 11:57）、
`requirements.txt`（17:11）、`src/ai_trader/secrets.py`（17:10）、`tests/helpers.py`（17:16）。

---

## 2. B-1 凭据身份与隔离

### 2.1 代码路径

- 进程级注册表：`src/ai_trader/broker/fosun_adapter.py:151-281`
  - 指纹 `sha256(prefix|name|value)`：`:191-200`（仅内存，不写日志/异常）
  - `is_ours()`：`:202-213`
  - `acquire()` 引用计数 + 冲突：`:215-237`
  - `release()` 归零清理/恢复：`:239-266`
- 凭证解析优先级：`:474-551`
  - 显式 env → Vault → **本进程注入值** → missing：`:505-532`
  - 复用注入值分支：`:526-529`
- 注入与回滚：`:553-590`（`acquire` 在 `:571`）、`:592-607`
- 初始化失败回滚：`:694-703`（`:698` 调用 `release_credentials`）
- A/B 冲突测试（现有）：`tests/test_prod_conn_repair_b4.py:266-378`

### 2.2 实测（手工探针，不联网、不落盘）

| 场景 | 结果 |
|---|---|
| A(Vault PUB1/PRIV1) 注入后，B(Vault PUB2/PRIV2) | B 解析到 **PUB2**（不是 A 的 PUB1）；`_inject_sdk_env` 抛 `CredentialConflict`；环境仍为 PUB1；owners=1 ✅ |
| A 与 B 同 Vault | 引用计数 2，A 释放后 env 保留，B 释放后清理 ✅（已有测试 `:266-294`） |
| B 部分注入（PUB 相同、PRIV 不同） | 冲突后由 `_init_client` 的回滚路径释放，owners 回到 1 ✅ |
| C **无 Vault** 配置 | 解析到 PUB1 并 `acquire`，owners=2，`configured` 可能为 True ⚠️ |
| 对象丢弃（无 `close`/`__del__`） | owner 永久留在注册表，env 不清理 ⚠️ |

### 2.3 结论：PARTIAL

- **已修复**：审计指出的“B 用自己的 Vault 却被 A 的注入值覆盖”不再发生；双 Vault 冲突 fail-closed；
  归零清理与恢复正确；显式 env 与注入 env 可区分。
- **缺口**：
  - `B1-01 (P2)` 双 Vault 场景**没有自动化测试**（现有测试用同 Vault / 改 env 模拟）。
  - `B1-02 (P2)` 成功实例没有 `close()`/`__del__`；`release_credentials` 唯一调用点是初始化失败
    （`:698`）。对象被丢弃后注册表引用与环境变量不会释放，后续不同凭证实例会持续
    `CredentialConflict` 直到重启。
  - `B1-03 (P2)` 实例自身**无 Vault** 时会复用他实例注入值（`:526-529`），不会 fail-closed。
    单账户 worker 场景可解释，但缺少显式开关/告警，审计问题 2“B 是否会错误读取 A 的凭据”
    在此配置下答案是“会（按设计）”。
  - `B1-04 (P3)` `_inject_sdk_env` 非原子；真实 `_init_client` 已回滚，直接调用者需自行回滚。
  - `B1-05 (P3)` `health()` 重新解析 Vault（`:1384`），凭证轮换后 status 的脱敏键可能与实际 SDK
    使用的键不一致（不泄露、不影响实际使用）。
  - `B1-06 (P3)` `blocked_reason` 拼接原始异常文本（`:699-702`），若 SDK 异常回显凭证会进入
    Web status；该字段不经过 logging 脱敏。
  - `B1-07 (P3)` 并发 acquire/release 无测试；RLock 代码阅读正确，但属 UNVERIFIED。

---

## 3. A-1 SDK 绕过路径

### 3.1 入口与顺序

- `runtime.build_feed_auth_provider()`：`src/ai_trader/runtime.py:95-126`
  - 先调 `feed_auth_ready()`，失败不创建 provider：`:109-116`
  - 对无 `feed_auth_ready` 的 broker 退化为“有 `.client` 即可”：`:109-116`
- `FosunBroker.feed_auth_ready()`：`:404-422`（gate → routing → client → auth_manager）
- `feed_auth_headers()`：`:424-446`（先 `_require()`，再 `get_valid_session()`）
- `_require()`：`:778-793`（gate + routing + client）
- 哨兵：`_MarketPushDisabled` `:284-311`；安装 `:609-634`；调用点 `:688`
- 初始化顺序：`SDKClient` 构造 `:669-675` → 环境可读校验 `:679-683` → 哨兵 `:688` → `configured=True` `:692`

### 3.2 实测

| 检查 | 结果 |
|---|---|
| 哨兵安装 | `install=True`，`client.market_push` 被替换 ✅ |
| `connect/subscribe/unsubscribe/close` | 全部抛 `BrokerError(code=FEED_AUTH_BYPASS)` ✅ |
| `market_push` 只读属性 | `install=False`，`_init_client` 会 fail-closed ✅ |
| `sdk_type=ops` + 个人接口 | `feed_auth_ready()=False`、`_require()` 拒绝；provider 不创建（测试 `b4:185-206`）✅ |
| SDK 初始化是否联网 | `SDKClient.__init__` 仅构造 `SessionManager`（`sdk_src/fsopenapi/client.py:25-57`）；`SessionManager.__init__` 仅本地加载 PEM（`auth.py:18-51`），不取 session ✅（代码阅读） |
| `get_valid_session` 其他调用点 | SDK 内仅 `client._request`（`client.py:101`）与 `market_push.connect`（`market_push.py:43`）；项目内仅 `feed_auth_headers`（已守卫）✅（grep） |

### 3.3 结论：PARTIAL

- 项目当前**没有**其他调用 `MarketPushAPI`/`get_valid_session` 的路径；provider 与业务入口均 fail-closed。
- **缺口**：
  - `A1-01 (P2)` 哨兵**没有自动化测试**（`grep FEED_AUTH_BYPASS tests/` 无结果）；当前只有本次手工探针。
  - `A1-02 (P3)` 哨兵只替换 `client` 实例属性；任何代码 `from fsopenapi import MarketPushAPI`
    直接实例化仍可绕过。项目当前未这样做（`cli.py:253` 仅列举类），因此是“未调用”，不是“无法绕过”。
  - `A1-03 (P3)` `_reconnect()`（`:813-828`）不重新检查 gate/routing；业务入口已过 `_require`，
    但调用途中关闭 gate 后仍可能 `create_session`（TOCTOU）。
  - `A1-04 (P3)` `build_feed_auth_provider` 对无 `feed_auth_ready` 的 broker 仅看 `.client`，
    自定义 Broker 可绕过门禁（当前仅两种 Broker）。
  - `A1-05 (P3)` `_create_session_guarded` 的 `ThreadPoolExecutor.shutdown(wait=True)` 会等待超时线程，
    墙钟超时被抵消（既有 P1-6，非本批引入）。
  - `A1-06 (P3)` `sdk_type=ops` 冲突时 `configured` 仍可能为 True；业务/feed 均拒绝，但 status 易误读。

---

## 4. F-2 统一日志脱敏

### 4.1 代码与安装

- 规则表：`src/ai_trader/log_redaction.py:101-124`
- 敏感键名：`:128-133`
- `redact_text`：`:203-240`（2 MiB 输入上限 `:224`，256 KiB 输出上限 `:238-239`）
- `redact_obj`：`:261-308`（深度 6、单容器 100、节点 1000）
- `_sanitize_record`：`:366-408`（msg/args、exc_text、stack_info、extra）
- fail-closed：`:411-427`；`RedactionFilter.filter`：`:441-447`
- 安装：`install_on_handler` `:464-485`；`install_on_handlers` `:488-513`；`install_global_hook` `:516-543`
- 运行时：`runtime.setup_logging` `:206-222`（`install_on_handlers()` + `install_global_hook()`）
- feed 兼容导出：`market/feed.py:73-98`；登记：`:785`
- 适配器登记：`fosun_adapter.py:318-332`，在 `_resolve_credentials` 返回前 `:550`

### 4.2 实测（真实 handler 输出）

| 检查 | 结果 |
|---|---|
| `ai_trader.market.feed.child`（自身无 filter） | `X-Api-Key: ***`，原值 0 次 ✅ |
| `ai_trader.strategy.engine` | `token: ***` ✅ |
| `websockets.server` DEBUG 握手（对象 args） | 三个凭证均不出现 ✅ |
| traceback（标准 Formatter） | 异常类型保留，凭证打码 ✅ |
| 已存在的 `exc_text` | 二次脱敏 ✅ |
| 嵌套 extra | dict/list/tuple 内敏感键与裸字面量均打码 ✅ |
| setup 后新建 handler | 首次输出前自动补 filter ✅ |
| 具名 logger handler / `lastResort` | 安装探针通过 ✅（无自动化测试） |
| 重复安装 / 重复过滤 | filter 数量 1；输出仅 1 个 `***` ✅ |
| 过滤器内部异常 | 输出 `***[log redaction failed]***`，原文 0 次 ✅ |
| SDK `session_id` extra | `sid=***`（键名打码）✅ |

### 4.3 发现的绕过与缺口

1. **`F2-01 (P2)` 规则缺口**：`session_id=ABCD…`、`sessionId: ABCD…`、`x_api_key=ABCD…`
   均原样输出。原因：通用规则只匹配 `session`（`\b` 在 `session_id` 处不成立），特定规则只写
   `x-api-key`（不接受下划线）。当前 SDK 的 session 走 extra（被键名打码），无现网路径。
2. **`F2-02 (P2)` 转义 JSON**：`{\"api_key\": \"<PROBE_SECRET>\"}` 不匹配（JSON 规则要求 `"` 紧跟键名）。
3. **`F2-03 (P2)` `exc_info` 重渲染绕过**：过滤器只预置 `record.exc_text`，不清空 `exc_info`
   （`:384-392`）。SDK 自带 `JsonFormatter` 在 `sdk_src/fsopenapi/logging_utils.py:131-132`
   直接 `formatException(record.exc_info)`，实测输出 `<PROBE_SECRET>`（`leak: True`）。
   项目 `setup_logging` 用标准 Formatter（读 `exc_text`），当前生产路径不受影响；但 SDK examples
   使用 `JsonFormatter`，运维脚本照抄即泄露。
4. **`F2-04 (P2)` 自定义 handler**：`install_global_hook` 包装的是 `Handler.handle`；
   若唯一 handler 重写 `handle()` 或直接 `emit()`，filter 不执行。实测输出
   `X-Api-Key: <PROBE_KEY>`。项目当前无此类 handler。
5. **`F2-05 (P3)` 死分支**：`message` 在 `_STANDARD_ATTRS`（`:362`），循环在 `:400` 先 `continue`，
   `:405` 的 `elif key == "message"` 不可达；标准 Formatter 每次重算 message，无现网影响。
6. **`F2-06 (P2)` 测试缺口**：没有 `stack_info`、具名 logger handler、`lastResort`、`session_id` extra、
   args-dict、自定义 handler / `JsonFormatter` 绕过的用例。
7. **`F2-07 (P2)` 报告声明不实**：`US_P1_BATCH4_1_FIX_REPORT.md:160` 称“18 例全部读 StringIO
   handler 输出”；实际 `tests/test_log_redaction_b41.py:238-303` 有 6 个纯函数/登记表用例，
   `:117-199` 还有直接调用 filter 的用例，约 11 例走 handler 输出。
8. **`F2-08 (P3)` hook 可被覆盖**：后续对 `logging.Handler.handle` 的赋值可替换钩子；模块重载会
   双重包装（仍幂等但多一层）。
9. **`F2-09 (P3)` 登记表明文驻留**：`_SECRETS` 为模块全局（`:140-153`），按字面量替换所必需；
   无日志、无公开读取，但进程内代码可访问，且无 TTL/清理 API。
10. **`F2-10 (P3)` 会话 ID 注册时序**：SDK `session_create_success` 日志早于
    `register_feed_secrets`（`feed.py:785`）；当前靠 extra 键名打码，若 SDK 改为消息文本则落入 F2-01。
11. **`F2-11 (P3)` 静默降级**：`_register_secret_values` 吞 `ImportError`（`adapter:324-332`）。
12. **`F2-12 (P3)` 结构化 extra 的 CPU 上限非全局**：每串 2 MiB，但 1000 节点 × 2 MiB 仍可能较慢。

### 4.4 结论：PARTIAL

主路径（标准 Formatter + 现有 logger + 现有 handler）已实现 handler 级脱敏，fail-closed 与限额有效；
但“统一/无死角”的表述不成立：至少 4 条可复现绕过（F2-01/02/03/04），其中 F2-03 使用 SDK 自带
`JsonFormatter` 即可触发。无当前生产路径触发，故不升为 P1。

---

## 5. C-1 / D-1 回归保护

- `feed.py` 相对备份的 diff **仅文件头部 13 行新增 / 75 行删除**（脱敏实现替换），状态机与关闭
  分类零改动（`diff -u` 仅 `@@ -58`、`@@ -66` 两个 hunk，均在 100 行内）。
- C-1：
  - `ACK_UNVERIFIED` 作为 `start()` 终态立即返回：`feed.py:330-335`
  - 回执无法核实 → `FeedAckUnverified` → `ACK_UNVERIFIED`：`:675-683`
  - 超时分类：收到过回执但不可核实 → `ACK_UNVERIFIED`：`:955-958`
  - 测试：`tests/test_prod_conn_repair_b4.py:503-520`（RID-only 不订阅）、`:568-584`（不报告 fresh）
- D-1：
  - 正常关闭分支不计 errors、不写 last_error：`feed.py:692-700`
  - 测试：`b4:586-604`（normal close errors=0）、`:606-621`（异常关闭仍计 errors）、`:623-636`（stop 不算错误）

**结论：PASS。** 未发现 C-1/D-1 语义被本轮改动；既有测试仍通过。

---

## 6. 测试证据与局限

### 6.1 报告声明的证据

| 证据 | 内容 | 核验 |
|---|---|---|
| `audit_evidence/us_p1_b41_pytest.log` | `41 passed in 3.89s` | 文件 mtime 21:34:49，晚于最终代码 21:34:35 ✅ |
| `audit_evidence/junit_b41.xml` | `tests=41 failures=0 errors=0 skipped=0` | XML 解析一致，含 18 个 F-2 用例 ✅ |
| `audit_evidence/us_p1_b41_full.log` | `517 passed, 1 skipped in 61.14s` | 文件 mtime 21:36:00，晚于代码 ✅ |
| `US_P1_BATCH4_1_FIX_REPORT.md` | 文件清单 / SHA256 / 结论 | 4 个源文件哈希与实物一致 ✅；范围声明与 adapter 变更不符 ❌（DOC-01） |

### 6.2 审计者独立复跑（只读参数：`-p no:cacheprovider` + `PYTHONDONTWRITEBYTECODE=1`）

```
18 passed in 0.06s
   tests/test_log_redaction_b41.py

41 passed in 3.49s
   tests/test_prod_conn_repair_b4.py 先、tests/test_log_redaction_b41.py 后（顺序交换）

517 passed, 1 skipped, 1 warning in 56.61s   exit code = 0
   全量 tests/
```

- 顺序交换未出现失败，未观察到用例间顺序依赖；但**全局 hook 与登记表仍跨用例残留**（T-01/T-02）。
- 全量 517 = Batch 4 的 499 + 本轮 18，数量自洽。

### 6.3 测试局限

- **fake SDK**：B-1/A-1 的 `configured`、SessionCreate=0 均基于 `tests/test_prod_conn_repair_b4.py:101-139`
  的假 SDK；真实 SDK 端到端 UNVERIFIED（无生产凭证，符合边界）。
- **覆盖声明夸大**：F-2 报告称 18 例全部走 handler 输出，实际约 11 例；`stack_info`、具名 handler、
  `lastResort`、`session_id` extra、args-dict、自定义 handler 绕过均无用例。
- **全量无 JUnit**：517 只能按 stdout 计数，无法逐用例核对。
- **全局 hook 污染**：`install_global_hook` 一旦在 pytest 进程安装不会卸载，后续用例的日志被静默
  脱敏，可能掩盖原始日志回归（T-01）。

---

## 7. 交易安全边界

| 开关 | 值 | 来源 |
|---|---|---|
| `LIVE_TRADING` | `false` | `config/config.yaml:22`、`.env:33` |
| `ORDER_EXECUTION_ENABLED` | `false` | `config/config.yaml:23`、`.env:34` |
| `PAPER_TRADING` | `true` | `config/config.yaml:24`、`.env:35` |
| `kill_switch` | `false` | `config/config.yaml:25` |

- 未读取/输出任何密钥值；仅读取上述三个非秘密开关。
- `audit_evidence/*.log|json` 中 grep `openapi(-push).fosunxcz.com`、`wss://openapi`、`https://openapi`
  均无命中；测试使用本机 `127.0.0.1` 假服务。
- 本轮未连接生产 API、未启动真实行情、未发送订单。

**结论：PASS。**

---

## 8. 问题清单（按严重程度）

### P2（12 项，需在下一阶段前处置）

| ID | 领域 | 问题 | 关键证据 |
|---|---|---|---|
| B1-01 | B-1 | A/B 双 Vault 冲突无自动化测试 | `b4:266-378`；手工探针通过 |
| B1-02 | B-1 | 成功实例无 `close`/`__del__`，注册表/env 泄漏 | `adapter:592-607`、`:698`；`base.py` 无 close |
| B1-03 | B-1 | 无 Vault 实例静默复用他实例身份 | `adapter:526-529`；探针 owners=2 |
| A1-01 | A-1 | `market_push` 哨兵无自动化测试 | `adapter:609-634`；`grep FEED_AUTH_BYPASS tests/` 无结果 |
| F2-01 | F-2 | `session_id=`/`sessionId:`/`x_api_key=` 不匹配 | `log_redaction:101-124`；探针原样输出 |
| F2-02 | F-2 | 转义 JSON `{\"api_key\":…}` 不匹配 | `:112-114`；探针原样输出 |
| F2-03 | F-2 | `exc_info` 重渲染绕过（SDK `JsonFormatter`） | `logging_utils.py:131-132`；探针 `leak: True` |
| F2-04 | F-2 | 自定义 `Handler.handle`/`emit` 绕过 | `log_redaction:516-543`；探针输出原值 |
| F2-06 | F-2 | 多条路径无测试（stack_info 等） | `test_log_redaction_b41.py:51-303` |
| F2-07 | F-2 | 报告称 18 例全部走 handler，不实 | `US_P1_BATCH4_1_FIX_REPORT.md:160` |
| T-01 | 测试 | 全局 hook 永久驻留 pytest 进程 | `log_redaction:516-543`；fixture 不卸载 |
| DOC-01 | 文档 | 4.1 报告未记录 adapter 的 B-1/A-1 变更 | 4.1 报告 `:4`、`:22`；adapter +161/−27 |

### P3（19 项 OPEN + 1 项 CLOSED，建议随下一批清理）

B1-04、B1-05、B1-06、B1-07、A1-02、A1-03、A1-04、A1-05、A1-06、F2-05、F2-08、F2-09、F2-10、F2-11、F2-12、DOC-02、DOC-03、T-02、T-03（19 项 OPEN）；DOC-04 为 CLOSED（清单中的 `test_prod_conn_repair_b41.py` 不存在，用户原文为“如存在”）。详见 JSON。

### P0 / P1

**无。** 未发现当前生产调用图中可直接触发的凭证泄露、认证绕过或交易安全问题。

---

## 9. 未验证事项（UNVERIFIED）

1. 真实 SDK 端到端 `SessionCreate` 次数与 `market_push` 行为（无生产凭证）。
2. 真实订阅回执 `subRep` 格式（无生产样本）。
3. 跨进程凭证协调（注册表按设计仅进程内）。
4. 并发 `acquire`/`release` 竞态（无测试）。
5. 进程内存中明文登记表的读取防护（OS 级不属于本机制）。

---

## 10. 结论与放行

- **B-1：PARTIAL** —— 核心串用已修复且双 Vault 手工验证通过；缺测试、缺生命周期释放、
  无 Vault 继承身份需显式化。
- **A-1：PARTIAL** —— provider 门禁有测试；哨兵有效但无测试，且不是 SDK 级封堵。
- **F-2：PARTIAL** —— handler 边界/fail-closed/限额成立；存在 4 条可复现绕过（当前生产路径未触发）。
- **C-1 / D-1：PASS**。
- **测试证据：PARTIAL**；**安全边界：PASS**。

**最终总评：PARTIAL。**

**是否允许进入下一阶段：允许（有条件）。** 不触发 P0/P1 阻断；但在 Batch 4.2 开始前，应修复
或由人工书面接受 P2 项，尤其是 F2-03（`exc_info` 重渲染）、F2-04（自定义 handler）、
F2-01/02（规则缺口）、B1-02（生命周期泄漏）、B1-03（无 Vault 继承）、A1-01（哨兵无测试）、
DOC-01（变更清单不完整）。在此之前，不得对外宣称 F-2 是“统一、无死角”的脱敏边界。

**审计到此停止，等待人工审核；不进入 Batch 4.2。**
