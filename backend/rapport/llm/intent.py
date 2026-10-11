"""M03: contextual, multi-label intent reading of one message.

Design constraints come straight from the measured evidence (2026-09-15, scripts/emotion_probe.py):
with context the model got only 9/20 of these scenarios right, abstained 20/20 without
context, and systematically over-read coldness. So this module:

- keeps `ambiguous` as a first-class outcome instead of forcing one label;
- requires every claim to cite message ids from the sent episode;
- **holds negative readings back** until >= 2 independent snippets support them
  (`MIN_INDEPENDENT_UNITS`), so the product never tells a user "they are cooling off" on
  the strength of a single ambiguous line;
- never sends MBTI/zodiac labels.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from rapport.features.cues import cues_block
from rapport.features.episodes import Episode, select_targets
from rapport.llm.extract import format_line
from rapport.llm.limits import INTENT_MAX_TOKENS
from rapport.llm.limits import budget as _budget
from rapport.llm.prompts import load_prompt
from rapport.llm.provider import CallUsage, LLMProvider
from rapport.llm.redact import RedactStats
from rapport.schemas.message_v1 import Conversation, Sender
from rapport.schemas.report_v1 import (
    EMOTIONS,
    NEGATIVE_EMOTIONS,
    NEGATIVE_PRAGMATIC,
    PRAGMATIC_TAGS,
    Confidence,
    IntentCandidate,
    IntentReading,
)

INTENT_SYSTEM = load_prompt("intent")

MIN_INDEPENDENT_UNITS = 2
CONSISTENCY_UNANIMOUS = 1.0  # all k draws agree on the merged tag set
TARGET_MARK = "  ◀ 目标句"


@dataclass
class IntentInput:
    episode: Episode
    lines: list[str]
    valid_ids: set[str]
    reference_ids: list[str] = field(default_factory=list)
    reference_block: str = ""
    patterns: list[dict] = field(default_factory=list)
    cues_block: str = ""  # v0.5 §3A structural cues (facts), empty when the switch is off

    def user_prompt(self) -> str:
        parts = [
            "对话片段（A 是使用者，B 是对方；行首是消息 id）：",
            "\n".join(self.lines),
        ]
        if self.reference_block:
            parts += [
                "",
                "以下是公开参考例（只作为语用习惯的类比，不是本次对话的证据；不要引用它们的 id）：",
                self.reference_block,
            ]
        if self.patterns:
            rows = [
                f"- 结构：{p.get('structure', '')} → 更可能是：{'、'.join(p.get('implies') or [])}"
                + (f"；反例：{p['contrast']}" if p.get("contrast") else "")
                for p in self.patterns
            ]
            parts += [
                "",
                "以下是本项目整理的语用模式（用**结构**判断，不要依赖具体措辞；这些是参考规则，不是本次对话的证据，也不要引用其编号）：",
                "\n".join(rows),
            ]
        if self.cues_block:
            parts += ["", self.cues_block]
        parts += [
            "",
            f"请判断被标记为「{TARGET_MARK.strip()}」的那一条消息在**这段上下文**中的意图。",
        ]
        return "\n".join(parts)


def build_intent_input(
    conv: Conversation,
    episode: Episode,
    stats: RedactStats | None = None,
    reference_examples: list[dict] | None = None,
    line_overrides: dict[str, str] | None = None,
    patterns: list[dict] | None = None,
    cues: bool = False,
) -> IntentInput:
    """Format one episode for the model: redacted lines, target marked, references appended.

    `line_overrides` carries the user's edits from the outbound preview (message id -> line),
    so an edit the user made and confirmed is the content actually sent to the model.
    """
    stats = stats if stats is not None else RedactStats()
    names = [(conv.participants.me, "A"), (conv.participants.other, "B")]
    overrides = line_overrides or {}
    lines: list[str] = []
    for m in episode.messages:
        if m.id in overrides:
            line = overrides[m.id]
            if TARGET_MARK not in line and m.id == episode.target.id:
                line += TARGET_MARK
        else:
            line = format_line(m, names, stats)
            if m.id == episode.target.id:
                line += TARGET_MARK
        lines.append(line)
    refs = reference_examples or []
    return IntentInput(
        episode=episode,
        lines=lines,
        valid_ids={m.id for m in episode.messages},
        reference_ids=[str(r.get("example_id", "")) for r in refs if r.get("example_id")],
        reference_block=_format_references(refs),
        patterns=list(patterns or []),
        cues_block=cues_block(episode) if cues else "",
    )


def _format_references(refs: list[dict]) -> str:
    out: list[str] = []
    for r in refs[:6]:
        ctx = " / ".join(str(c) for c in (r.get("context") or [])[-3:])
        tags = "、".join(r.get("pragmatic_tags") or [])
        out.append(f"- 上下文：{ctx}\n  这句话：{r.get('text', '')}\n  语用标签：{tags}")
    return "\n".join(out)


def run_intent(provider: LLMProvider, inp: IntentInput, max_tokens: int = INTENT_MAX_TOKENS, repair_error: str | None = None) -> tuple[dict, CallUsage]:
    user = inp.user_prompt()
    if repair_error:
        user += f"\n\n上一次输出未通过校验：{repair_error}\n请严格按结构重新输出完整 JSON。"
    return provider.complete_json(INTENT_SYSTEM, user, max_tokens=_budget(max_tokens, bool(getattr(provider, "reasoning", False))))


def estimate_intent_tokens(
    conv: Conversation,
    events: list,
    gap_hours: float = 6.0,
    max_targets: int = 8,
    with_references: bool = True,
    reasoning: bool = False,
    line_overrides: dict[str, str] | None = None,
    samples: int = 1,
) -> tuple[int, int, int]:
    """Estimate tokens and call count for the intent stage, for the outbound preview.

    Returns (tokens_in, tokens_out, n_calls). Counting the calls here is what keeps the
    cost confirmation honest: the stage adds one model call per selected episode.
    """
    from rapport.knowledge.reference import load_library, retrieve
    from rapport.llm.budget import estimate_tokens

    sel = select_targets(conv, events, gap_hours=gap_hours, max_targets=max_targets, mode="review")
    if not sel.episodes:
        return 0, 0, 0
    library = load_library() if with_references else None
    tokens_in = 0
    for episode in sel.episodes:
        refs = []
        if library is not None:
            query = " ".join(m.text or "" for m in episode.messages)
            refs = [e.prompt_dict() for e in retrieve(query, library, k=4)]
        inp = build_intent_input(conv, episode, RedactStats(), refs, line_overrides)
        tokens_in += estimate_tokens(INTENT_SYSTEM) + estimate_tokens(inp.user_prompt())
    per_call_out = _budget(INTENT_MAX_TOKENS, reasoning)
    k = max(1, samples)
    return tokens_in * k, per_call_out * len(sel.episodes) * k, len(sel.episodes) * k


# ---------------------------------------------------------------- validation + gate


def _clean_tag_entries(
    values: Any, allowed: tuple[str, ...], valid: set[str]
) -> tuple[list[str], dict[str, list[str]], list[str]]:
    """Accept both the per-tag shape and the legacy bare-string shape.

    Returns (tags_with_evidence, tag_basis, tags_demoted). A tag whose own basis is missing or
    cites nothing valid is demoted: the reading keeps it as a candidate instead of a claim.
    """
    if not isinstance(values, list):
        return [], {}, []
    tags: list[str] = []
    basis: dict[str, list[str]] = {}
    demoted: list[str] = []
    for v in values:
        if isinstance(v, dict):
            tag = str(v.get("tag") or "").strip()
            ids = _ids(v.get("basis_ids"), valid)
        else:
            tag = str(v).strip()
            ids = []
        if tag not in allowed or tag in tags or tag in demoted:
            continue
        if not ids:
            demoted.append(tag)
            continue
        tags.append(tag)
        basis[tag] = ids
    return tags, basis, demoted


def _ids(values: Any, valid: set[str]) -> list[str]:
    if not isinstance(values, list):
        return []
    return [str(v) for v in values if isinstance(v, str) and v in valid]


def _conf(v: Any, default: Confidence = Confidence.low) -> Confidence:
    try:
        return Confidence(str(v))
    except ValueError:
        return default


def validate_intent(
    data: dict,
    inp: IntentInput,
    independent_units: int = 1,
    min_units: int = MIN_INDEPENDENT_UNITS,
) -> IntentReading:
    """Turn raw model output into a contract object, enforcing the guard rails.

    Illegal evidence ids are dropped. A reading with no surviving evidence becomes
    `ambiguous` rather than a confident claim. Negative readings are demoted to
    candidates unless enough independent snippets back them.
    """
    if not isinstance(data, dict):
        data = {}
    target = inp.episode.target
    evidence = _ids(data.get("evidence_ids"), inp.valid_ids)
    counter = _ids(data.get("counter_evidence_ids"), inp.valid_ids)
    tags, tag_basis, demoted_tags = _clean_tag_entries(data.get("pragmatic_tags"), PRAGMATIC_TAGS, inp.valid_ids)
    emotions, emotion_basis, demoted_emotions = _clean_tag_entries(data.get("emotions"), EMOTIONS, inp.valid_ids)
    tag_basis = {**tag_basis, **emotion_basis}

    candidates: list[IntentCandidate] = []
    raw_candidates = data.get("candidates") if isinstance(data.get("candidates"), list) else []
    for c in raw_candidates:
        if not isinstance(c, dict) or not str(c.get("reading", "")).strip():
            continue
        candidates.append(
            IntentCandidate(
                reading=str(c["reading"])[:80],
                likelihood=_conf(c.get("likelihood")),
                basis_ids=_ids(c.get("basis_ids"), inp.valid_ids),
            )
        )

    boundary = str(data.get("boundary_status") or "none")
    if boundary not in ("none", "explicit_decline", "stop_contact", "safety_concern"):
        boundary = "none"

    strength = _conf(data.get("evidence_strength"))
    raw_conf = data.get("model_confidence_raw")
    try:
        model_conf = max(0.0, min(1.0, float(raw_conf)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        model_conf = None

    ambiguous = bool(data.get("ambiguous", False))
    abstain = data.get("abstain_reason")
    abstain = str(abstain)[:120] if abstain else None
    if not evidence and not ambiguous:
        # a claim with no surviving evidence is not a claim
        ambiguous = True
        abstain = abstain or "模型没有给出可核验的证据，已按不确定处理。"

    if demoted_tags or demoted_emotions:
        for t in [*demoted_tags, *demoted_emotions]:
            candidates.append(
                IntentCandidate(reading=f"{t}（该标签没有给出自己的证据，已降级为参考）"[:80], likelihood=Confidence.low, basis_ids=evidence)
            )
        candidates = candidates[:3]
    reading = IntentReading(
        msg_id=target.id,
        episode_id=inp.episode.id,
        speaker="me" if target.sender == Sender.me else "other",
        surface_text=(target.text or "")[:160],
        literal_affect=str(data.get("literal_affect") or "")[:30],
        pragmatic_tags=tags,
        emotions=emotions,
        tag_basis=tag_basis,
        boundary_status=boundary,
        candidates=candidates[:3],
        evidence_strength=strength,
        model_confidence_raw=model_conf,
        evidence_ids=evidence,
        counter_evidence_ids=counter,
        reference_example_ids=[r for r in inp.reference_ids if r][:6],
        post_hoc=bool(inp.episode.post_hoc),
        context_mode="review" if inp.episode.post_hoc else "reply",
        ambiguous=ambiguous,
        abstain_reason=abstain,
        independent_units=max(0, independent_units),
        consistency=_consistency(data.get("consistency")),
        samples=int(data.get("samples") or 1),
    )
    # v0.8: no negative gate. independent_units / consistency stay on the reading as confidence
    # information for the UI; nothing is demoted.
    _ = min_units
    return reading


def _consistency(v: Any) -> float | None:
    try:
        return None if v is None else max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return None


def apply_negative_gate(reading: IntentReading, min_units: int = MIN_INDEPENDENT_UNITS) -> IntentReading:
    # v0.8: no-op kept for callers; the original gate lives on as `_legacy_negative_gate`.
    _ = min_units
    return reading


def _legacy_negative_gate(reading: IntentReading, min_units: int = MIN_INDEPENDENT_UNITS) -> IntentReading:
    """Hold back coldness-flavoured readings until >= `min_units` independent snippets exist.

    The reading is not deleted: it is moved into `candidates` (with a note) so the user can
    still see it, but it never becomes a headline claim. Measured on 2026-09-15 the model
    over-predicted this class, and a product that says "they are cooling off" on one
    ambiguous line is worse than a product that stays quiet.
    """
    if reading.independent_units >= min_units:
        return reading
    # v0.5 §3B: a negative reading that every sampled draw agreed on has earned its place even in
    # a single snippet. Measured 2026-09-19 (docs/EVAL.md §15.3): with one snippet per case the
    # gate zeroed every negative class on real material, right or wrong. Unanimity across draws is
    # the second key; a single deterministic call (samples == 1) still needs the snippets.
    if reading.samples >= 2 and (reading.consistency or 0.0) >= CONSISTENCY_UNANIMOUS:
        return reading
    held = [t for t in reading.pragmatic_tags if t in NEGATIVE_PRAGMATIC]
    held += [e for e in reading.emotions if e in NEGATIVE_EMOTIONS]
    if not held:
        return reading
    reading.pragmatic_tags = [t for t in reading.pragmatic_tags if t not in NEGATIVE_PRAGMATIC]
    reading.emotions = [e for e in reading.emotions if e not in NEGATIVE_EMOTIONS]
    note = f"（该判断只在当前片段出现，独立片段不足 {min_units} 个" + (f"，{reading.samples} 次采样也未一致" if reading.samples >= 2 else "") + "，已降级为参考）"
    existing = [c.reading for c in reading.candidates]
    for h in held:
        text = f"{h}{note}"
        if text not in existing:
            reading.candidates.append(IntentCandidate(reading=text[:80], likelihood=Confidence.low, basis_ids=reading.evidence_ids))
    reading.suppressed_reason = (
        f"「{'、'.join(held)}」类判断在样本中出现过模型系统性高估，"
        f"本次只有 {reading.independent_units} 个独立片段支持"
        + (f"，{reading.samples} 次采样也未一致（一致度 {reading.consistency}）" if reading.samples >= 2 else "")
        + "，暂不作为结论展示。"
    )
    if not reading.pragmatic_tags and not reading.emotions:
        reading.ambiguous = True
        reading.abstain_reason = reading.abstain_reason or "扣掉证据不足的负面判断后，没有可上浮的结论。"
    return reading
