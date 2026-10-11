"""Phase 4: why the advice changed — playbook change notes (M08)."""

from __future__ import annotations

import pytest

from rapport.jobs.pipeline import run_rules
from rapport.parsers import ParseOptions, parse
from rapport.report.playbook_change import diff_playbook
from rapport.schemas.profile import Profile
from rapport.schemas.report_v1 import (
    Action,
    Boundary,
    BoundaryValue,
    Landmine,
    Persona,
    PersonaChangeModel,
    Playbook,
    Stage,
    StageValue,
)

CHAT = "\n".join(f"[2026-08-{d:02d} 20:{d:02d}] {'A' if d % 2 else 'B'}: 消息{d} 周末有空吗" for d in range(1, 24))


def _report(**kw):
    conv, _ = parse(CHAT, "c1", "paste", None, ParseOptions())
    return run_rules(conv, conv.events(), Profile(**kw.pop("profile", {})), **kw)


def _body(stage: str = "getting_to_know", boundary: str = "none", version: int = 1, goal: str = "读懂互动", landmines: list[str] | None = None) -> dict:
    return {
        "stage": {"value": stage},
        "boundary": {"value": boundary},
        "persona": {"version": version},
        "playbook": {
            "actions": [{"goal": goal}],
            "landmines": [{"topic": t} for t in (landmines or [])],
        },
    }


def test_no_previous_report_means_no_notes():
    assert diff_playbook(None, _report()) == []
    assert diff_playbook({}, _report()) == []


def test_stage_change_is_explained_with_direction():
    rep = _report()
    rep.stage = Stage(value=StageValue.mutually_confirmed, change=None)
    notes = diff_playbook(_body(stage="initial_contact"), rep)
    assert notes and "关系阶段" in notes[0]
    assert "陌生" in notes[0] and "确定关系" in notes[0]


def test_boundary_change_is_explained_and_prioritised_in_wording():
    rep = _report()
    rep.boundary = Boundary(value=BoundaryValue.stop_contact)
    notes = diff_playbook(_body(boundary="none"), rep)
    assert any("边界状态" in n and "优先" in n for n in notes)


def test_portrait_revision_is_explained_with_its_reason():
    rep = _report()
    rep.persona = Persona(available=True, version=3, change_notes=[PersonaChangeModel(field="表达直接性", kind="changed", before="偏直接", after="偏含蓄", reason="依据 m000003")])
    notes = diff_playbook(_body(version=2), rep)
    assert any("画像更新到 v3" in n and "m000003" in n for n in notes)


def test_changed_main_action_and_new_landmine_are_explained():
    rep = _report()
    rep.playbook = Playbook(actions=[Action(goal="提出一次低压力邀约", action="a", stop_when="s")], landmines=[Landmine(topic="深夜消息", why="w")])
    notes = diff_playbook(_body(goal="读懂互动", landmines=[]), rep)
    assert any("主行动" in n and "提出一次低压力邀约" in n for n in notes)
    assert any("新增雷区" in n and "深夜消息" in n for n in notes)


def test_unchanged_advice_produces_no_notes():
    rep = _report()
    rep.stage = Stage(value=StageValue.getting_to_know)
    rep.boundary = Boundary(value=BoundaryValue.none)
    rep.persona = Persona(available=False, version=0)
    rep.playbook = Playbook(actions=[Action(goal="读懂互动", action="a", stop_when="s")])
    assert diff_playbook(_body(goal="读懂互动"), rep) == []


def test_notes_are_capped():
    rep = _report()
    rep.stage = Stage(value=StageValue.stable)
    rep.boundary = Boundary(value=BoundaryValue.explicit_decline)
    rep.persona = Persona(available=True, version=9)
    rep.playbook = Playbook(actions=[Action(goal="全新行动", action="a", stop_when="s")], landmines=[Landmine(topic="新雷区", why="w")])
    notes = diff_playbook(_body(stage="initial_contact", boundary="none", version=1, goal="旧行动"), rep)
    assert len(notes) == 5


def test_second_analysis_records_why_the_advice_changed(app_client):
    c = app_client
    imp = c.post("/api/imports", json={"text": CHAT}).json()
    c.post(f"/api/imports/{imp['id']}/confirm", json={})

    first = c.post("/api/analyses", json={"import_id": imp["id"], "mode": "rules", "profile": {"goal": "understand"}}).json()
    c.app_state.runner.run_pending_once()
    rep1 = c.get(f"/api/reports/{c.get(f'/api/analyses/{first['id']}').json()['report_id']}").json()
    assert rep1["playbook"]["change_notes"] == [], "the first report has nothing to compare against"

    second = c.post("/api/analyses", json={"import_id": imp["id"], "mode": "rules", "profile": {"goal": "invite"}}).json()
    c.app_state.runner.run_pending_once()
    rid2 = c.get(f"/api/analyses/{second['id']}").json()["report_id"]
    rep2 = c.get(f"/api/reports/{rid2}").json()
    assert rep2["playbook"]["changed_from_report"] == rep1["id"]
    assert rep2["playbook"]["change_notes"], "a changed goal must be explained"
    md = c.get(f"/api/reports/{rid2}/export?format=md").text
    assert "与上次相比的变化" in md


@pytest.mark.parametrize("value", ["advance", "regress"])
def test_direction_wording_follows_the_recorded_transition(value):
    from rapport.schemas.report_v1 import StageChangeModel

    rep = _report()
    rep.stage = Stage(value=StageValue.getting_to_know, change=StageChangeModel(before="x", after="y", direction=value, reason=""))
    notes = diff_playbook(_body(stage="initial_contact"), rep)
    assert ("推进" in notes[0]) if value == "advance" else ("回退" in notes[0])
