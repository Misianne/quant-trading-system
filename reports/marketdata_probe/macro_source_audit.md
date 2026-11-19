# 宏观经济数据源审计（阶段二）

**项目**：`C:\FosunAI-Trading`　**时间**：2026-11-19T06:2x–06:3xZ
**范围**：FRED / BLS / U.S. Treasury Fiscal Data　**性质**：只读调查与实测，未建设决策引擎
**证据**：`reports/marketdata_probe/macro_probe/`（8 个原始响应文件）

---

## 一、结论

| 数据源 | 能否免注册取数 | 历史深度 | **能否保留首次公布值（vintage）** | 费用 | 结论 |
|---|---|---|---|---|---|
| **FRED** | ❌ 需免费 Key | 极深（数十年） | ✅ **唯一支持**（`output_type=4`） | 免费 | **主源** |
| **BLS** | ✅ **可**（v1/v2 均实测通过） | **10 年**（无 key）/ 20 年（注册） | ❌ **无 vintage API** | 免费 | **校验源** |
| **Treasury Fiscal Data** | ✅ **可**（完全免 key） | 1993 起 | ➖ 不适用（数据不修订） | 免费 | **财政/利率专用** |

**推荐组合**：

```
宏观基本面（CPI/PCE/就业/GDP/联邦基金利率/国债收益率）
        ├── 主源：FRED（唯一能提供"当时看到的值"）
        ├── 校验：BLS（CPI/非农/时薪的原始发布方，免注册）
        └── 补充：Treasury（现金余额、国债余额、日度收益率曲线，免 key）
```

**为什么必须用 FRED 做主源**：任务清单里的 12 项数据横跨 **BLS、BEA、美联储、财政部** 四个机构。FRED 是唯一把它们收在一处的聚合器；更关键的是，**只有 FRED 能用 API 取回"首次公布值"**（见 §四）。没有它，§三那个"不能用未来才知道的修订值"的硬要求就无法用程序满足。

---

## 二、逐项评分

| 维度 | FRED | BLS | Treasury Fiscal Data |
|---|---|---|---|
| **数据质量** | 高（官方转载，注明来源与单位） | **最高**（原始发布方） | 高（官方一手） |
| **历史深度** | 极深（CPI 可回溯到 1947，未逐条验证） | **10 年**（免注册，已实测）；20 年（注册） | DTS 现金 **2005-10-03**、国债余额 **1993-04-01**、收益率曲线 CSV **2010 年起**（均已实测） |
| **更新频率** | 跟随源发布（月/季/日） | CPI 月度、就业月度 | DTS **每工作日**、国债余额每工作日、收益率曲线每交易日 |
| **授权** | 多数序列为公共领域；**部分第三方序列有再分发限制**（未逐条核实） | 美国政府作品，公共领域 | 美国政府作品，公共领域 |
| **费用** | **免费**（需注册 32 位 key） | **免费**（v1 无需注册） | **完全免费、无需 key** |
| **接口稳定性** | 端点可达（实测 400 参数校验），数据通道待验 | **实测稳定**（多次调用均 200） | **实测稳定**（多次调用均 200） |
| **本机网络可达性** | ⚠️ **`fredgraph.csv` 通道被切断**（见 §3.1） | ✅ 正常 | ✅ 正常 |

---

## 三、实测记录

### 3.1 FRED —— 端点活着，但本机拿不到数据

| 测试 | 结果 |
|---|---|
| `GET api.stlouisfed.org/fred/series/observations?series_id=CPIAUCSL`（无 key） | **HTTP 400** · `"api_key is not set"` |
| 同上，带伪造 key | **HTTP 400** · `"The value for variable api_key is not a 32 character alpha-numeric lower-case string"` |
| `GET fred.stlouisfed.org/graph/fredgraph.csv?id=CPIAUCSL`（免 key 的 CSV 通道） | ❌ **`RemoteDisconnected` / `schannel: server closed abruptly`**，0 字节。Python TLS 与 curl 均失败，带 Referer 也一样 |
| `GET alfred.stlouisfed.org/graph/fredgraph.csv?...&vintage_date=...` | ❌ 同样被切断 |
| `GET fred.stlouisfed.org/series/CPIAUCSL`（HTML 页面） | ✅ **HTTP 200**，91,918 字节 |

**判断**：`api.stlouisfed.org` 完全可达（连 key 格式都校验了），但 **`fredgraph.csv` 这类免 key 的数据通道在本机网络下被切断**。所以：

