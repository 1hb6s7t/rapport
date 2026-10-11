"""Nudge text: rule templates bound to real evidence, never free-floating pep talk.

Each kind reads only what the latest report and the diary already contain. A template that
cannot cite an evidence id produces nothing — the drive layer then stays silent this hour rather
than sending a generic "想 TA 了吗". LLM-drafted nudges are a separate, opt-in path that goes
through the outbound gate; nothing in this module calls a model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class NudgeDraft:
    kind: str
    text: str
    evidence_ids: list[str] = field(default_factory=list)
    why: str = ""


def _other_evidence(body: dict, limit: int = 6) -> list[tuple[str, dict]]:
    ev = body.get("evidence") or {}
    rows = [(k, v) for k, v in ev.items() if isinstance(v, dict) and v.get("sender") == "other" and (v.get("text") or "").strip()]
    rows.sort(key=lambda kv: kv[1].get("ts") or "", reverse=True)
    return rows[:limit]


def _short(s: str, n: int = 40) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def recall(body: dict, other_name: str) -> NudgeDraft | None:
    """Bring back something the other person actually said, from the report's own evidence."""
    rows = _other_evidence(body)
    if not rows:
        return None
    # Prefer a line an observation cites, so the nudge points at something the report thought mattered.
    cited = [o for o in body.get("observations") or [] if o.get("evidence_ids")]
    pick_id, pick = None, None
    for o in cited:
        for eid in o["evidence_ids"]:
            for k, v in rows:
                if k == eid:
                    pick_id, pick = k, v
                    break
            if pick:
                break
        if pick:
            break
    if not pick:
        pick_id, pick = rows[0]
    when = ""
    if pick.get("ts"):
        try:
            when = datetime.fromisoformat(pick["ts"]).strftime("%m 月 %d 日")
        except ValueError:
            when = ""
    text = f"{other_name}{('在 ' + when) if when else '之前'}说过「{_short(pick['text'])}」。想起来的话，也许可以接着这个聊。"
    return NudgeDraft("recall", text, [pick_id], "引用报告证据里对方的原话")


def timing(body: dict, other_name: str) -> NudgeDraft | None:
    """Their observed reply rhythm, only if the feature layer measured it."""
    f = (body.get("features") or {}).get("other") or {}
    med = (f.get("median_reply_s") or {}).get("value")
    n = (f.get("median_reply_s") or {}).get("n") or 0
    late = (f.get("late_night_rate") or {}).get("value")
    if med is None or n < 5:
        return None
    mins = int(med // 60)
    when = "夜里也常回复" if (late or 0) >= 0.3 else "深夜回得少"
    text = f"按过去的记录，{other_name}通常在 {mins} 分钟内回复（{n} 次样本），{when}。要发消息的话，这是可参考的节奏，不是保证。"
    ids = [k for k, _ in _other_evidence(body, 2)]
    return NudgeDraft("timing", text, ids, "来自特征层的回复延迟中位数")


def draft(strict_drafts: list[dict], other_name: str) -> NudgeDraft | None:
    """One strict-mode draft (approved diary clause only); nothing is invented."""
    if not strict_drafts:
        return None
    d = strict_drafts[0]
    refs = [r.get("id") for r in d.get("memory_refs") or [] if r.get("id")]
    text = f"有一条你批准过的话术可以直接用：「{_short(d.get('text', ''), 60)}」（严格模式，逐字来自你的日记）。"
    return NudgeDraft("draft", text, refs, "严格模式话术，只用已批准短句")


def checkin(prompt: dict | None) -> NudgeDraft | None:
    if not prompt:
        return None
    return NudgeDraft("checkin", prompt.get("question", "后来怎么样了？"), [prompt.get("report_id", "")], "两周回访，只存本机")


def self_care(other_name: str) -> NudgeDraft:
    # Deliberately no numbers, no "they're cooling off": the only nudge allowed on falling momentum.
    return NudgeDraft("self_care", "最近你自己过得怎么样？关系里的事可以先放一放，先照顾好自己。", [], "下降趋势下只关心你自己，不报数字")


def build(kind: str, *, body: dict | None, other_name: str, strict_drafts: list[dict] | None, outcome_prompt: dict | None) -> NudgeDraft | None:
    body = body or {}
    if kind == "recall":
        return recall(body, other_name)
    if kind == "timing":
        return timing(body, other_name)
    if kind == "draft":
        return draft(strict_drafts or [], other_name)
    if kind == "checkin":
        return checkin(outcome_prompt)
    if kind == "self_care":
        return self_care(other_name)
    return None
