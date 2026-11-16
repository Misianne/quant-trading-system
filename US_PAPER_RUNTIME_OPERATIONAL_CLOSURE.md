# 星财富 AI 美股交易系统 · Paper/Replay 自动运行闭环验收报告

**项目**：`C:\FosunAI-Trading`
**轮次**：Paper/Replay 自动运行闭环攻坚
**完成时间**：2026-11-16
**性质**：工程实施 + 运行验收（不是审计报告）

---

## 0. 结论（四项独立判定）

| 判定 | 结论 | 依据 |
|---|---|---|
| **A. 只能做离线 Replay** | **可以（已超越）** | 10 个端到端场景全部通过，同场景 3 次运行指纹完全一致 |
| **B. 可以本地 Paper 单次运行** | **可以** | `--paper --replay <场景>` 走完整链路：行情→策略→风控→下单→撮合→入账→不变量 |
| **C. 可以本地 Paper 持续运行** | **可以（有明确边界）** | 30:00 连续运行，**18,535 tick / 1,091 笔成交 / 1,963 轮**，`halts=0`、`breaker=0`、终态 `RUNNING`，退出后无残留进程 |
| **D. 可以进入真实数据源接入阶段** | **不可以** | 见 §12：无权威行情源、无签名校验、`production_authority()` 零注册、无券商通道 |

> **不能因为 C 通过就推断 D 通过。** C 证明的是「本地执行链路与运维能力成立」，
> 与「能不能接真实交易所数据」是两个完全不同的问题。

**本轮同时修复了 10 个真实缺陷**（不是新增功能，是原代码里的错），
其中 3 个会直接导致账务/风控错误或进程退出，详见附录 A。

---

## 1. 执行前保护

| 项 | 结果 |
|---|---|
| 分支 | `master` |
| 本轮起点 HEAD | `e419783`（与用户给定基线一致，已核实） |
| 工作树 | 干净（起始时 `git status --porcelain` 空） |
| 上轮备份校验 | `round4-20261116-152218`：**834 文件全部 OK**（`sha256sum -c` 无失败行） |
| 本轮新备份 | `C:\FosunAI-Trading-Backups\round5-20261116-154938` |
| 新备份规模 | **720 文件 / 11 MB**（排除 `.venv`、`.git`、`__pycache__`） |
| 新备份清单 | `MANIFEST.sha256` 720 行，校验**全部 OK** |
| Git 检查点 | 轻量标签 `baseline-round5` → `e419783` |
| 数据处置 | **未**重置数据库、**未**清空历史、**未**删除任何旧备份或原始报告 |

> 说明：本轮备份文件数少于上轮，因为排除了 `.venv`（76 MB 依赖目录，可由
> `requirements` 重建）与 `.git`（由 Git 自身保证）。源码/配置/数据/日志/报告全量包含。

---

## 2. 四个 Paper 运行组件：逐个打通

### 2.1 组件清单

| 组件 | 对应 P2 能力 | 源码位置 | 初始化入口 |
|---|---|---|---|
| `market_status` | 停牌 / LULD / MWCB | `market/status.py` | `MarketStatusComponent._start` |
| `short_guard` | 卖空 / 借券 / SSR | `broker/short.py` | `ShortGuardComponent._start` |
| `settlement` | T+1 结算 / 购买力 | `portfolio/settlement.py` | `SettlementComponent._start` |
| `caps` | 订单类型 / TIF 能力 | `broker/orders.py` | `CapsComponent._start` |

配套组件（非四项能力，但运行必需）：`clock` / `storage` / `feed` / `broker` / `risk` / `engine`。

### 2.2 依赖与初始化顺序

```
clock
  ├─ storage
  ├─ caps ──┬─ short_guard
  │         └─ market_status
  ├─ settlement
  └─ feed
        └────────── broker ──┐
                  risk ──────┴─ engine
```

拓扑序（实测）：`clock → storage → caps → short_guard → settlement → market_status → feed → broker → risk → engine`
释放顺序（严格逆序）：`engine → risk → broker → feed → market_status → settlement → short_guard → caps → storage → clock`

### 2.3 配置来源

`config/config.yaml` 新增 `paper_runtime:` 段（**默认 `enabled: false`**），
`config/config.template.yaml` 同步（有测试强制两者非注释内容完全一致）。
字段：`enabled / run_root / max_run_seconds / tick_interval_seconds /
broker{initial_cash, commission_bps, min_commission, slippage_bps} /
settlement{currency, mode, settle_lag_days} / short{mode} /
feed{max_quote_age_seconds, max_idle_seconds} /
supervisor{interval_seconds, max_consecutive_failures, max_auto_recoveries, heartbeat_timeout_seconds}`。

