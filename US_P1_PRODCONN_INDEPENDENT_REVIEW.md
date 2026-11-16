# US_P1_PRODCONN_INDEPENDENT_REVIEW.md —— 生产接入修复 独立只读审计

**项目**：`C:\FosunAI-Trading`
**审计对象**：本轮 "Production Connectivity" 修复
**基线**：`backups/us_p1_prodconn_20261109-163216/`（pre-fix 8 文件 + post-fix 9 文件两份 MANIFEST）
**审计时间**：2026-11-09 16:51 – 17:25 (+08:00)
**性质**：**严格只读**。未修改任何源码/配置/测试/`.env`/vault/数据库；未连接生产环境；未发送任何业务请求；未重启服务；未重新生成密钥；未改变交易开关。
**方法**：读源码 + 与备份 diff + 现场跑全量测试 + **自行编写独立探针**（本机回环 WS/HTTP，全部假凭证，不输出任何真实密钥值）。
**口径**：不采信实现者报告的任何结论；每个判定均附本轮实测证据。

---

## 0. 总判定

**PARTIAL —— 不能据此得出"生产接入可上线"的结论。**

| 组 | 结果 |
|---|---|
| A. 代码核实（12 项） | **8 PASS / 3 PARTIAL / 1 FAIL** |
| B. 泄露面（7 条路径） | **PASS**（其中 1 条依赖日志级别，属脆弱缓解） |
| C. 生产路由（4 项） | **3 PASS / 1 FAIL** |
| D. 回归与现场保护（5 项） | **全部 PASS** |

**三条未达成项（必须处理）**：

1. **C.3 / FAIL / P1**：`sdk_type=ops` 时**只有 `connect()` 被阻断**；`get_accounts / get_summary / get_positions / get_quote / get_klines / place_order` 仍会向 `/api/ops/...` 发出个人接口请求 —— 实测 **6/6 全部放行**。
2. **A.3 / FAIL / P2**：SDK 构造失败后，**已注入 `os.environ` 的身份私钥不会被清理**（实测仍残留）。
3. **A.9 / FAIL / P2**：**错误 `requestid` 的订阅回执会被当作确认**，feed 进入 `SUBSCRIBED`（实测 `ack_mode=subRep`）。

**另需注意**：当前生产配置本身就处于路由冲突状态（`fosun.sdk_type: "ops"`，而适配器只调用个人接口），见 F-9。

---

## 1. 现场与基线核对

| 检查 | 结果 |
|---|---|
| 备份目录 | `backups/us_p1_prodconn_20261109-163216/` 存在，含 pre/post 两份 MANIFEST |
| pre-fix 基线 | 8 文件（`fosun_adapter.py`、`feed.py`、`runtime.py`、`cli.py`、`config.py`、`secrets.py`、`config.yaml`、`test_market_feed_b5.py`） |
| post-fix 清单 | 9 文件（新增 `tests/test_prod_connectivity_p0.py`，并记录 total=445/failed=0/errors=0/skipped=1/new_file_cases=21） |
| 当前工作区 vs post 清单 | **9/9 完全一致**（审计期间工作区未被改动） |
| 实际变更（pre→post） | `fosun_adapter.py`、`feed.py`、**`runtime.py`**、**`test_market_feed_b5.py`**、新增测试文件 |
| 与任务描述一致性 | ⚠️ 任务仅列 3 个文件，实际 **`runtime.py` 与 `test_market_feed_b5.py` 亦被修改**（见 F-8） |
| 备份范围 | 仅 8 个定点文件；未含 `audit_evidence/`、`.env`、`data/`（符合"不含密钥材料"声明） |

---

## 2. A. 逐项核实（12 项）

### A.1 空环境变量不会覆盖 vault 凭证 —— **PASS**

独立探针：`FSOPENAPI_SERVER_PUBLIC_KEY/CLIENT_PRIVATE_KEY/API_KEY` 全部设为 `""`，vault 提供有效值：

```
configured: True
credential_origin: {PUB: vault, PRIV: vault, AK: vault, FOSUN_BASE_URL: config}
```

`_env_value()` 明确区分 `None`（不存在）/ `""`（存在但空）/ 有效值，`if from_env:` 使空串不阻断 vault 注入。
对照旧实现 `os.environ.setdefault(...)`（对"已存在但为空"的键无效）—— P0-1 根因确已修掉。

