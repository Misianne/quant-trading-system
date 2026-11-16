# US Real LLM Evidence — V2 三轴模型独立逻辑审查（只读）

| 项 | 值 |
|---|---|
| 审查对象 | `US_REAL_LLM_EVIDENCE_DECISIONS_PENDING.md`（v2，`5ecd4b7c…`） |
| 参照 | `US_REAL_LLM_EVIDENCE_REMEDIATION_PLAN.md`（`3d569f16…`，仅参照，未改） |
| 性质 | **只读逻辑审查**。未修改任何文件；未读 SQLite；未建库/建表；未执行 SQL |
| 方法 | 逐条提取规则 → 建状态组合真值表 → 最小反例 → SQL/CHECK 语义推演（纯逻辑，无落库） |
| **结论** | **不建议按当前 v2 批准**。不变量集内部存在 **5 处硬矛盾**（部分状态无合法取值）+ **6 条 CHECK 可绕过** + **1 处语义未定义** |

---

## 0. 结论摘要

| 编号 | 问题 | 严重度 | 是否阻断确认 |
|---|---|---|---|
| **M-1** | `provenance=real_llm_unverified ⇒ transport_path=proxy_unverified`（L64）过窄：把"传输未验证"与"整链证据不全"混为一谈，导致 3 类真实场景**无合法状态** | 高 | 是 |
| **M-2** | `transport_path=proxy_unverified ⇒ endpoint_class ∈ {remote, unknown}`（L62）排除**回环代理**——恰是本机 `127.0.0.1:54481` 的真实情况 | 高 | 是 |
| **M-3** | `endpoint_class` 未定义是"目标地址"还是"实际对端"；代理场景下两者不同 | 高 | 是 |
| **M-4** | 本地**真实**模型（如本机 ollama）无对应 `provenance` 值，只能被标成 `stub_transport`（把真实当替身） | 中 | 是 |
| **M-5** | `upstream_id` 允许 `x-request-id` / `cf-ray` 顶替"上游 response_id"；这两个都不是模型供应商产出的 ID | 高 | 是 |
| **M-6** | 全部不变量未绑定 `NOT NULL` / 枚举 CHECK，SQLite 三值逻辑下有 6 条绕过路径 | 高 | 是 |
| **M-7** | readiness "11 项"只有数字、**从未逐条列出**（L74/L146），不可核验 | 中 | 是 |
| **M-8** | readiness 存在 `None == None` 型绕过（`evidence_id` 两侧同为 NULL 时判等） | 高 | 是 |

**唯一无争议的部分**：三轴的划分方向是对的（地址性质 / 连接路径 / 结论），比 v1 的合并枚举好；问题出在**边界值定义**和**约束写法**，不是模型方向。

---

## 1. 三轴语义审查

### 1.1 `endpoint_class` — 语义未定义（M-3）

确认单只给出取值（L45 表），**没有说明它描述的是"URL 里的目标主机"还是"实际 TCP 对端"**。

这不是措辞问题，而是会直接改变判定结果：

| 场景 | 目标主机类别 | 实际对端类别 | 该填哪个？ |
|---|---|---|---|
| 直连 `wb2.example.com`（CF） | remote | remote（CDN 边缘） | 两者一致，无歧义 |
| 经本机代理 `127.0.0.1:54481` 访问 `wb2.example.com` | remote | **loopback** | **歧义** |
| 经公司代理 `10.1.2.3` 访问公网 | remote | private | **歧义** |

按现文 I5（L62），代理路径要求 `endpoint_class ∈ {remote, unknown}`。若按"实际对端"定义，本机回环代理应判 `loopback` → **违反 I5 → 该场景无合法状态**（M-2 的根因）。

**推荐**：明确写死
- `endpoint_class` = **base_url 目标主机**解析后的地址类别（与"你要求访问谁"绑定）；
- 另设 `peer_class`（实际对端类别，可空）与 `proxy_class`（代理地址类别，可空）作为**观测字段**，但不参与 I1–I5 的约束。
这样"目标 remote + 对端 loopback"可被如实记录，而不是二选一。

### 1.2 `transport_path` — 与 `endpoint_class` 部分冗余，但可接受

5 个取值里，`local` / `private` 与 `endpoint_class` 的 `loopback` / `private` 是**一一对应**的（I1/I2）。也就是说这两组值不携带新信息，只是把"地址性质"复述了一遍；真正有新意的是 `direct_verified` / `proxy_unverified` / `unknown`（携带"是否验证过对端"）。

