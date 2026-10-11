"""Run the existing intent prompt/validation with a named *local* small LM.

No provider key or network endpoint is accepted. This is not a measurement of the
original cloud model and cannot be presented as one. Private details are encrypted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from pathlib import Path

from rapport.llm.provider import CallUsage, LLMError, ProviderInfo
from rapport.research.data import prepare


class LocalHFProvider:
    reasoning = False

    def __init__(self, model_path: Path, device="cpu", max_new_tokens=384):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        self.torch = torch
        self.device = device
        self.max_new_tokens = max_new_tokens
        self.tokenizer = AutoTokenizer.from_pretrained(str(model_path), local_files_only=True, trust_remote_code=False)
        self.model = AutoModelForCausalLM.from_pretrained(str(model_path), local_files_only=True, trust_remote_code=False,
                                                        torch_dtype=torch.float16 if device == "cuda" else torch.float32).to(device).eval()
        self.info = ProviderInfo("research_local_hf", "local-files", model_path.name, "local", "local", "not_applicable")

    def complete_json(self, system, user, max_tokens=2048, temperature=.2):
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        prompt = self.tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True, enable_thinking=False)
        encoded = self.tokenizer(prompt, return_tensors="pt").to(self.device)
        if encoded.input_ids.shape[-1] > 8192:
            raise LLMError("research_context_exceeded", "研究对照输入超出固定窗口")
        with self.torch.inference_mode():
            generated = self.model.generate(**encoded, do_sample=False, max_new_tokens=min(max_tokens, self.max_new_tokens),
                                             temperature=None, top_p=None, top_k=None, pad_token_id=self.tokenizer.eos_token_id)
        text = self.tokenizer.decode(generated[0, encoded.input_ids.shape[-1]:], skip_special_tokens=True)
        decoder = json.JSONDecoder()
        data = None
        for index, char in enumerate(text):
            if char == "{":
                try:
                    candidate, _ = decoder.raw_decode(text[index:])
                except ValueError:
                    continue
                if isinstance(candidate, dict):
                    data = candidate
                    break
        if data is None:
            raise LLMError("research_invalid_json", "本地研究模型没有返回有效JSON")
        return data, CallUsage(prompt_tokens=encoded.input_ids.shape[-1], completion_tokens=generated.shape[-1] - encoded.input_ids.shape[-1], calls=1)

    def ping(self):
        return True, "local pretrained model loaded"


def effective_labels(row, key):
    """Explicit absence may map to corpus 'none'; ambiguity remains abstention."""
    values = list(row.get(key) or [])
    if not values and row.get("valid") and not row.get("ambiguous", True):
        return ["none"]
    return values


def evaluate_existing_prompt(provider, example, evaluate_case, library):
    context = [f"{'A' if speaker == 0 else 'B'}: {text}" for text, speaker in
               zip(example.utterances[:example.target], example.speakers[:example.target], strict=True)]
    probe = (example.utterances[example.target], context, example.labels.get("intent", []),
             example.labels.get("emotion", []), {"id": example.id, "stratum": "ai_assisted"})
    # Existing code creates reply/causal episodes, original reference library, and invokes
    # run_intent + validate_intent. No alternative label mapping or production score invented.
    return evaluate_case(provider, 0, probe, "context", library, patterns=None, cues=False, samples=1)


def aligned_neural_pilot(root, report, all_test, pilot, crypto):
    """Recompute every neural method on the same pilot IDs, not its 400-item summary."""
    from rapport.research.runner import metrics

    comparisons = []
    for experiment in report["experiments"]:
        name = f"{experiment['method']}-{experiment['architecture']}-seed{experiment['seed']}"
        predictions = json.loads(crypto.decrypt((root / f"{name}.predictions.bin").read_bytes()))
        task_metrics = {}
        for task, trained_names in report["labels"].items():
            source_examples = [e for e in all_test if e.labels.get(task)]
            values = predictions["scores"][task]
            if len(source_examples) != len(values):
                raise ValueError("Neural stored predictions do not align with original test IDs")
            scores_by_id = {e.id: v for e, v in zip(source_examples, values, strict=True)}
            selected = [e for e in pilot if e.id in scores_by_id]
            names = sorted(set(trained_names) | {label for e in selected for label in e.labels.get(task, [])})
            truth = [[float(label in e.labels[task]) for label in names] for e in selected]
            scores = [[scores_by_id[e.id][trained_names.index(label)] if label in trained_names else 0 for label in names] for e in selected]
            task_metrics[task] = metrics(truth, scores, names, experiment["thresholds"][task])
        comparisons.append({"method": experiment["method"], "architecture": experiment["architecture"],
                            "seed": experiment["seed"], "samples": len(pilot), "tasks": task_metrics,
                            "macro_f1": sum(m["macro_f1"] for m in task_metrics.values()) / len(task_metrics)})
    return comparisons


def rebase_pilot(data_dir: Path, source_run: str, destination_run: str):
    """Reuse immutable LM predictions against corrected neural artifacts with identical IDs."""
    from rapport.datasets import DatasetStore
    from rapport.research.lifecycle import ResearchRun
    from rapport.security import Crypto

    for ident in (source_run, destination_run):
        if not ident.isalnum():
            raise ValueError("invalid research run")
    root = data_dir / "research" / "runs"
    source = root / source_run
    destination = root / destination_run
    a = json.loads((source / "report.json").read_text(encoding="utf-8"))
    b = json.loads((destination / "report.json").read_text(encoding="utf-8"))
    if a["dataset_id"] != b["dataset_id"] or a["data"]["ids_hash"] != b["data"]["ids_hash"] or a["labels"] != b["labels"]:
        raise ValueError("Cannot compare changed examples or labels")
    guard = ResearchRun(data_dir, f"{destination_run}_pilot_rebase", b["dataset_id"])
    guard.check()
    crypto = Crypto(data_dir / ".key")
    store = DatasetStore(data_dir / "datasets", crypto)
    cfg = b["config"]
    all_examples, _ = prepare(store.records(b["dataset_id"], "offline_review"), visibility=cfg["visibility"],
                              split_protocol=cfg["split_protocol"], max_context=cfg["max_context"],
                              limits={"train": cfg["train_limit"], "validation": cfg["validation_limit"], "test": cfg["test_limit"]})
    payload = (source / "llm-baseline.predictions.bin").read_bytes()
    predictions = json.loads(crypto.decrypt(payload))
    test = [e for e in all_examples if e.split == "test"]
    lookup = {e.id: e for e in test}
    pilot = [lookup[p["id"]] for p in predictions]
    report = json.loads((source / "llm-baseline.json").read_text(encoding="utf-8"))
    from rapport.research.runner import metrics

    for task, key in (("emotion", "emotions"), ("intent", "tags")):
        names = sorted(set(b["labels"][task]) | {v for e in pilot for v in e.labels.get(task, [])})
        truth = [[float(v in e.labels.get(task, [])) for v in names] for e in pilot]
        scores = [[float(v in effective_labels(row, key)) for v in names] for row in predictions]
        report["tasks"][task] = metrics(truth, scores, names)
    report["macro_f1"] = sum(m["macro_f1"] for m in report["tasks"].values()) / len(report["tasks"])
    report["none_policy"] = "valid explicit empty and non-ambiguous maps to none; ambiguous stays abstention, not 不确定"
    report["abstention_rate"] = sum(row.get("ambiguous", True) for row in predictions) / max(1, len(predictions))
    report["training_condition"] = "zero-shot local LM with existing prompt; supervised neural heads saw 1200 training examples"
    report["includes_product_evidence_gates"] = True
    report["aligned_neural_pilot"] = aligned_neural_pilot(destination, b, test, pilot, crypto)
    report["neural_run_id"] = destination_run
    report["lm_prediction_source_run"] = source_run
    guard.check()
    (destination / "llm-baseline.predictions.bin").write_bytes(payload)
    (destination / "llm-baseline.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    guard.finish("completed")
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--device", choices=["cpu", "cuda"], default="cpu")
    parser.add_argument("--limit", type=int, default=0, help="0 uses exactly all test IDs in the compared research run")
    parser.add_argument("--max-new-tokens", type=int, default=384)
    args = parser.parse_args()
    import torch

    from rapport.datasets import DatasetStore
    from rapport.knowledge.reference import load_library
    from rapport.learning.gpu import GPULease
    from rapport.research.data import digest
    from rapport.research.lifecycle import ResearchRun
    from rapport.research.runner import _weights_hash, metrics
    from rapport.security import Crypto

    if not args.run_id.isalnum():
        parser.error("invalid run id")
    torch.set_num_threads(2)
    root = args.data_dir / "research" / "runs" / args.run_id
    report = json.loads((root / "report.json").read_text(encoding="utf-8"))
    cfg = report["config"]
    if cfg["visibility"] != "causal":
        parser.error("Existing reply prompt comparison requires causal visibility")
    crypto = Crypto(args.data_dir / ".key")
    store = DatasetStore(args.data_dir / "datasets", crypto)
    examples, _ = prepare(store.records(report["dataset_id"], "offline_review"), visibility="causal",
                           split_protocol=cfg["split_protocol"], max_context=cfg["max_context"],
                           limits={"train": cfg["train_limit"], "validation": cfg["validation_limit"], "test": cfg["test_limit"]})
    all_test = [e for e in examples if e.split == "test"]
    test = list(all_test)
    if args.limit:
        test = test[:args.limit]
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
    from intent_eval import evaluate_case

    lease = GPULease(args.data_dir, f"research-llm:{args.run_id}") if args.device == "cuda" else None
    results = []
    latencies = []
    guard = ResearchRun(args.data_dir, f"{args.run_id}_llm", report["dataset_id"])
    status = "failed"
    try:
        if lease:
            lease.__enter__()
        provider = LocalHFProvider(args.model, args.device, args.max_new_tokens)
        library = load_library()
        for i, example in enumerate(test):
            guard.check()
            start = time.perf_counter()
            try:
                row = evaluate_existing_prompt(provider, example, evaluate_case, library)
                row["valid"] = bool(row.get("json_ok"))
            except Exception as exc:
                row = {"id": example.id, "emotions": [], "tags": [], "raw_tags": [], "valid": False, "error_type": type(exc).__name__}
            latencies.append(time.perf_counter() - start)
            results.append(row)
            guard.check()
            (root / "llm-baseline.predictions.bin").write_bytes(crypto.encrypt(json.dumps(results, ensure_ascii=False).encode()))
            print(json.dumps({"stage": "llm_baseline", "done": i + 1, "total": len(test), "valid": row["valid"]}), flush=True)
        task_metrics = {}
        for task, prediction_key in (("emotion", "emotions"), ("intent", "tags")):
            names = sorted(set(report["labels"][task]) | {v for e in test for v in e.labels.get(task, [])})
            truth = [[float(v in e.labels.get(task, [])) for v in names] for e in test]
            scores = [[float(v in effective_labels(r, prediction_key)) for v in names] for r in results]
            task_metrics[task] = metrics(truth, scores, names)
        result = {"method": "existing_intent_prompt_local_small_lm", "model": args.model.name,
                  "model_sha256": _weights_hash(args.model), "max_new_tokens": args.max_new_tokens,
                  "input_visibility": "causal", "samples": len(test), "same_full_test_set": not args.limit,
                  "test_ids_hash": digest([e.id for e in test]), "tasks": task_metrics,
                  "macro_f1": sum(m["macro_f1"] for m in task_metrics.values()) / len(task_metrics),
                  "format_failure_rate": sum(not r["valid"] for r in results) / max(1, len(results)),
                  "abstention_rate": sum(r.get("ambiguous", True) for r in results) / max(1, len(results)),
                  "none_policy": "valid explicit empty and non-ambiguous maps to none; ambiguous stays abstention, not 不确定",
                  "latency_seconds_p95": sorted(latencies)[max(0, math.ceil(.95 * len(latencies)) - 1)],
                  "prompt_sha256": hashlib.sha256(__import__("rapport.llm.intent", fromlist=["INTENT_SYSTEM"]).INTENT_SYSTEM.encode()).hexdigest(),
                  "is_original_cloud_model": False, "production_enabled": False,
                  "comparison_caveat": "分布外的小语言模型+现有提示，并非原云端供应商的复测；子集需与同ID神经预测比较"}
        result["aligned_neural_pilot"] = aligned_neural_pilot(root, report, all_test, test, crypto)
        guard.check()
        (root / "llm-baseline.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        status = "completed"
    finally:
        if lease:
            lease.__exit__(None, None, None)
        guard.finish(status)


if __name__ == "__main__":
    main()
