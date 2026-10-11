"""Session segmentation: messages separated by >= gap hours start a new session.

Without reliable timestamps the whole conversation is one session (segments==1),
which is later reported as a coverage limitation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from rapport.schemas.message_v1 import Message


@dataclass
class Session:
    index: int
    messages: list[Message]

    @property
    def first(self) -> Message:
        return self.messages[0]

    @property
    def last(self) -> Message:
        return self.messages[-1]


def split_sessions(events: list[Message], gap_hours: float = 6.0) -> list[Session]:
    if not events:
        return []
    gap = timedelta(hours=gap_hours)
    sessions: list[Session] = []
    current: list[Message] = [events[0]]
    prev_ts = events[0].ts if events[0].has_reliable_ts else None
    for m in events[1:]:
        if m.has_reliable_ts and prev_ts is not None and m.ts - prev_ts >= gap:
            sessions.append(Session(len(sessions), current))
            current = []
        current.append(m)
        if m.has_reliable_ts:
            prev_ts = m.ts
    sessions.append(Session(len(sessions), current))
    return sessions


def turns(events: list[Message]) -> list[list[Message]]:
    """Merge consecutive messages by the same sender into turns."""
    out: list[list[Message]] = []
    for m in events:
        if out and out[-1][0].sender == m.sender:
            out[-1].append(m)
        else:
            out.append([m])
    return out