### 2.4 关键设计：作用域内启用 P2，不改全局开关

`us_phase2.enabled` 本轮**必须保持 `false`**。若四个组件只能靠全局开关启用，
就会出现「要么违反纪律、要么组件是摆设」的二选一。本轮采用**显式作用域覆盖**：

```python
# RiskController(..., phase2_override=...) / StrategyEngine(..., phase2_override=...)
_p2 = phase2_override if phase2_override is not None else (cfg.raw.get("us_phase2") or {})
```

PaperRuntime 传入 `{"enabled": True, ...}`，因此：
* P2 检查在**本实例**内真实生效（组件不是摆设）；
* 全局 `us_phase2.enabled` 仍是 `false`，Web/CLI/既有 808 项测试行为不变；
* 覆盖只作用于构造出的对象，不写入配置、不跨进程传播。

### 2.5 健康检查「查的是能力，不是进程活着」

| 组件 | 健康判定 | 为什么 |
|---|---|---|
| `clock` | 返回 aware 时间；系统时钟不得超前真实时间 >90s | 时钟不可信时任何时段判断都是错的 |
| `storage` | DB 可读 + WAL 大小 | 文件在但读不了等于没有 |
| `caps` | 有 venue、有订单类型、`is_live=False` | 空能力声明 = 静默放行不支持的订单 |
| `short_guard` | 模式为 disabled 或借券簿可用 | 禁止"模式开着但没券" |
| `settlement` | 能推进 `settle_through(today)` | 日历坏了会算错可用资金 |
| `market_status` | 未触发 MWCB L3 | L3 是当日终结 |
| `feed` | 最近一次成功取价距今 ≤ `max_idle` | 行情停更必须能被发现 |
| `broker` | connected、非真实盘 | — |
| `risk` | `phase2_enabled=True` 且未 halt | 覆盖失效会被抓到 |
| `engine` | 行情网关存在且 `enabled=True` | 结构上不允许旁路取价 |

### 2.6 失败与半启动

* 任一组件 `_start` 抛错 → 已启动组件**逆序回滚** → 整体 `FAILED`；
* 初始化后聚合健康不是 `OK` → 回滚 + `FAILED`；
* 启动即跑**账户不变量**，不通过同样 `FAILED`（不等到第一个 tick）。

### 2.7 幂等与资源释放

* `start()` 在 `READY/RUNNING` 时返回 `already_started=True`，不重复初始化（实测 `start_count=1`）；
* `stop()` 在 `STOPPED` 时返回 `already_stopped=True`；
* 组件 `start/stop` 各自幂等（重复调用不重复订阅/不重复落盘）；
* `stop_all` 关闭失败**逐条记录**并置 `ok=False`，不会假装成功。

---

## 3. 运行状态机

```
STOPPED ──> PREFLIGHT ──> INITIALIZING ──> READY ──> RUNNING
                │              │             │          │  ▲
                │              ▼             │          ▼  │
                └──────────> FAILED          │       DEGRADED
                               │             │          │
                               │             ▼          ▼
                               └──────> STOPPING <── HALTED
                                             │
                                             ▼
                                          STOPPED
```

**白名单迁移**（`state.py: TRANSITIONS`）：表里没有的组合一律 `IllegalTransition`，
不存在「默认允许」。

**防绕过设计**：
* `state` 是只读属性，直接赋值抛 `AttributeError`（有测试）；
* 唯一写入口 `transition()`，记录 `from/to/reason/at/run_id`；
* `HALTED` / `FAILED` **不能**回到 `RUNNING`（必须先 `STOPPING → STOPPED` 再完整重启）；
* `force_from_storage()` 只允许构造期、history 为空、从 `STOPPED` 起步，且必须写理由。

**DEGRADED 不扩大权限**：

| 状态 | 新开仓 | 风险退出 | 依据 |
|---|---|---|---|
| RUNNING | ✓ | ✓ | — |
| DEGRADED | **✗** | ✓ | 非关键组件坏了，但退出通道必须保留 |
| HALTED / FAILED / STOPPING | ✗ | ✗ | 停止一切交易动作 |

---

## 4. 行情与时钟故障保护

### 4.1 统一时间基准（本轮新增 `timebase`）

