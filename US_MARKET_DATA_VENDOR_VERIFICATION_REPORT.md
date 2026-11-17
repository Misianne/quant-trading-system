# 第三方行情 API 数据源验证报告

**项目**：`C:\FosunAI-Trading`　**执行时间**：2026-11-17
**目标**：为日线波段策略寻找低成本数据源，替代星财富 $40/月 实时行情订阅
**结论**：**主源 = Twelve Data Basic（$0，已实测通过）；备源 = FMP Basic（$0，待注册复核）**
**关键提醒**：两家免费层的授权都**不能直接用于生产**；真要上实盘，最低成本的商业授权是 **Marketstack Basic $9.99/月**，而不是 Twelve Data 的 $79。

---

## 一、先说验证能力的边界（不掩饰）

任务要求"必须实际验证"。我能做到的和做不到的，必须分清：

| 数据源 | 是否拿到真实数据 | 说明 |
|---|---|---|
| **Twelve Data** | ✅ **是** | 用官方文档公开的 `demo` key 实测：AAPL/QQQ 日线 OHLCV、30 年历史深度、符号目录 |
| **FMP** | ❌ 否 | 官方 `demo` key 已失效（HTTP 401 `Invalid API KEY`）；免费 key 需注册 |
| **Marketstack** | ❌ 否 | **没有 demo key**；实测了无 key 的错误契约（`missing_access_key`） |
| **Nasdaq Data Link** | ❌ 否 | 本机访问被 **Incapsula WAF 403** 拦截（HTML 与 API 都是），连错误信息都拿不到 |

**我没有注册任何账号**——注册要绑定你的邮箱并代你接受服务条款，这属于需要你本人决定的事。因此 FMP / Marketstack / Nasdaq Data Link 的结论**全部标注为"文档声称，未实测"**，不冒充已验证。

**已归档的原始证据**：`reports/marketdata_probe/probe_results.json`（每家每标的的原始响应）、`td_qqq_30d.json`（30 根 QQQ 原始日线）、`td_symbol_search.json`（五标的符号目录）。

---

## 二、十项验证内容 × 四家 矩阵

图例：✅ 已实测　📄 文档声称（未实测）　❌ 不支持/不可用　💰 需付费

| # | 验证项 | Twelve Data | FMP | Nasdaq Data Link | Marketstack |
|---|---|---|---|---|---|
| 1 | 能否申请免费 Key | ✅ 有免费 Basic（`/register`） | 📄 Basic $0 | 📄 需注册 | 📄 Free $0 |
| 2 | 支持 NDX 指数 | ❌ **符号目录里查不到 NDX 指数**（21 条全是德国所股票） | 📄 未确认 | 📄 未确认 | ❌ **免费版不含指数**（Index 从 Basic 起） |
| 2 | 支持 QQQ | ✅ 实测有数据 | 📄 | 📄 | 📄 |
| 2 | 支持 QLD | ✅ **符号目录确认**：QLD / NYSE / ETF（ProShares Ultra QQQ）；OHLCV 💰 | 📄 | 📄 | 📄 |
| 2 | 支持 QID | ✅ **符号目录确认**：QID / NYSE / ETF；OHLCV 💰 | 📄 | 📄 | 📄 |
| 2 | 支持 TQQQ | ✅ **符号目录确认**：TQQQ / NASDAQ / ETF；OHLCV 💰 | 📄 | 📄 | 📄 |
| 3 | 日线 OHLCV | ✅ 实测 `open/high/low/close/volume` | 📄 EOD 含 | 📄 | 📄 EOD 含 |
| 4 | 历史深度 | ✅ **实测最早 1995-01-03**（AAPL） | 📄 免费 **5 年** / Premium 30 年 | 📄 未确认 | 📄 免费 **1 年** / Basic 10 年 / Pro 15 年 |
| 5 | 复权/分红/拆股 | 📄 接受 `adjust` 参数；Splits/Dividends 端点按套餐开放 | 📄 有拆股分红调整接口 | 📄 未确认 | 📄 免费版含 Splits & Dividends |
| 6 | 延迟行情 | ✅ 实测**含当日未收盘 bar**（见 §4.3） | 📄 免费版"Real-time: Not included"（即仅 EOD） | 📄 | 📄 免费版仅 EOD；实时从 Professional $49.99 起 |
| 7 | WebSocket | 💰 免费仅 **8 trial WS credits**；正式 WS 从 **Pro $229**（1500 WS） | 📄 未确认（免费版无实时） | 📄 | 📄 无（以 REST 为主） |
| 8 | 免费额度 | ✅ **8 credits/分，800/日** | 📄 **250 次/日** | 📄 未确认 | 📄 **100 次/月** |
| 8 | 付费价格 | ✅ Grow **$79**／Pro **$229**／Ultra **$999** 每月 | ✅ Starter **$29**（年付 $19）／Premium **$69**（$49）／Ultimate **$139**（$99） | 📄 页面纯 JS，未取到数字 | ✅ Basic **$9.99**（年付 $8.99）／Professional **$49.99**／Business **$149.99** |
| 8 | 授权限制 | ⚠️ **免费层"仅测试/评估/开发，不得用于生产系统、不得对外展示"** | 📄 未取到条款 | 📄 | ⚠️ **免费层 Non-Commercial**；商业用途从 Basic 起 |
| 9 | 时间戳/时区 | ✅ 响应含 `meta.exchange_timezone=America/New_York`，`datetime` 为交易日 `YYYY-MM-DD` | 📄 | 📄 | 📄 |
| 9 | 缺失数据 | ✅ 30 天窗口 29 个重叠交易日，无缺；仅多出当日未收盘 bar | 未测 | 未测 | 未测 |

