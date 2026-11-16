# FIX_REPORT.md —— P0/P1 安全修复报告

**项目**：`C:\FosunAI-Trading`
**依据**：`INDEPENDENT_AUDIT.md` + `audit_evidence/`
**修复时间**：2026-11-08 12:36 – 13:10 (+08:00)
**本轮约束遵守情况**：

| 约束 | 状态 | 证据 |
|---|---|---|
| `LIVE_TRADING=false` | ✅ 未改动 | `audit_evidence/check_env_after_fix.json` |
| `ORDER_EXECUTION_ENABLED=false` | ✅ 未改动 | 同上 |
| `PAPER_TRADING=true` | ✅ 未改动 | 同上 |
| 未申请/填入真实凭证 | ✅ 保险箱 0 个键，`.env` 凭证位全空 | 同上 |
| 未调用真实下单/撤单/账户修改 | ✅ 所有网络行为只指向 `127.0.0.1` | 本文件 §7 |
| 未删除数据/重置数据库/覆盖备份 | ✅ 只做 `ALTER TABLE ADD COLUMN` 增量迁移 | `journal.py:162 _migrate_columns` |

---

## 0. 一页结论

| 编号 | 问题 | 状态 | 关键证据 |
|---|---|---|---|
| **P0-1** | 编造订单号 + 假定 submitted + 无法对账 | ✅ **已修复** | `test_order_state_integrity.py` 14 例全绿 |
| **P0-2** | 急停不持久，重启自动解除 | ✅ **已修复** | `test_process_restart.py` 真实子进程 3 例全绿 |
| P1-1 | 持仓解析失败静默返回 `[]` | ✅ 已修复 | `test_positions_and_pnl.py::test_positions_unknown_shape_raises_not_empty` |
| P1-2 | 缺 `dayPnl` 时熔断"安静通过" | ✅ 已修复 | `test_daily_loss_check_fails_when_pnl_unknown` |
| P1-3 | HTTP 无超时 | ✅ 已修复 | `test_timeout_actually_fires_on_real_socket`（真回环 1s 超时生效） |
| P1-4 | 订单无超时、挂单永不撤 | ✅ 已修复 | `test_stale_limit_order_is_cancelled` |
| P1-5 | 重复单冷却不跨进程 | ✅ 已修复 | `test_same_slot_only_one_wins_across_connections` |
| P1-6 | 撤单未接线（死代码） | ✅ 已修复 | `test_cancel_chain.py` 10 例 + Web/CLI 入口 |
| P1-7 | 无重连、无对账 | ✅ 已修复 | `test_auth_error_triggers_reconnect_then_retry`、`test_reconcile_passes_client_id` |
| P1-8 | SDK 行情加密（未验证即改上游） | ✅ **按约束处理**：未改上游，走官方 GET 兜底 | `test_sdk_source_matches_audited_baseline`、`test_quote_falls_back_to_min_on_decrypt_error` |
| P1-9 | 模型静默降级仍下单 | ⚠️ **部分修复** | 见 §9.1（需人工决策默认行为） |
| P1-10 | 迁移口令走命令行 | ✅ 已修复 | `migrate_export.ps1` 改用环境变量传参 |
| P1-11 | 绑 0.0.0.0 未强制口令 | ✅ **已修复**（默认硬拒绝，可显式绕过） | `guard_bind()` + `test_bind_guard_blocks_non_loopback_without_password` / `test_web_cmd_exit_code_when_bind_refused`，实测 exit 4 |
| P1-12 | `.env` ACL 过宽 | ✅ 已修复 | bootstrap 默认收紧（`icacls /inheritance:r`），实测 ACL 只剩当前用户；`-SkipAcl` 可跳过 |

**测试**：`224 个用例，223 通过，1 跳过（LLM 真实调用，无 Key），0 失败`。较修复前 145 个增加 **79 个**。
**变异检验**：8 种"把修复改回旧 bug"的注入，**8/8 被测试抓住**（§6）。

---

## 1. 变更清单

### 1.1 新增文件

| 文件 | 行数 | 作用 |
|---|---|---|
| `src/ai_trader/safety/halt.py` | 202 | 急停哨兵（原子写、fail-closed 读、确认短语） |
| `src/ai_trader/safety/__init__.py` | 21 | 包导出 |
| `tests/helpers.py` | 214 | `ScriptedBroker` / `FixedSignalModel` / `live_cfg` 测试替身 |
| `tests/test_halt_persistence.py` | 12 例 | P0-2 |
| `tests/test_order_state_integrity.py` | 14 例 | P0-1 |
| `tests/test_positions_and_pnl.py` | 11 例 | P1-1 / P1-2 |
| `tests/test_cancel_chain.py` | 10 例 | P1-4 / P1-5 / P1-6 |
| `tests/test_concurrency.py` | 7 例 | P1-5 并发 |
| `tests/test_timeout_and_reconnect.py` | 17 例 | P1-3 / P1-7 / P1-8 |
| `tests/test_process_restart.py` | 3 例 | 跨进程重启 |
| `audit_evidence/sdk_baseline.json` | — | 上游 SDK 26 个文件的 sha256 基线 |
| 多个 `audit_evidence/*.log/.txt` | — | 本轮证据 |

### 1.2 修改文件

