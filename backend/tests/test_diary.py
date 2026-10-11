"""Phase 3: diary storage, memory authorisation and the strict fact slot (M09)."""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from rapport.config import reset_settings
from rapport.diary.authorize import (
    StrictFactError,
    authorize_memories,
    render_strict_reply,
    retrieve_if_enabled,
)
from rapport.schemas.diary import DiaryEntry, DiaryKind, MemoryHit
from rapport.state import AppState

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
R1, R2 = "rel-1", "rel-2"


def entry(
    eid: str = "d1",
    *,
    revision: int = 1,
    text: str = "今天下午去了公园，天气很好。",
    kind: DiaryKind = DiaryKind.fact,
    enabled: bool = True,
    relationships: frozenset[str] = frozenset({R1}),
    recorded_at: datetime = NOW - timedelta(hours=2),
    occurred_at: datetime | None = None,
    approved_clause: str | None = None,
    deleted: bool = False,
) -> DiaryEntry:
    return DiaryEntry(
        id=eid,
        revision=revision,
        text=text,
        kind=kind,
        enabled=enabled,
        allowed_relationships=relationships,
        recorded_at=recorded_at,
        occurred_at=occurred_at,
        approved_clause=approved_clause,
        deleted=deleted,
    )


def pick(entries, hits=None, *, relationship=R1, as_of=NOW, enabled=True, limit=3):
    store = {e.id: e for e in entries}
    hs = hits if hits is not None else [MemoryHit(entry_id=e.id, revision=e.revision, similarity=1.0) for e in entries]
    return authorize_memories(hs, store, relationship, as_of, enabled, limit)


# ---------------------------------------------------------------- authorisation


def test_allowed_entry_passes():
    result = pick([entry()])
    assert len(result.entries) == 1 and result.all_dropped == 0


def test_switch_off_prevents_retrieval_entirely():
    calls: list[int] = []

    def retriever():
        calls.append(1)
        return [MemoryHit(entry_id="d1", revision=1, similarity=1.0)]

    assert retrieve_if_enabled(False, retriever) == []
    assert calls == [], "a closed switch must not touch the index at all"
    assert retrieve_if_enabled(True, retriever) and calls == [1]
    assert pick([entry()], enabled=False).entries == []


def test_disabled_deleted_and_stale_entries_are_dropped():
    assert pick([entry(enabled=False)]).dropped == {"disabled": 1}
    assert pick([entry(deleted=True)]).dropped == {"deleted": 1}
    stale = pick([entry(revision=2)], hits=[MemoryHit(entry_id="d1", revision=1, similarity=1.0)])
    assert stale.dropped == {"stale_revision": 1}


def test_scope_isolation_and_empty_scope():
    assert pick([entry(relationships=frozenset({R2}))]).dropped == {"outside_scope": 1}
    assert pick([entry(relationships=frozenset())]).dropped == {"outside_scope": 1}
    assert pick([entry(relationships=frozenset({R1, R2}))], relationship=R2).entries


def test_future_diary_and_future_fact_are_dropped_but_plans_survive():
    future_diary = entry(recorded_at=NOW + timedelta(days=1))
    assert pick([future_diary]).dropped == {"future_diary": 1}

    future_fact = entry(occurred_at=NOW + timedelta(days=1), kind=DiaryKind.fact)
    assert pick([future_fact]).dropped == {"future_fact": 1}

    # a plan pointing at the future is legitimate — and must stay a plan
    plan = entry(kind=DiaryKind.plan, occurred_at=NOW + timedelta(days=3), text="周六想去看展。", approved_clause="周六想去看展。")
    result = pick([plan])
    assert len(result.entries) == 1 and result.entries[0].kind == DiaryKind.plan
    rendered = render_strict_reply(plan, R1, NOW)
    assert "计划" in rendered["text"] and "周六想去看展。" in rendered["text"]
    assert rendered["memory_refs"][0]["kind"] == "plan"


