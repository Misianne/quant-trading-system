# US-P1 Batch 4.1.1 第二轮独立安全审计报告

- **批次**：US-P1 Batch 4.1.1（定点修复 F-2 / B-1 / A-1 之后）
- **代码库**：`C:\FosunAI-Trading`（无 `.git`，以 SHA256 快照取证）
- **审计性质**：**第二轮独立审计**。不采信实施报告结论，全部结论来自本轮独立读取的源码与本地探针的真实输出
- **审计时间**：2026-11-10 12:40 – 13:23（本机时间）
- **解释器**：`C:\FosunAI-Trading\.venv\Scripts\python.exe`（Python 3.13.14）
- **只读声明**：本轮**未修改**任何源代码、测试、配置、`.env`、SDK 源码与数据；仅新增本报告与 `audit_evidence/us_p1_b41_1_second_audit.json`（及 5 份审计探针脚本）
- **安全姿态**：`LIVE_TRADING=false`、`ORDER_EXECUTION_ENABLED=false`、`PAPER_TRADING=true`（§6 实测）；未连生产、未创建 Session、未发订单、未访问 VPS

---

## 0. 总体结论（先看这里）

| 组 | 结论 | 一句话依据 |
|---|---|---|
| **F-2 日志脱敏** | **有条件通过** | 五层结构真实生效（真实 handler 输出验证 25 个用例）；但"末级降级恒可用"不成立，健康位存在两类误报（2 中 / 1 低 / 1 信息） |
| **B-1 凭据来源** | **有条件通过** | 原子登记、回滚、引用计数、跨实例隔离、fail-closed 全部实测成立；但**显式 env 的最高优先级在"与 .env 同值"时不成立**（Vault 越级），且 dotenv+export 混用会拒绝启动（2 中） |
| **A-1 行情推送** | **通过** | 用**真实** `MarketPushAPI` 验证：5 个入口 + 32 个薄包装全部 `FEED_AUTH_BYPASS`，引用计数/精确恢复/篡改检测/失败回滚全部成立，**零网络、零 Session** |
| **测试完整性** | **部分通过** | **T29 真实 PASS**（独立复现 `517 passed, 1 skipped`, exit 0）；既有测试 0 修改/0 删除/0 降断言；**T17、T20 不能计 PASS**（1 条部分实现、1 条重定义） |
| **安全边界** | **通过** | 6 个开关终值与要求一致；所有探针 `net=[]`、`session=[]` |

**是否阻断进入 Batch 4.2：否。** 本轮 4 条"中"级发现全部位于"健壮性 / 可用性 / 来源判定"面，不构成"以错误身份交易"或"密钥明文外泄"的现实通道；但 **AUD2-B1-01、AUD2-B1-02 必须在开启实盘前修掉**，并需把 T17/T20 的标注从 PASS 改为"部分通过 / 重定义"。

**是否阻断实盘：是（仅 B1 两条）。** 其余发现不阻断实盘。

---

## 1. 核对实际文件（要求 ①）

### 1.1 七份交付物

| 文件 | 存在 | 大小 (B) | SHA256（本轮实测） | 与实施报告一致 |
|---|---|---|---|---|
| `US_P1_BATCH4_1_1_FIX_REPORT.md` | 是 | 17214 | `d04b17de7428fd1ccd0171217c897747ebfa9affb9dd0309d427cc887baf6bae` | 是 |
| `US_P1_BATCH4_1_1_TEST_REPORT.md` | 是 | 9693 | `a1fbb59ea65515eca6ea03571466b8549c62ea825252829fc67c844399e51880` | 是 |
| `US_P1_BATCH4_1_1_FILE_MANIFEST.sha256` | 是 | 1658 | `b965ec12718ba3fd9b3c4a30ba6d3ae5d5e74a9b26e93fe16d8f808bc6bc9937` | 是 |
| `audit_evidence/us_p1_b41_1_test_results.json` | 是 | 10381 | `7341aff0843b62f0475fd04ce77b12a743c1afa4ee1cb965f147b39d61b3b2d9` | 是 |
| `audit_evidence/us_p1_b41_1_security_probes.json` | 是 | 6886 | `4ef53267620416090db8c9564bb510705db12c2d124203c55863c0ccc2f24986` | 是 |
| `audit_evidence/us_p1_b41_1_pytest.log` | 是 | 3556 | `495159b5ea6518256b0057f21aac24cae68d62271312c4006339be581b217661` | 是 |
| `audit_evidence/us_p1_b41_1_full.log` | 是 | 1161 | `c0d5f2c8f62cc0c52509a74d56a7f80af93aa8671ef21213c9e2b076f8436a1e` | 是 |

### 1.2 清单自校验

```
sha256sum -c US_P1_BATCH4_1_1_FILE_MANIFEST.sha256
→ OK=16  BAD=0
```

### 1.3 源码哈希与报告前后值

| 文件 | 报告"修复后" | 本轮实测 | 一致 |
|---|---|---|---|
| `src/ai_trader/log_redaction.py` | `bef31dea…12fb34` | `bef31deaafdd7c149a34c876022100617da07e8851d1fcbbb18cd6006412fb34` | 是 |
| `src/ai_trader/runtime.py` | `a3ad630c…616fd` | `a3ad630ca87df45d332c2dff10bc9b630f61bb7f47cdc269cff543b7739616fd` | 是 |
| `src/ai_trader/config.py` | `3ee1bc0f…362d` | `3ee1bc0fee23ad4a0b90928cc2303b44ff4779acc93b2cb2b4145c1dc019362d` | 是 |
| `src/ai_trader/broker/fosun_adapter.py` | `1cf22d39…10b9` | `1cf22d39343fe21bf178a3c1e718f085d59b12bfebf972fa56ea5d1dfa2a10b9` | 是 |

