# US-REAL-LLM-PAPER 独立验收审计（只读）

| 项 | 值 |
|---|---|
| 审计对象 | `US_REAL_LLM_PAPER_ACCEPTANCE_REPORT.md` + `audit_evidence/us_loop_real_llm/us_loop_evidence_20261110-183109.json` + `%TEMP%\fosun_ai_real_llm_paper2\` |
| 审计性质 | **只读**。未修改任何源码/测试/配置/报告/数据库；未读取任何密钥；未发起任何模型请求；未运行写入 `data/` 的命令 |
| 审计方式 | 独立复算哈希 + 读账本/证据 JSON + 读代码赋值路径 + 纯函数探针（不写盘） |
| **最终结论** | **PARTIAL** —— F-USL-01 不能独立确认 PASS |
| 关键理由 | 结构化决策与账本关联可确认；但"真实 HTTP 调用"没有任何持久化的传输层证据，且来源标记存在可伪造面 |

---

## 1. 交付物完整性（独立复算）

| 文件 | 大小 | mtime | SHA256 |
|---|---|---|---|
| `US_REAL_LLM_PAPER_ACCEPTANCE_REPORT.md` | 14,166 B | 2026-11-10 18:33:18 | 整文件 `3fb7fa6361bacb13223e34a99c4e97c8428201e123d1ee78f9a2a4035c82eb73` |
| 同上（去掉末行） | — | — | `87ae288aa4b6c4c9ae4bc276bde858fda13296635491e9ecf1696f9501de749d` |
| `us_loop_evidence_20261110-183109.json` | 9,569 B | 2026-11-10 18:31:09 | `23fde3aba2cea913ee1ac8fb072010287873e520d7cce33e491731d95827704e` |

- 报告采用"自指纹排除末行"写法，末行声明的 `87ae288a…` 与 `head -n -1 | sha256sum` 复算值**完全一致** ✅
- 你给的 `87ae288a…` 是**排除末行**口径；**整文件**哈希是 `3fb7fa63…`。两者都必须记录，否则无法用 `sha256sum` 直接校验整文件。
- 证据 JSON **未在报告中声明哈希**，本次首次计算（`23fde3ab…`）。

判定：**PASS**（可复算、声明与实际一致）。

---

## 2. 逐项核验

### C1 交付物完整性 — **PASS**
见上表，两种口径均复算一致。

### C2 HTTP 方法 / 路径 / 状态码 — **NOT_VERIFIED**

在证据 JSON 与两套沙盒账本里检索任何传输层痕迹：

| 应有 | 实际情况 |
|---|---|
| `POST` | 无记录 |
| `/v1/chat/completions` | 无记录 |
| `HTTP 200` | 无记录 |
| request-id / 响应头 | 无记录 |

`ai_decisions` 表 27 个字段里没有任何 `http_*`、`request_path`、`response_id`；
证据 JSON 顶层键为 `generated_at / settings / market_data / quote_source / model / account / positions / decisions / runs / journal_counts / readiness / restart`，无 http 段。

**这三项只存在于 18:29–18:31 的会话控制台输出里，没有被任何交付物持久化。独立复核者拿不到工件就无法复现。**

### C3 上游模型回显 — **NOT_VERIFIED**

账本里 `model_id = cn:deepseek-v4.1-flash`，但该值来自**请求侧**（`self.model.model_id`，`decisions.py:444` 附近），不是响应体 `model` 字段。
`response_digest` 是对**解析后的决策 JSON** 做 sha256，不含响应信封（`model`/`usage`/`id`）。
所以"响应回显 `deepseek-v4.1-flash`"无法从工件验证。

### C4 token 用量与 credit — **PARTIAL**

账本 `usage` 结构（22 个键）与本地测试替身（`{"prompt_tokens":123,"completion_tokens":45}`）差异极大，且含中转特有字段：

```
prompt_tokens=1188  completion_tokens=395  completion_thinking_tokens=313
credit=0.08  quota={model: deepseek-v4.1-flash, percent: 26}
prompt_cache_miss_tokens=1188  total_tokens=1583
```

这是**很强的旁证**（`credit` / `quota` 不是通用 OpenAI 字段），但它由被测代码在收到响应后自行写入数据库，属**自证**：没有任何上游签名、对账接口或第三方日志可交叉核对。

> 顺带纠正原报告：L62 写"395（含 **27** 个 reasoning tokens）"，账本实为 **313**。27 是 18:29 冒烟调用的值，被误嫁接到 18:30 的决策调用（见 F-05）。

### C5 latency — **PARTIAL**

`latency_ms=8478`；`loop_runs` 的 `started_at 18:30:29 → finished_at 18:30:38`（9 s）容纳得下，量级自洽。
但它是本地 `time.time()` 打点，只能证明"本进程阻塞了约 8.5 秒"，不能证明远端真做了推理。

### C6 解析为结构化决策 — **PASS**

- `raw` 键齐备：`action/confidence/entry_price/stop_loss/take_profit/position_pct/horizon/rationale`
- `audit.db.signals` 1 行，`signal_id=1915929d4ad74fc9`，与 `ai_decisions.signal_id` **完全一致**
- `loop_runs.summary.outcomes[0].stage=hold`、`valid=true`、`errors=[]`

### C7 来源标记防伪 — **FAIL**

对 `classify_endpoint / classify_provenance` 做 11 组地址形态测试（纯函数，不写盘）：

| 输入 | 判定 | 期望 |
|---|---|---|
| `127.0.0.1` / `localhost` / `0.0.0.0` / `[::1]` | `loopback` → `stub_transport` | ✅ |
| `192.168.1.5` | `remote` → **`real_llm`** | ❌ 局域网本地替身 |
| `10.0.0.7` | `remote` → **`real_llm`** | ❌ |
| `127.0.0.1.nip.io`（DNS 指向回环） | `remote` → **`real_llm`** | ❌ |
| `127.1`（回环简写） | `remote` → **`real_llm`** | ❌ |
| `2130706433`（127.0.0.1 的整数形式） | `remote` → **`real_llm`** | ❌ |

代码位置：`src/ai_trader/us_loop/decisions.py:70-92`（只比对 4 个字面量主机名）。
**结论：本地服务可以通过非规范地址形态拿到 `real_llm` 标记。**

### C8 readiness 是否依赖实际响应证据 — **FAIL**

```python
# src/ai_trader/us_loop/runner.py:345-356
real_rows = [d for d in self.ledger.list_decisions(limit=200)
             if d.get("provenance") == "real_llm"]
