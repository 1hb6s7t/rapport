"""Build the encrypted kNN anchor index from a built dataset's train split.

    python tools/build_anchors.py --data-dir desktop-data --dataset-id <id> [--per-label 300]

Every anchor is one of the user's own messages with an AI-assisted emotion label
(`quality: ai_assisted_high`) from the *train* split, so the evaluation in
`tools/evaluate_grounded.py` (test split) never sees them. The index is stored encrypted at
`<data-dir>/grounded/anchors.bin`; no anchor text is written anywhere in plaintext.
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--pool", default="offline_review")
    parser.add_argument("--per-label", type=int, default=300)
    parser.add_argument("--split", default="train")
    parser.add_argument("--min-chars", type=int, default=2)
    parser.add_argument("--max-chars", type=int, default=80)
    args = parser.parse_args()

    data_dir = Path(args.data_dir).resolve()
    os.environ.setdefault("RAPPORT_DATA_DIR", str(data_dir))

    from rapport.datasets import DatasetStore
    from rapport.embed.bge import load_encoder, model_dir
    from rapport.grounded.anchors import VERSION, anchors_path, write_index
    from rapport.security import Crypto

    crypto = Crypto(data_dir / ".key")
    store = DatasetStore(data_dir / "datasets", crypto)
    rows = store.records(args.dataset_id, args.pool)

    encoder = load_encoder()
    if encoder is None:
        print("BGE encoder unavailable; run tools/bge_model.py export/verify first")
        return 2

    per_label: dict[str, list[str]] = {}
    seen: set[str] = set()
    for row in rows:
        if row.get("split") != args.split or not row.get("training_eligible"):
            continue
        if row.get("quality") != "ai_assisted_high":
            continue
        text = (row.get("text") or "").strip()
        if not (args.min_chars <= len(text) <= args.max_chars) or text in seen:
            continue
        labels = (row.get("labels") or {}).get("emotion") or []
        if len(labels) != 1 or labels[0] in {"不确定", "none", "混合"}:
            continue
        label = labels[0]
        bucket = per_label.setdefault(label, [])
        if len(bucket) >= args.per_label:
            continue
        bucket.append(text)
        seen.add(text)

    texts: list[str] = []
    labels: list[str] = []
    for label in sorted(per_label):
        for text in per_label[label]:
            texts.append(text)
            labels.append(label)
    if not texts:
        print("no eligible anchors found")
        return 3

    vectors = encoder.encode(texts)
    meta = {
        "version": VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "dataset_id": args.dataset_id,
            "pool": args.pool,
            "split": args.split,
            "per_label": args.per_label,
        },
        "quality": "ai_assisted（AI 辅助标注，非人工金标）",
        "counts": dict(Counter(labels)),
        "model": {"id": encoder.manifest.get("id"), "dir": str(model_dir())},
    }
    path = anchors_path()
    write_index(path, crypto, meta, vectors, labels, texts)
    print(json.dumps({"path": str(path), "count": len(texts), "labels": meta["counts"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
