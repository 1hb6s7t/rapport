"""v0.4.1: the outbound gate for reply drafts, endpoint hardening, and the material-unchanged guard."""

from __future__ import annotations

from datetime import datetime

import pytest

from rapport.db import Analysis, Event, Import
from rapport.llm.openai_compat import OpenAICompatProvider, endpoint_risk
from rapport.llm.outbound import OutboundGate, payload_hash
from rapport.llm.provider import LLMError
from rapport.llm.reply import ReplyInput, first_person_claims, validate_replies
from tests.conftest import demo_text

R1 = "rel-1"
DIARY = "上周末我一个人去爬了山，山顶风很大。"
CHAT = """[2026-08-20 20:00] A: 今天好累
[2026-08-20 20:10] B: 辛苦了，早点休息
[2026-08-21 09:00] B: 周末你一般干嘛
"""


def _setup(c):
    c.post("/api/relationships", json={"id": R1, "name": R1})
    d = c.post("/api/diary", json={"text": DIARY, "allowed_relationships": [R1]}).json()
    c.patch(f"/api/diary/{d['id']}", json={"approved_clause": DIARY})
    imp = c.post("/api/imports", json={"text": CHAT, "relationship_id": R1}).json()
    c.post(f"/api/imports/{imp['id']}/confirm", json={})
    return imp


# ---------------------------------------------------------------- gate unit


def test_ticket_is_single_use_and_hash_bound():
    g = OutboundGate()
    dest = {"preset": "deepseek", "model": "m", "base_url": "https://x", "price_date": "d"}
    t = g.issue("reply", ["m1 B: hi"], dest, {"cny": 0.01, "price_known": True, "over_threshold": False}, diary_refs=[{"id": "d1", "revision": 2, "chars": 10}])
    assert t.payload_hash == payload_hash(["m1 B: hi"], dest, "d", [{"id": "d1", "revision": 2}])
    with pytest.raises(Exception) as ei:
        g.redeem(t.id, "wrong")
    assert getattr(ei.value, "code", "") == "hash_mismatch"
    assert g.redeem(t.id, t.payload_hash) is t
    with pytest.raises(Exception) as ei2:
        g.redeem(t.id, t.payload_hash)
    assert getattr(ei2.value, "code", "") == "ticket_unknown"


def test_hash_changes_when_diary_revision_changes():
    dest = {"preset": "p", "model": "m", "base_url": "u", "price_date": "d"}
    a = payload_hash(["l"], dest, "d", [{"id": "d1", "revision": 1}])
    b = payload_hash(["l"], dest, "d", [{"id": "d1", "revision": 2}])
    assert a != b


# ---------------------------------------------------------------- gate through the API


def test_preview_shows_the_diary_text_that_would_be_sent(app_client):
    c = app_client
    imp = _setup(c)
    pv = c.post("/api/replies/preview", json={"import_id": imp["id"], "relationship_id": R1}).json()
    assert pv["purpose"] == "reply" and pv["ticket_id"] and pv["payload_hash"]
    assert pv["diary_refs"] and pv["n_diary_chars"] == len(DIARY)
    assert any(DIARY in ln for ln in pv["diary_lines"]), "the user must see the diary sentence before it leaves"
    assert "日记" in "".join(pv["fields_sent"]) and pv["destination"]["connection_mode"] in ("local", "cloud")
    assert c.app_state.provider_override.calls == [], "preview never calls the model"


def test_confirm_refuses_stale_ticket_after_diary_edit(app_client):
    c = app_client
    imp = _setup(c)
    pv = c.post("/api/replies/preview", json={"import_id": imp["id"], "relationship_id": R1}).json()
    entry = c.get("/api/diary").json()["entries"][0]
    c.patch(f"/api/diary/{entry['id']}", json={"text": DIARY + "后来下雨了。"})  # revision bump
    r = c.post("/api/replies", json={"import_id": imp["id"], "relationship_id": R1, "ticket_id": pv["ticket_id"], "payload_hash": pv["payload_hash"], "accept_cost": True})
    assert r.status_code == 409 and r.json()["error"]["code"] == "hash_mismatch"
    assert c.app_state.provider_override.calls == []


