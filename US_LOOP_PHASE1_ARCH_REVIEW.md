# FosunAI-Trading：AI 模拟交易闭环专项 — 第一阶段现场架构核验

| 项 | 值 |
|---|---|
| 核验日期 | 2026-11-10 |
| 项目目录 | `C:\FosunAI-Trading` |
| 核验方式 | 只读阅读 + 沿调用链追踪 + 本地探针（未连生产、未建真实 Session） |
| 安全开关 | `LIVE_TRADING=false`、`ORDER_EXECUTION_ENABLED=false`、`PAPER_TRADING=true`（`config/config.yaml: safety` 段，实测读取一致） |
| 结论 | 骨架完整，**决策端与行情端是空的**：AI 不是真模型、行情是合成随机游走 |

> 判定原则：不按类名/注释认定功能已实现，全部沿实际调用链确认执行路径。

---

## 1. 结论速览（对八个必答问题）

| # | 问题 | 结论 | 关键证据 |
|---|---|---|---|
| 1 | 当前 AI 能否真正调用模型并获得推理结果 | **不能** | 真实 HTTP 调用代码存在（`ai/providers.py:252-289`），但配置的 provider 是规则引擎 `heuristic`（`config/config.yaml` → `ai.provider`；`runtime.py:164`），且 `AI_API_KEY` 为空（`.env` 有键无值，vault 不存在）→ `OpenAICompatibleModel.available=False`（`providers.py:245`） |
| 2 | AI 输出是否真正进入策略和风控 | **设计上是，当前跑不通** | `strategy/engine.py:609 model.analyze(ctx)` → `:653 validate` → `:678 risk.evaluate` → `:913 _execute`；但 `:604 authority.can_decide_entry` 对规则引擎/不可用模型一律 `allowed=False`，链路止于开仓闸门 |
| 3 | 行情是否有真实历史数据 | **之前没有** | `broker/paper.py:7-12` 文件头自认"默认行情源是合成数据"；`:105-147` `SyntheticQuoteSource.klines()` 用 `sha256(symbol\|date\|paper-v1)` 做种子生成随机游走；`data/` 下无任何 K 线文件 |
| 4 | 是否已存在 Paper Broker | **是，且完整** | `broker/paper.py:169 PaperBroker`：限价/市价撮合（`:318-390`）、滑点（`:311-313`）、手续费（`:308`）、现金/持仓更新（`:392-419`）、部分成交**未实现**（见 F-USL-02） |
| 5 | 订单状态是否持久化 | **是** | `paper.py:186-205`（JSON 全量落盘 + 启动加载）；账本 `audit/journal.py:54 orders`、`:97 order_attempts`、`:112 order_intents`、`:75 equity_snapshots` |
| 6 | 是否具备重启恢复能力 | **是** | `engine.py:759 _recover_order_intents`（未终结意图提升为 `unknown` 并强制对账）、`journal.py:691 recover_open_intents`、`paper.py:221 _load`；已有测试 `tests/test_process_restart.py` |
| 7 | 哪些模块可直接复用 | 见 §3（Paper Broker / 风控 / 账本 / 时钟日历 / 指标 / 引擎执行段） |
| 8 | 哪些模块缺失 | 真实行情源、真实模型可用性（凭据）、US-only 观察池收敛、部分成交、当日回转（T+0 缺陷，见 F-USL-03） |

---

## 2. 调用链实况（逐段带行号）

### 2.1 入口

```
cli.py:458-491        status / check-env / scan / monitor / guard / kill / resume / cancel / demo / web
runtime.py:186-200    build_runtime → build_journal(:70) / build_broker(:76) / build_ai(:149) / RiskController / StrategyEngine
runtime.py:81-93      PAPER_TRADING=true → PaperBroker（永远不碰真账，即使配了真实凭证）
```

### 2.2 决策链（引擎内部）

