"""The analysis status machine — one place that says which status may follow which.

Before this module the status strings were repeated in the runner, the API and the startup
reconcile, and the lists drifted: the `intent` stage (added in v0.2) was missing from the
"active" tuple, so a service restart during that stage left the row stuck as `intent`
instead of `interrupted`. Every consumer now imports the sets from here.
"""

from __future__ import annotations

from enum import StrEnum


class AnalysisStatus(StrEnum):
    created = "created"  # BYOK: waiting for the outbound preview to be confirmed
    queued = "queued"
    extracting = "extracting"
    synthesizing = "synthesizing"
    validating = "validating"
    intent = "intent"
    assembling = "assembling"
    completed = "completed"
    insufficient_data = "insufficient_data"
    failed = "failed"
    cancelled = "cancelled"
    interrupted = "interrupted"


S = AnalysisStatus

# Stages the worker moves through. Progress inside the pipeline may visit them in any order
# (a repair retry goes validating -> synthesizing), so RUNNING -> RUNNING is always allowed.
RUNNING: frozenset[str] = frozenset({S.extracting, S.synthesizing, S.validating, S.intent, S.assembling})
# Rows the worker owns or will claim. A restart marks these interrupted.
ACTIVE: frozenset[str] = frozenset(RUNNING | {S.queued})
# Rows that still need the raw material (retention purge must not delete under them).
NEEDS_MATERIAL: frozenset[str] = frozenset(ACTIVE | {S.created})
TERMINAL: frozenset[str] = frozenset({S.completed, S.insufficient_data, S.cancelled})
RETRYABLE: frozenset[str] = frozenset({S.failed, S.interrupted})

_FROM_RUNNING = frozenset(RUNNING | {S.completed, S.insufficient_data, S.failed, S.cancelled, S.interrupted})
TRANSITIONS: dict[str, frozenset[str]] = {
    S.created: frozenset({S.queued, S.cancelled}),
    S.queued: frozenset(RUNNING | {S.cancelled, S.failed, S.interrupted}),
    S.extracting: _FROM_RUNNING,
    S.synthesizing: _FROM_RUNNING,
    S.validating: _FROM_RUNNING,
    S.intent: _FROM_RUNNING,
    S.assembling: _FROM_RUNNING,
    S.failed: frozenset({S.queued}),
    S.interrupted: frozenset({S.queued, S.cancelled}),
    S.completed: frozenset(),
    S.insufficient_data: frozenset(),
    S.cancelled: frozenset(),
}


class IllegalTransition(ValueError):
    def __init__(self, before: str, after: str):
        super().__init__(f"analysis status cannot go from {before} to {after}")
        self.before = before
        self.after = after


def can_transition(before: str, after: str) -> bool:
    if before == after:
        return True
    return after in TRANSITIONS.get(before, frozenset())


def check_transition(before: str, after: str) -> None:
    if not can_transition(before, after):
        raise IllegalTransition(before, after)


def is_active(status: str) -> bool:
    return status in ACTIVE
