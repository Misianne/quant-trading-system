# 真实数据源接入准备报告（US Real Data Source Readiness）

**项目**：`C:\FosunAI-Trading`
**轮次**：真实数据源接入攻坚
**日期**：2026-11-16

---

## 0. 四个结论

| 结论 | 判定 | 说明 |
|---|---|---|
| **A. 离线 Replay** | **PASS** | 10 场景 ×3 次指纹一致；1169 项测试 0 失败 |
| **B. Paper Runtime** | **PASS** | 全链路执行，不变量通过，2 小时长跑已验证（上一阶段） |
| **C. 真实行情接入层** | **PARTIAL** | 模型/适配器/质量门禁/权威路由/离线夹具全部实现并通过测试；**未连真实端点**（无 Key、未采购、未授权） |
| **D. 交易所权威事件认证** | **BLOCKED** | 无任何自助供应商提供消息签名；`require_signature_for_resume=true` 下未签名事件一律不能恢复交易 |

---

## 1. 基线与备份

| 项 | 值 |
|---|---|
| 起始 HEAD | `559f8e3`（`docs(paper): 120-minute stability report...`） |
| 分支 | `master`，工作树干净 |
| Git 检查点 | 标签 `baseline-round7` → `559f8e3` |
| 本轮备份 | `C:\FosunAI-Trading-Backups\round7-20261116-210706` |
| 备份规模 | **2,155 个文件 / 52 MB**，`MANIFEST.sha256` 2,155 行 |
| SHA256 验证 | `sha256sum -c` → **0 条失败** |
| 历史数据 | `data/paper_runs` **17,224 个文件 / 548 MB 原地保留**，另生成 `PAPER_RUNS_INVENTORY.tsv` |
| 安全配置 | `us_phase2.enabled=false`、`paper_runtime.enabled=false`、`short.mode=disabled`（均未改动） |

> 备份排除 `data/paper_runs`（548 MB 运行产物），其余全量包含。

---

## 2. 上一轮遗留：测试数量不一致

用户指出存在 1038 / 1043 / 954 三个数字。核查结果：

| 数字 | 来源 | 时间 | 性质 |
|---|---|---|---|
| 954 | `r9.xml` | 17:17 | **上一阶段基线**（真实） |
| 1038 | `t_stab_full.xml` | 19:11 | **中间态**：加入 `test_paper_runtime_random_kill.py`（5 项）之前的全量结果 |
| 1043 | `r10.xml` | 20:48 | **本轮起点真实值**：1038 + 5 = 1043 |

**根因**：报告 §9 已更新为 1043，但 §0 结论表里残留了 1038（编辑时只替换了 §9 的字符串，
漏了带粗体标记的那一行）。这是**报告一致性问题，不是测试问题**。

**修复**：§0 已改为 1043；并用 `pytest --collect-only` 复核当前代码为 **1169 tests collected**
（1043 + 本轮新增 126），与 `r11.xml` 的 `tests="1169"` 完全一致。

| 核验项 | 结果 |
|---|---|
| 实际测试总数（本轮） | **1169** |
| passed / failed / skipped / error | **1168 / 0 / 1 / 0** |
| 新增测试 | **126** |
| 对应 commit | 见 §9（本轮提交） |
| 报告与 XML 是否一致 | **一致**（r11.xml `tests="1169" failures="0" errors="0" skipped="1"`） |

---

## 3. 供应商能力对比（官方文档证据）

> 以下均为**官方文档/官方站点**可查信息，查询时间 2026-11-16。
> 价格与套餐会变，接入前需再次核对。标注 `UNVERIFIED` 的项目表示未能从官方文档确认。

### 3.1 总览

