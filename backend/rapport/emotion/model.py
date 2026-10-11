"""Bundled offline emotion classifier (StructBERT-emotion-tiny via ONNX Runtime).

Provenance and limits, stated once here and surfaced in the UI:

- ModelScope ``iic/nlp_structbert_emotion-classification_chinese-tiny`` (Apache-2.0): a public
  7-class single-sentence emotion classifier (恐惧/愤怒/厌恶/喜好/悲伤/高兴/惊讶). It is NOT
  calibrated on this project's material, so confidence stays raw and low scores stay `ambiguous`.
- The mapping to the product's fixed emotion set (`RAW_TO_FIXED`) is a product decision.
- Runs fully offline through onnxruntime (already a dependency for ASR); no torch in the app.
- Objective affinity never reads these predictions — same contract as the label prior.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from rapport.config import get_settings
from rapport.emotion.tokenizer import BertTokenizer

log = logging.getLogger("rapport.emotion")

MODEL_ID = "iic/nlp_structbert_emotion-classification_chinese-tiny"
MODEL_DIRNAME = "emotion-zh-tiny"
SOURCE_URL = f"https://www.modelscope.cn/models/{MODEL_ID}"
LICENSE = "Apache License 2.0"
NOTE = "离线小模型：只判断单句情绪，未经本项目数据校准；不进入客观分数。"
AMBIGUOUS_BELOW = 0.5

# 7-class upstream labels -> the product's fixed emotion set (label_map_v2).
RAW_TO_FIXED = {
    "高兴": "开心",
    "喜好": "亲近",
    "悲伤": "伤心",
    "愤怒": "不满",
    "厌恶": "不满",
    "恐惧": "担忧",
    "惊讶": "惊讶困惑",
}


def model_dir() -> Path:
    """Env override, then the app tree, then the data dir (for repo installs without models/)."""
    override = os.environ.get("RAPPORT_EMOTION_MODEL_DIR")
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


@dataclass(frozen=True)
class Prediction:
    raw: str
    prob: float
    probs: dict[str, float]

    @property
    def mapped(self) -> str | None:
        return RAW_TO_FIXED.get(self.raw)

    @property
    def ambiguous(self) -> bool:
        return self.prob < AMBIGUOUS_BELOW


class LocalEmotionModel:
    def __init__(self, directory: Path):
        self.dir = directory
        self.manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        self.labels = [str(x) for x in (self.manifest.get("labels") or [])]
        if len(self.labels) < 2:
            raise ValueError("manifest.json carries no label list")
        self.max_len = int(self.manifest.get("max_len") or 128)
        self.tokenizer = BertTokenizer(directory / "vocab.txt")
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

    def predict(self, texts: list[str], batch_size: int = 16) -> list[Prediction]:
        import numpy as np

        session = self._get_session()
        out: list[Prediction] = []
        for start in range(0, len(texts), batch_size):
            batch = texts[start : start + batch_size]
            encoded = [self.tokenizer.encode(t, self.max_len) for t in batch]
            width = max(len(e) for e in encoded)
            ids = np.zeros((len(encoded), width), dtype=np.int64)
            mask = np.zeros((len(encoded), width), dtype=np.int64)
            for row, e in enumerate(encoded):
                ids[row, : len(e)] = e
                mask[row, : len(e)] = 1
            logits = session.run(
                ["logits"],
                {"input_ids": ids, "attention_mask": mask, "token_type_ids": np.zeros_like(ids)},
            )[0]
            logits = np.asarray(logits, dtype=np.float64)
            probs = np.exp(logits - logits.max(axis=1, keepdims=True))
            probs /= probs.sum(axis=1, keepdims=True)
            for row in range(len(batch)):
                p = probs[row]
                top = int(p.argmax())
                out.append(
                    Prediction(
                        raw=self.labels[top],
                        prob=float(p[top]),
                        probs={self.labels[j]: round(float(p[j]), 4) for j in range(len(self.labels))},
                    )
                )
        return out

    def status(self) -> dict:
        files = self.manifest.get("files") or {}
        onnx = files.get("model.onnx") or {}
        return {
            "id": self.manifest.get("id", MODEL_ID),
            "available": True,
            "dir": str(self.dir),
            "license": self.manifest.get("license", LICENSE),
            "source": self.manifest.get("source", SOURCE_URL),
            "labels": list(self.labels),
            "size_bytes": onnx.get("bytes"),
            "sha256": (onnx.get("sha256") or "")[:16] or None,
            "max_len": self.max_len,
            "note": NOTE,
            "reason": None,
        }


_cache: dict[str, LocalEmotionModel] = {}


def load_model() -> LocalEmotionModel | None:
    """The shared instance, or None when the model is not installed / unreadable."""
    directory = model_dir()
    key = str(directory)
    if key in _cache:
        return _cache[key]
    if not (directory / "model.onnx").is_file() or not (directory / "manifest.json").is_file():
        return None
    try:
        model = LocalEmotionModel(directory)
    except Exception:  # noqa: BLE001 - a broken install must never break an analysis run
        log.warning("emotion model unreadable at %s", directory, exc_info=True)
        return None
    _cache[key] = model
    return model


def emotion_model_status() -> dict:
    model = load_model()
    if model is not None:
        return model.status()
    directory = model_dir()
    return {
        "id": MODEL_ID,
        "available": False,
        "dir": str(directory),
        "license": LICENSE,
        "source": SOURCE_URL,
        "labels": [],
        "size_bytes": None,
        "sha256": None,
        "max_len": 128,
        "note": NOTE,
        "reason": "模型文件不完整" if (directory / "model.onnx").is_file() else "尚未安装（见 tools/emotion_model.py）",
    }
