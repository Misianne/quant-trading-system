# FosunProvider 实现报告

- 项目：`C:\FosunAI-Trading`
- 起始基线：`2622325`（工作树干净）
- 本轮代码提交：`a6cda18`
- 全量测试：**1409 passed / 0 failed / 0 errors / 1 skipped**（`r14.xml`）
- 本轮新增测试：**89**（协议/传输 27、Provider 51、风控门禁 11）
- 真实 API 请求：**0 次**（本机无凭据，见 `US_FOSUN_API_VALIDATION_REPORT.md`）

---

## 0. 十二项验收结论

| # | 问题 | 结论 | 依据 |
|---|---|---|---|
| 1 | FosunProvider 是否真实实现 | **PASS** | `fosun.py` 624 行，实现 `MarketDataProvider` 全部 5 个钩子，51 项测试 |
| 2 | 是否通过现有 QuoteGateway | **PASS** | `test_quote_flows_through_gateway`：feed → QuoteGateway 取到 201.20 |
| 3 | 是否成功查询真实 `usAAPL` 报价 | **BLOCKED** | 无凭据。四个环境变量全部未设置 |
| 4 | 是否证明 API 数据具有实时性 | **BLOCKED** | 依赖 #3；且 `capabilities.realtime=False` |
| 5 | 是否验证 WebSocket 实时推送 | **BLOCKED**（协议层 PASS） | 协议实现、重连/重订阅/缺口检测已实现并测试；未连真实服务 |
| 6 | 是否确认无需额外付费/开通 | **UNKNOWN** | 厂商无公开档位；SDK 的 403 文案指向"需开通行情服务" |
| 7 | 哪些字段没有可信时间戳 | **quote / min / orderbook** | 见 §4 |
| 8 | 四段时段覆盖程度 | **UNKNOWN** | 仅 SDK 注释声明；未实测 |
| 9 | 是否仍缺 LULD/MWCB 官方状态 | **是，仍缺** | 协议中不存在；`luld_events=False`、`mwcb_events=False` |
| 10 | 全量测试结果 | **1409 / 0 / 0 / 1** | `r14.xml` |
| 11 | Git 提交号 | **`a6cda18`** | `git log -1` |
| 12 | 未完成事项 | 见 §9 | 全部由"无凭据"派生 |

---

## 1. 为什么是"独立实现协议"而不是"安装 SDK"

官方仓库 `fosunwealth/openapi-python-sdk` **没有 LICENSE**（根目录无 LICENSE 文件、
GitHub `license` 字段为 `null`、`setup.py` 无 `license=`），且未发布到 PyPI。

因此本轮**没有**：

- 把 SDK 复制进项目
- 把 SDK 加入 `requirements` / `pyproject`
- `pip install -e` 官方仓库

而是按公开可见的协议约定，自己实现最小必要部分。协议（字节布局、签名原文、
KDF 参数）属于互操作事实，不是著作权客体；实现代码是本项目自写的。

**边界声明**：这不构成法律意见。若要把本项目对外分发，仍需法务确认协议实现
是否受任何专利/条款约束。

---

## 2. 交付文件

| 文件 | 行数 | 职责 |
|---|---|---|
| `src/ai_trader/market/providers/fosun_protocol.py` | 266 | 协议：ECDH/HKDF/ECDSA/HMAC/AES-GCM |
| `src/ai_trader/market/providers/fosun_transport.py` | 435 | 超时/重试/限流/WS 重连/订阅恢复/缺口检测 |
| `src/ai_trader/market/providers/fosun.py` | 614 | `FosunProvider` + 解析器 + 快照类型 |
| `src/ai_trader/market/providers/__init__.py` | +6 | 导出 |
| `tests/test_fosun_protocol_transport.py` | 27 用例 | 协议与传输 |
| `tests/test_fosun_provider.py` | 51 用例 | 解析与质量 |
| `tests/test_fosun_risk_gate.py` | 11 用例 | 风控门禁（一票否决） |
| `r14.xml` | — | JUnit 证据 |

