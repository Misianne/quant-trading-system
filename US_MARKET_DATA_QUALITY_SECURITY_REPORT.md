# 行情质量与安全报告（US Market Data Quality & Security）

**项目**：`C:\FosunAI-Trading`
**范围**：数据质量门禁、权威事件认证、威胁模型
**日期**：2026-11-16

---

## 1. 数据质量规则

每条事件在进入 `ProviderFeed` 之前都会被打上质量标记；**带阻断标记的事件一律丢弃**，
不会进入 `QuoteGateway`，更不会影响风控。

| 标记 | 触发条件 | 阻断 | 检出方式 |
|---|---|---|---|
| `DUPLICATE` | 序号 == 上一条 | ✅ | 序号守卫 |
| `OUT_OF_ORDER` | 序号 < 上一条 | ✅ | 序号守卫 |
| `SEQUENCE_REGRESSION` | 同上（独立标记，便于区分"迟到"与"回退"） | ✅ | 序号守卫 / `_ingest` |
| `GAP` | 序号 > 上一条 + 1 | ✅ | 序号守卫（记录缺口区间） |
| `STALE` | `now - event_time > max_age` | ✅ | 质量判定 |
| `FUTURE` | `event_time > now + max_future` | ✅ | 质量判定 |
| `NAIVE_TIME` | 时间戳无时区 | ✅ | 时间解析 |
| `UNKNOWN_SYMBOL` | 代码为空/无法归一 | ✅ | 解析 |
| `EXCHANGE_MISMATCH` | 事件交易所与期望不符 | ✅ | 权威路由 |
| `PROVIDER_DOWN` | 连接断开 / 主动抛错 | ✅ | `poll()` 直接抛 `ProviderUnavailable` |
| `NO_AUTHORITY` | 权威不足（保留） | ✅ | 权威路由 |

### 1.1 多交易所隔离

序号守卫的键是 **`(symbol, exchange)`**，不是 `symbol`：

```python
def check(self, symbol, exchange, seq):
    key = (symbol.upper(), exchange.lower())
```

同一代码在 Nasdaq 与 NYSE 各有序号空间；混用会把正常事件误判成"序号回退"。
测试 `test_sequence_isolated_per_exchange` 锁死该行为。

### 1.2 旧数据不得覆盖新数据

```python
if st.updated_at is not None and ev.event_time < st.updated_at:
    ev.quality.add(QualityFlag.OUT_OF_ORDER, "迟到事件")
    self.dropped += 1
    return
```

**这条是实测发现的缺陷**：原实现直接按到达顺序覆盖，晚到的旧成交会把最新价改回旧值。
测试 `test_feed_does_not_let_old_overwrite_new` 复现并锁死。

### 1.3 新鲜度按事件时间，不按接收时间

```python
st.updated_at = ev.event_time      # 市场发生时刻
st.received_at = ev.receive_time   # 仅用于延迟统计
```

若按接收时间判新鲜度，供应商把两小时前的数据"现在推过来"就会被当成最新行情。
这是第二个实测修复点（测试 `test_feed_quote_refuses_when_stale`）。

### 1.4 录制回放的时间语义

实时 provider 用墙钟判新鲜度；**录制样本必须用事件时间线**，否则历史样本会被判成
"过期两年"或"来自未来"。`MarketDataProvider._quality_now()` 提供这个切换点，
`RecordedProvider` 用已投递事件的最大 `event_time` 作为"现在"。

---

## 2. 时间处理

| 规则 | 实现 |
|---|---|
| 内部一律 UTC | `parse_timestamp()` 返回 `astimezone(timezone.utc)` |
| 保留原始时间 | `raw_time` 原样保存（不改写、不格式化） |
| 单位必须显式 | `unit ∈ {ns, ms, s, us, iso}`；`auto` 直接报错 |
| naive 时间不猜时区 | 按 UTC 占位 + 打 `NAIVE_TIME` 标记（阻断） |
| 美东换算 | `to_market_time()` 用 `zoneinfo`，DST 自动 |
| 时段判断 | `session_phase_of()` 仅用于展示/质量；交易门禁仍走 `ExchangeClock` |

