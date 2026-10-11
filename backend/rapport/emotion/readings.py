"""Turn the offline classifier's per-sentence output into report `IntentReading`s.

Contract, matching the AI-mode readings so downstream code needs no special case:

- one reading per 对方 text message; `evidence_ids` = that message, nothing else
- `post_hoc=False` / `context_mode="reply"`: the label uses only the message itself, so it was
  computable when the message arrived (reply drafting may reuse it as an online fact)
- `source="local_model"` so every surface can label the provenance
- objective scoring never reads these (same boundary as the label prior)
"""

from __future__ import annotations

import logging

from rapport.emotion.model import Prediction, load_model
from rapport.schemas.message_v1 import Conversation, Message, Sender
from rapport.schemas.report_v1 import Confidence, IntentReading

log = logging.getLogger("rapport.emotion")

NEGATIVE = frozenset({"不满", "伤心"})
CONFIDENT_ABOVE = 0.8
MAX_TEXT_CHARS = 200


def _reading(msg: Message, pred: Prediction) -> IntentReading:
    mapped = pred.mapped or "不确定"
    caution = None
    if mapped in NEGATIVE:
        # Measured on this project's material: sentence-level models over-read coldness
        # ("我好想你" lands on 悲伤). A single sentence is a hint, never a conclusion.
        caution = "单句模型对负面情绪容易高估（本项目实测）：这条只作提示，结合上下文再判断。"
    return IntentReading(
        msg_id=msg.id,
        speaker="other",
        surface_text=(msg.text or "")[:160],
        literal_affect=f"离线小模型判断：{pred.raw} {pred.prob:.0%}",
        emotions=[mapped],
        boundary_status="none",
        evidence_strength=Confidence.medium if pred.prob >= CONFIDENT_ABOVE else Confidence.low,
        model_confidence_raw=round(pred.prob * 100, 1),
        evidence_ids=[msg.id],
        post_hoc=False,
        context_mode="reply",
        ambiguous=pred.ambiguous,
        abstain_reason=(
            f"小模型对这句的把握约 {pred.prob:.0%}（未经校准），另一种情绪也可能。"
            if pred.ambiguous
            else None
        ),
        suppressed_reason=caution,
        samples=1,
        independent_units=1,
        source="local_model",
    )


def readings_from_conversation(
    conv: Conversation, start: int, end: int, max_messages: int = 120
) -> list[IntentReading]:
    """Classify the other person's most recent text messages in [start, end]; [] when absent."""
    model = load_model()
    if model is None:
        return []
    limit = max(0, int(max_messages))
    if not limit:
        return []
    messages = [
        m
        for m in conv.messages
        if start <= m.seq <= end and m.sender == Sender.other and m.is_text and (m.text or "").strip()
    ][-limit:]
    if not messages:
        return []
    predictions = model.predict([(m.text or "")[:MAX_TEXT_CHARS] for m in messages])
    out = [_reading(m, p) for m, p in zip(messages, predictions, strict=True)]
    log.info("local emotion model produced %d readings", len(out))
    return out