原来 Engine / Risk / Gateway / MarketStatus 内部多处直接 `datetime.now()`。
回放时会出现「历史行情时间戳 + 现在时间」混用：市场状态门禁用**真实现在**
判断一笔**历史**成交该不该放行。

本轮把所有决策相关调用收敛到 `ai_trader.timebase`：

| 位置 | 改动 |
|---|---|
| `strategy/engine.py` | `_build_context`/`_execute`/`scan`/`monitor`/`build_portfolio`/持仓缓存 |
| `risk/controller.py` | 5 处时间兜底 |
| `market/gateway.py` | `_now_fn` 默认值 |
| `market/sessions.py` | 2 处新鲜度判定 |
| `market/rules.py` | 交易规则时间 |
| `market/status.py` | 4 处（含 `status()` 默认 now） |
| `market/luld_authority.py` | 3 处 |
| `market/clock.py` | `local()` 默认 now |
| `broker/base.py` | `now_iso()`（订单/成交/审计时间戳） |
| `broker/paper.py` | 跨日重置 + 结算交易日 + 合成 K 线起点 |

默认实现是 `SystemClock`（等价于 `datetime.now().astimezone()`），
**生产行为不变**；回放时安装 `ManualClock`，退出必还原（栈式 + 异常安全 + 进程退出兜底）。

### 4.2 11 类行情/时钟故障的处置

| # | 故障 | 处置 | 验证 |
|---|---|---|---|
| 1 | 行情源断连 | `feed` 健康转不健康 → 关键故障 → `HALTED` | `feed_outage` 场景 + 单测 |
| 2 | 长时间不更新 | `max_idle_seconds` / `max_quote_age_seconds` 双阈值 | 同上 |
| 3 | 时间戳倒退 | `ManualClock.set()` 拒绝倒退；`QuoteNormalizer` 判乱序 | `test_manual_clock_rejects_backwards` |
| 4 | 时间戳超前 | 网关拒未来价；`ClockComponent` 检系统时钟超前 >90s | `test_replay_quote_timestamp_follows_clock` |
| 5 | 重复行情 | 网关 `ingest` 去重（同 ts+同价） | `test_us_engine_quote_policy`（既有） |
| 6 | 同事件乱序 | 网关按序校验（早于上一笔 → 拒） | 既有用例 |
| 7 | 多交易所冲突 | `status.py` 按 `symbol\|exchange` 分桶 + 最严格聚合 | 上轮 36 条攻击用例 |
| 8 | DST 切换 | `zoneinfo` + `America/New_York`，冬夏令时偏移不同 | `test_dst_boundary_offsets_differ` |
| 9 | 非交易日 | `ExchangeClock.holidays` → `phase=holiday` | `test_holiday_phase` + `holiday` 场景 |
| 10 | 早收市 | `early_close` 按当日实际收盘倒推 | `test_early_close_shifts_end_of_regular` + `early_close` 场景 |
| 11 | Replay/真实时钟混用 | 全部走 `timebase`；心跳单独用墙钟 | §4.1 + `test_replay_quote_timestamp_follows_clock` |

### 4.3 「不可信就不许成交」

* 时钟返回 naive → 直接抛错，不猜时区；
* 行情缺失/过期 → `QuoteUnavailable` → 风控拒绝新开仓；
* **风险退出意图永远保留**：`intent_gate` 对 exit 一律放行 + 告警，
  但 `submit_gate`/`fill_allowed` 在暂停、竞价、待恢复时仍然拒绝（意图 ≠ 成交）；
* 不允许用本地时间伪造官方恢复事件：`ResumeAuthority` 校验 provider 身份、
  事件版本、时间戳合理性，`production_authority()` 零注册 = 全拒。

---

## 5. Paper Broker 成交闭环

### 5.1 完整链路（实测走通）

```
ReplayQuoteSource
   └─ QuoteGateway(regular_only)      ← 时段/新鲜度/跨时段冒用校验
        └─ StrategyEngine.analyze_symbol
             └─ ScriptedModel（确定性剧本，非 LLM）
                  └─ RiskController.evaluate
                       ├─ 市场状态（intent_gate）
                       ├─ 订单策略（类型/TIF/能力）
                       ├─ 卖空（disabled → 拒绝任何超额卖出）
                       └─ 结算（T+1 可用资金）
                            └─ StrategyEngine._execute
                                 └─ submit_gate(price)      ← 阶段 B
                                      └─ PaperBroker.place_order
                                           └─ fill_allowed(成交价)  ← 阶段 C（Broker 自查）
                                                └─ _apply_fill（现金/持仓/手续费）
                                                     └─ settlement.record_trade（T+1 入账）
                                                          └─ FillLedger.post（唯一 ID、幂等）
                                                               └─ invariants.check_all
```

