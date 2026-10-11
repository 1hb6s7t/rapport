"""Research-grounded offline layer: fusion rules, anchors, calibration, change points."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from rapport.grounded import classify as gc
from rapport.grounded import markers
from rapport.grounded.anchors import AnchorIndex, Neighbor, anchors_path, clear_cache, load_index, write_index
from rapport.grounded.calibrate import apply_temperature
from rapport.schemas.message_v1 import Conversation, Message, MsgType, Sender
from rapport.scoring.changepoints import detect_change_points, to_schema
from rapport.security import Crypto
from rapport.state.emotion import DEFAULT_EMOTION_CONFIG, _config_from


def _conv(texts: list[str]) -> Conversation:
    msgs = [Message(id=f"m{i}", seq=i, sender=Sender.other, type=MsgType.text, text=t) for i, t in enumerate(texts)]
    return Conversation(id="c1", source="paste", messages=msgs)


# --- neutral path -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "https://platform.worldquantbrain.com/learn/documentation",
        "someone@example.com 教程如下",
        "sk_live_ABCDEF1234567890",
        "OK, see you tomorrow at 8pm.",
        "好",
    ],
)
def test_looks_neutral_flags_non_emotional_text(text):
    assert gc.looks_neutral(text) is not None


@pytest.mark.parametrize("text", ["想你了", "你怎么又这样", "今天中午吃的肯德基"])
def test_looks_neutral_keeps_ordinary_chinese(text):
    assert gc.looks_neutral(text) is None


# --- fusion -----------------------------------------------------------------------------------


def test_blend_renormalizes_over_available_signals():
    w = gc.WEIGHTS
    out = gc._blend({"structbert": {"开心": 1.0}, "bge_knn": {}, "centroid": {"开心": 0.5, "亲近": 0.5}})
    total = w["structbert"] + w["centroid"]
    assert out["开心"] == pytest.approx((w["structbert"] * 1.0 + w["centroid"] * 0.5) / total)
    assert out["亲近"] == pytest.approx((w["centroid"] * 0.5) / total)
    assert gc._blend({"structbert": {}, "bge_knn": {}, "centroid": {}}) == {}


def _clf() -> gc.GroundedClassifier:
    return gc.GroundedClassifier(None, None, None, use_markers=False)


def test_negative_label_needs_two_agreeing_signals():
    votes = {"structbert": "不满", "bge_knn": "开心", "centroid": "开心"}
    r = _clf()._decide("你怎么又这样", {"不满": 0.6, "开心": 0.3}, votes, [], (), 0.9)
    assert r.emotion == gc.UNSURE and r.ambiguous and r.suppressed_reason
    votes["bge_knn"] = "不满"
    r = _clf()._decide("你怎么又这样", {"不满": 0.6, "开心": 0.3}, votes, [], (), 0.9)
    assert r.emotion == "不满" and not r.ambiguous


def test_low_top_score_is_no_obvious_emotion_with_candidates():
    r = _clf()._decide("记得提交标准是什么", {"开心": 0.3, "亲近": 0.25, "无明显情绪": 0.2}, {}, [], (), None)
    assert r.emotion == gc.NEUTRAL and r.neutral_reason
    assert r.candidates == ["开心", "亲近"]  # runner-ups survive as candidates; 无明显情绪 itself never does


def test_small_margin_is_unsure_with_reason():
    r = _clf()._decide("可以呀", {"开心": 0.5, "亲近": 0.45}, {}, [], (), None)
    assert r.emotion == gc.UNSURE and r.ambiguous and "接近" in (r.abstain_reason or "")


def test_clear_positive_label_passes():
    r = _clf()._decide("那必须的", {"亲近": 0.7, "开心": 0.1}, {"structbert": "亲近"}, [], (), 0.8)
    assert r.emotion == "亲近" and not r.ambiguous


def test_link_text_is_neutral_regardless_of_scores():
    r = _clf()._decide("https://x.y/z", {"不满": 0.9}, {"structbert": "不满", "bge_knn": "不满"}, [], (), 0.9)
    assert r.emotion == gc.NEUTRAL


# --- calibration ------------------------------------------------------------------------------


def test_apply_temperature_identity_and_flattening():
    probs = {"a": 0.8, "b": 0.15, "c": 0.05}
    assert apply_temperature(probs, 1.0) == probs
    flat = apply_temperature(probs, 2.0)
    assert sum(flat.values()) == pytest.approx(1.0)
    assert flat["a"] < probs["a"] and max(flat, key=flat.get) == "a"


# --- anchors ----------------------------------------------------------------------------------


def _index() -> AnchorIndex:
    vectors = np.eye(3, dtype=np.float32)
    return AnchorIndex({"version": "t"}, vectors, ["开心", "不满", "亲近"], ["a", "b", "c"])


def test_anchor_index_math():
    idx = _index()
    near = idx.nearest(np.array([1.0, 0.0, 0.0], dtype=np.float32), k=1)
    assert near[0].label == "开心" and near[0].score == pytest.approx(1.0)
    knn = idx.knn_scores(np.array([0.9, 0.1, 0.0], dtype=np.float32), k=3)
    assert sum(knn.values()) == pytest.approx(1.0) and max(knn, key=knn.get) == "开心"
    cent = idx.centroid_scores(np.array([0.0, 1.0, 0.0], dtype=np.float32))
    assert set(cent) == {"开心", "不满", "亲近"} and max(cent, key=cent.get) == "不满"


def test_knn_vote_corrects_for_anchor_class_imbalance():
    """A crowded class must not drown out a rare one when the rare anchor is the closest match.

    Without the count**PRIOR_ALPHA correction the 20 crowded neighbours (sim 0.95 each) outweigh
    the single exact match (sim 1.0); with it the rare class wins, which is what recovers 伤心.
    """
    import math

    query = np.array([1.0, 0.0], dtype=np.float32)
    other = [0.95, math.sqrt(1 - 0.95**2)]
    vectors = np.array([[1.0, 0.0]] + [other] * 20, dtype=np.float32)
    labels = ["伤心"] + ["无奈"] * 20
    idx = AnchorIndex({"version": "t"}, vectors, labels, ["s"] + ["u"] * 20)
    assert idx.counts == {"伤心": 1, "无奈": 20}
    knn = idx.knn_scores(query, k=15)
    assert max(knn, key=knn.get) == "伤心"
    assert knn["伤心"] == pytest.approx(1.0 / (1.0 + 14 * 0.95**3 / 20), abs=1e-4)


def test_anchor_roundtrip_encrypted(tmp_path, monkeypatch):
    monkeypatch.setattr("rapport.grounded.anchors.get_settings", lambda: SimpleNamespace(data_dir=tmp_path))
    clear_cache()
    crypto = Crypto(tmp_path / ".key")
    write_index(anchors_path(), crypto, {"version": "t"}, np.eye(3, dtype=np.float32), ["开心", "不满", "亲近"], ["a", "b", "c"])
    raw = anchors_path().read_bytes()
    assert b"\xe5\xbc\x80\xe5\xbf\x83" not in raw  # 开心 never appears in plaintext on disk
    idx = load_index()
    assert idx is not None and idx.status()["count"] == 3 and idx.dim == 3
    clear_cache()


# --- readings ---------------------------------------------------------------------------------


def test_readings_carry_votes_and_neighbor_labels(monkeypatch):
    class Fake:
        engine_names = ["structbert", "bge_knn"]

        def classify(self, texts):
            return [
                gc.GroundedResult(
                    text=t, emotion="亲近", scores={"亲近": 0.7, "开心": 0.2},
                    votes={"structbert": "亲近", "bge_knn": "亲近", "centroid": "开心"},
                    neighbors=[Neighbor(0.83, "亲近", "secret anchor text")], cues=("询问",), struct_prob=0.6,
                )
                for t in texts
            ]

    monkeypatch.setattr(gc.GroundedClassifier, "build", classmethod(lambda cls, use_markers=True: Fake()))
    out = gc.readings_from_conversation(_conv(["想你了"]), 0, 0, {"max_messages": 10})
    assert len(out) == 1
    r = out[0]
    assert r.source == "grounded" and r.emotions == ["亲近"] and r.pragmatic_tags == ["询问"]
    assert r.method_votes["bge_knn"] == "亲近" and r.independent_units == 2
    assert r.similar_examples[0].label == "亲近" and "secret" not in r.model_dump_json()
    assert r.evidence_ids == ["m0"] and r.post_hoc is False


def test_readings_empty_when_disabled_or_no_text():
    assert gc.readings_from_conversation(_conv(["想你了"]), 0, 0, {"max_messages": 0}) == []
    assert gc.readings_from_conversation(_conv([]), 0, 0, {"max_messages": 10}) == []


# --- change points ----------------------------------------------------------------------------


def test_change_points_insufficient_sessions_is_quiet():
    payload = detect_change_points(_conv(["a", "b", "c", "d"]), 0, 3)
    assert payload["status"] == "insufficient_sessions" and payload["points"] == []
    assert to_schema(payload) == []


# --- config -----------------------------------------------------------------------------------


def test_config_from_clamps_and_defaults():
    assert _config_from({}) == DEFAULT_EMOTION_CONFIG
    cfg = _config_from({"engine": "nope", "max_messages": 99999, "use_markers": 0})
    assert cfg["engine"] == "grounded" and cfg["max_messages"] == 500 and cfg["use_markers"] is False
    assert _config_from({"engine": "structbert"})["engine"] == "structbert"


def test_marker_cues_map_only_listed_markers(monkeypatch):
    monkeypatch.setattr(markers, "markers_for", lambda text: ("please", "gratitude", "factuality"))
    assert markers.cues_for("x") == ("询问", "信息确认")
