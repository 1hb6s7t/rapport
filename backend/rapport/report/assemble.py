"""Report assembly: features + scoring (+ validated LLM output) -> report-v1."""

from __future__ import annotations

import uuid
from datetime import datetime

from rapport import config as _config
from rapport.config import APP_VERSION, PARSER_VERSION
from rapport.features.compute import Features
from rapport.knowledge import (
    chinese_zodiac_pair,
    compare_mbti,
    fallback_playbook,
    rule_situations,
    zodiac_pair,
)
from rapport.knowledge.prior_check import cards_for, check_priors
from rapport.llm.validate import Validated
from rapport.schemas.message_v1 import Conversation, Message, Sender
from rapport.schemas.profile import Profile
from rapport.schemas.report_v1 import (
    Cost,
    DoneReview,
    EvidenceSnippet,
    Fun,
    IntentReading,
    Landmine,
    Mode,
    Persona,
    PriorBreakdownModel,
    PriorCheckModel,
    PriorHypothesis,
    Report,
    Scope,
    Versions,
)
from rapport.scoring import (
    build_affinity,
    build_trend,
    constrain_stage,
    evaluate_transition,
    merge_boundary,
    rule_boundary,
    rule_stage,
)
from rapport.scoring.prior import DEFAULT_POLICY, PriorPolicy, build_hypotheses


def _scope(conv: Conversation, events: list[Message], f: Features, coverage: float) -> Scope:
    flags: list[str] = []
    if not f.has_reliable_time:
        flags.append("no_reliable_time")
    if f.other.texts < 20:
        flags.append("few_other_texts")
    if any(m.sender == Sender.unknown for m in events):
        flags.append("unknown_senders")
    if len(events) < len(conv.events()):
        flags.append("range_truncated")
    return Scope(
        msg_total=len(conv.messages),
        msg_analyzed=len(events),
        events_other=f.other.msgs,
        events_me=f.me.msgs,
        text_other=f.other.texts,
        text_me=f.me.texts,
        sessions=f.n_sessions,
        range_start=f.range_start,
        range_end=f.range_end,
        has_reliable_time=f.has_reliable_time,
        coverage=coverage,
        quality_flags=flags,
    )


def _collect_evidence(conv: Conversation, ids: set[str]) -> dict[str, EvidenceSnippet]:
    by_id = {m.id: m for m in conv.messages}
    out: dict[str, EvidenceSnippet] = {}
    for i in ids:
        m = by_id.get(i)
        if not m:
            continue
        text = m.text if m.is_text else f"[{m.type.value}]"
        out[i] = EvidenceSnippet(sender=m.sender.value, ts=m.ts, text=(text or "")[:160])
    return out


def _all_ids(report_parts: list) -> set[str]:
    ids: set[str] = set()

    def walk(o):
        if hasattr(o, "model_dump"):
            o = o.model_dump()
        if isinstance(o, dict):
            for k, v in o.items():
                if k.endswith("_ids") and isinstance(v, list):
                    ids.update(x for x in v if isinstance(x, str))
                else:
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    for p in report_parts:
        walk(p)
    return ids


def rule_landmines(f: Features) -> list[Landmine]:
    out: list[Landmine] = []
    if f.boundary_hits.get("decline_with_alternative"):
        out.append(Landmine(topic="重复同一提议", why="对方曾拒绝过一次安排但给了替代时间，重复原提议可能显得没听进去。", evidence_ids=f.boundary_hits["decline_with_alternative"][:3]))
    if f.conflict_ids:
        out.append(Landmine(topic="旧冲突话题", why="材料中出现过争执用语，重提时注意先确认对方状态。", evidence_ids=f.conflict_ids[:3]))
    if f.other.late_night_rate.value is not None and f.other.late_night_rate.value < 0.05 and f.me.late_night_rate.value and f.me.late_night_rate.value > 0.3:
        out.append(Landmine(topic="深夜消息", why="对方几乎不在深夜发消息，而你经常在深夜发，注意节奏差异。"))
    return out


DONE_VERDICT = {
    "accepted": "对方接受了：这条路走得通，可以在同一方向上再进一步。",
    "alt_time": "对方改了时间但没有拒绝：接住对方给的时间，不要再抛新提议。",
    "declined": "对方拒绝了：同一目标的推进选项本次不再推荐，先回到日常交流。",
    "no_reply": "对方没有回应：不要用更多消息去填；先观察对方是否主动。",
    "other": "结果不明确：按你记录的备注判断，本报告不据此调整。",
}


