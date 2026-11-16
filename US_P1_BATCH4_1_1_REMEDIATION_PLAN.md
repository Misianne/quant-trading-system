# US-P1 Batch 4.1.1 — 定点修复前置分析

- 工作目录：`C:\FosunAI-Trading`
- 分析时间：2026-11-09 22:10 — 22:3x（GMT+8）
- 本轮性质：**只读分析**。未修改任何代码、测试、配置、报告；未连接生产 API、未启动行情、未下单。
- 安全边界保持：`LIVE_TRADING=false`、`ORDER_EXECUTION_ENABLED=false`、`PAPER_TRADING=true`。
- 分析依据：`US_P1_BATCH4_1_SECOND_REVIEW.md`、`audit_evidence/us_p1_b41_second_review.json`、
  `US_P1_BATCH4_1_INDEPENDENT_AUDIT.md`、`audit_evidence/us_p1_b41_independent_audit.json`、
  `US_P1_BATCH4_1_FIX_REPORT.md`、`log_redaction.py`、`runtime.py`、`feed.py`、`fosun_adapter.py`、相关测试。
- 本文件是**计划**，不是实施记录。实施需人工审核后另开 Batch 4.1.1。

---

## 0. 摘要

| 项 | 结论 |
|---|---|
| 必须处置的 P2 | 4 项（`SR-F2-01` / `SR-F2-03` / `SR-B1-04` / `SR-B1-05`）+ 1 项 fail-open（`SR-F2-08`） |
| 需一并评估的加固项 | 6 项（转义 JSON、自定义 `handle`、hook 覆盖、`MarketPushAPI(client)`、`record.message`；其中"安装失败静默"与 `SR-F2-08` 是同一问题） |
| 涉及文件 | 4 个源码文件 + 3 个新增测试文件 + 1 份实施报告 |
| 是否需要改公开接口 | 否（仅新增内部函数与只读健康信息；`load_dotenv` 返回值需向后兼容扩展） |
| 是否存在行为变更 | **是，1 处高风险**：凭据来源优先级由「env 优先（含 dotenv）」改为「显式 env > Vault > dotenv > 注入」，**可能导致下次真实启动连到不同账户**，必须人工批准 |
| 无法在不改 SDK 的前提下 100% 封堵 | 5 项（见第 9 节） |
| 建议实施顺序 | 5 个阶段，每阶段带门槛测试（见第 10 节） |

**核心判断：本轮不能以"补几条正则"收口。** F-2 的问题不是规则不全，而是**边界选错**
——当前边界在 `Handler.handle`（`log_redaction.py:516-543`），
而记录可以绕过它（自定义 `handle`、安装失败、`Logger.handle` 直调）。
修复必须把边界上移一层（`Logger.handle`），并在最外层（`Handler.format`）加最终文本兜底，
同时对"无法保证脱敏的 handler"执行**拒绝输出**而不是静默放行。

---

## 1. Finding 对照表

> 两个清单存在一处重叠：`SR-F2-08`（handler 安装失败静默输出明文）既在"必须处置"里，
> 也在"另外必须评估"里。本文件保留**一个**完整条目（§4.7），并在两张表中都列出，不做合并或删除。

### 1.1 必须处置（5）

| ID | 领域 | 级别 | 标题 | 现网可达 | 处置类型 |
|---|---|---|---|---|---|
| `SR-F2-01` | F-2 | P2 | 5 种字段名变体不匹配，原样输出 | 否（新代码即触发） | 修规则 + 边界上移 |
| `SR-F2-03` | F-2 | P2 | `exc_info` 未中和，非标准 Formatter 重渲染泄露 traceback | 否（仅 SDK 示例） | 修 record 序列化 |
| `SR-B1-04` | B-1 | P2 | 三要素可来自不同来源，无同源校验 | **是**（默认 dotenv） | 改来源模型 |
| `SR-B1-05` | B-1 | P2 | 空 Vault 实例在 dotenv 场景下以他实例身份初始化 | 否（需 ≥2 实例） | 去掉静默复用 |
| `SR-F2-08` | F-2 | P3 | 安装失败静默 fail-open | 否（需自定义 filters） | 增加拒绝输出 |

### 1.2 另外必须评估（6，含 1 项重叠）

| ID | 领域 | 级别 | 标题 | 现网可达 |
|---|---|---|---|---|
| `SR-F2-02` | F-2 | P3 | 转义 JSON 不匹配 | 否 |
| `SR-F2-04` | F-2 | P3 | 自定义 `Handler.handle` 绕过过滤器 | 否 |
| `SR-F2-08` | F-2 | P3 | handler 安装失败后静默输出明文 | 否 |
| `SR-F2-05` | F-2 | P3 | `install_global_hook` 被覆盖后无法自愈 | 否 |
| `SR-A1-02` | A-1 | P3 | `MarketPushAPI(client)` 类级构造绕过哨兵 | 否 |
| `SR-F2-07` | F-2 | P3 | `record.message` 死分支 | 否 |

---

## 2. 当前保护边界（实测基线，供修复后对比）

| 检查 | 现状 | 来源 |
|---|---|---|
| 子 logger 传播 | ✅ 打码 | 场景 1 |
| `msg` + 字符串/对象 `args` | ✅ 打码 | 场景 8、14 |
| 标准 Formatter 的 traceback | ✅ 打码 | 场景 8 |
| 嵌套 extra / 敏感键名 | ✅ 打码 | 场景 9 |
| `lastResort` / 新建 handler | ✅ 打码 | 场景 10 |
| filter 顺序（脱敏先于其它 filter） | ✅ | 场景 11 |
| 5 种字段名变体 | ❌ 原样输出 | 场景 1 |
| 转义 JSON | ❌ 原样输出 | 场景 2 |
| `JsonFormatter` + `exc_info` | ❌ `exception` 字段含原文 | 场景 3 |
| 自定义 `handle()` | ❌ 原样输出 | 场景 4 |
| 安装失败 | ❌ 静默输出明文 | 场景 13 |
| `record.message` 预置值 | ❌ 不处理 | 场景 7 |
| `print` / `FaultRecorder` / `vault get --show` | ❌ 不在 F-2 范围 | 代码阅读 |

**结论不变**：F-2 目前是"handler 级尽力而为脱敏"，不是"日志安全边界"。

---

## 3. 修复总体架构（所有 F-2 条目共用）

当前只有一层：`Handler.handle` 上的 filter（`log_redaction.py:516-543`）。
建议改为**五层**，每层职责单一：

```
L0 凭据登记        register_secret / register_secrets            （已有，保留）
L1 记录级脱敏      _sanitize_record（msg/args/extra/exc_text）    （增强规则）
L2 分发级边界      logging.Logger.handle 钩子                     （新增）
L3 序列化级兜底    exc_info 中和 + Handler.format 最终文本脱敏     （新增）
L4 拒绝输出        无法安装脱敏的 handler → 禁用 + 告警 + 健康位   （新增）
```

### 3.1 为什么 L2 必须存在（不能只靠 L1+L3）

`Handler.handle` 是**实例方法**，任何子类重写都会遮蔽钩子（`SR-F2-04` 实测）。
`Logger.handle` 是**分发入口**：`Logger._log → Logger.handle → callHandlers → handler.handle`。
在 `Logger.handle` 处脱敏，记录在被交给任何 handler **之前**就已是安全形态，
因此重写 `handle` 的 handler 拿到的也是脱敏后的 record。

```python
# log_redaction.py 新增
_ORIGINAL_LOGGER_HANDLE = None

def _logger_handle(self, record):
    _GLOBAL_FILTER.filter(record)          # 幂等；异常内部 fail-closed
    return _ORIGINAL_LOGGER_HANDLE(self, record)

def install_logger_hook() -> bool:
    global _ORIGINAL_LOGGER_HANDLE
    with _INSTALL_LOCK:
        if getattr(logging.Logger.handle, "_aitrader_redaction", False):
            return False
        _ORIGINAL_LOGGER_HANDLE = logging.Logger.handle
        _logger_handle._aitrader_redaction = True
        logging.Logger.handle = _logger_handle
        return True
```

**边界说明（必须写进文档，不得含糊）**：
L2 只能覆盖"经由 `Logger.handle` 分发"的记录。若代码**直接**调用
`handler.handle(record)` / `handler.emit(record)`（例如自建队列消费、测试探针），
或 handler 在 `Logger.handle` 之外被直接驱动，则 L2 不生效——这类路径只能靠 L1
（若 handler 仍走 `Handler.handle`）或 L4（拒绝输出）。**不得宣称"所有 handler 均已保护"。**

