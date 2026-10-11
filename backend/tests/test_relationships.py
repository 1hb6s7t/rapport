"""v0.4: 关系档案 scoping — two people never share evidence, history or feedback."""

from __future__ import annotations

from rapport.db import DEFAULT_RELATIONSHIP_ID, Event, Import, ReportRow, Snapshot

CHAT_A = """[2026-08-20 20:00] A: 今天好累
[2026-08-20 20:10] B: 辛苦了，早点休息
[2026-08-21 09:00] B: 早呀
[2026-08-21 09:30] A: 早
[2026-08-22 21:00] B: 周末一起吃个饭？
[2026-08-22 21:05] A: 好呀
[2026-08-23 20:00] B: 在干嘛
[2026-08-23 20:05] A: 在看书
[2026-08-23 20:06] B: 什么书，推荐一下
"""
CHAT_B = CHAT_A.replace("B: 辛苦了，早点休息", "B: 嗯").replace("B: 周末一起吃个饭？", "B: 忙")


def _rel(c, rid: str, name: str = "someone") -> dict:
    r = c.post("/api/relationships", json={"id": rid, "name": name})
    assert r.status_code == 200, r.text
    return r.json()


def _run(c, text: str, rid: str | None = None) -> str:
    body = {"text": text}
    if rid:
        body["relationship_id"] = rid
    imp = c.post("/api/imports", json=body).json()
    assert imp["relationship_id"] == (rid or DEFAULT_RELATIONSHIP_ID)
    c.post(f"/api/imports/{imp['id']}/confirm", json={})
    a = c.post("/api/analyses", json={"import_id": imp["id"], "mode": "rules"}).json()
    c.app_state.runner.run_pending_once()
    return c.get(f"/api/analyses/{a['id']}").json()["report_id"]


def test_default_relationship_exists_and_cannot_be_deleted(app_client):
    c = app_client
    rows = c.get("/api/relationships").json()["relationships"]
    assert [r["id"] for r in rows] == [DEFAULT_RELATIONSHIP_ID]
    assert rows[0]["is_default"]
    assert c.delete(f"/api/relationships/{DEFAULT_RELATIONSHIP_ID}").status_code == 409


