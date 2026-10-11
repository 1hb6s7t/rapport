"""WhatsApp official TXT export parser.

Whitelisted line formats (tested with synthetic fixtures):
    [01/09/2026, 20:15:33] Name: message         (iOS, with seconds, brackets)
    01/09/2026, 20:15 - Name: message            (Android, dash)
    2026/09/01 20:15 - Name: message             (some locales, YMD)
    [2026/9/1 20:15:33] Name: message
Multi-line messages continue on following lines. Media placeholders become media events.
Ambiguous day/month (both <= 12) requires options.date_order.
"""

from __future__ import annotations

import re
from datetime import datetime

from rapport.parsers.base import Builder, ParseError, ParseOptions, Parser, classify_text, looks_system
from rapport.schemas.message_v1 import Conversation, MsgType, Participants, Sender, Source, TsPrecision

HEAD_RE = re.compile(
    r"^‎?\[?(?P<d1>\d{1,4})[/.\-](?P<d2>\d{1,2})[/.\-](?P<d3>\d{1,4}),?\s+"
    r"(?P<h>\d{1,2}):(?P<mi>\d{2})(?::(?P<s>\d{2}))?\s*(?P<ampm>[AaPp]\.?[Mm]\.?|上午|下午)?\]?\s*-?\s*"
    r"(?P<name>[^:]{1,40}?):\s(?P<body>.*)$"
)
SYSTEM_RE = re.compile(
    r"^‎?\[?(?P<d1>\d{1,4})[/.\-](?P<d2>\d{1,2})[/.\-](?P<d3>\d{1,4}),?\s+"
    r"(?P<h>\d{1,2}):(?P<mi>\d{2})(?::(?P<s>\d{2}))?\s*(?P<ampm>[AaPp]\.?[Mm]\.?|上午|下午)?\]?\s*-?\s*(?P<body>[^:]*)$"
)


def _resolve_date(d1: str, d2: str, d3: str, order: str | None) -> tuple[int, int, int, bool]:
    """Return (year, month, day, ambiguous)."""
    a, b, c = int(d1), int(d2), int(d3)
    if len(d1) == 4:
        return a, b, c, False  # YMD
    year = c if c > 99 else 2000 + c
    if a > 12 and b <= 12:
        return year, b, a, False  # DMY forced
    if b > 12 and a <= 12:
        return year, a, b, False  # MDY forced
    if a == b:
        return year, a, b, False
    if order == "DMY":
        return year, b, a, False
    if order == "MDY":
        return year, a, b, False
    return year, b, a, True  # default DMY but flag ambiguous