`audit_evidence/us_p1_b41_1_security_probes.json` 的 `source_sha256.after` 四值同样与实测逐一吻合 → **证据文件不是事后编造**。

### 1.4 与 P0 基线快照（61 文件）对比

| 项 | 数量 | 明细 |
|---|---|---|
| 变更 | **4** | `log_redaction.py`、`runtime.py`、`config.py`、`broker/fosun_adapter.py` |
| 缺失 | **0** | — |
| 新增 | **3** | `tests/test_log_redaction_b411.py`、`tests/test_credential_identity_b411.py`、`tests/test_market_push_guard_b411.py` |

→ 与实施报告 §4 的描述完全一致；**没有旧测试被修改、删除或降断言**。

---

## 2. F-2 独立复核（要求 ②）

### 2.1 方法

不使用"只调用脱敏函数"的验证方式。全部用例都构造真实 `logging` 记录、真实 handler、真实 Formatter，并**捕获最终输出字符串**再判断是否含密钥；每个用例开始前把三个钩子还原到进程初始对象（`PRISTINE` 快照）后重新 `setup_logging()`，避免用例间互相污染（这是第一版探针出现假阴的原因，已修正）。伪造凭证全部为 `AUDIT2-*` 随机串。

- 探针 v2（15 例）：`probe_f2.py`
- 探针 v3（10 例，定点补测 exc_text / 降级末档 / lastResort / 自愈）：`probe_f2_v3.py`

### 2.2 逐项核对结果

| 要求核对项 | 结论 | 证据 |
|---|---|---|
| `Logger.handle` 钩子是否**真实生效** | 是 | 自定义 `handle` 的 handler 输出 `x_api_key: ***`（真实输出，非空断言） |
| `Handler.handle` 是否可绕过 filter | 否 | `filters=()` 时自动走"包装实例 handle"，输出打码，`mode=wrapper` |
| `Handler.format` 是否可绕过 | 部分 | 正常 Formatter 兜底成立；**子类重写 `format()` 即绕过**（设计如此，§5 已披露） |
| `Formatter.formatException` 重渲染 | 否（已封堵） | `exc_info` 被中和为 `None`，重渲染 Formatter 拿到 `None`；`exc_text` 已被替换为打码文本 |
| `exc_info` 处理 | 正确 | 标准 Formatter 仍打印 `Traceback` + 异常类型，密钥不在（T05 语义复现） |
| 预置 `exc_text`（无 exc_info） | **正确** | KV 形态 → `bad x_api_key: ***`；**已登记**裸值 → `bad key ***`；未登记裸值属设计边界（AUD2-F2-04） |
| 结构化 extra（嵌套 dict/list） | 正确 | `%(payload)s` 中 `X-Api-Key` 与 `session_id` 均 `***` |
| 转义 JSON（0–3 层 `json.dumps`） | 正确 | 每层都打码；`F2-G` 4 组全部通过 |
| 不同键名（`x_api_key` / `apiKey` / `session_id` / `sessionToken`） | 正确 | 归一化匹配生效 |
| 钩子被覆盖后能否恢复 | **部分** | 包装式覆盖可自愈；**no-op 覆盖会永久静默**（AUD2-F2-02） |
| 安装失败是否真 fail-closed | **部分** | 三级降级前两级成立；**末级 `setLevel` 自身失败时未真正禁用**（AUD2-F2-01） |
| handler 输出是否可能发生在脱敏前 | 否 | `Handler.handle` 钩子在调用原实现**之前**补装 filter；`lastResort` 路径也打码（E5） |
| 第三方 handler 已知限制披露是否准确 | 基本准确，1 处过度承诺 | §5 #1 披露准确；但 §2.1 表格"L4 …最后一级**恒可用**"不成立（`setLevel` 可抛） |

**计数说明**：F-2 侧共 25 个用例（两版去重后 24 个场景）。用例的断言方向按设计不同：多数断言"应当打码"，少数（如 AUD2-F2-01 的复现用例）断言"缺陷确实存在"，其"通过"表示**复现成功**。逐条判定见 §7 与附录 11.2。

### 2.3 F-2 有效性的正面证据（摘要）

```
F2-A  Logger.handle：warning 输出 "handshake x_api_key: *** session_id: ***"   → 有输出且无密钥
F2-C  Rerender Formatter：exc_info_none=True，输出无密钥
F2-D  emit 读 msg/args：msg 已打码、args 已置空
F2-F  嵌套 extra：{"headers": {"X-Api-Key": "***"}, "list": [{"session_id": "***"}]}
F2-G  0–3 层转义：全部 "***"，且仍是合法 JSON
F2-M  callHandlers 直调：打码
F2-N  安装后新建 handler：打码
E1/E2 预置 exc_text（KV / 已登记裸值）：打码
E4    exc_info + 预置 exc_text 并存：以 exc_info 的脱敏文本覆盖，STALE-PRESET 不出现
E5    lastResort（无任何 handler，直写 stderr）："no handler api_key: ***"
E6    filters 只读（tuple）：mode=wrapper，输出打码
```

