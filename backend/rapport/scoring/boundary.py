"""Boundary detection (rule layer) and merge with LLM verdicts. Boundary is independent of stage."""

from __future__ import annotations

from rapport.features.compute import Features
from rapport.schemas.report_v1 import Boundary, BoundaryValue

SEVERITY = {
    BoundaryValue.none: 0,
    BoundaryValue.explicit_decline: 1,
    BoundaryValue.stop_contact: 2,
    BoundaryValue.safety_concern: 3,
}


def rule_boundary(f: Features) -> Boundary:
    """The boundary that is in force *now*.

    v0.6: only hits inside the trailing `BOUNDARY_RECENT_DAYS` (features.compute) set the
    boundary. On the user's real material (9 chats, 46k messages, 2026-09-18) seven of nine
    totals were hidden by a single 想死 / 不合适 from a year or more earlier — a boundary is a
    current state, not a permanent record. Older hits are still surfaced in the note and the
    basis ids so nothing is silently dropped, but they no longer gate the score.
    """
    h = f.boundary_recent if f.boundary_recent else f.boundary_hits
    all_hits = f.boundary_hits
    older = {k: [i for i in all_hits.get(k, []) if i not in set(h.get(k, []))] for k in all_hits}
    hist = _historical_note(older)
    if h.get("safety_concern"):
        return Boundary(value=BoundaryValue.safety_concern, basis_ids=h["safety_concern"][:5], note="规则匹配到安全相关词汇，请人工核对上下文。" + hist)
    if h.get("stop_contact"):
        return Boundary(value=BoundaryValue.stop_contact, basis_ids=h["stop_contact"][:5], note="对方文本中出现明确的停止联系表述。" + hist)
    if h.get("explicit_decline"):
        return Boundary(
            value=BoundaryValue.explicit_decline,
            basis_ids=h["explicit_decline"][:5],
            note="对方明确拒绝过某项提议且未给替代方案。" + hist,
        )
    if h.get("decline_with_alternative"):
        return Boundary(
            value=BoundaryValue.none,
            basis_ids=h["decline_with_alternative"][:5],
            note="对方拒绝过一次安排但提出了替代时间，不视为边界信号，已记入观察。" + hist,
        )
    if hist:
        # Nothing recent, but something older: say so, with evidence, without gating.
        ids = [i for k in ("safety_concern", "stop_contact", "explicit_decline") for i in older.get(k, [])][:5]
        return Boundary(value=BoundaryValue.none, basis_ids=ids, note=hist.strip())
    return Boundary(value=BoundaryValue.none)


def _historical_note(older: dict[str, list[str]]) -> str:
    parts = []
    labels = {"safety_concern": "安全相关词汇", "stop_contact": "停止联系表述", "explicit_decline": "明确拒绝"}
    for k, label in labels.items():
        n = len(older.get(k, []))
        if n:
            parts.append(f"{label} {n} 处")
    if not parts:
        return ""
    return f" 更早的材料里另有 {'、'.join(parts)}（超出最近 30 天，不作为当前边界，已列为证据）。"


def merge_boundary(rule: Boundary, llm: Boundary | None) -> Boundary:
    """Stricter one wins; the later evidence should already be reflected by the LLM layer."""
    if llm is None:
        return rule
    if SEVERITY[llm.value] > SEVERITY[rule.value]:
        return Boundary(value=llm.value, basis_ids=llm.basis_ids[:5], note=llm.note or "模型层判断")
    if SEVERITY[llm.value] == SEVERITY[rule.value] and llm.value != BoundaryValue.none:
        return Boundary(value=rule.value, basis_ids=list(dict.fromkeys(rule.basis_ids + llm.basis_ids))[:6], note=rule.note)
    return rule