### A.2 vault 缺失 / 解析失败 / 凭证为空 → fail-closed —— **PASS**（诊断不足另记）

| 场景 | 实测 |
|---|---|
| vault 文件不存在 + env 空 | `configured=False`，`blocked_reason=缺少凭证: …`，`_require()` 抛 `BrokerNotConfigured` |
| vault 文件内容损坏（`{ broken json`） | `SecretVault.get()` 返回 `None`（`_read_raw` 吞掉 `ValueError`）→ 同上 fail-closed |
| `vault.get()` 抛异常 | 被捕获，仅记录 `type(exc).__name__`（**不含异常正文**），→ missing → fail-closed |

`blocked_reason` / `health()` 均**不含任何密钥值**（探针逐字段扫描确认）。

⚠️ 诊断缺口：vault 文件损坏与"vault 里本来没配"在现象上完全相同（都只报 `缺少凭证`），运维无法区分。见 F-6。

### A.3 SDK 构造失败是否清理已注入的敏感环境变量 —— **FAIL**

`_inject_sdk_env()` 在 `try` **之外**执行；`except` 分支只做 `configured=False / client=None / blocked_reason=…`，**没有 `os.environ.pop()`**。

实测（`FakeSDK.__init__` 抛 `RuntimeError`）：

```
configured: False
env 中 PUB 仍在: True
env 中 PRIV 仍在: True      ← 私钥残留
blocked_reason 含密钥值: False
```

后果：构造失败后，进程环境仍持有**私钥 PEM**，直到进程退出；若随后 fork/子进程会被继承。属纵深防御缺陷，非直接外泄。见 F-1。

### A.4 `health()` 来源审计是否准确 —— **PASS**

* `health()` 调用 `_resolve_credentials(update_origin=False)`，避免"把初始化时自己注入 env 的值误判成 env 来源"。
* 实测：vault 注入场景 `credential_origin` 全为 `vault`；env 优先场景 PUB=`env`；全缺失为 `missing`；`FOSUN_BASE_URL` 为 `config`（env 为空时）。
* 新增字段 `api_prefix` / `routing_issue` / `sdk_env_injected`（只有变量名）均无值泄露。

⚠️ 一处副作用：`health()` 会**再次读取 vault**（探针中可见重复的 vault 告警），不是纯只读查询；若 vault 读取昂贵/加锁，`health()` 频繁调用会有开销。非安全问题。

### A.5 WebSocket 每次连接是否使用最新 Session —— **PASS**

* `_connect_and_serve()` 每次进入都 `await asyncio.to_thread(self._build_auth_headers)`（**每次建连现取**，不缓存）。
* `build_feed_auth_provider()` 返回闭包，内部调 `auth.get_valid_session()`（SDK 内部按 `expires_at - 60` 自动刷新）。
* 实测 `test_reconnect_refreshes_session_and_resubscribes`：重连后服务端收到的 `X-Session` == 新 Session，`len(set(sessions)) >= 2`。
* 我另做的独立重连探针同样观察到「重连 → 重新取 Session → 重新订阅」。

### A.6 认证头是否只进连接层、不进日志/异常/状态/故障 —— **PASS（缓解脆弱）**

在 root logger 与 `websockets.*` 全部设为 DEBUG 的真实本机 WS 握手中：

| 通道 | 结果 |
|---|---|
| 本进程作为 **client** 的 `websockets.client` 日志 | 0 条（因注入自定义 `logger=_WS_LOGGER`，级别 INFO，`debug=False`） |
| `ai_trader.market.feed.ws` 日志（默认） | 0 条；无 `> X-Api-Key` |
| `snapshot()` | 无任何头值（只有 `required / provider_configured / failures`） |
| `last_error` | 无头值（401 等只输出"提示语"） |
| 落盘产物（`audit.db` / `audit.jsonl` / `stops.json` / `faults.jsonl` / `alerts.log`） | 无 token |

⚠️ **脆弱点**：把 `logging.getLogger("ai_trader.market.feed.ws").setLevel(DEBUG)` 后，**立即复现** `> X-Api-Key: <明文>` —— 说明缓解手段是"日志级别"，不是"脱敏过滤器"。另：该注入依赖 `connect(..., logger=...)` 参数存在（17.4 有），而 `requirements.txt` 只写 `websockets>=10.0`（未锁版本）。见 F-7。