def test_unknown_relationship_is_refused_not_silently_created(app_client):
    c = app_client
    r = c.post("/api/imports", json={"text": CHAT_A, "relationship_id": "ghost"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "relationship_not_found"
    assert [x["id"] for x in c.get("/api/relationships").json()["relationships"]] == [DEFAULT_RELATIONSHIP_ID]


def test_create_validates_labels_and_rejects_duplicates(app_client):
    c = app_client
    r = c.post("/api/relationships", json={"id": "amy", "name": "Amy", "labels": {"mbti": "entj", "zodiac": "leo"}})
    assert r.status_code == 200
    assert r.json()["labels"] == {"mbti": "ENTJ", "zodiac": "leo", "chinese_zodiac": None}
    assert c.post("/api/relationships", json={"id": "amy", "name": "again"}).status_code == 409
    assert c.post("/api/relationships", json={"name": "bad", "labels": {"mbti": "XXXX"}}).status_code == 422


def test_evidence_history_and_reports_are_isolated_per_relationship(app_client):
    c = app_client
    st = c.app_state
    _rel(c, "amy")
    _rel(c, "ben")
    rid_a = _run(c, CHAT_A, "amy")
    rid_b = _run(c, CHAT_B, "ben")
    _run(c, CHAT_A + "[2026-08-24 20:00] B: 明天见\n", "amy")  # new material for amy: her history grows, ben's does not

    assert st.ledger_n_eff("amy") > 0 and st.ledger_n_eff("ben") > 0
    assert st.ledger_n_eff(DEFAULT_RELATIONSHIP_ID) == 0.0, "nothing went to the default scope"
    assert st.ledger_n_eff("amy") != st.ledger_n_eff("amy") + st.ledger_n_eff("ben")

    trend_a = c.get("/api/trend", params={"relationship_id": "amy"}).json()
    trend_b = c.get("/api/trend", params={"relationship_id": "ben"}).json()
    assert {p["report_id"] for p in trend_a["points"]} >= {rid_a}
    assert [p["report_id"] for p in trend_b["points"]] == [rid_b]
    assert c.get("/api/trend").json()["points"] == [], "the default scope stays empty"

    stages_b = c.get("/api/stage/history", params={"relationship_id": "ben"}).json()["points"]
    assert [p["report_id"] for p in stages_b] == [rid_b]

    reports_a = c.get("/api/reports", params={"relationship_id": "amy"}).json()
    assert all(r["relationship_id"] == "amy" for r in reports_a) and len(reports_a) == 2

    overview = c.get("/api/relationships/amy/overview").json()
    assert overview["relationship"]["counts"]["reports"] == 2
    assert overview["n_eff"] == st.ledger_n_eff("amy")


def test_same_untimed_line_from_two_people_does_not_share_a_ledger_row(app_client):
    c = app_client
    st = c.app_state
    _rel(c, "amy")
    _rel(c, "ben")
    untimed = "A: 早\nB: 早\nA: 吃了吗\nB: 吃了\n"
    for rid in ("amy", "ben"):
        imp = c.post("/api/imports", json={"text": untimed, "relationship_id": rid}).json()
        c.post(f"/api/imports/{imp['id']}/confirm", json={})
    with st.db.session() as s:
        by_rel = {}
        for e in s.query(Event).all():
            by_rel.setdefault(e.relationship_id, 0)
            by_rel[e.relationship_id] += 1
    assert by_rel.get("amy") == by_rel.get("ben") == 4, by_rel


def test_deleting_a_relationship_cascades_and_detaches_diary_scope(app_client):
    c = app_client
    st = c.app_state
    _rel(c, "amy")
    rid = _run(c, CHAT_A, "amy")
    c.post(f"/api/reports/{rid}/feedback", json={"target_kind": "stage", "verdict": "agree"})
    d = c.post("/api/diary", json={"text": "上周一起去看了展。", "allowed_relationships": ["amy", "other"]}).json()

    out = c.delete("/api/relationships/amy").json()
    assert out["ok"] and out["cleared"]["reports"] == 1 and out["cleared"]["feedback"] == 1
    assert out["cleared"]["diary_detached"] == 1
    assert c.get("/api/relationships/amy").status_code == 404
    assert c.get(f"/api/reports/{rid}").status_code == 404
    with st.db.session() as s:
        assert s.query(Import).filter(Import.relationship_id == "amy").count() == 0
        assert s.query(ReportRow).filter(ReportRow.relationship_id == "amy").count() == 0
        assert s.query(Snapshot).filter(Snapshot.relationship_id == "amy").count() == 0
        assert s.query(Event).filter(Event.relationship_id == "amy").count() == 0
    assert c.get(f"/api/diary/{d['id']}").json()["allowed_relationships"] == ["other"]
    assert not list(st.settings.blob_dir.glob("*.bin")), "the raw material of a deleted relationship is gone"


def test_update_archive_and_listing(app_client):
    c = app_client
    _rel(c, "amy")
    r = c.patch("/api/relationships/amy", json={"name": "Amy L.", "note": "同事", "archived": True}).json()
    assert r["name"] == "Amy L." and r["archived"] is True
    assert [x["id"] for x in c.get("/api/relationships").json()["relationships"]] == [DEFAULT_RELATIONSHIP_ID]
    assert "amy" in [x["id"] for x in c.get("/api/relationships", params={"include_archived": True}).json()["relationships"]]
    # the default relationship cannot be archived away
    assert c.patch(f"/api/relationships/{DEFAULT_RELATIONSHIP_ID}", json={"archived": True}).json()["archived"] is False


def test_wipe_keeps_only_the_default_relationship(app_client):
    c = app_client
    _rel(c, "amy")
    _run(c, CHAT_A, "amy")
    out = c.delete("/api/data").json()
    assert out["cleared"]["relationships"] == 1
    assert [x["id"] for x in c.get("/api/relationships").json()["relationships"]] == [DEFAULT_RELATIONSHIP_ID]