后果：约束必须成对写，否则 `endpoint=loopback + transport=private` 这类**语义自相矛盾但 CHECK 未必拦住**的组合会出现。
**推荐**：保留 5 值（用户已定），但在文档里加一句"`local`/`private` 仅用于无法做对端验证的场景；能验证时必须用 `direct_verified`"。

### 1.3 `provenance` — 承担了三件互不相同的事（M-1 的深层原因）

现取值：`real_llm` / `real_llm_unverified` / `stub_transport` / `rejected` / `unavailable` / `unknown`。拆开看：

| 值 | 说的是"来源" | 说的是"结果" | 说的是"可信度" |
|---|---|---|---|
| `real_llm` | ✅ 真实模型 | — | ✅ 已验证 |
| `real_llm_unverified` | ✅ 真实模型 | — | ⚠️ 未验证 |
| `stub_transport` | ✅ 替身 | — | — |
| `rejected` | ✖ 未知 | ✅ 失败 | — |
| `unavailable` | ✅ 无模型 | ✅ 未调用 | — |
| `unknown` | ✖ | ✖ | ✖ 兜底 |

`real_llm_unverified` 这一个值同时要表达"来源=真实模型"和"某处证据不全"，而**"哪处不全"没有载体**。于是 I7 只能粗暴地把它钉到"代理路径"上——这就是 M-1。

**最小修法（保持三轴）**：给 `real_llm_unverified` 配一个必填的 `unverified_reasons`（JSON 数组，枚举：`missing_upstream_id` / `missing_usage` / `envelope_digest_failed` / `transport_unverified` / `status_not_200` / `model_mismatch`），并**删除 I7**。
**更干净的修法（需改 D1 决策）**：把 `provenance` 拆成 `source_kind(llm/local/stub/none)` + `evidence_status(complete/partial/failed/none)`，即四轴。本次审查**不建议**在 D1 已确认的情况下临时加轴，先按最小修法。

---

## 2. 合法 / 非法状态组合真值表

### 2.1 (E × T) 合法矩阵（依 I1–I5）

| T \ E | remote | loopback | private | unknown |
|---|---|---|---|---|
| `direct_verified` | ✅ I3 | ❌ | ❌ | ❌ |
| `proxy_unverified` | ✅ I5 | ❌ **（M-2）** | ❌ | ✅ I5 |
| `local` | ❌ | ✅ I1 | ❌ | ❌ |
| `private` | ❌ | ❌ | ✅ I2 | ❌ |
| `unknown` | ❌ | ❌ | ❌ | ✅ I4 |

- 合法组合 **5 个**，非法 **15 个**（共 20）。
- 标红那格（loopback + proxy）就是本机上次事故的状态，现文**判为非法**。

### 2.2 `provenance` 可达性矩阵（依 I6/I7）

| (E,T) | real_llm | real_llm_unverified | stub_transport | rejected | unavailable | unknown |
|---|---|---|---|---|---|---|
| (remote, direct_verified) | ✅（需 upstream_id 非空） | ❌ **I7** | — | ✅ | ✅ | ✅ |
| (remote, proxy_unverified) | ❌ I6 | ✅ | — | ✅ | ✅ | ✅ |
| (loopback, local) | ❌ I6 | ❌ I7 | ✅ | ✅ | ✅ | ✅ |
| (private, private) | ❌ | ❌ | — | ✅ | ✅ | ✅ |
| (unknown, unknown) | ❌ | ❌ | — | ✅ | ✅ | ✅ |

**关键观察**：`real_llm_unverified` **只有一格可达**（代理）。"直连但证据不全"这一大类全部挤进 `unknown`——而 `unknown` 同时是"完全不知道发生了什么"的兜底值，两者无法区分。

### 2.3 六场景映射（问题 1 的正面回答）

