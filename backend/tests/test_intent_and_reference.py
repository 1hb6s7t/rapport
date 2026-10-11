"""Phase 1: reference library access rules and the intent contract."""

from __future__ import annotations

import json

import pytest

from rapport.features.episodes import build_episodes, episode_for
from rapport.knowledge.reference import (
    RESERVED_EVAL_PREFIX,
    STATUS_APPROVED,
    ReferenceLibrary,
    load_library,
    retrieve,
)
from rapport.llm.intent import (
    MIN_INDEPENDENT_UNITS,
    TARGET_MARK,
    apply_negative_gate,
    build_intent_input,
    validate_intent,
)
from rapport.llm.redact import RedactStats
from rapport.parsers import ParseOptions, parse
from rapport.schemas.report_v1 import Confidence, IntentReading

CHAT = """[2026-09-03 21:00] A: 你今天好像不太想聊
[2026-09-03 21:05] B: 没有啊
[2026-09-03 21:30] A: 那周末见一面？
[2026-09-03 21:31] B: 别再联系我了
"""


@pytest.fixture
def conv():
    c, _ = parse(CHAT, "c1", "paste", None, ParseOptions())
    return c


def _episode(conv, text: str):
    events = conv.events()
    blocks = build_episodes(conv, events)
    target = next(m for m in events if m.text == text)
    ep = episode_for(conv, blocks, target, mode="review")
    assert ep is not None
    return ep


# ---------------------------------------------------------------- reference library


def test_shipped_seeds_load_and_are_all_approved():
    lib = load_library()
    assert len(lib.examples) >= 40, "the plan requires >= 40 original seeds"
    assert all(e.license_review_status == STATUS_APPROVED for e in lib.examples)
    assert all(e.source_dataset == "rapport-original-seeds" for e in lib.examples)
    assert lib.skipped_unapproved == []


def test_all_pragmatic_tags_are_covered_by_seeds():
    lib = load_library()
    covered = {t for e in lib.examples for t in e.pragmatic_tags}
    for tag in ("真生气", "撒娇假生气", "调侃", "反讽", "委婉拒绝", "寻求安慰", "信息确认"):
        assert tag in covered, f"{tag} has no reference seed"


