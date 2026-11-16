# 星财富美股第一版（US-P0 ~ US-P3）全量融合审计

- 审计时间：2026-11-16 13:50 – 14:10（GMT+8）
- 项目：`C:\FosunAI-Trading`
- 方式：**严格只读**（源码 + 配置 + 测试执行 + 报告交叉验证）
- 结论：**第一版未全量融合。P0、P1 完成；P2、P3 基本未实现。**

---

## 0. 项目状态

| 项 | 值 |
|---|---|
| Git 仓库 | ❌ **不是 Git 仓库**（`fatal: not a git repository`） |
| 当前 commit / 分支 | **不可得** → 所有"Git 历史/回归"检查 **BLOCKED** |
| 工作区状态 | 不可得 |
| Python | 3.13.14（项目 `.venv`） |
| pytest | 9.2.0（项目 `.venv`） |
| 原始报告 | 5 份**全部存在**（US_MARKET_RULES / US_BROKER_CAPABILITY / INDEPENDENT_AUDIT 105 KB / US_P1_BATCH1_IMPL / US_P1_BATCH1_TEST） |

> 由于无 Git，**无法判断"曾经实现后被覆盖/回退"**。下文所有 REGRESSION 判定一律不适用，改为 BLOCKED。

---

## 1. 完成度总表

| 阶段 | 任务数 | PASS | PARTIAL | IMPLEMENTED/UNVERIFIED | NOT IMPLEMENTED | 完成率 |
|---|---:|---:|---:|---:|---:|---:|
| **US-P0** | 4 | 4 | 0 | 0 | 0 | **100%** |
| **US-P1** | 3 | 3 | 0 | 0 | 0 | **100%** |
| **US-P2** | 5 | 0 | 1 | 0 | 4 | **0%** |
| **US-P3** | 1 | 0 | 0 | 0 | 1 | **0%** |
| **合计** | **13** | **7** | **1** | 0 | **5** | **53.8%** |

> 按任务项统计。P2 的 US-T9 记 PARTIAL（有条件），故 PASS 率 7/13，含 PARTIAL 的"有落地"率 8/13。

---

## 2. US-P0：美股基础规则（4/4 PASS）

### US-T1 / US-T2 时区、DST、交易日 —— **PASS**

| 证据 | 内容 |
|---|---|
| 源码 | `src/ai_trader/market/clock.py:27` `from zoneinfo import ZoneInfo`；`:31` `"us": "America/New_York"` |
| 时段定义 | `clock.py:53-56` `DEFAULT_SESSION_SPEC["us"]`：`tz=America/New_York`，regular `09:30-16:00` |
| DST | 由 `zoneinfo` 自动处理（注释明确"DST/冬令时由 zoneinfo 自动处理"） |
| 测试 | `tests/test_us_p0.py`：`test_dst_regular_open_boundary`、`test_standard_time_regular_open_boundary`、`test_dst_transition_week_does_not_drift`、`test_beijing_saturday_is_new_york_friday` |
| 结果 | ✅ 实跑通过 |

**残留说明**：`clock.local()` 对 naive datetime 按**本机时区**解释（`dt.astimezone()`），文档已声明"调用方几乎都传本机时间"。属设计约定，非缺陷，但意味着 naive 入参的语义依赖部署机时区。

### US-T3 强制美股市场规则 —— **PASS**

| 证据 | 内容 |
|---|---|
| 配置 | `config/config.yaml:190` `enforce_markets: ["cn", "us"]` |
| 设计意图 | `US_P0_IMPLEMENTATION_REPORT.md:27` 明确记录"`enforce_markets: ["cn","us"]`；原为 `["cn"]`" —— **`cn` 是为 A 股历史回归保留，非遗漏** |
| 守卫 | `config.yaml:193` `required_markets: ["us"]`；`guards.py:113-117` 启动守卫按 `enforce_markets` 判定，未达标报 `G_MARKET_NOT_ENFORCED` |
| 规则层 | `market/rules.py:298` `self.enforce_markets = {...}`；`:713` `is_enforced()`；`:718` 未强制市场 `applied=False` 且显式记 `not_modeled`，**不再等价放行** |
| 未强制策略 | `config.yaml:195-198` `unenforced_market_policy: reject`（fail-closed） |
| 调用链 | `risk/controller.py` → `rules.check()` → `session_status()`（见 §4） |

