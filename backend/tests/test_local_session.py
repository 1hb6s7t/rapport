import pytest
from fastapi.testclient import TestClient

from rapport.config import Settings
from rapport.llm.fake import FakeProvider
from rapport.main import create_app
from rapport.security.auth import SESSION_COOKIE


@pytest.fixture
def local_app(tmp_path):
    return create_app(Settings(data_dir=tmp_path, local_auto_session=True), provider_override=FakeProvider(), start_runner=False)


def client(app, host="127.0.0.1"):
    return TestClient(app, base_url="http://127.0.0.1", client=(host, 12345))


def test_local_browser_opens_without_any_pairing_code(local_app):
    assert local_app.state.auth.pairing_code == ""
    with client(local_app) as c:
        assert c.get("/api/relationships").status_code == 401
        result = c.post("/api/session", headers={"x-rapport-local": "1", "origin": "http://127.0.0.1"})
        assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
        token = c.cookies[SESSION_COOKIE]
        assert "httponly" in result.headers["set-cookie"].lower()
        assert c.get("/api/relationships").status_code == 200
        repeated = c.post("/api/session", headers={"x-rapport-local": "1"})
        assert repeated.json()["csrf"] == result.json()["csrf"] and c.cookies[SESSION_COOKIE] == token
        assert c.post("/api/pair", json={"code": "000000"}).status_code == 404
        assert c.get("/api/health").json()["automatic_session"] is True


@pytest.mark.parametrize("headers", [
    {}, {"x-rapport-local": "1", "origin": "https://evil.example"},
    {"x-rapport-local": "1", "origin": "http://127.0.0.1:9999"},
    {"x-rapport-local": "1", "sec-fetch-site": "cross-site"},
    {"x-rapport-local": "1", "sec-fetch-site": "same-site"},
])
def test_cross_site_or_unexpected_bootstrap_is_rejected(local_app, headers):
    with client(local_app) as c:
        assert c.post("/api/session", headers=headers).status_code == 403
        assert not local_app.state.auth.sessions


def test_lan_peer_cannot_obtain_a_session_even_with_local_host_header(local_app):
    with client(local_app, "192.168.1.22") as c:
        assert c.post("/api/session", headers={"x-rapport-local": "1"}).status_code == 403


def test_sessions_stay_browser_scoped_and_writes_still_need_csrf(local_app):
    with client(local_app) as first:
        one = first.post("/api/session", headers={"x-rapport-local": "1"}).json()
        with client(local_app) as second:
            two = second.post("/api/session", headers={"x-rapport-local": "1"}).json()
            assert one["csrf"] != two["csrf"]
            assert first.cookies[SESSION_COOKIE] != second.cookies[SESSION_COOKIE]
            assert first.patch("/api/wechat/config", json={"enabled": True}).status_code == 403
            assert first.patch("/api/wechat/config", json={"enabled": True}, headers={"x-rapport-csrf": two["csrf"]}).status_code == 403
            assert first.patch("/api/wechat/config", json={"enabled": True}, headers={"x-rapport-csrf": one["csrf"]}).status_code == 200


def test_server_session_reset_can_reconnect_without_manual_input(local_app):
    with client(local_app) as c:
        before = c.post("/api/session", headers={"x-rapport-local": "1"}).json()
        local_app.state.auth.revoke_all()
        assert c.get("/api/relationships").status_code == 401
        after = c.post("/api/session", headers={"x-rapport-local": "1"}).json()
        assert before["csrf"] != after["csrf"]
        assert c.get("/api/relationships").status_code == 200