**为什么禁止"按位数猜单位"**：10 位=秒、13 位=毫秒、19 位=纳秒这条经验规则，
在 2001 年以前的毫秒数据上会把 1970 年算成 2038 年。错单位必须暴露，
而不是"看起来能跑"。

---

## 3. 权威事件认证

### 3.1 四类事件

```
A 普通行情（含供应商 halt 标记）  → 永远不能驱动交易状态
B 交易所官方停复牌                → 只能驱动该交易所
C LULD 官方状态/价格带            → 只能驱动覆盖的标的
D MWCB 官方全市场状态             → 全市场
```

分类入口：`AuthorityRouter.classify(provider, raw_type, payload)`。
**默认返回 A**；只有注册为 `native=True` 的 provider 才可能升到 B/C/D。

### 3.2 判定顺序（缺一即拒）

1. provider 是否注册（`PROVIDER_NOT_REGISTERED`）
2. 是否交易所原生（`NOT_NATIVE`）
3. 是否 A 类（`VENDOR_EVENT_CANNOT_DRIVE_STATE`）
4. 事件类别与 provider 声明是否一致（`AUTHORITY_MISMATCH`）
5. provider 是否覆盖该交易所（`EXCHANGE_NOT_COVERED`）
6. 事件交易所是否匹配（`EXCHANGE_MISMATCH`）
7. 事件代码是否匹配（`SYMBOL_MISMATCH`）
8. Nasdaq 专用事件是否用在别的交易所（`NASDAQ_LISTED_ONLY`）
9. 事件时间是否来自未来（`EVENT_IN_FUTURE`）
10. 事件是否过期（`EVENT_TOO_OLD`）
11. 版本是否单调（`VERSION_STALE`，防重放）
12. 签名是否有效（`SIGNATURE_INVALID`）
13. 未签名是否被允许（`NO_SIGNATURE`，默认拒绝）

### 3.3 签名：必须诚实

**本轮调查结论：Massive / Alpaca / Databento / Nasdaq / NYSE 的自助接口都不提供
对 halt/resume 消息的密码学签名。**

因此：

```python
class NoSignatureVerifier:
    def verify(self, ev, raw):
        return SignatureStatus.UNSIGNED, "供应商未提供消息签名（本轮调查结论）"

AuthorityRouter(..., require_signature_for_resume=True)   # 默认
```

后果：**任何未签名事件都不能恢复交易**，即使 provider 是交易所原生、即使事件内容看起来正确。
这条规则让"接入行情"和"能自动恢复交易"解耦：

* C 真实行情接入层 → 可以推进到 PARTIAL/PASS；
* D 交易所权威事件认证 → **BLOCKED**，直到有真正的签名/证书体系。

想放开必须显式注入验证器并改配置，不能"忘了配就默认放行"。

### 3.4 防伪造 / 防重放

| 攻击 | 防御 | 测试 |
|---|---|---|
| 拿 MSFT 的事件恢复 AAPL | `SYMBOL_MISMATCH` | `test_symbol_mismatch_forged` |
| 拿 NYSE 的事件恢复 Nasdaq 标的 | `EXCHANGE_MISMATCH` / `EXCHANGE_NOT_COVERED` | 2 项 |
| 拿 Nasdaq 专用指标恢复 NYSE 标的 | `NASDAQ_LISTED_ONLY` | `test_nasdaq_listed_only_blocks_other_exchanges` |
| 重放旧的恢复事件 | `VERSION_STALE` | `test_replayed_resume_rejected_by_version` |
| 伪造未来时间 | `EVENT_IN_FUTURE` | `test_future_event_rejected` |
| 重放很久以前的事件 | `EVENT_TOO_OLD` | `test_stale_event_rejected` |
| 未注册供应商 | `PROVIDER_NOT_REGISTERED` | 1 项 |
| 供应商自称权威 | `NOT_NATIVE` | 1 项 |
| 把普通行情当官方事件 | `VENDOR_EVENT_CANNOT_DRIVE_STATE` | 1 项 |

