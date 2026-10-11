from __future__ import annotations

import json

import pytest

from rapport.parsers import ParseError, ParseOptions, parse, sniff
from rapport.schemas.message_v1 import MsgType, Sender

WA_IOS = """[01/09/2026, 20:15:33] 小明: 你好呀
[01/09/2026, 20:16:10] 小红: 你好
在忙吗
[01/09/2026, 20:17:00] 小明: ‎image omitted
[02/09/2026, 09:00:05] 小红: 早
[13/09/2026, 09:00:05] 小明: 好
"""
WA_ANDROID = """13/09/2026, 20:15 - 小明: 你好呀
13/09/2026, 20:16 - 小红: <Media omitted>
13/09/2026, 20:17 - Messages and calls are end-to-end encrypted.
13/09/2026, 20:18 - 小红: 在
"""
WA_AMBIG = """03/04/2026, 20:15 - 小明: 你好
03/04/2026, 20:16 - 小红: 嗨
"""
QQ = """消息记录（此消息记录为文本格式，不支持查看图片、语音等）
================================================================
消息分组:我的好友
================================================================
消息对象:小红
================================================================

2026-09-01 20:15:33 小红(123456789)
你好呀

2026-09-01 20:16:02 我的昵称<me@example.com>
在吗
第二行

2026-09-01 20:17:00 小红(123456789)
[图片]

"""
WECHAT_CSV = """localId,TalkerId,Type,SubType,IsSender,CreateTime,StrTime,Remark,NickName,StrContent
1,2,1,0,0,1756728933,2026-09-01 20:15:33,小红,小红,你好呀
2,2,1,0,1,1756728960,2026-09-01 20:16:00,小红,小红,在
3,2,34,0,0,1756729000,2026-09-01 20:16:40,小红,小红,
4,2,1,0,0,1756729100,2026-09-01 20:18:20,小红,小红,"「小明: 在」
- - - - - - - - - - - - - - -
那你来吗"
5,2,10000,0,0,1756729200,2026-09-01 20:20:00,小红,小红,小红撤回了一条消息
"""
TG = {
    "name": "小红",
    "type": "personal_chat",
    "id": 1,
    "messages": [
        {"id": 1, "type": "message", "date": "2026-09-01T20:15:33", "from": "小明", "from_id": "user1", "text": "你好"},
        {"id": 2, "type": "message", "date": "2026-09-01T20:16:00", "from": "小红", "from_id": "user2", "text": [{"type": "bold", "text": "在"}, "吗"]},
        {"id": 3, "type": "message", "date": "2026-09-01T20:17:00", "from": "小红", "from_id": "user2", "media_type": "voice_message", "file": "x.ogg", "text": ""},
        {"id": 4, "type": "service", "date": "2026-09-01T20:18:00", "actor": "小红", "action": "phone_call", "text": ""},
    ],
}


def test_paste_basic():
    conv, pid = parse("A: 你好\nB: 嗨\nA: [图片]\n我：在吗\n对方：在", "c")
    assert pid == "paste"
    assert [m.sender for m in conv.messages] == [Sender.me, Sender.other, Sender.me, Sender.me, Sender.other]
    assert conv.messages[2].type == MsgType.image and conv.messages[2].text is None
    assert conv.warnings[0].code == "no_time"


def test_paste_with_time_and_names():
    conv, _ = parse("[2026-09-01 20:15] 小明: 你好\n2026-09-01 20:16 小红: 嗨\n续行", "c", "paste")
    assert conv.participants.me == "小明" and conv.participants.other == "小红"
    assert conv.messages[0].ts_precision == "minute"
    assert conv.messages[1].text == "嗨\n续行"


def test_paste_three_names_rejected():
    with pytest.raises(ParseError) as e:
        parse("甲: 1\n乙: 2\n丙: 3", "c", "paste")
    assert e.value.code == "too_many_participants"


def test_whatsapp_ios():
    conv, pid = parse(WA_IOS, "c")
    assert pid == "whatsapp_txt"
    assert len(conv.messages) == 5
    assert conv.messages[1].text == "你好\n在忙吗"
    assert conv.messages[2].type == MsgType.image
    assert conv.messages[0].ts.hour == 20 and conv.messages[0].ts.day == 1 and conv.messages[0].ts.month == 9
    assert conv.messages[0].ts_precision == "second"


