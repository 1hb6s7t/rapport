"""Transparent fusion of the offline signals into per-message `IntentReading`s.

Signals, all local and all inspectable:

- ``structbert``: the bundled StructBERT-tiny emotion model, temperature-calibrated
- ``bge_knn``: similarity-weighted vote of the k nearest labelled sentences (BGE embeddings),
  with each class's mass divided by its anchor count so a rare class (伤心 has 21 anchors against
  300 for the common ones) is not drowned out by a crowded one — see ``anchors.PRIOR_ALPHA``
- ``centroid``: cosine to each label's anchor centroid (a cheap prototype classifier)
- ``markers``: ConvoKit-zh politeness cues, shown as evidence, never as a decision

Fusion is a fixed weighted blend with an explicit *neutral* path, because the reported failure
mode of the single-sentence model was labelling links, group notices and plain chit-chat as
情绪. Rules that follow from that, and are visible in the output rather than hidden:

- obvious non-emotional text (URLs, emails, code/ids, notices) -> 无明显情绪, no vote needed
- a top label below ``NEUTRAL_THRESHOLD`` -> 无明显情绪; a top-two margin below
  ``MARGIN_THRESHOLD`` -> 不确定 with the runner-up kept as a candidate
- a negative label (不满/伤心) needs >= 2 of the 3 model signals to agree, otherwise it is
  demoted to 不确定 and the reason is carried in ``suppressed_reason``

The weights are product hypotheses (like the rubric anchors); the evidence for or against them
is the offline evaluation in ``tools/evaluate_grounded.py``, not the numbers in this file.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from rapport.emotion.model import RAW_TO_FIXED, load_model
from rapport.grounded import markers
from rapport.grounded.anchors import AnchorIndex, Neighbor, load_index
from rapport.grounded.calibrate import apply_temperature, calibration_status
from rapport.schemas.message_v1 import Conversation, Message, Sender
from rapport.schemas.report_v1 import Confidence, IntentCandidate, IntentReading, NeighborExample

log = logging.getLogger("rapport.grounded")

NEUTRAL = "无明显情绪"
UNSURE = "不确定"
NEGATIVE_EMOTIONS = frozenset({"不满", "伤心"})

# Chosen on the dataset *validation* split (tools/evaluate_grounded.py --split validation, 889 rows,
# 2026-10-10): retrieval alone reached macro-F1 0.259; adding the single-sentence model at 0.45
# weight dropped the fused result to 0.173, at 0.10 to 0.216, and dropping it entirely gave 0.230.
# It is kept at a minor weight so the negative-label agreement check has three signals to count.
WEIGHTS = {"structbert": 0.10, "bge_knn": 0.65, "centroid": 0.25}
# Same validation split: neutral 0.42 gave macro-F1 0.216 / acc 0.552; 0.35 gave 0.252 / 0.538 with
# 99.7% coverage; 0.30 gave 0.254 / 0.503. 0.35 keeps the minority classes without losing accuracy.
NEUTRAL_THRESHOLD = 0.35
MARGIN_THRESHOLD = 0.05
KNN_K = 15
NEIGHBORS_SHOWN = 3
MAX_TEXT_CHARS = 200

URL_RE = re.compile(r"https?://|www\.|t\.me/|\.com/|\.cn/|\.io/", re.I)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
CODE_HINT_RE = re.compile(r"[{}<>_=/\\|`]{1}|^[A-Za-z0-9_\-:.]{12,}$")
CJK_RE = re.compile(r"[一-鿿]")


@dataclass
class GroundedResult:
    text: str
    emotion: str
    scores: dict[str, float]
    votes: dict[str, str]
    neighbors: list[Neighbor] = field(default_factory=list)
    cues: tuple[str, ...] = ()
    struct_prob: float | None = None
    ambiguous: bool = False
    neutral_reason: str | None = None
    suppressed_reason: str | None = None
    abstain_reason: str | None = None
    candidates: list[str] = field(default_factory=list)


def looks_neutral(text: str) -> str | None:
    """A reason string when the text is plainly not emotional, else None."""
    stripped = (text or "").strip()
    if len(stripped) < 2:
        return "内容过短，不构成情绪线索"
    if URL_RE.search(stripped) or EMAIL_RE.search(stripped):
        return "链接/邮箱/联系方式，通常不带情绪"
    has_cjk = bool(CJK_RE.search(stripped))
    if not has_cjk and CODE_HINT_RE.search(stripped):
        return "代码/编号类文本，通常不带情绪"
    if not has_cjk and len(stripped) > 8:
        return "非中文文本，情绪线索不足"
    return None


def _to_fixed(raw_probs: dict[str, float]) -> dict[str, float]:
    out: dict[str, float] = {}
    for raw, value in raw_probs.items():
        fixed = RAW_TO_FIXED.get(raw)
        if fixed:
            out[fixed] = out.get(fixed, 0.0) + float(value)
    return out


def _blend(signals: dict[str, dict[str, float]]) -> dict[str, float]:
    """Weighted mean over the signals that are present, renormalized over available weights."""
    available = {name: dist for name, dist in signals.items() if dist}
    if not available:
        return {}
    total_weight = sum(WEIGHTS[name] for name in available)
    labels = {label for dist in available.values() for label in dist}
    return {
        label: sum(WEIGHTS[name] * dist.get(label, 0.0) for name, dist in available.items()) / total_weight
        for label in labels
    }


def _argmax(scores: dict[str, float]) -> tuple[str | None, float, float]:
    if not scores:
        return None, 0.0, 0.0
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    top, value = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    return top, float(value), float(value - second)


class GroundedClassifier:
    """Encoder + anchor index + calibrated emotion model, fused per message."""

    def __init__(self, encoder, index: AnchorIndex | None, model, use_markers: bool = True):
        self.encoder = encoder
        self.index = index
        self.model = model
        self.use_markers = use_markers

    @classmethod
    def build(cls, use_markers: bool = True) -> GroundedClassifier | None:
        encoder = None
        try:
            from rapport.embed import load_encoder

            encoder = load_encoder()
        except Exception:  # noqa: BLE001
            log.warning("bge encoder unavailable", exc_info=True)
        model = load_model()
        if encoder is None and model is None:
            return None
        index = load_index() if encoder is not None else None
        return cls(encoder, index, model, use_markers=use_markers)

    @property
    def engine_names(self) -> list[str]:
        names = []
        if self.model is not None:
            names.append("structbert")
        if self.encoder is not None and self.index is not None:
            names.append("bge_knn")
        if self.use_markers:
            names.append("markers")
        return names

    def classify(self, texts: list[str]) -> list[GroundedResult]:
        struct_dist: list[dict[str, float]] = [{} for _ in texts]
        struct_prob: list[float | None] = [None] * len(texts)
        if self.model is not None and texts:
            predictions = self.model.predict([t[:MAX_TEXT_CHARS] for t in texts])
            for i, pred in enumerate(predictions):
                struct_dist[i] = apply_temperature(_to_fixed(pred.probs))
                struct_prob[i] = float(pred.prob)

        vectors = None
        if self.encoder is not None and self.index is not None and texts:
            vectors = self.encoder.encode([t[:MAX_TEXT_CHARS] for t in texts])

        results: list[GroundedResult] = []
        for i, text in enumerate(texts):
            knn = self.index.knn_scores(vectors[i], KNN_K) if vectors is not None else {}
            cent = self.index.centroid_scores(vectors[i]) if vectors is not None else {}
            neighbors = self.index.nearest(vectors[i], NEIGHBORS_SHOWN) if vectors is not None else []
            cues = markers.cues_for(text) if self.use_markers else ()
            scores = _blend({"structbert": struct_dist[i], "bge_knn": knn, "centroid": cent})
            votes = {
                "structbert": (_argmax(struct_dist[i])[0] or "—"),
                "bge_knn": (_argmax(knn)[0] or "—"),
                "centroid": (_argmax(cent)[0] or "—"),
            }
            results.append(self._decide(text, scores, votes, neighbors, cues, struct_prob[i]))
        return results

    def _decide(
        self,
        text: str,
        scores: dict[str, float],
        votes: dict[str, str],
        neighbors: list[Neighbor],
        cues: tuple[str, ...],
        struct_prob: float | None,
    ) -> GroundedResult:
        neutral_reason = looks_neutral(text)
        top, value, margin = _argmax(scores)
        ranked = [label for label, _ in sorted(scores.items(), key=lambda kv: -kv[1]) if label != NEUTRAL][:2]
        ambiguous = False
        suppressed = None
        abstain = None
        candidates: list[str] = []
        if neutral_reason is not None or top is None:
            emotion = NEUTRAL
        elif value < NEUTRAL_THRESHOLD:
            emotion = NEUTRAL
            neutral_reason = f"最高信号强度只有 {value:.0%}，不足以判断具体情绪"
            candidates = ranked
        elif top in NEGATIVE_EMOTIONS:
            agree = sum(1 for name, label in votes.items() if label == top)
            if agree >= 2:
                emotion = top
            else:
                emotion = UNSURE
                ambiguous = True
                suppressed = "单句模型对负面情绪容易高估（本项目实测）：这条只作提示，结合上下文再判断。"
                abstain = f"{top} 只有一个信号支持，保留为候选。"
                candidates = ranked
        elif margin < MARGIN_THRESHOLD:
            emotion = UNSURE
            ambiguous = True
            abstain = f"{top} 与次高选项接近（相差 {margin:.0%}），无法定论。"
            candidates = ranked
        else:
            emotion = top
        return GroundedResult(
            text=text,
            emotion=emotion,
            scores={k: round(v, 4) for k, v in sorted(scores.items(), key=lambda kv: -kv[1])},
            votes=votes,
            neighbors=neighbors,
            cues=cues,
            struct_prob=struct_prob,
            ambiguous=ambiguous,
            neutral_reason=neutral_reason,
            suppressed_reason=suppressed,
            abstain_reason=abstain,
            candidates=candidates,
        )

    def status(self) -> dict:
        return {
            "engines": self.engine_names,
            "anchors": self.index.status() if self.index is not None else None,
            "calibration": calibration_status(),
            "markers": markers.status() if self.use_markers else None,
        }


def _reading(msg: Message, result: GroundedResult, engine: str) -> IntentReading:
    scores_text = " · ".join(f"{k} {v:.0%}" for k, v in list(result.scores.items())[:3])
    affect = f"研究层判断：{result.emotion}（{scores_text}）" if scores_text else f"研究层判断：{result.emotion}"
    if result.struct_prob is not None:
        affect += f"；单句模型自评 {result.struct_prob:.0%}（已校准）"
    return IntentReading(
        msg_id=msg.id,
        speaker="other",
        surface_text=(msg.text or "")[:160],
        literal_affect=affect,
        pragmatic_tags=list(result.cues),
        emotions=[result.emotion],
        boundary_status="none",
        evidence_strength=(
            Confidence.medium
            if result.emotion not in {NEUTRAL, UNSURE} and not result.ambiguous
            else Confidence.low
        ),
        model_confidence_raw=round(result.struct_prob * 100, 1) if result.struct_prob is not None else None,
        evidence_ids=[msg.id],
        post_hoc=False,
        context_mode="reply",
        ambiguous=result.ambiguous,
        abstain_reason=result.abstain_reason or result.neutral_reason,
        suppressed_reason=result.suppressed_reason,
        candidates=[IntentCandidate(reading=c, likelihood=Confidence.low, basis_ids=[msg.id]) for c in result.candidates],
        samples=1,
        independent_units=sum(1 for label in result.votes.values() if label == result.emotion),
        method_votes=dict(result.votes),
        similar_examples=[NeighborExample(label=n.label, score=n.score) for n in result.neighbors],
        source="grounded",
    )


def readings_from_conversation(
    conv: Conversation, start: int, end: int, config: dict, engine: str = "grounded"
) -> list[IntentReading]:
    """Classify the other person's most recent text messages in [start, end]; [] when absent."""
    limit = max(0, int(config.get("max_messages") or 0))
    if not limit:
        return []
    messages = [
        m
        for m in conv.messages
        if start <= m.seq <= end and m.sender == Sender.other and m.is_text and (m.text or "").strip()
    ][-limit:]
    if not messages:
        return []
    classifier = GroundedClassifier.build(use_markers=bool(config.get("use_markers", True)))
    if classifier is None:
        return []
    results = classifier.classify([(m.text or "") for m in messages])
    out = [_reading(m, r, engine) for m, r in zip(messages, results, strict=True)]
    log.info("grounded layer produced %d readings (%s)", len(out), ",".join(classifier.engine_names))
    return out
