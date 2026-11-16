# REPORT.md —— 准备工程报告

**工程**：星财富单 AI 自动交易系统（准备工程）
**目录**：`C:\FosunAI-Trading`
**券商**：星财富（复星财富 Fosun Wealth）
**SDK**：`fosunwealth/openapi-python-sdk` @ `c4c8a51`
**报告时间**：2026-11-08 12:05 (+08:00)
**执行环境**：Windows 11 / Python 3.13.14 / `.venv`

---

## 0. 最终状态（先看这里）

| 判定项 | 结论 | 依据 |
|---|---|---|
| **工程是否准备完成** | ✅ **是**（可交付、可运行、可测试、可部署） | 145 个测试全绿；Web/CLI/worker/备份/迁移全部实跑通过 |
| **AI 是否真实运行** | ⚠️ **部分**：规则引擎真实运行；**LLM 未运行** | 默认 `provider=heuristic`（`is_llm=false`），已产出真实信号；`OpenAICompatibleModel` 因无 `AI_API_KEY` 标记为 `untested_no_api_key`，真实调用**未测试** |
| **券商 API 是否已连接** | ❌ **否** | 无 `base_url` / `api_key` / 两段 PEM。`check-env` → `configured=false`，`blocked_reason="PAPER_TRADING=true，真实券商连接被禁用"` |
| **是否具备真实交易资格** | ❌ **否** | `live_authorized=false`、`real_order_authorized=false`；三重开关全为安全态；且平台侧交易权限尚未申请 |
| **行情是否为真实行情** | ❌ **否** | `PaperBroker` 使用 `SyntheticQuoteSource`（确定性合成序列），`health().synthetic_prices=true` |

**一句话**：**链路已通、账本可查、风控可阻断、部署可复现；但没有真钱、没有真行情、没有真券商连接。**

---

## 1. 十项准备工作的完成情况

| # | 任务 | 状态 | 产出 | 证据 |
|---|---|---|---|---|
| 1 | 克隆并审计官方 SDK | ✅ | `sdk_src/`、[API_STATUS.md](API_STATUS.md) | commit `c4c8a51`，23 个 .py / 2,854 行；`ai-trader sdk-audit` 反射出 8 个模块全部方法签名 |
| 2 | 独立 Python 虚拟环境 | ✅ | `.venv`（Python 3.13.14） | `pip list` → `fsopenapi 0.1.1`、`ai-trader 0.1.0` 均为 editable |
| 3 | 整理账户/行情/持仓/下单/撤单接口 | ✅ | `broker/base.py`（统一数据结构）、`broker/fosun_adapter.py`（映射） | [API_STATUS.md §3](API_STATUS.md) 逐接口映射表 |
| 4 | 统一 AI 模型接口 | ✅ | `ai/base.py` + `ai/providers.py` | `HeuristicModel` / `OpenAICompatibleModel` 同一 `AIModel.analyze()` 契约；`build_model(force_provider=...)` 可切换 |
| 5 | 策略引擎 + 风控模块 | ✅ | `strategy/engine.py`、`risk/controller.py` | 24 项检查、fail-closed、`_execute()` 唯一出口 |
| 6 | 模拟交易系统 | ✅ | `broker/paper.py` | 限价/市价撮合、手续费、滑点、多币种、状态持久化、挂单复核 |
| 7 | 交易记录 + 审计日志 | ✅ | `audit/journal.py` | SQLite 5 张表 + JSONL 哈希链；`verify_chain()` 可定位篡改行号 |
| 8 | Web 管理界面 | ✅ | `web/app.py` + 2 个模板 | 实跑 `http://127.0.0.1:8781` 返回 200，页面含状态/持仓/委托/信号/风控/审计 |
| 9 | Windows VPS 部署与迁移脚本 | ✅ | `scripts/*.ps1`（6 个） | 语法校验 6/6 通过；bootstrap 实跑 exit=0；backup / migrate_export / worker / healthcheck 均实跑 |
| 10 | 配置模板 + 密钥安全管理 | ✅ | `config/config.yaml`、`config.template.yaml`、`.env.example`、`secrets.py` | DPAPI 保险箱实测；`vault.json` 无明文；脱敏函数被测覆盖 |

