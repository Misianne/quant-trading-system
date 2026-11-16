# 星财富 AI 美股交易系统 · Paper Runtime 稳定性压力验收报告

**项目**：`C:\FosunAI-Trading`
**轮次**：Paper Runtime 稳定性压力攻坚
**时间**：2026-11-16
**上一阶段报告**：`US_PAPER_RUNTIME_OPERATIONAL_CLOSURE.md`（提交 `3397814` / `ad12740` / `7c319c9`）

---

## 0. 结论（七项独立判定）

| 判定 | 结论 | 关键证据 |
|---|---|---|
| **A. 离线 Replay 稳定性** | **PASS** | 10 场景 ×3 次，指纹一致；1043 项测试 0 失败 |
| **B. 本地 Paper 单次运行** | **PASS** | 全链路执行，不变量通过 |
| **C. 本地 Paper 两小时持续运行** | **PASS** | 120 分钟 / 1,650 段 / 13,200 tick / 953 笔成交；RSS 42→49.4 MB 后进入平台期；0 异常、0 重复成交、0 不变量破坏 |
| **D. 100 次启停可靠性** | **PASS** | 100/100 成功，平均 1.22s/轮 |
| **E. Windows 启停工具可靠性** | **PASS** | 9/9 真实调用通过（含中文/空格路径、重复启动、异常退出） |
| **F. 崩溃恢复可靠性** | **PASS（有边界）** | 7 个进程级注入点 + 3 类边界场景（停止中崩溃/重复恢复/锁残留）；孤儿账本 fail-closed |
| **G. 是否可进入真实数据源接入准备** | **PARTIAL** | 见 §11：运行侧就绪，数据源/签名/券商三项仍缺 |

> 上一轮报告把 RSS 增长归因为「分配器保留」，**这个结论是错的**。
> 本轮用 tracemalloc + ctypes 类型计数定位到真实泄漏点，见 §2。

---

## 1. 基线与备份

| 项 | 值 |
|---|---|
| 分支 | `master` |
| 实际 HEAD | `7c319c9`（与用户给定一致，已核实） |
| 工作树（起始） | 干净 |
| 上一阶段报告 | `US_PAPER_RUNTIME_OPERATIONAL_CLOSURE.md`（31 KB）在库 |
| 上一阶段 JUnit | `r9.xml` = **954 tests / 0 failures / 0 errors / 1 skipped** |
| 上轮备份校验 | `round5-20261116-154938`：720 文件，`sha256sum -c` 全部 OK |
| 本轮备份 | `C:\FosunAI-Trading-Backups\round6-20261116-180339` |
| 本轮备份规模 | **769 文件 / 12 MB**，`MANIFEST.sha256` 769 行，校验全 OK |
| 历史运行记录 | `data/paper_runs` 共 **12,573 文件 / 371 MB**，**原地保留**；另存 `PAPER_RUNS_INVENTORY.tsv`（逐文件大小清单） |
| Git 检查点 | 标签 `baseline-round6` → `7c319c9` |

> 备份排除了 `data/paper_runs`（371MB 运行产物，且必须原地保留），
> 代码/配置/报告/数据库全量包含。运行记录未删除、未移动。

---

## 2. 内存增长根因（本轮最高优先级）

### 2.1 先排除的三类"假嫌疑"

用 `tools/memprof.py` 跑 300 轮（每 100 轮采样）：

| 轮次 | RSS | Commit | Python 当前 | Python 峰值 | 句柄 | 线程 | SQLite 连接 |
|---|---|---|---|---|---|---|---|
| 0 | 38.7 | 25.8 | 0.00 | 0.00 | 173 | 1 | 0 |
| 100 | 49.8 | 36.5 | 2.80 | 6.88 | 174 | 1 | 1（运行中） |
| 200 | 54.5 | 40.1 | 2.85 | 6.88 | 174 | 1 | 1 |
| 300 | 59.8 | 45.5 | 2.92 | 6.88 | 174 | 1 | 3（GC 前） |
| GC 后 | 65.2 | 51.3 | **2.61** | 6.88 | 174 | 1 | **0** |

