# US-P1 Batch 4 修复报告（A-1 / C-1 / B-1 / D-1 / F-2）

- 修复时间：2026-11-09
- 修复范围：`src/ai_trader/broker/fosun_adapter.py`、`src/ai_trader/runtime.py`、`src/ai_trader/market/feed.py` + 测试
- 修复前快照：`backups/us_p1_b4_20261109-175153/`（含 `MANIFEST.sha256.json`：sha256 / mtime / size / EOL / 行数）
- 修复后快照：同目录 `MANIFEST_AFTER.sha256.json`
- 结论：**5 项全部修复，10 项验收全部通过**；全套 `499 passed, 1 skipped`
- 安全边界：`LIVE_TRADING=false`、`ORDER_EXECUTION_ENABLED=false`、`PAPER_TRADING=true` 未改动；未连生产、未下单、未改 `sdk_type`/接口前缀、未动 `.env`/vault/数据库、未重签密钥、未改 VPS/白名单、未重启服务

---

## 0. 变更总览（含 EOL 核验）

| 文件 | 行级 diff（EOL 归一后） | EOL 修复前 → 修复后 | 说明 |
|---|---|---|---|
| `src/ai_trader/market/feed.py` | +245 / −23 | CRLF → **CRLF** | C-1 状态机、D-1 关闭分类、F-2 脱敏 |
| `src/ai_trader/broker/fosun_adapter.py` | +174 / −42 | CRLF → **CRLF** | A-1 `feed_auth_ready/headers`、B-1 注册表 |
| `src/ai_trader/runtime.py` | +19 / −12 | CRLF → **CRLF** | A-1 provider 门禁前置 |
| `tests/test_prod_conn_repair_b3.py` | +30 / −4 | CRLF → **CRLF** | C-1 语义更新 + 日志级别还原 |
| `tests/test_prod_connectivity_p0.py` | +14 / −5 | LF → **LF** | C-1 语义更新 + 消除测试竞态 |
| `tests/conftest.py` | 纯追加（未改任何原有行） | LF → **LF** | 进程级凭证注册表按用例隔离 |
| `tests/test_prod_conn_repair_b4.py` | 新增 23 例 | — → CRLF | 本轮验收套件 |
| `audit_evidence/probe_b4.py` | 新增探针 | — | 输出实测数值 JSON，供审计复跑 |

> **仅换行差异 = 0 个文件**：5 个既有文件在修复前后 CRLF/LF 完全一致（CRLF 文件的 `\r\n` 计数在修复前后均等于总行数，LF 文件 `\r\n` 计数为 0）。补丁工具先 `decode → CRLF 归一为 LF → 替换 → 按原样式回写`，因此上表行数即**真实语义改动**，不含换行噪声。

---

## 1. A-1：`build_feed_auth_provider` 绕过门禁与路由校验

### 问题（审计原文结论）
`build_feed_auth_provider()` 直接读取 `client.auth_manager.get_valid_session()`，既不经过 `can_connect_broker()` 门禁，也不经过 `routing_issue()` 路由校验。在 `sdk_type=ops` + 个人接口的已知冲突下仍会发起 `POST /api/ops/v1/auth/SessionCreate`。

### 根因
"认证信息从哪来"被实现成"直接摸 SDK 内部对象"，绕开了唯一入口守卫 `_require()`。业务接口 6 个入口都有守卫，唯独行情认证这条旁路没有 —— 属于**第二套取凭证的路径**。

### 修改
`fosun_adapter.py`

- 新增 `feed_auth_ready() -> (bool, str)`：判据与 `_require()` **逐条一致**（`can_connect_broker` → `routing_issue` → `client` → `auth_manager` 是否存在），只返回布尔与原因，**不触碰 auth_manager**。
- 新增 `feed_auth_headers() -> Dict[str, str]`：先 `self._require()`（纵深防御，门禁中途变化也会被拦），再 `get_valid_session()`；取不到 / 为空 → `BrokerError(code="FEED_AUTH_UNAVAILABLE")`，异常文本只含异常类名，不含内部细节。

`runtime.py`

