# 宏观数据库建立报告（点时间 / point-in-time）

**执行日期**：2026-11-19
**基线提交**：`21ae834`（FRED 实测验证）
**数据库**：`data/macro_data.db`（独立库，56 MB）
**全量测试**：1632 项通过 / 0 失败 / 1 跳过（JUnit XML 核准）

---

## 一、执行前保护

| 项 | 结果 |
|---|---|
| 分支 / HEAD | `master` / `21ae834` |
| 工作区 | 干净（仅上一轮 WS 的 3 处未提交改动，本轮未触碰） |
| 备份 | `backups/us_macro_20261119-153407/`（3 个既有库 + SHA256 清单） |
| 既有库是否被改动 | **否**：`market_us.db` / `audit.db` / `us_loop.db` 逐一比对哈希，**0 不一致** |
| 是否重置/迁移既有库 | **否**，全程只读这三个库 |
| 新库位置 | `data/macro_data.db`（`data/*.db` 已在 `.gitignore`，与其它库一致） |

---

## 二、数据库结构

完整字段说明见 `docs/macro_db.md`。五张表：

| 表 | 行数 | 作用 |
|---|---:|---|
| `macro_series` | 12 | 序列元数据（单位、频率、季调、覆盖区间、时区、发布时刻提示） |
| `macro_observations` | 104,854 | **每个版本一行**，主键 `(series_id, observation_period, vintage_date)` |
| `macro_releases` | 104,854 | 发布事件（initial / revision，含精度与核验状态） |
| `macro_raw_responses` | 61 | 原始响应归档（参数已剔除 `api_key`） |
| `macro_ingest_log` | 15 | 入库任务日志（running / ok / failed） |

**多版本并存已实证**：4,909 个观测期有 ≥2 个版本，单期最多 **24 个版本**，
非初值版本共 **34,678** 行 —— 这些行如果被"覆盖式更新"，历史就永久丢失了。

---

## 三、12 条序列入库结果

| 序列 | 版本行数 | 覆盖区间 | 精度 |
|---|---:|---|---|
| PAYEMS | 13,642 | 1939-01 … 2026-10 | date |
| CPIAUCSL | 3,058 | 1947-01 … 2026-10 | date |
| CPILFESL | 1,782 | 1987-01 … 2026-10 | date |
| PCEPI | 7,669 | 1959-01 … 2026-09 | date |
| PCEPILFE | 7,709 | 1959-01 … 2026-09 | date |
| UNRATE | 2,053 | 1948-01 … 2026-10 | date |
| CES0500000003 | 1,485 | 2006-03 … 2026-10 | date |
| GDP | 2,883 | 1946-01 … 2026-07 | date |
| DFF | 26,438 | 1954-07 … 2026-11 | inferred |
| DGS2 | 12,612 | 1976-06 … 2026-11 | inferred |
| DGS10 | 16,204 | 1962-01 … 2026-11 | inferred |
| VIXCLS | 9,319 | 1990-01 … 2026-11 | inferred |

* 请求 **61** 次，**0** 次限流；原始响应全部归档在 `data/macro_raw/<task_id>/`（12 MB）。
* **幂等已验证**：原样重跑 `DFF` / `VIXCLS` → `inserted=0, updated=0, unchanged=26348`。

### 为什么后 4 条是 `inferred` 而不是 `date`

实测（不是推测）：

| 序列 | `output_type=3`（vintage 矩阵）表现 |
|---|---|
| `DFF` | **50 列**窗口即稳定 504（网关 60 s 超时），5 列才 200 |
| `DGS2` / `DGS10` | 同上 |
| `VIXCLS` | 可跑通，但只覆盖 **4,062 / 9,200** 个交易日（vintage 起点 2010-11-22） |

这 4 条又都是**零修订**序列（`fred_live_validation.md`：498~729 个比对点全部无修订）。
因此改取"全历史 + 推定发布时序"，并把口径如实标为 `inferred`、`verified=0`：

* `DGS2` / `DGS10` / `VIXCLS`：观测日 D 收盘值，按 **D 日 23:59:59 ET** 可见；
* `DFF`：纽约联储**次一工作日**发布，故推后 1 个工作日（只跳周末，不建模节假日）。

**这不是"伪造发布时间"**：`time_precision='inferred'` 会在每次快照里出现在
`precision_warnings` 中，调用方无法把它误当成实测时刻。

---

## 四、防未来数据泄漏：设计与实证

### 4.1 六个时间严格分开

`observation_period`（所属期间）/ `vintage_date`（版本生效日）/ `realtime_start·end`
（FRED 实时窗口）/ `release_date`（发布日）/ `release_timestamp` + `release_epoch`
（可得时点，UTC 秒）/ `ingested_at`（本地采集时刻）。

**没有把 `realtime_start` 当发布时间**。FRED 只给到"日"，所以：

