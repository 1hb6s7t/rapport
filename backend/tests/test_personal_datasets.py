"""Synthetic local corpus tests; no private fixtures, network or real conversations."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rapport.api.datasets import router
from rapport.datasets import DatasetStore
from rapport.datasets.corpus import build_records, informative_reply, load_corpus, validate_splits
from rapport.datasets.credentials import REDACTED, redact_text
from rapport.errors import RapportError
from rapport.security import Crypto


@pytest.fixture
def corpus_root(tmp_path):
    root = tmp_path / "readonly" / "eval"
    (root / "redacted").mkdir(parents=True)
    for s in range(4):
        name = f"private-name-{s}"
        messages = []
        anns = []
        for i in range(1, 9):
            mine = i % 2 == 0
            text = f"合成消息{s}-{i}"
            messages.append({"type": "文本消息", "content": text, "isSend": int(mine), "createTime": 1700000000 + s * 86400 + i * 30})
            if not mine:
                anns.append({"index": i, "speaker": "other", "text": text,
                             "pragmatic_tags": ["调侃"], "emotions": ["开心"],
                             "needs_review": i == 7, "media_unknown": False,
                             "annotation_basis": {"evidence_indices": [i + 1] if i == 3 else [i]}})
        (root / "redacted" / f"{name}.json").write_text(json.dumps({"session": {}, "messages": messages}), encoding="utf-8")
        ann = root / "annotations" / name
        ann.mkdir(parents=True)
        (ann / "batch_001.json").write_text(json.dumps({"semantic_review_version": "context-v4", "messages": anns}), encoding="utf-8")
    (root / "redacted" / "_alias-table.json").write_text("MUST NEVER READ invalid JSON", encoding="utf-8")
    (root / "gold").mkdir()
    (root / "gold" / "cases-v2.json").write_text(json.dumps({"schema": "mapped-v2", "cases": [{"stratum": "gold"}]}))
    (root / "gold" / "cases-v1.json").write_text("MUST NEVER READ invalid JSON")
    return root


@pytest.fixture
def store(tmp_path):
    return DatasetStore(tmp_path / "app" / "datasets", Crypto(tmp_path / "app" / ".key"))


def hashes(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()}


def test_wipe_only_app_snapshots_invalidates_inflight_writer(store, corpus_root):
    before = hashes(corpus_root)
    snapshot = store.build(corpus_root)
    seen = []
    result = store.wipe_all(before_delete=seen.append)
    assert result["deleted"] == 2 and snapshot["id"] in seen
    assert store.list() == [] and hashes(corpus_root) == before
    with pytest.raises(RapportError, match="资料已清空"):
        store._save({"id": "a" * 32}, {})


def test_wipe_refuses_unknown_files_without_partial_delete(store, corpus_root):
    store.build(corpus_root)
    extra = store.root / "unrelated.txt"
    extra.write_text("keep")
    with pytest.raises(RapportError):
        store.wipe_all()
    assert extra.read_text() == "keep" and len(store.list()) == 2


def test_refresh_unchanged_source_reuses_immutable_snapshot(store, corpus_root):
    snapshot = store.build(corpus_root)
    assert store.refresh(snapshot["id"])["id"] == snapshot["id"]
    assert len(store.list()) == 2


def test_credential_is_redacted_and_nearby_reply_samples_quarantined(corpus_root, store):
    path = next((corpus_root / "redacted").glob("private-name-0.json"))
    source = json.loads(path.read_text(encoding="utf-8"))
    sentinel = "sk-" + "A" * 35  # Synthetic token, never an actual credential.
    source["messages"][2]["content"] = "测试凭证 " + sentinel
    path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
    before = hashes(corpus_root)
    built = store.build(corpus_root)
    assert built["counts"]["credential_redactions"] == 1
    assert hashes(corpus_root) == before
    rows = {pool: store.records(built["id"], pool) for pool in built["pools"]}
    assert sentinel not in json.dumps(rows, ensure_ascii=False)
    assert REDACTED in json.dumps(rows["offline_review"], ensure_ascii=False)
    assert all(sentinel not in json.dumps(row, ensure_ascii=False) for row in rows["personal_reply"])
    assert len(rows["personal_reply"]) < 16
    assert all(not row["training_eligible"] for row in rows["offline_review"] if row.get("credential_redacted"))
    assert redact_text("普通聊天")[1] is False


def test_short_placeholder_replies_remain_testable_but_do_not_train():
    assert not informative_reply("好")
    assert not informative_reply("[表情]")
    assert not informative_reply("[链接]https://example.invalid")
    assert informative_reply("那我们明天再讨论一下")


def test_jwt_shaped_credentials_are_removed_from_derived_text():
    credential = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0LXVzZXIifQ.signature123456789"
    cleaned, found = redact_text("不要分享：" + credential)
    assert found and credential not in cleaned and REDACTED in cleaned


def test_tp_shaped_credential_is_quarantined_in_dataset_and_outbound(corpus_root, store):
    from rapport.llm.redact import redact_text as outbound_redact

    path = corpus_root / "redacted" / "private-name-0.json"
    source = json.loads(path.read_text(encoding="utf-8"))
    sentinel = "tp-" + "SyntheticToken123" * 3
    source["messages"][2]["content"] = sentinel
    path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
    before = hashes(corpus_root)
    built = store.build(corpus_root)
    assert built["counts"]["credential_redactions"] == 1
    rows = store.records(built["id"], "personal_reply")
    assert not any(row["text"] == "合成消息0-4" for row in rows)
    assert sentinel not in json.dumps({p: store.records(built["id"], p) for p in built["pools"]})
    assert outbound_redact(sentinel) == REDACTED
    assert hashes(corpus_root) == before
    assert redact_text("tp-short-name")[1] is False


def test_personal_reply_targets_only_first_self_message_after_incoming(corpus_root, store):
    path = corpus_root / "redacted" / "private-name-0.json"
    source = json.loads(path.read_text(encoding="utf-8"))
    source["messages"][2]["isSend"] = 1  # Three consecutive self messages now end at index 4.
    path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
    built = store.build(corpus_root)
    rows = store.records(built["id"], "personal_reply")
    assert all(row["context"][-1]["sender"] == "other" for row in rows)
    assert not any(row["text"] in {"合成消息0-3", "合成消息0-4"} for row in rows)
    assert len(rows) == 15


def test_media_augmentation_checks_credentials_before_cropping(corpus_root):
    path = corpus_root / "redacted" / "private-name-0.json"
    source = json.loads(path.read_text(encoding="utf-8"))
    source["messages"][1].update(type="图片消息", content="[图片]", isSend=1)
    source["messages"][2].update(isSend=1)
    source["messages"][3].update(isSend=0)
    source["messages"][4].update(isSend=1, content="你可以再试一次呀")
    path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
    corpus = load_corpus(corpus_root)
    sid = next(s["id"] for s in corpus["sessions"] if s["messages"][4]["text"] == "你可以再试一次呀")
    original = build_records(corpus)[0]["personal_reply"]
    expanded = build_records(corpus, allow_media_prefix=True)[0]["personal_reply"]
    assert not any(r["session_id"] == sid and r["target_index"] == 5 for r in original)
    added = next(r for r in expanded if r["session_id"] == sid and r["target_index"] == 5)
    assert [m["index"] for m in added["context"]] == [3, 4]
    source["messages"][0]["content"] = "tp-" + "SyntheticToken123" * 3
    path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
    protected = build_records(load_corpus(corpus_root), allow_media_prefix=True)[0]["personal_reply"]
    assert not any(r["text"].startswith("你可以再试一次呀") for r in protected)


def test_old_policy_audit_cannot_build_or_export(store, corpus_root):
    audit = store.audit(corpus_root)
    audit_file = store._path(audit["id"]) / "manifest.json"
    old = json.loads(audit_file.read_text(encoding="utf-8"))
    old["version"] = "personal-dataset-v1"
    audit_file.write_text(json.dumps(old), encoding="utf-8")
    with pytest.raises(RapportError, match="旧的"):
        store.build(audit_id=audit["id"])
    current = store.build(corpus_root)
    current_file = store._path(current["id"]) / "manifest.json"
    manifest = json.loads(current_file.read_text(encoding="utf-8"))
    manifest["version"] = "personal-dataset-v1"
    current_file.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RapportError, match="旧快照"):
        store.export_preview(current["id"], ["personal_reply"])


def test_refresh_changed_annotation_preserves_old_snapshot(store, corpus_root):
    first = store.build(corpus_root)
    old_rows = store.records(first["id"], "offline_review")
    path = next((corpus_root / "annotations").glob("*/batch_001.json"))
    source = json.loads(path.read_text(encoding="utf-8"))
    source["messages"][0]["needs_review"] = True
    path.write_text(json.dumps(source), encoding="utf-8")
    fresh = store.refresh(first["id"])
    assert fresh["id"] != first["id"] and fresh["provenance"] != first["provenance"]
    assert store.records(first["id"], "offline_review") == old_rows


def test_authoritative_audit_anonymous_provenance_read_only(corpus_root, store):
    before = hashes(corpus_root)
    audit = store.audit(corpus_root)
    assert audit["counts"]["messages"] == 32
    assert audit["counts"]["annotations"] == 16
    assert audit["counts"]["future_evidence"] == 4
    assert audit["counts"]["needs_review"] == 4
    assert audit["quality"]["human_gold"] is False
    assert "private-name" not in json.dumps(audit)
    assert hashes(corpus_root) == before
    assert b"\xe5\x90\x88\xe6\x88\x90\xe6\xb6\x88\xe6\x81\xaf" not in (store._path(audit["id"]) / "snapshot.bin").read_bytes()


def test_causal_pool_not_implicitly_human_gold(corpus_root, store):
    built = store.build(corpus_root)
    assert built["pools"]["offline_review"] == 16
    assert built["pools"]["realtime_review"] == 8
    assert built["eligible"]["realtime_review"] == 0
    for row in store.records(built["id"], "realtime_review"):
        assert not row["post_hoc"] and row["causal_review_required"] and not row["human_confirmed"]
        assert all(m["index"] < row["target_index"] for m in row["context"])
        assert all(m["index"] <= row["target_index"] for m in row["evidence"])


def test_reply_training_is_self_only_and_splits_are_isolated(corpus_root, store):
    built = store.build(corpus_root)
    records = {p: store.records(built["id"], p) for p in built["pools"]}
    assert built["pools"]["personal_reply"] == 16
    for row in records["personal_reply"]:
        assert row["target_index"] % 2 == 0
        assert row["response"] == row["text"]
        # Synthetic texts carry ids like "0-2" that the chat never said; v7 keeps them out of training.
        assert row["training_eligible"] is (not row["unbacked_specifics"])
    assert validate_splits(records)["source_overlap"] == 0
    for pool in records.values():
        assert set(r["split"] for r in pool) == {"train", "validation", "test"}


def test_build_uses_frozen_audit(corpus_root, store):
    audit = store.audit(corpus_root)
    file = next((corpus_root / "annotations").glob("*/batch*.json"))
    file.write_text("deliberately invalid later change")
    built = store.build(audit_id=audit["id"])
    assert built["pools"]["offline_review"] == 16


def test_explicit_export_preview_and_no_contact_names(corpus_root, store):
    built = store.build(corpus_root)
    preview = store.export_preview(built["id"], ["personal_reply"])
    with pytest.raises(RapportError, match="确认"):
        store.export(built["id"], preview["token"], False)
    data = store.export(built["id"], preview["token"], True)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert set(archive.namelist()) == {"manifest.json", "personal_reply/train.jsonl", "personal_reply/validation.jsonl", "personal_reply/test.jsonl"}
        assert b"private-name" not in b"".join(archive.read(p) for p in archive.namelist())
    with pytest.raises(RapportError):
        store.export(built["id"], "tampered", True)


def test_source_overlap_output_refused(corpus_root, tmp_path):
    store = DatasetStore(corpus_root / "output", Crypto(tmp_path / "unrelated.key"))
    with pytest.raises(RapportError) as exc:
        store.audit(corpus_root)
    assert exc.value.code == "dataset_output_overlaps_source"
    assert not (corpus_root / "output").exists()


def test_duplicate_annotations_not_duplicate_samples(corpus_root):
    ann = next((corpus_root / "annotations").glob("*/batch*.json"))
    (ann.parent / "batch_002.json").write_bytes(ann.read_bytes())
    corpus = load_corpus(corpus_root)
    assert corpus["counts"]["annotations"] == 16
    assert corpus["counts"]["duplicate_annotations"] == 4
    assert corpus["counts"].get("conflicting_duplicate_annotations", 0) == 0


def test_overlapping_export_versions_remain_in_same_split(corpus_root):
    path = next(p for p in (corpus_root / "redacted").glob("*.json") if not p.name.startswith("_"))
    original = json.loads(path.read_text())
    original["session"]["extra_metadata"] = "changed"
    copy = path.with_name("another-export-version.json")
    copy.write_text(json.dumps(original))
    ann = corpus_root / "annotations" / copy.stem
    ann.mkdir()
    (ann / "batch_001.json").write_bytes((corpus_root / "annotations" / path.stem / "batch_001.json").read_bytes())
    pools, protocol = build_records(load_corpus(corpus_root))
    assert protocol["leakage"]["source_overlap"] == 0
    refs = {}
    for row in pools["offline_review"]:
        for ref in row["content_refs"]:
            assert ref not in refs or refs[ref] == row["split"]
            refs[ref] = row["split"]


def test_media_and_unknown_roles_excluded_from_personal_targets(corpus_root):
    path = next(p for p in (corpus_root / "redacted").glob("*.json") if not p.name.startswith("_"))
    data = json.loads(path.read_text())
    data["messages"][1]["type"] = "图片消息"
    path.write_text(json.dumps(data))
    pools, _ = build_records(load_corpus(corpus_root))
    assert len(pools["personal_reply"]) == 12


def test_review_creates_new_snapshot_and_enables_only_reviewed_pool(corpus_root, store):
    built = store.build(corpus_root)
    row = store.records(built["id"], "realtime_review")[0]
    reviewed = store.review(built["id"], "realtime_review", row["id"], "accepted", "已检查当前之前的上下文")
    assert reviewed["id"] != built["id"]
    assert reviewed["parent_id"] == built["id"]
    assert reviewed["eligible"]["realtime_review"] == 1
    assert store.get(built["id"])["eligible"]["realtime_review"] == 0
    offline = store.records(reviewed["id"], "offline_review")[0]
    assert not offline["human_confirmed"]


def test_leakage_detected():
    with pytest.raises(RapportError) as exc:
        validate_splits({"a": [{"split": "train", "source_ids": ["s:1"]}, {"split": "test", "source_ids": ["s:1"]}]})
    assert exc.value.code == "dataset_split_leakage"


def test_chronological_splits_keep_whole_conversation_segments(corpus_root):
    for path in (corpus_root / "redacted").glob("*.json"):
        if path.name.startswith("_"):
            continue
        data = json.loads(path.read_text())
        for i, row in enumerate(data["messages"]):
            row["createTime"] += (i // 2) * 86400
        path.write_text(json.dumps(data))
    pools, protocol = build_records(load_corpus(corpus_root))
    assert protocol["time_leakage"]["source_overlap"] == 0
    seen = {}
    for row in pools["offline_review"]:
        if row["time_split"] == "excluded_boundary":
            continue
        for ref in row["source_ids"]:
            assert ref not in seen or seen[ref] == row["time_split"]
            seen[ref] = row["time_split"]


def test_corrupt_snapshot_and_expired_export_refused(corpus_root, store, monkeypatch):
    built = store.build(corpus_root)
    preview = store.export_preview(built["id"], ["personal_reply"])
    monkeypatch.setattr("rapport.datasets.store.time.time", lambda: 9999999999)
    with pytest.raises(RapportError) as exc:
        store.export(built["id"], preview["token"], True)
    assert exc.value.code == "dataset_export_invalid"
    (store._path(built["id"]) / "snapshot.bin").write_bytes(b"corrupt")
    with pytest.raises(RapportError) as exc:
        store.records(built["id"], "personal_reply")
    assert exc.value.code == "dataset_corrupt"


def test_api_auth_csrf_and_pagination(app_client, corpus_root):
    # Isolated subapplication lets this test run before the root router is integrated.
    app = FastAPI()
    app.state.rapport = app_client.app_state
    app.state.auth = app_client.app_state.auth
    app.include_router(router, prefix="/api")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/api/datasets").status_code == 401
        client.cookies.update(app_client.cookies)
        assert client.post("/api/datasets/audit", json={"source_root": str(corpus_root)}).status_code == 403
        client.headers["x-rapport-csrf"] = app_client.headers["x-rapport-csrf"]
        audit = client.post("/api/datasets/audit", json={"source_root": str(corpus_root)})
        assert audit.status_code == 200
        built = client.post("/api/datasets/build", json={"audit_id": audit.json()["id"]}).json()
        preview = client.get(f"/api/datasets/{built['id']}/preview?pool=personal_reply&page_size=2")
        assert preview.headers["cache-control"] == "no-store"
        assert len(preview.json()["items"]) == 2
        assert preview.json()["total"] == 16
        assert client.get(f"/api/datasets/{built['id']}/preview?page=-1").status_code == 422
