# NEXT_PHASE_REPORT.md —— 剩余安全问题收尾

**项目**：`C:\FosunAI-Trading`
**依据**：`FIX_REPORT.md`、`INDEPENDENT_AUDIT.md`
**时间**：2026-11-08 13:16 – 13:40 (+08:00)
**本轮允许修改代码，禁止真实交易**

---

## 0. 一页结论

| 编号 | 问题 | 状态 | 关键证据 |
|---|---|---|---|
| **P1-9** | AI 降级后仍用规则引擎交易 | ✅ **已修复**（行为已收口） | `tests/test_ai_authority.py` 17 例；9 个场景全部真跑 |
| **P1-11** | `--allow-insecure-bind` 逃生口 | ✅ **已移除** | `guard_bind(host)` 仅接受回环；非回环**有口令也拒绝** |
| 额外-1 | naive/aware 时区混算导致风控"内部异常"→ 全拒 | ✅ 已修（**本轮新发现**） | `risk/controller.py::_aware` |
| 额外-2 | 多连接并发首开健康库 `readonly database` | ✅ 已修（**本轮新发现**） | `journal._open_and_init` 重连重试 |
| 额外-3 | 哈希链跨进程并发写入断链 | ✅ 已修（**本轮新发现**） | `journal.log` 用 `BEGIN IMMEDIATE` 串行化 |

**测试**：`257 个用例，256 通过，1 跳过（LLM 真实调用，无 Key），0 失败`。较上轮 224 增加 **33 个**。

**⚠️ 行为变更（必须知悉）**：默认配置 `ai.provider=heuristic` 下，系统现在**只能分析、不能开仓**。
这是 P1-9 的设计结果 —— 要交易就必须配置真正的 AI（`AI_API_KEY`），规则引擎不再能顶替。

**安全开关**：仍是 `LIVE_TRADING=false` / `ORDER_EXECUTION_ENABLED=false` / `PAPER_TRADING=true`。

---

## 1. P1-9：AI 降级交易

### 1.1 修复前

`runtime.build_ai`：

```python
if getattr(model, "is_llm", False) and not getattr(model, "available", False):
    reason = "LLM 未配置 API Key（未测试），已降级为规则引擎 heuristic..."
    return build_model(cfg, force_provider="heuristic"), reason   # ← 降级后照样交易
```

后果：用户以为在用大模型，实际是均线规则在开仓；AI 挂掉时系统"悄悄换人"，
违反了"单 AI 自动交易"的定位。

### 1.2 修复后的判定链

```
                   ┌─────────────────────────┐
                   │ require_ai_for_entry ?  │  默认 true
                   └───────────┬─────────────┘
                        true   │
                               ▼
                   ┌─────────────────────────┐
                   │ model.available ?       │  LLM 有 Key 且构建成功
                   └───────────┬─────────────┘
                     false     │ true
                ┌────────────┐ ▼
                │AI_UNAVAILABLE│ ┌─────────────────────────┐
                └────────────┘ │ model.can_trade ?       │  规则引擎=False
                               └───────────┬─────────────┘
                                 false     │ true
                          ┌────────────────┐▼
                          │AI_NOT_DECISION │┌─────────────────────────┐
                          │  _AUTHORITY    ││ 处于故障冷却期 ?        │
                          └────────────────┘└───────────┬─────────────┘
                                                  true  │ false
                                        ┌───────────────┐▼
                                        │ AI_DEGRADED   │ 允许分析
                                        └───────────────┘
```

实现位置：

| 组件 | 文件 | 作用 |
|---|---|---|
| `AIModel.can_trade` | `ai/base.py` | 规则引擎/占位模型为 `False` |
| `UnavailableModel` | `ai/base.py` | LLM 不可用时的占位：`analyze()` 必抛 `AI_UNAVAILABLE` |
| `DecisionAuthority` | `ai/authority.py` | 四类拒绝码 + 熔断 + 信号新鲜度 |
| 引擎闸门 | `strategy/engine.py:analyze_symbol` | 调模型**之前**先过权威判定 |
| 装配 | `runtime.build_ai` | 不再降级；返回 `UnavailableModel` 并写明原因 |

### 1.3 四类拒绝码与语义

