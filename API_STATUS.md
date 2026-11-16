# API_STATUS.md —— 星财富 SDK 审计与接口状态

> 本文件区分三件事：**已验证**（有真实命令输出）、**已实现未联网**（代码路径存在但无凭证）、**未验证**（需要有凭证后逐项核对）。
> 不做"看起来能用"的假设。

---

## 一、审计对象

| 项 | 值 |
|---|---|
| 仓库 | https://github.com/fosunwealth/openapi-python-sdk |
| 本地路径 | `C:\FosunAI-Trading\sdk_src` |
| HEAD commit | `c4c8a51aef11053a823aa1ef9a4af114ecc1fd3e` |
| commit 时间 | 2026-10-16 09:21:04 +0800 |
| 最近提交 | `Merge pull request #12 from fosunwealth/feature/order-detail` |
| 包名 / 版本 | `fsopenapi` 0.1.1（`setup.py`） |
| 声明 Python | `>=3.8` |
| 依赖 | `requests>=2.28.0`, `cryptography>=41.0.0`, `websockets>=10.0`, `protobuf>=4.0.0` |
| 代码规模 | 23 个 .py，`fsopenapi/` 共 2,854 行，仓库 501 KB |
| 安装方式 | `pip install -e ./sdk_src`（editable，便于审计与升级） |
| 实测环境 | Windows 11 / Python 3.13.14 / venv `.venv` |

编译产物 `proto/calc_pb2.py`、`proto/push_pb2.py` 由 protobuf 生成，`models.py` 为空文件（无 dataclass 模型层，响应统一为 `dict`）。

---

## 二、审计结论摘要

### 2.1 安全机制（值得肯定的部分）

| 机制 | 实现位置 | 说明 |
|---|---|---|
| ECDH 会话握手 | `auth.py: create_session()` | P-384 临时密钥对，`POST {base}{prefix}/v1/auth/SessionCreate` |
| 双向身份验证 | `crypto.py` | 客户端用长期私钥对 `ephPub+nonce` 签名；服务端签名从响应头 `X-Signature` 取回后校验 |
| 请求签名 | `client.py: _request()` | `method/path/query/timestamp/nonce/body` 全参与签名 |
| 业务报文加密 | `crypto.py` + `client.py` | AES-GCM，AAD 绑定 `sessionId/timestamp/nonce`，防重放 |
| 密钥仅从环境变量读 | `__init__.py: SDKClient.__init__` | 缺 `FSOPENAPI_SERVER_PUBLIC_KEY` / `FSOPENAPI_CLIENT_PRIVATE_KEY` 直接抛 `ValueError`，**不会静默降级** |
| 日志脱敏 | `logging_utils.py` | 默认不记录 api_key/签名/nonce/完整 body；`log_body=False` 为生产默认 |
| 证书校验可关但默认开 | `lang.py: resolve_ssl_verify` | 默认 `True`，我们适配器显式传 `True` |
| 错误分层 | `exceptions.py` | `APIError / AuthenticationError / PermissionError / RateLimitError / CacheError` |

### 2.2 审计发现的 3 个问题（需在拿到凭证后复核）

**① 行情接口"不加密"的判定条件与 README 不一致（可能影响行情调用）**

`client.py:114`：

```python
is_market_path = request_path == "/market/" or "/optmarket/" in request_path
```

而实际请求路径是 `f"{api_prefix}{path}"`，即 `/api/v1/market/secu/quote` 或 `/api/ops/v1/market/...`。
两个条件都**不可能命中**（`== "/market/"` 永不成立；`"/optmarket/"` 只在期权接口里出现，且实际拼出来是 `/api/v1/optmarket/...`，含 `optmarket/` 但前面不是 `/optmarket/`，同样不命中）。

后果：**行情 POST 请求（quote / kline）会被当作敏感报文加密**，与 README「行情接口走 `/market/`，仅签名不加密」矛盾。GET 类行情（`min` / `orderbook` / `tick` / `broker_list`）因 `data is None` 仍不加密，不受影响。

