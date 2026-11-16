# US_P0_INDEPENDENT_REVIEW.md —— US-P0 独立最终复核

**项目**：`C:\FosunAI-Trading`
**复核时间**：2026-11-08 19:19 – 19:35 (+08:00)
**复核性质**：**只读独立复核**。未修改任何代码、配置、数据库、密钥或第三方 SDK；
未连接真实账户；未发送任何订单（所有券商交互均为进程内 `Broker` 契约替身，不联网）。
**复核人立场**：不复用实现方提供的测试日志，所有结论均由本次独立执行产生。

---

## 0. 复核方法与证据口径

| 手段 | 说明 |
|---|---|
| 哈希校验 | `MANIFEST.sha256.json`（70 文件）与 `sdk_baseline.json`（26 文件）全量重算 |
| 独立 oracle 对照 | 自写参考实现（仅用 `zoneinfo` 判断"纽约当地 09:30–16:00 + 非周末 + 非休市"），与 `MarketRuleSet.session_status` 对比 |
| 密集网格 | 4 个时间窗 × 每 5 分钟 = **6048 个采样点**，每个点同时用「纽约时间」与「北京时间」两种表达校验一致性 |
| 边界矩阵 | 夏令时/冬令时 ×（开盘前一分钟/开盘/收盘前一分钟/收盘/收盘后一分钟）+ 周末 + 节假日 + 早收市 |
| 端到端链路追踪 | 用 fake HTTP client 捕获**官方 SDK 实际构造的 POST payload**，验证 `useUsPre/Post/Night` 是否真送达 |
| 故障注入 | 持仓接口抛错、行情接口抛错、HTTP 返回 `None`、未知形状、缺字段；数据库 `log/find_orders/list_orders/orders_since` 分别抛错 |
| 全量回归 | 现场重跑 `pytest tests/`（不引用既有日志） |
| 决策量化 | 在**进程内存中**把 `unenforced_market_policy` 改为 `reject` 后重跑全套测试，量出真实破坏面（未写任何文件） |

---

## 1. 保护现场（第一组，7 项）

### 1.1 Git 是否存在

```
$ git status
fatal: not a git repository (or any of the parent directories): .git
$ ls -d .git
ls: cannot access '.git': No such file or directory
```

**结论**：✅ 无 Git 仓库；**复核期间未执行 `git init`**，也未创建任何 `.git` 目录。

### 1.2 备份目录

```
backups/us_p0_20261108-183854/
├── MANIFEST.sha256.json   （created_at=20261108-183854, root=C:\FosunAI-Trading）
├── .env.example  pyproject.toml  requirements.txt
└── config/  scripts/  src/  tests/
```

**结论**：✅ 存在，结构完整。

### 1.3 MANIFEST 全量校验（备份副本自身）

```
文件总数: 70
[3] 备份副本校验: 全部一致
```

**结论**：✅ 70/70 备份副本与清单哈希完全一致（备份未被篡改、可回滚）。

### 1.4 当前修改文件 SHA256（相对修复前）

```
[4] 当前树 vs 修复前基线：58 未变 / 12 变更 / 0 缺失
    ~ config/config.yaml                        fca9e31e2def1768 → 724394918e72a581
    ~ requirements.txt                          e2b65c6a2dc38264 → cfc4f91cfeafff1a
    ~ src/ai_trader/broker/base.py              37678553b8b25f79 → 10b7f6c11a5de7f0
    ~ src/ai_trader/broker/fosun_adapter.py     bb0fb679d336cd87 → f6ec7bb86ba1c573
    ~ src/ai_trader/broker/paper.py             1811b057b5322d23 → 109dbed41c4c7038
    ~ src/ai_trader/cli.py                      1adf89c9ce251ec8 → c1b80ef061608fe4
    ~ src/ai_trader/market/__init__.py          d199e1c4d53b4973 → 4e8a0fa1aa9192af
    ~ src/ai_trader/market/rules.py             82d1008234e68998 → 0b4d8a52b35a97c8
    ~ src/ai_trader/risk/controller.py          29d2649e7076c202 → 1e735500b32ab2e7
    ~ src/ai_trader/strategy/engine.py          4b2a37fcd38e234a → 23cbe0fe6eba0f66
    ~ tests/helpers.py                          b3942a7eca6f44bb → 2373e31430a32d39
    ~ tests/test_risk_controls.py               b9f46ef55644e137 → bcaf8b3a8b01287c
```

新增文件（不在清单内）：`src/ai_trader/market/clock.py`、`tests/test_us_p0.py`、
`audit_evidence/probe_us_p0.py`。

**结论**：✅ 与实现报告声明的改动面一致；复核结束时重算仍为同样 12 个变更，
**复核期间零写入**。

### 1.5 SDK 26 文件完整性

```
基线元信息: {'sdk_commit': 'c4c8a51aef11053a823aa1ef9a4af114ecc1fd3e', ...}
文件数 26；不一致 0
```

附带核查：`sdk_src` 下另有 6 个非库文件（`setup.py` + `example/*.py`），
mtime 均为 `2026-11-08 11:36`（远早于修复窗口 18:38），未被触碰。

**结论**：✅ 第三方 SDK 一字节未改。

### 1.6 data / .env / vault 是否被修改

```
data/  HALT.resumed.json  13:01
       audit.db           13:00
       audit.db-shm       13:54
       audit.db-wal       13:54
       paper_state.json   11:46
       worker_heartbeat.json 13:29
.env                      11:57
.env.example              11:57
data/vault.json           不存在
```

修复窗口为 18:38–19:13，复核窗口为 19:19–19:35。**`data/` 内所有文件 mtime
均 ≤ 13:54，`.env` 为 11:57，均早于两个窗口**；`vault.json` 不存在（无机密可泄露）。

**结论**：✅ 用户数据、数据库、密钥、凭证均未被修改（也未被创建）。

### 1.7 三个交易开关

```
LIVE_TRADING            = False
ORDER_EXECUTION_ENABLED = False
PAPER_TRADING           = True
KILL_SWITCH             = False

运行时实测：
  can_connect_broker    = (False, 'PAPER_TRADING=true，真实券商连接被禁用')
  can_send_real_order   = (False, 'PAPER_TRADING=true，真实券商连接被禁用')
  can_cancel_real_order = (False, 'PAPER_TRADING=true，真实券商连接被禁用')
  live_authorized       = False
  real_order_authorized = False
```

**结论**：✅ 三开关保持安全默认，门禁对连接/下单/撤单三处一致拒绝。

### 1.8 保护现场小结

**7/7 通过。** 现场可回滚、SDK 零改动、安全开关关闭、用户数据零触碰。

