# US_P1_BATCH1_INDEPENDENT_REVIEW.md —— US-P1 第一批独立只读复核

**项目**：`C:\FosunAI-Trading`
**复核时间**：2026-11-09 15:00 – 15:35 (+08:00)
**复核性质**：**严格只读**。未修改/格式化/迁移/重建/覆盖任何文件；未初始化 Git；
未连接真实券商；未发送任何真实订单；未读取或输出任何密钥内容。
**依据**：`US_P1_BATCH1_IMPLEMENTATION_REPORT.md`、`US_P1_BATCH1_TEST_REPORT.md`、`INDEPENDENT_AUDIT.md §13`
**复核口径**：所有结论均由**本轮独立执行**产生，未引用实现方旧日志。

---

## 0. 复核方法

| 手段 | 说明 |
|---|---|
| 哈希快照 | 复核开始前对 127 个文件建 SHA256 快照，结束前重算比对 |
| 真实文件系统 | 故障注入全部落在**临时目录**的真实文件（含同名目录、只读替换失败、ENOSPC） |
| 真实 SQLite | `FlakyJournal` 只让**单个方法**抛 `sqlite3.OperationalError`，其余走生产代码 |
| 真实时区 | `zoneinfo` + `tzdata 2026.6`，时间全部带时区 |
| 真实子进程 | CLI 守卫用 `subprocess` 起独立解释器；跨进程冷却用真实多线程多连接 |
| 全链路追踪 | 从 `engine.monitor/analyze_symbol` → `risk.evaluate` → `broker.place_order` 逐步观察 |
| 只读保护 | `PYTHONDONTWRITEBYTECODE=1` + `-p no:cacheprovider`，不产生 `.pyc` / `.pytest_cache` |

---

## 1. 现场保护（8 项）

| # | 检查项 | 结果 | 直接证据 |
|---|---|---|---|
| 1 | US-P0 备份 MANIFEST | ✅ 70 文件，副本不一致 **0** | 逐文件重算 SHA256 |
| 2 | US-P1 b1 备份 | ✅ 72 文件，副本不一致 **0**，`purpose=US-P1 batch1 修改前基线` | 同上 |
| 3 | 本轮修改文件 SHA256 | ✅ 相对 b1：**15 变更 / 0 缺失 / 3 新增**（新增= `audit/faults.py`、`guards.py`、`test_us_p1_batch1.py`） | `MANIFEST.sha256.json` 比对 |
| 4 | SDK 26 文件基线 | ✅ 与 `sdk_baseline_us_p1_b1.json` 及 `sdk_baseline.json` **各 0 不一致** | 双基线交叉校验 |
| 5 | data / audit.db / .env / vault | ✅ `audit.db` mtime `11-08 13:00`、`.env` `11-08 11:57`、`vault.json` 不存在；`data/` 内 6 个文件全部未变 | `ls --time-style=long-iso` |
| 6 | 三个交易开关 | ✅ `false / false / true`；`can_send_real_order=(False, 'PAPER_TRADING=true…')` | `load_config(use_dotenv=True)` |
| 7 | 复核期间哈希是否变化 | ✅ **127 → 127，新增 0 / 删除 0 / 变更 0** | 起止快照逐键比对 |
| 8 | 未初始化 Git | ✅ 仍 `fatal: not a git repository` | `git status` / `ls -d .git` |

**保护现场：8/8 通过。**

---

## 2. N-1：数据库故障隔离

### 2.1 A/B：9 种故障注入（真实 SQLite 文件）

`exit=✓` 表示 monitor 仍进入退出阶段；`卖单=✓` 表示券商确实收到止损委托。

| 注入方式 | monitor 返回 | 止损 | 故障计数 |
|---|---|---|---|
| `log` 失败 | `['exit']` | ✓ | `audit_write_failed: 3` |
| `find_orders` 失败 | `['phase_unavailable','phase_unavailable','exit']` | ✓ | `order_read_failed: 2` |
| `list_orders` 失败 | `['exit']` | ✓ | `order_read_failed: 2` |
| `orders_since` 失败 | `['exit']` | ✓ | `order_read_failed: 1` |
| 全方法 `database is locked` | `['phase_unavailable','phase_unavailable','exit']` | ✓ | `order_read_failed: 5` / `audit_write_failed: 3` |
| 六方法同时失败 | 同上 | ✓ | 同上 |

**A（是否仍进入后续风险处理）✅ 6/6 通过。**
**B（阶段失败是否明确标记）✅**：失败不伪装成功，而是以 `phase_unavailable` / `phase_failed` 显式进入返回值，
并写 `monitor_phase_failed` / `order_read_failed` 故障流水。

**C（故障日志是否独立于 SQLite）✅**：SQLite 四个读方法全部抛错时，
`faults.jsonl` 写入 8 行、`FAULT.json` 存在、`alerts.log` 8 条 `ALERT`，同时 `list_orders` 仍抛错。

### 2.2 D / E / F：三条通道的级联失效

| 场景 | faults.jsonl | FAULT.json | alerts.log | 结论 |
|---|---|---|---|---|
| D：`faults.jsonl` 不可写 | ✗ | **✓** | **✓** | ✅ 通道互相独立 |
| E：`FAULT.json` 不可写 | **✓** | ✗ | **✓** | ✅ |
| F：三条全部失败 | ✗ | ✗ | ✗ | ⚠️ **仅 stderr + 进程内 `persist_failed`** |

