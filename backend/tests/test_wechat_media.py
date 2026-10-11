"""Pictures, file cards and avatars on a WeChat page: pixels decide, placeholders stay, inner words never count.

Every page here is drawn synthetically; OCR boxes are placed where a recogniser would put them.
"""

from __future__ import annotations

import threading
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from rapport.desktop import wechat_native as native
from rapport.desktop.wechat import context_digest, to_conversation
from rapport.desktop.wechat_history import HistoryAssembler, agreed_shift
from rapport.ocr.engine import OcrBox
from rapport.ocr.layout import Page
from rapport.schemas.message_v1 import MsgType, Sender

np = pytest.importorskip("numpy")
pytest.importorskip("cv2")
Image = pytest.importorskip("PIL.Image")
ImageDraw = pytest.importorskip("PIL.ImageDraw")

W, H = 1400, 760
BACKGROUND = (250, 250, 250)
GREEN, WHITE, CARD = (149, 236, 105), (255, 255, 255), (238, 238, 240)


def glyphs(draw, x0, y0, x1, y1, colour=(20, 20, 20)):
    """Dark strokes standing in for characters inside a text box."""
    for x in range(int(x0), int(x1) - 4, 9):
        draw.rectangle((x, y0 + 3, x + 5, y1 - 3), fill=colour)


def avatar(draw, x, y, seed):
    rng = np.random.default_rng(seed)
    for i in range(0, 52, 4):
        for j in range(0, 52, 4):
            draw.rectangle((x + i, y + j, x + i + 3, y + j + 3), fill=tuple(int(v) for v in rng.integers(0, 256, 3)))


def bubble(draw, boxes, side, y, text, colour):
    width = 18 * len(text)
    x0 = 100 if side == "left" else W - 100 - width - 28
    draw.rounded_rectangle((x0, y, x0 + width + 28, y + 52), radius=8, fill=colour)
    box = OcrBox(x0 + 14, y + 13, x0 + 14 + width, y + 39, text, .98)
    glyphs(draw, box.x0, box.y0, box.x1, box.y1)
    boxes.append(box)
    avatar(draw, 28 if side == "left" else W - 80, y, len(boxes))


def phone_screenshot(draw, boxes, x0, y0, rows=12):
    """A sent picture of an app screen: white, rules and coloured icons, rows of small words in two columns."""
    x1, y1 = x0 + 158, y0 + 26 + rows * 26
    draw.rectangle((x0, y0, x1, y1), fill=WHITE)
    draw.rectangle((x0, y0, x1, y0 + 18), fill=(30, 30, 30))
    for row in range(rows):
        y = y0 + 26 + row * 26
        draw.rectangle((x0 + 4, y + 22, x1 - 4, y + 23), fill=(230, 230, 230))
        draw.ellipse((x1 - 14, y + 4, x1 - 6, y + 12), fill=(66, 133, 244))
        left, right = OcrBox(x0 + 4, y + 2, x0 + 60, y + 15, f"项目{row}", .7), OcrBox(x0 + 110, y + 2, x0 + 140, y + 15, "0.00", .9)
        for box in (left, right):
            glyphs(draw, box.x0, box.y0, box.x1, box.y1, (90, 90, 90))
            boxes.append(box)
    return x0, y0, x1, y1


def file_card(draw, boxes, y0, name="季度报告.docx", size="467.8K"):
    draw.rounded_rectangle((93, y0, 452, y0 + 123), radius=8, fill=CARD)
    draw.rectangle((392, y0 + 18, 432, y0 + 70), fill=(19, 126, 226))
    title = OcrBox(109, y0 + 19, 309, y0 + 45, name, .99)
    weight = OcrBox(109, y0 + 51, 175, y0 + 76, size, .99)
    footer = OcrBox(134, y0 + 90, 232, y0 + 118, "微信电脑版", .99)
    for box in (title, weight, footer):
        glyphs(draw, box.x0, box.y0, box.x1, box.y1, (120, 120, 120))
        boxes.append(box)
    avatar(draw, 28, y0, 99)


def photo(draw, x0, y0, w=180, h=240, seed=5):
    rng = np.random.default_rng(seed)
    for i in range(0, w, 6):
        for j in range(0, h, 6):
            draw.rectangle((x0 + i, y0 + j, x0 + i + 5, y0 + j + 5), fill=tuple(int(v) for v in rng.integers(0, 256, 3)))


