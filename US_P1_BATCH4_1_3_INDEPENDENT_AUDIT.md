# US-P1 Batch 4.1.3 — 遗留安全风险专项独立审计

| 项目 | 值 |
|---|---|
| 审计对象 | `C:\FosunAI-Trading`（Python 3.13.14，win32，无 `.git`） |
| 审计基准 | Batch 4.1.2 交付物（`US_P1_BATCH4_1_2_*`）及其后现场 |
| 审计时间 | 2026-11-10 14:07 – 14:20（本地） |
| 审计性质 | **独立安全审计**：只读取源码、只写临时探针与证据，**未修改任何源码/测试/配置** |
| 证据总数 | 8 个探针文件、112 个用例（80 通过 / 32 失败） |
| 判定口径 | 每个用例记录 输入 / 预期 / **真实 handler 实际输出** / 是否泄露假凭据 |

> 本文所有"实际输出"均来自真实 `logging` handler 捕获文本或真实 SDK 替身返回值，
> 不是对源码的推断。所有凭据均为 `FAKE-*` 形式的进程内假值。

---

## 0. 结论摘要

| # | 审计组 | 判定 | 关键依据 |
|---|---|---|---|
| 1 | **AUD2-F2-03** 脱敏标记伪造 | **FAIL** | 14 用例 / **7 泄露**；标记是可写普通属性，L3 直接放行 |
| 2 | **AUD2-F2-04** 未登记裸字面量 | **FAIL** | 50 用例 / **15 失败**；其中 **13 条为真实泄露**（按根因归并为 3 个 P1 + 1 个 P2 + 1 个 P3），1 条属 B 类边界，1 条判定口径无效 |
| 3 | **AUD2-B1-05** dotenv 多行 PEM | **FAIL** | 20 用例 / **9 失败**；多行 PEM 被截成 27 字符且静默 |
| 4 | 来源追踪器可靠性 | **PASS** | 21 用例 **全通过**；2 项残余缺口已记录 |
| 5 | Batch 4.1.2 四项修复回归 | **PARTIAL** | F2-01 / B1-01 / B1-02 无回归；**F2-02 信任锚可被 `__module__` 伪造** |
| 6 | A1 / C1 / D1 回归 | **PASS** | 23 个 b4 验收用例 + 独立 D1 探针全通过 |
| 7 | 安全边界 | **PASS** | 终值 `live=false / order=false / paper=true`；未触网、未读真实凭据 |

**缺陷计数：P0 = 0，P1 = 4，P2 = 5，P3 = 6。**

**阻断判断**

* **阻断 Batch 4.2：是**。4 条 P1 全部落在「凭据注入 + 异常日志」主链路上，4.2 一旦接入真实凭据即生效。
* **阻断实盘：是**。`B1-05-01` 使真实 PEM 凭据**必然不可用**（或被误判为"有凭据"）；`F2-04-01` 使 `log.exception()` 明文输出带 `api_key=` 的异常消息。
* 当前实盘风险为零：三个安全开关仍为关闭态，见 §11。

---

## 1. P0：交付物与现场核验

### 1.1 八个交付物（存在性 / 大小 / SHA256）

| 文件 | 存在 | 字节 | SHA256 |
|---|---|---|---|
| `US_P1_BATCH4_1_2_FIX_REPORT.md` | ✅ | 26 596 | `ea48d661d8491166b2d7ec4aeded50c732e84bb253498dff3c188564001b54cf` |
| `US_P1_BATCH4_1_2_TEST_REPORT.md` | ✅ | 15 732 | `2a73fbf5fb12c6e98f89da512a2b74fe4a358552fcc946e97d51b4d70a6e48ae` |
| `US_P1_BATCH4_1_2_FILE_MANIFEST.sha256` | ✅ | 1 437 | `2393c4278c0532631dbd26e187cf3dcbc41402d104c8509252979368d3474a6e` |
| `audit_evidence/us_p1_b41_2_security_probes.json` | ✅ | 4 283 | `caebcec16218ff134072b58f4425ba7b70d7886eecd1d83bab32cea7f9c014e5` |
| `audit_evidence/us_p1_b41_2_test_results.json` | ✅ | 3 498 | `167b48bd31a65cc788b5e3b7a2c815ee84e57d35c52148e354bef87578c5af7e` |
| `audit_evidence/us_p1_b41_2_probes.py` | ✅ | 15 827 | `afb2714b3b13ad311f657f221f0fb6a3ddfac8acb0e7ab08ca1672866baf164b` |
| `US_P1_BATCH4_1_1_SECOND_INDEPENDENT_AUDIT.md` | ✅ | 36 442 | `1feffaf9197838630e660c97b5c3a1775b2b600cc7e5cc86c8e33d4a8e6494ef` |
| `audit_evidence/us_p1_b41_1_second_audit.json` | ✅ | 34 442 | `63a1d293441209005fde513d693d33f0dd0dbdb6be7810ca566c22457a26d5af` |

