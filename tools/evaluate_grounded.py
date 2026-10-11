"""Honest offline evaluation of the rules-mode offline layer on the dataset *test* split.

    python tools/evaluate_grounded.py --data-dir desktop-data --dataset-id <id> [--limit 4000]

Compares, on the same rows, each signal alone and the fused classifier:

    majority | structbert (raw) | structbert (calibrated) | bge-knn | ensemble (grounded)

and reports macro-F1, accuracy, per-class precision/recall/F1, the ensemble's coverage /
abstain rate, and ECE for the raw vs calibrated model. Writes JSON + a Markdown summary.

Read the numbers with the label source in mind: the labels are AI-assisted (`ai_assisted_high`),
so this measures *agreement with weak labels*, not accuracy against independent labels.
With --gold the rows are scored against the Claude-annotated reference labels in app.db
(`claude_reference`, independent of the weak labels — still not human gold). The anchors come
from the train split, so the test rows were never retrieved into the index.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
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
    return round(out, 4)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--pool", default="offline_review")
    parser.add_argument("--limit", type=int, default=4000)
    parser.add_argument("--split", default="test", choices=["test", "validation", "train"])
    parser.add_argument("--weights", default="", help="override fusion weights: struct,knn,centroid")
    parser.add_argument("--neutral", type=float, default=0.0, help="override NEUTRAL_THRESHOLD")
    parser.add_argument("--margin", type=float, default=0.0, help="override MARGIN_THRESHOLD")
    parser.add_argument("--gold", action="store_true", help="score against the Claude-annotated reference labels stored in app.db (not human gold)")
    parser.add_argument("--dump-predictions", default="", help="write per-sample predictions of every method to this JSON")
    parser.add_argument("--out", default="output/grounded-eval.json")
    args = parser.parse_args()

    data_dir = Path(args.data_dir).resolve()
    os.environ.setdefault("RAPPORT_DATA_DIR", str(data_dir))

    from rapport.datasets import DatasetStore
    from rapport.emotion.model import load_model
    from rapport.grounded import classify as gc
    from rapport.grounded.anchors import load_index
    from rapport.grounded.calibrate import apply_temperature, calibration_status
    from rapport.grounded.classify import GroundedClassifier, _argmax, _to_fixed
    from rapport.security import Crypto

    if args.weights:
        s, k, c = (float(x) for x in args.weights.split(","))
        gc.WEIGHTS.update({"structbert": s, "bge_knn": k, "centroid": c})
    if args.neutral:
        gc.NEUTRAL_THRESHOLD = args.neutral
    if args.margin:
        gc.MARGIN_THRESHOLD = args.margin

    store = DatasetStore(data_dir / "datasets", Crypto(data_dir / ".key"))
    rows = store.records(args.dataset_id, args.pool)

    train_counts = Counter(
        (r.get("labels") or {}).get("emotion", [None])[0]
        for r in rows
        if r.get("split") == "train" and r.get("training_eligible")
    )
    majority = train_counts.most_common(1)[0][0] if train_counts else "无明显情绪"

    gold: dict[str, str] = {}
    if args.gold:
        import sqlite3

        con = sqlite3.connect(f"file:{(data_dir / 'app.db').as_posix()}?mode=ro", uri=True)
        try:
            gold = {
                sid: emo
                for sid, emo, skipped in con.execute(
                    "SELECT sample_id, emotion, skipped FROM gold_labels WHERE dataset_id = ?", (args.dataset_id,)
                )
                if emo and not skipped
            }
        finally:
            con.close()
        if not gold:
            print("no reference labels stored for this dataset yet")
            return 4

    texts: list[str] = []
    truth: list[str] = []
    ids: list[str] = []
    for row in rows:
        sample_id = str(row.get("id"))
        if args.gold:
            if sample_id not in gold:
                continue
        elif row.get("split") != args.split or not row.get("training_eligible"):
            continue
        text = (row.get("text") or "").strip()
        labels = (row.get("labels") or {}).get("emotion") or []
        if args.gold:
            if len(text) < 2:
                continue
            label = gold[sample_id]
        else:
            if len(text) < 2 or len(labels) != 1:
                continue
            label = labels[0]
        texts.append(text)
        truth.append(label)
        ids.append(sample_id)
        if args.limit and len(texts) >= args.limit:
            break
    if len(texts) < (20 if args.gold else 50):
        print(f"not enough rows ({len(texts)})")
        return 3

    labels = list(FIXED_EMOTIONS)
    model = load_model()
    index = load_index()
    classifier = GroundedClassifier.build(use_markers=False)

    predictions: dict[str, list[str]] = {"majority": [majority] * len(texts)}
    ece: dict[str, float] = {}

    if model is not None:
        preds = model.predict(texts)
        raw_dists = [_to_fixed(p.probs) for p in preds]
        predictions["structbert_raw"] = [_argmax(d)[0] or "不确定" for d in raw_dists]
        cal_dists = [apply_temperature(d) for d in raw_dists]
        predictions["structbert_calibrated"] = [_argmax(d)[0] or "不确定" for d in cal_dists]
        fixed = set(FIXED_EMOTIONS)
        keep = [i for i, t in enumerate(truth) if t in fixed]
        ece["structbert_raw"] = _ece([max(raw_dists[i].values()) for i in keep], [predictions["structbert_raw"][i] == truth[i] for i in keep])
        ece["structbert_calibrated"] = _ece([max(cal_dists[i].values()) for i in keep], [predictions["structbert_calibrated"][i] == truth[i] for i in keep])

    if index is not None and classifier is not None and classifier.encoder is not None:
        vectors = classifier.encoder.encode(texts)
        predictions["bge_knn"] = [_argmax(index.knn_scores(v))[0] or "不确定" for v in vectors]

    ensemble_results = []
    if classifier is not None:
        ensemble_results = classifier.classify(texts)
        predictions["ensemble"] = [r.emotion for r in ensemble_results]

    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dataset_id": args.dataset_id,
        "pool": args.pool,
        "split": "claude_reference" if args.gold else args.split,
        "n": len(texts),
        "label_source": "claude_reference（Claude 独立标注，非人工金标）" if args.gold else "ai_assisted（弱标注，非人工金标）",
        "weights": dict(gc.WEIGHTS),
        "neutral_threshold": gc.NEUTRAL_THRESHOLD,
        "margin_threshold": gc.MARGIN_THRESHOLD,
        "calibration": calibration_status(),
        "anchors": index.status() if index is not None else None,
        "results": {},
        "ece": ece,
    }
    for name, preds in predictions.items():
        metrics = _prf(truth, preds, labels)
        if name == "ensemble":
            abstained = sum(1 for p in preds if p == "不确定")
            metrics["coverage"] = round(1 - abstained / len(preds), 4)
            metrics["abstain_rate"] = round(abstained / len(preds), 4)
        report["results"][name] = metrics

    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    source_label = "claude_reference（Claude 独立标注）" if args.gold else args.split
    source_note = (
        "标签为 Claude 独立标注（独立于弱标注，非人工金标）；下列数字是「与独立参考标注的一致度」。"
        if args.gold
        else "标签为 AI 辅助弱标注，下列数字是「与弱标注的一致度」，不是相对人工金标的准确率。"
    )
    lines = [
        f"# 离线研究层评测（{args.dataset_id} · {args.pool} · {source_label} 前 {len(texts)} 条）",
        "",
        source_note,
        "",
        "| 方法 | macro-F1 | accuracy | 覆盖率 | 拒判率 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for name, metrics in report["results"].items():
        lines.append(
            f"| {name} | {metrics['macro_f1']} | {metrics['accuracy']} | "
            f"{metrics.get('coverage', '—')} | {metrics.get('abstain_rate', '—')} |"
        )
    lines += ["", f"ECE（原始 / 校准后）：{ece.get('structbert_raw', '—')} / {ece.get('structbert_calibrated', '—')}", ""]
    (out.with_suffix(".md")).write_text("\n".join(lines), encoding="utf-8")
    if args.dump_predictions:
        dump = ROOT / args.dump_predictions
        dump.parent.mkdir(parents=True, exist_ok=True)
        per_sample = {
            sample_id: {
                "truth": truth[i],
                **{name: preds[i] for name, preds in predictions.items()},
                **({"ensemble_scores": ensemble_results[i].scores, "neutral_text": ensemble_results[i].neutral_reason is not None
                    and ensemble_results[i].scores and max(ensemble_results[i].scores.values()) >= gc.NEUTRAL_THRESHOLD}
                   if ensemble_results else {}),
            }
            for i, sample_id in enumerate(ids)
        }
        dump.write_text(json.dumps(per_sample, ensure_ascii=False), encoding="utf-8")
        print(f"predictions -> {dump}")
    print("\n".join(lines))
    print(f"\nwrote {out} and {out.with_suffix('.md')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