影响面：本工程 `FosunBroker.get_quote()` / `get_quotes()` / `get_klines()` 走 POST，会发加密请求。
**未验证**：网关于是否接受加密形式的行情请求。若返回解密失败或报文错误，改法是在适配器里临时跳过加密，或给 SDK 打补丁（本工程**未擅自修改** `sdk_src`，保持审计基线）。

**② 官方提供"服务端模拟账户"接口，可用于比本地撮合更真实的验证**

`account.py`：

```python
def sim_account_create(self):  # POST /v1/account/SimAccountCreate
def sim_account_reset(self, sub_account_id):  # POST /v1/account/SimAccountReset
```

这是**券商侧**的模拟账户，与本工程的 `PaperBroker`（本地撮合）不是一回事。建议拿到凭证后优先用官方模拟账户做灰度，本地撮合只用于开发期回归。

**③ 会话是"懒建连"，显式 `create_session()` 会重复握手**

`client.py:101` 每次请求都调 `auth_manager.get_valid_session()`，内部按需握手并自动续期。
本工程 `FosunBroker.connect()` 显式握手一次是**冗余但无害**的（第二次请求会复用），保留是为了在启动时就把凭证问题暴露出来。

### 2.3 值得注意的能力（本次未接入）

| 能力 | 位置 | 说明 |
|---|---|---|
| 实时行情推送 | `api/market_push.py`（37 个方法） | WebSocket 订阅 quote/tick/orderbook/brokerqueue/盘前盘后/夜盘/期权，本工程未接入（当前用轮询式 `scan`） |
| 期权行情与交易 | `api/optmarket.py`、`create_order(product_type=15, expiry, strike, right)` | 未接入（首版只做正股） |
| OPS 配对交易 | `api/ops/trade.py`（11 个方法） | 适配器已按 `SDK_TYPE=ops` 支持 `apply_account_id` |
| RWA / 财富产品 | `api/rwa.py`、`api/wealth.py` | 未接入，不在本期范围 |
| 订单状态推送 | `trade.create_subscription(event_type="orderUpdate")` | 需公网 HTTPS 回调地址，VPS 可用；当前用轮询 `list_orders` |

---

## 三、接口清单（由 `ai-trader sdk-audit` 反射生成）

> 完整签名见 `logs/evidence/sdk_inventory.json`。下表为人工归纳。

### 3.1 账户 `client.account`

| 方法 | 端点 | 关键参数 | 本工程状态 |
|---|---|---|---|
| `list_accounts()` | `POST /v1/account/Accounts` | — | ✅ 已实现，**未联网验证** |
| `sim_account_create()` | `POST /v1/account/SimAccountCreate` | — | ⬜ 未接入（建议灰度用） |
| `sim_account_reset(sub_account_id)` | `POST /v1/account/SimAccountReset` | `subAccountId` | ⬜ 未接入 |
| `ops_accounts()` | 同上 | 需 `SDK_TYPE=ops` | ⬜ 已废弃，用 `opsAccount` |

### 3.2 组合 `client.portfolio`

| 方法 | 端点 | 关键参数 | 本工程状态 |
|---|---|---|---|
| `get_assets_summary()` | `POST /v1/portfolio/CashSummary` | `subAccountId, clientId, currency, applyAccountId` | ✅ 已实现，字段名**待核对** |
| `get_holdings()` | `POST /v1/portfolio/Holdings` | `subAccountId, start, count, productTypes, currencies, symbols, useUsPre/Post/Night` | ✅ 已实现，字段名**待核对** |

### 3.3 交易 `client.trade`（个人）/ `client.opsTrade`（OPS 配对）

