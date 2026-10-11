"""Relationship stage: LLM proposes, rules constrain. Never rewards; describes."""

from __future__ import annotations

from dataclasses import dataclass

from rapport.features.compute import Features
from rapport.schemas.profile import Profile
from rapport.schemas.report_v1 import (
    STAGE_LADDER,
    Boundary,
    Stage,
    StageValue,
)

# A relationship is only called 稳定 when the material itself shows durability: enough separate
# sessions spread over enough days, with the relationship confirmed and no boundary signal.
# Product hypotheses, not psychometrics.
MIN_STABLE_SESSIONS = 8
MIN_STABLE_DAYS = 21


@dataclass(frozen=True)
class StageChange:
    before: str
    after: str
    direction: str  # advance | regress | same
    reason: str
    basis_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "before": self.before,
            "after": self.after,
            "direction": self.direction,
            "reason": self.reason,
            "basis_ids": list(self.basis_ids),
        }


def rule_stage(f: Features, profile: Profile, boundary: Boundary) -> Stage:
    """Rule-only stage (used in Rules mode and as a cap for the LLM stage)."""
    if profile.stage_confirmed == "mutually_confirmed":
        return Stage(value=StageValue.mutually_confirmed, user_confirmed=True, note="由用户确认为已确定关系。")
    if f.confirm_ids:
        return Stage(value=StageValue.mutually_confirmed, basis_ids=f.confirm_ids[:3], note="聊天中出现明确的关系确认表述。")
    if f.conflict_ids and f.apology_ids:
        return Stage(value=StageValue.repair, basis_ids=(f.conflict_ids[:2] + f.apology_ids[:2]), note="出现冲突与道歉并存的片段。")
    o = f.other
    if o.texts < 10:
        return Stage(value=StageValue.unknown, note="对方文本过少，无法判断阶段。")
    aff = (o.affection_rate.value or 0) + (o.care_rate.value or 0)
    fut = o.future_rate.value or 0
    if aff >= 0.06 or fut >= 0.08 or o.affection_ids:
        return Stage(value=StageValue.affectionate_signals, basis_ids=(o.affection_ids + o.care_ids + o.future_ids)[:5], note="规则观察到关心/亲昵/见面相关表述。")
    if f.n_sessions >= 3 or o.texts >= 30:
        return Stage(value=StageValue.getting_to_know, note="多段持续对话，处于相互了解阶段（规则估计）。")
    return Stage(value=StageValue.initial_contact, note="对话量较少，处于初步接触（规则估计）。")


def _rank(value: StageValue) -> int | None:
    return STAGE_LADDER.index(value) if value in STAGE_LADDER else None


def meets_stable_requirements(f: Features, stage: Stage, boundary: Boundary) -> tuple[bool, str]:
    """Is the material durable enough to claim 稳定?"""
    # Proposing 稳定 already presupposes a confirmed relationship, so the gate is "confirmed or
    # explicitly asserted by the user", not "the proposal literally says mutually_confirmed".
    confirmed = stage.value in (StageValue.mutually_confirmed, StageValue.stable) or stage.user_confirmed
    if not confirmed:
        return False, "尚未确认关系，谈不上稳定"
    _ = boundary  # v0.8: a boundary is a note on the report, not a stage constraint
    if f.n_sessions < MIN_STABLE_SESSIONS:
        return False, f"只有 {f.n_sessions} 段会话，少于 {MIN_STABLE_SESSIONS} 段"
    if f.range_start and f.range_end:
        span = (f.range_end - f.range_start).days
        if span < MIN_STABLE_DAYS:
            return False, f"材料跨度 {span} 天，少于 {MIN_STABLE_DAYS} 天"
    else:
        return False, "缺少可靠时间，无法确认持续时长"
    return True, ""


def evaluate_transition(previous_value: str | None, proposed: Stage, f: Features, boundary: Boundary) -> Stage:
    """Apply the ladder rules to a proposed stage and record what changed.

    Rules, in order:
    1. A proposal may not skip a rung. Jumping straight from 陌生 to 确定关系 is downgraded to the
       next rung, because the missing rung has to be evidenced somewhere.
    2. 稳定 must satisfy the durability requirements above.
    3. Regressions are allowed and recorded — relationships cool down, and hiding that would make
       the trend dishonest.
    """
    change: StageChange | None = None
    if previous_value is None:
        if proposed.value == StageValue.stable:
            ok, why = meets_stable_requirements(f, proposed, boundary)
            if not ok:
                proposed = Stage(value=StageValue.mutually_confirmed, basis_ids=proposed.basis_ids, note=f"不能称为稳定：{why}")
        return proposed

    try:
        previous = StageValue(previous_value)
    except ValueError:
        return proposed

    prev_rank, new_rank = _rank(previous), _rank(proposed.value)
    if prev_rank is not None and new_rank is not None and new_rank > prev_rank + 1:
        target = STAGE_LADDER[prev_rank + 1]
        change = StageChange(
            before=previous.value,
            after=target.value,
            direction="advance",
            reason=f"不允许跳级：从{previous.value}只能先进入{target.value}",
            basis_ids=tuple(proposed.basis_ids),
        )
        return Stage(value=target, basis_ids=proposed.basis_ids, note=f"不允许跳级，已降为「{target.value}」。{proposed.note}".strip(), change=_stage_change_model(change))

    if proposed.value == StageValue.stable:
        ok, why = meets_stable_requirements(f, proposed, boundary)
        if not ok:
            change = StageChange(previous.value, StageValue.mutually_confirmed.value, "same", f"不能称为稳定：{why}")
            return Stage(value=StageValue.mutually_confirmed, basis_ids=proposed.basis_ids, note=f"不能称为稳定：{why}", change=_stage_change_model(change))

    if proposed.value == previous:
        return proposed

    if prev_rank is not None and new_rank is not None:
        direction = "advance" if new_rank > prev_rank else "regress"
    else:
        direction = "same"
    change = StageChange(
        before=previous.value,
        after=proposed.value,
        direction=direction,
        reason="材料显示的关系阶段发生变化",
        basis_ids=tuple(proposed.basis_ids),
    )
    return Stage(
        value=proposed.value,
        basis_ids=proposed.basis_ids,
        user_confirmed=proposed.user_confirmed,
        note=(proposed.note + ("（较上次回退）" if direction == "regress" else "")).strip(),
        change=_stage_change_model(change),
    )


def _stage_change_model(change: StageChange):
    from rapport.schemas.report_v1 import StageChangeModel

    return StageChangeModel(**change.as_dict())


def constrain_stage(llm_stage: Stage | None, rule: Stage, f: Features, profile: Profile) -> Stage:
    """Apply hard constraints to the LLM's stage."""
    if llm_stage is None:
        return rule
    allowed_confirmed = profile.stage_confirmed == "mutually_confirmed" or bool(f.confirm_ids) or bool(llm_stage.basis_ids)
    if llm_stage.value == StageValue.mutually_confirmed and not allowed_confirmed:
        return Stage(
            value=StageValue.affectionate_signals,
            basis_ids=llm_stage.basis_ids,
            note="模型判断为已确认关系，但材料中没有明确的关系确认证据，已降为“出现好感信号”。",
        )
    if rule.user_confirmed:
        return rule
    return llm_stage
