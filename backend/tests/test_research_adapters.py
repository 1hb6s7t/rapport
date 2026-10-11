"""Research adapters must not leak future/context groups or hide source provenance."""

from pathlib import Path

import pytest

from rapport.research.data import predecessor_edges, prepare, vocabulary


def sample():
    return {"id": "s1:2", "session_id": "friend1", "target_index": 2, "split": "train", "time_split": "test",
            "text": "当前消息", "context": [{"index": 1, "sender": "me", "text": "之前"},
                                          {"index": 2, "sender": "other", "text": "当前消息"},
                                          {"index": 3, "sender": "me", "text": "后续不能泄漏"}],
            "labels": {"emotion": ["sad"], "intent": ["share"]}, "quality": "ai_assisted_high",
            "training_eligible": True, "post_hoc": True, "content_refs": ["ref1"]}


def test_visibility_cutoff_and_post_hoc_labels_are_distinct():
    example, manifest = prepare([sample()], visibility="causal")
    assert example[0].utterances == ["之前", "当前消息"]
    assert example[0].target == 1
    assert manifest["post_hoc_labels"] == 1
    assert manifest["production_eligible"] is False
    offline, _ = prepare([sample()], visibility="offline")
    assert offline[0].utterances[-1] == "后续不能泄漏"


def test_target_missing_from_context_is_appended_once():
    row = sample()
    row["context"] = row["context"][:1]
    example, _ = prepare([row])
    assert example[0].target == 1
    assert example[0].speakers == [0, 1]


def test_friend_and_time_protocol_cannot_silently_mix():
    friend, _ = prepare([sample()], split_protocol="friend")
    temporal, _ = prepare([sample()], split_protocol="time")
    assert friend[0].split == "train"
    assert temporal[0].split == "test"


def test_repeat_source_cross_split_rejected():
    first = sample()
    other = dict(first, id="other:2", session_id="other", split="test")
    with pytest.raises(ValueError, match="content crosses"):
        prepare([first, other])


def test_vocabulary_fitted_only_to_training():
    a = sample()
    b = dict(a, id="test:1", split="test", content_refs=["ref2"], labels={"emotion": ["unseen"]})
    examples, _ = prepare([a, b])
    assert vocabulary(examples)["emotion"] == ["sad"]


def test_dag_predecessors_match_author_window_rule():
    edges = predecessor_edges([0, 1, 1, 0, 1])
    assert set(edges) == {(1, 0), (2, 1), (3, 2), (3, 1), (3, 0), (4, 3), (4, 2)}
    assert all(source < target for target, source in edges)


def test_upstream_licenses_and_commits_are_shipped():
    root = Path(__file__).parents[1] / "rapport" / "research" / "vendor"
    assert "Apache License" in (root / "dag_erc" / "LICENSE").read_text()
    assert "ba7639dd12cdfa77cbde762f22f94266b30073d2" in (root / "dag_erc" / "PROVENANCE.md").read_text()
    assert "Copyright (c) 2021 zerohd4869" in (root / "dialogue_crn" / "LICENSE").read_text()


def test_native_crn_segmented_ops_are_differentiable():
    torch = pytest.importorskip("torch")
    from rapport.research.vendor.dialogue_crn.ops import scatter_add, softmax

    x = torch.tensor([[1.], [2.], [4.]], requires_grad=True)
    index = torch.tensor([0, 0, 1])
    scores = softmax(x, index, 2)
    assert torch.allclose(scores[:2], x[:2].softmax(0))
    assert float(scores[2]) == 1
    result = scatter_add(scores * x, index, dim_size=2)
    result.sum().backward()
    assert x.grad is not None and torch.isfinite(x.grad).all()


