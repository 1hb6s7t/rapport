"""Versioned evidence context shared by reports and reply generation.

No inferred future-visible label is reused as an online fact. Auxiliary profile
facts have a capped product-policy role; they never alter objective scoring.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from rapport.db import ReportRow
from rapport.llm.redact import redact_names, redact_text
from rapport.scoring.prior import DEFAULT_POLICY, LABEL_KEYS, policy_version


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def _prior_policy(state):
    """The user's label budget (设置 → 个性化权重); a bare or fake state means the product default."""
    getter = getattr(state, "prior_policy", None)
    try:
        return getter() if callable(getter) else DEFAULT_POLICY
    except Exception:  # noqa: BLE001 - a broken settings row must not block drafting
        return DEFAULT_POLICY


def _person(state, rid, own=False):
    method = getattr(state, "self_profile" if own else "relationship_person", None)
    if method:
        value = method() if own else method(rid)
        return value.model_dump(mode="json") if hasattr(value, "model_dump") else dict(value)
    return {}


def _clean(value, names):
    if isinstance(value, str):
        return redact_text(redact_names(value, names))
    if isinstance(value, list):
        return [_clean(v, names) for v in value]
    if isinstance(value, dict):
        return {k: _clean(v, names) for k, v in value.items() if k not in {"alias", "name", "path", "source_path"}}
    return value


def _time(value, zone="Asia/Shanghai"):
    if not value:
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            tz = timezone(timedelta(hours=8)) if zone == "Asia/Shanghai" else ZoneInfo(zone)
            parsed = parsed.replace(tzinfo=tz)
        return parsed
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        return None