### 1.2 六项核验结果

| 核验项 | 结果 | 证据 |
|---|---|---|
| ① 报告与 manifest 哈希一致 | **一致** | manifest 14 条逐项比对，`ok=14 / failed=[]`；`sha256sum -c` 14/14 `OK`（exit 0） |
| ② 文件是否存在 | **全部存在** | 上述 8 项 + manifest 内 14 条路径全部命中 |
| ③ 报告引用的证据是否缺失 | **无缺失** | 解析两份报告，引用路径 23 个，全部存在 |
| ④ 源码与 manifest 是否一致 | **一致** | `log_redaction.py` / `config.py` / `runtime.py` / `fosun_adapter.py` 四个源文件 SHA256 与 manifest 逐字节相同 |
| ⑤ 是否存在未报告的修改 | **无（源码/测试/配置/报告）** | manifest mtime（14:03:00）之后共 27 个文件发生变更，**全部位于 `audit_evidence/` 下**（本轮探针自身产物）；`audit_evidence/` 之外为 **0** |
| ⑥ 是否只记录、未覆盖 | **是** | 本轮未写入任何既有报告/证据/备份；备份目录 `backups/us_p1_b412_20261110-133241` 未触碰 |

**过程中的一个非篡改说明**：4.1.2 manifest 采用 Windows CRLF 换行，直接 `sha256sum -c` 会因 `\r` 报"文件不存在"。探针先做 `CRLF→LF` 归一化再校验，14/14 通过。这是校验工具的换行兼容处理，**不是内容变更**。

---

## 2. AUD2-F2-03：脱敏标记伪造 → **FAIL**

### 2.1 机制

`src/ai_trader/log_redaction.py`

* `:455` `_REDACTED_FLAG = "_aitrader_redacted"`
* `:505` `_sanitize_record()` 末尾写入 `record.__dict__[_REDACTED_FLAG] = True`
* `:866-881` `install_format_guard()` 的 L3 兜底：

```python
text = original(self, record)
if getattr(record, _REDACTED_FLAG, False):
    return text        # ← 直接返回，不再脱敏
```

标记是 `LogRecord` 上的**普通可写属性**，任何能拿到 record 的代码都能写、能伪造、能继承。

### 2.2 逐项验证（14 用例，7 泄露）

| ID | 场景 | 实际输出 | 判定 |
|---|---|---|---|
| F2-03-01 | extra 里带同名标记 | `api_key: ***` | ✅ L1 仍脱敏 |
| F2-03-02 | 直写标记 + 走 `Logger.handle` | `api_key: ***` | ✅ L2 仍脱敏 |
| **F2-03-03** | **`install_on_handler(h)` 后自设标记 + `h.emit(record)`** | **`api_key=FAKE-F203-APIKEY-7c1d4a9b`** | ❌ **明文** |
| F2-03-04 | 直驱 `emit`、不设标记 | `api_key: ***` | ✅ L3 兜底生效 |
| **F2-03-05** | **`record._aitrader_redacted=True` + `h.format(record)`** | **`api_key=FAKE-F203-APIKEY-7c1d4a9b`** | ❌ **明文** |
| **F2-03-06** | **已脱敏后改写 `record.msg` 追加新秘密** | **`rotated api_key=FAKE-F203-NEWSECRET-2e5f8a1c`** | ❌ **明文** |
| F2-03-07 | 同一 record 跨两个 handler | 两路均 `***` | ✅ |
| **F2-03-08** | **自定义 `LogRecord.__getattr__` 恒返回 True** | **`api_key=FAKE-F203-APIKEY-7c1d4a9b`** | ❌ **明文** |
| F2-03-09 | 重复脱敏幂等（`count=5` 保留） | `api_key: ***` + `count=5` | ✅ 未误伤白名单字段 |
| F2-03-10 | 8 线程并发、各自秘密 | 无串用 | ✅ |
| F2-03-11 | 直接用 `Formatter().format(record)`（不经 handler） | 明文 | ⚪ 已知边界（本就不在保护链内） |
| **F2-03-12** | **`copy.copy(record)` 继承旧标记 + 新秘密** | **`retry api_key=FAKE-F203-NEWSECRET-2e5f8a1c`** | ❌ **明文** |
| **F2-03-13** | **正常 logger 路径 + 后置 Filter 追加敏感内容** | **`request done enriched_api_key=FAKE-F203-NEWSECRET-2e5f8a1c`** | ❌ **明文** |
| **F2-03-14** | **正常 logger 路径 + 自定义 Formatter 追加内容** | **`request done ctx_api_key=FAKE-F203-NEWSECRET-2e5f8a1c`** | ❌ **明文** |

### 2.3 发现

