"""Would running the local 8B only on messages the fusion layer flags as emotional be enough?

    python tools/experiment_gated_hybrid.py

Qwen3-8B zero-shot beats the hand-built fusion layer on the 400 Claude reference labels, but it is a
full local inference per message. The fusion layer is cheap and, on this data, is the better *neutral*
detector (it is right on 74% of the messages it calls 无明显情绪, against 68% for the 8B). So the
interesting design is a gate: keep the fusion verdict where it says 无明显情绪, and pay for the 8B
only where it says the message carries emotion.

This reads two prediction dumps keyed by sample id -- the shipped fusion layer at PRIOR_ALPHA=1.0
(``tools/check_transfer.py --dump-preds`` writes it in that shape after re-keying) and
``tools/evaluate_local_llm.py``'s 8B run -- joins them on id, and scores the gate against both
baselines with paired bootstrap intervals and an exact McNemar test.

Caveat the numbers cannot remove: the reference labels are Claude's own and the 8B is also an LLM, so
part of its edge may be same-source agreement rather than accuracy. And the gate's *direction* is
chosen on these same 400 rows, so the per-bucket accuracies are in-sample.
"""
from __future__ import annotations

import argparse
import json
import random
from collections import Counter
from math import comb
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXED = ("不满", "伤心", "开心", "担忧", "亲近", "混合", "无明显情绪", "无奈", "惊讶困惑", "不确定")
NEUTRAL = "无明显情绪"


def macro_f1(truth, pred, labels=FIXED):
    fs = []
    for lab in labels:
        tp = sum(1 for t, p in zip(truth, pred) if t == lab and p == lab)
        fp = sum(1 for t, p in zip(truth, pred) if t != lab and p == lab)
        fn = sum(1 for t, p in zip(truth, pred) if t == lab and p != lab)
        pr = tp / (tp + fp) if tp + fp else 0.0
        rc = tp / (tp + fn) if tp + fn else 0.0
        fs.append(2 * pr * rc / (pr + rc) if pr + rc else 0.0)
    return sum(fs) / len(fs)


def accuracy(truth, pred):
    return sum(1 for t, p in zip(truth, pred) if t == p) / len(pred)


def per_class_f1(truth, pred, lab):
    tp = sum(1 for t, p in zip(truth, pred) if t == lab and p == lab)
    fp = sum(1 for t, p in zip(truth, pred) if t != lab and p == lab)
    fn = sum(1 for t, p in zip(truth, pred) if t == lab and p != lab)
    pr = tp / (tp + fp) if tp + fp else 0.0
    rc = tp / (tp + fn) if tp + fn else 0.0
    return 2 * pr * rc / (pr + rc) if pr + rc else 0.0


