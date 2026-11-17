# 风控门禁回归报告（复星行情接入）

- 项目：`C:\FosunAI-Trading`
- 代码提交：`a6cda18`
- 全量测试：**1409 passed / 0 failed / 0 errors / 1 skipped**（`r14.xml`）
- 本报告的证据全部来自可重跑的测试用例，不是人工判断

---

## 0. 结论

**新增一个行情源，没有削弱任何既有安全门禁。**

八项保护逐一验证通过，其中四项是"一票否决"性质（一旦被绕过就是不可接受）。
关键结构性保证：**复星在 `AuthorityRouter` 眼里根本不存在**，
所以它的任何事件在代码路径上都无法驱动停牌/LULD/MWCB。

| # | 保护项 | 判定 | 测试 |
|---|---|---|---|
| 1 | QuoteGateway 仍是唯一行情入口 | **PASS** | `test_quote_flows_through_gateway` |
| 2 | PaperBroker 无法绕过行情质量门禁 | **PASS** | `test_paper_broker_blocks_fill_when_market_halted`、`test_provider_does_not_touch_broker_directly` |
| 3 | LULD 不允许普通报价自动恢复 | **PASS** | `test_fosun_luld_state_is_denied` |
| 4 | 普通停牌不允许定时器自动解除 | **PASS（未改动）** | 既有 `test_us_luld_authority_attacks.py` 全部通过 |
| 5 | MWCB 不允许根据普通报价自行解除 | **PASS** | `test_fosun_mwcb_state_is_denied` |
| 6 | 行情过期时禁止新风险暴露 | **PASS** | `test_stale_event_is_flagged`、`test_gateway_refuses_when_data_stale` |
| 7 | 风险退出保护不因行情组件缺失被关闭 | **PASS（未改动）** | 既有 `test_stale_quote_still_allows_stop_loss_via_position_mark` |
| 8 | 恢复授权体系保持不变 | **PASS** | `test_fosun_is_not_in_authority_registry`、`test_capabilities_block_authoritative_use` |

---

## 1. 为什么"没削弱"是可证的

本轮**只新增了 3 个文件 + 1 处导出**：

```
src/ai_trader/market/providers/fosun.py
src/ai_trader/market/providers/fosun_protocol.py
src/ai_trader/market/providers/fosun_transport.py
src/ai_trader/market/providers/__init__.py   （仅追加导出）
```

以下文件**零改动**（`git diff` 可验证）：

- `market/gateway.py`（QuoteGateway）
- `market/providers/feed.py`（汇聚/分桶/权威判定）
- `market/providers/authority.py`（13 步校验）
- `market/providers/models.py`（事件模型）
- `market/status.py`、`market/luld.py`、`market/luld_authority.py`
- `broker/paper.py`（成交门禁）
- `paper_runtime/*`（Replay / 账本 / 不变量 / Supervisor）

新增适配器只能通过 `MarketDataProvider` 的 5 个钩子接入，
这是既有抽象，不是为本次改动新开的通道。

---

## 2. 逐项证据

### 2.1 QuoteGateway 是唯一入口

```
FosunProvider.poll() → ProviderFeed._ingest → SymbolState
                                   ↓
                        QuoteGateway.quote() → 风控 → PaperBroker
```

- 测试 `test_quote_flows_through_gateway`：feed 只有一条 tick（201.20），
  `gw.price("AAPL","regular_only")` 返回 201.20，`snapshot()["source"] == "provider"`。
- 测试 `test_provider_does_not_touch_broker_directly`：断言 provider **没有**
  `broker` 属性、没有 `place_order` 方法。它拿不到下单能力。

### 2.2 PaperBroker 的成交门禁没被绕过

`test_paper_broker_blocks_fill_when_market_halted`：`MarketStatusService`
标记 `usAAPL` 停牌后，即使直接 `place_order`，返回状态也不是 `filled`。

这条与 provider 无关，是**独立第二道防线**——即使行情层被绕过，
账本层仍然拒绝成交。

### 2.3 LULD：复星的事件进不了权威层

`test_fosun_luld_state_is_denied`：

```python
ev = LULDState(provider="fosun", ..., resumed=True)
v = router.can_apply(ev, symbol="AAPL", exchange="fosun", now=UTC)
assert not v.accepted and "PROVIDER_NOT_REGISTERED" in v.reason
```

