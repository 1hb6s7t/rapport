"""Offline adapter loading, held-out reply evaluation and bounded local generation."""
from __future__ import annotations

import argparse
import io
import json
import random
import statistics
import sys
import tempfile
import time
import zipfile
from contextlib import contextmanager, nullcontext
from datetime import datetime
from pathlib import Path

from rapport.learning.gpu import GPULease
from rapport.learning.lora import BASE_PATTERNS, TOKENIZER_PATTERNS, _messages, is_quantized, verified_hash


@contextmanager
def loaded(service, record, *, cpu_cache=False, resident=False, timings=None):
    """Load the base model and the decrypted adapter.

    ``resident`` places the weights on the device without holding the GPU lease for the
    lifetime of the context; the caller then leases the GPU around each generation.
    ``timings`` (a dict) receives per-phase seconds so slow loads can be diagnosed.
    """
    import os

    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    timings = timings if timings is not None else {}
    started = time.perf_counter()
    base = service.root / "base_models" / record["base_model_id"]
    if base.is_symlink() or base.is_junction() or not base.resolve().is_relative_to((service.root / "base_models").resolve()):
        raise ValueError("基座路径无效")
    if verified_hash(base, BASE_PATTERNS) != record["base_model_hash"]:
        raise ValueError("基座权重版本已变化")
    if verified_hash(base, TOKENIZER_PATTERNS) != record["tokenizer_hash"]:
        raise ValueError("分词器版本已变化")
    timings["verify_s"] = round(time.perf_counter() - started, 3)
    torch.set_num_threads(max(2, min(8, (os.cpu_count() or 4) // 2)))
    device = "cuda" if torch.cuda.is_available() else "cpu"
    target = "cpu" if cpu_cache else device
    # A 4-bit (QLoRA) base is loaded onto the GPU as saved and never moved.
    quantized = is_quantized(base)
    if quantized and target != "cuda":
        raise ValueError("4 位量化基座需要在 NVIDIA 显卡上运行")
    lease = nullcontext() if cpu_cache or resident else GPULease(service.data_dir, "personal-reply")
    with lease, tempfile.TemporaryDirectory(prefix="adapter-", dir=service.root) as temporary:
        phase = time.perf_counter()
        folder = Path(temporary)
        with zipfile.ZipFile(io.BytesIO(service.load_artifact(record))) as archive:
            if sum(item.file_size for item in archive.infolist()) > 1024 * 1024 * 1024:
                raise ValueError("适配器文件过大")
            for item in archive.infolist():
                if item.is_dir() or Path(item.filename).name != item.filename or item.filename in {".", ".."}:
                    raise ValueError("适配器路径无效")
                (folder / item.filename).write_bytes(archive.read(item))
        timings["adapter_decrypt_s"] = round(time.perf_counter() - phase, 3)
        dtype = torch.bfloat16 if device == "cuda" and torch.cuda.is_bf16_supported() else torch.float32
        if device == "cuda":
            torch.cuda.reset_peak_memory_stats()
        phase = time.perf_counter()
        tokenizer = AutoTokenizer.from_pretrained(str(base), local_files_only=True, trust_remote_code=False)
        # Materialise straight onto the target device instead of building on CPU and copying.
        placement = {"device_map": target} if target == "cuda" else {}
        model = AutoModelForCausalLM.from_pretrained(str(base), local_files_only=True, trust_remote_code=False, torch_dtype=dtype,
                                                     low_cpu_mem_usage=True, **placement)
        timings["base_load_s"] = round(time.perf_counter() - phase, 3)
        phase = time.perf_counter()
        model = PeftModel.from_pretrained(model, str(folder), local_files_only=True, is_trainable=False)
        model = (model if quantized else model.to(target)).eval()
        model.config.use_cache = True
        timings["adapter_attach_s"] = round(time.perf_counter() - phase, 3)
        timings["total_s"] = round(time.perf_counter() - started, 3)
        try:
            yield model, tokenizer, device
        finally:
            del model
            if device == "cuda":
                torch.cuda.empty_cache()


def generate(model, tokenizer, messages, device, max_tokens=128):
    import torch

    values = tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=True,
                                           enable_thinking=False, return_tensors="pt")
    if values.shape[1] > 4096:
        raise ValueError("请求超过本地个人模型的上下文预算，请缩短对话或减少记忆")
    values = values.to(device)
    with torch.inference_mode():
        result = model.generate(input_ids=values, attention_mask=torch.ones_like(values), max_new_tokens=max_tokens,
                                do_sample=False, pad_token_id=tokenizer.eos_token_id)
    suffix = result[0, values.shape[1]:]
    return tokenizer.decode(suffix, skip_special_tokens=True).strip(), int(values.shape[1]), int(suffix.shape[0])


def _evaluate_legacy_json(service, job, rows, progress):
    import torch

    record = service.model(job["model_id"])
    tests = [r for r in rows if r.get("split") == "test" and r.get("training_eligible") and r.get("response")]
    random.Random(20260924).shuffle(tests)
    tests = tests[:job.get("max_eval_samples", 64)]
    if not tests:
        raise ValueError("没有独立测试回复")
    start = time.perf_counter()
    with loaded(service, record) as (model, tokenizer, device):
        load_s = time.perf_counter() - start
        losses, base_losses, samples, times, group_times = [], [], [], [], []
        schema_failures = 0
        for i, row in enumerate(tests):
            progress(.05 + .85 * i / len(tests), f"独立回复评测 {i}/{len(tests)}")
            messages = _messages(row)
            prefix = tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=True, enable_thinking=False)
            target = tokenizer.encode(row["response"], add_special_tokens=False)[:255] + [tokenizer.eos_token_id]
            prefix = prefix[-max(1, 512 - len(target)):]
            values = torch.tensor([prefix + target], device=device)
            labels = torch.tensor([[-100] * len(prefix) + target], device=device)
            with torch.inference_mode():
                losses.append(float(model(input_ids=values, labels=labels).loss.cpu()))
                with model.disable_adapter():
                    base_losses.append(float(model(input_ids=values, labels=labels).loss.cpu()))
            t0 = time.perf_counter()
            text, _, _ = generate(model, tokenizer, messages, device)
            times.append(time.perf_counter() - t0)
            with model.disable_adapter():
                base_text, _, _ = generate(model, tokenizer, messages, device)
            samples.append({"id": row["id"], "context": row["context"], "actual_reply": row["response"],
                            "candidate_reply": text, "base_reply": base_text})
            if i < 10:
                from rapport.llm.reply import REPLY_SYSTEM, ReplyInput, validate_replies

                inp = ReplyInput(lines=[f"{'A' if m.get('sender') == 'me' else 'B'}: {m.get('text', '')}" for m in row["context"]],
                                 styles=["casual", "sincere", "direct"], memories=[], goal="自然地回应对方")
                t0 = time.perf_counter()
                structured, _, _ = generate(model, tokenizer, [{"role": "system", "content": REPLY_SYSTEM},
                                            {"role": "user", "content": inp.user_prompt()}], device, 768)
                group_times.append(time.perf_counter() - t0)
                try:
                    if structured.startswith("```"):
                        structured = structured.split("\n", 1)[1].rsplit("```", 1)[0]
                    pack = validate_replies(json.loads(structured), inp, {}, "evaluation", datetime.now().astimezone())
                    passed = len({draft.style for draft in pack.drafts}) >= 2 and not any(d.needs_review for d in pack.drafts)
                    samples[-1]["structured_drafts"] = [d.model_dump(mode="json") for d in pack.drafts]
                except (ValueError, TypeError, AttributeError):
                    passed = False
                schema_failures += int(not passed)
        peak = torch.cuda.max_memory_reserved() if device == "cuda" else 0
    p95 = sorted(times)[min(len(times) - 1, int(.95 * len(times)))]
    reasons = []
    if len(tests) < 30:
        reasons.append("test_samples_below_30")
    if statistics.mean(losses) >= statistics.mean(base_losses):
        reasons.append("heldout_loss_did_not_improve")
    if p95 + load_s > 30:
        reasons.append("local_latency_over_30_seconds")
    group_p95 = sorted(group_times)[min(len(group_times) - 1, int(.95 * len(group_times)))]
    if group_p95 + load_s > 30:
        reasons.append("reply_group_latency_over_30_seconds")
    if schema_failures:
        reasons.append("structured_reply_validation_failed")
    if peak > 7 * 1024 ** 3:
        reasons.append("peak_vram_over_7_gib")
    if any(not s["candidate_reply"] for s in samples):
        reasons.append("empty_candidate_reply")
    folder = service.root / "models" / record["id"]
    raw = json.dumps(samples, ensure_ascii=False).encode()
    (folder / "evaluation.bin").write_bytes(service.crypto.encrypt(raw))
    metrics = {"samples": len(tests), "reply_loss": statistics.mean(losses), "base_reply_loss": statistics.mean(base_losses),
               "warm_single_reply_p95_s": p95, "model_load_s": load_s, "cold_estimate_p95_s": p95 + load_s,
               "peak_reserved_vram_bytes": peak, "generation_max_tokens": 128,
               "reply_group_warm_p95_s": group_p95, "structured_samples": len(group_times), "structured_failures": schema_failures,
               "scope": "personal_development", "quality_requires_human_review": True}
    gate = {"passed": False, "reasons": reasons + ["personal_generation_review_required"], "automatic_passed": not reasons}
    with service.db() as c:
        live = service._get(c, "models", record["id"])
        live["runtime_supported"] = True
        service._put(c, "models", live)
    service.evaluated(record["id"], metrics, gate)
    return {"model_id": record["id"], "gate": gate}


def evaluate_lora(service, job, rows, progress):
    from rapport.learning.reply_evaluation import evaluate

    return evaluate(service, job, rows, progress)


def main():
    from rapport.learning import LearningService

    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    service = LearningService(args.data_dir, spawn_jobs=False)
    try:
        request = json.loads(sys.stdin.read(200000))
        record = service.model(args.model)
        if record["state"] != "active":
            raise ValueError("个人回复模型尚未启用")
        with loaded(service, record) as (model, tokenizer, device):
            text, prompt_tokens, completion_tokens = generate(model, tokenizer,
                [{"role": "system", "content": request["system"]}, {"role": "user", "content": request["user"]}],
                device, min(768, int(request.get("max_tokens", 512))))
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]
        value = json.loads(text)
        if not isinstance(value, dict):
            raise ValueError("个人模型未返回有效结构")
        print(json.dumps({"data": value, "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"error": type(exc).__name__, "message": "本地适配器生成失败或输出不合规，未切换到云端。"}, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()
