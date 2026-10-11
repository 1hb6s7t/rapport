"""Public reference examples for cold-start pragmatics (v2 M03 §"公共参考库的准入与检索").

Access rules, in order of importance:

1. **Only approved entries load.** Anything whose `license_review_status` is not exactly
   `approved_for_local_reference` is skipped, and the loader reports how many it skipped.
   This project is MIT and non-commercial, but that does not grant rights to third-party
   corpora — CPED's repo licence does not clear its underlying film material, Dialogue-MPDD's
   licence is unspecified, and ESConv is academic-use-only. None of them may be bundled.
2. **Evaluation material never enters the library.** Ids reserved for evaluation (`eval:`)
   are rejected outright, so a probe can never be retrieved as its own hint.
3. Retrieval here is **lexical** (character bigrams), not semantic. It is enough to pick a
   few analogous examples at this scale; embeddings are a later phase and are not simulated.

Everything shipped in `reference_examples/` is original text written for this project.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

STATUS_APPROVED = "approved_for_local_reference"
RESERVED_EVAL_PREFIX = "eval:"
DEFAULT_DIR = Path(__file__).parent / "reference_examples"


@dataclass(frozen=True)
class ReferenceExample:
    example_id: str
    layer: str  # pragmatic | affect
    context: tuple[str, ...]
    text: str
    pragmatic_tags: tuple[str, ...] = ()
    emotions: tuple[str, ...] = ()
    source_dataset: str = ""
    source_url: str = ""
    license_review_status: str = ""
    revision: int = 1
    note: str = ""

    def prompt_dict(self) -> dict:
        return {
            "example_id": self.example_id,
            "layer": self.layer,
            "context": list(self.context),
            "text": self.text,
            "pragmatic_tags": list(self.pragmatic_tags),
            "emotions": list(self.emotions),
        }


@dataclass(frozen=True)
class ReferencePattern:
    """A structural rule: what a *shape* of exchange usually implies, independent of wording.

    Original project content (see knowledge/reference_examples/patterns.json), written as
    hypotheses with their contrasts rather than as validated pragmatics.
    """

    pattern_id: str
    structure: str
    implies: tuple[str, ...]
    contrast: str = ""
    why: str = ""

    def prompt_dict(self) -> dict:
        return {
            "pattern_id": self.pattern_id,
            "structure": self.structure,
            "implies": list(self.implies),
            "contrast": self.contrast,
            "why": self.why,
        }


@dataclass
class ReferenceLibrary:
    examples: list[ReferenceExample] = field(default_factory=list)
    skipped_unapproved: list[str] = field(default_factory=list)
    skipped_reserved: list[str] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)

    def by_layer(self, layer: str) -> list[ReferenceExample]:
        return [e for e in self.examples if e.layer == layer]

    def get(self, example_id: str) -> ReferenceExample | None:
        return next((e for e in self.examples if e.example_id == example_id), None)


def load_patterns(directory: Path | None = None) -> list[ReferencePattern]:
    """Load the pragmatic-pattern layer. Separate from the example library on purpose: patterns
    are meant to generalise past wording, so they are neither retrieved by lexical overlap nor
    counted as evidence.
    """
    path = (directory or DEFAULT_DIR) / "patterns.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    out: list[ReferencePattern] = []
    for raw in data.get("patterns", []):
        pid = str(raw.get("pattern_id", ""))
        structure = str(raw.get("structure", "")).strip()
        implies = [str(x) for x in raw.get("implies", []) if x]
        if not pid or not structure or not implies:
            continue
        out.append(
            ReferencePattern(
                pattern_id=pid,
                structure=structure,
                implies=tuple(implies),
                contrast=str(raw.get("contrast", "")),
                why=str(raw.get("why", "")),
            )
        )
    return out


def char_bigrams(text: str) -> set[str]:
    """Character bigrams — the lexical scorer shared by the reference library and diary search."""
    return _bigrams(text)


def _bigrams(text: str) -> set[str]:
    t = "".join(ch for ch in text if ch.strip())
    return {t[i : i + 2] for i in range(len(t) - 1)} if len(t) > 1 else ({t} if t else set())


def _score(query_bg: set[str], ex: ReferenceExample) -> float:
    target = _bigrams(" ".join(ex.context) + " " + ex.text)
    if not query_bg or not target:
        return 0.0
    inter = len(query_bg & target)
    return inter / (len(query_bg) ** 0.5 * len(target) ** 0.5)


def load_library(directory: Path | None = None) -> ReferenceLibrary:
    """Load every approved example from JSON files in `directory` (defaults to the shipped seeds)."""
    directory = directory or DEFAULT_DIR
    lib = ReferenceLibrary()
    if not directory.is_dir():
        return lib
    for path in sorted(directory.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for raw in data.get("examples", []):
            eid = str(raw.get("example_id", ""))
            status = str(raw.get("license_review_status", STATUS_APPROVED))
            if eid.startswith(RESERVED_EVAL_PREFIX):
                lib.skipped_reserved.append(eid)
                continue
            if status != STATUS_APPROVED:
                lib.skipped_unapproved.append(f"{eid}({status})")
                continue
            lib.examples.append(
                ReferenceExample(
                    example_id=eid,
                    layer=str(raw.get("layer", "pragmatic")),
                    context=tuple(str(c) for c in raw.get("context", [])),
                    text=str(raw.get("text", "")),
                    pragmatic_tags=tuple(str(t) for t in raw.get("pragmatic_tags", [])),
                    emotions=tuple(str(e) for e in raw.get("emotions", [])),
                    source_dataset=str(raw.get("source_dataset", "")),
                    source_url=str(raw.get("source_url", "")),
                    license_review_status=status,
                    revision=int(raw.get("revision", 1) or 1),
                    note=str(raw.get("note", "")),
                )
            )
        lib.sources.append({"file": path.name, "declared_schema": data.get("schema", ""), "note": data.get("note", "")})
    return lib


def retrieve(query: str, library: ReferenceLibrary, k: int = 4, layer: str = "pragmatic") -> list[ReferenceExample]:
    """Top-k analogous examples by lexical overlap. Deterministic ordering, ties broken by id."""
    q = _bigrams(query)
    scored = [(_score(q, e), e.example_id, e) for e in library.by_layer(layer)]
    scored = [s for s in scored if s[0] > 0.0]
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [e for _, _, e in scored[: max(0, k)]]
