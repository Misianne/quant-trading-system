# FosunAI-Trading：AI 模拟交易闭环专项 — 交付报告

| 项 | 值 |
|---|---|
| 日期 | 2026-11-10 |
| 范围 | 美股模拟交易闭环（真实历史行情 + 真实 AI 调用通道 + 独立风控 + Paper Broker + 账本 + 重启恢复） |
| 结论 | **PARTIAL — 1 项必要条件 BLOCKED**：AI 真实推理（无凭据）。其余链路已用真实行情跑通并留证 |
| 实盘状态 | 永久关闭，未开启，未申请开启 |
| 生产隔离 | 未访问 VPS、未连星财富 API、未建真实 Session、未读真实密钥 |

---

## 1. 一句话结论

行情端和决策端原本是**空的**（合成随机游走 + 规则引擎），本次把行情换成了**真实 Yahoo 日线**（500 根 × 3 标的，落 SQLite），把决策端接上**真实 OpenAI 兼容 HTTP 通道并补齐结构化审计**，并复用既有风控/撮合/账本把"行情 → 决策 → 风控 → 模拟成交 → 记账 → 重启恢复"整条链跑通。

**但 `AI_API_KEY` 为空、vault 无该键**，因此"AI 确实调用模型"这一条**无法达成**，按专项要求标记 **BLOCKED**，未用规则引擎或预设 JSON 冒充。

---

## 2. 交付标准逐条对照（专项 §7）

| # | 要求 | 状态 | 位置 |
|---|---|---|---|
| 1 | 实际修改文件清单 | ✅ **0 个既有文件被修改** | §3 |
| 2 | 新增文件清单 | ✅ 9 个 | §3 |
| 3 | 修改前后 SHA256 | ✅ 受保护文件指纹与基线一致；新增文件逐个列出 | §3 |
| 4 | 完整 AI 调用链 | ✅ 逐行 | §4 |
| 5 | 实际模型调用证据 | ❌ **BLOCKED**（无凭据）；仅提供传输层替身的调用证据 | §5 |
| 6 | 行情数据来源 | ✅ Yahoo chart 公开端点，500 根/标的，含质量报告 | §6 |
| 7 | 模拟订单执行证据 | ✅ 两笔真实撮合（买/卖），含滑点、手续费、盈亏 | §7 |
| 8 | 交易账本及重启恢复证据 | ✅ orders/equity/intents + 重启一致性 | §8 |
| 9 | 测试报告 | ✅ 新增 24 项全绿；全量 611 项（610 通过 / 1 跳过） | §9 |
| 10 | 未解决问题 | ✅ 3 项（1 阻断 + 2 缺陷） | §10 |
| 11 | 启动和运行命令 | ✅ | §11 |

---

## 3. 文件清单与指纹

### 3.1 修改的既有文件：**无**

```
git 不存在（仓库无 .git），改用指纹比对：
log_redaction.py   720b4f33ac98f7f09a95fddc1ca5244f3f17b577f77f38aa24825874f7556b4a  ← 与 Batch 4.1.4 基线一致
config.py          ddb8144c37b1d23806c1a8bc44753eade1f64f5b1927fdfdf1c7fd4ba0efe8e0  ← 同上
runtime.py         4fe3f75724f61c959430ac72b13d6e94ffa03e6993ef55a5746d526e15233524  ← 未触碰
fosun_adapter.py   1171d42c244d58ef1788c1af82bcaab12781c15e66a0069fdef5446fb73eee26  ← 未触碰
```

`.env`、`config/config.yaml`、`sdk_src/`、`data/audit.db` 结构、`broker/paper.py`、`risk/controller.py`、`strategy/engine.py`、`market/rules.py` **均未修改**（时间戳未变）。

### 3.2 新增文件

