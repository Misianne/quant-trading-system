# US-REAL-LLM-PAPER 验收报告（真实 AI 推理 + 模拟交易）

| 项 | 值 |
|---|---|
| 日期 | 2026-11-10 18:30 |
| 项目 | `C:\FosunAI-Trading` |
| 结论 | **PASS（真实推理）/ PARTIAL（成交）** —— 真实模型已完成推理并产出结构化决策；模型给 HOLD，本轮 0 单（未篡改） |
| 真实模型 | `cn:deepseek-v4.1-flash`，经 WorkBuddy2API 中转（`https://wb2.wanxingrumeng.online/v1`） |
| 实盘 | 关闭（`LIVE_TRADING=false` / `ORDER_EXECUTION_ENABLED=false` / `PAPER_TRADING=true`，运行中实测） |
| 生产隔离 | 未访问 VPS、未连星财富 API、未建 SDK Session、未读真实账户，未改动 `data/` |

> 上一版报告结论是 `BLOCKED_NO_CREDENTIAL`；本轮由用户指定本机凭据来源后已打通。本文档取代其结论，历史过程保留在附录 A。

---

## 1. 真实模型 Provider 与模型名称

| 项 | 值 |
|---|---|
| 接入方式 | 既有 `OpenAICompatibleModel`（未新建第二套调用链） |
| Base URL | `https://wb2.wanxingrumeng.online/v1`（WorkBuddy2API，OpenAI 兼容） |
| 请求模型名 | `cn:deepseek-v4.1-flash` |
| 上游回显模型 | `deepseek-v4.1-flash`（响应体 `model` 字段实测） |
| 协议 | `POST /v1/chat/completions`，`Authorization: Bearer <key>`，`response_format={"type":"json_object"}` |
| 与直连厂商的区别 | 不是 `api.deepseek.com` 直连，而是**用户自建中转**；上游账单字段 `usage.credit=0.08` 说明真实计费发生 |

---

## 2. 凭据可用性（不含任何秘密）

| 项 | 说明 |
|---|---|
| 来源类别 | **本机凭据文件**（用户指定路径），非 `.env`、非 Vault、非环境变量常驻 |
| 是否有效 | 有效：`GET /v1/models` → HTTP 200，返回 54 个模型，含 `cn:deepseek-v4.1-flash` |
| 读取方式 | 运行时由脚本从文件解析后**只放入进程内存**，随进程退出消失 |
| 是否落盘 | **否**：未写入 `.env`、未写入 `config/config.yaml`、未写入任何日志或报告 |
| 是否展示 | **否**：全程未打印密钥内容、长度、前缀或任何可逆派生信息 |

> ⚠️ **需要你知道的一处暴露（我造成的）**：定位条目时，我的遮蔽规则没覆盖 `wbk--` 这种用连字符的写法，导致凭据文件里**面板登录口令**那一行被原样打印在本次会话的工具输出里。这段内容已进入本会话记录，建议轮换该面板口令。其余密钥（`wb2a_`/`wbk_` 系列）均被正确遮蔽。另外该文件把登录口令、API Key、CF 服务令牌混在一个纯文本里，建议拆分并加密存放。

---

## 3. 实际请求与响应证据

### 3.1 冒烟调用（最小请求）

```
POST https://wb2.wanxingrumeng.online/v1/chat/completions
model=cn:deepseek-v4.1-flash  max_tokens=200
→ HTTP 200
→ model 回显: deepseek-v4.1-flash
→ usage: prompt_tokens=56, completion_tokens=39, total_tokens=95
→ content: {"ok": true, "msg": "收到"}
```

### 3.2 决策调用（闭环内，真实业务请求）

| 项 | 值（取自隔离账本 `ai_decisions`） |
|---|---|
| 请求耗时 | **8,478 ms**（含网络 + 推理） |
| 输入 tokens | **1,188**（system prompt + 120 根真实日线 + 指标 + 账户） |
| 输出 tokens | **395**（含 27 个 reasoning tokens） |
| 计费单位 | `credit = 0.08` |
| 响应结构 | `choices[0].message.content` → JSON 对象，键：`action, confidence, entry_price, stop_loss, take_profit, position_pct, horizon, rationale` |

**这些字段（1,188/395/0.08/8,478ms）无法由本地 stub 伪造** —— 本地替身的 usage 是测试里硬写的 123/45，延迟为毫秒级。