---

## 4. 威胁模型（本轮覆盖范围）

| 威胁 | 是否覆盖 | 说明 |
|---|---|---|
| 供应商推送重复消息 | ✅ | 序号守卫 + 去重 |
| 消息乱序/迟到 | ✅ | 序号 + 事件时间双判定 |
| 消息丢失/缺口 | ✅ | 缺口标记（阻断） |
| 断线后继续用旧价 | ✅ | 未连接直接抛错 |
| 时间戳错误（单位/时区） | ✅ | 显式单位 + naive 标记 |
| 旧数据覆盖新数据 | ✅ | `_ingest` 拒绝 |
| 用普通行情冒充官方停牌 | ✅ | 权威路由 |
| 伪造/重放恢复事件 | ✅ | 版本 + 绑定 + 时间窗 |
| **中间人伪造已签名事件** | ❌ **BLOCKED** | 无签名体系，无法防御 |
| **供应商内部作恶** | ❌ **BLOCKED** | 同上；只能靠合同与审计 |
| 交易所自身错误 | ❌ 超出范围 | 需要交易所更正消息 |

---

## 5. 与既有安全体系的衔接

| 既有机制 | 本轮是否改动 | 说明 |
|---|---|---|
| `MarketStatusService`（LULD/MWCB/停牌） | ❌ 未改 | provider 只提供事件，状态机不动 |
| `LuldLifecycle` 生命周期 | ❌ 未改 | — |
| `ResumeAuthority`（LULD 恢复身份验证） | ❌ 未改 | provider 层的 `AuthorityRouter` 是**额外**的一道闸 |
| `PaperBroker` 三阶段门禁 | ❌ 未改 | 停牌期间仍不成交（有测试） |
| `QuoteGateway` 时段/新鲜度 | ❌ 未改 | 只新增了 `provider` 数据源 |

**两层权威校验**：provider 层（事件能不能改状态）+ 交易层（当前状态允不允许成交）。
两层都过才可能成交。

---

## 6. 测试覆盖（安全相关）

| 类别 | 数量 | 文件 |
|---|---|---|
| 权威分类/签名/伪造/重放 | 20 | `test_provider_authority.py` |
| 质量标记/断线/隔离 | 24 | `test_provider_quality.py` |
| 端到端（停牌阻断、旧数据、跨时段） | 12 | `test_provider_fixture_e2e.py` |

关键用例：

* `test_unsigned_event_rejected_by_default` —— 默认拒绝未签名事件；
* `test_unsigned_official_halt_is_ignored` —— 即使 provider 原生，没签名也不能改状态；
* `test_paper_broker_blocks_fill_during_official_halt` —— 停牌期间不成交；
* `test_feed_does_not_let_old_overwrite_new` —— 旧数据不覆盖新数据；
* `test_gateway_rejects_cross_session_use` —— 盘后价不得当常规价。

---

## 7. 结论

**D. 交易所权威事件认证：BLOCKED。**

不是实现没做完，而是**外部条件不成立**：

1. 没有任何自助供应商提供消息签名；
2. Nasdaq/NYSE 原生馈送需要单独授权，且同样没有签名层；
3. MWCB 没有自助 API。

在拿到"可验证的签名/证书 + 明确的权威边界"之前，系统**必须**保持
`require_signature_for_resume=true` 与 `production_authority()` 零注册。
任何试图"先放开、以后再补签名"的做法都会把未经核验的字段伪装成官方事件 —— 本轮明确禁止。
