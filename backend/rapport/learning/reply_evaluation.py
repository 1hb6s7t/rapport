"""Held-out evaluations through the production batch runtime; no automatic promotion."""
from __future__ import annotations

import hashlib
import json
import math
import random
import statistics
from datetime import datetime

from rapport.datasets.corpus import reply_split
from rapport.learning.lora import (
    COMPILED_PROTOCOLS,
    RETRIEVAL_PROTOCOLS,
    _messages,
    answer_loss,
    encode_reply,
)
from rapport.learning.reply_batch import BatchRuntime
from rapport.llm.personal_request import (
    FACT_POLICY,
    VERSION,
    compile_request,
    expression_hint,
)
from rapport.llm.reply import ReplyInput, validate_replies


def percentile95(values):
    return sorted(values)[max(0, math.ceil(.95 * len(values)) - 1)]


def vram_budget_bytes(total_bytes: int | None) -> int:
    """Resident reply budget: 7 GiB, the original 8 GB-card target, or on a larger card its memory
    minus 5 GiB left for the desktop, OCR and the browser."""
    return max(7 * 1024 ** 3, int(total_bytes or 0) - 5 * 1024 ** 3)


def distinct_reply_count(drafts) -> int:
    return len({"".join(draft.text.split()).casefold() for draft in drafts if draft.text.strip()})


def evaluation_input(row):
    context = row["context"][-12:]
    return ReplyInput(lines=[f"{'A' if m.get('sender') == 'me' else 'B'}: {m.get('text', '')}" for m in context],
                      styles=["casual", "sincere", "direct"], memories=[], goal="自然地回应对方",
                      expression_hint=expression_hint(m.get("text") for m in context if m.get("sender") == "me"))


