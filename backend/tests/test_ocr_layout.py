from __future__ import annotations

from datetime import datetime

from rapport.ocr.engine import OcrBox
from rapport.ocr.layout import (
    Page,
    cluster_lines,
    dedupe_pages,
    lines_to_bubbles,
    pages_to_conversation,
    parse_time,
)
from rapport.schemas.message_v1 import Sender

W, H = 1080, 2000


def box(text, side, y, score=0.95, w=300):
    x0 = 80 if side == "left" else (W - 80 - w) if side == "right" else (W - w) / 2
    return OcrBox(x0, y, x0 + w, y + 40, text, score)


def test_cluster_lines_merges_same_row():
    boxes = [OcrBox(100, 100, 200, 140, "你好", 0.9), OcrBox(210, 102, 300, 138, "呀", 0.9), OcrBox(100, 200, 200, 240, "在吗", 0.9)]
    lines = cluster_lines(boxes)
    assert [ln.text for ln in lines] == ["你好呀", "在吗"]


def test_parse_time_variants():
    now = datetime(2026, 9, 14, 12, 0)
    assert parse_time("2026年9月1日 20:15")[0] == datetime(2026, 9, 1, 20, 15)
    assert parse_time("9月1日 下午8:15", now)[0] == datetime(2026, 9, 1, 20, 15)
    ts, _, w = parse_time("昨天 20:15", now)
    assert ts == datetime(2026, 9, 13, 20, 15) and w == "relative_day"
    assert parse_time("20:15")[2] == "time_only"
    assert parse_time("哈哈哈")[0] is None


def test_bubbles_sides_time_and_system():
    boxes = [box("9月1日 20:15", "center", 50, w=200), box("你好呀", "left", 150), box("你好", "right", 250), box("你已添加了小红", "center", 350, w=400), box("在吗", "right", 450), box("第二行", "right", 495)]
    bubbles, _ = lines_to_bubbles(cluster_lines(boxes), W)
    kinds = [(b.kind, b.side, b.text) for b in bubbles]
    assert kinds[0] == ("time", "center", "9月1日 20:15")
    assert kinds[1] == ("msg", "left", "你好呀")
    assert kinds[3][0] == "system"
    assert kinds[4] == ("msg", "right", "在吗第二行")  # merged multi-line bubble


def _page(name, texts_sides, start_y=100):
    boxes = []
    y = start_y
    for t, s in texts_sides:
        boxes.append(box(t, s, y))
        y += 120
    return Page(name, boxes, W, H)


def test_dedupe_overlap_but_keep_real_duplicates():
    p1 = _page("1", [("早", "left"), ("哈哈", "right"), ("哈哈", "right"), ("吃了吗", "left"), ("吃了", "right")])
    p2 = _page("2", [("哈哈", "right"), ("吃了吗", "left"), ("吃了", "right"), ("那走吧", "left")])
    conv = pages_to_conversation([p1, p2], "c", me_side="right")
    texts = [m.text for m in conv.messages]
    assert texts == ["早", "哈哈", "哈哈", "吃了吗", "吃了", "那走吧"]
    assert any(w.code == "dedup" for w in conv.warnings)


def test_conversation_assignment_and_low_confidence():
    p = Page("1", [box("9月1日 20:15", "center", 40, w=200), box("你好", "left", 150), box("嗨", "right", 300), box("模糊", "left", 450, score=0.3)], W, H)
    conv = pages_to_conversation([p], "c", me_side="right", now=datetime(2026, 9, 14))
    assert [m.sender for m in conv.messages] == [Sender.other, Sender.me, Sender.unknown]
    assert conv.messages[0].ts == datetime(2026, 9, 1, 20, 15)
    assert any(w.code == "low_confidence" for w in conv.warnings)
    conv2 = pages_to_conversation([Page("1", [box("你好", "left", 150), box("嗨", "right", 300)], W, H)], "c", me_side="left")
    assert conv2.messages[0].sender == Sender.me


def test_dedupe_no_false_positive_short_pages():
    p1 = _page("1", [("a", "left")])
    p2 = _page("2", [("b", "right")])
    assert dedupe_pages([p1, p2]) == 0