| 文件 | 改动要点 |
|---|---|
| `broker/base.py` | `OrderStateUnknown` 异常；`OrderResult.status` 支持 `unknown`、新增 `client_id/reconcile_note`；`AccountSummary.day_pnl_known`；`Broker.reconcile_order()`；`derive_client_id()`；`OrderRequest.client_id` |
| `broker/fosun_adapter.py` | 重写：删除编造订单号、`OrderStateUnknown`、`reconcile_order`、`_read` 有限重试、`_install_http_timeout`、严格解析（`PARSE_MISMATCH`）、`day_pnl_known`、行情 GET 兜底、`health()` 增加网络/兜底状态 |
| `broker/paper.py` | 记录 `client_id`；实现 `reconcile_order` |
| `risk/controller.py` | `halt/resume` 落盘 + 确认短语；`refresh_halt_state`；新增 `halt_storage` / `unresolved_order_state` 两项检查；`day_pnl_known` 判定；跨进程冷却占位 `_reserve_or_block`；`_find_unresolved` |
| `audit/journal.py` | `order_attempts` 表（`UNIQUE(symbol,side,bucket)`）；`update_order_status` / `find_orders` / `reserve_order_attempt` / `release_order_attempt`；WAL + `busy_timeout`；`orders` 表增量补列 `client_id/reconcile_note` |
| `strategy/engine.py` | `_execute` 捕获 `OrderStateUnknown` → `_reconcile_unknown`；`cancel_order` 完整链路；`_expire_stale_orders`；`_reconcile_pending_unknown`；持仓读取失败阻断整轮；无止损持仓告警；`day_pnl_known` 透传 |
| `config.py` | `SafetyState` 增加 halt 字段；`build_gate(raw, data_dir)` 读哨兵；`AppConfig.halt_store` |
| `ai/providers.py` | AI 调用改为 `(connect, read)` 双段超时 |
| `web/app.py` | `/api/resume` 需 `confirm`；`/api/kill` 回 `persisted`；新增 `/api/orders/{id}/cancel` |
| `web/templates/dashboard.html` | 急停横幅；撤单按钮；解除需输入短语 |
| `cli.py` | `resume --confirm`（必填）；新增 `cancel`；`kill` 回显持久化结果 |
| `scripts/run_worker.ps1` | 命名 Mutex、逐步墙钟超时 + Kill、心跳文件、`-SelfTest` |
| `scripts/healthcheck.ps1` | 检查 `HALT` 哨兵异常与 worker 心跳时效 |
| `scripts/migrate_export.ps1` | 口令改走环境变量（不再出现在命令行） |
| `scripts/bootstrap_windows.ps1` | 新增 5b 步：`icacls /inheritance:r` 收紧 `.env`/`data\`/`vault.json`/`HALT.json`；新增 `-SkipAcl` |
| `config/config.yaml` / `.template.yaml` | `risk.order_timeout_seconds`、`fosun.http_*_timeout`、`read_retry_attempts`、`market_encryption_workaround` |

**规模变化**：`src 5,037 → 6,475` 行，`tests 1,575 → 3,253` 行，`scripts 622 → 804` 行；合计 `7,234 → 10,532` 行。

---

## 2. P0-1：订单状态完整性

### 2.1 修复前

**代码**（旧 `fosun_adapter.py:365-372`）：

```python
oid = str(_first(payload, ("orderId", "order_id", "id"), ""))
return OrderResult(
    order_id=oid or f"FS-{datetime.now().strftime('%H%M%S')}",   # ← 编造
    broker_order_id=oid,
    status=str(_first(payload, ("status", "orderStatus"), "submitted")),  # ← 假定成功
    ...
)
```

**行为**（旧探针实测）：

```
=== 1) 下单响应为空 data={} ===
  order_id='FS-121425'  broker_order_id=''  status='submitted'
  >>> 是否编造 ID: True  是否假定成功状态: True
```

**后果**：本地记录了一笔券商可能根本不存在的单；状态未知的单被当作"已提交"，导致
`max_positions`/`daily_trade_count` 计数污染、无法撤单、持仓对不上。

### 2.2 修复后

#### (a) 状态模型

`broker/base.py`：

```python
class OrderStateUnknown(BrokerError):
    """下单结果未知 —— 券商可能已受理，也可能没有。
    处理原则（禁止盲目重试下单）：
      1. 绝不假定 submitted 或 filled；
      2. 必须带 client_id 去券商侧对账；
      3. 对账未完成前，该标的禁止再下单。
    """
```

`OrderResult.status` 增加 `unknown`；新增 `client_id`、`reconcile_note`；`is_unknown` 属性。

#### (b) 不再编造订单号

`fosun_adapter.py:638-680`：

```python
try:
    resp = client.trade.create_order(**kwargs)
except BaseException as exc:
    if self._is_auth_error(exc):
        raise BrokerNotConfigured(f"下单前鉴权失败，未发出委托（可安全重试）: {exc}")
    raise OrderStateUnknown(..., code="ORDER_STATE_UNKNOWN", cause=...)

payload = _payload(resp) or {}
if not row:                                  # 空响应
    raise OrderStateUnknown(..., code="EMPTY_ORDER_RESPONSE")
if not broker_oid:                           # 有响应体但没 orderId
    return OrderResult(order_id=f"UNCONFIRMED-{client_id}",
                       broker_order_id="", status="unknown", ...)