> **没有 FRED API Key，本轮无法取到任何一条 FRED 数据。** 我没有伪造结果。

**注册准备**：需在 `https://fred.stlouisfed.org/docs/api/api_key.html` 申请，key 为 **32 位小写字母数字**。建议存入用户环境变量 `FRED_API_KEY`（与已有的 `TWELVE_DATA_API_KEY` 同法，不进代码/日志）。

### 3.2 BLS —— 免注册可用，但有个"吃掉最新一年"的坑

**可用性（实测 200）**

```
GET  api.bls.gov/publicAPI/v1/timeseries/data/CUUR0000SA0
→ {"status":"REQUEST_SUCCEEDED", ... "year":"2026","period":"M10","periodName":"October",
   "latest":"true","value":"337.511"}
```

```
POST api.bls.gov/publicAPI/v1/timeseries/data/
{"seriesid":["CUUR0000SA0","CUUR0000SA0L1E","LNS14000000","CES0000000001","CES0500000003"],
 "startyear":"2016","endyear":"2026"}
→ 5 条序列全部返回，各 120 个观测
```

| 序列（实测） | 含义 | 最新值 |
|---|---|---|
| `CUUR0000SA0` | CPI-U 全项目 | 2026-10 = **337.511** |
| `CUUR0000SA0L1E` | 核心 CPI | 2026-10 = 330.506 |
| `LNS14000000` | 失业率 | 2026-09 = 4.4 |
| `CES0000000001` | 非农就业（千人） | 2026-09 = 158,432 |
| `CES0500000003` | 平均时薪 | 2026-09 = 37.02 |

**⚠️ 关键陷阱（实测发现）**

| 请求区间 | 实际返回 | 官方 `message` |
|---|---|---|
| 2016–2026（11 年） | **2016-01 .. 2025-12** | `"Year range has been reduced to the system-allowed limit of 10 years."` |
| 2017–2026（10 年） | 2017-01 .. **2026-10** | `[]`（无提示） |

**它丢的是最新的一年，不是最旧的一年**，而且**仍返回 `REQUEST_SUCCEEDED`**。如果代码只看 `status` 不读 `message`，就会把 2025-12 当成"最新数据"——比缺数据更危险。

**注册与额度（官方文档 + 实测）**

| 项 | v1（免注册） | v2 免注册 | v2 注册后 |
|---|---|---|---|
| 单查询序列数 | 25 | 25 | **50** |
| 时间跨度 | **10 年**（实测） | **10 年**（实测，未因 v2 放宽） | **20 年** |
| 默认 GET 窗口 | 3 年 | 3 年 | 3 年 |
| 费用 | 免费 | 免费 | 免费 |

> 实测：v2 端点**不填 registrationkey 也能返回 200**，但跨度仍是 10 年（请求 2006–2026 只拿到 2006–2015）。所以"v2 免 key 能拿 20 年"是**错的**。

**修订机制**：API **只返回当前值**，每个 period 只有一个 `value`。唯一的"临时"标记是 `footnotes`：

```json
"footnotes": [{"code": "P", "text": "Preliminary."}]
```

这不是版本历史，只是"这条是初值"的标签。**BLS 官方 API 没有任何 vintage 参数。**

### 3.3 Treasury Fiscal Data —— 完全免 key，三类数据都能拿

| 数据集 | 端点 | 实测结果 |
|---|---|---|
| 财政部现金余额（DTS） | `v1/accounting/dts/operating_cash_balance` | ✅ 200 · `record_date=2026-11-17` · TGA 期初余额 **895,774**（百万美元）· 最早 **2005-10-03** · 16,786 条 |
| 国债余额（Debt to the Penny） | `v2/accounting/od/debt_to_penny` | ✅ 200 · `2026-11-17` 公众持有 32,887,006,494,499.79 · 总债务 **40,714,946,316,573.34** · 最早 **1993-04-01** · 8,436 条 |
| 国债平均利率 | `v2/accounting/od/avg_interest_rates` | ✅ 200 · `2026-10-31` · Bills 4.014% |
| **日度收益率曲线** | ⚠️ **不在 Fiscal Data API 里** | `/v1/accounting/od/daily_treasury_yield_curve` → **404** |

**收益率曲线要走 treasury.gov，不是 Fiscal Data API**（实测均 200）：

```
CSV : home.treasury.gov/.../daily-treasury-rates.csv/2026/all?type=daily_treasury_yield_curve&...
XML : home.treasury.gov/.../interest-rates/pages/xml?data=daily_treasury_yield_curve&...
```

