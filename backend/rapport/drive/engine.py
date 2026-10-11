"""The drive engine: pure functions over a small state dict. No database, no clock of its own.

`tick(state, now, signals, params, rng)` advances one relationship's three variables by the
elapsed time, applies the signals observed since the last tick, and decides whether this hour
should produce a nudge. Everything it returns is explainable: the per-source contributions are
kept in `state["last_sources"]` so the UI can answer "why now?" and "why not?".
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime

from rapport.drive.params import DEFAULT, DriveParams

VARS = ("drive", "inhibition", "novelty")


def empty_state() -> dict:
    return {
        "drive": 0.0,
        "inhibition": 0.0,
        "novelty": 0.0,
        "last_tick": None,  # ISO
        "last_nudge_at": None,
        "last_kind_at": {},  # kind -> ISO
        "day": None,  # "YYYY-MM-DD" of `today_count`
        "today_count": 0,
        "boundary_pinned": False,
        "last_sources": {},
        "last_p": 0.0,
        "last_roll": None,
    }


@dataclass
class Signals:
    """What happened for this relationship since the last tick (all optional)."""

    affinity_total: int | None = None
    momentum: str = "unknown"  # rising | flat | falling | unknown
    boundary: str = "none"  # none | explicit_decline | stop_contact | safety_concern
    boundary_changed: bool = False  # only charge explicit_decline once, when it first appears
    days_since_report: float | None = None
    new_diary_entries: int = 0
    new_outcome: str | None = None  # better | same | worse | ended | unknown
    new_report: bool = False
    new_actions: int = 0
    dismissed: int = 0
    thumbs_down: int = 0
    extra: dict = field(default_factory=dict)


def _decay(value: float, hours: float, half_life_h: float) -> float:
    if hours <= 0 or value <= 0:
        return value
    return value * math.pow(0.5, hours / half_life_h)


def _clip(x: float, hi: float = 1.0) -> float:
    return max(0.0, min(hi, x))


def is_quiet(now: datetime, p: DriveParams = DEFAULT) -> bool:
    h = now.hour
    if p.quiet_start_h > p.quiet_end_h:  # wraps midnight
        return h >= p.quiet_start_h or h < p.quiet_end_h
    return p.quiet_start_h <= h < p.quiet_end_h


def hour_seed(relationship_id: str, now: datetime, salt: int = 0) -> int:
    key = f"{relationship_id}|{now:%Y-%m-%d-%H}|{salt}"
    return int.from_bytes(key.encode("utf-8"), "big") % (2**31 - 1)


def tick(state: dict, now: datetime, sig: Signals, p: DriveParams = DEFAULT, rng: random.Random | None = None, relationship_id: str = "default") -> tuple[dict, bool, str | None]:
    """Advance one relationship. Returns (new_state, should_nudge, allowed_kind_or_None).

    Idempotent within an hour: a second call in the same wall-clock hour returns the stored
    decision without re-rolling or re-applying gains.
    """
    st = dict(state) if state else empty_state()
    hour_key = f"{now:%Y-%m-%d-%H}"
    if st.get("last_tick") and st["last_tick"][:13] == now.isoformat()[:13]:
        return st, False, None  # already ticked this hour
    prev = datetime.fromisoformat(st["last_tick"]) if st.get("last_tick") else None
    hours = (now - prev).total_seconds() / 3600.0 if prev else 0.0
    sources: dict[str, dict[str, float]] = {"drive": {}, "inhibition": {}, "novelty": {}}

    # 1. decay
    for k, hl in (("drive", p.drive_half_life_h), ("inhibition", p.inhibition_half_life_h), ("novelty", p.novelty_half_life_h)):
        st[k] = _decay(float(st.get(k, 0.0)), hours, hl)

    # 2. secretion
    days = hours / 24.0 if hours else 0.0
    if sig.affinity_total is not None and sig.momentum != "falling":
        g = max(0.0, (sig.affinity_total - 50) / 50.0) * p.affinity_gain_per_day * days
        if g:
            sources["drive"]["affinity"] = g
    if sig.momentum == "rising" and sig.new_report:
        sources["drive"]["rising"] = p.rising_bonus
    if sig.days_since_report is not None and sig.momentum != "falling":
        g = min(p.silence_cap, p.silence_gain_per_3d * (sig.days_since_report / 3.0))
        # prorate: the silence gain is a level, not a rate; charge the delta since last tick
        already = float(st.get("silence_charged", 0.0))
        if g > already:
            sources["drive"]["silence"] = g - already
            st["silence_charged"] = g
    if sig.new_diary_entries:
        sources["drive"]["diary"] = p.diary_gain * sig.new_diary_entries
    if sig.new_outcome == "better":
        sources["drive"]["outcome_better"] = p.outcome_better_gain
    if sig.new_report:
        st["silence_charged"] = 0.0
        sources["novelty"]["new_report"] = p.new_report_gain
    if sig.new_actions:
        sources["novelty"]["new_action"] = p.new_action_gain * sig.new_actions

    pinned = sig.boundary in ("stop_contact", "safety_concern")
    st["boundary_pinned"] = pinned
    if pinned:
        sources["inhibition"]["boundary"] = p.boundary_pin
    elif sig.boundary == "explicit_decline" and sig.boundary_changed:
        sources["inhibition"]["explicit_decline"] = p.explicit_decline_gain
    if sig.dismissed:
        sources["inhibition"]["dismissed"] = p.dismiss_gain * sig.dismissed
    if sig.thumbs_down:
        sources["inhibition"]["thumbs_down"] = p.thumbs_down_gain * sig.thumbs_down

    rng = rng or random.Random(hour_seed(relationship_id, now))
    if rng.random() < p.pulse_p_per_hour:
        sources["novelty"]["pulse"] = rng.uniform(p.pulse_lo, p.pulse_hi)

    for var in VARS:
        st[var] = _clip(float(st[var]) + sum(sources[var].values()), p.max_state)
    if pinned:
        st["inhibition"] = p.boundary_pin

    # 3. daily counter
    day = f"{now:%Y-%m-%d}"
    if st.get("day") != day:
        st["day"], st["today_count"] = day, 0

    # 4. decide
    p_nudge = st["drive"] * (1.0 - st["inhibition"]) * (0.5 + st["novelty"])
    st["last_p"] = round(p_nudge, 4)
    blocked = None
    if is_quiet(now, p):
        blocked = "quiet_hours"
    elif st["today_count"] >= p.daily_cap:
        blocked = "daily_cap"
    elif pinned:
        blocked = "boundary"
    roll = rng.random()
    st["last_roll"] = round(roll, 4)
    should = blocked is None and roll < p_nudge
    st["last_sources"] = {k: {s: round(v, 4) for s, v in d.items()} for k, d in sources.items()}
    st["last_block"] = blocked
    st["last_tick"] = now.isoformat(timespec="minutes")
    st["hour_key"] = hour_key
    kind = None
    if should:
        kind = pick_kind(st, now, sig, p)
        if kind is None:
            should = False
            st["last_block"] = "no_kind_available"
    return st, should, kind


def pick_kind(st: dict, now: datetime, sig: Signals, p: DriveParams = DEFAULT) -> str | None:
    """Which nudge is allowed now. Falling momentum → only self_care. Kinds respect their cooldown."""
    order: list[str]
    if sig.momentum == "falling":
        order = ["self_care"]
    elif sig.boundary == "explicit_decline":
        order = ["checkin", "self_care", "recall"]
    else:
        order = ["recall", "timing", "draft", "checkin", "self_care"]
    last = st.get("last_kind_at") or {}
    for k in order:
        at = last.get(k)
        if at:
            since_h = (now - datetime.fromisoformat(at)).total_seconds() / 3600.0
            if since_h < p.same_kind_cooldown_h:
                continue
        return k
    return None


def record_nudge(st: dict, now: datetime, kind: str) -> dict:
    st = dict(st)
    st["last_nudge_at"] = now.isoformat(timespec="minutes")
    st.setdefault("last_kind_at", {})[kind] = now.isoformat(timespec="minutes")
    st["today_count"] = int(st.get("today_count", 0)) + 1
    return st


def explain(st: dict, p: DriveParams = DEFAULT) -> dict:
    """Human-readable account of the current state: values, last contributions, and the gate."""
    why_not = {
        None: None,
        "quiet_hours": f"静默时段（{p.quiet_start_h:02d}:00–{p.quiet_end_h:02d}:00）不提醒。",
        "daily_cap": f"今天已达上限（{p.daily_cap} 条）。",
        "boundary": "对方有停止联系或安全相关信号，边界优先，不主动提醒推进。",
        "no_kind_available": "可用的提醒类型都在冷却期内。",
    }.get(st.get("last_block"))
    return {
        "drive": round(float(st.get("drive", 0.0)), 3),
        "inhibition": round(float(st.get("inhibition", 0.0)), 3),
        "novelty": round(float(st.get("novelty", 0.0)), 3),
        "p_last_hour": st.get("last_p", 0.0),
        "roll_last_hour": st.get("last_roll"),
        "last_tick": st.get("last_tick"),
        "last_nudge_at": st.get("last_nudge_at"),
        "today_count": st.get("today_count", 0),
        "daily_cap": p.daily_cap,
        "boundary_pinned": bool(st.get("boundary_pinned")),
        "sources": st.get("last_sources", {}),
        "blocked_by": st.get("last_block"),
        "why_not": why_not,
        "note": "三个量都是产品假设的驱动状态，不是情绪测量；随机脉冲按小时种子生成，可复现。",
    }
