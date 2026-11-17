# 星财富 AI — 日线波段交易模式切换报告

**执行时间**：2026-11-17
**项目**：`C:\FosunAI-Trading`（分支 `master`，起点 `ebb90d8`）
**最终状态**：**仅历史回测与模拟交易；真实交易保持关闭**（`us_phase2.enabled` 未触碰）

---

## 一、结论速览

| 问题 | 结论 |
|---|---|
| WebSocket 是否已解除强制依赖 | ✅ **是**（结构性已验证 + 测试固化；`market_feed.enabled=false`） |
| 日线数据实际覆盖 | **2021-11-17 ～ 2026-11-16，1254 个公共交易日**，四标的零缺口 |
| 数据质量 | 2026 段与官方 NYSE 节假日表**逐日吻合**（0 违反）；拆股已调整、分红未调整（已量化） |
| 回测是否完成 | ✅ 7 个方案 + 8 类测试（32 项新测试，全量 1578 项通过） |
| **策略是否可用** | ❌ **不可用于实盘**：显著跑输 QQQ 买入持有，见 §6.3 |
| 数据库是否变化 | **仅 `data/market_us.db` 增加 5016 行日线，0 行被改/删**；另两个库哈希未变 |
| 尚未解决 | $300 本金买不起 QQQ 一手（整股）；分红未计入；日历配置仅覆盖 2026–2027；新日线引擎**未接入运行时主循环**（刻意，见 §8） |

**一句话**：日线架构**搭起来了、验证过了、可以回测和模拟运行**；但按任务书给定的规则组合，它在 5 年样本上**打不过直接持有 QQQ**，因此**不建议上线**，本次只交付可复现的回测与模拟能力。

---

## 二、执行前保护

| 项 | 值 |
|---|---|
| 路径 / 分支 | `C:\FosunAI-Trading` / `master` |
| 起始 HEAD | `ebb90d8f2b5e0ad827ad6d584c8f9895d24c7868` |
| 备份目录 | `backups/us_daily_swing_20261117-213125/`（新建，未覆盖历史备份） |
| 备份内容 | `src/` + `tests/` + `config/` + `data/*.db` + 运行状态 json/jsonl，共 **207** 个文件 |
| 清单 | `MANIFEST.sha256.json`，与当时工作树**逐条比对 0 不一致** |
| 数据库改前哈希 | `market_us.db d427bbf3…`、`audit.db c4bdfdf1…`、`us_loop.db ccc79f2a…` |
| 未删除 | 未删任何 WebSocket 代码、未重置数据库、未覆盖历史数据 |

---

## 三、修改文件及具体内容

### 3.1 新增生产模块（`src/`，三个）

| 文件 | 行数 | 作用 |
|---|---|---|
| `src/ai_trader/strategy/daily_swing.py` | 342 | 日线波段策略：指标、状态分类、目标映射、QID 四规则、决策对象 |
| `src/ai_trader/strategy/daily_backtest.py` | 420 | 回测引擎：D 决策 / D+1 开盘成交、MOC 退出、$1/边费用、滑点、订单状态机、指标 |
| `src/ai_trader/market/daily_gate.py` | 163 | 日线数据闸门：对齐/充分/新鲜/日历一致/bar 合规五项硬条件 |

### 3.2 新增工具（`tools/`，两个）

| 文件 | 作用 |
|---|---|
| `tools/fetch_daily_bars.py` | 抓取入库，复用既有 `fetch_chart`+`BarStore`，**额外保留 `events.splits`** |
| `tools/daily_swing_backtest.py` | 一键跑 7 个方案，产出 `reports/daily_swing_backtest.json` |

### 3.3 新增测试

`tests/test_daily_swing.py`（391 行 / **32 项**），严格对应任务书 §六的八类要求。

### 3.4 对既有代码的改动

**零。** `git status` 显示 `src/` 下**只有新增文件，没有修改任何既有文件**。
风控、策略引擎、订单、券商适配、Paper Runtime、配置、密钥**一行未动**。

> 从更早一轮遗留的两处未提交改动（`tools/fosun_ws_capture_once.py`、`tests/test_fosun_ws_capture.py`）与本轮无关，本轮未触碰。

---

## 四、WebSocket 强制依赖是否解除

**是，且是结构性的，不是绕过。**

