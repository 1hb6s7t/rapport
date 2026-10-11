"""Tendency poles and prior cards (v0.5 plan §4).

Each of the six communication tendencies gets two named poles. A prior card says which pole a
label (MBTI type) *predicts* for a tendency, and gives a counter-example prompt so the reader
knows what would refute it. `check_priors` then compares every card against what the model
actually observed and reports supported / contradicted / untested — with the tendency's own
evidence ids, so the verdict is traceable.

Everything here is a product hypothesis written by this project. It is deliberately not a
personality theory: the poles are about how someone writes in a chat, nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rapport.knowledge.priors import MBTI_PRIORS

# tendency -> (pole_a, pole_b, label words that imply pole_a, label words that imply pole_b)
TENDENCY_POLES: dict[str, tuple[str, str, tuple[str, ...], tuple[str, ...]]] = {
    "directness": ("direct", "indirect", ("直接", "直白", "干脆", "明确", "直说"), ("含蓄", "委婉", "绕", "铺垫", "暗示")),
    "emotional_expression": ("open", "reserved", ("外露", "外放", "明显", "丰富", "直接表达"), ("内敛", "克制", "含蓄", "少", "隐藏", "掩盖")),
    "detail_preference": ("detail", "gist", ("细节", "具体", "详细", "长"), ("概括", "简短", "要点", "短", "大概")),
    "need_expression": ("explicit", "implicit", ("明说", "直接提", "会说", "主动提"), ("不说", "暗示", "被动", "等对方", "很少提")),
    "disagreement_handling": ("confront", "avoid", ("直面", "反驳", "争论", "当场", "据理"), ("回避", "沉默", "让步", "转移", "冷处理")),
    "rhythm": ("fast", "slow", ("快", "即时", "秒回", "密集"), ("慢", "间隔", "延迟", "慢热", "稀疏")),
}

POLE_LABELS: dict[str, str] = {
    "direct": "偏直接", "indirect": "偏含蓄",
    "open": "情绪外露", "reserved": "情绪内敛",
    "detail": "重细节", "gist": "重概括",
    "explicit": "明说需求", "implicit": "不太提需求",
    "confront": "直面分歧", "avoid": "回避分歧",
    "fast": "节奏快", "slow": "节奏慢",
    "unclear": "不明",
}


def infer_pole(tendency_key: str, label: str) -> str:
    """Map a free-text tendency label to a pole by keyword; `unclear` when neither side matches."""
    spec = TENDENCY_POLES.get(tendency_key)
    if not spec or not label:
        return "unclear"
    a, b, a_words, b_words = spec
    ha = any(w in label for w in a_words)
    hb = any(w in label for w in b_words)
    if ha and not hb:
        return a
    if hb and not ha:
        return b
    return "unclear"


@dataclass(frozen=True)
class PriorCard:
    """One testable claim a label makes about one tendency."""

    label: str  # mbti
    value: str  # e.g. INTJ
    tendency_key: str
    lean: str  # pole name from TENDENCY_POLES
    counterexample_prompt: str  # what in the chat would refute it
    text: str  # the sentence shown to the user


@dataclass
class PriorCheck:
    card: PriorCard
    status: str  # supported | contradicted | untested
    observed_pole: str = "unclear"
    observed_label: str = ""
    evidence_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "label": self.card.label,
            "value": self.card.value,
            "tendency_key": self.card.tendency_key,
            "lean": self.card.lean,
            "lean_label": POLE_LABELS.get(self.card.lean, self.card.lean),
            "text": self.card.text,
            "counterexample_prompt": self.card.counterexample_prompt,
            "status": self.status,
            "observed_pole": self.observed_pole,
            "observed_label": self.observed_label,
            "evidence_ids": list(self.evidence_ids),
        }


# Three cards per type, each tied to one tendency. Written from the same one-line priors in
# `knowledge/priors.py`; the pole is what those sentences already imply.
_T = {
    # type: (directness, emotional_expression, detail_preference, need_expression, disagreement_handling, rhythm)
    "INTJ": ("direct", "reserved", "gist", None, "confront", "slow"),
    "INTP": (None, "reserved", "detail", "implicit", "avoid", "slow"),
    "ENTJ": ("direct", None, "gist", "explicit", "confront", "fast"),
    "ENTP": ("direct", "open", None, None, "confront", "fast"),
    "INFJ": ("indirect", "reserved", None, "implicit", "avoid", None),
    "INFP": ("indirect", "open", None, "implicit", "avoid", None),
    "ENFJ": (None, "open", None, "implicit", "avoid", "fast"),
    "ENFP": ("direct", "open", "gist", None, None, "fast"),
    "ISTJ": ("direct", "reserved", "detail", None, "confront", None),
    "ISFJ": ("indirect", "reserved", "detail", "implicit", "avoid", None),
    "ESTJ": ("direct", "reserved", None, "explicit", "confront", "fast"),
    "ESFJ": (None, "open", None, "explicit", None, "fast"),
    "ISTP": ("direct", "reserved", "gist", "implicit", "avoid", "slow"),
    "ISFP": ("indirect", "reserved", None, "implicit", "avoid", None),
    "ESTP": ("direct", "open", "gist", None, "confront", "fast"),
    "ESFP": ("direct", "open", None, "explicit", None, "fast"),
}
_KEYS = ("directness", "emotional_expression", "detail_preference", "need_expression", "disagreement_handling", "rhythm")
_COUNTER = {
    "directness": {"direct": "对方常绕着说、先铺垫再表态", "indirect": "对方常一句话就把立场说死"},
    "emotional_expression": {"open": "对方很少用情绪词，语气平", "reserved": "对方情绪词和感叹密集"},
    "detail_preference": {"detail": "对方回复多是一两句概括", "gist": "对方经常长段展开细节"},
    "need_expression": {"explicit": "对方从不主动提自己想要什么", "implicit": "对方会直接说\"我想要…\""},
    "disagreement_handling": {"confront": "有分歧时对方沉默或转移话题", "avoid": "有分歧时对方当场反驳"},
    "rhythm": {"fast": "对方回复间隔常以小时计", "slow": "对方常秒回、连发"},
}


def cards_for(labels: dict[str, str | None]) -> list[PriorCard]:
    mbti = (labels or {}).get("mbti")
    if not mbti or mbti not in _T:
        return []
    out: list[PriorCard] = []
    base = MBTI_PRIORS.get(mbti, "")
    for key, lean in zip(_KEYS, _T[mbti], strict=True):
        if not lean:
            continue
        out.append(
            PriorCard(
                label="mbti",
                value=mbti,
                tendency_key=key,
                lean=lean,
                counterexample_prompt=_COUNTER[key][lean],
                text=f"{mbti} 的起始假设：{POLE_LABELS[lean]}。" + (f"（{base}）" if base else ""),
            )
        )
    return out


def check_priors(cards: list[PriorCard], tendencies: dict) -> list[PriorCheck]:
    """Compare each card with the observed tendency. `tendencies` is Persona.tendencies
    (key -> Tendency-like with .label/.pole/.evidence_ids or a dict)."""
    out: list[PriorCheck] = []
    for c in cards:
        t = tendencies.get(c.tendency_key) if tendencies else None
        if t is None:
            out.append(PriorCheck(c, "untested"))
            continue
        label = getattr(t, "label", None) if not isinstance(t, dict) else t.get("label", "")
        pole = getattr(t, "pole", None) if not isinstance(t, dict) else t.get("pole")
        ev = getattr(t, "evidence_ids", None) if not isinstance(t, dict) else t.get("evidence_ids")
        if not pole or pole == "unclear":
            pole = infer_pole(c.tendency_key, label or "")
        if pole == "unclear" or not ev:
            out.append(PriorCheck(c, "untested", pole, label or "", list(ev or [])))
        elif pole == c.lean:
            out.append(PriorCheck(c, "supported", pole, label or "", list(ev)))
        else:
            out.append(PriorCheck(c, "contradicted", pole, label or "", list(ev)))
    return out


def coverage_report() -> dict[str, int]:
    """For the lint script: how many tendencies each type covers."""
    return {k: sum(1 for x in v if x) for k, v in _T.items()}
