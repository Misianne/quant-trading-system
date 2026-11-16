# US-P1 Batch 4.1.4 — 修复准备（Remediation Plan）

| 项目 | 值 |
|---|---|
| 阶段 | **修复准备（只读）**——本文件只是计划，**未执行任何代码修改** |
| 输入依据 | `US_P1_BATCH4_1_3_INDEPENDENT_AUDIT.md`、`audit_evidence/us_p1_b41_3_audit.json`、`audit_evidence/us_p1_b41_3_probe_results.json` |
| 源码基线 | `log_redaction.py` = `720b4f33ac98f7f09a95fddc1ca5244f3f17b577f77f38aa24825874f7556b4a`<br>`config.py` = `ddb8144c37b1d23806c1a8bc44753eade1f64f5b1927fdfdf1c7fd4ba0efe8e0` |
| 复核时间 | 2026-11-10 14:36 – 14:44（本地） |
| 范围 | 4.1.3 的四条 P1，以及任务点名的 F2-03 / F2-02 两个同源问题 |
| 结论 | 4 条 P1 **全部可复现**；修复设计完成；**建议按 F2-02 → F2-04 → F2-03 → B1-05 顺序实施** |

---

## 0. 一页结论

| 项 | 严重级别（4.1.3 原判） | 复核后 | 修复性质 | 预计改动量 |
|---|---|---|---|---|
| **F2-04-01** 外层键吞并内层赋值 | P1 | **P1 维持**（仅对**未登记**值生效） | 规则回调改造 | 中（1 个扫描器） |
| **F2-04-02** JSON 转义引号失配 | P1 | **P1 加重**（**已登记**值也会泄露） | 规则替换 | 中 |
| **F2-04-03** 多行值只打码首行 | P1 | **P1 维持**（完整 PEM 被 PEM 规则兜住；部分 PEM/非 PEM 多行值泄露） | 值域扩展 | 小 |
| **F2-03-01/02** 可伪造标记 | P2 | **P2 维持**（正常路径可达，当前无调用点） | 删除标记 + L3 自校验 | 中 |
| **F2-02-01** `__module__` 污染信任锚 | P3 | **P3 维持** | 判据改为对象身份 | 小 |
| **B1-05-01** 多行 PEM 截断 | P1 | **P1 维持，影响面收窄**（生产 `key_source: vault` 优先于 dotenv） | 解析器重写 + fail-closed | 中大 |

> 三条复核新事实（§2.4）改变了影响面判断，但**不改变任何 P1 的定级**：
> ① 已登记字面量有"替换兜底"，F2-04-01 只对未登记值生效；
> ② 但转义形态（F2-04-02）**能击穿**这个兜底，已登记的含引号凭据照样泄露；
> ③ B1-05 的生产路径是 vault，`.env` 是次路径——定级不变，紧急度排序下调。

**条目映射（避免把"4 条 P1"与"4 个主题"混为一谈）**

* 4.1.3 的 **4 条 P1** = `AUD3-F2-04-01`、`AUD3-F2-04-02`、`AUD3-F2-04-03`、`AUD3-B1-05-01`。
* 任务点 4 另外点名要设计 **F2-03（P2，2 条）** 与 **F2-02（P3，1 条）** 的修复。
* 因此本计划覆盖 **6 条发现 / 4 个主题**；§0 表格按主题列出，§10 风险按主题登记。

**修复期间必须保持的安全前提**（当前实测值，实施时逐项复查）：

```
live_trading = false      order_execution_enabled = false
paper_trading = true      kill_switch = false
can_connect_broker = false   can_send_real_order = false
```

---

## 1. 执行边界（本阶段已遵守）

* 未修改 `src/`、`tests/`、`config/`、`.env`、数据库、部署环境中的任何文件。
* 未覆盖 `audit_evidence/` 下任何既有证据；本文所有复核均以**内联脚本**执行，不落盘。
* 未连接真实券商、未访问 VPS、未创建真实 Session、未发送订单。
* 所有凭据为 `FAKE-PLAN-*` 假值；未读取真实 `.env` 内容。

---

## 2. 证据复核

### 2.1 复核方法

对每条 P1 用 4.1.3 的探针公共库（`audit_evidence/us_p1_b41_3_probes/_probe_lib.py`）重跑最小用例，
真实 `logging` handler 捕获输出。源码 SHA256 与 4.1.3 审计时**逐字节相同**，故结论可继承。

### 2.2 F2-04 三条 P1（重跑结果）

| 用例 | 输入 | 实际输出（复核当时） | 泄露 |
|---|---|---|---|
| F2-04-01 | `log.exception("request failed")`，异常消息 `api_key=FAKE-PLAN-APIKEY-1a2b3c4d` | `RuntimeError: api_key=FAKE-PLAN-APIKEY-1a2b3c4d` | ✅ 复现 |
| F2-04-02 | `payload={"api_key":"FAKE-PLAN-\"q\"-7g8h"}` | `payload={"api_key":"FAKE-PLAN-\"q\"-7g8h"}` | ✅ 复现 |
| F2-04-03 | `api_key=FAKE-PLAN-A\nFAKE-PLAN-PART2-5e6f` | `api_key: ***\nFAKE-PLAN-PART2-5e6f` | ✅ 复现 |
| F2-04-04(P2) | `api_key=FAKEWITH SPACE-9z` | `api_key: *** SPACE-9z` | ✅ 复现（后缀残留） |

### 2.3 B1-05（重跑结果）

临时目录假 `.env`，`AUD3PLAN_PEM="-----BEGIN PRIVATE KEY-----\n<384B 随机 base64>\n-----END PRIVATE KEY-----"`：

```
expect_len 566   actual_len 27
actual     '-----BEGIN PRIVATE KEY-----'
sha_expected 5431e2597df1f936   sha_actual 3021d90eb9437b2d
```

与 4.1.3 一致：**566 字符的合法 PEM 被截成 27 字符，且与"缺 END"的畸形输入得到同一个 sha**。

### 2.4 复核中发现的新事实（必须写进修复设计）

#### 事实 A：已登记字面量有"替换兜底"，F2-04-01 只打穿**未登记**值

`redact_text` 在跑完全部规则后，还会对登记表做一次 `out.replace(secret, "***")`。
实测：

| 场景 | 是否登记 | 输出 | 是否泄露 |
|---|---|---|---|
| traceback `api_key=<TOK>` | 是 | `RuntimeError: api_key=***` | ❌ 不泄露 |
| traceback `api_key=<TOK>` | 否 | `RuntimeError: api_key=FAKE-…` | ✅ 泄露 |

**影响**：生产凭据经 `_register_secret_values()`（`fosun_adapter.py:650-663`）注册，
因此 broker 三要素走 traceback 时会被兜底救回。真正暴露的是**未登记**的凭据：