| 发现 ID | 级别 | 说明 | 位置 |
|---|---|---|---|
| `AUD3-F2-03-01` | **P2** | `_REDACTED_FLAG` 可被业务/第三方代码直接设置，L3 无条件信任 → 绕过最终文本兜底 | `log_redaction.py:455, 505, 876` |
| `AUD3-F2-03-02` | **P2** | **正常路径**下，L1 之后的 Filter / 自定义 Formatter 追加的新内容会带着旧标记跳过 L3 → 新秘密明文。这是最现实的通道（无需伪造标记，只需"追加内容"） | `:876` |
| `AUD3-F2-03-03` | P3 | `copy.copy(record)` / 派生 record 继承标记，改写内容后仍被信任 | `:505, 876` |
| `AUD3-F2-03-04` | P3 | 脱敏后修改 `record.msg`，旧标记让新内容免检 | `:505, 876` |

**是否泄露假凭据**：是，7 个用例输出明文 `FAKE-F203-*`。
**是否影响真实部署**：当前 `src/` 内**没有**任何 `addFilter` 追加敏感内容、没有 `record.msg =` 主动改写、也没有自定义 Formatter 追加字段的调用点（`grep` 仅命中 `market/feed.py:80/:98` 安装 `_REDACTION`）。因此**现阶段无现实泄露通道**，但 API 未阻止后续代码这样做——属"防御未闭环"。
**是否阻断 Batch 4.2**：否（无现实调用点）。
**是否阻断实盘**：否（同上），但与 F2-04-01 叠加时风险升高。

---

## 3. AUD2-F2-04：未登记裸字面量 → **FAIL**

### 3.1 四类语义的基线（必须先区分清楚）

| 类别 | 定义 | 实测行为 | 是否缺陷 |
|---|---|---|---|
| A 已登记秘密 | `register_secret()` 之后 | 任何形态都被替换为 `***` | ✅ 正常 |
| B 未登记裸字面量 | 无键名、无登记、无形态特征 | **明文保留** | ⚪ 设计边界（无键名无法判定语义） |
| C 已知敏感字段 | `api_key=` / `sessionId` / `Authorization` 等 | 常规形态正确打码 | ✅ 正常（但见 3.2 的 4 个破例） |
| D 普通业务文本 | `symbol=AAPL qty=100 price=195.32 side=buy` | **未误伤**（F2-04-20 通过） | ✅ 正常 |

> 明确结论：实现**没有**为了避免误报而屏蔽全部普通日志；D 类样本原样输出。

**用例口径**：三个 F2-04 探针共 50 例，15 例判失败。其中 13 例是真实泄露
（`F2-04-06 / S09 / 06R` 同源、`13 / 13R` 同源、`14 / 14R` 同源、`X08`、`S01`、`S02`、`S03`、`S04`、`S07`），
1 例是 B 类边界（`07R`，traceback 内无键名的裸字面量），1 例判定口径无效
（`09`，handler 用 `%(message)s` 格式化，extra 根本未输出，已由 `X01-X08` 复测替代）。

### 3.2 四个破例（真实缺陷）

#### F2-04-01（P1）外层非敏感键吞并内层敏感键

正则定义（`log_redaction.py:171-175`）：

```python
_VALUE = r"(?:\"[^\"\r\n]{1,4096}\"|'[^'\r\n]{1,4096}'|[^\s,;>]{1,4096})"
_KV_RE = re.compile(rf"(?P<ident>{_IDENT})(?P<sep>\s*[:=]\s*)(?P<value>{_VALUE})")
```

`_VALUE` 的字符类**不排除 `=`**。当文本是 `Outer: inner_key=VALUE` 时，整个
`inner_key=VALUE` 被当成 `Outer` 的 value 匹配掉；回调 `_kv_sub` 判定 `Outer`
不敏感后**原样返回**，而 `re.sub` 从该 match 末尾继续扫描 → 内层键**永远不会被二次扫描**。

实测（真实 handler 输出）：

| 用例 | 输入 | 实际输出 |
|---|---|---|
| F2-04-S01 | `log.error("RuntimeError: api_key=%s", TOK)` | `RuntimeError: api_key=FAKE-F204X-TOKEN-6d1a8b3c` |
| F2-04-S02 | `"status: token=%s"` | `status: token=FAKE-F204X-TOKEN-6d1a8b3c` |
| F2-04-S03 | `"error: password=%s"` | `error: password=FAKE-F204X-TOKEN-6d1a8b3c` |
| F2-04-S04 | `"RuntimeError:  api_key=%s"`（双空格） | 同上，明文 |
| F2-04-S07 | `"data=api_key=%s"` | `data=api_key=FAKE-F204X-TOKEN-6d1a8b3c` |
| **F2-04-S09** | **`log.exception("request failed")`（真实 traceback）** | 源码行变成 `raise RuntimeError(f"api_key: ***`，**尾行 `RuntimeError: api_key=FAKE-...` 明文** |
| F2-04-X08 | `extra={"err": RuntimeError("api_key=...")}` | `v=RuntimeError: api_key=FAKE-...` |
| F2-04-S05/S06 | 无前缀 / `- api_key=` | `api_key: ***`（对照组正常） |

