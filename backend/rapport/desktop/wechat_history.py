"""Conservative visual page stitching and timestamp provenance for scroll capture."""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from statistics import median

from rapport.desktop.wechat import normalized_title
from rapport.errors import RapportError
from rapport.ocr.layout import MEDIA_TYPES, Bubble, Page, cluster_lines, lines_to_bubbles
from rapport.parsers.base import Builder, classify_text
from rapport.schemas.message_v1 import MsgType, Sender, Source, TsPrecision


def signature(bubble):
    return bubble.kind, bubble.side, normalized_title(bubble.text)


def visual_shift(previous, current):
    """Confirm a vertical screen overlap using textured bands, never blank space.

    Returns how far the old viewport moved down during an upward scroll. Images
    stay in memory; no message accessibility text is involved.
    """
    if previous is None or current is None or previous.size != current.size:
        return None
    import cv2
    import numpy as np

    old = cv2.cvtColor(np.asarray(previous), cv2.COLOR_RGB2GRAY)
    new = cv2.cvtColor(np.asarray(current), cv2.COLOR_RGB2GRAY)
    h, w = old.shape
    band = max(24, min(80, h // 6))
    offsets = []
    # Eight bands, not four: a long bubble or a flat picture can leave most bands untextured.
    for y in sorted({5, h // 10, h // 7, h // 5, h // 3, 2 * h // 5, h // 2, 3 * h // 5}):
        template = old[y:y + band, 8:w - 8]
        if float(template.std()) < 7 or y + band >= h:
            continue
        scores = cv2.matchTemplate(new[:, 8:w - 8], template, cv2.TM_CCOEFF_NORMED)[:, 0]
        # Only positive displacement is compatible with scrolling to older history.
        scores[:y + 3] = -1
        best = int(scores.argmax())
        score = float(scores[best])
        candidates = scores.copy()
        candidates[max(0, best - 3):best + 4] = -1
        if score >= .985 and float(candidates.max()) < score - .02:
            offsets.append(best - y)
    return agreed_shift(offsets)


def agreed_shift(offsets):
    """The shift most bands agree on (within 2 px), or None.

    That group must outnumber all the other bands together: two bands that disagree, or no
    majority at all, prove nothing. A single textured band is accepted as before.
    """
    if not offsets:
        return None
    groups = [[v for v in offsets if abs(v - value) <= 2] for value in offsets]
    best = max(groups, key=len)
    if len(offsets) > 1 and (len(best) < 2 or len(best) <= len(offsets) - len(best)):
        return None
    return int(median(best))


WEEKDAYS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


def label_time(text: str, now: datetime) -> tuple[datetime | None, bool]:
    """Anchor a WeChat time label to the capture moment: (time, is a full calendar date).

    WeChat shows HH:MM for today, 昨天 HH:MM for yesterday, a weekday within the past week,
    M月D日 earlier this year and a full date before that. A label that cannot be anchored
    consistently (a weekday that would be today or yesterday, a time after the capture) is None.
    """
    from rapport.ocr.layout import TIME_RES

    value = text.replace("：", ":").strip()
    for pattern, kind in TIME_RES:
        match = pattern.match(value)
        if not match:
            continue
        hour, minute = int(match.group("h")), int(match.group("mi"))
        period = match.groupdict().get("ap")
        if period in ("下午", "晚上") and hour < 12:
            hour += 12
        if period == "凌晨" and hour == 12:
            hour = 0
        day = now.date()
        try:
            if kind == "ymd":
                return datetime(int(match.group("y")), int(match.group("mo")), int(match.group("d")), hour, minute), True
            if kind == "md":
                result = datetime(now.year, int(match.group("mo")), int(match.group("d")), hour, minute)
                return (result if result <= now + timedelta(days=1) else result.replace(year=now.year - 1)), False
            if kind == "rel":
                day -= timedelta(days={"今天": 0, "昨天": 1, "前天": 2}[match.group("rel")])
            elif kind == "wd":
                back = (now.weekday() - WEEKDAYS[match.group("wd")[-1]]) % 7
                if back < 2:
                    return None, False
                day -= timedelta(days=back)
            result = datetime(day.year, day.month, day.day, hour, minute)
        except ValueError:
            return None, False
        return (result if result <= now + timedelta(minutes=10) else None), False
    return None, False


class HistoryAssembler:
    def __init__(self, now=None):
        self.now = now or datetime.now()
        self.rows: list[Bubble] = []
        self.last: list[Bubble] = []
        self.pages = 0
        self.removed = 0
        self.last_image = None
        self.last_shift = None  # how far the previous join moved, when pixels proved it
        self.media_text_removed = 0

    @property
    def count(self):
        """Text messages only: pictures and files never count towards the target."""
        return sum(b.kind == "msg" and not b.media for b in self.rows)

    def prepend(self, page: Page):
        rows, _ = lines_to_bubbles(cluster_lines(page.boxes), page.width, self.now, getattr(page, "media", ()))
        # Edge-clipped text is not a complete message. It will be read on the
        # next overlapping frame; a long bubble without a safe join stops capture.
        rows = [b for b in rows if b.y0 > 2 and b.y1 < page.height - 2
                and (b.kind != "msg" or b.media or classify_text(b.text)[0] == MsgType.text)]
        image = getattr(page, "image", None)
        shift = visual_shift(self.last_image, image)
        if not rows and shift is None:
            raise RapportError("wechat_history_no_text", "这一段未识别到完整文字，已暂停拼接。", status=409)
        if not self.rows:
            self.rows = rows
        elif shift is not None:
            prefix = [b for b in rows if b.y0 < shift + 2]
            self.rows = prefix + self.rows
            self.removed += sum(b.kind == "msg" for b in rows if b.y0 >= shift + 2)
        else:
            older = [b for b in rows if b.kind == "msg"]
            newer = [b for b in self.last if b.kind == "msg"]
            overlaps = []
            for n in range(1, min(len(older), len(newer)) + 1):
                a, b = older[-n:], newer[:n]
                if [signature(x) for x in a] != [signature(x) for x in b]:
                    continue
                shifts = [x.y0 - y.y0 for x, y in zip(a, b, strict=True)]
                if min(shifts) <= 2 or max(shifts) - min(shifts) > 12:
                    continue
                # Repeated short replies are not evidence of the same messages.
                if len({signature(x) for x in a}) < 2:
                    if n != 1 or len(normalized_title(a[0].text)) < 8:
                        continue
                    if sum(signature(x) == signature(a[0]) for x in older + newer) != 2:
                        continue
                overlaps.append(n)
            if not overlaps:
                raise RapportError("wechat_history_overlap", "无法可靠拼接这两段聊天，已保留此前读取的内容。",
                                   "可能是图片、超长消息、重复短句或窗口变化；不会猜测或重复累计。", 409)
            n = max(overlaps)
            first = older[-n]
            cut = next(i for i, row in enumerate(rows) if row is first)
            prefix = rows[:cut]
            # Drop repeated time/system rows at the join, not identical messages
            # elsewhere in the conversation. No global text-based deduplication.
            while prefix and self.rows and signature(prefix[-1]) == signature(self.rows[0]) and prefix[-1].kind != "msg":
                prefix.pop()
            self.rows = prefix + self.rows
            self.removed += n
        self.last = rows
        self.last_image = image
        self.last_shift = shift
        self.media_text_removed += getattr(page, "media_removed", 0)
        self.pages += 1

    def build(self, contact, target, reason):
        builder = Builder(source=Source.ocr, timezone="Asia/Shanghai")
        group = None
        label, full_date, first = None, False, False
        for row in self.rows:
            if row.kind == "time":
                group = row.text
                label, full_date = label_time(row.text, self.now) if row.score >= .7 else (None, False)
                first = True
                continue
            if row.kind != "msg":
                continue
            typ, text = classify_text(row.text)
            if row.media:
                typ, text = MEDIA_TYPES[row.media], None
            sender = Sender.me if row.side == "right" else Sender.other if row.side == "left" else Sender.unknown
            if row.score < .6:
                sender = Sender.unknown
            # WeChat prints a time only above the first bubble after a pause. Only a complete
            # calendar date on that first bubble is an exact minute; every other bubble keeps the
            # label's time as "label" precision: good for order and date, never for intervals.
            precision = (TsPrecision.none if label is None else TsPrecision.minute if full_date and first else TsPrecision.label)
            meta = {"ocr_confidence": round(row.score, 3), "time_label": group, "side": row.side,
                    "time_source": {TsPrecision.minute: "visible_label", TsPrecision.label: "time_label"}.get(precision,
                                                                                                          "group_only" if group else "unknown")}
            if row.media:
                meta["placeholder"] = row.text  # from pixels; words inside pictures are never kept
            builder.add(sender, text if typ == MsgType.text else None, label, precision, typ, meta=meta)
            first = False
        conv = builder.build(secrets.token_hex(8))
        text_at = [i for i, message in enumerate(conv.messages) if message.type == MsgType.text]
        if len(text_at) > target:
            conv.messages = conv.messages[text_at[-target]:]
        for seq, message in enumerate(conv.messages):
            message.seq, message.id = seq, f"m{seq:06d}"
        conv.participants.other = contact
        builder_warning = "只统计视觉识别到的文字消息；图片、文件显示为占位，不计入条数，其中的文字不作为聊天内容。时间标签不等于每条消息的发送时间，缺失时间不参与间隔统计。"
        from rapport.schemas.message_v1 import ParseWarning

        conv.warnings.append(ParseWarning(code="visual_history", msg=builder_warning))
        texts = [m for m in conv.messages if m.type == MsgType.text]
        if reason != "target_reached":
            conv.warnings.append(ParseWarning(code="history_partial", msg=f"目标 {target} 条，实际读取 {len(texts)} 条；本次读取未达到目标。"))
        intervals = []
        for before, after in zip(conv.messages, conv.messages[1:], strict=False):
            if before.has_reliable_ts and after.has_reliable_ts:
                delta = (after.ts - before.ts).total_seconds()
                if delta >= 0:
                    intervals.append(delta)
        stats = {"requested": target, "collected": len(texts), "pages": self.pages,
                 "overlap_removed": self.removed, "stop_reason": reason,
                 "me": sum(m.sender == Sender.me for m in texts),
                 "other": sum(m.sender == Sender.other for m in texts),
                 "unknown": sum(m.sender == Sender.unknown for m in texts),
                 "text_characters": sum(len(m.text or "") for m in texts),
                 "known_times": sum(m.has_reliable_ts for m in texts),
                 "label_times": sum(m.ts_precision == TsPrecision.label for m in texts),
                 "media": {"image": sum(m.type == MsgType.image for m in conv.messages),
                           "file": sum(m.type == MsgType.file for m in conv.messages),
                           "transfer": sum(m.type == MsgType.transfer for m in conv.messages)},
                 "media_text_removed": self.media_text_removed,
                 "interval_pairs": len(intervals), "median_interval_seconds": median(intervals) if intervals else None}
        conv.meta["wechat_history"] = stats
        return conv
