"""Genuine small, deterministic character n-gram Naive Bayes baselines.

This is deliberately NOT a reproduction of a graph/transformer paper. Shared and
separate variants isolate vocabulary sharing while using the same samples and budget.
Weights are plain numeric statistics, serialized encrypted by the learning service.
"""

from __future__ import annotations

import hashlib
import math
import random
import re
import time
from collections import Counter


def features(text: str) -> Counter:
    text = re.sub(r"\s+", " ", text.lower())[:8000]
    return Counter([text[i:i + n] for n in (1, 2) for i in range(max(0, len(text) - n + 1))])


def input_text(row: dict) -> str:
    # Causal by construction: the target's preceding context and target text only.
    context = row.get("context", [])[-12:]
    prefix = "\n".join(f"{m.get('sender', '')}: {m.get('text', '')}" for m in context)
    return f"{prefix}\n{row.get('text', '')}"[-8000:]


def labels(row: dict, task: str) -> list[str]:
    value = row.get("labels", {}).get(task, [])
    return [value] if isinstance(value, str) else sorted(set(str(x) for x in value if x))


def eligible(row: dict) -> bool:
    return bool(row.get("training_eligible")) and row.get("quality") != "weak"


def train(rows: list[dict], architecture: str, max_features: int, progress=lambda *_: None) -> dict:
    rows = [r for r in rows if r.get("split") == "train" and eligible(r)]
    if len(rows) < 4:
        raise ValueError("至少需要 4 条可训练样本；弱标注和待复核记录不会自动进入训练")
    common = Counter()
    task_vocab = {task: Counter() for task in ("emotion", "intent")}
    task_docs = {task: Counter() for task in task_vocab}
    counts = {task: {} for task in task_vocab}
    for i, row in enumerate(rows):
        fs = features(input_text(row))
        common.update(fs)
        for task in task_vocab:
            ls = labels(row, task)
            if not ls:
                continue
            task_vocab[task].update(fs)
            for label in ls:
                task_docs[task][label] += 1
                counts[task].setdefault(label, Counter()).update(fs)
        if i % 100 == 0:
            progress(i / len(rows), f"学习训练样本 {i}/{len(rows)}")
    heads = {}
    for task in task_vocab:
        vocab = [x for x, _ in (common if architecture == "shared" else task_vocab[task]).most_common(max_features)]
        allowed = set(vocab)
        heads[task] = {
            "vocabulary": vocab,
            "documents": dict(task_docs[task]),
            "counts": {label: {x: c for x, c in fs.items() if x in allowed} for label, fs in counts[task].items()},
        }
    if not any(h["documents"] for h in heads.values()):
        raise ValueError("数据没有可用的 emotion / intent 标签")
    return {"format": "rapport-char-nb-v1", "architecture": architecture, "heads": heads,
            "training_samples": len(rows), "tokenizer": "unicode-char-unigram-bigram-v1", "alpha": 1.0}


def predict(weights: dict, texts: list[str]) -> list[dict]:
    result = []
    for text in texts:
        fs = features(text)
        out = {}
        for task, head in weights["heads"].items():
            vocab = set(head["vocabulary"])
            fs_in = {x: n for x, n in fs.items() if x in vocab}
            docs = head["documents"]
            total_docs = sum(docs.values())
            scores = {}
            for label, n_docs in docs.items():
                count = head["counts"][label]
                denom = sum(count.values()) + max(1, len(vocab))
                scores[label] = math.log((n_docs + 1) / (total_docs + len(docs))) + sum(
                    n * math.log((count.get(x, 0) + 1) / denom) for x, n in fs_in.items()
                )
            if not scores:
                out[task] = {"label": None, "scores": {}, "uncalibrated": True}
                continue
            top = max(scores.values())
            exp = {label: math.exp(max(-700, score - top)) for label, score in scores.items()}
            norm = sum(exp.values())
            out[task] = {"label": max(scores, key=scores.get), "scores": {k: v / norm for k, v in exp.items()},
                         "uncalibrated": True}
        result.append(out)
    return result