### US-G1 全时段持仓查询 —— **PASS**

| 证据 | 内容 |
|---|---|
| 接口约定 | `broker/base.py:229-236` `get_positions(**kwargs)`，文档明确"任何时段的持仓都必须返回——漏掉一段就等于漏掉一段风险敞口（US-P0-3）" |
| 开关来源 | `broker/fosun_adapter.py:1456-1474` `_position_session_flags()`：默认 **三项全 True**；配置 `fosun.position_sessions` 可覆盖，**但不能关成漏风险，health() 会报出来** |
| 实际传递 | `fosun_adapter.py:1492-1494` 把 `use_us_pre/use_us_post/use_us_night` 传给 `client.portfolio.get_holdings` |
| 失败安全 | `fosun_adapter.py:1500+` 结构不符/缺字段一律抛 `BrokerError`，**绝不返回 `[]`**（避免上层 fail-open） |
| Paper | `broker/paper.py:283` 明确忽略 `use_us_*`（模拟盘无时段概念，接口对齐） |
| 测试 | `test_us_p0.py:test_all_session_positions_enter_risk_control`、`test_mixed_session_positions_all_counted` |

### US-P0 风险退出保护 —— **PASS**

| 要求 | 实现 | 位置 |
|---|---|---|
| 风险退出不被时段阻断 | `intent.is_exit` 时跳过时段检查 | `risk/controller.py:547` `if self.enforce_market_hours and not intent.is_exit` |
| 规则层豁免 | `rules.check(..., is_exit=True)` → `session_status(..., extended=True)`，盘前/盘后/夜盘均算可交易 | `rules.py:607-609`、`rules.py:518` |
| 最小金额不拦清仓 | `is_exit` 时 `min_notional` 直接放行 | `risk/controller.py:555-558`（"US-P1 N-2"） |
| 日历/数据异常 | 持仓查询异常"阻断新开仓但仍允许止损" | `test_us_p0.py:test_positions_query_exception_still_allows_stop_loss` |
| 行情失败退化 | 退回券商持仓价做止损判断 | `test_us_p0.py:test_quote_failure_uses_position_mark_for_stop` |
| 会话/时钟异常 | fail-closed | `test_session_service_exception_is_fail_closed`、`test_clock_data_missing_is_fail_closed` |
| 与开仓分离 | `is_exit` / `is_reduce_only` 独立字段 + `_reduce_only_gate` 四条件硬约束 | `risk/controller.py:118`、`:794-831` |

> `_reduce_only_gate` 明确标注"官方 SDK 没有 reduce-only 原语，属客户端约束，已在报告注明部分未解决"——**诚实的已知限制**，不是隐藏缺陷。

---

## 3. US-P1：市场规则完善（3/3 PASS）

### US-T5 节假日与提前收市 —— **PASS**

| 证据 | 内容 |
|---|---|
| 数据结构 | `rules.py:200` `holidays: set`、`:202` `early_close: Dict[date, Any]` |
| 配置数据 | `config.yaml:592-611` **2026 年 10 个 NYSE 休市日**（元旦/MLK/华盛顿诞辰/耶稣受难日/阵亡将士/六月节/独立日(observed)/劳动节/感恩节/圣诞） |
| 提前收市 | `config.yaml:614-618` `2026-11-27 → 13:00`、`2026-12-24 → 13:00` |
| 覆盖期 | `covered_from 2026-01-01` / `covered_until 2026-12-31` |
| 判定 | `rules.py:268` `if e.holidays and day in e.holidays` → 拒绝 |
| 测试 | `test_us_p0.py:test_us_holiday_and_calendar_coverage`、`test_us_early_close_day`；`test_market_rules_b4.py:test_holiday_rejected` |

> 设计上"**不内置任何节假日表**"（`rules.py:226`），全部由配置提供——好处是可审计，代价是每年必须人工更新。

### US-T4 Tick Size —— **PASS**

