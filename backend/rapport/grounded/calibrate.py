"""Temperature scaling for the bundled emotion model, fitted on this project's validation split.

The upstream classifier is trained on other material, so its softmax is over-confident here.
Temperature scaling (Guo et al. 2017, "On Calibration of Modern Neural Networks") fits a single
T on held-out data by minimizing NLL; the runtime applies it as power scaling on the probability
vector, which is the same operation without needing logits or torch:

    q_i = p_i ** (1/T) / sum_j p_j ** (1/T)

The fitted value, the ECE before/after and the split it was fitted on live in
``models/calibration-zh-v1.json`` (written by ``tools/fit_calibration.py``). Missing file =>
identity (T = 1) and the status says "未校准".
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from rapport.config import get_settings

log = logging.getLogger("rapport.grounded")

CALIBRATION_FILENAME = "calibration-zh-v1.json"


def calibration_path() -> Path:
    override = Path(__file__).resolve().parents[3] / "models" / CALIBRATION_FILENAME
    if override.is_file():
        return override
    return get_settings().data_dir / "models" / CALIBRATION_FILENAME


def load_calibration() -> dict | None:
    path = calibration_path()
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("calibration unreadable at %s", path, exc_info=True)
        return None


def temperature() -> float:
    data = load_calibration() or {}
    try:
        value = float(data.get("temperature") or 1.0)
    except (TypeError, ValueError):
        return 1.0
    return value if 0.05 <= value <= 20 else 1.0


def apply_temperature(probs: dict[str, float], t: float | None = None) -> dict[str, float]:
    """Power-scale a probability dict; returns a renormalized copy (identity when t == 1)."""
    if not probs:
        return {}
    value = temperature() if t is None else float(t)
    if abs(value - 1.0) < 1e-6:
        return dict(probs)

    power = 1.0 / value
    scaled = {k: max(float(v), 1e-12) ** power for k, v in probs.items()}
    total = sum(scaled.values()) or 1.0
    return {k: v / total for k, v in scaled.items()}


def calibration_status() -> dict:
    data = load_calibration()
    if data is None:
        return {"available": False, "temperature": 1.0, "fitted_on": None, "ece_before": None,
                "ece_after": None, "note": "未校准：使用上游模型的原始置信度。"}
    return {
        "available": True,
        "temperature": temperature(),
        "fitted_on": data.get("fitted_on"),
        "ece_before": data.get("ece_before"),
        "ece_after": data.get("ece_after"),
        "labels": data.get("labels"),
        "note": data.get("note") or "温度标定：在本项目验证集上拟合，用于修正过度自信。",
    }
