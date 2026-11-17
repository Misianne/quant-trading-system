# Databento 真实历史样本验证报告

- 项目：`C:\FosunAI-Trading`
- 基线 commit：`6e0c4ef`
- 本轮 commit：`143ff1c`
- 生成时间：2026-11-17
- 报告性质：**数据真实性验证**（不是实盘接入）

---

## 0. 结论先行

**本机没有任何 Databento 凭据，因此一条真实响应都没有取到。**

这不是"没时间做"，而是硬性外部条件不成立。四处都查过，全部为空：

| 检查位置 | 结果 |
|---|---|
| 进程环境变量 `DATABENTO_API_KEY` / `DATABENTO_KEY` / `DB_API_KEY` | 均未设置 |
| 用户级持久化环境变量（`HKCU\Environment`） | 只有 `Path/TEMP/TMP/OneDrive/GOPATH/JAVA_HOME` 等 8 项，无任何数据源凭据 |
| 既有凭据文件 `C:\Users\<redacted>\Desktop\<redacted>.txt`（766 行） | 无 `databento` / `db-` 关键字命中 |
| `~/.databento`、`~/.databento/config.toml` | 不存在 |
| 常见项目 `.env` | 不存在（或不含该关键字） |

按照"费用上限 0 美元、无法保证不扣费就 BLOCKED"的要求，**没有发起任何数据请求**，
也没有尝试购买、试用或注册。

因此本报告的核心内容不是"真实样本长什么样"（那部分必须标 BLOCKED），而是：
**在拿到真实样本之前，先把"能不能正确解析真实样本"这件事用官方 schema 验证掉，
并把所有会静默出错的地方修掉。**

结果：发现了 **5 个必然导致真实数据 100% 解析失败或数值错误**的缺陷（详见 §5），
**7 个安全性/可观测性缺陷**，以及 **1 个方向性安全缺陷**（详见 §6）。
这些都不是理论推演——任何一个放过，接上真实行情的那一天都会表现为
"没有数据"、"价格差了 9 个数量级"，或者"该停的票没停"。

---

## 1. 执行前保护

### 1.1 基线与工作树

```
HEAD    = 6e0c4ef  (feat(market): provider adapter layer for real US market data)
branch  = master
status  = clean
```

`6e0c4ef` 与本轮指定基线一致，未发现代码与报告不符。

### 1.2 本轮备份

```
路径     C:\FosunAI-Trading-Backups\round8-20261117-123834
文件数   2182
清单行数 2179（差的 3 个是各子目录既有的 MANIFEST.sha256，按名排除）
校验     sha256sum -c → 0 失败
体积     52 MB
```

备份**排除**了 `.venv`/`.git`/`data/paper_runs`。`data/paper_runs` 原地保留不动
（约 400 MB、1.2 万文件），这是用户明确要求的"不删除历史运行记录"。

### 1.3 Git 检查点

```
tag baseline-round8 → 6e0c4ef
```

### 1.4 隔离目录

```
verification/databento/
├── gen_fixtures.py        样本生成（253 行）
├── run_verification.py    证据生成（164 行）
├── fixtures/              7 个样本 + 5 个坏样本 + symbology.json
├── raw/                   探针产出（当前只有 probe-blocked.json）
├── out/verification.json  全部数字的唯一来源
└── logs/
```

所有验证使用独立临时数据库与独立目录，未触碰既有 Paper 账本。

---

## 2. 费用核验（0 美元上限）

### 2.1 没有可用额度信息，也没有可用凭据

Databento 的额度只能在 Portal 里看，没有公开的余额查询接口；而连 `metadata.get_cost`
这类**免费**估价接口都需要 `Authorization: Bearer`。没有 Key，连"这次要花多少钱"
都问不出来。

因此：

| 核验项 | 状态 |
|---|---|
| 可用免费额度是否真实存在 | **UNVERIFIED**（需登录 Portal 查看，未登录） |
| 本次请求预计消耗多少额度 | **UNVERIFIED**（估价接口需要凭据） |
| 是否存在免费额度以外的扣费风险 | **UNVERIFIED**（额度耗尽后会走绑定的支付方式） |
| 是否需要额外订阅或交易所授权 | 需要：`XNAS.ITCH` 属交易所直连数据集，通常另需 Nasdaq 授权 |

### 2.2 已实现的费用门禁（v2，三档分离）

