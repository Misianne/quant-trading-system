# 星财富美股 · LULD 恢复权限与成交门禁专项审计报告

**审计对象**：`C:\FosunAI-Trading`
**审计轮次**：第八阶段（LULD 恢复权限 / Limit State 成交门禁 / 多交易所隔离 / 竞价事件版本）
**完成时间**：2026-11-16
**审计方式**：追踪实际调用链 → 构造攻击性反例 → 修复真实缺陷 → 全量回归

---

## 0. 结论摘要

| 专项 | 判定 | 说明 |
|---|---|---|
| LULD 恢复权限 | **PASS（结构）／BLOCKED（数据源）** | 已建立身份验证层，裸字符串无法再宣布恢复；但生产权威源尚未接入，`production_authority()` 零注册 = 全拒 |
| Limit State 成交门禁 | **PASS** | 带内可成交、带外拒绝、缺价拒绝、无带拒绝；并修正"状态桶与生命周期不一致"的一致性漏洞 |
| 多交易所状态隔离 | **PASS（修复 P0 绕过后）** | 按 `symbol\|exchange` 分桶；未知场所查询改为**最严格聚合** |
| 竞价事件版本控制 | **PASS** | 竞价独立版本号、事件版本单调、重复事件幂等 |

**本轮发现并修复的真实缺陷（非表面修补）**

1. **P0 成交门禁绕过（严重）**：引入交易所维度后，停牌记在 `AAPL|nasdaq` 桶，而 `PaperBroker` 的成交门禁调用 `fill_allowed(sym, price=...)` **不传 exchange**，落到 `AAPL|*` 桶 —— 该桶无停牌 → 判定 OPEN → **交易所已停牌仍然成交**。已用回归测试实证：禁用聚合后该用例立即失败。
2. **状态不一致时"挑宽松的用"（中等）**：状态桶停在 `LULD_LIMIT_STATE`、而 LULD 生命周期已进入 `TRADING_PAUSE` 时，成交门禁只看桶不看生命周期，带内价仍可成交。已改为以生命周期为准。

**全量回归**：`808 passed, 0 failed, 0 errors, 1 skipped`（`r8.xml`，命令见 §8）
**配置状态**：`us_phase2.enabled: false` 未改动（已核验）

---

## 1. Git 基线与回滚点

| 项 | 值 |
|---|---|
| 本轮起点（审计基线） | `14c880e` docs(us-luld): reopening auction implementation report |
| 本轮提交 1 | `d82149a` feat(us-luld): resume authority layer + fail-safe fill gate + venue-isolated state |
| 本轮提交 2 | `1750da2` fix(us-luld): LIMIT_STATE fill/submit must defer to lifecycle phase |
| 当前 HEAD | `1750da2` |
| 工作树 | **干净**（`git status --porcelain` 空输出，已核验） |
| 配置开关 | `config/config.yaml:656  us_phase2.enabled: false`（未改动） |

**回滚命令**

```bash
cd /c/FosunAI-Trading
git reset --hard 14c880e          # 回到本轮起点
# 如需连数据一并回滚：
#   C:\FosunAI-Trading-Backups\round4-20261116-152218\ 覆盖回项目根
```

> 注：本轮**未**删除或改写任何历史提交，`14c880e` / `1f64f01` / `def8f23` 均完好可达。

---

## 2. 备份与 SHA256

| 项 | 值 |
|---|---|
| 备份目录 | `C:\FosunAI-Trading-Backups\round4-20261116-152218\` |
| 文件数 | **834**（另含 `MANIFEST.sha256` 本身，目录内共 835 项） |
| 清单行数 | **834** |
| 校验结果 | `sha256sum -c MANIFEST.sha256` → **全部 OK，无一条失败**（exit 0） |

备份在改动**之前**创建，覆盖 `src/` / `tests/` / `config/` / 文档与数据文件，可直接作为回滚源。

---

## 3. 完整调用链（源码级追踪）

### 3.1 下单主链

```
StrategyEngine._build_context()
  └─ quote_gateway.get(..., use_case="regular_only")        # 统一行情入口，拒朴素时间戳/过期/乱序