**F 不通过（见缺陷 R-3）**：三条通道全失败时：
- 唯一的记录是 `stderr`（`[FaultRecorder] …`）；
- `snapshot()["persist_failed"]=True`，但 **`snapshot()` 在全仓没有任何调用方**，
  `engine.status()` / Web `/api/status` / `check-env` **都不暴露**故障计数与 `persist_failed`；
- 磁盘满到连 `alerts.log` 都写不进去时，worker 自己的日志同样写不进去 → **无外部可见信号**。

### 2.3 G：券商已受理但本地落库失败（关键）

**场景**：券商返回 `status=unknown`、`order_id` 为空（只带 `client_id`），同时 `record_order` 抛错。

| 子项（任务要求） | 实测 | 判定 |
|---|---|---|
| client_id 是否仍可恢复 | 内存中 `record` 有 client_id（`1079134431`）；**故障流水里没有** —— 字段只有 `['detail','error','event','order_id','symbol']`，且 `order_id=''` | ❌ |
| 是否能够对账 | 重启后 `find_orders(unknown)=[]`、`list_orders=[]` → `_reconcile_pending_unknown` 无事可做，`monitor` 返回 `[]`，未发起任何对账 | ❌ |
| 是否阻止重复提交 | **未阻止**：重启后 +400s 再次 `analyze_symbol` → `stage=ordered`，券商**又收到一笔买单**（新 client_id `1026006985`） | ❌ |
| 重启后是否保持未知状态 | **不保持**：订单在本地**完全没有记录**（不是 `unknown`，而是"不存在"） | ❌ |
| 是否要求人工核对券商侧订单 | 有 `severity=critical` 告警文案（"必须人工核对券商侧订单"），但**不含 client_id/order_id**，人工无从核对具体是哪一笔 | ⚠️ 部分 |

**另**：`stops.json` 为空 —— 因为 `analyze_symbol` 只在 `status ∈ (filled, submitted, partial)` 时
`register_stops`，`unknown` 不登记。若该买单随后成交，持仓将**没有止损**（需下一轮 `position_without_stop` 告警才能发现）。

→ **缺陷 R-1（P1）**。

### 2.4 附加发现：撤单路径把"读失败"表述为"订单不存在"

```
数据库正常：  {'ok': True, 'order_id': 'REAL-1', 'status': 'cancelled'}
数据库读失败：{'ok': False, 'reason': '本地无此订单记录: REAL-1'}   ← 该订单其实存在
券商是否收到撤单请求：['REAL-1']（第一次）→ 第二次未发（正确）
故障记录：['order_read_failed']（已记录，但返回文案误导）
```

`cancel_order()` 用 `fetched, _ok = self._jlist_orders(...)` 后**丢弃了 `_ok`**，
DB 读不出来时按"本地无此记录"返回。动作上是安全的（未撤单），但运维/Web 会被误导为
"这笔单不存在" → 可能放弃追查一笔在途单。→ **缺陷 R-5（P2）**。

---

## 3. N-2：未知订单与风险退出

### 3.1 方向分类矩阵（10 场景）

| # | 场景 | approved | 拦截项 | 判定 |
|---|---|---|---|---|
| 1 | 未知买 + 止损（全额 10/10） | ✓ True | 无 | ✅ |
| 2 | 未知买 + 部分减仓（5/10） | ✓ True | 无 | ✅ |
| 3 | 未知卖 + 再次卖出 | ✗ False | `unresolved_order_state`, `duplicate_order` | ✅ |
| 4 | 未知卖 + 买入 | ✗ False | `unresolved_order_state` | ✅ |
| 5 | 未知止损卖 + 再次止损（两笔未知卖） | ✗ False | 同上 | ✅ |
| 6 | 未知订单查询失败（`orders_known=False`）+ 止损 | ✓ True（带警告） | 无 | ⚠️ 见 R-2 |
| 7 | 查询成功但状态仍 `pending_reconcile` | ✗ False | `unresolved_order_state` | ✅ |
| 8a | 多个未知买 + 止损 | ✓ True | 无 | ✅ |
| 8b | 未知买 + 未知卖 + 止损 | ✗ False（含 sell 即阻断） | `unresolved_order_state` | ✅ |
| 9/10 | 跨周期 / 重启后恢复 | 每轮重读 DB；重启后仍为 `unknown` 则继续阻断 | — | ✅ |

### 3.2 positions_verified 与 held 的新鲜度