status = str(_first(row, ("status", "orderStatus"), "") or "").strip().lower()
if not status: status = "unknown"            # 缺 status 不再默认 submitted
```

三种情况区分清楚：

| 情况 | 旧行为 | 新行为 |
|---|---|---|
| `data={}` | 编造 `FS-HHMMSS` + `submitted` | 抛 `OrderStateUnknown(EMPTY_ORDER_RESPONSE)` |
| 有字段但无 `orderId` | 编造 ID | `order_id=UNCONFIRMED-<clientId>`、`broker_order_id=""`、`status=unknown` |
| 有 `orderId` 但无 `status` | `submitted` | `orderId` 保留、`status=unknown` |
| 网络超时 | `requests` 异常上抛，本地无记录 | 抛 `OrderStateUnknown`，`client_id` 带回 |

#### (c) 稳定 client_id（幂等键）

`broker/base.py::derive_client_id(decision_id, signal_id, symbol, side)`：
同一决策永远得到同一值（sha256 前 12 位 hex 取模 2e9）。
适配器与引擎**用同一个函数**，所以"引擎算出来的键" == "发给券商的 clientId" == "对账时用的键"。

#### (d) 对账流程（禁止重试下单）

`engine.py:364 _reconcile_unknown()` 三种结果：

| 对账结果 | 落库状态 | 后续影响 |
|---|---|---|
| 查到订单 | 券商真实状态（`filled`/`submitted`/…） | 正常记账，取消封锁 |
| 明确查不到（返回 `None`） | `not_accepted` | 释放冷却槽位，允许后续下单 |
| 查询本身失败（抛错） | `unknown` | **保持封锁**，下一轮 `monitor` 再对账 |

全程 `place_order` 只调用一次（`test_engine_reconciles_when_broker_already_accepted` 断言 `broker.calls["place"] == 1`）。

#### (e) 封锁机制

`risk/controller.py:495` 新增检查 `unresolved_order_state`：

```python
unresolved = self._find_unresolved(sym, recent_orders)
if not self._check(checks, "unresolved_order_state", unresolved is None, ...):
    reasons.append(unresolved)
```

`_find_unresolved` 命中 `status in ("unknown", "pending_reconcile")` 即拒绝，
理由里带 `clientId`，提示"必须先完成对账才能继续交易"。

### 2.3 测试（14 例）

```
$ pytest tests/test_order_state_integrity.py -q
..............                                                           [100%]
14 passed
```

| 用例 | 断言 |
|---|---|
| `test_empty_payload_raises_unknown_state` | 空响应抛 `EMPTY_ORDER_RESPONSE`，`calls==1` |
| `test_missing_order_id_returns_unknown_not_submitted` | `status=="unknown"`，`broker_order_id==""`，`not startswith("FS-")` |
| `test_missing_status_with_order_id_is_unknown` | 有 ID 无状态 → `unknown` |
| `test_network_error_raises_unknown_with_client_id` | 超时 → `ORDER_STATE_UNKNOWN` + client_id + "禁止重试" |
| `test_auth_error_before_send_is_safe_to_retry` | 鉴权失败 → 非 unknown，文案"未发出委托" |
| `test_client_id_is_stable_for_same_decision` | 同决策同键，异决策异键 |
| `test_engine_reconciles_when_broker_already_accepted` | **"超时但券商已受理"** → 查到 BRK-REAL，落库 filled，只下过一次单 |
| `test_engine_marks_not_accepted_when_broker_has_no_order` | 查不到 → `not_accepted` |
| `test_engine_keeps_unknown_when_reconcile_fails` | 对账失败 → 保持 `unknown` |
| `test_unresolved_order_blocks_further_orders_on_symbol` | 有 unknown → 拒绝新单 |
| `test_not_accepted_order_does_not_block` | `not_accepted` 不封锁 |
| `test_monitor_reconciles_unknown_and_unblocks` | 二次对账成功 → 解除封锁 |
| `test_monitor_marks_not_accepted_on_second_pass` | 二次对账空 → `not_accepted` |
| `test_derive_client_id_is_deterministic` | 派生函数确定性 |

### 2.4 剩余风险

| 风险 | 说明 | 缓解 |
|---|---|---|
| 券商 `list_orders` 不支持 `clientId` 过滤 | 适配器有"最近订单近似匹配"兜底，但可能误配 | 匹配时要求 标的+方向+数量 全一致，并在 `reconcile_note` 标注"需人工复核" |
| `clientId` 溢出 | 模 2e9 后仍为 int32 范围；若券商要求更小需调整 | 已限制 `< 2_000_000_000` |
| 对账永远失败 | 状态停在 `unknown`，该标的永久封锁 | 这是**有意为之**（宁可停手）；`audit: order_unreconciled` 可告警 |

---

## 3. P0-2：急停持久化

### 3.1 修复前

`risk/controller.py:222-228`（旧）：

```python
def halt(self, reason):
    self.halt_reason = reason
    self.gate.state.kill_switch = True      # 只改内存
def resume(self):
    self.halt_reason = ""
    self.gate.state.kill_switch = False
