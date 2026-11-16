# US-P1 Batch 4.1.2 — 遗留安全问题定点修复报告

- **批次**：US-P1 Batch 4.1.2（依据第二轮独立审计 `US_P1_BATCH4_1_1_SECOND_INDEPENDENT_AUDIT.md`）
- **范围**：只修 4 项（`AUD2-F2-01` / `AUD2-F2-02` / `AUD2-B1-01` / `AUD2-B1-02`）+ T17/T20 原始意图核对
- **未做**：Batch 4.2、发布、实盘、VPS、生产配置、依赖、SDK 源码
- **生成时间**：2026-11-10

---

## 0. 结论摘要

| 项 | 审计 ID | 最终判定 | 一句话证据 |
|---|---|---|---|
| Handler 禁用失败时的健康位误报 | **AUD2-F2-01** | **FIXED** | `setLevel` 抛异常 / 空操作 / 回读不符 → `mode=unsecured`、`blocked_handlers=0`、`unsecured_handlers=1`、`healthy=false`，并向 stderr 告警 |
| 钩子被换成 no-op 后 `ensure_hooks` 误接受 | **AUD2-F2-02** | **FIXED** | no-op 被识别为"未知第三方"，链回导入期 `_PRESTINE` 快照；`logger_hook_trusted=true` 且日志不再静默（`out='token: ***'`） |
| 同值显式 export 被判 dotenv，Vault 越级 | **AUD2-B1-01** | **FIXED** | 来源判定从"值指纹"升级为"加载时来源元数据 + 写入日志"；审计复现 B1-C 由 `sources=全 vault` 变为 `sources=全 explicit_env`、`picked=env` |
| dotenv+explicit 被误判 mixed | **AUD2-B1-02** | **FIXED** | 同源校验改为按**信任域**判定；轮换场景 `configured=True`，跨域（vault↔本地）仍 `CREDENTIAL_SOURCE_MIX` |
| 计划测试项 T17 | — | **BLOCKED（部分实现）** | 可见性（`credential_mixed`）PASS；`policy=warn` 放行按授权**不实现**，等运维决策 |
| 计划测试项 T20 | — | **PARTIAL（与实现冲突）** | "复用开关"不存在且不得新增（会重开 SR-B1-05）；原断言保留，差异见 §8 |

**新增测试**：`tests/test_log_redaction_b412.py`（15 例）+ `tests/test_credential_identity_b412.py`（19 例）= 34 例，全部通过。
**回归**：全量 `583 passed, 1 skipped`（exit 0）；排除本轮新增两个文件后 `549 passed, 1 skipped`，与 4.1.1 基线**逐条一致**。

---

## 1. 执行边界核对（全部满足，无越界）

| 约束 | 实测 | 结论 |
|---|---|---|
| `LIVE_TRADING=false` | `gate.state.live_trading=false` | ✅ |
| `ORDER_EXECUTION_ENABLED=false` | `gate.state.order_execution_enabled=false` | ✅ |
| `PAPER_TRADING=true` | `gate.state.paper_trading=true` | ✅ |
| `KILL_SWITCH` 未被动过 | `kill_switch=false`（哨兵不存在） | ✅ |
| 不连券商 | `gate.can_connect_broker()=(false, "PAPER_TRADING=true…")` | ✅ |
| 不发真实单 | `gate.can_send_real_order()=(false, …)` | ✅ |
| 不访问 VPS `203.0.113.10` | 无任何网络调用；全部为本地进程内测试 | ✅ |
| 不连生产 API / 不建 Session / 不连行情 | SDK 全部替换为桩（记录构造参数），无 socket | ✅ |
| 不读/打印真实凭据 | 测试与探针全部使用随机伪造值（`uuid4`） | ✅ |
| 不改 `.env` / `config/config.yaml` | mtime + 未出现在本轮变更清单 | ✅ |
| 不改 SDK 源码 / 依赖清单 / 密钥文件 | 同上 | ✅ |
| 不改 `tests/helpers.py` | 同上 | ✅ |
| 不删/跳过/降既有断言 | 既有测试文件哈希与 4.1.1 快照**逐字节一致**（见 §2） | ✅ |
| 不执行 Git 破坏性命令 | 仓库无 `.git`，未执行任何 git 命令 | ✅ |
| 不覆盖既有备份与报告 | `backups/` 下 8 个既有目录全部保留；4.1.1 的三份报告未改 | ✅ |

