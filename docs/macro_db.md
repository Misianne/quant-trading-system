# 宏观数据库结构与点时间接口

文件：`data/macro_data.db`（SQLite，`PRAGMA user_version=1`）
代码：`src/ai_trader/macro/`（`schema.py` / `store.py` / `ingest.py` / `snapshot.py`）
入库工具：`tools/macro_ingest.py`

> 本库与 `market_us.db` / `audit.db` / `us_loop.db` **没有任何外键或 ATTACH 关系**，
> 也不被任何交易链路导入。删除它不会影响行情与交易。

---

## 1. 五张表

### `macro_series` — 序列元数据

| 字段 | 类型 | 说明 |
|---|---|---|
| `series_id` | TEXT PK | FRED 序列 ID |
| `title` / `units` / `frequency` / `seasonal_adjustment` | TEXT | 源站元数据原文 |
| `source` | TEXT | `FRED` |
| `metadata_updated_at` | TEXT | **本地**写入元数据的时间（UTC） |
| `observation_start` / `observation_end` | TEXT | 源站声明的最早/最晚观测期 |
| `last_updated` | TEXT | 源站给出的最近更新时间 |
| `timezone` | TEXT | 发布时区，默认 `America/New_York` |
| `release_time_hint` | TEXT | 常规发布时刻（如 `08:30`）。**仅提示，不参与计算**，`verified=0` |

### `macro_observations` — 每个版本的观测值（核心表）

| 字段 | 类型 | 说明 |
|---|---|---|
| `series_id` | TEXT | |
| `observation_period` | TEXT | **数据所属期间**（如 CPI 的 `2026-09-01` = 2026 年 9 月） |
| `vintage_date` | TEXT | **该版本在 FRED 生效的日期** |
| `realtime_start` / `realtime_end` | TEXT | FRED 的版本有效期（`realtime_end` 由下一版本推出） |
| `value` | REAL | 该版本的取值 |
| `is_initial_release` | INTEGER | 1 = **本库能取到的最早版本**（见 §4 限制） |
| `source` | TEXT | `FRED` |
| `ingested_at` | TEXT | **本地采集时间**（与发布时间无关，用于审计） |
| `release_date` | TEXT | 真实公开发布日（= `vintage_date`） |
| `release_timestamp` | TEXT | 可得时点（带偏移，ISO8601） |
| `release_epoch` | INTEGER | 同一时刻的 **UTC 秒**，**比较只用这个** |
| `time_precision` | TEXT | `exact` / `date` / `inferred` / `unknown` |
| `verified` | INTEGER | 1 = 已用官方发布日历核验（本轮全为 0） |

主键 `(series_id, observation_period, vintage_date)` —— 同一期间多版本**并存，永不覆盖**。

#### `time_precision` 的四个取值必须分清

| 值 | 含义 | 本轮哪些序列 |
|---|---|---|
| `exact` | 精确到时分秒，且经官方日历核验 | **无**（未核验，不冒充） |
| `date` | 有 FRED vintage 日作依据，只到日粒度 | 8 条统计序列（CPI/PCE/就业/GDP…） |
| `inferred` | **无** vintage 依据，按收盘规则推定 | DFF / DGS2 / DGS10 / VIXCLS |
| `unknown` | 完全无法确定 → `release_epoch IS NULL`，任何时点都不可见 | 无 |

`inferred` 的产生原因：这 4 条日频市场序列的 vintage 矩阵在 FRED 侧不可承受——
实测 `DFF` 在 **50 列**的窗口上就稳定 504（网关 60 秒超时），`DGS2` / `DGS10` 同理；
而 `VIXCLS` 的 vintage 模式只覆盖约 4,000 / 9,200 个交易日（其它日期没有版本记录）。
它们又都是**零修订**序列（`fred_live_validation.md`：498~729 个比对点全无修订），
因此改取"全历史 + 推定发布时序"，并把口径如实标成 `inferred`，快照会单独告警。

### `macro_releases` — 发布事件

| 字段 | 说明 |
|---|---|
| `series_id` / `observation_period` / `release_date` | 主键 |
| `release_timestamp` / `release_epoch` | 同上 |
| `timezone` | `America/New_York` |
| `release_type` | `initial` / `revision` |
| `time_precision` / `verified` | 精度与核验状态 |
| `notes` | 本轮固定写入"时刻未核验，按当日 23:59:59 保守处理" |

