# US_P0_IMPLEMENTATION_REPORT.md —— 美股 P0 修复实施报告

**项目**：`C:\FosunAI-Trading`（星财富单 AI 自动交易系统）
**阶段**：US-P0-1 ~ US-P0-4
**时间**：2026-11-08 18:38 – 19:10 (+08:00)
**性质**：**代码修复阶段**。未连接真实账户、未发出任何真实委托、未修改第三方 SDK。
**安全状态（运行 `check-env` 实测）**：

```
live_trading            = false
order_execution_enabled = false
paper_trading           = true
kill_switch             = false
can_send_real_order     = (False, 'PAPER_TRADING=true，真实券商连接被禁用')
```

---

## 0. 一句话结论

US-P0-1 ~ US-P0-4 四项全部完成，**全部测试通过（346 passed / 1 skipped / 0 failed）**，
第三方 SDK 一字节未动，安全开关保持关闭。已在 4 个方向做变异检验，4/4 被测试抓住。

| 项 | 状态 | 关键证据 |
|---|---|---|
| US-P0-1 交易所时区 | ✅ 完成 | 北京 22:29/22:30 判定翻转；北京周六=纽约周五；4 个时点修正 |
| US-P0-2 美股规则生效 | ✅ 完成 | `enforce_markets: ["cn","us"]`；tick size 校验；`applied=False` 不再等价放行 |
| US-P0-3 全时段持仓 | ✅ 完成 | `useUsPre/Post/Night` 全部 true 并可在 health 审计；查询失败 fail-closed |
| US-P0-4 风险处置不阻断 | ✅ 完成 | 减仓豁免时段/日历/日亏；行情失败退化到券商持仓价；持仓失败用快照兜底 |

---

## 1. 修改前的准备（按要求逐条执行）

### 1.1 读取既有结论

| 文件 | 读取内容 |
|---|---|
| `US_MARKET_RULES_REPORT.md` | §3.1–§3.7 时段/节假日/早收市/tick/熔断/SSR/T+1；§4 官方来源登记表（15 条）；§5 开发任务 US-T1…T12 |
| `US_BROKER_CAPABILITY_REPORT.md` | 官方 SDK 的 `get_holdings(use_us_pre, use_us_post, use_us_night)`；订单类型枚举未验证 |
| `INDEPENDENT_AUDIT.md` §11 | 4 个时段缺陷、`enforce_markets` 只含 cn、缺 `use_us_*`、阻断项 US-B1~B9 |

### 1.2 Git 状态 / 未提交修改 / 文件哈希

```
$ git status
fatal: not a git repository (or any of the parent directories): .git
```

**结论：本项目没有 Git 仓库**（历次阶段均为目录级交付），因此无法使用 `git status` /
`git stash`。改用**哈希清单备份**替代，见 1.3。

### 1.3 可回滚备份

```
backups/us_p0_20261108-183854/
├── MANIFEST.sha256.json     # 70 个文件 → sha256
└── (对应源文件副本)
```

* 覆盖范围：`src/`、`config/`、`scripts/`、`tests/`、`docs` 类 Markdown
* **明确排除**：`data/`、`logs/`、`.env`、`.venv/`、`sdk_src/`
* 已核验备份目录内**不含** `.env` / `*.db` / `vault.json`（未复制任何密钥与用户数据）

### 1.4 未覆盖用户数据

本轮**没有**写入/覆盖 `data/`（数据库、`stops.json`、`HALT.json`）、`.env`、
`data/vault.json`、`config/credentials*`。所有新写入都发生在 `src/`、`config/config.yaml`、
`tests/`、`audit_evidence/`。

---

## 2. US-P0-1：改用 `zoneinfo` 的 `America/New_York`

### 2.1 原缺陷（实测复现）

原实现把美股写成**北京时间的硬编码窗口** `21:30–23:59` + `00:00–04:00`，
并且"星期几"用**北京日期**：