注意这个测试是**故意构造**一条"已恢复"的 LULD 事件送进去的——
结果是拒绝。说明即使有人试图伪造，也过不了注册表这一关。

### 2.4 普通停牌不会被定时器自动解除

这一条本轮**没有改动任何相关代码**，因此既有保护原样保留。
回归证据是既有测试集全部通过（`test_us_luld_authority_attacks.py` 36 项、
`test_us_halt_lifecycle.py` 等）。

补一句设计说明：`MarketStatusService` 的暂停到期只把状态推进到
`RESUME_PENDING` / 进入竞价，**不会**直接回到可交易；恢复必须经过
`ResumeAuthority.verify()`。这条在复星接入前后完全一致。

### 2.5 MWCB：同样进不了

`test_fosun_mwcb_state_is_denied` 用 `MWCBState(provider="fosun", level=1, triggered=True)`
送进路由 → `PROVIDER_NOT_REGISTERED`。

### 2.6 过期行情禁止新风险暴露

- `test_stale_event_is_flagged`：`max_age_seconds=1` 时事件带 `STALE`，
  `usable=False`（`STALE ∈ BLOCKING_FLAGS`），feed 会丢弃。
- `test_gateway_refuses_when_data_stale`：网关层再次拒绝，
  抛 `QuoteUnavailable` / `FeedUnavailable`。

两层都拦，不是只靠一层。

### 2.7 风险退出不因行情缺失被关闭

这是前几轮踩过的坑：曾有一次"行情组件缺失 → 风险退出也被 fail-closed 挡住"，
后来改为**退出意图永远放行**。本轮没有触碰 `risk/controller.py`，
既有测试 `test_stale_quote_still_allows_stop_loss_via_position_mark` 仍然通过。

### 2.8 恢复授权体系不变

`test_fosun_is_not_in_authority_registry` 断言
`"fosun" not in {s.provider for s in surveyed_specs()}`。

`test_capabilities_block_authoritative_use` 对 `halt` / `luld` / `mwcb`
三类逐一断言 `supports_authoritative(kind) is False`。

**这两条是"结构上的不可能"，不是"操作上的小心"。**

---

## 3. 权限失败时的行为（fail-closed）

| 场景 | 行为 | 测试 |
|---|---|---|
| 403 / 70003 | 立即停止轮询，`permission_denied=True`，后续 poll 不再发请求 | `test_permission_denied_stops_polling` |
| 401 | 握手/会话失败 → `FosunAuthError`，**不重试** | `test_permission_and_auth_are_not_retryable` |
| 解密失败 | `FosunProtocolError` → `ProviderUnavailable` | `test_protocol_error_becomes_unavailable` |
| 连接断开 | `ProviderUnavailable`，恢复后可继续 | `test_disconnect_raises_and_recovers` |
| 响应缺 `ticks` | 视为不可用，不产出"空数据" | `test_malformed_ticks_payload` |
| WS 重连耗尽 | **抛错**，不静默返回 | `test_stream_loop_gives_up_after_max_attempts` |

**关键设计**：权限错误**不重试**。重试只会把"你缺权限"拖成"看起来在跑但没数据"。

---

## 4. 一个必须记录的边界

复星行情**永远不能**成为恢复交易的依据。原因不是"我们还没实现"，
而是三层同时成立：

1. `capabilities.halt_events = False`（协议里就没有停牌接口）
2. `fosun` 不在 `surveyed_specs()`（路由第一层就拒）
3. `require_signature_for_resume = True`（即使登记了，没有签名也过不了）

要放宽，必须**同时**改这三处代码——而只要改第 1 条，
`test_capabilities_block_authoritative_use` 会立刻变红。

**改代码才可能放宽，改配置不可能。** 这是本轮刻意保留的设计。

---

## 5. 回归范围

| 测试集 | 用例数 | 结果 |
|---|---|---|
| 本轮新增（fosun） | 89 | 全通过 |
| 既有全量 | 1320 | 全通过 |
| **合计** | **1409** | **0 failed / 0 errors / 1 skipped** |

`r14.xml` 为 JUnit 证据。

唯一跳过项为既有跳过（非本轮引入）。