| 文件 | 行数 | SHA256 | 作用 |
|---|---|---|---|
| `src/ai_trader/us_loop/__init__.py` | 29 | `4dfb4dde077350c2f42e979aa4c91189ddf95baad88cf6fa8dc6ecce804f3237` | 包导出 |
| `src/ai_trader/us_loop/bars.py` | 338 | `450e5503ee16a96f32bdb75bb92cbe9e54d6678f7a5f0e3260051a41f9aa1412` | 真实日线存储 + 质量校验（拒绝脏数据、禁无来源写入） |
| `src/ai_trader/us_loop/fetch.py` | 165 | `1769e902f926f7ae5f997778d33a7af3a08108ca9cd62c84dab661e2308493da` | Yahoo 公开端点抓取（无密钥） |
| `src/ai_trader/us_loop/quote_source.py` | 123 | `b1293803662f36601f32e2ff92c433560085fb065bd37d9df6f3b96bb65cc208` | 历史行情 → `QuoteSource`（防未来函数） |
| `src/ai_trader/us_loop/decisions.py` | 446 | `b65a8b15535bdd306e3d696e8e8aa3abf2c1b89e5b32c2d7adf44948ae59ec00` | 真实 LLM 调用 + 结构化决策落库 |
| `src/ai_trader/us_loop/runner.py` | 432 | `a63c287b5a977fbe7901a9f026721d51ebc4fbb645c74080fdcc7a7c0f7210ad` | 闭环装配与运行器 |
| `src/ai_trader/us_loop/__main__.py` | 176 | `b7319b5d37e89dee0e13b38ddb5ca585d01e2e0646317f3d057c0d9cb2566dee` | 命令行入口 |
| `tests/test_us_loop_b42.py` | 470 | `7a979e44901c8d3f69b055965f0066a4f5d7aa5c3f9e6bf22ee344dbe71dd38c` | 24 个闭环测试 |
| `audit_evidence/us_loop_demo_run.py` | 192 | `6cf3cfbe7b072f480c204587099d99700fdf22f11a11eba80bb0aba658b298c5` | 证据生成脚本 |

运行产物（数据，非代码）：`data/market_us.db`（真实日线）、`data/us_loop.db`（决策/运行记录）、`audit_evidence/us_loop/`（证据包）。

---

## 4. 完整 AI 调用链（代码级）

```
cli: python -m ai_trader.us_loop run --trade-date 2026-11-06
  └─ us_loop/runner.py:PaperLoop.__init__
       ├─ BarStore(data/market_us.db)                      ← 真实日线
       ├─ HistoricalQuoteSource(as_of = trade_date-1)      ← 防未来函数
       ├─ PaperBroker(quote_source=…)                      ← 复用 broker/paper.py:169
       ├─ AuditJournal(data/audit.db)                      ← 复用 audit/journal.py:192
       ├─ RiskController(cfg, cfg.gate)                    ← 复用 risk/controller.py:237
       ├─ build_llm(cfg)  → OpenAICompatibleModel | UnavailableModel   ← 复用 ai/providers.py:225
       ├─ AIGateway(model, DecisionLedger)                 ← 新增：决策落库
       └─ AuditedModel(model, gateway) → StrategyEngine    ← 引擎照旧调 model.analyze
  └─ us_loop/runner.py:PaperLoop.run(now=交易日 10:30 ET)
       ├─ engine.scan(symbols, now)                        ← 复用 strategy/engine.py:1159
       │    ├─ _build_context      engine.py:532   （quote + 120 根 K 线 + 指标 + 持仓/现金）
       │    ├─ _prefilter          engine.py:557   （K 线 <30 / ATR 过低 → skipped）
       │    ├─ authority.can_decide_entry engine.py:604 （AI 不可用 → blocked，不产生任何单）
       │    ├─ AuditedModel.analyze → model.analyze(ctx)   （真实 HTTP POST /chat/completions）
       │    │     └─ AIGateway.record_signal → data/us_loop.db:ai_decisions
       │    ├─ check_freshness     engine.py:630   （信号 TTL 300s，用运行时钟）
       │    ├─ signal.validate     engine.py:642   （置信度/止损方向/仓位上限）
       │    ├─ risk.size_position  engine.py:652
       │    ├─ risk.evaluate       engine.py:678   （30+ 项独立检查）
       │    └─ _execute            engine.py:913   （预写意图 → place_order → 结算 → 落库）
       ├─ journal.record_equity("USD", …)                  ← equity_snapshots
       └─ ledger.finish_run(…)                             ← loop_runs
```

**关键点：AI 不持有下单权。** 引擎在 `engine.py:604` 与 `:678` 两处强制闸门，`_execute:915-921` 还有一道"未获批意图绝不发单"的代码级兜底。

---

## 5. AI 调用证据：**BLOCKED**

### 5.1 阻断事实