* `AI_API_KEY`（`ai/providers.py:377-378`）——`ai/` 目录**没有任何** `register_secret` 调用；
* `WEB_SESSION_SECRET`、`WEB_ADMIN_PASSWORD`（`web/` 同样未登记）；
* `VAULT_PASSPHRASE`（只在 CLI 参数与 `secrets.py` 出现）；
* feed 的 `X-Api-Key`/`X-Session` 只在 headers 构造**之后**才登记（`feed.py:85-86`），
  构造前的失败不会被兜底。

#### 事实 B：转义形态**击穿**登记兜底 → F2-04-02 加重

登记的是未转义串 `FAKE-PLAN-"q"-7788`，日志里是转义串 `FAKE-PLAN-\"q\"-7788`，
`str.replace` 找不到 → 兜底失效：

```
[JSON-registered] 'payload={"api_key": "FAKE-PLAN-\\"q\\"-7788"}'   leaked=True
```

**结论：F2-04-02 是本轮唯一"即使凭据已登记仍会泄露"的路径**，修复优先级最高。

#### 事实 C：完整 PEM 被 `_PEM_RE` 先行覆盖，B1-05 的日志侧风险限于**部分 PEM**

`_RULES` 顺序是 `_PEM_RE → _AUTH_RE → _BEARER_RE → _JSON_RE → _KV_RE → _URL_RE`。

| 场景 | 输出 | 结论 |
|---|---|---|
| 已登记 + 完整 PEM + `api_key=` | `api_key: ***` | 被 `_PEM_RE` 整块吃掉 |
| **部分 PEM（缺 END）+ `api_key=`** | `api_key: *** PRIVATE KEY-----\nFAKEPLANBODY…` | **正文泄露** |

#### 事实 D：B1-05 的生产路径是 vault，`.env` 是次路径（影响面收窄）

`config/config.yaml:138` → `key_source: "vault"`；`_classify_source()`
（`fosun_adapter.py:831-884`）的取值优先级是
`explicit_env > vault > dotenv > injected`。
`config.yaml` 中 PEM 字段数为 **0**，PEM 只能来自环境变量或 vault。

* vault 有值 → 使用 vault，`.env` 的 PEM 被忽略 → **B1-05 不生效**；
* vault 为空（首次部署 / 开发）→ 落到 `dotenv` → **B1-05 生效，必然失败**；
* 运维 `export` 多行 PEM → 走 `explicit_env`，shell 环境本身支持换行 → 不受影响。

**同时更正 4.1.3 报告的一处表述**：`SecretVault.inject_into_environ()` 在 `src/` 中
**没有任何调用点**（仅 `tests/test_secrets_and_gate.py:141` 使用），因此
"截断值会被 `inject_into_environ` 直接注入 SDK"这一句不成立。
截断值进入 SDK 的真实链路是：`load_dotenv` → `os.environ` → `_classify_source` → SDK。
**该更正不改变 B1-05 的定级**（静默截断 + 与畸形不可区分仍是安全缺陷），
但影响面应按"无 vault 值的 `.env` 部署"来描述。原始审计文件不修改，以本节为准。

---

## 3. 源码定位（根因 / 触发条件 / 表面症状 / 调用链 / 影响范围）

### 3.1 F2-04 — 通用键值脱敏的四个缺陷

**位置**：`src/ai_trader/log_redaction.py`

| 行 | 内容 |
|---|---|
| 171 | `_VALUE = r"(?:\"[^\"\r\n]{1,4096}\"|'[^'\r\n]{1,4096}'|[^\s,;>]{1,4096})"` |
| 173-175 | `_KV_RE = re.compile(rf"(?P<ident>{_IDENT})(?P<sep>\s*[:=]\s*)(?P<value>{_VALUE})")` |
| 179-184 | `_JSON_RE`（value 分支 `(?:[^\"'\\\r\n]|\\[^\"'\r\n]){0,4096}`） |
| 191-197 | `_kv_sub`：非敏感 ident → `return match.group(0)` |
| 199-206 | `_json_sub`：非敏感 ident → `return match.group(0)` |
| 208-213 | `_url_sub`（本次不动） |
| 221-229 | `_RULES` 顺序表 |
| 285-324 | `redact_text`：先规则、后登记字面量替换 |
| 478-484 | `_sanitize_record` 对 `exc_info` 调 `redact_exception` |
| 327-336 | `redact_exception` → `Formatter().formatException()` → `redact_text` |

**根因（四条各不相同，不能合并成一条）**

| 子问题 | 根因 | 触发条件 | 表面症状 |
|---|---|---|---|
| F2-04-01 | `_VALUE` 允许 `=`；`re.sub` 单遍左→右，外层非敏感 match 吞掉内层赋值后**原样返回**，扫描从 match 末尾继续，内层永不再被检查 | 文本形如 `外层标识符: 敏感键=值`，且值内无空格 | `RuntimeError: api_key=xxx` 明文 |
| F2-04-02 | `_JSON_RE` 的 value 分支要求 `\` 后必须跟**非引号**字符，`\"` 无法匹配；`(?P=q)` 处闭合失败 → 整个 match 失败 → 不替换（**fail-open**） | 值内出现转义引号（`json.dumps` 遇到 `"`/`'`/`\`） | 整条 JSON 明文 |
| F2-04-03 | `_VALUE` 末支 `[^\s,;>]` 的 `\s` 含 `\n`；`_KV_RE` 不跨行 | 值内含真实换行 | 首行打码、后续行明文 |
| F2-04-04 (P2) | 同一字符类排除 ` `,`,`,`;`,`>` | 值内含这些字符 | 前缀打码、后缀残留 |
| F2-04-05 (P3) | `_VALUE` 允许 `"`/`)`，敏感值把结构字符一起吞掉 | 代码片段/表达式出现在日志中 | 源码行被截断（`…f"api_key: ***`） |

**调用链**

```
log.error/exception(...)
  └─ Logger.handle (L2 hook) ──> _GLOBAL_FILTER.filter
       └─ _sanitize_record
            ├─ record.msg = redact_text(getMessage())     ← F2-04-01/03/04 命中点
            ├─ record.exc_text = redact_exception(...)    ← traceback 尾行命中点
            └─ extra.* = redact_obj(...)                  ← X08 命中点（异常对象 → "%s: %s"）
  └─ Handler.handle (L1 filter 或 L4 降级)
       └─ Handler.format (L3) ──> redact_text(text)       ← 兜底，同样受上述缺陷影响
```

**影响范围（按真实调用点）**