> 说明：探针中服务端侧（`websockets.server`）会打印它**收到**的头，那是测试替身自身的日志，不是被测进程的外泄路径。

### A.7 websockets 17.4 `additional_headers` 调用是否正确 —— **PASS**

```
websockets.__version__ = 17.4
connect 参数: [... 'additional_headers', ... 'extra_headers' 不存在 ...]
```
`_header_kwarg_name()` 通过 `inspect.signature` 探测，17.4 命中 `additional_headers`；
且头名与官方 SDK `market_push.py` **完全一致**：`X-Api-Key` / `X-Session` / `X-Request-Client`（大小写一致）；
真实握手中服务端确实收到三者（长度校验非空）。

### A.8 400 / 401 / 403 / 409 是否正确停止重试 —— **PASS**

本机 WS 服务在握手阶段返回各状态码，实测：

| 状态 | start() | state | auth_failures | 是否重试 | 提示语 | 含密钥值 |
|---|---|---|---|---|---|---|
| 400 | False | FAILED | 1 | 否 | 请求参数缺失（X-Api-Key / X-Session 必填） | 否 |
| 401 | False | FAILED | 1 | 否 | 鉴权失败（…无效/过期） | 否 |
| 403 | False | FAILED | 1 | 否 | 行情权限验证失败 | 否 |
| 409 | False | FAILED | 1 | 否 | 该账号重复连接过多 | 否 |

`_is_auth_rejection()` 同时看 `status_code` 与消息文本中的 4xx 数字，然后转 `FeedAuthError` → `_run` 直接 `FAILED` 并 `return`（不进重连分支）。

### A.9 订阅拒绝 / 部分确认 / 超时 / **错误 requestid** 是否都不标记 SUBSCRIBED —— **FAIL（错误 requestid）**

| 场景 | 实测 | 判定 |
|---|---|---|
| 服务端 `errMsg` 拒绝 | `FAILED`，`last_error` 含"拒绝" | PASS |
| 只接受部分代码 | `FAILED`，"未被全部接受" | PASS |
| 完全不回执 | `FAILED`，"订阅确认超时" | PASS |
| `subRep` 无 requestid 但有 `act/topic` | 命中 pending rid 才确认 | PASS |
| **回执 requestid 错误（`WRONG-RID-0000`）+ 内容匹配** | **`SUBSCRIBED`，`ack_mode=subRep`** | **FAIL** |

根因在 `_ack_verdict()`：当 `rid not in pending` 时执行 `targets = dict(pending)`（按内容匹配全部待确认项），因此"requestid 不对但 subRep 内容对得上"会被当作确认。
影响：`state` 误报为 `SUBSCRIBED`；因 `is_fresh()` 还要求真实 tick，交易影响被削弱，但**状态机说谎**本身违反要求 9。见 F-2。

### A.10 `settle_updates` 在重连/重复/乱序/异常消息下是否符合配置 —— **PASS**

| 场景 | 实测 |
|---|---|
| `settle_updates=2`，第 1 条有效行情 | `settled=[]`，`count=1`，`is_fresh=False`（原因含 settle） |
| 再来一条**同 ts** | `count` 不变，`duplicates=1`（不计入） |
| 再来一条**更旧 ts** | `count` 不变，`out_of_order=1`（不计入） |
| 第 2 条有效（新 ts） | `settled=['hk00700']`，`count=2` |
| 客户端重连 | `settled=[]`、`counts={}`（清零，必须重新积累） |
| 心跳超时触发重连 | 同上 |
| 异常消息（坏 JSON / 缺 ts） | `bad_payload` / `ts_missing` 计数，**不进入 settle** |

`need = max(1, int(settle_updates or 1))`：0/负值退化为 1，不会"永不 settle"。

### A.11 `FAILED` 状态能否被正确恢复 —— **PARTIAL**

| 观察 | 结果 |
|---|---|
| 是否永久卡死 | **否**：`_run` 返回后线程退出，再调 `start()` 会重新起线程并最终 `SUBSCRIBED` |
| 恢复是否需要外部动作 | 是。认证/订阅类 FAILED **不自动重试**（刻意的 fail-closed） |
| **`start()` 返回值是否可信** | **否**：状态已是 FAILED 时，`start()` 看到 `state in (SUBSCRIBED, FAILED)` 立刻 break，**0.00s 返回 False**，而新线程随后成功进入 `SUBSCRIBED` |