class WhatsAppTxtParser(Parser):
    id = "whatsapp_txt"
    label = "WhatsApp 导出 TXT"
    source = Source.whatsapp_txt
    accepts_files = (".txt",)

    def sniff(self, text: str, filename: str | None = None) -> float:
        lines = [ln for ln in text.splitlines() if ln.strip()][:40]
        if not lines:
            return 0.0
        hits = sum(1 for ln in lines if HEAD_RE.match(ln) or SYSTEM_RE.match(ln))
        ratio = hits / len(lines)
        score = 0.9 * ratio if ratio >= 0.4 else 0.0
        if filename and "whatsapp" in filename.lower():
            score = max(score, 0.7)
        return score

    @staticmethod
    def infer_date_order(text: str) -> str | None:
        """Infer DMY/MDY from the whole file: forced lines first, then chronological monotonicity."""
        triples = []
        for raw in text.splitlines():
            m = HEAD_RE.match(raw) or SYSTEM_RE.match(raw)
            if m and len(m.group("d1")) != 4:
                triples.append((int(m.group("d1")), int(m.group("d2")), int(m.group("d3")), int(m.group("h")), int(m.group("mi"))))
        if not triples:
            return None
        if any(a > 12 for a, _, _, _, _ in triples):
            return "DMY"
        if any(b > 12 for _, b, _, _, _ in triples):
            return "MDY"

        def monotonic(order: str) -> bool:
            prev = None
            for a, b, c, h, mi in triples:
                y = c if c > 99 else 2000 + c
                mo, d = (b, a) if order == "DMY" else (a, b)
                try:
                    t = datetime(y, mo, d, h, mi)
                except ValueError:
                    return False
                if prev and t < prev:
                    return False
                prev = t
            return True

        dmy, mdy = monotonic("DMY"), monotonic("MDY")
        if dmy and not mdy:
            return "DMY"
        if mdy and not dmy:
            return "MDY"
        return None

    def parse(self, text: str, conv_id: str, options: ParseOptions | None = None) -> Conversation:
        options = options or ParseOptions()
        if not options.date_order:
            inferred = self.infer_date_order(text)
            if inferred:
                options = ParseOptions(date_order=inferred, me_name=options.me_name, timezone=options.timezone, extra=options.extra)
        b = Builder(source=self.source, timezone=options.timezone)
        names: list[str] = []
        ambiguous_lines: list[str] = []
        last = None
        for raw in text.splitlines():
            if not raw.strip():
                continue
            m = HEAD_RE.match(raw)
            if m:
                y, mo, d, amb = _resolve_date(m.group("d1"), m.group("d2"), m.group("d3"), options.date_order)
                if amb:
                    ambiguous_lines.append(raw)
                hh = int(m.group("h"))
                ampm = (m.group("ampm") or "").lower()
                if ampm.startswith("p") or ampm == "下午":
                    hh = hh % 12 + 12
                elif ampm.startswith("a") or ampm == "上午":
                    hh = hh % 12
                sec = m.group("s")
                try:
                    ts = datetime(y, mo, d, hh, int(m.group("mi")), int(sec) if sec else 0)
                except ValueError as e:
                    raise ParseError("bad_date", f"无法解析日期：{raw[:40]}", "请检查日期顺序设置。") from e
                prec = TsPrecision.second if sec else TsPrecision.minute
                name = m.group("name").strip().lstrip("‎")
                body = m.group("body")
                if name not in names:
                    names.append(name)
                if len(names) > 2:
                    raise ParseError(
                        "too_many_participants",
                        f"检测到超过两个参与者：{', '.join(names[:3])}",
                        "只支持单聊导出，群聊请先在 WhatsApp 中导出与对方的单聊。",
                    )
                me_name = options.me_name or names[0]
                sender = Sender.me if name == me_name else Sender.other
                typ, body_text = classify_text(body)
                last = b.add(sender, body_text if typ == MsgType.text else None, ts, prec, typ, raw=raw)
                if typ != MsgType.text:
                    last.meta["placeholder"] = body.strip()
                continue
            sm = SYSTEM_RE.match(raw)
            if sm and (looks_system(sm.group("body")) or ":" not in raw.split("]")[-1]):
                y, mo, d, _ = _resolve_date(sm.group("d1"), sm.group("d2"), sm.group("d3"), options.date_order)
                try:
                    ts = datetime(y, mo, d, int(sm.group("h")), int(sm.group("mi")))
                except ValueError:
                    ts = None
                last = b.add(Sender.system, sm.group("body").strip(), ts, TsPrecision.minute, MsgType.system, raw=raw)
                continue
            if last is not None:
                last.text = ((last.text or "") + "\n" + raw.strip()).strip() if last.type == MsgType.text else last.text
                last.raw = (last.raw or "") + "\n" + raw
        if not b.messages:
            raise ParseError("unsupported_format", "没有识别到 WhatsApp 导出格式的行。", "支持 [dd/mm/yyyy, hh:mm:ss] Name: 与 dd/mm/yyyy, hh:mm - Name: 两种格式。")
        if ambiguous_lines and not options.date_order:
            raise ParseError(
                "ambiguous_date",
                f"日期顺序有歧义（例如 {ambiguous_lines[0][:20]}），无法确定是 日/月 还是 月/日。",
                "请选择日期顺序（DMY 或 MDY）后重试。",
            )
        if names:
            me_name = options.me_name or names[0]
            other = next((n for n in names if n != me_name), "对方")
            b.participants = Participants(me=me_name, other=other)
        b.meta["names"] = names
        return b.build(conv_id)