def sample_page():
    image = Image.new("RGB", (W, H), BACKGROUND)
    draw = ImageDraw.Draw(image)
    boxes: list[OcrBox] = []
    time_label = OcrBox(640, 12, 760, 35, "9月16日 23:23", .98)
    glyphs(draw, time_label.x0, time_label.y0, time_label.x1, time_label.y1, (160, 160, 160))
    boxes.append(time_label)
    bubble(draw, boxes, "left", 50, "明天几点见面", WHITE)
    bubble(draw, boxes, "right", 120, "下午三点可以吗", GREEN)
    picture = phone_screenshot(draw, boxes, W - 100 - 158, 190)
    avatar(draw, W - 80, 190, 7)
    photo(draw, 100, 540, h=160)
    avatar(draw, 28, 540, 8)
    return image, boxes, picture


def test_picture_text_and_avatars_are_dropped_and_become_placeholders():
    from rapport.ocr import regions

    image, boxes, picture = sample_page()
    kept, found, removed = regions.split(image, boxes)
    assert sorted(box.text for box in kept) == sorted(["9月16日 23:23", "明天几点见面", "下午三点可以吗"])
    assert removed == 24, "every word inside the phone screenshot"
    assert [(r.kind, r.side) for r in sorted(found, key=lambda r: r.y0)] == [("image", "right"), ("image", "left")]
    shot = min(found, key=lambda r: r.y0)
    assert abs(shot.x0 - picture[0]) <= 2 and abs(shot.y1 - picture[3]) <= 2


def test_file_card_keeps_only_its_name_as_a_placeholder():
    from rapport.ocr import regions

    image = Image.new("RGB", (W, H), BACKGROUND)
    draw = ImageDraw.Draw(image)
    boxes: list[OcrBox] = []
    bubble(draw, boxes, "left", 40, "收到了吗", WHITE)
    file_card(draw, boxes, 200)
    kept, found, removed = regions.split(image, boxes)
    assert [box.text for box in kept] == ["收到了吗"]
    assert removed == 3
    (card,) = found
    assert (card.kind, card.side, card.placeholder) == ("file", "left", "[文件] 季度报告.docx")


def test_plain_bubbles_are_untouched():
    from rapport.ocr import regions

    image = Image.new("RGB", (W, H), BACKGROUND)
    draw = ImageDraw.Draw(image)
    boxes: list[OcrBox] = []
    for i in range(6):
        bubble(draw, boxes, "left" if i % 2 else "right", 30 + i * 110, "这是一条普通的消息" + str(i), WHITE if i % 2 else GREEN)
    kept, found, removed = regions.split(image, list(boxes))
    # Segmentation now enriches plain text with bubble ownership, preserving OCR.
    fields = ("x0", "y0", "x1", "y1", "text", "score")
    assert [tuple(getattr(b, k) for k in fields) for b in kept] == [tuple(getattr(b, k) for k in fields) for b in boxes]
    assert [b.side for b in kept] == ["right", "left"] * 3
    assert not found and removed == 0


def test_history_keeps_placeholders_in_order_but_counts_and_trims_by_text():
    image, boxes, _ = sample_page()
    from rapport.ocr import regions

    kept, found, removed = regions.split(image, boxes)
    page = Page("p", kept, W, H, media=found)
    page.media_removed = removed
    page.image = image
    assembler = HistoryAssembler()
    assembler.prepend(page)
    assert assembler.count == 2
    conv = assembler.build("合成好友", 1, "target_reached")
    kinds = [(m.type, m.sender, m.text, m.meta.get("placeholder")) for m in conv.messages]
    assert kinds == [(MsgType.text, Sender.me, "下午三点可以吗", None),
                     (MsgType.image, Sender.me, None, "[图片]"),
                     (MsgType.image, Sender.other, None, "[图片]")], "trimmed to the newest text message, placeholders after it kept"
    stats = conv.meta["wechat_history"]
    assert (stats["collected"], stats["me"], stats["other"]) == (1, 1, 0)
    assert stats["media"] == {"image": 2, "file": 0, "transfer": 0} and stats["media_text_removed"] == 24


def test_single_frame_placeholders_do_not_change_the_text_context_digest():
    image, boxes, _ = sample_page()
    from rapport.ocr import regions

    kept, found, _ = regions.split(image, boxes)
    with_media = to_conversation(kept, W, H, "合成好友", media=found)
    text_only = to_conversation(kept, W, H, "合成好友")
    assert [m.type for m in with_media.messages].count(MsgType.image) == 2
    assert context_digest(with_media) == context_digest(text_only), "a sent picture is not a changed text context"


