# Databento 样本说明

**这些文件是按官方 schema 构造的合成样本，不是真实 API 响应。**

本轮没有任何 Databento 凭据，因此无法获取真实历史数据。样本只用于验证
解析器与官方字段表是否一致（字段名、类型、枚举值、价格标度、时间单位、
缺失字段）。

生成脚本：`verification/databento/gen_fixtures.py`

| 文件 | 用途 |
|---|---|
| `synthetic_luld_pause.jsonl` | LULD 暂停→恢复，含停牌期间成交 |
| `synthetic_luld_pause_nosym.jsonl` | 同上，但**无 symbol 字段**（真实默认） |
| `synthetic_news_halt.jsonl` | 新闻停牌 HALT→TRADING |
| `synthetic_mwcb_halt.jsonl` | 市场级熔断 reason 120/124 |
| `synthetic_ambiguous_status.jsonl` | NOT_AVAILABLE_FOR_TRADING |
| `synthetic_non_halt_status.jsonl` | QUOTING/CLOSE/SSR_CHANGE |
| `synthetic_pretty.jsonl` | pretty 形态（ISO 时间 + 浮点价 + symbol） |
| `bad/*.jsonl` | 各类坏样本，用于 fail-closed 验证 |
| `symbology.json` | instrument_id → symbol 映射 |
