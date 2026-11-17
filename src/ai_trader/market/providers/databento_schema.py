"""Databento 官方 schema 常量。

本模块**不是**猜出来的字段表，每一个常量都能对应到官方来源：

* ``https://databento.com/docs/schemas/status`` —— status 字段表与三组枚举
* ``https://databento.com/docs/schemas/trades`` —— trades 字段表与价格标度
* ``databento-dbn`` 0.73.0 的 ``_lib.pyi`` —— ``StatusMsg`` / ``TradeMsg`` 的
  字段顺序与每个枚举的整数值（用 ``bytes(msg)`` 差分实测确认了字节偏移）
* ``databento`` 0.90.0 的 ``common/publishers.py::Venue.from_int`` ——
  ``publisher_id`` → 交易所代码的 72 项映射

为什么把这些放成常量而不是散在解析代码里：解析逻辑一旦和官方枚举脱节，
最典型的后果是"halt 判断永远为假"，而系统看起来一切正常（没有异常、
也没有成交），这种静默失效在生产里极难发现。集中成表之后，
``tests/test_databento_schema.py`` 可以用官方的整数值逐条盯住。

**重要边界**：这些常量只描述"字段怎么读"。它们**不**赋予任何事件
交易所权威身份 —— 供应商在传输层没有消息签名，见
``market/providers/authority.py``。
"""
from __future__ import annotations

from typing import Dict, FrozenSet, Tuple

# ---------------------------------------------------------------------- #
# 1. rtype：记录类型哨兵
# ---------------------------------------------------------------------- #
#: status schema 的 rtype 恒为 18（官方字段表原文："Always 18"）
RTYPE_STATUS = 18
#: trades schema 的 rtype 恒为 0（官方字段表原文："Always 0"）
RTYPE_TRADES = 0
#: 其余常见 schema，仅用于"认得出来但不解析"时给出准确名字
RTYPE_MBP_1 = 1
RTYPE_MBP_10 = 10
RTYPE_MBO = 160
RTYPE_TBBO = 197
RTYPE_BBO_1S = 195
RTYPE_OHLCV_1S = 32

RTYPE_NAME: Dict[int, str] = {
    RTYPE_TRADES: "trades",
    RTYPE_MBP_1: "mbp-1",
    RTYPE_MBP_10: "mbp-10",
    RTYPE_STATUS: "status",
    RTYPE_OHLCV_1S: "ohlcv-1s",
    RTYPE_MBO: "mbo",
    RTYPE_BBO_1S: "bbo-1s",
    RTYPE_TBBO: "tbbo",
}

#: 名字 → rtype（兼容只给了字符串 ``type``/``schema`` 的输入）
SCHEMA_RTYPE: Dict[str, int] = {
    "trades": RTYPE_TRADES,
    "trade": RTYPE_TRADES,
    "status": RTYPE_STATUS,
    "mbp-1": RTYPE_MBP_1,
    "mbp1": RTYPE_MBP_1,
    "mbp-10": RTYPE_MBP_10,
    "mbp10": RTYPE_MBP_10,
    "mbo": RTYPE_MBO,
    "tbbo": RTYPE_TBBO,
    "bbo-1s": RTYPE_BBO_1S,
}

# ---------------------------------------------------------------------- #
# 2. status.action（uint16 整数枚举）
# ---------------------------------------------------------------------- #
STATUS_ACTION: Dict[int, str] = {
    0: "NONE",
    1: "PRE_OPEN",
    2: "PRE_CROSS",
    3: "QUOTING",
    4: "CROSS",
    5: "ROTATION",
    6: "NEW_PRICE_INDICATION",
    7: "TRADING",
    8: "HALT",
    9: "PAUSE",
    10: "SUSPEND",
    11: "PRE_CLOSE",
    12: "CLOSE",
    13: "POST_CLOSE",
    14: "SSR_CHANGE",
    15: "NOT_AVAILABLE_FOR_TRADING",
}

