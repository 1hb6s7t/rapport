"""Background jobs. `JobRunner` is imported lazily so `jobs.status` stays import-light
(state -> jobs.status must not drag the runner, which itself needs state)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rapport.jobs.runner import JobRunner

__all__ = ["JobRunner"]


def __getattr__(name: str):
    if name == "JobRunner":
        from rapport.jobs.runner import JobRunner

        return JobRunner
    raise AttributeError(name)
