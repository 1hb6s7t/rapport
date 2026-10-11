"""Exact private-reply compilation and public API confirmation boundaries."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime

import pytest

from rapport.learning.reply_batch import validate_request
from rapport.llm.outbound import payload_hash
from rapport.llm.personal_request import (
    BATCH_SIZE,
    FACT_POLICY,
    MAX_INPUT_TOKENS,
    MAX_NEW_TOKENS,
    SYSTEM,
    compile_reply,
    compile_request,
    training_messages,
)
from rapport.llm.reply import ReplyInput, first_person_claims, validate_replies
from rapport.schemas.diary import DiaryEntry


def reply_input(**changes):
    values = dict(lines=["m01 A: 今天有空吗", "m02 B: 晚上有点累"], styles=["casual", "sincere", "direct"],
                  memories=[], goal="自然回应", analysis_context={"id": "context-1", "profile_facts": {}})
    return ReplyInput(**(values | changes))


def request_hash(inp):
    compiled = compile_reply(inp, "personal")
    return payload_hash(compiled.preview_lines(), {"preset": "personal", "model": "candidate-1", "base_url": "local://personal"}, "n/a")


def test_compilation_contains_actual_prompts_in_requested_order_and_no_invented_profile():
    inp = reply_input(styles=["direct", "casual", "casual", "unknown"])
    compiled = compile_reply(inp, "personal")
    packet = json.loads(compiled.user)
    assert compiled.system == SYSTEM and compiled.max_tokens == MAX_NEW_TOKENS
    assert compiled.preview_lines() == [compiled.system, compiled.user]
    assert [item["style"] for item in packet["prompts"]] == ["direct", "casual"]
    assert packet["max_new_tokens"] == MAX_NEW_TOKENS and packet["max_input_tokens"] == MAX_INPUT_TOKENS and packet["batch_size"] == BATCH_SIZE
    for item in packet["prompts"]:
        messages = item["messages"]
        assert [m["role"] for m in messages] == ["system", "user"]
        assert "你代写我的微信回复" in messages[0]["content"]
        data = json.loads(messages[1]["content"])
        assert data["已确认资料与可见证据"] == {}
        # Message ids and times are dropped: the model was trained and evaluated on "A: ..." lines.
        assert data["对话"] == [line.split(" ", 1)[1] for line in inp.lines]
    assert validate_request(packet) == packet["prompts"]


def test_compiler_limits_visible_lines_and_uses_only_authorized_memory_clause():
    memory = DiaryEntry(id="memory-1", revision=7, text="未批准长版本", approved_clause="周末想休息", recorded_at=datetime.now(UTC))
    packet = compile_request(reply_input(lines=[f"B: {i}" for i in range(20)], memories=[memory]))
    visible = json.loads(packet["prompts"][0]["messages"][1]["content"])
    assert visible["对话"] == [f"B: {i}" for i in range(8, 20)]
    assert visible["允许使用的本人记忆"] == [{"id": "memory-1", "revision": 7, "text": "周末想休息"}]
    assert "未批准长版本" not in json.dumps(packet, ensure_ascii=False)
    assert packet["diary_refs"] == [{"id": "memory-1", "revision": 7}]


def test_personal_training_uses_the_production_prompt_for_each_style():
    row = {"id": "s:3", "context": [{"sender": "other", "text": "今晚有空吗"},
                                    {"sender": "me", "text": "要看安排"},
                                    {"sender": "other", "text": "那明天呢"}]}
    for style in ("casual", "sincere", "direct"):
        expected = compile_request(ReplyInput(
            lines=["B: 今晚有空吗", "A: 要看安排", "B: 那明天呢"],
            styles=[style], memories=[], goal="自然地回应对方",
            expression_hint="我平时一条消息大约 4 个字"))["prompts"][0]["messages"]
        assert training_messages(row, style) == expected


def test_training_style_is_stable_per_sample_and_covers_all_production_styles():
    from rapport.llm.personal_request import TRAINING_STYLES, training_style

    row = {"id": "s:3", "context": [{"sender": "other", "text": "在吗"}]}
    assert training_style(row) == training_style(dict(row)) in TRAINING_STYLES
    assert training_messages(row) == training_messages(row, training_style(row))
    assert {training_style({"id": f"s:{i}"}) for i in range(60)} == set(TRAINING_STYLES)


def test_reply_evaluation_compiles_the_training_prompt_for_every_style():
    from rapport.learning.reply_evaluation import evaluation_input

    row = {"id": "s:3", "context": [{"sender": "other", "text": "今晚有空吗"},
                                    {"sender": "me", "text": "要看安排"},
                                    {"sender": "other", "text": "那明天呢"}]}
    prompts = compile_request(evaluation_input(row))["prompts"]
    assert [p["style"] for p in prompts] == ["casual", "sincere", "direct"]
    assert all(p["messages"] == training_messages(row, p["style"]) for p in prompts)


def test_personal_drafts_become_one_bubble_per_line():
    from datetime import datetime

    from rapport.llm.reply import validate_replies

    inp = ReplyInput(lines=["B: 在吗"], styles=["casual"], memories=[], goal="回应")
    data = {"drafts": [{"style": "casual", "text": "在的\n怎么啦\n\n慢慢说"}]}
    split = validate_replies(data, inp, {}, "r", datetime.now().astimezone(), strict_facts=True, split_lines=True)
    assert [item.text for item in split.drafts[0].items] == ["在的", "怎么啦", "慢慢说"]
    whole = validate_replies(data, inp, {}, "r", datetime.now().astimezone())
    assert len(whole.drafts[0].items) == 1


def test_reply_evaluation_requires_distinct_alternatives():
    from types import SimpleNamespace

    from rapport.learning.reply_evaluation import distinct_reply_count

    drafts = [SimpleNamespace(text="先看看酒店吧"), SimpleNamespace(text="先看 看酒店吧"),
              SimpleNamespace(text="那我们再找一家吧")]
    assert distinct_reply_count(drafts) == 2


def test_local_and_cloud_reply_prompts_hide_synthetic_credentials():
    token = "sk-" + "D" * 35
    inp = reply_input(lines=["B: 这个不能发 " + token], goal="自然回应")
    assert token not in inp.user_prompt()
    packet = compile_request(inp)
    assert token not in json.dumps(packet, ensure_ascii=False)
    assert "[已移除凭证]" in packet["prompts"][0]["messages"][1]["content"]


@pytest.mark.parametrize("draft", ["我刚上完课", "我之前在网上买了两台", "我给你发个链接", "我现在在外地工作"])
def test_unbacked_personal_actions_are_detected(draft):
    assert first_person_claims(draft)


def test_opinion_does_not_become_a_personal_experience_claim():
    assert first_person_claims("我觉得这个方案还要讨论") == []


def test_signature_changes_with_prompt_order_facts_revision_context_and_generation_budget():
    original = reply_input(analysis_context={"id": "context-1", "profile_facts": {"other": {"mbti": "INFP", "revision": 1}}})
    base = request_hash(original)
    assert request_hash(reply_input(styles=["direct", "sincere", "casual"], analysis_context=original.analysis_context)) != base
    changed = copy.deepcopy(original)
    changed.analysis_context["profile_facts"]["other"]["revision"] = 2
    assert request_hash(changed) != base
    changed = copy.deepcopy(original)
    changed.analysis_context["id"] = "context-2"
    assert request_hash(changed) != base
    compiled = compile_reply(original, "personal")
    packet = json.loads(compiled.user)
    packet["max_new_tokens"] += 1
    altered = payload_hash([compiled.system, json.dumps(packet, ensure_ascii=False, sort_keys=True)], {"preset": "personal", "model": "candidate-1", "base_url": "local://personal"}, "n/a")
    assert altered != base
    with pytest.raises(ValueError, match="配置不匹配"):
        validate_request(packet)


@pytest.mark.parametrize("style", ["casual", "轻松", " casual ", " 轻松 "])
def test_model_chinese_and_english_style_labels_share_same_validation(style):
    pack = validate_replies({"drafts": [{"style": style, "text": "那你好好休息呀"}]}, reply_input(styles=["casual"]), {}, "friend", datetime.now(UTC))
    assert [(draft.style, draft.text) for draft in pack.drafts] == [("casual", "那你好好休息呀")]


def test_unknown_duplicate_and_unrequested_model_styles_are_filtered():
    data = {"drafts": [None, {"style": "invalid", "text": "未知"}, {"style": "direct", "text": "未请求"},
                       {"style": "轻松", "text": "先休息吧"}, {"style": "casual", "text": "重复"},
                       {"style": "真诚", "text": "今晚放松一下吧"}]}
    pack = validate_replies(data, reply_input(styles=["casual", "sincere"]), {}, "friend", datetime.now(UTC))
    assert [draft.style for draft in pack.drafts] == ["casual", "sincere"]
    assert "重复" not in [draft.text for draft in pack.drafts]


@pytest.mark.parametrize("change", ["duplicate", "unknown", "wrong_role", "bad_protocol"])
def test_runtime_rejects_mutated_request_contract(change):
    packet = compile_request(reply_input())
    if change == "duplicate":
        packet["prompts"].append(packet["prompts"][0])
    elif change == "unknown":
        packet["prompts"][0]["style"] = "unknown"
    elif change == "wrong_role":
        packet["prompts"][0]["messages"][0]["role"] = "assistant"
    else:
        packet["protocol"] = "obsolete"
    with pytest.raises(ValueError):
        validate_request(packet)


def test_api_preview_contains_compiled_prompts_and_profile_change_invalidates_ticket(app_client, monkeypatch):
    c = app_client
    st = c.app_state
    cfg = st.task_provider_config("reply") | {"preset": "personal", "model": "candidate-1", "base_url": "local://personal", "has_key": True}
    monkeypatch.setattr(st, "task_provider_config", lambda task: cfg)
    imported = c.post("/api/imports", json={"text": "[2026-09-01 20:00] A: 今天怎么样\n[2026-09-01 20:01] B: 有点累\n"}).json()
    assert c.post(f"/api/imports/{imported['id']}/confirm", json={}).status_code == 200
    body = {"import_id": imported["id"], "relationship_id": "default", "styles": ["casual", "sincere"]}
    preview = c.post("/api/replies/preview", json=body)
    assert preview.status_code == 200, preview.text
    ticket = preview.json()
    packet = json.loads(ticket["lines"][1])
    assert ticket["lines"][0] == SYSTEM
    assert [p["style"] for p in packet["prompts"]] == ["casual", "sincere"]
    assert c.put("/api/relationships/default/profile", json={"mbti": "INTJ", "confirmed": True, "source": "user"}).status_code == 200
    called = []
    monkeypatch.setattr(st, "make_provider", lambda *a, **kw: called.append(True))
    response = c.post("/api/replies", json=body | {"ticket_id": ticket["ticket_id"], "payload_hash": ticket["payload_hash"]})
    assert response.status_code == 409 and response.json()["error"]["code"] == "hash_mismatch"
    assert not called


def test_review_api_requires_current_evaluation_hash_and_regular_auth(app_client):
    c = app_client
    service = c.app_state.learning
    record = {"id": "candidate-review", "created_at": 1, "task": "reply", "state": "evaluated", "metrics": {},
              "artifact_hash": "abc", "source_ids": [], "gate": {"passed": False, "reasons": ["personal_generation_review_required"]}}
    service.register(record)
    folder = service.root / "models" / record["id"]
    folder.mkdir()
    samples = [{"id": f"test-{index}", "candidate_reply": "合成测试回复"} for index in range(10)]
    (folder / "evaluation.bin").write_bytes(service.crypto.encrypt(json.dumps(samples).encode()))
    endpoint = f"/api/learning/models/{record['id']}"
    response = c.get(endpoint + "/samples")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    decisions = {sample["id"]: True for sample in samples}
    assert c.post(endpoint + "/review", json={"decisions": decisions}).status_code == 422
    assert c.post(endpoint + "/review", json={"decisions": decisions, "evaluation_hash": "0" * 64}).status_code == 409
    valid = {"decisions": decisions, "evaluation_hash": response.json()["evaluation_hash"]}
    assert c.post(endpoint + "/review", json=valid, headers={"x-rapport-csrf": "wrong"}).status_code == 403
    accepted = c.post(endpoint + "/review", json=valid)
    assert accepted.status_code == 200 and accepted.json()["gate"]["passed"]


def test_personal_style_training_is_not_authorization_for_first_person_experiences():
    inp = reply_input(analysis_context={"id": "context-1", "profile_facts": {"me": {"confirmed": True, "mbti": "INFP"}}})
    data = {"drafts": [{"style": "casual", "text": "我昨天去海边了，特别放松。", "used_memory_ids": ["invented-memory"]}]}
    pack = validate_replies(data, inp, {}, "friend", datetime.now(UTC), strict_facts=True)
    assert not pack.drafts
    assert [item["style"] for item in pack.withheld_drafts] == ["casual"]
    assert "海边" not in json.dumps(pack.model_dump(), ensure_ascii=False)
    assert pack.abstain_reason


def test_authorized_current_diary_fact_can_be_used_with_exact_revision_reference():
    memory = DiaryEntry(id="allowed", revision=3, text="我昨天去海边了，特别放松。", recorded_at=datetime.now(UTC),
                        allowed_relationships=frozenset({"friend"}), approved_clause="我昨天去海边了，特别放松。")
    inp = reply_input(memories=[memory])
    pack = validate_replies({"drafts": [{"style": "casual", "text": memory.approved_clause, "used_memory_ids": [memory.id]}]},
                            inp, {memory.id: memory}, "friend", datetime.now(UTC), strict_facts=True)
    assert len(pack.drafts) == 1 and not pack.withheld_drafts
    assert not pack.drafts[0].needs_review
    assert [(ref.id, ref.revision) for ref in pack.drafts[0].memory_refs] == [("allowed", 3)]


def test_safe_candidates_remain_available_without_reporting_raw_model_as_error_free():
    data = {"drafts": [{"style": "casual", "text": "我上周买了那本书。"},
                       {"style": "sincere", "text": "听起来挺辛苦的，今晚好好休息呀。"},
                       {"style": "direct", "text": "那今晚先休息，明天再聊吧。"}]}
    pack = validate_replies(data, reply_input(), {}, "friend", datetime.now(UTC), strict_facts=True)
    assert [draft.style for draft in pack.drafts] == ["sincere", "direct"]
    assert len(pack.withheld_drafts) == 1 and pack.withheld_drafts[0]["style"] == "casual"
    assert all(item.text != data["drafts"][0]["text"] for draft in pack.drafts for item in draft.items)
    assert "模型学到的表达不能当作本次事实" in pack.abstain_reason
    # Filtering is visible accounting, not evidence that the raw candidate made no error.
    assert len(pack.drafts) + len(pack.withheld_drafts) == len(data["drafts"])
    assert data["drafts"][0]["text"] == "我上周买了那本书。"


def test_fact_policy_is_signed_and_runtime_rejects_old_policy():
    inp = reply_input()
    compiled = compile_reply(inp, "personal")
    packet = json.loads(compiled.user)
    assert packet["fact_policy"] == FACT_POLICY
    packet["fact_policy"] = "previous-allow-all-policy"
    changed_hash = payload_hash([compiled.system, json.dumps(packet, ensure_ascii=False, sort_keys=True)],
                                {"preset": "personal", "model": "candidate-1", "base_url": "local://personal"}, "n/a")
    assert changed_hash != request_hash(inp)
    with pytest.raises(ValueError):
        validate_request(packet)


def test_personal_api_withholds_unsupported_facts_but_returns_safe_drafts(app_client, monkeypatch):
    from rapport.llm.provider import CallUsage, ProviderInfo

    c = app_client
    st = c.app_state
    cfg = st.task_provider_config("reply") | {"preset": "personal", "model": "candidate-1", "base_url": "local://personal", "has_key": True}
    monkeypatch.setattr(st, "task_provider_config", lambda task: cfg)
    imported = c.post("/api/imports", json={"text": "[2026-09-01 20:00] A: 今天怎么样\n[2026-09-01 20:01] B: 有点累\n"}).json()
    assert c.post(f"/api/imports/{imported['id']}/confirm", json={}).status_code == 200
    body = {"import_id": imported["id"], "relationship_id": "default", "styles": ["casual", "sincere"]}
    preview = c.post("/api/replies/preview", json=body).json()

    class FakePersonalProvider:
        info = ProviderInfo("personal", "local://personal", "candidate-1", "本机", "本机", "n/a")
        reasoning = False

        def complete_json(self, system, user, **kwargs):
            assert json.loads(user)["fact_policy"] == FACT_POLICY
            return {"drafts": [{"style": "casual", "text": "我昨天去了海边，挺放松的。"},
                               {"style": "sincere", "text": "听起来挺累的，好好休息呀。"}]}, CallUsage(10, 10, 1)

    monkeypatch.setattr(st, "make_provider", lambda *args, **kwargs: FakePersonalProvider())
    response = c.post("/api/replies", json=body | {"ticket_id": preview["ticket_id"], "payload_hash": preview["payload_hash"]})
    assert response.status_code == 200, response.text
    assert [draft["style"] for draft in response.json()["drafts"]] == ["sincere"]
    assert [item["style"] for item in response.json()["withheld_drafts"]] == ["casual"]
    assert "海边" not in response.text
