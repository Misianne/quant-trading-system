# Databento Provider 端到端测试报告

- 项目：`C:\FosunAI-Trading`
- 基线 `6e0c4ef` → 本轮 `143ff1c`
- 全量结果：**1290 通过 / 0 失败 / 0 错误 / 1 跳过**（`r12.xml`，169 s）
- 上一轮基线：1169 → **+121**

---

## 0. 结论

链路已打通，且打通的证明方式是**拒绝能力**：

```
Databento 原始行 → DatabentoProvider.parse → 质量判定
   → ProviderFeed（按 symbol×exchange 分桶）
   → AuthorityRouter（身份/覆盖/绑定/方向/签名）
   → QuoteGateway（时段/新鲜度/跳变）
   → PaperBroker（成交门禁）
```

| 能力 | 证明方式 | 结果 |
|---|---|---|
| 合格行情能进网关 | 两条成交流 → `gw.price()` 取到 201.35 | PASS |
| 停牌期间拒绝出价 | 停牌后 `gw.price()` 抛 `QuoteUnavailable` | PASS |
| 停牌期间不产生成交 | 拿不到价 → 不产生订单；账本为空 | PASS |
| 未认证恢复不能解锁 | 第 6 次 poll 含 `TRADING` 事件，`halted` 仍为 True | PASS |
| 旧数据不能覆盖新值 | 迟到 5 分钟的事件被拒 | PASS |
| 重复消息被标记 | `duplicate_every=1` 注入，`health.duplicate ≥ 1` | PASS |
| 乱序被拒 | `swap_pairs=True` 注入，`rejected["late_event"] ≥ 1` | PASS |
| 过期行情 fail-closed | `max_age=1s` → 抛错 | PASS |
| 同输入结果一致 | 3 次运行 digest 完全相同 | PASS |

---

## 1. 实际调用链（代码级）

### 1.1 Provider 层

```
DatabentoProvider.parse(item)
├── _rtype_of()        rtype(18/0/1) → 或回退 type/schema 字符串
├── _symbol_of()       symbol 字段 → 否则 symbology[instrument_id] → 否则计 MISSING_SYMBOLOGY
├── _times_of()        ts_event / ts_recv（ns→UTC，整数运算）→ 返回时区告警
├── venue_of()         publisher_id → 72 项映射；合并发布者单独计数
├── DataQuality        时区缺失 → NAIVE_TIME
└── 分支
    ├── rtype 18 → HaltEvent(action→halted, reason, is_trading, is_quoting, ssr, trading_event)
    ├── rtype 0  → TradeEvent(price 按类型定标度, size, side, sequence)
    ├── rtype 1  → QuoteEvent(levels[0] …)
    └── 其它     → 计 schema_unsupported:<name>
```

### 1.2 汇聚层

```
ProviderFeed.poll()
├── provider.poll()
│   ├── _poll_raw()        读记录（含 duplicate/swap/drop/down 注入）
│   ├── _normalize()       parse() + 异常计数
│   └── _apply_quality()   序号守卫 / 新鲜度(按 event_time) / 未来时间 / naive
├── for ev: if not ev.quality.usable → dropped + 按标记计数
└── _ingest(ev)
    ├── 迟到事件（event_time < 已记录）→ 拒绝，计 late_event
    ├── TradeEvent  → st.last_trade
    ├── HaltEvent   → _can_authorize() 通过才写 st.halt
    └── st.updated_at = ev.event_time（不是 receive_time）
```

### 1.3 权威层

```
AuthorityRouter.can_apply(ev, symbol, exchange, now, version)
 1  未注册 provider           → PROVIDER_NOT_REGISTERED
 2  非交易所原生              → NOT_NATIVE
 3  事件是 VENDOR_QUOTE       → VENDOR_EVENT_CANNOT_DRIVE_STATE
 4  类型与声明不符            → AUTHORITY_MISMATCH
 5  未覆盖该交易所            → EXCHANGE_NOT_COVERED
 6  交易所不一致              → EXCHANGE_MISMATCH
 7  标的不一致                → SYMBOL_MISMATCH
 8  Nasdaq 上市限定           → NASDAQ_LISTED_ONLY
 9  事件来自未来              → EVENT_IN_FUTURE
10  事件过旧                  → EVENT_TOO_OLD
11  版本回退                  → VERSION_STALE
12  签名无效                  → SIGNATURE_INVALID
13  方向为放大且未签名        → NO_SIGNATURE      ← 本轮改为**仅放大方向**检查
```

第 13 步就是本轮最重要修复：早期实现是对称的，会连"停牌"一起拒掉。

---

## 2. 修复的缺陷（测试视角）

