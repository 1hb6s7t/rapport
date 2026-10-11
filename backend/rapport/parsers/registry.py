"""Parser registry: explicit format selection first, then sniffing."""

from __future__ import annotations

from rapport.parsers.base import ParseError, ParseOptions, Parser
from rapport.parsers.generic_json import GenericJsonParser
from rapport.parsers.paste_ab import PasteABParser
from rapport.parsers.qq_txt import QQTxtParser
from rapport.parsers.telegram_json import TelegramJsonParser
from rapport.parsers.wechat_export import WeChatCsvParser, WeChatJsonParser
from rapport.parsers.whatsapp_txt import WhatsAppTxtParser
from rapport.parsers.wx_session_json import WxSessionJsonParser
from rapport.schemas.message_v1 import Conversation

PARSERS: dict[str, Parser] = {
    p.id: p
    for p in (
        GenericJsonParser(),
        TelegramJsonParser(),
        WxSessionJsonParser(),
        WeChatJsonParser(),
        WeChatCsvParser(),
        QQTxtParser(),
        WhatsAppTxtParser(),
        PasteABParser(),
    )
}


def list_formats() -> list[dict]:
    return [{"id": p.id, "label": p.label, "files": list(p.accepts_files)} for p in PARSERS.values()]


def sniff(text: str, filename: str | None = None) -> list[tuple[str, float]]:
    scores = [(pid, p.sniff(text, filename)) for pid, p in PARSERS.items()]
    return sorted([s for s in scores if s[1] > 0], key=lambda x: -x[1])


def parse(
    text: str,
    conv_id: str,
    fmt: str | None = None,
    filename: str | None = None,
    options: ParseOptions | None = None,
) -> tuple[Conversation, str]:
    """Return (conversation, parser_id). Raises ParseError."""
    if fmt and fmt != "auto":
        if fmt not in PARSERS:
            raise ParseError("unknown_format", f"未知格式：{fmt}")
        return PARSERS[fmt].parse(text, conv_id, options), fmt
    ranked = sniff(text, filename)
    if not ranked:
        raise ParseError(
            "unsupported_format",
            "无法自动识别材料格式。",
            "请手动选择格式，或使用 A/B 粘贴模板：每行「A: 内容」「B: 内容」。",
        )
    last_err: ParseError | None = None
    for pid, _ in ranked[:3]:
        try:
            return PARSERS[pid].parse(text, conv_id, options), pid
        except ParseError as e:
            if e.code in ("ambiguous_date", "too_many_participants", "multi_chat_export", "not_personal_chat"):
                raise
            last_err = e
    assert last_err is not None
    raise last_err
