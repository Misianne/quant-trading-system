# US_BROKER_CAPABILITY_REPORT.md —— 星财富 OpenAPI 美股适配性审计

**项目**：`C:\FosunAI-Trading`
**日期**：2026-11-08 18:15 – 19:00 (+08:00)
**性质**：只读。未连接真实网关，未取得任何凭证。
**审计对象**：`sdk_src/fsopenapi`（commit `c4c8a51`，未修改）+ 本工程 `broker/fosun_adapter.py`

---

## 0. 三档状态的区分（本报告的核心约定）

任务要求"必须区分官方 SDK 已定义、真实网关已验证、尚未验证"。本报告严格使用三档：

| 档位 | 含义 | 判定依据 |
|---|---|---|
| **A｜SDK 已定义** | 官方源码里有这个方法/参数，签名可读 | 静态阅读 `sdk_src/` |
| **B｜适配器已实现** | 本工程确实调用/传参了 | 静态阅读 `src/ai_trader/` |
| **C｜网关已验证** | 真实网关返回过正确结果 | **需要凭证 → 本报告全部为 ❌** |

> ⚠️ **没有任何一项达到 C 档。** 本项目从未连接过真实网关，`check-env` 的
> `fosun.configured=false` 就是证据。下面所有"支持"都只是 **A/B 档**。

---

## 1. 总表：美股六大能力

| # | 能力 | A: SDK 已定义 | B: 适配器已实现 | C: 网关已验证 |
|---|---|---|---|---|
| 1 | 美股账户与持仓 | ✅ | ⚠️ **部分**（未用盘前/盘后/夜盘标志） | ❌ |
| 2 | 美股实时行情 | ✅ | ✅（REST `quote`/`kline`） | ❌ |
| 3 | 盘前盘后行情 | ✅（`pqt/pmin/ptk` + `ob/bq`） | ❌ **未接入** | ❌ |
| 4 | 美股订单类型 | ✅（参数存在） | ⚠️ **只有限价/市价** | ❌ |
| 5 | 撤单与订单查询 | ✅ | ✅ | ❌ |
| 6 | 交易状态与成交回报 | ✅（`orderUpdate` 订阅 + `feeDetail`/`transactionList`） | ⚠️ 部分（只轮询查单，未订阅） | ❌ |

---

## 2. 逐项细查

### 2.1 美股账户与持仓

**A 档（SDK 已定义）**

```python
# fsopenapi/api/portfolio.py:18
def get_holdings(self, sub_account_id=None, start=0, count=100,
                 product_types=None, currencies=None, symbols=None,
                 use_us_pre=False, use_us_post=False, use_us_night=False,
                 client_id=None, apply_account_id=None, sub_account_class=None):
    payload["useUsPre"]  = bool(use_us_pre)
    payload["useUsPost"] = bool(use_us_post)
    payload["useUsNight"]= bool(use_us_night)
```

* **美股专属标志**：`useUsPre` / `useUsPost` / `useUsNight` —— 说明官方 API **明确支持**
  "盘前/盘后/夜盘持仓"三种视图（美股特有：盘前盘后成交会改变持仓与成本）。
* `account.list_accounts()` 无美股专属参数，账户的 `market` / `currency` / `accountType`
  字段名**未定义**（`models.py` 为空），只能靠候选字段名猜测。

**B 档（适配器现状）**

```python
# src/ai_trader/broker/fosun_adapter.py::get_positions
resp = self._read(client.portfolio.get_holdings,
                  sub_account_id=sub, start=0, count=200,
                  apply_account_id=apply_acc)
```

❌ **没有传 `use_us_pre/post/night`**，三个标志都取默认 `False`。

后果：美股盘前/盘后发生的成交**可能不计入**返回的持仓（取决于网关实现）。
如果盘前成交了一笔买入，而持仓接口只返回常规时段持仓，风控会以为"没有这个仓位"，
进而：
* `max_position_pct` 失效（以为可以继续买）；
* 止损注册表与实际持仓不一致。