**为什么这是最严重的一条**：`traceback.format_exception` 的最后一行永远是
`异常类名: 异常消息`。只要异常消息里带 `api_key=...`，**默认的 `log.exception()` 就会明文写出**，
不需要任何特殊写法、不需要攻击者。

#### F2-04-02（P1）JSON 值含转义引号 → 整条失配

```python
_JSON_RE = re.compile(
    r"(?i)(?P<pre>\\{0,15}\"|')(?P<ident>[A-Za-z0-9_.\-]{1,64})(?P=pre)"
    r"(?P<sep>\s*:\s*)(?P<q>\\{0,15}\"|')"
    r"(?P<value>(?:[^\"'\\\r\n]|\\[^\"'\r\n]){0,4096})(?P=q)")
```

值分支 `\\[^"'\r\n]` 要求反斜杠后必须跟一个**非引号**字符，因此 `\"` 无法被匹配；
`(?P=q)` 处又要求闭合引号 → 整个 match 失败 → **不替换**。

| 用例 | 输入 | 实际输出 |
|---|---|---|
| F2-04-14R | `payload={"api_key":"FAKE-F204-\"quoted\"-9f1"}` | `payload={"api_key":"FAKE-F204-\"quoted\"-9f1"}` |
| **F2-04-14T** | **`json.dumps({"api_key": 'FAKE-F204-"quoted"-9f1'})`** | **`payload={"api_key": "FAKE-F204-\"quoted\"-9f1"}`** |

`F2-04-14T` 是标准 `json.dumps` 输出（含 `": "` 空格），**整个 API Key 明文**。
源码注释说明这是为了避免"把后续业务字段一起抹掉"的取舍——但代价是 **fail-open**。

> 方法学更正：该用例在自动化 `ok` 位上被标为 PASS，因为比对时用的是未转义的期望串
> `FAKE-F204-"quoted"-9f1`，而输出是转义形态 `FAKE-F204-\"quoted\"-9f1`，`in` 判定失配。
> **人工复核确认这是泄露**，本报告按泄露计。

#### F2-04-03（P1）多行值只打码首行

`_VALUE` 末尾分支 `[^\s,;>]{1,4096}` 中 `\s` 包含 `\n`。

| 用例 | 输入 | 实际输出 |
|---|---|---|
| F2-04-13R | `api_key=FAKE-F204-PART1\nFAKE-F204-PART2` | `api_key: ***\nFAKE-F204-PART2` |

第二行**完整明文**。对多行 PEM、多行证书、含换行的 token 直接失效。

#### F2-04-04（P2）值含分隔符时只打码前缀

`_VALUE` 排除 `\s , ; >`，遇到这些字符即截断，**后缀残留**：

| 输入值 | 实际输出 |
|---|---|
| `FAKE-F204-WITH SPACE-9f1` | `api_key: *** SPACE-9f1` |
| `FAKE-F204-WITH,COMMA-9f1` | `api_key: ***,COMMA-9f1` |
| `FAKE-F204-WITH;SEMI-9f1` | `api_key: ***;SEMI-9f1` |
| `FAKE-F204-WITH>ANGLE-9f1` | `api_key: ***>ANGLE-9f1` |

#### F2-04-05（P3）traceback 源码行被吞掉结构字符

`_VALUE` 允许 `"` 和 `)`，导致 `api_key={TOK}")` 整段被替换，源码行变成
`raise RuntimeError(f"api_key: ***` —— 尾部 `")` 丢失。不影响安全，破坏可读性与日志解析。

### 3.3 通过项（证明实现并非全不可用）

| 用例 | 场景 | 输出 |
|---|---|---|
| F2-04-01 | 已登记值出现在自由文本 | `handshake done with ***` |
| F2-04-03 | 未登记 + `api_key=` | `api_key: ***` |
| F2-04-04 | 嵌套 JSON `sessionId` | `"sessionId":"***"` |
| F2-04-05 | URL `X-Session=` | `X-Session: ***` |
| F2-04-08/15/16/17 | extra 敏感键 / Bearer / PEM / Authorization | 全部 `***` |
| F2-04-18 | 8 层嵌套里的 `api_key` | 仍打码（深度上限内） |
| F2-04-19 | 150 项列表末尾的 `api_key` | 仍打码 |
| F2-04-20 | 普通业务文本 | 原样保留，未误伤 |
| X01/X02/X03 | extra `auth_token` / `apiKey` / `upstream_session_id` | 全部 `***` |
| X06/X07 | extra 嵌套 dict / PEM | 全部 `***` |

**是否影响真实部署**：是。`F2-04-01` 与 `F2-04-02` 在**正常日志写法**下即可触发。
**是否阻断 Batch 4.2**：**是**。
**是否阻断实盘**：**是**。

---

## 4. AUD2-B1-05：dotenv 多行 PEM → **FAIL**

### 4.1 解析实现

`src/ai_trader/config.py:508-540`

```python
for line in env_path.read_text(encoding="utf-8").splitlines():
    ...
    key, _, value = line.partition("=")
    key = key.strip()
    value = value.strip().strip('"').strip("'")
```

