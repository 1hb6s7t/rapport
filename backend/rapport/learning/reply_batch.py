"""The same short-reply batch path for private inference and held-out evaluation."""
from __future__ import annotations

import gc
import hashlib
import json
import os
import re
import time
from contextlib import contextmanager

from rapport.learning.gpu import GPULease
from rapport.llm.personal_request import (
    BATCH_SIZE,
    DISTINCT_RETRIES,
    FACT_POLICY,
    JUDGE,
    MAX_INPUT_TOKENS,
    MAX_NEW_TOKENS,
    NO_REPEAT_NGRAM_SIZE,
    REPETITION_PENALTY,
    REPETITION_SCOPE,
    SAMPLES_PER_STYLE,
    TEMPERATURE,
    TOP_P,
    VERSION,
)
from rapport.schemas.reply import STYLE_LABELS

MAX_REPLY_LINES = 3
IDLE_SECONDS = int(os.environ.get("RAPPORT_PERSONAL_IDLE_SECONDS", "1800"))


def validate_request(request):
    if request.get("protocol") != VERSION:
        raise ValueError("个人回复协议已变化，请重新预览")
    if any(request.get(k) != v for k, v in {"max_new_tokens": MAX_NEW_TOKENS, "max_input_tokens": MAX_INPUT_TOKENS, "batch_size": BATCH_SIZE,
                                          "repetition_penalty": REPETITION_PENALTY, "no_repeat_ngram_size": NO_REPEAT_NGRAM_SIZE,
                                          "repetition_scope": REPETITION_SCOPE, "temperature": TEMPERATURE, "top_p": TOP_P,
                                          "distinct_retries": DISTINCT_RETRIES, "samples_per_style": SAMPLES_PER_STYLE,
                                          "judge": JUDGE}.items()):
        raise ValueError("个人回复生成配置不匹配")
    lines = request.get("judge_lines")
    if not isinstance(lines, list) or len(lines) > 12 or any(not isinstance(line, str) or len(line) > 2000 for line in lines):
        raise ValueError("自评所需的对话无效")
    if request.get("fact_policy") != FACT_POLICY:
        raise ValueError("个人事实校验策略已变化，请重新预览")
    prompts = request.get("prompts")
    if not isinstance(prompts, list) or not 1 <= len(prompts) <= len(STYLE_LABELS):
        raise ValueError("回复风格数量无效")
    styles = [p.get("style") for p in prompts]
    if len(set(styles)) != len(styles) or any(s not in STYLE_LABELS for s in styles):
        raise ValueError("回复风格无效或重复")
    for prompt in prompts:
        messages = prompt.get("messages")
        if not isinstance(messages, list) or [m.get("role") for m in messages] != ["system", "user"]:
            raise ValueError("回复输入结构无效")
        if any(not isinstance(m.get("content"), str) or len(m["content"]) > 30000 for m in messages):
            raise ValueError("回复输入过长或无效")
    return prompts


def clean_short_text(value):
    """Strip role prefixes and wrapping quotes from each line; keep at most three short lines."""
    lines = []
    for line in str(value).strip().splitlines():
        line = re.sub(r"^(?:A|我|回复|草稿)[：:]\s*", "", line.strip())
        if len(line) >= 2 and (line[0], line[-1]) in {('"', '"'), ('“', '”')}:
            line = line[1:-1].strip()
        if line:
            lines.append(line)
    text = "\n".join(lines[:MAX_REPLY_LINES])
    if not text or text.startswith(("{", "```", "<think>")):
        return ""
    return text


def normalized(text: str) -> str:
    return "".join(str(text).split()).casefold()


def banned_ngram_tokens(tokens: list[int], size: int) -> set[int]:
    """Next tokens that would repeat an n-gram already present in ``tokens``."""
    if size < 2 or len(tokens) < size - 1:
        return set()
    prefix = tokens[len(tokens) - size + 1:]
    return {tokens[i + size - 1] for i in range(len(tokens) - size + 1) if tokens[i:i + size - 1] == prefix}


