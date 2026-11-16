# US-P1 Batch 4.1.4 — 实施前最终审查（Final Review）

| 项 | 值 |
|---|---|
| 批次 | US-P1 Batch 4.1.4（实施前最终审查） |
| 审查对象 | `US_P1_BATCH4_1_4_REMEDIATION_PLAN.md`（835 行，mtime 14:44:25）及四项修复设计 |
| 审查性质 | **只读**。未修改任何源码 / 测试 / 配置；未改数据库与环境变量；未连接券商；未访问 VPS |
| 解释器 | `C:\FosunAI-Trading\.venv\Scripts\python.exe`（3.13.14，win32） |
| 审查时间 | 2026-11-10 |
| 结论摘要 | **不建议整体批准**。四项修复中 3 项可按"补充条件"批准，**F2-03 的缓存设计需重写后再审**（见 §3.3、§9） |

> 本文件是 4.1.4 的唯一交付物。4.1.3 的审计报告与全部证据保持原样，未覆盖、未删除、未清理。

---

## 1. 现场完整性核验

### 1.1 manifest 逐项校验

`audit_evidence/us_p1_b41_3_manifest.sha256` 共 30 项，逐项重算：

```
OK_COUNT=30        BAD=0
```

结论：4.1.3 的全部证据文件（审计报告、审计 JSON、探针结果 JSON、8 个探针脚本、探针公共库）**内容未变**。

### 1.2 关键源码指纹

| 文件 | 当前 SHA256 | 与 4.1.3 审计时 |
|---|---|---|
| `src/ai_trader/log_redaction.py` | `720b4f33ac98f7f09a95fddc1ca5244f3f17b577f77f38aa24825874f7556b4a` | **逐字节相同** |
| `src/ai_trader/config.py` | `ddb8144c37b1d23806c1a8bc44753eade1f64f5b1927fdfdf1c7fd4ba0efe8e0` | **逐字节相同** |

### 1.3 未报告修改排查

- `src/`、`tests/`、`config/` 自 14:00 起**零文件变更**（`find -newermt` 逐项核对）。
- 唯一 mtime 变化是 `US_P1_BATCH4_1_3_INDEPENDENT_AUDIT.md`（14:22:42），系 4.1.3 阶段 §4.2/§5 两次编辑所致，**内容仍在 manifest 校验范围内且一致**。
- 仓库**无 `.git`**（`git status` 不可用），因此"工作区未被 reset / checkout / clean"以文件指纹与 manifest 双重证明，未执行任何版本控制操作。

### 1.4 本阶段自身的只读约束执行情况

- 全部探针、原型、生成脚本位于 `%TEMP%\us_p1_b414_review\`，**不在仓库内**，不参与 manifest。
- 只读 pytest 口径未变：`env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 <python> -B -m pytest … -q -p no:cacheprovider -o addopts=""`。
- 未写入 `os.environ`（唯一涉及环境的探针在独立子进程内运行，进程结束即消失）。
- 未读取任何真实私钥；PEM 相关实验全部使用**运行时生成的 2048 位假密钥**。

---

## 2. 四项修复的函数级修改边界

### 2.1 F2-02（信任锚）

| 位置 | 现有代码 | 改动 | 边界 |
|---|---|---|---|
| `log_redaction.py:567` | `_PRESTINE: Dict[str, Any] = {}` | 改为 `_TRUSTED_HOOKS`（对象身份） | 只改语义，不改名可保留别名 |
| `log_redaction.py:571-576` | `_HOOK_TARGETS` 三元组 | 不变 | — |
| `log_redaction.py:578-582` | `_is_stdlib_hook` 只看 `__module__` | **仅在 `_capture_pristine()` 内调用**，结果固化 | 不得在运行期判据中使用 |
| `log_redaction.py:584-595` | `_capture_pristine()` | 增加"已捕获则不再覆盖"的幂等守卫 | **新增**（见 §3.2 缺陷 A） |
| `log_redaction.py:798-803` | `_PRESTINE[key] = current` | **删除该赋值** | 与 `_wrap_method` 同一 PR |
| `log_redaction.py:612`、`646`、`768`、`821`、`847`、`866` | 各自调用 `_is_stdlib_hook(...)` | 改为 `_is_trusted_original(fn, key)` | 6 处调用点需全部替换，漏一处即失效 |

**不改**：`uninstall_hooks`（969）、`redaction_health`（901）的既有字段、`_HOOK_TARGETS` 内容。

### 2.2 F2-04（脱敏扫描器）

| 位置 | 现有代码 | 改动 | 边界 |
|---|---|---|---|
| `log_redaction.py:171` | `_VALUE` | **删除**（不再由正则取值） | 保留常量名会给"仍在用"的错觉，建议直接删 |
| `log_redaction.py:173-175` | `_KV_RE` | 降级为 `_KV_KEY`（只定位 ident+sep） | 外部若有引用需保留别名 |
| `log_redaction.py:179-184` | `_JSON_RE` | 值分支改为"定位 + 扫描" | 见 §3.3 关于引号层数的约束 |
| `log_redaction.py:191-197` | `_kv_sub` | 删除打码职责 | 测试可能直接调用，需同步 |
| `log_redaction.py:199-206` | `_json_sub` | 保留（JSON 重建形态依赖 `pre`/`sep`） | — |
| `log_redaction.py:208-213` | `_url_sub` | **不变**，但 `_URL_RE` 必须前置到扫描器之前 | 否则 URL 形态被改写 |
| `log_redaction.py:221-229` | `_RULES` 元组 | 去掉 `_KV_RE` 项，新增 `_redact_assignments()` 调用点 | 顺序：PEM → AUTH → BEARER → URL → assignments → 登记字面量 |
| `log_redaction.py:285-324` | `redact_text` | 在规则 sub 之后、`_secret_snapshot()` 之前插入 `_redact_assignments(out)` | 登记字面量仍是最后兜底 |
| 新增 | `_scan_value` / `_mask_span` / `_redact_assignments` | 3 个私有函数 | 必须线性、必须有 `MAX_SCAN_CHARS` 上限 |

**不改**：`MAX_TEXT_CHARS`（80）、`MAX_RAW_TEXT_CHARS`（82）、`MAX_VALUE_CHARS`（96）语义、`redact_exception`（327）、`redact_obj`（357）。

### 2.3 F2-03（脱敏标记与 registry）

| 位置 | 现有代码 | 改动 | 边界 |
|---|---|---|---|
| `log_redaction.py:455` | `_REDACTED_FLAG = "_aitrader_redacted"` | **删除** | `grep -r _REDACTED_FLAG` 必须为 0 |
| `log_redaction.py:497-499` | L3 读 flag 跳过 | **删除** | — |
| `log_redaction.py:505` | L1 写 flag | **删除** | — |
| `log_redaction.py:458-508` | `_sanitize_record` extra 遍历中的跳过分支 | **删除** | 少一个分支，需回归 extra 用例（X01–X08） |
| `log_redaction.py:866-883` | `install_format_guard` 的 `_format` | 改为"文本摘要校验" | **设计需重写**（见 §3.3） |
| 新增 | registry 容器 | 建议 `WeakKeyDictionary[LogRecord, tuple[str, str]]` | 与计划 §4.2.2 的决策相反 |

**不改**：`_secret_snapshot`（274）、`register_secret`（237）的 `MIN_SECRET_LEN=8` / `MAX_SECRETS=64`、`RedactionFilter`（528）的过滤语义。

### 2.4 B1-05（dotenv 与 PEM）

| 位置 | 现有代码 | 改动 | 边界 |
|---|---|---|---|
| `config.py:508-540` | `load_dotenv` 逐行 `partition("=")` | 换成显式解析器（跨行状态机） | 签名、返回值语义不变 |
| `config.py:535` | `_DOTENV_FP[name] = _dotenv_fingerprint(value)` | 必须用**最终多行值** | 最危险回归点（AUD2-B1-01） |
| `config.py:414-444` | `_dotenv_fingerprint` | **不变** | 只改调用时机 |
| `config.py:446-497` | `dotenv_origin` | **不变** | 靠指纹匹配，故 535 必须同源 |
| `config.py:402-413` | `dotenv_source_diagnostics` | 增加 `parse_errors` 计数 | 只加计数，不加内容 |
| 新增 | `_parse_dotenv_text` / `dotenv_parse_errors` / `_validate_pem` | 3 个函数 | 错误结构只含键名/原因/行号 |

**不改**：`_DotenvValue`（250）、`internal_env_write`（333）、`_install_environ_tracker`（350）、`setdefault` 语义。

---

## 3. 四项方案的风险与可行性

### 3.1 F2-04：方案方向正确，但"吞并"比 4.1.3 描述的更严重

**实测证据（当前源码，非推理）**：

| 输入 | 输出 | 结论 |
|---|---|---|
| `RuntimeError: api_key=secret` | `RuntimeError: api_key=secret` | **完全未打码** |
| `Error: token=abc123` | 原样 | 完全未打码 |
| `WARNING: password=hunter2` | 原样 | 完全未打码 |
| `Field: secret=xyz` | 原样 | 完全未打码 |

机制已定位到 match span 级：

```
input  = "RuntimeError: api_key=secret"
_KV_RE match span = (0, 28)
  ident = 'RuntimeError'
  value = 'api_key=secret'