---

## 三、实际测试记录（可复核）

### 3.1 Twelve Data —— 真的取到了数据

**请求**（官方文档公开的 `demo` key，非付费）：
```
GET https://api.twelvedata.com/time_series?symbol=QQQ&interval=1day&outputsize=30&apikey=demo
```
**响应元数据**：
```json
{"symbol":"QQQ","interval":"1day","currency":"USD",
 "exchange_timezone":"America/New_York","exchange":"NASDAQ","mic_code":"XNMS","type":"ETF"}
```
返回 30 根，`2026-10-07 .. 2026-11-17`。

**与本地 `us_bars`（Yahoo 口径）逐日比对**——这是本轮最有说服力的一条：

| 日期 | TD close | 本地 close | 偏差 | TD volume | 本地 volume |
|---|---:|---:|---:|---:|---:|
| 2026-11-05 | 647.05 | 647.05 | 0.00 bp | 130,677,400 | 130,677,400 |
| 2026-11-06 | 656.10 | 656.10 | 0.00 bp | 70,691,400 | 70,691,400 |
| 2026-11-09 | 679.35 | 679.35 | 0.00 bp | 54,905,600 | 54,905,600 |
| 2026-11-13 | 679.89 | 679.89 | 0.00 bp | 42,098,600 | 42,098,600 |
| 2026-11-16 | 661.75 | 661.75 | 0.00 bp | 42,801,200 | 42,801,200 |

**29 个重叠交易日：收盘价最大偏差 0.00 bp、中位 0.00 bp，成交量逐日一致。**

### 3.2 五个标的的实际可达性

| 标的 | `time_series`（demo key） | 符号目录 |
|---|---|---|
| AAPL | ✅ 返回日线 | ✅ NASDAQ / Common Stock |
| QQQ | ✅ 返回日线 | ✅ NASDAQ / ETF |
| QLD | ❌ `401 The 'demo' API key is only used for initial familiarity` | ✅ **NYSE / ETF（ProShares Ultra QQQ）** |
| QID | ❌ 同上 | ✅ **NYSE / ETF** |
| TQQQ | ❌ 同上 | ✅ **NASDAQ / ETF** |
| NDX | ❌ 同上 | ❌ **未找到纳斯达克 100 指数**（21 条结果全是 `NDX1`/`NDX` 的德国交易所 ADR/普通股） |

> **这条必须说清楚**：`demo` key 是"固定白名单"（AAPL、QQQ 等），**不是"不支持"**。QLD/QID/TQQQ 的 OHLCV 需要真实免费 key 才能验证，本轮标记为**待验证**；而 NDX 是**符号目录里就没有指数**，这是"大概率不支持"，需用真实 key 再确认。

### 3.3 历史深度实测

```
GET .../time_series?symbol=AAPL&interval=1day&start_date=1995-01-01&end_date=1995-01-15&apikey=demo
→ 9 根，最早 1995-01-03
```
2005、2015 同样正常返回。**免费层至少可回溯到 1995 年（30 年）**，长期回测完全够用。

### 3.4 另外三家的错误契约（这些是真的测到了）

