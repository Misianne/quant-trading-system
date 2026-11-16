# LULD 完整生命周期 + 重新开盘竞价 实施报告

- 日期：2026-11-16
- 起点 `def8f23` → 本轮 **`1f64f01`**
- **结论：生命周期与竞价状态机已完整建模并通过 Replay 验证；交易所差异部分 UNVERIFIED；实时数据源仍 BLOCKED。**

---

## 1. 基线与备份

| 项 | 值 |
|---|---|
| 起始 HEAD | `def8f23`（工作树干净） |
| 本轮提交 | **`1f64f01`**（5 文件，+1028/−7） |
| 关键历史提交 | `9f864e8` / `dfacba8` 均存在 |
| `us_phase2.enabled` | **false**（未动） |
| 本轮备份 | `C:\FosunAI-Trading-Backups\round3-20261116-150719\` |
| 备份文件数 | **816** |
| SHA256 清单 | 816 行，`sha256sum -c` → **exit 0** |
| JUnit 证据 | `final2.xml` |

---

## 2. 规则来源

| 来源 | 用途 | 版本/日期 |
|---|---|---|
| LULD Plan（含 Amendment 12 / 18） | 暂停时长、15 秒、带宽、开盘/收盘窗口 | Amendment 18 生效 2020-02-24 |
| **Nasdaq Rule 4120(c)(10)** | Display Only Period、Auction Reference Price、Auction Collar、延长 | SR-NASDAQ-2019-057 文本 |
| **SR-NASDAQ-2026-064**（19b-4） | 竞价字段与 3:50 收盘竞价规则 | 2026 提案文本 |
| NYSE *Enhancement to Reopening Process after LULD Trading Pause* | NYSE 侧的 ARP / collar / 收盘竞价 | NYSE Group 说明 |
| NYSE *Amendment 12 Overview* | 5 分钟分段、5% 放宽、3:50 延后至收盘竞价 | 同上 |
| SEC 34-86412 | Nasdaq 竞价流程的 SEC 公告版 | 2019 |

> 未逐字抓取：`nasdaqtrader.com/TraderNews.aspx?id=UTP2019-09`（页面结构变化）。开盘双倍取消仍以 Amendment 18 为准。

---

## 3. 领域模型

### 3.1 ReferencePriceState

| 字段 | 说明 |
|---|---|
| `symbol` / `tier` | 归一化符号 + Tier |
| `previous_close` | 前收（决定价格档位） |
| `reference_price` | 最近 5 分钟**合格成交**均值 |
| `reference_window_start/end` | 窗口边界 |
| `last_eligible_trade_at` | 最后一笔合格成交 |
| `effective_at` / `source` / `version` | 生效时刻 / 来源 / 版本 |
| `quality` | `ok` / `no_eligible_trades` / `no_previous_close` / `out_of_session` |

**关键实现**：计算参考价时**先取此前 5 分钟均值，再把当前成交纳入窗口** —— 当前成交不能进入自身参考价（上一轮的真实缺陷，已修）。

### 3.2 PriceBandState

`reference_price / upper_band / lower_band / tier / price_bucket / band_width / effective_at / expires_at / session / source / version / doubled`

沿用官方三档 × 两 Tier；`doubled` 由 Amendment 18 决定（开盘单倍；收市前 25 分钟 Tier1 与 Tier2≤$3 双倍）。

### 3.3 LimitStateRecord

`entered_at / direction / band / observed_until / returned_inside / triggered_pause / version / source`

15 秒窗口；方向翻转会**重新计时**（更保守）；`returned_inside=True` 才回到 OPEN。

### 3.4 TradingPause

`pause_started_at / minimum_pause_until / pause_reason / reference_price / band / auction_status / source / version`

`minimum_pause_until = started + 5min` —— **只是最短时间**，到期不会自动 OPEN。

### 3.5 AuctionState（竞价）

`symbol / exchange / auction_id / auction_started_at / indicative_price / collar_lower / collar_upper / paired_quantity / imbalance_quantity / imbalance_side / extension_count / last_update_at / official_resume_at / source / version / status / auction_reference_price`

---

## 4. Nasdaq 与 NYSE 的差异

| 维度 | Nasdaq（Rule 4120(c)(10)） | NYSE（Amendment 12） | 本实现 |
|---|---|---|---|
| Auction Reference Price | Limit Down → **Lower Band**；Limit Up → **Upper Band** | 同（触发暂停的价格带那一侧） | ✅ 两者一致 |
| 初始 collar | 失衡侧 = ARP ± 5%（≤$3 为 $0.15）；对侧 = **对侧价格带** | 同 | ✅ |
| 分段 | 5 分钟 Initial Display Only，失衡则 Extended | 5 分钟一段 | ✅ 统一用 `MIN_PAUSE`/`EXTENSION` |
| 每次延长 | 只放宽**失衡侧** 5%（或 $0.15），对侧不动 | 同 | ✅ |
| 恢复条件 | 无失衡 → 释放（须交易所发布） | 同 | ✅ 统一要求 `on_official_resume` |
| 收盘前 10 分钟 | 转 **LULD Closing Cross**（Rule 4754(b)(6)） | 延后并入 **closing auction** | ✅ 抽象为 `CLOSE_CUTOFF_MINUTES`，`auction_status=AUCTION_CANCELLED` |
| 跨交易所 | 若主交易所未开、且无 Regulatory Halt，其他所不得先恢复 | 同 | ⚠️ 由"只认官方恢复"间接体现，**未建模多所联动**（UNVERIFIED） |
| 撮合算法 | Halt Cross 价格选择算法（Rule 4753(b)(2)(A)-(D)） | DMM 主导的开盘竞价 | ❌ **未建模**（UNVERIFIED） |

> 两个 Policy 类已分开（`NasdaqAuctionPolicy` / `NyseAuctionPolicy`），字段与流程的**未核实差异**标注在 `notes` 里，没有凭猜测填参数。

---

## 5. 完整状态机

```
        ┌──────────────────────────────────────────────────────────┐
        │                                                          │
   OPEN ─┬─ 成交在带内 ────────────────────────────────────────────┘
        │
        ├─ 触及带 ──> LIMIT_STATE ──(≤15s 回带内)──> OPEN
        │                 │
        │                 └─(≥15s 仍在带外)──> TRADING_PAUSE
        │                                          │
        │                          (收市前 ≤10min) └──> 保持暂停（AUCTION_CANCELLED）
        │                                          │
        │                          (满 5min 且启动竞价)
        │                                          ↓
        │                                  REOPENING_AUCTION
        │                                    │            │
        │                     (指示价越界/失衡)│            │(无失衡)
        │                                    ↓            ↓
        │                            AUCTION_EXTENDED  RESUME_PENDING
        │                                    │            │
        │                                    └─(直到无失衡)┘
        │                                                 │
        │                                   (收到官方恢复) ↓
        └──────────────────────────────────────────── REOPENED