def test_duplicate_hits_and_zero_limit():
    e = entry()
    dupes = [MemoryHit(entry_id="d1", revision=1, similarity=1.0), MemoryHit(entry_id="d1", revision=1, similarity=0.9)]
    result = pick([e], hits=dupes)
    assert len(result.entries) == 1 and result.dropped == {"duplicate": 1}
    assert pick([e], limit=0).entries == []


def test_limit_is_respected_and_similarity_ordered():
    entries = [entry("d1"), entry("d2"), entry("d3")]
    hits = [
        MemoryHit(entry_id="d1", revision=1, similarity=0.2),
        MemoryHit(entry_id="d2", revision=1, similarity=0.9),
        MemoryHit(entry_id="d3", revision=1, similarity=0.5),
    ]
    result = pick(entries, hits=hits, limit=2)
    assert [e.id for e in result.entries] == ["d2", "d3"]


def test_missing_entry_and_non_finite_similarity():
    assert pick([], hits=[MemoryHit(entry_id="gone", revision=1, similarity=1.0)]).dropped == {"not_found": 1}
    with pytest.raises(ValueError):
        pick([entry()], hits=[MemoryHit(entry_id="d1", revision=1, similarity=float("nan"))])


def test_timezone_awareness_is_required():
    with pytest.raises(ValueError):
        DiaryEntry(id="d1", text="x", recorded_at=datetime(2026, 9, 15, 12, 0))  # naive
    with pytest.raises(ValueError):
        pick([entry()], as_of=datetime(2026, 9, 15, 12, 0))  # naive as_of


# ---------------------------------------------------------------- strict fact slot


def test_strict_reply_uses_only_the_approved_clause():
    e = entry(text="今天下午去了公园，天气很好。", approved_clause="今天下午去了公园，天气很好。")
    out = render_strict_reply(e, R1, NOW)
    assert out["text"].endswith(e.approved_clause)
    assert out["mode"] == "strict_slot" and out["requires_user_review"] is True
    assert "不会改写" in out["note"]


def test_invented_or_unapproved_clause_is_rejected():
    e = entry(text="今天下午去了公园。", approved_clause=None)
    with pytest.raises(StrictFactError):
        render_strict_reply(e, R1, NOW)

    swapped = entry(text="今天下午去了公园。", approved_clause="今天下午去爬山了。")
    with pytest.raises(StrictFactError):
        render_strict_reply(swapped, R1, NOW)


def test_strict_reply_refuses_out_of_scope_or_stale_entries():
    e = entry(approved_clause="今天下午去了公园，天气很好。", relationships=frozenset({R2}))
    with pytest.raises(StrictFactError):
        render_strict_reply(e, R1, NOW)


# ---------------------------------------------------------------- storage + API


@pytest.fixture
def state():
    yield AppState(reset_settings(data_dir=Path(tempfile.mkdtemp()), pairing_code="123456"))


def _create(st: AppState, text: str = "今天下午去了公园，天气很好。", **kw) -> DiaryEntry:
    e = entry("d-" + str(abs(hash(text)) % 10**6), text=text, **kw)
    return st.diary_create(e)


def test_edit_bumps_revision_and_invalidates_the_clause_it_no_longer_contains(state):
    e = _create(state, "今天下午去了公园，天气很好。", approved_clause="天气很好。")
    assert st_get(state, e.id).revision == 1
    updated = state.diary_update(e.id, {"text": "今天下午加班到很晚。"})
    assert updated.revision == 2
    assert updated.approved_clause is None, "a clause the new text does not contain cannot stay approved"
    # a hit built from revision 1 is now stale
    dropped = state.diary_search("公园", R1, True, as_of=NOW)
    assert dropped["entries"] == []


def st_get(st: AppState, eid: str) -> DiaryEntry:
    got = st.diary_get(eid)
    assert got is not None
    return got


def test_approved_clause_must_exist_verbatim(state):
    e = _create(state, "今天下午去了公园。")
    with pytest.raises(ValueError):
        state.diary_update(e.id, {"approved_clause": "今天下午去了海边。"})
    ok = state.diary_update(e.id, {"approved_clause": "今天下午去了公园。"})
    assert ok.approved_clause == "今天下午去了公园。"


