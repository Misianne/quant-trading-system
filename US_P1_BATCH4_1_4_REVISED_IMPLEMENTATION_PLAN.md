# US-P1 Batch 4.1.4 — 修订实施方案（Revised Implementation Plan）

| 项 | 值 |
|---|---|
| 批次 | US-P1 Batch 4.1.4（修订实施规划） |
| 性质 | **只读规划**。未修改任何源码 / 测试 / 配置 / 数据 / 既有报告 |
| 取代 | 本文取代 `US_P1_BATCH4_1_4_REMEDIATION_PLAN.md` 的 §4.1–§4.4 设计部分（原文件保留，不删改） |
| 依据 | `US_P1_BATCH4_1_4_FINAL_REVIEW.md`、`US_P1_BATCH4_1_3_INDEPENDENT_AUDIT.md`、`US_P1_BATCH4_1_2_FIX_REPORT.md`、`US_P1_BATCH4_1_2_TEST_REPORT.md`、当前源码、112 条探针证据 |
| 基线指纹 | `log_redaction.py` = `720b4f33…6b4a`；`config.py` = `ddb8144c…e8e0`（与本轮审查时逐字节相同） |
| 解释器 | `C:\FosunAI-Trading\.venv\Scripts\python.exe`（3.13.14，win32） |

> 本文件是"可执行的实施合同"，不是设计草稿：每个改动的落点、行号、失败行为、验收门槛与回滚条件都已固定。实施时若与本文不符，必须先回到本文修订，不得在代码里临时决定。

---

## 0. 结论摘要

| 项 | 结论 |
|---|---|
| F2-03 | **推翻原缓存方案**，改为"弱引用对象键 + 输入摘要 + 输出文本"三元结构；命中只返回已脱敏输出 |
| F2-04 | 保留 JSON 独立重建、URL 前置、`_mask_span` 结构尾巴；新增"非敏感 ident 只前移游标"规则；补"已登记凭据转义形态"兜底 |
| F2-02 | 锚点外置到独立稳定模块；信任判定改为纯对象身份；锚点缺失一律 fail-closed 且 `healthy=False` |
| B1-05 | 显式状态机 + 归一化 + 结构/Base64/SDK 三级校验；`B1-05-05` 已核实为双引号包裹，`\n` 解码规则成立 |
| 实施顺序 | 1 F2-04 → 2 F2-02 → 3 B1-05 → 4 F2-03 → 5 全量回归 + 独立审计（与任务书一致，理由见 §9.1） |
| 分批 | 4 个可独立回滚的批次，禁止合并成单一大改动 |
| 性能 | 8 类基准分别验收，门槛 **60%**（不接受 40%） |

---

## 1. 基线核对与本轮新增实证

### 1.1 继承的既有证据（不重跑，直接引用）

| 事实 | 来源 |
|---|---|
| 112 条探针：80 PASS / 32 FAIL；8 个文件分布 0/20/7/14/20/17/13/21 | `audit_evidence/us_p1_b41_3_probe_results.json` |
| F2-03 / F2-04 / B1-05 三组 FAIL | `US_P1_BATCH4_1_3_INDEPENDENT_AUDIT.md` |
| `_PRESTINE` 被 `_wrap_method` 污染 | `log_redaction.py:798-803`（审查报告 §3.2） |
| `MIN_SECRET_LEN=8`、`MAX_SECRETS=64`、`MAX_TEXT_CHARS=262144`、`MAX_RAW_TEXT_CHARS=2MB` | `log_redaction.py:80-96` |

### 1.2 本轮新增的四项实证（均在 `%TEMP%` 内完成，不触碰仓库）

**A. 锚点外置跨 reload 有效**

```
1st capture: cold            | _P 值 is 原函数: True
reload 后: _P 为空: True
recapture: from-anchor       | _P 值 is 原函数: True
第 3 次 reload 后: from-anchor | 仍 is 原函数: True
对照（纯模块内 _P）:  reload 后 _P == {}   ← 锚点丢失
```

结论：`importlib.reload` 只重建被 reload 模块的全局，**不重建 `sys.modules` 里的兄弟模块**，因此锚点模块可作为稳定身份源。

**B. `WeakKeyDictionary` 并发 + GC 稳定**

```
8 线程 × 2000 次（set/get + 每 7 次 del + gc.collect(0)）
faults=[]  size_after=0  elapsed=42ms
```

结论：以 LogRecord 对象为键时，弱引用表在并发与回收下不抛异常、不残留。

**C. 目标输出对 `redact_text` 幂等**

```
'api_key: ***'                  -> 不变
'{"api_key":"***"}'             -> 不变
'api_key=*** , x=1'             -> 'api_key: *** , x=1'   （分隔符归一，二次不再变）
'api_key: *** session_id: ***'  -> 不变
```

结论：F2-03 的双跑（L1 → L3）不会把已脱敏文本再破坏。

**D. PEM 归一化校验的边界（关键反例）**

| 用例 | 结构+Base64 校验 | `cryptography` 解析 |
|---|---|---|
| 合法（尾部 `\n`） | ok | OK |
| 合法（strip） | ok | OK |
| 合法 CRLF | ok | OK |
| 合法 CRLF + 尾部空行 | ok | OK |
| 两段 PEM 连续 | ok | OK |
| 缺 END | `pem_incomplete` | ValueError |
| END 名不匹配 | `pem_incomplete` | ValueError |
| 换行换成空格 | `pem_incomplete` | **OK** |
| body 含非法字符 | `pem_body_charset` | ValueError |
| 仅首行 | `pem_incomplete` | ValueError |
| **整行截断（base64 仍合法）** | **ok** | ValueError |

两条必须写进设计的结论：

1. **SDK 不能替代格式检查**（"换行换成空格"能解析成功）；
2. **Base64 不能发现整行截断**（截断后仍是合法 base64）——这一项只能靠 SDK 解析或 DER 长度校验。

### 1.3 修正上一轮审查中的两处判断

| 原判断 | 实测 | 结论 |
|---|---|---|
| `B1-05-05` 若不带引号则与"`\n` 只在双引号内解码"冲突 | 探针源码 `probe_b105_dotenv_pem.py:147` 为 `f'{k}="{esc}"'`，**带双引号** | **冲突不存在**，原计划的解码规则可直接成立 |
| B1-05 指纹错位会导致"Vault 越级覆盖" | `config.py:446` 的 `dotenv_origin` 在任一项不确定时判 `explicit_env`；`fosun_adapter.py:857` 的 explicit 分支在 vault **之前**返回 | 方向相反：错位会让**本地破损值压过保险箱里的有效凭据**（详见 §6.4） |

---

## 2. 原始发现 ID 对照表

| 原始 ID | 一句话 | 4.1.3 判定 | 本轮编号 | 源码位置 | 处理批次 |
|---|---|---|---|---|---|
| `AUD3-F2-04-01` | 外层非敏感键吞并内层赋值，整段绕过 | P1 | `R-204-A` | `log_redaction.py:171,191-197` | B1 |
| `AUD3-F2-04-02` | 已登记凭据在 JSON 转义上下文仍泄露 | P1 | `R-204-B` | `:179-184, 285-324` | B1 |
| `AUD3-F2-04-03` | `_JSON_RE` 遇 `\"` 整条 match 失败 → fail-open | P1 | `R-204-C` | `:179-184` | B1 |
| `AUD3-B1-05-01` | 多行 PEM 被逐行截断成首行 | P1 | `R-105-A` | `config.py:508-540` | B3 |
| `AUD3-F2-03-01` | `_REDACTED_FLAG` 是普通可写属性，可伪造 | P1 | `R-203-A` | `:455, 497-505` | B4 |
| `AUD3-F2-02-01` | `_PRESTINE` 被 `_wrap_method` 永久污染 | P1 | `R-202-A` | `:578, 798-803` | B2 |
| `SA-4.1.3-01`（新） | reload 后锚点变 None 但 `healthy=True` | P1 | `R-202-B` | `:584-595, 886-935` | B2 |
| `SA-4.1.3-02`（新） | `_aitrader_original` / `_aitrader_trusted` 可伪造 | P1 | `R-202-C` | `:604-610, 812-820` | B2 |
| `SA-4.1.3-03`（新） | 结构尾巴被吞（`…f"api_key: ***` 丢 `")`） | P3 | `R-204-D` | `:171` | B1 |
| `SA-4.1.3-04`（新） | PEM 校验对尾部换行/CRLF 敏感，误杀合法凭据 | P1 | `R-105-B` | 计划 §4.4.1 | B3 |
| `AUD2-F2-01` | 末级降级失败时健康位误报 | 已修 | `G-201` | `:646-740` | 回归护栏 |
| `AUD2-F2-02` | 钩子被换成 no-op 后当原实现 | 已修 | `G-202` | `:768-820` | 回归护栏 |
| `AUD2-B1-01` | 同值显式 export 被判 dotenv，Vault 越级 | 已修 | `G-101` | `config.py:446` | 回归护栏 |
| `AUD2-B1-02` | dotenv + explicit 误判 `mixed` | 已修 | `G-102` | `fosun_adapter.py:880-900` | 回归护栏 |

---

## 3. A. F2-03 修订设计（推翻原缓存方案）

### 3.1 原方案为什么必须废弃

原计划 `US_P1_BATCH4_1_4_REMEDIATION_PLAN.md:426-449`：

```python
digest = _digest(text)                                  # 输入摘要
if hit is not None and hit[0] is record and hit[1] == digest:
    return text                                         # ← 返回原文
_FORMATTED[key] = (record, _digest(out))                # 存输出摘要
```

两种读法都不成立：

- 按字面：`digest(out) != digest(text)`，只要发生过打码就**永不命中**，缓存是死的；
- 若"修正"为存 `digest(text)`：命中时 `return text` 返回**明文**，第二个 handler 直接泄露。

同时 §4.2.2 拒绝 weakref 的理由（"无法阻止 `id()` 复用"）被 §1.2-B 的实测否定。

### 3.2 新结构

```python
# 模块级，import 时创建一次
_DIGEST_SALT = os.urandom(16)                    # 进程内随机盐，避免摘要成为离线口令预言机
_FORMAT_CACHE: "weakref.WeakKeyDictionary[logging.LogRecord, Tuple[bytes, str]]" = \
    weakref.WeakKeyDictionary()
_FORMAT_LOCK = threading.RLock()
```

