"""Affinity rubric v0.1: weights, anchors and the rule-side dimension scoring.

ALL anchors below are product hypotheses awaiting validation (see docs/eval.md).
They are deliberately conservative: a mediocre-but-consistent interaction lands near 50.

Each anchor table maps a raw metric to 0..100 via piece-wise linear interpolation.
"""

from __future__ import annotations

from dataclasses import dataclass

from rapport.features.compute import Features, Metric
from rapport.schemas.report_v1 import Confidence

WEIGHTS: dict[str, float] = {
    "responsiveness": 0.25,
    "initiative": 0.20,
    "investment": 0.20,
    "warmth": 0.20,
    "future": 0.15,
}
COVERAGE_MIN = 0.60  # v2 M05 gate: >=0.60 of baseline weight observed (was 0.80 -> 0.70 -> 0.60)
MIN_OTHER_TEXTS = 20
MIN_SESSIONS = 2

# Stage-conditional weights and per-dimension reliability (v0.5, shadow only).
#
# These are product hypotheses, not measurements. A dimension that is informative early — does the
# person reply at all — says less once the relationship is established, and the reverse holds for
# investment and future plans. `RELIABILITY` is how much a dimension is worth when it *is*
# observed: reply timing is read directly off timestamps, while "future" leans on a lexicon.
#
# Nothing here changes the displayed score. `build_affinity` computes this weighted total as an
# alternative alongside the static one, and the report keeps showing the static number until the
# check-in direction table (plan §6) says which of the two matches what users report. A weight
# change that is never validated against outcomes is just a different set of opinions.
STAGE_BETA: dict[str, dict[str, float]] = {
    "initial_contact": {"responsiveness": 1.4, "initiative": 1.2, "investment": 0.9, "warmth": 0.7, "future": 0.8},
    "getting_to_know": {"responsiveness": 1.2, "initiative": 1.1, "investment": 1.0, "warmth": 1.0, "future": 1.0},
    "affectionate_signals": {"responsiveness": 0.9, "initiative": 0.9, "investment": 1.1, "warmth": 1.2, "future": 1.2},
    "mutually_confirmed": {"responsiveness": 0.8, "initiative": 0.8, "investment": 1.2, "warmth": 1.1, "future": 1.3},
    "stable": {"responsiveness": 0.7, "initiative": 0.7, "investment": 1.2, "warmth": 1.1, "future": 1.3},
    "repair": {"responsiveness": 1.1, "initiative": 0.9, "investment": 1.2, "warmth": 1.3, "future": 0.7},
    # `unknown` deliberately absent: no stage means no adjustment, not a neutral guess.
}
RELIABILITY: dict[str, float] = {
    "responsiveness": 0.9,
    "initiative": 0.7,
    "investment": 0.8,
    "warmth": 0.6,
    "future": 0.7,
}

# (metric value, score) points; values outside range clamp to nearest score.
ANCHORS: dict[str, list[tuple[float, float]]] = {
    # seconds
    "median_reply_s": [(60, 95), (300, 85), (900, 72), (3600, 58), (4 * 3600, 42), (12 * 3600, 25), (24 * 3600, 10)],
    "reply_within_5min_rate": [(0.0, 20), (0.2, 40), (0.5, 65), (0.8, 85), (1.0, 95)],
    "reply_rate_24h": [(0.0, 5), (0.5, 35), (0.8, 65), (0.95, 85), (1.0, 92)],
    "initiation_rate": [(0.0, 15), (0.2, 35), (0.4, 55), (0.5, 65), (0.7, 82), (1.0, 92)],
    "double_text_rate": [(0.0, 35), (0.15, 55), (0.35, 75), (0.6, 88)],
    "question_rate": [(0.0, 25), (0.1, 45), (0.25, 65), (0.4, 82), (0.6, 90)],
    "char_share": [(0.1, 15), (0.3, 40), (0.45, 60), (0.55, 72), (0.7, 85), (0.85, 92)],
    "length_ratio": [(0.3, 20), (0.7, 45), (1.0, 62), (1.5, 78), (2.5, 90)],
    "media_share": [(0.0, 40), (0.05, 55), (0.15, 70), (0.3, 80)],
    "care_rate": [(0.0, 30), (0.03, 50), (0.08, 68), (0.15, 82), (0.3, 92)],
    "laugh_rate": [(0.0, 35), (0.05, 50), (0.15, 65), (0.3, 78)],
    "affection_rate": [(0.0, 40), (0.02, 60), (0.06, 78), (0.15, 92)],
    "future_rate": [(0.0, 25), (0.03, 45), (0.08, 65), (0.15, 80), (0.3, 92)],
    "short_reply_rate_inv": [(0.0, 85), (0.2, 70), (0.4, 50), (0.6, 30), (0.8, 15)],
    # Invitation chains (v0.5). The future dimension reads these when the material has chains at
    # all, and falls back to `future_rate` only when it does not. `alt_time_rate` is deliberately
    # inverted here (a high re-schedule rate is a low score), so the weight can stay positive.
    "propose_rate": [(0.0, 20), (0.02, 45), (0.05, 65), (0.1, 80), (0.2, 90)],
    "accept_rate": [(0.0, 10), (0.2, 30), (0.4, 50), (0.6, 70), (0.8, 88), (1.0, 96)],
    "alt_time_rate": [(0.0, 90), (0.2, 72), (0.4, 52), (0.6, 32), (0.8, 16), (1.0, 8)],
    "commit_rate": [(0.0, 25), (0.3, 50), (0.6, 72), (0.8, 85), (1.0, 92)],
}


