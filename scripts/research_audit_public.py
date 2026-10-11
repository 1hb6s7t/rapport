"""Validate locally downloaded official MELD dyadic splits, never loads pickles."""

import argparse
import json
from collections import Counter
from pathlib import Path

from rapport.research.public_data import read_meld_dyadic

parser = argparse.ArgumentParser()
parser.add_argument("folder", type=Path)
args = parser.parse_args()
examples, manifest = read_meld_dyadic(args.folder)
manifest["counts"] = dict(Counter(e.split for e in examples))
manifest["dialogues"] = {split: len({e.group for e in examples if e.split == split}) for split in ("train", "validation", "test")}
manifest["emotion_counts"] = dict(Counter(label for e in examples for label in e.labels["emotion"]))
print(json.dumps(manifest, ensure_ascii=False, indent=2))
