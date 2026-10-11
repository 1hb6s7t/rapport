import threading
import time
from contextlib import nullcontext
from datetime import datetime
from types import SimpleNamespace

import pytest

from rapport.desktop import wechat_native as native
from rapport.desktop.wechat_history import HistoryAssembler, label_time, visual_shift
from rapport.desktop.wechat_jobs import HistoryJobs
from rapport.errors import RapportError
from rapport.ocr.engine import OcrBox
from rapport.ocr.layout import Page
from rapport.schemas.message_v1 import TsPrecision


def page(start, size=10):
    boxes = [OcrBox(40 if n % 2 else 620, 20 + i * 60, 200 if n % 2 else 780, 40 + i * 60,
                    f"这是一条合成测试消息 {n:04d}", .99) for i, n in enumerate(range(start, start + size))]
    p = Page(str(start), boxes, 850, 640)
    p.visual_hash = str(start)
    return p


def test_thousand_messages_stitched_trimmed_and_ordered_without_duplicates():
    assembler = HistoryAssembler()
    for start in range(990, -1, -5):
        assembler.prepend(page(start))
    assert assembler.count == 1000
    conv = assembler.build("合成好友", 1000, "target_reached")
    assert len(conv.messages) == 1000
    assert conv.messages[0].text.endswith("0000")
    assert conv.messages[-1].text.endswith("0999")
    assert len({m.id for m in conv.messages}) == 1000
    assert conv.meta["wechat_history"]["overlap_removed"] == 990
    assert not any(m.ts for m in conv.messages)


def test_keeps_same_text_at_distinct_positions_and_refuses_unproven_join():
    assembler = HistoryAssembler()
    newer = page(5)
    newer.boxes[-1].text = newer.boxes[0].text
    assembler.prepend(newer)
    assembler.prepend(page(0))
    assert assembler.count == 15
    assert sum(b.text.endswith("0005") for b in assembler.rows) == 2
    with pytest.raises(RapportError):
        assembler.prepend(page(100))
    assert assembler.count == 15


def test_time_labels_date_messages_without_inventing_exact_times():
    p = Page("time", [
        OcrBox(340, 10, 500, 30, "2026-09-22 10:00", .99),
        OcrBox(40, 55, 200, 75, "第一条", .99),
        OcrBox(620, 110, 780, 130, "第二条", .99),
        OcrBox(340, 160, 500, 180, "昨天 11:00", .99),
        OcrBox(40, 210, 200, 230, "第三条", .99),
    ], 850, 300)
    a = HistoryAssembler(now=datetime(2026, 9, 27, 9, 0))
    a.prepend(p)
    conv = a.build("合成好友", 10, "no_more_loaded_history")
    first, second, third = conv.messages
    assert (first.ts, first.ts_precision) == (datetime(2026, 9, 22, 10, 0), TsPrecision.minute)
    assert (second.ts, second.ts_precision) == (datetime(2026, 9, 22, 10, 0), TsPrecision.label), "the label's time, not its own minute"
    assert (third.ts, third.ts_precision) == (datetime(2026, 9, 26, 11, 0), TsPrecision.label), "昨天 is anchored to the capture day"
    assert not second.has_reliable_ts and not third.has_reliable_ts
    assert second.meta["time_label"] == "2026-09-22 10:00" and second.meta["time_source"] == "time_label"
    stats = conv.meta["wechat_history"]
    assert stats["interval_pairs"] == 0 and stats["median_interval_seconds"] is None and stats["label_times"] == 2


def test_label_time_anchors_every_wechat_label_form_to_the_capture_moment():
    now = datetime(2026, 9, 27, 9, 30)  # a Sunday
    assert label_time("08:15", now) == (datetime(2026, 9, 27, 8, 15), False)
    assert label_time("下午 3:05", datetime(2026, 9, 27, 16, 0)) == (datetime(2026, 9, 27, 15, 5), False)
    assert label_time("昨天 23:50", now) == (datetime(2026, 9, 26, 23, 50), False)
    assert label_time("星期三 14:00", now) == (datetime(2026, 9, 23, 14, 0), False)
    assert label_time("9月1日 20:00", now) == (datetime(2026, 9, 1, 20, 0), False)
    assert label_time("12月30日 20:00", now) == (datetime(2025, 12, 30, 20, 0), False), "a later month is last year"
    assert label_time("2024年3月5日 07:00", now) == (datetime(2024, 3, 5, 7, 0), True)
    assert label_time("21:00", now) == (None, False), "a time after the capture cannot be today"
    assert label_time("星期六 10:00", now) == (None, False), "WeChat says 昨天 for yesterday"
    assert label_time("撤回了一条消息", now) == (None, False)


def test_intervals_require_adjacent_explicit_times():
    p = Page("time", [OcrBox(340, 10, 500, 30, "2026-09-22 10:00", .99),
                      OcrBox(40, 55, 200, 75, "第一条", .99),
                      OcrBox(340, 110, 500, 130, "2026-09-22 10:08", .99),
                      OcrBox(620, 160, 780, 180, "第二条", .99)], 850, 240)
    a = HistoryAssembler()
    a.prepend(p)
    stats = a.build("合成好友", 10, "cancelled").meta["wechat_history"]
    assert stats["interval_pairs"] == 1 and stats["median_interval_seconds"] == 480


def test_job_scope_busy_cancel_and_revoke():
    jobs = HistoryJobs()
    done = threading.Event()

    def work(cancel, report):
        report(phase="reading", collected=12)
        cancel.wait(3)
        done.set()
        return None

    job = jobs.start("owner", "rid", 1000, work)
    with pytest.raises(RapportError):
        jobs.get(job.id, "another")
    with pytest.raises(RapportError):
        jobs.start("owner", "rid", 100, work)
    jobs.clear("rid")
    assert done.wait(1)
    with pytest.raises(RapportError):
        jobs.get(job.id, "owner")