---

## 2. US-P0-1 独立复核（交易所时钟）

### 2.1 代码审查结论

| 检查项 | 结论 | 直接证据 |
|---|---|---|
| 是否使用 `America/New_York` | ✅ | `clock.py:31` `TZ_BY_MARKET = {"us": "America/New_York", ...}`；`DEFAULT_SESSION_SPEC["us"]["tz"]` 同 |
| 是否使用真实 IANA 时区库 | ✅ | `from zoneinfo import ZoneInfo`；未手写任何 DST 规则 |
| Windows 是否真能加载 tzdata | ✅ | `tzdata 2026.6`；`ZoneInfo("America/New_York")` 实测成功；`ZoneInfo("America/Nowhere")` 抛 `ZoneInfoNotFoundError` |
| 是否按纽约当地交易日判星期 | ✅ | `session()` 用 `loc.date()`（`loc = astimezone(tz)`）判 `weekday()`，不再用本机日期 |
| 是否正确处理 DST | ✅ | 2026-07-15 → `-04:00`，2026-11-04 → `-05:00`；切换点 01:30→`-04:00`、03:00→`-05:00` |
| 是否正确处理跨午夜 | ✅ | 夜盘 `20:00–04:00` 用"或"判断；实测 纽约 02:00 → `phase=night` |
| 是否区分盘前/常规/盘后/夜盘 | ✅ | 见 2.4 阶段表 |
| 是否存在绕过 `ExchangeClock` 的时段判断 | ⚠️ **无代码旁路，但有第二份"默认时段常量"** | 全仓 `zoneinfo` 仅出现在 `clock.py`；`rules.py:283-287` 的 `DEFAULT_SESSIONS` 仍存在，只在"配置未给 sessions 时"作为兜底，且其 `us` 已改为 `09:30–16:00`（交易所当地时间），由 `build_clock` 解释 |

**关键设计核查（防 fail-open）**：

* `ExchangeClock.configured=False`（没配常规窗口）→ `phase=closed`，**不用默认窗口顶替**。
* `load_tz` 失败 → `ClockError` → `MarketRuleSet.clock_errors[mkt]` → `session_status` 恒 `False`；
  `local_date()` 返回 `None` → `validate_order` 追加 `NOT_TRADING_DAY`。
  实测：把 `sessions.us.tz` 设为 `America/Nowhere` 后，正常交易时刻 `check_order` 返回
  `['OUT_OF_SESSION','NOT_TRADING_DAY']` —— **双保险 fail-closed**。

### 2.2 独立 oracle 密集对照（6048 点）

参考实现完全独立（只用 `zoneinfo` + 官方休市日/早收市表）：

```python
def oracle_open(ny_dt):
    d, t = ny_dt.date(), ny_dt.time().replace(tzinfo=None)
    if d.weekday() >= 5: return False
    if d in HOL: return False
    return time(9,30) <= t < EARLY.get(d, time(16,0))
```

覆盖 4 个时间窗（含 DST 前后各一周、纯 EDT 周、含感恩节+早收市周），每 5 分钟采样，
并对每个瞬间同时用纽约时间与北京时间两种表达调用生产接口：

```
网格采样 6048 个时点（含纽约/北京两种表达），不一致 0
```

**结论**：✅ 与独立参考实现 **零偏差**，且两种时区表达结果完全一致（证明时区换算无方向性错误）。

### 2.3 边界矩阵（本次独立执行）

| 场景 | 纽约当地时间 | 判定 | 备注 |
|---|---|---|---|
| 夏令时 开盘前一分钟 | 07-15 09:29 EDT | 休市 | 盘前 04:00-09:30 |
| 夏令时 开盘时刻 | 07-15 09:30 EDT | **开市** | 常规时段 09:30-16:00 |
| 夏令时 收盘前一分钟 | 07-15 15:59 EDT | 开市 | — |
| 夏令时 收盘时刻 | 07-15 16:00 EDT | 休市 | 盘后 16:00-20:00 |
| 夏令时 收盘后一分钟 | 07-15 16:01 EDT | 休市 | — |
| 冬令时 开盘前一分钟 | 11-04 09:29 EST | 休市 | — |
| 冬令时 开盘时刻 | 11-04 09:30 EST | **开市** | — |
| 冬令时 收盘前一分钟 | 11-04 15:59 EST | 开市 | — |
| 冬令时 收盘时刻 | 11-04 16:00 EST | 休市 | — |
| 冬令时 收盘后一分钟 | 11-04 16:01 EST | 休市 | — |
| 周六 | 11-07 12:00 EST | 休市 | 当地周末 |
| 周日 | 11-08 12:00 EST | 休市 | 当地周末 |
| 感恩节（周四） | 11-26 10:00 EST | 休市 | 在当地节假日表内 |
| 早收市 12:59 | 11-27 12:59 EST | **开市** | 常规 09:30-**13:00** |
| 早收市 13:00 | 11-27 13:00 EST | 休市 | 盘后 13:00-20:00 |
| 早收市 13:01 | 11-27 13:01 EST | 休市 | — |

**16/16 通过。**

### 2.4 DST 切换瞬间（不存在 / 重复的当地时间）

| 场景 | 结果 |
|---|---|
| 春季前跳 2026-03-08 01:59（EST） | 正常判定，无异常 |
| 春季前跳 2026-03-08 02:30（**该时刻不存在**） | 正常判定，无异常（Python 归一到 `-05:00`） |
| 春季后 2026-03-08 03:30（EDT） | 正常判定 |
| 秋季回拨 2026-11-01 01:30（**重复两次**） | `fold=0` → `-04:00`，无异常 |
| 秋季回拨 2026-11-01 01:30 `fold=1` | `-05:00`，无异常 |

补充（因 DST 切换发生在**周日**，需另测切换后的**首个交易日**）：

```
03-09 09:29 EDT 休市 | 03-09 09:30 EDT 开市 | 03-09 15:59 开市 | 03-09 16:00 休市
11-02 09:29 EST 休市 | 11-02 09:30 EST 开市 | 11-02 15:59 开市 | 11-02 16:00 休市

同一瞬间的北京时间：纽约 03-09 09:30 EDT = 北京 21:30 CST → 开市
                    纽约 11-02 09:30 EST = 北京 22:30 CST → 开市
```

**结论**：✅ DST 两个方向都正确，切换后首个交易日边界精确。

### 2.5 跨日 / 星期归属（北京时间观察轴）

