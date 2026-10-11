"""Zodiac & Chinese zodiac pairing blurbs. Entertainment only; predictive_weight is always 0.

Text is generated from element/trine tables rather than a 144-entry hand-written matrix,
so it stays short, original and obviously playful.
"""

from __future__ import annotations

from rapport.schemas.profile import ChineseZodiac, Zodiac
from rapport.schemas.report_v1 import FunItem

ZODIAC_ZH = {
    "aries": "白羊座", "taurus": "金牛座", "gemini": "双子座", "cancer": "巨蟹座",
    "leo": "狮子座", "virgo": "处女座", "libra": "天秤座", "scorpio": "天蝎座",
    "sagittarius": "射手座", "capricorn": "摩羯座", "aquarius": "水瓶座", "pisces": "双鱼座",
}
ELEMENT = {
    "aries": "火", "leo": "火", "sagittarius": "火",
    "taurus": "土", "virgo": "土", "capricorn": "土",
    "gemini": "风", "libra": "风", "aquarius": "风",
    "cancer": "水", "scorpio": "水", "pisces": "水",
}
ELEMENT_PAIR = {
    ("火", "火"): "两团火凑一起，聊天节奏快、热得快，也容易一言不合就冷场几天。",
    ("火", "风"): "风助火势型组合，话题一开就停不下来，缺点是都不太爱收尾。",
    ("火", "土"): "一个想马上出发，一个想先查攻略。谁先妥协谁请客。",
    ("火", "水"): "热情撞上细腻，火花是有的，就看谁愿意先读懂对方的“嗯”。",
    ("风", "风"): "聊天像开脑洞大会，从外太空聊到楼下奶茶，唯一的风险是忘了正事。",
    ("风", "土"): "一个负责天马行空，一个负责把想法落地，合作型 CP 预定。",
    ("风", "水"): "一个说得多，一个想得多。风要记得停下来问一句“你怎么看”。",
    ("土", "土"): "稳定得像存款利率，惊喜不多，但靠谱。适合慢热型玩家。",
    ("土", "水"): "水滋养土的经典搭配，安全感拉满，就是都不太主动，等着对方先开口。",
    ("水", "水"): "情绪频道高度同步，能共情到一起哭，也能一起沉默一整天。",
}
CZ_ZH = {
    "rat": "鼠", "ox": "牛", "tiger": "虎", "rabbit": "兔", "dragon": "龙", "snake": "蛇",
    "horse": "马", "goat": "羊", "monkey": "猴", "rooster": "鸡", "dog": "狗", "pig": "猪",
}
CZ_ORDER = ["rat", "ox", "tiger", "rabbit", "dragon", "snake", "horse", "goat", "monkey", "rooster", "dog", "pig"]
TRINE_TEXT = {
    0: "同一“三合”阵营，民间说法里是天然队友，聊起来容易有默契。",
    4: "同一“三合”阵营，民间说法里是天然队友，聊起来容易有默契。",
    8: "同一“三合”阵营，民间说法里是天然队友，聊起来容易有默契。",
    6: "民间说法里的“六冲”，据说容易互相较劲——也可能是互相吸引的另一种说法。",
    1: "相邻属相，民间叫“六合”或邻居缘，日常相处大概率平顺。",
    11: "相邻属相，民间叫“六合”或邻居缘，日常相处大概率平顺。",
}


def zodiac_pair(a: Zodiac | None, b: Zodiac | None) -> FunItem | None:
    if not a or not b:
        return None
    ea, eb = ELEMENT[a.value], ELEMENT[b.value]
    key = (ea, eb) if (ea, eb) in ELEMENT_PAIR else (eb, ea)
    text = ELEMENT_PAIR[key]
    return FunItem(title=f"{ZODIAC_ZH[a.value]} × {ZODIAC_ZH[b.value]}", text=f"{text}（{ea}象 × {eb}象，娱乐向）")


def chinese_zodiac_pair(a: ChineseZodiac | None, b: ChineseZodiac | None) -> FunItem | None:
    if not a or not b:
        return None
    ia, ib = CZ_ORDER.index(a.value), CZ_ORDER.index(b.value)
    diff = (ib - ia) % 12
    text = TRINE_TEXT.get(diff) or "民间说法里没什么特别标签的组合，全靠两个人自己写剧本。"
    if diff == 0:
        text = "同属相，据说要么特别懂对方，要么谁也不服谁。"
    return FunItem(title=f"属{CZ_ZH[a.value]} × 属{CZ_ZH[b.value]}", text=f"{text}（娱乐向）")