| 检查 | 结果 |
|---|---|
| `positions_verified` 是否来自最新查询 | ✅ 由 `_positions_for_monitor()` 在**本轮** `get_positions()` 成功后置 True；降级路径置 False |
| held 是否每次退出前重读 | ✅ 每轮 `monitor` 调用 `get_positions()` **2 次**（持仓段 + `build_portfolio`），退出用的是**第 2 次**（更新）的结果 |
| 实证 | 构造"第 1 次返回 10、第 2 次返回 7"的券商：intent 由第 1 次算出 qty=10，而 gate 用第 2 次的 held=7 → **`unsupported` 拦截**（`exit_blocked`）。说明 held 取自最新查询，且不一致时 fail-closed |
| qty ≤ held | ✅ 超额 11 > 10 被拦（双重理由：`no_naked_short` + reduce-only） |
| 多个未知卖是否合并 | ✅ 只要 `sides & {"sell"}` 即整体阻断，等价于"合并计算后不允许再卖" |
| 未知买后续成交 | ⚠️ 见 R-7：卖出成交会 `clear_stops`，随后未知买成交 → 持仓回来了但**没有止损**（第二轮记录 `position_without_stop`，但不会自动重建止损） |
| 重复止损是否会超卖 | ✅ 进程内：exit 成功后 `clear_stops` → 不再重复；被拦时不发单 → 不会超卖 |

### 3.3 R-2 复现与定级（重点）

**场景**：券商侧已有一笔在途卖单 10 股；现价 180 < 止损 190；同时 `list_orders` 失败。

```
[数据库可读]  monitor kinds = ['order_expire_failed','exit_blocked']
              拦截原因 = "usAAPL 已有未完成/状态未知的 sell 委托（INFLIGHT-1），不得重复提交"
              券商收到新委托 = 无                        ← 正确

[数据库不可读] monitor kinds = ['exit']
              券商收到新委托 = [{'side':'sell','qty':10.0,'client_id':'610621250'}]
              券商侧卖单总数 = 2 ；名义卖出总量 = 20 > 持仓 10   ← 超卖
              故障记录 = 2 条，但**无任何"可能重复卖出"的提示**
```

**可复现路径（5 步）**：
1. 正常买入 10 股，登记止损；
2. 某轮 monitor 触发止损，卖单在券商侧处于 `submitted`（未成交），本地落库正常；
3. 下一轮前 SQLite 变为不可读（WAL 锁/磁盘满/文件被占用）→ `list_orders` 抛错；
4. `_run_exit_pass` 取 `recent_orders=[]`、`orders_known=False` → `_unresolved_orders` 空、
   `_duplicate_check_detail` 看不到在途单 → 仅剩 reduce-only 四约束；
5. 价格仍在止损下方 → 再发 10 股卖单。券商侧累计 20 股卖单 > 持仓 10 → 若两笔都成交即超卖/反向开仓。

**根因**：`_reduce_only_gate` 的 `held` 是**持仓快照**，不含"在途卖单占用的额度"；
而"在途卖单"的唯一来源就是被读不出来的订单表。

**R-2 定级：P1（可导致超卖）。** 已用四约束并不能覆盖此路径——四约束保证的是
"≤ 已核实持仓"，而不是"≤ 可卖余量"。

**缓解方向（供决策，不含实现）**：
- 卖出前用券商侧 `list_orders(status=submitted/partial)` 做二次确认（不依赖本地库）；
- 或改用券商返回的"可用/可卖数量"（frozen qty）作为上限，而非总持仓；
- 或在 `orders_known=False` 时禁止**首笔之后**的减仓（每标的每进程只允许一次）。

---

## 4. N-3：急停存储故障

### 4.1 九种状态的真实分类

| 场景 | read_failed | write_failed | kind | 判定 |
|---|---|---|---|---|
| 文件不存在 | False | False | `missing` | ✅ 不阻断 |
| 内容损坏 | **True** | False | `corrupt` | ✅ |
| 权限不足（`PermissionError`） | **True** | False | `permission` | ✅ |
| `HALT.json` 是目录（IsADirectory） | **True** | False | `permission` | ✅ |
| 磁盘空间不足（`fsync` → ENOSPC） | False | **True** | `no_space` | ✅ |
| 原子替换失败（`os.replace` → EXDEV） | False | **True** | `atomic_failed` | ✅ |
| 目录不可写（data_dir 是文件） | False | **True** | `dir_failed` | ✅ |
| 正常写入→读取 | False | False | — | ✅ `halted=True` |
| 重启恢复 | 见 4.3 | | | ⚠️ |

### 4.2 A/B/C/D/E/F 检查

| 项 | 结果 | 证据 |
|---|---|---|
| **A** 读失败是否阻断所有（含减仓） | ✅ **是** | 真实损坏文件 → 买入 `['kill_switch','halt_storage_read']`，减仓 `['kill_switch','halt_storage_read']` |
| **B** 写失败默认阻断 | ✅ **是** | 真实写失败 → 买入/减仓均 `['kill_switch','halt_storage_write']` |
| **C** reduce_only 默认关闭 | ✅ **是** | `config.yaml` 与代码默认均为 `"block"` |
| **D** 是否存在配置/CLI/Web/环境变量旁路 | ✅ **无** | 环境变量只能覆盖 `LIVE_TRADING/ORDER_EXECUTION_ENABLED/PAPER_TRADING/KILL_SWITCH/LIVE_ACK_PHRASE`；`market_rules.*`、`risk.halt_write_failure_exit_mode`、`halt_store_failed` **均不可被 env 覆盖**；`resume()` 需精确短语 |
| **E** 人工审批是否有审计与撤销机制 | ⚠️ **无** | `halt_write_failure_exit_mode` 只能改配置文件；**没有任何审计事件记录"谁在何时把 block 改成 reduce_only"**，也没有配套撤销流程（改回 block 即可，但无痕） |
| **F** 启用 reduce_only 后是否仍要求实时持仓核实/防反向/对账 | ⚠️ **要求存在，但真实路径不可达** | 见下 |