```
北京周六 02:00 → 纽约周五 13:00   local=2026-11-06 wd=4 phase=regular  开市   ← 旧实现误判休市
北京周六 10:00 → 纽约周五 21:00   local=2026-11-06 wd=4 phase=night    休市
北京周一 02:00 → 纽约周日 13:00   local=2026-11-08 wd=6 phase=weekend  休市   ← 旧实现误判开市
北京周一 22:30 → 纽约周一 09:30   local=2026-11-09 wd=0 phase=regular  开市
北京周二 04:59 → 纽约周一 15:59   local=2026-11-09 wd=0 phase=regular  开市
北京周二 05:00 → 纽约周一 16:00   local=2026-11-09 wd=0 phase=post     休市
```

### 2.6 阶段区分

```
纽约 04:00 pre  | 09:29 pre | 09:30 regular | 15:59 regular
纽约 16:00 post | 17:00 post | 20:00 night  | 23:59 night
纽约 次日 02:00 night（跨当地午夜）
```

⚠️ 观察点：周五 21:00 之后跨到周六凌晨的夜盘，会被判为 `weekend`（按当地日期）。
**安全影响：无** —— `sessions.us.extended_tradable=false`，夜盘不开放新开仓；
减仓豁免时段检查。记录为已知建模缺口（见 §8 P3-3）。

### 2.7 US-P0-1 结论

**✅ 通过（有直接证据）**。8 项代码检查全过，6048 点 oracle 对照零偏差，
16 项边界矩阵全过，DST 双向与切换后首日精确，跨日/星期归属正确，
无代码级旁路，时区缺失时双保险 fail-closed。

---

## 3. US-P0-2 独立复核（美股规则真正生效）

### 3.1 是否真正进入所有开仓路径

**生产配置实测**：

```
enforce_markets = ['cn', 'us']
unenforced_market_policy = 'warn'
clocks = {'us': 'America/New_York', 'hk': 'Asia/Hong_Kong', 'cn': 'Asia/Shanghai'}
us 板块 = VERIFIED, lot=1, tick=0.01/<1=$0.0001, t+0
        not_modeled=['luld','settlement_t1_funds','mwcb','short_sale_ssr','order_types','pdt']
us 日历 = VERIFIED, 覆盖 2026-01-01~2026-12-31, 早收市 ['2026-11-27','2026-12-24']
```

**唯一规则入口**：

```
$ grep -rn "check_order(\|validate_order(" src/ai_trader
  market/rules.py:715  def check_order(...)
  market/rules.py:742      violations = self.validate_order(notes=notes, **kwargs)
  risk/controller.py:439   rule_check = self.rules.check_order(...)
```

**唯一下单出口**：

```
$ grep -rn "place_order" src/ai_trader
  broker/base.py:249          def place_order       （契约）
  broker/fosun_adapter.py:625 def place_order       （实现）
  broker/paper.py:306         def place_order       （实现）
  strategy/engine.py:517      result = self.broker.place_order(req)   ← 唯一调用点
```

`_execute()` 是唯一调用 `place_order` 的函数，其调用点为
`analyze_symbol()`（第 444 行）与 `monitor()`（第 838 行），两处都先经
`risk.evaluate()`，且 `_execute` 内部第一行为 `if not decision.approved: return`。

**四条执行入口全部汇聚到同一路径**：

| 入口 | 实现 | 是否经规则层 |
|---|---|---|
| CLI `scan` | `cli.py:38` → `rt.engine.scan()` | ✅ |
| CLI `monitor` | `cli.py:45` → `rt.engine.monitor()` | ✅ |
| Web `/api/scan` | `web/app.py:201` → `runtime.engine.scan()` | ✅ |
| Web `/api/monitor` | `web/app.py:207` → `runtime.engine.monitor()` | ✅ |
| Worker | `run_worker.ps1:186/197` → `python -m ai_trader.cli monitor/scan` | ✅ |
| Web 撤单 | `engine.cancel_order()`（不是开仓） | 撤单链，非开仓 |

Web 路由清单内**没有**任何直接下单端点（`/api/scan`、`/api/monitor`、`/api/kill`、
`/api/resume`、`/api/orders/{id}/cancel`、`/api/paper/reset`、若干只读 GET）。

**结论**：✅ 不存在代码级的开仓旁路。

### 3.2 `applied=False` 的 bypass 矩阵（实测）

对 `MarketRuleSet` 做**内存副本**变换（不改配置文件），观察 US/HK 两市场：

| 配置态 | US `applied` / 违规 | HK `applied` / 违规 |
|---|---|---|
| ① 生产配置（`["cn","us"]`, `warn`） | `True` / 无 | `False` / 无（"未建模"标注） |
| ② `policy=reject` | `True` / 无 | `True` / `MARKET_NOT_ENFORCED` ✅ |
| ③ `policy=REJECT`（大写） | `True` / 无 | `True` / `MARKET_NOT_ENFORCED` ✅（大小写归一） |
| ④ `enforce_markets` 去掉 `us` | **`False` / 无 → 规则层整体旁路** ⚠️ | `False` / 无 |
| ⑤ `market_rules.enabled=false` | **`False` / 无 → 旁路** ⚠️ | `False` / 无 |
| ⑥ `market_rules.enforce=false` | **`False` / 无 → 旁路** ⚠️ | `False` / 无 |
| ⑦ `us` 板块 `status=UNKNOWN` | `True` / `RULE_UNKNOWN` ✅ | — |
| ⑧ 删掉 `us` 板块 | `True` / `RULE_UNKNOWN` ✅ | — |

**结论**：规则**逻辑**无法被 `applied=False` 绕过（⑦⑧ 都正确拒绝），
但**配置**层面有三处"一键旁路"：④⑤⑥。当前无任何启动断言防止它们被改错（见 §8 P2-2）。

### 3.3 价格精度 / 数量 / 时段是否真生效

| 检查 | 实测 |
|---|---|
| tick `$0.01` | `price=150.003` → `PRICE_TICK`；`150.000` → 通过 |
| tick `< $1` 用 `$0.0001` | `0.5001` → 通过；`0.50005` → `PRICE_TICK` |
| $0.005 逐股档 | `tick_size_half_cent: []` —— **未假装支持** ✅ |
| 数量 | `qty=0` → `LOT_SIZE` |
| 时段 | 纽约 03:00 → `OUT_OF_SESSION` |
| 日历 | 圣诞节 → `NOT_TRADING_DAY` |

### 3.4 US-P0-2 结论

**✅ 通过（逻辑层）／⚠️ 部分通过（配置层）**：
不存在代码级旁路；但 `enabled`/`enforce`/`enforce_markets` 三者任一被改动即可整体旁路，
且**没有任何测试或启动检查会报警**（见 §6.5 与 §8 P2-2/P2-3）。

