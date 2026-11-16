# INDEPENDENT_AUDIT.md —— 独立审计报告

**被审对象**：`C:\FosunAI-Trading`（星财富单 AI 自动交易系统 · 准备工程）
**审计时间**：2026-11-08 12:10 – 12:20 (+08:00)
**审计性质**：**只读**。未修改任何产品代码，未向 `src/` `tests/` `scripts/` `config/` `sdk_src/` 写入任何字节，未连接真实券商，未启用实盘。
**新增文件**：仅 `INDEPENDENT_AUDIT.md` 与 `audit_evidence/`（探针脚本与原始输出，属审计工件，不是产品代码）。

> **2026-11-08 19:00 状态更新**：项目方向调整为**仅美股**。
> §8 第二轮（P0）、§9 第三轮（P1 收尾）、§10 第四轮（B4/B5）、**§11 第五轮（美股方向调整 + 美股缺口）**。
> 细节见 `FIX_REPORT.md`、`NEXT_PHASE_REPORT.md`、`B4_MARKET_RULES_REPORT.md`、
> `B5_MARKET_PUSH_REPORT.md`、`B4_B5_FINAL_REVIEW.md`、`US_MARKET_RULES_REPORT.md`、
> `US_BROKER_CAPABILITY_REPORT.md`。
> 本报告正文保持审计当时的原始结论不变（作为对照基线）。

---

## 0. 审计声明与方法

### 0.1 只读约束如何被遵守

| 约束 | 遵守方式 | 可核查的证据 |
|---|---|---|
| 不修改代码 | 所有探针脚本放在 `audit_evidence/`；运行时仅做**内存 monkeypatch**，不落盘 | 产品文件 mtime 早于 12:10（见 §6.2） |
| 不连接真实账户 | 无凭证；探针用 `127.0.0.1` 回环 HTTP 服务替代网关 | `check_env_after_audit.json` → `configured=false` |
| 不启用实盘 | 未改任何开关；探针用**内存构造**的 live 配置，仅验证代码分支 | §1.3 三项开关仍为 `false/false/true` |
| 不伪造凭证 | 探针用的 P-384 密钥为**进程内临时生成、不落盘、用后即弃**，且仅用于通过构造函数校验 | `find . -name "*.pem"` 结果为空 |

### 0.2 审计手段

1. **重跑测试**并保留逐条结果（`pytest_verbose.txt`）
2. **AST 静态分析**：函数体空实现检测、接口实现覆盖检测
3. **内存变异检验**：注入 6 种错误实现，看测试是否抓得住（证明测试非空转）
4. **回环探针**：本地假 LLM / 假网关，抓取真实 HTTP 报文
5. **桩对象探针**：替换 SDK client，验证异常输入下的行为
6. **端口与 ACL 实测**：`Get-NetTCPConnection`、`icacls`
7. **跨进程行为验证**：构造"重启"场景验证状态持久性

### 0.3 本次没有做的事

- 没有验证真实券商响应字段（无凭证，不可能验证）
- 没有验证真实大模型输出的稳定性（无 Key）
- 没有做渗透测试、没有做压力测试
- 没有审计 `sdk_src/` 的密码学正确性（只审计其**调用方式与副作用**）

---

## 1. 结论摘要

### 1.1 总体判定

| 维度 | 判定 | 一句话 |
|---|---|---|
| 测试真实性 | ✅ **真实** | 145 个用例逐条存在，144 通过 1 跳过；6 组变异全部被抓，无空转测试 |
| 代码真实性 | ✅ **无空壳** | 272 个函数中仅 14 个基类抽象占位；11 个券商接口 100% 双实现 |
| AI 链路可用性 | ✅ **可用** | 回环实测：假 LLM → 真实 HTTP → 解析 → 校验 → 产出 `TradingSignal` |
| 合成/真实行情隔离 | ✅ **隔离** | 真实券商取行情失败时报 `error`，**无任何退回合成数据的路径** |
| 下单入口唯一性 | ✅ **唯一** | 全库仅 1 处 `broker.place_order()`，前置 `decision.approved` |
| 止损/止盈 | ✅ 基本可用 | 双层强制 + 落盘注册表；⚠️ 无止损持仓静默放过（P1-8） |
| 订单超时 | ❌ **缺失** | 挂单永不过期、永不撤销（P1-4/P1-5） |
| 断线重连 | ❌ **缺失** | 无重试/无超时，SDK 本身也未设 HTTP timeout（P1-6） |
| 重复下单 | ⚠️ 部分 | 三层拦截有效，但 L1 内存层跨进程失效（P2-3） |
| SDK 行情加密 | ⚠️ **确认存在** | 实证：行情 POST 被 AES-GCM 加密，与官方 README 矛盾（P1-1） |
| 密钥与脱敏 | ✅ 合格 | 无明文落盘、无日志泄漏；⚠️ 迁移口令走命令行（P1-10）、`.env` ACL 过宽（P1-12） |
| 管理台暴露面 | ✅ 仅回环 | 实测 `127.0.0.1:8781`，无 `0.0.0.0`；⚠️ 无口令时无鉴权（P1-11）、`/api/docs` 免鉴权（P2-1） |

### 1.2 问题计数

| 级别 | 数量 | 含义 |
|---|---|---|
| **P0** | **2** | 阻断真实交易，必须先修，否则不得开关 |
| **P1** | **12** | 真实交易前必须修；模拟盘可接受但需知情 |

> 复评后（§8+§9）：P0 = 0，P1 = **0**（全部关闭），P2 = 8 项未动；
> 另新发现 3 个 P1 级并发/时区缺陷，均已修复（见 §9.2）。
| **P2** | **8** | 工程质量/加固项，可在灰度期并行处理 |

### 1.3 安全状态复核（审计前后一致）

```json
// audit_evidence/check_env_after_audit.json
"safety": {
  "live_trading": false,
  "order_execution_enabled": false,
  "paper_trading": true,
  "kill_switch": false,
  "live_ack_satisfied": false,
  "live_authorized": false,
  "real_order_authorized": false
},
"fosun": { "configured": false, "blocked_reason": "PAPER_TRADING=true，真实券商连接被禁用" },
"vault": { "key_count": 0 }
```

**结论**：三项开关未被本次审计触碰；保险箱为空（无任何凭证）；真实下单资格为 `false`。

---

## 2. 逐项审计结果

### 2.1 【第 1 项】145 项测试是否真实存在

**结论：真实存在，且非空转。**

#### 证据 1：逐条测试名

`audit_evidence/pytest_verbose.txt`（156 行，pytest 原始输出）：

```
collected 145 items
tests/test_ai.py::test_heuristic_model_returns_valid_buy_on_uptrend PASSED
...
tests/test_web.py::test_no_bypass_order_endpoint_exists PASSED
======================= 144 passed, 1 skipped in 4.85s ========================
```

分文件计数（用 `grep -c` 从日志统计，非声明）：

| 文件 | 用例数 |
|---|---|
| `tests/test_ai.py` | 17 |
| `tests/test_audit.py` | 8 |
| `tests/test_cli.py` | 6 |
| `tests/test_engine_flow.py` | 10 |
| `tests/test_market_data.py` | 15 |
| `tests/test_paper_trading.py` | 12 |
| `tests/test_risk_controls.py` | 34 |
| `tests/test_sdk_install.py` | 7 |
| `tests/test_secrets_and_gate.py` | 25 |
| `tests/test_web.py` | 11 |
| **合计** | **145** |

唯一跳过项，理由如实写在输出里：

```
tests/test_ai.py:128: 未配置 AI_API_KEY —— LLM 真实调用【未测试】
```

#### 证据 2：断言密度（AST 统计）

| 文件 | 测试函数 | `assert` 数 |
|---|---|---|
| `test_risk_controls.py` | 34 | 73 |
| `test_secrets_and_gate.py` | 20 | 53 |
| `test_web.py` | 11 | 41 |
| `test_engine_flow.py` | 10 | 35 |
| `test_market_data.py` | 11 | 33 |
| `test_paper_trading.py` | 12 | 33 |
| `test_ai.py` | 11 | 24 |
| `test_audit.py` | 8 | 21 |
| `test_cli.py` | 6 | 11 |
| `test_sdk_install.py` | 7 | 9 |

4 个函数用 `pytest.raises` 上下文做断言（AST 中 `asserts=0` 但 `with=True`），属正常写法，不是空测试。

#### 证据 3：变异检验（关键证据 —— 证明测试与行为耦合）

用 `audit_evidence/mutation_plugin.py` 在**内存中**注入错误实现，不修改任何文件：

| 注入的错误 | 目标测试 | 结果 |
|---|---|---|
| `RiskController` 全部放行 | `test_risk_controls.py` + `test_engine_flow.py` | **19 failed** |
| `SafetyGate` 永远允许真实下单 | `test_secrets_and_gate.py` | **5 failed** |
| `verify_chain` 永远返回"完整" | `test_audit.py` | **2 failed** |
| `_commission` 恒为 0 | `test_paper_trading.py` | **2 failed** |
| `_duplicate_check` 永不拦截 | `test_risk_controls.py` | **2 failed** |
| kill switch 检查被忽略 | `test_risk_controls.py` + `test_engine_flow.py` | **1 failed** |

原始输出见 `audit_evidence/mutation_report.txt`。**6/6 全部被捕获** —— 如果测试是空转的，这些变异一个都不会失败。

复现命令：

```powershell
$env:PYTHONPATH="audit_evidence"; $env:AUDIT_MUTATE="approve_all"
.venv\Scripts\python.exe -m pytest -o addopts="" -q -p mutation_plugin tests/test_risk_controls.py
```

---

### 2.2 【第 2 项】7,234 行代码中是否存在空实现 / 假接口 / 演示逻辑冒充真实功能

**结论：无空壳实现；有 1 处"演示性兜底"已在文档中如实标注，但有 1 处真实的"伪造订单号"（见 P0-1）。**

#### 证据 1：规模核算（`wc -l` 口径，可复算）

| 目录 | 行数 | 文件数 |
|---|---|---|
| `src/` | 5,037 | 23（含 2 个 HTML 模板） |
| `tests/` | 1,575 | 11 |
| `scripts/` | 622 | 6 |
| **合计** | **7,234** | **40** |

`src/` 的 Python 部分：4,632 行，其中空行 622（13.4%）、纯注释 181（3.9%），**有效代码约 3,829 行**。

#### 证据 2：函数体空实现扫描（AST）

```
扫描函数 272 个
空/占位实现:
  src\ai_trader\ai\base.py:153 analyze() 抽象占位(NotImplementedError)
  src\ai_trader\broker\base.py:164..196 get_accounts/get_summary/get_positions/
        get_quote/get_quotes/get_klines/place_order/cancel_order/get_order/
        list_orders/health() 抽象占位(NotImplementedError)
  src\ai_trader\broker\paper.py:72 quote() 抽象占位(NotImplementedError)
  src\ai_trader\broker\paper.py:75 klines() 抽象占位(NotImplementedError)
```

14 个占位**全部**位于抽象基类（`Broker`、`AIModel`、`QuoteSource`），是接口契约，不是空实现。
**零个**具体实现是空的，也没有"仅 `return None`"的方法。

#### 证据 3：接口实现覆盖（反射）

```
Broker 声明 11 个方法
  cancel_order     Paper=自实现   Fosun=自实现
  get_accounts     Paper=自实现   Fosun=自实现
  get_klines       Paper=自实现   Fosun=自实现
  get_order        Paper=自实现   Fosun=自实现
  get_positions    Paper=自实现   Fosun=自实现
  get_quote        Paper=自实现   Fosun=自实现
  get_quotes       Paper=自实现   Fosun=自实现
  get_summary      Paper=自实现   Fosun=自实现
  health           Paper=自实现   Fosun=自实现
  list_orders      Paper=自实现   Fosun=自实现
  place_order      Paper=自实现   Fosun=自实现
```

11/11 双实现，无"继承到的 NotImplementedError"。

#### 证据 4：真实券商适配器是否在"演"

`src/ai_trader/broker/fosun_adapter.py` 全部方法都真实调用 SDK：

| 适配器方法 | 实际调用 | 行号 |
|---|---|---|
| `get_accounts` | `client.account.list_accounts()` | 213 |
| `get_summary` | `client.portfolio.get_assets_summary(...)` | 230 |
| `get_positions` | `client.portfolio.get_holdings(...)` | 251 |
| `get_quote` | `client.market.quote(codes=[...])` | 274 |
| `get_klines` | `client.market.kline(...)` | 310 |
| `place_order` | `client.trade.create_order(**kwargs)` | 363 |
| `cancel_order` | `client.trade.cancel_order(...)` | 380 |
| `list_orders` | `client.trade.list_orders(...)` | 419 |

无一处硬编码返回值。**但**：`place_order` 在响应为空时会编造订单号（P0-1），这是本次审计发现的最严重问题。

#### 证据 5："演示逻辑"是否被如实标注

| 位置 | 内容 | 是否如实标注 |
|---|---|---|
| `broker/paper.py:79` `SyntheticQuoteSource` | 合成行情 | ✅ 类名、docstring、`health().synthetic_prices=true`、Web 顶部无隐藏 |
| `ai/providers.py:47` `HeuristicModel` | 规则引擎，非 LLM | ✅ `is_llm=False`，`health().note="规则引擎，非 LLM"` |
| `risk/controller.py:147` `SESSIONS` | 交易时段近似表 | ✅ 注释与 `RISK_CONTROL.md §9` 明确"近似，未处理假期/DST" |
| `paper.py:5` docstring | 撮合规则 | ✅ 明确"保守，宁可少成交" |

**结论**：没有"用演示逻辑冒充真实功能"的情况。合成行情/规则引擎都被显式暴露为"非真实"，且都只出现在 `PAPER_TRADING=true` 的路径上。

---

### 2.3 【第 3 项】AI 模型调用链：接入大模型后能否真实产出 TradingSignal

**结论：能。已用真实 HTTP 往返验证全链路。**

#### 方法

`audit_evidence/probe_ai_chain.py`：在 `127.0.0.1:8931` 启一个假的 OpenAI 兼容端点，设置 `AI_API_KEY/AI_BASE_URL/AI_MODEL`，走**配置驱动**的 `build_model`（不是 `force_provider`），然后调用 `model.analyze(ctx)`。

#### 证据：6 个用例的真实输出

```
[probe] fake LLM listening on http://127.0.0.1:8931
[probe] model=probe-model available=True status=untested

=== A. 合法买入 ===
  -> action=buy conf=0.78 entry=295.2 sl=283.392 tp=321.768 pos_pct=0.1
  -> valid=True errors=[]
  -> provider=openai_compatible model=probe-model

=== B. 模型输出缺止损 ===
  -> action=buy conf=0.9 entry=295.2 sl=None tp=321.768 pos_pct=0.15
  -> valid=False errors=['缺少 stop_loss（策略强制要求止损）']

=== C. 仓位越界（0.9 > 上限 0.15）===
  -> action=buy conf=0.8 ... pos_pct=0.15        <-- 被截断到 0.15
  -> valid=True

=== D. 带 markdown 围栏 ===
  -> action=sell conf=0.7 entry=295.2 sl=309.96 tp=271.584
  -> valid=True                                    <-- ```json 围栏被正确剥离

=== E. 非 JSON 输出 ===
  -> AIModelError code=NO_JSON: 模型输出不含 JSON: 我觉得应该买入，但不确定。

