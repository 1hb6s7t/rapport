"""Token and cost estimation. Conservative; estimate != actual usage."""

from __future__ import annotations

import re
from dataclasses import dataclass

from rapport.config import PROVIDER_PRESETS, Price
from rapport.llm.limits import CONTEXT_WINDOW_DEFAULT, REASONING_MULTIPLIER

_CJK = re.compile(r"[㐀-鿿豈-﫿　-〿＀-￯]")


def estimate_tokens(text: str) -> int:
    cjk = len(_CJK.findall(text))
    other = len(text) - cjk
    return int(cjk * 1.0 + other / 3.5) + 1


@dataclass
class CostEstimate:
    tokens_in: int
    tokens_out: int
    cny: float | None
    currency: str
    price_known: bool
    price_date: str
    over_threshold: bool
    note: str


REASONING_OUTPUT_FACTOR = REASONING_MULTIPLIER  # kept for callers; the single source is llm.limits


# ---- context budget planning (v2 M03) ------------------------------------------------
# Reference allocation for a 16K context, from the v2 main report §2.3. Measured token
# counts decide the final split; the numbers below are the *ceiling* per section.

DEFAULT_ALLOCATION: dict[str, int] = {
    "task": 1500,
    "reference_examples": 2000,
    "episode": 6000,
    "related_history": 2000,
    "summary": 1000,
    "output": 2000,
    "reserve": 1500,
}
# Trim order: public reference examples first, then the rolling summary, then related
# history. Everything else is protected.
TRIM_ORDER = ("reference_examples", "summary", "related_history")
PROTECTED = ("task", "episode", "output", "reserve")


@dataclass
class ContextBudget:
    total: int
    fits: bool
    allocations: dict[str, int]
    reduced: list[str]
    overflow_note: str | None = None

    def as_dict(self) -> dict:
        return {"total": self.total, "fits": self.fits, "allocations": dict(self.allocations), "reduced": list(self.reduced), "note": self.overflow_note}


def plan_context_budget(actual: dict[str, int], total_tokens: int = CONTEXT_WINDOW_DEFAULT) -> ContextBudget:
    """Fit measured section sizes into the context window.

    `actual` maps section name -> estimated tokens (task, reference_examples, episode,
    related_history, summary). Output and reserve are added from DEFAULT_ALLOCATION.

    Trimming never touches `task`, `episode` or the output reserve: boundary and refusal
    evidence lives inside the episode, and dropping it to save space would silently change
    the judgement. If the protected sections alone do not fit, the plan reports `fits=False`
    and the caller must ask the user to narrow the range instead.
    """
    alloc = dict(DEFAULT_ALLOCATION)
    for key, value in actual.items():
        if key in alloc:
            alloc[key] = max(0, int(value))
    for key in ("task", "episode"):
        alloc[key] = min(alloc[key], DEFAULT_ALLOCATION[key])
    reduced: list[str] = []
    fits = sum(alloc.values()) <= total_tokens
    for key in TRIM_ORDER:
        if fits:
            break
        if alloc.get(key, 0) > 0:
            alloc[key] = 0
            reduced.append(key)
            fits = sum(alloc.values()) <= total_tokens
    note = None
    if not fits:
        note = (
            "即使去掉公共参考例子与摘要，当前 episode 仍然超出上下文预算；"
            "请缩小区间或拆分为多次分析，不会为了塞进去而裁剪边界与拒绝证据。"
        )
    return ContextBudget(total=total_tokens, fits=fits, allocations=alloc, reduced=reduced, overflow_note=note)


def estimate_cost(tokens_in: int, tokens_out: int, preset: str, threshold_cny: float, price: Price | None = None) -> CostEstimate:
    """`tokens_out` must already include the reasoning multiplier (each stage applies it once via
    `llm.limits.budget`); this function no longer inflates it a second time."""
    p = price or PROVIDER_PRESETS[preset].price
    if p.input_per_m is None or p.output_per_m is None:
        return CostEstimate(tokens_in, tokens_out, None, p.currency, False, p.verified_on, True, "价格未知，无法估算费用；继续前请自行确认供应商定价。")
    cny = tokens_in / 1e6 * p.input_per_m + tokens_out / 1e6 * p.output_per_m
    return CostEstimate(
        tokens_in,
        tokens_out,
        round(cny, 4),
        p.currency,
        True,
        p.verified_on,
        cny > threshold_cny,
        f"按 {p.verified_on} 核对的价格估算（输入 {p.input_per_m}/M，输出 {p.output_per_m}/M）。实际费用以供应商账单为准。",
    )


def actual_cost(prompt_tokens: int, completion_tokens: int, preset: str) -> float | None:
    p = PROVIDER_PRESETS.get(preset)
    if not p or p.price.input_per_m is None or p.price.output_per_m is None:
        return None
    return round(prompt_tokens / 1e6 * p.price.input_per_m + completion_tokens / 1e6 * p.price.output_per_m, 4)