---

## 3. B-1 独立复核（要求 ③）

### 3.1 方法

16 个用例，全部使用随机伪造的 PEM / API Key；`Vault` 为内存假实现；SDK 桩**补齐了 `market_push` 的五个方法**（否则类级守卫会以 `missing_methods` 拒绝初始化，导致所有 broker `configured=False` —— 这是第一版探针 7 条无效的根因，已修正）。`.env` 场景改用**单行**值写入，因为本项目的 `load_dotenv` 逐行解析、不支持多行 PEM（见 AUD2-B1-05）。

- 探针：`probe_b1_v2.py`（16 例，13 通过 / 3 失败）

### 3.2 逐项核对结果

| 要求核对项 | 结论 | 证据（实际输出） |
|---|---|---|
| 四来源优先级 explicit > vault > dotenv > injected | **部分** | A（三要素全 explicit + Vault 有值）→ 全 explicit，PASS；**C（explicit 与 .env 同值）→ Vault 越级**，FAIL |
| 三要素严格同源 | 是 | E（env+vault 混用）→ `CREDENTIAL_SOURCE_MIX`，`client is None` |
| 多实例是否互相继承 | 否 | F：A 成立、B（空 Vault）`CREDENTIAL_INCOMPLETE`，`owners=1`；P：不同凭证的第二实例被拒且 A 环境不变 |
| `acquire_many()` 是否真正原子 | 是 | G：两线程同时 `_inject_sdk_env`，结果 `configured=[True, False]`，环境里 PUB/PRIV **同一套**，无半套身份 |
| 初始化失败是否完整回滚 | 是 | H：SDK 构造抛错后三个环境变量全 `None`，`_CRED_ENV.snapshot()=={}` |
| 释放是否误删他人变量 | 否 | I：A、B 同凭证，A 关闭后环境仍在；第三方改写后 B 关闭**不覆盖**（保留 `third-party-value`）；N：双实例 `owners 1→2`，先关一个保留、归零才删 |
| `dotenv_origin()` 是否仅凭值指纹 | **是（正是如此，故有缺陷）** | K：`loaded_value=True`、`changed_value=False`、**`explicit_same_value=True`** → 无法区分"后来显式 export 同值" |
| 显式 env 与 dotenv 值完全相同时如何识别 | **识别错误** | C：被判 `dotenv` → Vault 越级；C2：来源标签写 `dotenv`（应为 `explicit_env`） |
| 无法识别来源时是否 fail-closed | 是 | J：全无 → `incomplete`、`configured=False`；M：Vault 只有 1/3 → `CREDENTIAL_INCOMPLETE` |

### 3.3 B-1 的正面证据（摘要）

```
B1-A  sources={PUB_KEY: explicit_env, PRIV_KEY: explicit_env, API_KEY: explicit_env}  picked=env
B1-B  sources 全为 vault，SDK 拿到 Vault 的 PRIV                                  picked=vault
B1-E  凭证来源冲突: 来源不一致（API_KEY=vault, PRIV_KEY=vault, PUB_KEY=explicit_env）
B1-G  configured=[True, False]  env_pair_consistent=True  owners={PUB:1, PRIV:1}
B1-H  leftover={PUB: None, PRIV: None, API_KEY: None}  owners={}
B1-I  both_configured=(True, True)  after_a_close_kept=True  after_b_close='third-party-value'
B1-N  owners_after_1=1  after_2=2  kept_after_first_close=True  cleared_after_second=True
B1-P  a=True  b=False  a_env_intact=True  reason="SDK 初始化失败: CredentialConflict: …"
```

---

## 4. A-1 独立复核（要求 ④）

### 4.1 方法（本轮最严格的一处）

- **不使用自制止替身冒充被保护对象**：直接从 `sdk_src` 导入**真实** `fsopenapi.api.market_push.MarketPushAPI`，在该类上安装拦截，再用真实类实例调用。
- 客户端桩完全仿照 `SDKClient.__init__`：`self.market_push = MarketPushAPI(self)`（与 `sdk_src/fsopenapi/__init__.py:40` 一致）。
- **本地哨兵**：`socket.socket.connect` / `connect_ex` / `create_connection` / `websockets.connect` 全部替换为"记录 + 抛错"；`auth_manager.get_valid_session` 用计数器（一旦被调用即抛断言）。
- 13 个用例，**13 通过 / 0 失败**，两次运行均 `net=[]`、`session=[]`。

### 4.2 逐入口结果

| 入口 | 类级拦截 | 调用即抛 `FEED_AUTH_BYPASS` | 真实方法是否执行 | 网络 |
|---|---|---|---|---|
| `connect` | 是 | 是（同步抛，不需 `await`） | 否 | 0 |
| `subscribe` | 是 | 是 | 否 | 0 |
| `unsubscribe` | 是 | 是 | 否 | 0 |
| `close` | 是 | 是 | 否 | 0 |
| `send_heartbeat` | 是 | 是 | 否 | 0 |

补充：`subscribeXxx` / `unsubscribeXxx` **32 个薄包装**逐个调用，全部在 `await` 时抛 `FEED_AUTH_BYPASS`（因为它们都 `await self.subscribe(...)`），无一漏网。

### 4.3 其它要求的核对

