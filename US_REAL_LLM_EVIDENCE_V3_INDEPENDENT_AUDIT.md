# US Real LLM Evidence — V3 第三方独立审计（只读）

| 项 | 值 |
|---|---|
| 审计对象 | `US_REAL_LLM_EVIDENCE_DECISIONS_V3.md`（`49fec2e9…`，373 行） |
| 对照 | `..._V2_LOGIC_AUDIT.md`（`9e21e56f…`）、`..._DECISIONS_PENDING.md`（`5ecd4b7c…`）、`..._REMEDIATION_PLAN.md`（`3d569f16…`） |
| 原则 | 不采信作者自我总结；每条结论重新推导；只读 |
| 结论 | **PASS WITH CONDITIONS** —— 无 P0，4 项 P1 必须在 SQL 定稿前修 |
| 是否可作 S0 依据 | **可以（仅第 3 步"备份"）**；**不可**作为第 4 步"dry-run SQL"的依据，直到 P1 修完 |

---

## 1. M/C 追踪完整性

### 1.1 M-1…M-9：**覆盖完整，无遗漏**

从 V2 审计报告重新提取 9 条 M 项，逐条对照 V3 §2（L32–46）与 §1（L15–30）：

| 审查项 | V3 条款 | 是否修复 | 独立验证 |
|---|---|---|---|
| M-1（I7 过窄） | V3-02 | ✅ | I7 已删（§5 L134 起，无 I7 行）；改由 `unverified_reasons` 约束（§7.2）；S2/S4/S5 三场景在 §4 中均有合法取值 |
| M-2（I5 排除回环代理） | V3-03 | ✅ | §7.2 第二组第 4 条已放宽为 4 值集合 |
| M-3（E 语义未定义） | V3-04 | ✅ | §3.1 明写"目标地址"；§3.4 增 `peer_class`/`proxy_class` |
| M-4（本地真模型） | V3-01/V3-05 | ⚠️ 部分 | 值已加（§3.3），组合已合法（§4 S7/S8）；但 DB 层无法区分 `local_llm` 与 `stub_transport`（见 A-08） |
| M-5（ID 冒充） | V3-06 | ⚠️ 部分 | 规则已收紧（§6）；值本身仍不可证（见 §6 本报告） |
| M-6（NULL 绕过） | V3-07 | ✅（但见 A-03/A-05） | 三轴 NOT NULL + 枚举 CHECK 已落（§7.1） |
| M-7（"11 项"无清单） | V3-08 | ✅ | §9 已列 R1–R16 |
| M-8（evidence_id NULL 判等） | V3-09 | ✅（但见 A-06） | R13 加了 `IS NOT NULL`；然而该列未进入 §7.1 DDL |
| M-9（I4 过强） | V3-10 | ✅ | §5 中 I4 标删除，§7.2 以 `T='unknown' ⇒ P≠'real_llm'` 替代 |

### 1.2 C 类：**覆盖不完整（A-10）**

- V2 审计 §5.2 的 **C1–C6**：V3 §8 全部覆盖，且给出 v2/v3 对照 ✅
- V2 审计 §7 的 **CE-1…CE-8**（8 条测试反例）：V3 **未逐条追踪**。其中 CE-1/2/3 与 C2/C1/C5 重复、CE-7↔C7、CE-8（`peer_ip_global` 整数比较）在 V3 中**完全消失**——R10 虽已改 `= 1`，但未记录这是为了修 CE-8。
- V3 §8 标题写"C1–C6 + 用户要求的 9 项"（L254），实际只有 **12 行（C1–C12）**。标题与内容不符。

### 1.3 是否存在"只改措辞未修逻辑"

逐条比对后：**M-1、M-2、M-9 是真修**（约束文本变了，真值表结果随之改变）；**M-7 是真修**（补了清单）。未发现"改字不改逻辑"的情形。

---

## 2. 三轴与观测字段

### 2.1 逐字段核查

