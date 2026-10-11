from __future__ import annotations

from tests.conftest import demo_text


def test_unpaired_rejected(app_client):
    c = app_client
    c.cookies.clear()
    r = c.post("/api/imports", json={"text": "A: hi"})
    assert r.status_code == 401


def test_csrf_required(app_client):
    c = app_client
    r = c.post("/api/imports", json={"text": "A: hi"}, headers={"x-rapport-csrf": "wrong"})
    assert r.status_code == 403


def test_bad_host_and_origin(app_client):
    c = app_client
    assert c.get("/api/health", headers={"host": "evil.example"}).status_code == 403
    r = c.post("/api/imports", json={"text": "A: hi"}, headers={"origin": "http://evil.example"})
    assert r.status_code == 403


def test_pairing_lockout(app_client):
    c = app_client
    for _ in range(5):
        assert c.post("/api/pair", json={"code": "000000"}).status_code == 401
    assert c.post("/api/pair", json={"code": "123456"}).status_code == 401  # locked


def test_full_rules_flow_and_delete(app_client):
    c = app_client
    r = c.post("/api/imports", json={"text": demo_text()})
    assert r.status_code == 200
    imp = r.json()
    r = c.post(f"/api/imports/{imp['id']}/confirm", json={"range_start": 0, "range_end": 40})
    assert r.status_code == 200 and r.json()["selected"]["truncated"]
    r = c.post("/api/analyses", json={"import_id": imp["id"], "mode": "rules"})
    aid = r.json()["id"]
    r2 = c.post("/api/analyses", json={"import_id": imp["id"], "mode": "rules"})
    assert r2.json()["id"] == aid and r2.json().get("reused")
    c.app_state.runner.run_pending_once()
    a = c.get(f"/api/analyses/{aid}").json()
    assert a["status"] in ("completed", "insufficient_data") and a["report_id"]
    rep = c.get(f"/api/reports/{a['report_id']}").json()
    assert rep["scope"]["msg_analyzed"] == 41
    md = c.get(f"/api/reports/{a['report_id']}/export?format=md").text
    assert "test-key" not in md and "已省略" not in md
    js = c.get(f"/api/reports/{a['report_id']}/export?format=json").json()
    assert all(v["text"] == "[已省略]" for v in js["evidence"].values())
    assert c.delete(f"/api/reports/{a['report_id']}").status_code == 200
    assert c.get(f"/api/reports/{a['report_id']}").status_code == 404


def test_unknown_sender_blocks_confirm_and_patch_fixes(app_client):
    c = app_client
    rows = [{"StrContent": "hi", "Type": 1, "StrTime": "2026-09-01 20:15:33"}, {"StrContent": "yo", "IsSender": 1, "Type": 1, "StrTime": "2026-09-01 20:16:33"}]
    import json

    r = c.post("/api/imports", json={"text": json.dumps(rows), "format": "wechat_json"})
    imp = r.json()
    assert imp["n_unknown"] == 1
    assert c.post(f"/api/imports/{imp['id']}/confirm", json={}).status_code == 422
    r = c.patch(f"/api/imports/{imp['id']}", json={"messages": [{"id": "m000000", "sender": "other"}]})
    assert r.json()["n_unknown"] == 0
    assert c.post(f"/api/imports/{imp['id']}/confirm", json={}).status_code == 200


def test_byok_preview_confirm_run(app_client):
    c = app_client
    imp = c.post("/api/imports", json={"text": demo_text()}).json()
    c.post(f"/api/imports/{imp['id']}/confirm", json={})
    a = c.post("/api/analyses", json={"import_id": imp["id"], "mode": "byok", "profile": {"other": {"mbti": "ENTJ"}}}).json()
    assert a["needs_confirmation"]
    assert c.post(f"/api/analyses/{a['id']}/confirm", json={"payload_hash": "nope"}).status_code == 409
    pv = c.get(f"/api/analyses/{a['id']}/preview").json()
    assert pv["destination"]["has_key"] and pv["cost"]["price_known"]
    joined = "\n".join(ln for w in pv["windows"] for ln in w)
    assert "ENTJ" not in joined
    r = c.post(f"/api/analyses/{a['id']}/confirm", json={"payload_hash": pv["payload_hash"]})
    assert r.status_code == 200 and r.json()["status"] == "queued"
    c.app_state.runner.run_pending_once()
    a = c.get(f"/api/analyses/{a['id']}").json()
    assert a["status"] == "completed"
    rep = c.get(f"/api/reports/{a['report_id']}").json()
    assert rep["mode"] == "byok" and rep["persona"]["available"] and rep["cost"]["actual_usage"]["calls"] >= 2
    # M03 must be visible over HTTP, evidence-bound, and rendered in the export
    assert rep["intent_readings"], "AI mode must return intent readings"
    for ir in rep["intent_readings"]:
        assert ir["msg_id"] and ir["evidence_ids"], "every reading cites its evidence"
        assert all(i in rep["evidence"] for i in ir["evidence_ids"])
        assert ir["candidates"], "every reading offers an alternative explanation"
        assert ir["independent_units"] >= 0
    md = c.get(f"/api/reports/{a['report_id']}/export?format=md").text
    assert "语义与情绪" in md