### 4.3 关键发现：reduce_only 通道在真实路径上不可达

```
真实写失败（data_dir 是文件）:
   write_failed=True  read_failed=False  kind=dir_failed
   默认 block            → 买入 False ['kill_switch','halt_storage_write']
                            减仓 False ['kill_switch','halt_storage_write']
   显式 reduce_only 开启  → 买入 False ['kill_switch','halt_storage_write']
                            减仓 False ['kill_switch']            ← 仍被拦
```

原因：`risk.halt()` 的**第一步**就把 `kill_switch=True`（"先停止再落盘"），
而 `kill_switch` 检查**不区分方向**。于是任何由 `risk.halt()` 产生的写失败，
都会同时具备 `kill_switch=True` → reduce_only 分支永远进不去。

**事实性结论**：`halt_write_failure_exit_mode="reduce_only"` 在当前代码里是
**纸面配置**（除手工调用 `mark_halt_storage_failure(phase="write")` 外无法触发）。

而实现报告的用例 `test_halt_write_failure_reduce_only_channel_structure`
正是**手工设置标志**构造的（helper 级），因此测试通过但真实路径不通。
→ **缺陷 R-4（P2）**，同时命中任务 §7.5「测试只验证 helper」的检查项。

### 4.4 运行期读失败与恢复

```
启动正常       → 买入 True  减仓 True
运行期损坏     → kill_switch=True read_failed=True kind=corrupt → 买入/减仓均 False
删除文件后 refresh → kill_switch=True（保持）read_failed=False → 买入/减仓仍 False
```

✅ 读恢复后**不会自动恢复交易**，仍需人工 `resume`（fail-closed 符合预期）。
注：`refresh_halt_state()` 会清除 `halt_read_failed` 标志，但 `kill_switch` 保持为粘性，
这是实际阻断层——若未来有人给 `kill_switch` 加"自动复位"，就会变成真旁路（见 §9 建议）。

---

## 5. N-4：时间戳与冷却

### 5.1 边界矩阵（`now = 2026-11-04 10:00 ET`，`max_clock_skew_seconds=300`）

| ts | 开仓 | 减仓 | anomaly |
|---|---|---|---|
| 过去 60s | ✗ 拦（正常冷却） | ✓ 放行 | 无 |
| 当前 | ✗ 拦 | ✓ | 无 |
| 未来 +1s | ✗ 拦 | ✓ | 无 |
| 未来 +300s（边界内） | ✗ 拦 | ✓ | 无 |
| 未来 +301s（超界） | ✓ 放行 | ✓ | ✅「时间戳在未来 301s…已排除出冷却窗口」 |
| 未来 +200 天 | ✓ 放行 | ✓ | ✅ 含秒数差 |
| 等价时区（北京表达同一瞬间） | ✗ 拦 | ✓ | 无（换算正确） |
| 无时区 | ✓ 放行 | ✓ | ✅「缺少时区（按本机时区解释）」 |
| 无法解析 | ✓ 放行 | ✓ | ✅「无法解析」 |
| 空字符串 | ✓ 放行 | ✓ | ✅「时间戳为空」 |

### 5.2 其它要求

| 要求 | 结果 |
|---|---|
| 未来时间戳是否被**安全隔离**而非简单忽略 | ✅ 进入 `order_timestamp_anomaly` 检查项 + `KIND_TIMESTAMP` 故障流水 + `warnings`，不是静默跳过 |
| 系统时间回拨 | ✅ 复现（now=11:30 ET，订单 ts=12:00 ET）：开仓/减仓均放行 + anomaly「时间戳在未来 1800s，疑似时钟回拨」 |
| 多进程并发 | ✅ 8 线程抢同一冷却槽位：1 成功 / 7 被拒 / 0 异常（真实 SQLite，`UNIQUE` 约束生效） |
| 减仓是否豁免冷却 | ✅ 5 秒前有成交卖单 → 减仓放行；**未完成**状态仍阻断 |
| 冷却是否被删除 | ✅ 对照：5 秒前带时区买单 → 开仓 `duplicate_order` 拒绝 |

### 5.3 副作用（新发现）

**R-6（P2）**：时间戳**不可解析/缺时区/为空**时，该订单被排除出冷却 → 开仓**直接放行**。
方向是 fail-open（允许交易），与"防重复"目标相反。

```
缺时区   开仓 approved=True 拦截=无
无法解析 开仓 approved=True 拦截=无
对照（正常 ts, 10 秒前）开仓 approved=False ['duplicate_order']
```

现状下 `orders.ts` 由系统 `utcnow_iso()` 写入（带时区），正常数据不会命中；
但数据迁移/手工订正/第三方写入可能产生这类记录，届时冷却静默失效。
`max_clock_skew_seconds` 之外的未来时间戳同理（R-2 之外的另一条 fail-open 路径）。

---

## 6. 配置守卫

### 6.1 九种配置（真实子进程 `ai-trader guard`）

