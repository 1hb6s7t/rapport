"""Episode construction for contextual intent reading (v2 M03).

An *episode* is the independent context unit a judgement is allowed to rely on:

- Base unit: a session segment (messages separated by >= `session_gap_hours`).
- Split further at quote boundaries and at long silences inside a session, so a judgement
  never silently mixes two unrelated conversations.

Two modes, and the distinction matters for honesty:

- `reply`  — only messages with event_time <= the target may be used. This is what a real
  "what should I say next" decision is allowed to know. `post_hoc = False`.
- `review` — a symmetric window around the target, i.e. hindsight is available. The result
  is labelled `post_hoc = True` so the UI never presents a retrospective reading as if it
  were a prediction made at the time.

Topic segmentation here is session + quote based, NOT semantic: we do not claim to detect
conversation topics. Everything this module returns is a window over real messages, and the
episode id is stable so evidence can be deduplicated per episode by `scoring.evidence`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from rapport.features.sessions import split_sessions
from rapport.schemas.message_v1 import Conversation, Message, Sender

DEFAULT_MAX_MESSAGES = 40
LONG_SILENCE_HOURS = 12.0


@dataclass
class Episode:
    id: str
    messages: list[Message]
    target: Message
    as_of: str | None = None  # ISO time of the target; reply mode never looks past it
    post_hoc: bool = False
    session_index: int = 0
    truncated: bool = False  # window was cut short by max_messages

    @property
    def target_ids(self) -> list[str]:
        return [m.id for m in self.messages]

    def lines(self, formatter) -> list[str]:
        """Format messages for a prompt. `formatter(message, episode)` decides the style."""
        return [formatter(m, self) for m in self.messages]


def _split_in_session(group: list[Message]) -> list[list[Message]]:
    """Split one session at quote boundaries and long silences (>= LONG_SILENCE_HOURS)."""
    blocks: list[list[Message]] = []
    current: list[Message] = []
    for m in group:
        if current:
            prev = current[-1]
            near_quote = bool(m.quoted_text or prev.quoted_text)
            long_silence = (
                m.has_reliable_ts and prev.has_reliable_ts and (m.ts - prev.ts) >= timedelta(hours=LONG_SILENCE_HOURS)
            )
            if m.sender == Sender.system or near_quote or long_silence:
                blocks.append(current)
                current = []
        current.append(m)
    if current:
        blocks.append(current)
    return blocks


def build_episodes(conv: Conversation, events: list[Message], gap_hours: float = 6.0) -> list[list[Message]]:
    """All conversation blocks of a conversation, in order."""
    blocks: list[list[Message]] = []
    for sess in split_sessions(events, gap_hours):
        blocks.extend(_split_in_session(sess.messages))
    return [b for b in blocks if any(m.sender in (Sender.me, Sender.other) for m in b)]


def episode_for(
    conv: Conversation,
    blocks: list[list[Message]],
    target: Message,
    *,
    mode: str = "review",
    max_messages: int = DEFAULT_MAX_MESSAGES,
) -> Episode | None:
    """Window around `target` inside its own block."""
    idx = next((i for i, b in enumerate(blocks) if any(m.id == target.id for m in b)), None)
    if idx is None:
        return None
    block = blocks[idx]
    pos = next(i for i, m in enumerate(block) if m.id == target.id)
    if mode == "reply":
        window = block[max(0, pos - max_messages + 1) : pos + 1]
        post_hoc = False
    else:
        half = max_messages // 2
        window = block[max(0, pos - half) : pos + half + 1]
        post_hoc = True
    truncated = len(window) < len(block) and (window[0] is not block[0] or window[-1] is not block[-1])
    return Episode(
        id=f"{conv.id}:b{idx}:{target.id}",
        messages=window,
        target=target,
        as_of=target.ts.isoformat() if target.ts else None,
        post_hoc=post_hoc,
        session_index=idx,
        truncated=truncated,
    )


@dataclass
class TargetSelection:
    episodes: list[Episode] = field(default_factory=list)
    skipped_reason: str | None = None


def select_targets(
    conv: Conversation,
    events: list[Message],
    *,
    gap_hours: float = 6.0,
    max_targets: int = 8,
    mode: str = "review",
    boundary_ids: list[str] | None = None,
) -> TargetSelection:
    """Pick the messages worth reading intent for, cheapest-first.

    Priority: messages that triggered a boundary hit, then the other person's message after
    each silence (a "reply after a gap" is where intent is most distinguishable), then the
    last other-person message of each block. Never more than `max_targets`.
    """
    blocks = build_episodes(conv, events, gap_hours)
    if not blocks:
        return TargetSelection([], "材料中没有可用的双方消息")
    by_id = {m.id: m for m in events}
    picked: list[Message] = []
    seen: set[str] = set()

    def take(m: Message | None) -> None:
        if m is None or m.id in seen or not m.is_text or m.sender != Sender.other:
            return
        seen.add(m.id)
        picked.append(m)

    for bid in boundary_ids or []:
        take(by_id.get(bid))
    for block in blocks:
        prev = None
        for m in block:
            if m.sender == Sender.other and prev is not None and prev.sender == Sender.me:
                take(m)
                break
            prev = m
    for block in blocks:
        others = [m for m in block if m.sender == Sender.other and m.is_text]
        if others:
            take(others[-1])
    out: list[Episode] = []
    for m in picked[:max_targets]:
        ep = episode_for(conv, blocks, m, mode=mode)
        if ep is not None:
            out.append(ep)
    if not out:
        return TargetSelection([], "没有足够的对方文本可用于语义判断")
    return TargetSelection(out, None)
