"""Phase 2: the label prior engine — decay, budget split, and the FR-002 evidence rule."""

from __future__ import annotations

import math
import tempfile
from pathlib import Path

import pytest

from rapport.config import reset_settings
from rapport.jobs.pipeline import run_rules
from rapport.knowledge.priors import CHINESE_ZODIAC_PRIORS, MBTI_PRIORS, ZODIAC_PRIORS
from rapport.parsers import ParseOptions, parse
from rapport.schemas.profile import ChineseZodiac, Person, Profile, Zodiac
from rapport.scoring.prior import (
    PRIOR_ALPHA0,
    PRIOR_SCALE,
    PRIOR_SHARES,
    PriorError,
    build_hypotheses,
    decay_table,
    prior_alpha,
    prior_weights,
)
from rapport.state import AppState

ALL_LABELS = {"mbti": "INTJ", "zodiac": "aries", "chinese_zodiac": "dragon"}


# ---------------------------------------------------------------- decay


def test_alpha_starts_at_the_documented_value_and_decays_monotonically():
    assert prior_alpha(0) == pytest.approx(PRIOR_ALPHA0) == pytest.approx(0.15)
    values = [prior_alpha(n) for n in range(0, 200, 5)]
    assert all(b < a for a, b in zip(values, values[1:], strict=False)), "must decrease monotonically"
    assert min(values) >= 0.0


def test_alpha_matches_the_published_formula():
    for n in (0, 10, 40, 100):
        assert prior_alpha(n) == pytest.approx(0.15 * math.exp(-n / PRIOR_SCALE))


def test_alpha_rejects_bad_parameters():
    for bad in (-1, float("nan"), float("inf")):
        with pytest.raises(PriorError):
            prior_alpha(bad)
    with pytest.raises(PriorError):
        prior_alpha(10, alpha0=1.5)
    with pytest.raises(PriorError):
        prior_alpha(10, scale=0)


def test_decay_table_is_displayable():
    table = decay_table(n_eff_max=60, step=20)
    assert table[0] == (0, 0.15) and len(table) == 4
    assert table[-1][1] < table[0][1]


# ---------------------------------------------------------------- budget split


def test_full_labels_claim_the_documented_budget():
    b = prior_weights(0, ALL_LABELS)
    assert b.weights["mbti"] == pytest.approx(0.15 * 4 / 7)
    assert b.weights["zodiac"] == pytest.approx(0.15 * 2 / 7)
    assert b.weights["chinese_zodiac"] == pytest.approx(0.15 / 7)
    assert b.unknown == pytest.approx(0.85)
    assert set(b.present) == {"mbti", "zodiac", "chinese_zodiac"}


def test_missing_label_goes_to_unknown_not_to_the_others():
    """A blank label must not silently strengthen the labels the user did provide."""
    only_mbti = prior_weights(0, {"mbti": "INTJ"})
    assert only_mbti.weights["mbti"] == pytest.approx(0.15 * 4 / 7), "MBTI keeps its own share only"
    assert "zodiac" not in only_mbti.weights and "chinese_zodiac" not in only_mbti.weights
    assert only_mbti.unknown == pytest.approx(1 - 0.15 * 4 / 7)
    assert sum(only_mbti.weights.values()) + only_mbti.unknown == pytest.approx(1.0)


def test_no_labels_means_everything_is_unknown():
    b = prior_weights(0, {})
    assert b.weights == {} and b.unknown == pytest.approx(1.0) and b.present == ()


def test_evidence_takes_over_as_labels_fade():
    """The hand-over the user asked for: chat evidence grows as the label weight decays."""
    start = prior_weights(0, ALL_LABELS)
    mid = prior_weights(40, ALL_LABELS)
    late = prior_weights(200, ALL_LABELS)
    assert sum(start.weights.values()) > sum(mid.weights.values()) > sum(late.weights.values())
    assert start.unknown < mid.unknown < late.unknown
    for key in ALL_LABELS:
        assert start.weights[key] > mid.weights[key] > late.weights[key]
    assert start.weights["mbti"] == pytest.approx(0.15 * 4 / 7), "initial MBTI is capped within the 15% product budget"
    assert sum(late.weights.values()) < 0.02, "after ~5x scale the labels are effectively gone"


