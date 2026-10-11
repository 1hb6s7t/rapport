"""Affinity aggregation: fuse rule + llm per dimension, gate, suppress, round."""

from __future__ import annotations

from rapport.config import AFFINITY_DISCLAIMER
from rapport.features.compute import Features
from rapport.schemas.message_v1 import Message, Sender
from rapport.schemas.report_v1 import (
    DIMENSIONS,
    Affinity,
    BoundaryValue,
    Confidence,
    DimScore,
    InvitationChain,
)
from rapport.scoring.rubric import (
    COVERAGE_MIN,
    MIN_OTHER_TEXTS,
    MIN_SESSIONS,
    RELIABILITY,
    STAGE_BETA,
    WEIGHTS,
    RuleDim,
    round_half_up,
    rule_dims,
)


def fuse(rule: int | None, llm: int | None, rule_conf: Confidence) -> tuple[int | None, Confidence]:
    if rule is not None and llm is not None:
        conf = Confidence.high if rule_conf == Confidence.high else Confidence.medium
        return round_half_up(0.5 * rule + 0.5 * llm), conf
    if rule is not None:
        return rule, rule_conf if rule_conf != Confidence.high else Confidence.medium
    if llm is not None:
        return llm, Confidence.low
    return None, Confidence.low


def aggregate(dims: dict[str, DimScore]) -> tuple[int | None, float | None, float]:
    """Return (rounded_total, raw_total, coverage)."""
    avail = [(WEIGHTS[k], dims[k].score) for k in DIMENSIONS if k in dims and dims[k].score is not None]
    coverage = sum(w for w, _ in avail)
    if coverage == 0:
        return None, None, 0.0
    raw = sum(w * s for w, s in avail) / coverage
    return round_half_up(raw), round(raw, 3), round(coverage, 3)


MIN_HALF_TEXTS = 5
# Effective-evidence count at which the neutral shrinkage is gone. Product hypothesis.
NEFF_FULL = 20.0
NEUTRAL = 50.0


def shrinkage(n_eff: float, neff_full: float = NEFF_FULL) -> float:
    """rho: how much to pull the observed score toward neutral because evidence is thin.

    rho = 1 at no evidence, 0 once `neff_full` confirmed units exist. It is a function of
    evidence only — never of MBTI/zodiac.
    """
    if n_eff <= 0:
        return 1.0
    if n_eff >= neff_full:
        return 0.0
    return 1.0 - (n_eff / neff_full)


def provisional_total(observed: float, rho: float) -> int:
    """S_prov = rho * 50 + (1 - rho) * S_obs."""
    return round_half_up(rho * NEUTRAL + (1.0 - rho) * observed)


def independent_units(f: Features, events: list[Message] | None = None) -> tuple[int, str]:
    """How many independent context snippets back this judgement, and of what kind.

    With reliable timestamps the unit is a session segment (>= session_gap_hours apart).
    Without them independence cannot be proven from time, so we fall back to an order-based
    check: both halves of the material in message order must each carry enough other-person
    text. A single burst of messages followed by silence therefore counts as one snippet.
    """
    if f.has_reliable_time:
        return f.n_sessions, "会话段"
    ev = events or []
    if len(ev) < 2:
        return 0, "顺序片段"
    mid = len(ev) // 2
    halves = 0
    for part in (ev[:mid], ev[mid:]):
        n = sum(1 for m in part if m.sender == Sender.other and m.is_text)
        if n >= MIN_HALF_TEXTS:
            halves += 1
    return halves, "顺序片段"


def stage_weighted_total(dims: dict[str, DimScore], stage: str | None) -> tuple[int | None, dict[str, float]]:
    """The same dimensions aggregated with stage-conditional weights (plan §1C).

    Shadow only — nothing shown to the user comes from here yet. w_j = WEIGHTS·β_stage·reliability,
    renormalised over the dimensions that are actually observed.
    """
    beta = STAGE_BETA.get(stage or "", {})
    ws: dict[str, float] = {}
    for k in DIMENSIONS:
        if k in dims and dims[k].score is not None:
            ws[k] = WEIGHTS[k] * beta.get(k, 1.0) * RELIABILITY.get(k, 1.0)
    tot = sum(ws.values())
    if not tot:
        return None, {}
    raw = sum(w * dims[k].score for k, w in ws.items()) / tot
    return round_half_up(raw), {k: round(w / tot, 4) for k, w in ws.items()}