CSV 头两行（真实数据）：

```csv
Date,"1 Mo","1.5 Month","2 Mo","3 Mo","4 Mo","6 Mo","1 Yr","2 Yr","3 Yr","5 Yr","7 Yr","10 Yr","20 Yr","30 Yr"
11/18/2026,4.10,4.24,4.27,4.34,4.35,4.43,4.62,4.80,4.84,4.89,4.96,5.07,5.44,5.41
```

**一次拿到 14 个期限**（含 2Yr 4.80、10Yr 5.07），按年分文件：2026 = 221 行、2020 = 251 行、2010 = 252 行——**至少回溯到 2010 已实测**。

---

## 四、重点：能否保留"当时市场看到的那个值"

这是本任务最硬的要求。结论：

### 4.1 三家能力对比

| 能力 | FRED | BLS | Treasury |
|---|---|---|---|
| 数据所属期间 | ✅ `date` | ✅ `year`+`period` | ✅ `record_date` |
| **首次公布值** | ✅ **`output_type=4`（Initial Release Only）** | ❌ 无；只能靠 `footnotes=Preliminary` 猜 | ➖ 数据不修订 |
| **完整修订历史** | ✅ `output_type=2`（全部）/ `3`（仅新增与修订） | ❌ | ➖ |
| **按发布日还原** | ✅ `realtime_start` / `realtime_end` | ❌ | ❌ |
| **指定 vintage 快照** | ✅ `vintage_dates=2020-04-29,...` | ❌ | ❌ |
| 实际发布时间 | ✅ 观测带 `realtime_start/end`；另有 `release/dates` 接口 | ⚠️ 需自行记录调用时间 | ⚠️ 同上 |

### 4.2 FRED 的官方契约（原文摘录）

> `output_type`：`1` = Observations by Real-Time Period；`2` = Observations by Vintage Date, All Observations；`3` = Observations by Vintage Date, New and Revised Observations Only；**`4` = Observations, Initial Release Only**

> `vintage_dates`："Vintage dates are used to download data **as it existed on these specified dates in history**."

> `realtime_start`/`realtime_end` 默认 = **今天**——这就是"只拿到最终修订值"这一常见错误的根源。

**取首次公布值（推荐）**：

```
GET /fred/series/observations?series_id=CPIAUCSL&output_type=4&api_key=...
```

**取完整修订轨迹**：

```
GET /fred/series/observations?series_id=CPIAUCSL&output_type=2&api_key=...
（体积太大时用 output_type=3，只回新增与被修订的观测）
```

### 4.3 建议的点时间（point-in-time）存储模型

**这是本轮唯一需要立刻定下来的设计**，否则以后回测会系统性使用未来信息：

```sql
CREATE TABLE macro_observations (
    series_id    TEXT NOT NULL,   -- CPIAUCSL
    period_end   TEXT NOT NULL,   -- 数据所属期间（2026-09-01）
    vintage_date TEXT NOT NULL,   -- 这条值"在当时库里存在"的日期
    value        REAL,
    release_ts   TEXT,            -- 实际发布时间（能取到就填）
    source       TEXT NOT NULL,   -- fred / bls / treasury
    source_url   TEXT,
    ingested_at  TEXT NOT NULL,
    PRIMARY KEY (series_id, period_end, vintage_date)
);
```

回测取数铁律：**只允许 `realtime_end <= 决策时点`**。

```sql
-- 站在 2026-10-15 这一天，CPI 当时是多少
SELECT value FROM macro_observations
WHERE series_id='CPIAUCSL' AND realtime_end <= '2026-10-15'
ORDER BY vintage_date DESC LIMIT 1;
```

有了这张表，"首次公布 3.2%、后来修订为 3.1%"就能被完整表达：同一 `period_end` 有多行，靠 `vintage_date` 区分。

### 4.4 BLS / Treasury 怎么办

- **BLS**：API 拿不到历史版本。可行路径是 **①每次发布时立刻抓一次并落库**（自建 vintage，从接入日开始积累）；**②用 FRED/ALFRED 镜像的 BLS 序列**（CPIAUCSL、PAYEMS 等本身就在 FRED 里，且带 vintage）。**推荐 ②**，这也是"用 FRED 做主源"的第二个理由。
- **Treasury**：DTS、国债余额、收益率曲线在实务上不修订（只可能因错误重发）。按"发布即终值"处理即可，但**仍建议记录 `ingested_at`**，以便发现异常重发。