实测：

```
已是 FAILED 状态时 start() 返回: False（耗时 0.00s）
之后实际 state: SUBSCRIBED
→ 返回值与实际状态是否一致: False
```

影响：调用方若用返回值决定"是否启用行情闸门"，会把一次**成功**的恢复误判为失败。见 F-4。

### A.12 新增异常路径与资源关闭 —— **PARTIAL**

| 检查 | 结果 |
|---|---|
| `market-feed` 线程 | 启动 1 个，`stop()` 后 0 个（无泄漏） |
| `_ws` 引用 | `stop()` 后清空 |
| `_safe_close` | 在 `_connect_and_serve` 的 `finally` 中调用 |
| 事件循环 | `_thread_main` 的 `finally` 中 `loop.close()` |
| **关闭期异常** | **复现未捕获 traceback**：`Exception in callback _ProactorBasePipeTransport._call_connection_lost()` → `websockets/streams.py: feed_eof() → EOFError: stream ended`，打印到 stderr |
| 连接失败场景终态 | `max_reconnect_attempts=1` 时 `start()` 返回 False，采样到的 `state` 为 `CONNECTING`（非终态），`stop()` 后才变 `STOPPED` |

EOFError 不含任何凭证，但会在 stderr 刷出 traceback，可能掩盖真实错误，也让"日志干净"这一诉求打折。见 F-5。

---

## 3. B. 泄露面检查（逐条）

| 路径 | 结果 | 证据 |
|---|---|---|
| Python logging | **PASS（默认）** | root+websockets 全 DEBUG 时，`ai_trader.market.feed.ws`（INFO）0 行；client 侧无 `X-Api-Key` |
| websockets DEBUG logger | **PASS（默认）/ 脆弱** | 把该 logger 调 DEBUG 即复现明文头（F-7） |
| traceback | **PASS（无凭证）** | 唯一的 traceback 是关闭期 `EOFError: stream ended`，内容为库内部帧；SDK 初始化异常只记录 `type+msg`（不含凭证值） |
| HTTP/WebSocket 异常文本 | **PASS** | 4xx 全部替换为固定提示语；`last_error` 不含头值 |
| SQLite | **PASS** | 用 token 跑真实 monitor/scan 后扫描 `audit.db`/`audit.jsonl`，无 token |
| JSON 状态文件 | **PASS** | `stops.json` 无 token；feed `snapshot()` 无头值 |
| 故障流水 | **PASS** | `faults.py` 记录显式字段（event/client_id/error 文本），不 dump 环境；token 扫描未命中 |
| CLI 输出 | **PASS** | `ai_trader.cli check-env`（注入假 PEM/假 AK）输出 5209 字节，**不含任何 token 片段** |
| 测试失败输出 | **PASS** | 新增测试全部使用 `fake-api-key-for-tests` / `header-api-key-9f3a` 等占位串 |

补充：`health()` 26 个字段逐字段扫描，`server_public_key`/`client_private_key` 经 `redact_pem` 后**不含密钥正文**。

**额外发现（潜在，不在上述 7 条内）**：`connect()` 成功后 `self._session_dumped = client.dump_session()`，而 SDK 的 `dump_session()` 返回 **含 `clientPrivateKey` 明文 PEM** 的字典。该字段在全仓**无任何读取方**（死字段），从未落盘/入日志，但会在内存中长期持有私钥副本且无清理。见 F-10。

---

## 4. C. 生产路由

### C.1 SDK README 个人 vs OPS 区分 —— **PASS（已核实原文）**

`sdk_src/README.md` L76–L86、L130：

* 不设 `SDK_TYPE` → `/api/v1/...`；`SDK_TYPE=ops` → `/api/ops/v1/...`
* **个人用户**：`client.trade` / `portfolio` / `account`（个人路径）
* **OPS 配对**：`client.opsTrade` / `opsPortfolio` / `opsAccount`，且 `apply_account_id` 必填、须 `SDK_TYPE=ops`
* 原文明确："请勿再用个人 `client.trade.*` 拼 OPS 配对参数"

### C.2 适配器实际调用的接口族 —— **PASS（全部个人入口）**

```
account.list_accounts      portfolio.get_assets_summary   portfolio.get_holdings
market.quote / min / kline trade.create_order / cancel_order / get_order_detail / list_orders
```

