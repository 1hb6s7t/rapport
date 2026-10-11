"""Audited provider: every model call is recorded as metadata (never content).

Wraps any `LLMProvider`. The pipeline does not know it is wrapped: `info`, `reasoning`,
`ping()` and `complete_json()` behave exactly as the inner provider's, and the only side effect
is one `LLMCallRecord` per call handed to `sink`. The purpose of a call is read from the first
line of the system prompt (`TASK: extract|synthesize|intent|reply`), so no stage has to
announce itself.

What is recorded: purpose, preset/model, token usage (or "unknown"), max_tokens, latency,
status (ok / truncated / bad_json / error code), a hash of the request. What is never
recorded: the prompt, the completion, the chat.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass

from rapport.llm.budget import actual_cost
from rapport.llm.provider import CallUsage, LLMError, LLMProvider, ProviderInfo


@dataclass
class LLMCallRecord:
    purpose: str
    preset: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    usage_known: bool
    max_tokens: int
    latency_ms: int
    status: str  # ok | truncated | bad_json | error
    error_code: str | None
    cost_cny: float | None
    prompt_hash: str
    analysis_id: str | None = None
    relationship_id: str | None = None


Sink = Callable[[LLMCallRecord], None]


def purpose_of(system: str) -> str:
    first = (system or "").strip().splitlines()[0] if (system or "").strip() else ""
    if first.upper().startswith("TASK:"):
        return first.split(":", 1)[1].strip().lower()[:16] or "unknown"
    return "unknown"


class AuditedProvider:
    def __init__(
        self,
        inner: LLMProvider,
        sink: Sink,
        analysis_id: str | None = None,
        relationship_id: str | None = None,
    ):
        self.inner = inner
        self.sink = sink
        self.analysis_id = analysis_id
        self.relationship_id = relationship_id

    # ---- pass-through surface ----
    @property
    def info(self) -> ProviderInfo:
        return self.inner.info

    @property
    def reasoning(self) -> bool:
        return bool(getattr(self.inner, "reasoning", False))

    def __getattr__(self, name: str):
        # tests reach into FakeProvider.calls / scripted; keep that working through the wrapper
        return getattr(self.inner, name)

    def bind(self, *, analysis_id: str | None = None, relationship_id: str | None = None) -> AuditedProvider:
        if analysis_id is not None:
            self.analysis_id = analysis_id
        if relationship_id is not None:
            self.relationship_id = relationship_id
        return self

    def ping(self) -> tuple[bool, str]:
        t0 = time.perf_counter()
        ok, msg = self.inner.ping()
        self._emit("ping", 0, 0, False, 5, t0, "ok" if ok else "error", None if ok else "ping_failed", "")
        return ok, msg

    def complete_json(self, system: str, user: str, max_tokens: int = 2048, temperature: float = 0.2) -> tuple[dict, CallUsage]:
        purpose = purpose_of(system)
        phash = hashlib.sha256((system + "\x1f" + user).encode("utf-8", "ignore")).hexdigest()[:32]
        t0 = time.perf_counter()
        try:
            data, usage = self.inner.complete_json(system, user, max_tokens=max_tokens, temperature=temperature)
        except LLMError as e:
            status = "truncated" if e.code == "truncated" else ("bad_json" if e.code == "bad_json" else "error")
            self._emit(purpose, 0, 0, False, max_tokens, t0, status, e.code, phash)
            raise
        except Exception as e:  # noqa: BLE001 - record, then let the caller decide
            self._emit(purpose, 0, 0, False, max_tokens, t0, "error", type(e).__name__[:32], phash)
            raise
        self._emit(purpose, usage.prompt_tokens, usage.completion_tokens, not usage.unknown, max_tokens, t0, "ok", None, phash)
        return data, usage

    def _emit(
        self,
        purpose: str,
        prompt_tokens: int,
        completion_tokens: int,
        usage_known: bool,
        max_tokens: int,
        t0: float,
        status: str,
        error_code: str | None,
        prompt_hash: str,
    ) -> None:
        info = self.inner.info
        cost = actual_cost(prompt_tokens, completion_tokens, info.preset) if usage_known else None
        rec = LLMCallRecord(
            purpose=purpose,
            preset=info.preset,
            model=info.model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            usage_known=usage_known,
            max_tokens=max_tokens,
            latency_ms=int((time.perf_counter() - t0) * 1000),
            status=status,
            error_code=error_code,
            cost_cny=cost,
            prompt_hash=prompt_hash,
            analysis_id=self.analysis_id,
            relationship_id=self.relationship_id,
        )
        try:
            self.sink(rec)
        except Exception:  # noqa: BLE001 - the ledger must never break an analysis
            pass