#: 这些 action 表示"交易所明确停止了交易"→ 可以映射为 halted=True。
#:
#: 为什么不含 ``NOT_AVAILABLE_FOR_TRADING``：官方定义是
#: "either trading has closed or been halted"，把"已收盘"当成停牌会造出
#: 一条永不恢复的假停牌记录。收盘由交易日历负责，不由停牌事件负责。
HALTING_ACTIONS: FrozenSet[int] = frozenset({8, 9, 10})

#: 这些 action 表示"交易已恢复/正在进行"→ halted=False。
#:
#: 注意：这只是**供应商数据层面的状态**，不构成恢复权限。真正的恢复
#: 必须经过 ``ResumeAuthority`` / ``AuthorityRouter`` 校验，见
#: ``market/luld_authority.py`` 与 ``market/providers/authority.py``。
TRADING_ACTIONS: FrozenSet[int] = frozenset({7})

#: 语义模糊、必须保守处理的 action。
#: ``NOT_AVAILABLE_FOR_TRADING`` 无法区分"收盘"与"停牌"，因此判为不可交易
#: 但打上 ``AMBIGUOUS_STATUS`` 标记，供上层决定是否升级为停牌。
AMBIGUOUS_ACTIONS: FrozenSet[int] = frozenset({15})

#: LULD 暂停在官方语义里就是 action=PAUSE(9) 且 reason=LULD_PAUSE(50)。
LULD_PAUSE_REASON = 50

# ---------------------------------------------------------------------- #
# 3. status.reason（uint16 整数枚举，取值不连续）
# ---------------------------------------------------------------------- #
STATUS_REASON: Dict[int, str] = {
    0: "NONE",
    1: "SCHEDULED",
    2: "SURVEILLANCE_INTERVENTION",
    3: "MARKET_EVENT",
    4: "INSTRUMENT_ACTIVATION",
    5: "INSTRUMENT_EXPIRATION",
    6: "RECOVERY_IN_PROCESS",
    10: "REGULATORY",
    11: "ADMINISTRATIVE",
    12: "NON_COMPLIANCE",
    13: "FILINGS_NOT_CURRENT",
    14: "SEC_TRADING_SUSPENSION",
    15: "NEW_ISSUE",
    16: "ISSUE_AVAILABLE",
    17: "ISSUES_REVIEWED",
    18: "FILING_REQS_SATISFIED",
    30: "NEWS_PENDING",
    31: "NEWS_RELEASED",
    32: "NEWS_AND_RESUMPTION_TIMES",
    33: "NEWS_NOT_FORTHCOMING",
    40: "ORDER_IMBALANCE",
    50: "LULD_PAUSE",
    60: "OPERATIONAL",
    70: "ADDITIONAL_INFORMATION_REQUESTED",
    80: "MERGER_EFFECTIVE",
    90: "ETF",
    100: "CORPORATE_ACTION",
    110: "NEW_SECURITY_OFFERING",
    120: "MARKET_WIDE_HALT_LEVEL1",
    121: "MARKET_WIDE_HALT_LEVEL2",
    122: "MARKET_WIDE_HALT_LEVEL3",
    123: "MARKET_WIDE_HALT_CARRYOVER",
    124: "MARKET_WIDE_HALT_RESUMPTION",
    130: "QUOTATION_NOT_AVAILABLE",
}

#: 这些 reason 是**市场级**熔断（MWCB），不是单只证券的停牌。
MWCB_REASONS: FrozenSet[int] = frozenset({120, 121, 122, 123, 124})

# ---------------------------------------------------------------------- #
# 4. status.trading_event
# ---------------------------------------------------------------------- #
TRADING_EVENT: Dict[int, str] = {
    0: "NONE",
    1: "NO_CANCEL",
    2: "CHANGE_TRADING_SESSION",
    3: "IMPLIED_MATCHING_ON",
    4: "IMPLIED_MATCHING_OFF",
}