无任何 `opsTrade` / `opsPortfolio` / `opsAccount`。

### C.3 `sdk_type=ops` 时是否所有入口均被阻断 —— **FAIL**

`routing_issue()` 只在两处被调用：`connect()` 与 `health()`。`_require()`（所有数据/交易入口的守卫）**不检查** `routing_issue()`；`_init_client()` 也不因冲突而拒绝构造。

用假 SDK（记录调用，不联网）在 `sdk_type=ops` 下逐个调用公共入口：

```
client 已构造: True | routing_issue: True
connect()               → 阻断 = 是（BrokerNotConfigured）
get_accounts            → 阻断 = 否   调用=['account.list_accounts']
get_summary             → 阻断 = 否   调用=['portfolio.get_assets_summary']
get_positions           → 阻断 = 否   调用=['portfolio.get_holdings']
get_quote               → 阻断 = 否   调用=['market.quote']
get_klines              → 阻断 = 否   调用=['market.kline']
place_order             → 阻断 = 否   调用=['trade.create_order']
```

即：**只有建连被拦，读写与下单一律放行**，会被打到 `/api/ops/v1/...` 且携带个人接口的请求体。
当前被 `PAPER_TRADING`/`LIVE_TRADING=false` 两道门禁掩盖，但只要有人"补上凭证 + 打开实盘"，就会真发出去。见 F-3（最高优先级）。

### C.4 是否擅自改成 `sdk_type=normal` 或 `/api` —— **PASS（未擅改）**

`config/config.yaml` 与 pre-fix 基线**逐字节一致**（`fe744191…`），仍为 `sdk_type: "ops"`；
适配器只"报告冲突"，未自动切换前缀，符合"不允许自动改"的要求。
但正因为没改，**生产配置现在就是冲突态**（`api_prefix=/api/ops`、`routing_issue` 非空）。见 F-9。

---

## 5. D. 回归与现场保护

### D.1 完整测试结果 —— **PASS**

```
$ python -m pytest tests/ -o addopts="" -q -p no:cacheprovider
444 passed, 1 skipped, 1 warning in 38.82s

$ python -m pytest tests/test_prod_connectivity_p0.py -q
21 passed in 2.60s
```

与 post-fix MANIFEST 记录（total 445 / failed 0 / errors 0 / skipped 1 / new_file_cases 21）**完全一致**。
跳过项为"未配置 AI Key"，合理。

### D.2 新增测试是否覆盖真实调用链 —— **PARTIAL（覆盖充分但有一处缺口）**

21 个用例中：

| 类别 | 数量 | 说明 |
|---|---|---|
| **真实本机 WS/HTTP 链路**（`PushServer` + 真实握手） | **11** | 含握手认证、4xx、无回执、拒绝、部分确认、opaque、重连换 Session |
| 直接调用凭证/路由/配置 | 10 | 凭证解析、health、ops 冲突、feed 默认值、settle 计数（2 个） |

‑ 缺口：`settle_updates` 的 2 个用例是**直接调 `ingest()`**（不建连），未覆盖"重连后由真实推送流重新积累 settle"；重连用例虽真实，但未断言 settle 清零。
‑ 缺口：`test_ops_sdk_type_is_reported_as_routing_conflict` **只断言 `connect()` 被拒**，恰好漏掉 C.3 的"其他入口未阻断"（测试与实现同源缺陷）。

### D.3 三个交易安全开关 —— **PASS**

```
LIVE_TRADING             = False
ORDER_EXECUTION_ENABLED  = False
PAPER_TRADING            = True
KILL_SWITCH              = False
can_connect_broker       = (False, 'PAPER_TRADING=true，真实券商连接被禁用')
can_send_real_order      = (False, 'PAPER_TRADING=true，真实券商连接被禁用')
```

### D.4 数据库 / vault / .env / 配置 / SDK 的 SHA256 与时间戳 —— **PASS**

