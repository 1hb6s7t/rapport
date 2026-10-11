"""v0.8 local profiles: native Ollama provider, per-task routing, doctor/local-on endpoints.

Ollama itself is never required here: `/api/*` is served by an httpx MockTransport.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import httpx
import pytest

from rapport.config import PROVIDER_PRESETS, ConnectionMode, reset_settings
from rapport.jobs.pipeline import ByokContext
from rapport.llm.fake import FakeProvider
from rapport.llm.limits import LOCAL_CTX, TASKS
from rapport.llm.ollama import OllamaProvider
from rapport.llm.provider import LLMError
from rapport.state import AppState


@pytest.fixture
def state():
    return AppState(reset_settings(data_dir=Path(tempfile.mkdtemp()), pairing_code="123456"))


@pytest.fixture
def client(app_client):
    return app_client


def _mock(handler):
    return httpx.Client(base_url="http://127.0.0.1:11434", transport=httpx.MockTransport(handler))


def test_presets_route_every_task():
    for pid in ("ollama", "ollama-split", "ollama-lite"):
        p = PROVIDER_PRESETS[pid]
        assert p.native == "ollama" and p.task_models
        assert set(p.task_models) == set(TASKS)
    split = PROVIDER_PRESETS["ollama-split"].task_models
    assert split["extract"] == "qwen3:4b" and split["intent"] == "qwen3:8b"


def test_ollama_provider_sends_json_mode_thinking_off_and_ctx():
    seen = {}

    def handler(req: httpx.Request):
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"message": {"role": "assistant", "content": '{"ok": true}'}, "done_reason": "stop", "prompt_eval_count": 12, "eval_count": 4})

    p = OllamaProvider("ollama", "qwen3:8b", task="synthesize")
    p._client = _mock(handler)
    data, usage = p.complete_json("sys", "user", max_tokens=77, temperature=0.3)
    assert data == {"ok": True}
    assert seen["path"] == "/api/chat"
    b = seen["body"]
    assert b["format"] == "json" and b["think"] is False and b["stream"] is False
    assert b["options"]["num_ctx"] == LOCAL_CTX["synthesize"] == 16384
    assert b["options"]["num_predict"] == 77 and b["options"]["temperature"] == 0.3
    assert usage.prompt_tokens == 12 and usage.completion_tokens == 4 and usage.calls == 1 and not usage.unknown


def test_ollama_provider_errors_are_llm_errors():
    def truncated(req):
        return httpx.Response(200, json={"message": {"content": '{"a":'}, "done_reason": "length"})

    def missing(req):
        return httpx.Response(404, json={"error": "model not found"})

    def bad(req):
        return httpx.Response(200, json={"message": {"content": "not json at all"}, "done_reason": "stop"})

    p = OllamaProvider("ollama", "qwen3:8b")
    for h, code in ((truncated, "truncated"), (missing, "status"), (bad, "bad_json")):
        p._client = _mock(h)
        with pytest.raises(LLMError) as e:
            p.complete_json("s", "u")
        assert e.value.code == code


def test_ollama_provider_refuses_non_loopback():
    with pytest.raises(LLMError) as e:
        OllamaProvider("ollama", "qwen3:8b", base_url="http://10.0.0.5:11434")
    assert e.value.code == "unsafe_endpoint"


def test_ping_reports_missing_model():
    def handler(req):
        if req.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3:4b"}]})
        return httpx.Response(200, json={"message": {"content": '{"ok": true}'}, "done_reason": "stop"})

    p = OllamaProvider("ollama", "qwen3:8b")
    p._client = _mock(handler)
    ok, msg = p.ping()
    assert not ok and "尚未拉取" in msg
    p4 = OllamaProvider("ollama-lite", "qwen3:4b")
    p4._client = _mock(handler)
    assert p4.ping()[0]


def test_make_provider_routes_by_task(state):
    state.save_provider_config("ollama-split", None, None, None, None)
    assert state.connection_mode() == ConnectionMode.local
    ex = state.make_provider(task="extract")
    it = state.make_provider(task="intent")
    assert ex.info.model == "qwen3:4b" and it.info.model == "qwen3:8b"
    assert ex.info.extra["native"] == "ollama"
    # a user override of the model applies to every task
    state.save_provider_config("ollama-split", "glm4:9b", None, None, None)
    assert state.make_provider(task="extract").info.model == "glm4:9b"


def test_make_provider_local_needs_no_key(state):
    state.save_provider_config("ollama", None, None, None, None)
    p = state.make_provider()
    assert p.info.base_url == "http://127.0.0.1:11434"
    cfg = state.provider_config()
    assert cfg["native"] == "ollama" and cfg["task_models"]["reply"] == "qwen3:8b"


def test_byok_context_provider_for_falls_back():
    a, b = FakeProvider(), FakeProvider()
    ctx = ByokContext(provider=a, payload=None, providers={"extract": b})  # type: ignore[arg-type]
    assert ctx.provider_for("extract") is b
    assert ctx.provider_for("intent") is a


def test_doctor_report_without_ollama(monkeypatch):
    from rapport import models_cli

    monkeypatch.setattr(models_cli, "ollama_version", lambda *a, **k: None, raising=False)
    monkeypatch.setattr("rapport.llm.ollama.ollama_version", lambda *a, **k: None)
    monkeypatch.setattr(models_cli, "_gpu", lambda: (None, None))
    rep = models_cli.doctor_report()
    assert rep["ollama_running"] is False
    assert rep["recommended_profile"] == "lite"
    assert set(rep["profiles"]) == {"solo", "split", "lite"}
    assert not rep["profiles"]["solo"]["ready"]
    assert models_cli.recommend(8.0, 32.0) == "solo" and models_cli.recommend(4.0, 16.0) == "lite"


def test_models_endpoints(client, monkeypatch):
    monkeypatch.setattr("rapport.llm.ollama.ollama_version", lambda *a, **k: None)
    r = client.get("/api/models/doctor")
    assert r.status_code == 200 and r.json()["ollama_running"] is False
    r = client.post("/api/models/pull", json={"profile": "solo"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "ollama_down"
    r = client.post("/api/models/local-on", json={"profile": "lite"})
    assert r.status_code == 200
    j = r.json()
    assert j["preset"] == "ollama-lite" and j["connection_mode"] == "local" and j["ready"] is False
    s = client.get("/api/settings").json()
    assert s["provider"]["preset"] == "ollama-lite" and s["connection_mode"] == "local"
    assert any(p["id"] == "ollama-split" and p["task_models"] for p in s["presets"])
    r = client.post("/api/models/local-on", json={"profile": "nope"})
    assert r.status_code == 400
