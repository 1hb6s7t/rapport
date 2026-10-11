"""Situation templates for rules mode (v0.5 §2): 情境 → 选项, from the feature layer only.

Each template is (predicate(Features) -> trigger ids or None, builder(goal) -> Situation). A
situation fires only when the predicate can point at real messages; the options it carries are
skeletons (personalized=False) with the same contract the model must meet: ≥2 expected
reactions (one of them a stop), a stop_when, a risk.
"""

from __future__ import annotations

from collections.abc import Callable

from rapport.features.compute import Features
from rapport.schemas.profile import Goal
from rapport.schemas.report_v1 import BoundaryValue, Option, Reaction, Situation

Predicate = Callable[[Features], list[str] | None]


def _opt(goal: str, action: str, trig: list[str], text: str, reactions: list[tuple[str, str, str]], stop: str, risk: str) -> Option:
    return Option(
        goal=goal,
        action=action,
        trigger_ids=trig[:6],
        suggested_text=text,
        expected_reactions=[Reaction(if_they=a, then=b, signal=c) for a, b, c in reactions],
        stop_when=stop,
        risk=risk,
        personalized=False,
    ).ensure_id()


# ---------- predicates ----------


def _chains(f: Features) -> list[dict]:
    return list((f.invitations or {}).get("chains") or [])


def p_unanswered_offers(f: Features) -> list[str] | None:
    ids = [c["propose_id"] for c in _chains(f) if c.get("response") in ("none", "deflect")]
    return ids[-3:] if len(ids) >= 2 else None


def p_alt_time_open(f: Features) -> list[str] | None:
    ids = [c.get("response_id") or c["propose_id"] for c in _chains(f) if c.get("response") == "alt_time" and not c.get("commit_ids")]
    return ids[-2:] if ids else None


def p_accepted_not_settled(f: Features) -> list[str] | None:
    ids = [c.get("response_id") or c["propose_id"] for c in _chains(f) if c.get("response") == "accept" and not c.get("commit_ids")]
    return ids[-2:] if ids else None


def p_explicit_decline(f: Features) -> list[str] | None:
    ids = (f.boundary_recent or f.boundary_hits).get("explicit_decline") or []
    return ids[-2:] if ids else None


def p_conflict_unresolved(f: Features) -> list[str] | None:
    if f.conflict_ids and not f.apology_ids:
        return f.conflict_ids[-3:]
    return None


def p_they_ask_you_answer(f: Features) -> list[str] | None:
    # The other person asks, you rarely do: an opening to ask back.
    if f.other.question_ids and len(f.me.question_ids) * 2 < len(f.other.question_ids):
        return f.other.question_ids[-3:]
    return None


def p_they_initiate(f: Features) -> list[str] | None:
    v = f.other.initiation_rate.value
    if v is not None and v >= 0.6 and f.n_sessions >= 4:
        return (f.other.care_ids or f.other.question_ids)[-2:] or None
    return None


# ---------- builders ----------


def s_unanswered_offers(trig: list[str], goal: Goal) -> Situation:
    return Situation(
        id="unanswered_offers",
        title="你提议了几次，对方没有正面回应",
        trigger_ids=trig,
        why="最近的提议没有得到接受、改期或拒绝，只是被绕开。",
        options=[
            _opt(
                "把选择权交给对方，一次说清",
                "不再重复同一提议。换成开放的一句：说明你想见面，问对方近期有没有方便的时候，然后停下来等。",
                trig,
                "最近老是我在提，你要是这阵子没心情或没空就直说，我不会介意的～有空的时候你说个时间？",
                [("给出一个时间或条件", "这是明确的正向信号，按对方给的时间定下来", "good"), ("再次绕开或只回表情", "两次绕开就是答案，把注意力放回自己", "stop"), ("说最近忙、以后再说", "接受这个说法，两周内不再提", "neutral")],
                "对方再次绕开，或明确说不想。",
                "对方可能感到被施压；语气要轻。",
            ),
            _opt(
                "先退一步，只做轻互动",
                "接下来一两周不提见面，只分享有趣的东西、回应对方的话题，看对方是否主动。",
                trig,
                "",
                [("对方开始主动开话题", "说明压力小了互动反而多，之后再提议", "good"), ("互动继续冷", "接受现状，不要用更多消息去填", "stop")],
                "两周内对方没有任何主动。",
                "这不是策略性冷处理，是真的降低频率；别一边退一边试探。",
            ),
        ],
    )


def s_alt_time_open(trig: list[str], goal: Goal) -> Situation:
    return Situation(
        id="alt_time_open",
        title="对方改了时间，但还没定下来",
        trigger_ids=trig,
        why="对方没有拒绝，给了替代时间，只是还没落到具体安排。",
        options=[
            _opt(
                "接住对方给的时间，把它定下来",
                "直接按对方说的时间给出一个具体提议（哪天、几点、在哪），只问一个是否。",
                trig,
                "那就按你说的这周末？周六下午三点，XX 见，可以的话我订位子。",
                [("直接答应或微调", "定下来后不要再确认第三次", "good"), ("又改一次时间", "再接一次；连续第三次改就停", "neutral"), ("没回或含糊", "不追问，一周后再自然提一次", "stop")],
                "对方连续第三次改期，或不再回应。",
                "别在对方给出时间后再抛更多选项，会显得没听进去。",
            ),
        ],
    )


