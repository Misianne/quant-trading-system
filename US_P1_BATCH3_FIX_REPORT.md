# US_P1_BATCH3_FIX_REPORT.md —— 路由旁路 / 凭证清理 / requestid / 异步关闭

**项目**：`C:\FosunAI-Trading`
**依据**：`US_P1_PRODCONN_INDEPENDENT_REVIEW.md`（F-1 / F-2 / F-3 / F-4 / F-5 / F-6 / F-7 / F-10）
**时间**：2026-11-09 17:07 – 17:55 (+08:00)
**性质**：允许修改代码/测试；**未连接生产环境、未发送任何业务请求、未修改 `.env`/vault/数据库、未改 `sdk_type` 与 API 前缀、未改 SDK 源码**
**安全开关（修复前 = 修复后）**：

```
LIVE_TRADING            = False
ORDER_EXECUTION_ENABLED = False
PAPER_TRADING           = True
can_send_real_order     = (False, 'PAPER_TRADING=true，真实券商连接被禁用')
fosun.sdk_type          = 'ops'（未改动）  fosun.base_url = ''（未改动）
```

---

## 0. 结论摘要

| 缺陷 | 级别 | 状态 | 关键证据 |
|---|---|---|---|
| **F-3** 路由旁路（`_require()` 不查冲突） | P1 | ✅ **已封堵** | 六个入口 6/6 拒绝，**每次 SDK 调用次数 = 0**，`trade.create_order` 合计 0 |
| **F-1** 构造失败不清理凭证 | P2 | ✅ **已修复** | 空 env + vault → 失败后 env 干净；预存 env → 失败后**完好保留** |
| **F-2** 错误 requestid 被确认 | P2 | ✅ **已修复** | 错误/缺失/不可解释 RID → **不确认**，最终 FAILED；只有严格命中 RID 才确认 |
| **F-4** `start()` 0 秒假 False | P2 | ✅ **已修复** | FAILED 后再 `start()`：返回值与最终状态一致，且耗时 > 0 |
| **F-5** 关闭期 EOFError traceback | P2 | ✅ **已修复** | 正常关闭 / 死端口关闭均无 `Traceback`、无 `Exception in callback` |
| **F-6** vault 缺失/损坏/权限不可区分 | P3 | ✅ **已修复** | `vault.state ∈ {ok, absent, damaged, unreadable}` + `kind`，不含内容 |
| **F-10** `_session_dumped` 持私钥 PEM | P2（潜在） | ✅ **已移除** | 不再调用 `dump_session()`，测试断言其调用次数为 0 |
| **F-7** 认证头防泄露依赖日志级别 | P2（脆弱） | ⚠️ **部分** | 约束 `websockets>=13,<18`（未升级，当前 17.4）；级别→脱敏过滤器的改造**未做**（见 §12 R-1） |

**回归**：`476 passed / 1 skipped / 0 failed`（修复前 444+1；新增 32 个用例）。

---

## 1. 修复前保护

### 1.1 Git 状态

```
$ git status
fatal: not a git repository (or any of the parent directories): .git
```

无 Git 仓库（历次阶段均为目录级交付），**未初始化**。

### 1.2 上一轮备份核对

`backups/us_p1_prodconn_20261109-163216/` 存在，含 `MANIFEST.sha256.json`（pre）与
`MANIFEST.after.sha256.json`（post），与本轮起点一致。

### 1.3 本轮备份

```
backups/us_p1_b3_20261109-170814/
├── MANIFEST.sha256.json        （pre-fix，5 文件，含 sha256/mtime/size）
├── MANIFEST.after.sha256.json  （post-fix，7 文件）
└── src/… tests/… requirements.txt（副本）
```

`leak=无`（已排除 `.env` / `vault.json` / `*.db`）。

### 1.4 实际修改文件清单（先声明后修改）