| 文件 | SHA256(前16) | mtime | 说明 |
|---|---|---|---|
| `data/audit.db` | `b826768c85b8e949` | 2026-11-08 13:00:21 | 与上一轮一致，未被打开写 |
| `.env` | `c914b6c3b58158a9` | 2026-11-08 11:57:25 | 未改动 |
| `data/vault.json` | **不存在** | — | 无机密可泄露 |
| `config/config.yaml` | `fe744191bc9fbeea` | 2026-11-09 12:38:02 | **与 pre-fix 基线一致**（本轮未改配置） |
| `src/.../fosun_adapter.py` | `3d291e1f204d3bdb` | 2026-11-09 16:40:22 | 与 post 清单一致 |
| `src/.../market/feed.py` | `2664faf272d58a51` | 2026-11-09 16:38:08 | 同上 |
| `tests/test_prod_connectivity_p0.py` | `59d6b400426e5338` | 2026-11-09 16:40:36 | 同上 |
| **SDK 26 文件** | 与 `sdk_baseline_us_p1_b1.json` | — | **不一致 0**（SDK 未改） |

### D.5 是否有真实账户连接 / 订单请求 / 生产业务请求 —— **PASS**

* `fosun.base_url == ""`、`market_feed.url == ""` → 无生产端点可打。
* `market_feed.enabled == False` → 生产路径当前根本不启动推送。
* 门禁：`can_connect_broker/can_send_real_order` 均为 `False`（PAPER_TRADING）。
* 新增测试的所有 URL 均为 `127.0.0.1` 或 `.test` 占位域；未出现真实网关。
* 审计全程我只调用本机回环服务与假 SDK，未发起任何外部请求。

---

## 6. 缺陷清单（含复现步骤与影响面）

> 影响面三列：**新开仓 / 持仓退出 / 重启恢复**。「—”= 不影响。

### F-1（P2）SDK 构造失败不清理已注入的身份环境变量 —— **A.3 FAIL**

* **复现**：`FSOPENAPI_SERVER_PUBLIC_KEY/CLIENT_PRIVATE_KEY` 置为有效值 → 让 `SDKClient(...)` 抛异常（或触发 `unreadable` 检查）→ 检查 `os.environ`。
* **现象**：`configured=False`、`client=None`，但两个身份变量**仍在 `os.environ`**（实测 `True/True`）。
* **影响**：失败后进程内长期持有私钥副本；子进程继承。不直接外泄到日志/文件。
* **新开仓/持仓退出/重启恢复**：— / — / —
* **建议**：`except` 分支或 `finally` 中按 `self._sdk_env_injected` 逐个 `pop`（并清空列表）。

### F-2（P2）错误 `requestid` 的订阅回执被当成确认 —— **A.9 FAIL**

* **复现**：本机服务对 `act=sub` 回 `{"requestid":"WRONG-RID-0000","subRep":"{...正确内容...}"}`。
* **现象**：`feed.start()` 返回 `True`、`state=SUBSCRIBED`、`ack_mode=subRep`。
* **影响**：订阅状态可能"名义已订、实际未订"；`is_fresh()` 仍要求真实 tick，故对交易的直接冲击有限，但状态机与告警会失真。
* **新开仓**：间接（若上层以 `state==SUBSCRIBED` 作为"行情可用"依据）｜**持仓退出**：— ｜**重启恢复**：—
* **建议**：`rid` 不在 `pending` 时不要用内容兜底；或仅在"服务端明确不回声 requestid"且 `act` 明确为订阅确认时，才允许**单主题**且 `len(pending)==1` 的内容匹配。

### F-3（P1）`sdk_type=ops` 只在 `connect()` 阻断，数据/下单入口仍放行 —— **C.3 FAIL**

* **复现**：`fosun.sdk_type: ops` + `apply_account_id` 已配 + 凭证齐备（可用假 SDK）→ 依次调用 `get_accounts/get_summary/get_positions/get_quote/get_klines/place_order`。
* **现象**：6/6 均向 SDK 发出调用（`account.list_accounts`…`trade.create_order`），仅 `connect()` 抛 `BrokerNotConfigured`。
* **影响**：路由冲突下会向 `/api/ops/v1/...` 发送个人接口请求；极端情况下可能被网关按另一套语义处理。实盘打开后即为真实请求。
* **新开仓**：**是**（`place_order` 可达）｜**持仓退出**：**是**（`cancel_order`/`list_orders` 同样只走 `_require`）｜**重启恢复**：—
* **建议**：把 `routing_issue()` 下沉到 `_require()`（或 `_require` 内加一行检查），使**所有**入口一致阻断；`connect()` 保留原样。

### F-4（P2）FAILED 后 `start()` 返回值不可信 —— **A.11 PARTIAL**

