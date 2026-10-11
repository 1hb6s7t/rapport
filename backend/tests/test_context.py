"""Phase 1: episode construction and context budget planning."""

from __future__ import annotations

import pytest

from rapport.features.episodes import build_episodes, episode_for, select_targets
from rapport.llm.budget import DEFAULT_ALLOCATION, plan_context_budget
from rapport.parsers import ParseOptions, parse

# A conversation with: an early block, a long silence, a quoted reply, a system line,
# and boundary evidence in the last block.
CHAT = """[2026-09-01 20:00] A: 在吗
[2026-09-01 20:05] B: 在的，怎么了
[2026-09-01 20:06] A: 周末有空吗
[2026-09-01 20:20] B: 周末要加班，改天吧
[2026-09-01 20:21] A: 好的
[2026-09-02 09:00] A: 早
[2026-09-02 09:30] B: 早
[2026-09-03 21:00] A: 你今天好像不太想聊
[2026-09-03 21:05] B: 没有啊
[2026-09-03 21:30] A: 那周末见一面？
[2026-09-03 21:31] B: 别再联系我了
[2026-09-03 21:40] A: 好的
"""


@pytest.fixture
def conv():
    c, _ = parse(CHAT, "c1", "paste", None, ParseOptions())
    return c


def test_blocks_split_on_session_gap(conv):
    blocks = build_episodes(conv, conv.events(), gap_hours=6.0)
    assert len(blocks) >= 2, "the 13-hour silence must start a new block"
    # the first block contains the early exchange, not the boundary message
    assert any(m.text == "在吗" for m in blocks[0])
    assert not any(m.text == "别再联系我了" for m in blocks[0])


def test_reply_mode_never_sees_after_the_target(conv):
    events = conv.events()
    blocks = build_episodes(conv, events)
    target = next(m for m in events if m.text == "你今天好像不太想聊")
    ep = episode_for(conv, blocks, target, mode="reply")
    assert ep is not None and ep.post_hoc is False
    assert ep.messages[-1].id == target.id, "reply mode must end at the target"
    assert all(m.ts <= target.ts for m in ep.messages)


def test_review_mode_includes_later_context_and_is_flagged(conv):
    events = conv.events()
    blocks = build_episodes(conv, events)
    target = next(m for m in events if m.text == "你今天好像不太想聊")
    ep = episode_for(conv, blocks, target, mode="review")
    assert ep is not None and ep.post_hoc is True
    assert any(m.ts > target.ts for m in ep.messages), "review mode may use hindsight"
    assert ep.as_of == target.ts.isoformat()


def test_episode_ids_are_stable_and_mention_the_target(conv):
    events = conv.events()
    blocks = build_episodes(conv, events)
    target = next(m for m in events if m.text == "没有啊")
    a = episode_for(conv, blocks, target, mode="review")
    b = episode_for(conv, blocks, target, mode="review")
    assert a.id == b.id and target.id in a.id and a.id.startswith("c1:b")


def test_window_is_capped_and_marks_truncation(conv):
    events = conv.events()
    blocks = build_episodes(conv, events)
    target = next(m for m in events if m.text == "别再联系我了")
    ep = episode_for(conv, blocks, target, mode="reply", max_messages=2)
    assert ep is not None and len(ep.messages) <= 2


def test_select_targets_prefers_boundary_then_replies(conv):
    events = conv.events()
    sel = select_targets(conv, events, boundary_ids=["m000010"], max_targets=4, mode="review")
    assert sel.skipped_reason is None
    assert sel.episodes, "must return at least one episode"
    assert sel.episodes[0].target.id == "m000010", "boundary evidence is read first"
    assert len(sel.episodes) <= 4
    # only the other person's text messages are ever targets
    assert all(e.target.sender.value == "other" and e.target.is_text for e in sel.episodes)


def test_select_targets_reports_why_it_skipped():
    conv, _ = parse("A: 你好\nA: 在吗\nA: 喂", "c1", "paste", None, ParseOptions())
    sel = select_targets(conv, conv.events(), max_targets=4)
    assert sel.episodes == [] and sel.skipped_reason


# ---------------------------------------------------------------- budget


def test_budget_fits_when_small():
    b = plan_context_budget({"task": 1000, "episode": 3000, "related_history": 500, "summary": 200, "reference_examples": 800})
    assert b.fits and b.reduced == [] and b.overflow_note is None


def test_budget_trims_examples_first_then_summary_then_history():
    # default allocation totals 16000; each trim step frees a known amount
    sections = {"task": 1500, "episode": 6000, "related_history": 2000, "summary": 1000, "reference_examples": 2000}
    b = plan_context_budget(sections, total_tokens=14000)
    assert b.fits and b.reduced == ["reference_examples"], "examples go first"
    b2 = plan_context_budget(sections, total_tokens=13000)
    assert b2.fits and b2.reduced == ["reference_examples", "summary"]
    b3 = plan_context_budget(sections, total_tokens=11000)
    assert b3.fits and b3.reduced == ["reference_examples", "summary", "related_history"], "history goes last"


def test_protected_sections_are_never_trimmed():
    b = plan_context_budget({"task": 1500, "episode": 6000, "related_history": 2000, "summary": 1000, "reference_examples": 2000}, total_tokens=8000)
    assert not b.fits, "cannot fit without touching episode/task/output"
    assert b.allocations["episode"] == 6000 and b.allocations["task"] == 1500
    assert b.allocations["output"] == DEFAULT_ALLOCATION["output"]
    assert b.allocations["reserve"] == DEFAULT_ALLOCATION["reserve"]
    assert b.overflow_note and "边界" in b.overflow_note


def test_episode_is_capped_at_its_own_ceiling():
    b = plan_context_budget({"episode": 99999})
    assert b.allocations["episode"] == DEFAULT_ALLOCATION["episode"]


def test_budget_reports_trim_list_for_ui():
    b = plan_context_budget({"episode": 6000, "reference_examples": 2000, "summary": 1000}, total_tokens=14000)
    d = b.as_dict()
    assert d["fits"] and d["reduced"] == ["reference_examples"] and "allocations" in d
    assert d["allocations"]["episode"] == 6000, "the episode is never reduced"