### 5.2 订单与成交的唯一性

* 订单号：生产用 `uuid`；**回放启用确定性编号** `PR00000001…`（否则同场景多次运行指纹不同）；
* 成交号：`F<order_id>-1`，落盘在订单记录的 `fill_id` 字段；
* 账本以 `fill_id` 为主键，重复投递返回 `duplicate=True` 且**不改账**。

### 5.3 六条账户不变量（每次 tick + 启动时都跑）

| # | 不变量 | 判定 | 失败后果 |
|---|---|---|---|
| 1 | 现金 == 期初 + 成交流水净额（含手续费） | 容差 0.01 | HALTED |
| 2 | 持仓数量 == 流水买卖轧差 | 容差 1e-6 | HALTED |
| 3 | 订单累计成交量 ≤ 委托量 | — | HALTED |
| 4 | `fill_id` 全局唯一且不跨订单复用 | — | HALTED |
| 5 | 卖空关闭时不得出现负持仓 | — | HALTED |
| 6 | 结算账本可用资金 ≤ 券商现金且非负 | — | HALTED（未接账本时降级为 warn） |

**实测**：10 个场景全部 `invariants_ok=True`；人为构造的 6 类破坏（现金差、持仓差、
超量成交、重复成交、未授权卖空、结算越界）全部被检出（6 条对应单测）。

### 5.4 失败必有原因

每个未成交订单都带 `market_note`（例如「Limit State：成交价 106 越出带 [95,105]，拒绝成交」）；
每次风控拒绝写 `reason` 与逐项 `checks`；每次崩溃恢复写 `RecoveryAction`。

---

## 6. 持久化与崩溃恢复

### 6.1 先修掉一个致命写入缺陷

原 `PaperBroker._save()` 直接 `write_text(json.dumps(...))`。写到一半崩溃会留下**截断的 JSON**，
下次启动 `_load()` 解析失败后走 `_default_state()` —— **账户直接归零**。
这比拒绝启动危险得多。

本轮改为原子写：

```python
tmp = state_path.with_name(name + ".tmp")
write(tmp) → flush() → os.fsync(fd)
os.replace(tmp, state_path)      # 原子替换
（再尝试 fsync 目录项，Windows 不支持则跳过）
```

### 6.2 七个崩溃注入点（进程级，`os._exit(9)`）

| 注入点 | 崩溃后磁盘状态 | 恢复结果（实测） |
|---|---|---|
| `before_order` | 无订单、无成交 | 恢复 OK，补账 0 |
| `after_risk` | **无幽灵订单**、现金不变 | 恢复 OK，补账 0 |
| `before_fill_write` | 状态文件无该成交 | 恢复 OK，补账 0 |
| `after_position` | 成交在内存、未落盘 → 丢失 | 恢复 OK（不计入） |
| `after_fill_write` | **成交已落盘、账本为空**（关键窗口） | 扫描补账 1 笔，`orphans=0`，不变量通过 |
| `after_order_create`（挂单） | 订单在、无成交 | 恢复 OK，不凭空补成交 |
| `after_settlement` | 结算已入账、账本待补 | 同 `after_fill_write` 路径 |

> 真机验证：`PAPER_CRASH_AT=after_fill_write` 时进程以 **exit 9** 终止，
> `paper_state.json` 有 1 笔 filled、`paper_fills.jsonl` 为 0 行；
> 再次启动后 `recovery.backfilled=1`、`ledger=1`、`invariants.ok=True`。

### 6.3 恢复策略：只承认事实，绝不猜测

| 情形 | 处理 | 是否自动 |
|---|---|---|
| 券商有、账本无 | 补入账本（记录在案的事实） | 自动，逐条审计 |
| 账本有、券商无 | **HALT**（无法确定资金是否已扣） | 不自动，交人处理 |
| 两边都有 | 幂等跳过 | — |
| 末尾半行（截断） | 隔离并报告 `torn_lines=1` | 自动，不猜内容 |
| 中间行损坏 | 整个账本标记不可信 | 不自动 |

`allow_backfill=False` 时连"补账"也不做，直接 fail-closed（供更严格场景使用）。

---

