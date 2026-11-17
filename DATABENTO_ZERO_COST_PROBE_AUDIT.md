# Databento 零成本探针审计报告

- 项目：`C:\FosunAI-Trading`
- 基线 commit：`b186aea`
- 本轮 commit：`0e4c3b4`（探针重写）、`737f88e`（测试补全）、本报告另提交
- 审计对象：`tools/databento_probe.py`（唯一对象）
- 生成时间：2026-11-17

---

## 0. 结论

**上一版的"估价"不是本地计算，是 B 类网络调用；并且费用上限可被 `nan` 绕过。**

审计发现 6 项问题，其中 1 项是可直接导致超支的真实漏洞。已全部修复，
并用 socket 层拦截证明 A 档零网络。

| 项 | 判定 |
|---|---|
| 三档模式是否真实分离 | **是**（v2） |
| A 档是否有任何网络可能 | **否**（客户端构造即抛错 + socket 拦截实测 0 调用） |
| 费用上限是否可被绕过 | **否**（nan/inf/负数/超硬顶/environment 全部拦） |
| 实际扣款 | **0 美元**（未发出任何请求） |
| 是否可进入授权购买阶段 | **可以，等你决定**（见 §8） |

---

## 1. 执行前保护

```
HEAD    = b186aea   (docs(databento): sample / status-semantics / e2e test reports)
branch  = master
status  = clean（先把上轮冒烟测试重写的 probe-blocked.json 恢复到提交版本）
```

备份：

```
路径     C:\FosunAI-Trading-Backups\round9-20261117-131918
文件数   2211
清单行数 2208（差的 3 个是各子目录既有 MANIFEST.sha256，按名排除）
校验     sha256sum -c → 0 失败
体积     53 MB
```

未修改任何 Paper 数据、交易逻辑、账本或配置开关。

---

## 2. 逐项审计：v1 的真实执行路径

审计方法：从 `main()` 入口逐行读到 `urllib.request.urlopen`，不看注释只看代码。

### 2.1 参数解析

`argparse`，无互斥组。唯一的"模式"概念不存在——所有参数都可以任意组合。
`--confirm` 只是布尔开关，`--max-cost-usd` 只是浮点数。

**问题**：没有任何"我只要估价"的表达方式。程序永远走同一条路。

### 2.2 默认费用上限

```python
ap.add_argument("--max-cost-usd", type=float, default=0.0, ...)
```

默认 0.0 本身是对的。但 `argparse` 的 `type=float` 接受 `nan`、`inf`、`-1`。

### 2.3 估价逻辑

```python
cost, raw_cost = estimate_cost(key, dataset=..., ...)
```

`estimate_cost()` 内部第一行就是 `_http_get(f"{BASE}/metadata.get_cost?...")`。

**这是本次审计最关键的发现**：所谓"估价"是一次完整的 HTTPS 请求——
DNS 解析、TCP 连接、TLS 握手、携带 `Authorization: Bearer <key>`。
它不下载行情（所以不产生数据费），但它**绝对不是本地计算**。

### 2.4 HTTP 请求入口

```python
def _http_get(url, key, timeout=30.0):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}", ...})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()
```

**问题**：没有任何闸门。任何代码路径都能直接调用它，包括"估价"路径。

### 2.5 网络客户端初始化

不存在客户端对象。`urllib.request.urlopen` 是模块级函数，随用随连。

**问题**：没有可拦截的构造点 → 无法在结构上保证"这次运行不联网"。

### 2.6 账户额度查询

**完全没有实现。** Databento 没有公开的余额查询接口，额度只能在 Portal 看。
这也是为什么"可用免费额度是否真实存在"一直是 UNVERIFIED。

### 2.7 数据请求入口

```python
blob = get_range(key, dataset=..., limit=args.limit)
```

与估价共用 `_http_get`，没有任何"这是付费操作"的独立标记。

### 2.8 异常处理

只捕获 `Blocked`。`urllib.error.HTTPError`/`URLError` 在 `_http_get` 内被转成
`Blocked`。但 `OSError`（写文件失败）、`json.JSONDecodeError`（写 meta 时）
会直接抛出 traceback。

### 2.9 exit code 3 / 5 的触发条件

| 码 | 触发点 | 审计结论 |
|---|---|---|
| 3 | `_key()` 抛出：环境变量缺失或长度 < 8 | 触发点**在**参数校验之前，所以 `--start` 缺失且无凭据时返回 3 而不是 2（顺序错误） |
| 5 | `cost > args.max_cost_usd` | 判定式本身可被 `nan`/`inf` 绕过（见 §3.1） |

---

## 3. v1 缺陷清单