**这一条建议列为 P0**（若确认网关按标志区分），因为它是"风控看得见的仓位不完整"。

**C 档**：未验证。需要凭证后对比 `use_us_pre=True/False` 两次返回差异。

### 2.2 美股实时行情（REST）

**A 档**

```python
# fsopenapi/api/market.py
def quote(self, codes, fields=None):     # POST /v1/market/secu/quote
def kline(self, code, ktype, ...):       # POST /v1/market/kline
def min(self, code, count=5):            # GET  /v1/market/min
def orderbook(self, code, count=5):      # GET  /v1/market/secu/orderbook
def tick(self, code, count=20, id=-1):   # GET  /v1/market/secu/tick
def broker_list(self, code):             # GET  /v1/market/secu/brokerq
```

代码格式：`marketCode + stockCode`，美股即 `usAAPL`、`usNVDA`（README 明确给出）。
`market_push.py` 注释：`qt/min/tk 支持港美A`；`ob` 支持美股盘前盘后；`bq` 支持美股盘前盘后+夜盘。

**B 档**：`get_quote` / `get_quotes` / `get_klines` 均已实现，使用 `us*` 代码。

**已知上游问题（沿用 B4/B5 审计）**：
* 行情 POST 会被 SDK 加密（`client.py:114` 的判定写错），我方已做 GET `/v1/market/min` 兜底；
* `ob/tick/broker_list` 是 GET，不受该问题影响，但**我方未接入**（只用 quote/kline）。

**C 档**：未验证 —— 美股代码格式、字段名、是否需额外行情权限，全部待确认。

### 2.3 盘前 / 盘后 / 夜盘行情

**A 档（SDK 明确提供专用主题）**

| 主题 | 方法 | 注释含义 |
|---|---|---|
| `pqt` / `pmin` / `ptk` | `subscribePretMarketQuote/Min/Tick` | **美股盘前盘后**报价/分时/逐笔 |
| `qt_nt` / `min_nt` / `tk_nt` | `subscribeNightMarketQuote/Min/Tick` | **美股夜盘** |
| `ob` | `subscribeOrderbook` | 注释："支持美股盘前盘后" |
| `bq` | `subscribeBrokerQueue` | 注释："支持美股盘前盘后，美股夜盘" |
| `mkt` | `subscribeMarketStatus` | 市场状态（可用于判断开盘/收盘/停牌） |

**B 档**：❌ **完全未接入**。`market_feed.py` 只订阅 `market_feed.topics` 配置项，
默认 `["qt"]`，且解析器只认 `qt` 的字段结构。

**C 档**：未验证（需要 ws 地址 + 行情权限）。

**影响**：如果要在美股做"开盘前挂单/收盘后处理"，必须有盘前盘后行情；否则只能用
常规时段行情 + 上一日收盘价，信号质量差且无法感知盘前跳空。

### 2.4 美股订单类型

**A 档（`trade.py:34-62` 的全部下单参数）**

| 参数 | 我们的映射 | 美股语义（推定） |
|---|---|---|
| `direction` | 1=买 / 2=卖 | 明确 |
| `order_type` | **1=限价 / 3=市价** | README 只标注 `1 # 限价`；**3 的语义未标注** |
| `quantity` | 股数（`lot_size=1`） | 美股无整手，合理 |
| `price` | 限价单价格 | 明确 |
| `time_in_force` | ❌ **从不传** | DAY/GTC/GTD/IOC/FOK —— **枚举未知** |
| `exp_type` | ❌ 从不传 | 推测与"有效时段/盘前盘后"有关 —— **未验证** |
| `short_sell_type` | ❌ 从不传 | 推测为卖空类型（借券/普通） —— **未验证** |
| `trig_price` | ❌ 从不传 | 推测为触发价（Stop 单） —— **未验证** |
| `tail_type` / `tail_amount` / `tail_pct` | ❌ 从不传 | 推测为追踪止损（Trailing） —— **未验证** |
| `profit_trig_price` / `profit_quantity` | ✅ 传（止盈） | 推测为条件止盈 |
| `stop_loss_trig_price` / `stop_loss_quantity` | ✅ 传（止损） | 推测为条件止损 |
| `product_type` | ❌ 从不传 | 期权需要（15），正股可能不需要 |
| `apply_account_id` | ✅ 传 | OPS 场景必填 |