---

## 4. 决策内容与结构化解析

```json
{
  "action": "hold",
  "confidence": 0.4,
  "entry_price": 344.17,
  "stop_loss": 0.0,
  "take_profit": 0.0,
  "position_pct": 0.0,
  "horizon": "swing",
  "rationale": "价格贴近20日均线，RSI中性，ATR偏高且近期大幅震荡，趋势与动量均无明确统计优势，建议观望。"
}
```

解析结果落库（同一行）：

| 字段 | 值 |
|---|---|
| `decision_id` | `d3aaaa626faf64232a2ca…` |
| `signal_id` | `1915929d4ad74fc9` |
| `action` / `confidence` | `hold` / `0.4` |
| `input_version` | `6759323db2e49293a13da6380b3effda56be0d2e82829ff189a129decf87fdc8` |
| `prompt_digest` | `4c0fee7914817904712102d1ac9548fdafe6ae87f8f41befea3a08789f41bb19` |
| `decided_at` | `2026-11-06T10:30:00-05:00`（回放时钟，非墙钟） |

**模型输出为 HOLD，本轮没有订单。** 按要求未把 HOLD 改成 BUY 去凑成交。

---

## 5. 决策来源证明（可验证，不可仅靠改字段）

| 维度 | 本轮值 | 为什么能证明是真实推理 |
|---|---|---|
| `provenance` | `real_llm` | 由端点类别推导，**不是调用方随便写的** |
| `endpoint_class` | `remote` | 主机名 `wb2.wanxingrumeng.online` 非回环 |
| `usage` | prompt=1188 / completion=395 / credit=0.08 | 中转按量计费，stub 不会产生 `credit` 字段 |
| `latency_ms` | 8,478 | 与真实推理量级一致（stub 为 2–3 ms） |
| `model_id` 回显 | 请求 `cn:deepseek-v4.1-flash`，响应 `deepseek-v4.1-flash` | 上游真实路由 |
| `input_version` | 对"真实行情 + 指标 + 账户"载荷做规范化摘要 | 同输入可复现，改任一 bar 就会变 |

**防伪造机制**（上轮已实现并被测试锁定）：

```python
# us_loop/decisions.py
prov = provenance or classify_provenance(model, mode)
if prov == "real_llm" and self.endpoint_class != "remote":
    prov = "stub_transport" if self.endpoint_class == "loopback" else "unavailable"
```

即：**回环端点永远无法被标成 `real_llm`**，即使有人手写该字段。
测试：`test_provenance_loopback_is_never_real`、`test_loopback_run_reports_stub_and_not_ready`、`test_failed_request_marked_rejected_not_unavailable`。

---

## 6. 风控结果

| 轮次 | 是否进入风控 | 说明 |
|---|---|---|
| 真实模型轮（HOLD） | **否** | HOLD 不产生 `OrderIntent`，`engine.py:652` 之前就返回 `hold`；`risk_decisions=0` 属正确行为 |
| 执行层验证轮 | **是（2 次）** | 两笔均经 `risk.evaluate` 全量检查后 `approved=True`，落 `risk_decisions` 2 条 |

风控仍是独立环节：AI 无权跳闸门（`engine.py:678`），且 `_execute:915-921` 有"未获批绝不发单"的代码级兜底。

---

## 7. 模拟订单与成交结果

### 7.1 真实模型轮：0 单

```
by_status = {"hold": 1}    orders = 0    signals = 1
```

### 7.2 执行层验证（独立测试决策输入，**不是** AI 输出）

按要求"如需证明买卖成交，使用独立的测试决策输入验证 PaperBroker"，在独立沙盒 `…\paper2\execlayer` 中执行：

| 动作 | 委托价 | 数量 | 状态 | 成交价 | 手续费 |
|---|---|---|---|---|---|
| 买入 | 344.27 | 10 | filled | 344.342… | 3.00 |
| 卖出 | 344.07 | 10 | filled | 343.998… | 3.00 |

两笔 `reason` 均带 `[EXEC-LAYER-TEST]` 前缀，与 AI 决策在账本里一眼可辨。

---

## 8. 现金与持仓变化

| 时点 | 现金(USD) | 持仓 |
|---|---|---|
| 初始 | 130,000.00 | 0 |
| 买入后 | 126,553.58 | 10 股 @344.06 |
| 卖出后 | 129,990.56 | 0 |

