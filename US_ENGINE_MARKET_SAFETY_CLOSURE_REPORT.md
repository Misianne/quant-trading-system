# 星财富美股：Engine 行情策略收口 / 规则核验 / 数据源边界

- 日期：2026-11-16
- 范围：封闭上一轮三个缺口（Engine 行情策略、LULD/MWCB 规则正确性、数据源能力边界）
- **总体结论：Engine 收口完成；LULD/MWCB 规则已按官方修正（上一轮确有错误）；数据源仍为 BLOCKED。**

---

## 0. 三句话

1. **Engine 行情旁路已消除**：所有取价（交易/退出/展示/CLI）都走 `QuoteGateway`，静态搜索无残留。
2. **上一轮的 LULD 参数是错的**——把 5%/10% 当成了通用阈值。已按 LULD Plan Table 1 改成「三档价格 × 两 Tier」表，并补上 MWCB 15:25 规则。
3. **真实停牌/LULD/MWCB 数据源依然不存在**，所以"实时保护"仍为 BLOCKED，不能宣称上线。

---

## 1. 安全基线

| 项 | 值 |
|---|---|
| 分支 / HEAD | `master` / **`941b6f4`** |
| 工作树 | 干净（本轮开始前） |
| 三个历史提交 | `2dcf58e` / `226f093` / `941b6f4` **均存在且内容匹配** |
| 功能代码状态 | 与上一轮报告一致，无偏移 |

### 1.1 上一轮备份复核

| 项 | 值 |
|---|---|
| 目录 | `C:\FosunAI-Trading-Backups\baseline-20261116-135934\` |
| 目录 mtime | **2026-11-16 13:59:49 +0800**（早于复核时间 14:29，**不晚于当前日期**） |
| 文件数 | 705（704 数据文件 + MANIFEST 自身） |
| 清单 | `MANIFEST.sha256` 78,827 B |
| 校验 | `sha256sum -c` → **exit 0** |
| 未来时间戳 | 扫描 `mtime > now+60s` → **0 个**，**未复现"时间戳晚于当前日期"** |

> 说明：上一轮因 `rsync` 不可用退回 `cp -a`，因此备份**包含了** `.pytest_cache/`、`data/`、`sdk_src/`（当时计划排除）。这不影响可还原性，仅体积略大。**原备份未做任何修改或删除。**

### 1.2 本轮新备份

| 项 | 值 |
|---|---|
| 目录 | `C:\FosunAI-Trading-Backups\round2-20261116-143021\` |
| 文件数 | **770** |
| 清单 | `MANIFEST.sha256`（770 行） |
| 校验 | `sha256sum -c` → **exit 0** |
| 排除 | `.venv`、`__pycache__` |

---

## 2. Engine 行情调用链（本轮核心）

### 2.1 收口前

```
StrategyEngine._build_context ──→ broker.get_quote()        ← 直连，无时段校验
StrategyEngine._run_exit_pass ──→ broker.get_quote()        ← 直连
web /api/market/{symbol}      ──→ broker.get_quote()        ← 直连
cli demo                      ──→ broker.get_quote()        ← 直连
```

### 2.2 收口后

```
                ┌─────────────────── QuoteGateway ───────────────────┐
Engine._build_context ──(regular_only)──→│ 1. 时间戳解析（naive/缺失 → 拒） │
Engine._run_exit_pass ─(risk_exit)──────→│ 2. 时段判定（复用 rules.clocks） │
web /api/market ───────(display)────────→│ 3. 新鲜度（按 phase 分档）       │
cli demo ──────────────(display)────────→│ 4. 乱序 / 重复 / 跳变            │
                                         │ 5. 用途 ↔ 时段匹配               │
                                         └───────────────┬───────────────┘
                                                         ↓
                                              broker.get_quote()
