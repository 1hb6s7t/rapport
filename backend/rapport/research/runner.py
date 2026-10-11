"""Reproducible, local-only Chinese adaptation of contextual emotion research."""

from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
import math
import random
import time
import uuid
from collections import Counter
from pathlib import Path

from rapport.research.data import digest, prepare, vocabulary


def _weights_hash(folder):
    h = hashlib.sha256()
    for p in sorted(folder.glob("*.safetensors")):
        with p.open("rb") as f:
            for block in iter(lambda: f.read(1024 * 1024), b""):
                h.update(block)
    return h.hexdigest()


def _report(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def code_provenance():
    root = Path(__file__).parent
    h = hashlib.sha256()
    for file in sorted(root.rglob("*.py")):
        h.update(file.relative_to(root).as_posix().encode())
        h.update(file.read_bytes())
    return {"code_sha256": h.hexdigest(), "dag_erc_commit": "ba7639dd12cdfa77cbde762f22f94266b30073d2",
            "dialogue_crn_commit": "2c2070d07ff190be25082804c13521aac8d25844",
            "dialogue_gcn": "paper D19-1015 equations 1-6; independent dense implementation",
            "paper_score_reproduction": False, "adaptation": "frozen Chinese encoder, multilabel emotion/intent heads",
            "torch": __import__("torch").__version__}


def feature_cache(examples, model_dir, data_dir, crypto, device, max_tokens=96, checkpoint=lambda: None):
    import torch
    from transformers import AutoModel, AutoTokenizer

    texts = sorted({text for e in examples for text in e.utterances})
    model_hash = _weights_hash(model_dir)
    config = {"model_sha256": model_hash, "texts_sha256": digest(texts), "max_tokens": max_tokens,
              "pooling": "l2_cls", "revision": "7999e1d3359715c523056ef9478215996d62a620"}
    folder = data_dir / "research" / "feature_cache"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{digest(config)}.bin"
    if path.exists():
        data = torch.load(io.BytesIO(crypto.decrypt(path.read_bytes())), map_location="cpu", weights_only=True)
        return {text: vector for text, vector in zip(texts, data["vectors"], strict=True)}, config
    tokenizer = AutoTokenizer.from_pretrained(str(model_dir), local_files_only=True, trust_remote_code=False)
    model = AutoModel.from_pretrained(str(model_dir), local_files_only=True, trust_remote_code=False).to(device).eval()
    vectors = []
    start = time.perf_counter()
    with torch.no_grad():
        for i in range(0, len(texts), 32):
            checkpoint()
            batch = tokenizer(texts[i:i + 32], padding=True, truncation=True, max_length=max_tokens, return_tensors="pt").to(device)
            cls = model(**batch).last_hidden_state[:, 0]
            vectors.append(torch.nn.functional.normalize(cls, p=2, dim=-1).cpu())
            if i % 320 == 0:
                print(json.dumps({"stage": "encode", "processed": i, "total": len(texts)}, ensure_ascii=False), flush=True)
    combined = torch.cat(vectors)
    buffer = io.BytesIO()
    torch.save({"vectors": combined}, buffer)
    checkpoint()
    path.write_bytes(crypto.encrypt(buffer.getvalue()))
    config["encoding_seconds"] = time.perf_counter() - start
    config["parameters"] = sum(x.numel() for x in model.parameters())
    del model
    if device == "cuda":
        torch.cuda.empty_cache()
    return {text: vector for text, vector in zip(texts, combined, strict=True)}, config


def collate(examples, vectors, labels, device):
    import torch

    lengths = torch.tensor([len(e.utterances) for e in examples], dtype=torch.long)
    width = next(iter(vectors.values())).numel()
    x = torch.zeros((len(examples), int(lengths.max()), width))
    speakers = torch.full((len(examples), int(lengths.max())), -1, dtype=torch.long)
    targets = torch.tensor([e.target for e in examples], dtype=torch.long)
    yy = {task: torch.zeros((len(examples), len(names))) for task, names in labels.items()}
    masks = {task: torch.zeros(len(examples), dtype=torch.bool) for task in labels}
    for i, e in enumerate(examples):
        x[i, :lengths[i]] = torch.stack([vectors[text] for text in e.utterances])
        speakers[i, :lengths[i]] = torch.tensor(e.speakers)
        for task, names in labels.items():
            for label in e.labels.get(task, []):
                if label in names:
                    yy[task][i, names.index(label)] = 1
            masks[task][i] = bool(e.labels.get(task))
    return x.to(device), speakers.to(device), lengths.to(device), targets.to(device), {k: v.to(device) for k, v in yy.items()}, {k: v.to(device) for k, v in masks.items()}


def metrics(truth, scores, names, threshold=.5, single_label=False):
    import torch

    truth = torch.tensor(truth).bool()
    probabilities = torch.tensor(scores)
    chosen = torch.nn.functional.one_hot(probabilities.argmax(-1), len(names)).bool() if single_label else probabilities >= threshold
    by_label = {}
    for i, label in enumerate(names):
        tp = int((truth[:, i] & chosen[:, i]).sum())
        fp = int((~truth[:, i] & chosen[:, i]).sum())
        fn = int((truth[:, i] & ~chosen[:, i]).sum())
        p = tp / (tp + fp) if tp + fp else 0
        r = tp / (tp + fn) if tp + fn else 0
        by_label[label] = {"precision": p, "recall": r, "f1": 2 * p * r / (p + r) if p + r else 0,
                           "support": int(truth[:, i].sum())}
    all_tp = int((truth & chosen).sum())
    all_fp = int((~truth & chosen).sum())
    all_fn = int((truth & ~chosen).sum())
    micro = 2 * all_tp / max(1, 2 * all_tp + all_fp + all_fn)
    ece = 0.0
    correctness = (chosen == truth).float()
    confidence = torch.where(chosen, probabilities, 1 - probabilities)
    for low in range(10):
        mask = (confidence >= low / 10) & (confidence < (low + 1) / 10 if low < 9 else confidence <= 1)
        if mask.any():
            ece += float(mask.float().mean() * (confidence[mask].mean() - correctness[mask].mean()).abs())
    return {"macro_f1": sum(x["f1"] for x in by_label.values()) / max(1, len(names)), "micro_f1": micro,
            "per_class": by_label, "exact_set_accuracy": float((chosen == truth).all(-1).float().mean()),
            "labelwise_ece": ece, "threshold": threshold, "calibrated": False,
            "empty_prediction_rate": float((chosen.sum(-1) == 0).float().mean())}


def evaluate(model, examples, vectors, labels, device, batch_size=16, thresholds=None, single_label=False):
    import torch

    model.eval()
    scores = {task: [] for task in labels}
    truth = {task: [] for task in labels}
    latencies = []
    with torch.no_grad():
        for start in range(0, len(examples), batch_size):
            batch = examples[start:start + batch_size]
            x, s, lengths, targets, yy, masks = collate(batch, vectors, labels, device)
            if device == "cuda":
                torch.cuda.synchronize()
            began = time.perf_counter()
            logits = model(x, s, lengths, targets)
            if device == "cuda":
                torch.cuda.synchronize()
            latencies.append((time.perf_counter() - began) / len(batch) * 1000)
            for task in labels:
                probabilities = logits[task].softmax(-1) if single_label else logits[task].sigmoid()
                scores[task].extend(probabilities[masks[task]].cpu().tolist())
                truth[task].extend(yy[task][masks[task]].cpu().tolist())
    task_metrics = {}
    for task, names in labels.items():
        if not truth[task]:
            continue
        relevant = [e for e in examples if e.labels.get(task)]
        unknown = Counter(label for e in relevant for label in e.labels[task] if label not in names)
        extra = sorted(unknown)
        threshold = (thresholds or {}).get(task, .5)
        known = metrics(truth[task], scores[task], names, threshold, single_label)
        full_truth = [ys + [float(label in e.labels[task]) for label in extra] for ys, e in zip(truth[task], relevant, strict=True)]
        full_scores = [ps + [0.] * len(extra) for ps in scores[task]]
        value = metrics(full_truth, full_scores, names + extra, threshold, single_label)
        value["macro_f1_known_labels"] = known["macro_f1"]
        value["unknown_label_false_negatives"] = sum(unknown.values())
        for label in extra:
            value["per_class"][label]["unseen_in_training"] = True
        task_metrics[task] = value
    return {"macro_f1": sum(x["macro_f1"] for x in task_metrics.values()) / max(1, len(task_metrics)),
            "tasks": task_metrics, "samples": len(examples),
            "context_head_latency_ms_p95": sorted(latencies)[max(0, math.ceil(len(latencies) * .95) - 1)],
            "latency_excludes_frozen_encoding": True}, truth, scores


def train_one(method, architecture, examples, vectors, labels, config, output, crypto, checkpoint=lambda: None):
    import torch
    from torch import nn

    from rapport.research.models import MultiTask

    seed = config["seed"]
    random.seed(seed)
    torch.manual_seed(seed)
    device = config["device"]
    single_label = config.get("classification") == "single_label"
    model = MultiTask(method, architecture, next(iter(vectors.values())).numel(), config["hidden"],
                      {k: len(v) for k, v in labels.items()}, config["visibility"] == "causal").to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=1e-3, weight_decay=1e-4)
    train = [e for e in examples if e.split == "train"]
    validation = [e for e in examples if e.split == "validation"]
    test = [e for e in examples if e.split == "test"]
    class_weights = {}
    for task, names in labels.items():
        count = Counter(label for e in train for label in e.labels.get(task, []))
        class_weights[task] = torch.tensor([min(20, math.sqrt((len(train) - count[label]) / max(1, count[label]))) for label in names], device=device)
    best_score = -1
    best_weights = None
    history = []
    began = time.perf_counter()
    for epoch in range(config["epochs"]):
        model.train()
        shuffled = list(train)
        random.Random(seed + epoch).shuffle(shuffled)
        losses = []
        for start in range(0, len(shuffled), config["batch_size"]):
            checkpoint()
            optimizer.zero_grad()
            x, s, lengths, targets, yy, masks = collate(shuffled[start:start + config["batch_size"]], vectors, labels, device)
            logits = model(x, s, lengths, targets)
            tasks = [nn.functional.cross_entropy(logits[task][masks[task]], yy[task][masks[task]].argmax(-1)) if single_label else
                     nn.functional.binary_cross_entropy_with_logits(logits[task][masks[task]], yy[task][masks[task]],
                                                                    pos_weight=class_weights[task]) for task in labels if masks[task].any()]
            loss = sum(tasks) / len(tasks)
            if not torch.isfinite(loss):
                raise ValueError("Non-finite research loss")
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 1)
            optimizer.step()
            losses.append(float(loss.detach().cpu()))
        validation_result, _, _ = evaluate(model, validation, vectors, labels, device, single_label=single_label)
        history.append({"epoch": epoch + 1, "loss": sum(losses) / len(losses), "validation_macro_f1": validation_result["macro_f1"]})
        print(json.dumps({"stage": "train", "method": method, "architecture": architecture, **history[-1]}), flush=True)
        if validation_result["macro_f1"] > best_score:
            best_score = validation_result["macro_f1"]
            best_weights = copy.deepcopy(model.state_dict())
    model.load_state_dict(best_weights)
    _, valid_truth, valid_scores = evaluate(model, validation, vectors, labels, device, single_label=single_label)
    thresholds = {}
    for task, names in labels.items():
        thresholds[task] = .5 if single_label else max([.15, .25, .35, .45, .5, .55, .65], key=lambda threshold: metrics(valid_truth[task], valid_scores[task], names, threshold)["macro_f1"])
    validation_result, _, _ = evaluate(model, validation, vectors, labels, device, thresholds=thresholds, single_label=single_label)
    test_result, truth, scores = evaluate(model, test, vectors, labels, device, thresholds=thresholds, single_label=single_label)
    # Fitted thresholds only use validation; test never selects epochs or parameters.
    unknown_test_labels = {task: dict(Counter(label for e in test for label in e.labels.get(task, []) if label not in labels[task])) for task in labels}
    buffer = io.BytesIO()
    torch.save({k: v.cpu() for k, v in model.state_dict().items()}, buffer)
    artifact = buffer.getvalue()
    model_name = f"{method}-{architecture}-seed{seed}"
    checkpoint()
    (output / f"{model_name}.weights.bin").write_bytes(crypto.encrypt(artifact))
    (output / f"{model_name}.predictions.bin").write_bytes(crypto.encrypt(json.dumps({"ids": [e.id for e in test], "truth": truth, "scores": scores}).encode()))
    return {"method": method, "architecture": architecture, "seed": seed, "validation": validation_result, "test": test_result,
            "thresholds": thresholds, "unknown_test_labels": unknown_test_labels, "history": history,
            "train_seconds": time.perf_counter() - began, "trainable_parameters": sum(p.numel() for p in model.parameters()),
            "artifact_sha256": hashlib.sha256(artifact).hexdigest(), "paper_score_reproduction": False,
            "production_enabled": False, "artifact_file": f"{model_name}.weights.bin"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--encoder", type=Path, required=True)
    parser.add_argument("--methods", default="utterance_mlp,dag_erc,dialogue_gcn,dialogue_crn")
    parser.add_argument("--architectures", default="shared,separate")
    parser.add_argument("--device", choices=["cpu", "cuda"], default="cpu")
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--seed", type=int, default=20260922)
    parser.add_argument("--train-limit", type=int, default=1200)
    parser.add_argument("--validation-limit", type=int, default=200)
    parser.add_argument("--test-limit", type=int, default=400)
    parser.add_argument("--visibility", choices=["causal", "offline"], default="causal")
    parser.add_argument("--split-protocol", choices=["friend", "time"], default="friend")
    parser.add_argument("--max-context", type=int, default=8)
    parser.add_argument("--hidden", type=int, default=64)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--encode-only", action="store_true")
    args = parser.parse_args()
    import torch

    from rapport.datasets import DatasetStore
    from rapport.learning.gpu import GPULease
    from rapport.research.lifecycle import ResearchCancelled, ResearchRun
    from rapport.research.models import ENCODERS
    from rapport.security import Crypto

    methods = args.methods.split(",")
    architectures = args.architectures.split(",")
    if set(methods) - set(ENCODERS) or set(architectures) - {"shared", "separate"}:
        parser.error("Unsupported method/architecture")
    torch.set_num_threads(2)
    crypto = Crypto(args.data_dir / ".key")
    store = DatasetStore(args.data_dir / "datasets", crypto)
    manifest = store.get(args.dataset_id)
    examples, data_info = prepare(store.records(args.dataset_id, "offline_review"), visibility=args.visibility,
                                  split_protocol=args.split_protocol, max_context=args.max_context,
                                  limits={"train": args.train_limit, "validation": args.validation_limit, "test": args.test_limit})
    if any(data_info["counts"].get(split, 0) < 4 for split in ("train", "validation", "test")):
        parser.error("Each split must include at least four examples")
    labels = vocabulary(examples)
    if not all(labels.values()):
        parser.error("Training labels missing")
    config = {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()}
    run_id = uuid.uuid4().hex
    guard = ResearchRun(args.data_dir, run_id, args.dataset_id)
    guard.check()
    output = args.data_dir / "research" / "runs" / run_id
    output.mkdir(parents=True)
    result = {"id": run_id, "status": "running", "data": data_info, "config": config, "dataset_id": args.dataset_id,
              "provenance": code_provenance(),
              "snapshot_sha256": manifest["snapshot_sha256"], "labels": labels, "experiments": [],
              "limitations": ["AI辅助标注的开发实验，不是盲测", "因果输入不等于标签已完成因果复核", "单个随机种子不能证明统计优势",
                              "原论文公开任务未复现", "不推断真实心理或关系阶段", "不自动进入生产模型注册表"]}
    _report(output / "report.json", result)
    lease = GPULease(args.data_dir, f"research:{run_id}") if args.device == "cuda" else None
    try:
        if lease:
            lease.__enter__()
            torch.cuda.reset_peak_memory_stats()
        guard.check()
        vectors, encoder_info = feature_cache(examples, args.encoder, args.data_dir, crypto, args.device, checkpoint=guard.check)
        result["encoder"] = encoder_info
        if args.encode_only:
            result["status"] = "features_ready"
            _report(output / "report.json", result)
            print(json.dumps({"stage": "features_ready", "run_id": run_id}), flush=True)
            return
        for method in methods:
            for architecture in architectures:
                try:
                    experiment = train_one(method, architecture, examples, vectors, labels, config, output, crypto, checkpoint=guard.check)
                    result["experiments"].append(experiment)
                except Exception as error:
                    result["experiments"].append({"method": method, "architecture": architecture, "status": "failed", "error_type": type(error).__name__})
                    _report(output / "report.json", result)
                    raise
                guard.check()
                _report(output / "report.json", result)
        result["status"] = "completed"
        if args.device == "cuda":
            result["peak_reserved_vram_bytes"] = torch.cuda.max_memory_reserved()
        _report(output / "report.json", result)
        print(json.dumps({"run_id": run_id, "report": str(output / "report.json"), "completed": len(result["experiments"])}), flush=True)
    except BaseException as error:
        result["status"] = "cancelled" if isinstance(error, (KeyboardInterrupt, ResearchCancelled)) else "failed"
        result["error_type"] = type(error).__name__
        if output.exists():
            _report(output / "report.json", result)
        raise
    finally:
        if lease:
            lease.__exit__(None, None, None)
        guard.finish(result["status"])


if __name__ == "__main__":
    main()
