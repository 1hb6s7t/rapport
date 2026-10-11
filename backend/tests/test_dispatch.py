from __future__ import annotations

import hashlib
import io
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from rapport.api import dispatch as api
from rapport.api.wechat import owner
from rapport.db import Import, Setting
from rapport.desktop.dispatch import DispatchStore, verify_text_append
from rapport.desktop.wechat import to_conversation
from rapport.errors import RapportError
from rapport.ocr.engine import OcrBox
from rapport.schemas.reply import ReplyDraft, ReplyItem


def conversation(text="你好"):
    return to_conversation([OcrBox(10, 20, 80, 40, text, .99), OcrBox(300, 80, 430, 100, "收到", .99)], 500, 200, "演示好友")


def test_reply_items_preserve_text_and_refuse_paths():
    draft = ReplyDraft(style="warm", text="你好")
    assert draft.items == [] and draft.ranking == {}
    assert ReplyItem(kind="image", asset_id="asset123").text is None
    for value in ({"kind": "text", "text": " "}, {"kind": "text", "text": "hello", "asset_id": "x"},
                  {"kind": "image", "asset_id": "x", "path": "C:/private.png"}, {"kind": "sticker", "text": "x"}):
        with pytest.raises(ValidationError):
            ReplyItem(**value)


@pytest.fixture
def bound(app_client):
    state = app_client.app_state
    state._set_flag("desktop.wechat.read", True)
    state._set_flag("desktop.wechat.fill", True)
    who = owner(SimpleNamespace(cookies=app_client.cookies))
    capture = state.wechat_captures.add(who, 123, 456, "演示好友", "default", conversation())
    imp = state.save_import(capture.conversation, "wechat_visual", "test", relationship_id="default")
    with state.db.session() as session:
        session.get(Import, imp.id).status = "confirmed"
    capture.import_id = imp.id
    return app_client, capture


def preview(client, capture, items=None):
    return client.post("/api/dispatch/preview", json={"capture_token": capture.token, "import_id": capture.import_id,
                       "relationship_id": capture.relationship_id, "items": items or [{"kind": "text", "text": "第一条"}, {"kind": "text", "text": "第二条"}]})


@pytest.fixture
def adapter(monkeypatch):
    calls = []

    class Adapter:
        def __init__(self, state=None):
            pass

        @contextmanager
        def session(self, ticket, items, max_pixels):
            calls.append("open")
            yield self

        def send(self, item):
            calls.append(item["text"])
            return True

    monkeypatch.setattr(api, "WindowsDispatchAdapter", Adapter)
    return calls, Adapter


def test_preview_does_not_operate_desktop_and_confirm_single_use(bound, adapter):
    client, capture = bound
    calls, _ = adapter
    result = preview(client, capture)
    assert result.status_code == 200, result.text
    data = result.json()
    assert calls == [] and data["contact"] == capture.contact
    assert data["items"][0]["text"] == "第一条"
    assert result.headers["cache-control"] == "no-store"
    body = {"token": data["token"], "digest": data["digest"]}
    sent = client.post("/api/dispatch/confirm", json=body)
    assert sent.status_code == 200, sent.text
    assert sent.json()["status"] == "complete"
    assert calls == ["open", "第一条", "第二条"]
    assert capture.filled
    assert client.post("/api/dispatch/confirm", json=body).json()["status"] == "complete"
    assert calls == ["open", "第一条", "第二条"]
    assert preview(client, capture).status_code == 409


def test_uncertain_item_stops_group_and_cannot_resume(bound, adapter):
    client, capture = bound
    calls, cls = adapter

    def send(self, item):
        calls.append(item["text"])
        if item["text"] == "第二条":
            raise RuntimeError("private text must not leak")
        return True

    cls.send = send
    data = preview(client, capture, [{"kind": "text", "text": text} for text in ("第一条", "第二条", "第三条")]).json()
    body = {"token": data["token"], "digest": data["digest"]}
    sent = client.post("/api/dispatch/confirm", json=body)
    assert "private text" not in sent.text
    assert [item["status"] for item in sent.json()["results"]] == ["sent", "uncertain", "pending"]
    assert calls == ["open", "第一条", "第二条"]
    client.post("/api/dispatch/confirm", json=body)
    assert calls == ["open", "第一条", "第二条"]


