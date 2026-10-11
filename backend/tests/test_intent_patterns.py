"""Wiring of the pragmatic-pattern reference layer into the product pipeline.

The layer was built because wording-level examples did not generalise (docs/EVAL.md §12), so what
matters here is that it is *optional*, off by default, and that when it is on it reaches the model
as reference rules — never as evidence.
"""

from __future__ import annotations

from rapport.jobs.pipeline import ByokContext, run_byok
from rapport.knowledge.reference import load_patterns
from rapport.llm.extract import build_payload
from rapport.llm.fake import FakeProvider
from rapport.parsers import ParseOptions, parse
from rapport.schemas.profile import Profile

CHAT = "\n".join(f"[2026-08-{d:02d} 20:00] {'A' if d % 2 else 'B'}: 消息{d} 今天有点累" for d in range(1, 16))


def _conv():
    return parse(CHAT, "c1", "paste", None, ParseOptions())[0]


def _run(use_patterns: bool) -> FakeProvider:
    conv = _conv()
    fake = FakeProvider()
    ctx = ByokContext(
        provider=fake,
        payload=build_payload(conv, conv.events(), window_size=40, overlap=5),
        preset="ollama",
        use_patterns=use_patterns,
    )
    run_byok(conv, conv.events(), Profile(), ctx, report_id="r1")
    return fake


def test_pattern_layer_ships_as_original_structural_rules():
    patterns = load_patterns()
    assert len(patterns) >= 12, "the layer should carry a useful set of structural rules"
    assert all(p.structure and p.implies for p in patterns)
    assert all(p.contrast for p in patterns), "every pattern needs its contrast, or it invites over-reading"
    assert len({p.pattern_id for p in patterns}) == len(patterns)


def test_patterns_are_off_by_default_in_the_pipeline():
    sent = "\n".join(system + user for system, user in _run(use_patterns=False).calls)
    assert "语用模式" not in sent, "off must mean the block is not sent at all"


def test_turning_patterns_on_adds_the_reference_block():
    sent = "\n".join(system + user for system, user in _run(use_patterns=True).calls)
    assert "语用模式" in sent
    assert "不要依赖具体措辞" in sent, "the block must tell the model to judge by structure"
    assert "不要引用其编号" in sent, "patterns are reference material, not citable evidence"


def test_pattern_block_does_not_become_evidence():
    """A pattern id must never appear where evidence ids are expected."""
    conv = _conv()
    fake = FakeProvider()
    ctx = ByokContext(provider=fake, payload=build_payload(conv, conv.events(), window_size=40, overlap=5), preset="ollama", use_patterns=True)
    rep = run_byok(conv, conv.events(), Profile(), ctx, report_id="r1")
    for reading in rep.intent_readings:
        assert all(not i.startswith("pat:") for i in reading.evidence_ids)
        assert all(not i.startswith("pat:") for i in reading.reference_example_ids)