```

**不存在**：`TRADING_PAUSE ──(计时)──> OPEN`
**不存在**：`RESUME_PENDING ──(推断)──> REOPENED`
**存在**：`任意 ──数据中断──> DATA_UNAVAILABLE`（不可交易，不接受恢复）
**存在**：`非法输入 ──> INVALID_EVENT`

辅助状态：`REFERENCE_PRICE`、`PRICE_BAND`（参考价/带宽更新的中间态）、`MARKET_CLOSED`、`AUCTION_CANCELLED`。

---

## 6. 调用链接线

```
MarketStatusService.status()
   ├─ LULD 暂停到期 ──> lifecycle.start_auction()   ← 不再直接 OPEN
   └─ 状态仍为 LULD_PAUSED（不可交易）

RiskController.evaluate()      → intent_gate()      允许退出意图
Engine._execute()              → submit_gate()      暂停时拒绝提交，记 order_submit_blocked
PaperBroker.place_order()      → fill_allowed()     暂停/竞价期间不成交（含 lifecycle.tradable 复核）
```

`fill_allowed` 现在做**双重判定**：`status().state == OPEN` **且** `lifecycle.tradable`。
即使状态因某种原因显示 OPEN，只要生命周期还在竞价/待恢复，也不会成交。

---

## 7. Replay 样例（测试中的真实序列）

```
t0        open=100  band=[95,105]
t0+1s     trade 80  → LIMIT_STATE(lower)
t0+15s    trade 80  → 仍在 LIMIT_STATE（15s 边界）
t0+17s    trade 80  → TRADING_PAUSE，minimum_until=t0+5m17s
t0+5m18s  start_auction → REOPENING_AUCTION，ARP=95，collar=[90.25,105]
t0+6m     auction_update(ver=2, ind=100) → RESUME_PENDING（无失衡）
t0+7m     official_resume(ver=100) → REOPENED，tradable=True
```

反例序列（均有测试）：

| # | 反例 | 预期 |
|---|---|---|
| 1 | 4:59 就发竞价 | 忽略，保持 TRADING_PAUSE |
| 2 | 满 5 分钟无竞价数据 | 保持 TRADING_PAUSE |
| 3 | 指示价越出 collar | AUCTION_EXTENDED，只放宽该侧 |
| 4 | 反复延长 | `extension_count` 递增，collar 单调放宽 |
| 5 | 恢复后又来新暂停 | 重新进入 TRADING_PAUSE |
| 6 | 恢复后收到旧版本恢复 | 忽略，不回退 |
| 7 | 同一竞价事件重复 10 次 | 只生效 1 次 |
| 8 | 数据中断 + 时钟前进 | DATA_UNAVAILABLE，不接受恢复 |
| 9 | 暂停期遗留止损单 | `fill_allowed=False`，Paper 不成交 |
| 10 | 恢复后旧订单价格 | 由 quote gateway 判过期（上一轮已有） |
| 11 | `AAPL` vs `usAAPL` | `normalize_symbol` 归一；`USB`/`CNC` 不误剥 |
| 12 | 一所恢复、另一所暂停 | 两个 lifecycle 独立 |
| 13 | 早收市套用正常收盘 | 收盘前 10 分钟 → 不恢复连续交易 |
| 14 | 竞价状态未知 | 永不判 REOPENED |

---

## 8. 测试

| 项 | 值 |
|---|---|
| 全量 | **765** |
| 失败 / 错误 | **0 / 0** |
| 跳过 | 1 |
| 耗时 | 76 s |
| 本轮新增 | **26**（`tests/test_us_luld_lifecycle.py`） |
| JUnit | `C:\FosunAI-Trading\final2.xml` |

**本轮修改 1 处旧断言**（`test_luld_pause_expires_after_5_minutes`）：
旧断言"5 分钟到期 → 不再是 LULD_PAUSED"与本轮规则矛盾，已改为
**"到期后仍是 LULD_PAUSED + note 提到竞价/恢复 + `fill_allowed=False`"** —— 比原来更严格。

**未删除任何测试，未跳过失败项。**

---

## 9. 未实现 / UNVERIFIED / BLOCKED

| 项 | 状态 | 说明 |
|---|---|---|
| 竞价**撮合算法**（Nasdaq Halt Cross 价格选择、NYSE DMM 竞价） | **UNVERIFIED / 未实现** | 只建模了"失衡→延长"与"释放需官方"，未模拟撮合定价 |
| 多交易所联动（一所未开、其他所不得恢复） | **UNVERIFIED** | 由"只认官方恢复"间接约束，未显式建模 |
| `paired_quantity` 的官方定义细节 | **UNVERIFIED** | 字段已留，语义按"已配对数量"理解，未逐条核对 |
| 竞价延长后的"10 分钟后可提前恢复" | **未实现** | 当前每次失衡都延长 5 分钟，不实现"满足条件即释放"的加速路径 |
| 收盘竞价（Closing Cross） | **未建模** | 只标记 `AUCTION_CANCELLED`，不模拟收盘撮合 |
| 真实停牌/竞价数据源 | **BLOCKED** | 无数据源，全部输入需注入 |
| Tier 1/2 名单 | **PARTIAL** | 需外部名单，系统只支持显式配置 |
| 符号归一 | ✅ 已修 | `usAAPL`→`AAPL`；`USB`/`CNC` 不再误伤；`hk00700`→`00700` |

---

## 10. 是否满足本地 Paper/Replay 验证条件

**满足。**

- ✅ 生命周期全链路可在 Replay 下复现（确定性事件 + 注入时钟）
- ✅ 14 个反例均有测试
- ✅ Paper 在暂停/竞价期不成交，恢复后可成交
- ✅ 三阶段（意图/提交/成交）隔离
- ⚠️ 但**竞价撮合价格**不模拟，`REOPENED` 必须由外部注入官方恢复 —— 这是设计上的诚实边界
- ⚠️ `us_phase2.enabled=false`，所以生产默认链路不会强制这些规则

**不能**：宣称"实时 LULD 保护已上线"。

---

## 11. 复现与回滚

```powershell
cd C:\FosunAI-Trading
.\.venv\Scripts\python.exe -m pytest tests/ -q --ignore=tests/test_sdk_install.py
# → 765 passed, 1 skipped

git reset --hard def8f23      # 回到本轮开始前
```

---

*本报告不含任何密钥或账户信息。未连接真实券商，未发真实订单，未开启 `us_phase2`。*