复现命令（只读）：

```bash
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 \
  .venv/Scripts/python.exe -B audit_evidence/us_p1_b41_2_probes.py
# 末条 SEC 记录：live_trading=false, order_execution_enabled=false,
#                paper_trading=true, can_send_real_order=false
```

---

## 2. 工作区状态与备份

### 2.1 工作区

- 仓库**没有 `.git`**，无法用 `git status` 证明"改了哪些文件"；改用**双证据**：
  1. 批前逐文件 SHA256 快照（`backups/us_p1_b412_20261110-133241/MANIFEST.sha256.json`）；
  2. 批后按 mtime 扫描 `src/ tests/ config/ .env requirements.txt pyproject.toml pytest.ini setup.cfg`。

扫描结果（唯一被改动的文件，`__pycache__` 已排除）：

```
src/ai_trader/log_redaction.py            (修改)
src/ai_trader/config.py                   (修改)
src/ai_trader/runtime.py                  (修改)
src/ai_trader/broker/fosun_adapter.py     (修改)
tests/test_log_redaction_b412.py          (新增)
tests/test_credential_identity_b412.py    (新增)
```

与 4.1.1 预快照（`audit_evidence/b411_pre_snapshot_20261110-123437.json`，61 个条目）逐文件比对：
**57 条完全一致 / 0 条缺失 / 仅 4 条不一致（就是上表四个源文件）**。
即：**没有任何既有测试文件被修改或删除**（断言未被降低）。

### 2.2 独立时间戳备份

```
backups/us_p1_b412_20261110-133241/
├── MANIFEST.sha256.json
└── src/ai_trader/{log_redaction.py, config.py, runtime.py, broker/fosun_adapter.py}
```

备份写入后逐字节校验 `identical: true`（4/4）。`backups/` 下 8 个既有目录
（`us_p0_20261108-183854`、`us_p1_b1_…`、`us_p1_b2_…`、`us_p1_b3_…`、`us_p1_b4_…`、
`us_p1_b41_…`、`us_p1_b411_20261110-123437`、`us_p1_prodconn_…`）均未删除、未覆盖。

---

## 3. 修改文件清单（前后 SHA256）

| 文件 | 前 bytes | 前 SHA256 | 后 bytes | 后 SHA256 |
|---|---|---|---|---|
| `src/ai_trader/log_redaction.py` | 31 452 | `bef31deaafdd7c149a34c876022100617da07e8851d1fcbbb18cd6006412fb34` | 42 695 | `720b4f33ac98f7f09a95fddc1ca5244f3f17b577f77f38aa24825874f7556b4a` |
| `src/ai_trader/config.py` | 15 546 | `3ee1bc0fee23ad4a0b90928cc2303b44ff4779acc93b2cb2b4145c1dc019362d` | 25 691 | `ddb8144c37b1d23806c1a8bc44753eade1f64f5b1927fdfdf1c7fd4ba0efe8e0` |
| `src/ai_trader/runtime.py` | 10 706 | `a3ad630ca87df45d332c2dff10bc9b630f61bb7f47cdc269cff543b7739616fd` | 11 509 | `4fe3f75724f61c959430ac72b13d6e94ffa03e6993ef55a5746d526e15233524` |
| `src/ai_trader/broker/fosun_adapter.py` | 94 691 | `1cf22d39343fe21bf178a3c1e718f085d59b12bfebf972fa56ea5d1dfa2a10b9` | 99 668 | `1171d42c244d58ef1788c1af82bcaab12781c15e66a0069fdef5446fb73eee26` |
| `tests/test_log_redaction_b412.py` | — | — | 新增 | 见 `US_P1_BATCH4_1_2_FILE_MANIFEST.sha256` |
| `tests/test_credential_identity_b412.py` | — | — | 新增 | 同上 |