| 要求核对项 | 结论 | 证据 |
|---|---|---|
| 类级拦截 | 成立 | `install=installed`，五个入口全部 `blocked` |
| 实例级保护 | 成立 | `isinstance(client.market_push, _MarketPushDisabled)`；取属性即抛（业务属性），下划线属性走 `AttributeError`（`__deepcopy__`/`_private`/`_ws` 均正常） |
| 引用计数 | 成立 | 两 broker `owners=2`；关 A 后 `owners=1` 且**仍在拦截**；关 B 后 `installed=False` |
| 重复安装 | 幂等 | 同类重复安装 `reason=already_installed`，不叠加 |
| 重复释放 | 幂等 | `close()` 三次：`released=[True, False, False]` |
| 异常回滚 | 成立 | 缺方法 → `missing_methods`，类未被改动（`connect is` 原对象）；`setattr` 中途失败 → `setattr_failed:AttributeError`，五个方法**全量回滚**且 `all_original=True` |
| 原方法精确恢复 | 成立 | 归零后 `MarketPushAPI.connect` 非守卫函数且 `__name__=="connect"`；被第三方改写的 `subscribe` **不覆盖**，记入 `skipped`/`tampered` |
| 篡改检测 | 成立 | 改回 `subscribe` 后 `tampered=['subscribe']`，`health` 明细同步 |
| 守卫装好后初始化失败 | 成立 | 强制返回 False → `configured=False`、`owners=0`、`installed=False`（异常路径也释放引用计数） |
| 是否触达真实网络 | **否** | 全部用例 `net=[]`、`session=[]` |
| 是否创建真实 Session | **否** | `get_valid_session` 调用计数 0 |

**A-1 结论：通过，无发现。**

---

## 5. 测试完整性（要求 ⑤）

### 5.1 T29 的真实状态（独立复现，不采信报告）

```
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 .venv/Scripts/python.exe -m pytest tests \
  --ignore=tests/test_log_redaction_b411.py \
  --ignore=tests/test_credential_identity_b411.py \
  --ignore=tests/test_market_push_guard_b411.py \
  -q -p no:cacheprovider -o addopts=""
→ 517 passed, 1 skipped, 1 warning in 63.54s   exit=0
```

**T29 真实 PASS**：排除三个新文件后的存量用例数与计划基线 `517 passed, 1 skipped` **完全一致**，退出码 0。含新文件时为 549，增量 32，与 `32 passed in 1.90s` 的独立复现一致。

### 5.2 T17 / T20 的判定（不计 PASS）

| ID | 计划原始意图（`REMEDIATION_PLAN.md:1041,1044`） | 实际实现 | 判定 |
|---|---|---|---|
| **T17** | "`policy=warn` 时 `credential_mixed=true`" —— 即提供 warn 降级开关并验证其行为 | **不实现 warn**，只暴露 `credential_mixed` / `credential_validation.mixed` 字段（`test_t17_*` 仅断言字段存在与指纹格式） | **部分通过（未完成计划目标）**，不得计 PASS |
| **T20** | "显式开关开启且三要素齐全才复用 + 告警" | 开关不实现；测试改为"运维 export 的同值不被误判为注入"（`test_t20_operator_exported_value_is_not_mistaken_for_injection`） | **重定义（未覆盖计划目标）**，不得计 PASS |

说明：实施报告 §7 与测试报告 §4 **已如实披露**这两处偏离，属"透明偏离"而非隐瞒；但按本次审计规则，"重定义测试目标"不能算 PASS，因此 T17/T20 必须改标注。

**修正后的计数**：T01–T29 共 29 条 → **27 PASS / 1 部分通过（T17）/ 1 重定义（T20）**。

### 5.3 其它核查

| 项 | 结论 |
|---|---|
| 旧测试是否被修改 | **否**（0 个文件哈希变化） |
| 旧测试是否被删除 | **否**（0 个缺失） |
| 断言是否被降低 | **未发现**（除 4 个源文件外，`src/`+`tests/` 全部哈希一致；新增仅 3 个测试文件） |
| T07（断言"确实泄露"）标 `PASS（边界）` 是否合理 | **合理**：该用例目的就是防止后人误以为该路径已覆盖，断言方向与其它相反且被显式记录 |
| T12 是否覆盖"末级降级失败" | **未覆盖**：只验证 `setLevel` 成功的场景（`level==CRITICAL+10`）。`setLevel` 自身抛异常的分支无测试（对应 AUD2-F2-01） |
| 是否新增 skip | 否（仍为 1 条既有 skip） |

---

## 6. 安全边界（要求 ⑥）

只做静态读取 + 本地 `load_config()`（生产路径，`use_dotenv=True`），未构造 SDK、未连网络。

| 项 | 实测值 | 是否符合要求 |
|---|---|---|
| `live_trading` | **false** | 是 |
| `order_execution_enabled` | **false** | 是 |
| `paper_trading` | **true** | 是 |
| `kill_switch` | false | 是 |
| `can_connect_broker()` | **false** — `PAPER_TRADING=true，真实券商连接被禁用` | 是 |
| `can_send_real_order()` | **false** — 同上 | 是 |
| `live_ack_satisfied` | false | 是（未提供实盘确认短语） |
| `halt_store_failed` | false | 是 |
| 是否触达生产网络 | **否**（A-1 探针 `net=[]`） | 是 |
| 是否创建真实 Session | **否**（`session=[]`） | 是 |
| 是否执行真实订单 | **否**（未调用任何下单入口） | 是 |
| 是否访问 VPS `203.0.113.10` | **否** | 是 |