| # | 严重度 | 缺陷 | 后果 |
|---|---|---|---|
| 1 | **高** | `--max-cost-usd nan` → `cost > nan` 恒为 False | **费用上限完全失效**，可直接取数产生扣款 |
| 2 | **高** | `--max-cost-usd inf` 同理 | 同上 |
| 3 | 中 | "估价"必须联网 + 需要凭据，却被称为纯估价 | 无法证明零网络；无法在无凭据环境自检 |
| 4 | 中 | 无模式概念，`--confirm` 是唯一闸门 | 估价与下载共用一条路径，容易误触发 |
| 5 | 低 | `_key()` 先于参数校验 | 缺参数的报错码是 3 不是 2，误导排查 |
| 6 | 低 | `--limit` 无边界（可负、可极大） | 误传导致异常请求 |

### 3.1 第 1/2 条的实测证据

```
cost=0.42 cap=0.0    -> '超过上限'判定 = True    （正常拦截）
cost=0.42 cap=nan    -> '超过上限'判定 = False   ← 绕过
cost=0.42 cap=inf    -> '超过上限'判定 = False   ← 绕过
cost=0.42 cap=-1.0   -> '超过上限'判定 = True
```

`float('nan')` 参与的任何比较都是 False，所以 `if cost > cap: raise` 永远不会触发。
这不是理论问题：`--max-cost-usd nan` 是一个只输三个字母就能造成的越权。

---

## 4. A / B / C 三类的真实归属

审计要求明确区分三件事，这里逐一对应到 v1 和 v2：

| 类 | 定义 | v1 是否存在 | v2 对应模式 |
|---|---|---|---|
| **A** | 纯本地计算估价（无网络、无凭据） | **不存在** | `--estimate-only`（默认） |
| **B** | 向 Databento 查价/查额度（有网络、需凭据） | 存在，且被当成"估价" | `--estimate-online` |
| **C** | 实际请求历史数据（有网络、需凭据、产生费用） | 存在 | `--fetch` |

**必须写清楚的一点**：v1 的 `metadata.get_cost` 属于 **B**。
它"免费"仅指不产生数据费用，不代表不联网。上一轮报告把它描述为
"官方免费的估价接口"在字面上没错，但在"是否联网"这个维度上是含混的——
本轮审计把这一点纠正，并回改了那份报告（见 §9）。

---

## 5. v2 的结构性门禁

### 5.1 三档互斥

```python
mode = ap.add_mutually_exclusive_group()
mode.add_argument("--estimate-only", ...)   # A，缺省
mode.add_argument("--estimate-online", ...) # B
mode.add_argument("--fetch", ...)           # C
```

同时给两个模式 → argparse 直接 `exit 2`。默认是 A（最安全的一档）。

### 5.2 A 档的四条硬保证

1. **不读凭据**：`_key()` 只在 B/C 分支调用；A 分支连 `os.environ` 都不碰。
   测试用 `monkeypatch.setattr(probe, "_key", ...)` 断言"一次都没被调用"。
2. **不构造客户端**：`HttpClient.__init__` 检查全局闸门 `_NETWORK_ENABLED`，
   为 False 时抛 `NetworkForbidden`。A 档下闸门恒为 False。
3. **`_http_get` 是死代码**：A 分支不引用它。测试把它替换成"一调用就炸"的桩，
   断言零调用。
4. **只做算术**：`local_estimate()` 只读价格表与 `--expected-gb`，做一次乘法。

### 5.3 价格无法核实就 UNKNOWN_COST

`load_price_book()` 要求**同时**满足：

- 文件存在且可解析
- `verified is True`（严格等于布尔 true）
- `source` 非空
- `unit_price_usd` 有限且 > 0
- `unit ∈ {per_gb, per_1000_records}`
- `dataset` 与请求一致（非空时）

任一不满足 → `UNKNOWN_COST`，exit 7。再叠加 `--expected-gb` 缺失/非法 → 同样 7。

**本机没有可核实单价，所以 A 档的实际输出就是 `UNKNOWN_COST`。** 这是正确结果：
编一个价格让人以为"很便宜"，正是超支的起点。

### 5.4 C 档的三重授权

```
--fetch                      → 明确声明"我要下载"（不再由 --confirm 顺带触发）
--max-cost-usd > 0           → 默认 0 表示未授权任何花费
--confirm                    → 二次确认
```

三者缺一即拒，且**检查顺序在最前面**——不满足授权就不取凭据、不构造客户端。
（v2 初稿曾把客户端构造放在上限检查之前，被本轮测试抓出来并前移。）

### 5.5 上限的四道锁

```python
def _validate_cap(raw):
    if not math.isfinite(cap):  raise CAP_INVALID          # nan / inf
    if cap < 0:                 raise CAP_INVALID          # 负数
    if cap > HARD_CAP_USD:      raise CAP_ABOVE_HARD_LIMIT # 硬顶 25
```

