"""Preference round (DPO) on top of a trained personal adapter.

The parent adapter drafts replies for its own training rows, exactly as it would live (production
prompts and decoding, our earlier replies to that friend as examples). The self judge (the same base
weights with the adapter switched off, see ``self_judge``) scores every draft. When a row's lowest
draft scores clearly low, it becomes the rejected answer and our observed reply the chosen one; on
32 hand-labelled validation groups, drafts in that score range were unusable nine times out of ten
(invented plans and whereabouts, answers to the wrong turn, repeated questions).

The parent adapter is then trained further with the DPO loss. The reference is the parent itself:
its log-probabilities for every pair are computed once, before any update. A small likelihood term
on the chosen reply keeps what supervised training taught. Replies stating our own status, plans
or whereabouts are never chosen, as in supervised training.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import random
import tempfile
import time
import zipfile
from pathlib import Path

from rapport.learning.gpu import GPULease
from rapport.learning.reply_batch import sample_shared
from rapport.learning.self_judge import JUDGE_VERSION, LOW_SCORE

PREFERENCE_PROTOCOL = "personal-preference-v1"
HYPERPARAMETERS = {"beta": .1, "chosen_nll_weight": .2, "learning_rate": 1e-5, "warmup_fraction": .1, "schedule": "cosine",
                   "gradient_accumulation": 8, "epochs": 1, "samples_per_style": 2,
                   # Mean judge log-odds at or below this (the lowest fifth of validation drafts) marks a draft rejected.
                   "reject_at_or_below": LOW_SCORE, "judge": JUDGE_VERSION, "dropout": 0.0}
CACHE_VERSION = 1


def normalized(text: str) -> str:
    return "".join(str(text).split()).casefold()


def pick_rejected(drafts: list[str], scores: list[float], chosen: str, threshold: float) -> int | None:
    """Index of the lowest-scoring usable-looking draft to reject, or None when no draft is clearly bad."""
    options = [(score, i) for i, (text, score) in enumerate(zip(drafts, scores, strict=True))
               if text.strip() and normalized(text) != normalized(chosen) and score <= threshold]
    return min(options)[1] if options else None


def dpo_terms(policy_chosen, policy_rejected, reference_chosen, reference_rejected, beta: float):
    """DPO loss and the implicit reward margin (positive: the policy prefers the chosen answer more than the reference)."""
    import torch.nn.functional as F

    margin = beta * ((policy_chosen - reference_chosen) - (policy_rejected - reference_rejected))
    return -F.logsigmoid(margin), margin


def sequence_logprob(model, prefix: list[int], answer: list[int], device):
    """Sum of the answer tokens' log-probabilities; logits only for the answer positions."""
    import torch

    ids = torch.tensor([prefix + answer], dtype=torch.long, device=device)
    logits = model(input_ids=ids, attention_mask=torch.ones_like(ids), logits_to_keep=len(answer) + 1).logits
    logp = torch.log_softmax(logits[0, :-1].float(), dim=-1)
    target = torch.tensor(answer, dtype=torch.long, device=device)
    return logp.gather(1, target[:, None]).sum()


def _cache_path(service, parent_id: str) -> Path:
    return service.root / "models" / parent_id / f"preference-drafts-v{CACHE_VERSION}.bin"


def _load_cache(service, parent_id):
    path = _cache_path(service, parent_id)
    if not path.is_file():
        return {}
    try:
        value = json.loads(service.crypto.decrypt(path.read_bytes()))
    except Exception:  # noqa: BLE001 - an unreadable cache is rebuilt, never trusted
        return {}
    fresh = isinstance(value, dict) and value.get("judge") == JUDGE_VERSION and value.get("samples_per_style") == HYPERPARAMETERS["samples_per_style"]
    return value if fresh else {}


def _save_cache(service, parent_id, cache):
    path = _cache_path(service, parent_id)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(service.crypto.encrypt(json.dumps(cache, ensure_ascii=False).encode()))
    temporary.replace(path)


