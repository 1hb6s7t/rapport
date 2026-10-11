"""Local-model helper shared by the evaluation scripts (v0.8).

`--preset ollama|ollama-split|ollama-lite` needs no key file and no network: the provider is the
native Ollama client on loopback. Everything else keeps going through `OpenAICompatProvider`.
"""

from __future__ import annotations

from rapport.config import PROVIDER_PRESETS

LOCAL_PRESETS = tuple(k for k, p in PROVIDER_PRESETS.items() if p.native == "ollama")


def is_local_preset(preset: str | None) -> bool:
    return preset in LOCAL_PRESETS


def make_local_provider(preset: str, model: str | None, base_url: str | None = None, task: str = "intent", timeout: float = 600.0):
    from rapport.llm.ollama import OllamaProvider

    return OllamaProvider(preset, model, base_url, task=task, timeout=timeout)