| 层 | 事实 | 证据 |
|---|---|---|
| 配置 | `market_feed.enabled: false` | `config/config.yaml` |
| 构建 | `build_feed()` 在 `enabled=false` 时**直接 return None** | `runtime.py:132-135` |
| 引擎门禁 | `_feed_gate()` 首行 `if self.feed is None or not self._feed_required: return True` | `engine.py:749` |
| 市场范围 | 仅 `enforce_markets: ["cn"]`；美股**从未纳入**推送校验 | `config.yaml` |
| 新日线链路 | 闸门 → 策略 → 回测，**全程不持有任何 feed 对象** | `tests/test_daily_swing.py::test_daily_pipeline_runs_without_any_feed` |

新增 3 项测试固化：`build_feed` 返回 None、`_feed_gate` 放行、日线全链路无 feed 可跑通。
**WebSocket 代码完整保留、默认关闭**，随时可重新启用而不改结构。

**延迟行情标注**：`FosunProvider.capabilities()` 恒为 `realtime=False`，`fosun_sessions` 的判定集里 `DELAYED` 与 `REALTIME_CONFIRMED` 严格互斥；日线入口只接受 `BarStore` 的 `trade_date/OHLCV`，测试 `test_delayed_quote_is_not_treated_as_daily_history` 固化了"延迟快照不能当日线"这一点。

---

## 五、日线数据实际覆盖与质量

### 5.1 覆盖范围

| 标的 | 根数 | 起 | 止 | 来源 |
|---|---:|---|---|---|
| `usqqq` (QQQ) | 1254 | 2021-11-17 | 2026-11-16 | `yahoo:chart:1d` |
| `usqld` (QLD) | 1254 | 2021-11-17 | 2026-11-16 | 同上 |
| `usqid` (QID) | 1254 | 2021-11-17 | 2026-11-16 | 同上 |
| `ustqqq` (TQQQ) | 1254 | 2021-11-17 | 2026-11-16 | 同上 |

**公共交易日 1254 个**，四标的日期**完全一致**，`weekday_gaps` 全为 **0**。
入库时 `rows_rejected=0`、`duplicates_dropped=0`（既有 `validate_bar` 全部通过）。

### 5.2 交易日历校验（用官方表，不用推测）

配置里的美股日历标注 `source: NYSE Holidays & Trading Hours`、`covered_from: 2026-01-01`。

对 **2026-01-01 ～ 2026-11-16**（220 个交易日）逐日比对：

- 官方节假日却出现 bar：**0**
- 应交易却缺 bar：**0**

对配置未覆盖的 **2021-11-17 ～ 2025-12-31**：1076 个工作日中有 1034 根 bar，**缺口 42 个 = 10.2/年**，且逐个可识别为美股全市场假日（2021-11-25 感恩节、2022-04-15 耶稣受难日、2022-06-20 六月节、2022-12-26 圣诞补休……）。**没有"非假日的缺数据"。**

### 5.3 复权处理（发现了与代码注释不一致的事实）

`bars.py` 的文档写"存储的是**未复权**原始 OHLCV"。**实测不是这样。**

四只标的共检测到 4 次拆股事件（来自 Yahoo `events.splits`）：

| 标的 | 日期 | 比例 |
|---|---|---|
| QLD | 2025-11-20 | 2:1 |
| QID | 2024-04-10 | 1:5（反向） |
| TQQQ | 2022-01-13 | 2:1 |
| TQQQ | 2025-11-20 | 2:1 |

若真是未复权，拆股日必有 ±50%/+400% 的跳空。**实测四个拆股日的跳空分别是 +3.96%、+2.48%、+1.03%、+5.85%** —— 序列是连续的。结论：

> **Yahoo `quote` 的 OHLC 是「拆股已调整、分红未调整」。**

量化分红缺口（QQQ 5 年）：`close` 口径 **+66.42%** vs `adjclose` **+71.47%**，即**少计 2.94%**（≈0.58%/年）。
**对本策略是保守偏差**（分红不计入收益、模拟账本也不派息），已在 §8 列为已知偏差，不做静默补偿。

异常价格：全窗口 `|日收益| >= 35%` 仅 1 处 —— TQQQ `2025-04-09 +35.24%`，为 3 倍杠杆下的真实单日行情（标的 +11.7%），**不是拆股残留**，测试中已显式白名单并注明理由。

---

## 六、回测结果