**逐行解析**，不可能跨行拼接；`strip('"')` 只削掉首尾引号，不要求成对。

### 4.2 逐字节比对结果（假 PEM：573 字符 / 10 行）

| ID | 场景 | env 值长度 | 与期望比对 |
|---|---|---|---|
| B1-05-03 | 标准多行 PEM + 双引号 | **27** | 长度不同（27 vs 573），前 27 字符一致 |
| B1-05-04 | 标准多行 PEM 无引号 | **27** | 同上 |
| B1-05-06 | 多行 + CRLF | **27** | 同上 |
| B1-05-07 | 多行 + 文件末尾无换行 | **27** | 同上 |
| B1-05-10 | 多行中夹空行与注释 | **27** | 同上 |
| B1-05-13 | 单引号包裹 | **64** | 只剩第一行 base64 |
| B1-05-05 | `\n` 转义单行 | **582** | 首个差异 @27：`env='\\'` vs `expect='\n'` |
| B1-05-09 | 缺失 END 标记 | **27** | 与 B1-05-03 **sha 完全相同** |
| B1-05-18 | 键名带 UTF-8 BOM | — | 实际键名为 `\ufeffAUD3B105_PEM`，目标键未设置 |

**`-----BEGIN PRIVATE KEY-----`（27 字符）就是被写进环境变量的全部内容。**

关键点：**B1-05-03（合法完整 PEM）与 B1-05-09（缺 END 的畸形 PEM）得到完全相同的值**
（`sha=3021d90eb9437b2d`）。解析器**无法区分**"完整凭据"与"残缺凭据"，且
**不抛异常、不告警、不 fail-closed**（20 个用例全部 `exception=None`）。

### 4.3 通过项

`B1-05-11`（`abc==def`，只按首个 `=` 分割）、`B1-05-12`（无等号行跳过）、
`B1-05-14`（未闭合引号静默接受）、`B1-05-15`（值内空格保留）、`B1-05-16/17`（空文件/仅注释返回 False）、
`B1-05-19`（`#` 不截断，优于 python-dotenv）、`B1-05-20`（setdefault 语义，不覆盖进程值）。

### 4.4 发现

| 发现 ID | 级别 | 说明 | 位置 |
|---|---|---|---|
| `AUD3-B1-05-01` | **P1** | 多行 PEM 被截断为 27 字符 BEGIN 头，其余静默丢弃；**与畸形输入不可区分**，无任何告警 | `config.py:508-540` |
| `AUD3-B1-05-02` | P2 | `\n` 转义不解码，值保留字面反斜杠 n（与 python-dotenv 不兼容），长度 582 vs 573 | 同上 |
| `AUD3-B1-05-03` | P2 | UTF-8 BOM 污染首行键名，该键**完全未被设置**（静默配置丢失） | 同上 |
| `AUD3-B1-05-04` | P3 | 解析失败一律静默，无 fail-closed、无 `dotenv_source_diagnostics` 计数 | 同上 |

### 4.5 现实影响（为什么是 P1）

`.env.example` 明确包含两个 PEM 字段：

```
FSOPENAPI_SERVER_PUBLIC_KEY
FSOPENAPI_CLIENT_PRIVATE_KEY
```

全仓库**没有任何 PEM 结构校验**（`grep` 仅命中 `log_redaction.py:162` 的脱敏正则与
`secrets.py:82 redact_pem` 的展示脱敏）。`SecretVault.inject_into_environ()`
会把截断值直接写进 `os.environ` 交给 SDK。

因此：运维按最自然的多行写法填写 PEM → 进程里只有 `-----BEGIN PRIVATE KEY-----`
→ SDK 必然握手失败；若任何一层只看 `BEGIN` 前缀，则会误判为"已配置凭据"。

**是否泄露假凭据**：否（这是可用性/正确性缺陷，不是泄露）。
**是否影响真实部署**：**是，必然**。
**是否阻断 Batch 4.2**：**是**。
**是否阻断实盘**：**是**。

---

## 5. 来源追踪器专项审计 → **PASS**

`config.py:350-500`。判定链：键名登记 → 外部写入日志 → 值指纹 → （无写入日志时）对象身份。

21 个用例**全部符合预期**：

