from __future__ import annotations

import io
import json
import struct
import uuid
import zipfile
from datetime import datetime, timedelta

import pytest

from rapport.archives import get_service
from rapport.archives.chunks import CHUNK_SIZE, MAGIC, read_chunks, write_chunks
from rapport.db import Import
from rapport.errors import RapportError
from rapport.schemas.message_v1 import Conversation, Message, MsgType, Sender, Source
from rapport.security.crypto import Crypto


def archive_fixture(client, relationship_id="default", text='<script>alert("private")</script>'):
    state = client.app_state
    conv = Conversation(id=uuid.uuid4().hex[:16], source=Source.generic_json, messages=[
        Message(id="m000001", seq=0, sender=Sender.other, text=text, raw="secret-source-path", meta={"filename": "D:/secret.key"}),
        Message(id="m000002", seq=1, sender=Sender.me, type=MsgType.image),
    ])
    imported = state.save_import(conv, "test", "private-contact-name.json", relationship_id)
    assert client.post(f"/api/imports/{imported.id}/confirm", json={}).status_code == 200
    service = get_service(state)
    return service, service.create(imported.id, relationship_id), imported


def test_chunk_encryption_roundtrip_and_no_plaintext(tmp_path):
    crypto = Crypto(tmp_path / ".key")
    path = tmp_path / "stream.enc"
    blocks = [b"private-data" * 200_000, b"tail"]
    size, digest = write_chunks(path, crypto, blocks, 5_000_000)
    assert size == sum(map(len, blocks))
    assert len(digest) == 64
    assert b"private-data" not in path.read_bytes()
    assert b"".join(read_chunks(path, crypto)) == b"".join(blocks)
    assert max(map(len, read_chunks(path, crypto))) <= CHUNK_SIZE


@pytest.mark.parametrize("corruption", ["truncate", "reorder", "append", "bitflip", "stream_splice"])
def test_chunk_stream_rejects_corruption(tmp_path, corruption):
    crypto = Crypto(tmp_path / ".key")
    path = tmp_path / "stream.enc"
    write_chunks(path, crypto, [b"A" * CHUNK_SIZE, b"B" * CHUNK_SIZE], 3 * CHUNK_SIZE)
    data = path.read_bytes()
    offset = len(MAGIC) + 16
    first_size = struct.unpack(">I", data[offset : offset + 4])[0] + 4
    if corruption == "truncate":
        data = data[:-10]
    elif corruption == "reorder":
        second_size = struct.unpack(">I", data[offset + first_size : offset + first_size + 4])[0] + 4
        data = data[:offset] + data[offset + first_size : offset + first_size + second_size] + data[offset : offset + first_size] + data[offset + first_size + second_size :]
    elif corruption == "append":
        data += b"extra"
    elif corruption == "bitflip":
        data = data[:offset + 40] + b"!" + data[offset + 41 :]
    else:
        other = tmp_path / "other.enc"
        write_chunks(other, crypto, [b"C" * CHUNK_SIZE], 2 * CHUNK_SIZE)
        data = data[:offset] + other.read_bytes()[offset:]
    path.write_bytes(data)
    with pytest.raises(ValueError):
        list(read_chunks(path, crypto))


def test_chunk_size_limit_leaves_no_partial_file(tmp_path):
    path = tmp_path / "stream.enc"
    with pytest.raises(ValueError, match="too_large"):
        write_chunks(path, Crypto(tmp_path / ".key"), [b"12345"], 4)
    assert not path.exists()
    assert not path.with_suffix(".part").exists()


def test_archive_confirmed_scope_dedupe_and_stable_references(app_client):
    service, archive, imported = archive_fixture(app_client)
    assert service.create(imported.id, "default")["id"] == archive["id"]
    assert archive["message_count"] == 2
    messages = service.messages(archive["id"], "default")
    assert all(m["message_ref"].startswith(archive["id"] + ":") for m in messages)
    assert "raw" not in messages[0] and "meta" not in messages[0]
    other = app_client.app_state.relationship_create("Another")
    with pytest.raises(RapportError) as error:
        service.create(imported.id, other["id"])
    assert error.value.status == 404
    with pytest.raises(RapportError):
        service.messages(archive["id"], other["id"])
    assert service.search(other["id"], "private") == []
    assert service.search("default", "private")[0]["retrieval_method"] == "lexical-v1"
    for path in service.root.rglob("*"):
        if path.is_file():
            assert b"<script>alert" not in path.read_bytes()
            assert b"private-contact-name" not in path.read_bytes()


