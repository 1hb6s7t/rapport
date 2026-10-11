"""Invitation chains: offer → answer → settled time (v0.5, plan §1A).

Why this exists: the future dimension counted how often the *other* person's messages matched a
list of future-ish words. That number cannot tell these two apart:

    我: 周末一起吃饭吧        B: 改天吧        (×10)
    我: 周末一起吃饭吧        B: 好啊，周六六点老地方   (×10)

Both scored as "future intent", because 「一起」「周末」 appear either way — and 「改天」 counted as
a future word too, even though it is the polite form of a no. The result was a dimension that
mostly measured *my* proposing, and the warm/cooling fixtures landed 7 points apart.

So the act is modelled as a chain instead: my offer, their answer (accept / alt_time / decline /
deflect / none), and whether a concrete time came out of it. The rates over those chains are what
the future dimension reads, and the word-frequency fallback stays only for material with no chain
at all (a one-off paste, a single screenshot).

Rule-based and deterministic; every judgement carries the message ids behind it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rapport.features import lexicon as lx
from rapport.schemas.message_v1 import Message, Sender

ACCEPT = "accept"
ALT_TIME = "alt_time"
DECLINE = "decline"
DEFLECT = "deflect"
NONE = "none"

# An answer has to be plausibly about *this* offer: soon after it, and before I offer again.
MAX_GAP_HOURS = 48
MAX_LOOKAHEAD = 6
# How far after an acceptance a settled time may still show up (both sides count — "周六六点" is
# usually the other person answering my "几点?").
COMMIT_LOOKAHEAD = 6


@dataclass
class Invitation:
    propose_id: str
    propose_text: str
    response: str = NONE
    response_id: str | None = None
    commit_ids: list[str] = field(default_factory=list)

    def d(self) -> dict:
        return {
            "propose_id": self.propose_id,
            "propose_text": self.propose_text,
            "response": self.response,
            "response_id": self.response_id,
            "commit_ids": list(self.commit_ids),
        }


@dataclass
class InvitationReport:
    """Counts plus the chains themselves, so a number can always be traced back to messages."""

    chains: list[Invitation] = field(default_factory=list)
    my_texts: int = 0
    their_texts: int = 0
    their_proposals: int = 0
    their_proposal_ids: list[str] = field(default_factory=list)

    @property
    def proposed(self) -> int:
        return len(self.chains)

    def _count(self, kind: str) -> int:
        return sum(1 for c in self.chains if c.response == kind)

    @property
    def answered(self) -> int:
        return sum(1 for c in self.chains if c.response in (ACCEPT, ALT_TIME, DECLINE, DEFLECT))

    @property
    def accepted(self) -> int:
        return self._count(ACCEPT)

    @property
    def alt_timed(self) -> int:
        return self._count(ALT_TIME)

    @property
    def declined(self) -> int:
        return self._count(DECLINE)

    @property
    def committed(self) -> int:
        return sum(1 for c in self.chains if c.response == ACCEPT and c.commit_ids)

    def d(self) -> dict:
        return {
            "proposed": self.proposed,
            "answered": self.answered,
            "accepted": self.accepted,
            "alt_timed": self.alt_timed,
            "declined": self.declined,
            "deflected": self._count(DEFLECT),
            "unanswered": self._count(NONE),
            "committed": self.committed,
            "my_texts": self.my_texts,
            "their_texts": self.their_texts,
            "their_proposals": self.their_proposals,
            "chains": [c.d() for c in self.chains],
        }


def _too_far(a: Message, b: Message) -> bool:
    """Only checked when both sides carry a usable timestamp; otherwise order is all we have."""
    if not (a.has_reliable_ts and b.has_reliable_ts):
        return False
    return (b.ts - a.ts).total_seconds() > MAX_GAP_HOURS * 3600


def _commits_after(texts: list[Message], index: int) -> list[str]:
    out: list[str] = []
    start = texts[index]
    for j in range(index + 1, min(len(texts), index + 1 + COMMIT_LOOKAHEAD)):
        m = texts[j]
        if _too_far(start, m):
            break
        t = (m.text or "").strip()
        if m.sender == Sender.me and lx.PROPOSE_RE.search(t):
            break  # a new offer ends the window of this one
        if lx.COMMIT_RE.search(t):
            out.append(m.id)
    return out


def _answer_to(texts: list[Message], index: int) -> tuple[str, str | None, list[str]]:
    propose = texts[index]
    for j in range(index + 1, min(len(texts), index + 1 + MAX_LOOKAHEAD)):
        m = texts[j]
        if _too_far(propose, m):
            break
        t = (m.text or "").strip()
        if m.sender == Sender.me:
            # I said something else — unless it is another offer, the first one is still open.
            if lx.PROPOSE_RE.search(t):
                return NONE, None, []
            continue
        # Their first reply decides it. Order matters: a re-schedule outranks an agreement word,
        # because 「好啊，改天吧」 is a no with a smile on it.
        if lx.DECLINE_RE.search(t):
            return DECLINE, m.id, []
        if lx.ALT_TIME_RE.search(t):
            return ALT_TIME, m.id, []
        if lx.ACCEPT_RE.search(t):
            return ACCEPT, m.id, _commits_after(texts, j)
        return DEFLECT, m.id, []
    return NONE, None, []


def build_invitations(events: list[Message]) -> InvitationReport:
    """Walk the conversation once and read every offer the user made, with its answer."""
    texts = [m for m in events if m.is_text and (m.text or "").strip()]
    rep = InvitationReport(
        my_texts=sum(1 for m in texts if m.sender == Sender.me),
        their_texts=sum(1 for m in texts if m.sender == Sender.other),
    )
    for m in texts:
        if m.sender == Sender.other and lx.PROPOSE_RE.search((m.text or "").strip()):
            rep.their_proposals += 1
            rep.their_proposal_ids.append(m.id)

    for i, m in enumerate(texts):
        if m.sender != Sender.me:
            continue
        t = (m.text or "").strip()
        if not lx.PROPOSE_RE.search(t):
            continue
        response, rid, commit_ids = _answer_to(texts, i)
        rep.chains.append(
            Invitation(propose_id=m.id, propose_text=t[:40], response=response, response_id=rid, commit_ids=commit_ids)
        )
    return rep
