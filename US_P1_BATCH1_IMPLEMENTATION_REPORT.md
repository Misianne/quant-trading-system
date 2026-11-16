# US_P1_BATCH1_IMPLEMENTATION_REPORT.md —— 交易安全补强（第一批）

**项目**：`C:\FosunAI-Trading`
**依据**：`US_P0_INDEPENDENT_REVIEW.md` 的 N-1 / N-2 / N-3 / N-4 + 配置守卫
**时间**：2026-11-08 20:19 – 2026-11-09 12:50 (+08:00)
**性质**：允许修改代码/配置/测试；**未连接真实账户、未发送任何真实委托、未修改官方 SDK**
**安全状态（`load_config(use_dotenv=True)` 实测）**：

```
live_trading             = False
order_execution_enabled  = False
paper_trading            = True
kill_switch              = False
halt_store_failed        = False   halt_read_failed = False   halt_write_failed = False
can_send_real_order      = (False, 'PAPER_TRADING=true，真实券商连接被禁用')
```

---

## 0. 结论摘要

| 项 | 状态 | 关键证据 |
|---|---|---|
| **N-1** 数据库故障隔离 | ✅ 完成 | 6 个 DB 方法同时抛错，monitor 仍发出止损；故障写入独立通道（`faults.jsonl` / `FAULT.json` / `alerts.log`） |
| **N-2** 未知单与减仓 | ✅ 完成（券商侧 reduce-only 无原语 → 标注部分未解决） | 未知买单 + 止损 → reduce-only 放行并留下 `exit_authorized_reduce_only` 审计；未知卖单 + 再卖 → 拒绝 |
| **N-3** 急停读写拆分 | ✅ 完成 | 读失败全阻断（含减仓）；写失败默认阻断；`reduce_only` 通道已实现但**默认关闭** |
| **N-4** 订单时间戳 | ✅ 完成 | 冷却窗口加**上界**；未来时间戳只告警不计入；减仓不适用时间冷却 |
| **配置守卫** | ✅ 完成 | `guard` 子命令 + worker 首步 + `/api/scan` 前置；6 类违规实测退出码 4；模板与生产逐键一致 |
| 回归 | ✅ 全绿 | **387 passed / 1 skipped / 0 failed**（原 347 → 现 388，新增 41） |
| 变异检验 | ✅ 4/4 被抓住 | 见 `US_P1_BATCH1_TEST_REPORT.md` §4 |

---

## 一、修改前保护

### 1.1 校验既有 US-P0 备份

```
$ python（读 backups/us_p0_20261108-183854/MANIFEST.sha256.json 逐文件重算）
备份文件 70 个；副本不一致 0
```

### 1.2 本轮独立备份

```
backups/us_p1_b1_20261108-201935/
├── MANIFEST.sha256.json   （created_at / root / purpose="US-P1 batch1 修改前基线"）
├── config/ scripts/ src/ tests/
└── .env.example  pyproject.toml  requirements.txt
```

| 项 | 值 |
|---|---|
| 文件数 | **72** |
| 大小 | 692 KB |
| 秘密泄漏检查 | **无**（已排除 `.env` / `vault.json` / `*.db` / `logs/`） |
| 是否含 `config.template.yaml` | 是（模板同步前的基线） |

### 1.3 修改前 SHA256

见 `MANIFEST.sha256.json`（72 个文件全量哈希）。本轮最终变更面：
**12 变更 + 3 新增**（外加 3 个测试文件的注释/夹具调整）。

### 1.4 SDK 26 文件指纹

```
audit_evidence/sdk_baseline_us_p1_b1.json   （sdk_commit c4c8a51…，26 文件 SHA256）
修改后复核：26 文件，变更 0（SDK 一字节未动）
```

### 1.5 data / .env / vault