def mcnemar(truth, a, b):
    """Exact two-sided binomial on the discordant pairs."""
    x = sum(1 for t, p, q in zip(truth, a, b) if p == t and q != t)
    y = sum(1 for t, p, q in zip(truth, a, b) if p != t and q == t)
    n = x + y
    if not n:
        return x, y, 1.0
    k = min(x, y)
    return x, y, min(1.0, 2 * sum(comb(n, i) for i in range(k + 1)) / 2 ** n)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fusion-preds", default="output/current-fusion-preds.json")
    ap.add_argument("--llm-preds", default="output/grounded-eval-llm-8b-v2-preds.json")
    ap.add_argument("--bootstrap", type=int, default=4000)
    ap.add_argument("--seed", type=int, default=20261011)
    ap.add_argument("--out", default="output/gated-hybrid.json")
    ap.add_argument("--report", default="output/gated-hybrid.txt")
    args = ap.parse_args()

    fusion = json.loads((ROOT / args.fusion_preds).read_text(encoding="utf-8"))
    llm = json.loads((ROOT / args.llm_preds).read_text(encoding="utf-8"))

    truth_by_id, fusion_pred = fusion["truth"], fusion["pred"]
    ids = [i for i in truth_by_id if i in llm and llm[i]["truth"] == truth_by_id[i]]
    truth = [truth_by_id[i] for i in ids]
    F = [fusion_pred[i] for i in ids]
    L = [llm[i]["pred"] for i in ids]

    lines: list[str] = []

    def emit(s=""):
        print(s, flush=True)
        lines.append(s)

    emit(f"joined on sample id: {len(ids)} rows "
         f"(fusion {len(truth_by_id)}, 8B {len(llm)})")

    a_ix = [i for i, f in enumerate(F) if f == NEUTRAL]
    b_ix = [i for i, f in enumerate(F) if f != NEUTRAL]
    emit("")
    emit(f"fusion says 无明显情绪: n={len(a_ix)} ({100 * len(a_ix) / len(ids):.1f}%)  "
         f"fusion acc {accuracy([truth[i] for i in a_ix], [F[i] for i in a_ix]):.4f}  "
         f"8B acc {accuracy([truth[i] for i in a_ix], [L[i] for i in a_ix]):.4f}")
    emit(f"fusion says emotion:    n={len(b_ix)} ({100 * len(b_ix) / len(ids):.1f}%)  "
         f"fusion acc {accuracy([truth[i] for i in b_ix], [F[i] for i in b_ix]):.4f}  "
         f"8B acc {accuracy([truth[i] for i in b_ix], [L[i] for i in b_ix]):.4f}")

    cands = {
        "fusion only": F,
        "8B only": L,
        "gate: fusion neutral, else 8B": [(F[i] if F[i] == NEUTRAL else L[i]) for i in range(len(ids))],
        "gate reversed": [(L[i] if F[i] == NEUTRAL else F[i]) for i in range(len(ids))],
    }
    emit("")
    emit(f"{'method':<32}{'macro-F1':>10}{'accuracy':>10}{'伤心 F1':>9}")
    for name, p in cands.items():
        emit(f"{name:<32}{macro_f1(truth, p):>10.4f}{accuracy(truth, p):>10.4f}"
             f"{per_class_f1(truth, p, '伤心'):>9.3f}")
    emit(f"gate sends {100 * len(b_ix) / len(ids):.1f}% of messages to the 8B")

    rng = random.Random(args.seed)
    n = len(ids)

    def boot(pa, pb):
        dm, da = [], []
        for _ in range(args.bootstrap):
            ix = [rng.randrange(n) for _ in range(n)]
            t = [truth[i] for i in ix]
            dm.append(macro_f1(t, [pa[i] for i in ix]) - macro_f1(t, [pb[i] for i in ix]))
            da.append(accuracy(t, [pa[i] for i in ix]) - accuracy(t, [pb[i] for i in ix]))
        dm.sort()
        da.sort()
        lo, hi = int(0.025 * args.bootstrap), int(0.975 * args.bootstrap)
        return (dm[lo], dm[hi], sum(1 for x in dm if x <= 0) / args.bootstrap,
                da[lo], da[hi], sum(1 for x in da if x <= 0) / args.bootstrap)

    out = {"rows": len(ids), "gate_share": len(b_ix) / len(ids), "results": {}, "comparisons": {}}
    for name, p in cands.items():
        out["results"][name] = {"macro_f1": round(macro_f1(truth, p), 4),
                                "accuracy": round(accuracy(truth, p), 4),
                                "sad_f1": round(per_class_f1(truth, p, "伤心"), 4)}
    for a, b in (("gate: fusion neutral, else 8B", "fusion only"),
                 ("gate: fusion neutral, else 8B", "8B only"),
                 ("gate reversed", "fusion only")):
        r = boot(cands[a], cands[b])
        x, y, pv = mcnemar(truth, cands[a], cands[b])
        emit("")
        emit(f"{a}  minus  {b}")
        emit(f"  macro  {macro_f1(truth, cands[a]) - macro_f1(truth, cands[b]):+.4f} "
             f"CI [{r[0]:+.4f}, {r[1]:+.4f}]  P(<=0)={r[2]:.3f}")
        emit(f"  acc    {accuracy(truth, cands[a]) - accuracy(truth, cands[b]):+.4f} "
             f"CI [{r[3]:+.4f}, {r[4]:+.4f}]  P(<=0)={r[5]:.3f}")
        emit(f"  McNemar: gate-only {x}, other-only {y}, exact p={pv:.4f}")
        out["comparisons"][f"{a} vs {b}"] = {
            "macro_diff": round(macro_f1(truth, cands[a]) - macro_f1(truth, cands[b]), 4),
            "macro_ci": [round(r[0], 4), round(r[1], 4)],
            "acc_diff": round(accuracy(truth, cands[a]) - accuracy(truth, cands[b]), 4),
            "acc_ci": [round(r[3], 4), round(r[4], 4)],
            "mcnemar": {"gate_only": x, "other_only": y, "p": round(pv, 4)},
        }

    emit("")
    emit(f"fusion predicted: {dict(Counter(F))}")
    emit(f"8B predicted:     {dict(Counter(L))}")
    emit(f"truth:            {dict(Counter(truth))}")

    (ROOT / args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    (ROOT / args.report).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {ROOT / args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