| 字段 | 定义是否清晰 | 是否可观测 | 问题 |
|---|---|---|---|
| `endpoint_class` | ✅ §3.1 明确=目标 | ✅ DNS 解析 | 无 |
| `transport_path` | ✅ §3.2 | ✅ 连接路径 | `local`/`private` 与 E 冗余（已在 V2 审计指出，V3 未消解，但无害） |
| `provenance` | ⚠️ §3.3 称 real_llm 为"全链证据通过" | 派生 | **语义过强**（A-02）：DB 只能保证 I6 的 12 项，不能保证 R4/R5/R13–R15 |
| `peer_class` | ✅ §3.4 | ✅ getpeername | **无一致性约束**（A-03） |
| `proxy_class` | ✅ §3.4 | ⚠️ 依赖代理自报 | **单向约束**（A-04） |
| `proxy_used` | ⚠️ 仅注释 `'none'` | ✅ 我方决定 | **无枚举/非空约束**（A-05） |
| `proxy_env_present` | ✅ §3.4 说明"存在≠使用" | ✅ 读环境变量 | 无 |
| `unverified_reasons` | ⚠️ 仅长度>2 | 派生 | 未限定取值集合（P3） |

### 2.2 六个"特别检查"逐条回答

| 检查点 | 结论 | 依据 |
|---|---|---|
| 代理环境变量存在 ≠ 实际使用 | ✅ 已区分 | §3.4 `proxy_env_present` 与 `proxy_used` 分离；§7.2 代理一致性 CHECK 只在 `proxy_used≠'none'` 时约束 |
| 使用代理 ≠ 已验证代理上游对端 | ✅ 已区分 | §3.2 `proxy_unverified` 定义"代理不参与上游对端校验"；§4 S6 明确 `peer_class=loopback` |
| 目标地址与实际对端能分别记录 | ✅ | `endpoint_class` + `peer_class` 两列并存 |
| 代理回环地址不能被认定成最终上游 | ⚠️ **部分** | 语义上已分开，但 DB 无约束阻止 `proxy_class='none'` 而 `proxy_used≠'none'`（A-04） |
| 本地真模型不能误标 stub | ⚠️ **仅值层面** | `local_llm` 存在，但与 `stub_transport` 同在 loopback+local 组合下，DB 无法区分（A-08） |
| 合法状态必须有明确取值，不靠 unknown 掩盖 | ✅ 基本达成 | §4 的 11 场景中 10 个有确定值；S9/S10 用 `unknown` 是**语义正确**（确实不知道），非掩盖 |

---

## 3. 不变量逐条结果（真值表）

对 §5（L134–156）9 条逐条构造边界：

| # | 规则 | 合法样例 | 非法样例（是否被拦） | 结论 |
|---|---|---|---|---|
| I1 | `local ⇒ loopback` | (local, loopback) | (local, remote) → §7.2 第 1 条 CHECK 拒绝 | ✅ |
| I2 | `private ⇒ private` | (private, private) | (private, loopback) → 第 2 条拒绝 | ✅ |
| I3 | `direct_verified ⇒ remote` | (direct_verified, remote) | (direct_verified, loopback) → 第 3 条拒绝 | ✅ |
| I4 | 已删除 | — | `T=unknown` + `P=real_llm` → 第 5 条拒绝 | ✅ 替代有效 |
| I5 | `proxy ⇒ E∈{remote,loopback,private,unknown}` | (proxy, loopback) 现可写 | 任何 E 值都被允许 → **约束退化为恒真** | ⚠️ 见 A-15 |
| I6 | 12 项合取 | 全满足 | 见 §3.1 | ⚠️ 散文与 SQL 不一致（A-01） |
| I7′ | `unverified ⇒ reasons>2` | reasons 非 '[]' | `'[]'`（len=2）→ 拒绝 | ✅ |
| I8 | `local_llm ⇒ T∈{local,private} ∧ E∈{loopback,private}` | (local, loopback, local_llm) | (direct_verified, remote, local_llm) → 拒绝 | ✅ |
| I9 | 跨表断言 | — | CHECK 不覆盖，靠写入断言 + R13–R15 | ⚠️ 列未定义（A-06） |

**I5 的新问题（A-15，P2）**：放宽到 4 值后，`endpoint_class` 的 4 个取值全部允许，**该约束等于没有约束**。它仍能防止"未来新增第 5 个 E 值"，但对现有取值毫无筛选作用。建议：若保留，改为"`proxy_unverified` 时 `proxy_class ≠ 'none'`"（这才是有信息量的约束）。

### 3.1 I6 十二项合取完整性（重点）

