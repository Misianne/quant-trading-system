# RISK_CONTROL.md —— 风控规格书

> 风控是本系统里**唯一**有权决定"能不能下单"的模块。
> AI 只能建议，策略引擎只能组装，券商层只能执行 —— 三者都不具备放行权。

实现文件：`src/ai_trader/risk/controller.py`（`RiskController`）

---

## 1. 设计契约（不可协商）

| # | 契约 | 落地方式 | 验证 |
|---|---|---|---|
| C1 | AI 无执行权 | 模型只能返回 `TradingSignal`，不含任何下单方法 | `TradingSignal` 无 `execute()`；`test_illegal_signal_without_stop_loss_is_blocked` |
| C2 | 唯一出口 | `broker.place_order()` 只在 `StrategyEngine._execute()` 内被调用，首行 `if not decision.approved: return None` | 代码级 grep 可验证 |
| C3 | 失败即拒绝 | `RiskController.evaluate()` 外层 `try/except`，异常转为 `approved=False` | `test_fail_closed_on_internal_error` |
| C4 | 全部检查留痕 | 每项产生 `CheckResult(name, ok, detail)`，写入 `risk_decisions.checks` | Web「风控决策」面板可见 |
| C5 | 紧急停止优先 | `kill_switch` 是第 2 项检查，且最终汇总时再校验一次 | `test_kill_switch_blocks_everything` |
| C6 | 不静默降级 | 无止损 → 拒绝，不是"自动补一个止损" | `test_buy_without_stop_loss_is_rejected` |
| C7 | Web 无后门 | 无 `/api/order`、`/api/buy` 等直通接口 | `test_no_bypass_order_endpoint_exists` |

---

## 2. 执行顺序与 26 项检查

顺序即代码顺序。**所有检查都会执行并记录**（不因前项失败而跳过），最终 `reasons` 非空即拒绝 —— 这样一次拒绝能给出完整原因，而不是"改一个再报下一个"。

| # | 检查名 | 规则 | 拒绝示例（真实输出） |
|---|---|---|---|
| 0 | `intent_valid` | symbol 非空、side ∈ {buy,sell}、qty > 0 | `交易意图非法` |
| 1 | `kill_switch` | 未触发紧急停止 | `紧急停止生效：测试紧急停止` |
| 1b | `halt_storage` | 急停哨兵可读写（P0-2） | `急停状态存储异常（...），拒绝交易以免停止态失效` |
| 2 | `trading_permission` | 模拟盘：`PAPER_TRADING=true`；真实盘：`can_send_real_order()` | `无交易权限：ORDER_EXECUTION_ENABLED=false...` |
| 3 | `market_enabled` | `markets.<mkt>.enabled` | `市场 xx 未启用` |
| 4 | `symbol_allowlist` | 在 `universe` 内 | `usZZZZ 不在允许交易的观察池内` |
| 5 | `market_hours` | 交易时段（减仓豁免） | `不在交易时段：hk 周末休市` |
| 6 | `price_available` | 参考价 > 0 | `无法取得有效参考价格` |
| 7 | `min_notional` | 名义金额 ≥ 100 | `委托金额 5.00 低于最小下单金额 100.00` |
| 8 | `max_notional` | 买单名义金额 ≤ 50,000 | `委托金额 150000.00 超过单笔上限 50000.00` |
| 9 | `no_naked_short` | 卖出量 ≤ 持仓量 | `卖出数量 100 超过持仓 0（未开通做空）` |
| 10 | `max_positions` | 新建仓时持仓标的数 < 8 | `持仓标的数已达上限 8` |
| 11 | `max_position_pct` | 建仓后该标的占比 ≤ 15% | `建仓后 hk00700 占比 150.00% 超过上限 10.00%` |
| 12 | `max_gross_exposure` | 总敞口 ≤ 60% | `总敞口 25.00% 超过上限 20.00%` |
| 13 | `max_stop_distance` | 止损距离 ≤ 12% | `止损距离 16.67% 过宽，超过 12.00%` |
| 14 | `stop_side` | 买入止损 < 入场价 | `止损价方向错误` |
| 15 | `per_trade_risk` | (入场−止损) × 股数 ≤ 权益 × 1% | `单笔风险超限：最大亏损 20000.00 > 预算 10000.00` |
| 16 | `daily_loss_limit` | 当日亏损 < 3% | `触及单日亏损上限 3.00%，今日禁止新开仓` |
| 17 | `daily_trade_count` / `daily_order_count` | < 20 笔成交 / < 40 笔委托 | `今日成交笔数已达上限 20` |
| 17b | `unresolved_order_state` | 该标的无状态未知（`unknown`）的委托（P0-1） | `hk00700 存在状态未知的委托（UNCONFIRMED-123，clientId=123），必须先完成对账才能继续交易` |
| 18 | `duplicate_order` | 冷却窗口 + 未完成委托（**数据库唯一约束，跨进程生效**） | `hk00700 在 300s 内已有 buy 委托（11:59:37），判定为重复下单` |
| 19 | `rate_limit` | 近 60s 委托 < 5 笔 | `下单频率超限（5 笔/分钟 > 5）` |
| 20 | `price_deviation` | 委托价偏离最新价 ≤ 5% | `委托价偏离 13.33%，疑似错价` |
| 21 | `cash_sufficient` | 名义金额 ≤ 可用现金 | `现金不足：需要 30000.00，可用 1000.00` |
| 22 | `margin_disabled` | 默认关闭融资 | 仅告警，不拒绝 |