def evaluate(service, job, rows, progress):
    import torch

    from rapport.learning.style_rag import example_pool, examples_for

    record = service.model(job["model_id"])
    tests = [r for r in rows if reply_split(r) == "test"
             and r.get("evaluation_eligible", r.get("training_eligible")) and r.get("response")]
    random.Random(20260924).shuffle(tests)
    # offset 0: the groups every earlier candidate was reviewed on; 64: the style route's fresh window.
    offset = int(job.get("eval_offset") or 0)
    tests = tests[offset:offset + job.get("max_eval_samples", 64)]
    if not tests:
        raise ValueError("没有独立测试回复")
    # Retrieval-trained candidates see the same examples as the style route (earlier train replies).
    pool = example_pool(rows) if record.get("training_protocol") in RETRIEVAL_PROTOCOLS else None
    shown = {row["id"]: examples_for(pool, row) for row in tests} if pool is not None else {}
    runtime = BatchRuntime(service, record)
    progress(.03, "预热个人回复模型")
    runtime.load()
    losses, base_losses, samples, times, base_times = [], [], [], [], []
    counts = {"empty_groups": 0, "truncated_groups": 0, "schema_failures": 0, "fact_review_groups": 0,
              "withheld_fact_drafts": 0, "insufficient_safe_candidates": 0,
              "insufficient_distinct_candidates": 0, "abstained_groups": 0}
    try:
        with runtime.on_device():
            for i, row in enumerate(tests):
                progress(.05 + .2 * i / len(tests), f"独立回复损失 {i}/{len(tests)}")
                if record.get("training_protocol") in COMPILED_PROTOCOLS:
                    prefix, target = encode_reply(runtime.tokenizer, row, examples=shown.get(row["id"]))
                else:
                    prefix = runtime.tokenizer.apply_chat_template(_messages(row), tokenize=True, add_generation_prompt=True, enable_thinking=False)
                    target = runtime.tokenizer.encode(row["response"], add_special_tokens=False)[:255] + [runtime.tokenizer.eos_token_id]
                    prefix = prefix[-max(1, 512 - len(target)):]
                with torch.inference_mode():
                    losses.append(float(answer_loss(runtime.model, prefix, target, runtime.device).cpu()))
                    with runtime.model.disable_adapter():
                        base_losses.append(float(answer_loss(runtime.model, prefix, target, runtime.device).cpu()))
        for i, row in enumerate(tests):
            progress(.26 + .69 * i / len(tests), f"生产同路径整组回复 {i}/{len(tests)}")
            inp = evaluation_input(row)
            inp.style_examples = list(shown.get(row["id"], []))
            request = compile_request(inp)
            # Alternate order to reduce systematic warm-cache ordering bias.
            if i % 2:
                base = runtime.complete(request, base=True)
                candidate = runtime.complete(request)
            else:
                candidate = runtime.complete(request)
                base = runtime.complete(request, base=True)
            times.append(candidate["elapsed_s"])
            base_times.append(base["elapsed_s"])
            pack = validate_replies(candidate["data"], inp, {}, "evaluation", datetime.now().astimezone(), strict_facts=True, split_lines=True)
            baseline = validate_replies(base["data"], inp, {}, "evaluation", datetime.now().astimezone(), strict_facts=True, split_lines=True)
            counts["schema_failures"] += int(len(pack.drafts) + len(pack.withheld_drafts) != len(inp.styles))
            counts["empty_groups"] += int(bool(candidate["invalid_styles"]))
            counts["truncated_groups"] += int(bool(candidate["truncated_styles"]))
            counts["fact_review_groups"] += int(bool(pack.withheld_drafts))
            counts["withheld_fact_drafts"] += len(pack.withheld_drafts)
            counts["insufficient_safe_candidates"] += int(len(pack.drafts) < min(2, len(inp.styles)))
            counts["insufficient_distinct_candidates"] += int(distinct_reply_count(pack.drafts) < min(2, len(inp.styles)))
            counts["abstained_groups"] += int(not pack.drafts)
            samples.append({"id": row["id"], "context": row["context"], "actual_reply": row["response"],
                            "candidate_reply": pack.drafts[0].text if pack.drafts else "",
                            "base_reply": baseline.drafts[0].text if baseline.drafts else "",
                            "structured_drafts": [d.model_dump(mode="json") for d in pack.drafts],
                            "base_drafts": [d.model_dump(mode="json") for d in baseline.drafts],
                            "withheld_drafts": pack.withheld_drafts,
                            "request_hash": hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
                            "prompt_tokens": candidate["prompt_tokens"], "completion_tokens": candidate["completion_tokens"],
                            "truncated_styles": candidate["truncated_styles"],
                            "resampled_styles": candidate.get("resampled_styles", 0)})
        peak = torch.cuda.max_memory_reserved() if runtime.device == "cuda" else 0
        budget = vram_budget_bytes(torch.cuda.get_device_properties(0).total_memory if runtime.device == "cuda" else 0)
        load_seconds = runtime.load_seconds
        load_breakdown = dict(getattr(runtime, "load_breakdown", {}))
    finally:
        runtime.close()
    reasons = []
    if len(tests) < 30:
        reasons.append("test_samples_below_30")
    if statistics.mean(losses) >= statistics.mean(base_losses):
        reasons.append("heldout_loss_did_not_improve")
    if percentile95(times) > 30:
        reasons.append("reply_group_latency_over_30_seconds")
    if peak > budget:
        reasons.append("peak_vram_over_budget")
    if counts["schema_failures"] or counts["empty_groups"]:
        reasons.append("structured_reply_validation_failed")
    if counts["truncated_groups"]:
        reasons.append("reply_generation_truncated")
    # Missing personal facts must be allowed to cause an explicit abstention.
    # Do not force fabricated alternatives just to fill every requested style.
    coverage = 1 - counts["abstained_groups"] / len(tests)
    if coverage < .9:
        reasons.append("reply_coverage_below_90_percent")
    distinct_coverage = 1 - counts["insufficient_distinct_candidates"] / len(tests)
    if distinct_coverage < .9:
        reasons.append("reply_alternatives_not_distinct")
    folder = service.root / "models" / record["id"]
    raw = json.dumps(samples, ensure_ascii=False).encode()
    temporary = folder / "evaluation.tmp"
    temporary.write_bytes(service.crypto.encrypt(raw))
    temporary.replace(folder / "evaluation.bin")
    metrics = {"samples": len(tests), "reply_loss": statistics.mean(losses), "base_reply_loss": statistics.mean(base_losses),
               "model_load_s": load_seconds, "cold_estimate_p95_s": percentile95(times) + load_seconds,
               "reply_group_warm_p95_s": percentile95(times), "base_reply_group_warm_p95_s": percentile95(base_times),
               "peak_reserved_vram_bytes": peak, "vram_budget_bytes": budget,
               "structured_samples": len(tests), "structured_failures": counts["schema_failures"],
               "validation_counts": counts, "generation_protocol": VERSION, "gpu_residency": "resident_lease_per_generation",
               "model_load_breakdown": load_breakdown,
               "test_ids_hash": hashlib.sha256("\n".join(r["id"] for r in tests).encode()).hexdigest(),
               "eval_offset": offset, "style_examples": pool is not None,
               "scope": "personal_development", "quality_requires_human_review": True,
               "loss_prompt_protocol": record.get("training_protocol", "natural-chat-v1"),
               "validation_policy": FACT_POLICY,
               "safe_candidate_group_coverage": coverage, "minimum_group_coverage": .9,
               "distinct_candidate_group_coverage": distinct_coverage,
               "acceptance_policy": "personal-selective-v1",
               "structure_note": "程序封装风格和消息结构；格式通过不是语义质量合格。"}
    gate = {"passed": False, "reasons": reasons + ["personal_generation_review_required"], "automatic_passed": not reasons}
    with service.db() as connection:
        live = service._get(connection, "models", record["id"])
        if not live or live["state"] == "revoked":
            raise ValueError("模型来源已撤销，评测不再可用")
        live["runtime_supported"] = True
        service._put(connection, "models", live)
    service.evaluated(record["id"], metrics, gate)
    return {"model_id": record["id"], "gate": gate, "metrics": metrics}