def interp(metric: str, x: float | None) -> float | None:
    if x is None:
        return None
    pts = ANCHORS[metric]
    if x <= pts[0][0]:
        return pts[0][1]
    if x >= pts[-1][0]:
        return pts[-1][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:], strict=False):
        if x0 <= x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return None


@dataclass
class RuleDim:
    score: int | None
    confidence: Confidence
    inputs: dict
    evidence_ids: list[str]
    note: str


def _avg(parts: list[tuple[float | None, float]]) -> float | None:
    """Weighted mean over available parts."""
    tot = sum(w for v, w in parts if v is not None)
    if tot == 0:
        return None
    return sum(v * w for v, w in parts if v is not None) / tot


def _val(m: Metric) -> float | None:
    return m.value


def rule_dims(f: Features) -> dict[str, RuleDim]:
    o, me = f.other, f.me
    out: dict[str, RuleDim] = {}

    # responsiveness
    parts = [
        (interp("median_reply_s", _val(o.median_reply_s)), 0.5),
        (interp("reply_within_5min_rate", _val(o.reply_within_5min_rate)), 0.25),
        (interp("reply_rate_24h", _val(o.reply_rate_24h)), 0.25),
    ]
    s = _avg(parts)
    n = o.median_reply_s.n
    out["responsiveness"] = RuleDim(
        _r(s),
        Confidence.high if n >= 15 else Confidence.medium if n >= 5 else Confidence.low,
        {"median_reply_s": o.median_reply_s.d(), "reply_within_5min_rate": o.reply_within_5min_rate.d(), "reply_rate_24h": o.reply_rate_24h.d()},
        [],
        "无可靠时间，回应性不可用" if s is None else f"对方中位回复 {_fmt_s(o.median_reply_s.value)}，24h 内回复率 {_pct(o.reply_rate_24h.value)}",
    )

    # initiative
    parts = [
        (interp("initiation_rate", _val(o.initiation_rate)), 0.5),
        (interp("double_text_rate", _val(o.double_text_rate)), 0.2),
        (interp("question_rate", _val(o.question_rate)), 0.3),
    ]
    s = _avg(parts)
    out["initiative"] = RuleDim(
        _r(s),
        Confidence.medium if f.n_sessions >= 5 else Confidence.low,
        {"initiation_rate": o.initiation_rate.d(), "double_text_rate": o.double_text_rate.d(), "question_rate": o.question_rate.d()},
        o.question_ids[:5],
        f"对方发起 {_pct(o.initiation_rate.value)} 的会话，提问率 {_pct(o.question_rate.value)}",
    )

    # investment
    total_chars = o.chars + me.chars
    char_share = (o.chars / total_chars) if total_chars else None
    total_msgs = o.msgs + me.msgs
    media_share = (o.media / o.msgs) if o.msgs else None
    parts = [
        (interp("char_share", char_share), 0.4),
        (interp("length_ratio", _val(f.length_ratio)), 0.3),
        (interp("media_share", media_share), 0.1),
        (interp("short_reply_rate_inv", _val(o.short_reply_rate)), 0.2),
    ]
    s = _avg(parts)
    out["investment"] = RuleDim(
        _r(s),
        Confidence.medium if o.texts >= 30 else Confidence.low,
        {"char_share": char_share, "length_ratio": f.length_ratio.d(), "media_share": media_share, "short_reply_rate": o.short_reply_rate.d(), "total_msgs": total_msgs},
        [],
        f"对方字数占比 {_pct(char_share)}，平均长度是你的 {f.length_ratio.value:.1f} 倍" if f.length_ratio.value else "文本不足",
    )

    # warmth (lexicon only → low confidence)
    parts = [
        (interp("care_rate", _val(o.care_rate)), 0.45),
        (interp("laugh_rate", _val(o.laugh_rate)), 0.25),
        (interp("affection_rate", _val(o.affection_rate)), 0.30),
    ]
    s = _avg(parts)
    out["warmth"] = RuleDim(
        _r(s),
        Confidence.low,
        {"care_rate": o.care_rate.d(), "laugh_rate": o.laugh_rate.d(), "affection_rate": o.affection_rate.d()},
        (o.care_ids + o.affection_ids + o.laugh_ids)[:6],
        "词表估计：关心/幽默/亲昵用语频率（仅规则）",
    )

    # future — the invitation chains when the material has them, word frequency only as a fallback
    parts = [
        (interp("propose_rate", _val(f.propose_rate)), 0.35),
        (interp("accept_rate", _val(f.accept_rate)), 0.30),
        (interp("alt_time_rate", _val(f.alt_time_rate)), 0.15),
        (interp("commit_rate", _val(f.commit_rate)), 0.20),
    ]
    s = _avg(parts)
    if s is None:
        # No offer anywhere in the material (a lone screenshot, a single window pasted). The old
        # word-frequency estimate is all there is — and the note says that, instead of dressing it
        # up as a chain.
        s = interp("future_rate", _val(o.future_rate))
        out["future"] = RuleDim(
            _r(s),
            Confidence.low,
            {"future_rate": o.future_rate.d()},
            o.future_ids[:6],
            "词表兜底（这份材料里没有可用的邀约链）：对方提到未来安排的频率（仅规则）",
        )
        return out
    inv = f.invitations or {}
    out["future"] = RuleDim(
        _r(s),
        Confidence.low,
        {
            "propose_rate": f.propose_rate.d(),
            "accept_rate": f.accept_rate.d(),
            "alt_time_rate": f.alt_time_rate.d(),
            "commit_rate": f.commit_rate.d(),
        },
        _chain_ids(f),
        (
            f"邀约链：我提议 {inv.get('proposed', 0)} 次 · 对方接受 {inv.get('accepted', 0)}、改期 {inv.get('alt_timed', 0)}、"
            f"拒绝 {inv.get('declined', 0)}、未答 {inv.get('unanswered', 0)} · 谈定具体时间 {inv.get('committed', 0)} 次（仅规则）"
        ),
    )
    return out