> **计数说明**（修复后实测）：代码中共 **26 个检查名**。一次买入评估产生 **25 条**
> `CheckResult`（`no_naked_short` 只在卖单触发），全部 `ok=true` 才放行。
> 其中 `margin_disabled` 仅产生告警不拒绝，其余 25 个都是硬性拒绝条件。
>
> 本轮新增 3 个与安全修复直接相关的检查：`halt_storage`（P0-2）、
> `unresolved_order_state`（P0-1）、以及 `daily_loss_limit` 的"无法判定即拒绝"分支（P1-2）。

---

## 3. 阈值总表（默认值）

全部来自 `config/config.yaml` 的 `risk:` 段，改配置即可调整，无需改代码。

| 参数 | 默认 | 含义 | 调紧的后果 |
|---|---|---|---|
| `max_gross_exposure_pct` | 0.60 | 总持仓市值 / 净值 | 资金利用率下降，回撤更小 |
| `max_positions` | 8 | 同时持仓标的数 | 更集中，单票风险上升 |
| `max_position_pct` | 0.15 | 单标的最大占比 | 分散度提高 |
| `max_order_notional` | 50000 | 单笔委托上限（账户本币） | 单笔冲击成本下降 |
| `min_order_notional` | 100 | 最小下单金额 | 过滤碎单，减少手续费侵蚀 |
| `risk_per_trade_pct` | 0.01 | 单笔最大亏损 / 净值 | **最核心的参数**：直接决定仓位 |
| `max_stop_distance_pct` | 0.12 | 止损距离上限 | 拒绝"止损太远等于没止损"的信号 |
| `max_daily_loss_pct` | 0.03 | 单日亏损上限 | 触发后当日停手 |
| `max_daily_trades` | 20 | 单日成交笔数 | 抑制高频冲动 |
| `max_daily_orders` | 40 | 单日委托笔数 | 抑制频繁撤改 |
| `duplicate_window_seconds` | 300 | 同标的同方向冷却 | 防重复下单/双击 |
| `max_orders_per_minute` | 5 | 频率上限 | 防失控循环 |
| `max_price_deviation_pct` | 0.05 | 委托价偏离最新价 | 防错价单 |
| `enforce_market_hours` | true | 是否限制交易时段 | 仅影响新开仓 |
| `allow_short` | false | 是否允许做空 | 关闭时禁止裸卖空 |
| `allow_margin` | false | 是否允许融资 | 关闭时要求现金充足 |