**结论**：

* **A 档丰富**：SDK 覆盖了限价/市价/止损/止盈/追踪/卖空/TIF/触发价等一整套参数；
* **B 档贫瘠**：我们只用了 `order_type=1|3` + 止损止盈价，**TIF/exp_type/short_sell_type/trig_price 全部未用**；
* **枚举未定义**：SDK 没有任何枚举常量或文档说明这些整数取值，`order_type=3` 到底是
  "市价"还是别的，**无法从源码确认** —— 这是**跨市场**的既有假设（美股同样受影响）。

**风险举例**：若 `order_type=3` 实际是"增强限价"而非"市价"，我们的"市价单"会变成
一个未指定价格的限价单，可能不成交或价格失控。

### 2.5 撤单与订单查询

**A 档**

```python
cancel_order(order_id, sub_account_id=None, client_id=None, product_type=None, apply_account_id=None)
list_orders(sub_account_id, apply_account_id=None, start, count, stock_code=None,
            status_arr=None, from_date, to_date, direction=None, market=None,
            sort="desc", client_id=None, show_type=None)
get_order_detail(sub_account_id, order_id, client_id=None, apply_account_id=None, product_type=None)
get_cash_flows(sub_account_id, trade_date_from=None, trade_date_to=None, ...)
```

* `list_orders` 有 **`market`** 参数 → 可按 `us` 过滤 ✅
* `show_type`：0=正股、1=正股+期权、2=只期权（README 注释）
* `get_order_detail` 返回 `feeDetail`（费用明细）与 `transactionList`（成交明细）—— **成交回报的数据来源**

**B 档**：`cancel_order` / `list_orders` / `get_order_detail` 均已实现，但 `list_orders`
**未传 `market`**（拉全市场订单），`get_order_detail` 未传 `product_type`。

**C 档**：未验证。

### 2.6 交易状态与成交回报

**A 档**

* 轮询：`list_orders(status_arr=[...])` + `get_order_detail(transactionList)`
* 推送：`create_subscription(event_type="orderUpdate", endpoint="https://...")`
  → **要求公网 HTTPS 回调地址**（`channel_type=1` 为 HTTP Webhook）

**B 档**：只做了**轮询**（`monitor()` 每轮 `list_orders` + 超时撤单 + 未知单对账）。
订阅（Webhook）**未接入**——需要公网地址，且 VPS 需配置 TLS。

**C 档**：未验证。`orderUpdate` 的回调报文结构**完全没有文档**。

---

## 3. 美股专属缺口清单

| # | 缺口 | 严重度 | 说明 |
|---|---|---|---|
| **G1** | 持仓未传 `use_us_pre/post/night` | **P0** | 盘前盘后成交可能不计入持仓 → 风控看不见真实仓位 |
| **G2** | 时段判断无 DST、周末用北京日期 | **P0** | 见 `US_MARKET_RULES_REPORT.md §2.1/2.2`（4 个错点） |
| **G3** | `enforce_markets: ["cn"]` | **P0** | 美股规则层未启用 |
| G4 | `order_type` 语义未确认（1/3） | **P1** | 可能把市价单下成限价单 |
| G5 | `time_in_force` 未传 | **P1** | 无法表达 GTC/IOC/FOK；美股 DAY 单默认假设未验证 |
| G6 | 盘前/盘后/夜盘行情未接 | **P1** | 无法做盘前决策与跳空判断 |
| G7 | 盘前/盘后**下单**能力未知 | **P1** | 需确认 `exp_type` 或专用字段 |
| G8 | 卖空（`short_sell_type`）未实现 | **P2** | 当前 `allow_short=false`，与需求一致；若要卖空需此项 |
| G9 | `ob`/`tick`/`broker_list` 未接入 | **P2** | 盘口与逐笔是美股高频信号的重要来源 |
| G10 | `orderUpdate` Webhook 未接入 | **P2** | 成交回报延迟取决于轮询间隔 |
| G11 | `get_order_detail` 未取 `feeDetail` | P2 | 成本核算依赖它（美股有 SEC/FINRA 费） |
| G12 | 无结算/可用资金模型 | P2 | T+1 结算；卖出资金可用性取决于券商 |

