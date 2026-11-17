# 数据供应商采购前决策审计

- 项目：`C:\FosunAI-Trading`
- 基线 commit：`bc56555`（工作树干净）
- 审计日期：2026-11-17
- 调查方式：**只读、零网络**。未运行 `--estimate-online`，未运行 `--fetch`，
  未读取 API Key，未发起 DNS/HTTP/WebSocket，未登录 Portal，未购买任何服务。
- 价格证据日期：**2026-11-16**（上一轮官方页面查询所得，已固化在
  `US_REAL_DATA_SOURCE_READINESS_REPORT.md` §3；本轮**未联网刷新**）

---

## 0. 结论与推荐

**结论：本轮不采购。唯一推荐动作是"人工去 Portal 看三个数"，而不是花钱。**

三条决定性事实：

1. **数据量根本不是成本瓶颈。** 三个最小验证方案的数据量分别是 200 B / 288 KB / 1.15 MB。
   即使单价高到 $100/GB，最贵的方案也只有 **$0.115**。所以"值不值得买"这个问题
   跟单价几乎无关，只跟**最低计费门槛和授权要求**有关——而这两项只能由人工在
   Portal 看到。
2. **买数据解决不了最硬的那个缺口。** D 项（交易所权威恢复事件的密码学认证）
   是结构性的：Massive / Alpaca / Databento / Nasdaq / NYSE **都没有消息签名**。
   花多少钱都买不到"能验签的恢复事件"。
3. **最需要被验证的东西，恰好最便宜。** 当前 A 项 BLOCKED 的唯一原因是
   "解析器没在真实 payload 上跑过"，而一个 5 分钟窗口的 `status` 样本是 200 字节。

| 供应商 | 判定 |
|---|---|
| **Databento** | **暂缓**（条件性：Portal 确认无授权费/有可用额度后，只做一次性样本，不订阅） |
| **Massive** | **不推荐**（实时需 $199/月；LULD 指标仅 Nasdaq 上市；对当前缺口帮助最小、成本最高） |
| **Alpaca** | **暂缓**（免费档可零成本验证 trades 路径，但**没有**任何停复牌事件，无法替代 status 样本） |
| 最低成本真实样本路径 | **Databento 一次性 5 分钟 `status`+`trades`（≈288 KB）**；若需最低充值则改为 Alpaca 免费 IEX 验证 trades + 继续离线验证 status |
| 是否批准进入在线估价阶段 | **本轮不批准**；批准条件见 §6.3 |

---

## 1. 取证边界与证据来源

### 1.1 本轮做了什么

| 动作 | 是否执行 |
|---|---|
| 读本地报告、配置、源码、官方客户端包 | ✅ |
| 运行 `--estimate-online` / `--fetch` | ❌ |
| 读取 `DATABENTO_API_KEY` | ❌ |
| DNS / HTTP / WebSocket | ❌ |
| 登录 Portal / 购买 / 充值 | ❌ |
| 修改代码、配置、开关 | ❌ |
| 删除或覆盖文件 | ❌ |

### 1.2 本地证据清单

| 证据 | 位置 | 性质 |
|---|---|---|
| 供应商能力对比（2026-11-16 官方页面） | `US_REAL_DATA_SOURCE_READINESS_REPORT.md` §3 | 文档级，非账户级 |
| 官方 schema 字段表与枚举 | `DATABENTO_STATUS_SEMANTICS_REPORT.md` | 文档 + 官方客户端字节编码 |
| 各家能力声明（本项目代码） | `src/ai_trader/market/providers/adapters.py` | **我方声明**，非厂商证明 |
| 供应商配置（数据集、套餐名） | `config/config.yaml → market_data_providers` | 我方配置 |
| **官方数据集清单（53 个）** | `databento` 0.90.0 `common/publishers.py::Dataset` | 官方客户端，本地包 |
| **计费口径** | 同上 `historical/api/metadata.py` | 官方客户端 docstring |
| **DBN 单记录字节数** | `databento-dbn` 0.73.0 实测 `bytes(msg)` | 官方客户端，实测 |