**口径**：初始 **$300**，买入/卖出各 **$1 固定手续费**，单边滑点 **5bp**；决策在 D 日收盘后、成交在 **D+1 开盘**（QID 的止盈/趋势失效/强制退出按规则为收盘成交 MOC）；不使用任何未来数据。

### 6.1 方案矩阵

| 方案 | 净收益 | 最大回撤 | 交易次数 | 胜率 | 盈亏比 PF | 每笔净期望 | 手续费 | 空仓比例 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **A 仅 QQQ(1x)** | **+41.73%** | **-13.63%** | 19 | 31.6% | **2.06** | **+$6.59** | $38 | 45.2% |
| B +QLD(2x) | +10.44% | -27.70% | 59 | 55.9% | 1.13 | +$0.53 | $118 | 54.2% |
| C +QLD+QID | **-15.75%** | -31.02% | 64 | 54.7% | 0.81 | -$0.74 | $128 | 53.8% |
| D 仅QQQ 费率×2 | +27.10% | -14.86% | 19 | 31.6% | 1.59 | +$4.28 | $76 | 45.2% |
| E 仅QQQ 费率×5 | -16.78% | -32.96% | 19 | 31.6% | 0.74 | -$2.65 | $190 | 45.2% |
| F 仅QQQ 滑点×4 | +33.51% | -14.19% | 19 | 31.6% | 1.78 | +$5.29 | $38 | 45.2% |
| G 仅QQQ 整股 | -3.62% | -8.29% | 2 | 0% | 0.00 | -$5.43 | $4 | 96.3% |
| **基准：QQQ 买入持有** | **+131.30%** | **-22.88%** | 1 | — | — | — | 0 | 0% |

### 6.2 各市场状态的表现（方案 A）

| 状态 | 交易数 | 净利润 | 胜率 |
|---|---:|---:|---:|
| `UPTREND_NORMAL`（→QQQ 1x） | 18 | +$68.63 | 27.8% |
| `UPTREND_STRONG`（→QQQ 1x，因 QLD 关闭） | 1 | +$56.56 | 100% |
| `DOWNTREND` / `PANIC` / `REBOUND` / `CHOP` | 0 | $0 | — |

方案 C 中 QID 单独看：**5 笔、净 -$49.02、胜率 40%** —— 反向 ETF 这一腿是**净亏损来源**。

### 6.3 三条必须直说的结论

**① 策略跑输买入持有，且风险调整后也跑输。**
A 方案 +41.7% / 回撤 -13.6%（收益回撤比 3.06），基准 +131.3% / -22.9%（比值 **5.74**）。基准在**两个维度同时占优**。根因不是实现错误，而是规则本身：45% 时间强制空仓，等于放弃了这段牛市的一半仓位。

**② QLD（2x）升级"经验证后"应当不启用。**
A→B：收益从 +41.7% 掉到 +10.4%，交易数 19→59，回撤从 -13.6% 恶化到 -27.7%。
原因是 2x 触发条件（`UPTREND_STRONG`）与 1x 交替出现，每次切换都是"卖 QQQ + 买 QLD"两笔成本（≈$2 + 滑点 = 0.77%），一年切好几次就把 2x 的超额吃光，还额外承担了杠杆损耗。
**这正是任务书"经过验证后考虑"应有的答案：验证结果 = 不启用。**

**③ 杠杆 ETF 的每日复位损耗有实测数据支撑。**
同窗口 2021-11-17→2026-11-16：QQQ **+66.4%**、QLD **+67.7%**、TQQQ **+25.5%**、QID **-79.8%**。
2x 与 1x 几乎打平，3x 反而只有 1x 的 38%。这就是"TQQQ 不得默认启用""QID 只在持续确认下跌中评估"的数据依据，而不是凭感觉设限。

### 6.4 成本敏感性

- 手续费翻倍（$2/边）：+27.1%，仍盈利 → **对费率有缓冲**。
- 手续费 5 倍（$5/边）：**-16.8%** → 盈亏平衡点在 $2～$5/边之间。
- 滑点 4 倍（20bp/边）：+33.5% → 滑点不是主要风险。
- **换手才是主要风险**：A→C 只多了 QLD/QID 两条腿，交易数 19→64，手续费 $38→$128，收益由 +41.7% 转为 -15.8%。

$300 本金下每笔往返成本率 = `2/300 = 0.67%`，加滑点约 **0.77%**。这是硬约束，**任何高频变体都会被成本碾平**。