| # | 文件 | 改动 |
|---|---|---|
| 1 | `src/ai_trader/broker/fosun_adapter.py` | `_require()` 路由校验；env 所有权跟踪 + `release_credentials()`；移除 `_session_dumped`；`health()` 增加 `vault`/`credentials_released` |
| 2 | `src/ai_trader/market/feed.py` | `_ack_verdict` 严格 RID；`start()` 竞态；关闭期异常分类器；回执诊断计数与 `snapshot()` 字段 |
| 3 | `src/ai_trader/secrets.py` | `last_error_kind` 分类 + `diagnostics()`（不含内容） |
| 4 | `requirements.txt` | `websockets>=13.0,<18`（约束，不升级） |
| 5 | `tests/helpers.py` | **额外改动**：`live_cfg` 默认 `sdk_type=""`（见 §2.6） |
| 6 | `tests/test_prod_conn_repair_b3.py` | **新增 32 用例** |

> 说明：第 5 项（`tests/helpers.py`）**未在初始声明的 5 个文件内**，是回归时发现必需的最小改动，
> 已在 `MANIFEST.after` 中单独标注，并按精确反向还原给出"修改前哈希"。见 §12 R-4。

### 1.5 修复前后 SHA256

| 文件 | 修复前 | 修复后 |
|---|---|---|
| `src/ai_trader/broker/fosun_adapter.py` | `3d291e1f204d3bdb…` | `96751563ca86f1bc…` |
| `src/ai_trader/market/feed.py` | `2664faf272d58a51…` | `703dae9fd60ac54e…` |
| `src/ai_trader/secrets.py` | `49571295976d56b7…` | `5e28f80b323d2efa…` |
| `requirements.txt` | `cfc4f91cfeafff1a…` | `9138fc16a7de48d9…` |
| `tests/test_prod_connectivity_p0.py` | `59d6b400426e5338…` | **未变**（`59d6b400426e5338…`） |
| `tests/helpers.py` | `2ae41a4f7464ba08…`（精确反向还原） | `6138b66409e20b03…` |
| `tests/test_prod_conn_repair_b3.py` | —（新增） | `ac6bac4d6a90bcc5…` |

完整 sha256/mtime/size 见 `MANIFEST.after.sha256.json`。

---

## 2. 缺陷根因与修复

### 2.1 F-3（P1）路由旁路

**根因**：`routing_issue()` 只在 `connect()` 与 `health()` 被调用。
所有数据/交易入口走 `_require()`，而 `_require()` 只检查 `can_connect_broker()` 与 `client is not None`。
于是 `sdk_type=ops` 时只有"建连"被拦，`get_*/place_order` 仍会打到 `/api/ops/v1/...` 且携带个人接口请求体。
更隐蔽的是：`place_order` 在 `_require()` 之前还有 `_require_order_permission()`（只查门禁）与
`apply_account_id` 检查 —— 后者恰好让人误以为"ops 已被处理"。

**修复**（`fosun_adapter.py`）：

```python
def _require(self) -> Any:
    ok, reason = self.cfg.gate.can_connect_broker()
    if not ok:
        raise BrokerNotConfigured(reason)
    issue = self.routing_issue()          # ← 新增：与 connect() 同一判据
    if issue:
        raise BrokerNotConfigured(issue)
    if self.client is None:
        raise BrokerNotConfigured(...)
    return self.client
```

* 单点收口：六个入口（含未来新增的个人接口方法）自动覆盖；
* **不依赖** `PAPER_TRADING`（测试在实盘门禁全开时验证）；
* **未**修改 `sdk_type`、**未**改 `/api` 或 `/api/ops` 配置、**未**自动切 normal。

### 2.2 F-1（P2）构造失败清理凭证

**根因**：`_inject_sdk_env()` 在 `try` 之外；`except` 只改 `configured/client`，不清 env。

**修复**：引入**所有权**概念。

