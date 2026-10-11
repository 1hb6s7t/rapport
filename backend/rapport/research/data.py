"""Snapshot adapters preserve sender identity and explicit temporal visibility."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass


@dataclass
class Example:
    id: str
    group: str
    split: str
    utterances: list[str]
    speakers: list[int]
    target: int
    labels: dict[str, list[str]]
    post_hoc: bool


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def prepare(rows: list[dict], *, visibility="causal", split_protocol="friend", max_context=8, limits=None):
    if visibility not in {"causal", "offline"} or split_protocol not in {"friend", "time"}:
        raise ValueError("Unknown visibility/split protocol")
    examples = []
    rejected = Counter()
    seen = {}
    source_splits = {}
    for row in rows:
        if not row.get("training_eligible") or row.get("quality") == "weak":
            rejected["ineligible_or_weak"] += 1
            continue
        split = row.get("split" if split_protocol == "friend" else "time_split")
        if split not in {"train", "validation", "test"}:
            rejected["excluded_boundary"] += 1
            continue
        if row["id"] in seen:
            if seen[row["id"]] != split:
                raise ValueError("Message crosses dataset split")
            rejected["duplicate_id"] += 1
            continue
        seen[row["id"]] = split
        for ref in row.get("content_refs", []):
            if ref in source_splits and source_splits[ref] != split:
                raise ValueError("Repeated content crosses dataset split")
            source_splits[ref] = split
        target_index = row.get("target_index")
        context = []
        target = None
        for message in row.get("context", []):
            # Dataset IDs encode index but numeric `index` is preferable when present.
            index = message.get("index", message.get("message_index"))
            if index is None and message.get("id"):
                tail = str(message["id"]).rsplit(":", 1)[-1]
                index = int(tail) if tail.isdigit() else None
            is_target = message.get("text") == row.get("text") and (index == target_index if index is not None else False)
            if visibility == "causal" and index is not None and target_index is not None and index > target_index:
                continue
            if is_target:
                target = len(context)
            context.append(message)
        if target is None:
            context.append({"sender": "other", "text": row.get("text", "")})
            target = len(context) - 1
        if visibility == "causal":
            context = context[:target + 1]
        start = max(0, target - max_context + 1)
        stop = min(len(context), target + max_context) if visibility == "offline" else target + 1
        context = context[start:stop]
        target -= start
        example = Example(str(row["id"]), str(row.get("session_id") or row.get("conversation_id")), split,
                          [str(m.get("text") or "")[:1200] for m in context],
                          [0 if m.get("sender") == "me" else 1 for m in context], target,
                          {k: sorted(set(v if isinstance(v, list) else [v])) for k, v in row.get("labels", {}).items()
                           if k in {"emotion", "intent"} and v}, bool(row.get("post_hoc")))
        examples.append(example)
    # Deterministic order/subsampling, never use held-out labels to balance classes.
    examples.sort(key=lambda x: digest(x.id))
    if limits:
        selected = []
        counts = Counter()
        for e in examples:
            if counts[e.split] < limits.get(e.split, 10**12):
                selected.append(e)
                counts[e.split] += 1
        examples = selected
    return examples, {"counts": dict(Counter(e.split for e in examples)), "rejected": dict(rejected),
                      "source_groups": {split: len({e.group for e in examples if e.split == split}) for split in ("train", "validation", "test")},
                      "visibility": visibility, "split_protocol": split_protocol,
                      "ids_hash": digest([e.id for e in examples]), "examples_hash": digest([asdict(e) for e in examples]),
                      "labels": "ai_assisted_development_not_human_gold", "production_eligible": False,
                      "post_hoc_labels": sum(e.post_hoc for e in examples)}


def vocabulary(examples):
    # Label vocabulary is learned exclusively from training records.
    return {task: sorted({label for e in examples if e.split == "train" for label in e.labels.get(task, [])})
            for task in ("emotion", "intent")}


def predecessor_edges(speakers: list[int], window_same_speaker=1):
    """DAG-ERC author's dataset.get_adj_v1 predecessor selection."""
    edges = []
    for i, speaker in enumerate(speakers):
        count = 0
        for j in range(i - 1, -1, -1):
            edges.append((i, j))
            if speakers[j] == speaker:
                count += 1
                if count == window_same_speaker:
                    break
    return edges