| 配置 | exit | 违规码 |
|---|---|---|
| us 正常启用 | **0** | — |
| us 缺失（`enforce_markets=["cn"]`） | **4** | `GUARD_MARKET_NOT_ENFORCED` |
| `enabled=false` | **4** | `GUARD_RULES_DISABLED` |
| `enforce=false` | **4** | `GUARD_RULES_NOT_ENFORCED` |
| `unenforced_market_policy=warn` | **4** | `GUARD_UNENFORCED_POLICY_NOT_REJECT` |
| `sessions.us` 缺失 | **4** | `GUARD_SESSION_MISSING` |
| 日历过期（covered_until 2026-01-31） | **4** | `GUARD_CALENDAR_EXPIRED` |
| 日历 `UNVERIFIED` | **4** | `GUARD_CALENDAR_NOT_VERIFIED` |
| YAML 语法错误 | **1**（非 4） | traceback（见 R-9） |

### 6.2 其它入口

| 入口 | 结果 |
|---|---|
| CLI `scan`/`monitor` | ✅ 守卫失败 `return EXIT_GUARD(4)`，不构建 Runtime |
| Web `POST /api/scan` | ✅ **409** + `detail.violations`（实测 `GUARD_RULES_DISABLED`） |
| Web `POST /api/monitor` | ✅ 200（刻意不受限，保证减仓不被配置问题阻断） |
| worker | ✅ 脚本第 0 步 `guard`，失败 `exit 4`，且守卫块位于 `monitor` 之前 |
| 环境变量覆盖 | ✅ 设 `UNENFORCED_MARKET_POLICY` / `MARKET_RULES_ENABLED` / `HALT_WRITE_FAILURE_EXIT_MODE` 后 `guard` 仍 exit=0 且**配置未被改变**（env 只认 `LIVE_TRADING` 等 5 项） |
| 模板一致性 | ✅ `yaml.safe_load(config.yaml) == yaml.safe_load(config.template.yaml)`（逐键相等） |

### 6.3 守卫 ≠ 交易授权

```
guard ok = True（生产配置）| can_send_real_order = (False, 'PAPER_TRADING=true，真实券商连接被禁用')
LIVE/ORDER/PAPER = False / False / True
```

✅ 守卫通过只代表"配置姿态合规"，**不代表**允许真实交易；真实交易仍由
`SafetyGate` 三重开关把关，两者是独立闸门。

---

## 7. 测试与代码审查

### 7.1 现场重跑（不引用旧日志）

```
$ PYTHONDONTWRITEBYTECODE=1 python -m pytest tests/ -o addopts="" -q -p no:cacheprovider
387 passed, 1 skipped, 1 warning in 34.89s

$ ... pytest tests/test_us_p1_batch1.py -q
41 passed in 2.11s
```

| 指标 | 声称 | 实测 | 一致 |
|---|---|---|---|
| passed | 387 | **387** | ✅ |
| failed | 0 | **0** | ✅ |
| skipped | 1 | **1**（无 AI Key） | ✅ |

### 7.2 新测试的链路覆盖

| 类别 | 数量 | 说明 |
|---|---|---|
| 走真实组件（engine.monitor/scan、risk.evaluate、FaultRecorder） | **22 / 41** | 真正驱动故障隔离与 reduce-only |
| 配置/文本断言（guard、模板、CLI 退出码、worker 脚本文本） | 10 / 41 | 对守卫类目标是恰当的 |
| 纯 helper 级 | 1（`test_halt_write_failure_reduce_only_channel_structure`） | ⚠️ 见 R-4：伪造标志，真实路径不通 |

### 7.3 monkeypatch 是否掩盖真实故障

- `test_us_p1_batch1.py` 中仅 1 处声明 `monkeypatch`（`test_guard_blocks_scan_cli`），且**未实际使用**（残留参数）。
- 故障注入靠 `FlakyJournal` 子类（真实 SQLite，仅让指定方法抛错）——**没有 mock 掉被测逻辑**。
- ✅ 未发现"用 monkeypatch 让测试变绿"的情况。

### 7.4 reference_day 是否改变生产行情

| 检查 | 结果 |
|---|---|
| 类默认值 | `SyntheticQuoteSource.reference_day = None` → 回落 `date.today()` |
| 生产是否随日期变化 | ✅ 今天/昨天种子不同 |
| 仅测试设置 | `tests/conftest.py` 会话级 fixture 设置并在 `finally` 还原 |
| 定量证据（hk00700 30 步游走末值） | 11-08 **297.83**（偏离 entry=300 仅 0.7%）／11-09 **402.73**（34.2%）／11-10 **352.54**（17.5%） |

→ 完全印证实现报告的说法：那 10 个用例**只在 11-08 恰好通过**，修复是必要且不改变生产行为的。

### 7.5 港股 warn 夹具与生产 reject 是否真正隔离

| 对象 | 值 |
|---|---|
| `load_config()`（生产） | **reject** |
| `conftest.make_cfg()` | warn（显式声明） |
| `helpers.live_cfg()` | warn（显式声明） |
| 夹具是否污染生产 | ✅ 否（调用 `live_cfg` 后再次 `load_config` 仍为 reject） |