---

## 4. 仓位计算（size_position）

风控不只"拒绝"，也负责"算多大"。四道上限取**最小值**，再按置信度收缩，最后向下取整到手数：

```
风险上限   = (权益 × risk_per_trade_pct) / (入场价 − 止损价)
占比上限   = (权益 × max_position_pct) / 入场价
现金上限   = 可用现金 / 入场价
单笔上限   = max_order_notional / 入场价
------------------------------------------------
原始数量   = min(四者) × clamp(置信度, 0, 1)
最终数量   = floor(原始数量 / 每手股数) × 每手股数
```

只要 `(入场−止损) > 0` 且价格有效，任何一档上限被触碰都会体现在 `binding` 字段里，便于事后解释"为什么只买了这么点"。

**实测示例**（`tests/test_risk_controls.py`）：

```
权益 1,000,000 / 价格 300 / 止损 290 / 单笔风险 1%
  → 风险上限 1000 股，占比上限 500 股，binding = pct_limited，最终 500 股

权益 1,000,000 / 价格 300 / 止损 250 / 单笔风险 1%
  → 风险上限 200 股，binding = risk_limited，最终 200 股

默认 max_order_notional=50000 / 价格 300
  → binding = max_notional，最终 100 股（50000/300=166.7，按 100 股整手向下取整）
```

注意第三条：**单笔金额上限经常是最紧的一档**。如果发现仓位总是偏小，先看 `binding` 是不是 `max_notional`。

---

## 5. 重复订单拦截（三道）

| 层 | 判据 | 触发场景 |
|---|---|---|
| L1 内存冷却 | 同标的 + 同方向，在 `duplicate_window_seconds`(300s) 内有下单记录 | 用户连点两次"扫描" |
| L2 未完成委托 | 券商/日志中存在该标的同方向的 `submitted/pending/partial/new` 委托 | 挂单未成交又下新单 |
| L3 近期成交 | 该标的同方向在冷却窗口内已成交 | 止损单刚成交又触发一次 |

冷却窗口是**按标的+方向**独立计算的，所以"先买后卖"不会误伤。

---

## 6. 止损 / 止盈

### 6.1 信号层强制

`TradingSignal.validate()` 对 `buy`：

- `stop_loss` 必须存在且 > 0 → 否则 `缺少 stop_loss`
- `stop_loss < entry_price` → 否则 `stop_loss 必须低于 entry_price`
- `take_profit > entry_price`（若提供）→ 否则报错

`sell` 方向对称（`take_profit < entry < stop_loss`）。

### 6.2 风控层强制

即使信号合法，风控还要求：

- 止损距离 ≤ `max_stop_distance_pct`（12%）
- `(entry − stop) × qty ≤ 权益 × risk_per_trade_pct`

**两层都过**才算通过。这是刻意的冗余：模型可以骗过格式校验（填一个看起来合法但离谱的止损），但骗不过"止损距离 + 单笔风险"这两道数量校验。

### 6.3 触发与执行

`RiskController.scan_exits(positions, stops, quotes)`：

```python
if   stop_loss  and last <= stop_loss :  →  sell 全部持仓，reason="止损触发..."
elif take_profit and last >= take_profit: →  sell 全部持仓，reason="止盈触发..."
```

生成的意图标记 `is_exit=True`，享受两项豁免：

1. **交易时段豁免** —— 允许在盘外时段执行减仓（防隔夜跳空）
2. **频次/单日笔数豁免** —— 止损不能被"今天下单次数用完了"挡住

止损注册表持久化在 `data/stops.json`，**重启后仍然保护已有持仓**。

**实测**（`ai-trader demo` 第 3 步）：

```
3_stop_loss: executed= [('exit', '止损触发：最新 297.831 <= 止损 303.78762', 'filled')]
   positions_after: []          # 持仓已清零
   stops 已清除 hk00700
```