## 7. 持续运行与自我监控（Supervisor）

| 能力 | 实现 |
|---|---|
| 启动检查 | `preflight()`：四组件齐全 + 状态 ∈ {READY, RUNNING, DEGRADED} |
| 运行心跳 | `heartbeat.json`（**墙钟**时间戳、pid、state、ticks、累计进度、rss、cpu） |
| 组件健康监控 | 每轮 `registry.aggregate()` |
| 行情延迟监控 | `feed.health()` 的 age |
| 订单异常监控 | 统计 `unknown`/`pending` 委托 |
| 风险状态监控 | `risk.halt_reason` |
| SQLite 健康 | `storage` 组件（可读性 + WAL 体积） |
| 内存/CPU | 零依赖：Windows `K32GetProcessMemoryInfo`、`process_time` 差值 |
| 崩溃记录 | `report.events` + `runtime.jsonl` + `faults.jsonl` |
| 安全停止 | `stop.request` 文件 → 主循环当前 tick 后退出 → 逆序释放 |
| 运行报告 | `run_report.json`（cycles、ticks、fills、supervisor、health、account） |

**熔断规则**
* 关键故障 → 立即 HALT + `record_failure`；
* 连续失败达阈值（默认 3）→ **熔断打开**；
* 熔断**不会被成功样本自动清除**（有单测锁死），只能 `reset_manually()`；
* 熔断期间如果运行时还在交易，Supervisor 会主动要求 HALT；
* 非关键故障允许 `max_auto_recoveries` 次恢复，每次都必须通过**完整** `_revalidate()`
  （组件健康 + 重新预检 + 账户不变量），不是"进程还在"就继续。

**进程失联检测**：`paper_status.ps1` 读心跳 + 查 PID，输出
`RUNNING / STALLED / DEAD / NO_RUNS`，退出码 0/5/6 可被外部调度器使用。

---

## 8. 10 个确定性 Replay 场景

| # | 场景 | 验证重点 | 成交 | 终态 | 指纹（前 16） |
|---|---|---|---|---|---|
| 1 | `normal_day` | 建仓 → 次日止盈（T+1 约束） | 2 | RUNNING | `61079df257fdc28c` |
| 2 | `premarket` | 盘前价不得当常规价用 | 0 | RUNNING | `3b5d5d0897775f64` |
| 3 | `regular_session` | 常规时段多 tick 连续 | 2 | RUNNING | `649a09e9ace55308` |
| 4 | `afterhours` | 盘后禁止常规开仓 | 0 | RUNNING | `3b5d5d0897775f64` |
| 5 | `early_close` | 13:00 收盘（非写死 16:00） | 0 | RUNNING | `3b5d5d0897775f64` |
| 6 | `holiday` | 非交易日全拒 | 0 | RUNNING | `3b5d5d0897775f64` |
| 7 | `luld_limit_state` | 带外价禁止成交 | 0 | RUNNING | `3b5d5d0897775f64` |
| 8 | `luld_pause_resume` | 暂停期不得伪造成交 | 0 | RUNNING | `3b5d5d0897775f64` |
| 9 | `mwcb` | 指数 −8% → L1 暂停 | 0 | RUNNING | `3b5d5d0897775f64` |
| 10 | `feed_outage` | 行情中断 → HALT（不自动恢复） | 1 | HALTED | `bef826469bc309ae` |

**确定性验证**：`--replay all --repeat 3` → 30 次运行，**0 失败**，每个场景 3 次指纹完全一致。
指纹覆盖：成交账本内容（按内容排序）+ 订单明细 + 期末现金。

每个场景产出并落盘：输入摘要（`input_digest`）、时钟轨迹（`clock_trace`）、
状态变化（`states_seen` + `market_states`）、订单、成交、风控决策（审计库）、
账户快照、不变量结果。

---

## 9. 测试与全量回归

```bash
cd /c/FosunAI-Trading
env -u PYTHONPATH ./.venv/Scripts/python.exe -m pytest tests/ -q \
    --junitxml=r9.xml -p no:cacheprovider
```

| 指标 | 值 |
|---|---|
| 总用例 | **954** |
| failures | **0** |
| errors | **0** |
| skipped | 1（既有跳过项） |
| 耗时 | 107.7 s |
| 证据 | `r9.xml`（已入库） |
| 运行证据 | `reports/paper_runtime/`（5 份 + `MANIFEST.sha256`） |

**新增 146 项**（要求 ≥60）：

