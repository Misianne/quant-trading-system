"""生成 Databento 官方 schema 形状的样本文件。

**这不是真实抓包。** 本轮没有 Databento API Key（环境变量、用户级持久变量、
凭据文件、`.env` 四处均无），因此无法调用历史接口取真实响应。这里生成的是
**按官方字段表逐字段构造**的样本，用来验证解析器是否与官方 schema 一致。

它证明得了什么、证明不了什么，必须分清：

* 证明得了：字段名、类型、枚举取值、价格标度、时间单位、缺失字段（symbol /
  sequence）时解析器的行为是否符合预期；
* 证明不了：某只股票在某天是否真的停牌、供应商实际投递顺序、真实抖动。

因此报告里所有"真实数据"结论一律标 BLOCKED，本文件产出的结论只用于
"解析语义是否正确"。

官方来源
--------
* status 字段与枚举：https://databento.com/docs/schemas/status
* trades 字段与价格标度：https://databento.com/docs/schemas/trades
* publisher_id 映射：databento 0.90.0 ``common/publishers.py::Venue.from_int``

用法
----
    python verification/databento/gen_fixtures.py
"""
from __future__ import annotations

import json
import pathlib
from datetime import datetime, timedelta, timezone

OUT = pathlib.Path(__file__).resolve().parent / "fixtures"

#: 2026-11-16 14:30:00Z = 美东 09:30（常规时段开盘）
T0 = datetime(2026, 11, 16, 14, 30, 0, tzinfo=timezone.utc)

NS = 1_000_000_000
PUBLISHER_XNAS = 2
AAPL_INSTRUMENT_ID = 42


def ns(dt: datetime) -> int:
    return int(dt.timestamp()) * NS + dt.microsecond * 1000


def iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def trade(dt: datetime, price: float, size: int, seq: int, side: str = "B",
          recv_delta_us: int = 120) -> dict:
    """一条 trades 记录，字段与官方 trades 表逐项对应。"""
    return {
        "ts_recv": ns(dt) + recv_delta_us * 1000,
        "ts_event": ns(dt),
        "rtype": 0,
        "publisher_id": PUBLISHER_XNAS,
        "instrument_id": AAPL_INSTRUMENT_ID,
        "action": "T",            # 官方：trades 里恒为 'T'
        "side": side,
        "depth": 0,
        "price": int(round(price * NS)),   # 官方：int64，1 unit = 1e-9
        "size": size,
        "flags": 128,             # F_LAST
        "ts_in_delta": -recv_delta_us * 1000,
        "sequence": seq,
    }


def status(dt: datetime, action: int, reason: int, *, is_trading: str,
           is_quoting: str, ssr: str = "~", trading_event: int = 0) -> dict:
    """一条 status 记录。注意官方表里**没有** sequence，也没有 symbol。"""
    return {
        "ts_recv": ns(dt) + 80_000,
        "ts_event": ns(dt),
        "rtype": 18,
        "publisher_id": PUBLISHER_XNAS,
        "instrument_id": AAPL_INSTRUMENT_ID,
        "action": action,          # 官方：uint16 枚举，HALT=8 / PAUSE=9 / TRADING=7
        "reason": reason,          # 官方：uint16 枚举，LULD_PAUSE=50
        "trading_event": trading_event,
        "is_trading": is_trading,
        "is_quoting": is_quoting,
        "is_short_sell_restricted": ssr,
    }


