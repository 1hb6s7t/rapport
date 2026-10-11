"""Preference round (DPO) on a personal adapter and the base-model self judge that picks rejected drafts."""

from __future__ import annotations

import pytest

from rapport.datasets.corpus import VERSION as DATASET_VERSION
from rapport.errors import RapportError
from rapport.learning import LearningService, lora, preference, reply_batch, self_judge
from rapport.learning.worker import run


def test_the_rejected_draft_is_the_lowest_clearly_bad_one_that_differs_from_our_reply():
    drafts = ["好啊", "我明天在学校", "  ", "好 啊", "你说呢"]
    scores = [-6.0, -9.0, -12.0, -7.5, 2.0]
    assert preference.pick_rejected(drafts, scores, "好啊", -4.4) == 1  # blank and our own reply never count
    assert preference.pick_rejected(drafts, [-1.0, -3.0, -12.0, -2.0, 2.0], "好啊", -4.4) is None  # none clearly bad
    assert preference.pick_rejected(["好啊"], [-9.0], "好 啊", -4.4) is None


def test_preference_rounds_keep_the_retrieval_prompts_of_their_parent():
    assert preference.PREFERENCE_PROTOCOL in lora.RETRIEVAL_PROTOCOLS
    assert preference.PREFERENCE_PROTOCOL in lora.COMPILED_PROTOCOLS


def test_live_requests_carry_the_judge_and_its_redacted_plain_lines():
    from rapport.learning.reply_batch import validate_request
    from rapport.llm.personal_request import JUDGE, SAMPLES_PER_STYLE, compile_request
    from rapport.llm.reply import ReplyInput

    assert JUDGE == self_judge.JUDGE_VERSION and preference.HYPERPARAMETERS["reject_at_or_below"] == self_judge.LOW_SCORE
    packet = compile_request(ReplyInput(lines=["m000012 09-26 14:32 B: 密码是 sk-abcdefghijklmnopqrstuvwx 记一下"], styles=["casual"],
                                        memories=[], goal="回应"))
    assert packet["samples_per_style"] == SAMPLES_PER_STYLE and packet["judge"] == JUDGE
    assert packet["judge_lines"][0].startswith("B: 密码是 ") and "sk-abcdefghijklmnopqrstuvwx" not in packet["judge_lines"][0]
    assert validate_request(packet) == packet["prompts"]
    for broken in ({"judge_lines": "B: 在吗"}, {"judge_lines": ["x"] * 13}, {"judge": "other"}, {"samples_per_style": 1}):
        with pytest.raises(ValueError):
            validate_request(packet | broken)


def test_dpo_loss_starts_at_log_two_and_falls_as_the_chosen_reply_gains():
    torch = pytest.importorskip("torch")
    same = torch.tensor(-10.)
    loss, margin = preference.dpo_terms(same, torch.tensor(-12.), -10., -12., .1)
    assert torch.isclose(loss, torch.log(torch.tensor(2.))) and margin == 0
    better, margin = preference.dpo_terms(torch.tensor(-9.), torch.tensor(-14.), -10., -12., .1)
    assert margin > 0 and better < loss
    worse, margin = preference.dpo_terms(torch.tensor(-11.), torch.tensor(-11.), -10., -12., .1)
    assert margin < 0 and worse > loss


class CharTokenizer:
    """Tiny-vocabulary character tokenizer with a chat template, enough for a random Qwen3."""

    eos_token_id = 1
    pad_token_id = 0
    padding_side = "left"
    words = {"是": 2, "否": 3, "有": 4, "没": 5}

    def encode(self, text, add_special_tokens=False):
        return [self.words.get(c, 6 + ord(c) % 50) for c in text]

    def __call__(self, text, add_special_tokens=False):
        return {"input_ids": self.encode(text)}

    def decode(self, ids, skip_special_tokens=True):
        return "".join(chr(ord("a") + i % 26) for i in ids if i > 1)

    def apply_chat_template(self, messages, tokenize=True, add_generation_prompt=True, enable_thinking=False):
        text = "".join(f"<{m['role']}>{m['content']}" for m in messages) + ("<assistant>" if add_generation_prompt else "")
        return self.encode(text) if tokenize else text