| 北京时间（2026-11-04 周三，EST 冬令时） | 对应纽约时间 | 旧判定 | 正确判定 | 问题 |
|---|---|---|---|---|
| 21:30 | 08:30 EST | 开市 | **休市** | 早开 1 小时（打在盘前） |
| 22:00 | 09:00 EST | 开市 | **休市** | 同上 |
| 22:29 | 09:29 EST | 开市 | **休市** | 同上 |
| 04:30（次日） | 15:30 EST | 休市 | **开市** | 漏掉尾盘 30 分钟 |
| 北京 11-07（周六）02:00 | 11-06（周五）13:00 | 休市 | **开市** | 周五夜盘被误杀 |
| 北京 11-09（周一）02:00 | 11-08（周日）13:00 | 开市 | **休市** | 周日误开 |

实测输出见 `audit_evidence/us_p0_probe_output.txt`（「旧实现 vs 新实现」段，
`不一致时点数：4`）。

### 2.2 实现

**新增 `src/ai_trader/market/clock.py`（369 行）**

| 组件 | 作用 |
|---|---|
| `TZ_BY_MARKET` | `us→America/New_York`、`hk→Asia/Hong_Kong`、`cn→Asia/Shanghai` |
| `load_tz()` | 取 `zoneinfo`；缺失抛 `ClockError`（含 Windows 需 `pip install tzdata` 的提示） |
| `SessionWindow` | 交易所当地时间的半开区间 `[open, close)` |
| `SessionInfo` | `local_now / local_date / weekday / phase / is_regular_open / is_extended_open / closes_at / reason` |
| `ExchangeClock` | `local()` 时区换算、`is_trading_day()`、`close_time()`（吃早收市）、`session()`、`is_open()` |
| `build_clock()` | 统一解析 `sessions.<mkt>`（新 dict 格式 / 旧 list 格式都支持） |
| `DEFAULT_SESSION_SPEC` | 内置默认值，**写的是交易所当地时间**（美东 09:30–16:00），不再是北京窗口 |

关键设计点：

1. **naive datetime 视为本机时区**（与 `datetime.now()` 语义一致），aware 输入按其时区换算。
2. `configured=False`（没配常规窗口）时 `session()` 直接返回 `closed`，**不用默认 09:30–16:00 顶替**（那是 fail-open）。
3. 阶段：`pre / regular / post / night / closed / weekend / holiday`；夜盘支持跨当地午夜（`20:00–04:00` 用"或"判断）。
4. 提前收市：`early_close` 覆盖当日收盘时间，且盘后从提前的收盘时刻起算。

**接入 `market/rules.py`**

* `MarketRuleSet._build_clocks()`：为每个市场建 `ExchangeClock`；`ClockError` 不中断程序，记 `clock_errors[mkt]` 并让该市场恒定拒绝。
* `session_status(market, now, extended=False)`：改为走时钟；`extended=True` 时盘前/盘后/夜盘也算可交易（供减仓判断）。
* `local_date(market, now)`：暴露交易所当地日期。
* `TradingCalendar` 支持**分市场**日历：`CalendarEntry(status/source/covered_from/covered_until/holidays/early_close)`；
  新增 `covered_until` —— **超出覆盖范围必须拒绝**（"没登记"≠"是交易日"）。

**接入 `risk/controller.py`**

* 删除 `SESSIONS` 里的北京硬编码窗口，`is_market_open()` 改为 `build_clock(...).is_open()`；
  时区缺失/未配置 → `False`（fail-closed）。
* `RiskController.evaluate` 第 5 项 `market_hours` 改调 `self.rules.session_status(...)`，
  让**配置文件**成为唯一真相（不再有第二份时段表）。

**配置 `config/config.yaml`**

