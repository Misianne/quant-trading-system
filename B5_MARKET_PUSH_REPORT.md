# B5_MARKET_PUSH_REPORT.md —— 实时行情推送

**项目**：`C:\FosunAI-Trading`
**阶段**：B5（交易基础能力建设第二阶段）
**时间**：2026-11-08 14:05 – 14:35 (+08:00)
**约束**：仅本机 WebSocket 测试服务，未连接真实推送网关，未修改 SDK

---

## 0. 一页结论

| 项 | 状态 |
|---|---|
| 官方 SDK 推送实现审计 | ✅ 完成（**无重连 / 无心跳超时 / 无去重 / 无时间戳校验**） |
| 订阅 / 取消订阅 | ✅ 实现（协议对齐 SDK JSON 模式） |
| 心跳检测 | ✅ 双向：回应服务端 `hb` + 自己设超时看门狗 |
| 断线重连 | ✅ 指数退避，可配上限 |
| 重连后恢复订阅 | ✅ 自动重发 `sub` |
| 消息解析 / 异常处理 | ✅ 容错解析，坏包计数不崩 |
| 时间戳检查 | ✅ 缺失即丢弃；过期即丢弃 |
| 重复 / 乱序 | ✅ 按标的时间戳单调递增，重复与更旧都丢弃 |
| 连接状态监控 | ✅ 6 状态机 + `snapshot()` 全量指标 |
| 行情中断暂停开仓 | ✅ 引擎闸门 `FEED_NOT_FRESH` |
| 恢复后重新验证 | ✅ 重连清空 `settled`，收到新行情才算恢复 |
| 不冒充真实行情 | ✅ `source` 标注，本地测试源为 `local-ws-test`，协议标 `unverified` |
| 测试 | ✅ **20 个用例全部通过**（含真实本机 WS 服务） |

---

## 1. 审计：官方 SDK 的推送实现

### 1.1 结构（`sdk_src/fsopenapi/api/market_push.py`，326 行）

```python
class MarketPushAPI:
    def __init__(self, client):
        self._url = "wss://openapi-push.fosunxcz.com/msg/v1/push"
        self._ws = None
        self._subscriptions = {}   # 注释：本地记录已订阅的合约，断线后用于恢复
        self._refresh_task = None

    async def connect(self)          # 带 X-Api-Key / X-Session 头
    async def _receive_loop(self)    # 只处理 hb 回应 + 打日志
    async def send_heartbeat(self, hbValue)
    async def subscribe(self, topic, codes)
    async def unsubscribe(self, topic, codes)
    async def close(self)
```

协议（JSON 模式，`Msg_Use_Proto = False`）：

```
订阅  → {"act":"sub","items":[{"topic":"qt","code":["cn600519"]}],"requestid":"..."}
心跳  ← {"topic":"hb","hbValue":X}      客户端回 {"act":"hb","hbValue":X}
推送  ← {"topic":"qt","items":[{"code":"...","msg":"{...}"}],"t":...}
```

### 1.2 审计结论：缺什么

| 能力 | SDK 现状 | 影响 |
|---|---|---|
| 断线重连 | ❌ 完全没有 | 网络抖动后行情永久中断，直到进程重启 |
| 心跳超时检测 | ❌ 只被动回应 | 服务端假死（TCP 未断但不推送）时永久卡住 |
| 重连后恢复订阅 | ❌ `_subscriptions` 是**死字段**（从未写入也未读取） | 即使自己重连，也不会重新订阅 |
| 消息去重 | ❌ 没有 | 重复推送会重复触发决策 |
| 乱序处理 | ❌ 没有 | 旧价可能覆盖新价 |
| 时间戳校验 | ❌ 没有 | 过期行情被当作实时行情使用 |
| 连接状态机 | ❌ 只有 `self._ws` 是否为 None | 无法区分"连接中/已订阅/假死" |
| 异常处理 | ⚠️ 只 `except Exception` 打日志 | 连接关闭后不会通知上层 |

**结论**：SDK 的推送层只能"演示能收到消息"，不能用于自动交易。
本工程的 `market/feed.py` 在其协议之上补齐可靠性，**不修改 SDK**。

---

## 2. 实现结构