def tiny_model():
    torch = pytest.importorskip("torch")
    transformers = pytest.importorskip("transformers")
    config = transformers.Qwen3Config(vocab_size=64, hidden_size=32, intermediate_size=64, num_hidden_layers=2,
                                      num_attention_heads=4, num_key_value_heads=2, head_dim=8, eos_token_id=1, pad_token_id=0)
    torch.manual_seed(0)
    return transformers.Qwen3ForCausalLM(config).eval()


def test_shared_prefix_log_probs_equal_separate_forward_passes():
    torch = pytest.importorskip("torch")
    model, tokenizer = tiny_model(), CharTokenizer()
    prefix = tokenizer.encode("对话：B说今晚吃什么\n候选回复：")
    sequences = [prefix + tokenizer.encode(tail) for tail in ("火锅吧", "随便", "我在学校呢没空去")]
    got = self_judge.next_token_logprobs(model, tokenizer, sequences, "cpu")
    for row, sequence in enumerate(sequences):
        with torch.no_grad():
            expected = torch.log_softmax(model(input_ids=torch.tensor([sequence])).logits[0, -1].float(), -1)
        assert torch.allclose(got[row], expected, atol=1e-4)


def test_draft_scores_are_the_mean_log_odds_of_the_three_questions(monkeypatch):
    torch = pytest.importorskip("torch")
    tokenizer = CharTokenizer()

    def fake_logprobs(model, tokenizer, sequences, device):
        assert len(sequences) == 6  # two drafts, three questions each
        logp = torch.full((6, 64), -20.)
        for row in range(6):
            good = 2 if row % 3 != 1 else 5  # "是" for questions 1 and 3, "没" (有没有 → 没有) for question 2
            bad = 3 if row % 3 != 1 else 4
            logp[row, good], logp[row, bad] = (-1., -3.) if row < 3 else (-3., -1.)
        return logp

    monkeypatch.setattr(self_judge, "next_token_logprobs", fake_logprobs)
    assert self_judge.score_drafts(None, tokenizer, ["B: 在吗"], ["在的", "我在上班"], "cpu") == pytest.approx([2., -2.])
    assert self_judge.score_drafts(None, tokenizer, ["B: 在吗"], [], "cpu") == []


def test_cached_sampling_matches_plain_batched_generation_when_greedy(monkeypatch):
    torch = pytest.importorskip("torch")
    from rapport.learning.reply_batch import generated_only_processors

    monkeypatch.setattr(reply_batch, "MAX_NEW_TOKENS", 6)
    model, tokenizer = tiny_model(), CharTokenizer()
    prompts = [[{"role": "system", "content": "风格：轻松"}, {"role": "user", "content": "B: 今晚吃什么"}],
               [{"role": "system", "content": "风格：直接一点"}, {"role": "user", "content": "B: 今晚吃什么"}]]
    got, prompt_tokens, _ = reply_batch.sample_shared(model, tokenizer, prompts, 2, "cpu", seed=1, greedy=True)
    assert [len(group) for group in got] == [2, 2]
    encoded = [tokenizer.apply_chat_template(p) for p in prompts]
    width = max(map(len, encoded))
    assert prompt_tokens == sum(map(len, encoded))
    ids = torch.tensor([[0] * (width - len(e)) + e for e in encoded])
    mask = torch.tensor([[0] * (width - len(e)) + [1] * len(e) for e in encoded])
    with torch.no_grad():
        plain = model.generate(input_ids=ids, attention_mask=mask, max_new_tokens=6, do_sample=False,
                               logits_processor=generated_only_processors(width), pad_token_id=0)
    for group, tokens in zip(got, plain[:, width:].tolist(), strict=True):
        cut = tokens.index(1) if 1 in tokens else len(tokens)
        expected = tokenizer.decode(tokens[:cut])
        assert [text for text, _ in group] == [expected, expected]



