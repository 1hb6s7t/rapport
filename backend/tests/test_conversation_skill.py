import json

import pytest

from rapport.agent import ConversationAgent
from rapport.agent.skill import MARKER, augment, sources
from rapport.llm.personal_request import compile_reply
from rapport.llm.reply import ReplyInput


def test_full_pinned_sources_are_loaded_without_compression():
    system, names = augment("TASK: reply\n", "repair")
    assert len(names) == 3
    for name in names:
        assert sources()[name] in system
    assert MARKER in system


def test_opponent_uses_own_profile_and_swapped_speaker_mapping():
    agent = ConversationAgent(skill=False)
    req = agent.prepare({"A": "A-only fact", "B": "B-only fact"}, [{"party": "A", "text": "hello"}], party="B")
    data = json.loads(req.user)
    assert data["档案"]["本人"] == "B-only fact"
    assert data["当前对话"] == [{"说话方": "B", "正文": "hello"}]
    assert "A-only fact" not in req.user


def test_agent_does_not_respond_to_its_own_last_message():
    with pytest.raises(ValueError):
        ConversationAgent().prepare({"A": "", "B": ""}, [{"party": "A", "text": "hello"}], party="A")


def test_skill_changes_inspectable_prompt_and_hash():
    inp = ReplyInput(lines=["B: 最近怎么样？"], styles=["brief"], memories=[], goal="自然回复")
    bare = compile_reply(inp, "selected-local")
    inp.skill_mode = "goutoujunshi"
    equipped = compile_reply(inp, "selected-local")
    assert MARKER not in bare.system and MARKER in equipped.system
    assert equipped.user == bare.user
    assert equipped.max_tokens == bare.max_tokens == 400


def test_changed_skill_invalidates_preview(app_client, tmp_path):
    from tests.test_selected_local import choose, context
    choose(app_client.app_state, tmp_path)
    body = context(app_client)
    preview = app_client.post("/api/replies/preview", json={**body, "skill": "goutoujunshi"}).json()
    assert any(MARKER in line for line in preview["lines"])
    result = app_client.post("/api/replies", json={**body, "skill": "off", "ticket_id": preview["ticket_id"],
                             "payload_hash": preview["payload_hash"], "accept_cost": True})
    assert result.status_code == 409
    assert not app_client.app_state.provider_override.calls


def test_skill_confirm_uses_same_full_prompt(app_client, tmp_path):
    from tests.test_selected_local import choose, context
    choose(app_client.app_state, tmp_path)
    body = {**context(app_client), "skill": "goutoujunshi"}
    preview = app_client.post("/api/replies/preview", json=body).json()
    result = app_client.post("/api/replies", json={**body, "ticket_id": preview["ticket_id"],
                             "payload_hash": preview["payload_hash"], "accept_cost": True})
    assert result.status_code == 200
    assert MARKER in app_client.app_state.provider_override.calls[-1][0]