```yaml
sessions:
  us: { tz: America/New_York, regular: [["09:30","16:00"]],
        pre: ["04:00","09:30"], post: ["16:00","20:00"], night: ["20:00","04:00"],
        extended_tradable: false }
  hk: { tz: Asia/Hong_Kong, regular: [["09:30","12:00"],["13:00","16:00"]] }
  cn: { tz: Asia/Shanghai,  regular: [["09:30","11:30"],["13:00","15:00"]] }
```

### 2.3 依赖变更

Windows 无自带 IANA tzdata，`ZoneInfo("America/New_York")` 会抛 `ZoneInfoNotFoundError`。
已在 `.venv` 安装 **`tzdata 2026.6`**（纯数据包，无代码执行），实测：

```
纽约 2026-07-15 12:00 → -04:00 (EDT)
纽约 2026-11-04 12:00 → -05:00 (EST)
纽约 2026-11-01 01:30 → -04:00；03:00 → -05:00（DST 切换点正确）
```

---

## 3. US-P0-2：把 `us` 纳入强制校验，并让检查真的生效

### 3.1 配置变更

```yaml
market_rules:
  enforce_markets: ["cn", "us"]        # 原为 ["cn"]
  unenforced_market_policy: "warn"     # 新增
```

`us` 板块：

| 字段 | 修改前 | 修改后 | 依据 |
|---|---|---|---|
| `meta.status` | UNVERIFIED | **VERIFIED** | 见下行来源 |
| `meta.source_tier` | none | **primary** | 法规原文 + 交易所官方页 |
| `meta.source` | 空 | NYSE Holidays & Trading Hours；17 CFR 242.612；17 CFR 240.15c6-1 | — |
| `tick_size` | 无此字段 | **0.01** | 17 CFR 242.612(b)(2) |
| `tick_size_below_1` | 无此字段 | **0.0001** | 17 CFR 242.612(b)(3)/(c) |
| `not_modeled` | `luld, settlement, session_dst` | `luld, settlement_t1_funds, mwcb, short_sale_ssr, order_types, pdt` | `session_dst` 已不再是缺口 |

### 3.2 新增价格精度校验（`CODE_PRICE_TICK`）

`BoardRule` 新增 `tick_size / tick_size_below_1 / tick_size_half_cent`，
`validate_order` 第 8 项用 `Decimal` 判整倍数（避开 `10.005/0.005=2000.9999…` 浮点尾差）。

**为什么不是简单的 0.01**：17 CFR 242.612 是**逐股、半年一调**的三档制
（≥$1 为 $0.01；≥$1 且 TWAS ≤ $0.015 为 $0.005；<$1 为 $0.0001）。
把 tick 写死 0.01 会把 <$1 的股票"过严 100 倍"。因此：

* `tick_size=0.01`、`tick_size_below_1=0.0001` 已按条款落地；
* `tick_size_half_cent`（$0.005 档）**默认留空** —— 需要 TWAS 数据源，未取得就**不启用**，
  并在 `not_modeled` 之外留了显式字段，避免"假装支持"。

### 3.3 堵住 `applied=False` 的旁路

原实现里"市场不在 `enforce_markets`"→ `applied=False` → `violations=[]`，
上层看到"没有违规"就放行，等于规则层可以被一个字配置旁路。

新增 `unenforced_market_policy`：

| 取值 | 行为 |
|---|---|
| `warn`（当前） | `applied=False` + 明确 note（"未建模项：…"），上层记录"未强制校验" |
| `reject` | `applied=True` + `CODE_MARKET_NOT_ENFORCED` 违规 → 真正拒绝 |

美股已在 `enforce_markets` 内，走的是完整校验路径（`applied=True`），
因此价格精度、数量、时段、日历**都真跑**。`reject` 模式有专门测试覆盖，
随时可切成更严格的姿态（见 §7 遗留项 1）。

---

## 4. US-P0-3：持仓查询补 `use_us_pre / use_us_post / use_us_night`

### 4.1 官方接口事实（来自 `sdk_src`，未修改）