**附带观察**：生产配置为 `fosun.sdk_type="ops"` 且 `fosun.base_url` 为空。`sdk_type=ops` 与"本适配器只调个人接口"的冲突是**既有**独立审计 F-9 事项（`tests/helpers.py` 已注明），不属本轮回归；`base_url` 为空会使 `_init_client` 的 `missing` 检查直接 fail-closed。

---

## 7. 发现清单（要求 ⑦）

严重级别：高 / 中 / 低 / 信息。每条含 ID、级别、文件与行号、复现步骤、实际结果、预期结果、是否阻断下一阶段、是否阻断实盘。

### AUD2-F2-01｜末级降级失败时"计入 blocked"≠"真的禁用"

- **严重级别**：中
- **文件与行号**：`src/ai_trader/log_redaction.py:617-627`（`setLevel` 与两处 `_BLOCKED_HANDLERS.append`）、`src/ai_trader/log_redaction.py:746-755`（`redaction_health`）
- **复现步骤**：
  1. 构造一个 handler：`filters` 只读（返回 `()`）、`__setattr__` 拒写 `handle`/`level`、`setLevel()` 抛异常、**并重写 `format()`**；
  2. `install_on_handler(h)`；
  3. 直接 `h.handle(LogRecord(..., "api_key=%s", (AK,)))`（模拟第三方不经 logger 直驱 handler）；
  4. 读取 `h.stream.getvalue()` 与 `redaction_health()["blocked_handlers"]`。
- **实际结果**：`install=False`，`blocked_handlers=1`，但 `direct_leak=True` —— 输出 `api_key=AUDIT2-BARE-APIKEY-4f7c1d9e`（明文）。经 logger 的正常路径仍被打码（L2 生效）。
- **预期结果**：`_BLOCKED_HANDLERS` 只应统计"确实已无法输出"的 handler；末级 `setLevel` 失败时应记为"未能禁用"，并在健康位中单列（例如 `unblocked_handlers`），而不是混入 `blocked`。
- **是否阻断下一阶段**：否
- **是否阻断实盘**：否（项目内所有 handler 均由 `setup_logging` 创建，未发现重写 `format()` 的 handler；但实盘前应确认接入的第三方 handler 不改写 `format`）

### AUD2-F2-02｜钩子被替换为 no-op 后，"自愈"会把 no-op 当成原实现，日志永久静默

- **严重级别**：中
- **文件与行号**：`src/ai_trader/log_redaction.py:658-669`（`_wrap_method` 取 `original = getattr(current, "_aitrader_original", current)`）、`:703-720`、`:758-767`（`ensure_hooks`）
- **复现步骤**：
  1. 正常 `setup_logging()`；
  2. 第三方执行 `logging.Logger.handle = lambda self, record: None`；
  3. 调用 `ensure_hooks()`；
  4. `log.error("token=%s", TK)` 并读取输出。
- **实际结果**：`repaired={'logger': True}`、`health["logger_hook"]=True`，但输出为**空字符串** —— 所有日志永久静默，且健康位显示正常。`logging.Handler.handle` 被同样替换时行为一致。
- **预期结果**：`_wrap_method` 只应把"确实会调用记录的对象"当作原实现；当检测到当前实现既非我们的包装、又不是原始实现（例如 `__code__` 签名不符/无 `_aitrader_original` 且非 `logging` 原生函数）时，应保留原函数引用（进程启动时的 `PRISTINE` 快照）或至少让 `redaction_health()` 暴露"自愈后无输出"的状态。
- **是否阻断下一阶段**：否
- **是否阻断实盘**：否（仓库内无任何改写 `Logger.handle` 的代码，`grep` 仅命中测试夹具；但实盘期间若失去审计日志，属高风险运维事件，建议修）

### AUD2-F2-03｜`_aitrader_redacted` 是普通属性，可被伪造以跳过 L3

- **严重级别**：低
- **文件与行号**：`src/ai_trader/log_redaction.py:449`（定义）、`:734-740`（L3 读取该标记直接返回原文）
- **复现步骤**：构造 `LogRecord("...", "connect api_key=%s", (AK,))`，`setattr(record, "_aitrader_redacted", True)`，直接调用 `handler.format(record)`。
- **实际结果**：返回 `connect api_key=AUDIT2-BARE-APIKEY-4f7c1d9e`（未打码）。
- **预期结果**：跳过判据不应依赖记录上的可写属性；可用"记录 id 集合 + 弱引用"或私有哨兵对象。
- **是否阻断下一阶段**：否
- **是否阻断实盘**：否（仅在记录**同时绕过** L1 filter 与 L2 `Logger.handle` 时才可达；能绕过的代码本就已持有明文）

### AUD2-F2-04｜未登记的裸字面量不打码（设计边界，建议在 §5 明确列出）

