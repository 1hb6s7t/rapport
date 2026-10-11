from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from rapport.archives import get_service
from rapport.datasets import DatasetStore
from rapport.datasets.archives import append_authorized_archives, learning_source_bindings
from rapport.datasets.corpus import VERSION, validate_splits
from rapport.db import Import
from rapport.errors import RapportError
from rapport.schemas.message_v1 import Conversation, Message, MsgType, Sender, Source, TsPrecision

NOW = datetime(2026, 9, 22, 12, tzinfo=UTC)


def make_archive(client, relationship_id="default", *, start=None, texts=None, source=Source.wechat_json, change=None):
    start = start or NOW - timedelta(days=10)
    texts = texts or ["今天怎么样", "挺好的", "晚上有空吗", "可以聊一会", "几点方便", "八点见"]
    messages = [Message(id=f"m{i}", seq=i, sender=Sender.other if i % 2 == 0 else Sender.me,
                        ts=start + timedelta(minutes=i), ts_precision=TsPrecision.second, text=text)
                for i, text in enumerate(texts)]
    if change:
        change(messages)
    state = client.app_state
    conv = Conversation(id=uuid.uuid4().hex, source=source, messages=messages)
    imported = state.save_import(conv, "archive-dataset-test", None, relationship_id)
    client.post(f"/api/imports/{imported.id}/confirm", json={})
    return get_service(state).create(imported.id, relationship_id)


def store(client):
    return DatasetStore(client.app_state.settings.data_dir / "datasets", client.app_state.crypto)


def test_authorized_archive_build_anon_encrypted_and_private_revoke_bindings(app_client):
    rid = app_client.app_state.relationship_create("不应出现在数据集的真实名字")["id"]
    archive = make_archive(app_client, rid)
    built = append_authorized_archives(app_client.app_state, relationship_ids=[rid], now=NOW)
    assert built["delta"]["new_samples"] == 3
    assert built["included_source_refs"] == ["archive:" + archive["id"]]
    assert built["provenance"] == [{"kind": "confirmed_archive", "source_id": "archive:" + archive["id"],
                                    "sha256": archive["content_hash"], "version": "archive-v1"}]
    rows = store(app_client).records(built["id"], "personal_reply")
    assert len(rows) == 3
    assert all(row["session_id"].startswith("a_") and row["quality"] == "observed_self_reply" for row in rows)
    assert all(m["ts"] < row["target_ts"] for row in rows for m in row["context"])
    assert rid not in json.dumps(rows) and rid not in json.dumps(built)
    assert "不应出现在" not in json.dumps(rows, ensure_ascii=False)
    refs = learning_source_bindings(store(app_client), built["id"])
    assert refs == sorted(["archive:" + archive["id"], "relationship:" + rid])
    assert learning_source_bindings(store(app_client), built["id"], ["unrelated"]) == []
    assert validate_splits({"personal_reply": rows})["source_overlap"] == 0


def test_archive_reply_skips_consecutive_self_continuations(app_client):
    make_archive(app_client, change=lambda messages: setattr(messages[2], "sender", Sender.me))
    built = append_authorized_archives(app_client.app_state, relationship_ids=["default"], now=NOW)
    rows = store(app_client).records(built["id"], "personal_reply")
    assert len(rows) == 2
    assert all(row["context"][-1]["sender"] == "other" for row in rows)


def test_repeat_update_idempotent_and_base_immutable(app_client):
    make_archive(app_client)
    first = append_authorized_archives(app_client.app_state, relationship_ids=["default"], now=NOW)
    path = store(app_client)._path(first["id"]) / "snapshot.bin"
    before = path.read_bytes()
    second = append_authorized_archives(app_client.app_state, relationship_ids=["default"], base_dataset_id=first["id"], now=NOW + timedelta(days=1))
    assert second["id"] == first["id"]
    assert second["unchanged"] and second["delta"]["new_samples"] == 0
    assert path.read_bytes() == before
    make_archive(app_client, start=NOW - timedelta(days=1))
    third = append_authorized_archives(app_client.app_state, relationship_ids=["default"], base_dataset_id=second["id"], now=NOW + timedelta(days=1))
    assert third["id"] != first["id"] and third["delta"]["new_samples"] == 3
    assert len(store(app_client).records(third["id"], "personal_reply")) == 6
    assert path.read_bytes() == before