@pytest.mark.parametrize("method", ["utterance_mlp", "dag_erc", "dialogue_gcn", "dialogue_crn"])
@pytest.mark.parametrize("architecture", ["shared", "separate"])
def test_paper_adapters_forward_backward(method, architecture):
    torch = pytest.importorskip("torch")
    pytest.importorskip("transformers")
    from rapport.research.models import MultiTask

    torch.set_num_threads(1)
    model = MultiTask(method, architecture, 16, 8, {"emotion": 3, "intent": 4})
    x = torch.randn(2, 4, 16)
    speakers = torch.tensor([[0, 1, 0, 1], [0, 1, -1, -1]])
    lengths = torch.tensor([4, 2])
    y = model(x, speakers, lengths, torch.tensor([3, 1]))
    assert y["emotion"].shape == (2, 3)
    assert y["intent"].shape == (2, 4)
    sum(v.square().mean() for v in y.values()).backward()
    assert all(p.grad is None or torch.isfinite(p.grad).all() for p in model.parameters())


def test_causal_gcn_and_dag_cannot_observe_future_node():
    torch = pytest.importorskip("torch")
    pytest.importorskip("transformers")
    from rapport.research.models import MultiTask

    for method in ("dialogue_gcn", "dag_erc"):
        torch.manual_seed(5)
        model = MultiTask(method, "shared", 8, 8, {"emotion": 3}, causal=True).eval()
        x = torch.randn(1, 4, 8)
        s = torch.tensor([[0, 1, 0, 1]])
        a = model(x, s, torch.tensor([4]), torch.tensor([1]))["emotion"]
        x[:, 2:] += 100
        b = model(x, s, torch.tensor([4]), torch.tensor([1]))["emotion"]
        assert torch.allclose(a, b, atol=1e-6)


def test_clear_fences_running_research_before_deleting(tmp_path, monkeypatch):
    from rapport.errors import RapportError
    from rapport.research.lifecycle import ResearchCancelled, ResearchRun, safe_wipe

    dataset = tmp_path / "datasets" / "snapshot" / "manifest.json"
    dataset.parent.mkdir(parents=True)
    dataset.write_text("{}")
    private = tmp_path / "research" / "runs" / "run" / "weights.bin"
    private.parent.mkdir(parents=True)
    private.write_bytes(b"encrypted-test")
    guard = ResearchRun(tmp_path, "run", "snapshot")
    guard.check()
    monkeypatch.setattr("rapport.research.lifecycle._alive", lambda _: True)
    with pytest.raises(RapportError, match="正在停止"):
        safe_wipe(tmp_path)
    with pytest.raises(RapportError, match="暂不能开始"):
        ResearchRun(tmp_path, "new-worker", "snapshot")
    assert private.exists()
    with pytest.raises(ResearchCancelled):
        guard.check()
    guard.finish("cancelled")
    assert safe_wipe(tmp_path)["erased"]
    assert not private.exists()
    with pytest.raises(ResearchCancelled):
        guard.check()


def test_deleted_dataset_cancels_research_writer(tmp_path):
    from rapport.research.lifecycle import ResearchCancelled, ResearchRun

    guard = ResearchRun(tmp_path, "run", "missing")
    with pytest.raises(ResearchCancelled, match="快照已删除"):
        guard.check()


def test_reconcile_marks_dead_worker_interrupted(tmp_path, monkeypatch):
    import json

    from rapport.research.lifecycle import ResearchRun, reconcile_reports

    report = tmp_path / "research" / "runs" / "run" / "report.json"
    report.parent.mkdir(parents=True)
    report.write_text('{"status":"running"}')
    ResearchRun(tmp_path, "run", "dataset")
    monkeypatch.setattr("rapport.research.lifecycle._alive", lambda _: False)
    reconcile_reports(tmp_path)
    assert json.loads(report.read_text())["status"] == "interrupted"


def test_multilabel_metric_does_not_silently_keep_first_truth():
    pytest.importorskip("torch")
    from rapport.research.runner import metrics

    result = metrics([[1, 1], [0, 1]], [[.9, .1], [.1, .9]], ["happy", "close"])
    assert result["per_class"]["close"]["support"] == 2
    assert result["per_class"]["close"]["recall"] == .5
    assert result["per_class"]["happy"]["recall"] == 1