| 场景 | 判定 | 失败时动作 | 成功时 |
|---|---|---|---|
| 变量调用前不存在 / 为空 | `owned` | **pop** | 保留（SDK 需要） |
| 变量调用前已有**有效值** | 借用（`prev` 暂存旧值） | **写回旧值**（不误删） | 丢弃 `prev` 副本 |
| 解析不出凭证 | — | 仅清理"空字符串"占位 | — |

新增 `release_credentials(restore_previous=False)`：
只删 `_sdk_env_owned`，返回值仅含变量名与动作（`{"popped": [...], "restored": [...]}`）。
`_init_client` 的 `except` 调用 `release_credentials(restore_previous=True)`，并在 `blocked_reason`
里只写"已清理 N 个 / 回滚 M 个"，**不含任何凭证值**。

### 2.3 F-2（P2）requestid 严格校验

**根因**：`_ack_verdict` 在 `rid not in pending` 时执行 `targets = dict(pending)`，
用回执**内容**去匹配所有待确认项 → 错误 RID 也能确认。

**修复**：取消内容兜底。

| 输入 | 处理 |
|---|---|
| `rid` 缺失 / 不在 pending（含迟到、重复回执） | **不确认**；`ack_unmatched++` 并记录一句不含内容的诊断 |
| `rid` 命中，**无 subRep**，`act ∈ {sub,subrep,ack,suback}` | 确认（RID 唯一关联该请求） |
| `rid` 命中，subRep 不可解析 | **不确认**；`ack_unverifiable++`（不猜结构） |
| `rid` 命中，回执缺该 topic | 抛 `FeedSubscriptionError` |
| `rid` 命中，topic 未列代码 | **不确认**（无法证明全部代码被接受） |
| `rid` 命中，代码不全 | 抛 `FeedSubscriptionError`（部分不接受 = 整体失败） |

超时信息追加 `_ack_diag_suffix()`（只含计数与固定措辞）；`snapshot().subscription` 新增
`unmatched_acks / unverifiable_acks / diagnostic`。

### 2.4 F-4（P2）start() 竞态

**根因**：`start()` 的等待循环把**历史**终态 `FAILED` 当作本次结果 → 0 秒返回 False，
而新线程随后又进入 `SUBSCRIBED`。

**修复**：

```python
if self._thread and self._thread.is_alive():
    return self.state == FeedState.SUBSCRIBED   # 不再无条件 True
self._closing = False
with self._lock:
    self._state = FeedState.CONNECTING          # ← 先落地本轮起点
    self._state_since = now
... 等待 SUBSCRIBED / FAILED / STOPPED ...
超时 → 返回 False + 记录 last_error（后台仍继续重试）
```

### 2.5 F-5（P2）关闭期 EOFError

**根因**：`loop.close()` 之后底层传输回调 `connection_lost` → `websockets.streams.feed_eof()` 抛
`EOFError: stream ended`，asyncio 默认打印 traceback。

**修复**：

1. `loop.set_exception_handler(self._on_loop_exception)`：
   * 预期关闭（`_closing` 或 STOPPED/FAILED）的 `EOFError/CancelledError/ConnectionError` → 只计 `shutdown_noise`，不打印；
   * 连接拆除时的 `EOFError: stream ended` → 同样只计数（连接错误本身已在 `_connect_and_serve` 抛出并进入重试）；
   * 其它异常 → `errors++`，**只打一行**（含类型），不打印 traceback、不带消息负载。
2. `_thread_main` 的 `finally` 先 `run_until_complete(asyncio.sleep(0))` 让关闭回调跑完，再 `loop.close()`。

### 2.6 额外改动：`tests/helpers.py` 的 `sdk_type` 默认值

修复 F-3 后，21 个既有用例（用 `live_cfg` 起真实配置）全部被正确拦下 —— 因为生产配置就是
`sdk_type: "ops"`。为不掩盖产品行为、也不改生产配置，夹具层声明"验证个人接口的用例默认 `sdk_type=""`"：

```python
if "fosun.sdk_type" not in overrides:
    raw["fosun"]["sdk_type"] = ""
```

