"""Phase 2: neutral shrinkage (M05) and real cross-import snapshots (FR-010)."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from rapport.config import reset_settings
from rapport.db import Snapshot
from rapport.jobs.pipeline import run_rules
from rapport.parsers import ParseOptions, parse
from rapport.schemas.profile import Profile
from rapport.scoring.affinity import NEFF_FULL, NEUTRAL, provisional_total, shrinkage
from rapport.state import AppState

CHAT = """[2026-08-20 20:00] A: 今天好累
[2026-08-20 20:10] B: 辛苦了，早点休息
[2026-08-21 09:00] B: 早呀
[2026-08-21 09:30] A: 早
[2026-08-22 21:00] B: 周末一起吃个饭？
[2026-08-22 21:05] A: 好呀
[2026-08-23 20:00] B: 在干嘛
[2026-08-23 20:05] A: 在看书
[2026-08-23 20:06] B: 什么书，推荐一下
"""


# ---------------------------------------------------------------- shrinkage


def test_shrinkage_full_at_zero_evidence_and_gone_at_the_target():
    assert shrinkage(0) == pytest.approx(1.0)
    assert shrinkage(NEFF_FULL) == pytest.approx(0.0)
    assert shrinkage(NEFF_FULL * 3) == pytest.approx(0.0), "never negative"
    assert shrinkage(NEFF_FULL / 2) == pytest.approx(0.5)


def test_shrinkage_is_monotone():
    values = [shrinkage(n) for n in range(0, 40)]
    assert all(b <= a for a, b in zip(values, values[1:], strict=False))


def test_provisional_pulls_toward_neutral_then_releases():
    assert provisional_total(80.0, 1.0) == int(NEUTRAL), "no evidence → fully neutral"
    assert provisional_total(80.0, 0.0) == 80, "enough evidence → the observed score"
    assert provisional_total(80.0, 0.5) == 65


def test_report_exposes_rho_and_provisional_without_changing_the_headline():
    conv, _ = parse(CHAT, "c1", "paste", None, ParseOptions())
    fresh = run_rules(conv, conv.events(), Profile(), n_eff=0.0)
    settled = run_rules(conv, conv.events(), Profile(), n_eff=NEFF_FULL)
    assert fresh.affinity.rho == pytest.approx(1.0) and settled.affinity.rho == pytest.approx(0.0)
    if fresh.affinity.total is not None:
        assert fresh.affinity.provisional_total == int(NEUTRAL)
        assert settled.affinity.provisional_total == settled.affinity.total
        assert fresh.affinity.total == settled.affinity.total, "rho must not move the headline score"
    assert "实验性" in fresh.affinity.provisional_note


# ---------------------------------------------------------------- snapshots


@pytest.fixture
def state():
    yield AppState(reset_settings(data_dir=Path(tempfile.mkdtemp()), pairing_code="123456"))


def _import(st: AppState, text: str, cid: str):
    conv, pid = parse(text, cid, "paste", None, ParseOptions())
    imp = st.save_import(conv, pid, None)
    st.confirm_events(imp.id)
    return imp, conv


def test_same_material_and_versions_write_one_point(state):
    imp1, conv1 = _import(state, CHAT, "c1")
    rep1 = run_rules(conv1, conv1.events(), Profile(), report_id="r1")
    assert state.save_snapshot(rep1, imp1.id, "hash-a", 3.0) is not None

    imp2, conv2 = _import(state, CHAT, "c2")  # identical material re-imported
    rep2 = run_rules(conv2, conv2.events(), Profile(), report_id="r2")
    assert state.save_snapshot(rep2, imp2.id, "hash-a", 3.0) is None, "no duplicate history point"
    with state.db.session() as s:
        assert s.query(Snapshot).count() == 1


def test_new_material_adds_a_point_and_keeps_n_eff(state):
    imp1, conv1 = _import(state, CHAT, "c1")
    rep1 = run_rules(conv1, conv1.events(), Profile(), report_id="r1")
    state.save_snapshot(rep1, imp1.id, "hash-a", 4.0)

    extended = CHAT + "\n[2026-08-24 20:00] B: 明天见\n"
    imp2, conv2 = _import(state, extended, "c2")
    rep2 = run_rules(conv2, conv2.events(), Profile(), report_id="r2")
    state.save_snapshot(rep2, imp2.id, "hash-b", 9.0)

    series = state.trend_series()
    assert len(series["points"]) == 2
    assert [p["n_eff"] for p in series["points"]] == [4.0, 9.0]
    assert series["points"][0]["created_at"] <= series["points"][1]["created_at"]
    assert "断点" in series["note"]


def test_version_change_breaks_the_line(state):
    imp, conv = _import(state, CHAT, "c1")
    rep = run_rules(conv, conv.events(), Profile(), report_id="r1")
    state.save_snapshot(rep, imp.id, "hash-a", 4.0)

    rep2 = run_rules(conv, conv.events(), Profile(), report_id="r2")
    rep2.versions.rubric = "affinity-rubric-v0.3"  # a rule change must be visible, not silent
    state.save_snapshot(rep2, imp.id, "hash-a", 4.0)

    points = state.trend_series()["points"]
    assert len(points) == 2
    assert points[0]["comparable"] is False and points[1]["comparable"] is True


def test_snapshot_records_dims_and_flags_boundary_points(state):
    imp, conv = _import(state, CHAT + "\n[2026-08-25 20:00] B: 别再联系我了\n", "c1")
    rep = run_rules(conv, conv.events(), Profile(), report_id="r1")
    state.save_snapshot(rep, imp.id, "hash-a", 4.0)
    p = state.trend_series()["points"][0]
    assert p["note"] == "boundary", "the point is flagged so the trend can mark it"
    assert p["total"] is not None, "v0.8: a boundary report still contributes its number"
    assert set(p["dims"]) == {"responsiveness", "initiative", "investment", "warmth", "future"}


def test_runner_writes_a_snapshot_per_completed_analysis(app_client):
    c = app_client
    st = c.app_state
    assert c.get("/api/trend").json()["points"] == []

    imp = c.post("/api/imports", json={"text": CHAT}).json()
    c.post(f"/api/imports/{imp['id']}/confirm", json={})
    a = c.post("/api/analyses", json={"import_id": imp["id"], "mode": "rules"}).json()
    st.runner.run_pending_once()

    series = c.get("/api/trend").json()
    assert len(series["points"]) == 1
    point = series["points"][0]
    assert point["report_id"] == c.get(f"/api/analyses/{a['id']}").json()["report_id"]
    assert point["n_eff"] > 0, "the confirmed import is what makes this a real point"
    assert point["comparable"] is True


def test_confirming_an_import_is_what_creates_evidence(app_client):
    c = app_client
    st = c.app_state
    imp = c.post("/api/imports", json={"text": CHAT}).json()
    assert st.ledger_n_eff() == 0.0
    c.post(f"/api/imports/{imp['id']}/confirm", json={})
    assert st.ledger_n_eff() > 0.0


# ---------------------------------------------------------------- version derivation


def test_changing_a_scoring_parameter_changes_the_rubric_digest(monkeypatch):
    """The version string is derived from the parameters themselves, so an edited anchor or weight
    cannot be silently wired onto an existing history line (plan §1 D)."""
    from rapport.scoring import rubric

    before = rubric.rubric_digest()
    monkeypatch.setattr(rubric, "COVERAGE_MIN", rubric.COVERAGE_MIN + 0.01)
    assert rubric.rubric_digest() != before


def test_changing_a_lexicon_pattern_changes_the_features_digest(monkeypatch):
    import re

    from rapport.features import lexicon

    before = lexicon.lexicon_digest()
    monkeypatch.setattr(lexicon, "CONFLICT_RE", re.compile("这个正则只存在于测试里"))
    assert lexicon.lexicon_digest() != before


def test_a_rule_change_rebinds_the_version_strings(monkeypatch):
    from rapport import config
    from rapport.scoring import rubric

    before = config.RUBRIC_VERSION
    monkeypatch.setattr(config, "RUBRIC_VERSION", before)  # restored on teardown
    monkeypatch.setattr(rubric, "COVERAGE_MIN", rubric.COVERAGE_MIN + 0.01)
    config._bind_scoring_versions()
    assert config.RUBRIC_VERSION != before, "改了规则而版本串不动，历史曲线就会跨规则连线"
    assert config.RUBRIC_VERSION.startswith("affinity-rubric-v0.2+")
