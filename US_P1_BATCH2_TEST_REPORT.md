# US_P1_BATCH2_TEST_REPORT.md —— 订单不确定性与减仓幂等性 测试报告

**项目**：`C:\FosunAI-Trading`
**时间**：2026-11-09 15:15 – 16:05 (+08:00)
**解释器**：`.venv\Scripts\python.exe`（Python 3.13.14 / Windows）
**原则**：所有数字为**真实命令输出**；不使用 mock 顶替被测逻辑。

---

## 1. 总结果

```
$ .venv\Scripts\python.exe -m pytest tests/ -o addopts="" -q -p no:cacheprovider
423 passed, 1 skipped, 1 warning in 36.85s

$ ... pytest tests/test_us_p1_batch2.py -q
36 passed in 2.68s
```

| 指标 | Batch1 结束 | Batch2 结束 | 变化 |
|---|---|---|---|
| 用例总数 | 388 | **424** | +36 |
| 通过 | 387 | **423** | +36 |
| 跳过 | 1 | 1 | 0（无 AI Key） |
| 失败 | 0 | **0** | 0 |

---

## 2. 环境真实性

| 项 | 做法 |
|---|---|
| 数据库 | 真实 SQLite 文件（临时目录）；`Flaky` 只让**指定方法**抛 `sqlite3.OperationalError` |
| 券商 | `Broker` 契约子类（`RecordingBroker`），可编排 filled/unknown/timeout/reject，非 `mock` |
| 并发 | 8 线程（同进程）+ **6 个真实进程**（`subprocess`）抢同一预留 |
| 重启 | 关闭连接 → 新 `AuditJournal` + 新 `StrategyEngine`，同一 DB 文件 |
| 时区 | 真实 `zoneinfo`（`America/New_York`），时间全部带时区 |
| **mock / monkeypatch** | `tests/test_us_p1_batch2.py` 中 **0 处**（无 `unittest.mock`、无 `monkeypatch`） |

---

## 3. R-1 验收（10 项）

| # | 用例 | 验证点 | 结果 |
|---|---|---|---|
| 1 | `test_r1_prewrite_success_normal_order` | 预写成功 + 正常成交；意图终态、`request_digest/request_json` 有值、client_id 与券商一致 | PASS |
| 2 | `test_r1_prewrite_success_unknown_keeps_intent` | 券商 unknown + 空 order_id → 意图保留；monitor 二次对账后终结 | PASS |
| 3 | `test_r1_prewrite_success_timeout_marks_unknown` | broker 超时（非 BrokerError）→ 保持 `unknown` + 记录 `last_error` | PASS |
| 4 | `test_r1_broker_rejected_releases_reservation` | 明确拒单 → `rejected`，不占用预留 | PASS |
| 5 | `test_r1_record_order_failure_keeps_intent_visible` | `record_order` 失败：orders=0 行，但意图仍在且含 client_id | PASS |
| 6 | `test_r1_prewrite_failure_never_calls_broker` | 预写失败 → 券商 **0 笔**；故障类别 `order_intent_failed` | PASS |
| 7 | `test_r1_persistence_unavailable_blocks_all_entries` | 持久化不可用 → `scan` 被拒，0 委托 | PASS |
| 8 | `test_r1_crash_after_broker_before_settle_recovers_as_unknown` | 模拟"券商返回后、结算前"崩溃 → 新进程恢复为 `unknown` | PASS |
| 9 | `test_r1_after_restart_reentry_blocked_until_reconciled` | 未对账 → 开仓被 `unresolved_order_state` 拒；对账确认后解除 | PASS |
| 10 | `test_r1_two_workers_same_decision_place_once` | 同 client_id 第二次预写被 `duplicate` 拒绝（不会重复发单） | PASS |

### 3.1 复核报告五步复现（真实链路，端到端）

```
步骤1-3  下单（券商 unknown + 空 order_id），record_order 失败
  stage: ordered | 券商收到 client_id: 1490029478
  orders 行数: 0
  意图行: [('1490029478', 'unknown')]
  故障流水含 client_id: True            ← 修复前为 False

步骤4    进程重启（新连接 + 新 engine，同一 DB）
  恢复出的未终结意图: ['1490029478']
  monitor 结果: ['reconcile_failed']
  对账尝试的 client_id: ['1490029478']
  对账失败后状态: ['unknown']

步骤5    +400 秒再次开仓
  stage: rejected
  拦截项: ['unresolved_order_state', 'duplicate_order']
  券商收到新单: 无
  结论: 已阻断
```

**对照**：修复前同一场景 `find_orders(unknown)=[]`、重启后订单不存在、+400s 又下一单。

---