---

## 4. US-P0-3 独立复核（全时段持仓）

### 4.1 完整调用链（端到端实测）

用 fake HTTP client 承接**官方 SDK 真实构造的 payload**：

```
engine → broker.get_positions()  →  fosun_adapter.get_positions(**kw)
      → _position_session_flags() → SDK PortfolioAPI.get_holdings(...)
      → client.post("/v1/portfolio/Holdings", data=payload)

SDK POST: /v1/portfolio/Holdings
    useUsPre     = True
    useUsPost    = True
    useUsNight   = True
    start        = 0
    count        = 200
    全部字段: ['count','currencies','productTypes','start','symbols',
              'useUsNight','useUsPost','useUsPre']
```

| 检查项 | 结论 |
|---|---|
| 是否真实传入 SDK | ✅ payload 中三个布尔键确实存在且为 `True`（不是仅传进 Python 函数） |
| 中间层是否丢弃 | ✅ 显式 `use_us_night=False` → payload `useUsNight=False`；不传 kwargs → 回落到配置默认全 `True` |
| 默认值是否可能意外关闭某时段 | ✅ 代码兜底 `cfg.get(key, True)`：**即使配置项整个缺失**，payload 仍为三个 `True` |
| 查询失败是否被转成空持仓 | ✅ 见 4.2 |
| `health()` 能否识别配置不完整 | ✅ 缺两段时 `warning` 非空 |

### 4.2 失败路径（全部实测）

| 注入 | 结果 |
|---|---|
| HTTP 网络异常 | ✅ 异常向上传播（`RuntimeError`） |
| `data={}` | ✅ `BrokerError(PARSE_MISMATCH)` |
| 未知形状 `{"foo":[...]}` | ✅ `BrokerError(PARSE_MISMATCH)` |
| 行缺 `stockCode` | ✅ `BrokerError(PARSE_MISMATCH)` |
| HTTP 真返回 `None` | ✅ `BrokerError(PARSE_MISMATCH)` |
| 真空账户 `{"list": []}` | ✅ 返回 `[]`（合法空，不误报） |

`StrategyEngine.scan()` 对 `get_positions()` 的捕获已放宽为 `except Exception`，
实测持仓抛错时：`blocked_reason="持仓读取失败，禁止交易：boom"`，`place` 次数 **0**。

### 4.3 `health()` 完整性识别

```
全 True : {'use_us_pre': True, 'use_us_post': True, 'use_us_night': True,
           'last_query': {}, 'warning': ''}
缺两段  : {'use_us_pre': True, 'use_us_post': False, 'use_us_night': False,
           'last_query': {}, 'warning': '有盘前/盘后/夜盘持仓未被查询 —— 那些持仓将脱离风控'}
```

### 4.4 US-P0-3 结论

**✅ 通过（有直接证据）**，但存在一条**未验证**的链路末端（见 §9）：
官方网关是否真的按 `useUsNight` 返回夜盘仓位、以及持仓行里时段字段名是什么，
无凭证无法验证。

---

## 5. US-P0-4 独立复核（风险退出保护）

### 5.1 逐条核查（7 项）

| # | 要求 | 结论 | 证据 |
|---|---|---|---|
| 1 | 止损不受日亏熔断阻断 | ✅ | `is_exit` 分支直接置 `daily_loss_limit=True`；实测 日亏 −50%（上限 3%）时止损 `approved=True` 并发出卖单 |
| 2 | 止损不受市场开关阻断 | ✅ | 实测 `risk.markets={}`（市场停用）时止损仍 `approved=True`，仅加 warning |
| 3 | 止损不受休市/节假日规则阻断 | ✅ | 圣诞节 10:00 走完整链 → `kinds=['exit']`，券商收到 `('usAAPL','sell',10)`；规则层仅留痕"减仓豁免" |
| 4 | 行情失败仍有处置与告警 | ✅ | `get_quote` 全抛时用券商持仓 mark 判损并成交；审计写入 `quote_fallback_position_mark`；无 mark 时写 `quote_unavailable` 告警 |
| 5 | 持仓读取失败不假设空仓 | ✅ | `scan` 整轮停手；`monitor` 用"最近快照 + 本地止损登记"兜底，且 `_positions_stale=True` |
| 6 | 订单状态未知不重复提交 | ✅ | 第 1 次 `stage=ordered ... → unknown`（place=1）；第 2 次 `stage=rejected`，理由含"存在状态未知的委托…必须先完成对账"+"300s 内已有 buy 委托"；`place` 仍为 1 |
| 7 | 急停存储异常时减仓符合 fail-closed | ⚠️ **符合约束，但代价是"不能跑"** | 实测 `halt_store_failed=True` → `kinds=['exit_blocked']`，理由"急停状态存储异常，拒绝交易以免停止态失效"；`place=0` |

### 5.2 特别检查：`monitor()` 在 行情 / 持仓 / 数据库 异常下的可靠性

这是本次复核**最重要的一组测试**（故障注入，非 mock，用真实 `AuditJournal` 子类只让单个方法抛错）：

| 故障 | `monitor()` 表现 | 券商收到卖单 | 判定 |
|---|---|---|---|
| 一切正常 | `kinds=['exit']` | `('usAAPL','sell',10,180)` | ✅ |
| `get_quote` 全部抛错 | `kinds=['exit']` | `('usAAPL','sell',10,180)` | ✅ |
| `get_positions` 抛错 | `kinds=['positions_unavailable','exit']` | `('usAAPL','sell',10,180)` | ✅ |
| `journal.orders_since()` 抛错 | `kinds=['exit']` | `('usAAPL','sell',10,180)` | ✅（`_portfolio_for_exit` 已保护） |
| **`journal.log()` 抛错** | **✗ 抛异常 `RuntimeError`** | **无** | ❌ |
| **`journal.find_orders()` 抛错** | **✗ 抛异常 `RuntimeError`** | **无** | ❌ |
| **`journal.list_orders()` 抛错** | **✗ 抛异常 `RuntimeError`** | **无** | ❌ |

**分析**：

* `find_orders()` 在 `_expire_stale_orders()` 第一步就被调用 —— 崩在"读持仓之前"，
  连止损逻辑都没进入。
* `list_orders()` 在 `risk.evaluate(recent_orders=...)` 参数求值阶段崩 —— 崩在风控之前。
* `log()` 崩在 `_positions_for_monitor()` 的降级告警写入处（即"持仓失败时"这条路径本身会崩）。