| 目标 | 处理 |
|---|---|
| `data/` | 未复制、未写入、未重置；`audit.db` mtime 仍为 `2026-11-08 13:00` |
| `.env` | 未读取内容（只做键名 grep）、未复制、未写入；mtime 仍为 `11-08 11:57` |
| `data/vault.json` | 不存在（无机密可泄露） |
| Git | 未初始化（仍无 `.git`） |
| 历史备份 | 未清理、未覆盖（`us_p0_…` 与 `us_p1_b1_…` 并存） |

---

## 二、N-1：monitor 数据库故障隔离

### 2.1 原缺陷

`monitor()` 的 5 个阶段全部直接调用 `self.journal.*`：

| 原调用点 | 故障后果 |
|---|---|
| `_expire_stale_orders` → `find_orders` | 崩在"读持仓之前"，止损逻辑根本没进入 |
| `_reconcile_pending_unknown` → `find_orders` | 同上 |
| `risk.evaluate(recent_orders=journal.list_orders(...))` | 参数求值阶段崩，崩在风控之前 |
| `_positions_for_monitor` 的告警 `journal.log` | "持仓失败"这条降级路径本身会崩 |
| `build_portfolio` → `orders_since` | 组合快照崩 |

一旦 SQLite 局部失效，**整轮止损、撤单、对账全部不执行**，而且"没有执行"这件事
也记录不下来（记录依赖同一个坏掉的库）。`cli.py` 未包 try/except，
worker 只写 `heartbeat=error`，无外部告警。

### 2.2 实现

**新增 `src/ai_trader/audit/faults.py`（195 行）**

| 组件 | 作用 |
|---|---|
| `FaultRecorder.record(kind, detail, severity, alert)` | 记录一条故障；**任何情况下都不抛异常** |
| `data/faults.jsonl` | 只追加故障流水（纯文件 IO，不依赖 SQLite，`fsync` 落盘） |
| `data/FAULT.json` | 当前故障哨兵（`mkstemp`→`fsync`→`os.replace` 原子写），供外部监控读取 |
| `logs/alerts.log` | 面向人的 `ALERT severity=… kind=…` 行，便于日志采集 |
| `read_all()` / `current()` / `snapshot()` | 验收与排查用；读不到返回空，不抛异常 |
| 最后防线 | 文件也写不进去时写 `stderr` + 进程内 `counts` |

**故障分类**（与三条通道对应）：

```
audit_write_failed / order_read_failed / position_read_failed
halt_storage_failed / order_timestamp_anomaly / config_guard_violation / unknown_order_block
```

**`StrategyEngine` 新增 7 个安全包装器**（全部不抛异常、失败即写故障通道）：

| 包装器 | 失败语义 |
|---|---|
| `_jlog()` | 审计写入失败 → `KIND_AUDIT_WRITE`（warn/error 可选） |
| `_jlist_orders()` | 返回 `(rows, ok)`；**ok=False 表示读不出来，不是"没有订单"** |
| `_jfind_orders()` | 同上 |
| `_jorders_since()` | 同上 |
| `_jrecord_order()` | 订单已提交但落库失败 → **critical** 故障（"必须人工核对券商侧订单"） |
| `_jrecord_risk_decision()` | 决策落库失败 → 故障流水 |
| `_jupdate_status()` | 状态回写失败 → 故障流水 |

**`monitor()` 重构为 5 个彼此隔离的阶段**

```
_phase("refresh_pending", ...)        # 券商能力
_phase("expire_orders", ...)          # 依赖 find_orders
_phase("reconcile_unknown", ...)      # 依赖 find_orders
_positions_for_monitor(...)           # 券商，失败降级到快照/本地登记
_run_exit_pass(...)                   # 行情 + 止损止盈（最关键，独立成段 + 兜底 critical）
```

`_phase()` 捕获任何异常 → 写故障 → 返回 `phase_failed` 记录 → **继续下一阶段**。
`_run_exit_pass` 自身再包一层，异常时写 `exit_pass_failed`（critical）并提示人工核对。

### 2.3 风险退出策略（数据库不可用时）

