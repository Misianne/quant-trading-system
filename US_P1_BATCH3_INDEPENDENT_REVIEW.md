# US_P1_BATCH3_INDEPENDENT_REVIEW.md —— Batch 3 独立只读审计

**项目**：`C:\FosunAI-Trading`
**审计对象**：`US_P1_BATCH3_FIX_REPORT.md`（对应备份 `backups/us_p1_b3_20261109-170814`）
**审计时间**：2026-11-09 17:31 – 18:05 (+08:00)
**性质**：**严格只读**。未修改任何源码/配置/测试/依赖/`.env`/vault/数据库；未连接生产；未发送订单或撤单；未改 `sdk_type`/API 前缀；未重生成密钥；未重启服务。
**方法**：直读源码 + 与备份 diff + 自写独立探针（假凭证 / 本机回环 WS / 假 SDK 计数器）+ 现场重跑全量测试。
**口径**：每项分别标注「实现报告声称」与「本轮独立验证」，不以报告结论代替证据。

---

## 0. 总判定

| 组 | 结果 |
|---|---|
| A 路由保护 | **PARTIAL**（六入口 PASS；发现 1 条绕过 `_require()` 的路径；夹具改动削弱生产配置覆盖） |
| B 凭证所有权 | **PARTIAL**（单实例 6/6 PASS；多实例所有权竞态 FAIL） |
| C requestid 严格性 | **PARTIAL**（错误/缺失/重复/迟到/不可解析全部正确拒绝；**仅 requestid 的 ack 仍算确认**） |
| D start() 与异步关闭 | **PARTIAL**（恢复/超时/快速启停/重复关闭/远端关闭均 PASS；**正常关闭被计为 error**） |
| E 私钥与 vault | **PASS** |
| F 日志泄露 | **PARTIAL**（默认 INFO 安全；**动态调到 DEBUG 会打印真实认证头**） |
| G 回归与现场保护 | **PASS**（476/1/0；但 4 个文件发生 LF→CRLF 转换） |

**结论：Batch 3 的主目标（P1 路由旁路）在六个业务入口上确实封堵成功，且有 0 调用证据；但"全部入口"并不成立，且 requestid 严格性仍未满足"只有能确认实际订阅代码才可 SUBSCRIBED"的要求。因此不建议据此进入依赖行情闸门的下一阶段。**

---

## 1. A. 路由保护

### A.1 六个入口：独立计数器验证 —— **PASS**

**报告声称**：六入口全拒绝、SDK 调用 0 次；不依赖 `PAPER_TRADING`。

**独立验证**（自写计数器，`can_send_real_order=(True,'ok')`，`sdk_type=ops`）：

```
门禁 can_send_real_order: (True, 'ok')
client 已构造: True | routing_issue: True
  get_accounts   拒绝   SDK 调用次数=0
  get_summary    拒绝   SDK 调用次数=0
  get_positions  拒绝   SDK 调用次数=0
  get_quote      拒绝   SDK 调用次数=0
  get_klines     拒绝   SDK 调用次数=0
  place_order    拒绝   SDK 调用次数=0
  trade.create_order 累计: 0
```

✅ 与报告一致。`_require()` 中 `routing_issue()` 位于 `can_connect_broker()` 之后、`client is None` 之前，
判据与 `connect()` 完全相同（同一函数），不存在两份实现漂移。

### A.2 其他入口是否都能拒绝 —— **PARTIAL**

| 入口 | 结果 | 备注 |
|---|---|---|
| `get_order` / `list_orders` / `reconcile_order` | 拒绝，0 调用 | ✅ |
| `connect()` | 拒绝，0 调用 | ✅ |
| `cancel_order` | 先过 `can_cancel_real_order()`，再过 `_require()` | ✅（门禁在 paper 下先拒） |
| **`get_quotes`** | **不抛出，返回 `{}`**（0 次 SDK 调用） | ⚠️ 把 `BrokerNotConfigured` 吞进 per-symbol `except`，只打一条 warning。**安全方向正确（未发调用）**，但调用方会得到"没有行情"而非"路由冲突"，可诊断性下降（P3） |

**发现 A-1（P2）：存在一条绕过 `_require()` 的真实请求路径。**

