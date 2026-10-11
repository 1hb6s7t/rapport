"""Analysis endpoints: create, preview (BYOK), confirm, status, cancel."""

from __future__ import annotations

import hashlib

from fastapi import APIRouter, Request

from rapport.api.deps import err, get_state
from rapport.config import PROMPTS_VERSION, PROVIDER_PRESETS
from rapport.db import Analysis, Import
from rapport.jobs.status import RETRYABLE, AnalysisStatus, can_transition
from rapport.llm.budget import estimate_cost
from rapport.llm.extract import apply_line_edits, build_payload
from rapport.llm.intent import estimate_intent_tokens
from rapport.llm.outbound import payload_hash as _gate_hash
from rapport.llm.synthesize import MAX_SIGNALS
from rapport.schemas.api import AnalysisConfirmIn, AnalysisIn
from rapport.security import SessionDep

router = APIRouter(prefix="/analyses", dependencies=[SessionDep])


def _payload_hash(lines: list[list[str]], cfg: dict, preset) -> str:
    """Hash of exactly what will be sent, so a confirmation cannot drift from the preview."""
    return _gate_hash(lines, cfg, preset.price.verified_on)


def _range_of(imp, conv) -> tuple[int, int]:
    start = imp.range_start or 0
    end = imp.range_end if imp.range_end is not None else len(conv.messages) - 1
    return start, end


def _view(a: Analysis) -> dict:
    return {
        "id": a.id,
        "import_id": a.import_id,
        "mode": a.mode,
        "status": a.status,
        "stage_done": a.stage_done,
        "stage_total": a.stage_total,
        "message": a.message,
        "error": {"code": a.error_code, "msg": a.error_msg} if a.error_code else None,
        "report_id": a.report_id,
        "provider": a.provider,
        "model": a.model,
        "est_cost": a.est_cost,
        "created_at": a.created_at.isoformat(),
        "updated_at": a.updated_at.isoformat(),
        "needs_confirmation": a.mode == "byok" and a.status == "created",
        "relationship_id": getattr(a, "relationship_id", None) or "default",
    }


@router.post("")
def create_analysis(body: AnalysisIn, request: Request):
    st = get_state(request)
    if body.mode not in ("rules", "byok"):
        raise err(400, "bad_mode", "mode 必须是 rules 或 byok")
    with st.db.session() as s:
        imp = s.get(Import, body.import_id)
        if not imp or imp.deleted:
            raise err(404, "not_found", "导入不存在")
        if imp.status != "confirmed" or not imp.content_hash:
            raise err(409, "not_confirmed", "请先在校正页确认角色与区间")
        relationship_id = imp.relationship_id or "default"
        profile = st.profile_snapshot(relationship_id, body.profile if "profile" in body.model_fields_set else None)
        cfg = st.provider_config() if body.mode == "byok" else None
        provider = cfg["preset"] if cfg else None
        model = cfg["model"] if cfg else None
        key = "|".join([
            relationship_id, imp.id, imp.content_hash, profile.cache_hash(), st.prior_policy_version(),
            body.mode, provider or "", model or "", PROMPTS_VERSION if body.mode == "byok" else "",
        ])
        idem = hashlib.sha256(key.encode()).hexdigest()[:32]
        existing = s.query(Analysis).filter(Analysis.idempotency_key == idem, Analysis.tombstone.is_(False), Analysis.status.notin_(["failed", "cancelled", "interrupted"])).first()
        if existing:
            return _view(existing) | {"reused": True}
        from rapport.state import new_id

        a = Analysis(
            id=new_id(),  # the idempotency key is a separate column; a failed run must not block a fresh id
            import_id=imp.id,
            relationship_id=relationship_id,
            mode=body.mode,
            status="queued" if body.mode == "rules" else "created",
            profile=profile.model_dump(mode="json"),
            idempotency_key=idem,
            content_hash=imp.content_hash,
            provider=provider,
            model=model,
            message="排队中" if body.mode == "rules" else "等待出网确认",
        )
        s.add(a)
        s.flush()
        out = _view(a)
    if st.runner:
        st.runner.wake()
    return out


def _window_hint(events, cap: int) -> str:
    """Tell the user which trailing window would fit under the AI cap, instead of "make it smaller"."""
    tail = events[-cap:]
    first = next((m.ts for m in tail if m.ts), None)
    last = next((m.ts for m in reversed(tail) if m.ts), None)
    if first and last:
        days = max(1, (last - first).days + 1)
        return f"请回到校正页缩小区间。最近 {cap} 条互动消息约覆盖 {first:%Y-%m-%d} 至 {last:%Y-%m-%d}（{days} 天），可作为参考区间；规则模式不受此限制。"
    return "请回到校正页缩小区间；规则模式不受此限制。"