```

首个 match 的 `ident` 非敏感 → `_kv_sub` 原样返回 → `re.sub` 游标跳到 match 末尾 (28) → 内层 `api_key=secret` **再也不会被检查**。

这不是"部分泄露"，是**整段绕过**；而且命中的是 `Type: message` 这种最常见的日志形态。`redact_exception`（走真实 `exc_info` 三元组）尾行实测同样泄露：

```
redact_exception(exc_info).splitlines()[-1]
  -> 'RuntimeError: api_key=SECRET-abcdefgh'
```

`redact_obj(RuntimeError("api_key=secret"))` → `'RuntimeError: api_key=secret'`，同一路径同样泄露。

**已登记仍泄露的路径已用合法假凭据确证**（排除"未登记"这一解释）：

```
MIN_SECRET_LEN = 8
register_secret('abc"defgh')  -> True      # 9 字符，合法登记
redact_text('log abc"defgh end')          -> 'log *** end'        # 登记字面量生效
redact_text('{"api_key":"abc\\"defgh"}')  -> 原样（含明文）        # 转义形态击穿 replace
```

结论：F2-04-02 是**唯一"已登记也泄露"**的路径，4.1.3 的判断成立。

**结构尾巴被吞**（F2-04-05）实测：

```
in : 'raise RuntimeError(f"api_key=secret")'
out: 'raise RuntimeError(f"api_key: ***'      # 丢失 ")
```

**计划的修法在原理上成立**，且我已验证其中两条关键判据：

1. `_mask_span` 用 `raw.rstrip("\"')]}>;,")` 把结构尾巴留在外面 —— 能同时满足 `api_key=a"b`（全抹）与 `api_key=secret")`（保留 `")`）。
2. `_scan_value` 的闭合判据"反斜杠个数与开头 token 完全相等" —— 对 `{"api_key":"abc\"defgh"}` 能正确闭合到最后一个 `"`（转义引号的反斜杠数一定比开头多）。

**但计划缺一条必须写明的规则**：非敏感 ident 命中时，游标只能前进到 `sep.end()`，**不能吞掉 value**。计划的 §4.1.2 只写了"定位"，没写这一步；而它恰恰是 F2-04-01 的唯一解。计划 §6.1 的 `KV-02`/`KV-04` 暗含了该行为，但设计正文缺失，实施时极易写成"整段回写并跳到 match 末尾"（我的原型第一版就是这样，`RuntimeError:` 仍会漏）。

**Unicode 绕过（残余风险，非回归）**：

```
'ａｐｉ＿ｋｅｙ=secret'   -> 原样（现行与原型均不打码）
'api\u200b_key=secret'  -> 原样（同上）
```

现状同样不打码，**修复不会使其变差**。不建议在扫描器里做 NFKC 归一化：归一化会改变字符偏移，使 `_mask_span` 的位置映射全部错位。处置：登记字面量兜底 + 在文档中列为已知边界（P3）。

### 3.2 F2-02：设计覆盖 5/6 项，**"模块重载"仍是 fail-open**

用户点名的 6 个场景逐项判定：

| 场景 | 计划能否覆盖 | 依据 |
|---|---|---|
| 原始函数身份被替换 | ✅ | `fn is _TRUSTED_HOOKS[key]` 取代 `__module__` |
| 伪造 `__module__="logging"` | ✅ | 身份比较不读 `__module__` |
| monkey patch 后恢复 | ✅ | 锚点不被 `_wrap_method` 覆盖 |
| **模块重载** | ❌ **未覆盖** | 见下方实测 |
| 多线程初始化竞争 | ✅ | 导入期由 import lock 串行；`_INSTALL_LOCK` 已存在 |
| 锚点不可被运行期覆盖 | ⚠️ 部分 | `_TRUSTED_HOOKS` 仍是可写模块全局，有 import 权限者可改（与 `uninstall_hooks` 同级，属既有边界） |

**模块重载实测（当前源码）**：

```
PRESTINE before reload: {'Handler.handle': True, 'Logger.handle': True, 'Handler.format': True}
PRESTINE after  reload: {'Handler.handle': False, 'Logger.handle': True, 'Handler.format': False}

ensure_hooks() after reload:
  healthy = True
  repaired = {'handler': False, 'logger': True, 'format': False}
```

即：重载后 `Handler.handle` 与 `Handler.format` 的锚点变成 `None`，而 `ensure_hooks()` 仍报 `healthy=True`、且认为 handler/format 无需修复。

原因是 `importlib.reload` 在**同一模块字典**内重新执行模块体：`_PRESTINE` 被重建，`_capture_pristine()` 面对的是**已被包装过的**方法；包装器 `__module__` 为 `ai_trader.log_redaction`（实测 `__qualname__ = install_global_hook.<locals>._factory.<locals>._handle`），`_looks_like_stdlib` 判否 → 记 `None`。

重载后脱敏功能**实测仍正常**（旧闭包还在，输出 `api_key: ***`），但新模块的信任锚为空：此后对**新** handler 调 `install_on_handler` 会把现有钩子当 foreign，走降级/禁用分支（`degraded` 路径）。这属于"锚点丢失但自报健康"，正是用户要求重点检查的"初始化时序"缺陷。

计划 §4.3.1 的 `_capture_pristine()` 仍在模块内、仍会被 reload 重跑，**未改变该失效模式**。

**建议补救（三选一，按优先级）**：

1. 锚点外置：新建极小模块 `ai_trader/_redaction_anchor.py`（只含 3 个槽位），`log_redaction` 重载时从 `sys.modules` 读同一对象 → 身份跨 reload 稳定。
2. 包装器自描述：`_wrap_method` 给包装器打 `__redaction_original__`，`_capture_pristine()` 沿该属性回解到真原始函数（需在写入处加不可伪造性说明）。
3. 幂等守卫：`if _TRUSTED_HOOKS and not force: return` —— 只能防"重复捕获"，**防不住 reload 重建字典**，单独使用无效。

**另有一处未讨论的时序窗口**：`_capture_pristine()` 在导入期执行，若第三方在本模块导入**之前**已替换钩子并伪造 `__module__`，捕获到的是伪造品。计划的 `_looks_like_stdlib` 初始筛选对此无效。处置：接受为残余风险（需在 `logging` 被污染前完成导入，属部署约束），或改用"从 `logging` 源码重新 exec 一份参照实现"（成本高，不建议）。

### 3.3 F2-03：**缓存设计存在方向性错误，必须重写**

计划 §4.2.1 的代码：

```python
digest = _digest(text)                                  # 输入文本摘要
...
if hit is not None and hit[0] is record and hit[1] == digest:
    return text                                         # ← 返回原文
...
_FORMATTED[key] = (record, _digest(out))                # 存的是输出摘要
```

两种读法都不可接受：

- **按字面读**：`hit[1]` 是 `digest(out)`，与 `digest(text)` 比较 → 只要发生过打码就**永不相等** → 缓存永不命中 → "多 handler 扇出只脱敏一次"的收益为 0（只剩 `_QUICK_HIT` 已覆盖的无操作场景）。缓存是死的。
- **若把比较改成"存 `digest(text)`"**：第二次命中时 `return text` 返回的是**未脱敏的原文** → 第二个 handler 直接拿到明文。这是**引入新的泄露**，比现状更糟。

正确形态应是"输入摘要 → 输出文本"的映射，命中时返回**输出**：

```python
key = record                      # 对象本身作键（见下）
hit = _CACHE.get(key)
if hit is not None and hit[0] == digest(text):
    return hit[1]                 # 返回已脱敏文本
out = redact_text(text, truncate=False)
_CACHE[key] = (digest(text), out)
return out
```

**计划 §4.2.2 拒绝 weakref 的理由不成立**。原文："不引入 `weakref`（无法阻止 `id()` 复用，且 LogRecord 生命周期短，强引用 + 上限更稳）"。实测：

```
weakref.ref(record)() is record      -> True
WeakKeyDictionary: wk[record]='d1'   -> size 1, get(record)='d1'
del record; gc.collect()             -> size 0
copy.copy(record) is record          -> False   # 新对象 → 未命中 → 重脱敏 ✓
copy.deepcopy(record) is record      -> False   # 同上 ✓
```

用**对象本身**作键时，`id()` 复用根本不会发生：条目随对象死亡被自动移除，新对象即使复用同一 `id` 也查不到旧条目。弱引用方案同时消解了用户点名的①`id()` 重用、②强引用释放、③无界增长三项，且不需要 512 上限，也不需要 `hit[0] is record` 这层防御。

**其余各项实测**：