def build_done_review(last_actions: list[dict], boundary: str) -> list[DoneReview]:
    out: list[DoneReview] = []
    for a in last_actions[:6]:
        observed = str(a.get("observed") or "other")
        out.append(
            DoneReview(
                option_id=str(a.get("option_id") or "")[:40],
                goal=str(a.get("goal") or "")[:120],
                observed=observed,
                note=str(a.get("note") or "")[:300],
                verdict=DONE_VERDICT.get(observed, DONE_VERDICT["other"]),
            )
        )
    return out


def assemble(
    conv: Conversation,
    events: list[Message],
    f: Features,
    profile: Profile,
    mode: Mode,
    validated: Validated | None = None,
    window_overrides: dict[int, dict[str, int]] | None = None,
    window_evidence: dict[int, list[str]] | None = None,
    cost: Cost | None = None,
    versions_extra: dict | None = None,
    ai_status: str = "not_requested",
    ai_error: str | None = None,
    report_id: str | None = None,
    intent_readings: list[IntentReading] | None = None,
    change_points: list | None = None,
    change_note: str = "",
    n_eff: float = 0.0,
    evidence_counts: dict[str, int] | None = None,
    previous_stage: str | None = None,
    last_actions: list[dict] | None = None,
    prior_policy: PriorPolicy | None = None,
) -> Report:
    rb = rule_boundary(f)
    boundary = merge_boundary(rb, validated.boundary if validated else None)
    rs = rule_stage(f, profile, boundary)
    stage = constrain_stage(validated.stage if validated else None, rs, f, profile)
    # The ladder rules live here so Rules mode and AI mode share them: no skipped rungs, 稳定
    # only with durability evidence, and regressions recorded rather than hidden.
    stage = evaluate_transition(previous_stage, stage, f, boundary)
    affinity = build_affinity(
        f,
        validated.llm_dims if validated else None,
        boundary.value,
        events,
        n_eff,
        stage=getattr(stage, "value", stage),
    )
    trend = build_trend(
        conv, events, f.has_reliable_time, window_overrides, window_evidence,
        change_points=change_points, change_note=change_note,
    )
    landmines = rule_landmines(f)
    if validated and validated.playbook:
        playbook = validated.playbook
        seen = {ln.topic for ln in playbook.landmines}
        playbook.landmines = (playbook.landmines + [ln for ln in landmines if ln.topic not in seen])[:4]
        # under boundary, put the boundary skeleton first as advice; nothing is removed
        if boundary.value != "none":
            fb = fallback_playbook(profile.goal, boundary.value, stage.value)
            playbook.actions = ([fb.actions[0]] + [a for a in playbook.actions])[:4]
            playbook.note = fb.note
    else:
        playbook = fallback_playbook(profile.goal, boundary.value, stage.value, landmines)
    # v0.5 §2: situations. The model's (validated) situations take precedence; the rule layer fills in
    # when the model gave none, and always supplies the boundary-safe set under a boundary.
    if not playbook.situations:
        playbook.situations = rule_situations(f, profile.goal, boundary.value)
        if playbook.situations and (not playbook.actions or all(not a.personalized for a in playbook.actions)):
            from rapport.llm.validate import _option_to_action

            # The goal skeleton stays first (it is what changes when the user changes goal); the
            # situations' first options follow so old clients still see them in `actions`.
            skeleton = list(playbook.actions[:1])
            playbook.actions = (skeleton + [_option_to_action(s.options[0]) for s in playbook.situations if s.options])[:3]
    playbook.done_review = build_done_review(last_actions or [], boundary.value)
    if playbook.done_review:
        declined_goals = {d.goal for d in playbook.done_review if d.observed == "declined"}
        if declined_goals:
            # Do not recommend again what the other person already turned down.
            playbook.situations = [
                s.model_copy(update={"options": [o for o in s.options if o.goal not in declined_goals]}) for s in playbook.situations
            ]
            playbook.situations = [s for s in playbook.situations if s.options]
            playbook.actions = [a for a in playbook.actions if a.goal not in declined_goals]
    persona = validated.persona if validated else Persona()
    if persona.available and profile.other.mbti:
        persona.mbti_comparison = compare_mbti(profile.other.mbti, persona.inferred_axes)
    observations = validated.observations if validated else []
    fun = Fun(
        zodiac_pair=zodiac_pair(profile.me.zodiac, profile.other.zodiac),
        chinese_zodiac_pair=chinese_zodiac_pair(profile.me.chinese_zodiac, profile.other.chinese_zodiac),
    )
    readings = intent_readings or []
    hypotheses, prior_breakdown = build_hypotheses(
        n_eff,
        {
            "mbti": profile.other.mbti,
            "zodiac": profile.other.zodiac.value if profile.other.zodiac else None,
            "chinese_zodiac": profile.other.chinese_zodiac.value if profile.other.chinese_zodiac else None,
        },
        prior_policy or DEFAULT_POLICY,
    )
    ids = _all_ids([affinity, observations, persona, stage, boundary, playbook, readings])
    evidence = _collect_evidence(conv, ids)
    notes: list[str] = []
    model_readings = sum(1 for r in readings if r.source == "local_model")
    grounded_readings = sum(1 for r in readings if r.source == "grounded")
    if mode == Mode.rules:
        notes.append("规则模式：情感温度与未来指向仅基于词表，置信度低；性格侧写与个性化建议需要开启 AI 分析。")
        if model_readings:
            notes.append("逐句情绪由本机小模型离线判断（StructBERT-tiny，Apache-2.0），未经人工校准，只作参考；不参与客观关系分数。")
        if grounded_readings:
            notes.append(
                "逐句判断来自离线研究层：BGE-small-zh 近邻检索 + 温度校准后的情绪小模型 + 中文礼貌标记；"
                "标签来自本机弱标注语料（AI 辅助标注，非人工金标），只作参考，不参与客观关系分数。"
            )
    llm_readings = [r for r in readings if r.source not in {"local_model", "grounded"}]
    if llm_readings:
        thin = sum(1 for r in llm_readings if r.independent_units < 2)
        notes.append(
            f"语义与情绪判断共 {len(llm_readings)} 处，均带上下文证据与备选解释"
            + (f"；其中 {thin} 处只有单个片段支持，置信度低" if thin else "")
            + "。"
        )
    if change_points:
        notes.append(f"趋势检测到 {len(change_points)} 个探索性变化点（ruptures PELT，多参数一致），仅供定位查看，不是结论。")
    if validated and validated.dropped:
        notes.append("校验时剔除了部分无证据或不合规的模型输出：" + ", ".join(f"{k}×{v}" for k, v in validated.dropped.items()))
    ve = dict(versions_extra or {})
    return Report(
        id=report_id or uuid.uuid4().hex[:12],
        created_at=datetime.now(),
        mode=mode,
        synthetic=conv.source == "demo",
        versions=Versions(
            app=APP_VERSION,
            parser=PARSER_VERSION,
            features=_config.FEATURES_VERSION,
            rubric=_config.RUBRIC_VERSION,
            prompts=_config.PROMPTS_VERSION if mode == Mode.byok else None,
            provider=ve.get("provider"),
            model=ve.get("model"),
            price_date=ve.get("price_date"),
        ),
        scope=_scope(conv, events, f, affinity.coverage),
        affinity=affinity,
        trend=trend,
        observations=observations,
        intent_readings=readings,
        priors=[PriorHypothesis(**h.as_dict()) for h in hypotheses],
        prior_checks=[PriorCheckModel(**c.as_dict()) for c in check_priors(cards_for({"mbti": profile.other.mbti}), persona.tendencies)],
        prior_breakdown=PriorBreakdownModel(n_eff=round(n_eff, 2), evidence_counts=dict(evidence_counts or {}), **prior_breakdown.as_dict()),
        persona=persona,
        stage=stage,
        boundary=boundary,
        playbook=playbook,
        fun=fun,
        evidence=evidence,
        cost=cost or Cost(),
        features=f.d(),
        notes=notes,
        ai_status=ai_status,
        ai_error=ai_error,
    )