## 4. R-2 验收（12 项）

| # | 用例 | 验证点 | 结果 |
|---|---|---|---|
| 1 | `test_r2_hold10_reserved10_sell10_rejected` | 持仓 10 / 预留 10 / 卖 10 → `sell_capacity` 拒绝 | PASS |
| 2 | `test_r2_hold10_unknown_sell6_sell5_rejected` | 持仓 10 / 未知卖 6 / 卖 5 → 拒绝 | PASS |
| 3 | `test_r2_two_unknown_sells_merged` | 两笔未知卖 4+6 → 卖 1 也拒绝（合并计算） | PASS |
| 4 | `test_r2_partial_room_allows_partial_exit` | 预留 4 → 卖 6 ✅ / 卖 7 ❌ | PASS |
| 5 | `test_r2_reservations_unreadable_blocks_sell` | 预留读失败 → `reduction_blocked_unverified` | PASS |
| 6 | `test_r2_intents_unavailable_blocks_sell` | 意图表不可用 → 拒绝卖出 | PASS |
| 7 | `test_r2_stop_loss_cannot_bypass_capacity` | 止损优先级高也不能绕过 | PASS |
| 8 | `test_r2_atomic_reservation_single_winner` | 8 线程抢额度 → 1 成功 / 7 拒，预留合计 10 | PASS |
| 9 | `test_r2_reservation_released_only_after_confirmed_terminal` | unknown 不释放；cancelled 才释放 | PASS |
| 10 | `test_r2_rechecks_position_size_each_cycle` | 持仓 10→8、预留 6 → 卖 2 ✅ / 卖 3 ❌ | PASS |
| 11 | `test_r2_engine_blocks_second_sell_when_db_unreadable` | 生产链路：有在途卖 + 库不可读 → 0 笔新卖单 | PASS |
| 12 | `test_r2_engine_allows_exit_without_reservation` | 无在途卖 → 止损正常发出（未误杀） | PASS |

### 4.1 跨进程并发实测（6 个独立解释器进程）

```
$ python worker.py <db> p1..p6   （每个进程 prewrite sell 10，额度 10）
p1|True|ok
p2|False|capacity: 未完成卖单已预留 10 + 本单 10 > 已核实持仓 10
p3|False|capacity: ...
p4|False|capacity: ...
p5|False|capacity: ...
p6|False|capacity: ...
```

**1 成功 / 5 被拒**，由 SQLite `BEGIN IMMEDIATE` + `UNIQUE(client_id)` 保证。

### 4.2 修复前的超卖路径已被封堵

```
[修复前] 库可读 → 拦截；库不可读 → 再发 10 → 名义卖出 20 > 持仓 10
[修复后] 库不可读 → reservations_known=False → reduction_blocked_unverified
         券商收到 0 笔（test_r2_engine_blocks_second_sell_when_db_unreadable）
```

---

## 5. R-7 验收（6 项）

| # | 用例 | 验证点 | 结果 |
|---|---|---|---|
| 1 | `test_r7_exit_does_not_clear_stops_immediately` | 退出后 `exit_pending=True`，`protected_qty` 保持 | PASS |
| 2 | `test_r7_unknown_buy_filled_keeps_stop` | 未知买单成交（持仓回到 10）→ `stops_retained`，`protected_qty=10` | PASS |
| 3 | `test_r7_stops_cleared_only_after_verified_flat` | 核实清仓后才 `stops_cleared` | PASS |
| 4 | `test_r7_positions_unverified_keeps_stop` | 持仓接口失败 → 保留保护 | PASS |
| 5 | `test_r7_stops_survive_restart_with_protected_qty` | 重启后 `stops.json` 仍含 `protected_qty` | PASS |
| 6 | `test_r7_partial_exit_keeps_remaining_protected` | 部分成交（持仓 4）→ 保护数量跟随为 4 | PASS |

原有的 `test_engine_flow::test_stop_loss_monitor_sells_position` 与
`test_ai_authority::test_scenario_09` 已按两段式语义更新（**增强**断言，非放宽）。

---

## 6. R-4 验收（8 项）

| # | 用例 | 验证点 | 结果 |
|---|---|---|---|
| 1-3 | `test_r4_entry_blocked_on_unparsable_ts[空/无法解析/缺失]` | 开仓 `duplicate_order` 拒绝 + `order_timestamp_anomaly` | PASS |
| 4 | `test_r4_entry_blocked_on_naive_ts` | 无时区 → 开仓拒绝 | PASS |
| 5 | `test_r4_exit_still_allowed_with_bad_ts` | 减仓仍放行 | PASS |
| 6 | `test_r4_normal_recent_ts_still_blocks_and_old_ts_allows` | 冷却未被删除 | PASS |
| 7 | `test_r4_future_ts_isolated_not_ignored` | 未来 ts → 放行但留 anomaly | PASS |
| 8 | `test_r4_bad_ts_blocks_entry_through_engine` | 生产链路：库里有 `ts="???"` → 开仓被拒、0 委托 | PASS |