**后果**：数据库一旦局部不可写/不可读（SQLite WAL 增长、磁盘满、杀软锁文件、
`audit.db` 被占用），`monitor()` 整体抛异常 → **本轮止损、撤单、未知单对账全部不执行**。
`cli.py` 的 `cmd_monitor` 未包 try/except；`run_worker.ps1` 只会写
`Write-Heartbeat "error"` 并以 exit 1 结束，**没有任何外部告警通道**（无邮件/webhook/通知）。

**这是 US-P0-4 "行情失败、持仓失败、数据库异常时仍能可靠处理已有持仓风险"的
唯一未通过项。**

### 5.3 额外发现：未知单未对清期间，同标的的止损被拦

```
第1次 analyze_symbol(usAAPL) → stage=ordered 放行 buy × 207 → unknown（place=1）
register_stops(usAAPL, stop=210, qty=10)
monitor() → kinds=['reconcile_failed','exit_blocked']
            理由: usAAPL 存在状态未知的委托（…clientId=577168849），必须先完成对账才能继续交易
            place 总数: 1 （止损未发出）
```

`_find_unresolved(sym)` 只按 **symbol** 匹配，不区分方向。当未知单是**买入**时，
持仓数量不确定（可能已成交也可能没有），拦截卖出在方向上是保守的；但
**代价是该标的在对账成功前完全没有止损保护**，若对账持续失败（网络问题），
止损永久失效。这是 "no naked assumption" 与 "must be able to exit" 之间的真实冲突。

### 5.4 额外发现：未来时间戳的订单会永久卡住冷却窗口（含减仓）

`_duplicate_check` 只判下界 `if ts >= cutoff`，**不判上界**。写入一条
`ts=2027-06-01` 的成交记录（模拟系统时钟回拨 / 券商时间戳异常）后：

```
减仓 approved = False
拦截项: ['duplicate_order']
理由: usAAPL 在冷却窗口内已成交 sell（2027-06-01T10:00:00-04:00），判定为重复
开仓 approved = True（方向不同，不受影响）
```

即：**一条未来时间戳的同向记录会让该标的同向委托永久被拒**，止损也在此列
（`duplicate_order` 未对 `is_exit` 豁免）。这又构成一条"无法执行风险退出"的路径。

### 5.5 US-P0-4 结论

**⚠️ 部分通过。** 7 项要求中 6 项通过（第 7 项"符合 fail-closed 约束"通过，
但代价是无法减仓）；**"数据库异常下仍能处置风险"这一项未通过**；
另外发现 3 条新的退出不可达路径（§8 P1-1 ~ P1-4）。

---

## 6. 测试复核

### 6.1 现场重跑（不引用既有日志）

```
$ .venv\Scripts\python.exe -m pytest tests/ -o addopts="" -q -p no:cacheprovider
346 passed, 1 skipped, 1 warning in 32.23s
exit=0
```

### 6.2 数量核实

| 项 | 声称 | 实测 | 一致 |
|---|---|---|---|
| passed | 346 | **346** | ✅ |
| skipped | 1 | **1** | ✅ |
| failed | 0 | **0** | ✅ |
| 合计 | 347 | **347** | ✅ |

唯一跳过项：`tests/test_ai.py::test_llm_real_call_if_key_present SKIPPED (未配置 …)`
—— 因未配置 AI API Key，属**合理跳过**，非掩盖失败。

### 6.3 四个 P0 的独立负向测试（本次自写）

| P0 | 负向场景 | 实测结果 | 判定 |
|---|---|---|---|
| P0-1 | 北京周一 02:00（= 纽约周日 13:00） | `regular_open=False`（旧实现会判开市） | ✅ |
| P0-2 | 美股标的在圣诞节开仓 | `['OUT_OF_SESSION','NOT_TRADING_DAY']` | ✅ |
| P0-3 | 持仓接口抛 `BrokerError` | `blocked_reason=持仓读取失败…`，`place=0` | ✅ |
| P0-4a | 日亏 −50% + 圣诞节休市 → 止损 | `kinds=['exit']`，卖单已发 | ✅ |
| P0-4b | 行情全失败 → 止损 | `kinds=['exit']`，卖单已发，审计 `quote_fallback_position_mark` 1 条 | ✅ |
| P0-4c | **`journal.log` 抛错 → 止损** | **`monitor` 抛异常，`place=0`** | ❌（§8 P1-1） |

### 6.4 是否有测试"只验证 helper、没有验证真实调用链"

对 `tests/test_us_p0.py` 的 38 个用例逐条归类：

| 类别 | 数量 | 说明 |
|---|---|---|
| 走完整调用链（engine / risk.evaluate / adapter） | **10** | 持仓异常→scan、持仓异常→止损、行情失败→止损、三开关送达、开关关闭告警、日亏不阻断止损、dayPnl 缺失不阻断、降级组合、市场停用、盘前拒绝开仓 |
| 直接验证规则层/时钟（`check_order`、`session_status`、`session_info`） | **24** | 这是**生产风控实际调用的同一函数**（`risk/controller.py:439 → rules.check_order`），属"入口级"而非"内部实现级" |

**结论**：✅ 不存在"只测 helper"的空转问题；规则层用例打的是风控真正使用的
`check_order`。但有一处**手法偏弱**：`test_all_session_positions_enter_risk_control`
用 `RiskController.__new__(...)` 绕过构造函数直接调用 `scan_exits`，
虽调用的是真实方法、但不覆盖"引擎怎么把该 intent 送进 evaluate"这一段
（该段由另外 3 个 monitor 级用例覆盖，故不构成实质缺口）。

### 6.5 是否存在 mock / monkeypatch / 测试配置掩盖生产行为

| 项 | 复核结论 |
|---|---|
| 时区是否被 mock | ✅ **没有**。全部使用真实 `zoneinfo` + 真实 `tzdata 2026.6`；测试一律传 aware datetime，与本机时区无关 |
| 数据库是否被 mock | ✅ 没有。用真实 SQLite 文件（`AuditJournal` 实盘落盘） |
| 并发是否被 mock | ✅ 没有。既有 7 项并发用例使用真实多连接/多进程 |
| 券商是否被 mock | ⚠️ 使用 `Broker` 契约替身（`Stub`/`ScriptedBroker`），**这是必要的**（禁止真实下单），且替身走真实调用路径，不是 `unittest.mock` |
| monkeypatch 使用 | 仅 2 处，均为**故障注入**（让 `ExchangeClock.session` 抛错、让 `build_clock` 抛 `ClockError`），不是掩盖成功路径 |
| **测试配置 vs 生产配置** | ⚠️ **存在实质性脱耦**：`test_us_p0.py` 中 25 处使用**手写** `_ruleset()`，只有 3 处用 `live_cfg`/`make_cfg`。**没有任何测试断言生产 `config/config.yaml` 里的 us 配置**（`grep enforce_markets tests/` 全部是覆盖值或手写值） |