`runtime.build_feed_auth_provider(broker)` 直接访问 `broker.client.auth_manager`，**不经过 `_require()`**：

```python
prov = build_feed_auth_provider(b)      # 冲突状态下依然生成成功
prov()  →  auth.get_valid_session()  →  （无会话时）create_session()
        →  POST https://x.test/api/ops/v1/auth/SessionCreate
```

实测（假 SDK + 计数器，`sdk_type=ops`）：

```
routing_issue 非空: True | client 存在: True
provider 是否生成: True （未经过 _require）
是否发生了 SessionCreate 调用: ['POST https://x.test/api/ops/v1/auth/SessionCreate']
```

即"所有入口均被阻断"不成立：**认证/会话通道**仍会向 `/api/ops` 前缀发出请求。
现实缓解：`market_feed.enabled=false` 时 `build_feed()` 在创建 provider 之前就返回 `None`，
因此当前不会触发；但一旦启用推送且配置仍是 `ops`，就会发出。

### A.3 CLI / worker / monitor / Web 是否有独立调用路径 —— **PASS（fail-closed）**

| 路径 | 结论 |
|---|---|
| CLI `scan` / `monitor` / `cancel` | 全部走 `engine.*`，引擎内部经 `broker.*` → `_require()`；无直连 |
| CLI `demo` | 使用 `PaperBroker`，与真实适配器无关 |
| worker | 只调 `python -m ai_trader.cli monitor/scan`（另有 config guard 前置） |
| Web `/api/positions`、`/api/market/{symbol}`、首页 | **直接调用** `runtime.broker.get_positions/get_quote/get_klines/get_summary`，但都经 `_require()` |

Web 实测（`raise_server_exceptions=False`）：

```
GET /api/positions      status=500 body='Internal Server Error'
GET /api/market/usAAPL  status=500 body='Internal Server Error'
期间 SDK 业务调用: 0 次
```

✅ 未放行、未发调用。⚠️ 但 `BrokerNotConfigured` 被渲染成 **500**，而非结构化 409/503，
运维看不出是配置冲突（P3，诊断性）。

### A.4 `tests/helpers.py` 专项审计 —— **PARTIAL**

**(1) 修改前哈希可验证性 —— PASS**

按报告给出的"精确反向还原"在内存中还原（未改真实文件），得到：

```
反向还原哈希: 2ae41a4f7464ba08…  | 报告声称: 2ae41a4f7464ba08…  | 一致: True
还原后 warn 块出现次数: 4（2 段 × 2 行，符合 batch1 已知重复）
```

报告的 before-hash 是**可复算的**，不是编造的。

**(2) 是否影响生产配置测试 —— PARTIAL（真实存在的影响）**

`live_cfg` 现在**强制** `raw["fosun"]["sdk_type"] = ""`（除非用例显式覆盖）。
因此凡是用 `live_cfg` 的用例都不再反映生产 `config.yaml` 的真实路由状态（`ops`）。

**(3) 是否把生产配置测试伪装成个人接口测试 —— PARTIAL**

* 冲突**行为**有覆盖：`test_prod_connectivity_p0::test_ops_sdk_type_is_reported_as_routing_conflict`
  与 `test_prod_conn_repair_b3` 的三条用例都显式 `sdk_type="ops"`；
* 但**没有任何测试断言"出厂 config.yaml 就是 ops（冲突态）"**。实测 `grep sdk_type tests/` 后，
  只有夹具与显式覆盖，没有对 `load_config()` 结果的断言。
* 后果：若有人把 `config.yaml` 的 `sdk_type` 改掉（或改成 `""`），测试**依然全绿**，
  而生产行为会从"全部拒绝"变成"全部放行"。这是一个**覆盖空洞**（P3，非当前错误）。

另需说明：`tests/helpers.py` **不在**预修复备份内（5 文件之外的第 6 个改动），
报告已单列并给出可复算哈希，属**已披露的计划外改动**，不是隐瞒。

---

## 2. B. 凭证所有权与清理

### B.1–B.5 单实例六种场景 —— **PASS**