| 方法 | 端点 | 关键参数 | 本工程状态 |
|---|---|---|---|
| `create_order()` | `POST /v1/trade/OrderCreate` | `subAccountId, stockCode, direction(1买/2卖), orderType(1限价/3市价?), quantity, price, marketCode, currency, timeInForce, stopLossPrice, profitPrice, applyAccountId` | ✅ 已实现，受三重门禁；**未联网验证** |
| `cancel_order()` | `POST /v1/trade/OrderCancel` | `orderId, subAccountId, productType, applyAccountId` | ✅ 已实现，受门禁 |
| `order_modify()` | `POST /v1/trade/OrderModify` | `subAccountId, orderId, modifyType, quantity, price, ...` | ⬜ 未接入（首版用"撤单+重下"） |
| `list_orders()` | `POST /v1/trade/OrderList` | `subAccountId, start, count, statusArr, fromDate, toDate, direction, market, showType` | ✅ 已实现 |
| `get_order_detail()` | `POST /v1/trade/OrderDetail` | `subAccountId, orderId, productType` | ✅ 已实现（含 `feeDetail` / `transactionList`） |
| `get_cash_flows()` | `POST /v1/trade/CashFlows` | `subAccountId, tradeDateFrom/To, flowType, businessType` | ⬜ 未接入 |
| `get_bid_ask_info()` | `POST /v1/trade/BidAskInfo` | 下单前校验：`stockCode, orderType, marketCode, quantity, price, direction` | ⬜ **建议上线前接入**（见 §5） |
| `create/update/delete/list_subscriptions()` | `/v1/trade/Subscription*` | `eventType=orderUpdate`, `channelType=1(HTTP Webhook)` | ⬜ 未接入（用轮询替代） |

### 3.4 行情 `client.market`

| 方法 | 端点 | 方式 | 本工程状态 |
|---|---|---|---|
| `quote(codes, fields)` | `POST /v1/market/secu/quote` | POST（**会加密**，见 §2.2①） | ✅ 已实现，字段名**待核对** |
| `kline(code, ktype, num, ...)` | `POST /v1/market/kline` | POST（同上） | ✅ 已实现 |
| `min(code, count)` | `GET /v1/market/min` | GET | ⬜ 未接入 |
| `orderbook(code, count)` | `GET /v1/market/secu/orderbook` | GET | ⬜ 未接入 |
| `tick(code, count, id, ts)` | `GET /v1/market/secu/tick` | GET | ⬜ 未接入 |
| `broker_list(code)` | `GET /v1/market/secu/brokerq` | GET | ⬜ 未接入 |

代码格式：`marketCode + stockCode`，如 `hk00700`、`usAAPL`。A股通代码格式 SDK 未标注，**待确认**（推测 `sh600519` / `sz000001`，本工程 `market_of()` 已按此前缀解析）。

### 3.5 会话 `client.session`

| 方法 | 端点 | 说明 |
|---|---|---|
| `create_session()` | `POST /v1/auth/SessionCreate` | ECDH 握手 |
| `query_session()` | — | 查询服务端当前会话 |
| `is_session_valid()` | — | 本地判断是否过期 |
| `delete_session()` | — | 主动登出 |
| `get_session_info()` | — | 本地会话信息 |

`SDKClient.dump_session()` / `restore_session()` 可持久化会话（含 `clientPrivateKey`），**该文件等价于凭证**，本工程未落盘。

---

## 四、接口状态总表

| 需求 | 需要的接口 | 实现 | 实测 |
|---|---|---|---|
| 账户查询 | `account.list_accounts`, `portfolio.get_assets_summary` | ✅ | ❌ 无凭证 |
| 持仓查询 | `portfolio.get_holdings` | ✅ | ❌ 无凭证 |
| 行情 | `market.quote`, `market.kline` | ✅ | ❌ 无凭证（当前用合成行情） |
| 下单 | `trade.create_order` | ✅ | ❌ 无凭证 + 门禁关闭 |
| 撤单 | `trade.cancel_order` | ✅ | ❌ 无凭证 + 门禁关闭 |
| 订单查询 | `trade.list_orders`, `get_order_detail` | ✅ | ❌ 无凭证 |
| 实时推送 | `market_push.*` | ❌ 未实现 | ❌ |
| 港股 / 美股 / A股通 | 由 `market_code` + `currency` 区分 | ✅ 参数层面 | ❌ |
| 期权 | `optmarket.*` / `product_type=15` | ❌ 未实现 | ❌ |