def test_archive_unconfirmed_and_expired_are_refused(app_client):
    state = app_client.app_state
    conv = Conversation(id="unconfirmed", source=Source.paste, messages=[Message(id="m1", seq=1, sender=Sender.me, text="hello")])
    imported = state.save_import(conv, "test", None)
    with pytest.raises(RapportError) as error:
        get_service(state).create(imported.id, "default")
    assert error.value.code == "archive_import_unconfirmed"
    app_client.post(f"/api/imports/{imported.id}/confirm", json={})
    state.blobs.delete(imported.blob_id)
    with pytest.raises(RapportError) as error:
        get_service(state).create(imported.id, "default")
    assert error.value.code == "archive_import_expired"


def test_archive_survives_temporary_retention_purge(app_client):
    service, archive, imported = archive_fixture(app_client)
    with app_client.app_state.db.session() as session:
        row = session.get(Import, imported.id)
        row.expires_at = datetime.now() - timedelta(hours=25)
    assert app_client.app_state.purge_expired() == 1
    assert not app_client.app_state.blobs.exists(imported.blob_id)
    assert len(service.messages(archive["id"], "default")) == 2


def test_asset_missing_thumbnail_original_and_filename_handling(app_client):
    service, archive, _ = archive_fixture(app_client)
    missing = service.assets(archive["id"], "default")[0]
    assert missing["status"] == "missing"
    with pytest.raises(RapportError):
        service.read_asset(missing["id"], "default")
    png = b"\x89PNG\r\n\x1a\n" + b"image-data"
    thumb = service.add_asset(archive["id"], "default", missing["message_ref"], io.BytesIO(png), "../../evil.png", status="thumbnail")
    assert thumb["filename"] == "evil.png"
    assert {a["status"] for a in service.assets(archive["id"], "default")} == {"missing", "thumbnail"}
    original = service.add_asset(archive["id"], "default", missing["message_ref"], io.BytesIO(png + b"original"), "original.png")
    assert {a["status"] for a in service.assets(archive["id"], "default")} == {"original", "thumbnail"}
    assert service.read_asset(original["id"], "default") == png + b"original"
    duplicate = service.add_asset(archive["id"], "default", missing["message_ref"], io.BytesIO(png + b"original"), "duplicate.png")
    assert duplicate["id"] == original["id"]
    assert len(service.list_assets_for_relationship("default")) == 1
    with pytest.raises(RapportError):
        service._path("../../outside")
    with pytest.raises(RapportError):
        service.add_asset(archive["id"], "default", "other:000001", io.BytesIO(png), "a.png")
    with pytest.raises(RapportError):
        service.add_asset(archive["id"], "default", missing["message_ref"], io.BytesIO(b"<html>"), "a.png")


def test_bounded_video_and_asset_deletion(app_client, monkeypatch):
    service, archive, _ = archive_fixture(app_client)
    message_ref = service.messages(archive["id"], "default")[1]["message_ref"]
    import rapport.archives.store as module
    monkeypatch.setattr(module, "MAX_ASSET_BYTES", 50)
    with pytest.raises(RapportError) as error:
        service.add_asset(archive["id"], "default", message_ref, io.BytesIO(b"\x00\x00\x00\x18ftypmp42" + b"x" * 100), "movie.mp4", kind="video")
    assert error.value.status == 413
    assert not list(service.objects.glob("*.part"))
    asset = service.add_asset(archive["id"], "default", message_ref, io.BytesIO(b"\x00\x00\x00\x18ftypmp42body"), "movie.mp4", kind="video")
    assert asset["mime"] == "video/mp4"
    with pytest.raises(RapportError):
        service.read_asset(asset["id"], "default", max_bytes=2)
    service.delete(archive["id"], "default")
    assert not list(service.objects.glob("*.enc"))


def test_export_confirmation_scoped_expiring_and_snapshot_bound(app_client):
    service, archive, _ = archive_fixture(app_client)
    preview = service.export_preview(archive["id"], "default", "browser")
    assert preview["manifest"]["plaintext"] is True
    for owner, confirmed, include_media in [("other-browser", True, False), ("browser", False, False), ("browser", True, True)]:
        with pytest.raises(RapportError):
            service.export_confirm(archive["id"], "default", owner, preview["token"], include_media, confirmed)
    service.tickets[preview["token"]]["expires"] = 0
    with pytest.raises(RapportError):
        service.export_confirm(archive["id"], "default", "browser", preview["token"], False, True)
    preview = service.export_preview(archive["id"], "default", "browser")
    message_ref = service.messages(archive["id"], "default")[1]["message_ref"]
    service.add_asset(archive["id"], "default", message_ref, io.BytesIO(b"GIF89afake"), "sticker.gif", kind="sticker")
    with pytest.raises(RapportError):
        service.export_confirm(archive["id"], "default", "browser", preview["token"], False, True)