| 要素 | 规定 |
|---|---|
| **缓存键** | **LogRecord 对象本身**。不用 `id(record)`：`LogRecord` 未定义 `__eq__`/`__hash__`，默认按对象身份哈希；条目随对象死亡被弱引用回调移除，`id()` 复用不可能命中旧条目 |
| **缓存值** | `(digest: bytes, redacted: str)`。`digest = blake2b(text.encode("utf-8","surrogatepass"), digest_size=16, key=_DIGEST_SALT).digest()`；`redacted` 是 `redact_text(text, truncate=False)` 的**输出** |
| **不存什么** | **绝不存输入原文**。输入只留 16 字节摘要，避免缓存延长明文寿命 |
| **生命周期** | 与 record 同寿。record 被 GC → 弱引用回调删除条目 → 无需 LRU、无需上限、无"淘汰策略"可被利用 |
| **失效策略** | 每次格式化都重算 **当前文本** 的摘要；与缓存值不等 → 视为未命中 → 重跑 `redact_text`。这是"记录被追加/改写后必须重新校验"的唯一判据 |
| **失败行为** | 读缓存异常 / 写缓存异常 / 弱引用不可用 → **一律按未命中处理并继续走 `redact_text`**，绝不 `return text` |
| **并发** | 所有缓存读写包在 `_FORMAT_LOCK`（RLock）内；热路径**只允许**单键 `get`/`set`，**禁止** `len()` / `items()` / `clear()`（`WeakKeyDictionary` 的这些方法会触发 `_commit_removals` 迭代，与 GC 回调交叉会 `RuntimeError: dictionary changed size`） |
| **诊断** | 另设 `format_cache_stats()`：在锁内 `len()`，只在健康检查/测试中调用，不进热路径 |

### 3.3 热路径伪代码（可直接落地）

```python
def _format(self, record):
    text = original(self, record)
    if not isinstance(text, str):
        return text

    # 1) 否定判据：结构上不可能存在可脱敏构造 → 原样返回（唯一允许"不脱敏"的出口）
    if not _needs_scan(text):
        return text

    # 2) 命中判定：只比摘要，不读任何记录上的标记
    try:
        dg = _digest_text(text)
        with _FORMAT_LOCK:
            ent = _FORMAT_CACHE.get(record)
        if ent is not None and ent[0] == dg:
            return ent[1]                     # 只返回已脱敏输出
    except Exception:                          # noqa: BLE001
        dg = None                              # 缓存故障 → 继续脱敏，不降级为明文

    # 3) 未命中：重新脱敏（失败也不会返回原文：redact_text 内部 fail-closed）
    out = redact_text(text, truncate=False)
    if dg is not None:
        try:
            with _FORMAT_LOCK:
                _FORMAT_CACHE[record] = (dg, out)
        except Exception:                      # noqa: BLE001
            pass                               # 写缓存失败不影响本次已脱敏结果
    return out
```

### 3.4 两个核心攻击场景的处置

**场景 1：同一记录由第二个 Handler 处理**

- 两个 handler 共用同一 `LogRecord` 对象 → 缓存键相同；
- 第二个 handler 的 `original(self, record)` 若产生**相同文本** → 摘要相同 → 命中 → 返回 `out`（已脱敏）；
- 若两个 handler 的 Formatter 配置不同 → 文本不同 → 摘要不同 → 未命中 → 重跑 `redact_text`；
- **任何分支都不会返回 `text`**：唯一 `return text` 在 `_needs_scan` 否定分支，而该分支已证明"无敏感构造"。

**场景 2：已脱敏记录被追加新的秘密字段后再次处理**

- 追加发生在 L1 之后（Filter / Formatter / 业务代码改 `record.msg`）→ 文本变化 → 摘要变化 → 未命中 → 重跑；
- 追加发生在 L3 **之前**且命中缓存？不可能：命中的前提是摘要等于缓存值，而摘要取自**当前**文本；
- 旧标记机制下这条会泄露（`F2-03-06/12/13/14` 四条实测 FAIL），新结构下由"摘要必然重算"覆盖。

### 3.5 `_QUICK_HIT` 与 `_SENSITIVE_KEYS` 的同源规则

要求是"派生，避免两套词表"。但**纯词表派生是错的**：`_is_sensitive_token` 会把候选键去掉非字母数字后比对，因此 `api_key` / `X-Api-Key` / `apiKey` / `apikey` 都指向同一敏感词，任何字面量词表都覆盖不全，且必然漂移。

采用"**结构性肯定 + 词表由常量派生 + 词表从不作否定判据**"三层规则：

```python
# 词表：import 时从 _SENSITIVE_KEYS 派生，不做人工维护
_QUICK_WORDS = tuple(sorted(_SENSITIVE_KEYS))
_QUICK_WORD_RE = re.compile("|".join(re.escape(w) for w in _QUICK_WORDS), re.I)

# 结构标记：与词表无关，覆盖"无分隔符也能脱敏"的构造
_STRUCT_HIT = re.compile(r"[:=]|\bbearer\b|-----BEGIN|authorization|proxy-authorization|cookie", re.I)

_QUICK_PREFIX = 4096

def _needs_scan(text: str) -> bool:
    if _STRUCT_HIT.search(text):                 # 肯定信号 1：键值/头/PEM
        return True
    if _registered_hit(text):                    # 肯定信号 2：登记字面量（含转义形态）
        return True
    if len(text) <= _QUICK_PREFIX:               # 肯定信号 3：短文本才用词表
        norm = _NON_KEY_CHARS.sub("", text.lower())
        if _QUICK_WORD_RE.search(norm) or any(w in norm for w in _QUICK_WORDS):
            return True
    return False
```

关键性质：**词表只用于"说要扫"，从不用于"说不用扫"**。因此即使词表漏词，也不会漏脱敏（结构信号仍然覆盖）；`api_key=` 这类文本靠 `[:=]` 必然命中。同时 `_QUICK_WORDS == tuple(sorted(_SENSITIVE_KEYS))` 用一条单测锁死，杜绝两套词表。

### 3.6 必须删除的旧机制

| 位置 | 动作 | 验收 |
|---|---|---|
| `:455` `_REDACTED_FLAG` | 删除 | `grep -rn _REDACTED_FLAG src/` = 0 |
| `:497-499` L3 读标记 | 删除 | 同上 |
| `:505` L1 写标记 | 删除 | 同上 |
| `:458` `_sanitize_record` 内 extra 跳过分支 | 删除 | `F2-04-X01…X08` 全绿 |

> 注意：`_sanitize_record` 的 extra 递归本身保留（`X01–X07` 依赖它），只删"看到标记就跳过"的那一支。

---

## 4. B. F2-04 修订设计（游标规则）

### 4.1 规则表最终形态

```python
_RULES = (
    (_PEM_RE,     _MASK),          # 1 整块
    (_AUTH_RE,    _auth_sub),      # 2 整行
    (_BEARER_RE,  "Bearer ***"),   # 3 裸令牌
    (_URL_RE,     _url_sub),       # 4 URL —— 必须在 assignments 之前
)                                  # 5 assignments（新增，函数而非 regex.sub）
                                   # 6 登记字面量（含转义形态），最后兜底
```

`_KV_RE` 与 `_JSON_RE` **保留符号名**，但职责从"sub 打码"降级为"定位"（`_KV_KEY = _KV_RE`）。理由：`tests/` 未直接引用这两个名字（已核），但保留可降低认知成本与外部脚本断裂风险。

### 4.2 定位与游标（R-204-A 的唯一解）

```python
def _redact_assignments(text: str) -> str:
    out, pos = [], 0
    while True:
        m = _KV_KEY.search(text, pos)          # ident + \s*[:=]\s*
        if m is None:
            out.append(text[pos:]); break
        ident, sep_end = m.group("ident"), m.end()
        if not _is_sensitive_token(ident):
            # ★ 核心规则：非敏感 ident 只把游标前移到 sep.end()，绝不消费 value
            out.append(text[pos:sep_end]); pos = sep_end; continue
        vstart = _skip_spaces(text, sep_end)
        if vstart >= len(text) or text[vstart] in _VALUE_TERM:
            out.append(text[pos:vstart]); pos = vstart; continue     # 空值不制造 ***
        end, kind, token = _scan_value(text, vstart)
        out.append(text[pos:m.start("ident")])
        out.append(f"{ident}: {_mask_span(text[vstart:end], kind, token)}")
        pos = end
    return "".join(out)
```

**验收锚点**：`RuntimeError: api_key=secret` 的第一个 match 是 `RuntimeError: `（`ident=RuntimeError`，`sep_end` 指向冒号后空格之后），非敏感 → 游标只到 `sep_end` → 下一个 `_KV_KEY.search` 从 `api_key=` 开始 → 打码。若把 `pos = sep_end` 写成 `pos = m.end()` 之外的任何"消费 value"形式，`F2-04-S01/S02/S03/S04/S07` 会全部回红。

### 4.3 取值与打码（保留结构）

```python
_VALUE_TERM = set(" \t\r\n,;>)]}")

def _scan_value(text, pos):                    # -> (end, kind, token)
    m = _QUOTE_TOKEN.match(text, pos)          # r'\\{0,15}["\']'
    if m:
        token = m.group(0); prefix = len(token) - 1; i = m.end()
        while i < len(text):
            if text[i] in "\"'":
                if _count_backslashes_before(text, i) == prefix \
                        and text.startswith(token, i - prefix):
                    return i - prefix + len(token), "quoted", token
            i += 1
        return len(text), "unterminated", token
    end = text.find("\n", pos)                 # 裸值到行尾（含续行，见 §4.4）
    ...
    return end, "bare", ""

def _mask_span(raw, kind, token):
    if kind == "quoted":
        return f"{token}{_MASK}{token}"        # 引号保留，内容全抹
    core = raw.rstrip("\"')]}>;,")             # ★ 结构尾巴不吞（R-204-D）
    return f"{_MASK}{raw[len(core):]}"
```

**保留的三条既有正确行为**（不得改动）：

1. `_JSON_RE` 的独立重建路径 —— `_json_sub` 输出 `f"{pre}{ident}{pre}{sep}{q}{MASK}{q}"`，引号层数原样保留，4 层 `json.dumps` 解开后仍是合法 JSON；
2. `_URL_RE` 的位置 —— 必须在 assignments 之前，否则 `?api_key=x` 会被改写成 `api_key: ***`，破坏 `URL-01` 与 `F2-04-05`；
3. `_mask_span` 的 `rstrip` 集合 —— 同时满足 `api_key=a"b`（全抹）与 `api_key=secret")`（保留 `")`）。