StrategyEngine._run_exit_pass()
  └─ quote_gateway.get(..., use_case="risk_exit")           # 退出用宽松用例
StrategyEngine._execute()
  └─ MarketStatusService.intent_gate(sym, is_exit)           # 阶段 A：意图
  └─ MarketStatusService.submit_gate(sym, is_exit, price)    # 阶段 B：提交（本轮新增 price）
  └─ PaperBroker.place_order(...)
        └─ MarketStatusService.fill_allowed(sym, price=成交价)  # 阶段 C：成交（本轮新增 price）
              ├─ _status_local(sym, now, exchange)           # 本单位解析（计时器/行情新鲜度）
              └─ _fuse(st, sym, exchange)                    # 跨场所最严格聚合（本轮新增）
```

**关键点**：阶段 C 由 Broker 自己再验一次，**不依赖** Engine 事先检查。
即"绕过 Engine 直接对 PaperBroker 下单"同样被拦（`test_paper_cannot_bypass_gate_directly`）。

### 3.2 LULD 生命周期链

```
MarketStatusService.observe_trade()
  └─ LuldLifecycle.on_trade(price, ts)
        └─ 越带 → LimitStateRecord → LULD_LIMIT_STATE（15 秒判定）
              └─ 满 15 秒 → TradingPause(pause_event_id, trading_date) → LULD_PAUSED
MarketStatusService.status()   [5 分钟到期]
  └─ LuldLifecycle.start_auction()          # 到期 ≠ 恢复，必须进竞价
        └─ AuctionState(auction_id, trading_date, 独立 version=0)
              └─ on_auction_update(indicative, imbalance, version, ts)
                    └─ RESUME_PENDING        # 指示价/失衡**不能**推出恢复
                          └─ on_official_resume(event, authority)
                                └─ ResumeAuthority.verify()   # 8 项校验
                                      ├─ 通过 → REOPENED（tradable）
                                      └─ 拒绝 → 保持 RESUME_PENDING + resume_rejected