| 情况 | 开仓 | 减仓 | 依据 |
|---|---|---|---|
| `orders_since` 失败（组合快照不完整） | ❌ 拒绝 | ✅ 放行（降级组合） | `portfolio.degraded` + `portfolio_integrity` |
| `list_orders` 失败（订单历史不可读） | ❌ 拒绝（`orders_history` 检查失败） | ✅ 放行，但必须满足 reduce-only 四项硬约束 | `Portfolio.orders_known=False` |
| `find_orders` 失败（无法撤单/对账） | ❌ 拒绝 | ✅ 放行 | 撤单阶段跳过并记录，不阻断止损 |
| `record_order` 失败 | — | — | 订单已发出 → **critical** 告警 + 人工核对 |

**明确不做的事**：捕获异常后"继续执行普通开仓"。所有开仓路径在
`orders_known=False` / `positions_stale` / 配置守卫失败时都被拒绝。

### 2.4 实测（`audit_evidence/us_p1_b1_fault_demo.txt`）

```
[场景] journal.log/find_orders/list_orders/orders_since/record_risk_decision 全部抛错
monitor 返回: ['phase_unavailable', 'phase_unavailable', 'exit']
券商收到委托: [{'symbol': 'usAAPL', 'side': 'sell', 'qty': 10.0, ...}]
故障计数: {'order_read_failed': 5, 'audit_write_failed': 3}

data/faults.jsonl（独立于 SQLite）→ 正常写入
data/FAULT.json → latest.kind = audit_write_failed, severity=critical
logs/alerts.log → 8 条 ALERT 行
```

---

## 三、N-2：未知订单与减仓

### 3.1 原缺陷

`_find_unresolved(sym)` 只按 **symbol** 匹配，不区分方向 → 一个未对清的**买单**
会把同标的的**卖出止损**一起锁死；对账持续失败时，止损永久失效。

### 3.2 实现：方向分级 + reduce-only 硬约束

**新增 `OrderIntent.exit_mode`**（`stop_loss` / `take_profit` / `signal_exit` / `manual`）
与 `is_reduce_only`（`is_exit and side=="sell"`）。

**新增方向规则**（`_unresolved_reduce_only`）：

| 未知单方向 | 本次意图 | 结果 |
|---|---|---|
| buy | sell（退出） | ✅ 允许，但须过 reduce-only 四项硬约束 |
| sell | sell | ❌ 阻断（可能已成交 → 再卖超卖） |
| 任意 | buy（开仓/加仓） | ❌ 阻断 |
| 方向不明 | 任意 | ❌ 阻断 |

**reduce-only 四项硬约束**（`_reduce_only_gate`，全部满足才放行）：

1. **方向**：必须是"卖出 + 已标记退出"（不允许借道反向开仓）
2. **持仓已核实**：`Portfolio.positions_verified=True`（本轮由券商实时返回；
   降级快照/本地止损登记**不算**）
3. **持仓 > 0**：已核实持仓为 0 时不得减仓（防反向开仓）
4. **数量**：`qty ≤ 已核实持仓`（不允许超过真实持仓）

**退出授权审计**：放行时写 `exit_authorized_reduce_only`，含 `symbol / side / qty /
exit_mode / positions_verified / held / checks` 明细。

### 3.3 券商侧 reduce-only 原语：**不存在（已明确标注）**

```
$ grep "reduce\|reduceOnly\|positionSide\|close" sdk_src/fsopenapi/api/trade.py
（无输出）
$ grep -n "def create_order" -A 45 sdk_src/fsopenapi/api/ops/trade.py
  create_order(..., quantity, tail_amount, profit_quantity, stop_loss_quantity, ...)
  → 无 reduceOnly / close / positionSide 参数
```

**结论**：本通道的"只减不增"保证**仅在客户端**（sell-only + qty≤持仓），
券商侧无法二次保证。按用户要求，**明确标记为"部分未解决"**，不假装安全。
风险：若该标的另有一笔未记录的卖出在途，客户端约束无法阻止超卖
（`orders_known=False` 时尤其如此）。