### 3.2 为什么 L3 必须存在

L1 只清洗 `record`，但 Formatter 可以在 `format()` 里**重新生成**内容
（`JsonFormatter` 用 `exc_info` 重渲染就是实例）。L3 在 `Handler.format()` 的返回值上
再跑一次脱敏，等于"最后一道网"：

```python
_ORIGINAL_HANDLER_FORMAT = None

def _handler_format(self, record):
    text = _ORIGINAL_HANDLER_FORMAT(self, record)
    if getattr(record, "_aitrader_redacted", False) is False:
        return redact_text(text, truncate=False)   # 新增 truncate 参数
    return text
```

- `truncate=False`：避免把已经成型的 JSON/长行截断。
- `_aitrader_redacted` 标记由 L1 设置；已标记则跳过，避免每条日志两次全量正则。
- 该层对**任何** Formatter 生效（含第三方），但它只处理"字符串"，无法挽回
  formatter 直接写文件/套接字的行为。

### 3.3 L4 的语义（用户明确要求：拒绝输出，而非静默放行）

`install_on_handler` 失败时的降级链：

1. 正常：`handler.filters.insert(0, _GLOBAL_FILTER)`；
2. 失败（`filters` 不可变/被代理）→ 在**实例**上包 `handler.handle`：
   `handler.handle = _wrap(original_handle)`，包装内先跑 filter 再调原方法；
3. 再失败 → `handler.setLevel(logging.CRITICAL + 1)`（等于静默丢弃）
   + 向 `sys.stderr` 打一行 `[redaction] handler disabled: <repr>`（**不含记录内容**）
   + 记入 `_BLOCKED_HANDLERS`，由 `redaction_health()` 暴露；
4. 任一步成功都要在 handler 上打标 `_aitrader_redaction_mode ∈ {filter, wrapper, blocked}`，
   供 `redaction_health()` 与测试断言使用。

`install_on_handler` 的返回值语义保持 `bool`（True = 该 handler 已处于"不会明文输出"状态，
包含 wrapper/blocked 两种），避免破坏 `install_on_handlers` 的调用方。

---

## 4. 逐条分析

每条按用户要求的 10 项展开。所有"最小复现"均可直接落到
`%TEMP%\b41_second_review\probe_f2.py <n>` / `probe_b1_a1.py <mode>`
（本机已存在，本轮未改动）。

---

### 4.1 `SR-F2-01` — 5 种字段名变体不匹配（P2）

**[1] 原始描述**
`session_id=…`、`sessionId: …`、`x_api_key=…`、`session_token=…`、`sessionid=…`
五种写法均原样进入日志；对照组 `X-Session:` / `x-api-key:` 正常打码。

**[2] 真实触发路径**
任意 logger 以文本形式输出会话/密钥标识，例如
`logger.info("connect %s session_id=%s", url, sid)`。
当前仓库无此调用点，但这是**最自然的下一条日志**；`feed.py` 与 SDK 均持有
`X-Session` 值（`feed.py:774-783`、`adapter:437-446`），一旦有人为了排障打印，
立即泄露。

**[3] 文件与行号**
- 通用规则：`src/ai_trader/log_redaction.py:118-120`（分支仅 `session`，要求其后紧跟 `\s*[:=]`）
- 专用头规则：`src/ai_trader/log_redaction.py:109-110`（只认连字符 `x-api-key`）
- URL 规则：`src/ai_trader/log_redaction.py:122-123`（只认 `session=`）
- 键名判定：`src/ai_trader/log_redaction.py:141-145`（结构化 extra 用归一化精确匹配，文本路径未复用）

**[4] 最小复现**
`probe_f2.py 1`：

```python
setup_logging("INFO")
log = logging.getLogger("ai_trader.market.feed.child")
log.warning("A url=wss://x.test/ws?session_id=%s", SID)
log.warning("B sessionId: %s", SID)
log.warning("C x_api_key=%s", XK)
log.warning("D session_token=%s", SID)
log.warning("E sessionid=%s", SID)
log.warning("F X-Session: %s", XS)   # 对照
```

实测输出：A/B/C/D/E 五行原样；F 为 `X-Session: ***`。

**[5] 根因**
文本规则用**字面量分支**枚举键名，而 Python 生态里同一语义有
`snake_case` / `camelCase` / 连字符 / 无分隔 四种写法。
结构化 extra 路径已有归一化函数 `_is_sensitive_key`（`:141-145`），
但 `redact_text` 完全没有复用它——**两套判定逻辑，只有一套正确**。

**[6] 推荐修复（不是"再加一条正则"）**
分三步，缺一不可：

1. **统一判定源**：把 `_SENSITIVE_KEYS` 扩展为 `_SENSITIVE_TOKENS`
   （`sessionid` / `sessiontoken` / `sessionkey` / `apikey` / `xapikey` / `xsession` /
   `accesstoken` / `refreshtoken` / `idtoken` / `clientsecret` / `privatekey` …），
   文本与 extra 共用同一张表。
2. **文本侧改为"标识符归一化匹配"**：
   用一条规则捕获"键标识符 + 分隔符 + 值"：

   ```python
   _IDENT = r"[A-Za-z0-9_.\-]{1,64}"
   _SEP = r"\s*[:=]\s*"
   _VAL = r"(\"[^\"]{1,4096}\"|'[^']{1,4096}'|[^\s,;>]{1,4096})"
   # 先粗匹配，再用 _is_sensitive_token(normalize(ident)) 判定，命中才替换
   ```

   实现上用 `re.sub` 的回调（而不是固定替换串）：回调里对捕获的标识符做
   `_NON_KEY_CHARS.sub("", ident.lower())`，命中 `_SENSITIVE_TOKENS` 才替换，
   否则原样返回。这样 `token_count=5`、`session_timeout=30` 不会被误伤。
3. **后缀白名单**（防误伤）：归一化后若以
   `count|len|size|ttl|timeout|expire|expiry|name|type|mode|enabled|at|ts` 结尾则不替换。

同时把 URL 规则改为"参数名走同一判定"（`:122-123` 的回调版本）。

**[7] 副作用**
- 会多打码一部分"看起来像键值对"的文本，例如
  `session_timeout=30` 已被白名单排除，但 `sessionIdle=abc` 会被打码（可接受）。
- 正则改为回调后，`redact_text` 的耗时会略升（每条多一次回调）；
  需要基准测试：目标 < 1.5×（现有单条 2 MiB 上限 `:224` 不变）。
- 文本规则与 extra 规则合并后，`_RULES` 从 7 条变为 6 条 + 1 个回调规则，
  需更新 `test_redact_text_is_idempotent_and_keeps_normal_text`（`:238-243`）的预期。

**[8] 必须新增的回归测试**
- `test_session_id_variants_are_masked`：5 种写法逐一断言 `SID not in out`。
- `test_sensitive_token_suffix_not_masked`：`token_count=5`、`session_timeout=30`
  原样保留（防误伤，负向用例）。
- `test_text_and_extra_share_token_table`：同一词在 `msg` 与 `extra` 中结果一致。
- `test_url_query_session_id_masked`：`?session_id=…&x_api_key=…`。

**[9] 接口/兼容**
无公开接口变更。行为变更是"多打码"，仅影响日志文本，不影响业务分支。

**[10] 验收证明**
`probe_f2.py 1` 的 5 行 `LEAK` 全部变为 `False`，且 F/G 对照仍为 `False`；
新增负向用例证明未误伤；全量 517 例不回归。

---

### 4.2 `SR-F2-03` — `exc_info` 未中和，非标准 Formatter 重渲染（P2）

**[1] 原始描述**
过滤器把打码后的 traceback 写进 `record.exc_text`，但保留原始 `record.exc_info`。
SDK 的 `JsonFormatter` 直接 `formatException(record.exc_info)`，把未打码的
完整 traceback 写进 `exception` 字段。

**[2] 真实触发路径**
`handler.setFormatter(JsonFormatter())`（`sdk_src/example/example.py:28`、
`push_example.py:29` 的标准写法）→ `log.exception(...)` →
`JsonFormatter.format`（`logging_utils.py:117-134`）→ `:131-132`
`payload["exception"] = self.formatException(record.exc_info)`。
项目运行时不使用该 Formatter（`runtime.py:216-220` 用 stdlib），
但运维脚本照抄 SDK 示例即命中。

**[3] 文件与行号**
- 过滤器异常处理：`src/ai_trader/log_redaction.py:384-392`
  （`:387` 只在 `exc_text` 为空时生成；生成后**未清空** `exc_info`）