---

## 4. AUD2-F2-01 — 末级降级失败时的健康位误报

### 4.1 根因（审计 `fosun`，`log_redaction.py:617-627, 746-755`）

三级降级的最后一档是 `handler.setLevel(CRITICAL+10)`。旧实现把"`setLevel` 没抛异常"直接
等同于"已拒绝输出"，于是：

* `setLevel` 被重写成**空操作**（不抛异常、level 不变）→ 仍计入 `blocked_handlers`；
* `setLevel` 抛异常 → 仍计入 `blocked_handlers`；
* `redaction_health().blocked_handlers=1`，但该 handler 直连驱动时**照常输出明文**。

审计实测：`install=False, blocked_handlers=1, via_logger_masked=True, direct_leak=True`。

### 4.2 修改点

| 位置 | 改动 |
|---|---|
| `log_redaction.py:612` `_level_gate_verified()` | 新增。**回读** `handler.level`：必须是 `int`（非 `bool`）且 `> logging.CRITICAL`。不再使用 `Handler.isEnabledFor`（`logging.Handler` 根本没有这个方法，旧实现踩过这个坑） |
| `log_redaction.py:637` `_neutralize_handle()` | 新增。`setLevel` 成功后**再**尝试把实例 `handle` 换成 `_drop_record`，连"绕过 logger 直驱"也堵住；改不动不算失败但如实记为 gated |
| `log_redaction.py:646` `install_on_handler()` | 末级降级重写：`blocked = _level_gate_verified(handler)`；`blocked=True` → `_BLOCKED_HANDLERS`（+ `_GATED_HANDLERS` 若只能靠门禁）+ stderr 告警；`blocked=False` → `mode=unsecured`、`_UNSECURED_HANDLERS`、返回 `False`、stderr 明确告警"未被脱敏保护" |
| `log_redaction.py:901` `redaction_health()` | 新增 `blocked_handlers / gated_handlers / unsecured_handlers / wrapped_handlers`；`healthy` 要求 `unsecured_handlers == 0`；`degraded` 覆盖"有 handler 只能靠 level 门禁" |
| `runtime.py:242` | `setup_logging()` 在 `ensure_hooks()` 后检查 `health["healthy"]`，不通过则 `logger.error(...)` 明确写出 `unsecured_handlers / 三个钩子 / foreign_hooks`，禁止把"未受保护"当"已保护"用 |

### 4.3 修复前后对比（审计同一构造：`filters` 只读返回 `()`、`__setattr__` 拒写 `handle`/`level`、`setLevel` 抛异常）

| 量 | 修复前 | 修复后 | 期望 |
|---|---|---|---|
| `install_on_handler()` | `False` | `False` | 失败要如实返回 |
| `_aitrader_redaction_mode` | `blocked` | **`unsecured`** | 没禁用就不能叫 blocked |
| `blocked_handlers` | `1` | **`0`** | 只统计确实无法输出者 |
| `unsecured_handlers` | 不存在该字段 | **`1`** | 必须单列 |
| `redaction_health().healthy` | `True` | **`False`** | 健康位反映真实输出能力 |
| 直连驱动 `h.handle()` 是否明文 | 是 | 是（不变） | 正因为会泄露，才不能计入 blocked |
| stderr 告警 | 无 | `handler NOT secured (cannot disable it) … healthy=false` | 必须可见 |

### 4.4 定点用例（`tests/test_log_redaction_b412.py`）

`test_f201_setlevel_success_is_blocked_and_silent`、
`test_f201_setlevel_raises_is_unsecured_not_blocked`、
`test_f201_setlevel_silent_noop_is_unsecured`、
`test_f201_unsecured_handler_still_redacted_through_logger_but_leaks_directly`、
`test_f201_blocked_count_matches_real_output_with_mixed_handlers`、
`test_f201_verified_blocked_is_idempotent_and_not_double_counted`。

