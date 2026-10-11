"""Full parameter sweep of the offline fusion layer on a public benchmark (EWECT).

    python tools/sweep_public_fusion.py --ewect-dir <dir> [--n-random 3000] [--out output/...]

Why: the production layer's weights / thresholds were chosen on the dataset's *weak* (AI-assisted)
validation split. EWECT is the official SMP2020 Weibo emotion benchmark with human labels, 27.8k
train / 2k dev / 5k test, six classes that map onto six of the product's ten:

    angry->不满  happy->开心  neutral->无明显情绪  sad->伤心  fear->担忧  surprise->惊讶困惑

We rebuild the same fusion pipeline on EWECT (anchors from EWECT train, tuned on dev, reported on
test) and sweep every tunable parameter. The winner is then checked against the product's own 400
Claude-reference labels, so a public-data optimum that does not transfer is visible rather than
assumed.

The decision rules are re-implemented vectorised but mirror ``GroundedClassifier._decide`` exactly:
looks_neutral shortcut -> NEUTRAL_THRESHOLD -> negative two-signal guard -> MARGIN_THRESHOLD.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
from collections import Counter
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

FIXED = ("不满", "伤心", "开心", "担忧", "亲近", "混合", "无明显情绪", "无奈", "惊讶困惑", "不确定")
NEUTRAL, UNSURE = "无明显情绪", "不确定"
NEGATIVE = {"不满", "伤心"}
EWECT_MAP = {
    "angry": "不满",
    "happy": "开心",
    "neutral": "无明显情绪",
    "sad": "伤心",
    "fear": "担忧",
    "surprise": "惊讶困惑",
}
PROD = {"w_struct": 0.10, "w_knn": 0.65, "w_cent": 0.25, "neutral": 0.35, "margin": 0.05, "k": 15, "alpha": 1.0, "exp": 3.0, "scale": 12.0}

URL_RE = re.compile(r"https?://|www\.|t\.me/|\.com/|\.cn/|\.io/", re.IGNORECASE)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
CODE_HINT_RE = re.compile(r"[{}<>_=/\\|`]{1}|^[A-Za-z0-9_\-:.]{12,}$")
CJK_RE = re.compile(r"[一-鿿]")


def load_ewect(path: Path) -> list[tuple[str, str]]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for r in rows:
        lab = EWECT_MAP.get(r.get("label"))
        text = (r.get("content") or "").strip()
        if lab and len(text) >= 2:
            out.append((text, lab))
    return out


def looks_neutral(text: str) -> bool:
    s = text.strip()
    if len(s) < 2:
        return True
    if URL_RE.search(s) or EMAIL_RE.search(s):
        return True
    return bool(not CJK_RE.search(s) and (CODE_HINT_RE.search(s) or len(s) > 8))


def macro_f1(truth: list[str], pred: list[str], labels: list[str]) -> float:
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
    ap.add_argument("--ewect-dir", required=True)
    ap.add_argument("--n-random", type=int, default=3000)
    ap.add_argument("--per-label", type=int, default=300)
    ap.add_argument("--out", default="output/sweep-public-fusion.json")
    args = ap.parse_args()

    d = Path(args.ewect_dir)
    train = load_ewect(d / "usual_train.txt")
    dev = load_ewect(d / "usual_eval_labeled.txt")
    test = load_ewect(d / "usual_test_labeled.txt")
    labels = sorted({l for _, l in train})
    print(f"EWECT train {len(train)} dev {len(dev)} test {len(test)} classes {len(labels)}", flush=True)

    # anchors: same filters as tools/build_anchors.py (2..80 chars, dedup, per-label cap)
    buckets: dict[str, list[str]] = {}
    seen: set[str] = set()
    for text, lab in train:
        if not (2 <= len(text) <= 80) or text in seen:
            continue
        b = buckets.setdefault(lab, [])
        if len(b) >= args.per_label:
            continue
        b.append(text)
        seen.add(text)
    anchor_texts, anchor_labels = [], []
    for lab in sorted(buckets):
        for t in buckets[lab]:
            anchor_texts.append(t)
            anchor_labels.append(lab)
    print("anchors:", dict(Counter(anchor_labels)), flush=True)

    from rapport.embed import load_encoder
    from rapport.emotion.model import RAW_TO_FIXED, load_model
    from rapport.grounded.anchors import AnchorIndex

    encoder = load_encoder()
    model = load_model()
    if encoder is None or model is None:
        print("encoder/model unavailable")
        return 2

    av = encoder.encode(anchor_texts)
    idx = AnchorIndex({"version": "ewect"}, av, anchor_labels, anchor_texts)
    cls_list = sorted(set(anchor_labels))
    cls_ix = {c: i for i, c in enumerate(cls_list)}
    counts = np.array([idx.counts[c] for c in cls_list], dtype=np.float64)
    cent = np.stack([idx.centroids[c] for c in cls_list])  # (C, dim)

    def featurise(items):
        texts = [t[:200] for t, _ in items]
        vecs = encoder.encode(texts)
        # top-N neighbours
        sims = vecs @ idx.vectors.T  # (n, A)
        N = 40
        order = np.argsort(-sims, axis=1)[:, :N]
        nb_lab = np.array([[cls_ix[anchor_labels[j]] for j in row] for row in order])
        nb_sim = np.take_along_axis(sims, order, axis=1)
        # centroid raw cosines
        raw_cent = vecs @ cent.T  # (n, C)
        # structbert fixed distribution over the 10 fixed labels
        sdist = np.zeros((len(texts), len(FIXED)))
        for i, p in enumerate(model.predict(texts)):
            for raw, v in p.probs.items():
                fx = RAW_TO_FIXED.get(raw)
                if fx:
                    sdist[i, FIXED.index(fx)] += float(v)
        neutral_flag = np.array([looks_neutral(t) for t, _ in items])
        truth = [lab for _, lab in items]
        return {"nb_lab": nb_lab, "nb_sim": nb_sim, "raw_cent": raw_cent, "sdist": sdist,
                    "neutral_flag": neutral_flag, "truth": truth}

    F = featurise(dev)
    print("dev features ready", flush=True)

    def predict(F, cfg):
        n = len(F["truth"])
        k = cfg["k"]
        w = np.where(np.arange(F["nb_sim"].shape[1]) < k, F["nb_sim"], 0.0)
        w = np.maximum(w, 0.0) ** cfg["exp"]
        mass = np.zeros((n, len(cls_list)))
        for c in range(len(cls_list)):
            mass[:, c] = (w * (F["nb_lab"] == c)).sum(axis=1)
        mass = mass / (counts ** cfg["alpha"])
        knn = mass / np.maximum(mass.sum(axis=1, keepdims=True), 1e-12)
        rc = F["raw_cent"]
        e = np.exp((rc - rc.max(axis=1, keepdims=True)) * cfg["scale"])
        cent = e / e.sum(axis=1, keepdims=True)
        # blend onto the 10-label space
        scores = np.zeros((n, len(FIXED)))
        for c, lab in enumerate(cls_list):
            scores[:, FIXED.index(lab)] += cfg["w_knn"] * knn[:, c] + cfg["w_cent"] * cent[:, c]
        scores += cfg["w_struct"] * F["sdist"]
        tot = cfg["w_knn"] + cfg["w_cent"] + cfg["w_struct"]
        scores /= tot
        top_i = scores.argmax(axis=1)
        srt = np.sort(scores, axis=1)
        value = srt[:, -1]
        margin = srt[:, -1] - srt[:, -2]
        # per-signal votes
        k_vote = np.array([cls_list[i] for i in knn.argmax(axis=1)])
        c_vote = np.array([cls_list[i] for i in cent.argmax(axis=1)])
        s_vote = np.array([FIXED[i] for i in F["sdist"].argmax(axis=1)])
        top_lab = np.array([FIXED[i] for i in top_i])
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

    def score(F, cfg):
        pred = predict(F, cfg)
        return macro_f1(F["truth"], pred, labels), sum(1 for t, p in zip(F["truth"], pred) if t == p) / len(pred)

    base_m, base_a = score(F, PROD)
    print(f"production config on EWECT dev: macro-F1 {base_m:.4f} acc {base_a:.4f}", flush=True)

    rng = random.Random(20261011)
    best = (base_m, dict(PROD))
    results = []
    for it in range(args.n_random):
        a, b, c = rng.random(), rng.random(), rng.random()
        s = a + b + c
        cfg = {
            "w_struct": a / s, "w_knn": b / s, "w_cent": c / s,
            "neutral": rng.uniform(0.10, 0.75), "margin": rng.uniform(0.0, 0.15),
            "k": rng.randint(4, 32), "alpha": rng.uniform(0.0, 1.6),
            "exp": rng.uniform(1.0, 6.0), "scale": rng.uniform(4.0, 26.0),
        }
        m, _ = score(F, cfg)
        results.append((m, cfg))
        if m > best[0]:
            best = (m, dict(cfg))
        if (it + 1) % 500 == 0:
            print(f"  {it+1}/{args.n_random} best dev macro-F1 {best[0]:.4f}", flush=True)

    # coordinate refinement around the best
    cfg = dict(best[1])
    for _ in range(3):
        for key, lo, hi, step in (
            ("w_struct", 0.0, 0.6, 0.02), ("neutral", 0.10, 0.75, 0.01), ("margin", 0.0, 0.15, 0.005),
            ("alpha", 0.0, 1.6, 0.05), ("exp", 1.0, 6.0, 0.25), ("scale", 4.0, 26.0, 1.0),
        ):
            for delta in (step, -step):
                trial = dict(cfg)
                trial[key] = min(hi, max(lo, cfg[key] + delta))
                m, _ = score(F, trial)
                if m > best[0]:
                    best = (m, dict(trial))
                    cfg = dict(trial)
        for dk in (-2, -1, 1, 2):
            trial = dict(cfg)
            trial["k"] = max(4, min(32, cfg["k"] + dk))
            m, _ = score(F, trial)
            if m > best[0]:
                best = (m, dict(trial))
                cfg = dict(trial)
    print(f"\ntuned dev macro-F1 {best[0]:.4f}", flush=True)

    results.sort(key=lambda x: -x[0])
    report = {
        "dev_rows": len(dev), "test_rows": len(test), "anchor_counts": dict(Counter(anchor_labels)),
        "production_on_dev": {"macro_f1": round(base_m, 4), "accuracy": round(base_a, 4)},
        "tuned": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in best[1].items()},
        "top20": [{"macro_f1": round(m, 4), **{k: (round(v, 4) if isinstance(v, float) else v) for k, v in c.items()}}
                  for m, c in results[:20]],
    }

    T = featurise(test)
    tm, ta = score(T, PROD)
    bm, ba = score(T, best[1])
    report["test_production"] = {"macro_f1": round(tm, 4), "accuracy": round(ta, 4)}
    report["test_tuned"] = {"macro_f1": round(bm, 4), "accuracy": round(ba, 4)}
    print(f"EWECT test  production macro-F1 {tm:.4f} acc {ta:.4f}", flush=True)
    print(f"EWECT test  tuned      macro-F1 {bm:.4f} acc {ba:.4f}", flush=True)

    # per-class on test, tuned
    pred = predict(T, best[1])
    pc = {}
    for lab in labels:
        tp = sum(1 for t, p in zip(T["truth"], pred) if t == lab and p == lab)
        fp = sum(1 for t, p in zip(T["truth"], pred) if t != lab and p == lab)
        fn = sum(1 for t, p in zip(T["truth"], pred) if t == lab and p != lab)
        pr = tp / (tp + fp) if tp + fp else 0.0
        rc = tp / (tp + fn) if tp + fn else 0.0
        pc[lab] = {"f1": round(2 * pr * rc / (pr + rc) if pr + rc else 0.0, 3), "n": tp + fn}
    report["test_per_class_tuned"] = pc
    print("test per-class (tuned):", pc, flush=True)

    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
