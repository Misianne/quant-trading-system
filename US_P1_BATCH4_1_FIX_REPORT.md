# US-P1 Batch 4.1 修复报告（F-2：统一日志脱敏）

- 修复时间：2026-11-09 21:12 起（GMT+8）
- 修复范围：**仅 F-2**。按用户指令"不要扩大修改范围"，本轮不触碰 C-1 / D-1、不改 B-1/A-1 的既有实现
- 修复前快照：`backups/us_p1_b41_20261109-203830/`（`MANIFEST.sha256.json`）
- 结论：F-2 五项要求全部落实；新增 18 例、Batch 4 套件 23 例、全量 **517 passed / 1 skipped / 0 failed**
- 安全边界：`LIVE_TRADING=false`、`ORDER_EXECUTION_ENABLED=false`、`PAPER_TRADING=true`；未连生产 API、未启动真实行情、未下单；未改 SDK / `.env` / `config/config.yaml` / `data/**` / `requirements.txt` / `secrets.py` / `tests/helpers.py`

---

## 0. 变更总览（EOL / 编码 / SHA256）

| 文件 | 语义 diff | EOL | 行数 | 字节 | SHA256 |
|---|---|---|---|---|---|
| `src/ai_trader/log_redaction.py` | **新增** | CRLF | 543 | 22619 | `a4733d97f20de17bea1dc7c3d3b656ee2b68bf15d26a8a25482bbc52af1820f0` |
| `src/ai_trader/runtime.py` | +10 / −0 | CRLF → **CRLF** | 222 | 9847 | `769ccec94d74f28138923eca5620ed6cf7ee093bc1a361e6420742f5aff044a7` |
| `src/ai_trader/market/feed.py` | +13 / −75 | CRLF → **CRLF** | 1156 | 53540 | `fe073db4e8fc5c2185a553733f3e275177e8f9f1a1951109bb21fc4a0ff24a8b` |
| `tests/test_log_redaction_b41.py` | **新增 18 例** | CRLF | 304 | 11467 | `1166c12cc193d841869e102395dedae47ad257ca9e9786810aed155b776c34da` |

- 4 个文件均为 **UTF-8 无 BOM**、**纯 CRLF**（`\r\n` 计数 = 总行数，LF-only = 0）。
- `feed.py` 的 13 行新增全部集中在模块头部（兼容导出 + logger 级 filter），75 行删除为**旧脱敏实现**；`C-1` / `D-1` 所在的状态机与关闭分类代码 **零改动**（diff 仅覆盖文件前 95 行）。
- `broker/fosun_adapter.py` 本轮未修改（20:47 的 B-1/A-1 版本保持不变）；其 `from ..log_redaction import register_secret` 现在指向真实模块。

---

## 1. 问题与目标

Batch 4 的 F-2 只给 3 个 logger 挂了 filter，且旧实现仅在"格式化后文本发生变化"时才回写
`record.msg`。独立审计指出两个病根：

1. **父 logger 的 filter 不会处理子 logger 传播上来的记录**。`ai_trader.market.feed.child`
   的记录绕过 `ai_trader.market.feed` 的 filter，直接进入 root handler —— DEBUG 下仍可能
   把 `X-Api-Key` / `X-Session` 原样写出。
2. **未来创建的 handler 没有保护**。`setup_logging()` 之后由第三方库/测试/运维脚本新增的
   handler 不在任何 filter 链上；"只钉几个 logger"依然是掩盖而非修复。

本轮目标：把脱敏下沉到**真正的输出边界（handler）**，并把实现收敛为一个可复用模块。

---

## 2. 实现

### 2.1 `src/ai_trader/log_redaction.py`（新增）

| 能力 | 实现 |
|---|---|
| `redact_text(text)` | 7 条通用形态规则 + 已登记字面量替换；幂等；任何内部异常返回 `***`（不回退原文） |
| `redact_obj(obj)` | 递归 `dict/list/tuple/set/frozenset`，保持容器类型；敏感键名整值打码；`bytes` 解码后脱敏；异常对象转文本后脱敏；不透明对象只保留脱敏文本 |
| `RedactionFilter` | handler 级过滤器；覆盖 `msg/args`、`exc_info`→`exc_text`、`stack_info`、`record.__dict__` 中的结构化 extra |
| `register_secret(value)` | 只写登记表（`>=8` 字符，上限 64，FIFO 淘汰）；无读取接口、不打印、不返回 |
| `install_on_handler` / `install_on_handlers` | 幂等安装到 root、所有已存在 logger 的 handler、`logging.lastResort` |
| `install_global_hook` | 包装 `logging.Handler.handle`：未来创建的 handler 在首次输出前自动补 filter |

**限额**（防止日志处理本身成为故障源）：单条文本输出 256 KiB、输入硬上限 2 MiB
（超过整条丢弃，避免正则开销与"半截凭证"）、递归深度 6、单容器 100 项、单记录
1000 节点。超限部分用 `...[truncated]` 占位，**不输出剩余内容**。

