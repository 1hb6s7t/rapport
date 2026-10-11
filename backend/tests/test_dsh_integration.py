import threading
import time
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path

import pytest

from rapport.desktop.dsh_source import DBAdapter, DshService
from rapport.desktop.dsh_worker import DesktopWorker
from rapport.errors import RapportError

ACCOUNT = "a" * 32


def row(seq=1, local=1, shard="message/message_0.db", user="wxid_friend", text="你好", kind=1, server=""):
    return {"sort_seq": seq, "local_id": local, "shard": shard, "sender_username": user,
            "sender_id": 2, "content": text, "raw_type": kind, "server_id": server, "create_time": 1700000000 + seq}


class Reader:
    def __init__(self):
        self.rows = [row()]
        self.closed = False
        self.calls = []

    def get_self_info(self):
        return {"username": "wxid_me", "nick_name": "我的昵称"}

    def search_contact(self, name):
        return [{"username": "wxid_friend", "remark": "测试好友", "nick_name": "别名"}]

    def fetch(self, user, limit, cursors=None):
        self.calls.append((user, deepcopy(cursors)))
        available = [r for r in self.rows if cursors is None or [r["sort_seq"], r["local_id"]] > cursors.get(r["shard"], [0, 0])]
        available.sort(key=lambda r: (r["sort_seq"], r["shard"], r["local_id"]), reverse=cursors is None)
        available = available[:limit]
        boundary = deepcopy(cursors or {})
        for r in available:
            boundary[r["shard"]] = max(boundary.get(r["shard"], [0, 0]), [r["sort_seq"], r["local_id"]])
        return deepcopy(available), boundary

    def close(self):
        self.closed = True

    def assert_process_account(self, process_id):
        assert process_id == 22


def install_service(client, reader=None):
    reader = reader or Reader()
    state = client.app_state
    state.dsh = DshService(state, accounts=lambda: [{"id": ACCOUNT, "label": "本机账号", "path": Path("account")}],
                           opener=lambda *args: reader)
    return state.dsh, reader


def test_sender_two_is_not_assumed_to_be_self():
    adapter = DBAdapter(Reader(), ACCOUNT)
    assert adapter.convert(row(user="wxid_friend"), "wxid_friend").sender.value == "other"
    assert adapter.convert(row(user="wxid_me"), "wxid_friend").sender.value == "me"
    assert adapter.convert(row(user=""), "wxid_friend").sender.value == "unknown"


def test_exact_unique_private_recipient_is_required():
    reader = Reader()
    adapter = DBAdapter(reader, ACCOUNT)
    assert adapter.resolve_contact("测试好友") == "wxid_friend"
    with pytest.raises(RapportError):
        adapter.resolve_contact("测试")
    reader.search_contact = lambda _: [{"username": "a", "remark": "同名"}, {"username": "b", "remark": "同名"}]
    with pytest.raises(RapportError):
        adapter.resolve_contact("同名")
    reader.search_contact = lambda _: [{"username": "a@chatroom", "remark": "同名"}]
    with pytest.raises(RapportError):
        adapter.resolve_contact("同名")


@pytest.mark.parametrize("subtype,kind", [("2000", "transfer"), ("2001", "transfer"), ("6", "file"), ("5", "link")])
def test_card_labels_are_not_chat_text(subtype, kind):
    message = DBAdapter(Reader(), ACCOUNT).convert(row(kind=49, text=f"<msg><appmsg><type>{subtype}</type><title>已收款</title></appmsg></msg>"), "wxid_friend")
    assert message.type.value == kind and message.text is None


def test_quote_and_unavailable_body_are_distinct():
    adapter = DBAdapter(Reader(), ACCOUNT)
    quote = adapter.convert(row(kind=49, text="<msg><appmsg><type>57</type><title>回复</title><refermsg><content>引用</content></refermsg></appmsg></msg>"), "wxid_friend")
    assert quote.text == "回复" and quote.quoted_text == "引用"
    assert adapter.convert(row(text="[文本]"), "wxid_friend").type.value == "unknown"