### 1.3 一个重要纠正：`$125 免费额度`没有出处

`US_REAL_DATA_SOURCE_READINESS_REPORT.md` §10 写着"用 Databento 的免费额度（$125）"，
但该数字**在本地任何文件里都没有引用来源**，而后续两份报告（
`DATABENTO_REAL_HISTORICAL_SAMPLE_REPORT.md` §2.1、
`DATABENTO_ZERO_COST_PROBE_AUDIT.md` §10）都已把"免费额度是否真实存在"标为
**UNVERIFIED**。

**本报告的处理**：`$125` 视为**无出处的传闻**，不作为任何决策依据。
所有涉及额度的条目一律 `BLOCKED`（需登录 Portal 才能核实）。

---

## 2. Databento 采购条件逐项核查

> 判定口径：`VERIFIED` = 有可指认的官方依据；`UNVERIFIED` = 文档层面无法确认；
> `BLOCKED` = 必须登录账户/看合同才能知道，本轮禁止。

### 2.1 逐项结论

| # | 问题 | 判定 | 依据 / 缺什么 |
|---|---|---|---|
| 1 | `XNAS.ITCH` 是否需要单独购买授权 | **PARTIAL**：数据集独立性 **VERIFIED**；是否单独计费 **UNVERIFIED** | 官方客户端 `Dataset` 枚举里 `XNAS.ITCH` 是与 `EQUS.*`、`DBEQ.BASIC` 并列的**独立数据集**（53 个之一），说明它是单独的产品线。但"买数据集是否还要另付 Nasdaq 授权"属账户条款，本地无原文。我方代码里 `requires_exchange_license=True` 是**我方声明**，不能反过来当证据 |
| 2 | AAPL 是否属于该数据集可查询证券 | **UNVERIFIED** | `XNAS.ITCH` = Nasdaq TotalView-ITCH，覆盖 Nasdaq 上市证券；AAPL 在 Nasdaq 上市，**逻辑上应当在范围内**。但本地没有官方 symbol 清单，也没有 `get_dataset_condition` 的实际返回 |
| 3 | `status` 和 `trades` 是否都在拟用权限内 | **UNVERIFIED** | 两者都是官方支持的 schema（`get_billable_size` 的 schema 参数列表里明确含 `status`），但**某个数据集是否开放某 schema** 要查 `get_dataset_condition`，需联网 |
| 4 | 历史数据费用如何计算 | **机制 VERIFIED / 数值 UNKNOWN** | 官方客户端 docstring：`list_unit_prices` 返回"**USD per gigabyte**，按 feed mode × schema"；`get_billable_size` 返回"**billable uncompressed raw binary size**"；`get_cost` 由两者推出。**单价数值本地没有**，标 UNKNOWN |
| 5 | 是否有最低收费、额外交易所费、税费 | **BLOCKED** | 账户条款 / 发票口径，只能看 Portal 与合同 |
| 6 | 免费额度是否适用于目标数据集 | **BLOCKED** | 未登录；且额度存在性本身 UNVERIFIED |
| 7 | 免费额度是否覆盖历史下载（而非仅赠金） | **BLOCKED** | 同上 |
| 8 | 账户额度是否过期 | **BLOCKED** | 同上 |
| 9 | 是否允许个人算法研究与 Paper Trading | **UNVERIFIED** | 需服务条款原文；本地没有任何厂商 ToS 副本 |
| 10 | 是否允许商业用途 / 是否限制再分发 | **UNVERIFIED**（且**很可能受限**） | 交易所数据通常禁止再分发。本地无原文，不得推断为"允许" |

### 2.2 必须写清楚的两条边界

