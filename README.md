# FosunAI-Trading

星财富（复星财富 / Fosun Wealth）**单 AI 自动交易系统** —— 准备工程。

面向港股、美股、A股通，把「AI 判断 → 独立风控 → 下单 → 审计」串成一条**可审计、可阻断、默认不碰真钱**的链路。

> **当前状态：仅历史回测与模拟盘。真实交易从未开启，也未验证过盈利能力。**
> 四道开关全部关闭：`live_trading=false`、`order_execution_enabled=false`、
> `us_phase2.enabled=false`、`market_feed.enabled=false`。仓库内**没有任何真实凭据**，
> 因此从未连接券商、未读取真实账户、未提交过真实订单。

### 研发阶段（截至 2026-11-20）

| 阶段 | 内容 | 结论 |
|---|---|---|
| P0 / P1 | 订单状态完整性、急停持久化、持仓盈亏 fail-closed、重连对账、AI 决策权威、仅回环监听 | 已修复，见 [FIX_REPORT.md](FIX_REPORT.md) |
| 行情源审计 | 星财富 OpenAPI REST / WebSocket 实测 | WS 订阅被服务端 `denied`（账户未开通实时行情权限），代码保留、默认关闭 |
| 日线波段架构 | 用已完成日线替代实时推送 | 已实现；**回测跑不过买入持有**（详见 [US_DAILY_SWING_ARCHITECTURE_REPORT.md](US_DAILY_SWING_ARCHITECTURE_REPORT.md)） |
| 宏观数据库 | 12 条 FRED 序列、104,854 个版本行、点时间查询 | 已建成，见 [docs/macro_db.md](docs/macro_db.md) |
| 宏观研究 | 6 状态分类 × QQQ 前瞻收益（60 个月末决策） | **未发现显著增量价值**，见 [reports/macro_research/macro_regime_research.md](reports/macro_research/macro_regime_research.md) |
| 事件情报层 | 官方事件时间模型 + 事件研究 | 免费源**没有发布级一致预期**，因此只能做风控、不能做方向，见 [reports/market_research/expectations_event_sources.md](reports/market_research/expectations_event_sources.md) |

> **验证边界（请务必读完再判断这个项目）**
> 1. 没有实盘记录，**没有任何真实盈亏数据**；
> 2. 日线策略的结论是**负面的**（跑不过买入持有），不是"待优化"；
> 3. 宏观层的结论也是**负面的**（p 值全部不显著，唯一显著的只有"事件日波动更大"）；
> 4. 回测使用未复权收盘价（拆股已调整、分红未调整），且只覆盖 2021-11 起的 5 年；
> 5. 未做交易成本之外的滑点建模，未考虑杠杆 ETF 的每日复位损耗之外的费用。
>
> ⚠️ **未配置 AI 时系统只做分析、不会开仓**（规则引擎无开仓决策权）。

---

## 1. 这个工程解决什么问题

自动交易系统的失败大多不是"AI 不够聪明"，而是：

| 风险 | 本工程的对策 |
|---|---|
| AI 给出无止损、仓位过大的信号 | 信号必须自带上行/下行价格，且被风控二次裁剪 |
| 风控被"绕过" | 券商层只能在 `StrategyEngine._execute()` 内被调用，而该函数第一行就检查 `decision.approved` |
| 半夜连续亏损无人管 | 单日亏损上限触发后自动停止新开仓 |
| 重复下单 / 手抖双击 | 同标的同方向冷却窗口 + 未完成委托检测 + 每分钟频率上限 |
| 出事说不清 | 每个动作落 SQLite + JSONL，并串成哈希链，可校验是否被篡改 |
| 测试时误触真钱 | 三重开关 + 确认短语，缺一不可 |

---

## 2. 架构