§5（L143）散文列出：T、E、upstream_id 非空、status=200、sha256=64、bytes>0、model 非空、peer_global=1、proxy_used=none、redirect=0 → **10 项**，但同一行标注"12 个合取项"。
§7.2 SQL 实际是 **12 项**：把"upstream_id 非空"拆成 `IS NOT NULL` + `length>0`（+1），并**多出 `upstream_id_source='body.id'`（+1）**。

> **A-01（P1）**：散文漏了 `upstream_id_source='body.id'` —— 这恰好是 M-5 反冒充的核心条件。只读 §5 的人会漏掉它；两处表述不一致。

其余 11 项与 SQL 一致 ✅。

### 3.2 `real_llm` 是否必须满足全部 readiness

**否。** `P='real_llm'` 只需 I6 的 12 项；R4（method）、R5（path）、R13（evidence_id 跨表）、R14（chain）、R15（late_write）**不在 I6 内**。因此可以存在"`provenance=real_llm` 但 readiness=false"的行。

这本身是合理分层（DB 管自洽、readiness 管结论），但 §3.3 表（L71–81）把 `real_llm` 标为"全链证据通过 ✅（唯一）"，**读起来像充分条件**。
> **A-02（P1）**：须明确写"`real_llm` 是 readiness 的**必要非充分**条件"。

### 3.3 `real_llm_unverified` 能否表达"直连但证据不全"

**能。** §4 S2/S4/S5 三场景均为 `T=direct_verified` + `P=real_llm_unverified`，I7′ 只要求 reasons 非空，不约束 T —— M-1 已真正修复 ✅。

### 3.4 `local_llm` 是否有完整独立状态

**有值，无独立判据。** `(E=loopback, T=local, P=local_llm)` 与 `(E=loopback, T=local, P=stub_transport)` 在 DB 层**完全等价**，唯一区别是 P 的取值由写入方声明。
> **A-08（P2）**：`local_llm` 是**运维声明**，不是观测结果。文档未说明，容易被当成"已证实是真实模型"。

### 3.5 `proxy_used` 与 `transport_path` 双向约束

§7.2 末三条：
```sql
CHECK (proxy_used = 'none' OR transport_path = 'proxy_unverified'),      -- A
CHECK (transport_path <> 'proxy_unverified' OR proxy_used <> 'none'),    -- B
CHECK (proxy_used <> 'none' OR proxy_class = 'none')                     -- C
```
- A + B 合起来 = `proxy_used≠'none' ⟺ T='proxy_unverified'` ✅ 双向正确
- C 只单向：`proxy_used='none' → proxy_class='none'`；反向不成立 → **A-04（P1）**：`proxy_used='env:127.0.0.1:54481'` + `proxy_class='none'` 合法，等于把回环代理记成"无代理类别"，恰好掩盖本项目的核心场景。
  建议补：`CHECK (proxy_used = 'none' OR proxy_class IN ('loopback','private','remote','unknown'))`。

### 3.6 是否存在合法场景被拒 / 非法组合被允许

| 类型 | 实例 | 判定 |
|---|---|---|
| 合法被拒 | 无（11 场景全部通过） | ✅ |
| 非法被允许 | `E=remote + peer_class=loopback + peer_ip_global=1`（自相矛盾） | ❌ **A-03** |
| 非法被允许 | `proxy_used≠'none' + proxy_class='none'` | ❌ **A-04** |
| 非法被允许 | `proxy_used=''`（空串，非 'none'） | ❌ **A-05** |

---

## 4. R1–R16 与 I6 对应

| R | 对应 I6 项 | 覆盖 |
|---|---|---|
| R1 provenance | —（结论） | — |
| R2 endpoint=remote | I6-2 | ✅ |
| R3 transport=direct_verified | I6-1 | ✅ |
| R4 method=POST | **不在 I6** | 仅 readiness |
| R5 path 后缀 | **不在 I6** | 仅 readiness |
| R6 status=200 | I6-6 | ✅ |
| R7 model 非空 | I6-9 | ✅ |
| R8 sha256=64 | I6-7 | ✅ |
| R9 bytes>0 | I6-8 | ✅ |
| R10 peer_global=1 | I6-10 | ✅ |
| R11 proxy_used=none | I6-11 | ✅ |
| R12 redirect=0 | I6-12 | ✅ |
| R13 evidence_id 非空+相等 | **不在 I6**（跨表） | 仅 readiness + 断言 |
| R14 chain_ok | **不在 I6** | 仅 readiness |
| R15 late_write=0 | **不在 I6** | 仅 readiness |
| R16 upstream_id 非空 | I6-3/4 | ✅ |
| **（缺）** `upstream_id_source='body.id'` | I6-5 | **无 R 项**，只靠 CHECK |