> **本节原先描述的是 v1 探针，已被 `DATABENTO_ZERO_COST_PROBE_AUDIT.md` 推翻并重写。**
> v1 把 `metadata.get_cost` 称为"免费估价接口"——**免费指的是不产生数据费用，
> 它仍然是一次需要凭据 + DNS + TCP + TLS 的 HTTPS 请求**。
> 那是审计里的 B 类（向 Databento 查价），不是 A 类（本地计算）。
> 在 v1 下根本无法做到"纯估价零网络"。

v2 把三件事在参数层和代码层彻底分开：

| 档位 | 模式 | 需要凭据 | 需要网络 | 请求历史数据 | exit |
|---|---|---|---|---|---|
| **A** | `--estimate-only`（默认） | 否 | **否** | 否 | 0 / 7 |
| **B** | `--estimate-online` | 是 | 是 | 否 | 0 / 3 / 4 |
| **C** | `--fetch` | 是 | 是 | **是** | 0 / 3 / 4 / 5 / 6 |

A 档的硬保证：不读 `DATABENTO_API_KEY`、不构造 `HttpClient`、`_http_get` 是死代码；
只根据 `--price-book` 里**显式声明 `verified=true`** 的单价 + `--expected-gb` 估算；
缺任一项返回 `UNKNOWN_COST`（exit 7）。本机没有可核实单价，所以 A 档默认结果就是 7。

C 档的三重授权：`--fetch`（明确要下载）+ `--max-cost-usd > 0`（默认 0=不允许花钱）
+ `--confirm`（二次确认）。上限另有硬顶 `HARD_CAP_USD = 25`，且**完全忽略环境变量**。

v1 的一个真实漏洞已在本轮修掉：`--max-cost-usd nan` 会让 `cost > cap` 恒为 False，
上限形同虚设；`inf` 同理。现在 `nan/inf/负数/超硬顶` 一律 exit 2。

门禁测试从 12 项扩到 **37 项**（`tests/test_databento_fee_gate.py`），
其中联网证明改用 **socket 层拦截**（`getaddrinfo`/`socket`/`create_connection`/
`ssl.wrap_socket` 全部替换为计数+抛错），不再依赖"日志里没写"。

### 2.3 实际发生的扣款

**0 美元。** 一条请求都没有发出。

---

## 3. 样本获取：BLOCKED

### 3.1 为什么不能伪造

原计划是取一只流动性好的美股在一段有公开停牌记录的历史窗口内的
`status` + `trades`。没有凭据就无法获取，而"用记忆里的例子编一段"是本轮明确禁止的。

因此本轮采用的做法是：**按官方字段表逐字段构造样本，并明确标注它是合成的**。

`verification/databento/fixtures/README.md` 第一行就写着：

> **这些文件是按官方 schema 构造的合成样本，不是真实 API 响应。**

这样做的价值是真实的：官方字段表规定了字段名、类型、枚举取值、价格标度、时间单位，
一个"照着自己想象写"的解析器会在这里立刻暴露。事实也正是如此（见 §5）。

### 3.2 合成样本清单

| 文件 | 覆盖点 |
|---|---|
| `synthetic_luld_pause.jsonl` | LULD 暂停→恢复；含**停牌期间成交**（迟到打印） |
| `synthetic_luld_pause_nosym.jsonl` | 同上，但**无 `symbol` 字段**（真实默认形态） |
| `synthetic_news_halt.jsonl` | 新闻停牌 `HALT/NEWS_PENDING` → `TRADING/NEWS_RELEASED` |
| `synthetic_mwcb_halt.jsonl` | 市场级熔断 `reason=120` / `124` |
| `synthetic_ambiguous_status.jsonl` | `NOT_AVAILABLE_FOR_TRADING`（收盘 or 停牌，官方不区分） |
| `synthetic_non_halt_status.jsonl` | `QUOTING` / `CLOSE` / `SSR_CHANGE`（不得造出假停牌） |
| `synthetic_pretty.jsonl` | `pretty_px/pretty_ts` 形态：ISO 时间 + 浮点价 + `symbol` |
| `bad/*.jsonl` | 未知 rtype、无 symbol 无映射、`UNDEF_PRICE`、未知 action、naive 时间 |

### 3.3 原始响应

**无。** `verification/databento/raw/` 里目前只有 `probe-blocked.json`，
内容是凭据缺失的结构化记录。