---

## 2. 交付物清单

### 文档
- `README.md` —— 架构、快速开始、安全模型
- `REPORT.md` —— 本文件
- `DEPLOYMENT.md` —— VPS 部署、计划任务、备份、迁移、8 类故障排查
- `RISK_CONTROL.md` —— 风控规格书（24 项检查、阈值、fail-closed 论证、未覆盖风险）
- `API_STATUS.md` —— SDK 审计、接口状态、3 个审计发现、待申请清单
- `.env.example` —— 环境变量模板（全部留空，**无任何假凭证**）

### 代码
```
src/ai_trader/   11 个模块（config/secrets/runtime/cli + broker/ai/strategy/risk/audit/web）
tests/           9 个测试文件，145 个测试
scripts/         6 个 PowerShell 脚本
config/          主配置 + 基线模板
```

### 证据（`logs/evidence/`）
| 文件 | 内容 |
|---|---|
| `junit.xml` | 145 测试的逐条结果 |
| `sdk_inventory.json` | `ai-trader sdk-audit` 的完整反射输出 |
| `check_env.json` | `ai-trader check-env` 的门禁/凭证快照 |
| `sdk_commit.txt` | SDK HEAD commit 与时间 |
| `pip_list.txt` | 实际安装的包与版本 |
| `python_version.txt` | 解释器版本 |

---

## 3. 测试证据（真实命令输出）

### 3.1 全量测试

```
$ .venv\Scripts\python.exe -m pytest -q
................................................................s....... [ 49%]
........................................................................ [ 99%]
.                                                                        [100%]

junit 统计：tests=145  failures=0  errors=0  skipped=1  time=4.731s
```

唯一跳过项：`tests/test_ai.py::test_llm_real_call_if_key_present` → `pytest.skip("未配置 AI_API_KEY —— LLM 真实调用【未测试】")`。
**这是如实标记，不是通过。**

### 3.2 SDK 安装测试

```
$ .venv\Scripts\python.exe -c "import fsopenapi; print(fsopenapi.__file__)"
C:\FosunAI-Trading\sdk_src\fsopenapi\__init__.py

$ .venv\Scripts\python.exe -c "import inspect,fsopenapi; print([n for n in fsopenapi.__all__][:8])"
['SDKClient', 'DEFAULT_LANG', 'LANG_EN', 'LANG_ZH_CN', 'LANG_ZH_TC', 'APIError', 'AuthenticationError', 'PermissionError']

$ .venv\Scripts\python.exe -c "from fsopenapi import SDKClient; SDKClient('https://x','k')"
ValueError: 缺少必要的环境变量: FSOPENAPI_SERVER_PUBLIC_KEY, FSOPENAPI_CLIENT_PRIVATE_KEY。
            请在启动前设置 FSOPENAPI_SERVER_PUBLIC_KEY 和 FSOPENAPI_CLIENT_PRIVATE_KEY
```

第三条证明：**SDK 在缺密钥时直接抛错，不会假装连上**。测试 `test_sdk_client_requires_pem_env` 断言了这一点。

### 3.3 环境与门禁

```
$ .venv\Scripts\python.exe -m ai_trader.cli check-env
{
  "python": "3.13.14",
  "dpapi_available": true,
  "safety": {
    "live_trading": false,
    "order_execution_enabled": false,
    "paper_trading": true,
    "kill_switch": false,
    "live_ack_satisfied": false,
    "live_authorized": false,
    "real_order_authorized": false
  },
  "vault": { "dpapi": true, "exists": false, "keys": [], "key_count": 0 },
  "fosun": {
    "gate_allows_connect": false,
    "gate_reason": "PAPER_TRADING=true，真实券商连接被禁用",
    "configured": false,
    "connected": false,
    "base_url_set": false,
    "api_key_present": false,
    "server_public_key": "",
    "client_private_key": "",
    "can_send_real_order": false
  },
  "sdk_importable": { "ok": true, "path": "C:\\FosunAI-Trading\\sdk_src\\fsopenapi\\__init__.py" }
}
```

### 3.4 AI 模型调用测试

