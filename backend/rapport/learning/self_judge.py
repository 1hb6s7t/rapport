"""Base-model self judge for personal reply drafts.

The weights already loaded for the personal model, with the adapter switched off, answer three
yes/no questions about a draft (can A send it as is, does it invent anything, does it answer B's
last turn). The three questions share one prompt prefix (rubric and chat), computed once; a draft's
score is the mean of the three answers' log-odds (probabilities saturate near 0 and 1, where the
lowest drafts of a group still differ).

Measured on 32 validation groups (288 drafts from the round-2 model, labelled by hand): AUROC 0.71;
the lowest fifth of scores was 91% unusable (52/57) and the highest fifth 75% usable (43/57),
against a 42% base rate. Choosing the best of three drafts per style raised the groups with a
usable draft only from 24 to 26 of 32, so it is used where its low end is reliable: picking clearly
bad drafts to reject in a preference round.
"""
from __future__ import annotations

JUDGE_VERSION = "self-judge-v1"
# Mean log-odds at or below this was the lowest fifth of validation drafts, 91% of them unusable.
LOW_SCORE = -4.4
JUDGE_SYSTEM = (
    "你是严格的微信回复审核员。A 是我，B 是我的好友。你要判断一条候选回复能不能由 A 原样直接发给 B。\n"
    "四条标准，全部满足才算能发：\n"
    "1. 切题：回应的是对话最后 B 说的话；不答错轮次，不重复 B 或 A 刚说过的话，不再问 B 已经回答过的事，不把 A 和 B 搞反。\n"
    "2. 不编造：A 的位置、行程、时间、计划、经历、身体状况、做过或要做的事、数字，以及别人说过的话，"
    "对话里没有依据的都不能写成事实。\n"
    "3. 像 A 本人：口吻、长度、用词像 A 在对话里的说法，不像客服或助手。\n"
    "4. 可直接发送：通顺、完整、自然，不含说明、编号或角色名。"
)
# (question, answers meaning "good", answers meaning "bad"); only each answer's first token is read.
QUESTIONS = (
    ("这条候选回复能不能由 A 原样直接发出（四条标准全部满足）？只回答“是”或“否”。", ("是",), ("否",)),
    ("这条候选回复有没有编造：写了对话里没有依据的 A 的事情、数字，或别人说过的话？只回答“有”或“没有”。", ("没有", "没"), ("有",)),
    ("这条候选回复是否切题地回应了 B 最后说的话，而且没有把 A 和 B 搞反？只回答“是”或“否”。", ("是",), ("否",)),
)


def judge_messages(lines: list[str], candidate: str, question: str) -> list[dict]:
    """The candidate sits after the chat so that every candidate and question share the prefix."""
    user = "对话（最后是 B 刚说的话）：\n" + "\n".join(lines) + "\n\n候选回复：\n" + candidate + "\n\n" + question
    return [{"role": "system", "content": JUDGE_SYSTEM}, {"role": "user", "content": user}]


def first_token_ids(tokenizer, words) -> list[int]:
    return sorted({tokenizer.encode(word, add_special_tokens=False)[0] for word in words})


def common_prefix_length(sequences: list[list[int]]) -> int:
    """Tokens shared by every sequence, leaving at least one token of each for the batched pass."""
    first = sequences[0]
    limit = min(len(s) for s in sequences) - 1
    length = 0
    while length < limit and all(s[length] == first[length] for s in sequences):
        length += 1
    return length


def next_token_logprobs(model, tokenizer, sequences: list[list[int]], device):
    """Log-probabilities of each sequence's next token; the common prefix is encoded once and shared.

    The differing tails are left-padded after the shared prefix (masked, positions continuing the
    prefix), so every tail ends at the last column and only that column's logits are computed.
    """
    import torch

    n = len(sequences)
    shared = common_prefix_length(sequences)
    pad = tokenizer.pad_token_id if tokenizer.pad_token_id is not None else tokenizer.eos_token_id
    with torch.inference_mode():
        cache = None
        if shared:
            cache = model(input_ids=torch.tensor([sequences[0][:shared]], dtype=torch.long, device=device), use_cache=True,
                          logits_to_keep=1).past_key_values
            cache.batch_repeat_interleave(n)
        suffixes = [s[shared:] for s in sequences]
        width = max(len(s) for s in suffixes)
        ids = torch.tensor([[pad] * (width - len(s)) + s for s in suffixes], dtype=torch.long, device=device)
        mask = torch.tensor([[1] * shared + [0] * (width - len(s)) + [1] * len(s) for s in suffixes], dtype=torch.long, device=device)
        positions = torch.tensor([[shared] * (width - len(s)) + list(range(shared, shared + len(s))) for s in suffixes],
                                 dtype=torch.long, device=device)
        logits = model(input_ids=ids, attention_mask=mask, past_key_values=cache, position_ids=positions,
                       cache_position=torch.arange(shared, shared + width, device=device), use_cache=cache is not None,
                       logits_to_keep=1).logits
        return torch.log_softmax(logits[:, -1].float(), dim=-1)


def draft_log_odds(model, tokenizer, lines: list[str], drafts: list[str], device) -> list[list[float]]:
    """Per draft, log P(good answer) - log P(bad answer) for each question (adapter off: caller's job)."""
    import torch

    if not drafts:
        return []
    sequences, answers = [], []
    for draft in drafts:
        for question, good, bad in QUESTIONS:
            sequences.append(tokenizer.apply_chat_template(judge_messages(lines, draft, question), tokenize=True,
                                                           add_generation_prompt=True, enable_thinking=False))
            answers.append((first_token_ids(tokenizer, good), first_token_ids(tokenizer, bad)))
    logp = next_token_logprobs(model, tokenizer, sequences, device)
    odds = [float(torch.logsumexp(logp[row, good], dim=0) - torch.logsumexp(logp[row, bad], dim=0))
            for row, (good, bad) in enumerate(answers)]
    per = len(QUESTIONS)
    return [odds[i * per:(i + 1) * per] for i in range(len(drafts))]


def score_drafts(model, tokenizer, lines: list[str], drafts: list[str], device) -> list[float]:
    """Mean log-odds of the good answer over the three questions, per draft; higher is better."""
    return [sum(values) / len(values) for values in draft_log_odds(model, tokenizer, lines, drafts, device)]