def test_public_meld_split_and_seven_emotion_adapter(tmp_path):
    import csv

    from rapport.research.public_data import read_meld_dyadic

    for name in ("train_sent_emo_dya.csv", "dev_sent_emo_dya.csv", "test_sent_emo_dya.csv"):
        with (tmp_path / name).open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["Dialogue_ID", "Utterance_ID", "Speaker", "Utterance", "Emotion"])
            writer.writeheader()
            writer.writerow({"Dialogue_ID": 0, "Utterance_ID": 0, "Speaker": "A", "Utterance": "hello", "Emotion": "joy"})
            writer.writerow({"Dialogue_ID": 0, "Utterance_ID": 1, "Speaker": "B", "Utterance": "hi", "Emotion": "neutral"})
    examples, manifest = read_meld_dyadic(tmp_path)
    assert len(examples) == 6
    assert len({e.group for e in examples}) == 3
    assert examples[0].labels == {"emotion": ["joy"]}
    assert manifest["original_split_preserved"] is True
    assert manifest["paper_score_reproduction"] is False


def test_existing_prompt_adapter_uses_real_shipped_validation():
    import sys

    sys.path.insert(0, str(Path(__file__).parents[2] / "scripts"))
    from intent_eval import evaluate_case

    from rapport.knowledge.reference import load_library
    from rapport.llm.fake import FakeProvider
    from rapport.research.data import Example
    from rapport.research.llm_baseline import evaluate_existing_prompt

    example = Example("synthetic", "friend", "test", ["你还好吗", "还好谢谢"], [0, 1], 1,
                      {"emotion": ["不确定"], "intent": ["信息确认"]}, False)
    result = evaluate_existing_prompt(FakeProvider(), example, evaluate_case, load_library())
    assert result["id"] == "synthetic"
    assert "tag_basis" in result
    assert "gated" in result
    assert "json_ok" in result


def test_crn_padding_is_not_attributed_to_either_speaker():
    torch = pytest.importorskip("torch")
    from rapport.research.models import CRNEncoder

    encoder = CRNEncoder(8, 8)
    captured = {}

    def record_input(module, inputs):
        captured["qmask"] = inputs[1].detach().clone()

    handle = encoder.model.register_forward_pre_hook(record_input)
    encoder(torch.randn(2, 4, 8), torch.tensor([[0, 1, 0, 1], [0, 1, -1, -1]]), torch.tensor([4, 2]))
    handle.remove()
    assert torch.equal(captured["qmask"][2:, 1], torch.zeros(2, 2))


def test_none_is_distinct_from_uncertainty_and_abstention():
    from rapport.research.llm_baseline import effective_labels

    assert effective_labels({"valid": True, "ambiguous": False, "tags": []}, "tags") == ["none"]
    assert effective_labels({"valid": True, "ambiguous": True, "tags": []}, "tags") == []
    assert effective_labels({"valid": True, "ambiguous": False, "emotions": ["不确定"]}, "emotions") == ["不确定"]


def test_temperature_fits_validation_nll_and_preserves_half_threshold():
    pytest.importorskip("torch")
    from rapport.research.calibration import apply_temperature, fit_temperature, probability_metrics

    validation_truth = [[1], [0], [1], [0]] * 4
    validation_probabilities = [[.99]] * 16
    fit = fit_temperature(validation_truth, validation_probabilities)
    assert fit["fit_split"] == "validation"
    assert fit["temperature"] > 1
    calibrated = apply_temperature([[.8], [.2]], fit["temperature"])
    assert .5 < calibrated[0][0] < .8
    assert .2 < calibrated[1][0] < .5
    before = probability_metrics(validation_truth, validation_probabilities)
    after = probability_metrics(validation_truth, apply_temperature(validation_probabilities, fit["temperature"]))
    assert after["binary_nll"] < before["binary_nll"]


def test_rejection_curve_has_fixed_thresholds_and_explicit_zero_coverage():
    pytest.importorskip("torch")
    from rapport.research.calibration import rejection_curve

    result = rejection_curve([[1, 0], [0, 1]], [[.7, .3], [.8, .2]])
    assert result["thresholds_selected_on_test"] is False
    assert result["full_multilabel_correctness"] is False
    assert result["points"][0]["top1_set_error"] == .5
    assert result["points"][-1]["coverage"] == 0
    assert result["points"][-1]["top1_set_error"] is None