def test_message_ids_include_account_chat_and_shard_identity():
    adapter = DBAdapter(Reader(), ACCOUNT)
    first = adapter.convert(row(), "wxid_friend")
    assert first.id != adapter.convert(row(shard="message/message_1.db"), "wxid_friend").id
    assert first.id != adapter.convert(row(), "wxid_other").id
    assert first.id != DBAdapter(Reader(), "b" * 32).convert(row(), "wxid_friend").id
    assert adapter.convert(row(server="100"), "wxid_friend").id == adapter.convert(row(shard="message/message_1.db", server="100"), "wxid_friend").id


def test_incremental_batch_is_encrypted_and_resumes_after_recreation(app_client):
    service, reader = install_service(app_client)
    first = service.read(ACCOUNT, "测试好友", "default", 100)
    stored = service._path(first["snapshot_id"]).read_bytes()
    assert b'wxid_friend' not in stored and "你好".encode() not in stored
    reader.rows += [row(seq=1, local=2), row(seq=1, local=1, shard="message/message_1.db")]
    service, _ = install_service(app_client, reader)
    result = service.read(ACCOUNT, "测试好友", "default", 100, snapshot_id=first["snapshot_id"])
    assert result["added"] == 2 and len(result["messages"]) == 3
    again = service.read(ACCOUNT, "测试好友", "default", 100, snapshot_id=first["snapshot_id"])
    assert again["added"] == 0 and again["revision"] == result["revision"]