@router.get("/{analysis_id}/preview")
def preview(analysis_id: str, request: Request):
    st = get_state(request)
    with st.db.session() as s:
        a = s.get(Analysis, analysis_id)
        if not a or a.tombstone:
            raise err(404, "not_found", "分析不存在")
        if a.mode != "byok":
            raise err(400, "not_byok", "只有 AI 模式需要预览")
        imp = s.get(Import, a.import_id)
    conv = st.load_conversation(imp) if imp else None
    if conv is None:
        raise err(410, "expired", "原始材料已清理")
    start, end = _range_of(imp, conv)
    events = [m for m in conv.messages[start : end + 1] if m.is_event]
    if len(events) > st.settings.max_ai_messages:
        raise err(
            422,
            "too_many_for_ai",
            f"区间内 {len(events)} 条互动消息超过单次 AI 上限 {st.settings.max_ai_messages}",
            _window_hint(events, st.settings.max_ai_messages),
        )
    cfg = st.provider_config()
    preset = PROVIDER_PRESETS[cfg["preset"]]
    payload = build_payload(conv, events, reasoning=preset.reasoning)
    # The intent stage sends one more request per selected episode: count it, or the estimate lies.
    intent_in, intent_out, intent_calls = estimate_intent_tokens(
        conv, events, st.settings.session_gap_hours, reasoning=preset.reasoning, samples=st.intent_samples()
    )
    cost = estimate_cost(
        payload.est_tokens_in + intent_in,
        payload.est_tokens_out + intent_out,
        cfg["preset"],
        float(cfg.get("cost_warn_cny", 0.5)),
    )
    payload_hash = _payload_hash(payload.lines_by_window, cfg, preset)
    with st.db.session() as s:
        a = s.get(Analysis, analysis_id)
        a.payload_hash = payload_hash
        a.est_cost = cost.cny
    return {
        "analysis_id": analysis_id,
        "payload_hash": payload_hash,
        "destination": {
            "preset": cfg["preset"],
            "label": preset.label,
            "base_url": cfg["base_url"],
            "model": cfg["model"],
            "endpoint_region": preset.endpoint_region,
            "processing_scope": preset.processing_scope,
            "has_key": cfg["has_key"],
            "ready": cfg["ready"],
        },
        "windows": payload.lines_by_window,
        "n_windows": len(payload.windows),
        "n_intent_calls": intent_calls,
        "n_calls": len(payload.windows) + 1 + intent_calls,
        "n_messages": len(events),
        # v0.7: long material is allowed; the preview says what it costs in calls and wall time so the
        # decision is the user's, not a hard-coded 300. ~20 s per extraction call on a flash model.
        "large_run": len(events) > st.settings.ai_warn_messages,
        "est_minutes": round((len(payload.windows) * 20 + intent_calls * 12 + 30) / 60, 1),
        "large_run_note": (
            f"区间内 {len(events)} 条互动消息，将分 {len(payload.windows)} 个窗口逐段发送并逐窗保存进度（中断可续跑）。"
            f"综合阶段只读取各窗口的信号摘要（上限 {MAX_SIGNALS} 条），不会把全文塞进一次请求。"
            if len(events) > st.settings.ai_warn_messages
            else ""
        ),
        "redaction": {"counts": payload.stats.counts, "names_replaced": payload.stats.names_replaced},
        "cost": cost.__dict__,
        "fields_sent": ["消息 id", "相对时间（月-日 时:分）", "A/B 角色", "脱敏后的正文", "媒体类型占位", "行为统计数字", "你的目标与补充说明"],
        "fields_not_sent": ["真实昵称", "MBTI/星座/属相", "API Key", "文件名", "未选区间的消息"],
        "prompts_version": PROMPTS_VERSION,
    }