```

**设计要点**：本地计时器只负责"到点进入竞价"，**永远不能**直接产生 `REOPENED`。
`test_local_timer_cannot_resume` 锁死这条边界。

---

## 4. 状态 × 成交许可矩阵

`✓` = 允许；`✗` = 拒绝；`条件` = 满足附加约束才允许。

| 市场状态 | 意图·新开仓 | 意图·退出 | 提交·新开仓 | 提交·退出 | 成交 | 附加约束 / 依据 |
|---|---|---|---|---|---|---|
| `OPEN` | ✓ | ✓ | ✓ | ✓ | ✓ | 须 LULD 生命周期 `tradable`；行情须新鲜 |
| `LULD_LIMIT_STATE` | ✓（带告警） | ✓（带告警） | 条件 | 条件 | 条件 | **仅带内价**；缺价 → ✗；无价格带 → 成交 ✗；生命周期非 tradable → ✗ |
| `LULD_PAUSED` | ✗ | ✓（告警） | ✗ | ✗ | ✗ | 5 分钟到期只进竞价，不恢复 |
| `HALTED` | ✗ | ✓（告警） | ✗ | ✗ | ✗ | 只认恢复通知；预计恢复时间过期仍保持停牌 |
| `MWCB_PAUSED` | ✗ | ✓（告警） | ✗ | ✗ | ✗ | 15 分钟；收市前 35 分钟内不触发 |
| `MWCB_CLOSED` | ✗ | ✓（告警） | ✗ | ✗ | ✗ | L3，当日不再恢复 |
| `DATA_STALE` | ✗ | ✓（告警） | ✗ | ✗ | ✗ | 行情超 `max_quote_age`（默认 60s） |
| `UNKNOWN` | ✗ | ✓（告警） | ✗ | ✗ | ✗ | 无行情；**新成交可将其抬升为 OPEN** |

**三条不变量**

1. **退出意图永不被市场状态否掉**（阶段 A 一律放行 + 告警）。这是 P0/P1 风控语义，本轮未削弱。
2. **阶段 B/C 对退出同样拒绝**。退出意图"保留"≠"允许成交"——暂停期间提交也不会被交易所接受。
3. **跨场所聚合只能更严**。`_fuse()` 仅在候选状态严格度更高时覆盖，绝无"放宽"分支。

---

## 5. 恢复事件身份验证机制

实现文件：`src/ai_trader/market/luld_authority.py`（280 行）

### 5.1 校验项（缺一即拒）

| # | 校验 | 拒绝码 |
|---|---|---|
| 1 | provider 已在注册表 | `PROVIDER_UNKNOWN` |
| 2 | provider 声明 `can_authorize_resume` | `PROVIDER_NOT_AUTHORIZED` |
| 3 | provider 类型非 `UNKNOWN` | `PROVIDER_KIND_UNKNOWN` |
| 4 | **测试身份不得在生产模式签发**（反之亦然） | `TEST_PROVIDER_IN_PRODUCTION` |
| 5 | provider 已授权该交易所 | `PROVIDER_EXCHANGE_MISMATCH` |
| 6 | 事件交易所 == 期望交易所 | `EXCHANGE_MISMATCH` |
| 7 | 证券代码匹配（经 `normalize_symbol`） | `SYMBOL_MISMATCH` |
| 8 | 交易日匹配 | `TRADING_DATE_MISMATCH` |
| 9 | `pause_event_id` 与当前暂停匹配 | `PAUSE_ID_MISMATCH` |
| 10 | `auction_id` 与当前竞价匹配 | `AUCTION_ID_MISMATCH` |
| 11 | `status == "resumed"` | `STATUS_INVALID` |
| 12 | `event_version` **严格大于**已应用版本 | `VERSION_STALE` |
| 13 | 时间戳不得来自未来（容差 2 分钟） | `TIMESTAMP_FUTURE` |
| 14 | 时间戳不得过旧（上限 2 小时） | `TIMESTAMP_STALE` |

> 报告口径说明：为避免夸大，这里列出 **14 条判定**、归为 **8 类**（身份 / 隔离 / 授权 / 绑定 / 语义 / 版本 / 时间 / 幂等）。

### 5.2 生产边界（fail-closed）

```python
def production_authority() -> ResumeAuthority:
    """生产模式：只认 EXCHANGE_FEED 且显式授权的 provider。"""
    return ResumeAuthority([], mode="production")   # 故意零注册
```

**零注册 = 任何恢复事件都被拒**。这是刻意的：在真实 LULD/停牌权威源接入并通过验收前，
生产路径**不可能**靠"构造一个事件对象"把标的放回可交易。
`test_production_authority_rejects_everything_without_registered_provider` 锁死该行为。

### 5.3 未做的事（明确声明）

本层**不做密码学签名验证**。它做的是**身份 + 绑定 + 版本 + 时间**校验。
真实数据源接入时必须在其上补签名/证书校验（见 §10 UNVERIFIED）。

---

## 6. 多交易所状态隔离设计

### 6.1 写入：按桶隔离

```python
@staticmethod
def _key(symbol: str, exchange: str = "*") -> str:
    return f"{str(symbol).upper()}|{str(exchange or '*').lower()}"