| 调用点 | 形态 | 风险 |
|---|---|---|
| `fosun_adapter.py:1374` `logger.error("重连失败：%s", self.last_error)` | `last_error = f"{type(exc).__name__}: {exc}"`（`:1321`、`:1373`） | **命中 F2-04-01**：`重连失败：RuntimeError: api_key=…` |
| `fosun_adapter.py:1607` `logger.warning("批量取价失败 %s: %s", s, exc)` | 取决于 `str(exc)` 是否带类名前缀 | 部分形态命中 |
| `feed.py:340/429`、`runtime.py:118/145/169/175` | 无异常对象参数 | 不命中 |
| 任何 `json.dumps` 进日志 | 含引号的值 | **命中 F2-04-02** |
| 任何多行 token / 部分 PEM | — | 命中 F2-04-03 |

**当前代码中触发点的实际数量**：`src/` 内 `log.exception` / `exc_info=True` 为 **0 处**；
`logger.*` 共 **25 处**，其中仅 `:1374`、`:1607` 传入异常对象。
→ **F2-04-01 是"潜伏缺陷 + 一个现实触发点"，F2-04-02/03 是"无现成触发点但 API 级必现"。**

### 3.2 F2-03 — 可伪造脱敏标记

**位置**：`log_redaction.py:455`、`:497-499`、`:505-507`、`:866-883`

```python
_REDACTED_FLAG = "_aitrader_redacted"
...
record.__dict__[_REDACTED_FLAG] = True          # L1 写
...
if getattr(record, _REDACTED_FLAG, False):      # L3 读
    return text
```

**根因**：L3 的"是否需要脱敏"这一安全判断，建立在一个**任何代码都能写**的 `LogRecord` 属性上；
且判断口径是"曾经脱敏过"，而不是"当前这段文本已经脱敏"。

**触发条件（由弱到强）**

1. 业务代码 / 第三方 handler 直接设置 `record._aitrader_redacted = True` → L3 放行；
2. 自定义 `LogRecord.__getattr__` 恒返回 True → 同上；
3. **正常路径**：L1 之后，后置 `Filter` 或自定义 `Formatter` 往文本里追加了新内容，
   而记录仍带旧标记 → 新内容跳过 L3；
4. `copy.copy(record)` / 复用 record 改写 `msg` → 旧标记随对象继承。

**表面症状**：`request done enriched_api_key=FAKE-…`、`rotated api_key=FAKE-…`。

**调用链**：`Logger.handle`（L2）→ `Handler.format`（L3）。L1 与 L3 之间存在
**任意第三方可插入的执行窗口**（`handler.filters` 是列表，`Formatter.format` 可被子类重写）。
只要这个窗口里追加了内容，旧标记就会让兜底失效。

**影响范围**：`src/` 内无 `addFilter` 追加敏感内容、无 `record.msg =` 改写、无自定义
`Formatter` 追加字段（`grep` 仅 `feed.py:80/:98` 安装 `_REDACTION`）→ **当前无现实通道**；
但 `install_format_guard` 是最后一道网，它的判据不该是"用户可写状态"。

### 3.3 F2-02 — `__module__` 污染信任锚

**位置**：`log_redaction.py:578-583`（`_is_stdlib_hook`）、`:590-600`（`_capture_pristine`）、
`:798-803`（`_wrap_method` 内的 `_PRESTINE[key] = current`）

```python
def _is_stdlib_hook(fn):
    mod = getattr(fn, "__module__", "") or ""
    return mod == "logging" or mod.startswith("logging.")
...
if _is_stdlib_hook(current):
    trusted = current
    _PRESTINE[key] = current        # ← 把攻击者的函数写成"可信原实现"
```

**根因**：可信性判据是**可写字符串属性** `__module__`，且判真后**覆盖**了导入期快照。
一次伪造即永久污染，后续所有 `ensure_hooks()` 都会链回被污染的实现。

**触发条件**：第三方把 `logging.Logger.handle` 换成 no-op 并设置 `__module__="logging"`。

**表面症状**：日志被完全静默（连空行都没有），而 `redaction_health()` 仍返回
`healthy=true`、`logger_hook_trusted=true`、`foreign_hooks={}`。

**影响范围**：仅影响"日志完整性/可审计性"，不直接泄露凭据。
但它是**审计链的静默失效**——比泄露更难发现。

### 3.4 B1-05 — dotenv 多行 PEM

**位置**：`src/ai_trader/config.py:508-540`

```python
for line in env_path.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, _, value = line.partition("=")
    value = value.strip().strip('"').strip("'")
```

**根因**：
1. **逐行**解析，不存在跨行状态机 → 多行值必然只剩首行；
2. `strip('"').strip("'")` 不校验引号成对、不区分单双引号、不解码转义；
3. `read_text(encoding="utf-8")` 不剥离 BOM；
4. 解析失败**无任何信号**（不抛、不记、不计数）。

**触发条件**：`.env` 中出现带真实换行的值，或带 `\n` 转义的 PEM，或文件带 BOM。

**表面症状**：环境变量值为 27 字符的 `-----BEGIN PRIVATE KEY-----`；
BOM 场景下目标键**根本不存在**。

**调用链**：`load_config()`（`config.py:623`，`use_dotenv=True` 默认）→ `load_dotenv()`
→ `os.environ` → `_classify_source()` → `_register_secret_values()` → SDK。
唯一调用点，**影响面清晰**。

**影响范围**：见 §2.4 事实 D。

---

## 4. 最小修复设计

> 原则：**不新增保护层、不改变对外 API 形状、不引入新依赖**；每个修复都必须
> 定义"无法判定时向哪边倒"（一律 fail-closed）。

### 4.1 F2-04：用"定位 + 取值扫描"替换两条正则

#### 4.1.1 为什么不是四个正则补丁

四个子问题共用 `_VALUE`，但补丁方向互相冲突：

| 补丁 | 解决 | 破坏 |
|---|---|---|
| 值域排除 `=` | 防吞并 | `api_key=abc=def` 只打码 `abc`，更糟 |
| 值域放宽到行尾 | 后缀残留、多行首行 | `api_key=xxx symbol=AAPL` 会连业务字段一起抹 |
| JSON 值允许 `\"` | 转义引号 | 正则回溯会把相邻键并成一个值（源码注释已记录该坑） |

因此改为**一次定位、一次取值**的两段式扫描。

#### 4.1.2 设计

**Step 1 — 定位（正则只负责找"键"，不再负责取值）**

```python
_KV_KEY   = re.compile(rf"(?P<ident>{_IDENT})(?P<sep>\s*[:=]\s*)")
_JSON_KEY = re.compile(rf"(?P<pre>\\{{0,15}}[\"'])(?P<ident>[A-Za-z0-9_.\-]{{1,64}})"
                       rf"(?P=pre)(?P<sep>\s*:\s*)")
```