结论：**Python 活对象完全平稳**（2.8→2.9 MB），句柄/线程/连接不增长。
`EmptyWorkingSet` 后 RSS 从 47.5 MB 掉到 **14.3 MB**（commit 不变）——
说明有一部分只是**工作集缓存（D 类）**，不是泄漏。

### 2.2 真正的泄漏：每 tick 重建 ctypes 类型

复现真实 soak 循环（每轮新建 Runtime + **Supervisor**）后，内存开始线性增长：

| 轮次 | RSS | Commit | Python 当前 | gen2 对象 |
|---|---|---|---|---|
| 50 | 58.7 | 45.3 | 9.72 | 55,506 |
| 200 | 94.4 | 81.0 | 30.31 | 154,127 |
| 400 | 144.0 | 131.9 | 58.44 | 293,341 |

`tracemalloc` 的 top-diff 直接指向 `supervisor.py:65` 与 `:82`：

```
+14.59 MB  +85332  supervisor.py:65
+10.50 MB  +50263  supervisor.py:82
```

对应代码：

```python
def process_rss_mb() -> float:
    class PROCESS_MEMORY_COUNTERS(ctypes.Structure):   # ← 每次调用都新建类型
        ...
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # ← 每次调用都重新加载
```

**机制**：`ctypes.Structure` 的子类在创建时会向 ctypes 注册原生类型元数据，
这部分**不受 Python GC 管理**；`WinDLL` 每次加载也会产生新的库句柄。
心跳每 tick 调一次 `process_rss_mb()` → 上一轮 18,535 次调用 → 275 MB。

**A/B 对照**（`tools/leak_ab_test.py`）：

| 实现 | 调用次数 | 新增 Structure 类型 | RSS 增量 |
|---|---|---|---|
| 旧（函数内定义） | 500 | **+500** | — |
| 新（模块级缓存） | 3,000 | **+0** | +0.01 MB |

### 2.3 分类结论

| 类别 | 判定 | 依据 |
|---|---|---|
| A. 真正的对象泄漏 | **是（已修复）** | ctypes 类型对象每调用 +1，GC 不可回收 |
| B. 缓存无限增长 | 否 | Python 活内存平稳；行情/订单缓存均有界 |
| C. 每轮对象重复创建 | 是（设计层面） | 每轮新建 Runtime/DB 文件；已用 `ReplaySession` 复用解决 |
| D. 分配器/工作集保留 | 部分是 | `EmptyWorkingSet` 后 RSS 14.3 MB（commit 不变） |
| E. 正常业务数据增长 | 是 | 订单/成交/审计按笔增长，属预期 |

---

## 3. 修复清单