**这一脱耦的实证后果**：`config/config.template.yaml`（新部署时的复制源）**未同步**
本轮改动 —— 仍是 `enforce_markets: ["cn"]`、无 `sessions` 块、`us` 板块 `UNVERIFIED`、
无 `calendar.markets.us`。测试全绿也发现不了（见 §8 P2-1）。

---

## 7. 专项决策审计

### 7.1 A：`unenforced_market_policy` 改为 `reject` 对美股生产配置的影响

**做法**：在进程内存中包装 `load_config` 把该项改为 `reject`，重跑全套测试（**未写任何文件**）。

```
28 failed, 316 passed, 3 skipped
```

**对美股的影响（实测，非推断）**：

| 场景 | `warn` | `reject` |
|---|---|---|
| 美股开仓 | approved=True | **approved=True（不变）** |
| 美股止损 | approved=True | **approved=True（不变）** |
| 港股开仓 | 被 `market_hours` 拦 | 被 `rule:MARKET_NOT_ENFORCED` + `market_hours` 双拦 |

**结论**：
1. **对美股零影响** —— 因为 `us` 已在 `enforce_markets` 内，本来就走 `applied=True` 分支。
2. 28 个失败全部是**港股相关用例**：它们把"hk 未建模 → 放行"当作前提写进断言
   （如 `test_clean_intent_is_approved`、`test_engine_flow`、`test_concurrency` 等）。
   这些**不是产品缺陷**，而是历史测试对"warn 语义"的依赖。
3. **"不因旧港股测试失败而降低美股生产安全标准"的判断**：`reject` 使规则层对
   *所有* 未建模市场默认拒绝，属**提高**安全标准。代价是 28 个测试需要改造
   （把 hk 夹具换为 us，或给 hk 配 VERIFIED 规则并纳入 enforce_markets）——
   **是测试改造成本，不是安全成本**。
4. 建议路径：`reject` + 分批改造 28 个 hk 用例（每批改造后回归），
   并在 `reject` 生效前给 `config` 加启动断言（§8 P2-2）。

### 7.2 B：`halt_storage` 异常时是否存在安全的 reduce-only 退出方案

**现状**：`halt_store_failed` 把两类完全不同的故障合并为一个标志：

| 故障类型 | 代码位置 | 语义 | 当前处置 |
|---|---|---|---|
| 启动/读取失败 | `config.py:294`（`safe_read` 转换后） | **"不知道是否被人为停止"** | 拒绝一切（含减仓） |
| 写盘失败 | `controller.py:286`（`halt()` 抛错后） | **"知道自己没停，但停不下来"** | 拒绝一切（含减仓） |
| 长驻进程刷新失败 | `controller.py:321`（`refresh_halt_state`） | 同读取失败 | 拒绝一切 |

**评估：可以设计出安全的 reduce-only 退出，但不是"无条件放开减仓"**。

可行方案（按推荐度排序）：

| 方案 | 机制 | 安全边界 | 风险 |
|---|---|---|---|
| **B-1 拆分两种故障语义**（推荐） | 新增 `halt_store_read_failed` / `halt_store_write_failed` 两个标志：<br>· **写失败** → 内存态明确"未停止" → 允许 reduce-only<br>· **读失败** → 仍拒绝一切 | 写失败时不存在"违反已知停止态"的可能；读失败时仍保持最严 | 需改代码，且要论证"内存态可信"（进程刚重启时内存态不可信 → 必须按读失败处理） |
| **B-2 reduce-only + 硬约束** | 允许卖，但要求同时满足：① 券商实时持仓可读；② `qty ≤ 持仓`；③ 本地 `stops.json` 有该标的登记；④ 价格偏离 ≤ 阈值；⑤ 强制审计 + 外部告警 | 排除"借道减仓开反向仓" | 停用了"持仓也可能不准"的假设；需 B-3 兜底 |
| **B-3 券商侧保护单**（架构级正解） | 建仓时同时在券商挂 stop 单，退出不依赖本进程 | 进程/存储全挂时仍能退出 | 需券商支持 Stop/Stop-Limit 与 TIF，**当前订单类型枚举未验证**（US-B6） |
| B-4 人工兜底 | CLI `ai-trader sell` 直连券商 App | 最终安全网 | 依赖人工在场，不满足无人值守 |

**明确不建议**：直接把 `halt_storage` 检查改为 `is_exit` 豁免。理由：读失败时
"人可能刚按了急停"，此时放行卖出会绕过人工停止指令，违背 P0-2 的原始审计要求。

**结论**：B-1（拆分语义）+ B-2（reduce-only 硬约束）是当前代码结构下最小可行的安全方案；
B-3 是终局方案但受 US-B6 阻塞。**本轮不修改，作为 US-P1 决策项提交。**

---

## 8. 分类清单

### 8.1 已通过且有直接证据

| # | 项 | 关键证据 |
|---|---|---|
| P-1 | 保护现场 7/7 | MANIFEST 70/70 一致、SDK 26/26 一致、`data/`/`.env` mtime 早于两个窗口、三开关 false/false/true |
| P-2 | 使用 `America/New_York` + 真实 IANA tzdata | `tzdata 2026.6` 可加载；`ZoneInfo` 为唯一时区来源 |
| P-3 | DST 双向正确 | 07-15 `-04:00` / 11-04 `-05:00`；切换点与切换后首个交易日边界精确 |
| P-4 | 按纽约当地日期判星期 | 北京周六 02:00 → 纽约周五 → 开市；北京周一 02:00 → 纽约周日 → 休市 |
| P-5 | 跨午夜 / 四阶段区分 | 夜盘 02:00 → `phase=night`；pre/regular/post/night 边界全对 |
| P-6 | 时区缺失 fail-closed | `clock_errors` → `session_status=False` + `OUT_OF_SESSION`/`NOT_TRADING_DAY` 双违规 |
| P-7 | 6048 点 oracle 零偏差 | 独立参考实现对照，纽约/北京双表达一致 |
| P-8 | 唯一规则入口 / 唯一下单出口 | `check_order` 仅被 `risk/controller.py:439` 调用；`place_order` 仅被 `engine.py:517` 调用 |
| P-9 | 四入口全部经规则层 | CLI scan/monitor、Web /api/scan、/api/monitor、worker 均 → `engine.scan/monitor` |
| P-10 | tick/数量/时段/日历真生效 | 150.003 拒 / 150.00 过 / 0.50005 拒 / qty=0 拒 / 03:00 拒 / 圣诞节拒 |
| P-11 | `applied=False` 未成为逻辑旁路 | `status=UNKNOWN`、板块缺失均正确 `RULE_UNKNOWN` 拒绝 |
| P-12 | `useUsPre/Post/Night` 真送达 SDK | 捕获 `/v1/portfolio/Holdings` 的 payload，三个键均 `True` |
| P-13 | 默认值不会意外关闭时段 | 配置项整个缺失时仍为三个 `True`（代码兜底） |
| P-14 | 持仓失败绝不返回空 | 5 种畸形输入全部抛错；真空账户正确返回 `[]` |
| P-15 | `health()` 识别配置不完整 | 缺两段 → `warning` 非空 |
| P-16 | 止损不受日亏/市场开关/休市阻断 | 三条全链路实测 `kinds=['exit']` 并真实发单 |
| P-17 | 行情失败仍有处置+告警 | 退化到持仓 mark 并写 `quote_fallback_position_mark` |
| P-18 | 未知单不重复提交 | 第 2 次 `rejected`，`place` 计数保持 1 |
| P-19 | 测试数量真实 | 现场重跑 346 passed / 1 skipped / 0 failed，跳过项为无 AI Key |

