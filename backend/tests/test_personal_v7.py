"""Personal model v7: retrieved style examples in the prompt, 4-bit (QLoRA) bases, answer-only loss, VRAM budget."""

from __future__ import annotations

import json

import pytest

from rapport.learning import lora, style_rag
from rapport.learning.reply_evaluation import evaluation_input, vram_budget_bytes
from rapport.llm.personal_request import EXAMPLES_KEY, compile_request, training_messages
from rapport.llm.reply import ReplyInput

GIB = 1024 ** 3


def _row(ident, when, said, reply, split="train", session="s1", eligible=True):
    return {"id": ident, "session_id": session, "time_split": split, "training_eligible": eligible, "response": reply,
            "context": [{"sender": "other", "text": said, "ts": when}]}


def _user_block(packet):
    return json.loads(packet["prompts"][0]["messages"][1]["content"])


def test_examples_are_shown_to_the_personal_model_only_when_retrieved():
    plain = ReplyInput(lines=["B: 在吗"], styles=["casual"], memories=[], goal="自然地回应对方")
    assert EXAMPLES_KEY not in _user_block(compile_request(plain))
    shown = ReplyInput(lines=["B: 在吗"], styles=["casual"], memories=[], goal="自然地回应对方",
                       style_examples=[{"said": "睡了吗", "reply": "还没呢"}])
    assert _user_block(compile_request(shown))[EXAMPLES_KEY] == [{"B": "睡了吗", "我": "还没呢"}]


def test_live_chat_lines_reach_the_model_in_the_evaluated_form():
    def inp(lines):
        return ReplyInput(lines=lines, styles=["casual", "direct"], memories=[], goal="自然地回应对方", expression_hint="")

    plain = ["A: 在吗", "B: 今晚吃啥", "B: m0001 这句正文不动", "B: [sticker]"]
    live = ["m000011 09-26 14:30 A: 在吗", "m000012 09-26 14:32 B: 今晚吃啥", "m000013 --:-- B: m0001 这句正文不动", "m000014 B: [sticker]"]
    assert compile_request(inp(live))["prompts"] == compile_request(inp(plain))["prompts"]
    assert _user_block(compile_request(inp(plain)))["对话"] == plain  # the evaluated form is left as it is


def test_training_prompt_with_examples_is_the_production_prompt():
    row = {"id": "s:3", "context": [{"sender": "other", "text": "今晚有空吗"}]}
    examples = [{"said": "明天呢", "reply": "明天可以"}]
    expected = compile_request(ReplyInput(lines=["B: 今晚有空吗"], styles=["direct"], memories=[], goal="自然地回应对方",
                                          expression_hint="", style_examples=examples))["prompts"][0]["messages"]
    assert training_messages(row, "direct", examples) == expected
    assert training_messages(row, "direct") != expected  # no examples: the v6 prompt shape


def test_training_rows_only_see_their_friends_earlier_replies():
    rows = [_row("a", "2026-01-01T10:00:00", "吃饭了吗", "吃过啦"),
            _row("b", "2026-01-02T10:00:00", "吃饭了吗今天", "还没呢"),
            _row("c", "2026-01-03T10:00:00", "吃饭了吗晚上", "刚吃完"),
            _row("x", "2026-01-01T09:00:00", "吃饭了吗", "别的好友的回复", session="s2"),
            _row("t", "2026-02-01T10:00:00", "吃饭了吗", "测试回复", split="test")]
    pool = style_rag.example_pool(rows)
    assert {e["reply"] for e in pool} == {"吃过啦", "还没呢", "刚吃完", "别的好友的回复"}  # train split only
    earlier = style_rag.examples_for(pool, rows[1], earlier_only=True)
    assert [e["reply"] for e in earlier] == ["吃过啦"]  # not itself, not later, not another friend
    assert style_rag.examples_for(pool, rows[0], earlier_only=True) == []
    held_out = style_rag.examples_for(pool, rows[4])
    assert {e["reply"] for e in held_out} == {"吃过啦", "还没呢", "刚吃完"}
    assert style_rag.examples_for(pool, dict(rows[1], context=[{"sender": "other", "text": "x"}]), earlier_only=True) == []


class CharTokenizer:
    """One token per character; the chat template joins roles and contents."""
    eos_token_id = 0
    chat_template = "chars"

    def encode(self, text, add_special_tokens=False):
        return [ord(c) for c in text]

    def apply_chat_template(self, messages, tokenize=True, add_generation_prompt=True, enable_thinking=False):
        return self.encode("".join(m["role"] + m["content"] for m in messages))


