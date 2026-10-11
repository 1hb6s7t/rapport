"""Validation-only temperature fitting and held-out probability/rejection evaluation."""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path


def fit_temperature(truth, probabilities):
    """One bounded positive temperature per task, minimizing unweighted binary NLL.

    Deterministic log grid with validation-only refinement avoids optimizer dependencies.
    This is a multilabel sigmoid adaptation of scalar temperature scaling.
    """
    import torch

    y = torch.tensor(truth, dtype=torch.float64)
    p = torch.tensor(probabilities, dtype=torch.float64).clamp(1e-8, 1 - 1e-8)
    logits = torch.logit(p)
    grid = torch.logspace(-1, 1, 81, dtype=torch.float64)

    def losses(candidates):
        z = logits[None] / candidates[:, None, None]
        return torch.nn.functional.binary_cross_entropy_with_logits(z, y.expand_as(z), reduction="none").mean((1, 2))

    best = int(losses(grid).argmin())
    low = float(grid[max(0, best - 1)])
    high = float(grid[min(len(grid) - 1, best + 1)])
    fine = torch.linspace(low, high, 81, dtype=torch.float64)
    optimum = int(losses(fine).argmin())
    return {"temperature": float(fine[optimum]), "validation_binary_nll": float(losses(fine)[optimum]),
            "fit_samples": len(truth), "fit_labels": y.shape[1], "fit_split": "validation", "bounds": [.1, 10.],
            "objective": "unweighted_multilabel_binary_nll"}


def apply_temperature(probabilities, temperature):
    import torch

    p = torch.tensor(probabilities, dtype=torch.float64).clamp(1e-8, 1 - 1e-8)
    return (torch.logit(p) / temperature).sigmoid().tolist()


def probability_metrics(truth, probabilities):
    import torch

    y = torch.tensor(truth, dtype=torch.float64)
    p = torch.tensor(probabilities, dtype=torch.float64).clamp(1e-8, 1 - 1e-8)
    ece = 0.
    bins = []
    # Standard labelwise reliability: probability versus observed positive frequency,
    # not correctness of a thresholded decision.
    for index in range(10):
        mask = (p >= index / 10) & (p < (index + 1) / 10 if index < 9 else p <= 1)
        count = int(mask.sum())
        if count:
            confidence, observed = float(p[mask].mean()), float(y[mask].mean())
            ece += count / p.numel() * abs(confidence - observed)
            bins.append({"lower": index / 10, "upper": (index + 1) / 10, "count": count,
                         "mean_probability": confidence, "positive_frequency": observed})
    per_label = []
    for column in range(p.shape[1]):
        error = 0.
        for index in range(10):
            mask = (p[:, column] >= index / 10) & (p[:, column] < (index + 1) / 10 if index < 9 else p[:, column] <= 1)
            if mask.any():
                error += float(mask.float().mean()) * abs(float(p[mask, column].mean()) - float(y[mask, column].mean()))
        per_label.append(error)
    return {"labelwise_ece": ece, "macro_label_ece": sum(per_label) / len(per_label),
            "brier": float((p - y).square().mean()), "binary_nll": float(torch.nn.functional.binary_cross_entropy(p, y)),
            "reliability_bins": bins, "samples": y.shape[0], "labels": y.shape[1]}


def rejection_curve(truth, probabilities):
    """Fixed confidence cutoffs; top-1-in-true-set error, NOT full multilabel accuracy."""
    import torch

    y = torch.tensor(truth).bool()
    p = torch.tensor(probabilities)
    confidence, labels = p.max(-1)
    correct = y[torch.arange(len(y)), labels]
    points = []
    for threshold in (0., .5, .6, .7, .8, .9, .95):
        accepted = confidence >= threshold
        n = int(accepted.sum())
        points.append({"threshold": threshold, "coverage": n / len(y), "accepted": n,
                       "top1_set_error": 1 - float(correct[accepted].float().mean()) if n else None})
    return {"metric": "highest_probability_label_membership_in_true_set", "full_multilabel_correctness": False,
            "thresholds_selected_on_test": False, "points": points}