覆盖审计要求的全部 6 个场景：`setLevel` 成功 / 抛异常 / 不抛但不生效 / handler 仍尝试
输出秘密 / `blocked_handlers` 与真实输出一致 / 多 handler 中单个失败不得报全部安全。

---

## 5. AUD2-F2-02 — 钩子被替换为 no-op 后被当成原实现

### 5.1 根因（`log_redaction.py:658-669, 703-720, 758-767`）

`_wrap_method` 把"当前被装的函数"直接当作 `original` 链入。第三方把
`logging.Logger.handle` 换成 `lambda self, record: None` 后：

* `ensure_hooks()` 认为"已安装"，`repaired={'logger': True}`、`logger_hook=True`；
* 但整条日志链**永久静默**（`emitted=''`），健康位完全正常。

### 5.2 修改点

| 位置 | 改动 |
|---|---|
| `log_redaction.py:567` `_PRESTINE` | 模块导入时抓取三个钩子的**可信原实现**（只认 `fn.__module__ == "logging"`），见 `_capture_pristine()` |
| `log_redaction.py:578` `_is_stdlib_hook()` | 新增。可信性判据：`__module__` 必须是 `logging` / `logging.*` |
| `log_redaction.py:768` `_wrap_method(owner, name, wrapper, key)` | 重写。四种情形分开处理：① 当前是我们的**有效**包装（`_aitrader_original` 可信）→ 幂等返回 `False`；② 当前是 stdlib 原实现 → 用它并刷新 `_PRESTINE`；③ 当前是未知替换但 `_aitrader_original` 可信 → 链回原实现；④ 未知替换且无可信原实现 → **不安装**，写 `_FOREIGN_HOOKS[key] = {replaced_by_module/name, chain_to:"none", installed:false}` |
| `log_redaction.py:886` `_hook_state()` | 新增。`installed`（我们的包装在）+ `trusted_original`（原实现可信）两个维度 |
| `log_redaction.py:901` `redaction_health()` | `*_hook_trusted` 三字段 + `foreign_hooks` 明细；`healthy` 要求三个钩子 `installed and trusted`，且 `foreign_hooks` 里没有 `installed=false` |
| `log_redaction.py:934` `ensure_hooks()` | 未通过健康检查时向 stderr 打印**具体组件名**，不虚报 |
| `log_redaction.py:967` `uninstall_hooks()` | 新增。只恢复"当前仍是我们的包装"的钩子，第三方改过的一律跳过（不覆盖别人的改动），返回 `{restored, skipped}` |

### 5.3 修复前后对比

| 场景 | 修复前 | 修复后 |
|---|---|---|
| `Logger.handle` 换成 no-op 后 `ensure_hooks()` | `repaired={'logger': True}`、`logger_hook=True`、输出 `''`（永久静默） | `repaired={'logger': True}`、`logger_hook=True`、`logger_hook_trusted=True`、`foreign_hooks['Logger.handle']={replaced_by_module:'__main__', chain_to:'trusted', installed:true}`、输出 `'token: ***'` |
| `Handler.handle` 换成 no-op | 同上（静默） | `handler_hook=True`、输出 `'api_key: ***'` |
| 被换成"有真实行为的第三方函数" | 被当作原实现链入（后续再包装会叠加） | 识别为 foreign，链回可信原实现，输出仍正确、仍打码 |
| 重复 `ensure_hooks()` | 可能叠加包装 | `same_object=True`、包装层数 `1`、链上 `original.__module__='logging'` |
| 连可信原实现都拿不到 | 虚报 `logger_hook=True` | `logger_hook=False`、`healthy=False`、`foreign_hooks[...].installed=false` |
| 正常卸载 | 无该入口 | 三个钩子**对象级**恢复为 `_PRESTINE`（`is` 相等） |