既有代码**零修改**（除了 `__init__.py` 的导出）。`QuoteGateway`、`feed.py`、
`authority.py`、`models.py`、`PaperBroker`、`MarketStatusService`、LULD、Replay
均未改动——这是"复用而不是另造"的直接证据。

---

## 3. 能力声明：那一串 `False` 是安全闸门

```python
provider="fosun"
exchanges=("fosun",)          # 合成交易所
realtime=False                # 未实测，不声明
trades=True
quotes=False                  # quote 无事件时间
order_book=False              # orderbook 无事件时间
halt_events=False             # 协议中不存在
halt_events_native=False
luld_events=False
mwcb_events=False
exchange_field=False
sequence_field=False
source_event_id=False
```

为什么重要：`AuthorityRouter.can_apply()` 第一层查 provider 是否登记，
`supports_authoritative()` 查 `halt_events and halt_events_native`。
只要这些是 `False` 且 `fosun` 不进 `surveyed_specs()`，
**它的任何事件在结构上都不可能改变交易状态**。

测试证实：`test_fosun_is_not_in_authority_registry`、
`test_capabilities_block_authoritative_use`（对 halt/luld/mwcb 三者逐一断言）。

---

## 4. 时间语义：本实现最核心的设计约束

官方六个接口里，只有两个带事件时间：

| 接口 | 时间字段 | 单位 | 本实现 |
|---|---|---|---|
| `tick` | `time` | 毫秒 | ✅ 转 `TradeEvent.event_time` |
| `kline` | 数组首元素 | 毫秒 | ✅ 解析为 `FosunKlineBar.event_time`（不产出事件） |
| `quote` | **无** | — | ⛔ 快照，`time_semantics="unknown"`，`event_time=None` |
| `orderbook` | **无** | — | ⛔ 快照，`time_semantics="missing"` |
| `min` | 仅"第几分钟" | — | ⛔ 无日期，不可还原，不解析 |

**没有任何一处用 `receive_time` 冒充 `event_time`。** 这是刻意的：
一旦伪造，15 分钟前的报价会被当成"刚刚"，新鲜度检查全部失效。

时间戳防护（`ms_to_utc`）：

- 非数字 / 空 / `NaN` / `Inf` → `bad_time`
- `<= 0` → `bad_time`
- **秒级时间戳**（如 `1794839400`）→ 年份检查拦下（否则会变成 1970 年）
- **纳秒级**（`1794839400000000`）→ 同样被年份检查拦下
- 使用**整数运算** `divmod(raw, 1000)`，不经过浮点（避免微秒级误差）

测试：`test_invalid_timestamp_rejected`（9 组参数）、
`test_seconds_timestamp_is_caught_not_shifted`、
`test_tick_without_time_is_rejected_not_faked`。

---

## 5. 符号与交易所

- 入参 `usAAPL` / `AAPL` 都能处理：`normalize_symbol` 去掉小写 `us` 前缀，
  出站用 `wire_code()` 补回。
- `exchange` **恒为 `"fosun"`**。原因见能力矩阵报告：券商聚合行情无法拆回
  单一交易所，映射到 `nasdaq` 会让 `MarketStatusService` 的
  `(symbol, exchange)` 分桶产生"Nasdaq 独立行情"的错觉。
- `side` 映射：官方 `B/S` → 我们的 `B/A`（A = 卖方主动）。
  `test_side_mapping` 覆盖 8 种输入，未知值映射为 `""` 而不是猜。

---

## 6. 可靠性的六件事

