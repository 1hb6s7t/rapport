"""Database text dispatch must never depend on screenshot/OCR acceptance."""

from types import SimpleNamespace as NS

import pytest

from rapport.desktop import dispatch
from rapport.desktop import wechat_native as native
from rapport.desktop.dsh_source import DBAdapter
from rapport.desktop.wechat import context_digest
from rapport.errors import RapportError
from rapport.schemas.message_v1 import Conversation, MsgType, Sender, Source
from tests.test_dsh_integration import ACCOUNT, Reader, install_service, row


def context(rows):
    adapter = DBAdapter(Reader(), ACCOUNT)
    messages = [adapter.convert(r, "wxid_friend") for r in rows]
    for seq, message in enumerate(messages):
        message.seq = seq + 1
    return Conversation(
        id="live",
        source=Source.wechat_json,
        messages=messages,
        meta={"context_source": "dsh_database", "account": ACCOUNT, "contact_id": "wxid_friend"},
    )


def test_bind_uses_current_database_context_without_capture_by_default(app_client, monkeypatch):
    service, reader = install_service(app_client)
    data = service.read(ACCOUNT, "测试好友", "default", 100)
    body = {key: data[key] for key in ("snapshot_id", "revision", "relationship_id")}
    material = app_client.post("/api/wechat/dsh/import", json=body).json()
    reader.rows.append(row(seq=2, local=2, text="新消息"))
    monkeypatch.setattr(native, "capture_contact", lambda *_: pytest.fail("must not capture pixels"))

    def bind(contact, read):
        # This callback executes on the worker; acquiring DB lock would deadlock
        # if the API still held it on the request thread.
        assert service.lock.acquire(blocking=False)
        service.lock.release()
        return 11, 22, read(22)

    monkeypatch.setattr(native, "bind_contact", bind)
    result = app_client.post("/api/wechat/dsh/bind", json=body)
    assert result.status_code == 200, result.text
    ticket = app_client.app_state.wechat_captures.items[result.json()["token"]]
    assert ticket.context_source == "dsh_database" and ticket.import_id == material["id"]
    assert len(ticket.conversation.messages) == 1
    current = service.current_context(ticket.source_binding, ticket.contact, "default", 22)
    assert len(current.messages) == 2 and ticket.digest == context_digest(current)
    assert service.load(data["snapshot_id"], "default")["revision"] == data["revision"]
    # No cursor/material changes from a preflight read.
    assert app_client.post("/api/wechat/dsh/import", json=body).json()["id"] == material["id"]
    with pytest.raises(RapportError):
        service.current_context({**ticket.source_binding, "account": "b" * 32}, ticket.contact, "default", 22)
    app_client.patch("/api/wechat/dsh/config", json={"enabled": False})
    with pytest.raises(RapportError, match="数据库读取已关闭"):
        service.current_context(ticket.source_binding, ticket.contact, "default", 22)


@pytest.mark.parametrize("change", ["card", "type", "sender", "text", "quote", "account", "contact"])
def test_database_digest_covers_all_material_types_and_scope(change):
    before = context([row(kind=49, text="<msg><appmsg><type>2000</type></appmsg></msg>")])
    after = before.model_copy(deep=True)
    m = after.messages[0]
    if change == "card":
        m.raw += "changed"
    if change == "type":
        m.type = MsgType.image
    if change == "sender":
        m.sender = Sender.me
    if change == "text":
        m.text = "new"
    if change == "quote":
        m.quoted_text = "new"
    if change == "account":
        after.meta["account"] = "b" * 32
    if change == "contact":
        after.meta["contact_id"] = "other"
    assert context_digest(before) != context_digest(after)


def test_database_receipt_distinguishes_repeated_text_by_identity():
    first = row(local=1, seq=1, user="wxid_me", text="重复文字", server="101")
    second = row(local=2, seq=2, user="wxid_me", text="重复文字", server="102")
    before, after = context([first]), context([first, second])
    assert dispatch.verify_database_append(before, after, "重复文字")
    assert not dispatch.verify_database_append(before, before, "重复文字")
    assert not dispatch.verify_database_append(before, after, "重复 文字")
    assert dispatch.verify_database_append(context([]), context([second]), "重复文字")


@pytest.mark.parametrize(
    "change",
    [
        "sender",
        "zero_server",
        "missing_server",
        "bad_server",
        "unverified",
        "local_id",
        "old_id",
        "account",
        "contact",
        "overlap",
        "extra",
        "visual",
    ],
)
def test_database_receipt_rejects_ambiguous_changes(change):
    a = row(local=1, seq=1, server="100")
    b = row(local=2, seq=2, user="wxid_me", text="发送文本", server="200")
    before, after = context([a]), context([a, b])
    last = after.messages[-1]
    if change == "sender":
        last.sender = Sender.other
    if change == "zero_server":
        last.meta["server_id"] = "0"
    if change == "missing_server":
        last.meta.pop("server_id")
    if change == "bad_server":
        last.meta["server_id"] = "pending"
    if change == "unverified":
        last.meta["sender_verified"] = False
    if change == "local_id":
        last.meta["local_id"] = 1
    if change == "old_id":
        last.id = before.messages[0].id
    if change == "account":
        after.meta["account"] = "b" * 32
    if change == "contact":
        after.meta["contact_id"] = "different"
    if change == "overlap":
        after.messages[0].raw = "changed"
    if change == "extra":
        after.messages.insert(1, context([row(seq=3, local=3, server="300")]).messages[0])
    if change == "visual":
        after.meta = {}
    assert not dispatch.verify_database_append(before, after, "发送文本")


