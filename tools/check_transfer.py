"""Apply a fusion-layer configuration to the *production* anchors and score it on the product's own rows.

    python tools/check_transfer.py --data-dir desktop-data --dataset-id <id> --configs <json>

`tools/sweep_public_fusion.py` tunes on EWECT (public, human labels). This checks whether a
configuration found there also wins on the product's real data: the 400 Claude reference labels and
the dataset's weak-labelled validation split. Same vectorised decision rules as the sweep, so the
numbers are comparable, and the production anchor corpus is the one actually shipped.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

FIXED = ("不满", "伤心", "开心", "担忧", "亲近", "混合", "无明显情绪", "无奈", "惊讶困惑", "不确定")
NEUTRAL, UNSURE = "无明显情绪", "不确定"
NEGATIVE = {"不满", "伤心"}
PROD = {"w_struct": 0.10, "w_knn": 0.65, "w_cent": 0.25, "neutral": 0.35, "margin": 0.05, "k": 15, "alpha": 1.0, "exp": 3.0, "scale": 12.0}

URL_RE = re.compile(r"https?://|www\.|t\.me/|\.com/|\.cn/|\.io/", re.IGNORECASE)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
CODE_HINT_RE = re.compile(r"[{}<>_=/\\|`]{1}|^[A-Za-z0-9_\-:.]{12,}$")
CJK_RE = re.compile(r"[一-鿿]")


def looks_neutral(text: str) -> bool:
    s = text.strip()
    if len(s) < 2:
        return True
    if URL_RE.search(s) or EMAIL_RE.search(s):
        return True
    return bool(not CJK_RE.search(s) and (CODE_HINT_RE.search(s) or len(s) > 8))


def macro_f1(truth, pred, labels):
    fs = []
    for lab in labels:
        tp = sum(1 for t, p in zip(truth, pred) if t == lab and p == lab)
        fp = sum(1 for t, p in zip(truth, pred) if t != lab and p == lab)
        fn = sum(1 for t, p in zip(truth, pred) if t == lab and p != lab)
        pr = tp / (tp + fp) if tp + fp else 0.0
        rc = tp / (tp + fn) if tp + fn else 0.0
        fs.append(2 * pr * rc / (pr + rc) if pr + rc else 0.0)
    return sum(fs) / len(fs)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="desktop-data")
    ap.add_argument("--dataset-id", required=True)
    ap.add_argument("--pool", default="offline_review")
    ap.add_argument("--configs", required=True, help="JSON file: {name: {cfg}}")
    ap.add_argument("--sweep", type=int, default=0, help="random-search the space on the validation split, then report on reference")
    ap.add_argument("--out", default="")
    ap.add_argument("--dump-preds", default="")
    args = ap.parse_args()

    data_dir = Path(args.data_dir).resolve()
    os.environ.setdefault("RAPPORT_DATA_DIR", str(data_dir))

    from rapport.datasets import DatasetStore
    from rapport.embed import load_encoder
    from rapport.emotion.model import RAW_TO_FIXED, load_model
    from rapport.grounded.anchors import load_index
    from rapport.security import Crypto

    store = DatasetStore(data_dir / "datasets", Crypto(data_dir / ".key"))
    rows = store.records(args.dataset_id, args.pool)

    def emo(r):
        lab = r.get("labels")
        e = lab.get("emotion") if isinstance(lab, dict) else lab
        if isinstance(e, list):
            e = e[0] if e else None
        return e

    val = [(str(r.get("id")), (r.get("text") or "").strip(), emo(r))
           for r in rows if r.get("split") == "validation" and r.get("training_eligible")]
    val = [(i, t, e) for i, t, e in val if e in FIXED and len(t) >= 2]

    con = sqlite3.connect(f"file:{(data_dir / 'app.db').as_posix()}?mode=ro", uri=True)
    gold = {s: e for s, e, sk in con.execute(
        "SELECT sample_id, emotion, skipped FROM gold_labels WHERE dataset_id = ?", (args.dataset_id,)) if e and not sk}
    con.close()
    byid = {str(r.get("id")): r for r in rows}
    ref = []
    for s, e in gold.items():
        r = byid.get(s)
        if r and len((r.get("text") or "").strip()) >= 2:
            ref.append((s, (r.get("text") or "").strip(), e))

    idx = load_index()
    encoder = load_encoder()
    model = load_model()
    if idx is None or encoder is None or model is None:
        print("production index/encoder/model unavailable")
        return 2
    cls_list = sorted(set(idx.labels))
    cls_ix = {c: i for i, c in enumerate(cls_list)}
    counts = np.array([idx.counts[c] for c in cls_list], dtype=np.float64)
    cent = np.stack([idx.centroids[c] for c in cls_list])
    print("production anchors:", {c: int(counts[i]) for c, i in cls_ix.items()}, flush=True)

    def featurise(items):
        texts = [t[:200] for _, t, _ in items]
        vecs = encoder.encode(texts)
        sims = vecs @ idx.vectors.T
        N = 40
        order = np.argsort(-sims, axis=1)[:, :N]
        nb_lab = np.array([[cls_ix[idx.labels[j]] for j in row] for row in order])
        nb_sim = np.take_along_axis(sims, order, axis=1)
        raw_cent = vecs @ cent.T
        sdist = np.zeros((len(texts), len(FIXED)))
        for i, p in enumerate(model.predict(texts)):
            for raw, v in p.probs.items():
                fx = RAW_TO_FIXED.get(raw)
                if fx:
                    sdist[i, FIXED.index(fx)] += float(v)
        return {"nb_lab": nb_lab, "nb_sim": nb_sim, "raw_cent": raw_cent, "sdist": sdist,
                    "neutral_flag": np.array([looks_neutral(t) for _, t, _ in items]),
                    "truth": [e for _, _, e in items]}

    def predict(F, cfg):
        n = len(F["truth"])
        k = int(cfg["k"])
        w = np.where(np.arange(F["nb_sim"].shape[1]) < k, F["nb_sim"], 0.0)
        w = np.maximum(w, 0.0) ** cfg["exp"]
        mass = np.zeros((n, len(cls_list)))
        for c in range(len(cls_list)):
            mass[:, c] = (w * (F["nb_lab"] == c)).sum(axis=1)
        mass = mass / (counts ** cfg["alpha"])
        knn = mass / np.maximum(mass.sum(axis=1, keepdims=True), 1e-12)
        rc = F["raw_cent"]
        e = np.exp((rc - rc.max(axis=1, keepdims=True)) * cfg["scale"])
        ct = e / e.sum(axis=1, keepdims=True)
        scores = np.zeros((n, len(FIXED)))
        for c, lab in enumerate(cls_list):
            scores[:, FIXED.index(lab)] += cfg["w_knn"] * knn[:, c] + cfg["w_cent"] * ct[:, c]
        scores += cfg["w_struct"] * F["sdist"]
        scores /= (cfg["w_knn"] + cfg["w_cent"] + cfg["w_struct"])
        srt = np.sort(scores, axis=1)
        value, margin = srt[:, -1], srt[:, -1] - srt[:, -2]
        top_lab = np.array([FIXED[i] for i in scores.argmax(axis=1)])
        k_vote = np.array([cls_list[i] for i in knn.argmax(axis=1)])
        c_vote = np.array([cls_list[i] for i in ct.argmax(axis=1)])
        s_vote = np.array([FIXED[i] for i in F["sdist"].argmax(axis=1)])
        agree = (k_vote == top_lab).astype(int) + (c_vote == top_lab).astype(int) + (s_vote == top_lab).astype(int)
        is_neg = np.isin(top_lab, list(NEGATIVE))
        pred = top_lab.copy()
        pred[F["neutral_flag"]] = NEUTRAL
        low = (~F["neutral_flag"]) & (value < cfg["neutral"])
        pred[low] = NEUTRAL
        neg = (~F["neutral_flag"]) & (~low) & is_neg & (agree < 2)
        pred[neg] = UNSURE
        mar = (~F["neutral_flag"]) & (~low) & (~is_neg) & (margin < cfg["margin"])
        pred[mar] = UNSURE
        return pred.tolist()

    configs = json.loads(Path(args.configs).read_text(encoding="utf-8"))
    Fv, Fr = featurise(val), featurise(ref)
    print(f"validation rows {len(val)} | reference rows {len(ref)}\n", flush=True)
    print(f"{'config':<26} | {'validation (weak)':<18} | {'reference (Claude)':<18}")
    print(f"{'':<26} | {'macro-F1   acc':<18} | {'macro-F1   acc':<18}")
    out = {"validation_rows": len(val), "reference_rows": len(ref), "results": {}}
    preds_out = {}

    if args.sweep:
        import random as _r

        rng = _r.Random(20261011)

        def sc(F, cfg):
            p = predict(F, cfg)
            return macro_f1(F["truth"], p, list(FIXED)), sum(1 for t, x in zip(F["truth"], p) if t == x) / len(p)

        bm, _ = sc(Fv, PROD)
        best = (bm, dict(PROD))
        found = []
        for it in range(args.sweep):
            a, b, c = rng.random(), rng.random(), rng.random()
            s = a + b + c
            cfg = {"w_struct": a / s, "w_knn": b / s, "w_cent": c / s, "neutral": rng.uniform(0.10, 0.75),
                       "margin": rng.uniform(0.0, 0.15), "k": rng.randint(4, 32), "alpha": rng.uniform(0.0, 1.6),
                       "exp": rng.uniform(1.0, 6.0), "scale": rng.uniform(4.0, 26.0)}
            m, _ = sc(Fv, cfg)
            found.append((m, cfg))
            if m > best[0]:
                best = (m, dict(cfg))
        print(f"local sweep on validation: prod {bm:.4f} -> best {best[0]:.4f} (in-sample on validation)\n", flush=True)
        found.sort(key=lambda x: -x[0])
        out["local_sweep"] = {"validation_best": round(best[0], 4),
                              "top10": [{"val_macro": round(m, 4), "cfg": c} for m, c in found[:10]]}
        for m, c in found[:6]:
            pv, pr = predict(Fv, c), predict(Fr, c)
            mv = macro_f1(Fv["truth"], pv, list(FIXED))
            av = sum(1 for t, x in zip(Fv["truth"], pv) if t == x) / len(pv)
            mr = macro_f1(Fr["truth"], pr, list(FIXED))
            ar = sum(1 for t, x in zip(Fr["truth"], pr) if t == x) / len(pr)
            print(f"  val {mv:.4f}/{av:.4f} -> ref {mr:.4f}/{ar:.4f} | ws {c['w_struct']:.2f} wk {c['w_knn']:.2f} "
                  f"wc {c['w_cent']:.2f} neu {c['neutral']:.2f} mar {c['margin']:.3f} k {c['k']} "
                  f"a {c['alpha']:.2f} e {c['exp']:.2f} s {c['scale']:.1f}")
        print(flush=True)

    for name, cfg in configs.items():
        pv, pr = predict(Fv, cfg), predict(Fr, cfg)
        mv, av = macro_f1(Fv["truth"], pv, list(FIXED)), sum(1 for t, p in zip(Fv["truth"], pv) if t == p) / len(pv)
        mr, ar = macro_f1(Fr["truth"], pr, list(FIXED)), sum(1 for t, p in zip(Fr["truth"], pr) if t == p) / len(pr)
        sad = macro_f1(Fr["truth"], pr, ["伤心"])
        print(f"{name:<26} | {mv:.4f}  {av:.4f}   | {mr:.4f}  {ar:.4f}   伤心F1 {sad:.3f}", flush=True)
        preds_out[name] = pr
        out["results"][name] = {"val_macro": round(mv, 4), "val_acc": round(av, 4),
                                "ref_macro": round(mr, 4), "ref_acc": round(ar, 4), "ref_sad_f1": round(sad, 4),
                                "cfg": cfg}
    if args.dump_preds:
        dp = ROOT / args.dump_preds
        dp.parent.mkdir(parents=True, exist_ok=True)
        dp.write_text(json.dumps({"truth": Fr["truth"], "preds": preds_out}, ensure_ascii=False), encoding="utf-8")
    if args.out:
        p = ROOT / args.out
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwrote {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