| # | 场景 | 独立实测 |
|---|---|---|
| 1 | 空 env + vault 有效 + 构造失败 | `configured=False`，env 三项**全空**（本次注入已清理），`blocked_reason` 不含 PEM/AK |
| 2 | 原 env 有效 + 构造失败 | `configured=False`，env 三项**仍有值**（未误删） |
| 3 | vault 缺失 | `configured=False`，`vault.state=absent` |
| 4 | vault 损坏 | `configured=False`，`vault={'state':'damaged','kind':'corrupt_json'}` |
| 5 | 构造成功 | `configured=True`，env 保留，`_sdk_env_prev` 为空，**不保存 `dump_session`** |
| 6 | `release_credentials()` 只释放本实例拥有 | 自己创建的被 pop；调用前已存在的被保留/写回 |

`blocked_reason` 与 `health()` 均不含任何凭证值（逐字段扫描通过）。

### B.6–B.8 交错实例与所有权竞态 —— **FAIL**

实测（A 从空 env + vault 创建，B 随后创建）：

```
6) A 从空 env+vault 创建: owned=['FSOPENAPI_SERVER_PUBLIC_KEY', 'FSOPENAPI_CLIENT_PRIVATE_KEY']
7) B 随后创建: owned=[] （借用，不拥有）
8) A.release_credentials() 后 env: PUB=空 PRIV=空 API_KEY=空
   → B 仍在运行但凭证已从 env 消失: True
```

**发现 B-1（P3）：所有权是"每实例"的，资源是"进程全局"的。**
A 释放会清掉 B 依赖的环境变量。实际影响有限（`SDKClient` 在构造时已读取凭证，
B 的内存客户端不受影响；只有 B **重建**时才会失败），但这正是审计要求的"所有权竞态"。

另：若两个实例使用**不同 vault**，后者会把前者的值覆盖为"借用"（`prev` 暂存旧值），
成功时丢弃 `prev` → 前者若重建，拿到的是后者的凭证。同属该竞态（P3）。

---

## 3. C. requestid 严格性（重点：报告中的"矛盾"）

### 3.1 矛盾的性质 —— **不是矛盾，是两个不同的输入**

| 报告位置 | 说的输入 | 实测结果 |
|---|---|---|
| §3.4「`opaque` → FAILED」 | `subRep` 存在但**不可解析**（`"not-json{{"`） | 不确认 → 超时 → **FAILED** ✅ |
| §12 R-3「无 subRep 的 opaque ack 仍算确认」 | `subRep` **字段缺失**，仅 `requestid` | **确认**（`ack_mode=requestid`）→ SUBSCRIBED |

两条陈述都成立，**报告内部不矛盾**；但并列时容易误读为"同一个场景"，建议后续澄清措辞。

### 3.2 判定矩阵（独立直测 `_ack_verdict`）—— **PARTIAL**

| 输入 | 结果 | 是否符合"能可靠确认实际订阅代码" |
|---|---|---|
| 正确 RID + 有效 subRep（代码齐全） | 确认 `['rid-1']`，`mode=subRep` | ✅ 符合 |
| **正确 RID + 无 subRep（仅 rid/topic）** | **确认 `['rid-1']`，`mode=requestid`** | ❌ **只证明"服务端收到了这个请求"，未证明任何代码被接受** |
| 正确 RID + 不可解析 subRep | 不确认（`ack_unverifiable`） | ✅ 保守 |
| 正确 RID + 未列代码（`{"qt":[]}`） | 不确认 | ✅ 保守 |
| 正确 RID + 缺代码 | 抛 `FeedSubscriptionError` | ✅ |
| 错误 RID + 有效 subRep | **不确认**（无内容兜底） | ✅ 已修复 F-2 |
| 缺失 RID | 不确认 | ✅ |
| 重复 RID（已确认后） | 不确认 | ✅ |
| 完全拒绝（`errMsg`） | 抛 `FeedSubscriptionError` | ✅ |

### 3.3 端到端（真实本机 WS，只回 requestid）

```
start = True | state = SUBSCRIBED
subscribed_codes = 2 （请求 2 个代码，但回执未列任何代码）
ack_mode = requestid
```

**发现 C-1（P2）：仅 requestid 的 ack 仍进入 `SUBSCRIBED` 并宣称 `subscribed_codes=2`。**
与审计要求"只有能可靠确认实际订阅代码的回执才能进入 SUBSCRIBED；无法确认则标记 UNVERIFIED"不符。
且 `FeedState` **没有 `UNVERIFIED` 状态**（现有：DISCONNECTED/CONNECTING/CONNECTED/SUBSCRIBED/RECONNECTING/STOPPED/FAILED），
只有 `ack_mode="requestid"` 这一弱标记，不足以阻止上层把"已订阅"当作"行情可用"。

