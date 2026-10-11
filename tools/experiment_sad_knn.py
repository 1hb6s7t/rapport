"""Scratch experiment: can a class-prior correction in the kNN vote recover 伤心?

    python tools/experiment_sad_knn.py [--alphas 0,0.5,0.7,0.8,0.9,1.0] [--out output/experiment-sad-knn.json]

Diagnosis (2026-10-11): the anchor corpus holds 21 伤心 examples against 300 for every other
class, and `AnchorIndex.knn_scores` weights neighbours by sim**3 with no correction for that
imbalance, so a sad message retrieves 无奈 / 无明显情绪 neighbours and the fused score for
伤心 never clears the neutral threshold. The fix under test divides each class's raw kNN mass
by count**alpha before renormalising (alpha=0 is today's behaviour).

Scores both the weak-labelled validation split and the 400 Claude reference labels, reusing the
real `GroundedClassifier._decide` so the fusion rules are identical to production. Tuning
protocol is unchanged: pick alpha on the validation split, then read the reference column.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tools"))

FIXED = ("不满", "伤心", "开心", "担忧", "亲近", "混合", "无明显情绪", "无奈", "惊讶困惑", "不确定")
I = {l: i for i, l in enumerate(FIXED)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="desktop-data")
    ap.add_argument("--dataset-id", required=True)
    ap.add_argument("--pool", default="offline_review")
    ap.add_argument("--alphas", default="0,0.5,0.7,0.8,0.9,1.0")
    ap.add_argument("--out", default="output/experiment-sad-knn.json")
    args = ap.parse_args()

    data_dir = Path(args.data_dir).resolve()
    os.environ.setdefault("RAPPORT_DATA_DIR", str(data_dir))

    from evaluate_grounded import _prf
    from rapport.datasets import DatasetStore
    from rapport.emotion.model import RAW_TO_FIXED
    from rapport.grounded.calibrate import apply_temperature
    from rapport.grounded.classify import GroundedClassifier, _argmax, _blend
    from rapport.security import Crypto

    store = DatasetStore(data_dir / "datasets", Crypto(data_dir / ".key"))
    rows = store.records(args.dataset_id, args.pool)

    def emo(r):
        lab = r.get("labels")
        e = lab.get("emotion") if isinstance(lab, dict) else lab
        if isinstance(e, list):
            e = e[0] if e else None
        return e

    val = []
    for r in rows:
        if r.get("split") != "validation" or not r.get("training_eligible"):
            continue
        t = (r.get("text") or "").strip()
        e = emo(r)
        if e in I and len(t) >= 2:
            val.append((str(r.get("id")), t, e))

    con = sqlite3.connect(f"file:{(data_dir / 'app.db').as_posix()}?mode=ro", uri=True)
    gold = {
        s: e
        for s, e, sk in con.execute(
            "SELECT sample_id, emotion, skipped FROM gold_labels WHERE dataset_id = ?", (args.dataset_id,)
        )
        if e and not sk
    }
    con.close()
    byid = {str(r.get("id")): r for r in rows}
    ref = []
    for s, e in gold.items():
        r = byid.get(s)
        if not r:
            continue
        t = (r.get("text") or "").strip()
        if len(t) >= 2:
            ref.append((s, t, e))

    print(f"validation rows {len(val)} | reference rows {len(ref)}", flush=True)

    clf = GroundedClassifier.build(use_markers=False)
    if clf is None or clf.index is None:
        print("classifier/index unavailable")
        return 2
    idx = clf.index
    cnt = Counter(idx.labels)
    print("anchor counts:", {l: cnt[l] for l in FIXED if cnt[l]}, flush=True)

    def enc_pred(items):
        texts = [t[:200] for _, t, _ in items]
        vecs = clf.encoder.encode(texts)
        sdists = []
        for pred in clf.model.predict(texts):
            d = {}
            for raw, v in pred.probs.items():
                fx = RAW_TO_FIXED.get(raw)
                if fx:
                    d[fx] = d.get(fx, 0.0) + float(v)
            sdists.append(apply_temperature(d))
        return vecs, sdists

    def run(alpha, vecs, sdists, items):
        preds = []
        for (_sid, txt, _truth), v, sd in zip(items, vecs, sdists):
            neigh = idx.nearest(v, 15)
            w = {}
            for n in neigh:
                w[n.label] = w.get(n.label, 0.0) + max(n.score, 0.0) ** 3
            w = {l: val / (cnt[l] ** alpha) for l, val in w.items()}
            tot = sum(w.values()) or 1.0
            knn = {l: val / tot for l, val in w.items()}
            cent = idx.centroid_scores(v)
            scores = _blend({"structbert": sd, "bge_knn": knn, "centroid": cent})
            votes = {
                "structbert": (_argmax(sd)[0] or "-"),
                "bge_knn": (_argmax(knn)[0] or "-"),
                "centroid": (_argmax(cent)[0] or "-"),
            }
            r = clf._decide(txt, scores, votes, neigh[:3], (), None)
            preds.append(r.emotion)
        return preds

    vv, vs = enc_pred(val)
    rv, rs = enc_pred(ref)
    report = {"anchor_counts": {l: cnt[l] for l in FIXED if cnt[l]}, "runs": []}
    print(flush=True)
    print("alpha | split      | macro-F1 | acc    | 伤心 F1 (P/R,n)")
    for a in [float(x) for x in args.alphas.split(",")]:
        for name, items, vecs, sdists in (("validation", val, vv, vs), ("reference", ref, rv, rs)):
            preds = run(a, vecs, sdists, items)
            truth = [t for _, _, t in items]
            m = _prf(truth, preds, list(FIXED))
            report["runs"].append({"alpha": a, "split": name, "metrics": m, "truth": truth, "preds": preds})
            sad = m["per_class"]["伤心"]
            print(
                f"{a:5.2f} | {name:10s} | {m['macro_f1']:.4f}   | {m['accuracy']:.4f} | "
                f"{sad['f1']:.3f} (P{sad['precision']:.2f}/R{sad['recall']:.2f},n{sad['support']})"
            )
    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
