# US_P1_BATCH1_TEST_REPORT.md —— 交易安全补强（第一批）测试报告

**项目**：`C:\FosunAI-Trading`
**时间**：2026-11-08 20:19 – 2026-11-09 12:55 (+08:00)
**解释器**：`.venv\Scripts\python.exe`（Python 3.13.14 / Windows）
**原则**：所有数字均为**真实命令输出**；未 mock 掉被测逻辑本身。

**环境真实性核对**：

| 项 | 做法 |
|---|---|
| 数据库 | 真实 SQLite 文件（临时目录），真实 `sqlite3.OperationalError` |
| 文件系统 | 真实文件落盘（`faults.jsonl` / `FAULT.json` / `alerts.log` / `HALT.json`） |
| 时区 | 真实 `zoneinfo` + `tzdata 2026.6`，时间全部带时区 |
| 故障注入 | 子类只让**指定方法**抛错（`FlakyJournal`），其余走生产代码 |
| 券商 | `Broker` 契约替身（禁止真实下单），不是 `unittest.mock` |

---

## 1. 总结果

```
$ .venv\Scripts\python.exe -m pytest tests/ -o addopts="" -q
387 passed, 1 skipped, 1 warning in 33.84s
```

| 指标 | 本轮开始 | 本轮结束 | 变化 |
|---|---|---|---|
| 用例总数 | 347 | **388** | +41 |
| 通过 | 346 | **387** | +41 |
| 跳过 | 1 | 1 | 0 |
| 失败 | 0 | **0** | 0 |

跳过项：`test_ai.py::test_llm_real_call_if_key_present`（未配置 AI Key，合理跳过）。

按文件分布（`--collect-only` 实测）：

```
     41 tests/test_us_p1_batch1.py     ← 本轮新增
     38 tests/test_us_p0.py
     34 tests/test_risk_controls.py
     32 tests/test_market_rules_b4.py
     27 tests/test_secrets_and_gate.py
     20 tests/test_market_feed_b5.py
     17 tests/test_timeout_and_reconnect.py
     17 tests/test_ai_authority.py
     17 tests/test_ai.py
     15 tests/test_web.py
     15 tests/test_market_data.py
     15 tests/test_long_running.py
     14 tests/test_order_state_integrity.py
     12 tests/test_paper_trading.py
     12 tests/test_halt_persistence.py
     11 tests/test_positions_and_pnl.py
     10 tests/test_engine_flow.py
     10 tests/test_cancel_chain.py
      8 tests/test_audit.py
      7 tests/test_sdk_install.py
      7 tests/test_concurrency.py
      6 tests/test_cli.py
      3 tests/test_process_restart.py
     ─────
     388 合计
```

完整日志：`audit_evidence/us_p1_b1_pytest.log`

---

## 2. 任务要求的测试项 · 逐项对照

### 2.1 N-1：数据库五类局部故障

| 要求 | 用例 | 结果 |
|---|---|---|
| `log` 失败 | `test_monitor_still_liquidates_when_db_method_fails[log]` | PASS |
| `find_orders` 失败 | `...[find_orders]` | PASS |
| `list_orders` 失败 | `...[list_orders]` | PASS |
| `orders_since` 失败 | `...[orders_since]` | PASS |
| 多方法同时失败 | `test_monitor_survives_all_db_read_methods_failing` | PASS |
| 故障后仍能检测持仓风险 | 上述 5 条都断言"券商收到 sell 委托" | PASS |
| 独立持久化 + 外部告警 | `test_fault_channel_is_independent_and_persistent` | PASS |
| 不得转成空持仓 | `test_db_failure_is_not_treated_as_empty_positions` | PASS |
| 订单历史不可读禁开仓 | `test_orders_history_unreadable_blocks_entry` | PASS |
| 落库失败要 critical | `test_record_order_failure_is_critical_fault` | PASS |

### 2.2 N-2：未知订单

| 要求 | 用例 | 结果 |
|---|---|---|
| 未知买入单不阻断减仓 | `test_unknown_buy_allows_reduce_only_exit` | PASS |
| 退出数量 ≤ 真实持仓 | `test_unknown_buy_blocks_exit_over_held_qty` | PASS |
| 持仓未核实不得减仓 | `test_unknown_buy_blocks_exit_when_positions_unverified` | PASS |
| 未知卖出单 → 再卖阻断 | `test_unknown_sell_blocks_another_sell` | PASS |
| 未知单期间禁止开仓 | `test_unknown_order_blocks_new_entry` | PASS |
| 持仓为 0 不得减仓（防反向） | `test_reduce_only_exit_requires_holdings` | PASS |
| 退出授权需留审计 | `test_reduce_only_exit_is_audited` | PASS |
| 对账成功/失败 | 既有 `test_order_state_integrity.py`（14 项） | PASS |

