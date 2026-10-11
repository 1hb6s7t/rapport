from types import SimpleNamespace

import pytest

from rapport.analysis_context import build_context, rank_candidates
from rapport.features.episodes import build_episodes, episode_for
from rapport.parsers import parse
from rapport.schemas.reply import ReplyDraft


def material(client, relationship_id="default", confirm=True):
    value = client.post("/api/imports", json={"text": "我: 今天有空吗？\n对方: 明天可以。\n我: 好的", "relationship_id": relationship_id}).json()
    if confirm:
        client.post(f"/api/imports/{value['id']}/confirm", json={})
    return value


def test_reply_rejects_unconfirmed_and_cross_relationship_material(app_client):
    c = app_client
    raw = material(c, confirm=False)
    assert c.post("/api/replies/preview", json={"import_id": raw["id"]}).status_code == 409
    c.post(f"/api/imports/{raw['id']}/confirm", json={})
    other = c.post("/api/relationships", json={"name": "第二个对象"}).json()
    assert c.post("/api/replies/preview", json={"import_id": raw["id"], "relationship_id": other["id"]}).status_code == 403
    assert not c.app_state.provider_override.calls


def test_goal_and_style_changes_invalidate_reply_preview(app_client):
    c = app_client
    imp = material(c)
    ticket = c.post("/api/replies/preview", json={"import_id": imp["id"], "goal": "确认时间"}).json()
    r = c.post("/api/replies", json={"import_id": imp["id"], "goal": "另一个目标", "ticket_id": ticket["ticket_id"], "payload_hash": ticket["payload_hash"], "accept_cost": True})
    assert r.status_code == 409 and r.json()["error"]["code"] == "hash_mismatch"
    assert not c.app_state.provider_override.calls


def test_context_filters_hindsight_and_future_evidence():
    conv, _ = parse("我: 你怎么了？\n对方: 没事。\n我: 那我先忙", "example", "auto")
    target = conv.events()[1]
    episode = episode_for(conv, build_episodes(conv, conv.events()), target, mode="reply")
    report = {"id": "r", "intent_readings": [
        {"evidence_ids": [target.id], "post_hoc": True, "context_mode": "review", "emotions": ["anger"]},
        {"evidence_ids": [conv.events()[2].id], "post_hoc": False, "context_mode": "reply", "emotions": ["sad"]},
        {"evidence_ids": [target.id], "post_hoc": False, "context_mode": "reply", "emotions": ["uncertain"]},
    ]}
    imp = SimpleNamespace(id="i", content_hash="h", range_start=0, range_end=2)
    state = SimpleNamespace()
    ctx = build_context(state, "default", imp, conv, episode, report=report)
    assert ctx["excluded_post_hoc_readings"] == 2
    assert len(ctx["readings"]) == 1 and ctx["readings"][0]["emotions"] == ["uncertain"]
    assert ctx["auxiliary_policy"]["affects_objective_score"] is False


def test_auxiliary_ranking_is_capped_and_missing_auxiliary_does_not_fabricate():
    first = ReplyDraft(style="casual", text="先尊重你的安排", ranking={"evidence_fit": .8, "auxiliary_fit": .1})
    second = ReplyDraft(style="warm", text="辅助资料偏好", ranking={"evidence_fit": .3, "auxiliary_fit": 1})
    ranked = rank_candidates([second, first], True)
    assert ranked[0] is first
    assert ranked[0].ranking["score"] == pytest.approx(.85 * .8 + .15 * .1)
    assert rank_candidates([first], False)[0].ranking["auxiliary_weight"] == 0


def test_birthday_revision_invalidates_outbound_preview(app_client):
    c = app_client
    imp = material(c)
    ticket = c.post("/api/replies/preview", json={"import_id": imp["id"]}).json()
    assert c.put("/api/relationships/default/profile", json={"birthday": {"year": 2000}, "confirmed": True}).status_code == 200
    response = c.post("/api/replies", json={"import_id": imp["id"], "ticket_id": ticket["ticket_id"], "payload_hash": ticket["payload_hash"], "accept_cost": True})
    assert response.status_code == 409 and response.json()["error"]["code"] == "hash_mismatch"
    assert not c.app_state.provider_override.calls


def test_task_route_matches_preview_and_custom_model_wins(app_client):
    state = app_client.app_state
    state.save_provider_config("ollama-split", None, None, None, None)
    assert state.task_provider_config("extract")["model"] == "qwen3:4b"
    assert state.task_provider_config("reply")["model"] == "qwen3:8b"
    state.save_provider_config("ollama-split", "custom-local", None, None, None)
    assert state.task_provider_config("extract")["model"] == "custom-local"
