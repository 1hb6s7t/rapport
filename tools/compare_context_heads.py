"""Compare the research contextual heads with the grounded layer on the SAME rows and metric.

Two steps, each in the venv that can run it:

  # 1. backend venv: per-sample predictions of the offline layer (and its single signals)
  python tools/evaluate_grounded.py --data-dir desktop-data --dataset-id <id> --split validation --limit 0 \
      --dump-predictions output/grounded-preds-validation.json

  # 2. train venv (torch): load the trained heads from a research run and score them on those rows
  python tools/compare_context_heads.py --data-dir desktop-data --dataset-id <id> --run latest \
      --encoder <bge weights dir> --preds output/grounded-preds-validation.json --split validation

The research runner's own metric averages emotion+intent multilabel F1, which is not comparable to
the grounded layer's single-label emotion macro-F1. This script applies one metric (argmax emotion,
macro-F1 over the fixed label set) to every method on the intersection of rows, and writes the head
predictions so a later fusion experiment can reuse them without torch.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

FIXED_EMOTIONS = ("不满", "伤心", "开心", "担忧", "亲近", "混合", "无明显情绪", "无奈", "惊讶困惑", "不确定")


def _prf(truth: list[str], preds: list[str], labels: list[str]) -> dict:
    per = {}
    for lab in labels:
        tp = sum(1 for t, p in zip(truth, preds, strict=True) if t == lab and p == lab)
        fp = sum(1 for t, p in zip(truth, preds, strict=True) if t != lab and p == lab)
        fn = sum(1 for t, p in zip(truth, preds, strict=True) if t == lab and p != lab)
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        per[lab] = {"precision": round(prec, 4), "recall": round(rec, 4), "f1": round(f1, 4), "support": tp + fn}
    macro = sum(v["f1"] for v in per.values()) / len(per) if per else 0.0
    acc = sum(1 for t, p in zip(truth, preds, strict=True) if t == p) / max(len(truth), 1)
    return {"macro_f1": round(macro, 4), "accuracy": round(acc, 4), "per_class": per}


def _latest_run(runs_dir: Path) -> Path:
    candidates = sorted(runs_dir.glob("*/report.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        raise SystemExit(f"no research runs under {runs_dir}")
    return candidates[0].parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--run", default="latest")
    parser.add_argument("--encoder", required=True)
    parser.add_argument("--preds", required=True)
    parser.add_argument("--split", default="validation")
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    data_dir = Path(args.data_dir).resolve()
    os.environ.setdefault("RAPPORT_DATA_DIR", str(data_dir))
    import torch
    from rapport.datasets import DatasetStore
    from rapport.research.data import prepare
    from rapport.research.models import MultiTask
    from rapport.research.runner import collate, feature_cache
    from rapport.security import Crypto

    crypto = Crypto(data_dir / ".key")
    store = DatasetStore(data_dir / "datasets", crypto)
    runs_dir = data_dir / "research" / "runs"
    run_dir = _latest_run(runs_dir) if args.run == "latest" else runs_dir / args.run
    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    cfg = report["config"]
    labels = report["labels"]

    examples, _ = prepare(
        store.records(args.dataset_id, "offline_review"),
        visibility=cfg["visibility"], split_protocol=cfg["split_protocol"], max_context=cfg["max_context"],
    )
    preds = json.loads((ROOT / args.preds).read_text(encoding="utf-8"))
    selected = [e for e in examples if e.split == args.split and e.id in preds]
    if not selected:
        raise SystemExit("no overlapping rows between the research examples and the dumped predictions")
    device = "cuda" if args.device == "cuda" and torch.cuda.is_available() else "cpu"
    vectors, _ = feature_cache(examples, Path(args.encoder), data_dir, crypto, device)
    width = next(iter(vectors.values())).numel()

    truth = [preds[e.id]["truth"] for e in selected]
    results: dict[str, dict] = {}
    for method in ("majority", "structbert_calibrated", "bge_knn", "ensemble"):
        if all(method in preds[e.id] for e in selected):
            results[method] = _prf(truth, [preds[e.id][method] for e in selected], list(FIXED_EMOTIONS))

    head_predictions: dict[str, dict] = {}
    for experiment in report.get("experiments", []):
        artifact = experiment.get("artifact_file")
        if not artifact or not (run_dir / artifact).is_file():
            continue
        model = MultiTask(
            experiment["method"], experiment["architecture"], width, cfg["hidden"],
            {k: len(v) for k, v in labels.items()}, cfg["visibility"] == "causal",
        ).to(device)
        state = torch.load(io.BytesIO(crypto.decrypt((run_dir / artifact).read_bytes())), map_location=device, weights_only=True)
        model.load_state_dict(state)
        model.eval()
        probs_out: list[dict[str, float]] = []
        with torch.no_grad():
            for start in range(0, len(selected), 32):
                batch = selected[start : start + 32]
                x, s, lengths, targets, _yy, _masks = collate(batch, vectors, labels, device)
                logits = model(x, s, lengths, targets)["emotion"].sigmoid().cpu()
                for row in logits:
                    probs_out.append({labels["emotion"][j]: round(float(row[j]), 4) for j in range(len(labels["emotion"]))})
        pred_labels = [max(p, key=p.get) for p in probs_out]
        name = f"head_{experiment['method']}"
        results[name] = _prf(truth, pred_labels, list(FIXED_EMOTIONS))
        results[name]["runner_validation_macro_f1"] = experiment.get("validation", {}).get("macro_f1")
        head_predictions[name] = {e.id: {"label": lab, "probs": p} for e, lab, p in zip(selected, pred_labels, probs_out, strict=True)}

    out = ROOT / (args.out or f"output/context-heads-compare-{args.split}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "run_id": run_dir.name,
        "dataset_id": args.dataset_id,
        "split": args.split,
        "n": len(selected),
        "config": {k: cfg.get(k) for k in ("hidden", "max_context", "visibility", "split_protocol", "epochs", "batch_size")},
        "label_source": "ai_assisted（弱标注，非人工金标）",
        "results": results,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (out.with_name(out.stem + "-head-preds.json")).write_text(json.dumps(head_predictions, ensure_ascii=False), encoding="utf-8")

    lines = [f"# 上下文头 vs 离线研究层（{args.split}，同 {len(selected)} 行，单标签 argmax macro-F1）", "",
             "| 方法 | macro-F1 | accuracy |", "| --- | --- | --- |"]
    for name, metrics in results.items():
        lines.append(f"| {name} | {metrics['macro_f1']} | {metrics['accuracy']} |")
    (out.with_suffix(".md")).write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
