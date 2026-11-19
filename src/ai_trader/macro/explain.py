"""把一次宏观判定翻译成**结构化解释**（10 段），并强制标注不确定性。

这个模块唯一的职责是"不许编"：

* 每一段都只允许引用两样东西——``Assessment`` 里的实测读数，或本库的元数据；
* 没有新闻/预期/持仓数据的段落，必须显式写成 ``status="unverifiable"`` 并说明
  **缺什么数据**才能回答，而不是给一个像样的猜测；
* 「市场可能已计价」这类判断，最多给"该状态已持续 N 个月"这种可核对的事实，
  其余一律标为推断（``status="inferred"``）并写明依据。

因此本模块**不调用任何模型**、不生成自然语言结论。它输出结构化字段，
由人（或上层模型）在字段约束内表述。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .regime import (COOLING, COOLING_RESILIENT, COOLING_WEAKENING,
                     CONTRADICTORY, Assessment, HEATING, HEATING_RESILIENT,
                     HEATING_WEAKENING, INSUFFICIENT, REGIME_LABEL, RESILIENT,
                     WEAKENING)

#: 段落状态：可核对的事实 / 由事实推出的判断 / 无法确认
FACT = "fact"
INFERRED = "inferred"
UNVERIFIABLE = "unverifiable"

STATE_BIAS = {
    COOLING_RESILIENT: "该状态在 5 年样本中出现 11 个月，20 日前瞻收益均值 +2.92%、"
                       "中位 +4.32%，但置换检验 p=0.256，**不显著**",
    COOLING_WEAKENING: "该状态在本库 5 年样本中**从未出现**（无衰退），不做方向性陈述",
    HEATING_RESILIENT: "该状态出现 10 个月，20 日均值 -1.85%（p=0.092），但**剔除 "
                       "2021-11~2022-04 这一段后均值变为 +1.34%**，符号反转，不可外推",
    HEATING_WEAKENING: "该状态在本库 5 年样本中**从未出现**（无滞胀），不做方向性陈述",
    # 矛盾状态最常见，它本身就不是方向性信息 —— 必须说清楚，否则等于假装有观点
    CONTRADICTORY: "该状态出现 38 个月，20 日均值 +1.57%，与全样本 +1.06% 无显著差异"
                     "（p=0.397）；「矛盾」意味着**不指向任何方向**，不是看空或看多",
    INSUFFICIENT: "数据不足，任何方向性陈述都缺乏依据",
}


@dataclass
class Section:
    key: str
    title: str
    status: str                       # fact | inferred | unverifiable
    content: List[str] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)   # 缺什么才能回答

    def as_dict(self) -> Dict[str, Any]:
        return {"key": self.key, "title": self.title, "status": self.status,
                "content": self.content, "sources": self.sources,
                "missing": self.missing}


def _fmt_pp(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{v:+.2f}pp"


def _src(a: Assessment, sid: str) -> str:
    period = a.data_dates.get(sid, "?")
    ver = a.versions.get(sid, "?")
    return f"{sid} {period}（vintage {ver}）"


def explain(a: Assessment) -> Dict[str, Any]:
    """生成 10 段结构。每段都带 status / sources / missing。"""
    infl = a.inflation
    grow = a.growth
    pol = a.policy

    # 1 当前宏观环境
    s1 = Section("environment", "当前宏观环境", FACT, [
        f"状态判定：{a.label}（{a.state}）",
        f"通胀方向：{infl.direction}（投票 {infl.score:+d}）；"
        f"增长方向：{grow.direction}（投票 {grow.score:+d}）",
        f"政策环境：{pol.get('stance')}，名义利率 {pol.get('level')}%，"
        f"实际利率 {_fmt_pp(pol.get('real_rate'))}",
    ], [_src(a, s) for s in ("CPIAUCSL", "PAYEMS", "GDP")])

    # 2 关键变化
    changes = []
    for v in infl.votes:
        if v.vote != 0:
            changes.append(f"[通胀] {v.name}: {v.value}")
    for v in grow.votes:
        if v.vote != 0:
            changes.append(f"[增长] {v.name}: {v.value}")
    s2 = Section("changes", "关键变化", FACT, changes or ["本期没有任何指标越过方向阈值"],
                 [_src(a, s) for s in a.data_dates])

    # 3 对纳指 100 的潜在影响
    bias = STATE_BIAS.get(a.state)
    s3 = Section("impact", "对纳指 100 的潜在影响", INFERRED if bias else UNVERIFIABLE,
                 [bias] if bias else ["该状态无法映射到历史参照"],
                 ["reports/macro_research/macro_regime_research.md"])
    s3.missing = ["未做因果识别，不能把条件相关当作驱动因素"]

    # 4 正面因素 / 5 负面因素
    pos = [x for x in a.evidence_for if ("增长" in x and grow.direction == RESILIENT)
           or ("通胀" in x and infl.direction == COOLING)]
    neg = [x for x in a.evidence_for if ("增长" in x and grow.direction == WEAKENING)
           or ("通胀" in x and infl.direction == HEATING)]
    s4 = Section("positives", "正面因素", FACT,
                 pos or ["本库口径本期未检测到支持风险资产的方向性证据"
                         "（不等于没有风险，只等于没被这 8 条序列捕捉到）"],
                 [_src(a, s) for s in a.data_dates])
    s5 = Section("negatives", "负面因素", FACT,
                 neg or ["本库口径本期未检测到反向的方向性证据"
                         "（不等于安全，只等于没被这 8 条序列捕捉到）"],
                 [_src(a, s) for s in a.data_dates])

    # 6 可能已计价 / 7 未计价
    s6 = Section("priced_in", "市场可能已经计价的部分", UNVERIFIABLE, [
        "本库没有一致预期、持仓、资金流或新闻数据，**无法判断市场计价程度**",
    ], [], ["一致预期（如 CPI 预期中位数）", "持仓/资金流数据", "事件窗口内的新闻与公告"])

    s7 = Section("not_priced", "尚未计价或无法确认的部分", UNVERIFIABLE, [
        "同上：缺少预期与持仓数据，无法区分「已反映」与「未反映」",
        "数据版本风险：本期读数若被后续修订改变方向，本判定需重做（见 §9）",
    ], [], ["后续修订值（尚未发生）", "预期与实际之差（surprise）"])

    # 8 置信度
    s8 = Section("confidence", "置信度", FACT, [
        f"{a.confidence}（由两块投票强度与数据新鲜度决定）",
        *a.notes,
    ], [_src(a, s) for s in a.data_dates])

    # 9 失效条件
    s9 = Section("invalidators", "判断失效条件", FACT, list(a.invalidators))

    # 10 来源与发布时间
    s10 = Section("sources", "信息来源与发布时间", FACT,
                  [f"{sid}: 观测期 {p}，版本 {a.versions.get(sid, '?')}"
                   for sid, p in sorted(a.data_dates.items())],
                  [], ["发布时刻只到日粒度；不是精确到分钟的发布时间"])

    sections = [s1, s2, s3, s4, s5, s6, s7, s8, s9, s10]
    return {"as_of": a.as_of, "state": a.state, "label": a.label,
            "sections": [s.as_dict() for s in sections],
            "unverifiable_sections": [s.key for s in sections if s.status == UNVERIFIABLE]}


def render_text(doc: Dict[str, Any]) -> str:
    """把结构渲染成便于人读的文本（不做任何额外推断）。"""
    lines = [f"宏观解读 @ {doc['as_of']}　状态：{doc['label']}"]
    for s in doc["sections"]:
        tag = {"fact": "事实", "inferred": "推断", "unverifiable": "无法确认"}[s["status"]]
        lines.append(f"\n{s['title']}［{tag}］")
        for c in s["content"]:
            lines.append(f"  - {c}")
        if s["missing"]:
            lines.append(f"  · 需要但缺失：{'；'.join(s['missing'])}")
    return "\n".join(lines)