# ---------------------------------------------------------------------- #
# 5. publisher_id → 交易所
# ---------------------------------------------------------------------- #
#: 来自官方客户端 ``Venue.from_int``（共 72 项）。只保留美股相关与
#: 期权/指数，未知 id 一律映射为 ``"unknown"``，**不猜**。
PUBLISHER_VENUE: Dict[int, str] = {
    1: "glbx", 2: "nasdaq", 3: "nasdaq_texas", 4: "nasdaq_psx",
    5: "cboe_bzx", 6: "cboe_byx", 7: "cboe_edga", 8: "cboe_edgx",
    9: "nyse", 10: "nyse_national", 11: "nyse_american", 12: "nyse_arca",
    13: "nyse_texas", 14: "iex", 15: "finra_trf_carteret",
    16: "finra_trf_chicago", 17: "finra_trf_nyse", 18: "memx",
    19: "miax_pearl", 20: "nyse_american_options", 21: "box_options",
    22: "cboe_options", 23: "miax_emerald", 24: "cboe_edgx_options",
    25: "nasdaq_gemx", 26: "nasdaq_ise", 27: "nasdaq_mrx",
    28: "miax_options", 29: "nyse_arca_options", 30: "opra",
    31: "miax_pearl_options", 32: "nasdaq_ndq", 33: "cboe_bxo",
    34: "cboe_c2", 35: "nasdaq_phlx", 36: "cboe_bats_options",
    37: "miax_sapphire", 38: "ice_ffe_uk", 39: "ice_endex",
    40: "db_eq", 41: "sphr", 42: "ltse", 43: "off_exchange",
    44: "aspen", 45: "asmt", 46: "aspi", 47: "equs", 48: "ice_ffe_us",
    49: "ice_ffe_eu_fin", 50: "eurex", 51: "eex", 52: "cfe",
    53: "blue_ocean", 54: "miax_to", 55: "iex_options",
    56: "main", 57: "txse", 58: "twentyfour_eq", 59: "cta",
    60: "utp", 61: "xadf", 62: "msci", 63: "ftse", 64: "inav",
    65: "morningstar", 66: "cccy", 67: "cgi", 68: "cdef",
    69: "tokyo", 70: "tokyo_topix", 71: "osaka", 72: "db_index",
}

#: 合并/汇总型 publisher：它们不是单个交易所，不能用于判"某交易所停牌"。
CONSOLIDATED_PUBLISHERS: FrozenSet[int] = frozenset({40, 47, 59, 60})

# ---------------------------------------------------------------------- #
# 6. 价格与三态
# ---------------------------------------------------------------------- #
#: 官方：``price`` 为 int64，"every 1 unit corresponds to 1e-9"
PRICE_SCALE = 1_000_000_000.0

#: 官方未定义价的哨兵值（INT64_MAX）。出现它必须丢弃，不能当成 0 或天价。
UNDEF_PRICE = 9_223_372_036_854_775_807

#: status 的三个 char 字段：'Y' / 'N' / '~'
TRI_STATE: Dict[str, bool] = {"Y": True, "N": False}

#: ``ts_recv`` 在 ``ts_event`` 之前多少纳秒属正常（超出则记录异常）
#: 官方 ``ts_in_delta`` 就是 ``ts_recv - ts_event``，量级通常在毫秒以内。
TS_IN_DELTA_MAX_NS = 5_000_000_000


