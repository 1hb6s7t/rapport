"""Offline fusion experiment: does adding a trained head's probabilities to the grounded layer help?

    python tools/fuse_heads_experiment.py --preds output/grounded-preds-validation.json \
        --heads output/context-heads-compare-validation-head-preds.json

Inputs are the per-sample dumps of `evaluate_grounded.py --dump-predictions` (with `ensemble_scores`)
and `compare_context_heads.py` (head probabilities). For each head and weight w the fused score is
(1 - w) * ensemble_scores + w * head_probs (head probs renormalized to sum 1), decided with the
production neutral threshold (argmax below it -> 无明显情绪; plain-text neutral rows stay neutral).
The negative two-signal guard is not replicated here, so treat the numbers as an upper-bound sketch
of what integrating the head could buy, measured on the validation split only.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

FIXED_EMOTIONS = ("不满", "伤心", "开心", "担忧", "亲近", "混合", "无明显情绪", "无奈", "惊讶困惑", "不确定")
NEUTRAL = "无明显情绪"


def _prf(truth, preds, labels):
    per = {}
    for lab in labels:
        tp = sum(1 for t, p in zip(truth, preds, strict=True) if t == lab and p == lab)
        fp = sum(1 for t, p in zip(truth, preds, strict=True) if t != lab and p == lab)
        fn = sum(1 for t, p in zip(truth, preds, strict=True) if t == lab and p != lab)
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        per[lab] = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    macro = sum(per.values()) / len(per)
    acc = sum(1 for t, p in zip(truth, preds, strict=True) if t == p) / max(1, len(truth))
    return round(macro, 4), round(acc, 4)


def _decide(scores: dict[str, float], neutral_text: bool, threshold: float) -> str:
    if neutral_text or not scores:
        return NEUTRAL
    top = max(scores, key=scores.get)
    return NEUTRAL if scores[top] < threshold else top


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preds", required=True)
    parser.add_argument("--heads", required=True)
    parser.add_argument("--threshold", type=float, default=0.35)
    parser.add_argument("--out", default="output/fuse-heads-experiment.json")
    args = parser.parse_args()

    preds = json.loads((ROOT / args.preds).read_text(encoding="utf-8"))
    heads = json.loads((ROOT / args.heads).read_text(encoding="utf-8"))
    ids = [i for i in preds if "ensemble_scores" in preds[i] and all(i in h for h in heads.values())]
    truth = [preds[i]["truth"] for i in ids]
    rows = [f"# 头部融合实验（validation，{len(ids)} 行，阈值 {args.threshold}）", "",
            "| 方案 | macro-F1 | accuracy |", "| --- | --- | --- |"]
    results = {}
    base = [_decide(preds[i]["ensemble_scores"], bool(preds[i].get("neutral_text")), args.threshold) for i in ids]
    results["ensemble_rule_only"] = _prf(truth, base, FIXED_EMOTIONS)
    results["ensemble_production_labels"] = _prf(truth, [preds[i]["ensemble"] for i in ids], FIXED_EMOTIONS)
    rows.append(f"| 融合层（生产标签） | {results['ensemble_production_labels'][0]} | {results['ensemble_production_labels'][1]} |")
    rows.append(f"| 融合层（本脚本简化规则） | {results['ensemble_rule_only'][0]} | {results['ensemble_rule_only'][1]} |")
    for name, table in heads.items():
        alone = [table[i]["label"] for i in ids]
        results[f"{name}_alone"] = _prf(truth, alone, FIXED_EMOTIONS)
        rows.append(f"| {name} 单独 argmax | {results[f'{name}_alone'][0]} | {results[f'{name}_alone'][1]} |")
        for w in (0.2, 0.3, 0.4, 0.5, 0.6):
            fused = []
            for i in ids:
                probs = table[i]["probs"]
                total = sum(probs.values()) or 1.0
                head_dist = {k: v / total for k, v in probs.items()}
                ens = preds[i]["ensemble_scores"]
                labels = set(ens) | set(head_dist)
                scores = {lab: (1 - w) * ens.get(lab, 0.0) + w * head_dist.get(lab, 0.0) for lab in labels}
                fused.append(_decide(scores, bool(preds[i].get("neutral_text")), args.threshold))
            results[f"{name}_w{w}"] = _prf(truth, fused, FIXED_EMOTIONS)
            rows.append(f"| 融合层 + {name}（w={w}） | {results[f'{name}_w{w}'][0]} | {results[f'{name}_w{w}'][1]} |")
    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"n": len(ids), "threshold": args.threshold, "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    out.with_suffix(".md").write_text("\n".join(rows), encoding="utf-8")
    print("\n".join(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