| 能力 | Massive（原 Polygon.io） | Alpaca | Databento | Nasdaq 官方 | NYSE 官方 |
|---|---|---|---|---|---|
| 美股全市场 | ✅ 32,000+ 标的、55+ 场所 | ✅（付费档） | ✅ | 仅 Nasdaq 上市/在 Nasdaq 交易 | 仅 NYSE 系 |
| SIP 合并行情 | ✅ | ✅（Algo Trader Plus） | ❌（交易所直连，非 SIP） | ❌（单交易所） | ❌（单交易所） |
| 实时 | ✅（Advanced） | ✅（付费档） | ✅ | ✅ | ✅ |
| 延迟档 | 15 分钟（Starter/Developer） | 15 分钟（Basic 的 SIP 部分） | — | — | — |
| 历史 | 20+ 年 | 2016 起 | 5 年（美股） | 多年（TAQ/历史产品） | 2015 起（TAQ） |
| 盘前/常规/盘后 | ✅（含隔夜，23/5 将于 2026-12-06 起） | ✅（含 overnight） | ✅（部分数据集） | ✅（TotalView Plus 覆盖 23 小时） | ✅ |
| 逐笔成交 | ✅ | ✅ | ✅ | ✅ | ✅ |
| 报价（NBBO） | ✅ | ✅ | ✅（mbp-1） | ✅ | ✅ |
| 盘口深度 | ❌ | ❌ | ✅（mbp-10 / mbo L3） | ✅（TotalView 全深度） | ✅（Integrated Feed 全深度） |
| 停牌/恢复事件 | ⚠️ 仅 LULD 通道的 17/18，**仅 Nasdaq 上市** | ❌ 无 | ✅ `status` schema（交易所原生） | ✅ Trading Action 消息 | ✅ Security Status 消息 |
| LULD 状态/价格带 | ✅ LULD 通道（SIP 派生） | ❌ | ❌（status 不等于 LULD 带） | ⚠️ 通过 SIP/衍生 | ⚠️ 通过 SIP/衍生 |
| MWCB 状态 | ❌ | ❌ | ❌（`status` 只覆盖单交易所） | ⚠️ 系统事件/公告 | ⚠️ 系统事件/公告 |
| 交易所字段 | ✅ `z` 磁带 | ✅ `x`/`bx`/`ax` | ✅ `publisher_id` | ✅ | ✅ |
| 序列号 | ✅ `q`（按标的递增、不连续） | ❌ | ✅ `sequence` | ✅ | ✅ |
| 事件 ID | ❌ | ❌ | ✅ | ✅（tracking number） | ✅ |
| WebSocket 重连 | ✅ 客户端 SDK 支持 | ✅ SDK 支持 | ✅ 客户端库支持 | 需自行实现 | 需自行实现 |
| 历史补数据 | ✅ | ✅ | ✅ | ✅ | ✅ |
| 消息签名 | ❌ | ❌ | ❌ | ❌ | ❌ |
| 需要交易所授权 | ✅ | ⚠️（含在订阅内，再分发受限） | ✅ | ✅ | ✅ |
| 自助价格 | $0 / $29 / $79 / **$199**（实时） | $0（IEX）/ **$99**（SIP） | 按量/$199 起 | 需询价 | 需询价 |

### 3.2 关键证据（逐条）

**Massive（Polygon.io）**
* 2025-10-30 品牌更名为 Massive，API 与 Key 不变（官方 blog `Polygon.io is Now Massive`）。
* Stocks Advanced **$199/月** 才含实时；Starter/Developer 为 15 分钟延迟。
* LULD 通道官方文档 `/docs/websocket/stocks/luld`：
  * 字段 `ev / T / h / l / i / z / t / q`；
  * **"Halt and resumption messages (indicators 17 and 18) are only available for NASDAQ listed securities."** —— 这句话是"不能当作全市场权威停复牌"的直接依据；
  * `q` 为"increasing and unique per ticker symbol, but will not always be sequential" → 只能判重复/回退，不能假设连续；
  * 时间字段：文档标 "Unix MS"，官方 blog 示例为纳秒 → **同一字段两种说法，必须由配置显式指定单位**；
  * 需要 Stocks Advanced 或 LULD Expansion 附加包。

**Alpaca**
* 官方 `docs/market-data` 套餐表：Basic 免费但**仅 IEX**、WebSocket 30 个标的、200 req/min；
  Algo Trader Plus **$99/月** 覆盖"All US Stock Exchanges"，数据来自 CTA + UTP（即 SIP），10,000 req/min、不限标的。
