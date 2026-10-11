"""Sticker search/preview/single-click contract; no native WeChat operation."""

from __future__ import annotations

import hashlib
import io
from types import SimpleNamespace

import pytest
from PIL import Image

from rapport.api import dispatch as api
from rapport.api.wechat import owner
from rapport.db import Import
from rapport.desktop.stickers import StickerSearchStore, _visible_candidates
from rapport.desktop.wechat import to_conversation
from rapport.errors import RapportError
from rapport.ocr.engine import OcrBox


def _bound(app_client):
    state = app_client.app_state
    state._set_flag("desktop.wechat.read", True)
    state._set_flag("desktop.wechat.fill", True)
    conversation = to_conversation([OcrBox(10, 20, 80, 40, "你好", .99),
                                    OcrBox(300, 80, 430, 100, "收到", .99)], 500, 200, "文件传输助手")
    capture = state.wechat_captures.add(owner(SimpleNamespace(cookies=app_client.cookies)),
                                        123, 456, "文件传输助手", "default", conversation)
    material = state.save_import(conversation, "demo", "synthetic", relationship_id="default")
    with state.db.session() as session:
        session.get(Import, material.id).status = "confirmed"
    capture.import_id = material.id
    return state, capture


def _image():
    output = io.BytesIO()
    Image.new("RGB", (90, 90), "#b47a99").save(output, "PNG")
    return output.getvalue()


def _body(capture):
    return {"capture_token": capture.token, "relationship_id": "default", "import_id": capture.import_id}


def test_store_is_scoped_to_browser_relationship_and_capture():
    store = StickerSearchStore()
    image = _image()
    capture = SimpleNamespace(token="capture-a", relationship_id="friend-a")
    [choice] = store.add("browser-a", capture, "你好", [{"index": 0, "label": "你好表情，来自示例",
        "class_name": "client-image sticker", "sha256": hashlib.sha256(image).hexdigest(),
        "image": image, "chat_signature": "a" * 64, "rect": (10, 20, 100, 110)}])
    assert choice["preview_url"].startswith("data:image/png;base64,")
    assert store.get(choice["token"], "browser-a", capture).query == "你好"
    with pytest.raises(RapportError, match="不属于"):
        store.get(choice["token"], "browser-b", capture)
    with pytest.raises(RapportError, match="不属于"):
        store.get(choice["token"], "browser-a", SimpleNamespace(token="capture-b", relationship_id="friend-a"))
    store.clear("friend-b")
    assert store.get(choice["token"], "browser-a", capture)
    store.clear("friend-a")
    with pytest.raises(RapportError, match="失效"):
        store.get(choice["token"], "browser-a", capture)


def test_disabling_wechat_drops_temporary_sticker_thumbnails(app_client):
    state, capture = _bound(app_client)
    image = _image()
    [choice] = state.sticker_search.add(capture.owner, capture, "你好", [{"index": 0,
        "label": "你好表情，来自示例", "class_name": "client-image sticker",
        "sha256": hashlib.sha256(image).hexdigest(), "image": image,
        "chat_signature": "a" * 64, "rect": (10, 20, 100, 110)}])
    assert state.sticker_search.get(choice["token"], capture.owner, capture)
    response = app_client.patch("/api/wechat/config", json={"enabled": False})
    assert response.status_code == 200
    with pytest.raises(RapportError, match="失效"):
        state.sticker_search.get(choice["token"], capture.owner, capture)


def test_visible_stickers_are_siblings_of_viewport_and_virtual_rows_are_excluded():
    class Box:
        def __init__(self, left, top, right, bottom):
            self.left, self.top, self.right, self.bottom = left, top, right, bottom

        def width(self):
            return self.right - self.left

        def height(self):
            return self.bottom - self.top

    class Node:
        def __init__(self, klass, kind, box, children=(), name=""):
            self.ClassName, self.ControlTypeName, self.BoundingRectangle = klass, kind, box
            self.IsOffscreen, self.Name = False, name
            self.children = children

        def GetChildren(self):
            return self.children

    area = Node("mmui::EmoticonSearchPageBaseView", "GroupControl", Box(0, 0, 500, 330))
    visible = Node("client-image sticker", "ListItemControl", Box(10, 10, 100, 101), name="你好表情")
    virtual = Node("online-image sticker", "ListItemControl", Box(10, 350, 100, 441), name="表情")
    panel = Node("mmui::EmoticonPopover", "WindowControl", Box(0, 0, 500, 400), (area, visible, virtual))
    assert _visible_candidates(panel) == [visible]


