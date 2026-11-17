# Databento `status` 事件语义报告

- 项目：`C:\FosunAI-Trading`
- 基线 `6e0c4ef` → 本轮 `143ff1c`
- 证据：`verification/databento/out/verification.json`

---

## 0. 一句话结论

`status` 是**交易所原生**的交易状态消息，字段语义可完整对齐官方文档；
但它**没有消息签名**，所以它是"可信来源"，不是"可信证明"。

这条区分决定了系统怎么用它：

| 用途 | 是否允许 | 原因 |
|---|---|---|
| 发现"这只票停了"并停止交易 | ✅ 允许 | 停牌是收敛风险，宁可信其有 |
| 发现"这只票恢复交易了"并放开交易 | ❌ 不允许 | 恢复是放大风险，必须有可验证的权威身份 |
| 替代 LULD 价格带 | ❌ 不允许 | `status` 只有 `PAUSE + reason=LULD_PAUSE`，**没有上下限** |
| 替代 MWCB 全市场状态 | ❌ 不允许 | 只有单标的的 `reason=120..124`，没有全市场级别对象 |

---

## 1. 字段映射总表

左列是解析后的事件字段，右列是官方原始字段。

### 1.1 `status`（rtype = 18）

| 目标字段 | 原始字段 | 转换 | 说明 |
|---|---|---|---|
| `event_time` | `ts_event` | uint64 ns → UTC datetime | 撮合引擎时间 |
| `receive_time` | `ts_recv` | 同上 | 抓包服务器时间；缺失时退回 `ts_event` 并计数 |
| `symbol` | `symbol` **或** `instrument_id` | symbology 映射 | 原始记录**默认没有** `symbol` |
| `exchange` | `publisher_id` | 72 项整数映射 | 未知 → `unknown`，不猜 |
| `sequence` | — | 固定 0 | **官方无此字段** |
| `source_event_id` | — | 固定 `""` | **官方无事件 ID 字段** |
| `halt_code` | `action` | uint16 → 代码名 | 8→HALT，9→PAUSE，10→SUSPEND |
| `reason` | `reason` | uint16 → 代码名 | 取值不连续，见 §2.2 |
| `trading_event` | `trading_event` | uint16 → 代码名 | 0–4 |
| `is_trading` | `is_trading` | `Y`→True，`N`→False，`~`→None | `~` **不等于** False |
| `is_quoting` | `is_quoting` | 同上 | |
| `ssr_restricted` | `is_short_sell_restricted` | 同上 | |
| `halted` | 由 `action` 推导 | 见 §3 | |
| `authority` | — | 固定 `EXCHANGE_HALT` | 表示来源性质，**不是**认证结果 |

### 1.2 `trades`（rtype = 0）

| 目标字段 | 原始字段 | 转换 |
|---|---|---|
| `price` | `price` | int64 ÷ 1e9；float 原样 |
| `size` | `size` | uint32 → float |
| `side` | `side` | `A`/`B`/`N` 原样保留 |
| `sequence` | `sequence` | uint32，**存在** |
| `price_scale` | — | `fixed_1e-9` 或 `float`，用于自证没有二次缩放 |

### 1.3 `mbp-1`（rtype = 1）

`levels[0].bid_px` / `ask_px` / `bid_sz` / `ask_sz`（÷1e9），
`sequence` 存在。顶层没有 `bid_px`。

---

## 2. 枚举全表

### 2.1 `action`（uint16，16 项）

| 值 | 名称 | 是否停牌 | 处理 |
|---|---|---|---|
| 0 | NONE | 否 | 忽略 |
| 1 | PRE_OPEN | 否 | 忽略（时段由交易所时钟负责） |
| 2 | PRE_CROSS | 否 | 忽略 |
| 3 | QUOTING | 否 | 忽略（"在报价但不在交易"） |
| 4 | CROSS | 否 | 忽略 |
| 5 | ROTATION | 否 | 忽略 |
| 6 | NEW_PRICE_INDICATION | 否 | 忽略 |
| 7 | **TRADING** | 否（恢复） | `halted=False`，**需签名才生效** |
| 8 | **HALT** | 是 | `halted=True` |
| 9 | **PAUSE** | 是 | `halted=True`（LULD 暂停走这里） |
| 10 | **SUSPEND** | 是 | `halted=True` |
| 11 | PRE_CLOSE | 否 | 忽略 |
| 12 | CLOSE | 否 | 忽略（**收盘不是停牌**） |
| 13 | POST_CLOSE | 否 | 忽略 |
| 14 | SSR_CHANGE | 否 | 忽略（卖空已禁用） |
| 15 | **NOT_AVAILABLE_FOR_TRADING** | 模糊 | `halted=True` + `AMBIGUOUS_STATUS` |

**为什么 12 CLOSE 不算停牌**：把它算停牌会造出一条永不恢复的记录，
下一个交易日开盘时标的仍然"停牌"，只能靠人工清理。收盘由交易日历负责。

**为什么 15 算停牌**：官方定义是 "either trading has closed or been halted"。
无法区分时选择**不可交易**（保守）；同时打 `AMBIGUOUS_STATUS` 标记供审计。