| 证据 | 内容 |
|---|---|
| 配置 | `config.yaml:490-494`：`tick_size: 0.01`、`tick_size_below_1: 0.0001`、`tick_size_half_cent: []` |
| 法源 | 注释引用 **17 CFR 242.612**（Rule 612 最小报价单位） |
| 实现 | `rules.py:126-149` `tick_size` / `tick_size_below_1` / `tick_size_half_cent` + `tick_for(price)`：`< $1` 用 0.0001，否则 0.01 |
| 已声明缺口 | 逐股 TWAS ≤ $0.015 时的 $0.005 档"需数据源，**未启用**"（注释写明） |
| 测试 | `test_market_rules_b4.py:test_price_limit_rounding_half_up` 等价格精度用例 |

### US-T6 三段交易时段 —— **PASS**

| 证据 | 内容 |
|---|---|
| 定义 | `clock.py:53-60`：`pre 04:00-09:30`、`regular 09:30-16:00`、`post 16:00-20:00`、`night 20:00-04:00` |
| 阶段常量 | `clock.py:45` `TRADABLE_PHASES = (PRE, REGULAR, POST, NIGHT)` |
| 边界判定 | `clock.py:255-258` 盘前分支（"早于第一个常规窗口开盘"） |
| 提前收市联动 | `clock.py:14` 注释 + `rules.py` 用 `early_close` 覆盖收盘 |
| 测试 | `test_us_p0.py:test_session_phases_are_recognised`、`test_dst_regular_open_boundary` |

---

## 4. 真实业务调用链（P0/P1 的融合证据）

```
config/config.yaml
  └─ market_rules.enforce_markets: ["cn","us"] / required_markets: ["us"] / calendar / tick
      ↓ 加载
config.py  →  rules.py: RulesEngine(config)   enforce_markets / holidays / early_close / tick
      ↓
risk/controller.py:547   if enforce_market_hours and not intent.is_exit:
                              rules.session_status(market, now)          ← 时段
risk/controller.py:516   rules.check(..., is_exit=bool(intent.is_exit))  ← 规则（含 tick/日历/价格带）
                              ↓ rules.py:607  session_status(extended=is_exit)
strategy/engine.py → OrderIntent(is_exit=...) → broker.place_order(...)
      ↓
broker/fosun_adapter.py  place_order → 官方 SDK
broker/fosun_adapter.py:1492  get_holdings(use_us_pre/post/night)
```

**判定**：P0/P1 的每一项都**落在真实调用链上**，不是孤立函数。

---

## 5. 自动化测试

| 测试文件 | 用例数 | 覆盖 | 结果 |
|---|---:|---|---|
| `tests/test_us_p0.py` | 34 | 时区/DST/时段/节假日/早收市/持仓 fail-closed/退出保护 | ✅ 通过 |
| `tests/test_us_p1_batch1.py` | 41 | P1 第一批 | ✅ 通过 |
| `tests/test_us_p1_batch2.py` | 36 | P1 第二批 | ✅ 通过 |
| `tests/test_market_rules_b4.py` | 29 | A 股规则回归（含 tick/日历/时段） | ✅ 通过 |
| **合计** | **147** | — | **全部通过（0 失败）** |

执行命令（只读、无外部连接）：

```
.venv\Scripts\python.exe -m pytest tests/test_us_p0.py tests/test_us_p1_batch1.py \
    tests/test_us_p1_batch2.py tests/test_market_rules_b4.py -q
→ 147 passed
```

---

## 6. US-P2：交易能力（0/5 PASS）

### G6 盘前行情 —— **NOT IMPLEMENTED**

- `market/feed.py` **没有**盘前/时段感知的行情获取（只有 websocket 的 `X-Session` 头，与盘前行情无关）。
- 系统有"盘前时段"的概念（clock），但**没有"盘前行情数据质量/时间戳/策略使用"**。
- 证据：`grep -i "premarket|盘前" src/ai_trader/market/*.py` 仅命中 clock.py 的时段窗口注释。

### G4 / G5 订单类型与 TIF —— **NOT IMPLEMENTED**