| 能力 | 实现 | 测试 |
|---|---|---|
| 超时 | 默认 3 秒，`requests` 显式传 `timeout` | `test_transport_signs_and_unwraps_data` 断言 `kwargs["timeout"]` |
| 重试 | 指数退避 0.5/1/2s，最多 3 次，**只重试可恢复错误** | `test_transport_retries_upstream_5xx_then_succeeds` |
| 不重试 | 401/403/70001/解密失败 一律不重试 | `test_transport_does_not_retry_permission_error`、`test_permission_and_auth_are_not_retryable` |
| 限流 | 令牌桶，默认 **2 req/s、突发 4**（官方未公布配额，取保守值） | `test_token_bucket_*` |
| WS 重连 | 退避 1/2/4/8…上限 30s，**最多 10 次后放弃并抛错** | `test_reconnect_policy_is_bounded`、`test_stream_loop_gives_up_after_max_attempts` |
| 订阅恢复 | 自维护 `SubscriptionRegistry`，每次重连重放全部订阅 | `test_stream_loop_reconnects_and_resubscribes` |
| 缺口 | `GapDetector` 按事件时间轴判断，不看消息条数 | `test_gap_detector_flags_large_gap_only` |

**一个请求挂死不会拖住 PaperRuntime**：`_poll_raw` 把任何非协议异常都转成
`ProviderUnavailable`，feed 会记 `provider_error` 并继续。

**不允许无限重试掩盖权限错误**：`_receive_loop` 耗尽后 `raise`，
不是静默返回（这一点在实现时被自己的测试抓出来过，见 §8）。

---

## 7. 去重与乱序：为什么不用 tick id 当序号

`tick` 的 `id` 看起来是递增游标，但**协议没有承诺单调**。因此：

- `sequence` 字段恒为 `0`（不冒充协议序号），`source_event_id` 记录 id 供审计
- 本地维护有界 id 窗口（默认 5000）做**去重 / 乱序 / 缺口**判定
- 重叠窗口（轮询必然出现）识别为 `DUPLICATE` 而不是错误
- 缺口只在 `strict_gap_check=True` 时才打 `GAP` 标记——因为断线重连后
  首包必然"跳号"，那不代表数据缺失

测试：`test_overlapping_window_is_treated_as_duplicate_not_error` 专门钉住
"重叠不是故障"这一点。

---

## 8. 实现过程中被自己的测试抓出的两个缺陷

| 缺陷 | 后果 | 修法 |
|---|---|---|
| `run_stream_loop` 在重连耗尽后**静默返回 stats** | 上层以为"跑完了"，实际一条数据没收到 | 改为 `raise FosunUpstreamError` |
| 重复/乱序事件被 `return None` 静默丢弃 | 只涨计数器，上层看不到"为什么被拦" | 改为带 `DUPLICATE`/`OUT_OF_ORDER` 标记返回，由 `BLOCKING_FLAGS` 拦截 |

第二个尤其重要：**能被观测到的失败才是失败**，静默丢弃会让排障变成猜谜。

---

## 9. 未完成事项

| # | 事项 | 原因 | 解除条件 |
|---|---|---|---|
| 1 | 真实 `usAAPL` 报价 | 本机四个环境变量均未设置 | 提供凭据（见验证报告 §2） |
| 2 | 实时性判定 | 依赖 #1 | 需实测延迟并收集证据 |
| 3 | WebSocket 实测 | 依赖 #1 | 需真实会话与行情权限 |
| 4 | 四段时段覆盖 | 依赖 #1 | 需在盘前/常规/盘后/夜盘各采样 |
| 5 | 限流真实值 | 官方未公布 | 需厂商答复或谨慎探测 |
| 6 | LULD / MWCB | 协议中不存在 | 需另一数据源（交易所直连） |

---

## 10. 一个必须写明的边界

**FosunProvider 只能提供行情，不能获得停牌恢复授权。**

这不是"暂时没做"，而是设计上不可能：
`halt_events=False` + 不在 `surveyed_specs()` + `AuthorityRouter` 的 13 步校验。
即便将来有人给它加一个"status"接口，也必须先改 `capabilities`，
而那会立刻让 `test_capabilities_block_authoritative_use` 变红。

**改代码才能放宽，改配置不能。** 这是有意的。