| 步骤 | 位置 | 行为 |
|---|---|---|
| 取上下文 | `engine.py:532-555` | `broker.get_quote` + `broker.get_klines(120)` + 指标快照 + 持仓/权益/现金 |
| 波动过滤 | `engine.py:557-569` | K 线 < 30 或 ATR% 过低 → `skipped` |
| 行情闸门 | `engine.py:716-726` | `market_feed.enabled=false` → `feed is None` → 直接放行 |
| **AI 权威闸门** | `engine.py:601-607` | 规则引擎 / 不可用模型 / 故障冷却 → `blocked`，**不产生任何开仓** |
| **模型调用** | `engine.py:609` | `self.model.analyze(ctx)` |
| 信号新鲜度 | `engine.py:630-640` | `authority.check_freshness(signal.ts, now)`，TTL 300s |
| 信号校验 | `engine.py:642-647` | `signal.validate(min_confidence=0.6, require_stop_loss=True)` |
| 定仓 | `engine.py:652-670` | `risk.size_position(...)`，`position_pct` 上限 15% |
| **独立风控** | `engine.py:678-689` | `risk.evaluate(intent, portfolio, last_price, ...)`，落 `risk_decisions` |
| **执行** | `engine.py:913-990` | 预写意图（`journal.prewrite_intent`）→ `broker.place_order` → 结算意图 → 落 `orders` |

### 2.3 风控实际检查项（`risk/controller.py:408-720`）

`intent_valid`、`kill_switch`、`halt_storage`（读写与可用性）、`orders_history`、
`portfolio_integrity`、`trading_permission`、`market_enabled`、`symbol_allowlist`、
`market_hours`、`price_available`、`min_notional`、`max_notional`、`no_naked_short`、
`sell_capacity`、`max_positions`、`max_position_pct`、`max_gross_exposure`、
`per_trade_risk`、`max_stop_distance`、`duplicate_order`（状态型 + 冷却槽位型）、
`max_orders_per_minute`、`max_daily_*`、`price_deviation`、`rule:*`（市场规则）。

风控**未做**现金校验，现金不足由 `PaperBroker.place_order:363-372` 抛 `INSUFFICIENT_CASH` 兜底。
风控异常一律 `approved=False`（`controller.py:395-407`）。

### 2.4 行情与账本落盘

- 报价源协议：`broker/paper.py:57-70`（`quote` / `klines` 两个方法）
- 合成实现：`paper.py:72-163`（`BASE_PRICES` 硬编码 + 随机游走 + `datetime.now()` 生成日期）
- 真实接口（**禁止访问**）：`broker/fosun_adapter.py:1610 get_klines` 需生产 Session
- 账本：`data/audit.db`（`signals` 41 行、`risk_decisions` 9、`orders` 0、`equity_snapshots` 0、`audit_log` 135，核验时为原始状态）
- 模拟账户：`data/paper_state.json`（USD 130,000，无持仓，`transactional` 全量 JSON 重写，无 WAL/原子替换）

---

## 3. 可直接复用 / 需要新建

| 类别 | 模块 | 复用方式 |
|---|---|---|
| 可直接复用 | `PaperBroker`（`paper.py:169`） | 只需注入 `quote_source`，无需改一行 |
| 可直接复用 | `RiskController`（`controller.py:237`） | 引擎已调用；本专项沿用同一实例 |
| 可直接复用 | `AuditJournal`（`journal.py:192`） | 信号/风控/订单/权益全部走它 |
| 可直接复用 | `ExchangeClock` + `TradingCalendar`（`market/clock.py`、`rules.py:238`） | US 日历 `status=VERIFIED`，覆盖 2026-01-01~12-31 |
| 可直接复用 | `strategy/indicators.py` | 指标快照 `compute_snapshot` / `snapshot_for_prompt` |
| 可直接复用 | `OpenAICompatibleModel`（`providers.py:225`） | 真实 HTTP 客户端 + JSON 解析 + 信号构造 |
| 必须新建 | 真实历史行情存储 + 行情源实现 | 本次新建 `us_loop/bars.py`、`fetch.py`、`quote_source.py` |
| 必须新建 | 结构化决策记录（input_version 等） | 本次新建 `us_loop/decisions.py` |
| 必须新建 | 闭环运行器 | 本次新建 `us_loop/runner.py` + `__main__.py` |
| 缺失能力 | Paper Broker 部分成交 | `allow_partial_fill` 只被保存、从未使用（F-USL-02） |
| 缺失能力 | T+0 当日回转 | 规则层无条件扣减当日买入（F-USL-03） |
| 缺失条件 | 真实 AI 凭据 | `AI_API_KEY` 为空（F-USL-01，**BLOCKED**） |

