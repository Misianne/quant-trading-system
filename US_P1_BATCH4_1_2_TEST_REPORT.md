# US-P1 Batch 4.1.2 — 测试与回归报告

- **批次**：US-P1 Batch 4.1.2
- **被测代码**：`src/ai_trader/{log_redaction,config,runtime}.py`、`src/ai_trader/broker/fosun_adapter.py`
- **新增测试**：`tests/test_log_redaction_b412.py`（15 例）、`tests/test_credential_identity_b412.py`（19 例）
- **口径**：只读、离线、假凭据；**不把 skipped 当 passed**；失败一律如实列出

---

## 1. 测试环境与命令口径

| 项 | 值 |
|---|---|
| 解释器 | `C:\FosunAI-Trading\.venv\Scripts\python.exe`（CPython 3.13.14） |
| 工作目录 | `C:\FosunAI-Trading` |
| 统一命令前缀 | `env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 <python> -B -m pytest … -q -p no:cacheprovider -o addopts=""` |
| 为什么清 `PYTHONPATH` | 避免 agent 会话的 `sitecustomize` 删除钩子介入测试进程（本机已知问题） |
| 为什么禁 `__pycache__`/cacheprovider | 保证只读、不产出仓库副产物、结果可复现 |
| 网络 | 无（SDK 全部替换为记录构造参数的桩，无 socket） |
| 凭据 | 全部随机伪造（`uuid4`），不读真实 `.env` |

机器可读结果：`audit_evidence/us_p1_b41_2_test_results.json`
原始日志：`audit_evidence/us_p1_b41_2_pytest.log`（定点）、`audit_evidence/us_p1_b41_2_full.log`（全量）

---

## 2. 逐套件结果（退出码 + 通过/失败/跳过）

| # | 套件 | 命令（fragment） | 通过 | 失败 | 跳过 | 退出码 |
|---|---|---|---|---|---|---|
| 1 | **F2 定点**（`AUD2-F2-01/02`） | `test_log_redaction_b41.py test_log_redaction_b411.py test_log_redaction_b412.py` | **48** | 0 | 0 | 0 |
| 2 | **B1 定点**（`AUD2-B1-01/02`） | `test_credential_identity_b411.py test_credential_identity_b412.py` | **29** | 0 | 0 | 0 |
| 3 | **A1 回归**（行情推送守卫，本轮未改） | `test_market_push_guard_b411.py` | **7** | 0 | 0 | 0 |
| 4 | **C1/D1 回归** | `test_market_feed_b5.py test_market_rules_b4.py` | **52** | 0 | 0 | 0 |
| 5 | Batch 3 回归 | `test_prod_conn_repair_b3.py test_prod_connectivity_p0.py` | **53** | 0 | 0 | 0 |
| 6 | Batch 4 回归 | `test_prod_conn_repair_b4.py test_us_p0.py` | **61** | 0 | 0 | 0 |
| 7 | Batch 4.1 / 4.1.1 回归 | `test_log_redaction_b41/b411, test_credential_identity_b411, test_market_push_guard_b411` | **50** | 0 | 0 | 0 |
| 8 | **全量 `tests`** | `tests` | **583** | 0 | **1** | 0 |
| 9 | 全量排除本轮新增两文件（基线对比） | `tests --ignore=…b412.py ×2` | **549** | 0 | **1** | 0 |

> 第 9 行是本轮最关键的对照：**549 passed, 1 skipped** 与 4.1.1 报告的基线
> `549 passed, 1 skipped` **完全一致** ⇒ 存量用例**零变化**（无删除、无跳过、无降断言），
> 本轮增量 34 例 = 15（F2）+ 19（B1），`549 + 34 = 583`。

**唯一跳过项**（预先存在，与本轮无关）：

```
SKIPPED [1] tests/test_ai.py:128: 未配置 AI_API_KEY —— LLM 真实调用【未测试】
```

