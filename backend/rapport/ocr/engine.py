"""Local PaddleOCR models through RapidOCR/ONNX; chat images never leave this machine."""

from __future__ import annotations

import hashlib
import io
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache

_MAGIC = {b"\x89PNG": "png", b"\xff\xd8\xff": "jpeg", b"RIFF": "webp", b"BM": "bmp"}
_INFERENCE_LOCK = threading.RLock()
_CHAT_CACHE_LOCK = threading.RLock()
_CHAT_CACHE: OrderedDict[tuple[str, tuple[int, int], str, str], tuple[float, tuple[OcrBox, ...]]] = OrderedDict()
_CHAT_CACHE_TTL_S = 15
_ACTIVE_DEVICE = "待初始化"
_FAST_DEVICE = "待初始化"
_LAST_DEVICE = "待初始化"
GPU_DEVICE = "GPU · DirectML"
CPU_FALLBACK_DEVICE = "CPU（显卡被占用）"
MODEL_LABEL = "PP-OCRv5 中文增强识别"


def info() -> dict:
    return {"model": MODEL_LABEL, "device": _ACTIVE_DEVICE, "fast_device": _FAST_DEVICE,
            "last_device": _LAST_DEVICE, "local": True}


def last_device() -> str:
    """Where the most recent chat recognition actually ran (a busy GPU sends it to the CPU)."""
    return _LAST_DEVICE


@dataclass
class OcrBox:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    score: float
    side: str | None = None
    bubble_id: str | None = None
    bubble_y0: float | None = None
    bubble_y1: float | None = None

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def h(self) -> float:
        return self.y1 - self.y0


def available() -> bool:
    try:
        import onnxruntime  # noqa: F401
        import rapidocr  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


def load_image(data: bytes, max_pixels: int):
    from PIL import Image

    if not any(data.startswith(m) for m in _MAGIC):
        raise ValueError("不是支持的图片格式（png/jpeg/webp/bmp）")
    Image.MAX_IMAGE_PIXELS = max_pixels
    try:
        img = Image.open(io.BytesIO(data))
        img.verify()
        img = Image.open(io.BytesIO(data))
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"图片损坏或超过像素上限：{type(e).__name__}") from e
    if img.width * img.height > max_pixels:
        raise ValueError("图片像素超过上限")
    return img.convert("RGB")


def _directml_requested() -> bool:
    # DirectML can fail during session creation on some Windows driver setups.
    # A local CPU override keeps OCR available without changing model weights.
    try:
        import onnxruntime
    except ImportError:
        return False
    return os.getenv("RAPPORT_OCR_CPU_ONLY") != "1" and "DmlExecutionProvider" in onnxruntime.get_available_providers()


def _build(use_dml: bool):
    from rapidocr import ModelType, OCRVersion, RapidOCR

    from rapport.config import get_settings

    model_dir = get_settings().data_dir / "ocr-models"
    model_dir.mkdir(parents=True, exist_ok=True)
    return RapidOCR(params={
        "Det.model_type": ModelType.SMALL,
        "Det.ocr_version": OCRVersion.PPOCRV6,
        "Rec.model_type": ModelType.SERVER,
        "Rec.ocr_version": OCRVersion.PPOCRV5,
        "Global.log_level": "warning",
        "Global.model_root_dir": str(model_dir),
        "EngineConfig.onnxruntime.intra_op_num_threads": min(4, os.cpu_count() or 1),
        "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        "EngineConfig.onnxruntime.use_dml": use_dml,
    })


@lru_cache(maxsize=1)
def _engine():
    global _ACTIVE_DEVICE
    result = _build(_directml_requested())
    providers = result.text_rec.session.session.get_providers()
    _ACTIVE_DEVICE = GPU_DEVICE if providers[0] == "DmlExecutionProvider" else "CPU"
    return result


@lru_cache(maxsize=1)
def _cpu_engine():
    """The same models on the CPU, for pages read while training or local generation holds the GPU."""
    return _build(False)


