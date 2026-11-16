# LULD / MWCB / 停牌：规则闭环与三阶段交付报告

- 日期：2026-11-16
- 起点：`dfacba8` → 本轮：**`9f864e8`**
- **总体：规则闭环已完成（PASS）；实时数据源仍 BLOCKED。**

---

## 1. 修改前后的规则差异

| # | 项 | 修改前（dfacba8） | 修改后（9f864e8） | 官方依据 |
|---|---|---|---|---|
| 1 | LULD 暂停时长 | 与 MWCB 共用 `halt_expiry_minutes=15` | **独立 `luld_pause=5min`** | LULD Plan（15 秒未回带内 → 暂停 5 分钟） |
| 2 | MWCB L1/L2 暂停 | 15 分钟 | 15 分钟（独立参数） | MWCB FAQ |
| 3 | MWCB L3 | 当日关闭（已对） | 当日关闭 + **永不自动恢复** | MWCB FAQ |
| 4 | 交易所停牌 | 到期自动转 OPEN | **只认恢复通知**（`notify_resume`），过期时间仅作展示 | 交易所规则 |
| 5 | 状态事件 | 只有 `note` 字段 | `StatusEvent{version,kind,state,triggered_at,expected_resume_at,recovery_notice_at,source}` | 审计要求 |
| 6 | LULD 适用时段 | 未校验 | **仅常规时段**；盘前/盘后/夜间 → `OUT_OF_SESSION` | LULD 仅 9:30–16:00 ET |
| 7 | 参考价 | 调用方随意给 | **最近 5 分钟合格成交均值**，≥1% 才更新；无成交 → `NO_REFERENCE` | LULD Plan |
| 8 | 15 秒 Limit State | 无 | 新增 `LULD_LIMIT_STATE` 状态，15 秒未回带内才暂停 | LULD Plan |
| 9 | 开盘双倍 | 一律 20%（错） | **开盘一律单倍**（Amendment 18） | Amendment 18 / SIAC 2020-02-18 |
| 10 | 收市双倍 | 全部 Tier 双倍 | **Tier1 双倍；Tier2 ≤$3 双倍；Tier2 >$3 单倍** | Amendment 18 |
| 11 | 收市窗口 | 固定 15:35 | **按当日实际收盘时间倒推 25 分钟**（早收市自动跟随） | SIAC 通知 |
| 12 | MWCB 截止 | 固定 15:25（=收市前 35 分钟） | **收市前 35 分钟**（早收市自动跟随） | MWCB FAQ |
| 13 | MWCB 重复触发 | 未限制 | 每级每日只触发一次，`reset_day()` 跨日重置 | MWCB FAQ |
| 14 | 三阶段语义 | 退出被"整体放行" | **意图 / 提交 / 成交** 分离 | 本轮 P0-4 |

---

## 2. 参数与官方来源

| 参数 | 值 | 来源 |
|---|---|---|
| LULD 暂停 | **5 分钟** | LULD Plan §Limit State / Trading Pause |
| LULD Limit State 判定 | **15 秒** | 同上 |
| LULD 参考价窗口 | **5 分钟成交均值**，移动 ≥1% 才更新 | LULD Plan |
| LULD 带宽 >$3 | Tier1 5% / Tier2 10% | LULD Plan Table 1 |
| LULD 带宽 $0.75–$3 | 20%（两 Tier 同） | 同上 |
| LULD 带宽 <$0.75 | min($0.15, 75%)；双倍窗 min($0.30,150%) | 同上 |
| 收市双倍窗口 | 最后 **25 分钟** | Amendment 18 / NYSE |
| 开盘双倍 | **取消**（单倍） | Amendment 18（2020-02-24 生效）；SIAC 2020-02-18 通知 |
| MWCB L1/L2/L3 | **−7% / −13% / −20%** | MWCB FAQ / Investor.gov |
| MWCB L1/L2 暂停 | **15 分钟** | 同上 |
| MWCB 截止 | 收市前 **35 分钟**以内不再触发 L1/L2 | 同上 |
| MWCB L3 | 当日剩余交易关闭 | 同上 |

> 说明：`https://www.nasdaqtrader.com/TraderNews.aspx?id=UTP2019-09` 与 NYSE 交易信息页本轮**未能逐字抓取**（站点结构/反爬），
> 开盘双倍取消的结论以 Amendment 18 与 SIAC 通知为准，并已在代码注释中标注来源。

---

## 3. 状态生命周期