### 5.4 定点用例（`tests/test_log_redaction_b412.py`）

`test_f202_normal_install_keeps_logging_alive`、
`test_f202_logger_handle_replaced_by_noop_is_healed_not_accepted`、
`test_f202_handler_handle_replaced_by_noop_keeps_platform_logging`、
`test_f202_plain_third_party_function_is_detected`、
`test_f202_repeat_install_does_not_stack`、
`test_f202_restore_failure_is_unhealthy_not_fake_success`、
`test_f202_health_matches_actual_behaviour_after_heal`、
`test_f202_uninstall_restores_trusted_original`、
`test_f202_heal_keeps_redaction_after_noop`。

覆盖审计要求的全部 8 个场景：正常安装 / 被换成 no-op / 被换成普通第三方函数 /
`ensure_hooks` 检测 / 重复安装 / 恢复失败 / 健康位与行为一致 / 正常日志不永久静默。

---

## 6. AUD2-B1-01 — 同值显式 export 被判 dotenv，Vault 越级

### 6.1 根因（`config.py:261-283` + `fosun_adapter.py:831, 842-848`）

`dotenv_origin()` 只比对"键名 + 值指纹"。运维把**与 `.env` 完全相同**的值显式 export 后：

* 指纹判定仍返回 `True` → `from_dotenv=True`；
* `_classify_source` 的 explicit 分支要求 `not from_dotenv` → **被跳过**；
* 流程落到 Vault 分支 → **Vault 越级**，SDK 拿到 Vault 的值。

审计实测：`sources={PUB: vault, PRIV: vault, API: vault}`、`picked=vault`。

### 6.2 修改点（`src/ai_trader/config.py`）

| 位置 | 改动 |
|---|---|
| `:250` `_DotenvValue(str)` | `load_dotenv` 写入环境的值改成 `str` 子类**标记对象**：`repr/len/==/encode` 与普通 str 完全一致，不携带任何额外信息 |
| `:286` `_DOTENV_TOUCHED` | 载入后**被外部重写过**的键集合（哪怕写回一模一样的值） |
| `:310` `_InternalEnvWrite` / `:333` `internal_env_write()` | 受控写入标记（可重入）：`load_dotenv` 载入、凭据注册表注入/恢复 都走它，这类写入**不算**外部改写 |
| `:338` `_note_env_write()` / `:350` `_install_environ_tracker()` | 给 `type(os.environ).__setitem__` 套一层**只记账**的包装（幂等、异常全吞、先调原实现）。这是跨平台判据：POSIX 下 `os.environ` 内部存 `bytes`，看不到对象身份，只能靠"写入动作" |
| `:392` `_environ_storage_kind()` / `:402` `dotenv_source_diagnostics()` | 可审计诊断：追踪器是否装上、内部存储类型、标记/被改写键数（不含键名与值） |
| `:446` `dotenv_origin()` | 四个条件缺一不可：键名由本进程载入 / 载入后无外部写入 / 值指纹一致 / 无写入日志时必须对象身份一致。**任何一项无法确认一律判 explicit_env**（保守：不让 Vault 覆盖进程环境里已存在的值） |
| `:508` `load_dotenv()` | 载入时安装追踪器、在 `internal_env_write()` 内写入标记对象、登记归一化键名与指纹 |

`fosun_adapter.py:831` `_classify_source` 的优先级**保持不变**（`explicit_env > vault > dotenv > injected`），
但 `from_dotenv` 现在来自**加载时元数据**，因此同值显式 export 会走 explicit 分支并在 Vault **之前**返回。

### 6.3 修复前后对比（审计 B1-C：`.env` 载入 → export 同值 → Vault 另有一套值）