**Step 2 — 取值（扫描器，唯一新增逻辑）**

```python
def _scan_value(text: str, pos: int) -> tuple[int, str, str]:
    """返回 (end, kind, token)；kind ∈ {quoted, bare, unterminated}。"""
    m = _QUOTE_TOKEN.match(text, pos)           # r"\\{0,15}[\"']"
    if m:
        token = m.group(0)                      # 例如 '"' / '\"' / '\\\"'
        prefix = len(token) - 1                 # 开头反斜杠个数
        i = m.end()
        while i < len(text):
            if text[i] in "\"'":
                n = _count_backslashes_before(text, i)
                # 结构引号 = 反斜杠个数与开头**完全相等**，且 token 对齐。
                # 转义引号一定比开头多（prefix+1 / prefix+3 …），故不相等。
                if n == prefix and i - prefix >= m.end() \
                        and text.startswith(token, i - prefix):
                    return i - prefix + len(token), "quoted", token
            i += 1
        return len(text), "unterminated", token
    # 裸值：到行尾；下一行若不像新字段，视为同一值的续行
    end = text.find("\n", pos)
    if end < 0:
        return len(text), "bare", ""
    lines = 1
    while lines < MAX_SCAN_LINES and _CONTINUATION.match(text, end + 1):
        nxt = text.find("\n", end + 1)
        if nxt < 0:
            return len(text), "bare", ""
        end = nxt
        lines += 1
    return end, "bare", ""
```

`_CONTINUATION = re.compile(r"(?![ \t]*[A-Za-z_][A-Za-z0-9_.\-]{0,63}[ \t]*[:=])")`：
下一行**不是**新字段 → 视为同一个值（覆盖 `api_key=PART1\nPART2`）；
是 `symbol=AAPL` 这种新字段 → 停止（避免误伤）。

**Step 3 — 打码（保留结构，只抹内容）**

```python
def _mask_span(raw: str, kind: str, token: str) -> str:
    if kind == "quoted":
        inner = raw[len(token):-len(token)]      # 去掉起止引号 token
        return f"{token}{_MASK}{token}"          # 引号保留，内容全抹
    core = raw.rstrip("\"')]}>;,")               # 裸值：结构尾巴不吞（修 F2-04-05）
    return f"{_MASK}{raw[len(core):]}"
```

**Step 4 — 失败关闭（本设计的关键）**

定位到**敏感键**但取值扫描返回 `unterminated`（到文本结尾都没有闭合）→
**从键的冒号一直打码到文本结尾**，而不是原样返回。
JSON 形态下若整条无法闭合，同样从 `:` 打码到行尾/文本尾。

**Step 5 — 重建（两种形态分别保留各自的结构）**

```python
# KV：api_key=xxx  →  api_key: ***
out = f"{ident}: {_mask_span(raw, kind, token)}"

# JSON：{"api_key":"xxx"}  →  {"api_key":"***"}
out = f"{pre}{ident}{pre}{sep}{_mask_span(raw, kind, token)}"
```

`ident` / `sep` / `pre` 均取自定位正则的命名组，原始分隔符与引号层数**原样保留**，
因此 4 层 `json.dumps` 嵌套解开后仍是合法 JSON（`test_t03` 的既有约束）。

#### 4.1.3 规则表调整

```python
_RULES = (
    (_PEM_RE, _MASK),
    (_AUTH_RE, _auth_sub),
    (_BEARER_RE, "Bearer ***"),
    (_URL_RE, _url_sub),            # 保持不变，先处理 URL 形态
)
# 新增：rules 之后、登记字面量之前
out = _redact_assignments(out)      # 内部处理 JSON 键与 KV 键
```

保留 `_JSON_RE` / `_KV_RE` 名称作为**定位**用途（不再直接 `sub` 打码），
避免外部（测试）引用断裂。

#### 4.1.4 明确不做

* 不把 `symbol`/`qty`/`price` 这类非敏感字段纳入打码（D 类文本必须保持可读）。
* 不对超长文本改变 `MAX_TEXT_CHARS` / `MAX_RAW_TEXT_CHARS` 语义。
* 不引入第三方 JSON 解析器（日志不是合法 JSON，`json.loads` 不可用）。

#### 4.1.5 已知取舍

* 敏感键的裸值放宽到行尾 → `api_key=xxx symbol=AAPL` 会连 `symbol=AAPL` 一起打码。
  这是**故意的过度打码**（安全优先），必须在测试里显式固定该行为。
* 续行判定是启发式：若续行恰好长得像 `foo=bar`，会被当成新字段而停止打码。
  缓解：对 `*PRIVATE_KEY*` / `*PUBLIC_KEY*` 这类键**跳过启发式**，一律按"整块到 END 标记或文本尾"处理。

### 4.2 F2-03：删除可伪造标记，L3 改为"自校验 + 文本摘要缓存"

#### 4.2.1 设计

1. **删除** `_REDACTED_FLAG` 常量、L1 的写入、L3 的读取、extra 遍历里的跳过分支。
2. L3 不再问"记录是否脱敏过"，而是问"**这段文本我是否已经脱敏过**"：

```python
_FORMATTED: "OrderedDict[int, tuple[logging.LogRecord, str]]" = OrderedDict()
_FORMATTED_MAX = 512
_FORMATTED_LOCK = threading.Lock()

def _format_guard(self, record):
    text = original(self, record)
    if not isinstance(text, str):
        return text
    if not _QUICK_HIT.search(text) and not _secret_prefix_hit(text):
        return text                                  # 快速否定：无候选，零成本
    digest = _digest(text)
    key = id(record)
    with _FORMATTED_LOCK:
        hit = _FORMATTED.get(key)
        if hit is not None and hit[0] is record and hit[1] == digest:
            _FORMATTED.move_to_end(key)
            return text
    out = redact_text(text, truncate=False)
    with _FORMATTED_LOCK:
        _FORMATTED[key] = (record, _digest(out))
        _FORMATTED.move_to_end(key)
        while len(_FORMATTED) > _FORMATTED_MAX:
            _FORMATTED.popitem(last=False)
    return out
```

* **持有 record 强引用**：防止 `id()` 复用导致"新记录命中旧摘要"。
* **`hit[0] is record`**：防止伪造字典项（必须拿到真对象）。
* **摘要比较的是 L3 自己产生的文本**：Formatter 追加内容 → 文本变 → 摘要不符 → 重新脱敏。
* `copy.copy` → 新 id → 未命中 → 重新脱敏。
* 记录上不再有任何标记 → 伪造无从下手。

3. **成本控制**（实测 `redact_text` ≈ 29 µs / 74 字符行）：

