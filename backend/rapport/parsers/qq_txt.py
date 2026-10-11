"""QQ message manager TXT export parser.

Format (as exported by QQ 消息管理器 → 导出消息记录 → txt):
    消息记录（此消息记录为文本格式，不支持查看图片、语音等）
    ================================================================
    消息分组:我的好友
    ================================================================
    消息对象:小明
    ================================================================

    2026-09-01 20:15:33 小明(123456789)
    你好

    2026-09-01 20:16:02 我的昵称<me@example.com>
    在吗
Header line = timestamp + name + (qq) or <email>. Following lines until blank = body.
"""

from __future__ import annotations

import re
from datetime import datetime

from rapport.parsers.base import Builder, ParseError, ParseOptions, Parser, classify_text, looks_system
from rapport.schemas.message_v1 import Conversation, MsgType, Participants, Sender, Source, TsPrecision

HEAD_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2}:\d{2})\s+(?P<name>.+?)\s*(?:\((?P<qq>\d{5,12})\)|<(?P<mail>[^>]+)>)\s*$"
)
OBJECT_RE = re.compile(r"^消息对象[:：]\s*(?P<name>.+?)\s*$")


class QQTxtParser(Parser):
    id = "qq_txt"
    label = "QQ 消息管理器 TXT"
    source = Source.qq_txt
    accepts_files = (".txt",)

    def sniff(self, text: str, filename: str | None = None) -> float:
        head = text[:2000]
        score = 0.0
        if "消息记录" in head and "消息对象" in head:
            score = 0.95
        lines = [ln for ln in text.splitlines() if ln.strip()][:80]
        hits = sum(1 for ln in lines if HEAD_RE.match(ln))
        if lines and hits / len(lines) >= 0.25:
            score = max(score, 0.8)
        return score

    def parse(self, text: str, conv_id: str, options: ParseOptions | None = None) -> ParseOptions | Conversation:
        options = options or ParseOptions()
        b = Builder(source=self.source, timezone=options.timezone)
        other_name: str | None = None
        names: list[str] = []
        current: tuple[datetime, str, str] | None = None  # (ts, name, uid)
        body_lines: list[str] = []

        def flush() -> None:
            nonlocal current, body_lines
            if current is None:
                return
            ts, name, uid = current
            body = "\n".join(body_lines).strip()
            if uid not in names:
                names.append(uid)
            if other_name and name == other_name:
                sender = Sender.other
            elif options.me_name and name == options.me_name:
                sender = Sender.me
            elif other_name:
                sender = Sender.me
            else:
                sender = Sender.me if uid == names[0] else Sender.other
            typ, body_text = classify_text(body)
            if looks_system(body):
                sender, typ = Sender.system, MsgType.system
            m = b.add(sender, body_text if typ == MsgType.text else body, ts, TsPrecision.second, typ)
            if typ != MsgType.text and typ != MsgType.system:
                m.text = None
                m.meta["placeholder"] = body
            current = None
            body_lines = []

        for raw in text.splitlines():
            om = OBJECT_RE.match(raw.strip())
            if om and other_name is None:
                other_name = om.group("name")
                continue
            hm = HEAD_RE.match(raw.rstrip())
            if hm:
                flush()
                ts = datetime.strptime(hm.group("ts"), "%Y-%m-%d %H:%M:%S")
                uid = hm.group("qq") or hm.group("mail") or hm.group("name")
                current = (ts, hm.group("name").strip(), uid)
                continue
            if current is not None:
                if raw.strip() == "" and body_lines:
                    flush()
                elif raw.strip() or body_lines:
                    body_lines.append(raw.rstrip())
        flush()
        if not b.messages:
            raise ParseError("unsupported_format", "没有识别到 QQ 消息管理器导出格式。", "请使用 QQ 消息管理器导出为 .txt。")
        if len(names) > 2:
            raise ParseError("too_many_participants", "检测到超过两个发言者，可能是群聊导出。", "只支持好友单聊记录。")
        me_alias = options.me_name or "我"
        b.participants = Participants(me=me_alias, other=other_name or "对方")
        b.meta["names"] = names
        return b.build(conv_id)