```

**实测**（旧探针）：进程 1 `kill_switch=True` → 进程 2（重启）`kill_switch=False`，`data/` 无任何标记文件。

### 3.2 修复后

#### (a) 哨兵文件

`data/HALT.json`：

```json
{"halted": true, "reason": "HTTP 修复验证", "at": "2026-11-08T13:01:27+08:00", "actor": "web"}
```

写入用 `mkstemp` → `fsync` → `os.replace`（原子，不会出现半截文件）。

#### (b) 启动即生效（结构性，不依赖调用方记性）

`config.py:build_gate(raw, data_dir)`：

```python
if data_dir is not None:
    store = HaltStore(data_dir)
    hs = store.safe_read()
    if hs.halted:
        state.kill_switch = True            # 覆盖配置里的 false
        state.halt_source = hs.source
        state.halt_reason = hs.reason
    if store.failed:
        state.halt_store_failed = True      # 读不出来也停
        state.kill_switch = True
```

`load_config()` 自动传入 `data_dir`，`RiskController.__init__` 再 `refresh_halt_state()` 二次确认。
**任何入口（CLI / Web / worker / demo）拿到的 `gate` 都已经是停止态。**

#### (c) 读失败 → 停（fail-closed）

`HaltStore.safe_read()` 把这些情况全部转成"已停止 + `halt_store_failed`"：
文件损坏、JSON 非法、缺 `halted` 字段、`halted` 不是布尔、无读权限。

#### (d) 写失败 → 停 + 明确报错

`RiskController.halt()` 顺序：先置内存停止 → 再落盘。落盘失败时：

```python
except HaltStoreError as exc:
    self.halt_store_error = str(exc)
    self.gate.state.halt_store_failed = True   # 风控后续全部拒绝
    raise
```

风控新增检查 `halt_storage`（`controller.py:347`），只要 `halt_store_failed` 为真就拒绝一切交易。

#### (e) 解除需人工确认

`HaltStore.resume(confirmation)` 要求逐字等于 `RESUME-TRADING`：

```python
if not confirmation or confirmation != RESUME_PHRASE:
    raise HaltStoreError(f"解除急停需要精确确认短语 {RESUME_PHRASE!r}，收到 {confirmation!r}")
```

空字符串、大小写不符、带空格、`None` 全部拒绝（`test_resume_requires_exact_phrase` 逐个验证）。
CLI 的 `--confirm` 是**必填参数**；Web 的 `confirm` 校验失败返回 400；两者的拒绝都会写审计 `kill_switch_resume_rejected`。

### 3.3 测试（12 例 + 3 例跨进程）

```
$ pytest tests/test_halt_persistence.py -q
............                                                             [100%]
12 passed

$ pytest tests/test_process_restart.py -q
...                                                                      [100%]
3 passed
```

跨进程测试真实流程（`test_halt_survives_real_process_restart`，全部用 `subprocess` 起全新解释器）：

```
1) 进程A: ai-trader kill --reason "跨进程测试急停"   → exit 0, persisted=true, HALT.json 存在
2) 进程B: ai-trader status                        → kill_switch=true, source=sentinel, reason 含原文
3) 进程C: ai-trader scan                          → blocked_reason 非空, outcomes=[]
4) 进程D: resume --confirm resume-trading         → exit 2（拒绝）, 仍停止
5) 进程E: resume --confirm RESUME-TRADING         → exit 0, HALT.json 消失
```

### 3.4 剩余风险

| 风险 | 说明 | 缓解 |
|---|---|---|
| 有人手工删 `HALT.json` | 无技术手段阻止 | 写审计 `kill_switch_off`；`HALT.resumed.json` 留痕；建议加文件完整性监控 |
| `data` 目录整体不可写 | 写哨兵失败 → 进程内停止但重启后丢失 | 已置 `halt_store_failed` 使本进程全拒；**部署时应保证 data 可写**（见 DEPLOYMENT §6） |
| 与 `config.yaml` 的 `kill_switch: true` 混淆 | 配置里也有一个 `kill_switch` | 启动时 `halt_source` 会区分 `config` / `sentinel`，Web 与 `status` 均展示来源 |

---

## 4. P1-1：持仓读取失败必须报错

### 4.1 修复前

```python
for item in _as_list(_payload(resp)):   # 结构不符 → _as_list 返回 []
    ...
return out                              # 返回 []
```

实测：`{"foo":[{"bar":1}]}` → `get_positions() -> []` → 风控以为无持仓 → 仓位/敞口/持仓数上限**全部 fail-open**。

### 4.2 修复后

新增 `_extract_rows(payload) -> (rows, shape_ok)`，把"读不懂"与"确实为空"分开：

| 响应 | shape_ok | 结果 |
|---|---|---|
| `{"list": []}` | True | `[]`（真空账户，正常） |
| `{"list": [row...]}` | True | 解析 |
| `{"foo": [...]}` | False | **抛 `PARSE_MISMATCH`** |
| `{}` | False | **抛 `PARSE_MISMATCH`** |
| 行内缺标的代码 | — | **抛 `PARSE_MISMATCH`** |

`get_positions` 抛错文案明确写着"拒绝以「空持仓」继续交易"。

**引擎层再加一道**（`engine.py:530-538`）：

```python
try:
    self.broker.get_positions()
except BrokerError as exc:
    report.blocked_reason = f"持仓读取失败，禁止交易：{exc}"
    ... return report          # 整轮停手，不是跳过单标的
