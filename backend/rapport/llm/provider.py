"""LLM provider abstraction. Only JSON completions; the model never gets tools."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from rapport.errors import RapportError


class LLMError(RapportError):
    """Provider / model failure. HTTP 400 by default; `mode_violation` and `no_key` are user config."""

    def __init__(self, code: str, msg: str, hint: str = ""):
        super().__init__(code, msg, hint, status=400)


@dataclass
class CallUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0
    unknown: bool = False  # provider did not return usage

    def add(self, other: CallUsage) -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.calls += other.calls
        self.unknown = self.unknown or other.unknown


@dataclass
class ProviderInfo:
    preset: str
    base_url: str
    model: str
    endpoint_region: str
    processing_scope: str
    price_date: str
    extra: dict[str, Any] = field(default_factory=dict)


class LLMProvider(Protocol):
    info: ProviderInfo
    reasoning: bool

    def complete_json(self, system: str, user: str, max_tokens: int = 2048, temperature: float = 0.2) -> tuple[dict, CallUsage]: ...

    def ping(self) -> tuple[bool, str]: ...