def test_confirm_with_wrong_hash_or_reused_ticket_is_refused(app_client):
    c = app_client
    imp = _setup(c)
    pv = c.post("/api/replies/preview", json={"import_id": imp["id"], "relationship_id": R1}).json()
    body = {"import_id": imp["id"], "relationship_id": R1, "ticket_id": pv["ticket_id"], "payload_hash": "0" * 32, "accept_cost": True}
    assert c.post("/api/replies", json=body).status_code == 409
    body["payload_hash"] = pv["payload_hash"]
    assert c.post("/api/replies", json=body).status_code == 200
    r = c.post("/api/replies", json=body)
    assert r.status_code == 404 and r.json()["error"]["code"] == "ticket_unknown", "a ticket is single-use"


def test_reply_response_carries_the_context_it_answered(app_client):
    c = app_client
    imp = _setup(c)
    pv = c.post("/api/replies/preview", json={"import_id": imp["id"], "relationship_id": R1}).json()
    out = c.post("/api/replies", json={"import_id": imp["id"], "relationship_id": R1, "ticket_id": pv["ticket_id"], "payload_hash": pv["payload_hash"], "accept_cost": True}).json()
    ctx = out["context"]
    assert ctx["import_id"] == imp["id"] and ctx["messages"] and ctx["target_msg_id"] == ctx["messages"][-1]["id"]
    assert ctx["messages"][-1]["text"] == "周末你一般干嘛"


def test_strict_mode_needs_no_ticket(app_client):
    c = app_client
    imp = _setup(c)
    assert c.post("/api/replies/preview", json={"import_id": imp["id"], "relationship_id": R1, "mode": "strict"}).status_code == 400
    out = c.post("/api/replies", json={"import_id": imp["id"], "relationship_id": R1, "mode": "strict"}).json()
    assert out["drafts"] and out["drafts"][0]["mode"] == "strict_slot"


# ---------------------------------------------------------------- first-person fact check


def test_first_person_claims_detector():
    assert first_person_claims("我昨天也去了那家店，味道不错。") == ["我昨天也去了那家店，味道不错"]
    assert first_person_claims("你周末一般干嘛？") == []
    assert first_person_claims("我觉得可以试试。") == []


def test_unbacked_first_person_experience_is_flagged_not_dropped():
    inp = ReplyInput(lines=[], styles=["casual"], memories=[], goal="g")
    data = {"drafts": [{"style": "casual", "text": "我上周也去爬了山，累坏了。你呢？", "used_memory_ids": [], "why": "", "risk_notes": []}]}
    pack = validate_replies(data, inp, {}, R1, datetime.now().astimezone())
    assert len(pack.drafts) == 1
    d = pack.drafts[0]
    assert d.needs_review is True and d.unbacked_claims and "核对" in d.verification_note


# ---------------------------------------------------------------- endpoint hardening


def test_endpoint_risk_classification():
    assert endpoint_risk("https://api.deepseek.com") is None
    assert endpoint_risk("http://127.0.0.1:11434/v1") is None
    assert endpoint_risk("https://169.254.169.254/latest") == "metadata_endpoint"
    assert endpoint_risk("https://10.0.0.5/v1") == "private_network"
    assert endpoint_risk("https://192.168.1.20/v1") == "private_network"


def test_private_endpoints_are_refused_unless_allowed():
    with pytest.raises(LLMError) as ei:
        OpenAICompatProvider("custom", "k", "m", "https://169.254.169.254/v1")
    assert ei.value.code == "unsafe_endpoint"
    p = OpenAICompatProvider("custom", "k", "m", "https://10.0.0.5/v1", allow_private=True)
    assert p.info.base_url == "https://10.0.0.5/v1"