### `macro_raw_responses` — 原始响应归档

| 字段 | 说明 |
|---|---|
| `request_id` | `YYYYMMDDTHHMMSS-<hash 前 10 位>` |
| `endpoint` | `/series`、`/series/observations`、`/series/vintagedates` |
| `request_parameters` | JSON，**已剔除 `api_key`**，写入后再复查一次 |
| `response_hash` | 响应体的 SHA256 |
| `response_path` | 落盘路径 `data/macro_raw/<task_id>/<request_id>.json` |
| `fetched_at` / `http_status` / `bytes` / `row_count` | 审计字段 |

### `macro_ingest_log` — 入库任务日志

`(task_id, series_id)` 为主键，记录 `started_at` / `finished_at` / `rows_inserted` /
`rows_updated` / `status`(`running|ok|failed|skipped`) / `error_message`。
中断后重跑同一 `task_id` 会把该行重置为 `running`，不会产生重复行。

---

## 2. 六个"时间"必须分开，不能混用

| 概念 | 字段 | 例子 |
|---|---|---|
| 数据**所属期间** | `observation_period` | 2026 年 9 月的 CPI → `2026-09-01` |
| FRED **vintage 日** | `vintage_date` | `2026-10-15` |
| FRED **实时窗口** | `realtime_start` / `realtime_end` | `2026-10-15` ~ `2026-11-13` |
| **真实发布日** | `release_date` | `2026-10-15` |
| **可得时点** | `release_timestamp` + `release_epoch` | `2026-10-15T23:59:59-04:00` |
| **本地采集时刻** | `ingested_at` | `2026-11-19T07:57:20+00:00` |

把 `realtime_start` 直接当成"发布时间"是常见错误：它是**版本生效日**，
而 FRED 并没有给出当天几点几分发布。因此本库：

```
release_date      = vintage_date                    （日粒度，有据）
release_timestamp = 当日 23:59:59 America/New_York  （保守，宁可晚不可早）
time_precision    = 'date'
verified          = 0                                （未用官方日历核验）
release_epoch     = int(release_timestamp)
```

**为什么取 23:59:59 而不是 08:30**：CPI 实际在 08:30 ET 发布，但我们没有把
"08:30"落到每一行上的可靠依据。取当日末刻意味着**最多延迟约 15 小时才可见**，
方向是"晚知道"，不会"提前知道"——防泄漏要求的是后者不能发生。
等接入官方发布日历时，把该行改成 `time_precision='exact'`、`verified=1`、
`release_timestamp` 为精确时刻即可，**表结构无需变动**。

对 `inferred` 的四条日频序列，规则写死在 `ingest.build_market_rows`：

* `DGS2` / `DGS10` / `VIXCLS`：观测日 D 的收盘值，按 **D 日 23:59:59** 可见
  （财政部当日下午已公布，CBOE 收盘后公布）；
* `DFF`：有效联邦基金利率由纽约联储在**次一工作日**发布，故推后 1 个工作日
  （只跳周末、不建模节假日，方向仍是"更晚"）。

跨时区比较一律走 `release_epoch`（UTC 秒）。字符串比较在夏令时切换时会错：
`2026-10-30T23:59:59-04:00` 与 `2026-11-02T23:59:59-05:00` 的字典序恰好正确，
但只要偏移不同、日期相近，就可能出现"字面更早、实际更晚"。

---

## 3. 查询接口

```python
from ai_trader.macro import get_macro_snapshot

snap = get_macro_snapshot("data/macro_data.db",
                          "2026-10-16T00:00:00-04:00",
                          series=["CPIAUCSL", "GDP"])
snap.value("CPIAUCSL")        # 该时点已公开的最新值；未发布 → None
snap.latest("GDP")            # SnapshotPoint(value, vintage_date, release_timestamp, ...)
snap.missing_series           # 该时点完全没有可见数据的序列
snap.precision_warnings       # 时间精度不足的提示（不伪装成精确时刻）
snap.as_dict()                # 可 JSON 序列化，便于存档
```

规则与实现的对应：

