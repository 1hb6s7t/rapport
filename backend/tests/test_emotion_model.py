"""The offline emotion layer: tokenizer mechanics, the reading contract, the rules-mode wiring.

Only the parity/sanity tests need the real model; everything else runs with a stub, so the
suite stays green on an installation without `models/emotion-zh-tiny`.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from rapport.config import reset_settings
from rapport.db import Setting
from rapport.emotion import model as emodel
from rapport.emotion.readings import readings_from_conversation
from rapport.emotion.tokenizer import BertTokenizer
from rapport.jobs.pipeline import run_rules
from rapport.parsers import ParseOptions, parse
from rapport.schemas.profile import Profile
from rapport.schemas.report_v1 import IntentReading
from rapport.state import AppState

MODEL_DIR = Path(__file__).resolve().parents[2] / "models" / "emotion-zh-tiny"
needs_model = pytest.mark.skipif(
    not (MODEL_DIR / "model.onnx").is_file() or not (MODEL_DIR / "manifest.json").is_file(),
    reason="offline emotion model not installed",
)

VOCAB = ["[PAD]", "[UNK]", "[CLS]", "[SEP]", "你", "好", "吗", "？", "！", "he", "##llo", "##world", "world", "hello"]


# ---------------------------------------------------------------- tokenizer mechanics


@pytest.fixture
def tiny_tokenizer(tmp_path):
    path = tmp_path / "vocab.txt"
    path.write_text("\n".join(VOCAB), encoding="utf-8")
    return BertTokenizer(path)


def test_cjk_chars_and_punctuation_become_their_own_tokens(tiny_tokenizer):
    assert tiny_tokenizer.encode("你好吗？") == [2, 4, 5, 6, 7, 3]


def test_wordpiece_greedy_longest_match_and_unknown(tiny_tokenizer):
    # Continuation pieces need the "##" prefix: "world" alone is not a valid second piece.
    hello, world = tiny_tokenizer.vocab["hello"], tiny_tokenizer.vocab["##world"]
    assert tiny_tokenizer.encode("helloworld") == [2, hello, world, 3]
    assert tiny_tokenizer.encode("helloz") == [2, 1, 3]  # a word that cannot be fully covered


def test_empty_text_is_just_specials(tiny_tokenizer):
    assert tiny_tokenizer.encode("") == [2, 3]


def test_truncation_keeps_cls_and_sep(tiny_tokenizer):
    ids = tiny_tokenizer.encode("你好吗", max_len=3)
    assert len(ids) == 3 and ids[0] == 2 and ids[-1] == 3


# ---------------------------------------------------------------- the real artifact


@needs_model
def test_tokenizer_ids_match_reference_samples():
    parity = json.loads((MODEL_DIR / "parity.json").read_text(encoding="utf-8"))
    tokenizer = BertTokenizer(MODEL_DIR / "vocab.txt")
    for sample in parity["samples"]:
        assert tokenizer.encode(sample["text"], parity["max_len"]) == sample["ids"], sample["text"][:24]


@needs_model
def test_predictions_are_a_distribution_over_the_manifest_labels():
    model = emodel.load_model()
    assert model is not None
    preds = model.predict(["今天真的好开心呀，谢谢你陪我！", "……"])
    assert preds[0].raw == "高兴" and preds[0].mapped == "开心" and preds[0].prob > 0.9
    assert set(preds[0].probs) == set(model.labels)
    assert all(abs(sum(p.probs.values()) - 1) < 0.02 for p in preds)
    assert preds[1].ambiguous, "a bare ellipsis must not become a confident reading"


# ---------------------------------------------------------------- reading contract


DEMO = """[2026-08-20 20:00] A: 今天好累
[2026-08-20 20:10] B: 辛苦了
[2026-08-20 20:12] A: 嗯
[2026-08-20 20:13] B: 早点休息
"""


def _conv():
    return parse(DEMO, "c1", "paste", None, ParseOptions())[0]


class FakeModel:
    def __init__(self, predictions):
        self.predictions = predictions

    def predict(self, texts):
        assert len(texts) == len(self.predictions)
        return self.predictions


def _other_messages(conv):
    return [m for m in conv.messages if m.sender.value == "other" and m.is_text]


def test_readings_contract_from_a_stubbed_model(monkeypatch):
    conv = _conv()
    other = _other_messages(conv)
    predictions = [
        emodel.Prediction(raw="高兴", prob=0.9, probs={"高兴": 0.9}),
        emodel.Prediction(raw="悲伤", prob=0.4, probs={"悲伤": 0.4}),
    ]
    monkeypatch.setattr("rapport.emotion.readings.load_model", lambda: FakeModel(predictions))
    readings = readings_from_conversation(conv, 0, 10**9)
    assert len(readings) == len(other) == 2
    first = readings[0]
    assert first.source == "local_model" and first.post_hoc is False and first.context_mode == "reply"
    assert first.evidence_ids == [other[0].id] and first.emotions == ["开心"] and first.pragmatic_tags == []
    assert first.samples == 1 and first.independent_units == 1 and not first.ambiguous
    second = readings[1]
    assert second.ambiguous and second.abstain_reason
    assert second.suppressed_reason, "single-sentence negatives always carry the over-reading caution"


def test_cap_keeps_the_most_recent_messages(monkeypatch):
    conv = _conv()
    monkeypatch.setattr(
        "rapport.emotion.readings.load_model",
        lambda: FakeModel([emodel.Prediction(raw="高兴", prob=0.9, probs={})]),
    )
    readings = readings_from_conversation(conv, 0, 10**9, max_messages=1)
    assert len(readings) == 1 and readings[0].surface_text == "早点休息"


def test_no_model_means_no_readings(monkeypatch):
    monkeypatch.setattr("rapport.emotion.readings.load_model", lambda: None)
    assert readings_from_conversation(_conv(), 0, 10**9) == []


# ---------------------------------------------------------------- report + context wiring


def test_rules_report_carries_model_readings_without_moving_the_score():
    conv = _conv()
    profile = Profile()
    base = run_rules(conv, conv.events(), profile, n_eff=0.0)
    reading = IntentReading(
        msg_id=conv.messages[0].id,
        emotions=["开心"],
        evidence_ids=[conv.messages[0].id],
        post_hoc=False,
        context_mode="reply",
        independent_units=1,
        source="local_model",
    )
    with_model = run_rules(conv, conv.events(), profile, n_eff=0.0, intent_readings=[reading])
    assert with_model.intent_readings[0].source == "local_model"
    assert with_model.affinity.model_dump() == base.affinity.model_dump()
    assert any("本机小模型" in n for n in with_model.notes)


def test_model_readings_pass_the_reply_visibility_gate():
    from rapport.analysis_context import build_context

    conv, _ = parse("我: 你怎么了？\n对方: 没事。\n我: 那我先忙", "example", "auto")
    imp = SimpleNamespace(id="i", content_hash="h", range_start=0, range_end=2)
    other = next(m for m in conv.messages if m.sender.value == "other")
    reading = IntentReading(
        msg_id=other.id,
        emotions=["伤心"],
        evidence_ids=[other.id],
        post_hoc=False,
        context_mode="reply",
        independent_units=1,
        source="local_model",
    )
    ctx = build_context(
        SimpleNamespace(), "default", imp, conv, report={"id": "r", "intent_readings": [reading.model_dump()]}
    )
    assert ctx["readings"] and ctx["readings"][0]["source"] == "local_model"


# ---------------------------------------------------------------- setting + API


@pytest.fixture
def state():
    yield AppState(reset_settings(data_dir=Path(tempfile.mkdtemp()), pairing_code="123456"))


def test_emotion_config_defaults_clamps_and_stores_default_as_empty(state, monkeypatch):
    monkeypatch.setattr("rapport.state.emotion.engines_status", lambda: {"emotion_model": {"available": False}})
    view = state.emotion_config_view()
    default = {"enabled": False, "engine": "grounded", "max_messages": 120, "use_markers": True, "use_changepoints": True}
    assert view["config"] == default == view["default"]
    state.set_emotion_config({"enabled": True, "max_messages": 9000})
    assert state.emotion_config() == {**default, "enabled": True, "max_messages": 500}
    state.set_emotion_config({"max_messages": 40, "engine": "structbert"})
    assert state.emotion_config() == {**default, "enabled": True, "max_messages": 40, "engine": "structbert"}
    state.set_emotion_config({"enabled": False, "max_messages": 120, "engine": "grounded"})
    with state.db.session() as s:
        assert s.get(Setting, "emotion.config").value == {}, "an all-default config is stored as {}"


def test_emotion_api_roundtrip(app_client):
    c = app_client
    v = c.get("/api/preferences/emotion").json()
    assert v["config"]["enabled"] is False and "model" in v
    v = c.patch("/api/preferences/emotion", json={"enabled": True, "max_messages": 60}).json()
    assert v["config"]["enabled"] is True and v["config"]["max_messages"] == 60 and v["config"]["engine"] == "grounded"
    assert c.patch("/api/preferences/emotion", json={"engine": "structbert"}).json()["config"]["engine"] == "structbert"
    assert c.patch("/api/preferences/emotion", json={"engine": "nope"}).json()["config"]["engine"] == "grounded"
    assert c.patch("/api/preferences/emotion", json={"max_messages": 5000}).status_code == 422
    assert c.patch("/api/preferences/emotion", json={"enabled": False, "max_messages": 120}).json()["config"]["enabled"] is False


def test_runner_generates_readings_only_when_enabled(state):
    from rapport.jobs.runner import JobRunner

    conv = _conv()
    runner = JobRunner(state)
    assert runner._offline_readings(conv, 0, 10**9) is None
    state.set_emotion_config({"enabled": True, "engine": "structbert"})
    readings = runner._offline_readings(conv, 0, 10**9)
    if emodel.load_model() is None:
        assert readings is None, "a missing model degrades to the plain rules report"
    else:
        assert readings and all(r.source == "local_model" for r in readings)
    state.set_emotion_config({"engine": "grounded"})
    grounded = runner._offline_readings(conv, 0, 10**9)
    if grounded is not None:
        assert all(r.source == "grounded" and r.evidence_ids == [r.msg_id] for r in grounded)
