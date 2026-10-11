"""Label prior engine (v2 M04, the part that reverses v1's "labels are bypassed" decision).

What the user asked for: MBTI / zodiac / Chinese zodiac initialise the portrait with a real
weight, and that weight decays as genuine chat evidence accumulates. What must NOT happen:
labels leaking into the affinity score, or a label being read as evidence that the other
person likes the user.

So the mechanism is deliberately split:

    alpha(N_eff) = alpha0 * exp(-N_eff / scale)      # how much the label hypothesis weighs
    prior budget  = alpha0 maximum (default 0.15), split by the user's ratio (default 4:2:1)
    evidence/unknown = at least 1 - alpha0; missing shares are never redistributed

`alpha0`, `scale` and the label ratio live in `PriorPolicy`: 15% and 4:2:1 are the product
defaults the user can change (设置 → 个性化权重), not a hard-coded ceiling.

- `N_eff` counts **confirmed observed-chat evidence units**, deduplicated per canonical event
  and capped per episode (see `scoring.evidence`). Re-imports, AI drafts and public reference
  examples contribute nothing.
- Missing labels move their share into `unknown`; the remaining labels are never scaled up.
- The result feeds the *portrait hypothesis* only. `scoring.affinity` has no parameter for it,
  and there is a test asserting labels cannot move the score.

All numbers here are product hypotheses for an entertainment tool, not psychometrics.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from math import exp, isfinite

from rapport.knowledge.priors import CHINESE_ZODIAC_PRIORS, MBTI_PRIORS, ZODIAC_PRIORS

# Defaults, not limits: `PriorPolicy` lets the user move the budget, the decay speed and the
# split between the three labels. The numbers below are what "默认" means in the UI.
PRIOR_ALPHA0 = 0.15
PRIOR_SCALE = 40.0
# Product allocation only, not validated psychology. Objective scoring is unchanged.
PRIOR_SHARES: dict[str, float] = {"mbti": 0.15 * 4 / 7, "zodiac": 0.15 * 2 / 7, "chinese_zodiac": 0.15 / 7}
UNKNOWN_SHARE_AT_ZERO = 0.85
PRIOR_POLICY_VERSION = "auxiliary-cap-15-v1"

LABEL_KEYS = ("mbti", "zodiac", "chinese_zodiac")


class PriorError(ValueError):
    pass


@dataclass(frozen=True)
class PriorPolicy:
    """The user-adjustable label budget. Defaults reproduce the original 15% cap and 4:2:1 split."""

    alpha0: float = PRIOR_ALPHA0
    scale: float = PRIOR_SCALE
    # Relative importance of mbti : zodiac : chinese_zodiac. Only the ratios matter.
    ratio: tuple[float, float, float] = (4.0, 2.0, 1.0)

    def __post_init__(self):
        if not all(isfinite(x) for x in (self.alpha0, self.scale, *self.ratio)):
            raise PriorError("finite parameters required")
        if not 0.0 <= self.alpha0 <= 1.0 or self.scale <= 0 or any(r < 0 for r in self.ratio):
            raise PriorError("invalid prior policy")

    def as_dict(self) -> dict:
        return {"alpha0": self.alpha0, "scale": self.scale, "ratio": list(self.ratio)}


DEFAULT_POLICY = PriorPolicy()


def policy_shares(policy: PriorPolicy = DEFAULT_POLICY) -> dict[str, float]:
    """Absolute share per label = alpha0 × its part of the ratio. An all-zero ratio claims nothing."""
    total = sum(policy.ratio)
    if total <= 0:
        return {key: 0.0 for key in LABEL_KEYS}
    return {key: policy.alpha0 * r / total for key, r in zip(LABEL_KEYS, policy.ratio, strict=True)}


def policy_version(policy: PriorPolicy = DEFAULT_POLICY) -> str:
    """Stable identity of a policy: cache keys and every report carry it, so a changed setting
    never silently reuses an analysis built under the old numbers."""
    if policy == DEFAULT_POLICY:
        return PRIOR_POLICY_VERSION
    raw = json.dumps(policy.as_dict(), sort_keys=True)
    return "auxiliary-custom-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def prior_decay(n_eff: float, scale: float = PRIOR_SCALE) -> float:
    """The decay factor alone, in [0, 1]. Absolute label weights are share * decay."""
    if not all(isfinite(x) for x in (n_eff, scale)):
        raise PriorError("finite parameters required")
    if n_eff < 0 or scale <= 0:
        raise PriorError("invalid decay parameters")
    return exp(-n_eff / scale)


def prior_alpha(n_eff: float, alpha0: float = PRIOR_ALPHA0, scale: float = PRIOR_SCALE) -> float:
    """How much the label hypothesis still weighs after `n_eff` confirmed evidence units.

    Monotonically decreasing, never negative. At N_eff = 0 it is the full alpha0.
    """
    if not all(isfinite(x) for x in (n_eff, alpha0, scale)):
        raise PriorError("finite parameters required")
    if n_eff < 0 or not 0 <= alpha0 <= 1 or scale <= 0:
        raise PriorError("invalid decay parameters")
    return alpha0 * exp(-n_eff / scale)


@dataclass(frozen=True)
class PriorHypothesis:
    """One revisable assumption derived from a user-declared label."""

    label: str  # mbti | zodiac | chinese_zodiac
    value: str
    weight: float
    hypothesis: str
    source_type: str = "tag_hypothesis"
    basis: str = "用户自述标签（初识阶段参考，可被真实聊天推翻）"
    entertainment: bool = False  # zodiac / Chinese zodiac are explicitly non-predictive

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "value": self.value,
            "weight": round(self.weight, 4),
            "hypothesis": self.hypothesis,
            "source_type": self.source_type,
            "basis": self.basis,
            "entertainment": self.entertainment,
        }


@dataclass(frozen=True)
class PriorBreakdown:
    alpha: float
    weights: dict[str, float]
    unknown: float
    present: tuple[str, ...]
    policy: PriorPolicy = DEFAULT_POLICY

    def as_dict(self) -> dict:
        return {
            "alpha": round(self.alpha, 4),
            "alpha0": round(self.policy.alpha0, 4),
            "shares": {k: round(v, 4) for k, v in policy_shares(self.policy).items()},
            "policy_version": policy_version(self.policy),
            "weights": {k: round(v, 4) for k, v in self.weights.items()},
            "unknown": round(self.unknown, 4),
            "present": list(self.present),
        }


def prior_weights(
    n_eff: float,
    labels: dict[str, str | None],
    policy: PriorPolicy = DEFAULT_POLICY,
) -> PriorBreakdown:
    """Split the decaying prior budget across the labels that were actually provided.

    A label the user left blank does not hand its share to the others: that share goes to
    `unknown`, which is the honest way to say "we have no information here".
    """
    decay = prior_decay(n_eff, policy.scale)
    shares = policy_shares(policy)
    weights: dict[str, float] = {}
    present: list[str] = []
    for key in LABEL_KEYS:
        value = (labels or {}).get(key)
        if value:
            # Absolute shares within the user's budget decay without redistributing gaps.
            weights[key] = shares[key] * decay
            present.append(key)
    # Everything the labels do not claim is the space available to chat evidence (and to the
    # honest "we do not know" region). A blank label leaves its share here instead of handing it
    # to the other labels, and as alpha decays this space grows — that is the hand-over.
    unknown = 1.0 - sum(weights.values())
    if abs(sum(weights.values()) + unknown - 1.0) > 1e-9:  # pragma: no cover - invariant guard
        raise PriorError("prior budget does not sum to 1")
    return PriorBreakdown(
        alpha=prior_alpha(n_eff, policy.alpha0, policy.scale),
        weights=weights,
        unknown=unknown,
        present=tuple(present),
        policy=policy,
    )


def build_hypotheses(
    n_eff: float,
    labels: dict[str, str | None],
    policy: PriorPolicy = DEFAULT_POLICY,
) -> tuple[list[PriorHypothesis], PriorBreakdown]:
    """Turn label values into explicit, revisable hypotheses with their current weight."""
    breakdown = prior_weights(n_eff, labels, policy)
    out: list[PriorHypothesis] = []
    mbti = (labels or {}).get("mbti")
    if mbti and mbti in MBTI_PRIORS:
        out.append(
            PriorHypothesis(
                label="mbti",
                value=mbti,
                weight=breakdown.weights.get("mbti", 0.0),
                hypothesis=MBTI_PRIORS[mbti],
            )
        )
    zodiac = (labels or {}).get("zodiac")
    if zodiac and zodiac in ZODIAC_PRIORS:
        out.append(
            PriorHypothesis(
                label="zodiac",
                value=zodiac,
                weight=breakdown.weights.get("zodiac", 0.0),
                hypothesis=ZODIAC_PRIORS[zodiac],
                entertainment=True,
            )
        )
    chinese = (labels or {}).get("chinese_zodiac")
    if chinese and chinese in CHINESE_ZODIAC_PRIORS:
        out.append(
            PriorHypothesis(
                label="chinese_zodiac",
                value=chinese,
                weight=breakdown.weights.get("chinese_zodiac", 0.0),
                hypothesis=CHINESE_ZODIAC_PRIORS[chinese],
                entertainment=True,
            )
        )
    return out, breakdown


def decay_table(n_eff_max: float = 120.0, step: float = 20.0, policy: PriorPolicy = DEFAULT_POLICY) -> list[tuple[float, float]]:
    """(N_eff, alpha) samples, for the UI to show what "the label is fading" means."""
    out: list[tuple[float, float]] = []
    n = 0.0
    while n <= n_eff_max:
        out.append((n, round(prior_alpha(n, policy.alpha0, policy.scale), 4)))
        n += step
    return out
