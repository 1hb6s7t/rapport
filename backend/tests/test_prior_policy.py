"""The label prior policy is a user setting: the defaults reproduce the documented 15% / 4:2:1
split, custom values flow through reports and cache keys, and the objective score never sees them."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from rapport.config import reset_settings
from rapport.jobs.pipeline import run_rules
from rapport.parsers import ParseOptions, parse
from rapport.report.assemble import refresh_labels
from rapport.schemas.profile import ChineseZodiac, Person, Profile, Zodiac
from rapport.scoring.prior import (
    DEFAULT_POLICY,
    PRIOR_POLICY_VERSION,
    PRIOR_SHARES,
    PriorError,
    PriorPolicy,
    policy_shares,
    policy_version,
    prior_weights,
)
from rapport.state import AppState

ALL = {"mbti": "INTJ", "zodiac": "aries", "chinese_zodiac": "dragon"}


# ---------------------------------------------------------------- engine


def test_default_policy_is_the_documented_one():
    assert policy_shares(DEFAULT_POLICY) == pytest.approx(PRIOR_SHARES)
    assert policy_version(DEFAULT_POLICY) == PRIOR_POLICY_VERSION
    d = prior_weights(0, ALL).as_dict()
    assert d["alpha0"] == pytest.approx(0.15) and d["policy_version"] == PRIOR_POLICY_VERSION


def test_custom_budget_and_split_flow_into_weights():
    p = PriorPolicy(alpha0=0.3, ratio=(1, 1, 0))
    b = prior_weights(0, ALL, p)
    assert b.weights["mbti"] == pytest.approx(0.15)
    assert b.weights["zodiac"] == pytest.approx(0.15)
    assert b.weights["chinese_zodiac"] == pytest.approx(0.0)
    assert b.unknown == pytest.approx(0.7)
    assert sum(b.weights.values()) + b.unknown == pytest.approx(1.0)
    v = b.as_dict()["policy_version"]
    assert v.startswith("auxiliary-custom-")
    assert v != policy_version(PriorPolicy(alpha0=0.3, ratio=(1, 1, 1))), (
        "a different split is a different version"
    )


def test_zero_budget_means_labels_claim_nothing():
    b = prior_weights(0, ALL, PriorPolicy(alpha0=0.0))
    assert all(w == 0 for w in b.weights.values()) and b.unknown == pytest.approx(1.0)
    b2 = prior_weights(0, ALL, PriorPolicy(ratio=(0, 0, 0)))
    assert all(w == 0 for w in b2.weights.values()) and b2.unknown == pytest.approx(1.0)


def test_scale_changes_how_fast_labels_fade():
    slow = prior_weights(40, ALL, PriorPolicy(scale=400)).alpha
    fast = prior_weights(40, ALL, PriorPolicy(scale=10)).alpha
    assert slow > prior_weights(40, ALL).alpha > fast


def test_policy_rejects_bad_values():
    for bad in (
        {"alpha0": 1.5},
        {"alpha0": -0.1},
        {"scale": 0},
        {"ratio": (-1, 1, 1)},
        {"alpha0": float("nan")},
    ):
        with pytest.raises(PriorError):
            PriorPolicy(**bad)


# ---------------------------------------------------------------- persisted setting


@pytest.fixture
def state():
    yield AppState(reset_settings(data_dir=Path(tempfile.mkdtemp()), pairing_code="123456"))


def test_unset_policy_is_default_and_patches_are_clamped_and_merged(state):
    assert state.prior_policy() == DEFAULT_POLICY
    assert state.prior_policy_view()["is_default"] is True
    state.set_prior_policy({"alpha0": 0.4, "mbti": 5})
    p = state.prior_policy()
    assert p.alpha0 == pytest.approx(0.4) and p.ratio == (5.0, 2.0, 1.0) and p.scale == 40.0
    state.set_prior_policy({"alpha0": 7})  # out of range → clamped; other fields untouched
    p = state.prior_policy()
    assert p.alpha0 == 1.0 and p.ratio == (5.0, 2.0, 1.0)
    assert state.prior_policy_version().startswith("auxiliary-custom-")
    view = state.prior_policy_view()
    assert view["is_default"] is False and view["default"]["alpha0"] == pytest.approx(0.15)
    state.set_prior_policy({"reset": True})
    assert state.prior_policy() == DEFAULT_POLICY
    assert state.prior_policy_version() == PRIOR_POLICY_VERSION


def test_saving_the_default_numbers_explicitly_still_counts_as_default(state):
    state.set_prior_policy({"alpha0": 0.15, "scale": 40, "mbti": 4, "zodiac": 2, "chinese_zodiac": 1})
    assert state.prior_policy() == DEFAULT_POLICY and state.prior_policy_view()["is_default"]


# ---------------------------------------------------------------- report integration

DEMO = """[2026-08-20 20:00] A: 今天好累
[2026-08-20 20:10] B: 辛苦了，早点休息
[2026-08-21 09:00] B: 早呀
[2026-08-21 09:30] A: 早
[2026-08-22 21:00] B: 周末一起吃个饭？
[2026-08-22 21:05] A: 好呀
"""


def _conv():
    return parse(DEMO, "c1", "paste", None, ParseOptions())[0]


def test_report_carries_the_policy_and_the_score_ignores_it():
    conv = _conv()
    profile = Profile(other=Person(mbti="INTJ", zodiac=Zodiac.aries, chinese_zodiac=ChineseZodiac.dragon))
    base = run_rules(conv, conv.events(), profile, n_eff=0.0)
    heavy = run_rules(
        conv, conv.events(), profile, n_eff=0.0, prior_policy=PriorPolicy(alpha0=0.6, ratio=(1, 0, 0))
    )
    assert base.prior_breakdown.policy_version == PRIOR_POLICY_VERSION
    assert heavy.prior_breakdown.policy_version.startswith("auxiliary-custom-")
    assert heavy.prior_breakdown.alpha0 == pytest.approx(0.6)
    assert heavy.prior_breakdown.weights["mbti"] == pytest.approx(0.6)
    assert (
        heavy.prior_breakdown.weights["zodiac"] == 0 and heavy.prior_breakdown.weights["chinese_zodiac"] == 0
    )
    assert sum(p.weight for p in heavy.priors) > sum(p.weight for p in base.priors)
    for key in ("affinity", "trend", "stage", "boundary", "playbook"):
        assert getattr(base, key).model_dump() == getattr(heavy, key).model_dump(), (
            f"{key} must not move with the policy"
        )


def test_relabel_uses_the_policy_it_is_given():
    conv = _conv()
    rep = run_rules(conv, conv.events(), Profile(other=Person(mbti="INTJ")), n_eff=0.0)
    rep2 = refresh_labels(rep, Profile(other=Person(mbti="ESFJ")), PriorPolicy(alpha0=0.5, ratio=(1, 0, 0)))
    assert rep2.prior_breakdown.alpha0 == pytest.approx(0.5)
    assert [p.value for p in rep2.priors] == ["ESFJ"] and rep2.priors[0].weight == pytest.approx(0.5)


# ---------------------------------------------------------------- API


def test_preferences_api_roundtrip(app_client):
    c = app_client
    v = c.get("/api/preferences/prior").json()
    assert (
        v["is_default"] is True
        and v["alpha0"] == pytest.approx(0.15)
        and v["version"] == PRIOR_POLICY_VERSION
    )
    v = c.patch("/api/preferences/prior", json={"alpha0": 0.3, "zodiac": 0}).json()
    assert v["is_default"] is False and v["alpha0"] == pytest.approx(0.3) and v["ratio"]["zodiac"] == 0
    assert v["shares"]["zodiac"] == 0 and v["shares"]["mbti"] == pytest.approx(0.3 * 4 / 5)
    assert v["version"].startswith("auxiliary-custom-") and c.app_state.prior_policy_version() == v["version"]
    assert c.patch("/api/preferences/prior", json={"alpha0": 2}).status_code == 422
    v = c.patch("/api/preferences/prior", json={"reset": True}).json()
    assert v["is_default"] is True and v["version"] == PRIOR_POLICY_VERSION


# ---------------------------------------------------------------- reply side


def test_zero_budget_hides_labels_from_drafting_and_ranking():
    from types import SimpleNamespace

    from rapport.analysis_context import build_context, rank_candidates
    from rapport.schemas.reply import ReplyDraft

    conv, _ = parse("我: 你怎么了？\n对方: 没事。\n我: 那我先忙", "example", "auto")
    imp = SimpleNamespace(id="i", content_hash="h", range_start=0, range_end=2)
    person = Person(alias="B", mbti="INTJ", zodiac=Zodiac.aries, confirmed=True)

    off = SimpleNamespace(
        relationship_person=lambda rid: person, prior_policy=lambda: PriorPolicy(alpha0=0.0)
    )
    ctx = build_context(off, "default", imp, conv, report={"id": "r"})
    assert ctx["auxiliary_policy"]["auxiliary_max"] == 0 and ctx["auxiliary_policy"]["evidence"] == 1
    assert "mbti" not in ctx["profile_facts"]["other"] and "zodiac" not in ctx["profile_facts"]["other"]

    on = SimpleNamespace(relationship_person=lambda rid: person, prior_policy=lambda: PriorPolicy(alpha0=0.4))
    ctx = build_context(on, "default", imp, conv, report={"id": "r"})
    assert ctx["profile_facts"]["other"]["mbti"] == "INTJ"
    assert ctx["auxiliary_policy"]["auxiliary_max"] == pytest.approx(0.4) and ctx["auxiliary_policy"][
        "version"
    ].startswith("auxiliary-custom-")

    bare = build_context(SimpleNamespace(), "default", imp, conv, report={"id": "r"})
    assert bare["auxiliary_policy"]["auxiliary_max"] == pytest.approx(0.15), (
        "a state without the setting keeps the default"
    )

    def drafts():
        evidence_first = ReplyDraft(style="casual", text="a", ranking={"evidence_fit": 0.8, "auxiliary_fit": 0.1})
        label_first = ReplyDraft(style="warm", text="b", ranking={"evidence_fit": 0.3, "auxiliary_fit": 1})
        return evidence_first, label_first

    evidence_first, label_first = drafts()
    ranked = rank_candidates([label_first, evidence_first], True, auxiliary_weight=0.0)
    assert ranked[0] is evidence_first and ranked[0].ranking["auxiliary_weight"] == 0
    evidence_first, label_first = drafts()
    ranked = rank_candidates([label_first, evidence_first], True, auxiliary_weight=0.9)
    assert ranked[0] is label_first and ranked[0].ranking["auxiliary_weight"] == pytest.approx(0.9)
    # Re-ranking drafts that were already ranked under a zero weight must not crash.
    assert rank_candidates(ranked, True, auxiliary_weight=0.0)[0].ranking["auxiliary_weight"] == 0