* **复现**：`feed._set_state(FAILED)` → `feed.start(timeout=6)`，服务端正常。
* **现象**：`start()` **0.00s 返回 False**；0.5s 后 `state` 已是 `SUBSCRIBED`。
* **影响**：调用方误判恢复失败；若据此关闭行情闸门，会错失一段本可交易的时间（偏保守，非风险放大）。
* **新开仓**：间接｜**持仓退出**：— ｜**重启恢复**：**是**（恢复路径的返回值）
* **建议**：`start()` 启动新线程前把状态置为 `CONNECTING`（或等待"本次启动后产生的新终态"而不是历史终态）。

### F-5（P3）关闭期 asyncio 传输回调抛出未捕获 `EOFError` —— **A.12 PARTIAL**

* **复现**：对不可达端口 `start()` 后 `stop()`（或正常 stop 时连接正被拆）。
* **现象**：stderr 打印 `Exception in callback _ProactorBasePipeTransport._call_connection_lost()` + `EOFError: stream ended` traceback。
* **影响**：日志噪声；不含凭证；可能掩盖真实异常。
* **建议**：`stop()` 中先 `await`/等待连接任务结束再关 loop，或对 `loop.set_exception_handler` 做已知噪声过滤。

### F-6（P3）vault 损坏与"未配置"不可区分 —— **A.2 诊断缺口**

* **复现**：写入 `{"broken` 到 `vault.json` → `SecretVault.get()` 返回 `None`。
* **现象**：只报 `缺少凭证`，无"vault 解析失败"提示。
* **建议**：`_read_raw` 捕获 `ValueError` 时置一个 `last_error`，由 `health()`/`check-env` 暴露（当前 `secrets.py` 未被本轮修改，属可接受遗留）。

### F-7（P2，脆弱缓解）认证头防泄露依赖"日志级别"而非"脱敏" —— **A.6/B**

* **复现**：`logging.getLogger("ai_trader.market.feed.ws").setLevel(logging.DEBUG)` 后重连 → 日志出现 `> X-Api-Key: <明文>`。
* **影响**：任何后续调级/调试动作都会让明文头进日志；`requirements.txt` 仅 `websockets>=10.0`（未锁版本），若升级/降级导致 `connect` 无 `logger` 参数，注入失效。
* **建议**：安装 `logging.Filter` 对 `X-Api-Key/X-Session` 做正则脱敏（双保险）；并把 websockets 版本锁到 `>=13,<18`（或精确 17.4 验证过的范围）。

### F-8（P3）改动范围与任务描述不一致

* 任务列 3 个文件；实际改了 4 个（多出 `runtime.py`）+ 1 个既有测试 `test_market_feed_b5.py` + 1 个新测试。
* `runtime.py` 的改动是必要的（认证提供者接线），但**未被描述**，评审者容易漏看。

### F-9（P1）生产配置当前即为路由冲突态

* `config/config.yaml` 仍是 `fosun.sdk_type: "ops"`，而适配器只调个人入口 → `health()["api_prefix"]="/api/ops"`、`routing_issue` 非空。
* 当前因 `base_url=""` + `PAPER_TRADING=true` 而未爆发；一旦补凭证并开实盘，叠加 F-3 即为真实错误路由。
* **建议**：在取得星财富确认前，把 `sdk_type` 留空（或用新开关显式声明账户类型）；并由配置守卫在"ops + 个人接口"时报错拒绝启动（当前 guard 未覆盖此项）。

### F-10（P2，潜在）`_session_dumped` 在内存持有私钥且无人使用

* `connect()` 后执行 `self._session_dumped = client.dump_session()`；SDK 该方法返回含 `clientPrivateKey` **明文 PEM** 的字典。
* 全仓无读取方（死字段），未落盘、未入日志；但缺少清理，且一旦有人打印 broker 对象即泄露。
* **建议**：删除该赋值，或改用 `restore_session` 需要的加密形式并显式清理。

---

## 7. 需向星财富（服务端）确认的事项

1. **账户类型判定**：本账户是"个人证券账户"还是"OPS 配对账户"？
   —— 决定 `sdk_type` 留空还是 `ops`，以及是否必须改走 `opsTrade/opsPortfolio/opsAccount`。