```
                 ┌──────────────────────────────────────────────┐
                 │  Web 管理台 (FastAPI, :8781)                  │
                 │  看：状态/持仓/委托/信号/风控/审计             │
                 │  做：扫描/监控/紧急停止/恢复/重置模拟盘        │
                 └──────────────────┬───────────────────────────┘
                                    │ 只调用下面这层的公开方法
┌───────────────────────────────────▼───────────────────────────┐
│  StrategyEngine        行情 → 上下文 → 模型 → 定仓 → 执行       │
│  ├─ ai/                统一模型接口（可替换）                  │
│  │   ├─ HeuristicModel         规则引擎（无需密钥，确定性）     │
│  │   └─ OpenAICompatibleModel  DeepSeek / OpenAI / 任意兼容端点 │
│  ├─ risk/RiskController  ★ 唯一放行闸门（24 项检查，fail-closed）│
│  └─ audit/AuditJournal   业务记录 + 哈希链审计                  │
└───────────────┬───────────────────────────────┬────────────────┘
                │                               │
      ┌─────────▼─────────┐          ┌──────────▼──────────────┐
      │ PaperBroker       │          │ FosunBroker             │
      │ 本地模拟撮合       │          │ 星财富官方 SDK 适配器    │
      │ 合成行情(确定性)   │          │ 受 SafetyGate 三重门禁   │
      └───────────────────┘          └─────────────────────────┘
```

关键设计：**AI 没有执行权**。模型只返回 `TradingSignal`（建议），风控返回 `RiskDecision`（裁决），只有 `approved=True` 的裁决才能变成 `OrderRequest`。

---

## 3. 目录结构

```
C:\FosunAI-Trading\
├── sdk_src\                    官方 SDK（git clone，未修改，审计基线）
├── src\ai_trader\
│   ├── config.py               配置加载 + SafetyGate（三重开关）
│   ├── secrets.py              DPAPI 保险箱 + 口令派生加密 + 脱敏工具
│   ├── runtime.py              唯一装配入口
│   ├── cli.py                  命令行（status/scan/monitor/kill/demo/vault/...）
│   ├── broker\
│   │   ├── base.py             统一数据结构与接口契约
│   │   ├── paper.py            模拟撮合（含确定性合成行情）
│   │   └── fosun_adapter.py    星财富适配器（含宽松响应解析）
│   ├── ai\base.py, providers.py 统一模型接口 + 两种实现
│   ├── strategy\               策略引擎 + 纯函数技术指标
│   ├── risk\controller.py      ★ 独立风控（24 项）
│   ├── audit\journal.py        审计日志 + 哈希链
│   └── web\                    FastAPI 管理台 + 模板
├── config\config.yaml          主配置（安全默认值）
├── config\config.template.yaml 基线副本（供 VPS 重建）
├── scripts\                    bootstrap / run_web / run_worker / backup /
│                               migrate_export / healthcheck
├── tests\                      139 个测试
├── logs\data\                  运行期数据（已 gitignore）
├── README.md  REPORT.md  DEPLOYMENT.md  RISK_CONTROL.md  API_STATUS.md
└── .env.example
```

---

## 4. 五分钟跑起来（模拟盘）

```powershell
cd C:\FosunAI-Trading

# 1) 引导（创建 venv、装依赖、生成 .env、跑测试）
powershell -ExecutionPolicy Bypass -File scripts\bootstrap_windows.ps1

# 2) 看环境与门禁状态
.venv\Scripts\python.exe -m ai_trader.cli check-env

# 3) 离线全链路演示（沙盒，不碰主账户）
.venv\Scripts\python.exe -m ai_trader.cli demo

# 4) 启动管理台
powershell -ExecutionPolicy Bypass -File scripts\run_web.ps1
#    → http://127.0.0.1:8781
```

### 常用命令

| 命令 | 作用 |
|---|---|
| `ai-trader status` | 打印完整运行状态（JSON） |
| `ai-trader check-env` | 门禁 / 凭证 / DPAPI / SDK 可用性 |
| `ai-trader scan [--symbols hk00700,usAAPL]` | 执行一次扫描，走完 AI→风控→下单 |
| `ai-trader monitor` | 持仓监控：挂单复核 + 止损/止盈 |
| `ai-trader kill --reason "..."` | 触发紧急停止（写入 `data/HALT.json`，重启后仍生效） |
| `ai-trader resume --confirm RESUME-TRADING` | 解除紧急停止（必须逐字确认，否则 exit 2） |
| `ai-trader cancel --order-id X` | 撤单（走完整链路：查单→门禁→撤单→回写） |
| `ai-trader demo` | 沙盒全链路演示（可 `--keep` 保留现场） |
| `ai-trader sdk-audit` | 反射列出官方 SDK 全部接口签名 |
| `ai-trader vault set/get/list/export/import` | 密钥保险箱 |
| `ai-trader web` | 启动管理台 |