| 项 | 实情 |
|---|---|
| 订单类型 | `broker/base.py:158` `order_type: str = "limit"  # limit \| market` —— **只有两种** |
| TIF | `broker/base.py:160` `tif: str = "day"` —— 字段存在，但 **`grep "\.tif"` 全项目 0 命中**，从未下传给 broker |
| 自我声明 | `config.yaml:496` `not_modeled: [..., "order_types", "pdt"]`；`:508` 注释"未建模：…订单类型/TIF…**这些不在本系统里做假校验**" |

→ **字段是死的，能力不存在。** 判定 NOT IMPLEMENTED（不是 PARTIAL，因为连调用都没接）。

### US-T7 熔断与停牌 —— **NOT IMPLEMENTED**

- 全项目搜 `halt` 只命中 **kill-switch（急停哨兵）**：`safety/halt.py`、`cli.py:83` `risk.halt()`、`audit/faults.py:41 halt_storage_failed`。
- **那不是市场停牌**，是系统自己的紧急停止开关。
- 市场侧：`config.yaml:496` `not_modeled` 含 `"mwcb"`（熔断）；无 LULD 价格带、无交易暂停识别、无恢复机制。
- `rules.py` 有 `suspended`（停牌）判定，但那是 **A 股历史逻辑**（`test_market_rules_b4.py:test_suspended_symbol_rejected`），未接入美股行情/策略链。

### US-T8 卖空 —— **NOT IMPLEMENTED**

- `fosun_adapter.py` 中 `short` 的命中全部是**凭据字段的短名**（`_SDK_CRED_FIELDS` 的 `short` 变量），与卖空无关。
- 无 shortable 查询、无可借券数量、无 SSR（`not_modeled: "short_sale_ssr"`）。
- 反向证据：`risk/controller.py` 第 7 项检查是"**卖出必须有持仓（禁止裸卖空）**" —— 系统明确**不支持**卖空。

### US-T9 结算资金 —— **PARTIAL**

| 项 | 实情 |
|---|---|
| 购买力 | **有**：`broker/base.py:142 buying_power`；`fosun_adapter.py:1451` 从 `buyingPower / maxBuyQty / availableFund` 读取；`paper.py:276` 用现金 |
| T+1 结算建模 | **无**：`config.yaml:496 not_modeled` 含 `"settlement_t1_funds"`，注释明确"T+1 资金可用性…不在本系统里做假校验" |
| 交易侧 T+N | `config.yaml:485 t_plus: 0`（美股当日可卖，这是对的） |

→ 购买力**直接采信券商数字**，系统自身不做未结算资金扣减。作为风控依赖外部数值，可接受但未建模 → **PARTIAL**。

---

## 7. US-P3：LULD（0/1 PASS）

### US-T12 Limit Up-Limit Down —— **NOT IMPLEMENTED**

- 全项目（`src` + `tests`）搜 `luld|limit_up_limit_down|价格带|price_band`：**唯一命中是一句注释**——`market/rules.py:13`：

  > "港股/美股的做市、LULD 等未建模，显式标注 `not_modeled`，不做假校验。"

- `config.yaml:496` `not_modeled` 含 `"luld"`；`:508` 注释"未建模：**LULD 价格带**、MWCB 熔断、SSR…"
- **没有**价格限制状态识别、没有波动暂停、没有行情/策略/订单/风控接入、没有边界测试。

→ 纯粹的 NOT IMPLEMENTED，**连预留接口都没有**（只有一个声明式的 `not_modeled` 字符串）。

---

## 8. 逐项核对总表