---

## 四、N-3：急停存储异常

### 4.1 原缺陷

`HaltStore.failed` 把四类完全不同的故障合成一个布尔：

* 文件损坏 / 权限不足 / 磁盘满 / 原子写失败 / 目录不可建 —— 全部一视同仁，
  只能"一律阻断"，运维无法判断该修什么。

### 4.2 实现：故障分类 + 读写分离

**`HaltStoreError` 增加 `kind` / `phase`**；故障类型：

```
read_failed / corrupt / permission / no_space / atomic_failed
write_failed / dir_failed / missing / unknown
```

**`HaltStore` 新增**：`read_failed`、`write_failed`（互不覆盖）、`last_kind`、
`last_phase`、`classify()`。`read()` 成功**不会**清除 `write_failed`（反之亦然）。

**`SafetyState` 新增**：`halt_read_failed`、`halt_write_failed`、`halt_error_kind`、
`halt_error_phase`，并提供 `mark_halt_storage_failure(kind, phase)` /
`clear_halt_storage_failure()`；`halt_store_failed` 保留为聚合标志（向后兼容）。

### 4.3 处置策略

| 故障 | 默认 | 说明 |
|---|---|---|
| **读取失败**（corrupt / permission / no_space / read_failed） | **全部阻断**（含减仓） | "不知道是否被人为停止" → 不得绕过人工停止指令 |
| **写入失败**（dir_failed / write_failed / atomic_failed） | **全部阻断** | 默认最安全 |
| 写入失败 + `risk.halt_write_failure_exit_mode="reduce_only"` + 过四项硬约束 | 允许受限减仓 | **本轮默认关闭**，需人工审批后开启 |

配置新增（默认值）：`halt_write_failure_exit_mode: "block"`。

**本轮明确没有对所有 `is_exit` 开放权限**：`reduce_only` 通道要求
（1）显式配置、（2）持仓实时核实、（3）数量≤持仓、（4）sell-only，四条缺一不可。

---

## 五、N-4：订单时间戳与冷却

### 5.1 原缺陷

```python
if ts >= cutoff:            # 只判下界
    return "冷却窗口内已成交，判定为重复"
```

一条 `ts` 在**未来**的记录（系统时钟回拨、券商时间戳异常、时区混用）
会永远满足 `ts >= cutoff` → 该标的同向委托**永久被拒**，止损也在此列。

### 5.2 实现

`_duplicate_check_detail()` 重写（`_duplicate_check` 保留为兼容包装）：

1. **时间窗有界**：`cutoff ≤ ts ≤ now + max_clock_skew_seconds`（默认 300s）
2. **未来时间戳 → 明确异常状态**：不计入冷却，产出 `anomaly` 字段，
   由引擎写入 `KIND_TIMESTAMP` 故障流水并落到 decision 的
   `order_timestamp_anomaly` 检查项
3. **无时区时间戳 → 按本机时区解释但留痕**（避免静默口径不明）
4. **无法解析的时间戳 → 跳过 + 留痕**（不再静默 `continue`）
5. **未完成/未知同向委托 → 永远阻断**（与时钟无关的状态型检查，
   这是"防真重复"的可靠依据）
6. **减仓不适用时间冷却**：`is_exit=True` 时时间冷却只记录不拦截；
   `_reserve_or_block` 也跳过减仓（不占用跨进程冷却槽位）

**没有删除冷却**：`test_recent_timestamp_still_blocks_entry` 断言
正常时间戳仍然拦重复开仓；`test_exit_not_blocked_by_time_cooldown` 断言
"已成交"放行但"未完成"仍拒绝。

配置新增：`max_clock_skew_seconds: 300`。

### 5.3 附带修复（同一类"退出被无谓规则挡住"问题）