- `build_feed_auth_provider()` 改为**先问 `feed_auth_ready()`**：不可用 → `logger.error(原因)` 并 `return None`，**不创建闭包、不触达 auth_manager、不发任何 HTTP**；可用 → 闭包内调用 `feed_auth_headers()`（每次建连现取，Session 过期由 SDK 刷新）。
- `build_feed()` 的提示文案同步改为"门禁未通过 / 路由冲突 / 凭证缺失 / 无 auth_manager"，避免把 A-1 场景误报成"未提供 auth_manager"。

### 证据（`audit_evidence/probe_b4_result.json`）

```json
"A1_provider_guard": {
  "routing_issue": true,
  "provider_created": false,
  "session_create_calls": 0,
  "sdk_init_calls": 1
}
```

`SDKClient.__init__` = 1（构造阶段本就允许，只在入口处拦），`get_valid_session` = **0** —— 即真实环境下 SessionCreate 请求数 = 0。

---

## 2. C-1：只带 requestid 的回执被当成订阅成功

### 问题
`_ack_verdict()` 里存在"RID 命中且 `act/topic` 像回执 ⇒ 确认，`ack_mode="requestid"`"的分支。但 requestid 只能证明"服务端收到了这个请求"，证明不了**任何一个代码被接受**。结果是：服务端只要echo一个 requestid，客户端就进入 `SUBSCRIBED`，行情层对外宣称"已订阅"，风控据此放行开仓。

### 修改（`market/feed.py`）

1. **删除**"RID-only ⇒ 确认"分支。确认的必要条件收紧为：`requestid` 严格命中 + `subRep` 可解析 + 主题存在 + **回执列出的代码集合 ⊇ 请求代码集合**。任何一环缺失 → 不确认。
2. 新增状态 `FeedState.ACK_UNVERIFIED = "ACK_UNVERIFIED"` 与异常 `FeedAckUnverified(FeedSubscriptionError)`（`code="FEED_ACK_UNVERIFIED"`），语义：**"服务端回了话，但无法核实订阅了哪些代码"**，与 `FAILED`（"压根没回/被拒"）区分开。
3. 新增 `self._ack_seen`：仅当 **rid 严格命中本次待确认请求**时置位（每次新连接重置）。超时归类由 `_timeout_error()` 决定：`_ack_seen` → `FeedAckUnverified`；否则 → `FeedSubscriptionError`。
4. `_run()` 中 `FeedAckUnverified` 分支**排在** `FeedSubscriptionError` 之前，进入 `ACK_UNVERIFIED`、`subscribe_failures+1`、`ack_unverified_terminal+1`，**不计 `errors`**（"回了但核实不了"不是网络错误）。
5. 状态机一致性：`start()` 把 `ACK_UNVERIFIED` 视为**本次尝试终态**立即返回 `False`（否则会傻等超时并把 `last_error` 覆盖成"等待超时"，抹掉更准确的诊断）；`_on_loop_exception()` 把 `ACK_UNVERIFIED` 列入"预期终态"，关闭期噪声不计 `errors`。
6. **不发明协议**：没有新增任何猜测性字段名，仍只认 `subRep`/`requestid`，判定逻辑只是"更严格"，不是"更聪明"。

### 三类结果对照（实测）

| 服务端行为 | 状态 | `subscribed_codes` | `errors` |
|---|---|---|---|
| 完整代码清单 | `SUBSCRIBED`（`ack_mode=subRep`） | 2 | 0 |
| 只回 `{"requestid": rid}` | **`ACK_UNVERIFIED`** | 0 | 0 |
| 部分代码 / 被拒 | `FAILED` | 0 | ≥1 |
| 完全不回执 | `FAILED` | 0 | ≥1 |

```json
"C1_ack_unverified": {
  "start_ok": false,
  "state": "ACK_UNVERIFIED",
  "subscribed_codes": 0,
  "ack_unverified_terminal": 1,
  "ack_unverifiable": 1,
  "errors": 0
}
```

`ACK_UNVERIFIED` 下 `is_fresh()` 返回 `(False, "行情连接状态 ACK_UNVERIFIED")`，依赖行情的开仓门槛自然关闭。

---

## 3. B-1：多实例凭证所有权竞争