### 8.2 部分通过

| # | 项 | 通过部分 | 未达标部分 |
|---|---|---|---|
| PP-1 | US-P0-4「数据库异常下仍能处置风险」 | 行情失败 ✅、持仓失败 ✅、`orders_since` 失败 ✅ | **`journal.log` / `find_orders` / `list_orders` 失败 → `monitor()` 崩溃，止损全部不执行** |
| PP-2 | US-P0-2「规则真正生效」 | 逻辑层无旁路 ✅ | **配置层 3 个一键旁路无守卫**（`enabled`/`enforce`/移除 `us`），且无测试锁定生产配置 |
| PP-3 | 测试覆盖 | 38 项新用例真实覆盖时钟/规则/链路 🠔 | 25/38 用**手写规则集**，生产 `config.yaml` 的 us 配置无回归断言 → 模板配置脱耦未被发现 |
| PP-4 | 止损可执行性 | 4 类常规异常下止损可用 | **4 条路径下止损不可达**（见 8.4） |

### 8.3 未验证（无凭证/无官方依据，无法在只读离线环境确认）

| # | 项 | 为什么无法验证 |
|---|---|---|
| U-1 | 官方网关是否真的接受并返回 `useUsPre/Post/Night` 对应仓位 | 需真实账户；当前只验证到"payload 正确发出" |
| U-2 | 持仓行里的时段字段名（`tradeSession`/`sessionType`/`session`） | 券商文档未给出；适配器做了三种兜底，但**未经验证** |
| U-3 | 夜盘（20:00–04:00 ET）是否真实存在、券商是否支持 | 属券商/ATS 行为，未取得依据 |
| U-4 | 订单类型 / TIF 枚举（含盘前盘后单） | 未取得官方枚举（US-B6） |
| U-5 | LULD / MWCB / SSR / PDT | 未取得官方条款，已列入 `not_modeled` |
| U-6 | `halt_storage` 异常在真实 VPS 上的触发概率与场景 | 需长跑；本轮仅做逻辑注入 |
| U-7 | 高并发/DB 锁竞争下的 `monitor()` 稳定性（长跑） | 需长跑；本轮只做单点故障注入 |

### 8.4 发现的新问题

| # | 级别 | 问题 | 证据 | 影响 |
|---|---|---|---|---|
| **N-1** | **P1** | `monitor()` 在 `journal.log()` / `find_orders()` / `list_orders()` 抛错时整体崩溃，止损/撤单/对账全部不执行；`cli.py` 未包 try/except，worker 只写 heartbeat=error，**无外部告警** | 5.2 表；3 种注入全部 `RuntimeError`，`place=0` | 数据库局部故障期间持仓**完全无保护** |
| **N-2** | **P1** | 未知单未对清时，**同标的的减仓**被 `unresolved_order_state` 拦截（检查不区分方向） | 5.3：`kinds=['reconcile_failed','exit_blocked']`，`place` 保持 1 | 对账持续失败 → 止损永久失效 |
| **N-3** | **P1** | `halt_store_failed` 时减仓被拦（= 既有 US-B11，本轮确认） | 5.1 第 7 项：`exit_blocked`，`place=0` | 急停存储故障期间无法离场 |
| **N-4** | **P1** | `_duplicate_check` 只判下界不判上界：一条**未来时间戳**的同向记录使该标的同向委托（含止损）永久被拒 | 5.4：`ts=2027-06-01` → 减仓 `duplicate_order` | 系统时钟回拨/券商时间戳异常 → 止损永久失效 |
| **N-5** | **P2** | `config/config.template.yaml` **未同步**本轮改动：仍是 `enforce_markets: ["cn"]`、无 `sessions` 块、`us` 板块 `UNVERIFIED`、无 `calendar.markets.us` | 直接比对；`grep sessions: → 0` | 按模板新部署 → 美股**丧失 tick/日历强制**（`applied=False`+`warn`），仅时段校验仍生效 |
| **N-6** | **P2** | 无启动断言保证 `us ∈ enforce_markets` 且 `enabled/enforce=true`；三者任一改动即整体旁路，**测试全绿** | 3.2 矩阵 ④⑤⑥；`grep enforce_markets tests/` 无生产配置断言 | 一次配置误改即静默失去规则层 |
| **N-7** | **P2** | 测试与生产配置脱耦：25/38 新用例用手写 `_ruleset()`；无任何用例断言 `config.yaml` 的 us 段 | 6.4/6.5 | 配置漂移（如 N-5）无法被回归发现 |
| **N-8** | **P3** | 板块/市场由**符号前缀**决定：`hkAAPL` 形式的美股标的不受美股规则约束（走 hk 板块 + `warn` 放行） | 3.9/3.10 实测 `applied=False`；`market_of("hkAAPL")="hk"` | 需经纪侧拒绝兜底；`AAPL`（无前缀）已正确 `RULE_UNKNOWN` 拒绝 |
| **N-9** | **P3** | `TZPATH=()`，`tzdata` 是唯一 tzdb 来源；卸载即全市场时段判定失效（fail-closed，但会**全面停摆**） | 2.3 实测 `TZPATH: ()` | 部署/升级时需纳入必装清单（已在 `requirements.txt`），但无运行时自检告警 |
| **N-10** | **P3** | 跨午夜夜盘（周五 21:00 → 周六 02:00）归属上一交易日未建模，被判 `weekend` | 2.6 | 安全影响：无（`extended_tradable=false`，夜盘不开放新仓） |