它属于"需要真实 LLM Key 才能跑"的用例，环境未提供 Key，因此**未执行**。
本报告不把它计入通过数。

---

## 3. 新增 F2 定点用例（`tests/test_log_redaction_b412.py`，15 例）

| 用例 | 针对 | 断言要点 |
|---|---|---|
| `test_f201_setlevel_success_is_blocked_and_silent` | F2-01 | `setLevel` 成功 + 回读 `level=CRITICAL+10` → `mode=blocked`、经 logger 无输出、`healthy=true` |
| `test_f201_setlevel_raises_is_unsecured_not_blocked` | F2-01 | `setLevel` 抛异常 → `install=False`、`blocked=0`、`unsecured=1`、`healthy=false` |
| `test_f201_setlevel_silent_noop_is_unsecured` | F2-01 | 不抛异常但不生效 → 同样 `unsecured`，`level` 未被改动 |
| `test_f201_unsecured_handler_still_redacted_through_logger_but_leaks_directly` | F2-01 | 未受保护 handler：经 logger 仍打码、**直连确实泄露** —— 正因如此不能计入 blocked |
| `test_f201_blocked_count_matches_real_output_with_mixed_handlers` | F2-01 | 一好一坏两个 handler：`blocked` 与**真实输出**逐一对齐，`unsecured=1`、`healthy=false` |
| `test_f201_verified_blocked_is_idempotent_and_not_double_counted` | F2-01 | 重复 `install_on_handler` 返回 `False` 且计数不翻倍 |
| `test_f202_normal_install_keeps_logging_alive` | F2-02 | 正常安装后日志仍输出且打码（防"为了安全把日志搞哑"） |
| `test_f202_logger_handle_replaced_by_noop_is_healed_not_accepted` | F2-02 | no-op 替换 → 自愈、`logger_hook_trusted=true`、输出非空且打码 |
| `test_f202_handler_handle_replaced_by_noop_keeps_platform_logging` | F2-02 | `Handler.handle` 被替换 → 平台日志不静默 |
| `test_f202_plain_third_party_function_is_detected` | F2-02 | 普通第三方函数 → `foreign_hooks` 记录 `__module__/__name__`，链回可信实现 |
| `test_f202_repeat_install_does_not_stack` | F2-02 | 重复 `ensure_hooks` 不叠加包装层（层数=1，链尾 `logging`） |
| `test_f202_restore_failure_is_unhealthy_not_fake_success` | F2-02 | 拿不到可信原实现 → `logger_hook=false`、`healthy=false`，不虚报 |
| `test_f202_health_matches_actual_behaviour_after_heal` | F2-02 | 健康位与"是否真有输出"一致 |
| `test_f202_uninstall_restores_trusted_original` | F2-02 | `uninstall_hooks()` 对象级恢复（`is` 相等） |
| `test_f202_heal_keeps_redaction_after_noop` | F2-02 | 自愈后**仍打码**（不是只恢复输出、丢掉脱敏） |

隔离措施：autouse fixture 把三个钩子还原到 `lr._PRESTINE`，并快照/恢复
`_BLOCKED_HANDLERS / _GATED_HANDLERS / _UNSECURED_HANDLERS / _WRAPPED_HANDLERS / _FOREIGN_HOOKS`，
避免像 4.1.1 审计 `SR-TEST-01` 指出的"全局钩子留在 pytest 进程里"。

---

## 4. 新增 B1 定点用例（`tests/test_credential_identity_b412.py`，19 例）

### 4.1 `AUD2-B1-01`（7 例）

