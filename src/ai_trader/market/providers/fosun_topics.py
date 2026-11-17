"""官方 WebSocket 主题 → 交易时段 的证据登记表。

为什么单独一个文件
------------------
官方 SDK（`fsopenapi/api/market_push.py`）里有一批订阅主题，但**只有注释**说明
它们覆盖哪个时段，没有正式的协议文档、没有字段说明、没有时段边界。

所以本模块的定位是"证据台账"而不是"配置"：每个主题都带
``evidence`` 等级与一句话出处，任何结论都能追溯到具体来源。

证据等级
--------
``SDK_COMMENT``
    仅来自官方 SDK 源码里的中文注释（例如"pqt pmin ptk 支持美股盘前盘后的股票消息"）。
    属于厂商自述，但**不是**协议规范。
``UNKNOWN``
    名字出现在 SDK 里，但注释没有说明时段/语义。

**边界时刻一律 UNKNOWN**：官方从未给出 04:00 / 09:30 / 16:00 / 20:00 这类定义
（见 `fosun_sessions.py` 的 ``SPEC_SOURCE_UNVERIFIED``）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple

SDK_COMMENT = "SDK_COMMENT"
UNKNOWN = "UNKNOWN"

#: 时段的边界来源。与 fosun_sessions 保持一致：不是官方数据。
BOUNDARY_EVIDENCE = UNKNOWN


@dataclass(frozen=True)
class TopicSpec:
    """一个订阅主题的已知信息。"""

    name: str
    #: 依据官方注释，该主题**可能**覆盖的时段。空元组 = 注释里没提时段。
    sessions: Tuple[str, ...] = ()
    evidence: str = UNKNOWN
    #: 官方原文（或"无"）。引用而不是转述，便于核对。
    quote: str = ""
    #: 是否已知消息结构
    payload_schema: str = UNKNOWN

    def covers(self, phase: str) -> bool:
        return phase in self.sessions

    def as_dict(self) -> Dict[str, object]:
        return {"topic": self.name, "sessions": list(self.sessions),
                "evidence": self.evidence, "quote": self.quote,
                "payload_schema": self.payload_schema}


#: 官方 SDK 的订阅主题清单。
#:
#: 原文均出自 `fsopenapi/api/market_push.py` 的注释块，逐字引用。
TOPICS: Dict[str, TopicSpec] = {
    # --- 港美A 通用（注释未区分时段）---
    "qt": TopicSpec("qt", (), SDK_COMMENT,
                    "qt min tk 支持港美A的股票消息"),
    "min": TopicSpec("min", (), SDK_COMMENT,
                     "qt min tk 支持港美A的股票消息"),
    "tk": TopicSpec("tk", (), SDK_COMMENT,
                    "qt min tk 支持港美A的股票消息"),
    # --- 盘口：注释明确提到"支持美股盘前盘后" ---
    "ob": TopicSpec("ob", ("pre", "post"), SDK_COMMENT,
                    "ob 支持港美A的股票消息，支持美股盘前盘后"),
    # --- 经纪队列：盘前盘后 + 夜盘 ---
    "bq": TopicSpec("bq", ("pre", "post", "night"), SDK_COMMENT,
                    "bq 支持港美A的股票消息，支持美股盘前盘后，美股夜盘"),
    # --- 市场快照：注释只说"市场快照"，语义未文档化 ---
    "mkt": TopicSpec("mkt", (), SDK_COMMENT, "订阅市场快照"),
    # --- 美股盘前盘后专用 ---
    "pqt": TopicSpec("pqt", ("pre", "post"), SDK_COMMENT,
                     "pqt pmin ptk 支持美股盘前盘后的股票消息"),
    "pmin": TopicSpec("pmin", ("pre", "post"), SDK_COMMENT,
                      "pqt pmin ptk 支持美股盘前盘后的股票消息"),
    "ptk": TopicSpec("ptk", ("pre", "post"), SDK_COMMENT,
                     "pqt pmin ptk 支持美股盘前盘后的股票消息"),
    # --- 美股夜盘专用 ---
    "qt_nt": TopicSpec("qt_nt", ("night",), SDK_COMMENT,
                       "qt_nt min_nt tk_nt 支持美股夜盘的股票消息"),
    "min_nt": TopicSpec("min_nt", ("night",), SDK_COMMENT,
                        "qt_nt min_nt tk_nt 支持美股夜盘的股票消息"),
    "tk_nt": TopicSpec("tk_nt", ("night",), SDK_COMMENT,
                       "qt_nt min_nt tk_nt 支持美股夜盘的股票消息"),
    # --- 交易消息 ---
    "trade": TopicSpec("trade", (), SDK_COMMENT, "trade 支持股票的交易消息"),
}

#: 订阅报文的主题名就是这些字符串；未登记的主题不允许订阅。
KNOWN_TOPICS = tuple(sorted(TOPICS))

#: 心跳主题不是业务主题，单独处理。
HEARTBEAT_TOPIC = "hb"


def topics_for_session(phase: str, *, include_unclassified: bool = False) -> Tuple[str, ...]:
    """返回**注释声称**覆盖该时段的主题。

    ``include_unclassified=True`` 时把注释未提时段的主题（qt/min/tk/...）也带上——
    它们可能覆盖全部时段，但这只是可能性，不能当结论。
    """
    hits = [t for t, s in TOPICS.items() if s.covers(phase)]
    if include_unclassified:
        hits += [t for t, s in TOPICS.items() if not s.sessions]
    return tuple(sorted(set(hits)))


def unknown_payload_topics() -> Tuple[str, ...]:
    """消息结构未知的主题。目前是**全部**——从未收到过真实推送样本。"""
    return tuple(t for t, s in TOPICS.items() if s.payload_schema == UNKNOWN)


def evidence_table() -> Dict[str, Dict[str, object]]:
    return {t: s.as_dict() for t, s in TOPICS.items()}


def assert_known(topic: str) -> TopicSpec:
    """订阅前校验主题名。未登记 → 抛错，不静默接受陌生主题。"""
    spec = TOPICS.get(str(topic or "").strip())
    if spec is None:
        raise ValueError(f"未登记的订阅主题：{topic!r}（已知：{', '.join(KNOWN_TOPICS)}）")
    return spec