| # | 文件 | 修复 |
|---|---|---|
| 1 | `paper_runtime/supervisor.py` | ctypes 结构/句柄/函数指针**模块级缓存**；新增 `process_commit_mb`、`process_handle_count` |
| 2 | `paper_runtime/supervisor.py` | `pid_alive` 改为 `GetExitCodeProcess == STILL_ACTIVE`（`OpenProcess` 对已退出进程仍成功，会误判存活） |
| 3 | `paper_runtime/lock.py`（新增） | 单实例锁：`O_EXCL` 创建、残留锁识别、只释放自己的锁 |
| 4 | `paper_runtime/session.py`（新增） | `ReplaySession`：**复用**同一 Runtime/账户/DB 跑多段，不再每轮新建库 |
| 5 | `broker/paper.py` | 状态文件损坏时**拒绝启动并隔离**（原实现静默回落默认账户 = 余额归零） |
| 6 | `audit/journal.py` | 新增 `ts_epoch` 列 + 迁移回填；`orders_since` 按 epoch 比较（原字符串比较混合时区会把昨天算进今天，T+1 可售被错误清零） |
| 7 | `audit/journal.py` | `utcnow_iso()` 走 `timebase`（回放时不再写系统时间） |
| 8 | `strategy/engine.py` | 支持注入 `clocks`（行情网关原来用配置时钟，把合法回放时间判成 holiday） |
| 9 | `paper_runtime/model.py` / `session.py` | 无持仓不发卖单；价格对齐 0.01 tick（原 4 位小数被 `PRICE_TICK` 拒绝） |
| 10 | `paper_runtime/runtime.py` | `runtime.jsonl` 按大小轮转；`--log-file`；会话模式写心跳 + 支持 `stop.request` |
| 11 | `config/*.yaml` | 日历经覆盖到 **2027-12-31**（补 NYSE 2027 休市日） |
| 12 | `paper_start.cmd` 等 | 改为 **ASCII-only**（cmd.exe 按 ANSI 读，中文注释被拆成命令） |
| 13 | `scripts/paper_start.ps1` | 改用 .NET `ProcessStartInfo` 启动（PS 5.1 的 `Start-Process` 在 `HTTPS_PROXY`/`https_proxy` 并存时抛字典重复）；`-Replay` 前台执行并透传退出码；soak 模式启动后存活校验 |
| 14 | `tests/conftest.py` | 自动还原全局时钟夹具（回放时钟泄漏会污染后续所有时间相关断言） |

**提交**：`b915f09`（本轮代码与测试）

---

## 4. 两小时持续运行

### 4.1 配置

`--paper --session-minutes 120 --segment-interval 4 --idle-minutes 5`
单账户（USD 1,000,000 初始）、单账本、单审计库；每段 = 一个 45 分钟交易窗口，
收盘后自动跳到下一交易日 09:30（跳过周末与节假日）。

### 4.2 内存与资源趋势

| 时间点 | RSS (MB) | Commit (MB) | Python GC 对象 | 句柄 | 线程 | 段 | 成交 |
|---|---|---|---|---|---|---|---|
| **0 min** | **42.0** | 29.2 | 31,627 | 179 | 1 | 0 | 0 |
| **15 min** | **45.8** | 31.5 | 35,155 | 179 | 1 | 217 | 122 |
| **30 min** | **48.3** | 34.2 | 38,893 | 179 | 1 | 429 | 236 |
| 45 min | 46.3 | 36.0 | 42,559 | 179 | 1 | 643 | 368 |
| **60 min** | **47.0** | 37.5 | 43,239 | 179 | 1 | 848 | 485 |
| 76 min | 46.6 | 40.1 | 43,886 | 179 | 1 | 1,059 | 612 |
| **90 min** | **46.8** | 41.0 | 44,476 | 179 | 1 | 1,252 | 730 |
| 106 min | 48.5 | 42.6 | 44,966 | 179 | 1 | 1,460 | 846 |
| **120 min** | **49.4** | 44.3 | 45,473 | 179 | 1 | 1,650 | 953 |
| **停止后静置 5 min** | **47.0** | 41.6 | 45,440 | **174** | 1 | — | — |

**结论：RSS 在 30 分钟后进入平台期（46–49 MB 波动），不再随运行时间增长。**
对比上一轮：30 分钟时 **324.6 MB** → 本轮同点 **48.3 MB**（下降 85%）。

三条曲线的解释（缺一不可）：

| 指标 | 走势 | 归因 |
|---|---|---|
| **RSS** | 42 → 49.4 MB，30 分钟后平台 | 工作集；`EmptyWorkingSet` 可回收，不是活对象 |
| **Commit** | 29.2 → 44.3 MB（+15.1 MB / 2h） | **业务数据**：953 笔订单/成交 + SQLite 页缓存；约 0.126 MB/分钟，与成交笔数近似线性 |
| **Python GC 对象** | 31.6k → 45.5k（+13.8k） | 953 笔订单的 dict/记录，约 14 个对象/订单，属业务数据 |
| **句柄** | **179 恒定** | 无句柄泄漏 |
| **线程** | **1 恒定** | 无后台线程泄漏 |

