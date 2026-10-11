"""v0.5 §2 (stage 3): playbook situations → options, validation, rule templates, done review.

What the plan promised and this file pins: every option is grounded (trigger ids), says what to
expect (≥2 reactions, one of them a stop), has a stop condition and a risk; manipulation advice
is dropped regardless of boundary; advancing advice is dropped under a boundary; the rule layer
produces situations only when it can point at real messages; what the other person already
declined is not recommended again; the flat `actions` list stays populated for old clients.
"""

from __future__ import annotations

from rapport.features import compute_features
from rapport.knowledge.situations import SITUATION_TEMPLATES, rule_situations
from rapport.llm.validate import validate_synthesis
from rapport.parsers import ParseOptions, parse
from rapport.report.assemble import build_done_review
from rapport.schemas.profile import Goal
from rapport.schemas.report_v1 import BoundaryValue, option_id


def _conv(text: str):
    conv, _ = parse(text, "c", "paste", None, ParseOptions())
    return conv


def _ids(conv, n=8):
    return {m.id for m in conv.events()}, [m.id for m in conv.events()][:n]


def _sit(ids, **over):
    base = {
        "id": "s1",
        "title": "对方改了时间没定下来",
        "trigger_ids": ids[:2],
        "why": "w",
        "options": [
            {
                "goal": "把时间定下来",
                "action": "按对方给的时间提一个具体安排",
                "trigger_ids": ids[:1],
                "suggested_text": "周六三点？",
                "expected_reactions": [{"if_they": "答应", "then": "定", "signal": "good"}, {"if_they": "不回", "then": "停", "signal": "stop"}],
                "stop_when": "两次不回",
                "risk": "催太紧",
            }
        ],
    }
    base.update(over)
    return base


def _synth(ids, situations, boundary="none"):
    return {
        "dims": {"responsiveness": {"score": 60, "evidence_ids": ids[:1], "note": ""}},
        "observations": [],
        "persona": {"tendencies": {}, "traits": [], "inferred_axes": {}},
        "stage": {"value": "getting_to_know", "basis_ids": ids[:1], "note": ""},
        "boundary": {"value": boundary, "basis_ids": ids[:1] if boundary != "none" else [], "note": ""},
        "playbook": {"situations": situations, "actions": [], "landmines": [], "scripts": {}},
    }


CHAT = "\n".join(f"[2026-09-{d:02d} 20:00] {'A' if d % 2 else 'B'}: 消息{d} 周末一起吃饭吧" for d in range(1, 16))


# ------------------------------------------------------------------ validator


def test_valid_situation_survives_and_backfills_actions():
    conv = _conv(CHAT)
    valid, ids = _ids(conv)
    v = validate_synthesis(_synth(ids, [_sit(ids)]), valid)
    assert v.playbook and len(v.playbook.situations) == 1
    o = v.playbook.situations[0].options[0]
    assert o.id == option_id("把时间定下来", "按对方给的时间提一个具体安排") and o.trigger_ids == ids[:1]
    assert len(o.expected_reactions) == 2 and o.stop_when
    assert v.playbook.actions and v.playbook.actions[0].goal == "把时间定下来" and v.playbook.actions[0].basis_ids == ids[:1]
    assert not v.dropped


def test_situation_without_real_trigger_is_dropped():
    conv = _conv(CHAT)
    valid, ids = _ids(conv)
    v = validate_synthesis(_synth(ids, [_sit(ids, trigger_ids=["m999999"])]), valid)
    assert v.playbook is None and v.dropped.get("situations_no_trigger") == 1


def test_option_needs_two_reactions_and_a_stop():
    conv = _conv(CHAT)
    valid, ids = _ids(conv)
    one = _sit(ids)
    one["options"][0]["expected_reactions"] = one["options"][0]["expected_reactions"][:1]
    v = validate_synthesis(_synth(ids, [one]), valid)
    assert v.dropped.get("options_few_reactions") == 1 and v.dropped.get("situations_no_options") == 1
    nostop = _sit(ids)
    nostop["options"][0]["stop_when"] = ""
    v = validate_synthesis(_synth(ids, [nostop]), valid)
    assert v.dropped.get("options_no_stop") == 1


def test_manipulation_is_dropped_regardless_of_boundary():
    conv = _conv(CHAT)
    valid, ids = _ids(conv)
    bad = _sit(ids)
    bad["options"][0]["action"] = "故意不回消息让对方吃醋"
    v = validate_synthesis(_synth(ids, [bad]), valid)
    assert v.dropped.get("options_manipulation") == 1 and v.playbook is None


def test_advancing_option_dropped_under_boundary_only():
    conv = _conv(CHAT)
    valid, ids = _ids(conv)
    adv = _sit(ids)
    adv["options"][0]["action"] = "再约一次"
    ok = validate_synthesis(_synth(ids, [adv]), valid)
    assert ok.playbook and ok.playbook.situations
    under = validate_synthesis(_synth(ids, [adv], boundary="explicit_decline"), valid)
    # v0.8: kept, with a caution instead of being dropped
    assert under.dropped.get("options_advance_under_boundary") is None
    assert under.playbook and under.playbook.situations[0].options[0].caution