### 6.4 已知局限

- 止损基于**最新价轮询**，不是券商的"条件单"。极端跳空时实际成交价可能远差于止损价。
- 星财富 SDK 的 `create_order` 支持 `stopLossPrice` / `profitPrice` 参数（服务端条件单），本工程**未启用**（未验证其语义与触发逻辑）。上线前应评估改用服务端条件单，把止损托管给券商，避免进程宕机导致失去保护。

---

## 7. 紧急停止（Kill Switch）

### 7.1 两条触发路径

| 路径 | 命令 | 效果 |
|---|---|---|
| CLI | `ai-trader kill --reason "..."` | `risk.halt(reason)` → `gate.state.kill_switch=True` |
| Web | POST `/api/kill` | 同上，写审计 `kill_switch_on` |

### 7.2 生效范围

| 动作 | kill switch 触发后 |
|---|---|
| 新建仓 / 加仓 | ❌ 拒绝（`kill_switch` 检查） |
| 减仓 / 止损 / 止盈 | ⚠️ `is_exit=True` 的意图 `trading_permission` 仍放行，但 `kill_switch` 检查会拒绝 |

> **这里刻意选择了更保守的口径**：紧急停止 = 全面停止，包括减仓。理由是"紧急停止"通常发生在系统行为异常时（例如价格源错乱导致误判止损），此时自动卖出同样是风险。人工平仓应由券商 App 执行。
>
> 如果你希望"紧急停止但仍然允许自动止损"，把 `_evaluate_inner` 中 `kill_switch` 检查改为 `not self.gate.state.kill_switch or intent.is_exit` 即可。**这是唯一需要改动的一行**，且必须重新跑 `tests/test_risk_controls.py`。

### 7.3 恢复

`ai-trader resume` 或 POST `/api/resume`，清空 `halt_reason` 并把 `kill_switch` 置回 `false`。

⚠️ 恢复不会自动重跑错过的扫描，也不会重置当日亏损计数（当日亏损是账户真实状态，不该被"恢复"抹掉）。

---

## 8. 单日亏损计算口径

```
day_pnl   = 当前权益 − 当日开盘权益
loss_pct  = -day_pnl / 当前权益        （仅当 day_pnl < 0 时有意义）
拒绝条件  = loss_pct >= max_daily_loss_pct
```

- 「当日开盘权益」由 `PaperBroker._roll_day()` 在跨日时记录；真实券商应改用 `get_assets_summary().dayPnl`。
- 分母用**当前权益**而非开盘权益，是偏保守的选择（亏损后权益变小，同样的亏损额占比更高，更早触发）。
- 已实现盈亏与浮动盈亏都计入（因为用的是权益差，而非已实现）。

---

## 9. 交易时段（近似）

`SESSIONS` 常量：

| 市场 | 时段（本机时区） |
|---|---|
| `hk` | 09:30–12:00, 13:00–16:00 |
| `cn` | 09:30–11:30, 13:00–15:00 |
| `us` | 21:30–23:59, 00:00–04:00（近似，未做 DST 精确推算） |

周末一律判为休市。

**已知局限**：没有处理香港/美股公众假期、半日市、美股夏令时切换。生产环境应改为查询券商的 `market status` 接口，或引入交易日历表。当前实现是"宁可判为休市也不误开仓"的保守近似。

---

## 10. fail-closed 论证

任何异常都不允许变成"放行"：

```python
try:
    return self._evaluate_inner(...)
except Exception as exc:
    return RiskDecision(approved=False, ..., reasons=[f"风控内部异常，按拒绝处理: ..."])
```

可能抛异常的点：`portfolio.position_of()`（券商返回异常数据）、`intent.notional`（qty 为 None）、字段缺失等。
以上全部被捕获并转为拒绝，且异常本身写入 `checks` 供排查。

对应测试：

