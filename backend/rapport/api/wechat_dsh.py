"""Default DSH history source with explicit, session-bound reads and sync."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from rapport.api.deps import get_state
from rapport.api.imports import _import_view
from rapport.api.wechat import capture_preview, no_cache, owner
from rapport.db import Import
from rapport.desktop import wechat_native
from rapport.desktop.dsh_source import DATABASE_ENABLED_SETTING
from rapport.desktop.wechat import context_digest
from rapport.errors import RapportError
from rapport.schemas.message_v1 import Conversation
from rapport.security import SessionDep

router = APIRouter(prefix="/wechat/dsh", dependencies=[SessionDep, Depends(no_cache)])


class Configure(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class ReadIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account: str = Field(pattern=r"^[0-9a-f]{32}$")
    contact: str = Field(min_length=1, max_length=256)
    contact_id: str | None = Field(default=None, min_length=1, max_length=256)
    relationship_id: str = "default"
    limit: int = Field(default=100, ge=10, le=5000)
    snapshot_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    transcribe_voice: bool = True


class SnapshotIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    relationship_id: str = "default"
    snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")


class ContactsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account: str = Field(pattern=r"^[0-9a-f]{32}$")


class BindIn(SnapshotIn):
    visual: bool = False


def enabled(request):
    state = get_state(request)
    if not state.dsh.enabled:
        raise RapportError("dsh_disabled", "请先开启数据库读取。", status=409)
    return state


@router.get("/config")
def config(request: Request):
    import sys

    state = get_state(request)
    return {"enabled": state.dsh.enabled, "available": sys.platform == "win32",
            "connected": state.dsh.connected_account, "source": "dsh-wechat-bot / wechatauto-replica 1.2.4.3",
            "note": "首次连接从本机已登录微信读取数据库密钥。密钥和同步结果加密保存；连接期间使用本机临时数据库副本，断开后删除副本。仅同步指定好友，不自动回复。"}


@router.patch("/config")
def configure(body: Configure, request: Request):
    state = get_state(request)
    state._set_flag(DATABASE_ENABLED_SETTING, body.enabled)
    if not body.enabled:
        state.dsh_jobs.clear()
        state.dsh.close()
    return config(request)


@router.get("/accounts")
def accounts(request: Request, refresh: bool = Query(False)):
    state = enabled(request)
    return {"accounts": state.dsh.directory.list_accounts(refresh=refresh)}


@router.post("/contacts")
def contacts(body: ContactsIn, request: Request):
    return enabled(request).dsh.directory.contacts(body.account)


@router.post("/read")
def read(body: ReadIn, request: Request):
    state = enabled(request)
    rid = state.resolve_relationship_id(body.relationship_id)
    contact = body.contact.strip()
    if not contact:
        raise RapportError("dsh_contact_required", "请填写完整好友备注。", status=422)

    def work(cancel, report):
        report(phase="connecting", collected=0, target=body.limit, pages=0)
        result = state.dsh.read(body.account, contact, rid, body.limit, snapshot_id=body.snapshot_id, cancel=cancel,
                                transcribe_voice=body.transcribe_voice, report=report, contact_id=body.contact_id)
        if cancel.is_set() or not state.dsh.enabled:
            return None
        if result:
            report(phase="complete", collected=len(result["messages"]), target=body.limit, pages=0)
        return result

    job = state.dsh_jobs.start(owner(request), rid, body.limit, work)
    return {"id": job.id, "status": job.status}


@router.get("/jobs/{job_id}")
def job_status(job_id: str, request: Request):
    jobs = get_state(request).dsh_jobs
    with jobs.lock:
        job = jobs.get(job_id, owner(request))
        return {"id": job.id, "status": job.status, "progress": job.progress, "result": job.result, "error": job.error}


@router.post("/jobs/{job_id}/cancel")
def cancel(job_id: str, request: Request):
    get_state(request).dsh_jobs.get(job_id, owner(request)).cancel.set()
    return {"ok": True}


def snapshot(state, body):
    rid = state.resolve_relationship_id(body.relationship_id)
    data = state.dsh.load(body.snapshot_id, rid)
    if data["revision"] != body.revision:
        raise RapportError("dsh_snapshot_changed", "同步结果已变化，请使用最新结果。", status=409)
    return rid, data


@router.post("/snapshot")
def get_snapshot(body: SnapshotIn, request: Request):
    state = enabled(request)
    with state.dsh.lock:
        _, data = snapshot(state, body)
        return state.dsh.public(data)


@router.post("/import")
def import_snapshot(body: SnapshotIn, request: Request):
    state = enabled(request)
    with state.dsh.lock:
        rid, data = snapshot(state, body)
        conv = Conversation.model_validate(data["conversation"])
        with state.db.session() as session:
            imp = session.get(Import, conv.id)
        if imp and (imp.deleted or imp.relationship_id != rid):
            raise RapportError("dsh_import_deleted", "对应材料已删除，请重新读取。", status=409)
        if imp is None:
            imp = state.save_import(conv, "dsh_database", "微信数据库读取", relationship_id=rid)
        material = state.load_conversation(imp)
        if material is None:
            raise RapportError("dsh_import_expired", "材料已过期，请重新读取。", status=410)
        data["import_id"] = imp.id
        state.dsh.save(data)
        return _import_view(imp, material)


@router.post("/bind")
def bind(body: BindIn, request: Request):
    state = enabled(request)
    with state.dsh.lock:
        rid, data = snapshot(state, body)
        with state.db.session() as session:
            imp = session.get(Import, data.get("import_id")) if data.get("import_id") else None
        if not imp or imp.deleted or imp.relationship_id != rid:
            raise RapportError("dsh_import_required", "请先导入本次读取结果。", status=409)
        material = state.load_conversation(imp)
        if material is None:
            raise RapportError("dsh_import_expired", "材料已过期，请重新读取。", status=410)
    # Release the database lock before entering the desktop worker: its callback
    # reads the DB on that thread and must acquire the same lock there.
    source_binding = {key: data[key] for key in ("snapshot_id", "revision", "account", "contact_id")}
    if body.visual:
        visible = state.desktop.run(wechat_native.capture_contact, data["contact"], state.settings.max_image_pixels)
        visible.view_digest = context_digest(visible.conversation)
        visible.conversation = material
        result = capture_preview(state, owner(request), rid, data["contact"], visible)
        ticket = state.wechat_captures.get(result["token"], owner(request), rid)
    else:
        window_id, process_id, current = state.desktop.run(
            wechat_native.bind_contact, data["contact"],
            lambda pid: state.dsh.current_context(source_binding, data["contact"], rid, pid))
        state.resolve_relationship_id(rid)
        ticket = state.wechat_captures.add(owner(request), window_id, process_id, data["contact"], rid, material)
        ticket.digest = context_digest(current)
        ticket.context_source = "dsh_database"
        ticket.source_binding = source_binding
    ticket.import_id = imp.id
    state._set_flag("desktop.wechat.read", True)
    return {"token": ticket.token, "import_id": imp.id, "relationship_id": rid, "contact": ticket.contact,
            "context_source": ticket.context_source}