| 检查 | 原行为 | 现行为 | 理由 |
|---|---|---|---|
| `min_notional` | 减仓也校验（1 股 × $50 < 100 → 拒） | 减仓豁免 | 最小金额是"过滤碎单"，不能阻止清掉小持仓 |
| `rate_limit` | 减仓也限 5 笔/分 | 减仓豁免 | 频率限制防刷单，不防救命 |

---

## 六、配置守卫

### 6.1 新增 `src/ai_trader/guards.py`（239 行）

`check_trading_config(cfg)` / `assert_trading_config(cfg)` / `guard_summary(cfg)`，
违规码：

```
GUARD_RULES_DISABLED          enabled=false
GUARD_RULES_NOT_ENFORCED      enforce=false
GUARD_MARKET_NOT_ENFORCED     required_markets ⊄ enforce_markets
GUARD_UNENFORCED_POLICY_NOT_REJECT
GUARD_SESSION_MISSING         未显式配置 sessions.<mkt>（不靠代码默认值兜底）
GUARD_CLOCK_UNAVAILABLE       时区数据缺失
GUARD_BOARD_MISSING / GUARD_BOARD_NOT_VERIFIED
GUARD_CALENDAR_MISSING / GUARD_CALENDAR_NOT_VERIFIED / GUARD_CALENDAR_EXPIRED
GUARD_POSITION_SESSIONS_INCOMPLETE   use_us_* 被显式关闭
GUARD_TICK_SIZE_MISSING
GUARD_SAFETY_CONFLICT
```

`required_markets` 默认 `["us"]`，可由 `market_rules.required_markets` 覆盖。

### 6.2 接线

| 入口 | 行为 |
|---|---|
| `ai-trader guard` | 新子命令，输出 JSON，通过=0 / 违规=**4** |
| `ai-trader scan` / `monitor` | 先过守卫，失败直接 `return 4`，**不构建 Runtime、不碰券商** |
| `ai-trader check-env` | 输出 `config_guard` 摘要 |
| `scripts/run_worker.ps1` | 第 0 步执行 `guard`，失败 `exit 4`，**不进入 monitor/scan** |
| Web `POST /api/scan` | 守卫失败返回 **409** + 违规明细（`/api/monitor` 不受限，保证减仓不被配置问题阻断） |

### 6.3 配置变更

```yaml
market_rules:
  enabled: true            # 显式写出两个总闸
  enforce: true
  enforce_markets: ["cn", "us"]
  required_markets: ["us"]
  unenforced_market_policy: "reject"     # 原 "warn"
risk:
  max_clock_skew_seconds: 300
  halt_write_failure_exit_mode: "block"
```

### 6.4 模板同步

`config/config.template.yaml` 由 `config.yaml` **逐字同步**（仅替换头部注释），
生成前有安全闸（若 `sub_account_id` / `apply_account_id` / `base_url` /
`live_ack_phrase` / `api_key` 非空则拒绝生成）。校验：

```
语义一致: True   （tests/test_us_p1_batch1.py::test_template_matches_production_semantics 常驻断言）
```

### 6.5 实测退出码

```
  正常               exit=0
  enabled=false    exit=4  GUARD_RULES_DISABLED
  enforce=false    exit=4  GUARD_RULES_NOT_ENFORCED
  us 不在 enforce    exit=4  GUARD_MARKET_NOT_ENFORCED
  policy=warn      exit=4  GUARD_UNENFORCED_POLICY_NOT_REJECT
  删除 sessions.us   exit=4  GUARD_SESSION_MISSING
```

### 6.6 A 股/港股历史行为

`unenforced_market_policy` 改为 `reject` 后，**港股订单会被规则层拒绝**
（`MARKET_NOT_ENFORCED`）—— 这是刻意的安全收紧，对美股零影响（美股本就在
`enforce_markets` 内，走 `applied=True`）。