| 用例 | 断言要点 |
|---|---|
| `test_b101_untouched_dotenv_stays_dotenv` | 无人改写 → 仍 `dotenv`（防"把 dotenv 全判成 explicit"的过度修复） |
| `test_b101_same_value_rewrite_is_no_longer_dotenv` | **同值**普通赋值 → `dotenv_origin=False`、`touched_keys=3`、诊断不含键名/值 |
| `test_b101_same_value_explicit_env_beats_vault` | 审计原复现：来源全 `explicit_env`、SDK 拿到的是 env 值、`domain=local` |
| `test_b101_same_value_explicit_env_label_without_vault` | 无 Vault 时标签也必须是 `explicit_env`（顺带覆盖 `AUD2-B1-03`） |
| `test_b101_changed_value_is_explicit_env` | 被改成不同值 → `explicit_env`（既有语义回归） |
| `test_b101_inherited_process_env_is_explicit_not_dotenv` | 进程启动前已存在的变量 → `load_dotenv` 不覆盖、`dotenv_keys()` 为空、`explicit_env` |
| `test_b101_identity_fallback_when_write_tracker_unavailable` | 写入日志不可用时，Windows 仍靠对象身份识别同值改写；POSIX 保守判非 dotenv |

### 4.2 `AUD2-B1-02`（10 例）

| 用例 | 断言要点 |
|---|---|
| `test_b102_rotation_scenario_starts` | 轮换场景（PUB 留 `.env`，PRIV/API_KEY export）**能启动** |
| `test_b102_rotation_sources_and_domain_fields` | `mixed=false`、`mixed_within_domain=true`、`domain=local`、`identity_consistent=true`、`detail` 含"本地配置" |
| `test_b102_rotation_sdk_gets_rotated_identity` | 三要素**各自按来源取对值**（放行 ≠ 随便取一个） |
| `test_b102_cross_domain_explicit_plus_vault_fails_closed` | 跨域仍 `CREDENTIAL_SOURCE_MIX`，不构造 SDK，`identity_consistent=false` |
| `test_b102_cross_domain_dotenv_plus_vault_fails_closed` | 跨域（dotenv↔vault）仍拒绝 |
| `test_b102_incomplete_still_fail_closed` | 缺要素仍 `CREDENTIAL_INCOMPLETE` |
| `test_b102_all_dotenv_is_not_mixed_within_domain` | 全 dotenv：`mixed_within_domain=false`、`detail` 为空 |
| `test_b102_all_explicit_is_local_and_consistent` | 全 explicit：`domain=local`、`identity_consistent=true`、`credential_mixed=false` |
| `test_b102_identity_consistent_flag_tracks_status` | 标志与真实判定一致（成功=true / 跨域=false），禁止"启动成功就忽略身份一致性" |
| `test_b102_validate_source_model_matrix` | 判定矩阵 7 组 + `_source_domain` 3 组（域内 ok / 跨域 mixed / 缺要素 incomplete） |

### 4.3 T17 / T20（2 例）

| 用例 | 判定 | 断言要点 |
|---|---|---|
| `test_t17_original_intent_policy_warn_is_blocked_not_bypassed` | **BLOCKED（可见性 PASS）** | 配置里写 `credential_source_policy=warn` / `credential_mixed_policy=warn` 后，跨域混合仍 `configured=false` + `CREDENTIAL_SOURCE_MIX` + `credential_mixed=true`；且 `__init__` 签名不含 `policy/allow/force/ignore` 参数 |
| `test_t20_original_intent_reuse_switch_absent_but_stricter` | **PARTIAL（与实现冲突）** | 他人注入的身份（即使环境里"三要素都有值"、即使 B 自配 API_KEY）一律不复用；无 `reuse/inherit/share/adopt` 参数或全局；运维自己 export 的值仍判 `explicit_env` |

> 既有 `test_t17_health_exposes_sources_and_mixed_flag` 与
> `test_t20_operator_exported_value_is_not_mistaken_for_injection` **原样保留、未改一字**，
> 上述两条是**新增**，用于补上审计指出的"未覆盖计划目标"。

---

## 5. 审计探针复跑（第三方证据，不是自建）

### 5.1 `probe_f2_v3.py`（F-2，10 项 → **7 通过 / 3 失败**）