### 问题
SDK 只从 `os.environ` 读身份密钥，导致同进程多个适配器共享全局状态。旧实现按"单实例 owned 标记"清理：实例 A 释放时会把**实例 B 仍在用的**变量删掉（B 随后拿到空凭证），或反之互相覆盖。

### 修改（`fosun_adapter.py`）

新增进程级 `_CredEnvRegistry`（`RLock` + 单例 `_CRED_ENV`），每个实例持有唯一 `_cred_owner`（`fosun-<12hex>`）：

- `acquire(name, value, owner)`：首个 owner 记录 `preexisting` 后写入环境；后续 owner **只登记引用**；**值不同 → 抛 `CredentialConflict(BrokerError, code="CREDENTIAL_CONFLICT")`，禁止静默覆盖**，且失败的 acquire 不留下引用。
- `release(owner)`：只减自己的引用；**归零才清理**——`preexisting` 非空则恢复旧值，否则删除；删除前核对"环境值仍是我们写入的那个"，避免误删别人后来写入的值；**重复调用幂等**。
- `_init_client()` 把注入放进 `try` 内：冲突 → 走既有 fail-closed 分支（`configured=False`、`release_credentials()`、`blocked_reason` 含 `CredentialConflict`），**不会**带着别人的凭证继续初始化。
- `snapshot()` 只返回"变量名 → 引用数"，`health()` / 日志不含任何值。

### 证据

```json
"B1_cred_refcount": {
  "owners_after_two": 2,
  "owners_after_release_a": 1,
  "env_present_after_release_a": true,   // A 释放后 B 仍可用
  "owners_after_release_b": 0,
  "env_present_after_release_b": false   // 归零才清理
}
```

已覆盖：同值共享（refcount=2）、A 释放不影响 B、归零清理、`preexisting` 恢复、空串 `""` 视为未配置（释放后删除而非留空）、重复释放幂等、冲突 fail-closed 且不接管所有权。

### 诚实边界
注册表是**进程级**的。跨进程（多 worker / 多实例部署）仍共享同一份 `os.environ`，本修复**不解决**跨进程竞争 —— 那需要"一进程一凭证"的部署约束（已在代码注释与本报告标注），不在本轮可改范围（改部署/白名单属禁止项）。

---

## 4. D-1：正常关闭被计为错误

### 问题
服务端以 close code 1000 正常关闭（发布轮换、网关重启）时，`ws.recv()` 抛 `ConnectionClosedOK`，被 `_run()` 的兜底 `except Exception` 当作网络故障：`errors+1`、`last_error` 被写脏。运维据此误判链路不稳，告警信噪比失真。

### 修改（`market/feed.py`）

- 新增 `_close_code(exc)`：先读 `rcvd`/`sent` 帧的 `code`（websockets ≥13 已把 `ConnectionClosed.code` 标 deprecated，直接读会每次关闭刷 DeprecationWarning），读不到返回 `None`，**不按类名猜**。
- 新增 `_is_normal_close(exc)`，判据是**close code + 关闭意图**：
  - `ConnectionClosedOK`（1000/1001）→ 正常；
  - 任意异常但 code == 1000 → 正常；
  - 本端 `_closing`（`stop()` 中）且无可判定 code → 预期关闭；
  - **心跳超时（`ConnectionError`、无 code、非 closing）不是正常关闭**。
- `_run()` 新增正常关闭分支：`normal_closes+1`，**不写 `last_error`、不计 `errors`**；`_closing` → 干净退出到 `STOPPED`；否则仍按退避重连（服务端轮换不该让行情永久停摆），重连同样清 `settled`。
- `_thread_main()` 捕获 `asyncio.CancelledError` → 计 `normal_closes`、`STOPPED`，不再让取消冒泡成线程异常。
- `_recv_loop()` 心跳超时若处于 `_closing`，计 `shutdown_noise` 且不写 `last_error`。
- 新增统计键 `normal_closes`（`snapshot()["stats"]` 自动带出，无需改 Web/health）。

### 证据

```json
"D1_close_metrics": {
  "normal_close":   {"conns": 2, "state": "SUBSCRIBED", "errors": 0, "normal_closes": 1, "last_error": ""},
  "abnormal_close": {"conns": 2, "state": "SUBSCRIBED", "errors": 1, "normal_closes": 0,
                     "last_error": "ConnectionClosedError: received 1011 (internal error) boom; ..."}
}
```