| 场景 | 结果 |
|---|---|
| 规则引擎（`heuristic`）| ✅ 真实运行，产出确定性信号 |
| 上升趋势 + 高位横盘 | → `buy`，`stop_loss=200.88 < entry=205.40 < take_profit=212.18`，`position_pct=0.1425`，`validate()` 无错误 |
| 单边直线上涨（RSI=100）| → `hold`（理由含"超买"），**主动放弃追高** |
| K 线不足 30 根 | → `hold`（"K 线数量不足"）|
| OpenAI 兼容 LLM（无 Key）| → `available=false`，`last_status="untested_no_api_key"`，调用抛 `AIModelError(code="NO_API_KEY")` |
| OpenAI 兼容 LLM（真实调用）| ⏭ **未测试**（无 `AI_API_KEY`）|
| JSON 解析容错 | ✅ 三种输入（纯 JSON / ```json 围栏 / 前后夹中文）均能解析；纯中文描述正确报错 |

### 3.5 行情数据结构测试

```
$ # tests/test_market_data.py（21 个用例）
quote.as_dict() 键：symbol market last open high low prev_close volume source
kline 校验：60 根，low <= open,close <= high，时间戳单调递增
source == "synthetic"      ← 诚实标注非真实行情
确定性：同 symbol 两次调用结果完全一致；不同 symbol 不同
标的映射：hk00700→(hk,HKD)  usAAPL→(us,USD)  sh600519/sz000001→(cn,CNY)
指标：SMA/EMA/RSI/ATR/BOLL/动量/最大回撤 全部有边界与自洽断言
```

### 3.6 模拟买卖测试

`ai-trader demo` 的真实输出（沙盒内，`enforce_market_hours=false`）：

```
scan  : {'ordered': 1} | 放行 buy hk00700 × 100 → filled
acct  : cash=970178.17  mv=29783.10  equity=999961.27
stop  : [('止损触发：最新 297.831 <= 止损 303.78762', 'filled')]
dup   : False ['hk00700 在 300s 内已有 buy 委托（12:04:16），判定为重复下单']
kill  : KILL_SWITCH 已触发：演示用紧急停止 | orders: 0
audit : signals=1 risk_decisions=2 orders=2 audit_log=7
        chain={'ok': True, 'broken_at_seq': None, 'total': 7}
```

账务自洽验证：初始 1,000,000 − 买入 29,821.83（含 3.00 手续费与滑点）= 970,178.17 ✓
权益 970,178.17 + 29,783.10 = 999,961.27，差额 −38.73 即摩擦成本（手续费 + 滑点）✓

### 3.7 止损止盈测试

| 用例 | 结果 |
|---|---|
| 止损线在现价之上 | → 触发 `sell`，`is_exit=True`，`reason="止损触发..."`，持仓清零，止损注册表清除 |
| 止盈线在现价之下 | → 触发 `sell`，`reason="止盈触发..."` |
| 价格在止损/止盈之间 | → 不触发（`[]`）|
| 买入信号无止损 | → 风控拒绝（`per_trade_risk`），不下单 |
| 止损方向错误（买止损 > 入场）| → 拒绝（`stop_side`）|
| 止损距离 16.7% > 12% | → 拒绝（`max_stop_distance`）|
| 止损注册表持久化 | → 写 `data/stops.json`，重启后仍保护持仓 |

### 3.8 仓位限制测试

| 用例 | 结果 |
|---|---|
| 权益 100k，买 150k（占比 150%）| → 拒绝 `max_position_pct`（上限 10%）|
| 已持 2 个标的，上限 2 | → 拒绝 `max_positions` |
| 已有 15% 敞口，再买至 25%（上限 20%）| → 拒绝 `max_gross_exposure` |
| 定仓：权益 1M / 价 300 / 止损 290 | → `binding=pct_limited`，500 股 |
| 定仓：权益 1M / 价 300 / 止损 250 | → `binding=risk_limited`，200 股 |
| 定仓：默认单笔上限 50k / 价 300 | → `binding=max_notional`，100 股（整手向下取整）|
| 置信度 1.0 vs 0.5 | → 后者股数更少（按置信度收缩）|

### 3.9 紧急停止测试

```
kill  : KILL_SWITCH 已触发：演示用紧急停止 | orders: 0
```

| 用例 | 结果 |
|---|---|
| 触发后扫描 | → `blocked_reason` 非空，`outcomes=[]`，无任何委托 |
| 触发后单笔意图 | → 拒绝 `kill_switch` |
| `resume()` 后 | → 恢复正常，意图可放行 |
| 环境变量强开 live 但无确认短语 | → 仍不可真实下单（`test_env_override_cannot_silently_enable_live`）|

### 3.10 VPS 部署测试

```
$ powershell -ExecutionPolicy Bypass -File scripts\bootstrap_windows.ps1
[ OK ] 复用 .venv 中的 Python 3.13.14
[ OK ] Git: git version 2.55.0.windows.3
[ OK ] DPAPI 可用（230 字节密文）
[ OK ] 复用已存在的 .venv
[ OK ] config\config.yaml 已存在，保持不变
EXITCODE=0