**结论**：R 表与 I6 是"必要子集 + 5 项跨表/请求侧"的关系，逻辑上自洽；但 `upstream_id_source` 没有对应 R 项（A-01 的延伸）。

### 4.1 14 → 16 的算术（A-11）

§9.1（L298–321）称"拆分 2 条（P7、P12 各拆为 1 条但加强）、新增 2 条、净 +2"。实际映射是 **1:1 全映射**（P1→R1 … P14→R15，跳过 R3），条目**从未拆分**。
- P7（`bool(sha256)`）→ R8（加强为 `length=64`），仍是 1 条
- P12（`evidence_id 真值 + 相等`）→ R13（内部两项断言），仍是 1 条
正确表述应为：**14 条保序映射 + 新增 R3/R16 = 16 条**。"拆分"说法自相矛盾（"拆为 1 条"不叫拆分）。

---

## 5. SQLite 反例测试结果

> 方法：按 §7.1/§7.2 的表达式做**纯逻辑推演**（本轮禁止建库）。全部结论标记"待实测"。

| # | 反例 | 预期 | 实际（推演） | 拦截者 | 结论 |
|---|---|---|---|---|---|
| 1 | 三轴任一 NULL | 拒绝 | 拒绝 | `NOT NULL`（CHECK 本身会放过 NULL） | ✅ 必须靠 NOT NULL，V3 已加 |
| 2 | 三轴空串 | 拒绝 | 拒绝 | `CHECK IN`（'' 不在集合） | ✅ |
| 3 | 非法枚举值 | 拒绝 | 拒绝 | `CHECK IN` | ✅ |
| 4 | `upstream_id=NULL` + P=real_llm | 拒绝 | 拒绝 | I6 的 `IS NOT NULL` | ✅ |
| 5 | `upstream_id=''` + P=real_llm | 拒绝 | 拒绝 | I6 的 `length>0` | ✅ |
| 6 | `http_status=NULL` | 拒绝 | 拒绝 | `NOT NULL` | ✅ |
| 7 | `http_status=500` + P=real_llm | 拒绝 | 拒绝 | I6 的 `=200` | ✅ |
| 8 | `evidence_id=NULL` | 拒绝 | **不拒绝** | 该列不在 §7.1 DDL 内，CHECK 无从约束 | ❌ **A-06** |
| 9 | `proxy_used` 与 T 矛盾 | 拒绝 | 拒绝 | 末三条 CHECK 的双向部分 | ✅ |
| 10 | `E=remote + peer_class=loopback + peer_global=1` | 拒绝 | **不拒绝** | 无此约束 | ❌ **A-03** |
| 11 | `proxy_used≠'none' + proxy_class='none'` | 拒绝 | **不拒绝** | C 只单向 | ❌ **A-04** |
| 12 | `proxy_used=''` | 拒绝 | **不拒绝** | 无枚举/非空约束 | ❌ **A-05** |
| 13 | `P=unverified + reasons='[]'` | 拒绝 | 拒绝 | I7′ `length>2` | ✅ |
| 14 | `P=unverified + reasons='[ ]'`（len=3） | 拒绝 | **不拒绝** | 只看长度，不看内容 | ⚠️ P3 |
| 15 | 多字段同时 NULL | 拒绝 | 拒绝 | 逐列 `NOT NULL` | ✅ |

**结论**：15 条中 11 条被正确拒绝，**4 条可写进库**（A-03/A-04/A-05/A-06），1 条形式合规但内容无校验（P3）。

---

## 6. C8 与证据真实性评价

| 命题 | V3 是否宣称 | 审计判定 |
|---|---|---|
| CHECK 能证明 `upstream_id` 来自上游 | **否** | ✅ V3 §6 明写"无法从值本身证明"，§11 R-1 列为架构性残余风险 |
| 同事务能证明上游身份 | **否** | ✅ 未宣称；V3 只把同事务用于"不留孤证" |
| 哈希链能防有写权限者重写整条链 | **未宣称，但也未纠正** | ⚠️ **A-13（P2）**：方案 §3.5 曾写"链式 integrity_hash 防止插入/删改中间行"；拥有 DB 写权限者可重算全链。V3 未继承该表述，但也未指出其局限 → 建议补一句"链只发现局部改写" |
| 本地记录的模型名能证明实际响应模型 | **否** | ✅ V3 §6 区分了"记录值"与"值真实性" |