- **严重级别**：信息
- **文件与行号**：`src/ai_trader/log_redaction.py:279-318`（`redact_text` 的 `_RULES` 与 `_secret_snapshot` 两条路径）
- **复现步骤**：`fresh_install()` 后 `log.info("received push for session %s", "AUDIT2-...")`，或预置 `exc_text="... bad key AUDIT2-..."`。
- **实际结果**：裸令牌原样输出；同一值经 `register_secret()` 登记后即被打码。
- **预期结果**：这是基于模式的脱敏的固有边界（无键名、未登记则无法识别）。**FIX_REPORT §5 未单列此条**，建议补入，以免被误读为"任意形态密钥都不会泄露"。
- **是否阻断下一阶段**：否
- **是否阻断实盘**：否（生产代码中没有以裸令牌方式记录 session/api_key 的调用点，已 `grep` 确认）

### AUD2-B1-01｜显式 env 与 `.env` 同值时，Vault 越级胜出（`explicit_env` 最高优先级失效）

- **严重级别**：中
- **文件与行号**：`src/ai_trader/broker/fosun_adapter.py:831`（`from_dotenv` 判定）、`:842-848`（优先级分支）、`src/ai_trader/config.py:261-283`（`dotenv_origin` 只看键名+值指纹）
- **复现步骤**：
  1. 写 `.env`：`PUB=PUB_D, PRIV=PRIV_D, API_KEY=AK_D`，`load_dotenv()`；
  2. 运维 `export` **完全相同**的三个值（模拟"值没变、只是显式声明"）；
  3. 用另一套值的 `Vault`（`PUB_V/PRIV_V/AK_V`）构造 `FosunBroker`；
  4. 读 `health()["credential_sources"]` 与 SDK 实际拿到的 `priv`。
- **实际结果**：`sources={PUB_KEY: vault, PRIV_KEY: vault, API_KEY: vault}`，SDK 拿到 `PRIV_V`（Vault 值）。B1-C。
- **预期结果**：`explicit_env` 优先级高于 `vault`，应取 `PRIV_D` 且来源标注 `explicit_env`；`dotenv_origin` 只能证明"值与 .env 相同"，不能证明"来源是 .env"，因此不足以把显式 export 降级为 dotenv。
- **是否阻断下一阶段**：否
- **是否阻断实盘**：**是**（运维的显式覆盖会静默失效，系统可能用一套非预期的密钥签名，导致 401 或落到非预期账户）

### AUD2-B1-02｜dotenv 与显式 export 混用被判 `mixed`，真实轮换场景无法启动

- **严重级别**：中
- **文件与行号**：`src/ai_trader/broker/fosun_adapter.py:860-880`（`_validate_source_model` 把"dotenv + explicit"当混合身份）
- **复现步骤**：
  1. `.env` 保留 `PUB=PUB_D`（公钥未变）；
  2. 只 `export` 轮换后的 `PRIV=PRIV_B`、`API_KEY=AK_B`；
  3. `load_config()`（生产默认 `use_dotenv=True`，`config.py:388`）后构造 broker。
- **实际结果**：`configured=False`，`blocked_reason="凭证来源冲突: 来源不一致（API_KEY=explicit_env, PRIV_KEY=explicit_env, PUB_KEY=dotenv）（CREDENTIAL_SOURCE_MIX…"`。B1-D。
- **预期结果**：三要素都来自"运维提供的本地配置"（`.env` 与 export 属同一信任域），不应判为混合身份；至少应把 `dotenv` 与 `explicit_env` 视为同一"本地配置"族，仅在 `vault` 与本地配置之间做同源校验。
- **是否阻断下一阶段**：否
- **是否阻断实盘**：**是**（该部署方式下 broker 直接拒绝启动；属可用性阻断，无安全风险）
- **备注**：现有 `test_t20` 只覆盖"三要素全部 export、且无 `.env` 干扰"的场景，未覆盖 `.env` + 部分 export 的真实轮换路径。

### AUD2-B1-03｜同值场景的来源标签写错（`dotenv` 而非 `explicit_env`）

- **严重级别**：低
- **文件与行号**：`src/ai_trader/broker/fosun_adapter.py:847-848`
- **复现步骤**：如 B1-01，但 Vault 为空。
- **实际结果**：`sources={PUB_KEY: dotenv, PRIV_KEY: dotenv, API_KEY: dotenv}`，取值正确。B1-C2。
- **预期结果**：来源标签应为 `explicit_env`（值由运维显式设置）。仅影响审计字段与面板展示，不影响取值。
- **是否阻断下一阶段**：否
- **是否阻断实盘**：否

### AUD2-B1-04｜`CredentialConflict` 的错误码未写入 `blocked_reason`

- **严重级别**：低
- **文件与行号**：`src/ai_trader/broker/fosun_adapter.py:1130-1174`（`_inject_sdk_env` 在 `try` 内抛出，被 `except Exception` 泛化）
- **复现步骤**：A 用凭证 1 配置成功；B 用凭证 2 构造。
- **实际结果**：B `configured=False`（正确）、A 环境未被破坏（正确），但 `blocked_reason="SDK 初始化失败: CredentialConflict: …"`，**不含** `CREDENTIAL_CONFLICT` 码；而同类的 mix/incomplete 都带码。B1-P。
- **预期结果**：面板/告警按 code 分类时，冲突应与"SDK 初始化失败"区分；`except BrokerError` 应单独分支并保留 `exc.code`。
- **是否阻断下一阶段**：否
- **是否阻断实盘**：否

### AUD2-B1-05｜`load_dotenv` 不支持多行 PEM，`.env` 里的 PEM 会被截断（既有实现）