| 量 | 修复前 | 修复后 | 期望 |
|---|---|---|---|
| `health()['credential_sources']` | 三个全 `vault` | **三个全 `explicit_env`** | explicit 标签 |
| SDK 实际拿到的私钥 | Vault 值 | **`.env`/export 的值** | 不得被 Vault 覆盖 |
| `dotenv_origin(同值)` | `True` | **`False`** | 同值不能证明来源 |
| `dotenv_origin(未改写的值)` | `True` | `True` | 不能把 dotenv 全部误杀 |
| 无 Vault 时标签（审计 B1-C2 / B1-03） | `dotenv` | **`explicit_env`** | 顺手修掉 B1-03 |

### 6.4 定点用例（`tests/test_credential_identity_b412.py`，7 例）

`test_b101_untouched_dotenv_stays_dotenv`、`test_b101_same_value_rewrite_is_no_longer_dotenv`、
`test_b101_same_value_explicit_env_beats_vault`、`test_b101_same_value_explicit_env_label_without_vault`、
`test_b101_changed_value_is_explicit_env`、`test_b101_inherited_process_env_is_explicit_not_dotenv`、
`test_b101_identity_fallback_when_write_tracker_unavailable`。

（最后一条验证"写入日志装不上时"，Windows 仍靠**对象身份**识别同值改写；POSIX 无法看到身份时
按 explicit 保守处理。）

---

## 7. AUD2-B1-02 — dotenv + explicit 误判 mixed，轮换场景无法启动

### 7.1 根因（`fosun_adapter.py:808-851, 860-880`）

`_validate_source_model` 把"出现两种及以上来源"一律判 `mixed` 并抛
`CredentialSourceMix`。但 `dotenv` 与 `explicit_env` **本来就都是运维本地配置**：
`.env` 里保留未变的公钥、只 export 轮换后的私钥与 API_KEY，是完全正常的轮换操作，
却被判成"混合身份"而拒绝启动。

### 7.2 修改点（`src/ai_trader/broker/fosun_adapter.py`）

| 位置 | 改动 |
|---|---|
| `:447` `_SOURCE_DOMAIN` | 新增**信任域**映射：`explicit_env/dotenv/injected → local`、`vault → vault`。`injected` 归 `local` 的理由写在注释里（它只在"我们写入且 Vault 为空"时出现，来源本就是本地配置；若单列一域，成功初始化后的 health 重解析会把正常显式配置误报成 mixed） |
| `:886` `_validate_source_model` | 改为按域判定：跨域才 `mixed`；域内混用返回 `ok` 并在 `detail` 写明"本地配置内混用…三要素齐全且同属本地信任域"；缺要素仍 `incomplete`；跨域优先于缺要素上报 |
| `:917` `_source_domain` | 新增审计字段：`local / vault / mixed / none` |
| `:927` `_resolve_credentials` | ① 记录 `mixed_within_domain`、`domain`、`identity_consistent`；② health 重解析时，若环境里的值仍是**当初我们自己注入**的那个（指纹 + owner 双重确认），按**决策当时的来源与取值**汇报 —— 否则一次成功的显式配置会在 `health()` 里被重算成 `vault/injected` 混合，误报 `credential_mixed=true` |
| `:981/:1957` | `health()` 暴露 `mixed_within_domain / domain / identity_consistent` |

### 7.3 修复前后对比

| 场景 | 修复前 | 修复后 |
|---|---|---|
| 轮换：`PUB` 留在 `.env`，`PRIV/API_KEY` export | `configured=False`，`blocked_reason` 含 `CREDENTIAL_SOURCE_MIX` | **`configured=True`**，`sources={PUB:dotenv, PRIV:explicit_env, API:explicit_env}`，`domain=local`，`mixed=False`，`mixed_within_domain=True` |
| 跨域：`PUB` 显式 env + `PRIV/API` Vault | `mixed` 拒绝 | `mixed` 拒绝（**未削弱**） |
| 跨域：`PUB` 来自 `.env` + `PRIV/API` Vault | `mixed` 拒绝 | `mixed` 拒绝（**未削弱**） |
| 缺一个要素 | `incomplete` | `incomplete`（**未削弱**） |
| 全 dotenv / 全 explicit | `ok` | `ok`，且 `mixed_within_domain=False`、`identity_consistent=True` |

