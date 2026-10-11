"""Opt-in, session-bound visible WeChat read/review/fill endpoints."""

from __future__ import annotations

import base64
import hashlib
from datetime import datetime

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from rapport.api.deps import get_state
from rapport.api.desktop import _log
from rapport.api.imports import _import_view
from rapport.db import Import
from rapport.desktop import wechat_native
from rapport.errors import RapportError
from rapport.security import SessionDep
from rapport.security.auth import SESSION_COOKIE

ENABLED = "desktop.wechat.read"
ALLOW_FILL = "desktop.wechat.fill"


def no_cache(response: Response):
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/wechat", dependencies=[SessionDep, Depends(no_cache)])


class ConfigIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None
    allow_fill: bool | None = None


class CaptureIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contact: str = Field(min_length=1, max_length=80)
    relationship_id: str = "default"


class TicketIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=16, max_length=120)
    relationship_id: str = "default"


class HistoryIn(CaptureIn):
    target_count: int = Field(default=1000, ge=10, le=5000)


class FillIn(TicketIn):
    import_id: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=4000)


def owner(request: Request) -> str:
    return hashlib.sha256((request.cookies.get(SESSION_COOKIE) or "").encode()).hexdigest()


def enabled_state(request: Request):
    state = get_state(request)
    if not state._flag(ENABLED, False):
        raise RapportError("wechat_disabled", "请先开启微信可见聊天读取。", status=409)
    return state


@router.get("/config")
def config(request: Request):
    from rapport.ocr.engine import info

    state = get_state(request)
    capabilities = wechat_native.capabilities()
    return {**capabilities, "enabled": state._flag(ENABLED, False), "allow_fill": state._flag(ALLOW_FILL, False),
            "available": all(capabilities.values()), "capture_ttl_s": state.wechat_captures.ttl, "ocr_engine": info(),
            "note": "输入好友完整备注后，自动找到微信、搜索好友并定位聊天区域。识别在本机完成；填入或发送前会重新核对好友。发送具体内容仍需单独预览确认。"}


@router.patch("/config")
def configure(body: ConfigIn, request: Request):
    state = get_state(request)
    if body.enabled is not None:
        state._set_flag(ENABLED, body.enabled)
        if not body.enabled:
            state._set_flag(ALLOW_FILL, False)
            state.wechat_captures.clear()
            state.wechat_history.clear()
            state.sticker_search.clear()
    if body.allow_fill is not None:
        if body.allow_fill and not state._flag(ENABLED, False):
            raise RapportError("wechat_disabled", "先开启读取，再允许确认后填入。", status=409)
        state._set_flag(ALLOW_FILL, body.allow_fill)
    return config(request)


@router.post("/capture")
def capture(body: CaptureIn, request: Request):
    state = enabled_state(request)
    rid = state.resolve_relationship_id(body.relationship_id)
    if not body.contact.strip():
        raise RapportError("wechat_invalid_selection", "请填写好友完整名称或备注。", status=422)
    captured = state.desktop.run(wechat_native.capture_contact, body.contact.strip(), state.settings.max_image_pixels)
    enabled_state(request)
    return capture_preview(state, owner(request), rid, body.contact.strip(), captured)


def capture_preview(state, browser_owner, rid, contact, captured):
    conversation = captured.conversation
    if len(conversation.messages) > state.settings.max_messages:
        raise RapportError("too_many_messages", "识别内容超过消息上限。", status=413)
    state.resolve_relationship_id(rid)
    ticket = state.wechat_captures.add(browser_owner, captured.window_id, captured.process_id, contact, rid, conversation)
    if captured.view_digest:
        ticket.digest = captured.view_digest
    return {"token": ticket.token, "contact": ticket.contact, "relationship_id": rid, "expires_in_s": state.wechat_captures.ttl,
            "image": "data:image/png;base64," + base64.b64encode(captured.screenshot).decode(),
            "messages": [message.model_dump(mode="json") for message in conversation.messages],
            "warnings": [warning.model_dump() for warning in conversation.warnings],
            "history": conversation.meta.get("wechat_history")}


