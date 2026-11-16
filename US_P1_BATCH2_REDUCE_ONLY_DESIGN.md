# US_P1_BATCH2_REDUCE_ONLY_DESIGN.md —— R-3 / R-8 设计与测试方案（**不启用**）

**项目**：`C:\FosunAI-Trading`
**状态**：**仅分析 + 测试设计。本轮未启用、未改默认值、未新增审批逻辑。**
**当前取值（实测）**：

```
risk.halt_write_failure_exit_mode = "block"     （config.yaml 与代码默认均未改动）
```

> 任何启用动作都需要人工书面批准，本文只提交方案，不构成批准。

---

## 1. 结论先行

| 问题 | 结论 |
|---|---|
| `risk.halt()` 与 `kill_switch` 的真实顺序 | `halt()` **先**置 `kill_switch=True`（内存），**再**落盘；落盘失败时 `mark_halt_storage_failure(phase="write")` |
| reduce-only 在完整链路中是否可达 | **不可达**。唯一能产生 `halt_write_failed` 的路径是 `halt()`，而它必然先置 `kill_switch`；`kill_switch` 检查不分方向 → 减仓直接被打回 |
| 是否有人工审批 / 审计 / 撤销 / 恢复机制 | **全部缺失**：改配置文件即可，无审计事件、无审批留痕、无撤销流程、无"启用后定期复核"机制 |
| 建议 | **维持 block**。若要启用，必须先补齐"分方向 kill_switch + 审批审计 + 撤销"三项，否则该开关只是纸面功能 |

---

## 2. `risk.halt()` 与 `kill_switch` 的真实调用顺序

源码（`src/ai_trader/risk/controller.py::halt`）：

```
L  9:  self.gate.state.kill_switch = True          ← ① 先让本进程进入停止态
L 17:  st = store.halt(reason, actor=actor)        ← ② 再尝试落盘
L 24:  self.gate.state.mark_halt_storage_failure(  ← ③ 失败时打标 phase="write"
           kind=..., phase="write")
```

设计意图是合理的（"先停止、后持久化"，避免出现"以为停了其实没停"）。
但它对 reduce-only 造成了结构性后果：

* 只要走 `halt()`，`kill_switch` 一定为 `True`；
* `evaluate()` 的第 1 项检查 `kill_switch` **不区分 `is_exit`**；
* 因此 `halt_write_failure_exit_mode="reduce_only"` 的分支永远进不去（被更早的 `kill_switch` 拦住）。

**实测（复现命令见 §5）**：

```
kill_switch = True | write_failed = True | kind = dir_failed
减仓 approved = False
拦截项 = ['kill_switch']
可达性结论: 不可达（kill_switch 先阻断）
```

### 2.1 为什么"理论上还有别的路径"，但实际都为 0

| 可能来源 | 是否会产生 `write_failed` | 说明 |
|---|---|---|
| `risk.halt()` | ✅ | 唯一真实来源；但必然连带 `kill_switch` |
| `build_gate()` | ❌ | 只做 `safe_read()`（读），只会产生 `read_failed` |
| `refresh_halt_state()` | ❌ | 同样只读；`store.last_phase` 为 `read` |
| `resume()` | ❌ | 失败即抛，且失败时仍保持停止 |
| 手工 `mark_halt_storage_failure(phase="write")` | ✅ | **仅测试代码会这么干**，生产中无调用方 |

**推论**：`reduce_only` 在生产路径上是**死分支**；而 batch1 的用例
`test_halt_write_failure_reduce_only_channel_structure` 正是手工打标构造的，
因此"测试通过"并不能证明该通道可用 —— 这一条已在 Batch1 复核（R-4）中被点出，
本批予以确认并记录。

---

## 3. 若将来要启用：必须解决的四个缺口

### 3.1 缺口 A：`kill_switch` 需要"方向化"

现状：`kill_switch` 是一个布尔，语义是"停止一切"。
要允许 reduce-only，必须引入**第三态**，例如：

```
kill_scope = "all" | "reduce_only" | "none"
```

* `all`：默认，**一切**拒绝（当前行为）
* `reduce_only`：仅允许"已核实持仓内的卖出"，且必须通过 §3.3 的全部约束
* `none`：正常

