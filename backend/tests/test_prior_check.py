"""v0.5 §4 (stage 5): prior cards checked against observed poles; rewording is not a revision.

Pinned: every MBTI type has ≥3 cards over ≥3 tendencies; a card is supported / contradicted /
untested by pole with the tendency's evidence ids; a persona whose wording changes but whose
pole does not produces no change note; pole_stability is the share of recent versions agreeing;
the report carries prior_checks only when an MBTI label is set.
"""

from __future__ import annotations

from rapport.knowledge.prior_check import (
    POLE_LABELS,
    TENDENCY_POLES,
    cards_for,
    check_priors,
    coverage_report,
    infer_pole,
)
from rapport.report.persona_versions import diff_persona, pole_stability
from rapport.schemas.report_v1 import Persona, Tendency


def test_every_type_has_at_least_three_cards_over_three_tendencies():
    cov = coverage_report()
    for mbti, n in cov.items():
        cards = cards_for({"mbti": mbti})
        assert n >= 3 and len(cards) == n, mbti
        assert len({c.tendency_key for c in cards}) >= 3, mbti
        for c in cards:
            a, b = TENDENCY_POLES[c.tendency_key][:2]
            assert c.lean in (a, b) and c.counterexample_prompt and c.text and POLE_LABELS[c.lean]


def test_no_label_means_no_cards_and_zodiac_never_produces_checks():
    assert cards_for({}) == [] and cards_for({"mbti": None}) == [] and cards_for({"zodiac": "leo"}) == []


def test_infer_pole_from_wording_and_unclear_when_mixed():
    assert infer_pole("directness", "偏直接") == "direct"
    assert infer_pole("directness", "比较含蓄") == "indirect"
    assert infer_pole("directness", "直接又含蓄") == "unclear"
    assert infer_pole("rhythm", "秒回") == "fast" and infer_pole("nope", "x") == "unclear"


def test_check_priors_three_states_with_evidence():
    cards = cards_for({"mbti": "ENTJ"})  # directness=direct, rhythm=fast, ...
    tend = {
        "directness": Tendency(label="偏含蓄", evidence_ids=["m1"]),  # contradicts
        "rhythm": Tendency(label="回复很快", pole="fast", evidence_ids=["m2"]),  # supports
        "need_expression": Tendency(label="说不清", evidence_ids=[]),  # unclear -> untested
    }
    out = {c.card.tendency_key: c for c in check_priors(cards, tend)}
    assert out["directness"].status == "contradicted" and out["directness"].evidence_ids == ["m1"]
    assert out["rhythm"].status == "supported" and out["rhythm"].observed_pole == "fast"
    assert out["need_expression"].status == "untested"
    assert out["detail_preference"].status == "untested"  # tendency absent
    d = out["directness"].as_dict()
    assert d["lean_label"] == "偏直接" and d["counterexample_prompt"]


def test_rewording_on_the_same_pole_is_not_a_change_but_a_pole_move_is():
    a = Persona(available=True, tendencies={"directness": Tendency(label="偏直接", evidence_ids=["m1"])})
    b = Persona(available=True, tendencies={"directness": Tendency(label="说话干脆", evidence_ids=["m2"])})
    assert diff_persona(a, b) == [], "same pole, different wording: no revision"
    c = Persona(available=True, tendencies={"directness": Tendency(label="比较含蓄", evidence_ids=["m3"])})
    ch = diff_persona(a, c)
    assert len(ch) == 1 and ch[0].kind == "changed" and ch[0].after == "比较含蓄"


def test_pole_stability_counts_agreement_over_recent_versions():
    hist = [
        {"directness": {"label": "偏直接", "pole": "direct"}},
        {"directness": {"label": "很直白"}},  # pole inferred
        {"directness": {"label": "偏含蓄", "pole": "indirect"}},
    ]
    cur = {"directness": Tendency(label="直接", pole="direct")}
    st = pole_stability(hist, cur)
    assert st == {"directness": 0.67}
    assert pole_stability([], cur) == {}
    assert pole_stability(hist, {"directness": Tendency(label="???")}) == {}  # unclear current: skipped


def test_report_carries_prior_checks_only_with_an_mbti_label(app_client):
    c = app_client
    chat = "\n".join(f"[2026-09-{d:02d} 20:00] {'A' if d % 2 else 'B'}: 消息{d} 今天怎么样" for d in range(1, 12))
    imp = c.post("/api/imports", json={"text": chat}).json()
    c.post(f"/api/imports/{imp['id']}/confirm", json={})
    a = c.post("/api/analyses", json={"import_id": imp["id"], "mode": "rules", "profile": {"other": {"mbti": "INTJ"}}}).json()
    c.app_state.runner.run_pending_once()
    rep = c.get(f"/api/reports/{c.get(f'/api/analyses/{a['id']}').json()['report_id']}").json()
    assert len(rep["prior_checks"]) >= 3
    assert all(pc["status"] == "untested" for pc in rep["prior_checks"]), "rules mode has no observed tendencies"
    # a different goal so the idempotency key (labels excluded) does not reuse the INTJ analysis
    a2 = c.post("/api/analyses", json={"import_id": imp["id"], "mode": "rules", "profile": {"goal": "invite"}}).json()
    c.app_state.runner.run_pending_once()
    rep2 = c.get(f"/api/reports/{c.get(f'/api/analyses/{a2['id']}').json()['report_id']}").json()
    assert rep2["prior_checks"] == []