| 检查 | 结果 |
|---|---|
| `.env` 是否含 `AI_API_KEY` | 键存在、**值为空**（`present=False len=0`） |
| 密钥保险箱 | `state=absent`，`get("AI_API_KEY")` → `None` |
| 本地模型服务 | 127.0.0.1 端口 11434/1234/8000/8080/3000/5000 等**全部未监听** |
| 配置的 provider | `ai.provider = heuristic`（规则引擎），`runtime.build_ai` 判定 `can_trade=False` |

### 5.2 真实调用的代码证据（已就绪，只差凭据）

```python
# ai/providers.py:272
resp = requests.post(url, json=body, headers=headers, timeout=(connect_to, read_to))
# body = {model, messages, temperature, max_tokens, response_format={"type":"json_object"}}
```

只要 `AI_API_KEY` 非空，`build_llm`（`us_loop/decisions.py:270-300`）就会构造
`OpenAICompatibleModel` 并走同一条链路，无需改任何代码。

### 5.3 本次获得的"调用"证据是什么、不是什么

- **是**：本机 `127.0.0.1` 上的 OpenAI 兼容传输层替身（测试替身），验证了 HTTP 客户端、JSON 解析、信号校验、引擎集成、决策落库。
- **不是**：AI 推理。所有这类记录都标 `mode=stub-transport`，`provenance` 仅表示"走的是 LLM 通道"，**不计入"AI 真实性"验收**。

---

## 6. 行情数据来源与质量

| 项 | 值 |
|---|---|
| 来源 | `https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=2y&interval=1d`（公开端点，无需密钥） |
| 落库 | `data/market_us.db`，表 `us_bars`，主键 `(symbol, trade_date)`，带 `source / fetched_at / digest` |
| 覆盖 | usaapl / usnvda / ustsla 各 **500 根**，2024-11-11 → 2026-11-09 |
| 时区口径 | epoch → `America/New_York` 日历日（避免 UTC 把周五记成周六） |
| 校验 | 价格 > 0 且在 [1e-4, 1e6]、`high ≥ max(o,c)`、`low ≤ min(o,c)`、`volume ≥ 0`、日期可解析 |
| 脏数据 | 本次 3×500 行 **0 拒绝**；单测另有 5 类脏数据全部被拒（`test_bars_validation_rejects_dirty_rows`） |
| 重复数据 | 幂等：同 `(symbol,date)` 同 digest 不重写（`test_bars_are_idempotent`） |
| 缺口 | 每标的 21 个工作日缺口（美国市场节假日，属正常）；逐日列出见证据包 `market_data.symbols.*.gap_examples` |
| 拆股 | 用 `|日收益| ≥ 35%` 探测疑似拆股/异常跳空，**三只标的均为 0 次**；未做复权，已在模块文档声明 |
| 未来函数 | `load/candles` 强制 `trade_date <= as_of`；决策日 D 只用 ≤ D-1 的 bar（`BAR_LAG_DAYS=1`） |

---

## 7. 模拟订单执行证据

真实行情（AAPL 2026-11-05 收盘 344.17）下的完整两笔撮合：

| 字段 | 买入 | 卖出 |
|---|---|---|
| 委托价 | 344.27（限价） | 344.07（限价） |
| 数量 | 12 | 12 |
| 状态 | filled | filled |
| 成交价（含 5bps 滑点） | 344.342098 | 343.997928 |
| 手续费（max(8bps, 3)） | 3.3057 | 3.3024 |
| 账户影响 | 现金 130,000 → 125,864.59 | 现金 → 129,989.26 |
| 持仓 | 12 股 @ 344.06 | 0 |

往返成本 10.74 USD（滑点 4.13 + 手续费 6.61），与 18bps 的配置吻合。

前置闸门实际生效记录（同一轮）：`usnvda` 因 `hold` 未下单；早期演示中另一标的一次买入被
`max_price_deviation` 拒单（"委托价偏离最新价 89.52%，疑似错价"），证明错价保护在工作。

---

## 8. 账本与重启恢复证据