def test_budget_always_sums_to_one():
    for n_eff in (0, 5, 40, 1000):
        for labels in (ALL_LABELS, {"mbti": "ENFP"}, {}):
            b = prior_weights(n_eff, labels)
            assert sum(b.weights.values()) + b.unknown == pytest.approx(1.0)


def test_full_labels_total_equals_alpha():
    """With every label present the label weights must sum to the reported alpha."""
    for n_eff in (0, 10, 40):
        b = prior_weights(n_eff, ALL_LABELS)
        assert sum(b.weights.values()) == pytest.approx(b.alpha)
        assert b.alpha == pytest.approx(prior_alpha(n_eff))
        assert b.unknown == pytest.approx(1.0 - b.alpha)


def test_shares_are_the_documented_ones():
    assert PRIOR_SHARES == {"mbti": 0.15 * 4 / 7, "zodiac": 0.15 * 2 / 7, "chinese_zodiac": 0.15 / 7}
    assert sum(PRIOR_SHARES.values()) + 0.85 == pytest.approx(1.0)


# ---------------------------------------------------------------- hypotheses


def test_hypotheses_carry_source_type_weight_and_text():
    hs, breakdown = build_hypotheses(0, ALL_LABELS)
    by_label = {h.label: h for h in hs}
    assert set(by_label) == {"mbti", "zodiac", "chinese_zodiac"}
    assert by_label["mbti"].source_type == "tag_hypothesis"
    assert by_label["mbti"].hypothesis == MBTI_PRIORS["INTJ"]
    assert by_label["mbti"].weight == pytest.approx(breakdown.weights["mbti"])
    assert by_label["mbti"].entertainment is False
    assert by_label["zodiac"].entertainment is True and by_label["chinese_zodiac"].entertainment is True
    assert by_label["zodiac"].hypothesis == ZODIAC_PRIORS["aries"]
    assert by_label["chinese_zodiac"].hypothesis == CHINESE_ZODIAC_PRIORS["dragon"]
    assert "初识阶段" in by_label["mbti"].basis


def test_hypotheses_are_omitted_when_labels_are_absent_or_unknown():
    hs, _ = build_hypotheses(0, {"mbti": None, "zodiac": "not-a-sign"})
    assert [h.label for h in hs] == [], "unknown or invalid values produce no hypothesis"


def test_all_mbti_types_have_original_prior_text():
    assert len(MBTI_PRIORS) == 16
    assert all(len(v) > 10 for v in MBTI_PRIORS.values())
    assert all("假设" in v for v in MBTI_PRIORS.values()), "priors must read as assumptions"
    assert len(ZODIAC_PRIORS) == 12 and len(CHINESE_ZODIAC_PRIORS) == 12


# ---------------------------------------------------------------- report integration


def _conv(text: str):
    return parse(text, "c1", "paste", None, ParseOptions())[0]


DEMO = """[2026-08-20 20:00] A: 今天好累
[2026-08-20 20:10] B: 辛苦了，早点休息
[2026-08-21 09:00] B: 早呀
[2026-08-21 09:30] A: 早
[2026-08-22 21:00] B: 周末一起吃个饭？
[2026-08-22 21:05] A: 好呀
"""