- SDK 重渲染：`sdk_src/fsopenapi/logging_utils.py:131-132`
- `exc_text` 在 JSON 里不可见的原因：`_RESERVED_RECORD_FIELDS`
  （`logging_utils.py:30`）把它跳过

**[4] 最小复现**
`probe_f2.py 3`：

```python
h = logging.StreamHandler(buf); h.setFormatter(JsonFormatter())
try: raise RuntimeError("handshake rejected: api_key=%s" % SECRET)
except RuntimeError: log.exception("connect failed api_key=%s", SECRET)
```

实测：`payload.msg = "connect failed api_key: ***"`（证明 filter 跑了），
但 `payload.exception` 含 `SECRET`，`LEAK[exception] = True`；
标准 Formatter 对照 `LEAK = False`。

**[5] 根因**
`exc_text` 只是**标准 Formatter 的缓存**，不是"唯一事实来源"。
任何直接读 `exc_info` 的序列化器都能重建原文。
同类风险还包括 `traceback.format_exception(*record.exc_info)`、
`record.exc_info[1].__traceback__`、以及 `__cause__`/`__context__` 链。

**[6] 推荐修复**
L1 末尾（`_sanitize_record` 的异常段之后）做**中和**：

```python
if record.exc_info:
    text = _format_exception(record.exc_info)      # 现有逻辑
    record.exc_text = redact_text(text)
    record.safe_exception = record.exc_text        # 供 JSON 消费方读取（extra）
    record.exc_info = None                         # 关键：切断重渲染来源
```

- 先取文本、脱敏、存 `exc_text`，再置 `exc_info = None`。
- 标准 `Formatter.format` 在 `exc_info` 为空但 `exc_text` 非空时**仍会追加** `exc_text`，
  诊断信息不丢（实测见 §4.2 的对照）。
- 为照顾 JSON 消费方，额外提供 `safe_exception` 属性；`JsonFormatter` 会把它
  当作普通 extra 序列化（它只跳过 `_RESERVED_RECORD_FIELDS`）。

同时 **L3**（`Handler.format` 兜底）作为第二道保险，覆盖"连 `exc_text` 都不读、
自己从别处拿原始异常"的极端 Formatter（能做到的有限，见第 9 节）。

**[7] 副作用**
- **行为变更**：第三方 Formatter 若依赖 `record.exc_info`（例如 Sentry 集成、
  结构化日志库的 exception 字段），将拿不到异常对象，只能拿到字符串。
  这是 fail-closed 的必然代价，必须写入变更说明。
- `record.safe_exception` 会成为所有 Handler 可见的 extra；
  若该 Formatter 把 extra 全量序列化，日志体积增加约 1 份 traceback。
- 标准 Formatter 输出不变（`exc_text` 仍被追加）。

**[8] 必须新增的回归测试**
- `test_exc_info_is_neutralized`：filter 后 `record.exc_info is None`、
  `record.exc_text` 含 `***`、不含原文。
- `test_sdk_json_formatter_does_not_leak`：用真实 `JsonFormatter`，
  断言 `json.loads(out)["exception"]` 不含原文，且 `safe_exception` 含 `***`。
- `test_standard_formatter_still_prints_traceback`：断言 `RuntimeError` 与
  `Traceback` 仍在输出里（防止"为了安全把诊断信息全删了"）。

**[9] 接口/兼容**
`record.exc_info` 的语义改变（对外可见）。需在 `log_redaction.py` 模块 docstring
与修复报告中明确写出，并列为"已知行为变更"。

**[10] 验收证明**
`probe_f2.py 3` 的 `LEAK[exception]` 变为 `False`；
同时 `payload.msg` 仍含 `api_key: ***`、`safe_exception` 含 `RuntimeError`。

---

### 4.3 `SR-B1-04` — 三要素来源不一致，无同源校验（P2）

**[1] 原始描述**
同一实例的 `SERVER_PUBLIC_KEY` / `CLIENT_PRIVATE_KEY` 可来自 Vault，
而 `FSOPENAPI_API_KEY` 来自环境（dotenv 或 ops 导出），形成"混合身份"。
`health().configured` 仍为 `True`，`credential_origin` 只在细看时才暴露。

**[2] 真实触发路径**
`load_config()` 默认 `use_dotenv=True`（`config.py:338-340`）→
`load_dotenv()` 把 `.env` 的键 `setdefault` 进 `os.environ`（`config.py:238-256`）→
`_resolve_credentials()` 对三个键分别决策（`adapter:500-532`）：
- PUB/PRIV 若已是"我们注入的" → 回查 Vault（`:508-511`）；
- API_KEY 从未被注入（`_SDK_ENV_MAP` 只有两项，`:452-455`）→ 永远走 `env` 分支。
结果：`origin = {PUB: vault, PRIV: vault, API_KEY: env}`。
实测见 `probe_b1_a1.py b1e`：
`vault 里的 API_KEY 是否被采用 = False | 实际 = AK_ENV`。

**[3] 文件与行号**
- `src/ai_trader/broker/fosun_adapter.py:452-455`（`_SDK_ENV_MAP` 仅 2 项）
- `src/ai_trader/broker/fosun_adapter.py:500-532`（逐键决策，无跨键一致性检查）
- `src/ai_trader/broker/fosun_adapter.py:547-551`（记录 origin、登记字面量）
- `src/ai_trader/config.py:238-256`、`:253-254`（dotenv 用 `setdefault`，不可区分来源）
- `src/ai_trader/config.py:338-340`（默认加载 dotenv）

**[4] 最小复现**
`probe_b1_a1.py b1e`（用假值 `AK_ENV` 模拟 `.env` 已提供 API_KEY）：

```python
os.environ["FSOPENAPI_API_KEY"] = AK_ENV
a = FosunBroker(cfg, Vault({PUB1, PRIV1, AK1}))
print(a._cred_origin)          # {PUB: vault, PRIV: vault, API_KEY: env}
print(a.client.api_key_arg)    # AK_ENV，不是 AK1
```

**[5] 根因**
三个键的**来源判定彼此独立**，没有任何"必须同源"的约束；
更深一层：`load_dotenv` 用 `os.environ` 作为唯一载体，导致
"运维显式 export" 与 "应用自己从 `.env` 读入" 在进程内**完全不可区分**，
于是 `_resolve_credentials` 的优先级 1（"调用方显式设置的环境变量"）
把 dotenv 也当成了显式配置。

**[6] 推荐修复**
1. **来源可区分**（`config.py`，向后兼容）：
   ```python
   _DOTENV_KEYS: set[str] = set()

   def load_dotenv(path=None) -> bool:      # 返回值保持 bool
       ...
       if key not in os.environ:
           os.environ[key] = value
           _DOTENV_KEYS.add(key)            # 新增
       ...

   def dotenv_keys() -> frozenset[str]:     # 新增只读访问器
       return frozenset(_DOTENV_KEYS)
   ```
   注意：若某键已被真实环境变量占用，`setdefault` 不会写入，
   也就**不会**进入 `_DOTENV_KEYS` —— 这正是我们要的语义。

2. **来源模型**（`adapter.py`）：
   ```python
   class _CredSource(str, Enum):
       EXPLICIT_ENV = "explicit_env"
       DOTENV = "dotenv"
       VAULT = "vault"
       INJECTED = "injected"
       MISSING = "missing"
   ```
   优先级（新）：`EXPLICIT_ENV > VAULT > DOTENV > INJECTED > MISSING`。
   —— 即 dotenv 不再压过 Vault。

3. **同源校验**：三要素解析完成后求来源集合
   `sources = {src(PUB), src(PRIV), src(API_KEY)}`：
   - `sources ⊆ {VAULT}` 或 `⊆ {EXPLICIT_ENV}` 或 `⊆ {DOTENV}` → 通过；
   - 混合 → `CredentialSourceMix`（`BrokerError` 子类，`code=CREDENTIAL_SOURCE_MIX`），
     **fail-closed**；策略开关 `fosun.credential_mix_policy: reject|warn`，
     默认 `reject`，`warn` 仅供排障（并在 `health()` 里返回 `mixed=true`）。

4. **`health()` 增补**（只读、不含值）：
   `credential_sources: {PUB: "vault", ..., API_KEY: "dotenv"}`、
   `credential_mixed: bool`、`credential_policy: "reject"`。

