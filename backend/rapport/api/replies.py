"""Reply draft endpoints (v2 M10).

Drafts are suggestions for the user to copy. Nothing here sends anything, and free mode always
runs through the no-fabrication check before a draft is returned.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Request

from rapport.api.deps import err, get_state
from rapport.config import STYLE_REPLY_PRESET
from rapport.db import Import, ReportRow
from rapport.features.episodes import build_episodes, episode_for
from rapport.llm.budget import estimate_cost, estimate_tokens
from rapport.llm.limits import budget as _budget
from rapport.llm.outbound import payload_hash as gate_hash
from rapport.llm.personal_request import compile_reply
from rapport.llm.reply import build_reply_input, run_reply, strict_pack, validate_replies
from rapport.schemas.api import ReplyConfirmIn, ReplyIn
from rapport.schemas.reply import ReplyPack
from rapport.security import SessionDep

router = APIRouter(prefix="/replies", dependencies=[SessionDep])


def _latest_import(st, relationship_id: str) -> Import | None:
    with st.db.session() as s:
        return (
            s.query(Import)
            .filter(Import.deleted.is_(False), Import.status == "confirmed", Import.relationship_id == relationship_id)
            .order_by(Import.updated_at.desc())
            .first()
        )


def _stage_notes(st, relationship_id: str) -> list[str]:
    """Risk hints from the latest report of this relationship, so a draft can avoid a known landmine."""
    with st.db.session() as s:
        row = (
            s.query(ReportRow)
            .filter(ReportRow.deleted.is_(False), ReportRow.relationship_id == relationship_id)
            .order_by(ReportRow.created_at.desc())
            .first()
        )
    if row is None or not isinstance(row.body, dict):
        return []
    notes = [f"雷区：{lm.get('topic', '')}（{lm.get('why', '')}）" for lm in (row.body.get("playbook") or {}).get("landmines", []) if lm.get("topic")]
    boundary = (row.body.get("boundary") or {}).get("value")
    if boundary and boundary != "none":
        notes.append("当前材料显示存在边界信号：只建议尊重边界的表达，不要推进")
    return notes[:4]


def _prepare(st, body: ReplyIn):
    """Everything up to (but not including) the model call. Shared by preview and confirm."""
    mode = body.mode or "free"
    if mode not in ("free", "strict"):
        raise err(400, "bad_mode", "mode 只能是 free 或 strict")
    relationship_id = st.resolve_relationship_id(body.relationship_id)

    imp = None
    if body.import_id:
        with st.db.session() as s:
            imp = s.get(Import, body.import_id)
        if imp is None or imp.deleted:
            raise err(404, "not_found", "导入不存在或已删除")
        if imp.relationship_id != relationship_id:
            raise err(403, "relationship_mismatch", "这份聊天材料不属于当前好友，请重新选择。")
        if imp.status != "confirmed":
            raise err(409, "review_required", "请先校对并确认聊天材料。")
    else:
        imp = _latest_import(st, relationship_id)
    conv = st.load_conversation(imp) if imp else None

    episode = None
    if conv is not None:
        start = imp.range_start if imp and imp.range_start is not None else 0
        end = imp.range_end if imp and imp.range_end is not None else len(conv.messages) - 1
        # Import ranges are zero-based list positions; parser sequence numbers
        # can start at one (DSH) or contain gaps. Include the actual selected tail.
        events = [m for m in conv.messages[start:end + 1] if m.is_event]
        blocks = build_episodes(conv, events, st.settings.session_gap_hours)
        others = [m for m in events if m.sender.value == "other" and m.is_text]
        if others:
            episode = episode_for(conv, blocks, others[-1], mode="reply")

    # Lexical retrieval needs the whole episode as the query: a two-message tail often shares no
    # wording with the diary at all, which would silently turn memory off. Semantic retrieval
    # (embeddings) is a later phase; until then this stays deliberately lexical and bounded.
    query = ""
    if episode is not None:
        query = " ".join(m.text or "" for m in episode.messages)
    elif conv is not None:
        query = " ".join(m.text or "" for m in conv.events()[-20:])

    memory_enabled = st.memory_enabled_by_default() if body.memory_enabled is None else body.memory_enabled
    search = st.diary_search(query or "日常", relationship_id, memory_enabled, as_of=datetime.now().astimezone(), limit=body.memory_limit)
    authorized = [e for e in (st.diary_get(r["id"]) for r in search["entries"]) if e is not None]

    return mode, relationship_id, conv, episode, imp, authorized, memory_enabled, search


def _reply_input(st, relationship_id, conv, episode, imp, authorized, goal, styles, route=None, skill="off"):
    from rapport.analysis_context import build_context
    from rapport.llm.redact import redact_names, redact_text

    context = build_context(st, relationship_id, imp, conv, episode=episode, purpose="reply")
    names = [(conv.participants.me, "A"), (conv.participants.other, "B")]
    assets = []
    try:
        from rapport.archives import get_service

        assets = get_service(st).list_assets_for_relationship(relationship_id, limit=20)
        assets = [{"asset_id": a.get("id", a.get("asset_id")), "kind": a["kind"],
                   "caption": redact_text(redact_names(str(a.get("caption") or ""), names))[:80]}
                  for a in assets if a.get("kind") in {"image", "sticker"} and a.get("status") == "original"]
    except (ImportError, AttributeError):
        pass
    notes = []
    if context["boundary"].get("value", "none") != "none":
        notes = ["当前材料存在边界信号，请尊重边界，不推进、不施压。"]
    inp = build_reply_input(conv, episode, authorized, goal, styles, notes, analysis_context=context, asset_candidates=assets)
    inp.skill_mode = skill
    if route in {STYLE_REPLY_PRESET, "personal", "selected-local"}:
        # Both local routes are shown our earlier replies to this friend; nothing leaves the machine.
        from rapport.learning.style_rag import live_examples

        inp.style_examples = live_examples(conv, episode, names)
    return inp


def _local_route(preset: str) -> bool:
    """Replies drafted on this machine from our own expression: strict fact checks, one bubble per line."""
    return preset in {"personal", STYLE_REPLY_PRESET, "selected-local"}


def _finish(pack: ReplyPack, memory_enabled: bool, search: dict, episode, imp, context=None) -> dict:
    pack.memory_enabled = memory_enabled
    pack.dropped_memories = search["dropped"]
    out = pack.model_dump(mode="json")
    out["context"] = {
        "import_id": imp.id if imp else None,
        "target_msg_id": episode.target.id if episode else None,
        "messages": [
            {"id": m.id, "sender": m.sender.value, "ts": m.ts.isoformat() if m.ts else None, "text": (m.text or "")[:160] if m.is_text else f"[{m.type.value}]"}
            for m in (episode.messages[-6:] if episode else [])
        ],
    }
    out["analysis_context"] = context or {}
    return out


@router.post("/preview")
def preview_drafts(body: ReplyIn, request: Request):
    """What a free-mode draft request would send: redacted lines, the diary text that would
    ride along, destination and cost. Nothing is sent. Strict mode needs no preview (no model)."""
    st = get_state(request)
    mode, relationship_id, conv, episode, imp, authorized, memory_enabled, search = _prepare(st, body)
    if mode == "strict":
        raise err(400, "no_preview_needed", "严格模式不调用模型，直接生成即可")
    if conv is None or episode is None:
        raise err(422, "no_context", "没有可用于起草的对话材料", "请先确认一次导入，或直接指定 import_id。")
    goal = body.goal or "自然地回应对方，并留出继续说下去的空间"
    cfg = st.task_provider_config("reply")
    inp = _reply_input(st, relationship_id, conv, episode, imp, authorized, goal, body.styles, route=cfg["preset"], skill=body.skill)
    from rapport.config import PROVIDER_PRESETS

    preset = PROVIDER_PRESETS[cfg["preset"]]
    compiled = compile_reply(inp, cfg["preset"])
    tokens_in = estimate_tokens(compiled.system) + estimate_tokens(compiled.user)
    tokens_out = _budget(compiled.max_tokens, preset.reasoning)
    cost = estimate_cost(tokens_in, tokens_out, cfg["preset"], float(cfg.get("cost_warn_cny", 0.5)))
    destination = {
        "preset": cfg["preset"],
        "label": preset.label,
        "base_url": cfg["base_url"],
        "model": cfg["model"],
        "endpoint_region": preset.endpoint_region,
        "processing_scope": preset.processing_scope,
        "has_key": cfg["has_key"],
        "ready": cfg.get("ready", cfg["has_key"]),
        "price_date": preset.price.verified_on,
        "connection_mode": st.connection_mode().value,
    }
    diary_refs = [{"id": m.id, "revision": m.revision, "kind": m.kind.value, "chars": len(m.approved_clause or m.text)} for m in inp.memories]
    ticket = st.outbound.issue(
        "reply",
        compiled.preview_lines(),
        destination,
        cost.__dict__,
        diary_refs=diary_refs,
        extra={
            "diary_lines": [f"[{m.kind.value} · 日记 {m.id[:6]} v{m.revision}] {m.approved_clause or m.text}" for m in inp.memories],
            "goal": goal,
            "styles": inp.styles,
            "analysis_context": inp.analysis_context,
            "fields_sent": ["最近一段对话（脱敏后）", "所选日记的原文或已批准短句", "你的目标", "风格要求", "统一分析状态与已确认档案", "同一好友的相关历史", "可选素材ID与说明"]
            + (["你以前回复这位好友的原话（脱敏，只给本机模型当示例）"] if inp.style_examples else []),
            "fields_not_sent": ["真实昵称", "未确认档案", "API Key", "其它对象的日记", "已关闭或未授权的日记", "素材原文件"],
        },
    )
    return ticket.view()


@router.post("")
def create_drafts(body: ReplyConfirmIn, request: Request):
    """Generate drafts. Free mode requires a ticket from `/replies/preview` whose hash matches:
    the diary text leaves the machine only after it was shown."""
    st = get_state(request)
    mode, relationship_id, conv, episode, imp, authorized, memory_enabled, search = _prepare(st, body)

    if mode == "strict":
        pack = strict_pack(authorized, relationship_id, datetime.now().astimezone(), body.styles)
        return _finish(pack, memory_enabled, search, episode, imp)

    if conv is None or episode is None:
        raise err(422, "no_context", "没有可用于起草的对话材料", "请先确认一次导入，或直接指定 import_id。")
    if not body.ticket_id or not body.payload_hash:
        raise err(409, "preview_required", "自由模式需要先预览将发送的内容", "先调用 /replies/preview，确认后携带 ticket_id 与 payload_hash。")

    goal = body.goal or "自然地回应对方，并留出继续说下去的空间"
    cfg = st.task_provider_config("reply")
    inp = _reply_input(st, relationship_id, conv, episode, imp, authorized, goal, body.styles, route=cfg["preset"], skill=body.skill)
    ticket = st.outbound.redeem(body.ticket_id, body.payload_hash)
    from rapport.config import PROVIDER_PRESETS

    compiled = compile_reply(inp, cfg["preset"])
    live_hash = gate_hash(
        compiled.preview_lines(), cfg, PROVIDER_PRESETS[cfg["preset"]].price.verified_on, [{"id": m.id, "revision": m.revision} for m in inp.memories]
    )
    if live_hash != ticket.payload_hash:
        raise err(409, "hash_mismatch", "对话或日记在预览后发生了变化，请重新预览")
    if not ticket.cost.get("price_known", True) or ticket.cost.get("over_threshold"):
        if not body.accept_cost:
            raise err(409, "cost_unknown" if not ticket.cost.get("price_known", True) else "cost_over_threshold", "价格未知或超过预警阈值，需要勾选接受费用")

    provider = st.make_provider(cfg, relationship_id=relationship_id, task="reply")
    data, _usage = run_reply(provider, inp, preset=cfg["preset"])
    pack: ReplyPack = validate_replies(data, inp, st.diary_store(), relationship_id, datetime.now().astimezone(),
                                       strict_facts=_local_route(cfg["preset"]), split_lines=_local_route(cfg["preset"]),
                                       reject_echo=cfg["preset"] in {STYLE_REPLY_PRESET, "selected-local"})
    return _finish(pack, memory_enabled, search, episode, imp, inp.analysis_context)