为**不改变既有港股用例的原意**（它们用 hk 作为"通用标的"验证风控机制），
`tests/conftest.py::make_cfg` 与 `tests/helpers.py::live_cfg` **显式声明**
`unenforced_market_policy="warn"`，并注明"生产是 reject，由专项用例断言"。
A 股/港股代码、规则、历史用例**一项未删**。

> 这是本轮唯一的"测试侧放宽"，且是**显式、可审计、有对照用例**的放宽：
> `test_production_config_is_reject_policy` 与
> `test_reject_policy_really_rejects_unmodelled_market` 保证生产口径是 reject。

---

## 七、附带修复：测试套件的日期依赖（复评建议 D-3 的连带项）

把合成行情固定参考日后，发现**修改前代码有 10 个用例在不同日历日结果不同**：

```
$ PYTHONPATH=backups/us_p1_b1_20261108-201935/src pytest tests/ -q
10 failed, 336 passed, 1 skipped
```

根因：`SyntheticQuoteSource` 用 `date.today()` 播种，`quote()` 取 30 步随机游走的末值，
于是"今天恰好是空头排列"时，`HeuristicModel` 返回 hold、
`FixedSignalModel(entry=300)` 与当日价格偏离 >5% 被风控拒单 ——
与这些用例真正要验证的东西（AI 权威、并发去重、推送门槛）无关。

修复方式（**不削弱任何断言**）：

* `SyntheticQuoteSource` 增加 `reference_day`（类级默认 `None`＝跟随系统日期，
  生产/演示行为不变）；
* `tests/conftest.py` 用 `autouse session fixture` 把参考日固定为 `2026-11-08`；
* 结果：与本机日期无关，任何一天跑都是同一套行情。

---

## 八、修改文件清单

| # | 文件 | +/-（行） | 修改原因 |
|---|---|---|---|
| 1 | `src/ai_trader/audit/faults.py` | **新增 195** | 独立于 SQLite 的故障流水 + 哨兵 + 告警 |
| 2 | `src/ai_trader/guards.py` | **新增 239** | 配置守卫（启动前置） |
| 3 | `src/ai_trader/safety/halt.py` | +134 / -22 | 故障分类（corrupt/permission/no_space/atomic…）、读写分离、`classify()` |
| 4 | `src/ai_trader/config.py` | +32 / -1 | `SafetyState` 读/写分离标志 + `mark/clear_halt_storage_failure` |
| 5 | `src/ai_trader/risk/controller.py` | +281 / -61 | 急停分级、`orders_history`、未知单方向分级 + reduce-only 四约束、时间戳有界冷却、减仓豁免 min_notional/rate_limit、`Portfolio.positions_verified/orders_known`、`OrderIntent.exit_mode` |
| 6 | `src/ai_trader/strategy/engine.py` | +321 / -114 | 7 个安全包装器、monitor 五阶段隔离、故障通道、`positions_verified`、退出授权审计 |
| 7 | `src/ai_trader/cli.py` | +33 / -2 | `guard` 子命令、scan/monitor 前置守卫、`check-env` 增加 `config_guard` |
| 8 | `src/ai_trader/web/app.py` | +14 / -0 | `/api/scan` 守卫（409）；`/api/monitor` 明确不受限 |
| 9 | `src/ai_trader/broker/paper.py` | +13 / -3 | `SyntheticQuoteSource.reference_day`（测试确定性） |
| 10 | `scripts/run_worker.ps1` | +16 / -1 | 第 0 步 `guard`，失败 `exit 4` |
| 11 | `config/config.yaml` | +14 / -3 | `enforce` 显式化、`required_markets`、`policy=reject`、`max_clock_skew_seconds`、`halt_write_failure_exit_mode` |
| 12 | `config/config.template.yaml` | +109 / -33 | 与生产逐字同步 |
| 13 | `tests/conftest.py` | +31 / -1 | 固定合成行情参考日；夹具显式声明 warn 语义 |
| 14 | `tests/helpers.py` | +6 / -0 | 同上 |
| 15 | `tests/test_halt_persistence.py` | +6 / -1 | 断言读写分级后的检查名 |
| 16 | `tests/test_process_restart.py` | +11 / -3 | 基于生产配置构造（守卫要求完整配置） |
| 17 | `tests/test_web.py` | +3 / -0 | `/api/scan` 夹具切生产口径 |
| 18 | `tests/test_us_p1_batch1.py` | **新增 704** | 41 个验收用例 |
| 19 | `audit_evidence/mutation_plugin.py` | +约 90 | 4 个 US-P1 变异模式 |