* `_QUICK_HIT`：一条合并正则（`api_key|token|secret|password|private|bearer |-----BEGIN|authorization|cookie`，忽略大小写），
  无命中直接返回，约 2–3 µs；
* `_secret_prefix_hit`：对登记表取前 8 字符做 `in` 判断（≤64 项），约 5 µs；
* 多 handler 扇出时同一 record 的相同文本只脱敏一次。

#### 4.2.2 明确不做

* 不把 registry 放到 record 上（否则又变成可写状态）。
* 不引入 `weakref`（无法阻止 `id()` 复用，且 LogRecord 生命周期短，强引用 + 上限更稳）。
* 不改 `redaction_health()` 的既有字段（避免测试整字典断言断裂）；如需可观测，
  另加只读函数 `format_cache_stats()`。

#### 4.2.3 残余边界（必须写进文档）

`Handler` 子类**同时重写 `handle` 与 `format`** 时，L2/L3 都绕不过去
（`test_t07`、`test_f201_..._leaks_directly` 覆盖的正是此边界）。
本修复**不改变**该边界，也不应宣称"日志绝对安全"。

### 4.3 F2-02：信任锚改为对象身份，且永不覆盖

#### 4.3.1 设计

```python
# 导入期快照：键 → 真实函数对象（不是模块名字符串）
_TRUSTED_HOOKS: Dict[str, Any] = {}
def _capture_pristine() -> None:
    for key, owner, name in _HOOK_TARGETS:
        fn = getattr(owner, name, None)
        _TRUSTED_HOOKS[key] = fn if _looks_like_stdlib(fn) else None

def _is_trusted_original(fn, key=None) -> bool:
    if fn is None:
        return False
    if key is not None:
        return fn is _TRUSTED_HOOKS.get(key)
    return any(fn is v for v in _TRUSTED_HOOKS.values())
```

* `_looks_like_stdlib` 只在**导入期**使用一次（此时 `logging` 未被替换的概率最高），
  仍保留 `__module__` 作为**初始**筛选，但结果固化为对象身份；
* `_wrap_method` 中删除 `_PRESTINE[key] = current`；`_PRESTINE` 只在
  `_capture_pristine()`（导入期 / 测试显式重置）时写入；
* 所有 `_is_stdlib_hook(current_original)` 改为 `_is_trusted_original(current_original, key)`。

#### 4.3.2 修复后的预期行为（与 4.1.3 探针的差异）

RG-F2-02-B（伪造 `__module__`）修复后应为：

```
trusted 判定：伪造函数不是 _TRUSTED_HOOKS[key] → 记为 foreign
行为：链回真实 stdlib 实现 → 日志恢复正常、脱敏生效
health：healthy=true（因为已自愈），foreign_hooks 记录该替换
_PRISTINE：不变（未被污染）
```

→ **4.1.3 探针 `RG-F2-02-B` 的 `ok` 期望需要改写**（原期望 `trusted=False`，
正确期望是"自愈且不污染"）。这不是"改测试让它通过"，而是修正验收口径，
必须在实施 PR 中单独说明。

#### 4.3.3 明确不做

* 不阻止第三方替换钩子（做不到，也不该做）；只保证**替换不会被误认为可信**。
* 不改 `uninstall_hooks()` 的语义。

### 4.4 B1-05：显式解析器 + 结构校验 + 逐键 fail-closed

#### 4.4.1 设计

```python
_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", '"': '"'}
_PEM_KEY = re.compile(r"(?i)(private|public)_key$")

def _parse_dotenv_text(text: str) -> tuple[list[tuple[str, str]], list[dict]]:
    """返回 (键值对, 解析错误)。错误只含键名/原因/行号，绝不含值。"""
```

状态机规则：

| 输入形态 | 处理 | 与现状的差异 |
|---|---|---|
| `K=V`（无引号单行） | `V.strip()` | **不变**（逐字节兼容） |
| `K="V"`（单行双引号） | 去引号 + 解码 `\n \r \t \\ \"` | 新增转义解码 |
| `K='V'` | 去引号，**不解码** | 同现状 |
| `K="多行..."` | 跨行累积到闭合引号，行间以 `\n` 连接 | **新增** |
| 未闭合引号 | **不写入该键** + 记 `unterminated_quote` | **fail-closed**（现状：静默写半截） |
| 文件带 BOM | `utf-8-sig` 读取 | 修复键名污染 |
| `K=`（空值） | 写入空串 | 不变 |
| 无 `=` 行 | 跳过 | 不变 |

PEM 结构校验（仅对 `*PRIVATE_KEY*` / `*PUBLIC_KEY*` 或值以 `-----BEGIN ` 开头）：

```python
m = re.fullmatch(r"-----BEGIN ([A-Z0-9 ]+)-----(.*)-----END \1-----", value, re.S)
if not m:                     error = "pem_incomplete"      → 不写入
elif len(m.group(2).strip()) < 64:
                              error = "pem_body_too_short"  → 不写入
```

诊断暴露：

* `dotenv_source_diagnostics()` 增加 `"parse_errors": <int>`（**只加计数，不加内容**，
  避免 `test_credential_identity_b412.py:173` 的"诊断不含键名/值"断言误伤）；
* 新增 `dotenv_parse_errors() -> List[Dict[str, str]]`，返回
  `[{"key": "...", "reason": "unterminated_quote", "line": 3}]`——**不含值**。

#### 4.4.2 兼容性决策（有证据才做）

* **不添加 `export KEY=V` 支持**：`.env.example` 中 `^export ` 计数为 **0**，无证据。
* **`\n` 解码只在双引号内**：`.env.example` 中 `\n` 字面量计数为 **0**；
  无引号值保持原样，避免把 `C:\new` 这类路径改写。
* **不改变 `load_dotenv` 签名与返回值语义**（仍返回"是否载入了至少一个键"）。
* **不改变 `setdefault` 语义**（进程已有值优先）。

#### 4.4.3 元数据与来源追踪（跨模块依赖）

`_DOTENV_FP` / `_DOTENV_MARKED` **必须用最终值**计算：

```python
marked = _DotenvValue(value)
...
_DOTENV_FP[name] = _dotenv_fingerprint(value)     # value 已是多行完整值
_DOTENV_MARKED[name] = marked
```

否则多行键的指纹永远不匹配 → `dotenv_origin()` 恒 False → 被误判成
`explicit_env` → Vault 越级覆盖（**AUD2-B1-01 回归**）。这是本项最危险的隐性回归点。

---

## 5. 冲突与共同依赖

### 5.1 依赖图

