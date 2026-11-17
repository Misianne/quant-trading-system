"""供应商适配器。

本轮实现的是**可离线验证**的适配器：解析各家**官方文档里给出的字段形状**，
用录制的 fixture 驱动。它们不发起任何网络连接，也不读取任何密钥。

为什么先做离线适配器
--------------------
1. 真实接入需要 API Key 与付费套餐（用户明确要求不要密钥、不要采购）；
2. 供应商字段语义（尤其是时间单位、停牌事件覆盖范围）必须先用真实样本验证，
   否则"接上了"只是把错误数据接进了风控；
3. 适配器可独立测试是接入层质量的前提。

一旦拿到 Key，只需实现 ``_connect/_poll_raw`` 的真实传输，解析逻辑不用改。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from .base import MarketDataProvider, ProviderCapabilities, ProviderError
from .models import (
    DataQuality,
    EventAuthority,
    EventType,
    HaltEvent,
    LULDState,
    MWCBState,
    MarketEvent,
    OrderBookEvent,
    QualityFlag,
    QuoteEvent,
    SessionEvent,
    SessionPhase,
    TradeEvent,
)
from . import databento_schema as ds
from .normalize import normalize_exchange, normalize_symbol, parse_timestamp


# ---------------------------------------------------------------------- #
class RecordedProvider(MarketDataProvider):
    """从 JSONL fixture 回放原始消息，支持注入重复/乱序/缺口/断线。"""

    name = "recorded"

    def __init__(self, fixture: Optional[Path | str] = None,
                 config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.fixture = Path(fixture) if fixture is not None else None
        self._records: List[Dict[str, Any]] = []
        self._cursor = 0
        self.batch_size = int(self.config.get("batch_size", 50))
        # 故障注入
        self.duplicate_every = int(self.config.get("duplicate_every", 0))
        self.swap_pairs = bool(self.config.get("swap_pairs", False))
        self.drop_indexes = set(self.config.get("drop_indexes", []) or [])
        self.down_at = set(self.config.get("down_at", []) or [])
        self._tick = 0
        #: 录制回放的事件时钟（取已投递事件的最大 event_time）
        self._replay_now: Optional[datetime] = None

    def _quality_now(self, ev: MarketEvent) -> Optional[datetime]:
        if not self.config.get("replay_clock", True):
            return None
        if self._replay_now is None or ev.event_time > self._replay_now:
            self._replay_now = ev.event_time
        return self._replay_now

    def _connect(self) -> None:
        if self.fixture is None:
            raise ProviderError("未指定 fixture（离线适配器必须提供样本文件）",
                                "FIXTURE_REQUIRED")
        if not self.fixture.exists():
            raise ProviderError(f"fixture 不存在：{self.fixture}", "FIXTURE_MISSING")
        recs: List[Dict[str, Any]] = []
        for line in self.fixture.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            recs.append(json.loads(line))
        if not recs:
            raise ProviderError("fixture 为空", "FIXTURE_EMPTY")
        self._records = recs
        self._cursor = 0
        self._tick = 0

    def _close(self) -> None:
        self._cursor = 0

    def _subscribe_raw(self, symbols: List[str]) -> None:
        return None

    def _poll_raw(self) -> List[Any]:
        if self._tick in self.down_at:
            self._tick += 1
            raise ProviderError("模拟断线", "PROVIDER_DOWN")
        out: List[Dict[str, Any]] = []
        end = min(self._cursor + self.batch_size, len(self._records))
        for idx in range(self._cursor, end):
            if idx in self.drop_indexes:
                continue
            rec = dict(self._records[idx])
            rec["_index"] = idx
            out.append(rec)
            if self.duplicate_every and idx and idx % self.duplicate_every == 0:
                out.append(dict(rec))       # 重复投递
        if self.swap_pairs:
            for i in range(0, len(out) - 1, 2):
                out[i], out[i + 1] = out[i + 1], out[i]
        self._cursor = end
        self._tick += 1
        return out

    # ------------------------------------------------------------------ #
    def _normalize(self, item: Any) -> Optional[MarketEvent]:
        try:
            ev = self.parse(item)
        except Exception as exc:  # noqa: BLE001 - 单条坏数据不该中断整批
            # 但不许**静默**：字段形状对不上时如果只是返回 None，
            # 上层看到的是"没有数据"，而不是"解析失败"，故障会被藏起来。
            self.health.parse_errors += 1
            self.health.last_error = f"parse: {type(exc).__name__}: {exc}"
            self._reject("parse_exception", f"{type(exc).__name__}: {exc}")
            return None
        if ev is None:
            return None
        # 录制回放：**保留记录自己的 receive_time**。
        #
        # 早期实现无条件把它改成 event_time，理由是"保证确定性"。但确定性
        # 并不需要抹掉字段：样本里的 ts_recv 本身就是固定值，重复运行当然一致。
        # 抹掉之后，"供应商延迟"这类信息就永久丢失了 —— 而 ts_recv - ts_event
        # 正是判断数据延迟与乱序的唯一依据。
        if not self.config.get("replay_clock", True):
            ev.receive_time = datetime.now(timezone.utc)
        if not ev.provider:
            ev.provider = self.name
        return ev

    def parse(self, item: Dict[str, Any]) -> Optional[MarketEvent]:  # pragma: no cover
        raise NotImplementedError


# ---------------------------------------------------------------------- #
# Massive（原 Polygon.io）
# ---------------------------------------------------------------------- #
class MassiveProvider(RecordedProvider):
    """字段依据：massive.com 文档 ``/docs/websocket/stocks/luld`` 与 trades/quotes。

    LULD 消息：``{"ev":"LULD","T":"MSFT","h":492.99,"l":446.04,"i":[15],"z":3,
    "t":1764086430905642800,"q":5925769}``
    * ``t`` 为**纳秒** epoch（官方 blog 示例）；文档另处写 "Unix MS" → 由配置
      ``time_unit`` 显式指定，默认 ``ns`` 并在适配器里记录该歧义。
    * ``i`` 中 17=暂停 / 18=恢复，**仅 Nasdaq 上市证券**。
    * ``z`` 磁带：1=NYSE, 2=AMEX, 3=Nasdaq。
    """

    name = "massive"

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=self.name, us_full_market=True, sip_consolidated=True,
            exchanges=("nyse", "nasdaq", "amex", "arca", "cboe_bzx"),
            realtime=True, historical=True, history_years=20, backfill=True,
            premarket=True, regular=True, afterhours=True, overnight=True,
            trades=True, quotes=True, order_book=False,
            halt_events=True, halt_events_native=False,
            halt_events_nasdaq_listed_only=True,
            luld_events=True, luld_events_native=False,
            mwcb_events=False, mwcb_events_native=False,
            exchange_field=True, sequence_field=True, source_event_id=False,
            requires_exchange_license=True,
            notes="LULD 通道含价格带与 17/18 指标；指标仅 Nasdaq 上市；非交易所原生")

    TAPE = {1: "nyse", 2: "amex", 3: "nasdaq"}

    def parse(self, item: Dict[str, Any]) -> Optional[MarketEvent]:
        ev_type = str(item.get("ev", "")).upper()
        sym = normalize_symbol(item.get("T") or item.get("sym"))
        if not sym:
            return None
        unit = str(self.config.get("time_unit", "ns"))
        ts, err = parse_timestamp(item.get("t"), unit)
        if ts is None:
            return None
        tape = item.get("z")
        exchange = self.TAPE.get(int(tape), "unknown") if tape is not None else "unknown"
        q = DataQuality()
        if err:
            q.add(QualityFlag.NAIVE_TIME, err)
        seq = int(item.get("q") or 0)

        base = dict(provider=self.name, symbol=sym, exchange=exchange,
                    event_time=ts, receive_time=ts, sequence=seq,
                    raw_time=str(item.get("t")), raw_type=ev_type, quality=q)

        if ev_type == "LULD":
            ind = item.get("i") or []
            ind_s = ",".join(str(x) for x in ind) if isinstance(ind, list) else str(ind)
            return LULDState(upper=float(item.get("h") or 0),
                             lower=float(item.get("l") or 0),
                             indicator=ind_s,
                             paused=("17" in [str(x) for x in ind]),
                             resumed=("18" in [str(x) for x in ind]),
                             authority=EventAuthority.LULD_OFFICIAL, **base)
        if ev_type in ("T", "XT"):
            return TradeEvent(price=float(item.get("p") or 0),
                              size=float(item.get("s") or 0),
                              conditions=[str(c) for c in (item.get("c") or [])],
                              authority=EventAuthority.VENDOR_QUOTE, **base)
        if ev_type in ("Q",):
            return QuoteEvent(bid=float(item.get("bp") or 0),
                              ask=float(item.get("ap") or 0),
                              bid_size=float(item.get("bs") or 0),
                              ask_size=float(item.get("as") or 0),
                              authority=EventAuthority.VENDOR_QUOTE, **base)
        return None


# ---------------------------------------------------------------------- #
# Alpaca
# ---------------------------------------------------------------------- #
class AlpacaProvider(RecordedProvider):
    """字段依据：alpaca.markets/docs/market-data 的实时流（trade/quote）。

    注意：Basic（免费）只有 IEX；Algo Trader Plus 才是 SIP 全市场。
    本适配器不声明任何官方停复牌事件能力（文档未提供）。
    """

    name = "alpaca"

    #: Alpaca 用单字母交易所代码；**不能**套用全局别名表
    #: （例如 ``N`` 在 Alpaca 是 NYSE，而通用表里 n 曾误映射到 nasdaq）
    EXCHANGES = {"A": "amex", "B": "cboe_bzx", "N": "nyse", "Q": "nasdaq",
                 "V": "iex", "P": "arca", "M": "cboe_edgx"}

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=self.name, us_full_market=True, sip_consolidated=True,
            exchanges=("iex", "nyse", "nasdaq", "amex", "arca"),
            realtime=True, historical=True, history_years=10, backfill=True,
            premarket=True, regular=True, afterhours=True, overnight=True,
            trades=True, quotes=True, order_book=False,
            halt_events=False, halt_events_native=False,
            luld_events=False, luld_events_native=False,
            mwcb_events=False, mwcb_events_native=False,
            exchange_field=True, sequence_field=False, source_event_id=False,
            requires_exchange_license=False,
            notes="SIP 仅 Algo Trader Plus；无停复牌/LULD 事件通道")

    def parse(self, item: Dict[str, Any]) -> Optional[MarketEvent]:
        t = str(item.get("T", "")).lower()
        sym = normalize_symbol(item.get("S"))
        if not sym:
            return None
        ts, err = parse_timestamp(item.get("t"), "iso")
        if ts is None:
            return None
        q = DataQuality()
        if err:
            q.add(QualityFlag.NAIVE_TIME, err)
        raw_ex = str(item.get("x") or item.get("bx") or item.get("ax") or "")
        ex = self.EXCHANGES.get(raw_ex.upper()) or normalize_exchange(raw_ex)
        base = dict(provider=self.name, symbol=sym, exchange=ex,
                    event_time=ts, receive_time=ts,
                    sequence=int(item.get("i") or 0),
                    raw_time=str(item.get("t")), raw_type=t, quality=q)
        if t == "t":
            return TradeEvent(price=float(item.get("p") or 0),
                              size=float(item.get("s") or 0),
                              authority=EventAuthority.VENDOR_QUOTE, **base)
        if t == "q":
            return QuoteEvent(bid=float(item.get("bp") or 0),
                              ask=float(item.get("ap") or 0),
                              bid_size=float(item.get("bs") or 0),
                              ask_size=float(item.get("as") or 0),
                              authority=EventAuthority.VENDOR_QUOTE, **base)
        return None


# ---------------------------------------------------------------------- #
# Databento
# ---------------------------------------------------------------------- #
class DatabentoProvider(RecordedProvider):
    """Databento 历史/实时行情适配器。

    字段语义**逐条对齐官方文档与官方客户端**（见 ``databento_schema.py``
    顶部的来源清单），三个关键事实与"想当然"的写法不同：

    1. 记录类型字段叫 ``rtype`` 且是**整数**（status=18、trades=0），
       不是 ``type="status"`` 这样的字符串；
    2. ``status.action`` 是 **uint16 整数枚举**（HALT=8、TRADING=7），
       不是 ``"H"`` 这样的字符；``trades.action`` 才是字符且恒为 ``"T"``；
    3. ``status`` 记录**没有** ``sequence``，两条 schema 都**没有** ``symbol``
       —— 标的是用 ``instrument_id`` 表示，必须由 symbology 映射还原。

    第 3 条是最容易踩的坑：按字符串 ``type`` 解析、按 ``symbol`` 取标的，
    真实数据会 100% 解析失败且看不出原因。因此本实现：
    ``rtype`` 缺失时才退回 ``type``/``schema`` 字符串；``symbol`` 缺失时
    查 ``symbology`` 映射；两者都没有则**明确计为 MISSING_SYMBOLOGY 丢弃**，
    绝不猜。

    仍然**没有消息签名** —— ``status`` 是交易所原生状态，但不构成密码学
    可信的"官方恢复"依据，权限判定见 ``authority.py``。
    """

    name = "databento"

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=self.name, us_full_market=True, sip_consolidated=False,
            exchanges=("nasdaq", "nyse", "nyse_american", "nyse_arca",
                       "cboe_bzx", "cboe_byx", "cboe_edga", "cboe_edgx",
                       "iex", "memx", "nasdaq_psx", "ltse"),
            realtime=True, historical=True, history_years=5, backfill=True,
            premarket=True, regular=True, afterhours=True, overnight=False,
            trades=True, quotes=True, order_book=True, order_book_depth=10,
            halt_events=True, halt_events_native=True,
            # status.action 里有 PAUSE + reason=LULD_PAUSE，
            # 但**没有**价格带上下限 —— 不能据此重建 LULD 带宽。
            luld_events=False, luld_events_native=False,
            mwcb_events=False, mwcb_events_native=False,
            exchange_field=True,
            # 只有 trades/mbp 带 sequence；status 不带。声明为 True 会让
            # 上层误以为 status 也受序号保护。
            sequence_field=False,
            source_event_id=False,
            requires_exchange_license=True,
            notes="交易所直连（XNAS.ITCH/XNYS.PILLAR 等）；status 为原生状态；"
                  "无签名；status 无 sequence；标的需 symbology 映射")

    # ------------------------------------------------------------------ #
    def __init__(self, fixture: Optional[Path | str] = None,
                 config: Optional[Dict[str, Any]] = None):
        super().__init__(fixture, config)
        #: instrument_id → 原始符号。默认从 config 读，也支持从
        #: Databento 的 metadata.symbol_map 导出后直接塞进来。
        raw_map = self.config.get("symbology") or {}
        self.symbology: Dict[int, str] = {}
        for k, v in dict(raw_map).items():
            try:
                self.symbology[int(k)] = str(v)
            except (TypeError, ValueError):
                continue
        #: 记录最近一次 price 的标度，便于报告里核对
        self.price_scale_seen: str = ""
        #: 按 action 归类的"非停牌状态"计数（正常现象，不是丢弃）
        self.ignored_status: Dict[str, int] = {}

    # ------------------------------------------------------------------ #
    def _rtype_of(self, item: Dict[str, Any]) -> Optional[int]:
        """取记录类型。``rtype`` 优先，其次 ``type``/``schema`` 字符串。"""
        rt = item.get("rtype")
        if isinstance(rt, bool):
            return None
        if isinstance(rt, (int, float)):
            return int(rt)
        if isinstance(rt, str) and rt.strip().isdigit():
            return int(rt.strip())
        for key in ("type", "schema"):
            name = str(item.get(key) or "").strip().lower()
            if name and name in ds.SCHEMA_RTYPE:
                return ds.SCHEMA_RTYPE[name]
        return None

    def _symbol_of(self, item: Dict[str, Any]) -> Optional[str]:
        """还原标的。``symbol`` 优先，其次 symbology 映射。不猜。"""
        raw = item.get("symbol")
        if raw:
            sym = normalize_symbol(raw)
            if sym:
                return sym
        iid = item.get("instrument_id")
        if iid is not None:
            try:
                key = int(iid)
            except (TypeError, ValueError):
                key = None
            if key is not None:
                mapped = self.symbology.get(key)
                if mapped:
                    sym = normalize_symbol(mapped)
                    if sym:
                        return sym
                self._reject("missing_symbology",
                             f"instrument_id={key} 不在 symbology 映射里")
                return None
        self._reject("missing_symbol", f"既无 symbol 也无 instrument_id：{item!r}")
        return None

    def _times_of(self, item: Dict[str, Any]) -> Optional[tuple]:
        """取 ``(ts_event, ts_recv, 时区告警)``。官方单位：uint64 纳秒。

        ``ts_recv`` 缺失时退回 ``ts_event``（并记录一次，便于发现字段缺失）。
        第三个返回值把"缺少时区"这类告警**带回调用方**去加质量标记 ——
        早期实现把 err 直接丢掉了，导致 naive 时间戳被静默当成 UTC。
        """
        ev_raw = item.get("ts_event")
        ev, err = parse_timestamp(ev_raw, "ns")
        if ev is None:
            self._reject("bad_ts_event", f"ts_event={ev_raw!r} {err}")
            return None
        tz_note = err or ""
        rec_raw = item.get("ts_recv")
        if rec_raw is None:
            rec = ev
            self._reject("missing_ts_recv", "缺少 ts_recv，已用 ts_event 兜底")
        else:
            rec, err2 = parse_timestamp(rec_raw, "ns")
            if rec is None:
                self._reject("bad_ts_recv", f"ts_recv={rec_raw!r} {err2}")
                rec = ev
            elif err2 and not tz_note:
                tz_note = err2
        return ev, rec, tz_note

    # ------------------------------------------------------------------ #
    def parse(self, item: Dict[str, Any]) -> Optional[MarketEvent]:
        rtype = self._rtype_of(item)
        if rtype is None:
            self._reject("unknown_schema",
                         f"无法识别 rtype：keys={sorted(item)[:12]}")
            return None

        sym = self._symbol_of(item)
        if not sym:
            return None

        times = self._times_of(item)
        if times is None:
            return None
        ts_event, ts_recv, tz_note = times

        venue, consolidated = ds.venue_of(item.get("publisher_id"))
        if venue == "unknown":
            alt = item.get("exchange")
            if alt:
                venue = normalize_exchange(alt)
            if venue == "unknown":
                self._reject("unknown_publisher",
                             f"publisher_id={item.get('publisher_id')!r} 无映射")
        if consolidated:
            # 合并行情发布者的"交易所"字段不是单一交易所，
            # 用它去驱动某交易所的停牌状态会把 A 所的停牌作用到全市场。
            self._reject("consolidated_publisher",
                         f"publisher_id={item.get('publisher_id')} 为合并发布者")

        q = DataQuality()
        if tz_note:
            # 缺少时区 / 解析告警必须留下痕迹。按 UTC 占位可以，但不能假装没这回事。
            q.add(QualityFlag.NAIVE_TIME, tz_note)
        base = dict(
            provider=self.name, symbol=sym, exchange=venue,
            event_time=ts_event, receive_time=ts_recv,
            sequence=0, source_event_id="",
            raw_time=str(item.get("ts_event")), raw_type=ds.RTYPE_NAME.get(rtype, str(rtype)),
            quality=q,
        )

        # ---- status（rtype 18）---------------------------------------- #
        if rtype == ds.RTYPE_STATUS:
            action_name, action_ok = ds.status_action_code(item.get("action"))
            reason_name, reason_ok = ds.status_reason_code(item.get("reason"))
            event_name, _ = ds.trading_event_code(item.get("trading_event"))
            if not action_ok:
                # 未知 action 无法判断方向。**不能丢弃**：
                # 丢掉意味着沿用上一次状态，若上次是"可交易"就等于放行。
                # 按"不可交易"处理 —— 保守方向唯一正确的选择。
                q.add(QualityFlag.UNKNOWN_ENUM,
                      f"未知 status.action={item.get('action')!r}，按不可交易处理")
                self._reject("unknown_status_action_forced_halt",
                             f"action={item.get('action')!r}")
                return HaltEvent(
                    halted=True, reason="UNKNOWN_ACTION",
                    halt_code=f"UNKNOWN_{item.get('action')!r}",
                    expected_resume=None,
                    authority=EventAuthority.EXCHANGE_HALT, **base)

            # 归一成官方整数（8/9/10 才是停牌）。不能直接 int()：
            # 字符形态会得到 None，使所有 "in HALTING_ACTIONS" 判断恒假。
            code = ds.status_action_int(item.get("action"))
            is_trading = _tristate(item.get("is_trading"))
            is_quoting = _tristate(item.get("is_quoting"))
            ssr = _tristate(item.get("is_short_sell_restricted"))

            if code in ds.HALTING_ACTIONS:
                halted = True
            elif code in ds.TRADING_ACTIONS:
                halted = False
            elif code in ds.AMBIGUOUS_ACTIONS:
                # 官方定义无法区分"收盘"与"停牌"，保守判为不可交易并标记
                halted = True
                q.add(QualityFlag.AMBIGUOUS_STATUS,
                      "NOT_AVAILABLE_FOR_TRADING 无法区分收盘/停牌")
            else:
                # PRE_OPEN/QUOTING/CROSS/CLOSE/SSR_CHANGE/NONE 等不是停牌事件，
                # 时段由 ExchangeClock 负责，这里不制造假停牌。
                # 这**不是**数据丢弃 —— 它们本来就不该变成 HaltEvent，
                # 因此单独计数，不污染 dropped/rejected 语义。
                self.ignored_status[action_name] = \
                    self.ignored_status.get(action_name, 0) + 1
                self.skipped += 1
                return None

            if not reason_ok:
                # reason 不认识不影响"是否停牌"这个判断本身，
                # 因此只记录、不阻断（阻断会把已知的 HALT 事件丢掉）。
                q.add(QualityFlag.UNKNOWN_ENUM,
                      f"未知 status.reason={item.get('reason')!r}")
            if int(item.get("reason") or 0) in ds.MWCB_REASONS:
                # 市场级熔断：不能当成单只证券的普通停牌
                q.add(QualityFlag.AMBIGUOUS_STATUS, f"市场级熔断 {reason_name}")

            return HaltEvent(
                halted=halted, reason=reason_name, halt_code=action_name,
                expected_resume=None,
                is_trading=is_trading, is_quoting=is_quoting,
                ssr_restricted=ssr, trading_event=event_name,
                # 供应商 status 只到 A 类（普通行情里的状态字段）；
                # 是否能当"交易所权威事件"由 AuthorityRouter 决定。
                authority=EventAuthority.EXCHANGE_HALT, **base)

        # ---- trades（rtype 0）----------------------------------------- #
        if rtype == ds.RTYPE_TRADES:
            px, scale = ds.price_to_float(item.get("price"))
            self.price_scale_seen = scale
            if scale in ("invalid", "undef"):
                q.add(QualityFlag.PRICE_UNDEFINED, f"price={item.get('price')!r} ({scale})")
                self._reject("price_undefined", f"price={item.get('price')!r}")
                return None
            size = _num(item.get("size"))
            seq = item.get("sequence")
            seq_i = int(seq) if isinstance(seq, (int, float)) and not isinstance(seq, bool) else 0
            side = str(item.get("side") or "").upper()
            return TradeEvent(
                price=px, size=size, side=side, price_scale=scale,
                authority=EventAuthority.VENDOR_QUOTE,
                **{**base, "sequence": seq_i})

        # ---- mbp-1（rtype 1）------------------------------------------ #
        if rtype == ds.RTYPE_MBP_1:
            level = (item.get("levels") or [{}])[0] if isinstance(item.get("levels"), list) \
                else {}
            bid, s1 = ds.price_to_float(level.get("bid_px", item.get("bid_px")))
            ask, s2 = ds.price_to_float(level.get("ask_px", item.get("ask_px")))
            self.price_scale_seen = s1 or s2
            if bid <= 0 or ask <= 0:
                q.add(QualityFlag.PRICE_UNDEFINED, f"bid={bid} ask={ask}")
                self._reject("price_undefined", f"bid={bid} ask={ask}")
                return None
            seq = item.get("sequence")
            seq_i = int(seq) if isinstance(seq, (int, float)) and not isinstance(seq, bool) else 0
            return QuoteEvent(
                bid=bid, ask=ask,
                bid_size=_num(level.get("bid_sz", item.get("bid_sz"))),
                ask_size=_num(level.get("ask_sz", item.get("ask_sz"))),
                authority=EventAuthority.VENDOR_QUOTE,
                **{**base, "sequence": seq_i})

        # ---- 认得但不解析 --------------------------------------------- #
        self._reject(f"schema_unsupported:{ds.RTYPE_NAME.get(rtype, rtype)}",
                     f"rtype={rtype}")
        return None

    # ------------------------------------------------------------------ #
    def snapshot(self) -> Dict[str, Any]:
        out = super().snapshot()
        out["databento"] = {
            "symbology_size": len(self.symbology),
            "price_scale_seen": self.price_scale_seen,
            "ignored_status": dict(self.ignored_status),
            "rejected": self.rejections(),
        }
        return out


def _num(value: Any) -> float:
    if isinstance(value, bool) or value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _tristate(value: Any) -> Optional[bool]:
    """官方三态：``Y`` / ``N`` / ``~``（未知）。不把 ``~`` 当成 False。"""
    if value is None:
        return None
    return ds.TRI_STATE.get(str(value).strip().upper())


# ---------------------------------------------------------------------- #
# 交易所原生（本阶段只能以录制样本验证）
# ---------------------------------------------------------------------- #
class NasdaqItchProvider(RecordedProvider):
    """Nasdaq TotalView Trading Action 消息（'H' halt / 'Q' quote resume / 'T' trade resume）。"""

    name = "nasdaq_itch"

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=self.name, us_full_market=False, sip_consolidated=False,
            exchanges=("nasdaq",), realtime=True, historical=True,
            history_years=5, backfill=True,
            premarket=True, regular=True, afterhours=True, overnight=False,
            trades=True, quotes=True, order_book=True, order_book_depth=10,
            halt_events=True, halt_events_native=True,
            luld_events=False, mwcb_events=False,
            exchange_field=True, sequence_field=True, source_event_id=True,
            requires_exchange_license=True,
            notes="需要 Nasdaq 直连授权；本阶段仅离线样本")

    def parse(self, item: Dict[str, Any]) -> Optional[MarketEvent]:
        sym = normalize_symbol(item.get("symbol"))
        ts, err = parse_timestamp(item.get("timestamp"), "ns")
        if not sym or ts is None:
            return None
        q = DataQuality()
        if err:
            q.add(QualityFlag.NAIVE_TIME, err)
        action = str(item.get("action", "")).upper()
        if action not in ("H", "Q", "T"):
            return None
        return HaltEvent(halted=(action == "H"),
                         reason=str(item.get("reason") or ""),
                         halt_code=action,
                         authority=EventAuthority.EXCHANGE_HALT,
                         provider=self.name, symbol=sym, exchange="nasdaq",
                         event_time=ts, receive_time=ts,
                         sequence=int(item.get("sequence") or 0),
                         source_event_id=str(item.get("id") or ""),
                         raw_time=str(item.get("timestamp")),
                         raw_type="trading_action", quality=q)