SDK：**0 变更**。

---

## 九、残留风险与未解决项（诚实清单）

| # | 项 | 级别 | 说明 |
|---|---|---|---|
| R-1 | 券商侧无 reduce-only 原语 | **未解决（外部）** | SDK `create_order` 无 `reduceOnly`；本通道只是客户端约束。若存在未记录的并行卖单，客户端无法防超卖 |
| R-2 | `orders_known=False` 时的减仓仍可能重复卖 | P1 | 数据库读不到订单历史时，reduce-only 只能保证"卖 ≤ 已核实持仓"，无法排除"另一笔在途卖单"。已在故障流水标注 `phase=exit_evaluate` |
| R-3 | `halt_write_failure_exit_mode=reduce_only` 未启用 | **待人工决策** | 通道已实现并被用例覆盖，默认 `block`。开启需运维书面确认（这是"允许在急停不可持久化时仍能减仓"的口子） |
| R-4 | `_duplicate_check` 的进程内记录无上界修正外的时间源 | P2 | 进程内 `note_order` 用的是调用方传入的 `now`；若调用方时钟跳变仍可能异常，已有 `ts > upper` 告警兜底 |
| R-5 | 故障通道本身写不进去时只有 stderr | P3 | 磁盘满到 `faults.jsonl` 也写不了时，只剩 stderr 与进程内计数；已在 `snapshot().persist_failed` 标记 |
| R-6 | 测试夹具显式 `warn` | 说明 | 见 §6.6；生产 `reject` 有专项断言 |
| R-7 | 夜盘/订单类型/TIF 等 | 沿用 US-P0 | 未变，仍在 `not_modeled` |

---

## 十、回滚方法

```powershell
$B = "C:\FosunAI-Trading\backups\us_p1_b1_20261108-201935"

# 1) 逐文件回滚（先看清单）
Get-ChildItem $B -Recurse -File | Select-Object -First 20

# 2) 覆盖回滚（示例：全部代码/配置/测试）
Copy-Item "$B\src\*"     "C:\FosunAI-Trading\src\"     -Recurse -Force
Copy-Item "$B\config\*"  "C:\FosunAI-Trading\config\"  -Recurse -Force
Copy-Item "$B\scripts\*" "C:\FosunAI-Trading\scripts\" -Recurse -Force
Copy-Item "$B\tests\*"   "C:\FosunAI-Trading\tests\"   -Recurse -Force

# 3) 删除本轮新增文件（不在清单里）
Remove-Item C:\FosunAI-Trading\src\ai_trader\audit\faults.py
Remove-Item C:\FosunAI-Trading\src\ai_trader\guards.py
Remove-Item C:\FosunAI-Trading\tests\test_us_p1_batch1.py

# 4) 校验：与 MANIFEST 比对，应 0 差异
python -c "import hashlib,json,pathlib;m=json.load(open(r'$B\MANIFEST.sha256.json'));f=m['files'];print('差异:',[k for k,v in f.items() if hashlib.sha256(pathlib.Path(k).read_bytes()).hexdigest()!=v])"
```

回滚后**不需要**改回 `config.yaml`（它已在备份内），也不需要动 `data/`（本轮未写入）。

---

## 十一、是否可进入独立复核

**可以。** `387 passed / 1 skipped / 0 failed`，4/4 变异被抓住，SDK 零改动，
安全开关保持关闭。详见 `US_P1_BATCH1_TEST_REPORT.md`。