def test_client_does_not_follow_redirects_or_trust_env_proxies():
    p = OpenAICompatProvider("deepseek", "k")
    http = p._client._client  # httpx.Client under the SDK
    assert http.follow_redirects is False
    assert http.trust_env is False


# ---------------------------------------------------------------- material-unchanged guard


def test_patching_material_with_an_active_analysis_is_refused(app_client):
    c = app_client
    imp = c.post("/api/imports", json={"text": demo_text()}).json()
    c.post(f"/api/imports/{imp['id']}/confirm", json={})
    c.post("/api/analyses", json={"import_id": imp["id"], "mode": "rules"})
    r = c.patch(f"/api/imports/{imp['id']}", json={"swap_roles": True})
    assert r.status_code == 409 and r.json()["error"]["code"] == "analysis_active"


def test_runner_fails_an_analysis_whose_material_changed(app_client):
    """Belt and braces: even if the API guard were bypassed, the worker checks the hash."""
    c = app_client
    st = c.app_state
    imp = c.post("/api/imports", json={"text": demo_text()}).json()
    c.post(f"/api/imports/{imp['id']}/confirm", json={})
    a = c.post("/api/analyses", json={"import_id": imp["id"], "mode": "rules"}).json()
    with st.db.session() as s:
        s.get(Import, imp["id"]).content_hash = "tampered"
    st.runner.run_pending_once()
    v = c.get(f"/api/analyses/{a['id']}").json()
    assert v["status"] == "failed" and v["error"]["code"] == "import_changed"


def test_swap_roles_rebuilds_the_event_ledger(app_client):
    c = app_client
    st = c.app_state
    imp = c.post("/api/imports", json={"text": CHAT}).json()
    with st.db.session() as s:
        before = {e.sender for e in s.query(Event).filter(Event.first_import_id == imp["id"]).all()}
        n_before = s.query(Event).filter(Event.first_import_id == imp["id"]).count()
    c.patch(f"/api/imports/{imp['id']}", json={"swap_roles": True})
    with st.db.session() as s:
        rows = s.query(Event).filter(Event.first_import_id == imp["id"]).all()
    assert len(rows) == n_before and {r.sender for r in rows} == before
    first = c.get(f"/api/imports/{imp['id']}").json()["messages"][0]
    assert first["sender"] == "other"
    assert any(r.msg_id == first["id"] and r.sender == "other" for r in rows)
    assert all(not r.confirmed for r in rows), "rebuilt events wait for a fresh confirmation"


def test_failed_analysis_does_not_block_a_fresh_one(app_client):
    """v0.3 derived the analysis id from the idempotency key: a failed run made the next 500."""
    c = app_client
    imp = c.post("/api/imports", json={"text": demo_text()}).json()
    c.post(f"/api/imports/{imp['id']}/confirm", json={})
    a = c.post("/api/analyses", json={"import_id": imp["id"], "mode": "rules"}).json()
    with c.app_state.db.session() as s:
        s.get(Analysis, a["id"]).status = "failed"
    r = c.post("/api/analyses", json={"import_id": imp["id"], "mode": "rules"})
    assert r.status_code == 200 and r.json()["id"] != a["id"] and not r.json().get("reused")


def test_prompts_version_tracks_prompt_content():
    from rapport.config import PROMPTS_VERSION
    from rapport.llm.prompts import prompts_digest, prompts_version

    assert PROMPTS_VERSION == prompts_version() and PROMPTS_VERSION.endswith(prompts_digest())
    assert len(prompts_digest()) == 10


def test_rapport_errors_render_uniformly(app_client):
    """A RapportError raised anywhere becomes {error:{code,msg,hint}} with its own status."""
    c = app_client
    r = c.post("/api/imports", json={"text": CHAT, "relationship_id": "ghost"})
    assert r.status_code == 404
    body = r.json()["error"]
    assert body["code"] == "relationship_not_found" and body["hint"]