```
release_date      = vintage_date                     （有据）
release_timestamp = 当日 23:59:59 America/New_York    （保守）
time_precision    = 'date'    verified = 0            （未核验，如实标记）
release_epoch     = int(release_timestamp)
```

取 23:59:59 而不是 08:30，意味着**最多晚约 15 小时才可见** —— 方向是"晚知道"，
不可能"提前知道"。等接入官方发布日历，把该行改成 `exact` + `verified=1` 即可，
表结构无需变动。

跨时区比较一律走 `release_epoch`（UTC 秒），不用字符串序。

### 4.2 查询接口

```python
snap = get_macro_snapshot("data/macro_data.db", "2023-04-28T00:00:00-04:00",
                          series=["GDP", "CPIAUCSL"])
snap.value("GDP")            # 该时点已公开的最新值
snap.missing_series          # 该时点完全无可见数据
snap.precision_warnings      # 精度不足的告警（按序列聚合）
```

实现：`WHERE release_epoch <= :as_of` + `ROW_NUMBER() OVER (PARTITION BY series,period
ORDER BY release_epoch DESC, vintage_date DESC)` 取 `rn=1`。未发布的行**不出现在结果里**。

### 4.3 真实回放证据（`reports/marketdata_probe/macro_replay_evidence.json`）

| 序列 | 观测期 | 版本数 | 值域 | 回放结论 |
|---|---|---:|---|---|
| GDP | 1994Q1 | 15 | 6,574.7 → 7,115.652（**+8.2%**） | 无泄漏 |
| PCE | 2003-04 | 17 | 112.81 → 77.593（**-31%**，基准重定） | 无泄漏 |
| 核心 PCE | 2003-04 | 17 | 111.72 → 78.561（-30%） | 无泄漏 |
| 非农就业 | 1990-01 | 24 | 108,944 ~ 109,931 | 无泄漏 |
| CPI | 2016-08 | 7 | 240.301 ~ 240.602 | 无泄漏 |
| 核心 CPI | 2016-08 | 7 | 248.332 ~ 248.423 | 无泄漏 |
| 平均时薪 | 2006-03 | 8 | 23.77 ~ 23.82 | 无泄漏 |
| 失业率 | 1948-01 | 4 | — | 无泄漏 |

**12 条序列逐个回放，`leaks=[]`。**

三个最有说服力的具体事实：

1. **2023-04-28 那天看到的 GDP 是 26,465.865**（2023-04-27 初值），
   而不是今天的 27,241.282 —— 差 **2.93%**。用今天的值回测 2023 年 4 月的决策，
   会凭空多出 3 个百分点的信息优势。
2. **PCE 2003-04 的 17 个版本从 112.81 一路变成 77.593**。这不是"修订"，
   是**基准重定（rebase）**：把不同 vintage 的 PCE 水平值直接连起来算通胀率，
   会得到完全错误的同比。"同一序列不同版本不可混用"是硬约束。
3. **非农 1990-01 有 24 个版本** —— 年度基准修正每年都会回头改十几年前的数字。

---

## 五、测试

`tests/test_macro_db.py`：**33 项**，全量 **1632 项通过 / 0 失败 / 0 错误 / 1 跳过**（JUnit XML 核准）。

| 你要求覆盖的场景 | 对应测试 |
|---|---|
| 同一观测期间多个 vintage | `test_multiple_vintages_of_same_period_coexist` |
| 初值与修订值并存 | `test_initial_and_revision_are_distinguishable` |
| 决策时间早于首次发布 | `test_snapshot_before_first_release_returns_missing` |
| 决策时间恰好在发布日期 | `test_snapshot_exactly_at_release_is_visible` |
| 决策时间在修订发布之后 | `test_snapshot_after_revision_uses_latest_visible_version` |
| 时区与夏令时边界 | `test_dst_boundary_offsets_differ`、`test_epoch_comparison_beats_string_comparison`、`test_naive_timestamp_is_rejected` |
| 数据缺失与发布时间未知 | `test_unknown_release_time_is_excluded_and_reported`、`test_precision_warning_is_exposed`、`test_market_daily_rows_are_marked_inferred_and_lagged` |
| 重复导入 | `test_duplicate_import_is_idempotent`、`test_reimport_does_not_overwrite_historical_initial_value` |
| 中断恢复 | `test_interrupted_ingest_can_resume`、`test_failed_series_does_not_block_others` |
| 损坏 / 迁移失败回滚 | `test_schema_migration_rolls_back_completely`、`test_schema_refuses_downgrade`、`test_real_db_volume_and_integrity`（`PRAGMA integrity_check` = ok） |
| 真实 GDP/就业/CPI 回放 | `test_real_replay_never_reads_future_version[CPIAUCSL/GDP/PAYEMS]`、`test_real_db_has_no_future_leak_across_series`、`test_real_gdp_revision_is_material` |

