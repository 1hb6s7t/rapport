"""v0.4: the model-call ledger (metadata only) and the user feedback loop."""

from __future__ import annotations

import tempfile
from pathlib import Path

from rapport.config import reset_settings
from rapport.db import LLMCall
from rapport.llm.audit import AuditedProvider, LLMCallRecord, purpose_of
from rapport.llm.fake import FakeProvider
from rapport.llm.provider import LLMError
from rapport.state import AppState
from tests.conftest import demo_text

# ---------------------------------------------------------------- unit: wrapper


class Exploding(FakeProvider):
    def __init__(self, code: str):
        super().__init__()
        self.code = code

    def complete_json(self, system, user, max_tokens=2048, temperature=0.2):
        raise LLMError(self.code, "boom")


def test_purpose_is_read_from_the_task_line():
    assert purpose_of("TASK: intent\n\n你在帮...") == "intent"
    assert purpose_of("TASK: synthesize") == "synthesize"
    assert purpose_of("no marker here") == "unknown"
    assert purpose_of("") == "unknown"


def test_wrapper_records_metadata_and_never_content():
    seen: list[LLMCallRecord] = []
    p = AuditedProvider(FakeProvider(), sink=seen.append, analysis_id="a1", relationship_id="amy")
    system = "TASK: extract\n\n秘密提示词内容"
    user = "m000001 09:00 B: 一句绝不能进账本的聊天"
    data, usage = p.complete_json(system, user, max_tokens=777)
    assert data and usage.calls == 1
    assert len(seen) == 1
    rec = seen[0]
    assert (rec.purpose, rec.analysis_id, rec.relationship_id, rec.max_tokens, rec.status) == ("extract", "a1", "amy", 777, "ok")
    assert rec.prompt_tokens == usage.prompt_tokens and rec.usage_known
    dumped = repr(rec)
    assert "秘密提示词" not in dumped and "绝不能进账本" not in dumped
    assert len(rec.prompt_hash) == 32


def test_wrapper_records_truncation_and_errors_then_reraises():
    seen: list[LLMCallRecord] = []
    for code, status in (("truncated", "truncated"), ("bad_json", "bad_json"), ("rate_limit", "error")):
        p = AuditedProvider(Exploding(code), sink=seen.append)
        try:
            p.complete_json("TASK: intent", "x")
        except LLMError as e:
            assert e.code == code
        else:  # pragma: no cover
            raise AssertionError("must re-raise")
        assert seen[-1].status == status and seen[-1].error_code == code


def test_wrapper_passes_through_info_reasoning_and_inner_attributes():
    inner = FakeProvider()
    p = AuditedProvider(inner, sink=lambda r: None)
    assert p.info is inner.info and p.reasoning is False
    p.complete_json("TASK: reply", "hi")
    assert p.calls is inner.calls and len(inner.calls) == 1, "tests may still inspect the fake through the wrapper"


def test_a_broken_sink_never_breaks_the_call():
    def bad_sink(rec):
        raise RuntimeError("ledger down")

    p = AuditedProvider(FakeProvider(), sink=bad_sink)
    data, _ = p.complete_json("TASK: extract", "m000001 B: hi")
    assert data


# ---------------------------------------------------------------- integration: ledger


def _byok_report(c) -> tuple[str, str]:
    imp = c.post("/api/imports", json={"text": demo_text()}).json()
    c.post(f"/api/imports/{imp['id']}/confirm", json={})
    a = c.post("/api/analyses", json={"import_id": imp["id"], "mode": "byok"}).json()
    pv = c.get(f"/api/analyses/{a['id']}/preview").json()
    c.post(f"/api/analyses/{a['id']}/confirm", json={"payload_hash": pv["payload_hash"], "accept_cost": True})
    c.app_state.runner.run_pending_once()
    return a["id"], c.get(f"/api/analyses/{a['id']}").json()["report_id"]


def test_every_pipeline_call_lands_in_the_ledger_with_its_purpose(app_client):
    c = app_client
    aid, rid = _byok_report(c)
    out = c.get(f"/api/analyses/{aid}/calls").json()
    purposes = {r["purpose"] for r in out["calls"]}
    assert {"extract", "synthesize", "intent"} <= purposes, purposes
    assert out["summary"]["calls"] == len(out["calls"]) >= 3
    rep = c.get(f"/api/reports/{rid}").json()
    assert rep["cost"]["actual_usage"]["calls"] == out["summary"]["calls"], "report usage and ledger agree"
    with c.app_state.db.session() as s:
        row = s.query(LLMCall).first()
        assert not hasattr(row, "prompt") and not hasattr(row, "completion")