def test_existing_pending_message_gaining_server_id_is_not_new_send():
    pending = row(local=2, seq=2, user="wxid_me", text="重复文字", server="0")
    before = context([row(server="1"), pending])
    after = context([row(server="1"), {**pending, "server_id": "100"}])
    assert not dispatch.verify_database_append(before, after, "重复文字")


def test_database_dispatch_reader_never_calls_visual_reader(monkeypatch):
    current = context([row()])
    controls = NS(
        title=NS(Name="测试好友", AutomationId="title.current_chat_name_label"), editor=NS(Name="测试好友")
    )
    ticket = NS(
        contact="测试好友",
        context_source="dsh_database",
        source_binding={"account": ACCOUNT},
        relationship_id="default",
        window_id=11,
        process_id=22,
    )
    monkeypatch.setattr(native.navigation, "locate_chat", lambda *_: controls)
    monkeypatch.setattr(native, "checked_window", lambda *_: None)
    monkeypatch.setattr(native, "read_chat", lambda *_a, **_k: pytest.fail("no screenshot/OCR"))
    state = NS(dsh=NS(current_context=lambda *_: current))
    auto = NS(ControlsAreSame=lambda a, b: a is b)
    assert dispatch.read_dispatch_chat(auto, None, ticket, 100, state=state) == (
        None,
        current,
        controls.editor,
    )


@pytest.mark.parametrize("obscured", [False, True])
def test_database_click_checks_only_actual_button_and_never_retries(obscured):
    root = NS(NativeWindowHandle=11, ProcessId=22)
    box = NS(left=10, top=20, right=110, bottom=60)
    button = NS(BoundingRectangle=box, IsEnabled=True)
    hit = NS(GetParentControl=lambda: None) if obscured else button
    calls = []
    auto = NS(
        GetForegroundControl=lambda: root,
        GetMonitorsRect=lambda: [box],
        ControlFromPoint=lambda x, y: calls.append((x, y)) or hit,
        ControlsAreSame=lambda a, b: a is b,
        Click=lambda x, y, waitTime: calls.append("click"),
    )
    if obscured:
        with pytest.raises(RapportError) as error:
            dispatch.click_database_send(auto, root, button)
        assert error.value.code == "dispatch_send_control_obscured"
        assert calls == [(60, 40)]
    else:
        dispatch.click_database_send(auto, root, button)
        assert calls == [(60, 40), "click"]


def test_database_send_uses_database_receipt_and_one_click(monkeypatch):
    from rapport.desktop import media

    before = context([row(server="1")])
    after = context([row(server="1"), row(local=2, seq=2, user="wxid_me", text="测试 {Enter}", server="2")])
    values = {"value": "", "clicked": 0, "pending": 0}
    pattern = NS(IsReadOnly=False, SetValue=lambda text: values.update(value=text))
    editor = NS(SetFocus=lambda: None, GetValuePattern=lambda: pattern)
    button = NS(IsEnabled=True)
    root = NS(NativeWindowHandle=11, ProcessId=22)
    auto = NS(ControlsAreSame=lambda a, b: a is b, GetForegroundControl=lambda: root)
    ticket = NS(context_source="dsh_database")

    def read(*_args, **kwargs):
        assert kwargs["state"] is state
        if values["clicked"] and not values["pending"]:
            values["pending"] += 1
            raise RapportError("dsh_database_changing", "busy")
        return None, after if values["clicked"] else before, editor

    def click(*_):
        values["clicked"] += 1
        values["value"] = ""

    state = object()
    monkeypatch.setattr(dispatch, "read_dispatch_chat", read)
    monkeypatch.setattr(dispatch, "click_database_send", click)
    monkeypatch.setattr(dispatch, "recipient_controls", lambda *_: NS(editor=editor))
    monkeypatch.setattr(dispatch, "verify_text_append", lambda *_: pytest.fail("no OCR receipt"))
    monkeypatch.setattr(native, "_input_value", lambda _: values["value"])
    monkeypatch.setattr(native.navigation, "assert_focus", lambda *_: None)
    monkeypatch.setattr(media, "image_controls", lambda _: [])
    monkeypatch.setattr(dispatch.WindowsDispatchAdapter, "_send_button", lambda *_: button)
    session = dispatch._WindowsTextSession(auto, root, ticket, 100, before, state)
    assert session.send({"kind": "text", "text": "测试 {Enter}"})
    assert values["clicked"] == 1 and values["pending"] == 1