```python
def test_fail_closed_on_internal_error(risk):
    class Boom:  # position_of / position_count 抛 RuntimeError
        ...
    d = risk.evaluate(_buy(), Boom(), last_price=300.0, now=WEEKDAY_1000)
    assert not d.approved
    assert any("内部异常" in r for r in d.reasons)
```

---

## 11. 变更风控参数的正确流程

1. 改 `config/config.yaml` 的 `risk:` 段
2. `pytest tests/test_risk_controls.py -q` —— 必须全绿
3. `ai-trader demo` —— 观察沙盒里的实际下单量是否符合预期（重点看 `binding`）
4. 至少跑一个完整交易日的模拟盘，对比 `risk_decisions` 表里的拒绝原因分布
5. 才考虑调整真实交易的开关

**不要**为了"让某笔单通过"而临时放宽阈值。风控拒绝是有信息量的：先看 `reasons`，判断是信号问题还是参数问题。

---

## 12. 风控未覆盖的风险（诚实清单）

| 风险 | 现状 | 建议 |
|---|---|---|
| 滑点与流动性 | 模拟盘固定 5bp 滑点 | 真实盘需按盘口深度估算，或改用市价单+限价保护 |
| 汇率 | 多币种各自独立计算权益，**未做 FX 折算** | 真实盘需统一折算到报告币种 |
| 集中度相关性 | 只限制单标的占比，未限制同行业/同因子暴露 | 引入行业分类与相关性矩阵 |
| 隔夜跳空 | 止损为轮询触发 | 改用券商服务端条件单 |
| 数据错误 | 行情源异常时仍可能下单 | 增加"行情合理性校验"（如涨跌幅 > 20% 暂停） |
| 进程宕机 | 止损注册表在本地文件 | 部署 watchdog / 计划任务巡检 |
| T+1 / 涨跌停 | 未建模 | A股通必须补，否则会出现"看起来能卖其实不能卖" |
| 部分成交 | 模拟盘一次性全额成交 | 真实盘需处理 `partial` 状态并调整止损数量 |

---

## 13. 2026-11-08 修复补充（P0/P1）

本节记录本轮安全修复对风控的改动，与上面章节冲突时**以本节为准**。

### 13.1 急停从"运行时状态"变为"持久状态"

| 项 | 修复前 | 修复后 |
|---|---|---|
| 存储 | 内存字段 | `data/HALT.json`（原子写） |
| 重启后 | 恢复运行 | **仍然停止**（`build_gate` 读哨兵） |
| 解除 | `resume()` 无参 | `resume("RESUME-TRADING")` 逐字确认 |
| 存储异常 | 无感知 | `halt_store_failed=True` → `halt_storage` 检查拒绝一切交易 |

实现：`safety/halt.py`、`risk/controller.py:228-280`、`config.py:build_gate`。

### 13.2 新增 `unresolved_order_state`（订单状态未知即封锁）

券商未返回 `orderId` 或网络中断时，订单状态记为 `unknown`。
在对账确认之前，**该标的禁止任何新委托**（不只是同方向）。

理由：状态未知意味着"可能已成交也可能没有"，此时再下单可能变成双倍仓位。
解除封锁只有两条路：对账查到真单（转为真实状态），或对账明确查不到（转 `not_accepted`）。

### 13.3 `daily_loss_limit` 的"无法判定即拒绝"

`Portfolio.day_pnl_known=False`（券商未返回 `dayPnl`）时，检查直接判 `ok=False`：

```
当日盈亏不可用，单日亏损熔断无法判定，拒绝新开仓
```

这不是保守，而是逻辑必然：熔断的目的是"亏到 3% 就停"，如果连当前盈亏都不知道，
就无法证明没到 3%。

### 13.4 跨进程重复单改为数据库约束

| 层 | 修复前 | 修复后 |
|---|---|---|
| L1 冷却 | 进程内 `list` | `order_attempts` 表 + `UNIQUE(symbol, side, bucket)` |
| 跨进程 | ❌ 失效 | ✅ 第二个进程 `IntegrityError` → 拒绝 |
| 落库失败 | 无处理 | 拒绝下单（"无法写入跨进程冷却记录"） |

