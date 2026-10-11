"""Paired-browser preview/confirm API for ordered desktop messages."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from rapport.api.deps import get_state
from rapport.api.wechat import ALLOW_FILL, enabled_state, no_cache, owner
from rapport.db import Import
from rapport.desktop.dispatch import (
    DispatchStore,
    WindowsDispatchAdapter,
    public_receipt,
    require_supported_items,
)
from rapport.desktop.media import MediaCollectionAdapter, preview_assets, revalidate_assets
from rapport.desktop.stickers import TTL as STICKER_TTL
from rapport.desktop.stickers import StickerSearchStore, inspect_choice, search_stickers, send_choice
from rapport.errors import RapportError
from rapport.schemas.reply import ReplyItem
from rapport.security import SessionDep

router = APIRouter(prefix="/dispatch", dependencies=[SessionDep, Depends(no_cache)])


class PreviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    capture_token: str = Field(min_length=16, max_length=120)
    relationship_id: str = "default"
    import_id: str = Field(min_length=1, max_length=100)
    items: list[ReplyItem] = Field(min_length=1, max_length=12)


class TokenIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=16, max_length=120)


class ConfirmIn(TokenIn):
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class MediaPreviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    capture_token: str = Field(min_length=16, max_length=120)
    relationship_id: str = "default"
    archive_id: str = Field(min_length=1, max_length=100)
    message_ref: str = Field(min_length=1, max_length=150)


class MediaCollectIn(MediaPreviewIn):
    selection_token: str = Field(min_length=16, max_length=120)


class StickerSearchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    capture_token: str = Field(min_length=16, max_length=120)
    relationship_id: str = "default"
    import_id: str = Field(min_length=1, max_length=100)
    query: str = Field(min_length=1, max_length=24)


class StickerPreviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    capture_token: str = Field(min_length=16, max_length=120)
    relationship_id: str = "default"
    import_id: str = Field(min_length=1, max_length=100)
    selection_token: str = Field(min_length=16, max_length=120)


def store(state):
    # AppState normally constructs this. Compatibility for old test fixtures.
    if not hasattr(state, "dispatch"):
        state.dispatch = DispatchStore(state)
    return state.dispatch


def collection(state):
    if not hasattr(state, "media_collection"):
        state.media_collection = MediaCollectionAdapter()
    return state.media_collection


def stickers(state):
    if not hasattr(state, "sticker_search"):
        state.sticker_search = StickerSearchStore()
    return state.sticker_search


def media_target(body, request):
    from rapport.archives import get_service

    state = enabled_state(request)
    rid = state.resolve_relationship_id(body.relationship_id)
    ticket = state.wechat_captures.get(body.capture_token, owner(request), rid)
    require_supported_items(ticket, [{"kind": "image"}])
    service = get_service(state)
    messages = service.messages(body.archive_id, rid)
    matched = [message for message in messages if message["message_ref"] == body.message_ref]
    if len(matched) != 1 or matched[0]["type"] not in {"image", "video", "sticker"}:
        raise RapportError("wechat_media_archive_mismatch", "请选择属于当前好友归档的图片、表情或视频消息。", status=422)
    return state, ticket


def check_material(state, capture, rid, import_id):
    if capture.import_id != import_id or capture.filled:
        raise RapportError("dispatch_target_used", "这次聊天绑定已使用或材料不匹配，请重新读取。", status=409)
    with state.db.session() as session:
        imp = session.get(Import, import_id)
    if not imp or imp.deleted or imp.status != "confirmed" or imp.relationship_id != rid:
        raise RapportError("dispatch_material_invalid", "请先校对并确认属于当前好友的聊天材料。", status=409)


@router.post("/preview")
def preview(body: PreviewIn, request: Request):
    state = enabled_state(request)
    browser = owner(request)
    rid = state.resolve_relationship_id(body.relationship_id)
    capture = state.wechat_captures.get(body.capture_token, browser, rid)
    check_material(state, capture, rid, body.import_id)
    require_supported_items(capture, [item.model_dump() for item in body.items])
    items = preview_assets(state, rid, body.items)
    token, receipt = store(state).preview(browser, capture, items)
    return public_receipt(token, receipt)


@router.post("/status")
def status(body: TokenIn, request: Request):
    receipt = store(get_state(request)).get(body.token, owner(request))
    return public_receipt(body.token, receipt)


@router.post("/confirm")
def confirm(body: ConfirmIn, request: Request):
    state = get_state(request)
    browser = owner(request)

    def operation(receipt, update):
        enabled_state(request)
        if not state._flag(ALLOW_FILL, False):
            raise RapportError("wechat_fill_disabled", "请先在微信读取页允许确认后的输入操作。", status=409)
        rid = state.resolve_relationship_id(receipt["relationship_id"])
        capture = state.wechat_captures.get(receipt["capture_token"], browser, rid)
        check_material(state, capture, rid, receipt["import_id"])
        if (capture.digest != receipt["context_digest"] or capture.contact != receipt["contact"]
                or capture.window_id != receipt["window_id"] or capture.process_id != receipt["process_id"]
                or capture.context_source != receipt.get("context_source", "visual")
                or capture.source_binding != receipt.get("source_binding")):
            raise RapportError("dispatch_context_changed", "聊天绑定发生变化，请重新预览。", status=409)
        revalidate_assets(state, rid, receipt["items"])
        adapter = WindowsDispatchAdapter(state)
        with adapter.session(capture, receipt["items"], state.settings.max_image_pixels) as native_session:
            state.wechat_captures.consume_fill(capture)
            for index, item in enumerate(receipt["items"]):
                # Changes to enablement/deletion also stop a group between items.
                enabled_state(request)
                state.resolve_relationship_id(rid)
                if not state._flag(ALLOW_FILL, False):
                    raise RapportError("wechat_fill_disabled", "输入操作已关闭，剩余消息未发送。", status=409)
                revalidate_assets(state, rid, [item])
                update(index, "uncertain")  # durable before any fill/paste/click
                if native_session.send(item) is not True:
                    raise RapportError("dispatch_send_uncertain", "无法确认发送结果，剩余消息未发送。", status=409)
                update(index, "sent")

    receipt = state.desktop.run(store(state).execute, body.token, browser, body.digest, operation)
    return public_receipt(body.token, receipt)


@router.post("/media/preview")
def media_preview(body: MediaPreviewIn, request: Request):
    state, ticket = media_target(body, request)
    selections = collection(state).preview(ticket, body.archive_id, body.message_ref, state.settings.max_image_pixels)
    return {"contact": ticket.contact, "message_ref": body.message_ref, "selections": selections,
            "note": "请选择与该归档消息对应的媒体。以下只是屏幕缩略图；只有微信另存为取得的文件才会保存为原件。"}


@router.post("/media/collect")
def media_collect(body: MediaCollectIn, request: Request):
    state, ticket = media_target(body, request)
    return collection(state).collect(state, ticket, body.selection_token, body.archive_id, body.message_ref)


@router.post("/stickers/search")
def sticker_search(body: StickerSearchIn, request: Request):
    state = enabled_state(request)
    browser = owner(request)
    rid = state.resolve_relationship_id(body.relationship_id)
    capture = state.wechat_captures.get(body.capture_token, browser, rid)
    check_material(state, capture, rid, body.import_id)
    require_supported_items(capture, [{"kind": "wechat_sticker"}])
    query = body.query.strip()
    found = search_stickers(capture, query, state.settings.max_image_pixels)
    candidates = stickers(state).add(browser, capture, query, found)
    return {"contact": capture.contact, "query": query, "candidates": candidates,
            "expires_in_s": STICKER_TTL,
            "note": "搜索结果来自微信。优先使用表情的无障碍名称；名称不清楚时只识别你选中的缩略图。"}


@router.post("/stickers/preview")
def sticker_preview(body: StickerPreviewIn, request: Request):
    state = enabled_state(request)
    browser = owner(request)
    rid = state.resolve_relationship_id(body.relationship_id)
    capture = state.wechat_captures.get(body.capture_token, browser, rid)
    check_material(state, capture, rid, body.import_id)
    choice = stickers(state).get(body.selection_token, browser, capture)
    recognized = inspect_choice(choice)
    item = {"kind": "wechat_sticker", "selection_token": choice.token,
            "query": choice.query, "label": choice.label, "sha256": choice.sha256,
            "chat_signature": choice.chat_signature,
            "preview_url": choice.public()["preview_url"], **recognized}
    token, receipt = store(state).preview(browser, capture, [item])
    return public_receipt(token, receipt)


@router.post("/stickers/confirm")
def sticker_confirm(body: ConfirmIn, request: Request):
    state = get_state(request)
    browser = owner(request)

    def operation(receipt, update):
        enabled_state(request)
        if not state._flag(ALLOW_FILL, False):
            raise RapportError("wechat_fill_disabled", "请先允许确认后的微信发送。", status=409)
        rid = state.resolve_relationship_id(receipt["relationship_id"])
        capture = state.wechat_captures.get(receipt["capture_token"], browser, rid)
        check_material(state, capture, rid, receipt["import_id"])
        if capture.digest != receipt["context_digest"] or capture.contact != receipt["contact"]:
            raise RapportError("dispatch_context_changed", "聊天绑定已变化，没有发送表情。", status=409)
        if len(receipt["items"]) != 1 or receipt["items"][0]["kind"] != "wechat_sticker":
            raise RapportError("sticker_preview_changed", "预览项目已变化，没有发送表情。", status=409)
        item = receipt["items"][0]
        choice = stickers(state).get(item["selection_token"], browser, capture)
        if any(item[key] != getattr(choice, key) for key in ("query", "label", "sha256", "chat_signature")):
            raise RapportError("sticker_preview_changed", "所选表情已变化，没有发送。", status=409)
        state.wechat_captures.consume_fill(capture)
        if send_choice(capture, choice, state.settings.max_image_pixels,
                       lambda: update(0, "uncertain")) is not True:
            raise RapportError("dispatch_send_uncertain", "无法核对表情发送结果；不会重试。", status=409)
        update(0, "sent")

    receipt = state.desktop.run(store(state).execute, body.token, browser, body.digest, operation)
    return public_receipt(body.token, receipt)