def test_unapproved_and_eval_entries_are_not_loaded(tmp_path):
    (tmp_path / "mixed.json").write_text(
        json.dumps(
            {
                "schema": "rapport-reference-v1",
                "examples": [
                    {"example_id": "ok:1", "text": "在的", "layer": "pragmatic", "license_review_status": STATUS_APPROVED, "pragmatic_tags": ["信息确认"]},
                    {"example_id": "third:1", "text": "未授权语料", "layer": "pragmatic", "license_review_status": "pending_review"},
                    {"example_id": f"{RESERVED_EVAL_PREFIX}p01", "text": "评测题不得进入库", "layer": "pragmatic", "license_review_status": STATUS_APPROVED},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    lib = load_library(tmp_path)
    assert [e.example_id for e in lib.examples] == ["ok:1"]
    assert any("third:1" in s for s in lib.skipped_unapproved)
    assert lib.skipped_reserved == [f"{RESERVED_EVAL_PREFIX}p01"]


def test_missing_directory_yields_empty_library(tmp_path):
    lib = load_library(tmp_path / "nope")
    assert lib.examples == [] and lib.sources == []


def test_retrieval_is_relevant_deterministic_and_capped():
    lib = load_library()
    q = "你不是说六点一定到吗 路上堵车 上次你也是这么说的 我生气了"
    a = retrieve(q, lib, k=3)
    b = retrieve(q, lib, k=3)
    assert [e.example_id for e in a] == [e.example_id for e in b], "ordering must be deterministic"
    assert len(a) <= 3 and a, "must return analogues"
    assert a[0].example_id == "seed:p001", "the 真生气 seed for this exact surface form should rank first"
    assert retrieve("完全不相关的外语内容 zzz", lib, k=3) == []


def test_retrieval_filters_by_layer():
    lib = load_library()
    assert retrieve("我生气了", lib, layer="affect") == []
    assert retrieve("我生气了", lib, layer="pragmatic")


# ---------------------------------------------------------------- intent input


def test_intent_input_redacts_names_marks_target_and_carries_ids(conv):
    lib = load_library()
    ep = _episode(conv, "没有啊")
    inp = build_intent_input(conv, ep, RedactStats(), [e.prompt_dict() for e in retrieve("没有啊 你今天好像不太想聊", lib, k=2)])
    joined = "\n".join(inp.lines)
    assert TARGET_MARK in joined and sum(1 for ln in inp.lines if TARGET_MARK in ln) == 1
    assert "没有啊" in joined and "A:" in joined and "B:" in joined
    assert inp.valid_ids == {m.id for m in ep.messages}
    prompt = inp.user_prompt()
    assert "参考例" in prompt and "不要引用它们的 id" in prompt
    assert inp.reference_ids and all(r.startswith("seed:") for r in inp.reference_ids)


def test_intent_input_without_references_stays_clean(conv):
    ep = _episode(conv, "没有啊")
    inp = build_intent_input(conv, ep)
    assert inp.reference_block == "" and inp.reference_ids == []
    assert "参考例" not in inp.user_prompt()


# ---------------------------------------------------------------- validation


def _raw(**kw) -> dict:
    base = {
        "literal_affect": "字面平淡",
        "pragmatic_tags": [{"tag": "真生气", "basis_ids": ["m000000"], "why": "上下文指向不满"}],
        "emotions": [{"tag": "不满", "basis_ids": ["m000000", "m000002"]}],
        "boundary_status": "none",
        "candidates": [{"reading": "可能只是累了", "likelihood": "low", "basis_ids": ["m000000"]}],
        "evidence_ids": ["m000000", "m000002"],
        "counter_evidence_ids": [],
        "evidence_strength": "medium",
        "model_confidence_raw": 0.8,
        "ambiguous": False,
        "abstain_reason": None,
    }
    base.update(kw)
    return base


def test_validate_intent_drops_illegal_ids_and_keeps_valid_ones(conv):
    ep = _episode(conv, "没有啊")
    inp = build_intent_input(conv, ep)
    r = validate_intent(_raw(evidence_ids=["m000000", "m999999"]), inp, independent_units=5)
    assert r.evidence_ids == ["m000000"]
    assert r.msg_id == ep.target.id and r.episode_id == ep.id and r.speaker == "other"
    assert r.model_confidence_raw == 0.8


def test_validate_intent_downgrades_claims_without_evidence(conv):
    ep = _episode(conv, "没有啊")
    inp = build_intent_input(conv, ep)
    r = validate_intent(_raw(evidence_ids=["m999999"], ambiguous=False, abstain_reason=None), inp, independent_units=5)
    assert r.evidence_ids == [] and r.ambiguous is True and r.abstain_reason


def test_validate_intent_filters_unknown_labels_and_clamps_confidence(conv):
    ep = _episode(conv, "没有啊")
    inp = build_intent_input(conv, ep)
    r = validate_intent(
        _raw(
            pragmatic_tags=[{"tag": "真生气", "basis_ids": ["m000000"]}, {"tag": "编造的标签", "basis_ids": ["m000000"]}],
            emotions=[{"tag": "不满", "basis_ids": ["m000000"]}, {"tag": "不存在的情绪", "basis_ids": ["m000000"]}],
            model_confidence_raw=7,
            boundary_status="乱填",
        ),
        inp,
        independent_units=5,
    )
    assert r.pragmatic_tags == ["真生气"] and r.emotions == ["不满"]
    assert r.model_confidence_raw == 1.0 and r.boundary_status == "none"


def test_validate_intent_caps_candidates(conv):
    ep = _episode(conv, "没有啊")
    inp = build_intent_input(conv, ep)
    r = validate_intent(_raw(candidates=[{"reading": f"解释{i}"} for i in range(6)]), inp, independent_units=5)
    assert len(r.candidates) == 3


def test_single_snippet_reading_is_shown_with_its_unit_count(conv):
    ep = _episode(conv, "没有啊")
    inp = build_intent_input(conv, ep)
    r = validate_intent(_raw(), inp, independent_units=1)
    # v0.8: no negative gate — a single snippet still shows the reading, flagged by its unit count
    assert r.pragmatic_tags == ["真生气"] and r.suppressed_reason is None and r.independent_units == 1


def test_negative_gate_allows_reading_with_enough_snippets(conv):
    ep = _episode(conv, "没有啊")
    inp = build_intent_input(conv, ep)
    r = validate_intent(_raw(), inp, independent_units=MIN_INDEPENDENT_UNITS)
    assert r.pragmatic_tags == ["真生气"] and r.suppressed_reason is None and r.ambiguous is False


def test_positive_readings_are_never_gated(conv):
    ep = _episode(conv, "没有啊")
    inp = build_intent_input(conv, ep)
    r = validate_intent(
        _raw(pragmatic_tags=[{"tag": "信息确认", "basis_ids": ["m000000"]}], emotions=[{"tag": "亲近", "basis_ids": ["m000002"]}]),
        inp,
        independent_units=1,
    )
    assert r.pragmatic_tags == ["信息确认"] and r.emotions == ["亲近"] and r.suppressed_reason is None


def test_apply_negative_gate_is_idempotent():
    r = IntentReading(msg_id="m1", pragmatic_tags=["真生气"], emotions=["不满"], independent_units=1, evidence_ids=["m0"])
    once = apply_negative_gate(r)
    twice = apply_negative_gate(once)
    assert once.pragmatic_tags == twice.pragmatic_tags == ["真生气"], "v0.8: the gate is a no-op"


def test_boundary_status_is_preserved(conv):
    ep = _episode(conv, "别再联系我了")
    inp = build_intent_input(conv, ep)
    r = validate_intent(_raw(boundary_status="stop_contact", evidence_ids=[ep.target.id]), inp, independent_units=1)
    assert r.boundary_status == "stop_contact"
    assert r.evidence_ids == [ep.target.id]


def test_candidate_likelihood_defaults_to_low(conv):
    ep = _episode(conv, "没有啊")
    inp = build_intent_input(conv, ep)
    r = validate_intent(_raw(candidates=[{"reading": "随便写", "likelihood": "bogus"}]), inp, independent_units=5)
    assert r.candidates[0].likelihood == Confidence.low


def test_library_type_helpers_work():
    lib = ReferenceLibrary(examples=load_library().examples)
    assert lib.get("seed:p001") is not None and lib.get("missing") is None
    assert len(lib.by_layer("pragmatic")) == len(lib.examples)


def test_tag_without_its_own_evidence_is_demoted(conv):
    """A tag must name its own basis; otherwise it becomes a candidate, not a claim."""
    ep = _episode(conv, "没有啊")
    inp = build_intent_input(conv, ep)
    r = validate_intent(
        _raw(pragmatic_tags=[{"tag": "真生气", "basis_ids": []}, {"tag": "调侃", "basis_ids": ["m000000"]}]),
        inp,
        independent_units=5,
    )
    assert r.pragmatic_tags == ["调侃"], "only the evidenced tag survives"
    assert r.tag_basis["调侃"] == ["m000000"]
    assert any("真生气" in c.reading for c in r.candidates), "the unevidenced tag stays visible as a candidate"


def test_tag_citing_a_nonexistent_id_is_demoted(conv):
    ep = _episode(conv, "没有啊")
    inp = build_intent_input(conv, ep)
    r = validate_intent(_raw(pragmatic_tags=[{"tag": "真生气", "basis_ids": ["m999999"]}]), inp, independent_units=5)
    assert r.pragmatic_tags == []


def test_legacy_bare_string_tags_are_demoted(conv):
    """Backward compatibility: old-shape output cannot smuggle in unevidenced claims."""
    ep = _episode(conv, "没有啊")
    inp = build_intent_input(conv, ep)
    r = validate_intent(_raw(pragmatic_tags=["真生气"], emotions=["不满"]), inp, independent_units=5)
    assert r.pragmatic_tags == [] and r.emotions == []
    assert any("没有给出自己的证据" in c.reading for c in r.candidates)


def test_each_emotion_can_carry_its_own_basis(conv):
    ep = _episode(conv, "没有啊")
    inp = build_intent_input(conv, ep)
    r = validate_intent(
        _raw(emotions=[{"tag": "不满", "basis_ids": ["m000000"]}, {"tag": "伤心", "basis_ids": []}]),
        inp,
        independent_units=5,
    )
    assert r.emotions == ["不满"] and "伤心" not in r.tag_basis