| 记录 | 结果 | 说明 |
|---|---|---|
| F2-E1 / E2 / E4 / E5 / E6 | PASS | `exc_text` 各形态、lastResort、filter 不可用时的 wrapper 脱敏 |
| **F2-E7** 末级降级失败 | FAIL（**断言反转**） | 该记录的 `ok` 判据是 `health["blocked_handlers"] >= 1`，即"缺陷存在"。修复后为 `0` ⇒ 判据不成立。原始 `detail`：`install=False blocked=0 via_logger_masked=True direct_leak=True` |
| **F2-E9 / E10** 钩子 no-op 自愈 | PASS | 4.1.1 审计时这两条 `emitted=''`（静默）；现在 `out='token: ***'`、`emitted=True` |
| F2-E3 未登记裸令牌 | FAIL | `AUD2-F2-04` 设计边界，非本轮范围 |
| F2-E8 伪造脱敏标记 | FAIL | `AUD2-F2-03`（低），本轮未授权修复 |

### 5.2 `probe_b1_v2.py`（B-1，16 项 → **15 通过 / 1 失败**）

| 记录 | 结果 | 说明 |
|---|---|---|
| **B1-C** 同值显式 env | PASS（**修复项**） | 4.1.1 审计：`sources=全 vault, picked=vault`；现在 `sources=全 explicit_env, picked=env` |
| **B1-C2** 同值无 Vault 标签 | PASS（**修复项**） | 现在 `explicit_env`（原为 `dotenv`） |
| **B1-D** dotenv+explicit 轮换 | PASS（**修复项**） | 4.1.1 审计：`configured=False` + `CREDENTIAL_SOURCE_MIX`；现在 `configured=True` |
| B1-A/B/E/F/G/H/I/J/L/M/N/P | PASS | 优先级、跨域 fail-closed、无继承、原子性、回滚、释放、引用计数、冲突均未回退 |
| **B1-K** `dotenv_origin` 是否仅凭值指纹 | FAIL（**断言反转**） | 该记录的 `ok` 判据要求 `after=True`（同值重写后仍判 dotenv）。修复后 `after=False` ⇒ 判据不成立。`detail` 里 `explicit_same_value=False` 正是修复结果 |

### 5.3 早期 `probe_f2.py`（已被 v3 取代，11/15）

失败 4 项及归属：`F2-E`（预置 exc_text，已被 v3 拆成 E1–E4 且全 PASS）、
`F2-I`（直接调用 handler 自己的 `emit()`，物理上无法拦截；现在该 handler 被如实记为
`unsecured` + `healthy=false`）、`F2-J`（`AUD2-F2-03`）、`F2-K`（`AUD2-F2-04`）。
**不用于本轮结论**，仅留档。

---

## 6. 自建安全探针（`audit_evidence/us_p1_b41_2_security_probes.json`，**14/14 通过**）

与审计探针的区别：审计探针的 `ok` 编码"缺陷是否复现"，修复后必然有记录翻假；
本探针断言**修复后的期望行为**，可直接作为通过证据。

| ID | 断言 | 结果 |
|---|---|---|
| F2-01a | 只能靠 level 门禁：计入 `blocked+gated`、经 logger 无输出、`degraded=true` | PASS |
| F2-01b | `setLevel` 抛异常 → `blocked=0`、`unsecured=1`、`healthy=false`、直连确实泄露 | PASS |
| F2-01c | `setLevel` 静默失败 → 同样 `unsecured`，`level` 未变 | PASS |
| F2-01d | 多 handler 单个失败：不报全部安全，也不误伤已装好的 | PASS |
| F2-02a | `Logger.handle` 换 no-op → 自愈、`trusted=true`、输出 `'token: ***'`、`foreign_hooks` 有记录 | PASS |
| F2-02b | `Handler.handle` 换 no-op → 自愈且仍打码 | PASS |
| F2-02c | 普通第三方函数 → 如实记录 `__module__/__name__` 并链回可信实现 | PASS |
| F2-02d | 重复 `ensure_hooks` 不叠加（层数=1） | PASS |
| F2-02e | `uninstall_hooks()` 精确恢复三个钩子（对象级） | PASS |
| F2-02f | 无可信原实现 → 不虚报 `logger_hook=true` | PASS |
| B1-01 | 同值 export：改前 `True` → 改后 `False` | PASS |
| B1-01 | 写入追踪器可用（`installed=true`, `storage=str`, `touched=1`） | PASS |
| B1-02 | 轮换=`ok` / 跨域=`mixed` / 缺要素=`incomplete` | PASS |
| SEC | `live_trading=false`、`order_execution_enabled=false`、`paper_trading=true`、`can_send_real_order=false` | PASS |

