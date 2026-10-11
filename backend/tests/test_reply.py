"""Phase 3: reply drafts — the no-fabrication check, strict mode and 'never sends' (M10)."""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rapport.config import reset_settings
from rapport.diary.authorize import render_strict_reply
from rapport.features.episodes import build_episodes, episode_for
from rapport.llm.reply import (
    ReplyInput,
    build_reply_input,
    strict_pack,
    validate_replies,
    verify_no_unauthorized_diary_text,
)
from rapport.parsers import ParseOptions, parse
from rapport.schemas.diary import DiaryEntry, DiaryKind
from rapport.state import AppState

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
R1, R2 = "rel-1", "rel-2"
CHAT = """[2026-08-20 20:00] A: 今天好累
[2026-08-20 20:10] B: 辛苦了，早点休息
[2026-08-21 09:00] B: 早呀
[2026-08-21 09:30] A: 早
[2026-08-22 21:00] B: 周末一起吃个饭？
[2026-08-22 21:05] A: 好呀
"""


def test_reply_target_uses_confirmed_positions_not_parser_sequence(app_client):
    from rapport.api.replies import _prepare
    from rapport.db import Import
    from rapport.schemas.api import ReplyIn
    from rapport.schemas.message_v1 import Conversation, Message, Sender, Source

    state = app_client.app_state
    conv = Conversation(id="one-based-dsh", source=Source.wechat_json, messages=[
        Message(id="old", seq=1, sender=Sender.other, text="旧消息"),
        Message(id="last", seq=99, sender=Sender.other, text="测试收到"),
    ])
    imp = state.save_import(conv, "dsh_database", "test", relationship_id="default")
    with state.db.session() as session:
        saved = session.get(Import, imp.id)
        saved.status, saved.range_start, saved.range_end = "confirmed", 1, 1
    prepared = _prepare(state, ReplyIn(import_id=imp.id, relationship_id="default", memory_enabled=False))
    assert prepared[3].target.id == "last"
    assert [m.id for m in prepared[3].messages] == ["last"]

DIARY_TEXT = "周末和朋友一起吃了饭，挺开心的。"  # overlaps the conversation on purpose


def entry(eid: str, text: str = DIARY_TEXT, *, enabled: bool = True, relationships=frozenset({R1}), approved: str | None = None) -> DiaryEntry:
    return DiaryEntry(
        id=eid,
        text=text,
        kind=DiaryKind.fact,
        enabled=enabled,
        allowed_relationships=relationships,
        recorded_at=NOW - timedelta(hours=3),
        approved_clause=approved,
    )


# ---------------------------------------------------------------- the check itself


def test_authorised_diary_text_is_allowed_and_credited():
    e = entry("d1")
    ok, note, credited = verify_no_unauthorized_diary_text(f"我{DIARY_TEXT}你呢？", {"d1": e}, {"d1": e})
    assert ok and credited == {"d1"} and "已核对" in note


def test_text_from_a_disabled_or_out_of_scope_entry_is_refused():
    other_text = "昨天去看了一场很晚的电影。"
    for bad in (entry("d2", text=other_text, enabled=False), entry("d3", text=other_text, relationships=frozenset({R2}))):
        ok, note, credited = verify_no_unauthorized_diary_text(other_text, {"d1": entry("d1")}, {"d1": entry("d1"), bad.id: bad})
        assert not ok and credited == set()
        assert "未获授权" in note


def test_unrelated_text_passes_without_credit():
    ok, _note, credited = verify_no_unauthorized_diary_text("最近在忙什么？", {"d1": entry("d1")}, {"d1": entry("d1")})
    assert ok and credited == set()


# ---------------------------------------------------------------- validation


def _input(memories: list[DiaryEntry]) -> ReplyInput:
    conv, _ = parse(CHAT, "c1", "paste", None, ParseOptions())
    events = conv.events()
    ep = episode_for(conv, build_episodes(conv, events), events[-1], mode="reply")
    return build_reply_input(conv, ep, memories, "自然地回应")


def test_fabricated_draft_is_dropped_but_clean_drafts_survive():
    memories = [entry("d1")]
    data = {"drafts": [
        {"style": "casual", "text": f"我{DIARY_TEXT}", "used_memory_ids": ["d1"], "why": "带日记素材"},
        {"style": "sincere", "text": "昨天去看了一场很晚的电影。", "used_memory_ids": [], "why": "偷偷用了未授权内容"},
    ]}
    store = {"d1": entry("d1"), "d9": entry("d9", text="昨天去看了一场很晚的电影。", enabled=False)}
    pack = validate_replies(data, _input(memories), store, R1, NOW)
    assert [d.style for d in pack.drafts] == ["casual"], "the draft reusing a disabled entry must be dropped"
    assert pack.drafts[0].verified is True
    assert pack.drafts[0].memory_refs[0].id == "d1"
    assert pack.drafts[0].memory_refs[0].revision == 1