---

## 4. 字段与官方文档的交叉核对

不能"解析成功就算对"。本轮把三处独立来源对齐：

1. 官方文档 `https://databento.com/docs/schemas/status`（字段表 + 三组枚举）
2. 官方文档 `https://databento.com/docs/schemas/trades`（字段表 + 价格标度）
3. **官方客户端 `databento-dbn` 0.73.0 的类型存根与编码字节**
   —— 文档只给了枚举的整数值，字节布局是另外用 `bytes(msg)` 差分实测确认的
4. 官方客户端 `databento` 0.90.0 的 `common/publishers.py::Venue.from_int`（72 项映射）

### 4.1 文档与"想当然"的差异

| 项 | 想当然的写法 | 官方实际 |
|---|---|---|
| 记录类型 | `type: "status"` | `rtype: 18`（uint8 整数） |
| `status.action` | `"H"` 字符 | **uint16 整数**，HALT=8 / PAUSE=9 / TRADING=7 |
| `trades.action` | 整数 | **char**，恒为 `"T"` |
| `status.sequence` | 存在 | **不存在** |
| `symbol` | 每条都有 | **两条 schema 都没有**，只有 `instrument_id` |
| `publisher_id` | `"XNAS"` 字符串 | **整数**（2=nasdaq，9=nyse…） |
| `price` | 永远 int64 / 1e9 | int64/1e9，**但 `pretty_px=True` 时是 float** |

### 4.2 `status` 字段（官方原文）

| 字段 | 类型 | 说明 |
|---|---|---|
| `ts_recv` | uint64 | 抓包服务器收到时间，ns since epoch |
| `ts_event` | uint64 | 撮合引擎时间，ns since epoch |
| `rtype` | uint8 | 恒为 18 |
| `publisher_id` | uint16 | 数据集+场所 |
| `instrument_id` | uint32 | 数值标的 ID |
| `action` | uint16 | 状态变化类型（0–15） |
| `reason` | uint16 | 变化原因（**取值不连续**） |
| `trading_event` | uint16 | 附加信息（0–4） |
| `is_trading` | char | `Y`/`N`/`~` |
| `is_quoting` | char | `Y`/`N`/`~` |
| `is_short_sell_restricted` | char | `Y`/`N`/`~` |

### 4.3 `trades` 字段（官方原文）

`ts_recv`、`ts_event`、`rtype`（恒 0）、`publisher_id`、`instrument_id`、
`action`（恒 `T`）、`side`（`A`/`B`/`N`）、`depth`、`price`（**int64，1 unit = 1e-9**）、
`size`（uint32）、`flags`、`ts_in_delta`（int32）、`sequence`（uint32）。

### 4.4 枚举整数值的实测确认

文档给了整数值，但"文档说的"和"库里编的"未必一致。用官方库把消息编码成字节、
按偏移读出字段值来交叉确认（`bytes(StatusMsg(...))` 差分定位偏移）：

```
StatusMsg 长度 40 字节；字段偏移经差分实测：
  action          @24   (uint8)   HALT=8
  reason          @26   (uint8)   LULD_PAUSE=50
  trading_event   @28   (uint8)
  is_trading      @30   (char)    'Y'/'N'/'~'
  is_quoting      @31   (char)
  ssr             @32   (char)
```

`StatusAction` 全 16 项与文档逐一吻合（0=NONE … 15=NOT_AVAILABLE_FOR_TRADING），
`StatusReason` 全 34 项取值（含 30/50/120–124/130 这些不连续值）逐一吻合。

### 4.5 `publisher_id` 映射

官方 `Venue.from_int` 共 **72 项**。其中 4 个是**合并发布者**，不能用来判"某个
交易所停牌"：

```
40 = DBEQ    47 = EQUS    59 = CTA    60 = UTP
```

适配器对这 4 个 id 会额外计数 `consolidated_publisher`，避免把"全市场合并流"当成
"某交易所的停牌"。

---

## 5. 本轮修复的 12 个真实缺陷

按影响排序。前 5 个属于"接上真实数据必然出错"，后 7 个属于"出错时看不出来"。
（§6 那一条方向性缺陷另算，它的影响比这 12 条都大。）