def test_long_prompts_drop_old_context_then_examples_but_never_the_rules(monkeypatch):
    tokenizer = CharTokenizer()
    row = {"id": "r", "response": "好的", "context": [{"sender": "other", "text": f"第{i}句" + "很长" * 10} for i in range(12)]}
    examples = [{"said": "问" * 40, "reply": "答" * 40} for _ in range(8)]
    full, _ = lora.encode_reply(tokenizer, row, "casual", examples)
    monkeypatch.setattr(lora, "MAX_SEQUENCE_TOKENS", len(full) - 200)
    prefix, answer = lora.encode_reply(tokenizer, row, "casual", examples)
    text = "".join(map(chr, prefix))
    assert len(prefix) + len(answer) <= lora.MAX_SEQUENCE_TOKENS
    assert "你代写我的微信回复" in text  # rules kept whole
    assert "第11句" in text and "第0句" not in text  # the newest lines stay, the oldest go first
    assert text.count("问" * 40) == 8  # 200 tokens came out of the context before any example
    monkeypatch.setattr(lora, "MAX_SEQUENCE_TOKENS", len(full) - 500)  # more than eight old lines can give
    prefix, answer = lora.encode_reply(tokenizer, row, "casual", examples)
    text = "".join(map(chr, prefix))
    assert 0 < text.count("问" * 40) < 8 and "第11句" in text and "第8句" in text and "你代写我的微信回复" in text


def test_evaluation_input_carries_no_examples_until_the_evaluation_adds_them():
    inp = evaluation_input({"context": [{"sender": "other", "text": "在吗"}]})
    assert inp.style_examples == []


def test_quantized_base_is_recognised_from_its_config(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"model_type": "qwen3"}), encoding="utf-8")
    assert not lora.is_quantized(tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({"quantization_config": {"load_in_4bit": True}}), encoding="utf-8")
    assert lora.is_quantized(tmp_path)
    assert not lora.is_quantized(tmp_path / "missing")


def test_vram_budget_scales_with_the_card_but_never_below_the_old_target():
    assert vram_budget_bytes(None) == 7 * GIB
    assert vram_budget_bytes(8 * GIB) == 7 * GIB
    assert vram_budget_bytes(16 * GIB) == 11 * GIB


def test_evaluation_jobs_record_the_test_window(tmp_path):
    from rapport.learning.service import LearningService

    service = LearningService(tmp_path, spawn_jobs=False)
    from rapport.datasets.corpus import VERSION

    service.datasets = lambda: type("Store", (), {"get": staticmethod(lambda ident: {"version": VERSION})})()
    service.register({"id": "m1", "created_at": 1, "task": "reply", "state": "candidate", "metrics": {},
                      "dataset_id": "d1", "pool": "personal_reply", "architecture": "reply_lora", "artifact_hash": "x"})
    job = service.create_job("evaluate", model_id="m1", max_eval_samples=64, eval_offset=64)
    assert job["eval_offset"] == 64 and job["max_eval_samples"] == 64


def test_answer_loss_equals_the_prompt_masked_label_loss():
    torch = pytest.importorskip("torch")
    transformers = pytest.importorskip("transformers")
    config = transformers.Qwen3Config(vocab_size=64, hidden_size=32, intermediate_size=64, num_hidden_layers=2,
                                      num_attention_heads=4, num_key_value_heads=2, head_dim=8)
    torch.manual_seed(0)
    model = transformers.Qwen3ForCausalLM(config).eval()
    prefix, answer = [5, 9, 11, 3, 7], [21, 22, 0]
    ids = torch.tensor([prefix + answer])
    with torch.no_grad():
        expected = model(input_ids=ids, labels=torch.tensor([[-100] * len(prefix) + answer])).loss
        got = lora.answer_loss(model, prefix, answer, "cpu")
    assert torch.allclose(got, expected, atol=1e-5)


def test_the_turn_to_answer_is_bs_trailing_messages_in_either_line_format():
    from rapport.llm.personal_request import TARGET_KEY, reply_target

    assert reply_target(["B: 在吗", "A: 在", "B: 明天有空吗", "B: 想约你吃饭"]) == ["明天有空吗", "想约你吃饭"]
    assert reply_target(["m000011 09-26 14:30 A: 在", "m000012 09-26 14:32 B: 几点"]) == ["几点"]
    assert reply_target(["m000012 --:-- B: 几点"]) == ["几点"]
    assert reply_target(["B: 在吗", "A: 在"]) == []  # we spoke last: nothing to answer
    assert reply_target([f"B: {i}" for i in range(6)]) == ["3", "4", "5"]
    packet = compile_request(ReplyInput(lines=["A: 在", "B: 几点"], styles=["casual"], memories=[], goal="自然地回应对方"))
    assert _user_block(packet)[TARGET_KEY] == ["几点"]
    silent = compile_request(ReplyInput(lines=["B: 几点", "A: 三点"], styles=["casual"], memories=[], goal="自然地回应对方"))
    assert TARGET_KEY not in _user_block(silent)


@pytest.mark.parametrize("reply", ["我问过了，他说没有", "我在学校呢", "我明天有课", "我搁你书包里了", "我给你发过去",
                                   "我这边已经跟他们说了", "我们下周去"])
def test_replies_stating_our_own_status_are_not_fitted(reply):
    assert lora.states_own_status(reply)


@pytest.mark.parametrize("reply", ["我觉得他在忙", "我寻思你睡着了", "我没事", "哈哈哈哈", "那你去吧", "我要是你就不去",
                                   "你在哪呢", "我也是这么想的"])
def test_opinions_and_reactions_are_still_fitted(reply):
    assert not lora.states_own_status(reply)