def generated_only_processors(prompt_length: int):
    """Repetition penalty and no-repeat n-grams over generated tokens only.

    Hugging Face's built-in versions also count the prompt, which pushed the model away
    from words the other person had just used and made replies drift off-topic.
    """
    import torch
    from transformers import LogitsProcessor, LogitsProcessorList

    class GeneratedRepetitionPenalty(LogitsProcessor):
        def __call__(self, input_ids, scores):
            generated = input_ids[:, prompt_length:]
            if generated.shape[1] == 0:
                return scores
            picked = torch.gather(scores, 1, generated)
            picked = torch.where(picked < 0, picked * REPETITION_PENALTY, picked / REPETITION_PENALTY)
            return scores.scatter(1, generated, picked)

    class GeneratedNoRepeatNGram(LogitsProcessor):
        def __call__(self, input_ids, scores):
            for row, tokens in enumerate(input_ids[:, prompt_length:].tolist()):
                banned = banned_ngram_tokens(tokens, NO_REPEAT_NGRAM_SIZE)
                if banned:
                    scores[row, sorted(banned)] = -float("inf")
            return scores

    return LogitsProcessorList([GeneratedRepetitionPenalty(), GeneratedNoRepeatNGram()])


def sample_shared(model, tokenizer, prompts: list[list[dict]], k: int, device, *, seed: int, greedy: bool = False):
    """k drafts per prompt with production decoding; each prompt is encoded once and its cache shared.

    Prompts are left-padded to one width, as a plain batched ``generate`` pads them, so both see the
    same positions. Returns (one list of (text, ended) per prompt, prompt tokens, generated tokens).
    ``greedy`` exists for tests only.
    """
    import torch

    encoded = [tokenizer(tokenizer.apply_chat_template(p, tokenize=False, add_generation_prompt=True, enable_thinking=False),
                         add_special_tokens=False)["input_ids"] for p in prompts]
    width = max(len(e) for e in encoded)
    if width > MAX_INPUT_TOKENS:
        raise ValueError("本地回复输入超过2048个词元，请减少记忆或对话")
    pad = tokenizer.pad_token_id if tokenizer.pad_token_id is not None else tokenizer.eos_token_id
    ids = torch.tensor([[pad] * (width - len(e)) + e for e in encoded], dtype=torch.long, device=device)
    mask = torch.tensor([[0] * (width - len(e)) + [1] * len(e) for e in encoded], dtype=torch.long, device=device)
    positions = (mask.cumsum(-1) - 1).clamp(min=0)
    torch.manual_seed(seed)
    with torch.inference_mode():
        # Only the cache is needed; logits over a ~150k vocabulary for every prompt position cost gigabytes.
        cache = model(input_ids=ids[:, :-1], attention_mask=mask[:, :-1], position_ids=positions[:, :-1], use_cache=True,
                      logits_to_keep=1).past_key_values
        cache.batch_repeat_interleave(k)
        rows = ids.repeat_interleave(k, dim=0)
        output = model.generate(input_ids=rows, attention_mask=mask.repeat_interleave(k, dim=0), past_key_values=cache,
                                max_new_tokens=MAX_NEW_TOKENS,
                                **({"do_sample": False} if greedy else {"do_sample": True, "temperature": TEMPERATURE, "top_p": TOP_P}),
                                logits_processor=generated_only_processors(width), pad_token_id=pad)
    stops = getattr(getattr(model, "generation_config", None), "eos_token_id", None)
    stops = set(stops if isinstance(stops, list) else [stops]) | {tokenizer.eos_token_id}
    results, generated = [[] for _ in prompts], 0
    for row, tokens in enumerate(output[:, width:].tolist()):
        cut = next((i for i, t in enumerate(tokens) if t in stops), None)
        generated += cut + 1 if cut is not None else len(tokens)
        text = clean_short_text(tokenizer.decode(tokens[:cut] if cut is not None else tokens, skip_special_tokens=True))
        results[row // k].append((text, cut is not None))
    return results, sum(len(e) for e in encoded), generated


class BatchRuntime:
    """Weights stay resident on the device; the GPU lease is held only while generating."""
    def __init__(self, service, record):
        self.service, self.record = service, record
        self.model = self.tokenizer = None
        self.device = "cpu"
        self._loaded = None
        self.load_seconds = 0.
        self.load_breakdown = {}

    def load(self):
        if self.model is not None:
            return
        from rapport.learning.reply_runtime import loaded

        t0 = time.perf_counter()
        self.load_breakdown = {}
        self._loaded = loaded(self.service, self.record, resident=True, timings=self.load_breakdown)
        self.model, self.tokenizer, self.device = self._loaded.__enter__()
        self.tokenizer.padding_side = "left"
        if self.tokenizer.pad_token_id is None:
            self.tokenizer.pad_token_id = self.tokenizer.eos_token_id
        self.load_seconds = time.perf_counter() - t0

    @contextmanager
    def on_device(self):
        self.load()
        with GPULease(self.service.data_dir, "personal-reply-batch"):
            yield

    def _seed(self, request, attempt: int, start: int) -> int:
        body = json.dumps(request, ensure_ascii=False, sort_keys=True).encode()
        return int(hashlib.sha256(body + f":{attempt}:{start}".encode()).hexdigest()[:8], 16)

    def complete(self, request, *, base=False):
        """Each style is sampled SAMPLES_PER_STYLE times; the base model with the adapter off scores the
        drafts and each style keeps its best one not already taken. A style left without a draft is
        sampled again (at most DISTINCT_RETRIES times)."""
        from contextlib import nullcontext

        from rapport.learning import self_judge

        prompts = validate_request(request)
        lines = list(request["judge_lines"])
        self.load()
        chosen = {}
        n_input = n_output = 0
        retried = 0
        t0 = time.perf_counter()
        with self.on_device():
            pending = prompts
            for attempt in range(DISTINCT_RETRIES + 1):
                with self.model.disable_adapter() if base else nullcontext():
                    sampled, used_input, used_output = sample_shared(self.model, self.tokenizer, [p["messages"] for p in pending],
                                                                     SAMPLES_PER_STYLE, self.device, seed=self._seed(request, attempt, 0))
                n_input += used_input
                n_output += used_output
                texts = sorted({text for group in sampled for text, _ in group if text})
                with self.model.disable_adapter():
                    scores = dict(zip(texts, self_judge.score_drafts(self.model, self.tokenizer, lines, texts, self.device), strict=True))
                taken = {normalized(text) for text, _, _ in chosen.values()}
                for prompt, group in zip(pending, sampled, strict=True):
                    options = sorted(((scores[text], text, ended) for text, ended in group
                                      if text and normalized(text) not in taken), key=lambda option: -option[0])
                    if options:
                        score, text, ended = options[0]
                        chosen[prompt["style"]] = (text, ended, score)
                        taken.add(normalized(text))
                pending = [prompt for prompt in prompts if prompt["style"] not in chosen]
                if not pending or attempt == DISTINCT_RETRIES:
                    break
                retried += len(pending)
        drafts, truncated, invalid = [], [], []
        for prompt in prompts:
            if prompt["style"] not in chosen:
                invalid.append(prompt["style"])
                continue
            text, ended, score = chosen[prompt["style"]]
            notes = [] if ended else ["回复到达长度上限，请核对句意是否完整。"]
            if not ended:
                truncated.append(prompt["style"])
            if score <= self_judge.LOW_SCORE:
                notes.append("本地自评认为这条可能没接住对方的话，或写了对话里没有的情况，请核对后再用。")
            drafts.append({"style": prompt["style"], "text": text, "used_memory_ids": [], "risk_notes": notes,
                           "judge_score": round(score, 3),
                           "items": [{"kind": "text", "text": line} for line in text.split("\n")]})
        return {"data": {"drafts": drafts, "overall_note": "由个人表达模型生成，仍需核对后使用。"},
                "prompt_tokens": n_input, "completion_tokens": n_output,
                "truncated_styles": truncated, "invalid_styles": invalid, "resampled_styles": retried,
                "elapsed_s": time.perf_counter() - t0, "protocol": VERSION}

    def close(self):
        self.model = self.tokenizer = None
        if self._loaded:
            self._loaded.__exit__(None, None, None)
            self._loaded = None
        gc.collect()


def main():
    """Bounded stdin/stdout protocol; no prompts, completions or credentials are logged."""
    import argparse
    import queue
    import sys
    import threading
    from pathlib import Path

    from rapport.learning.service import LearningService

    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    service = LearningService(args.data_dir, spawn_jobs=False)
    record = service.model(args.model)
    runtime = BatchRuntime(service, record)
    incoming = queue.Queue(maxsize=2)

    def reader():
        while True:
            line = sys.stdin.readline(250000)
            incoming.put(line)
            if not line:
                return

    threading.Thread(target=reader, daemon=True).start()
    try:
        while True:
            try:
                line = incoming.get(timeout=IDLE_SECONDS)
            except queue.Empty:
                break
            if not line:
                break
            try:
                packet = json.loads(line)
                request_id = packet.get("id")
                live = service.model(args.model)
                if live["state"] != "active" or live["artifact_hash"] != record["artifact_hash"]:
                    raise ValueError("个人模型已变更或撤销")
                if packet.get("operation") == "warm":
                    runtime.load()
                    response = {"load_seconds": runtime.load_seconds, "load_breakdown": runtime.load_breakdown, "ready": True}
                else:
                    response = runtime.complete(packet["request"])
                response["id"] = request_id
                print(json.dumps(response, ensure_ascii=False), flush=True)
            except Exception as exc:
                print(json.dumps({"error": type(exc).__name__, "message": "个人模型生成失败；未切换云端。"}, ensure_ascii=False), flush=True)
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
