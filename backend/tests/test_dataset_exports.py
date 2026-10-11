"""Fine-grained exports use synthetic records, never private corpus fixtures."""

from __future__ import annotations

import io
import json
import uuid
import zipfile

import pytest

from rapport.datasets import DatasetStore, exports
from rapport.datasets.corpus import POOLS, VERSION
from rapport.errors import RapportError
from rapport.security import Crypto


def sample(session, index, split, *, eligible=True):
    message_id = f"{session}:{index}"
    return {"id": message_id, "session_id": session, "conversation_id": f"{session}:c1", "friend_id": session + ":real-name",
            "_relationship_id": session + ":private-database-key", "target_index": index, "split": split, "time_split": split,
            "text": f"匿名内容 {index}", "response": f"本人回复 {index}", "training_eligible": eligible, "human_confirmed": eligible,
            "labels": {"emotion": ["开心"], "intent": ["调侃"], "private_label": "never-export"},
            "context": [{"id": f"{session}:0", "index": 0, "text": "匿名上下文", "sender": "other", "type": "text", "ts": "2026-01-01T12:00:00", "ts_precision": "second", "identity": "SECRET-NAME", "filename": "D:/secret/key"}],
            "source_ids": [f"{session}:0", message_id], "content_refs": [f"{session}:hash0", f"{session}:hash{index}"],
            "raw": "SECRET-RAW", "source_root": "D:/.rapport/private", "relationship_id": "SECRET-RELATIONSHIP",
            "source_paths": ["D:/private/file"], "identity_map": {"real": "name"}, "review": {"note": "SECRET-REVIEW"}}


def save_fixture(store):
    pools = {pool: [] for pool in POOLS}
    for session, split in (("session-a", "train"), ("session-b", "validation"), ("session-c", "test")):
        for index in range(1, 5):
            for pool in POOLS:
                pools[pool].append(sample(session, index, split, eligible=index != 4))
    dataset_id = uuid.uuid4().hex
    manifest = {"id": dataset_id, "status": "built", "version": VERSION, "created_at": "2026-01-01", "pools": {pool: len(rows) for pool, rows in pools.items()},
                "private_relationship_ids": ["SECRET-RELATIONSHIP"], "source_root": "D:/PRIVATE", "identity_map": {"secret": "name"}}
    store._save(manifest, {"pools": pools, "private_relationships": {"private": "SECRET"}})
    return dataset_id


@pytest.fixture
def local_store(tmp_path):
    store = DatasetStore(tmp_path / "datasets", Crypto(tmp_path / ".key"))
    return store, save_fixture(store)


def zip_rows(data):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        rows = [json.loads(line) for name in archive.namelist() if name.endswith(".jsonl") for line in archive.read(name).splitlines()]
        return manifest, rows, archive.namelist()


def test_options_only_expose_anonymous_groups(local_store):
    store, dataset_id = local_store
    opts = exports.options(store, dataset_id)
    assert len(opts["sessions"]) == len(opts["friends"]) == 3
    assert opts["splits"] == ["train", "validation", "test"]
    assert all(item["id"].startswith("f_") for item in opts["friends"])
    text = json.dumps(opts)
    assert "session-a" not in text and "real-name" not in text and "private-database" not in text


def test_exact_selection_cap_and_splits(local_store):
    store, dataset_id = local_store
    opts = exports.options(store, dataset_id)
    session = opts["sessions"][0]["id"]
    preview = store.export_preview(dataset_id, ["personal_reply"], selectors={"session_ids": [session], "splits": ["train"], "eligible_only": True, "max_samples": 2})
    assert preview["total"] == 2
    assert preview["manifest"]["counts"] == {"personal_reply": 2}
    _, rows, paths = zip_rows(store.export(dataset_id, preview["token"], True))
    assert rows == preview["items"]
    assert paths == ["manifest.json", "personal_reply/train.jsonl"]
    assert all(row["session_id"] == session and row["training_eligible"] for row in rows)


def test_explicit_sample_ids_can_use_existing_or_anonymous_id(local_store):
    store, dataset_id = local_store
    preview = store.export_preview(dataset_id, ["offline_review"], selectors={"sample_ids": ["session-a:2", "session-c:3"]})
    assert preview["total"] == 2
    assert {row["target_index"] for row in preview["items"]} == {2, 3}
    selected_ids = [row["id"] for row in preview["items"]]
    second = store.export_preview(dataset_id, ["offline_review"], selectors={"sample_ids": selected_ids})
    assert second["items"] == preview["items"]
    assert second["manifest"]["selection_sha256"] == preview["manifest"]["selection_sha256"]


def test_friend_and_conversation_filters_intersect(local_store):
    store, dataset_id = local_store
    opts = exports.options(store, dataset_id)
    preview = store.export_preview(dataset_id, ["personal_reply"], selectors={"friend_ids": [opts["friends"][0]["id"]], "conversation_ids": [opts["conversations"][0]["id"]]})
    assert preview["total"] == 4
    with pytest.raises(RapportError) as error:
        store.export_preview(dataset_id, ["personal_reply"], selectors={"friend_ids": [opts["friends"][0]["id"]], "conversation_ids": [opts["conversations"][1]["id"]]})
    assert error.value.code == "dataset_export_empty"