def mock_desktop(monkeypatch, pages):
    from PIL import Image

    root = SimpleNamespace(NativeWindowHandle=100, ProcessId=200)
    auto = SimpleNamespace(GetRootControl=lambda: SimpleNamespace(GetChildren=lambda: [root]))
    monkeypatch.setattr(native, "automation", lambda: nullcontext(auto))
    monkeypatch.setattr(native, "process_name", lambda _: "weixin.exe")
    monkeypatch.setattr(native.navigation, "open_contact", lambda *args: (root, None))
    from rapport.ocr import engine

    monkeypatch.setattr(engine, "available", lambda: True)
    img = Image.new("RGB", (850, 700))
    monkeypatch.setattr(native, "seek_latest", lambda *args: (img, page(990)))
    scrolls = []
    monkeypatch.setattr(native, "scroll_chat", lambda *args, **kwargs: scrolls.append(args[3]))
    frames = iter(pages)
    monkeypatch.setattr(native, "read_chat", lambda *args, **kwargs: (img, next(frames), None))
    return scrolls


def test_native_scroll_loop_reaches_1000_and_preserves_latest_digest(monkeypatch):
    scrolls = mock_desktop(monkeypatch, [page(n) for n in range(985, -1, -5)])
    progress = []
    result = native.capture_history("合成好友", 20_000_000, 1000, threading.Event(), lambda **kw: progress.append(kw))
    assert len(result.conversation.messages) == 1000
    assert len(scrolls) == 198 and set(scrolls) == {"up"}
    assert result.view_digest is not None
    assert result.conversation.meta["wechat_history"]["returned_to_latest"]
    assert progress[-1]["collected"] == 1000


def test_native_stops_when_no_more_history_without_claiming_target(monkeypatch):
    mock_desktop(monkeypatch, [page(990), page(990), page(990)])
    cancel = threading.Event()
    monkeypatch.setattr(cancel, "wait", lambda _: False)
    result = native.capture_history("合成好友", 20_000_000, 1000, cancel, lambda **kw: None)
    assert len(result.conversation.messages) == 10
    assert result.conversation.meta["wechat_history"]["stop_reason"] == "no_more_loaded_history"


def test_api_history_auth_progress_result_and_cancel(app_client, monkeypatch):
    client = app_client
    client.patch("/api/wechat/config", json={"enabled": True})
    a = HistoryAssembler()
    a.prepend(page(0))
    conv = a.build("合成好友", 10, "target_reached")

    def work(contact, max_pixels, target, cancel, report):
        report(phase="reading", collected=10, target=target, pages=1)
        return native.NativeCapture(b"preview", conv, 100, 200, "latest-view-digest")

    monkeypatch.setattr(native, "capture_history", work)
    assert client.post("/api/wechat/history", json={"contact": "合成好友", "target_count": 0}).status_code == 422
    response = client.post("/api/wechat/history", json={"contact": "合成好友", "target_count": 10})
    assert response.status_code == 200
    job_id = response.json()["id"]
    for _ in range(100):
        state = client.get(f"/api/wechat/history/{job_id}").json()
        if state["status"] != "running":
            break
        time.sleep(.01)
    assert state["status"] == "completed"
    assert state["result"]["history"]["collected"] == 10
    ticket = client.app_state.wechat_captures.items[state["result"]["token"]]
    assert ticket.digest == "latest-view-digest"
    assert client.post(f"/api/wechat/history/{job_id}/cancel", json={}, headers={"x-rapport-csrf": "wrong"}).status_code == 403
    pair = client.post("/api/pair", json={"code": "123456"})
    client.headers["x-rapport-csrf"] = pair.json()["csrf"]
    assert client.get(f"/api/wechat/history/{job_id}").status_code == 403


def test_visual_overlap_crosses_media_without_merging_repeated_short_replies():
    pytest.importorskip("cv2")
    import numpy as np
    from PIL import Image

    image = Image.fromarray(np.random.default_rng(123).integers(0, 256, (700, 850, 3), dtype=np.uint8))
    older_image = image.crop((0, 40, 850, 440))
    newer_image = image.crop((0, 100, 850, 500))
    assert visual_shift(newer_image, older_image) == 60
    newer = Page("new", [OcrBox(40, 50, 150, 70, "好", .99), OcrBox(40, 180, 150, 200, "好", .99)], 850, 400)
    newer.image = newer_image
    older = Page("old", [OcrBox(40, 15, 150, 35, "好", .99), OcrBox(40, 110, 150, 130, "好", .99), OcrBox(40, 240, 150, 260, "好", .99)], 850, 400)
    older.image = older_image
    a = HistoryAssembler()
    a.prepend(newer)
    a.prepend(older)
    assert a.count == 3
    assert a.removed == 2


def test_visual_overlap_rejects_blank_or_resized_frames():
    pytest.importorskip("cv2")
    from PIL import Image

    assert visual_shift(Image.new("RGB", (300, 400), "white"), Image.new("RGB", (300, 400), "white")) is None
    assert visual_shift(Image.new("RGB", (300, 400)), Image.new("RGB", (300, 500))) is None


def test_cancel_keeps_collected_messages_and_does_not_keep_scrolling(monkeypatch):
    scrolls = mock_desktop(monkeypatch, [])
    cancel = threading.Event()

    def progress(**value):
        if value["phase"] == "reading":
            cancel.set()

    result = native.capture_history("合成好友", 20_000_000, 1000, cancel, progress)
    assert not scrolls
    stats = result.conversation.meta["wechat_history"]
    assert stats["collected"] == 10 and stats["stop_reason"] == "cancelled"
    assert not stats["returned_to_latest"]
