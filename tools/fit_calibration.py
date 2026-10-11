"""Fit temperature scaling for the bundled emotion model on the dataset validation split.

    python tools/fit_calibration.py --data-dir desktop-data --dataset-id <id>

Writes ``models/calibration-zh-v1.json`` (temperature, ECE before/after, the split it was fitted
on). Runs with the *backend* venv: it only needs numpy + onnxruntime, no torch.

Honest scope: the model has no 无明显情绪 class, so fitting and ECE use only the samples whose
label is inside the model's own output space (the other classes are still predicted and still
scored, they just cannot be calibrated against a target the model cannot produce).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

GRID = [round(0.50 + 0.05 * i, 2) for i in range(int((4.0 - 0.5) / 0.05) + 1)]


def _ece(confidences: list[float], correct: list[bool], bins: int = 10) -> float:
    total = len(confidences)
    if not total:
        return 0.0
    out = 0.0
    for i in range(bins):
        lo, hi = i / bins, (i + 1) / bins
        members = [k for k in range(total) if (lo < confidences[k] <= hi) or (i == 0 and confidences[k] <= hi)]
        if not members:
            continue
        avg_conf = sum(confidences[k] for k in members) / len(members)
        avg_acc = sum(1.0 for k in members if correct[k]) / len(members)
        out += len(members) / total * abs(avg_acc - avg_conf)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--pool", default="offline_review")
    parser.add_argument("--limit", type=int, default=0, help="0 = all validation rows")
    args = parser.parse_args()

    data_dir = Path(args.data_dir).resolve()
    os.environ.setdefault("RAPPORT_DATA_DIR", str(data_dir))

    from rapport.datasets import DatasetStore
    from rapport.emotion.model import RAW_TO_FIXED, load_model
    from rapport.grounded.calibrate import CALIBRATION_FILENAME, calibration_path
    from rapport.security import Crypto

    model = load_model()
    if model is None:
        print("emotion model unavailable")
        return 2

    store = DatasetStore(data_dir / "datasets", Crypto(data_dir / ".key"))
    rows = store.records(args.dataset_id, args.pool)
    fixed_labels = sorted(set(RAW_TO_FIXED.values()))

    samples: list[tuple[str, str]] = []
    for row in rows:
        if row.get("split") != "validation" or not row.get("training_eligible"):
            continue
        text = (row.get("text") or "").strip()
        labels = (row.get("labels") or {}).get("emotion") or []
        if len(text) < 2 or len(labels) != 1 or labels[0] not in fixed_labels:
            continue
        samples.append((text, labels[0]))
        if args.limit and len(samples) >= args.limit:
            break
    if len(samples) < 50:
        print(f"not enough validation samples ({len(samples)})")
        return 3

    texts = [t for t, _ in samples]
    truth = [y for _, y in samples]
    predictions = model.predict(texts)
    dists: list[dict[str, float]] = []
    for pred in predictions:
        dist: dict[str, float] = {}
        for raw, value in pred.probs.items():
            fixed = RAW_TO_FIXED.get(raw)
            if fixed:
                dist[fixed] = dist.get(fixed, 0.0) + float(value)
        total = sum(dist.values()) or 1.0
        dists.append({k: v / total for k, v in dist.items()})

    def nll(temperature: float) -> float:
        power = 1.0 / temperature
        total = 0.0
        for dist, target in zip(dists, truth, strict=True):
            scaled = {k: max(v, 1e-12) ** power for k, v in dist.items()}
            norm = sum(scaled.values()) or 1.0
            total -= math.log(max(scaled.get(target, 1e-12) / norm, 1e-12))
        return total / len(dists)

    best_t = min(GRID, key=nll)

    def top_conf(dists_: list[dict[str, float]], temperature: float) -> tuple[list[float], list[bool]]:
        power = 1.0 / temperature
        confidences, correct = [], []
        for dist, target in zip(dists_, truth, strict=True):
            scaled = {k: max(v, 1e-12) ** power for k, v in dist.items()}
            norm = sum(scaled.values()) or 1.0
            scaled = {k: v / norm for k, v in scaled.items()}
            top = max(scaled, key=scaled.get)
            confidences.append(scaled[top])
            correct.append(top == target)
        return confidences, correct

    conf_before, correct_before = top_conf(dists, 1.0)
    conf_after, correct_after = top_conf(dists, best_t)
    payload = {
        "version": "calibration-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": model.manifest.get("id"),
        "labels": fixed_labels,
        "temperature": best_t,
        "fitted_on": {"dataset_id": args.dataset_id, "pool": args.pool, "split": "validation", "n": len(samples)},
        "label_counts": dict(Counter(truth)),
        "nll_before": round(nll(1.0), 4),
        "nll_after": round(nll(best_t), 4),
        "ece_before": round(_ece(conf_before, correct_before), 4),
        "ece_after": round(_ece(conf_after, correct_after), 4),
        "note": "温度标定：在本项目验证集上拟合，仅覆盖模型可输出的情绪类别（不含无明显情绪）。",
    }
    out = ROOT / "models" / CALIBRATION_FILENAME
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: payload[k] for k in ("temperature", "nll_before", "nll_after", "ece_before", "ece_after")}, ensure_ascii=False))
    print(f"wrote {out} (also looked for {calibration_path()})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
