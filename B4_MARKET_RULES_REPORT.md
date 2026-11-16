# B4_MARKET_RULES_REPORT.md —— A 股通交易规则

**项目**：`C:\FosunAI-Trading`
**阶段**：B4（交易基础能力建设第一阶段）
**时间**：2026-11-08 13:37 – 14:05 (+08:00)
**约束**：仅本机模拟数据与模拟盘，未连接真实券商

---

## 0. 一页结论

| 项 | 状态 |
|---|---|
| 现有实现审计 | ✅ 完成（原实现只有"周末 + 时段"，**无 T+1、无涨跌停、无最小单位、无停牌、无日历**） |
| 规则配置化 | ✅ 9 个板块 + 日历，全部 YAML 配置，带 `source` / `effective_from` / `status` |
| T+1 | ✅ 实现并测试（含跨日可卖） |
| 涨跌停 | ✅ 分板块计算，含 0.01 元四舍五入边界 |
| 分证券类别 | ✅ 主板/科创/创业/北交所/主板 ST/科创 ST/创业 ST |
| 停牌 + 非交易时段 | ✅ 实现（停牌双向禁止，时段按市场） |
| 最小交易单位 | ✅ 含科创板 200 股起/1 股递增、零股一次性卖出 |
| 资金/可用数量 | ✅ 卖出可卖数 = 持仓 − 当日买入 |
| 交易日历 | ⚠️ 机制完整，**节假日表未内置**（无法离线核实）→ 默认拒绝 A 股交易 |
| 规则不明确即拒绝 | ✅ `UNKNOWN` 一律拒绝；`UNVERIFIED` 由 `allow_unverified_rules` 决定 |
| 测试 | ✅ **30 个用例全部通过** |

**当前实际效果**：A 股通在默认配置下**不可交易** —— 因为节假日表状态是 `UNKNOWN` 且
`calendar.strict: true`。这是刻意设计：没有可信日历就不做 A 股。填入官方休市日后即可启用。

---

## 1. 审计：原有市场规则实现

### 1.1 原有实现（`risk/controller.py:165-190`）

```python
SESSIONS = {
    "hk": [((9,30),(12,0)), ((13,0),(16,0))],
    "cn": [((9,30),(11,30)), ((13,0),(15,0))],
    "us": [((21,30),(23,59)), ((0,0),(4,0))],
}

def is_market_open(market, now):
    if now.weekday() >= 5: return False, "周末休市"
    ...按分钟比较...
```

### 1.2 审计结论

| 能力 | 原有状态 | 问题 |
|---|---|---|
| T+1 | ❌ 完全没有 | 当日买入可以立刻卖出 —— 在 A 股属于违规 |
| 涨跌停 | ❌ 完全没有 | 可以下出交易所必然拒绝的价格 |
| 分板块差异 | ❌ 完全没有 | 只有 `markets.cn.lot_default: 100`，科创板 200 股起的要求被掩盖 |
| 停牌 | ❌ 完全没有 | 停牌标的照常下单 |
| 交易日历 | ❌ 只有周末 | 国庆/春节等休市日照常交易 |
| 最小交易单位 | ⚠️ 只有 `lot_default` | 仅在 `size_position` 里取整，没有任何校验 |
| 规则来源 | ❌ 无 | 数字写死在代码里，没有来源、没有生效日期 |
| 规则不明确时 | ⚠️ 默认放行 | `is_market_open` 无配置时返回 `True`（fail-open） |

**最严重的一条**：原 `is_market_open` 在"市场无时段配置"时 **返回 True**，
即"不知道就当能交易"。新实现改为 **返回 False**（不知道就当不能交易）。

---

## 2. 规则来源与可信度

### 2.1 依据

2026-04-24 上交所/深交所/北交所同步发布《交易规则(2026 年修订)》，
**2026-07-06 起正式实施**。本次修订中与本工程直接相关的一条：

