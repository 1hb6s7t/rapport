"""Lint the prior cards: every MBTI type has >= 3 cards over >= 3 tendencies, every card's lean is a
valid pole, and no card text contains diagnostic language. Zero cost, no network.

    uv run python ../scripts/prior_cards_lint.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from rapport.knowledge.prior_check import TENDENCY_POLES, cards_for, coverage_report  # noqa: E402

FORBIDDEN = re.compile(r"障碍|病|诊断|人格分裂|自恋|抑郁症|焦虑症")


def main() -> int:
    bad = 0
    for mbti, n in sorted(coverage_report().items()):
        cards = cards_for({"mbti": mbti})
        keys = {c.tendency_key for c in cards}
        ok = n >= 3 and len(keys) >= 3
        for c in cards:
            a, b = TENDENCY_POLES[c.tendency_key][:2]
            if c.lean not in (a, b) or FORBIDDEN.search(c.text) or FORBIDDEN.search(c.counterexample_prompt):
                ok = False
        print(f"{mbti}: cards={n} tendencies={len(keys)} {'OK' if ok else 'FAIL'}")
        bad += 0 if ok else 1
    print(f"DONE types=16 failed={bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