现实影响：`is_fresh()` 仍要求真实 tick，故直接交易风险被削弱；但**状态机与告警会失真**，
且若上层以 `state==SUBSCRIBED` 作为行情闸门，会被误导。

---

## 4. D. start() 与异步关闭

| 项 | 实测 | 判定 |
|---|---|---|
| D1 FAILED 恢复 | `start=True`、`state=SUBSCRIBED`、耗时 0.041s、返回值与状态一致 | **PASS** |
| D2 连接超时 | `start=False`、耗时 3.02s（有界）、`errors=1` | **PASS**（附注：采样瞬间 `state=CONNECTING`，未落到 FAILED） |
| D3 连接成功但回执迟到 | 迟到 RID 不确认（见 C）；无回执→超时 FAILED | **PASS** |
| D4 连续快速 start/stop ×5 | 无异常，线程残留 0 | **PASS** |
| D5 重复关闭（stop×3） | `state=STOPPED`，stderr 无 `Traceback` | **PASS** |
| D6 远端先关闭 | `errors=1`、`last_error="ConnectionClosedOK: received 1001 (going away)"`（真实关闭被记录，未吞） | **PASS** |
| D7 本地任务取消 | 代码路径有 `CancelledError` 分支与 `_safe_close`；未构造出可靠的独立用例 | **UNVERIFIED** |
| D8 正常 vs 异常关闭区分 | **正常 `stop()` 后 `errors` 0→1，`last_error` 被写成 `ConnectionClosedOK: sent 1000 (OK)`** | **PARTIAL** |
| D9 资源残留（线程/socket/loop） | 5 组场景后 `market-feed` 线程均为 0；`_ws` 清空；无 traceback | **PASS** |
| D10 分类器是否误吞真实网络错误 | 远端 close 与不可达端口均计入 `errors`；仅 `EOFError("stream ended")` 被静默计数 | **PASS** |

**发现 D-1（P3）：正常关闭被计为 error。**
`_run()` 的通用 `except` 在 `_closing` 判断**之前**就 `errors += 1` 并写 `last_error`，
于是每次正常 `stop()` 都会留下一条"错误"记录。traceback 已消除（原 F-5 已修），
但**指标语义仍然混淆**——运维会把正常停机看成故障。

---

## 5. E. 私钥与 vault —— **PASS**

| 检查 | 结果 |
|---|---|
| 全仓 `clientPrivateKey` / `dump_session` / `_session_dumped` | 仅剩**注释**（`fosun_adapter.py` 两处说明）与**测试**（`test_prod_conn_repair_b3.py` 的假 SDK/断言）；无生产代码保存私钥副本 |
| 内存对象 | `hasattr(broker,'_session_dumped') == False`；`connect()` 不再调用 `dump_session()`（测试断言调用次数 0） |
| vault 四态 | `absent` / `damaged(corrupt_json)` / `unreadable(permission/os_error)` / `ok` 均可区分；`diagnostics()` 只含 `state/kind/exists/note` |
| `get()` 语义 | 与 pre-batch3 对比：真实差异 55 行，**均为新增分类与 `diagnostics()`**，`get()`/`set()`/`delete()` 的返回语义与 fail-closed 行为未变 |
| 异常/诊断/CLI 含秘密 | `blocked_reason`、`health()`、`check-env`（5358B 输出）均 0 命中假 token |

---

## 6. F. 日志泄露 —— **PARTIAL**

| 路径 | 默认（INFO） | 把 `ai_trader.market.feed.ws` 调到 DEBUG |
|---|---|---|
| 应用侧日志（`ai_trader*`） | 0 行含认证头 | **2 行含认证头，且含真实值** |
| snapshot / health | 无值 | 无值 |
| asyncio / traceback | 无 | 无 |
| SQLite / JSON 状态 / 故障流水 / CLI | 无（前轮与本轮 token 扫描均 0 命中） | — |

DEBUG 级别实测（值已替换为占位符）：

