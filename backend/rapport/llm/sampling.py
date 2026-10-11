"""Sampling consistency for the intent reading (v0.5 plan §3B).

The model's `model_confidence_raw` is a self-report and does not track correctness (docs/EVAL.md
§15.3: high-confidence error rate 0.29 on real material). Agreement across k independent draws
at a non-zero temperature is an observable property of the judgement, not a claim about it.

    run_intent_sampled(provider, inp, k=3, temperature=0.7)

returns one merged raw dict plus the draws: a tag survives when it appears in >= 2 of 3 draws,
a tag seen once becomes a candidate, and `consistency` is the share of draws that agree with the
merged tag set. The merged dict has exactly the shape `validate_intent` already accepts, so the
gate, the evidence rules and the contract are untouched.
"""

from __future__ import annotations

from collections import Counter

from rapport.llm.provider import CallUsage, LLMError, LLMProvider

DEFAULT_K = 3
DEFAULT_TEMPERATURE = 0.7
MAJORITY = 2  # of DEFAULT_K


def _tag_names(entries) -> list[str]:
    out: list[str] = []
    for t in entries or []:
        name = t.get("tag") if isinstance(t, dict) else t
        if isinstance(name, str) and name and name not in out:
            out.append(name)
    return out


def _basis_of(entries, name: str) -> list[str]:
    for t in entries or []:
        if isinstance(t, dict) and t.get("tag") == name:
            ids = t.get("basis_ids") or []
            return [i for i in ids if isinstance(i, str)]
    return []


def merge_samples(samples: list[dict], majority: int = MAJORITY) -> dict:
    """Combine k raw readings into one raw reading (same JSON shape the prompt asks for).

    - pragmatic_tags / emotions: keep tags present in >= `majority` samples; basis ids are the
      union of that tag's basis across the samples that gave it.
    - tags present in exactly one sample become candidates ("N 次判断中出现 1 次").
    - boundary_status: the most severe value any majority of samples agrees on, else the modal one.
    - evidence / counter evidence: union. ambiguous: majority vote.
    - consistency: mean over samples of Jaccard(sample tags, merged tags) — 1.0 means every draw
      said the same thing; 0 means no draw matched the merged set.
    """
    usable = [s for s in samples if isinstance(s, dict)]
    if not usable:
        return {}
    k = len(usable)
    majority = min(majority, k)
    merged: dict = {}
    for field in ("pragmatic_tags", "emotions"):
        counts: Counter[str] = Counter()
        for s in usable:
            counts.update(_tag_names(s.get(field)))
        kept = [t for t, n in counts.items() if n >= majority]
        merged[field] = [
            {"tag": t, "basis_ids": sorted({i for s in usable for i in _basis_of(s.get(field), t)}), "why": f"{counts[t]}/{k} 次采样一致"}
            for t in kept
        ]
        merged[f"_{field}_once"] = [t for t, n in counts.items() if n < majority]
    # candidates: the model's own alternatives plus tags that appeared only once
    cands: list[dict] = []
    seen: set[str] = set()
    for s in usable:
        for c in s.get("candidates") or []:
            if isinstance(c, dict) and str(c.get("reading", "")).strip() and c["reading"] not in seen:
                seen.add(c["reading"])
                cands.append(c)
    for t in merged.pop("_pragmatic_tags_once") + merged.pop("_emotions_once"):
        text = f"{t}（{k} 次采样中只出现 1 次）"
        if text not in seen:
            seen.add(text)
            cands.append({"reading": text, "likelihood": "low", "basis_ids": []})
    merged["candidates"] = cands[:3]
    sev = {"none": 0, "explicit_decline": 1, "stop_contact": 2, "safety_concern": 3}
    bvals = [str(s.get("boundary_status") or "none") for s in usable]
    bcount = Counter(bvals)
    agreed = [b for b, n in bcount.items() if n >= majority]
    merged["boundary_status"] = max(agreed, key=lambda b: sev.get(b, 0)) if agreed else bcount.most_common(1)[0][0]
    merged["evidence_ids"] = sorted({i for s in usable for i in (s.get("evidence_ids") or []) if isinstance(i, str)})
    merged["counter_evidence_ids"] = sorted({i for s in usable for i in (s.get("counter_evidence_ids") or []) if isinstance(i, str)})
    merged["ambiguous"] = sum(1 for s in usable if s.get("ambiguous")) * 2 > k
    reasons = [str(s.get("abstain_reason")) for s in usable if s.get("abstain_reason")]
    merged["abstain_reason"] = reasons[0] if reasons and merged["ambiguous"] else None
    strengths = Counter(str(s.get("evidence_strength") or "low") for s in usable)
    merged["evidence_strength"] = strengths.most_common(1)[0][0]
    confs = [float(s["model_confidence_raw"]) for s in usable if isinstance(s.get("model_confidence_raw"), (int, float))]
    merged["model_confidence_raw"] = sum(confs) / len(confs) if confs else None
    merged["literal_affect"] = next((str(s.get("literal_affect")) for s in usable if s.get("literal_affect")), "")
    # consistency
    merged_tags = {t["tag"] for t in merged["pragmatic_tags"]} | {t["tag"] for t in merged["emotions"]}
    scores = []
    for s in usable:
        st = set(_tag_names(s.get("pragmatic_tags"))) | set(_tag_names(s.get("emotions")))
        union = st | merged_tags
        scores.append(1.0 if not union else len(st & merged_tags) / len(union))
    merged["consistency"] = round(sum(scores) / len(scores), 3)
    merged["samples"] = k
    return merged


def run_intent_sampled(
    provider: LLMProvider,
    inp,
    k: int = DEFAULT_K,
    temperature: float = DEFAULT_TEMPERATURE,
    max_tokens: int | None = None,
) -> tuple[dict, CallUsage, list[dict]]:
    """k independent draws, merged. A draw that fails is skipped (never silently); if every draw
    fails the last error is raised so the caller records it like a single-call failure."""
    from rapport.llm.intent import INTENT_MAX_TOKENS, INTENT_SYSTEM, _budget

    user = inp.user_prompt()
    budget = _budget(max_tokens or INTENT_MAX_TOKENS, bool(getattr(provider, "reasoning", False)))
    usage = CallUsage()
    draws: list[dict] = []
    last_err: LLMError | None = None
    for _ in range(max(1, k)):
        try:
            data, u = provider.complete_json(INTENT_SYSTEM, user, max_tokens=budget, temperature=temperature)
        except LLMError as e:
            last_err = e
            continue
        usage.add(u)
        if isinstance(data, dict):
            draws.append(data)
    if not draws:
        assert last_err is not None
        raise last_err
    merged = merge_samples(draws)
    merged["_failed_draws"] = max(1, k) - len(draws)
    return merged, usage, draws