def test_authorization_empty_foreign_and_unconfirmed(app_client):
    with pytest.raises(RapportError) as error:
        append_authorized_archives(app_client.app_state, relationship_ids=[], now=NOW)
    assert error.value.code == "dataset_archive_scope_required"
    other = app_client.app_state.relationship_create("其他好友")["id"]
    archive = make_archive(app_client, other)
    with pytest.raises(RapportError) as error:
        append_authorized_archives(app_client.app_state, relationship_ids=["default"], archive_ids=[archive["id"]], now=NOW)
    assert error.value.code == "dataset_archive_not_authorized"
    conv = Conversation(id=uuid.uuid4().hex, source=Source.wechat_json, messages=[Message(id="draft", seq=1, sender=Sender.me, text="未实际发送")])
    imported = app_client.app_state.save_import(conv, "test", None, "default")
    with pytest.raises(RapportError):
        get_service(app_client.app_state).create(imported.id, "default")


def test_unknown_media_quotes_and_drafts_break_context(app_client):
    def change(messages):
        messages[1].text = "[AI草稿]尚未发送"
        messages[2].quoted_text = "引用不是正文"
        messages[3].type = MsgType.image
    make_archive(app_client, change=change)
    built = append_authorized_archives(app_client.app_state, relationship_ids=["default"], now=NOW)
    rows = store(app_client).records(built["id"], "personal_reply")
    assert [r["response"] for r in rows] == ["八点见"]
    assert [m["text"] for m in rows[0]["context"]] == ["几点方便"]
    assert built["delta"]["context_barriers"] == 3


def test_archive_credential_breaks_reply_context_without_changing_source(app_client):
    synthetic = "sk-" + "B" * 35
    archive = make_archive(app_client, texts=["你好", "好的", synthetic, "再说", "后来呢", "后来回复"])
    original = get_service(app_client.app_state).messages(archive["id"], "default")
    built = append_authorized_archives(app_client.app_state, relationship_ids=["default"], now=NOW)
    rows = store(app_client).records(built["id"], "personal_reply")
    assert built["delta"]["credential_redactions"] == 1
    assert synthetic in json.dumps(original, ensure_ascii=False)  # Archive remains an exact local original.
    assert synthetic not in json.dumps(rows, ensure_ascii=False)
    assert all("[已移除凭证]" not in json.dumps(row, ensure_ascii=False) for row in rows)


def test_screen_capture_archive_with_label_times_yields_whole_turn_samples(app_client):
    base = NOW - timedelta(days=10)
    texts = ["置顶的旧消息", "在吗", "在的", "晚上吃什么", "随便吧", "哈哈", "那就火锅", "好呀", "我昨天去过那家", "周六几点",
             "看你", "哈哈"]
    senders = [Sender.other, Sender.other, Sender.me, Sender.other, Sender.me, Sender.me, Sender.other, Sender.me, Sender.me,
               Sender.other, Sender.me, Sender.me]

    def change(messages):
        for message, sender in zip(messages, senders, strict=True):
            message.sender = sender
            message.ts, message.ts_precision = base, TsPrecision.label  # one WeChat time label for the screen
        messages[0].ts, messages[0].ts_precision = None, TsPrecision.none  # above the first visible label

    make_archive(app_client, texts=texts, source=Source.ocr, change=change)
    built = append_authorized_archives(app_client.app_state, relationship_ids=["default"], now=NOW)
    delta = built["delta"]
    assert delta["unreliable_time_archives"] == 0 and delta["ambiguous_repetition_archives"] == 0, "repeated 哈哈 are distinct turns"
    rows = {row["response"]: row for row in store(app_client).records(built["id"], "personal_reply")}
    assert set(rows) == {"在的", "随便吧\n哈哈", "好呀\n我昨天去过那家", "看你\n哈哈"}, "a reply carries our follow-up bubbles"
    assert rows["好呀\n我昨天去过那家"]["unbacked_first_person"] and not rows["好呀\n我昨天去过那家"]["training_eligible"]
    assert rows["随便吧\n哈哈"]["bubbles"] == 2