### 2.2 `reason`（uint16，34 项，**取值不连续**）

| 值 | 名称 | 值 | 名称 |
|---|---|---|---|
| 0 | NONE | 60 | OPERATIONAL |
| 1 | SCHEDULED | 70 | ADDITIONAL_INFORMATION_REQUESTED |
| 2 | SURVEILLANCE_INTERVENTION | 80 | MERGER_EFFECTIVE |
| 3 | MARKET_EVENT | 90 | ETF |
| 4 | INSTRUMENT_ACTIVATION | 100 | CORPORATE_ACTION |
| 5 | INSTRUMENT_EXPIRATION | 110 | NEW_SECURITY_OFFERING |
| 6 | RECOVERY_IN_PROCESS | **120** | MARKET_WIDE_HALT_LEVEL1 |
| 10 | REGULATORY | **121** | MARKET_WIDE_HALT_LEVEL2 |
| 11 | ADMINISTRATIVE | **122** | MARKET_WIDE_HALT_LEVEL3 |
| 12 | NON_COMPLIANCE | **123** | MARKET_WIDE_HALT_CARRYOVER |
| 13 | FILINGS_NOT_CURRENT | **124** | MARKET_WIDE_HALT_RESUMPTION |
| 14 | SEC_TRADING_SUSPENSION | 130 | QUOTATION_NOT_AVAILABLE |
| 15 | NEW_ISSUE | | |
| 16 | ISSUE_AVAILABLE | | |
| 17 | ISSUES_REVIEWED | | |
| 18 | FILING_REQS_SATISFIED | | |
| 30 | NEWS_PENDING | | |
| 31 | NEWS_RELEASED | | |
| 32 | NEWS_AND_RESUMPTION_TIMES | | |
| 33 | NEWS_NOT_FORTHCOMING | | |
| 40 | ORDER_IMBALANCE | | |
| 50 | **LULD_PAUSE** | | |

7–9、19–29、34–39、41–49、51–59、61–69、71–79、81–89、91–99、101–109、111–119、125–129
**均未定义**。按连续枚举做反查会静默落到相邻项——这是本轮特意加了
`test_status_reason_has_non_contiguous_values` 盯住的地方。

### 2.3 `trading_event`（uint16，5 项）

`0 NONE`、`1 NO_CANCEL`（允许下单，不允许改单/撤单）、`2 CHANGE_TRADING_SESSION`
（交易日切换，日统计重置）、`3 IMPLIED_MATCHING_ON`、`4 IMPLIED_MATCHING_OFF`。

---

## 3. `action` → `halted` 的推导规则

```
halted = True   if action ∈ {8 HALT, 9 PAUSE, 10 SUSPEND}
halted = True   if action == 15 NOT_AVAILABLE_FOR_TRADING   (+ AMBIGUOUS_STATUS)
halted = False  if action == 7 TRADING
其它 action     → 不产生 HaltEvent（不是停牌事件）
未知 action     → 产生 halted=True 的 HaltEvent (+ UNKNOWN_ENUM)
```

最后一行是关键取舍：**未知 action 不能丢弃**。丢弃意味着沿用上一次状态；
若上次是"可交易"，那么这个看不懂的状态就变成了放行。按不可交易处理，
方向永远是对的。

---

## 4. 时间语义

| 字段 | 含义 | 用途 |
|---|---|---|
| `ts_event` | 撮合引擎收到并处理的时间 | **判断事件顺序与新鲜度的基准** |
| `ts_recv` | Databento 抓包服务器收到的时间 | 延迟统计 `ts_recv - ts_event` |
| `ts_in_delta` | `ts_event` 相对 `ts_recv` 的偏移（ns，int32，trades 才有） | 交叉校验 |

三条硬规则：

1. **新鲜度必须按 `ts_event` 算**，不能用 `ts_recv`。否则供应商把两小时前的数据
   "现在推过来"，`ts_recv` 是刚刚，会被当成最新。
2. **纳秒必须用整数运算转换**。浮点除法会把 `...123456789` 变成 `...123457µs`
   （实测差 1µs）。
3. **`datetime` 只有微秒精度**，纳秒后三位必然丢失。要保留完整 ns 必须用整数
   或专用类型。当前实现保留到微秒，并在 `raw_time` 里保存原始整数值。

---

## 5. 与"官方权威"的距离

这是本报告最重要的一节。

### 5.1 四类事件的分级

| 级别 | 含义 | Databento `status` 属于 |
|---|---|---|
| A | 普通行情 / 供应商状态字段 | — |
| B | **交易所原生**停复牌消息 | ✅ 属于 B |
| C | LULD 官方价格带与状态 | ❌ 不属于（无上下限） |
| D | MWCB 官方全市场状态 | ❌ 不属于（无全市场对象） |

### 5.2 属于 B 级 ≠ 可以恢复交易

B 级只说明"这条消息来自交易所的馈送，不是供应商推断的"。它不说明：

