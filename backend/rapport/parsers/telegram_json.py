"""Telegram Desktop single-chat export (result.json) parser."""

from __future__ import annotations

import json
from datetime import datetime

from rapport.parsers.base import Builder, ParseError, ParseOptions, Parser
from rapport.schemas.message_v1 import Conversation, MsgType, Participants, Sender, Source, TsPrecision


def _flatten_text(t) -> str:
    if isinstance(t, str):
        return t
    if isinstance(t, list):
        parts = []
        for p in t:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict):
                parts.append(str(p.get("text", "")))
        return "".join(parts)
    return ""


class TelegramJsonParser(Parser):
    id = "telegram_json"
    label = "Telegram Desktop 导出 JSON"
    source = Source.telegram_json
    accepts_files = (".json",)

    def sniff(self, text: str, filename: str | None = None) -> float:
        head = text[:600]
        if '"messages"' in head and ('"from_id"' in text[:5000] or '"type": "personal_chat"' in head):
            return 0.95
        return 0.0

    def parse(self, text: str, conv_id: str, options: ParseOptions | None = None) -> Conversation:
        options = options or ParseOptions()
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ParseError("bad_json", f"JSON 语法错误：{e.msg}（第 {e.lineno} 行）") from e
        if isinstance(data, dict) and "chats" in data:
            raise ParseError(
                "multi_chat_export",
                "这是整个账号的导出，包含多个会话。",
                "请在 Telegram Desktop 中打开与对方的单聊，使用「导出聊天记录」导出为 JSON。",
            )
        if not isinstance(data, dict) or "messages" not in data:
            raise ParseError("unsupported_format", "不是 Telegram 单聊导出的 result.json。")
        if data.get("type") not in (None, "personal_chat", "private"):
            raise ParseError("not_personal_chat", f"会话类型为 {data.get('type')}，只支持单聊。")
        b = Builder(source=self.source, timezone=options.timezone)
        from_ids: list[str] = []
        names: dict[str, str] = {}
        for item in data["messages"]:
            if item.get("type") == "service":
                b.add(Sender.system, _flatten_text(item.get("text")) or item.get("action", "service"), _ts(item), TsPrecision.second, MsgType.system)
                continue
            fid = str(item.get("from_id") or item.get("from") or "?")
            if fid not in from_ids:
                from_ids.append(fid)
                names[fid] = str(item.get("from") or fid)
            if len(from_ids) > 2:
                raise ParseError("too_many_participants", "检测到第三个参与者，只支持单聊。", "请导出与对方的单聊。")
            me_key = options.me_name
            if me_key:
                sender = Sender.me if names.get(fid) == me_key or fid == me_key else Sender.other
            else:
                sender = Sender.me if fid == from_ids[0] else Sender.other
            txt = _flatten_text(item.get("text")).strip()
            typ = MsgType.text
            if item.get("media_type") in ("voice_message", "audio_file"):
                typ = MsgType.voice
            elif item.get("media_type") == "sticker":
                typ = MsgType.sticker
            elif item.get("media_type") in ("video_message", "video_file"):
                typ = MsgType.video
            elif item.get("photo"):
                typ = MsgType.image
            elif item.get("file") and item.get("media_type") not in ("sticker",):
                typ = MsgType.file
            m = b.add(sender, txt if txt else None, _ts(item), TsPrecision.second, typ if not txt or typ != MsgType.text else MsgType.text)
            if typ != MsgType.text:
                m.type = typ
                m.meta["media_type"] = item.get("media_type") or ("photo" if item.get("photo") else "file")
            if item.get("reply_to_message_id"):
                m.meta["reply_to"] = item["reply_to_message_id"]
            m.meta["tg_id"] = item.get("id")
        if not b.messages:
            raise ParseError("empty", "导出中没有消息。")
        if from_ids:
            me_id = from_ids[0]
            other_id = from_ids[1] if len(from_ids) > 1 else None
            b.participants = Participants(me=names.get(me_id, "我"), other=names.get(other_id, "对方") if other_id else "对方")
        b.meta["names"] = [names[f] for f in from_ids]
        return b.build(conv_id)


def _ts(item: dict) -> datetime | None:
    raw = item.get("date")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None