2. **个人接口在 `/api/ops` 前缀下的行为**：是否直接 404/400，还是会被路由到另一套语义（风险等级由此决定）。
3. **推送网关握手头的精确要求**：`X-Api-Key` / `X-Session` / `X-Request-Client` 的确切拼写与大小写；
   401/403/409 的语义是否与 SDK 内置提示一致（`400 参数缺失 / 401 鉴权 / 403 行情权限 / 409 重复连接`）。
4. **`X-Session` 生命周期**：过期前是否允许复用同一连接；重连是否必须换新 Session；同一账号并发连接上限（409 的触发阈值）。
5. **订阅回执的真实格式**：`subRep` 是否会回显 `requestid`？是否会逐主题/逐代码确认？"接受但未列代码"是否等价于全部接受？
6. **`settle_updates` 的语义对齐**：服务端重连后是否会重放快照（若是，首条快照是否算"一条新行情"）。
7. **SDK 版本兼容矩阵**：`logger=` 参数在目标版本范围内是否稳定存在。

---

## 8. 最终回答

1. **空环境变量是否确实不会覆盖有效 vault 凭证** —— **PASS**。实测空串不阻断 vault；来源审计显示 `vault`。
2. **vault 缺失/解析失败/凭证为空是否 fail-closed** —— **PASS**（失败关闭），但"损坏"与"未配置"不可区分（F-6）。
3. **SDK 构造失败是否清理已注入的敏感环境变量** —— **FAIL**。私钥 PEM 仍留在 `os.environ`（F-1）。
4. **`health()` 来源审计是否准确** —— **PASS**（`credential_origin` 与注入时机匹配）；副作用是会重复读 vault。
5. **WebSocket 每次连接是否使用最新 Session** —— **PASS**（每次建连现取 + SDK 自动刷新）。
6. **认证头是否只进连接层** —— **PASS（默认）**；缓解依赖日志级别，脆弱（F-7）。
7. **websockets 17.4 `additional_headers` 是否正确** —— **PASS**（版本、参数名、头名三重核对一致）。
8. **400/401/403/409 是否正确停止重试** —— **PASS**（4/4 均 FAILED 且不重试）。
9. **订阅拒绝/部分确认/超时/错误 requestid 是否都不标记 SUBSCRIBED** —— **FAIL**：前三种 PASS，**错误 requestid 会被确认**（F-2）。
10. **`settle_updates` 在重连/重复/乱序/异常下是否符合配置** —— **PASS**。
11. **`FAILED` 能否正确恢复** —— **PARTIAL**：可恢复，但 `start()` 返回值在恢复场景为假阴性（F-4）。
12. **新增异常路径与资源关闭** —— **PARTIAL**：无线程/连接泄漏，但关闭期有未捕获 `EOFError` traceback（F-5）。

**综合**：
* **A 组**：8 PASS / 3 PARTIAL / 1 FAIL
* **B 组**：PASS（1 条脆弱）
* **C 组**：3 PASS / 1 FAIL
* **D 组**：5/5 PASS

**结论：本轮修复方向正确、安全基线（开关/无真实连接/无泄露）保持良好，但存在 1 个 P1（C.3 路由未全入口阻断）与 2 个 P2 功能缺陷（A.3 清理、A.9 错误 requestid），因此判为 PARTIAL，不建议据此宣布"生产接入就绪"。**

**建议的最小返工顺序**：
1. `_require()` 增加 `routing_issue()` 检查（F-3 / F-9）—— 一行改动，收益最高；
2. `except/finally` 清理注入的 env（F-1）；
3. `_ack_verdict` 严格化 requestid（F-2）；
4. `start()` 恢复路径修返回值（F-4）；
5. 日志脱敏 Filter + 版本锁定（F-7）；删除 `_session_dumped`（F-10）；关闭期异常处理（F-5）。

---

## 9. 只读声明

* 未修改任何源码、配置、测试、`.env`、vault、数据库（工作区 9 个受管文件与 post-fix MANIFEST **逐一致**）。
* 未初始化/重置 Git，未重启服务，未重新生成密钥，未改变三个交易开关。
* 未连接任何生产网关；探针仅使用 `127.0.0.1` 回环服务与进程内假 SDK；**未发送任何业务请求**。
* 报告中不含任何真实密钥、Session 或认证头的值（探针统一使用 `PROBE-*` / `APIKEY-SECRET-*` 假串，且输出中已用占位符替代）。