| # | 场景 | E | T | 应有的 P | 现文是否合法 | 说明 |
|---|---|---|---|---|---|---|
| S1 | 直连公网成功，缺 `response_id` | remote | direct_verified | `real_llm_unverified` + reason=`missing_upstream_id` | ❌ **无合法值** | `real_llm` 被 I6 挡（upstream_id 空）；`real_llm_unverified` 被 I7 挡（非代理）；只剩 `unknown`，丢信息 |
| S2 | 直连成功，HTTP ≠ 200 | remote | direct_verified | `rejected` | ✅ | 无约束，正确 |
| S3 | 直连成功，信封摘要缺失/校验失败 | remote | direct_verified | `real_llm_unverified` + reason=`envelope_digest_failed` | ❌ **无合法值** | 同 S1 |
| S4 | TLS/IP 通过，但 usage 缺失等 | remote | direct_verified | `real_llm_unverified` + reason=`missing_usage` | ❌ **无合法值** | 同 S1 |
| S5 | 显式代理成功，无法验证代理上游对端；**代理在回环** | 目标 remote / 对端 loopback | proxy_unverified | `real_llm_unverified` + reason=`transport_unverified` | ⚠️ 半合法 | 填 `endpoint=remote` 合法但**掩盖了对端是回环**；填 `loopback` 则违反 I5 |
| S6 | 本地**真实**模型（ollama 等） | loopback | local | 应有一个"本地真实模型"值 | ❌ **无此值** | 只能填 `stub_transport` → 把真实当替身（M-4） |

**结论**：6 个场景里 **3 个无任何合法状态**（S1/S3/S4），**1 个语义缺失**（S6），**1 个只能靠填错值规避**（S5）。这不是文档瑕疵，是不变量集不完备。

---

## 3. 7 条跨轴不变量逐条审查

| # | 原文（L58–64） | 判定 | 理由 / 建议 |
|---|---|---|---|
| I1 | `transport_path=local ⇒ endpoint_class=loopback` | ✅ 保留 | 语义正确 |
| I2 | `transport_path=private ⇒ endpoint_class=private` | ✅ 保留 | 同上 |
| I3 | `transport_path=direct_verified ⇒ endpoint_class=remote` | ✅ 保留 | 与"直连且对端全局"一致 |
| I4 | `transport_path=unknown ⇒ endpoint_class=unknown` | ⚠️ **过强** | DNS 解析到 private 但钉死失败时，E 可能是 private 而 T=unknown，被 I4 判非法。建议改为 `transport_path=unknown ⇒ endpoint_class ∈ {unknown, private, loopback}`（"T 未知"不代表"E 未知"）。若不改，需明确"E 只由解析结果决定，解析失败时 E 必须填 unknown"，并把该规则写进文档，避免实现分叉 |
| I5 | `transport_path=proxy_unverified ⇒ endpoint_class ∈ {remote, unknown}` | ❌ **改** | 排除回环/私网代理（本机真实情况），见 M-2。建议 `∈ {remote, loopback, private, unknown}` |
| I6 | `provenance=real_llm ⇒ T=direct_verified ∧ E=remote ∧ upstream_id≠""` | ⚠️ **不完整** | 缺 `http_status=200`、`envelope_bytes>0`、`envelope_sha256` 非空。当前可写出"status=500 但 provenance=real_llm"的行（见 C6）。建议补全这 3 项 |
| I7 | `provenance=real_llm_unverified ⇒ transport_path=proxy_unverified` | ❌ **删除** | 制造 M-1，3 场景无解。改为"`P=real_llm_unverified` 必须带非空 `unverified_reasons`"，不约束 T |

---

## 4. `upstream_id` 来源与缺失处理审查

### 4.1 字段映射不是一对一（M-5）

确认单 L88–89：

> `body.id` → `header.x-request-id` → `header.cf-ray`；三者皆无 → `upstream_id=""`

| 候选 | 谁产出 | 能否证明"模型被调用" | 结论 |
|---|---|---|---|
| `body.id` | 模型供应商（或中转上游） | ✅ 是上游响应信封的一部分 | **唯一合格** |
| `x-request-id` | **中转/网关自己**（用户自建） | ❌ 只是"网关收到过请求" | 不能顶替 |
| `cf-ray` | Cloudflare 边缘 | ❌ 只证明"经过 CF" | 不能顶替 |

关键点：`x-request-id` 由**中转**签发——而中转正是我们要防的"自证"环节。允许它满足 D2 的一票否决，等于把"上游 response_id"降级为"我方网关 ID"，与 D2 的初衷（"不得用本地生成 ID 冒充上游 ID"）自相矛盾。

**推荐**
- `upstream_id` **只允许** `body.id`；
- `x-request-id` / `cf-ray` 写入独立的 `network_request_id` + `network_request_id_source`，**不参与** readiness；
- 若中转确实不返回 `body.id`，则 readiness 恒为 false——这是**应有的代价**，应写进文档而不是用次级 ID 绕过。

### 4.2 空值 / 异常类型处理不足