def test_no_unreliable_or_synthetic_source_training(app_client):
    make_archive(app_client, source=Source.paste)
    make_archive(app_client, change=lambda m: setattr(m[0], "ts_precision", TsPrecision.day))
    make_archive(app_client, start=NOW - timedelta(days=2), change=lambda m: setattr(m[1], "ts", m[0].ts - timedelta(hours=1)))
    built = append_authorized_archives(app_client.app_state, relationship_ids=["default"], now=NOW)
    assert built["delta"]["new_samples"] == 0
    assert built["delta"]["unobserved_source_archives"] == 1
    assert built["delta"]["unreliable_time_archives"] == 1
    assert built["delta"]["non_monotonic_archives"] == 1


def test_open_conversation_waits_until_closed(app_client):
    make_archive(app_client, start=NOW - timedelta(hours=1))
    first = append_authorized_archives(app_client.app_state, relationship_ids=["default"], now=NOW)
    assert first["delta"]["open_conversations"] == 1
    assert first["delta"]["new_samples"] == 0
    second = append_authorized_archives(app_client.app_state, relationship_ids=["default"], base_dataset_id=first["id"], now=NOW + timedelta(hours=6))
    assert second["delta"]["new_samples"] == 3


def test_overlapping_archives_deduplicated_and_ambiguous_overlap_quarantined(app_client):
    full = [f"第{i}条" for i in range(10)]
    make_archive(app_client, texts=full[:6])
    make_archive(app_client, texts=full[2:], start=NOW - timedelta(days=10) + timedelta(minutes=2))
    built = append_authorized_archives(app_client.app_state, relationship_ids=["default"], now=NOW)
    rows = store(app_client).records(built["id"], "personal_reply")
    assert len(rows) == 5
    assert len({r["id"] for r in rows}) == 5
    assert built["delta"]["duplicate_messages"] == 4
    make_archive(app_client, texts=["不同版本甲", "不同版本乙"], start=NOW - timedelta(days=10) + timedelta(minutes=1))
    second = append_authorized_archives(app_client.app_state, relationship_ids=["default"], base_dataset_id=built["id"], now=NOW)
    assert second["delta"]["ambiguous_overlap_archives"] == 1
    assert second["delta"]["new_samples"] == 0


def test_foreground_test_holdout_stays_frozen_even_after_review(app_client):
    make_archive(app_client)
    first = append_authorized_archives(app_client.app_state, relationship_ids=["default"], now=NOW)
    for day in range(1, 21):
        make_archive(app_client, start=NOW + timedelta(days=day), texts=[f"未来第{day}天", "本人新回复"])
    second = append_authorized_archives(app_client.app_state, relationship_ids=["default"], base_dataset_id=first["id"], now=NOW + timedelta(days=30))
    rows = store(app_client).records(second["id"], "personal_reply")
    held = [r for r in rows if r["prospective_holdout"]]
    assert held and all(r["split"] == "test" and not r["training_eligible"] for r in held)
    assert second["delta"]["new_forward_holdout"] == len(held)
    with pytest.raises(RapportError) as error:
        store(app_client).review(second["id"], "personal_reply", held[0]["id"], "accepted")
    assert error.value.code == "dataset_holdout_frozen"
    third = append_authorized_archives(app_client.app_state, relationship_ids=["default"], base_dataset_id=second["id"], now=NOW + timedelta(days=90))
    assert third["prospective_holdouts"] == second["prospective_holdouts"]
    assert {r["id"] for r in store(app_client).records(third["id"], "personal_reply") if r["prospective_holdout"]} == {r["id"] for r in held}


def test_deleted_temporary_import_still_uses_confirmed_archive(app_client):
    archive = make_archive(app_client)
    with app_client.app_state.db.session() as session:
        imported = session.get(Import, archive["source_import_id"])
        app_client.app_state.blobs.delete(imported.blob_id)
        imported.deleted = True
    built = append_authorized_archives(app_client.app_state, relationship_ids=["default"], now=NOW)
    assert built["delta"]["new_samples"] == 3