def train_preference(service, job, rows, progress):
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer, get_cosine_schedule_with_warmup

    from rapport.datasets.corpus import reply_split
    from rapport.learning import self_judge
    from rapport.learning.lora import (
        BASE_PATTERNS,
        TOKENIZER_PATTERNS,
        answer_loss,
        encode_reply,
        is_quantized,
        states_own_status,
        verified_hash,
    )
    from rapport.learning.reply_evaluation import evaluation_input
    from rapport.learning.service import digest
    from rapport.learning.style_rag import example_pool, examples_for
    from rapport.llm.personal_request import compile_request

    parent = service.model(job["parent_model_id"])
    if parent.get("task") != "reply" or parent.get("state") == "revoked" or parent.get("dataset_id") != job["dataset_id"]:
        raise ValueError("偏好训练只能接在同一数据集、未撤销的个人回复模型之后")
    settings = dict(HYPERPARAMETERS, epochs=job.get("epochs") or HYPERPARAMETERS["epochs"])
    eligible = [r for r in rows if r.get("training_eligible") and r.get("response") and not states_own_status(r["response"])]
    train_rows = [r for r in eligible if reply_split(r) == "train"]
    valid_rows = [r for r in eligible if reply_split(r) == "validation"]
    random.Random(20260929).shuffle(train_rows)
    random.Random(20260930).shuffle(valid_rows)
    train_rows = train_rows[:job.get("max_train_samples") or len(train_rows)]
    valid_rows = valid_rows[:job.get("max_eval_samples", 64)]
    if len(train_rows) < 20 or len(valid_rows) < 5:
        raise ValueError("偏好训练至少需要 20 条训练样本和 5 条独立验证样本")
    base = service.root / "base_models" / parent["base_model_id"]
    if base.is_symlink() or not base.resolve().is_relative_to((service.root / "base_models").resolve()):
        raise ValueError("模型路径必须位于本地注册目录")
    progress(.02, "校验本地基座与父模型")
    base_hash = verified_hash(base, BASE_PATTERNS)
    tokenizer_hash = verified_hash(base, TOKENIZER_PATTERNS)
    if base_hash != parent["base_model_hash"] or tokenizer_hash != parent["tokenizer_hash"]:
        raise ValueError("父模型的基座或分词器已变化")
    if not torch.cuda.is_available():
        raise ValueError("偏好训练需要 NVIDIA 显卡")
    pool = example_pool(rows)
    # As in supervised training: training rows see only earlier replies, validation rows the train pool.
    shown = {row["id"]: examples_for(pool, row, earlier_only=True) for row in train_rows}
    shown.update({row["id"]: examples_for(pool, row) for row in valid_rows})
    device = "cuda"
    torch.manual_seed(20260929)
    with GPULease(service.data_dir, f"preference:{job['id']}"), tempfile.TemporaryDirectory(prefix="rapport-pref-", dir=service.root) as temporary:
        folder = Path(temporary) / "parent"
        folder.mkdir()
        with zipfile.ZipFile(io.BytesIO(service.load_artifact(parent))) as archive:
            for item in archive.infolist():
                if item.is_dir() or Path(item.filename).name != item.filename:
                    raise ValueError("适配器路径无效")
                (folder / item.filename).write_bytes(archive.read(item))
        tokenizer = AutoTokenizer.from_pretrained(str(base), local_files_only=True, trust_remote_code=False)
        tokenizer.padding_side = "left"
        if tokenizer.pad_token_id is None:
            tokenizer.pad_token_id = tokenizer.eos_token_id
        quantized = is_quantized(base)
        model = AutoModelForCausalLM.from_pretrained(str(base), local_files_only=True, trust_remote_code=False, torch_dtype=torch.bfloat16,
                                                     low_cpu_mem_usage=True, device_map={"": 0} if quantized else None)
        if not quantized:
            model.to(device)
        model = PeftModel.from_pretrained(model, str(folder), is_trainable=True)
        torch.cuda.reset_peak_memory_stats()

        # 1. The parent's own drafts, judged with the adapter off; cached per parent so a retry resumes.
        cache = _load_cache(service, parent["id"]) or {"judge": JUDGE_VERSION, "samples_per_style": settings["samples_per_style"], "rows": {}}
        model.eval()
        model.config.use_cache = True
        started = time.monotonic()
        todo = [row for row in train_rows + valid_rows if row["id"] not in cache["rows"]]
        for i, row in enumerate(todo):
            if i % 4 == 0:
                left = (time.monotonic() - started) / i * (len(todo) - i) / 60 if i else 0.
                progress(.04 + .4 * i / len(todo), f"父模型起草并自评 {i}/{len(todo)} · 显存峰值 "
                         f"{torch.cuda.max_memory_reserved() / 1024 ** 3:.1f} GiB · 剩余约 {left:.0f} 分钟")
            inp = evaluation_input(row)
            inp.style_examples = list(shown[row["id"]])
            prompts = compile_request(inp)["prompts"]
            seed = int(hashlib.sha256(f"preference:{row['id']}".encode()).hexdigest()[:8], 16)
            try:
                sampled, _, _ = sample_shared(model, tokenizer, [p["messages"] for p in prompts], settings["samples_per_style"], device, seed=seed)
            except ValueError:  # over the live input limit: production would refuse this request too
                cache["rows"][row["id"]] = []
                continue
            drafts = [(p["style"], text) for p, group in zip(prompts, sampled, strict=True) for text, _ in group if text]
            with model.disable_adapter():
                scores = self_judge.score_drafts(model, tokenizer, inp.lines, [text for _, text in drafts], device)
            cache["rows"][row["id"]] = [[style, text, round(score, 3)] for (style, text), score in zip(drafts, scores, strict=True)]
            if i % 25 == 24 or i + 1 == len(todo):
                _save_cache(service, parent["id"], cache)
        torch.cuda.empty_cache()

        def pairs_of(selected):
            pairs = []
            for row in selected:
                drafts = cache["rows"].get(row["id"]) or []
                picked = pick_rejected([d[1] for d in drafts], [d[2] for d in drafts], row["response"], settings["reject_at_or_below"])
                if picked is None:
                    continue
                style, text, score = drafts[picked]
                prefix, chosen = encode_reply(tokenizer, row, style, shown[row["id"]])
                _, rejected = encode_reply(tokenizer, dict(row, response=text), style, shown[row["id"]])
                pairs.append({"id": row["id"], "prefix": prefix, "chosen": chosen, "rejected": rejected, "rejected_score": score})
            return pairs

        train_pairs, valid_pairs = pairs_of(train_rows), pairs_of(valid_rows)
        if len(train_pairs) < 20 or len(valid_pairs) < 5:
            raise ValueError("父模型的草稿里明显不合格的太少，无法构成偏好训练")

        # 2. Reference log-probabilities: the parent before any update.
        with torch.no_grad():
            for i, pair in enumerate(train_pairs + valid_pairs):
                if i % 50 == 0:
                    progress(.45 + .05 * i / (len(train_pairs) + len(valid_pairs)), f"父模型参考概率 {i}/{len(train_pairs) + len(valid_pairs)}")
                pair["ref_chosen"] = float(sequence_logprob(model, pair["prefix"], pair["chosen"], device))
                pair["ref_rejected"] = float(sequence_logprob(model, pair["prefix"], pair["rejected"], device))
            parent_valid_loss = sum(float(answer_loss(model, row_prefix, row_answer, device)) for row_prefix, row_answer in
                                    (encode_reply(tokenizer, row, examples=shown[row["id"]]) for row in valid_rows)) / len(valid_rows)

        # 3. DPO with a small chosen-likelihood term; dropout off so policy and reference agree at step 0.
        for module in model.modules():
            if isinstance(module, torch.nn.Dropout):
                module.p = 0.
        model.train()
        model.config.use_cache = False
        model.gradient_checkpointing_enable()
        model.enable_input_require_grads()
        optimizer = torch.optim.AdamW((p for p in model.parameters() if p.requires_grad), lr=settings["learning_rate"])
        accumulation = settings["gradient_accumulation"]
        total = len(train_pairs) * settings["epochs"]
        steps = max(1, math.ceil(total / accumulation))
        scheduler = get_cosine_schedule_with_warmup(optimizer, max(1, int(settings["warmup_fraction"] * steps)), steps)
        losses, margins = [], []
        optimizer.zero_grad()
        started = time.monotonic()
        for step in range(total):
            if step % 4 == 0:
                recent_loss, recent_margin = losses[-32:], margins[-32:]
                left = (time.monotonic() - started) / step * (total - step) / 60 if step else 0.
                progress(.5 + .4 * step / total, f"偏好训练 {step}/{total} · 近 {len(recent_loss)} 条损失 "
                         f"{(sum(recent_loss) / len(recent_loss)) if recent_loss else float('nan'):.3f} · 偏好正确 "
                         f"{(sum(m > 0 for m in recent_margin) / len(recent_margin)) if recent_margin else float('nan'):.2f} · 显存峰值 "
                         f"{torch.cuda.max_memory_allocated() / 1024 ** 3:.1f} GiB · 剩余约 {left:.0f} 分钟")
            pair = train_pairs[step % len(train_pairs)]
            policy_chosen = sequence_logprob(model, pair["prefix"], pair["chosen"], device)
            policy_rejected = sequence_logprob(model, pair["prefix"], pair["rejected"], device)
            loss, margin = dpo_terms(policy_chosen, policy_rejected, pair["ref_chosen"], pair["ref_rejected"], settings["beta"])
            loss = loss + settings["chosen_nll_weight"] * (-policy_chosen / len(pair["chosen"]))
            if not torch.isfinite(loss):
                raise ValueError("训练损失非有限值，已停止并保留父模型")
            (loss / accumulation).backward()
            losses.append(float(loss.detach()))
            margins.append(float(margin.detach()))
            if (step + 1) % accumulation == 0 or step + 1 == total:
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1)
                optimizer.step()
                scheduler.step()
                optimizer.zero_grad()

        # 4. Held-out preference accuracy and reply loss (validation rows only).
        model.eval()
        with torch.no_grad():
            valid_margins = []
            for i, pair in enumerate(valid_pairs):
                progress(.91 + .03 * i / len(valid_pairs), f"验证偏好 {i}/{len(valid_pairs)}")
                _, margin = dpo_terms(sequence_logprob(model, pair["prefix"], pair["chosen"], device),
                                      sequence_logprob(model, pair["prefix"], pair["rejected"], device),
                                      pair["ref_chosen"], pair["ref_rejected"], settings["beta"])
                valid_margins.append(float(margin))
            valid_loss = sum(float(answer_loss(model, row_prefix, row_answer, device)) for row_prefix, row_answer in
                             (encode_reply(tokenizer, row, examples=shown[row["id"]]) for row in valid_rows)) / len(valid_rows)
        out = Path(temporary) / "adapter"
        model.save_pretrained(out, safe_serialization=True)
        tokenizer.save_pretrained(out)
        progress(.95, "加密保存偏好训练后的适配器")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as z:
            for p in out.iterdir():
                if p.is_file():
                    z.write(p, p.name)
        payload = buffer.getvalue()
        model_folder = service.root / "models" / job["id"]
        model_folder.mkdir(parents=True, exist_ok=True)
        (model_folder / "weights.bin").write_bytes(service.crypto.encrypt(payload))
        peak = torch.cuda.max_memory_allocated()
        peak_reserved = torch.cuda.max_memory_reserved()
        del model, optimizer, scheduler
        torch.cuda.empty_cache()
    rejected_scores = sorted(p["rejected_score"] for p in train_pairs)
    return {"id": job["id"], "task": "reply", "architecture": "reply_lora", "method": "peft_lora_dpo",
            "state": "candidate", "pool": "personal_reply", "scope": "personal_reply", "dataset_id": job["dataset_id"],
            "dataset_hash": digest(rows), "source_ids": sorted(set(parent.get("source_ids", [])) | {f"dataset:{job['dataset_id']}"}),
            "artifact_hash": hashlib.sha256(payload).hexdigest(), "artifact_format": "encrypted_lora_zip",
            "base_model_id": parent["base_model_id"], "base_model_hash": base_hash, "tokenizer_hash": tokenizer_hash,
            "created_at": time.time(), "job_id": job["id"], "parent_model_id": parent["id"],
            "metrics": {"validation": {"loss": valid_loss, "samples": len(valid_rows), "parent_loss": parent_valid_loss,
                                       "preference_pairs": len(valid_pairs),
                                       "preference_accuracy": sum(m > 0 for m in valid_margins) / len(valid_margins),
                                       "mean_reward_margin": sum(valid_margins) / len(valid_margins)},
                        "train_loss": sum(losses) / len(losses),
                        "train_preference_accuracy": sum(m > 0 for m in margins[-len(train_pairs):]) / min(len(margins), len(train_pairs)),
                        "rows_drafted": len(train_rows) + len(valid_rows), "train_pairs": len(train_pairs),
                        "rejected_score_median": rejected_scores[len(rejected_scores) // 2],
                        "peak_training_vram_bytes": peak, "peak_training_reserved_vram_bytes": peak_reserved,
                        "training_seconds": round(time.monotonic() - started, 1)},
            "gate": {"passed": False, "reasons": ["generation_evaluation_required", "adapter_runtime_not_registered"]},
            "runtime_supported": False, "trained_samples": len(train_pairs), "quantized_base": is_quantized(base),
            "training_protocol": PREFERENCE_PROTOCOL, "parent_training_protocol": parent.get("training_protocol"),
            "hyperparameters": dict(parent.get("hyperparameters", {}), preference=settings),
            "trained_sample_ids": [p["id"] for p in train_pairs], "paper_replication": False}