```

理由：风控的敞口/持仓数/占比全依赖持仓，缺失时对任何标的下单都是盲下。

### 4.3 测试

```
$ pytest tests/test_positions_and_pnl.py -q
...........                                                              [100%]
11 passed
```

关键 4 例：`test_positions_unknown_shape_raises_not_empty`、`test_positions_empty_dict_raises`、
`test_positions_genuinely_empty_is_ok`（防止误伤真空账户）、`test_scan_blocked_when_positions_read_fails`（断言 `broker.calls["place"] == 0`）。

---

## 5. P1-2：日内盈亏缺失必须拒绝

### 5.1 修复前

```python
day_pnl=_fnum(_first(row, ("dayPnl", "todayPnl", "dailyPnl"))),   # 缺字段 → 0.0
```

实测：`day_pnl=0` → `loss_pct=0` → `daily_loss_limit ok=True`，detail 显示"当日盈亏 +0.00"。

### 5.2 修复后

- `AccountSummary.day_pnl_known: bool`
- 适配器：`day_pnl_known = _has(row, _DAY_PNL_KEYS)`，缺字段时 `day_pnl=0.0` 但 `known=False`
- `Portfolio.day_pnl_known` 透传
- 风控（`controller.py:464`）：

```python
if not getattr(portfolio, "day_pnl_known", True):
    self._check(checks, "daily_loss_limit", False,
                "无法获取真实日内盈亏（券商未返回 dayPnl），无法判定单日亏损熔断 → 拒绝新开仓")
    reasons.append("日内盈亏不可用，单日亏损熔断无法判定，拒绝新开仓")
```

"不知道"等价于"不允许"。

### 5.3 测试（4 例）

`test_day_pnl_missing_marks_unknown`、`test_day_pnl_present_marks_known`、
`test_daily_loss_check_fails_when_pnl_unknown`、`test_engine_propagates_unknown_day_pnl`（适配器→引擎→风控全链路）。

---

## 6. P1-3 / P1-4 / P1-5 / P1-6 / P1-7 / P1-8

### 6.1 P1-3 HTTP 超时

**问题**：SDK 两处 `requests` 调用都没有 `timeout`（`client.py:174`、`auth.py:179`），网关挂起则进程永久阻塞。

**修复**（不改 SDK）：

1. `FosunBroker._install_http_timeout()` 包装实例的 `_http_session.request`，`kwargs.setdefault("timeout", (connect, read))`，幂等（打标记避免重复包裹）。
2. 握手走 `requests.post`，无法按实例包装 → `_create_session_guarded()` 用 `ThreadPoolExecutor` + `future.result(timeout)` 做墙钟限制。
3. AI 调用改为 `(connect_timeout, read_timeout)` 双段（`providers.py`）。
4. 配置：`fosun.http_connect_timeout: 10`、`fosun.http_read_timeout: 30`。

**测试**（17 例中含 3 例）：
- `test_http_timeout_injected_into_sdk_session`：断言 `timeout == (3.0, 7.0)`
- `test_http_timeout_not_installed_twice`：幂等
- `test_timeout_actually_fires_on_real_socket`：**真回环服务 sleep 3s，测得 1s 内抛 `ReadTimeout`，总耗时 < 2.5s**

### 6.2 P1-4 订单超时 + P1-6 撤单链路

**问题**：`refresh_pending` 只成交不撤单；`cancel_order` 全库无调用者（死代码）。

**修复**：

| 新增 | 位置 | 行为 |
|---|---|---|
| `risk.order_timeout_seconds`（默认 600，0=关闭） | 配置 | 挂单存活上限 |
| `StrategyEngine._expire_stale_orders(now)` | `engine.py:628` | `find_orders(statuses=[submitted/pending/partial], older_than=cutoff)` → 逐个 `cancel_order` → 状态置 `expired` |
| `StrategyEngine.cancel_order(order_id, reason)` | `engine.py:448` | 查单 → 状态校验 → 必要时先对账拿真实单号 → 调券商 → 回写状态 → 审计 |
| `POST /api/orders/{id}/cancel` | `web/app.py` | Web 入口 |
| `ai-trader cancel --order-id X` | `cli.py` | CLI 入口 |

`cancel_order` 的关键判断：

- `filled/cancelled/rejected/expired/not_accepted` → 拒绝（"不可撤销"）
- 本地无记录 → 拒绝（"本地无此订单记录"）
- 本地 `UNCONFIRMED-*`（无券商单号）→ 先 `reconcile_order`；查不到则标记 `not_accepted` 并返回"无需撤单"（**不发撤单请求**）
- 券商报错 → 不改状态，写 `cancel_failed`，返回 `retryable`

**测试**：`test_cancel_chain.py` 10 例，含 `test_stale_limit_order_is_cancelled`（700s 前的挂单被撤，券商侧也真的 `cancelled`）、`test_timeout_zero_disables_expiry`、`test_cancel_unknown_order_reconciles_first`（断言 `cancel==0`）、`test_web_cancel_endpoint`。

### 6.3 P1-5 跨进程重复单

**问题**：L1 冷却是内存 list，重启即失效。

**修复**：新增 `order_attempts` 表，用**数据库唯一约束**做互斥：

```sql
CREATE TABLE order_attempts (
    ..., symbol TEXT, side TEXT, bucket INTEGER, window_s INTEGER,
    UNIQUE(symbol, side, bucket)
)
```

`bucket = int(now.timestamp()) // window_seconds`。
`RiskController._reserve_or_block()` 在**所有检查通过之后**尝试插入；`IntegrityError` → 判为重复单并拒绝。
插入失败（磁盘/锁）→ 也拒绝（"无法写入跨进程冷却记录，拒绝下单以免重复"）。