### 7.4 定点用例（`tests/test_credential_identity_b412.py`，10 例）

`test_b102_rotation_scenario_starts`、`test_b102_rotation_sources_and_domain_fields`、
`test_b102_rotation_sdk_gets_rotated_identity`、`test_b102_cross_domain_explicit_plus_vault_fails_closed`、
`test_b102_cross_domain_dotenv_plus_vault_fails_closed`、`test_b102_incomplete_still_fail_closed`、
`test_b102_all_dotenv_is_not_mixed_within_domain`、`test_b102_all_explicit_is_local_and_consistent`、
`test_b102_identity_consistent_flag_tracks_status`、`test_b102_validate_source_model_matrix`。

---

## 8. T17 / T20 原始意图与最终判定

### 8.1 原始定义（`US_P1_BATCH4_1_1_REMEDIATION_PLAN.md:1041, 1044`）

| ID | 计划原文 | 计划意图 |
|---|---|---|
| T17 | `policy=warn` 时 `credential_mixed=true` | 提供 warn 降级开关，并验证即使降级，混合标记仍可见 |
| T20 | 显式开关开启且三要素齐全才复用 + 告警 | 提供**显式开关**允许复用他人注入的身份，但必须三要素齐全并告警 |

### 8.2 实际实现（4.1.1）与审计判定

| ID | 4.1.1 实际测试 | 审计判定 |
|---|---|---|
| T17 | `test_t17_health_exposes_sources_and_mixed_flag` —— 只断言字段存在与指纹格式，**没有任何 `policy` 开关** | 部分通过，不计 PASS（`AUD2-T-01`） |
| T20 | `test_t20_operator_exported_value_is_not_mistaken_for_injection` —— 断言的是"运维 export 的同值不被误判为注入"，与"复用开关"无关 | 重定义，不计 PASS（`AUD2-T-02`） |

### 8.3 本轮处理与最终判定

**T17 → BLOCKED（可见性一半 PASS）**

- 恢复的那一半：`credential_mixed` / `credential_validation.mixed` 必须在混合身份时**如实为 true**，
  且**任何配置都无法掩盖它**。已实现，见 `test_t17_original_intent_policy_warn_is_blocked_not_bypassed`：
  配置里写 `fosun.credential_source_policy=warn` / `credential_mixed_policy=warn` 后，
  跨域混合仍 `configured=False` + `CREDENTIAL_SOURCE_MIX` + `credential_mixed=True`。
- **不实现**的那一半：`policy=warn` 放行。它会成为**绕过同源校验的开关**，让"一半来自 Vault、
  一半来自本地"的混合身份成功初始化，与 fail-closed 直接冲突。按本轮授权
  "不得为通过而开放不安全回退" → **标记 BLOCKED，等运维/架构决策**。
- 附带的反向保护：用例断言 `FosunBroker.__init__` 签名里**不存在** `policy/allow/force/ignore`
  之类参数，防止后人偷偷加回放行口子。

**T20 → PARTIAL（与实现冲突，实现更严格；原断言保留）**

- 实现现状：**不存在**"复用他人注入身份"的开关，复用被**无条件禁止**（三要素齐全也不行）。
  这比计划原意**更严格**，且不能为了对齐计划而新增开关 —— 新增即重新打开 SR-B1-05
  （A 释放后 B 会以 A 的身份继续跑）。
- 按"若与实现冲突则保留原始断言并报告差异"：既有 `test_t20_operator_exported_value_is_not_mistaken_for_injection`
  **原样保留、未改一字**；新增 `test_t20_original_intent_reuse_switch_absent_but_stricter` 保留原意中的
  **安全实质**并锁定差异：
  1. 环境里"看起来有"PUB/PRIV（实为 A 注入）时，B 不得把它们当自己的；
  2. B 自己提供 API_KEY（三要素里有自己配的一项）也不行；
  3. 不存在 `reuse/inherit/share/adopt` 参数或同名全局常量；
  4. 运维**自己 export** 的值仍按 `explicit_env` 处理（不误杀）。
