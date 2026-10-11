"""Single-chat JSON export parser: the nested `session` / `messages` shape.

Written for exports produced by common third-party WeChat backup tools, but the parser
never depends on which tool wrote the file: the header block can be called anything, and
only `session` + `messages` are required.

Shape::

    {"export": {"version": ..., "exportedAt": ..., "generator": ...},
     "session": {"wxid": ..., "nickname": ..., "remark": ..., "displayName": ..., ...},
     "messages": [{"localId", "createTime" (epoch seconds, str), "formattedTime", "type" (Chinese label),
                   "localType", "content", "isSend" ("0"/"1"), "senderUsername", "senderDisplayName",
                   "quotedContent"?, "quotedSender"?, "emojiMd5"?, ...}, ...],
     "avatars": {...}}

What matters for analysis:
- `type` is a Chinese label ("文本消息", "图片消息", ...); `localType` is WeChat's numeric type.
- Voice messages that WeChat already transcribed carry `[语音转文字] …` in `content`; we keep them as
  **text** (`meta.from_voice=True`) because the words are observable, unlike an untranscribed clip.
- Images / videos / stickers carry only a relative media path; the file itself is never opened here,
  so they stay media placeholders (evidence quality decides how much they count, not this parser).
- Quote messages put the reply body first and `[引用 <who>：<quoted>]` last; the quoted part is context.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from rapport.parsers.base import Builder, ParseError, ParseOptions, Parser, looks_system
from rapport.schemas.message_v1 import Conversation, MsgType, Participants, Sender, Source, TsPrecision

VOICE_TRANSCRIPT_PREFIX = "[语音转文字]"
QUOTE_TAIL_RE = re.compile(r"\s*\[引用\s*(?P<who>[^：:\]]{0,40})[：:]\s*(?P<quoted>.*?)\]\s*$", re.S)

# Chinese type label -> message type. Anything not listed becomes `unknown` (counted, never guessed).
TYPE_MAP: dict[str, MsgType] = {
    "文本消息": MsgType.text,
    "引用消息": MsgType.text,
    "语音消息": MsgType.voice,
    "图片消息": MsgType.image,
    "视频消息": MsgType.video,
    "动画表情": MsgType.sticker,
    "文件消息": MsgType.file,
    "通话消息": MsgType.call,
    "转账消息": MsgType.transfer,
    "红包消息": MsgType.transfer,
    "链接消息": MsgType.link,
    "小程序消息": MsgType.link,
    "名片消息": MsgType.link,
    "位置消息": MsgType.link,
    "音乐消息": MsgType.link,
    "系统消息": MsgType.system,
    "聊天记录": MsgType.unknown,
    "其他消息": MsgType.unknown,
}

_SNIFF_HEAD = 400000


def _export_header(data: dict) -> dict:
    """The export's header block, whatever the exporter called it: the first top-level object
    carrying `version` or `exportedAt`. Used for display metadata only."""

    for value in data.values():
        if isinstance(value, dict) and ("version" in value or "exportedAt" in value):
            return value
    return {}


def _export_header(data: dict) -> dict:
    """The export's header block, whatever the exporter called it: the first top-level object
    carrying `version` or `exportedAt`. Used for display metadata only."""

    for value in data.values():
        if isinstance(value, dict) and ("version" in value or "exportedAt" in value):
            return value
    return {}


def _export_header(data: dict) -> dict:
    """The export's header block, whatever the exporter called it: the first top-level object
    carrying `version` or `exportedAt`. Used for display metadata only."""

    for value in data.values():
        if isinstance(value, dict) and ("version" in value or "exportedAt" in value):
            return value
    return {}


class WxSessionJsonParser(Parser):
    id = "wx_session_json"
    label = "微信单聊导出 JSON（session + messages）"
    source = Source.wechat_json
    accepts_files = (".json",)

    def sniff(self, text: str, filename: str | None = None) -> float:
        # Structural, never brand-based. The head is read generously because `messages` can sit
        # after a long `session` block, and a real multi-year export has thousands of characters
        # of it. Must beat PasteABParser, which scores a single-line JSON blob at up to 0.6.
        head = text[:_SNIFF_HEAD]
        if '"session"' in head and '"messages"' in head:
            return 0.99
        return 0.0

    def parse(self, text: str, conv_id: str, options: ParseOptions | None = None) -> Conversation:
        options = options or ParseOptions()
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ParseError("bad_json", f"JSON 语法错误：{e.msg}（第 {e.lineno} 行）") from e
        if not isinstance(data, dict) or not isinstance(data.get("messages"), list) or not isinstance(data.get("session"), dict):
            raise ParseError(
                "unsupported_format",
                "不是支持的单聊导出 JSON（需要 session 与 messages 两个顶层键）。",
                "请导出与某一位联系人的单聊，而不是群聊或整库导出。",
            )
        session = data.get("session") or {}
        if str(session.get("type") or "").lower() in ("group", "chatroom"):
            raise ParseError("not_personal_chat", "这是群聊导出，只支持单聊。")

        b = Builder(source=self.source, timezone=options.timezone)
        tz = _tz(options.timezone)
        other_name = str(session.get("remark") or session.get("displayName") or session.get("nickname") or "对方").strip() or "对方"
        me_name = "我"
        counts: dict[str, int] = {}
        unknown_types: dict[str, int] = {}
        n_voice_transcribed = 0
        n_voice_raw = 0

        for item in data["messages"]:
            if not isinstance(item, dict):
                continue
            label = str(item.get("type") or "").strip()
            typ = TYPE_MAP.get(label)
            if typ is None:
                typ = MsgType.unknown
                unknown_types[label or "?"] = unknown_types.get(label or "?", 0) + 1
            counts[typ.value] = counts.get(typ.value, 0) + 1

            is_send = str(item.get("isSend") or "0") == "1"
            content = str(item.get("content") or "").strip()
            ts, prec = _ts(item, tz)
            meta: dict = {"export_type": label}
            if item.get("localId") is not None:
                meta["local_id"] = str(item["localId"])

            if typ == MsgType.system:
                b.add(Sender.system, content or label, ts, prec, MsgType.system, meta=meta)
                continue

            sender = Sender.me if is_send else Sender.other
            if not is_send and item.get("senderDisplayName"):
                other_name = str(item["senderDisplayName"]).strip() or other_name
            elif is_send and item.get("senderDisplayName") and me_name == "我":
                me_name = str(item["senderDisplayName"]).strip() or "我"

            if typ == MsgType.voice:
                if content.startswith(VOICE_TRANSCRIPT_PREFIX):
                    words = content[len(VOICE_TRANSCRIPT_PREFIX) :].strip()
                    if words:
                        meta["from_voice"] = True
                        n_voice_transcribed += 1
                        b.add(sender, words, ts, prec, MsgType.text, meta=meta)
                        continue
                n_voice_raw += 1
                b.add(sender, None, ts, prec, MsgType.voice, meta=meta)
                continue

            if typ == MsgType.text:
                quoted = None
                body = content
                if label == "引用消息":
                    q = QUOTE_TAIL_RE.search(content)
                    if q:
                        body = content[: q.start()].strip()
                        quoted = q.group("quoted").strip() or None
                        meta["quoted_by"] = q.group("who").strip()
                    if not quoted and item.get("quotedContent"):
                        quoted = str(item["quotedContent"]).strip() or None
                if not body:
                    # An empty text row is not evidence of anything; keep it as unknown.
                    b.add(sender, None, ts, prec, MsgType.unknown, quoted=quoted, meta=meta)
                    continue
                if looks_system(body):
                    b.add(Sender.system, body, ts, prec, MsgType.system, meta=meta)
                    continue
                b.add(sender, body, ts, prec, MsgType.text, quoted=quoted, meta=meta)
                continue

            # Media and other non-text rows: keep the placeholder, never the content.
            if typ in (MsgType.image, MsgType.video, MsgType.sticker, MsgType.file):
                meta["media_ref"] = content[:200] if content else None
                if item.get("emojiMd5"):
                    meta["emoji_md5"] = str(item["emojiMd5"])
            elif typ == MsgType.call:
                meta["call"] = content[:60]
            elif typ == MsgType.transfer:
                meta["transfer"] = content[:60]
            elif typ == MsgType.link:
                title = item.get("linkTitle") or item.get("appMsgAppName") or content[:80]
                meta["link_title"] = str(title)[:120] if title else None
            b.add(sender, None, ts, prec, typ, meta=meta)

        if not b.messages:
            raise ParseError("empty", "导出中没有消息。")
        if options.me_name and options.me_name == other_name:
            # The user says the "other" display name is actually them: flip roles.
            for m in b.messages:
                if m.sender == Sender.me:
                    m.sender = Sender.other
                elif m.sender == Sender.other:
                    m.sender = Sender.me
            me_name, other_name = other_name, me_name
        b.participants = Participants(me=me_name, other=other_name)
        b.meta["names"] = [me_name, other_name]
        header = _export_header(data)
        b.meta["export"] = {
            "version": header.get("version"),
            "exported_at": header.get("exportedAt"),
            "type_counts": counts,
            "unknown_types": unknown_types,
            "voice_transcribed": n_voice_transcribed,
            "voice_untranscribed": n_voice_raw,
        }
        n_media = sum(counts.get(k, 0) for k in ("image", "video", "sticker", "file", "voice"))
        if n_media:
            b.warn(
                "media_unobserved",
                f"{n_media} 条图片/视频/表情/文件/语音只保留了占位，内容未读取，不作为证据。",
            )
        if n_voice_transcribed:
            b.warn("voice_transcribed", f"{n_voice_transcribed} 条语音使用微信自带转写文字，按文本处理。")
        return b.build(conv_id)


def _tz(name: str):
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # pragma: no cover - platform without tzdata
        return timezone(timedelta(hours=8))


def _ts(item: dict, tz) -> tuple[datetime | None, TsPrecision]:
    raw = item.get("createTime")
    if raw not in (None, ""):
        try:
            return datetime.fromtimestamp(int(str(raw)), tz).replace(tzinfo=None), TsPrecision.second
        except (ValueError, OverflowError, OSError):
            pass
    fmt = item.get("formattedTime")
    if fmt:
        for pattern, prec in (("%Y-%m-%d %H:%M:%S", TsPrecision.second), ("%Y-%m-%d %H:%M", TsPrecision.minute), ("%Y-%m-%d", TsPrecision.day)):
            try:
                return datetime.strptime(str(fmt), pattern), prec
            except ValueError:
                continue
    return None, TsPrecision.none