def build_context(state, relationship_id, imp, conv, episode=None, purpose="reply", report=None):
    from rapport.config import FEATURES_VERSION, PROMPTS_VERSION

    start = imp.range_start if imp.range_start is not None else 0
    end = imp.range_end if imp.range_end is not None else len(conv.messages) - 1
    scope_digest = _digest([{"id": m.id, "sender": m.sender.value, "text": m.text, "ts": m.ts, "type": m.type.value}
                            for m in conv.messages if start <= m.seq <= end])
    messages = [m for m in (episode.messages if episode else conv.events()) if start <= m.seq <= end]
    valid_ids = {m.id for m in messages}
    cutoff = episode.target if episode else (messages[-1] if messages else None)
    past_ids = {m.id for m in conv.messages if start <= m.seq <= end and (cutoff is None or m.seq <= cutoff.seq)}
    names = [(conv.participants.me, "A"), (conv.participants.other, "B")]
    persons = {"me": _person(state, relationship_id, own=True), "other": _person(state, relationship_id)}
    names.extend((p.get("alias", ""), "A" if who == "me" else "B") for who, p in persons.items())
    profile_facts = {who: _clean(p, names) for who, p in persons.items() if p.get("confirmed")}
    body = report.model_dump(mode="json") if hasattr(report, "model_dump") else report
    report_id = body.get("id") if isinstance(body, dict) else None
    if body is None:
        with state.db.session() as session:
            row = session.query(ReportRow).filter(ReportRow.relationship_id == relationship_id,
                  ReportRow.import_id == imp.id, ReportRow.deleted.is_(False)).order_by(ReportRow.created_at.desc()).first()
        body, report_id = (row.body, row.id) if row else ({}, None)
        previous_context = (body or {}).get("analysis_context") or {}
        if previous_context.get("input_digest") != scope_digest:
            body, report_id = {}, None
    body = body or {}
    readings = []
    rejected = 0
    for reading in body.get("intent_readings", []):
        ids = set(reading.get("evidence_ids") or reading.get("basis_ids") or [])
        # Legacy readings without an explicit visibility contract are review-only.
        online = reading.get("post_hoc") is False and reading.get("context_mode") == "reply"
        if purpose == "reply" and (not online or not ids or not ids <= past_ids):
            rejected += 1
            continue
        if ids and ids <= (past_ids if purpose == "reply" else valid_ids):
            readings.append(_clean(reading, names))
    features = {"messages": len(messages), "me_messages": sum(m.sender.value == "me" for m in messages),
                "other_messages": sum(m.sender.value == "other" for m in messages),
                "reliable_times": sum(m.has_reliable_ts for m in messages)}
    history = []
    try:
        from rapport.archives import get_service

        history = get_service(state).search(relationship_id, " ".join(m.text or "" for m in messages[-8:]), limit=6)
    except (ImportError, AttributeError):
        pass  # Module optional while upgrading an existing installation.
    if purpose == "reply":
        cutoff_time = _time(cutoff.ts, conv.timezone) if cutoff else None
        history = [h for h in history if cutoff_time and _time(h.get("ts"), conv.timezone) and _time(h["ts"], conv.timezone) <= cutoff_time]
    persona = body.get("persona") or {}
    boundary = body.get("boundary") or {}
    if purpose == "reply":
        # Earlier citations alone do not prove the annotator did not see later text.
        # Reuse a report-wide interpretation only if its *whole* input was visible.
        whole_report_visible = cutoff is not None and end <= cutoff.seq
        if not whole_report_visible:
            persona = {}
            boundary = {}
    model_result = None
    learning = getattr(state, "learning", None)
    if learning and hasattr(learning, "predict_analysis"):
        model_result = learning.predict_analysis([m.text or "" for m in messages if m.is_text], relationship_id=relationship_id)
    policy = _prior_policy(state)
    if policy.alpha0 <= 0:
        # A zero budget means the labels are not even shown to the drafting model.
        profile_facts = {who: {k: v for k, v in p.items() if k not in LABEL_KEYS} for who, p in profile_facts.items()}
    context = {
        "schema_version": "analysis-context-v1", "relationship_id": relationship_id, "import_id": imp.id,
        "material_hash": imp.content_hash, "purpose": purpose, "post_hoc": purpose == "review",
        "input_digest": scope_digest, "material_scope": {"start": start, "end": end},
        "analyzer_versions": {"features": FEATURES_VERSION, "prompts": PROMPTS_VERSION},
        "profile_revisions": {who: p.get("revision", 0) for who, p in persons.items()},
        "profile_facts": profile_facts, "observed": features, "readings": readings,
        "excluded_post_hoc_readings": rejected, "report_id": report_id,
        "history": _clean(history, names), "analyzer": model_result,
        "boundary": _clean(boundary, names),
        "persona": _clean(persona, names),
        "auxiliary_policy": {
            "evidence": round(1 - policy.alpha0, 4),
            "auxiliary_max": policy.alpha0,
            "affects_objective_score": False,
            "version": policy_version(policy),
        },
        "uncertainty": "观察和模型标签是待核对解释；未知不补造。回顾性画像不直接当成实时事实。",
    }
    context["id"] = _digest(context)[:24]
    return context


def rank_candidates(drafts, auxiliary_present, auxiliary_weight=.15):
    """Transparent product reranking, not a calibrated relationship probability.

    `auxiliary_weight` is the user's label budget (the same alpha0 as the report prior); it only
    applies when the profile actually carries labels, and a zero budget removes them entirely.
    """
    for draft in drafts:
        score = getattr(draft, "ranking", None) or {}
        evidence = min(1., max(0., float(score.get("evidence_fit", .5))))
        # A draft ranked before under a zero weight carries `auxiliary_fit: None`; treat as unknown.
        auxiliary_fit = score.get("auxiliary_fit")
        auxiliary = min(1., max(0., float(.5 if auxiliary_fit is None else auxiliary_fit)))
        if draft.needs_review:
            evidence = min(evidence, .25)
        weight = min(1., max(0., float(auxiliary_weight))) if auxiliary_present else 0.
        draft.ranking = {"evidence_fit": evidence, "auxiliary_fit": auxiliary if weight else None,
                         "auxiliary_weight": weight, "score": round((1-weight)*evidence+weight*auxiliary, 4),
                         "source": "model_self_rating_uncalibrated"}
    return sorted(drafts, key=lambda draft: draft.ranking["score"], reverse=True)
