"""Parser base classes and shared helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from rapport.config import PARSER_VERSION
from rapport.errors import RapportError
from rapport.schemas.message_v1 import (
    Conversation,
    Message,
    MsgType,
    ParseWarning,
    Participants,
    Sender,
    Source,
    TsPrecision,
    make_id,
)


class ParseError(RapportError):
    def __init__(self, code: str, msg: str, hint: str = ""):
        status = 422 if code in ("ambiguous_date", "too_many_participants", "multi_chat_export") else 400
        super().__init__(code, msg, hint, status=status)


@dataclass
class ParseOptions:
    """User-selected options that resolve parser ambiguities."""

    date_order: str | None = None  # "DMY" | "MDY" | "YMD"
    me_name: str | None = None  # which participant name is "me"
    timezone: str = "Asia/Shanghai"
    extra: dict = field(default_factory=dict)


@dataclass
class Builder:
    """Accumulates messages with sequential ids."""

    source: Source
    participants: Participants = field(default_factory=Participants)
    messages: list[Message] = field(default_factory=list)
    warnings: list[ParseWarning] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    timezone: str = "Asia/Shanghai"

    def add(
        self,
        sender: Sender,
        text: str | None,
        ts: datetime | None = None,
        precision: TsPrecision = TsPrecision.none,
        type_: MsgType = MsgType.text,
        quoted: str | None = None,
        raw: str | None = None,
        meta: dict | None = None,
    ) -> Message:
        seq = len(self.messages)
        if ts is None:
            precision = TsPrecision.none
        m = Message(
            id=make_id(seq),
            seq=seq,
            sender=sender,
            ts=ts,
            ts_precision=precision,
            type=type_,
            text=text,
            quoted_text=quoted,
            raw=raw,
            meta=meta or {},
        )
        self.messages.append(m)
        return m

    def warn(self, code: str, msg: str, ids: list[str] | None = None) -> None:
        self.warnings.append(ParseWarning(code=code, msg=msg, msg_ids=ids or []))

    def build(self, conv_id: str) -> Conversation:
        return Conversation(
            id=conv_id,
            source=self.source,
            participants=self.participants,
            timezone=self.timezone,
            parser_version=PARSER_VERSION,
            warnings=self.warnings,
            messages=self.messages,
            meta=self.meta,
        )


class Parser:
    """Base parser. Subclasses implement `sniff` and `parse`."""

    id: str = ""
    label: str = ""
    source: Source = Source.generic_json
    accepts_files: tuple[str, ...] = ()

    def sniff(self, text: str, filename: str | None = None) -> float:
        """Return a confidence in [0, 1] that this parser can handle the input."""
        return 0.0

    def parse(self, text: str, conv_id: str, options: ParseOptions | None = None) -> Conversation:
        raise NotImplementedError


# ---------- shared media / text classification helpers ----------

MEDIA_PLACEHOLDERS: list[tuple[re.Pattern[str], MsgType]] = [
    (re.compile(r"^\s*\[(图片|照片|image|photo)\]\s*$", re.I), MsgType.image),
    (re.compile(r"^\s*\[(语音|voice|audio)(\s*\d+[\"”′']?)?\]\s*$", re.I), MsgType.voice),
    (re.compile(r"^\s*\[(视频|video)\]\s*$", re.I), MsgType.video),
    (re.compile(r"^\s*\[(表情|动画表情|sticker|emoji|贴纸)\]\s*$", re.I), MsgType.sticker),
    (re.compile(r"^\s*\[(文件|file)(:.*)?\]\s*$", re.I), MsgType.file),
    (re.compile(r"^\s*\[(语音通话|视频通话|通话|call)[^\]]*\]\s*$", re.I), MsgType.call),
    (re.compile(r"^\s*\[(转账|红包|transfer)[^\]]*\]\s*$", re.I), MsgType.transfer),
    (re.compile(r"^\s*\[(链接|link|分享|卡片)[^\]]*\]\s*$", re.I), MsgType.link),
    (re.compile(r"^\s*<?(Media omitted|image omitted|video omitted|audio omitted|sticker omitted|document omitted)>?\s*$", re.I), MsgType.image),
    (re.compile(r"^\s*‎?(图片|视频|音频|语音|贴图|文档)已省略\s*$"), MsgType.image),
    (re.compile(r"^\s*‎?<附件[:：].*>\s*$"), MsgType.file),
]

_WA_OMITTED_KIND = {
    "image": MsgType.image,
    "video": MsgType.video,
    "audio": MsgType.voice,
    "sticker": MsgType.sticker,
    "document": MsgType.file,
    "图片": MsgType.image,
    "视频": MsgType.video,
    "音频": MsgType.voice,
    "语音": MsgType.voice,
    "贴图": MsgType.sticker,
    "文档": MsgType.file,
}


def classify_text(text: str) -> tuple[MsgType, str | None]:
    """Map placeholder texts to media types. Returns (type, text_or_none)."""
    stripped = text.replace("‎", "").replace("‏", "").strip()
    if not stripped:
        return MsgType.text, None
    for pat, typ in MEDIA_PLACEHOLDERS:
        if pat.match(stripped):
            # refine WhatsApp "xxx omitted"
            m = re.match(r"^\s*‎?<?(\w+|[一-鿿]+)\s*(omitted|已省略)", stripped, re.I)
            if m and m.group(1).lower() in _WA_OMITTED_KIND:
                return _WA_OMITTED_KIND[m.group(1).lower()], None
            return typ, None
    return MsgType.text, stripped


SYSTEM_HINTS = (
    "撤回了一条消息",
    "recalled a message",
    "以上是打招呼的内容",
    "你已添加了",
    "已经是你的好友",
    "拍了拍",
    "Messages and calls are end-to-end encrypted",
    "消息和通话都进行了端到端加密",
    "changed the subject",
    "joined using",
    "对方已通过你的朋友验证",
)


def looks_system(text: str) -> bool:
    return any(h in text for h in SYSTEM_HINTS)