def test_preview_bound_to_browser_digest_and_confirmed_material(bound, adapter):
    client, capture = bound
    data = preview(client, capture).json()
    bad = client.post("/api/dispatch/confirm", json={"token": data["token"], "digest": "0" * 64})
    assert bad.status_code == 409
    # Same paired browser without CSRF cannot confirm.
    csrf = client.headers.pop("x-rapport-csrf")
    assert client.post("/api/dispatch/confirm", json={"token": data["token"], "digest": data["digest"]}).status_code == 403
    client.headers["x-rapport-csrf"] = csrf
    with client.app_state.db.session() as session:
        session.get(Import, capture.import_id).status = "awaiting_confirmation"
    result = client.post("/api/dispatch/confirm", json={"token": data["token"], "digest": data["digest"]})
    assert result.json()["results"][0]["status"] == "failed"
    assert adapter[0] == []


def test_plaintext_not_in_receipt_metadata_and_restart_never_replays(bound):
    client, capture = bound
    data = preview(client, capture).json()
    service = api.store(client.app_state)
    with client.app_state.db.session() as session:
        row = session.get(Setting, service.key(data["token"]))
        assert "第一条" not in str(row.value) and capture.contact not in str(row.value)
        assert b"\xe7\xac\xac" not in client.app_state.blobs._p(row.value["blob_id"]).read_bytes()
    receipt = service.get(data["token"], capture.owner)
    receipt["status"] = "running"
    receipt["results"][0]["status"] = "uncertain"
    service._save(data["token"], receipt)
    restarted = DispatchStore(client.app_state)
    result = restarted.execute(data["token"], capture.owner, data["digest"], lambda *_: pytest.fail("replayed"))
    assert result["results"][0]["status"] == "uncertain"
    with pytest.raises(RapportError):
        restarted.get(data["token"], "another-browser")


def test_clearing_relationship_removes_receipt_and_encrypted_blob(bound):
    client, capture = bound
    data = preview(client, capture).json()
    service = api.store(client.app_state)
    service.clear("other-relationship")
    assert service.get(data["token"], capture.owner)
    service.clear("default")
    with pytest.raises(RapportError):
        service.get(data["token"], capture.owner)


def test_text_append_verification_rejects_unchanged_or_other_sender():
    before = conversation()
    after = before.model_copy(deep=True)
    last = after.messages[-1].model_copy(update={"id": "new", "seq": 2, "text": "新回复"})
    after.messages.append(last)
    assert verify_text_append(before, after, "新回复")
    assert not verify_text_append(before, before, "收到")
    assert not verify_text_append(before, after, "不同文字")


def test_asset_preview_enforces_original_checksum_scope(bound):
    from PIL import Image

    client, capture = bound
    output = io.BytesIO()
    Image.new("RGB", (32, 32), "pink").save(output, format="PNG")
    data = output.getvalue()
    record = {"id": "asset1", "kind": "image", "status": "original", "sha256": hashlib.sha256(data).hexdigest(), "mime": "image/png", "archive_id": "archive1", "caption": "测试图"}

    class Assets:
        def get_asset(self, asset_id, rid):
            assert asset_id == "asset1" and rid == "default"
            return record

        def read_asset(self, asset_id, rid):
            return data

    client.app_state.archives = Assets()
    result = preview(client, capture, [{"kind": "image", "asset_id": "asset1"}])
    assert result.status_code == 200, result.text
    assert result.json()["items"][0]["preview_url"].startswith("data:image/png;base64,")
    record["status"] = "thumbnail"
    assert preview(client, capture, [{"kind": "image", "asset_id": "asset1"}]).status_code == 409
    record["status"] = "original"
    record["sha256"] = "bad"
    assert preview(client, capture, [{"kind": "image", "asset_id": "asset1"}]).status_code == 409