- 消息在传输过程中没有被篡改；
- 消息确实由该交易所发出（而不是被伪装成 `publisher_id=2`）；
- 消息不是重放的旧消息。

前两点需要**密码学签名**。Databento 的 HTTP/WebSocket 传输只有 TLS，
没有消息级签名。这意味着：

> 一个能改网络流量的人，可以把一条 `action=7 TRADING` 塞进来，
> 系统无法从消息本身分辨它是真是假。

因此本项目的策略是：**停牌可以信（收敛），恢复必须有签名（放大）**。

### 5.3 LULD 的特殊说明

`status` 里确实有 LULD 信息：
```
action = 9 (PAUSE)
reason = 50 (LULD_PAUSE)
```
但**没有价格带上下限**。而 LULD 的核心恰恰是"价格带"——没有带就无法判断
某笔成交是否越界、无法计算暂停后的重新开盘价。

所以 `DatabentoProvider.capabilities()` 明确声明：

```python
luld_events=False, luld_events_native=False,
notes="...status.action 里有 PAUSE + reason=LULD_PAUSE，
       但没有价格带上下限 —— 不能据此重建 LULD 带宽。"
```

价格带仍需 `Massive` 的 LULD 通道（且仅覆盖 Nasdaq 上市证券）或交易所直连。

### 5.4 MWCB 的特殊说明

`reason` 120–124 表示市场级熔断，但它挂在**单只证券**的 `status` 记录上。
这意味着：

- 可以知道"这只票因为 L1 熔断停了"；
- **不能**据此构造一个"全市场已熔断"的对象；
- 不同标的的 120/121/122 到达时间可能相差数秒，不能当成一个原子事件。

因此 MWCB 被标记为 `AMBIGUOUS_STATUS`（提示级，不阻断），并且
`mwcb_events=False`。

---

## 6. `publisher_id` → 交易所

官方 `Venue.from_int` 共 72 项，本实现全部收录。美股相关核心项：

| id | venue | id | venue |
|---|---|---|---|
| 2 | nasdaq | 9 | nyse |
| 3 | nasdaq_texas | 10 | nyse_national |
| 4 | nasdaq_psx | 11 | nyse_american |
| 5 | cboe_bzx | 12 | nyse_arca |
| 6 | cboe_byx | 13 | nyse_texas |
| 7 | cboe_edga | 14 | iex |
| 8 | cboe_edgx | 18 | memx |

**合并发布者**（不能判"某交易所停牌"）：`40 DBEQ`、`47 EQUS`、`59 CTA`、`60 UTP`。

未收录的 id 一律映射 `unknown`，不猜。猜错的后果很具体：把 A 交易所的停牌
作用到 B 交易所的标的上，或者在 B 交易所已停牌时仍然放行。

---

## 7. 语义验证结果

`verification/databento/out/verification.json` 里逐样本记录了实际解析结果：

| 样本 | 事件数 | 关键断言 |
|---|---|---|
| `synthetic_luld_pause` | 6 | 2 笔成交 → `PAUSE/LULD_PAUSE`(halted) → 停牌中成交 → `TRADING`(未停) → 成交 |
| `synthetic_luld_pause_nosym` | 6 | 同上，全部靠 symbology 还原标的 |
| `synthetic_news_halt` | 4 | `HALT/NEWS_PENDING` → `TRADING/NEWS_RELEASED` |
| `synthetic_mwcb_halt` | 3 | `HALT/LEVEL1`、`TRADING/RESUMPTION`，两者带 `ambiguous_status` |
| `synthetic_ambiguous_status` | 2 | `NOT_AVAILABLE_FOR_TRADING` → halted + 标记 |
| `synthetic_non_halt_status` | 0 | QUOTING/CLOSE/SSR 各 1 条，全部归入 `ignored_status`，不产生假停牌 |
| `synthetic_pretty` | 3 | ISO 时间 + 浮点价 + `symbol`，价格不再二次缩放 |

---

## 8. 判定

| 项 | 判定 | 理由 |
|---|---|---|
| **Status 事件语义** | **PARTIAL** | 字段/枚举/标度/时间单位已与官方文档 **和官方客户端字节编码** 双向对齐；`action→halted` 的每条分支都有用例。**未完成**：真实流量下的投递顺序、UTC 午夜快照行为、供应商实际 `pretty_*` 默认值 |

不给 PASS 的原因很直接：语义正确性最终要用真实消息确认，而本轮一条都没有。
不给 FAIL 的原因同样直接：所有可离线判定的部分都通过了，且是通过**两个独立来源**
（文档 + 官方库编码）交叉确认的，不是"解析不报错就算对"。

---

## 9. 未解决

| 项 | 状态 |
|---|---|
| `status` 在 UTC 午夜的快照记录内容 | UNVERIFIED（官方文档提到，未实测） |
| `encoding=json` 与 `to_json()` 的枚举表示是否一致 | UNVERIFIED（已同时兼容整数与字符两种形态） |
| 真实 `ts_recv - ts_event` 分布 | UNVERIFIED |
| 停牌期间成交的真实占比 | UNVERIFIED |
| 消息签名 | **BLOCKED（外部条件）** |
