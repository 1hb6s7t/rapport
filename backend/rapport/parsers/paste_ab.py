"""A/B paste template parser.

Accepted line forms (each message on its own line, continuation lines are appended):
    A: 你好
    B: 在吗
    我：今天有空吗
    对方：有的
    [2026-09-01 20:15] A: 你好
    2026-09-01 20:15 B: 在
    A [20:15]: 你好
Names other than A/B/我/对方 are allowed when the file uses exactly two names;
the first name seen becomes "me" unless options.me_name says otherwise.
"""

from __future__ import annotations

import re
from datetime import datetime

from rapport.parsers.base import Builder, ParseError, ParseOptions, Parser, classify_text, looks_system
from rapport.schemas.message_v1 import Conversation, MsgType, Participants, Sender, Source, TsPrecision

_TS_BODY = r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?|\d{1,2}:\d{2}(?::\d{2})?"
LINE_RE = re.compile(
    rf"^\s*(?:\[(?P<ts>{_TS_BODY})\]\s*|(?P<ts1>{_TS_BODY})\s+)?(?P<name>[^:：\[\]]{{1,24}}?)\s*(?:\[(?P<ts2>[^\]]+)\])?\s*[:：]\s?(?P<body>.*)$"
)

ME_NAMES = {"a", "我", "me", "自己", "本人"}
OTHER_NAMES = {"b", "对方", "other", "ta", "他", "她"}


def _parse_ts(s: str | None) -> tuple[datetime | None, TsPrecision]:
    if not s:
        return None, TsPrecision.none
    s = s.strip().replace("/", "-").replace(".", "-").replace("T", " ")
    for fmt, prec in (
        ("%Y-%m-%d %H:%M:%S", TsPrecision.second),
        ("%Y-%m-%d %H:%M", TsPrecision.minute),
        ("%Y-%m-%d", TsPrecision.day),
    ):
        try:
            return datetime.strptime(s, fmt), prec
        except ValueError:
            continue
    return None, TsPrecision.none


class PasteABParser(Parser):
    id = "paste"
    label = "A/B 粘贴模板"
    source = Source.paste
    accepts_files = (".txt",)

    def sniff(self, text: str, filename: str | None = None) -> float:
        lines = [ln for ln in text.splitlines() if ln.strip()][:60]
        if not lines:
            return 0.0
        hits = 0
        ab_hits = 0
        for ln in lines:
            m = LINE_RE.match(ln)
            if m and m.group("name"):
                hits += 1
                if m.group("name").strip().lower() in ME_NAMES | OTHER_NAMES:
                    ab_hits += 1
        ratio = hits / len(lines)
        if ratio > 0.5 and ab_hits / len(lines) > 0.5:
            return 0.97
        return 0.6 * ratio if ratio > 0.5 else 0.0

    def parse(self, text: str, conv_id: str, options: ParseOptions | None = None) -> Conversation:
        options = options or ParseOptions()
        b = Builder(source=self.source, timezone=options.timezone)
        names: list[str] = []
        last = None
        n_with_ts = 0
        for raw in text.splitlines():
            if not raw.strip():
                continue
            m = LINE_RE.match(raw)
            if not m or not m.group("body") and not m.group("name"):
                if last is not None:
                    # continuation line
                    last.text = ((last.text or "") + "\n" + raw.strip()).strip()
                    last.raw = (last.raw or "") + "\n" + raw
                continue
            name = m.group("name").strip()
            body = m.group("body")
            ts, prec = _parse_ts(m.group("ts") or m.group("ts1") or m.group("ts2"))
            if ts is not None:
                n_with_ts += 1
            key = name.lower()
            if key in ME_NAMES:
                sender = Sender.me
            elif key in OTHER_NAMES:
                sender = Sender.other
            elif looks_system(body) or key in {"system", "系统"}:
                sender = Sender.system
            else:
                if name not in names:
                    names.append(name)
                if len(names) > 2:
                    raise ParseError(
                        "too_many_participants",
                        f"检测到超过两个发言者：{', '.join(names[:3])}…",
                        "A/B 模板只支持两人对话，请使用 A:/B: 或 我：/对方：。",
                    )
                me_name = options.me_name or names[0]
                sender = Sender.me if name == me_name else Sender.other
            typ, body_text = classify_text(body)
            if sender == Sender.system:
                typ = MsgType.system
            last = b.add(sender, body_text if typ == MsgType.text else None, ts, prec, typ, raw=raw)
            if typ != MsgType.text:
                last.meta["placeholder"] = body.strip()
        if not b.messages:
            raise ParseError("empty", "没有解析出任何消息。", "每行格式：A: 内容 或 我：内容。")
        if names:
            me_name = options.me_name or names[0]
            other_name = next((n for n in names if n != me_name), "对方")
            b.participants = Participants(me=me_name, other=other_name)
        if n_with_ts and n_with_ts < len(b.messages):
            b.warn("partial_time", f"{len(b.messages) - n_with_ts} 条消息缺少时间，时间类指标只统计有时间的消息。")
        if n_with_ts == 0:
            b.warn("no_time", "材料没有时间信息，回复速度等时间指标将不可用。")
        return b.build(conv_id)