def test_whatsapp_android_media_and_system():
    conv, _ = parse(WA_ANDROID, "c")
    kinds = [(m.sender, m.type) for m in conv.messages]
    assert kinds[1] == (Sender.other, MsgType.image)
    assert kinds[2][0] == Sender.system
    assert conv.messages[0].ts_precision == "minute"


def test_whatsapp_ambiguous_date_requires_choice():
    with pytest.raises(ParseError) as e:
        parse(WA_AMBIG, "c")
    assert e.value.code == "ambiguous_date"
    conv, _ = parse(WA_AMBIG, "c", options=ParseOptions(date_order="MDY"))
    assert conv.messages[0].ts.month == 3 and conv.messages[0].ts.day == 4
    conv, _ = parse(WA_AMBIG, "c", options=ParseOptions(date_order="DMY"))
    assert conv.messages[0].ts.month == 4


def test_qq_txt():
    conv, pid = parse(QQ, "c")
    assert pid == "qq_txt"
    assert conv.participants.other == "小红"
    assert [m.sender for m in conv.messages] == [Sender.other, Sender.me, Sender.other]
    assert conv.messages[1].text == "在吗\n第二行"
    assert conv.messages[2].type == MsgType.image
    assert conv.messages[0].ts_precision == "second"


def test_wechat_csv():
    conv, pid = parse(WECHAT_CSV, "c", filename="msg.csv")
    assert pid == "wechat_csv"
    assert conv.messages[0].sender == Sender.other and conv.messages[1].sender == Sender.me
    assert conv.messages[2].type == MsgType.voice and conv.messages[2].text is None
    assert conv.messages[3].quoted_text == "在" and conv.messages[3].text.startswith("那你来吗") or "那你来吗" in conv.messages[3].text
    assert conv.messages[4].sender == Sender.system
    assert conv.participants.other == "小红"


def test_wechat_json():
    rows = [
        {"StrContent": "hi", "IsSender": 1, "Type": 1, "StrTime": "2026-09-01 20:15:33"},
        {"StrContent": "", "IsSender": 0, "Type": 47, "StrTime": "2026-09-01 20:16:00"},
    ]
    conv, pid = parse(json.dumps(rows, ensure_ascii=False), "c")
    assert pid == "wechat_json"
    assert conv.messages[1].type == MsgType.sticker


def test_telegram():
    conv, pid = parse(json.dumps(TG, ensure_ascii=False), "c")
    assert pid == "telegram_json"
    assert conv.messages[1].text == "在吗"
    assert conv.messages[2].type == MsgType.voice
    assert conv.messages[3].sender == Sender.system
    assert conv.participants.me == "小明"


def test_telegram_multi_chat_rejected():
    with pytest.raises(ParseError) as e:
        parse(json.dumps({"chats": {"list": []}}), "c", "telegram_json")
    assert e.value.code == "multi_chat_export"


def test_generic_json_roundtrip():
    conv, _ = parse("A: 1\nB: 2", "x", "paste")
    text = conv.model_dump_json()
    conv2, pid = parse(text, "y")
    assert pid == "generic_json" and len(conv2.messages) == 2 and conv2.id == "y"


def test_generic_json_duplicate_id():
    bad = {"schema_version": "message-v1", "source": "generic_json", "messages": [{"id": "m1", "seq": 0, "sender": "me"}, {"id": "m1", "seq": 1, "sender": "other"}]}
    with pytest.raises(ParseError) as e:
        parse(json.dumps(bad), "c", "generic_json")
    assert e.value.code == "schema"


def test_sniff_unknown():
    assert sniff("just some prose without any structure") == []
    with pytest.raises(ParseError) as e:
        parse("just some prose", "c")
    assert e.value.code == "unsupported_format"


# ---------- 微信单聊导出 JSON（session + messages） ----------

from pathlib import Path  # noqa: E402

EXPORT_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "synthetic" / "wechat_session_export.json"


