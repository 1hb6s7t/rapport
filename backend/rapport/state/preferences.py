"""PreferencesMixin: the user's own weighting knobs, persisted like the drive config.

v1 holds the label prior policy — how much MBTI / 星座 / 属相 may weigh at the start, how that
fades with chat evidence, and how the budget splits between the three. The product defaults
(15%, 4:2:1, scale 40) apply when nothing is saved; a saved value is clamped on read so a
hand-edited database can never yield an invalid policy. Objective scoring never reads any of
this (see `scoring.prior`), so these knobs shape the portrait hypothesis, not the score.
"""

from __future__ import annotations

from math import isfinite

from rapport.db import Setting
from rapport.scoring.prior import (
    DEFAULT_POLICY,
    LABEL_KEYS,
    PriorError,
    PriorPolicy,
    policy_shares,
    policy_version,
)

PRIOR_POLICY_KEY = "prior.policy"
ALPHA0_RANGE = (0.0, 1.0)
SCALE_RANGE = (5.0, 500.0)
RATIO_RANGE = (0.0, 100.0)
PATCH_KEYS = ("alpha0", "scale", *LABEL_KEYS)


def _clamp(value, lo: float, hi: float) -> float | None:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    if not isfinite(x):
        return None
    return round(max(lo, min(hi, x)), 4)


def _policy_from(v: dict) -> PriorPolicy:
    """Saved dict → policy; every missing or unusable field falls back to the default."""
    d = DEFAULT_POLICY
    alpha0 = _clamp(v.get("alpha0"), *ALPHA0_RANGE)
    scale = _clamp(v.get("scale"), *SCALE_RANGE)
    ratio = tuple(_clamp(v.get(key), *RATIO_RANGE) for key in LABEL_KEYS)
    try:
        return PriorPolicy(
            alpha0=d.alpha0 if alpha0 is None else alpha0,
            scale=d.scale if scale is None else scale,
            ratio=tuple(d.ratio[i] if r is None else r for i, r in enumerate(ratio)),
        )
    except PriorError:
        return d


def _view(policy: PriorPolicy) -> dict:
    return {
        "alpha0": policy.alpha0,
        "scale": policy.scale,
        "ratio": dict(zip(LABEL_KEYS, policy.ratio, strict=True)),
        "shares": policy_shares(policy),
        "version": policy_version(policy),
    }


class PreferencesMixin:
    # ---------- label prior policy ----------
    def prior_policy(self) -> PriorPolicy:
        with self.db.session() as s:
            row = s.get(Setting, PRIOR_POLICY_KEY)
            v = dict(row.value) if row and isinstance(row.value, dict) else {}
        return _policy_from(v)

    def set_prior_policy(self, patch: dict) -> PriorPolicy:
        """Merge a partial update over what is saved; `reset` returns to the product defaults."""
        with self.db.session() as s:
            row = s.get(Setting, PRIOR_POLICY_KEY) or Setting(key=PRIOR_POLICY_KEY, value={})
            v = {} if patch.get("reset") else dict(row.value or {})
            if not patch.get("reset"):
                for key in PATCH_KEYS:
                    if patch.get(key) is not None:
                        v[key] = patch[key]
            policy = _policy_from(v)
            row.value = (
                {}
                if policy == DEFAULT_POLICY
                else {
                    "alpha0": policy.alpha0,
                    "scale": policy.scale,
                    **dict(zip(LABEL_KEYS, policy.ratio, strict=True)),
                }
            )
            s.merge(row)
        return policy

    def prior_policy_version(self) -> str:
        return policy_version(self.prior_policy())

    def prior_policy_view(self) -> dict:
        policy = self.prior_policy()
        return {**_view(policy), "is_default": policy == DEFAULT_POLICY, "default": _view(DEFAULT_POLICY)}
