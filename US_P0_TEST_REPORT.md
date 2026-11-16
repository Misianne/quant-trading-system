# US_P0_TEST_REPORT.md —— 美股 P0 修复测试报告

**项目**：`C:\FosunAI-Trading`
**时间**：2026-11-08 18:38 – 19:12 (+08:00)
**解释器**：`C:\FosunAI-Trading\.venv\Scripts\python.exe`（Python 3.13.14，Windows）
**原则**：所有结果均为**真实命令输出**，未做任何修饰或伪造。
**禁止事项核对**：未 mock 时区（用真实 `zoneinfo` + 真实 `tzdata 2026.6`）、
未 mock 数据库（用真实 SQLite 文件）、未 mock 并发（真实线程/进程）。

---

## 1. 总结果

```
$ .venv\Scripts\python.exe -m pytest tests/ -o addopts="" -q
346 passed, 1 skipped, 1 warning in 35.13s
```

| 指标 | 阶段开始时（基线） | 修复后 | 变化 |
|---|---|---|---|
| 用例总数 | 309 | **347** | +38 |
| 通过 | 308 | **346** | +38 |
| 跳过 | 1 | 1 | 0 |
| 失败 | 0 | **0** | 0 |

跳过项为既有 `test_sdk_install.py` 的网络依赖项（需真实凭证），非本轮引入。

完整日志：`audit_evidence/us_p0_pytest_full.log`（`-v` 逐条输出）
US-P0 专项日志：`audit_evidence/us_p0_pytest_us.log`

按文件分布（`--collect-only` 实测）：

```
     38 tests/test_us_p0.py          ← 本轮新增
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
     347 合计
```

---

## 2. 任务要求的 12 项测试 · 逐项对照

| 要求 | 对应测试 | 结果 |
|---|---|---|
| ① DST 常规开盘边界 | `test_dst_regular_open_boundary`、`test_dst_beijing_equivalents`、`test_dst_transition_week_does_not_drift` | PASS |
| ② 冬令时常规开盘边界 | `test_standard_time_regular_open_boundary` | PASS |
| ③ 北京周六 = 纽约周五 | `test_beijing_saturday_is_new_york_friday`、`test_new_york_saturday_is_closed` | PASS |
| ④ 北京周一 = 纽约周日 | `test_beijing_monday_is_new_york_sunday` | PASS |
| ⑤ 美股提前收市日 | `test_us_early_close_day`、`test_us_holiday_and_calendar_coverage` | PASS |
| ⑥ 盘前持仓进入风控 | `test_all_session_positions_enter_risk_control[pre]` | PASS |
| ⑦ 盘后持仓进入风控 | `test_all_session_positions_enter_risk_control[post]` | PASS |
| ⑧ 夜盘持仓进入风控 | `test_all_session_positions_enter_risk_control[night]` | PASS |
| ⑨ 持仓查询异常 | `test_positions_query_exception_blocks_new_entries`、`test_positions_query_exception_still_allows_stop_loss` | PASS |
| ⑩ 时段服务异常 | `test_session_service_exception_is_fail_closed`、`test_clock_data_missing_is_fail_closed` | PASS |
| ⑪ 规则未配置必须拒绝 | `test_unresolvable_symbol_rejected`、`test_unknown_board_status_rejected`、`test_unverified_board_rejected_when_not_allowed`、`test_unenforced_market_cannot_bypass_rules` | PASS |
| ⑫ A 股/港股旧测试回归 | `test_cn_t_plus_one_regression`、`test_cn_session_still_by_shanghai_time`、`test_hk_session_still_by_hong_kong_time`、`test_is_market_open_helper_uses_exchange_time` + 全部既有 309 项 | PASS |

额外补充（12 项之外，为验证修复而不产生假阳性）：

| # | 用例 | 验证点 |
|---|---|---|
| 13 | `test_mixed_session_positions_all_counted` | 四个时段持仓同时存在时，持仓数=4 且 session 标签齐全 |
| 14 | `test_session_phases_are_recognised` | pre/regular/post/night 与跨午夜夜盘阶段判定 |
| 15 | `test_quote_failure_uses_position_mark_for_stop` | 行情失败 → 用券商持仓 mark 触发止损 |
| 16 | `test_us_rules_really_enforced` | tick/数量/时段在 `applied=True` 下真生效 |
| 17 | `test_us_exit_not_blocked_by_session_or_calendar` | 圣诞节当天减仓不被规则层拦 |
| 18 | `test_positions_passes_all_us_session_flags` | `useUsPre/Post/Night` 全 True 送达官方接口 |
| 19 | `test_positions_flags_can_be_overridden_and_warned` | 关掉某段 → health 必须告警 |
| 20 | `test_daily_loss_does_not_block_exit` | 已触及日亏上限，止损仍放行 |
| 21 | `test_unknown_day_pnl_does_not_block_exit` | dayPnl 缺失，止损仍放行 |
| 22 | `test_degraded_portfolio_allows_exit_but_blocks_entry` | 降级快照：可平仓、禁开仓 |
| 23 | `test_exit_allowed_when_market_disabled` | 市场停用，止损仍放行 |
| 24 | `test_entry_blocked_outside_us_session` | 盘前 09:00 ET 拒绝开仓 |

