# Provider Adapter 实现报告（US Provider Adapter Implementation）

**项目**：`C:\FosunAI-Trading`
**范围**：真实美股行情接入层（离线可验证）
**日期**：2026-11-16

---

## 1. 设计目标与本轮边界

| 目标 | 本轮状态 |
|---|---|
| 在既有架构内做统一接入层（不另起平行系统） | ✅ `QuoteGateway` 仍是唯一行情入口 |
| 明确的 Provider 接口与数据模型 | ✅ 8 类事件 + 能力声明 |
| 覆盖 Quote/Trade/OrderBook/Session/Halt/LULD/MWCB/Health | ✅ |
| 完整来源字段（provider/symbol/exchange/event_time/receive_time/sequence/event_id/quality） | ✅ 强制字段 |
| 内部 UTC + 保留原始时间 | ✅ `event_time` UTC，`raw_time` 原样 |
| 重复/乱序/延迟/断线/缺口/时间戳异常 | ✅ 序号守卫 + 质量标记 + fail-closed |
| 旧数据不得覆盖新数据 | ✅ `_ingest` 拒绝迟到事件 |
| 无权威来源时不得生成恢复事件 | ✅ 权威路由默认拒绝 |
| 可独立测试，不需要真实账户 | ✅ 126 项测试全部离线 |

**边界**：本轮**不连接任何真实端点**（无 Key、未采购、无交易所授权）。
适配器是"可运行的解析与门禁"，传输层留了一个明确的扩展点。

---

## 2. 文件清单

| 文件 | 行数 | 职责 |
|---|---|---|
| `src/ai_trader/market/providers/models.py` | ~330 | 8 类事件、质量枚举、ProviderHealth |
| `src/ai_trader/market/providers/base.py` | ~290 | Provider 基类、能力声明、序号守卫 |
| `src/ai_trader/market/providers/normalize.py` | ~150 | 代码/交易所归一、时间解析、时段判断 |
| `src/ai_trader/market/providers/authority.py` | ~250 | A/B/C/D 分类、签名策略、权威路由 |
| `src/ai_trader/market/providers/feed.py` | ~230 | 多 provider 汇聚、按交易所隔离的状态 |
| `src/ai_trader/market/providers/adapters.py` | ~400 | Massive / Alpaca / Databento / Nasdaq ITCH / Recorded |
| `src/ai_trader/market/providers/__init__.py` | ~40 | 对外导出 |
| `src/ai_trader/market/gateway.py`（改） | +30 | 新增可选 `provider` 参数 |
| `config/config.yaml` + `config.template.yaml`（改） | +60 | `market_data_providers` 段（只写环境变量名） |
| `tests/test_provider_*.py` | 5 文件 | 126 项测试 |
| `tests/fixtures/marketdata/*.jsonl` | 4 + 3 | 录制样本与坏样本 |

---

## 3. 类与职责

### 3.1 `MarketDataProvider`（base.py）

```python
class MarketDataProvider:
    name: str
    def connect() / close() / reconnect()
    def subscribe(symbols) / unsubscribe(symbols) / subscriptions()
    def poll() -> List[MarketEvent]        # 已归一化 + 已打质量标记
    def _connect / _close / _poll_raw / _subscribe_raw   # 子类实现
```

* `poll()` 在未连接时**直接抛错**（不允许返回最后一笔价格）；
* 健康统计：`messages / dropped / gaps / duplicate / out_of_order / reconnects`；
* 质量判定使用 `_quality_now()`：实时用墙钟，录制回放用事件时间线。

### 3.2 `ProviderCapabilities`

声明式能力（未声明 = 不支持）：

```
us_full_market / sip_consolidated / exchanges / realtime / delay_seconds /
historical / history_years / backfill / premarket / regular / afterhours / overnight /
trades / quotes / order_book / order_book_depth /
halt_events / halt_events_native / halt_events_nasdaq_listed_only /
luld_events / luld_events_native / mwcb_events / mwcb_events_native /
exchange_field / sequence_field / source_event_id /
requires_exchange_license
```

`supports_authoritative(kind)` 只有 `native=True` 才返回 True。

### 3.3 `ProviderFeed`

* 多 provider 汇聚；
* 状态键 = `(symbol, exchange)` → **多交易所天然隔离**；
* `quote(symbol)` 返回与 `Broker.get_quote` 同形的 `Quote`；
* 以下情况**拒绝出价**：无数据 / 过期 / 停牌 / 无有效价格；
* 迟到事件被丢弃并计入 `rejected["late_event"]`。

### 3.4 `AuthorityRouter`

