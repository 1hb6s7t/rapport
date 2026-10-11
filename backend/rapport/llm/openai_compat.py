"""OpenAI-compatible provider (DeepSeek / Qwen / Moonshot / OpenAI / Ollama / custom)."""

from __future__ import annotations

import ipaddress
import json
import re
from urllib.parse import urlparse

import httpx
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    OpenAI,
    RateLimitError,
)

from rapport import __version__
from rapport.config import PROVIDER_PRESETS
from rapport.llm.provider import CallUsage, LLMError, ProviderInfo

USER_AGENT = f"rapport/{__version__} (self-hosted relationship-analyzer)"

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.S)
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


def endpoint_risk(url: str) -> str | None:
    """Why a custom endpoint should be refused: cloud metadata address or private network."""
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return "bad_url"
    if host in ("127.0.0.1", "localhost", "::1"):
        return None
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None  # a DNS name; resolution happens at request time and is out of scope here
    if ip.is_link_local or host.startswith("169.254."):
        return "metadata_endpoint"
    if ip.is_private:
        return "private_network"
    return None


class OpenAICompatProvider:
    def __init__(self, preset: str, api_key: str | None, model: str | None = None, base_url: str | None = None, timeout: float = 90.0, allow_private: bool = False):
        p = PROVIDER_PRESETS[preset]
        url = base_url or p.base_url
        if not url:
            raise LLMError("no_base_url", "自定义端点需要填写 base_url")
        if not url.startswith("https://") and not url.startswith("http://127.0.0.1") and not url.startswith("http://localhost"):
            raise LLMError("insecure_endpoint", "只允许 HTTPS 端点或本机 http 端点")
        risk = endpoint_risk(url)
        if risk and not allow_private:
            raise LLMError(
                "unsafe_endpoint",
                "该端点指向云元数据地址或内网地址，默认拒绝。" if risk != "bad_url" else "端点地址无法解析",
                "如果这确实是你自己的内网模型服务，设置 RAPPORT_ALLOW_PRIVATE_ENDPOINTS=1 后重启。",
            )
        self.info = ProviderInfo(
            preset=preset,
            base_url=url,
            model=model or p.default_model,
            endpoint_region=p.endpoint_region,
            processing_scope=p.processing_scope,
            price_date=p.price.verified_on,
        )
        self._json_mode = p.supports_json_mode
        self.reasoning = p.reasoning
        # Some gateways (and WAFs) block the SDK's default User-Agent / x-stainless-* fingerprint.
        # Identifying ourselves explicitly is both politer and more portable than looking like the SDK.
        # No redirects (a redirect could move the request to another host after the check above),
        # no proxy inherited from the environment (a proxy is a third party the user did not name).
        self._client = OpenAI(
            api_key=api_key or "sk-local",
            base_url=url,
            timeout=timeout,
            max_retries=1,
            default_headers={"User-Agent": USER_AGENT},
            http_client=httpx.Client(follow_redirects=False, trust_env=False, timeout=timeout),
        )

    def ping(self) -> tuple[bool, str]:
        try:
            r = self._client.chat.completions.create(
                model=self.info.model,
                messages=[{"role": "user", "content": "回复 OK"}],
                max_tokens=5,
                temperature=0,
            )
            return True, (r.choices[0].message.content or "")[:20]
        except AuthenticationError:
            return False, "认证失败：API Key 无效"
        except APIConnectionError as e:
            return False, f"无法连接端点：{type(e).__name__}"
        except APIStatusError as e:
            return False, f"供应商返回 {e.status_code}"
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}"

    def complete_json(self, system: str, user: str, max_tokens: int = 2048, temperature: float = 0.2) -> tuple[dict, CallUsage]:
        kwargs: dict = dict(
            model=self.info.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=max_tokens,
            temperature=temperature,
        )
        if self._json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            try:
                r = self._client.chat.completions.create(**kwargs)
            except APIStatusError as e:
                # Some OpenAI-compatible endpoints reject response_format with 400. Retry once without it
                # and rely on the robust JSON extraction in parse_json().
                body = ""
                try:
                    body = str(getattr(e, "body", "") or getattr(e, "message", "") or "")
                except Exception:  # noqa: BLE001
                    body = ""
                if e.status_code == 400 and "response_format" in kwargs and ("response_format" in body or "json_object" in body or not body):
                    kwargs.pop("response_format")
                    self._json_mode = False
                    r = self._client.chat.completions.create(**kwargs)
                else:
                    raise
        except AuthenticationError as e:
            raise LLMError("auth", "认证失败：API Key 无效或无权限") from e
        except RateLimitError as e:
            raise LLMError("rate_limit", "供应商限流（429），请稍后重试") from e
        except APITimeoutError as e:
            raise LLMError("timeout", "请求超时；供应商可能已计费但未返回结果") from e
        except APIConnectionError as e:
            raise LLMError("connection", "无法连接供应商端点") from e
        except APIStatusError as e:
            # Carry the gateway's own one-line reason: "403" alone hid "All available accounts
            # exhausted" behind what looked like an auth problem (2026-09-18).
            detail = _status_detail(e)
            raise LLMError("status", f"供应商返回错误状态 {e.status_code}" + (f"：{detail}" if detail else "")) from e
        choice = r.choices[0]
        content = choice.message.content or ""
        if len(content.encode("utf-8", "ignore")) > MAX_RESPONSE_BYTES:
            raise LLMError("response_too_large", "模型返回体超过上限，已丢弃")
        if choice.finish_reason == "length":
            raise LLMError(
                "truncated",
                f"输出被 max_tokens={max_tokens} 截断（推理型模型会先消耗思考 token）。",
            )
        usage = CallUsage(calls=1)
        if r.usage:
            usage.prompt_tokens = r.usage.prompt_tokens or 0
            usage.completion_tokens = r.usage.completion_tokens or 0
        else:
            usage.unknown = True
        return parse_json(content), usage


def _status_detail(e: APIStatusError) -> str:
    """The provider's short error message, if the body carries one; never the whole body."""
    body = getattr(e, "body", None)
    msg = None
    if isinstance(body, dict):
        err = body.get("error")
        msg = err.get("message") if isinstance(err, dict) else (err if isinstance(err, str) else body.get("message"))
    elif isinstance(body, str):
        msg = body
    msg = " ".join((msg or "").split())
    return msg[:120]


def parse_json(content: str) -> dict:
    content = content.strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    m = _JSON_BLOCK.search(content)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    start, end = content.find("{"), content.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(content[start : end + 1])
        except json.JSONDecodeError as e:
            raise LLMError("bad_json", f"模型输出不是合法 JSON：{e.msg}") from e
    raise LLMError("bad_json", "模型输出中没有 JSON 对象")
