"""Original action skeletons used as fallbacks (Rules mode, or when the LLM playbook fails validation).

Each skeleton is keyed by (goal, boundary, stage bucket). All entries carry stop_when.
"""

from __future__ import annotations

from rapport.schemas.profile import Goal
from rapport.schemas.report_v1 import Action, BoundaryValue, Landmine, Playbook, Script, StageValue

BOUNDARY_ACTIONS: dict[BoundaryValue, list[Action]] = {
    BoundaryValue.explicit_decline: [
        Action(
            goal="尊重对方已经表达的拒绝，同时保留正常交流的空间",
            action="不要再就同一件事发起邀请或追问原因。可以自然地聊别的，或者暂时降低频率，让对方感到没有压力。",
            suggested_text="好的，那就不勉强啦～",
            observe="接下来一两周对方是否会主动开启话题；如果没有，也不需要解读为讨厌你。",
            stop_when="对方再次表达不想继续这个话题，或连续多次不回应。",
            alternatives=["直接告诉对方你收到了、不会再提，然后把注意力放回自己的生活。"],
            personalized=False,
        )
    ],
    BoundaryValue.stop_contact: [
        Action(
            goal="尊重对方“不想继续联系”的表达",
            action="停止发起联系，不解释、不追问、不通过其他渠道或朋友转达。若需要，可以发一条简短的告别信息，然后不再发送。",
            suggested_text="收到，我不会再打扰你了。祝你一切都好。",
            observe="这一步没有需要观察的对方反应；观察的是你自己是否能守住这个决定。",
            stop_when="立即停止推进。任何“再试一次”的想法都是停止条件。",
            alternatives=["不发告别信息，直接停止联系。", "把想说的话写下来但不发送，找信任的朋友聊聊。"],
            personalized=False,
        )
    ],
    BoundaryValue.safety_concern: [
        Action(
            goal="优先处理安全，而不是关系推进",
            action="如果材料中涉及你或对方的人身安全、自伤或威胁，请联系可信的人或当地求助渠道。本工具不能提供危机干预。",
            suggested_text="",
            observe="—",
            stop_when="立即停止把这段关系当作“攻略”来推进。",
            alternatives=["与家人、朋友或专业人士沟通。"],
            personalized=False,
        )
    ],
}