def write(name: str, rows: list[dict]) -> pathlib.Path:
    path = OUT / name
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8")
    return path


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- #
    # 1. LULD 暂停 → 恢复（DBN 原始形态：整数枚举 + ns + 1e-9 定点价）
    # ---------------------------------------------------------------- #
    rows = [
        trade(T0, 201.20, 100, 1001, "B"),
        trade(T0 + timedelta(seconds=1), 201.15, 200, 1002, "A"),
        status(T0 + timedelta(seconds=2), 9, 50,
               is_trading="N", is_quoting="Y", trading_event=0),
        # 停牌期间仍可能有成交（迟到打印/交叉盘）—— 这正是要验证的点
        trade(T0 + timedelta(seconds=10), 200.90, 50, 1003, "A"),
        status(T0 + timedelta(seconds=300), 7, 0,
               is_trading="Y", is_quoting="Y"),
        trade(T0 + timedelta(seconds=301), 201.00, 100, 1004, "B"),
    ]
    # 真实默认形态：**没有 symbol 字段**，标的是 instrument_id
    write("synthetic_luld_pause_nosym.jsonl", rows)
    # 带 symbol 的形态（等价于 to_json(map_symbols=True)）
    write("synthetic_luld_pause.jsonl",
          [{**r, "symbol": "AAPL"} for r in rows])

    # ---------------------------------------------------------------- #
    # 2. 普通新闻停牌 → 恢复（含 MWCB 市场级 reason）
    # ---------------------------------------------------------------- #
    rows = [
        trade(T0, 400.10, 10, 2001),
        status(T0 + timedelta(seconds=1), 8, 30,
               is_trading="N", is_quoting="N"),          # HALT / NEWS_PENDING
        status(T0 + timedelta(seconds=1800), 7, 31,
               is_trading="Y", is_quoting="Y"),          # TRADING / NEWS_RELEASED
        trade(T0 + timedelta(seconds=1801), 402.30, 10, 2002),
    ]
    write("synthetic_news_halt.jsonl", rows)

    # ---------------------------------------------------------------- #
    # 3. 市场级熔断 reason（120-124）——不能当单只证券普通停牌
    # ---------------------------------------------------------------- #
    rows = [
        trade(T0, 300.00, 10, 3001),
        status(T0 + timedelta(seconds=1), 8, 120,
               is_trading="N", is_quoting="N"),          # MWCB level 1
        status(T0 + timedelta(seconds=900), 7, 124,
               is_trading="Y", is_quoting="Y"),          # MWCB resumption
    ]
    write("synthetic_mwcb_halt.jsonl", rows)

    # ---------------------------------------------------------------- #
    # 4. 模糊状态：NOT_AVAILABLE_FOR_TRADING（收盘 or 停牌，官方不区分）
    # ---------------------------------------------------------------- #
    rows = [
        trade(T0, 150.00, 10, 4001),
        status(T0 + timedelta(seconds=1), 15, 0,
               is_trading="N", is_quoting="N"),
    ]
    write("synthetic_ambiguous_status.jsonl", rows)

    # ---------------------------------------------------------------- #
    # 5. 非停牌状态（QUOTING / CROSS / CLOSE）——不得制造假停牌
    # ---------------------------------------------------------------- #
    rows = [
        status(T0, 3, 1, is_trading="N", is_quoting="Y"),   # QUOTING
        status(T0 + timedelta(seconds=1), 12, 1,
               is_trading="N", is_quoting="N"),             # CLOSE
        status(T0 + timedelta(seconds=2), 14, 0,
               is_trading="Y", is_quoting="Y", ssr="Y"),    # SSR_CHANGE
    ]
    write("synthetic_non_halt_status.jsonl", rows)

    # ---------------------------------------------------------------- #
    # 6. "pretty" 形态（等价于 to_json(pretty_px=True, pretty_ts=True)）
    #    ISO 时间串 + 已转浮点价 + symbol 存在
    # ---------------------------------------------------------------- #
    pretty = []
    for r in [
        trade(T0, 201.20, 100, 1001),
        status(T0 + timedelta(seconds=2), 9, 50, is_trading="N", is_quoting="Y"),
        status(T0 + timedelta(seconds=302), 7, 0, is_trading="Y", is_quoting="Y"),
    ]:
        p = dict(r)
        for key in ("ts_event", "ts_recv"):
            dt = datetime.fromtimestamp(p[key] / NS, tz=timezone.utc)
            p[key] = iso(dt.replace(microsecond=(p[key] % NS) // 1000))
        if p["rtype"] == 0:
            p["price"] = round(p["price"] / NS, 2)
        p["symbol"] = "AAPL"
        pretty.append(p)
    write("synthetic_pretty.jsonl", pretty)

    # ---------------------------------------------------------------- #
    # 7. 坏样本：用于 fail-closed 验证
    # ---------------------------------------------------------------- #
    bad = OUT / "bad"
    bad.mkdir(exist_ok=True)
    (bad / "unknown_rtype.jsonl").write_text(
        json.dumps({"rtype": 99, "ts_event": ns(T0), "publisher_id": 2,
                    "instrument_id": 42, "symbol": "AAPL"}) + "\n",
        encoding="utf-8")
    (bad / "no_symbol_no_map.jsonl").write_text(
        json.dumps({"rtype": 0, "ts_event": ns(T0), "ts_recv": ns(T0),
                    "publisher_id": 2, "instrument_id": 999,
                    "price": 100 * NS, "size": 1, "sequence": 1}) + "\n",
        encoding="utf-8")
    (bad / "undef_price.jsonl").write_text(
        json.dumps({"rtype": 0, "ts_event": ns(T0), "ts_recv": ns(T0),
                    "publisher_id": 2, "instrument_id": 42, "symbol": "AAPL",
                    "price": 9223372036854775807, "size": 1, "sequence": 7}) + "\n",
        encoding="utf-8")
    (bad / "unknown_action.jsonl").write_text(
        json.dumps({"rtype": 18, "ts_event": ns(T0), "ts_recv": ns(T0),
                    "publisher_id": 2, "instrument_id": 42, "symbol": "AAPL",
                    "action": 77, "reason": 0}) + "\n",
        encoding="utf-8")
    (bad / "naive_ts.jsonl").write_text(
        json.dumps({"rtype": 0, "ts_event": "2026-11-16T14:30:00",
                    "ts_recv": ns(T0), "publisher_id": 2,
                    "instrument_id": 42, "symbol": "AAPL",
                    "price": 100 * NS, "size": 1, "sequence": 8}) + "\n",
        encoding="utf-8")

    # ---------------------------------------------------------------- #
    # 8. symbology 映射（模拟 metadata.symbol_map 导出）
    # ---------------------------------------------------------------- #
    (OUT / "symbology.json").write_text(
        json.dumps({str(AAPL_INSTRUMENT_ID): "AAPL"}, indent=2),
        encoding="utf-8")

    # ---------------------------------------------------------------- #
    # 9. 说明文件（避免后人把合成样本误当真实抓包）
    # ---------------------------------------------------------------- #
    (OUT / "README.md").write_text(
        "# Databento 样本说明\n\n"
        "**这些文件是按官方 schema 构造的合成样本，不是真实 API 响应。**\n\n"
        "本轮没有任何 Databento 凭据，因此无法获取真实历史数据。样本只用于验证\n"
        "解析器与官方字段表是否一致（字段名、类型、枚举值、价格标度、时间单位、\n"
        "缺失字段）。\n\n"
        "生成脚本：`verification/databento/gen_fixtures.py`\n\n"
        "| 文件 | 用途 |\n"
        "|---|---|\n"
        "| `synthetic_luld_pause.jsonl` | LULD 暂停→恢复，含停牌期间成交 |\n"
        "| `synthetic_luld_pause_nosym.jsonl` | 同上，但**无 symbol 字段**（真实默认） |\n"
        "| `synthetic_news_halt.jsonl` | 新闻停牌 HALT→TRADING |\n"
        "| `synthetic_mwcb_halt.jsonl` | 市场级熔断 reason 120/124 |\n"
        "| `synthetic_ambiguous_status.jsonl` | NOT_AVAILABLE_FOR_TRADING |\n"
        "| `synthetic_non_halt_status.jsonl` | QUOTING/CLOSE/SSR_CHANGE |\n"
        "| `synthetic_pretty.jsonl` | pretty 形态（ISO 时间 + 浮点价 + symbol） |\n"
        "| `bad/*.jsonl` | 各类坏样本，用于 fail-closed 验证 |\n"
        "| `symbology.json` | instrument_id → symbol 映射 |\n",
        encoding="utf-8")

    names = sorted(p.name for p in OUT.iterdir())
    print("fixtures:", names)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