```

- `halt_symbol("AAPL", exchange="nasdaq")` 只写 `AAPL|nasdaq`
- `halt_symbol("AAPL")` 写 `AAPL|*`（**标的级事实**）
- `notify_resume` / `record_trade` / `observe_trade` 同样按桶写入

### 6.2 读取：按最严格聚合（**本轮修复**）

| 查询方式 | 参考的桶 | 理由 |
|---|---|---|
| `exchange="nasdaq"` | `AAPL\|nasdaq` + `AAPL\|*` | 标的级停牌对全场所生效；**不参考其它具体场所**（保隔离） |
| `exchange="*"`（未指定） | 该标的**所有**桶 | **场所未知 → 无法排除任何一家停牌 → 必须保守** |

聚合只考虑**显式事件状态**（`HALTED` / `LULD_PAUSED` / `MWCB_PAUSED` / `MWCB_CLOSED` / `LULD_LIMIT_STATE`）。
行情新鲜度是标的级（合并行情），不参与场所聚合，避免空桶用 `UNKNOWN` 污染结果。

严格度序：`OPEN(0) < LULD_LIMIT_STATE(1) < UNKNOWN(2) < DATA_STALE(3) < LULD_PAUSED(4) < HALTED(5) < MWCB_PAUSED(6) < MWCB_CLOSED(7)`

### 6.3 被修复的 P0 绕过（实证）

修复前行为（禁用 `_fuse` 后实测）：

```
svc.halt_symbol("AAPL", exchange="nasdaq")
svc.fill_allowed("AAPL", price=100.0)        # PaperBroker 的真实调用形态
→ True（允许成交）                            # 交易所已停牌却成交
```

修复后：`False, "AAPL 处于 halted，不允许成交"`，并且 `PaperBroker.place_order` 返回非 `filled`。

**实证方式**：临时将 `_fuse()` 短路为 `return st`，4 条用例立即失败；恢复后全绿（见 §7 说明）。

### 6.4 已知边界（不夸大）

`SymbolStatus` 已按 `(symbol, exchange)` 隔离；但以下三处仍是 **symbol 维度**：

| 结构 | 维度 | 理由 / 残余风险 |
|---|---|---|
| `_last_trade` | symbol | 美国合并行情（CTA/UTP）本就是标的级；同一标的极少在两家所独立报价 |
| `_bands`（LULD 价格带） | symbol | 价格带由参考价决定，参考价是标的级事实 |
| `lifecycles` | symbol | 生命周期对象按标的创建；`ensure_lifecycle(sym, exchange=...)` 的 exchange 只用于竞价策略选择 |

**若**未来接入"同一标的多场所独立报价"场景（如跨所双重上市），这三处需一并扩维，
且 `_lifecycle_ok()` 必须改为按桶查询。**当前不构成实盘风险，但属于明确的技术债。**

---

## 7. 攻击测试清单及结果

文件：`tests/test_us_luld_authority_attacks.py` —— **36 条用例，全部通过**。
（31 个测试函数，其中伪造字段用例参数化 6 例 → 31 - 1 + 6 = 36）

### A. Limit State 成交门禁（5 条）

| # | 用例 | 攻击方式 | 预期 | 结果 |
|---|---|---|---|---|
| A1 | `test_limit_state_allows_in_band_fill` | 带内价成交 | 允许 | PASS |
| A2 | `test_limit_state_rejects_out_of_band_fill` | 带外价成交 | 拒绝 | PASS |
| A3 | `test_limit_state_submit_requires_price_in_band` | 带外价提交 / 缺价提交 | 拒绝 | PASS |
| A4 | `test_aggregation_only_tightens_never_loosens` | Limit State 只在 `*` 桶，查具体场所 | 仍须带内，且 `tradable=False` | PASS |
| A5 | `test_limit_state_bucket_cannot_outlive_lifecycle_pause` | 桶=LIMIT_STATE，生命周期=TRADING_PAUSE | 拒绝（以生命周期为准） | PASS |

### B. 暂停 / 竞价 / 待恢复期间禁止成交（4 条）

| # | 用例 | 攻击方式 | 预期 | 结果 |
|---|---|---|---|---|
| B1 | `test_pause_rejects_fill_and_submit` | 暂停期提交 + 成交 | 双拒 | PASS |
| B2 | `test_auction_period_rejects_continuous_fill` | 竞价期连续交易成交 | 拒绝 | PASS |
| B3 | `test_resume_pending_rejects_fill` | 待恢复期成交 | 拒绝 | PASS |
| B4 | `test_exit_intent_allowed_but_fill_denied_during_pause` | 用退出意图绕门禁 | 意图放行、成交仍拒 | PASS |

### C. 行情与状态不一致（2 条）

| # | 用例 | 攻击方式 | 预期 | 结果 |
|---|---|---|---|---|
| C1 | `test_stale_quote_blocks_fill_even_if_state_open` | 状态 OPEN 但报价过期 | 拒绝成交 | PASS |
| C2 | `test_indicative_price_cannot_imply_resume` | 用竞价指示价暗示已恢复 | 不改变状态 | PASS |

### D. 恢复事件身份伪造（14 条）

| # | 用例 | 攻击方式 | 预期 | 结果 |
|---|---|---|---|---|
| D1 | `test_no_authority_cannot_resume` | 旧式裸字符串恢复（`on_official_resume(ts, version)`） | `TypeError`（签名已移除） | PASS |
| D2 | `test_production_authority_rejects_everything_without_registered_provider` | 生产层零注册时伪造事件 | 全拒 | PASS |
| D3 | `test_test_provider_rejected_in_production_mode` | 测试身份在生产模式签发 | `TEST_PROVIDER_IN_PRODUCTION` | PASS |
| D4 | `test_unauthorized_provider_rejected` | 已注册但未授权恢复 | `PROVIDER_NOT_AUTHORIZED` | PASS |
| D5 | `test_unknown_provider_rejected` | 未注册 provider | `PROVIDER_UNKNOWN` | PASS |
| D6 | `test_forged_resume_fields_rejected[symbol]` | 伪造证券代码 | `SYMBOL_MISMATCH` | PASS |
| D7 | `test_forged_resume_fields_rejected[exchange]` | 伪造交易所 | `EXCHANGE_MISMATCH` | PASS |
| D8 | `test_forged_resume_fields_rejected[trading_date]` | 伪造交易日 | `TRADING_DATE_MISMATCH` | PASS |
| D9 | `test_forged_resume_fields_rejected[pause_event_id]` | 伪造暂停事件 ID | `PAUSE_ID_MISMATCH` | PASS |
| D10 | `test_forged_resume_fields_rejected[auction_id]` | 伪造竞价 ID | `AUCTION_ID_MISMATCH` | PASS |
| D11 | `test_forged_resume_fields_rejected[status]` | 伪造状态串 | `STATUS_INVALID` | PASS |
| D12 | `test_stale_resume_version_rejected` | 重放旧版本事件 | `VERSION_STALE` | PASS |
| D13 | `test_future_and_stale_timestamps_rejected` | 未来时间 / 过旧事件 | `TIMESTAMP_FUTURE` / `TIMESTAMP_STALE` | PASS |
| D14 | `test_local_timer_cannot_resume` | 让本地 5 分钟计时器直接产生"已恢复" | 只能进竞价，不得恢复 | PASS |

### E. 多交易所隔离与绕过（8 条）

| # | 用例 | 攻击方式 | 预期 | 结果 |
|---|---|---|---|---|
| E1 | `test_two_exchanges_have_independent_state` | nasdaq 停牌不应影响 nyse | 状态独立 | PASS |
| E2 | `test_unknown_exchange_write_does_not_clear_venue_halt` | 在 `*` 桶登记成交冲掉场所停牌 | 停牌保持 | PASS |
| E3 | `test_unknown_exchange_query_is_conservative` | **用默认参数查询绕过场所停牌** | 必须 HALTED | PASS |
| E4 | `test_symbol_level_halt_blocks_every_exchange` | 标的级停牌只挡 `*` 桶 | 全场所生效 | PASS |
| E5 | `test_broker_fill_gate_cannot_bypass_venue_halt` | **PaperBroker 不传 exchange 绕过** | 不成交 | PASS |
| E6 | `test_resume_one_exchange_does_not_touch_another` | 恢复一家顺带放开另一家 | 另一家仍 HALTED | PASS |
| E7 | `test_fused_result_does_not_pollute_stored_state` | 篡改返回对象污染存储 | 存储不变 | PASS |
| E8 | `test_authority_rejects_wrong_exchange_provider` | NYSE 授权源签发 Nasdaq 恢复 | `PROVIDER_EXCHANGE_MISMATCH` | PASS |

### F. Broker 端到端与幂等（3 条）

| # | 用例 | 攻击方式 | 预期 | 结果 |
|---|---|---|---|---|
| F1 | `test_paper_cannot_bypass_gate_directly` | 绕过 Engine 直接下单 | 不成交 | PASS |
| F2 | `test_paper_limit_state_in_band_fills_out_of_band_blocked` | 带内成交 / 带外拒绝 | 符合预期 | PASS |
| F3 | `test_duplicate_resume_is_idempotent` | 重复投递同一恢复事件 | 幂等，不重复推进 | PASS |

### 反例有效性实证（先红后绿）

为证明这些用例**真的**能抓住缺陷，而非"写完就绿"，做了隔离实验：

| 实验 | 操作 | 结果 |
|---|---|---|
| 禁用跨场所聚合 | 临时令 `_fuse()` 直接 `return st` | `test_unknown_exchange_query_is_conservative`、`test_symbol_level_halt_blocks_every_exchange`、`test_broker_fill_gate_cannot_bypass_venue_halt`、`test_aggregation_only_tightens_never_loosens` **全部失败** |
| 恢复聚合 | 回滚实验改动 | 36 条**全绿** |
| 回退整个交易所维度 | `git stash` 掉 `status.py` | 6 条用例 `TypeError: unexpected keyword 'exchange'`（证明维度确实是新增的） |

---

## 8. 全量测试结果

```bash
cd /c/FosunAI-Trading
env -u PYTHONPATH ./.venv/Scripts/python.exe -m pytest tests/ -q \
    --junitxml=r8.xml -p no:cacheprovider
