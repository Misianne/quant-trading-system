# US_P1_BATCH2_IMPLEMENTATION_REPORT.md —— 订单不确定性与减仓幂等性修复

**项目**：`C:\FosunAI-Trading`
**依据**：`US_P1_BATCH1_INDEPENDENT_REVIEW.md`（R-1 / R-2 / R-7 / R-4；R-3、R-8 仅分析）
**时间**：2026-11-09 15:15 – 16:05 (+08:00)
**性质**：允许修改代码/测试；**未连接真实券商、未发送任何真实订单、未修改官方 SDK、未触碰用户数据库**
**安全开关（实测，前后一致）**：

```
LIVE_TRADING            = False
ORDER_EXECUTION_ENABLED = False
PAPER_TRADING           = True
can_send_real_order     = (False, 'PAPER_TRADING=true，真实券商连接被禁用')
risk.halt_write_failure_exit_mode = "block"   （未改动）
```

---

## 0. 结论摘要

| 项 | 目标 | 结果 | 关键证据 |
|---|---|---|---|
| **R-1** 下单后落库失败致订单丢失 | 发单前预写、重启可恢复、对账前禁止再开仓 | ✅ **已修复** | 复核报告的五步复现全部反转：重启后意图仍在、对账被触发、+400s 开仓被 `unresolved_order_state` 阻断且券商未收到新单 |
| **R-2** 库不可读时重复卖出（可超卖） | 可卖余量 = 持仓 − 未完成卖单预留；读不到就禁卖 | ✅ **已修复** | 6 个真实进程抢同一额度：**1 成功 / 5 被拒**；库不可读时止损被 `reduction_blocked_unverified` 拦下，券商收到 0 笔 |
| **R-7** 未知买单成交后止损被清除 | 保护绑定实际持仓 + 版本，不再"发过卖单就删" | ✅ **已修复** | 退出后 `exit_pending=True`；未知买单成交（持仓回到 10）→ `stops_retained`，`protected_qty` 重算为 10 |
| **R-4** 异常时间戳让冷却失效 | 开仓 fail-closed、减仓保留执行 | ✅ **已修复** | 空/缺失/无法解析/无时区 ts → 开仓 `duplicate_order` 拒绝；减仓仍放行 |
| **R-3 / R-8** reduce-only | 只分析，不启用 | ✅ 遵守 | `halt_write_failure_exit_mode` 仍为 `block`；设计见 `US_P1_BATCH2_REDUCE_ONLY_DESIGN.md` |
| 回归 | 全绿 | ✅ **423 passed / 1 skipped / 0 failed** | 原 388 → 新增 36 |

---

## 一、执行纪律核对

| 要求 | 执行情况 |
|---|---|
| 先读复核报告/架构 | 已读 `US_P1_BATCH1_INDEPENDENT_REVIEW.md`、`journal.py` schema、`engine._execute/_reconcile_*`、`risk.controller` 的 `_reduce_only_gate`/`_duplicate_check_detail`、`faults.py`、HALT 机制 |
| 备份 + SHA256 基线 | `backups/us_p1_b2_20261109-151512`（**75 文件**，`MANIFEST.sha256.json`） |
| 排除敏感数据 | 未包含 `.env` / `vault.json` / `*.db` / `logs/`；`leak=无` |
| 不初始化 Git | 仍无 `.git` |
| 不重置数据库 | `data/audit.db` mtime 仍 `2026-11-08 13:00`，未打开写、未迁移真实库 |
| 不改官方 SDK | 26 文件 SHA256 与 `sdk_baseline_us_p1_b1.json` **0 不一致** |
| 不连真实账户 / 不下真实单 | 全部券商交互为进程内 `Broker` 契约替身 |
| 不为过测试放宽风控 | 本批**只收紧不放松**（新增 `sell_capacity`、时间戳 fail-closed、预写门禁） |
| 不把 unknown/异常/落库失败当"不存在" | 全部显式建模为 `unknown` 并持续占用预留 |
| 每项修复有生产路径测试 | 36 个新用例全部驱动 `engine.scan/monitor` / `risk.evaluate`，无 helper-only 用例（R-3 除外，本轮不启用） |