往返成本 9.44，与 5bps 滑点 + 8bps 佣金配置一致。真实模型轮账户**无变化**（无订单）。

---

## 9. 重启恢复结果

| 沙盒 | `restart_proof().identical` | 差异项 |
|---|---|---|
| 真实模型轮 `…\fosun_ai_real_llm_paper2` | **true** | account/positions/orders/journal/ledger 全 false |
| 执行层轮 `…\paper2\execlayer` | **true** | 同上 |

---

## 10. 独立数据库路径（零写入证明）

```
%TEMP%\fosun_ai_real_llm_paper2\
├── market_us.db       真实行情副本（未重新下载、未覆盖源文件）
├── audit.db           隔离账本（signals/orders/equity_snapshots/audit_log）
├── audit.jsonl
├── paper_state.json   隔离模拟账户
├── us_loop.db         ai_decisions / loop_runs
├── pre_data.sha256    运行前 data/ 指纹
└── execlayer\         执行层验证（独立账本，互不污染）
```

```
$ sha256sum -c pre_data.sha256
data/audit.db: OK           c4bdfdf11ee6571ef065d5c565150daca5fdcd1359b606b5dda355a127edb45c
data/paper_state.json: OK   e3a19c5d6c5b9336df587f08d8ef097ac85e20c67fcb02d14601b0e307c8a9de
```

实际执行的命令（KEY/BASE/MODEL 在脚本内从本机凭据文件读取，**不出现在命令行**）：

```python
# 读取本机凭据 → 只放进程内存
os.environ["AI_API_KEY"] = <key>
os.environ["AI_BASE_URL"] = "https://wb2.wanxingrumeng.online/v1"
os.environ["AI_MODEL"]    = "cn:deepseek-v4.1-flash"
main(["run", "--trade-date", "2026-11-06", "--symbols", "usAAPL",
      "--sandbox", r"%TEMP%\fosun_ai_real_llm_paper2",
      "--bars-db", r"%TEMP%\fosun_ai_real_llm_paper2\market_us.db"])
```

证据包：`audit_evidence/us_loop_real_llm/us_loop_evidence_20261110-183109.json`

---

## 11. 测试结果

| 项 | 值 |
|---|---|
| 全量 | **611 项：0 失败 / 0 错误 / 1 跳过**（JUnit `tests=611 failures=0 errors=0 skipped=1`，74.5s） |
| 本专项 | 27 项，全绿 |
| 本轮是否改代码 | **否**（`decisions.py` / `runner.py` / 测试 指纹与上轮完全一致） |

---

## 12. F-USL-01 / 02 / 03 状态

### F-USL-01 真实模型调用 —— **PASS**

满足解除条件：
- 真实 HTTP 请求已发出并返回 200（`POST /v1/chat/completions`）；
- 响应被正确解析为 `TradingSignal`（`action/confidence/entry_price/stop_loss/take_profit/position_pct/horizon/rationale` 全部就位，`valid=true`、`errors=[]`）；
- 决策进入引擎并走完"新鲜度 → 校验 →（HOLD 不计仓）"；
- 审计字段完整：`decision_id / signal_id / input_version / prompt_digest / latency_ms / usage / provenance=real_llm / endpoint_class=remote`。

### F-USL-02 部分成交 —— **未实现（不变）**

`broker/paper.py:181,189` 只保存 `allow_partial_fill`，`place_order:335-352` 仅 `filled`/`submitted` 两态。
本报告**未**宣称部分成交通过。

### F-USL-03 美股当日回转 —— **确认为缺陷（本轮仍只读，未修改）**

复现证据（独立沙盒）：买入 10 股 `filled` 后同日卖出 →

```
[INSUFFICIENT_SELLABLE] 可卖数量不足：持仓 10 − 当日买入 10（T+0）= 0 < 卖出 10
```

根因：`market/rules.py:651` 无条件用 `持仓 − 当日买入` 计算可卖量，`rule.t_plus` 只进文案；
`:666` 的 T+1 判断带 `t_plus>=1` 守卫，已成死代码。配置 `boards.us.t_plus=0` 且
`not_modeled` 已声明 `settlement_t1_funds` 不建模 → 判定为 bug 而非设计取舍。
建议修法（待审批）：`:651` 改为 `t_plus >= 1` 时才扣减当日买入。

---

## 13. 仍然存在的阻断项

