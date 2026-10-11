from __future__ import annotations

import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from rapport.db import Import
from rapport.desktop import wechat_native as native
from rapport.desktop.wechat import (
    CaptureTickets,
    context_digest,
    to_conversation,
    verify_contact,
)
from rapport.errors import RapportError
from rapport.ocr.engine import OcrBox


@pytest.fixture(autouse=True)
def isolated_chat_ocr_cache():
    from rapport.ocr import engine

    engine._CHAT_CACHE.clear()
    yield
    engine._CHAT_CACHE.clear()


def conversation(text="明天见"):
    return to_conversation([OcrBox(15, 20, 160, 45, text, .98), OcrBox(360, 85, 485, 110, "好的", .99)], 500, 250, "演示好友")


def ticket(store=None):
    store = store or CaptureTickets()
    return store, store.add("browser", 100, 200, "演示好友", "default", conversation())


def test_exact_title_and_confidence_guard():
    verify_contact([OcrBox(0, 0, 80, 20, "演示 好友", .98)], "演示好友")
    for wrong in ("演示好友群", "另一位好友"):
        with pytest.raises(RapportError, match="未能核对"):
            verify_contact([OcrBox(0, 0, 80, 20, wrong, .99)], "演示好友")
    with pytest.raises(RapportError):
        verify_contact([OcrBox(0, 0, 80, 20, "演示好友", .5)], "演示好友")


def test_reconstructed_roles():
    conv = conversation()
    assert [message.sender.value for message in conv.events()] == ["other", "me"]
    assert conv.source.value == "ocr"
    assert any(warning.code == "visible_only" for warning in conv.warnings)
    assert context_digest(conv) != context_digest(conversation("新的消息"))


def test_split_contact_title_requires_entire_name():
    boxes = [OcrBox(12, 8, 90, 27, "测试 好友", .98), OcrBox(98, 8, 160, 27, "Lucien", .98)]
    verify_contact(boxes, "测试好友 Lucien")
    for partial in ("测试好友", "Lucien"):
        with pytest.raises(RapportError):
            verify_contact(boxes, partial)
    boxes[1].score = .4
    with pytest.raises(RapportError):
        verify_contact(boxes, "测试好友")


def rect(x0, y0, x1, y1):
    return SimpleNamespace(left=x0, top=y0, right=x1, bottom=y1, width=lambda: x1-x0, height=lambda: y1-y0)


def chat_controls(left=0, header_height=50, contact="演示好友"):
    return SimpleNamespace(title=SimpleNamespace(BoundingRectangle=rect(left, 0, 742, header_height)),
                           messages=SimpleNamespace(BoundingRectangle=rect(left, header_height, 742, 414)),
                           editor=SimpleNamespace(Name=contact))


@pytest.mark.parametrize("left,header_height", [(0, 50), (223, 71)])
def test_window_layout_preserves_title_and_first_message(monkeypatch, left, header_height):
    from PIL import Image

    from rapport.ocr import engine

    image = Image.new("RGB", (742, 755), "white")
    image.putpixel((left, 0), (1, 2, 3))
    calls = []

    def recognize_region(part):
        calls.append(part.size)
        if len(calls) == 1:
            assert part.getpixel((8, 8)) == (1, 2, 3)
            assert part.height == header_height + 16
            return [OcrBox(12, 8, 150, 27, "测试好友 Lucien", .98)]
        # First message at y=88 in the window must still be in the message crop.
        return [OcrBox(70, 88 - header_height, 160, 107 - header_height, "测试消息", .98)]

    monkeypatch.setattr(engine, "run", recognize_region)
    crop, conv = native.recognize_controls(image, rect(0, 0, 742, 755), chat_controls(left, header_height, "测试好友 Lucien"), "测试好友 Lucien")
    assert crop.width == 742 - left
    assert conv.events()[0].text == "测试消息"
    assert conv.events()[0].sender.value == "other"
    assert len(calls) == 2