**但**：`tests/helpers.py` 第 46-51 行出现**重复的**注释+if 块（同一补丁打了两次），
`tests/conftest.py` 同样重复一次。功能无影响（幂等），属补丁卫生问题 → **R-11（P3）**。

---

## 8. 缺陷清单

> 影响面三列：**新开仓** / **持仓退出** / **重启恢复**。

### R-1（P1）券商已受理但本地落库失败 → 订单彻底丢失

- **复现**：`record_order` 抛错 + 券商返回 `status=unknown, order_id=""`（带 client_id）→ 下单 → 关闭连接重建 engine（同 DB）。
- **现象**：故障记录 `severity=critical` 但**无 client_id**、`order_id=''`；重启后 `find_orders(unknown)=[]`；+400s 再开仓**又下一单**（新 client_id）；`stops.json` 为空。
- **新开仓**：❌ 影响（可重复开仓）
- **持仓退出**：❌ 间接影响（若首单成交则无止损）
- **重启恢复**：❌ 影响（订单不可见、不可对账）
- **修复方向**：故障流水必须落 `client_id` + `decision_id`；`_jrecord_order` 失败时把订单写入**独立**的 `pending_orders.jsonl`（与 faults 同通道），重启时据此恢复 `unknown` 状态并强制对账。

### R-2（P1）`orders_known=False` 时仍可能重复卖出（超卖）

- **复现**：见 §3.3 五步。
- **新开仓**：✅ 不受影响（此路径下开仓已被 `orders_history` 拒绝）
- **持仓退出**：❌ **影响**（可能卖出 2 倍持仓）
- **重启恢复**：❌ 影响（重启后同样看不到在途单）
- **修复方向**：见 §3.3。

### R-3（P2）三条故障通道全失败时无外部可见信号

- **复现**：`data_dir` 与 `log_dir` 都指向文件 → `record()` → 三文件均未生成，仅 stderr。
- **新开仓 / 持仓退出 / 重启恢复**：均不直接影响（只是"故障不可见"）
- **修复方向**：把 `faults.snapshot()` 接进 `engine.status()` → Web `/api/status`；worker 在 exit≠0 时写 `heartbeat` 已存在，可再加"faults_persist_failed" 状态；`check-env` 增加 faults 计数。

### R-4（P2）`halt_write_failure_exit_mode=reduce_only` 真实路径不可达

- **复现**：`risk.halt()` 写失败 → `kill_switch=True` → reduce_only 分支永不进入（减仓被 `kill_switch` 拦）。
- **新开仓**：✅ 无影响（本就该拦）
- **持仓退出**：❌ 影响（写了"可放行"的配置却放行不了）
- **重启恢复**：—（写失败不持久化）
- **修复方向**：要么让 `kill_switch` 检查对"经授权 reduce-only"开口（需极谨慎），要么明确文档化"该开关仅在无 kill_switch 的写失败场景生效"，并把测试改成真实路径断言（当前 helper 级测试会给出错误信心）。

### R-5（P2）撤单读失败被表述为"本地无此订单记录"

- **复现**：`list_orders` 抛错时调用 `engine.cancel_order("REAL-1")` → `{'ok': False, 'reason': '本地无此订单记录: REAL-1'}`。
- **新开仓**：✅ 无
- **持仓退出**：⚠️ 间接（运维误判导致在途单未处理）
- **重启恢复**：—
- **修复方向**：区分 `ORDER_LOOKUP_UNAVAILABLE` 与 `NOT_FOUND`，前者提示"数据库不可读，未能确认订单是否存在"。

### R-6（P2）时间戳不可解析/缺时区 → 冷却失效（开仓方向 fail-open）

- **复现**：`ts="2026-11-04 10:00:00 ???"` 或 `ts="2026-11-04T10:00:00"`（无时区）→ 开仓 `approved=True`。
- **新开仓**：❌ 影响（可能重复开仓）
- **持仓退出**：✅ 无影响（本就豁免时间冷却）
- **重启恢复**：—
- **修复方向**：对**开仓**，`ts` 不可确认时应按"可能重复"处理（阻断或退回状态型检查）；对减仓维持放行。

### R-7（P2）未知买单后续成交 → 止损已被清除 → 持仓无止损

- **复现**：未知买 10 + 止损触发 → 卖出成交 → `clear_stops` → 未知买成交 → 持仓回到 10，`stops.json={}`。
- **新开仓**：✅ 无
- **持仓退出**：❌ 影响（该部分持仓无止损，仅下一轮 `position_without_stop` 告警）
- **重启恢复**：❌ 影响（`stops.json` 为空，重启也不会恢复止损）
- **修复方向**：reduce-only 退出后若该标的仍有未知买单，保留/重建止损登记（qty 取"退出后预计持仓"），或标记 `needs_stop_reregistration`。

### R-8（P2）`halt_write_failure_exit_mode` 无审批审计与撤销机制

- **复现**：改 `config.yaml` 即可，无任何审计事件。
- **修复方向**：启用该模式时写审计事件（谁/何时/依据）+ 启动时 `check-env` 显式告警；撤销时同样留痕。

### R-9（P3）YAML 语法错误 → exit 1 + traceback