def test_declared_but_unused_memory_ids_are_reconciled():
    memories = [entry("d1"), entry("d2", text="晚上和朋友吃了火锅。")]
    data = {"drafts": [{"style": "casual", "text": "在忙什么？", "used_memory_ids": ["d1", "d2"], "why": "", "risk_notes": []}]}
    pack = validate_replies(data, _input(memories), {m.id: m for m in memories}, R1, NOW)
    assert pack.drafts[0].memory_refs == [], "nothing was actually used"
    assert "并未真正用到" in pack.drafts[0].verification_note


def test_risk_notes_are_carried_and_empty_output_abstains():
    memories = [entry("d1")]
    data = {"drafts": [{"style": "direct", "text": "周末一起吃饭吧", "used_memory_ids": [], "risk_notes": ["对方最近回复偏短", "别催"]}]}
    pack = validate_replies(data, _input(memories), {m.id: m for m in memories}, R1, NOW)
    assert pack.drafts[0].risk_notes == ["对方最近回复偏短", "别催"]
    assert "不会代替你发送" in pack.send_note

    empty = validate_replies({"drafts": []}, _input(memories), {m.id: m for m in memories}, R1, NOW)
    assert empty.drafts == [] and empty.abstain_reason


def test_unknown_style_or_empty_text_is_ignored():
    memories = [entry("d1")]
    data = {"drafts": [{"style": "编造的风格", "text": "x"}, {"style": "casual", "text": "   "}]}
    pack = validate_replies(data, _input(memories), {m.id: m for m in memories}, R1, NOW)
    assert pack.drafts == []


# ---------------------------------------------------------------- strict mode


def test_strict_mode_is_verbatim_and_needs_no_model():
    approved = DIARY_TEXT
    e = entry("d1", approved=approved)
    out = render_strict_reply(e, R1, NOW)
    assert out["text"].endswith(approved)
    pack = strict_pack([e], R1, NOW)
    assert pack.drafts and pack.drafts[0].mode == "strict_slot"
    assert pack.drafts[0].text.endswith(approved)
    assert "逐字" in pack.drafts[0].verification_note


def test_strict_mode_abstains_without_an_approved_clause():
    pack = strict_pack([entry("d1")], R1, NOW)
    assert pack.drafts == [] and pack.abstain_reason


# ---------------------------------------------------------------- API


def _state() -> AppState:
    return AppState(reset_settings(data_dir=Path(tempfile.mkdtemp()), pairing_code="123456"))


def _rels(c) -> None:
    """Relationships must exist before they can scope a draft (v0.4)."""
    for rid in (R1, R2):
        c.post("/api/relationships", json={"id": rid, "name": rid})


def _seed(c, *, enabled: bool = True, approved: bool = True, relationships: list[str] | None = None) -> dict:
    _rels(c)
    body = {"text": DIARY_TEXT, "allowed_relationships": relationships if relationships is not None else [R1]}
    created = c.post("/api/diary", json=body).json()
    if approved:
        c.patch(f"/api/diary/{created['id']}", json={"approved_clause": DIARY_TEXT})
    if not enabled:
        c.post(f"/api/diary/{created['id']}/enabled", params={"enabled": "false"})
    return created


def _free(c, body: dict) -> dict:
    """v0.4.1: free mode goes preview -> confirm; the ticket + hash prove the diary text was shown."""
    pv = c.post("/api/replies/preview", json=body)
    assert pv.status_code == 200, pv.text
    t = pv.json()
    r = c.post("/api/replies", json={**body, "ticket_id": t["ticket_id"], "payload_hash": t["payload_hash"], "accept_cost": True})
    assert r.status_code == 200, r.text
    return r.json()


def test_free_mode_returns_verified_drafts_and_credits_memories(app_client):
    c = app_client
    _seed(c)
    imp = c.post("/api/imports", json={"text": CHAT, "relationship_id": R1}).json()
    c.post(f"/api/imports/{imp['id']}/confirm", json={})

    out = _free(c, {"import_id": imp["id"], "relationship_id": R1, "styles": ["casual", "sincere"]})
    assert out["drafts"], "free mode should produce drafts"
    assert all(d["verified"] for d in out["drafts"])
    assert out["send_note"].startswith("这些只是草稿")
    assert out["memory_enabled"] is True
    casual = next(d for d in out["drafts"] if d["style"] == "casual")
    assert casual["memory_refs"], "the diary sentence used must be credited"
    assert casual["memory_refs"][0]["revision"] == 1