```python
# fsopenapi/api/portfolio.py:18
def get_holdings(self, sub_account_id=None, start=0, count=100, product_types=None,
                 currencies=None, symbols=None,
                 use_us_pre=False, use_us_post=False, use_us_night=False, ...):
    payload = { ..., "useUsPre": bool(use_us_pre),
                     "useUsPost": bool(use_us_post),
                     "useUsNight": bool(use_us_night), ... }
```

**修改前的实现只传了 `sub_account_id / start / count / apply_account_id`**，
三个美股时段开关全部走 SDK 默认值 `False` —— 盘前/盘后/夜盘建仓的持仓
可能**根本不在返回结果里**，那些仓位会完全脱离止损与敞口控制。

### 4.2 修改

`broker/fosun_adapter.py`：

```python
def _position_session_flags(self, overrides=None) -> Dict[str, bool]:
    # 默认三项全 True：宁可多看持仓（多一次风控），不能漏一段敞口
    cfg = self.cfg.fosun.get("position_sessions") or {}
    return {k: bool(overrides.get(k, cfg.get(k, True)))
            for k in ("use_us_pre", "use_us_post", "use_us_night")}

def get_positions(self, **kwargs) -> List[Position]:
    flags = self._position_session_flags(kwargs)
    self._last_position_flags = dict(flags)
    resp = self._read(client.portfolio.get_holdings, ...,
                      use_us_pre=flags["use_us_pre"],
                      use_us_post=flags["use_us_post"],
                      use_us_night=flags["use_us_night"])
```

* `health()["position_sessions"]` 现在给出 `{use_us_pre, use_us_post, use_us_night, last_query, warning}`；
  **关掉任一项都会产生 warning**（"那些持仓将脱离风控"）。
* `Position` 新增 `session` 字段：券商若返回 `tradeSession/sessionType/session` 就带上，
  否则留空 —— **不猜**。空值同样进入风控（不允许"不知道时段"就成为排除理由）。
* 配置项 `fosun.position_sessions` 三项均为 `true`。

### 4.3 查询失败必须 fail-closed

保持并加强既有约束：

| 场景 | 行为 |
|---|---|
| 响应结构不认识 | 抛 `BrokerError(PARSE_MISMATCH)`，**绝不返回 `[]`** |
| 持仓行缺标的代码 | 抛 `BrokerError(PARSE_MISMATCH)` |
| 网络/鉴权异常 | 异常向上传播；`scan()` 捕获**任何** `Exception` → 整轮停手 |

`StrategyEngine.scan()` 原来的 `except BrokerError` 已放宽为 `except Exception`：
非 `BrokerError` 的意外异常（`KeyError`、`ConnectionError`…）此前会直接冒到 worker，
现在统一转成 `blocked_reason="持仓读取失败，禁止交易：…"` 并落审计。

---

## 5. US-P0-4：风险处置路径不得被行情/时段错误阻断

逐条核查了 4 条风险处置路径，发现并修复 4 个真实缺陷：

### 缺陷 1：单日亏损熔断会拦住止损（严重）

`RiskController.evaluate` 第 12 项对**所有方向**生效 —— 一旦当日亏损触及上限，
连"卖出止损"也被拒。等于"亏到线以后不许跑"。同项在 `day_pnl_known=False` 时也拒绝，
于是"券商不回 dayPnl"会同时封死开仓与平仓。

```python
# 修复后
if intent.is_exit:
    self._check(checks, "daily_loss_limit", True,
                "减仓动作不适用单日亏损熔断（越亏越必须允许离场）")
elif portfolio.equity > 0:
    ...
```

### 缺陷 2：市场开关会拦住止损

`market_enabled` 对减仓同样生效 —— 配置里把某市场 `enabled` 置 false，
已有持仓就再也平不掉。改为：减仓放行 + 记 `warnings`（配置错误不该变成资金风险）。

### 缺陷 3：规则层的时段/日历检查拦住减仓（B4_B5_FINAL_REVIEW §2.10 的 C4）