```
F2-02（信任锚）───独立───────────────────────────┐
                                                  │
F2-04（规则/扫描器）───必须先于───> F2-03（L3 自校验）│
        │                                         │
        └──共同不变量：redact_text 幂等 + fail-closed
                                                  │
B1-05（配置解析）───依赖───> dotenv_origin 指纹必须用最终值
        └──共同不变量：不改变单行 .env 的字节结果
```

### 5.2 冲突点与处置

| 冲突 | 说明 | 处置 |
|---|---|---|
| F2-03 让 L3 **每次都跑**（首次）→ F2-04 的缺陷被**放大一倍** | L1 已泄露的内容会进 `record.msg`，L3 再跑同一套规则仍然泄露 | **先修 F2-04，再修 F2-03**；顺序不可交换 |
| F2-04 放宽值域 → `redact_text` 结果变化 → F2-03 的摘要缓存可能"缓存了错的结果" | 缓存只存**本次实现**的输出；不跨版本持久化 | 无持久化，重启即失效；无需迁移 |
| F2-03 删除 `_REDACTED_FLAG` → `_sanitize_record` 的 extra 遍历少一个跳过分支 | 若不清干净，`_REDACTED_FLAG` 会被当成普通 extra 再处理一遍 | 同步删除常量、写入、读取、跳过四处；用 `grep -r _REDACTED_FLAG` 验收为 0 |
| F2-02 改变 `_is_stdlib_hook` 语义 → `_wrap_method` 的 `_PRESTINE[key] = current` 必须同步删除 | 只改判据不删赋值，仍会污染 | 同一 PR 内完成；验收断言 `_PRESTINE` 对象身份不变 |
| B1-05 改变值 → `_DOTENV_FP` 与 `_DOTENV_MARKED` 必须同源 | 见 §4.4.3 | 加跨模块测试：多行 PEM 载入后 `dotenv_origin == True` |
| B1-05 fail-closed → 键不存在 → `_classify_source` 判 `missing` → `CredentialIncomplete` | 这是**期望行为**（宁可不启动，也不用半截凭据） | 在测试中固定该失败信息 |
| 四项都改 `log_redaction.py` / `config.py` 的模块级状态 → 探针 `reset_all()` 需要同步 | 新增 `_FORMATTED`、`_TRUSTED_HOOKS`、`_DOTENV_ERRORS` | 更新探针公共库的重置函数（**属于测试基础设施，不是产品代码**） |

### 5.3 共同不变量（四项修复都必须满足）

1. `redact_text(redact_text(x)) == redact_text(x)`（幂等）——F2-03 的双跑依赖它。
2. 任何"无法判定"一律向**打码/不写入**倒，不得原样放行。
3. 不把可用性建立在"用户不会写某个属性"上。
4. 不新增对外 API；新增的内部状态必须有上限、有锁、可重置。

---

## 6. 测试矩阵与验收标准

### 6.1 F2-04

**独立测试（新增 `tests/test_log_redaction_b414.py`）**

| ID | 输入 | 期望 |
|---|---|---|
| KV-01 | `log.exception`，异常消息 `api_key=AK` | 输出不含 AK |
| KV-02 | `"status: token=AK"` | 不含 AK |
| KV-03 | `"data=api_key=AK"` | 不含 AK |
| KV-04 | `"outer: middle: api_key=AK"` | 不含 AK（三层） |
| KV-05 | `api_key=abc=def` | 输出 `api_key: ***`（**不允许**只打码 `abc`） |
| KV-06 | `api_key=AK\nsymbol=AAPL` | 不含 AK，**且保留** `symbol=AAPL` |
| KV-07 | `api_key=PART1\nPART2`（PART2 不像字段） | 两行都不含 |
| JSON-01 | `{"api_key":"a\"b"}` | 不含 `a\"b` |
| JSON-02 | `json.dumps({"api_key": 'a"b'})` | 不含转义后的值 |
| JSON-03 | 4 层 `json.dumps` 嵌套 + 值内引号 | 每层解开后 `api_key=="***"`、`symbol=="AAPL"` |
| JSON-04 | 敏感键但 JSON 未闭合（截断日志） | 从 `:` 到行尾全部打码 |
| TAIL-01 | `raise RuntimeError(f"api_key={AK}")` | 源码行保留 `")`，不畸形 |
| REG-01 | 已登记含引号凭据 + JSON 形态 | 不泄露（**当前唯一击穿兜底的路径**） |

**对抗测试**

* 伪造 `__str__` 返回 `api_key=…` 的异常对象作为 `extra`；
* `{"api_key": "x"}` 与 `{'api_key': 'x'}` 混排 + 后续业务字段；
* 超长值（4096 / 4097 / 2 MiB 边界）；
* 值内含 `\r\n`、`\t`、`\u0000`；
* 200 条真实日志语料的"零误伤"回归（`symbol/qty/price/side/order_id` 必须原样保留）。

**验收标准**

1. 上表 13 条全绿；
2. `tests/test_log_redaction_b41.py` / `b411` / `b412` 全绿（三个文件合计 50 例，其中 34 例为 4.1.2 定向用例）；
3. 幂等性：随机语料 1000 条，`redact_text∘redact_text == redact_text`；
4. 误伤率：普通业务文本语料（≥200 条）零改动。

### 6.2 F2-03

| ID | 场景 | 期望 |
|---|---|---|
| FLAG-01 | `record._aitrader_redacted = True` + `h.emit` | 打码 |
| FLAG-02 | 自定义 `LogRecord.__getattr__` 恒真 | 打码 |
| FLAG-03 | 直接 `h.format(record)` | 打码 |
| FLAG-04 | 后置 Filter 追加 `api_key=…` | 打码 |
| FLAG-05 | 自定义 Formatter 追加 `ctx_api_key=…` | 打码 |
| FLAG-06 | `copy.copy(record)` 后改 msg | 打码 |
| FLAG-07 | `grep -r "_REDACTED_FLAG" src/` | 0 命中 |
| PERF-01 | 10k 条普通日志（无敏感词） | 相对基线吞吐 ≥ 60%，p95 增量 ≤ 80 µs/条 |
| PERF-02 | 10k 条含敏感词日志 | 相对基线吞吐 ≥ 40% |

**验收标准**：FLAG-01…07 全绿；PERF 达标；`redaction_health()` 字段集合与修复前**完全一致**。

### 6.3 F2-02

| ID | 场景 | 期望 |
|---|---|---|
| HOOK-01 | no-op 替换（`__module__="__main__"`） | 自愈，`foreign_hooks` 有记录，日志正常 |
| HOOK-02 | no-op 替换 + **伪造** `__module__="logging"` | 自愈，`_PRESTINE` 前后**同一对象**，`foreign_hooks` 有记录 |
| HOOK-03 | 伪造函数带 `_aitrader_redaction=True` | 不被当成"我们的包装" |
| HOOK-04 | 重复 `ensure_hooks()` | 幂等 |
| HOOK-05 | `uninstall_hooks()` | 精确恢复，不覆盖第三方改动 |