---

## 7. 并发 / 重启 / 故障注入

| 类别 | 用例 | 结果 |
|---|---|---|
| 并发（线程） | `test_r2_atomic_reservation_single_winner`（8 线程） | PASS |
| 并发（进程） | 6 个真实 `subprocess` 抢额度 | 1/5（见 §4.1） |
| 重启恢复 | `test_r1_crash_after_broker_before_settle_recovers_as_unknown`、`test_r1_after_restart_reentry_blocked_until_reconciled`、`test_r7_stops_survive_restart_with_protected_qty` | PASS |
| 故障注入 | `prewrite_intent` / `record_order` / `update_intent` / `list_open_intents` / `open_sell_reserved` / `list_orders` 逐个抛错 | PASS |

---

## 8. 新增测试是否使用 mock / monkeypatch

```
$ grep -n "monkeypatch\|unittest.mock\|Mock\|MagicMock" tests/test_us_p1_batch2.py
（无输出）
```

* `RecordingBroker` / `Flaky` 均为**真实类的子类**：前者实现完整 `Broker` 契约，
  后者只在指定方法上抛 `sqlite3.OperationalError`，其余方法走生产代码。
* 仅使用 `pytest.mark.parametrize`（参数化，非打桩）。

---

## 9. 全量回归与失败项

```
423 passed, 1 skipped, 0 failed
```

* 跳过项：`test_ai::test_llm_real_call_if_key_present`（未配置 AI Key，合理跳过）。
* 本批**无失败项**。
* 过程中被修正的 3 个既有用例（非放宽）：
  1. `test_engine_flow::test_stop_loss_monitor_sells_position` — 断言改为两段式解除（更严）；
  2. `test_ai_authority::test_scenario_09_positions_protected_while_ai_down` — 同上；
  3. `test_long_running::test_unknown_order_blocks_trading_until_reconciled` — 无需改动，
     因实现修复（orders 行与意图同步）后自然通过。

---

## 10. 迁移与数据库

```
在真实库副本上打开新版本：
迁移前: orders=0 signals=36 risk_decisions=8 audit_log=68 order_attempts=0
迁移后: orders=0 signals=36 risk_decisions=8 audit_log=68 order_attempts=0
行数完全一致: True
新表: order_intents（22 列）+ idx_intents_symbol / idx_intents_state
真实库 data/audit.db：未打开写，mtime 仍 2026-11-08 13:00
```

---

## 11. 安全开关（前后一致）

```
LIVE_TRADING            = False
ORDER_EXECUTION_ENABLED = False
PAPER_TRADING           = True
can_send_real_order     = (False, 'PAPER_TRADING=true，真实券商连接被禁用')
risk.halt_write_failure_exit_mode = 'block'   （未启用 reduce-only）
```

---

## 12. 仍未验证 / 依赖券商外部能力

| # | 项 | 说明 |
|---|---|---|
| 1 | 券商是否支持按 `client_id` 查询 | 代码已支持"仅 client_id 对账"；若网关不支持则永久 unknown（符合要求，但需人工） |
| 2 | 券商侧 reduce-only | SDK `create_order` 无该参数；本批只能保证"本系统不重复卖出" |
| 3 | 券商对"已被系统外终端下的单"是否可见 | 不可见则预留无法覆盖，存在外部超卖风险 |
| 4 | 真实网络超时/网关半开 | 仅用进程内异常模拟，未做真实网络故障注入 |
| 5 | reduce-only 通道（R-3/R-8） | 未启用，见设计文档 |

---

## 13. 复现步骤

```powershell
cd C:\FosunAI-Trading

# 全量
.\.venv\Scripts\python.exe -m pytest tests/ -o addopts="" -q -p no:cacheprovider

# 本批专项
.\.venv\Scripts\python.exe -m pytest tests/test_us_p1_batch2.py -o addopts="" -v

# 迁移兼容性（在副本上，不碰真实库）
#   见 US_P1_BATCH2_IMPLEMENTATION_REPORT.md §七 的命令
```

---

## 14. 结论

**423 passed / 1 skipped / 0 failed；36 个新用例全部走生产调用链；无 mock；
并发（线程 + 6 进程）与重启恢复均实测通过；SDK 与配置零改动；
三开关保持 false/false/true；reduce-only 未启用。**
