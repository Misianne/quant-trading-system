# 数据源测试报告（US Data Source Test Report）

**项目**：`C:\FosunAI-Trading`
**日期**：2026-11-16

---

## 1. 全量测试结果

```bash
cd /c/FosunAI-Trading
env -u PYTHONPATH ./.venv/Scripts/python.exe -m pytest tests/ -q \
    --junitxml=r11.xml -p no:cacheprovider
```

| 指标 | 值 |
|---|---|
| 总用例 | **1169** |
| passed | **1168** |
| failed | **0** |
| errors | **0** |
| skipped | **1**（既有跳过项，非本轮新增） |
| 耗时 | 177.4 s |
| 证据 | `r11.xml` |
| 上一轮基线 | 1043（`r10.xml`） |
| 本轮新增 | **126** |

**与 `--collect-only` 交叉验证**：1169 collected = 1169 executed，无收集/执行偏差。

---

## 2. 新增测试明细

| 文件 | 数量 | 覆盖 |
|---|---|---|
| `test_provider_models_normalize.py` | **49** | 8 类事件模型、质量枚举、代码/交易所归一、4 种时间单位、UTC/DST、时段判断 |
| `test_provider_quality.py` | **24** | 序号守卫（重复/回退/缺口/跨交易所隔离）、过期、未来、naive、断线、重连、Feed 丢弃与拒绝 |
| `test_provider_adapters.py` | **21** | 4 家适配器的字段解析、能力声明、时间单位歧义、坏行隔离 |
| `test_provider_authority.py` | **20** | A/B/C/D 分类、9 类拒绝码、签名策略、伪造、重放、审计 |
| `test_provider_fixture_e2e.py` | **12** | 录制样本 → Feed → Gateway → 风控/撮合、确定性、停牌阻断、旧数据、跨时段 |
| **合计** | **126** | — |

---

## 3. 录制样本（Recorded Fixture）

位置：`tests/fixtures/marketdata/`

| 文件 | 内容 | 用途 |
|---|---|---|
| `massive_luld.jsonl` | 6 条：2 次价格带更新、1 笔成交、1 个报价、1 次暂停（17）、1 次恢复（18） | Massive LULD 通道解析 |
| `alpaca_trades.jsonl` | 3 条：2 笔成交 + 1 个报价（AAPL/MSFT） | Alpaca 实时流解析 |
| `databento_status.jsonl` | 3 条：halt(H) → trade → reopen(T) | Databento `status` schema |
| `nasdaq_trading_action.jsonl` | 2 条：Halt / Resume | Nasdaq 官方 Trading Action |
| `bad/naive_ts.jsonl` | 无时区时间戳 | 质量标记 |
| `bad/future_ts.jsonl` | 2030 年时间戳 | 质量标记 |
| `bad/unknown_symbol.jsonl` | 空代码 | 解析拒绝 |

**样本特征**：
* 全部为**脱敏、固定**数据（无真实账户信息、无 Key）；
* 时间戳为构造值，但单位与格式严格按官方文档；
* 同一样本多次运行输出**逐字段一致**（`test_fixture_replay_is_deterministic`）。

### 3.1 确定性验证

```python
def run():
    p = MassiveProvider(fixture, {"batch_size": 10})
    feed = ProviderFeed([p]); feed.connect_all(); feed.subscribe(["AAPL"])
    return [e.as_dict() for e in feed.poll()]

assert run() == run()
```

确定性来自三处：
1. 样本固定；
2. `RecordedProvider` 的 `receive_time` 取事件时间（不是 `datetime.now()`）；
3. 质量判定用事件时间线（`_quality_now`），不用墙钟。

---

## 4. 需求覆盖对照