```
ai_trader.market.feed.ws|DEBUG|> X-Api-Key: <K-REDACTED>     包含真实 K: True
ai_trader.market.feed.ws|DEBUG|> X-Session: <S-REDACTED>     包含真实 S: True
```

**发现 F-1（P2）：防泄露仍是"日志级别"而非"脱敏过滤器"。**
实现报告 §12 R-1 已承认并列为延后项；本轮**只加了版本约束**（`websockets>=13,<18`，当前 17.4），未加 Filter。

**发现 F-2（P3）：他们的泄露测试无法检出该场景。**
`test_no_secret_in_logs_snapshot_or_health` 只把 handler 挂在 `ai_trader` logger 上，
并**显式把 `ai_trader.market.feed.ws` pin 到 INFO**。因此它证明的是"默认配置安全"，
不是"没有泄露路径"。测试给人"已覆盖"的错觉。

**关于第三方升级**：`connect(..., logger=...)` 参数在 17.4 存在；约束 `<18` 后，
若上游在大版本内移除该参数，注入会静默失效并回落到 `websockets.client`（DEBUG 时会打印头）。
**逐版本行为：UNVERIFIED**（未做多版本矩阵验证）。

---

## 7. G. 回归与现场保护 —— **PASS（含 1 项格式缺陷）**

| 检查 | 结果 |
|---|---|
| 全量测试 | **476 passed, 1 skipped, 0 failed**（现场重跑，不引用实现者日志） |
| 三项开关 | `LIVE_TRADING=False`、`ORDER_EXECUTION_ENABLED=False`、`PAPER_TRADING=True`、`KILL_SWITCH=False`；`can_send_real_order=(False,…)` |
| `sdk_type` / `base_url` / 推送 URL | `'ops'` / `''` / `''`（`market_feed.enabled=False`）——**均未被改动** |
| `.env` | `c914b6c3b58158a9…` mtime `2026-11-08 11:57:25`（未变） |
| `data/audit.db` | `b826768c85b8e949…` mtime `2026-11-08 13:00:21`（未变） |
| `data/vault.json` | 不存在 |
| `config/config.yaml` | `fe744191bc9fbeea…` mtime `12:38:02`（未变） |
| `config/config.template.yaml` | `1759ea36015b3124…`（未变） |
| SDK 26 文件 | 与基线**0 不一致** |
| 计划内 5 文件 + `helpers.py` | 前后哈希与 `MANIFEST.after` **7/7 一致** |
| 计划外文件变化 | **无**（17:05 后改动仅 6 个，全部已声明） |
| 真实生产连接/业务请求 | 无：`base_url=''`、feed url `''`、全部探针走 `127.0.0.1` 或假 SDK；测试中唯一非回环地址是既有用例里的 `http://x/...` 假 session |

**发现 G-1（P3）：4 个文件发生 LF→CRLF 换行符转换。**

| 文件 | 备份 CRLF | 现 CRLF |
|---|---|---|
| `src/ai_trader/secrets.py` | 0 | 390 |
| `src/ai_trader/broker/fosun_adapter.py` | 0 | 1143 |
| `requirements.txt` | 0 | 24 |
| `tests/helpers.py` | 0（batch2 备份） | 217 |

后果：`diff` 显示整文件重写（例如 `secrets.py` 真实差异仅 **55 行**，却被呈现为 347→390 行全改），
严重降低可审查性；Python/YAML 功能不受影响。属流程/格式缺陷。

---

## 8. 实现报告声明 vs 独立验证

| 报告声明 | 独立验证 | 差异 |
|---|---|---|
| 六入口 6/6 拒绝、SDK 调用 0 | ✅ 完全一致（含 `can_send_real_order=True`） | 无 |
| `place_order` 拒绝时 `trade.create_order` 计数 0 | ✅ 一致 | 无 |
| 凭证清理"所有权隔离" | ⚠️ 单实例成立，多实例**不成立** | 报告未提单实例前提 |
| `opaque` → FAILED | ✅ 对"不可解析 subRep"成立 | 报告 §12 R-3 已披露另一场景，但两处措辞易混 |
| `start()` 返回值与状态一致 | ✅ 一致（0.041s，非 0 秒） | 无 |
| 正常关闭无未处理 traceback | ✅ 成立 | 但 `errors` 被写脏（报告未提） |
| `dump_session` 已移除 | ✅ 全仓无生产代码引用 | 无 |
| vault 四态区分 | ✅ 一致 | 无 |
| 泄露面"0 命中" | ✅ 对默认 INFO 成立 | 报告未强调"仅默认级别" |
| 全量 476 passed | ✅ 一致 | 无 |
| 变更 6 文件（含 helpers） | ✅ 一致，且无计划外文件 | 未提换行符转换 |