@lru_cache(maxsize=1)
def _fast_engine():
    """Small CPU OCR for a selected sticker thumbnail, never a full chat page."""
    from rapidocr import ModelType, OCRVersion, RapidOCR

    from rapport.config import get_settings

    global _FAST_DEVICE
    model_dir = get_settings().data_dir / "ocr-models"
    model_dir.mkdir(parents=True, exist_ok=True)
    result = RapidOCR(params={
        "Det.model_type": ModelType.SMALL,
        "Det.ocr_version": OCRVersion.PPOCRV6,
        "Rec.model_type": ModelType.MOBILE,
        "Rec.ocr_version": OCRVersion.PPOCRV5,
        "Global.log_level": "warning",
        "Global.model_root_dir": str(model_dir),
        "EngineConfig.onnxruntime.intra_op_num_threads": min(4, os.cpu_count() or 1),
        "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        "EngineConfig.onnxruntime.use_dml": False,
    })
    _FAST_DEVICE = "CPU · mobile"
    return result


def _boxes(result) -> list[OcrBox]:
    boxes: list[OcrBox] = []
    b_list = getattr(result, "boxes", None)
    if b_list is not None:
        raw_texts = getattr(result, "txts", None)
        raw_scores = getattr(result, "scores", None)
        txts = [] if raw_texts is None else list(raw_texts)
        scores = [] if raw_scores is None else list(raw_scores)
        for i, quad in enumerate(b_list):
            xs = [p[0] for p in quad]
            ys = [p[1] for p in quad]
            boxes.append(OcrBox(min(xs), min(ys), max(xs), max(ys), str(txts[i]), float(scores[i]) if i < len(scores) else 0.0))
    elif isinstance(result, tuple) and result and result[0]:
        for quad, txt, sc in result[0]:
            xs = [p[0] for p in quad]
            ys = [p[1] for p in quad]
            boxes.append(OcrBox(min(xs), min(ys), max(xs), max(ys), str(txt), float(sc)))
    return boxes


def run_sticker_thumbnail(img) -> list[OcrBox]:
    """Inspect one chosen small tile only; UIA labels are preferred when available."""
    from rapport.errors import RapportError

    if img.width > 512 or img.height > 512 or img.width * img.height > 120_000:
        raise RapportError("sticker_ocr_too_large", "只允许识别选中的表情缩略图。", status=422)
    with _INFERENCE_LOCK:
        return _boxes(_fast_engine()(img))


def run_chat_cached(img) -> list[OcrBox]:
    """Reuse OCR only for pixel-identical chat crops during short preflight loops."""
    from rapport.config import get_settings

    key = (str(get_settings().data_dir), img.size, img.mode,
           hashlib.sha256(img.tobytes()).hexdigest())
    now = time.monotonic()
    with _CHAT_CACHE_LOCK:
        entry = _CHAT_CACHE.get(key)
        if entry and now - entry[0] < _CHAT_CACHE_TTL_S:
            _CHAT_CACHE.move_to_end(key)
            return [OcrBox(**vars(box)) for box in entry[1]]
        result = run(img)
        _CHAT_CACHE[key] = (time.monotonic(), tuple(result))
        _CHAT_CACHE.move_to_end(key)
        while len(_CHAT_CACHE) > 8:
            _CHAT_CACHE.popitem(last=False)
        return [OcrBox(**vars(box)) for box in result]


def run(img) -> list[OcrBox]:
    # RapidOCR interprets ndarray as BGR but handles PIL RGB explicitly. Its
    # callable also mutates shared settings, so serialize construction/inference.
    from rapport.config import get_settings
    from rapport.learning.gpu import GPUBusy, GPULease

    global _LAST_DEVICE
    with _INFERENCE_LOCK:
        if not _directml_requested() or _ACTIVE_DEVICE == "CPU":
            # CPU runtime, or a DirectML session that already fell back: no GPU in use, no lease.
            result = _engine()(img)
            _LAST_DEVICE = _ACTIVE_DEVICE
        else:
            try:
                with GPULease(get_settings().data_dir, "ocr"):
                    result = _engine()(img)
                _LAST_DEVICE = _ACTIVE_DEVICE
            except GPUBusy:
                # Training or local generation holds the GPU for as long as it runs. Recognise
                # on the CPU with the same models: much slower, never blocked, never competing
                # for video memory.
                result = _cpu_engine()(img)
                _LAST_DEVICE = CPU_FALLBACK_DEVICE
    # rapidocr >= 2: result has .boxes/.txts/.scores ; older returns (list, elapse)
    return _boxes(result)