---

## 3. 关键用例的实测行为

### 3.1 US-P0-1：时区与 DST

```
# 真实命令：python audit_evidence/probe_us_p0.py
本机时区      : 中国标准时间 (UTC8:00:00)
tzdata 版本   : 2026.6
纽约 2026-07-15 12:00 : 2026-07-15 12:00:00-04:00     ← EDT
纽约 2026-11-04 12:00 : 2026-11-04 12:00:00-05:00     ← EST
DST 切换（2026-11-01 01:30 当地）: -04:00
DST 切换（2026-11-01 03:00 当地）: -05:00
```

**旧实现 vs 新实现**（观察轴 = 北京时间，2026-11-04 周三，EST）：

```
北京时间            → 纽约时间                旧判定   新判定   差异
11-04 21:00        11-04 08:00 EST          休市    休市
11-04 21:30        11-04 08:30 EST          开市    休市    ← 不一致
11-04 22:00        11-04 09:00 EST          开市    休市    ← 不一致
11-04 22:29        11-04 09:29 EST          开市    休市    ← 不一致
11-04 22:30        11-04 09:30 EST          开市    开市
11-04 23:00        11-04 10:00 EST          开市    开市
11-04 23:59        11-04 10:59 EST          开市    开市
11-05 00:30        11-04 11:30 EST          开市    开市
11-05 03:59        11-04 14:59 EST          开市    开市
11-05 04:00        11-04 15:00 EST          开市    开市
11-05 04:30        11-04 15:30 EST          休市    开市    ← 不一致
11-05 05:00        11-04 16:00 EST          休市    休市
不一致时点数：4
```

**跨星期归属**：

```
北京 2026-11-07(周六) 02:00  → 纽约 2026-11-06 Fri 13:00   旧=休市  新=开市
北京 2026-11-09(周一) 02:00  → 纽约 2026-11-08 Sun 13:00   旧=开市  新=休市
```

### 3.2 提前收市（2026-11-27，1:00 p.m. ET）

```
纽约 12:59 → phase=regular  regular_open=True  closes_at=13:00 (常规时段 09:30-13:00)
纽约 13:00 → phase=post     regular_open=False closes_at=13:00 (盘后 13:00-20:00)
纽约 15:00 → phase=post     regular_open=False closes_at=13:00 (盘后 13:00-20:00)
```

### 3.3 US-P0-3：持仓时段开关

```
# 真实命令：python -m ai_trader.cli check-env
"position_sessions": {
  "use_us_pre": true,
  "use_us_post": true,
  "use_us_night": true,
  "last_query": {},
  "warning": ""
}
```

`test_positions_flags_can_be_overridden_and_warned` 把 `use_us_night` 置 false 后：

```
h["position_sessions"]["use_us_night"] == False
h["position_sessions"]["warning"] != ""      ← "有盘前/盘后/夜盘持仓未被查询 —— 那些持仓将脱离风控"
```

### 3.4 US-P0-4：降级路径

| 场景 | 断言 |
|---|---|
| 持仓接口抛 `BrokerError` | `scan().blocked_reason` 含"持仓读取失败"；`place` 调用次数 = 0 |
| 持仓接口抛错 + 已有快照 | `monitor()` 返回 `positions_unavailable`，**仍发出 sell**（`last_request.side == "sell"`），`_positions_stale=True` |
| 行情接口抛错 + 持仓有 mark | 用 `source="position_mark"` 的 Quote 判定止损，触发 `exit` |
| 已触及日亏上限 + 减仓 | `approved=True` |
| `day_pnl_known=False` + 减仓 | `approved=True` |
| 降级 Portfolio + 开仓 | `approved=False`，`portfolio_integrity` 在 `failed_checks` |
| 市场 `enabled=false` + 减仓 | `approved=True` |
| 纽约 09:00（盘前）+ 开仓 | `approved=False`，`market_hours` 在 `failed_checks` |

### 3.5 US-P0-2：价格精度

```
price=150.003, tick=0.01   → PRICE_TICK 违规（不是 0.01 的整数倍）
price=150.000              → 通过
price=0.5001,  tick<1=0.0001 → 通过
price=0.50005              → PRICE_TICK 违规
```

---

## 4. 变异检验（证明测试不是空转）

用 `audit_evidence/mutation_plugin.py` 在**内存中注入错误实现**，不改产品代码。
命令模板：