### 2.3 N-3：急停存储

| 要求 | 用例 | 结果 |
|---|---|---|
| 读取失败（文件损坏） | `test_halt_read_failure_blocks_everything`（`kind=corrupt`） | PASS |
| 读取失败连减仓也阻断 | 同上（断言 `halt_storage_read` 在减仓的 failed_checks 里） | PASS |
| 写入失败默认阻断 | `test_halt_write_failure_blocks_by_default`（`kind=dir_failed`） | PASS |
| reduce-only 通道结构 | `test_halt_write_failure_reduce_only_channel_structure` | PASS |
| 读写标志不互相污染 | `test_halt_read_and_write_flags_do_not_leak` | PASS |
| 跨进程重启仍停止 | 既有 `test_process_restart.py`（3 项，真实子进程） | PASS |

### 2.4 N-4：时间戳 / 冷却

| 要求 | 用例 | 结果 |
|---|---|---|
| 未来时间戳不阻断止损 | `test_future_timestamp_does_not_block_stop_loss` | PASS |
| 未来时间戳不永久卡死开仓 | `test_future_timestamp_does_not_block_entry_forever` | PASS |
| 冷却逻辑未被删除 | `test_recent_timestamp_still_blocks_entry` | PASS |
| 无时区时间戳告警 | `test_naive_timestamp_is_flagged_but_usable` | PASS |
| 无法解析时间戳不崩 | `test_unparsable_timestamp_is_skipped_not_crashing` | PASS |
| 减仓不受时间冷却 | `test_exit_not_blocked_by_time_cooldown` | PASS |

### 2.5 配置守卫

| 要求 | 用例 | 结果 |
|---|---|---|
| 生产配置包含 us 且通过 | `test_production_config_passes_guard` | PASS |
| 生产策略为 reject | `test_production_config_is_reject_policy` | PASS |
| 模板与生产一致 | `test_template_matches_production_semantics` | PASS |
| `enabled=false` 拒绝 | `test_guard_rejects_unsafe_config[over0-…RULES_DISABLED]` | PASS |
| `enforce=false` 拒绝 | `…[over1-…RULES_NOT_ENFORCED]` | PASS |
| us 不在 enforce_markets | `…[over2-…MARKET_NOT_ENFORCED]` | PASS |
| policy=warn 拒绝 | `…[over3-…POLICY_NOT_REJECT]` | PASS |
| 时段为空/缺失拒绝 | `…[over4/over5-…SESSION_MISSING]` | PASS |
| 日历过期拒绝 | `…[over6-…CALENDAR_EXPIRED]` | PASS |
| 板块未验证 | `test_guard_rejects_unverified_board` | PASS |
| CLI 拒绝启动（退出码 4） | `test_guard_blocks_scan_cli` | PASS |
| worker 首步守卫 | `test_worker_script_runs_guard_before_trading` | PASS |
| reject 真的拒绝未建模市场 | `test_reject_policy_really_rejects_unmodelled_market` | PASS |
| 美股仍完整校验 | `test_us_market_still_fully_enforced` | PASS |

### 2.6 原有全部回归

23 个测试文件、347 项既有用例**全部保留并通过**（未删除、未跳过、未重置）。
`test_risk_controls`（34）、`test_market_rules_b4`（32）、`test_market_feed_b5`（20）、
`test_ai_authority`（17）、`test_concurrency`（7）、`test_halt_persistence`（12）等
A股/港股相关用例一项未删。

---

## 3. 关键场景实测

### 3.1 最坏情况：6 个数据库方法同时故障（`audit_evidence/us_p1_b1_fault_demo.txt`）

```
[场景] journal.log/find_orders/list_orders/orders_since/record_risk_decision 全部抛错
monitor 返回: ['phase_unavailable', 'phase_unavailable', 'exit']
券商收到委托: [{'symbol': 'usAAPL', 'side': 'sell', 'qty': 10.0, ...}]
故障计数: {'order_read_failed': 5, 'audit_write_failed': 3}

--- data/faults.jsonl（独立于 SQLite）---
{"ts":"…","kind":"order_read_failed","severity":"error","detail":{"op":"find_orders",…}}
…
{"ts":"…","kind":"audit_write_failed","severity":"critical",
 "detail":{"event":"record_order","order_id":"STUB-1","symbol":"usAAPL",
           "error":"OperationalError: …","detail":"订单已提交但落库失败 —— 必须人工核对券商侧订单"}}

--- data/FAULT.json ---
{"latest":{"kind":"audit_write_failed","severity":"critical",…},"counts":{…}}

--- logs/alerts.log ---
2026-11-09T12:48:59+08:00 ALERT severity=error kind=order_read_failed detail={…}
…（共 8 条）
```