- **差异清单（待决策）**：计划要求的"显式开关 + 告警"机制**未实现且不建议实现**；
  如运维确实需要"多实例共享同一身份"，正确做法是让两个实例指向**同一个 Vault 条目**
  （已有 T19b/T21 覆盖的引用计数路径），而不是复用一个实例的注入。

---

## 9. 未解决风险与已知边界（如实披露）

| # | 项 | 级别 | 状态 |
|---|---|---|---|
| 1 | `gated_handlers`：handler 的 `handle` 不可写、只能靠 level 门禁时，**直连驱动**（不经 logger）仍可能输出（若该 handler 同时重写了 `format` 绕过 L3） | 中（既有边界，T12 已接受） | 现计入 `gated_handlers` 且 `degraded=true`；未改变 T12 语义。彻底封堵需替换 `Handler.handle` 类级钩子（会与第三方冲突），留待决策 |
| 2 | `AUD2-F2-03`：`record._aitrader_redacted` 是普通属性，可伪造以跳过 L3 | 低 | **本轮未修**（不在四项授权内）。触发前提是记录同时绕过 L1/L2，现实路径不存在 |
| 3 | `AUD2-F2-04`：未登记、无键名的裸字面量不打码 | 信息（设计边界） | 未修；`src/` 内无以裸令牌记录 session/api_key 的调用点 |
| 4 | `AUD2-B1-04`：`CredentialConflict` 的错误码被泛化 `except` 吞掉，`blocked_reason` 不含 `CREDENTIAL_CONFLICT` | 低 | **本轮未修**（不在授权内）。fail-closed 行为本身正确（A 的环境未被破坏） |
| 5 | `AUD2-B1-05`：`load_dotenv` 逐行解析，多行 PEM 会被截断 | 低（既有实现） | 未修；生产 `key_source="vault"`，PEM 走 DPAPI 保险箱，不走 `.env` |
| 6 | 域内混用**不等于**密码学配对校验：本修复只保证三要素齐全且同属一个信任域，无法数学证明 PUB/PRIV/API_KEY 属于同一身份 | 中（本质限制） | 已在 `identity_consistent` 的注释与本报告中写明；真正的配对校验只能由服务端在握手时完成 |
| 7 | 写入追踪器是**进程内**对 `type(os.environ).__setitem__` 的记账包装（只记账、行为不变、幂等） | 低 | 已披露；装不上时自动退回"对象身份"判据（Windows）或保守判 `explicit_env`（POSIX） |
| 8 | 依赖 CPython `os.environ` 的内部结构做对象身份判定 | 低 | 非 CPython / `environ` 被整体替换时 `dotenv_origin` 返回 `False`（保守方向，不让 Vault 覆盖环境值） |

---

## 10. 结束声明

- 本轮**只**完成上述 4 项修复 + T17/T20 核对 + 回归与证据生成。
- **未**开始 Batch 4.2；**未**发布；**未**开启实盘；**未**访问 VPS `203.0.113.10`；
  **未**连接生产 API / 创建真实 Session / 连接真实行情 / 发送或撤销订单。
- **未**修改 `.env`、`config/config.yaml`、SDK 源码、数据库、真实交易数据、依赖清单、密钥文件、`tests/helpers.py`。
- **未**删除 / 跳过 / 降低任何既有测试断言；既有测试文件与 4.1.1 快照逐字节一致。
- 证据与复核入口：
  - `US_P1_BATCH4_1_2_TEST_REPORT.md`
  - `US_P1_BATCH4_1_2_FILE_MANIFEST.sha256`
  - `audit_evidence/us_p1_b41_2_security_probes.json`（14/14）
  - `audit_evidence/us_p1_b41_2_test_results.json`（9 组命令 + 退出码）
  - `audit_evidence/us_p1_b41_2_pytest.log`、`audit_evidence/us_p1_b41_2_full.log`