---

## 二、R-1：下单后落库失败（P1）

### 2.1 原缺陷（复核报告 §8 R-1 复现）

券商返回 `status=unknown` + 空 `order_id`，且 `record_order` 写入失败时：
订单在本地**完全消失**（不是 unknown，而是"不存在"）→ 重启后不可见、不可对账，
`+400s` 可再次下单；故障流水不含 `client_id`，人工无从核对。

### 2.2 实现：订单意图预写日志（write-ahead）

**新增表 `order_intents`（additive，`CREATE TABLE IF NOT EXISTS`）**

| 列 | 说明 |
|---|---|
| `client_id` | **UNIQUE**；唯一恢复依据（不依赖 `order_id`） |
| `created_at` / `updated_at` | 意图创建与最近更新 |
| `symbol/market/side/qty/price/order_type/currency/stop_loss/take_profit` | 请求参数快照 |
| `signal_id` / `decision_id` | 溯源 |
| `request_digest` / `request_json` | 请求参数摘要（sha256）+ 原文 |
| `state` | `intent / accepted / submitted / unknown / terminal / rejected` |
| `broker_order_id` / `broker_status` / `last_error` / `attempts` | 券商回报与失败原因 |

**状态机（只有终态释放卖出预留）**

```
intent ──券商受理──> accepted/submitted ──终结──> terminal
   │                        │
   └──结果不明/超时/异常────> unknown ──对账确认──> terminal
                                └──对账失败──> 保持 unknown（永久占用预留）
```

`INTENT_OPEN_STATES = (intent, accepted, submitted, unknown)` —— **`unknown` 明确包含在内**，
绝不自动释放。

### 2.3 关键控制点（对应任务 9 条）

| # | 要求 | 落点 |
|---|---|---|
| 1 | 调用 broker **之前**持久化 client_id/标的/方向/数量/请求摘要/时间/初始状态 | `_execute` → `_prewrite_intent()` → `journal.prewrite_intent()`（`request_json` + `request_digest` + `created_at` + `state=intent`） |
| 2 | 只有预写成功才允许进 broker | 预写返回 `False` 直接 `return None`，并写 `order_intent_prewrite_blocked` 审计 |
| 3 | broker 异常/超时/空单号/unknown 也保留 client_id 与请求 | `_mark_intent_unknown()`（只改 state，不删行） |
| 4 | 后续状态更新失败**不得删除**预写记录 | `update_intent` 只做 `UPDATE`；`_jupdate_intent` 失败仅记录故障 |
| 5 | 重启后扫描未完成订单 → 恢复为 unknown | `__init__` → `_recover_order_intents()` → `journal.recover_open_intents()` |
| 6 | 对账前禁止同标的同风险方向再开仓 | 意图并入 `_recent_orders_for()` → 命中 `unresolved_order_state`；`orders_known` 同时与 `_intents_available` 做与运算 |
| 7 | 不依赖 order_id；支持只有 client_id | 对账统一走 `reconcile_order(client_id=...)`；`recover_open_intents` 以 client_id 为主键 |
| 8 | 持久化介质完全不可用 → 阻止开仓，不得先发单 | `prewrite_intent` 抛错 → `_prewrite_intent` 返回 `(False, ...)` → 不调用券商；`_intents_available=False` 进一步让 `orders_known=False` |
| 9 | 券商不支持按 client_id 查询 → 标注外部能力缺失并持续阻断 | 对账抛错 → `_jupdate_intent(unknown)`，预留与封锁**持续保留**，直到人工确认（`not_accepted` 只有券商明确回答才写入） |

**附加**：`_jrecord_order` 失败时，critical 故障现在带上 `client_id / broker_order_id / side / status`，
人工可据此在券商侧检索。

### 2.4 修复前后对照（真实链路）

```
修复前（复核报告）                     修复后（本轮实测）
orders 行数 0                          orders 行数 0（模拟落库失败仍如此）
find_orders(unknown) = []              意图表：client_id/unknown 保留
重启后订单不可见                        重启后 _recovered_intents = [client_id]
无对账动作                              monitor → reconcile_failed（确实按 client_id 尝试过）
+400s 再开仓 → 又下一单                 +400s → rejected，拦截项
                                        ['unresolved_order_state','duplicate_order']，
                                        券商收到 0 笔
故障流水无 client_id                    故障流水含 client_id ✅
```