**结论**：数据库全坏时，止损仍然发出；每一步失败都被独立记录，且"订单已提交但
落库失败"升级为 **critical**。

### 3.2 未知买单期间的止损

```
test_unknown_buy_allows_reduce_only_exit
  → approved=True
  → checks["unresolved_order_state"].detail 含 "reduce-only"
test_reduce_only_exit_is_audited
  → 审计出现 exit_authorized_reduce_only
```

### 3.3 急停读取失败 vs 写入失败

```
读取失败（HALT.json 内容为 "{not json"）
  → halt_read_failed=True, halt_error_kind="corrupt"
  → 买入 failed_checks 含 halt_storage_read
  → 卖出（止损）failed_checks 也含 halt_storage_read  ← 全阻断

写入失败（data_dir 指向一个文件）
  → HaltStoreError.kind="dir_failed"
  → halt_write_failed=True, halt_read_failed=False
  → 默认 failed_checks 含 halt_storage_write
  → 显式开启 reduce_only 后：sell 通过、超额 sell 拒绝、buy 仍拒绝
```

### 3.4 未来时间戳

```
ts = 2027-06-01（相对 now 未来 200 天）
  → 减仓 approved=True，checks 含 order_timestamp_anomaly（"…时间戳在未来…"）
  → 开仓 approved=True，但同样带 anomaly 检查（不再永久卡死）
对照：ts = now-10s 的买单 → 开仓 approved=False（duplicate_order）  ← 冷却未被删除
```

### 3.5 配置守卫退出码（真实子进程）

```
  正常               exit=0
  enabled=false    exit=4  GUARD_RULES_DISABLED
  enforce=false    exit=4  GUARD_RULES_NOT_ENFORCED
  us 不在 enforce    exit=4  GUARD_MARKET_NOT_ENFORCED
  policy=warn      exit=4  GUARD_UNENFORCED_POLICY_NOT_REJECT
  删除 sessions.us   exit=4  GUARD_SESSION_MISSING
```

---

## 4. 变异检验（证明测试不是空转）

`audit_evidence/mutation_plugin.py` 在**内存中**注入"旧行为"，不改产品代码：

```powershell
$env:PYTHONPATH="audit_evidence"; $env:AUDIT_MUTATE="<mode>"
python -m pytest tests/test_us_p1_batch1.py -o addopts="" -q -p mutation_plugin
```

| 变异 | 注入的错误行为 | 结果 |
|---|---|---|
| `p1_monitor_no_isolation` | `_jlog` 重新变成"失败就抛"（旧行为） | **1 failed** / 40 passed |
| `p1_unresolved_blocks_all` | 未知单一律阻断（旧行为，不区分方向） | **5 failed** / 36 passed |
| `p1_halt_flags_merged` | 急停读写故障合并为单一 `halt_storage` 检查 | **1 failed** / 40 passed |
| `p1_dup_no_upper_bound` | 冷却窗口恢复"只判下界"（未来时间戳永久卡死） | **5 failed** / 36 passed |

**4/4 被抓住**。日志：`audit_evidence/us_p1_b1_mutation.log`

---

## 5. 对既有测试的改动（全部逐条说明，未降低标准）

| 文件 | 改动 | 原因 | 是否削弱断言 |
|---|---|---|---|
| `tests/conftest.py` | 新增 `autouse` 会话夹具固定合成行情参考日；`make_cfg` 显式声明 `unenforced_market_policy="warn"` | ① 原套件**存在 10 个日期依赖用例**（见 §6）；② 生产改 `reject` 后，用 hk 当通用标的的旧用例会误伤，显式声明保留其原意 | ❌ 未削弱：日期依赖是**缺陷修复**；warn 仅测试夹具，生产 reject 有专项断言 |
| `tests/helpers.py` | `live_cfg` 同上；`ScriptedBroker.get_positions(**kw)` 记录 kwargs | 与 conftest 一致 | ❌ |
| `tests/test_halt_persistence.py` | `assert "halt_storage"` → `assert "halt_storage_write"` + 断言 `halt_read_failed is False` | 检查项按读写拆分（更细） | ❌ 增强（多断言一条） |
| `tests/test_process_restart.py` | 配置改为"基于生产配置 + 覆盖路径"，不再手写最小配置 | `scan` 现在有守卫前置，最小配置会被正确拒绝 | ❌ 增强（顺带验证守卫放行时跨进程行为不变） |
| `tests/test_web.py` | `client` 夹具把 policy 切到 `reject` | `/api/scan` 有守卫；生产即 reject | ❌ 增强（改走生产口径） |