def s_accepted_not_settled(trig: list[str], goal: Goal) -> Situation:
    return Situation(
        id="accepted_not_settled",
        title="对方答应了，时间还没落实",
        trigger_ids=trig,
        why="有接受的表述，但没有出现具体时间地点。",
        options=[
            _opt(
                "把「好呀」变成一个具体时间",
                "给两个具体时段让对方选，不要再问「什么时候」。",
                trig,
                "周六下午还是周日晚上？我都可以，你挑一个。",
                [("选了一个", "定下来，发个位置，不再反复确认", "good"), ("说都不行", "问一次对方方便的时间，之后停", "neutral"), ("不回", "一周内不再提", "stop")],
                "对方两次都没有给出时间。",
                "答应不等于急着定；给对方两天再问。",
            ),
        ],
    )


def s_explicit_decline(trig: list[str], goal: Goal) -> Situation:
    return Situation(
        id="explicit_decline",
        title="对方明确拒绝过一次提议",
        trigger_ids=trig,
        why="材料里有明确拒绝且没有替代方案。",
        options=[
            _opt(
                "尊重拒绝，保留正常交流",
                "不就同一件事再发起。可以自然聊别的，或降低频率，让对方感到没有压力。",
                trig,
                "好的，那就不勉强啦～",
                [("对方之后主动开话题", "交流还在，只是这件事不行", "good"), ("对方回应变少", "不需要解读为讨厌你，给彼此空间", "neutral"), ("对方再次表达不想", "停止推进", "stop")],
                "对方再次表达不想继续这个话题，或连续多次不回应。",
                "解释或追问原因会让拒绝变得更重。",
            ),
        ],
    )


def s_conflict_unresolved(trig: list[str], goal: Goal) -> Situation:
    return Situation(
        id="conflict_unresolved",
        title="有过争执，还没有人先松口",
        trigger_ids=trig,
        why="出现过冲突表述，之后没有道歉或和解的句子。",
        options=[
            _opt(
                "先认自己那一部分",
                "具体说出你做得不好的地方（不是笼统道歉），说明你打算怎么做，然后问对方的感受。",
                trig,
                "那天我说话太冲了，这是我的问题。我之后会先听你说完。你现在还生气吗？",
                [("对方也说出自己的部分", "这是修复在发生，不要急着翻篇", "good"), ("对方说需要时间", "给时间，不催", "neutral"), ("对方说不想再谈", "停止，尊重", "stop")],
                "对方明确表示需要空间，或不想再谈这段关系。",
                "文字容易误读；如果对方愿意，改语音。",
            ),
        ],
    )


def s_they_ask_you_answer(trig: list[str], goal: Goal) -> Situation:
    return Situation(
        id="they_ask_you_answer",
        title="对方常提问，你很少问回去",
        trigger_ids=trig,
        why="对方的提问明显多于你的，互动是单向了解。",
        options=[
            _opt(
                "把对方问过你的，问回去",
                "挑一个对方问过你的话题，认真问回去，并跟进一个追问。",
                trig,
                "你上次问我周末干嘛，那你呢？你周末一般怎么过？",
                [("对方展开讲", "投入在上升，继续跟进细节", "good"), ("对方简短带过", "不勉强，换个更轻的话题", "neutral"), ("对方转移话题", "不要再问，先观察", "stop")],
                "对方两次都没有展开。",
                "一次问太多会像审问；一个问题加一个追问就够。",
            ),
        ],
    )


def s_they_initiate(trig: list[str], goal: Goal) -> Situation:
    return Situation(
        id="they_initiate",
        title="对方经常主动开启对话",
        trigger_ids=trig,
        why="会话由对方开启的比例较高。",
        options=[
            _opt(
                "接住对方的主动，回一个具体提议",
                "下次对方开启话题时，从那个话题延伸出一个小而具体的邀请。",
                trig,
                "你说的那家店我记着呢，这周末要不要一起去？没空也没关系。",
                [("接受或给时间", "定下来", "good"), ("模糊带过", "不追问，下次再从别的话题延伸", "neutral"), ("明确不想", "停止，回到日常交流", "stop")],
                "对方明确说不方便且没有替代时间。",
                "对方主动聊不等于想见面；提议要轻。",
            ),
        ],
    )


SITUATION_TEMPLATES: list[tuple[str, Predicate, Callable[[list[str], Goal], Situation]]] = [
    ("explicit_decline", p_explicit_decline, s_explicit_decline),
    ("conflict_unresolved", p_conflict_unresolved, s_conflict_unresolved),
    ("unanswered_offers", p_unanswered_offers, s_unanswered_offers),
    ("alt_time_open", p_alt_time_open, s_alt_time_open),
    ("accepted_not_settled", p_accepted_not_settled, s_accepted_not_settled),
    ("they_ask_you_answer", p_they_ask_you_answer, s_they_ask_you_answer),
    ("they_initiate", p_they_initiate, s_they_initiate),
]


def rule_situations(f: Features, goal: Goal, boundary: BoundaryValue, limit: int = 3) -> list[Situation]:
    """Situations the feature layer can prove. v0.8: a boundary no longer removes situations; it
    prefixes the reason and marks advancing options with a caution."""
    out: list[Situation] = []
    note = {
        BoundaryValue.stop_contact: "⚠ 对方近期表达了不想继续联系。",
        BoundaryValue.safety_concern: "⚠ 材料中出现安全相关信号。",
        BoundaryValue.explicit_decline: "⚠ 对方近期明确拒绝过一项提议。",
    }.get(boundary, "")
    advancing = {"unanswered_offers", "alt_time_open", "accepted_not_settled", "they_initiate"}
    for _key, pred, build in SITUATION_TEMPLATES:
        trig = pred(f)
        if not trig:
            continue
        s = build(trig, goal)
        if note:
            s.why = (note + " " + s.why).strip()
            if s.id in advancing:
                for o in s.options:
                    o.caution = "对方近期有拒绝或停止联系的表述，这个选项是推进类：先想清楚要不要做。"
        out.append(s)
        if len(out) >= limit:
            break
    return out