| # | 缺陷 | 复现测试 |
|---|---|---|
| 1 | `type` 字符串分派（真实是 `rtype`） | `test_rtype_numeric_dispatch` / `test_unknown_rtype_is_rejected_with_reason` |
| 2 | 依赖 `symbol` 字段（真实没有） | `test_symbol_resolved_from_symbology_map` / `test_missing_symbology_fails_closed_and_is_counted` |
| 3 | `action == "H"`（真实是整数 8） | `test_pause_maps_to_halted` / `test_all_halting_actions`（3 参） |
| 4 | 浮点价二次除 1e9 | `test_pretty_price_not_divided_again` |
| 5 | `publisher_id` 当字符串 | `test_publisher_id_maps_to_exchange` / `test_unknown_publisher_counted_not_guessed` |
| 6 | 解析异常被吞 | `test_unknown_rtype_is_rejected_with_reason`（断言 `dropped/parse_errors`） |
| 7 | 浮点除法丢 1µs | `test_nanosecond_precision_preserved` |
| 8 | naive 时间戳静默当 UTC | `test_naive_timestamp_is_flagged_not_guessed` |
| 9 | `receive_time` 被覆盖 | `test_event_time_and_receive_time_are_distinct` |
| 10 | 非停牌状态污染 `dropped` | `test_non_halt_status_does_not_create_fake_halt` |
| 11 | 未知 action 被丢弃 | `test_unknown_action_forces_halt_instead_of_dropping` |
| 12 | MBP-1 买卖价读错位置 | `test_mbp1_reads_levels_array` |
| 13 | **签名门禁方向对称** | `test_unsigned_halt_accepted` / `test_unsigned_resume_denied` |

第 13 条有专门的回归意义：如果哪天有人把门禁改回对称，这两条会同时变红——
一条因为"停牌被拒"，一条因为"恢复被放行"。

---

## 3. 停牌期间成交门禁（C 项）

这是本轮唯一能给出 PASS 的安全项，因为它**不依赖真实数据**。

### 3.1 样本里确实有停牌期间的成交

`synthetic_luld_pause.jsonl` 的时间线：

```
14:30:00  trades  201.20  seq 1001
14:30:01  trades  201.15  seq 1002
14:30:02  status  PAUSE / LULD_PAUSE   is_trading=N
14:30:10  trades  200.90  seq 1003     ← 停牌期间仍有成交（迟到打印）
14:35:00  status  TRADING              ← 未签名，被拒
14:35:01  trades  201.00  seq 1004
```

### 3.2 三道独立防线

| 防线 | 测试 | 行为 |
|---|---|---|
| 网关 | `test_halt_blocks_gateway_price` | `QuoteGateway.quote()` → `QuoteUnavailable` |
| 策略 | `test_no_order_and_no_fill_during_halt` | 取不到价 → 不产生订单 → 账本为空 |
| 券商 | `test_broker_gate_blocks_fill_even_if_forced` | 即使强行下单，`PaperBroker` 成交门禁拒绝 |

第三条用的是 `MarketStatusService` 表达的交易所停牌事实，与 provider 层是**两个
独立防线**。这样设计是故意的：任何一层单独失效，都不应该导致成交。

### 3.3 未签名恢复不能解锁

```
poll 0..5 逐步摄取
最终 state.halted = True
feed.rejected["halt_not_authoritative"] ≥ 1
authority.audit 中出现 [NO_SIGNATURE]
```

对比用例：注入 `Verified` 验证器后，同一样本的 `halted` 变为 `False`——
说明"恢复被拒"确实是因为缺签名，不是因为别的原因碰巧挡住了。

---

## 4. 数据质量：注入式验证

| 注入 | 参数 | 期望 | 实际 |
|---|---|---|---|
| 重复投递 | `duplicate_every=1` | 标 `DUPLICATE`，不入账两次 | PASS |
| 成对乱序 | `swap_pairs=True` | 计 `late_event` | PASS |
| 断线 | `down_at={n}` | 抛 `PROVIDER_DOWN` | PASS（既有用例） |
| 丢包 | `drop_indexes={n}` | 序号缺口 → `GAP` | PASS（既有用例） |
| 迟到事件 | 手工构造 5 分钟前的事件 | 不覆盖新值 | PASS |
| 过期 | `max_age_seconds=1` | 抛 `FeedUnavailable` | PASS |

注意：`test_out_of_order_cannot_overwrite_newer` 直接调 `_ingest`，绕过了 provider
的序号守卫——因为要验证的是**汇聚层自身**也有防线，不能只靠上游。

---

## 5. 确定性

同一份样本、同一配置，重复 3 次：

```
verification.json → determinism
  runs       = 3
  distinct   = 1
  identical  = true
  sha256     = e8968536…（前 8 字节）
```

digest 覆盖：全部事件 `as_dict()`、`halted`、`version`、`feed.rejected`、
provider 健康统计。

为达成确定性，本轮修掉了一个隐藏的非确定性来源：`RecordedProvider` 曾把
`receive_time` 无条件改成 `event_time`。改回保留真实 `ts_recv` 后，确定性依然成立
（因为样本里的 `ts_recv` 本来就是固定值），但延迟信息不再丢失。

---

## 6. 测试清单