---

## 三、R-2：数据库不可读时重复卖出（P1）

### 3.1 原缺陷

可卖上限只取"已核实持仓"，未扣除**未完成卖单**；`orders_known=False` 时
`recent_orders=[]`，风控看不到在途卖单 → 再发一笔同额卖单 → 名义卖出 20 > 持仓 10。

### 3.2 实现

**1) 预留量来源 = 意图表（权威）**

```python
journal.open_sell_reserved(symbol) -> ({symbol: qty}, ok)
   # SUM(qty) WHERE side='sell' AND state IN (intent, accepted, submitted, unknown)
```

未完成卖单包括 **pending / submitted / unknown / 状态更新失败 / 其他未终结** —— 全部计入。

**2) `Portfolio` 新增**

* `open_sell_reserved: Dict[str, float]`
* `reservations_known: bool`（False = 读不出来）
* `intents_available: bool`
* `sellable_now(symbol) = 持仓 − 预留`

**3) 风控新增 `sell_capacity` 检查（对所有卖出生效，含止损）**

```
持仓 10 − 未完成卖单预留 6 = 可卖 4，卖出 5   → 拒绝
reservations_known=False                     → 拒绝（reduction_blocked_unverified）
intents_available=False                      → 拒绝
```

**4) 下单前二次原子校验**

`prewrite_intent(..., sell_capacity=held)` 在 `BEGIN IMMEDIATE` 事务内
`SUM(未完成卖单) + 本单 ≤ 持仓`，由 SQLite 唯一事务保证**跨进程**互斥。

**5) `reduce-only` 门（N-2 遗留）同步改用可卖余量**，不再用总持仓。

### 3.3 与大清算口径的差异（重要）

`orders_known=False` 时，batch1 是"放行减仓"，**本批改为禁止一切新增卖出**。
这是任务 §3.4 明确要求的（"数据库读取失败、对账失败、订单方向不明时，禁止再次卖出"），
代价是**数据库故障期间无法止损**，必须靠故障告警 + 人工介入；详见 §九 残留风险。

### 3.4 实测矩阵

| 场景 | 结果 |
|---|---|
| 持仓 10 / 预留 10 / 卖 10 | ❌ `sell_capacity` |
| 持仓 10 / 未知卖 6 / 卖 5 | ❌ `sell_capacity` |
| 两笔未知卖 4+6 / 卖 1 | ❌ `超过可卖余量` |
| 持仓 10 / 预留 4 / 卖 6 | ✅ 通过；卖 7 ❌ |
| 预留量读失败 | ❌ `reduction_blocked_unverified` |
| 意图表不可用 | ❌ `sell_capacity` |
| **6 个真实进程**抢卖 10（额度 10） | **1 成功 / 5 被拒**，预留合计 10 |
| unknown 状态 | 不释放预留（显式断言） |
| 确认终结（filled/cancelled） | 释放预留 |
| 持仓变化（10→8，预留 6） | 卖 2 ✅ / 卖 3 ❌ |
| 止损触发时预留已满 | ❌ 不能绕过 |

---

## 四、R-7：未知买单与止损保护

### 4.1 实现：保护绑定"实际持仓 + 版本"，不再按订单状态清除

* `register_stops()` 写入 `version`（自增）、`protected_qty`、`exit_pending=False`。
* 退出成交后**不再** `clear_stops`，改为 `_mark_stop_exit_pending()`（记录 `last_exit_client_id/status/at`）。
* 每轮 `monitor` 在读到持仓后执行 `_reconcile_stops(positions)`：

| 条件 | 动作 |
|---|---|
| 持仓未核实（接口失败/降级） | **保留**保护 + `stops_protection_anomaly` 告警 |
| 持仓 > 0（未知买单成交） | **保留**保护，`protected_qty` 重算为实际持仓，`exit_pending=False`，记 `stops_retained` |
| 持仓 = 0 且无未终结买单 | 才允许解除，记 `stops_cleared_after_flat` |
| 持仓 = 0 但仍有未终结买单 | 不解除 |
| 意图表读不出来 | 视为"有未终结买单" → 不解除 |