```
                    ┌──────────── LULD ────────────┐
 OPEN ──越带──> LULD_LIMIT_STATE ──(≥15s)──> LULD_PAUSED ──(5min)──> OPEN
                 └──(<15s 回带内)──> OPEN

 OPEN ──MWCB L1/L2（收市前>35min）──> MWCB_PAUSED ──(15min)──> OPEN
 OPEN ──MWCB L3（任意时间）──────> MWCB_CLOSED ──(永久，直到 reset_day)

 OPEN ──交易所公告──> HALTED ──(必须 notify_resume)──> OPEN
                      └─ 预计恢复时间到期也不自动解除

 任意 ──无行情/超龄──> DATA_STALE / UNKNOWN ──(新行情)──> OPEN
```

**关键约束**：
- 交易所停牌**没有时间自动恢复路径**（代码里不存在"HALTED 到期 → OPEN"的分支）。
- LULD 的 5 分钟计时器**只对 `LULD_PAUSED` 生效**，不会触碰 `HALTED`（有专门测试）。
- 停牌解除 ≠ 行情新鲜：解除后若没有新行情，状态会落到 `DATA_STALE`/`UNKNOWN`，**这是安全行为**。

---

## 4. 风险退出三阶段调用链

```
风险模块 / 策略
  │
  ├─ A 意图  risk.market_status.intent_gate(symbol, is_exit=True)
  │         └─ HALTED/LULD/MWCB 时 **仍 allowed=True**，附 warning「保留退出意图」
  │             → risk.evaluate() 放行，止损意图不会因组件缺失或暂停而消失
  │
  ├─ B 提交  strategy/engine.py:_execute()
  │         └─ market_status.submit_gate(symbol, is_exit=...)
  │             若不允许 → 记 `order_submit_blocked` 审计 + 意图标记 rejected
  │             **不调用 broker.place_order**（退出单也不会被标记为可执行）
  │
  └─ C 成交  broker/paper.py:place_order()
            └─ market_status.fill_allowed(symbol)
                不允许 → status 保持 "submitted"、filled_qty=0、exec_price=None
                         并在订单上记 `market_note`
                **不生成任何成交，也不伪造资金/持仓变动**
```

三阶段各自有测试：
`test_three_stages_during_halt`、`test_unknown_state_blocks_submit_but_keeps_exit_intent`、
`test_paper_does_not_fill_during_halt`、`test_paper_fills_after_resume`、
`test_engine_submit_gate_is_wired`（源码级断言 Engine 确实调用 `submit_gate`）。

---

## 5. 测试

| 项 | 值 |
|---|---|
| 全量 | **739** |
| 失败 / 错误 | **0 / 0** |
| 跳过 | 1 |
| JUnit XML | `C:\FosunAI-Trading\final.xml`（84 KB） |
| 本轮新增 | **34**（`tests/test_us_halt_lifecycle.py`） |

### 5.1 新增覆盖（对照 P0-6 要求）

| 要求 | 测试 |
|---|---|
| LULD 开盘前 15 分钟单倍 | `test_opening_window_is_never_doubled` |
| 收市前 Tier1 双倍 | `test_tier1_near_close_band_doubles` |
| 收市前 Tier2 低价双倍 | `test_luld_doubled_rules[2-2.0-10.0]` |
| 收市前 Tier2 高价单倍 | `test_tier2_high_price_band_stays_single_near_close` |
| 正常收市日 / 早收市日 | `test_early_close_uses_actual_close_time`、`test_mwcb_early_close_cutoff_shifts` |
| 盘前/盘后/夜间不算 LULD | `test_luld_out_of_regular_session_returns_out_of_session` |
| 15 秒 Limit State 边界 | `test_luld_limit_state_15_seconds_boundary`、`..._cleared_within_15s` |
| 5 分钟 LULD 暂停 | `test_luld_pause_is_5_minutes_not_15`、`test_luld_pause_expires_after_5_minutes` |
| MWCB 15 分钟 / L3 当日 | `test_mwcb_pause_is_15_minutes`、`test_mwcb_l3_closes_for_day_never_auto_resumes` |
| 三阶段隔离 | 见 §4 |
| 停牌期间不得 Paper 成交 | `test_paper_does_not_fill_during_halt` |
| 恢复通知 / 事件重复 / 乱序 | `test_events_carry_version_source_and_times`、`test_clock_jump_backwards_does_not_restore`、`test_exchange_halt_not_cleared_by_timer` |
| 参考价不得用 last 冒充 | `test_luld_without_qualifying_trades_is_unavailable`、`test_luld_reference_from_5min_trades_not_last` |
| 组件缺失不阻断退出（回归） | `test_market_status_does_not_block_exit`（沿用） |

### 5.2 本轮修正的测试（**未放宽断言**，是语义更新）