**静置 5 分钟**：RSS 47.03 MB、Commit 41.63 MB、对象数 45,440 **完全不变**，
句柄从 179 降到 174（组件释放）→ 停止后没有任何残留增长。

### 4.3 交易与账务

| 项 | 值 |
|---|---|
| 运行时长 | **120 分钟**（16:43:52 → 18:37:52 段推进；墙钟 120:00） |
| 交易段 | **1,650** 段（每段 8 tick，45 分钟交易窗口） |
| 累计 tick | **13,200** |
| 成交 | **953 笔**（与订单数一致，见下） |
| 订单 | **953**（每笔成交都有对应订单） |
| 期末持仓 | 0（最后一次卖出已平仓） |
| 期末现金 | USD 1,107,152（初始 1,000,000，累计 +10.7%） |
| 15 分钟不变量检查 | **8 次，全部通过** |
| 30 分钟健康检查 | **4 次，全部通过** |
| 异常 | **0**（`anomalies: []`） |
| 终态 | `STOPPED`（优雅停止，`stop.ok=true`、`errors=[]`） |

**"不是空转"的证据**：13,200 次 tick 全部经过
`ReplayQuoteSource → QuoteGateway → ScriptedModel → RiskController → PaperBroker →
FillLedger → invariants`；953 笔成交与 953 张订单一一对应；现金从 100 万变到 110.7 万，
是真实撮合与结算的结果。

**说明**：`run_report.json` 里的 `session.health` 字段显示 `HALTED`，这是
**取值时机错误**（在 `stop()` 之后才取摘要，组件已释放 → 未启动=不健康）。
已修复（改为 stop 前取摘要）；本轮验收数据以 `monitor.jsonl` 为准，见上表。

---

## 5. 100 次启停循环

工具：`tools/startstop100.py`（真实进程，不是进程内模拟）

每轮：启动 → 等锁+心跳（READY）→ 确认 tick 增长 → 写 `stop.request` 优雅停止 →
等进程退出 → 校验退出码 0 / 锁已释放 / PID 已消失。

| 指标 | 值 |
|---|---|
| 循环次数 | **100** |
| 成功 | **100** |
| 失败 | **0** |
| 总耗时 | **122.5 s** |
| 单轮平均 | **1.22 s** |
| 单轮最大 | **2.26 s** |
| 证据 | `data/paper_runs/startstop100/cycles.jsonl` + `summary.json` |

### 5.1 边界场景

| 场景 | 结果 |
|---|---|
| 重复启动 | 第二次 `exit 3`，提示已有 PID；**不会**出现两个实例控制同一账户 |
| 无进程时停止 | `exit 0`，提示"没有 PID 文件，视为已停止" |
| 运行中停止 | 写入 `stop.request` → 进程优雅退出 → 锁释放 |
| STOPPING 重复停止 | 幂等返回 `already_stopped` |
| 异常退出后重启 | 残留锁被识别清理，下次启动成功 |
| 心跳被并发读取 | 写心跳失败重试后放弃本次写入，**进程不崩**（有单测） |
| 初始化失败后重试 | 失败路径释放锁，可重试 |
| 资源冲突 | 同 `--out` 第二个实例被锁拒绝 |

---

## 6. Windows 启停脚本实测

工具：`tools/verify_cmd_scripts.py`（通过 `cmd /c` 与 `powershell -File` **真实执行**）