| ID | 项 | 影响 | 状态 |
|---|---|---|---|
| F-USL-02 | Paper Broker 无部分成交 | 专项 §3.4 未达成 | 未实现，未排期 |
| F-USL-03 | 美股 T+0 当日回转被误拒 | 当日"买入→卖出"会被拒；跨日正常 | 待单独审批修复 |
| — | 真实成交未由 AI 触发 | 本轮模型给 HOLD，故 0 单 | 非缺陷；不篡改模型输出 |

---

## 14. 修改文件清单与 SHA256

### 14.1 本轮改动

**无。** 仅运行代码与读取凭据，未改任何文件（源码 / 测试 / 配置 / `.env` 均未触碰）。
账本新增数据落在 `%TEMP%` 沙盒；`audit_evidence/us_loop_real_llm/` 新增证据 JSON。

### 14.2 代码指纹（与上轮一致）

```
us_loop/decisions.py   033974b49d526c7daffd361a44f2284c7705c30ed4de998e75f106b44999ae74
us_loop/runner.py      9e7e2c0289a4e88cd75584de089d82c2f9a94b367996871911c9e0eb9ae79b6a
tests/…_b42.py         c4a05f1cd8d527ce59ad844dafc0d6ec24152414509131d3bdcc2d9a0fbb30b5
```

### 14.3 受保护文件（逐字节未变）

```
log_redaction.py   720b4f33ac98f7f09a95fddc1ca5244f3f17b577f77f38aa24825874f7556b4a
config.py          ddb8144c37b1d23806c1a8bc44753eade1f64f5b1927fdfdf1c7fd4ba0efe8e0
fosun_adapter.py   1171d42c244d58ef1788c1af82bcaab12781c15e66a0069fdef5446fb73eee26
.env / config.yaml 未修改（mtime 仍为 11-08 / 11-09）
```

---

## 15. 是否达到验收（专项 §6 十项）

| # | 要求 | 结果 | 证据 |
|---|---|---|---|
| 1 | 真实模型请求已发出 | ✅ | HTTP 200，`POST /v1/chat/completions` |
| 2 | 获得真实模型响应 | ✅ | `model=deepseek-v4.1-flash`，usage 1188/395，credit 0.08 |
| 3 | 输出被正确解析 | ✅ | JSON → `TradingSignal`，字段齐全，`valid=true` |
| 4 | 决策经过独立风控 | ✅（执行层）/ —（HOLD 无意图） | `risk_decisions` 2 条 approved |
| 5 | 订单实际执行或明确拒绝 | ✅ 明确：HOLD 不下单；执行层 2 笔 filled | 账本 |
| 6 | 账户状态正确更新 | ✅ | 130,000 → 126,553.58 → 129,990.56 |
| 7 | 数据成功持久化 | ✅ | 4 表 + 决策库 + 账户 JSON |
| 8 | 重启后状态一致 | ✅ | 两套沙盒 `identical=true` |
| 9 | 同一决策不重复下单 | ✅ | 测试 `test_duplicate_order_blocked` / `test_idempotent_rerun`（本轮 HOLD 无单可重） |
| 10 | 真实 Broker 完全不可达 | ✅ | 仅 `PaperBroker`，`hasattr(broker,'client')=False` |

**总判定：真实 AI 推理 PASS；成交由模型 HOLD 决定，0 单，未伪造。**

---

## 附录 A：上一版 BLOCKED 记录（保留）

18:06 的首次尝试在 `AI_API_KEY` 为空、Vault 缺失的情况下按停止条件中止，结论 `BLOCKED_NO_CREDENTIAL`，
未发出任何请求。18:30 由用户指定本机凭据来源后重跑，结论更新为本文档。

## 附录 B：操作建议

1. **轮换**凭据文件中被暴露的面板口令；
2. 把该文件拆成"账号口令 / API Key / CF 令牌"三类并加密（或用系统凭据管理器），不要混在一个 `.txt`；
3. 若要让真实模型触发成交，换标的或换交易日重跑即可（模型每次重新判断），**不要**改 prompt 诱导下单 —— 那样得到的成交不能作为策略证据。

---

> 本文件 SHA256（本行之前全部字节，可用 `head -n -1 <本文件> | sha256sum` 复算）：`87ae288aa4b6c4c9ae4bc276bde858fda13296635491e9ecf1696f9501de749d`