**回放断言的正确表述**（第一版写错过）：不是"发布前什么都看不到"，而是
"第 i 版发布前 1 秒，看到的必须**严格等于第 i−1 版**的值"；只有 i=0 才要求不可见。
写成"一律不可见"会把正常的历史可见性误判成泄漏。

**测试通过发现了 1 个真 bug**：`upsert_releases` 把 `verified` 当成
`time_precision` 比较，导致"精度从 date 升级到 exact"这种更新永远检不出来
（已修，`test_release_precision_change_is_detected` 固化）。

---

## 六、备份与回滚

* 备份：`backups/us_macro_20261119-153407/`（`MANIFEST.sha256.json` 3 条，
  与当前工作树逐一比对 **0 不一致**）。
* 回滚整个库：`rm data/macro_data.db*`（独立文件，不影响任何其它库）。
* 迁移失败自动回滚：`schema.ensure_schema` 在**同一个显式事务**里逐条执行 DDL，
  任一条失败即 `ROLLBACK`。**不能用 `executescript`** —— 它会先隐式 COMMIT，
  第 3 条 DDL 语法错时前 2 张表已经落盘（已用注入非法 DDL 的测试证明）。

---

## 七、未解决 / 边界（如实列出）

1. **发布时刻仍未核验**（`verified=0`）。`date` 精度按当日 23:59:59 处理，
   `inferred` 按收盘规则推定。**没有接 BLS/BEA/纽约联储的官方发布日历** ——
   这需要逐月抓取发布日程并逐条对齐，本轮未做，所以也没有把任何一行标成 `exact`。
2. **`is_initial_release` = "本库能取到的最早版本"，不等于真实首次公布**。
   FRED 的 vintage 历史不从序列诞生开始（`CPIAUCSL` 起于 1972-07-21，
   `CPILFESL` 起于 1987），更早观测的"初值"其实是当时在流通的某一版。
   要拿真实初值需 `output_type=4` + 官方日历。
3. **PCE 的基准重定没有专门处理**。库里如实保存了 112.81 → 77.593 的全部版本，
   但**没有任何代码阻止使用者跨版本相减**。若要用 PCE 算同比，必须在同一 vintage
   内计算。这是一个已知的、会真实咬人的坑。
4. **4 条日频序列的修订历史未保存**（`inferred` 单版本）。理由是 FRED 侧 vintage
   矩阵 504 + 实测零修订；若将来发现它们确实被修订过，需要重做这 4 条。
5. **未建节假日日历**。`DFF` 的"次一工作日"只跳周末，遇到联邦假日会**提前**
   一天可见（比如周五观测、周一为假日时，实际周二才发布）。方向虽然是"晚"，
   但这一条本身是"早"，属已知偏差，未修。
6. **未接交易策略**。按你的边界，本轮只建库 + 读接口，`src/ai_trader/strategy/`
   与 `broker/` 一行未动，`us_phase2.enabled` 仍为 `false`。

---

## 八、交付物

| 文件 | 说明 |
|---|---|
| `data/macro_data.db` | 独立库，5 张表，104,854 个版本行 |
| `docs/macro_db.md` | 结构文档 + 六时间模型 + 查询接口 + 限制 |
| `src/ai_trader/macro/{schema,store,ingest,snapshot}.py` | 建表 / 存储 / 入库 / 点时间快照 |
| `tools/macro_ingest.py` | 幂等入库 CLI（`--dry-run` / `--series` / `--report`） |
| `tools/macro_snapshot.py` | 命令行时点查询（与回测同一函数） |
| `tools/macro_replay_check.py` | 生成真实回放证据 |
| `tests/test_macro_db.py` | 33 项测试 |
| `reports/marketdata_probe/macro_replay_evidence.json` | 12 条序列的版本链与三时点回放 |
| `data/macro_raw/<task_id>/` | 61 份原始响应（12 MB，未含 key） |
| `reports/marketdata_probe/macro_ingest_summary.json` | 逐序列入库结果（由 `macro_ingest_log` 汇总） |

> `macro_ingest_log` 里保留了一条 `DFF / running` 的旧记录 —— 那是第一次全量运行
> 在 DFF 上被中断的痕迹（vintage 矩阵 504 卡住）。后续任务 `ingest-20261119T161907`
> 用另一种口径把 DFF/ DGS2 / DGS10 / VIXCLS 全部导入成功，**数据无缺口**；
> 这条记录没有删，用作"中断确实发生过、但可恢复"的证据。

**安全核对**：key 未打印、未落盘、未入库（`archive_raw` 剔除 `api_key` 并在写库后
复查一次，有单测）；未改策略、未连实盘、未下单、未买套餐、未建 `macro_data.db`
以外的任何库。