| 用户检查项 | 结论 |
|---|---|
| ④多线程同时处理同一 LogRecord | 16 线程 × 同一 record：`faults=[]`、`leaks=0`（现状已安全） |
| ⑤多 Handler 共用 Record | 现状安全（F2-03-07 PASS），但扇出时每条重复全量正则 |
| ⑥Filter 脱敏后追加 | **现状泄露**（F2-03-13 FAIL）→ 由摘要校验覆盖 |
| ⑦Formatter 脱敏后追加 | **现状泄露**（F2-03-14 FAIL）→ 同上 |
| ⑧`copy` / `deepcopy` | 新对象 → 未命中 → 重脱敏 ✓（计划判断正确） |
| ⑨摘要不一致是否重新脱敏 | 计划方向正确，但前提是命中时返回 `out`（见上） |
| ⑩registry 异常降级 | **计划未写**。必须 fail-closed：异常 → 清空缓存 → 走 `redact_text`；不得 `return text` |
| 性能不得换安全 | `_QUICK_HIT` 是纯优化，未命中即跳过 → 若 `_QUICK_HIT` 词表漏词，等于放弃 L3 兜底。**这是新的单点风险**，词表必须由 `_SENSITIVE_KEYS` 派生而非手写 |

`_QUICK_HIT` 与 `_secret_prefix_hit` 的设计还有一个隐患：`_secret_prefix_hit` 取登记值前 8 字符做 `in` 判断，而 `MIN_SECRET_LEN=8` 意味着前 8 字符就是整个值——等价于把凭据前缀放进热点路径，且只对长度恰为 8 的值有效；对 40 字符的 key 取前 8 字符误判率极高。建议改为"任一字面量 `in` 文本"（64 项 × `str.find`，实测成本可接受），或直接依赖 `_QUICK_HIT`。

### 3.4 B1-05：11 项中 8 项覆盖，**3 项有实质缺口**

| # | 检查项 | 计划 | 缺口 |
|---|---|---|---|
| ① | 多行引号字段 | ✅ 跨行状态机 | — |
| ② | CRLF / LF | ⚠️ | 只提 `utf-8-sig` 读取，**未要求值内 `\r\n → \n` 归一**，见下 |
| ③ | UTF-8 BOM | ✅ `utf-8-sig` | — |
| ④ | 转义换行 | ⚠️ | 与 `B1-05-05` 冲突，见下 |
| ⑤ | BEGIN/END 同名匹配 | ⚠️ | 正则对尾部换行敏感，见下 |
| ⑥ | Base64 body 合法性 | ❌ **未做** | 计划只查 `len(body.strip()) < 64`，不查字符集/长度 |
| ⑦ | 缺 END / 错 END / 空 body | ✅ | 但"空 body"依赖 ⑥ 的补齐 |
| ⑧ | 多个 PEM 连续 | ❌ **未提** | `.env` 内两个 PEM 字段、或一个值含两段 PEM |
| ⑨ | 单行 `.env` 兼容 | ✅ | 由 DOT-01/02/11/12/19 固定 |
| ⑩ | 指纹用最终值 | ✅ | 计划 §4.4.3 已识别为最危险回归点 |
| ⑪ | SDK 能否解析 | ❌ **未提** | 建议作为验收测试 |

**⑤/② 的实测反例（这是我发现的计划级缺陷）**：

用运行时生成的 2048 位假密钥（1704 字符、28 行、尾部带 `\n`）套用计划 §4.4.1 的校验式：

```
RX = re.compile(r"-----BEGIN ([A-Z0-9 ]+)-----(.*)-----END \1-----", re.S)

原样（尾部带换行）   fullmatch = False     ← 合法 PEM 被判 pem_incomplete
strip 后             fullmatch = True
rstrip 换行          fullmatch = True
CRLF 原样            fullmatch = False     ← 合法 CRLF 文件同样被判失败
CRLF + strip         fullmatch = True      ← 但 body 内残留 \r
```

后果不是"少打码"，而是**把合法凭据判成畸形 → 不写入 → `CredentialIncomplete` → 拒绝启动**。方向虽同为 fail-closed，但对象错了：它拒绝的是好配置。必须在校验前 `value.strip()`（或 `rstrip("\r\n")`）并显式 `\r\n → \n` 归一；CRLF 若不归一会让 `b64decode(validate=True)` 直接失败（`\r` 非 base64 字符）。

**⑥/⑪ 的三类检查必须分开（实测边界）**：

| 检查 | 能抓 | 抓不到 |
|---|---|---|
| 格式（BEGIN/END 同名） | 缺 END、END 名不匹配、结构错位 | body 截断、非法字符 |
| Base64（`b64decode(validate=True)`） | body 截断 1 字符即 `Error`、非法字符 | 结构问题、密钥语义 |
| SDK（`load_pem_private_key`） | 仅首行、字面 `\n`、缺 END、END 名不匹配、body 截断 | **"换行被替换成空格"仍 PARSE_OK** |

最后一行是关键：`cryptography 50.0.2` 对 `-----BEGIN…----- AAAA BBBB -----END…-----`（空格代替换行）**解析成功**。所以 **SDK 检查不能替代格式检查**，三者必须都做。依赖可用性已确认（`cryptography 50.0.2` 在 venv 内，`paramiko`/`ccxt`/`openai`/`jwt` 均未安装）。

**④ 与 `B1-05-05` 的冲突（必须实施前定案）**：

`B1-05-05` 的输入是"转义写法 `\n`（单行）"，当前 `env_len=582`，期望 `573`。计划 §4.4.2 决策为"`\n` 解码**只在双引号内**，无引号值保持原样（避免把 `C:\new` 这类路径改写）"。若该用例**不带引号**，则按计划永远解不出 573，该条无法翻绿。

处置建议：对键名匹配 `(?i)(private|public)_key$|_pem$|_key$` 的字段，**无引号时也解码 `\n`**。理由是 PEM 正文不可能包含字面 `\n` 两字符，而 `C:\new` 这类路径不会出现在 `*_KEY` 字段里。该例外必须显式写进设计，不能留给实现者猜。

**⑧ 多个 PEM 连续的缺口**：计划的 `re.fullmatch` 只允许**一段** PEM 撑满整个值。若一个字段含两段（证书链、或 `CERT` + `KEY` 合并写入），`fullmatch` 失败 → 判 `pem_incomplete` → 与 ⑤ 同类的误杀。建议改为 `re.finditer` 逐段校验，段数与 `BEGIN` 计数一致即通过。

**fail-closed 的行为变化（需在测试中固定）**：`B1-05-09`（缺 END）与 `B1-05-14`（未闭合引号）当前**静默写入半截值**（27 / 64 字符）；修复后变为**不写入 + `parse_errors` 计数**。这是期望行为，但会让 `CredentialIncomplete` 提前触发，必须在测试中断言失败信息，避免被误判为回归。

---

## 4. 112 条旧探针映射表

口径说明：

- **保持**：断言仍成立，修复后必须继续 PASS（回归护栏）。
- **翻绿**：断言本身正确，当前 FAIL 是因为缺陷存在；修复后自然 PASS，**不需要改预期**。
- **改写**：预期文本本身要改（把"不断言"改成明确断言，或把"静默接受"改成"fail-closed"）。
- **反转**：当前把"允许泄露"写成期望并标记 PASS；修复后必须翻转为"不含明文"。
- **边界保留**：断言编码的是不可修复的边界（无键名裸值、直接 Formatter 路径），保持原样但需在图注中说明理由。
- 统计：保持 70、翻绿 30、翻绿* 2、改写 2、反转 1、边界保留 7（合计 112）。
- `翻绿*` 表示"缺陷修复后自然通过，但该条的断言范围或前置条件需在实施 PR 中确认"。