`MarketRuleSet.validate_order` 的 session/calendar 检查不区分方向。
修复：`is_exit=True` 时改用 `extended=True` 判定，仍不通过则**只留痕不拦截**
（`RuleCheckResult.note` 会带 `减仓豁免时段检查：…`）。
理由：券商侧若真的不可交易会自行拒单；我们不替券商做"不许平仓"的决定。

同时修掉一个连带错误：日历判断原来用 `now.date()`（**本机日期**），
已改为 `self.local_date(market, now)`（交易所当地日期）。
纽约 15:30（北京次日 03:30）此前会被拿去查北京日期。

### 缺陷 4：行情失败 → 该标的止损静默失效

`monitor()` 里 `except BrokerError: continue` —— 行情取不到就跳过，这条持仓
**完全没有止损**，且只在内存里消失，没有任何告警。

修复：行情失败时退化用**券商持仓里的最新价**（交易所回报的 mark，不是本地造的价格）：

```python
except Exception as exc:
    mark = float(getattr(p, "last_price", 0.0) or 0.0)
    if mark > 0:
        quotes[p.symbol] = Quote(symbol=..., last=mark, source="position_mark")
        self.journal.log("quote_fallback_position_mark", {...})
    else:
        self.journal.log("quote_unavailable", {...})   # 明确告警"无法评估止损"
```

### 附加修复：持仓查询失败时，止损仍要继续评估

`monitor()` 原来直接 `positions = self.broker.get_positions()`，异常会让整轮
监控崩掉（止损、撤单、对账全部停摆）。新增 `_positions_for_monitor()`：

```
broker.get_positions() 成功 → 返回，并缓存快照
               ↓ 失败
用"最近一次成功的真实持仓快照"（带价格、带时段）
   + 本地止损登记里缺失的标的（qty 来自成交回报，session="local_stops"）
               ↓ 都没有
返回 []，但写 `positions_unavailable` 审计（明确"本轮无法评估止损"）
```

同时 `_portfolio_for_exit()`：`build_portfolio()` 失败时构造**降级 Portfolio**
（`degraded=True`、权益/现金未知），只允许减仓；`portfolio_integrity` 检查会拦住
基于降级快照的任何**新开仓**。

### 未知委托

未改动（P0-1 已实现）：`OrderStateUnknown` → 落 `unknown` → 封锁该标的 → `monitor`
二次对账；**查询失败不等于"没有这张单"**，保持封锁。这一条在
`test_order_state_integrity.py` 与 `test_us_p0.py` 都有覆盖。

---

## 6. 修改文件清单（含原因与行数）

`+/-` 为相对备份 `backups/us_p0_20261108-183854` 的行数。