=== F. HTTP 500 ===
  -> AIModelError code=HTTP_500: HTTP 500: {"error": {"message": "probe-injected failure"}}

=== 请求侧证据 ===
  path=/v1/chat/completions
  auth_header_present=True
  model=probe-model response_format={'type': 'json_object'}
  messages=2 roles=['system', 'user']
  user_prompt_bytes=299  含行情JSON=True
  user_prompt_head='请分析以下标的并给出交易决策。\n```json\n{"symbol": "hk00700", "market": "hk", "currency": "HKD",'
```

#### 链路逐段核实

| 环节 | 位置 | 是否真实 |
|---|---|---|
| 配置 → 模型构建 | `ai/providers.py:351 build_model` | ✅ 读 `provider` / `AI_API_KEY` / `AI_BASE_URL` / `AI_MODEL` |
| 上下文组装 | `strategy/engine.py:153 _build_context` | ✅ 真实取 quote + klines + 持仓 + 权益 |
| 提示词 | `ai/providers.py:16 SYSTEM_PROMPT` + `:35 build_user_prompt` | ✅ 含完整行情 JSON + 硬约束（必须止损、仓位上限、禁止做空/融资） |
| HTTP 调用 | `ai/providers.py:247 _post` | ✅ `requests.post`，60s 超时，Bearer 认证，`response_format=json_object` |
| 输出解析 | `ai/providers.py:198 extract_json` | ✅ 容错围栏/前后夹文本，失败抛 `NO_JSON` |
| 硬约束裁剪 | `ai/providers.py:322-324` | ✅ 仓位超限被截断到 `max_position_pct` |
| 结构校验 | `ai/base.py:105 TradingSignal.validate` | ✅ 缺止损/方向错/置信度不足 → `valid=False` |
| 落入引擎 | `strategy/engine.py:224-236` | ✅ `errs` 非空即 `rejected`，不下单 |

#### 唯一未验证项

真实模型（DeepSeek/GPT）的输出**质量与稳定性**。这需要 Key，属客观阻塞，不是实现缺陷。
`test_llm_real_call_if_key_present` 已就绪，配好 `AI_API_KEY` 后会自动从 skip 变成实跑。

⚠️ 但请注意 **P1-9**：配置了 LLM 却没 Key 时，系统会**静默降级为规则引擎并继续下单**。

---

### 2.4 【第 4 项】合成行情与真实行情的隔离

**结论：隔离干净，合成数据无法进入真实交易路径。**

#### 证据 1：静态引用关系

```bash
$ grep -rn "SyntheticQuoteSource\|synthetic\|quote_source" \
    src/ai_trader/broker/fosun_adapter.py src/ai_trader/strategy/engine.py src/ai_trader/runtime.py
# （无输出 —— 真实适配器与策略引擎完全不认识 synthetic）
```

`SyntheticQuoteSource` 只在 `broker/paper.py` 内部被引用：

| 位置 | 用途 |
|---|---|
| `paper.py:79` | 类定义 |
| `paper.py:177` | `PaperBroker.__init__` 的默认 `quote_source` |
| `paper.py:451` | `refresh_pending` 取价 |
| `paper.py:243/277` | 计算市值/持仓最新价 |

#### 证据 2：券商选择逻辑

`runtime.py:68 build_broker`：

```python
if cfg.gate.state.paper_trading:
    broker = PaperBroker(...)      # ← 只有这里会用合成行情
    return broker, "paper"
fosun = FosunBroker(cfg, vault)    # ← 真实路径，行情来自 SDK
return fosun, "fosun_live" if fosun.configured else "fosun_unconfigured"
```

分支互斥，没有"paper 失败则降级到 live"或反向的路径。

#### 证据 3：动态验证（关键）

用假的 `FailingBroker`（`get_quote`/`get_klines` 抛 `BrokerError`）替换券商，跑真实引擎：

```
=== B) 真实券商取行情失败时，是否会退回合成行情 ===
  outcome: stage=error  detail=取行情失败: 网关不可达（探针注入）
  >>> 是否退回合成行情: False
  >>> 行为正确（报错而非伪造行情）: True
```

`strategy/engine.py:200-205` 的 `except BrokerError` / `except Exception` 都返回 `SymbolOutcome(stage="error")`，**不构造任何替代行情**。

#### 缺陷

⚠️ **合成行情这件事本身没有进入审计记录**（P1-7）：`engine.py:174` 把 `行情源={quote.source}` 写进了 `MarketContext.notes`，但 `notes` 既不在 `to_prompt_dict()` 里，也不在任何 `journal.record_*` 调用中。

实测：

```
>>> signal 中是否有行情来源字段: False
>>> decision checks 中是否提到行情来源: False
>>> MarketContext.notes 内容是否被落库: False
>>> audit_log 中检索 'synthetic': 0
```

后果：事后翻审计库，无法证明某笔成交是基于真实行情还是合成行情。

---

### 2.5 【第 5 项】全部下单入口审计

**结论：下单入口唯一，无绕过风控的路径。**

#### 证据 1：全库 `place_order` 调用点（穷举）

```bash
$ grep -rn "place_order\|create_order\|cancel_order" src/ --include=*.py \
    | grep -v "def place_order\|def cancel_order\|def create_order" | grep -v "broker/base.py"
```

```
src/ai_trader/broker/fosun_adapter.py:363:  resp = client.trade.create_order(**kwargs)   ← 在 FosunBroker.place_order 内部
src/ai_trader/broker/fosun_adapter.py:380:  resp = client.trade.cancel_order(...)        ← 在 FosunBroker.cancel_order 内部
src/ai_trader/strategy/engine.py:308:       result = self.broker.place_order(req)        ← 唯一业务出口
```

**全库仅 1 处**调用券商 `place_order()`。

#### 证据 2：唯一出口的前置守卫

`strategy/engine.py:292-300`：

```python
def _execute(self, intent: OrderIntent, decision) -> Optional[Dict[str, Any]]:
    if not decision.approved:
        # 代码级兜底：没有获批的决策绝不可能到达券商
        logger.error("拒绝执行未获批意图 %s", intent.symbol)
        return None
    if decision.approved_qty <= 0:
        return None
```

配合变异检验 `approve_all` 导致 19 个测试失败，说明守卫有测试覆盖。

#### 证据 3：Web 层无直通接口

`tests/test_web.py::test_no_bypass_order_endpoint_exists` 断言：

```python
paths = {r.path for r in client.app.routes}
assert "/api/order" not in paths
assert "/api/buy" not in paths
assert "/api/sell" not in paths
```

实测路由清单（16 条）：`/login` `/logout` `/` `/api/status` `/api/positions` `/api/orders` `/api/signals` `/api/risk` `/api/audit` `/api/market/{symbol}` `/api/scan` `/api/monitor` `/api/kill` `/api/resume` `/api/paper/reset` + FastAPI 自带 `/api/docs` `/openapi.json`。
其中 `/api/scan` `/api/monitor` 走引擎（必经风控）；`/api/paper/reset` 仅在 `broker_kind=="paper"` 且 `confirm=RESET` 时可执行，不产生委托。

#### 证据 4：风控 fail-closed

`risk/controller.py:248-257`：`evaluate()` 外层 `try/except`，任何异常 → `approved=False`。
对应测试 `test_fail_closed_on_internal_error` 通过；变异 `approve_all` 使该测试失败 → 覆盖有效。

**结论**：无法在本代码库内找到绕过风控的下单路径。

---

### 2.6 【第 6 项】止损、止盈、订单超时、断线重连、重复下单

#### 6.1 止损 / 止盈 —— ✅ 可用，双层强制

| 层 | 位置 | 规则 |
|---|---|---|
| 信号层 | `ai/base.py:105 TradingSignal.validate` | 买必须 `stop_loss < entry < take_profit`；缺止损直接 `valid=False` |
| 风控层 | `risk/controller.py:363-384` | 止损距离 ≤ 12%；`(entry−stop)×qty ≤ 权益×1%`；方向必须正确 |
| 触发层 | `risk/controller.py:534 scan_exits` | `last <= stop` → 全量卖出；`last >= take_profit` → 全量卖出 |
| 持久化 | `strategy/engine.py:111 register_stops` → `data/stops.json` | 重启后仍保护持仓 |

实测（`ai-trader demo` 与 `tests/test_engine_flow.py`）：

```
stop  : [('止损触发：最新 297.831 <= 止损 303.78762', 'filled')]  → positions_after: []
```

⚠️ **缺陷 P1-8**：有持仓但无止损登记时，`monitor()` 静默返回 `[]`，审计日志**一条事件都不写**：

```
持仓数=1  stops 注册表={}
monitor 返回=[]
>>> 无止损持仓是否被静默放过（无告警、无动作）: True
审计日志中的相关事件: []
```

#### 6.2 订单超时 —— ❌ **完全缺失**

```bash
$ grep -rniE "timeout|expire|stale|超时|过期|撤单" \
    src/ai_trader/strategy/engine.py src/ai_trader/risk/controller.py
# （无输出）
```

`paper.py:444 refresh_pending()` 只做"能成交就成交"，**没有"挂太久就撤"**：

```python
for oid, order in list(self._state["orders"].items()):
    if order["status"] != "submitted":
        continue                     # 未成交就跳过，永不过期
    ...
    else:
        continue                     # ← 价格没穿越，继续挂着，无时间上限