| # | 原编号 | 4.1.3 现状 | 原预期 / 实测 | 修复后预期 | 处理 | 关联新测试 |
|---|---|---|---|---|---|---|
| 1 | `B1-05-01` | PASS | 不断言内容，仅记录行为 / `env_len=27` | 单行压平值仍为 27 字符（逐字节兼容护栏） | 保持 | DOT-01 |
| 2 | `B1-05-02` | PASS | 不断言内容，仅记录行为 / `env_len=27` | 单行双引号仍为 27 字符 | 保持 | DOT-02 |
| 3 | `B1-05-03` | **FAIL** | AUD3B105_PEM == 完整 PEM（573 字符） / `env_len=27` | 573 字符完整 PEM | 翻绿 | DOT-03 |
| 4 | `B1-05-04` | **FAIL** | AUD3B105_PEM == 完整 PEM（573 字符） / `env_len=27` | 573 字符完整 PEM | 翻绿 | DOT-04 |
| 5 | `B1-05-05` | **FAIL** | AUD3B105_PEM == 完整 PEM（573 字符） / `env_len=582` | 573 字符；**须先确认该用例是否带引号**——无引号则与 §4.4.2「\n 只在双引号内解码」冲突 | 翻绿* | DOT-05 |
| 6 | `B1-05-06` | **FAIL** | AUD3B105_PEM == 完整 PEM（573 字符） / `env_len=27` | 573 字符且值内无 \r（CRLF 归一） | 翻绿 | DOT-06 |
| 7 | `B1-05-07` | **FAIL** | AUD3B105_PEM == 完整 PEM（573 字符） / `env_len=27` | 573 字符（文件末尾无换行） | 翻绿 | DOT-07 |
| 8 | `B1-05-08` | **FAIL** | AUD3B105_PEM == 完整 PEM（573 字符） / `env_len=27` | 573 字符，其余 3 键不受影响 | 翻绿 | DOT-08 |
| 9 | `B1-05-09` | PASS | 不断言内容，仅记录行为 / `env_len=27` | 不写入该键 + parse_errors=1(pem_incomplete)（当前静默写入 27 字符半截值） | 改写 | DOT-09 |
| 10 | `B1-05-10` | **FAIL** | AUD3B105_PEM == 完整 PEM（573 字符） / `env_len=27` | 573 字符（空行/注释在引号内不截断） | 翻绿 | DOT-10 |
| 11 | `B1-05-11` | PASS | AUD3B105_PEM == 完整 PEM（8 字符） / `env_len=8` | abc==def 仍为 8 字符（= 不二次切分） | 保持 | DOT-11 |
| 12 | `B1-05-12` | PASS | AUD3B105_PEM == 完整 PEM（1 字符） / `env_len=1` | 无等号行仍跳过，值仍为 1 字符 | 保持 | DOT-12 |
| 13 | `B1-05-13` | **FAIL** | AUD3B105_PEM == 完整 PEM（519 字符） / `env_len=64` | 519 字符完整 PEM（单引号不去转义） | 翻绿 | DOT-13 |
| 14 | `B1-05-14` | PASS | 不断言内容，仅记录行为 / `env_len=64` | 不写入该键 + unterminated_quote（当前静默写入 64 字符） | 改写 | DOT-14 |
| 15 | `B1-05-15` | PASS | 不断言内容，仅记录行为 / `env_len=65` | 前后空格仍被剥离，值 65 字符不变 | 保持 | DOT-15 |
| 16 | `B1-05-16` | PASS | 不断言内容，仅记录行为 / `env_len=None` | 空文件返回 False、不抛异常 | 保持 | DOT-16 |
| 17 | `B1-05-17` | PASS | 不断言内容，仅记录行为 / `env_len=None` | 仅注释文件返回 False | 保持 | DOT-17 |
| 18 | `B1-05-18` | **FAIL** | AUD3B105_PEM == 完整 PEM（1 字符） / `env_len=None` | 键名为 AUD3B105_PEM（无 \ufeff），值 1 字符 | 翻绿 | DOT-18 |
| 19 | `B1-05-19` | PASS | 不断言内容，仅记录行为 / `env_len=7` | 值含 # 仍完整保留 7 字符（不得当注释截断） | 保持 | DOT-19 |
| 20 | `B1-05-20` | PASS | AUD3B105_PEM == 完整 PEM（12 字符） / `env_len=12` | 已存在进程值不被覆盖，仍 12 字符 | 保持 | DOT-20 |
| 21 | `RG-F2-01-A` | PASS | install=False, unsecured>=1, blocked=0,  / `{"install_return": false, "mode": "unsecured…` | install=False、unsecured>=1、blocked=0 | 保持 | RG-01 |
| 22 | `RG-F2-01-B` | PASS | install=False, unsecured>=1, blocked=0 / `{"install_return": false, "unsecured": 1, "b…` | 同上 | 保持 | RG-01 |
| 23 | `RG-F2-01-C` | PASS | install=True, blocked>=1, unsecured=0 / `{"install_return": true, "blocked": 1, "unse…` | install=True、blocked>=1、unsecured=0 | 保持 | RG-02 |
| 24 | `RG-F2-02-A` | PASS | 自愈：trusted=True, 脱敏恢复, foreign_hooks 记录 / `{"logger_hook_trusted": true, "logger_hook":…` | 自愈且脱敏恢复，_PRESTINE 不被污染 | 保持 | F202-01 |
| 25 | `RG-F2-02-B` | **FAIL** | 应识别为不可信（trusted=False） / `{"logger_hook_trusted": true, "healthy": tru…` | 伪造 __module__ 应 trusted=False；断言范围需确认（若仅断言 trusted 则无需改写，若断言 healthy 则需改为「自愈后 healthy=True」） | 翻绿* | F202-02 |
| 26 | `RG-F2-02-C` | PASS | 两次 repaired 全 False，healthy=True / `{"first": {"handler": false, "logger": false…` | 两次 repaired 全 False、healthy=True | 保持 | F202-03 |
| 27 | `RG-D1-A` | PASS | 1000→True, 1011→False, 无 code→按意图 / `{"results": {"code1000_frame": true, "code10…` | 1000→True、1011→False、无 code 按意图 | 保持 | RG-03 |
| 28 | `F2-03-01 extra 设标记 + 正常 logger 调用` | PASS | L1 仍脱敏，输出不含明文 / `api_key: ***` | L1 仍脱敏 | 保持 | F203-01 |
| 29 | `F2-03-02 直写标记 + Logger.handle 正常路径` | PASS | L2 仍脱敏 / `api_key: ***` | L2 仍脱敏 | 保持 | F203-02 |
| 30 | `F2-03-03 直驱 emit + 伪造标记` | **FAIL** | 若标记可信则 L3 跳过 → 泄露；期望不泄露 / `api_key=FAKE-F203-APIKEY-7c1d4a9b` | 直驱 emit + 伪造标记后仍打码（标记机制删除） | 翻绿 | F203-03 |
| 31 | `F2-03-04 直驱 emit 无标记（对照）` | PASS | L3 兜底脱敏 / `api_key: ***` | 无标记时 L3 兜底打码 | 保持 | F203-04 |
| 32 | `F2-03-05 直接 handler.format + 伪造标记` | **FAIL** | 不应泄露 / `api_key=FAKE-F203-APIKEY-7c1d4a9b` | 直接 handler.format 仍打码 | 翻绿 | F203-05 |
| 33 | `F2-03-06 脱敏后追加新秘密并沿用旧标记` | **FAIL** | 新秘密必须仍被打码 / `rotated api_key=FAKE-F203-NEWSECRET-2e5f8a1c` | 脱敏后追加的新秘密仍被打码（摘要不一致→重脱敏） | 翻绿 | F203-06 |
| 34 | `F2-03-07 跨 handler 传递同一记录` | PASS | 两路输出都不含明文 / `A='api_key: ***' B='api_key: ***'` | 跨 handler 两路均不含明文 | 保持 | F203-07 |
| 35 | `F2-03-08 自定义 LogRecord 伪造标记` | **FAIL** | 不应泄露 / `api_key=FAKE-F203-APIKEY-7c1d4a9b` | 自定义 LogRecord 伪造标记无效 | 翻绿 | F203-08 |
| 36 | `F2-03-09 同一记录连续脱敏两次` | PASS | 幂等、普通字段不被破坏 / `once='user=alice token: *** count=5' twice='…` | 幂等，user=/count= 不被破坏 | 保持 | F203-09 |
| 37 | `F2-03-10 8 线程并发脱敏同一记录` | PASS | 无异常、最终文本不含任何假秘密 / `faults=[] msg='api_key: *** session_id: ***'` | 8 线程无异常、无泄漏 | 保持 | F203-10 |
| 38 | `F2-03-11 直接用 Formatter 格式化（不经 Handler.format）` | PASS | 本项目未覆盖该路径（已知边界） / `api_key=FAKE-F203-APIKEY-7c1d4a9b` | 直接用 Formatter 不经 Handler.format 仍不覆盖（既有边界，本批不改变） | 边界保留 | F203-11 |
| 39 | `F2-03-12 copy 记录后仍带旧标记 + 新秘密` | **FAIL** | 新秘密必须被打码 / `retry api_key=FAKE-F203-NEWSECRET-2e5f8a1c` | copy 后新秘密仍被打码 | 翻绿 | F203-12 |
| 40 | `F2-03-13 后置 Filter 追加敏感内容（正常 logger 路径）` | **FAIL** | 最终文本里必须打码 / `request done enriched_api_key=FAKE-F203-NEWS…` | 后置 Filter 追加的敏感内容被打码 | 翻绿 | F203-13 |
| 41 | `F2-03-14 自定义 Formatter 追加敏感内容（正常 logger 路径）` | **FAIL** | 最终文本里必须打码 / `request done ctx_api_key=FAKE-F203-NEWSECRET…` | 自定义 Formatter 追加的敏感内容被打码 | 翻绿 | F203-14 |
| 42 | `F2-04-01` | PASS | 输出不含任何假秘密 / `handshake done with ***` | 已登记字面量仍被打码 | 保持 | REG-02 |
| 43 | `F2-04-02` | PASS | 允许泄露（设计边界） / `handshake done with FAKE-F204-TOKEN-3a7c9e1d` | 未登记、无键名的裸值无法识别（登记后才打码） | 边界保留 | REG-03 |
| 44 | `F2-04-03` | PASS | 输出不含任何假秘密 / `api_key: ***` | api_key: *** | 保持 | KV-05 |
| 45 | `F2-04-04` | PASS | 输出不含任何假秘密 / `payload={"data":{"sessionId":"***"}}` | 嵌套 dict 的 sessionId 被打码 | 保持 | OBJ-01 |
| 46 | `F2-04-05` | PASS | 输出不含任何假秘密 / `GET /v1/q?X-Session: ***` | URL 查询参数被打码 | 保持 | URL-01 |
| 47 | `F2-04-06` | **FAIL** | 输出不含任何假秘密 / `request failed Traceback (most recent call l…` | 多行 traceback 中的敏感值被打码 | 翻绿 | KV-01 |
| 48 | `F2-04-07` | PASS | 允许泄露（设计边界） / `request failed Traceback (most recent call l…` | 无键名裸值，登记后才打码 | 边界保留 | REG-03 |
| 49 | `F2-04-08` | PASS | 输出不含任何假秘密 / `context attached` | context attached 场景不误伤 | 保持 | D-01 |
| 50 | `F2-04-09` | **FAIL** | 允许泄露（设计边界） / `context attached` | 同 06（多行场景） | 翻绿 | KV-01 |
| 51 | `F2-04-10` | PASS | 允许泄露（设计边界） / `a=*** b=FAKE-F204-TOKEN2-5b8d2f4a` | a=*** b=<未登记裸值> 保持 | 边界保留 | REG-03 |
| 52 | `F2-04-11` | PASS | 输出不含任何假秘密 / `api_key: ***` | api_key: *** | 保持 | KV-05 |
| 53 | `F2-04-12` | PASS | 允许泄露（设计边界） / `value is FAKE-7x` | 无键名裸值保持 | 边界保留 | REG-03 |
| 54 | `F2-04-13` | **FAIL** | 输出不含任何假秘密 / `api_key: *** FAKE-F204-PART2` | 多行值续行不残留 PART2 | 翻绿 | KV-07 |
| 55 | `F2-04-14` | **FAIL** | 输出不含任何假秘密 / `payload={"api_key":"FAKE-F204-\"quoted\"-9f1…` | JSON 值含转义引号被打码 | 翻绿 | JSON-01 |
| 56 | `F2-04-15` | PASS | 输出不含任何假秘密 / `Authorization failed for Bearer ***` | Bearer 仍打码 | 保持 | AUTH-01 |
| 57 | `F2-04-16` | PASS | 输出不含任何假秘密 / `loaded pem: ***` | PEM 整块打码 | 保持 | PEM-01 |
| 58 | `F2-04-17` | PASS | 输出不含任何假秘密 / `request headers: Authorization: ***` | Authorization 头打码 | 保持 | AUTH-02 |
| 59 | `F2-04-18` | PASS | 输出不含任何假秘密 / `deep={'n': {'n': {'n': {'n': {'n': {'n': {'n…` | 深层结构不崩、不泄漏 | 保持 | OBJ-02 |
| 60 | `F2-04-19` | PASS | 输出不含任何假秘密 / `many=[{'i': 0}, {'i': 1}, {'i': 2}, {'i': 3}…` | 大列表不崩、不泄漏 | 保持 | OBJ-03 |
| 61 | `F2-04-20` | PASS | 输出不含任何假秘密 / `symbol=AAPL qty=100 price=195.32 side=buy` | D 类业务字段完全可读（不得误伤） | 保持 | D-02 |
| 62 | `F2-04-X01` | PASS | 输出不含假秘密 \| fmt=%(message)s \| v=%(auth_t / `ctx \| v=*** ` | extra 键 auth_token 打码且文本形态不变 | 保持 | EXT-01 |
| 63 | `F2-04-X02` | PASS | 输出不含假秘密 \| fmt=%(message)s \| v=%(apiKey / `ctx \| v=*** ` | extra 键 apiKey 打码 | 保持 | EXT-02 |
| 64 | `F2-04-X03` | PASS | 输出不含假秘密 \| fmt=%(message)s \| v=%(upstre / `ctx \| v=*** ` | extra 键 upstream_* 打码 | 保持 | EXT-03 |
| 65 | `F2-04-X04` | PASS | 允许泄露（设计边界） \| fmt=%(message)s \| v=%(blo / `ctx \| v=FAKE-F204X-TOKEN-6d1a8b3c ` | extra 键 blob 非敏感名，不打码 | 边界保留 | EXT-04 |
| 66 | `F2-04-X05` | PASS | 允许泄露（设计边界） \| fmt=%(message)s \| v=%(tok / `ctx \| v=FAKE-F204X-TOKEN-6d1a8b3c ` | extra 键名不含敏感词，不打码 | 边界保留 | EXT-05 |
| 67 | `F2-04-X06` | PASS | 输出不含假秘密 \| fmt=%(message)s \| v=%(payloa / `ctx \| v={'api_key': '***'} ` | extra 内嵌 dict 的 api_key 打码 | 保持 | EXT-06 |
| 68 | `F2-04-X07` | PASS | 输出不含假秘密 \| fmt=%(message)s \| v=%(materi / `ctx \| v=*** ` | material 键打码 | 保持 | EXT-07 |
| 69 | `F2-04-X08` | **FAIL** | 输出不含假秘密 \| fmt=%(message)s \| v=%(err)s / `ctx \| v=RuntimeError: api_key=FAKE-F204X-TOK…` | extra 中异常对象的 api_key= 被打码 | 翻绿 | KV-01 |
| 70 | `F2-04-S01` | **FAIL** | 输出不含假秘密 \| fmt=%(message)s / `RuntimeError: api_key=FAKE-F204X-TOKEN-6d1a8…` | RuntimeError: api_key=… 被打码（吞并修复） | 翻绿 | KV-02 |
| 71 | `F2-04-S02` | **FAIL** | 输出不含假秘密 \| fmt=%(message)s / `status: token=FAKE-F204X-TOKEN-6d1a8b3c ` | status: token=… 被打码 | 翻绿 | KV-02 |
| 72 | `F2-04-S03` | **FAIL** | 输出不含假秘密 \| fmt=%(message)s / `error: password=FAKE-F204X-TOKEN-6d1a8b3c ` | error: password=… 被打码 | 翻绿 | KV-02 |
| 73 | `F2-04-S04` | **FAIL** | 输出不含假秘密 \| fmt=%(message)s / `RuntimeError:  api_key=FAKE-F204X-TOKEN-6d1a…` | 双空格分隔仍被打码 | 翻绿 | KV-04 |
| 74 | `F2-04-S05` | PASS | 输出不含假秘密 \| fmt=%(message)s / `api_key: *** ` | api_key: *** | 保持 | KV-05 |
| 75 | `F2-04-S06` | PASS | 输出不含假秘密 \| fmt=%(message)s / `- api_key: *** ` | 列表项形态不误伤 | 保持 | D-03 |
| 76 | `F2-04-S07` | **FAIL** | 输出不含假秘密 \| fmt=%(message)s / `data=api_key=FAKE-F204X-TOKEN-6d1a8b3c ` | data=api_key=… 嵌套赋值被打码 | 翻绿 | KV-03 |
| 77 | `F2-04-S08` | PASS | 输出不含假秘密 \| fmt=%(message)s / `outer: middle: api_key: *** ` | 三层 outer: middle: 已能打码 | 保持 | KV-04 |
| 78 | `F2-04-S09` | **FAIL** | 输出不含假秘密 \| fmt=%(message)s / `request failed Traceback (most recent call l…` | traceback 尾行被打码 | 翻绿 | KV-01 |
| 79 | `F2-04-06R` | **FAIL** | 见 actual.full_text / `request failed Traceback (most recent call l…` | 多行 traceback 打码 | 翻绿 | KV-01 |
| 80 | `F2-04-07R` | **FAIL** | 见 actual.full_text / `request failed Traceback (most recent call l…` | 多行 traceback 打码（重复形态） | 翻绿 | KV-01 |
| 81 | `F2-04-09R` | PASS | 见 actual.full_text / `context attached ` | 未误伤 | 保持 | D-01 |
| 82 | `F2-04-09S` | PASS | 见 actual.full_text / `context attached ` | 未误伤 | 保持 | D-01 |
| 83 | `F2-04-13R` | **FAIL** | 见 actual.full_text / `api_key: *** FAKE-F204-PART2 ` | 续行 PART2 被打码 | 翻绿 | KV-07 |
| 84 | `F2-04-13S` | PASS | 见 actual.full_text / `payload={"api_key":"***"} ` | payload 内 JSON 已打码 | 保持 | JSON-01 |
| 85 | `F2-04-14R` | **FAIL** | 见 actual.full_text / `payload={"api_key":"FAKE-F204-\"quoted\"-9f1…` | payload 内转义引号值被打码 | 翻绿 | JSON-02 |
| 86 | `F2-04-14S` | PASS | 见 actual.full_text / `api_key: *** ` | api_key: *** | 保持 | KV-05 |
| 87 | `F2-04-14T` | PASS | 见 actual.full_text / `payload={"api_key": "FAKE-F204-\"quoted\"-9f…` | 当前「允许泄露」的断言必须翻转为「不含明文」 | 反转 | JSON-03 |
| 88 | `F2-04-21` | PASS | 见 actual.full_text / `api_key: *** SPACE-9f1 ` | 空格分隔后缀不残留 | 保持 | TAIL-02 |
| 89 | `F2-04-22` | PASS | 见 actual.full_text / `api_key: ***,COMMA-9f1 ` | 逗号后缀不残留 | 保持 | TAIL-02 |
| 90 | `F2-04-23` | PASS | 见 actual.full_text / `api_key: ***;SEMI-9f1 ` | 分号后缀不残留 | 保持 | TAIL-02 |
| 91 | `F2-04-24` | PASS | 见 actual.full_text / `api_key: ***>ANGLE-9f1 ` | 尖括号后缀不残留 | 保持 | TAIL-02 |
| 92 | `SRC-01` | PASS | dotenv_origin=True / `origin=True,exception=None,env_present=True` | dotenv_origin 判定不变（来源追踪器不得被 F2-04/B1-05 破坏） | 保持 | SRC-跨模块 |
| 93 | `SRC-02` | PASS | dotenv_origin=False / `origin=False,exception=None,env_present=True` | dotenv_origin 判定不变（来源追踪器不得被 F2-04/B1-05 破坏） | 保持 | SRC-跨模块 |
| 94 | `SRC-03` | PASS | dotenv_origin=False / `origin=False,exception=None,env_present=True` | dotenv_origin 判定不变（来源追踪器不得被 F2-04/B1-05 破坏） | 保持 | SRC-跨模块 |
| 95 | `SRC-04` | PASS | dotenv_origin=False / `origin=False,exception=None,env_present=True` | dotenv_origin 判定不变（来源追踪器不得被 F2-04/B1-05 破坏） | 保持 | SRC-跨模块 |
| 96 | `SRC-05` | PASS | dotenv_origin=False / `origin=False,exception=None,env_present=True` | dotenv_origin 判定不变（来源追踪器不得被 F2-04/B1-05 破坏） | 保持 | SRC-跨模块 |
| 97 | `SRC-06` | PASS | dotenv_origin=False / `origin=False,exception=None,ior=ok` | dotenv_origin 判定不变（来源追踪器不得被 F2-04/B1-05 破坏） | 保持 | SRC-跨模块 |
| 98 | `SRC-07` | PASS | dotenv_origin=True / `origin=True,exception=None,putenv_visible=Tr…` | dotenv_origin 判定不变（来源追踪器不得被 F2-04/B1-05 破坏） | 保持 | SRC-跨模块 |
| 99 | `SRC-08` | PASS | dotenv_origin=False / `origin=False,exception=None,env_present=True` | dotenv_origin 判定不变（来源追踪器不得被 F2-04/B1-05 破坏） | 保持 | SRC-跨模块 |
| 100 | `SRC-09` | PASS | dotenv_origin=False / `origin=False,exception=None,env_present=Fals…` | dotenv_origin 判定不变（来源追踪器不得被 F2-04/B1-05 破坏） | 保持 | SRC-跨模块 |
| 101 | `SRC-10` | PASS | dotenv_origin=False / `origin=False,exception=None,env_present=True` | dotenv_origin 判定不变（来源追踪器不得被 F2-04/B1-05 破坏） | 保持 | SRC-跨模块 |
| 102 | `SRC-11` | PASS | dotenv_origin=False / `origin=False,exception=None,load_return=Fals…` | dotenv_origin 判定不变（来源追踪器不得被 F2-04/B1-05 破坏） | 保持 | SRC-跨模块 |
| 103 | `SRC-12` | PASS | dotenv_origin=False / `origin=False,exception=None,data_patch=patch…` | dotenv_origin 判定不变（来源追踪器不得被 F2-04/B1-05 破坏） | 保持 | SRC-跨模块 |
| 104 | `SRC-13` | PASS | dotenv_origin=True / `origin=True,exception=None,env_present=True` | dotenv_origin 判定不变（来源追踪器不得被 F2-04/B1-05 破坏） | 保持 | SRC-跨模块 |
| 105 | `SRC-14` | PASS | dotenv_origin=False / `origin=False,exception=None,env_present=True` | dotenv_origin 判定不变（来源追踪器不得被 F2-04/B1-05 破坏） | 保持 | SRC-跨模块 |
| 106 | `SRC-15` | PASS | dotenv_origin=False / `origin=False,exception=None,threads=no-error` | dotenv_origin 判定不变（来源追踪器不得被 F2-04/B1-05 破坏） | 保持 | SRC-跨模块 |
| 107 | `SRC-16` | PASS | dotenv_origin=True / `origin=True,exception=None,b_present=b-value` | dotenv_origin 判定不变（来源追踪器不得被 F2-04/B1-05 破坏） | 保持 | SRC-跨模块 |
| 108 | `SRC-17` | PASS | dotenv_origin=True / `origin=True,exception=None,env_present=True` | dotenv_origin 判定不变（来源追踪器不得被 F2-04/B1-05 破坏） | 保持 | SRC-跨模块 |
| 109 | `SRC-18` | PASS | dotenv_origin=True / `origin=True,exception=None,installed=False` | dotenv_origin 判定不变（来源追踪器不得被 F2-04/B1-05 破坏） | 保持 | SRC-跨模块 |
| 110 | `SRC-19` | PASS | dotenv_origin=False / `origin=False,exception=None,simulated=bytes-…` | dotenv_origin 判定不变（来源追踪器不得被 F2-04/B1-05 破坏） | 保持 | SRC-跨模块 |
| 111 | `SRC-20` | PASS | dotenv_origin=False / `origin=False,exception=None,env_present=True` | dotenv_origin 判定不变（来源追踪器不得被 F2-04/B1-05 破坏） | 保持 | SRC-跨模块 |
| 112 | `SRC-21` | PASS | dotenv_origin=True / `origin=True,exception=None,errors=no-error` | dotenv_origin 判定不变（来源追踪器不得被 F2-04/B1-05 破坏） | 保持 | SRC-跨模块 |

**需要特别说明的 4 类**：

1. `F2-04-02` / `07` / `10` / `12` / `X04` / `X05` / `F2-04-14T` 的"允许泄露（设计边界）"**不是**假阳性：这些用例的值**没有键名、也没有登记**，任何基于规则的扫描器都不可能识别。修复后它们仍应 PASS。唯独 `F2-04-14T` 例外——它的值在 `payload={"api_key": …}` 内，**有键名**，属可修复范围，断言必须反转。
2. `B1-05-11/12/20` 曾被怀疑是"把缺陷编码成期望"，逐条核对后排除：三者用的是小型对照值（`abc==def` 8 字符 / `v` 1 字符 / `from_process` 12 字符），断言长度与输入一致，是**合法的回归护栏**（尤其 `abc==def` 防 `=` 二次切分、`#` 值 7 字符防注释截断）。**不得改动。**
3. `B1-05-01/02/09/14/15/16/17/19` 的"不断言内容，仅记录行为"属于**假阴性设计**（永远 PASS，等于没有断言）。其中 09/14 已改为明确断言（见上表"改写"），01/02/15/19 应保留"逐字节不变"断言，16/17 保留布尔返回断言。
4. `F2-03-11`（直接用 `Formatter` 不经 `Handler.format`）是**既有边界**，本批不改变，因此不列为待修项；但它必须出现在 §8"尚未解决问题"里，防止后续被误读成"已覆盖"。

---

## 5. 新增测试矩阵

### 5.1 F2-04（`tests/test_log_redaction_b414.py`）

| ID | 输入 | 断言 | 备注 |
|---|---|---|---|
| KV-01 | `log.exception`，消息含 `api_key=AK` | 输出不含 AK | 走真实 `exc_info` 三元组 |
| KV-02 | `status: token=AK` | 不含 AK | 吞并修复的最小用例 |
| KV-03 | `data=api_key=AK` | 不含 AK | 双层赋值 |
| KV-04 | `outer: middle: api_key=AK` | 不含 AK | 三层 |
| KV-05 | `api_key=abc=def` | 输出 `api_key: ***` | **不得**只抹 `abc` |
| KV-06 | `api_key=AK\nsymbol=AAPL` | 不含 AK **且保留** `symbol=AAPL` | 续行启发式正向 |
| KV-07 | `api_key=PART1\nPART2` | 两行都不含 | 反向 |
| KV-08 | `api_key=a"b` | 输出 `api_key: ***` | 裸值内的引号属值 |
| TAIL-01 | `raise RuntimeError(f"api_key={AK}")` | 保留 `")` | 结构尾巴 |
| TAIL-02 | `api_key=AK{空格/,/;/<,/,…}` 五种后缀 | 后缀逐字保留 | 复位 21–24 |
| JSON-01 | `{"api_key":"a\"b"}` | 不含 `a\"b` | 转义引号 |
| JSON-02 | `json.dumps({"api_key": 'a"b'})` | 不含转义值 | — |
| JSON-03 | 4 层 `json.dumps` + 值内引号 | 每层解开后 `api_key=="***"`、`symbol=="AAPL"` | 引号层数上界 15 |
| JSON-04 | 未闭合 JSON | 从 `:` 到行尾全抹 | fail-closed |
| REG-01 | `register_secret('abc"defgh')` + JSON 形态 | 不含明文 | **当前唯一击穿兜底路径** |
| URL-01 | `?api_key=x&b=1` | `api_key=***`、`b=1` 保留 | URL 形态不得被改写成 `: ***` |
| D-01 | `symbol=AAPL qty=100 price=195.32 side=buy` | 逐字节不变 | 零误伤 |
| D-02 | `token_count=5 timeout=3` | 不变 | 后缀白名单 |
| UNI-01 | 全角键 / 零宽字符键 | **已知不打码**（xfail，注明 P3 残余） | 防后续误当成回归 |

**变异测试要求（关键）**：KV-02/KV-03/KV-07/JSON-01/REG-01 必须做"删除修复即失败"验证——把 `_redact_assignments` 的调用注释掉，这 5 条必须全部变红。只跑通过不算验收。

### 5.2 F2-03（`tests/test_log_redaction_b414.py`）

| ID | 场景 | 断言 |
|---|---|---|
| F203-03 | 直驱 `emit` + 伪造 `_aitrader_redacted` | 打码（`grep` 确认该常量已不存在） |
| F203-05 | 直接 `handler.format` | 打码 |
| F203-06 | 脱敏后追加新秘密 | 新秘密被打码（摘要不一致 → 重脱敏） |
| F203-08 | 自定义 `LogRecord` 伪造 | 打码 |
| F203-12 | `copy.copy` 后追加新秘密 | 打码 |
| F203-13 | 后置 Filter 追加 | 打码 |
| F203-14 | 自定义 Formatter 追加 | 打码 |
| F203-15（新） | 两个 handler 同 record 扇出 | 输出一致、且 `redact_text` 只调用 1 次（用计数器打桩） |
| F203-16（新） | registry 抛异常 | 不得 `return text`；必须走 `redact_text` 且不含明文 |
| F203-17（新） | 100k 条记录依次流过 | `len(_CACHE) <= 上限`、无 `id` 复用误命中 |
| F203-18（新） | `_QUICK_HIT` 词表 vs `_SENSITIVE_KEYS` | 词表必须覆盖全部敏感词（防止漏词即放弃兜底） |

### 5.3 F2-02（`tests/test_log_redaction_b414.py`）

| ID | 场景 | 断言 |
|---|---|---|
| F202-02 | 伪造 `__module__` | `trusted=False`；`_PRESTINE` 对象身份不变 |
| F202-04（新） | **`importlib.reload`** | 重载后 `_TRUSTED_HOOKS[key] is` 原 stdlib 函数；`ensure_hooks()["repaired"]` 全 False **且** 锚点非 None（二者必须同时成立） |
| F202-05（新） | 重载后新建 handler | `install_on_handler` 正常返回，不落入 `degraded` |
| F202-06（新） | 替换后恢复 | 恢复原函数后 `trusted=True` |

### 5.4 B1-05（`tests/test_config_b414.py`）

见 §4 表的 `DOT-01…DOT-20`；另加：

| ID | 场景 | 断言 |
|---|---|---|
| PEM-01 | 合法 PEM **尾部带 `\n`** | 成功载入（防 §3.4 的误杀） |
| PEM-02 | 合法 PEM **CRLF** | 成功载入，且值内无 `\r` |
| PEM-03 | 值内两段 PEM 连续 | 成功载入 |
| PEM-04 | body 含非 base64 字符 | 不写入 + `pem_body_invalid` |
| SDK-01 | `load_pem_private_key(value)` | 解析成功（仅假密钥） |
| SDK-02 | 当前实现产物（仅首行） | **必须失败**（证明修复的必要性） |
| FP-01 | 多行 PEM 载入后 `dotenv_origin(name, value)` | `True`（指纹用最终值；AUD2-B1-01 回归点） |

---

## 6. 性能验收标准

### 6.1 修改前基线（实测，本机）

| 场景 | 长度 | 耗时 | 说明 |
|---|---|---|---|
| `redact_text` 普通行 × 20k | 64 B | **442.1 ms → 22.11 µs/次** | 主基线 |
| 1 MB 单行（含 `api_key=`） | 1.00 MB | **1147.8 ms** | 已存在，非本次引入 |
| 1 MB 无分隔符 | 1.00 MB | 1145.2 ms | 同上 |
| 20k 个 `k=v` | 257 KB | 85.9 ms | — |
| `_KV_RE.finditer` 单独 | 10k→400k | 1.23 / 1.23 / 1.25 / 1.20 / 1.16 µs/char | **线性，常数极大（≈64×）** |

热点定位清楚：1 MB 的 1.15 s 全部来自 `_KV_RE` —— 每个起始位置都要尝试 `[A-Za-z_][A-Za-z0-9_.\-]{0,63}` 再失配于 `[:=]`，即 O(64·n)。

另注：`redact_text` 默认 `truncate=True` 会在 262144 字符处截断，但**截断发生在扫描之后**，所以 1 MB 的扫描成本完全保留；而 L3 的 `install_format_guard` 传 `truncate=False`（`869:880`），既全量扫描又全量输出，是延迟放大器。

### 6.2 候选实现的实测（临时原型，仅验证数量级）

以"分隔符为锚、回取 ident"的写法（避免 O(64·n)）：

| 场景 | 现行 | 候选 | 比值（候选/现行） |
|---|---|---|---|
| 64 B 普通行 × 20k | 439.9 ms | 562.7 ms | **1.28×（吞吐 78%）** |
| 1 MB 单行敏感 | 1153.0 ms | 89.4 ms | **0.078×（快 12.9×）** |
| 1 MB 无分隔符 | 1156.0 ms | 35.1 ms | **0.030×（快 33×）** |
| 20k 个 `a:` | 39.8 ms | 87.9 ms | **2.21×（吞吐 45%）** |
| 10k 敏感键 | 21.1 ms | 43.4 ms | **2.05×（吞吐 49%）** |
| 200k 转义引号 | 32.5 ms | 77.8 ms | **2.39×（吞吐 42%）** |
| 20k `k=v` | 87.3 ms | 113.6 ms | **1.30×（吞吐 77%）** |
| 10k JSON 片段 | 37.8 ms | 70.5 ms | **1.86×（吞吐 54%）** |

### 6.3 门槛判定：计划门槛与用户门槛冲突

| 门槛 | 计划 §6 | 用户要求 | 冲突 |
|---|---|---|---|
| 无敏感词日志 | PERF-01 ≥60% | ≥60% | 一致 |
| 含敏感词日志 | **PERF-02 ≥40%** | **≥60%** | **冲突** |

按用户口径（≥60% = 比值 ≤1.667×），上表 **8 类中 5 类不达标**（2.21× / 2.05× / 2.39× / 1.86×）。计划用 40% 兜住这 5 类，等于自降门槛。

结论：**不能以"计划已定 40%"通过性能审查**。必须在实施前把扫描器做成不劣于 1.667×，或改设计：

- **建议 A（推荐）**：保留 `_KV_RE` 快路径不动，仅在 `_QUICK_HIT` 命中时启用扫描器。绝大多数日志行不含敏感词 → 走原路径，吞吐≈100%；含敏感词的行数量少，2× 成本可接受。
- **建议 B**：扫描器内部避免逐字符 Python 循环（`_scan_value` 的 `while i < len(text)` 在 1 MB 上是 100 万次解释器迭代）；改用 `re`/`str.find` 找候选位置。
- **建议 C（兜底）**：`_redact_assignments` 入口对 `len(text) > 64 KB` 直接整段 `_MASK`（fail-closed），把最坏情况从"1.15 s"压到"常数时间"。这也顺带修掉 L3 的延迟放大。

### 6.4 资源与长跑验收（必须在实施 PR 中给出实测）

| 指标 | 门槛 |
|---|---|
| 绝对吞吐（64 B 普通行） | ≥ 18 µs/次（基线 22.11 µs，允许 20% 劣化） |
| 含敏感词行吞吐 | ≥ 60%（用户口径） |
| 1 MB 单行 | ≤ 200 ms（现行 1148 ms，属改善项） |
| L3 单条增量（`_QUICK_HIT` 未命中） | ≤ 20 µs |
| registry 峰值规模 | 弱引用方案：无上限但实测 `len==活跃 record 数`；强引用方案：≤512 |
| 长跑内存增长（100k 条后） | RSS 增量 ≤ 5 MB；`gc.collect()` 后 registry 归零（实测弱引用可归零） |
| 并发压力 | 16 线程 × 1000 条：`faults=0`、`leaks=0`、p95 ≤ 2× 单线程 |

---

## 7. 文件修改清单（仅计划，未执行）

### 7.1 产品代码

| 文件 | 函数 / 位置 | 改动 | 关联 |
|---|---|---|---|
| `src/ai_trader/log_redaction.py` | `_PRESTINE`(567)、`_is_stdlib_hook`(578)、`_capture_pristine`(584)、`_wrap_method`(798-803) | 信任锚改对象身份；删除污染赋值；加幂等守卫；**锚点外置**（§3.2 建议 1） | F2-02 |
| 同上 | `_VALUE`(171)、`_KV_RE`(173)、`_JSON_RE`(179)、`_kv_sub`(191)、`_RULES`(221)、`redact_text`(285) | 定位 + 取值两段式；新增 `_scan_value`/`_mask_span`/`_redact_assignments` | F2-04 |
| 同上 | `_REDACTED_FLAG`(455)、`_sanitize_record`(458/497-505)、`install_format_guard`(866) | 删标记；改摘要校验；**registry 改弱引用** | F2-03 |
| `src/ai_trader/config.py` | `load_dotenv`(508-540)、`_DOTENV_FP` 写入(535) | 显式解析器 + 结构校验 + 指纹用最终值 | B1-05 |
| 同上 | `dotenv_source_diagnostics`(402) | 增加 `parse_errors` 计数 | B1-05 |
| 同上（新增） | `_parse_dotenv_text`、`_validate_pem`、`dotenv_parse_errors` | 3 个私有/内部函数 | B1-05 |
| `src/ai_trader/_redaction_anchor.py`（新增，可选） | 3 个槽位 | 跨 reload 的信任锚 | F2-02 建议 1 |

### 7.2 测试

| 文件 | 性质 |
|---|---|
| `tests/test_log_redaction_b414.py`（新增） | F2-02 / F2-03 / F2-04 的 40+ 用例（§5.1–5.3） |
| `tests/test_config_b414.py`（新增） | B1-05 的 20+ 用例（§5.4） |
| `tests/test_log_redaction_b41.py` | **不改断言**。仅 `test_traceback_is_redacted`（L~150）需补一条"去 `auth failed` 前缀"的用例——现有写法因 `auth` 后形成扫描边界而**恰好掩盖**吞并缺陷（计划 §7 已识别） |
| `tests/test_log_redaction_b411.py` / `b412` | 不改 |
| `audit_evidence/us_p1_b41_3_probes/_probe_lib.py` | 重置函数需同步新增状态（`_CACHE` / `_TRUSTED_HOOKS` / `_DOTENV_ERRORS`）；属测试基础设施 |

### 7.3 明确不修改

`config/config.yaml`（含 `key_source: "vault"`，实盘安全开关保持原值）、`.env`、`audit_evidence/` 下任何既有文件、`US_P1_BATCH4_1_3_INDEPENDENT_AUDIT.md`、`US_P1_BATCH4_1_4_REMEDIATION_PLAN.md`。

---

## 8. 尚未解决的问题

| # | 问题 | 级别 | 状态 |
|---|---|---|---|
| 1 | **F2-03 缓存方向错误**：存输出摘要比输入摘要 → 永不命中；若改成存输入摘要，命中时 `return text` 返回明文 | **P1（阻断）** | 需重写 §4.2.1 后再审 |
| 2 | **F2-03 拒绝 weakref 的决策错误**：实测 LogRecord 可弱引用、`WeakKeyDictionary` 可回收；对象作键可根除 `id()` 复用 | P1 | 建议改弱引用 |
| 3 | **B1-05 PEM 校验对尾部换行/CRLF 敏感**：合法 PEM 判 `pem_incomplete` → 拒绝启动 | **P1（阻断）** | 校验前必须 `strip` + `\r\n→\n` |
| 4 | **B1-05-05 与 §4.4.2 冲突**：转义 `\n` 单行（582→573）在"无引号不解码"下无法翻绿 | P1 | 需定案（建议 `*_KEY` 字段例外） |
| 5 | **F2-02 模块重载未覆盖**：reload 后锚点变 `None`，`ensure_hooks()` 仍报 healthy=True | **P1（阻断）** | 锚点需外置 |
| 6 | **性能门槛冲突**：计划 PERF-02=40% < 用户 60%；实测 5/8 类跌破 60% | P1 | 见 §6.3 建议 A/B/C |
| 7 | B1-05 未做 Base64 合法性检查（只查长度 <64） | P2 | 补 `b64decode(validate=True)` |
| 8 | B1-05 未考虑"一个值含多段 PEM" | P2 | 改 `finditer` 逐段校验 |
| 9 | L3 `truncate=False` 对超大文本全量扫描 + 全量输出 | P2 | 见 §6.3 建议 C |
| 10 | `_QUICK_HIT` 词表手写 → 漏词等于放弃 L3 兜底 | P2 | 必须由 `_SENSITIVE_KEYS` 派生 + 单测断言覆盖 |
| 11 | Unicode 绕过（全角键 `ａｐｉ＿ｋｅｙ`、零宽字符键 `api\u200b_key`） | P3 | 现状同样存在，非回归；不做 NFKC（会破坏位置映射），靠登记字面量兜底 |
| 12 | 直接 `Formatter().format(record)`（不经 `Handler.format`）仍不覆盖 | P3 | 既有边界（F2-03-11），本批不改变，不得宣称"日志绝对安全" |
| 13 | `Handler` 子类同时重写 `handle` 与 `format` 时 L2/L3 均绕过 | P3 | 既有边界（`test_t07`） |
| 14 | 导入期捕获存在"先脏后捕"窗口 | P3 | 属部署约束，接受 |
| 15 | `_TRUSTED_HOOKS` / `_CACHE` 仍是可写模块全局 | P3 | 与 `uninstall_hooks` 同级，属既有边界 |

---

## 9. 最终实施批准建议

### 9.1 逐条判定（对应任务书第七节）

| 批准条件 | 判定 | 依据 |
|---|---|---|
| 没有未解释的高危设计缺陷 | **不满足** | §8 的 1/3/5 三处：F2-03 缓存可致明文回传；B1-05 误杀合法凭据；F2-02 重载后锚点丢失 |
| 四项修复边界清晰 | 部分满足 | F2-02/F2-04/B1-05 的函数级边界已明确（§2）；F2-03 需重写后重新界定 |
| 测试预期与安全目标一致 | 部分满足 | 112 条中 1 条需反转（`F2-04-14T`）、2 条需改写（`B1-05-09/14`）、2 条需确认断言范围（`B1-05-05`、`RG-F2-02-B`）；其余 107 条可原样沿用 |
| 不存在会破坏来源追踪器的设计 | **满足** | 指纹改用最终值（§4.4.3）方向正确；`SRC-01…21` 全部列为"保持"，且新增 `FP-01` 跨模块回归 |
| 真实凭据不进入测试输出 | **满足** | 全程使用运行时生成的假密钥与 `FAKE-*` 占位；未读取任何真实私钥；探针 `actual` 只含假值 |
| 实盘安全开关保持原值 | **满足** | 未触碰 `config/config.yaml`、`.env`、环境变量 |

### 9.2 结论

**不建议按现计划整体批准。**

建议拆成两批：

**可批准（附 5 个补充条件）**：F2-04、F2-02、B1-05

1. F2-04 必须写明"非敏感 ident 只前移到 `sep.end()`"这条游标规则，并按 §6.3 建议 A 保留快路径（否则性能不达标）。
2. F2-02 必须新增"跨 `reload` 锚点稳定"的设计与 `F202-04/05` 测试。
3. B1-05 的 PEM 校验必须在校验前 `strip()` 且 `\r\n→\n`，并补 Base64 检查与多段 PEM 支持。
4. B1-05-05 的引号形态必须在实施 PR 中定案；若为无引号，按"`*_KEY` 字段例外"处理并写入设计。
5. `F2-04-14T` 的断言必须反转为"不含明文"；`B1-05-09/14` 改为"不写入 + parse_errors"。

**需重写后再审**：F2-03

- 缓存必须是"输入摘要 → 输出文本"，命中返回 `out`；
- registry 改用 `WeakKeyDictionary`（对象作键），删除 512 上限与 `hit[0] is record` 防御；
- 补"registry 异常 → 清空 + 走 `redact_text`"的 fail-closed 路径；
- `_QUICK_HIT` 词表由 `_SENSITIVE_KEYS` 派生并加覆盖单测。

### 9.3 复审范围

修订后只需复审 3 处，其余结论可继承本文件：

1. `US_P1_BATCH4_1_4_REMEDIATION_PLAN.md` §4.2（F2-03 缓存与 registry）；
2. 同文件 §4.3.1（F2-02 锚点外置）与 §4.4.1（PEM 校验归一化/多段）；
3. 同文件 §6 的 PERF-02 门槛（40% → 60%，或明确降级理由并取得人工豁免）。

**本阶段到此结束。未实施任何修复，未进入 Batch 4.2。等待人工批准。**