```
src/ai_trader/market/feed.py          可靠推送层（约 560 行）
  ├─ FeedState        DISCONNECTED / CONNECTING / CONNECTED / SUBSCRIBED
  │                   / RECONNECTING / STOPPED / FAILED
  ├─ QuoteTick        symbol / last / prev_close / ts / received_at / source / seq
  ├─ FeedConfig       url / topics / codes / 心跳超时 / 过期阈值 / 退避 / settle
  └─ MarketFeed
       ├─ start()/stop()        后台线程 + asyncio 事件循环，对外同步 API
       ├─ ingest(payload)       消息解析、去重、乱序、过期（纯逻辑，可单测）
       ├─ is_fresh(symbol)      开仓闸门判据
       └─ snapshot()            状态与全量指标
```

### 2.1 线程模型

引擎与风控都是同步的，而 WebSocket 是异步的。取舍：

* `MarketFeed` 在**独立后台线程**跑自己的事件循环；
* 对外暴露 `is_fresh()` / `snapshot()` 等**加锁的同步方法**；
* 不引入 `asyncio.run` 嵌套，不污染主线程。

### 2.2 重连与恢复

```
connect → CONNECTED → 发 sub → SUBSCRIBED → 收消息
   ↑                                    │
   │                      心跳超时/连接关闭/异常
   │                                    ▼
   └── 退避(0.2→0.4→…→5s) ── RECONNECTING ── 清空 settled
```

关键点：**重连会 `settled.clear()`**。这样即使本地还留着断线前的缓存，
也不会被当作"行情已恢复"直接用来开仓 —— 必须等到**重连后收到的第一条新行情**。

### 2.3 去重与排序

对每个标的维护 `last_ts`：

| 情况 | 计数 | 处理 |
|---|---|---|
| `ts == last_ts` | `duplicates` | 丢弃 |
| `ts < last_ts` | `out_of_order` | 丢弃（**不得覆盖最新价**） |
| `received_at - ts > stale_after` | `stale_dropped` | 丢弃 |
| `ts` 缺失 | `ts_missing` | 丢弃（无法判断新鲜度） |
| `msg` 非 JSON / 无价格字段 | `bad_payload` | 丢弃并计数 |

### 2.4 心跳超时

```python
msg = await asyncio.wait_for(ws.recv(), timeout=self.cfg.heartbeat_timeout)
# 超时 → 计 heartbeat_timeouts + 抛 ConnectionError → 触发重连
```

注意：超时的判据是"**任何消息**都没收到"，而不只是 `hb` —— 因为高频行情本身
就证明连接活着。这样既不会误杀正常连接，也能抓住"服务端假死"。

### 2.5 与引擎的联动

```
行情新鲜度闸门（B5）  →  AI 决策权威（P1-9）  →  AI 分析  →  风控  →  下单
        │                                            │
   只挡"新开仓"                                已有持仓的止损走 monitor()，
   不挡止损                                     完全不经过这两道闸门
```

* `market_feed.required_for_entry: true` + `enforce_markets: ["cn"]`
* 不新鲜时返回 `blocked` + `FEED_NOT_FRESH`，并写审计 `entry_blocked`
* 未启用推送时（默认）闸门直接放行，不影响模拟盘

---

## 3. 测试（20 例，全部通过）

```
$ pytest tests/test_market_feed_b5.py -q
....................                                                     [100%]
20 passed
```

### 3.1 A 组：消息处理纯逻辑（10 例）

| 用例 | 断言 |
|---|---|
| `test_ingest_accepts_valid_quote` | 正常 tick 落入 `_ticks`，`quotes` 计数 +1 |
| `test_duplicate_message_dropped` | 同时间戳第二次 → 丢弃，`duplicates=1` |
| `test_out_of_order_message_dropped` | 更旧时间戳 → 丢弃，**最新价仍是 300.0**（未被 999 覆盖） |
| `test_stale_message_dropped` | 10s 前的时间戳 → `stale_dropped=1` |
| `test_missing_timestamp_rejected` | 无 `time` → `ts_missing=1`，不进入 ticks |
| `test_bad_payload_counted` | 非 JSON / 价格非数字 → `bad_payload=2` |
| `test_heartbeat_topic_not_treated_as_quote` | `topic=hb` 不计入 quotes |
| `test_is_fresh_requires_subscribed_state` | 未连接 → "行情连接状态 DISCONNECTED" |
| `test_is_fresh_after_manual_state` | 订阅 + 有 tick → 新鲜；未订阅标的 → "未收到任何行情" |
| `test_parse_ts_variants` | ISO / Z / 毫秒 / 秒 都能解析，乱码返回 None |

### 3.2 B 组：本机 WebSocket 服务（6 例）

测试里起了一个真实 `websockets` 服务（`127.0.0.1:随机端口`），
支持"正常推送 / 静默 / 主动断开"三种模式。

