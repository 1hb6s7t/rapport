"""Visible WeChat capture contracts and short-lived, in-memory review tickets.

Images and unimported OCR are never written to disk. Explicit imports use the existing
encrypted material store. The adapter reads pixels; it never reads a chat database.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
import unicodedata
from dataclasses import dataclass

from rapport.errors import RapportError
from rapport.ocr.engine import OcrBox
from rapport.ocr.layout import Page, cluster_lines, pages_to_conversation
from rapport.schemas.message_v1 import Conversation, ParseWarning


def normalized_title(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).split())


def verify_contact(boxes: list[OcrBox], expected: str) -> None:
    target = normalized_title(expected)
    if not target:
        raise RapportError("wechat_contact_required", "请填写微信聊天顶部显示的好友名称。", status=422)
    # Compare the complete title line: a separately detected surname or English
    # suffix must not pass as the full contact name.
    candidates = [line.text for line in cluster_lines(boxes) if line.score >= 0.70]
    if target not in {normalized_title(text) for text in candidates}:
        hint = ("未能从自动定位的聊天标题识别到清晰文字，请确认微信没有弹窗遮挡后重试。"
                if not candidates else "请填写好友完整名称或备注（包含英文后缀）；不会使用部分姓名放行。")
        raise RapportError("wechat_contact_mismatch", "未能核对指定好友，本次没有导入聊天内容。", hint, 409)


def to_conversation(boxes: list[OcrBox], width: int, height: int, contact: str, me_side: str = "right", media=()) -> Conversation:
    if me_side not in ("left", "right"):
        raise RapportError("wechat_bad_side", "请选择我方气泡所在方向。", status=422)
    page = Page("微信可见聊天", boxes, width, height, media=list(media))
    conversation = pages_to_conversation([page], secrets.token_hex(8), me_side=me_side)
    conversation.participants.other = contact
    conversation.warnings.append(ParseWarning(code="visible_only", msg="仅包含本次窗口中可见的文字；图片、语音及未显示的历史不在本次读取范围。请核对角色、文字与时间。"))
    if not conversation.events():
        raise RapportError("wechat_no_messages", "当前聊天画面未识别到文字。", "可能只有图片或语音，请确认该好友有可读取的文字聊天后重试。", 422)
    return conversation


def text_events(conversation: Conversation) -> list:
    """Visible text messages only. Picture and file placeholders come from pixels, not words: a
    sent image must not read as a changed text context, nor be confused with a sent text."""
    return [message for message in conversation.events() if message.text is not None]


def context_digest(conversation: Conversation) -> str:
    if conversation.meta.get("context_source") == "dsh_database":
        scope = [conversation.meta.get("account"), conversation.meta.get("contact_id")]
        return hashlib.sha256(json.dumps(["dsh-v1", scope, database_rows(conversation)],
                                         ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    rows = [f"{message.sender.value}:{normalized_title(message.text or '')}" for message in text_events(conversation)]
    return hashlib.sha256("\n".join(rows).encode()).hexdigest()


def database_rows(conversation: Conversation):
    """Stable source IDs and all message types; never reduce cards to empty text."""
    return [(m.id, m.sender.value, m.type.value, m.text, m.quoted_text,
             m.ts.isoformat() if m.ts else None, hashlib.sha256((m.raw or "").encode()).hexdigest())
            for m in conversation.messages]


@dataclass
class CaptureTicket:
    token: str
    owner: str
    window_id: int
    process_id: int
    contact: str
    relationship_id: str
    conversation: Conversation
    digest: str
    created: float
    import_id: str | None = None
    filled: bool = False
    context_source: str = "visual"
    source_binding: dict | None = None


class CaptureTickets:
    """Bounded capability store. Browser session and relationship are both part of the scope."""

    ttl = 600
    limit = 8

    def __init__(self):
        self.items: dict[str, CaptureTicket] = {}
        self.lock = threading.RLock()

    def add(self, owner: str, window_id: int, process_id: int, contact: str, relationship_id: str,
            conversation: Conversation) -> CaptureTicket:
        with self.lock:
            now = time.monotonic()
            self.items = {key: value for key, value in self.items.items() if now - value.created < self.ttl}
            if len(self.items) >= self.limit:
                self.items.pop(min(self.items, key=lambda key: self.items[key].created))
            ticket = CaptureTicket(secrets.token_urlsafe(24), owner, window_id, process_id, contact, relationship_id,
                                   conversation, context_digest(conversation), now)
            self.items[ticket.token] = ticket
            return ticket

    def get(self, token: str, owner: str, relationship_id: str) -> CaptureTicket:
        with self.lock:
            ticket = self.items.get(token)
            if not ticket or time.monotonic() - ticket.created >= self.ttl:
                self.items.pop(token, None)
                raise RapportError("wechat_capture_expired", "本次读取已失效，请重新读取微信窗口。", status=409)
            if ticket.owner != owner or ticket.relationship_id != relationship_id:
                raise RapportError("wechat_target_scope", "该读取记录不属于当前会话或关系对象。", status=403)
            return ticket

    def consume_fill(self, ticket: CaptureTicket) -> None:
        with self.lock:
            current = self.get(ticket.token, ticket.owner, ticket.relationship_id)
            if current.filled:
                raise RapportError("wechat_already_filled", "这次读取已执行过填入，请在微信查看；如需再次操作请重新读取。", status=409)
            current.filled = True

    def clear(self) -> None:
        with self.lock:
            self.items.clear()

    def discard_relationship(self, relationship_id: str) -> None:
        with self.lock:
            self.items = {key: item for key, item in self.items.items() if item.relationship_id != relationship_id}