* 需要验证冲突的用例仍显式覆盖为 `"ops"`（`test_prod_connectivity_p0.py` 与
  `test_prod_conn_repair_b3.py` 各有专项）；
* 生产 `config/config.yaml` **逐字节未改**（§3.2 已核对 mtime/hash）。

### 2.7 F-6 / F-10 / F-7

* **F-6**：`SecretVault` 新增 `last_error_kind`（`missing/corrupt_json/not_object/permission/os_error/decrypt_failed`）
  与 `diagnostics()`（只回 `state/kind/exists/note`）。`get()` 语义不变（仍 fail-closed 返回 `None`）。
  `health()["vault"]` 暴露该诊断。
* **F-10**：删除 `self._session_dumped = client.dump_session()`（该值含 `clientPrivateKey` 明文 PEM，
  且全仓无读取方）。测试断言 `dump_session` 调用次数为 0。
* **F-7**（部分）：`requirements.txt` 将 `websockets>=10.0` 改为 `>=13.0,<18`（约束，不升级；当前 17.4 在范围内）。
  **未做**日志脱敏过滤器，见 §12 R-1。

---

## 3. 验证证据

全部证据文件：`audit_evidence/us_p1_b3_evidence.txt`。

### 3.1 路由冲突：六个入口 + SDK 调用次数

`sdk_type=ops` + `apply_account_id` 已配 + 假 SDK（带计数器）——**未联网**：

| 入口 | 是否拒绝 | 本次 SDK 调用次数 |
|---|---|---|
| `get_accounts` | True | **0** |
| `get_summary` | True | **0** |
| `get_positions` | True | **0** |
| `get_quote` | True | **0** |
| `get_klines` | True | **0** |
| `place_order` | True | **0** |
| **合计 `trade.create_order`** | — | **0** |

拒绝理由均为 `BrokerNotConfigured: 配置冲突…`（同一判据来自 `routing_issue()`）。

**不依赖 PAPER_TRADING**：`test_routing_conflict_does_not_depend_on_paper_trading`
在 `can_send_real_order()==True` 的实盘门禁下断言仍被拒且调用数不变。

**对照组**：`sdk_type=""` 时 `routing_issue()==""`，`get_positions` 正常调用
`portfolio.get_holdings` 1 次（未被误杀）。

### 3.2 生产配置现状（未改动）

```
api_prefix: /api/ops | routing_issue 非空: True
config/config.yaml sha256=fe744191bc9fbeea（与上一轮一致）mtime=2026-11-09 12:38:02
credential_origin: 全部 missing | configured=False | api_key_present=False
```

即：**冲突仍存在**（等星财富确认账户类型），但现在**任何入口都会拒绝**，而不是只在建连时拒绝。

### 3.3 凭证清理（所有权）

| 场景 | configured | 失败后 env | 结论 |
|---|---|---|---|
| 空 env + vault 有效 + 构造失败 | False | PUB=空、PRIV=空 | ✅ 本次注入被清理 |
| env 有效 + 构造失败 | False | PUB=有值、PRIV=有值 | ✅ 预存变量未被误删 |
| env 有效 + 构造成功 | True | PUB/PRIV 保留 | ✅ SDK 运行所需凭证保留 |
| `release_credentials` 仅部分变量为我们创建 | — | 预存的保留、注入的清除 | ✅ 所有权隔离（`test_release_credentials_is_ownership_scoped`） |
| A 失败清理后构造 B | — | B 仍可正常构造 | ✅ 不影响其他实例 |

`blocked_reason` 中**不含** PEM 正文或 APIKEY（断言 `B3-FAKE-CLIENT-PRIV" not in ...`）。

### 3.4 requestid 严格校验

判定函数直测（`pending={"rid-1": ("qt", {"hk00700","usaapl"})}`）：

