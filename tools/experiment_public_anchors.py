"""Does adding public human-labelled anchors help the thin classes?

    python tools/experiment_public_anchors.py --ewect-dir <dir> [--caps 0,100,300]

The kNN anchors come from the product's own weak-labelled *train* split, where 伤心 has only 21
eligible sentences against 300 for the common classes. EWECT is human-labelled and has thousands
per class. This adds EWECT's emotional classes as extra anchors alongside the product's own and
scores the result on the product's 400 Claude-reference labels with the shipped configuration.

Deliberately excluded: EWECT's ``neutral`` class. Its examples are news / encyclopaedia text, a
different register from chat's neutral, so adding them would corrupt the neutral gate rather than
help it. ``亲近 / 无奈 / 混合 / 不确定`` have no EWECT counterpart at all.

Caveat this experiment cannot settle: the product shows the nearest anchor's label and score as
evidence. A Weibo anchor is not the user's own sentence, so a numeric win here is not by itself a
reason to ship.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

FIXED = ("不满", "伤心", "开心", "担忧", "亲近", "混合", "无明显情绪", "无奈", "惊讶困惑", "不确定")
NEUTRAL, UNSURE = "无明显情绪", "不确定"
NEGATIVE = {"不满", "伤心"}
PROD = {"w_struct": 0.10, "w_knn": 0.65, "w_cent": 0.25, "neutral": 0.35, "margin": 0.05,
        "k": 15, "alpha": 1.0, "exp": 3.0, "scale": 12.0}
EWECT_MAP = {"angry": "不满", "happy": "开心", "sad": "伤心", "fear": "担忧", "surprise": "惊讶困惑"}

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
    ap.add_argument("--ewect-dir", required=True)
    ap.add_argument("--data-dir", default="desktop-data")
    ap.add_argument("--dataset-id", required=True)
    ap.add_argument("--pool", default="offline_review")
    ap.add_argument("--caps", default="0,100,300")
    ap.add_argument("--out", default="output/experiment-public-anchors.json")
    args = ap.parse_args()

    data_dir = Path(args.data_dir).resolve()
    os.environ.setdefault("RAPPORT_DATA_DIR", str(data_dir))

    from rapport.datasets import DatasetStore
    from rapport.embed import load_encoder
    from rapport.emotion.model import RAW_TO_FIXED, load_model
    from rapport.grounded.anchors import AnchorIndex, load_index
    from rapport.security import Crypto

    prod = load_index()
    encoder = load_encoder()
    model = load_model()
    if prod is None or encoder is None or model is None:
        print("production index/encoder/model unavailable")
        return 2
    print("production anchors:", dict(Counter(prod.labels)), flush=True)

    # --- EWECT extra anchors (emotional classes only; neutral deliberately dropped) ---
    raw = json.loads((Path(args.ewect_dir) / "usual_train.txt").read_text(encoding="utf-8"))
    seen: set[str] = set()
    pool: dict[str, list[str]] = {}
    for r in raw:
        lab = EWECT_MAP.get(r.get("label"))
        text = (r.get("content") or "").strip()
        if not lab or not (2 <= len(text) <= 80) or text in seen:
            continue
        b = pool.setdefault(lab, [])
        if len(b) >= max(int(x) for x in args.caps.split(",")):
            continue
        b.append(text)
        seen.add(text)
    print("EWECT candidates:", {k: len(v) for k, v in pool.items()}, flush=True)

    # --- evaluation rows: the product's 400 Claude reference labels ---
    store = DatasetStore(data_dir / "datasets", Crypto(data_dir / ".key"))
    rows = store.records(args.dataset_id, args.pool)
    byid = {str(r.get("id")): r for r in rows}
    con = sqlite3.connect(f"file:{(data_dir / 'app.db').as_posix()}?mode=ro", uri=True)
    gold = {s: e for s, e, sk in con.execute(
        "SELECT sample_id, emotion, skipped FROM gold_labels WHERE dataset_id = ?", (args.dataset_id,)) if e and not sk}
    con.close()
    items = []
    for s, e in gold.items():
        r = byid.get(s)
        if r and len((r.get("text") or "").strip()) >= 2:
            items.append(((r.get("text") or "").strip()[:200], e))
    print(f"reference rows {len(items)}", flush=True)

    eval_vecs = encoder.encode([t for t, _ in items])
    sdist = np.zeros((len(items), len(FIXED)))
    for i, p in enumerate(model.predict([t for t, _ in items])):
        for raw_lab, v in p.probs.items():
            fx = RAW_TO_FIXED.get(raw_lab)
            if fx:
                sdist[i, FIXED.index(fx)] += float(v)
    truth = [e for _, e in items]
    neutral_flag = np.array([looks_neutral(t) for t, _ in items])

    def evaluate(index, tag):
        cls_list = sorted(set(index.labels))
        cls_ix = {c: i for i, c in enumerate(cls_list)}
        counts = np.array([index.counts[c] for c in cls_list], dtype=np.float64)
        cent = np.stack([index.centroids[c] for c in cls_list])
        sims = eval_vecs @ index.vectors.T
        N = 40
        order = np.argsort(-sims, axis=1)[:, :N]
        nb_lab = np.array([[cls_ix[index.labels[j]] for j in row] for row in order])
        nb_sim = np.take_along_axis(sims, order, axis=1)
        rc = eval_vecs @ cent.T
        cfg = PROD
        w = np.where(np.arange(N) < cfg["k"], nb_sim, 0.0)
        w = np.maximum(w, 0.0) ** cfg["exp"]
        mass = np.zeros((len(items), len(cls_list)))
        for c in range(len(cls_list)):
            mass[:, c] = (w * (nb_lab == c)).sum(axis=1)
        mass = mass / (counts ** cfg["alpha"])
        knn = mass / np.maximum(mass.sum(axis=1, keepdims=True), 1e-12)
        e = np.exp((rc - rc.max(axis=1, keepdims=True)) * cfg["scale"])
        ct = e / e.sum(axis=1, keepdims=True)
        scores = np.zeros((len(items), len(FIXED)))
        for c, lab in enumerate(cls_list):
            scores[:, FIXED.index(lab)] += cfg["w_knn"] * knn[:, c] + cfg["w_cent"] * ct[:, c]
        scores += cfg["w_struct"] * sdist
        srt = np.sort(scores, axis=1)
        value, margin = srt[:, -1], srt[:, -1] - srt[:, -2]
        top_lab = np.array([FIXED[i] for i in scores.argmax(axis=1)])
        k_vote = np.array([cls_list[i] for i in knn.argmax(axis=1)])
        c_vote = np.array([cls_list[i] for i in ct.argmax(axis=1)])
        s_vote = np.array([FIXED[i] for i in sdist.argmax(axis=1)])
        agree = (k_vote == top_lab).astype(int) + (c_vote == top_lab).astype(int) + (s_vote == top_lab).astype(int)
        is_neg = np.isin(top_lab, list(NEGATIVE))
        pred = top_lab.copy()
        pred[neutral_flag] = NEUTRAL
        low = (~neutral_flag) & (value < cfg["neutral"])
        pred[low] = NEUTRAL
        pred[(~neutral_flag) & (~low) & is_neg & (agree < 2)] = UNSURE
        pred[(~neutral_flag) & (~low) & (~is_neg) & (margin < cfg["margin"])] = UNSURE
        pred = pred.tolist()
        m = macro_f1(truth, pred, list(FIXED))
        a = sum(1 for t, p in zip(truth, pred) if t == p) / len(pred)
        sad = macro_f1(truth, pred, ["伤心"])
        print(f"  {tag:<28} anchors {len(index.labels):>5} | macro {m:.4f} acc {a:.4f} 伤心F1 {sad:.3f}", flush=True)
        return {"macro": round(m, 4), "acc": round(a, 4), "sad_f1": round(sad, 4), "anchors": len(index.labels)}

    report = {"production_anchors": dict(Counter(prod.labels)), "results": {}}
    report["results"]["production only"] = evaluate(prod, "production only")
    for cap in [int(x) for x in args.caps.split(",") if int(x) > 0]:
        texts = [t for lab in sorted(pool) for t in pool[lab][:cap]]
        labs = [lab for lab in sorted(pool) for t in pool[lab][:cap]]
        extra = encoder.encode(texts)
        vectors = np.vstack([prod.vectors, extra])
        labels = list(prod.labels) + labs
        idx = AnchorIndex({"version": "hybrid"}, vectors, labels, list(prod.texts) + texts)
        report["results"][f"+EWECT cap {cap}"] = evaluate(idx, f"+EWECT cap {cap}")

    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