| 需求（§5） | 测试 | 结果 |
|---|---|---|
| 重复消息 | `test_duplicate_messages_flagged` / `test_sequence_duplicate` | ✅ |
| 乱序消息 | `test_out_of_order_flagged` / `test_sequence_regression` | ✅ |
| 丢失消息 | `test_gap_flagged` / `test_sequence_gap` | ✅ |
| WebSocket 断线与重连 | `test_provider_down_raises` / `test_reconnect_increments_counter` | ✅ |
| 重连后的历史补齐 | 能力声明 `backfill=True`；补齐流程在 Provider 层（`reconnect()` + `_poll_raw`），**真实端点未连** → 标记 PARTIAL | ⚠️ |
| 时间戳异常 | `test_naive_timestamp_flagged` / `test_invalid_timestamp_returns_error` | ✅ |
| UTC 与美东转换 | `test_utc_to_new_york_winter` / `summer` | ✅ |
| DST 切换 | 同上（-5 vs -4） | ✅ |
| 过期报价 | `test_stale_event_flagged` / `test_feed_quote_refuses_when_stale` | ✅ |
| 未来时间戳 | `test_future_event_flagged` / `test_future_event_rejected` | ✅ |
| 错误证券代码 | `test_normalize_symbol_empty` / `test_bad_row_does_not_break_batch` | ✅ |
| 交易所不匹配 | `test_exchange_mismatch` / `test_exchange_not_covered` | ✅ |
| 同一 Symbol 多交易所隔离 | `test_sequence_isolated_per_exchange` / `test_feed_isolated_per_exchange_state` | ✅ |
| 供应商状态与官方状态冲突 | `test_vendor_halt_cannot_set_halted` / `test_unsigned_official_halt_is_ignored` | ✅ |
| 未授权恢复事件 | `test_unregistered_provider_rejected` / `test_non_native_provider_rejected` | ✅ |
| 伪造恢复事件 | `test_symbol_mismatch_forged` | ✅ |
| 重放旧恢复事件 | `test_replayed_resume_rejected_by_version` | ✅ |
| 序列号回退 | `test_sequence_regression` | ✅ |
| 签名错误 | `test_invalid_signature_rejected_even_if_not_required` | ✅ |
| 证书失效 | ❌ **UNVERIFIED**：无证书体系可测，`NoSignatureVerifier` 恒返回 UNSIGNED | ⚠️ |
| 数据源不可用 | `test_disconnected_provider_refuses_poll` / `test_provider_down_raises` | ✅ |

> "重连后的历史补齐"与"证书失效"两项只能标 PARTIAL/UNVERIFIED：
> 前者需要真实端点，后者需要真实证书链。不伪造 PASS。

---

## 5. 回归安全性

| 检查 | 结果 |
|---|---|
| 原有 1043 项 | **全部通过**（0 回归） |
| P0/P1 规则测试 | 通过（`test_us_p0` / `test_us_p1_batch*`） |
| LULD/MWCB 生命周期 | 通过（`test_us_luld_lifecycle` / `test_us_halt_lifecycle` / `test_us_luld_mwcb_rules`） |
| 成交门禁 | 通过（`test_us_luld_authority_attacks` 36 项） |
| Paper Runtime | 通过（`test_paper_runtime_*`） |
| 配置模板一致性 | 通过（新增 `market_data_providers` 段已同步） |

**未删除任何测试、未降低断言、未扩大跳过范围**（skipped 仍为 1，与上一轮相同）。

---

## 6. 测试执行环境

| 项 | 值 |
|---|---|
| Python | 3.13.12（`.venv`） |
| 平台 | Windows 11 |
| 数据库 | 全部使用 `tmp_path` 临时库，不触碰 `data/` 生产账本 |
| 网络 | **不需要**（全部离线，无真实端点） |
| 密钥 | **不需要**（无任何 Key 参与） |

`test_fixture_data_does_not_touch_real_accounts` 显式验证：跑完 provider 测试后
`data/*.db` 数量不变。

---

## 7. 结论

* 新增 **126** 项测试，全部通过；
* 全量 **1169** 项，0 失败、0 错误、1 跳过（既有）；
* 报告数字与 `r11.xml`、`--collect-only` 三方一致；
* 两处能力缺口（重连补齐的真实链路、证书失效）已如实标注，未伪造。