**fail-closed 的两条路径**：

1. `_sanitize_record` 内部异常（含 `RecursionError`）→ `_fail_closed()` 把整条记录替换为
   `***[log redaction failed]***`，清空 `args` / `exc_info` / `exc_text` / extra；
2. 连替换都失败（极端的自定义 `LogRecord`）→ `filter()` 返回 `False`，记录被丢弃。

特别地：`getMessage()` 格式化失败时**不会**退回去脱敏原始 `msg`（那条路径可能含未登记
字面量），而是直接写占位符。

### 2.2 `runtime.setup_logging()`

```python
logging.basicConfig(...)      # 原样保留：级别、format、datefmt 均未改
install_on_handlers()         # 已存在的 root / logger handler + lastResort
install_global_hook()         # 之后创建的 handler
```

重复调用幂等：已挂 filter 的 handler 不会被重复挂载（`install_on_handler` 用
`isinstance` 判定，并在锁内二次检查）。

### 2.3 `market/feed.py`（只删重复实现）

- 兼容导出：`redact_log_text = redact_text`、`_RedactionFilter = RedactionFilter`
  （Batch 4 测试与外部调用不需要改）。
- `register_feed_secrets()` 委托给 `register_secret()`。
- 保留 `ai_trader.market.feed` / `.feed.ws` / `websockets.client` 的 **logger 级** filter
  作为纵深防御（不是主防线）。
- 删除 `_SECRETS` / `_REDACT_RULES` / `_register_secret` / `redact_log_text` /
  `_RedactionFilter` 的本地实现，规则表只有一份。

### 2.4 凭据注册时序

| 路径 | 注册点 | 是否早于任何可能打印凭证的日志 |
|---|---|---|
| 券商适配器 | `FosunBroker._resolve_credentials()` 返回前（`_register_secret_values`） | 是：早于 `_inject_sdk_env`、`SDKClient` 初始化、任何 warning/异常 |
| 行情推送 | `MarketFeed._build_auth_headers()` 组完 headers 后、`connect()` 之前 | 是：早于 websockets 握手 DEBUG |

登记动作不产生任何日志；`_resolve_credentials` 内的 Vault 读取失败日志只输出环境变量名
与异常类型，不含值。

---

## 3. 测试（18 例，全部通过）

`tests/test_log_redaction_b41.py`，覆盖用户列出的全部场景：

| # | 用例 | 验证点 |
|---|---|---|
| 1 | `test_child_logger_is_redacted_at_handler` | `ai_trader.market.feed.child` 自身无 filter，handler 输出仍打码 |
| 2 | `test_strategy_engine_logger_is_redacted` | `ai_trader.strategy.engine` 打码 |
| 3 | `test_websockets_server_debug_handshake_is_redacted` | DEBUG + `msg="%s > %s"` + 对象 args，三个凭证均不出现 |
| 4 | `test_traceback_is_redacted` | `logger.exception()` 的 traceback 打码且保留异常类型 |
| 5 | `test_preexisting_exc_text_is_redacted_too` | 已存在的 `exc_text` 也会被二次脱敏 |
| 6 | `test_nested_extra_and_future_handler_are_redacted` | 嵌套 dict/list/tuple extra；**setup 之后新增的 handler** 也生效 |
| 7 | `test_registered_secret_masked_even_without_field_name` | 裸字面量（无字段名）被打码 |
| 8 | `test_filter_exception_fails_closed_without_leaking` | `__str__` 抛异常 → 输出占位符，原文 0 次出现 |
| 9 | `test_filter_never_raises_on_weird_record` | 过滤器不向 logging 调用方抛异常 |
| 10 | `test_install_on_handler_is_idempotent` | 重复安装返回 False，filter 数量恒为 1 |
| 11 | `test_setup_logging_installs_on_existing_root_handlers` | 已有 root handler 被安装，重复 `setup_logging` 不重复 |
| 12 | `test_repeated_filtering_is_idempotent` | 同一条记录多次过滤，输出仅 1 个 `***` |
| 13 | `test_redact_text_is_idempotent_and_keeps_normal_text` | 幂等；中文正常日志不受影响 |
| 14 | `test_redact_text_masks_pem_blocks` | PEM 私钥整块抹除 |
| 15 | `test_redact_obj_preserves_container_types_and_sensitive_keys` | tuple 保持 tuple；`token_count` 不被误伤 |
| 16 | `test_redact_obj_bounds_depth_items_and_length` | 50 层嵌套、循环引用、超长文本、超大列表均受限 |
| 17 | `test_redact_obj_handles_bytes_and_exceptions` | bytes 与异常对象不泄露 |
| 18 | `test_short_values_are_not_registered` | 过短值 / None 不登记 |

### 实测结果（非预估）

