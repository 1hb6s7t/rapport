"""Stage 4: windowed extraction. Builds the redacted payload and runs the provider per window."""

from __future__ import annotations

from dataclasses import dataclass, field

from rapport.features.windows import Window, extraction_windows
from rapport.llm.budget import estimate_tokens
from rapport.llm.limits import (
    EXTRACT_MAX_TOKENS,
    EXTRACT_OUTPUT_PER_WINDOW,
    EXTRACT_OVERLAP,
    EXTRACT_WINDOW,
    SYNTH_INPUT_OVERHEAD,
    SYNTH_OUTPUT,
)
from rapport.llm.limits import budget as _budget
from rapport.llm.prompts import load_prompt
from rapport.llm.provider import CallUsage, LLMError, LLMProvider
from rapport.llm.redact import RedactStats, redact_names, redact_text
from rapport.schemas.message_v1 import Conversation, Message, Sender

EXTRACT_SYSTEM = load_prompt("extract")
SYNTH_SYSTEM = load_prompt("synthesize")

VALID_KINDS = {
    "care", "humor", "affection", "self_disclosure", "invitation", "accept", "decline", "deflect",
    "question", "plan", "complaint", "apology", "cold", "short_reply", "conflict", "boundary",
}


@dataclass
class Signal:
    msg_ids: list[str]
    speaker: str
    kind: str
    note: str = ""


@dataclass
class WindowResult:
    index: int
    warmth: int | None
    future: int | None
    tone: str
    signals: list[Signal]
    notable: list[dict]
    msg_ids: list[str]


@dataclass
class RedactedPayload:
    lines_by_window: list[list[str]]
    windows: list[Window]
    stats: RedactStats
    est_tokens_in: int
    est_tokens_out: int
    all_ids: list[str] = field(default_factory=list)

    def preview_text(self, limit_lines: int = 400) -> str:
        out: list[str] = []
        for i, lines in enumerate(self.lines_by_window):
            out.append(f"--- 窗口 {i + 1}/{len(self.lines_by_window)} ---")
            out.extend(lines)
            if len(out) > limit_lines:
                out.append(f"…（共 {sum(len(x) for x in self.lines_by_window)} 行，预览已截断）")
                break
        return "\n".join(out)


def apply_line_edits(lines_by_window: list[list[str]], edited_lines: dict[int, list[str]]) -> list[list[str]]:
    """Return the payload with the user's preview edits applied, window by window."""
    if not edited_lines:
        return [list(w) for w in lines_by_window]
    out: list[list[str]] = []
    for i, window in enumerate(lines_by_window):
        edits = edited_lines.get(i)
        out.append(list(edits) if edits else list(window))
    return out


def lines_by_id(lines_by_window: list[list[str]]) -> dict[str, str]:
    """Map message id -> final line text, so other stages reuse the same edited content."""
    out: dict[str, str] = {}
    for window in lines_by_window:
        for line in window:
            mid = line.split(" ", 1)[0]
            if mid:
                out[mid] = line
    return out


def format_line(m: Message, names: list[tuple[str, str]], stats: RedactStats) -> str:
    who = "A" if m.sender == Sender.me else "B"
    ts = m.ts.strftime("%m-%d %H:%M") if m.has_reliable_ts else ("--:--" if m.ts is None else m.ts.strftime("%m-%d"))
    if m.is_text:
        body = redact_text(m.text or "", stats)
        body = redact_names(body, names, stats)
        body = body.replace("\n", " ")
        if len(body) > 300:
            body = body[:300] + "…"
    else:
        body = f"[{m.type.value}]"
    if m.quoted_text:
        q = redact_names(redact_text(m.quoted_text, stats), names, stats)[:60]
        body = f"（引用：{q}）{body}"
    return f"{m.id} {ts} {who}: {body}"


def build_payload(conv: Conversation, events: list[Message], window_size: int = EXTRACT_WINDOW, overlap: int = EXTRACT_OVERLAP, reasoning: bool = False) -> RedactedPayload:
    stats = RedactStats()
    names = [(conv.participants.me, "A"), (conv.participants.other, "B")]
    windows = extraction_windows(events, window_size, overlap)
    lines_by_window: list[list[str]] = []
    tokens = 0
    all_ids: list[str] = []
    for w in windows:
        lines = [format_line(m, names, stats) for m in w.messages]
        lines_by_window.append(lines)
        tokens += estimate_tokens("\n".join(lines)) + estimate_tokens(EXTRACT_SYSTEM)
        all_ids.extend(m.id for m in w.messages)
    synth_in = estimate_tokens(SYNTH_SYSTEM) + SYNTH_INPUT_OVERHEAD
    est_out = _budget(EXTRACT_OUTPUT_PER_WINDOW * len(windows) + SYNTH_OUTPUT, reasoning)
    return RedactedPayload(lines_by_window, windows, stats, tokens + synth_in, est_out, list(dict.fromkeys(all_ids)))



def run_extraction(provider: LLMProvider, payload: RedactedPayload, index: int, override_lines: list[str] | None = None) -> tuple[WindowResult, CallUsage]:
    lines = override_lines or payload.lines_by_window[index]
    valid_ids = {ln.split(" ", 1)[0] for ln in lines}
    user = "聊天片段（A 是使用者，B 是对方）：\n" + "\n".join(lines)
    budget = _budget(EXTRACT_MAX_TOKENS, bool(getattr(provider, "reasoning", False)))
    try:
        data, usage = provider.complete_json(EXTRACT_SYSTEM, user, max_tokens=budget)
    except LLMError as e:
        if e.code != "truncated":
            raise
        data, usage = provider.complete_json(EXTRACT_SYSTEM, user, max_tokens=budget * 2)
    ws = data.get("window_summary") or {}
    signals: list[Signal] = []
    for s in data.get("signals") or []:
        if not isinstance(s, dict):
            continue
        ids = [i for i in (s.get("msg_ids") or []) if i in valid_ids]
        kind = s.get("kind")
        if not ids or kind not in VALID_KINDS:
            continue
        signals.append(Signal(ids, "A" if s.get("speaker") == "A" else "B", kind, str(s.get("note") or "")[:40]))
    notable = [
        {"msg_ids": [i for i in (n.get("msg_ids") or []) if i in valid_ids], "what": str(n.get("what") or "")[:60]}
        for n in (data.get("notable") or [])
        if isinstance(n, dict)
    ]
    notable = [n for n in notable if n["msg_ids"]]
    return (
        WindowResult(
            index=index,
            warmth=_int_or_none(ws.get("warmth")),
            future=_int_or_none(ws.get("future")),
            tone=str(ws.get("tone") or "")[:20],
            signals=signals,
            notable=notable,
            msg_ids=sorted(valid_ids),
        ),
        usage,
    )


def _int_or_none(v) -> int | None:
    try:
        x = int(v)
    except (TypeError, ValueError):
        return None
    return max(0, min(100, x))


class ExtractionError(LLMError):
    pass