def test_message_ocr_never_runs_before_contact_matches(monkeypatch):
    from PIL import Image

    from rapport.ocr import engine

    calls = []
    def wrong_header(image):
        calls.append(image.size)
        return [OcrBox(0, 0, 100, 20, "未指定好友", .99)]
    monkeypatch.setattr(engine, "run", wrong_header)
    with pytest.raises(RapportError):
        native.recognize_controls(Image.new("RGB", (742, 755)), rect(0, 0, 742, 755), chat_controls(), "演示好友")
    assert len(calls) == 1
    assert calls[0][1] < 110


def test_small_contact_label_is_padded_and_upscaled_for_ocr(monkeypatch):
    from PIL import Image

    from rapport.ocr import engine

    sizes = []

    def ocr(image):
        sizes.append(image.size)
        return [OcrBox(12, 8, 150, 27, "演示好友", .98)] if len(sizes) == 1 else [OcrBox(20, 30, 120, 50, "消息", .98)]

    monkeypatch.setattr(engine, "run", ocr)
    native.recognize_controls(Image.new("RGB", (742, 755)), rect(0, 0, 742, 755), chat_controls(header_height=25), "演示好友")
    assert sizes[0] == ((742 + 16) * 2, (25 + 16) * 2)
    assert sizes[1] == (742, 414 - 25)


def test_uncommon_name_uses_two_exact_accessible_names_not_ocr(monkeypatch):
    from PIL import Image

    from rapport.ocr import engine

    controls = chat_controls(contact="劳娈淇")
    controls.title.Name = "劳娈淇"
    controls.title.AutomationId = "title.current_chat_name_label"
    calls = []

    def ocr(image):
        calls.append(image.size)
        # A title OCR call would misread 娈 as 变. It must not be called at all
        # when the actual UIA title and editor have both matched exactly.
        if image.height < 100:
            return [OcrBox(5, 5, 80, 25, "劳变淇", .94)]
        return [OcrBox(20, 30, 120, 50, "测试消息", .98)]

    monkeypatch.setattr(engine, "run", ocr)
    _, conv = native.recognize_controls(Image.new("RGB", (742, 755)), rect(0, 0, 742, 755), controls, "劳娈淇")
    assert calls == [(742, 364)]
    assert conv.participants.other == "劳娈淇"


def test_contact_change_during_ocr_aborts_result(monkeypatch):
    from PIL import Image

    from rapport.ocr import engine

    controls = chat_controls(contact="指定好友")
    controls.title.Name = "指定好友"
    controls.title.AutomationId = "title.current_chat_name_label"

    def changed(image):
        controls.title.Name = "另一位好友"
        return [OcrBox(20, 30, 120, 50, "测试消息", .98)]

    monkeypatch.setattr(engine, "run", changed)
    engine._CHAT_CACHE.clear()
    try:
        with pytest.raises(RapportError):
            native.recognize_controls(Image.new("RGB", (742, 755)), rect(0, 0, 742, 755), controls, "指定好友")
    finally:
        engine._CHAT_CACHE.clear()


def test_ticket_scope_expiry_single_use_and_capacity():
    store, item = ticket()
    for owner, rid in (("someone_else", "default"), ("browser", "other")):
        with pytest.raises(RapportError) as error:
            store.get(item.token, owner, rid)
        assert error.value.status == 403
    store.consume_fill(item)
    with pytest.raises(RapportError):
        store.consume_fill(item)
    item.created -= store.ttl + 1
    with pytest.raises(RapportError):
        store.get(item.token, "browser", "default")
    for _ in range(12):
        ticket(store)
    assert len(store.items) == 8
    store.discard_relationship("default")
    assert not store.items


def test_window_pid_process_and_visibility_checks(monkeypatch):
    control = SimpleNamespace(NativeWindowHandle=100, ProcessId=200, IsOffscreen=False)
    auto = SimpleNamespace(ControlFromHandle=lambda _: control)
    monkeypatch.setattr(native, "process_name", lambda _: "notepad.exe")
    with pytest.raises(RapportError) as error:
        native.checked_window(auto, 100, 200)
    assert error.value.code == "wechat_not_wechat"
    monkeypatch.setattr(native, "process_name", lambda _: "weixin.exe")
    with pytest.raises(RapportError):
        native.checked_window(auto, 100, 201)
    control.IsOffscreen = True
    with pytest.raises(RapportError):
        native.checked_window(auto, 100, 200)