def test_a_prompt_over_the_live_input_limit_is_refused_before_any_forward_pass(monkeypatch):
    pytest.importorskip("torch")
    from rapport.learning import reply_batch

    model, tokenizer = tiny_model(), CharTokenizer()
    monkeypatch.setattr(reply_batch, "MAX_INPUT_TOKENS", 20)
    monkeypatch.setattr(model, "forward", lambda *a, **k: pytest.fail("no forward pass for a refused prompt"))
    with pytest.raises(ValueError):
        reply_batch.sample_shared(model, tokenizer, [[{"role": "user", "content": "很长的对话" * 10}]], 2, "cpu", seed=1)

class ReplyDatasets:
    def __init__(self, rows):
        self.rows = rows

    def get(self, ident):
        return {"id": ident, "version": DATASET_VERSION}

    def records(self, ident, pool):
        return self.rows


def _parent(service, **changes):
    record = {"id": "parent", "task": "reply", "state": "evaluated", "dataset_id": "d1", "base_model_id": "base",
              "pool": "personal_reply", "architecture": "reply_lora", "created_at": 1, "metrics": {}, "artifact_hash": "x"}
    record.update(changes)
    service.register(record)
    return record


@pytest.fixture
def reply_learning(tmp_path, monkeypatch):
    service = LearningService(tmp_path, spawn_jobs=False)
    rows = [{"id": f"r{i}", "session_id": "s1", "time_split": "train", "training_eligible": True, "response": "好啊",
             "context": [{"sender": "other", "text": "去吗"}], "source_ids": [f"src{i}"]} for i in range(3)]
    data = ReplyDatasets(rows)
    monkeypatch.setattr(service, "datasets", lambda: data)
    monkeypatch.setattr(service, "capabilities", lambda: {"lora_dependencies": True})
    (tmp_path / "learning" / "base_models" / "base").mkdir(parents=True)
    (tmp_path / "learning" / "base_models" / "base" / "config.json").write_text("{}", encoding="utf-8")
    return service


def test_a_preference_job_continues_the_parent_on_its_dataset_and_base(reply_learning):
    _parent(reply_learning)
    job = reply_learning.create_job("train", parent_model_id="parent", epochs=1, max_eval_samples=32)
    assert (job["parent_model_id"], job["dataset_id"], job["base_model_id"]) == ("parent", "d1", "base")
    assert (job["pool"], job["architecture"], job["epochs"]) == ("personal_reply", "reply_lora", 1)
    _parent(reply_learning, id="gone", state="revoked")
    with pytest.raises(RapportError):
        reply_learning.create_job("train", parent_model_id="gone")
    with pytest.raises(RapportError):
        reply_learning.create_job("evaluate", model_id="parent", parent_model_id="parent")
    with pytest.raises(RapportError):
        reply_learning.create_job("train", parent_model_id="missing")


def test_the_worker_runs_the_preference_round_for_a_job_with_a_parent(reply_learning, monkeypatch):
    _parent(reply_learning)
    job = reply_learning.create_job("train", parent_model_id="parent", epochs=1)
    calls = []

    def fake_round(service, job, rows, progress):
        calls.append(job["parent_model_id"])
        return {"id": job["id"], "task": "reply", "state": "candidate", "dataset_id": job["dataset_id"], "created_at": 2,
                "source_ids": [], "trained_sample_ids": ["r0", "r1"], "parent_model_id": job["parent_model_id"], "metrics": {}}

    monkeypatch.setattr(preference, "train_preference", fake_round)
    monkeypatch.setattr(lora, "train_lora", lambda *args: pytest.fail("a job with a parent must not retrain from the base"))
    run(reply_learning, job["id"])
    finished = reply_learning.job(job["id"])
    assert finished["status"] == "completed" and calls == ["parent"]
    assert reply_learning.model(finished["result"]["model_id"])["parent_model_id"] == "parent"