checks["ai_model_called"] = {"met": bool(real_rows), ...}
```

- 只看**字段**，不看 HTTP 状态、不看 usage 是否非空、不看端点类别；
- 注释写"只有 provenance=real_llm **且 endpoint_class=remote**"，**代码没有实现后半句**（`endpoint_class` 只进 detail，不参与过滤）。

配合 C7，可以造出 `met=true` 而没有真实推理。

### C9 决策与账本关联 — **PASS**

| 字段 | 值 | 交叉核对 |
|---|---|---|
| `decision_id` | `d3aaaa626faf64232a2ca` | 唯一 |
| `signal_id` | `1915929d4ad74fc9` | 与 `audit.db.signals` 同行一致 |
| `input_version` | `6759323db2e4…7fdc8` | 由真实 bar 载荷摘要得出 |
| `prompt_digest` | `4c0fee791481…4bb19` | 含 provider/model + 载荷 |
| `provenance` / `endpoint_class` | `real_llm` / `remote` | — |
| 订单 | `orders=0`、`order_intents=0`、`risk_decisions=0` | HOLD 不下单 ✅ |

### C10 独立 BUY 测试是否混入 — **PASS**

| 账本 | orders |
|---|---|
| `…\paper2\audit.db`（真实模型轮） | **0** |
| `…\paper2\execlayer\audit.db`（执行层） | **2**（buy 10 / sell 10，均 filled） |

物理上分开，未混入真实决策账本 ✅。
但原报告 L153 称"两笔 reason 均带 `[EXEC-LAYER-TEST]` 前缀，账本里一眼可辨"——**该前缀没有落库**（`orders.note=NULL`，`risk_decisions.reasons=[]`），见 F-06。

### C11 原项目 `data/` 零写入 — **PASS**（就这两个文件）

```
data/audit.db        c4bdfdf11ee6571e…  == 运行前快照 ✅
data/paper_state.json e3a19c5d6c5b9336… == 运行前快照 ✅
data/ 下 18:20 之后被修改的文件：0
logs/ 下 18:20 之后被修改的文件：0
```

### C12 隔离参数覆盖 — **PARTIAL**

`--sandbox` + `--bars-db` 覆盖了：`data_dir`、`audit.db`、`audit.jsonl`、`paper_state.json`、`us_loop.db`、`stops.json`、`bars_db`。
**未覆盖**：`cfg.log_dir`（`FaultRecorder` 的 `faults.jsonl / FAULT.json / alerts.log` 指向项目 `logs/`，见 `us_loop/runner.py:214-221`、`strategy/engine.py:139`）。
本次未发生故障所以没写，但**一旦出故障就会写回项目目录**。

### C13 风险边界 — **PASS**

```
live_trading=False  order_execution_enabled=False  paper_trading=True  kill_switch=False
system.mode=paper   us_loop 中 fosun 代码引用数=0   account.source=paper
```

未触发任何通往星财富真实交易接口的路径。

---

## 3. 发现清单

| ID | 级别 | 发现 | 位置 | 影响 |
|---|---|---|---|---|
| **F-01** | 高 | 真实 HTTP 调用无持久化传输证据（方法/路径/状态码/响应信封） | `decisions.py:330-450`；证据 JSON 无 http 段 | F-USL-01 无法被独立复核到 PASS |
| **F-02** | 高 | 来源标记有伪造面：`192.168.x` / `10.x` / `*.nip.io` / `127.1` / 整数 IP 均被判 `remote`→`real_llm` | `decisions.py:70-92` | "回环不可冒充远端"不成立 |
| **F-03** | 中 | `readiness` 只看 `provenance` 字段，且注释与实现不一致 | `runner.py:345-356` | 可 `met=true` 而无真实推理 |
| **F-04** | 中 | 沙盒未重定向 `log_dir` | `runner.py:214-221`、`engine.py:139` | 隔离保证不完整（本次未触发） |
| **F-05** | 中 | 原报告写"27 个 reasoning tokens"，实际 313 | 报告 L62 vs 账本 | 报告存在事实错误 |
| **F-06** | 低 | 原报告称执行层订单带 `[EXEC-LAYER-TEST]` 前缀，实际未落库 | 报告 L153；`orders.note=NULL` | 只能靠库文件隔离区分 |

---

## 4. 六个问题的直接回答

**1. F-USL-01 能否独立确认 PASS？**
**不能，判 PARTIAL。** 可确认的部分：模型输出被解析为结构化决策、决策进入引擎、账本字段串成一条链、HOLD 未下单、`data/` 零写入。
不可确认的部分：请求确实以 `POST` 打到 `/v1/chat/completions` 并返回 `200`、上游回显模型、usage/credit 来自上游。这些全都没有落盘。

**2. 是否存在伪造真实模型来源标记的路径？**
**是。** 两条：
(a) 端点判定：5 种本地地址写法被判 `remote`（F-02）；
(b) 就绪判定：`met` 只看字段不看证据（F-03）。
另注：直接传 `provenance="real_llm"` 对**非 remote** 端点会被降级，这条防线是有效的。

**3. 本次 HOLD 是否真实进入引擎？**
**是。** `audit.db.signals` 1 行（`signal_id=1915929d4ad74fc9`，与决策一致）、`loop_runs.summary.outcomes[0].stage=hold`、`by_status={"hold":1}`、`orders=0`、`order_intents=0`。

**4. 是否确认没有产生真实订单？**
**是。** 真实模型沙盒 `orders=0`；`us_loop` 包内无任何真实券商代码引用；`.gate.state.live_trading=False`、`order_execution_enabled=False`；账户 `source=paper`。

**5. 是否确认原项目 `data/` 零写入？**
**是（限于 `data/audit.db` 与 `data/paper_state.json`）。** 两者 sha256 与运行前快照逐字节一致，`data/` 与 `logs/` 均无 18:20 后的新写入。
但**隔离设计不完整**（F-04）：`log_dir` 未重定向，故障场景会写回项目目录。

**6. 能否进入多交易日、多标的历史模拟验证？**
**有条件可以，但现在不建议。** 先补三件事：
- **P1** 把传输层证据落盘：`http_method / request_path / http_status / upstream_model / response_id / response_envelope_sha256`，随 `ai_decisions` 同事务写入；
- **P2** 收紧 `classify_endpoint`：解析后判断是否落回私网/回环网段（`ipaddress.ip_address(host).is_loopback / is_private`，含 DNS 别名解析与整数/简写形式），或改用"必须命中显式白名单主机"的正向策略；
- **P3** `readiness` 改为同时校验：`http_status==200` 且 `usage.prompt_tokens>0` 且 `endpoint_class==remote`。
否则扩大样本只会放大"不可复核"的范围。

---

## 5. 要让 F-USL-01 升到 PASS，需要补什么

| 缺口 | 最低要求 |
|---|---|
| 传输证据 | 每笔决策持久化 `method/path/status/upstream_model/response_id`（或整段响应信封的 sha256 + 长度），与 `decision_id` 同事务 |
| 上游可信 | 响应体带 provider 侧 id（如 `id`/`system_fingerprint`），且与中转的用量对账页一致 |
| 来源防伪 | 端点判定覆盖私网/回环/别名/整数形式；或改为白名单制 |
| 就绪判据 | 依赖上述证据字段，而非单一 `provenance` |
| 报告 | 引用 usage 时逐字段核对；不要跨调用挪用数值（F-05/F-06） |

---

## 6. 本次审计自身的边界

- 未读取凭据文件、未打印任何 Authorization/API Key/CF Token；
- 未发起任何模型请求（因此也无法用"再打一次"来交叉验证，这本身是刻意选择：避免用新事件替代旧事件的证据）；
- 未运行 pytest（会写 `.pytest_cache`），故原报告"611 项/27 项全绿"标记为 **NOT_VERIFIED**；
- 未验证 F-USL-02（部分成交）/F-USL-03（T+0）之外的行为，仅确认报告对其描述与代码现状一致；
- 未构造故障场景验证 `logs/` 写入（F-04 的判断来自代码阅读，不是实测）。

---

## 7. 文件清单与指纹

| 文件 | 说明 | SHA256 |
|---|---|---|
| `audit_evidence/us_loop_real_llm/independent_audit.json` | 本次审计的机器可读证据（13 项检查 / 6 项发现） | `eade6cfbde79cffc7f182737de21a7bd742a50a39f3d9d5a16ecaaf7af498127` |
| `US_REAL_LLM_PAPER_INDEPENDENT_AUDIT.md` | 本文件 | 见末行（排除末行口径） |

**本次审计未修改**：源码、测试、配置、既有报告、`data/`、沙盒账本。

---

## 8. 结论

**PARTIAL。** 无法把 F-USL-01 认定为"已独立确认"。

已确认的：真实结构化决策存在、进入引擎、HOLD 未下单、账本可串链、`data/` 零写入、风险开关正确。
未确认的：该决策确实来自一次真实的远端 HTTP 推理调用（证据链在传输层断开），以及来源标记的抗伪造性。

在补齐 P1–P3 之前，任何"真实 LLM 闭环"的结论都只能算**自证**。

---

> 本文件 SHA256（本行之前全部字节，可用 `head -n -1 <本文件> | sha256sum` 复算）：`2d7e84885c0a20b25496d19647215fdf6b02a22f8415d2ebd017da8ddb39de23`