### 4.4 多行与续行

裸值默认到行尾。若下一行满足"不像新字段"且不像注释，则并入同一值（上限 64 行）：

```python
_CONTINUATION = re.compile(r"(?![ \t]*[A-Za-z_][A-Za-z0-9_.\-]{0,63}[ \t]*[:=])(?![ \t]*#)")
```

- `api_key=PART1\nPART2` → 两行都打码（`F2-04-13`）；
- `api_key=AK\nsymbol=AAPL` → 停在 `symbol=` 之前（`KV-06`）；
- 已知取舍：`api_key=x symbol=AAPL` 同行时 `symbol=AAPL` 会被一起打码。这是**故意的过度打码**，必须在测试里固定，不允许"优化"回来。

### 4.5 已登记凭据的转义形态（R-204-B / R-204-C）

`redact_text` 当前的兜底是 `out.replace(secret, MASK)`，只能命中"原样字节"。JSON 里合法凭据会以转义形态出现（`abc"defgh` → `abc\"defgh`），因此击穿。

修订：登记表在替换阶段额外生成**转义投影**（只在内存里，不落盘）：

```python
for secret in _secret_snapshot():
    forms = {secret}
    if '"' in secret or "\\" in secret:
        forms.add(secret.replace("\\", "\\\\").replace('"', '\\"'))   # JSON 单层转义
        forms.add(json.dumps(secret)[1:-1])                           # 与 json.dumps 对齐
    for f in forms:
        if f in out:
            out = out.replace(f, _MASK)
```

- 代价：≤64 条 × ≤3 形态 × `str.find`，与现状同量级；
- 效果：`{"api_key":"abc\"defgh"}` → `{"api_key":"***"}`（`REG-01`、`F2-04-14R`）；
- 边界：多层转义（`\\\\\"`）不保证覆盖，列入 §11 残余风险。

### 4.6 未登记裸值的正确表述

无键名、未登记的裸值（`handshake done with FAKE-…`、`a=*** b=FAKE-…`）**无法被规则识别**，这是信息论边界，不是缺陷。修订后：

- 行为不变（仍不打码）；
- 断言从"允许泄露（设计边界）"改为**"显式声明不识别"**，并在测试名/注释里禁止使用"安全""已覆盖"字样；
- 文档中不得出现"已识别全部裸值"的表述。

---

## 5. C. F2-02 修订设计（稳定锚点）

### 5.1 新增模块（唯一新增文件）

`src/ai_trader/_redaction_anchor.py`：

```python
"""跨 reload 稳定的信任锚。只存对象引用，无逻辑、无依赖、不被 reload。

本模块存在的唯一理由：``importlib.reload(log_redaction)`` 会重建该模块的
全部全局变量，使 ``_PRESTINE`` 丢失；而 ``sys.modules`` 里的兄弟模块不受影响。
"""
VERSION = 1
HOOKS: Dict[str, Any] = {}          # key -> 导入期抓到的真实函数对象
CAPTURED_AT = ""                    # 仅诊断用，不含路径/环境信息
```

### 5.2 捕获与判定

```python
def _capture_pristine() -> None:
    from . import _redaction_anchor as _anchor          # 循环导入安全：只读属性
    if _anchor.HOOKS:                                   # reload 后直接复用
        _PRESTINE.update(_anchor.HOOKS)
        return
    for key, owner, name in _HOOK_TARGETS:
        fn = getattr(owner, name, None)
        if _looks_like_stdlib(fn):                       # 仅此处使用 __module__
            _PRESTINE[key] = fn
    _anchor.HOOKS.update(_PRESTINE)

def _is_trusted_original(fn, key=None) -> bool:
    if fn is None:
        return False
    if key is not None:
        return fn is _PRESTINE.get(key)                  # 纯身份比较
    return any(fn is v for v in _PRESTINE.values())
```

行为矩阵：

| 情形 | `_PRESTINE[key]` | `_is_trusted_original` | 结果 |
|---|---|---|---|
| 首次导入、logging 干净 | 真函数 | True | 正常包装 |
| reload（锚点模块在） | 真函数（从 anchor 恢复） | True | **正常，不报 repaired** |
| reload 且 anchor 被清空 | None | False | fail-closed：装不上、`healthy=False` |
| 被第三方替换 | 真函数 | False | 链回真函数 + `foreign_hooks` 记录 |
| 被替换后再恢复 | 真函数 | True | 幂等 |
| 伪造 `__module__` | — | False（身份不符） | 视为 foreign |

### 5.3 `_wrap_method` 的三处替换

`:790-800` 现在是：

```python
if ours and _is_stdlib_hook(current_original):          # ← 改为身份
trusted = _PRESTINE.get(key)
if _is_stdlib_hook(current):                            # ← 改为身份（且仅在导入期语义下）
    trusted = current; _PRESTINE[key] = current         # ← 删除写入
elif _is_stdlib_hook(current_original):                 # ← 改为身份
```

改为：

```python
if ours and _is_trusted_original(current_original, key):
    return False
trusted = _PRESTINE.get(key)                             # 只认锚点，不认属性
if trusted is None:
    _FOREIGN_HOOKS[key] = {..., "chain_to": "none", "installed": False, "reason": "anchor_missing"}
    return False                                         # fail-closed
if current is trusted:
    pass                                                 # 已是原函数
elif current_original is trusted:
    pass                                                 # 是我们的旧包装
else:
    _FOREIGN_HOOKS[key] = {..., "chain_to": "trusted", "installed": True}
```

**`_aitrader_redaction` / `_aitrader_original` / `_aitrader_trusted` 降级为纯标记**：只用于幂等判断与诊断展示，**任何信任决策都不得读取它们**。这样 `R-202-C`（伪造属性）失效。

### 5.4 健康位（R-202-B）

`_hook_state` 的 `trusted` 改为：

```python
original = getattr(current, "_aitrader_original", None)
trusted = installed and _is_trusted_original(original, key)
anchor_missing = _PRESTINE.get(key) is None
return {"installed": installed, "trusted_original": trusted,
        "anchor_missing": anchor_missing, ...}
```

`redaction_health()`：

- `hooks_ok` 追加 `not any(s["anchor_missing"] ...)`；
- 新增只读字段 `anchor_missing: bool`（只加字段，不改既有字段语义）；
- `healthy=False` 时 `runtime.setup_logging`（`runtime.py:241`）会打印告警，语义从"误报健康"变为"如实不健康"。

**验收**：reload 后 `ensure_hooks()` 必须满足 `healthy=True` **且** `repaired` 三项全 False **且** `_PRESTINE` 三项非 None（三者同时成立才算通过；当前代码只满足前两项）。

---

## 6. D. B1-05 修订设计（PEM 合法性）

### 6.1 解析器状态机

```python
def _parse_dotenv_text(text: str) -> Tuple[List[Tuple[str, str]], List[Dict[str, str]]]:
    text = text.lstrip("\ufeff")                     # BOM（R-105）
    lines = text.splitlines()                        # 统一 \r\n / \r / \n
```

| 输入形态 | 处理 | 与现状差异 |
|---|---|---|
| `K=V` 无引号单行 | `V` 只剥引号**外**的空白 | 不变（逐字节兼容） |
| `K="V"` 双引号单行 | 去引号 + 单遍解码 `\n \r \t \\ \"` | 新增解码 |
| `K='V'` 单引号 | 去引号，**不解码** | 同现状 |
| `K="多行..."` | 跨行累积到闭合引号，行间 `\n` | 新增（R-105-A） |
| `K=-----BEGIN …` 无引号 | **PEM 例外**：一直吃到同名 `-----END` 行 | 新增（B1-05-04 需要） |
| 未闭合引号 / 无 END | **不写入** + `unterminated_quote` / `pem_incomplete` | fail-closed |
| 文件带 BOM | `utf-8-sig` / `lstrip("\ufeff")` | 修键名污染 |
| `K=` 空值 | 写入 `""` | 不变 |
| 无 `=` 行 | 跳过 | 不变 |
| 值内 `#` | 保留 | 不变（B1-05-19） |

**归一化顺序（R-105-B 的关键）**：

```
原始字节 → utf-8-sig 解码 → splitlines()（吃掉所有换行变体）→ 按行拼装
        → 引号外 strip → 引号内保持原样（B1-05-15 的 65 字符）
        → CRLF 已在 splitlines 阶段消失，值内不会残留 \r
```

**禁止**：对值做二次 `strip()`（会吃掉 `K=" v "` 的引号内空格，B1-05-15 从 65 变 63）。

### 6.2 PEM 校验（三级，各司其职）

```python
_PEM_BLOCK = re.compile(r"-----BEGIN ([A-Z0-9 ]+)-----\n(.*?)\n-----END \1-----", re.S)

def _validate_pem(value: str) -> Tuple[bool, str]:
    v = value.replace("\r\n", "\n").replace("\r", "\n").strip()   # ★ 先归一化
    blocks = list(_PEM_BLOCK.finditer(v))
    if not blocks:
        return False, "pem_incomplete"
    if _PEM_BLOCK.sub("", v).strip():
        return False, "pem_residual"                # 多段之间的杂内容
    for m in blocks:
        body = "".join(m.group(2).split())
        if len(body) < 64:
            return False, "pem_body_too_short"
        if not re.fullmatch(r"[A-Za-z0-9+/=]+", body):
            return False, "pem_body_charset"
        if len(body) % 4:
            return False, "pem_body_padding"
        try:
            base64.b64decode(body, validate=True)
        except Exception:
            return False, "pem_body_b64"
    return True, "ok"
```

三级检查的职责边界（§1.2-D 的实测结论）：

| 级别 | 抓得住 | 抓不住 |
|---|---|---|
| 结构（BEGIN/END 同名、residual） | 缺 END、END 名不匹配、仅首行、空格代替换行 | body 截断、非法字符 |
| Base64（charset/padding/decode） | 非法字符、padding 错 | **整行截断（仍是合法 base64）** |
| SDK（`cryptography`，已声明依赖 `requirements.txt:3`） | 整行截断、DER 结构错 | 加密私钥（`BEGIN ENCRYPTED PRIVATE KEY`）会误判 |

因此 SDK 检查的定位是**诊断 + 有条件硬门禁**：