外加：**完全忽略 `DATABENTO_MAX_COST_USD` / `MAX_COST_USD` 等环境变量**
（`IGNORED_ENV_VARS` 常量把这些名字显式列出来，便于审计时一眼看到）。
提高上限的唯一方式是改代码里的 `HARD_CAP_USD`，那是明确的人工动作。

---

## 6. 网络调用证据

不是"日志里没写联网"，而是**把网络栈的入口全部换成会炸的桩**，再跑 A 档。

拦截点（`tests/test_databento_fee_gate.py::NetSpy`）：

| 拦截目标 | 覆盖的协议层 |
|---|---|
| `socket.getaddrinfo` | DNS 解析 |
| `socket.socket` | TCP/UDP socket 创建 |
| `socket.create_connection` | 高层连接建立 |
| `socket.socketpair` | 本地套接字 |
| `ssl.SSLContext.wrap_socket` | TLS 握手 |

同时把 `probe._http_get` 与 `probe.urllib.request.urlopen` 替换成"一调用就抛错"。

实测结果：

| 场景 | 退出码 | 网络调用次数 | 凭据读取 |
|---|---|---|---|
| A 档，无凭据，有价格表 | 0 | **0** | 未读取 |
| A 档，**有**凭据（环境里存在） | 0 | **0** | 未读取（断言 `_key` 零调用） |
| A 档，无价格表 | 7（UNKNOWN_COST） | **0** | 未读取 |
| B 档，无凭据 | 3（NO_CREDENTIAL） | **0** | 尝试读取后拒绝，未联网 |

真实执行留痕（已入库）：

```
verification/databento/raw/probe-blocked.json
  mode = estimate-only, network_allowed = false, blocked = UNKNOWN_COST

verification/databento/raw/network-mode/probe-blocked.json
  mode = estimate-online, network_allowed = true, blocked = NO_CREDENTIAL
```

第二份是关键对照：即使 `network_allowed=true`，没有凭据时**也没有发出任何请求**——
因为 `_key()` 在 `HttpClient` 构造之前就失败了。

---

## 7. 费用门禁测试

`tests/test_databento_fee_gate.py` 从 12 项扩到 **42 项**。

| 要求 | 对应用例 | 结果 |
|---|---|---|
| 1. 无凭据运行估价 | `test_estimate_without_credential_is_local_only` | PASS |
| 2. 有凭据运行估价 | `test_estimate_with_credential_still_does_not_read_it` | PASS |
| 3. 网络客户端构造被禁止 | `test_http_client_cannot_be_constructed_when_gate_closed`、`test_estimate_mode_never_constructs_client` | PASS |
| 4. HTTP 请求被禁止 | `test_estimate_without_credential_is_local_only`（`no_http` 桩） | PASS |
| 5. DNS/TCP 没有发生 | `test_estimate_mode_does_no_dns_or_tcp`（5 个拦截点） | PASS |
| 6. 未知价格 → UNKNOWN_COST | `test_no_price_book_is_unknown_cost` + 6 项参数化（未 verified/无 source/0/负数/单位非法/数据集不符） | PASS |
| 7. 上限 0 拒绝实际请求 | `test_fetch_refused_when_cap_is_zero` | PASS |
| 8. 超上限拒绝 | `test_fetch_refused_when_cost_exceeds_cap` | PASS |
| 9. 估价模式不能切到下载 | `test_estimate_mode_cannot_escalate`（4 项参数化） | PASS |
| 10. 不输出凭据 | `test_secret_never_printed_or_written`（stdout/stderr/全部产出文件） | PASS |
| 附加：nan/inf/负数/硬顶 | `test_nan_cap_cannot_bypass`、`test_invalid_cap_rejected`、`test_cap_above_hard_limit_rejected` | PASS |
| 附加：环境变量无效 | `test_env_var_cannot_raise_cap` | PASS |
| 附加：B 档不取数 | `test_online_estimate_does_not_fetch_data` | PASS |
| 附加：闸门运行后复位 | `test_network_gate_is_closed_after_each_run` | PASS |

### 7.1 全量回归

```
1320 passed / 0 failed / 0 errors / 1 skipped   (r13.xml, 168.5 s)
上一轮 1290 → +30
```

`+30` 全部来自费用门禁用例的扩充（12 → 42）。

### 7.2 重写时曾丢掉 3 条错误路径用例（已补回）

第一版 v2 测试写完后我核对了一遍"旧用例是否都有对应"，发现有 3 条被漏掉：

| 旧用例 | 遗漏原因 | 现状 |
|---|---|---|
| 估价响应不是 JSON | 新 FakeClient 只会返回合法 JSON | 已补，参数化为 4 种畸形响应 |
| 响应里没有 `cost` 字段 | 同上 | 已补 |
| `cost` 不是数字 | 同上 | 已补（另加 `inf` → `COST_NOT_FINITE`） |