---

## 5. 安全模型（三层）

### 5.1 三重开关

```yaml
safety:
  live_trading: false            # 是否允许建立真实券商会话
  order_execution_enabled: false # 是否允许真实下单/撤单
  paper_trading: true            # 使用本地模拟撮合
```

真实下单要求**同时**满足：

1. `PAPER_TRADING=false`
2. `LIVE_TRADING=true`
3. `ORDER_EXECUTION_ENABLED=true`
4. `LIVE_ACK_PHRASE` 精确等于 `I UNDERSTAND REAL MONEY IS AT RISK`
5. `KILL_SWITCH=false`
6. 风控模块逐项放行

任一条不满足，`SafetyGate.can_send_real_order()` 返回 `False` 并给出中文原因。若 `paper_trading` 与 `live_trading` 同时为真，**强制回落到模拟盘**（取更安全的一方）。

### 5.1.1 急停是持久化的

`ai-trader kill` 会写 `data/HALT.json`。**进程崩溃 / 计划任务重启 / 机器重启后仍然停止** ——
启动时 `build_gate()` 直接读哨兵覆盖配置，不依赖任何调用方记得去查。

解除必须 `ai-trader resume --confirm RESUME-TRADING`（逐字）；哨兵损坏时按 `storage_error` 处理并**继续停止**；
哨兵写不进去时本进程立即全拒（`halt_store_failed`）。

> 环境变量可以覆盖开关，但**无法跳过确认短语** —— 这是刻意设计，防止 `.env` 被误改一行就上线真钱。

### 5.1.2 AI 决策权威（谁有权开仓）

系统是「单 AI 自动交易」，**规则引擎不能代替 AI 做开仓决策**。

| 模型状态 | 能否开仓 | 拒绝码 |
|---|---|---|
| 真正的 LLM 正常响应并通过校验 | ✅ | — |
| 规则引擎 `HeuristicModel` | ❌ | `AI_NOT_DECISION_AUTHORITY` |
| LLM 无 Key / 构建失败 | ❌ | `AI_UNAVAILABLE` |
| 连续失败进入冷却期 | ❌ | `AI_DEGRADED` |
| 信号超过 `signal_ttl_seconds` | ❌ | `STALE_SIGNAL` |

关键点：

* **已有持仓不受影响** —— 止损/止盈由 `monitor()` 的独立风控驱动，不经过 AI 闸门
* **恢复后不补发旧信号** —— 引擎从不缓存信号，且信号有 TTL
* **每次拒绝都写审计** `entry_blocked`（含 code / reason / symbol / model）

查看当前状态：

```bash
.venv/Scripts/python.exe -m ai_trader.cli check-env   # 关注 ai.entry_allowed / ai.code
```

### 5.1.3 交易规则与行情闸门（B4 / B5）

| 能力 | 说明 |
|---|---|
| A 股通规则 | T+1、分板块涨跌幅、最小单位、停牌、交易日历；全部配置化并带来源/生效日期 |
| 规则不明 | `UNKNOWN` 一律拒绝；`UNVERIFIED` 由 `allow_unverified_rules` 决定（默认拒绝） |
| 实时推送 | 重连 / 心跳超时 / 去重 / 乱序 / 时间戳校验 / 重连后重订阅 |
| 行情中断 | 只**暂停新开仓**，已有持仓的止损照常执行 |

⚠️ **A 股默认不可交易**：节假日表状态为 `UNKNOWN`（离线无法核实），
需按交易所公告录入 `market_rules.calendar.holidays.cn` 后才可交易。
详见 [B4_MARKET_RULES_REPORT.md](B4_MARKET_RULES_REPORT.md) 与 [B5_MARKET_PUSH_REPORT.md](B5_MARKET_PUSH_REPORT.md)。