- **严重级别**：低
- **文件与行号**：`src/ai_trader/config.py:286-315`（逐行 `partition("=")`，不做多行/转义处理）
- **复现步骤**：把标准多行 PEM 写进 `.env`（`FSOPENAPI_SERVER_PUBLIC_KEY=-----BEGIN PUBLIC KEY-----\nMIIB...`），`load_dotenv()` 后读取该变量。
- **实际结果**：变量值变成 `-----BEGIN PUBLIC KEY-----`（后续行因不含 `=` 被丢弃）；指纹照常记录，来源被标为 `dotenv`，但值是坏的（B-1 探针首版即因此全部误判，已定位）。
- **预期结果**：要么支持引号内多行 / `\n` 转义，要么在检测到 PEM 被截断时 fail-closed 并明确报错（现在会在 SDK 侧以认证失败呈现）。
- **是否阻断下一阶段**：否
- **是否阻断实盘**：否（条件性：仅当把多行 PEM 直接写入 `.env`；改用单行或 Vault 即可规避）
- **备注**：这是**既有**实现（本批次 `config.py` 仅 +50 行指纹逻辑），非本轮回归；但它决定了 "dotenv 来源" 在 PEM 场景下实际不可用。

### AUD2-T-01｜T17 实际"部分通过"，不应以 PASS 呈现

- **严重级别**：低
- **文件与行号**：`US_P1_BATCH4_1_1_TEST_REPORT.md:50`、`audit_evidence/us_p1_b41_1_test_results.json`（matrix 计数 `PASS(部分):1`）
- **复现步骤**：对照 `US_P1_BATCH4_1_1_REMEDIATION_PLAN.md:1041` 的 T17 定义与 `tests/test_credential_identity_b411.py:151-168` 的实际断言。
- **实际结果**：计划要求 `policy=warn` 行为；实现未提供该开关，测试只断言 `credential_mixed`/`mixed` 字段与指纹格式。
- **预期结果**：矩阵中应记为"部分通过（未实现 warn）"，不计入 PASS 数；汇总处不应出现 T01–T29 全 PASS 的印象。
- **是否阻断下一阶段**：否
- **是否阻断实盘**：否

### AUD2-T-02｜T20 已被重定义，不能算 PASS

- **严重级别**：低
- **文件与行号**：`US_P1_BATCH4_1_1_TEST_REPORT.md:53`、`tests/test_credential_identity_b411.py:213-230`
- **复现步骤**：对照 `REMEDIATION_PLAN.md:1044` 与 `test_t20_operator_exported_value_is_not_mistaken_for_injection`。
- **实际结果**：计划目标"显式开关开启且三要素齐全才复用 + 告警"未实现；测试改测 `is_operator_value` 的另一条性质（该性质本身有价值）。
- **预期结果**：明确记为"未实现（重定义）"，并从 PASS 计数中剔除；如需保留该测试，应另编号。
- **是否阻断下一阶段**：否
- **是否阻断实盘**：否

### AUD2-T-03｜报告称"最后一级恒可用"，与实测不符

- **严重级别**：低
- **文件与行号**：`US_P1_BATCH4_1_1_FIX_REPORT.md:59`（L4 "最后一级恒可用"）、`src/ai_trader/log_redaction.py:619-627`
- **复现步骤**：同 AUD2-F2-01（`setLevel` 抛异常）。
- **实际结果**：末级 `setLevel` 自身可失败；失败后 handler 仍在工作，只被计入 `_BLOCKED_HANDLERS`。
- **预期结果**：表述改为"末级为 `setLevel(CRITICAL+10)`；若 `setLevel` 亦失败，则仅记录告警，无法保证拒绝输出"，并在 §5 增加对应边界。
- **是否阻断下一阶段**：否
- **是否阻断实盘**：否

### AUD2-S-01｜生产配置 `sdk_type=ops` 与个人接口适配器冲突（既有，非本轮回归）

- **严重级别**：信息
- **文件与行号**：`config/config.yaml`（`fosun.sdk_type: ops`）、`src/ai_trader/broker/fosun_adapter.py` 的 `_require()` 生产路由校验
- **复现步骤**：`load_config()` 后读 `cfg.fosun["sdk_type"]`，并查看 `tests/helpers.py:47-53` 的注释。
- **实际结果**：`sdk_type="ops"`，`base_url=""`；本地夹具默认把它改写成 `""` 才能走通个人接口路径。
- **预期结果**：这是独立审计 F-9 已登记事项（等待星财富确认账户类型），本轮不擅自改配置；但应在实盘前闭环，否则 `_require()` 会 fail-closed 拒绝连接。
- **是否阻断下一阶段**：否
- **是否阻断实盘**：否（当前会 fail-closed，不会误连）

---

## 8. 与实施报告的差异（只列有实质分歧的部分）

