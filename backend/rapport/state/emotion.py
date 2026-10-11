"""EmotionMixin: the rules-mode offline layer switch (设置 → 分析与个性化).

Two engines share one switch, both fully offline:

- ``grounded`` (default): BGE-small-zh retrieval over this project's labelled corpus, a
  temperature-calibrated emotion model, and ConvoKit-zh politeness cues
- ``structbert``: the bundled single-sentence emotion model alone (the earlier, simpler layer)

Off by default — rules mode stays pure word-list unless the user turns this on. Values are
clamped on read, so a hand-edited database cannot ask for an unbounded run. The models are never
hard dependencies: when one is missing the switch still works and the status view says so.
"""

from __future__ import annotations

import logging

from rapport.db import Setting

log = logging.getLogger("rapport.state")

EMOTION_KEY = "emotion.config"
ENGINES = ("grounded", "structbert")
DEFAULT_EMOTION_CONFIG = {
    "enabled": False,
    "engine": "grounded",
    "max_messages": 120,
    "use_markers": True,
    "use_changepoints": True,
}
MAX_MESSAGES_RANGE = (10, 500)


def _config_from(v: dict) -> dict:
    enabled = bool(v.get("enabled", DEFAULT_EMOTION_CONFIG["enabled"]))
    engine = str(v.get("engine") or DEFAULT_EMOTION_CONFIG["engine"])
    if engine not in ENGINES:
        engine = DEFAULT_EMOTION_CONFIG["engine"]
    try:
        n = int(v.get("max_messages", DEFAULT_EMOTION_CONFIG["max_messages"]))
    except (TypeError, ValueError):
        n = DEFAULT_EMOTION_CONFIG["max_messages"]
    lo, hi = MAX_MESSAGES_RANGE
    return {
        "enabled": enabled,
        "engine": engine,
        "max_messages": max(lo, min(hi, n)),
        "use_markers": bool(v.get("use_markers", DEFAULT_EMOTION_CONFIG["use_markers"])),
        "use_changepoints": bool(v.get("use_changepoints", DEFAULT_EMOTION_CONFIG["use_changepoints"])),
    }


def engines_status() -> dict:
    """Availability of every offline component; each block degrades independently."""
    from rapport.emotion.model import emotion_model_status

    status: dict = {"emotion_model": emotion_model_status()}
    try:
        from rapport.embed import bge_status

        status["bge"] = bge_status()
    except Exception:  # noqa: BLE001
        log.warning("bge status unavailable", exc_info=True)
    try:
        from rapport.grounded.anchors import anchors_status

        status["anchors"] = anchors_status()
    except Exception:  # noqa: BLE001
        log.warning("anchors status unavailable", exc_info=True)
    try:
        from rapport.grounded.calibrate import calibration_status

        status["calibration"] = calibration_status()
    except Exception:  # noqa: BLE001
        log.warning("calibration status unavailable", exc_info=True)
    try:
        from rapport.grounded import markers

        status["markers"] = markers.status()
    except Exception:  # noqa: BLE001
        log.warning("markers status unavailable", exc_info=True)
    return status


class EmotionMixin:
    def emotion_config(self) -> dict:
        with self.db.session() as s:
            row = s.get(Setting, EMOTION_KEY)
            v = dict(row.value) if row and isinstance(row.value, dict) else {}
        return _config_from(v)

    def set_emotion_config(self, patch: dict) -> dict:
        with self.db.session() as s:
            row = s.get(Setting, EMOTION_KEY) or Setting(key=EMOTION_KEY, value={})
            merged = {**(row.value or {}), **{k: v for k, v in patch.items() if v is not None}}
            config = _config_from(merged)
            row.value = {} if config == DEFAULT_EMOTION_CONFIG else config
            s.merge(row)
        return config

    def emotion_config_view(self) -> dict:
        return {
            "config": self.emotion_config(),
            "default": dict(DEFAULT_EMOTION_CONFIG),
            "engines": ENGINES,
            "model": engines_status(),
        }
