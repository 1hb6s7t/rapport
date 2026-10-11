"""Structural cues for one target message (v0.5 plan §3A): facts the program can compute,
handed to the model as context. No labels, no hints about what the answer should be.

The real-material baseline (docs/EVAL.md §15.3) showed two failure modes the model repeats: it
reads an answer to a question as a 委婉拒绝, and it flips polarity between 调侃 and 真生气 when
the surrounding lines were laughing. Both are structural facts the feature layer already has;
this module states them in plain words so the model does not have to infer them from a short
window. Every cue is a (label, value) the reader can check against the lines above it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rapport.features import lexicon as lx
from rapport.features.episodes import Episode
from rapport.schemas.message_v1 import Message, Sender

LOOKBACK = 6


@dataclass
class Cues:
    answers_a_question: bool = False  # the previous A line was a question
    who_opened: str = ""  # A | B | 未知 — who wrote the first line of the window
    a_repeated_requests: int = 0  # A questions / offers in the last LOOKBACK A lines
    a_conceded_or_apologised: bool = False
    laughs_before: int = 0  # laugh markers in the LOOKBACK lines before the target
    b_laughed_before: bool = False
    gap_before_target_s: float | None = None
    target_len: int = 0
    b_median_len: float | None = None
    decline_words: bool = False
    alt_time_words: bool = False
    is_question: bool = False
    extra: dict = field(default_factory=dict)

    def lines(self) -> list[str]:
        out = [
            f"- 目标句是否在回答 A 的提问：{'是' if self.answers_a_question else '否'}",
            f"- 这段对话由谁开启：{self.who_opened or '未知'}",
            f"- 目标句之前 A 连续提问/提议的次数：{self.a_repeated_requests}",
            f"- A 在之前是否让步或道歉：{'是' if self.a_conceded_or_apologised else '否'}",
            f"- 目标句之前 {LOOKBACK} 句里的笑标记数：{self.laughs_before}（B 自己笑过：{'是' if self.b_laughed_before else '否'}）",
        ]
        if self.gap_before_target_s is not None:
            g = self.gap_before_target_s
            human = f"{int(g)} 秒" if g < 120 else (f"{int(g // 60)} 分钟" if g < 7200 else f"{g / 3600:.1f} 小时")
            out.append(f"- 目标句距上一条消息：{human}")
        if self.b_median_len:
            out.append(f"- 目标句长度 {self.target_len} 字；B 在本段的中位长度 {self.b_median_len:.0f} 字")
        out.append(f"- 目标句含拒绝词：{'是' if self.decline_words else '否'}；含改期词：{'是' if self.alt_time_words else '否'}；本身是提问：{'是' if self.is_question else '否'}")
        return out


def _text(m: Message) -> str:
    return (m.text or "").strip()


def compute_cues(episode: Episode) -> Cues:
    msgs = episode.messages
    target = episode.target
    try:
        t_idx = next(i for i, m in enumerate(msgs) if m.id == target.id)
    except StopIteration:
        t_idx = len(msgs) - 1
    before = msgs[:t_idx]
    c = Cues()
    c.target_len = len(_text(target))
    c.is_question = bool(lx.QUESTION_RE.search(_text(target)))
    c.decline_words = bool(lx.DECLINE_RE.search(_text(target)))
    c.alt_time_words = bool(lx.ALT_TIME_RE.search(_text(target)))
    if msgs:
        first = msgs[0]
        c.who_opened = "A" if first.sender == Sender.me else ("B" if first.sender == Sender.other else "未知")
    prev_a = next((m for m in reversed(before) if m.sender == Sender.me and _text(m)), None)
    if prev_a is not None:
        c.answers_a_question = bool(lx.QUESTION_RE.search(_text(prev_a)) or lx.PROPOSE_RE.search(_text(prev_a)))
    recent_a = [m for m in before if m.sender == Sender.me and _text(m)][-LOOKBACK:]
    c.a_repeated_requests = sum(1 for m in recent_a if lx.QUESTION_RE.search(_text(m)) or lx.PROPOSE_RE.search(_text(m)))
    c.a_conceded_or_apologised = any(lx.APOLOGY_RE.search(_text(m)) for m in recent_a)
    window = [m for m in before if _text(m)][-LOOKBACK:]
    c.laughs_before = sum(len(lx.LAUGH_RE.findall(_text(m))) for m in window)
    c.b_laughed_before = any(m.sender == Sender.other and lx.LAUGH_RE.search(_text(m)) for m in window)
    if before and target.ts and before[-1].ts:
        c.gap_before_target_s = max(0.0, (target.ts - before[-1].ts).total_seconds())
    b_lens = sorted(len(_text(m)) for m in msgs if m.sender == Sender.other and _text(m) and m.id != target.id)
    if b_lens:
        mid = len(b_lens) // 2
        c.b_median_len = float(b_lens[mid] if len(b_lens) % 2 else (b_lens[mid - 1] + b_lens[mid]) / 2)
    return c


def cues_block(episode: Episode) -> str:
    """The prompt section. Facts only; the model is told they are not labels."""
    c = compute_cues(episode)
    return "\n".join(
        [
            "结构线索（程序按上下文算出的事实，不是标签，也不是本次对话的证据 id；用来帮你判断这句话在回应什么）：",
            *c.lines(),
        ]
    )