def task_metrics(truth: list[list[str]], predictions: list[dict]) -> dict:
    all_labels = sorted(set(x for y in truth for x in y) | {p["label"] for p in predictions if p["label"]})
    by_label = {}
    for label in all_labels:
        tp = sum(label in y and p["label"] == label for y, p in zip(truth, predictions, strict=True))
        fp = sum(label not in y and p["label"] == label for y, p in zip(truth, predictions, strict=True))
        fn = sum(label in y and p["label"] != label for y, p in zip(truth, predictions, strict=True))
        precision = tp / (tp + fp) if tp + fp else 0
        recall = tp / (tp + fn) if tp + fn else 0
        by_label[label] = {"precision": precision, "recall": recall,
                           "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0,
                           "support": tp + fn}
    correct = [p["label"] in y for y, p in zip(truth, predictions, strict=True)]
    ece = 0.0
    for low in range(10):
        bucket = [i for i, p in enumerate(predictions) if low / 10 <= max(p["scores"].values(), default=0) <= (low + 1) / 10
                  and (low == 0 or max(p["scores"].values(), default=0) > low / 10)]
        if bucket:
            confidence = sum(max(predictions[i]["scores"].values(), default=0) for i in bucket) / len(bucket)
            acc = sum(correct[i] for i in bucket) / len(bucket)
            ece += len(bucket) / max(1, len(truth)) * abs(confidence - acc)
    confusion = Counter()
    for y, p in zip(truth, predictions, strict=True):
        for label in y:
            confusion[f"{label} → {p['label']}"] += 1
    return {"macro_f1": sum(v["f1"] for v in by_label.values()) / max(1, len(by_label)),
            "accuracy_any_label": sum(correct) / max(1, len(correct)), "per_class": by_label,
            "ece": ece, "samples": len(truth), "confusion": dict(confusion), "multilabel_policy": "top1_vs_all_truth_labels"}


def evaluate(weights: dict, rows: list[dict], split: str = "test", progress=lambda *_: None) -> dict:
    selected = [r for r in rows if r.get("split") == split and eligible(r)]
    if not selected:
        raise ValueError(f"{split} 集没有合格样本")
    predictions = []
    latencies = []
    for i in range(0, len(selected), 100):
        start = time.perf_counter()
        batch = predict(weights, [input_text(x) for x in selected[i:i + 100]])
        latencies.append((time.perf_counter() - start) / max(1, len(batch)))
        predictions.extend(batch)
        progress(i / len(selected), f"评测 {split} {i}/{len(selected)}")
    result = {}
    baselines = {}
    for task, head in weights["heads"].items():
        relevant = [i for i, row in enumerate(selected) if labels(row, task)]
        if not relevant or not head["documents"]:
            continue
        truth = [labels(selected[i], task) for i in relevant]
        pp = [predictions[i][task] for i in relevant]
        result[task] = task_metrics(truth, pp)
        majority = max(head["documents"], key=head["documents"].get)
        baselines[task] = task_metrics(truth, [{"label": majority, "scores": {majority: 1}} for _ in truth])
    macro = sum(m["macro_f1"] for m in result.values()) / max(1, len(result))
    base_macro = sum(m["macro_f1"] for m in baselines.values()) / max(1, len(baselines))
    # Resample complete conversation groups, not correlated individual messages.
    rng = random.Random(20260922)
    deltas = []
    clusters = {}
    for row, p in zip(selected, predictions, strict=True):
        d = []
        for task, h in weights["heads"].items():
            y = labels(row, task)
            if y and h["documents"]:
                majority = max(h["documents"], key=h["documents"].get)
                d.append(int(p[task]["label"] in y) - int(majority in y))
        if d:
            delta = sum(d) / len(d)
            deltas.append(delta)
            clusters.setdefault(row.get("session_id") or row["id"], []).append(delta)
    grouped = [(sum(values), len(values)) for values in clusters.values()]
    boot = []
    for _ in range(200):
        sampled = rng.choices(grouped, k=len(grouped)) if grouped else []
        boot.append(sum(total for total, _ in sampled) / max(1, sum(n for _, n in sampled)))
    boot.sort()
    return {"split": split, "samples": len(selected), "macro_f1": macro, "tasks": result,
            "majority_baseline": {"macro_f1": base_macro, "tasks": baselines},
            "accuracy_delta_ci95": [boot[5], boot[194]], "calibrated": False,
            "uncertainty_unit": "conversation_group", "bootstrap_groups": len(grouped),
            "latency_ms_p95": sorted(latencies)[max(0, math.ceil(len(latencies) * .95) - 1)] * 1000,
            "test_ids_hash": hashlib.sha256("\n".join(str(x["id"]) for x in selected).encode()).hexdigest(),
            "limitations": ["AI辅助标注不等同人工金标准", "未对比当前生产大模型", "不是论文复现", "不直接推断喜欢程度"]}
