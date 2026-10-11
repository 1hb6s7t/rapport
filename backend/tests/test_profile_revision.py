"""Durable, scoped profile metadata and report-cache boundaries."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from rapport.db import Analysis
from rapport.schemas.profile import Birthday, Person, Profile
from tests.conftest import demo_text


@pytest.mark.parametrize("data,precision", [
    ({}, "unknown"), ({"year": 1998}, "year"),
    ({"year": 1998, "month": 2}, "year_month"),
    ({"month": 2, "day": 29}, "month_day"),
    ({"year": 2000, "month": 2, "day": 29}, "day"),
    ({"calendar": "lunar", "month": 6, "day": 30, "is_leap_month": True}, "month_day"),
])
def test_partial_birthday_preserves_unknowns(data, precision):
    birthday = Birthday(**data)
    assert birthday.precision == precision
    assert Birthday.model_validate(birthday.model_dump()) == birthday
    person = Person(birthday=birthday)
    assert person.zodiac is None and person.chinese_zodiac is None


@pytest.mark.parametrize("data", [
    {"month": 5}, {"day": 10}, {"year": 2000, "day": 3},
    {"year": 2001, "month": 2, "day": 29}, {"month": 4, "day": 31},
    {"year": 1998, "precision": "day"}, {"year": 9999},
    {"calendar": "lunar", "month": 6, "day": 31},
    {"month": 2, "day": 1, "is_leap_month": True},
])
def test_invalid_dates_are_not_silently_normalized(data):
    with pytest.raises(ValidationError):
        Birthday(**data)


def test_self_and_friend_profile_are_durable_scoped_and_revisioned(app_client):
    c = app_client
    c.post("/api/relationships", json={"id": "a", "name": "朋友 A"})
    c.post("/api/relationships", json={"id": "b", "name": "朋友 B"})
    self_data = {"alias": "本人", "mbti": "infp", "birthday": {"year": 1998}, "source": "user", "confirmed": True}
    own = c.put("/api/relationships/self/profile", json=self_data)
    assert own.status_code == 200, own.text
    assert own.json()["revision"] == 1
    friend_data = {"alias": "朋友 A", "birthday": {"calendar": "lunar", "month": 3, "day": 5}, "source": "user"}
    friend = c.put("/api/relationships/a/profile", json=friend_data).json()
    assert friend["revision"] == 1
    assert c.get("/api/relationships/a/profile").json() == friend
    assert c.get("/api/relationships/b/profile").json()["birthday"] is None
    assert c.get("/api/relationships/self/profile").json() == own.json()
    # The new profile lives in existing durable JSON storage, not process-only state.
    from rapport.state import AppState

    reloaded = AppState(c.app_state.settings)
    assert reloaded.relationship_person("a").birthday.calendar == "lunar"
    assert reloaded.self_profile().birthday.year == 1998
    assert c.put("/api/relationships/a/profile", json=friend).json()["revision"] == 1
    assert c.put("/api/relationships/a/profile", json=friend | {"revision": 0}).status_code == 409
    cleared = c.put("/api/relationships/a/profile", json=friend | {"birthday": None}).json()
    assert cleared["revision"] == 2 and cleared["birthday"] is None


def test_legacy_labels_do_not_erase_birthday_metadata(app_client):
    c = app_client
    c.put("/api/relationships/default/profile", json={"birthday": {"year": 2000}, "confirmed": True})
    old_revision = c.get("/api/relationships/default/profile").json()["revision"]
    updated = c.patch("/api/relationships/default", json={"labels": {"mbti": "ENTJ", "zodiac": "leo"}})
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert body["labels"] == {"mbti": "ENTJ", "zodiac": "leo", "chinese_zodiac": None}
    assert body["person"]["birthday"]["year"] == 2000
    assert body["profile_revision"] == old_revision + 1


def _import(c, rid="default"):
    response = c.post("/api/imports", json={"text": demo_text(), "relationship_id": rid})
    assert response.status_code == 200, response.text
    iid = response.json()["id"]
    assert c.post(f"/api/imports/{iid}/confirm", json={}).status_code == 200
    return iid


def test_cache_uses_scoped_person_snapshot_while_core_score_excludes_labels(app_client):
    c = app_client
    c.post("/api/relationships", json={"id": "b", "name": "B"})
    iid = _import(c)
    first = c.post("/api/analyses", json={"import_id": iid}).json()
    assert c.post("/api/analyses", json={"import_id": iid}).json()["id"] == first["id"]
    c.app_state.runner.run_pending_once()
    report_a = c.get(f"/api/reports/{c.get('/api/analyses/' + first['id']).json()['report_id']}").json()
    friend = c.put("/api/relationships/default/profile", json={"mbti": "ENTJ", "birthday": {"year": 1992}}).json()
    second = c.post("/api/analyses", json={"import_id": iid}).json()
    assert second["id"] != first["id"]
    with c.app_state.db.session() as session:
        snapshot = session.get(Analysis, second["id"]).profile
    assert snapshot["other"]["birthday"]["year"] == 1992
    assert snapshot["other"]["revision"] == friend["revision"]
    c.app_state.runner.run_pending_once()
    report_b = c.get(f"/api/reports/{c.get('/api/analyses/' + second['id']).json()['report_id']}").json()
    assert report_a["affinity"] == report_b["affinity"]
    b_iid = _import(c, "b")
    third = c.post("/api/analyses", json={"import_id": b_iid}).json()
    assert third["id"] not in {first["id"], second["id"]}
    with c.app_state.db.session() as session:
        assert session.get(Analysis, third["id"]).profile["other"]["mbti"] is None


def test_profile_hash_and_outbound_fields_keep_objective_features_separate():
    first = Profile()
    second = Profile(other=Person(mbti="ENTJ", birthday=Birthday(year=1995), revision=4))
    assert first.core_hash() == second.core_hash()
    assert first.cache_hash() != second.cache_hash()


def test_stale_snapshot_is_rejected_without_overwriting_profile(app_client):
    c = app_client
    iid = _import(c)
    c.put("/api/relationships/default/profile", json={"mbti": "ENTJ"})
    res = c.post("/api/analyses", json={"import_id": iid, "profile": {"other": {"mbti": "INTJ", "revision": 0}}})
    assert res.status_code == 409
    assert c.get("/api/relationships/default/profile").json()["mbti"] == "ENTJ"


def test_profile_endpoints_require_pairing_and_csrf(app_client):
    c = app_client
    assert c.put("/api/relationships/self/profile", json={}, headers={"x-rapport-csrf": "wrong"}).status_code == 403
    c.cookies.clear()
    assert c.get("/api/relationships/self/profile").status_code == 401
