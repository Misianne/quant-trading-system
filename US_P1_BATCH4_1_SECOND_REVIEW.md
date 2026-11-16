# US-P1 Batch 4.1 — 第二执行体独立复核

- 复核对象：`C:\FosunAI-Trading`（星财富单 AI 自动交易系统）
- 复核范围：Batch 4.1（B-1 / A-1 / F-2 / C-1 / D-1 回归 / 测试 / 安全边界）
- 复核时间：2026-11-09 21:52:29 — 2026-11-09 22:06（GMT+8）
- 复核执行体：第二执行体（未参与 Batch 4.1 的 F-2 实现）
- 权限：**只读**。除本文件与 `audit_evidence/us_p1_b41_second_review.json` 两份交付物外，
  未在仓库内新建、修改、删除、格式化任何文件；未改数据库/配置/凭据/账户状态；
  未连接生产 API；未启动真实行情；未下单；未读取或输出 `.env` 的任何秘密值。
- 全部探针使用**构造的虚假字符串**（`SID-FAKE-…` / `XKEY-FAKE-…` / `sk-live-FAKE-…`），
  探针脚本存放在 `%TEMP%\b41_second_review\`，**不在仓库内**。

---

## 0. 结论

**总评：PARTIAL。无 P0 / 无 P1。不阻止进入 Batch 4.2，但附第 9 节的前置条件。**

| 分项 | 结论 | 一句话依据 |
|---|---|---|
| F-2 日志脱敏 | **PARTIAL** | handler 级主路径真实有效；但存在 4 类可复现绕过 + 2 条 fail-open 安装路径，不能称为"统一/可靠边界" |
| B-1 凭据身份与隔离 | **PARTIAL** | 冲突检测与引用计数实测成立；`.env` 存在时（默认启动）仍可跨实例复用身份，且三要素可来自不同来源 |
| A-1 SDK 绕过路径 | **PARTIAL** | 实例级哨兵有效（0 次会话请求）；类级构造与另外 5 个 SDK 入口未覆盖 |
| C-1 / D-1 回归 | **PASS** | `class FeedState` 起 1029 行与备份**逐字节一致** |
| 测试独立复核 | **PARTIAL** | 41 / 517 全部通过、退出码 0；但缺 4 类关键用例，1 处报告表述不实 |
| 交易安全边界 | **PASS** | 三开关 `false / false / true`；本次审计全程无网络请求、无真实凭据 |

### 三个特别判断（直接回答）

1. **F-2 当前是否可以作为可靠的日志安全边界？→ 不能。**
   它是一条**有效的 handler 级尽力而为（best-effort）脱敏**，对 stdlib `Formatter` +
   现有 logger + 现有 handler 的路径确实有效（第 4.5、3.5 节实测）。
   但"边界"意味着不可绕过，而实测有 4 类绕过（字段名、转义 JSON、`exc_info` 重渲染、
   自定义 `handle`）和 2 条 fail-open（安装失败静默放行、hook 被覆盖后无法自愈）。
   对外表述应改为"handler 级脱敏"，不应写"统一 / 无死角"。

2. **B-1 的凭据串用是否可能跨适配器造成身份混淆？→ 可能，但需同一进程内 ≥2 个实例。**
   干净环境下空 Vault 实例是 fail-closed（本审计实测，与上一份审计的笼统表述不同）；
   但只要进程环境里存在 `FSOPENAPI_API_KEY`（**默认启动即由 `load_config()` 从 `.env` 载入**），
   空 Vault 实例就会 `configured=True` 并以第一个实例的身份工作（`owners=2`）。
   另外三要素可分别来自 Vault 与环境，形成"混合身份"（服务端会 401，但配置看起来完全正确）。
   当前运行时 `build_broker()` 只创建一个实例，**现网不可达**。

3. **是否应阻止进入 Batch 4.2？→ 不阻止。**
   无 P0/P1，且 4 个 P2 均需"多实例"或"非默认 Formatter/Handler"才可达。
   但**在 P2 修复或书面接受之前，不得开启真实交易、不得把日志接入外部采集**。

---

## 1. 基线与只读证据

### 1.1 受审文件（本次实测，SHA256）

| 文件 | 字节 | 行 | EOL | BOM | SHA256 |
|---|---|---|---|---|---|
| `src/ai_trader/log_redaction.py` | 22619 | 543 | 纯 CRLF | 无 | `a4733d97…af1820f0` |
| `src/ai_trader/runtime.py` | 9847 | 222 | 纯 CRLF | 无 | `769ccec9…7f5aff044a7` |
| `src/ai_trader/market/feed.py` | 53540 | 1156 | 纯 CRLF | 无 | `fe073db4…f24a8b` |
| `src/ai_trader/broker/fosun_adapter.py` | 69860 | 1433 | 纯 CRLF | 无 | `c399f6b2…edb73162` |
| `tests/test_log_redaction_b41.py` | 11467 | 304 | 纯 CRLF | 无 | `1166c12c…776c34da` |
| `US_P1_BATCH4_1_INDEPENDENT_AUDIT.md` | 22305 | 359 | — | 无 | `b5bdf88b…41f2cc9f` |
| `US_P1_BATCH4_1_FIX_REPORT.md` | 12429 | 199 | 纯 CRLF | 无 | `cf580601…541c10ed` |

四个代码文件均为 UTF-8 无 BOM、`CRLF` 计数 == 总行数、裸 LF = 0。

### 1.2 只读证明

- 审计开始时对 `src/ tests/ config/` 下 68 个 `.py/.yaml/.yml/.txt` 建立 SHA256 清单；
  审计结束时逐条比对：**68/68 完全一致**。
- `find . -newermt "2026-11-09 21:52:00"`（排除 `.venv`、`backups`）在写入交付物之前**无任何命中**。
- pytest 以 `PYTHONDONTWRITEBYTECODE=1` + `-p no:cacheprovider` 运行，未产生 `__pycache__`/`.pytest_cache` 变更。
- 探针脚本与日志位于 `%TEMP%\b41_second_review\`（`probe_f2.py`、`probe_b1_a1.py`、`probe_all.log`）。

### 1.3 变更范围实测（相对 `backups/us_p1_b41_20261109-203830`）

用 `difflib.SequenceMatcher` 逐行比较（比 `diff -u` 的计数更贴近真实增删）：

| 文件 | 实测 | 报告声明 | 说明 |
|---|---|---|---|
| `runtime.py` | **+12 / −0** | +10 / −0 | 差异为 import、`setup_logging` docstring 与两行安装调用 |
| `feed.py` | **+14 / −87** | +13 / −75 | 差异全部在文件头部（旧第 61–174 行），语义仍是"脱敏实现替换" |
| `fosun_adapter.py` | **+185 / −27** | 上一份审计记 +161 / −27 | 计数口径不同；本轮未修改（mtime 20:47，早于 F-2 的 21:12） |

**关键更正（相对上一份审计）**：`feed.py` 的 diff **不止前 95 行**，最后一个 hunk 落在旧文件
第 163–174 行（`websockets.client` logger 的 filter 安装）。但该 hunk 仍属脱敏区，
且 `class FeedState`（旧 201 行 / 新 128 行）至文件末尾 **1029 行逐字节一致**，
`class MarketFeed` 尾部同样一致 —— C-1/D-1 零改动的结论**成立且证据更强**。

---

## 2. 方法与独立性声明

本复核**不采信**任何一份既有结论，三条原则：

1. **只认真实 handler 输出**：除纯函数用例外，全部经由 `logging` 真实链路
   （logger → `Handler.handle` → `emit` → formatter → stream）捕获文本，
   不直接对 `redact_text()` 断言。
2. **只认真实代码路径**：B-1/A-1 用真实 `FosunBroker` + 假 `SDKClient` + 内存配置
   （`live_cfg` 仅用于打开门禁分支），不联网、不触碰真实凭证。
3. **可复现**：每条结论给出探针编号、实际输出、源码 `文件:行号`。

需要如实说明：本执行体与前一份审计的不同之处仅在于**未参与 F-2 实现**；
但两者运行在同一台机器、同一份代码、同一套测试上。这不是组织意义上的第三方审计。

---

## 3. F-2 四条泄露路径逐条复现

探针：`%TEMP%\b41_second_review\probe_f2.py`（场景 1–4）。全部场景在**独立子进程**中运行，
每次先执行生产入口 `runtime.setup_logging()`，再把 root handler 换成 `StringIO`，
因此测的是"记录进入 handler 时"的真实形态。

### 3.1 路径一：未覆盖的字段名（`session_id=` / `x_api_key=` 等）

**结论：成立（5/5 泄露）。严重程度 P2。**

最小复现（场景 1）：

```python
setup_logging("INFO")
log = logging.getLogger("ai_trader.market.feed.child")   # 子 logger，自身无 filter
log.warning("A url=wss://x.test/ws?session_id=%s", SID)
log.warning("B sessionId: %s", SID)
log.warning("C x_api_key=%s", XK)
log.warning("D session_token=%s", SID)
log.warning("E sessionid=%s", SID)
log.warning("F X-Session: %s", XS)      # 对照
log.warning("G x-api-key: %s", XK)      # 对照
```

实际输出（原文摘录）：

```
A url=wss://x.test/ws?session_id=SID-FAKE-8f3a2c1d9e7b
B sessionId: SID-FAKE-8f3a2c1d9e7b
C x_api_key=XKEY-FAKE-4b7c9d2e6a10
D session_token=SID-FAKE-8f3a2c1d9e7b
E sessionid=SID-FAKE-8f3a2c1d9e7b
F X-Session: ***
G x-api-key: ***
LINE[A session_id=] found=True LEAK=True
LINE[B sessionId:]  found=True LEAK=True
LINE[C x_api_key=]  found=True LEAK=True
LINE[D session_token=] found=True LEAK=True
LINE[E sessionid=]  found=True LEAK=True
LINE[F 对照 X-Session] found=True LEAK=False
LINE[G 对照 x-api-key] found=True LEAK=False
```

源码原因（两条规则都不覆盖）：

- 通用规则 `src/ai_trader/log_redaction.py:118-120` 的分支是 `session`，
  要求其后**紧跟** `\s*[:=]`；而 `session_id` / `sessionId` / `session_token` / `sessionid`
  在 `session` 之后还有字符，`\b` 边界也不成立 → 不匹配。
- 专用规则 `:109-110` 只写 `x-api-key` / `x-session`（**连字符**），不接受下划线
  → `x_api_key=` 不匹配。
- URL 规则 `:122-123` 同样只认 `session=` 整体，不认 `session_id=`。

**真实可达性：当前不可达。**
依据：仓库内没有任何以文本形式打印认证头的调用
（`grep "X-Api-Key\|X-Session" src` 仅命中注释）；SDK 的 session 一律走结构化 extra
（`sdk_src/fsopenapi/auth.py:97-100`、`:129-132`），由 `_SENSITIVE_KEYS`
（`log_redaction.py:129-136`，含 `sessionid`）整值打码；历史 `logs/**` 中
`X-Api-Key` / `X-Session` / `session_id=` / `x_api_key=` 命中数均为 **0**。
**风险在"下一行新代码"**：任何人写 `logger.info(f"sid={session_id}")` 就泄露。

### 3.2 路径二：转义 JSON

**结论：成立（1/1 泄露）。严重程度 P3。**

最小复现（场景 2）：

```python
raw = json.dumps({"api_key": SECRET})      # {"api_key": "sk-live-FAKE-..."}
escaped = json.dumps(raw)                  # "{\"api_key\": \"sk-live-FAKE-...\"}"
log.warning("H raw_body=%s", raw)
log.warning("I escaped_body=%s", escaped)
```

实际输出：

```
H raw_body={"api_key": "***"}
I escaped_body="{\"api_key\": \"sk-live-FAKE-2f9a7c4e1b6d8035\"}"
LINE[H 对照 raw] found=True LEAK=False
LINE[I escaped]   found=True LEAK=True
```

源码原因：JSON 规则 `log_redaction.py:112-114` 要求 `"key"` 的**闭合引号紧跟键名**；
转义形式里闭合引号是 `\"`（`\` 在引号之前），因此不匹配；通用规则 `:118-120`
又要求键名后紧跟 `[:=]`，同样被 `\` 隔断。

**真实可达性：当前不可达。** 仓库与 SDK 都没有"先 `json.dumps` 再当字符串打印"的路径；
SDK 的 `log_body` 走 `sanitize_payload()`（`sdk_src/fsopenapi/client.py:78-86`），
结构化键值会被 SDK 自己打码。定级 P3 而非 P2：需要"双重编码"这一非自然写法。

### 3.3 路径三：SDK `JsonFormatter` 直接重渲染 `exc_info`

**结论：成立（过滤器确实执行了，`exception` 字段仍泄露）。严重程度 P2。**

最小复现（场景 3）：

```python
from fsopenapi.logging_utils import JsonFormatter
handler = logging.StreamHandler(buf)
handler.setFormatter(JsonFormatter())          # 新 handler，靠 hook 自动装 filter
try:
    raise RuntimeError("handshake rejected: api_key=%s" % SECRET)
except RuntimeError:
    log.exception("connect failed api_key=%s", SECRET)
```

实际输出：

```
payload.msg       = connect failed api_key: ***
payload.exception = Traceback (most recent call last): ... raise RuntimeError("hands…
payload.exc_text  = None
LEAK[exception] = True
LEAK[msg]       = False

对照（标准 Formatter）LEAK = False
```

`payload.msg` 已被打码，证明 **filter 确实执行了**（不是没装）；泄露只发生在
`exception` 字段。源码：

- 过滤器只预置 `record.exc_text`，**不清空/不替换 `record.exc_info`**：
  `src/ai_trader/log_redaction.py:385-392`（`:387` 仅在 `exc_text` 为空时生成，`:389-390`
  写入的是打码后的文本，但 `record.exc_info` 原样保留）。
- SDK `JsonFormatter` 无视 `exc_text`，直接用 `exc_info` 重新渲染：
  `sdk_src/fsopenapi/logging_utils.py:131-132`
  （`payload["exception"] = self.formatException(record.exc_info)`）。
- `exc_text` 之所以在 payload 里为 `None`，是因为它在
  `_RESERVED_RECORD_FIELDS`（`logging_utils.py:30`）里被跳过，不是"没设置"。

**真实可达性：项目运行时不使用该 Formatter。**
`JsonFormatter` 在本仓库仅出现于 `sdk_src/example/example.py:28` 与
`sdk_src/example/push_example.py:29`；`runtime.setup_logging()` 用 stdlib `Formatter`
（`runtime.py:216-220`），且实测标准 Formatter 不泄露。
定级 P2 的理由：修复成本极低（把 `exc_info` 中和为已脱敏形式），而一旦运维照抄 SDK 示例，
泄露的是**完整 traceback**，通常包含请求上下文。

### 3.4 路径四：自定义 Handler 重写 `handle()`

**结论：成立。严重程度 P3。**

最小复现（场景 4）：

```python
class _HandleOverride(logging.Handler):
    def __init__(self): super().__init__(); self.lines = []
    def handle(self, record):                 # 不调用 super().handle
        self.lines.append(self.format(record))
    def emit(self, record): pass

log = logging.getLogger("ai_trader.strategy.engine")   # 无 logger 级 filter
log.warning("J X-Api-Key: %s", XK)
```

实际输出：

```
J X-Api-Key: XKEY-FAKE-4b7c9d2e6a10
LEAK[handle-override] = True

对照（只重写 emit）LEAK = False
```

源码原因：`install_global_hook()` 包装的是 `logging.Handler.handle`
（`log_redaction.py:529-541`），子类重写 `handle` 会**遮蔽**该包装；
而 filter 本来就在 `Handler.handle` 里执行，所以两条保护同时失效。
只重写 `emit` 是安全的（filter 已在 `handle` 阶段跑过）—— 对照实测确认。

**真实可达性：当前不可达。** 仓库内无任何 `logging.Handler` 子类
（`grep "logging.Handler\|def handle(\|def emit(" src` 仅命中 `log_redaction.py` 自身）。
定级 P3：需要第三方库或运维脚本采用这种非标准写法。

### 3.5 四条路径汇总

| 编号 | 路径 | 实测 | 现网可达 | 严重程度 |
|---|---|---|---|---|
| SR-F2-01 | `session_id=` / `sessionId:` / `x_api_key=` / `session_token=` / `sessionid=` | 5/5 原样输出 | 否 | **P2** |
| SR-F2-02 | 转义 JSON `{\"api_key\": …}` | 原样输出 | 否 | P3 |
| SR-F2-03 | SDK `JsonFormatter` 重渲染 `exc_info` | `exception` 含原文，`msg` 已打码 | 否（仅 SDK 示例） | **P2** |
| SR-F2-04 | 自定义 `Handler.handle` | 原样输出 | 否 | P3 |

**四条全部可复现，且全部不是"当前生产路径"。** 这与上一份审计的核心判断一致，
但本轮把可达性与严重程度逐条拆开，没有整体打包成"4 条 P2"。

---

## 4. 额外审查

### 4.1 `install_global_hook()` 是否会被第三方库重新赋值覆盖

**会，而且无法自愈。严重程度 P3（SR-F2-05）。**

探针（场景 5）：先保存 `logging.Handler.handle`（模拟"先导入的库"持有的原版引用），
`setup_logging()` 之后再把它赋回去（模拟库用陈旧引用覆盖），然后调用
`install_global_hook()`：

```
reinstall() -> False (期望 True 才算能自愈)
LEAK[after-overwrite] = True
```

源码：`log_redaction.py:525-528` 用模块级 `_ORIGINAL_HANDLE is not None` 判断"已安装"，
一旦为真就直接返回 `False`，**不再检查 `logging.Handler.handle` 是否仍是自己的包装**。
后果分两层：

- 已装 filter 的 handler **仍然受保护**（场景 12 实测：`V first…***` / `W second…***`）；
- `setup_logging()` 之后**新建**的 handler 完全没有保护，且不会报错。

现实性：完全替换 `Handler.handle` 的库很少，但用 `mock.patch` 的测试、APM/追踪类库、
热重载框架都可能短暂替换它。定级 P3。

### 4.2 多次安装、重复初始化、异常恢复

| 检查 | 实测 | 结论 |
|---|---|---|
| `install_on_handlers()` 重复调用 | 第 1/2 次均返回 0（setup 时已装），filter 数恒为 1 | 幂等 ✅ |
| `install_global_hook()` 重复调用 | 第二次返回 `False` | 幂等 ✅ |
| 模块 `importlib.reload` 后再安装 | 返回 `True`（**又包一层**），输出仍只有 1 个 `***` | 输出正确，但每 reload 增加一层包装（P3，SR-F2-06） |
| 过滤器内部异常 | 输出 `***[log redaction failed]***`，原文 0 次 | fail-closed ✅ |
| 构造失败后重建（B-1） | `release` 后环境清空，新实例 `configured=True`、`owners=1` | 恢复 ✅ |

### 4.3 Formatter / Handler / Filter 的调用顺序

- 顺序为 `logger.filter → Handler.handle → handler.filters → emit → Formatter.format`。
  实测（场景 11）后续 filter 看到的是**已打码**文本：
  `后续 filter 看到 = ['U X-Session: ***']`，`LEAK[spy] = False`。
- `install_on_handler(prepend=True)`（`:479-480`）确实把脱敏 filter 放在最前，符合"其它 filter
  可能读 `getMessage()`"的设计意图。
- `lastResort` 路径（无任何 handler）实测有效：`T X-Session: ***`（场景 10）。
- **`record.message` 是死分支**（SR-F2-07，P3）：`message` 在 `_STANDARD_ATTRS`
  （`:358-363`）里，循环在 `:399-401` 先 `continue`，`:405-406` 的
  `elif key == "message"` 永远不可达。场景 7 直接把 `record.message` 预置为
  `api_key=<FAKE>`，filter 后仍原样：
  `filter 后 record.message = api_key=sk-live-FAKE-…`，`LEAK[preset-message] = True`。
  标准 Formatter 会在 format 时重算 `message`，所以现网无影响；但任何"读 `record.message`
  而不重算"的 Formatter 会拿到未脱敏文本。

### 4.4 是否存在不经过 `Handler.handle()` 的输出路径

**有，且不止一类。** 这是"F-2 是边界"这一说法最根本的漏洞：

1. **Handler 子类重写 `handle()`** —— 3.4 已实测泄露。
2. **安装本身失败且静默放行**（SR-F2-08，P3）：
   `install_on_handler` 用 `except Exception: return False`（`:484-485`）吞掉所有错误，
   `setup_logging` 忽略返回值（`runtime.py:221-222`）。
   场景 13 用 `filters = ()`（不可变序列）的 handler：
   `install_on_handler() -> False`，随后输出
   `Y X-Session: XSESS-FAKE-c5d8e1f4a72b`，`LEAK[install-failed] = True`。
   即"装不上"与"装上了"对外表现完全相同（都是静默）。
3. **`print()` / `sys.stderr` / 文件落盘**：
   - `src/ai_trader/cli.py:305-306`：`ai-trader vault get --show` 直接把**明文密钥**
     打到 stdout（设计如此，需显式 `--show`；但它是绕过 F-2 的真实出口）。
   - `src/ai_trader/audit/faults.py:140-155`：`FaultRecorder` 把 `entry.detail` 的 JSON
     写入 `faults.jsonl` 并原样打到 stderr，**完全不经 logging**。
     现有调用点的 detail 都是订单/审计字段，不含凭证；但这是一条无脱敏的旁路。
4. **`warnings` 模块**：仓库未调用 `logging.captureWarnings`，
   `warnings.warn` 走 `showwarning` 直写 stderr，不经 handler。
5. **子进程/子解释器输出**（如运维脚本、uvicorn reload 子进程）不在本进程 hook 范围内。

结论：F-2 只能覆盖"经过 logging 且经由标准 `Handler.handle`"的输出。
这一点在 `log_redaction.py:35-37` 的"边界与限制"里已写明（"只保护经过 logging 的记录"），
但**没有覆盖上面第 1、2 类**，且与"统一/无死角"的表述冲突。

### 4.5 `exc_info` / `exc_text` / 结构化 `extra` 的最终保护

| 载体 | 实测（场景 8、9、14） | 结论 |
|---|---|---|
| `record.msg` + `record.args`（字符串） | `O token: ***` | ✅ |
| `record.args` 为**对象**（websockets 握手风格） | `server > <Request headers X-Api-Key: *** X-Session: ***` | ✅ |
| `record.exc_text`（标准 Formatter） | `ValueError: boom ***`，原文 0 次 | ✅ |
| 嵌套 extra：`{"nested":[{"api_key":…}], "tuple":(…)}` | `{"nested": [{"api_key": "***"}], "tuple": ["***", 1]}` | ✅ |
| 非敏感键名但值含已登记字面量 | `note = value=***` | ✅ |
| 敏感键名 `session`（extra） | `session = ***` | ✅ |
| `exc_info`（自定义/JSON Formatter） | 见 3.3 | ❌ P2 |

补充：`_sanitize_record` 在异常路径下会把 `record.msg` 直接替换为占位符并清空 `args`
（`:373-375`），不会回退到未登记原文——这一点比"保留原 msg 再格式化"的实现更安全。

### 4.6 B-1：无 Vault 消费者是否会继承其他实例的凭据

**分两种环境，结论不同。这是本复核与上一份审计最大的分歧点。**

#### 环境 A：进程环境干净（无 `FSOPENAPI_API_KEY`）

探针 `b1`，真实 `FosunBroker` + 假 SDK + 内存 `live_cfg`：

```
[A] configured = True  | env = {PUB1, PRIV1, API_KEY: None} | owners = 1
[B] configured = False
[B] blocked_reason = SDK 初始化失败: CredentialConflict: 进程内已存在不同的
                     FSOPENAPI_SERVER_PUBLIC_KEY（…），拒绝静默覆盖；
[B] env = {PUB1, PRIV1} | owners = 1
[C 空 Vault] configured = False | origin = {PUB: injected, PRIV: injected, API_KEY: missing}
[D vault=None] configured = False | origin = 同上
```

原因：`_SDK_ENV_MAP`（`fosun_adapter.py:452-455`）**只含 PUB / PRIV 两个变量**，
API Key 走构造参数不写环境；所以空 Vault 实例只能继承到 2/3 个要素，
第三个 `missing` → `_init_client` 的 `missing` 分支（`:644-654`）直接 fail-closed。

**结论：干净环境下不存在"静默继承"，上一份审计的"无条件继承"表述不准确。**
冲突检测（B 用不同 Vault）与引用计数（`owners`）则完全成立。

#### 环境 B：环境里有 `FSOPENAPI_API_KEY`（**默认启动即如此**）

`load_config()` 默认 `use_dotenv=True`（`config.py:338-340`），`load_dotenv()` 会把
`.env` 的键 `setdefault` 进 `os.environ`（`config.py:238-256`，`:253-254`），
而本仓库 `.env` 中 `FSOPENAPI_SERVER_PUBLIC_KEY` / `FSOPENAPI_CLIENT_PRIVATE_KEY` /
`FSOPENAPI_API_KEY` **三个键均已声明**（仅核查键名，未读取值）。

探针 `b1e`（用假值 `AK_ENV` 模拟 `.env` 提供 API_KEY）：

```
[A/.env] configured = True | origin = {PUB: vault, PRIV: vault, API_KEY: env}
[A/.env] vault 里的 API_KEY 是否被采用 = False | 实际 = AK_ENV
[C/.env 空 Vault] configured = True | origin = {PUB: injected, PRIV: injected, API_KEY: env}
[C/.env 空 Vault] 实际 sdk api_key = AK_ENV | owners = 2
```

两个独立问题：

- **SR-B1-04（P2）混合来源**：同一实例的三个凭证可分别来自 Vault 与环境。
  本探针里 PUB/PRIV 来自 Vault、API_KEY 来自环境。若两者属于不同账户，
  SDK 会因签名与 API Key 不匹配被服务端拒绝（401），**不会**以他人身份成交；
  但 `health()` 仍显示 `configured=True`、`credential_origin` 只在细看时才暴露混合来源，
  排障成本高，且"Vault 轮换 API Key 不生效"是安全隐患。
- **SR-B1-05（P2）空 Vault 继承**：C 以 A 的身份完成初始化（`owners=2`），
  且 `configured=True`。它本应 fail-closed。当前 `build_broker()`
  （`runtime.py:73-92`）只建一个实例，**现网不可达**；多账户/多 worker/测试夹具场景可达。

**是否造成"身份混淆"？** 是 —— 但准确的表述是：
"一个本应未配置的实例，会以另一个实例的完整身份对外提供服务"，
而不是"两个不同账户互相串用"（后者会被 `CredentialConflict` 拦住，实测 B 已拦住）。

### 4.7 A-1：是否还有未被 sentinel 覆盖的 SDK 网络入口

探针 `a1`，真实适配器 + 假 SDK：

```
client.market_push 类型 = _MarketPushDisabled | isinstance = True
哨兵 connect() -> FEED_AUTH_BYPASS | 禁止直接调用 SDK 行情推送接口 market_push.connect…
哨兵调用后 get_valid_session 次数 = 0
feed_auth_ready = (True, '')
feed_auth_headers 键 = ['X-Api-Key', 'X-Session'] | 本次 get_valid_session = 1
```

实例级哨兵**有效**：`connect/subscribe/unsubscribe/close` 全部在
`__getattr__`（`fosun_adapter.py:300-308`）处立即抛错，且**0 次**会话请求。
`feed_auth_headers` 的 1 次 `get_valid_session` 是合法路径（真实业务需要 Session）。

未覆盖的入口：

- **SR-A1-02（P3）类级构造绕过**：`from fsopenapi.api.market_push import MarketPushAPI`
  后 `MarketPushAPI(broker.client)` 不经过实例属性，哨兵完全不参与。实测：

  ```
  直连构造 connect() -> RuntimeError | 连接异常: RuntimeError: err: [NETWORK BLOCKED BY PROBE]
  直连构造 get_valid_session = 1 | 触发 websockets.connect = True
  ```

  （`websockets.connect` 被探针替换为立即抛错，**未发起任何真实连接**。）
  即：先取 Session，再连硬编码生产地址 —— 正是哨兵要阻止的行为。
  仓库内无此调用点，故 P3。
- **SR-A1-03（P3）其它 5 个 SDK 入口未守卫**：
  `client` 上 `market / trade / portfolio / account / session` 均不是哨兵
  （`__init__.py:32-43` 共创建 10 个 API 对象，适配器只替换了 `market_push`）。
  这些入口的每次调用都会在 `OpenAPIClient._request`（`client.py:105`）里
  `auth_manager.get_valid_session()` → 可能触发 SessionCreate。
  适配器自身的业务方法都先过 `_require()`（`fosun_adapter.py:778-793`），
  但 `broker.client` 是公开属性，任何持有它的代码都能绕过。
- **未见其它旁路**：适配器内 `get_valid_session` 只有 1 处调用点
  （`fosun_adapter.py:437`，在 `_require()` 之后）；无直接 `requests.*` 调用。

---

## 5. 测试独立复核

### 5.1 独立复跑（只读参数：`PYTHONDONTWRITEBYTECODE=1`、`-p no:cacheprovider`、`-o addopts=""`）

| 范围 | 命令要点 | 结果 | 退出码 |
|---|---|---|---|
| F-2 专项 | `pytest tests/test_log_redaction_b41.py -q` | **18 passed in 0.07s** | 0 |
| Batch 4.1 定向 | `… test_log_redaction_b41.py test_prod_conn_repair_b4.py` | **41 passed in 3.45s** | 0 |
| 含行情套件 | 上述 + `test_market_feed_b5.py` | **61 passed in 5.61s** | 0 |
| 全量 | `pytest tests -q` | **517 passed, 1 skipped, 1 warning in 61.42s** | 0 |

与报告声明的 41 / 517 一致；`1 skipped` 为既有跳过，非本轮引入。

### 5.2 覆盖缺口（实测确认）

`tests/test_log_redaction_b41.py` 18 例中，**8 例**真正读取 handler 输出
（`:51`、`:65`、`:77`、`:104`、`:129`、`:159`、`:181`、`:221`）；
其余 10 例是纯函数（`:238-303`）、直接调用 `filter()`（`:117-123`、`:193-196`）
或只检查 `handler.filters`（`:202-218`）。

**报告表述不实（SR-DOC-01，P3）**：`US_P1_BATCH4_1_FIX_REPORT.md:160` 写
"18 例全部读 `StringIO` handler 输出"—— 实测为 8/18。

缺失的用例（正是本复核发现问题的四类，故测试无法拦住）：

1. `session_id=` / `sessionId:` / `x_api_key=` / `sessionid=`（会失败，故未写）；
2. 转义 JSON；
3. `exc_info` + 非标准 Formatter（`JsonFormatter`）；
4. 自定义 `Handler.handle`；
5. `stack_info`、具名 logger handler、`lastResort`；
6. hook 被覆盖后的自愈、安装失败的行为。

### 5.3 测试卫生

`root_capture`（`:30-45`）在 fixture 中调用 `setup_logging()`，但 finally 只移除自己加的
handler（`:43-45`），**不卸载 `logging.Handler.handle` 上的全局 hook**。
`test_setup_logging_installs_on_existing_root_handlers`（`:209-218`）同样。
后果：hook 常驻 pytest 进程，影响后续用例的日志行为（当前 517 例全绿，未造成失败）。
这不是产品缺陷，但会让"某个用例通过"失去部分鉴别力（SR-TEST-01，P3）。

---

## 6. 与第一份审计的差异（确认 / 推翻）

本复核独立复算后，对上一份审计（`US_P1_BATCH4_1_INDEPENDENT_AUDIT.md`）作如下处置：

| 上一份结论 | 本复核 | 依据 |
|---|---|---|
| B1-03「无 Vault 实例静默复用他实例身份」P2 | **部分推翻**：干净环境是 fail-closed；仅当环境有 `FSOPENAPI_API_KEY`（.env）时成立 | 探针 `b1` vs `b1e` |
| F2-01/02/03/04 四条均 P2 | **重新分级**：F2-01、F2-03 为 P2；F2-02、F2-04 为 P3（可达性不同） | 3.1–3.4 |
| F2-05「`message` 死分支」P3 | **确认**，并补了动态证据 | 场景 7 |
| F2-08「hook 可被覆盖」P3 | **确认**，并证明"已装 filter 的 handler 仍安全，只有新 handler 失守" | 场景 5、12 |
| DOC-01「报告称 adapter 本轮未修改，与 +161/−27 不符」P2 | **推翻（误报）**：adapter mtime 20:47，早于 F-2 开工的 21:12，"本轮未修改"字面为真；相对备份的差异来自更早的 B-1/A-1 轮 | `ls --time-style=full-iso` |
| 「`feed.py` diff 仅前 95 行」 | **修正**：diff 延伸到旧第 174 行，但状态机区逐字节一致 | 1.3 节 |
| 「4 条绕过，无当前生产路径触发」 | **确认** | 3.5 节 |
| 总评 PARTIAL、不阻止下一阶段 | **确认** | 第 9 节 |

**新增问题（上一份未列）**：SR-B1-04 混合来源、SR-F2-08 安装失败静默 fail-open、
SR-A1-02 类级构造绕过、SR-DOC-02 diff 计数偏差。

---

## 7. 问题清单

### P0 / P1

**无。** 判定依据：三条安全开关实测为 `live_trading=false`、`order_execution_enabled=false`、
`paper_trading=true`（`status()` / `gate.snapshot()`），真实券商连接被 `can_connect_broker()`
在 `runtime.py:78-92`、`fosun_adapter.py:637-641` 两处拦住；本次审计全程零网络请求。

### P2（4 项，需在开启真实交易前处置）

| 编号 | 领域 | 问题 | 证据 | 可达性 |
|---|---|---|---|---|
| SR-F2-01 | F-2 | 5 种 session/api-key 字段名变体不匹配，原样输出 | `log_redaction.py:109-110`、`:118-120`、`:122-123`；场景 1 | 当前无路径，新代码即触发 |
| SR-F2-03 | F-2 | `exc_info` 未中和，非标准 Formatter 重渲染泄露整个 traceback | `log_redaction.py:385-392`；`logging_utils.py:131-132`；场景 3 | 仅 SDK 示例/运维脚本 |
| SR-B1-04 | B-1 | 三要素可来自不同来源（Vault + env），无同源校验 | `adapter:500-551`；`config.py:238-256`；探针 `b1e` | 默认 .env 场景即成立 |
| SR-B1-05 | B-1 | 空 Vault 实例在 `.env` 场景下以他实例身份 `configured=True` | `adapter:526-529`；探针 `b1e`（owners=2） | 需同进程 ≥2 实例 |

### P3（加固项，建议随下一批清理）

| 编号 | 领域 | 问题 | 证据 |
|---|---|---|---|
| SR-F2-02 | F-2 | 转义 JSON 不匹配 | `log_redaction.py:112-114`；场景 2 |
| SR-F2-04 | F-2 | 自定义 `Handler.handle` 绕过 | `:516-543`；场景 4 |
| SR-F2-05 | F-2 | hook 被覆盖后 `install_global_hook()` 返回 `False`，无法自愈 | `:525-528`；场景 5 |
| SR-F2-06 | F-2 | 模块 reload 后重复包装（输出仍正确） | 场景 6 |
| SR-F2-07 | F-2 | `record.message` 死分支，预置值不被处理 | `:358-363`、`:399-406`；场景 7 |
| SR-F2-08 | F-2 | 安装失败静默 fail-open | `:484-485`、`runtime:221-222`；场景 13 |
| SR-A1-02 | A-1 | `MarketPushAPI(client)` 类级构造绕过哨兵 | `adapter:609-634`；探针 `a1` |
| SR-A1-03 | A-1 | `market/trade/portfolio/account/session` 五个入口未守卫 | `sdk __init__.py:32-43`；`client.py:105` |
| SR-TEST-01 | 测试 | 全局 hook 常驻 pytest 进程，fixture 不卸载 | `test_log_redaction_b41.py:30-45`、`:209-218` |
| SR-DOC-01 | 文档 | "18 例全部读 handler 输出"不实（实测 8/18） | `US_P1_BATCH4_1_FIX_REPORT.md:160` |
| SR-DOC-02 | 文档 | diff 计数偏差（`feed.py` 实为 +14/−87，非 +13/−75；`runtime.py` +12，非 +10） | 1.3 节 |

---

## 8. 未验证事项（UNVERIFIED）

| 项 | 原因 |
|---|---|
| 真实券商服务端对"混合身份"的实际响应 | 无凭证、禁止联网；只能推断为签名/密钥不匹配 → 401 |
| `websockets` 各版本 DEBUG 日志的真实字段格式 | 未安装真实推送、未连接；用对象 `__str__` 等价复现 |
| uvicorn `dictConfig` 在真实启动时是否替换 root handler | 未启动真实服务；只验证了"新 handler 经 hook 自动装 filter"这一机制 |
| 多线程并发首次输出时 filter 插入的竞争 | 未做压力测试；代码有锁内二次检查，但未实测 |
| `QueueHandler`/`QueueListener` 跨线程/跨进程路径 | 仓库未使用；未测 |
| 生产 `.env` 与 Vault 是否属于同一账户 | 禁止读取秘密值；只确认三个键均已声明 |
| `record.message` 死分支的现实触发者 | 未找到会读取预置 `message` 的 Formatter；仅证明该分支不可达 |

---

## 9. 结论与放行

### 9.1 分项结论

| 项 | 结论 |
|---|---|
| F-2 日志脱敏 | **PARTIAL** |
| B-1 凭据身份与隔离 | **PARTIAL** |
| A-1 SDK 绕过路径 | **PARTIAL** |
| C-1 / D-1 回归 | **PASS** |
| 测试独立复核 | **PARTIAL** |
| 交易安全边界 | **PASS** |
| **总评** | **PARTIAL** |

### 9.2 是否放行 Batch 4.2

**不阻止。** 无 P0/P1，且 4 个 P2 均需"多实例"或"非默认 Formatter/Handler"才可达。
进入 Batch 4.2 前需满足以下任一处置（修复或**书面接受**，二者均可）：

1. SR-F2-01：补充 `session_id` / `sessionId` / `session_token` / `sessionid` / `x_api_key`
   规则（或在 `redact_text` 前做一次键名归一化）；
2. SR-F2-03：在 `_sanitize_record` 末尾中和 `record.exc_info`
   （置 `None` 或替换为已脱敏的安全形式），保证任何 Formatter 都拿不到原始 traceback；
3. SR-B1-04 / SR-B1-05：对三要素做同源校验，或把"无 Vault 实例复用注入值"限制为
   显式开关（默认 fail-closed）；
4. SR-F2-08：`install_on_handler` 失败时至少 `logger.error` 一次（当前完全静默）。

### 9.3 明确禁止

在上述 4 项处置完成或书面接受之前：

- **不得开启真实交易**（`LIVE_TRADING` / `ORDER_EXECUTION_ENABLED` 保持 `false`）；
- **不得把 `logs/**` 接入外部采集/第三方 APM**（F-2 不是可靠边界）；
- **不得在文档或对外说明中把 F-2 描述为"统一/无死角日志脱敏"**。

---

*本复核仅为只读审计。全部探针脚本位于 `%TEMP%\b41_second_review\`，未写入仓库；
仓库 `src/ tests/ config/` 68 个文件在审计前后哈希完全一致。*