---

## 4. 现场发现的缺陷（本次不修，只登记）

### F-USL-01 AI 凭据缺失（BLOCKED，阻断"AI 真实推理"验收）

- `.env` 中 `AI_API_KEY` 存在但为空；`AI_BASE_URL=https://api.deepseek.com/v1`、`AI_MODEL=deepseek-chat` 已配置。
- `secrets.default_vault().diagnostics()` → `state=absent`，vault 中无该键。
- `config/config.yaml` 的 `ai.provider = heuristic`（规则引擎），而 `runtime.build_ai:171-177` 对规则引擎返回 `can_trade=False`。
- 结论：**当前不存在可用的真实模型**。按专项要求，不得用规则引擎或预设 JSON 冒充 AI，故该验收项标记 BLOCKED。

### F-USL-02 Paper Broker 无部分成交

- `paper.py:181`（参数）、`paper.py:189`（赋值）之外，`allow_partial_fill` **无任何读取**。
- `place_order:335-352` 只有 `filled` / `submitted` 两态；`filled_qty` 恒等于 `qty` 或 0。
- 影响：专项 §3.4 "部分成交" 未实现（已用 `tests/test_us_loop_b42.py::test_partial_fill_gap_is_explicit` 锁定缺口）。

### F-USL-03 规则层把"当日买入"无条件从可卖数量扣掉（T+0 市场被误拦）

```python
# src/ai_trader/market/rules.py:651-656
sellable = max(0.0, float(position_qty) - float(today_bought_qty))
if q > sellable + 1e-9:
    v.append(RuleViolation(CODE_SELLABLE,
        f"可卖数量不足：持仓 {position_qty:g} − 当日买入 {today_bought_qty:g}"
        f"（T+{rule.t_plus}）= {sellable:g} < 卖出 {q:g}", rule.board))
```

- `rule.t_plus` 只用于文案；真正的 T+1 判断在 `:666` 且带 `rule.t_plus >= 1` 守卫，**永远走不到**。
- 实测（美股 `t_plus=0`）：当日买入 10 股后立刻卖出 10 股 → `INSUFFICIENT_SELLABLE … （T+0）= 0 < 卖出 10`。
- 影响：AI 当日"买入→卖出"回转在美股上被误拒；回放多日时若账本时钟与回放时钟不同源，也会误拦（`build_portfolio` 的 `today_bought` 用当日 0 点切分，见 `engine.py:209-223`）。
- 本次**未修改**（属 US-P1 市场规则面，超出本专项授权），仅在运行器中规避并在报告登记。

---

## 5. 核验边界

- 未访问 VPS `203.0.113.10`，未连接星财富生产 API，未创建真实 Session，未读真实密钥内容（仅判断是否存在）；
- 未修改 `.env`、`config/config.yaml`、SDK 源码、Batch 4.1.x 安全修复文件；
- 核验阶段只读；后续实施仅新增文件（清单见交付报告）。

---

> 本文件 SHA256（本行之前全部字节，可用 `head -n -1 <本文件> | sha256sum` 复算）：`89be7c46338655be0012bcd2b33c80607f0663fdffac6df72da1e975171d3b0a`