def fake_fill_adapter(monkeypatch, initial="", changed=False, wrong_focus=False, clipboard=False, uncertain=False):
    state = {"value": initial, "writes": [], "keys": []}
    class Pattern:
        IsReadOnly = False
        @property
        def Value(self):
            return state["value"]
        def SetValue(self, value):
            state["writes"].append(value)
            state["value"] = "" if uncertain else value
            return True
    def paste(value, **kwargs):
        state["keys"].append(value)
        state["value"] = state["clipboard"]

    control = SimpleNamespace(GetValuePattern=lambda: None if clipboard else Pattern(), SetFocus=lambda: True,
                              GetTextPattern=lambda: SimpleNamespace(DocumentRange=SimpleNamespace(GetText=lambda _: state["value"])),
                              GetTopLevelControl=lambda: SimpleNamespace(NativeWindowHandle=100, ProcessId=200),
                              SendKeys=paste)
    auto = SimpleNamespace(GetFocusedControl=lambda: object() if wrong_focus else control, ControlsAreSame=lambda a, b: a is b,
                           SetClipboardText=lambda value: state.update(clipboard=value))
    monkeypatch.setattr(native, "automation", lambda: nullcontext(auto))
    monkeypatch.setattr(native, "checked_window", lambda *args: object())
    monkeypatch.setattr(native.navigation, "restore", lambda *args: None)
    monkeypatch.setattr(native, "read_chat", lambda *args: (None, conversation("新的消息") if changed else conversation(), control))
    monkeypatch.setattr(native.time, "sleep", lambda _: None)
    return state


def test_fill_literal_text_no_enter_and_ticket_consumed(monkeypatch):
    state = fake_fill_adapter(monkeypatch)
    store, item = ticket()
    draft = "文字里的 {Enter} 只是文字\n第二行"
    result = native.fill(item, draft, 20_000_000, lambda: store.consume_fill(item))
    assert result["ok"] and not result["sent"]
    assert state["writes"] == [draft] and state["keys"] == []
    assert item.filled


def test_clipboard_fallback_pastes_literal_text_without_enter(monkeypatch):
    state = fake_fill_adapter(monkeypatch, clipboard=True)
    store, item = ticket()
    draft = "{Enter} 是文字，不是发送命令"
    native.fill(item, draft, 20_000_000, lambda: store.consume_fill(item))
    assert state["value"] == draft
    assert state["keys"] == ["{Ctrl}v"]


def test_uncertain_write_consumes_ticket_and_refuses_retry(monkeypatch):
    state = fake_fill_adapter(monkeypatch, uncertain=True)
    store, item = ticket()
    with pytest.raises(RapportError) as error:
        native.fill(item, "草稿", 20_000_000, lambda: store.consume_fill(item))
    assert error.value.code == "wechat_fill_uncertain" and item.filled
    with pytest.raises(RapportError):
        native.fill(item, "草稿", 20_000_000, lambda: store.consume_fill(item))
    assert state["writes"] == ["草稿"]


@pytest.mark.parametrize("options,code", [({"initial": "已有草稿"}, "wechat_input_not_empty"), ({"changed": True}, "wechat_context_changed"), ({"wrong_focus": True}, "wechat_focus_changed")])
def test_fill_refuses_unsafe_target_without_writing(monkeypatch, options, code):
    state = fake_fill_adapter(monkeypatch, **options)
    store, item = ticket()
    with pytest.raises(RapportError) as error:
        native.fill(item, "准备填入的文字", 20_000_000, lambda: store.consume_fill(item))
    assert error.value.code == code
    assert not item.filled and not state["writes"] and not state["keys"]


@pytest.fixture
def reader(app_client, monkeypatch):
    monkeypatch.setattr(native, "capabilities", lambda: {"windows": True, "automation": True, "ocr": True})
    monkeypatch.setattr(native, "capture_contact", lambda *args: native.NativeCapture(b"synthetic-preview", conversation(), 100, 200))
    return app_client


def enable(client, fill=False):
    result = client.patch("/api/wechat/config", json={"enabled": True, "allow_fill": fill})
    assert result.status_code == 200


