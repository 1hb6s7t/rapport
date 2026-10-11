"""Drive parameters: the "hormone" constants in one place, hashed into a version.

The user's idea, made mechanical: three slow variables per relationship, each fed by observable
events and decaying on a half-life, whose combination sets the *probability* that the tool nudges
the user about this person this hour. Nothing here is learned or fitted; every number is a
product hypothesis and is part of `drive_digest()` so changing one is visible in every nudge.

    drive       想起对方的冲动   fed by affinity, momentum, silence, diary, good outcomes
    inhibition  抑制             fed by boundaries, dismissals, thumbs-down, daily cap, quiet hours
    novelty     刺激             seeded random pulses (the "irregular, variable stimulus") + new events

    p(nudge this hour) = drive × (1 − inhibition) × (0.5 + novelty)

Falling momentum never feeds drive: a cooling relationship must not turn into a stream of
"they're drifting away" prompts (docs/TECH-PLAN-v2.md: 不推送焦虑型下降提醒).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class DriveParams:
    # half-lives, hours
    drive_half_life_h: float = 36.0
    inhibition_half_life_h: float = 72.0
    novelty_half_life_h: float = 12.0
    # drive sources (per day unless noted; applied per tick, prorated)
    affinity_gain_per_day: float = 0.30  # × max(0, (total − 50) / 50)
    rising_bonus: float = 0.10  # one-off when the latest report's momentum is rising
    silence_gain_per_3d: float = 0.05  # every 3 days since the last report / nudge, capped
    silence_cap: float = 0.30
    diary_gain: float = 0.15  # per new diary entry for this relationship
    outcome_better_gain: float = 0.20
    # inhibition sources
    boundary_pin: float = 1.0  # stop_contact / safety_concern: pinned, no decay while it holds
    explicit_decline_gain: float = 0.50
    dismiss_gain: float = 0.30
    thumbs_down_gain: float = 0.40
    cap_hit_gain: float = 1.0  # daily cap reached: silent for the rest of the day
    # novelty sources
    pulse_p_per_hour: float = 0.08
    pulse_lo: float = 0.10
    pulse_hi: float = 0.40
    new_report_gain: float = 0.30
    new_action_gain: float = 0.30
    # hard constraints
    daily_cap: int = 1
    quiet_start_h: int = 23
    quiet_end_h: int = 8
    same_kind_cooldown_h: int = 48
    max_state: float = 1.0
    kinds: tuple[str, ...] = field(default=("recall", "timing", "draft", "checkin", "self_care"))


DEFAULT = DriveParams()


def drive_digest(p: DriveParams = DEFAULT) -> str:
    raw = json.dumps(asdict(p), sort_keys=True, ensure_ascii=False).encode("utf-8")
    return "drive-v1+" + hashlib.sha256(raw).hexdigest()[:12]
