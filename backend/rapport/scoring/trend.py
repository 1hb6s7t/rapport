"""Trend: per-bucket affinity from rule features (optionally LLM window signals)."""

from __future__ import annotations

from rapport.features.compute import compute_features
from rapport.features.windows import trend_buckets
from rapport.schemas.message_v1 import Conversation, Message
from rapport.schemas.report_v1 import DIMENSIONS, DimScore, Trend, TrendPoint
from rapport.scoring.affinity import aggregate
from rapport.scoring.rubric import rule_dims

MIN_BUCKET_MSGS = 8


def build_trend(
    conv: Conversation,
    events: list[Message],
    has_time: bool,
    window_overrides: dict[int, dict[str, int]] | None = None,
    window_evidence: dict[int, list[str]] | None = None,
    change_points: list | None = None,
    change_note: str = "",
) -> Trend:
    """window_overrides: {bucket_index: {"warmth": 70, "future": 40}} from LLM window summaries.
    window_evidence: {bucket_index: [message ids]} the model actually read for that segment, so a
    point can show both halves of its evidence — the rule-side ids and the ones the model cited."""
    kind, buckets = trend_buckets(events, has_time)
    points: list[TrendPoint] = []
    for w in buckets:
        n_other_text = sum(1 for m in w.messages if m.sender == "other" and m.is_text)
        insufficient = len(w.messages) < MIN_BUCKET_MSGS
        score = None
        dims_out: dict[str, int | None] = {}
        dim_ev: dict[str, list[str]] = {}
        dim_in: dict[str, dict] = {}
        ov_used: dict[str, int] = {}
        ids: list[str] = []
        if not insufficient:
            f = compute_features(conv, w.messages)
            rd = rule_dims(f)
            dims: dict[str, DimScore] = {}
            for k in DIMENSIONS:
                s = rd[k].score
                ov = (window_overrides or {}).get(w.index, {})
                if k in ov and ov[k] is not None:
                    s = ov[k] if s is None else int(round(0.5 * s + 0.5 * ov[k]))
                    ov_used[k] = ov[k]
                dims[k] = DimScore(score=s)
                dims_out[k] = s
                dim_ev[k] = list(rd[k].evidence_ids)
                dim_in[k] = dict(rd[k].inputs)
                ids.extend(rd[k].evidence_ids)
            ids.extend((window_evidence or {}).get(w.index, []))
            score, _, cov = aggregate(dims)
            if cov < 0.6:
                score, insufficient = None, True
        points.append(
            TrendPoint(
                label=w.label,
                start=w.start,
                end=w.end,
                score=score,
                n_msgs=len(w.messages),
                n_other_text=n_other_text,
                insufficient=insufficient,
                dims=dims_out,
                evidence_ids=list(dict.fromkeys(ids))[:12],
                dim_evidence=dim_ev,
                dim_inputs=dim_in,
                llm_override=ov_used,
            )
        )
    return Trend(
        bucket=kind,
        points=points,
        momentum=_momentum(points),
        note=_note(kind, has_time),
        change_points=change_points or [],
        change_note=change_note,
    )


def _momentum(points: list[TrendPoint]) -> str:
    valid = [p.score for p in points if p.score is not None]
    if len(valid) < 4:
        return "unknown"
    head = sum(valid[:2]) / 2
    tail = sum(valid[-2:]) / 2
    if tail - head >= 8:
        return "rising"
    if head - tail >= 8:
        return "falling"
    return "flat"


def _note(kind: str, has_time: bool) -> str:
    if not has_time:
        return "材料缺少可靠时间，趋势按消息顺序等分为若干段，不代表真实时间跨度。"
    return {"day": "按天分段", "week": "按周分段", "month": "按月分段"}.get(kind, "") + "；样本少于 8 条的分段不计分。"