**验收标准**：HOOK-01…05 全绿；`_PRESTINE` 在全部用例后与导入期一致；
`redaction_health()` 在 HOOK-02 后为 `healthy=true` 且 `degraded=true`（有 foreign 记录）。

### 6.4 B1-05

| ID | 输入 | 期望 |
|---|---|---|
| PEM-01 | 双引号多行 PEM（LF） | 值 == 原始 PEM（逐字节） |
| PEM-02 | 无引号多行 PEM | 值 == 原始 PEM |
| PEM-03 | 单引号多行 PEM | 值 == 原始 PEM（不解码转义） |
| PEM-04 | `\n` 转义单行 + 双引号 | 解码为真实换行，值 == 原始 PEM |
| PEM-05 | CRLF 多行 | 值 == 原始 PEM（`\n` 归一化） |
| PEM-06 | 文件末尾无换行 | 值 == 原始 PEM |
| PEM-07 | 同文件多字段 | 其余键逐字节不变 |
| PEM-08 | 缺 END | **键不存在** + `parse_errors==1` |
| PEM-09 | 空 body / body<64 | 键不存在 + `parse_errors==1` |
| PEM-10 | 未闭合引号 | 键不存在 + `reason=unterminated_quote` |
| PEM-11 | 带 BOM | 键正常写入（键名无 `\ufeff`） |
| PEM-12 | 单行 `K=V`（12 种既有形态） | **逐字节等同于修复前** |
| PEM-13 | 多行 PEM 载入后 `dotenv_origin()` | `True`（指纹用最终值） |
| PEM-14 | 多行 PEM + 进程已有同键 | 不覆盖（setdefault） |

**验收标准**：PEM-01…14 全绿；`dotenv_source_diagnostics()` 原字段全部不变，
仅新增 `parse_errors` 计数；诊断内容不含任何值（沿用 b412 的断言）。

### 6.5 四项联调

| ID | 场景 | 期望 |
|---|---|---|
| E2E-01 | 多行 PEM 载入 → `_register_secret_values` → 日志打印该 PEM | 日志中无 PEM 任何片段 |
| E2E-02 | 多行 PEM 载入 → traceback 含 `api_key=<PEM>` | 无 PEM 片段（PEM 规则 + 值扫描 + 登记三重） |
| E2E-03 | 旧 `.env`（单行）+ 新解析器 → 全量测试套件 | 无回归 |
| E2E-04 | 畸形 PEM → 启动 | `CredentialIncomplete`（fail-closed），不构造 SDK |

---

## 7. 现有测试的假阳性 / 覆盖盲区

| 测试 | 问题 | 影响 |
|---|---|---|
| `test_log_redaction_b41.py::test_traceback_is_redacted` | 异常消息是 `auth failed api_key=AK`。`RuntimeError: auth` 会在 `auth` 后形成扫描边界，恰好掩盖了 `RuntimeError: api_key=AK` 的吞并缺陷。**去掉 `auth failed` 三个字即泄露** | **高风险假阳性**：它让人误以为 traceback 已覆盖 |
| `test_log_redaction_b411.py::test_t04_exc_info_is_neutralized...` | 用例内先 `register_secret(AK)`，即使规则失配也会被字面量兜底 | 中：不能证明规则有效 |
| `test_log_redaction_b411.py::test_t03_escaped_json...` | payload 的值内部**没有引号/反斜杠**，从未触发 `\"` 分支 | 高：T03 名字里有"转义"，实际只测了"整串被再转义一层"，没测"值内含转义引号" |
| `test_log_redaction_b411.py::test_t02_non_sensitive_suffixes...` | 只覆盖非敏感 ident（`token_count` 等），不覆盖敏感 ident 的后缀残留 | 中 |
| `test_log_redaction_b411.py::test_t07` / `b412::test_f201_..._leaks_directly` | 断言"直连会泄露"，是**已知边界**而非缺陷；但容易被引用成"已覆盖" | 低（文档性） |
| `test_credential_identity_b412.py` 的 dotenv 用例 | 全部是单行 `K=V`，无多行 / 引号 / BOM / 畸形 | 高：B1-05 完全无覆盖 |
| `tests/test_secrets_and_gate.py:141` | 测 `inject_into_environ`，但该方法在 `src/` 无调用点 | 中：造成"vault 注入路径已覆盖"的错觉 |
| 4.1.3 探针 `F2-03-03/05/06/08/12/13/14` | `ok=False` 编码的是"缺陷存在"；修复后必然翻绿 | 需在验收时**反转期望**，不能当成回归失败 |
| 4.1.3 探针 `RG-F2-02-B` | 期望 `trusted=False`，但正确设计下应是"自愈且不污染" | 需改写期望（§4.3.2） |

---

## 8. 迁移与兼容性检查

| 面 | 是否需要迁移 | 证据 | 处置 |
|---|---|---|---|
| 数据库（`data/audit.db`） | **否** | 四项修复只改内存中的字符串处理与配置解析，不涉及表结构/字段 | 不动 |
| `config/config.yaml` | **否** | 无 PEM 字段（`grep` = 0）；四项均不新增配置项 | 不动 |
| `.env` 格式 | **不需要重写文件**，但需**重启进程** | 已载入的截断值只存在于进程环境；重启后按新解析器重新读取 | 文档说明；不自动改写用户文件 |
| `.env` 兼容性 | **是**（单行值必须逐字节不变） | B1-05 有 20 个用例的基线；新增 PEM-12 回归 | 兼容性测试固定 |
| Vault 内容 | **可能需要人工核对**，不做自动迁移 | 若 vault 曾被写入截断值（27 字符），修复后不会自动修复 | 提供只读检查脚本（BEGIN/END/长度），由人工决定是否重新录入 |
| `dotenv_origin` 元数据 | **否**（内存态，无持久化） | `_DOTENV_*` 均为模块级 dict | 重启即重建 |
| 日志格式/字段 | **否** | L3 输出形态不变（`***`） | 不动 |
| 依赖 | **否** | 全部标准库 | 不动 |

> 没有证据支持的改动一律不引入：不新增配置开关、不加环境变量、不改 CLI 参数、
> 不引入 `python-dotenv` 依赖。

---

## 9. 文件修改清单（**仅计划，未执行**）

### 9.1 产品代码

