"""Analysis pipeline: Stage 2 (rules) and Stages 4-8 (BYOK). Pure functions + a checkpoint hook."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from rapport.features import Features, compute_features
from rapport.features.episodes import select_targets
from rapport.knowledge.reference import load_library, load_patterns, retrieve
from rapport.llm.budget import actual_cost, estimate_tokens, plan_context_budget
from rapport.llm.extract import (
    RedactedPayload,
    Signal,
    WindowResult,
    apply_line_edits,
    lines_by_id,
    run_extraction,
)
from rapport.llm.intent import build_intent_input, estimate_intent_tokens, run_intent, validate_intent
from rapport.llm.provider import CallUsage, LLMError, LLMProvider
from rapport.llm.redact import RedactStats
from rapport.llm.synthesize import build_synth_user, run_synthesis
from rapport.llm.validate import ValidationFailed, validate_synthesis
from rapport.report.assemble import assemble
from rapport.schemas.message_v1 import Conversation, Message
from rapport.schemas.profile import Profile
from rapport.schemas.report_v1 import Cost, IntentReading, Mode, Report, Usage
from rapport.scoring.affinity import independent_units
from rapport.scoring.prior import PriorPolicy


class Cancelled(Exception):
    pass


@dataclass
class Progress:
    stage: str = "created"
    done: int = 0
    total: int = 0
    message: str = ""


@dataclass
class PipelineHooks:
    progress: Callable[[Progress], None] = lambda p: None
    is_cancelled: Callable[[], bool] = lambda: False
    checkpoint: Callable[[str, Any], None] = lambda k, v: None
    load_checkpoint: Callable[[str], Any] = lambda k: None


@dataclass
class ByokContext:
    provider: LLMProvider
    payload: RedactedPayload
    # v0.8: optional per-task providers (local split profile). Missing keys fall back to `provider`.
    providers: dict[str, LLMProvider] = field(default_factory=dict)

    def provider_for(self, task: str) -> LLMProvider:
        return self.providers.get(task, self.provider)

    edited_lines: dict[int, list[str]] = field(default_factory=dict)  # user edits in preview
    versions_extra: dict = field(default_factory=dict)
    preset: str = "custom"
    run_intent: bool = True  # M03 contextual intent reading (one model call per episode)
    max_intent_targets: int = 8
    use_patterns: bool = False  # pragmatic-pattern reference layer (docs/EVAL.md §12)
    use_cues: bool = False  # structural cues block in the intent prompt (v0.5 §3A)
    intent_samples: int = 1  # v0.5 §3B: k sampled draws per episode (1 = single deterministic call)
    last_actions: list[dict] = field(default_factory=list)  # 「我做了」 since the previous report (v0.5 §2)


def _run_intent_stage(
    conv: Conversation,
    events: list[Message],
    f: Features,
    ctx: ByokContext,
    hooks: PipelineHooks,
    usage: CallUsage,
    gap_hours: float,
) -> list[IntentReading]:
    """M03: read the intent of selected messages, one episode per call.

    Boundary evidence is read first (it is the most consequential), then replies after a
    silence, then each block's last reply. Reference examples are dropped first when the
    context budget is tight — the episode and the boundary evidence are never trimmed.
    """
    readings: list[IntentReading] = []
    boundary_ids = [i for ids in f.boundary_hits.values() for i in ids]
    sel = select_targets(
        conv,
        events,
        gap_hours=gap_hours,
        max_targets=ctx.max_intent_targets,
        mode="review",
        boundary_ids=boundary_ids,
    )
    if sel.skipped_reason:
        hooks.progress(Progress("intent", 0, 0, f"跳过语义判断：{sel.skipped_reason}"))
        return readings

    library = load_library()
    # The pattern layer is optional and off by default; when on, it is reference material only
    # (never evidence, never retrieved by wording).
    patterns = []
    if ctx.use_patterns:
        patterns = [p.prompt_dict() for p in load_patterns()]
    units, _kind = independent_units(f, events)
    # Same content the user previewed and confirmed, edits included.
    overrides = lines_by_id(apply_line_edits(ctx.payload.lines_by_window, ctx.edited_lines))
    total = len(sel.episodes)
    for i, episode in enumerate(sel.episodes):
        if hooks.is_cancelled():
            raise Cancelled()
        hooks.progress(Progress("intent", i, total, f"判断第 {i + 1}/{total} 处语义"))
        cached = hooks.load_checkpoint(f"intent:{i}")
        if cached:
            readings.append(IntentReading(**cached))
            continue
        query = " ".join(m.text or "" for m in episode.messages)
        refs = [e.prompt_dict() for e in retrieve(query, library, k=4)]
        inp = build_intent_input(conv, episode, RedactStats(), refs, overrides, patterns=patterns, cues=ctx.use_cues)
        budget = plan_context_budget(
            {
                "episode": estimate_tokens("\n".join(inp.lines)),
                "reference_examples": estimate_tokens(inp.reference_block),
            }
        )
        if "reference_examples" in budget.reduced:
            inp = build_intent_input(conv, episode, line_overrides=overrides, patterns=patterns, cues=ctx.use_cues)  # drop analogues, keep the episode
        if ctx.intent_samples >= 2:
            from rapport.llm.sampling import run_intent_sampled

            data, u, _draws = run_intent_sampled(ctx.provider_for("intent"), inp, k=ctx.intent_samples)
        else:
            data, u = run_intent(ctx.provider_for("intent"), inp)
        usage.add(u)
        reading = validate_intent(data, inp, independent_units=units)
        readings.append(reading)
        hooks.checkpoint(f"intent:{i}", reading.model_dump(mode="json"))
    hooks.progress(Progress("intent", total, total, "语义判断完成"))
    return readings


def run_rules(
    conv: Conversation,
    events: list[Message],
    profile: Profile,
    gap_hours: float = 6.0,
    report_id: str | None = None,
    n_eff: float = 0.0,
    previous_stage: str | None = None,
    evidence_counts: dict[str, int] | None = None,
    last_actions: list[dict] | None = None,
    prior_policy: PriorPolicy | None = None,
    intent_readings: list[IntentReading] | None = None,
    change_points: list | None = None,
    change_note: str = "",
) -> Report:
    f = compute_features(conv, events, gap_hours)
    return assemble(
        conv,
        events,
        f,
        profile,
        Mode.rules,
        report_id=report_id,
        n_eff=n_eff,
        previous_stage=previous_stage,
        evidence_counts=evidence_counts,
        last_actions=last_actions,
        prior_policy=prior_policy,
        intent_readings=intent_readings,
        change_points=change_points,
        change_note=change_note,
    )


TRANSIENT = ("rate_limit", "timeout", "connection", "status")
RETRY_WAITS = (5, 20, 60)  # seconds; a long run must survive a burst of 429s without losing its windows


def _extract_with_retry(ctx: ByokContext, i: int, hooks: PipelineHooks):
    last: LLMError | None = None
    for attempt, wait in enumerate((0, *RETRY_WAITS)):
        if wait:
            hooks.progress(Progress("extracting", i, len(ctx.payload.windows), f"第 {i + 1} 段遇到 {last.code if last else '错误'}，{wait} 秒后重试（第 {attempt} 次）"))
            for _ in range(wait):
                if hooks.is_cancelled():
                    raise Cancelled()
                time.sleep(1)
        try:
            return run_extraction(ctx.provider_for("extract"), ctx.payload, i, ctx.edited_lines.get(i))
        except LLMError as e:
            if e.code not in TRANSIENT:
                raise
            last = e
    assert last is not None
    raise last


def run_byok(
    conv: Conversation,
    events: list[Message],
    profile: Profile,
    ctx: ByokContext,
    hooks: PipelineHooks | None = None,
    gap_hours: float = 6.0,
    report_id: str | None = None,
    n_eff: float = 0.0,
    evidence_counts: dict[str, int] | None = None,
    previous_stage: str | None = None,
    prior_policy: PriorPolicy | None = None,
) -> Report:
    hooks = hooks or PipelineHooks()
    f = compute_features(conv, events, gap_hours)
    usage = CallUsage()
    n = len(ctx.payload.windows)
    results: list[WindowResult] = []

    # Stage 4 — extraction with per-window checkpoints
    for i in range(n):
        if hooks.is_cancelled():
            raise Cancelled()
        hooks.progress(Progress("extracting", i, n, f"提取第 {i + 1}/{n} 段"))
        cached = hooks.load_checkpoint(f"window:{i}")
        if cached:
            c = dict(cached)
            c["signals"] = [Signal(**x) if isinstance(x, dict) else x for x in c.get("signals", [])]
            results.append(WindowResult(**c))
            continue
        wr, u = _extract_with_retry(ctx, i, hooks)
        usage.add(u)
        results.append(wr)
        hooks.checkpoint(f"window:{i}", _wr_dump(wr))
    hooks.progress(Progress("extracting", n, n, "提取完成"))

    # Stage 5/6 — synthesis + validation (one repair attempt)
    if hooks.is_cancelled():
        raise Cancelled()
    hooks.progress(Progress("synthesizing", 0, 1, "综合分析"))
    valid_ids = {i for r in results for i in r.msg_ids}
    user = build_synth_user(f, results, profile, ctx.last_actions)
    validated = None
    ai_error: str | None = None
    try:
        data, u = run_synthesis(ctx.provider_for("synthesize"), user)
        usage.add(u)
        hooks.progress(Progress("validating", 0, 1, "校验证据"))
        try:
            validated = validate_synthesis(data, valid_ids)
        except ValidationFailed as e:
            if hooks.is_cancelled():
                raise Cancelled() from None
            hooks.progress(Progress("synthesizing", 0, 1, "输出不合规，尝试修复一次"))
            data, u = run_synthesis(ctx.provider_for("synthesize"), user, repair_error=str(e))
            usage.add(u)
            validated = validate_synthesis(data, valid_ids)
    except (LLMError, ValidationFailed) as e:
        ai_error = getattr(e, "msg", None) or str(e)
        validated = None

    if hooks.is_cancelled():
        raise Cancelled()

    overrides = {r.index: {"warmth": r.warmth, "future": r.future} for r in results if r.warmth is not None}
    # map extraction windows -> trend buckets is approximate; we pass per-window values keyed by window index
    trend_overrides, trend_evidence = _map_overrides_to_buckets(ctx.payload, results, events, f.has_reliable_time)

    # Stage 7 — contextual intent reading (M03)
    intent_readings: list[IntentReading] = []
    if ctx.run_intent and not hooks.is_cancelled():
        intent_readings = _run_intent_stage(conv, events, f, ctx, hooks, usage, gap_hours)
    intent_in, intent_out, _intent_calls = estimate_intent_tokens(
        conv, events, gap_hours, ctx.max_intent_targets, reasoning=getattr(ctx.provider, "reasoning", False)
    ) if ctx.run_intent else (0, 0, 0)
    cost = Cost(
        est_tokens_in=ctx.payload.est_tokens_in + intent_in,
        est_tokens_out=ctx.payload.est_tokens_out + intent_out,
        actual_usage=Usage(prompt_tokens=usage.prompt_tokens, completion_tokens=usage.completion_tokens, calls=usage.calls),
        actual_cny=None if usage.unknown else actual_cost(usage.prompt_tokens, usage.completion_tokens, ctx.preset),
    )
    hooks.progress(Progress("assembling", 0, 1, "生成报告"))
    report = assemble(
        conv,
        events,
        f,
        profile,
        Mode.byok,
        validated=validated,
        window_overrides=trend_overrides,
        window_evidence=trend_evidence,
        cost=cost,
        versions_extra=ctx.versions_extra,
        intent_readings=intent_readings,
        n_eff=n_eff,
        evidence_counts=evidence_counts,
        last_actions=ctx.last_actions,
        prior_policy=prior_policy,
        previous_stage=previous_stage,
        ai_status="completed" if validated else "failed",
        ai_error=ai_error,
        report_id=report_id,
    )
    if not validated:
        report.notes.append("AI 综合未通过校验或调用失败，已回退为规则结果 + 通用建议骨架。")
    _ = overrides
    return report


def _wr_dump(wr: WindowResult) -> dict:
    return {
        "index": wr.index,
        "warmth": wr.warmth,
        "future": wr.future,
        "tone": wr.tone,
        "signals": [s.__dict__ for s in wr.signals],
        "notable": wr.notable,
        "msg_ids": wr.msg_ids,
    }


def _map_overrides_to_buckets(
    payload: RedactedPayload, results: list[WindowResult], events: list[Message], has_time: bool
) -> tuple[dict[int, dict[str, int]], dict[int, list[str]]]:
    """Assign each trend bucket the mean warmth/future of extraction windows overlapping it.

    Returns the overrides and, alongside them, the message ids the model actually read for that
    bucket — a trend point has to be able to show both the rule-side evidence and the model side.
    """
    from rapport.features.windows import trend_buckets

    _, buckets = trend_buckets(events, has_time)
    id_to_vals: dict[str, list[tuple[int | None, int | None]]] = {}
    for r in results:
        for mid in r.msg_ids:
            id_to_vals.setdefault(mid, []).append((r.warmth, r.future))
    out: dict[int, dict[str, int]] = {}
    evidence: dict[int, list[str]] = {}
    for b in buckets:
        ws: list[int] = []
        fs: list[int] = []
        ids: list[str] = []
        for m in b.messages:
            if m.id in id_to_vals:
                ids.append(m.id)
            for w, f_ in id_to_vals.get(m.id, []):
                if w is not None:
                    ws.append(w)
                if f_ is not None:
                    fs.append(f_)
        if ids:
            evidence[b.index] = ids
        if ws or fs:
            out[b.index] = {}
            if ws:
                out[b.index]["warmth"] = int(sum(ws) / len(ws))
            if fs:
                out[b.index]["future"] = int(sum(fs) / len(fs))
    return out, evidence