| 码 | 触发条件 | 运维含义 |
|---|---|---|
| `AI_NOT_DECISION_AUTHORITY` | 模型 `can_trade=False`（规则引擎） | 去配置真正的 AI，别指望规则引擎 |
| `AI_UNAVAILABLE` | LLM 无 Key / 构建失败 | 补 `AI_API_KEY` |
| `AI_DEGRADED` | 连续失败 ≥ N 次，冷却期内 | 等冷却或排查上游 |
| `STALE_SIGNAL` | 信号时间戳超过 `signal_ttl_seconds` | 说明信号被搁置太久（不应发生，属兜底） |

### 1.4 五条要求的落实

| 要求 | 落实方式 | 证据 |
|---|---|---|
| AI 正常响应且通过校验才允许开仓 | 权威闸门 → 模型调用 → `validate()` → 风控，四道串行 | `test_scenario_01` |
| 超时/断线/非法内容/API 错误禁止开仓 | 全部转成 `AIModelError` → `record_failure` → `_block_entry` | `test_scenario_02/03/04` |
| 规则引擎只能提供指标 | `can_trade=False`，引擎在调用前就拒绝 | `test_rule_engine_cannot_open_positions` |
| 已有持仓继续执行风控/止损 | `monitor()` → `risk.scan_exits` → `_execute`，**不经过**权威闸门 | `test_scenario_09` |
| AI 恢复后不补发旧信号 | ① 引擎从不缓存信号；② `check_freshness` 丢弃超 TTL 的信号 | `test_scenario_07`、`test_stale_signal_is_rejected` |
| 每次拒绝记录具体原因 | `journal.log("entry_blocked", {code, reason, symbol, model, action_taken})` | `test_rule_engine_cannot_open_positions` |

### 1.5 熔断与恢复

```python
max_consecutive_failures: 3     # 连续 3 次失败进入冷却
failure_cooldown_seconds: 120   # 冷却 2 分钟
signal_ttl_seconds: 300         # 信号 5 分钟内有效
```

冷却期内 `can_decide_entry()` 返回 `AI_DEGRADED`，**所有标的**都不开仓；
冷却结束后自动恢复，无需人工干预（但恢复后的第一笔仍是**新分析**的结果）。
恢复时会写一条审计 `ai_recovered`。

### 1.6 故意保留的"逃生口"（需你确认）

`ai.require_ai_for_entry: false` 可以让规则引擎开仓。保留原因：

* `ai-trader demo` 需要它（沙盒，走 `allow_rule_engine_entry=True`，并写审计 `rule_engine_sandbox`）
* 回测/参数调优也需要

**生产装配 `build_runtime` 永远传 `False`**，所以正常启动路径不可能打开它。
如果你希望彻底删除这个配置项，告诉我，我可以移除（代价是 demo 无法再演示下单）。

---

## 2. P1-11：不安全绑定逃生口

### 2.1 修复前

```python
def guard_bind(host, allow_insecure=False):
    if is_loopback(host): return
    if os.getenv("WEB_ADMIN_PASSWORD"): return      # 有口令就放行
    if allow_insecure: return                        # 或者显式开关放行
    raise SystemExit(...)
```

问题：① 有口令即可绑 `0.0.0.0`；② `--allow-insecure-bind` 相当于"我知道有风险"就放行。
两种都不符合"默认只允许 localhost"。

### 2.2 修复后

```python
def guard_bind(host: str) -> None:
    if is_loopback(host):
        return
    raise SystemExit(
        f"拒绝启动：管理台只能监听回环地址，收到 {host!r}。..."
    )
```

* `--allow-insecure-bind` **已删除**（CLI 解析它会直接报 unrecognized arguments）
* `allow_insecure` 参数**已删除**（函数签名只剩 `host`）
* 非回环**即使设置了 `WEB_ADMIN_PASSWORD` 也拒绝**

### 2.3 实测

```
$ ai-trader web --host 0.0.0.0 --port 8799
拒绝启动：管理台只能监听回环地址，收到 '0.0.0.0'。
管理台拥有急停/撤单/扫描权限，暴露到非回环地址必须另有认证方案。
如需公网访问，请保持 127.0.0.1 监听，并在前面加一层带鉴权的反向代理。
→ 退出码 4

$ ai-trader web --host 0.0.0.0 --allow-insecure-bind
→ error: unrecognized arguments: --allow-insecure-bind
```

### 2.4 公网访问的正确姿势（已写入 DEPLOYMENT）

```
Internet ──TLS──▶ Caddy/Nginx/IIS（Basic Auth / mTLS / IP 白名单）
                        │ 127.0.0.1:8781
                        ▼
                   FosunAI web（只监听回环）
```