def test_stats_endpoint_aggregates_by_purpose(app_client):
    c = app_client
    assert c.get("/api/stats").json()["llm"]["totals"]["calls"] == 0
    _byok_report(c)
    stats = c.get("/api/stats", params={"days": 7}).json()
    llm = stats["llm"]
    assert llm["totals"]["calls"] >= 3 and set(llm["by_purpose"]) >= {"extract", "synthesize", "intent"}
    assert llm["totals"]["failure_rate"] == 0.0 and "avg_latency_ms" in llm["totals"]
    assert "提示词" in llm["note"]
    assert stats["relationships"] == 1


def test_test_provider_ping_is_recorded(app_client):
    c = app_client
    c.post("/api/settings/test-provider")
    st = c.app_state
    assert st.llm_stats()["by_purpose"].get("ping", {}).get("calls") == 1


# ---------------------------------------------------------------- feedback


def test_feedback_roundtrip_replaces_and_deletes(app_client):
    c = app_client
    _, rid = _byok_report(c)
    rep = c.get(f"/api/reports/{rid}").json()
    msg = rep["intent_readings"][0]["msg_id"]
    r = c.post(f"/api/reports/{rid}/feedback", json={"target_kind": "intent", "target_key": msg, "verdict": "disagree", "note": "其实是在开玩笑", "correction": {"pragmatic_tags": ["调侃"]}})
    assert r.status_code == 200 and r.json()["verdict"] == "disagree"
    # a second verdict on the same claim replaces the first instead of stacking
    c.post(f"/api/reports/{rid}/feedback", json={"target_kind": "intent", "target_key": msg, "verdict": "agree"})
    items = c.get(f"/api/reports/{rid}/feedback").json()["items"]
    assert len(items) == 1 and items[0]["verdict"] == "agree" and items[0]["correction"] == {}
    assert c.post(f"/api/reports/{rid}/feedback", json={"target_kind": "vibes", "verdict": "agree"}).status_code == 422
    assert c.post(f"/api/reports/{rid}/feedback", json={"target_kind": "stage", "verdict": "maybe"}).status_code == 422
    assert c.delete(f"/api/feedback/{items[0]['id']}").status_code == 200
    assert c.get(f"/api/reports/{rid}/feedback").json()["items"] == []


def test_feedback_export_builds_eval_cases_from_intent_verdicts(app_client):
    c = app_client
    _, rid = _byok_report(c)
    rep = c.get(f"/api/reports/{rid}").json()
    reading = rep["intent_readings"][0]
    c.post(f"/api/reports/{rid}/feedback", json={"target_kind": "intent", "target_key": reading["msg_id"], "verdict": "disagree", "correction": {"pragmatic_tags": ["调侃"]}})
    c.post(f"/api/reports/{rid}/feedback", json={"target_kind": "action", "target_key": "0", "verdict": "agree"})
    out = c.get("/api/feedback/export").json()
    assert out["n"] == 1, "only intent verdicts become evaluation cases"
    case = out["cases"][0]
    assert case["text"] == reading["surface_text"] and case["verdict"] == "disagree"
    assert case["corrected"] == {"pragmatic_tags": ["调侃"]} and case["adjudicated"] is False
    assert case["model_pragmatic_tags"] == reading["pragmatic_tags"]
    assert "裁定" in out["note"]


def test_feedback_never_changes_the_report_and_dies_with_it(app_client):
    c = app_client
    _, rid = _byok_report(c)
    before = c.get(f"/api/reports/{rid}").json()
    c.post(f"/api/reports/{rid}/feedback", json={"target_kind": "affinity", "verdict": "disagree", "note": "太高了"})
    after = c.get(f"/api/reports/{rid}").json()
    assert before["affinity"] == after["affinity"] and before["intent_readings"] == after["intent_readings"]
    assert c.get("/api/stats").json()["feedback"]["by_kind"]["affinity"]["disagree"] == 1
    c.delete(f"/api/reports/{rid}")
    assert c.get("/api/stats").json()["feedback"]["total"] == 0


def test_feedback_note_stays_out_of_exports_and_diagnostics(app_client):
    c = app_client
    _, rid = _byok_report(c)
    c.post(f"/api/reports/{rid}/feedback", json={"target_kind": "affinity", "verdict": "unsure", "note": "这条反馈备注不该外泄"})
    assert "不该外泄" not in c.get(f"/api/reports/{rid}/export?format=md").text
    assert "不该外泄" not in c.get("/api/diagnostics").text
    assert "不该外泄" not in c.get(f"/api/reports/{rid}").text


def test_wipe_clears_ledger_and_feedback():
    st = AppState(reset_settings(data_dir=Path(tempfile.mkdtemp()), pairing_code="123456", api_key="test-key"), provider_override=FakeProvider())
    st.make_provider().complete_json("TASK: extract", "m000001 B: hi")
    assert st.llm_stats()["totals"]["calls"] == 1
    out = st.wipe_all()
    assert out["llm_calls"] == 1 and st.llm_stats()["totals"]["calls"] == 0