**（a）"文档说有" ≠ "你的账户能用"。**
上表 1–3 项即便全部 VERIFIED，也只证明"产品存在、schema 存在"，
**不证明你的账户被授权**。账户级授权只有 Portal 能回答。

**（b）`status` 是交易所原生状态，但不是密码学认证的事件。**
Databento 传输层只有 TLS，没有消息签名。所以无论买不买，
"未签名事件不得恢复交易"这条门禁都必须保留（当前实现已是如此，
`AuthorityRouter` 的不对称策略）。

### 2.3 一个容易被忽略的替代产品

官方数据集清单里有 `EQUS.SIP`、`EQUS.MINI`、`EQUS.SUMMARY`、`EQUS.ALL`、
`DBEQ.BASIC` 等**合并类**数据集，与单交易所的 `XNAS.ITCH` 并列。

这意味着"必须买 Nasdaq 授权"未必成立——**可能存在不需要单交易所授权的合并产品**。
但：

- 合并产品是否包含 `status` schema → **UNVERIFIED**
- 合并产品的停牌事件是否仍带 `publisher_id`（能否区分交易所）→ **UNVERIFIED**
- 合并产品单价 → **UNKNOWN**

**这一条值得在 Portal 里一并确认**：如果 `EQUS.*` 也提供 `status` 且不需要
单交易所授权，那它才是正确选择，`XNAS.ITCH` 反而不是。

---

## 3. 三家对比（面向当前系统，不是面向 API 好不好用）

> 能力列来源：`US_REAL_DATA_SOURCE_READINESS_REPORT.md` §3（2026-11-16 官方页面）。
> 标 `*` 的是我方代码声明，非厂商证明。

| 维度 | Massive | Alpaca | Databento |
|---|---|---|---|
| 美股覆盖 | 全市场（32,000+ 标的） | 全市场（付费档）/ 仅 IEX（免费档） | 全市场（按数据集） |
| SIP 合并 | ✅ | ✅（Algo Trader Plus） | ❌（交易所直连，非 SIP） |
| 实时 | ✅（Advanced） | ✅（付费档） | ✅ |
| 历史 | 20+ 年 | 2016 起 | 5 年（美股） |
| Trades | ✅ | ✅ | ✅ |
| Quotes | ✅ | ✅ | ✅（mbp-1） |
| **Status / 停复牌** | ⚠️ 仅 LULD 通道 17/18，**仅 Nasdaq 上市** | **❌ 完全没有** | ✅ `status`（交易所原生） |
| LULD 价格带 | ✅（SIP 派生，$199 档） | ❌ | ❌（`status` 只有 PAUSE+reason，无上下限） |
| MWCB | ❌ | ❌ | ❌ |
| 盘前/盘后/隔夜 | ✅（含隔夜） | ✅ | ✅（部分数据集） |
| 交易所可追溯 | ✅ `z` 磁带 | ✅ `x`/`bx`/`ax` | ✅ `publisher_id`（72 项映射） |
| 序列号 | ✅ `q`（不保证连续） | ❌ | ✅ 仅 trades/mbp |
| **消息签名** | ❌ | ❌ | ❌ |
| 自助价格 | $0 / $29 / $79 / **$199** | $0（IEX）/ **$99**（SIP） | 按量计费（USD/GB），单价 **UNKNOWN** |
| 历史样本验证成本 | 需 $199 档才有 LULD；基础档 15 分钟延迟 | **$0**（IEX 免费档，但无 status） | 按量，体积极小 → 预计 < $0.12（见 §4） |

### 3.1 面向本项目的能力缺口映射

| 当前缺口 | Massive 能补 | Alpaca 能补 | Databento 能补 |
|---|---|---|---|
| A 项：解析器未经真实 payload 验证 | 部分（trades/quotes） | **部分（trades/quotes，$0）** | **✅ 含 status** |
| B 项：status 语义真实流量 | ❌ | ❌ | ✅ |
| C 项：停牌期成交门禁 | 已 PASS，无需补 | — | — |
| D 项：恢复事件密码学认证 | ❌ | ❌ | ❌ |
| LULD 价格带 | ✅（$199，仅 Nasdaq 上市） | ❌ | ❌ |
| MWCB | ❌ | ❌ | ❌ |

