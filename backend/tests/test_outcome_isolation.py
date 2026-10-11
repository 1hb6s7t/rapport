"""Outcome labels and action logs are records, never inputs.

The promise shown to the user — 「结果标签只存本机、只用于回看与方向校准，永远不进入好感度分数」 — is
only worth something if a scoring module cannot reach those tables at all. This pins that at the
source level: nothing under `rapport/scoring/` may import or name them. A behavioural test would
pass the day someone wires it up behind a flag; this one fails.
"""

from __future__ import annotations

from pathlib import Path

import rapport.scoring as scoring

FORBIDDEN = (
    "ActionLog",
    "Outcome",
    "state.actions",
    "action_done",
    "actions_for_",
    "outcome_record",
    "outcome_prompt",
    "outcomes_for_relationship",
)


def _scoring_sources() -> list[Path]:
    root = Path(scoring.__file__).resolve().parent
    files = sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)
    assert files, "scoring package looks empty — the guard below would pass vacuously"
    return files


def test_scoring_never_names_the_outcome_or_action_tables():
    offenders = []
    for path in _scoring_sources():
        text = path.read_text(encoding="utf-8")
        for token in FORBIDDEN:
            if token in text:
                offenders.append(f"{path.name}: {token}")
    assert not offenders, "scoring must not read user-reported outcomes or action logs: " + ", ".join(offenders)


def test_outcome_ask_window_is_two_weeks():
    from rapport.state.actions import OUTCOME_ASK_AFTER_DAYS

    # The history page shows the check-in only after this many days; a shorter window would ask
    # about a change the user cannot have observed yet.
    assert OUTCOME_ASK_AFTER_DAYS == 14
