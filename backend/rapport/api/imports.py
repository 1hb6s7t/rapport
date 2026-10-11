"""Import endpoints: text/file/OCR -> message-v1, review edits, confirm range."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, File, Form, Request, UploadFile

from rapport.api.deps import err, get_state
from rapport.db import Analysis, Import
from rapport.jobs.status import NEEDS_MATERIAL
from rapport.parsers import ParseError, ParseOptions, parse
from rapport.schemas.api import ConfirmIn, ImportPatch, ImportTextIn
from rapport.schemas.message_v1 import Conversation, MsgType, ParseWarning, Sender
from rapport.security import SessionDep
from rapport.state import new_id
from rapport.state.relationships import RelationshipNotFound

router = APIRouter(prefix="/imports", dependencies=[SessionDep])


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "gb18030", "utf-16"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise err(400, "bad_encoding", "无法识别文件编码", "请另存为 UTF-8 后重试。")


def _do_import(request: Request, text: str, fmt: str, filename: str | None, options: ParseOptions, relationship_id: str | None = None):
    st = get_state(request)
    try:
        relationship_id = st.resolve_relationship_id(relationship_id)
    except RelationshipNotFound as e:
        raise err(404, "relationship_not_found", f"对象 {relationship_id} 不存在", "请先在「历史」页创建该对象，或留空使用默认对象。") from e
    if len(text.encode("utf-8", "ignore")) > st.settings.max_text_bytes:
        raise err(413, "too_large", f"文本超过 {st.settings.max_text_bytes // 1024 // 1024} MiB 限制", "请缩小范围后重试，不会自动截断。")
    conv_id = new_id()
    try:
        conv, parser_id = parse(text, conv_id, fmt, filename, options)
    except ParseError as e:
        status = 422 if e.code in ("ambiguous_date", "too_many_participants", "multi_chat_export") else 400
        raise err(status, e.code, e.msg, e.hint) from e
    if len(conv.messages) > st.settings.max_messages:
        raise err(413, "too_many_messages", f"解析出 {len(conv.messages)} 条消息，超过 {st.settings.max_messages} 条限制", "请只导入需要分析的时间段。")
    if len(conv.messages) > st.settings.large_import_messages:
        conv.warnings.append(
            ParseWarning(
                code="large_import",
                msg=f"共 {len(conv.messages)} 条消息。规则模式与 AI 模式都可以分析全部；AI 模式会按约 120 条一窗逐段发送，超过 {st.settings.ai_warn_messages} 条时预览页会给出调用次数、预计时长与费用，由你决定是否整段发送或缩小区间。",
            )
        )
    imp = st.save_import(conv, parser_id, filename, relationship_id=relationship_id)
    return _import_view(imp, conv)


def _import_view(imp: Import, conv: Conversation, page: int = 0, page_size: int = 5000) -> dict:
    msgs = conv.messages[page * page_size : (page + 1) * page_size]
    events = conv.events()
    return {
        "id": imp.id,
        "relationship_id": getattr(imp, "relationship_id", None) or "default",
        "status": imp.status,
        "source": imp.source,
        "parser_id": imp.parser_id,
        "filename": imp.filename,
        "participants": conv.participants.model_dump(),
        "timezone": conv.timezone,
        "n_messages": len(conv.messages),
        "n_events": len(events),
        "n_unknown": sum(1 for m in conv.messages if m.sender == Sender.unknown),
        "has_reliable_time": conv.has_reliable_time(),
        "warnings": [w.model_dump() for w in conv.warnings],
        "range_start": imp.range_start,
        "range_end": imp.range_end,
        "content_hash": imp.content_hash,
        "expires_at": imp.expires_at.isoformat() if imp.expires_at else None,
        "messages": [m.model_dump(mode="json") for m in msgs],
    }


@router.post("")
def import_text(body: ImportTextIn, request: Request):
    opts = ParseOptions(date_order=body.date_order, me_name=body.me_name, timezone=body.timezone)
    return _do_import(request, body.text, body.format, body.filename, opts, body.relationship_id)


@router.post("/file")
async def import_file(
    request: Request,
    file: UploadFile = File(...),
    format: str = Form("auto"),
    date_order: str | None = Form(None),
    me_name: str | None = Form(None),
    timezone: str = Form("Asia/Shanghai"),
    relationship_id: str | None = Form(None),
):
    st = get_state(request)
    # Read in chunks so a 300 MB upload does not need a single 300 MB buffer per request handler.
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(4 * 1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > st.settings.max_text_bytes:
            raise err(413, "too_large", f"文件超过 {st.settings.max_text_bytes // 1024 // 1024} MiB 限制")
        chunks.append(chunk)
    text = _decode(b"".join(chunks))
    opts = ParseOptions(date_order=date_order, me_name=me_name, timezone=timezone)
    return _do_import(request, text, format, file.filename, opts, relationship_id)


@router.get("/formats")
def formats():
    from rapport.parsers import list_formats

    return list_formats()


@router.get("/{import_id}")
def get_import(import_id: str, request: Request):
    st = get_state(request)
    with st.db.session() as s:
        imp = s.get(Import, import_id)
    if not imp or imp.deleted:
        raise err(404, "not_found", "导入不存在或已删除")
    conv = st.load_conversation(imp)
    if conv is None:
        raise err(410, "expired", "原始材料已按保留策略清理")
    return _import_view(imp, conv)


@router.patch("/{import_id}")
def patch_import(import_id: str, body: ImportPatch, request: Request):
    st = get_state(request)
    with st.db.session() as s:
        imp = s.get(Import, import_id)
        if not imp or imp.deleted:
            raise err(404, "not_found", "导入不存在")
        active = s.query(Analysis).filter(Analysis.import_id == import_id, Analysis.tombstone.is_(False), Analysis.status.in_(sorted(NEEDS_MATERIAL))).count()
        if active:
            raise err(409, "analysis_active", "这份材料有分析正在排队或进行中，不能修改", "请先取消该分析，或等它完成后再修改。")
        conv = st.load_conversation(imp)
        if conv is None:
            raise err(410, "expired", "原始材料已清理")
        by_id = {m.id: m for m in conv.messages}
        if body.swap_roles:
            for m in conv.messages:
                if m.sender == Sender.me:
                    m.sender = Sender.other
                elif m.sender == Sender.other:
                    m.sender = Sender.me
            conv.participants.me, conv.participants.other = conv.participants.other, conv.participants.me
        for p in body.messages:
            m = by_id.get(p.id)
            if not m:
                continue
            if p.sender is not None:
                try:
                    m.sender = Sender(p.sender)
                except ValueError as e:
                    raise err(400, "bad_sender", f"非法发送者 {p.sender}") from e
            if p.type is not None:
                try:
                    m.type = MsgType(p.type)
                except ValueError as e:
                    raise err(400, "bad_type", f"非法类型 {p.type}") from e
            if p.text is not None:
                m.text = p.text if p.text.strip() else None
                if m.text and m.type != MsgType.text:
                    m.type = MsgType.text
        if body.participants:
            conv.participants.me = body.participants.get("me", conv.participants.me)[:30]
            conv.participants.other = body.participants.get("other", conv.participants.other)[:30]
        if body.timezone:
            conv.timezone = body.timezone
        st.store_conversation(imp, conv)
        imp.participants = conv.participants.model_dump()
        # Sender / text edits change the canonical events; the ledger for this import is rebuilt
        # (unconfirmed until the user confirms again).
        st.rebuild_events(imp, conv)
        imp.content_hash = None
        imp.confirmed_at = None
        imp.status = "awaiting_confirmation"
        return _import_view(imp, conv)


@router.post("/{import_id}/confirm")
def confirm_import(import_id: str, body: ConfirmIn, request: Request):
    st = get_state(request)
    with st.db.session() as s:
        imp = s.get(Import, import_id)
        if not imp or imp.deleted:
            raise err(404, "not_found", "导入不存在")
        conv = st.load_conversation(imp)
        if conv is None:
            raise err(410, "expired", "原始材料已清理")
        n = len(conv.messages)
        start = max(0, body.range_start)
        end = n - 1 if body.range_end is None else min(n - 1, body.range_end)
        if start > end:
            raise err(400, "bad_range", "区间无效")
        sel = conv.messages[start : end + 1]
        unknown = [m.id for m in sel if m.sender == Sender.unknown]
        if unknown:
            raise err(422, "unknown_senders", f"区间内有 {len(unknown)} 条消息发送者未确认", "请在校正页为这些消息指定 我/对方，或缩小区间。")
        events = [m for m in sel if m.is_event]
        if len(events) > st.settings.ai_warn_messages:
            note = f"区间内 {len(events)} 条互动消息。AI 模式将分约 {max(1, -(-len(events) // 110))} 个窗口逐段发送，预览页会显示调用次数与费用；也可以缩小区间。"
        else:
            note = None
        imp.range_start, imp.range_end = start, end
        imp.content_hash = st.content_hash(conv, start, end)
        imp.confirmed_at = datetime.now()
        imp.status = "confirmed"
        # Confirming an import is what turns its events into evidence for N_eff (FR-002).
        st.confirm_events(imp.id)
        out = _import_view(imp, conv)
        out["messages"] = []
        out["selected"] = {
            "n_messages": len(sel),
            "n_events": len(events),
            "n_me": sum(1 for m in events if m.sender == Sender.me),
            "n_other": sum(1 for m in events if m.sender == Sender.other),
            "first_ts": sel[0].ts.isoformat() if sel[0].ts else None,
            "last_ts": sel[-1].ts.isoformat() if sel[-1].ts else None,
            "untimed_ratio": round(sum(1 for m in events if not m.has_reliable_ts) / max(1, len(events)), 3),
            "truncated": len(sel) < n,
            "ai_limit_note": note,
        }
        return out


@router.delete("/{import_id}")
def delete_import(import_id: str, request: Request):
    st = get_state(request)
    with st.db.session() as s:
        imp = s.get(Import, import_id)
        if not imp:
            raise err(404, "not_found", "导入不存在")
        st.blobs.delete(imp.blob_id)
        imp.deleted = True
        imp.status = "deleted"
    return {"ok": True}
