import json

import httpx
import pytest

from rapport.config import reset_settings
from rapport.llm.ollama import KEEP_ALIVE, OllamaProvider


@pytest.mark.parametrize('resident, expected', [([], 0), ([{'name': 'qwen3:8b'}], KEEP_ALIVE)])
def test_only_request_owned_ollama_loads_are_released(tmp_path, resident, expected):
    reset_settings(data_dir=tmp_path, reply_adapter_manifest='selected.json')
    requests = []

    def respond(request):
        if request.url.path == '/api/ps':
            return httpx.Response(200, json={'models': resident})
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'message': {'content': '{"ok":true}'}, 'done_reason': 'stop',
                                        'prompt_eval_count': 3, 'eval_count': 3})
    provider = OllamaProvider('ollama', 'qwen3:8b')
    provider._client.close()
    provider._client = httpx.Client(base_url='http://127.0.0.1:11434', transport=httpx.MockTransport(respond))
    try:
        provider.complete_json('TASK: intent', 'synthetic')
        assert requests[0]['keep_alive'] == expected
    finally:
        provider._client.close()


def test_resident_reply_model_is_released_before_another_gpu_provider(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from rapport.llm import gpu_guard

    events = []

    @contextmanager
    def lease(*args):
        events.append("lease")
        yield

    monkeypatch.setattr(gpu_guard, "GPULease", lease)
    inner = SimpleNamespace(complete_json=lambda *_: events.append("generate"))
    guarded = gpu_guard.GPUGuardedProvider(inner, tmp_path, before_call=lambda: events.append("unload_selected"))
    guarded.complete_json("synthetic")
    assert events == ["unload_selected", "lease", "generate"]