| # | 文件 | +/- | 修改原因 |
|---|---|---|---|
| 1 | `src/ai_trader/market/clock.py` | **新增 369** | 交易所时钟：`zoneinfo` 时区换算、常规/盘前/盘后/夜盘阶段、提前收市、跨午夜 |
| 2 | `src/ai_trader/market/rules.py` | +305 / -44 | 接入时钟；`session_status`/`local_date` 改造；分市场日历 + `covered_until`；`tick_size` 与 `CODE_PRICE_TICK`；`unenforced_market_policy`；减仓豁免时段/日历 |
| 3 | `src/ai_trader/risk/controller.py` | +61 / -30 | 删北京硬编码窗口；`is_market_open` 走时钟；`market_hours` 走配置；日亏/市场开关对减仓豁免；`Portfolio.degraded` + `portfolio_integrity` 检查 |
| 4 | `src/ai_trader/strategy/engine.py` | +132 / -8 | `scan()` 放宽异常捕获；`_positions_for_monitor` / `_portfolio_for_exit` / 持仓快照缓存；行情失败退化到持仓 mark；`status()` 增加 `sessions` / `positions_stale` |
| 5 | `src/ai_trader/broker/fosun_adapter.py` | +40 / -1 | `get_positions` 传 `use_us_pre/post/night`；`_position_session_flags`；`health().position_sessions`；`Position.session` 解析 |
| 6 | `src/ai_trader/broker/base.py` | +10 / -1 | `Position.session` 字段；`Broker.get_positions(**kwargs)` 契约 |
| 7 | `src/ai_trader/broker/paper.py` | +2 / -1 | 签名对齐（模拟盘忽略 `use_us_*`） |
| 8 | `src/ai_trader/market/__init__.py` | +24 / -1 | 导出时钟与新违规码 |
| 9 | `src/ai_trader/cli.py` | +20 / -2 | `check-env` 增加 `sessions`（各市场当地时刻/阶段）、`calendar_markets`、`clock_errors`、`unenforced_market_policy` |
| 10 | `config/config.yaml` | +95 / -32 | `sessions`（三市场时区）；`enforce_markets` 加 `us`；`unenforced_market_policy`；`us` 板块 tick/T+0/VERIFIED/来源；`calendar.markets.us`（2026 节假日 + 2 个早收市）；`fosun.position_sessions` |
| 11 | `tests/helpers.py` | +2 / -1 | `ScriptedBroker.get_positions(**kwargs)` 记录 kwargs |
| 12 | `tests/test_risk_controls.py` | +6 / -1 | 修正 `test_market_hours_helper` 的**美股断言**（原断言编码了北京窗口 bug） |
| 13 | `tests/test_us_p0.py` | **新增 659** | 38 个 US-P0 验收用例 |
| 14 | `audit_evidence/probe_us_p0.py` | **新增 145** | 只读探针：时区/DST/新旧判定对照/配置生效/安全开关 |
| 15 | `audit_evidence/mutation_plugin.py` | +约 80 | 新增 4 个 US-P0 变异模式 |
| 16 | `requirements.txt` | +6 / -0 | 增加 `tzdata>=2024.1`（Windows 无 IANA tzdb，缺它 `ZoneInfo("America/New_York")` 直接抛错） |
| 17 | `DEPLOYMENT.md` | +3 / -0 | 部署引导说明 `tzdata` 的必要性（缺它 → 时段判定整体 fail-closed，表现为"永远不在交易时段"） |

**哈希清单比对结果（相对备份，实测）**：

```
变更文件 (12):
  config/config.yaml
  requirements.txt
  src/ai_trader/broker/base.py
  src/ai_trader/broker/fosun_adapter.py
  src/ai_trader/broker/paper.py
  src/ai_trader/cli.py
  src/ai_trader/market/__init__.py
  src/ai_trader/market/rules.py
  src/ai_trader/risk/controller.py
  src/ai_trader/strategy/engine.py
  tests/helpers.py
  tests/test_risk_controls.py
新增（不在清单）: src/ai_trader/market/clock.py, tests/test_us_p0.py,
                  audit_evidence/probe_us_p0.py
SDK: 0 变更（26 文件 sha256 与 audit_evidence/sdk_baseline.json 完全一致）
```

### 关于第 12 项（唯一被修改的旧测试）

```python
# 修改前（断言的是 bug）
assert is_market_open("us", WEEKDAY_1000.replace(hour=22, minute=0))[0] is True
# 修改后（断言正确行为）
assert is_market_open("us", WEEKDAY_1000.replace(hour=22, minute=0))[0] is False  # 22:00 北京 = 09:00 EST 盘前
assert is_market_open("us", WEEKDAY_1000.replace(hour=22, minute=30))[0] is True  # 22:30 北京 = 09:30 EST 开盘
```

`WEEKDAY_1000 = datetime(2026, 11, 4, 10, 0)`，2026-11-04 已在 EST。
这条断言原本把"北京 22:00 开市"当作正确行为 —— 正是 US-B3 缺陷本身。
A 股/港股相关断言**一行未改**。

