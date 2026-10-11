"""Native Ollama provider: JSON mode, thinking off, explicit context window, per-task models.

Why not the OpenAI-compatible layer Ollama also exposes: that layer does not pass `think`
(Qwen3 spends its output budget on hidden reasoning and then truncates the JSON), uses the
model's default `num_ctx` (2048 or 4096 — a 120-message extraction window is silently cut), and
its `response_format` support is best-effort. The native `/api/chat` has `format: "json"`,
`think`, `options.num_ctx` and `keep_alive`, which is exactly what a long local run needs.

Everything stays on loopback; `make_provider` refuses anything else in local mode.
"""

from __future__ import annotations

import json
import time

import httpx

from rapport.config import PROVIDER_PRESETS, is_loopback_url
from rapport.llm.limits import LOCAL_CTX
from rapport.llm.openai_compat import parse_json
from rapport.llm.provider import CallUsage, LLMError, ProviderInfo

DEFAULT_BASE = "http://127.0.0.1:11434"
KEEP_ALIVE = "30m"
PING_SYSTEM = "你是一个 JSON 生成器，只输出一个 JSON 对象，不要解释。"
PING_USER = '输出这个 JSON 对象：{"ok": true}'
PING_MAX_TOKENS = 32


def _root(base_url: str | None) -> str:
    url = (base_url or DEFAULT_BASE).rstrip("/")
    return url[:-3] if url.endswith("/v1") else url


class OllamaProvider:
    reasoning = False  # thinking is switched off per request; budgets are the plain ones

    def __init__(self, preset: str, model: str | None, base_url: str | None = None, task: str = "intent", timeout: float = 600.0):
        p = PROVIDER_PRESETS[preset]
        root = _root(base_url or p.base_url)
        if not is_loopback_url(root):
            raise LLMError("unsafe_endpoint", "本地模型端点必须在本机（127.0.0.1）")
        self.root = root
        self.task = task
        self.timeout = timeout
        self.info = ProviderInfo(
            preset=preset,
            base_url=root,
            model=model or p.default_model,
            endpoint_region=p.endpoint_region,
            processing_scope=p.processing_scope,
            price_date=p.price.verified_on,
            extra={"native": "ollama", "task": task, "num_ctx": LOCAL_CTX.get(task, 8192)},
        )
        self._client = httpx.Client(base_url=root, timeout=timeout, trust_env=False, follow_redirects=False)

    # ---- health ----
    def tags(self) -> list[dict]:
        r = self._client.get("/api/tags")
        r.raise_for_status()
        return list(r.json().get("models") or [])

    def ping(self) -> tuple[bool, str]:
        try:
            names = {m.get("name") for m in self.tags()}
        except Exception as e:  # noqa: BLE001
            return False, f"Ollama 未运行或无法连接（{type(e).__name__}）"
        if not has_model(self.info.model, names):
            return False, f"模型 {self.info.model} 尚未拉取（已有：{', '.join(sorted(n for n in names if n)) or '无'}）"
        # The self-check prompt is deliberately explicit: with the terse "只输出 JSON。/ 返回 {...}" pair
        # qwen3:4b answered `{"error": {"code": 400, ...` in 1–2 of 5 tries and hit the 20-token cap
        # (measured 2026-09-20 on Ollama 0.34.2); this wording passed 10/10 on both 4B and 8B.
        try:
            data, _u = self.complete_json(PING_SYSTEM, PING_USER, max_tokens=PING_MAX_TOKENS)
            return bool(data.get("ok")) or bool(data), "本地模型可用"
        except LLMError as e:
            return False, f"{e.code}: {e.msg}"

    # ---- the one call ----
    def complete_json(self, system: str, user: str, max_tokens: int = 2048, temperature: float = 0.2) -> tuple[dict, CallUsage]:
        from rapport.config import get_settings

        keep_alive = KEEP_ALIVE
        if get_settings().reply_adapter_manifest:
            # This profile shares VRAM with an HF reply adapter. Release a model
            # loaded by this request; preserve any model already resident.
            try:
                resident = self._client.get("/api/ps")
                resident.raise_for_status()
                if resident.json().get("models") == []:
                    keep_alive = 0
            except (httpx.HTTPError, ValueError):
                pass  # Ownership unknown: do not change another caller's residency.
        body = {
            "model": self.info.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": False,
            "format": "json",
            "think": False,
            "keep_alive": keep_alive,
            "options": {"temperature": temperature, "num_predict": max_tokens, "num_ctx": LOCAL_CTX.get(self.task, 8192)},
        }
        t0 = time.time()
        try:
            r = self._client.post("/api/chat", json=body)
        except httpx.TimeoutException as e:
            raise LLMError("timeout", f"本地模型 {self.info.model} 超时（{self.timeout:.0f}s）") from e
        except httpx.HTTPError as e:
            raise LLMError("connection", f"无法连接 Ollama（{type(e).__name__}）") from e
        if r.status_code == 404:
            raise LLMError("status", f"Ollama 返回 404：模型 {self.info.model} 未拉取或名字不对")
        if r.status_code >= 400:
            detail = ""
            try:
                detail = str(r.json().get("error", ""))[:120]
            except Exception:  # noqa: BLE001
                detail = r.text[:120]
            raise LLMError("status", f"Ollama 返回 {r.status_code}" + (f"：{detail}" if detail else ""))
        data = r.json()
        content = (data.get("message") or {}).get("content") or ""
        if data.get("done_reason") == "length":
            raise LLMError("truncated", f"输出被 num_predict={max_tokens} 截断")
        usage = CallUsage(calls=1, prompt_tokens=int(data.get("prompt_eval_count") or 0), completion_tokens=int(data.get("eval_count") or 0))
        usage.unknown = "prompt_eval_count" not in data
        _ = time.time() - t0
        try:
            return parse_json(content), usage
        except LLMError:
            raise
        except Exception as e:  # noqa: BLE001
            raise LLMError("bad_json", f"本地模型返回的不是 JSON（{type(e).__name__}）") from e


def has_model(wanted: str, names: set[str] | list[str]) -> bool:
    """Exact tag match; a bare name matches its `:latest` tag. `qwen3:8b` never matches `qwen3:4b`."""
    tags = {n for n in names if n}
    if wanted in tags:
        return True
    if ":" not in wanted:
        return f"{wanted}:latest" in tags
    return False


def list_local_models(base_url: str | None = None, timeout: float = 5.0) -> list[dict]:
    """[{name, size_gb, family}] or raise LLMError when Ollama is not reachable."""
    root = _root(base_url)
    try:
        r = httpx.get(f"{root}/api/tags", timeout=timeout, trust_env=False)
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise LLMError("connection", f"无法连接 Ollama（{type(e).__name__}）") from e
    out = []
    for m in r.json().get("models") or []:
        out.append({"name": m.get("name"), "size_gb": round((m.get("size") or 0) / 1e9, 1), "family": ((m.get("details") or {}).get("family") or "")})
    return out


def ollama_version(base_url: str | None = None) -> str | None:
    try:
        r = httpx.get(f"{_root(base_url)}/api/version", timeout=3.0, trust_env=False)
        return str(r.json().get("version")) if r.status_code == 200 else None
    except httpx.HTTPError:
        return None


def dumps(o) -> str:
    return json.dumps(o, ensure_ascii=False)