---

## 七、测试结果

**全量 1578 项，失败 0、错误 0、跳过 1**（JUnit XML 核准；基线 1546 + 本轮 32）。

| # | 要求 | 对应测试 | 结果 |
|---|---|---|---|
| 1 | 历史日线数据质量 | `test_data_coverage_and_no_gaps`、`test_data_ohlc_sanity_and_no_duplicates`、`test_data_has_no_split_like_jump_in_window`、`test_data_last_bar_is_a_confirmed_close` | ✅ |
| 2 | 策略回测 | `test_backtest_never_uses_future_data`、`test_backtest_daily_decisions_are_unique`、`test_backtest_metrics_are_internally_consistent` | ✅ |
| 3 | 手续费与滑点压力 | `test_cost_stress_is_monotone`、`test_flat_fee_dominates_at_small_capital` | ✅ |
| 4 | 杠杆 ETF 每日复位 | `test_leverage_decay_is_visible_in_real_data`、`test_daily_reset_math_for_two_day_path` | ✅ |
| 5 | 五种市场状态 | `test_regime_classification`（3 组参数化）、`test_panic_and_rebound_are_detected`、`test_strong_uptrend_maps_to_qld...`、`test_tqqq_is_never_default`、`test_cash_regimes_never_short_psq_or_sqqq` | ✅ |
| 6 | 模拟订单与成交状态 | `test_insufficient_cash_is_rejected_not_faked`、`test_missing_next_bar_marks_unfilled`、`test_no_same_day_close_fill_for_entry`、`test_qid_forced_exit_within_two_sessions`、`test_duplicate_decision_guard` | ✅ |
| 7 | WebSocket 关闭时完整运行 | `test_ws_disabled_does_not_block`、`test_feed_gate_passes_without_feed`、`test_daily_pipeline_runs_without_any_feed` | ✅ |
| 8 | 缺失/延迟/过期安全 | `test_gate_blocks_stale_and_missing`、`test_gate_flags_incomplete_history`、`test_gate_flags_calendar_gap`、`test_strategy_refuses_misaligned_data`、`test_strategy_refuses_insufficient_history`、`test_delayed_quote_is_not_treated_as_daily_history` | ✅ |

未删除任何既有测试、未降低任何断言标准。`test_daily_swing.py` 中的合成序列仅存在于内存，**未写入数据库**。

---

## 八、回测过程中发现并修复的 6 个真问题

这轮不是"一次写对"，下面每个都是**跑出异常结果后定位到根因**再修的。

| # | 问题 | 症状 | 根因 | 修复 |
|---|---|---|---|---|
| 1 | **QID 退出用错价格** | 出现"均价 127 → 收盘 274，+115% 止盈"的荒谬成交 | `evaluate_exit(pos, hist, ...)` 传的是 **QQQ** 的 bars，拿 QQQ 收盘价去比 QID 成本 | 改传 `_bars_until(SYM_QID, day)`，并加断言：缺当日 QID bar 直接抛错 |
| 2 | **持仓估值用错标的** | 回撤与净值曲线失真 | `equity = cash + qty * qqq_close`，对 QLD/QID/TQQQ 也用了 QQQ 价 | 改为 `close_of(holder, day)`，缺当日 bar 抛错（禁止旧价顶替） |
| 3 | **`STRONG_VOL_CAP` 单位错误** | `UPTREND_STRONG` **一次都没触发**，QLD 腿完全空转 | 写成 `0.028`（当成日波动），但 `realized_vol` 返回**年化** | 改为 `0.28`（28% 年化上限），QLD 腿随即正常触发 |
| 4 | **横盘被判成上涨** | 完全平坦的序列返回 `UPTREND_NORMAL` | `close == SMA50` 时 `close < sma50` 为 False → 归入"在均线上方"，且斜率 `>= 0` | 改为严格判定：`above_slow = close > sma50 + eps`、`slope > 0` |
| 5 | **决策字段不一致** | 审计消费方 KeyError | QID 退出分支用 `date`，常规分支用 `decide_on` | 统一为 `decide_on` |
| 6 | **`DOWN_CONFIRM_DAYS` 定义了没用** | QID 在下跌第 1 天就开仓，反复被 2 日强平 | 常量存在但 `decide()` 没读 | 接入 `regime_streak`，下跌需连续 ≥10 日才评估 QID |