正常关闭后**照常重连并回到 SUBSCRIBED**，`errors` 保持 0、`last_error` 保持空；1011 异常关闭仍然计入 `errors` 并留下原因。

---

## 5. F-2：DEBUG 级别泄露认证头

### 问题
`websockets` 在 DEBUG 级别会输出 `client > <Request ... X-Api-Key: xxx X-Session: yyy>`。原实现把 `feed.ws` 的 logger **钉死在 INFO** 来回避 —— 这是掩盖而非修复：换 logger、换调用路径、或别处把级别调回 DEBUG 就又漏。

### 修改（`market/feed.py`）

1. **删除 `_WS_LOGGER.setLevel(INFO)`**（现为 `NOTSET`，跟随全局）。
2. 新增 `_RedactionFilter(logging.Filter)` + `redact_log_text()`：
   - websockets 的记录是 `msg="%s > %s"` + `args=(side, Request)`，真正拼串发生在 handler 的 `format()` 里，只改 `record.msg` 抹不掉 `args` 里的对象。过滤器**先 `getMessage()` 取到完整文本 → 脱敏 → 以无参形式写回**（`msg=脱敏文本, args=None`），任何 handler/formatter 都拿不到原值；`exc_info` 也预先渲染并脱敏。
   - 规则：`X-Api-Key` / `X-Session` / `ApiKey` / `Session` / `token` / `secret` 等字段 → 值替换为 `***`；`Authorization`/`Cookie`（值可能含空格）整行剩余抹掉；`Bearer xxx` 抹掉；**已登记的字面量**（实际使用过的 key/session 值，≥8 字符，上限 64 条 FIFO）在任何位置出现都抹掉。
   - 脱敏自身 `try/except` 兜底，失败返回 `***`，绝不影响行情线程。
3. 挂载点（3 个）：`ai_trader.market.feed`（应用侧）、`ai_trader.market.feed.ws`（传给 `connect(logger=...)`）、**`websockets.client`（兜底：万一某版本/某路径没接受 `logger=` 参数，库会退回自己的 logger，同样被脱敏）**。
4. `_build_auth_headers()` 取到实际值后调用 `register_feed_secrets()` 登记字面量 —— 只登记、不回读、不落盘。

### 证据

```json
"F2_log_redaction": {
  "log_bytes": 1035,
  "leak_api_key": 0,
  "leak_session": 0,
  "redacted_markers": 2,
  "ws_logger_level": 0
}
```

`ws_logger_level=0` 即 `NOTSET` —— 脱敏来自过滤器，不再依赖级别压制。测试同时验证：`msg+args` 对象路径被脱敏、字面量裸奔被脱敏、真实建连 + 全量 DEBUG 捕获下 API Key / Session / 私钥出现次数为 0、脱敏幂等且不抛异常。

### 诚实边界
脱敏作用于**挂了过滤器的 3 个 logger**。若将来有代码把 `feed_auth_headers()` 的返回值交给**其它** logger 打印，字面量登记不会自动生效（过滤器不在那条链上）。当前全仓无此路径（`feed_auth_headers` 返回值只在调用栈内使用），若新增请一并挂过滤器。

> **Batch 4.1 更新**：上述边界已在 Batch 4.1 修复 —— 脱敏实现统一到
> `src/ai_trader/log_redaction.py`，由 `runtime.setup_logging()` 安装到
> **handler**（覆盖所有 logger 来源）并覆盖之后创建的 handler。
> 本节描述的是 Batch 4 时点的状态，最新状态见 `US_P1_BATCH4_1_FIX_REPORT.md`。

---

## 6. 测试

### 新增 `tests/test_prod_conn_repair_b4.py`（23 例，全通过）