**"实测"一栏全部为 ❌ 的原因**：没有星财富开放平台下发的 `base_url`、`api_key`、服务端公钥、客户端私钥四件套。这是客观事实，不是遗漏。

已实测的是**SDK 本身可用性**（不依赖凭证）：

```
$ .venv\Scripts\python.exe -c "import fsopenapi; print(fsopenapi.__file__)"
C:\FosunAI-Trading\sdk_src\fsopenapi\__init__.py

$ .venv\Scripts\python.exe -c "from fsopenapi import SDKClient; SDKClient('https://x','k')"
ValueError: 缺少必要的环境变量: FSOPENAPI_SERVER_PUBLIC_KEY, FSOPENAPI_CLIENT_PRIVATE_KEY。
          请在启动前设置 FSOPENAPI_SERVER_PUBLIC_KEY 和 FSOPENAPI_CLIENT_PRIVATE_KEY
```

---

## 五、上线前必须补的 4 个接口/动作

| # | 项目 | 原因 | 优先级 |
|---|---|---|---|
| 1 | `trade.get_bid_ask_info()` 下单前校验 | 官方提供该接口就是为了在下单前确认可买数量/价格/权限，直接下单容易被拒或部分成交 | 高 |
| 2 | `market_push` WebSocket 订阅 | 轮询式 `scan`（默认 300s）无法满足止损实时性；接推送后才谈得上"持续监控" | 高 |
| 3 | `trade.create_subscription(orderUpdate)` | 委托状态异步变化（部分成交、废单）无法靠轮询及时感知 | 中 |
| 4 | 官方 `SimAccountCreate` 灰度 | 券商侧模拟账户比本地撮合更真实（含撮合规则、费用、T+1 限制） | 高 |

---

## 六、需要申请 / 配置的清单

### 6.1 需要向星财富申请

- [ ] 开放平台账号与**网关 base_url**
- [ ] **API Key**
- [ ] **服务端公钥**（PEM）与**客户端私钥**（PEM）—— 由平台签发，本工程**不会也不能**自行生成
- [ ] 交易权限（是否开通港股 / 美股 / A股通 / 融资 / 做空）
- [ ] 行情权限等级（实时 / 延迟 15 分钟 / 仅日线）
- [ ] 是否属于 OPS 配对场景（决定 `SDK_TYPE=ops` 与 `apply_account_id`）
- [ ] 限流配额（每秒/每分钟请求数）

### 6.2 需要自行配置

- [ ] `config\config.yaml` 的 `fosun.sub_account_id` / `apply_account_id`
- [ ] 密钥写入保险箱（推荐）或 `.env`
- [ ] AI 模型：`AI_API_KEY` / `AI_BASE_URL` / `AI_MODEL`（不给则一直用规则引擎）
- [ ] `WEB_ADMIN_PASSWORD`（对外暴露 8781 端口时必须设置）
- [ ] Windows VPS 防火墙、TLS 反代（若需公网访问管理台）

### 6.3 配置完成后如何自证"真的连上了"

```powershell
# 1) 门禁状态：必须全部为 false/true 的预期值
.venv\Scripts\python.exe -m ai_trader.cli check-env

# 2) 只读连通性验证（不涉及下单）
.venv\Scripts\python.exe -c "
from ai_trader.config import load_config
from ai_trader.secrets import SecretVault
from ai_trader.broker.fosun_adapter import FosunBroker
cfg = load_config(); b = FosunBroker(cfg, SecretVault())
print('configured =', b.configured, '| blocked =', b.blocked_reason)
"
```

在 `PAPER_TRADING=true` 下 `FosunBroker` 会被门禁拦下，`configured=False`、`blocked_reason="PAPER_TRADING=true..."`。
**这是正确行为**：模拟盘期间不允许触碰真实账户。要做只读连通测试，需要临时把 `PAPER_TRADING=false` 且保持 `ORDER_EXECUTION_ENABLED=false`，此时可连、可读、不可下单。

---

## 七、2026-11-08 修复后的接口状态变化

### 7.1 订单状态：新增 `unknown` 与对账流程