def build_affinity(
    f: Features,
    llm_dims: dict[str, dict] | None = None,
    boundary: BoundaryValue = BoundaryValue.none,
    events: list[Message] | None = None,
    n_eff: float = 0.0,
    stage: str | None = None,
) -> Affinity:
    rd: dict[str, RuleDim] = rule_dims(f)
    dims: dict[str, DimScore] = {}
    for k in DIMENSIONS:
        r = rd[k]
        llm_info = (llm_dims or {}).get(k) or {}
        llm_score = llm_info.get("score")
        score, conf = fuse(r.score, llm_score, r.confidence)
        dims[k] = DimScore(
            score=score,
            rule=r.score,
            llm=llm_score,
            confidence=conf,
            evidence_ids=list(dict.fromkeys(r.evidence_ids + list(llm_info.get("evidence_ids") or [])))[:8],
            note=llm_info.get("note") or r.note,
            inputs=r.inputs,
        )
    total, raw, coverage = aggregate(dims)
    units, unit_kind = independent_units(f, events)
    # v0.8: no gate hides the number. Thin evidence and boundary signals become caveats and a
    # lower overall confidence; the total is always computed from whatever dimensions exist.
    # (History: v0.3 hid it under 20 texts / 2 sessions / 0.6 coverage / any boundary; v0.5 stopped
    # hiding it for 「不用了」; v0.8 the user asked for the number, with the caveats beside it.)
    caveats: list[str] = []
    insufficient = None
    min_evidence = None
    if f.other.texts < MIN_OTHER_TEXTS:
        insufficient = f"对方文本消息只有 {f.other.texts} 条，少于 {MIN_OTHER_TEXTS} 条，分数很粗"
        caveats.append(insufficient)
    if units < MIN_SESSIONS:
        min_evidence = f"只有 {units} 个独立{unit_kind}：还不能确认这是持续互动而非单次片段"
        caveats.append(min_evidence)
    if coverage < COVERAGE_MIN:
        caveats.append(f"观察覆盖度 {coverage:.2f}，有维度缺失，总分按可用维度归一")
    suppressed = None
    if boundary in (BoundaryValue.stop_contact, BoundaryValue.safety_concern):
        suppressed = {
            BoundaryValue.stop_contact: "对方近期表达了不想继续联系；分数描述的是过去的互动，不是现在的意愿",
            BoundaryValue.safety_concern: "材料中出现安全相关信号；请先关注安全，再看分数",
        }[boundary]
        caveats.append(suppressed)
    elif boundary == BoundaryValue.explicit_decline:
        caveats.append("对方近期明确拒绝过一项提议")
    if f.other.texts < MIN_OTHER_TEXTS or units < MIN_SESSIONS:
        overall_conf = "low"
    elif coverage < COVERAGE_MIN or n_eff < NEFF_FULL:
        overall_conf = "medium"
    else:
        overall_conf = "high"
    rho = shrinkage(n_eff)
    prov = None if raw is None else provisional_total(raw, rho)
    prov_note = (
        f"证据量 {n_eff:.1f}（满 {NEFF_FULL:.0f} 起不再收缩）：观测分向中性 {NEUTRAL:.0f} 收缩 {rho * 100:.0f}%。"
        "这是实验性展示，主界面只显示观测分。"
    )
    alt_total, weights_used = stage_weighted_total(dims, stage)
    return Affinity(
        provisional_total=prov,
        rho=round(rho, 4),
        provisional_note=prov_note,
        total=total,
        raw_total=raw,
        coverage=coverage,
        suppressed_reason=suppressed,
        insufficient_reason=insufficient,
        min_evidence_reason=min_evidence,
        caveats=caveats,
        confidence=overall_conf,
        independent_units=units,
        independent_unit_kind=unit_kind,
        dims=dims,
        future_chains=[InvitationChain(**c) for c in (f.invitations or {}).get("chains", [])],
        # Both totals follow the same gates, so the comparison below is between two weightings and
        # not between "a number" and "no number".
        alt_totals={"static": total, "stage_weighted": alt_total},
        weights_used=weights_used,
        disclaimer=AFFINITY_DISCLAIMER,
    )
