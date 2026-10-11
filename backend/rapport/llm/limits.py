"""Model-call limits in one place: token budgets, reasoning multiplier, window sizes.

Before this module the ×3 reasoning multiplier lived in four files and the cost estimator
applied a second ×2.5 on top, so a reasoning model's estimated output was inflated 7.5× and
the cost warning fired far too early. One multiplier, used exactly once per path.
"""

from __future__ import annotations

# Hidden-reasoning models spend completion tokens thinking before the answer. This is both the
# generation budget multiplier and the cost-estimate multiplier — applied once, not twice.
REASONING_MULTIPLIER = 3

EXTRACT_MAX_TOKENS = 2000
SYNTH_MAX_TOKENS = 4000
INTENT_MAX_TOKENS = 2000
REPLY_MAX_TOKENS = 1500

EXTRACT_WINDOW = 120
EXTRACT_OVERLAP = 10

# Estimated tokens the synthesis input and per-window outputs carry, for the preview.
SYNTH_INPUT_OVERHEAD = 2500
EXTRACT_OUTPUT_PER_WINDOW = 600
SYNTH_OUTPUT = 2500

CONTEXT_WINDOW_DEFAULT = 16384

# Local (Ollama) context windows per task. Ollama's default num_ctx (2048/4096) silently drops the
# head of a 120-message window; these are what the prompts actually need with headroom.
LOCAL_CTX: dict[str, int] = {"extract": 8192, "synthesize": 16384, "intent": 8192, "reply": 8192}
TASKS = ("extract", "synthesize", "intent", "reply")


def budget(base: int, reasoning: bool) -> int:
    return base * (REASONING_MULTIPLIER if reasoning else 1)