`bucket = int(now.timestamp()) // duplicate_window_seconds`。
失败的下单会 `release_order_attempt` 释放槽位，不会白等 300 秒。

### 13.5 订单超时撤单

新增 `risk.order_timeout_seconds`（默认 600，0=关闭）。
`StrategyEngine.monitor()` 每轮会把 `submitted/pending/partial` 且超过时限的委托撤掉并置 `expired`。

为什么这属于风控：未撤的挂单会**永久占用**资金额度、`max_positions` 名额，
并通过 `duplicate_order`（L2 未完成委托）**永久阻断**该标的的同向新单。

### 13.6 持仓读取失败 = 整轮停手

风控的敞口/持仓数/单标的占比全部依赖持仓数据。持仓读不出来时，
`StrategyEngine.scan()` 直接 `blocked_reason = "持仓读取失败，禁止交易：..."` 并返回，
**不进入任何标的的决策流程**。

### 13.7 仍未覆盖

| 项 | 说明 |
|---|---|
| 无止损持仓 | 已加 `audit: position_without_stop` 告警，但**不会自动补止损**（那等于替用户做决定） |
| 汇率 | 仍未折算，多币种权益为名义加总 |
| T+1 / 涨跌停 | 仍未建模（A股通上真实盘前必须补） |
| 隔夜跳空 | 止损仍靠轮询；建议改用券商服务端条件单 |

---

## 14. 2026-11-08 第三轮补充：AI 决策权威（P1-9）

### 14.1 位置：风控**之前**的独立闸门

```
行情 → 指标 → 【AI 决策权威】 → AI 分析 → 信号校验 → 【风控】 → 下单
                    ↑                                        ↑
              规则引擎在这里被拦                 本文件第 2–13 节的全部检查
```

决策权威不是风控的一部分，而是风控的**前置条件**：
它回答"有没有资格产生开仓信号"，风控回答"这个信号能不能执行"。

之所以放在风控之前：如果 AI 不可用，连"要不要交易"的判断都不该存在，
不必浪费风控配额、也不必产生一堆 `rejected` 噪音。

### 14.2 四类拒绝码

| 码 | 条件 | 是否影响已有持仓 |
|---|---|---|
| `AI_NOT_DECISION_AUTHORITY` | 模型 `can_trade=False`（规则引擎） | 否 |
| `AI_UNAVAILABLE` | LLM 无 Key / 构建失败 | 否 |
| `AI_DEGRADED` | 连续失败 ≥ `max_consecutive_failures`，冷却期内 | 否 |
| `STALE_SIGNAL` | 信号时间戳超过 `signal_ttl_seconds` | 否 |

**"不影响已有持仓"是关键设计**：止损/止盈走 `RiskController.scan_exits`，
由 `monitor()` 调用，完全不经过决策权威。AI 挂了，仓位照样被保护。

### 14.3 与风控的关系（不要混淆）

| | 决策权威 | 风控 |
|---|---|---|
| 管什么 | 有没有资格产生开仓信号 | 这个信号能不能执行、多大 |
| 输入 | 模型可用性 / 熔断状态 / 信号时间 | 意图 + 组合 + 市场状态 |
| 输出 | 允许分析 / 拒绝（带码） | `RiskDecision`（逐项检查） |
| 失败时 | 不开新仓 | 拒绝该笔 |

两者都满足才下单。任何一个说不，就不下单。

### 14.4 配置项

```yaml
ai:
  require_ai_for_entry: true      # 规则引擎不得开仓（默认）
  signal_ttl_seconds: 300         # 信号有效期
  max_consecutive_failures: 3     # 连续失败几次进入冷却
  failure_cooldown_seconds: 120   # 冷却时长
```

