"""message-v1: unified conversation contract produced by every parser."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class Sender(StrEnum):
    me = "me"
    other = "other"
    system = "system"
    unknown = "unknown"


class TsPrecision(StrEnum):
    second = "second"
    minute = "minute"
    day = "day"
    # Inherited from the chat's nearest visible time label (WeChat hides per-message times):
    # usable for ordering and dating, never for response intervals.
    label = "label"
    none = "none"


class MsgType(StrEnum):
    text = "text"
    image = "image"
    voice = "voice"
    video = "video"
    sticker = "sticker"
    file = "file"
    call = "call"
    transfer = "transfer"
    link = "link"
    system = "system"
    unknown = "unknown"


class Source(StrEnum):
    paste = "paste"
    whatsapp_txt = "whatsapp_txt"
    qq_txt = "qq_txt"
    wechat_csv = "wechat_csv"
    wechat_json = "wechat_json"
    telegram_json = "telegram_json"
    generic_json = "generic_json"
    ocr = "ocr"
    demo = "demo"


MEDIA_TYPES = {MsgType.image, MsgType.voice, MsgType.video, MsgType.sticker, MsgType.file}


class Message(BaseModel):
    id: str
    seq: int
    sender: Sender
    ts: datetime | None = None
    ts_precision: TsPrecision = TsPrecision.none
    type: MsgType = MsgType.text
    text: str | None = None
    quoted_text: str | None = None
    raw: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def _id_format(cls, v: str) -> str:
        if not v:
            raise ValueError("message id required")
        return v

    @property
    def is_text(self) -> bool:
        return self.type == MsgType.text and bool(self.text and self.text.strip())

    @property
    def is_event(self) -> bool:
        """Any message by me/other counts as an interaction event."""
        return self.sender in (Sender.me, Sender.other)

    @property
    def has_reliable_ts(self) -> bool:
        return self.ts is not None and self.ts_precision in (TsPrecision.second, TsPrecision.minute)


class ParseWarning(BaseModel):
    code: str
    msg: str
    msg_ids: list[str] = Field(default_factory=list)


class Participants(BaseModel):
    me: str = "我"
    other: str = "对方"


class Conversation(BaseModel):
    schema_version: str = "message-v1"
    id: str
    source: Source
    participants: Participants = Field(default_factory=Participants)
    timezone: str = "Asia/Shanghai"
    parser_version: str = "parsers-v1"
    warnings: list[ParseWarning] = Field(default_factory=list)
    messages: list[Message] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)

    @field_validator("messages")
    @classmethod
    def _unique_ids(cls, msgs: list[Message]) -> list[Message]:
        seen: set[str] = set()
        for m in msgs:
            if m.id in seen:
                raise ValueError(f"duplicate message id {m.id}")
            seen.add(m.id)
        return msgs

    def has_reliable_time(self) -> bool:
        events = [m for m in self.messages if m.is_event]
        if not events:
            return False
        n = sum(1 for m in events if m.has_reliable_ts)
        return n >= max(2, len(events) // 2)

    def events(self) -> list[Message]:
        return [m for m in self.messages if m.is_event]


def make_id(seq: int) -> str:
    return f"m{seq:06d}"