- **复现**：`market_rules: [unclosed` → `guard` exit=1，stderr 为 Python traceback。
- **影响**：不可启动（安全），但不符合"守卫违规=4"的约定，排障体验差。
- **修复方向**：`cmd_guard` 捕获 `yaml.YAMLError` → 输出结构化 `GUARD_CONFIG_UNPARSABLE` 并 exit 4。

### R-10（P3）实施报告的变更计数与实际不一致

- 报告 §1.3 写"**12 变更 + 3 新增**"，其自身表格与实测均为 **15 变更 + 3 新增**。
- 属文档准确性，不影响代码。

### R-11（P3）补丁卫生

- `tests/helpers.py` 第 46–51 行、`tests/conftest.py` 第 41/49 行存在**重复的注释+if 块**；
- `test_guard_blocks_scan_cli` 声明了未使用的 `monkeypatch` / `capsys`。

### R-12（P3）`FaultRecorder.snapshot()` 无调用方

- 与 R-3 同源：故障计数/`persist_failed` 只有进程内可读，没有任何暴露面。

---

## 9. 分类汇总

### 9.1 已通过（有直接证据）

| 项 | 证据 |
|---|---|
| 现场保护 8/8 | 127 文件哈希起始=结束；两份 MANIFEST 0 差异；SDK 26/26 一致；开关 false/false/true |
| N-1 A/B/C/D/E | 6 类注入全部仍发出止损；阶段失败显式标记；faults 独立于 SQLite；单通道故障不影响其它通道 |
| N-2 方向分类 10/10 | 见 §3.1（除场景 6 的 R-2 外全部符合预期） |
| N-2 持仓新鲜度 | held 取自第 2 次实时查询；两次读取不一致时 fail-closed |
| N-3 A/B/C/D | 读失败全阻断；写失败默认阻断；reduce_only 默认 block；无 config/CLI/Web/env 旁路 |
| N-3 故障分类 | corrupt / permission / no_space / atomic_failed / dir_failed 全部正确归类 |
| N-4 边界 10/10 | 见 §5.1；未来时间戳进入异常状态而非静默忽略 |
| N-4 并发 | 8 线程抢槽位 1/7 正确 |
| 配置守卫 8/8 + 三入口 | CLI 4 / Web 409 / worker 第 0 步；env 无法覆盖；模板语义一致 |
| 守卫 ≠ 授权 | guard ok 且 `can_send_real_order=False` |
| 回归 | 387 passed / 1 skipped / 0 failed |
| reference_day | 生产行为不变（默认 `None`）；定量解释 10 个日期依赖用例 |
| 港股夹具隔离 | 生产 reject / 夹具 warn / 无污染 |

### 9.2 部分通过

| 项 | 通过部分 | 未达标部分 |
|---|---|---|
| N-1 G（下单后落库失败） | 有 critical 告警文案；未重试下单 | client_id 不可恢复、重启后订单不存在、可重复提交、无止损 |
| N-2 未知单与退出 | 方向分级与四约束正确 | `orders_known=False` 时仍可超卖（R-2）；未知买后成交无止损（R-7） |
| N-3 E/F | 通道结构存在 | 真实路径不可达（R-4）；无审批审计（R-8） |
| N-1 F | 三条通道互不依赖 | 全失败时无外部可见状态（R-3/R-12） |

### 9.3 未解决 / 未验证

| # | 项 | 原因 |
|---|---|---|
| U-1 | 券商侧 reduce-only 原语 | SDK `create_order` 无相关参数（已复核），客户端约束无法替代 |
| U-2 | `useUsNight` 是否真返回夜盘仓位 | 无凭证 |
| U-3 | 真实 `kill_switch` 在急停写失败后的粘性行为 | 逻辑已读，但未在真实长驻进程中验证 |
| U-4 | 磁盘满场景下 worker 自身日志是否也失效 | 无法安全构造（不修改系统盘） |

---

## 10. 八个结论问题

### 1. N-1 是否真正隔离？

**基本隔离，但不完整。**

- ✅ 六个数据库方法单独或同时失败时，**止损仍然发出**（实测 6/6），阶段失败显式标记，故障落独立通道。
- ❌ 但"下单成功 → 本地落库失败"这条路径**没有被隔离**：订单在本地完全消失（R-1），
  重启后既不可见也不可对账，且可重复提交。
- ❌ 三条通道全失败时没有外部可见状态（R-3）。

### 2. N-2 是否仍有重复卖出风险？

**有，且可复现（P1）。**

`orders_known=False` + 券商侧存在在途卖单 → 引擎再发一笔同额卖单 → 名义卖出 20 > 持仓 10（§3.3）。
"四约束"只保证 `qty ≤ 已核实持仓`，**不保证** `qty ≤ 可卖余量（扣除在途卖单）`。
此外未知买单后续成交会留下"无止损的持仓"（R-7）。

### 3. N-3 是否存在急停旁路？

**在"授权"层面不存在旁路，但有一处功能与声明不符。**

- ✅ 读失败全阻断（含减仓）、写失败默认阻断、无 env/config/CLI/Web 旁路、恢复后仍需人工 `resume`。
- ⚠️ `reduce_only` 开关**在实际路径上无法生效**（被 `kill_switch` 先拦），
  即"配置写了 reduce_only 也没用"——这是**过严**而非旁路，但会让运维误以为已开通道。