**[7] 副作用（高风险，必须人工批准）**
- **行为变更**：`.env` 里同时有 PUB/PRIV/API_KEY（当前仓库即如此）的部署，
  升级后 PUB/PRIV 会优先取 Vault。若 Vault 与 `.env` 属于不同账户，
  **下次真实启动会连到不同账户**。
- 缓解：提供一次性开关 `fosun.credential_source_policy: strict|legacy`，
  `legacy` 复刻旧优先级（env 含 dotenv 优先），默认 `strict`；
  上线前用 `ai-trader doctor`（只读）对比两种策略解析出的**来源**（不打印值）。
- `load_dotenv` 增加模块级状态：同一进程多次调用、不同 path 的测试需清理
  `_DOTENV_KEYS`（提供 `_reset_dotenv_keys()` 供测试夹具）。

**[8] 必须新增的回归测试**
- `test_dotenv_api_key_does_not_override_vault`：`.env` 有 AK_ENV、Vault 有 AK1
  → 解析结果必须是同一来源（Vault 全三要素），或抛 `CredentialSourceMix`。
- `test_explicit_env_still_wins`：真实 `os.environ`（非 dotenv）设置 → 优先。
- `test_mixed_sources_rejected_by_default`：构造 PUB=vault / AK=env → 抛错。
- `test_mixed_sources_warn_policy`：`policy=warn` → `configured=True` +
  `health().credential_mixed is True`。
- `test_health_does_not_leak_values`：`health()` 序列化结果不含任何凭证字面量。

**[9] 接口/兼容**
- `load_dotenv()` 返回值不变（bool），新增 `dotenv_keys()`；
- 新增异常类型 `CredentialSourceMix`；
- 新增配置键 `fosun.credential_source_policy`、`fosun.credential_mix_policy`
  （**不改 `config/config.yaml`**，用代码默认值 + `cfg.fosun.get(..., 默认)` 读取）。

**[10] 验收证明**
`probe_b1_a1.py b1e` 重跑：`A` 的 `origin` 三键同源（全 vault 或全 env），
`vault 里的 API_KEY 是否被采用 = True`；混合场景抛 `CREDENTIAL_SOURCE_MIX` 且
`configured=False`、环境变量未被写入。

---

### 4.4 `SR-B1-05` — 空 Vault 实例静默继承他实例身份（P2）

**[1] 原始描述**
同进程内 A（Vault 有值）先注入 PUB/PRIV；随后 C（空 Vault）在
`_resolve_credentials` 的 `elif injected_value:` 分支（`adapter:526-529`）
复用 A 的值。若环境里还有 `FSOPENAPI_API_KEY`（dotenv 默认提供），
C 会 `configured=True`、`owners=2`，以 A 的完整身份工作。

**[2] 真实触发路径**
`FosunBroker(cfg, Vault({}))` 或 `FosunBroker(cfg, None)`，且同进程已有成功实例。
干净环境实测 C 是 fail-closed（缺 API_KEY，`probe b1`）；
`probe b1e` 下 C `configured=True`、`owners=2`。

**[3] 文件与行号**
- `src/ai_trader/broker/fosun_adapter.py:526-529`（复用注入值）
- `src/ai_trader/broker/fosun_adapter.py:507-515`（`is_ours` 分支）
- `src/ai_trader/broker/fosun_adapter.py:553-590`（注入与引用计数）
- `src/ai_trader/broker/fosun_adapter.py:636-654`（`missing` 判定）

**[4] 最小复现**
`probe_b1_a1.py b1e`：

```python
a = FosunBroker(cfg, Vault({PUB1, PRIV1, AK1}))
c = FosunBroker(cfg, Vault({}))
print(c.configured, c._cred_origin, _CRED_ENV.owners_of("FSOPENAPI_SERVER_PUBLIC_KEY"))
# True {'PUB':'injected','PRIV':'injected','API_KEY':'env'} 2
```

**[5] 根因**
"没有自己的凭证"与"使用别人的凭证"在代码里是同一条分支，
缺少"这是**我自己的**身份"的判据。设计文档里把它解释为
"单账户多实例/worker 场景复用"（`:484-492`），但没有任何显式开关，
也没有告警——默认行为就是继承。

**[6] 推荐修复**
1. **删除静默复用**：`elif injected_value:` 改为 `else: missing`
   （即 `origin="missing"`，由 `_init_client` 的 `missing` 分支 fail-closed）。
2. **保留能力但显式化**（可选）：新配置
   `fosun.allow_injected_credential_reuse: bool = False`；
   仅当为 `True` 且**三要素全部来自 injected**时才复用，
   并在 `health()` 返回 `credential_reuse=true` + 一条 `logger.warning`（只含变量名）。
   若 `API_KEY` 不在 injected 集合内（当前 `_SDK_ENV_MAP` 只注入两项），
   即使开关打开也必须 fail-closed —— 否则又是"混合来源"。
3. **配套**：把 `FSOPENAPI_API_KEY` 纳入 `_SDK_ENV_MAP`（`:452-455`），
   使三要素的注入/引用计数/来源判定完全对称。
   SDK 本来就支持从环境读 API Key（`client.py:29` 的 `resolved_api_key`），
   注入不改变行为，只让"来源"可追踪。

**[7] 副作用**
- 依赖"隐式复用"的多 worker 部署会开始 fail-closed —— 这正是修复目标，
  但必须在发布说明中标注，并提供显式开关。
- 把 API_KEY 写入 `os.environ` 扩大了进程内明文分布面；
  它本就在 `SDKClient(api_key=...)` 参数里，风险增量有限，
  但需在 `release_credentials` 时一并清理（引用计数已覆盖）。

**[8] 必须新增的回归测试**
- `test_empty_vault_instance_fails_closed`（干净环境 + dotenv 环境各一份）：
  `configured=False`、`blocked_reason` 含 `missing`、`owners` 不增加。
- `test_reuse_requires_explicit_flag`：开关 false → fail-closed；
  开关 true 且三要素齐全 → 复用且 `owners=2` + 告警。
- `test_reuse_never_mixes_api_key_source`：开关 true 但 API_KEY 来自 env → fail-closed。

**[9] 接口/兼容**
新增配置键（代码默认值，不改 yaml）；`configured` 的取值在少数部署下由
`True` 变 `False`，属于安全方向的行为变更，必须在报告"已知影响"中列出。

**[10] 验收证明**
`probe_b1_a1.py b1e`：C 的 `configured=False`、`owners=1`（不再 +1）；
开启显式开关的对照用例 `configured=True` 且 `credential_reuse=true`。

---

### 4.5 `SR-F2-02` — 转义 JSON 不匹配（P3）

**[1] 原始描述**
`{"api_key": "…"}` 会被打码，但 `{\"api_key\": \"…\"}`（JSON 字符串里再嵌 JSON）不会。

**[2] 真实触发路径**
任何"把请求体 `json.dumps` 一次再当字符串记录"的代码；或日志采集链路对
已序列化的 JSON 再做一次转义。仓库当前无此路径（SDK 的 `log_body` 走
`sanitize_payload`，`client.py:68-75`）。
> 勘误：`US_P1_BATCH4_1_SECOND_REVIEW.md` §3.2 把该位置写作 `client.py:78-86`，
> 实测 `_build_request_log_fields` 位于 `:68-75`；本计划以实测行号为准，不回改已交付报告。

**[3] 文件与行号**
`src/ai_trader/log_redaction.py:112-114`（要求闭合引号紧跟键名）。

**[4] 最小复现**
`probe_f2.py 2`：`escaped = json.dumps(json.dumps({"api_key": SECRET}))` →
`I escaped_body="{\"api_key\": \"sk-live-FAKE-…\"}"`，`LEAK=True`。

**[5] 根因**
规则按"未转义 JSON"的字面形状编写；转义后键名两侧多出 `\`，
`"key"` 与 `\"key\"` 是两种不同的字节序列。

**[6] 推荐修复**
1. 规则容许 0–2 层反斜杠：把引号写成 `\\{0,2}"`，值部分同样处理；
2. 与 §4.1 的"标识符回调"共用判定：转义 JSON 里提取出的键名先 `unescape`
   （只处理 `\"` / `\\`），再走 `_is_sensitive_token`；
3. **不要**为了匹配而先整体 `text.replace('\\"','"')` 再输出——那会改变日志原文。

**[7] 副作用**
- 回调里做小范围 `unescape` 需要限制长度（沿用 `MAX_RAW_TEXT_CHARS`）；
- 可能出现"多打码"：普通文本里恰好出现 `\"token\": \"x\"` 也会被打码（可接受）。