### 8.5 需要人工决策

| # | 决策项 | 选项 | 复核建议 |
|---|---|---|---|
| **D-1** | `unenforced_market_policy` 是否切 `reject` | ① 保持 `warn`；② 切 `reject` 并改造 28 个 hk 用例 | **切 `reject`**。实测对美股零影响；28 个失败是测试改造成本而非安全成本。配合 N-6 的启动断言一起做 |
| **D-2** | `halt_storage` 异常时是否允许 reduce-only | ① 维持全拦；② 拆分读写语义（B-1）；③ B-1+B-2 硬约束；④ 券商侧保护单（B-3） | **B-1 优先**（写失败允许 reduce-only、读失败仍全拦）；B-3 是终局但被 US-B6 阻塞 |
| **D-3** | N-1/N-2/N-4 三条"退出不可达"的优先级 | ① 作为 US-P1 首批；② 降级为 P2 随 US-P1 排期 | **作为 US-P1 首批阻断项**，在实盘前必须清零 |
| **D-4** | 是否接受"数据库故障期间无风险处置"作为可接受残余风险 | ① 接受（人工值守+心跳监控）；② 不接受，需在本进程外加看门狗 | 若选择①，必须补**外部告警**（heartbeat 文件被监控）并写入运行手册 |
| **D-5** | `config.template.yaml` 同步责任 | ① 本轮补；② 在 US-P1 补 | 属配置维护，建议与 D-1 一起做；在此之前**禁止用模板初始化新环境** |

---

## 9. 五个结论问题

### 问题 1：US-P0 是否可以验收？

**结论：代码实现层面可验收（4 项 P0 目标均已达成且有直接证据），
但整体验收应判定为「有条件通过」。**

* 可验收部分：US-P0-1 完全通过（6048 点零偏差）；US-P0-2 逻辑层完全通过；
  US-P0-3 完全通过；US-P0-4 的 6/7 项通过。
* 条件：US-P0-4 的"数据库异常下仍能处置风险"**未通过**，且存在 4 条
  "止损不可达"路径（N-1~N-4）。这些必须在进入实盘前清零，但不构成"US-P0 实现无效"。

### 问题 2：是否存在绕过规则的开仓路径？

**代码路径：不存在。** 唯一 `place_order` 在 `_execute()`，
`_execute` 之前必经 `risk.evaluate()` → `rules.check_order()`；
CLI / Web / worker 四入口全部收敛到 `engine.scan/monitor`；Web 无直接下单端点。

**配置路径：存在 3 个一键旁路**（N-6）：移除 `us` from `enforce_markets`、
`market_rules.enabled=false`、`market_rules.enforce=false` —— 任一发生即规则层整体旁路，
且当前**无启动断言、无测试**会阻止或报警。另外模板配置（N-5）本身就是这种状态。

### 问题 3：是否存在看不见真实持仓的路径？

**单次查询层面：不存在。** 三个时段开关默认全 `True` 并真实进入 SDK payload；
任何解析失败/网络异常都抛错而非返回空；`scan` 整轮停手、`monitor` 用快照兜底并标记。

**但存在 3 处"可能看不见"的边界**：

1. **未验证**（U-1/U-2）：网关是否真的按 `useUsNight` 返回夜盘仓位、时段字段名是什么
   —— 只能等真实凭证验证。
2. **降级期**：持仓查询失败时 `monitor` 用"最近快照 + 本地止损登记"评估止损。
   若快照之后又发生成交（而查询一直失败），则**评估用的数量/价格是旧的**，
   会把"已变化的仓位"当成旧仓位处理。已留痕（`positions_unavailable` + `cached_at`），
   但确实存在"看见的不是最新"的窗口。
3. **符号前缀**（N-8）：以 `hkAAPL` 等错误前缀表达的标的会走错市场规则；
   其持仓若被记错前缀，规则层将按 hk 处理。

### 问题 4：是否存在无法执行风险退出的路径？

**存在 4 条，全部实测复现：**

| # | 路径 | 触发条件 | 实测 |
|---|---|---|---|
| 1 | 数据库局部故障 | `journal.log/find_orders/list_orders` 抛错 | `monitor()` 抛 `RuntimeError`，`place=0` |
| 2 | 未知单未对清 | 该标的存在 `status=unknown` 委托 | `kinds=['exit_blocked']`，`place` 保持 1 |
| 3 | 急停存储异常 | `halt_store_failed=True` | `kinds=['exit_blocked']`，`place=0` |
| 4 | 未来时间戳订单 | 同向记录 `ts` 在未来 | `duplicate_order` 拒绝，减仓不可发 |

其中 1、4 属"设计遗漏"（应修），2、3 属"刻意 fail-closed 的代价"（需决策是否开 reduce-only 口子）。

### 问题 5：是否允许进入 US-P1？

**允许进入 US-P1，但不允许在 US-P1 完成前开实盘。**

前置条件（建议作为 US-P1 的 P0 输入）：

1. **N-1**：`monitor()` 对 `journal.*` 异常降级（至少保证止损段不因日志失败而中断），
   并补外部告警通道。
2. **N-2**：`unresolved_order_state` 区分方向 —— 未知**买**单不应无条件阻断**卖**出（或提供
   显式 reduce-only 豁免）。
3. **N-3**：按 D-2 决策落地 `halt_storage` 的 reduce-only 方案。
4. **N-4**：`_duplicate_check` 增加上界判定（`ts <= now + 容差`）。
5. **N-5 / N-6 / N-7**：同步模板配置 + 增加启动断言（`us ∈ enforce_markets` 且
   `enabled/enforce=true`）+ 补生产配置回归用例。
6. **D-1**：切 `unenforced_market_policy: reject` 并改造 28 个 hk 用例。

---

## 10. 复核签署

| 项 | 结论 |
|---|---|
| 只读执行 | ✅ 未修改任何代码/配置/数据库/密钥/SDK；复核前后哈希清单一致（同 12 个变更，无新增） |
| 未连接真实账户 | ✅ `can_connect_broker=False`；所有券商交互均在进程内替身完成 |
| 未发送订单 | ✅ 全部 `place_order` 均为 stub；真实适配器在 `_require()` 即被门禁拒绝 |
| 测试现场重跑 | ✅ 346 passed / 1 skipped / 0 failed（未引用既有日志） |
| 独立 oracle | ✅ 6048 点零偏差 |
| 变异/故障注入 | ✅ 覆盖行情/持仓/数据库/时区/未知单/急停 6 类异常 |

**总判定：US-P0 实现「有条件验收」；进入 US-P1 允许；实盘开放不允许。**
