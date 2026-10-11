"""Dataset v6 reply targets, generation helpers and load shortcuts; synthetic data only."""

from __future__ import annotations

import json

from rapport.datasets.corpus import build_records, load_corpus, reply_split
from rapport.datasets.credentials import redact_text
from rapport.learning import lora
from rapport.learning.reply_batch import banned_ngram_tokens, clean_short_text, normalized


def msg(text, mine, seconds, kind="文本消息"):
    return {"type": kind, "content": text, "isSend": int(mine), "createTime": 1700000000 + seconds}


def corpus(tmp_path, exports):
    root = tmp_path / "eval"
    (root / "redacted").mkdir(parents=True)
    (root / "annotations").mkdir()
    for name, messages, meta in exports:
        body = {"session": meta, "messages": messages}
        (root / "redacted" / f"{name}.json").write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    return load_corpus(root)


def replies(tmp_path, messages, extra=()):
    pools, protocol = build_records(corpus(tmp_path, [("friend", messages, {}), *extra]))
    return pools["personal_reply"], protocol["personal_reply_protocol"]


def test_follow_up_bubbles_within_three_minutes_join_one_reply_target(tmp_path):
    rows, protocol = replies(tmp_path, [
        msg("明天去爬山吗", False, 0), msg("好呀", True, 30), msg("几点出发", True, 60),
        msg("我可以带点水果", True, 400),  # More than 180 s later: a separate thought, not this turn.
    ])
    assert [row["response"] for row in rows] == ["好呀\n几点出发"]
    assert rows[0]["bubbles"] == 2 and len(rows[0]["source_ids"]) == 3
    assert protocol["counts"]["multi_bubble_targets"] == 1 and protocol["split"] == "time_split"


def test_first_person_experience_targets_are_evaluated_but_never_trained(tmp_path):
    rows, protocol = replies(tmp_path, [
        msg("你在干嘛", False, 0), msg("我刚到家准备做饭", True, 30),
        msg("那早点休息哦", False, 1000), msg("好的呀谢谢你关心", True, 1030),
    ])
    by_text = {row["response"]: row for row in rows}
    assert by_text["我刚到家准备做饭"]["unbacked_first_person"] is True
    assert by_text["我刚到家准备做饭"]["training_eligible"] is False
    assert by_text["我刚到家准备做饭"]["evaluation_eligible"] is True
    assert by_text["好的呀谢谢你关心"]["training_eligible"] is True
    assert protocol["counts"]["unbacked_first_person_targets"] == 1


def test_replies_stating_numbers_the_chat_never_mentioned_are_not_trained(tmp_path):
    rows, protocol = replies(tmp_path, [
        msg("你学号多少", False, 0), msg("学号是18036427哈", True, 30),
        msg("几点的飞机", False, 1000), msg("你说的18点25那班吧", True, 1030),
        msg("18.25落地", False, 2000), msg("好的那18.25见哈", True, 2030),
    ])
    by_text = {row["response"]: row for row in rows}
    assert by_text["学号是18036427哈"]["training_eligible"] is False
    assert by_text["你说的18点25那班吧"]["training_eligible"] is False  # "18"/"25" were never said.
    assert by_text["好的那18.25见哈"]["training_eligible"] is True
    assert protocol["counts"]["unbacked_specific_targets"] == 2


def test_unbacked_specifics_ignore_vague_quantities():
    from rapport.llm.reply import unbacked_specifics

    assert unbacked_specifics("实习期一年，试用期两年", "B: 要背调") == ["一年", "两年"]
    assert unbacked_specifics("过几天再说，早一点睡", "B: 困") == []
    assert unbacked_specifics("那就24号见", "B: 我24号回") == []
    assert unbacked_specifics("百分之25的补贴", "B: 有补贴") == ["25"]


def test_personal_drafts_with_invented_numbers_are_withheld():
    from datetime import datetime

    from rapport.llm.reply import ReplyInput, validate_replies

    inp = ReplyInput(lines=["B: 填学号"], styles=["casual", "sincere"], memories=[], goal="回应")
    data = {"drafts": [{"style": "casual", "text": "18036427"}, {"style": "sincere", "text": "填完了跟你说"}]}
    pack = validate_replies(data, inp, {}, "r", datetime.now().astimezone(), strict_facts=True)
    assert [d.style for d in pack.drafts] == ["sincere"]
    assert pack.withheld_drafts[0]["style"] == "casual"
    cloud = validate_replies(data, inp, {}, "r", datetime.now().astimezone())
    assert len(cloud.drafts) == 2  # Only the personal (strict) path withholds.


def test_overlapping_export_versions_do_not_duplicate_reply_targets(tmp_path):
    messages = [msg("周末有空吗", False, 0), msg("有的呀你说", True, 30)]
    rows, protocol = replies(tmp_path, messages, extra=[("friend-again", messages, {"export": "second"})])
    assert len(rows) == 1
    assert protocol["counts"]["duplicate_targets"] == 1


def test_earlier_redaction_placeholders_still_quarantine_replies(tmp_path):
    assert redact_text("密钥是 [REDACTED_CREDENTIAL]")[1] is True
    assert redact_text("已经 [已移除凭证] 了")[1] is True
    rows, _ = replies(tmp_path, [msg("把 key 发我 [REDACTED_CREDENTIAL]", False, 0), msg("好的收到了", True, 30)])
    assert rows == []


def test_reply_models_use_the_chronological_split():
    assert reply_split({"split": "test", "time_split": "train"}) == "train"
    assert reply_split({"split": "validation", "time_split": "excluded_boundary"}) == "excluded_boundary"
    assert reply_split({"split": "test"}) == "test"


def test_verified_hash_reuses_cache_until_a_weight_file_changes(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    weights = tmp_path / "model.safetensors"
    weights.write_bytes(b"abc")
    calls = []
    original = lora._hash_tree

    def counted(root, patterns):
        calls.append(patterns)
        return original(root, patterns)

    monkeypatch.setattr(lora, "_hash_tree", counted)
    first = lora.verified_hash(tmp_path, lora.BASE_PATTERNS)
    assert lora.verified_hash(tmp_path, lora.BASE_PATTERNS) == first and len(calls) == 1
    weights.write_bytes(b"abcd")
    assert lora.verified_hash(tmp_path, lora.BASE_PATTERNS) != first and len(calls) == 2


def test_no_repeat_ngram_bans_only_repeats_of_generated_text():
    assert banned_ngram_tokens([1, 2, 3, 1, 2], 3) == {3}
    assert banned_ngram_tokens([1, 2], 3) == set()
    assert banned_ngram_tokens([5, 6, 7], 4) == set()


def test_short_text_cleanup_keeps_three_clean_lines():
    assert clean_short_text("A：好呀\n“几点出发”\n\n我带水\n第四行") == "好呀\n几点出发\n我带水"
    assert clean_short_text("```json\n{}```") == ""
    assert normalized("好 呀\nOK") == "好呀ok"