同时 `PRAGMA journal_mode=WAL` + `busy_timeout=10000` 保证多连接并发不互相阻塞。

**测试**（7 例）：`test_same_slot_only_one_wins_across_connections`（两连接 + `threading.Barrier`，结果必须 `[True, False]`）、
`test_two_workers_place_only_one_order`（两个引擎、两个券商、共享审计库，**总委托数必须为 1**）、
`test_sequential_duplicate_after_success_is_blocked`。

### 6.4 P1-7 断线重连 + 对账

**修复**：

| 机制 | 实现 |
|---|---|
| 只读重试 | `_read()`：网络错误重试（默认 2 次，指数退避 0.2s×n） |
| 鉴权重连 | `_is_auth_error` → `_reconnect()`（`create_session` 带墙钟超时）→ 重试一次 |
| 下单不重试 | `place_order` 完全不走 `_read` |
| 撤单可重试 | 网络错误时重试一次（幂等动作） |
| 对账 | `reconcile_order(client_id)`：先按 `clientId` 过滤，再按 `clientId` 字段匹配，最后 标的+方向+数量 近似匹配（并标注"需人工复核"） |

**测试**：`test_read_retries_on_network_error`、`test_read_does_not_retry_beyond_limit`、
`test_auth_error_triggers_reconnect_then_retry`（断言 `session.creates==1`）、
`test_place_order_never_retried`（断言 `trade.calls==1`）、`test_reconcile_passes_client_id`（断言 `client_id=123456` 被传给 SDK）、
`test_reconcile_raises_when_unable_to_query`（"查不清"必须抛错而不是返回 None）。

### 6.5 P1-8 SDK 行情加密（按约束处理）

**约束回顾**：「修复前必须先核实官方协议，不允许直接修改第三方 SDK 后冒充官方兼容」。

**本轮的核实结论**（依据官方仓库源码，未做任何修改）：

| 事实 | 出处 |
|---|---|
| 官方 README 声明"行情接口走 `/market/`，仅签名不加密" | `sdk_src/README.md` |
| 实际判定代码 `request_path == "/market/"` 对真实路径恒为 False | `fsopenapi/client.py:114` |
| 期权分支用 `"/optmarket/" in ...`，但实际路径是 `/v1/market/opt/...`，同样不命中 | `fsopenapi/api/optmarket.py:41,49` |
| 因此**所有带 body 的 POST（含行情）都会被 AES-GCM 加密** | 实测 `probe_sdk_encryption.log`：`market.quote → encrypted=True`，解密回 `{"codes":["hk00700"]}` |
| 网关是否接受加密的行情 POST | **无法验证**（无凭证） |

**处理方式（不改上游）**：

1. `sdk_src/` 逐文件 sha256 基线化（`audit_evidence/sdk_baseline.json`，26 个文件）。
2. 适配器新增 `get_quote` 兜底：仅当 POST 行情因**解密/解析类错误**失败时，改用官方 GET `/v1/market/min`（未加密的公开端点）；成功后置 `_quote_via_min`，后续不再重复失败一次。
3. `health()` 暴露 `market_encryption_fallback` / `market_fallback_reason` / `sdk_modified: false`。
4. 配置 `fosun.market_encryption_workaround: auto|off` 可关闭。
5. 鉴权类错误**不**触发兜底（避免掩盖真正的问题）。

**测试**（4 例）：

```
test_sdk_source_matches_audited_baseline          # 26 个文件 sha256 全等
test_client_still_contains_unpatched_market_branch # `== "/market/"` 原样存在（证明没偷偷打补丁）
test_quote_falls_back_to_min_on_decrypt_error      # 兜底生效，source=fosun_market_min_fallback
test_quote_no_fallback_on_auth_error               # 鉴权错误不兜底
```

> 这不是"冒充官方兼容"，而是**客户端降级取价**，并且写进了 `API_STATUS.md` 与 `health()`。

---

## 7. 跨进程与并发证据（原始输出）

### 7.1 急停跨进程（真实子进程）

```
$ pytest tests/test_process_restart.py -q
...                                                                      [100%]
3 passed in 3.76s
```

### 7.2 两个 worker 并发同单

```
$ pytest tests/test_concurrency.py -q
.......                                                                  [100%]
7 passed in 0.54s
```

其中 `test_two_workers_place_only_one_order`：两个 `StrategyEngine`（独立券商 + 独立连接）同时
`scan(["hk00700"])`，断言 `paper_a 委托数 + paper_b 委托数 == 1`，且一个 `ordered` 一个 `rejected`。

### 7.3 worker 互斥（真实脚本）

```
=== 模拟另一个 worker 正在运行（持有同名 Mutex）===
父进程持有 mutex: True
[2026-11-08 13:00:12] === worker skipped: another instance is running ===
子 worker EXIT=4  (期望 4 = skipped_concurrent)
```

### 7.4 worker 超时监控（真实脚本自检）

```
$ powershell -File scripts\run_worker.ps1 -SelfTest
[2026-11-08 12:59:54] === SELFTEST start: child sleeps 5s, timeout is 1s ===
[2026-11-08 12:59:55] selftest TIMEOUT after 1s (pid=1884) -> killing
[2026-11-08 12:59:55] selftest code=124 timedOut=True
TIMEOUT_OK
EXIT=0  elapsed=1s
```