| 证据 | 位置 | 结果 |
|---|---|---|
| 决策记录 | `data/us_loop.db: ai_decisions` | 含 `decision_id / decided_at / action / confidence / target_qty / target_pct / rationale / risk_conditions / input_version / prompt_digest / provider / model_id / latency_ms / usage / provenance` |
| 运行记录 | `data/us_loop.db: loop_runs` | 每轮 `started_at / finished_at / status / summary` |
| 信号/风控/订单/权益 | `data/audit.db` | `signals`、`risk_decisions`、`orders`、`equity_snapshots`、`order_intents` 全部落库 |
| 账户状态 | `data/paper_state.json` | 现金、持仓、订单、已实现盈亏、日内基准 |
| 重启一致性 | `restart_proof()` | 新建一套实例读同一批文件：`account/positions/orders/journal(除 audit_log)/ledger` **逐项一致**（`identical=true`） |

演练日两轮扫描后 `restart_identical=true`，`equity_snapshots=2`，`orders=2`，账户权益 129,989.26。

---

## 9. 测试报告

### 9.1 新增测试（`tests/test_us_loop_b42.py`，24 项，全绿）

专项 §4 的 17 个必测场景覆盖情况：

| # | 场景 | 用例 | 覆盖 |
|---|---|---|---|
| 1 | AI 买入决策 | `test_ai_buy_decision_fills_and_persists` | ✅ |
| 2 | AI 卖出决策 | `test_ai_sell_decision_closes_position` | ✅ |
| 3 | AI 持有决策 | `test_ai_hold_creates_no_order` | ✅ |
| 4 | 风控拒绝 | `test_risk_rejects_second_entry_same_symbol` | ✅ |
| 5 | 现金不足 | `test_insufficient_cash_never_orders` | ✅ |
| 6 | 超出持仓限制 | 同 4（`max_position_pct` + 重复单拦截） | ✅ 部分（未单列"占比超限"独立断言） |
| 7 | 重复订单 | `test_duplicate_order_blocked` | ✅ |
| 8 | 行情过期 | `test_missing_market_data_fails_closed` | ⚠️ 部分：覆盖"无数据即拒绝"，未覆盖"实时源断流后过期"（无实时源） |
| 9 | AI 超时 | `test_ai_timeout_is_blocked` | ✅ |
| 10 | AI 输出格式错误 | `test_ai_bad_json_is_blocked_not_guessed` | ✅ |
| 11 | 模拟成交 | `test_exec_layer_buy_fills` | ✅ |
| 12 | 部分成交 | `test_partial_fill_gap_is_explicit` | ❌ **未实现**（F-USL-02），测试显式锁定缺口 |
| 13 | 模拟拒单 | `test_zero_qty_intent_is_rejected`、`test_unfilled_limit_order_stays_submitted` | ✅ |
| 14 | 重启恢复 | `test_restart_recovery` | ✅ |
| 15 | 重复运行幂等 | `test_idempotent_rerun` | ✅ |
| 16 | 模拟/真实隔离 | `test_paper_broker_isolation` | ✅ |
| 17 | 全局停止开关 | `test_kill_switch_blocks_scan` | ✅ |

另含行情层 5 项：脏数据拒绝、无来源拒绝、幂等、防未来函数、来源可追溯。

### 9.2 全量回归

```
python -m pytest -q
JUnit: tests=611 failures=0 errors=0 skipped=1   （74.5s，退出码 0）
```

新增模块未触碰任何既有文件，既有 589 项全部保持通过。

---

## 10. 未解决问题（3 项）

| ID | 问题 | 级别 | 影响 |
|---|---|---|---|
| **F-USL-01** | `AI_API_KEY` 为空、vault 无该键、本机无本地模型 | **BLOCKED** | "AI 确实调用模型"无法验收；引擎会正确阻断开仓，不会误下单 |
| **F-USL-02** | Paper Broker 无部分成交：`allow_partial_fill` 只保存不使用（`paper.py:181,189`） | 中 | 专项 §3.4 的"部分成交"未实现 |
| **F-USL-03** | 规则层无条件扣减当日买入（`market/rules.py:651`），美股 T+0 当日回转被误拒 | 中 | 当日"买入→卖出"会被 `INSUFFICIENT_SELLABLE` 拒绝；跨日不受影响 |

> F-USL-03 属 US-P1 市场规则面，本专项未修改（越权风险），仅在运行器中规避并在
> `US_LOOP_PHASE1_ARCH_REVIEW.md:§4` 登记，建议由后续批次单独修复并配回归测试。

### 10.1 策略评估（专项 §5）— 未做，原因明确