def test_privacy_projection_removes_nested_identity_binding_paths_notes(local_store):
    store, dataset_id = local_store
    preview = store.export_preview(dataset_id, list(POOLS))
    manifest, rows, _ = zip_rows(store.export(dataset_id, preview["token"], True))
    content = json.dumps({"manifest": manifest, "rows": rows})
    for secret in ("SECRET", "D:/", "private-database", "real-name", "source_root", "identity_map", "relationship_id", "source_paths", "private_label", "review"):
        # pool names contain review; field review must never occur.
        if secret == "review":
            assert '"review":' not in content
        else:
            assert secret not in content
    assert "匿名内容" in json.dumps(rows, ensure_ascii=False)
    assert all(row["id"].startswith("m_") for row in rows)
    assert all(context["id"].startswith("m_") for row in rows for context in row["context"])


def test_pagination_is_bound_to_exact_selection_and_browser(local_store):
    store, dataset_id = local_store
    preview = store.export_preview(dataset_id, ["personal_reply"], owner="browser-a", target_environment="独立训练机 A")
    first = exports.preview_page(store, dataset_id, preview["token"], owner="browser-a", page=0, page_size=5)
    second = exports.preview_page(store, dataset_id, preview["token"], owner="browser-a", page=1, page_size=5)
    assert first["total"] == second["total"] == 12
    assert set(row["id"] for row in first["items"]).isdisjoint(row["id"] for row in second["items"])
    with pytest.raises(RapportError):
        exports.preview_page(store, dataset_id, preview["token"], owner="other")
    with pytest.raises(RapportError):
        store.export(dataset_id, preview["token"], True, owner="browser-a", target_environment="其他环境")
    _, rows, _ = zip_rows(store.export(dataset_id, preview["token"], True, owner="browser-a", target_environment="独立训练机 A"))
    assert rows[:5] == first["items"] and rows[5:10] == second["items"]


@pytest.mark.parametrize("selectors", [{"splits": ["invalid"]}, {"session_ids": ["unknown"]}, {"friend_ids": ["unknown"]}, {"sample_ids": ["unknown"]}, {"max_samples": 0}, {"max_samples": 100001}, {"unexpected": True}])
def test_invalid_selectors_fail_closed(local_store, selectors):
    store, dataset_id = local_store
    with pytest.raises(RapportError):
        store.export_preview(dataset_id, ["personal_reply"], selectors=selectors)


@pytest.mark.parametrize("kind", ["source", "content", "friend", "session", "conversation"])
def test_cross_split_leakage_cannot_be_hidden_by_cap(local_store, kind):
    store, dataset_id = local_store
    private = store._load(dataset_id)
    train, test = private["pools"]["personal_reply"][0], private["pools"]["personal_reply"][-1]
    if kind == "source":
        test["source_ids"] = train["source_ids"]
    elif kind == "content":
        test["content_refs"] = train["content_refs"]
    elif kind == "friend":
        test["_relationship_id"] = train["_relationship_id"]
    elif kind == "session":
        test["session_id"] = train["session_id"]
    else:
        test["conversation_id"] = train["conversation_id"]
    manifest = {**store.get(dataset_id), "id": uuid.uuid4().hex}
    store._save(manifest, private)
    with pytest.raises(RapportError) as error:
        store.export_preview(manifest["id"], ["personal_reply"], selectors={"max_samples": 1})
    assert error.value.code == "dataset_split_leakage"


def test_manifest_change_token_tamper_and_expiry_refused(local_store, monkeypatch):
    store, dataset_id = local_store
    preview = store.export_preview(dataset_id, ["personal_reply"])
    with pytest.raises(RapportError):
        store.export(dataset_id, preview["token"][:-8] + "tampered", True)
    monkeypatch.setattr("rapport.datasets.exports.time.time", lambda: 9999999999)
    with pytest.raises(RapportError):
        store.export(dataset_id, preview["token"], True)


def test_legacy_pools_only_export_remains_compatible(local_store):
    store, dataset_id = local_store
    preview = store.export_preview(dataset_id, ["personal_reply"])
    manifest, rows, paths = zip_rows(store.export(dataset_id, preview["token"], True))
    assert len(rows) == 12 and len(paths) == 4
    assert manifest["exported_pools"] == ["personal_reply"]
    assert manifest["local_download_only"] is True


def test_api_bound_preview_page_environment_and_auth(app_client):
    store = DatasetStore(app_client.app_state.settings.data_dir / "datasets", app_client.app_state.crypto)
    dataset_id = save_fixture(store)
    prefix = f"/api/datasets/{dataset_id}"
    opts = app_client.get(prefix + "/export-options")
    assert opts.status_code == 200 and opts.headers["cache-control"] == "no-store"
    body = {"pools": ["personal_reply"], "splits": ["train"], "max_samples": 2, "eligible_only": True, "target_environment": "local-test"}
    preview = app_client.post(prefix + "/export-preview", json=body)
    assert preview.status_code == 200 and preview.json()["total"] == 2
    token = preview.json()["token"]
    page = app_client.post(prefix + "/export-preview/page", json={"token": token, "page_size": 1})
    assert page.status_code == 200 and len(page.json()["items"]) == 1
    assert app_client.post(prefix + "/export", json={"token": token, "confirmed": True}).status_code == 409
    exported = app_client.post(prefix + "/export", json={"token": token, "confirmed": True, "target_environment": "local-test"})
    assert exported.status_code == 200 and len(zip_rows(exported.content)[1]) == 2
    assert app_client.post(prefix + "/export-preview", json={**body, "max_samples": 0}).status_code == 422
    old_csrf = app_client.headers.pop("x-rapport-csrf")
    assert app_client.post(prefix + "/export-preview/page", json={"token": token}).status_code == 403
    app_client.headers["x-rapport-csrf"] = old_csrf
    app_client.cookies.clear()
    assert app_client.get(prefix + "/export-options").status_code == 401
