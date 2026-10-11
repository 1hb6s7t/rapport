from __future__ import annotations

from datetime import datetime, timedelta

from rapport.features import compute_features, split_sessions, turns
from rapport.parsers import parse
from rapport.schemas.message_v1 import Conversation, Message, MsgType, Sender, Source, TsPrecision


def mk(seq, sender, ts, text=None, typ=MsgType.text, prec=TsPrecision.minute):
    return Message(id=f"m{seq:06d}", seq=seq, sender=sender, ts=ts, ts_precision=prec if ts else TsPrecision.none, type=typ, text=text)


def conv_of(msgs):
    return Conversation(id="c", source=Source.paste, messages=msgs)


def test_voice_then_text_reply_latency():
    t = datetime(2026, 9, 1, 12, 0)
    msgs = [
        mk(0, Sender.me, t, "你好"),
        mk(1, Sender.other, t + timedelta(minutes=1), None, MsgType.voice),
        mk(2, Sender.other, t + timedelta(minutes=30), "在的"),
    ]
    f = compute_features(conv_of(msgs))
    assert f.other.median_reply_s.value == 60
    assert f.other.median_text_reply_s.value == 1800


def test_gap_not_bridged_and_untimed_not_zero():
    t = datetime(2026, 9, 1, 12, 0)
    msgs = [mk(0, Sender.me, t, "a"), mk(1, Sender.other, None, "b"), mk(2, Sender.me, t + timedelta(hours=1), "c"), mk(3, Sender.other, t + timedelta(hours=1, minutes=5), "d")]
    f = compute_features(conv_of(msgs))
    # reply from m1 has no ts -> skipped; only m3 counts
    assert f.other.median_reply_s.n == 1 and f.other.median_reply_s.value == 300


def test_no_time_hides_time_metrics():
    msgs = [mk(i, Sender.me if i % 2 == 0 else Sender.other, None, f"t{i}") for i in range(10)]
    f = compute_features(conv_of(msgs))
    assert not f.has_reliable_time
    assert f.other.median_reply_s.value is None and f.other.median_reply_s.unavailable_reason == "no_time"
    assert f.other.initiation_rate.value is None
    assert f.other.question_rate.value is not None


def test_turns_and_double_text():
    t = datetime(2026, 9, 1, 12, 0)
    msgs = [mk(0, Sender.other, t, "a"), mk(1, Sender.other, t + timedelta(minutes=1), "b"), mk(2, Sender.me, t + timedelta(minutes=2), "c")]
    tr = turns(msgs)
    assert len(tr) == 2 and len(tr[0]) == 2
    f = compute_features(conv_of(msgs))
    assert f.other.turns == 1 and f.other.double_text_rate.value == 1.0


def test_sessions_split_on_gap():
    t = datetime(2026, 9, 1, 12, 0)
    msgs = [mk(0, Sender.me, t, "a"), mk(1, Sender.other, t + timedelta(minutes=5), "b"), mk(2, Sender.other, t + timedelta(hours=7), "c"), mk(3, Sender.me, t + timedelta(hours=7, minutes=1), "d")]
    s = split_sessions(msgs, 6)
    assert len(s) == 2 and s[1].first.sender == Sender.other
    f = compute_features(conv_of(msgs))
    assert f.other.initiation_rate.value == 0.5


def test_lexicon_and_boundary_hits():
    t = datetime(2026, 9, 1, 12, 0)
    msgs = [mk(0, Sender.me, t, "周末出来吗？"), mk(1, Sender.other, t + timedelta(minutes=2), "别再联系我了"), mk(2, Sender.me, t + timedelta(minutes=3), "好的，早点休息")]
    f = compute_features(conv_of(msgs))
    assert f.boundary_hits["stop_contact"] == ["m000001"]
    assert f.me.question_ids == ["m000000"] and f.me.care_ids == ["m000002"]


def test_deterministic():
    from tests.conftest import demo_text

    conv, _ = parse(demo_text(), "c", "paste")
    a = compute_features(conv).d()
    b = compute_features(conv).d()
    assert a == b