### 7.5 Web HTTP 层

```
$ curl -X POST -F "reason=HTTP 修复验证" .../api/kill
{"kill_switch":true,"reason":"HTTP 修复验证","persisted":true,...}

$ cat data/HALT.json
{"halted": true, "reason": "HTTP 修复验证", "at": "2026-11-08T13:01:27+08:00", "actor": "web"}

$ curl -X POST -F "confirm=" .../api/resume
HTTP 400
{"error":"解除急停需要 confirm=RESUME-TRADING（收到 ''）"}

$ curl -X POST -F "confirm=RESUME-TRADING" .../api/resume
{"kill_switch":false,...}   → HALT.json 已删除
```

页面在停止态会显示红色横幅（实测 HTML 含"系统处于停止状态" + 来源 + 原因 + `RESUME-TRADING` 提示），
解除后横幅消失（`grep -c` 为 0）。

### 7.6 worker 心跳监测（先失败后恢复，证明真的在监测）

```
# 心跳过期（上一轮是 mutex 测试，11 分钟前）
$ healthcheck.ps1
  [ OK ] http / audit_chain / paper_mode / no_real_order_in_paper / broker_kind
  [FAIL] worker_heartbeat: status=skipped_concurrent age=11.1min
健康检查失败：1 项        → EXIT=1

# 跑一次 worker 刷新心跳
$ run_worker.ps1
  monitor exit=0 timeout=False
  scan exit=0 timeout=False
  === worker ok ===       → EXIT=0

# 再检查
$ healthcheck.ps1
  [ OK ] worker_heartbeat: status=ok age=0.0min
健康检查通过。            → EXIT=0
```

原始输出：`audit_evidence/healthcheck_stale_heartbeat.txt`、`audit_evidence/healthcheck_after_fix.txt`。
这说明 `healthcheck` 检查的不是"进程是否存在"，而是"worker 是否真的在按时完成工作"。

---

## 8. 测试总览

```
$ pytest -o addopts="" -v
...
======================= 223 passed, 1 skipped in 13.59s =======================

junit: tests=224  failures=0  errors=0  skipped=1  time=13.659s
```

| 文件 | 用例数 | 覆盖 |
|---|---|---|
| `test_halt_persistence.py` | 12 | P0-2 |
| `test_order_state_integrity.py` | 14 | P0-1 |
| `test_positions_and_pnl.py` | 11 | P1-1 / P1-2 |
| `test_cancel_chain.py` | 10 | P1-4 / P1-5 / P1-6 |
| `test_concurrency.py` | 7 | P1-5 并发 |
| `test_timeout_and_reconnect.py` | 17 | P1-3 / P1-7 / P1-8 |
| `test_process_restart.py` | 3 | 跨进程重启 |
| 原有 10 个文件 | 145 | 回归（其中 `test_web` 11→14、`test_secrets_and_gate` 25→27 因新增守卫用例） |
| **合计** | **224** | |

修复前 145 → 修复后 224，**新增 79 个**，全部针对本轮修复点。
唯一跳过项仍是 `test_llm_real_call_if_key_present`（无 `AI_API_KEY`）。

### 8.1 审计要求的三类测试如何落实

| 要求 | 落实方式 |
|---|---|
| 有针对性测试 | 每个修复点至少 2 例正/反 |
| 有失败场景测试 | 空响应、超时、结构不符、缺字段、写盘失败、对账失败、撤单失败… |
| 有进程重启测试 | `test_process_restart.py` 用 `subprocess` 起真实新解释器（5 个进程依次执行） |
| 有并发测试 | `test_concurrency.py` 用 `threading.Barrier` + 两个独立 DB 连接 |
| 有回归测试 | 原 145 例全部保留并通过 |

---

## 9. 变异检验（证明测试不是空转）

把修复**改回旧 bug**，看测试是否抓得住。用 `audit_evidence/mutation_plugin.py` 在内存注入，不改源码：

| 注入的"回退" | 目标 | 结果 |
|---|---|---|
| 不拦状态未知的委托 | `test_order_state_integrity.py` | **1 failed** |
| 持仓解析失败返回 `[]`（旧 bug） | `test_positions_and_pnl.py` | **3 failed** |
| `dayPnl` 缺失当成 0 | `test_positions_and_pnl.py` | **3 failed** |
| 急停不落盘（只在内存） | `test_halt_persistence.py` | **6 failed** |
| 跨进程冷却永不拦截 | `test_concurrency.py` | **3 failed** |
| 订单超时撤单失效 | `test_cancel_chain.py` | **1 failed** |
| 风控全部放行 | `test_risk_controls.py` | **19 failed** |
| 门禁永远允许真实下单 | `test_secrets_and_gate.py` | **5 failed** |

**8/8 全部被捕获** → 这些测试确实与行为耦合。原始输出：`audit_evidence/mutation_report_after_fix.txt`。

---

## 10. 安全状态确认（修复完成后）

```json
// audit_evidence/check_env_after_fix.json
{
  "safety": {
    "live_trading": false,
    "order_execution_enabled": false,
    "paper_trading": true,
    "kill_switch": false,
    "halt_source": "none",
    "halt_store_failed": false,
    "live_authorized": false,
    "real_order_authorized": false
  },
  "fosun_configured": false,
  "blocked": "PAPER_TRADING=true，真实券商连接被禁用"
}
```