见安全报告。核心：`can_apply(event, symbol, exchange, now, version)` 返回
`AuthorityVerdict(accepted, reason, authority, signature)`。

### 3.5 适配器

| 类 | 数据源 | 解析依据 | 关键限制 |
|---|---|---|---|
| `MassiveProvider` | Massive（原 Polygon） | `/docs/websocket/stocks/luld`、trades/quotes | 17/18 仅 Nasdaq 上市；时间单位需显式配置 |
| `AlpacaProvider` | Alpaca | `docs/market-data` 实时流 | 无停复牌/LULD 事件；单字母交易所码需专用映射 |
| `DatabentoProvider` | Databento | `status` / `trades` / `mbp-1` schema | 价格字段为 1e-9 定点；无签名 |
| `NasdaqItchProvider` | Nasdaq TotalView | Trading Action（H/Q/T） | 需交易所授权 |
| `RecordedProvider` | 本地 JSONL | 通用基类 | 支持注入重复/乱序/丢包/断线 |

**每种适配器都声明了自己"没有什么"** —— 这是本实现最重要的部分：
Alpaca 明确 `halt_events=False`，Massive 明确 `luld_events_native=False`，
Databento 明确 `luld_events=False`（status ≠ LULD 价格带）。

---

## 4. 与既有系统的接入点

### 4.1 `QuoteGateway`（唯一入口）

```python
QuoteGateway(broker, clocks, enabled=True, provider=feed)
```

* `provider is None` → 走 `broker.get_quote()`，**与之前完全一致**；
* `provider is not None` → 走 `feed.quote()`，但时段校验、新鲜度、跨时段冒用、
  跳变限制**全部保留**；
* `snapshot()["source"]` 明确标注 `provider` / `broker`。

### 4.2 为什么不让 Engine 直接调 provider

Engine 只认 `QuoteGateway`，这是上一阶段建立的"结构上消除旁路"的收口。
本轮没有破坏它：provider 只是网关的一个数据源。

### 4.3 交易状态机不变

`MarketStatusService`、`LuldLifecycle`、`ResumeAuthority`、`PaperBroker` 的三个阶段门禁
**一行都没改**。provider 层只负责"给数据"和"给权威判定"，不碰交易决策。

---

## 5. 配置模板（只写变量名，不含密钥）

```yaml
market_data_providers:
  enabled: false
  active: []                    # massive / alpaca / databento / nasdaq_itch / recorded
  max_age_seconds: 60
  max_future_seconds: 5
  require_signature_for_resume: true    # 不要改成 false
  providers:
    massive:
      api_key_env: MASSIVE_API_KEY
      time_unit: ns
      plan: stocks_advanced
    alpaca:
      key_id_env: ALPACA_KEY_ID
      secret_env: ALPACA_SECRET_KEY
      feed: sip
    databento:
      api_key_env: DATABENTO_API_KEY
      dataset: XNAS.ITCH
      schemas: [status, trades, mbp-1]
    recorded:
      fixture_dir: tests/fixtures/marketdata
      replay_clock: true
```

**整个仓库不含任何密钥**；代码只读 `os.environ[<env_name>]`。

---

## 6. 如何接真实传输（下一步的最小改动）

1. 在对应适配器里实现 `_connect()`：建立 WebSocket/HTTP，鉴权用
   `os.environ[self.config["api_key_env"]]`；
2. `_poll_raw()` 返回原始 JSON 数组（解析逻辑已就绪）；
3. `_subscribe_raw()` 发送订阅帧；
4. 断线时调用基类 `reconnect()`（会计数），并触发一次历史补齐（`backfill`）；
5. **不要**在适配器里判断"是否可以恢复交易"——那是 `AuthorityRouter` 的职责。

---

## 7. 已知限制

| 限制 | 影响 | 处理 |
|---|---|---|
| 未连真实端点 | 字段语义只经文档与样本验证 | BLOCKED，需 Key/授权 |
| 时间单位文档歧义（Massive） | 单位错会把年份算错 | 强制显式配置 + 单测覆盖"单位错了必须暴露" |
| 交易所单字母码（Alpaca） | 通用别名表会误判（`N`=NYSE 而非 nasdaq） | 适配器内专用映射表 |
| `status` ≠ LULD 价格带 | 不能把 Databento status 当 LULD | 能力声明里明确 |
| 无签名 | 不能作为权威恢复 | 默认拒绝，见安全报告 |

---

## 8. 结论

**C. 真实行情接入层：PARTIAL。**
接入层的**代码与门禁**已完成并通过 126 项测试；缺的只有"真实传输 + 授权"，
属于外部依赖，不是实现缺口。在拿到 Key 之前，任何"已接入真实行情"的说法都是不成立的。
