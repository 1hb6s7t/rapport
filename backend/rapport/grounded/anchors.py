"""kNN anchor index: this project's own labelled sentences, retrieved by BGE similarity.

Built offline from a built dataset's train split (`tools/build_anchors.py`), capped at N
sentences per emotion label. Stored encrypted (`data_dir/grounded/anchors.bin`, fernet+zlib,
the same protection the dataset snapshots use) because the anchor texts are the user's own
messages; only the small metadata file is plaintext and it carries no chat text.

Why retrieval rather than a classifier: it needs no training, it can say *which* sentences a
label came from (evidence, not just a number), and a new label only needs new examples. The
labels are AI-assisted (`quality: ai_assisted_high`), which is stated wherever the index is
described — this is retrieval over weak labels, not a gold benchmark.

The per-label cap makes the corpus uneven (a class with 21 eligible sentences contributes 21
anchors, a common one the full 300), and a plain similarity-weighted vote is biased toward the
big classes: a sad sentence retrieves 无奈 / 无明显情绪 neighbours and the 伤心 mass never
survives. ``PRIOR_ALPHA`` divides each class's accumulated similarity mass by its anchor count
before renormalising, which removes that bias.
"""

from __future__ import annotations

import base64
import json
import logging
import zlib
from dataclasses import dataclass
from pathlib import Path

from rapport.config import get_settings
from rapport.security import Crypto

log = logging.getLogger("rapport.grounded")

VERSION = "anchors-v1"
ANCHORS_DIRNAME = "grounded"
ANCHORS_FILENAME = "anchors.bin"

# Class-prior correction for the kNN vote. Each class's accumulated similarity mass is divided by
# count**PRIOR_ALPHA before renormalising: 0.0 is the uncorrected vote, 1.0 is a fully
# class-balanced vote. Chosen 2026-10-11 (see GROUNDED-LAYER.md): on the validation split alpha
# 0.7 and 1.0 are indistinguishable (2000-sample bootstrap CI crosses zero), so the principled
# full-balance value is used rather than one picked off the reference column. It lifts 伤心 F1
# from 0.000 to 0.436 on the 400 Claude reference labels (CI 0.26-0.59) and macro-F1 0.221 ->
# 0.276 (CI 0.023-0.086), while the weak-labelled validation split independently moves the same
# way (伤心 0.000 -> 0.270, macro 0.2395 -> 0.2576).
PRIOR_ALPHA = 1.0


@dataclass(frozen=True)
class Neighbor:
    score: float
    label: str
    text: str


def anchors_path() -> Path:
    return get_settings().data_dir / ANCHORS_DIRNAME / ANCHORS_FILENAME