```json
// Marketstack 无 key
{"error":{"code":"missing_access_key",
          "message":"You have not supplied an API Access Key. [Required format: access_key=YOUR_ACCESS_KEY]"}}

// FMP demo key
{"Error Message":"Invalid API KEY. Feel free to create a Free API Key or visit ..."}
```
```
// Nasdaq Data Link（任意路径）
HTTP 403  <html ... _Incapsula_Resource ...>   ← WAF 拦截页，不是 API 响应
```

---

## 四、逐项关键结论

### 4.1 免费额度够不够？

日线波段策略每天只需要：**5 个标的 × 1 次 = 5 次请求**（收盘后跑一次）。

| 数据源 | 免费额度 | 日需求占比 | 结论 |
|---|---|---|---|
| Twelve Data | 800/日 | **0.6%** | 绰绰有余 |
| FMP | 250/日 | 2% | 够 |
| Marketstack | **100/月** | 100 次/月 ÷ 5 = **20 天** | ⚠️ **一个月只够跑 20 天**，不够用 |

**Marketstack 免费版额度是硬伤**：100 次/月看着不少，但按 5 标的算，一个月只能覆盖 20 个交易日。

### 4.2 要不要为 WebSocket 付费？

**不要，而且这个项目已经不需要它了。** 上一轮已把架构切到日线（`market_feed.enabled=false`，WS 不参与启动/策略/模拟交易）。日线策略每天收盘后取一次数据即可。
Twelve Data 的 WS 要 **Pro $229/月** 才有正式额度（免费层只有 8 个 trial credits），**为 WS 付费是纯浪费**。

### 4.3 ⚠️ 一个必须防范的坑：日线接口会返回"当日未收盘 bar"

Twelve Data 的 `2026-11-17` 那根 bar（open 663.08 / close 661.71 / volume **33,295**）是**当日盘中快照**，成交量只有正常日的千分之一。
如果直接把返回值当"最新收盘价"，就会用一根随时会变的半成品 bar 去决策。

**本项目的 `market/daily_gate.py` 已经能挡住这种情况**——它要求 `decision_date` 必须等于最后一根 bar 的日期且该日必须已收盘；把 `2026-11-17` 当作决策日会被判 `DATA_STALE`。接入新数据源时**必须保留这道闸门**，不能直接 `values[0]`。

### 4.4 授权限制（最容易被忽略、但最关键）

| 数据源 | 免费层原文 | 影响 |
|---|---|---|
| Twelve Data | "Data may be used internally for testing, evaluation, or development purposes only. The data cannot be displayed to users, shared externally, or **used in production systems**." | **当前阶段（回测+模拟）合规；上实盘不合规** |
| Marketstack | Free 套餐标注 "**Non-Commercial Use**" | 同上，且**历史只有 1 年** |
| FMP | 未取到条款 | 待确认 |

**这直接决定了推荐的分层**：免费层只够支撑"回测与模拟"，一旦要跑真钱，必须换商业授权。

---

## 五、NDX 指数 vs QQQ ETF：到底有没有明显差异？

四个候选源里**没有一个**能在免费/未注册状态下取到 NDX。为回答这个问题，我用项目已在使用、且已验证可靠的 **Yahoo 公开 chart 端点（`^NDX`）** 做参考口径，取 5 年日线比对（**明确标注：参考源不是候选源**）：

| 指标 | 结果 |
|---|---|
| 重叠交易日 | 1254（2021-11-17 … 2026-11-16） |
| **日收益相关系数** | **0.999398** |
| beta（NDX 对 QQQ） | **1.006963** |
| 年化波动 | NDX 23.56% vs QQQ 23.39% |
| 5 年累计 | NDX **+66.77%** vs QQQ **+66.42%**（差 **-0.35pp**） |
| 年化收益 | NDX +10.82% vs QQQ +10.78%（年差 **-0.05%**） |
| 日收益绝对差 | 均值 **3.69 bp**，最大 34.3 bp |

**结论：不存在实质差异。** 相关系数 0.9994、beta 1.007，5 年只差 0.35 个百分点（ETF 费率与跟踪误差的量级）。

**实践含义（很重要）**：
1. **不需要 NDX。** 用 QQQ 做趋势判断与 NDX 等价，而且 QQQ 可交易、NDX 不可交易。
2. **反过来更好**：策略信号本来就算在 QQQ 上，省掉"指数↔ETF"的映射与对齐问题。
3. 因此"某数据源不支持 NDX" **不构成选型否决项** —— 这是本轮最省钱的结论。

---

## 六、成本对比：能不能真的省下 $40/月

