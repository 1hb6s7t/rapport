"""Lexicons for rule-based content features. Original, extensible word lists.

These are heuristics; each metric that depends on them is reported with confidence=low.
"""

from __future__ import annotations

import re

QUESTION_RE = re.compile(r"[?？]|(?:吗|呢|么|嘛)\s*$|(?:怎么|什么|为什么|哪|几点|多久|有没有|是不是|要不要|能不能|可以吗)")
LAUGH_RE = re.compile(r"哈哈|嘿嘿|嘻嘻|hhh+|233+|lol|😂|🤣|笑死|xswl|绝了|哈哈哈", re.I)
CARE_RE = re.compile(
    r"早点休息|早点睡|注意休息|吃饭了吗|吃了吗|记得吃饭|注意安全|小心|辛苦了|辛苦啦|别太累|多喝水|保重|好好休息|"
    r"照顾好自己|路上小心|到家了吗|到家说一声|感冒|还好吗|你还好|没事吧|担心你|想你|挂念|加油|别难过|抱抱|摸摸头"
)
FUTURE_RE = re.compile(
    r"下次|下回|改天|有空|周末|一起|约|见面|见个面|出来|吃个饭|看电影|去哪|什么时候|要不要来|到时候|以后|"
    r"下周|明天|后天|放假|旅行|旅游|计划|安排|等你|带你|陪你"
)

# `FUTURE_RE` above conflates two different things — offering to meet, and agreeing on when — and
# it counted the same words that `ALT_TIME_RE` reads as a soft no ("改天吧" scored as future
# intent). The three below split the act into proposal, agreement and a settled time, so the
# future dimension can tell "always proposing, never accepted" from "plans actually made".
# `FUTURE_RE` stays as the fallback for material with no invitation chain at all.
PROPOSE_RE = re.compile(
    r"要不要一起|要不要来|一起去|一起吃饭|一起吃个饭|吃个饭|见个面|见面|出来玩|出来吃|出来聊聊|约一下|约个|约你|"
    r"我请你|请你吃|(?<![回再])来找你|去找你|去接你|接你下班|陪你|带你|来我家|来我这|周末有空|有空吗|有空一起|有时间吗|"
    r"什么时候有空|哪天有空|看个电影|看电影|一起去"
)
ACCEPT_RE = re.compile(
    r"好啊|好呀|好的呀|行啊|行呀|可以啊|可以呀|没问题|都行|听你的|你定|你说了算|说定了|一言为定|必须的|"
    r"有空呀|有空啊|有空滴|有空的|那我?去|我去|我来|几点|在哪|哪里见|哪儿见|地址|老地方|期待|一定到|不见不散|好耶|冲|走起"
)
COMMIT_RE = re.compile(
    r"[0-9０-９一二三四五六七八九十两]+\s*(点|号|日|点钟)|周[一二三四五六日天末]|礼拜[一二三四五六日]|星期[一二三四五六日天]|"
    r"明天|后天|明晚|今晚|今晚上|下午|上午|中午|晚上见|定在|就定|说好|到时候见|老地方|几点见|在哪见|见面前"
)
AFFECTION_RE = re.compile(r"喜欢你|爱你|想你|宝贝|亲爱的|么么|mua|❤|💕|😘|🥰|抱抱|亲亲|老公|老婆|宝|乖")
EXCLAIM_RE = re.compile(r"[!！]{1,}")
EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\U0001F900-\U0001F9FF]|\\[[^\\]]{1,6}\\]"
)
SHORT_REPLY_RE = re.compile(r"^(嗯|哦|噢|哦哦|嗯嗯|好|好的|行|ok|okay|嗯呢|哈|呵呵|？|\.|。|…+|收到|知道了|随便|都行|不知道)$", re.I)

# Boundary lexicon: explicit decline / stop contact / safety.
STOP_CONTACT_RE = re.compile(
    r"别再联系我|不要再联系|不要联系我|别联系我|不想再联系|不要再找我|别再找我|不要再发了|别发了|不要再发消息|"
    r"我们到此为止|就到这吧|到这里吧|不合适|不适合|不要再打扰|别打扰我|拉黑|删了你|删除好友|不想再聊|不想聊了|"
    r"我们不可能|不可能的|放过我|别烦我|请你别|我有对象了|我有男朋友|我有女朋友|我结婚了"
)
DECLINE_RE = re.compile(
    r"不去了|不想去|不方便|去不了|不太想|算了吧|不了吧|不用了|没时间|没空|不太合适|还是不了|"
    r"我不太想|不太方便|没兴趣|不感兴趣|不需要|不用啦|不用麻烦"
)
# "想死你了" (miss you) and "累得想死 / 困得想死" (exhausted) are everyday speech, not risk; on real
# material (2026-09-18) every 想死 hit across 46k messages was one of those. Keep the bare form.
SAFETY_RE = re.compile(r"报警|自杀|(?<![累困饿渴忙烦笑疼痛热冷])(?<!得)想死(?!你)|不想活|自残|割腕|威胁|跟踪我|骚扰|恐吓|打我|家暴|性骚扰|警察")
RELATIONSHIP_CONFIRM_RE = re.compile(
    r"在一起吧|我们在一起|做我(女|男)朋友|你是我(女|男)朋友|我(女|男)朋友|交往|官宣|谈恋爱|正式交往|做我对象"
)
ALT_TIME_RE = re.compile(r"改天|下次|换个时间|别的时间|下周|周末|有空再|再约|再说|以后|过段时间")
APOLOGY_RE = re.compile(r"对不起|抱歉|不好意思|是我不对|我的错|原谅")
CONFLICT_RE = re.compile(r"你总是|你从来|烦死了|无语|随便你|你根本|懒得|算了|不想说|吵|生气|气死|受不了|离谱|够了")


def lexicon_digest() -> str:
    """Content hash over every compiled pattern in this module, in name order."""
    import hashlib

    h = hashlib.sha256()
    for name, value in sorted(globals().items()):
        if name.endswith("_RE") and hasattr(value, "pattern"):
            h.update(name.encode())
            h.update(value.pattern.encode("utf-8"))
    return h.hexdigest()[:10]