| # | 缺陷 | 后果 | 修法 |
|---|---|---|---|
| 1 | 按 `type: "status"` 字符串分派，真实字段是 `rtype: 18` | **真实数据 100% 被丢弃**，表现为"供应商没数据" | `_rtype_of()` 优先 `rtype`，兼容旧字符串 |
| 2 | 按 `item["symbol"]` 取标的，真实记录**没有该字段** | 100% 丢弃 | 缺失时查 `symbology` 映射；两者都无则计 `MISSING_SYMBOLOGY` 并丢弃 |
| 3 | `action == "H"` 判断停牌，真实是整数 `8` | **停牌永远判不出来** → 该停的时候不停 | `status_action_int()` 归一；`{8,9,10}` 为停牌 |
| 4 | `price / 1e9` 无条件执行，`pretty_px=True` 时价格已是浮点 | **价格差 9 个数量级**（201.2 → 2e-7），且不报错 | 按类型定标度：int→/1e9，float→原样；`UNDEF_PRICE` 丢弃 |
| 5 | `publisher_id` 当字符串查别名表，真实是整数 | 交易所永远是 `unknown` | 72 项整数映射；未知→`unknown` 并计数 |
| 6 | 解析异常被 `except: return None` 吞掉 | 字段对不上表现为"没数据"，无法定位 | 计 `parse_errors` + 按原因计数 `rejected` |
| 7 | `parse_timestamp` 用浮点除法 | 纳秒精度丢 1µs（`...123456789` → `123457µs`） | 改整数 `divmod`，向下取整 |
| 8 | naive 时间戳的告警被丢弃 | 缺时区的数据被静默当成 UTC | 告警回传给调用方加 `NAIVE_TIME` 标记 |
| 9 | `RecordedProvider` 无条件把 `receive_time` 改成 `event_time` | `ts_recv` 信息永久丢失，无法评估延迟/乱序 | 保留记录自身的 `ts_recv` |
| 10 | 非停牌 status（QUOTING/CLOSE）计入 `dropped` | 正常状态被当成故障，掩盖真实丢弃 | 引入 `skipped` 与 `dropped` 分离 |
| 11 | 未知 `action` 直接丢弃事件 | 沿用旧状态 → 若旧状态可交易，等于**放行** | 按"不可交易"处理并标记 `UNKNOWN_ENUM` |
| 12 | MBP-1 从顶层读 `bid_px`，真实在 `levels[0]` | 买卖价恒为 0 → 报价被丢 | 读 `levels[0]`，兼容顶层 |

> 还有一个**方向性**的安全缺陷，影响面比上面都大，单独写在 §6。

---

## 6. 权威门禁的方向性缺陷（最重要的安全修复）

原实现的签名策略是**对称**的：只要 `require_signature_for_resume=True`，
未签名事件一律拒绝。听起来很安全，实际上方向错了：

- 未签名的**恢复**被拒 → 正确（不解除停牌）
- 未签名的**停牌**也被拒 → **错误**：停牌没生效，标的停在"可交易"状态

后者是 **fail-open**。停牌是**收敛风险**，恢复是**放大风险**，两者不该同一标准。

已改为不对称：

```
收敛风险（halted=True）   ：身份 + 交易所覆盖 + 绑定 + 时效 全部通过即生效
放大风险（halted=False）  ：额外要求密码学签名，未签名一律 NO_SIGNATURE
```

实测（`verification.json → authority`，节选字段）：

```json
"unsigned_halt":   {"accepted": true,  "risk_increasing": false,
                    "signature": "unsigned"}
"unsigned_resume": {"accepted": false, "risk_increasing": true,
                    "reason": "[NO_SIGNATURE] 未通过签名验证（unsigned）：供应商未提供消息签名（本轮调查结论）"}
"asymmetry_holds": true
```

这条修复同时解释了为什么 D 项（官方恢复权限认证）必须是 **BLOCKED** 而不是 PASS：
代码层的门禁是对的，但**外部没有签名体系**，所以"能验签"这件事不成立。

---

## 7. 判定

