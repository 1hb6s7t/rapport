"""Phase 2: portrait revisions — versions, change reasons, history and invalidation (M04)."""

from __future__ import annotations

import tempfile
from pathlib import Path

from rapport.config import reset_settings
from rapport.db import PersonaVersion
from rapport.jobs.pipeline import ByokContext, run_byok, run_rules
from rapport.llm.extract import build_payload
from rapport.llm.fake import FakeProvider
from rapport.parsers import ParseOptions, parse
from rapport.report.persona_versions import diff_persona
from rapport.schemas.profile import Profile
from rapport.schemas.report_v1 import AxisLean, Confidence, Persona, Tendency, Trait
from rapport.state import AppState

DEMO_CHAT = """[2026-08-20 20:00] A: 今天好累
[2026-08-20 20:10] B: 辛苦了，早点休息
[2026-08-21 09:00] B: 早呀
[2026-08-21 09:30] A: 早
[2026-08-22 21:00] B: 周末一起吃个饭？
[2026-08-22 21:05] A: 好呀
"""


def _persona(directness: str = "偏直接", traits: list[str] | None = None, ei: str = "E") -> Persona:
    return Persona(
        available=True,
        tendencies={"directness": Tendency(label=directness, evidence_ids=["m000001"])},
        traits=[Trait(statement=t, evidence_ids=["m000002"], confidence=Confidence.medium) for t in (traits or ["在轻松话题上更活跃"])],
        inferred_axes={"EI": AxisLean(lean=ei, confidence=Confidence.medium)},
    )


# ---------------------------------------------------------------- diff


def test_first_portrait_has_nothing_to_diff():
    assert diff_persona(None, _persona()) == []


def test_changed_tendency_records_before_after_and_reason():
    changes = diff_persona(_persona("偏直接"), _persona("偏含蓄"))
    assert len(changes) == 1
    c = changes[0]
    assert c.kind == "changed" and c.before == "偏直接" and c.after == "偏含蓄"
    assert c.reason == "依据 m000001", "a change must point at its evidence"
    assert "修正" in c.describe() and "偏直接 → 偏含蓄" in c.describe()


def test_new_and_dropped_traits_are_both_recorded():
    changes = diff_persona(_persona(traits=["旧特质"]), _persona(traits=["新特质"]))
    kinds = {(c.kind, c.after or c.before) for c in changes}
    assert ("added", "新特质") in kinds
    assert ("removed", "旧特质") in kinds
    removed = next(c for c in changes if c.kind == "removed")
    assert "没有支持它的证据" in removed.reason


def test_axis_change_is_recorded():
    changes = diff_persona(_persona(ei="E"), _persona(ei="I"))
    assert any(c.field == "EI 轴" and c.before == "E" and c.after == "I" for c in changes)


def test_identical_portraits_produce_no_changes():
    assert diff_persona(_persona(), _persona()) == []


# ---------------------------------------------------------------- versions


def _conv():
    return parse(DEMO_CHAT, "c1", "paste", None, ParseOptions())[0]


def _byok_report(fake: FakeProvider, report_id: str):
    conv = _conv()
    ctx = ByokContext(provider=fake, payload=build_payload(conv, conv.events(), window_size=30, overlap=5), preset="ollama")
    return run_byok(conv, conv.events(), Profile(), ctx, report_id=report_id)


def _state():
    return AppState(reset_settings(data_dir=Path(tempfile.mkdtemp()), pairing_code="123456"))


def test_first_ai_report_creates_version_one_without_notes():
    st = _state()
    rep = _byok_report(FakeProvider(), "r1")
    assert st.save_persona_version(rep) == 1
    assert rep.persona.version == 1 and rep.persona.change_notes == []
    assert rep.persona.changed_from_report is None
    history = st.persona_history()
    assert len(history["versions"]) == 1 and history["versions"][0]["active"] is True


def test_second_report_creates_a_new_version_and_supersedes_the_old():
    st = _state()
    rep1 = _byok_report(FakeProvider(), "r1")
    st.save_persona_version(rep1)

    rep2 = _byok_report(FakeProvider(), "r2")
    rep2.persona.tendencies["directness"] = Tendency(label="偏含蓄", evidence_ids=["m000003"])
    rep2.persona.traits = []
    assert st.save_persona_version(rep2) == 2
    assert rep2.persona.version == 2 and rep2.persona.changed_from_report == "r1"
    assert rep2.persona.change_notes, "the revision must say what changed"
    assert any("偏含蓄" in c.after for c in rep2.persona.change_notes)

    history = st.persona_history()
    assert [v["version"] for v in history["versions"]] == [2, 1], "newest first"
    assert history["versions"][0]["active"] is True and history["versions"][1]["active"] is False
    assert history["versions"][1]["superseded_by"] == history["versions"][0]["report_id"] or history["versions"][1]["superseded_by"]
    assert "旧版本保留内容" in history["note"]
    with st.db.session() as s:
        assert s.query(PersonaVersion).count() == 2, "old versions are kept, not overwritten"


def test_rules_mode_creates_no_portrait_version():
    st = _state()
    conv = _conv()
    rep = run_rules(conv, conv.events(), Profile(), report_id="r1")
    assert rep.persona.available is False
    assert st.save_persona_version(rep) == 0
    assert st.persona_history()["versions"] == []


def test_deleting_a_report_deactivates_its_version():
    st = _state()
    rep = _byok_report(FakeProvider(), "r1")
    st.save_persona_version(rep)
    assert st.persona_history()["versions"][0]["active"] is True
    assert st.deactivate_persona_versions("r1") == 1
    assert st.persona_history()["versions"][0]["active"] is False


# ---------------------------------------------------------------- integration


def test_byok_analysis_stores_a_portrait_version_through_the_api(app_client):
    c = app_client
    imp = c.post("/api/imports", json={"text": DEMO_CHAT}).json()
    c.post(f"/api/imports/{imp['id']}/confirm", json={})
    a = c.post("/api/analyses", json={"import_id": imp["id"], "mode": "byok", "profile": {"other": {"mbti": "ENTJ"}}}).json()
    pv = c.get(f"/api/analyses/{a['id']}/preview").json()
    c.post(f"/api/analyses/{a['id']}/confirm", json={"payload_hash": pv["payload_hash"], "accept_cost": True})
    c.app_state.runner.run_pending_once()

    history = c.get("/api/persona/history").json()
    assert len(history["versions"]) == 1
    version = history["versions"][0]
    assert version["version"] == 1 and version["active"] is True
    assert version["tendencies"], "the stored version keeps the portrait content"

    rid = c.get(f"/api/analyses/{a['id']}").json()["report_id"]
    rep = c.get(f"/api/reports/{rid}").json()
    assert rep["persona"]["version"] == 1, "the report body carries its version number"

    # deleting the report takes the portrait revision out of service
    c.delete(f"/api/reports/{rid}")
    assert c.get("/api/persona/history").json()["versions"][0]["active"] is False