| 情形 | 现文 | 问题 | 建议 |
|---|---|---|---|
| 字段不存在 | `""` | 与"存在但为空串"混同 | 用 `NULL` 表示缺失，`""` 视为非法 |
| 值为 `null` | 未规定 | `json.loads` 得 `None`，若直接写入会变成 SQL NULL，而 `!= ''` 对 NULL 为 NULL → **通过 CHECK** | 显式转 NULL 并记 `upstream_id_type=null` |
| 值为非字符串（数字/对象） | 未规定 | `str(123)` 会伪造出一个"ID" | 只接受 `str` 且长度∈[1,200] 且匹配 `^[A-Za-z0-9_\-:.]+$`；否则拒收 |
| 空白串 `"   "` | 未规定 | `strip()` 后为空但仍非 `""` | 入库前 `strip()`，为空则 NULL |
| 本地 ID 混入 | L91 禁止写入 + L92 断言不等于 `decision_id`/`signal_id` | **太弱**：换一个本地生成的 UUID 就绕过了 | 唯一防线应是"单一写入路径（只从响应解析结果取）+ 格式校验；`upstream_id` 列无任何本地写入口" |
| 历史数据 | 未提 | 老行无该列 → NULL；若 readiness 用 `!= ''`，NULL **通过**（见 C2） | readiness 必须 `IS NOT NULL AND length>0` |

---

## 5. 数据库 CHECK 约束审查

### 5.1 现文没有给出任何 SQL

L55 只写"实现时用 CHECK 或写入前断言强制"，7 条全部是散文式 `⇒`。因此**无法逐条核对 SQL**，只能核对"若按最直白的方式翻译会怎样"。这本身是 M-6 的一部分：**约束形式未定，不可验收**。

### 5.2 SQLite 三值逻辑下的 6 条绕过路径

SQL 语义前提：**CHECK 只在表达式求值为 FALSE(0) 时拒绝；求值为 NULL 时视为通过。**

| 编号 | 反例（可写入的行） | 为什么能过 | 需要的修补 |
|---|---|---|---|
| **C1** | `transport_path=NULL, provenance='real_llm', upstream_id='x', http_status=200` | I6 = `NOT(P='real_llm') OR (NULL='direct_verified' AND ...)` → `FALSE OR NULL` = **NULL** → 通过 | 三轴全部 `NOT NULL` + `CHECK (T IN (...))` |
| **C2** | `upstream_id=NULL, provenance='real_llm', T='direct_verified', E='remote'` | `upstream_id <> ''` 对 NULL 求值为 **NULL** → 通过 | 改 `upstream_id IS NOT NULL AND length(upstream_id)>0` |
| **C3** | `endpoint_class=NULL, transport_path='direct_verified'` | I3 → `FALSE OR NULL` = NULL → 通过 | `endpoint_class NOT NULL` |
| **C4** | `transport_path=''`（空串，非枚举值） | 所有 `T='xxx'` 比较均为 FALSE，I1–I5 全部"不适用"→ 通过 | `CHECK (T IN ('direct_verified',...))` |
| **C5** | `provenance='real_llm', http_status=500` | I6 **不含** status/envelope 条件 → 通过 | I6 补 `http_status=200 AND envelope_bytes>0 AND envelope_sha256 IS NOT NULL` |
| **C6** | `provenance='real_llm_unverified', transport_path='direct_verified'` | I7 应拒绝——但只要 I7 未落成 CHECK（现文未给 SQL）就会通过 | 删除 I7、改用 `unverified_reasons` 非空约束 |

> C1/C2/C3 是同一类：**NULL 让蕴含式恒为 NULL**。这不是 SQLite 的 bug，是"用 `!= ''` 而不是 `IS NOT NULL`"的经典坑。
> 建议在实施阶段用一条内存 SQLite 断言验证（本轮禁止建库，故仅作语义推演，标记为**待实测**）。

### 5.3 推荐的最小可校验约束集（供修订参考，不构成本轮实施）

