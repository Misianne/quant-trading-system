# US-P1 Batch 4.1.1 测试报告

- **批次**：US-P1 Batch 4.1.1
- **范围**：T01–T29（F-2 / B-1 / A-1 / 回归 四组）
- **环境**：Windows / Python 3.13.14 / pytest 9.2.0 / 解释器 `C:\FosunAI-Trading\.venv\Scripts\python.exe`
- **只读参数**：`PYTHONDONTWRITEBYTECODE=1`、`-p no:cacheprovider`、`-o addopts=""`（不写 `.pyc`、不写 pytest 缓存）

---

## 1. 命令、退出码、通过数

| # | 命令 | 退出码 | 结果 |
|---|---|---|---|
| CMD-1 | `PYTHONDONTWRITEBYTECODE=1 .venv/Scripts/python.exe -m pytest tests/test_log_redaction_b411.py tests/test_credential_identity_b411.py tests/test_market_push_guard_b411.py -v -p no:cacheprovider -o addopts=""` | **0** | **32 passed**, 0 failed, 0 skipped（1.73 s） |
| CMD-2 | `PYTHONDONTWRITEBYTECODE=1 .venv/Scripts/python.exe -m pytest tests -q -p no:cacheprovider -o addopts=""` | **0** | **549 passed, 1 skipped**, 0 failed（63.81 s） |

基线（本轮开工前）：**517 passed, 1 skipped** → 现在 **549 passed, 1 skipped**，增量 **+32**，全部来自三个新文件；**既有 517 条无一条被修改或删除**。

原始日志：
- `audit_evidence/us_p1_b41_1_pytest.log`（CMD-1，逐用例）
- `audit_evidence/us_p1_b41_1_full.log`（CMD-2，全量）
- 结构化结果：`audit_evidence/us_p1_b41_1_test_results.json`

唯一的 skip 是既有用例（`tests/test_ai.py` 里依赖可选运行时的场景），与上一批次一致，本轮未新增 skip。

---

## 2. T01–T29 实测矩阵

> "计划预期"取自 `US_P1_BATCH4_1_1_REMEDIATION_PLAN.md` §测试矩阵；"实测"为本次真实执行结果，未做任何删改。

