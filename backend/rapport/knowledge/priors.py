"""Original communication-style priors for MBTI / zodiac / Chinese zodiac.

These are **starting assumptions to be tested against real chat**, not descriptions of a person.
Each text is written as a hypothesis about how someone *might* prefer to communicate, so that the
product can later show "we assumed X at the start; the chat now suggests Y".

Everything here is original text by this project. Nothing is copied from MBTI communities,
astrology apps or any dataset. The zodiac and Chinese-zodiac entries are explicitly entertainment
in the model (`entertainment=True`) and carry small weights.
"""

from __future__ import annotations

MBTI_PRIORS: dict[str, str] = {
    "INTJ": "假设：偏好先想清楚再表达，回复可能偏短但信息密度高；对含糊的邀约可能不太回应，对具体安排反应更快。",
    "INTP": "假设：容易顺着话题发散，可能跳过寒暄直接讲兴趣点；对被追问情绪可能反应慢。",
    "ENTJ": "假设：倾向于直接给结论和安排，喜欢明确的下一步；对反复的试探可能显得不耐烦。",
    "ENTP": "假设：对话里爱开玩笑和挑战观点，话题切换快；情绪表达可能被玩笑掩盖。",
    "INFJ": "假设：在意用词的分寸，回复常带铺垫；不满可能先以含蓄方式出现。",
    "INFP": "假设：表达偏柔和、重感受；被否定时可能先沉默而不是当场反驳。",
    "ENFJ": "假设：主动关心对方状态，回复里常有照顾性的话；也可能因为顾全对方而不直说不愿意。",
    "ENFP": "假设：热情外放、话题多，回复节奏起伏大；热度高不代表承诺度高。",
    "ISTJ": "假设：重约定与准时，习惯按计划办事；对临时改变安排比较敏感。",
    "ISFJ": "假设：记得细节、表达体贴，较少主动提自己的需求；不满可能长期不表达。",
    "ESTJ": "假设：说话直接、讲效率，喜欢把事情定下来；对情绪化的表达可能回应得比较生硬。",
    "ESFJ": "假设：重视互动氛围，回复积极、常主动开启话题；对被冷落会明显在意。",
    "ISTP": "假设：话少但动手能力强，偏好具体的事而不是情绪讨论；需要自己的空间。",
    "ISFP": "假设：表达含蓄、在意当下感受；不喜欢被安排，可能用回避代替拒绝。",
    "ESTP": "假设：反应快、爱开玩笑、行动优先；对长篇情绪表达可能耐心有限。",
    "ESFP": "假设：情绪外露、喜欢热闹互动，回复有画面感；对冷场比较敏感。",
}

# Zodiac signs are grouped by element; the text is about conversational rhythm, not fate.
_ZODIAC_ELEMENT_TEXT: dict[str, str] = {
    "火": "假设：节奏偏快、喜欢即兴发起；对拖沓的回应耐心较低。（娱乐性假设，权重低）",
    "土": "假设：偏好稳定与可预期，回复可能不快但较可靠；对突然的变动需要适应。（娱乐性假设，权重低）",
    "风": "假设：话题跳跃、爱聊想法；容易被新话题带走，也可能忘记回上一条。（娱乐性假设，权重低）",
    "水": "假设：在意语气与感受，回复常带情绪色彩；不满可能不直说。（娱乐性假设，权重低）",
}
ZODIAC_ELEMENT: dict[str, str] = {
    "aries": "火", "leo": "火", "sagittarius": "火",
    "taurus": "土", "virgo": "土", "capricorn": "土",
    "gemini": "风", "libra": "风", "aquarius": "风",
    "cancer": "水", "scorpio": "水", "pisces": "水",
}
ZODIAC_PRIORS: dict[str, str] = {sign: _ZODIAC_ELEMENT_TEXT[element] for sign, element in ZODIAC_ELEMENT.items()}

CHINESE_ZODIAC_PRIORS: dict[str, str] = {
    "rat": "假设：反应快、会察言观色，聊天里常接得住话。（娱乐性假设，权重最低）",
    "ox": "假设：慢热、说话实在，不太主动开启话题但回应认真。（娱乐性假设，权重最低）",
    "tiger": "假设：直接、有行动力，喜欢就事论事。（娱乐性假设，权重最低）",
    "rabbit": "假设：温和、避免冲突，可能用委婉方式表达不愿意。（娱乐性假设，权重最低）",
    "dragon": "假设：表达有气势、喜欢主导话题。（娱乐性假设，权重最低）",
    "snake": "假设：话不多但观察细，回复常点到关键。（娱乐性假设，权重最低）",
    "horse": "假设：节奏快、爱自由，不喜欢被追问行踪。（娱乐性假设，权重最低）",
    "goat": "假设：情绪敏感、在意氛围，容易因为语气受伤。（娱乐性假设，权重最低）",
    "monkey": "假设：机灵爱玩笑，常用幽默化解尴尬。（娱乐性假设，权重最低）",
    "rooster": "假设：表达清晰、要求明确，可能会纠正细节。（娱乐性假设，权重最低）",
    "dog": "假设：重信任与承诺，对失约的反应会比一般人大。（娱乐性假设，权重最低）",
    "pig": "假设：随和、包容度高，不太计较回应快慢。（娱乐性假设，权重最低）",
}