| 编号 | 任务 | 状态 | 关键证据 |
|---|---|---|---|
| US-T1/T2 | 时区 / DST / 交易日 | **PASS** | `clock.py:27,31,53`；`test_us_p0.py` DST 4 例 |
| US-T3 | 强制美股规则 | **PASS** | `config.yaml:190,193`；`rules.py:298,713`；`guards.py:113` |
| US-G1 | 全时段持仓 | **PASS** | `fosun_adapter.py:1456-1494`；fail-closed 1500+ |
| P0-风险退出 | 退出不被阻断 | **PASS** | `controller.py:547,555`；`rules.py:607`；4 个专项测试 |
| US-T5 | 节假日 / 提前收市 | **PASS** | `config.yaml:592-618`；`rules.py:200-268` |
| US-T4 | Tick Size | **PASS** | `config.yaml:490-494`；`rules.py:126-149` |
| US-T6 | 三段交易时段 | **PASS** | `clock.py:45,53-60,255` |
| G6 | 盘前行情 | **NOT IMPLEMENTED** | `feed.py` 无时段感知行情 |
| G4/G5 | 订单类型 / TIF | **NOT IMPLEMENTED** | `base.py:158,160`；tif 从未下传 |
| US-T7 | 熔断 / 停牌 | **NOT IMPLEMENTED** | 只有 kill-switch；`not_modeled: mwcb` |
| US-T8 | 卖空 | **NOT IMPLEMENTED** | 明令禁止裸卖空；`not_modeled: short_sale_ssr` |
| US-T9 | 结算资金 | **PARTIAL** | `buying_power` 采信券商；`not_modeled: settlement_t1_funds` |
| US-T12 | LULD | **NOT IMPLEMENTED** | 仅一行注释声明未建模 |

---

## 9. 系统与第一版原始设计的差异

项目的选择是**"不假装建模"**：把所有做不了的项集中写进 `config.yaml` 的 `not_modeled`：

```yaml
not_modeled: ["luld", "settlement_t1_funds", "mwcb", "short_sale_ssr", "order_types", "pdt"]
```

并配注："**这些不在本系统里做假校验**"。

这是**诚实的工程选择**，但它也意味着：**第一版计划的 P2/P3 从一开始就被划到范围外**，而不是"做了没做完"。所以"未融合"的本质是**范围裁剪**，不是实现遗漏。

---

## 10. 是否"全量融合"—— 直接回答

| 问题 | 回答 |
|---|---|
| 1. 第一版是否已全部融合 | **否**。13 项中 7 项 PASS（全部集中在 P0/P1），5 项完全未实现，1 项部分 |
| 2. 哪些真正 PASS | US-T1/T2、US-T3、US-G1、P0 风险退出、US-T5、US-T4、US-T6 —— **共 7 项，全在 P0/P1** |
| 3. 哪些只是部分实现 | US-T9 结算资金（购买力采信券商，T+1 未建模） |
| 4. 哪些完全没实现 | G6 盘前行情、G4/G5 订单类型与 TIF、US-T7 熔断停牌、US-T8 卖空、US-T12 LULD |
| 5. 哪些存在回归 | **无法判断**（BLOCKED：目录不是 Git 仓库，无提交历史可比） |
| 6. 报告路径 | `C:\FosunAI-Trading\US_FIRST_VERSION_FULL_INTEGRATION_AUDIT.md` |

**一句话**：**P0 和 P1 是真融合（代码+配置+调用链+147 个测试全过）；P2 和 P3 基本是画了范围线没做。**

---

## 11. 未验证 / BLOCKED

| 项 | 原因 |
|---|---|
| 实现是否被回退/覆盖 | **BLOCKED** —— 非 Git 仓库，无历史 |
| 真实券商侧行为 | 未连接真实账户（按规则跳过） |
| `tif` 是否被官方 SDK 隐式默认 | 未验证（字段未下传，券商侧行为不可知） |
| `buying_power` 在 T+1 未结算时的实际取值 | 需真实账户 |

---

## 12. 证据索引

| 主题 | 位置 |
|---|---|
| 时区/DST | `src/ai_trader/market/clock.py:27,31,53-60,255` |
| 规则引擎 | `src/ai_trader/market/rules.py:126-149,200-268,298,511,572-627,713` |
| 市场强制守卫 | `src/ai_trader/guards.py:113-117` |
| 配置 | `config/config.yaml:179-198,480-512,585-620` |
| 风险控制 | `src/ai_trader/risk/controller.py:118,472-560,794-831,1062` |
| 券商适配 | `src/ai_trader/broker/fosun_adapter.py:1456-1500` |
| 订单结构 | `src/ai_trader/broker/base.py:142,158,160,229-236` |
| 测试 | `tests/test_us_p0.py`(34)、`test_us_p1_batch1.py`(41)、`test_us_p1_batch2.py`(36)、`test_market_rules_b4.py`(29) |

---

*本审计全程只读：未修改源码/配置/数据库，未安装依赖，未连接真实券商，未发送订单。*
