"""Intent evaluation set and harness (Phase 1's "ruler").

Why this exists: on 2026-09-15 a first 20-scenario probe measured only 9/20 correct with
context and 2/20 high-confidence errors, with the model systematically over-reading coldness.
That is too weak to ship and too weak to tune against by feel, so this harness fixes the
measurement first.

Design rules:
- Every scenario is a **synthetic** conversation written for this project. No real chats.
- 30 surface utterances x 4 different contexts = 120 scenarios (batch B adds forms batch A did not use), so the same words appear with
  different gold labels. A model that ignores context cannot score well by accident.
- Gold labels are **multi-label** (real anger and 撒娇 can coexist), matching the contract.
- The harness drives the shipped code path: parse -> episodes -> reference retrieval ->
  run_intent -> validate_intent. It does not reimplement the prompt logic.
- Conditions: `context` (episode in review mode + reference examples), `no_refs` (episode but
  no reference library) and `single` (the utterance alone). The last one is the honesty check:
  a model that answers confidently without context is guessing.

Usage:
    uv run python ../scripts/intent_eval.py "C:/path/API KEY.txt" \
        --model "DeepSeek V4.1 Flash" --out ../docs/EVAL-RESULTS.md
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _probe import Probe as _Probe  # noqa: E402
from real_byok_check import load_key  # noqa: E402

from rapport.features.compute import compute_features  # noqa: E402
from rapport.features.episodes import build_episodes, episode_for  # noqa: E402
from rapport.knowledge.reference import load_library, load_patterns, retrieve  # noqa: E402
from rapport.llm.intent import build_intent_input, run_intent, validate_intent  # noqa: E402
from rapport.llm.provider import LLMError  # noqa: E402
from rapport.llm.redact import RedactStats  # noqa: E402
from rapport.parsers import ParseOptions, parse  # noqa: E402
from rapport.schemas.report_v1 import PRAGMATIC_TAGS  # noqa: E402
from rapport.scoring.affinity import independent_units as count_units  # noqa: E402

# ---------------------------------------------------------------- evaluation set
# (surface, context lines, gold pragmatic tags, gold emotions)

G = {
    "anger": ["真生气"],
    "sulk": ["撒娇假生气"],
    "tease": ["调侃"],
    "irony": ["反讽"],
    "decline": ["委婉拒绝"],
    "comfort": ["寻求安慰"],
    "info": ["信息确认"],
    # v2 (v0.7)
    "share": ["分享陈述"],
    "ask": ["询问"],
    "opinion": ["观点评价"],
    "complain": ["抱怨"],
    "agree": ["同意接受"],
}

PROBES: list[tuple[str, list[str], list[str], list[str]]] = [
    # 1) 我生气了
    ("我生气了", ["A: 你不是说六点一定到吗", "B: 路上堵车", "A: 上次你也是这么说的"], G["anger"], ["不满"]),
    ("我生气了", ["A: 你今天怎么这么笨哈哈哈", "B: 你自己也没好到哪去", "A: 那我们俩是笨蛋组合"], G["tease"], ["开心"]),
    ("我生气了", ["A: 我给你点了奶茶，猜你在加班", "B: 你怎么知道", "A: 猜的呀"], G["sulk"], ["开心", "亲近"]),
    ("我生气了", ["A: 我把你的事情跟别人说了", "B: 你说什么", "A: 开个玩笑嘛"], G["anger"], ["不满", "伤心"]),
    # 2) 你真讨厌
    ("你真讨厌", ["A: 我记得你说过想喝那家豆乳", "B: 你居然还记得", "A: 顺手买的"], G["sulk"], ["开心", "亲近"]),
    ("你真讨厌", ["A: 这种事不好笑，你别再提了", "B: 至于这么认真吗", "A: 我说了不好笑"], G["anger"], ["不满"]),
    ("你真讨厌", ["A: 你自己说要早睡的", "B: 你还说你要减肥呢", "A: 别扯我"], G["tease"], ["开心"]),
    ("你真讨厌", ["A: 我把闹钟关了你睡吧", "B: 你怎么老这样", "A: 我怕你太累"], G["sulk"], ["开心"]),
    # 3) 随便你
    ("随便你", ["A: 周末去爬山吧", "B: 那天我要加班", "A: 你就是不想陪我"], G["anger"], ["不满"]),
    ("随便你", ["A: 晚上吃火锅还是日料", "B: 你选吧我都可以", "A: 那你选"], ["同意接受"], ["不确定"]),
    ("随便你", ["A: 我按我的想法定了", "B: 你从来不问我的意见", "A: 那你说啊"], G["anger"], ["不满"]),
    ("随便你", ["A: 你想喝什么我去买", "B: 都行", "A: 那我就买常喝的那家"], ["同意接受"], ["开心"]),
    # 4) 呵呵
    ("呵呵", ["A: 我这不是为了你好吗", "B: 你想过我要不要吗", "A: 那你说怎么办"], G["irony"], ["不满"]),
    ("呵呵", ["A: 那只猫摔了个跟头哈哈哈", "B: 你也太没同情心了", "A: 它爬起来还装作没事"], G["tease"], ["开心"]),
    ("呵呵", ["A: 你不是说一定做到吗", "B: 计划赶不上变化", "A: 每次都是这个理由"], G["irony"], ["不满"]),
    ("呵呵", ["A: 你看我新学的魔术", "B: 上次那个我还记得", "A: 这次不一样"], G["tease"], ["开心"]),
    # 5) 好吧
    ("好吧", ["A: 周六那个展我们去吧", "B: 好啊", "A: 那就周六下午两点"], ["同意接受"], ["开心"]),
    ("好吧", ["A: 对不起，周末要出差，见面取消", "B: 哦", "A: 你别生气"], G["anger"], ["不满", "伤心"]),
    ("好吧", ["A: 我把行程发你了", "B: 收到", "A: 有问题随时说"], ["信息确认"], ["不确定"]),
    ("好吧", ["A: 我今晚要陪家里人，不能打电话了", "B: 嗯", "A: 明天补给你"], G["anger"], ["伤心"]),
    # 6) 你忙吧
    ("你忙吧", ["A: 在吗，想和你说个事", "B: 在改方案，晚点说", "A: 那算了"], G["anger"], ["不满"]),
    ("你忙吧", ["A: 我今天要通宵赶稿", "B: 那你别熬太晚", "A: 知道了"], G["info"], ["亲近"]),
    ("你忙吧", ["A: 我在跟朋友吃饭", "B: 那我不打扰了", "A: 你怎么又这样"], G["anger"], ["不满"]),
    ("你忙吧", ["A: 我这边会开到九点", "B: 好，我先去健身", "A: 十点见"], G["info"], ["不确定"]),
    # 7) 不用了
    ("不用了", ["A: 要我去接你吗，下雨了", "B: 不用了，我打车", "A: 真的不用"], G["decline"], ["不确定"]),
    ("不用了", ["A: 我给你带份早饭吧", "B: 不用了", "A: 上次你也说不用"], G["irony"], ["不满"]),
    ("不用了", ["A: 要不要帮你问问同事", "B: 不用了，我自己处理", "A: 好吧"], G["decline"], ["不确定"]),
    ("不用了", ["A: 我陪你一起去吧", "B: 不用了", "A: 你每次都这么说"], G["irony"], ["不满"]),
    # 8) 我没事
    ("我没事", ["A: 刚才那话我不是那个意思", "B: 嗯", "A: 那你怎么不说话"], G["anger"] + G["comfort"], ["伤心"]),
    ("我没事", ["A: 抱歉踩到你脚了", "B: 我没事", "A: 疼吗"], ["同意接受"], ["不确定"]),
    ("我没事", ["A: 你今天一整天都没理我", "B: 我在忙", "A: 我知道你忙"], G["anger"] + G["comfort"], ["伤心", "不满"]),
    ("我没事", ["A: 那个方案被否了", "B: 我没事，下次再来", "A: 你确定"], ["分享陈述"], ["不确定"]),
    # 9) 好的呢
    ("好的呢", ["A: 帮我把文件发一下", "B: 好的呢", "A: 谢谢"], ["同意接受"], ["开心"]),
    ("好的呢", ["A: 你顺便把下周报表也做了吧", "B: 好的呢", "A: 你最近是不是很累"], G["decline"], ["不满"]),
    ("好的呢", ["A: 记得带伞", "B: 好的呢", "A: 到家说一声"], ["同意接受"], ["亲近"]),
    ("好的呢", ["A: 周末你帮我值个班吧", "B: 好的呢", "A: 你上次也答应了"], G["decline"], ["不满"]),
    # 10) 就这样吧
    ("就这样吧", ["A: 那我们把方案定成A版", "B: 就这样吧", "A: 那就这么办"], ["同意接受"], ["不确定"]),
    ("就这样吧", ["A: 你到底想怎么样", "B: 我不想吵了", "A: 那你说清楚"], G["anger"], ["不满", "伤心"]),
    ("就这样吧", ["A: 时间定在周六下午", "B: 就这样吧", "A: 我发日历邀请"], ["同意接受"], ["不确定"]),
    ("就这样吧", ["A: 你又开始翻旧账", "B: 我没什么好说的", "A: 你总这样"], G["anger"], ["不满"]),
    # 11) 知道了知道了
    ("知道了知道了", ["A: 你昨天怎么不回我", "B: 手机没电了", "A: 那你充上电也不回"], G["anger"], ["不满"]),
    ("知道了知道了", ["A: 记得把材料带上", "B: 知道了知道了", "A: 别忘就行"], ["信息确认"], ["不确定"]),
    ("知道了知道了", ["A: 我说过很多次了", "B: 知道了知道了", "A: 你根本没听"], G["anger"], ["不满"]),
    ("知道了知道了", ["A: 明天八点出发", "B: 知道了知道了", "A: 我定闹钟了"], ["信息确认"], ["开心"]),
    # 12) 我看看安排
    ("我看看安排", ["A: 周末有空吗", "B: 怎么啦", "A: 想约你看电影"], G["decline"], ["不确定"]),
    ("我看看安排", ["A: 你几点能到", "B: 我看看安排", "A: 大概就行"], G["info"], ["不确定"]),
    ("我看看安排", ["A: 下周一起吃个饭吧", "B: 我看看安排", "A: 你上次也说看看"], G["decline"], ["不满"]),
    ("我看看安排", ["A: 明天要开个短会", "B: 我看看安排", "A: 十分钟就好"], G["info"], ["不确定"]),
    # 13) 行，你真行
    ("行，你真行", ["A: 我把你上次的录音删了", "B: 那是我给客户做的", "A: 我不知道"], G["irony"], ["不满"]),
    ("行，你真行", ["A: 我把你藏起来的零食吃完了", "B: 行，你真行", "A: 我给你买回来"], G["tease"], ["开心"]),
    ("行，你真行", ["A: 我一个人把两个人的活干了", "B: 行，你真行", "A: 下次别找我"], G["irony"], ["不满"]),
    ("行，你真行", ["A: 我做完饭还把碗洗了", "B: 行，你真行", "A: 夸得这么勉强"], G["tease"], ["开心"]),
    # 14) 那你下次别等我了
    ("那你下次别等我了", ["A: 你又迟到了半小时", "B: 路上真的堵", "A: 每次都堵"], G["anger"], ["不满"]),
    ("那你下次别等我了", ["A: 我迟到了，你别生气", "B: 那你下次别等我了", "A: 我请你喝奶茶"], G["tease"], ["开心"]),
    ("那你下次别等我了", ["A: 我等了你一个小时", "B: 我又没让你等", "A: 行"], G["anger"], ["不满"]),
    ("那你下次别等我了", ["A: 天太冷了，你不用下来", "B: 那你下次别等我了", "A: 顺路嘛"], G["tease"], ["亲近"]),
    # 15) 我陪你去吧
    ("我陪你去吧", ["A: 我明天要去医院复查", "B: 几点", "A: 早上八点"], G["comfort"], ["亲近"]),
    ("我陪你去吧", ["A: 我要去取个快递", "B: 我陪你去吧", "A: 就在楼下"], ["分享陈述"], ["不确定"]),
    ("我陪你去吧", ["A: 面试结果今天出", "B: 要不要一起看", "A: 我有点怕"], G["comfort"], ["亲近"]),
    ("我陪你去吧", ["A: 我要去趟超市", "B: 我陪你去吧", "A: 那顺便买菜"], ["分享陈述"], ["开心"]),
]

# ---- batch B: surface forms the first batch did not use ------------------------------

PROBES.extend(
    [
        # 你在忙吗
        ("你在忙吗", ["A: 我昨天发的消息你看到了吗", "B: 看到了", "A: 那你为什么不回", "B: 在忙"], G["anger"], ["不满"]),
        ("你在忙吗", ["A: 在改方案，晚点回你", "B: 好", "A: 现在方便了吗"], ["询问"], ["不确定"]),
        ("你在忙吗", ["A: 今天好无聊", "B: 在上班", "A: 那你忙吧", "B: 你在忙吗"], G["sulk"], ["开心", "亲近"]),
        ("你在忙吗", ["A: 我这边会开到九点", "B: 你几点能聊", "A: 十点", "B: 你在忙吗"], ["询问"], ["不确定"]),
        # 都听你的
        ("都听你的", ["A: 那这事就按我说的办", "B: 我提的意见你听过吗", "A: 你的太麻烦了", "B: 都听你的"], G["irony"], ["不满"]),
        ("都听你的", ["A: 晚上吃什么", "B: 你定", "A: 那我选火锅", "B: 都听你的"], ["同意接受"], ["开心"]),
        ("都听你的", ["A: 你从来没考虑过我的感受", "B: 我不是都让着你吗", "A: 那不一样", "B: 都听你的"], G["anger"], ["不满", "伤心"]),
        ("都听你的", ["A: 周末去哪玩", "B: 都听你的", "A: 你上次也这么说"], G["tease"], ["开心"]),
        # 我随便
        ("我随便", ["A: 你想吃什么", "B: 我随便", "A: 你认真点行不行", "B: 我真的很随便"], ["同意接受"], ["不确定"]),
        ("我随便", ["A: 你到底想不想去", "B: 我随便", "A: 你能不能给个明确答复", "B: 我随便"], G["decline"], ["不满"]),
        ("我随便", ["A: 你又在敷衍我", "B: 我没有", "A: 那你选啊", "B: 我随便"], G["anger"], ["不满"]),
        ("我随便", ["A: 我带你去个地方", "B: 我随便", "A: 保证你喜欢", "B: 那我等着"], G["tease"], ["开心"]),
        # 我不打扰你了
        ("我不打扰你了", ["A: 我刚才在忙", "B: 嗯", "A: 你在生气吗", "B: 我不打扰你了"], G["anger"], ["不满"]),
        ("我不打扰你了", ["A: 我要陪家里人吃饭", "B: 好", "A: 晚点找你", "B: 我不打扰你了"], G["decline"], ["不确定"]),
        ("我不打扰你了", ["A: 我今天加班到很晚", "B: 那你早点休息", "A: 谢谢理解"], ["同意接受"], ["亲近"]),
        ("我不打扰你了", ["A: 我周末要出差", "B: 那你路上小心", "A: 就两天", "B: 我不打扰你了"], G["sulk"], ["亲近"]),
        # 你早点睡
        ("你早点睡", ["A: 我今天不想聊了", "B: 怎么了", "A: 没什么", "B: 你早点睡"], G["anger"], ["不满", "伤心"]),
        ("你早点睡", ["A: 我在赶方案", "B: 你早点睡", "A: 你先睡吧"], ["分享陈述"], ["亲近"]),
        ("你早点睡", ["A: 再说会儿", "B: 明天还要早起呢", "A: 好吧", "B: 你早点睡"], ["分享陈述"], ["亲近"]),
        ("你早点睡", ["A: 你昨天也是这么说的，结果聊到两点", "B: 那你别等我了", "A: 你早点睡"], G["tease"], ["开心"]),
        # 我们改天再聊
        ("我们改天再聊", ["A: 那件事你想好了吗", "B: 还没", "A: 都问了三遍了", "B: 我们改天再聊"], G["decline"], ["不满"]),
        ("我们改天再聊", ["A: 我今天有点累", "B: 那我们改天再聊", "A: 谢谢", "B: 早点休息"], ["同意接受"], ["亲近"]),
        ("我们改天再聊", ["A: 你到底怎么想的", "B: 我不想说了", "A: 你总这样", "B: 我们改天再聊"], G["anger"], ["不满"]),
        ("我们改天再聊", ["A: 你刚才说的那个太有意思了", "B: 下次接着聊", "A: 我们改天再聊"], G["tease"], ["开心"]),
        # 我先忙了
        ("我先忙了", ["A: 你听我说完", "B: 我在开会", "A: 每次都这样", "B: 我先忙了"], G["decline"], ["不满"]),
        ("我先忙了", ["A: 你在干嘛", "B: 在赶稿", "A: 那你先忙", "B: 我先忙了"], ["分享陈述"], ["不确定"]),
        ("我先忙了", ["A: 你是不是不想理我", "B: 没有", "A: 那你为什么总是忙", "B: 我先忙了"], G["anger"], ["不满"]),
        ("我先忙了", ["A: 别熬夜啊", "B: 我先忙了", "A: 忙完说一声"], ["分享陈述"], ["亲近"]),
        # 你说得都对
        ("你说得都对", ["A: 我这是为你好", "B: 你想过我的想法吗", "A: 你说说看", "B: 你说得都对"], G["irony"], ["不满"]),
        ("你说得都对", ["A: 这个方案我改了三版", "B: 你说得都对", "A: 那还有问题吗", "B: 没有了"], ["同意接受"], ["不确定"]),
        ("你说得都对", ["A: 你就是不讲道理", "B: 行，你说得都对", "A: 你别这种语气", "B: 那你要我怎么样"], G["anger"], ["不满"]),
        ("你说得都对", ["A: 我觉得我讲得好有道理", "B: 你说得都对", "A: 你这是夸我还是损我", "B: 你猜"], G["tease"], ["开心"]),
        # 你想多了
        ("你想多了", ["A: 你刚才那句话什么意思", "B: 没什么意思", "A: 怎么可能", "B: 你想多了"], G["anger"], ["不满"]),
        ("你想多了", ["A: 你是不是在躲着我？", "B: 没有", "A: 真的吗?", "B: 你想多了"], ["观点评价"], ["不确定"]),
        ("你想多了", ["A: 我觉得他喜欢你", "B: 你想多了", "A: 我认真的", "B: 那也没有"], G["tease"], ["开心"]),
        ("你想多了", ["A: 你最近是不是变了", "B: 你想多了", "A: 那你怎么解释", "B: 我没什么好解释的"], G["irony"], ["不满"]),
        # 我无所谓
        ("我无所谓", ["A: 那你的意见呢", "B: 我无所谓", "A: 你每次都这样", "B: 那你要我怎样"], G["anger"], ["不满"]),
        ("我无所谓", ["A: 喝奶茶还是咖啡", "B: 我无所谓", "A: 那我买奶茶", "B: 好"], ["同意接受"], ["不确定"]),
        ("我无所谓", ["A: 我们一起去吧", "B: 我无所谓", "A: 你怎么这么冷淡", "B: 我只是不挑"], G["decline"], ["不满"]),
        ("我无所谓", ["A: 我打算给你个惊喜", "B: 我无所谓", "A: 那我不给了", "B: 别别别"], G["tease"], ["开心"]),
        # 别理我了
        ("别理我了", ["A: 我刚才说话重了点", "B: 你是重了点", "A: 那我不说了", "B: 别理我了"], G["anger"], ["不满", "伤心"]),
        ("别理我了", ["A: 你最近都不怎么找我", "B: 工作太忙了", "A: 我知道", "B: 别理我了"], G["sulk"], ["亲近"]),
        ("别理我了", ["A: 我们聊聊", "B: 我现在不想聊", "A: 就十分钟", "B: 别理我了"], G["decline"], ["不满"]),
        ("别理我了", ["A: 你今天怎么这么笨", "B: 你才笨", "A: 哈哈哈", "B: 别理我了"], G["tease"], ["开心"]),
        # 我错了行了吧
        ("我错了行了吧", ["A: 你每次都这样", "B: 我怎么了", "A: 你自己心里清楚", "B: 我错了行了吧"], G["anger"], ["不满"]),
        ("我错了行了吧", ["A: 你是不是又忘了", "B: 我错了行了吧", "A: 你这态度", "B: 我是真的忘了"], G["irony"], ["不满"]),
        ("我错了行了吧", ["A: 你说好陪我看电影的", "B: 我错了行了吧", "A: 那你补偿我", "B: 好呀"], G["sulk"], ["开心"]),
        ("我错了行了吧", ["A: 你把我零食吃完了？", "B: 我错了行了吧", "A: 就这么一句？", "B: 我买回来"], G["tease"], ["开心"]),
        # 没看到消息
        ("没看到消息", ["A: 你三天没回我了", "B: 没看到消息", "A: 你在线还发朋友圈", "B: 那不一样"], G["anger"], ["不满"]),
        ("没看到消息", ["A: 你怎么才回", "B: 手机静音了，没看到消息", "A: 好吧", "B: 抱歉"], ["分享陈述"], ["不确定"]),
        ("没看到消息", ["A: 我发的消息你看了吗", "B: 没看到消息", "A: 每次都这样", "B: 真没看到"], G["decline"], ["不满"]),
        ("没看到消息", ["A: 我给你发了个特别好笑的东西", "B: 没看到消息", "A: 你错过了", "B: 再发一次"], G["tease"], ["开心"]),
        # 你决定吧
        ("你决定吧", ["A: 周末去哪里", "B: 你决定吧", "A: 你从来不出主意", "B: 那你决定"], G["anger"], ["不满"]),
        ("你决定吧", ["A: 那家店要订位吗", "B: 你决定吧", "A: 好", "B: 听你的"], ["同意接受"], ["开心"]),
        ("你决定吧", ["A: 我们要不要再见一次", "B: 你决定吧", "A: 我想听你的想法", "B: 我没有想法"], G["decline"], ["不确定"]),
        ("你决定吧", ["A: 今晚吃什么", "B: 你决定吧", "A: 那吃火锅", "B: 都行"], ["同意接受"], ["不确定"]),
        # 我等你
        ("我等你", ["A: 我可能要迟到很久", "B: 我等你", "A: 你先回去吧", "B: 没事"], ["分享陈述"], ["亲近"]),
        ("我等你", ["A: 我要加班到十点", "B: 我等你", "A: 你别等了", "B: 那我也等"], G["sulk"], ["亲近"]),
        ("我等你", ["A: 我这边还要一会儿", "B: 我等你", "A: 你上次说等我结果先走了", "B: 这次真的等"], G["anger"], ["不满"]),
        ("我等你", ["A: 我在改方案，可能很晚", "B: 我等你", "A: 你先睡吧", "B: 我说了等就等"], G["comfort"], ["亲近"]),
    ]
)


# ---------------------------------------------------------------- harness


def build_conversation(probe_id: str, context: list[str], utterance: str):
    text = "\n".join(context + [f"B: {utterance}"])
    conv, _ = parse(text, probe_id, "paste", None, ParseOptions())
    return conv


def _unpack(probe) -> tuple[str, list[str], list[str], list[str], dict]:
    """PROBES rows are 4-tuples; `--cases` rows carry a fifth element with case metadata."""
    if len(probe) == 5:
        return probe[0], probe[1], probe[2], probe[3], probe[4] or {}
    utterance, context, gold_tags, gold_emotions = probe
    return utterance, context, gold_tags, gold_emotions, {}


def evaluate_case(provider, index: int, probe, condition: str, library, patterns: list | None = None, cues: bool = False, samples: int = 1) -> dict:
    utterance, context, gold_tags, gold_emotions, meta = _unpack(probe)
    # Three digits, matching the positional ids `apply_frozen_labels` looks up (s001..) — a
    # two-digit id here meant an adjudicated label could never be matched to the row it labelled.
    pid = meta.get("id") or f"s{index:03d}"
    conv = build_conversation(pid, context, utterance)
    events = conv.events()
    blocks = build_episodes(conv, events)
    target = events[-1]
    if condition == "single":
        ep = episode_for(conv, blocks, target, mode="reply", max_messages=1)
    else:
        ep = episode_for(conv, blocks, target, mode="reply")
    assert ep is not None
    refs = []
    if condition == "context":
        refs = [e.prompt_dict() for e in retrieve(" ".join(context) + " " + utterance, library, k=4)]
    inp = build_intent_input(conv, ep, RedactStats(), refs, patterns=patterns if condition == "context" else None, cues=cues and condition == "context")
    if samples >= 2:
        from rapport.llm.sampling import run_intent_sampled

        data, usage, _draws = run_intent_sampled(provider, inp, k=samples)
    else:
        data, usage = run_intent(provider, inp)
    raw_tags = [
        (t.get("tag") if isinstance(t, dict) else t)
        for t in (data.get("pragmatic_tags") or [])
    ]
    raw_tags = [t for t in raw_tags if t in PRAGMATIC_TAGS]
    # The gate must see the unit count of the actual material, not a value chosen per condition,
    # otherwise we would be measuring our own harness instead of the product.
    units, _kind = count_units(compute_features(conv, events), events)
    reading = validate_intent(data, inp, independent_units=units)
    return {
        "id": pid,
        "condition": condition,
        "utterance": utterance,
        "gold_tags": gold_tags,
        "gold_emotions": gold_emotions,
        "raw_tags": raw_tags,
        "tags": reading.pragmatic_tags,
        "units": units,
        "emotions": reading.emotions,
        "ambiguous": reading.ambiguous,
        "confidence": reading.model_confidence_raw,
        "gated": bool(reading.suppressed_reason),
        "json_ok": isinstance(data, dict) and "pragmatic_tags" in data,
        "tokens_in": usage.prompt_tokens,
        "tokens_out": usage.completion_tokens,
        # Exported so a wrong call can be read back and checked, not just counted: which tags were
        # demoted to candidates, what evidence each surviving tag rests on, what speaks against it,
        # and how strong the model itself judged the evidence. None of this feeds `score()`.
        "candidates": [
            {"reading": c.reading, "likelihood": str(c.likelihood), "basis_ids": list(c.basis_ids)}
            for c in reading.candidates
        ],
        "tag_basis": {k: list(v) for k, v in reading.tag_basis.items()},
        "counter_evidence_ids": list(reading.counter_evidence_ids),
        "evidence_ids": list(reading.evidence_ids),
        "evidence_strength": str(reading.evidence_strength),
        "boundary_status": reading.boundary_status,
        "suppressed_reason": reading.suppressed_reason,
        "stratum": meta.get("stratum", "synthetic"),
        "gold_boundary": meta.get("boundary", ""),
        "consistency": reading.consistency,
        "samples": reading.samples,
    }


# The keys every row carries, success or failure, so a failure row and a normal row render through
# the same table instead of one silently missing a column. `error` is the one extra key a failure
# row adds; a test pins both producers (`evaluate_case` / `_error_row`) to this set.
ROW_FIELDS = (
    "id",
    "condition",
    "utterance",
    "gold_tags",
    "gold_emotions",
    "raw_tags",
    "tags",
    "units",
    "emotions",
    "ambiguous",
    "confidence",
    "gated",
    "json_ok",
    "tokens_in",
    "tokens_out",
    "candidates",
    "tag_basis",
    "counter_evidence_ids",
    "evidence_ids",
    "evidence_strength",
    "boundary_status",
    "suppressed_reason",
    "stratum",
    "gold_boundary",
    "consistency",
    "samples",
)


def _error_row(i: int, probe, condition: str, error: str) -> dict:
    """A recorded failure. Kept in the results so a flaky call is visible, never silently dropped."""
    utterance, _ctx, gold_tags, gold_emotions, meta = _unpack(probe)
    return {
        "id": meta.get("id") or f"s{i:03d}",
        "condition": condition,
        "utterance": utterance,
        "gold_tags": gold_tags,
        "gold_emotions": gold_emotions,
        "raw_tags": [],
        "tags": [],
        "units": 0,
        "emotions": [],
        "ambiguous": True,
        "confidence": None,
        "gated": False,
        "json_ok": False,
        "tokens_in": 0,
        "tokens_out": 0,
        "candidates": [],
        "tag_basis": {},
        "counter_evidence_ids": [],
        "evidence_ids": [],
        "evidence_strength": None,
        "boundary_status": "",
        "suppressed_reason": None,
        "stratum": meta.get("stratum", "synthetic"),
        "gold_boundary": meta.get("boundary", ""),
        "consistency": None,
        "samples": 1,
        "error": error,
    }


# Gold "none" (from label_map.py) means the fixed vocabulary has no class for this message. It is
# scored on its own axis (`none_false_positive_rate`), never as a tag the model should emit.
NONE_TAG = "none"
NEGATIVE_TAGS = {"真生气", "反讽", "委婉拒绝"}
POSITIVE_TAGS = {"调侃", "撒娇假生气"}


def polarity(tags) -> str:
    t = set(tags) & (NEGATIVE_TAGS | POSITIVE_TAGS)
    if t & NEGATIVE_TAGS and not t & POSITIVE_TAGS:
        return "neg"
    if t & POSITIVE_TAGS and not t & NEGATIVE_TAGS:
        return "pos"
    return "mixed" if t else "neutral"


def prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return p, r, f


def _macro(rows: list[dict], key: str) -> tuple[float, dict[str, dict]]:
    """Macro-F1 over multi-label tags read from `key` ("raw_tags" = model, "tags" = shipped)."""
    per_tag: dict[str, dict[str, int]] = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0})
    for r in rows:
        gold, pred = set(r["gold_tags"]) - {NONE_TAG}, set(r[key])
        for t in gold & pred:
            per_tag[t]["tp"] += 1
        for t in pred - gold:
            per_tag[t]["fp"] += 1
        for t in gold - pred:
            per_tag[t]["fn"] += 1
    f1s, detail = [], {}
    for tag in PRAGMATIC_TAGS:
        c = per_tag.get(tag)
        if not c:
            continue
        p_, rec, f = prf(c["tp"], c["fp"], c["fn"])
        detail[tag] = {"precision": round(p_, 3), "recall": round(rec, 3), "f1": round(f, 3), "support": c["tp"] + c["fn"]}
        f1s.append(f)
    return (round(statistics.fmean(f1s), 3) if f1s else 0.0), detail


def score(rows: list[dict]) -> dict:
    usable = [r for r in rows if not r.get("error")]
    n = len(usable) or 1
    rows = usable or [{"gold_tags": [], "raw_tags": [], "tags": [], "ambiguous": True, "gated": False, "json_ok": True, "confidence": None}]
    macro_raw, per_tag_raw = _macro(rows, "raw_tags")
    macro_ship, per_tag_ship = _macro(rows, "tags")
    # Gold `none` has nothing to hit; those rows are scored on the none-false-positive axis instead.
    hit_rows = [r for r in rows if set(r["gold_tags"]) - {NONE_TAG}]
    superset = sum(1 for r in hit_rows if set(r["gold_tags"]) - {NONE_TAG} <= set(r["tags"]))
    superset_raw = sum(1 for r in hit_rows if set(r["gold_tags"]) - {NONE_TAG} <= set(r["raw_tags"]))
    abstains = sum(1 for r in rows if r["ambiguous"])
    gated = sum(1 for r in rows if r["gated"])
    json_bad = sum(1 for r in rows if not r["json_ok"])
    failures = Counter(r.get("error", "").split(":")[0] for r in rows if r.get("error"))
    # "the model was confident and wrong" is judged on its own reading, before our gate
    high_conf = [
        r for r in rows
        if (r["confidence"] or 0) >= 0.7
        and not (set(r["gold_tags"]) - {NONE_TAG} and set(r["gold_tags"]) - {NONE_TAG} <= set(r["raw_tags"]))
        and not (r["gold_tags"] == [NONE_TAG] and not set(r["raw_tags"]) & NEGATIVE_TAGS)
    ]
    confs = [r["confidence"] for r in rows if r["confidence"] is not None]
    # Polarity error: gold clearly negative and the model (raw) clearly positive, or the reverse.
    polar_pairs = [(polarity(r["gold_tags"]), polarity(r["raw_tags"])) for r in rows]
    polar_judged = [(g, p_) for g, p_ in polar_pairs if g in ("neg", "pos") and p_ in ("neg", "pos")]
    polar_err = sum(1 for g, p_ in polar_judged if g != p_)
    # none false positive: the fixed vocabulary has no class here, yet the shipped result asserts a negative one.
    none_rows = [r for r in rows if r["gold_tags"] == [NONE_TAG]]
    none_fp = sum(1 for r in none_rows if set(r["tags"]) & NEGATIVE_TAGS)
    strata = Counter(r.get("stratum", "synthetic") for r in rows)
    cons = [r["consistency"] for r in rows if r.get("consistency") is not None]
    return {
        "mean_consistency": round(sum(cons) / len(cons), 3) if cons else None,
        "n": len(rows),
        "strata": dict(strata),
        "polarity_judged": len(polar_judged),
        "polarity_error_rate": round(polar_err / len(polar_judged), 3) if polar_judged else None,
        "none_cases": len(none_rows),
        "none_false_positive_rate": round(none_fp / len(none_rows), 3) if none_rows else None,
        "failed_calls": sum(1 for r in rows if r.get("error")),
        "macro_f1_raw": macro_raw,
        "macro_f1_shipped": macro_ship,
        "superset_hit_rate_raw": round(superset_raw / len(hit_rows), 3) if hit_rows else 0.0,
        "superset_hit_rate": round(superset / len(hit_rows), 3) if hit_rows else 0.0,
        "abstain_rate": round(abstains / n, 3),
        "negative_gate_fired": gated,
        "gate_rate": round(gated / n, 3),
        "json_failures": json_bad,
        "failure_reasons": dict(failures),
        "mean_confidence": round(statistics.fmean(confs), 3) if confs else None,
        "high_conf_error_rate": round(len(high_conf) / n, 3),
        "high_conf_errors": [
            (r["id"], r["utterance"], sorted(r["gold_tags"]), sorted(r["raw_tags"]), r["confidence"]) for r in high_conf
        ],
        "per_tag_raw": per_tag_raw,
        "per_tag_shipped": per_tag_ship,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("key_file", nargs="?", default=None, help="API key file; omit it when using --fake")
    ap.add_argument("--preset", default="custom")
    ap.add_argument("--model", default="DeepSeek V4.1 Flash")
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--conditions", default="context,no_refs,single")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--offset", type=int, default=0, help="skip the first N scenarios (e.g. --offset 60 --limit 60 measures batch B, which the reference library does not cover)")
    ap.add_argument("--sleep", type=float, default=0.0, help="seconds between calls; use it when the endpoint rate-limits a long run")
    ap.add_argument("--patterns", action="store_true", help="also inject the pragmatic-pattern layer (docs/EVAL.md §12); off by default so the current behaviour stays the baseline")
    ap.add_argument("--cues", action="store_true", help="inject the structural-cues block (v0.5 §3A: 是否在回答提问、谁开的话题、前文笑标记…); off by default")
    ap.add_argument("--samples", type=int, default=1, help="v0.5 §3B: k sampled draws per case (temperature 0.7), merged by majority; costs k× calls")
    ap.add_argument("--timeout", type=float, default=180.0, help="per-request timeout; reasoning models need a large one")
    ap.add_argument("--repeat", type=int, default=1, help="re-run every condition N times and report mean/min/max (the noise floor is ~0.02 macro-F1)")
    ap.add_argument("--labels", default=None, help="frozen labels from scripts/adjudicate.py freeze; overrides the built-in gold")
    ap.add_argument("--cases", default=None, help="external cases json from scripts/label_map.py (real material, AI weak gold); replaces the built-in PROBES")
    ap.add_argument("--stratum", default="gold", help="with --cases: gold | weak | all")
    ap.add_argument("--sample", type=int, default=0, help="with --cases: stratified random sample of N cases (seeded), proportional per segment")
    ap.add_argument("--seed", type=int, default=20260918)
    ap.add_argument("--enrich", type=int, default=0, help="with --cases --sample: guarantee at least N cases per fixed tag (真生气/反讽/…) where available, then fill the rest proportionally; a proportional sample of real chat leaves the risk classes with a support of 1–3")
    ap.add_argument("--progress", default=None, help="append one line per completed case here so a long run is observable while it runs")
    ap.add_argument("--out", default=None)
    ap.add_argument("--fake", action="store_true", help="run against the offline fake provider: no key, no network, no cost. Required dry-run before any paid run (docs/EVAL.md §13)")
    args = ap.parse_args()

    if args.fake:
        # A paid run is always preceded by `--limit 1` and `--limit 1 --repeat 2` on this path, so a
        # broken harness is found here instead of after the bill (docs/EVAL.md §13).
        from rapport.llm.fake import FakeProvider

        provider = FakeProvider()
        print("provider=fake（离线假供应商：不出网、不产生费用；数字不可用于任何效果结论）")
    else:
        from _local import is_local_preset, make_local_provider

        if is_local_preset(args.preset):
            # v0.8 local profile: no key, no network; the model runs in Ollama on this machine.
            provider = make_local_provider(args.preset, args.model, args.base_url, task="intent", timeout=args.timeout)
            ok, msg = provider.ping()
            print(f"provider=local ping ok={ok} msg={msg}")
            if not ok:
                return 2
        else:
            if not args.key_file:
                ap.error("需要 key_file；只想验证流程请用 --fake")
            key, file_url = load_key(Path(args.key_file))
            from rapport.llm.openai_compat import OpenAICompatProvider

            provider = OpenAICompatProvider(args.preset, key, args.model, args.base_url or file_url, timeout=args.timeout)
    library = load_library()
    # Carried into every report header: a fake-provider run must never be quotable as an effect result.
    model_label = provider.info.model + ("（假供应商 · 仅供流程验证）" if args.fake else "")

    # defined before the header prints it (an earlier insertion landed below the print)
    patterns = [p.prompt_dict() for p in load_patterns()] if args.patterns else []
    if args.cases:
        if args.out and not args.fake:
            from _private import ensure_private_out

            ensure_private_out(args.out, "intent-real.md")  # refuses a path inside the repo
        _CASE_SOURCE["external"] = True
        probes, case_note = load_cases(Path(args.cases), args.stratum, args.sample, args.seed, enrich=args.enrich)
        label_stats = {"adjudicated": 0, "author_carried": 0, "missing": 0}
        print(case_note)
    else:
        probes, label_stats = apply_frozen_labels(PROBES, args.labels)
    if args.labels and not args.cases:
        print(f"标签来源：裁定 {label_stats['adjudicated']} 条 · 沿用作者 {label_stats['author_carried']} 条 · 未覆盖 {label_stats['missing']} 条")
    print(f"语用模式层：{'开启（' + str(len(patterns)) + ' 条）' if patterns else '关闭'}  结构线索：{'开启' if args.cues else '关闭'}")
    probes = probes[args.offset :]
    probes = probes[: args.limit] if args.limit else probes
    # 报告里要写清这是全量还是切片，否则两份报告的口径对不上（docs/EVAL.md §14）
    slice_note = ""
    if args.offset or args.limit:
        slice_note = f"--offset {args.offset}" + (f" --limit {args.limit}" if args.limit else "")
    if args.cues:
        slice_note = (slice_note + " " if slice_note else "") + "--cues"
    if args.samples >= 2:
        slice_note = (slice_note + " " if slice_note else "") + f"--samples {args.samples}"
    if args.cases:
        slice_note = (slice_note + " " if slice_note else "") + f"--cases {Path(args.cases).name} --stratum {args.stratum}" + (f" --sample {args.sample} --seed {args.seed}" if args.sample else "")
    print(f"model={model_label}  scenarios={len(probes)}  reference seeds={len(library.examples)}")
    print(f"conditions={args.conditions}\n")
    all_rows: dict[str, list[dict]] = {}
    per_rep: dict[str, list[dict]] = {}
    probe_out = Probe(args.progress)
    multi = args.repeat > 1
    for rep in range(max(1, args.repeat)):
        tag = f" rep={rep + 1}/{args.repeat}" if multi else ""
        if multi:
            print(f"\n===== 第 {rep + 1}/{args.repeat} 轮 =====", flush=True)
        for condition in args.conditions.split(","):
            rows = _run_condition(provider, probes, condition, library, probe_out, patterns, args.sleep, tag, cues=args.cues, samples=args.samples)
            per_rep.setdefault(condition, []).append(score(rows))
            all_rows[condition if not multi else f"{condition}#rep{rep + 1}"] = rows
            if args.out:
                # Write after every condition, in both modes: a long run that dies half way still
                # leaves the rounds that finished (a 459/900 MiMo run once left nothing, 2026-09-19).
                Path(args.out).write_text(
                    render_repeat_markdown(model_label, per_rep, all_rows, cases=len(probes), slice_note=slice_note)
                    if multi
                    else render_markdown(model_label, {c: score(v) for c, v in all_rows.items()}, all_rows, cases=len(probes), slice_note=slice_note),
                    encoding="utf-8",
                )

    if multi:
        _print_repeat_summary(per_rep)
        if args.out:
            # The repeat report gets the rows too, so a repeat run is as traceable as a single run.
            Path(args.out).write_text(
                render_repeat_markdown(model_label, per_rep, all_rows, cases=len(probes), slice_note=slice_note),
                encoding="utf-8",
            )
            print(f"\n报告写入 {args.out}")
        return 0

    print("\n================ 结果 ================")
    summary = {c: score(rows) for c, rows in all_rows.items()}
    for c, s in summary.items():
        print(f"\n--- {c} ---")
        print(f"  样本 {s['n']} | macro-F1 原始 {s['macro_f1_raw']} / 上线后 {s['macro_f1_shipped']}")
        print(f"  全标签命中 原始 {s['superset_hit_rate_raw']} / 上线后 {s['superset_hit_rate']} | 弃答率 {s['abstain_rate']}")
        print(f"  负面门槛触发 {s['negative_gate_fired']} ({s['gate_rate']}) | JSON 失败 {s['json_failures']} | 高置信度误判率 {s['high_conf_error_rate']} | 平均自评 {s['mean_confidence']}")
        for tag, d in s["per_tag_raw"].items():
            ship = s["per_tag_shipped"].get(tag, {})
            print(f"    {tag:<8} 原始 P {d['precision']:.2f} R {d['recall']:.2f} F1 {d['f1']:.2f} | 上线 F1 {ship.get('f1', 0):.2f} (n={d['support']})")
        if s["high_conf_errors"]:
            print(f"    高置信度误判: {s['high_conf_errors']}")

    if args.out:
        Path(args.out).write_text(
            render_markdown(model_label, summary, all_rows, cases=len(probes), slice_note=slice_note),
            encoding="utf-8",
        )
        print(f"\n报告写入 {args.out}")
    return 0




def load_cases(path: Path, stratum: str = "gold", sample: int = 0, seed: int = 20260918, enrich: int = 0) -> tuple[list, str]:
    """External cases (scripts/label_map.py) as 5-tuples: (utterance, context, tags, emotions, meta).

    `stratum` picks gold / weak / all; `sample` draws a seeded, per-segment proportional sample so a
    paid run on a 28k-message corpus can be a few hundred calls and still be reproducible.
    """
    import json as _json
    import random as _random

    data = _json.loads(path.read_text(encoding="utf-8"))
    cases = data.get("cases") or []
    if stratum != "all":
        cases = [c for c in cases if c.get("stratum") == stratum]
    total = len(cases)
    enriched = 0
    if sample and sample < total:
        rng = _random.Random(seed)
        picked: list = []
        taken: set[str] = set()
        if enrich:
            # Floor per fixed tag first (excluding 信息确认/不确定/none, which never lack support).
            for tag in PRAGMATIC_TAGS:
                if tag in ("信息确认", "不确定"):
                    continue
                pool = [c for c in cases if tag in (c.get("gold_tags") or []) and c["id"] not in taken]
                for c in rng.sample(pool, min(enrich, len(pool))):
                    picked.append(c)
                    taken.add(c["id"])
            enriched = len(picked)
        rest = [c for c in cases if c["id"] not in taken]
        remaining = max(0, sample - len(picked))
        by_seg: dict[str, list] = {}
        for c in rest:
            by_seg.setdefault(c["segment"], []).append(c)
        for seg in sorted(by_seg):
            k = max(1, round(remaining * len(by_seg[seg]) / max(1, len(rest))))
            picked += rng.sample(by_seg[seg], min(k, len(by_seg[seg])))
        cases = picked[:sample] if len(picked) > sample else picked
    probes = [
        (
            c["utterance"],
            list(c.get("context") or []),
            list(c.get("gold_tags") or []),
            list(c.get("gold_emotions") or []),
            {"id": c["id"], "stratum": c.get("stratum"), "boundary": c.get("boundary"), "segment": c.get("segment")},
        )
        for c in cases
    ]
    note = (
        f"用例来源：{path.name}（{data.get('source', '?')}；映射表 {data.get('label_map', {}).get('version')} "
        f"{data.get('label_map', {}).get('digest')}）· 分层 {stratum}：{total} 条"
        + (f" · 抽样 {len(probes)} 条（seed {seed}，按段比例" + (f"，其中 {enriched} 条为每类 ≥{enrich} 的富集" if enrich else "") + "）" if sample else "")
    )
    return probes, note


def apply_frozen_labels(probes: list, path: str | None) -> tuple[list, dict]:
    """Replace built-in gold with adjudicated labels where they exist (docs/EVAL.md §11).

    Scenario ids are positional (s001..), which is why the probe list order is frozen: appending
    scenarios is fine, reordering would silently re-point every adjudicated label.
    """
    if not path:
        return probes, {"adjudicated": 0, "author_carried": 0, "missing": 0}
    import json as _json
    from pathlib import Path as _Path

    data = _json.loads(_Path(path).read_text(encoding="utf-8"))
    labels = data.get("labels", {})
    stats = {"adjudicated": 0, "author_carried": 0, "missing": 0}
    out: list = []
    for i, (surface, context, tags, emotions) in enumerate(probes):
        lab = labels.get(f"s{i + 1:03d}")
        if lab is None:
            stats["missing"] += 1
            out.append((surface, context, tags, emotions))
        else:
            stats["adjudicated" if lab.get("source") == "adjudicated" else "author_carried"] += 1
            out.append((surface, context, lab.get("pragmatic_tags", tags), lab.get("emotions", emotions)))
    return out, stats



# The progress probe moved to `_probe.py` so all four evaluation scripts share one definition
# instead of four copies drifting apart; this alias keeps the existing call sites working.
Probe = _Probe


def _run_condition(
    provider,
    probes,
    condition: str,
    library,
    probe_out: Probe | None = None,
    patterns: list | None = None,
    sleep_s: float = 0.0,
    tag: str = "",
    cues: bool = False,
    samples: int = 1,
) -> list[dict]:
    """Run one condition end to end. The only producer of result rows in this script.

    The single-condition path and the repeat path both call this, so a run reports the same lines
    and the same DONE marker whichever mode it was started in (docs/EVAL.md §14).
    """
    rows: list[dict] = []
    for i, probe in enumerate(probes, 1):
        if sleep_s and i > 1:
            time.sleep(sleep_s)
        try:
            row = evaluate_case(provider, i, probe, condition, library, patterns, cues=cues, samples=samples)
        except LLMError as e:
            row = _error_row(i, probe, condition, f"{e.code}: {e.msg}")
        except Exception as e:  # noqa: BLE001 - a flaky endpoint must not abort the batch
            row = _error_row(i, probe, condition, f"{type(e).__name__}: {e}")
        rows.append(row)
        mark = "OK " if set(row["gold_tags"]) <= set(row["tags"]) else "MISS"
        line = f"[{condition}]{tag} {i:>3}/{len(probes)} {probe[0]:<10} {mark} gold={probe[2]} pred={row['tags']}"
        (probe_out.line(line) if probe_out else print(line, flush=True))
    errors = sum(1 for r in rows if r.get("error"))
    if errors:
        # Failures stay in the results; say the number out loud so the report's n is never read
        # as "the model got all of these".
        line = f"[{condition}]{tag} 失败 {errors}/{len(rows)} 条（已记录，不静默丢弃）"
        (probe_out.line(line) if probe_out else print(line, flush=True))
    done = f"DONE condition={condition}{tag} cases={len(rows)} failed={errors}"
    (probe_out.done(done) if probe_out else print(done, flush=True))
    return rows


def _print_repeat_summary(per_rep: dict[str, list[dict]]) -> None:
    print("\n================ 多轮汇总（均值 / 最小 / 最大）================")
    for condition, scores in per_rep.items():
        for key in REPEAT_KEYS:
            vals = [s_.get(key) for s_ in scores if s_.get(key) is not None]
            if not vals:
                continue
            mean = sum(vals) / len(vals)
            print(f"{condition:<10} {key:<26} 均值 {mean:.3f}  区间 [{min(vals):.3f}, {max(vals):.3f}]  n={len(vals)}")


REPEAT_KEYS = (
    "macro_f1_raw",
    "macro_f1_shipped",
    "superset_hit_rate_raw",
    "high_conf_error_rate",
    "abstain_rate",
    "gate_rate",
    "polarity_error_rate",
    "none_false_positive_rate",
    "mean_consistency",
)


def _case_table(L: list[str], all_rows: dict[str, list[dict]]) -> None:
    """Append the per-case tables — the part a reader uses to check a specific judgement.

    Shared by the single-run and repeat reports so a repeat run produces the same traceable output
    as a single run instead of only a summary table (docs/EVAL.md §14).
    """
    for c, rows in all_rows.items():
        errors = sum(1 for r in rows if r.get("error"))
        L += [
            "",
            f"## 逐条（{c}）",
            "",
            f"本条件 {len(rows)} 条，其中调用失败 {errors} 条（失败行保留在表内，不计入指标）。" if errors else f"本条件 {len(rows)} 条，无调用失败。",
            "",
            "| # | 这句话 | 金标签 | 模型判断 | 上线后 | 弃答 | 门槛 | 自评 | 错误 |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for r in rows:
            L.append(
                f"| {r['id']} | {r['utterance']} | {'、'.join(r['gold_tags'])} | {'、'.join(r['raw_tags']) or '—'} | "
                f"{'、'.join(r['tags']) or '—'} | {'是' if r['ambiguous'] else ''} | {'是' if r['gated'] else ''} | {r['confidence']} | {r.get('error', '')[:60]} |"
            )


def _case_detail_table(L: list[str], all_rows: dict[str, list[dict]]) -> None:
    """Append each case's demoted candidates, per-tag evidence and counter-evidence.

    This is what makes a wrong call correctable: the reader can see why the model said it, and
    what it weighed against.
    """
    detailed = {c: [r for r in rows if r.get("candidates") or r.get("counter_evidence_ids")] for c, rows in all_rows.items()}
    for c, rows in detailed.items():
        if not rows:
            continue
        L += ["", f"## 判断依据与候选解释（{c}）", ""]
        for r in rows:
            parts = []
            for cand in r.get("candidates") or []:
                basis = "、".join(cand.get("basis_ids") or []) or "无引用"
                parts.append(f"候选「{cand['reading']}」（{cand['likelihood']}，引用 {basis}）")
            if r.get("counter_evidence_ids"):
                parts.append("反向证据 " + "、".join(r["counter_evidence_ids"]))
            if r.get("suppressed_reason"):
                parts.append(f"门槛：{r['suppressed_reason']}")
            if r.get("evidence_strength"):
                parts.append(f"自评证据强度 {r['evidence_strength']}")
            L.append(f"- {r['id']}「{r['utterance']}」：" + "；".join(parts))
        L.append("")


_CASE_SOURCE = {"external": False}  # flipped by main() when --cases is used


def _slice_line(cases: int) -> str:
    # The run's actual size, never the pool size: two batch-B reports once said 「场景数 120」
    # while 60 cases had run, so anyone comparing them read the wrong thing (docs/EVAL.md §14).
    if _CASE_SOURCE["external"]:
        return f"- 场景数：{cases}（本批实际运行条数；**真实材料，AI 弱金标签，见 docs/LABEL-MAP.md**）"
    return f"- 场景数：{cases}（本批实际运行条数，全部为原创合成对话；题池共 {len(PROBES)} 条）"


def _scope_line() -> str:
    if _CASE_SOURCE["external"]:
        return "- 口径：**真实材料上的工程指标；金标签是外部 AI 标注经映射得到的弱金，不是人工金标准。** `none` = 固定词表无此类，不计入 macro-F1，只算 none 误报率。"
    return "- 口径：**这是合成对照集上的工程指标，不是产品准确率，也不代表对真实关系的判断能力。** v0.8 起产品无负向门槛，「上线后」= 「原始」，该列只为与历史报告对照而保留。"


def render_repeat_markdown(
    model: str,
    per_rep: dict[str, list[dict]],
    all_rows: dict[str, list[dict]] | None = None,
    cases: int = 0,
    slice_note: str = "",
) -> str:
    import datetime as _dt

    reps = len(next(iter(per_rep.values())))
    L = [
        "# 语义情绪评测结果（多轮）",
        "",
        f"- 模型：`{model}`",
        f"- 日期：{_dt.date.today().isoformat()}",
        f"- 每条件重复 {reps} 轮，用于把信号从噪声里分出来（噪声底约 ±0.02 macro-F1）",
        # Same discipline as the single-run report: the run's actual size and slice, never the pool.
        _slice_line(cases) if cases else f"- 场景数：未记录（题池共 {len(PROBES)} 条）",
        f"- 切片：{slice_note or '全量（未使用 --offset/--limit）'}",
        _scope_line() + " 两轮之间的差值小于噪声底时不构成改进证据。",
        "",
        "| 条件 | 指标 | 均值 | 最小 | 最大 | 轮数 |",
        "|---|---|---:|---:|---:|---|",
    ]
    for condition, scores in per_rep.items():
        for key in REPEAT_KEYS:
            vals = [s_.get(key) for s_ in scores if s_.get(key) is not None]
            if not vals:
                continue
            L.append(f"| {condition} | {key} | {sum(vals) / len(vals):.3f} | {min(vals):.3f} | {max(vals):.3f} | n={len(vals)} |")
        n_pol = [s_.get("polarity_judged", 0) for s_ in scores]
        n_none = [s_.get("none_cases", 0) for s_ in scores]
        L.append(f"| {condition} | （极性可判 n / none n） | {sum(n_pol) / len(n_pol):.0f} / {sum(n_none) / len(n_none):.0f} | | | |")
    # Per-tag table averaged over rounds: on real material the risk classes may have a support of
    # one or two, and that has to be visible next to the F1 or the number reads as a capability.
    L += ["", "## 分标签（各轮均值；支持数为该标签在金标签中的出现次数）", "", "| 标签 | 原始 F1 均值 | 上线后 F1 均值 | 支持数 |", "|---|---:|---:|---:|"]
    for condition, scores in per_rep.items():
        L.append(f"| （{condition}） | | | |")
        tags = sorted({t for s_ in scores for t in s_.get("per_tag_raw", {})}, key=lambda t: PRAGMATIC_TAGS.index(t) if t in PRAGMATIC_TAGS else 99)
        for t in tags:
            raw = [s_["per_tag_raw"][t]["f1"] for s_ in scores if t in s_.get("per_tag_raw", {})]
            ship = [s_.get("per_tag_shipped", {}).get(t, {}).get("f1", 0.0) for s_ in scores]
            sup = [s_["per_tag_raw"][t]["support"] for s_ in scores if t in s_.get("per_tag_raw", {})]
            L.append(f"| {t} | {sum(raw) / len(raw):.3f} | {sum(ship) / len(ship):.3f} | {max(sup)} |")
    if all_rows:
        _case_table(L, all_rows)
        _case_detail_table(L, all_rows)
    return "\n".join(L) + "\n"


def render_markdown(model: str, summary: dict, all_rows: dict, cases: int, slice_note: str = "") -> str:
    import datetime as _dt

    L = [
        "# 语义情绪评测结果（Phase 1）",
        "",
        f"- 模型：`{model}`",
        f"- 日期：{_dt.date.today().isoformat()}",
        # 写实际跑的条数，不写题池大小：两份 B 批报告曾因写死 len(PROBES) 而标着「场景数 120」
        # 实际只跑了 60 条，读报告的人据此对比会错（docs/EVAL.md §14）。
        _slice_line(cases),
        f"- 切片：{slice_note or '全量（未使用 --offset/--limit）'}",
        _scope_line(),
        "- 「原始」= 模型自己的判断；「上线后」= 经过负向门槛与证据校验后用户实际会看到的结果。",
        "",
        "## 汇总",
        "",
        "| 条件 | macro-F1(原始) | macro-F1(上线后) | 全标签命中(原始) | 弃答率 | 负面门槛触发 | JSON 失败 | 高置信度误判率 | 极性错误率 | none 误报率 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for c, s_ in summary.items():
        pe = s_.get("polarity_error_rate")
        nf = s_.get("none_false_positive_rate")
        L.append(
            f"| {c} | {s_['macro_f1_raw']} | {s_['macro_f1_shipped']} | {s_['superset_hit_rate_raw']} | "
            f"{s_['abstain_rate']} | {s_['negative_gate_fired']} ({s_['gate_rate']}) | {s_['json_failures']} | {s_['high_conf_error_rate']} | "
            f"{'—' if pe is None else pe} (n={s_.get('polarity_judged', 0)}) | {'—' if nf is None else nf} (n={s_.get('none_cases', 0)}) |"
        )
    # These two sections used to read `summary["context"]` unconditionally, so a run of
    # `--conditions single` printed an empty per-tag table with no hint that it was empty by
    # construction. Fall back to whatever condition actually ran, and say which one it was.
    focus = "context" if "context" in summary else next(iter(summary), None)
    L += ["", "## 分标签（原始模型能力 vs 上线后）", ""]
    if focus:
        L += [f"（按 `{focus}` 条件展开）", ""]
    L += ["| 标签 | 原始 P | 原始 R | 原始 F1 | 上线后 F1 | 支持数 |", "|---|---:|---:|---:|---:|---:|"]
    if focus:
        for tag, d in summary[focus].get("per_tag_raw", {}).items():
            ship = summary[focus].get("per_tag_shipped", {}).get(tag, {})
            L.append(f"| {tag} | {d['precision']} | {d['recall']} | {d['f1']} | {ship.get('f1', 0)} | {d['support']} |")
        if summary[focus].get("high_conf_errors"):
            L += ["", f"## 高置信度误判（原始判断，{focus} 条件）", ""]
            for e in summary[focus]["high_conf_errors"]:
                L.append(f"- {e[0]}「{e[1]}」金标签 {e[2]} → 模型判断 {e[3]}（自评 {e[4]}）")
    _case_table(L, all_rows)
    _case_detail_table(L, all_rows)
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
