"""Stage 5: synthesis input assembly + call. Stage 6 validation lives in validate.py."""

from __future__ import annotations

import json
from collections import defaultdict

from rapport.features.compute import Features
from rapport.llm.extract import SYNTH_SYSTEM, Signal, WindowResult
from rapport.llm.limits import SYNTH_MAX_TOKENS
from rapport.llm.limits import budget as _budget
from rapport.llm.provider import CallUsage, LLMError, LLMProvider
from rapport.schemas.profile import Goal, Profile

GOAL_ZH = {
    Goal.understand: "理解对方当前的互动方式",
    Goal.invite: "判断是否/如何提出一次邀约",
    Goal.clarify: "澄清一个误会或疑问",
    Goal.repair: "复盘一次冲突并修复",
    Goal.decide: "决定是否继续投入",
}
MAX_SIGNALS = 80


def _feature_summary(f: Features) -> dict:
    def m(x):
        return None if x.value is None else round(x.value, 3)

    return {
        "消息数": {"A": f.me.msgs, "B": f.other.msgs},
        "文本消息数": {"A": f.me.texts, "B": f.other.texts},
        "会话段数": f.n_sessions,
        "有可靠时间": f.has_reliable_time,
        "B中位回复秒": m(f.other.median_reply_s),
        "A中位回复秒": m(f.me.median_reply_s),
        "B五分钟内回复率": m(f.other.reply_within_5min_rate),
        "B发起会话比例": m(f.other.initiation_rate),
        "A发起会话比例": m(f.me.initiation_rate),
        "B提问率": m(f.other.question_rate),
        "B平均长度": m(f.other.avg_len),
        "A平均长度": m(f.me.avg_len),
        "B短回复率": m(f.other.short_reply_rate),
        "B双发率": m(f.other.double_text_rate),
        "B关心词率": m(f.other.care_rate),
        "B未来计划词率": m(f.other.future_rate),
        "B深夜消息率": m(f.other.late_night_rate),
        "规则层边界命中": {k: len(v) for k, v in f.boundary_hits.items() if v},
    }


def sample_signals(results: list[WindowResult], cap: int = MAX_SIGNALS) -> list[Signal]:
    by_kind: dict[str, list[Signal]] = defaultdict(list)
    for r in results:
        for s in r.signals:
            by_kind[s.kind].append(s)
    out: list[Signal] = []
    # round-robin across kinds so rare kinds (boundary, decline) are never dropped
    kinds = sorted(by_kind, key=lambda k: (k not in ("boundary", "decline", "conflict"), k))
    idx = {k: 0 for k in kinds}
    while len(out) < cap and any(idx[k] < len(by_kind[k]) for k in kinds):
        for k in kinds:
            if idx[k] < len(by_kind[k]) and len(out) < cap:
                out.append(by_kind[k][idx[k]])
                idx[k] += 1
    return out


def build_synth_user(f: Features, results: list[WindowResult], profile: Profile, last_actions: list[dict] | None = None) -> str:
    signals = sample_signals(results)
    payload = {
        "用户目标": GOAL_ZH[profile.goal],
        "用户补充": profile.goal_text[:500],
        # v0.5 §2: what the user did after the previous report and what the other person did.
        "上次行动回顾": [
            {"目标": a.get("goal", ""), "对方反应": OBSERVED_ZH.get(a.get("observed", ""), a.get("observed", "")), "备注": (a.get("note") or "")[:120]}
            for a in (last_actions or [])
        ][:6],
        "行为统计": _feature_summary(f),
        "分段摘要": [{"窗口": r.index, "warmth": r.warmth, "future": r.future, "tone": r.tone} for r in results],
        "互动信号": [{"msg_ids": s.msg_ids, "speaker": s.speaker, "kind": s.kind, "note": s.note} for s in signals],
        "值得注意": [n for r in results for n in r.notable][:15],
        "可引用的消息id": sorted({i for r in results for i in r.msg_ids}),
    }
    return json.dumps(payload, ensure_ascii=False)



OBSERVED_ZH = {"accepted": "接受了", "alt_time": "改了时间", "declined": "拒绝了", "no_reply": "没有回应", "other": "其它"}


def run_synthesis(provider: LLMProvider, user: str, repair_error: str | None = None) -> tuple[dict, CallUsage]:
    u = user
    if repair_error:
        u = user + f"\n\n上一次输出未通过校验：{repair_error}\n请严格按照结构重新输出完整 JSON。"
    budget = _budget(SYNTH_MAX_TOKENS, bool(getattr(provider, "reasoning", False)))
    try:
        return provider.complete_json(SYNTH_SYSTEM, u, max_tokens=budget)
    except LLMError as e:
        if e.code != "truncated":
            raise
        return provider.complete_json(SYNTH_SYSTEM, u, max_tokens=budget * 2)