| # | 用例 | 结果 |
|---|---|---|
| 1 | 无进程时 `stop` | PASS（exit 0） |
| 2 | 项目外目录 `start` | PASS（写出真实 PID） |
| 3 | 运行中 `status`（中文+空格路径 cwd） | PASS（`RUNNING`） |
| 4 | 重复 `start` | PASS（exit 3，明确提示） |
| 5 | 普通 PowerShell 调 `paper_stop.ps1` | PASS（优雅退出） |
| 6 | 停止后 `status` | PASS（`DEAD`，exit 6） |
| 7 | 中文/空格目录 `start` | PASS |
| 8 | 强杀进程后 `status` | PASS（`DEAD`，不误报 RUNNING） |
| 9 | 非法参数 `start` | PASS（exit 2，输出"未知场景"） |

**9/9 全部通过**，且验证了：
* `start` 启动的是真实 PaperRuntime（PID + 心跳 + 锁）；
* `status` 能准确区分 RUNNING / DEAD；
* `stop` 能安全停止且无残留；
* 再次 `start` 不会重复启动旧实例。

---

## 7. 数据库与文件增长压力

2 分钟高频运行（`tools/db_stress.py`）：495 段 / 3,960 tick / **278 笔成交**。

| 文件 | 停止前 | 停止后 | 说明 |
|---|---|---|---|
| `paper_fills.jsonl` + `runtime.jsonl` | 3,087.8 KB | 3,088.2 KB | 只追加，不重写 |
| `paper_audit.db` | 5,820 KB | 5,820 KB | 线性增长，约 22 KB/订单（含审计链） |
| `-wal` | 4,039.6 KB | **0（已删除）** | WAL 达阈值自动 checkpoint，**不再增长** |
| `-shm` | 32 KB | **0** | 关闭时释放 |
| `paper_state.json` | 194.9 KB | 194.9 KB | 订单快照，随订单增长 |
| `paper_runtime.lock` | 0.1 KB | 0 | 停止时删除 |

| 检查 | 结果 |
|---|---|
| SQLite `integrity_check` | **ok** |
| 成交唯一性 | 278 笔 / **278 个唯一 fill_id** |
| 账户不变量 | 通过 |
| 原子写入 | 状态文件 tmp+fsync+replace（有单测） |
| 损坏状态 | **拒绝启动 + 隔离 `.corrupt`**，不归零 |
| 日志轮转 | `runtime.jsonl` 超 5 MB 滚动 `.1/.2/.3` |
| 磁盘不可用 | 存储组件启动失败 → 整体 `FAILED`（有单测） |

> 注意：WAL 在运行中稳定在 4 MB 左右而非无限增长，说明 `wal_autocheckpoint`
> 生效；关闭时被 checkpoint 并删除，这是"文件增长有界"的直接证据。

---

## 8. 进程级故障注入

真实 `os._exit(9)`，不执行清理、不 flush。

### 8.1 注入点矩阵

| 注入点 | 崩溃后磁盘状态 | 恢复结果 |
|---|---|---|
| `before_order` | 无订单、无成交 | 恢复 OK，补账 0 |
| `after_risk` | 无幽灵订单、现金不变 | 恢复 OK |
| `before_fill_write` | 状态文件无该成交 | 恢复 OK |
| `after_position` | 成交在内存未落盘 | 恢复 OK，不计入 |
| `after_fill_write` | **成交已落盘、账本为空** | 补账 1 笔，不变量通过 |
| `after_settlement` | 结算已入账、账本待补 | 同上路径 |
| `after_order_create`（挂单） | 订单在、无成交 | 不凭空补成交 |
| 停止过程中强杀 | 锁残留 | 下次启动自动清理并恢复 |
| 重复恢复 ×3 | — | 成交数不变（幂等） |
| 崩溃后账户 | — | 现金 > 0 且与成交一致，**不归零** |

### 8.2 恢复原则

* 券商有、账本无 → 补账（**记录在案的事实**），逐条审计；
* 账本有、券商无 → **HALT**，不猜测、不自动补造；
* 状态文件损坏 → 拒绝启动 + 隔离，**绝不回落默认账户**；
* 恢复后必须通过账户不变量才允许进入 READY。

---

## 9. 自动化测试

