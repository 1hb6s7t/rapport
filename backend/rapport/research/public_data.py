"""Read official MELD dyadic CSV splits, without redistribution or unsafe pickle loads."""

from __future__ import annotations

import csv
import hashlib
from collections import defaultdict
from pathlib import Path

from rapport.research.data import Example

MELD_REVISION = "e8cedf27b5d2877e198332c957127e16eb214afe"


def read_meld_dyadic(folder: Path, *, max_context=32, causal=False):
    examples = []
    files = {}
    for split, name in (("train", "train_sent_emo_dya.csv"), ("validation", "dev_sent_emo_dya.csv"), ("test", "test_sent_emo_dya.csv")):
        path = folder / name
        files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        dialogues = defaultdict(list)
        with path.open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                dialogues[row["Dialogue_ID"]].append(row)
        for ident, rows in sorted(dialogues.items()):
            rows.sort(key=lambda r: int(r["Utterance_ID"]))
            people = sorted({row["Speaker"] for row in rows})
            if len(people) > 2:
                raise ValueError("MELD dyadic input contains more than two speakers")
            for target, row in enumerate(rows):
                start = max(0, target - max_context + 1)
                stop = target + 1 if causal else min(len(rows), target + max_context)
                context = rows[start:stop]
                examples.append(Example(f"meld:{split}:{ident}:{row['Utterance_ID']}", f"meld:{split}:{ident}", split,
                                        [r["Utterance"] for r in context], [people.index(r["Speaker"]) for r in context], target - start,
                                        {"emotion": [row["Emotion"]]}, not causal))
    return examples, {"dataset": "MELD_Dyadic", "upstream_commit": MELD_REVISION, "file_sha256": files,
                      "license": "official repository GPL-3.0; research CSV not redistributed by Rapport",
                      "original_split_preserved": True, "paper_score_reproduction": False}