**[8] 必须新增的回归测试**
- `test_escaped_json_api_key_masked`（1 层、2 层转义各一例）；
- `test_escaped_json_keeps_valid_json`：打码后 `json.loads(json.loads(out))` 仍可解析；
- `test_plain_backslash_text_untouched`：`C:\path\to\file` 不被误伤。

**[9] 接口/兼容**
无。

**[10] 验收证明**
`probe_f2.py 2` 的 `LINE[I escaped] LEAK=False`。

---

### 4.6 `SR-F2-04` — 自定义 `Handler.handle` 绕过过滤器（P3）

**[1] 原始描述**
Handler 子类重写 `handle()` 且不调用 `super()` 时，filter 与 `Handler.handle` 钩子
同时失效，记录原样输出。

**[2] 真实触发路径**
第三方库/运维脚本自定义 Handler；仓库当前无此类实现。

**[3] 文件与行号**
`src/ai_trader/log_redaction.py:516-543`（钩子挂在 `logging.Handler.handle`）。

**[4] 最小复现**
`probe_f2.py 4`：

```python
class _HandleOverride(logging.Handler):
    def handle(self, record): self.lines.append(self.format(record))
    def emit(self, record): pass
```

实测 `J X-Api-Key: XKEY-FAKE-…`；只重写 `emit` 的对照组打码正常。

**[5] 根因**
保护点选在"被重写的那个方法"上。子类重写会遮蔽基类实现，而 Python 没有
"方法调用拦截"机制（除非元类/`__getattribute__` 包装实例）。

**[6] 推荐修复**
1. **L2（`Logger.handle` 钩子）**：记录在分发前脱敏 → 该类 handler 拿到的是
   已脱敏 record（主修复）。
2. **L3（`Handler.format` 兜底）**：即使 handler 绕过 filter，
   它最终仍要调 `self.format(record)` 才拿到字符串；在 `Handler.format` 上兜底。
3. **残余**：若 handler 既重写 `handle`、又重写 `format`、还绕过 logger 直接
   接收 record —— 无法在不替换该 handler 实例的前提下拦截（见第 9 节）。

**[7] 副作用**
- L2 让**所有**记录在分发前脱敏：CPU 增加（每条一次 filter），
  但 L1 已有 `_Budget` 限额；需基准对比（目标 < +10% 日志开销）。
- L3 在 `Handler.format` 上做字符串级脱敏，可能对"非文本"formatter（返回 bytes）
  出错 → 必须判断 `isinstance(text, str)`，非 str 原样返回。

**[8] 必须新增的回归测试**
- `test_custom_handle_override_is_redacted`：场景 4 的 handler，断言打码；
- `test_custom_format_override_is_redacted`：重写 `format` 返回手工拼串，
  断言 L3 兜底生效；
- `test_direct_handler_handle_call_is_documented_gap`：
  直接 `handler.handle(record)` 且 handler 重写 handle → **断言仍泄露**，
  作为"已知边界"的显式用例（防止后人误以为已封堵）。

**[9] 接口/兼容**
新增 `install_logger_hook()`；`setup_logging` 多调用一次。无公开 API 破坏。

**[10] 验收证明**
`probe_f2.py 4` 的 `LEAK=False`；同时 `P-F2-04` 的"已知边界"用例仍按预期泄露，
证明文档描述与实际一致。

---

### 4.7 `SR-F2-08` — handler 安装失败后静默输出明文（P3，两张清单重叠项）

**[1] 原始描述**
`install_on_handler` 用 `except Exception: return False` 吞掉所有安装错误，
`setup_logging` 不看返回值。结果是"装不上"与"装上了"对外完全一样：
日志照常输出，只是没有脱敏。

**[2] 真实触发路径**
第三方 handler 把 `self.filters` 换成 tuple/只读序列，或 `filters` 被属性代理。
实测 `probe_f2.py 13`。

**[3] 文件与行号**
- `src/ai_trader/log_redaction.py:471-485`（`isinstance` 检查 + `except: return False`）
- `src/ai_trader/runtime.py:221-222`（忽略返回值）

**[4] 最小复现**
`probe_f2.py 13`：

```python
class _ImmutableFilters(logging.StreamHandler):
    def __init__(self, buf): super().__init__(buf); self.filters = ()
```

实测：`install_on_handler() -> False`，随后输出
`Y X-Session: XSESS-FAKE-c5d8e1f4a72b`（明文）。

**[5] 根因**
把"脱敏是增强"当成了"脱敏可失败"（`adapter:321-322` 的注释也是同一思路）。
对日志来说，**"无法脱敏"必须等价于"不许输出"**，否则安全属性取决于
第三方 handler 的实现细节。

**[6] 推荐修复**
按 §3.3 的降级链：filter → 包装实例 `handle` → 禁用 + stderr 告警 + 健康位。
另外：
- `setup_logging` 检查 `install_on_handlers()` 的返回与
  `redaction_health()["blocked_handlers"]`，非 0 时 `logger.error`（只含 repr）；
- `Runtime.status()`（`runtime.py:41-64`）增加
  `"log_redaction": {"mode": ..., "blocked_handlers": n, "hooks_intact": bool}`。

**[7] 副作用**
- 极端情况下会**静默丢弃**某类日志（level 调到 100）——这是有意的 fail-closed，
  必须同时写 stderr 告警，否则排障困难。
- 包装实例 `handle` 会改变 `isinstance`/签名反射的结果（`inspect.signature` 可能变），
  对把 handler 当对象做内省的库有轻微影响。

**[8] 必须新增的回归测试**
- `test_install_failure_falls_back_to_wrapper`：`filters=()` → 输出仍打码；
- `test_install_failure_blocks_handler_when_wrapper_fails`：
  连 `handle` 都不可写 → 输出为空 + `blocked_handlers == 1`；
- `test_setup_logging_reports_blocked_handler`：`caplog`/stderr 捕获到告警。

**[9] 接口/兼容**
`install_on_handler` 仍返回 bool，但语义变为"已处于不会明文输出的状态"；
新增 `redaction_health()` 只读函数（不涉及业务接口）。

**[10] 验收证明**
`probe_f2.py 13` 的 `LEAK=False`；`redaction_health()` 返回
`{"mode": "wrapper", "blocked_handlers": 0}`。

---

### 4.8 `SR-F2-05` — `install_global_hook` 被覆盖后无法自愈（P3）

**[1] 原始描述**
安装后若第三方把 `logging.Handler.handle` 重新赋值，钩子被摘掉；
再次调用 `install_global_hook()` 因 `_ORIGINAL_HANDLE is not None` 直接返回 `False`，
新建 handler 从此不再受保护（已装 filter 的旧 handler 仍安全）。

**[2] 真实触发路径**
测试里的 `mock.patch("logging.Handler.handle")`、APM/追踪库、热重载框架。

**[3] 文件与行号**
`src/ai_trader/log_redaction.py:525-528`（幂等判据是"曾经装过"而不是"现在还在"）。

**[4] 最小复现**
`probe_f2.py 5`：`setup_logging()` 后 `logging.Handler.handle = std_handle`，
再 `install_global_hook()` → `False`，新建 handler 输出 `LEAK=True`。
`probe_f2.py 12` 证明存量 handler 仍安全。

**[5] 根因**
用"模块级布尔"表达"钩子存在"，而钩子的真实状态在 `logging.Handler.handle`
这个可变属性上。状态与事实分离。

**[6] 推荐修复**
1. 给包装函数打标：`_handle._aitrader_redaction = True`；
2. 幂等判据改为**检查当前属性**：
   ```python
   def install_global_hook(force: bool = False) -> bool:
       with _INSTALL_LOCK:
           current = logging.Handler.handle
           if getattr(current, "_aitrader_redaction", False) and not force:
               return False
           _ORIGINAL_HANDLE = current if not getattr(current, "_aitrader_redaction", False) \
                             else _ORIGINAL_HANDLE
           logging.Handler.handle = _make_wrapper(_ORIGINAL_HANDLE)
           return True
   ```
3. 新增 `ensure_hooks() -> dict`：同时校验 `Handler.handle` 与 `Logger.handle`，
   不一致就重装；由 `setup_logging()` 与 `Runtime.status()` 调用；
4. 模块重载：`_make_wrapper` 检查 `getattr(original, "_aitrader_redaction", False)`，
   若已是旧包装则先解包到真正的原始实现，避免每 reload 加一层。

**[7] 副作用**
- `Runtime.status()` 会触发一次 hook 校验（有锁，极短）；
- 若第三方**故意**替换 `Handler.handle` 并在之后依赖自己的实现，
  我们的重装会把它包在外层而不是丢弃它（链式调用，行为保留）。

