"""Deterministic behavioural features. Same input + same version => same output.

Every metric carries its denominator (`n`) and, when unavailable, a reason.
Time metrics require minute/second precision on both sides of a pair.
"""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

from rapport import config as _config
from rapport.features import lexicon as lx
from rapport.features.invitations import build_invitations
from rapport.features.sessions import Session, split_sessions, turns
from rapport.schemas.message_v1 import MEDIA_TYPES, Conversation, Message, MsgType, Sender

REPLY_CUTOFF_S = 24 * 3600


@dataclass
class Metric:
    value: float | None
    n: int = 0
    unavailable_reason: str | None = None
    unit: str = ""

    def d(self) -> dict[str, Any]:
        return asdict(self)


def _m(value: float | None, n: int, unit: str = "", reason: str | None = None) -> Metric:
    if value is None and reason is None:
        reason = "no_data"
    return Metric(value=None if value is None else round(value, 4), n=n, unavailable_reason=reason, unit=unit)


@dataclass
class SideFeatures:
    msgs: int = 0
    texts: int = 0
    turns: int = 0
    chars: int = 0
    media: int = 0
    stickers: int = 0
    avg_len: Metric = field(default_factory=lambda: _m(None, 0))
    median_len: Metric = field(default_factory=lambda: _m(None, 0))
    question_rate: Metric = field(default_factory=lambda: _m(None, 0))
    laugh_rate: Metric = field(default_factory=lambda: _m(None, 0))
    care_rate: Metric = field(default_factory=lambda: _m(None, 0))
    future_rate: Metric = field(default_factory=lambda: _m(None, 0))
    affection_rate: Metric = field(default_factory=lambda: _m(None, 0))
    emoji_rate: Metric = field(default_factory=lambda: _m(None, 0))
    exclaim_rate: Metric = field(default_factory=lambda: _m(None, 0))
    short_reply_rate: Metric = field(default_factory=lambda: _m(None, 0))
    late_night_rate: Metric = field(default_factory=lambda: _m(None, 0))
    double_text_rate: Metric = field(default_factory=lambda: _m(None, 0))
    initiation_rate: Metric = field(default_factory=lambda: _m(None, 0))
    ending_rate: Metric = field(default_factory=lambda: _m(None, 0))
    median_reply_s: Metric = field(default_factory=lambda: _m(None, 0))
    median_text_reply_s: Metric = field(default_factory=lambda: _m(None, 0))
    reply_within_5min_rate: Metric = field(default_factory=lambda: _m(None, 0))
    reply_rate_24h: Metric = field(default_factory=lambda: _m(None, 0))
    care_ids: list[str] = field(default_factory=list)
    future_ids: list[str] = field(default_factory=list)
    affection_ids: list[str] = field(default_factory=list)
    question_ids: list[str] = field(default_factory=list)
    laugh_ids: list[str] = field(default_factory=list)

    def d(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in self.__dict__.items():
            out[k] = v.d() if isinstance(v, Metric) else v
        return out


@dataclass
class Features:
    version: str
    n_events: int
    n_sessions: int
    has_reliable_time: bool
    range_start: datetime | None
    range_end: datetime | None
    me: SideFeatures
    other: SideFeatures
    length_ratio: Metric
    latency_ratio: Metric
    initiation_ratio: Metric
    boundary_hits: dict[str, list[str]]
    confirm_ids: list[str]
    conflict_ids: list[str]
    apology_ids: list[str]
    # Invitation chains (v0.5): offer → answer → settled time, the basis of the future dimension.
    # Empty dict + unavailable metrics for material with no chain, which is when the dimension
    # falls back to the old word-frequency estimate.
    # v0.6: same hits, but only those inside the trailing BOUNDARY_RECENT_DAYS of the material.
    # A boundary is a *current* state; a 不合适 from two years ago is history, not a gate.
    boundary_recent: dict[str, list[str]] = field(default_factory=dict)
    invitations: dict[str, Any] = field(default_factory=dict)
    propose_rate: Metric = field(default_factory=lambda: _m(None, 0))
    accept_rate: Metric = field(default_factory=lambda: _m(None, 0))
    alt_time_rate: Metric = field(default_factory=lambda: _m(None, 0))
    commit_rate: Metric = field(default_factory=lambda: _m(None, 0))

    def d(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "n_events": self.n_events,
            "n_sessions": self.n_sessions,
            "has_reliable_time": self.has_reliable_time,
            "range_start": self.range_start.isoformat() if self.range_start else None,
            "range_end": self.range_end.isoformat() if self.range_end else None,
            "me": self.me.d(),
            "other": self.other.d(),
            "length_ratio": self.length_ratio.d(),
            "latency_ratio": self.latency_ratio.d(),
            "initiation_ratio": self.initiation_ratio.d(),
            "boundary_hits": self.boundary_hits,
            "boundary_recent": self.boundary_recent,
            "confirm_ids": self.confirm_ids,
            "conflict_ids": self.conflict_ids,
            "apology_ids": self.apology_ids,
            "invitations": self.invitations,
            "invitation_rates": {
                "propose_rate": self.propose_rate.d(),
                "accept_rate": self.accept_rate.d(),
                "alt_time_rate": self.alt_time_rate.d(),
                "commit_rate": self.commit_rate.d(),
            },
        }


def _rate(hits: int, n: int, reason_if_empty: str = "no_text") -> Metric:
    if n == 0:
        return _m(None, 0, "rate", reason_if_empty)
    return _m(hits / n, n, "rate")


def _side(events: list[Message], sessions: list[Session], who: Sender, has_time: bool) -> SideFeatures:
    sf = SideFeatures()
    mine = [m for m in events if m.sender == who]
    texts = [m for m in mine if m.is_text]
    sf.msgs = len(mine)
    sf.texts = len(texts)
    sf.media = sum(1 for m in mine if m.type in MEDIA_TYPES)
    sf.stickers = sum(1 for m in mine if m.type == MsgType.sticker)
    lens = [len(m.text or "") for m in texts]
    sf.chars = sum(lens)
    if lens:
        sf.avg_len = _m(statistics.fmean(lens), len(lens), "chars")
        sf.median_len = _m(statistics.median(lens), len(lens), "chars")
    hits = {k: 0 for k in ("q", "laugh", "care", "future", "aff", "emoji", "excl", "short")}
    for m in texts:
        t = m.text or ""
        if lx.QUESTION_RE.search(t):
            hits["q"] += 1
            sf.question_ids.append(m.id)
        if lx.LAUGH_RE.search(t):
            hits["laugh"] += 1
            sf.laugh_ids.append(m.id)
        if lx.CARE_RE.search(t):
            hits["care"] += 1
            sf.care_ids.append(m.id)
        if lx.FUTURE_RE.search(t):
            hits["future"] += 1
            sf.future_ids.append(m.id)
        if lx.AFFECTION_RE.search(t):
            hits["aff"] += 1
            sf.affection_ids.append(m.id)
        if lx.EMOJI_RE.search(t):
            hits["emoji"] += 1
        if lx.EXCLAIM_RE.search(t):
            hits["excl"] += 1
        if lx.SHORT_REPLY_RE.match(t.strip()):
            hits["short"] += 1
    n = len(texts)
    sf.question_rate = _rate(hits["q"], n)
    sf.laugh_rate = _rate(hits["laugh"], n)
    sf.care_rate = _rate(hits["care"], n)
    sf.future_rate = _rate(hits["future"], n)
    sf.affection_rate = _rate(hits["aff"], n)
    sf.emoji_rate = _rate(hits["emoji"] + sf.stickers, n + sf.stickers)
    sf.exclaim_rate = _rate(hits["excl"], n)
    sf.short_reply_rate = _rate(hits["short"], n)
    # late night
    timed = [m for m in mine if m.has_reliable_ts]
    if timed:
        late = sum(1 for m in timed if m.ts.hour >= 23 or m.ts.hour < 5)
        sf.late_night_rate = _m(late / len(timed), len(timed), "rate")
    else:
        sf.late_night_rate = _m(None, 0, "rate", "no_time")
    # turns / double text
    tr = turns(events)
    my_turns = [t for t in tr if t[0].sender == who]
    sf.turns = len(my_turns)
    if my_turns:
        double = sum(1 for t in my_turns if len(t) >= 2)
        sf.double_text_rate = _m(double / len(my_turns), len(my_turns), "rate")
    # initiation / ending (session based)
    if sessions and (has_time or len(sessions) >= 2):
        starts = sum(1 for s in sessions if s.first.sender == who)
        ends = sum(1 for s in sessions if s.last.sender == who)
        sf.initiation_rate = _m(starts / len(sessions), len(sessions), "rate")
        sf.ending_rate = _m(ends / len(sessions), len(sessions), "rate")
    else:
        sf.initiation_rate = _m(None, 0, "rate", "no_time")
        sf.ending_rate = _m(None, 0, "rate", "no_time")
    # reply latency: this side replying to the other side's turn
    lat_any: list[float] = []
    lat_text: list[float] = []
    n_pairs = 0
    n_replied_24h = 0
    n_within_5 = 0
    for i in range(len(tr) - 1):
        cur, nxt = tr[i], tr[i + 1]
        if cur[0].sender == who or nxt[0].sender != who:
            continue
        anchor = cur[-1]
        if not anchor.has_reliable_ts:
            continue
        n_pairs += 1
        first_any = next((m for m in nxt if m.has_reliable_ts), None)
        if first_any is None:
            continue
        dt = (first_any.ts - anchor.ts).total_seconds()
        if dt < 0:
            continue
        if dt <= REPLY_CUTOFF_S:
            n_replied_24h += 1
            lat_any.append(dt)
            if dt <= 300:
                n_within_5 += 1
        first_text = next((m for m in nxt if m.is_text and m.has_reliable_ts), None)
        if first_text is not None:
            dtt = (first_text.ts - anchor.ts).total_seconds()
            if 0 <= dtt <= REPLY_CUTOFF_S:
                lat_text.append(dtt)
    if n_pairs == 0:
        reason = "no_time" if not has_time else "no_reply_pairs"
        sf.median_reply_s = _m(None, 0, "s", reason)
        sf.median_text_reply_s = _m(None, 0, "s", reason)
        sf.reply_within_5min_rate = _m(None, 0, "rate", reason)
        sf.reply_rate_24h = _m(None, 0, "rate", reason)
    else:
        sf.median_reply_s = _m(statistics.median(lat_any) if lat_any else None, len(lat_any), "s", None if lat_any else "no_reply_within_24h")
        sf.median_text_reply_s = _m(statistics.median(lat_text) if lat_text else None, len(lat_text), "s", None if lat_text else "no_text_reply")
        sf.reply_within_5min_rate = _m(n_within_5 / n_pairs, n_pairs, "rate")
        sf.reply_rate_24h = _m(n_replied_24h / n_pairs, n_pairs, "rate")
    return sf


BOUNDARY_RECENT_DAYS = 30


def _recent_hits(hits: dict[str, list[str]], events: list[Message], days: int) -> dict[str, list[str]]:
    """Filter boundary hits to the trailing `days` of the material. Untimed material keeps every hit
    (there is no way to say it is old), which is the pre-v0.6 behaviour."""
    timed = [m.ts for m in events if m.ts]
    if not timed:
        return {k: list(v) for k, v in hits.items()}
    cutoff = max(timed) - timedelta(days=days)
    ts_of = {m.id: m.ts for m in events}
    out: dict[str, list[str]] = {}
    for k, ids in hits.items():
        out[k] = [i for i in ids if ts_of.get(i) is None or ts_of[i] >= cutoff]
    return out


def _scan_global(events: list[Message]) -> tuple[dict[str, list[str]], list[str], list[str], list[str]]:
    hits: dict[str, list[str]] = {"stop_contact": [], "explicit_decline": [], "safety_concern": [], "decline_with_alternative": []}
    confirm: list[str] = []
    conflict: list[str] = []
    apology: list[str] = []
    for m in events:
        if not m.is_text:
            continue
        t = m.text or ""
        if m.sender == Sender.other:
            if lx.STOP_CONTACT_RE.search(t):
                hits["stop_contact"].append(m.id)
            elif lx.DECLINE_RE.search(t):
                if lx.ALT_TIME_RE.search(t):
                    hits["decline_with_alternative"].append(m.id)
                else:
                    hits["explicit_decline"].append(m.id)
        if lx.SAFETY_RE.search(t):
            hits["safety_concern"].append(m.id)
        if lx.RELATIONSHIP_CONFIRM_RE.search(t):
            confirm.append(m.id)
        if lx.CONFLICT_RE.search(t):
            conflict.append(m.id)
        if lx.APOLOGY_RE.search(t):
            apology.append(m.id)
    return hits, confirm, conflict, apology


def compute_features(conv: Conversation, events: list[Message] | None = None, gap_hours: float = 6.0) -> Features:
    events = events if events is not None else conv.events()
    has_time = _has_reliable_time(events)
    sessions = split_sessions(events, gap_hours) if has_time else ([Session(0, events)] if events else [])
    me = _side(events, sessions, Sender.me, has_time)
    other = _side(events, sessions, Sender.other, has_time)
    length_ratio = _m(
        (other.avg_len.value / me.avg_len.value) if me.avg_len.value and other.avg_len.value else None,
        min(me.texts, other.texts),
        "ratio",
        None if (me.avg_len.value and other.avg_len.value) else "no_text",
    )
    latency_ratio = _m(
        (other.median_reply_s.value / me.median_reply_s.value)
        if me.median_reply_s.value and other.median_reply_s.value
        else None,
        min(me.median_reply_s.n, other.median_reply_s.n),
        "ratio",
        None if (me.median_reply_s.value and other.median_reply_s.value) else "no_time",
    )
    initiation_ratio = _m(
        (other.initiation_rate.value / me.initiation_rate.value)
        if me.initiation_rate.value and other.initiation_rate.value is not None
        else None,
        len(sessions),
        "ratio",
        None if me.initiation_rate.value else "no_sessions",
    )
    hits, confirm, conflict, apology = _scan_global(events)
    recent = _recent_hits(hits, events, BOUNDARY_RECENT_DAYS)
    inv = build_invitations(events)
    # Rates come out of the chains. The denominator for acceptance is *offers made*, not offers
    # answered: "asked ten times, never answered" is a 0% acceptance rate, not a missing value —
    # only material with no offers at all is unavailable, and it says so.
    proposed = inv.proposed
    # "No offer at all" is missing evidence, not a zero: whether the user never asked, or simply
    # never pasted the part where they did, the rule layer cannot tell. It is reported unavailable
    # and the future dimension falls back to the word-frequency estimate. An offer that got no
    # answer is the opposite case — that is a real 0%.
    propose_rate = _m((proposed / inv.my_texts) if proposed else None, inv.my_texts, "rate", None if proposed else "no_offers")
    accept_rate = _m((inv.accepted / proposed) if proposed else None, proposed, "ratio", None if proposed else "no_offers")
    alt_time_rate = _m((inv.alt_timed / proposed) if proposed else None, proposed, "ratio", None if proposed else "no_offers")
    commit_rate = _m((inv.committed / inv.accepted) if inv.accepted else None, inv.accepted, "ratio", None if inv.accepted else "no_accepts")
    timed = [m.ts for m in events if m.ts]
    return Features(
        version=_config.FEATURES_VERSION,
        n_events=len(events),
        n_sessions=len(sessions),
        has_reliable_time=has_time,
        range_start=min(timed) if timed else None,
        range_end=max(timed) if timed else None,
        me=me,
        other=other,
        length_ratio=length_ratio,
        latency_ratio=latency_ratio,
        initiation_ratio=initiation_ratio,
        boundary_hits=hits,
        boundary_recent=recent,
        confirm_ids=confirm,
        conflict_ids=conflict,
        apology_ids=apology,
        invitations=inv.d(),
        propose_rate=propose_rate,
        accept_rate=accept_rate,
        alt_time_rate=alt_time_rate,
        commit_rate=commit_rate,
    )


def _has_reliable_time(events: list[Message]) -> bool:
    if not events:
        return False
    n = sum(1 for m in events if m.has_reliable_ts)
    return n >= max(2, len(events) // 2)
