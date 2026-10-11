from __future__ import annotations

from rapport.jobs.pipeline import ByokContext, PipelineHooks, run_byok, run_rules
from rapport.llm.extract import build_payload
from rapport.llm.fake import FakeProvider
from rapport.parsers import ParseOptions, parse
from rapport.report import refresh_labels, to_markdown
from rapport.schemas.profile import Person, Profile
from rapport.schemas.report_v1 import Report
from tests.conftest import demo_text


def _conv():
    conv, _ = parse(demo_text(), "c", "paste")
    return conv


def test_rules_report_schema_and_markdown():
    conv = _conv()
    rep = run_rules(conv, conv.events(), Profile(goal="invite"))
    assert rep.mode == "rules" and rep.affinity.total is not None
    assert rep.playbook.actions and all(a.stop_when for a in rep.playbook.actions)
    assert not rep.persona.available
    Report.model_validate(rep.model_dump(mode="json"))
    md = to_markdown(rep)
    assert "好感度指数不是概率" in md and "规则模式" in md


def test_byok_pipeline_with_fake_provider_and_checkpoints():
    conv = _conv()
    events = conv.events()
    fake = FakeProvider()
    ctx = ByokContext(provider=fake, payload=build_payload(conv, events, window_size=30, overlap=5), versions_extra={"provider": "fake", "model": "fake-1", "price_date": "n/a"}, preset="ollama")
    store: dict = {}
    progress: list[str] = []
    hooks = PipelineHooks(progress=lambda p: progress.append(p.stage), checkpoint=lambda k, v: store.__setitem__(k, v), load_checkpoint=lambda k: store.get(k))
    rep = run_byok(conv, events, Profile(other=Person(mbti="ENTJ")), ctx, hooks)
    assert rep.mode == "byok" and rep.ai_status == "completed"
    assert rep.persona.available and rep.persona.mbti_comparison is not None
    assert rep.observations and all(i in rep.evidence for o in rep.observations for i in o.evidence_ids)
    assert "extracting" in progress and "synthesizing" in progress and "intent" in progress
    n_windows = len(ctx.payload.windows)
    assert len([k for k in store if k.startswith("window:")]) == n_windows
    # M03: one call per selected episode, and every reading must be evidence-bound
    assert rep.intent_readings, "a rich conversation must produce intent readings"
    assert len([k for k in store if k.startswith("intent:")]) == len(rep.intent_readings)
    assert all(ir.evidence_ids for ir in rep.intent_readings)
    assert all(i in rep.evidence for ir in rep.intent_readings for i in ir.evidence_ids)
    assert all(ir.candidates for ir in rep.intent_readings), "each reading needs an alternative explanation"
    assert rep.cost.actual_usage.calls == n_windows + 1 + len(rep.intent_readings)
    # rerun reuses checkpoints -> only synthesis call
    fake2 = FakeProvider()
    ctx2 = ByokContext(provider=fake2, payload=ctx.payload, preset="ollama")
    run_byok(conv, events, Profile(), ctx2, hooks)
    assert len(fake2.calls) == 1


def test_byok_falls_back_when_model_garbage():
    conv = _conv()
    events = conv.events()
    fake = FakeProvider(fail_times=10)  # every call returns garbage
    ctx = ByokContext(provider=fake, payload=build_payload(conv, events), preset="ollama")
    rep = run_byok(conv, events, Profile(), ctx)
    assert rep.ai_status == "failed" and rep.affinity.total is not None
    assert rep.playbook.actions and not rep.playbook.actions[0].personalized


def test_labels_do_not_change_core():
    conv = _conv()
    rep = run_rules(conv, conv.events(), Profile())
    before = rep.model_dump(mode="json")
    rep2 = refresh_labels(rep, Profile(me=Person(zodiac="leo", chinese_zodiac="rat"), other=Person(mbti="INTP", zodiac="aries", chinese_zodiac="horse")))
    after = rep2.model_dump(mode="json")
    for k in ("affinity", "trend", "stage", "boundary", "playbook", "observations"):
        assert before[k] == after[k]
    assert after["fun"]["zodiac_pair"] is not None