同时 `fetch` 写盘用例的断言被削弱成"文件存在"，丢掉了 SHA256 校验。
已恢复为：字节完全一致 + `sha256` 匹配 + `rows` + `field_presence` 计数。

这类"重写顺便丢测试"是最难自查的——因为测试数从 12 涨到 37，
总数看起来是增加的，只有逐条比对旧用例才看得出缺口。

### 7.3 本轮抓到的自身缺陷

写测试时发现自己 v2 初稿也有一个问题：**`HttpClient` 在上限检查之前就被构造**。
`--fetch --max-cost-usd 0` 时会先建客户端再拒绝——虽然不会联网，
但语义上等于"先打着火再问要不要出门"。已把授权检查前移到凭据读取之前，
并由 `test_fetch_refused_when_cap_is_zero` 断言 `instances == []`。

---

## 8. 退出码约定（v2）

| 码 | 含义 | 触发档位 |
|---|---|---|
| 0 | 成功 | A/B/C |
| 2 | 用法错误 / 模式冲突 / 上限非法 | 全部 |
| 3 | 缺少凭据 | B/C |
| 4 | 网络或响应异常 / 估价不可解析 | B/C |
| 5 | 未授权花费 或 估价超过上限 | C |
| 6 | 缺少 `--confirm` | C |
| 7 | `UNKNOWN_COST`：本地无可用价格 | A |

---

## 9. 文档一致性修正

上一轮报告 `DATABENTO_REAL_HISTORICAL_SAMPLE_REPORT.md` §2.2 描述的是 v1 流程
（"1.读凭据 → 2.metadata.get_cost → 3.上限 → 4.confirm"），并把它称为
"结构上保证 0 美元"。这在"是否联网"维度上是含混的，本轮已回写：

- 明确指出 v1 的 `metadata.get_cost` 属于 **B 类**（联网 + 凭据）；
- 换成 v2 的三档表；
- 补记 `nan` 绕过漏洞及其修复；
- 测试数从 12 改为 37，说明联网证明改用 socket 拦截。

保留原文的修改痕迹（引用块说明"本节已被审计推翻"），而不是静默改写历史。

---

## 10. 未解决 / 仍需人工确认

| 项 | 状态 | 说明 |
|---|---|---|
| Databento 账户可用额度 | **UNVERIFIED** | 无公开余额查询接口，只能登录 Portal 看 |
| 是否有 `XNAS.ITCH` 的交易所授权 | **UNVERIFIED** | 通常需另购，账户是否已含未知 |
| 真实单价 | **UNKNOWN_COST** | 本机无可核实价格；A 档因此返回 7，不猜 |
| 估价与实扣的一致性 | UNVERIFIED | 需一次真实请求才能比对（本轮不允许） |
| 消息签名 | BLOCKED（外部） | 与上轮结论一致，供应商未提供 |

---

## 11. 是否可授权购买

**技术上已就绪，金额上仍未知。**

现在可以安全执行的一步（不花钱）：

```powershell
# B 档：只查价，不下载。需要有凭据。
$env:DATABENTO_API_KEY = "<从 Portal 复制>"
python tools/databento_probe.py --estimate-online `
  --dataset XNAS.ITCH --schema status --symbols AAPL `
  --start 2026-11-10T14:30:00Z --end 2026-11-10T14:35:00Z
# 输出里有 usd 与 within_cap；不产生数据费
```

看到金额后再决定是否授权：

```powershell
# C 档：真正取数。金额必须写进 --max-cost-usd，且 ≤ 25（硬顶）。
python tools/databento_probe.py --fetch --confirm `
  --max-cost-usd <上面看到的金额> ...
```

**注意**：B 档虽然是"只查价"，但它会真实联网并把 API Key 发给 Databento。
在你不确定是否要接这家供应商之前，连 B 档也不必跑——A 档（默认）已经足够
证明本地逻辑是自洽的。

---

## 12. 交付清单

| 文件 | 变化 |
|---|---|
| `tools/databento_probe.py` | 重写为三档（218 → 531 行） |
| `tests/test_databento_fee_gate.py` | 12 → 42 项，socket 拦截 |
| `verification/databento/raw/probe-blocked.json` | A 档 UNKNOWN_COST 证据 |
| `verification/databento/raw/network-mode/probe-blocked.json` | B 档 NO_CREDENTIAL 证据 |
| `verification/databento/raw/synthetic-demo/…` | 合成价格书演示（明确隔离标注） |
| `DATABENTO_REAL_HISTORICAL_SAMPLE_REPORT.md` | §2.2 回写为 v2 |
| `r13.xml` | 全量 JUnit |

交易开关复核（全部保持关闭）：

```
us_phase2.enabled            = false
paper_runtime.enabled        = false
market_data_providers.enabled= false
short.mode                   = disabled
```

实际扣款：**0 美元**。未发起任何网络请求，未购买任何服务。