def _chain_ids(f: Features, cap: int = 6) -> list[str]:
    """The messages behind the future score: the offer, the answer and any settled time."""
    out: list[str] = []
    for c in (f.invitations or {}).get("chains", []):
        out.append(c["propose_id"])
        if c.get("response_id"):
            out.append(c["response_id"])
        out.extend(c.get("commit_ids") or [])
        if len(out) >= cap:
            break
    return out[:cap]


def _r(x: float | None) -> int | None:
    return None if x is None else int(round_half_up(x))


def round_half_up(x: float) -> int:
    import math

    return int(math.floor(x + 0.5))


def _fmt_s(s: float | None) -> str:
    if s is None:
        return "—"
    if s < 60:
        return f"{int(s)} 秒"
    if s < 3600:
        return f"{s / 60:.0f} 分钟"
    return f"{s / 3600:.1f} 小时"


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.0f}%"


def rubric_digest() -> str:
    """Content hash over every scoring parameter here plus the affinity gates, so a changed
    anchor or weight changes RUBRIC_VERSION without anyone remembering to bump it."""
    import hashlib
    import json

    from rapport.scoring import affinity as _aff

    payload = {
        "weights": WEIGHTS,
        "anchors": ANCHORS,
        "coverage_min": COVERAGE_MIN,
        "min_other_texts": MIN_OTHER_TEXTS,
        "min_sessions": MIN_SESSIONS,
        "neff_full": _aff.NEFF_FULL,
        "min_half_texts": _aff.MIN_HALF_TEXTS,
        "stage_beta": STAGE_BETA,
        "reliability": RELIABILITY,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:10]