def refresh_labels(report: Report, profile: Profile, prior_policy: PriorPolicy | None = None) -> Report:
    """Recompute only the label-dependent parts (MBTI comparison, fun). Core untouched."""
    if report.persona.available and profile.other.mbti:
        report.persona.mbti_comparison = compare_mbti(profile.other.mbti, report.persona.inferred_axes)
    else:
        report.persona.mbti_comparison = None
    report.fun = Fun(
        zodiac_pair=zodiac_pair(profile.me.zodiac, profile.other.zodiac),
        chinese_zodiac_pair=chinese_zodiac_pair(profile.me.chinese_zodiac, profile.other.chinese_zodiac),
    )
    hypotheses, breakdown = build_hypotheses(
        report.prior_breakdown.n_eff,
        {
            "mbti": profile.other.mbti,
            "zodiac": profile.other.zodiac.value if profile.other.zodiac else None,
            "chinese_zodiac": profile.other.chinese_zodiac.value if profile.other.chinese_zodiac else None,
        },
        prior_policy or DEFAULT_POLICY,
    )
    report.priors = [PriorHypothesis(**h.as_dict()) for h in hypotheses]
    report.prior_checks = [PriorCheckModel(**c.as_dict()) for c in check_priors(cards_for({"mbti": profile.other.mbti}), report.persona.tendencies)]
    report.prior_breakdown = PriorBreakdownModel(n_eff=round(report.prior_breakdown.n_eff, 2), **breakdown.as_dict())
    return report