$ powershell -ExecutionPolicy Bypass -File scripts\healthcheck.ps1
健康检查 http://127.0.0.1:8781/api/status
  [ OK ] http: 已连通
  [ OK ] audit_chain: total=18
  [ OK ] paper_mode: paper_trading=True
  [ OK ] no_real_order_in_paper: real_order_authorized=False
  [ OK ] broker_kind: kind=paper connected=True
健康检查通过。
EXITCODE=0

$ powershell -ExecutionPolicy Bypass -File scripts\run_worker.ps1
[2026-11-08 12:03:33] === worker start (symbols='') ===
[2026-11-08 12:03:33] monitor exit=0
[2026-11-08 12:03:34] scan exit=0
[2026-11-08 12:03:34] === worker ok ===
EXITCODE=0

$ powershell -ExecutionPolicy Bypass -File scripts\backup.ps1
-> data\backups\backup-20261108-120309.zip (19.3 KB)
EXITCODE=0

$ powershell -ExecutionPolicy Bypass -File scripts\migrate_export.ps1 -Passphrase "test-passphrase-123"
-> secrets.enc (0.1 KB)
-> bundle.zip
-> MIGRATE.txt
EXITCODE=0
```

迁移包可解性验证（Python 侧）：

```
header: b'FSTV1'
decrypted keys: version=1, secrets=[]
wrong pass rejected: InvalidTag
```

`secrets=[]` 是因为保险箱当前为空（**没有编造任何凭证**），机制本身已验证。

PowerShell 语法校验（PSParser，6/6）：

```
OK   backup.ps1  tokens=488
OK   bootstrap_windows.ps1  tokens=747
OK   healthcheck.ps1  tokens=278
OK   migrate_export.ps1  tokens=384
OK   run_web.ps1  tokens=174
OK   run_worker.ps1  tokens=282
```

Web 服务实跑：

```
$ curl --noproxy '*' -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8781/  → 200
$ curl --noproxy '*' -s -X POST -F "symbols=hk00700" .../api/scan
{"summary":{"rejected":1},"outcomes":[{"stage":"rejected",
 "detail":"拒绝：不在交易时段：hk 周末休市", ...}]}     ← 周末自动拦，正确