@router.post("/{analysis_id}/confirm")
def confirm(analysis_id: str, body: AnalysisConfirmIn, request: Request):
    st = get_state(request)
    with st.db.session() as s:
        a = s.get(Analysis, analysis_id)
        if not a or a.tombstone:
            raise err(404, "not_found", "分析不存在")
        if a.mode != "byok":
            raise err(400, "not_byok", "规则模式不需要确认")
        if a.status == AnalysisStatus.queued or not can_transition(a.status, AnalysisStatus.queued):
            raise err(409, "bad_state", f"当前状态 {a.status} 不能确认")
        if not a.payload_hash or body.payload_hash != a.payload_hash:
            raise err(409, "hash_mismatch", "预览内容已变化，请重新预览后确认")
        cfg = st.provider_config()
        if not cfg["ready"]:
            raise err(400, "no_key", "尚未配置 API Key")
        cost = estimate_cost(0, 0, cfg["preset"], 0)
        if (a.est_cost is None or (a.est_cost > float(cfg.get("cost_warn_cny", 0.5)))) and not body.accept_cost and not cost.price_known:
            raise err(409, "cost_unknown", "价格未知或超过预警阈值，需要勾选接受费用")
        if a.est_cost is not None and a.est_cost > float(cfg.get("cost_warn_cny", 0.5)) and not body.accept_cost:
            raise err(409, "cost_over_threshold", f"预估费用 {a.est_cost} 元超过预警阈值，需要勾选接受费用")
        # v0.7: long material is allowed, but not by accident. Above `ai_warn_messages` the user has
        # to tick the same box even when the price is known and small: it is minutes and hundreds
        # of calls, not just money.
        imp_row = s.get(Import, a.import_id)
        if imp_row is not None:
            conv_ = st.load_conversation(imp_row)
            if conv_ is not None:
                s0, e0 = _range_of(imp_row, conv_)
                n_ev = sum(1 for m in conv_.messages[s0 : e0 + 1] if m.is_event)
                if n_ev > st.settings.ai_warn_messages and not body.accept_cost:
                    raise err(409, "large_run_needs_accept", f"区间内 {n_ev} 条互动消息，属于长材料分析（数百次调用、数分钟到数十分钟）；请在预览页勾选确认后再发送。")
        # The confirmation must describe what is actually sent, edits included.
        edited = {int(k): v for k, v in body.edited_lines.items()}
        imp_ = s.get(Import, a.import_id)
        conv_ = st.load_conversation(imp_) if imp_ else None
        if conv_ is not None:
            start_, end_ = _range_of(imp_, conv_)
            events_ = [m for m in conv_.messages[start_ : end_ + 1] if m.is_event]
            final_lines = apply_line_edits(build_payload(conv_, events_).lines_by_window, edited)
            a.confirmed_payload_hash = _payload_hash(final_lines, cfg, PROVIDER_PRESETS[cfg["preset"]])
        else:
            a.confirmed_payload_hash = body.payload_hash
        new_edits = {str(k): v for k, v in body.edited_lines.items()}
        if new_edits != (a.edited_lines or {}):
            a.checkpoints = {}  # the content changed: cached window results would describe the old lines
        a.edited_lines = new_edits
        a.status = "queued"
        a.message = "已确认，排队中"
        a.cancel_requested = False
        a.error_code = None
        a.error_msg = None
        out = _view(a)
    if st.runner:
        st.runner.wake()
    return out


@router.get("/{analysis_id}")
def get_analysis(analysis_id: str, request: Request):
    st = get_state(request)
    with st.db.session() as s:
        a = s.get(Analysis, analysis_id)
    if not a or a.tombstone:
        raise err(404, "not_found", "分析不存在")
    return _view(a)


@router.post("/{analysis_id}/cancel")
def cancel(analysis_id: str, request: Request):
    st = get_state(request)
    with st.db.session() as s:
        a = s.get(Analysis, analysis_id)
        if not a:
            raise err(404, "not_found", "分析不存在")
        if not can_transition(a.status, AnalysisStatus.cancelled):
            return _view(a)
        a.cancel_requested = True
        if a.status in (AnalysisStatus.created, AnalysisStatus.queued, AnalysisStatus.interrupted):
            a.status = AnalysisStatus.cancelled.value  # the worker settles the rest
        a.message = "取消请求已记录；在途模型调用可能已计费"
        return _view(a)


@router.post("/{analysis_id}/retry")
def retry(analysis_id: str, request: Request):
    st = get_state(request)
    with st.db.session() as s:
        a = s.get(Analysis, analysis_id)
        if not a or a.tombstone:
            raise err(404, "not_found", "分析不存在")
        if a.status not in RETRYABLE:
            raise err(409, "bad_state", "只有中断或失败的任务可以重试")
        if a.mode == "byok" and not a.confirmed_payload_hash:
            raise err(409, "not_confirmed", "请先预览并确认")
        a.status = "queued"
        a.cancel_requested = False
        a.error_code = None
        a.error_msg = None
        a.message = "重试排队中（已完成的分段将复用）"
        out = _view(a)
    if st.runner:
        st.runner.wake()
    return out


@router.get("/{analysis_id}/calls")
def calls(analysis_id: str, request: Request):
    """The model calls this analysis actually made: purpose, tokens, latency, status. No content."""
    st = get_state(request)
    with st.db.session() as s:
        a = s.get(Analysis, analysis_id)
    if not a or a.tombstone:
        raise err(404, "not_found", "分析不存在")
    rows = st.llm_calls_for(analysis_id)
    known = [r["cost_cny"] for r in rows if r["cost_cny"] is not None]
    return {
        "analysis_id": analysis_id,
        "calls": rows,
        "summary": {
            "calls": len(rows),
            "prompt_tokens": sum(r["prompt_tokens"] for r in rows),
            "completion_tokens": sum(r["completion_tokens"] for r in rows),
            "truncated": sum(1 for r in rows if r["status"] == "truncated"),
            "failed": sum(1 for r in rows if r["status"] in ("error", "bad_json")),
            "cost_cny": round(sum(known), 4) if known else None,
        },
    }
