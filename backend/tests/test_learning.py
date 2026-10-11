"""Persistent learning lifecycle, genuine numeric training and conservative gates."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime

import pytest

from rapport.errors import RapportError
from rapport.learning import LearningService
from rapport.learning.baseline import evaluate, predict, train
from rapport.learning.gpu import GPUBusy, GPULease
from rapport.learning.worker import activation_gate, run, safe_failure, validate_split


def rows():
    result = []
    for split in ("train", "validation", "test"):
        for i in range(40):
            label = "happy" if i % 2 else "sad"
            result.append({"id": f"{split}-{i}", "session_id": f"friend-{split}", "split": split,
                           "source_ids": [f"source-{split}-{i}"], "text": "真开心哈哈" if i % 2 else "很难过呜呜",
                           "context": [], "labels": {"emotion": [label], "intent": ["share"]},
                           "quality": "ai_assisted_high", "training_eligible": True, "human_confirmed": False})
    return result


class FakeDatasets:
    def __init__(self):
        self.data = rows()
        self.exists = True

    def get(self, ident):
        if not self.exists:
            raise RapportError("dataset_not_found", "missing", status=404)
        return {"id": ident, "version": "synthetic"}

    def records(self, ident, pool):
        self.get(ident)
        return self.data

    def build(self):
        return {"id": "synthetic"}


@pytest.fixture
def learning(tmp_path, monkeypatch):
    service = LearningService(tmp_path, spawn_jobs=False)
    data = FakeDatasets()
    monkeypatch.setattr(service, "datasets", lambda: data)
    return service


def make_model(service, arch="shared"):
    job = service.create_job("train", dataset_id="synthetic", architecture=arch, max_features=100)
    run(service, job["id"])
    assert service.job(job["id"])["status"] == "completed"
    return service.model(service.job(job["id"])["result"]["model_id"])


def test_private_models_and_jobs_wipe_preserves_public_base(learning):
    model = make_model(learning)
    public = learning.root / "base_models" / "public"
    public.mkdir()
    (public / "config.json").write_text("{}")
    learning.wipe_all()
    assert not learning.models() and not learning.jobs()
    assert not (learning.root / "models" / model["id"]).exists()
    assert (public / "config.json").exists()


def test_reply_review_does_not_override_failed_automatic_gate(learning):
    model = make_model(learning)
    model.update(task="reply", gate={"passed": False, "reasons": ["structured_reply_validation_failed", "personal_generation_review_required"]})
    learning.register(model)
    folder = learning.root / "models" / model["id"]
    samples = [{"id": f"test-{i}"} for i in range(10)]
    (folder / "evaluation.bin").write_bytes(learning.crypto.encrypt(json.dumps(samples).encode()))
    updated = learning.review_replies(model["id"], {sample["id"]: True for sample in samples})
    assert updated["gate"]["passed"] is False
    assert updated["gate"]["reasons"] == ["structured_reply_validation_failed"]
    with pytest.raises(RapportError):
        learning.activate(model["id"], confirm=True)


def test_scheduled_training_chains_one_evaluation_without_activation(learning):
    model = make_model(learning)
    learning.update_job(model["job_id"], scheduled=True)
    learning.set_schedule({"enabled": True, "dataset_id": "synthetic"})
    learning.tick()
    learning.tick()
    evaluations = [job for job in learning.jobs() if job["kind"] == "evaluate"]
    assert len(evaluations) == 1
    assert evaluations[0]["model_id"] == model["id"]
    assert learning.active_model() is None


def test_baseline_learns_and_reports_real_metrics():
    weights = train(rows(), "shared", 100)
    assert weights["training_samples"] == 40
    assert predict(weights, ["真开心哈哈"])[0]["emotion"]["label"] == "happy"
    metrics = evaluate(weights, rows())
    assert metrics["macro_f1"] == 1
    assert metrics["tasks"]["emotion"]["per_class"]["sad"]["recall"] == 1
    assert "sad → sad" in metrics["tasks"]["emotion"]["confusion"]
    assert metrics["calibrated"] is False
    assert metrics["uncertainty_unit"] == "conversation_group"
    assert metrics["bootstrap_groups"] == 1


def test_weak_and_ineligible_rows_are_not_training_answers():
    data = rows()
    for row in data:
        row["training_eligible"] = False
    with pytest.raises(ValueError, match="4"):
        train(data, "shared", 100)


def test_split_leakage_is_rejected():
    data = rows()
    data[40]["id"] = data[0]["id"]
    with pytest.raises(ValueError, match="相同消息"):
        validate_split(data)
    data = rows()
    data[40]["session_id"] = data[0]["session_id"]
    with pytest.raises(ValueError, match="相同会话"):
        validate_split(data)


def test_model_lifecycle_checkpoint_and_encryption(learning):
    model = make_model(learning)
    assert model["state"] == "candidate"
    assert learning.job(model["job_id"])["checkpoint"]["stage"] == "trained"
    raw = (learning.root / "models" / model["id"] / "weights.bin").read_bytes()
    assert "开心".encode() not in raw
    assert learning.load_artifact(model)["training_samples"] == 40
    with pytest.raises(RapportError, match="尚未通过"):
        learning.activate(model["id"], confirm=True)
    job = learning.create_job("evaluate", model_id=model["id"])
    run(learning, job["id"])
    model = learning.model(model["id"])
    assert model["state"] == "evaluated"
    assert model["metrics"]["test"]["samples"] == 40
    assert model["gate"]["passed"] is False
    assert model["gate"]["majority_check_passed"] is True
    assert "production_baseline_comparison_pending" in model["gate"]["reasons"]


def test_explicit_activation_rollback_and_missing_artifact(learning):
    first = make_model(learning)
    second = make_model(learning, "separate")
    # Trusted evaluation interface fixture: never pretends the production benchmark has run.
    for model in (first, second):
        learning.evaluated(model["id"], {"fixture": True}, {"passed": True, "reasons": []})
    with pytest.raises(RapportError, match="确认"):
        learning.activate(first["id"], confirm=False)
    learning.activate(first["id"], confirm=True)
    learning.activate(second["id"], confirm=True)
    assert learning.model(first["id"])["state"] == "evaluated"
    learning.activate(first["id"], confirm=True, rollback=True)
    assert learning.active_model()["id"] == first["id"]
    assert learning.predict_analysis(["你好"])["reason"] == "offline_only"
    (learning.root / "models" / second["id"] / "weights.bin").unlink()
    with pytest.raises(RapportError, match="缺失"):
        learning.activate(second["id"], confirm=True)


def test_source_deletion_revokes_active_model(learning):
    model = make_model(learning)
    learning.evaluated(model["id"], {}, {"passed": True, "reasons": []})
    learning.activate(model["id"], confirm=True)
    learning.invalidate_sources(["source-train-0"])
    assert learning.model(model["id"])["state"] == "revoked"
    assert learning.active_model() is None
    with pytest.raises(RapportError):
        learning.activate(model["id"], confirm=True)


def test_dataset_deletion_cancels_pending_job_and_revokes(learning):
    model = make_model(learning)
    job = learning.create_job("train", dataset_id="synthetic")
    learning.revoke_dataset("synthetic")
    assert learning.job(job["id"])["status"] == "cancelled"
    assert learning.model(model["id"])["state"] == "revoked"


def test_cancelled_jobs_do_not_run_and_retry_gets_new_id(learning):
    job = learning.create_job("train", dataset_id="synthetic")
    learning.cancel(job["id"])
    run(learning, job["id"])
    assert learning.job(job["id"])["status"] == "cancelled"
    retried = learning.retry(job["id"])
    assert retried["id"] != job["id"]
    assert retried["retry_of"] == job["id"]


def test_restart_detects_dead_worker_without_touching_completed_jobs(learning, monkeypatch):
    job = learning.create_job("train", dataset_id="synthetic")
    learning.update_job(job["id"], status="running", pid=99999999)
    with learning.db() as c:
        data = learning._get(c, "jobs", job["id"])
        data["updated_at"] = time.time() - 100
        learning._put(c, "jobs", data)
    monkeypatch.setattr("rapport.learning.service._alive", lambda pid: False)
    restarted = LearningService(learning.data_dir, spawn_jobs=False)
    restarted.reconcile()
    assert restarted.job(job["id"])["status"] == "interrupted"


def test_gpu_lease_excludes_other_owner_and_releases(tmp_path):
    with GPULease(tmp_path, "ocr"):
        with pytest.raises(GPUBusy), GPULease(tmp_path, "training"):
            pass
    with GPULease(tmp_path, "reply"):
        pass


def test_schedule_initially_off_and_candidate_only(learning):
    assert learning.schedule()["enabled"] is False
    assert learning.tick() is None
    data = learning.datasets().data
    for i in range(200):
        data.append(dict(data[0], id=f"extra-{i}"))
    learning.set_schedule({"enabled": True, "dataset_id": "synthetic", "weekly_day": 0})
    now = datetime(2026, 9, 21, 12, tzinfo=UTC).timestamp()
    job = learning.tick(now)
    assert job["kind"] == "train"
    assert learning.active_model() is None
    assert learning.tick(now + 20) is None


def test_missing_lora_dependency_cannot_fake_a_candidate(learning):
    with pytest.raises(RapportError):
        learning.create_job("train", dataset_id="synthetic", architecture="reply_lora", pool="personal_reply", base_model_id="none")
    assert learning.models() == []


def test_paths_commands_and_cloud_fields_are_not_an_execution_surface(learning):
    with pytest.raises(RapportError):
        learning.create_job("shell", dataset_id="synthetic")
    with pytest.raises(RapportError):
        learning.create_job("train", dataset_id="../../escape")


def test_api_auth_csrf_and_default_state(app_client):
    c = app_client
    # root integration installs this router; avoid patching production route tables here.
    response = c.get("/api/learning")
    assert response.status_code == 200
    assert response.json()["schedules"]["enabled"] is False
    assert response.headers["cache-control"] == "no-store"
    invalid = c.post("/api/learning/jobs", json={"kind": "train", "destination": "https://cloud.example", "command": "echo forbidden"})
    assert invalid.status_code == 422
    csrf = c.headers.pop("x-rapport-csrf")
    assert c.put("/api/learning/schedules", json={"enabled": False}).status_code == 403
    c.headers["x-rapport-csrf"] = csrf
    c.cookies.clear()
    assert c.get("/api/learning/jobs").status_code == 401


def test_real_subprocess_uses_fixed_worker_and_reports_missing_dataset(learning, monkeypatch):
    job = learning.create_job("train", dataset_id="synthetic")
    learning._spawn(job["id"])
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        status = learning.job(job["id"])["status"]
        if status == "failed":
            break
        time.sleep(.1)
    assert learning.job(job["id"])["status"] == "failed"
    assert learning.job(job["id"])["pid"]
    assert "synthetic" not in json.dumps(learning.job(job["id"])["error"])


def test_realtime_gate_requires_review_even_if_classification_is_perfect():
    data = rows()
    metrics = evaluate(train(data, "shared", 100), data)
    gate = activation_gate(metrics, data, "realtime_review")
    assert "causal_human_review_required" in gate["reasons"]


def test_gpu_failure_diagnostic_does_not_persist_private_error_text():
    diagnostic = safe_failure(RuntimeError("CUDA out of memory while reading private sample: SECRET-CONTENT"))
    assert diagnostic.startswith("gpu_out_of_memory:")
    assert "SECRET-CONTENT" not in diagnostic