| 测试 | 原断言 | 新断言 | 原因 |
|---|---|---|---|
| `test_halt_blocks_entry_but_not_exit` | warning 含"风险退出" | 含"退出意图" | 措辞更准确，仍校验 allowed=True |
| `test_mwcb_levels_and_l3_close` | `update_index(...)` 默认触发 | 显式 `at_or_after_325=False` | **无时间信息时不再擅自停市**（更安全） |
| `test_luld_reference_from_5min_trades_not_last` | 期望 102.0 | 期望 101.0 | 1% 更新阈值生效（官方行为） |
| `test_exchange_halt_not_cleared_by_timer` | 要求解除后 `OPEN` | 要求"不是 HALTED" + 事件含 OPEN | 解除≠行情新鲜 |
| `test_luld_pause_expires_after_5_minutes` | 要求 `OPEN` | 要求"不是 LULD_PAUSED" + 事件含 OPEN | 同上 |

**没有删除任何测试，也没有把断言改弱**——只是把断言从"派生状态"改成"权威事件"，并补了显式时间窗口参数。

### 5.3 本轮抓到并修复的真实缺陷

1. **参考价把当前成交算进了自己的窗口** → 参考价被当前价污染（测试实测 150 把均值拉到 114）。
   已改为「先算前 5 分钟均值，再纳入当前成交」。
2. **`status()` 在 LULD/停牌解除后立刻套用陈旧判定**，导致"已恢复"被误读为"仍停牌"。
   已明确：解除停牌与行情新鲜是两件事，解除后无行情 → `DATA_STALE`（安全）。
3. **Paper 测试符号不一致**（服务端键 `AAPL` vs 券商 `usAAPL`）——集成隐患，已在测试中统一，
   并在报告 §7 列为待办。

---

## 6. Git 与回滚

| 项 | 值 |
|---|---|
| 本轮提交 | **`9f864e8`**（+1114 / −243，9 文件） |
| 回滚点 | **`dfacba8`**（上一轮 HEAD） |
| 更早回滚 | `2dcf58e`（P0/P1 基线） |
| 工作树 | 干净 |
| 备份 | `C:\FosunAI-Trading-Backups\round2-20261116-143021\`（770 文件，SHA256 校验通过） |

```powershell
git reset --hard dfacba8     # 回到上一轮
```

---

## 7. 未实现 / 无法确认

| 项 | 状态 | 说明 |
|---|---|---|
| 真实停牌 / LULD / MWCB 数据源 | **BLOCKED** | `feed.py` 只解析 last/prev_close/ts/seq/source，无 halt/luld/mwcb 字段 |
| Tier 1 / Tier 2 名单 | **PARTIAL** | 需外部成员表（S&P500/Russell1000/ETP 名单），本系统不内置，只能显式配置 `tier2_symbols` |
| LULD 重新开盘竞价 | **未建模** | 5 分钟到期即视为可交易；官方可能因竞价失衡延长暂停（**UNVERIFIED**） |
| LULD 暂停延长 | **未建模** | 同上 |
| 参考价 1% 更新 | ✅ 已实现 | `RollingReferencePrice` |
| 早收市 | ✅ 已实现 | 由 `DayBounds.close_at` 驱动 |
| MOC/LOC、收盘竞价 | **不在本轮范围** | — |
| 符号规范化 | **待办** | 服务端用 `AAPL`、Paper 用 `usAAPL`，接真实数据源前必须统一 |
| MWCB 指数数据 | **BLOCKED** | 无 S&P 500 实时行情源，`update_index` 只能由外部注入 |

---

## 8. 是否仍 BLOCKED

**分项回答：**

| 能力 | 结论 |
|---|---|
| LULD / MWCB **规则模型** | ✅ **不再 BLOCKED**（规则、边界、状态机、测试齐备） |
| LULD / MWCB **实时保护** | ❌ **仍 BLOCKED**（无数据源，`us_phase2.enabled=false`） |
| 交易所停牌处理 | ✅ 模型闭环（只认恢复通知）／❌ 无停牌数据源 |
| Paper 撮合在暂停期的行为 | ✅ 可验证（暂停不成交、恢复后可成交） |
| 真实券商侧行为 | ❌ 未验证（未连接） |

**一句话：规则这一层闭环了；能"算对"，但还不能"看见"。**

---

## 9. 安全边界确认

- `us_phase2.enabled` = **false**（未改）
- 未连接真实券商、未发真实订单、未启用真实卖空
- 未部署、未改真实账户权限
- 未删除任何报告/备份/数据库
- P0/P1 保护未削弱：`test_us_p0.py`(34) / `test_us_p1_batch*`(77) / `test_market_rules_b4.py`(29) 全通过

---

*本报告不含任何密钥或账户信息。*
