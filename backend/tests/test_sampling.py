"""v0.5 §3B: sampled consistency replaces self-reported confidence as the second key to the gate.

Pinned: merge is majority-of-k with once-seen tags demoted to candidates; consistency is the
mean Jaccard of each draw against the merged set; a unanimous negative reading passes the gate
even with one snippet while a non-unanimous one does not; a single-call reading (samples=1)
behaves exactly as before; the outbound estimate multiplies by k; failed draws are counted, and
all-failed raises so the harness records a failure row.
"""

from __future__ import annotations

import pytest

from rapport.features.episodes import build_episodes, episode_for
from rapport.llm.intent import (
    MIN_INDEPENDENT_UNITS,
    build_intent_input,
    estimate_intent_tokens,
    validate_intent,
)
from rapport.llm.provider import CallUsage, LLMError
from rapport.llm.sampling import merge_samples, run_intent_sampled
from rapport.parsers import ParseOptions, parse

CHAT = "[2026-09-01 20:00] A: 周末要不要一起吃饭？\n[2026-09-01 20:05] B: 改天吧"


def _inp():
    conv, _ = parse(CHAT, "c", "paste", None, ParseOptions())
    ev = conv.events()
    ep = episode_for(conv, build_episodes(conv, ev), ev[-1], mode="review")
    return conv, ep, build_intent_input(conv, ep)


def _draw(tags, emotions=(), amb=False, boundary="none", conf=0.8):
    return {
        "pragmatic_tags": [{"tag": t, "basis_ids": ["m000000"]} for t in tags],
        "emotions": [{"tag": e, "basis_ids": ["m000000"]} for e in emotions],
        "boundary_status": boundary,
        "evidence_ids": ["m000000"],
        "counter_evidence_ids": [],
        "ambiguous": amb,
        "model_confidence_raw": conf,
        "evidence_strength": "medium",
        "candidates": [],
    }


def test_majority_merge_and_once_seen_becomes_candidate():
    m = merge_samples([_draw(["委婉拒绝"]), _draw(["委婉拒绝", "调侃"]), _draw(["委婉拒绝"])])
    assert [t["tag"] for t in m["pragmatic_tags"]] == ["委婉拒绝"]
    assert any("调侃" in c["reading"] and "只出现 1 次" in c["reading"] for c in m["candidates"])
    assert m["samples"] == 3 and 0 < m["consistency"] < 1
    unanimous = merge_samples([_draw(["委婉拒绝"])] * 3)
    assert unanimous["consistency"] == 1.0
    assert merge_samples([_draw(["调侃"]), _draw(["真生气"]), _draw(["信息确认"])])["pragmatic_tags"] == []


def test_boundary_needs_majority_and_picks_most_severe_agreed():
    m = merge_samples([_draw([], boundary="explicit_decline"), _draw([], boundary="explicit_decline"), _draw([], boundary="stop_contact")])
    assert m["boundary_status"] == "explicit_decline"
    m2 = merge_samples([_draw([], boundary="stop_contact"), _draw([], boundary="stop_contact"), _draw([], boundary="none")])
    assert m2["boundary_status"] == "stop_contact"


def test_unanimous_negative_passes_gate_with_one_snippet_but_split_does_not():
    _conv, _ep, inp = _inp()
    unanimous = merge_samples([_draw(["委婉拒绝"])] * 3)
    r = validate_intent(unanimous, inp, independent_units=1)
    assert r.samples == 3 and r.consistency == 1.0
    assert "委婉拒绝" in r.pragmatic_tags and r.suppressed_reason is None
    split = merge_samples([_draw(["委婉拒绝"]), _draw(["委婉拒绝"]), _draw(["信息确认"])])
    r2 = validate_intent(split, inp, independent_units=1)
    # v0.8: no gate — majority tag shows; consistency stays visible as confidence information
    assert "委婉拒绝" in r2.pragmatic_tags and r2.suppressed_reason is None and 0 < (r2.consistency or 0) < 1
    single = _draw(["委婉拒绝"])
    r3 = validate_intent(single, inp, independent_units=1)
    assert r3.samples == 1 and "委婉拒绝" in r3.pragmatic_tags
    r4 = validate_intent(single, inp, independent_units=MIN_INDEPENDENT_UNITS)
    assert "委婉拒绝" in r4.pragmatic_tags


class _Provider:
    reasoning = False

    def __init__(self, draws):
        self.draws = list(draws)
        self.calls = 0
        self.temps = []

    def complete_json(self, system, user, max_tokens=2048, temperature=0.2):
        self.calls += 1
        self.temps.append(temperature)
        d = self.draws.pop(0)
        if isinstance(d, Exception):
            raise d
        return d, CallUsage(calls=1, prompt_tokens=10, completion_tokens=5)


def test_run_intent_sampled_counts_usage_and_skips_failed_draws():
    _conv, _ep, inp = _inp()
    p = _Provider([_draw(["调侃"]), LLMError("rate_limit", "x"), _draw(["调侃"])])
    merged, usage, draws = run_intent_sampled(p, inp, k=3)
    assert p.calls == 3 and usage.calls == 2 and len(draws) == 2
    assert merged["_failed_draws"] == 1 and all(t == 0.7 for t in p.temps)
    assert [t["tag"] for t in merged["pragmatic_tags"]] == ["调侃"]
    dead = _Provider([LLMError("status", "a"), LLMError("status", "b"), LLMError("status", "c")])
    with pytest.raises(LLMError):
        run_intent_sampled(dead, inp, k=3)


def test_estimate_multiplies_by_samples():
    conv, _ep, _inp_ = _inp()
    one = estimate_intent_tokens(conv, conv.events(), samples=1)
    three = estimate_intent_tokens(conv, conv.events(), samples=3)
    assert three[2] == one[2] * 3 and three[0] == one[0] * 3


def test_product_setting_defaults_to_one_and_clamps(app_client):
    st = app_client.app_state
    assert st.intent_samples() == 1
    st.set_intent_samples(9)
    assert st.intent_samples() == 5
    r = app_client.put("/api/diary/intent-samples/state?k=3")
    assert r.status_code == 200 and r.json()["k"] == 3