def capture_request(client, rid="default"):
    return client.post("/api/wechat/capture", json={"contact": "演示好友", "relationship_id": rid})


def test_api_off_by_default_auth_csrf_and_no_cache(reader):
    assert not reader.get("/api/wechat/config").json()["enabled"]
    assert capture_request(reader).status_code == 409
    assert reader.patch("/api/wechat/config", json={"enabled": True}, headers={"x-rapport-csrf": "wrong"}).status_code == 403
    assert reader.get("/api/wechat/config").headers["cache-control"] == "no-store"
    reader.cookies.clear()
    assert reader.get("/api/wechat/config").status_code == 401


def test_capture_contract_does_not_accept_user_geometry(reader):
    enable(reader)
    for extra in ({"region": {"left": .3}}, {"window_id": 100}, {"me_side": "left"}):
        assert reader.post("/api/wechat/capture", json={"contact": "演示好友", **extra}).status_code == 422


def test_capture_is_ephemeral_until_import_and_import_is_idempotent(reader):
    enable(reader)
    result = capture_request(reader)
    assert result.status_code == 200
    with reader.app_state.db.session() as session:
        assert session.query(Import).count() == 0
    body = {"token": result.json()["token"], "relationship_id": "default"}
    first = reader.post("/api/wechat/import", json=body)
    second = reader.post("/api/wechat/import", json=body)
    assert first.status_code == second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["parser_id"] == "wechat_visual"
    message_id = first.json()["messages"][0]["id"]
    reader.patch(f"/api/imports/{first.json()['id']}", json={"messages": [{"id": message_id, "text": "校对后的文字"}]})
    assert reader.post("/api/wechat/import", json=body).json()["messages"][0]["text"] == "校对后的文字"
    with reader.app_state.db.session() as session:
        assert session.query(Import).count() == 1


def test_capture_not_shareable_with_another_paired_browser(reader):
    enable(reader)
    token = capture_request(reader).json()["token"]
    pair = reader.post("/api/pair", json={"code": "123456"})
    reader.headers["x-rapport-csrf"] = pair.json()["csrf"]
    assert reader.post("/api/wechat/target", json={"token": token}).status_code == 403


def test_fill_review_scope_replay_and_no_plaintext_log(reader, monkeypatch):
    enable(reader, fill=True)
    token = capture_request(reader).json()["token"]
    imported = reader.post("/api/wechat/import", json={"token": token}).json()
    body = {"token": token, "import_id": imported["id"], "text": "私密草稿内容"}
    assert reader.post("/api/wechat/fill", json=body).status_code == 409
    assert reader.post("/api/wechat/fill", json={**body, "send": True}).status_code == 422
    assert reader.post(f"/api/imports/{imported['id']}/confirm", json={"range_start": 0, "range_end": 1}).status_code == 200
    calls = []
    def fill(item, text, max_pixels, before_write):
        before_write()
        calls.append(text)
        return {"ok": True, "sent": False, "contact": item.contact}
    monkeypatch.setattr(native, "fill", fill)
    assert reader.post("/api/wechat/fill", json=body).status_code == 200
    assert reader.post("/api/wechat/fill", json=body).status_code == 409
    assert calls == [body["text"]]
    log = json.dumps(reader.get("/api/desktop/log").json(), ensure_ascii=False)
    assert body["text"] not in log and "演示好友" not in log


def test_disable_and_delete_revoke_bindings(reader):
    enable(reader)
    token = capture_request(reader).json()["token"]
    reader.patch("/api/wechat/config", json={"enabled": False})
    assert not reader.app_state.wechat_captures.items
    enable(reader)
    assert reader.post("/api/wechat/target", json={"token": token}).status_code == 409
    relationship = reader.post("/api/relationships", json={"name": "临时对象"}).json()
    assert capture_request(reader, relationship["id"]).status_code == 200
    reader.delete(f"/api/relationships/{relationship['id']}")
    assert not reader.app_state.wechat_captures.items
    capture_request(reader)
    reader.app_state.wipe_all()
    assert not reader.app_state.wechat_captures.items