GOAL_ACTIONS: dict[Goal, dict[str, Action]] = {
    Goal.invite: {
        "early": Action(
            goal="用低压力的方式试探一次线下或语音的可能",
            action="从你们聊过的具体话题延伸出一个小而具体的提议，给对方留出拒绝的台阶。",
            suggested_text="上次你说的那家店我一直记着，这周末或下周有空的话要不要一起去试试？没空也没关系～",
            observe="对方是直接答应、给替代时间，还是模糊带过。给替代时间是积极信号；模糊带过则先不追问。",
            stop_when="对方明确说不方便且没有提替代时间；或两次提议都没有正面回应。",
            alternatives=["先提议一个更轻的形式，比如分享一个链接问对方感不感兴趣。"],
            personalized=False,
        ),
        "warm": Action(
            goal="把已经出现的好感信号推进到一次明确的约定",
            action="直接给出一个具体时间和地点的邀请，避免“有空吗”这种开放式问题。",
suggested_text="周六下午两点，XX 附近有个展，我想和你一起去。可以吗？",
            observe="对方是否给出明确的是/否，或主动调整时间。",
            stop_when="对方明确拒绝且未给替代方案。",
            alternatives=["如果对方最近很忙，改为先约定“忙完这阵子”的一个时间点。"],
            personalized=False,
        ),
    },
    Goal.clarify: {
        "any": Action(
            goal="澄清一个具体的误会或疑问，而不是要求对方表态",
            action="只描述你观察到的事实和你的感受，不猜测对方动机，最后给对方一个容易回答的问题。",
            suggested_text="上次那件事之后我有点在意，想问问你当时是怎么想的？不管怎样我都想弄清楚，而不是猜。",
            observe="对方是否愿意解释；解释的内容是否与你的猜测不同。",
            stop_when="对方表示不想谈这个话题，或已经给出了解释。",
            alternatives=["先放一放，等下次自然聊到相关话题再顺势问。"],
            personalized=False,
        )
    },
    Goal.repair: {
        "any": Action(
            goal="为一次冲突承担自己的那部分，并邀请对方一起修复",
            action="具体指出你做得不好的地方（不是笼统道歉），说明你打算怎么做，然后询问对方的感受。",
            suggested_text="那天我说话太冲了，这是我的问题。我之后会先听你说完再表达。你现在还生气吗？",
            observe="对方是否接受、是否也说出自己的部分；如果对方需要时间，不要催。",
            stop_when="对方明确表示需要空间；或对方表示不想再谈这段关系。",
            alternatives=["如果文字容易误解，提议语音或见面谈。"],
            personalized=False,
        )
    },
    Goal.decide: {
        "any": Action(
            goal="给自己一个明确的观察期，而不是无限期等待",
            action="设定一个时间窗（如两周），在这期间保持自然交流，记录对方是否有主动、是否愿意见面。到期后根据记录做决定。",
            suggested_text="",
            observe="对方主动发起的次数、对你提议的回应、是否愿意安排见面。",
            stop_when="观察期结束；或期间对方明确表达了立场。",
            alternatives=["直接问一次对方对你们关系的看法，接受任何答案。"],
            personalized=False,
        )
    },
    Goal.understand: {
        "any": Action(
            goal="更准确地理解对方当前的互动方式",
            action="接下来一段时间，观察而不是测试：注意对方在什么话题上回复更长、更快，什么时候只回“嗯”。",
            suggested_text="",
            observe="对方主动开启的话题类型；你提问后对方是回答还是转移。",
            stop_when="你已经形成了自己的判断，或对方明确表达了立场。",
            alternatives=["直接分享一件你自己的事，看对方是否跟进提问。"],
            personalized=False,
        )
    },
}

SCRIPTS_DEFAULT = {
    "casual": Script(text="今天刷到一个东西突然想到你，哈哈。你最近怎么样？", when_to_use="日常重新开启话题，压力最低。"),
    "sincere": Script(text="其实和你聊天我挺开心的，想多了解你一点。", when_to_use="已经有一定互动基础，想让对方知道你的态度。"),
    "direct": Script(text="我想约你出来见一面，这周或下周你哪天方便？", when_to_use="对方已多次积极回应，适合明确推进。"),
}


def fallback_playbook(goal: Goal, boundary: BoundaryValue, stage: StageValue, landmines: list[Landmine] | None = None) -> Playbook:
    table = GOAL_ACTIONS[goal]
    if goal == Goal.invite:
        key = "warm" if stage in (StageValue.affectionate_signals, StageValue.mutually_confirmed, StageValue.stable) else "early"
    else:
        key = "any"
    action = table[key]
    if boundary != BoundaryValue.none:
        # v0.8: the boundary skeleton comes first as advice, the goal action stays (with a caution)
        # instead of being removed.
        lead = BOUNDARY_ACTIONS[boundary]
        goal_act = action.model_copy(update={"caution": "对方近期有拒绝或停止联系的表述，这个选项是推进类：先想清楚要不要做。"})
        return Playbook(
            actions=[*lead, goal_act],
            landmines=landmines or [],
            scripts=dict(SCRIPTS_DEFAULT),
            note="当前存在边界信号：第一条是尊重边界的做法，其余为通用行动骨架（未完成个性化）。",
        )
    return Playbook(
        actions=[action],
        landmines=landmines or [],
        scripts=dict(SCRIPTS_DEFAULT),
        note="以下为通用行动骨架（未完成个性化）。",
    )
