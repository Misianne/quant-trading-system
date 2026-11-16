# 星财富美股第一版 P2/P3 实施报告

- 日期：2026-11-16
- 范围：在保留 P0/P1 的前提下，实现并接入 盘前行情 / 订单类型与 TIF / 停牌熔断 LULD / 卖空 SSR / T+1 结算
- **总体结论：能力已落地并接入真实调用链；但真实数据源与真实券商能力均未验证，P3 的 LULD 仅到"规则模型 + 模拟"层。**

---

## 0. 一句话结论

> **代码不是"写完"，而是接通了风控与撮合两条真实链路，并被 672 条测试覆盖。**
> 但**没有任何一条数据来自真实交易所或真实券商** —— 这一点在下文"未完成"里逐条列明，不做粉饰。

---

## 1. 安全基线

### 1.1 备份（项目外，可回滚）

| 项 | 值 |
|---|---|
| 备份目录 | `C:\FosunAI-Trading-Backups\baseline-20261116-135934\` |
| 文件数 | **704** |
| 体积 | 11 MB（排除 `.venv` / `__pycache__` / `.pytest_cache`） |
| 清单 | `MANIFEST.sha256`（704 行，78 KB） |
| 校验 | `sha256sum -c MANIFEST.sha256` → **exit 0，全部通过** |

### 1.2 Git

| 项 | 值 |
|---|---|
| 初始化 | `C:\FosunAI-Trading\.git`（父目录 `C:\` 无 Git，未误初始化） |
| 基线提交 | **`2dcf58e baseline-us-market-p0-p1`**（155 文件） |
| 实现提交 | **`226f093 feat(us-p2-p3): ...`**（14 文件，+2911/−24） |
| 分支 | `master` |
| 敏感文件 | `.env` / `.venv` / `data/*.db` / `logs/` / `backups/` / `audit_evidence/` **全部被忽略**，`git ls-files` 验证 0 命中 |
| `.gitignore` | 保留原有全部规则，仅**追加**（运行期 DB、内部备份、审计探针输出、测试产物） |

---

## 2. 代码变更

| 文件 | 类型 | 说明 |
|---|---|---|
| `src/ai_trader/market/sessions.py` | 新增 249 行 | 时段感知行情：`SessionQuote` / `QuoteNormalizer` / `SessionPricePolicy` |
| `src/ai_trader/market/status.py` | 新增 393 行 | 市场状态：单证券停牌 / LULD 价格带 / MWCB 三级 |
| `src/ai_trader/broker/orders.py` | 新增 403 行 | 订单类型 / TIF / 状态机 / 券商能力声明 |
| `src/ai_trader/broker/short.py` | 新增 374 行 | 借券库存 / Locate / SSR / 空头核算 / 卖空裁决 |
| `src/ai_trader/portfolio/settlement.py` | 新增 353 行 | T+1 结算账本 + 对账（新建 `portfolio` 包） |
| `src/ai_trader/risk/controller.py` | 改 +150 行 | 注入四个组件 + 新增 3c/3d/3e/3f 四段检查 |
| `src/ai_trader/broker/paper.py` | 改 +127 行 | 撮合支持 stop/stop_limit、TIF 语义、卖空、账本入账 |
| `src/ai_trader/broker/base.py` | 改 +6 行 | `OrderRequest` 增加 `stop_price`，`tif` 注释扩展 |
| `config/config.yaml` + `.template.yaml` | 改 | 新增 `us_phase2` 段（默认 `enabled: false`） |
| `tests/test_us_p2_domains.py` | 新增 478 行 | **46** 个领域单测 |
| `tests/test_us_p2_integration.py` | 新增 320 行 | **22** 个接线/撮合测试 |

---

## 3. 五大能力实现情况

### 3.1 P2-A 盘前/盘后行情 —— **已接入，但无数据源**

| 要求 | 实现 |
|---|---|
| 统一时段模型 | `SessionQuote`（含 `phase` / `trading_day` / 交易所时区时间戳） |
| 识别 pre/regular/post/night | 复用 `clock.session()`，phase 由交易所本地时间判定 |
| 时间戳带时区 | `__post_init__` **拒绝 naive datetime**（`QuoteError`） |
| 交易日 | `trading_day` 取纽约当地日期 |
| 缺失/延迟/重复/乱序/异常 | `QuoteNormalizer`：过期（按 phase 分档 10/60/60/120s）、乱序（seq 回退）、重复（同 seq 同价）、异常跳变（>35%） |
| 防盘前价当盘中价 | `SessionPricePolicy.price(..., "regular_only")` → **`QuoteUnavailable`**，明确"禁止跨时段冒用" |
| 传给策略/风控/订单 | 通过 `SessionPricePolicy` 在取价处统一接入（风控侧用 `is_exit` 走 `risk_exit` 用例） |
| 不凭空生成 | **不产生任何行情**；无数据即 `QuoteUnavailable` |

> 未做：把 `SessionPricePolicy` 硬接到 `strategy/engine.py` 的取价路径（engine 仍走 `broker.get_quote`）。**这一处是"半接入"，见 §7-3**。

### 3.2 P2-B 订单类型与 TIF —— **Paper 完整，真实券商能力明确为"最小集"**

| 项 | 实现 |
|---|---|
| 类型 | `MARKET` / `LIMIT` / `STOP` / `STOP_LIMIT`（`OrderType` 枚举） |
| TIF | `DAY` / `GTC` / `IOC` / `FOK` / `OPG` / `CLS` |
| 不支持即拒绝 | `validate_order` → `ORDER_TYPE_UNSUPPORTED` / `TIF_UNSUPPORTED`，**绝不降级为 market** |
| 参数校验 | 限价单必须有 price；stop 必须有触发价；auction TIF 必须处于竞价窗口 |
| 状态机 | `OrderLifecycle` + `VALID_TRANSITIONS`，非法跃迁抛 `ILLEGAL_TRANSITION`；幂等；拒绝超量成交/成交回退/部分成交冒充满仓 |
| Paper 语义 | stopped 未触发→挂单；触发→市价/限价成交；IOC 未成交→`cancelled`；FOK 不可全成→`FOK_UNFILLABLE` |
| 审计 | 每次状态变更记 `OrderEvent`（时间/状态/成交量/均价/备注） |

**券商能力矩阵**

| Broker | 订单类型 | TIF | 卖空 | Locate | 竞价单 | 说明 |
|---|---|---|---|---|---|---|
| **Paper** | 4/4 | 6/6 | ✅（可关） | ✅ | ✅（仅窗口校验） | 完整模拟 |
| **Live（默认声明）** | market, limit | DAY | ❌ | ❌ | ❌ | `LIVE_DEFAULT_CAPABILITIES`：只保留已确认能力 |

> **真实券商未验证**：官方 SDK 文档未确认 stop/TIF 参数映射，因此默认能力集**故意收窄**，要放开必须先核实官方接口并显式修改能力声明。

### 3.3 P2-C 停牌 / LULD / MWCB —— **模型与状态机完整，数据源缺失**

| 项 | 实现 |
|---|---|
| 独立模型 | `MarketState`（open/luld_paused/halted/mwcb_paused/mwcb_closed/data_stale/unknown） |
| 单证券停牌 | `halt_symbol` / `resume_symbol` + 预计恢复时间 |
| LULD | 价格带 = 参考价 ± (5% Tier1 / 10% Tier2 / 20% 开收盘窗口)，**同方向二次触发带宽翻倍**（最多 2 次） |
| MWCB | L1 −7% 停 15min / L2 −13% 停 15min / L3 −20% 当日收盘 |
| 接入风控 | `controller.py` 3c 段：状态非 open → **禁止新开仓**；`is_exit=True` → **放行 + 告警** |
| 恢复 | 停牌按 `resume_at` 到期；LULD **必须由带内成交确认**才恢复（不自动恢复） |
| 区分 Kill Switch | 与 `safety/halt.py` 的急停完全独立，互不复用 |
| 数据过期/未知 | `DATA_STALE` / `UNKNOWN` → 拒绝新开仓（fail-closed） |

> **阈值来源**：可配置，默认值取自公开规则常识（5/10/20%、−7/−13/−20%）。**未逐条对照 SEC Rule 13H-1 / LULD Plan 原文**，属于"规则模型"而非"合规认证"。

### 3.4 P2-D 卖空与 SSR —— **Paper 完整，真实盘默认关闭且需人工批准**

| 项 | 实现 |
|---|---|
| 默认安全 | `ShortMode.DISABLED` 为默认；原有"卖出必须有持仓"保留 |
| 借券 | `BorrowBook`（库存 + 预留 + 释放），无库存 → 可借 0 → 拒绝 |
| Locate | `LocateService`（TTL、数量校验、占用/释放）；`auto_grant=False` 时明确"无数据源→拒绝" |
| SSR | `SSRRegistry`：较前收跌 ≥10% 触发；触发后卖空价必须 **> 参考价** |
| 空头核算 | `ShortPosition`（加空加权均价、买回平空算已实现盈亏） |
| 真实盘 | `is_live=True` 时必须 `mode=ENABLED` **且** `operator_approved=True`，否则拒绝 |
| Broker 能力 | `supports_short=False` 或 `supports_locate=False` → 直接拒绝 |

> 未做：借券费率计息、强制回补（buy-in）流程、Reg SHO 的 threshold securities 名单。

### 3.5 P2-E T+1 结算账本 —— **账本 + 对账 + 撮合闭环**

| 项 | 实现 |
|---|---|
| 独立账本 | `SettlementLedger`（逐笔 `LedgerEntry`） |
| 记录内容 | 交易日 / 结算日 / 金额 / 手续费 / 状态（unsettled/settled/void） |
| T+1 | `next_trading_day()` 走**交易日历**，跳周末 + 节假日（测试覆盖周五→周一、感恩节跳过） |
| 五类口径 | 已结算现金 / 未结算 / 总现金 / 购买力 / 可提现 |
| 现金 vs 保证金 | `cash`：只用已结算；`margin`：已结算×倍数 + 未结算 |
| 部分成交/撤单/失败 | `record_trade`（逐笔）+ `void_entry`（冲正） |
| 对账 | `reconcile(broker_bp)` → 差异超阈值 → `buying_power_mismatch` 告警；券商未给 BP → `broker_bp_missing` |
| 不误判 | `can_afford` / `effective_available` **取内部账本与券商的较小值** |
| 不覆盖券商 | 只产出 `available` 与 `alerts`，不写回券商数据 |
| 撮合闭环 | Paper 成交后自动 `record_trade`（测试：买入后现金减、卖出后进未结算、购买力不认未结算） |

---

## 4. 真实调用链

```
strategy/engine.py
  └─ 取价 ──→ SessionPricePolicy(regular_only)        ← P2-A（可选接入点）
  └─ OrderIntent(side, qty, price, order_type, tif, stop_price, is_exit)
      └─ risk/controller.evaluate()
          ├─ 3b 既有规则（板块/T+1/涨跌停/tick/日历）     ← P0/P1 保留
          ├─ 3c market_status.evaluate(is_exit)          ← P2-C
          ├─ 3d validate_order(intent, caps)             ← P2-B
          ├─ 3e short_guard.check(...)                   ← P2-D
          ├─ 3f settlement.effective_available(broker_bp)← P2-E
          └─ ...既有 4~9 项检查（白名单/时段/金额/持仓/频率...）
              └─ broker.place_order(OrderRequest)
                  ├─ Paper: parse_order_type/TIF → 撮合 → _apply_fill → settlement.record_trade
                  └─ Live : validate_order(caps) 已在上游完成，适配器按能力映射
```

**关键点**：新检查全部在 `evaluate()` 内部、与既有检查同一 `checks/reasons/warnings` 通道输出，**不是旁路**。

---

## 5. 测试

### 5.1 总览

| 项 | 值 |
|---|---|
| 全量用例 | **672** |
| 失败 | **0** |
| 错误 | **0** |
| 跳过 | 1（`test_sdk_install.py` 已排除，需外部 SDK） |
| 耗时 | 75 s |
| 命令 | `.venv\Scripts\python.exe -m pytest tests/ -q --ignore=tests/test_sdk_install.py` |

### 5.2 新增测试（68 个）

| 文件 | 数量 | 覆盖 |
|---|---|---|
| `test_us_p2_domains.py` | **46** | 行情时段（naive 拒绝/过期/乱序/重复/跨时段冒用）、订单类型与 TIF（含 6 组 TIF 语义参数化）、状态机（非法跃迁/超量/回退/部分冒充全成）、市场状态（未知/停牌/LULD 触发与恢复/翻倍/过期/MWCB 三级与收盘）、卖空（默认禁用/无借券/超借/SSR/人工批准/能力不足/空头盈亏）、结算（T+1 跳节假日/周五→周一/现金vs保证金/未结算不计入/对账差异/缺失告警/取严） |
| `test_us_p2_integration.py` | **22** | 开关关闭不新增检查、市场状态阻断开仓但不阻断退出、缺组件 fail-closed、订单类型拒绝与放行、裸卖空阻断、普通卖出放行、结算不足阻断买入、卖出不校验购买力、Paper 各类订单撮合、IOC 取消、FOK 拒绝、卖空默认拒绝/开启后成交/回补盈亏、撮合→账本闭环 |

### 5.3 P0/P1 回归

原有的 `test_us_p0.py`(34) / `test_us_p1_batch1.py`(41) / `test_us_p1_batch2.py`(36) / `test_market_rules_b4.py`(29) **全部通过**。

**P0/P1 关键项未退化**：
- `America/New_York` + `zoneinfo` + DST：未改动 `clock.py`
- `enforce_markets`：未改动 `rules.py` 与配置
- 全时段持仓 + fail-closed：未改动 `fosun_adapter.get_positions`
- **风险退出保护**：新增的 4 段检查全部显式尊重 `intent.is_exit`（有专门测试 `test_market_status_does_not_block_exit`）
- `_reduce_only_gate`：未改动

---

## 6. 逐项结果

| 模块 | 目标 | 结果 |
|---|---|---|
| 原 P0 | 全部保持 PASS | ✅ **PASS**（34 项测试通过，代码未动） |
| 原 P1 | 全部保持 PASS | ✅ **PASS**（106 项测试通过，代码未动） |
| 盘前行情 | 完整实现并接入 | ⚠️ **PARTIAL**：模型/校验/拒绝冒用已完成并接风控；**未接真实盘前数据源**，`engine` 取价路径仍走 `broker.get_quote` |
| 订单类型与 TIF | Paper 完整，Broker 能力明确 | ✅ **PASS**（Paper 4 类型 × 6 TIF 全实现；Live 明确收窄为 market/limit + DAY） |
| 熔断与停牌 | 规则、状态机、模拟完整 | ⚠️ **PARTIAL**：状态机与 LULD/MWCB 规则完整并接风控；**无交易所停牌/熔断数据源**，阈值未逐条对照法规原文 |
| 卖空 | Paper 完整，真实默认关闭 | ✅ **PASS**（借券/Locate/SSR/空头核算/回补全实现；真实盘默认禁用 + 人工批准） |
| T+1 结算 | 账本、对账、测试完整 | ✅ **PASS**（账本+日历+对账+撮合闭环；现金/保证金双口径） |
| LULD | 规则模型、数据接口、模拟验证 | ⚠️ **PARTIAL**：规则模型 + 注入接口 + 模拟验证齐备；**无实时数据源**，不能称为"实时保护已上线" |

---

## 7. 未完成与阻塞（如实列明）

1. **无任何真实市场数据源**。停牌、LULD、MWCB 的状态全靠调用方注入（`halt_symbol` / `on_quote` / `update_index`）。**当前只能做模拟与测试，不能宣称实时保护生效。**
2. **真实券商能力未验证**。官方 SDK 对 stop / 时间条件单的参数映射未确认，故 `LIVE_DEFAULT_CAPABILITIES` 只声明 market/limit + DAY。真实下单路径未接通这些新类型。
3. **`strategy/engine.py` 取价未切到 `SessionPricePolicy`**。目前盘前价防冒用只在"调用方主动使用 policy"时生效，engine 仍直接调 `broker.get_quote`。这是最需要补的一处（改动面小但影响交易语义）。
4. **结算账本未参与 engine 的资金扣减**。`Portfolio.cash` 仍来自券商 summary；账本只在 Paper 成交后记账 + 风控读取。
5. **OPG/CLS 只做窗口校验**，未实现集合竞价撮合逻辑。
6. **LULD/MWCB 阈值未逐条对照法规原文**（5/10/20%、−7/−13/−20% 为公开规则常识），代码里可配置但缺少"法源登记"。
7. **未做**：借券费率计息、强制回补、Reg SHO threshold list、PDT 规则、盘前行情数据质量评分。

---

## 8. 风险与后续建议

| 优先级 | 建议 |
|---|---|
| P0 | 把 `engine` 取价切到 `SessionPricePolicy`（否则盘前价冒用仍可能发生） |
| P0 | 接一个真实停牌/LULD 数据源，或明确声明"仅模拟"并在 UI/报告里标注 |
| P1 | 核实券商 SDK 的 stop/TIF 参数，再放开 `LIVE_DEFAULT_CAPABILITIES` |
| P1 | 结算账本纳入 engine 的现金流转（下单前预扣、成交后记账、每日对账） |
| P2 | 补 `us_phase2` 的配置校验（enabled=true 但组件缺失时启动即报错） |

---

## 9. 复现命令

```powershell
cd C:\FosunAI-Trading
.\.venv\Scripts\python.exe -m pytest tests/ -q --ignore=tests/test_sdk_install.py
# → 672 passed, 1 skipped
```

回滚：

```powershell
cd C:\FosunAI-Trading
git reset --hard 2dcf58e      # 回到 baseline-us-market-p0-p1
# 或从 C:\FosunAI-Trading-Backups\baseline-20261116-135934\ 全量还原
```

---

*本报告不含任何密钥、Token 或账户信息。未连接真实券商，未发送任何真实订单。*