| 项 | 判定 | 依据 |
|---|---|---|
| **A. 真实历史行情解析** | **BLOCKED**（二选一记 FAIL） | 零真实响应。验收条件"用真实响应验证"未满足，按你的标准不得记 PASS |
| **B. Status 事件语义** | **PARTIAL** | 字段名/类型/枚举/标度已与官方文档**和官方客户端字节编码**双重对齐；但**未经真实流量**验证，供应商实际投递顺序、抖动、快照行为未知 |
| **C. 停牌期间成交门禁** | **PASS** | 见 `DATABENTO_PROVIDER_E2E_TEST_REPORT.md` §3：停牌期间样本中确有成交，网关拒绝出价、Broker 独立门禁拒绝成交 |
| **D. 官方恢复权限认证** | **BLOCKED** | 代码层已实现"未签名不得恢复"并通过测试；但**不存在签名体系**，无法完成"认证"本身 |
| **E. 真实行情实时订阅准备** | **BLOCKED** | 需凭据 + 交易所授权 + 实时链路；本轮禁止建立 WebSocket |

A 为什么不能记 PASS：PASS 的含义是"解析器在真实数据上被验证过"。没有真实数据，
这个命题没有被检验过。B 之所以能给 PARTIAL 而不是 BLOCKED：语义是**可离线判定**的
（官方文档 + 官方库编码），这一层确实做完了；只是"真实流量"这一层没做。

---

## 8. 下一步唯一推荐行动

**在 Portal 里确认额度，然后用 `tools/databento_probe.py` 做一次 0 成本估价试探。**

```powershell
# 1) 先只估价，不取数（默认上限 0 美元，不会产生扣费）
$env:DATABENTO_API_KEY = "<从 Portal 复制，不要写进任何文件>"
python tools/databento_probe.py `
  --dataset XNAS.ITCH --schema status --symbols AAPL `
  --start 2026-11-10T14:30:00Z --end 2026-11-10T14:35:00Z
# exit 5 = 超预算（正常，说明门禁生效）；把看到的金额记下来
```

拿到估价后，只有两个选择：

1. 金额可接受 → `--max-cost-usd <该金额> --confirm` 取一小段，然后把
   `verification/databento/raw/*.jsonl` 喂给 `DatabentoProvider` 做一次真实解析；
2. 金额不可接受 → 保持 BLOCKED，本轮结论不变。

**在真实响应被解析验证之前，不要接任何实时订阅，也不要把任何供应商事件当作
官方恢复依据。** 三个开关保持关闭。

---

## 9. 未解决问题 / UNVERIFIED

| 项 | 状态 | 说明 |
|---|---|---|
| 真实响应字段是否与文档完全一致 | UNVERIFIED | 需一次真实取数 |
| `status` 是否在 UTC 午夜带快照记录 | UNVERIFIED | 官方文档提到会带，未实测 |
| `pretty_px/pretty_ts` 的实际默认值 | PARTIAL | 官方 `to_json()` 默认 `True`；HTTP `encoding=json` 的行为未实测 |
| `ts_recv - ts_event` 的真实量级 | UNVERIFIED | 无样本 |
| 停牌期间是否真有成交、占比多少 | UNVERIFIED | 样本里是构造的 |
| Databento 额度余额 | UNVERIFIED | 无接口，需 Portal |
| 交易所授权（`XNAS.ITCH`） | UNVERIFIED | 通常需另购，未确认账户是否已有 |
| 消息签名 | **BLOCKED（外部）** | 官方未提供，非本项目可解 |

---

## 10. 附：本轮产出文件

| 文件 | 行数 | 作用 |
|---|---|---|
| `src/ai_trader/market/providers/databento_schema.py` | 349 | 官方常量（rtype/枚举/场所/价格/三态） |
| `src/ai_trader/market/providers/adapters.py` | 修改 | `DatabentoProvider` 重写 |
| `src/ai_trader/market/providers/authority.py` | 修改 | 不对称签名门禁 |
| `src/ai_trader/market/providers/base.py` | 修改 | 按原因计数、skipped 分离 |
| `src/ai_trader/market/providers/models.py` | 修改 | 质量标记扩展、事件字段补齐 |
| `src/ai_trader/market/providers/normalize.py` | 修改 | 整数时间运算保精度 |
| `tools/databento_probe.py` | 217 | 费用门禁探针 |
| `verification/databento/gen_fixtures.py` | 253 | 样本生成（含"合成"声明） |
| `verification/databento/run_verification.py` | 164 | 证据生成 |
| `tests/test_databento_*.py` | 1194 | 111 个新用例（新文件内） |

全量测试：**1290 通过 / 0 失败 / 0 错误 / 1 跳过**（`r12.xml`，169 s）。
