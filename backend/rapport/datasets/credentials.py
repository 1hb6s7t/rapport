"""Conservative local credential quarantine before private dataset snapshots.

The source exports remain read-only. Matched values are replaced in derived data;
any reply sample touching a replacement is excluded from training and evaluation.
"""

from __future__ import annotations

import re

REDACTED = "[已移除凭证]"
TOKEN = re.compile(
    r"(?<![A-Za-z0-9])(?:(?:sk|tp)-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9_]{20,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{20,}|AIza[A-Za-z0-9_-]{25,}|"
    r"hf_[A-Za-z0-9]{25,}|"
    r"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})"
)
# Placeholders left by an earlier redaction pass (e.g. the handoff copy of the eval
# corpus) still mark a message that once held a credential; keep quarantining it.
PLACEHOLDER = re.compile(r"\[(?:REDACTED_CREDENTIAL|已移除凭证)\]")
ASSIGNMENT = re.compile(
    r"(?i)\b(?:api[_-]?key|access[_-]?token|secret[_-]?key|bearer)\b"
    r"\s*(?::|=|\s)\s*['\"]?([A-Za-z0-9_.-]{24,})"
)


def redact_text(text: str) -> tuple[str, bool]:
    text = str(text or "")
    updated, count = TOKEN.subn(REDACTED, text)
    count += len(PLACEHOLDER.findall(text))
    if ASSIGNMENT.search(updated):
        updated, assignments = ASSIGNMENT.subn(
            lambda match: match.group(0).replace(match.group(1), REDACTED), updated)
        count += assignments
    return updated, bool(count)