def test_memory_switch_off_produces_drafts_without_any_diary_material(app_client):
    c = app_client
    _seed(c)
    imp = c.post("/api/imports", json={"text": CHAT, "relationship_id": R1}).json()
    c.post(f"/api/imports/{imp['id']}/confirm", json={})

    out = _free(c, {"import_id": imp["id"], "relationship_id": R1, "memory_enabled": False})
    assert out["memory_enabled"] is False
    assert out["memories_used"] == []
    assert all(not d["memory_refs"] for d in out["drafts"])
    assert all(DIARY_TEXT not in d["text"] for d in out["drafts"]), "no diary text may appear when memory is off"


def test_disabled_diary_entry_never_reaches_a_draft(app_client):
    c = app_client
    _seed(c, enabled=False)
    imp = c.post("/api/imports", json={"text": CHAT, "relationship_id": R1}).json()
    c.post(f"/api/imports/{imp['id']}/confirm", json={})
    out = _free(c, {"import_id": imp["id"], "relationship_id": R1})
    assert all(DIARY_TEXT not in d["text"] for d in out["drafts"])
    assert out["dropped_memories"].get("disabled", 0) >= 1 or out["memories_used"] == []


def test_strict_mode_endpoint_calls_no_model(app_client):
    c = app_client
    _seed(c)
    imp = c.post("/api/imports", json={"text": CHAT, "relationship_id": R1}).json()
    c.post(f"/api/imports/{imp['id']}/confirm", json={})
    before = len(c.app_state.provider_override.calls)
    out = c.post("/api/replies", json={"import_id": imp["id"], "relationship_id": R1, "mode": "strict"}).json()
    assert len(c.app_state.provider_override.calls) == before, "strict mode must not call a model"
    assert out["drafts"] and out["drafts"][0]["mode"] == "strict_slot"


def test_scripted_fabrication_is_dropped_through_the_api(app_client):
    c = app_client
    _seed(c, enabled=False)  # stored but switched off
    imp = c.post("/api/imports", json={"text": CHAT, "relationship_id": R1}).json()
    c.post(f"/api/imports/{imp['id']}/confirm", json={})
    c.app_state.provider_override.scripted = [
        {"drafts": [{"style": "casual", "text": f"我{DIARY_TEXT}", "used_memory_ids": [], "why": "", "risk_notes": []}]}
    ]
    out = _free(c, {"import_id": imp["id"], "relationship_id": R1})
    assert out["drafts"] == [], "a draft quoting a switched-off entry must never be returned"
    assert out["abstain_reason"]


def test_reply_endpoint_validates_mode_and_import(app_client):
    c = app_client
    _rels(c)
    assert c.post("/api/replies", json={"relationship_id": "ghost"}).status_code == 404
    assert c.post("/api/replies", json={"mode": "telepathy"}).status_code == 400
    assert c.post("/api/replies", json={"import_id": "nope"}).status_code == 404
    # no context at all -> a clear business error, not a crash
    assert c.post("/api/replies", json={"relationship_id": R1}).status_code == 422
    # free mode with context but without a preview ticket must not reach the model
    imp = c.post("/api/imports", json={"text": CHAT, "relationship_id": R1}).json()
    c.post(f"/api/imports/{imp['id']}/confirm", json={})
    before = len(c.app_state.provider_override.calls)
    r = c.post("/api/replies", json={"import_id": imp["id"], "relationship_id": R1})
    assert r.status_code == 409 and r.json()["error"]["code"] == "preview_required"
    assert len(c.app_state.provider_override.calls) == before


def test_lexical_retrieval_does_not_find_unrelated_wording(app_client):
    """Documents a known limitation: retrieval is lexical until embeddings land."""
    c = app_client
    _rels(c)
    c.post("/api/diary", json={"text": "昨天去看了一场很晚的电影。", "allowed_relationships": [R1]})
    imp = c.post("/api/imports", json={"text": CHAT, "relationship_id": R1}).json()
    c.post(f"/api/imports/{imp['id']}/confirm", json={})
    out = _free(c, {"import_id": imp["id"], "relationship_id": R1})
    assert out["memories_used"] == [], "no wording overlap means no retrieval (lexical, not semantic)"
    assert out["drafts"], "drafts are still produced, just without diary material"