单标的两笔往返不足以评估胜率/盈亏比/最大回撤/夏普。且：
- 无真实 AI 决策 ⇒ 无法评估"AI 策略"的有效性；
- 样本仅 500 根日线 × 3 标的、单一时间窗 ⇒ 存在明显过拟合与幸存者偏差风险；
- 因此**不宣称任何策略有效**，只交付可评估的管线与指标（`equity_snapshots` 已可按日重建净值曲线）。

---

## 11. 启动与运行命令

```bash
cd C:/FosunAI-Trading
export PYTHONPATH=src          # Git Bash；PowerShell 用 $env:PYTHONPATH="src"

# 1) 抓真实日线（幂等，可重复跑；不需要任何密钥）
.venv/Scripts/python.exe -m ai_trader.us_loop fetch --symbols usAAPL,usNVDA,usTSLA --range 2y

# 2) 看状态（含就绪度自检、账户、账本计数）
.venv/Scripts/python.exe -m ai_trader.us_loop status --trade-date 2026-11-06

# 3) 跑一轮闭环（AI 可用时会产生真实决策；当前会如实报 AI_UNAVAILABLE）
.venv/Scripts/python.exe -m ai_trader.us_loop run --trade-date 2026-11-06

# 4) 导出证据包（含重启一致性校验）
.venv/Scripts/python.exe -m ai_trader.us_loop evidence --trade-date 2026-11-06

# 5) 隔离演练（全部状态放沙盒，不碰项目账本）
.venv/Scripts/python.exe -m ai_trader.us_loop run --trade-date 2026-11-06 --sandbox /tmp/us_loop_sbx

# 6) 测试
.venv/Scripts/python.exe -m pytest tests/test_us_loop_b42.py -q
```

---

## 12. 副作用声明（必须知悉）

调试期间（沙盒账本隔离修好之前）有一笔测试单写进了**项目模拟账本**：

```
data/audit.db orders: order_id=P49c17b55f4e941, usaapl, buy 34, filled, 2026-11-10T17:38:24
data/audit.db order_intents: 1 条对应意图
data/audit.db signals/risk_decisions/equity_snapshots: 各新增 1~4 条
```

- 该笔**未**影响 `data/paper_state.json`（现金与持仓未变），因此账本与账户存在一笔不一致记录；
- 未删除任何行：`audit_log` 有哈希链，删行会破坏链完整性；
- 影响范围：`today_bought` 会在次日 0 点自然归零；如需彻底清理，请在备份后按
  `order_id` 定向删除并重建链（建议由后续批次统一处理）。

---

## 13. 结束条件逐条判定（专项 §8）

| 条件 | 判定 |
|---|---|
| AI 确实调用模型 | ❌ **BLOCKED**（无凭据） |
| 使用真实历史美股行情 | ✅ Yahoo 日线 500×3，落 SQLite，可追溯到 `digest` |
| AI 产生真实推理决策 | ❌ BLOCKED（同上） |
| 决策经过独立风控 | ✅ 代码路径强制；测试与演练均落 `risk_decisions` |
| Paper Broker 实际模拟执行 | ✅ 买/卖各 1 笔真实撮合 |
| 现金和持仓正确更新 | ✅ 130,000 → 129,989.26，持仓 12 → 0 |
| 交易记录持久化 | ✅ 4 张表 + 决策库 + 账户 JSON |
| 重启后恢复正确 | ✅ `identical=true` |
| 真实 Broker 完全不可达 | ✅ 只实例化 `PaperBroker`，无 SDK client，`live_trading=false` |
| 全部安全边界测试通过 | ✅ 新增 24 项 + 全量 611 项（1 跳过） |

**总判定：PARTIAL / 1 项 BLOCKED。** 不满足"AI 确实调用模型"，按专项要求**不得**宣称闭环完成，也**不**标记 PASS。

解除 BLOCKED 只需一步：在 `.env` 填入有效的 `AI_API_KEY`（或由用户授权把已配置的合法 Provider 密钥写入 vault），然后执行 §11 第 3 步即可产生真实决策；代码无需改动。

---

> 本文件 SHA256（本行之前全部字节，可用 `head -n -1 <本文件> | sha256sum` 复算）：`a59fe05552f187da634c725a690ff8dada91dcf4d8d20d9d54851ca6da5fce0d`