def test_windows_media_unsupported_stops_whole_group_before_native_calls():
    from rapport.desktop.dispatch import WindowsDispatchAdapter

    with pytest.raises(RapportError, match="媒体预览"):
        with WindowsDispatchAdapter().session(None, [{"kind": "text", "text": "不能先发"}, {"kind": "image"}], 100):
            pytest.fail("must preflight")


def test_native_text_dispatch_uses_literal_content_and_verified_append(monkeypatch):
    from rapport.desktop import dispatch, media
    from rapport.desktop import wechat_native as native
    from rapport.desktop.dispatch import WindowsDispatchAdapter, _WindowsTextSession

    before = conversation()
    after = before.model_copy(deep=True)
    text = "测试 {Enter} 按原文发送"
    after.messages.append(after.messages[-1].model_copy(update={"id": "new", "seq": 2, "text": text}))
    state = {"clicked": 0, "value": ""}

    class Pattern:
        IsReadOnly = False

        def SetValue(self, value):
            state["value"] = value
            return True

    editor = SimpleNamespace(SetFocus=lambda: None, GetValuePattern=lambda: Pattern())

    def click(**kwargs):
        state["clicked"] += 1
        state["value"] = ""

    button = SimpleNamespace(IsEnabled=True, Click=click)
    auto = SimpleNamespace(ControlsAreSame=lambda a, b: a is b)
    reads = []

    def read(*_):
        reads.append("clicked" if state["clicked"] else "filled" if state["value"] else "empty")
        return None, after if state["clicked"] else before, editor

    monkeypatch.setattr(dispatch, "read_dispatch_chat", read)
    monkeypatch.setattr(dispatch, "recipient_controls", lambda *_: SimpleNamespace(editor=editor))
    monkeypatch.setattr(native, "_input_value", lambda _: state["value"])
    monkeypatch.setattr(native.navigation, "assert_focus", lambda *_: None)
    monkeypatch.setattr(media, "image_controls", lambda _: [])
    monkeypatch.setattr(WindowsDispatchAdapter, "_send_button", lambda *_: button)
    session = _WindowsTextSession(auto, object(), SimpleNamespace(contact="演示好友"), 100, before)
    assert session.send({"kind": "text", "text": text}) is True
    assert state["clicked"] == 1 and session.current is after
    assert "filled" not in reads, "no OCR pass between the fill and the click"


def test_receipt_ignores_ocr_line_joins_and_width_but_not_content():
    before = conversation()
    after = before.model_copy(deep=True)
    after.messages.append(after.messages[-1].model_copy(update={"id": "new", "seq": 2, "text": "第一行第二行，好的!"}))
    assert verify_text_append(before, after, "第一行\n第二行，好的！")
    assert not verify_text_append(before, after, "第一行\n第三行，好的！")


def test_native_focus_failure_records_preclick_stage(monkeypatch):
    from rapport.desktop import wechat_native as native
    from rapport.desktop.dispatch import _WindowsTextSession

    def lost_focus(*_):
        raise RapportError("wechat_focus_changed", "focus changed")

    monkeypatch.setattr(native.navigation, "assert_focus", lost_focus)
    with pytest.raises(RapportError) as caught:
        _WindowsTextSession._assert_editor_focus(None, None, None, "text_focus_before_click")
    assert caught.value.dispatch_stage == "text_focus_before_click"


@pytest.mark.parametrize("reason", ["context", "draft", "image"])
def test_native_stops_changed_chat_or_any_existing_draft(monkeypatch, reason):
    from rapport.desktop import dispatch, media
    from rapport.desktop import wechat_native as native
    from rapport.desktop.dispatch import _WindowsTextSession

    before = conversation()
    editor = object()
    monkeypatch.setattr(dispatch, "read_dispatch_chat", lambda *_: (None, conversation("新消息") if reason == "context" else before, editor))
    monkeypatch.setattr(native, "_input_value", lambda _: "已有草稿" if reason == "draft" else "")
    monkeypatch.setattr(media, "image_controls", lambda _: [object()] if reason == "image" else [])
    session = _WindowsTextSession(None, None, SimpleNamespace(contact="演示好友"), 100, before)
    with pytest.raises(RapportError):
        session.send({"kind": "text", "text": "不可发送"})