即：**认证在反代层做，应用层永不直接暴露**。

---

## 3. AI 交易链路验证（9 个场景，全部真跑）

测试文件：`tests/test_ai_authority.py`。
场景 2/3/4 走**真实本机 HTTP 服务**（`127.0.0.1`，非 mock），因此超时、断连、非法响应是真的发生过的网络行为。

| # | 场景 | 构造方式 | 断言结果 |
|---|---|---|---|
| 1 | AI 正常分析并生成信号 | 本地假模型返回合法 JSON | `ordered`，落库 1 笔 `filled`，provider 正确 |
| 2 | AI 输出非法 JSON | 返回 `hello, I think you should buy. {"action": oops` | `blocked`，错误码含 `NO_JSON`，**0 委托** |
| 3 | AI 请求超时 | 服务 sleep 3s，客户端 read timeout 1s | `blocked`，耗时 < 3s（真的中断了），**0 委托** |
| 4 | AI 服务断开 | `server_close()` 后请求 | `blocked`，连接被拒，**0 委托** |
| 5 | 缺止损价格 | JSON 无 `stop_loss` | `rejected`，detail 含"缺少 stop_loss"，**0 委托** |
| 6 | 重复产生相同信号 | 同标的连续两次 scan | 第二次被 `duplicate_order` 拒绝，**只有 1 笔** |
| 7 | AI 恢复后重新分析 | 故障 → 冷却过后再 scan | 恢复后**新增 1 笔**（不是重放旧信号），审计含 `ai_recovered` |
| 8 | 风控拒绝 AI 信号 | `max_order_notional=1` | `rejected`，失败项来自风控而非 AI |
| 9 | AI 失效期间持仓仍受保护 | 建仓 → 停 AI 服务 → 抬止损触发 | `exit`，持仓清零，止损注册表清除 |

### 3.1 附加权威单测（5 例）

* `test_authority_breaker_opens_after_failures`：连续失败达阈值 → `AI_DEGRADED`；冷却后自动放行
* `test_authority_success_resets_failures`：成功一次清零失败计数
* `test_authority_freshness_boundaries`：10s 前的新鲜、120s 前的过期、非法时间戳拒绝
* `test_config_must_explicitly_allow_rule_engine`：只有显式配置才允许规则引擎
* `test_sandbox_flag_is_the_only_way_for_rule_engine`：sandbox 会留审计

---

## 4. 长期运行测试（7 项）

测试文件：`tests/test_long_running.py`（15 例）。

| 项 | 测试 | 做法 | 结果 |
|---|---|---|---|
| 1 | worker 连续运行 | 40 轮 scan+monitor；另有 **5 轮真实 worker 脚本** | 无异常、审计条数线性、冷却表不膨胀、链完整 |
| 2 | 网络断开与恢复 | 券商前 2 次抛 `BrokerError`，之后正常 | 前 2 次 `error`（不伪造数据），第 3 次 `ordered` |
| 3 | 进程异常退出与重启 | 子进程写 500 条后 `kill()`；CLI 进程强杀后重跑 | 链 `ok=True`，新进程可继续写；CLI 重跑 exit 0 |
| 4 | 急停状态持久性 | 连续 3 次"重启"（重建配置） | 每次都仍为停止；配置写 `false` 也盖不住哨兵 |
| 5 | 订单 UNKNOWN 恢复 | 进程 A 留 unknown → 进程 B `monitor` 对账 | 变成 `filled`，封锁解除；未对账前禁止再下单 |
| 6 | 数据库并发 | 6 线程 × 25 条写；8 线程抢同一冷却槽 | 链完整、总数精确 = 150；**恰好 1 个赢家** |
| 7 | 日志密钥泄漏 | 扫 `logs/`、`audit.db`、`.env` | **0 命中**（PEM/AWS/OpenAI/Bearer 四类模式） |

### 4.1 worker 连续运行（真实脚本，非单测）

```
$ powershell -File scripts\soak_test.ps1 -Iterations 5 -IntervalSeconds 3
  round 1/5  exit=0   1213ms  heartbeat=ok
  round 2/5  exit=0   1177ms  heartbeat=ok
  round 3/5  exit=0   1177ms  heartbeat=ok
  round 4/5  exit=0   1165ms  heartbeat=ok
  round 5/5  exit=0   1165ms  heartbeat=ok

汇总: 成功 5 / 并发跳过 0 / 超时 0 / 失败 0
审计链: ok=True total=130
SOAK_OK
```