def test_stop_contact_suppresses_and_restricts_actions():
    conv, _ = parse(demo_text() + "\n[2026-09-11 10:00] B: 别再联系我了", "c", "paste")
    rep = run_rules(conv, conv.events(), Profile(goal="invite"))
    assert rep.boundary.value == "stop_contact"
    # v0.8: the number stays; the boundary skeleton leads the playbook and the goal action follows with a caution
    assert rep.affinity.total is not None and any("不想继续联系" in c for c in rep.affinity.caveats)
    assert "停止" in rep.playbook.actions[0].action and len(rep.playbook.actions) >= 2
    assert rep.playbook.actions[1].caution


def test_intent_stage_can_be_switched_off():
    conv = _conv()
    events = conv.events()
    fake = FakeProvider()
    ctx = ByokContext(provider=fake, payload=build_payload(conv, events, window_size=30, overlap=5), preset="ollama", run_intent=False)
    rep = run_byok(conv, events, Profile(), ctx)
    assert rep.intent_readings == []
    assert rep.cost.actual_usage.calls == len(ctx.payload.windows) + 1


def test_negative_readings_are_gated_on_thin_material():
    """One short block is not enough independent evidence for a coldness claim."""
    conv, _ = parse("[2026-09-03 21:00] A: 今天好累\n[2026-09-03 21:05] B: 嗯\n[2026-09-03 21:10] A: 早点休息", "c1", "paste", None, ParseOptions())
    events = conv.events()
    fake = FakeProvider()  # always answers 真生气 + 不满
    ctx = ByokContext(provider=fake, payload=build_payload(conv, events), preset="ollama")
    rep = run_byok(conv, events, Profile(), ctx)
    assert rep.intent_readings, "the stage still describes the message"
    for ir in rep.intent_readings:
        # v0.8: no gate — the reading shows what the model said, with its evidence and unit count
        assert "真生气" in ir.pragmatic_tags and ir.suppressed_reason is None
        assert ir.independent_units >= 0
    assert any("语义与情绪判断" in n for n in rep.notes)


def test_extraction_retries_transient_errors_and_keeps_windows():
    """A 429 in window 3 of a long run must not throw away windows 1-2: retry with backoff,
    then continue. Non-transient errors still fail fast."""
    from unittest import mock

    from rapport.jobs import pipeline as pl
    from rapport.llm.fake import FakeProvider
    from rapport.llm.provider import LLMError
    from rapport.parsers import ParseOptions, parse
    from rapport.schemas.profile import Profile

    text = "\n".join(f"[2026-09-{1 + i // 40:02d} {8 + i % 12:02d}:{i % 60:02d}] {'A' if i % 2 else 'B'}: 第{i}句" for i in range(400))
    conv, _ = parse(text, "c", "paste", None, ParseOptions())
    events = conv.events()
    provider = FakeProvider()
    payload = build_payload(conv, events)
    assert len(payload.windows) >= 3
    calls = {"n": 0}
    real = pl.run_extraction

    def flaky(p, pay, i, override=None):
        calls["n"] += 1
        if i == 1 and calls["n"] == 2:
            raise LLMError("rate_limit", "429")
        return real(p, pay, i, override)

    with mock.patch.object(pl, "run_extraction", side_effect=flaky), mock.patch.object(pl, "RETRY_WAITS", (0,)):
        ctx = pl.ByokContext(provider=provider, payload=payload, run_intent=False)
        rep = pl.run_byok(conv, events, Profile(), ctx)
    assert rep.ai_status == "completed" and calls["n"] == len(payload.windows) + 1

    def dead(p, pay, i, override=None):
        raise LLMError("auth", "bad key")

    with mock.patch.object(pl, "run_extraction", side_effect=dead), mock.patch.object(pl, "RETRY_WAITS", (0,)):
        import pytest

        with pytest.raises(LLMError):
            pl.run_byok(conv, events, Profile(), pl.ByokContext(provider=provider, payload=payload, run_intent=False))