**C8 评价：V3 的处理是诚实的。** 它把 `upstream_id_source='body.id'` 落进 CHECK 只是"记录声明"，没有升级为"证明"。这一条**通过**。

---

## 7. IP pinning / TLS 审查

| 要求 | V3 是否承接 | 判定 |
|---|---|---|
| 保留原始域名 | ⚠️ 隐含（§3.2 未提，方案 §4.2 有） | P2 |
| TLS SNI | ❌ **V3 全文未出现"SNI"** | **A-14（P2）** |
| 证书主机名验证 | ❌ 未出现"证书/主机名校验" | **A-14** |
| 多公网 IP 验证规则 | ⚠️ 仅 §11 R-6 提到 CDN 偶发失败 | P3 |
| DNS 变化/异常 | ✅ §4 S9/S10 + §3.1 `unknown` | ✅ |
| 代理 CONNECT 边界 | ❌ **未界定** | **A-14** |
| `tls_spki_sha256` 存储 | ❌ **§7.1 DDL 无该列**（方案 §3.2 有） | **A-07（P2）** |
| 是否宣称 pinning 证明上游身份 | **否** | ✅ 通过 |

**A-14 具体缺口**：在代理 CONNECT 隧道下，TLS 仍是端到端（证书主机名校验仍由本地执行），但 `getpeername()` 只能是代理地址；V3 只用 `peer_class` 记录"对端=代理"，未说明"此时仍可验证证书主机名、但无法验证网络路径"。这个边界不写清，实施者可能误以为 `proxy_unverified` 连证书都没验。

---

## 8. 问题分级

| ID | 级别 | 问题 | 位置 | 影响 |
|---|---|---|---|---|
| **A-01** | **P1** | §5 的 I6 散文只列 10 项，漏 `upstream_id_source='body.id'`，与 §7.2 的 12 项不一致 | V3 L143 vs L205–235 | 按散文实施会丢掉反冒充核心条件 |
| **A-02** | **P1** | §3.3 把 `real_llm` 标为"✅（唯一）"，暗示充分；实际 DB 仅保证 I6，R4/R5/R13–R15 未覆盖 | V3 L71–81 | 误以为 `real_llm` 即通过 readiness |
| **A-03** | **P1** | 缺 `endpoint_class` / `peer_class` / `peer_ip_global` 一致性约束 | V3 §7.2 | `E=remote + peer=loopback + global=1` 可入库，自相矛盾 |
| **A-04** | **P1** | `proxy_class` 只单向约束，用了代理可记 'none' | V3 §7.2 末条 | 回环代理的具体事实可被"合规地"抹掉 |
| **A-05** | P2 | `proxy_used` 无枚举/非空约束，`''` 可通过 | V3 §7.1 | "用了代理但不可审计" |
| **A-06** | P2 | §7.1 DDL 缺 `evidence_id`/`decision_id`/`received_at`/`created_at`/`seq`，而 R13/R15 依赖 | V3 §7.1 vs §9 | 单看 V3 无法落地 R13/R15 |
| **A-07** | P2 | 丢失 `tls_spki_sha256` | V3 §7.1（方案 §3.2 有） | pin 校验结果无处落库 |
| **A-08** | P2 | `local_llm` 与 `stub_transport` 在 DB 层不可区分 | V3 §3.3/§4 S7 | `local_llm` 实为运维声明 |
| **A-09** | P2 | §2 追踪表"验证位置"8 处引用错位（C 表在 §8 写成 §5；R 表在 §9 写成 §7） | V3 L32–46 | 按图索骥会找错 |
| **A-10** | P2 | §8 标题称"C1–C6 + 9 项"，实为 12 行；V2 的 CE-1…CE-8 未追踪 | V3 L254 | 反例覆盖不可核 |
| **A-11** | P3 | §9.1"拆分 2 条（各拆为 1 条）"自相矛盾，实际未拆分 | V3 L298–321 | 计数说明失真 |
| **A-12** | P3 | 非 real_llm 行允许 `upstream_id` 非空但 `upstream_id_source=''` | V3 §7.1 | 观测不一致 |
| **A-13** | P2 | 未纠正方案"链哈希防止插入/删改"的过强表述 | V3 §5 I9 / 方案 §3.5 | 高估链的作用 |
| **A-14** | P2 | 未承接 SNI/证书主机名/CONNECT 边界；只提 getpeername | V3 §3.2/§7 | 实施时丢失 D3 的关键要求 |
| **A-15** | P2 | I5 放宽到 4 值后约束恒真，失去筛选力 | V3 §5 L142 / §7.2 | 形式约束、无实际防伪 |