def test_delete_and_clear_make_entries_unusable_immediately(state):
    e1 = _create(state, "今天下午去了公园。")
    e2 = _create(state, "晚上和朋友吃了火锅。")
    assert state.diary_get(e2.id) is not None
    assert state.diary_delete(e1.id)
    assert state.diary_get(e1.id) is None
    assert state.diary_search("公园", R1, True, as_of=NOW)["entries"] == []
    assert state.diary_clear() == 1
    assert state.diary_list() == []
    assert state.diary_search("火锅", R1, True, as_of=NOW)["entries"] == []


def test_search_returns_authorised_entries_and_reports_dropped(state):
    _create(state, "今天下午去了公园，天气很好。")
    _create(state, "周末想去爬山，还没定。", kind=DiaryKind.plan)
    result = state.diary_search("公园 天气", R1, True, as_of=NOW)
    assert result["enabled"] is True and result["entries"]
    assert all(set(e["allowed_relationships"]) == {R1} for e in result["entries"])
    assert "回查当前状态" in result["note"]

    off = state.diary_search("公园 天气", R1, False, as_of=NOW)
    assert off["entries"] == [] and off["enabled"] is False


def test_memory_switch_defaults_on_and_persists(state):
    assert state.memory_enabled_by_default() is True
    state.set_memory_enabled(False)
    assert state.memory_enabled_by_default() is False


def test_diary_api_round_trip(app_client):
    c = app_client
    created = c.post("/api/diary", json={"text": "今天下午去了公园，天气很好。", "allowed_relationships": [R1]}).json()
    eid = created["id"]
    assert created["revision"] == 1

    assert len(c.get("/api/diary").json()["entries"]) == 1
    assert c.get(f"/api/diary/{eid}").json()["text"].startswith("今天下午")

    patched = c.patch(f"/api/diary/{eid}", json={"text": "今天下午去了公园，顺便买了花。"}).json()
    assert patched["revision"] == 2

    hits = c.get("/api/diary/search", params={"q": "公园", "relationship_id": R1}).json()
    assert hits["entries"] and hits["entries"][0]["revision"] == 2
    off = c.get("/api/diary/search", params={"q": "公园", "relationship_id": R1, "enabled": "false"}).json()
    assert off["entries"] == []

    assert c.post(f"/api/diary/{eid}/enabled", params={"enabled": "false"}).json()["enabled"] is False
    assert c.get("/api/diary/search", params={"q": "公园", "relationship_id": R1}).json()["entries"] == []

    assert c.delete(f"/api/diary/{eid}").status_code == 200
    assert c.get(f"/api/diary/{eid}").status_code == 404
    assert c.get("/api/diary").json()["entries"] == []


def test_diary_api_rejects_naive_time_and_bad_clause(app_client):
    c = app_client
    bad_time = c.post("/api/diary", json={"text": "x", "recorded_at": "2026-09-15T12:00:00"})
    assert bad_time.status_code == 422
    e = c.post("/api/diary", json={"text": "今天做了饭。"}).json()
    assert c.patch(f"/api/diary/{e['id']}", json={"approved_clause": "今天做了火锅。"}).status_code == 422
    assert c.post(f"/api/diary/{e['id']}/strict-reply").status_code == 422  # no approved clause yet


def test_strict_reply_endpoint_after_approval(app_client):
    c = app_client
    e = c.post("/api/diary", json={"text": "今天下午去了公园，天气很好。", "allowed_relationships": [R1]}).json()
    c.patch(f"/api/diary/{e['id']}", json={"approved_clause": "今天下午去了公园，天气很好。"})
    out = c.post(f"/api/diary/{e['id']}/strict-reply", params={"relationship_id": R1}).json()
    assert out["text"].endswith("今天下午去了公园，天气很好。")
    assert out["requires_user_review"] is True