- 默认开启（`cryptography` 已是依赖）；
- 对 `BEGIN ENCRYPTED PRIVATE KEY` / 非 `PRIVATE KEY`/`PUBLIC KEY` 的标签**跳过** SDK 检查，只做结构与 Base64；
- SDK 失败 → `pem_unparsable` → **不写入**（fail-closed），并记录 `parse_errors`。

### 6.3 指纹与来源一致性

`load_dotenv` 的写入路径必须保持"三同源"：

```python
final = value                                    # 已归一化、已解码、已去引号
marked = _DotenvValue(final)
with internal_env_write():
    os.environ[key] = marked                     # 写入的对象
_DOTENV_FP[name] = _dotenv_fingerprint(final)    # 指纹的对象
_DOTENV_MARKED[name] = marked                    # 身份的对象
```

`_CredEnvRegistry.fingerprint(name, value)`（`fosun_adapter.py:217-225`）对值做**无归一化**的 sha256，因此"写入值 == 指纹输入值"是硬约束，差一个 `\r` 或一个空格都会判定失配。

### 6.4 来源判定方向的更正（重要）

`dotenv_origin`（`config.py:446`）在任一项无法确认时返回 False；`fosun_adapter._classify_source`（`:857`）的 explicit 分支要求 `not from_dotenv` 且**在 vault 之前 return**。

因此指纹失配的后果是：

```
指纹失配 → from_dotenv=False → 判 explicit_env → 立即返回本地值 → 保险箱不被查询
```

即 **本地破损值压过保险箱里的有效凭据**，而不是原计划写的"Vault 越级覆盖"。方向相反，但同样是 AUD2-B1-01 的回归面。修订后的测试必须同时固定两个方向：

- `FP-01`：多行 PEM 载入后 `dotenv_origin == True`（本地值仍被认作 dotenv）；
- `FP-02`：载入后同值 export → `dotenv_origin == False`（G-101 不被破坏）。

### 6.5 诊断接口

- `dotenv_source_diagnostics()` 增加 `parse_errors: int`（只加计数）；
- 新增 `dotenv_parse_errors() -> List[Dict[str, str]]`，元素形如 `{"key": "...", "reason": "unterminated_quote", "line": 3}`，**不含值**；
- 断言沿用 `test_credential_identity_b412.py:173` 的"诊断不含键名/值"约束（键名可含，值不可含）。

---

## 7. 数据流与异常路径

### 7.1 日志脱敏全链路（修订后）

```
业务调用 logger.info(...)
   │
   ├─ L2 Logger.handle(wrapper) ──► _sanitize_record(record)         [:458]
   │       ├─ msg/args → redact_text()        ← 规则表 + assignments + 字面量
   │       └─ extra 递归 redact_obj()          ← 不再有 flag 跳过分支
   │
   ├─ Handler.handle(wrapper) ──► RedactionFilter.filter(record)    [:528]
   │
   └─ Handler.format(wrapper) ──► text = original(self, record)     [:866]
           ├─ _needs_scan(text) == False → return text     ← 唯一"不脱敏"出口
           ├─ 摘要命中 → return cached_out                 ← 只返回已脱敏文本
           └─ 未命中 → redact_text(text, truncate=False) → 写缓存 → return out
```

### 7.2 异常路径（逐点规定）

| 故障点 | 行为 | 是否可能输出明文 |
|---|---|---|
| `_needs_scan` 内部抛异常 | 视为"需要扫描"（True） | 否 |
| 摘要计算抛异常 | `dg=None`，跳过缓存，直接 `redact_text` | 否 |
| 缓存读抛异常 | 视为未命中 | 否 |
| 缓存写抛异常 | 忽略，返回已脱敏 `out` | 否 |
| `redact_text` 内部抛异常 | 返回 `***`（`:320` 既有 fail-closed） | 否 |
| `WeakKeyDictionary` 不可用（解释器裁剪） | 模块导入时降级为"无缓存"模式（`_FORMAT_CACHE=None`） | 否（每 handler 各跑一次脱敏） |
| 锚点 `_PRESTINE[key] is None` | `install_*` 返回 False → `unsecured_handlers` 增加 → `healthy=False` | 由 handler 降级链兜住 |
| handler 三级降级全失败 | handler 进 `_UNSECURED_HANDLERS`，`healthy=False`，stderr 告警 | **可能**（既有边界，见 §11） |
| PEM 校验抛异常 | `pem_invalid` → 不写入 | 不适用 |

### 7.3 配置加载链路（B1-05 后）

```
.env 字节 → utf-8-sig → _parse_dotenv_text
   ├─ (k,v) 列表 ──► PEM 校验（若键名/值形态命中）──► 不通过 → parse_errors + 不写入
   └─ 通过 ──► _DotenvValue(v) ──► internal_env_write → os.environ[k]=marked
                                  ├─ _DOTENV_FP[k]   = fp(v)
                                  ├─ _DOTENV_MARKED[k] = marked
                                  └─ _DOTENV_KEYS.add(k)
                                          │
fosun_adapter._classify_source ──► dotenv_origin(k, raw) ──► from_dotenv
                                          │                        │
                                    explicit_env / vault / dotenv / injected / missing
```

---

## 8. 测试矩阵

### 8.1 批次 1（F2-04）

| ID | 输入 | 断言 | 必须能失败的变异 |
|---|---|---|---|
| KV-01 | `log.exception`，消息 `api_key=AK` | 输出不含 AK | 注释掉 `_redact_assignments` 调用 |
| KV-02 | `status: token=AK` | 不含 AK | 把 `pos = sep_end` 改成 `pos = end` |
| KV-03 | `data=api_key=AK` | 不含 AK | 同上 |
| KV-04 | `outer: middle: api_key=AK` | 不含 AK | 同上 |
| KV-05 | `api_key=abc=def` | `api_key: ***` | 值域排除 `=` 的写法会失败 |
| KV-06 | `api_key=AK\nsymbol=AAPL` | 不含 AK 且保留 `symbol=AAPL` | 去掉续行判定 |
| KV-07 | `api_key=PART1\nPART2` | 两行都不含 | — |
| TAIL-01 | `raise RuntimeError(f"api_key={AK}")` | 保留 `")` | 去掉 `rstrip` |
| TAIL-02 | 空格/逗号/分号/尖括号四种后缀 | 后缀逐字保留 | — |
| JSON-01 | `{"api_key":"a\"b"}` | 不含 `a\"b` | 回退旧 `_JSON_RE` 值分支 |
| JSON-03 | 4 层 `json.dumps` | 每层解开 `api_key=="***"`、`symbol=="AAPL"` | — |
| REG-01 | `register_secret('abc"defgh')` + JSON 转义形态 | 不含明文 | 删除转义投影 |
| URL-01 | `?api_key=x&b=1` | `api_key=***`，保留 `b=1` | 把 `_URL_RE` 移到 assignments 之后 |
| D-01 | `symbol=AAPL qty=100 price=195.32 side=buy` | 逐字节不变 | 值域放宽到行尾会失败 |
| PERF-GATE | 见 §10 | — | — |

### 8.2 批次 2（F2-02）

| ID | 场景 | 断言 |
|---|---|---|
| F202-02 | 伪造 `__module__="logging"` | `logger_hook_trusted=False`；`_PRESTINE` 身份不变 |
| F202-04 | `importlib.reload(log_redaction)` | 三项 `_PRESTINE` 非 None；`healthy=True`；`repaired` 全 False |
| F202-05 | reload 后新建 handler + `install_on_handler` | 返回 True，不落入 `degraded`/`unsecured` |
| F202-06 | 替换后恢复原函数 | `trusted=True`，`foreign_hooks` 清空 |
| F202-07 | **真实 Handler 输出对比** | reload 前后各写一条 `api_key=FAKE`，两份输出都必须 `api_key: ***` |
| F202-08 | 伪造 `_aitrader_original` / `_aitrader_trusted` | 仍判 foreign（属性不再参与决策） |
| F202-09 | 锚点模块被 reload（清空） | `healthy=False`、`anchor_missing=True`、日志被丢弃而非放行 |

### 8.3 批次 3（B1-05）

| ID | 场景 | 断言 |
|---|---|---|
| DOT-01…20 | 见 §9 映射表逐条 | 见映射表 |
| PEM-01 | 合法 PEM **尾部带 `\n`** | `ok`（R-105-B 防误杀） |
| PEM-02 | 合法 PEM **CRLF** | `ok`，值内无 `\r` |
| PEM-03 | 两段 PEM 连续 | `ok` |
| PEM-04 | body 含非法字符 | `pem_body_charset` → 不写入 |
| PEM-05 | 缺 END | `pem_incomplete` → 不写入 |
| PEM-06 | 整行截断（base64 合法） | `pem_unparsable` → 不写入（SDK 级） |
| PEM-07 | 加密私钥 `BEGIN ENCRYPTED PRIVATE KEY` | 跳过 SDK 检查，结构通过即 `ok` |
| FP-01 | 多行 PEM 载入后 `dotenv_origin` | `True`（指纹用最终值） |
| FP-02 | 载入后同值 export | `dotenv_origin == False`（G-101 不破） |
| SDK-01/02 | 假密钥 `load_pem_private_key` | 成功；仅首行必须失败 |
| ENV-01 | 全部用例 | **不读取真实 `.env`**；只在临时目录写假文件 |

### 8.4 批次 4（F2-03）

| ID | 场景 | 断言 |
|---|---|---|
| **F203-A1** | **同一 record 由第二个 Handler 处理** | 两路输出均不含明文；第二路返回的是缓存中的**已脱敏**文本；`redact_text` 计数 == 1 |
| **F203-A2** | **已脱敏记录被追加新秘密后再次处理** | 新秘密不在输出；`redact_text` 计数 == 2（摘要不符 → 重跑） |
| F203-03/05/08 | 伪造标记 / 直接 `format` / 自定义 LogRecord | 均不含明文；`grep _REDACTED_FLAG` == 0 |
| F203-06/12/13/14 | 追加 / copy / 后置 Filter / 后置 Formatter | 均不含明文 |
| F203-15 | 缓存读抛异常（打桩） | 仍不含明文，且 `redact_text` 被调用 |
| F203-16 | 缓存写抛异常 | 返回仍是已脱敏文本 |
| F203-17 | 10 万条记录流过 | `format_cache_stats()["size"]` ≤ 活跃 record 数；无 `id` 复用误命中 |
| F203-18 | `_QUICK_WORDS` 与 `_SENSITIVE_KEYS` | 集合完全相等 |
| F203-19 | `_needs_scan` 否定分支的样本集 | 100 条无敏感构造文本全部原样返回（零误伤） |
| F203-20 | 16 线程 × 1000 条并发 | `faults=0`、无明文、p95 ≤ 2× 单线程 |