统计：**P0 = 0，P1 = 4，P2 = 8，P3 = 3。**

---

## 9. 与 V2 审计的对照（是否真的修好）

| V2 审计结论 | V3 后 |
|---|---|
| 5 处硬矛盾（3 场景无解） | **0 处**（11 场景全有唯一合法值） |
| 6 条 CHECK 绕过 | 剩 **3 条**（A-03/A-04/A-05）+ 1 条跨表（A-06） |
| "11 项"无清单 | 已列 16 项，但映射措辞有误（A-11） |
| `upstream_id` 可被网关 ID 顶替 | 已收紧到 `body.id` ✅ |
| readiness 双侧 NULL 判等 | 已加 `IS NOT NULL`，但列未进 DDL（A-06） |

**净评价**：核心逻辑缺陷（会导致"无解状态"的那类）已全部清除；剩余是**约束覆盖不全**与**文档一致性**问题，不影响模型方向，但会让实施漏掉关键防线。

---

## 10. 最终结论

**PASS WITH CONDITIONS。**

- 三轴模型方向正确，11 个场景全部有合法取值，M-1/M-2/M-4/M-5/M-9 的实质缺陷已修。
- 但有 4 项 P1 必须在 SQL 定稿前修复：
  1. **A-01** 统一 I6 的两处表述（散文补 `upstream_id_source='body.id'`，或让散文直接引用 SQL）；
  2. **A-02** 明确 `real_llm` 是必要非充分条件；
  3. **A-03** 补 `peer_class`/`peer_ip_global`/`endpoint_class` 三者一致性 CHECK；
  4. **A-04** 补 `proxy_class` 的反向约束。
- 另有 8 项 P2（DDL 字段缺失、SNI/CONNECT 未承接、追踪表引用错位等）建议同批修正。

### 是否建议把 V3 作为 S0 依据

| S0 子步骤 | 建议 | 理由 |
|---|---|---|
| 第 3 步：备份 `us_loop.db` + 记 SHA256 | **可以** | 只读备份，与约束内容无关 |
| 第 4 步：dry-run 打印 SQL | **不可以（暂缓）** | SQL 尚有 P1 未闭环（A-01/A-03/A-04），先修再生成，避免"打印一版错的再改" |
| 第 5 步：提交 SQL 待审 | 依赖第 4 步 | — |

即：**V3 可作为"实施方向"的依据，不可作为"SQL 定稿"的依据。**

---

## 11. 审计边界

- 未修改 V3（`49fec2e9…`）、V2（`5ecd4b7c…`）、审查报告（`9e21e56f…`）、方案（`3d569f16…`）；
- 未修改源码/测试/配置/数据/日志；未读密钥；未建库、未生成 SQL、未执行 SQL；未发模型请求；未启动 S0/S1；未做 Git 操作；
- §5 的 CHECK 行为为**逻辑推演**（SQLite 语义：CHECK 仅在谓词求值为 FALSE 时拒绝），**待实施阶段用内存断言实测**；
- 未验证 SQLite 版本差异（`DROP COLUMN` 需 ≥3.35），属实施细节。

---

## 12. 文件指纹

| 项 | 值 |
|---|---|
| 文件 | `US_REAL_LLM_EVIDENCE_V3_INDEPENDENT_AUDIT.md` |
| 大小 / 行数 | 见交付说明（`stat` / `wc -l` 实测） |
| 自指纹 | 见末行 |

---

**只读独立审计完成；实施未授权；S0 未启动。**

---

> 本文件 SHA256（本行之前全部字节，可用 `head -n -1 <本文件> | sha256sum` 复算）：`4ee071c4de70e03acc7a5272c7b5ec642149e9d22b430e90b73767f1b4a8f940`