* 文档未提供 halt / LULD / MWCB 事件通道 → 本次适配器**不声明**任何官方事件能力。
* 第三方资料称"paper-only 账户即使付费也只给 IEX"—— **UNVERIFIED**（未见官方原文），已列入风险。

**Databento**
* schema 列表含 `status`："Trading Status — Trading halts, reopenings, etc."；另有 `mbo`（L3）、`mbp-10`（L2）、`mbp-1`、`trades`、`imbalance`。
* 数据来自交易所**原始馈送**（XNAS.ITCH 等），非 SIP 转售；官方博客明确 SIP 才计算 LULD 价格带与 MWCB。
* 官方博客/LinkedIn：提供**纳秒级、可回溯的 status 消息历史**（这是它相对其他供应商的关键差异）。
* 无消息签名。

**Nasdaq 官方**
* TotalView / TotalView Plus 产品页与 Data Link 规格：
  * "Trading action messages are used to inform market participants when a security is halted/paused or released for trading"（**官方 Trading Action**）；
  * "Event controls, such as start of day, end of day and emergency market halt/resume"；
  * TotalView Plus 覆盖 23 小时交易日的盘前/常规/盘后/隔夜时段；
  * NOII 提供开收盘/IPO/Halt Cross 的失衡与指示价。

**NYSE 官方**
* Pillar Integrated Feed Client Specification（v2.5c）：
  * "The Integrated Feed also includes security status updates (e.g., trade corrections and **trading halts**) and stock summary messages"；
  * 控制消息含 `Security Status Message`（msg type 34）与 `Sequence Number Reset`；
  * 覆盖 NYSE / NYSE Arca / NYSE American / NYSE National / NYSE Texas；
  * 历史 TAQ Integrated Feed 自 2015-11-16 起。

### 3.3 A/B/C/D 四类数据的可得性

| 类别 | 谁提供 | 是否可自助获取 | 是否带签名 |
|---|---|---|---|
| **A 普通行情** | Massive / Alpaca / Databento | ✅ | ❌ |
| **B 交易所官方停复牌** | Nasdaq TotalView、NYSE Pillar、Databento `status` | ⚠️ 前两者需交易所授权；Databento 可自助但需相应 license | ❌ |
| **C LULD 官方状态** | SIP 派生（Massive LULD 通道） | ✅（$199 档） | ❌ |
| **D MWCB 官方状态** | 交易所公告 / SIP 系统事件 | ❌ 无自助 API | ❌ |

**结论**：A 类可自助；B 类部分可自助（Databento）；C 类只有供应商派生；**D 类没有自助来源**。
四类**都没有密码学签名**。

---

## 4. 实际实现的调用链

```
Provider Adapter (massive / alpaca / databento / nasdaq_itch / recorded)
   │  parse()：官方字段 → MarketEvent
   ▼
MarketDataProvider.poll()
   │  序号守卫（按 symbol+exchange 隔离）
   │  时间归一（UTC + 原始字段保留）
   │  质量标记（重复/乱序/缺口/过期/未来/naive）
   ▼
ProviderFeed._ingest()
   │  旧数据不得覆盖新数据
   │  只有 AuthorityRouter 通过的事件才能改 halt/luld 状态
   ▼
QuoteGateway(provider=feed)
   │  时段校验（regular_only 拒绝盘前/盘后）
   │  新鲜度 / 跨时段冒用 / 跳变
   ▼
StrategyEngine → RiskController → PaperBroker（阶段 A/B/C 门禁不变）
```

**接入方式**：`QuoteGateway` 新增可选 `provider` 参数；为 `None` 时行为与之前**完全一致**。
行情来源换了，但准入校验一条都没少。

---

## 5. 数据字段定义