| 用例 | 场景 | 实测 |
|---|---|---|
| SRC-01 | 基线 | `origin=True` |
| SRC-02 | 改成不同值 | `False`（指纹不符） |
| **SRC-03** | **改成完全相同的值（AUD2-B1-01 核心）** | **`False`** ← 修复有效 |
| SRC-04 | 写入另一个 `_DotenvValue` 同值对象 | `False`（写入动作被记账） |
| SRC-05 | `os.environ.update({k: 同值})` | `False`（走 `__setitem__`） |
| SRC-06 | `os.environ \|= {k: 同值}` | `False` |
| SRC-07 | `os.putenv(k, 同值)` | `True`（`os.environ` 视图未变，来源确未变） |
| SRC-08 | `pop` 后重写同值 | `False` |
| SRC-09 | `pop` 后不重设 | `False` |
| SRC-10 | `clear()` 后重写同值 | `False` |
| SRC-11 | 载入前已有同名变量 | `False`，`recorded=False` |
| SRC-12 | 直接改 `os.environ._data`（绕过 `__setitem__`） | `False`（指纹兜住） |
| SRC-13 | `internal_env_write()` 包裹的同值写 | `True`（见残余 1） |
| SRC-14 | 元数据丢失 | `False`（fail-closed） |
| SRC-15 | 8 线程并发写同值 | `False`，无异常 |
| SRC-16 | 先后载入两个 `.env`（同键） | `True`（setdefault，仍属第一个文件） |
| SRC-17 | 同文件重复载入 | `True`（幂等） |
| SRC-18 | 包装器不可用（Windows 对象身份降级） | `True`（`raw is marked`） |
| SRC-19 | 包装器不可用 + POSIX 字节存储（模拟） | `False`（fail-closed） |
| SRC-20 | 指纹被清空（早期记录形态） | `False` |
| SRC-21 | 两线程同时 `load_dotenv` 不同文件 | `True`，两键均在、无异常 |

**残余缺口（已记录，不晋升为缺陷）**

| 发现 ID | 级别 | 说明 |
|---|---|---|
| `AUD3-SRC-01` | P3 | `internal_env_write()` 是公开 API，任何代码都能用它把"同值外部改写"洗白成 dotenv（SRC-13 实测 `origin=True`）。当前只有 `load_dotenv` 与凭据注册表使用，属可接受的特权通道；但它把 AUD2-B1-01 的保护降级为"约定"而非"机制"。 |
| 观察 | — | `os.putenv` 只改 C 层环境，`os.environ` 视图不变（SRC-07），因此若有人用它改值，本进程看到的仍是旧值。当前代码只读 `os.environ`，无实际影响。 |

---

## 6. Batch 4.1.2 四项修复回归 → **PARTIAL**

### 6.1 独立探针（不复用项目测试判定）

| 用例 | 场景 | 实测 | 判定 |
|---|---|---|---|
| RG-F2-01-A | filters 冻结 + handle 只读 + `setLevel` 空操作 | `install=False, mode=unsecured, unsecured=1, blocked=0, healthy=false` | ✅ **无回归** |
| RG-F2-01-B | `setLevel` 直接抛异常 | `install=False, unsecured=1, blocked=0, healthy=false` | ✅ |
| RG-F2-01-C | 对照组：`setLevel` 正常 | `install=True, blocked=1, healthy=true` | ✅ |
| RG-F2-02-A | `Logger.handle` 被换成 no-op（`__module__="__main__"`） | 自愈成功；`trusted=true`；`foreign_hooks` 记录 `replaced_by_module="__main__"`；恢复后输出 `api_key: ***` | ✅ |
| **RG-F2-02-B** | **同一个 no-op，但伪造 `__module__="logging"`** | **`trusted=true`、`foreign=null`、`log_silenced=true`、`pristine_poisoned=true`、`healthy=true`** | ❌ **新发现** |
| RG-F2-02-C | 重复 `ensure_hooks()` | 两次 `repaired` 全 False，`healthy=true` | ✅ |
| RG-D1-A | 正常关闭判定 | `1000→True / 1011→False / 无 code 且非 closing→False / closing→True` | ✅ |

### 6.2 新发现

**`AUD3-F2-02-01`（P3）`__module__` 伪造骗过信任锚并永久污染 `_PRESTINE`**

`log_redaction.py:578-582`

```python
def _is_stdlib_hook(fn):
    mod = getattr(fn, "__module__", "") or ""
    return mod == "logging" or mod.startswith("logging.")
```

`__module__` 是普通可写属性。`_wrap_method`（`:768`）在 `_is_stdlib_hook(current)` 为真时：

```python
if _is_stdlib_hook(current):
    trusted = current
    _PRESTINE[key] = current      # ← 把攻击者的函数写成"可信原实现"
```

实测后果：一个伪造了 `__module__="logging"` 的 no-op 被当成 stdlib → **日志被完全静默**
（`log_silenced=true`，`api_key` 记录连空行都没有）→ `redaction_health()` 仍报
`healthy=true`、`trusted=true`、`foreign_hooks` 为空。而且 `_PRESTINE` 被替换成该函数
（`pristine_is_evil=true`），**后续所有自动修复都会链回攻击者的实现**，直到进程重启。

这与 F2-03 属同一类问题：**可信判据建立在一个可写属性上**。
项目自带的 15 个 `test_log_redaction_b412.py` 用例均未覆盖 `__module__` 伪造。

### 6.3 项目测试回归（只读执行）

```
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 python -B -m pytest ... -q -p no:cacheprovider -o addopts=""
```