### 4.2 测试覆盖

全成交 / 部分成交（持仓 4）/ 撤单 / 重复回报（重复 monitor）/ 重启（`stops.json` 保留
`protected_qty`）/ 持仓接口失败。**未知买单成交后止损不再丢失**（实测 `protected_qty=10`）。

---

## 五、R-4：异常时间戳（开仓 fail-closed）

| ts 情况 | 开仓 | 减仓 |
|---|---|---|
| 空字符串 / 缺失（None） | ❌ `duplicate_order`（"无法确认冷却状态"） | ✅ 放行 |
| 无法解析 | ❌ 同上 | ✅ |
| 无时区 | ❌ 同上 | ✅ |
| 未来 > skew | ✅ 放行 + `order_timestamp_anomaly` | ✅ |
| 正常近期 | ❌ 冷却拦截（未删除冷却） | ✅ |

实现：`_duplicate_check_detail` 在 `problem` 且 `not is_exit` 时**直接返回阻断理由**
（此前是 `continue`，等价于"没有冷却记录"）。

生产路径验证：`test_r4_bad_ts_blocks_entry_through_engine` —— 库里存在 `ts="???"` 的历史行时，
`analyze_symbol` 被拒且券商收到 0 笔。

---

## 六、R-3 / R-8：只分析，不启用

* `risk.halt_write_failure_exit_mode` 保持 `"block"`（配置文件与代码默认均未改动）。
* 未新增任何审批/开关逻辑。
* 设计与测试方案见 `US_P1_BATCH2_REDUCE_ONLY_DESIGN.md`（含真实调用顺序分析与可达性结论）。

---

## 七、数据库 schema 与迁移兼容性

```sql
CREATE TABLE IF NOT EXISTS order_intents ( ... 22 列 ... );
CREATE INDEX IF NOT EXISTS idx_intents_symbol ON order_intents(symbol);
CREATE INDEX IF NOT EXISTS idx_intents_state  ON order_intents(state);
```

**兼容性（在真实库的副本上实测）**：

```
迁移前: orders=0 signals=36 risk_decisions=8 audit_log=68 order_attempts=0
迁移后: orders=0 signals=36 risk_decisions=8 audit_log=68 order_attempts=0
行数完全一致: True
新表/索引已创建: order_intents / idx_intents_symbol / idx_intents_state
库文件: 139264 → 163840 字节
真实库 data/audit.db: 未打开写，mtime 仍 2026-11-08 13:00
```

* 纯增量：不 `ALTER` 既有表、不删表、不重建索引、不改历史行。
* 旧版本代码打开新库：多出的表被忽略，功能照旧（前向兼容）。
* 无需数据回填：历史订单没有意图行，按"仅存在于 orders 表"的兼容分支处理。

---

## 八、修改文件清单（逐文件原因）

| # | 文件 | +/- | 原因 |
|---|---|---|---|
| 1 | `src/ai_trader/audit/journal.py` | +243 / -0 | 新增 `order_intents` 表与索引；`prewrite_intent`（含原子卖量预留）、`update_intent`、`get_intent`、`list_open_intents`、`open_sell_reserved`、`recover_open_intents`、`sync_order_status_for_client`；意图状态常量 |
| 2 | `src/ai_trader/strategy/engine.py` | +483 / -51 | `_execute` 加入预写门禁与结果结算；新增 `_recover_order_intents`/`_prewrite_intent`/`_jupdate_intent`/`_mark_intent_unknown`/`_settle_intent_from_result`/`_load_sell_reservations`/`_recent_orders_for`；`_reconcile_pending_unknown` 改为意图驱动；`monitor` 增加止损重算；`_reconcile_stops`/`_mark_stop_exit_pending`（R-7）；Portfolio 透传预留 |
| 3 | `src/ai_trader/risk/controller.py` | +62 / -5 | `Portfolio` 新增 `open_sell_reserved`/`reservations_known`/`intents_available` 与 `reserved_sell_qty`/`sellable_now`；新增 `sell_capacity` 检查（对所有卖出）；`_reduce_only_gate` 改用可卖余量；时间戳异常对开仓 fail-closed（R-4） |
| 4 | `src/ai_trader/audit/faults.py` | +3 / -0 | 新增故障类别 `order_intent_failed` / `sell_reservation_unverified` / `stops_protection_anomaly` |
| 5 | `tests/test_engine_flow.py` | +6 / -0 | 止损用例改为"两段式解除"断言（R-7 语义） |
| 6 | `tests/test_ai_authority.py` | +3 / -0 | scenario_09 同上 |
| 7 | `tests/test_us_p1_batch2.py` | **新增 692** | 36 个验收用例（R-1/R-2/R-7/R-4） |