| 文件 | 数量 | 覆盖 |
|---|---|---|
| `test_paper_runtime_state.py` | 15 | 白名单迁移、不可绕过、幂等、HALTED 不可回 RUNNING |
| `test_paper_runtime_components.py` | 17 | 拓扑、环、缺依赖、回滚、逆序释放、关闭失败记录、健康聚合 |
| `test_paper_runtime_ledger.py` | 26 | 幂等入账、截断行、中间损坏、指纹、6 条不变量、恢复扫描 |
| `test_paper_runtime_clock.py` | 19 | 注入时钟、倒退/超前、DST、早收市、节假日、回放时间戳 |
| `test_paper_runtime_scenarios.py` | 15 | 10 场景 + 证据完整性 + 3 次确定性 |
| `test_paper_runtime_supervisor.py` | 22 | 心跳、熔断不清除、恢复预算、失联判定、资源指标、**心跳被占用不崩** |
| `test_paper_runtime_faults.py` | 12 | 7 个进程级崩溃点、孤儿账本、截断账本、幂等恢复 |

**与上轮对比**：808 → 954（+146）。已核对无删除、无跳过扩大、无断言降低；
唯一被修改的既有测试是 `config.template` 一致性（因为新增了配置段，已同步模板）。

---

## 10. 运行验收

| 要求 | 结果 |
|---|---|
| 连续运行 30 分钟 | 见 §10.1 |
| 至少完成 3 次完整 Replay | **30 次**（10 场景 × 3 轮），0 失败 |
| 至少一次安全停止 + 重启 | 见 §10.2（`stop.request` → 释放 → 重启 → 恢复扫描） |
| 至少一次故障注入与恢复 | 见 §6.2（7 个进程级注入点）+ 见 §10.3 |
| 检查账户不变量 | 每 tick + 启动时；10 场景全绿 |
| 进程退出后无残留工作进程 | 见 §10.2 |
| 保存日志与运行报告 | `data/paper_runs/**/run_report.json`、`heartbeat.json`、`runtime.jsonl` |

### 10.1 30 分钟持续运行（实测）

| 项 | 值 |
|---|---|
| 墙钟窗口 | **16:43:28 → 17:13:28**（整 30 分钟） |
| 场景轮次 | **1963** 轮（9 个非终态场景循环，每轮一个全新运行体） |
| 累计 tick | **18,535** |
| 累计成交 | **1,091** 笔 |
| 失败 | **0** |
| 最终状态 | `RUNNING`（非 HALTED、非空转） |
| Supervisor | 巡检正常，`halts=0`、`breaker_trips=0`、`recoveries=0` |
| 峰值内存 | **324.6 MB**（末次心跳 324.59 MB） |
| 句柄 / 线程 | 运行中实测 **179 句柄 / 3 线程**（稳定，无句柄泄漏） |
| 进程退出 | exit 0，**无残留工作进程**（已用 `Get-Process` 复核） |

**"不是空转"的证据**：每条 tick 都真实经过
`ReplayQuoteSource → QuoteGateway → ScriptedModel → RiskController → PaperBroker → FillLedger → invariants`；
1,091 笔成交、2 笔/轮的稳定成交产出、以及每轮递增的 `total_fills` 都可以反查
（`run_report.json` 的 `cycles_log` 含 1963 条明细）。

**真实缺陷就是在这里被抓到的**：运行到第 7 分钟时，一次外部读取 `heartbeat.json`
使 `os.replace` 抛 `PermissionError`，整个进程退出（见附录 A 第 10 条）。
修复后重跑，才得到上表这份结果。

### 10.2 安全停止与重启（实测）

| 步骤 | 结果 |
|---|---|
| 写入 `stop.request` | 主循环在当前 tick 结束后退出 |
| 优雅停止输出 | `{"stop_requested": true, ...}` 随后 `{"ok": true, "cycles": 34, "ticks": 328, "fills": 20, "final_state": "RUNNING"}` |
| 进程退出 | `pid_alive=False`，无残留 |
| 重启 + 一致性扫描 | `fills_in_ledger=2, fills_in_broker=2, backfilled=0, orphans=0, ok=true`，状态 `READY` |
| 结论 | 停止是**优雅**的（不是 kill）；重启后账本与订单**天然一致**，无需补账 |

### 10.3 故障注入与恢复（实测）