```

| 指标 | 值 |
|---|---|
| 总用例 | **808** |
| failures | **0** |
| errors | **0** |
| skipped | **1**（既有跳过项，非本轮新增） |
| 耗时 | 76.1 s |
| 证据文件 | `r8.xml`（已入库） |

**与上一轮 `r7.xml`（795）的差异说明（避免数字被误读）**

| 来源 | 增量 |
|---|---|
| 攻击测试新增 5 条（E3/E5/E4 等） | +5 |
| 攻击测试新增 1 条（A5 生命周期一致性） | +1 |
| `tests/test_sdk_install` 的 7 条 | +7（**非本轮改动**：该模块在 `r7` 那一次收集范围内未被收集到，本轮集合完整后出现；本轮未修改该文件） |
| 合计 | 795 + 13 = **808** |

> 已核对 `r7.xml` 与 `r8.xml` 的按模块分布：除 `test_us_luld_authority_attacks`（30→36）与 `test_sdk_install`（0→7）外，**其余模块用例数完全一致**，无用例被删除或跳过。

---

## 9. 官方规则依据

| 规则 | 出处 | 本系统落点 |
|---|---|---|
| LULD 价格带（3 价格档 × 2 档位） | LULD Plan **Table 1** | `luld_band_width()` |
| 带宽双倍（Amendment 18） | LULD Plan Amendment 18：**开盘 9:30–9:45 不再放宽**；收市前 25 分钟仅 Tier1、或 Tier2 且参考价 ≤ $3 | `_compute_band(doubled=...)` + `luld_close_window` |
| 5 秒 / 15 秒判定 | LULD Plan：越带后 15 秒内未回到带内 → 触发暂停 | `limit_state_window=15s` |
| 停牌最短 5 分钟 | LULD Plan | `luld_pause = 5min`（与 MWCB 的 15min **分开计时**） |
| 重新开盘竞价 | **Nasdaq Rule 4120(c)(10)**：Display Only Period、Auction Reference Price、Auction Collar、5% 放宽 | `NasdaqAuctionPolicy` |
| 重新开盘竞价（NYSE） | NYSE Amendment 12 | `NyseAuctionPolicy` |
| MWCB −7/−13/−20% | MWCB Plan：L1/L2 停 15 分钟；**收市前 35 分钟内不触发**；L3 当日收盘 | `_mwcb`、`mwcb_cutoff_minutes=35` |
| 恢复必须来自交易所 | 交易所恢复通知是唯一权威 | `ResumeAuthority` + `production_authority()` |

---

## 10. 尚未解决的 BLOCKED / UNVERIFIED

| 级别 | 项 | 影响 | 结论 |
|---|---|---|---|
| **BLOCKED** | 无真实 LULD / 停牌权威数据源（无交易所 feed、无 SIP 授权） | `production_authority()` 只能零注册 → 生产路径永远不放行恢复 | **实盘不可用**，Paper/Replay 不受影响 |
| **BLOCKED** | 无券商连接、无真实下单通道 | 无法验证真实回报/拒单语义 | 按用户明令禁止，本轮未接 |
| **UNVERIFIED** | 恢复事件无密码学签名/证书校验 | 中间人可伪造"已注册 provider 身份"的事件 | 接真实源时必须补 |
| **UNVERIFIED** | 竞价成交价与撮合优先级未建模 | 竞价阶段只能判"可/不可交易"，无法给出成交价 | Paper 用指示价近似 |
| **UNVERIFIED** | `_last_trade` / `_bands` / `lifecycles` 仍是 symbol 维度 | 同一标的多场所独立报价场景下隔离不完整 | 见 §6.4，当前不构成风险 |
| **UNVERIFIED** | LULD 参考价来源依赖调用方注入 | 注入错误前收 → 带宽错 | 已 fail-closed（取不到返回 `UNAVAILABLE`） |
| 已知 | `us_phase2.enabled: false` | P2 组件默认不生效 | 符合用户要求，保持关闭 |

---

## 11. 是否满足 Paper / Replay 验证条件

| 场景 | 判定 | 依据 |
|---|---|---|
| **Replay（历史回放）** | **满足** | `test_authority()` 提供 `mode="test"` 的授权源，可完整驱动 `暂停 → 竞价 → 恢复` 全链；事件版本/幂等/时间戳校验在回放中同样生效 |
| **Paper（模拟盘）** | **满足** | 三道门禁全部走真实入口；`PaperBroker` 端到端 3 条用例通过；停牌/暂停/Limit State 均能阻止成交（含本轮修复的绕过路径） |
| **Live（实盘）** | **不满足（NO-GO）** | 权威数据源为空（fail-closed）、无签名校验、无券商通道；且 `us_phase2.enabled=false` |

**开启 Paper 验证的前置动作**（本轮**未执行**，等指令）：

1. 注入 `MarketStatusService` + `ShortGuard` + `SettlementLedger` + `BrokerCapabilities`（缺一即 fail-closed）
2. 用 `test_authority()` 或自定义 `ProviderSpec(kind=TEST)` 驱动恢复
3. 保持 `short.mode: disabled`、`settlement.mode: cash`
4. 确认 `paper_state.json` 落盘路径与真实账本隔离

---

## 附：本轮改动文件清单

| 文件 | 类型 | 说明 |
|---|---|---|
| `src/ai_trader/market/luld_authority.py` | 新增 280 行 | 身份验证层 |
| `src/ai_trader/market/status.py` | 改 | 交易所维度键、`_fuse` 最严格聚合、三道门禁重写、Limit State 与生命周期一致性 |
| `src/ai_trader/market/luld.py` | 改 | `TradingPause.pause_event_id/trading_date`、`AuctionState.trading_date`、恢复事件接线 |
| `src/ai_trader/broker/paper.py` | 改 | 成交门禁传入**成交价** |
| `src/ai_trader/strategy/engine.py` | 改 | 提交门禁传入**委托价** |
| `tests/test_us_luld_authority_attacks.py` | 新增 36 例 | 攻击性反例 |
| `tests/test_us_luld_lifecycle.py` | 改 | 适配新签名 |
| `r7.xml` / `r8.xml` | 证据 | 修复前后全量回归 JUnit |