| 文件 | 位置 | 改动 | 归属 |
|---|---|---|---|
| `src/ai_trader/log_redaction.py` | L96-110 | 新增常量：`MAX_SCAN_LINES`、`_FORMATTED_MAX`、`_QUICK_HIT` | F2-04 / F2-03 |
| 同上 | L171-186 | `_VALUE` / `_KV_RE` / `_JSON_RE` 改为**定位**用途；新增 `_QUOTE_TOKEN`、`_CONTINUATION` | F2-04 |
| 同上 | L191-213 | 删除 `_kv_sub` / `_json_sub` 的打码职责（保留 `_url_sub`），新增 `_scan_value` / `_mask_span` / `_redact_assignments` | F2-04 |
| 同上 | L221-229 | `_RULES` 移除 JSON/KV 两条，改为在 rules 之后调用 `_redact_assignments` | F2-04 |
| 同上 | L455 | 删除 `_REDACTED_FLAG` | F2-03 |
| 同上 | L497-507 | 删除 extra 遍历的 flag 跳过、删除 flag 写入 | F2-03 |
| 同上 | L578-600 | `_is_stdlib_hook` → 对象身份判据；`_capture_pristine` 固化 `_TRUSTED_HOOKS` | F2-02 |
| 同上 | L768-812 | 删除 `_PRESTINE[key] = current`；`_wrap_method` 改用 `_is_trusted_original(..., key)` | F2-02 |
| 同上 | L866-883 | 重写 `install_format_guard`：删除 flag 判据，加快速否定 + 摘要缓存 | F2-03 |
| 同上 | L1-45 | 更新模块 docstring 的 L3 描述（"跳过"改为"自校验"） | 文档 |
| `src/ai_trader/config.py` | L290-340 | 新增 `_DOTENV_ERRORS`、`_ESCAPES`、`_PEM_KEY` | B1-05 |
| 同上 | L402-412 | `dotenv_source_diagnostics()` 增加 `parse_errors` 计数 | B1-05 |
| 同上 | L495-505 | 新增 `_parse_dotenv_text()`、`_validate_pem_value()`、`dotenv_parse_errors()` | B1-05 |
| 同上 | L508-540 | 重写 `load_dotenv()`：BOM 剥离、状态机、fail-closed、指纹用最终值 | B1-05 |

**不改**：`src/ai_trader/broker/fosun_adapter.py`、`src/ai_trader/market/feed.py`、
`src/ai_trader/runtime.py`、`src/ai_trader/secrets.py`、`config/config.yaml`、`.env`。

> 说明：`runtime.py` 不改意味着 `dotenv_parse_errors()` 在启动时**不会**被打印。
> 这是有意识的取舍（避免在日志系统就绪前引入新的日志路径）。
> 若产品要求启动可见，应作为**独立变更**单独立项，不塞进本批。

### 9.2 测试代码

| 文件 | 类型 | 内容 |
|---|---|---|
| `tests/test_log_redaction_b414.py` | **新增** | §6.1 + §6.2 + §6.3 的 25 条用例 |
| `tests/test_config_dotenv_pem_b414.py` | **新增** | §6.4 的 14 条用例 |
| `tests/test_b414_integration.py` | **新增** | §6.5 的 4 条联调用例 |
| `tests/test_log_redaction_b41.py` | **修改 1 条** | `test_traceback_is_redacted` 增加"无前缀"变体（不是删除原用例） |
| `tests/test_log_redaction_b411.py` | **修改 1 条** | `test_t03` 增加"值内含 `\"`"参数化层 |
| `tests/test_log_redaction_b412.py` | **不动** | 但需在验收时确认 34 例仍全绿 |
| `audit_evidence/us_p1_b41_3_probes/_probe_lib.py` | **修改** | `reset_all()` 增加 `_FORMATTED` / `_TRUSTED_HOOKS` / `_DOTENV_ERRORS` 重置（探针基础设施） |

### 9.3 交付与证据（实施阶段才产生）

* `US_P1_BATCH4_1_4_FIX_REPORT.md`
* `US_P1_BATCH4_1_4_TEST_REPORT.md`
* `US_P1_BATCH4_1_4_FILE_MANIFEST.sha256`
* `audit_evidence/us_p1_b41_4_*`

---

## 10. 风险登记

| 风险 | 概率 | 影响 | 缓解 | 回滚 |
|---|---|---|---|---|
| F2-04 值域放宽导致业务日志被过度打码 | 高 | 中（可读性下降） | 只对**敏感 ident** 放宽；普通文本零改动语料回归；在测试中显式固定"过度打码"行为 | 恢复 `_VALUE` 旧定义（单文件回滚） |
| F2-04 扫描器在超长/畸形文本上退化 | 中 | 中（CPU） | `MAX_VALUE_CHARS` / `MAX_SCAN_LINES` 双重上限；超过 2 MiB 直接 `_OVERSIZED` | 退回正则实现（保留旧函数） |
| F2-03 L3 常跑导致吞吐下降 | 中 | 中 | 快速否定正则（2–3 µs）；摘要缓存；PERF-01/02 门槛 | 保留 flag 作为"快速路径"但加摘要校验（降级方案） |
| F2-03 摘要缓存被 `id()` 复用污染 | 低 | 高（漏打码） | 缓存持有 record 强引用 + `is` 校验 + 上限 512 | 关闭缓存，每次重算 |
| F2-02 对象身份判据在同进程多次 reload 后失效 | 低 | 中（health 误报） | `_capture_pristine` 只在导入期与显式重置时调用；测试覆盖 | 恢复 `__module__` 判据（不推荐） |
| B1-05 解析器改动破坏既有单行 `.env` | 中 | 高（启动失败） | PEM-12 覆盖 12 种既有形态 + 全量测试套件 | 保留旧函数 `_load_dotenv_legacy`，按需切换 |
| B1-05 fail-closed 后键缺失 → 启动失败 | 中 | 中（可用性） | 明确 `CredentialIncomplete` 信息；文档说明"这是期望行为" | 不提供"宽松模式"（安全优先） |
| B1-05 指纹用错值 → `dotenv_origin` 恒 False | 中 | 高（Vault 越权覆盖） | PEM-13 专项用例 | 单点回滚元数据计算 |
| 四项同改一个文件导致 review 困难 | 高 | 低 | 按 §5 顺序拆 4 个 PR，每个 PR 独立可回滚 | 逐 PR revert |

**不引入的变更（明确记录）**：无数据库迁移、无配置项新增、无新依赖、
无 `.env` 自动改写、无 vault 自动迁移。

---

## 11. 停止声明

本阶段到此为止：

* **未修改任何源码 / 测试 / 配置 / 数据库 / 部署环境**；
* 未执行修复、未开始 Batch 4.2、未发布、未开启实盘；
* 未连接真实券商、未访问 VPS、未创建真实 Session；
* 未覆盖任何既有审计证据（4.1.3 报告与证据保持原样；§2.4 的更正以本文为准）。

等待人工审核后再进入实施。