另外修正了一处**文档与事实不符**：`bars.py` 声称"未复权"，实测为"拆股已调整、分红未调整"（见 §5.3）。**代码未改**（改它会破坏既有 `us_bars` 语义与 1500 行历史数据的一致性），而是在报告与抓取工具里显式记录。

### 设计被数据否决后加的约束

首版（无状态确认、无冷却）跑出 **-89.8%**、59 笔、手续费 $118。定位到"状态单日翻转 → 反复打脸"后加入两条**成本驱动**的约束：

- `MIN_ENTRY_STREAK = 2`：状态需连续 2 日才允许开仓；
- `REENTRY_COOLDOWN = 5`：平仓后同标的冷却 5 日。

结果：A 方案从 -89.8% 提升到 **+41.7%**、交易数 59→19。两条约束都是**费用结构**推出来的（往返 0.77%），不是对着收益曲线调参；报告同时给出未加约束的对照数字，便于复核。

---

## 九、数据库是否发生变化

| 库 | 变化 |
|---|---|
| `data/market_us.db` | **仅追加**：`us_bars` 1500 → **6516** 行（+5016，新增 4 个标的×1254 根）；`us_bar_ingest_log` 3 → 7 行 |
| 旧数据 | **删除 0 行、修改 0 行**（逐行 digest 比对确认） |
| `data/audit.db` | **哈希未变** |
| `data/us_loop.db` | **哈希未变** |

未重置数据库、未删除任何历史数据、未改写既有 1500 行。

---

## 十、尚未解决的阻塞问题

1. **$300 本金买不起 QQQ 一手（整股）** —— QQQ 在本窗口内一直在 $280～$660，整股模式下 5 年只成交 **2 笔**（方案 G：-3.62%、96.3% 空仓）。策略的核心 1x 腿在整股约束下**不可执行**。方案 A 的全部结果都基于**分数股**假设；分数股是否可用**未经券商确认**，属未验证前提。

2. **策略不敌买入持有**（+41.7% vs +131.3%，风险调整后亦落后）。按当前规则组合，它没有可上线的超额收益来源。若要改善，需要改**规则**（例如放宽空仓、加趋势跟随的加仓机制），那属于策略设计变更，**不在本轮授权范围**，也未擅自实施。

3. **分红未计入**：年化约 0.58% 的偏差（对策略保守）。要补需要引入 `adjclose` 并改造 `us_bars` 语义，影响既有 1500 行历史数据，需单独批准。

4. **交易日历配置只覆盖 2026-01-01 ～ 2027-12-31**。2026 前已用"公共日历 + 假日可识别性"间接验证（42 个缺口全为假日），但**没有逐日官方依据**。向前推进到 2028 需补官方表。

5. **新的日线引擎尚未接入运行时主循环**。当前它是一组可独立运行、可回测、可测试的模块 + 工具；`runtime.py` / `engine.py` 的实盘/模拟主循环**未改动**，因为接入必然要动订单与风控路径，而任务书要求"不允许未经验证修改现有风险限制"。**这是刻意的边界**，不是遗漏。

6. **TQQQ 极端事件条件只做到"候选 + 需人工确认"**（`TQQQ_CANDIDATE_ONLY` 标记、目标仓位 0），**没有实现具体触发阈值** —— 任务书要求"不得默认启用"，在缺乏验证样本时凭空定阈值等于编造，故留空并标注。

---

## 十一、复现方式

```bash
# 1) 抓取/更新日线（复用既有 fetch + BarStore）
python tools/fetch_daily_bars.py --range 5y

# 2) 跑 7 个方案，产出 reports/daily_swing_backtest.json
python tools/daily_swing_backtest.py

# 3) 八类验收测试
python -m pytest tests/test_daily_swing.py -q
```

产物：`reports/daily_swing_backtest.json`（含每条决策、每笔订单、每笔交易、完整净值曲线与数据质量明细）、`data/daily_fetch_report.json`（抓取元数据与拆股事件）。

---

## 十二、最终状态确认

- 真实交易：**关闭**（`safety.live_trading=false`、`order_execution_enabled=false`，本轮未改）
- `us_phase2.enabled`：**false**（未触碰）
- WebSocket：代码保留、默认关闭、不参与启动/策略/模拟交易
- 本轮产出能力：**历史回测 + 模拟交易**
- 未连接任何券商、未发送任何订单、未修改真实账户资金