SDK：**0 变更**。配置：**0 变更**（`halt_write_failure_exit_mode` 仍 `block`）。

---

## 九、残留风险与仍然未解决的外部依赖

| # | 项 | 级别 | 说明 |
|---|---|---|---|
| S-1 | **数据库故障期间无法止损** | 高（设计取舍） | R-2 要求"预留量读不出来就禁止卖出"，因此 DB 故障时止损会被 `reduction_blocked_unverified` 拦下。缓解：故障写独立通道 + `FAULT.json` + `alerts.log`；需要运维快速恢复 DB。此前的 batch1 行为（放行减仓）存在超卖风险，已按复核要求收紧 |
| S-2 | 券商侧无 reduce-only 原语 | 未解决（外部） | 本批只保证"**本系统**不会重复卖出"，无法阻止**系统外**（人工/其他终端）下的卖单。不能宣称彻底防超卖 |
| S-3 | 券商按 client_id 查询能力未验证 | 未解决（外部） | 代码支持"只有 client_id 也能对账"；若网关不支持，会持续 unknown 并永久阻断（符合要求 9，但需人工介入） |
| S-4 | 意图表无保留策略 | 低 | 长期运行会增长（每单 1 行）；本轮不做清理，避免误删恢复依据 |
| S-5 | 重启会把 `accepted/submitted` 一律降级为 unknown | 低（偏保守） | 即使券商侧有明确 order_id，重启后也强制重新对账；会增加一次对账开销，但不会误判 |
| S-6 | 同一 `decision_id` 重复提交被**永久阻断** | 低（设计） | `client_id` 由 decision_id 派生，重复即视为同单重发；新决策会得到新 client_id |
| S-7 | reduce-only 通道仍不可达 | 沿用 | 见 R-3/R-8 设计文档，未启用 |

---

## 十、回滚方式

```powershell
$B = "C:\FosunAI-Trading\backups\us_p1_b2_20261109-151512"

Copy-Item "$B\src\*"    "C:\FosunAI-Trading\src\"    -Recurse -Force
Copy-Item "$B\tests\*"  "C:\FosunAI-Trading\tests\"  -Recurse -Force
Remove-Item C:\FosunAI-Trading\tests\test_us_p1_batch2.py

# 校验
python -c "import hashlib,json,pathlib;m=json.load(open(r'$B\MANIFEST.sha256.json'));f=m['files'];print('差异:',[k for k,v in f.items() if hashlib.sha256(pathlib.Path(k).read_bytes()).hexdigest()!=v])"
```

**数据库回滚**：无需回滚。新增的 `order_intents` 表对旧代码无影响（旧代码不读它）；
如需彻底清除：`DROP TABLE order_intents;`（不影响任何历史数据）。
**注意**：回滚代码后，重启恢复能力随之消失，请勿在存在未对清订单时回滚。

---

## 十一、是否可进入独立复核

**可以。** `423 passed / 1 skipped / 0 failed`；R-1/R-2/R-7/R-4 均有真实生产路径测试；
并发（6 进程）与重启恢复均已实测；SDK 与配置零改动；三开关保持 `false/false/true`。

详细测试证据见 `US_P1_BATCH2_TEST_REPORT.md`；R-3/R-8 分析见 `US_P1_BATCH2_REDUCE_ONLY_DESIGN.md`。