def test_image_preview_matches_real_pixels_and_rejects_icons_or_flat_images():
    from PIL import Image, ImageDraw

    from rapport.desktop.media import matches_preview

    image = Image.new("RGB", (120, 90), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 55, 89), fill="pink")
    draw.ellipse((40, 10, 115, 80), fill="purple")
    assert matches_preview(image, image.resize((80, 60), Image.Resampling.LANCZOS))
    assert not matches_preview(image, Image.new("RGB", (80, 60), "pink"))
    assert not matches_preview(image, image.resize((30, 30)))
    assert not matches_preview(Image.new("RGB", (100, 100), "white"), Image.new("RGB", (100, 100), "white"))


def test_media_collection_token_scope_expiry_and_single_use_fail_before_ui():
    import time

    from rapport.desktop.media import MediaCollectionAdapter

    service = MediaCollectionAdapter()
    ticket = SimpleNamespace(owner="browser", token="capture")
    service.selections["selection"] = {"owner": "another", "capture_token": "capture", "archive_id": "a", "message_ref": "m", "used": False, "created": time.monotonic()}
    with pytest.raises(RapportError) as error:
        service.collect(None, ticket, "selection", "a", "m")
    assert error.value.code == "wechat_media_selection_scope"
    service.selections["selection"]["used"] = True
    with pytest.raises(RapportError) as error:
        service.collect(None, ticket, "selection", "a", "m")
    assert error.value.code == "wechat_media_selection_expired"


def test_gif_preserves_exact_animation_frames_and_bounds_decode():
    from PIL import Image, ImageDraw

    from rapport.desktop.media import media_frames

    frames = []
    for color in ("purple", "blue"):
        frame = Image.new("RGB", (80, 60), "white")
        ImageDraw.Draw(frame).rectangle((5, 5, 60, 40), fill=color)
        frames.append(frame)
    output = io.BytesIO()
    frames[0].save(output, format="GIF", save_all=True, append_images=frames[1:], duration=[120, 200], loop=0)
    decoded, durations = media_frames(output.getvalue(), "image/gif")
    assert len(decoded) == 2 and durations == [120, 200]
    assert decoded[0].tobytes() != decoded[1].tobytes()
    with pytest.raises(RapportError):
        media_frames(output.getvalue(), "image/png")
    with pytest.raises(RapportError):
        media_frames(output.getvalue(), "image/gif", max_pixels=100)


