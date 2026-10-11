"""Chinese politeness / pragmatic markers (ConvoKit-zh, MIT) as *observable cues*.

The vendored extractor lives in `rapport.research.vendor.convokit_zh` and tokenizes with jieba.
It was built for the research interaction study; this module is the production-facing wrapper:
a per-message marker set, cached, plus a deliberately tiny mapping to the product's pragmatic
tags. Two rules keep it honest:

- 礼貌词不等于情绪 (the marker's own words): a `please` is evidence of a request, never of warmth.
- Only markers whose meaning is unambiguous in Chinese chat map to a tag; everything else stays
  an evidence-only cue shown next to the reading.

jieba is an optional dependency: when it is missing this module returns no markers and the layer
keeps working on the other signals.
"""

from __future__ import annotations

import functools
import logging
import re

log = logging.getLogger("rapport.grounded")

# ConvoKit strategy -> product pragmatic tag. Kept short on purpose (see module docstring).
MARKER_TO_CUES: dict[str, tuple[str, ...]] = {
    "please": ("询问",),
    "can_you": ("询问",),
    "could_you": ("询问",),
    "factuality": ("信息确认",),
}

CUES_VERSION = "markers-v1"
MAX_MARKER_CHARS = 200


def _sentences(text: str) -> list[list[dict]]:
    import jieba

    return [
        [{"tok": token} for token in jieba.cut(sentence, HMM=False) if token.strip()]
        for sentence in re.split(r"(?<=[。！？!?])", text)
        if sentence.strip()
    ]


@functools.lru_cache(maxsize=4096)
def markers_for(text: str) -> tuple[str, ...]:
    """Active ConvoKit-zh politeness markers in one message; () when jieba is unavailable."""
    text = (text or "")[:MAX_MARKER_CHARS]
    if not text.strip():
        return ()
    try:
        from rapport.research.vendor.convokit_zh.strategy_extractor import (
            get_chinese_politeness_strategy_features,
        )

        features, _ = get_chinese_politeness_strategy_features(_sentences(text))
        return tuple(sorted(name for name, present in features.items() if present))
    except Exception:  # noqa: BLE001 - optional dependency; never break an analysis run
        log.debug("marker extraction skipped", exc_info=True)
        return ()


def cues_for(text: str) -> tuple[str, ...]:
    """Pragmatic tags supported by markers in this message (deduped, stable order)."""
    cues: list[str] = []
    for marker in markers_for(text):
        for tag in MARKER_TO_CUES.get(marker, ()):
            if tag not in cues:
                cues.append(tag)
    return tuple(cues)


def status() -> dict:
    available = True
    reason = None
    try:
        import jieba  # noqa: F401
    except Exception:  # noqa: BLE001
        available = False
        reason = "jieba 未安装，礼貌标记不可用"
    return {
        "available": available,
        "version": CUES_VERSION,
        "source": "ConvoKit-zh (MIT) Chinese politeness strategies",
        "markers": sorted(MARKER_TO_CUES),
        "reason": reason,
    }
