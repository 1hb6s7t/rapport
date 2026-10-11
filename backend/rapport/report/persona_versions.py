"""Persona revisions: what changed since the last portrait, and why.

The v2 requirement is explicit that the portrait must be *revised*, not re-generated: each new
portrait records a version, every change carries a reason, and old versions stay inspectable so a
user can see how the read of the other person moved. This is what makes "the label assumption was
overridden by real chat" visible instead of silent.
"""

from __future__ import annotations

from dataclasses import dataclass

from rapport.schemas.report_v1 import TENDENCY_LABELS, Persona


@dataclass(frozen=True)
class PersonaChange:
    field: str
    kind: str  # added | changed | removed
    before: str
    after: str
    reason: str

    def as_dict(self) -> dict:
        return {"field": self.field, "kind": self.kind, "before": self.before, "after": self.after, "reason": self.reason}

    def describe(self) -> str:
        verb = {"added": "新增", "changed": "修正", "removed": "移除"}[self.kind]
        arrow = f"{self.before} → {self.after}" if self.kind == "changed" else (self.after or self.before)
        return f"{verb}：{self.field} {arrow}（{self.reason}）"


def _pole_of(key: str, t) -> str:
    from rapport.knowledge.prior_check import infer_pole

    pole = getattr(t, "pole", None) or "unclear"
    return pole if pole != "unclear" else infer_pole(key, getattr(t, "label", "") or "")


def pole_stability(history_tendencies: list[dict], current: dict, window: int = 3) -> dict[str, float]:
    """Per tendency: share of the last `window` versions (plus current) whose pole equals the current
    pole. 1.0 = every recent version agreed; unclear poles are skipped from the denominator."""
    from rapport.knowledge.prior_check import infer_pole

    out: dict[str, float] = {}
    for key in TENDENCY_LABELS:
        cur = current.get(key)
        if cur is None:
            continue
        cp = _pole_of(key, cur)
        if cp == "unclear":
            continue
        poles = []
        for h in history_tendencies[:window]:
            t = (h or {}).get(key)
            if not t:
                continue
            p = t.get("pole") if isinstance(t, dict) else getattr(t, "pole", None)
            if not p or p == "unclear":
                p = infer_pole(key, (t.get("label") if isinstance(t, dict) else getattr(t, "label", "")) or "")
            if p != "unclear":
                poles.append(p)
        if not poles:
            continue
        out[key] = round(sum(1 for p in poles if p == cp) / len(poles), 2)
    return out


def _reason(evidence_ids: list[str], fallback: str = "材料变化") -> str:
    return f"依据 {', '.join(evidence_ids[:3])}" if evidence_ids else fallback


def diff_persona(previous: Persona | None, current: Persona) -> list[PersonaChange]:
    """Compare two portraits. `previous is None` means this is the first version."""
    if previous is None:
        return []
    changes: list[PersonaChange] = []

    for key, label in TENDENCY_LABELS.items():
        before = previous.tendencies.get(key)
        after = current.tendencies.get(key)
        b = before.label if before else ""
        a = after.label if after else ""
        # v0.5 §4: a reworded label on the same pole is not a change. Only when the pole moves
        # (or one side is unclear and the wording differs) do we record a revision.
        if before and after:
            bp = _pole_of(key, before)
            ap = _pole_of(key, after)
            if bp != "unclear" and ap != "unclear" and bp == ap:
                continue
        if a and not b:
            changes.append(PersonaChange(label, "added", "", a, _reason(after.evidence_ids if after else [])))
        elif b and not a:
            changes.append(PersonaChange(label, "removed", b, "", "本次材料没有支持它的证据"))
        elif b != a:
            changes.append(PersonaChange(label, "changed", b, a, _reason(after.evidence_ids if after else [])))

    before_traits = {t.statement: t for t in previous.traits}
    after_traits = {t.statement: t for t in current.traits}
    for statement, t in after_traits.items():
        if statement not in before_traits:
            changes.append(PersonaChange("特质", "added", "", statement, _reason(t.evidence_ids)))
    for statement in before_traits:
        if statement not in after_traits:
            changes.append(PersonaChange("特质", "removed", statement, "", "本次材料没有支持它的证据"))

    for axis in ("EI", "SN", "TF", "JP"):
        b = (previous.inferred_axes.get(axis).lean if previous.inferred_axes.get(axis) else "") or "unclear"
        cur_axis = current.inferred_axes.get(axis)
        a = (cur_axis.lean if cur_axis else "") or "unclear"
        if a != b:
            changes.append(PersonaChange(f"{axis} 轴", "changed", b, a, _reason([])))

    return changes