原始输出：`audit_evidence/soak_5rounds.txt`。

---

## 5. 本轮新发现的 3 个缺陷（原审计没看到）

这三个都是**写并发测试时才暴露**的，属于"原来就存在、只是没人试过"的那类。

### 5.1 naive/aware 时区混算 → 风控全拒（P1 级，已修）

**现象**：`TypeError: can't compare offset-naive and offset-aware datetimes`

**触发**：调用方传 `now=datetime.now()`（naive），而订单时间是 aware ISO 字符串，
`_duplicate_check` 里 `ts >= cutoff` 直接抛异常 → `evaluate` 的 fail-closed 把它变成
`risk_engine_exception` → **整个系统拒绝一切交易**。

**影响**：可用性事故。fail-closed 本身是对的，但它把"代码 bug"误判成"风险事件"，
而且日志只说"内部异常"，很难定位。

**修复**：`risk/controller.py` 与 `ai/authority.py` 统一加 `_aware()`，
在入口把 naive 视为本地时间。修复后同一场景正常通过。

### 5.2 多连接并发首开健康库 → `readonly database`（P1 级，已修）

**现象**：8 个连接同时 `AuditJournal(db)` 时，3 个报
`sqlite3.OperationalError: attempt to write a readonly database`。

**根因**：部分连接在 `delete` 日志模式下打开，另一个连接把库切成 `WAL`，
前者之后写入就只读失败。Windows 上尤其明显。

**影响**：worker 与 Web 同时启动（计划任务 + 开机自启）时，可能有一个直接失败。

**修复**：`_open_and_init` 加重连重试；写操作统一走 `_retry_write`（遇到
`readonly/locked/busy` 就关闭连接、重连、重试）。

### 5.3 哈希链跨进程并发 → 断链（P1 级，已修）

**现象**：6 线程各写 25 条后，`verify_chain()` 报 `broken_at_seq=14`。

**根因**：`log()` 先 `SELECT` 最后一条 hash，再 `INSERT`。两个连接会读到同一个
`prev`，于是出现两条 prev 相同的记录 —— 链在结构上就断了。

**影响**：这是最严重的一个 —— **正常的并发写入会被误判成"审计被篡改"**，
而篡改告警是我们最重要的证据机制。

**修复**：`log()` 改为在 `BEGIN IMMEDIATE` 写事务内完成"读 prev + 写新行"，
配合 `isolation_level=None`（显式事务）。修复后 150 条并发写入链完整、总数精确。

---

## 6. 测试总览

```
$ pytest -o addopts="" -v
...
======================= 256 passed, 1 skipped in 27.28s =======================
junit: tests=257  failures=0  errors=0  skipped=1  time=27.183s
```

| 文件 | 用例 | 说明 |
|---|---|---|
| `test_ai_authority.py` | **17** | 本轮新增：9 场景 + 权威单测 |
| `test_long_running.py` | **15** | 本轮新增：7 项稳定性 |
| `test_risk_controls.py` | 34 | 回归 |
| `test_secrets_and_gate.py` | 27 | 回归 |
| `test_timeout_and_reconnect.py` | 17 | 回归 |
| `test_ai.py` | 17 | 回归 |
| `test_web.py` | 15 | 含绑定守卫新语义 |
| `test_market_data.py` | 15 | 回归 |
| `test_order_state_integrity.py` | 14 | 回归 |
| `test_paper_trading.py` | 12 | 回归 |
| `test_halt_persistence.py` | 12 | 回归 |
| `test_positions_and_pnl.py` | 11 | 回归 |
| `test_engine_flow.py` | 10 | 改为显式沙盒 |
| `test_cancel_chain.py` | 10 | 回归 |
| `test_audit.py` | 8 | 回归 |
| `test_sdk_install.py` | 7 | 回归 |
| `test_concurrency.py` | 7 | 回归 |
| `test_cli.py` | 6 | 回归 |
| `test_process_restart.py` | 3 | 回归 |
| **合计** | **257** | 0 失败 |

上轮 224 → 本轮 257，**新增 33 个**。

---

## 7. 新增问题清单