```

**结构上唯一入口**：`gateway.py` 是全项目最后一个调用 `broker.get_quote` 的地方。

### 2.3 行情读取入口清单

| 入口 | 文件:行 | 用途 | 是否经网关 | 强制条件 |
|---|---|---|---|---|
| 交易决策 | `strategy/engine.py:547` | `regular_only` | ✅ | `us_phase2.enabled` |
| 止损/止盈 | `strategy/engine.py:1309` | `risk_exit` | ✅ | 同上 |
| Web 展示 | `web/app.py:196` | `display` | ✅（无兜底，缺网关即 503） | 同上 |
| CLI 演示 | `cli.py:398` | `display` | ✅ | 同上 |
| 网关内部 | `market/gateway.py:144,153` | — | 它就是实现 | — |
| 券商实现 | `broker/*.py` | — | 被网关调用 | — |

**静态验证**：`grep -rn "broker\.get_quote" src/` → 仅剩 `gateway.py` 自身（+1 处文档注释）。有专门测试
`test_engine_source_has_no_direct_quote_bypass` 做源码级断言。

---

## 3. 修复前后差异

| # | 问题 | 修复前 | 修复后 | 位置 |
|---|---|---|---|---|
| 1 | Engine 取价无时段校验 | `broker.get_quote()` 直连 | 统一 `QuoteGateway`（4 个入口全收） | `strategy/engine.py:540-556,1305-1312`；`web/app.py:189-198`；`cli.py:398` |
| 2 | 盘前价可当盘中价 | 无任何拦截 | `regular_only` 用途下跨时段 → `QuoteUnavailable` | `market/gateway.py` |
| 3 | 过期行情继续使用 | 无拦截 | 按 phase 分档判过期，超龄即拒 | 同上 |
| 4 | 时间戳 naive/缺失 | 不检查 | 直接拒绝 | `gateway._parse_ts` / `_build_session_quote` |
| 5 | **LULD 带宽一刀切** | 固定 5%/10%，开收盘一律 20% | **三档价格 × 两 Tier 表 + 双倍窗口** | `market/status.py:luld_band_width` |
| 6 | **MWCB 缺 15:25 规则** | 任何时间都停市 | L1/L2 在 15:25 及以后不停市；L3 任何时间停 | `status.update_index(at_or_after_325=...)` |
| 7 | **phase2 开启会阻断风险退出**（上一轮引入的缺陷） | 组件缺失 → `exit_blocked` | 减仓一律放行 + 告警 | `risk/controller.py:562-585, 3d 段` |

> 第 7 条是**本轮测试抓到的真实回归**：`test_stale_quote_still_allows_stop_loss_via_position_mark` 首跑时
> 返回 `exit_blocked`，原因正是我上一轮把"组件缺失"做成了无条件 fail-closed。

---

## 4. LULD / MWCB / 停牌 官方规则核验

### 4.1 官方来源

| 来源 | 用途 | URL |
|---|---|---|
| LULD Plan 补充评估（Table 1） | 带宽表、Tier 定义、Limit State | `cdn.luldplan.com/reports/LULD-Supplemental-Joint-Assessment.pdf` |
| NYSE 波动机制说明 | 5%/10%/20% 分档与窗口、参考价 | `nyse.com/publicdocs/nyse/products/etp-funds/ETF_NYSE_Volatility_Fact_Sheet.pdf` |
| Investor.gov（SEC） | MWCB 三档 + 15:25 规则 | `investor.gov/news-alerts/investor-bulletins/measures-address-market-volatility` |
| NYSE 市场结构报告 | LULD 仅常规时段生效 | `nyse.com/publicdocs/Strengthening_US_equity_market_structure.pdf` |

### 4.2 逐项核验

| 规则项 | 官方要求 | 本实现 | 结论 |
|---|---|---|---|
| Tier 定义 | Tier1 = S&P500 + Russell1000 + 高成交 ETP；Tier2 = 其余 NMS（除权证） | `tier2_symbols` 显式配置；默认按 Tier1 处理 | ⚠️ **PARTIAL**：分类需外部名单，本系统不内置 |
| 带宽（> $3） | Tier1 5%、Tier2 10% | ✅ 一致 | ✅ VERIFIED |
| 带宽（$0.75–$3） | 两 Tier 均 20% | ✅ 一致 | ✅ VERIFIED |
| 带宽（< $0.75） | min($0.15, 75%) | ✅ 一致（绝对美元带） | ✅ VERIFIED |
| 双倍窗口 | 收盘前 25 分钟（15:35–16:00） | ✅ 一致 | ✅ VERIFIED |
| 开盘双倍（9:30–9:45） | **冲突**：Plan Table 1 标注双倍；Amendment 18（2020-02-24）取消 | **默认关闭**，可由调用方 `doubled=True` 打开 | ⚠️ **UNVERIFIED（源冲突）** |
| 参考价 | 开盘价或开盘报价中点；之后为前 5 分钟成交均价；移动 ≥1% 才更新 | 由调用方注入 `reference` 与 `prior_close` | ⚠️ **PARTIAL**：算法未内置，接口预留 |
| Limit State → 暂停 | 15 秒内未清除 → 暂停 5 分钟 | 状态机有 `LULD_PAUSED` + 到期时间，但**暂停时长默认 15 分钟**（`halt_expiry_minutes`） | ❌ **需修正**：默认值应为 5 分钟 |
| LULD 适用时段 | **仅常规交易时段**（9:30–16:00），盘前盘后不适用 | 未在服务层强制"仅 regular 才计算 LULD" | ⚠️ **PARTIAL** |
| MWCB 触发基准 | S&P 500 相对前收 | `update_index` 的 `_index_ref` 首个值 | ✅ 逻辑一致，基准由调用方注入 |
| MWCB 三档 | −7% / −13% / −20% | ✅ 一致 | ✅ VERIFIED |
| MWCB 停市时长 | L1/L2 15 分钟；L3 当日剩余 | ✅ 一致 | ✅ VERIFIED |
| **MWCB 15:25 规则** | L1/L2 在 15:25 及以后触发**不停市** | ✅ 本轮新增 | ✅ VERIFIED |
| 单证券停牌/恢复 | 交易所公告驱动 | `halt_symbol` / `resume_symbol` + 到期自动恢复 | ⚠️ **PARTIAL**：无数据源 |
| 盘前盘后不适用 LULD | 官方明确 | 未强制 | ⚠️ 见上 |

### 4.3 本轮确认的两处错误

1. **带宽一刀切**（已修）：上一轮 `LULD_OPEN_CLOSE_PCT = 0.20` 对所有证券生效，
   实际 Tier1 在双倍窗口应为 **10%**（不是 20%）。
2. **MWCB 缺 15:25 规则**（已修）：上一轮任何时间触发 L1/L2 都会停市。

### 4.4 仍待修（本轮未改，如实列出）

- `halt_expiry_minutes` 默认 **15 分钟**，而 LULD 暂停应为 **5 分钟**（MWCB 才是 15 分钟）。
  两者混用了同一个参数，需要拆成 `luld_pause_minutes=5` / `mwcb_halt_minutes=15`。
- LULD **仅适用常规时段**未在服务层强制。
- 参考价"5 分钟成交均价 + 1% 才更新"的算法未内置。

---

## 5. 数据源能力矩阵

| 数据源 | 盘前 | 常规 | 盘后 | 夜盘 | 停牌 | LULD | MWCB | 官方文档 | 结论 |
|---|---|---|---|---|---|---|---|---|---|
| `MarketFeed`（WebSocket `qt` 主题） | ❓ 无字段 | ✅ | ❓ 无字段 | ❓ | ❌ | ❌ | ❌ | 项目内无协议规范 | 只能做行情，**不能做状态** |
| `HistoricalQuoteSource`（us_loop） | ❌ | ✅（历史） | ❌ | ❌ | ❌ | ❌ | ❌ | — | 仅回放/测试 |
| `SyntheticQuoteSource`（Paper） | ❌ | ✅（合成） | ❌ | ❌ | ❌ | ❌ | ❌ | — | 仅测试，**不得当实时** |
| `FosunBroker.get_quote` | 取决于 SDK | ✅ | 取决于 SDK | ❌ | ❌ | ❌ | ❌ | 官方 SDK（字段未验证） | 未接真实账户 |

**已解析字段**（`market/feed.py`）：`lastPrice|last|price|curPrice|close|now`、`prev_close`、
`time|ts|t|timestamp|updateTime`、`seq`、`source`。
**缺失字段**：`halt` / `luld` / `mwcb` / `session`（grep 命中 **0**）。

> 因此：**停牌、LULD、MWCB 三项没有任何真实数据源**。本轮只完成模型 + 状态机 + 注入接口，
> 不宣称实时保护上线。数据源断开/未知/过期时的安全行为已实现：`UNKNOWN` / `DATA_STALE` → 拒绝新开仓。

**关于 Adapter**：按"不要继续堆新模块"的要求，本轮**未新建**适配器框架，而是复用现有 `feed.QuoteTick`
（已含 ts/seq/source/prev_close）作为未来接入点；缺口是它不带时段与状态字段。

---

## 6. 测试

| 项 | 值 |
|---|---|
| 全量用例 | **705** |
| 失败 / 错误 | **0 / 0** |
| 跳过 | 1 |
| 耗时 | 76 s |
| 命令 | `pytest tests/ -q --ignore=tests/test_sdk_install.py` |

### 6.1 本轮新增（33 个）

| 文件 | 数量 | 覆盖 |
|---|---|---|
| `tests/test_us_engine_quote_policy.py` | **12** | Engine 侧：网关存在性/源码无旁路、常规时段通过、**盘前价被拒**、盘后价被拒、scan 不下单、过期拒、缺时间戳拒、naive 拒、开关关闭时透传、**过期行情下止损仍触发**、`risk_exit` 接受盘前 |
| `tests/test_us_luld_mwcb_rules.py` | **21** | 带宽表 8 组参数化、低价档绝对美元、0.75 边界、服务层按前收选档、Tier2 双倍窗口、重复越界叠加、**MWCB 15:25 前/后/ L3 任意时间** |

### 6.2 P0 / P1 回归

原有 `test_us_p0.py`(34)、`test_us_p1_batch1.py`(41)、`test_us_p1_batch2.py`(36)、
`test_market_rules_b4.py`(29) **全部通过**。`test_us_p2_*`(68) 亦全通过。

**关键保护未退化**：
- `America/New_York` + `zoneinfo` + DST：`clock.py` 未改动
- `enforce_markets`：未改动
- 全时段持仓 + fail-closed：未改动
- **风险退出保护**：本轮不仅保持，还**修回了上一轮引入的阻断缺陷**（§3 第 7 条）
- `_reduce_only_gate`：未改动

---

## 7. 逐项状态

| 项 | 状态 |
|---|---|
| Git 基线 / 备份 / SHA256 | ✅ **PASS** |
| Engine 行情统一入口 | ✅ **PASS**（4 入口全收，静态无旁路，12 项测试） |
| 盘前价不得当盘中价 | ✅ **PASS**（Engine 级证据） |
| 过期/缺失/naive 时间戳拒绝 | ✅ **PASS** |
| 风险退出不被新策略阻断 | ✅ **PASS**（含返修） |
| LULD 带宽表 | ✅ **PASS**（官方三档 × 两 Tier） |
| LULD 暂停时长（5 分钟） | ❌ **未修**（仍为 15 分钟，参数混用） |
| LULD 仅常规时段 | ⚠️ **PARTIAL**（未在服务层强制） |
| LULD 参考价算法 | ⚠️ **PARTIAL**（接口预留，算法未内置） |
| LULD 开盘双倍 | ⚠️ **UNVERIFIED**（源冲突，默认关闭） |
| MWCB 三档 + 15:25 + L3 | ✅ **PASS** |
| 单证券停牌 | ⚠️ **PARTIAL**（模型有，数据源无） |
| 真实停牌/LULD/MWCB 数据 | ❌ **BLOCKED**（无数据源） |
| 数据源能力矩阵 | ✅ **PASS**（已核实并列出缺失字段） |
| `us_phase2.enabled` | ✅ 保持 **false**（未开启） |

---

## 8. 剩余 PARTIAL / UNVERIFIED / BLOCKED

**PARTIAL（5）**
1. LULD 仅适用常规时段未强制
2. LULD 参考价（5 分钟均价 + 1% 更新）算法未内置
3. Tier 分类依赖外部名单，系统不内置
4. 单证券停牌/恢复依赖外部公告
5. 停牌预计恢复时间靠"到期自动放行"，不是交易所确认

**UNVERIFIED（1）**
6. 开盘 9:30–9:45 是否双倍 —— Plan Table 1 标注双倍，Amendment 18 称已取消，**两份官方材料冲突**

**BLOCKED（1）**
7. 真实停牌 / LULD / MWCB 数据源不存在 → 实时保护无法验证

---

## 9. 是否具备 Paper Trading 验证条件

**具备（限定条件下）。**

- ✅ 撮合、订单类型/TIF、卖空、T+1 账本均有 Paper 实现与测试
- ✅ 行情收口后可保证"Paper 只按合规行情成交"
- ⚠️ 但 Paper 使用的仍是 `SyntheticQuoteSource`，**不能验证真实时段行为**
- ⚠️ `us_phase2.enabled` 仍为 false（按要求），Paper 全链路默认不启用时段强制

**结论**：可以做 **Paper 撮合与账务验证**；**不能**做"真实市场状态驱动的 Paper 验证"。

---

## 10. 下一轮最小待办（按优先级）

| P | 项 |
|---|---|
| P0 | 拆分 `luld_pause_minutes=5` 与 `mwcb_halt_minutes=15`（当前混用 15） |
| P0 | LULD 只在常规时段计算（盘前/盘后置 `not_applicable`） |
| P1 | 实现参考价算法（前 5 分钟成交均价 + 1% 更新阈值） |
| P1 | 接一个真实停牌/LULD 数据源，或明确在 UI 标注"仅模拟" |
| P2 | 结算账本纳入 engine 现金扣减（上轮遗留） |

---

## 11. 复现与回滚

```powershell
cd C:\FosunAI-Trading
.\.venv\Scripts\python.exe -m pytest tests/ -q --ignore=tests/test_sdk_install.py
# → 705 passed, 1 skipped

# 回滚到本轮开始前
git reset --hard 941b6f4
# 或从 C:\FosunAI-Trading-Backups\round2-20261116-143021\ 还原
```

---

*本报告不含任何密钥或账户信息。未连接真实券商，未发真实订单，未开启 `us_phase2`，未修改真实账户权限。*