---

## 6. 发现的既有缺陷：测试套件的日期依赖性

用**修改前**的代码跑全量：

```
$ PYTHONPATH=backups/us_p1_b1_20261108-201935/src pytest tests/ -q
10 failed, 336 passed, 1 skipped
```

根因：`SyntheticQuoteSource` 以 `date.today()` 播种，`quote()` 取 30 步随机游走末值：

* 某些日期合成出"空头排列" → `HeuristicModel` 返回 `hold` →
  `test_config_must_explicitly_allow_rule_engine` 等 4 项失败；
* 某些日期价格偏离 `FixedSignalModel(entry=300)` 超过 5% → 被风控按"错价"拒绝 →
  `test_scenario_01` 等 4 项失败；
* 并发/推送用例同样受影响（2 项）。

即：这套测试**只在特定日历日通过**。本轮用 `reference_day` 固定种子修复，
使结果与运行日期无关（生产/演示仍按当天变化）。

---

## 7. 安全边界核对

```
live_trading            = False
order_execution_enabled = False
paper_trading           = True
kill_switch             = False
halt_store_failed/read_failed/write_failed = False/False/False
can_send_real_order     = (False, 'PAPER_TRADING=true，真实券商连接被禁用')

SDK：26 文件，变更 0（一字节未动）
data/：audit.db mtime 仍为 2026-11-08 13:00（本轮未写）
.env ：mtime 仍为 2026-11-08 11:57（本轮未读内容/未写）
vault：不存在
Git  ：未初始化
```

* 未连接真实账户（所有券商交互为进程内契约替身）
* 未发送任何真实委托（`place_order` 全部落在替身/模拟盘）
* 未索取或输出任何 API 密钥、私钥
* 未覆盖 `data` / `.env` / `vault`
* 未删除任何旧市场实现（A 股 32 项、港股相关用例全部保留）

---

## 8. 复现步骤

```powershell
cd C:\FosunAI-Trading

# 1) 全量回归
.\.venv\Scripts\python.exe -m pytest tests/ -o addopts="" -q

# 2) 本批专项
.\.venv\Scripts\python.exe -m pytest tests/test_us_p1_batch1.py -o addopts="" -v

# 3) 配置守卫（生产配置应 exit=0）
.\.venv\Scripts\python.exe -m ai_trader.cli guard

# 4) 故障隔离演示（打印 faults.jsonl / FAULT.json / alerts.log）
#    见 audit_evidence/us_p1_b1_fault_demo.txt 的生成脚本（同文件头部说明）

# 5) 变异检验
$env:PYTHONPATH="audit_evidence"; $env:AUDIT_MUTATE="p1_unresolved_blocks_all"
.\.venv\Scripts\python.exe -m pytest tests/test_us_p1_batch1.py -o addopts="" -q -p mutation_plugin
```

说明：`tests/conftest.py` 的默认 `addopts` 会抑制汇总行，复现时加 `-o addopts=""`。

---

## 9. 证据文件索引

| 文件 | 内容 |
|---|---|
| `audit_evidence/us_p1_b1_pytest.log` | 全量 `-v` 日志（388 项逐条） |
| `audit_evidence/us_p1_b1_mutation.log` | 4 个变异模式实测输出 |
| `audit_evidence/us_p1_b1_fault_demo.txt` | 6 个 DB 方法同时故障的完整证据 |
| `audit_evidence/us_p1_b1_check_env.txt` | `check-env` 完整 JSON（含 `config_guard`） |
| `audit_evidence/sdk_baseline_us_p1_b1.json` | SDK 26 文件 SHA256（本轮基线） |
| `backups/us_p1_b1_20261108-201935/MANIFEST.sha256.json` | 修改前 72 文件哈希（回滚用） |

---

## 10. 结论

**387 passed / 1 skipped / 0 failed；4/4 变异被抓住；SDK 零改动；
安全开关保持关闭；未触碰 data/.env/vault。允许进入独立只读复核。**