---

## 五、12 项数据的来源映射

| 需求 | 主源（含 vintage） | 免注册校验源 | 说明 |
|---|---|---|---|
| CPI | FRED `CPIAUCSL` | **BLS `CUUR0000SA0`** ✅实测 | BLS 是原始发布方 |
| 核心 CPI | FRED `CPILFESL` | **BLS `CUUR0000SA0L1E`** ✅实测 | |
| PCE / 核心 PCE | FRED `PCEPI` / `PCEPILFE` | ❌ BLS 不产 | 原始发布方是 **BEA**（本轮未测） |
| 非农就业 | FRED `PAYEMS` | **BLS `CES0000000001`** ✅实测 | |
| 失业率 | FRED `UNRATE` | **BLS `LNS14000000`** ✅实测 | |
| 平均时薪 | FRED `CES0500000003` | **BLS `CES0500000003`** ✅实测 | |
| GDP 增长率 | FRED `GDPC1` / `A191RL1Q225SBEA` | ❌ BLS 不产 | 原始发布方是 **BEA**（本轮未测） |
| 联邦基金利率 | FRED `DFF` / `EFFR` | ❌ | 原始发布方是 **纽约联储**（本轮未测） |
| 2Y / 10Y 收益率 | FRED `DGS2` / `DGS10` | **Treasury CSV** ✅实测 | CSV 一次给 14 个期限 |
| 收益率曲线 | FRED `T10Y2Y` | **Treasury CSV** ✅实测 | CSV 可直接算任意期限利差 |
| 财政部现金余额 | — | **Fiscal Data DTS** ✅实测 | 2005-10-03 起 |
| 国债相关 | — | **Fiscal Data debt_to_penny** ✅实测 | 1993-04-01 起 |

> **结构性发现**：这 12 项横跨 **BLS / BEA / 美联储 / 财政部** 四个机构。若不用 FRED 聚合，就要分别对接四家、四套口径、四套修订规则。**FRED 的价值主要在"统一 + vintage"，不在"数据本身"**。

---

## 六、尚未验证的项目（明确标注，不冒充）

| # | 项目 | 原因 |
|---|---|---|
| 1 | **FRED 实际取数**（CPI/PCE/就业/利率/收益率） | 无 API Key；免 key 的 CSV 通道在本机被切断 |
| 2 | FRED 的请求限额（具体数字） | 官方未公开硬性数值，需注册后在账户页确认 |
| 3 | FRED 各序列的**授权**（是否有第三方再分发限制） | 需逐序列核对，本轮未做 |
| 4 | 上表所有 **FRED 序列 ID 的真实返回值** | 同上①；ID 取自公开目录，未取数验证 |
| 5 | BLS **v2 注册后**的 20 年跨度与 50 序列上限 | 无注册 key；仅验到"免 key 仍限 10 年" |
| 6 | BLS 每日调用额度 | 文档称注册用户更高，具体数值未核实 |
| 7 | **BEA**（PCE/GDP 的原始发布方）与 **纽约联储**（EFFR） | 不在本轮三个来源范围内 |
| 8 | Treasury 数据是否曾经"重发/更正" | 只验了字段结构无版本，未查历史公告 |
| 9 | Treasury 收益率曲线 2010 年之前 | 只验了 2010/2020/2026 三年 |
| 10 | 各源在**盘中/发布瞬间**的可用性 | 只做了静态拉取 |

---

## 七、下一步（需要你决定）

1. **申请 FRED API Key**（免费，32 位）。拿到后我用与 Twelve Data 相同的方式配置到用户环境变量 `FRED_API_KEY`，不进代码/日志，然后补测 §六 的 1/2/4 项。
2. **确认是否要建 `macro_observations` 表**。这是回测正确性的前提，但会动数据库——按你的规则需要你批准。
3. **确认是否把 BEA 与纽约联储**纳入下一轮调查（PCE/GDP/EFFR 的原始发布方）。

---

## 八、安全边界确认

- 未改现有交易策略、未改行情数据库（`us_bars` 未写入）、未删除任何历史数据
- 未连接实盘、未提交订单、未购买任何套餐
- 未绕过任何数据源限制（FRED 的 CSV 通道被切断后**没有尝试代理/规避**，只记录失败）
- 未输出任何 API Key（BLS/Treasury 本轮全部免注册调用，不存在 key）
- 所有原始响应已归档到 `reports/marketdata_probe/macro_probe/`，可逐条复核