class AnchorIndex:
    def __init__(self, meta: dict, vectors, labels: list[str], texts: list[str]):
        import numpy as np

        self.meta = meta
        self.vectors = np.asarray(vectors, dtype=np.float32)
        self.labels = labels
        self.texts = texts
        self.counts: dict[str, int] = {}
        for label in labels:
            self.counts[label] = self.counts.get(label, 0) + 1
        self.dim = int(self.vectors.shape[1]) if self.vectors.size else 512
        self.centroids = self._centroids()

    def _centroids(self) -> dict:
        import numpy as np

        out = {}
        for label in sorted(set(self.labels)):
            rows = self.vectors[[i for i, x in enumerate(self.labels) if x == label]]
            if not len(rows):
                continue
            centroid = rows.mean(axis=0)
            norm = float(np.linalg.norm(centroid)) or 1.0
            out[label] = centroid / norm
        return out

    def nearest(self, vector, k: int = 12) -> list[Neighbor]:
        import numpy as np

        if not self.vectors.size:
            return []
        sims = self.vectors @ np.asarray(vector, dtype=np.float32)
        order = np.argsort(-sims)[: max(1, int(k))]
        return [Neighbor(round(float(sims[i]), 4), self.labels[i], self.texts[i]) for i in order]

    def knn_scores(self, vector, k: int = 15) -> dict[str, float]:
        """Similarity-weighted vote among the k nearest anchors, normalized to sum 1.

        The accumulated mass per class is divided by ``count**PRIOR_ALPHA`` so a class with few
        anchors is not drowned out by a crowded one (see ``PRIOR_ALPHA``).
        """
        neighbors = self.nearest(vector, k)
        if not neighbors:
            return {}
        weights: dict[str, float] = {}
        for n in neighbors:
            weights[n.label] = weights.get(n.label, 0.0) + max(n.score, 0.0) ** 3
        if PRIOR_ALPHA:
            weights = {label: mass / (self.counts.get(label, 1) ** PRIOR_ALPHA) for label, mass in weights.items()}
        total = sum(weights.values()) or 1.0
        return {label: mass / total for label, mass in weights.items()}

    def centroid_scores(self, vector) -> dict[str, float]:
        import numpy as np

        if not self.centroids:
            return {}
        vec = np.asarray(vector, dtype=np.float32)
        raw = {label: float(vec @ centroid) for label, centroid in self.centroids.items()}
        # softmax over a moderate scale so a 0.02 cosine gap is not treated as certainty
        import math

        top = max(raw.values())
        exps = {k: math.exp((v - top) * 12.0) for k, v in raw.items()}
        total = sum(exps.values()) or 1.0
        return {k: v / total for k, v in exps.items()}

    def status(self) -> dict:
        return {
            "available": True,
            "version": self.meta.get("version", VERSION),
            "labels": sorted(set(self.labels)),
            "count": len(self.labels),
            "dim": self.dim,
            "source": self.meta.get("source"),
            "created_at": self.meta.get("created_at"),
            "quality": self.meta.get("quality"),
        }


def _payload(meta: dict, vectors, labels: list[str], texts: list[str]) -> bytes:
    import numpy as np

    body = {
        "meta": meta,
        "labels": labels,
        "texts": texts,
        "vectors": base64.b64encode(np.asarray(vectors, dtype=np.float16).tobytes()).decode("ascii"),
    }
    return zlib.compress(json.dumps(body, ensure_ascii=False).encode(), level=6)


def write_index(path: Path, crypto: Crypto, meta: dict, vectors, labels: list[str], texts: list[str]) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(crypto.encrypt(_payload(meta, vectors, labels, texts)))
    return {"path": str(path), "count": len(labels)}


_cache: dict[str, AnchorIndex | None] = {}


def load_index() -> AnchorIndex | None:
    """The shared index, or None when it has not been built / cannot be read."""
    path = anchors_path()
    key = str(path)
    if key in _cache:
        return _cache[key]
    if not path.is_file():
        _cache[key] = None
        return None
    try:
        import numpy as np

        crypto = Crypto(get_settings().data_dir / ".key")
        body = json.loads(zlib.decompress(crypto.decrypt(path.read_bytes())).decode("utf-8"))
        vectors = np.frombuffer(base64.b64decode(body["vectors"]), dtype=np.float16).astype(np.float32)
        vectors = vectors.reshape(len(body["labels"]), -1)
        index = AnchorIndex(body["meta"], vectors, list(body["labels"]), list(body["texts"]))
    except Exception:  # noqa: BLE001 - a broken index must never break an analysis run
        log.warning("anchor index unreadable at %s", path, exc_info=True)
        _cache[key] = None
        return None
    _cache[key] = index
    return index


def clear_cache() -> None:
    _cache.clear()


def anchors_status() -> dict:
    index = load_index()
    if index is not None:
        return index.status()
    path = anchors_path()
    return {
        "available": False,
        "version": VERSION,
        "labels": [],
        "count": 0,
        "dim": 512,
        "source": None,
        "created_at": None,
        "quality": None,
        "reason": "尚未构建（见 tools/build_anchors.py）" if not path.is_file() else "索引不可读",
    }
