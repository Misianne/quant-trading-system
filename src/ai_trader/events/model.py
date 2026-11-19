"""事件记录的时间语义与回放规则（**不产生任何交易信号**）。

为什么要单独建模时间
--------------------
一条新闻至少有五个"时间"，混用任意两个都会造成未来信息泄漏：

======================  ==================================================
字段                     含义
======================  ==================================================
``event_time``          事情**发生**的时间（会议结束、袭击发生、政策签署）
``first_publication_time`` 该事件**首次对公众可见**的时间（本系统能确认的最早）
``source_published_at`` 来源页面自己标注的发布时间
``retrieved_at``        本系统**抓取到**它的时间
``revised_at``          来源后续修订该条目的时间
======================  ==================================================

回放规则
--------
* 一条事件可以有**多个版本**（首次通报 → 更正 → 补充）。每个版本独立存文本、URL、
  SHA256 与时间；**永不覆盖**。
* :meth:`EventRecord.visible_at` 只返回 ``available_at <= as_of`` 的最新版本。
* 若 ``source_published_at`` 缺失，只能用 ``retrieved_at`` 兜底，并把
  ``time_precision`` 标为 ``retrieved``（意味着"我们拿到它的时间"，不是"它公开的时间"）。
* 两者都缺失 → ``time_precision='unknown'``，**任何时点都不可见**（宁可不用，也不提前用）。

关于"市场预期"
--------------
本模块**没有**一致预期数据源。因此 :func:`explain_event` 在缺少 ``expectation`` 时
强制输出"市场预期未知"，且 ``can_assess_surprise=False``；它不会用上月值、趋势值
或任何模型输出冒充市场一致预期。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# ---- 时间精度 ----
EXACT = "exact"            # 有精确到分钟的来源发布时间
DATE_ONLY = "date"         # 只到日
RETRIEVED = "retrieved"    # 只能确认"我们抓到的时刻"
UNKNOWN = "unknown"        # 无法确认，禁止使用

# ---- 可信等级（数值越大越可信）----
TRUST_LEVELS = {"official": 3, "official_secondhand": 2, "wire": 2, "media": 1}

# ---- 影响渠道（枚举，不允许自由发挥）----
CHANNELS = {
    "rates": "利率与贴现率",
    "inflation": "通胀与成本",
    "growth": "增长与盈利",
    "liquidity": "流动性与信用",
    "risk_premium": "风险溢价与波动率",
    "regulation": "监管与合规成本",
    "geopolitics": "地缘政治与供应链",
    "fiscal": "财政与主权信用",
}


class EventError(RuntimeError):
    pass


def _parse(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise EventError(f"时间戳缺少时区，拒绝猜测: {ts!r}")
    return dt.astimezone(timezone.utc)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class EventVersion:
    """一个版本。同一事件的多版本按 ``version`` 递增，**不覆盖**。"""
    version: int
    text: str
    source_url: str
    source_published_at: Optional[str] = None
    retrieved_at: Optional[str] = None
    revised_at: Optional[str] = None
    source_hash: Optional[str] = None
    note: str = ""

    def __post_init__(self) -> None:
        if self.source_hash is None:
            self.source_hash = sha256_text(self.text)

    @property
    def available_at(self) -> Optional[str]:
        """该版本最早可被外部看到的时点。**优先来源发布时间**，缺失才用抓取时间。"""
        return self.source_published_at or self.retrieved_at

    @property
    def time_precision(self) -> str:
        if self.source_published_at:
            return EXACT if "T" in self.source_published_at else DATE_ONLY
        if self.retrieved_at:
            return RETRIEVED
        return UNKNOWN

    def as_dict(self) -> Dict[str, Any]:
        return {"version": self.version, "text": self.text,
                "source_url": self.source_url,
                "source_published_at": self.source_published_at,
                "retrieved_at": self.retrieved_at,
                "revised_at": self.revised_at,
                "source_hash": self.source_hash,
                "available_at": self.available_at,
                "time_precision": self.time_precision, "note": self.note}


@dataclass
class EventRecord:
    event_id: str
    title: str
    category: str                        # monetary | inflation | labor | fiscal |
                                         # geopolitics | regulation | energy | systemic
    trust_level: str = "official"
    event_time: Optional[str] = None
    first_publication_time: Optional[str] = None
    channels: List[str] = field(default_factory=list)
    versions: List[EventVersion] = field(default_factory=list)
    #: 市场一致预期。**没有就留空**，不要填任何估计值。
    expectation: Optional[float] = None
    expectation_source: Optional[str] = None
    actual: Optional[float] = None
    actual_unit: str = ""

    # ------------------------------------------------------------------ #
    @property
    def trust_rank(self) -> int:
        return TRUST_LEVELS.get(self.trust_level, 0)

    def add_version(self, text: str, source_url: str,
                    source_published_at: Optional[str] = None,
                    retrieved_at: Optional[str] = None,
                    revised_at: Optional[str] = None, note: str = "") -> EventVersion:
        v = EventVersion(version=len(self.versions) + 1, text=text,
                         source_url=source_url,
                         source_published_at=source_published_at,
                         retrieved_at=retrieved_at, revised_at=revised_at, note=note)
        self.versions.append(v)
        if self.first_publication_time is None and v.available_at:
            self.first_publication_time = v.available_at
        return v

    def visible_at(self, as_of: str) -> Optional[EventVersion]:
        """回放：返回 ``as_of`` 时点已公开的最新版本；无法确认时间 → ``None``。"""
        cutoff = _parse(as_of)
        best = None
        for v in self.versions:
            at = v.available_at
            if at is None:
                continue                       # 时间不确定的版本不参与回放
            if _parse(at) <= cutoff:
                if best is None or v.version > best.version:
                    best = v
        return best

    def is_visible(self, as_of: str) -> bool:
        return self.visible_at(as_of) is not None

    def time_precision(self) -> str:
        v = self.versions[0] if self.versions else None
        return v.time_precision if v else UNKNOWN

    def as_dict(self) -> Dict[str, Any]:
        return {"event_id": self.event_id, "title": self.title,
                "category": self.category, "trust_level": self.trust_level,
                "event_time": self.event_time,
                "first_publication_time": self.first_publication_time,
                "time_precision": self.time_precision(),
                "channels": self.channels,
                "expectation": self.expectation,
                "expectation_source": self.expectation_source,
                "actual": self.actual, "actual_unit": self.actual_unit,
                "versions": [v.as_dict() for v in self.versions]}


# --------------------------------------------------------------------------- #
# 十段式事件解释
# --------------------------------------------------------------------------- #
def explain_event(ev: EventRecord, as_of: str) -> Dict[str, Any]:
    """生成事件解释。**没有预期数据时强制写「市场预期未知」。**

    返回结构与宏观层一致：每段带 ``status``（fact / inferred / unverifiable）。
    """
    v = ev.visible_at(as_of)
    if v is None:
        return {"event_id": ev.event_id, "as_of": as_of, "usable": False,
                "reason": ("该事件在此时点尚无已公开版本，或发布时间无法确认 → "
                           "禁止使用（宁可不用，也不提前用）"),
                "sections": []}

    has_exp = ev.expectation is not None and ev.actual is not None
    surprise = None
    if has_exp:
        surprise = ev.actual - ev.expectation

    def sec(key, title, status, content, missing=None):
        return {"key": key, "title": title, "status": status,
                "content": content, "missing": missing or []}

    sections = [
        sec("what", "发生了什么", "fact",
            [f"{ev.title}", f"类别：{ev.category}", v.text[:500]]),
        sec("evidence", "可靠来源与证据", "fact",
            [f"来源：{v.source_url}", f"文本哈希：{v.source_hash[:16]}…",
             f"可信等级：{ev.trust_level}（{ev.trust_rank}/3）",
             f"版本：v{v.version}" + (f"，后续修订于 {v.revised_at}" if v.revised_at else "")]),
        sec("first_pub", "首次公开时间", "fact" if v.time_precision in (EXACT, DATE_ONLY) else "unverifiable",
            [f"{v.available_at}", f"时间精度：{v.time_precision}"],
            [] if v.time_precision in (EXACT, DATE_ONLY)
            else ["来源未标注发布时间，仅能确认我们抓取到的时刻"]),
        sec("expectation", "市场原本预期", "fact" if has_exp else "unverifiable",
            [f"{ev.expectation} {ev.actual_unit}（来源：{ev.expectation_source}）"]
            if has_exp else ["**市场预期未知** —— 本库无一致预期数据源，不做估计、不冒充"],
            [] if has_exp else ["发布前的一致预期（调查中位数）"]),
        sec("surprise", "实际结果与预期差", "fact" if has_exp else "unverifiable",
            [f"实际 {ev.actual} {ev.actual_unit}，预期 {ev.expectation}，"
             f"差值 {surprise:+.4g} {ev.actual_unit}"] if has_exp
            else ["**无法计算** —— 没有预期就没有预期差；"
                  "用上月值/趋势值/模型值代替都是伪造"]),
        sec("channels", "直接影响渠道", "fact" if ev.channels else "unverifiable",
            [f"{c}：{CHANNELS.get(c, c)}" for c in ev.channels]
            or ["未标注影响渠道"], [] if ev.channels else ["事件到价格的传导渠道"]),
        sec("impact", "对纳指 100 的潜在影响", "unverifiable",
            ["本层不提供方向判断：阶段四已证明宏观状态无显著增量，"
             "事件层更缺少一致预期，任何方向陈述都无依据"],
            ["预期差数据", "事件窗口内其它冲击的控制"]),
        sec("priced_in", "可能已经计价的部分", "unverifiable",
            ["无法判断"], ["发布前的仓位/资金流", "期权隐含波动率", "一致预期"]),
        sec("counter", "相反证据和不确定性", "fact",
            ["同一天可能存在多个事件，单事件归因不成立",
             "事件方向与实际市场反应可能相反（本轮实证里 FOMC 加息日当日均值为正）"]),
        sec("confidence", "置信度与判断失效条件", "fact",
            ["对「发生了什么」：高（有官方原文与哈希）",
             "对「会怎么影响纳指」：低或无（缺少预期差）",
             "失效条件：若来源修订文本且修订与本条结论冲突，需以新版本重做"]),
    ]
    return {"event_id": ev.event_id, "as_of": as_of, "usable": True,
            "can_assess_surprise": bool(has_exp and surprise is not None),
            "surprise": surprise, "sections": sections}