def status_action_int(value: object) -> Optional[int]:
    """把 ``action`` 的任意真实写法归一成**官方整数编码**。

    这一步必须独立存在：``status_action_code`` 只回答"叫什么"，
    而判定"是否停牌"依赖整数（8/9/10）。早期实现直接在解析处
    ``int(item.get("action"))``，遇到字符形态就得到 ``None``，
    于是 ``None in HALTING_ACTIONS`` 恒假 —— 所有停牌被静默丢弃。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        code = int(value)
        return code if code in STATUS_ACTION else None
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) == 1:
        return _LEGACY_CHAR_ACTION.get(text.upper())
    upper = text.upper()
    for num, name in STATUS_ACTION.items():
        if name == upper:
            return num
    return None


def status_action_code(value: object) -> Tuple[str, bool]:
    """把 ``action`` 归一成 ``(代码名, 是否已知)``。

    接受三种真实存在的写法：

    * 整数（DBN 原始编码，也是官方字段表的写法）
    * 单字符（某些 JSON 转码路径会输出 ``"H"``）
    * 枚举名（``"HALT"``）

    未知值返回 ``("UNKNOWN_<raw>", False)``，由调用方 fail-closed。
    """
    if isinstance(value, bool):
        return "UNKNOWN_True", False
    if isinstance(value, (int, float)):
        code = int(value)
        return STATUS_ACTION.get(code, f"UNKNOWN_{code}"), code in STATUS_ACTION
    text = str(value or "").strip()
    if not text:
        return "", False
    # 单字符：按官方 "Status action variants" 的字符习惯映射
    if len(text) == 1:
        code = _LEGACY_CHAR_ACTION.get(text.upper())
        if code is not None:
            return STATUS_ACTION[code], True
        return f"UNKNOWN_{text}", False
    upper = text.upper()
    for num, name in STATUS_ACTION.items():
        if name == upper:
            return name, True
    return f"UNKNOWN_{upper}", False


#: 单字符写法 → 整数枚举。
#:
#: 只收录**能从官方字符习惯唯一确定**的几个：T=TRADING、H=HALT、P=PAUSE、
#: S=SUSPEND、Q=QUOTING。其余不猜（多个 action 共享首字母，猜错会把
#: "收盘"当成"停牌"）。
_LEGACY_CHAR_ACTION: Dict[str, int] = {
    "T": 7, "H": 8, "P": 9, "S": 10, "Q": 3,
}


def status_reason_code(value: object) -> Tuple[str, bool]:
    """把 ``reason`` 归一成 ``(代码名, 是否已知)``。"""
    if isinstance(value, bool):
        return "UNKNOWN_True", False
    if isinstance(value, (int, float)):
        code = int(value)
        return STATUS_REASON.get(code, f"UNKNOWN_{code}"), code in STATUS_REASON
    text = str(value or "").strip()
    if not text:
        return "", False
    upper = text.upper()
    for num, name in STATUS_REASON.items():
        if name == upper:
            return name, True
    return f"UNKNOWN_{upper}", False


def trading_event_code(value: object) -> Tuple[str, bool]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        code = int(value)
        return TRADING_EVENT.get(code, f"UNKNOWN_{code}"), code in TRADING_EVENT
    upper = str(value or "").strip().upper()
    if not upper:
        return "", False
    for num, name in TRADING_EVENT.items():
        if name == upper:
            return name, True
    return f"UNKNOWN_{upper}", False


def venue_of(publisher_id: object) -> Tuple[str, bool]:
    """``publisher_id`` → 交易所代码。

    返回 ``(venue, 是否合并型)``。未知 id 返回 ``("unknown", False)`` ——
    绝不猜成某个具体交易所，因为猜错会让 A 交易所的停牌作用到 B 交易所的标的。
    """
    try:
        pid = int(publisher_id)          # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "unknown", False
    venue = PUBLISHER_VENUE.get(pid, "unknown")
    return venue, pid in CONSOLIDATED_PUBLISHERS


def price_to_float(value: object) -> Tuple[float, str]:
    """把 ``price`` 归一为浮点美元价，并说明是按哪种标度解读的。

    规则只有一条且**不靠猜测**：类型决定标度。

    * ``int`` → DBN 定点数，除以 1e9（官方："1 unit = 1e-9"）
    * ``float`` → 已经被 ``pretty_px`` 转过，直接使用
    * 哨兵 ``UNDEF_PRICE`` → 返回 ``(0.0, "undef")``，调用方必须丢弃
    """
    if isinstance(value, bool) or value is None:
        return 0.0, "invalid"
    if isinstance(value, int):
        if value == UNDEF_PRICE:
            return 0.0, "undef"
        return value / PRICE_SCALE, "fixed_1e-9"
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return 0.0, "invalid"
        return float(value), "float"
    text = str(value).strip()
    if not text:
        return 0.0, "invalid"
    if text.lstrip("-").isdigit():
        return price_to_float(int(text))
    try:
        return float(text), "float"
    except ValueError:
        return 0.0, "invalid"