| 测试文件 | 结果 |
|---|---|
| `test_log_redaction_b412.py` + `test_credential_identity_b412.py` | **34 passed**（1.48s） |
| `test_prod_conn_repair_b4.py` | **23 passed**（3.22s） |
| `test_log_redaction_b41.py` + `b411` + `credential_identity_b411` + `market_push_guard_b411` | **50 passed**（2.77s） |

`B1-01` 对应 `test_b101_*` 7 例全通过；`B1-02` 对应 `test_b102_*` 9 例全通过
（含 `test_b102_cross_domain_*_fails_closed`，跨信任域仍拒绝）。

---

## 7. A1 / C1 / D1 回归 → **PASS**

| 项 | 验证方式 | 结果 |
|---|---|---|
| **A1** 五个 SDK 入口 / `build_feed_auth_provider` 门禁 / `MarketPushAPI` 类级构造 | `test_prod_conn_repair_b4.py` 23 例（真实本机回环 + 假 SDK，不用 mock） | 全通过 |
| **C1** 仅带 requestid 的回执 → 必须 `ACK_UNVERIFIED` | 同上 | 全通过 |
| **D1** 正常关闭不计入 `errors` | 同上 + 独立探针 `RG-D1-A` | 全通过 |

独立探针确认 `MarketFeed._is_normal_close` 的判定依据是 **close code 与关闭意图**，
不是异常类名；`1011`（服务端错误）不被当作正常关闭；`_stats["normal_closes"]` 存在。

未连接任何真实 SDK / 真实行情服务；`test_prod_conn_repair_b4.py` 仅
`serve(..., "127.0.0.1", 0)` 与 `ws://127.0.0.1:1/x`（必然拒绝连接）。

---

## 8. 发现总表

| 发现 ID | 级别 | 所属 | 一句话 | 文件:行 |
|---|---|---|---|---|
| `AUD3-F2-04-01` | **P1** | F2-04 | `Outer: inner_key=value` 被外层键吞并，内层永不打码；**traceback 尾行即此形态** | `log_redaction.py:171,173-175` |
| `AUD3-F2-04-02` | **P1** | F2-04 | JSON 值含 `\"` 时整条失配，标准 `json.dumps` 输出明文 | `:179-184` |
| `AUD3-F2-04-03` | **P1** | F2-04 | 多行值只打码首行，后续行明文 | `:171` |
| `AUD3-B1-05-01` | **P1** | B1-05 | 多行 PEM 截断为 27 字符，与畸形输入不可区分，静默 | `config.py:508-540` |
| `AUD3-F2-03-01` | P2 | F2-03 | `_REDACTED_FLAG` 可写 → L3 放行明文 | `log_redaction.py:455,505,876` |
| `AUD3-F2-03-02` | P2 | F2-03 | 正常路径下后置 Filter / Formatter 追加内容 → 新秘密明文 | `:876` |
| `AUD3-F2-04-04` | P2 | F2-04 | 值含空格/逗号/分号/`>` 只打码前缀，后缀残留 | `:171` |
| `AUD3-B1-05-02` | P2 | B1-05 | `\n` 转义不解码，内容与预期不同 | `config.py:508-540` |
| `AUD3-B1-05-03` | P2 | B1-05 | UTF-8 BOM 污染键名，配置静默丢失 | 同上 |
| `AUD3-F2-03-03` | P3 | F2-03 | `copy.copy` 继承旧标记 | `:505,876` |
| `AUD3-F2-03-04` | P3 | F2-03 | 脱敏后改 `msg` 沿用旧标记 | `:505,876` |
| `AUD3-F2-04-05` | P3 | F2-04 | traceback 源码行被吞掉 `")`，输出畸形 | `:171` |
| `AUD3-B1-05-04` | P3 | B1-05 | 解析失败静默，无 fail-closed | `config.py:508-540` |
| `AUD3-SRC-01` | P3 | 追踪器 | `internal_env_write()` 可洗白同值外部改写 | `config.py:333,338` |
| `AUD3-F2-02-01` | P3 | 4.1.2 回归 | 伪造 `__module__="logging"` 污染 `_PRESTINE`，日志静默但 health 仍 healthy | `log_redaction.py:578,768,800` |

---

## 9. 已知边界与无法验证项

1. **F2-03-11**：直接使用 `logging.Formatter().format(record)` 而不经任何 handler，本就不在保护链内（L1/L2/L3 都挂在 logger/handler 上）。记为边界，不计缺陷。
2. **B 类裸字面量**（无键名、未登记）：任何基于文本的脱敏都无法判定语义，明文保留属设计取舍。本报告不将其计为缺陷，但也不建议用"全量屏蔽"来掩盖——那会摧毁日志可用性。
3. **`os.putenv` 与 `os.environ` 视图不一致**：只做了本进程观测，未验证子进程继承行为。
4. **POSIX 降级路径（`SRC-19`）**：本机为 Windows，采用"把 `_raw_environ_value` 替换为 bytes"的方式模拟，属逻辑级验证，非真实 POSIX 运行。
5. **`load_dotenv` 的第三方格式兼容性**：只测了本项目实现，未与 python-dotenv 做逐项对照（除 `\n` 解码与 `#` 截断两处已注明差异）。
6. **未执行**：任何真实 SDK 连接、真实行情、真实交易 Session、VPS 访问、真实 `.env` 读取。
7. **`internal_env_write` 的真实调用面**：只做了 `grep` 级确认（`load_dotenv` + 凭据注册表），未做运行时插桩统计。

