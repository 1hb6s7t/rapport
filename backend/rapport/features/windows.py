"""Windowing for trend buckets and LLM extraction windows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from rapport.schemas.message_v1 import Message


@dataclass
class Window:
    index: int
    label: str
    start: datetime | None
    end: datetime | None
    messages: list[Message]


def trend_buckets(events: list[Message], has_time: bool, n_fallback: int = 6) -> tuple[str, list[Window]]:
    """Bucket events by day/week/month when timed, else equal chunks by order."""
    timed = [m for m in events if m.has_reliable_ts]
    if not has_time or len(timed) < 2:
        return "session", _equal_chunks(events, n_fallback)
    start, end = min(m.ts for m in timed), max(m.ts for m in timed)
    span_days = (end - start).days + 1
    if span_days <= 14:
        kind, step = "day", timedelta(days=1)
        floor = lambda d: d.replace(hour=0, minute=0, second=0, microsecond=0)  # noqa: E731
    elif span_days <= 120:
        kind, step = "week", timedelta(days=7)
        floor = lambda d: (d - timedelta(days=d.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)  # noqa: E731
    else:
        kind, step = "month", None
        floor = lambda d: d.replace(day=1, hour=0, minute=0, second=0, microsecond=0)  # noqa: E731
    buckets: dict[datetime, list[Message]] = {}
    last_key: datetime | None = None
    for m in events:
        if m.has_reliable_ts:
            key = floor(m.ts)
            last_key = key
        elif last_key is not None:
            key = last_key  # untimed message inherits previous bucket
        else:
            continue
        buckets.setdefault(key, []).append(m)
    out: list[Window] = []
    for i, key in enumerate(sorted(buckets)):
        if kind == "month":
            nxt = (key.replace(day=28) + timedelta(days=4)).replace(day=1)
            label = key.strftime("%Y-%m")
        else:
            nxt = key + step
            label = key.strftime("%m-%d") if kind == "day" else f"{key.strftime('%m-%d')}周"
        out.append(Window(i, label, key, nxt, buckets[key]))
    return kind, out


def _equal_chunks(events: list[Message], n: int) -> list[Window]:
    if not events:
        return []
    n = max(1, min(n, len(events) // 8 or 1))
    size = -(-len(events) // n)
    out = []
    for i in range(n):
        chunk = events[i * size : (i + 1) * size]
        if chunk:
            out.append(Window(i, f"第{i + 1}段", None, None, chunk))
    return out


def extraction_windows(events: list[Message], size: int = 120, overlap: int = 10) -> list[Window]:
    """Sliding windows for LLM extraction over text-bearing events."""
    if not events:
        return []
    out = []
    i = 0
    idx = 0
    while i < len(events):
        chunk = events[i : i + size]
        out.append(Window(idx, f"w{idx}", chunk[0].ts, chunk[-1].ts, chunk))
        idx += 1
        if i + size >= len(events):
            break
        i += size - overlap
    return out