def run(data_dir: Path, run_ids: list[str]):
    import torch

    from rapport.datasets import DatasetStore
    from rapport.research.data import prepare
    from rapport.research.lifecycle import ResearchRun
    from rapport.research.models import MultiTask
    from rapport.research.runner import evaluate, feature_cache
    from rapport.security import Crypto

    torch.set_num_threads(2)
    crypto = Crypto(data_dir / ".key")
    all_results = []
    for ident in run_ids:
        if not ident.isalnum():
            raise ValueError("Invalid research run id")
        root = data_dir / "research" / "runs" / ident
        report = json.loads((root / "report.json").read_text(encoding="utf-8"))
        guard = ResearchRun(data_dir, f"{ident}_calibration", report["dataset_id"])
        status = "failed"
        try:
            cfg = report["config"]
            rows = DatasetStore(data_dir / "datasets", crypto).records(report["dataset_id"], "offline_review")
            examples, manifest = prepare(rows, visibility=cfg["visibility"], split_protocol=cfg["split_protocol"],
                                         max_context=cfg["max_context"], limits={"train": cfg["train_limit"], "validation": cfg["validation_limit"], "test": cfg["test_limit"]})
            if manifest["ids_hash"] != report["data"]["ids_hash"]:
                raise ValueError("Cannot calibrate changed test IDs")
            guard.check()
            vectors, _ = feature_cache(examples, Path(cfg["encoder"]), data_dir, crypto, "cpu", checkpoint=guard.check)
            labels = report["labels"]
            valid = [e for e in examples if e.split == "validation"]
            test = [e for e in examples if e.split == "test"]
            candidates = []
            for experiment in report["experiments"]:
                if experiment["method"] != "dag_erc":
                    continue
                guard.check()
                model = MultiTask("dag_erc", experiment["architecture"], next(iter(vectors.values())).numel(), cfg["hidden"],
                                  {k: len(v) for k, v in labels.items()}, causal=cfg["visibility"] == "causal")
                blob = crypto.decrypt((root / experiment["artifact_file"]).read_bytes())
                model.load_state_dict(torch.load(io.BytesIO(blob), map_location="cpu", weights_only=True))
                _, validation_truth, validation_scores = evaluate(model, valid, vectors, labels, "cpu")
                _, test_truth, test_scores = evaluate(model, test, vectors, labels, "cpu")
                tasks = {}
                private = {}
                for task in labels:
                    fit = fit_temperature(validation_truth[task], validation_scores[task])
                    calibrated = apply_temperature(test_scores[task], fit["temperature"])
                    before = probability_metrics(test_truth[task], test_scores[task])
                    after = probability_metrics(test_truth[task], calibrated)
                    tasks[task] = {"fit": fit, "test_before": before, "test_after": after,
                                   "ece_improved": after["labelwise_ece"] < before["labelwise_ece"],
                                   "brier_improved": after["brier"] < before["brier"],
                                   "rejection_before": rejection_curve(test_truth[task], test_scores[task]),
                                   "rejection_after": rejection_curve(test_truth[task], calibrated),
                                   "unseen_test_labels": experiment.get("unknown_test_labels", {}).get(task, {})}
                    private[task] = {"truth": test_truth[task], "before": test_scores[task], "after": calibrated}
                result = {"run_id": ident, "method": "dag_erc", "architecture": experiment["architecture"], "seed": experiment["seed"],
                          "tasks": tasks, "test_samples": len(test), "validation_samples": len(valid), "ids_hash": manifest["ids_hash"],
                          "classification_thresholds_changed": False, "production_enabled": False,
                          "limitations": ["开发AI辅助标签不是人工校准金标准", "单标量温度只校准既有标签概率，不创造未见类别能力",
                                          "未按测试结果选择温度或拒判阈值", "只有两个测试来源组，校准不能据此推广全体用户"]}
                guard.check()
                name = f"dag_erc-{experiment['architecture']}-seed{experiment['seed']}"
                (root / f"{name}.calibration.bin").write_bytes(crypto.encrypt(json.dumps(private).encode()))
                candidates.append(result)
                print(json.dumps({"stage": "calibration", "run": ident, "architecture": experiment["architecture"]}), flush=True)
            guard.check()
            (root / "calibration.json").write_text(json.dumps({"results": candidates}, ensure_ascii=False, indent=2), encoding="utf-8")
            all_results.extend(candidates)
            status = "completed"
        finally:
            guard.finish(status)
    return all_results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--run-ids", required=True)
    args = parser.parse_args()
    results = run(args.data_dir, args.run_ids.split(","))
    summary = []
    for architecture in ("shared", "separate"):
        group = [r for r in results if r["architecture"] == architecture]
        for task in ("emotion", "intent"):
            for key in ("labelwise_ece", "brier", "binary_nll"):
                summary.append({"architecture": architecture, "task": task, "metric": key,
                                "before_mean": sum(r["tasks"][task]["test_before"][key] for r in group) / len(group),
                                "after_mean": sum(r["tasks"][task]["test_after"][key] for r in group) / len(group),
                                "seeds": len(group)})
    print(json.dumps({"summary": summary}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