| 要求 | 实现 |
|---|---|
| 只返回指定时点**之前已公开**的数据 | `WHERE release_epoch <= :as_of_epoch` |
| 同期多版本只取**该时点已公开的最新版** | `ROW_NUMBER() OVER (PARTITION BY series,period ORDER BY release_epoch DESC, vintage_date DESC)` 取 `rn=1` |
| 尚未发布 → **缺失**，不提前显示 | 不满足条件的行**不出现在结果里**（不是返回当前值、也不是返回 NULL 行） |
| 无法确认发布时间的**标记精度不足** | `time_precision != 'exact'` → `precision_warnings`；`release_epoch IS NULL` → 任何时点都不可见 |
| 支持**历史时点回放** | 纯函数，同一 `as_of` 重复查询结果完全相同 |
| **严禁**用当前修订值覆盖历史初值 | 表无 UPDATE 历史值的路径；主键含 `vintage_date`，每个版本独立成行 |

`history(store, series_id, observation_period)` 返回某条观测的完整 vintage 链，用于审计。

---

## 4. 入库现状（2026-11-19）

| 序列 | 含义 | 版本行数 | 覆盖区间 | 精度 |
|---|---|---:|---|---|
| `PAYEMS` | 非农就业 | 13,642 | 1939-01 … 2026-10 | date |
| `CPIAUCSL` | CPI | 3,058 | 1947-01 … 2026-10 | date |
| `CPILFESL` | 核心 CPI | 1,782 | 1987-01 … 2026-10 | date |
| `PCEPI` | PCE | 7,669 | 1959-01 … 2026-09 | date |
| `PCEPILFE` | 核心 PCE | 7,709 | 1959-01 … 2026-09 | date |
| `UNRATE` | 失业率 | 2,053 | 1948-01 … 2026-10 | date |
| `CES0500000003` | 平均时薪 | 1,485 | 2006-03 … 2026-10 | date |
| `GDP` | GDP | 2,883 | 1946-01 … 2026-07 | date |
| `DFF` | 联邦基金有效利率 | 26,438 | 1954-07 … 2026-11 | inferred |
| `DGS2` | 2 年期国债 | 12,612 | 1976-06 … 2026-11 | inferred |
| `DGS10` | 10 年期国债 | 16,204 | 1962-01 … 2026-11 | inferred |
| `VIXCLS` | VIX | 9,319 | 1990-01 … 2026-11 | inferred |

合计 **104,854** 个版本行。前 8 条含真实修订（GDP 单个季度最多 15 个版本、
PCE 单个季度 17 个版本），后 4 条为零修订市场序列。

---

## 5. 已知限制（必须知道）

1. **`is_initial_release` 不等于"真实首次公布"**。它表示"本库能取到的最早版本"。
   FRED 的 vintage 历史并非从序列诞生开始（例如 `CPIAUCSL` 的 vintage 只回溯到
   1972-07-21，`CPILFESL` 到 1987），因此更早观测的"初值"其实是**当时已在流通的
   某个版本**。要拿到真实初值需 `output_type=4` + 官方日历，本轮未做。
2. **发布时刻未核验**（`verified=0`）。表结构已支持升级到 `exact`，但需要先接
   官方发布日历（BLS/BEA/Fed），否则就是编造时刻。
3. **`release_date` 用的是 FRED vintage 日**，个别序列可能与机构官方发布日相差
   1 个交易日（FRED 入库延迟）。当前方向仍是"晚"，不构成泄漏。
4. 库内数据止于最后一次采集（见 `macro_series.last_updated`）。**策略若要用，
   必须先过新鲜度闸门**，不能把旧值当最新。

---

## 6. 重建与回滚

```bash
# 全量重建（幂等，可反复运行）
python tools/macro_ingest.py --report data/macro_ingest_$(date +%Y%m%d).json

# 只评估体积、不写库
python tools/macro_ingest.py --dry-run --series CPIAUCSL

# 回滚：本库是独立文件，直接删除即可，不影响任何其它库
rm data/macro_data.db data/macro_data.db-wal data/macro_data.db-shm
```

迁移失败的回滚由 `schema.ensure_schema` 保证：所有 DDL 在**同一个显式事务**内
逐条执行，任一条失败即 `ROLLBACK`，不会留下半套表（`sqlite3.executescript`
做不到这点，它会先隐式 COMMIT）。