| # | 用例 | 覆盖 |
|---|---|---|
| 1 | `test_provider_not_created_when_gate_closed` | A-1 门禁关闭 → 不创建 provider、不构造 SDK、0 次取 Session |
| 2 | `test_provider_not_created_on_routing_conflict` | A-1 核心：`sdk_type=ops` 冲突 → provider=None 且 **SessionCreate=0** |
| 3 | `test_provider_ready_returns_headers_and_rechecks_gate` | 就绪可取头；`kill_switch` 后立刻拒绝且不再取 Session |
| 4 | `test_provider_raises_when_session_unavailable` | Session 失败 → `FEED_AUTH_UNAVAILABLE`，异常不含内部细节 |
| 5 | `test_two_instances_share_credentials_release_is_refcounted` | B-1 refcount 2→1→0，A 释放不影响 B |
| 6 | `test_release_cleans_empty_preexisting_and_is_idempotent` | 空串视为未配置；重复释放幂等 |
| 7 | `test_release_restores_preexisting_value` | 调用前有值 → 归零时恢复 |
| 8 | `test_conflicting_credential_values_rejected_by_registry` | 冲突抛 `CredentialConflict`，不覆盖、不留引用 |
| 9 | `test_second_instance_with_conflict_fails_closed` | 第二实例 fail-closed，不接管 A 的引用 |
| 10 | `test_requestid_only_ack_is_unverified_not_subscribed` | C-1 核心：RID-only → `ACK_UNVERIFIED`，`errors=0` |
| 11 | `test_full_code_list_ack_subscribes` | 可核实代码清单 → `SUBSCRIBED` |
| 12 | `test_partial_code_list_is_failed_not_unverified` | 部分接受 → `FAILED`（与"无法核实"区分） |
| 13 | `test_no_ack_at_all_is_failed_not_unverified` | 无回执 → `FAILED` |
| 14 | `test_unverified_state_does_not_report_fresh_data` | `ACK_UNVERIFIED` 下 `is_fresh=False` |
| 15 | `test_normal_close_not_counted_as_error` | D-1：1000 关闭 → `errors=0`、`last_error=""`、仍重连成功 |
| 16 | `test_abnormal_close_still_counted_as_error` | D-1：1011 仍计 `errors` 且有 `last_error` |
| 17 | `test_stop_on_live_connection_is_not_an_error` | `stop()` 不计错误 |
| 18 | `test_close_code_helper_does_not_guess` | code 只从异常读，不按类名猜 |
| 19 | `test_ws_logger_no_longer_pinned_to_info` | 级别为 NOTSET；`websockets.client` 也挂过滤器 |
| 20 | `test_redaction_filter_masks_arg_objects` | `msg+args` 对象路径脱敏 |
| 21 | `test_registered_secret_masked_even_without_header_name` | 字面量裸奔也脱敏 |
| 22 | `test_debug_logging_never_leaks_real_connection_headers` | 真实建连 + DEBUG 全量捕获 → 0 泄露 |
| 23 | `test_redaction_is_idempotent_and_never_raises` | 幂等、不抛异常、不误伤正常文本 |

### 既有测试的必要更新（C-1 语义收紧所致，非放宽）

| 文件 | 改动 | 原因 |
|---|---|---|
| `test_prod_conn_repair_b3.py` | `test_unmatched_or_unverifiable_ack_never_subscribes` 拆成 `test_unmatched_ack_never_subscribes[wrong_rid/missing_rid]`（仍 `FAILED`）+ 新增 `test_unverifiable_ack_is_ack_unverified`（`opaque` → `ACK_UNVERIFIED`） | RID 命中但回执不可解释，Batch 3 判 `FAILED`，Batch 4 起细分。**wrong_rid / missing_rid 的断言完全不变**（仍 `FAILED`） |
| 同上 | 泄露用例的 `feed.ws` 级别由 INFO 改为 **DEBUG**，并在 `finally` 还原级别 | F-2 后 DEBUG 已安全，顺手把"DEBUG 泄露"这条路也测掉；同时修掉该用例把全局 logger 级别留在 INFO 的污染 |
| `test_prod_connectivity_p0.py` | `test_opaque_ack_with_matching_requestid_counts` → `..._is_unverified`：断言 `ACK_UNVERIFIED`、`subscribed_codes=0` | 原用例断言的正是被审计判为缺陷的行为（"RID 命中即确认"） |
| 同上 | 重连用例里 `len(srv.subs) >= 2` 改为 `_wait_until(..., 5)` | 订阅报文异步到达，原断言早于报文落地，属测试自身竞态（**非语义变更**） |
| `tests/conftest.py` | 新增 autouse `_isolated_credential_registry`（用例前后 `_CRED_ENV.clear()`） | 进程级注册表是真实生产状态，用例间必须隔离；否则前一例的持有人会让后一例撞上 `CredentialConflict` —— 那是被测代码的**正确行为** |

