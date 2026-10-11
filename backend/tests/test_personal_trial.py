from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from rapport.datasets.corpus import VERSION as DATASET_VERSION
from rapport.errors import RapportError
from rapport.learning import LearningService
from rapport.llm.personal_request import FACT_POLICY, VERSION


def candidate(service):
    value = {"id": "trial-candidate", "created_at": 1, "task": "reply", "state": "evaluated", "scope": "personal_reply",
             "dataset_id": "synthetic", "runtime_supported": True, "artifact_hash": "same",
             "metrics": {"test": {"generation_protocol": VERSION, "validation_policy": FACT_POLICY}},
             "gate": {"passed": False, "automatic_passed": True, "reasons": ["personal_generation_review_required"]}}
    service.register(value)
    return value


def test_personal_trial_needs_explicit_confirmation_but_not_64_human_labels(tmp_path, monkeypatch):
    service = LearningService(tmp_path, spawn_jobs=False)
    model = candidate(service)
    monkeypatch.setattr(service, "datasets", lambda: SimpleNamespace(get=lambda _: {"version": DATASET_VERSION}))
    monkeypatch.setattr(service, "load_artifact", lambda _: {})
    monkeypatch.setattr(service.reply_workers(), "warm", Mock())
    with pytest.raises(RapportError, match="确认"):
        service.activate(model["id"], confirm=False, personal_trial=True)
    with pytest.raises(RapportError):
        service.activate(model["id"], confirm=True)
    active = service.activate(model["id"], confirm=True, personal_trial=True)
    assert active["state"] == "active" and active["activation_scope"] == "personal_trial"
    assert "reply_review" not in active
    assert active["gate"]["passed"] is False
    assert service.deactivate(model["id"])["state"] == "evaluated"
    assert service.active_model("reply") is None


@pytest.mark.parametrize("reason", ["structured_reply_validation_failed", "personal_generation_review_failed", "reply_generation_truncated"])
def test_personal_trial_cannot_override_failed_checks_or_negative_user_review(tmp_path, reason):
    service = LearningService(tmp_path, spawn_jobs=False)
    model = candidate(service)
    model["gate"]["reasons"].append(reason)
    service.register(model)
    assert service.models()[0]["personal_trial_available"] is False
    with pytest.raises(RapportError):
        service.activate(model["id"], confirm=True, personal_trial=True)