| 输入 | 结果 |
|---|---|
| 正确 RID + 完整代码 | 确认 `['rid-1']`，mode=`subRep` |
| **错误 RID** + 完整代码 | **确认=无**（不兜底） |
| **缺失 RID** + 完整代码 | **确认=无** |
| 正确 RID + 缺 1 个代码 | 抛 `FeedSubscriptionError: 订阅未被全部接受（qt 缺 1 个代码）` |
| 正确 RID + 未列代码 | **确认=无**（不可验证） |
| 正确 RID + subRep 不可解析 | **确认=无**（不猜结构） |

真实本机 WS 服务端到端（`RidServer`）：

| 模式 | `start()` | 最终 state | 备注 |
|---|---|---|---|
| `ok` | True | SUBSCRIBED | `subscribed_codes=2` |
| `wrong_rid` | False | **FAILED** | `unmatched_acks>=1`，`subscribed_codes=0` |
| `missing_rid` | False | **FAILED** | 同上 |
| `opaque`（正确 RID、体不可解析） | False | **FAILED** | `unverifiable_acks>=1` |
| `partial` | False | **FAILED** | `last_error` 含"未被全部接受" |
| `dup`（先确认再重复） | True | SUBSCRIBED | 重复回执不改变状态、不报错 |
| `late`（第二次回上一轮 RID） | True→心跳重连 | 由 `late` 语义决定 | 迟到 RID 不确认 |

### 3.5 异步关闭与 FAILED 恢复

| 场景 | 结果 |
|---|---|
| 正常 `stop()` | `state=STOPPED`，`market-feed` 线程数 1→0，`_ws` 清空 |
| 死端口 `start()`+`stop()` | stderr **无** `Traceback`、**无** `Exception in callback` |
| 分类器（预期噪声） | `shutdown_noise=1`、`errors=0` |
| 分类器（真实异常 ValueError） | `errors>=1`、`last_error` 含类型（不打印 traceback） |
| FAILED 后再 `start()` | 返回值 True 且最终 `SUBSCRIBED`；耗时 > 0.001s（不再 0 秒假 False） |
| 真失败（端口不通） | `start()` False，state ∈ {FAILED, RECONNECTING, CONNECTING} |
| 订阅永不回执 | `start()` 在超时上限内返回 False（不无限等待） |

### 3.6 泄露面复核

| 路径 | 结果 |
|---|---|
| 应用侧日志（`ai_trader*`，DEBUG） | 不含 `FAKE_PRIV/FAKE_AK/HDR_SESSION` |
| feed `snapshot()` | 不含认证值；仅计数与固定措辞 |
| `health()` | 26+ 字段，PEM 经 `redact_pem`；`vault` 只有状态 |
| CLI `check-env`（注入假 PEM/AK） | 5358 字节输出，**0 命中** token |
| `data/` 既有文件 | 0 命中（本轮未写入 `data/`） |
| 测试代码 | 全部使用 `B3-FAKE-*` 占位串 |

> 注：`websockets.server` 在 DEBUG 下会打印它**收到**的请求头 —— 那是本机测试服务端自身的日志，
> 不是被测进程的外泄路径（本轮泄露断言只捕获 `ai_trader*` logger）。

---

## 4. 新增测试与结果

```
$ python -m pytest tests/test_prod_conn_repair_b3.py -o addopts="" -q -p no:cacheprovider
32 passed in 11.70s

$ python -m pytest tests/ -o addopts="" -q -p no:cacheprovider
476 passed, 1 skipped, 1 warning in 51.45s
```

| 组 | 用例数 | 覆盖 |
|---|---|---|
| 路由旁路（P1） | 9 | 六入口参数化 + place_order 专项 + 不依赖 paper + 对照组 |
| 凭证所有权/清理 | 5 | 空 env+vault 失败、env 有效失败、成功保留、vault 缺失/损坏、跨实例、所有权隔离 |
| requestid | 8 | 正确/错误/缺失/部分/不可解析/重复/迟到 + 判定函数直测 |
| 异步（start/close） | 6 | FAILED 恢复、真失败、超时边界、断线重连、正常关闭、死端口关闭、分类器 |
| 泄露/vault 诊断 | 2 | 日志/snapshot/health 扫描、vault 诊断无内容 |

