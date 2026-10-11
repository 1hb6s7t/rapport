"""Memory authorization: what a retrieval result is allowed to become.

Retrieval returns candidates. Nothing from the vector index is trusted on its own — every hit is
re-checked against the authoritative store before it may enter a prompt or a draft, because the
index can be stale (the entry was edited), out of scope (belongs to another relationship),
switched off, deleted, or simply from the future relative to the moment being simulated.

Two rules are easy to get wrong and are therefore enforced here in one place:

1. **Off means no retrieval at all.** `retrieve_if_enabled(False, ...)` never calls the retriever,
   rather than retrieving and then discarding. A closed switch must not touch the index.
2. **A plan never becomes a fact.** `kind == plan` stays a plan in the rendered text; a `fact`
   whose `occurred_at` is in the future cannot be presented as something that already happened.

Ported and adapted from the v2 research package's reference implementation
(`reference_code/core_rules.py`, `authorize_memories` / `retrieve_if_enabled` /
`validate_exact_clause`).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from math import isfinite

from rapport.errors import RapportError
from rapport.schemas.diary import DiaryEntry, DiaryKind, MemoryHit

DEFAULT_LIMIT = 3


class StrictFactError(RapportError, ValueError):
    """Raised when a strict-mode clause is not an approved, verbatim diary sentence."""

    def __init__(self, msg: str):
        RapportError.__init__(self, "strict_fact_rejected", msg, status=422)


@dataclass
class AuthorizationResult:
    entries: list[DiaryEntry] = field(default_factory=list)
    dropped: dict[str, int] = field(default_factory=dict)

    def _drop(self, reason: str) -> None:
        self.dropped[reason] = self.dropped.get(reason, 0) + 1

    @property
    def all_dropped(self) -> int:
        return sum(self.dropped.values())


def retrieve_if_enabled(enabled: bool, retriever: Callable[[], Sequence[MemoryHit]]) -> list[MemoryHit]:
    """A closed switch means the retriever is not called — not called-and-ignored."""
    return list(retriever()) if enabled else []


def _aware(value: datetime | None, what: str) -> None:
    if value is None:
        return
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{what} must be timezone-aware")


def authorize_memories(
    hits: Sequence[MemoryHit],
    store: Mapping[str, DiaryEntry],
    relationship_id: str,
    as_of: datetime,
    memory_enabled: bool,
    limit: int = DEFAULT_LIMIT,
) -> AuthorizationResult:
    """Re-check hits against the authoritative store, newest similarity first."""
    result = AuthorizationResult()
    if not memory_enabled:
        return result
    if limit < 0:
        raise ValueError("limit must not be negative")
    if limit == 0:
        return result
    _aware(as_of, "as_of")

    for hit in sorted(hits, key=lambda h: h.similarity, reverse=True):
        if not isfinite(hit.similarity):
            raise ValueError("similarity must be finite")
        entry = store.get(hit.entry_id)
        if entry is None:
            result._drop("not_found")
            continue
        if entry.deleted:
            result._drop("deleted")
            continue
        if not entry.enabled:
            result._drop("disabled")
            continue
        if entry.revision != hit.revision:
            result._drop("stale_revision")
            continue
        if entry.id in {e.id for e in result.entries}:
            result._drop("duplicate")
            continue
        if relationship_id not in entry.allowed_relationships:
            # an empty scope therefore permits nothing, which is the safe default
            result._drop("outside_scope")
            continue
        _aware(entry.recorded_at, "recorded_at")
        _aware(entry.occurred_at, "occurred_at")
        if entry.recorded_at > as_of:
            result._drop("future_diary")
            continue
        if entry.kind == DiaryKind.fact and entry.occurred_at is not None and entry.occurred_at > as_of:
            result._drop("future_fact")
            continue
        result.entries.append(entry)
        if len(result.entries) >= limit:
            break
    return result


def validate_exact_clause(entry: DiaryEntry, revision: int, clause: str) -> None:
    """Strict mode: the clause must be user-approved and present verbatim in the entry.

    This is deliberately not a semantic-entailment check. It guarantees only that the sentence
    came from the diary unchanged; it does not vouch for the rest of a freely generated reply.
    """
    if entry.deleted or not entry.enabled or revision != entry.revision:
        raise StrictFactError("这条日记已被删除、被关闭或已被修改，旧内容不可再使用")
    if not clause or entry.approved_clause != clause or clause not in entry.text:
        raise StrictFactError("该短句不是用户批准的原文片段")


_PREFIX = {
    DiaryKind.fact: "说到这个，我之前记过这样一件事：",
    DiaryKind.plan: "说到这个，我有个还没完成的计划：",
    DiaryKind.feeling: "说到这个，我之前记下过一个感受：",
}


def render_strict_reply(entry: DiaryEntry, relationship_id: str, as_of: datetime) -> dict:
    """Fixed template + the approved clause. No paraphrase, no invention, always reviewable."""
    hit = MemoryHit(entry_id=entry.id, revision=entry.revision, similarity=1.0)
    authorized = authorize_memories([hit], {entry.id: entry}, relationship_id, as_of, True, limit=1)
    if not authorized.entries:
        raise StrictFactError("这条日记当前不可用于草稿")
    if not entry.approved_clause:
        raise StrictFactError("这条日记没有用户批准的短句")
    validate_exact_clause(entry, entry.revision, entry.approved_clause)
    return {
        "text": _PREFIX[entry.kind] + entry.approved_clause,
        "memory_refs": [{"id": entry.id, "revision": entry.revision, "kind": entry.kind.value}],
        "mode": "strict_slot",
        "requires_user_review": True,
        "note": "严格模式只允许原样使用你批准的短句，不会改写，也不会补写日记里没有的事。",
    }