```powershell
$env:PYTHONPATH="audit_evidence"; $env:AUDIT_MUTATE="<mode>"
python -m pytest tests/test_us_p0.py -o addopts="" -q -p mutation_plugin
```

实测结果（`audit_evidence/us_p0_mutation.log`）：

| 变异模式 | 注入的错误行为 | 结果 |
|---|---|---|
| `us_beijing_hardcoded_window` | 美股退回"北京 21:30–04:00 + 北京星期几" | **10 failed** / 28 passed |
| `us_clock_ignores_timezone` | `ExchangeClock.local()` 忽略时区，直接返回本机时间 | **6 failed** / 32 passed |
| `us_positions_drop_session_flags` | 持仓查询丢弃 `useUsPre/Post/Night` | **1 failed** / 37 passed |
| `us_exit_blocked_by_session` | 减仓不再豁免时段/日历（撤销 US-P0-4 修复） | **2 failed** / 36 passed |

**4/4 被抓住**，说明新增测试与真实行为耦合。

---

## 5. 第三方 SDK 完整性

```
SDK 基线文件数: 26
不一致: 无（SDK 一个字节都没动）
```

比对对象：`audit_evidence/sdk_baseline.json`（`sdk_src/fsopenapi/**` 26 个 .py 的 sha256）。
本轮**没有**修改 `sdk_src/` 下任何文件，行情加密问题仍保持"客户端绕行 + 显式标注"的既有处理。

---

## 6. 安全开关与副作用核对

```
# python -m ai_trader.cli check-env
"safety": {
  "live_trading": false,
  "order_execution_enabled": false,
  "paper_trading": true,
  "kill_switch": false,
  "live_ack_satisfied": false,
  "real_order_authorized": false
}
"fosun": { "gate_allows_connect": false,
           "gate_reason": "PAPER_TRADING=true，真实券商连接被禁用",
           "configured": false, "connected": false,
           "can_send_real_order": false,
           "sdk_modified": false }
```

* 未连接真实账户（`configured=false`、`connected=false`）
* 未发出任何真实委托（所有下单走 `ScriptedBroker` / `PaperBroker` 替身）
* 未生成假券商订单号（P0-1 的 `OrderStateUnknown` 路径未改动）
* 未修改 `data/`、`.env`、`vault.json`
* 未下载/更换模型、未改动任何 API 凭证

---

## 7. 复现步骤

```powershell
cd C:\FosunAI-Trading

# 0) 前置：Windows 需要 IANA 时区数据（本轮已装 tzdata 2026.6）
.\.venv\Scripts\python.exe -m pip show tzdata

# 1) 全量测试
.\.venv\Scripts\python.exe -m pytest tests/ -o addopts="" -q

# 2) 只跑 US-P0
.\.venv\Scripts\python.exe -m pytest tests/test_us_p0.py -o addopts="" -v

# 3) 时区/DST/新旧判定对照探针（只读、不联网）
.\.venv\Scripts\python.exe audit_evidence/probe_us_p0.py

# 4) 环境自检（安全开关、时段、规则来源、持仓开关）
.\.venv\Scripts\python.exe -m ai_trader.cli check-env

# 5) 变异检验（示例）
$env:PYTHONPATH="audit_evidence"; $env:AUDIT_MUTATE="us_beijing_hardcoded_window"
.\.venv\Scripts\python.exe -m pytest tests/test_us_p0.py -o addopts="" -q -p mutation_plugin
```

说明：`tests/conftest.py` 的默认 `addopts` 会抑制汇总行，因此复现时显式加
`-o addopts=""` 才能看到 `N passed` 统计。

---

## 8. 证据文件索引

| 文件 | 内容 |
|---|---|
| `audit_evidence/us_p0_pytest_full.log` | 全量 `-v` 测试日志（347 项逐条） |
| `audit_evidence/us_p0_pytest_us.log` | US-P0 专项日志（38 项） |
| `audit_evidence/us_p0_mutation.log` | 4 个变异模式的实测输出 |
| `audit_evidence/us_p0_probe_output.txt` | 时区/DST/新旧对照/配置/安全开关实测 |
| `audit_evidence/us_p0_cli_check_env.txt` | `check-env` 完整 JSON 输出 |
| `audit_evidence/probe_us_p0.py` | 探针源码（可复跑） |
| `audit_evidence/mutation_plugin.py` | 变异插件（新增 4 个 US-P0 模式） |
| `audit_evidence/sdk_baseline.json` | SDK 26 文件 sha256 基线 |
| `backups/us_p0_20261108-183854/MANIFEST.sha256.json` | 修改前 70 文件哈希清单（回滚用） |

---

## 9. 结论

**全部测试通过（346 passed / 1 skipped / 0 failed），4/4 变异被抓住，SDK 零改动，
安全开关保持关闭。允许进入独立复核。**
