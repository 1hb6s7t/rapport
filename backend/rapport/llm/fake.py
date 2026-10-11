"""Fake provider for tests and the demo pipeline. Deterministic, offline.

It produces plausible extraction / synthesis JSON from the actual prompt content
(it echoes back real message ids so validation passes).
"""

from __future__ import annotations

import json
import re

from rapport.llm.provider import CallUsage, ProviderInfo

_ID_RE = re.compile(r"\b(m\d{6})\b")


class FakeProvider:
    def __init__(self, scripted: list[dict] | None = None, fail_times: int = 0):
        self.info = ProviderInfo("fake", "local://fake", "fake-1", "本机", "本机", "n/a")
        self.scripted = list(scripted or [])
        self.calls: list[tuple[str, str]] = []
        self.fail_times = fail_times

    def ping(self) -> tuple[bool, str]:
        return True, "OK"

    def complete_json(self, system: str, user: str, max_tokens: int = 2048, temperature: float = 0.2) -> tuple[dict, CallUsage]:
        self.calls.append((system, user))
        usage = CallUsage(prompt_tokens=len(system) // 2 + len(user) // 2, completion_tokens=300, calls=1)
        if self.fail_times > 0:
            self.fail_times -= 1
            return {"garbage": True}, usage
        if self.scripted:
            return self.scripted.pop(0), usage
        ids = _ID_RE.findall(user)
        if "TASK: extract" in system:
            return self._extract(ids), usage
        if "TASK: intent" in system:
            return self._intent(ids, user), usage
        if "TASK: reply" in system:
            return self._reply(user), usage
        return self._synthesize(ids, user), usage

    @staticmethod
    def _intent(ids: list[str], user: str) -> dict:
        """Deterministic stand-in for M03: cites the target line and one context line."""
        target = next((i for i in reversed(ids) if i), None)
        ctx = [i for i in ids if i != target][-2:]
        # mirror the boundary rule so pipeline tests exercise the gate
        boundary = "stop_contact" if "别再联系我了" in user else "none"
        return {
            "literal_affect": "字面平淡",
            "pragmatic_tags": [{"tag": "真生气", "basis_ids": ([target] if target else []) + ctx, "why": "上下文指向不满"}],
            "emotions": [{"tag": "不满", "basis_ids": ctx}],
            "boundary_status": boundary,
            "candidates": [
                {"reading": "可能是真的不满", "likelihood": "medium", "basis_ids": [target] if target else []},
                {"reading": "也可能只是累了", "likelihood": "low", "basis_ids": ctx},
            ],
            "evidence_ids": ([target] if target else []) + ctx,
            "counter_evidence_ids": [],
            "evidence_strength": "medium",
            "model_confidence_raw": 0.8,
            "ambiguous": False,
            "abstain_reason": None,
        }

    @staticmethod
    def _reply(user: str) -> dict:
        """Deterministic reply pack. Uses a memory id only when one was actually provided."""
        import re as _re

        mem = _re.search(r"id=(\S+) revision=(\d+)", user)
        used = [mem.group(1)] if mem else []
        quote = ""
        if mem:
            line = next((ln for ln in user.splitlines() if ln.startswith(f"- id={mem.group(1)}")), "")
            quote = line.split("：", 1)[-1].strip() if "：" in line else ""
            if not quote:  # the local style prompt lists memories inside its JSON body
                found = _re.search(rf"id={_re.escape(mem.group(1))} revision=\d+ kind=[^：\s]+：([^\"\n]+)", user)
                quote = found.group(1).strip() if found else ""
        return {
            "drafts": [
                {"style": "casual", "text": ("最近还行，你呢？" + (f"（{quote}）" if quote else "")), "used_memory_ids": used, "why": "低压力的日常回应", "risk_notes": []},
                {"style": "sincere", "text": "和你聊天挺开心的，想多了解你一点。", "used_memory_ids": [], "why": "表达态度但不施压", "risk_notes": ["对方若近期回复偏短，可先不追问"]},
            ],
            "overall_note": "先看对方的回应长度再决定要不要推进话题。",
        }

    @staticmethod
    def _extract(ids: list[str]) -> dict:
        b_ids = ids[1::2][:6]
        return {
            "window_summary": {"warmth": 62, "future": 48, "tone": "轻松"},
            "signals": [
                {"msg_ids": b_ids[:1], "speaker": "B", "kind": "care", "note": "关心作息"},
                {"msg_ids": b_ids[1:2], "speaker": "B", "kind": "question", "note": "主动提问"},
                {"msg_ids": b_ids[2:3], "speaker": "B", "kind": "humor", "note": "开玩笑"},
            ],
            "notable": [{"msg_ids": b_ids[:1], "what": "对方主动关心"}],
        }

    @staticmethod
    def _synthesize(ids: list[str], user: str) -> dict:
        b_ids = ids[1::2][:8] or ids[:3]
        boundary = "none"
        if "stop_contact" in user:
            boundary = "stop_contact"
        elif "explicit_decline" in user:
            boundary = "explicit_decline"
        return {
            "dims": {
                "responsiveness": {"score": 70, "evidence_ids": b_ids[:2], "note": "回复大多认真"},
                "initiative": {"score": 60, "evidence_ids": b_ids[1:3], "note": "偶尔主动开启话题"},
                "investment": {"score": 65, "evidence_ids": b_ids[2:4], "note": "会分享自己的事"},
                "warmth": {"score": 68, "evidence_ids": b_ids[:3], "note": "有关心表达"},
                "future": {"score": 55, "evidence_ids": b_ids[3:5], "note": "提过一起吃饭"},
            },
            "observations": [
                {
                    "id": "o1",
                    "claim": "对方会主动关心你的作息",
                    "evidence_ids": b_ids[:2],
                    "counter_evidence_ids": [],
                    "alternatives": ["也可能是对方的习惯性客套"],
                    "limitations": "样本有限",
                }
            ],
            "persona": {
                "tendencies": {
                    "directness": {"label": "偏直接", "evidence_ids": b_ids[:1]},
                    "emotional_expression": {"label": "适度外露", "evidence_ids": b_ids[1:2]},
                    "detail_preference": {"label": "偏具体", "evidence_ids": b_ids[2:3]},
                    "need_expression": {"label": "较少主动提需求", "evidence_ids": []},
                    "disagreement_handling": {"label": "信息不足", "evidence_ids": []},
                    "rhythm": {"label": "回复较快", "evidence_ids": b_ids[:1]},
                },
                "traits": [{"statement": "在轻松话题上更活跃", "evidence_ids": b_ids[:2], "confidence": "medium"}],
                "inferred_axes": {
                    "EI": {"lean": "E", "confidence": "medium"},
                    "SN": {"lean": "S", "confidence": "low"},
                    "TF": {"lean": "F", "confidence": "medium"},
                    "JP": {"lean": "unclear", "confidence": "low"},
                },
            },
            "stage": {"value": "getting_to_know", "basis_ids": b_ids[:2], "note": "持续互动但无明确关系确认"},
            "boundary": {"value": boundary, "basis_ids": [], "note": ""},
            "playbook": {
                "actions": [
                    {
                        "goal": "自然推进一次线下见面",
                        "action": "从对方提到的吃饭话题延伸出一个具体提议",
                        "basis_ids": b_ids[3:5],
                        "suggested_text": "你上次说想吃那家火锅，这周末要不要去？",
                        "observe": "对方是否给出明确时间",
                        "stop_when": "对方两次没有正面回应",
                        "alternatives": ["先分享店铺链接试探"],
                    }
                ],
                "situations": [
                    {
                        "id": "s1",
                        "title": "对方提过一起吃饭但没定时间",
                        "trigger_ids": b_ids[3:5],
                        "why": "有吃饭的提议，没有具体时间",
                        "options": [
                            {
                                "goal": "把吃饭提议定成具体时间",
                                "action": "给两个时段让对方选",
                                "trigger_ids": b_ids[3:5],
                                "suggested_text": "周六还是周日？你挑。",
                                "expected_reactions": [
                                    {"if_they": "选了一个", "then": "定下来", "signal": "good"},
                                    {"if_they": "不回", "then": "一周内不再提", "signal": "stop"},
                                ],
                                "stop_when": "两次没给时间",
                                "risk": "催得太紧",
                            },
                            {
                                "goal": "先聊聊对方喜欢吃什么",
                                "action": "问一个具体的口味问题",
                                "trigger_ids": b_ids[3:4],
                                "suggested_text": "你上次说的火锅是哪家？",
                                "expected_reactions": [
                                    {"if_they": "展开讲", "then": "顺势提议", "signal": "good"},
                                    {"if_they": "简短带过", "then": "换话题", "signal": "neutral"},
                                ],
                                "stop_when": "对方转移话题",
                                "risk": "",
                            },
                        ],
                    }
                ],
                "landmines": [{"topic": "催促回复", "why": "对方工作忙时回复慢是常态", "evidence_ids": b_ids[:1]}],
                "scripts": {
                    "casual": {"text": "刚看到个好笑的，分享给你", "when_to_use": "日常"},
                    "sincere": {"text": "和你聊天挺开心的", "when_to_use": "有一定基础"},
                    "direct": {"text": "周末一起吃饭？", "when_to_use": "推进"},
                },
            },
        }


def dump(o: dict) -> str:
    return json.dumps(o, ensure_ascii=False)
