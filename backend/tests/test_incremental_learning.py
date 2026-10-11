"""Scheduling uses synthetic immutable rows and app-owned temporary archives."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfoNotFoundError

import pytest

from rapport.archives import get_service
from rapport.errors import RapportError
from rapport.learning import LearningService
from rapport.learning.incremental import (
    _ledger,
    job_scope,
    ledger_key,
    record_training_completion,
    sample_keys,
)
from rapport.learning.timezones import schedule_zone
from rapport.learning.worker import _register_checked, run
from rapport.schemas.message_v1 import Conversation, Message, Sender, Source, TsPrecision


def data_rows(n=250):
    return [{"id": f"stable-{i}", "session_id": "anonymous-friend", "conversation_id": "segment", "split": "train",
             "source_ids": [f"source-{i}"], "content_refs": [f"content-{i}"], "text": f"匿名{i}", "response": f"匿名{i}", "training_eligible": True}
            for i in range(n)]


class Datasets:
    def __init__(self, rows=None):
        self.rows = rows if rows is not None else data_rows()
        self.current = "snapshot1"

    def get(self, ident):
        return {"id": ident, "status": "built"}

    def records(self, ident, pool):
        return self.rows

    def refresh(self, ident):
        return self.get(self.current)


@pytest.fixture
def scheduled(tmp_path, monkeypatch):
    service = LearningService(tmp_path, spawn_jobs=False)
    datasets = Datasets()
    monkeypatch.setattr(service, "datasets", lambda: datasets)
    service.set_schedule({"enabled": True, "dataset_id": "snapshot1", "weekly_day": 0})
    return service, datasets


def test_shanghai_fallback_and_disabled_cleanup(monkeypatch, tmp_path):
    def missing(name):
        raise ZoneInfoNotFoundError(name)
    monkeypatch.setattr("rapport.learning.timezones.ZoneInfo", missing)
    assert schedule_zone("Asia/Shanghai").utcoffset(None) == timedelta(hours=8)
    assert schedule_zone("UTC").utcoffset(None) == timedelta(0)
    service = LearningService(tmp_path, spawn_jobs=False)
    service.set_schedule({"enabled": False, "timezone": "not-installed-zone"})
    assert service.wipe_all() == {"models": 0}
    with pytest.raises(RapportError):
        schedule_zone("not-installed-zone")


def test_scheduler_uses_user_local_weekday_not_utc(scheduled):
    service, _ = scheduled
    # Sunday UTC, Monday Asia/Shanghai.
    now = datetime(2026, 9, 20, 17, tzinfo=UTC).timestamp()
    job = service.tick(now)
    assert job["kind"] == "train" and job["scheduled"]
    assert service.schedule()["last_new_samples"] == 250
    assert service.tick(now + 30) is None
    assert not service.models()


def test_validation_and_test_samples_never_count_toward_training_threshold(scheduled):
    service, data = scheduled
    for row in data.rows[100:]:
        row["split"] = "test"
    assert service.tick(datetime(2026, 9, 20, 17, tzinfo=UTC).timestamp()) is None
    assert service.schedule()["last_new_samples"] == 100
    assert not service.jobs()


def test_new_snapshot_metadata_does_not_reset_consumed_samples(scheduled):
    service, data = scheduled
    job = service.create_job("train", dataset_id="snapshot1")
    service.update_job(job["id"], status="completed", result={"model_id": "manual"})
    record_training_completion(service, job, {"trained_sample_ids": [row["id"] for row in data.rows]}, data.rows)
    data.current = "new-snapshot-same-rows"
    assert service.tick(datetime(2026, 9, 20, 17, tzinfo=UTC).timestamp()) is None
    assert service.schedule()["dataset_id"] == "new-snapshot-same-rows"
    assert service.schedule()["last_new_samples"] == 0
    assert len(service.jobs()) == 1  # A manual training does not auto-evaluate.


def test_net_same_count_with_new_ids_is_still_new_data(scheduled):
    service, data = scheduled
    job = service.create_job("train", dataset_id="snapshot1")
    service.update_job(job["id"], status="completed")
    record_training_completion(service, job, {"trained_sample_ids": [row["id"] for row in data.rows]}, data.rows)
    data.rows = [{**row, "id": "new-" + row["id"], "content_refs": ["new-" + row["content_refs"][0]]} for row in data.rows]
    created = service.tick(datetime(2026, 9, 20, 17, tzinfo=UTC).timestamp())
    assert created["kind"] == "train" and service.schedule()["last_new_samples"] == 250


def test_actual_128_lora_samples_only_consumes_128_not_whole_dataset(tmp_path):
    service = LearningService(tmp_path, spawn_jobs=False)
    rows = data_rows(5113)
    job = {"id": "manual-lora", "kind": "train", "architecture": "reply_lora", "pool": "personal_reply", "base_model_id": "base", "dataset_id": "snapshot", "status": "completed", "created_at": 1, "updated_at": 1}
    with service.db() as connection:
        service._put(connection, "jobs", job)
    actual_ids = [row["id"] for row in rows[100:228]]
    record_training_completion(service, job, {"trained_sample_ids": actual_ids, "trained_samples": 128}, rows)
    consumed = set(_ledger(service)[ledger_key(job)])
    assert len(consumed) == 128
    assert consumed == sample_keys(rows[100:228])
    assert len(sample_keys(rows) - consumed) == 4985
    assert service.job(job["id"])["trained_sample_count"] == 128
    with service.db() as connection:
        stored = connection.execute("SELECT body FROM settings WHERE id='sample_ledger'").fetchone()[0]
    assert rows[100]["id"] not in stored
    assert next(iter(consumed)) not in stored


def test_legacy_lora_count_does_not_fabricate_sample_identity(tmp_path):
    service = LearningService(tmp_path, spawn_jobs=False)
    job = {"id": "legacy", "kind": "train", "architecture": "reply_lora", "pool": "personal_reply", "base_model_id": "base", "status": "completed"}
    with service.db() as connection:
        service._put(connection, "jobs", job)
    record_training_completion(service, job, {"trained_samples": 128}, data_rows())
    assert service.job("legacy")["training_sample_identity"] == "unknown_legacy"
    assert _ledger(service) == {}


def test_failed_training_does_not_consume_or_spin_every_tick(scheduled):
    service, _ = scheduled
    now = datetime(2026, 9, 20, 17, tzinfo=UTC).timestamp()
    job = service.tick(now)
    service.update_job(job["id"], status="failed", error="dependency unavailable")
    assert service.tick(now + 60) is None
    assert len(service.jobs()) == 1
    assert _ledger(service) == {}
    assert service.schedule()["last_error"] == "dependency unavailable"


def _archive(client, rid="default"):
    start = datetime.now(UTC) - timedelta(days=10)
    conv = Conversation(id=uuid.uuid4().hex, source=Source.wechat_json, messages=[
        Message(id=f"m{i}", seq=i, sender=Sender.other if i % 2 == 0 else Sender.me, ts=start + timedelta(minutes=i), ts_precision=TsPrecision.second, text=f"合成{i}") for i in range(6)])
    imported = client.app_state.save_import(conv, "test", None, rid)
    client.post(f"/api/imports/{imported.id}/confirm", json={})
    return get_service(client.app_state).create(imported.id, rid)


def test_archive_prepare_persistent_worker_scope_and_readonly_source(app_client):
    rid = app_client.app_state.relationship_create("私有好友")["id"]
    archive = _archive(app_client, rid)
    service = LearningService(app_client.app_state.settings.data_dir, spawn_jobs=False)
    job = service.create_job("archive_prepare", relationship_ids=[rid])
    assert job_scope(service, job) == [rid]
    assert rid not in json.dumps(job)
    run(service, job["id"])
    completed = service.job(job["id"])
    assert completed["status"] == "completed"
    assert completed["result"]["delta"]["new_samples"] == 3
    snapshot = service.datasets().get(completed["result"]["dataset_id"])
    assert snapshot["included_source_refs"] == ["archive:" + archive["id"]]
    assert not service.models()
    with pytest.raises(RapportError):
        service.create_job("archive_prepare", relationship_ids=[])
    with pytest.raises(RapportError):
        service.create_job("archive_prepare", relationship_ids=["nonexistent"])


def test_archive_schedule_bootstrap_daily_job_and_permission_cancellation(app_client, monkeypatch):
    rid = app_client.app_state.relationship_create("授权对象")["id"]
    _archive(app_client, rid)
    service = LearningService(app_client.app_state.settings.data_dir, spawn_jobs=False)
    monkeypatch.setattr(service, "capabilities", lambda: {"lora_dependencies": True})
    cfg = service.set_schedule({"enabled": True, "source_mode": "archives", "relationship_ids": [rid], "pool": "personal_reply", "architecture": "reply_lora", "base_model_id": "base"})
    assert cfg["dataset_id"] is None
    with service.db() as connection:
        persisted = connection.execute("SELECT body FROM settings WHERE id='schedule'").fetchone()[0]
    assert rid not in persisted
    now = datetime.now(UTC).timestamp()
    job = service.tick(now)
    assert job["kind"] == "archive_prepare"
    assert service.tick(now + 10) is None
    run(service, job["id"])
    service.tick(now + 20)
    assert service.schedule()["dataset_id"] == service.job(job["id"])["result"]["dataset_id"]
    assert service.schedule()["last_status"] == "ready"
    tomorrow = service.tick(now + 86400)
    assert tomorrow["kind"] == "archive_prepare"
    service.set_schedule({"enabled": False})
    assert service.job(tomorrow["id"])["status"] == "cancelled"
    assert service.schedule()["last_status"] == "disabled"


def test_prepare_cancellation_prevents_worker_and_missing_source_error_visible(app_client, monkeypatch):
    rid = app_client.app_state.relationship_create("即将删除")["id"]
    service = LearningService(app_client.app_state.settings.data_dir, spawn_jobs=False)
    job = service.create_job("archive_prepare", relationship_ids=[rid])
    service.cancel(job["id"])
    run(service, job["id"])
    assert service.job(job["id"])["status"] == "cancelled"
    monkeypatch.setattr(service, "capabilities", lambda: {"lora_dependencies": True})
    service.set_schedule({"enabled": True, "source_mode": "archives", "relationship_ids": [rid], "pool": "personal_reply", "architecture": "reply_lora", "base_model_id": "base"})
    with app_client.app_state.db.session() as session:
        from rapport.db import Relationship
        session.delete(session.get(Relationship, rid))
    assert service.tick() is None
    assert service.schedule()["enabled"] is False
    assert service.schedule()["last_error"] == "learning_archive_scope_missing"


def test_private_source_registration_encrypted_and_archive_delete_revokes(app_client):
    rid = app_client.app_state.relationship_create("模型来源好友")["id"]
    archive = _archive(app_client, rid)
    service = LearningService(app_client.app_state.settings.data_dir, spawn_jobs=False)
    app_client.app_state.learning = service
    job = service.create_job("archive_prepare", relationship_ids=[rid])
    run(service, job["id"])
    dataset_id = service.job(job["id"])["result"]["dataset_id"]
    rows = service.datasets().records(dataset_id, "personal_reply")
    model = {"id": "synthetic-candidate", "task": "reply", "state": "candidate", "dataset_id": dataset_id, "source_ids": [f"dataset:{dataset_id}"], "created_at": 1}
    _register_checked(service, model, dataset_id, rows)
    assert f"relationship:{rid}" in service.model(model["id"])["source_ids"]
    with service.db() as connection:
        persisted = connection.execute("SELECT body FROM models WHERE id=?", (model["id"],)).fetchone()[0]
    assert rid not in persisted
    response = app_client.delete(f"/api/archives/{archive['id']}", params={"relationship_id": rid})
    assert response.status_code == 200
    assert service.model(model["id"])["state"] == "revoked"


def test_archive_prepare_api_auth_and_explicit_scope(app_client):
    service = LearningService(app_client.app_state.settings.data_dir, spawn_jobs=False)
    app_client.app_state.learning = service
    assert app_client.post("/api/learning/jobs", json={"kind": "archive_prepare", "relationship_ids": []}).status_code == 422
    response = app_client.post("/api/learning/jobs", json={"kind": "archive_prepare", "source_mode": "archives", "relationship_ids": ["default"]})
    assert response.status_code == 200 and response.json()["kind"] == "archive_prepare"
    app_client.headers.pop("x-rapport-csrf")
    assert app_client.post("/api/learning/jobs", json={"kind": "archive_prepare", "relationship_ids": ["default"]}).status_code == 403