| 实施报告的说法 | 本轮实测 | 差异性质 |
|---|---|---|
| §0 "没有 P0/P1 遗留" | 同意（无 P0/P1）；但有 4 条"中"级健壮性/可用性缺陷 | 补充 |
| §2.1 L4 "最后一级恒可用" | **不成立**（`setLevel` 可抛） | 表述过度承诺 |
| §2.1 "被第三方覆盖的钩子会由 `ensure_hooks()` 自动修复" | 包装式覆盖可修；**no-op 覆盖会把日志永久静音** | 未披露的失败模式 |
| §2.2 优先级 "explicit_env > vault > dotenv > injected" | 三要素全 explicit 时成立；**与 .env 同值时 Vault 越级** | 实现与声明不一致 |
| §5 边界 5 条 | 基本准确，但缺"未登记裸字面量"与"`setLevel` 失败"两条 | 披露不全 |
| TEST_REPORT "T17 PASS（部分）/ T20 PASS（重定义）" | 应记为"部分通过 / 重定义"，**不计 PASS** | 标注不合规 |
| §6 "是否触达生产网络：否" | 同意（本轮独立验证 `net=[]`、`session=[]`） | 一致 |

---

## 9. 复现命令（全部只读）

```bash
cd C:\FosunAI-Trading

# F-2（15 + 10 例）
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 .venv/Scripts/python.exe \
  <audit>/probe_f2.py
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 .venv/Scripts/python.exe \
  <audit>/probe_f2_v3.py

# B-1（16 例）
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 .venv/Scripts/python.exe \
  <audit>/probe_b1_v2.py

# A-1（13 例，使用真实 MarketPushAPI + 网络哨兵）
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 .venv/Scripts/python.exe \
  <audit>/probe_a1.py

# 安全开关（只读）
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 .venv/Scripts/python.exe \
  <audit>/probe_safety.py

# T29 回归（排除三个新文件）
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 .venv/Scripts/python.exe -m pytest tests \
  --ignore=tests/test_log_redaction_b411.py \
  --ignore=tests/test_credential_identity_b411.py \
  --ignore=tests/test_market_push_guard_b411.py \
  -q -p no:cacheprovider -o addopts=""
```

`<audit>` = `audit_evidence/us_p1_b41_1_second_audit_probes/`（本报告附带，见 §11）。

---

## 10. 建议（本轮不执行，交人工决策）

| 优先级 | 事项 |
|---|---|
| **实盘前必修** | AUD2-B1-01（来源优先级）、AUD2-B1-02（dotenv+export 误判 mixed） |
| 建议本批次内补 | AUD2-F2-01（健康位误报）、AUD2-F2-02（no-op 自愈致静默）、AUD2-T-03（表述修正） |
| 报告整改 | T17/T20 改为"部分通过 / 重定义"，从 PASS 计数剔除；§5 补两条边界 |
| 可延后 | AUD2-F2-03、AUD2-B1-03、AUD2-B1-04、AUD2-B1-05 |
| 既有工单 | AUD2-S-01（F-9，`sdk_type=ops`） |

**本轮到此停止**：不修改代码、不开始 Batch 4.2、不发布、不开启实盘，等待人工审核。

---

## 11. 附录

### 11.1 本轮新增的审计产物（均不属产品代码）

| 文件 | 说明 |
|---|---|
| `US_P1_BATCH4_1_1_SECOND_INDEPENDENT_AUDIT.md` | 本报告 |
| `audit_evidence/us_p1_b41_1_second_audit.json` | 结构化结果（发现 13 条 + 五组结论 + 证据摘要） |
| `audit_evidence/us_p1_b41_1_second_audit_probes/probe_f2.py` | F-2 探针 v2（15 例） |
| `audit_evidence/us_p1_b41_1_second_audit_probes/probe_f2_v3.py` | F-2 探针 v3（10 例） |
| `audit_evidence/us_p1_b41_1_second_audit_probes/probe_b1_v2.py` | B-1 探针（16 例） |
| `audit_evidence/us_p1_b41_1_second_audit_probes/probe_a1.py` | A-1 探针（13 例） |
| `audit_evidence/us_p1_b41_1_second_audit_probes/probe_safety.py` | 安全开关只读核对 |

### 11.2 探针结果统计

| 组 | 用例数 | 符合断言 | 不符合断言 | 不符合的条目 |
|---|---|---|---|---|
| F-2 v2 | 15 | 10 | 5 | F2-E（探针自身把"未登记裸值"当成缺陷，已由 v3 拆分）、F2-H（自愈静默）、F2-I（末级降级）、F2-J（标记伪造）、F2-K（未登记裸值，边界） |
| F-2 v3 | 10 | 6 | 4 | E3（未登记裸值，边界）、E8（标记伪造）、E9（自愈静默）、E10（健康位） |
| B-1 | 16 | 13 | 3 | B1-C（优先级倒置）、B1-C2（来源标签）、B1-D（mixed 误判） |
| A-1 | 13 | 13 | 0 | — |

> 断言方向说明：绝大多数用例断言"**应该**打码 / 应该拦截 / 应该 fail-closed"；AUD2-F2-01 的复现用例（E7）反过来断言"**缺陷确实存在**"，因此它出现在"符合断言"一列，但同时是 §7 中的一条发现。F2-E 是第一版探针的构造问题（把"未登记裸令牌"与"exc_text 清理"混在一个用例），v3 的 E1/E2/E3 已把它拆成三个可判定场景。

### 11.3 本轮未做的事（避免越界）

- 未修改任何源代码 / 测试 / 配置 / `.env` / `sdk_src`
- 未运行任何会联网的代码；`websockets` 与 `socket` 均被本地哨兵替换
- 未构造真实 `SDKClient`、未调用 `get_valid_session`、未创建 Session
- 未执行任何下单 / 撤单 / 查询账户操作
- 未开始 Batch 4.2、未发布、未开启实盘