**结论**：只有 Databento 能补 B 项（真实 `status`）。Massive 的唯一独占能力是
LULD 价格带，但价格带**当前不阻塞任何已实现功能**（LULD 生命周期已用合成数据
验证完毕，缺的是真实带宽数值，不是规则）。

---

## 4. 三个最小验证方案的成本测算

### 4.1 测算依据（可复现）

| 参数 | 值 | 来源 |
|---|---|---|
| `status` 单记录 | **40 B** | `databento-dbn` 0.73.0 实测 `bytes(StatusMsg)` |
| `trades` 单记录 | **48 B** | 同上 `bytes(TradeMsg)` |
| 计费口径 | **未压缩原始二进制字节**，USD/GB | 官方客户端 `get_billable_size` docstring |
| 单价 | **UNKNOWN** | 本地无 `list_unit_prices` 结果 |

### 4.2 记录数假设（工程估计，非官方数据）

- AAPL 常规时段约 15–30 笔/秒（由日均成交笔数量级反推）。
- 5 分钟 ≈ 6,000 笔 → 288,000 B。
- `status` 在 5 分钟窗口内通常 0–5 条（开盘/收盘/SSR 变更才发）。
- 交易所 UTC 午夜会补一条快照——本方案窗口不跨 UTC 午夜，不计。

> 这些是**估算**，误差可能 ±50%。但因为总量在 MB 级以下，误差不影响结论。

### 4.3 三方案对照

| | **A. 只取 5 分钟 status** | **B. 5 分钟 status + trades** | **C. 停牌前中后完整窗口（约 20 分钟）** |
|---|---|---|---|
| 需要的权限 | `XNAS.ITCH`（或含 status 的合并数据集）+ `status` schema | 同上 + `trades` | 同上 |
| 交易所授权 | 若走 `XNAS.ITCH` 则可能需要 Nasdaq 授权（UNVERIFIED）；合并数据集可能不需要（UNVERIFIED） | 同 A | 同 A |
| 数据量 | **200 B** | **288,200 B ≈ 0.29 MB** | **1,152,200 B ≈ 1.15 MB** |
| 预估费用（$1/GB） | $0.0000002 | $0.000288 | $0.001152 |
| 预估费用（$10/GB） | $0.000002 | $0.0029 | $0.0115 |
| 预估费用（$100/GB） | $0.00002 | $0.0288 | $0.1152 |
| **官方价格依据** | **UNKNOWN**（单价未知） | **UNKNOWN** | **UNKNOWN** |
| 能验证的工程能力 | `status` 字段名/类型/枚举/时间单位/symbol 缺失/symbology；`action→halted` 映射；`ts_recv` 与 `ts_event` 差异 | 上述 + trades 价格标度（1e-9 vs float）、`side`、`sequence`；网关→账本全链路 | 上述 + 停牌期是否真有成交、恢复事件的时间顺序、暂停时长分布 |
| **不能验证的安全能力** | 签名/权威认证（D 项）；LULD 带宽；MWCB；实时链路可靠性 | 同 A | 同 A |

### 4.4 这张表真正的含义

**A 方案只要 200 字节。** 如果 Portal 显示"按量计费、无最低消费"，那么这次验证
的成本实质上是 **$0.00002**——比一次 API 调用的运维注意力还便宜。
真正决定"能不能做"的不是钱，是**授权**与**最低充值门槛**。

---

## 5. 是否值得采购：六个问题

### 5.1 真实历史样本能解决哪些剩余问题？