---

## 4. 需要真实凭证才能完成的项目

以下 **全部无法在无凭证情况下验证**，按依赖排序：

| # | 项目 | 需要的凭证/条件 | 验证方法 |
|---|---|---|---|
| 1 | 美股账户列表与字段名 | base_url + api_key + 两段 PEM | `account.list_accounts()` 看返回 |
| 2 | 美股持仓字段与 `useUs*` 行为 | 同上 | 对比 `use_us_pre=False/True` 两次返回 |
| 3 | 美股行情代码格式与字段 | 同上 + 行情权限 | `market.quote(["usAAPL"])` |
| 4 | 行情 POST 加密后网关是否接受 | 同上 | 见 `API_STATUS.md §2.2` |
| 5 | `order_type` 枚举 | 同上 + 官方参数表 | 沙盒逐一下单观察 |
| 6 | `time_in_force` / `exp_type` 枚举 | 同上 | 同上 |
| 7 | 盘前盘后下单是否支持 | 同上 + 券商权限 | 盘前时段下一笔小额限价单 |
| 8 | `short_sell_type` 语义 | 同上 + 卖空权限 | 沙盒卖空测试 |
| 9 | `orderUpdate` 回调报文 | 公网 HTTPS 地址 | 建订阅后观察回调 |
| 10 | 推送 `pqt/qt_nt` 主题 | ws 地址 + 行情权限 | 订阅后看是否收到 |
| 11 | 费用结构（`feeDetail`） | 同上 | 一笔成交后查详情 |
| 12 | 行情延迟等级 | 券商说明 | 与交易所时间戳比对 |

**建议**：拿到凭证后，先用官方**模拟账户**（`account.sim_account_create()`）做 1–12 的验证，
不要直接在真实账户上试 `order_type=3` 这类语义不明的参数。

---

## 5. 与"美股优先"的适配性结论

| 问题 | 回答 |
|---|---|
| 星财富 OpenAPI 能否支持美股交易？ | **从 SDK 看，能**：有 `market_code`/`currency`、美股盘前盘后夜盘标志、一整套订单参数、`show_type`、盘前盘后推送主题 |
| 适配器现在能交易美股吗？ | **技术上能下单，但不能安全交易**：时段判断错 1 小时、规则层不覆盖、TIF/订单类型未用、盘前盘后持仓可能不完整 |
| 最大的单点风险是什么？ | **G1（持仓不含盘前盘后）** + **G2（时段错 1 小时）** —— 前者让风控看不见仓位，后者让委托打在错误的时段 |
| 需要改多少代码？ | 时段：1 个函数（加 `zoneinfo`）；持仓：1 处传参；规则：1 段配置 + tick size 校验；订单类型：1 个映射表（需枚举）；盘前行情：1 个主题映射 |

---

## 6. 建议的美股开发顺序（与规则报告的任务编号对应）

| 阶段 | 任务 | 依赖 |
|---|---|---|
| 第 1 步（不依赖凭证） | US-T1/T2 时段与 DST、US-T3 规则生效市场、G1 持仓标志 | 无 |
| 第 2 步（不依赖凭证） | US-T5 节假日/提前收市、US-T4 tick size（需 TWAS 数据） | 数据源 |
| 第 3 步（部分依赖凭证） | G6 盘前行情接入（需 ws + 权限）、US-T6 三段时段 | 凭证 |
| 第 4 步（依赖凭证） | G4/G5/G7 订单类型与 TIF（需枚举文档）、G10 Webhook | 凭证 + 官方文档 |
| 第 5 步 | US-T7 熔断/停牌、US-T8 卖空、US-T9 结算资金 | 数据源 + 券商 |