| 文件 | 函数数 | 用例数 | 覆盖 |
|---|---|---|---|
| `test_databento_schema.py` | 19 | 32 | 官方常量逐条对照（含 11 项参数化的 action 归一） |
| `test_databento_provider_parse.py` | 32 | 34 | 字段/标度/时间/状态语义/缺失字段/fail-closed |
| `test_databento_authority_gate.py` | 22 | 22 | 13 步判定 + 方向不对称 + 汇聚层结果 |
| `test_databento_replay_e2e.py` | 11 | 11 | 网关/券商/质量/确定性 |
| `test_databento_fee_gate.py` | 12 | 12 | 费用门禁（离线桩） |
| **合计** | **96** | **111** | |

全量增量是 **+121** 而不是 +111，差出来的 10 项来自既有文件：

| 文件 | 增量 | 来源 |
|---|---|---|
| `test_provider_authority.py` | +2 | 新增不对称门禁用例（split 原有用例并补 2 条） |
| `test_provider_adapters.py` | +1 | 原"虚构 source_event_id"用例拆成 2 条 |
| `test_provider_fixture_e2e.py` | +1 | 原"未签名 halt 被忽略"改为"停牌生效 + 恢复被拒"两条 |
| `test_provider_models_normalize.py` | +6 | **非手写**：该文件有一条 `parametrize("flag", list(QualityFlag))`，本轮新增 6 个质量标记，自动多出 6 个用例 |

### 6.1 顺带修掉的测试质量问题

跑全量时发现 4 处测试本身有缺陷（不是代码的问题）：

| 测试 | 问题 | 修法 |
|---|---|---|
| `test_provider_fixture_e2e`（4 个用例） | 样本固定在 2026-11-16，却依赖真实墙钟判新鲜度——**写的时候通过，隔天全变"过期"** | 加 autouse 夹具把进程时钟钉在样本时间线上 |
| `test_provider_quality::test_feed_quote_returns_latest_trade` | 同上 | 该用例只验证"取最新"，放开新鲜度阈值 |
| `test_us_luld_authority_attacks::test_aggregation_only_tightens_never_loosens` | `status()` 未传 `now`，隔天变 `DATA_STALE` | 显式传时间线内的 `now` |
| `test_databento_source_event_id` | 断言从**虚构的** `id` 字段取事件 ID | 改为断言"官方无此字段，能力声明为 False" |

这一类问题值得单独记一笔：**用固定历史时间戳 + 真实墙钟，是一个会延迟一天才
爆发的定时炸弹**。全量回归之所以能抓到，是因为本轮跑在样本日期之后。

---

## 7. 判定

| 项 | 判定 | 依据 |
|---|---|---|
| **C. 停牌期间成交门禁** | **PASS** | 三道独立防线全部验证；样本中确有停牌期成交，未产生任何虚假成交；未签名恢复无法解锁 |
| **D. 官方恢复权限认证** | **BLOCKED** | 门禁代码正确（不对称、需签名），但外部**不存在签名体系**，无法完成"认证"这件事本身 |
| **E. 真实行情实时订阅准备** | **BLOCKED** | 需凭据 + 交易所授权；本轮禁止建立 WebSocket，未验证重连与回补 |

C 能给 PASS 是因为它**不需要真实数据**：门禁逻辑与数据来源无关。
D/E 不能给 PASS 是因为它们**本质需要外部条件**，不是写代码能补的。

---

## 8. 剩余风险

| 风险 | 影响 | 当前缓解 |
|---|---|---|
| 真实 JSON 的枚举表示可能同时出现整数与字符 | 解析分支走错 | 两条路径都实现并各有用例 |
| `status` 无 `sequence`，无法用序号去重 | 重复的 status 事件会被重复处理 | 幂等靠 `event_time` + 版本；`HaltEvent` 重复写同一状态无副作用 |
| 合并发布者（`publisher_id=47` 等）混入 | 交易所字段失真 | 单独计数 `consolidated_publisher` |
| 未知 `publisher_id` 落到 `unknown` | 交易所级隔离失效 | 计 `unknown_publisher`；`AuthorityRouter` 的 `EXCHANGE_NOT_COVERED` 兜底 |
| 停牌期间成交被当作"已恢复"的信号 | 提前解锁 | `_ingest` 只让 `HaltEvent` 改 `halt` 状态，成交不改 |
| 真实流量下 `ts_recv` 远晚于 `ts_event` | 延迟统计失真 | 两者分开保存，新鲜度只用 `ts_event` |

---

## 9. 复现方式

```powershell
# 生成样本（已入库，通常不需要重跑）
python verification/databento/gen_fixtures.py

# 生成证据（报告里所有数字的来源）
python verification/databento/run_verification.py

# 只跑本轮新增
python -m pytest tests/test_databento_*.py -q

# 全量
python -m pytest tests/ -q --junitxml=r12.xml
```

`verification/databento/out/verification.json` 的 SHA256 前 32 位：
`7d6851ba71a66333f8c59f9d1130acdd`。