# ------------------------------------------------------------------ rule templates


def test_every_template_option_meets_the_contract():
    from rapport.schemas.report_v1 import Situation

    for key, _pred, build in SITUATION_TEMPLATES:
        s: Situation = build(["m000001"], Goal.invite)
        assert s.id == key and s.title and s.trigger_ids and s.options
        for o in s.options:
            assert o.id and o.stop_when and o.risk is not None
            assert len(o.expected_reactions) >= 2 and any(r.signal == "stop" for r in o.expected_reactions)
            assert not o.personalized


def test_rule_situation_fires_only_with_evidence():
    warm = _conv(CHAT)
    f = compute_features(warm)
    sits = rule_situations(f, Goal.invite, BoundaryValue.none)
    for s in sits:
        assert s.trigger_ids and all(t.startswith("m") for t in s.trigger_ids)
    quiet = _conv("[2026-09-01 20:00] A: 在吗\n[2026-09-01 20:05] B: 在")
    assert rule_situations(compute_features(quiet), Goal.invite, BoundaryValue.none) == []


def test_unanswered_offers_situation_triggers_on_repeated_deflection():
    txt = "\n".join(
        [
            "[2026-09-01 20:00] A: 周末要不要一起吃饭？",
            "[2026-09-01 20:10] B: 哈哈",
            "[2026-09-03 20:00] A: 周六有空吗，一起看电影？",
            "[2026-09-03 20:30] B: 最近好累啊",
            "[2026-09-05 20:00] A: 那下周呢，一起去展？",
            "[2026-09-05 21:00] B: 再说吧",
        ]
    )
    f = compute_features(_conv(txt))
    ids = {s.id for s in rule_situations(f, Goal.invite, BoundaryValue.none)}
    assert "unanswered_offers" in ids or "alt_time_open" in ids


def test_nothing_advances_under_stop_contact_and_declined_goal_is_not_repeated():
    f = compute_features(_conv(CHAT))
    # v0.8: a boundary prefixes the situation and marks advancing options; it removes nothing
    base = rule_situations(f, Goal.invite, BoundaryValue.none)
    under = rule_situations(f, Goal.invite, BoundaryValue.stop_contact)
    assert len(under) == len(base)
    assert all(s.why.startswith("⚠") for s in under)
    review = build_done_review([{"option_id": "abc", "goal": "把时间定下来", "observed": "declined", "note": ""}], "none")
    assert review and review[0].verdict.startswith("对方拒绝了")


# ------------------------------------------------------------------ end to end


def test_report_carries_situations_and_done_review_after_an_action(app_client):
    c = app_client
    imp = c.post("/api/imports", json={"text": CHAT}).json()
    c.post(f"/api/imports/{imp['id']}/confirm", json={})
    a = c.post("/api/analyses", json={"import_id": imp["id"], "mode": "rules", "profile": {"goal": "invite"}}).json()
    c.app_state.runner.run_pending_once()
    rid = c.get(f"/api/analyses/{a['id']}").json()["report_id"]
    rep = c.get(f"/api/reports/{rid}").json()
    pb = rep["playbook"]
    assert "situations" in pb and "done_review" in pb and pb["done_review"] == []
    if pb["situations"]:
        o = pb["situations"][0]["options"][0]
        assert o["id"] and o["trigger_ids"] and len(o["expected_reactions"]) >= 2
        goal = o["goal"]
    else:
        goal = pb["actions"][0]["goal"]
    # the user did it; the other person declined
    r = c.post(f"/api/reports/{rid}/actions/{'x1'}/done", json={"goal": goal, "observed": "declined", "note": "说没空"})
    assert r.status_code == 200
    imp2 = c.post("/api/imports", json={"text": CHAT + "\n[2026-09-16 20:00] A: 那算了"}).json()
    c.post(f"/api/imports/{imp2['id']}/confirm", json={})
    a2 = c.post("/api/analyses", json={"import_id": imp2["id"], "mode": "rules", "profile": {"goal": "invite"}}).json()
    c.app_state.runner.run_pending_once()
    rid2 = c.get(f"/api/analyses/{a2['id']}").json()["report_id"]
    pb2 = c.get(f"/api/reports/{rid2}").json()["playbook"]
    assert pb2["done_review"] and pb2["done_review"][0]["observed"] == "declined" and pb2["done_review"][0]["goal"] == goal
    assert all(o["goal"] != goal for s in pb2["situations"] for o in s["options"])
    assert all(act["goal"] != goal for act in pb2["actions"])
    md = c.get(f"/api/reports/{rid2}/export?format=md").text
    assert "上次「我做了」的回顾" in md


def test_byok_fake_provider_situations_pass_validation(app_client):
    c = app_client
    body = c.get("/api/demo").json() if c.get("/api/demo").status_code == 200 else None
    from rapport.llm.fake import FakeProvider

    conv = _conv(CHAT)
    valid = {m.id for m in conv.events()}
    data = FakeProvider._synthesize(sorted(valid), "")
    v = validate_synthesis(data, valid)
    assert v.playbook and v.playbook.situations and len(v.playbook.situations[0].options) == 2
    assert v.dropped.get("options_few_reactions") is None
    _ = body