def test_report_shows_priors_and_they_decay_with_evidence():
    conv = _conv(DEMO)
    profile = Profile(
        other=Person(mbti="INTJ", zodiac=Zodiac.aries, chinese_zodiac=ChineseZodiac.dragon),
        me=Person(mbti="ENFP", zodiac=Zodiac.libra),
    )
    fresh = run_rules(conv, conv.events(), profile, n_eff=0.0)
    settled = run_rules(conv, conv.events(), profile, n_eff=40.0)
    assert fresh.priors and all(p.source_type == "tag_hypothesis" for p in fresh.priors)
    assert fresh.prior_breakdown.alpha > settled.prior_breakdown.alpha
    fresh_total = sum(p.weight for p in fresh.priors)
    settled_total = sum(p.weight for p in settled.priors)
    assert fresh_total > settled_total
    assert fresh.prior_breakdown.unknown < settled.prior_breakdown.unknown
    # the only labels used are the OTHER person's; my own labels must not appear
    assert {p.value for p in fresh.priors} == {"INTJ", "aries", "dragon"}


def test_priors_do_not_touch_the_affinity_score():
    """The hard boundary: labels shape a hypothesis, never the score."""
    conv = _conv(DEMO)
    bare = run_rules(conv, conv.events(), Profile(), n_eff=0.0)
    loaded = run_rules(
        conv,
        conv.events(),
        Profile(other=Person(mbti="INTJ", zodiac=Zodiac.aries, chinese_zodiac=ChineseZodiac.dragon)),
        n_eff=0.0,
    )
    assert bare.affinity.model_dump() == loaded.affinity.model_dump()
    assert bare.trend.model_dump() == loaded.trend.model_dump()
    assert bare.stage.model_dump() == loaded.stage.model_dump()
    assert bare.playbook.model_dump() == loaded.playbook.model_dump()
    assert loaded.priors, "but the hypothesis section does change"


def test_relabel_refreshes_priors_without_touching_core():
    from rapport.report.assemble import refresh_labels

    conv = _conv(DEMO)
    rep = run_rules(conv, conv.events(), Profile(other=Person(mbti="INTJ")), n_eff=0.0)
    before = rep.model_dump(mode="json")
    rep2 = refresh_labels(rep, Profile(other=Person(mbti="ESFJ", zodiac=Zodiac.pisces)))
    after = rep2.model_dump(mode="json")
    for key in ("affinity", "trend", "stage", "boundary", "playbook", "observations"):
        assert before[key] == after[key]
    assert {p["value"] for p in after["priors"]} == {"ESFJ", "pisces"}
    assert after["prior_breakdown"]["n_eff"] == before["prior_breakdown"]["n_eff"]


# ---------------------------------------------------------------- FR-002: only confirmed, deduped evidence counts


@pytest.fixture
def state():
    st = AppState(reset_settings(data_dir=Path(tempfile.mkdtemp()), pairing_code="123456"))
    yield st


CHAT = "\n".join(f"[2026-09-0{i} 20:00] {'A' if i % 2 else 'B'}: 消息{i} 哈哈" for i in range(1, 8))


def _save(st: AppState, text: str, cid: str):
    conv, pid = parse(text, cid, "paste", None, ParseOptions())
    return st.save_import(conv, pid, None), conv


def test_unconfirmed_imports_do_not_grow_n_eff(state):
    imp, _ = _save(state, CHAT, "c1")
    assert state.ledger_n_eff() == 0.0, "an unconfirmed import is not evidence yet"
    n = state.confirm_events(imp.id)
    assert n > 0
    assert state.ledger_n_eff() > 0.0


def test_reimporting_the_same_chat_does_not_grow_n_eff(state):
    """FR-002: repeated imports must not inflate the effective sample size."""
    imp1, _ = _save(state, CHAT, "c1")
    state.confirm_events(imp1.id)
    first = state.ledger_n_eff()

    imp2, _ = _save(state, CHAT, "c2")  # identical content, different import
    state.confirm_events(imp2.id)
    assert state.ledger_n_eff() == pytest.approx(first), "the same events must count once"


def test_episode_cap_bounds_a_single_long_session(state):
    """A single long burst cannot exceed the per-episode cap."""
    long_chat = "\n".join(f"[2026-09-01 20:{i:02d}] B: 消息{i}" for i in range(40))
    imp, _ = _save(state, long_chat, "c1")
    state.confirm_events(imp.id)
    assert state.ledger_n_eff() <= 5.0, "one episode is capped at 5 units"