```
tests/test_log_redaction_b41.py + tests/test_prod_conn_repair_b4.py
41 passed, 0 failed, 0 skipped in 3.89s        （18 新增 + 23 Batch 4）

全量 tests/
517 passed, 1 skipped, 0 failed in 61.14s      （提醒：1 skipped 为既有的 network 用例）
```

证据文件：

- `audit_evidence/us_p1_b41_pytest.log`（定向：新增 18 例 + Batch 4 的 23 例）
- `audit_evidence/junit_b41.xml`（`tests=41 failures=0 errors=0 skipped=0`）
- `audit_evidence/us_p1_b41_full.log`（全量）

---

## 4. 验收对照

| # | 要求 | 结果 | 证据 |
|---|---|---|---|
| 1 | 新建模块，提供 `redact_text` / `redact_obj` / `RedactionFilter` | ✅ | `log_redaction.py` |
| 2 | 支持字符串、嵌套 dict/list/tuple、异常文本、`exc_text`、extra | ✅ | 用例 3–7、15–17 |
| 3 | 深度 / 容器数量 / 字符串长度上限 | ✅ | 用例 16；常量 `MAX_TEXT_CHARS` / `MAX_RAW_TEXT_CHARS` / `MAX_DEPTH` / `MAX_ITEMS` / `MAX_NODES` |
| 4 | 不记录密钥原文 / 指纹 / 脱敏前内容 | ✅ | 模块内无任何 logging 调用；登记表无读取接口 |
| 5 | 过滤器异常 fail-closed | ✅ | 用例 8、9；两条 fail-closed 路径 |
| 6 | 安装到所有实际输出的 root handler | ✅ | 用例 11；`install_on_handlers` |
| 7 | 覆盖已有 handler、避免重复 | ✅ | 用例 10、11 |
| 8 | 未来创建的 handler | ✅ | `install_global_hook` + 用例 6 |
| 9 | 不只依赖父 logger filter | ✅ | 用例 1（子 logger 自身无 filter） |
| 10 | 不改变日志级别 / 格式 / 业务行为 | ✅ | `basicConfig` 参数未动；`feed.py` diff 仅头部 |
| 11 | `feed.py` 兼容导出、删除重复实现 | ✅ | `redact_log_text` / `_RedactionFilter` 别名 |
| 12 | 不改 C-1 / D-1 | ✅ | diff 仅前 95 行（脱敏区） |
| 13 | 凭据先注册、后日志 | ✅ | §2.4 |
| 14 | 独立测试（root handler 真实输出） | ✅ | 18 例全部读 `StringIO` handler 输出 |
| 15 | 换行符 / 编码 / 差异 / SHA256 | ✅ | §0 |
| 16 | 三个安全开关 `false / false / true` | ✅ | `config/config.yaml` + `.env` 实读 |

---

## 5. 诚实边界（未做 / 不覆盖）

1. **`install_global_hook` 包装了 `logging.Handler.handle`**（stdlib 方法）。这是覆盖"未来
   handler"的唯一无需第三方配合的办法；它只做一次 `isinstance` 检查并插入 filter，不改
   level/formatter。风险：若其它库也包装该方法，会形成链式调用（仍幂等）；自定义
   `handle()` 的 handler（如 `MemoryHandler`）不经过此包装，但其最终 target 会在输出时被
   覆盖。
2. **不覆盖非 logging 输出**：`print()`、直接写文件、子进程 stdout、uvicorn 自己的访问日志
   格式器若绕过 logging，均不在范围内。
3. **登记表是进程内的**：跨进程不共享；子进程（如 uvicorn worker）需要各自注册。
4. **登记表在内存中保存字面量**（替换所需），但无读取 API、无日志、无落盘；能读进程内存的
   攻击者不在本机制防御范围内。
5. **超长文本先脱敏再截断**，截断点之后的内容不会输出；这不是"完整保留日志"，是有意的
   fail-closed 取舍。
6. 未做真实行情/生产 API 验证（按保护边界禁止）；未重启任何服务。
7. `feed.py` 仍保留 3 个 logger 级 filter，属于纵深防御；即使移除，handler 级仍然生效。

---

## 6. 回滚

```bash
# 仅回滚 F-2（B-1/A-1 的 adapter 改动保留）
cp backups/us_p1_b41_20261109-203830/src/ai_trader/market/feed.py src/ai_trader/market/feed.py
cp backups/us_p1_b41_20261109-203830/src/ai_trader/runtime.py src/ai_trader/runtime.py
rm src/ai_trader/log_redaction.py tests/test_log_redaction_b41.py
```

回滚后 `fosun_adapter.py` 对 `log_redaction` 的导入在 `try/except` 内，会静默降级（不崩溃，
但 B-1/A-1 的脱敏登记失效）；如需完整回到 Batch 4 状态，再回滚 `backups/us_p1_b4_20261109-175153/`。

---

**本轮到此停止，等待独立只读审计。**