@pytest.mark.parametrize("include_media", [False, True])
def test_export_zip_is_viewable_escaped_and_does_not_leak_config(app_client, include_media):
    service, archive, _ = archive_fixture(app_client)
    message_ref = service.messages(archive["id"], "default")[1]["message_ref"]
    asset = service.add_asset(archive["id"], "default", message_ref, io.BytesIO(b"GIF89afake"), "../bad<script>.gif", kind="sticker")
    preview = service.export_preview(archive["id"], "default", "browser", include_media)
    data = b"".join(service.export_confirm(archive["id"], "default", "browser", preview["token"], include_media, True))
    with zipfile.ZipFile(io.BytesIO(data)) as archive_zip:
        assert {"messages.jsonl", "viewer.html", "media-manifest.json"} <= set(archive_zip.namelist())
        assert not any(".." in name or name.startswith("/") or ".key" in name for name in archive_zip.namelist())
        viewer = archive_zip.read("viewer.html").decode()
        assert "<script>" not in viewer
        assert "&lt;script&gt;" in viewer
        assert "D:/secret" not in viewer
        messages = [json.loads(row) for row in archive_zip.read("messages.jsonl").splitlines()]
        assert "raw" not in messages[0] and "meta" not in messages[0]
        media = [name for name in archive_zip.namelist() if name.startswith("media/")]
        assert bool(media) is include_media
        if include_media:
            assert archive_zip.read(f"media/{asset['id']}.gif") == b"GIF89afake"
    with pytest.raises(RapportError):
        service.export_confirm(archive["id"], "default", "browser", preview["token"], include_media, True)


def test_archive_api_auth_csrf_scope_and_export(app_client):
    service, archive, imported = archive_fixture(app_client)
    client = app_client
    response = client.get("/api/archives", params={"relationship_id": "default"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["items"][0]["id"] == archive["id"]
    response = client.post("/api/archives", json={"import_id": imported.id, "relationship_id": "default"})
    assert response.json()["id"] == archive["id"]
    response = client.get(f"/api/archives/{archive['id']}", params={"relationship_id": "default", "page_size": 1})
    assert len(response.json()["items"]) == 1 and response.json()["total"] == 2
    other = client.app_state.relationship_create("Other")
    assert client.get(f"/api/archives/{archive['id']}", params={"relationship_id": other["id"]}).status_code == 404
    prefix = f"/api/archives/{archive['id']}"
    body = {"relationship_id": "default", "include_media": False}
    preview = client.post(prefix + "/export-preview", json=body)
    token = preview.json()["token"]
    assert client.post(prefix + "/export", json={**body, "token": token}).status_code == 422
    exported = client.post(prefix + "/export", json={**body, "token": token, "confirmed": True})
    assert exported.status_code == 200 and exported.content.startswith(b"PK")
    csrf = client.headers.pop("x-rapport-csrf")
    assert client.delete(prefix, params={"relationship_id": "default"}).status_code == 403
    client.headers["x-rapport-csrf"] = csrf
    client.cookies.clear()
    assert client.get("/api/archives", params={"relationship_id": "default"}).status_code == 401
    assert service.get(archive["id"], "default")


def test_media_api_upload_download_scope_and_missing(app_client):
    service, archive, _ = archive_fixture(app_client)
    missing = service.assets(archive["id"], "default")[0]
    assert app_client.get(f"/api/archives/assets/{missing['id']}", params={"relationship_id": "default"}).status_code == 404
    upload = app_client.post(f"/api/archives/{archive['id']}/assets", data={"relationship_id": "default", "message_ref": missing["message_ref"], "kind": "sticker"}, files={"file": ("s.gif", b"GIF89afake", "text/html")})
    assert upload.status_code == 200
    asset = upload.json()
    assert asset["mime"] == "image/gif"
    download = app_client.get(f"/api/archives/assets/{asset['id']}", params={"relationship_id": "default"})
    assert download.content == b"GIF89afake"
    assert download.headers["x-content-type-options"] == "nosniff"
    assert download.headers["content-disposition"].startswith("attachment")
    other = app_client.app_state.relationship_create("Other")
    assert app_client.get(f"/api/archives/assets/{asset['id']}", params={"relationship_id": other["id"]}).status_code == 404


def test_cleanup_relationship_and_wipe_delete_encrypted_objects(app_client):
    service, archive, _ = archive_fixture(app_client)
    other = app_client.app_state.relationship_create("Other")
    _, another, _ = archive_fixture(app_client, other["id"])
    service.delete_relationship(other["id"])
    assert service.list(other["id"]) == []
    assert not service._path(another["id"]).exists()
    assert service._path(archive["id"]).exists()
    service.wipe_all()
    assert service.list("default") == []
    assert not list(service.objects.glob("*.enc"))