| 场景 | 修复前 | 修复后 |
|---|---|---|
| 券商响应无 `orderId` | 编造 `FS-HHMMSS`，状态 `submitted` | `order_id=UNCONFIRMED-<clientId>`、`broker_order_id=""`、`status=unknown` |
| 响应 `data={}` | 同上 | 抛 `OrderStateUnknown(EMPTY_ORDER_RESPONSE)` |
| 有 `orderId` 无 `status` | `submitted` | `unknown`（ID 保留） |
| 网络超时 | 异常上抛，本地无记录 | 抛 `OrderStateUnknown`，带 `client_id` |
| 后续动作 | 无 | `reconcile_order(client_id)` 查真实状态 |

**对账接口用法**（`fosun_adapter.py:796`）：

```python
found = broker.reconcile_order(client_id="1234567890", symbol="hk00700",
                               side="buy", qty=100)
# found is None  → 查询成功且确实没有这笔单（判定未受理）
# found 是对象   → 券商真实状态
# 抛 BrokerError → 查不清，必须继续视为 unknown
```

优先走 SDK 的 `list_orders(client_id=...)` 过滤；不支持时退化为"最近订单列表按 `clientId` 字段匹配"，
再退化为"标的+方向+数量"近似匹配（`reconcile_note` 会标注"需人工复核"）。

**待验证**：`trade.list_orders` 是否真的支持 `client_id` 过滤（SDK 签名里有该参数，但服务端是否生效未验证）。

### 7.2 行情加密兜底（未修改 SDK）

`fosun.market_encryption_workaround: auto`（默认）时：

```
POST /v1/market/secu/quote 失败（解密/解析类错误）
        ↓
GET  /v1/market/min?code=xxx&count=1   ← 官方端点，未加密
        ↓
Quote(source="fosun_market_min_fallback")
```

触发条件严格限定为"客户端解密/解析失败"，鉴权类错误**不**触发兜底（避免掩盖真正的问题）。
成功后 `_quote_via_min=True`，后续不再重复失败一次。
`health()` 会暴露 `market_encryption_fallback` 与 `market_fallback_reason`。

**限制**：该兜底只解决"最新价"，**K 线没有等价的未加密 GET 端点**。
若 `market.kline` 同样因加密失败，`get_klines` 会抛 `PARSE_MISMATCH`，扫描会报错停手（安全方向）。

### 7.3 SDK 未被修改（可验证）

```
audit_evidence/sdk_baseline.json   ← 26 个文件的 sha256 + commit c4c8a51
tests/test_timeout_and_reconnect.py::test_sdk_source_matches_audited_baseline
tests/test_timeout_and_reconnect.py::test_client_still_contains_unpatched_market_branch
```

第二个测试断言上游那行 `request_path == "/market/"` **原样存在** —— 用来证明"我们没偷偷打补丁"。
若将来官方修复了这个问题，该测试会失败，提示重新基线化并在 `FIX_REPORT` 记录。

### 7.4 客户端 HTTP 超时

SDK 仍未设置 timeout（上游问题）。适配器在**实例级别**包装 `_http_session.request` 注入
`(connect, read)`，握手用线程墙钟限制。配置项：

```yaml
fosun:
  http_connect_timeout: 10
  http_read_timeout: 30
  read_retry_attempts: 2
```

`health().http_timeout` 会回显实际生效值。

### 7.5 接口状态总表（修复后）

| 能力 | 实现 | 实测 |
|---|---|---|
| 账户/持仓/资金 | ✅ | ❌ 无凭证 |
| 行情（含 GET 兜底） | ✅ | ❌ 无凭证 |
| 下单（含 UNKNOWN + 对账） | ✅ | ❌ 无凭证 |
| 撤单（引擎/Web/CLI 三入口） | ✅ | ✅ 模拟盘实跑 |
| 订单查询 | ✅ | ❌ 无凭证 |
| 超时撤单 | ✅ | ✅ 单测 |
| 断线重连 | ✅ | ✅ 桩对象验证 |
| 实时推送 `market_push` | ❌ 未实现 | ❌ |
| 期权 / RWA / 财富 | ❌ 未实现 | ❌ |