| 问题 | 能否解决 | 说明 |
|---|---|---|
| A 项：解析器未经真实 payload | **✅ 能** | 这是唯一必须靠真实数据才能关闭的项 |
| `status` 实际投递频率、是否带快照 | ✅ 能 | 影响缺口检测阈值 |
| 停牌期间是否真有成交 | ✅ 能 | 决定 `trades-during-halt` 的处理权重 |
| 真实 `ts_recv - ts_event` 延迟量级 | ✅ 能 | 影响新鲜度阈值设定 |
| 供应商实际 JSON 枚举表示（int 还是 char） | ✅ 能 | 当前两种都兼容，实测可确认 |

### 5.2 哪些问题即使购买数据也解决不了？

| 问题 | 为什么解决不了 |
|---|---|
| **D 项：恢复事件的密码学认证** | 没有任何供应商提供消息签名。这是行业现状，不是钱的问题 |
| LULD 价格带（若买 Databento） | `status` 只有 `PAUSE + reason=LULD_PAUSE`，**没有上下限**。要带宽只能买 Massive $199 档或交易所直连 |
| MWCB 全市场状态 | 无自助 API |
| 实时链路可靠性（重连、回补、乱序） | 需要长期订阅 + 持续运行，一次样本证明不了 |
| 券商连接 / 真实下单 | 与数据源无关 |
| 交易所授权的**合同**约束（再分发、商业用途） | 只能读合同 |

### 5.3 是否值得先只买一次历史数据？

**值得，但必须先满足前置条件。** 理由：

- 成本量级 $0.0001–$0.12（取决于单价假设），相对它能关闭的 A 项而言极低；
- 它不产生任何持续义务（不是订阅）；
- 它是唯一能验证"我的解析器与真实世界一致"的手段。

**前置条件**（任一不满足就不做）：

1. Portal 显示该数据集**已授权**，或授权费为 0；
2. 无最低充值门槛，或门槛可接受；
3. 账户有可用额度（自有赠金或已充值余额）。

### 5.4 是否值得购买实时数据订阅？

**不值得，现在不。**

- 当前 `paper_runtime.enabled=false`、`market_data_providers.enabled=false`，
  没有任何进程会消费实时流；
- 实时链路的可靠性（重连、回补、乱序）需要长期运行才能暴露，
  而当前系统还没有进入"长期接实时"的阶段；
- 最便宜的实时方案（Alpaca $99 / Massive $199 / Databento 实时）都是**月费**，
  而收益是"提前验证一个暂时用不上的能力"。

### 5.5 是否应继续保持完全离线 Paper/Replay？

**应该，直到 §5.3 的三个前置条件被确认。**

离线状态下已经能跑：10 个 Replay 场景、120 分钟持续运行、账号不变量、
崩溃恢复、100 次启停。这些能力不依赖任何供应商。

### 5.6 有没有更低成本的替代验证方式？

有，两条，且都值得先做：

| 方式 | 成本 | 能验证 | 不能验证 |
|---|---|---|---|
| **Alpaca 免费 IEX 档**（需注册 + Key） | **$0** | trades/quotes 的真实 payload 形状、时间戳、网关→账本全链路 | **没有任何 status 事件**；单交易所（IEX），不代表 Nasdaq |
| **官方规范文档里的样例报文** | $0 | 字段语义、字节布局（上一轮已用 DBN 编码实测完成） | 真实投递顺序、真实抖动 |

Alpaca 免费档的价值在于：它能用**零成本**证明"我的 Provider → 网关 → 账本"
链路在真实数据上不崩。这恰好是 A 项的一半。它证明不了的是 `status`——
而那正是 Databento 唯一不可替代的地方。

---

## 6. 最终决策

### 6.1 三家判定

