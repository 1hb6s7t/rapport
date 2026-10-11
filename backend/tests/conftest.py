"""Shared fixtures."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rapport.config import reset_settings
from rapport.llm.fake import FakeProvider
from rapport.main import create_app

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "synthetic"
DEMO_CHAT = Path(__file__).resolve().parents[1] / "rapport" / "demo" / "demo_chat.txt"


@pytest.fixture
def app_client():
    tmp = Path(tempfile.mkdtemp())
    settings = reset_settings(data_dir=tmp, pairing_code="123456", api_key="test-key")
    app = create_app(settings, provider_override=FakeProvider(), start_runner=False)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        r = c.post("/api/pair", json={"code": "123456"})
        c.headers["x-rapport-csrf"] = r.json()["csrf"]
        c.app_state = app.state.rapport  # type: ignore[attr-defined]
        yield c


def demo_text() -> str:
    return DEMO_CHAT.read_text(encoding="utf-8")