`require_ai_for_entry: false` 会让规则引擎重新获得开仓权 —— **仅用于 demo/回测**，
生产装配不提供这个入口（`build_runtime` 固定 `allow_rule_engine_entry=False`）。

### 14.5 审计事件

| 事件 | 触发 | 关键字段 |
|---|---|---|
| `entry_blocked` | 每一次拒绝开仓 | `code`、`reason`、`symbol`、`model`、`action_taken=no_new_entry` |
| `ai_error` | 模型调用异常 | `code`、`error`、`failures`、`degraded` |
| `ai_recovered` | 从故障中恢复 | `at`、`model` |
| `stale_signal_rejected` | 信号过期 | `signal_id`、`signal_ts`、`reason` |

排查"为什么没交易"时，先搜 `entry_blocked`，里面一定有具体码。

---

## 15. 2026-11-08 第四轮补充：交易规则与行情闸门（B4/B5）

### 15.1 闸门顺序（最终形态）

```
行情 → 指标 → ①行情新鲜度(B5) → ②AI 决策权威(P1-9) → AI 分析 → 信号校验
     → ③交易规则(B4) → ④风控(第 2–13 节) → 下单
```

| 闸门 | 位置 | 只在什么情况下拦 |
|---|---|---|
| ① 行情新鲜度 | `engine._feed_gate` | 推送启用且该市场纳管时；只拦**新开仓** |
| ② AI 决策权威 | `engine.authority` | 规则引擎/无 Key/故障冷却/信号过期；只拦**新开仓** |
| ③ 交易规则 | `risk.rules.check_order` | 板块/涨跌停/最小单位/停牌/日历；买卖都拦 |
| ④ 风控 | 本文 §2–§13 | 仓位/风控/资金/频率等 |

三个新闸门的共同设计：**只拦新开仓，不拦止损**。
理由：风控退出是"降风险"，任何外部依赖故障都不应阻止减仓。

### 15.2 B4 规则层如何接入风控

规则校验在 `_evaluate_inner` 的 `market_enabled` 之后执行，产出 `rule:<CODE>` 检查项：

| 错误码 | 触发 |
|---|---|
| `RULE_UNKNOWN` | 标的无法解析到板块，或板块规则 `status=UNKNOWN` |
| `RULE_UNVERIFIED` | 规则 `UNVERIFIED` 且 `allow_unverified_rules=false` |
| `OUT_OF_SESSION` | 不在该市场的交易时段（未配置时段 → 拒绝） |
| `NOT_TRADING_DAY` | 周末 / 节假日 / 日历不可信且 `strict` |
| `SUSPENDED` | 在 `suspended_symbols` 内 |
| `LOT_SIZE` | 低于最小单位 / 非递增单位 / 零股未一次性卖出 |
| `INSUFFICIENT_SELLABLE` | 卖出 > 持仓 − 当日买入 |
| `T_PLUS_VIOLATION` | T+1 市场当日买入不可卖 |
| `PRICE_LIMIT` | 委托价超涨停 / 破跌停 |
| `NO_PRICE_LIMIT_DATA` | 该板块有涨跌幅限制但缺昨收价 |

规则层抛异常 → 风控 `fail-closed` 拒绝（检查名 `market_rules`）。

### 15.3 B5 行情闸门的判据

`feed.is_fresh(symbol)` 必须同时满足：

1. 连接状态 `SUBSCRIBED`
2. 该标的收到过行情
3. 行情有时间戳
4. 该标的在重连后已完成 `settle`
5. `now - ts <= stale_after_seconds`

不满足 → `FEED_NOT_FRESH`，只拦新开仓，审计写 `entry_blocked`。

### 15.4 与账户级风控的边界

| 规则层 | 账户层（本文 §2–§13） |
|---|---|
| 交易所规则（能不能下这一单） | 账户风险（该不该下、下多大） |
| 与账户规模无关 | 与权益/敞口/日亏相关 |
| 硬约束，无法通过调参放宽 | 可配置阈值 |

两者都过才下单。
