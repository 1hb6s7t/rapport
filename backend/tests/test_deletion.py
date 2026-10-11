"""Phase 5: deletion cascade and no-leak checks (FR-017 / FR-019).

What is verified here is what exists today: raw blobs, reports, snapshots, portrait versions and
diary entries. Vector indexes are not built yet, so there is nothing to cascade there — that is
stated rather than pretended.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from rapport.config import reset_settings
from rapport.db import (
    ActionLog,
    Analysis,
    DiaryRow,
    Event,
    Import,
    Outcome,
    PersonaVersion,
    ReportRow,
    Snapshot,
)
from rapport.state import AppState

CHAT = "\n".join(f"[2026-08-{d:02d} 20:00] {'A' if d % 2 else 'B'}: 消息{d} 今天有点累" for d in range(1, 16))


def _state() -> AppState:
    return AppState(reset_settings(data_dir=Path(tempfile.mkdtemp()), pairing_code="123456"))


def _blob_files(st: AppState) -> list[Path]:
    return sorted(st.settings.blob_dir.glob("*.bin"))


def _import_and_confirm(c, text: str = CHAT) -> dict:
    imp = c.post("/api/imports", json={"text": text}).json()
    c.post(f"/api/imports/{imp['id']}/confirm", json={})
    return imp


# ---------------------------------------------------------------- raw material


def test_deleting_an_import_removes_the_encrypted_blob(app_client):
    c = app_client
    st = c.app_state
    imp = _import_and_confirm(c)
    assert _blob_files(st), "the raw material should be stored encrypted while it is needed"

    assert c.delete(f"/api/imports/{imp['id']}").status_code == 200
    assert _blob_files(st) == [], "the encrypted blob must be gone from disk"
    assert c.get(f"/api/imports/{imp['id']}").status_code == 404


def test_analysis_on_a_deleted_import_fails_cleanly(app_client):
    c = app_client
    imp = _import_and_confirm(c)
    c.delete(f"/api/imports/{imp['id']}")
    r = c.post("/api/analyses", json={"import_id": imp["id"], "mode": "rules"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


def test_expired_material_is_purged_and_reported_as_gone(app_client):
    """Raw chats must not outlive the retention window; the API says so instead of pretending."""
    c = app_client
    st = c.app_state
    imp = _import_and_confirm(c)
    with st.db.session() as s:
        row = s.get(Import, imp["id"])
        row.expires_at = datetime.now() - timedelta(hours=1)

    assert st.purge_expired() == 1
    assert _blob_files(st) == []
    r = c.get(f"/api/imports/{imp['id']}")
    assert r.status_code == 404, "an expired import is not readable"


def test_events_survive_as_deduplicated_evidence_but_hold_no_text(app_client):
    """The ledger keeps fingerprints, not content — deleting the material cannot be undone from it."""
    c = app_client
    st = c.app_state
    imp = _import_and_confirm(c)
    with st.db.session() as s:
        before = [(e.fingerprint, e.sender, e.type) for e in s.query(Event).all()]
    assert before, "confirmed imports create canonical events"
    c.delete(f"/api/imports/{imp['id']}")
    with st.db.session() as s:
        rows = s.query(Event).all()
    assert [r.fingerprint for r in rows] == [f for f, _, _ in before]
    # no column on Event carries message text
    assert not hasattr(rows[0], "text")


# ---------------------------------------------------------------- derived data


def test_deleting_a_report_removes_its_body_snapshot_and_portrait_version(app_client):
    c = app_client
    st = c.app_state
    imp = _import_and_confirm(c)
    a = c.post("/api/analyses", json={"import_id": imp["id"], "mode": "byok", "profile": {"other": {"mbti": "ENTJ"}}}).json()
    pv = c.get(f"/api/analyses/{a['id']}/preview").json()
    c.post(f"/api/analyses/{a['id']}/confirm", json={"payload_hash": pv["payload_hash"], "accept_cost": True})
    st.runner.run_pending_once()
    rid = c.get(f"/api/analyses/{a['id']}").json()["report_id"]

    assert c.get("/api/trend").json()["points"], "a real analysis produces a history point"
    assert c.get("/api/persona/history").json()["versions"]

    assert c.delete(f"/api/reports/{rid}").status_code == 200
    assert c.get(f"/api/reports/{rid}").status_code == 404
    with st.db.session() as s:
        body = s.get(ReportRow, rid).body
        assert body == {"deleted": True}, "the report body must not survive deletion"
        assert s.query(Snapshot).filter(Snapshot.report_id == rid).count() == 0, "its history point goes too"
        assert not [v for v in s.query(PersonaVersion).all() if v.report_id == rid and v.active], "its portrait version is deactivated"


def test_wiping_clears_every_store_and_the_blob_directory(app_client):
    c = app_client
    st = c.app_state
    imp = _import_and_confirm(c)
    c.post("/api/diary", json={"text": "今天走了很远的路。", "allowed_relationships": ["rel-1"]})
    a = c.post("/api/analyses", json={"import_id": imp["id"], "mode": "rules"}).json()
    st.runner.run_pending_once()
    rid = c.get(f"/api/analyses/{a['id']}").json()["report_id"]
    # The two v0.5 records — what the user did and how it turned out — must wipe with everything
    # else, or "delete all data" would leave traces of the relationship behind.
    c.post(f"/api/reports/{rid}/actions/opt-1/done", json={"goal": "约她周末看展", "observed": "accepted"})
    with st.db.session() as s:
        rel = s.get(ReportRow, rid).relationship_id or "rel-1"
    st.outcome_record(rel, rid, "better", note="后来见了两次")

    out = c.delete("/api/data").json()
    assert out["ok"] and out["cleared"]["imports"] >= 1
    assert out["not_covered"], "what is out of scope must be stated"
    with st.db.session() as s:
        for model in (Import, Analysis, ReportRow, Snapshot, PersonaVersion, Event, DiaryRow, ActionLog, Outcome):
            assert s.query(model).count() == 0, f"{model.__name__} still has rows"
    assert _blob_files(st) == []


def test_deleting_a_report_removes_its_execution_log(app_client):
    """「我做了」belongs to the report it came from; deleting the report takes it along."""
    c = app_client
    st = c.app_state
    imp = _import_and_confirm(c)
    a = c.post("/api/analyses", json={"import_id": imp["id"], "mode": "rules"}).json()
    st.runner.run_pending_once()
    rid = c.get(f"/api/analyses/{a['id']}").json()["report_id"]
    r = c.post(f"/api/reports/{rid}/actions/opt-1/done", json={"goal": "周末约看展", "observed": "alt_time", "note": "她说改天"})
    assert r.status_code == 200
    assert c.get(f"/api/reports/{rid}/actions").json()["items"], "记录应当能读回来"

    assert c.delete(f"/api/reports/{rid}").status_code == 200
    with st.db.session() as s:
        assert s.query(ActionLog).filter(ActionLog.report_id == rid).count() == 0, "删报告必须连带清掉它的执行记录"


def test_an_action_on_a_deleted_report_is_refused(app_client):
    c = app_client
    st = c.app_state
    imp = _import_and_confirm(c)
    a = c.post("/api/analyses", json={"import_id": imp["id"], "mode": "rules"}).json()
    st.runner.run_pending_once()
    rid = c.get(f"/api/analyses/{a['id']}").json()["report_id"]
    c.delete(f"/api/reports/{rid}")
    r = c.post(f"/api/reports/{rid}/actions/opt-1/done", json={"goal": "约她", "observed": "accepted"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


def test_diary_deletion_is_immediate_and_leaves_no_clause(app_client):
    c = app_client
    e = c.post("/api/diary", json={"text": "周末和朋友一起吃了饭。", "allowed_relationships": ["rel-1"]}).json()
    c.patch(f"/api/diary/{e['id']}", json={"approved_clause": "周末和朋友一起吃了饭。"})
    assert c.delete(f"/api/diary/{e['id']}").status_code == 200
    assert c.get(f"/api/diary/{e['id']}").status_code == 404
    assert c.post(f"/api/diary/{e['id']}/strict-reply").status_code == 404
    assert c.get("/api/diary/search", params={"q": "周末 一起 吃饭", "relationship_id": "rel-1"}).json()["entries"] == []


# ---------------------------------------------------------------- no leaks


def test_diagnostics_and_exports_carry_no_secrets_or_diary_text(app_client):
    c = app_client
    imp = _import_and_confirm(c)
    c.post("/api/diary", json={"text": "这句日记不该出现在报告或诊断里。", "allowed_relationships": ["rel-1"]})
    a = c.post("/api/analyses", json={"import_id": imp["id"], "mode": "rules"}).json()
    c.app_state.runner.run_pending_once()
    rid = c.get(f"/api/analyses/{a['id']}").json()["report_id"]

    diag = c.get("/api/diagnostics").text
    assert "sk-" not in diag and "日记不该" not in diag
    md = c.get(f"/api/reports/{rid}/export?format=md").text
    assert "这句日记不该" not in md
    js = c.get(f"/api/reports/{rid}/export?format=json").json()
    assert all(v["text"] == "[已省略]" for v in js.get("evidence", {}).values())
    assert "这句日记不该" not in c.get("/api/settings").text


def test_keys_never_reach_the_frontend_surface(app_client):
    c = app_client
    c.put("/api/settings", json={"preset": "custom", "base_url": "https://example.invalid/v1", "api_key": "sk-super-secret"})
    for path in ("/api/settings", "/api/health", "/api/diagnostics"):
        assert "sk-super-secret" not in c.get(path).text
