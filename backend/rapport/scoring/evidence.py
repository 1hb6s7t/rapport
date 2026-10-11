"""Evidence ledger and effective sample size.

Ported from the v2 research package's reference implementation
(`relationship_agent_research_v2.0_2026-09-15/reference_code/core_rules.py`,
`Evidence` + `effective_evidence`), adapted to this codebase. The reference file states:

    "函数仅对已经解析为 canonical ID 的事件去重，不会识别重复截图、事实真假或语义支持。
     同一现实事件须先由导入/人工确认形成稳定 ID。"

Core rule this module enforces: **only confirmed, observed-chat evidence counts.**
AI drafts and public reference examples must never raise the effective sample size —
otherwise the model could bootstrap its own output into "facts".

All numeric parameters (episode cap) are product hypotheses, not validated constants.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

ORIGIN_OBSERVED = "observed_chat"
ORIGIN_AI_DRAFT = "ai_draft"
ORIGIN_PUBLIC_EXAMPLE = "public_example"

DEFAULT_EPISODE_CAP = 5.0


class EvidenceError(ValueError):
    """Raised when the ledger is fed contradictory or malformed evidence."""


@dataclass(frozen=True)
class Evidence:
    """One unit of evidence about one canonical event.

    source_event_id: stable id of the real-world event (see `state.event_fingerprint`).
    episode_id:      the independent context unit it belongs to (session / topic block).
    kind:            what was observed, e.g. "reply", "initiative", "care", "invitation".
    quality:         0..1 confidence that this observation is usable evidence.
    origin:          observed_chat | ai_draft | public_example.
    confirmed:       human/import confirmation; unconfirmed rows never count.
    """

    source_event_id: str
    episode_id: str
    kind: str
    quality: float
    origin: str = ORIGIN_OBSERVED
    confirmed: bool = True


def effective_evidence(items, episode_cap: float = DEFAULT_EPISODE_CAP) -> float:
    """Effective sample size N_eff.

        N_eff = sum over episodes of min(episode_cap, sum of unique qualities)

    - Unique key is (source_event_id, kind): re-imports, forwards and repeated model
      explanations of the same event collapse to one entry (highest quality wins).
    - Evidence whose origin is not `observed_chat`, or which is unconfirmed, is dropped.
    - The per-episode cap keeps many highly correlated messages from inflating the count.
    """
    if not isfinite(episode_cap) or episode_cap <= 0:
        raise EvidenceError("episode_cap must be positive and finite")
    unique: dict[tuple[str, str], Evidence] = {}
    for x in items:
        if not isfinite(x.quality) or not 0 <= x.quality <= 1:
            raise EvidenceError("quality must be in [0, 1]")
        if not x.source_event_id or not x.episode_id or not x.kind:
            raise EvidenceError("evidence source_event_id, episode_id and kind are required")
        if x.origin != ORIGIN_OBSERVED or not x.confirmed:
            continue
        key = (x.source_event_id, x.kind)
        old = unique.get(key)
        if old is not None and old.episode_id != x.episode_id:
            raise EvidenceError("one canonical event cannot move between episodes silently")
        if old is None or x.quality > old.quality:
            unique[key] = x
    per_episode: dict[str, float] = {}
    for x in unique.values():
        per_episode[x.episode_id] = per_episode.get(x.episode_id, 0.0) + x.quality
    return sum(min(episode_cap, v) for v in per_episode.values())


# Quality of one observed event as evidence (v0.4). The reference definition of q_i is
# "attribution correct, context complete, independent, confirmed"; attribution and
# confirmation are enforced upstream, so what varies per event is how much *context* it
# carries: a timed text message can be placed in a session and a reply chain, an untimed one
# only in message order, and a media placeholder says almost nothing about intent.
# Product hypotheses, not psychometrics; the per-episode cap still applies on top.
TEXT_TYPES = frozenset({"text"})
QUALITY_BY_PRECISION: dict[str, float] = {"second": 1.0, "minute": 1.0, "label": 0.9, "day": 0.9, "none": 0.8}
# v0.6: a media placeholder whose content nobody has read is not evidence of intent. Before
# this, 0.5 per image meant a few thousand unread photos drove N_eff up, alpha down to 0 and
# the shrinkage to 0 — "enough evidence" on the strength of pictures no one had looked at.
# Media that *has* been read (OCR'd screenshot, transcribed voice) is text by the time it
# reaches the ledger, so it never hits this branch.
QUALITY_MEDIA = 0.0
QUALITY_MEDIA_UNREAD = QUALITY_MEDIA  # explicit name for callers that mean "unobserved"


def event_quality(kind: str | None, ts_precision: str | None) -> float:
    if (kind or "text") in TEXT_TYPES:
        return QUALITY_BY_PRECISION.get(ts_precision or "none", 0.8)
    return QUALITY_MEDIA_UNREAD


MEDIA_KINDS = frozenset({"image", "voice", "video", "sticker", "file"})


def evidence_breakdown(rows) -> dict[str, int]:
    """How many confirmed observed events are countable text vs unread media vs other.

    Diagnostic only: tells the user "N media placeholders were not read and do not count",
    so a high message count with a low N_eff is explainable instead of mysterious.
    """
    out = {"text": 0, "media_unobserved": 0, "other": 0}
    for r in rows:
        if (getattr(r, "origin", None) or ORIGIN_OBSERVED) != ORIGIN_OBSERVED or not getattr(r, "confirmed", False):
            continue
        kind = getattr(r, "type", None) or "text"
        if kind in TEXT_TYPES:
            out["text"] += 1
        elif kind in MEDIA_KINDS:
            out["media_unobserved"] += 1
        else:
            out["other"] += 1
    return out


def ledger_from_events(rows) -> list[Evidence]:
    """Build ledger entries from stored canonical events.

    One event = one observation unit (kind = its message type, quality from `event_quality`).
    Confirmation is an explicit user act: unconfirmed rows stay in the ledger but are ignored by
    N_eff, which is what makes re-imports, AI drafts and public examples weightless.
    """
    out: list[Evidence] = []
    for r in rows:
        out.append(
            Evidence(
                source_event_id=r.fingerprint,
                episode_id=r.episode_id or "unassigned",
                kind=r.type or "text",
                quality=event_quality(r.type, getattr(r, "ts_precision", None)),
                origin=r.origin or ORIGIN_OBSERVED,
                confirmed=bool(getattr(r, "confirmed", False)),
            )
        )
    return out


def n_eff_from_events(rows, episode_cap: float = DEFAULT_EPISODE_CAP) -> float:
    return effective_evidence(ledger_from_events(rows), episode_cap=episode_cap)


def count_episodes(items, min_quality: float = 0.0) -> int:
    """Number of episodes holding at least one countable observation of quality >= min_quality.

    Used by the "at least two independent context snippets" display gate.
    """
    seen: set[str] = set()
    for x in items:
        if x.origin == ORIGIN_OBSERVED and x.confirmed and x.quality >= min_quality and x.episode_id:
            seen.add(x.episode_id)
    return len(seen)