> 沪深主板风险警示股票（ST/*ST）涨跌幅限制**由 ±5% 放宽至 ±10%**，
> 与主板普通股票一致；创业板、科创板 ST 维持 ±20%，北交所风险警示股 ±30% 不变。

来源：
- 央视网 `finance.cctv.cn` 2026-07-06《A股交易新规今起施行 投资者需注意三大变化》
- 中国经济网 `ce.cn` 2026-07-06 同日报道
- 人民财经网 2026-07-03 同主题报道

**可信度标注**：`source_tier: secondary`（权威媒体转述官方修订，**未取得交易所原文 PDF**）。
配置里每条规则都写了 `source` 与 `effective_from`，不做"看起来像官方"的模糊表述。

### 2.2 为什么必须配置化

如果按"训练数据里的常识"写死，会得到 **`ST 股 ±5%`** —— 而 2026-07-06 起
这个数字已经变成 10%。一条写死的常量会让系统在 ST 股上把所有正常报价判成"超涨停"，
直接停掉该类标的的交易。

因此：

```yaml
cn_st_main:
  price_limit_pct: 0.10
  meta:
    effective_from: "2026-07-06"
    note: "2026-07-06 之前为 ±5%，历史回测需按日期切换"
```

---

## 3. 实现的规则表

| 板块 | 代码前缀 | 涨跌幅 | 最小买入 | 递增 | T | 来源状态 |
|---|---|---|---|---|---|---|
| `cn_main` 沪深主板 | `cn6` `cn0` `sh6` `sz0` | ±10% | 100 | 100 | T+1 | VERIFIED(secondary) |
| `cn_st_main` 主板 ST | 手工登记 `st_symbols` | ±10%（2026-07-06 起） | 100 | 100 | T+1 | VERIFIED(secondary) |
| `cn_star` 科创板 | `cn688` `sh688` | ±20% | 200 | 1 | T+1 | VERIFIED(secondary) |
| `cn_st_star` 科创 ST | 手工登记 | ±20% | 200 | 1 | T+1 | VERIFIED(secondary) |
| `cn_chinext` 创业板 | `cn300` `cn301` `sz300` `sz301` | ±20% | 100 | 100 | T+1 | VERIFIED(secondary) |
| `cn_st_chinext` 创业 ST | 手工登记 | ±20% | 100 | 100 | T+1 | VERIFIED(secondary) |
| `cn_bj` 北交所 | `cn43` `cn83` `cn87` `cn92` | ±30% | 100 | 100 | T+1 | VERIFIED(secondary) |
| `hk` 港股 | `hk` | 不校验 | 1 | 1 | T+0 | **UNVERIFIED**（每手因标的而异） |
| `us` 美股 | `us` | 不校验 | 1 | 1 | T+0 | **UNVERIFIED**（LULD 未建模） |

### 3.1 强制校验范围

```yaml
enforce_markets: ["cn"]
```

只有 A 股通被强制校验。港股/美股**不做假校验** —— 它们的规则未建模，
`check_order()` 返回 `applied=False` 并在风控里留下
`market_rules: 未强制校验：市场 hk 未纳入...未建模项：[lot_size_per_symbol, price_limit, settlement]`
的痕迹，而不是默默"通过"。

### 3.2 为什么港股不套用"每手 100 股"

港股每手股数由发行人定（400/500/1000/2000 都有），写 100 会让大量委托被错误拒绝或放行。
配置里 `hk.lot_size: 1` + `not_modeled: ["lot_size_per_symbol"]`，明确表示"没做"。

---

## 4. 实现结构

```
src/ai_trader/market/rules.py        规则集（约 470 行）
  ├─ RuleMeta         status / source / effective_from / source_tier / note
  ├─ BoardRule        单板块硬规则
  ├─ TradingCalendar  交易日历（节假日表由配置提供）
  ├─ MarketRuleSet    resolve_symbol / check_order / validate_order
  └─ RuleViolation    code + detail + rule_id
```

### 4.1 校验顺序与错误码

| 顺序 | 检查 | 错误码 |
|---|---|---|
| 1 | 标的能否解析到板块 | `RULE_UNKNOWN` |
| 2 | 板块规则状态是否可用 | `RULE_UNKNOWN` / `RULE_UNVERIFIED` |
| 3 | 交易时段 | `OUT_OF_SESSION` |
| 4 | 交易日历 | `NOT_TRADING_DAY` |
| 5 | 停牌 | `SUSPENDED` |
| 6 | 数量/最小单位/零股 | `LOT_SIZE` |
| 7 | 可卖数量 | `INSUFFICIENT_SELLABLE` |
| 8 | T+1 | `T_PLUS_VIOLATION` |
| 9 | 涨跌停 | `PRICE_LIMIT` / `NO_PRICE_LIMIT_DATA` |

所有检查**独立执行并全部记录**，一次拒绝给出全部原因（和风控模块一致的风格）。

### 4.2 涨跌停计算

```python
def round_price(x, digits=2):
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

up   = round_price(prev_close * (1 + pct))
down = round_price(prev_close * (1 - pct))
```

用 `Decimal` 而不是 `round()`：Python 的 `round(10.005, 2)` 因二进制浮点会得到 `10.0`，
而交易所规则是四舍五入到 `10.01`。

实测：`round_price(9.955) == 9.96`、`round_price(10.005) == 10.01`。

### 4.3 T+1 的判定方式

可卖数量 = `持仓 − 当日已成交买入`。

`today_bought` 由引擎从审计库当日 `filled` 买单汇总（`engine.build_portfolio`），
而不是靠券商字段 —— 因为券商是否返回"可用数量"未验证，自己算更可控。

### 4.4 与风控的关系

规则校验是风控里新增的一组检查（`rule:<CODE>`），位于 `market_enabled` 之后、
数量/仓位/风险检查之前。**规则不过，风控直接拒绝**，不会进入定仓与下单。

规则层异常（例如配置写成非法类型）→ 抛错 → 风控 `fail-closed` 拒绝，
错误码 `market_rules`。

---

## 5. 测试（30 例，全部通过）

```
$ pytest tests/test_market_rules_b4.py -q
..............................                                           [100%]
30 passed
```

| 分组 | 用例 | 关键断言 |
|---|---|---|
| **T+1** | `test_t_plus_one_rejects_same_day_sell` | 今买今卖 → `T_PLUS_VIOLATION`，文案含 "T+1" |
| | `test_t_plus_one_allows_next_day_sell` | 次日 `today_bought=0` → 通过 |
| | `test_t_plus_zero_market_has_no_restriction` | hk 不适用（`applied=False`） |
| **涨跌停** | `test_price_limit_boundary_main_board` | 昨收 10.00 → 11.00/9.00 通过，11.01/8.99 拒绝 |
| | `test_price_limit_rounding_half_up` | 9.955→9.96、10.005→10.01；9.96 通过、9.97 拒绝 |
| | `test_price_limit_missing_prev_close_is_rejected` | 缺昨收 → `NO_PRICE_LIMIT_DATA` |
| **分板块** | `test_board_price_limit_differs`（4 参数） | 10%/20%/20%/30% 各自边界 |
| | `test_st_board_uses_ten_percent_after_20260706` | ST 用 10%（11.00 通过），且 `effective_from=2026-07-06` |
| | `test_star_board_lot_and_increment` | 200 通过、201 通过、150 拒绝 |
| **停牌/日历/时段** | `test_suspended_symbol_rejected` | 买卖双向都拒 |
| | `test_holiday_rejected` | 命中节假日表 |
| | `test_weekend_rejected` | 周六 |
| | `test_out_of_session_rejected` | 午休 12:00 |
| | `test_calendar_unknown_strict_blocks_trading` | 日历 UNKNOWN + strict → 拒 |
| | `test_calendar_unknown_non_strict_allows_weekday` | 非 strict 放行 |
| **数量** | `test_buy_quantity_must_be_multiple_of_lot` | 150 拒、200 通过、0 拒 |
| | `test_sell_odd_lot_must_be_all_at_once` | 零股 150 全卖通过、卖 50 拒绝 |
| **可用数量** | `test_sell_exceeding_position_rejected` / `test_sellable_ignores_today_bought` | 可卖 = 持仓 − 当日买入 |
| **规则不明** | `test_unknown_symbol_rejected` | `xx123456` → `RULE_UNKNOWN` |
| | `test_board_with_unknown_status_rejected` | 板块 UNKNOWN → 拒 |
| | `test_unverified_rule_rejected_by_default` | UNVERIFIED + 默认 → 拒；放开 → 通过 |
| | `test_unverified_rules_listed_for_report` | 能列出全部非 VERIFIED 规则 |
| **风控集成** | `test_risk_rejects_t_plus_one_sell` / `test_risk_allows_next_day_sell` | 检查名 `rule:*` 出现/消失 |
| | `test_risk_rejects_bad_lot` | `rule:LOT_SIZE` |
| | `test_risk_records_unverified_market_note` | hk 留下"未强制校验"痕迹 |

---

## 6. 未验证规则与剩余阻断项

### 6.1 未验证（明确标注）

| 项 | 状态 | 说明 |
|---|---|---|
| 交易所原文 | **未取得** | 涨跌幅数据来自权威媒体对 2026 修订的报道（`secondary`） |
| A 股节假日表 | **UNKNOWN** | 离线无法核实；已留 `calendar.holidays.cn` 配置位 |
| 新股上市前 5 日无涨跌幅 | **未建模** | 需要上市日期，当前无数据源 |
| 退市整理期首日无涨跌幅 | **未建模** | 同上 |
| ST/*ST 判定 | **需人工登记** | 系统无法从代码判断，用 `st_symbols` 显式登记 |
| 科创板盘后固定价格交易 | **未实现** | 2026 新规扩容至全 A 股，本工程未接入 |
| 港股每手股数 / 美股 LULD | **未建模** | 已标 `not_modeled`，不做假校验 |
| 港股通/深股通额度限制 | **未建模** | 需要券商接口 |

### 6.2 阻断项（要开 A 股实盘必须先解决）

| # | 阻断项 | 原因 |
|---|---|---|
| **R1** | 录入 A 股节假日表并把 `calendar.status` 改为 `VERIFIED` | 当前 `UNKNOWN + strict` → A 股完全不可交易 |
| **R2** | 取得交易所规则原文核对涨跌幅 | 当前为二级来源 |
| **R3** | 提供标的的 ST 标记与上市日期 | 否则 ST 股与新股会用错涨跌幅 |
| R4 | 确认券商接口是否已做涨跌停/数量校验 | 重复校验无害，但不能依赖 |
| R5 | A 股通额度与可交易名单 | 未建模 |

### 6.3 如何启用 A 股交易

```yaml
market_rules:
  calendar:
    status: VERIFIED
    strict: true
    source: "上交所/深交所 2026 年休市安排公告"
    effective_from: "2026-01-01"
    holidays:
      cn: ["2026-01-01", "2026-02-16", ...]   # 从公告逐条录入
```

改完跑：

```bash
ai-trader check-env            # 确认 unverified_count 下降、calendar_status=VERIFIED
ai-trader demo                 # 沙盒仍走 hk，不受影响
```

---

## 7. 已知副作用

1. **A 股默认不可交易** —— 这是"规则不明确即拒绝"的直接结果，不是 bug。
2. 风控检查项从 26 增加到 26 + 规则违规动态项（每个违规一条 `rule:<CODE>`），
   Web 的风控决策表可以看到具体是哪条规则不满足。
3. `market_rules.enforce_markets` 只含 `cn`，所以港股/美股的模拟盘行为与之前完全一致
   （全量 307 个测试里没有因为这个改动而失败的）。