### 结果

```
tests/test_prod_conn_repair_b4.py                       23 passed
tests/（b3 + p0 + b4）                                  76 passed
tests/ 全量                                499 passed, 1 skipped（59.22s）
```

证据：`audit_evidence/junit_b4.xml`、`audit_evidence/probe_b4.py`、`audit_evidence/probe_b4_result.json`。
运行方式：`PYTHONDONTWRITEBYTECODE=1 .venv/Scripts/python.exe -m pytest tests/ -p no:cacheprovider`（`-p no:cacheprovider` + 不写 `.pyc`，避免给审计留下无关注释文件）。

---

## 7. 十项验收对照

| # | 验收项 | 结果 | 证据 |
|---|---|---|---|
| 1 | 门禁/路由不可用时 **provider 不创建** | ✅ | b4#1/#2；probe `provider_created=false` |
| 2 | 冲突场景 **SessionCreate = 0** | ✅ | b4#2；probe `session_create_calls=0` |
| 3 | 只带 requestid 的回执 **不进入 SUBSCRIBED** | ✅ | b4#10；probe `state=ACK_UNVERIFIED` |
| 4 | 只有**完整代码清单**才 `SUBSCRIBED` | ✅ | b4#11（`ack_mode=subRep`、`subscribed_codes=2`） |
| 5 | A 释放**不影响** B（refcount） | ✅ | b4#5；probe `owners 2→1`, env 仍在 |
| 6 | 不同凭证**不静默覆盖**（fail-closed） | ✅ | b4#8/#9 |
| 7 | 正常关闭 **errors 不增**、不写 `last_error` | ✅ | b4#15/#17；probe `errors=0, last_error=""` |
| 8 | 真实网络异常**仍计入** errors | ✅ | b4#16；probe `errors=1` |
| 9 | DEBUG 下泄露 **0** | ✅ | b4#19–#22；probe `leak_*=0` |
| 10 | 全套测试通过 | ✅ | `499 passed, 1 skipped` |

---

## 8. 未做 / 未验证（诚实标注）

1. **A-1 的"0 次 SessionCreate"是在假 SDK 上以 `get_valid_session` 调用次数证明的**。真实 SDK 内部是否在其它时机（如 `SDKClient.__init__`）发 SessionCreate，本轮未验证 —— 按官方 SDK 代码，SessionCreate 只在 `get_valid_session` 首次取会话时触发；但无凭证环境下无法端到端确认。
2. **官方回执格式仍未验证**（无生产凭证）。C-1 只是把判定改得更严格（宁可不确认），没有任何"猜测字段名"的新逻辑；若真实回执不含 `subRep` 或结构不同，线上会稳定停在 `ACK_UNVERIFIED` 而非误判成功 —— 这是**预期的 fail-closed**，但需要拿到真实回执样本后才能标定。
3. **B-1 不覆盖跨进程**（见 §3 边界）。
4. **F-2 的脱敏范围限于互认的 3 个 logger**（见 §5 边界）；未改动全局 root logger / 第三方 handler。
5. `sdk_type=ops` 与个人接口的**配置冲突本身未解决**（审计 F-9，需人工确认账户类型），本轮只保证"冲突时不发请求、不假装已连接"。
6. 未做联网/生产连通性验证，未重启任何服务。

---

## 9. 回滚

```bash
# 单文件回滚（示例：feed.py）
cp backups/us_p1_b4_20261109-175153/src/ai_trader/market/feed.py src/ai_trader/market/feed.py

# 全量回滚（5 个既有文件）
cd backups/us_p1_b4_20261109-175153 && \
  cp -r src ../../src && cp tests/test_prod_conn_repair_b3.py ../../tests/ && \
  cp tests/test_prod_connectivity_p0.py ../../tests/
```

回滚后应恢复 `476 passed, 1 skipped`（本轮新增 23 例不计），并删除 `tests/test_prod_conn_repair_b4.py`。

---

**本轮到此停止，等待独立只读审计。**