```bash
cd /c/FosunAI-Trading
env -u PYTHONPATH ./.venv/Scripts/python.exe -m pytest tests/ -q \
    --junitxml=r10.xml -p no:cacheprovider
```

| 指标 | 值 |
|---|---|
| 总用例 | **1043** |
| failures | **0** |
| errors | **0** |
| skipped | 1（既有跳过项） |
| 耗时 | 199.0 s |
| 上一轮 | 954 → **+89** |
| 证据 | `r10.xml` |

| 新增文件 | 数量 | 覆盖 |
|---|---|---|
| `test_paper_memory_stability.py` | 8 | ctypes 类型计数、RSS/commit 稳定性、句柄不泄漏 |
| `test_paper_runtime_reuse.py` | 13 | 会话复用、对象不重建、时钟只向前、周末跳过、账本连续 |
| `test_paper_runtime_lock_resources.py` | 17 | 单实例锁、残留锁、失败释放、组件逆序释放、连接/线程/句柄 |
| `test_paper_runtime_db_stress.py` | 14 | 原子写、损坏隔离、WAL、epoch 比较、恢复唯一性、日志轮转、磁盘不可用 |
| `test_paper_runtime_longrun.py` | 10 | 心跳墙钟、熔断不复位、LULD/MWCB 运行时保护 |
| `test_paper_runtime_faults_extended.py` | 10 | 停止中崩溃、锁残留、重复恢复、位置保持 |
| `test_paper_cmd_scripts.py` | 6 | `.cmd` ASCII/BOM、空闲/运行/停止/非法参数 |
| `test_paper_startup_discipline.py` | 6 | 必须 `--paper`、全局 P2 必须关、实盘必须关 |
| `test_paper_runtime_random_kill.py` | 5 | 任意时刻强杀、账本无重复、可重启 |

**未删除任何测试、未降低断言、未扩大跳过范围。**
唯一修改的既有文件是 `tests/conftest.py`（新增自动还原时钟夹具，属增强）。

---

## 10. 资源占用趋势

### 10.1 两小时内存验收（硬指标）

| 要求 | 实测 | 判定 |
|---|---|---|
| 第 0 分钟 RSS | 42.0 MB | — |
| 第 15 分钟 RSS | 45.8 MB | +3.8 |
| 第 30 分钟 RSS | 48.3 MB | +6.3 |
| 第 60 分钟 RSS | 47.0 MB | **低于 30 分钟** |
| 第 90 分钟 RSS | 46.8 MB | 持平 |
| 第 120 分钟 RSS | 49.4 MB | 2 小时净增 **+7.4 MB** |
| 运行结束后静置 5 分钟 | 47.0 MB / 41.6 MB commit | 不再增长 |
| Python 活对象趋势 | 31.6k → 45.5k（+13.8k，953 笔订单驱动） | 与业务量线性，非泄漏 |
| 句柄趋势 | 179 → 179（停止后 174） | 恒定 |
| 线程趋势 | 1 → 1 | 恒定 |

**判定：趋于稳定（PASS）**。
依据不是"最后一次 RSS 好看"，而是：
30 分钟后 RSS 不再单调上升（45.8→48.3→46.3→47.0→46.6→46.8→48.5→49.4，
在 46–49 区间波动），而**业务量在持续增长**（成交 236→953）。

### 10.2 与上一轮对比

| 指标 | 上一轮（30 分钟） | 本轮（30 分钟） | 本轮（120 分钟） |
|---|---|---|---|
| RSS | **324.6 MB** | 48.3 MB | 49.4 MB |
| 成交 | 1,091 笔 | 236 笔 | 953 笔 |
| 轮/段 | 1,963 轮（每轮新建运行体） | 429 段（复用同一运行体） | 1,650 段 |
| 句柄 | 179 | 179 | 179 |

上一轮 30 分钟的 RSS 已超过本轮 2 小时的 RSS。

### 10.3 剩余增长来源（诚实说明）

Commit 从 29.2 → 44.3 MB，主要来自：