**关键约束**：`kill_scope` 的降级（`all → reduce_only`）**只能由人**触发，
不能由代码在"写失败"时自动降级 —— 否则等于给"停止态不可持久化"开了自动闸门。

### 3.2 缺口 B：审批与审计

当前**零审计**：把 `halt_write_failure_exit_mode` 从 `block` 改成 `reduce_only`
只需编辑 YAML，重启即生效，没有任何事件记录。

最小可行方案：

1. 配置项扩展为
   ```yaml
   risk:
     halt_write_failure_exit_mode: "block"          # block | reduce_only
     halt_write_failure_exit_approval:
       approved_by: ""                              # 必填人名/工号
       approved_at: ""                              # ISO 时间
       approval_ref: ""                             # 工单/邮件编号
       expires_at: ""                               # 必须过期（建议 ≤ 7 天）
   ```
2. 启动时校验：若 `mode=reduce_only` 而三个审批字段任一为空 / 已过期 → **拒绝启动**（fail-closed）
3. 每次走 reduce-only 放行时写审计事件 `exit_authorized_halt_write_failure`
   （含 `approved_by / approval_ref / 持仓 / 可卖余量 / client_id`）
4. `check-env` 输出该模式的当前状态与到期时间

### 3.3 缺口 C：启用后仍必须满足的硬约束（缺一不可）

即便审批通过，每次放行仍必须复用 Batch2 已有的约束：

| 约束 | 现有能力 | 落点 |
|---|---|---|
| 卖出方向且标记为退出 | ✅ | `OrderIntent.is_reduce_only` |
| 持仓**本轮实时核实** | ✅ | `Portfolio.positions_verified` |
| 持仓 > 0 | ✅ | `_reduce_only_gate` |
| **数量 ≤ 可卖余量（持仓 − 未完成卖单预留）** | ✅（Batch2 新增） | `Portfolio.sellable_now()` / 风控 `sell_capacity` 检查 |
| 预留量读不出来 → 禁卖 | ✅（Batch2 新增） | `reservations_known=False` |
| 订单幂等（预写 + client_id 唯一） | ✅（Batch2 新增） | `journal.prewrite_intent` |
| 不得重复提交未知单 | ✅ | `unresolved_order_state` |

**这意味着**：启用 reduce-only 并不会绕过 Batch2 的任何一道闸门，
它只是把"kill_switch 一律拒绝"放宽为"kill_switch 允许受限减仓"。

### 3.4 缺口 D：撤销与恢复

| 动作 | 现状 | 需要补充 |
|---|---|---|
| 撤销（改回 block） | 编辑 YAML + 重启 | 审计事件 `halt_exit_mode_revoked`（谁/何时/原因） |
| 到期自动失效 | 无 | `expires_at` 到期后自动按 `block` 处理（启动时判定，运行期每日判定） |
| 急停恢复 | `resume()` 需精确短语 | 保持；reduce-only 期间**不得**自动清除 `kill_scope` |
| 发生一次 reduce-only 放行后 | 无标记 | 建议在 `FAULT.json` 打 `reduce_only_used=true`，强制人工复核 |

---

## 4. 建议的处置（三选一，需人工决策）

| 方案 | 内容 | 优点 | 缺点 |
|---|---|---|---|
| **A（推荐）维持 disabled** | 保留 `block`，文档明确"该开关不可用" | 零风险；不误导运维 | 急停写失败时无法止损（与 Batch2 的 S-1 同源问题） |
| **B 实现后启用** | 补 §3.1~§3.4 四项，默认仍 `block`，按审批开启 | 真能在极端场景减仓 | 改动面大（kill_switch 方向化影响所有检查项） |
| **C 删除该开关** | 移除 `halt_write_failure_exit_mode` 与相关分支 | 消除"纸面功能"的误导 | 未来若要用需重写 |

**本轮建议 A**，并在 `check-env` 里把该开关标注为
`⚠️ 未实现（kill_switch 优先阻断）`，避免运维误以为可用。

---

## 5. 复现命令（只读，可独立复核）