def test_legacy_unknown_binding_quarantined_and_export_has_no_private_map(app_client):
    source_store = store(app_client)
    base = source_store._save({"id": uuid.uuid4().hex, "status": "built", "created_at": NOW.isoformat(), "version": VERSION,
                              "counts": {}, "quality": {}, "provenance": [{"kind": "redacted"}], "warnings": [],
                              "pools": {"offline_review": 0, "realtime_review": 0, "personal_reply": 0}, "splits": {}},
                             {"pools": {"offline_review": [], "realtime_review": [], "personal_reply": []}})
    archive = make_archive(app_client)
    result = append_authorized_archives(app_client.app_state, relationship_ids=["default"], base_dataset_id=base["id"], now=NOW)
    assert result["delta"]["new_samples"] == 0 and result["delta"]["pending_binding"] == 3
    assert result["included_source_refs"] == []
    assert "relationship_id" not in json.dumps(result)
    assert source_store._load(result["id"])["archive_bindings"]
    assert "archive:" + archive["id"] not in learning_source_bindings(source_store, result["id"])


def test_deleted_archive_cannot_be_registered_as_new_training_source(app_client):
    archive = make_archive(app_client)
    built = append_authorized_archives(app_client.app_state, relationship_ids=["default"], now=NOW)
    get_service(app_client.app_state).delete(archive["id"], "default")
    with pytest.raises(RapportError) as error:
        learning_source_bindings(store(app_client), built["id"])
    assert error.value.code == "dataset_archive_source_revoked"
    assert "archive:" + archive["id"] in learning_source_bindings(store(app_client), built["id"], verify_present=False)
    with pytest.raises(RapportError) as error:
        append_authorized_archives(app_client.app_state, relationship_ids=["default"], base_dataset_id=built["id"], now=NOW)
    assert error.value.code == "dataset_archive_source_revoked"


def test_friend_contexts_never_mix(app_client):
    first = app_client.app_state.relationship_create("甲")["id"]
    second = app_client.app_state.relationship_create("乙")["id"]
    make_archive(app_client, first, texts=["甲对方消息", "甲本人回复"])
    make_archive(app_client, second, texts=["乙对方消息", "乙本人回复"])
    built = append_authorized_archives(app_client.app_state, relationship_ids=[first, second], now=NOW)
    rows = store(app_client).records(built["id"], "personal_reply")
    assert len(rows) == 2
    for row in rows:
        letter = row["response"][0]
        assert all(m["text"].startswith(letter) for m in row["context"])
        assert all(ref.startswith(row["session_id"]) for ref in row["source_ids"])


def test_explicit_legacy_binding_preserves_original_friend_holdout(app_client):
    datasets = store(app_client)
    oldrow = {"id": "old:1", "session_id": "s_old", "split": "test", "time_split": "test",
              "context": [], "content_refs": ["older-content"], "source_ids": ["old:1"],
              "response": "旧回复", "training_eligible": True}
    base = datasets._save({"id": uuid.uuid4().hex, "status": "built", "created_at": NOW.isoformat(),
                           "version": VERSION, "counts": {}, "quality": {}, "warnings": [],
                           "provenance": [{"kind": "redacted"}]},
                          {"pools": {"offline_review": [], "realtime_review": [], "personal_reply": [oldrow]}})
    make_archive(app_client)
    result = append_authorized_archives(app_client.app_state, relationship_ids=["default"], base_dataset_id=base["id"],
                                        relationship_session_map={"default": "s_old"}, now=NOW)
    assert result["delta"]["new_samples"] == 3 and result["delta"]["pending_binding"] == 0
    assert result["delta"]["new_eligible"] == 0 and result["delta"]["new_holdout"] == 3
    assert {r["split"] for r in datasets.records(result["id"], "personal_reply")} == {"test"}
    with pytest.raises(RapportError):
        append_authorized_archives(app_client.app_state, relationship_ids=["default"], base_dataset_id=result["id"],
                                    relationship_session_map={"default": "s_unrelated"}, now=NOW)