| 用例 | 验证 |
|---|---|
| `test_ws_connect_and_subscribe` | 真的收到 `sub` 报文（含 `hk00700`），状态变 `SUBSCRIBED`，`is_fresh` 通过；`source=local-ws-test`、`protocol=unverified` |
| `test_ws_heartbeat_roundtrip` | 服务端发 `hb` → 客户端回 `act=hb`（服务端计数 `hb_acks>=1`） |
| `test_ws_heartbeat_timeout_triggers_reconnect` | 服务端静默 + 0.4s 超时 → `heartbeat_timeouts>=1`，**重连发生**（`conns` 增加） |
| `test_ws_reconnect_resubscribes` | 服务端主动断开 → 重连后 `subs` 数量增加（**证明重订阅**） |
| `test_ws_settle_required_after_reconnect` | 清空 settled 后 `is_fresh` 为 False；收到新行情后恢复 |
| `test_feed_snapshot_marks_source_and_protocol` | 明确标注 `source` / `protocol=unverified` |

### 3.3 C 组：引擎联动（4 例）

| 用例 | 验证 |
|---|---|
| `test_engine_blocks_entry_when_feed_stale` | 已连接但无行情 → `blocked` + `FEED_NOT_FRESH` + 0 委托 + `entry_blocked` 审计 |
| `test_engine_allows_entry_when_feed_fresh` | 有新鲜行情 → `ordered` |
| `test_engine_exit_not_blocked_by_feed` | **行情断开后止损仍然执行**（`exit`），持仓清零 |
| （合并在上） | — |

---

## 4. 诚实标注

| 项 | 标注 | 原因 |
|---|---|---|
| 真实推送的字段名 | `protocol: "unverified"` | 无凭证，未抓过真实报文；解析器用候选字段名 |
| 真实推送的鉴权头 | 未验证 | SDK 用 `X-Api-Key` + `X-Session`，本层未实现握手（依赖 SDK 建会话） |
| 本地测试行情 | `source: "local-ws-test"` | **绝不冒充真实行情** |
| `Msg_Use_Proto` | 只实现 JSON 模式 | proto 模式需要 `push_pb2` 结构，未验证 |
| 订阅主题 | 只实现 `qt`（报价） | `min/tk/ob/bq` 未接入 |

### 4.1 为什么不直接复用 SDK 的 `MarketPushAPI`

1. 它没有重连 —— 自动交易不能依赖"进程重启"来恢复行情
2. 它把"收到消息"直接写进 logger，没有回调/缓存，无法给风控用
3. 它的 `_subscriptions` 是死字段，恢复订阅必须自己实现
4. 修改 SDK 会破坏审计基线（`audit_evidence/sdk_baseline.json` 会漂移）

因此选择"在协议层复刻 + 自己实现可靠性"，SDK 源码零改动（有测试守护）。

---

## 5. 剩余阻断项

| # | 阻断项 | 影响 |
|---|---|---|
| **F1** | 没有推送网关地址与行情权限 | `market_feed.enabled=false`，系统退化为"轮询 REST 行情"，止损延迟取决于扫描间隔 |
| **F2** | 真实推送报文结构未核对 | `protocol=unverified`；字段名可能全不匹配，上线前需抓一次真实报文 |
| F3 | `min/tk/ob/bq` 主题未接入 | 无法做盘口/逐笔级别的信号 |
| F4 | 港股/美股交易时段未处理 DST | 行情订阅时段可能偏差 1 小时 |
| F5 | 推送不含停牌/复牌事件 | 停牌仍靠人工 `suspended_symbols` 登记 |
| F6 | 断线期间的价格缺口无法回补 | 重连后不会回补历史 tick，只用最新价 |

### 5.1 启用推送的步骤

```yaml
market_feed:
  enabled: true
  url: "wss://<gateway>/msg/v1/push"
  codes: ["cn600519", "cn000001"]
  required_for_entry: true
  enforce_markets: ["cn"]
```

```bash
ai-trader check-env     # 看 market_feed.url_configured / protocol
ai-trader status        # 看 feed.state / feed.stats
```

上线前必须做一次**真实报文抓取**，核对 `items[].msg` 的字段名，
否则解析器可能把所有推送都记为 `bad_payload`。

---

## 6. 已知副作用

1. `market_feed.enabled` 默认 `false`，所以默认行为与之前完全一致（全量测试无回归）。
2. 启用后若行情长时间不来，**新开仓会被阻断**，但止损不受影响 —— 这是刻意的不对称。
3. 每次重连会清空 `settled`，意味着"刚恢复的那一小段时间不能开新仓"，
   这是为了避免用断线前的旧价做决策。