---

## 7. 与基线对比

| 口径 | 4.1.1 基线 | 本轮 | 差异 |
|---|---|---|---|
| 全量 `tests` | `549 passed, 1 skipped` | **`583 passed, 1 skipped`** | +34（= 15 + 19 新用例），失败 0 |
| 全量排除本轮新增文件 | `549 passed, 1 skipped` | **`549 passed, 1 skipped`** | **0**（存量完全一致） |
| 退出码 | 0 | 0 | — |
| 既有测试文件改动 | — | **0 个**（与 4.1.1 快照逐字节一致） | — |
| 既有断言降低 | — | **无** | — |
| 新跳过/新 xfail | — | **无** | — |

唯一 skipped 仍是 `tests/test_ai.py:128`（缺 `AI_API_KEY`），与 4.1.1 相同，
**不计入通过**。

---

## 8. 未覆盖 / 无法覆盖的项（如实披露）

| 项 | 原因 | 影响 |
|---|---|---|
| 真实 LLM 调用 | 无 `AI_API_KEY` | `tests/test_ai.py:128` 保持 skipped（既有） |
| 真实券商/行情连接 | 本轮安全边界禁止 | 用 SDK 桩验证"构造参数与环境值"，不验证网络行为 |
| POSIX 平台的写入追踪器实测 | 本机为 Windows；用例改为"模拟追踪器不可用 + 分支断言"，两条路径都执行 | POSIX 上对象身份不可见，退回"写入日志 + 保守判 explicit"，已在代码注释与本报告写明 |
| `gated_handlers` 被第三方直接 `emit()` 的极端场景 | 无法拦截对方自己的方法 | 计入 `gated_handlers`、`degraded=true`，见 FIX_REPORT §9 |
| `AUD2-F2-03 / F2-04 / B1-04 / B1-05` | 不在本轮四项授权内 | 保持打开，已在 FIX_REPORT §9 列明 |

---

## 9. 复现指引

```bash
cd /c/FosunAI-Trading

# 1) 定点（F2 + B1 + A1）
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 .venv/Scripts/python.exe -B -m pytest \
  tests/test_log_redaction_b41.py tests/test_log_redaction_b411.py tests/test_log_redaction_b412.py \
  tests/test_credential_identity_b411.py tests/test_credential_identity_b412.py \
  tests/test_market_push_guard_b411.py -q -p no:cacheprovider -o addopts=""

# 2) 全量
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 .venv/Scripts/python.exe -B -m pytest \
  tests -q -p no:cacheprovider -o addopts=""

# 3) 安全探针（14 项，JSON 输出）
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 .venv/Scripts/python.exe -B \
  audit_evidence/us_p1_b41_2_probes.py

# 4) 审计方原始探针（注意：E7 / B1-K 的 ok 字段编码"缺陷存在"，修复后应为 False）
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 .venv/Scripts/python.exe -B \
  audit_evidence/us_p1_b41_1_second_audit_probes/probe_f2_v3.py
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 .venv/Scripts/python.exe -B \
  audit_evidence/us_p1_b41_1_second_audit_probes/probe_b1_v2.py

# 5) 文件完整性
sha256sum -c US_P1_BATCH4_1_2_FILE_MANIFEST.sha256
```