```sql
endpoint_class TEXT NOT NULL CHECK (endpoint_class IN ('remote','loopback','private','unknown')),
transport_path TEXT NOT NULL CHECK (transport_path IN ('direct_verified','proxy_unverified','local','private','unknown')),
provenance     TEXT NOT NULL CHECK (provenance IN ('real_llm','real_llm_unverified','stub_transport','local_llm','rejected','unavailable','unknown')),

CHECK (transport_path <> 'local'           OR endpoint_class = 'loopback'),
CHECK (transport_path <> 'private'         OR endpoint_class = 'private'),
CHECK (transport_path <> 'direct_verified' OR endpoint_class = 'remote'),
CHECK (transport_path <> 'unknown'         OR endpoint_class IN ('unknown','private','loopback')),
CHECK (transport_path <> 'proxy_unverified' OR endpoint_class IN ('remote','loopback','private','unknown')),

CHECK (provenance <> 'real_llm' OR (
        transport_path = 'direct_verified'
    AND endpoint_class = 'remote'
    AND upstream_id IS NOT NULL AND length(upstream_id) > 0
    AND http_status = 200
    AND envelope_bytes > 0
    AND envelope_sha256 IS NOT NULL AND length(envelope_sha256) = 64)),

CHECK (provenance <> 'real_llm_unverified' OR (
        unverified_reasons IS NOT NULL AND length(unverified_reasons) > 2))
```

注意 `CHECK (T <> 'local' OR E='loopback')` 在 T 为 NULL 时仍会通过——所以要**同时**有 `NOT NULL`。两者缺一不可。

---

## 6. readiness 十一项审查

### 6.1 "11 项"没有清单（M-7）

L74 与 L146 只写"共 11 项"，**全文未逐条列出**。方案 §5 列了 12 个谓词（provenance / endpoint_class / http_method / request_path / http_status / upstream_model / envelope_sha256 / envelope_bytes / peer_ip_global / proxy_used / redirect_count / evidence_id / chain_ok / late_write —— 实际 14 个），与"11"对不上。

结论：**无法核验项数**，也无法判断是否有遗漏。必须逐条编号（R1…R11），并标注每项的取值来源（观测 vs 自报）。

### 6.2 逐项短板（按方案 §5 的谓词逐条审）

| 谓词 | 是否用实测值 | 风险 |
|---|---|---|
| `provenance='real_llm'` | 写入时推导 | 若写入路径不唯一，可手改（C5） |
| `endpoint_class='remote'` | ✅ 解析+对端 | 目标/对端歧义（M-3） |
| `http_method='POST'` | ✅ | 无 |
| `request_path` 以 `/chat/completions` 结尾 | ✅ | 无 |
| `http_status=200` | ✅ | 若存成 TEXT `'200'`，与整数比较为 false（假阴性，安全） |
| `upstream_model` 非空 | ✅ | 未校验"是否等于请求模型或已知别名"，可填任意串 |
| `envelope_sha256` 非空 + `bytes>0` | ✅ | 未校验长度=64；空串 `''` 非 NULL 且 `!= ''`? 需 `length()=64` |
| `peer_ip_global is True` | ✅ | **实现陷阱**：从 SQLite 读出是 `1`，Python `1 is True` 为 **False** → 全部假阴性。须用 `bool(x) is True` 或 `x == 1` |
| `proxy_used='none'` | ✅ | 若为 NULL，`= 'none'` 求值 NULL → 在 Python 侧 `None == 'none'` 为 False（安全）；但 SQL 侧 `CHECK` 会放过（需 NOT NULL） |
| `redirect_count=0` | ✅ | NULL 时 SQL 通过、Python `None == 0` 为 False（安全）；建议 `NOT NULL DEFAULT 0` |
| `evidence_id == decision.evidence_id` | ✅ | **M-8：两侧同为 NULL 时 `None==None` 为 True → 绕过**。必须要求 `evidence_id` 非空 |
| `chain_ok` | ✅ | 若链字段为空串，链校验可能"空对空"通过；需非空断言 |
| `late_write` | ✅ | 若 `received_at`/`created_at` 任一为空，差值计算异常 → 须 fail-closed 而非跳过 |

### 6.3 是否存在 OR / 默认值 / 异常分支绕过

- 现文未给 readiness 的伪代码，**无法排除** `or` 短路与 `try/except: pass` 类写法。建议修订时附一段**唯一实现**（合取式，无 or、无 except 吞异常）。
- 默认值：`redirect_count`、`peer_ip_global`、`transport_verified` 都有默认值倾向；默认值必须"不利通过"（`peer_ip_global DEFAULT NULL`，而不是 `DEFAULT 1`）。

---

## 7. 最小反例清单（可直接用于验收测试）