**[8] 必须新增的回归测试**
- `test_hook_recovers_after_overwrite`：覆盖后 `ensure_hooks()` 返回
  `{"handler": True}`，新建 handler 打码；
- `test_reload_does_not_stack_wrappers`：`importlib.reload` 两次后，
  调用链深度不增加（用计数器断言）；
- `test_ensure_hooks_is_idempotent`：连续调用返回 `False/False`。

**[9] 接口/兼容**
新增 `ensure_hooks()`、`install_logger_hook()`；`install_global_hook` 增加
可选 `force` 参数（默认 False，向后兼容）。

**[10] 验收证明**
`probe_f2.py 5` 的 `LEAK=False`，且 `reinstall() -> True`。

---

### 4.9 `SR-A1-02` — `MarketPushAPI(client)` 类级构造绕过哨兵（P3）

**[1] 原始描述**
适配器只把 `client.market_push` 实例属性替换成哨兵（`adapter:609-634`）。
任何持有 `broker.client` 的代码都可以
`from fsopenapi.api.market_push import MarketPushAPI; MarketPushAPI(client).connect()`，
绕过门禁/路由校验，直接 `get_valid_session()` 并连硬编码生产地址
`wss://openapi-push.fosunxcz.com/msg/v1/push`（`market_push.py:37`）。

**[2] 真实触发路径**
仓库内无调用点；但 `broker.client` 是公开属性，
`cli.py:253` 已经在做 SDK 能力盘点（`getattr(fsopenapi, "MarketPushAPI", None)`），
说明"拿到类"是容易的。

**[3] 文件与行号**
- 哨兵：`src/ai_trader/broker/fosun_adapter.py:284-311`、`:609-634`
- SDK 类：`sdk_src/fsopenapi/api/market_push.py:34-42`（`class MarketPushAPI` / `async def connect`）
- 类被创建处：`sdk_src/fsopenapi/__init__.py:40`（`self.market_push = MarketPushAPI(self)`）

**[4] 最小复现**
`probe_b1_a1.py a1`：

```python
api = MarketPushAPI(broker.client)
asyncio.run(api.connect())     # websockets.connect 被探针替换为立即抛错
```

实测：`get_valid_session = 1`、`触发 websockets.connect = True`。

**[5] 根因**
保护对象是**实例属性**，而被保护的**能力**在类上。
实例属性可以被重新赋值，也可以被绕过——只要有人 `new` 一个。

**[6] 推荐修复（不改 SDK 源码，运行时拦截）**
在 `_install_market_push_guard()` 中，除替换实例属性外，再做一次
**类级方法拦截**（只拦行为，不改文件）：

```python
from fsopenapi.api.market_push import MarketPushAPI as _MPA

_BLOCKED_METHODS = ("connect", "subscribe", "unsubscribe", "close", "send_heartbeat")
_ORIGINAL_MPA = {}     # name -> original function

for name in _BLOCKED_METHODS:
    orig = getattr(_MPA, name, None)
    if orig is None or getattr(orig, "_aitrader_blocked", False):
        continue
    def _blocked(self, *a, __name=name, **kw):
        raise BrokerError(f"禁止直接调用 SDK 行情推送接口 market_push.{__name}（A-1）…",
                          code="FEED_AUTH_BYPASS")
    _blocked._aitrader_blocked = True
    _ORIGINAL_MPA[name] = orig
    setattr(_MPA, name, _blocked)
```

关键约束：
- **不能拦 `__init__`**：`SDKClient.__init__` 自己会 `MarketPushAPI(self)`
  （`__init__.py:40`），拦构造会让 SDK 客户端无法创建；
- 拦截必须**幂等**，并在 `release_credentials()`/`close()` 时按 `_ORIGINAL_MPA` 精确还原；
- 若 SDK 已被其他代码 import 并持有类引用，方法替换仍然生效
  （替换的是类属性，所有实例共享）；
- 若替换失败（`__slots__`/只读类）→ `_init_client` fail-closed（沿用 `:688-692` 逻辑）。

**[7] 副作用**
- **进程级副作用**：同一进程内任何代码都无法再使用 SDK 推送（这正是目标），
  但会影响共用该进程的其它 SDK 使用者；
- 还原必须精确，否则测试之间会串味（需 autouse fixture 负责 restore）；
- 若未来项目真要走 `MarketPushAPI`，必须先移除拦截——需要显式设计，不能默认放开。

**[8] 必须新增的回归测试**
- `test_sdk_market_push_class_is_blocked`：直接 `MarketPushAPI(client).connect()`
  → `BrokerError(code=FEED_AUTH_BYPASS)`，且 `get_valid_session` 计数为 0；
- `test_sdk_client_construction_still_works`：`SDKClient(...)` 可构造
  （证明没拦 `__init__`）；
- `test_guard_restore_on_release`：`release_credentials()` 后方法被还原；
- `test_guard_is_idempotent`：重复安装不叠加包装。

**[9] 接口/兼容**
无公开接口变更；新增模块级 `_ORIGINAL_MPA` 与还原函数。
**必须声明**：这是运行时补丁，不是 SDK 修复；若 SDK 升级改变方法名，
拦截会静默失效——需在 `redaction`/`health` 里暴露 `market_push_guard` 的检查结果
（现有 `adapter:1402-1403` 已有 `market_push_guard`，扩展为三项：
`instance_guard / class_guard / methods_blocked`）。

**[10] 验收证明**
`probe_b1_a1.py a1` 的"直连构造"分支变为抛 `FEED_AUTH_BYPASS`、
`get_valid_session = 0`、`触发 websockets.connect = False`。

---

### 4.10 `SR-F2-07` — `record.message` 死分支（P3）

**[1] 原始描述**
`_STANDARD_ATTRS` 含 `"message"`，extra 循环在 `:399-401` 先 `continue`，
`:405-406` 的 `elif key == "message"` 永远不可达。
若某个 Formatter 预先设置了 `record.message`，脱敏不会处理它。

**[2] 真实触发路径**
两个 handler 串联：前一个 handler 重写 `handle`/`format` 并调用
`record.getMessage()` 写入 `record.message`（`logging.Formatter.format` 会写
`record.message`），后一个 handler 的 Formatter 直接读 `record.message` 而不重算。
当前仓库无此类 Formatter。

**[3] 文件与行号**
`src/ai_trader/log_redaction.py:358-363`（`message` 在集合里）、
`:398-408`（循环与死分支）。

**[4] 最小复现**
`probe_f2.py 7`：

```python
rec = logging.LogRecord("x", logging.WARNING, __file__, 1, "clean", None, None)
rec.message = "api_key=" + SECRET
RedactionFilter().filter(rec)
# rec.message 仍含 SECRET，LEAK=True
```

**[5] 根因**
`message` 被误当成"logging 自己维护的标准属性"。实际上它是
`Formatter.format()` 计算出来的**缓存字段**，可以来自任何地方；
把它排除在脱敏范围外，等于信任"这个字段一定是安全的"。

**[6] 推荐修复**
1. 从 `_STANDARD_ATTRS` 移除 `"message"`（`:362`）；
2. 删掉 `:405-406` 的 `elif`，让 `message` 走通用 `redact_obj` 分支
   （`redact_text` 对字符串幂等）；
3. `_fail_closed`（`:411-427`）里对 `message` 的处理同步简化为通用分支。

注意：`_STANDARD_ATTRS` 移除 `message` 后，`_fail_closed` 的循环会把 `message`
置为 `***`，随后 `:423-424` 再覆盖为 `_PLACEHOLDER` —— 顺序需保持，避免出现
"占位符被二次覆盖成 `***`"的歧义。

**[7] 副作用**
- 每条记录多处理一个字段（可忽略）；
- 若某 Formatter 依赖"`message` 是原始字符串"（几乎不可能），行为会变。

**[8] 必须新增的回归测试**
- `test_preset_message_is_redacted`：场景 7 的断言反转为 `LEAK=False`；
- `test_formatter_message_cache_still_works`：标准 Formatter 输出仍正常
  （证明没把 `message` 逻辑弄坏）。

**[9] 接口/兼容**
无。

**[10] 验收证明**
`probe_f2.py 7` 的 `LEAK[preset-message]=False`，且场景 8 全链路仍 `LEAK=False`。

---

## 5. 文件修改清单

