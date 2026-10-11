"""report-v1: the output contract. Everything the UI renders and the exporters emit."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class Mode(StrEnum):
    demo = "demo"
    rules = "rules"
    byok = "byok"


class Confidence(StrEnum):
    low = "low"
    medium = "medium"
    high = "high"


class Versions(BaseModel):
    app: str
    parser: str
    features: str
    rubric: str
    prompts: str | None = None
    provider: str | None = None
    model: str | None = None
    price_date: str | None = None


class Scope(BaseModel):
    msg_total: int
    msg_analyzed: int
    events_other: int = 0
    events_me: int = 0
    text_other: int = 0
    text_me: int = 0
    sessions: int = 0
    range_start: datetime | None = None
    range_end: datetime | None = None
    has_reliable_time: bool = False
    coverage: float = 0.0
    quality_flags: list[str] = Field(default_factory=list)


DIMENSIONS = ("responsiveness", "initiative", "investment", "warmth", "future")
DIM_LABELS = {
    "responsiveness": "回应性",
    "initiative": "主动性",
    "investment": "投入度",
    "warmth": "情感温度",
    "future": "未来指向",
}


class DimScore(BaseModel):
    score: int | None = None
    rule: int | None = None
    llm: int | None = None
    confidence: Confidence = Confidence.low
    evidence_ids: list[str] = Field(default_factory=list)
    note: str = ""
    inputs: dict[str, Any] = Field(default_factory=dict)


class InvitationChain(BaseModel):
    """One offer and what came of it (v0.5).

    Carried into the report so the future card can show the chain a number came from: what was
    proposed, whether the answer was yes / another time / no / nothing, and whether a concrete
    time came out of it. Added with defaults, so `report-v1` stays the schema version.
    """

    propose_id: str
    propose_text: str = ""
    response: str = "none"  # accept | alt_time | decline | deflect | none
    response_id: str | None = None
    commit_ids: list[str] = Field(default_factory=list)


class Affinity(BaseModel):
    total: int | None = None
    raw_total: float | None = None
    coverage: float = 0.0
    # v0.8: none of these hides the total any more. They are kept for old report bodies and as
    # the source of `caveats`, which is what the UI shows next to the number.
    suppressed_reason: str | None = None
    insufficient_reason: str | None = None
    caveats: list[str] = Field(default_factory=list)
    confidence: str = "medium"  # overall: low | medium | high, from evidence volume
    min_evidence_reason: str | None = None  # why the >=2 independent snippets gate failed
    independent_units: int = 0
    independent_unit_kind: str = ""
    dims: dict[str, DimScore] = Field(default_factory=dict)
    # The chains behind the future dimension: empty when the material had no offer at all, which is
    # also when that dimension fell back to counting words.
    future_chains: list[InvitationChain] = Field(default_factory=list)
    # Shadow weighting (v0.5, plan §1C): the same dimensions aggregated with stage-conditional
    # weights, kept beside the static total so the two can be compared against user check-ins
    # before anything switches. `weights_used` is the renormalised weight of each dimension in the
    # `stage_weighted` figure.
    alt_totals: dict[str, int | None] = Field(default_factory=dict)
    weights_used: dict[str, float] = Field(default_factory=dict)
    # Shrinkage toward a neutral 50 when evidence is thin. This is NOT a label-matching score;
    # the MVP shows S_obs plus this note, and keeps the provisional number for experiments.
    provisional_total: int | None = None
    rho: float = 0.0
    provisional_note: str = ""
    disclaimer: str = ""


class TrendPoint(BaseModel):
    label: str
    start: datetime | None = None
    end: datetime | None = None
    score: int | None = None
    n_msgs: int = 0
    n_other_text: int = 0
    insufficient: bool = False
    dims: dict[str, int | None] = Field(default_factory=dict)
    # Drill-down (v0.5): the messages and inputs behind each dimension of this segment, and which
    # values came from the model rather than the rules. Defaults, so `report-v1` is unchanged.
    evidence_ids: list[str] = Field(default_factory=list)
    dim_evidence: dict[str, list[str]] = Field(default_factory=dict)
    dim_inputs: dict[str, dict] = Field(default_factory=dict)
    llm_override: dict[str, int] = Field(default_factory=dict)


class ChangePoint(BaseModel):
    """One candidate structural change in the interaction (ruptures PELT, exploratory)."""

    index: int = 0
    date: datetime | None = None
    stability: float = 0.0  # share of cost/penalty settings that agree on this index
    evidence_ids: list[str] = Field(default_factory=list)
    note: str = ""


class Trend(BaseModel):
    bucket: str = "session"  # day | week | month | session
    points: list[TrendPoint] = Field(default_factory=list)
    momentum: str = "unknown"  # rising | flat | falling | unknown
    note: str = ""
    # v0.8: exploratory change points from the session-feature series; never a score input.
    change_points: list[ChangePoint] = Field(default_factory=list)
    change_note: str = ""


# v2 (v0.7): five neutral pragmatic classes and three emotions added so that most everyday
# lines have a class. On the author's real material under v1, 26.7% of the other person's
# messages fell to "no class" and 51% of emotions were "none obvious" (docs/LABEL-V2-PROPOSAL.md).
# The negative sets below are unchanged: the new classes never enter the gate.
VOCAB_VERSION = "labels-v2"
PRAGMATIC_TAGS = (
    "真生气", "撒娇假生气", "调侃", "反讽", "委婉拒绝", "寻求安慰", "信息确认",
    "分享陈述", "询问", "观点评价", "抱怨", "同意接受",
    "不确定",
)
EMOTIONS = ("不满", "伤心", "开心", "担忧", "亲近", "混合", "无明显情绪", "无奈", "惊讶困惑", "不确定")
# Tags that carry no relationship signal on their own; the UI renders them quietly.
NEUTRAL_PRAGMATIC = frozenset({"信息确认", "分享陈述", "询问", "观点评价", "同意接受"})

# Readings that accuse the other person of coldness. They only reach the main UI when backed
# by >= 2 independent snippets (see scoring.affinity.independent_units) — measured on
# 2026-09-15 the model over-predicted this class, so surfacing it freely would systematically
# tell users "the other person is cooling off".
NEGATIVE_PRAGMATIC = frozenset({"真生气", "反讽", "委婉拒绝"})
NEGATIVE_EMOTIONS = frozenset({"不满", "伤心"})


class IntentCandidate(BaseModel):
    reading: str
    likelihood: Confidence = Confidence.low
    basis_ids: list[str] = Field(default_factory=list)


class NeighborExample(BaseModel):
    """A label the offline retrieval layer found most similar, with its similarity.

    Only the *label* and score are carried, never the neighbour's text: the anchor corpus is the
    user's own material (possibly another conversation), and a report has no business quoting it.
    """

    label: str
    score: float = 0.0


class IntentReading(BaseModel):
    """Multi-label, evidence-bound reading of ONE message in its context (v2 M03).

    Deliberately NOT a single mutually-exclusive label: real anger and 撒娇 can coexist,
    and `ambiguous` is a first-class outcome rather than a failure.
    """

    msg_id: str
    episode_id: str = ""
    speaker: str = "other"
    surface_text: str = ""
    literal_affect: str = ""
    pragmatic_tags: list[str] = Field(default_factory=list)
    emotions: list[str] = Field(default_factory=list)
    tag_basis: dict[str, list[str]] = Field(default_factory=dict)  # tag -> its own evidence ids
    boundary_status: str = "none"  # one of BoundaryValue; declared as str to avoid forward refs
    candidates: list[IntentCandidate] = Field(default_factory=list)
    evidence_strength: Confidence = Confidence.low
    model_confidence_raw: float | None = None  # uncalibrated self-report, never shown as probability
    evidence_ids: list[str] = Field(default_factory=list)
    counter_evidence_ids: list[str] = Field(default_factory=list)
    reference_example_ids: list[str] = Field(default_factory=list)
    post_hoc: bool = False  # read with hindsight (review mode); never a prediction made at the time
    context_mode: str = "unknown"  # legacy records have no causal visibility guarantee
    source: str | None = None  # "local_model" when produced offline by the bundled small model
    ambiguous: bool = False
    abstain_reason: str | None = None
    suppressed_reason: str | None = None  # negative reading held back for lack of independent evidence
    # v0.5 §3B: agreement across k sampled draws (1.0 = every draw said the same). Observable,
    # unlike `model_confidence_raw`. `samples` = 1 means a single deterministic call.
    consistency: float | None = None
    samples: int = 1
    independent_units: int = 0
    # v0.8 (research-grounded layer): what each offline signal voted, and the nearest labelled
    # examples the retrieval found. Defaults, so `report-v1` is unchanged.
    method_votes: dict[str, str] = Field(default_factory=dict)
    similar_examples: list[NeighborExample] = Field(default_factory=list)


class Observation(BaseModel):
    id: str
    claim: str
    evidence_ids: list[str] = Field(default_factory=list)
    counter_evidence_ids: list[str] = Field(default_factory=list)
    alternatives: list[str] = Field(default_factory=list)
    limitations: str = ""


class Tendency(BaseModel):
    label: str
    evidence_ids: list[str] = Field(default_factory=list)
    note: str = ""
    # v0.5 §4: which pole the label sits on (knowledge.prior_check.TENDENCY_POLES); `unclear` when
    # the wording does not commit. Poles, not wording, are what versions and prior checks compare.
    pole: str = "unclear"


class Trait(BaseModel):
    statement: str
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: Confidence = Confidence.low


class AxisLean(BaseModel):
    lean: str = "unclear"  # E|I|S|N|T|F|J|P|unclear
    confidence: Confidence = Confidence.low
    note: str = ""


class MbtiComparison(BaseModel):
    declared: str
    inferred: str  # e.g. "E?T?" with ? for unclear
    verdict: str  # similar | context_differs | insufficient
    notes: list[str] = Field(default_factory=list)


TENDENCY_KEYS = (
    "directness",
    "emotional_expression",
    "detail_preference",
    "need_expression",
    "disagreement_handling",
    "rhythm",
)
TENDENCY_LABELS = {
    "directness": "表达直接性",
    "emotional_expression": "情绪表达",
    "detail_preference": "细节偏好",
    "need_expression": "需求表达",
    "disagreement_handling": "分歧处理",
    "rhythm": "互动节奏",
}


class PriorHypothesis(BaseModel):
    """A label-derived starting assumption with its current, decaying weight."""

    label: str  # mbti | zodiac | chinese_zodiac
    value: str
    weight: float
    hypothesis: str
    source_type: str = "tag_hypothesis"
    basis: str = ""
    entertainment: bool = False


class PriorCheckModel(BaseModel):
    """One label card checked against the observed tendency (v0.5 §4)."""

    label: str
    value: str
    tendency_key: str
    lean: str
    lean_label: str = ""
    text: str = ""
    counterexample_prompt: str = ""
    status: str = "untested"  # supported | contradicted | untested
    observed_pole: str = "unclear"
    observed_label: str = ""
    evidence_ids: list[str] = Field(default_factory=list)


class PriorBreakdownModel(BaseModel):
    policy_version: str = "legacy"
    alpha: float = 0.0
    # The budget this report was built under (user-adjustable; 0.15 / 4:2:1 are the defaults).
    alpha0: float = 0.15
    shares: dict[str, float] = Field(default_factory=dict)
    weights: dict[str, float] = Field(default_factory=dict)
    unknown: float = 1.0
    present: list[str] = Field(default_factory=list)
    n_eff: float = 0.0
    # v0.6: what N_eff was built from. Unread media never counts; saying so on the report is
    # what keeps "1000 messages but N_eff 12" from looking like a bug.
    evidence_counts: dict[str, int] = Field(default_factory=dict)  # text | media_unobserved | other


class PersonaChangeModel(BaseModel):
    field: str
    kind: str  # added | changed | removed
    before: str = ""
    after: str = ""
    reason: str = ""


class Persona(BaseModel):
    version: int = 0
    changed_from_report: str | None = None
    change_notes: list[PersonaChangeModel] = Field(default_factory=list)
    tendencies: dict[str, Tendency] = Field(default_factory=dict)
    traits: list[Trait] = Field(default_factory=list)
    inferred_axes: dict[str, AxisLean] = Field(default_factory=dict)
    mbti_comparison: MbtiComparison | None = None
    limitations: str = "以下侧写只反映本段聊天材料中的表达方式，不是稳定人格诊断。"
    available: bool = False
    # v0.5 §4: per tendency, share of the last 3 versions whose pole agrees with this one.
    pole_stability: dict[str, float] = Field(default_factory=dict)


class StageValue(StrEnum):
    """The user-facing ladder: 陌生 → 初识 → 暧昧 → 确定关系 → 稳定.

    `repair` and `unknown` sit outside the ladder: repair describes a conflict episode rather
    than a station on it, and unknown means the material does not support any label.
    """

    unknown = "unknown"
    initial_contact = "initial_contact"  # 陌生
    getting_to_know = "getting_to_know"  # 初识
    affectionate_signals = "affectionate_signals"  # 暧昧
    mutually_confirmed = "mutually_confirmed"  # 确定关系
    stable = "stable"  # 稳定
    repair = "repair"


STAGE_LABELS = {
    "unknown": "无法判断",
    "initial_contact": "陌生",
    "getting_to_know": "初识",
    "affectionate_signals": "暧昧",
    "mutually_confirmed": "确定关系",
    "stable": "稳定",
    "repair": "修复期",
}

# The ladder used for "did it move, and by how much" checks. Repair/unknown are not on it.
STAGE_LADDER = (
    StageValue.initial_contact,
    StageValue.getting_to_know,
    StageValue.affectionate_signals,
    StageValue.mutually_confirmed,
    StageValue.stable,
)


class StageChangeModel(BaseModel):
    before: str
    after: str
    direction: str  # advance | regress | same | unknown
    reason: str = ""
    basis_ids: list[str] = Field(default_factory=list)


class Stage(BaseModel):
    value: StageValue = StageValue.unknown
    basis_ids: list[str] = Field(default_factory=list)
    user_confirmed: bool = False
    note: str = ""
    change: StageChangeModel | None = None


class BoundaryValue(StrEnum):
    none = "none"
    explicit_decline = "explicit_decline"
    stop_contact = "stop_contact"
    safety_concern = "safety_concern"


BOUNDARY_LABELS = {
    "none": "未发现明确边界信号",
    "explicit_decline": "对方明确拒绝过某项提议",
    "stop_contact": "对方表达了不想继续联系",
    "safety_concern": "出现安全相关信号",
}


class Boundary(BaseModel):
    value: BoundaryValue = BoundaryValue.none
    basis_ids: list[str] = Field(default_factory=list)
    note: str = ""


class Action(BaseModel):
    goal: str
    action: str
    basis_ids: list[str] = Field(default_factory=list)
    suggested_text: str = ""
    observe: str = ""
    stop_when: str
    alternatives: list[str] = Field(default_factory=list)
    personalized: bool = True
    caution: str = ""  # v0.8: boundary note instead of dropping the action


class Landmine(BaseModel):
    topic: str
    why: str
    evidence_ids: list[str] = Field(default_factory=list)


class Script(BaseModel):
    text: str
    when_to_use: str = ""
    caution: str = ""


class Reaction(BaseModel):
    """What the other person might do after an option, and what that would mean (v0.5 §2)."""

    if_they: str  # 对方的反应，≤40字
    then: str  # 这说明什么 / 你接着怎么做，≤60字
    signal: str = "neutral"  # good | neutral | stop


def option_id(goal: str, action: str) -> str:
    import hashlib

    return hashlib.sha1(f"{goal.strip()}|{action.strip()}".encode()).hexdigest()[:10]


class Option(BaseModel):
    """One concrete thing the user could do in a situation. Every option is tied to the evidence
    that triggered the situation, says what to expect, and says when to stop."""

    id: str = ""
    goal: str
    action: str
    trigger_ids: list[str] = Field(default_factory=list)  # evidence that makes this situation real
    suggested_text: str = ""
    expected_reactions: list[Reaction] = Field(default_factory=list)  # >= 2 after validation
    stop_when: str
    risk: str = ""  # ≤60字，这个选项可能出什么问题
    caution: str = ""  # v0.8: 边界提示（对方近期有拒绝/停止联系表述），不再丢弃选项
    personalized: bool = True

    def ensure_id(self) -> Option:
        if not self.id:
            self.id = option_id(self.goal, self.action)
        return self


class Situation(BaseModel):
    """A recognisable state of the chat ("你提议了两次都没回应") with 1–3 options."""

    id: str
    title: str  # ≤20字
    trigger_ids: list[str] = Field(default_factory=list)
    why: str = ""  # ≤60字，为什么认为你处在这个情境
    options: list[Option] = Field(default_factory=list)


class DoneReview(BaseModel):
    """What the user did after the previous report, and what came of it (from ActionLog)."""

    option_id: str
    goal: str
    observed: str  # accepted | alt_time | declined | no_reply | other
    note: str = ""
    verdict: str = ""  # 一句话：这次结果对下一步意味着什么


class Playbook(BaseModel):
    changed_from_report: str | None = None
    change_notes: list[str] = Field(default_factory=list)  # why this advice differs from last time
    actions: list[Action] = Field(default_factory=list)  # kept for compatibility: first option of each situation
    situations: list[Situation] = Field(default_factory=list)  # v0.5 §2: 情境 → 选项
    done_review: list[DoneReview] = Field(default_factory=list)  # 上次「我做了」的回顾
    landmines: list[Landmine] = Field(default_factory=list)
    scripts: dict[str, Script] = Field(default_factory=dict)  # casual | sincere | direct
    note: str = ""


class FunItem(BaseModel):
    title: str
    text: str
    predictive_weight: int = 0


class Fun(BaseModel):
    zodiac_pair: FunItem | None = None
    chinese_zodiac_pair: FunItem | None = None
    disclaimer: str = "星座与属相仅作文化背景参考，不能证明性格或关系。它们对客观关系评分的权重为 0；解释与回复排序中的所有辅助资料合计最多占 15%。"


class EvidenceSnippet(BaseModel):
    sender: str
    ts: datetime | None = None
    text: str


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0


class Cost(BaseModel):
    est_tokens_in: int = 0
    est_tokens_out: int = 0
    est_cny: float | None = None
    actual_usage: Usage | None = None
    actual_cny: float | None = None
    currency: str = "CNY"


class Report(BaseModel):
    schema_version: str = "report-v1"
    analysis_context: dict[str, Any] = Field(default_factory=dict)
    id: str
    created_at: datetime
    mode: Mode
    synthetic: bool = False
    versions: Versions
    scope: Scope
    affinity: Affinity
    trend: Trend
    observations: list[Observation] = Field(default_factory=list)
    intent_readings: list[IntentReading] = Field(default_factory=list)
    priors: list[PriorHypothesis] = Field(default_factory=list)
    prior_checks: list[PriorCheckModel] = Field(default_factory=list)  # v0.5 §4
    prior_breakdown: PriorBreakdownModel = Field(default_factory=PriorBreakdownModel)
    persona: Persona = Field(default_factory=Persona)
    stage: Stage = Field(default_factory=Stage)
    boundary: Boundary = Field(default_factory=Boundary)
    playbook: Playbook = Field(default_factory=Playbook)
    fun: Fun = Field(default_factory=Fun)
    evidence: dict[str, EvidenceSnippet] = Field(default_factory=dict)
    cost: Cost = Field(default_factory=Cost)
    features: dict[str, Any] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    ai_status: str = "not_requested"  # not_requested | completed | failed | insufficient
    ai_error: str | None = None