def test_search_preview_confirmation_is_bound_and_one_shot(app_client, monkeypatch):
    state, capture = _bound(app_client)
    image = _image()
    calls = []

    def search(ticket, query, pixels):
        calls.append(("search", ticket.contact, query))
        return [{"index": 0, "label": "你好表情，来自示例", "class_name": "client-image sticker",
                 "sha256": hashlib.sha256(image).hexdigest(), "image": image,
                 "chat_signature": "a" * 64, "rect": (10, 20, 100, 110)}]

    def send(ticket, choice, pixels, mark_uncertain):
        calls.append(("send", ticket.contact, choice.label))
        mark_uncertain()
        return True

    monkeypatch.setattr(api, "search_stickers", search)
    monkeypatch.setattr(api, "send_choice", send)
    monkeypatch.setattr(api, "inspect_choice", lambda choice: {"ocr_text": None, "ocr_used": False})
    found = app_client.post("/api/dispatch/stickers/search", json={**_body(capture), "query": "你好"})
    assert found.status_code == 200, found.text
    assert calls == [("search", "文件传输助手", "你好")]
    selected = found.json()["candidates"][0]
    preview = app_client.post("/api/dispatch/stickers/preview", json={**_body(capture), "selection_token": selected["token"]})
    assert preview.status_code == 200, preview.text
    receipt = preview.json()
    assert receipt["contact"] == "文件传输助手" and receipt["items"][0]["sha256"] == selected["sha256"]
    assert calls == [("search", "文件传输助手", "你好")]
    bad = app_client.post("/api/dispatch/stickers/confirm", json={"token": receipt["token"], "digest": "0" * 64})
    assert bad.status_code == 409 and len(calls) == 1
    body = {"token": receipt["token"], "digest": receipt["digest"]}
    sent = app_client.post("/api/dispatch/stickers/confirm", json=body)
    assert sent.status_code == 200 and sent.json()["status"] == "complete"
    assert [item["status"] for item in sent.json()["results"]] == ["sent"]
    assert capture.filled and len(calls) == 2
    again = app_client.post("/api/dispatch/stickers/confirm", json=body)
    assert again.json()["status"] == "complete" and len(calls) == 2
    assert state.dispatch.get(receipt["token"], capture.owner)["items"][0]["selection_token"] == selected["token"]


@pytest.mark.parametrize("clicked", [False, True])
def test_sticker_failure_never_retries(app_client, monkeypatch, clicked):
    _, capture = _bound(app_client)
    image = _image()
    calls = []
    monkeypatch.setattr(api, "search_stickers", lambda *_: [{"index": 0, "label": "表情，来自示例",
        "class_name": "online-image sticker", "sha256": hashlib.sha256(image).hexdigest(),
        "image": image, "chat_signature": "a" * 64, "rect": (10, 20, 100, 110)}])
    monkeypatch.setattr(api, "inspect_choice", lambda choice: {"ocr_text": "你好", "ocr_used": True})

    def fail(*args):
        calls.append("attempt")
        if clicked:
            args[-1]()
        raise RapportError("sticker_candidate_changed" if not clicked else "dispatch_send_uncertain", "stopped")

    monkeypatch.setattr(api, "send_choice", fail)
    result = app_client.post("/api/dispatch/stickers/search", json={**_body(capture), "query": "你好"}).json()
    preview = app_client.post("/api/dispatch/stickers/preview", json={**_body(capture),
        "selection_token": result["candidates"][0]["token"]}).json()
    body = {"token": preview["token"], "digest": preview["digest"]}
    first = app_client.post("/api/dispatch/stickers/confirm", json=body).json()
    assert first["status"] == "stopped"
    assert first["results"][0]["status"] == ("uncertain" if clicked else "failed")
    assert app_client.post("/api/dispatch/stickers/confirm", json=body).json() == first
    assert calls == ["attempt"]


def test_fast_ocr_only_accepts_a_small_selected_tile(monkeypatch):
    from rapport.ocr import engine

    received = []
    monkeypatch.setattr(engine, "_fast_engine", lambda: lambda img: received.append(img.size) or
                        SimpleNamespace(boxes=[[[1, 2], [12, 2], [12, 14], [1, 14]]],
                                        txts=["你好"], scores=[.9]))
    assert engine.run_sticker_thumbnail(Image.new("RGB", (90, 90), "white")) == [OcrBox(1, 2, 12, 14, "你好", .9)]
    assert received == [(90, 90)]
    with pytest.raises(RapportError, match="缩略图"):
        engine.run_sticker_thumbnail(Image.new("RGB", (800, 800), "white"))