@pytest.mark.parametrize(("offsets", "shift"), [
    ([60], 60), ([60, 61, 60], 60), ([60, 60, 61, 120], 60), ([60, 120], None), ([60, 61, 120, 121], None), ([], None),
])
def test_shift_needs_a_majority_of_textured_bands(offsets, shift):
    assert agreed_shift(offsets) == shift


@pytest.mark.parametrize(("ticks", "shift", "expected"), [(2, 80, 7), (2, 400, 1), (1, 60, 4), (2, 20, 8), (2, None, 2)])
def test_scroll_step_grows_to_about_forty_percent_of_the_view(ticks, shift, expected):
    assert native.next_ticks(ticks, shift, 700) == expected


def fake_desktop(monkeypatch, frames):
    root = SimpleNamespace(NativeWindowHandle=100, ProcessId=200)
    auto = SimpleNamespace(GetRootControl=lambda: SimpleNamespace(GetChildren=lambda: [root]))
    monkeypatch.setattr(native, "automation", lambda: nullcontext(auto))
    monkeypatch.setattr(native, "process_name", lambda _: "weixin.exe")
    monkeypatch.setattr(native.navigation, "open_contact", lambda *args: (root, None))
    from rapport.ocr import engine

    monkeypatch.setattr(engine, "available", lambda: True)
    monkeypatch.setattr(engine, "last_device", lambda: "GPU · DirectML")
    img = Image.new("RGB", (850, 700))
    first = next(frames)
    monkeypatch.setattr(native, "seek_latest", lambda *args: (img, first))
    monkeypatch.setattr(native, "scroll_chat", lambda *args, **kwargs: None)
    monkeypatch.setattr(native, "read_chat", lambda *args, **kwargs: (img, next(frames), None))


def text_page(start, tag):
    boxes = [OcrBox(40 if n % 2 else 620, 20 + i * 60, 200 if n % 2 else 780, 40 + i * 60, f"合成测试消息 {n:04d}", .99)
             for i, n in enumerate(range(start, start + 10))]
    page = Page(str(start), boxes, 850, 640)
    page.visual_hash = tag
    return page


def test_history_end_is_found_by_text_when_an_animation_keeps_changing_pixels(monkeypatch):
    frames = iter([text_page(990, "a0")] + [text_page(990, f"animated-{i}") for i in range(3)])
    fake_desktop(monkeypatch, frames)
    cancel = threading.Event()
    monkeypatch.setattr(cancel, "wait", lambda _: False)
    progress = []
    result = native.capture_history("合成好友", 20_000_000, 1000, cancel, lambda **kw: progress.append(kw))
    stats = result.conversation.meta["wechat_history"]
    assert stats["stop_reason"] == "no_more_loaded_history" and stats["collected"] == 10
    assert stats["ocr_devices"] == {"GPU · DirectML": 4}
    assert [p for p in progress if p["phase"] == "reading"][-1]["device"] == "GPU · DirectML"


def test_failed_big_step_rolls_back_to_one_notch_from_the_last_joined_frame(monkeypatch):
    scrolls, grown = [], []
    # 985 joins the latest frame; 975 is too far from 985; 980 (one notch back) joins; 975 then joins 980.
    frames = iter([text_page(990, "latest"), text_page(985, "a"), text_page(975, "too-far"), text_page(980, "b"),
                   text_page(975, "c")])
    fake_desktop(monkeypatch, frames)
    monkeypatch.setattr(native, "scroll_chat", lambda *args, **kwargs: scrolls.append((args[3], args[4])))

    def next_ticks(ticks, shift, height):
        grown.append(ticks)
        return 3 if len(grown) == 1 else ticks  # the first proven join lengthens the step to 3 notches

    monkeypatch.setattr(native, "next_ticks", next_ticks)
    result = native.capture_history("合成好友", 20_000_000, 25, threading.Event(), lambda **kw: None)
    assert scrolls == [("up", 2), ("up", 3), ("down", 2), ("up", 1)], "three notches failed: back two, then one at a time"
    stats = result.conversation.meta["wechat_history"]
    assert stats["collected"] == 25 and stats["stop_reason"] == "target_reached"