| ID | 文件 | 关注点 | 计划预期 | 实测 | 说明 |
|---|---|---|---|---|---|
| T01 | b411 log | F2-01 五种字段名变体 | FAIL→PASS | **PASS** | `session_id=` / `x_api_key=` / `X-Api-Key:` / `'sessionToken':` / `apiKey=` / URL 查询串 / `token=` 全部 `***` |
| T02 | 同上 | `token_count=5` 等不误伤 | PASS | **PASS** | 6 个带后缀字段原样保留 |
| T03 | 同上 | 转义 JSON 打码且仍是合法 JSON | FAIL→PASS | **PASS** | 0–4 层 `json.dumps` 嵌套：解回后 `symbol` 与 `api_key=="***"` 均正确 |
| T04 | 同上 | `exc_info` 中和（SDK `JsonFormatter`） | FAIL→PASS | **PASS** | 输出 JSON 无 `exception` 字段，`msg` 已打码，仍保留 `RuntimeError` 类型名 |
| T05 | 同上 | 标准 Formatter 仍打印 traceback | PASS | **PASS** | `Traceback (most recent call last)` + `ValueError` 均在，密钥不在 |
| T06 | 同上 | 自定义 `handle` 的 handler 输出打码 | FAIL→PASS | **PASS** | 靠 `Logger.handle` 层（L2）兜住 |
| T07 | 同上 | 直接 `handler.handle()` + 自定义 `format` | 记录为已知边界 | **PASS（边界）** | 断言"**确实泄露**"，同一记录走 logger 时被打码 —— 显式记录不改 SDK 无法覆盖的路径 |
| T08 | 同上 | 覆盖 `Handler.handle` 后 `ensure_hooks()` 自愈 | FAIL→PASS | **PASS** | `repaired.handler=True`，之后的输出仍打码 |
| T09 | 同上 | 重复安装不叠加 | — | **PASS** | 第二次返回 `False`，`Handler.handle` 对象不变，链深 1 |
| T10 | 同上 | 预置 `record.message` 被打码 | FAIL→PASS | **PASS** | `message` 已移出 `_STANDARD_ATTRS` |
| T11 | 同上 | `filters` 只读为元组 → wrapper 生效 | FAIL→PASS | **PASS** | `_aitrader_redaction_mode == "wrapper"`，绕过 logger 直驱也打码 |
| T12 | 同上 | wrapper 也失败 → 拒绝输出 + 计数 | FAIL→PASS | **PASS** | `level == CRITICAL+10`，四个级别均无输出，`blocked_handlers >= 1` |
| T13 | 同上 | `msg/args/extra/filter 顺序/lastResort` 回归 | PASS | **PASS** | `%s` 合并后 args 置空、普通 extra 原样、敏感 extra 打码、第三方 filter 仍可拒收；`lastResort` 单独用例（T13b）也打码 |
| T14 | 同上 | 2 MiB 丢弃 / 256 KiB 截断 / 深度 / 节点 / 环 | PASS | **PASS** | 四个上限逐一断言 |
| T15 | b411 cred | dotenv 不再压过 Vault | FAIL→PASS | **PASS** | 三个来源均为 `vault`，SDK 拿到的是 Vault 值 |
| T16 | 同上 | 混合来源默认 fail-closed | FAIL→PASS | **PASS** | `CREDENTIAL_SOURCE_MIX`，`client is None`，SDK 未构造 |
| T17 | 同上 | `credential_mixed=true` | — | **PASS（部分）** | 字段已提供；**未实现 `policy=warn`**（见 §4） |
| T18 | 同上 | 空 Vault（干净 env）fail-closed | PASS | **PASS** | `CREDENTIAL_INCOMPLETE`，注册表为空 |
| T19 | 同上 | 空 Vault 不继承其它实例 | FAIL→PASS | **PASS** | A 成立（owners=1），B `CREDENTIAL_INCOMPLETE`，owners 仍为 1；同身份共享另用 T19b 覆盖 |
| T20 | 同上 | （重定义）运维 export 的同值不被误判为注入 | — | **PASS（重定义）** | 见 §4 |
| T21 | 同上 | 双 Vault 冲突仍 `CredentialConflict` | PASS | **PASS** | 环境值不变、owners=1、A 仍 configured |
| T22 | 同上 | 释放按引用计数、只清自己 | PASS | **PASS** | A 关闭后 B 仍可用；归零才清理；重复关闭幂等 |
| T23 | 同上 | 并发初始化不写混合身份 | — | **PASS** | 2 线程不同凭证：恰好 1 成功 1 冲突；环境里 PUB/PRIV 是**同一套** |
| T24 | b411 guard | `MarketPushAPI(client).connect()` 抛 `FEED_AUTH_BYPASS` | FAIL→PASS | **PASS** | 五个入口逐个断言；薄包装 `subscribeQuote` 在 await 时同样抛 |
| T25 | 同上 | `get_valid_session` 计数 0、`websockets` 未触达 | — | **PASS** | 真实方法体一次都没执行（计数器为空）、`connects==0` |
| T26 | 同上 | `SDKClient(...)` 构造不受影响 | PASS | **PASS** | `configured=True`、`blocked_reason=""`、实例哨兵在位 |
| T27 | 同上 | 释放后类方法精确还原；多实例不提前还原 | — | **PASS** | owners 2→1 时仍拦截；归零后五个方法 `is` 原实现，且能正常执行 |
| T28 | 同上 | 完整还原 + 第三方覆盖可检测 | PASS | **PASS** | 改回 `connect` 后 `tampered == ["connect"]`，`health()` 明细同步（另有用例断言 detail 字段） |
| T29 | 全量 | 回归 | 517 passed, 1 skipped | **PASS** | 549 passed, 1 skipped, exit 0 |

