"""Local style retrieval: the default local reply route when no personal adapter is enabled."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from rapport.config import STYLE_REPLY_PRESET, ConnectionMode
from rapport.features.episodes import build_episodes, episode_for
from rapport.learning import style_rag
from rapport.llm.personal_request import compile_reply
from rapport.llm.reply import ReplyInput, echo_lines, mostly_echo, validate_replies
from rapport.schemas.diary import DiaryEntry, DiaryKind
from rapport.schemas.message_v1 import (
    Conversation,
    Message,
    MsgType,
    Participants,
    Sender,
    Source,
    TsPrecision,
)

R1 = "rel-1"
T0 = datetime(2026, 8, 18, 20, 0, tzinfo=UTC)
CHAT = """[2026-08-18 20:00] B: 最近在忙什么呀
[2026-08-18 20:02] A: 忙着搬家呢，东西太多了
[2026-08-18 20:03] A: 累得不想动
[2026-08-19 21:00] B: 那个电影好看吗
[2026-08-19 21:01] A: 还行吧，结尾有点拖
[2026-08-20 09:00] B: 几点到啊
[2026-08-20 09:05] A: 我昨天去了趟医院，下午3点到
[2026-08-22 21:00] B: 周末一起吃个饭？
"""


def _message(seq: int, sender: Sender, text: str, minutes: int) -> Message:
    return Message(id=f"m{seq:06d}", seq=seq, sender=sender, ts=T0 + timedelta(minutes=minutes),
                   ts_precision=TsPrecision.minute, type=MsgType.text, text=text)


def _conversation(messages: list[Message]) -> Conversation:
    return Conversation(id="c1", source=Source.paste, participants=Participants(me="小明", other="小红"), messages=messages)


def _episode(conv: Conversation):
    events = conv.events()
    return episode_for(conv, build_episodes(conv, events, 6), events[-1], mode="reply")


# ---------------------------------------------------------------- offline pool (evaluation)


def test_example_pool_uses_earlier_trained_rows_of_the_same_friend_only():
    rows = [
        {"id": "r1", "session_id": "s1", "time_split": "train", "training_eligible": True, "response": "哈哈那我先睡了",
         "context": [{"sender": "other", "text": "你困不困"}]},
        {"id": "r2", "session_id": "s1", "time_split": "test", "training_eligible": True, "response": "测试集的回复",
         "context": [{"sender": "other", "text": "你困不困"}]},
        {"id": "r3", "session_id": "s1", "time_split": "train", "training_eligible": False, "response": "我昨天去爬山了",
         "context": [{"sender": "other", "text": "你困不困"}]},
        {"id": "r4", "session_id": "s2", "time_split": "train", "training_eligible": True, "response": "别的好友的回复",
         "context": [{"sender": "other", "text": "你困不困"}]},
    ]
    pool = style_rag.example_pool(rows)
    assert {e["reply"] for e in pool} == {"哈哈那我先睡了", "别的好友的回复"}
    picked = style_rag.retrieve(pool, "s1", "困不困")
    assert [e["reply"] for e in picked] == ["哈哈那我先睡了"], "examples never cross to another friend"


# ---------------------------------------------------------------- live examples (production)


def test_live_examples_come_from_earlier_turns_only_and_are_redacted():
    messages = [
        _message(1, Sender.other, "最近在忙什么呀", 0),
        _message(2, Sender.me, "忙着搬家呢，小红你呢", 2),
        _message(3, Sender.me, "累得不想动", 3),
        _message(4, Sender.other, "几点到啊", 60),
        _message(5, Sender.me, "我昨天去了趟医院，下午3点到", 61),
        _message(6, Sender.other, "周末一起吃个饭？", 60 * 72),
    ]
    conv = _conversation(messages)
    episode = _episode(conv)
    assert [m.seq for m in episode.messages] == [6]
    examples = style_rag.live_examples(conv, episode, [("小明", "A"), ("小红", "B")])
    assert examples == [{"said": "最近在忙什么呀", "reply": "忙着搬家呢，B你呢 累得不想动"}], (
        "one turn with its follow-up bubble; names redacted; the reply claiming an experience and an unsaid time is skipped")


def test_live_examples_are_empty_without_earlier_turns():
    conv = _conversation([_message(1, Sender.other, "在吗", 0)])
    assert style_rag.live_examples(conv, _episode(conv), []) == []
    assert style_rag.live_examples(None, None, []) == []


# ---------------------------------------------------------------- prompt


def test_compile_uses_bare_lines_and_lists_only_authorised_memories():
    memory = DiaryEntry(id="d1", text="周末和朋友吃了饭", kind=DiaryKind.fact, enabled=True, allowed_relationships=frozenset({R1}),
                        recorded_at=T0, approved_clause="周末和朋友吃了饭")
    inp = ReplyInput(lines=["m000001 08-18 20:00 B: 周末一起吃个饭？"], styles=["casual"], memories=[], goal="自然地回应",
                     style_examples=[{"said": "在吗", "reply": "在呢"}])
    compiled = compile_reply(inp, STYLE_REPLY_PRESET)
    assert compiled.system == style_rag.SYSTEM and compiled.temperature == style_rag.TEMPERATURE
    assert "B: 周末一起吃个饭？" in compiled.user and "m000001" not in compiled.user
    assert "在呢" in compiled.user and "本人记忆" not in compiled.user
    inp.memories = [memory]
    assert "id=d1 revision=1 kind=fact：周末和朋友吃了饭" in compile_reply(inp, STYLE_REPLY_PRESET).user


# ---------------------------------------------------------------- echo filter


def test_echo_filter_withholds_drafts_that_repeat_the_chat():
    lines = ["B: 道里哪块儿啊？", "A: 没事，我那块有10个的", "B: 郑州经贸学院"]
    assert echo_lines("放心，我那块有10个的\n你要是需要随时找我", lines) == ["放心，我那块有10个的"]
    assert mostly_echo("放心，我那块有10个的\n你要是需要随时找我", lines)
    assert not mostly_echo("郑州经贸学院？\n听起来挺有意思的", lines), "a short echo inside a real reply is fine"
    assert not echo_lines("好的", lines), "very short acknowledgements are never echoes"
    inp = ReplyInput(lines=lines, styles=["casual", "sincere"], memories=[], goal="回应")
    data = {"drafts": [{"style": "casual", "text": "道里哪块儿？"}, {"style": "sincere", "text": "那我去接你吧"}]}
    pack = validate_replies(data, inp, {}, R1, datetime.now(UTC), reject_echo=True)
    assert [d.style for d in pack.drafts] == ["sincere"] and pack.withheld_drafts[0]["style"] == "casual"
    assert len(validate_replies(data, inp, {}, R1, datetime.now(UTC)).drafts) == 2, "off unless asked for"


# ---------------------------------------------------------------- routing and API


def test_local_replies_default_to_style_retrieval(app_client, monkeypatch):
    state = app_client.app_state
    assert state.connection_mode() == ConnectionMode.local
    cfg = state.task_provider_config("reply")
    assert (cfg["preset"], cfg["model"], cfg["ready"]) == (STYLE_REPLY_PRESET, "qwen3:8b", True)
    assert "127.0.0.1" in cfg["base_url"]
    assert state.task_provider_config("extract")["preset"] != STYLE_REPLY_PRESET, "only replies take this route"
    state.save_provider_config("ollama-lite", None, None, None, None)
    assert state.task_provider_config("reply")["model"] == "qwen3:4b", "the configured local model is kept"
    monkeypatch.setattr(state.learning, "active_model", lambda scope, relationship_id=None: {"id": "adapter-1"})
    assert state.task_provider_config("reply")["preset"] == "personal", "an enabled personal adapter wins"
    monkeypatch.undo()
    state.set_connection_mode(ConnectionMode.cloud)
    state.save_provider_config("deepseek", None, None, None, None)
    assert state.task_provider_config("reply")["preset"] == "deepseek", "cloud mode keeps the chosen cloud route"


def test_style_route_preview_shows_examples_and_drafts_run_the_strict_checks(app_client):
    c = app_client
    c.post("/api/relationships", json={"id": R1, "name": R1})
    imp = c.post("/api/imports", json={"text": CHAT, "relationship_id": R1}).json()
    c.post(f"/api/imports/{imp['id']}/confirm", json={})
    body = {"import_id": imp["id"], "relationship_id": R1}
    pv = c.post("/api/replies/preview", json=body).json()
    assert pv["destination"]["preset"] == STYLE_REPLY_PRESET and pv["destination"]["ready"]
    assert any("你以前回复这位好友的原话" in field for field in pv["fields_sent"])
    shown = str(pv)
    assert "还行吧，结尾有点拖" in shown and "下午3点到" not in shown, "the user sees the examples that ride along"
    out = c.post("/api/replies", json={**body, "ticket_id": pv["ticket_id"], "payload_hash": pv["payload_hash"], "accept_cost": True})
    assert out.status_code == 200, out.text
    system, user = c.app_state.provider_override.calls[-1]
    assert system == style_rag.SYSTEM and "我以前回复这位好友的真实例子" in user
    assert out.json()["drafts"], "the fake model's plain drafts pass the strict checks"