---

## 9. 阻断项 / 可延后 / 需确认

### 9.1 必须修复的阻断项

| # | 项 | 级别 | 理由 |
|---|---|---|---|
| **C-1** | 仅 requestid 的 ack 仍进入 `SUBSCRIBED` 并宣称全部代码已订阅 | **P2（阻断下一阶段）** | 与"只有能确认实际订阅代码才可 SUBSCRIBED"直接冲突；缺 `UNVERIFIED` 状态 |
| **A-1** | `build_feed_auth_provider` 绕过 `_require()`，冲突下仍会 `POST /api/ops/v1/auth/SessionCreate` | **P2（阻断）** | 使"全部入口阻断"不成立；启用推送即可触发 |

### 9.2 可以延后的风险

| # | 项 | 级别 | 说明 |
|---|---|---|---|
| B-1 | 多实例所有权竞态（A 释放清空 B 的 env） | P3 | 当前进程只建一个 broker；影响限于"重建" |
| D-1 | 正常关闭被计为 error、`last_error` 被写 | P3 | 指标/排障误导，不影响安全 |
| F-1 | DEBUG 级别泄露认证头 | P2 | 需加 `logging.Filter` 脱敏（本轮明令不加） |
| F-2 | 泄露测试只覆盖默认级别 | P3 | 测试假阳性 |
| A-2 | `get_quotes` 吞掉路由错误返回 `{}` | P3 | 安全方向正确，仅诊断性 |
| A-3 | Web 直接调用被拒时返回 **500** 而非 409/503 | P3 | fail-closed 正确，UX/告警语义差 |
| G-1 | 4 文件 LF→CRLF | P3 | 可审查性 |
| A-4 | 无测试断言出厂 `config.yaml` 的 `sdk_type=ops` | P3 | 覆盖空洞：改配置不会被打红 |
| D-2 | `max_reconnect_attempts` 耗尽瞬间 `state` 可能停在 `CONNECTING` | P3 | 采样竞态，非安全 |

### 9.3 需向星财富确认

1. `subRep` 是否**回显 requestid**？是否逐主题/逐代码列出接受结果？
2. 只回 `requestid` + `topic:"sub"` 的回执，是否等价于"全部代码订阅成功"？（决定 C-1 能否用官方语义支持当前实现）
3. 回执是否可能"接受但未列代码"？此时语义是什么？
4. `SessionCreate` 是否属于"个人接口"？（决定 A-1 的严重级别）
5. 409（重复连接）的阈值与清理策略；`X-Session` 生命周期与可复用范围。
6. `websockets` 目标版本区间内，`connect(logger=...)` 是否稳定存在。

### 9.4 是否允许进入下一阶段

**不允许直接进入"依赖行情闸门"的下一阶段。**
理由：C-1（订阅确认强度不足）与 A-1（认证通道绕过路由守卫）都直接影响"行情是否可信"，
且两者都在本轮修复的语义范围内。

**允许的前提**：先修 C-1 与 A-1（或从配置层禁止 `ops + 启用推送` 的组合），
其余 P3 项可随下一批处理。

---

## 10. 只读声明

* 未修改任何源码、配置、测试、依赖、`.env`、vault、数据库（受影响 7 文件与 `MANIFEST.after` **逐一致**）。
* 未连接生产网关：所有探针使用 `127.0.0.1` 回环服务、假 SDK 或 `https://x.test` 占位域（被 mock 拦截，不发包）。
* 未发送订单/撤单；未改 `sdk_type`/API 前缀；未重生成密钥；未重启服务。
* 本报告不含任何真实 PEM、APIKEY 或 Session 值（探针使用 `PROBE-*` / `F-*-LEAKCHK-*` 假串并在输出中替换为占位符）。