| 供应商 | 判定 | 理由 |
|---|---|---|
| **Databento** | **暂缓** | 唯一能补 `status` 真实样本的供应商，成本极低（< $0.12）；但账户授权、最低门槛、额度三项全 BLOCKED，必须人工确认后才能动。**不做订阅，只做一次性样本** |
| **Massive** | **不推荐** | 实时需 $199/月（是 Databento 样本成本的 1,700 倍以上）；`status` 能力仅覆盖 Nasdaq 上市证券且非原生；唯一独占的 LULD 价格带当前不阻塞任何功能 |
| **Alpaca** | **暂缓** | 免费档可零成本验证 trades 路径，是"最低成本备选"；但完全没有停复牌事件，无法替代 Databento 的样本价值。**不买 $99 档**（买了也只是 SIP 行情，仍无 status） |

### 6.2 最低成本真实样本验证路径

```
第 0 步（人工，5 分钟，零风险）
  登录 Databento Portal，只看四个数：
    ① 账户是否有可用额度？多少？
    ② 额度是否过期？
    ③ XNAS.ITCH 是否需要单独授权/授权费？
    ④ 是否存在最低充值门槛？
  → 任一项不利，直接跳到第 3 步（Alpaca 免费档）

第 1 步（若第 0 步全部有利）
  用 B 档查价（只读元数据，不下载）：
    python tools/databento_probe.py --estimate-online \
      --dataset XNAS.ITCH --schema status --symbols AAPL \
      --start <T> --end <T+5min>
  看输出的 usd 与 within_cap

第 2 步（金额可接受）
  一次性取 5 分钟样本（方案 B，288 KB）：
    python tools/databento_probe.py --fetch --confirm \
      --max-cost-usd <上一步金额> ...
  拿到 raw JSONL 后跑 DatabentoProvider 解析，关闭 A 项

第 3 步（若 Databento 不可行）
  Alpaca 免费 IEX 档：注册 → 取 trades → 跑同一套 Provider
  关闭 A 项的一半（trades 路径），status 继续依赖官方文档 + 合成样本
```

**注意**：第 1、2 步都会真实联网并发送 Key，必须由你明确点头。
第 0 步不涉及任何自动化，只是看一眼 Portal。

### 6.3 是否批准进入在线估价阶段

**本轮不批准。** 批准条件（三条同时满足）：

1. 你已完成第 0 步，并告知"账户有可用额度"且"无最低充值门槛"；
2. 你明确同意工具用 Key 发起一次**只读元数据**请求（不下载行情）；
3. 仍保持 `--max-cost-usd 0`（估价不产生费用，但闸门不放开）。

理由：在不知道"有没有授权"之前去查价，最可能的结果是拿到一个 403，
白跑一次却仍不知道该怎么办。**先看 Portal，再查价**，顺序不能反。

---

## 7. 未解决 / 未知项汇总

| 项 | 状态 | 谁能解决 |
|---|---|---|
| Databento 账户额度 | **BLOCKED** | 人工看 Portal |
| 额度是否过期 | **BLOCKED** | 同上 |
| `XNAS.ITCH` 授权费 | **BLOCKED** | 同上 |
| 最低充值门槛 | **BLOCKED** | 同上 |
| 合并数据集（`EQUS.*`）是否含 `status` | **UNVERIFIED** | 查官方数据集页（需联网）或 `get_dataset_condition` |
| 各家单价数值 | **UNKNOWN** | `list_unit_prices`（需联网）或官方定价页 |
| 服务条款（个人/商业/再分发） | **UNVERIFIED** | 读 ToS 原文（需联网） |
| 消息签名 | **BLOCKED（行业性）** | 无解，除非交易所直连 mTLS + 自定义签名 |
| `$125 免费额度` | **无出处** | 已作废，不得引用 |

---

## 8. 本轮未做的事（明确声明）

- 未运行 `--estimate-online`、未运行 `--fetch`
- 未读取 API Key
- 未发起任何 DNS/HTTP/WebSocket 请求
- 未登录 Portal、未购买、未充值、未订阅
- 未修改任何代码、配置、开关
- 未删除或覆盖任何文件

下一轮如需联网查官方定价页或数据集页，会先报告"需要联网"并等待授权。
