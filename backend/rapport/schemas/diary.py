"""Diary contract (v2 M09).

A diary entry is the user's own record, and it is the most sensitive thing this project stores.
The rules below come from the v2 research package's reference implementation, which is unusually
careful here — these are the parts worth porting verbatim rather than reinventing:

- an entry has a **revision**; editing bumps it, and retrieval results carrying an older revision
  are stale and must be dropped rather than silently used;
- `kind` separates an event that happened (`fact`) from something planned (`plan`) and from a
  feeling (`feeling`). A plan must never be rendered as something that already happened, and a
  future fact must not be treated as completed;
- `enabled` is a per-entry switch, and `allowed_relationships` is the isolation scope: an empty
  scope permits nothing, so a new entry cannot leak into an unrelated relationship by default;
- `approved_clause` is the exact sentence the user approved for use in a generated reply. Strict
  mode may only reproduce an approved clause verbatim — never a paraphrase.

Timestamps are timezone-aware. Naive datetimes are rejected rather than guessed at.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class DiaryKind(StrEnum):
    fact = "fact"  # something that happened
    plan = "plan"  # something intended, not yet done
    feeling = "feeling"  # an internal state, not an event


KIND_LABELS = {"fact": "发生的事", "plan": "计划", "feeling": "感受"}


class DiaryEntry(BaseModel):
    id: str
    revision: int = 1
    text: str
    recorded_at: datetime  # when the user wrote it down
    occurred_at: datetime | None = None  # when the thing happened / will happen
    kind: DiaryKind = DiaryKind.fact
    enabled: bool = True
    allowed_relationships: frozenset[str] = Field(default_factory=frozenset)
    approved_clause: str | None = None
    deleted: bool = False

    @field_validator("text")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("diary text cannot be empty")
        return v

    @field_validator("recorded_at", "occurred_at")
    @classmethod
    def _aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and (v.tzinfo is None or v.utcoffset() is None):
            raise ValueError("timestamps must be timezone-aware")
        return v

    @field_validator("allowed_relationships", mode="before")
    @classmethod
    def _scope(cls, v):
        if v is None:
            return frozenset()
        return frozenset(v) if not isinstance(v, frozenset) else v

    def to_record(self) -> dict:
        return {
            "id": self.id,
            "revision": self.revision,
            "text": self.text,
            "kind": self.kind.value,
            "enabled": self.enabled,
            "recorded_at": self.recorded_at.isoformat(),
            "occurred_at": self.occurred_at.isoformat() if self.occurred_at else None,
            "allowed_relationships": sorted(self.allowed_relationships),
            "approved_clause": self.approved_clause,
        }


class MemoryHit(BaseModel):
    """One retrieval candidate. Carries the revision it was built from, so staleness is detectable."""

    entry_id: str
    revision: int
    similarity: float


class DiaryIn(BaseModel):
    text: str
    kind: DiaryKind = DiaryKind.fact
    recorded_at: datetime | None = None
    occurred_at: datetime | None = None
    allowed_relationships: list[str] = Field(default_factory=list)
    approved_clause: str | None = None


class DiaryPatch(BaseModel):
    text: str | None = None
    kind: DiaryKind | None = None
    occurred_at: datetime | None = None
    allowed_relationships: list[str] | None = None
    approved_clause: str | None = None
    enabled: bool | None = None