def _export_text() -> str:
    return EXPORT_FIXTURE.read_text(encoding="utf-8")


def test_export_sniff_beats_paste_even_on_one_line():
    one_line = json.dumps(json.loads(_export_text()), ensure_ascii=False)
    ranked = sniff(one_line)
    assert ranked and ranked[0][0] == "wx_session_json" and ranked[0][1] >= 0.99


def test_export_types_and_roles():
    conv, pid = parse(_export_text(), "c")
    assert pid == "wx_session_json"
    by_local = {m.meta["local_id"]: m for m in conv.messages}
    assert by_local["1"].sender == Sender.other and by_local["1"].type == MsgType.text
    assert by_local["2"].sender == Sender.me
    assert by_local["6"].type == MsgType.image and by_local["6"].text is None and by_local["6"].meta["media_ref"]
    assert by_local["7"].type == MsgType.sticker and by_local["7"].meta["emoji_md5"] == "abc123"
    assert by_local["8"].type == MsgType.video
    assert by_local["9"].type == MsgType.file
    assert by_local["10"].type == MsgType.call
    assert by_local["11"].type == MsgType.transfer
    assert by_local["12"].type == MsgType.link and by_local["12"].meta["link_title"] == "周末去哪儿"
    assert by_local["13"].sender == Sender.system and by_local["13"].type == MsgType.system
    assert by_local["14"].type == MsgType.unknown
    assert conv.participants.me == "小明" and conv.participants.other == "小红"


def test_export_transcribed_voice_is_text_but_raw_voice_is_not():
    conv, _ = parse(_export_text(), "c")
    by_local = {m.meta["local_id"]: m for m in conv.messages}
    v = by_local["3"]
    assert v.type == MsgType.text and v.is_text and v.text == "有空呀，周六下午两点？" and v.meta["from_voice"] is True
    raw = by_local["4"]
    assert raw.type == MsgType.voice and raw.text is None and "from_voice" not in raw.meta
    assert conv.meta["export"]["voice_transcribed"] == 1 and conv.meta["export"]["voice_untranscribed"] == 1


def test_export_quote_body_and_quoted_context():
    conv, _ = parse(_export_text(), "c")
    q = {m.meta["local_id"]: m for m in conv.messages}["5"]
    assert q.type == MsgType.text and q.text == "好，到时候见"
    assert q.quoted_text == "有空呀，周六下午两点？" and q.meta["quoted_by"] == "小红"


def test_export_timestamps_are_second_precision():
    conv, _ = parse(_export_text(), "c")
    first = conv.messages[0]
    assert first.ts is not None and first.ts_precision == "second"
    assert first.ts.strftime("%Y-%m-%d %H:%M:%S") == "2026-09-01 20:15:33"


def test_export_media_warning_and_counts():
    conv, _ = parse(_export_text(), "c")
    codes = {w.code for w in conv.warnings}
    assert "media_unobserved" in codes and "voice_transcribed" in codes
    tc = conv.meta["export"]["type_counts"]
    assert tc["image"] == 1 and tc["sticker"] == 1 and tc["video"] == 1 and tc["unknown"] == 1


def test_export_me_name_override_flips_roles():
    conv, _ = parse(_export_text(), "c", options=ParseOptions(me_name="小红"))
    assert conv.participants.me == "小红" and conv.participants.other == "小明"
    assert conv.messages[0].sender == Sender.me


def test_export_group_export_rejected():
    data = json.loads(_export_text())
    data["session"]["type"] = "group"
    with pytest.raises(ParseError) as ei:
        parse(json.dumps(data, ensure_ascii=False), "c")
    assert ei.value.code == "not_personal_chat"


def test_export_unknown_type_is_counted_not_guessed():
    data = json.loads(_export_text())
    data["messages"].append({"localId": "99", "createTime": "1756729700", "type": "未来的新类型", "content": "x", "isSend": "0"})
    conv, _ = parse(json.dumps(data, ensure_ascii=False), "c")
    m = {m.meta["local_id"]: m for m in conv.messages}["99"]
    assert m.type == MsgType.unknown and m.text is None
    assert conv.meta["export"]["unknown_types"] == {"未来的新类型": 1}