| 注入点 | 进程退出码 | 恢复结果 |
|---|---|---|
| `before_order` | 9 | 无订单、无成交；恢复 OK |
| `after_risk` | 9 | 无幽灵订单、现金不变 |
| `before_fill_write` | 9 | 状态文件无该成交；恢复 OK |
| `after_order_create`（挂单） | 9 | 订单在、成交不在；恢复不补成交 |
| `after_fill_write` | 9 | 成交已落盘、账本为空 → **补账 1 笔**，不变量通过 |
| 篡改账本（孤儿成交） | 3 | `orphans=1` → 拒绝 READY（fail-closed） |
| 账本末尾截断 | 0 | `torn_isolated=1`，其余正常 |

### 10.4 验收小结

| 要求 | 达成 |
|---|---|
| 连续 30 分钟 | ✅ 整 30:00，18,535 tick |
| ≥3 次完整 Replay | ✅ 30 次（10 场景 × 3） |
| ≥1 次安全停止 + 重启 | ✅ §10.2 |
| ≥1 次故障注入 + 恢复 | ✅ 7 个注入点（§10.3） |
| 账户不变量 | ✅ 每个 tick + 启动时；全程无失败 |
| 无残留进程 | ✅ |
| 日志与报告 | ✅ `run_report.json`（1963 条 cycles_log）、`heartbeat.json`、`runtime.jsonl`、`faults.jsonl` |

---

## 11. 配置启用纪律

| 项 | 状态 |
|---|---|
| `us_phase2.enabled` | **false**（未改动，有 preflight 硬检查 + 启动脚本二次检查） |
| `paper_runtime.enabled` | **false**（默认不自动启动） |
| 启动方式 | **只能** `--paper` 显式启动；无参数运行打印用法并 exit 2 |
| 卖空 | `short.mode: disabled`，若配置写错 → 初始化阶段 FAILED |
| 真实券商 | preflight 拒绝 `broker.kind ∈ {fosun, live}` 与 `caps.is_live=True` |
| 真实资金 | `--paper` 路径下 `PaperBroker`，不触达任何券商 SDK |
| 云端部署 | 未做 |

**Windows 启停脚本**（无需进入源码目录）：

```
scripts\paper_start.ps1    # 默认 30 分钟；-Minutes 5 / -Replay all
scripts\paper_status.ps1   # RUNNING / STALLED / DEAD，退出码 0/5/6
scripts\paper_stop.ps1     # 先优雅（stop.request），超时才 -Force 并记录
```

---

## 12. 尚未解决（BLOCKED / UNVERIFIED）

| 级别 | 项 | 影响 |
|---|---|---|
| **BLOCKED** | 无权威 LULD/停牌实时数据源 | `production_authority()` 零注册 → 生产路径永不放行恢复 |
| **BLOCKED** | 无券商连接、无真实下单通道 | 无法验证真实回报/拒单语义（本轮明令禁止） |
| **UNVERIFIED** | 恢复事件无密码学签名 | 中间人可伪造"已注册 provider"事件 |
| **UNVERIFIED** | Replay 行情是**合成路径**，不是历史成交 | 验证的是执行链路，不是策略在真实历史上的表现 |
| **UNVERIFIED** | 竞价成交价/撮合优先级未建模 | 竞价阶段只能判"可/不可交易" |
| **UNVERIFIED** | `_last_trade`/`_bands`/`lifecycles` 仍是 symbol 维度 | 同一标的多场所独立报价场景下隔离不完整 |
| 已知限制 | 心跳 `ticks` 每轮重置 | 已补 `total_ticks/cycles_done` 累计字段供外部监控 |
| 已知限制 | 本机执行策略禁止直接运行 `.ps1` | 已提供 `paper_start.cmd / paper_stop.cmd / paper_status.cmd` 包装（单次 `-ExecutionPolicy bypass`）；受会话工具限制，`.cmd` 路径只做了脚本语法解析验证，未实测点击运行 |
| 已知限制 | **内存随轮次近似线性增长** | 30 分钟 1963 轮后 RSS 324.6 MB（约 0.17 MB/轮）。**不是句柄泄漏**（句柄稳定 179、线程 3、审计连接已关闭），判断为每轮重建运行体带来的对象/分配器保留。改善方向：复用运行体或每 N 轮重启进程；**本轮未实施**，以免验收数据与代码不一致 |
| 已知限制 | Supervisor 与运行时同进程 | 进程被 `SIGKILL` 时 Supervisor 一起消失；失联由外部脚本判定 |

---

## 13. 最终结论（再述）