| 文件 | 类型 | 预计变更 | 涉及条目 | 风险 |
|---|---|---|---|---|
| `src/ai_trader/log_redaction.py` | 重构 | +260 / −45 | F2-01/02/03/04/05/07/08 | 高（核心） |
| `src/ai_trader/runtime.py` | 小改 | +8 / −2 | F2-04/05/08 | 低 |
| `src/ai_trader/config.py` | 小改 | +18 / −2 | B1-04 | 中（来源语义） |
| `src/ai_trader/broker/fosun_adapter.py` | 中改 | +150 / −25 | B1-04/05、A1-02 | 高（身份/网络） |
| `src/ai_trader/market/feed.py` | **不改** | 0 | — | — |
| `tests/test_log_redaction_b411.py` | 新增 | ~380 行 / 20 例 | F2 全部 | 低 |
| `tests/test_credential_identity_b411.py` | 新增 | ~260 行 / 10 例 | B1-04/05 | 低 |
| `tests/test_market_push_guard_b411.py` | 新增 | ~160 行 / 6 例 | A1-02 | 低 |
| `US_P1_BATCH4_1_1_FIX_REPORT.md` | 新增 | 实施报告 | — | — |

**明确不修改**：`.env`、`config/config.yaml`、`data/**`、`sdk_src/**`、
`requirements.txt`、`src/ai_trader/secrets.py`、`tests/helpers.py`；
不删除、不覆盖任何既有审计证据与报告。

---

## 6. 逐文件修改设计

### 6.1 `log_redaction.py`

| 区块 | 现状行号 | 改动 |
|---|---|---|
| 常量 | `:73-94` | 新增 `MAX_IDENT_LEN=64`、`_TOKEN_SUFFIX_DENY`；`redact_text` 增加 `truncate` 参数 |
| 规则表 | `:101-124` | 删除固定 `session` 分支；新增"标识符回调"规则与转义 JSON 规则；URL 规则改回调 |
| 敏感表 | `:129-136` | 扩展为 `_SENSITIVE_TOKENS`，文本/extra 共用 |
| 判定 | `:141-145` | 新增 `_is_sensitive_token()`（归一化 + 后缀白名单） |
| `_sanitize_record` | `:366-408` | exc_info 中和；`message` 移除出 `_STANDARD_ATTRS`；打标 `_aitrader_redacted` |
| 安装 | `:464-513` | 降级链（filter → wrapper → blocked）；`redaction_health()` |
| 钩子 | `:516-543` | 打标 + 按当前属性判幂等 + `ensure_hooks()` + `install_logger_hook()` + `Handler.format` 兜底 |

代码骨架（关键片段，非最终实现）：

```python
def _sensitive_kv(match: re.Match) -> str:
    ident, sep, value = match.group("ident"), match.group("sep"), match.group("value")
    if not _is_sensitive_token(ident):
        return match.group(0)
    return f"{ident}{sep}***"

_IDENT_KV = re.compile(
    r"(?P<ident>[A-Za-z_][A-Za-z0-9_.\-]{0,63})(?P<sep>\s*[:=]\s*)"
    r"(?P<value>\"[^\"\r\n]{1,4096}\"|'[^'\r\n]{1,4096}'|[^\s,;>]{1,4096})")
```

> 量词上界（`{1,4096}`）是必须的：无上限的 `[^"]*` 在超长文本上会退化为 O(n²)。
> 所有新规则都必须有上界，并复用 `MAX_RAW_TEXT_CHARS`（`:224`）的硬闸。

### 6.2 `runtime.py`

```python
def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(...)                 # 参数不动
    installed = install_on_handlers()
    install_global_hook()
    install_logger_hook()
    install_format_guard()
    health = ensure_hooks()
    if health["blocked_handlers"]:
        logger.error("日志脱敏不可用，已禁用 %d 个 handler（详见 redaction_health）",
                     health["blocked_handlers"])
```

`Runtime.status()` 增加只读字段：

```python
"log_redaction": redaction_health(),   # {"handler_installed": n, "blocked_handlers": k,
                                      #  "hooks_intact": bool, "mode": "strict"}
```

### 6.3 `config.py`

只加"记录 dotenv 键名"的最小能力，不改解析逻辑、不改默认值：

```python
_DOTENV_KEYS: set[str] = set()

def dotenv_keys() -> frozenset[str]: ...
def _reset_dotenv_keys() -> None: ...   # 测试夹具用
```

### 6.4 `fosun_adapter.py`

| 区块 | 现状行号 | 改动 |
|---|---|---|
| 注册表 | `:151-281` | 新增 `resolve_and_acquire()`（一次加锁完成"判定 + 写入"），消除 TOCTOU |
| `_SDK_ENV_MAP` | `:452-455` | 加入 `FSOPENAPI_API_KEY`，三要素对称 |
| `_resolve_credentials` | `:474-551` | 引入 `_CredSource`；优先级改为 explicit > vault > dotenv > injected；同源校验；删除静默复用 |
| `_inject_sdk_env` | `:553-590` | 改用 `resolve_and_acquire`；部分失败自动回滚本 owner |
| `release_credentials` | `:592-607` | 保持引用计数语义；新增类级守卫还原 |
| `_install_market_push_guard` | `:609-634` | 追加类级方法拦截 + 还原表 |
| `health` | `:1383-1433` | 新增 `credential_sources` / `credential_mixed` / `market_push_guard` 三项明细 |
| `close()` | 新增 | 释放凭证 + 还原类级守卫；`__del__` 只做 best-effort（不抛异常） |

### 6.5 `feed.py`

**不改。** 理由：兼容导出与 `register_feed_secrets` 已足够；改动会破坏 C-1/D-1
的"零改动"证据链。唯一相关风险（会话值注册早于 SDK 日志）属于 SDK 侧时序，
不在本文件修复范围（见第 9 节）。

---

## 7. 测试矩阵

| 测试 ID | 文件 | 对应条目 | 断言要点 | 修复前 | 修复后 |
|---|---|---|---|---|---|
| T01 | `test_log_redaction_b411.py` | F2-01 | 5 种字段名变体全部 `***` | FAIL | PASS |
| T02 | 同上 | F2-01 | `token_count=5` / `session_timeout=30` 不误伤 | PASS | PASS |
| T03 | 同上 | F2-02 | 1 层/2 层转义 JSON 打码且仍是合法 JSON | FAIL | PASS |
| T04 | 同上 | F2-03 | `record.exc_info is None`；`JsonFormatter.exception` 不含原文 | FAIL | PASS |
| T05 | 同上 | F2-03 | 标准 Formatter 仍打印 `Traceback` + 异常类型 | PASS | PASS |
| T06 | 同上 | F2-04 | 自定义 `handle` 的 handler 输出打码 | FAIL | PASS |
| T07 | 同上 | F2-04 | 直接 `handler.handle()` + 自定义 `format` 的**已知边界**用例（应泄露） | PASS | PASS（文档化） |
| T08 | 同上 | F2-05 | 覆盖 `Handler.handle` 后 `ensure_hooks()` 自愈 | FAIL | PASS |
| T09 | 同上 | F2-05 | reload 两次包装层数不增 | — | PASS |
| T10 | 同上 | F2-07 | 预置 `record.message` 被打码 | FAIL | PASS |
| T11 | 同上 | F2-08 | `filters=()` → wrapper 生效，输出打码 | FAIL | PASS |
| T12 | 同上 | F2-08 | wrapper 也失败 → 输出为空 + `blocked_handlers=1` | FAIL | PASS |
| T13 | 同上 | F2 回归 | `msg/args/extra/lastResort/filter 顺序` 全部保持 | PASS | PASS |
| T14 | 同上 | F2 限额 | 2 MiB 丢弃、256 KiB 截断、深度/节点上限 | PASS | PASS |
| T15 | `test_credential_identity_b411.py` | B1-04 | dotenv API_KEY 不再压过 Vault | FAIL | PASS |
| T16 | 同上 | B1-04 | 混合来源默认 `CREDENTIAL_SOURCE_MIX` fail-closed | FAIL | PASS |
| T17 | 同上 | B1-04 | `policy=warn` 时 `credential_mixed=true` | — | PASS |
| T18 | 同上 | B1-05 | 空 Vault（干净 env）fail-closed，owners 不增 | PASS | PASS |
| T19 | 同上 | B1-05 | 空 Vault（dotenv env）fail-closed，owners 不增 | FAIL | PASS |
| T20 | 同上 | B1-05 | 显式开关开启且三要素齐全才复用 + 告警 | — | PASS |
| T21 | 同上 | B1 回归 | 双 Vault 冲突仍 `CredentialConflict`，env 不变、owners=1 | PASS | PASS |
| T22 | 同上 | B1 释放 | A 释放不影响 B 持有；归零才清理 | PASS | PASS |
| T23 | 同上 | B1 并发 | 2 线程同时初始化不同凭证：一个成功、一个冲突，无 env 交错 | — | PASS |
| T24 | `test_market_push_guard_b411.py` | A1-02 | `MarketPushAPI(client).connect()` 抛 `FEED_AUTH_BYPASS` | FAIL | PASS |
| T25 | 同上 | A1-02 | `get_valid_session` 计数 0、`websockets.connect` 未触达 | — | PASS |
| T26 | 同上 | A1-02 | `SDKClient(...)` 构造不受影响 | PASS | PASS |
| T27 | 同上 | A1-02 | `release_credentials()` 后类方法还原 | — | PASS |
| T28 | 同上 | A1 回归 | 实例哨兵 `client.market_push` 仍为 `_MarketPushDisabled` | PASS | PASS |
| T29 | 既有全量 | 回归 | `517 passed, 1 skipped` | PASS | PASS |

