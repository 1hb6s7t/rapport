"""Probe: does the model disambiguate the SAME surface utterance across different contexts?

This measures the single biggest risk in the v2 requirements ("我生气了" real vs 撒娇 vs 玩笑,
反话, 敷衍). It compares two conditions:

  A. context given  — the previous turns are included, as M03 requires
  B. no context     — the utterance alone, which is what a naive implementation would do

Gold labels were authored by hand for synthetic, invented conversations. No real chats.

Run:
    uv run python ../scripts/emotion_probe.py "C:/path/API KEY.txt" --model "DeepSeek V4.1 Flash"
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from real_byok_check import load_key  # noqa: E402

from rapport.llm.openai_compat import OpenAICompatProvider  # noqa: E402
from rapport.llm.provider import LLMError  # noqa: E402

LABELS = ["真实负面", "撒娇", "玩笑调侃", "反话", "敷衍冷淡", "真诚积极", "无法判断"]

SYSTEM = f"""TASK: intent

你在帮一个关系复盘工具判断聊天里一句话的真实意图。必须结合给出的上下文。
可选标签（只能选一个）：{"、".join(LABELS)}
判定要点：
- 同一句话在不同上下文下含义完全不同，必须以上下文为准，不要只看字面。
- 反话：字面与真实态度相反。
- 撒娇：以佯装不满/嗔怪来索取关注，通常伴随亲昵语境。
- 玩笑调侃：双方在轻松语境下互动，没有真实负面情绪。
- 敷衍冷淡：用简短回应拉开距离、终止话题。
- 若上下文确实不足以判断，选「无法判断」，不要硬猜。
只输出 JSON：{{"label": "上面之一", "confidence": 0.0-1.0, "evidence": "依据上下文中的哪句话，≤40字", "alternative": "另一种可能的解释，≤20字"}}"""


@dataclass
class Probe:
    id: str
    utterance: str
    context: list[str]
    gold: str
    note: str = ""


PROBES: list[Probe] = [
    Probe("p01", "我生气了", ["A: 不好意思，今天临时有事，改天吧", "B: 嗯", "A: 真的抱歉，下次一定", "B: 上次你也是这么说的"], "真实负面", "第三次爽约"),
    Probe("p02", "我生气了", ["A: 你今天怎么这么笨哈哈哈", "B: 你自己也好不到哪去🤣", "A: 那我们俩是笨蛋组合了"], "玩笑调侃", "互相调侃"),
    Probe("p03", "你真讨厌", ["A: 我给你点了奶茶，猜你加班", "B: 你真讨厌", "A: 不喜欢吗？", "B: 喜欢的，谢谢你还记得"], "撒娇", "被照顾后的嗔怪"),
    Probe("p04", "你真讨厌", ["A: 我已经把你上次说的事告诉别人了", "B: 你真讨厌", "A: 开个玩笑而已", "B: 这种事不好笑"], "真实负面", "隐私被当玩笑"),
    Probe("p05", "随便你", ["A: 晚上吃火锅还是日料？", "B: 你选吧我都可以", "A: 那你选", "B: 随便你"], "真诚积极", "真的不介意"),
    Probe("p06", "随便你", ["A: 我周末想去爬山，你陪我", "B: 我那天要加班", "A: 你就是不想陪我", "B: 随便你"], "真实负面", "被指责后的负气"),
    Probe("p07", "呵呵", ["A: 你不是说你一定会做到吗", "B: 计划赶不上变化", "A: 每次都是这个理由", "B: 呵呵"], "敷衍冷淡", "冷笑收场"),
    Probe("p08", "呵呵", ["A: 刚才那只猫摔了个跟头哈哈哈", "B: 呵呵", "A: 你怎么这么没同情心", "B: 我是笑它爬起来还装作没事"], "玩笑调侃", "被逗乐"),
    Probe("p09", "好吧", ["A: 周六那个展我们去吧？", "B: 好啊", "A: 那就周六下午两点", "B: 好吧"], "真诚积极", "确认安排"),
    Probe("p10", "好吧", ["A: 对不起，周末临时要出差，见面取消了", "B: 好吧", "A: 你别生气", "B: 我没生气"], "真实负面", "失望但压着"),
    Probe("p11", "你忙吧", ["A: 在吗？想和你说个事", "B: 在改方案，晚点说", "A: 那算了", "B: 你忙吧"], "真实负面", "被搁置后的负气"),
    Probe("p12", "你忙吧", ["A: 我今天要通宵赶稿", "B: 你忙吧，注意身体", "A: 谢谢理解", "B: 明天记得补觉"], "真诚积极", "体谅"),
    Probe("p13", "不用了", ["A: 要我去接你吗？外面下雨了", "B: 不用了", "A: 真的不用？", "B: 不用了，我打车就行"], "真诚积极", "礼貌拒绝"),
    Probe("p14", "不用了", ["A: 我给你带份早饭吧", "B: 不用了", "A: 上次你也是说不用", "B: 你非要问就是不用了"], "反话", "期待坚持"),
    Probe("p15", "我没事", ["A: 刚才那话我不是那个意思", "B: 我没事", "A: 那你为什么不说话", "B: 真的没事，你别问了"], "真实负面", "压着情绪"),
    Probe("p16", "我没事", ["A: 抱歉刚才踩到你脚了", "B: 我没事", "A: 疼吗", "B: 不疼，走吧"], "真诚积极", "确实没事"),
    Probe("p17", "好的呢", ["A: 帮我把那份文件发一下", "B: 好的呢", "A: 谢谢！", "B: 不客气"], "真诚积极", "配合回应"),
    Probe("p18", "好的呢", ["A: 你顺便把下周的报表也做了吧", "B: 好的呢", "A: 你最近怎么这么冷淡", "B: 没有啊"], "敷衍冷淡", "不情愿的应付"),
    Probe("p19", "就这样吧", ["A: 那我们把方案定成 A 版？", "B: 就这样吧", "A: 那就这么办", "B: 嗯"], "真诚积极", "结束讨论"),
    Probe("p20", "就这样吧", ["A: 你到底想怎么样", "B: 我不想吵了", "A: 那你说清楚", "B: 就这样吧"], "真实负面", "冲突收场"),
]


def build_user(p: Probe, with_context: bool) -> str:
    ctx = "\n".join(p.context) if with_context else "（无上下文，只有这一句）"
    return f"上下文：\n{ctx}\n\n要判断的这一句：{p.utterance}\n（A 是使用者，B 是对方）"


def run(provider, p: Probe, with_context: bool) -> tuple[str, float, str, bool, str]:
    """Return (label, confidence, evidence, json_ok, raw_snippet)."""
    try:
        data, _ = provider.complete_json(SYSTEM, build_user(p, with_context), max_tokens=1200)
    except LLMError as e:
        return f"<{e.code}>", 0.0, e.msg[:60], False, ""
    if not isinstance(data, dict) or "label" not in data:
        return "<no-label>", 0.0, json.dumps(data, ensure_ascii=False)[:80], False, json.dumps(data, ensure_ascii=False)[:120]
    label = str(data.get("label", "")).strip()
    try:
        conf = float(data.get("confidence", 0))
    except (TypeError, ValueError):
        conf = 0.0
    return label, conf, str(data.get("evidence", ""))[:50], True, ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("key_file")
    ap.add_argument("--preset", default="custom")
    ap.add_argument("--model", default="DeepSeek V4.1 Flash")
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--only", default=None, help="run a single probe id")
    args = ap.parse_args()

    key, file_url = load_key(Path(args.key_file))
    provider = OpenAICompatProvider(args.preset, key, args.model, args.base_url or file_url)
    print(f"model={provider.info.model} base_url={provider.info.base_url}\n")

    probes = [p for p in PROBES if not args.only or p.id == args.only]
    rows = []
    for p in probes:
        ctx_label, ctx_conf, ctx_ev, ctx_ok, ctx_raw = run(provider, p, True)
        nc_label, nc_conf, nc_ev, nc_ok, nc_raw = run(provider, p, False)
        rows.append(dict(p=p, ctx=(ctx_label, ctx_conf, ctx_ok, ctx_ev, ctx_raw), nc=(nc_label, nc_conf, nc_ok, nc_ev, nc_raw)))
        mark = "✓" if ctx_label == p.gold else "✗"
        print(f"{p.id} 「{p.utterance}」 gold={p.gold}")
        print(f"    有上下文: {mark} {ctx_label} (conf {ctx_conf:.2f}) {ctx_ev}")
        print(f"    无上下文: {'✓' if nc_label == p.gold else '✗'} {nc_label} (conf {nc_conf:.2f}) {nc_ev}")

    n = len(rows)
    ctx_hits = sum(1 for r in rows if r["ctx"][0] == r["p"].gold)
    nc_hits = sum(1 for r in rows if r["nc"][0] == r["p"].gold)
    nc_unk = sum(1 for r in rows if r["nc"][0] == "无法判断")
    ctx_unk = sum(1 for r in rows if r["ctx"][0] == "无法判断")
    json_bad = [r["p"].id for r in rows if not (r["ctx"][2] and r["nc"][2])]
    ctx_conf = [r["ctx"][1] for r in rows if r["ctx"][0] not in ("无法判断",)]
    wrong_high = [(r["p"].id, r["ctx"][0], r["ctx"][1]) for r in rows if r["ctx"][0] != r["p"].gold and r["ctx"][1] >= 0.7]

    print("\n================ 结果 ================")
    print(f"样本数: {n}")
    print(f"有上下文正确: {ctx_hits}/{n} = {ctx_hits / n:.0%}   （其中选“无法判断”: {ctx_unk}）")
    print(f"无上下文正确: {nc_hits}/{n} = {nc_hits / n:.0%}   （其中选“无法判断”: {nc_unk}）")
    print(f"上下文增益: {(ctx_hits - nc_hits) / n:+.0%}")
    if ctx_conf:
        print(f"有上下文时的平均置信度: {statistics.fmean(ctx_conf):.2f}")
    print(f"JSON 解析失败: {json_bad or '无'}")
    print(f"高置信度误判(>=0.70): {wrong_high or '无'}")
    print("\n按标签统计（有上下文）:")
    for lab in LABELS:
        tot = [r for r in rows if r["p"].gold == lab]
        if tot:
            ok = sum(1 for r in tot if r["ctx"][0] == lab)
            print(f"  {lab}: {ok}/{len(tot)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