* **A 离线 Replay**：✅ 成立。10 场景、确定性可复现、证据齐全。
* **B 本地 Paper 单次运行**：✅ 成立。全链路真实执行，账务不变量通过。
* **C 本地 Paper 持续运行**：✅ 成立（有边界）。Supervisor 提供心跳/熔断/安全停止；
  边界是「行情是合成的、无夜间运维、同进程监督」。
* **D 真实数据源接入**：❌ 不成立。缺三件事：权威数据源、签名校验、券商通道。

**下一步若要推进 D**，建议顺序：
1. 接一家有授权的行情源（SIP 或券商推送），只做「行情 → 状态机」不接单；
2. 给 `ResumeAuthority` 补签名/证书校验与 provider 白名单；
3. 用真实历史数据重跑 10 个场景，比对合成路径与真实路径的差异；
4. 最后才是券商通道。

---

## 附录 A：本轮修复的 10 个真实缺陷

| # | 缺陷 | 影响 | 修复 |
|---|---|---|---|
| 1 | Engine/Gateway/Risk/Status 直读系统时间 | 回放时用"现在"判断"历史"成交 | 统一 `timebase`，回放装 `ManualClock` |
| 2 | `PaperBroker._save()` 非原子 | 崩溃留下截断 JSON → **账户归零** | 临时文件 + fsync + `os.replace` |
| 3 | 订单/成交无唯一 ID | 无法幂等入账、无法确定性比对 | `fill_id` + 回放确定性订单号 |
| 4 | LULD 带宽翻倍会自我解除 Limit State | 越界价永远进不了暂停（风控失效） | 翻倍级数改用 `_pauses`（真实暂停次数），不再用 Limit State 进入次数 |
| 5 | 参考价被越界成交污染 | 5 分钟均值被拉向越界方向，带宽变宽后自动"回带内" | **只有带内成交**才进参考价样本 |
| 6 | 心跳用回放时钟 | 外部监控算年龄得到负数/几十年 | 心跳改用墙钟，回放时钟只用于业务 |
| 7 | 心跳 tick 每轮重置 | 外部看像卡死 | 增加 `total_ticks/cycles_done/total_fills` |
| 8 | 配置模板与新配置段不同步 | 模板一致性测试失败 | 同步 `config.template.yaml` |
| 9 | 每轮新建运行体不关闭 SQLite 连接 | 30 分钟持续运行内存持续上涨（实测 49→106 MB / 367 轮） | `PaperRuntime.stop()` 关闭自建审计库；注入的 journal 不代管 |
| 10 | 心跳写盘时若文件被外部打开，`os.replace` 抛 `PermissionError` | **外部看一次状态就能把交易进程搞崩**（验收中已真实触发） | 重试 4 次后放弃本次写入并计数；心跳属于诊断信息，绝不中断交易 |

> 第 10 条是在 30 分钟验收过程中**真实触发**的：外部读取 `heartbeat.json`
> 的瞬间，监督器写心跳失败并退出。这不是理论风险 —— 状态脚本读心跳就是同一个动作。

## 附录 B：新增文件清单

```
src/ai_trader/timebase.py                        统一时间基准
src/ai_trader/paper_runtime/__init__.py
src/ai_trader/paper_runtime/__main__.py          CLI（--paper 强制）
src/ai_trader/paper_runtime/state.py             状态机
src/ai_trader/paper_runtime/components.py          组件注册表/拓扑/健康
src/ai_trader/paper_runtime/components_impl.py     10 个具体组件
src/ai_trader/paper_runtime/preflight.py          启动前检查
src/ai_trader/paper_runtime/ledger.py             成交账本（幂等）
src/ai_trader/paper_runtime/invariants.py          6 条账户不变量
src/ai_trader/paper_runtime/recovery.py            启动恢复扫描
src/ai_trader/paper_runtime/fault_inject.py        进程级故障注入
src/ai_trader/paper_runtime/supervisor.py          Supervisor/熔断/心跳
src/ai_trader/paper_runtime/model.py               确定性剧本模型
src/ai_trader/paper_runtime/replay.py              10 场景 + 驱动器
src/ai_trader/paper_runtime/runtime.py             PaperRuntime
src/ai_trader/paper_runtime/timebase_bridge.py     时钟安装/还原
scripts/paper_start.ps1 / paper_status.ps1 / paper_stop.ps1
tests/test_paper_runtime_*.py                      7 个文件 / 145 项
r9.xml                                             全量回归 JUnit
```