---

## 7. 遗留问题（诚实清单）

| # | 问题 | 级别 | 为什么不本轮做 |
|---|---|---|---|
| 1 | `unenforced_market_policy` 仍为 `warn`（`hk` 未强制校验只标注不拒绝） | P1 | 改成 `reject` 会让现有 15 个港股用例失败 —— 那属于"影响非目标市场"。建议在正式只交易美股时切成 `reject`（已有测试覆盖该模式） |
| 2 | $0.005 逐股 tick 档未启用 | P1 | 需 TWAS 数据源（季度评估期数据），无数据就不能假装支持；已留 `tick_size_half_cent` 字段 |
| 3 | LULD / MWCB / SSR / PDT / 订单类型与 TIF 未建模 | P1 | 官方条款未取得，**拒绝猜测**；已写入 `not_modeled` 并在 `check-env` 可见 |
| 4 | 夜盘（20:00–04:00 ET）窗口是配置定义，非交易所规则 | P2 | 夜盘属券商/ATS 行为（如 Blue Ocean），星财富是否支持未验证；`extended_tradable=false` 确保不靠它开仓 |
| 5 | `market_feed.enforce_markets` 仍为 `["cn"]` | P2 | 推送默认关闭（`enabled=false`），美股推送需行情权限，属 B5 后续 |
| 6 | 日历仅覆盖 2026 年 | 已缓解 | `covered_until: 2026-12-31` 之后一律拒绝；2027 年前必须录入 NYSE 新表 |
| 7 | 跨午夜夜盘归属上一交易日 | P2 | 当前只影响 `phase` 标签，不影响开仓判定（夜盘不开放新仓） |
| 8 | `halt_storage` 异常仍拦截**所有**方向（含减仓） | 待定 | 这是 P0-2 审计要求（"存储异常必须拒绝交易"）；与 US-P0-4 存在张力，需人工决策是否对减仓开口子 |

---

## 8. 回滚方法

本项目无 Git，回滚用备份 + 哈希清单：

```powershell
# 1) 查看备份
Get-ChildItem C:\FosunAI-Trading\backups\us_p0_20261108-183854

# 2) 逐文件回滚（示例：只回滚股票规则与配置）
Copy-Item C:\FosunAI-Trading\backups\us_p0_20261108-183854\src\ai_trader\market\rules.py `
          C:\FosunAI-Trading\src\ai_trader\market\rules.py -Force
Copy-Item C:\FosunAI-Trading\backups\us_p0_20261108-183854\config\config.yaml `
          C:\FosunAI-Trading\config\config.yaml -Force

# 3) 删除本轮新增文件（它们不在清单里，回滚时必须手工删）
Remove-Item C:\FosunAI-Trading\src\ai_trader\market\clock.py
Remove-Item C:\FosunAI-Trading\tests\test_us_p0.py

# 4) 校验回滚是否完整：与 MANIFEST 比对，应 0 差异
python -c "import hashlib,json,pathlib; m=json.load(open(r'C:\FosunAI-Trading\backups\us_p0_20261108-183854\MANIFEST.sha256.json')); f=m.get('files',m); bad=[k for k,v in f.items() if hashlib.sha256(pathlib.Path(k).read_bytes()).hexdigest()!=v]; print('差异:',bad)"
```

回滚后需一并回退依赖：`pip uninstall tzdata`（否则只是多一个数据包，无副作用；
但 `clock.py` 删除后不再引用它）。

---

## 9. 是否允许进入独立复核

**允许。** 依据：`346 passed, 1 skipped, 0 failed`（含 38 个新增 US-P0 用例），
4 个方向的变异检验全部被抓住，SDK 26 个文件 sha256 与基线完全一致，
安全开关保持 `LIVE_TRADING=false / ORDER_EXECUTION_ENABLED=false / PAPER_TRADING=true`。

详细测试证据见 `US_P0_TEST_REPORT.md`。