| 编号 | 级别 | 问题 | 状态 | 建议 |
|---|---|---|---|---|
| N-1 | P1 | naive/aware 时区混算导致风控全拒 | ✅ 已修 | 保持 `_aware()` 入口归一化；新增调用方传 naive 时间的测试 |
| N-2 | P1 | 并发首开健康库 `readonly database` | ✅ 已修 | 若未来引入更多写入方，考虑单写进程 + 队列 |
| N-3 | P1 | 哈希链跨进程并发断链 | ✅ 已修 | `BEGIN IMMEDIATE` 会让写串行化，高并发下吞吐下降（可接受） |
| N-4 | P2 | `check-env` 的日志走 stderr，`2>&1` 会污染 JSON | 已知 | 用法上不要合并流；或未来加 `--quiet` |
| N-5 | P2 | 默认 `provider=heuristic` 时系统"看起来在跑但不能交易" | 已知 | 已加 `/api/status` 与首页"开仓权限=无"徽标；文档已写明 |
| N-6 | P2 | `soak_test.ps1` 只测 worker，不测 Web 长跑 | 已知 | 需要时按同样思路加 |

---

## 8. 剩余实盘阻断项

按"必须解决才能开真实盘"排序。前 4 项是硬阻断。

| # | 阻断项 | 现状 | 为什么阻断 |
|---|---|---|---|
| **B1** | 没有星财富凭证（`base_url`/`API Key`/两段 PEM） | ❌ 未申请 | 无法验证任何真实响应字段名；`PARSE_MISMATCH` 会直接停手 |
| **B2** | 没有 AI Key（`AI_API_KEY`） | ❌ 未配置 | **P1-9 之后，没有 AI 就完全不交易**（这是设计） |
| **B3** | 券商协议未验证：行情加密后网关是否接受、`list_orders(client_id)` 是否生效 | ❌ 无凭证 | 若行情 POST 被拒，扫描全报错；若 clientId 过滤不生效，对账只能近似匹配 |
| **B4** | A股通 T+1 / 涨跌停未建模 | ❌ 未实现 | 会出现"以为能卖其实不能卖"，属规则风险 |
| B5 | 实时行情推送 `market_push` 未接入 | ❌ | 止损靠 300s 轮询，隔夜跳空保护不足 |
| B6 | 服务端条件单（`stopLossPrice`）语义未验证 | ❌ | 进程宕机即失去止损保护 |
| B7 | 汇率折算缺失 | ❌ | 多币种权益为名义加总，敞口计算失真 |
| B8 | 交易日历（港股/美股假期、半日市） | ❌ 近似表 | 可能在不该交易的时点下单 |
| B9 | P2 清单 8 项（`/api/docs` 免鉴权、Cookie 无 Secure、日志轮转等） | 部分 | 工程质量/加固 |
| B10 | 真实 LLM 输出质量与成本未知 | ❌ 无 Key | 无法评估信号稳定性与费用 |

**结论：当前不具备实盘资格。** 本轮把"代码层面的安全语义"补齐了，
但 B1–B4 是**外部依赖与业务建模**问题，不是再写几个测试能解决的。

---

## 9. 证据文件

| 文件 | 内容 |
|---|---|
| `audit_evidence/pytest_next_phase.txt` | 本轮 257 例逐条输出 |
| `audit_evidence/junit_next_phase.xml` | 机器可读结果 |
| `audit_evidence/soak_5rounds.txt` | worker 连续 5 轮 + 审计链校验 |
| `audit_evidence/check_env_next_phase.json` | 安全状态 + AI 权威摘要 |
| `audit_evidence/demo_next_phase.json` | 沙盒全链路（含止损触发、急停阻断） |
| `audit_evidence/healthcheck_after_fix.txt` | 健康检查（含心跳） |

---

## 10. 人工审核清单

- [ ] **确认 AI 决策权语义**：规则引擎不能开仓，必须配真正的大模型
- [ ] **确认默认行为变更**：默认 `provider=heuristic` → 系统只能分析不能交易
- [ ] **确认是否保留 `require_ai_for_entry` 开关**（当前保留，仅用于 demo/回测）
- [ ] **确认熔断参数**：`max_consecutive_failures=3`、`failure_cooldown_seconds=120`
- [ ] **确认信号 TTL**：`signal_ttl_seconds=300`
- [ ] **确认 P1-11 已彻底关闭**：非回环一律拒绝，无逃生口
- [ ] **确认新增的 3 个缺陷修复**（时区、并发建库、哈希链）无副作用
- [ ] 阅读第 8 节的 B1–B4，判断是否启动"申请凭证"流程

**在以上未全部确认前，建议保持 `PAPER_TRADING=true`。**

本轮到此结束，等待人工审核。
