"""Outcome check-ins (plan §5.2): asked late, recorded once, never scored.

The check-in exists so the user's own experience can later calibrate the direction of the score
(stage 6). It is deliberately hard to answer too early: a report younger than the ask window has
no outcome to report yet, and asking anyway would train the user to click through the question.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from rapport.db import DEFAULT_RELATIONSHIP_ID, Outcome, ReportRow
from rapport.state.actions import OUTCOME_ASK_AFTER_DAYS, OUTCOMES

CHAT = "\n".join(f"[2026-08-{d:02d} 20:00] {'A' if d % 2 else 'B'}: 消息{d} 今天有点累" for d in range(1, 16))


def _report(c, age_days: int = 0) -> tuple[str, str]:
    """One finished report, aged by `age_days`, plus the relationship it belongs to."""
    imp = c.post("/api/imports", json={"text": CHAT}).json()
    c.post(f"/api/imports/{imp['id']}/confirm", json={})
    a = c.post("/api/analyses", json={"import_id": imp["id"], "mode": "rules"}).json()
    c.app_state.runner.run_pending_once()
    rid = c.get(f"/api/analyses/{a['id']}").json()["report_id"]
    with c.app_state.db.session() as s:
        row = s.get(ReportRow, rid)
        row.relationship_id = row.relationship_id or DEFAULT_RELATIONSHIP_ID
        if age_days:
            row.created_at = datetime.now() - timedelta(days=age_days)
        rel = row.relationship_id
    return rid, rel


def test_a_fresh_report_is_not_asked_about(app_client):
    c = app_client
    _rid, rel = _report(c)
    assert c.app_state.outcome_prompt(rel) is None, "刚出的报告还没有结果可报，问也白问"


def test_the_check_in_arrives_after_the_window(app_client):
    c = app_client
    st = c.app_state
    rid, rel = _report(c, age_days=OUTCOME_ASK_AFTER_DAYS + 1)
    prompt = st.outcome_prompt(rel)
    assert prompt is not None
    assert prompt["report_id"] == rid
    assert prompt["days_since"] >= OUTCOME_ASK_AFTER_DAYS
    assert set(prompt["options"]) == set(OUTCOMES)
    assert "不进入任何分数" in prompt["question"], "问的时候就要说清它不会被用来打分"


def test_answering_once_stops_the_asking(app_client):
    c = app_client
    st = c.app_state
    rid, rel = _report(c, age_days=OUTCOME_ASK_AFTER_DAYS + 1)
    st.outcome_record(rel, rid, "better", note="见了两次")
    assert st.outcome_prompt(rel) is None, "答过一次就不再追问同一份报告"
    with st.db.session() as s:
        assert s.query(Outcome).filter(Outcome.report_id == rid).count() == 1


def test_an_unknown_outcome_label_is_refused(app_client):
    c = app_client
    st = c.app_state
    rid, rel = _report(c, age_days=OUTCOME_ASK_AFTER_DAYS + 1)
    with pytest.raises(ValueError):
        st.outcome_record(rel, rid, "much_better")


def test_the_endpoint_reports_the_prompt_and_the_no_scoring_note(app_client):
    c = app_client
    rid, rel = _report(c, age_days=OUTCOME_ASK_AFTER_DAYS + 1)
    body = c.get(f"/api/relationships/{rel}/outcomes").json()
    assert body["prompt"] and body["prompt"]["report_id"] == rid
    assert "永远不进入好感度分数" in body["note"]

    r = c.post(f"/api/relationships/{rel}/outcomes", json={"report_id": rid, "outcome": "same", "met_since": True})
    assert r.status_code == 200 and r.json()["outcome"] == "same"
    assert c.get(f"/api/relationships/{rel}/outcomes").json()["prompt"] is None
    assert c.post(f"/api/relationships/{rel}/outcomes", json={"report_id": rid, "outcome": "nope"}).status_code == 422