```powershell
cd C:\FosunAI-Trading
.\.venv\Scripts\python.exe - <<'PY'
import pathlib, sys, tempfile
sys.path.insert(0, "src"); sys.path.insert(0, ".")
import tests.test_us_p1_batch1 as B1
from ai_trader.risk.controller import RiskController, OrderIntent, Portfolio
from ai_trader.market.rules import MarketRuleSet
from ai_trader.broker.base import Position
from ai_trader.safety.halt import HaltStore, HaltStoreError

tmp = pathlib.Path(tempfile.mkdtemp(prefix="r3_"))
bad = tmp/"data_as_file"; bad.write_text("x", encoding="utf-8")
cfg = B1._cfg(tmp, **{"risk.halt_write_failure_exit_mode": "reduce_only"})
cfg.halt_store = HaltStore(bad)
risk = RiskController(cfg, cfg.gate, rules=MarketRuleSet(cfg.raw["market_rules"]))
try: risk.halt("probe")
except HaltStoreError: pass
pf = Portfolio(currency="USD", equity=1e6, cash=1e6, positions_verified=True,
               positions=[Position(symbol="usAAPL", market="us", qty=10,
                                   avg_cost=200.0, last_price=200.0, currency="USD")])
it = OrderIntent(symbol="usAAPL", side="sell", qty=10, price=200.0, market="us",
                 currency="USD", is_exit=True, exit_mode="stop_loss")
d = risk.evaluate(it, pf, last_price=200.0, now=B1.NOW)
print("减仓 approved =", d.approved, "拦截 =", [c.name for c in d.checks if not c.ok])
PY
```

**期望输出**（与本文一致）：`approved = False, 拦截 = ['kill_switch']`。

---

## 6. 若批准启用：测试设计（本轮**不实现**）

> 以下为方案，实施时须全部通过才允许开启。

### 6.1 可达性

| # | 用例 | 期望 |
|---|---|---|
| 1 | 写失败 + `mode=reduce_only` + 合法减仓 | 放行，审计含 `exit_authorized_halt_write_failure` |
| 2 | 写失败 + `mode=reduce_only` + 买入 | 拒绝（`kill_scope=reduce_only` 只允许卖） |
| 3 | 写失败 + `mode=reduce_only` + 卖出超过可卖余量 | 拒绝（`sell_capacity`） |
| 4 | 写失败 + `mode=reduce_only` + 预留量不可读 | 拒绝（`reduction_blocked_unverified`） |
| 5 | 读失败（`corrupt`）+ `mode=reduce_only` | **仍全阻断**（读失败不得走该通道） |
| 6 | `kill_switch` 由人工 `halt()` 置位（非存储故障） | 不得因 `mode=reduce_only` 放行减仓 |

### 6.2 审批与撤销

| # | 用例 | 期望 |
|---|---|---|
| 7 | `mode=reduce_only` 但 `approved_by` 为空 | 启动被拒（fail-closed） |
| 8 | `approval_ref` 为空 | 启动被拒 |
| 9 | `expires_at` 已过期 | 启动被拒 / 运行期自动降级为 block |
| 10 | 运行期撤销（改回 block） | 下一轮 `refresh` 生效并写 `halt_exit_mode_revoked` |
| 11 | `resume()` 后 `kill_scope` | 恢复为 `none`，不得残留 reduce_only |

### 6.3 回归

| # | 用例 | 期望 |
|---|---|---|
| 12 | `mode=block`（默认）下全部既有测试 | 全部通过，行为与 Batch2 完全一致 |
| 13 | 配置缺省（无该字段） | 按 `block` 处理 |

### 6.4 验收门槛

* 6.1~6.3 全部通过；
* `check-env` 能显示当前模式、审批人、到期时间；
* 至少一次"演练"：在模拟环境人为制造写失败，确认只放行减仓且审计完整；
* 演练报告经人工签署后才允许在实盘配置中开启。

---

## 7. 与 Batch2 的关系

* 本批**未**修改 `halt_write_failure_exit_mode` 的默认值或判定逻辑；
* 本批新增的 `sell_capacity` / 预留量 / 预写幂等，恰好是未来启用 reduce-only
  所必需的前置约束 —— 也就是说 **Batch2 已经把"启用前的安全底座"补齐了**，
  剩下的是"如何安全地打开这个开关"的治理问题，而不是技术能力问题。
* 结论：**该开关继续保持关闭**；是否启用，等待人工审批。