def test_failed_persistence_does_not_advance_cursor(app_client, monkeypatch):
    service, reader = install_service(app_client)
    first = service.read(ACCOUNT, "测试好友", "default", 100)
    reader.rows.append(row(seq=2, local=2))
    original = service.save
    monkeypatch.setattr(service, "save", lambda _: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        service.read(ACCOUNT, "测试好友", "default", 100, snapshot_id=first["snapshot_id"])
    assert service.load(first["snapshot_id"], "default")["revision"] == first["revision"]
    monkeypatch.setattr(service, "save", original)
    assert service.read(ACCOUNT, "测试好友", "default", 100, snapshot_id=first["snapshot_id"])["added"] == 1


def test_cancel_does_not_commit_or_read_messages(app_client):
    service, reader = install_service(app_client)
    cancel = threading.Event()
    cancel.set()
    assert service.read(ACCOUNT, "测试好友", "default", 100, cancel=cancel) is None
    assert reader.calls == []


def test_api_default_database_source_scope_preview_and_idempotent_import(app_client, monkeypatch):
    client = app_client
    service, reader = install_service(client)
    monkeypatch.setattr(client.app_state.desktop, "run", lambda *_: pytest.fail("database history must not use the desktop reader"))
    body = {"account": ACCOUNT, "contact": "测试好友", "relationship_id": "default", "limit": 100}
    config = client.get("/api/wechat/dsh/config").json()
    assert config["enabled"] is True and config["connected"] is None
    assert "path" not in client.get("/api/wechat/dsh/accounts").json()["accounts"][0]
    assert service.adapter is None and reader.calls == []
    job = client.post("/api/wechat/dsh/read", json=body).json()
    for _ in range(100):
        result = client.get("/api/wechat/dsh/jobs/" + job["id"]).json()
        if result["status"] != "running":
            break
        time.sleep(.01)
    assert result["status"] == "completed"
    assert reader.calls == [("wxid_friend", None)]
    result = result["result"]
    assert result["sent"] is False
    snapshot = {k: result[k] for k in ("snapshot_id", "revision", "relationship_id")}
    assert client.post("/api/wechat/dsh/import", json={**snapshot, "revision": "f" * 64}).status_code == 409
    imported = client.post("/api/wechat/dsh/import", json=snapshot)
    assert imported.status_code == 200 and imported.json()["status"] != "confirmed"
    assert client.post("/api/wechat/dsh/import", json=snapshot).json()["id"] == imported.json()["id"]
    with pytest.raises(RapportError):
        service.load(result["snapshot_id"], "different-relationship")


def test_explicit_database_disable_closes_connection_and_survives_recreation(app_client):
    service, reader = install_service(app_client)
    body = {"account": ACCOUNT, "contact": "测试好友", "relationship_id": "default", "limit": 100}
    service.read(ACCOUNT, "测试好友", "default", 100)
    calls = list(reader.calls)
    response = app_client.patch("/api/wechat/dsh/config", json={"enabled": False})
    assert response.status_code == 200
    assert response.json()["enabled"] is False and response.json()["connected"] is None
    assert reader.closed and service.adapter is None

    service, _ = install_service(app_client, reader)
    assert app_client.get("/api/wechat/dsh/config").json()["enabled"] is False
    blocked = app_client.post("/api/wechat/dsh/read", json=body)
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "dsh_disabled"
    assert app_client.get("/api/wechat/dsh/accounts").status_code == 409
    assert reader.calls == calls and service.adapter is None

    assert app_client.patch("/api/wechat/dsh/config", json={"enabled": True}).json()["enabled"] is True
    assert reader.calls == calls and service.adapter is None


def test_worker_preserves_exact_content_serializes_and_does_not_retry():
    worker = DesktopWorker(context_factory=nullcontext)
    calls = []
    long_text = "完整文字" * 1000
    try:
        def operation(text):
            calls.append((threading.get_ident(), text))
            return text
        assert worker.run(operation, long_text) == long_text
        assert worker.run(operation, "第二条") == "第二条"
        assert len({call[0] for call in calls}) == 1
        assert calls[0][0] != threading.get_ident()
        attempts = []
        def fail():
            attempts.append(1)
            raise RuntimeError("uncertain")
        with pytest.raises(RuntimeError):
            worker.run(fail)
        assert attempts == [1]
    finally:
        worker.close()
    with pytest.raises(RapportError):
        worker.run(operation, "不可执行")


def test_database_reply_binding_uses_fresh_visible_context_and_existing_material(app_client, monkeypatch):
    from rapport.desktop import wechat_native
    from rapport.desktop.wechat import context_digest
    from rapport.schemas.message_v1 import Conversation, Message, Sender, Source

    service, _ = install_service(app_client)
    app_client.patch("/api/wechat/dsh/config", json={"enabled": True})
    result = service.read(ACCOUNT, "测试好友", "default", 100)
    body = {k: result[k] for k in ("snapshot_id", "revision", "relationship_id")}
    assert app_client.post("/api/wechat/dsh/bind", json=body).status_code == 409
    material = app_client.post("/api/wechat/dsh/import", json=body).json()
    visible = Conversation(id="visible", source=Source.ocr, messages=[Message(id="v1", seq=1, sender=Sender.other, text="最新消息")])
    monkeypatch.setattr(wechat_native, "capture_contact", lambda *_: wechat_native.NativeCapture(b"preview", visible, 11, 22))
    bound = app_client.post("/api/wechat/dsh/bind", json={**body, "visual": True})
    assert bound.status_code == 200 and bound.json()["import_id"] == material["id"]
    ticket = app_client.app_state.wechat_captures.items[bound.json()["token"]]
    assert ticket.digest == context_digest(visible)
    assert ticket.conversation.messages[0].text == "你好"
    assert ticket.import_id == material["id"] and not ticket.filled


def test_relationship_cleanup_revokes_dsh_snapshot(app_client):
    service, reader = install_service(app_client)
    result = service.read(ACCOUNT, "测试好友", "default", 100)
    service.clear("default")
    assert reader.closed
    with pytest.raises(RapportError):
        service.load(result["snapshot_id"], "default")