@router.post("/history")
def start_history(body: HistoryIn, request: Request):
    state = enabled_state(request)
    rid = state.resolve_relationship_id(body.relationship_id)
    browser_owner = owner(request)
    contact = body.contact.strip()
    if not contact:
        raise RapportError("wechat_contact_required", "请填写好友完整备注。", status=422)

    def work(cancel, report):
        captured = state.desktop.run(wechat_native.capture_history, contact, state.settings.max_image_pixels, body.target_count, cancel, report)
        if captured is None or not state._flag(ENABLED, False):
            return None
        with state.wechat_history.lock:
            if not any(job.cancel is cancel for job in state.wechat_history.items.values()):
                return None
        return capture_preview(state, browser_owner, rid, contact, captured)

    job = state.wechat_history.start(browser_owner, rid, body.target_count, work)
    return {"id": job.id, "status": job.status}


@router.get("/history/{job_id}")
def history_status(job_id: str, request: Request):
    state = get_state(request)
    with state.wechat_history.lock:
        job = state.wechat_history.get(job_id, owner(request))
        return {"id": job.id, "status": job.status, "progress": job.progress,
                "result": job.result, "error": job.error}


@router.post("/history/{job_id}/cancel")
def cancel_history(job_id: str, request: Request):
    job = get_state(request).wechat_history.get(job_id, owner(request))
    job.cancel.set()
    return {"ok": True}


@router.post("/import")
def import_capture(body: TicketIn, request: Request):
    state = enabled_state(request)
    rid = state.resolve_relationship_id(body.relationship_id)
    with state.wechat_captures.lock:
        ticket = state.wechat_captures.get(body.token, owner(request), rid)
        if ticket.import_id:
            with state.db.session() as session:
                imp = session.get(Import, ticket.import_id)
            if not imp or imp.deleted:
                raise RapportError("wechat_import_deleted", "这次读取对应的材料已删除，请重新读取。", status=409)
        else:
            imp = state.save_import(ticket.conversation, "wechat_visual", "微信可见聊天", relationship_id=rid)
            ticket.import_id = imp.id
        conversation = state.load_conversation(imp)
        if conversation is None:
            raise RapportError("wechat_import_expired", "材料已清理，请重新读取。", status=410)
        return _import_view(imp, conversation)


@router.post("/target")
def target(body: TicketIn, request: Request):
    state = enabled_state(request)
    rid = state.resolve_relationship_id(body.relationship_id)
    ticket = state.wechat_captures.get(body.token, owner(request), rid)
    return {"contact": ticket.contact, "import_id": ticket.import_id, "relationship_id": rid,
            "filled": ticket.filled, "allow_fill": state._flag(ALLOW_FILL, False)}


@router.post("/fill")
def fill(body: FillIn, request: Request):
    state = enabled_state(request)
    if not state._flag(ALLOW_FILL, False):
        raise RapportError("wechat_fill_disabled", "请在微信读取页打开「允许确认后填入草稿」。", status=409)
    rid = state.resolve_relationship_id(body.relationship_id)
    ticket = state.wechat_captures.get(body.token, owner(request), rid)
    if ticket.import_id != body.import_id or ticket.filled:
        raise RapportError("wechat_target_changed", "这份草稿的会话绑定已变化或已经使用，请重新读取。", status=409)
    with state.db.session() as session:
        imp = session.get(Import, body.import_id)
    if not imp or imp.deleted or imp.relationship_id != rid or imp.status != "confirmed":
        raise RapportError("wechat_review_required", "请先校对并确认这次读取的聊天材料。", status=409)
    def before_write():
        # OCR and focus checks can take seconds; honor a toggle changed meanwhile.
        if not state._flag(ALLOW_FILL, False):
            raise RapportError("wechat_fill_disabled", "填入权限已关闭，本次未填入。", status=409)
        state.wechat_captures.consume_fill(ticket)

    options = {"state": state} if ticket.context_source == "dsh_database" else {}
    result = state.desktop.run(wechat_native.fill, ticket, body.text, state.settings.max_image_pixels, before_write, **options)
    _log(state, {"at": datetime.now().isoformat(timespec="seconds"), "ok": result["ok"], "reason": "wechat_filled",
                 "window": "WeChat", "control": "verified_input", "sent": False, "source": "wechat", "chars": len(body.text)})
    return result