```

---

## 4. 测试与需求的对应表

| 需求中的测试项 | 对应测试文件 | 用例数 | 结果 |
|---|---|---|---|
| SDK 安装测试 | `test_sdk_install.py` | 7 | ✅ |
| AI 模型调用测试（无密钥则标记未测试）| `test_ai.py` | 19（1 skip）| ✅ / LLM 真实调用 ⏭ 未测试 |
| 行情数据结构测试 | `test_market_data.py` | 21 | ✅ |
| 模拟买卖测试 | `test_paper_trading.py` | 13 | ✅ |
| 止损止盈测试 | `test_engine_flow.py`、`test_risk_controls.py` | 9 | ✅ |
| 仓位限制测试 | `test_risk_controls.py` | 12 | ✅ |
| 紧急停止测试 | `test_risk_controls.py`、`test_engine_flow.py`、`test_web.py` | 6 | ✅ |
| VPS 部署测试 | 脚本实跑 + `PSParser` 校验（见 §3.10）| — | ✅ |
| （附加）审计链防篡改 | `test_audit.py` | 9 | ✅ |
| （附加）密钥与门禁 | `test_secrets_and_gate.py` | 24 | ✅ |
| （附加）Web 界面 | `test_web.py` | 11 | ✅ |
| （附加）CLI | `test_cli.py` | 6 | ✅ |

---

## 5. 关键设计决策与理由

| 决策 | 理由 | 代价 |
|---|---|---|
| AI 只产出 `TradingSignal`，无执行权 | 「AI 不得绕过风控」是硬要求，靠"接口上没有下单方法"来保证，而不是靠约定 | 需要在策略引擎里多一层转换 |
| 风控 fail-closed | 交易系统的默认安全态必须是"不动" | 偶发异常会误拒，需要用 `checks` 排查 |
| `PAPER_TRADING` 与 `LIVE_TRADING` 互斥并强制回落模拟盘 | 防止 `live=true` 忘记关 `paper` 导致"以为在模拟其实在下真单" | 切换真实盘必须显式关 paper |
| 默认行情为**合成**而非接入真实免费行情 | 合成数据可复现（同 symbol+日期 → 同序列），适合回归测试；真实行情需凭证，接入会造成"看起来能用其实不可靠" | 收益数字无参考价值，需刻意标注 |
| 默认 AI 为规则引擎而非 LLM | 无密钥时如果静默失败，整套系统就"看起来在工作"。规则引擎可审计、可复现，是诚实的基线 | 信号质量弱于 LLM |
| 止损注册表落盘 | 进程重启/计划任务重启后仍保护持仓 | 多实例会争抢同一文件（已用 `MultipleInstances IgnoreNew` 规避）|
| 审计用哈希链 | 让"事后改账"可被发现 | 写入多一次 SHA256，可忽略 |
| SDK 保持只读（不 patch） | 审计基线必须干净；上游问题记录在 `API_STATUS.md` 而非私自修改 | 行情加密问题需上线前解决（见 API_STATUS §2.2①）|

---

## 6. 已实测 vs 未验证（诚实边界）

### 已实测（有命令输出）

- SDK 可导入、类/方法/签名与文档一致、缺 PEM 时正确报错
- 虚拟环境隔离、依赖版本、editable 安装
- 规则引擎在 5 种行情形态下的决策（买/卖/观望/跳过/拒绝）
- 模拟撮合：市价/限价、手续费下限、滑点、多币种、状态持久化、撤单、挂单复核
- 风控 24 项检查的**每一条**拒绝路径（含边界值）
- 止损/止盈触发与持仓清零
- 紧急停止的阻断范围与恢复
- 审计哈希链生成与篡改检测（能定位到具体行号）
- DPAPI 加密落盘（`vault.json` 无明文）、口令包导出/导入/错口令拒绝
- Web 11 个接口、鉴权、无后门接口
- 6 个 PowerShell 脚本语法 + 其中 5 个实跑

### 未验证（需凭证或需更多工程）

| 项 | 阻塞原因 | 影响 |
|---|---|---|
| 全部真实券商接口 | 无凭证 | 无法确认响应字段名、错误码、限流 |
| 星财富行情加密行为 | 无凭证 | `API_STATUS §2.2①` 的 SDK 疑点未闭环 |
| A股通代码格式 | 无凭证 + 文档未注明 | 推测 `sh/sz` 前缀，未确认 |
| LLM 真实调用 | 无 `AI_API_KEY` | 模型输出稳定性、成本、延迟未知 |
| 实时行情推送 | 未实现（`market_push` 34 个 subscribe 方法未接） | 止损仅靠 300s 轮询，实时性不足 |
| 官方模拟账户（`SimAccountCreate`）| 未接入 | 只能用本地撮合替代 |
| 港/美股假期与半日市 | 未实现交易日历 | 时段判断为近似 |
| FX 折算 | 未实现 | 多币种权益为"名义加总"，非统一币种 |
| T+1 / 涨跌停（A股通）| 未建模 | A股通直接上真实盘有违规/拒单风险 |

---

## 7. 仍需申请或配置的环节（按优先级）

### P0 —— 没有这些就无法进入下一步

1. **星财富开放平台账号与网关 `base_url`**
2. **API Key**
3. **服务端公钥 PEM**
4. **客户端私钥 PEM**
   > 以上 4 项必须由平台签发。本工程**没有、也不会**自行生成或填写任何假凭证。

### P1 —— 拿到凭证后 24h 内应完成

5. `config.yaml` 填 `sub_account_id` / `apply_account_id`（OPS 场景）
6. 确认是否 OPS 配对（`SDK_TYPE=ops`）
7. 用 `PAPER_TRADING=false` + `ORDER_EXECUTION_ENABLED=false` 做一次**只读连通性验证**（能连、能读、不能下单）
8. 核对真实响应字段名，修正 `fosun_adapter.py` 的候选字段表
9. 闭环 `API_STATUS §2.2①` 的行情加密疑点

### P2 —— 进入灰度前

10. 接入 `trade.get_bid_ask_info()` 下单前校验
11. 接入 `market_push` WebSocket 实时行情
12. 评估改用券商服务端条件单（`stopLossPrice`/`profitPrice`）托管止损
13. 申请并开通交易权限（港股/美股/A股通，是否含融资/做空）
14. 确认限流配额，据此调整 `max_orders_per_minute`

### P3 —— 真实资金上线前

15. AI 密钥配置并做 ≥ 100 次调用的一致性/成本评估
16. A股通 T+1 与涨跌停建模
17. FX 折算与统一报告币种
18. 交易日历（含半日市）
19. 至少 5 个交易日模拟盘 + 1 个交易日"只读真实"对照
20. 小资金灰度（建议 ≤ 总资产 5%），并保留人工一键平仓能力

---

## 8. 遗留问题与建议

### 8.1 SDK 侧（需上游确认）

- 行情 POST 被加密（`API_STATUS §2.2①`）——**建议向星财富反馈**；本工程不 patch 上游
- `models.py` 为空，全部响应为裸 `dict`，字段名无契约 → 适配器只能用候选字段名猜测，这是**结构性脆弱点**
- `create_order` 的 `orderType` 取值（1=限价，3=市价？）文档未明确 → 适配器按 README 示例取 1/3，**待验证**

### 8.2 工程侧

- **单进程假设**：`PaperBroker` 用 JSON 文件持久化，多进程并发写会互相覆盖。生产环境应换 SQLite 或加文件锁
- **止损依赖轮询**：计划任务 1 分钟粒度 + 交易时段判断，非交易时段止损延迟可能到下一个开盘
- **Web 无 CSRF token**：当前依赖 `SameSite=Lax` Cookie。若对外暴露，建议加 CSRF 或改用 Bearer token
- **`/api/docs` 未鉴权**：OpenAPI 文档在生产应关闭（`docs_url=None`）
- **日志无轮转**：`logs/audit.jsonl` 会持续增长，建议接 `logging.handlers.RotatingFileHandler` 或按日切分

### 8.3 建议的下一步（最小可行动作）

```
1. 向星财富提交开放平台申请（4 项凭证 + 交易权限）
2. 同时：把 market_push 接进来（改动集中在 broker/fosun_adapter.py）
3. 同时：补 A股通 T+1 与涨跌停（改动集中在 risk/controller.py）
4. 拿到凭证后：只读验证 → 官方模拟账户灰度 → 小资金真实盘
```

---

## 9. 结论

这套准备工程的价值不在"能赚钱"，而在于：

- **把不可绕过的约束变成了代码结构**（AI 没有下单方法、风控是唯一出口、fail-closed）
- **把"到底连没连上"变成了可查询的事实**（`check-env` 的 `configured/blocked_reason`，不是靠感觉）
- **把"出过什么事"变成了可验证的证据**（哈希链审计，能定位到第几条被改）
- **把"能不能搬到别的机器"变成了一个脚本**（迁移包实测可解、错口令正确拒绝）

它现在**可以安全地跑模拟盘**，并且**在拿到凭证后有一条清晰的、每一步都能自证的升级路径**。

它**不能**直接下真单，这是当前设计的正确状态，而不是缺陷。
