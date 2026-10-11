"""Bubble layout reconstruction from OCR boxes.

Steps: cluster boxes into lines → classify centered lines as time/system → assign side by x-center →
merge adjacent same-side lines into bubbles → dedupe overlapping consecutive screenshots.
Pure functions; tested with synthetic boxes (no OCR engine needed).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from rapport.ocr.engine import OcrBox
from rapport.parsers.base import Builder, classify_text, looks_system
from rapport.schemas.message_v1 import Conversation, MsgType, Sender, Source, TsPrecision

TIME_RES = [
    (re.compile(r"^(?P<y>\d{4})[年\-/](?P<mo>\d{1,2})[月\-/](?P<d>\d{1,2})日?\s*(?P<h>\d{1,2}):(?P<mi>\d{2})$"), "ymd"),
    (re.compile(r"^(?P<mo>\d{1,2})月(?P<d>\d{1,2})日\s*(?P<ap>上午|下午|晚上|凌晨|中午)?\s*(?P<h>\d{1,2}):(?P<mi>\d{2})$"), "md"),
    (re.compile(r"^(?P<rel>昨天|前天|今天)\s*(?P<ap>上午|下午|晚上|凌晨|中午)?\s*(?P<h>\d{1,2}):(?P<mi>\d{2})$"), "rel"),
    (re.compile(r"^(?P<wd>星期[一二三四五六日天]|周[一二三四五六日天])\s*(?P<ap>上午|下午|晚上|凌晨|中午)?\s*(?P<h>\d{1,2}):(?P<mi>\d{2})$"), "wd"),
    (re.compile(r"^(?P<ap>上午|下午|晚上|凌晨|中午)?\s*(?P<h>\d{1,2}):(?P<mi>\d{2})$"), "hm"),
]
LOW_CONF = 0.6


@dataclass
class Line:
    x0: float
    x1: float
    y0: float
    y1: float
    text: str
    score: float
    side: str | None = None
    bubble_id: str | None = None
    bubble_y0: float | None = None
    bubble_y1: float | None = None

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def h(self) -> float:
        return self.y1 - self.y0


@dataclass
class Bubble:
    side: str  # left | right | center
    kind: str  # msg | time | system
    text: str
    score: float
    y0: float
    y1: float
    ts: datetime | None = None
    ts_precision: TsPrecision = TsPrecision.none
    media: str | None = None  # image | file: a placeholder from pixel evidence, never words read inside it
    bubble_id: str | None = None


@dataclass
class Page:
    name: str
    boxes: list[OcrBox]
    width: int
    height: int
    bubbles: list[Bubble] = field(default_factory=list)
    media: list = field(default_factory=list)  # rapport.ocr.regions.Region: pictures and file cards on this page


MEDIA_TYPES = {"image": MsgType.image, "file": MsgType.file, "transfer": MsgType.transfer, "card": MsgType.unknown}


def cluster_lines(boxes: list[OcrBox]) -> list[Line]:
    """Group boxes with overlapping vertical extent into lines; merge left→right."""
    if not boxes:
        return []
    boxes = sorted(boxes, key=lambda b: (b.cy, b.x0))
    lines: list[list[OcrBox]] = []
    for b in boxes:
        if lines:
            last = lines[-1]
            ref = sum(x.cy for x in last) / len(last)
            ref_h = sum(x.h for x in last) / len(last)
            same_region = all(getattr(x, "bubble_id", None) == getattr(b, "bubble_id", None) for x in last)
            if same_region and abs(b.cy - ref) < 0.5 * max(ref_h, b.h):
                last.append(b)
                continue
        lines.append([b])
    out: list[Line] = []
    for grp in lines:
        grp.sort(key=lambda b: b.x0)
        out.append(
            Line(
                x0=min(b.x0 for b in grp),
                x1=max(b.x1 for b in grp),
                y0=min(b.y0 for b in grp),
                y1=max(b.y1 for b in grp),
                text="".join(b.text for b in grp).strip(),
                score=min(b.score for b in grp),
                side=getattr(grp[0], "side", None),
                bubble_id=getattr(grp[0], "bubble_id", None),
                bubble_y0=getattr(grp[0], "bubble_y0", None),
                bubble_y1=getattr(grp[0], "bubble_y1", None),
            )
        )
    return out


def parse_time(text: str, now: datetime | None = None) -> tuple[datetime | None, TsPrecision, str | None]:
    """Return (datetime, precision, warning)."""
    now = now or datetime.now()
    t = text.replace("：", ":").strip()
    for rx, kind in TIME_RES:
        m = rx.match(t)
        if not m:
            continue
        h, mi = int(m.group("h")), int(m.group("mi"))
        ap = m.groupdict().get("ap")
        if ap in ("下午", "晚上") and h < 12:
            h += 12
        if ap == "凌晨" and h == 12:
            h = 0
        try:
            if kind == "ymd":
                return datetime(int(m.group("y")), int(m.group("mo")), int(m.group("d")), h, mi), TsPrecision.minute, None
            if kind == "md":
                return datetime(now.year, int(m.group("mo")), int(m.group("d")), h, mi), TsPrecision.minute, "year_assumed"
            if kind == "rel":
                off = {"今天": 0, "昨天": 1, "前天": 2}[m.group("rel")]
                d = now - timedelta(days=off)
                return datetime(d.year, d.month, d.day, h, mi), TsPrecision.minute, "relative_day"
            if kind == "wd":
                return None, TsPrecision.none, "weekday_only"
            if kind == "hm":
                return None, TsPrecision.none, "time_only"
        except ValueError:
            return None, TsPrecision.none, "bad_time"
    return None, TsPrecision.none, None


def lines_to_bubbles(lines: list[Line], width: int, now: datetime | None = None, media=()) -> tuple[list[Bubble], list[str]]:
    warnings: list[str] = []
    bubbles: list[Bubble] = []
    events = sorted([(ln.y0, 0, ln) for ln in lines] + [(region.y0, 1, region) for region in media], key=lambda e: (e[0], e[1]))
    for _, is_media, item in events:
        if is_media:
            # A picture or file card stands on its own: text above or below never merges into it.
            bubbles.append(Bubble(item.side, "msg", item.placeholder, 1.0, item.y0, item.y1, media=item.kind))
            continue
        ln = item
        rel_cx = ln.cx / width
        rel_w = (ln.x1 - ln.x0) / width
        centered = not ln.side and 0.35 < rel_cx < 0.65 and rel_w < 0.55
        if centered:
            ts, prec, warn = parse_time(ln.text, now)
            if ts is not None or warn in ("weekday_only", "time_only"):
                if warn:
                    warnings.append(warn)
                bubbles.append(Bubble("center", "time", ln.text, ln.score, ln.y0, ln.y1, ts, prec))
                continue
            if looks_system(ln.text) or len(ln.text) <= 2:
                bubbles.append(Bubble("center", "system", ln.text, ln.score, ln.y0, ln.y1))
                continue
        # attach to previous bubble if same side and close vertically (multi-line bubble)
        side = ln.side or ("left" if rel_cx < 0.5 else "right")
        if not ln.side and rel_w > 0.7:  # long line spanning; decide by left edge
            side = "left" if ln.x0 / width < 0.15 else "right"
        same_region = bool(bubbles and ln.bubble_id and bubbles[-1].bubble_id == ln.bubble_id)
        legacy_near = bool(bubbles and not ln.bubble_id and not bubbles[-1].bubble_id
                           and (ln.y0 - bubbles[-1].y1) < 0.9 * ln.h)
        if bubbles and bubbles[-1].kind == "msg" and bubbles[-1].media is None and bubbles[-1].side == side and (same_region or legacy_near):
            b = bubbles[-1]
            b.text += ln.text
            b.y1 = max(b.y1, ln.bubble_y1 if ln.bubble_y1 is not None else ln.y1)
            b.score = min(b.score, ln.score)
            continue
        bubbles.append(Bubble(side, "msg", ln.text, ln.score,
                              ln.bubble_y0 if ln.bubble_y0 is not None else ln.y0,
                              ln.bubble_y1 if ln.bubble_y1 is not None else ln.y1,
                              bubble_id=ln.bubble_id))
    return bubbles, warnings


def dedupe_pages(pages: list[Page], k: int = 3) -> int:
    """Remove overlap between consecutive pages: if the last k message texts of page i equal the first k of page i+1
    (as a contiguous run), drop those from page i+1. Only the overlapping run is dropped."""
    removed = 0
    for prev, cur in zip(pages, pages[1:], strict=False):
        p_msgs = [b for b in prev.bubbles if b.kind == "msg"]
        c_msgs = [b for b in cur.bubbles if b.kind == "msg"]
        if not p_msgs or not c_msgs:
            continue
        best = 0
        for n in range(min(k + 2, len(p_msgs), len(c_msgs)), 0, -1):
            if [b.text for b in p_msgs[-n:]] == [b.text for b in c_msgs[:n]] and n >= min(k, len(c_msgs)):
                best = n
                break
        if best:
            drop = set(id(b) for b in c_msgs[:best])
            # also drop time/system lines above the first kept message
            first_kept_y = c_msgs[best].y0 if best < len(c_msgs) else float("inf")
            cur.bubbles = [b for b in cur.bubbles if id(b) not in drop and not (b.kind != "msg" and b.y1 <= first_kept_y and b.y0 < c_msgs[0].y0)]
            removed += best
    return removed


def pages_to_conversation(pages: list[Page], conv_id: str, timezone: str = "Asia/Shanghai", me_side: str = "right", now: datetime | None = None) -> Conversation:
    all_warn: set[str] = set()
    for p in pages:
        lines = cluster_lines(p.boxes)
        p.bubbles, w = lines_to_bubbles(lines, p.width, now, p.media)
        all_warn.update(w)
    removed = dedupe_pages(pages)
    b = Builder(source=Source.ocr, timezone=timezone)
    cur_ts: datetime | None = None
    cur_prec = TsPrecision.none
    n_low = 0
    low_ids: list[str] = []
    for p in pages:
        for bub in p.bubbles:
            if bub.kind == "time":
                if bub.ts is not None:
                    cur_ts, cur_prec = bub.ts, bub.ts_precision
                continue
            if bub.kind == "system":
                b.add(Sender.system, bub.text, cur_ts, cur_prec, MsgType.system, meta={"page": p.name})
                continue
            if bub.side == "center":
                sender = Sender.unknown
            elif (bub.side == "right") == (me_side == "right"):
                sender = Sender.me
            else:
                sender = Sender.other
            typ, text = classify_text(bub.text)
            meta = {"page": p.name, "ocr_confidence": round(bub.score, 3), "side": bub.side}
            if bub.media:
                typ, text = MEDIA_TYPES[bub.media], None
                meta["placeholder"] = bub.text
            if bub.score < LOW_CONF:
                sender = Sender.unknown
            m = b.add(sender, text if typ == MsgType.text else None, cur_ts, cur_prec, typ, meta=meta)
            if bub.score < LOW_CONF:
                n_low += 1
                low_ids.append(m.id)
    if n_low:
        b.warn("low_confidence", f"{n_low} 条识别置信度低于 {LOW_CONF}，已标为未知发送者，请在校正页确认。", low_ids)
    if removed:
        b.warn("dedup", f"相邻截图重叠，已去重 {removed} 条。")
    if "relative_day" in all_warn:
        b.warn("relative_day", "截图时间为“昨天/前天”等相对日期，已按当前日期换算，可能不准确。")
    if "year_assumed" in all_warn:
        b.warn("year_assumed", "截图时间缺少年份，已假定为今年。")
    if "time_only" in all_warn or "weekday_only" in all_warn:
        b.warn("time_only", "部分时间标记只有时:分或星期，无法确定日期，相关消息时间置空。")
    if not b.messages:
        from rapport.parsers.base import ParseError

        raise ParseError("ocr_empty", "截图中没有识别到聊天内容。", "请确认是清晰的聊天界面截图。")
    return b.build(conv_id)