| 编号 | 构造 | 现文是否通过 | 期望 |
|---|---|---|---|
| CE-1 | 直连成功、`upstream_id=NULL`、其余齐全 | 被 I6 挡（若 CHECK 正确）；但若 CHECK 写成 `!= ''` 则**通过**（C2） | 必须拒绝 |
| CE-2 | `T=NULL` + `P='real_llm'` | 通过（C1） | 必须拒绝 |
| CE-3 | `P='real_llm'` + `http_status=500` | 通过（C5） | 必须拒绝 |
| CE-4 | 回环代理 + `T='proxy_unverified'` | 被 I5 挡（M-2）→ 无法记录真实情况 | 应允许并标 unverified |
| CE-5 | 直连缺 id，想标 unverified | 无解（M-1） | 应允许 + reason |
| CE-6 | 本地真模型 | 只能标 `stub_transport`（M-4） | 应有 `local_llm` 或类似值 |
| CE-7 | `evidence_id` 两侧 NULL | readiness 判等为 True（M-8） | 必须 False |
| CE-8 | `peer_ip_global` 存为整数 1 | Python `is True` → False | 实现须用 `== 1` |

---

## 8. 必须修订项与推荐规则

| 编号 | 修订内容 | 推荐写法 |
|---|---|---|
| **M-1** | 删除 I7；`P=real_llm_unverified` 不再绑定传输路径 | `P='real_llm_unverified' ⇒ unverified_reasons 非空`；T 允许 direct/proxy/unknown |
| **M-2** | 放宽 I5 | `T='proxy_unverified' ⇒ E ∈ {remote, loopback, private, unknown}` |
| **M-3** | 定义 E 的口径 | 文档写明 E 指"base_url 目标主机"；新增 `peer_class` / `proxy_class` 观测字段（不参与约束） |
| **M-4** | 覆盖本地真实模型 | provenance 增 `local_llm`；`(E=loopback, T=local, P=local_llm)` 合法，且**不得**过 readiness |
| **M-5** | `upstream_id` 只认 `body.id` | `x-request-id`/`cf-ray` → `network_request_id`，不参与 readiness |
| **M-6** | 三轴 `NOT NULL` + 枚举 CHECK；空值判 `IS NOT NULL AND length>0` | 见 §5.3 |
| **M-7** | readiness 逐条编号 R1–R11 | 附唯一实现（纯合取，无 or/except） |
| **M-8** | `evidence_id` 与 `chain_ok` 要求非空 | `evidence_id IS NOT NULL AND evidence_id = decision.evidence_id` |
| **M-9**（新增） | I4 过强 | `T='unknown' ⇒ E ∈ {unknown, private, loopback}` |

---

## 9. 是否建议批准当前 v2 确认单

**不建议按现文批准。**

理由：不是措辞问题，而是**存在无解状态**——S1/S3/S4 三种"直连成功但证据不全"的真实情形，在现文约束下没有任何合法 `provenance` 取值；本机实际发生过的回环代理场景（S5）被 I5 直接判非法；本地真实模型（S6）无值可用。一个"无解"的约束集无法落地，落地时必然被实现者私自放宽——那比没有约束更危险。

**建议路径**：按 M-1…M-9 修订后出 v3，再走一次同样的真值表核验（预计可从"5 处硬矛盾"降到 0），然后才作为实施依据。

**可以保留不动的部分**：三轴划分方向、D2 的一票否决立场、D3 的钉死+SNI+fail-closed、D4 的零改动标注。这四项本身没有问题，问题只在不变量的边界与 SQL 写法。

---

## 10. 审查报告指纹

| 项 | 值 |
|---|---|
| 文件 | `US_REAL_LLM_EVIDENCE_V2_LOGIC_AUDIT.md` |
| 大小 / 行数 | 由交付时 `stat` / `wc -l` 实测（数值随末行指纹变化，写入正文会造成自指循环，故见交付说明） |
| 自指纹 | 见末行 |

---

## 11. 本次操作边界

未修改 v2 确认单（`5ecd4b7c…`）、未修改方案（`3d569f16…`）、未修改 `src/`/`tests/`/`config/`；未访问 `.env`/密钥/凭据；未读或写任何 SQLite；未建库/建表；未执行 SQL；未运行模型请求；未执行 S0/S1；未触碰 Git。

> 说明：§5.2 关于 NULL 使 CHECK 通过的判断基于 SQL 标准与 SQLite 语义（CHECK 仅在谓词为 FALSE 时拒绝）。本轮禁止建库，故该点标记为**语义推演、待实测**；实施阶段应补一条内存 SQLite 断言验证。

---

> 本文件 SHA256（本行之前全部字节，可用 `head -n -1 <本文件> | sha256sum` 复算）：`af91409951f44b8e8bd41c113760d5a1778d9d6e444c80bd8ed1cd49b9a447ad`
