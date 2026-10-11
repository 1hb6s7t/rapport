from __future__ import annotations

import pytest

from rapport.config import PROVIDER_PRESETS
from rapport.llm.budget import estimate_cost
from rapport.llm.extract import build_payload, run_extraction
from rapport.llm.limits import EXTRACT_MAX_TOKENS, REASONING_MULTIPLIER, SYNTH_MAX_TOKENS
from rapport.llm.provider import CallUsage, LLMError, ProviderInfo
from rapport.llm.synthesize import run_synthesis
from rapport.parsers import parse


class TruncatingProvider:
    """Fails the first N calls with `truncated`, then answers."""

    def __init__(self, fail_times: int = 1, payload: dict | None = None):
        self.info = ProviderInfo("stub", "local://stub", "stub-1", "本机", "本机", "n/a")
        self.reasoning = True
        self.fail_times = fail_times
        self.budgets: list[int] = []
        self.payload = payload or {
            "window_summary": {"warmth": 50, "future": 50, "tone": "中性"},
            "signals": [],
            "notable": [],
        }

    def complete_json(self, system: str, user: str, max_tokens: int = 2048, temperature: float = 0.2):
        self.budgets.append(max_tokens)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise LLMError("truncated", f"输出被 max_tokens={max_tokens} 截断")
        return self.payload, CallUsage(prompt_tokens=10, completion_tokens=10, calls=1)

    def ping(self):
        return True, "OK"


def _conv():
    return parse("[2026-09-01 20:00] A: 你好\n[2026-09-01 20:05] B: 你好呀", "c", "paste")[0]


def test_extraction_retries_with_double_budget_on_truncation():
    conv = _conv()
    payload = build_payload(conv, conv.events())
    p = TruncatingProvider(fail_times=1)
    wr, usage = run_extraction(p, payload, 0)
    expected = EXTRACT_MAX_TOKENS * REASONING_MULTIPLIER
    assert p.budgets == [expected, expected * 2]
    assert wr.msg_ids and usage.calls == 1


def test_extraction_does_not_swallow_other_errors():
    class AuthFail(TruncatingProvider):
        def complete_json(self, system, user, max_tokens=2048, temperature=0.2):
            raise LLMError("auth", "认证失败")

    with pytest.raises(LLMError) as e:
        run_extraction(AuthFail(), build_payload(_conv(), _conv().events()), 0)
    assert e.value.code == "auth"


def test_synthesis_retries_with_double_budget_on_truncation():
    p = TruncatingProvider(fail_times=1, payload={"dims": {}})
    data, usage = run_synthesis(p, "payload")
    expected = SYNTH_MAX_TOKENS * REASONING_MULTIPLIER
    assert p.budgets == [expected, expected * 2] and data == {"dims": {}} and usage.calls == 1


def test_non_reasoning_provider_uses_base_budget():
    p = TruncatingProvider(fail_times=0)
    p.reasoning = False
    conv = _conv()
    run_extraction(p, build_payload(conv, conv.events()), 0)
    assert p.budgets == [EXTRACT_MAX_TOKENS]


def test_mimo_preset_flags_reasoning_and_unknown_price():
    p = PROVIDER_PRESETS["mimo"]
    assert p.reasoning is True
    assert p.base_url == "https://token-plan-cn.xiaomimimo.com/v1"
    assert p.price.input_per_m is None and p.price.output_per_m is None
    c = estimate_cost(1000, 1000, "mimo", 0.5)
    assert c.cny is None and not c.price_known and c.over_threshold


def test_reasoning_multiplier_is_applied_once_not_twice():
    """v0.4.1: the stage budget carries the multiplier; estimate_cost must not inflate again."""
    from rapport.llm.limits import budget

    conv = _conv()
    plain = build_payload(conv, conv.events(), reasoning=False)
    reasoning = build_payload(conv, conv.events(), reasoning=True)
    assert reasoning.est_tokens_out == plain.est_tokens_out * REASONING_MULTIPLIER
    assert budget(100, True) == 100 * REASONING_MULTIPLIER and budget(100, False) == 100
    same = estimate_cost(1000, 1000, "mimo", 999)
    assert same.tokens_out == 1000, "estimate_cost takes the already-multiplied budget as is"


def test_provider_sends_its_own_user_agent():
    """Gateways may block the SDK default UA; we must identify ourselves instead."""
    from rapport.llm.openai_compat import USER_AGENT, OpenAICompatProvider

    provider = OpenAICompatProvider("custom", "test-key", "test-model", "https://example.invalid/v1")
    ua = provider._client.default_headers.get("User-Agent") or provider._client.default_headers.get("user-agent")
    assert ua == USER_AGENT and ua.startswith("rapport/") and "OpenAI" not in ua