```

后果链：
1. 挂单永久占用资金/额度
2. 该标的同向新单被 `duplicate_order`（L2 未完成委托）**永久阻断**
3. `max_daily_orders` 被历史挂单持续占用

→ **P1-4**

#### 6.3 撤单能力 —— ⚠️ 实现存在但**未接线**

```bash
$ grep -rn "cancel_order" src/ tests/ --include=*.py | grep -v "def cancel_order"
src/ai_trader/broker/fosun_adapter.py:380:   resp = client.trade.cancel_order(     ← 适配器内部
tests/test_paper_trading.py:82:              cancelled = paper.cancel_order(...)   ← 测试直接调 paper
```

`StrategyEngine` / Web / CLI **从不调用** `broker.cancel_order()`。
`FosunBroker.cancel_order` 是彻底的死代码（除测试外无调用者）。

→ **P1-5**：这是 6.2（超时撤单）无法实现的前置缺口。

#### 6.4 断线重连 / 重试 —— ❌ **缺失**

```bash
$ grep -rniE "reconnect|retry|backoff|refresh_session|restore_session" src/ --include=*.py
src/ai_trader/broker/base.py:15:     def __init__(..., retryable: bool = False)   ← 只定义了标志
src/ai_trader/broker/base.py:30:     ... code=code, retryable=False
src/ai_trader/broker/fosun_adapter.py:198: ... code="CONNECT_FAILED", retryable=True  ← 设置了却无人读取
src/ai_trader/broker/fosun_adapter.py:277: ... code="EMPTY_QUOTE", retryable=True     ← 同上
```

`retryable` 标志**全库无任何读取点**（死字段）。

SDK 侧唯一的容错是会话自动续期（`auth.py:271-284`，过期前 60s 重建），但：

```bash
$ grep -rn "timeout" sdk_src/fsopenapi/*.py sdk_src/fsopenapi/api/*.py
# （无输出 —— SDK 完全没有设置 HTTP 超时）
```

`client.py:174` 与 `auth.py:179` 的 `requests` 调用均未传 `timeout` → **默认无限等待**。

后果链：
1. 网关挂起 → worker 进程卡死
2. 计划任务 `MultipleInstances IgnoreNew` → 后续轮次全被跳过
3. 系统静默停摆，无任何告警

→ **P1-6**（含 SDK 无超时 + 自身无重试/重连）

#### 6.5 重复下单 —— ⚠️ 三层有效，一层不跨进程

`risk/controller.py:453 _duplicate_check` 三层：

| 层 | 数据源 | 跨进程 |
|---|---|---|
| L1 内存冷却（300s） | `self._recent_orders`（list） | ❌ **失效** |
| L2 未完成委托 | `recent_orders` 参数（来自 `journal.list_orders`） | ✅ |
| L3 近期成交 | 同上 | ✅ |

实测：

```
同进程内重复: duplicate_order in failed = True
新进程（无历史）：duplicate_order in failed = False
>>> 内存冷却是否跨进程失效: True
```

→ **P2-3**：L2/L3 提供兜底，风险可控，但"连续两次点击扫描"在重启后可能各下一单。

---

### 2.7 【第 7 项】重点复核：星财富 SDK 行情加密问题

**结论：确认存在。SDK 对**所有** POST 请求（含行情）都做了 AES-GCM 加密，与官方 README「行情接口仅签名不加密」矛盾。根因是把 `in` 误写成 `==`。未修改上游代码。**

#### 7.1 源码定位

`C:\FosunAI-Trading\sdk_src\fsopenapi\client.py:114-115`：

```python
is_market_path = request_path == "/market/" or "/optmarket/" in request_path
is_encrypted  = data is not None and not is_market_path
```

其中 `request_path` 由 `client.py:97` 拼出：

```python
request_path = f"{self.api_prefix}{path if path.startswith('/') else f'/{path}'}"
# 实际值示例：/api/v1/market/secu/quote
```

#### 7.2 为什么这个判断恒为 False（静态推理）

| 实际 `request_path` | `== "/market/"` | `"/optmarket/" in ...` |
|---|---|---|
| `/api/v1/market/secu/quote` | `False` | `False` |
| `/api/v1/market/kline` | `False` | `False` |
| `/api/v1/market/opt/secu/quote` | `False` | `False` |
| `/api/ops/v1/market/secu/quote` | `False` | `False` |

- `== "/market/"`：要相等，除非 `api_prefix=""` 且 path 恰为 `/market/` —— 实际不可能的调用形态。
- `"/optmarket/" in ...`：期权接口实际路径是 `/v1/market/opt/...`（见 `optmarket.py:41,49,55,63`），不含 `/optmarket/`。

→ **该分支是死代码**，`is_market_path` 永远 `False`，因此**任何带 body 的请求都会被加密**。

作者意图可从 `optmarket` 分支反推：原本想同时豁免 market 与 optmarket，只是 market 用了 `==` 而非 `in`。

#### 7.3 动态实证（关键证据）

`audit_evidence/probe_sdk_encryption.py`：回环回声服务 + 运行时替换 `get_valid_session` 为固定假会话，抓取真实发出的报文。

```
调用                                                 方法     报文形态
--------------------------------------------------------------------------------------------------------------
market.quote  (POST /v1/market/secu/quote)         POST   encrypted=True (iv=16B tag=24B content=28B)
market.kline  (POST /v1/market/kline)              POST   encrypted=True (iv=16B tag=24B content=60B)
market.min    (GET  /v1/market/min)                GET    encrypted=False
market.orderbook (GET /v1/market/secu/orderbook)   GET    encrypted=False
market.tick   (GET  /v1/market/secu/tick)          GET    encrypted=False
portfolio.get_holdings (POST /v1/portfolio/Holdings) POST   encrypted=True (iv=16B tag=24B content=192B)
account.list_accounts (POST /v1/account/Accounts)  POST   encrypted=True (iv=16B tag=24B content=4B)

=== 证据：行情 quote 的原始 body ===
{
  "encrypted": true,
  "iv": "fgA9A7S6HfLEZLLn",
  "tag": "gd+HS2LsDDgBHLAVrTADzw==",
  "content": "4vQg1Jkp82ExH1IsqFjpwcnkRhFF"
}
签名头存在: True
X-session 头: probe-session-1

=== 附加验证：解密行情密文 ===
解密成功 → 明文 {"codes":["hk00700"]}
```

最后一行是**决定性证据**：用同一把会话密钥能解回 `{"codes":["hk00700"]}`，证明这不是"乱码"或"空包"，而是**真正对行情 body 做了 AES-256-GCM 加密**。

#### 7.4 期权接口对照

```
/api/v1/market/secu/quote          encrypted=True
/api/v1/market/opt/secu/quote      encrypted=True     ← 期权行情也被加密
```

即 `"/optmarket/" in request_path` 这个本应豁免期权的分支同样未命中。

#### 7.5 风险定级与"为什么不由我改"

| 情形 | 后果 |
|---|---|
| 网关对**所有** POST 统一解密 | ✅ 无影响，只是 README 表述过时 |
| 网关的行情路由期望**明文** | ❌ `market.quote()` / `kline()` 全部失败 → 本工程 `get_quote()` 抛 `BrokerError` → 扫描全 `error` |

**无法判断属于哪一种**：需要真实凭证 + 一次真实行情调用。属客观未验证项。

**为什么不在本次修改**：

1. 用户明确要求"不要未经验证修改上游代码"
2. `sdk_src/` 是审计基线，保持与 upstream `c4c8a51` 字节一致，才能证明"我们没有偷偷补丁"
3. 正确的修法是提 PR 给上游（把 `==` 改 `in`），或等官方确认后再决定本地绕行方案

**建议的上游 issue 文本（供提交）**：

> `fsopenapi/client.py:114` 的 `is_market_path = request_path == "/market/" or "/optmarket/" in request_path` 恒为 `False`，因为 `request_path` 是 `/api/v1/market/...`。导致 `market.quote()` / `kline()` 的 POST body 被 AES-GCM 加密，与 README「行情接口走 /market/，仅签名不加密」矛盾。建议改为 `"/market/" in request_path`，同时补上 `/optmarket/` 或改为 `"/optmarket/" in request_path or "/market/opt/" in request_path`。

**拿到凭证后的三条验证路径（按成本排序）**：

1. 直接调 `client.market.quote(["hk00700"])`，看是否返回数据 —— 一步定性
2. 若失败，用 `client.market.min("hk00700")`（GET，未加密）作为降级取价源
3. 若仍需 POST 行情，给 SDK 打本地 patch 并在 `API_STATUS.md` 记录升级风险

---

### 2.8 【第 8 项】密钥加密、日志脱敏、VPS 迁移、紧急停止

#### 8.1 密钥加密 —— ✅ 合格

| 检查项 | 结果 | 证据 |
|---|---|---|
| 是否有明文落盘 | ✅ 无 | `data/` 下只有 `audit.db` / `paper_state.json`，**无 `vault.json`**（保险箱为空） |
| 磁盘上是否可读明文 | ✅ 加密 | `secrets.py:228 _wrap` → `dpapi:` 前缀密文；`test_vault_stores_ciphertext_on_disk` 断言明文 CANARY 不出现在文件里 |
| 是否写入系统环境变量 | ✅ 不写 | `secrets.py:326 inject_into_environ` 只写 `os.environ`（进程内）；`test_inject_into_environ_only_affects_process` 覆盖 |
| PEM 是否泄漏 | ✅ 不泄漏 | `redact_pem()` → `<pem len=... sha8=...>` 指纹，`test_redact_pem_gives_fingerprint_not_content` |
| 跨机迁移是否可解 | ✅ 可解且错口令拒绝 | `FSTV1` + scrypt(N=2^15) + AES-256-GCM；实测错口令 `InvalidTag` |
| DPAPI 是否绑定用户 | ✅ 是 | `test_dpapi_encrypted_value_is_user_bound_ciphertext` |

#### 8.2 日志脱敏 —— ✅ 无泄漏，⚠️ 两处可改进

全量扫描 `logs/` 中疑似敏感字段：

```
logs/evidence/check_env.json:33:    "api_key_present": false,
logs/evidence/check_env.json:35:    "client_private_key": "",
```

仅字段名与空值，**无任何真实密钥值**。源码侧：

```bash
$ grep -rn "api_key\|private_key" src/ --include=*.py | grep -iE "log|print|logger"
# （无输出）
```

脱敏实现 `secrets.py:63 redact()` 递归处理 dict/list，`test_audit_redacts_sensitive_fields` 断言 `sk-super-secret-value` / `hunter2` 不出现在审计 detail 里。

⚠️ 两处可改进：

1. **`logs/evidence/junit.xml` 等审计工件包含完整 payload** —— 本次审计发现 junit XML 会把参数化用例的入参写进文件（如测试里的 JSON 字符串）。当前无敏感值，但若测试未来传入密钥样例，会落盘。→ P2-9
2. **`ai/providers.py:271` 的 `AIModelError` 带 `resp.text[:300]`** —— 上游错误体可能回显部分请求头。该字符串进入 `model.health()["last_error"]` 并显示在 Web。当前无泄漏，但属潜在面。→ P2-8

#### 8.3 VPS 迁移 —— ✅ 机制可用，⚠️ 口令传递有泄漏面

| 检查项 | 结果 | 证据 |
|---|---|---|
| 导出包是否含明文 | ✅ 否 | `secrets.enc` 116 字节，`b"key-1" not in blob` |
| 错口令是否拒绝 | ✅ 是 | `InvalidTag`（AES-GCM 认证失败） |
| 是否可跨机器 | ✅ 是 | scrypt 派生，不依赖 DPAPI |
| 业务数据是否含密钥 | ✅ 否 | `bundle.zip` 只含 `config.yaml` / `audit.db` / `paper_state.json` / `stops.json` |
| 是否包含 `.env` | ✅ 不包含 | `migrate_export.ps1:62` 的 `$items` 无 `.env` |

⚠️ **P1-10：迁移口令通过命令行传递**

`scripts/migrate_export.ps1:50`：

```powershell
& $venvPy -m ai_trader.cli vault export --passphrase $Passphrase --out $encPath
```

`--passphrase` 会出现在子进程命令行里，Windows 上可通过任务管理器详细信息、`Get-CimInstance Win32_Process`、Sysmon 事件、或进程审计日志读取。脚本第 48 行虽然设了 `$env:VAULT_PASSPHRASE`，但**没有用它**。

同样问题存在于 `DEPLOYMENT.md §8` 的 `vault import --passphrase ...`。

#### 8.4 紧急停止 —— ⚠️ **机制有效但状态不持久（P0-2）**

生效范围实测（`test_risk_controls.py` + `test_engine_flow.py`）：

| 场景 | 结果 |
|---|---|
| 触发后单笔意图 | ✅ 拒绝，`kill_switch` in `failed_checks` |
| 触发后整轮扫描 | ✅ `blocked_reason` 非空，`outcomes=[]`，0 委托 |
| `resume()` 后 | ✅ 恢复 |
| 环境变量强开 live 但无确认短语 | ✅ 仍不可下单 |

跨进程实测（`audit_evidence/probe_killswitch_persist.log`）：

```
=== 进程 1：触发紧急停止 ===
  kill_switch = True
  can_send_real_order = (False, 'KILL_SWITCH 已触发，禁止一切券商连接')
  data 目录写入的急停标记文件: []            ← 没有落盘

=== 进程 2：重启后（模拟进程崩溃 / 计划任务重启）===
  kill_switch = False   <-- 是否仍为 True ?
  halt_reason = ''
```

原因：`risk/controller.py:222-229` 只改内存字段，`config.py:248 build_gate` 每次从 config/env 读 `kill_switch`（默认 `false`）。

→ **P0-2**：紧急停止实质是"本次进程内的临时静音"。进程崩溃/计划任务重启/机器重启后，系统自动恢复交易能力。

#### 8.5 `.env` 权限（新发现）

```
$ icacls .env
.env BUILTIN\Administrators:(I)(F)
     NT AUTHORITY\SYSTEM:(I)(F)
     BUILTIN\Users:(I)(RX)
     NT AUTHORITY\Authenticated Users:(I)(M)      ← 任意已认证本地用户可修改
```

在单用户 VPS 上影响有限；一旦机器有第二个本地账户（运维、备份服务账户），该账户可改写 `.env` 把 `LIVE_TRADING` 改成 `true`。

`secrets.py:223` 的 `os.chmod(tmp, 0o600)` 在 Windows 上基本无效（无 POSIX 权限位）。

→ **P1-12**

---

### 2.9 【第 9 项】管理台是否仅监听 localhost

**结论：是。实测仅 `127.0.0.1:8781`，无 `0.0.0.0` 绑定。但缺少"非回环必须设口令"的强制校验。**

#### 证据 1：实际监听

```
$ Get-NetTCPConnection -State Listen -LocalPort 8781
LocalAddress LocalPort OwningProcess
------------ --------- -------------
127.0.0.1         8781         10980

=== 是否存在任何 0.0.0.0 监听 ===
无（8781/8770 均未绑定 0.0.0.0）
```

配置默认值 `config/config.yaml:105-107`：

```yaml
web:
  host: "127.0.0.1"
  port: 8781
```

#### 证据 2：鉴权覆盖矩阵

| 路由 | 需要口令 | 实测（设口令后未登录） |
|---|---|---|
| `/login` `/logout` | 否 | — |
| `/` | 是（Cookie） | 303 → `/login` |
| `/api/status` `/api/positions` `/api/orders` `/api/signals` `/api/risk` `/api/audit` `/api/market/{symbol}` | 是 | **401** |
| `/api/scan` `/api/monitor` `/api/kill` `/api/resume` `/api/paper/reset` | 是 | **401**（POST `/api/kill` 实测 401） |
| **`/api/docs`** | **否** | **200** ⚠️ |
| **`/openapi.json`** | **否** | **200** ⚠️ |

12 个业务路由对应 12 处 `check(request)`，覆盖完整。

#### 缺陷

⚠️ **P1-11：绑定非回环地址时未强制要求口令**

```bash
$ grep -c "0.0.0.0\|is_loopback\|must set WEB_ADMIN_PASSWORD" src/ai_trader/web/app.py
0
```

`host` 来自 `WEB_HOST` 或 `config.web.host`，**没有任何校验分支**。
若用户按 `DEPLOYMENT.md §5` 的示例执行 `run_web.ps1 -Host_ 0.0.0.0` 又忘了设 `WEB_ADMIN_PASSWORD`，则 `Auth.verify()` 因 `enabled=False` 恒返回 `True` —— **全程无鉴权，包括 `/api/kill`**。

⚠️ **P2-1：`/api/docs` 与 `/openapi.json` 免鉴权**（`app.py:81 docs_url="/api/docs"`），暴露完整 API schema。

---

### 2.10 补充审计：本次额外发现的三个问题

以下不在用户的 9 项清单内，但属同等级风险，一并列出。

#### （a）下单结果不可对账（并入 P0-1）

`strategy/engine.py:307-313`：

```python
try:
    result = self.broker.place_order(req)
except BrokerError as exc:
    ... 写 broker_reject 日志，return None
```

只捕获 `BrokerError`。若 SDK 抛 `requests.ConnectionError` / `socket.timeout`（**SDK 无超时，只有进程被杀才会中断**），异常会穿透到 `scan()` 的兜底 `except Exception` → 标记该标的 `error`，但：

- 订单**可能已在券商侧成交**
- 本地**没有任何记录**
- 没有用 `client_id` 回查（虽然 `fosun_adapter.py:350` 生成了 `client_id`，但从不用于对账）

#### （b）持仓解析静默返回空（P1-2）

`fosun_adapter.py:255`：

```python
for item in _as_list(_payload(resp)):   # 结构不符 → _as_list 返回 []
    ...
return out                              # 返回 []
```

实测：

```
=== 2) 持仓响应结构未知（字段名全不匹配）===
  get_positions() -> []
  >>> 静默返回空列表: True（风控会以为没有持仓）
```

风控拿到空持仓 → `max_positions`(8)、`max_position_pct`(15%)、`max_gross_exposure`(60%) 全部失效 → **fail-open**。

对比：同样是解析失败，`get_summary` 返回 `equity=0` 会让所有买单被 `per_trade_risk` 拒绝（fail-closed），而 `get_positions` 是 fail-open。两者方向不一致。

#### （c）单日亏损熔断依赖券商字段（P1-3）

`fosun_adapter.py:242`：

```python
day_pnl=_fnum(_first(row, ("dayPnl", "todayPnl", "dailyPnl"))),   # 缺字段 → 0.0
```

实测（券商返回无 `dayPnl`）：

```
  get_summary -> equity=1000000.0 day_pnl=0.0
  daily_loss_limit check: [CheckResult(name='daily_loss_limit', ok=True,
      detail='当日盈亏 +0.00（-0.00%）/ 上限 3.00%')]
  >>> 熔断是否形同虚设: True
```

熔断"安静地通过"，而不是"无法判断则拒绝"。

---

## 3. 问题清单与修复建议

### 3.1 P0（阻断真实交易，必须先修）

#### P0-1 下单结果不可信：编造订单号 + 假定成功 + 无法对账

| 项 | 内容 |
|---|---|
| **位置** | `src/ai_trader/broker/fosun_adapter.py:367`、`:370`；`src/ai_trader/strategy/engine.py:307-313` |
| **现象** | 响应体为空/结构不符时，用 `f"FS-{HHMMSS}"` 编造 `order_id`，并把 `status` 默认成 `"submitted"` |
| **实测证据** | `probe_adapter_edges.log`：`order_id='FS-121425'  broker_order_id=''  status='submitted'`，`是否编造 ID: True  是否假定成功状态: True` |
| **风险** | 真实交易下产生"幽灵订单"：本地记录一笔券商不存在的单；后续查单/撤单/持仓对账全部错位；风控的 `max_positions`、`daily_trade_count` 计数被污染 |
| **修复** | ① `oid` 为空 → `raise BrokerError("券商未返回 orderId，订单状态未知", code="UNKNOWN_ORDER_STATE")`，**绝不编造**；② `status` 缺失 → 标 `"unknown"` 而非 `"submitted"`；③ `_execute` 捕获范围扩到 `Exception`，并在异常时用 `clientId` 调 `list_orders(client_id=...)` 回查，查不到才判定"未提交"；④ `client_id` 改为由 `(symbol, side, date, signal_id)` 稳定派生，使重试具备幂等性 |
| **验证** | 新增测试：`test_place_order_empty_payload_raises`、`test_place_order_unknown_status_not_submitted`、`test_execute_reconciles_by_client_id_on_exception` |

```python
# 建议改法（示意，未执行）
oid = str(_first(payload, ("orderId", "order_id", "id"), ""))
if not oid:
    raise BrokerError(
        f"券商未返回 orderId，无法确认 {req.symbol} 订单状态（payload keys={list(payload)[:6]}）",
        code="UNKNOWN_ORDER_STATE",
    )
status = str(_first(payload, ("status", "orderStatus"), "unknown"))
```

#### P0-2 紧急停止不持久化，进程重启自动解除

| 项 | 内容 |
|---|---|
| **位置** | `src/ai_trader/risk/controller.py:222-229`（`halt`/`resume` 只改内存）；`src/ai_trader/config.py:248`（`build_gate` 每次从 config 读默认 `false`） |
| **现象** | 急停状态仅存于 `SafetyGate.state`，无任何落盘 |
| **实测证据** | `probe_killswitch_persist.log`：进程 1 `kill_switch=True`；进程 2（重启）`kill_switch=False`，`data/` 目录无任何 halt 标记文件 |
| **风险** | 你在生产环境按了急停，系统随后因崩溃/计划任务/机器重启而自动恢复交易。这是最危险的 fail-open 场景 |
| **修复** | 引入哨兵文件：`halt()` 写 `data/HALT.json`（含 reason/时间/操作者），`build_gate()` 启动时若检测到该文件则强制 `kill_switch=True`；`resume()` 删除文件并写审计。Web 首页显示哨兵内容 |
| **验证** | 新增测试：`test_kill_switch_survives_restart`（同 `probe_killswitch_persist` 的双进程模式）、`test_halt_sentinel_blocks_first_scan_after_restart` |

```python
# 建议改法（示意，未执行）
HALT_SENTINEL = "HALT.json"
def halt(self, reason: str) -> None:
    self.halt_reason = reason
    self.gate.state.kill_switch = True
    (self.cfg.data_dir / HALT_SENTINEL).write_text(
        json.dumps({"reason": reason, "at": utcnow_iso()}, ensure_ascii=False), encoding="utf-8")
# build_gate 后追加：
if (cfg.data_dir / HALT_SENTINEL).exists():
    cfg.gate.state.kill_switch = True
```

---

### 3.2 P1（真实交易前必须修）

| # | 问题 | 位置 | 风险 | 修复建议 |
|---|---|---|---|---|
| **P1-1** | SDK 行情 POST 被加密，与官方 README 矛盾 | `sdk_src/fsopenapi/client.py:114` | 网关若期望明文，`quote`/`kline` 全失败 → 扫描全部 `error` | 不改上游。① 提 issue 给星财富（文本见 §7.5）；② 拿到凭证后先用 `market.min`（GET 未加密）降级取价；③ 必要时本地 patch 并把风险写进 `API_STATUS.md` |
| **P1-2** | `get_positions()` 解析失败静默返回 `[]` | `fosun_adapter.py:255` | 风控以为无持仓 → 仓位/敞口/持仓数上限全部 fail-open | 解析后校验：若 `payload` 非空但 `out==[]`，抛 `BrokerError("持仓解析失败，拒绝以空持仓继续", code="PARSE_MISMATCH")` |
| **P1-3** | 单日亏损熔断依赖券商 `dayPnl`，缺字段即失效 | `fosun_adapter.py:242`；`risk/controller.py:389` | 券商不回该字段时，3% 熔断"安静通过" | 缺字段时改为**自行计算**（当日权益快照 − 当前权益），仍拿不到则 `daily_loss_limit` 判定为 `ok=False`（fail-closed） |
| **P1-4** | 无订单超时机制 | `paper.py:444 refresh_pending`；无 `engine` 侧超时 | 挂单永久占用资金/额度；同向新单被 L2 永久阻断 | 增加 `order_timeout_seconds`（建议 300–1800），`monitor()` 中把 `status=="submitted"` 且超时的单标记 `expired` 并调 `cancel_order` |
| **P1-5** | `cancel_order` 是死代码，引擎从不调用 | `fosun_adapter.py:380`；全库无调用者 | 无法实现 P1-4；人工撤单只能上券商 App | 在 `StrategyEngine` 增 `cancel(order_id)`，Web 增 `/api/orders/{id}/cancel`（经风控 `can_cancel_real_order`） |
| **P1-6** | 无重连/重试；SDK 无 HTTP 超时 | `src/` 无重试；`sdk_src/fsopenapi/client.py:174`、`auth.py:179` 未传 `timeout` | 网关挂起 → worker 卡死 → 计划任务跳过 → 系统静默停摆 | ① `socket.setdefaulttimeout(30)` 作为兜底（不改 SDK）；② `FosunBroker` 对 `retryable` 错误做有限重试（3 次指数退避）；③ 会话失效（401/`AuthenticationError`）时 `client.auth_manager.create_session()` 重建后重试一次 |
| **P1-7** | 行情来源（合成/真实）不进审计 | `engine.py:174` 的 `notes` 未被持久化 | 事后无法证明某笔成交基于何种行情 | `TradingSignal` 增 `quote_source` 字段；`record_signal` / `record_order` 落库该字段；Web 委托表展示 |
| **P1-8** | 无止损持仓静默不告警 | `risk/controller.py:534 scan_exits` | 持仓裸奔，`monitor()` 返回 `[]`，审计零事件 | `monitor()` 中对"有持仓但无止损规则"的标的写 `audit: position_without_stop` 并在 Web 高亮 |
| **P1-9** | LLM 配置缺失时静默降级为规则引擎并继续下单 | `runtime.py:90 build_ai`；`config.yaml ai.provider` | 用户以为在用大模型，实际是规则信号 | ① live 模式下 `ai_fallback_reason` 非空 → 禁止下单；② 或要求 `ai.allow_fallback: true` 显式同意 |
| **P1-10** | 迁移口令通过命令行传递 | `scripts/migrate_export.ps1:50`；`DEPLOYMENT.md §8` | 口令出现在进程命令行，可被同机进程/审计日志读取 | CLI 支持 `--passphrase-env VAULT_PASSPHRASE` 或 stdin 读取；脚本只用环境变量传参，并 `Remove-Item Env:` 清理 |
| **P1-11** | 绑定非回环地址时未强制口令 | `web/app.py:81` 附近；`main()` 的 host 取值 | `-Host_ 0.0.0.0` + 无口令 = 全程无鉴权（含 kill switch） | 启动时校验：`host not in ("127.0.0.1","localhost","::1") and not WEB_ADMIN_PASSWORD` → 直接拒绝启动并打印原因 |
| **P1-12** | `.env` ACL 过宽 | 文件系统继承权限 | 任意本地已认证用户可改写安全开关 | `icacls .env /inheritance:r /grant:r "%USERNAME%:F"`；`.env` 创建后自动收紧；文档补一行 |

---

### 3.3 P2（工程质量与加固）

| # | 问题 | 位置 | 修复建议 |
|---|---|---|---|
| **P2-1** | `/api/docs`、`/openapi.json` 免鉴权 | `web/app.py:81` | 生产设 `docs_url=None, openapi_url=None`；或对二者也加 `check()` |
| **P2-2** | `paper_state.json` 损坏静默重置（资金回到 1,000,000、持仓清零） | `paper.py:194-205` | 损坏时备份为 `.corrupt` 并**拒绝启动**，或至少 `journal.log("state_reset", ...)` |
| **P2-3** | L1 内存冷却跨进程失效 | `risk/controller.py:453` | 把冷却记录也落库（或在 `evaluate` 时把 journal 最近订单作为 L1 数据源） |
| **P2-4** | Cookie 无 `Secure` 标志；无 CSRF token | `web/app.py:99` | 反代 TLS 后加 `secure=True`；POST 接口加 CSRF token 或改 Bearer |
| **P2-5** | 无 FX 折算，多币种权益名义加总 | `risk/controller.py:40` | 引入汇率表，统一折算到报告币种；或明确限制单币种交易 |
| **P2-6** | 交易时段为近似表（无假期/半日市/DST） | `risk/controller.py:147 SESSIONS` | 接券商 `market status` 或引入交易日历 |
| **P2-7** | `audit.jsonl` 无轮转；`logging` 无文件 handler | `audit/journal.py:173`；`runtime.py:125` | 按日切分 + 保留 N 天；`RotatingFileHandler` |
| **P2-8** | `AIModelError` 带上游响应片段，显示在 Web | `ai/providers.py:271` | 截断到 120 字符并过滤疑似 header 行 |
| **P2-9** | `junit.xml` 会把测试入参落盘 | `logs/evidence/junit.xml` | 审计工件单独存放并 gitignore；测试中避免传入类密钥字符串 |

---

## 4. 未验证项（诚实边界）

| 项 | 阻塞原因 | 影响 | 解禁条件 |
|---|---|---|---|
| 真实券商接口的响应字段名 | 无 `base_url`/`api_key`/两段 PEM | `_first()` 候选字段表可能全不命中（→ P1-2 触发） | 拿到凭证 |
| 网关是否接受加密的行情 POST | 同上 | P1-1 的最终后果未知 | 一次真实 `market.quote` |
| A股通代码格式（`sh`/`sz` 前缀？） | 文档未注明 | `market_of()` 的推测可能错 | 一份官方代码表 |
| 真实 LLM 输出质量 | 无 `AI_API_KEY` | 信号可靠性未知 | 配 Key 后跑 `test_llm_real_call_if_key_present` |
| 券商 `orderType` 取值语义 | README 示例只给了 1/3 | 可能下错单类型 | 官方参数表 |
| 限流配额 | 未申请 | `max_orders_per_minute` 取值可能过激 | 官方配额 |
| 服务端条件单（`stopLossPrice`）语义 | 未验证 | 是否可托管止损未知 | 官方说明 + 沙箱试 |

---

## 5. 建议的修复路线

```
第 1 步（P0，1–2 天）
  ├─ P0-1 订单号编造 → 抛错 + client_id 对账
  └─ P0-2 急停落盘哨兵
      └─ 每步补 2–3 个测试，跑 mutation 验证新测试有效

第 2 步（P1 中"不依赖凭证"的 8 项，3–5 天）
  ├─ P1-2 持仓解析 fail-closed
  ├─ P1-3 熔断自行计算
  ├─ P1-4 订单超时 + P1-5 撤单接线
  ├─ P1-6 超时与有限重试
  ├─ P1-7 行情来源进审计
  ├─ P1-8 无止损持仓告警
  ├─ P1-9 模型降级在 live 下禁止下单
  ├─ P1-10 口令改走环境变量
  ├─ P1-11 非回环强制口令
  └─ P1-12 .env ACL 收紧

第 3 步（拿到凭证后，1–2 天）
  ├─ P1-1 定性：直接调 market.quote 看是否通
  ├─ 核对全部响应字段名，修正候选字段表
  └─ 用官方 SimAccountCreate 做灰度

第 4 步（P2，可与第 3 步并行）
  └─ 9 项工程质量项
```

---

## 6. 附录

### 6.1 证据文件清单（`audit_evidence/`）

| 文件 | 内容 | 对应结论 |
|---|---|---|
| `pytest_verbose.txt` | 156 行逐条测试输出 | §2.1 |
| `mutation_plugin.py` | 6 种内存变异的注入插件 | §2.1 证据 3 |
| `mutation_report.txt` | 6 组变异的结果 | §2.1 证据 3 |
| `probe_interface_coverage.log` | Broker/AIModel 契约 vs 实现 | §2.2 |
| `probe_empty_bodies.log` | 272 函数空体扫描 | §2.2 |
| `probe_ai_chain.py` / `.log` | 假 LLM 全链路 | §2.3 |
| `probe_adapter_edges.py` / `.log` | 编造 ID / 静默空持仓 / 状态重置 / 冷却跨进程 | §2.2、§3.1、§3.2 |
| `probe_sdk_encryption.py` / `.log` | 行情加密实证 + 解密回明文 | §2.7 |
| `probe_optmarket_compare.log` | 期权路径对照 | §2.7.4 |
| `probe_killswitch_persist.log` | 急停跨进程失效 | §2.8.4、P0-2 |
| `probe_stop_and_isolation.log` | 无止损持仓静默 + 行情隔离 | §2.4、§2.6.1 |
| `probe_traceability.log` | 行情来源不进审计 + 熔断依赖字段 | P1-3、P1-7 |
| `probe_web_auth.log` | 鉴权矩阵 / docs 免鉴权 | §2.9 |
| `check_env_after_audit.json` | 审计后安全快照 | §1.3 |

### 6.2 只读性自查

```bash
# 审计开始（12:09）后，产品源文件是否有任何改动
$ find src tests scripts config -newermt "2026-11-08 12:09" -type f -not -path "*__pycache__*" | wc -l
0

# 上游 SDK 是否被改动
$ find sdk_src -newermt "2026-11-08 12:09" -name "*.py" -not -path "*/.git/*" -not -path "*__pycache__*" | wc -l
0
```

**两项均为 0** —— 7,234 行产品代码与 `sdk_src/` 全部字节未变。

新增文件仅：

```
INDEPENDENT_AUDIT.md          （本报告）
audit_evidence/               （16 个文件：探针脚本 + 原始输出）
```

`audit_evidence/` 内的 `.py` 是审计工具，不被产品代码 import，不影响运行时。

### 6.3 复现本次审计

```powershell
cd C:\FosunAI-Trading
$py = ".venv\Scripts\python.exe"

# 1) 测试
& $py -m pytest -o addopts="" -v -p no:warnings

# 2) 变异检验
$env:PYTHONPATH="audit_evidence"; $env:AUDIT_MUTATE="approve_all"
& $py -m pytest -o addopts="" -q -p mutation_plugin tests/test_risk_controls.py
Remove-Item Env:\AUDIT_MUTATE, Env:\PYTHONPATH

# 3) 回环探针（均不联网）
& $py audit_evidence/probe_ai_chain.py
& $py audit_evidence/probe_sdk_encryption.py
& $py audit_evidence/probe_adapter_edges.py

# 4) 安全状态
& $py -m ai_trader.cli check-env
```

### 6.4 对上一轮 `REPORT.md` 的修正建议

本次审计发现 `REPORT.md` 有两处表述需要更新（**我未修改该文件**，留待决定）：

1. §0「AI 是否真实运行」：应补充"LLM 配置缺失时会静默降级为规则引擎并继续下单"（P1-9）这一行为特征。
2. §8.2「工程侧遗留问题」：应把 P0-1（编造订单号）、P0-2（急停不持久）、P1-4/P1-5（订单超时/撤单未接线）、P1-6（无超时无重连）列为**已知缺陷**，而不是仅在"建议"里提及。
3. §5 表格中"止损注册表落盘 → 重启后仍保护持仓"表述正确，但应补一句"前提是该持仓当初登记了止损；未登记的持仓不会被监控"。

---

## 7. 审计者结论

这份工程的骨架是扎实的：**风控确实不可绕过，合成数据确实进不了真实路径，AI 链路确实能产出信号，测试确实能抓住回归**。这些都是我一开始怀疑、最后被证据说服的部分。

但也必须直说：**它现在还不能上真实资金。** 两个 P0 都不是"风格问题"，而是会在真实场景里直接造成损失或失控的缺陷：

- 编造订单号会让你以为有一笔单，其实券商那边可能根本没有；或者反过来，单成交了而你这边什么都没记。
- 急停不持久意味着"我按了停止按钮"这个人类最重要的保险，在进程重启后就自动失效了。

12 个 P1 里，P1-2（静默空持仓）、P1-3（熔断失效）属于**安全机制在异常输入下悄悄失灵**的类型 —— 比直接报错更危险，因为它们不报警。

建议按 §5 的顺序推进，**P0 修完并补上跨进程测试之前，不要打开 `LIVE_TRADING`**。

---

## 8. 修复后复评（2026-11-08 13:15）

本节是**二次核查**，不是自我声明。每条都回到代码与测试验证，证据文件在 `audit_evidence/`。

### 8.1 P0 复评

#### P0-1 订单状态完整性 —— ✅ 已关闭

| 核查点 | 结论 | 证据 |
|---|---|---|
| 是否还编造订单号 | ❌ 不再编造 | `fosun_adapter.py:652-672`：无 `orderId` 时 `order_id=f"UNCONFIRMED-{client_id}"`、`broker_order_id=""`、`status="unknown"` |
| 是否还默认 `submitted` | ❌ 不再默认 | 缺 status → `"unknown"`；空响应 → 抛 `OrderStateUnknown(EMPTY_ORDER_RESPONSE)` |
| 是否用 `client_id` 对账 | ✅ 是 | `fosun_adapter.py:796 reconcile_order()` 先按 `clientId` 过滤，再按字段/近似匹配 |
| 网络异常是否进入核对流程 | ✅ 是 | `engine.py:364 _reconcile_unknown()`；三种结果分别落 `filled/…`、`not_accepted`、`unknown` |
| 是否禁止盲目重试 | ✅ 是（双重） | ① `place_order` 不用 `_read`（无重试）；② 风控 `unresolved_order_state` 封锁该标的 |
| 回归测试 | ✅ 14 例 | `test_order_state_integrity.py`；含"超时但券商已受理"场景，断言 `place` 只调用 1 次 |

**复评意见**：原判断"会产生幽灵订单"的路径已全部封堵。新增的 `unknown` 状态是**显式的未知**，
而不是伪装成已知 —— 这是正确的方向。

#### P0-2 急停持久化 —— ✅ 已关闭

| 核查点 | 结论 | 证据 |
|---|---|---|
| 是否落盘 | ✅ `data/HALT.json` | 原子写（mkstemp→fsync→replace） |
| 重启后是否仍停止 | ✅ 是 | `config.py:build_gate(raw, data_dir)` 读哨兵覆盖配置；`test_halt_survives_restart` |
| 跨进程是否仍停止 | ✅ 是 | `test_process_restart.py` 用 5 个真实子进程依次验证 |
| 解除是否需人工确认 | ✅ 逐字 `RESUME-TRADING` | CLI `--confirm` 必填；Web 空值 400；`test_resume_requires_exact_phrase` 覆盖 5 种错误写法 |
| 存储异常是否拒绝交易 | ✅ 是 | 读失败→`halt_store_failed`；写失败→仍停 + 抛错；风控新增 `halt_storage` 检查 |

**复评意见**：这是本轮最有价值的修复。急停从"临时静音"变成了"持久停机"。

### 8.2 P1 复评

| 编号 | 原问题 | 复评 | 关键验证 |
|---|---|---|---|
| P1-1 | 持仓解析失败静默 `[]` | ✅ 关闭 | `_extract_rows` 区分"读不懂/真空"；引擎整轮阻断；`broker.calls["place"] == 0` |
| P1-2 | 缺 `dayPnl` 熔断失效 | ✅ 关闭 | `day_pnl_known` 三段透传；`daily_loss_limit` 判 `ok=False` |
| P1-3 | HTTP 无超时 | ✅ 关闭 | 实例级注入 `(connect,read)`；真回环 1s 超时实测生效 |
| P1-4 | 订单无超时 | ✅ 关闭 | `_expire_stale_orders` + `order_timeout_seconds`；700s 挂单被撤并置 `expired` |
| P1-5 | 冷却不跨进程 | ✅ 关闭 | `UNIQUE(symbol,side,bucket)`；两连接并发只一个成功；两 worker 只下 1 单 |
| P1-6 | 撤单死代码 | ✅ 关闭 | `engine.cancel_order` + Web + CLI 三入口；单测 10 例 |
| P1-7 | 无重连/无对账 | ✅ 关闭 | `_read` 有限重试、鉴权重连一次、下单不重试、`reconcile_order` |
| P1-8 | SDK 行情加密 | ✅ 按约束处理 | 未改上游（26 文件 sha256 全等 + 那行 bug 原样存在）；走官方 GET `/v1/market/min` 兜底并标注 |
| P1-9 | 模型静默降级仍下单 | ⚠️ **仅部分** | 已暴露 `ai_fallback_reason`；未禁止下单（需产品决策），见 `FIX_REPORT §11.1` |
| P1-10 | 迁移口令走命令行 | ✅ 关闭 | CLI 支持环境变量传口令；`migrate_export.ps1` 不再拼 `--passphrase` |
| P1-11 | 非回环绑定无口令 | ✅ 关闭 | `guard_bind()` 默认硬拒绝，实测 exit 4；`--allow-insecure-bind` 为显式逃生口 |
| P1-12 | `.env` ACL 过宽 | ✅ 关闭 | `icacls /inheritance:r`；实测收紧后仅 `DESKTOP-381H7ON\User:(F)` |

### 8.3 复评中发现的新问题（审计当时没看到）

| 级别 | 问题 | 说明 |
|---|---|---|
| P2 | **Windows `Start-Process` 与本机代理变量冲突** | 环境同时存在 `HTTP_PROXY` 与 `http_proxy` 时，`Start-Process` 与 `Env:` 驱动器都会抛 `已添加项。字典中的关键字…`。已改用 `System.Diagnostics.Process` 直连，并写入 `DEPLOYMENT.md §10.9` |
| P2 | `WaitForExit(ms)` 后 `ExitCode` 为 `$null` | PS 5.1 已知行为；改为无参 `WaitForExit()` 后取值 |
| P2 | 数据目录 ACL 同样过宽 | 与 `.env` 同源问题，一并收紧 |

### 8.4 复评结论

| 项 | 修复前 | 修复后 |
|---|---|---|
| P0 | 2 | **0** |
| P1 | 12 | 10 关闭 + 2 部分（P1-9 需决策；P1-10…12 已关闭） |
| P2 | 8 | 8（未处理，均为加固项）+ 3 新增（工具链） |
| 测试 | 145（1 skip） | **224（1 skip，0 失败）** |
| 变异检验 | 6/6 捕获 | **8/8 捕获** |

**但是**：复评**不改变**"尚不具备真实交易资格"的结论。原因从"代码缺陷"变成了"外部未验证"：

1. 无凭证 → 券商响应字段名、`clientId` 过滤能力、行情加密网关行为全部未知
2. 无 AI Key → LLM 真实调用仍未测试
3. P1-9 的降级策略仍待人工决策
4. 本轮所有网络验证都只到 `127.0.0.1`

**建议**：拿到凭证后，按 `FIX_REPORT.md §12` 的审核清单逐项确认，并先做"只读连通性 + 官方模拟账户"两阶段灰度，
再考虑把 `PAPER_TRADING` 置为 `false`。

---

## 9. 第三轮复评（2026-11-08 13:40）—— P1 收尾

> 本节接续 §8。对象是上一轮遗留的 P1-9 / P1-11，以及本轮**新发现**的 3 个缺陷。
> 原文正文不修改，保留审计当时的判断。

### 9.1 遗留 P1 关闭情况

| 编号 | 原问题 | 复评 | 关键证据 |
|---|---|---|---|
| **P1-9** | 模型静默降级为规则引擎后仍下单 | ✅ **关闭** | `ai/authority.py` 四类拒绝码；`build_ai` 不再降级；9 场景真跑 |
| **P1-11** | `--allow-insecure-bind` 逃生口 + 有口令即可非回环 | ✅ **关闭** | `guard_bind(host)` 只接受回环；CLI 参数已删；非回环 exit 4 |

#### P1-9 复评细节

| 核查点 | 结论 | 证据 |
|---|---|---|
| 规则引擎能否开仓 | ❌ 不能（`can_trade=False`） | `test_rule_engine_cannot_open_positions`：`blocked` + `AI_NOT_DECISION_AUTHORITY` + 0 委托 |
| LLM 缺 Key 时行为 | 返回 `UnavailableModel`，`analyze()` 必抛 | `test_unavailable_model_blocks_with_reason` |
| AI 超时/断线/非法 JSON/HTTP 错误 | 全部 `blocked`，0 委托 | 场景 2/3/4（真实回环 HTTP） |
| 已持仓在 AI 失效时是否受保护 | ✅ 仍执行止损 | 场景 9：停掉 AI 后抬止损 → 触发 `exit` |
| 恢复后是否重放旧信号 | ❌ 不会 | 场景 7：恢复后**新增** 1 笔；`STALE_SIGNAL` 拦截超 TTL 信号 |
| 每次拒绝是否留原因 | ✅ `entry_blocked` 事件带 code/reason/symbol/model | 审计断言 |

**复评意见**：这一项从"行为约定"变成了"结构约束"。特别值得注意的是
**它改变了系统的开箱行为** —— 默认 `provider=heuristic` 时系统只分析不交易。
这是正确的，但必须在运维文档里讲清楚（已写入 README 与 `check-env` 输出）。

#### P1-11 复评细节

| 核查点 | 结论 |
|---|---|
| `--allow-insecure-bind` 是否还存在 | ❌ 已删除（解析即报 unrecognized） |
| 有口令能否绑非回环 | ❌ 不能（实测 exit 4） |
| 回环是否仍可用 | ✅ 127.0.0.1 / localhost / ::1 |
| 是否留下测试 | ✅ 3 例（仅回环、有口令也拒、参数不存在） |

**复评意见**：这是最干脆的一次修复 —— 直接取消能力，而不是加警告。

### 9.2 本轮新发现的 3 个缺陷（原审计未覆盖）

这 3 个都不是"新引入的 bug"，而是**原有代码在并发/时区场景下的隐患**，
因为上一轮没有对应的测试形态，所以没被触发。

| 编号 | 缺陷 | 严重度 | 触发条件 | 后果 | 修复 |
|---|---|---|---|---|---|
| N-1 | naive/aware 时区混算 | **P1** | 调用方传 naive `now`，订单时间为 aware ISO | `TypeError` → 风控 fail-closed → **全系统拒绝交易**（可用性事故） | `risk/controller.py` 与 `ai/authority.py` 统一 `_aware()` |
| N-2 | 多连接并发首开健康库 | **P1** | 8 连接同时 `AuditJournal(db)` | 部分连接 `attempt to write a readonly database` → worker 启动失败 | `_open_and_init` 重连重试 + `_retry_write` |
| N-3 | 哈希链跨进程并发断链 | **P1** | Web 与 worker 同时写审计 | `verify_chain()` 误报"被篡改"（`broken_at_seq=14`） | `log()` 用 `BEGIN IMMEDIATE` 把"读 prev + 写行"串行化 |

**N-3 值得单独说明**：审计链是我们证明"没被改过"的核心机制。
如果并发写入会让它误报，那这个机制在真实部署（Web + worker 双进程）下就是废的。
修复后 150 条并发写入链完整、条数精确。

### 9.3 测试与证据

```
$ pytest -o addopts="" -v
======================= 256 passed, 1 skipped in 27.28s =======================
junit: tests=257  failures=0  errors=0  skipped=1
```

| 项 | 上轮 | 本轮 |
|---|---|---|
| 用例总数 | 224 | **257**（+33） |
| 新增测试文件 | — | `test_ai_authority.py`(17)、`test_long_running.py`(15) |
| worker 连续运行 | 未覆盖 | 5 轮脚本实测 + 40 轮单测 |
| 并发写审计 | 未覆盖 | 6×25 写 + 8 抢槽 |

### 9.4 未关闭项

| 编号 | 项 | 状态 | 说明 |
|---|---|---|---|
| P2 全部 | 8 项加固 | ❌ 未动 | `/api/docs` 免鉴权、Cookie 无 Secure、日志轮转、FX、交易日历等 |
| N-4 | `check-env` 日志污染 JSON | 已知 | 不要 `2>&1` 合并；或用 `--quiet` |
| N-5 | 默认"能跑但不能交易" | 已知 | 已加 UI 徽标与 `entry_allowed` 字段 |
| N-6 | soak 脚本只测 worker | 已知 | 需要时按同思路加 Web 长跑 |

### 9.5 复评结论

| 项 | 状态 |
|---|---|
| P0 | 0（上轮已清零） |
| P1 | 12 → **0**（10 上轮关闭 + 2 本轮关闭） |
| 新发现 P1 | 3 个，**全部已修** |
| P2 | 8 项未动 |
| 测试 | 257 例，0 失败 |

**但现在仍然不具备实盘资格**，而且原因和上一轮不同了：

- 上一轮是"代码有洞" → 现在代码层面的洞基本补完
- 这一轮是**"外部未验证 + 业务模型缺失"**：
  无券商凭证（B1）、无 AI Key（B2）、券商协议未验证（B3）、A股通 T+1/涨跌停未建模（B4）

**B2 需要特别强调**：P1-9 修复后，"没有 AI Key"不再是一个可以忽略的配置缺失，
而是**系统完全不交易**的直接原因。这是设计意图，不是故障。

**建议**：在拿到 B1/B2 之前，把工作重心放在 B4（交易规则建模）与 B5（实时行情）上，
这两项不依赖凭证，可以先把代码写好、用模拟数据测通。

---

## 10. 第四轮复评（2026-11-08 14:40）—— B4 + B5 基础能力

> 本节对应 `B4_MARKET_RULES_REPORT.md` 与 `B5_MARKET_PUSH_REPORT.md`。
> 这两项不是"修 bug"，而是**补齐原本完全缺失的能力**，因此审计角度是
> "先审原实现 → 再看新实现是否诚实"。

### 10.1 B4 复评：A 股通交易规则

#### 原实现审计（结论：几乎为空）

| 能力 | 原状态 | 判定 |
|---|---|---|
| T+1 | 无 | **P0 级缺失**（A 股今买今卖属违规） |
| 涨跌停 | 无 | **P0 级缺失**（必然被交易所拒单） |
| 分板块差异 | 无（只有 `lot_default: 100`） | 科创板 200 股起的规则被掩盖 |
| 停牌 | 无 | 停牌标的照常下单 |
| 交易日历 | 只有周末 | 法定节假日照常交易 |
| 最小交易单位校验 | 无（只在定仓时取整） | 可下出 150 股这类非法数量 |
| 规则来源/生效日期 | 无 | 数字写死，无法追溯 |
| 未配置时的行为 | **`return True`（fail-open）** | 不知道就当能交易 —— 已修正为 fail-closed |

#### 新实现的诚实性核查

| 核查点 | 结论 |
|---|---|
| 是否还有写死的统一涨跌幅 | ❌ 没有。9 个板块各自配置 |
| 是否带来源与生效日期 | ✅ 每条 `meta.source` / `effective_from` |
| 是否承认来源不足 | ✅ `source_tier: secondary`，明确写"未取得交易所原文" |
| 规则不明时是否拒绝 | ✅ `UNKNOWN` 一律拒绝；`UNVERIFIED` 由开关决定（默认拒绝） |
| 是否把未建模市场伪装成已校验 | ❌ 没有。`applied=False` + `not_modeled` 明细 |
| 是否处理了 2026-07-06 的规则变更 | ✅ 主板 ST 用 10%，并在 note 里写明"此前为 5%" |

**特别值得记一笔**：如果按"常识"写死，会得到 `ST 股 ±5%` —— 这是 2026-07-06 之前的值。
用配置化 + `effective_from` 才避免了这类"过期知识"造成的系统性错误。

#### 复评意见

机制部分是扎实的（分板块、边界、四舍五入、零股、T+1 可卖数）。
但**默认状态下 A 股不可交易** —— 因为节假日表是 `UNKNOWN`。
这是本轮最需要人工决策的地方：要么录入官方休市日，要么显式接受
"只用周末判断"（把 `calendar.status` 改为 `UNVERIFIED` 并开 `allow_unverified_rules`）。
我选择默认拒绝，因为"编一张假的节假日表"比"暂时不交易"危险得多。

### 10.2 B5 复评：实时行情推送

#### 官方 SDK 审计（新增结论）

`MarketPushAPI` 只有 connect / subscribe / unsubscribe / send_heartbeat / close：

| 缺失 | 后果 |
|---|---|
| 断线重连 | 网络抖动后行情永久中断 |
| 心跳超时检测 | 服务端假死（TCP 未断但不推）时永久卡住 |
| 重连后恢复订阅 | `self._subscriptions` 是**死字段**，从未写入/读取 |
| 去重 / 乱序 / 时间戳校验 | 旧价可覆盖新价，过期行情被当实时 |
| 连接状态机 | 无法区分"连接中/已订阅/假死" |

**这一条是原审计（§2.3）只写了"未接入"、没有深入看实现的地方**。
深入看后确认：即使接入了，也必须自己补可靠性，否则不能用。

#### 新实现的诚实性核查

| 核查点 | 结论 |
|---|---|
| 是否重连 | ✅ 指数退避 0.2→5s，可无限或限次 |
| 是否恢复订阅 | ✅ 重连后重发 `sub`（有测试计数验证） |
| 是否检测心跳超时 | ✅ `wait_for(recv, timeout)`，判据是"任何消息"而非仅 hb |
| 是否去重/排序 | ✅ 按标的时间戳单调；重复与更旧都丢弃并计数 |
| 是否校验时间戳 | ✅ 缺失即丢、过期即丢 |
| 是否冒充真实行情 | ❌ 没有。`source=local-ws-test`，`protocol=unverified` |
| 恢复后是否直接复用旧数据 | ❌ 没有。重连清空 `settled`，必须收到新行情 |
| 行情断了是否只挡开仓 | ✅ 止损不受影响（有测试） |
| 是否修改 SDK | ❌ 没有（sha256 基线测试仍通过） |

#### 复评意见

可靠性设计是对的（尤其是"重连后必须重新 settle"这一点，很多实现会漏）。
但**真实报文结构未核对** —— 这也是为什么 `protocol` 字段直接写 `unverified`。
上线前必须抓一次真实推送核对字段名，否则解析器可能把全部消息记成 `bad_payload`。

### 10.3 本轮新发现的问题

| 编号 | 问题 | 级别 | 状态 |
|---|---|---|---|
| N-7 | `is_market_open` 对未配置市场返回 `True`（fail-open） | P2 | ✅ 已改为 fail-closed + 2 个回归测试 |
| N-8 | SDK 推送层 `_subscriptions` 是死字段 | 记录 | 已在 B5 报告标注，未改上游 |
| N-9 | 涨跌幅若按"常识"写死会错（ST 已从 5% 改 10%） | 记录 | 已用配置化 + `effective_from` 规避 |

### 10.4 测试

```
$ pytest -o addopts="" -v
======================= 308 passed, 1 skipped in 31.49s =======================
junit: tests=309  failures=0  errors=0  skipped=1
```

| 项 | 上轮 | 本轮 |
|---|---|---|
| 用例总数 | 257 | **309**（+52） |
| 新增文件 | — | `test_market_rules_b4.py`(32)、`test_market_feed_b5.py`(20) |
| 本机 WS 服务测试 | 无 | 6 例（连接/心跳/超时/重连/重订阅/settle） |

### 10.5 剩余阻断项（更新）

| # | 阻断项 | 类别 | 说明 |
|---|---|---|---|
| **B1** | 无券商凭证 | 外部 | 券商字段名、协议全部未验证 |
| **B2** | 无 AI Key | 外部 | P1-9 后无 AI 即不交易 |
| **B3** | 券商协议未验证（行情加密、clientId 过滤） | 外部 | 需凭证 |
| **R1** | **A 股节假日表未录入** | B4 | 当前 `UNKNOWN + strict` → A 股不可交易 |
| **R2** | 涨跌幅仅有二级来源 | B4 | 需交易所原文核对 |
| **R3** | ST 标记 / 上市日期缺失 | B4 | 影响 ST 股与新股涨跌幅 |
| **F1** | 无推送网关地址与权限 | B5 | 退化为轮询，止损延迟 |
| **F2** | 真实推送报文未核对 | B5 | `protocol=unverified` |

原 B4/B5 两项从"完全缺失"变成了"机制完整、数据待补"。
剩下的阻断项**没有一项能靠写代码解决** —— 都需要凭证、官方文档或人工录入。

### 10.6 结论

| 项 | 状态 |
|---|---|
| B4 A 股通规则 | ✅ 机制完成（32 例测试）；默认因日历未知而不交易 |
| B5 实时推送 | ✅ 机制完成（20 例测试，含真实本机 WS）；真实协议待核对 |
| P0 / P1 | 0 / 0（保持） |
| P2 | 8 项未动 + N-7 已修 |
| 测试 | 309 例，0 失败 |

**仍然不具备实盘资格**，且本轮之后"缺什么"更清楚了：
不是代码能力，而是 **9 项外部依赖与人工录入**（B1–B3、R1–R3、F1–F2）。

**建议下一步**（按性价比）：
1. 录入 A 股节假日表（半小时的事，解锁 A 股模拟交易）
2. 用官方模拟账户（`SimAccountCreate`）核对券商字段名（有凭证后）
3. 抓一次真实推送报文，核对字段名（有凭证后）
4. 补齐新股的上市日期数据源（解决"前 5 日无涨跌幅"）

---

## 11. 第五轮复评（2026-11-08 19:00）—— 方向调整为"仅美股"

> 对应 `US_MARKET_RULES_REPORT.md` 与 `US_BROKER_CAPABILITY_REPORT.md`。
> 本轮**未修改代码**，只做审计与文档。

### 11.1 方向调整的边界（已确认）

| 项 | 处理 |
|---|---|
| 目标市场 | **仅美股**（NYSE/Nasdaq 上市证券，经星财富 OpenAPI） |
| A 股规则开发 | 暂停（板块配置与测试保留，不删除） |
| 港股规则开发 | 暂停 |
| A 股 T+1 / ST / 北交所 / 港股每手 | **移出阻断项**（降级为"已实现但不使用"） |
| 通用引擎 | 不受影响（引擎/风控/审计/推送与市场无关） |
| 测试 | 309 例保留，308 通过 / 1 跳过（本轮实测） |

### 11.2 本轮新发现的问题

| 编号 | 问题 | 级别 | 证据 |
|---|---|---|---|
| **US-1** | 美股时段硬编码北京窗口（21:30–04:00），**无 DST** → 冬令时早开 1 小时、漏掉尾盘 1 小时 | **P0** | 探针：周一 21:45（08:45 ET 盘前）判"开"；周二 04:30（15:30 ET）判"关" |
| **US-1b** | 周末判定用**北京日期** → 周五夜盘被判"周末休市"、周日深夜被判"开市" | **P0** | 探针：周六 00:30（周五 11:30 ET）判"关"；周一 00:30（周日 11:30 ET）判"开" |
| **US-2** | `enforce_markets: ["cn"]` → **目标市场美股完全不受规则层约束**（次美分、碎单、当日回转都放行） | **P0** | 探针：`check_order(usAAPL, 200.005)` → `applied=False, ok=True` |
| **US-3** | 盘前/盘后/夜盘未建模（但 SDK 有 `pqt/pmin/ptk`、`qt_nt`） | P1 | 代码检索 `premarket/afterhours` 零命中 |
| **US-4** | 订单类型只有限价/市价，`time_in_force`/`exp_type`/`short_sell_type`/`trig_price` 全未使用 | P1 | `fosun_adapter.place_order` 只传 8 个参数 |
| **US-5** | `get_positions` 未传 `use_us_pre/post/night` → 盘前盘后持仓可能缺失 | **P0** | `fosun_adapter.py::get_positions` 只传 4 个参数 |
| **US-6** | `order_type=3` 被当作"市价"，但 SDK/README **从未定义该枚举** | P1 | README 仅 `1 # 限价`；3 无注释 |

**US-2 值得单独说明**：上一轮（B4）把 A 股做成"规则不明确即拒绝"，这本身是对的；
但目标市场一换成美股，这套规则就变成了**"严格管住不做的事，放任要做的事"**。
这是方向调整后最需要立刻纠正的一处。

### 11.3 官方来源进展（比 B4 轮显著改善）

B4 轮只拿到"文件存在+发布日"；本轮取得了 **4 条法规条款原文**：

| # | 规则 | 来源 | 条款 |
|---|---|---|---|
| 1 | 最小报价单位 | 17 CFR **242.612** | (b)(2)(i)(ii) $0.01/$0.005、(b)(3) $0.0001、(c) 新股 $0.01 |
| 2 | 卖空 10% 触发 SSR | 17 CFR **242.201** | (b)(1)(i)(ii) 当日剩余+次日禁止 ≤NBB 卖空 |
| 3 | 结算周期 | 17 CFR **240.15c6-1** | (a) "later than the first business day" → **T+1** |
| 4 | 熔断阈值 | NasdaqTrader（Rule 2013 plan） | 7%/13% 暂停 15 分钟、20% 当日停牌、3:25 p.m. 规则 |
| 5 | 交易时段/节假日/提前收市 | NYSE `hours-calendars` | 9:30–16:00 ET；2026 年 10 个休市日；11/27、12/24 收 13:00 |

**仍然 UNVERIFIED**（本轮未取到）：DST 法定依据、LULD 价格带、订单类型/TIF 枚举、Reg SHO Rule 203 locate、PDT、Nasdaq 盘前盘后起止、行情延迟等级。

### 11.4 一个值得记住的规则细节

`242.612` 在 2024 年被修订过，现在的 tick size **不是固定 0.01**：

* ≥ $1.00 → **$0.01**
* ≥ $1.00 且该股评估期 TWAS ≤ $0.015 → **$0.005**
* < $1.00 → **$0.0001**
* 新 NMS 股票 → $0.01

如果在代码里写死 `0.01`，对低价股会**过严 100 倍**。
这条规则同时说明："最小报价单位"是**逐股、半年一调**的，必须靠数据源，不能靠常量。

### 11.5 阻断项清单（换成美股口径）

| # | 阻断项 | 类别 | 说明 |
|---|---|---|---|
| **US-B1** | 无券商凭证 | 外部 | 账户/持仓/行情/下单字段全部未验证 |
| **US-B2** | 无 AI Key | 外部 | P1-9 后无 AI 即不交易（沿用） |
| **US-B3** | 时段与 DST 未修 | **代码** | 现在会打在盘前、漏尾盘 |
| **US-B4** | 规则层未覆盖美股 | **代码** | tick size / SSR / 停牌全空 |
| **US-B5** | 盘前盘后持仓未取 | **代码** | 可能导致风控看不见仓位 |
| **US-B6** | 订单类型/TIF 枚举未知 | 需官方文档 | 可能把市价单下成限价单 |
| **US-B7** | LULD / 停牌数据源 | 数据 | 无法判断个股是否可交易 |
| **US-B8** | TWAS 数据（逐股 tick size） | 数据 | 影响 tick 校验 |
| **US-B9** | 结算/可用资金模型 | 代码 + 券商字段 | 影响资金占用判断 |

原 A 股相关阻断（R1 节假日表、R2 交易所原文、R3 ST/上市日期）**全部移出**。

### 11.6 结论

1. **方向调整在文档层面已完成**：A/港股开发暂停、代码与测试完整保留、通用引擎不受影响。
2. **美股现状是"能下单但不能安全交易"**：时段错 1 小时、规则层不覆盖、盘前盘后不建模、
   订单类型只有两种。
3. **官方依据比上一轮好**：拿到了 4 条法规原文 + 2 个交易所页面，足以支撑
   "常规时段/节假日/熔断/tick size/SSR/T+1"这 6 条规则的落地。
4. **建议立刻做的三件事**（都不依赖凭证）：
   US-T1/T2 时段与 DST、US-T3 规则生效市场改为 `us`、US-G1 持仓补 `use_us_pre/post/night`。
5. 其余全部依赖凭证或数据源，已在两份报告的"阻断项"里逐条列明。

**本轮不修改代码，等待审核后再按 `US_MARKET_RULES_REPORT.md §5` 的顺序推进。**

---

## 12. 第六轮复评（2026-11-08 19:12）—— US-P0 修复验收

**性质**：修复后复核。承接 §11 的 US-B3 / US-B4 / US-B5 三项代码阻断。
**交付**：`US_P0_IMPLEMENTATION_REPORT.md`、`US_P0_TEST_REPORT.md`。
**实测环境**：Python 3.13.14 / Windows / `tzdata 2026.6`。

### 12.1 结论摘要

| 项 | §11 时的状态 | 现状 | 判定依据 |
|---|---|---|---|
| US-B3 时段与 DST | ❌ 未修（会打在盘前、漏尾盘） | ✅ **已修** | 北京 22:29/22:30 判定翻转、04:30 由"休市"变"开市"、北京周六/周一归属修正 |
| US-B4 规则层未覆盖美股 | ❌ `enforce_markets: ["cn"]` | ✅ **已修** | `["cn","us"]`；tick size 三档；`applied=False` 增加 reject 策略 |
| US-B5 盘前盘后持仓未取 | ❌ 只传 4 个参数 | ✅ **已修** | `useUsPre/Post/Night` 全 True，health 可审计，可覆盖并强制告警 |
| US-B6 订单类型/TIF 枚举未知 | ❌ | ❌ **仍未解决**（需官方文档） | 已写入 `not_modeled` |
| US-B7 LULD / 停牌数据源 | ❌ | ❌ **仍未解决**（需数据） | 同上 |
| US-B8 TWAS 逐股 tick 数据 | ❌ | ❌ **仍未解决**（需数据） | `tick_size_half_cent` 留空，不假装支持 |
| US-B9 结算/可用资金模型 | ❌ | ❌ **仍未解决**（需券商字段） | `settlement_t1_funds` 在 `not_modeled` |

**新增发现并修复的 4 个风险处置缺陷**（§11 未列，本轮自查发现）：

| # | 缺陷 | 危害 | 修复 |
|---|---|---|---|
| **US-N1** | 单日亏损熔断对减仓同样生效 | 亏到线后连止损都发不出（"不许跑"） | `is_exit` 豁免该检查 |
| **US-N2** | 市场开关对减仓生效 | 配置里关掉某市场 → 已有持仓再也平不掉 | 减仓放行 + warning |
| **US-N3** | 规则层时段/日历不区分方向（= §10.2 C4） | 休市/节假日时止损被规则层拦死 | `is_exit` 只留痕不拦截 |
| **US-N4** | `monitor()` 行情失败即 `continue` | 该标的不再有止损，且无任何告警 | 退化用券商持仓 mark + 审计留痕 |

### 12.2 US-P0-1 复评：时区判定

**原始缺陷实证**（`audit_evidence/us_p0_probe_output.txt`）：

```
北京 21:30 → 纽约 08:30 EST   旧=开市  新=休市   ← 早开 1 小时
北京 22:00 → 纽约 09:00 EST   旧=开市  新=休市
北京 22:29 → 纽约 09:29 EST   旧=开市  新=休市
北京 04:30 → 纽约 15:30 EST   旧=休市  新=开市   ← 漏掉尾盘 30 分钟
不一致时点数：4（共 12 个采样点）
```

```
北京 2026-11-07(周六) 02:00 → 纽约 2026-11-06 Fri 13:00   旧=休市  新=开市
北京 2026-11-09(周一) 02:00 → 纽约 2026-11-08 Sun 13:00   旧=开市  新=休市
```

**实现审查**（`src/ai_trader/market/clock.py`，新增 369 行）：

| 检查点 | 结论 |
|---|---|
| 是否用 `zoneinfo` 而非手写 DST 规则 | ✅ `load_tz()` → `ZoneInfo("America/New_York")` |
| 时区数据缺失时是否 fail-closed | ✅ 抛 `ClockError` → `clock_errors[mkt]` → `session_status` 恒 `False` |
| 未配置常规窗口时是否 fail-open | ✅ `configured=False` → `phase=closed`，**不用**默认 09:30–16:00 顶替 |
| 边界语义 | ✅ `[open, close)`：09:30 开、16:00 收 |
| 提前收市 | ✅ `early_close` 覆盖 `close_time()`，盘后从提前收盘起算，实测 13:00 正确切换 |
| 跨午夜夜盘 | ✅ `night.open >= night.close` 时用"或"判断，实测 02:00 判为 `night` |
| 是否仍有第二份时段表 | ✅ 已删 `risk/controller.py` 的北京 `SESSIONS` 内容，`is_market_open` 走同一 `build_clock` |
| naive datetime 语义 | ✅ 视为本机时区（与 `datetime.now()` 一致），aware 输入按其时区换算 |

**日历诚实性**：`calendar.markets.us` 标 `VERIFIED`（NYSE 官方页）并带
`covered_until: 2026-12-31`；超出覆盖范围（如 2027-01-05）**拒绝**，
有测试 `test_us_holiday_and_calendar_coverage` 断言这一点。
`calendar.status`（顶层）仍为 `UNKNOWN`，cn 沿用 strict 拒绝，**A 股通行为未变**。

### 12.3 US-P0-2 复评：规则真正生效

| 检查点 | 结论 |
|---|---|
| `enforce_markets` 是否含 `us` | ✅ `["cn","us"]`（`check-env` 实测） |
| 价格精度是否真校验 | ✅ `CODE_PRICE_TICK` + `Decimal` 整倍数判定；150.003 被拒、150.00 通过 |
| `< $1` 是否用 $0.0001 | ✅ `tick_size_below_1=0.0001`；0.5001 通过、0.50005 被拒 |
| $0.005 档是否假装支持 | ✅ **未假装**：`tick_size_half_cent: []`，需 TWAS 数据 |
| 数量检查 | ✅ `lot_size=1`，`qty<=0` 触发 `LOT_SIZE` |
| 时段检查 | ✅ `session_status` 直接拦截，`test_us_rules_really_enforced` 断言 |
| `applied=False` 是否等于放行 | ⚠️ **默认仍是 warn**；已提供 `reject` 并有用例覆盖。美股不在该分支（`applied=True`） |
| US 板块规则状态是否诚实 | ✅ `VERIFIED` + `source_tier=primary` + 明确来源（NYSE 页 / 17 CFR 242.612 / 240.15c6-1）；未建模项 6 条全列在 `not_modeled` |

**残留 P1**：`unenforced_market_policy` 仍为 `warn`。港股因未纳入强制校验，
规则层只标注不拒绝。改 `reject` 会破 15 个既有港股用例（属"影响非目标市场"），
故本轮不动，建议正式只交易美股时切换（模式已实现且有用例）。

### 12.4 US-P0-3 复评：全时段持仓

| 检查点 | 结论 |
|---|---|
| 是否真的传了三个开关 | ✅ 实测 `use_us_pre/use_us_post/use_us_night = True` 送达 `get_holdings` |
| 默认值方向 | ✅ 默认全 True（多看持仓=多一道风控），不是"少看=省事" |
| 关掉任一项是否可发现 | ✅ `health().position_sessions.warning` 非空 |
| 各时段持仓是否都进风控 | ✅ `Position.session` 保留（含空值），`position_count` / `sellable_qty` / `scan_exits` 均覆盖 |
| 查询失败是否返回空 | ✅ 结构不符/缺字段抛 `PARSE_MISMATCH`；`scan()` 捕获**任何** Exception 并整轮停手 |
| "不知道时段"是否会被排除 | ✅ 不会 —— 空 `session` 同样进入风控 |

### 12.5 US-P0-4 复评：风险处置路径

| 路径 | 修复前 | 修复后 | 用例 |
|---|---|---|---|
| 止损（正常） | 可用 | 可用 | `test_all_session_positions_enter_risk_control[*]` |
| 止损（触及日亏上限） | ❌ 被拦 | ✅ 放行 | `test_daily_loss_does_not_block_exit` |
| 止损（dayPnl 缺失） | ❌ 被拦 | ✅ 放行 | `test_unknown_day_pnl_does_not_block_exit` |
| 止损（市场被停用） | ❌ 被拦 | ✅ 放行 + warning | `test_exit_allowed_when_market_disabled` |
| 止损（休市/节假日） | ❌ 规则层被拦 | ✅ 只留痕不拦 | `test_us_exit_not_blocked_by_session_or_calendar` |
| 止损（行情接口失败） | ❌ 静默跳过 | ✅ 退化到券商持仓 mark | `test_quote_failure_uses_position_mark_for_stop` |
| 止损（持仓接口失败） | ❌ 整轮崩溃 | ✅ 快照/本地登记兜底 | `test_positions_query_exception_still_allows_stop_loss` |
| 撤单（超时/未知单） | 可用 | 可用（未改动） | `test_cancel_chain.py`（10 项） |
| 未知单对账 | 可用 | 可用（未改动） | `test_order_state_integrity.py`（14 项） |
| 查询失败是否假定"没这张单" | 否 | 否（保持封锁） | `test_order_state_integrity.py` |

### 12.6 测试与变异检验

```
$ python -m pytest tests/ -o addopts="" -q
346 passed, 1 skipped, 1 warning in 35.13s

$ python -m pytest tests/test_us_p0.py -o addopts="" -q
38 passed in 0.75s
```

变异检验（内存注入错误实现，不改产品代码）：

| 变异 | 结果 |
|---|---|
| `us_beijing_hardcoded_window` | 10 failed / 28 passed |
| `us_clock_ignores_timezone` | 6 failed / 32 passed |
| `us_positions_drop_session_flags` | 1 failed / 37 passed |
| `us_exit_blocked_by_session` | 2 failed / 36 passed |

**4/4 被抓**，说明新测试与真实行为耦合，不是空转。

### 12.7 合规性复核（对照历次硬性要求）

| 要求 | 结论 |
|---|---|
| 不修改第三方 SDK | ✅ 26 个文件 sha256 与 `sdk_baseline.json` 完全一致 |
| 不覆盖用户数据/数据库/密钥/凭证 | ✅ 未写 `data/`、`.env`、`vault.json`；备份目录也不含这些 |
| 安全开关保持 | ✅ `LIVE_TRADING=false / ORDER_EXECUTION_ENABLED=false / PAPER_TRADING=true` |
| 不连接真实账户、不发真实委托 | ✅ `configured=false`、`connected=false`、`can_send_real_order=false` |
| 不伪造券商订单号 | ✅ `OrderStateUnknown` 路径未改动，`unknown` 状态保留 |
| 不 mock 掩盖时区问题 | ✅ 真实 `zoneinfo` + 真实 `tzdata 2026.6`；测试一律用 aware datetime，结果与本机时区无关 |
| 不 mock 掩盖数据库问题 | ✅ 沿用真实 SQLite（`AuditJournal` 落盘） |
| 不 mock 掩盖并发问题 | ✅ 未改动并发相关代码；既有 7 项并发用例全绿 |
| 无官方依据不得猜测 | ✅ LULD/MWCB/SSR/TIF/PDT 全部留在 `not_modeled`，不写假规则 |
| 保留 A 股/港股代码与测试 | ✅ 9 个板块规则、32 项 B4 用例、20 项 B5 用例全部保留并通过 |
| 修改前备份可回滚 | ✅ `backups/us_p0_20261108-183854`（70 文件 + MANIFEST） |

### 12.8 是否放行

**US-P0-1 ~ US-P0-4 验收通过。**

理由：三项代码阻断（US-B3/B4/B5）已消除并有实测证据；4 个新发现的风险处置缺陷
（US-N1~N4）已修复且被测试锁定；全部 347 项测试通过；4/4 变异被抓住；
SDK 零改动；安全开关保持关闭。

**但仍不能进入美股实盘**，剩余阻断与 §11 一致且均为外部依赖：

| # | 阻断项 | 本轮变化 |
|---|---|---|
| US-B1 | 无券商凭证（账户/持仓/行情/下单字段未验证） | 未变（外部） |
| US-B2 | 无 AI Key | 未变（外部） |
| US-B6 | 订单类型 / TIF 枚举未知 | 未变（需官方文档） |
| US-B7 | LULD / 个股停牌数据源 | 未变（需数据） |
| US-B8 | TWAS 逐股 tick 数据 | 未变（需数据） |
| US-B9 | 结算 / 可用资金模型 | 未变（需券商字段） |
| **US-B10** | `unenforced_market_policy` 仍为 `warn` | **新增**（建议切 `reject`） |
| **US-B11** | `halt_storage` 异常仍拦减仓 | **新增**（与 US-P0-4 存在张力，需人工决策） |

**下一步建议顺序**：取得凭证 → 用真实接口核对 `get_holdings` 三个开关与返回字段
→ 补 US-B6 枚举 → 切 `unenforced_market_policy: reject` → 再谈实盘。

**本轮修改代码，已全部测试通过，等待独立复核。**

---

## 13. 第七轮：US-P1 第一批（交易安全补强）

**时间**：2026-11-08 20:19 – 2026-11-09 12:55
**性质**：**本轮修改代码/配置/测试**（前六轮为只读或含修复）。
**依据**：§12.8 列出的 N-1 ~ N-4 与配置守卫。
**交付**：`US_P1_BATCH1_IMPLEMENTATION_REPORT.md`、`US_P1_BATCH1_TEST_REPORT.md`。

### 13.1 结论摘要

| 项 | §12 时的状态 | 现状 | 关键证据 |
|---|---|---|---|
| **N-1** 数据库故障致 monitor 崩溃 | ❌ 3 个方法抛错即整体退出 | ✅ **已修** | 6 个方法同时抛错仍发出止损；故障独立落盘 |
| **N-2** 未知单持续阻断退出 | ❌ 不区分方向 | ✅ **已修（券商侧限制未解决）** | 未知买单 + 止损 → reduce-only 放行 + 授权审计 |
| **N-3** 急停读写语义混淆 | ❌ 单一 `failed` 布尔 | ✅ **已修** | `kind` 分类 + 读写分离；读失败全阻断、写失败默认阻断 |
| **N-4** 未来时间戳永久卡死冷却 | ❌ 只判下界 | ✅ **已修** | 上下界 + 异常状态告警；冷却未被删除 |
| **配置守卫** | ❌ 三个一键旁路无守卫 | ✅ **已修** | `guard` 子命令 + worker 首步 + `/api/scan`；6 类违规退出码 4 |
| 回归 | 347 项全绿 | ✅ **388 项全绿**（387 passed / 1 skipped） | 无删除、无跳过 |

### 13.2 N-1：数据库故障隔离

**新增 `src/ai_trader/audit/faults.py`**：`data/faults.jsonl`（追加 + fsync）、
`data/FAULT.json`（原子写哨兵）、`logs/alerts.log`（`ALERT severity=… kind=…`）。
三者**完全不依赖 SQLite**；自身失败时退化到 stderr + 进程内计数。

**`StrategyEngine` 新增 7 个包装器**（`_jlog/_jlist_orders/_jfind_orders/_jorders_since/
`_jrecord_order/_jrecord_risk_decision/_jupdate_status`），全部"不抛异常 + 失败即记录"。
`_jlist_orders` 等返回 `(rows, ok)`，**ok=False 表示读不出来，不是"没有订单"**。

**`monitor()` 五阶段隔离**：`refresh_pending` / `expire_orders` / `reconcile_unknown` /
`positions` / `exit_pass` 彼此独立，任一失败只记 `phase_failed` 并继续。

**风险退出策略**（三条通道分别定级）：

| 通道 | 开仓 | 减仓 |
|---|---|---|
| 组合快照（`orders_since`）失败 | 拒绝 | 放行（降级组合） |
| 订单历史（`list_orders`）失败 | 拒绝（`orders_history`） | 放行 + reduce-only 四约束 |
| 撤单/对账（`find_orders`）失败 | 拒绝 | 放行（该阶段跳过并记录） |

**明确不做**：捕获异常后继续普通开仓。所有开仓路径在
`orders_known=False` / `positions_stale` / 守卫失败时均被拒绝。

**实测**（`audit_evidence/us_p1_b1_fault_demo.txt`）：

```
monitor 返回: ['phase_unavailable', 'phase_unavailable', 'exit']
券商收到委托: [{'symbol': 'usAAPL', 'side': 'sell', 'qty': 10.0, …}]
故障计数: {'order_read_failed': 5, 'audit_write_failed': 3}
FAULT.json: latest.severity = critical ("订单已提交但落库失败 —— 必须人工核对券商侧订单")
```

### 13.3 N-2：未知单方向分级 + reduce-only

`OrderIntent.exit_mode`（stop_loss / take_profit / signal_exit / manual）+
`is_reduce_only`。方向规则：

| 未知单 | 本次 | 结果 |
|---|---|---|
| buy | sell(exit) | 允许，须过四约束 |
| sell | sell | 阻断（防超卖） |
| 任意 | buy | 阻断 |

**四约束**（`_reduce_only_gate`）：sell-only / `positions_verified=True` /
held>0 / `qty ≤ held`。放行时写 `exit_authorized_reduce_only` 审计
（含 held、qty、exit_mode、触发的检查项）。

**券商侧 reduce-only 原语：不存在。** 复核确认 `fsopenapi/api/trade.py` 与
`api/ops/trade.py` 的 `create_order` 均无 `reduceOnly / close / positionSide` 参数，
因此"只减不增"仅为**客户端**保证。按用户要求**明确标注为部分未解决**，未假装安全。

### 13.4 N-3：急停哨兵读写分离

`HaltStoreError` 增加 `kind`（`corrupt/permission/no_space/atomic_failed/dir_failed/
write_failed/read_failed/missing/unknown`）与 `phase`；`HaltStore` 分离
`read_failed` / `write_failed`（互不清除）。

`SafetyState` 增加 `halt_read_failed / halt_write_failed / halt_error_kind /
halt_error_phase` + `mark/clear_halt_storage_failure()`；`halt_store_failed` 保留为聚合位。

| 故障 | 默认处置 |
|---|---|
| 读取失败 | **全阻断（含减仓）** —— 不知道是否被人为停止，不得绕过人工停止 |
| 写入失败 | **全阻断** |
| 写入失败 + 显式 `halt_write_failure_exit_mode=reduce_only` | 允许受限减仓（须过四约束） |

**本轮没有对所有 `is_exit` 开放权限**：开关默认 `block`，通道虽已实现并被用例覆盖，
但需人工书面审批后才开启。

### 13.5 N-4：时间戳与冷却

`_duplicate_check_detail()` 重写：

1. 时间窗 **有上下界**（`cutoff ≤ ts ≤ now + max_clock_skew_seconds`，默认 300s）；
2. 未来时间戳 → `order_timestamp_anomaly` 检查项 + `KIND_TIMESTAMP` 故障流水，
   **不计入冷却**；
3. 无时区 / 无法解析 → 留痕（不再静默 `continue`）；
4. 未完成/未知同向委托 → **永远阻断**（状态型检查，与时钟无关）；
5. 减仓不适用时间冷却，`_reserve_or_block` 跳过减仓。

**冷却未被删除**：`test_recent_timestamp_still_blocks_entry` 断言正常时间戳仍拦重复开仓。

附带修复同类问题：`min_notional` 与 `rate_limit` 对减仓豁免（原实现会因
"1 股 × $50 < 100" 或"6 笔/分"挡住止损）。

### 13.6 配置守卫

新增 `src/ai_trader/guards.py`（239 行），13 个违规码，`required_markets` 默认 `["us"]`。

接线：`ai-trader guard`（exit 4）／`scan`+`monitor` 前置／`check-env` 摘要／
`run_worker.ps1` 第 0 步（exit 4，不进入 monitor/scan）／Web `/api/scan`（409）。

配置变更：`enforce: true` 显式化、`required_markets: ["us"]`、
`unenforced_market_policy: "reject"`、`max_clock_skew_seconds: 300`、
`halt_write_failure_exit_mode: "block"`。

模板：`config.template.yaml` 由 `config.yaml` 逐字同步（生成前有凭证安全闸），
常驻断言 `test_template_matches_production_semantics`（YAML 语义逐键相等）。

实测退出码：

```
正常 exit=0 │ enabled=false exit=4 │ enforce=false exit=4
us 不在 enforce exit=4 │ policy=warn exit=4 │ 缺 sessions.us exit=4
```

### 13.7 对既有测试的改动

| 文件 | 改动 | 判定 |
|---|---|---|
| `tests/conftest.py` | 固定合成行情参考日（修 10 个日期依赖用例）；夹具显式 `warn` | **缺陷修复**，非削弱 |
| `tests/helpers.py` | 同上 | 同上 |
| `tests/test_halt_persistence.py` | 断言 `halt_storage_write` + `halt_read_failed is False` | **增强** |
| `tests/test_process_restart.py` | 改用完整生产配置构造 | **增强** |
| `tests/test_web.py` | 夹具切 `reject` | **增强** |

**A 股 / 港股代码与规则一项未删**；相关 60+ 用例全部保留并通过。

`unenforced_market_policy=reject` 对港股的收紧（`MARKET_NOT_ENFORCED`）是刻意的，
对美股零影响（`test_us_market_still_fully_enforced` 断言）；
`test_reject_policy_really_rejects_unmodelled_market` 断言港股确实被拒。

### 13.8 新增发现：测试套件日期依赖（既有缺陷）

用本轮修改前的代码跑全量：**10 failed / 336 passed**。
根因 `SyntheticQuoteSource` 以 `date.today()` 播种，导致：

* 合成"空头排列"日 → `HeuristicModel` 返回 hold → 4 项 AI 权威用例失败；
* 价格偏离 `FixedSignalModel(entry=300)` >5% → 风控按"错价"拒单 → 4 项失败；
* 并发/推送用例 2 项受影响。

即**这套测试只在特定日历日通过**。本轮用 `reference_day` 固定种子修复
（生产/演示仍按当天变化）。

### 13.9 残留风险（未解决项，如实登记）

| # | 项 | 级别 | 状态 |
|---|---|---|---|
| R-1 | 券商侧无 reduce-only 原语 | **未解决（外部）** | SDK `create_order` 无相关参数；本通道仅客户端约束 |
| R-2 | `orders_known=False` 时减仓仍可能重复卖 | P1 | 只能保证 qty ≤ 已核实持仓，无法排除在途卖单 |
| R-3 | `halt_write_failure_exit_mode=reduce_only` 未启用 | **待人工决策** | 通道已实现+有用例，默认 `block` |
| R-4 | 进程内冷却记录的 `now` 由调用方提供 | P2 | 有 `ts > now+skew` 告警兜底 |
| R-5 | 故障通道自身写不进时只剩 stderr | P3 | `snapshot().persist_failed` 标记 |
| R-6 | 夜盘 / 订单类型 / TIF / LULD 等 | 沿用 US-P0 | 仍在 `not_modeled` |

### 13.10 合规核对

| 要求 | 结论 |
|---|---|
| 不修改官方 SDK | ✅ 26 文件 SHA256 与 `sdk_baseline_us_p1_b1.json` 完全一致 |
| 不覆盖 data / .env / vault | ✅ `audit.db` mtime 仍为 11-08 13:00；`.env` 仍 11-57；`vault.json` 不存在 |
| 不初始化 Git / 不清理历史备份 | ✅ 仍无 `.git`；`us_p0_*` 与 `us_p1_b1_*` 并存 |
| 三开关保持 | ✅ `false / false / true`，`can_send_real_order=(False,…)` |
| 不连接真实账户 / 不下单 | ✅ 所有券商交互为进程内契约替身 |
| 不删除旧市场实现 | ✅ A 股 32 项 + 港股 60+ 项用例全部保留通过 |
| 真实 SQLite / 文件系统 / 时区库 | ✅ 未用 mock 顶替；故障注入只让单个方法抛错 |
| 无法安全实现的不假装完成 | ✅ R-1/R-2 明确标注"部分未解决" |

### 13.11 是否放行

**US-P1 第一批验收通过（5/5 项完成，1 项部分完成并已标注）。**

```
387 passed / 1 skipped / 0 failed
变异检验 4/4 被抓住
SDK 零改动；安全开关关闭；未触碰用户数据
```

**仍不允许开实盘**，阻断项更新为：

| # | 阻断项 | 变化 |
|---|---|---|
| US-B1 | 无券商凭证（真实接口字段未验证） | 未变 |
| US-B2 | 无 AI Key | 未变 |
| US-B6 | 订单类型 / TIF 枚举未知 | 未变 |
| US-B7 | LULD / 停牌数据源 | 未变 |
| US-B9 | 结算 / 可用资金模型 | 未变 |
| **US-B12** | 券商侧无 reduce-only 原语（新） | **新增**，需券商支持或改用"先查后卖"二次确认 |
| **US-B13** | `halt_write_failure_exit_mode` 待人工决策（新） | **新增** |

**建议下一批（US-P1 第二批）**：券商凭证核实 → US-B6 枚举 → US-B12 的
"卖前二次查持仓/查在途单"方案 → 再评估实盘。

**本轮修改代码，已全部测试通过，等待独立只读复核。**

---

## 14. 第八轮：US-P1 Batch 2（订单不确定性与减仓幂等性）

**时间**：2026-11-09 15:15 – 16:05
**性质**：**修改代码/测试**（承接 §13.9 的 R-1 / R-2 / R-7 / R-4）。
**交付**：`US_P1_BATCH2_IMPLEMENTATION_REPORT.md`、`US_P1_BATCH2_TEST_REPORT.md`、
`US_P1_BATCH2_REDUCE_ONLY_DESIGN.md`。

### 14.1 结论摘要

| 项 | §13 时的状态 | 现状 | 关键证据 |
|---|---|---|---|
| **R-1** 下单后落库失败 → 订单丢失 | ❌ 重启后不可见、可重复下单 | ✅ **已修** | 五步复现全部反转：意图保留、重启可见、对账被触发、+400s 开仓被阻断 |
| **R-2** 库不可读时重复卖出 | ❌ 可超卖 | ✅ **已修** | 6 进程抢额度 1 成功/5 拒；库不可读时 0 笔新卖单 |
| **R-7** 未知买单成交后止损被清除 | ❌ 保护丢失 | ✅ **已修** | 两段式解除 + 按实际持仓重算 `protected_qty` |
| **R-4** 异常时间戳致冷却失效 | ❌ 开仓 fail-open | ✅ **已修** | 空/缺失/无时区/不可解析 → 开仓拒绝；减仓放行 |
| **R-3 / R-8** reduce-only | ❌ 纸面功能、无审批 | ⏸ **仍不启用** | 可达性复核确认为死分支；设计与测试方案已提交待批 |
| 回归 | 388 全绿 | ✅ **424（423 passed / 1 skipped）** | 无失败、无跳过（跳过项为无 AI Key） |

### 14.2 R-1：write-ahead 订单意图

**新增 `order_intents` 表**（22 列，`client_id` UNIQUE，纯 `CREATE TABLE IF NOT EXISTS` 增量）：

* 发单**之前**写入：`client_id / 标的 / 方向 / 数量 / 价格 / 请求摘要(sha256) / 请求原文 / created_at / state=intent`
* 状态机：`intent → accepted/submitted → terminal`；结果不明 → `unknown`（**不释放**）
* 重启：`recover_open_intents()` 把一切未终结提升为 `unknown`
* 对账：统一按 `client_id`（不依赖 `order_id`）；对账失败保持 `unknown` 并持续阻断
* 预写失败 → **不调用券商**；意图表不可用 → `orders_known=False` 进一步禁开仓
* 同一 `client_id` 重复预写 → `duplicate` 直接阻断（防多 worker 重复发单）

**修复前后对照**（真实链路）：

```
修复前: orders 0 行 → 重启 find_orders(unknown)=[] → +400s 又下一单
修复后: 意图 client_id/unknown 保留 → 重启恢复 → 对账按 client_id 尝试
        → +400s rejected（unresolved_order_state），券商收到 0 笔
        故障流水含 client_id ✅（修复前不含）
```

### 14.3 R-2：可卖余量 = 持仓 − 未完成卖单预留

* 预留来源：`order_intents` 中 `side='sell' AND state IN (intent,accepted,submitted,unknown)`
  —— **unknown 计入**，绝不自动释放
* `Portfolio.open_sell_reserved / reservations_known / intents_available` + `sellable_now()`
* 风控新增 **`sell_capacity`** 检查，对**所有卖出**生效（含止损）
* 预留量读不出来 → `reduction_blocked_unverified`（不再"当成 0 放行"）
* 下单前二次原子校验：`prewrite_intent(..., sell_capacity=held)` 在
  `BEGIN IMMEDIATE` 内做 `SUM(未完成卖)+本单 ≤ 持仓`

**实测**：持仓10/预留10/卖10 拒；未知卖6/卖5 拒；两笔未知卖 4+6/卖1 拒；
`unknown` 不释放；cancelled/filled 才释放；**6 个真实进程并发 → 1 成功 / 5 拒**。

**行为反转（需注意）**：`orders_known=False` 从 batch1 的"放行减仓"改为
**禁止一切新增卖出**（任务 §3.4 要求）。代价是 DB 故障期间无法止损 →
登记为残留风险 S-1，需靠故障告警 + 人工介入。

### 14.4 R-7：止损保护绑定实际持仓

* `register_stops` 增加 `version / protected_qty / exit_pending`
* 退出成交后**不再立即清除**，只标 `exit_pending`
* 每轮 `monitor` 执行 `_reconcile_stops(positions)`：
  未核实 → 保留；持仓>0 → 保留并重算数量；持仓=0 且无未终结买单 → 才解除；
  意图表不可读 → 视为有未终结买单 → 不解除
* 实测：未知买单成交（持仓回到 10）→ `stops_retained`，`protected_qty=10`

### 14.5 R-4：异常时间戳

`_duplicate_check_detail` 在 `problem 且 not is_exit` 时**直接返回阻断理由**
（此前是 `continue`，等价于没有冷却记录）。减仓仍放行，但受 §14.3 的预留/幂等约束。
实测：`ts="???"` 的历史行会让生产链路 `analyze_symbol` 被拒且 0 委托。

### 14.6 R-3 / R-8：reduce-only 复核（不启用）

**可达性**：`halt()` 先置 `kill_switch=True`（第 9 行）再落盘（第 17 行）；
`kill_switch` 检查不分方向 → 写失败后 reduce-only 分支**不可达**。

```
kill_switch=True | write_failed=True | kind=dir_failed
减仓 approved=False  拦截项=['kill_switch']
```

**治理缺口**：无审批字段、无审计事件、无撤销流程、无到期机制。
**结论**：维持 `block`；建议方案 A（维持禁用 + 文档标注），
启用前须补"kill_switch 方向化 + 审批审计 + 到期撤销"三项。
设计与测试方案见 `US_P1_BATCH2_REDUCE_ONLY_DESIGN.md`。

### 14.7 schema 与迁移

`order_intents`（22 列）+ 2 个索引，纯增量。在**真实库副本**上实测：

```
迁移前后各表行数完全一致（signals=36 / risk_decisions=8 / audit_log=68）
真实库 data/audit.db 未被打开写，mtime 仍 2026-11-08 13:00
```

旧版本代码读取新库：多余表被忽略，功能不变（前向兼容）。

### 14.8 合规核对

| 要求 | 结论 |
|---|---|
| 不修改官方 SDK | ✅ 26 文件 SHA256 0 不一致 |
| 不重置/覆盖数据库 | ✅ 真实库未打开写；仅在其**副本**上验证迁移 |
| 不连真实券商 / 不下真实单 | ✅ 全部为进程内 `Broker` 契约子类 |
| 不为过测试放宽风控 | ✅ 本批只收紧（新增 `sell_capacity`、时间戳 fail-closed、预写门禁） |
| 不把 unknown/异常/落库失败当"不存在" | ✅ 全部显式建模并占用预留 |
| 每项有生产路径测试 | ✅ 36 个新用例全部驱动 `engine.scan/monitor` 或 `risk.evaluate` |
| 测试无 mock | ✅ `test_us_p1_batch2.py` 中 0 处 monkeypatch/mock |
| 三开关 | ✅ `false / false / true` |

### 14.9 残留风险

| # | 项 | 级别 |
|---|---|---|
| S-1 | DB 故障期间无法止损（R-2 要求的直接后果） | 高（设计取舍，需运维快速恢复） |
| S-2 | 券商侧无 reduce-only；本批只防"本系统"重复卖 | 未解决（外部） |
| S-3 | 券商按 client_id 查询能力未验证 | 未解决（外部） |
| S-4 | 意图表无保留策略 | 低 |
| S-5 | 重启把 accepted/submitted 降级为 unknown（偏保守） | 低 |
| S-6 | 同一 decision_id 重复提交被永久阻断 | 低（设计） |
| S-7 | reduce-only 仍不可达 | 沿用 |

### 14.10 是否放行

**US-P1 Batch 2 验收通过（R-1 / R-2 / R-7 / R-4 全部关闭；R-3/R-8 按约束只分析）。**

```
423 passed / 1 skipped / 0 failed
新用例 36 个，全部生产路径
6 进程并发预留：1 成功 / 5 拒
SDK 零改动；配置零改动；三开关 false/false/true
```

**仍不允许实盘**。新增/沿用的阻断项：

| # | 阻断项 | 变化 |
|---|---|---|
| US-B1 | 无券商凭证 | 未变 |
| US-B2 | 无 AI Key | 未变 |
| US-B6 | 订单类型/TIF 枚举未知 | 未变 |
| US-B12 | 券商侧无 reduce-only 原语 | 未变（本批仅客户端约束） |
| **US-B14** | DB 故障期间无法止损（S-1） | **新增**，需运维/看门狗方案 |
| **US-B15** | 券商 client_id 查询能力未验证（S-3） | **新增**，取得凭证后验证 |

**建议下一批（US-P1 Batch 3）**：取得凭证 → 验证 client_id 查询与持仓可卖数量字段 →
US-B14 的运维预案（DB 故障快速恢复/外部看门狗）→ 再评估实盘。

**本轮修改代码，已全部测试通过，等待独立只读复核。**