def test_dynamic_sticker_preview_is_original_gif_not_flattened(bound):
    from PIL import Image

    client, capture = bound
    frames = [Image.new("RGB", (40, 40), color) for color in ("pink", "purple")]
    output = io.BytesIO()
    frames[0].save(output, format="GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
    data = output.getvalue()
    record = {"id": "gif1", "kind": "sticker", "status": "original", "sha256": hashlib.sha256(data).hexdigest(),
              "mime": "image/gif", "archive_id": "archive1", "caption": "合成动态表情"}
    client.app_state.archives = SimpleNamespace(get_asset=lambda *_: record, read_asset=lambda *_: data)
    result = preview(client, capture, [{"kind": "sticker", "asset_id": "gif1"}])
    assert result.status_code == 200, result.text
    item = result.json()["items"][0]
    assert item["animated"] and item["frame_count"] == 2
    assert item["preview_url"].startswith("data:image/gif;base64,")


def test_staged_gif_is_exact_original_and_private_temporary(bound):
    from rapport.desktop.media import staged_asset

    client, _ = bound
    data = b"GIF89a test synthetic placeholder"
    record = {"status": "original", "mime": "image/gif", "sha256": hashlib.sha256(data).hexdigest()}
    client.app_state.archives = SimpleNamespace(get_asset=lambda *_: record, read_asset=lambda *_: data)
    with staged_asset(client.app_state, "asset", "default", record["sha256"]) as path:
        assert path.suffix == ".gif"
        assert path.read_bytes() == data
        assert path.is_relative_to(client.app_state.settings.data_dir)
    assert not path.exists()


def test_animation_requires_two_distinct_verified_frames(monkeypatch):
    import time

    from PIL import Image

    from rapport.desktop import media
    from rapport.desktop import wechat_native as native

    sender = media.StaticImageSender(None)
    session = SimpleNamespace(root=None, ticket=SimpleNamespace(contact="文件传输助手"), auto=None, max_pixels=100)
    monkeypatch.setattr(native.navigation, "locate_chat", lambda *_: None)
    monkeypatch.setattr(native.navigation, "verify_accessible_contact", lambda *_: True)
    sequence = iter([[(None, {"frame1"})], [(None, {"frame2"})]])
    monkeypatch.setattr(sender, "_matching_frames", lambda *_args, **_kw: next(sequence))
    assert sender._verify_animation(session, None, [Image.new("RGB", (40, 40))], True, False)
    # A still first frame is insufficient even though it matches the exact GIF.
    monkeypatch.setattr(sender, "_matching_frames", lambda *_args, **_kw: [(None, {"frame1"})])
    moments = iter([0, 1, 5])
    monkeypatch.setattr(time, "monotonic", lambda: next(moments))
    assert not sender._verify_animation(session, None, [], True, False)


def test_native_collection_api_checks_archive_message_ownership(bound, monkeypatch):
    from tests.test_archives import archive_fixture

    client, capture = bound
    service, archive, _ = archive_fixture(client)
    messages = service.messages(archive["id"], "default")
    calls = []
    fake = SimpleNamespace(preview=lambda *args: calls.append(args) or [{"selection_token": "x" * 24, "status": "thumbnail", "preview": "data:image/png;base64,AA=="}])
    monkeypatch.setattr(api, "collection", lambda _: fake)
    body = {"capture_token": capture.token, "archive_id": archive["id"], "message_ref": messages[1]["message_ref"]}
    result = client.post("/api/dispatch/media/preview", json=body)
    assert result.status_code == 200 and len(calls) == 1
    assert result.json()["selections"][0]["status"] == "thumbnail"
    assert client.post("/api/dispatch/media/preview", json={**body, "message_ref": messages[0]["message_ref"]}).status_code == 422
    assert client.post("/api/dispatch/media/preview", json={**body, "archive_id": "unknown"}).status_code == 404
    assert len(calls) == 1


def test_strict_dispatch_reader_requires_two_accessible_names_and_passes_strict_flag(monkeypatch):
    from rapport.desktop import dispatch
    from rapport.desktop import wechat_native as native

    controls = SimpleNamespace(title=SimpleNamespace(Name="文件传输助手", AutomationId="title.current_chat_name_label"), editor=SimpleNamespace(Name="文件传输助手"))
    monkeypatch.setattr(native.navigation, "locate_chat", lambda *_: controls)
    calls = []

    def read(*args, **kwargs):
        calls.append(kwargs)
        return None, conversation(), controls.editor

    monkeypatch.setattr(native, "read_chat", read)
    dispatch.read_dispatch_chat(None, None, SimpleNamespace(contact="文件传输助手"), 100)
    assert calls == [{"cached_controls": controls, "require_accessible_contact": True}]


@pytest.mark.parametrize("title,editor,aid", [("", "文件传输助手", "title.current_chat_name_label"),
                                              ("文件传输助手", "文件传输助手", "title_container"),
                                              ("其他完整姓名", "文件传输助手", "title.current_chat_name_label"),
                                              ("文件传输助手", "文件", "title.current_chat_name_label")])
def test_strict_dispatch_never_uses_partial_name_or_ocr_fallback(monkeypatch, title, editor, aid):
    from rapport.desktop import dispatch
    from rapport.desktop import wechat_native as native

    controls = SimpleNamespace(title=SimpleNamespace(Name=title, AutomationId=aid), editor=SimpleNamespace(Name=editor))
    calls = []
    monkeypatch.setattr(native.navigation, "locate_chat", lambda *_: calls.append("locate") or controls)
    monkeypatch.setattr(native, "read_chat", lambda *_a, **_kw: pytest.fail("must not reach OCR"))
    with pytest.raises(RapportError) as error:
        dispatch.read_dispatch_chat(None, None, SimpleNamespace(contact="文件传输助手"), 100)
    assert calls == ["locate"]
    assert error.value.dispatch_stage == "recipient_dual_accessibility"
    assert all(isinstance(value, bool) for value in error.value.dispatch_identity.values())


def test_native_strict_mode_rejects_missing_accessibility_before_title_ocr(monkeypatch):
    from PIL import Image

    from rapport.desktop import wechat_native as native
    from rapport.ocr import engine
    from tests.test_wechat_visual import chat_controls, rect

    monkeypatch.setattr(engine, "run", lambda _: pytest.fail("strict sending must not OCR a title"))
    with pytest.raises(RapportError) as error:
        native.recognize_controls(Image.new("RGB", (742, 755)), rect(0, 0, 742, 755), chat_controls(), "演示好友", require_accessible_contact=True)
    assert error.value.code == "wechat_identity_unavailable"


def test_preflight_does_not_reactivate_already_foreground_window(monkeypatch):
    from contextlib import nullcontext

    from rapport.desktop import dispatch, wechat_native
    from rapport.desktop.wechat import context_digest

    current = conversation()
    root = SimpleNamespace(NativeWindowHandle=1, ProcessId=2)
    auto = SimpleNamespace(GetForegroundControl=lambda: root)
    ticket = SimpleNamespace(window_id=1, process_id=2, contact="演示好友", digest=context_digest(current))
    monkeypatch.setattr(wechat_native, "automation", lambda: nullcontext(auto))
    monkeypatch.setattr(wechat_native, "checked_window", lambda *_: root)
    monkeypatch.setattr(wechat_native.navigation, "restore", lambda *_: pytest.fail("unnecessary activation"))
    monkeypatch.setattr(dispatch, "read_dispatch_chat", lambda *_: (None, current, object()))
    monkeypatch.setattr(dispatch, "editor_is_empty", lambda _: True)
    monkeypatch.setattr(dispatch.WindowsDispatchAdapter, "_send_button", lambda *_: object())
    with dispatch.WindowsDispatchAdapter().session(ticket, [{"kind": "text", "text": "hello"}], 100):
        pass


def test_failure_receipt_preserves_stage_without_recipient_or_content(bound):
    client, capture = bound
    data = preview(client, capture).json()

    def operation(*_):
        error = RapportError("wechat_contact_mismatch", "Private text never stored")
        error.dispatch_stage = "recipient_dual_accessibility"
        error.dispatch_identity = {"title_exact": True, "editor_exact": False}
        raise error

    result = api.store(client.app_state).execute(data["token"], capture.owner, data["digest"], operation)
    assert result["failure"] == {"code": "wechat_contact_mismatch", "stage": "recipient_dual_accessibility", "identity": {"title_exact": True, "editor_exact": False}}
    assert "Private text" not in str(result)


def test_preview_source_cannot_change_before_confirmation(bound, adapter):
    client, ticket = bound
    ticket.context_source = "dsh_database"
    ticket.source_binding = {"account": "a" * 32}
    data = preview(client, ticket).json()
    ticket.source_binding["account"] = "b" * 32
    result = client.post("/api/dispatch/confirm", json={k: data[k] for k in ("token", "digest")}).json()
    assert result["failure"]["code"] == "dispatch_context_changed" and adapter[0] == []
    assert preview(client, ticket, [{"kind": "image", "asset_id": "unused"}]).json()["error"]["code"] == "dsh_text_only"