def test_edit_invalidates_hash_and_analysis_requires_confirm(app_client):
    c = app_client
    imp = c.post("/api/imports", json={"text": demo_text()}).json()
    h1 = c.post(f"/api/imports/{imp['id']}/confirm", json={}).json()["content_hash"]
    r = c.patch(f"/api/imports/{imp['id']}", json={"swap_roles": True})
    assert r.json()["content_hash"] is None
    assert c.post("/api/analyses", json={"import_id": imp["id"], "mode": "rules"}).status_code == 409
    h2 = c.post(f"/api/imports/{imp['id']}/confirm", json={}).json()["content_hash"]
    assert h1 != h2


def test_settings_roundtrip_never_returns_key(app_client):
    c = app_client
    r = c.put("/api/settings", json={"preset": "qwen", "api_key": "sk-secret-123", "cost_warn_cny": 1.0})
    assert r.status_code == 200
    s = c.get("/api/settings").json()
    assert "sk-secret" not in r.text and s["provider"]["preset"] == "qwen" and s["provider"]["has_key"]
    assert c.put("/api/settings", json={"preset": "custom", "base_url": "http://evil.example/v1"}).status_code == 400
    d = c.get("/api/diagnostics").json()
    assert "sk-" not in str(d)


def test_wipe(app_client):
    c = app_client
    c.post("/api/imports", json={"text": demo_text()})
    r = c.delete("/api/data")
    assert r.json()["cleared"]["imports"] == 1 and r.json()["not_covered"]


def test_rules_report_has_no_semantic_readings(app_client):
    """Rules mode is local-only and must not invent semantic readings."""
    c = app_client
    imp = c.post("/api/imports", json={"text": demo_text()}).json()
    c.post(f"/api/imports/{imp['id']}/confirm", json={})
    a = c.post("/api/analyses", json={"import_id": imp["id"], "mode": "rules"}).json()
    c.app_state.runner.run_pending_once()
    a = c.get(f"/api/analyses/{a['id']}").json()
    rep = c.get(f"/api/reports/{a['report_id']}").json()
    assert rep["intent_readings"] == []
    assert "语义与情绪" not in c.get(f"/api/reports/{a['report_id']}/export?format=md").text


def test_local_mode_default_and_switchable_over_http(app_client):
    c = app_client
    st = c.app_state
    from rapport.config import ConnectionMode

    assert c.get("/api/settings").json()["connection_mode"] == "local"
    r = c.put("/api/settings", json={"preset": "custom", "base_url": "https://api.example.invalid/v1", "connection_mode": "cloud"})
    assert r.status_code == 200
    assert c.get("/api/settings").json()["connection_mode"] == "cloud"
    assert st.connection_mode() == ConnectionMode.cloud
    c.put("/api/settings", json={"preset": "ollama", "connection_mode": "local"})
    assert st.connection_mode() == ConnectionMode.local


def test_preview_counts_intent_calls_and_edits_are_honoured(app_client):
    """The cost confirmation must include the intent stage, and confirmed content must match what is sent."""
    c = app_client
    imp = c.post("/api/imports", json={"text": demo_text()}).json()
    c.post(f"/api/imports/{imp['id']}/confirm", json={})
    a = c.post("/api/analyses", json={"import_id": imp["id"], "mode": "byok"}).json()
    pv = c.get(f"/api/analyses/{a['id']}/preview").json()
    assert pv["n_intent_calls"] >= 2, "the intent stage must be visible in the preview"
    assert pv["cost"]["tokens_in"] > sum(len(w) for w in pv["windows"]), "estimate must cover the extra calls"

    # edit one line, confirm, and check the stored hash describes the edited payload
    lines = [list(w) for w in pv["windows"]]
    lines[0][0] = lines[0][0].split(" ", 1)[0] + " 08-20 19:02 A: [已由我修改]"
    r = c.post(f"/api/analyses/{a['id']}/confirm", json={"payload_hash": pv["payload_hash"], "edited_lines": {"0": lines[0]}, "accept_cost": True})
    assert r.status_code == 200
    st = c.app_state
    with st.db.session() as ses:
        from rapport.db import Analysis

        row = ses.get(Analysis, a["id"])
        assert row.confirmed_payload_hash != pv["payload_hash"], "confirmed hash must cover the edits"
        assert row.edited_lines["0"] == lines[0]