| 方案 | 月成本 | 历史 | 商业授权 | 备注 |
|---|---:|---|---|---|
| **现状：星财富实时行情** | **$40** | — | — | 含实时推送，但美股 `qt` 仍 `denied` |
| Twelve Data Basic | **$0** | 30 年 | ❌ 仅测试/评估 | 当前阶段够用 |
| Twelve Data Grow | **$79** | 30 年 | ✅ | **比现状还贵 98%，不划算** |
| FMP Basic | **$0** | 5 年 | 待确认 | 250/日 |
| FMP Starter | **$29**（年付 $19） | 5 年 | 待确认 | 含实时 |
| **Marketstack Basic** | **$9.99**（年付 **$8.99**） | 10 年 | ✅ | **唯一低于 $40 的商业方案** |
| Marketstack Free | $0 | **1 年** | ❌ 非商业 | 100 次/月，不够跑 |

**关键洞察**：真正能省钱的不是"免费层"（不能上生产），而是 **Marketstack Basic $9.99/月**——比现状省 **75%**，且给 10 年历史 + 商业授权。Twelve Data 想上生产要 $79，反而更贵。

---

## 七、最终推荐

### 主数据源：Twelve Data（Basic，$0）

**理由**：
1. **唯一被实际验证过的**——QQQ/AAPL 日线取到了，且与本地数据 **0.00 bp 完全一致**；
2. 历史深度实测到 **1995 年**，足够长期回测；
3. 800 次/日 对 5 标的×1 次/天有 **160 倍余量**；
4. 响应自带 `exchange_timezone=America/New_York`，与项目既有 `trade_date` 口径一致。

**限定条件（必须遵守）**：
- 仅用于**回测与模拟**；免费层授权明确禁止生产使用；
- 接入时必须过 `daily_gate`，**丢掉当日未收盘 bar**（§4.3）；
- QLD/QID/TQQQ 的实际 OHLCV 需用真实免费 key 复核（符号目录已确认存在）。

### 备用数据源：FMP（Basic，$0）

**理由**：免费 250 次/日、**5 年** EOD、是唯一与 Twelve Data 量级相当且完全免费的备选；5 年对本策略足够（现有回测窗口就是 5 年）。
**待办**：`demo` key 已失效，需注册后用真实 key 复核三件事——① QQQ/QLD/QID/TQQQ 是否都能取 EOD；② 是否含拆股/分红调整；③ 免费层授权条款。

### 若要上生产：Marketstack Basic（$9.99/月，年付 $8.99）

**理由**：唯一低于 $40 的商业授权方案，10 年历史 + Splits & Dividends + 指数（NDX 也在内）。
**代价**：免费层额度只有 100 次/月，**必须付费**才能日常跑；且未实测过数据质量（无 demo key），上线前需要一周的灰度对比。

### 明确不推荐

- **Nasdaq Data Link**：本机被 WAF 403 拦截，**连文档价格都取不到**，无法评估；且其免费层以"数据集"为单位，美股日线多为第三方数据集，授权与稳定性都要重新评估。**本轮直接排除**。
- **Twelve Data Grow（$79）**：为拿商业授权付 $79，比现有 $40 更贵，与"降本"目标相反。
- **任何为 WebSocket 付费的方案**：日线架构不需要 WS，付费纯浪费。

---

## 八、尚未解决 / 需要你决定

1. **是否授权我注册免费账号？** FMP 与 Marketstack 的免费 key 需要邮箱注册（无信用卡）。你给一个邮箱（或授权我用现有邮箱），我可以在 10 分钟内补齐 QQQ/QLD/QID/TQQQ 的实测数据与授权条款核对。
2. **QLD/QID/TQQQ 在 Twelve Data 的真实 OHLCV 未验证** —— 需要真实免费 key；符号存在已确认，但"存在"不等于"免费层可读"。
3. **NDX 在四个候选源中的可用性未验证** —— 但 §五 已证明**不需要 NDX**，建议直接放弃这个需求。
4. **Marketstack 免费层 100 次/月** 是否够你未来多标的扩展（比如加到 20 个标的），需要重新算。
5. **数据授权**：免费层都不能上生产；一旦要跑真钱，必须切到 Marketstack Basic 或 FMP 付费档，这一步需要你确认预算。

---

## 九、附：本轮没有做的事

- 未注册任何账号、未提交任何申请、未购买任何套餐
- 未修改现有生产数据源（`us_bars` 仍是 Yahoo 口径，未动）
- 未把任何 API Key 写入源码、日志或报告（本轮用的 `demo` 是官方文档公开的测试 key，非私人凭据）
- 未启用真实交易
