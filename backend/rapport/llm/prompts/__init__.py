"""Prompt loading and versioning.

`PROMPTS_VERSION` used to be a hand-written constant that nobody bumped, so editing a prompt
left the payload hash, the idempotency key and the snapshot comparability rule all believing
nothing had changed. The version is now derived from the prompt files themselves: a short
content hash over every `*.md` in this directory, in name order. Edit a prompt and every
downstream key changes with it.
"""

from __future__ import annotations

import hashlib
from functools import cache
from pathlib import Path

PROMPT_DIR = Path(__file__).parent
PROMPT_FAMILY = "prompts-v1"  # bump only for a deliberate contract change; content hash tracks edits


@cache
def load_prompt(name: str) -> str:
    return (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")


@cache
def prompts_digest() -> str:
    h = hashlib.sha256()
    for p in sorted(PROMPT_DIR.glob("*.md")):
        h.update(p.name.encode())
        h.update(p.read_bytes())
    return h.hexdigest()[:10]


@cache
def prompts_version() -> str:
    return f"{PROMPT_FAMILY}+{prompts_digest()}"
