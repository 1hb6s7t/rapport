"""v0.5 §3A: structural cues are facts, computed from the episode, injected only when asked.

Pinned here: cues answer the two questions the real baseline showed the model getting wrong
(is the target answering A's question; was the room laughing), they never contain a label word,
the switch is off by default in both the product and the eval harness, and the block is part of
the prompt only when on.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from rapport.features.cues import LOOKBACK, compute_cues, cues_block
from rapport.features.episodes import build_episodes, episode_for
from rapport.llm.intent import build_intent_input
from rapport.parsers import ParseOptions, parse
from rapport.schemas.report_v1 import EMOTIONS, PRAGMATIC_TAGS

T0 = datetime(2026, 9, 1, 20, 0)


def _conv(lines: list[tuple[str, str, int]]):
    txt = "\n".join(f"[{(T0 + timedelta(minutes=m)):%Y-%m-%d %H:%M}] {who}: {text}" for who, text, m in lines)
    conv, _ = parse(txt, "c", "paste", None, ParseOptions())
    return conv


def _episode(conv, target_index: int = -1):
    events = conv.events()
    blocks = build_episodes(conv, events)
    target = events[target_index]
    ep = episode_for(conv, blocks, target, mode="review")
    assert ep is not None
    return ep


def test_answering_a_question_is_detected_and_laughs_are_counted():
    conv = _conv([("A", "哈哈哈 你周末要不要一起吃饭？", 0), ("B", "哈哈 改天吧", 3)])
    c = compute_cues(_episode(conv))
    assert c.answers_a_question and c.who_opened == "A"
    assert c.laughs_before >= 1 and not c.b_laughed_before
    assert c.decline_words is False and c.alt_time_words is True
    assert c.gap_before_target_s == 180.0


def test_repeated_requests_and_apology_are_counted():
    conv = _conv([("A", "周末有空吗？", 0), ("B", "看看", 1), ("A", "那下周呢？", 2), ("B", "嗯", 3), ("A", "对不起我催太紧了", 4), ("B", "没事", 5)])
    c = compute_cues(_episode(conv))
    assert c.a_repeated_requests >= 2 and c.a_conceded_or_apologised
    assert c.answers_a_question is False  # the last A line was an apology, not a question


def test_cues_block_contains_no_label_words():
    conv = _conv([("A", "你生气了？", 0), ("B", "没有啊", 1)])
    block = cues_block(_episode(conv))
    for w in list(PRAGMATIC_TAGS) + list(EMOTIONS):
        if w in ("不确定",):
            continue
        assert w not in block, w
    assert "结构线索" in block and "不是标签" in block
    assert str(LOOKBACK) in block


def test_switch_is_off_by_default_and_block_only_appears_when_on():
    conv = _conv([("A", "在吗？", 0), ("B", "在", 1)])
    ep = _episode(conv)
    off = build_intent_input(conv, ep)
    on = build_intent_input(conv, ep, cues=True)
    assert off.cues_block == "" and "结构线索" not in off.user_prompt()
    assert on.cues_block and "结构线索" in on.user_prompt()
    # the block sits before the final instruction, after the lines
    p = on.user_prompt()
    assert p.index("结构线索") > p.index("在吗") and p.index("结构线索") < p.index("请判断")


def test_product_switch_defaults_off_and_persists(app_client):
    st = app_client.app_state
    assert st.cues_enabled() is False
    st.set_cues_enabled(True)
    assert st.cues_enabled() is True