测试真实性：11 个用例走**真实本机 WebSocket 握手**（`RidServer`）；
券商侧为**实现完整 `Broker` 契约的假 SDK + 调用计数器**；无 `unittest.mock`、无 `monkeypatch` 打桩
（`monkeypatch` 仅用于 `setenv/delenv` 与替换 `SDKClient` 类，不替换被测逻辑）。

---

## 5. 剩余风险

| # | 项 | 级别 | 说明 |
|---|---|---|---|
| R-1 | 认证头防泄露仍是"日志级别"而非"脱敏过滤器" | P2 | 若有人把 `ai_trader.market.feed.ws` 调到 DEBUG，仍会输出 `> X-Api-Key`。已约束 `websockets>=13,<18` 降低版本漂移风险；**建议后续加 `logging.Filter` 正则脱敏**（本轮未做，避免扩大改动面） |
| R-2 | 生产配置 `sdk_type=ops` 仍是冲突态 | P1（外部） | 本轮**按约束不改**。在星财富确认账户类型前，所有个人接口入口现在会全部拒绝 —— 属正确的 fail-closed，但意味着**拿到凭证后仍不能直接交易** |
| R-3 | 无回执 act 的"opaque ack"仍算确认 | P3 | 当 RID 严格命中且 `act ∈ {sub,subrep,ack,suback}`、**无 subRep** 时确认。这是 SDK 自带协议的形态（RID 唯一关联请求），但不证明"代码级"接受；若官方确认不会出现这种回执，可进一步收紧为必须带可解析 subRep |
| R-4 | `tests/helpers.py` 属计划外改动 | P3 | 已在 after 清单单列并给出"修改前哈希（精确反向还原）"；pre 备份未包含该文件，回滚需手工还原该段 |
| R-5 | 真实网关行为仍未验证 | 未验证 | `X-Api-Key/X-Session` 大小写、409 阈值、subRep 真实格式、`logger=` 参数在目标版本的存在性 —— 均需拿到凭证后实测 |
| R-6 | `settle` 的"快照重放"语义 | P3 | 若服务端重连后重放快照，首条快照会被计入 settle；未验证 |

---

## 6. 回滚方式

```powershell
$B = "C:\FosunAI-Trading\backups\us_p1_b3_20261109-170814"
Copy-Item "$B\src\*" "C:\FosunAI-Trading\src\" -Recurse -Force
Copy-Item "$B\requirements.txt" "C:\FosunAI-Trading\requirements.txt" -Force
Remove-Item C:\FosunAI-Trading\tests\test_prod_conn_repair_b3.py

# tests/helpers.py 需手工把那 6 行夹具块还原为"两段相同的 warn 声明"
# （见 MANIFEST.after 中 before_sha256_reconstructed）

# 校验
python -c "import hashlib,json,pathlib;m=json.load(open(r'$B\MANIFEST.sha256.json'));f=m['files'];print('差异:',[k for k,v in f.items() if hashlib.sha256(pathlib.Path(k).read_bytes()).hexdigest()!=v['sha256']])"
```

数据库/vault/`.env` **无需回滚**（本轮未触碰）。

---

## 7. 停止声明

三项主要缺陷（F-3 / F-1 / F-2）与两项稳定性问题（F-4 / F-5）已修复并有测试证据；
F-6 / F-10 已处理；F-7 做了依赖约束（脱敏过滤器留作后续）。

未连接星财富生产环境、未发送真实订单/撤单、未修改真实账户数据、未改 `sdk_type` 或 API 前缀、
未改 `.env`/vault、未重新生成密钥、未改生产服务/VPS/白名单、未重启服务、未将真实密钥写入任何测试/日志/报告。

**完成修复与本地测试，立即停止，等待独立只读审计。**