`MarketEvent` 公共字段（`market/providers/models.py`）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `provider` | str | 供应商标识 |
| `symbol` | str | 归一化代码（`usAAPL` → `AAPL`） |
| `exchange` | str | 交易所（小写）；未知为 `unknown`，**不猜** |
| `event_time` | datetime | **UTC**，市场发生时刻 |
| `receive_time` | datetime | **UTC**，本地收到时刻（延迟统计用） |
| `sequence` | int | 供应商序号（可能不连续） |
| `source_event_id` | str | 供应商事件 ID（有则记录） |
| `authority` | enum | A/B/C/D 分类 |
| `quality` | DataQuality | 质量标记 + 延迟毫秒 |
| `raw_time` / `raw_type` | str | 原始时间字段与原始消息类型（不改写） |

子类型：`QuoteEvent`(bid/ask/sizes)、`TradeEvent`(price/size/conditions)、
`OrderBookEvent`(bids/asks)、`SessionEvent`(phase)、`HaltEvent`(halted/reason/code)、
`LULDState`(upper/lower/indicator/paused/resumed)、`MWCBState`(level/triggered/closed_for_day)。

---

## 6. 权威事件认证机制

见 `US_MARKET_DATA_QUALITY_SECURITY_REPORT.md` §3。要点：

1. 供应商 `halt: true` **永远是 A 类**，不得驱动状态；
2. B/C/D 类要求 provider 注册为 `native=True` 且覆盖该交易所；
3. `require_signature_for_resume=True`（默认）：未签名事件一律拒绝恢复；
4. 事件版本必须单调（防重放）、时间不得来自未来/过旧、symbol/exchange 必须匹配；
5. Nasdaq 上市的停复牌不得用于 NYSE 标的（`nasdaq_listed_only`）。

---

## 7. 未解决问题 / BLOCKED

| 级别 | 项 | 说明 |
|---|---|---|
| **BLOCKED** | 无 API Key / 未采购套餐 | 未连接任何真实端点；适配器只用官方文档形状 + 录制样本验证 |
| **BLOCKED** | 无交易所直连授权 | Nasdaq TotalView / NYSE Pillar 需签约，无法自助 |
| **BLOCKED** | 无消息签名体系 | 所有供应商均不提供 → 权威恢复不可达 |
| **BLOCKED** | 无 MWCB 自助数据源 | 只能靠交易所公告，无 API |
| **UNVERIFIED** | Alpaca paper 账户是否只给 IEX | 第三方说法，未见官方原文 |
| **UNVERIFIED** | Massive 时间单位（ms vs ns） | 同一文档两种写法，需用真实报文核对 |
| **UNVERIFIED** | Databento `status` 是否覆盖全部 halt 类型 | schema 存在，但字段级语义需实测样本 |
| **UNVERIFIED** | 再分发/商业使用许可 | 各家条款不同，接入前需法务确认 |

---

## 8. 是否具备接入真实行情的条件

**工程条件：具备（PARTIAL）。**
接入层已就绪：模型、适配器、质量门禁、权威路由、离线夹具、配置模板全部可用，
换真实传输只需实现 `_connect/_poll_raw`，解析与门禁不变。

**运营条件：不具备。**
缺 Key、缺套餐、缺交易所授权；且**权威恢复能力仍然 BLOCKED** —— 这不是靠写代码能解决的，
需要交易所/供应商提供可验证的签名机制。

---

## 9. 提交记录

| commit | 内容 |
|---|---|
| `559f8e3`（基线） | 上一阶段稳定性报告 |
| 见 `git log` 本轮提交 | Provider 接入层 + 测试 + 四份报告 |

---

## 10. 下一阶段唯一推荐行动

> **只做一件事：用 Databento 的免费额度（$125）拉取一段真实 `status` + `trades` 样本，
> 用本轮实现的 `DatabentoProvider` 解析，核对三件事：
> (1) `status` 的 action 语义是否与实现一致；
> (2) `ts_event` 纳秒精度与交易所时区；
> (3) 停牌期间是否仍有成交/报价。
> 在此之前不要接任何实时订阅，也不要把任何供应商事件当作官方恢复依据。**

理由：Databento 是唯一"可自助 + 交易所原生 status + 有免费额度"的组合，
能用最低成本验证接入层最关键的两个假设（字段语义、时间精度）。
其余供应商要么没有官方事件（Alpaca）、要么只有 Nasdaq 上市（Massive 17/18）。
