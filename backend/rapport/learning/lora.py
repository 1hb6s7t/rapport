"""Optional, genuinely trainable local PEFT adapter (no downloads, no cloud calls).

Only installed Hugging Face weights are accepted. Ollama/GGUF files are not training
checkpoints. This trainer does not imply that generation/runtime acceptance passed.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import random
import re
import tempfile
import time
import zipfile
from contextlib import nullcontext
from pathlib import Path

from rapport.learning.gpu import GPULease
from rapport.learning.service import digest


def _hash_tree(root: Path, patterns: tuple[str, ...]):
    h = hashlib.sha256()
    for p in sorted({p for pattern in patterns for p in root.glob(pattern)}):
        if p.is_symlink():
            raise ValueError("模型目录不能包含链接文件")
        h.update(p.name.encode())
        with p.open("rb") as f:
            for block in iter(lambda: f.read(1024 * 1024), b""):
                h.update(block)
    return h.hexdigest()


def verified_hash(root: Path, patterns: tuple[str, ...]):
    """``_hash_tree`` cached against each file's name, size and mtime.

    Re-hashing ~4 GB of base weights on every load was a large part of the cold start.
    Any change to a weight file's size or modification time forces a full re-hash.
    """
    files = sorted({p for pattern in patterns for p in root.glob(pattern)})
    if any(p.is_symlink() for p in files):
        raise ValueError("模型目录不能包含链接文件")
    signature = [[p.name, p.stat().st_size, p.stat().st_mtime_ns] for p in files]
    key = "|".join(patterns)
    cache_path = root / ".rapport-verified.json"
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    entry = cache.get(key) if isinstance(cache, dict) else None
    if isinstance(entry, dict) and entry.get("files") == signature and isinstance(entry.get("sha256"), str):
        return entry["sha256"]
    value = _hash_tree(root, patterns)
    cache = cache if isinstance(cache, dict) else {}
    cache[key] = {"files": signature, "sha256": value}
    try:
        temporary = cache_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(cache), encoding="utf-8")
        temporary.replace(cache_path)
    except OSError:
        pass  # Verification still happened; only the shortcut is lost.
    return value


BASE_PATTERNS = ("*.safetensors", "pytorch_model*.bin", "config.json")
TOKENIZER_PATTERNS = ("tokenizer*", "vocab*", "merges*", "special_tokens*")
MAX_SEQUENCE_TOKENS = 2048
MAX_ANSWER_TOKENS = 255
# v4: each sample also shows our earlier replies to the same friend (retrieved as the live route does).
# v5: replies stating our own status, plans, whereabouts or possessions are not fitted (see below).
TRAINING_PROTOCOL = "personal-instruction-style-v5"
# Protocols trained with retrieved examples in the prompt, and those whose loss prompt is the
# production compiler's (``encode_reply``).
# personal-preference-v1 (learning/preference.py): a preference round on such an adapter, same prompts.
RETRIEVAL_PROTOCOLS = {"personal-instruction-style-v4", TRAINING_PROTOCOL, "personal-preference-v1"}
COMPILED_PROTOCOLS = {"personal-instruction-natural-v1", "personal-instruction-style-v2", "personal-instruction-style-v3"} | RETRIEVAL_PROTOCOLS
# Our own status, plans, whereabouts, possessions or actions ("我在学校", "我问过了", "我明天有课"),
# not opinions or reactions ("我觉得", "我寻思", "我没事"). Such replies rest on things only we knew;
# fitted, they taught the model to invent them (the commonest review failure), so v5 leaves them out
# of training. Evaluation and the style examples keep them.
_OWN_SAFE = (r"(?:觉得|感觉|寻思|认为|估计|猜|知道|明白|懂|理解|同意|喜欢|希望|也是|也觉得|服了|靠|滴妈|嘞个|"
             r"没事|没关系|没问题|没毛病|没想到|要是|有点|有些)")
OWN_STATUS_RE = re.compile(r"我(?:们)?(?:这边|这里|这)?(?!" + _OWN_SAFE + r").{0,3}?"
                           r"(?:在|去|到|回|有|没有|没|要|打算|准备|刚|已经|明天|后天|今天|今晚|一会|待会|等会|下周|这周|周末|"
                           r"问(?:过|了)|说(?:过|了)|跟.{0,6}说|买|搁|放|带|拿|发|给你|帮你|来(?:处理|弄|搞|办)|"
                           r"上课|有课|上班|下班|加班|出门|出发|住|用|做|写|改|看了|吃了|考)")


def states_own_status(text) -> bool:
    return any(OWN_STATUS_RE.search(part) for part in re.split(r"[。！？!?\n，,]", str(text)))
HYPERPARAMETERS = {"lora_r": 16, "lora_alpha": 32, "lora_dropout": .05, "learning_rate": 2e-4,
                   "warmup_fraction": .05, "schedule": "cosine", "gradient_accumulation": 8,
                   "max_sequence_tokens": MAX_SEQUENCE_TOKENS, "max_answer_tokens": MAX_ANSWER_TOKENS,
                   "style_examples": "same friend, strictly earlier, top 8 by character bigrams",
                   "target_filter": "replies stating our own status, plans, whereabouts or possessions are not fitted"}


def is_quantized(base: Path) -> bool:
    """A registered base saved already quantized (bitsandbytes 4-bit): QLoRA, loaded straight onto the GPU."""
    try:
        return "quantization_config" in json.loads((base / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False


def encode_reply(tokenizer, row, style=None, examples=None):
    """Prompt and answer token ids. To fit, drops the oldest context lines (keeping four), then the least
    similar examples, then context down to one line; the rules are never cut."""
    from rapport.llm.personal_request import training_messages

    answer = tokenizer.encode(str(row["response"]), add_special_tokens=False)[:MAX_ANSWER_TOKENS]
    if tokenizer.eos_token_id is not None:
        answer.append(tokenizer.eos_token_id)
    context = list(row.get("context", [])[-12:])
    examples = list(examples or [])
    while True:
        messages = training_messages(dict(row, context=context), style, examples)
        if getattr(tokenizer, "chat_template", "unset") is not None:
            prefix = tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=True, enable_thinking=False)
        else:
            prefix = tokenizer.encode("\n".join(f"{m['role']}: {m['content']}" for m in messages) + "\nassistant:")
        if len(prefix) + len(answer) <= MAX_SEQUENCE_TOKENS:
            break
        if len(context) > 4:
            context = context[1:]
        elif examples:
            examples = examples[:-1]
        elif len(context) > 1:
            context = context[1:]
        else:
            break
    return prefix[-max(1, MAX_SEQUENCE_TOKENS - len(answer)):], answer


def answer_loss(model, prefix, answer, device):
    """Mean cross-entropy over the answer tokens, as with prompt-masked ``labels``.

    Logits are computed only for the answer positions: prompt-length logits over a ~150k vocabulary
    would take about 1 GB at 2k tokens for nothing.
    """
    import torch
    import torch.nn.functional as F

    ids = torch.tensor([prefix + answer], dtype=torch.long, device=device)
    logits = model(input_ids=ids, attention_mask=torch.ones_like(ids), logits_to_keep=len(answer) + 1).logits
    return F.cross_entropy(logits[0, :-1].float(), torch.tensor(answer, dtype=torch.long, device=device))


def _messages(row):
    result = []
    for msg in row.get("context", [])[-12:]:
        text = str(msg.get("text") or "").strip()
        if text:
            result.append({"role": "assistant" if msg.get("sender") in {"me", "self", "user"} else "user", "content": text})
    return result


def train_lora(service, job, rows, progress):
    import torch
    from peft import LoraConfig, TaskType, get_peft_model
    from transformers import AutoModelForCausalLM, AutoTokenizer, get_cosine_schedule_with_warmup

    from rapport.datasets.corpus import reply_split
    from rapport.learning.style_rag import example_pool, examples_for

    train_rows = [r for r in rows if reply_split(r) == "train" and r.get("training_eligible") and r.get("response")]
    own_status = sum(states_own_status(r["response"]) for r in train_rows)
    train_rows = [r for r in train_rows if not states_own_status(r["response"])]
    valid_rows = [r for r in rows if reply_split(r) == "validation" and r.get("training_eligible") and r.get("response")]
    if len(train_rows) < 20 or len(valid_rows) < 5:
        raise ValueError("回复训练至少需要 20 条合格训练样本和 5 条独立验证样本")
    random.Random(20260922).shuffle(train_rows)
    random.Random(20260923).shuffle(valid_rows)
    train_rows = train_rows[:job.get("max_train_samples") or len(train_rows)]
    valid_rows = valid_rows[:job.get("max_eval_samples", 64)]
    base = service.root / "base_models" / job["base_model_id"]
    if base.is_symlink() or not base.resolve().is_relative_to((service.root / "base_models").resolve()):
        raise ValueError("模型路径必须位于本地注册目录")
    progress(.02, "校验本地基座与分词器")
    base_hash = verified_hash(base, BASE_PATTERNS)
    tokenizer_hash = verified_hash(base, TOKENIZER_PATTERNS)
    # Examples for every row once: training rows see only their friend's earlier replies (as live),
    # validation rows the whole train pool, like the held-out evaluation and the style route.
    pool = example_pool(rows)
    shown = {row["id"]: examples_for(pool, row, earlier_only=True) for row in train_rows}
    shown.update({row["id"]: examples_for(pool, row) for row in valid_rows})
    torch.manual_seed(20260922)
    torch.set_num_threads(4)
    use_cuda = torch.cuda.is_available()
    device = "cuda" if use_cuda else "cpu"
    quantized = is_quantized(base)
    if quantized and not use_cuda:
        raise ValueError("4 位量化基座只能在 NVIDIA 显卡上训练")
    with (GPULease(service.data_dir, f"lora:{job['id']}") if use_cuda else nullcontext()), tempfile.TemporaryDirectory(prefix="rapport-lora-", dir=service.root) as temporary:
        tokenizer = AutoTokenizer.from_pretrained(str(base), local_files_only=True, trust_remote_code=False)
        # A 4-bit base loads straight onto the GPU and stays frozen (QLoRA); only the adapters train.
        model = AutoModelForCausalLM.from_pretrained(str(base), local_files_only=True, trust_remote_code=False,
                                                    torch_dtype=torch.bfloat16 if use_cuda and torch.cuda.is_bf16_supported() else torch.float32,
                                                    **({"device_map": {"": 0}, "low_cpu_mem_usage": True} if quantized else {}))
        model = get_peft_model(model, LoraConfig(task_type=TaskType.CAUSAL_LM, r=HYPERPARAMETERS["lora_r"],
                                                lora_alpha=HYPERPARAMETERS["lora_alpha"],
                                                lora_dropout=HYPERPARAMETERS["lora_dropout"], target_modules="all-linear"))
        if not quantized:
            model.to(device)
        model.config.use_cache = False
        model.gradient_checkpointing_enable()
        model.enable_input_require_grads()
        optimizer = torch.optim.AdamW((p for p in model.parameters() if p.requires_grad), lr=HYPERPARAMETERS["learning_rate"])
        accumulation = HYPERPARAMETERS["gradient_accumulation"]
        optimizer_steps = max(1, math.ceil(len(train_rows) * job["epochs"] / accumulation))
        scheduler = get_cosine_schedule_with_warmup(optimizer, max(1, int(HYPERPARAMETERS["warmup_fraction"] * optimizer_steps)),
                                                    optimizer_steps)

        def loss_of(net, row):
            prefix, answer = encode_reply(tokenizer, row, examples=shown[row["id"]])
            return answer_loss(net, prefix, answer, device)

        progress(.05, "测量微调前的验证回复损失")
        base_losses = []
        model.eval()
        with torch.no_grad(), model.disable_adapter():
            for i, row in enumerate(valid_rows):
                if i % 8 == 0:
                    progress(.05, f"基座验证 {i}/{len(valid_rows)}")
                base_losses.append(float(loss_of(model, row).detach().cpu()))
        model.train()
        total = len(train_rows) * job["epochs"]
        losses = []
        optimizer.zero_grad()
        started = time.monotonic()
        for step in range(total):
            if step % 4 == 0:
                # Probe line: step, recent loss, peak VRAM so far and remaining time.
                recent = losses[-32:]
                peak_gib = torch.cuda.max_memory_allocated() / 1024 ** 3 if use_cuda else 0.
                left_min = (time.monotonic() - started) / step * (total - step) / 60 if step else 0.
                progress(.08 + .72 * step / total, f"LoRA 样本 {step}/{total} · 近 {len(recent)} 条损失 "
                         f"{(sum(recent) / len(recent)) if recent else float('nan'):.3f} · 显存峰值 {peak_gib:.1f} GiB · 剩余约 {left_min:.0f} 分钟")
            loss = loss_of(model, train_rows[step % len(train_rows)])
            if not torch.isfinite(loss):
                raise ValueError("训练损失非有限值，已停止并保留旧模型")
            (loss / accumulation).backward()
            losses.append(float(loss.detach().cpu()))
            if (step + 1) % accumulation == 0 or step + 1 == total:
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1)
                optimizer.step()
                scheduler.step()
                optimizer.zero_grad()
        progress(.82, "评估独立验证集的回复损失")
        model.eval()
        validation_losses = []
        with torch.no_grad():
            for i, row in enumerate(valid_rows):
                if i % 20 == 0:
                    progress(.82 + .1 * i / len(valid_rows), f"验证回复 {i}/{len(valid_rows)}")
                validation_losses.append(float(loss_of(model, row).detach().cpu()))
        out = Path(temporary) / "adapter"
        model.save_pretrained(out, safe_serialization=True)
        tokenizer.save_pretrained(out)
        progress(.95, "加密保存真实 LoRA 适配器")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as z:
            for p in out.iterdir():
                if p.is_file():
                    z.write(p, p.name)
        payload = buffer.getvalue()
        folder = service.root / "models" / job["id"]
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "weights.bin").write_bytes(service.crypto.encrypt(payload))
        peak = torch.cuda.max_memory_allocated() if use_cuda else None
        peak_reserved = torch.cuda.max_memory_reserved() if use_cuda else None
        del model, optimizer, scheduler
        if use_cuda:
            torch.cuda.empty_cache()
    return {"id": job["id"], "task": "reply", "architecture": "reply_lora", "method": "peft_lora",
            "state": "candidate", "pool": "personal_reply", "scope": "personal_reply", "dataset_id": job["dataset_id"],
            "dataset_hash": digest(rows), "source_ids": sorted({str(x) for r in rows for x in r.get("source_ids", [])} | {f"dataset:{job['dataset_id']}"}),
            "artifact_hash": hashlib.sha256(payload).hexdigest(), "artifact_format": "encrypted_lora_zip", "base_model_id": job["base_model_id"],
            "base_model_hash": base_hash, "tokenizer_hash": tokenizer_hash, "created_at": time.time(), "job_id": job["id"],
            "metrics": {"validation": {"loss": sum(validation_losses) / len(validation_losses), "samples": len(valid_rows)},
                        "base_validation_loss": sum(base_losses) / len(base_losses),
                        "train_loss": sum(losses) / len(losses), "peak_training_vram_bytes": peak,
                        "peak_training_reserved_vram_bytes": peak_reserved,
                        "training_seconds": round(time.monotonic() - started, 1)},
            "gate": {"passed": False, "reasons": ["generation_evaluation_required", "adapter_runtime_not_registered"]},
            "runtime_supported": False, "trained_samples": len(train_rows), "quantized_base": quantized,
            "excluded_own_status_targets": own_status,
            "training_protocol": TRAINING_PROTOCOL, "hyperparameters": dict(HYPERPARAMETERS, epochs=job["epochs"]),
            "trained_sample_ids": [row["id"] for row in train_rows], "paper_replication": False}