- ⚠️ 该开关的启停无审计、无审批（R-8）。

### 4. N-4 是否仍会永久阻断止损？

**不会。** 未来时间戳（±任意跨度）、时钟回拨、等价时区、不可解析时间戳下，减仓全部放行（实测 10/10）。
代价是**开仓方向**变成 fail-open：不可解析/缺时区的时间戳会让冷却失效（R-6）。

### 5. 配置守卫是否存在旁路？

**未发现旁路。**

8 类配置违规全部 exit 4；Web 409；worker 第 0 步拦截；环境变量无法改写 `market_rules`；
模板与生产逐键一致；且 `guard` 通过 ≠ 真实交易授权。
唯一瑕疵：YAML 语法错误返回 exit 1 + traceback（非 4），仍属"拒绝启动"，不构成旁路（R-9）。

### 6. US-P1 Batch 1 是否可以验收？

**可以验收为"实现完成"，但不能验收为"实盘就绪"。**

- 5 项任务目标中：N-1 部分达成、N-2 部分达成、N-3 达成（含一处功能不可达）、N-4 达成、配置守卫达成。
- 387/1/0 全绿、4/4 变异被抓住、SDK 零改动、安全开关关闭、用户数据未触碰——**交付物本身是可信的**。
- 但存在 **2 个 P1**（R-1 订单丢失、R-2 可超卖）与 **6 个 P2**，
  其中 R-1/R-2 直接削弱"持仓退出"的安全性。

### 7. 是否允许进入下一批？

**允许进入下一批，但下一批必须以 R-1/R-2 为首批阻断项**，且建议顺序：

1. R-1（订单落库失败的独立持久化 + client_id 可恢复）——直接决定"重启后能否对账"；
2. R-2（卖出前券商侧在途单二次确认 / 用可卖余量替代总持仓）；
3. R-4 + R-8（reduce_only 真实可达性 与 审批审计，二选一：要么修好，要么明确删除该开关避免误导）；
4. R-6（开仓方向时间戳不可确认时改为 fail-closed）；
5. R-5 / R-3 / R-7 / R-9（诊断准确性与暴露面）。

### 8. 是否允许真实交易？

**不允许。** 除既有阻断项（US-B1 无凭证、US-B2 无 AI Key、US-B6 订单类型、US-B7 LULD、
US-B9 结算、US-B12 券商无 reduce-only）外，本批新增两条 P1（R-1、R-2）
都会直接造成**重复下单或超卖**，属实盘不可接受风险。

安全状态确认：`LIVE_TRADING=false`、`ORDER_EXECUTION_ENABLED=false`、`PAPER_TRADING=true`，
`can_send_real_order=(False, …)`。

---

## 11. 建议（不构成本轮修改）

| 优先 | 建议 | 对应缺陷 |
|---|---|---|
| P0 | 把"订单已提交但落库失败"写入独立 `pending_orders.jsonl`（含 client_id/decision_id），启动时恢复为 `unknown` 并强制对账 | R-1 |
| P0 | 卖出前用券商侧在途单/可卖数量做上限，而非总持仓 | R-2 |
| P1 | `reduce_only` 要么真实可达（并对 `kill_switch` 做方向化例外），要么显式移除该开关 | R-4 |
| P1 | 时间戳不可确认时，开仓按"可能重复"处理 | R-6 |
| P1 | 启用 `halt_write_failure_exit_mode` 写审计事件 + `check-env` 告警 | R-8 |
| P2 | `faults.snapshot()` 接入 `status()` / `/api/status` / `check-env` | R-3 / R-12 |
| P2 | 撤单区分 `ORDER_LOOKUP_UNAVAILABLE` 与 `NOT_FOUND` | R-5 |
| P2 | reduce-only 退出后若仍有未知买单，重建/保留止损登记 | R-7 |
| P3 | guard 捕获 YAML 异常 → exit 4 | R-9 |
| P3 | 清理重复补丁块与未使用参数；修正报告计数 | R-10 / R-11 |

---

## 12. 复核签署

| 项 | 结论 |
|---|---|
| 只读执行 | ✅ 127 文件哈希起始=结束；未新增/删除/变更任何项目文件；未初始化 Git |
| 未连接真实券商 | ✅ 全部券商交互为进程内 `Broker` 契约替身 |
| 未发送真实订单 | ✅ `can_send_real_order=False`；所有 `place_order` 落在替身 |
| 未修改 SDK/配置/数据库/密钥 | ✅ SDK 26/26 一致；`config.yaml` mtime 未变；`data/audit.db` mtime `11-08 13:00` |
| 未引用旧日志 | ✅ 全部结论来自本轮现场执行 |
| 交付 | `US_P1_BATCH1_INDEPENDENT_REVIEW.md`（本文件） |

**总判定：N-1 部分通过｜N-2 部分通过（P1 可超卖）｜N-3 通过（含 1 处功能不可达）｜N-4 通过（含 1 处 fail-open）｜配置守卫通过。
US-P1 Batch 1 可验收为"实现完成"；下一批允许启动；真实交易不允许。**