### 5.2 风控不可绕过

- `StrategyEngine._execute()` 是**唯一**调用 `broker.place_order()` 的地方，首行即 `if not decision.approved: return None`。
- Web 层**没有**任何"直接下单"接口（`tests/test_web.py::test_no_bypass_order_endpoint_exists` 断言了这一点）。
- 风控内部异常 → 按**拒绝**处理（fail-closed），由 `test_fail_closed_on_internal_error` 覆盖。

### 5.3 密钥

- 默认存 Windows **DPAPI** 密文（绑定当前用户+机器），`data/vault.json` 里没有明文。
- 只在启动进程内注入 `os.environ`，**从不修改系统环境变量**。
- 日志/审计/Web 输出统一经过 `redact()`，PEM 只输出 `<pem len=... sha8=...>` 指纹。
- 跨机迁移用 `scrypt + AES-256-GCM` 口令包，导出文件里同样没有明文。

---

## 6. 默认配置下的行为

| 项 | 值 | 含义 |
|---|---|---|
| 模式 | `paper` | 本地模拟撮合 |
| 行情 | **合成数据** | `SyntheticQuoteSource`，由 `symbol+日期` 确定性生成，**不是真实行情** |
| AI | `heuristic`（规则引擎，非 LLM） | 没有 AI API Key 时的诚实基线 |
| 单笔风险 | 权益 1% | 由入场价与止损价距离反推股数 |
| 单标的上限 | 15% | 超过则拒绝 |
| 单日亏损 | 3% | 触发后当日禁止新开仓 |
| 单笔委托上限 | 50,000 | 账户本币 |
| 重复单冷却 | 300 秒 | 同标的同方向，**数据库唯一约束，跨进程生效** |
| 订单超时 | 600 秒 | 未成交自动撤单并置 `expired` |
| 订单状态未知 | 封锁标的 | 未对账前该标的禁止下单 |

改这些值请编辑 `config\config.yaml` 的 `risk:` 段，并在改完后跑一次 `pytest`。

---

## 7. 测试

```powershell
.venv\Scripts\python.exe -m pytest -q
```

**结果（真实执行）**：`tests=309, failures=0, errors=0, skipped=1`，耗时 31.5s。
唯一跳过项是 `test_llm_real_call_if_key_present` —— 因为没有 `AI_API_KEY`，LLM 真实调用**未测试**（这是如实标记，不是通过）。

测试文件与需求的对应关系见 [REPORT.md §4](REPORT.md)。

---

## 8. 相关文档

| 文档 | 内容 |
|---|---|
| [REPORT.md](REPORT.md) | 准备工程报告：完成度、测试证据、最终状态判定、待办 |
| [API_STATUS.md](API_STATUS.md) | SDK 审计结果、接口清单、已验证/未验证的边界 |
| [RISK_CONTROL.md](RISK_CONTROL.md) | 风控规格书：24 项检查、阈值、fail-closed 论证 |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Windows VPS 部署、计划任务、备份、迁移、加固 |

---

## 9. 免责与边界

- 本项目是**工程脚手架**，不是投资建议，也不保证策略盈利。
- 合成行情仅用于打通链路，任何基于它的收益数字都没有参考价值。
- 真实交易前必须自行完成：开放平台权限申请、合规审查、小资金灰度、至少一个完整交易日的只读观察。

---

## 10. 许可证

本项目以 **Apache License 2.0** 发布，全文见 [LICENSE](LICENSE)。

- 允许商业使用、修改、分发，需保留版权与许可声明；
- 若修改后分发，需说明修改内容；
- 附带专利授权；
- **不提供任何担保**，尤其不担保本项目的策略能盈利。

第三方依赖仍归各自作者所有（见 `requirements.txt` 与 `pyproject.toml`）。
券商 OpenAPI 的 Python SDK 不在本仓库内，需自行从官方渠道获取。