---

## 9. 112 条映射表（修订后预期）

分类统计：**保持 70、翻绿 30、翻绿\* 2、改写 2、反转 1、边界保留 7**（合计 112）。

- **保持**：断言文本不变；其中 `B1-05-01/02` 的实际值会从 27 变为 573（旧断言是"不断言"，故分类仍为保持），实施时**新增** `DOT-01b/02b` 显式断言，不改旧断言。
- **翻绿**：断言本身正确，当前 FAIL 只因缺陷存在；**不得**为了让它们通过而改预期。
- **翻绿\***：断言范围需在实施 PR 中确认（`B1-05-05` 的引号形态已核实；`RG-F2-02-B` 的断言范围）。
- **改写**：预期文本从"不断言/静默接受"改为明确 fail-closed（`B1-05-09/14`）。
- **反转**：`F2-04-14T` 把"允许泄露"写成期望，必须删除并改为"不含明文"。
- **边界保留**：无键名且未登记的裸值、直接 `Formatter` 路径；断言改为**显式声明"不识别"**，禁止表述为安全能力。

| # | 原编号 | 原输入 | 旧源码输出 | 正确目标输出 | 修订后测试断言 | 分类 | 理由 |
|---|---|---|---|---|---|---|---|
| 1 | `B1-05-01` | 单行（真实换行被压平）不带引号 | `env_len=27` | env_len=573（PEM 例外条款生效） | 旧断言不变（不断言）；新增 DOT-01b 断言 len==573 | 保持 | 与 04 同字节输入，必须同结果；旧断言未约束 |
| 2 | `B1-05-02` | 单行双引号包裹 | `env_len=27` | env_len=573（跨行引号闭合） | 旧断言不变；新增 DOT-02b 断言 len==573 | 保持 | 引号跨行是 dotenv 合法写法，当前被逐行截断 |
| 3 | `B1-05-03` | 标准多行 PEM + 双引号（dotenv 常见写法） | `env_len=27` | env_len=573 | 断言 len==573 且 sha 一致 | 翻绿 | 跨行状态机 + 引号闭合 |
| 4 | `B1-05-04` | 标准多行 PEM 无引号 | `env_len=27` | env_len=573 | 断言 len==573 且 sha 一致 | 翻绿 | 无引号 PEM 例外条款（值以 -----BEGIN 开头） |
| 5 | `B1-05-05` | 转义写法 \n（单行） | `env_len=582` | env_len=573 | 断言 len==573 | 翻绿* | 双引号内解码 \n；已核对用例确为双引号包裹（probe:147） |
| 6 | `B1-05-06` | 多行 + CRLF 换行 | `env_len=27` | env_len=573 且无 \r | 断言 len==573 且 '\r' not in value | 翻绿 | CRLF→LF 归一后再拼接 |
| 7 | `B1-05-07` | 多行 + 文件末尾无换行 | `env_len=27` | env_len=573 | 断言 len==573 | 翻绿 | 无尾换行不影响闭合判定 |
| 8 | `B1-05-08` | 同文件多字段（PEM + 3 个普通键） | `env_len=27` | env_len=573，另 3 键原值 | 断言 PEM 完整且 LIVE/URL/EMPTY 不受影响 | 翻绿 | 引号内闭合后立即结束该键，不吞后续键 |
| 9 | `B1-05-09` | 缺失 END 标记 | `env_len=27` | 键不存在（不写入） | 断言 key not in environ 且 parse_errors==1(pem_incomplete) | 改写 | fail-closed：半截凭据比没有更危险 |
| 10 | `B1-05-10` | 多行中夹空行与注释 | `env_len=27` | env_len=573 | 断言 len==573 | 翻绿 | 空行/注释在引号内属值内容 |
| 11 | `B1-05-11` | 值自身含 '='（base64 填充） | `env_len=8` | env_len=8（abc==def） | 断言值 == 'abc==def' | 保持 | 只按首个 = 切分，回归护栏 |
| 12 | `B1-05-12` | 无等号行（应跳过） | `env_len=1` | env_len=1（v） | 断言无 '=' 行被跳过 | 保持 | 不把无等号行当续行 |
| 13 | `B1-05-13` | 单引号包裹 | `env_len=64` | env_len=519 | 断言 len==519 且单引号内不解码 | 翻绿 | 单引号保留字面量 |
| 14 | `B1-05-14` | 未闭合双引号 | `env_len=64` | 键不存在（不写入） | 断言 key not in environ 且 parse_errors==1(unterminated_quote) | 改写 | 未闭合不得静默写入 64 字符 |
| 15 | `B1-05-15` | 值前后带空格 | `env_len=65` | env_len 不变（引号内首尾空格保留） | 断言值保留引号内空格 | 保持 | 只在引号外 strip；不得二次 strip |
| 16 | `B1-05-16` | 空文件 | `env_len=None` | return=False | 断言返回 False 且不抛异常 | 保持 | 空文件 |
| 17 | `B1-05-17` | 仅注释文件 | `env_len=None` | return=False | 断言返回 False | 保持 | 仅注释 |
| 18 | `B1-05-18` | 键名带 UTF-8 BOM | `env_len=None` | 键名 AUD3B105_PEM，值 1 字符 | 断言 '\ufeff' not in keys 且 new_env_keys==[k] | 翻绿 | utf-8-sig 去 BOM，不能污染键名 |
| 19 | `B1-05-19` | 值含 '#' | `env_len=7` | env_len=7（abc#def） | 断言值 == 'abc#def' | 保持 | 值内 # 不是注释起始，回归护栏 |
| 20 | `B1-05-20` | 已存在的同名环境变量（setdefault 语义） | `env_len=12` | env_len=12（from_process） | 断言进程值未被覆盖 | 保持 | setdefault 语义 |
| 21 | `RG-F2-01-A` | 三级降级全失败（filters 冻结 / handle 只读 / setLevel 空操 | `{"install_return": false, "mode": "unsec…` | 与旧输出一致 | 断言 install=False / unsecured>=1 / blocked=0 | 保持 | AUD2-F2-01 回归护栏 |
| 22 | `RG-F2-01-B` | setLevel 直接抛异常 | `{"install_return": false, "unsecured": 1…` | 与旧输出一致 | 断言同上 | 保持 | AUD2-F2-01 回归护栏 |
| 23 | `RG-F2-01-C` | 对照组：setLevel 正常 | `{"install_return": true, "blocked": 1, "…` | 与旧输出一致 | 断言 install=True / blocked>=1 / unsecured=0 | 保持 | 回读校验必须真的禁用 |
| 24 | `RG-F2-02-A` | Logger.handle 被换成 no-op 后 ensure_hooks() | `{"logger_hook_trusted": true, "logger_ho…` | 自愈且脱敏恢复 | 断言 trusted=True 且 _PRESTINE 身份不变 | 保持 | AUD2-F2-02 回归护栏 |
| 25 | `RG-F2-02-B` | 伪造 __module__='logging' 的 no-op 替换 | `{"logger_hook_trusted": true, "healthy":…` | trusted=False 且自愈、锚点不污染 | 断言 logger_hook_trusted=False；新增断言 _PRESTINE 身份 == 导入期对象 | 翻绿* | 伪造 __module__ 不得被信任；断言范围需实施时确认 |
| 26 | `RG-F2-02-C` | 重复 ensure_hooks 幂等 | `{"first": {"handler": false, "logger": f…` | repaired 全 False、healthy=True | 断言两次调用结果一致 | 保持 | 幂等 |
| 27 | `RG-D1-A` | 正常关闭（1000/意图）与故障区分 | `{"results": {"code1000_frame": true, "co…` | 1000→True / 1011→False | 断言三值 | 保持 | 与本次改动无关的回归护栏 |
| 28 | `F2-03-01` | log.error('api_key=%s', FAKE, extra={'_aitra | `api_key: ***` | api_key: *** | 断言不含明文 | 保持 | L1 仍执行 |
| 29 | `F2-03-02` | record.__dict__['_aitrader_redacted']=True;  | `api_key: ***` | api_key: *** | 断言不含明文 | 保持 | L2 仍执行 |
| 30 | `F2-03-03` | install_on_handler(h); record._aitrader_reda | `api_key=FAKE-F203-APIKEY-7c1d4a9b` | 不含 FAKE-F203-APIKEY | 断言 not contains 且标记常量已删除 | 翻绿 | 删除可伪造标记 |
| 31 | `F2-03-04` | install_on_handler(h); h.emit(record)  # 无标记 | `api_key: ***` | api_key: *** | 断言不含明文 | 保持 | L3 兜底对照 |
| 32 | `F2-03-05` | record._aitrader_redacted=True; h.format(rec | `api_key=FAKE-F203-APIKEY-7c1d4a9b` | 不含 FAKE-F203-APIKEY | 断言 not contains | 翻绿 | L3 按文本摘要判定，不读标记 |
| 33 | `F2-03-06` | filter(record); record.msg='rotated api_key= | `rotated api_key=FAKE-F203-NEWSECRET-2e5f…` | 两段都打码（旧+新秘密） | 断言不含 FAKE-F203-NEWSECRET | 翻绿 | 文本变化→摘要不符→重脱敏 |
| 34 | `F2-03-07` | a.emit(record); b.emit(record) | `A='api_key: ***' B='api_key: ***'` | 两路均 api_key: *** | 断言两路一致且无明文 | 保持 | 多 handler 扇出 |
| 35 | `F2-03-08` | class LyingRecord(LogRecord): __getattr__('_ | `api_key=FAKE-F203-APIKEY-7c1d4a9b` | 不含 FAKE-F203-APIKEY | 断言 not contains | 翻绿 | 自定义 LogRecord 无标记可伪造 |
| 36 | `F2-03-09` | filter(record) x2 | `once='user=alice token: *** count=5' twi…` | 幂等、user/count 保留 | 断言 once==twice 且普通字段不变 | 保持 | 幂等性 |
| 37 | `F2-03-10` | 8 threads call filter(record) on the same re | `faults=[] msg='api_key: *** session_id: …` | 8 线程无异常无泄漏 | 断言 faults==[] 且无明文 | 保持 | 并发安全 |
| 38 | `F2-03-11` | logging.Formatter('%(message)s').format(reco | `api_key=FAKE-F203-APIKEY-7c1d4a9b` | 仍不覆盖（直接 Formatter） | 断言显式标注 KNOWN_GAP，不宣称已覆盖 | 边界保留 | 绕过 Handler.format 的路径本批不改 |
| 39 | `F2-03-12` | clone = copy.copy(sanitized); clone.msg='ret | `retry api_key=FAKE-F203-NEWSECRET-2e5f8a…` | 不含 FAKE-F203-NEWSECRET | 断言 not contains | 翻绿 | copy 产生新对象→未命中→重脱敏 |
| 40 | `F2-03-13` | h.addFilter(Enrich); log.error('request done | `request done enriched_api_key=FAKE-F203-…` | enriched_api_key: *** | 断言不含 FAKE-F203-NEWSECRET | 翻绿 | Filter 追加后文本变化→重脱敏 |
| 41 | `F2-03-14` | Formatter.format 末尾追加 ctx_api_key=...; log.e | `request done ctx_api_key=FAKE-F203-NEWSE…` | ctx_api_key: *** | 断言不含 FAKE-F203-NEWSECRET | 翻绿 | Formatter 追加后文本变化→重脱敏 |
| 42 | `F2-04-01` | 已登记字面量出现在自由文本 | `handshake done with ***` | handshake done with *** | 断言不含 FAKE-F204-TOKEN | 保持 | 已登记字面量兜底 |
| 43 | `F2-04-02` | 未登记裸字面量、无键名 | `handshake done with FAKE-F204-TOKEN-3a7c…` | 原样（不识别） | 断言显式声明'未登记裸值不识别'，禁止写成安全能力 | 边界保留 | 无键名无登记，任何规则都无法识别 |
| 44 | `F2-04-03` | 未登记 + api_key= | `api_key: ***` | api_key: *** | 断言不含 FAKE | 保持 | 基础 KV |
| 45 | `F2-04-04` | 未登记 + 嵌套 JSON sessionId | `payload={"data":{"sessionId":"***"}}` | payload={"data":{"sessionId":"***"}} | 断言不含 FAKE | 保持 | 结构化 extra 递归 |
| 46 | `F2-04-05` | 未登记 + URL X-Session | `GET /v1/q?X-Session: ***` | GET /v1/q?X-Session: *** | 断言不含 FAKE | 保持 | URL 规则前置 |
| 47 | `F2-04-06` | 未登记 + 异常消息 api_key= | `request failed Traceback (most recent ca…` | traceback 尾行不含 FAKE | 断言不含 FAKE | 翻绿 | 吞并修复后尾行被扫描 |
| 48 | `F2-04-07` | 未登记 + traceback 里的裸字面量 | `request failed Traceback (most recent ca…` | 原样（不识别） | 断言显式声明不识别 | 边界保留 | 无键名无登记 |
| 49 | `F2-04-08` | 未登记 + extra 敏感键 | `context attached` | context attached | 断言无明文 | 保持 | 不误伤 |
| 50 | `F2-04-09` | 未登记 + extra 非敏感键 blob | `context attached` | 不含 FAKE | 断言不含 FAKE | 翻绿 | 多行场景同 06 |
| 51 | `F2-04-10` | 同一条记录两个秘密，只登记一个 | `a=*** b=FAKE-F204-TOKEN2-5b8d2f4a` | a=*** b=<原样> | 断言 a 打码、b 不识别 | 边界保留 | b 无键名无登记 |
| 52 | `F2-04-11` | 短值（<8）登记后 + api_key= | `api_key: ***` | api_key: *** | 断言不含 FAKE | 保持 | 基础 KV |
| 53 | `F2-04-12` | 短值（<8）裸字面量 | `value is FAKE-7x` | 原样（不识别） | 断言显式声明不识别 | 边界保留 | 无键名无登记 |
| 54 | `F2-04-13` | 含换行的值 + api_key= | `api_key: *** FAKE-F204-PART2` | 两行都不含 FAKE | 断言 FAKE-PART2 不出现 | 翻绿 | 续行纳入同一值域 |
| 55 | `F2-04-14` | 含转义引号的值 + JSON | `payload={"api_key":"FAKE-F204-\"quoted\"…` | payload={"api_key":"***"} | 断言不含 FAKE 且含 "***" | 翻绿 | JSON 值分支允许 \x 转义 |
| 56 | `F2-04-15` | 未登记 Bearer 令牌 | `Authorization failed for Bearer ***` | Authorization failed for Bearer *** | 断言不含 sk- | 保持 | Bearer 规则 |
| 57 | `F2-04-16` | 未登记 PEM 私钥块 | `loaded pem: ***` | loaded pem:\n*** | 断言不含 BEGIN/body | 保持 | PEM 整块 |
| 58 | `F2-04-17` | Authorization 头未登记 | `request headers: Authorization: ***` | request headers: Authorization: *** | 断言不含凭据 | 保持 | AUTH 规则 |
| 59 | `F2-04-18` | 8 层嵌套里的 api_key | `deep={'n': {'n': {'n': {'n': {'n': {'n':…` | 深层结构不崩 | 断言无明文且不抛异常 | 保持 | 深度预算 |
| 60 | `F2-04-19` | 150 项列表末尾的 api_key | `many=[{'i': 0}, {'i': 1}, {'i': 2}, {'i'…` | 大列表不崩 | 断言无明文且节点数受限 | 保持 | 节点预算 |
| 61 | `F2-04-20` | 普通业务文本（D 类） | `symbol=AAPL qty=100 price=195.32 side=bu…` | symbol=AAPL qty=100 price=195.32 side=buy | 断言逐字节不变 | 保持 | 零误伤（最重要回归） |
| 62 | `F2-04-X01` | extra 精确敏感键 auth_token | `ctx \| v=*** ` | ctx \| v=*** | 断言不含 FAKE | 保持 | extra 敏感键 |
| 63 | `F2-04-X02` | extra 驼峰 apiKey | `ctx \| v=*** ` | ctx \| v=*** | 断言不含 FAKE | 保持 | camelCase 归一 |
| 64 | `F2-04-X03` | extra 后缀 upstream_session_id | `ctx \| v=*** ` | ctx \| v=*** | 断言不含 FAKE | 保持 | 后缀匹配 |
| 65 | `F2-04-X04` | extra 非敏感键 blob | `ctx \| v=FAKE-F204X-TOKEN-6d1a8b3c ` | ctx \| v=<原样> | 断言显式声明 blob 非敏感键不打码 | 边界保留 | 键名不敏感 |
| 66 | `F2-04-X05` | extra token_count（白名单后缀） | `ctx \| v=FAKE-F204X-TOKEN-6d1a8b3c ` | ctx \| v=<原样> | 断言显式声明不识别 | 边界保留 | 键名不含敏感词 |
| 67 | `F2-04-X06` | extra dict 内含 api_key | `ctx \| v={'api_key': '***'} ` | ctx \| v={'api_key': '***'} | 断言不含 FAKE | 保持 | 嵌套 dict |
| 68 | `F2-04-X07` | extra 值为 PEM | `ctx \| v=*** ` | ctx \| v=*** | 断言不含 FAKE | 保持 | material 键 |
| 69 | `F2-04-X08` | extra 值为异常对象 | `ctx \| v=RuntimeError: api_key=FAKE-F204X…` | ctx \| v=RuntimeError: api_key: *** | 断言不含 FAKE | 翻绿 | extra 内异常走 redact_obj→吞并修复 |
| 70 | `F2-04-S01` | 类名前缀 + api_key=（traceback 形态） | `RuntimeError: api_key=FAKE-F204X-TOKEN-6…` | RuntimeError: api_key: *** | 断言不含 FAKE | 翻绿 | 吞并修复（最小用例） |
| 71 | `F2-04-S02` | status: token= | `status: token=FAKE-F204X-TOKEN-6d1a8b3c ` | status: token: *** | 断言不含 FAKE | 翻绿 | 双层：非敏感 status 不吞 token |
| 72 | `F2-04-S03` | error: password= | `error: password=FAKE-F204X-TOKEN-6d1a8b3…` | error: password: *** | 断言不含 FAKE | 翻绿 | 同上 |
| 73 | `F2-04-S04` | 冒号后双空格 | `RuntimeError:  api_key=FAKE-F204X-TOKEN-…` | RuntimeError:  api_key: *** | 断言不含 FAKE | 翻绿 | 双空格不阻断定位 |
| 74 | `F2-04-S05` | 无外层前缀（对照） | `api_key: *** ` | api_key: *** | 断言不含 FAKE | 保持 | 基础 |
| 75 | `F2-04-S06` | 破折号前缀（对照） | `- api_key: *** ` | - api_key: *** | 断言列表项不误伤 | 保持 | 前缀符号 |
| 76 | `F2-04-S07` | data=api_key= | `data=api_key=FAKE-F204X-TOKEN-6d1a8b3c ` | data=api_key: *** | 断言不含 FAKE | 翻绿 | 嵌套赋值，游标前移规则 |
| 77 | `F2-04-S08` | 两层外层键 | `outer: middle: api_key: *** ` | outer: middle: api_key: *** | 断言不含 FAKE | 保持 | 三层已可打码 |
| 78 | `F2-04-S09` | 真实 traceback 尾部 | `request failed Traceback (most recent ca…` | traceback 尾行不含 FAKE | 断言不含 FAKE | 翻绿 | 多行 traceback |
| 79 | `F2-04-06R` | 异常消息 api_key= （全量） | `request failed Traceback (most recent ca…` | 尾行不含 FAKE | 断言不含 FAKE | 翻绿 | 与 06 同型 |
| 80 | `F2-04-07R` | 异常消息裸字面量（全量） | `request failed Traceback (most recent ca…` | 尾行不含 FAKE | 断言不含 FAKE | 翻绿 | 与 07 同型 |
| 81 | `F2-04-09R` | extra 非敏感键 blob（全量） | `context attached ` | context attached | 断言无明文 | 保持 | 不误伤 |
| 82 | `F2-04-09S` | extra 键 session_blob（是否后缀命中） | `context attached ` | context attached | 断言无明文 | 保持 | 不误伤 |
| 83 | `F2-04-13R` | 值含换行 + api_key=（全量） | `api_key: *** FAKE-F204-PART2 ` | 两行均打码 | 断言 FAKE-PART2 不出现 | 翻绿 | 续行 |
| 84 | `F2-04-13S` | 值含换行 + JSON（转义后） | `payload={"api_key":"***"} ` | payload={"api_key":"***"} | 断言不含 FAKE | 保持 | JSON 已覆盖 |
| 85 | `F2-04-14R` | 值含转义引号 + JSON（全量） | `payload={"api_key":"FAKE-F204-\"quoted\"…` | payload 内值打码 | 断言不含 FAKE | 翻绿 | 转义引号 |
| 86 | `F2-04-14S` | 值含转义引号 + api_key= | `api_key: *** ` | api_key: *** | 断言不含 FAKE | 保持 | 基础 |
| 87 | `F2-04-14T` | json.dumps 真实转义引号 + JSON | `payload={"api_key": "FAKE-F204-\"quoted\…` | payload={"api_key": "***"} | 断言不含 FAKE 且含 "***"（旧断言"允许泄露"必须删除） | 反转 | 有键名即可修复，旧预期把泄露写成正确行为 |
| 88 | `F2-04-21` | 值含空格 + api_key= | `api_key: *** SPACE-9f1 ` | api_key: *** SPACE-9f1 | 断言后缀保留且不含值 | 保持 | 结构尾巴 |
| 89 | `F2-04-22` | 值含逗号 + api_key= | `api_key: ***,COMMA-9f1 ` | api_key: ***,COMMA-9f1 | 断言逗号后缀保留 | 保持 | 结构尾巴 |
| 90 | `F2-04-23` | 值含分号 + api_key= | `api_key: ***;SEMI-9f1 ` | api_key: ***;SEMI-9f1 | 断言分号后缀保留 | 保持 | 结构尾巴 |
| 91 | `F2-04-24` | 值含尖括号 + api_key= | `api_key: ***>ANGLE-9f1 ` | api_key: ***>ANGLE-9f1 | 断言尖括号后缀保留 | 保持 | 结构尾巴 |
| 92 | `SRC-01` | 基线：load 后未改动 | `origin=True,exception=None,env_present=T…` | origin=True,exception=None,env_present=T… | 断言 dotenv_origin 结果不变 | 保持 | 来源追踪器不得被 B1-05 破坏 |
| 93 | `SRC-02` | 载入后写成**不同**值 | `origin=False,exception=None,env_present=…` | origin=False,exception=None,env_present=… | 断言 dotenv_origin 结果不变 | 保持 | 来源追踪器不得被 B1-05 破坏 |
| 94 | `SRC-03` | 载入后写成**完全相同**的值（AUD2-B1-01 核心场景） | `origin=False,exception=None,env_present=…` | origin=False,exception=None,env_present=… | 断言 dotenv_origin 结果不变 | 保持 | 来源追踪器不得被 B1-05 破坏 |
| 95 | `SRC-04` | 载入后写入另一个 _DotenvValue 同值对象（模拟伪造标记） | `origin=False,exception=None,env_present=…` | origin=False,exception=None,env_present=… | 断言 dotenv_origin 结果不变 | 保持 | 来源追踪器不得被 B1-05 破坏 |
| 96 | `SRC-05` | os.environ.update({k: 同值}) | `origin=False,exception=None,env_present=…` | origin=False,exception=None,env_present=… | 断言 dotenv_origin 结果不变 | 保持 | 来源追踪器不得被 B1-05 破坏 |
| 97 | `SRC-06` | os.environ \|= {k: 同值} | `origin=False,exception=None,ior=ok` | origin=False,exception=None,ior=ok | 断言 dotenv_origin 结果不变 | 保持 | 来源追踪器不得被 B1-05 破坏 |
| 98 | `SRC-07` | os.putenv(k, 同值)（C 层，不经 os.environ） | `origin=True,exception=None,putenv_visibl…` | origin=True,exception=None,putenv_visibl… | 断言 dotenv_origin 结果不变 | 保持 | 来源追踪器不得被 B1-05 破坏 |
| 99 | `SRC-08` | os.environ.pop 后重新写同值 | `origin=False,exception=None,env_present=…` | origin=False,exception=None,env_present=… | 断言 dotenv_origin 结果不变 | 保持 | 来源追踪器不得被 B1-05 破坏 |
| 100 | `SRC-09` | os.environ.pop 后不重设 | `origin=False,exception=None,env_present=…` | origin=False,exception=None,env_present=… | 断言 dotenv_origin 结果不变 | 保持 | 来源追踪器不得被 B1-05 破坏 |
| 101 | `SRC-10` | os.environ.clear() 后重写同值 | `origin=False,exception=None,env_present=…` | origin=False,exception=None,env_present=… | 断言 dotenv_origin 结果不变 | 保持 | 来源追踪器不得被 B1-05 破坏 |
| 102 | `SRC-11` | 载入前已有同名变量（setdefault 语义） | `origin=False,exception=None,load_return=…` | origin=False,exception=None,load_return=… | 断言 dotenv_origin 结果不变 | 保持 | 来源追踪器不得被 B1-05 破坏 |
| 103 | `SRC-12` | 载入后直接改 os.environ._data（绕过 __setitem__） | `origin=False,exception=None,data_patch=p…` | origin=False,exception=None,data_patch=p… | 断言 dotenv_origin 结果不变 | 保持 | 来源追踪器不得被 B1-05 破坏 |
| 104 | `SRC-13` | internal_env_write 包裹的同值写（受控写入） | `origin=True,exception=None,env_present=T…` | origin=True,exception=None,env_present=T… | 断言 dotenv_origin 结果不变 | 保持 | 来源追踪器不得被 B1-05 破坏 |
| 105 | `SRC-14` | 元数据丢失（_reset_dotenv_keys） | `origin=False,exception=None,env_present=…` | origin=False,exception=None,env_present=… | 断言 dotenv_origin 结果不变 | 保持 | 来源追踪器不得被 B1-05 破坏 |
| 106 | `SRC-15` | 8 线程并发写同值 | `origin=False,exception=None,threads=no-e…` | origin=False,exception=None,threads=no-e… | 断言 dotenv_origin 结果不变 | 保持 | 来源追踪器不得被 B1-05 破坏 |
| 107 | `SRC-16` | 先载入 a.env 再载入 b.env（同键） | `origin=True,exception=None,b_present=b-v…` | origin=True,exception=None,b_present=b-v… | 断言 dotenv_origin 结果不变 | 保持 | 来源追踪器不得被 B1-05 破坏 |
| 108 | `SRC-17` | 同一文件重复载入（幂等） | `origin=True,exception=None,env_present=T…` | origin=True,exception=None,env_present=T… | 断言 dotenv_origin 结果不变 | 保持 | 来源追踪器不得被 B1-05 破坏 |
| 109 | `SRC-18` | 包装器不可用时的降级（Windows 对象身份） | `origin=True,exception=None,installed=Fal…` | origin=True,exception=None,installed=Fal… | 断言 dotenv_origin 结果不变 | 保持 | 来源追踪器不得被 B1-05 破坏 |
| 110 | `SRC-19` | 包装器不可用 + POSIX 字节存储（模拟） | `origin=False,exception=None,simulated=by…` | origin=False,exception=None,simulated=by… | 断言 dotenv_origin 结果不变 | 保持 | 来源追踪器不得被 B1-05 破坏 |
| 111 | `SRC-20` | 值指纹被清空（只有键名，早期记录形态） | `origin=False,exception=None,env_present=…` | origin=False,exception=None,env_present=… | 断言 dotenv_origin 结果不变 | 保持 | 来源追踪器不得被 B1-05 破坏 |
| 112 | `SRC-21` | 两线程同时 load_dotenv 不同文件（并发初始化） | `origin=True,exception=None,errors=no-err…` | origin=True,exception=None,errors=no-err… | 断言 dotenv_origin 结果不变 | 保持 | 来源追踪器不得被 B1-05 破坏 |

---

## 10. 性能验收标准

### 10.1 八类基准（分别验收，不得只看 1 MB）

| 类 | 场景 | 长度 | 现行基线 | 门槛 |
|---|---|---|---|---|
| P1 | 普通业务行（无敏感词）× 20k | 64 B | 22.11 µs/次 | **≥60% 吞吐**（≤36.9 µs） |
| P2 | 含单个敏感键的普通行 × 20k | 64 B | 22.11 µs/次 | ≥60%（≤36.9 µs） |
| P3 | 20k 个 `k=v`（无敏感） | 257 KB | 85.9 ms | ≥60%（≤143 ms） |
| P4 | 10k 个敏感键 | 139 KB | 21.1 ms | ≥60%（≤35 ms） |
| P5 | 200k 个转义引号 | 400 KB | 32.5 ms | ≥60%（≤54 ms） |
| P6 | 10k 个 JSON 片段 | 199 KB | 37.8 ms | ≥60%（≤63 ms） |
| P7 | 1 MB 单行（含敏感键） | 1 MB | 1147.8 ms | ≤200 ms（现行属病态，改善项） |
| P8 | 1 MB 无分隔符 | 1 MB | 1145.2 ms | ≤50 ms |

`_needs_scan` 门控的作用：P1/P3/P6 等"无敏感构造"文本在门控处直接返回，不进入扫描器，因此 P1 的增量只剩一次正则 `search`（目标 ≤3 µs，**需实测填表**，不得估算）。

### 10.2 附加指标（每批都要记录）

| 指标 | 门槛 |
|---|---|
| 延迟 p50 / p95 / p99（P1、P2） | p95 增量 ≤20 µs；p99 增量 ≤50 µs |
| 内存峰值（10 万条后） | RSS 增量 ≤5 MB；`gc.collect()` 后 `format_cache_stats()["size"]` == 活跃 record 数 |
| 误报（D 类文本） | `symbol/qty/price/side` 零改动（P1 的 20 条样本逐字节比对） |
| 漏报（32 条 FAIL 变绿后的复跑） | 全绿，且 `actual` 中不出现任何 `FAKE-` 片段 |
| 缓存命中率（扇出 3 handler） | `hits/(hits+misses) ≥ 60%` |

### 10.3 不允许的做法

- 把门槛降到 40%；
- 用 P7 的巨大改善抵消 P1–P6 的退化；
- 用"平均耗时"掩盖单类超标；
- 关闭 `_needs_scan` 以换取实现简单。

---

## 11. 实施分批、文件清单、备份与回滚

### 11.1 顺序与理由

| 批次 | 内容 | 为何在此位置 |
|---|---|---|
| **B1** | F2-04（规则 + 游标 + 转义投影） | 唯一改变"文本层"的批次；B4 让 L3 每次都跑，若先做 B4 会把 F2-04 的泄露放大一倍 |
| **B2** | F2-02（锚点模块 + 身份判定 + 健康位） | 与文本层正交；纯钩子层，输出格式零变化，适合独立验证 |
| **B3** | B1-05（解析器 + PEM 校验） | 只动 `config.py`，与日志链无交集；可独立回滚 |
| **B4** | F2-03（缓存结构重写） | 依赖 B1 的文本层已正确；依赖 B2 的健康位如实 |
| **B5** | 全量回归 + 独立审计 | 见 §11.4 |

与任务书建议顺序一致。**不建议**把 B2 提前到 B1 之前：虽然 B2 更"独立"，但 B1 的 FAIL 探针最多（32 条中 19 条与文本层相关），先修它能让后续批次的回归基线立即变干净，减少"在噪声中判断"的成本。

### 11.2 每批文件清单

**B1 — F2-04**

| 文件 | 位置 | 改动 |
|---|---|---|
| `src/ai_trader/log_redaction.py` | `:171` `_VALUE` | 删除 |
| 同上 | `:173-175` `_KV_RE` | 降级为定位器（保留名） |
| 同上 | `:179-184` `_JSON_RE` | 值分支允许 `\\.` |
| 同上 | `:191-197` `_kv_sub` | 删除打码职责 |
| 同上 | `:221-229` `_RULES` | 去 `_KV_RE` 项，`_URL_RE` 前置 |
| 同上 | `:285-324` `redact_text` | 插入 `_redact_assignments` + 转义投影 |
| 同上（新增） | `_scan_value` / `_mask_span` / `_redact_assignments` / `_needs_scan` | 4 个私有函数 |
| `tests/test_log_redaction_b414.py`（新增） | — | §8.1 |

**B2 — F2-02**

| 文件 | 位置 | 改动 |
|---|---|---|
| `src/ai_trader/_redaction_anchor.py`（新增） | — | `HOOKS` / `VERSION` / `CAPTURED_AT` |
| `src/ai_trader/log_redaction.py` | `:584-595` `_capture_pristine` | 优先从 anchor 恢复 |
| 同上 | `:578` `_is_stdlib_hook` | 仅导入期使用（注释+调用点收敛） |
| 同上 | `:768-820` `_wrap_method` | 三处判定改身份；删 `_PRESTINE[key]=current` |
| 同上 | `:886-900` `_hook_state` | `trusted` 改身份；新增 `anchor_missing` |
| 同上 | `:901-935` `redaction_health` | `healthy` 纳入 `anchor_missing` |
| `tests/test_log_redaction_b414.py` | — | §8.2 |

**B3 — B1-05**

| 文件 | 位置 | 改动 |
|---|---|---|
| `src/ai_trader/config.py` | `:508-540` `load_dotenv` | 改为调用解析器 + 三同源写入 |
| 同上（新增） | `_parse_dotenv_text` / `_validate_pem` / `dotenv_parse_errors` | 3 个函数 |
| 同上 | `:402-413` `dotenv_source_diagnostics` | 增加 `parse_errors` 计数 |
| `tests/test_config_b414.py`（新增） | — | §8.3 |

**B4 — F2-03**

| 文件 | 位置 | 改动 |
|---|---|---|
| `src/ai_trader/log_redaction.py` | `:455` | 删除 `_REDACTED_FLAG` |
| 同上 | `:458-508` | 删 extra 跳过分支；删 flag 写入 |
| 同上 | `:866-883` `install_format_guard` | 新缓存结构 + `_needs_scan` 门控 |
| 同上（新增） | `_FORMAT_CACHE` / `_FORMAT_LOCK` / `_DIGEST_SALT` / `_digest_text` / `format_cache_stats` | 缓存与诊断 |
| `tests/test_log_redaction_b414.py` | — | §8.4 的 `F203-A1/A2` 等 |

### 11.3 备份与回滚（每批独立）

```bash
# 批前（示例：B1）
TS=$(date +%Y%m%d_%H%M%S)
mkdir -p backup/b414/$TS
cp -p src/ai_trader/log_redaction.py backup/b414/$TS/
cp -p src/ai_trader/config.py        backup/b414/$TS/
sha256sum src/ai_trader/log_redaction.py src/ai_trader/config.py \
  > backup/b414/$TS/BEFORE.sha256
```

| 项 | 要求 |
|---|---|
| 备份位置 | `backup/b414/<批次>_<时间戳>/`（与仓库同盘，不进 git） |
| 记录 | `BEFORE.sha256` / `AFTER.sha256` / 测试输出 / 探针 JSON |
| 回滚命令 | `cp -p backup/b414/<批次>_<ts>/*.py src/ai_trader/` + 重跑该批验收 |
| 回滚验收 | 该批 FAIL 项回到基线、其余批次 PASS 项不变 |
| 禁止 | 用 `git checkout` / `reset --hard`（仓库无 `.git`，也不允许引入） |

### 11.4 每批的停止条件（任一命中即停，不再前进）

1. 本批任何**保持**类探针从 PASS 变 FAIL；
2. 任何证据文件里出现真实凭据或完整 PEM（只允许 `len/sha8` 摘要）；
3. `redaction_health()["healthy"] == False` 且原因未解释；
4. 任一类性能基准 < 60%；
5. `src/` 之外的文件被改动（`config/config.yaml`、`.env`、`audit_evidence/`）；
6. 发现需要修改 `tests/` 既有断言才能通过（除本文明确列出的 2 改写 + 1 反转）；
7. 需要降低门槛、放宽 `MAX_*`、或关闭某一层防护。

### 11.5 全量回归（B5）

```bash
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 \
  .venv/Scripts/python.exe -B -m pytest tests/ -q -p no:cacheprovider -o addopts=""
```

- 全量通过 + 112 条探针复跑（预期 112/112，含 1 条反转、2 条改写后的新断言）；
- 独立审计（第三方视角，不允许由实施者自审）；
- 生成 `audit_evidence/us_p1_b41_4_*`：探针结果、manifest、before/after 指纹。

---

## 12. 风险分级

| 级别 | 风险 | 触发条件 | 处置 |
|---|---|---|---|
| **P1** | 缓存命中返回明文 | 实现时把 `return ent[1]` 写成 `return text` | 代码评审门禁 + `F203-A1` 必须能失败 |
| **P1** | 非敏感 ident 消费 value | `pos = sep_end` 写错 | `KV-02/03/04/S07` 变异测试 |
| **P1** | 锚点缺失但 `healthy=True` | `_hook_state` 未纳入 `anchor_missing` | `F202-04/09` |
| **P1** | 合法 PEM 被判 `pem_incomplete` | 校验前未归一化 | `PEM-01/02` |
| **P1** | 指纹与写入值不同源 | 先 strip 后算指纹 / 先算后改 | `FP-01/02` |
| **P2** | 转义投影漏多层转义 | 3 层以上 `json.dumps` | 记录为残余，`JSON-03` 只保证 4 层解开后正确 |
| **P2** | `_needs_scan` 误判为"无需扫描" | 结构信号覆盖不全 | 词表只作肯定信号；`F203-19` 零误伤 |
| **P2** | 弱引用回调与迭代竞争 | 热路径调用 `len()`/`items()` | 代码规范 + 注释；`F203-20` 并发压测 |
| **P3** | 直接 `Formatter.format` 绕过 | 不经 `Handler.format` | 既有边界，文档披露 |
| **P3** | `Handler` 同时重写 `handle`+`format` | 自定义 handler | 既有边界 |
| **P3** | Unicode 全角/零宽键绕过 | 攻击者控制日志文本 | 不做 NFKC（破坏位置映射），登记兜底 |
| **P3** | 锚点模块自身被 reload | 运维手动 reload | 等价冷启动 + 脏钩子 → fail-closed |

---

## 13. 尚未解决的问题

| # | 问题 | 影响 | 拟处置 |
|---|---|---|---|
| 1 | base64 合法但整行截断的 PEM，结构+Base64 抓不住 | 半截凭据进环境 | 靠 SDK 级 `pem_unparsable` 兜住；加密私钥例外 |
| 2 | 加密私钥（`BEGIN ENCRYPTED PRIVATE KEY`）跳过 SDK 检查 | 截断的加密私钥无法被 SDK 发现 | 记录为已知缺口；运行时由券商 SDK 报错 |
| 3 | 多层转义（≥3 层 `json.dumps`）的已登记凭据 | 极窄场景仍可能泄露 | 转义投影只覆盖 1–2 层；`JSON-03` 固定 4 层"解开后"正确 |
| 4 | Unicode 全角 / 零宽字符键 | 规则不识别 | P3 接受，登记字面量兜底 |
| 5 | 直接 `Formatter().format(record)` | 不经 `Handler.format`，L3 不生效 | 既有边界，不得宣称"日志绝对安全" |
| 6 | `Handler` 子类同时重写 `handle` 与 `format` | L2/L3 均绕过 | 既有边界（`test_t07`） |
| 7 | 三级降级全失败时 handler 仍可能输出 | `_UNSECURED_HANDLERS` | 仅能靠 `healthy=False` 告警，不能物理阻止 |
| 8 | `_PRESTINE` / `_FORMAT_CACHE` 仍是可写模块全局 | 有 import 权限者可篡改 | 与 `uninstall_hooks` 同级，属既有边界 |
| 9 | 锚点模块被显式 reload | 锚点清空 | 视为冷启动 + 脏钩子 → fail-closed |
| 10 | `_QUICK_PREFIX` 之外的长文本不用词表 | 词表覆盖率下降 | 结构信号仍覆盖；词表从不是否定判据 |
| 11 | 缓存摘要碰撞（16 字节） | 理论上命中错误条目 | 概率 2⁻¹²⁸；且命中只返回"另一条已脱敏文本"，不产生明文 |
| 12 | 强引用 `record` 的隐患 | 无 | 弱引用已消除，`gc.collect()` 后 size 归零（实测） |

---

## 14. 结束声明

- 本阶段**只读规划**：未修改任何源码、测试、配置、数据与既有报告；
- 未启动 Batch 4.2、未发布、未访问 VPS、未连接真实交易 API、未开启实盘；
- 未读取任何真实凭据；全部 PEM 实验使用运行时生成的假密钥；
- 本文与 `US_P1_BATCH4_1_4_FINAL_REVIEW.md` 冲突时，以本文为准（本文是修订后的实施合同），但**不得删除或覆盖**该审查报告。