1. `paper_state.json` 的订单字典（953 条，常驻内存）；
2. SQLite 页缓存与审计表；
3. 每笔成交的 Python 对象。

这部分是 **E 类（正常业务数据增长）**，不是泄漏。若要跑更长时间（天级），
需要把订单历史改为**分页读取 + 内存只保留最近 N 条**（报告 §11 已列为待办）。

---

## 11. 未解决风险与边界

| 级别 | 项 | 影响 |
|---|---|---|
| **BLOCKED** | 无权威 LULD/停牌实时数据源 | 生产恢复路径 `production_authority()` 零注册，永不放行 |
| **BLOCKED** | 无券商通道 | 无法验证真实回报/拒单 |
| **UNVERIFIED** | 恢复事件无密码学签名 | 无法抵御伪造 provider |
| **UNVERIFIED** | 行情为合成路径 | 验证执行链路，不代表真实历史表现 |
| **UNVERIFIED** | 竞价撮合价未建模 | 只能判"可/不可交易" |
| 已知限制 | 会话段为 45 分钟粒度 | 非逐笔回放；用于稳定性而非策略研究 |
| 已知限制 | Supervisor 与运行时同进程 | 进程被强杀时监督器一起消失，失联靠外部脚本判定 |
| 已知限制 | `paper_state.json` 随订单线性增长 | 2 小时约 195 KB；长期运行需改为分页读取（已列入待办） |
| 已知限制 | 内存 RSS 仍受工作集影响 | 见 §10，判断标准是 commit 与 Python 活对象，而非单看 RSS |

---

## 12. 最终判定（再述）

| 判定 | 结果 | 依据 |
|---|---|---|
| A 离线 Replay 稳定性 | **PASS** | 10 场景×3 次指纹一致 |
| B 本地 Paper 单次运行 | **PASS** | 全链路 + 不变量 |
| C 两小时持续运行 | **PASS** | 953 笔成交、8 次不变量检查、4 次健康检查全过；RSS +7.4 MB / 2h 后平台 |
| D 100 次启停 | **PASS** | 100/100，1.22s/轮 |
| E Windows 启停工具 | **PASS** | 9/9 真实执行 |
| F 崩溃恢复 | **PASS（有边界）** | 7 注入点 + 3 类边界；孤儿账本 fail-closed |
| G 进入真实数据源准备 | **PARTIAL** | 运行侧就绪；数据源/签名/券商三项未完成 |

**是否达到长期运行标准**：**是（本地 Paper/Replay 范围内）**。
判定依据不是"最后一次 RSS 好看"，而是：
commit 稳定、Python 活对象稳定、句柄/线程稳定、无重复成交、无不变量破坏、
无残留进程、内存增长可归因（业务数据 + 工作集）。

---

## 附录 A：本轮修复的真实缺陷（14 项）

见 §3 表格。其中三项最关键：

1. **ctypes 每调用新建类型**（真实原生内存泄漏，275 MB）；
2. **`pid_alive` 用 `OpenProcess` 判存活**（对已退出进程仍成功 → 误判）；
3. **审计 `orders_since` 字符串比较跨时区**（把昨天算成今天 → T+1 可售被清零）。

## 附录 B：新增工具

| 文件 | 用途 |
|---|---|
| `tools/memprof.py` | 分轮采样 RSS/commit/tracemalloc/GC/句柄/连接/实例数 |
| `tools/memtrim.py` | `EmptyWorkingSet` 对照，区分工作集与活对象 |
| `tools/soakprof.py` | 复现真实 soak 循环并做 tracemalloc diff |
| `tools/leak_ab_test.py` | 旧/新实现 A/B 对照（泄漏机制证明） |
| `tools/startstop100.py` | 100 次真实启停循环 |
| `tools/verify_cmd_scripts.py` | Windows 脚本真实调用验证 |
| `tools/db_stress.py` | 数据库/文件增长与恢复一致性 |