保险箱 `key_count = 0`；`.env` 的 4 个凭证位全空；本机 `*.pem` 数量为 0。
**本次修复未产生、未填写任何真实或虚假凭证。**

---

## 11. 未解决问题与剩余风险

### 11.1 P1-9 模型静默降级（部分修复）

**现状**：`build_ai` 在 LLM 缺 Key 时仍会降级为规则引擎并继续交易，但已在 `status` 与 Web 横幅暴露 `ai_fallback_reason`。

**未做**：没有在 live 模式下"降级即禁止下单"。

**原因**：这会改变产品行为（用户可能就想用规则引擎打底），需要产品决策，不适合在安全修复里单方面改。

**建议**：新增 `ai.allow_fallback`（默认 `true`），当 `PAPER_TRADING=false` 时若 `allow_fallback=false` 则拒绝启动。**留给人工审核决定**。

### 11.2 P1-11 非回环绑定未强制口令（已修复，但保留逃生口）

**现状**：`web/app.py::guard_bind()` **默认硬拒绝**，退出码 4：

```
$ ai-trader web --host 0.0.0.0 --port 8799
拒绝启动：绑定非回环地址 '0.0.0.0' 时必须设置 WEB_ADMIN_PASSWORD。
否则任何能访问该端口的人都能操作急停/撤单/扫描（无鉴权）。
若确实需要无鉴权暴露，请显式加 --allow-insecure-bind 并自担风险。
→ 真实退出码: 4
```

**保留的逃生口**：`--allow-insecure-bind` 会打印醒目警告后放行。

**为什么不做成绝对禁止**：某些内网部署确实用反向代理做鉴权（Caddy basic-auth），
此时管理台本身不需要口令。**这是需要人工判断的取舍**，所以做成"默认拒绝 + 显式声明"。

**残余风险**：有人为了省事加了这个开关。缓解：启动横幅会持续打印 `[WARN] 无鉴权暴露`。

### 11.3 仍未验证（依赖凭证）

| 项 | 影响 |
|---|---|
| 券商响应真实字段名 | `_extract_rows` 的"结构不符"可能把正常响应判为错 → 现在会**报错停手**（安全方向），但会阻断交易 |
| `list_orders` 是否支持 `clientId` 过滤 | 若不支持，只能走近似匹配（已在 `reconcile_note` 标注"需人工复核"） |
| 网关是否接受加密行情 POST | 未知；已备 GET 兜底 |
| `orderType` 取值语义 | 仍按 README 的 1/3 推测 |
| A股通代码格式 | 仍按 `sh/sz` 前缀推测 |

### 11.4 已知但本轮未处理（原 P2）

`/api/docs` 免鉴权、`paper_state.json` 损坏静默重置、Cookie 无 `Secure`、无 FX 折算、交易时段近似、`audit.jsonl` 无轮转。见 `INDEPENDENT_AUDIT.md` 的 P2 清单。

### 11.5 修复引入的新风险（诚实列出）

| 新风险 | 说明 | 缓解 |
|---|---|---|
| 持仓接口一有异常就整轮停手 | 可用性下降 | 这是有意的安全取舍；`scan.blocked_reason` 明确可查 |
| `unknown` 状态可能长期封锁标的 | 若对账接口一直不可用 | `monitor` 每轮重试；`audit: order_unreconciled` 可告警 |
| 冷却槽位被"失败的下单"占用 300s | 下单被券商拒绝时会白等 | 已实现 `release_order_attempt`，正常失败会释放 |
| `ALTER TABLE` 迁移 | 旧库自动补列 | 幂等、只加列、不动数据；已用现有 `data/audit.db` 实测通过 |
| worker 依赖命名 Mutex | 跨会话（不同 Windows 用户）不互斥 | `Global\` 前缀已覆盖同机多会话；跨机器需分布式锁 |

---

## 12. 人工审核清单

请逐项确认后再决定是否推进真实交易：

- [ ] 认可 **P0-1** 的处理口径：无订单号 → `unknown` → 必须对账 → 未对账前封锁该标的
- [ ] 认可 **P0-2** 的处理口径：急停落盘、重启仍停、`RESUME-TRADING` 才能解除
- [ ] 确认 `data/HALT.json` 的运维流程（谁有权删、删了如何留痕）
- [ ] 决定 **P1-9**：LLM 降级时是否允许继续交易（当前：允许 + 显式告警）
- [ ] 决定 **P1-11**：是否保留 `--allow-insecure-bind` 逃生口（当前：默认硬拒绝，显式开关可绕过）
- [ ] 审核 `risk.order_timeout_seconds=600` 是否符合你的交易周期
- [ ] 审核 `fosun.http_read_timeout=30` 是否足够（行情高峰可能更慢）
- [ ] 拿到凭证后，先跑 `ai-trader check-env` 与只读连通性验证，**再**考虑 `PAPER_TRADING=false`
- [ ] 认可"仍未验证"清单（§11.3）中的 5 项都属于客观阻塞

**在以上未全部确认前，建议继续保持 `LIVE_TRADING=false` / `ORDER_EXECUTION_ENABLED=false` / `PAPER_TRADING=true`。**

本轮修复到此为止，等待人工审核。