---

## 10. 建议修复方向（本轮**不实施**）

> 按用户要求，仅评估方向，不改代码。

**F2-03（标记不可伪造）**

* 放弃"记录上的可写属性"作为跳过判据。可选：
  1. 进程内登记表：`WeakSet` 记录已脱敏的 record 对象 + 单调递增 `seq`，L3 只认登记表；
  2. 指纹绑定：登记 `(id(record), sha256(msg+args))`，L3 发现内容变了就**重新脱敏**而不是跳过；
  3. 最保守：L3 永远重新脱敏，用 `redact_text` 的幂等性换性能（实测 8 线程无串用，成本可测）。
* 无论选哪种，都必须让"内容变了"优先于"曾经脱敏过"。

**F2-04（正则 fail-closed）**

* `_VALUE` 需排除 `=`（或改成"值内不允许再出现 `ident=`"），并对 `Outer: inner=` 做二次扫描；
* `_JSON_RE` 的值分支必须允许 `\"`（即 `\\.`），失败时**回退为整段打码**而非原样返回；
* `\s` 改为 `[ \t]`，让 `\n` 进入值内（多行值整段打码）；
* 分隔符截断后应把"剩余部分"也纳入打码范围，避免后缀残留。

**B1-05（多行 PEM）**

* 解析器需支持：跨行引号值（读到闭合引号为止）、`\n` 转义解码、`\r\n` 归一化、BOM 剥离；
* **结构校验 fail-closed**：`BEGIN` 有而 `END` 无 → 视为解析失败并计数/告警，绝不写入半截值；
* 对 `*PRIVATE_KEY*` / `*PUBLIC_KEY*` 这类键，额外的"长度与行数下限"检查成本极低、收益很高。

**F2-02（信任锚）**

* 不要用 `__module__` 判断可信性。改用导入期保存的函数对象身份（`is` 比较）或
  `id()` 白名单；`_PRESTINE` 一旦抓到可信实现就**永不覆盖**。

---

## 11. 安全开关终值（审计结束时实测）

```
live_trading              = false
order_execution_enabled   = false
paper_trading             = true
kill_switch               = false
can_connect_broker        = [false, "PAPER_TRADING=true，真实券商连接被禁用"]
can_send_real_order       = [false, "PAPER_TRADING=true，真实券商连接被禁用"]
fosun_sdk_type            = "ops"
fosun_base_url            = ""
has_git                   = false
```

读取方式：`load_config(use_dotenv=False)` + `SafetyGate`，**不读真实 `.env`**。
审计全程未修改任何开关，未创建真实 Session，未发送任何订单/撤单。

---

## 12. 本轮交付物（SHA256 见 `audit_evidence/us_p1_b41_3_manifest.sha256`）

| 文件 | 内容 |
|---|---|
| `US_P1_BATCH4_1_3_INDEPENDENT_AUDIT.md` | 本报告 |
| `audit_evidence/us_p1_b41_3_audit.json` | 结构化结论（7 组判定 + 15 条发现） |
| `audit_evidence/us_p1_b41_3_probe_results.json` | 112 个用例的完整 input/expected/actual |
| `audit_evidence/us_p1_b41_3_manifest.sha256` | 本轮全部交付物与探针的哈希清单 |
| `audit_evidence/us_p1_b41_3_probes/` | 8 个探针源码 + 各自的 `.out.json` / `.err.log` |

探针执行口径（可复现）：

```
cd audit_evidence/us_p1_b41_3_probes
env -u PYTHONPATH PYTHONDONTWRITEBYTECODE=1 C:\FosunAI-Trading\.venv\Scripts\python.exe -B <probe>.py
```

---

## 13. 审计声明

* 本轮**未修改**任何源代码、测试文件、配置文件；**未修改** `.env`、`config/config.yaml`、SDK、数据库、交易数据。
* **未修改**任何既有报告、证据、备份；**未执行** `git reset/checkout/clean`（仓库无 `.git`）。
* **未开启**实盘、**未改变**任何安全开关、**未访问** VPS `203.0.113.10`。
* 所有探针使用 `FAKE-*` 假凭据与临时目录，未读取、未打印、未复制任何真实密钥。
* 失败用例全部保留原始输出，未删除、未修改后宣布通过；判定口径的两处偏差（`F2-04-14T` 的转义比对、`F2-04-09/08` 的 extra 未输出）已在正文显式更正。
* 审计到此处停止：不修复、不进入 Batch 4.2、不发布、不开启实盘。