**测试隔离要求**：新增用例必须使用 autouse fixture 还原
`logging.Handler.handle` / `logging.Logger.handle` / `logging.Handler.format`
与 `_CRED_ENV` / `_DOTENV_KEYS`，避免像现有 `root_capture`（`:30-45`）那样把
全局 hook 留在 pytest 进程里（`SR-TEST-01`）。

---

## 8. 备份与回滚

### 8.1 备份

实施前创建 `backups/us_p1_b411_<YYYYMMDD-HHMMSS>/`，结构与既有备份一致：

```
backups/us_p1_b411_20261109-HHMMSS/
├── MANIFEST.sha256.json          # 路径 / 字节 / 行 / EOL / SHA256
└── src/ai_trader/{log_redaction.py,runtime.py,config.py,broker/fosun_adapter.py}
```

同时把 `backups/us_p1_b41_20261109-203830/` 作为**二级基线**保留（不覆盖）。

### 8.2 回滚

| 场景 | 动作 |
|---|---|
| 单文件回退 | `cp backups/us_p1_b411_*/<path> <path>` + 重跑 T13/T21/T29 |
| 全量回退 | 按 MANIFEST 逐文件还原 + `sha256sum -c` 校验 + 全量测试 |
| 仅回退 B-1 行为 | 设 `fosun.credential_source_policy=legacy`（代码内置开关，不改 yaml） |
| 仅回退 A-1 类级拦截 | 设 `fosun.block_sdk_market_push_class=false`（默认 true） |
| 测试污染 | `_CRED_ENV.clear()` + `_reset_dotenv_keys()` + 还原三个 logging 钩子 |

**回滚不删除**任何审计证据、报告或备份目录。

---

## 9. 风险与未解决边界

### 9.1 在不修改 `sdk_src/**` 的前提下**无法 100% 封堵**的项

| # | 项 | 能做到什么 | 为什么封不死 |
|---|---|---|---|
| 1 | `MarketPushAPI` 类级构造 | 运行时替换 `connect/subscribe/unsubscribe/close`（§4.9） | 若调用方在替换前已 `from ... import MarketPushAPI` 并保存了**未绑定方法引用**（`f = MarketPushAPI.connect`），或 SDK 升级改方法名，拦截失效；无法在不改源码的前提下保证全覆盖 |
| 2 | SDK 自带日志/Formatter | 中和 `exc_info` + `Handler.format` 兜底 | 若使用方绕过 `logging` 直接 `print`/写文件（SDK 示例即如此），F-2 不可见 |
| 3 | SDK 会话值的注册时序 | 靠 extra 键名打码 | `get_valid_session()` 在 `feed_auth_headers` 返回**之前**就可能记录 session（`auth.py:97-100`）；此时 `register_feed_secrets`（`feed.py:785`）还没执行。要彻底解决需在 SDK 内注册，属改 SDK |
| 4 | 第三方 Handler 完全自定义 | L2 + L3 覆盖"经 logger 分发"与"调 self.format" | 若 handler 既重写 `handle`、又重写 `format`、还绕过 logger 被直接驱动 → 无法拦截（T07 显式记录该边界） |
| 5 | 非 logging 输出 | 无 | `print`、`FaultRecorder`（`faults.py:140-155`）、`vault get --show`（`cli.py:305-306`）、`warnings.warn`、子进程输出，均不在 F-2 范围 |

**结论**：F-2 修复后应表述为"覆盖所有**经由 Python logging 且经标准分发链**的输出"，
不得表述为"覆盖所有输出"。

### 9.2 行为变更风险（需人工批准）

| 风险 | 影响 | 缓解 |
|---|---|---|
| 凭据来源优先级变更（B1-04） | 下次真实启动可能连到**不同账户** | `credential_source_policy=legacy` 开关 + 上线前只读对比来源 |
| `exc_info` 置空（F2-03） | 依赖 `exc_info` 的 APM/Sentry 拿不到异常对象 | 提供 `safe_exception` 字符串 + 变更说明 |
| 空 Vault 不再复用（B1-05） | 多 worker 部署开始 fail-closed | 显式开关 `allow_injected_credential_reuse` |
| 安装失败改为拒绝输出（F2-08） | 极端 handler 会丢日志 | stderr 告警 + `redaction_health` 暴露 |
| 类级拦截（A1-02） | 同进程其它 SDK 使用者受影响 | 默认开启但可配置关闭 + 精确还原 |

### 9.3 性能风险

- L2 使每条记录多跑一次 filter；L3 再跑一次字符串脱敏（已由
  `_aitrader_redacted` 标记短路）。需基准：1 万条含 1KB extra 的记录，
  目标总耗时增量 < 15%。
- 新正则全部带量词上界；禁止 `.*` 跨行贪婪匹配（PEM 规则 `:103-104` 已是 `.*?`，
  需评估在超长文本上的回溯——建议加 `MAX_RAW_TEXT_CHARS` 前置闸，已有）。

---

## 10. 实施顺序

| 阶段 | 内容 | 门槛（必须全绿才进入下一阶段） |
|---|---|---|
| **P0 基线** | 建备份 + MANIFEST；确认三开关；跑一次全量（517/1） | 备份校验通过；全量绿 |
| **P1 F-2 核心** | `log_redaction.py`：规则归一化（F2-01/02）、exc 中和（F2-03）、message（F2-07）、安装降级（F2-08） | T01–T05、T10–T12 绿；T13/T14 不回归 |
| **P2 F-2 边界** | `Logger.handle` 钩子、`Handler.format` 兜底、`ensure_hooks`（F2-04/05）；`runtime.py` 接线 | T06–T09 绿；T07 已知边界用例按预期 |
| **P3 B-1** | `config.py` dotenv 键名 + `adapter.py` 来源模型/同源校验/去复用 | T15–T23 绿；`probe_b1_a1.py b1e` 复跑通过 |
| **P4 A-1** | 类级方法拦截 + 还原 + health 明细 | T24–T28 绿；`probe_b1_a1.py a1` 复跑通过 |
| **P5 收口** | 全量测试、四份探针全跑、写 `US_P1_BATCH4_1_1_FIX_REPORT.md`、更新哈希清单 | 517+新增 全绿；探针 0 泄露；报告哈希与实物一致 |

每个阶段结束都必须：
1. 重跑**上一阶段**的门槛测试（防回归）；
2. 复跑对应探针，把"修复前/修复后"输出并排记录；
3. 更新 SHA256 清单（含 EOL/BOM）。

---

## 11. 明确无法完全封堵的清单（汇总）

1. **`MarketPushAPI` 类级调用**：只能运行时替换方法；预存的方法引用、SDK 升级改名、
   子解释器导入均可能绕过。
2. **SDK 直接输出**：`sdk_src/example/**` 用 `print`/自建 handler，不经本项目 logging。
3. **会话值注册时序**：`get_valid_session()` 的日志早于 `register_feed_secrets`；
   当前仅靠 SDK 自己的 `_PARTIAL_MASK_FIELDS` + 本项目 extra 键名打码。
4. **第三方 Handler 全自定义且绕过 logger**：T07 已把该边界写成显式用例。
5. **非 logging 输出**：`print`、`FaultRecorder` 文件/stderr、`vault get --show`、
   `warnings`、子进程。

以上 5 项必须在修复报告的"诚实边界"章节逐条列出，
**不得**用"已统一脱敏"一笔带过。

---

*本轮为只读前置分析，未修改任何代码/测试/配置/报告。实施须经人工审核后另开批次。*