def test_intent_stage_sends_the_edited_lines(app_client):
    """An edit the user confirmed must be the content the intent stage sends."""
    c = app_client
    imp = c.post("/api/imports", json={"text": demo_text()}).json()
    c.post(f"/api/imports/{imp['id']}/confirm", json={})
    a = c.post("/api/analyses", json={"import_id": imp["id"], "mode": "byok"}).json()
    pv = c.get(f"/api/analyses/{a['id']}/preview").json()
    edited_line = pv["windows"][0][0].split(" ", 1)[0] + " 08-20 19:02 A: 标记内容XYZ"
    c.post(f"/api/analyses/{a['id']}/confirm", json={"payload_hash": pv["payload_hash"], "edited_lines": {"0": [edited_line]}, "accept_cost": True})
    c.app_state.runner.run_pending_once()
    sep = chr(10)
    sent = sep.join(x + y for x, y in c.app_state.provider_override.calls)
    assert "标记内容XYZ" in sent, "the confirmed edit must reach the model"


def test_long_material_is_previewable_and_needs_an_explicit_accept(app_client):
    """v0.7: the 300-message AI cap is gone. Long material previews with a call count and a
    large_run flag, and confirming it without the accept box is refused."""
    c = app_client
    lines = [f"[2026-0{1 + i // 900}-{1 + (i // 30) % 28:02d} {8 + (i % 12):02d}:{i % 60:02d}] {'A' if i % 2 else 'B'}: 第{i}句话聊点什么" for i in range(2400)]
    imp = c.post("/api/imports", json={"text": "\n".join(lines)}).json()
    c.post(f"/api/imports/{imp['id']}/confirm", json={})
    c.put("/api/settings", json={"preset": "deepseek", "api_key": "test-key", "connection_mode": "cloud"})
    a = c.post("/api/analyses", json={"import_id": imp["id"], "mode": "byok"}).json()
    pv = c.get(f"/api/analyses/{a['id']}/preview")
    assert pv.status_code == 200, pv.text
    p = pv.json()
    assert p["n_messages"] == 2400 and p["large_run"] is True and p["n_windows"] >= 20
    assert p["n_calls"] == p["n_windows"] + 1 + p["n_intent_calls"] and p["est_minutes"] > 0
    r = c.post(f"/api/analyses/{a['id']}/confirm", json={"payload_hash": p["payload_hash"], "accept_cost": False})
    assert r.status_code == 409 and r.json()["error"]["code"] in ("large_run_needs_accept", "cost_over_threshold")
    r = c.post(f"/api/analyses/{a['id']}/confirm", json={"payload_hash": p["payload_hash"], "accept_cost": True})
    assert r.status_code == 200, r.text


def test_desktop_paste_is_off_by_default_and_send_needs_two_switches(app_client, monkeypatch):
    """v0.7: pasting into the user's chat window is opt-in; pressing Enter needs a second switch
    and still a per-request flag. Nothing here touches a real window: the actuator is patched."""
    import rapport.api.desktop as dsk

    c = app_client
    cfg = c.get("/api/desktop/config").json()
    assert cfg["enabled"] is False and cfg["allow_send"] is False
    r = c.post("/api/desktop/paste", json={"text": "hi", "delay_s": 0})
    assert r.status_code in (409, 501)
    # allow_send cannot be turned on before enabled
    assert c.patch("/api/desktop/config", json={"allow_send": True}).status_code == 409
    monkeypatch.setattr(dsk, "available", lambda: True)
    calls = []

    class R:
        def __init__(self, send):
            self.ok, self.reason, self.window_title, self.control_type, self.sent = True, "pasted", "微信", "EditControl", send

        def as_dict(self):
            return {"ok": self.ok, "reason": self.reason, "window_title": self.window_title, "control_type": self.control_type, "sent": self.sent}

    monkeypatch.setattr(dsk, "paste_into_focus", lambda text, send=False, delay_s=0.0: (calls.append((text, send)), R(send))[1])
    assert c.patch("/api/desktop/config", json={"enabled": True}).json()["enabled"] is True
    r = c.post("/api/desktop/paste", json={"text": "你好", "delay_s": 0}).json()
    assert r["ok"] and r["sent"] is False and calls[-1] == ("你好", False)
    # asking to send without the switch is refused, not silently downgraded
    assert c.post("/api/desktop/paste", json={"text": "你好", "send": True, "delay_s": 0}).status_code == 409
    c.patch("/api/desktop/config", json={"allow_send": True})
    r = c.post("/api/desktop/paste", json={"text": "你好", "send": True, "delay_s": 0}).json()
    assert r["sent"] is True and calls[-1] == ("你好", True)
    log = c.get("/api/desktop/log").json()["items"]
    assert log and log[0]["sent"] is True and "text" not in log[0] and log[0]["chars"] == 2
    # turning the feature off also drops allow_send
    assert c.patch("/api/desktop/config", json={"enabled": False}).json()["allow_send"] is False


def test_upload_limit_is_300_mib_and_streams(app_client):
    c = app_client
    assert c.get("/api/settings").json()["limits"]["max_text_bytes"] == 300 * 1024 * 1024
    big = ("[2026-09-01 20:00] A: 你好\n[2026-09-01 20:01] B: 在\n" * 20000).encode("utf-8")  # ~1 MB, streamed in chunks
    r = c.post("/api/imports/file", files={"file": ("chat.txt", big, "text/plain")}, data={"format": "auto"})
    assert r.status_code == 200, r.text[:200]
