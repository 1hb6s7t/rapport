"""Human gold labels: stratified plan, causal-only sample view, label/undo/export, API."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from rapport.config import reset_settings
from rapport.errors import RapportError
from rapport.schemas.report_v1 import EMOTIONS
from rapport.state import AppState
from rapport.state.gold import MIN_PER_CLASS, GoldMixin, stratified_ids

DS = "a" * 32


def _rows() -> list[dict]:
    rows = []
    labels = ["无明显情绪"] * 120 + ["开心"] * 50 + ["不满"] * 40 + ["伤心"] * 35 + ["亲近"] * 45
    for i, label in enumerate(labels):
        rows.append({
            "id": f"s:{i}", "split": "test" if i % 2 else "validation", "training_eligible": True,
            "text": f"消息{i}", "labels": {"emotion": [label], "intent": ["分享陈述"]}, "target_index": 5,
            "context": [
                {"index": 3, "sender": "me", "text": "前一句"},
                {"index": 4, "sender": "other", "text": "再前一句"},
                {"index": 5, "sender": "other", "text": f"消息{i}"},
                {"index": 6, "sender": "me", "text": "后一句不该显示"},
            ],
        })
    rows.append({"id": "s:train", "split": "train", "training_eligible": True, "text": "训练行", "labels": {"emotion": ["开心"]}})
    rows.append({"id": "s:weak", "split": "test", "training_eligible": False, "text": "不合格", "labels": {"emotion": ["开心"]}})
    return rows


@pytest.fixture
def state(monkeypatch):
    rows = {r["id"]: r for r in _rows()}
    monkeypatch.setattr(GoldMixin, "_gold_rows", lambda self, dataset_id, pool: rows)
    yield AppState(reset_settings(data_dir=Path(tempfile.mkdtemp()), pairing_code="123456"))


def test_stratified_ids_floor_fill_and_exclusions():
    ids, quotas = stratified_ids(_rows(), 200, seed=1)
    assert len(ids) == 200 and len(set(ids)) == 200
    assert quotas == {"不满": 40, "亲近": 40, "伤心": 35, "开心": 40, "无明显情绪": 40}
    assert all(q >= min(MIN_PER_CLASS, 35) for q in quotas.values())
    assert "s:train" not in ids and "s:weak" not in ids
    assert stratified_ids(_rows(), 200, seed=1)[0] == ids, "same seed, same plan"


def test_plan_label_undo_export(state):
    assert state.gold_plan(DS) is None
    view = state.create_gold_plan(DS, size=100, seed=3)
    assert view["size"] == 100 and "sample_ids" not in view
    with pytest.raises(RapportError):
        state.create_gold_plan(DS, size=100)

    nxt = state.gold_next(DS)
    sample = nxt["sample"]
    assert not nxt["done"] and sample["text"].startswith("消息") and sample["emotions"] == list(EMOTIONS)
    assert [m["text"] for m in sample["context"]] == ["前一句", "再前一句"], "causal context only, target excluded"
    assert "labels" not in sample and "weak" not in json.dumps(sample, ensure_ascii=False)

    progress = state.gold_label(DS, sample["sample_id"], "开心", "分享陈述", note="x")
    assert progress["labelled"] == 1 and progress["remaining"] == 99 and progress["per_emotion"] == {"开心": 1}
    exported = state.gold_export(DS)
    assert exported[0]["emotion"] == "开心" and exported[0]["weak_emotion"] in EMOTIONS and exported[0]["split"] in {"validation", "test"}

    second = state.gold_next(DS)["sample"]
    assert second["sample_id"] != sample["sample_id"]
    state.gold_label(DS, second["sample_id"], None, None, skipped=True)
    assert state.gold_progress(DS)["skipped"] == 1
    state.gold_undo(DS)
    after = state.gold_progress(DS)
    assert after["skipped"] == 0 and after["labelled"] == 1 and after["stored_labels_total"] == 1

    with pytest.raises(RapportError):
        state.gold_label(DS, sample["sample_id"], "nope", None)
    with pytest.raises(RapportError):
        state.gold_label(DS, "s:train", "开心", None)
    with pytest.raises(RapportError):
        state.gold_label(DS, sample["sample_id"], "开心", "不是语用标签")


def test_plan_replace_keeps_stored_labels(state):
    state.create_gold_plan(DS, size=80, seed=1)
    sid = state.gold_next(DS)["sample"]["sample_id"]
    state.gold_label(DS, sid, "不满", None)
    view = state.create_gold_plan(DS, size=80, seed=2, replace=True)
    assert view["seed"] == 2 and state.gold_progress(DS)["stored_labels_total"] == 1


def test_gold_api_roundtrip(app_client, monkeypatch):
    rows = {r["id"]: r for r in _rows()}
    monkeypatch.setattr(GoldMixin, "_gold_rows", lambda self, dataset_id, pool: rows)
    c = app_client
    assert c.get(f"/api/gold/plan?dataset_id={DS}").json()["has_plan"] is False
    assert c.get(f"/api/gold/next?dataset_id={DS}").status_code == 404
    created = c.post("/api/gold/plan", json={"dataset_id": DS, "size": 60})
    assert created.status_code == 200 and created.json()["has_plan"] and created.json()["total"] == 60
    assert c.post("/api/gold/plan", json={"dataset_id": DS, "size": 60}).status_code == 409
    nxt = c.get(f"/api/gold/next?dataset_id={DS}").json()
    sid = nxt["sample"]["sample_id"]
    labelled = c.post("/api/gold/label", json={"dataset_id": DS, "sample_id": sid, "emotion": "亲近"}).json()
    assert labelled["progress"]["labelled"] == 1 and labelled["sample"]["sample_id"] != sid
    assert c.post("/api/gold/label", json={"dataset_id": DS, "sample_id": sid, "emotion": "坏标签"}).status_code == 422
    assert c.get(f"/api/gold/export?dataset_id={DS}").json()["labels"][0]["emotion"] == "亲近"
    assert c.post("/api/gold/undo", json={"dataset_id": DS}).json()["progress"]["labelled"] == 0
