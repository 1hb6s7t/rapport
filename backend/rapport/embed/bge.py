"""Bundled BGE-small-zh-v1.5 sentence encoder (ONNX Runtime, fully offline).

``BAAI/bge-small-zh-v1.5`` (MIT) is the retrieval encoder the project's research runs already
used (`rapport.research.runner.feature_cache`). Exporting it to ONNX lets the *production* rules
mode reuse the same embeddings, so a sentence label can come with "here are the sentences the
encoder found most similar, and what this project's corpus labelled them".

Contract: 512-dim L2-normalized CLS vector, 96 tokens, onnxruntime only (no torch in the app).
Provenance, sha256 and tokenizer parity live in ``models/bge-small-zh-v1.5/manifest.json``,
written by ``tools/bge_model.py verify``.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from rapport.config import get_settings
from rapport.emotion.tokenizer import BertTokenizer

log = logging.getLogger("rapport.embed")

MODEL_ID = "BAAI/bge-small-zh-v1.5"
MODEL_DIRNAME = "bge-small-zh-v1.5"
SOURCE_URL = "https://huggingface.co/BAAI/bge-small-zh-v1.5"
LICENSE = "MIT"
NOTE = "离线检索编码器：把句子变成向量，用于在本机语料里找最相似的例句；本身不直接下结论。"
DEFAULT_MAX_TOKENS = 96


def model_dir() -> Path:
    """Env override, then the app tree, then the data dir (repo installs without models/)."""
    override = os.environ.get("RAPPORT_BGE_MODEL_DIR")
    if override:
        return Path(override)
    candidates = [
        Path(__file__).resolve().parents[3] / "models" / MODEL_DIRNAME,
        get_settings().data_dir / "models" / MODEL_DIRNAME,
    ]
    for candidate in candidates:
        if (candidate / "model.onnx").is_file():
            return candidate
    return candidates[0]


class BgeEncoder:
    def __init__(self, directory: Path):
        self.dir = directory
        self.manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        self.max_tokens = int(self.manifest.get("max_tokens") or DEFAULT_MAX_TOKENS)
        self.dim = int(self.manifest.get("hidden_size") or 512)
        do_lower_case = bool((self.manifest.get("tokenizer") or {}).get("do_lower_case", False))
        self.tokenizer = BertTokenizer(directory / "vocab.txt", do_lower_case=do_lower_case)
        self._session = None

    def _get_session(self):
        if self._session is None:
            import onnxruntime as ort

            opts = ort.SessionOptions()
            opts.intra_op_num_threads = max(1, min(4, os.cpu_count() or 2))
            opts.inter_op_num_threads = 1
            self._session = ort.InferenceSession(
                str(self.dir / "model.onnx"), sess_options=opts, providers=["CPUExecutionProvider"]
            )
        return self._session

    def encode(self, texts: list[str], batch_size: int = 16):
        """Return an (n, dim) float32 array of L2-normalized CLS vectors."""
        import numpy as np

        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        session = self._get_session()
        rows: list = []
        for start in range(0, len(texts), batch_size):
            batch = texts[start : start + batch_size]
            encoded = [self.tokenizer.encode(t, self.max_tokens) for t in batch]
            width = max(len(e) for e in encoded)
            ids = np.zeros((len(encoded), width), dtype=np.int64)
            mask = np.zeros((len(encoded), width), dtype=np.int64)
            for row, e in enumerate(encoded):
                ids[row, : len(e)] = e
                mask[row, : len(e)] = 1
            vector = session.run(
                ["embedding"],
                {"input_ids": ids, "attention_mask": mask, "token_type_ids": np.zeros_like(ids)},
            )[0]
            vector = np.asarray(vector, dtype=np.float32)
            norms = np.linalg.norm(vector, axis=1, keepdims=True)
            rows.append(vector / np.where(norms > 1e-9, norms, 1.0))
        return np.concatenate(rows, axis=0)

    def status(self) -> dict:
        files = self.manifest.get("files") or {}
        onnx = files.get("model.onnx") or {}
        return {
            "id": self.manifest.get("id", MODEL_ID),
            "available": True,
            "dir": str(self.dir),
            "license": self.manifest.get("license", LICENSE),
            "source": self.manifest.get("source", SOURCE_URL),
            "dim": self.dim,
            "max_tokens": self.max_tokens,
            "size_bytes": onnx.get("bytes"),
            "sha256": (onnx.get("sha256") or "")[:16] or None,
            "note": NOTE,
            "reason": None,
        }


_cache: dict[str, BgeEncoder] = {}


def load_encoder() -> BgeEncoder | None:
    """The shared instance, or None when the model is not installed / unreadable."""
    directory = model_dir()
    key = str(directory)
    if key in _cache:
        return _cache[key]
    if not (directory / "model.onnx").is_file() or not (directory / "manifest.json").is_file():
        return None
    try:
        encoder = BgeEncoder(directory)
    except Exception:  # noqa: BLE001 - a broken install must never break an analysis run
        log.warning("bge encoder unreadable at %s", directory, exc_info=True)
        return None
    _cache[key] = encoder
    return encoder


def bge_status() -> dict:
    encoder = load_encoder()
    if encoder is not None:
        return encoder.status()
    directory = model_dir()
    return {
        "id": MODEL_ID,
        "available": False,
        "dir": str(directory),
        "license": LICENSE,
        "source": SOURCE_URL,
        "dim": 512,
        "max_tokens": DEFAULT_MAX_TOKENS,
        "size_bytes": None,
        "sha256": None,
        "note": NOTE,
        "reason": "模型文件不完整" if (directory / "model.onnx").is_file() else "尚未安装（见 tools/bge_model.py）",
    }
