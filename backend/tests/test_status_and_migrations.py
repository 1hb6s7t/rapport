"""v0.4: analysis status machine, additive schema upgrades, report schema seam, evidence quality."""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import inspect

from rapport.config import reset_settings
from rapport.db import Database
from rapport.jobs.status import (
    ACTIVE,
    NEEDS_MATERIAL,
    RUNNING,
    AnalysisStatus,
    IllegalTransition,
    can_transition,
    check_transition,
)
from rapport.schemas.migrate import UnsupportedReportSchema, register_upgrade, upgrade_report_body
from rapport.scoring.evidence import Evidence, effective_evidence, event_quality
from rapport.state import AppState
from tests.conftest import demo_text

# ---------------------------------------------------------------- status machine

S = AnalysisStatus


def test_intent_stage_is_part_of_the_active_set():
    """The v0.2 regression: a restart during the intent stage left rows stuck as `intent`."""
    assert S.intent in RUNNING and S.intent in ACTIVE and S.intent in NEEDS_MATERIAL
    assert S.created in NEEDS_MATERIAL and S.created not in ACTIVE


@pytest.mark.parametrize(
    "before,after,ok",
    [
        (S.created, S.queued, True),
        (S.created, S.extracting, False),
        (S.queued, S.assembling, True),
        (S.validating, S.synthesizing, True),  # repair retry
        (S.synthesizing, S.intent, True),
        (S.assembling, S.completed, True),
        (S.assembling, S.insufficient_data, True),
        (S.completed, S.queued, False),
        (S.failed, S.queued, True),
        (S.interrupted, S.cancelled, True),
        (S.cancelled, S.completed, False),
        (S.extracting, S.extracting, True),
    ],
)
def test_transitions(before, after, ok):
    assert can_transition(before, after) is ok
    if ok:
        check_transition(before, after)
    else:
        with pytest.raises(IllegalTransition):
            check_transition(before, after)


def test_restart_marks_a_row_interrupted_in_the_intent_stage(app_client):
    c = app_client
    st = c.app_state
    imp = c.post("/api/imports", json={"text": demo_text()}).json()
    c.post(f"/api/imports/{imp['id']}/confirm", json={})
    a = c.post("/api/analyses", json={"import_id": imp["id"], "mode": "rules"}).json()
    from rapport.db import Analysis

    with st.db.session() as s:
        s.get(Analysis, a["id"]).status = S.intent.value  # simulate a crash mid-stage
    out = st.reconcile_on_startup()
    assert out["interrupted"] == 1
    assert c.get(f"/api/analyses/{a['id']}").json()["status"] == "interrupted"
    assert c.post(f"/api/analyses/{a['id']}/retry").status_code == 200


def test_runner_refuses_to_complete_a_cancelled_row(app_client):
    c = app_client
    st = c.app_state
    imp = c.post("/api/imports", json={"text": demo_text()}).json()
    c.post(f"/api/imports/{imp['id']}/confirm", json={})
    a = c.post("/api/analyses", json={"import_id": imp["id"], "mode": "rules"}).json()
    c.post(f"/api/analyses/{a['id']}/cancel")
    assert st.runner.run_pending_once() == 0, "a cancelled row is never claimed"
    assert c.get(f"/api/analyses/{a['id']}").json()["status"] == "cancelled"
    assert c.post(f"/api/analyses/{a['id']}/retry").status_code == 409


# ---------------------------------------------------------------- schema upgrades


def test_ensure_schema_adds_missing_columns_to_an_old_database():
    tmp = Path(tempfile.mkdtemp()) / "old.db"
    con = sqlite3.connect(tmp)
    # a v0.3 `events` table: no relationship_id, no ts_precision
    con.execute(
        "CREATE TABLE events (fingerprint VARCHAR(40) PRIMARY KEY, first_import_id VARCHAR(32), msg_id VARCHAR(32),"
        " sender VARCHAR(16), type VARCHAR(16), ts DATETIME, episode_id VARCHAR(64), origin VARCHAR(16),"
        " confirmed BOOLEAN, seen_count INTEGER)"
    )
    con.execute("INSERT INTO events VALUES ('fp1','i1','m1','other','text',NULL,'i1:s0','observed_chat',1,1)")
    con.commit()
    con.close()

    db = Database(tmp)
    assert set(db.schema_changes.get("events", [])) >= {"relationship_id", "ts_precision"}
    cols = {c["name"] for c in inspect(db.engine).get_columns("events")}
    assert {"relationship_id", "ts_precision"} <= cols
    assert "relationships" in inspect(db.engine).get_table_names()
    with db.session() as s:
        from rapport.db import Event

        row = s.get(Event, "fp1")
        assert row.relationship_id == "default" and row.ts_precision == "none", "old rows read with defaults"
    # second open is a no-op
    assert Database(tmp).schema_changes == {}


def test_app_state_creates_the_default_relationship_on_an_upgraded_file():
    st = AppState(reset_settings(data_dir=Path(tempfile.mkdtemp()), pairing_code="123456"))
    assert [r["id"] for r in st.relationship_list()] == ["default"]


def test_report_body_upgrade_registry():
    assert upgrade_report_body({"schema_version": "report-v1", "id": "x"})["schema_version"] == "report-v1"
    assert upgrade_report_body({"id": "x"})["schema_version"] == "report-v1", "missing version means v1"
    with pytest.raises(UnsupportedReportSchema):
        upgrade_report_body({"schema_version": "report-v9"})

    @register_upgrade("report-v0", "report-v1")
    def _v0(body: dict) -> dict:
        out = dict(body)
        out["upgraded"] = True
        return out

    out = upgrade_report_body({"schema_version": "report-v0"})
    assert out["upgraded"] and out["schema_version"] == "report-v1"


def test_report_with_a_future_schema_is_refused_cleanly(app_client):
    c = app_client
    imp = c.post("/api/imports", json={"text": demo_text()}).json()
    c.post(f"/api/imports/{imp['id']}/confirm", json={})
    a = c.post("/api/analyses", json={"import_id": imp["id"], "mode": "rules"}).json()
    c.app_state.runner.run_pending_once()
    rid = c.get(f"/api/analyses/{a['id']}").json()["report_id"]
    from rapport.db import ReportRow

    with c.app_state.db.session() as s:
        row = s.get(ReportRow, rid)
        row.body = dict(row.body) | {"schema_version": "report-v9"}
    r = c.get(f"/api/reports/{rid}")
    assert r.status_code == 409 and r.json()["error"]["code"] == "unsupported_schema"


# ---------------------------------------------------------------- evidence quality


def test_event_quality_reflects_how_much_context_an_event_carries():
    assert event_quality("text", "minute") == 1.0
    assert event_quality("text", "second") == 1.0
    assert event_quality("text", "day") < 1.0
    assert event_quality("text", "none") < event_quality("text", "day")
    assert event_quality("image", "minute") < event_quality("text", "none")
    assert event_quality(None, None) == event_quality("text", "none")


def test_untimed_material_yields_less_effective_evidence_than_timed():
    timed = [Evidence(f"e{i}", f"s{i}", "text", event_quality("text", "minute")) for i in range(4)]
    untimed = [Evidence(f"e{i}", f"s{i}", "text", event_quality("text", "none")) for i in range(4)]
    assert effective_evidence(untimed) < effective_evidence(timed)