补充用例（不与 T 编号冲突）：
- `test_t13b_last_resort_handler_is_redacted`：`logging.lastResort` 这条"最后兜底"输出也被打码。
- `test_t19b_same_identity_may_share_via_dotenv`：两实例读同一 `.env` → 同一身份，`owners=2` 合理共享。
- `test_install_failure_is_fail_closed`：SDK 缺一个入口方法 → 安装回滚、`configured=False`、类方法保持原样。
- `test_health_reports_guard_detail`：`market_push_guard_detail` 的 `installed/class/methods/owners`。

---

## 3. 分组结论

| 组 | 用例 | 结论 |
|---|---|---|
| F-2（T01–T14） | 15 条 | 五条泄露路径（字段名 / 转义 JSON / exc_info / 自定义 handle / 安装失败）全部在**真实 handler 输出层**关闭；1 条已知边界显式记录 |
| B-1（T15–T23） | 10 条 | 来源可区分、混合拒绝、部分集合拒绝、无 Vault 不继承、并发原子、释放按引用计数 |
| A-1（T24–T28） | 5 条 + 2 补充 | 类级拦截覆盖全部实例与薄包装、调用即抛、无 Session/无网络、引用计数与精确还原 |
| 回归（T29） | 全量 | 549 passed / 1 skipped，既有 517 条零回归 |

---

## 4. 与计划的偏差（如实登记）

1. **T17**：计划含"`policy=warn` 时 `credential_mixed=true`"。本轮**不提供 warn 降级**（它会成为绕过同源校验的开关），只提供 `credential_mixed` 与 `credential_validation.mixed` 字段，故记为"部分通过"。
2. **T20**：计划原意是"显式开关允许复用注入凭证"。该开关会重新打开 SR-B1-05，**不实现**；改为验证"运维 export 的同值不会被误判为注入"（`is_operator_value`），这是真实需求且已被单测覆盖。
3. **T07**：不是"通过修复"，而是"通过记录边界"。断言方向与其它用例相反（`assert 泄露`），用于防止后续有人误以为该路径已被覆盖。
4. **未新增 skip、未删除任何测试、未降低任何断言**：`tests/` 下除三个新文件外，57 个文件的 SHA256 与 P0 快照完全一致。

---

## 5. 安全与只读性

| 项 | 结果 |
|---|---|
| 是否联网 | **否**（假 SDK、假 Vault、本机 `StringIO` handler） |
| 是否创建真实 Session | **否**（`get_valid_session` 计数 0） |
| 是否下真实订单 | **否** |
| 是否读取真实密钥 | **否**（全部 `*-FAKE-*` 占位串） |
| 是否写 `.pyc` / pytest 缓存 | **否**（`PYTHONDONTWRITEBYTECODE=1` + `-p no:cacheprovider`） |
| 三个安全开关 | `live_trading=false`、`order_execution_enabled=false`、`paper_trading=true`（终值实测） |

---

## 6. 复现步骤

```bash
cd C:\FosunAI-Trading
# 定点测试（32 条）
PYTHONDONTWRITEBYTECODE=1 .venv/Scripts/python.exe -m pytest \
  tests/test_log_redaction_b411.py tests/test_credential_identity_b411.py \
  tests/test_market_push_guard_b411.py -v -p no:cacheprovider -o addopts=""
# 全量回归（549 条）
PYTHONDONTWRITEBYTECODE=1 .venv/Scripts/python.exe -m pytest tests -q \
  -p no:cacheprovider -o addopts=""
# 安全探针（当前代码）
PYTHONDONTWRITEBYTECODE=1 .venv/Scripts/python.exe audit_evidence/us_p1_b411_probe.py --src src
```

修复前对照：把 `src/` 拷贝到临时目录，用 `backups/us_p1_b411_20261110-123437/src/` 里的 4 个原文件覆盖，再对那份源码树跑同一探针（详见 `audit_evidence/us_p1_b41_1_security_probes.json` 的 `method` 字段）。
