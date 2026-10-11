"""Invitation chains (v0.5, plan §1A): offer → answer → settled time.

The point of the chains is to make the future dimension answer a question the old word-frequency
metric could not: *did anything actually come of asking?* These pin the three outcomes that matter
— accepted with a time, re-scheduled, not answered — and the fallback for material with no offer.
"""

from __future__ import annotations

from rapport.features import lexicon as lx
from rapport.features.compute import Features, compute_features
from rapport.parsers import ParseOptions, parse
from rapport.scoring.rubric import rule_dims

ME = "A"


def _features(*lines: str) -> Features:
    text = "\n".join(f"[2026-08-{i + 1:02d} 20:00] {ln}" for i, ln in enumerate(lines))
    conv, _ = parse(text, "t", "paste", None, ParseOptions())
    return compute_features(conv, conv.events())


def test_an_accepted_offer_with_a_settled_time_is_a_chain():
    f = _features(
        "A: 周末有空吗",
        "B: 有空呀，怎么啦",
        "A: 想约你去看那个展",
        "B: 好啊，周六下午两点？",
        "A: 就周六两点",
    )
    inv = f.invitations
    assert inv["proposed"] >= 1
    assert inv["accepted"] >= 1
    assert inv["committed"] >= 1
    assert f.accept_rate.value is not None and f.accept_rate.value > 0
    assert f.commit_rate.value is not None and f.commit_rate.value > 0


def test_a_reschedule_is_not_an_acceptance():
    """「改天吧」 is a future word *and* a soft no. The chain has to read it as the second."""
    f = _features("A: 周末一起吃饭吧", "B: 改天吧", "A: 好", "B: 嗯")
    assert lx.FUTURE_RE.search("改天吧"), "the old lexicon still matches — that is why it is only a fallback"
    assert f.invitations["accepted"] == 0
    assert f.invitations["alt_timed"] >= 1
    assert f.alt_time_rate.value == 1.0


def test_no_answer_is_a_zero_rate_not_a_missing_value():
    """"asked three times, never answered" is 0% acceptance, not unavailable evidence."""
    f = _features("A: 周末有空吗", "B: 在忙", "A: 那下周呢", "B: 嗯")
    assert f.invitations["proposed"] >= 1
    assert f.accept_rate.value == 0.0
    assert f.accept_rate.unavailable_reason is None


def test_rates_are_unavailable_when_nothing_was_offered():
    f = _features("A: 今天好累", "B: 辛苦了，早点休息", "A: 晚安", "B: 晚安")
    assert f.invitations["proposed"] == 0
    assert f.accept_rate.value is None
    assert f.accept_rate.unavailable_reason == "no_offers"


def test_a_promise_to_come_by_is_not_an_invitation():
    """「回来我去接你」 matched the offer lexicon through 「来找你」 and inflated the proposal count."""
    f = _features("A: 我下周要出差", "B: 几天呀", "A: 三天", "B: 回来我去接你", "A: 好")
    assert f.invitations["proposed"] == 0


def test_the_future_dimension_reads_the_chain():
    warm = _features(
        "A: 周末有空吗",
        "B: 有空呀",
        "A: 想约你看展",
        "B: 好啊，周六两点",
        "A: 就周六两点",
    )
    cooling = _features(
        "A: 周末有空吗",
        "B: 这周有点忙，改天吧",
        "A: 那下周呢",
        "B: 再说吧",
    )
    dim_warm = rule_dims(warm)["future"]
    dim_cool = rule_dims(cooling)["future"]
    assert dim_warm.score is not None and dim_cool.score is not None
    assert dim_warm.score > dim_cool.score, "答应并谈定时间必须高于一再说改天"
    assert "邀约链" in dim_warm.note
    assert "改期" in dim_cool.note


def test_without_a_chain_it_says_so_and_falls_back():
    f = _features("A: 下周一起去旅行吧", "B: 以后再说", "A: 好吧")
    # 「一起去」 matches the offer list, so this one still has a chain; the note proves which path ran.
    note = rule_dims(f)["future"].note
    assert "邀约链" in note or "词表兜底" in note
    empty = _features("A: 今天好累", "B: 嗯", "A: 明天放假", "B: 哦")
    assert rule_dims(empty)["future"].note.startswith("词表兜底")


def test_the_chain_carries_the_ids_behind_it():
    f = _features("A: 周末有空吗", "B: 好啊，周六两点", "A: 就周六两点")
    chain = f.invitations["chains"][0]
    assert chain["propose_id"]
    assert chain["response"] == "accept"
    assert chain["response_id"]
    assert chain["commit_ids"]
