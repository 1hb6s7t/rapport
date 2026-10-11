"""Phase 4: the stage ladder — no skipped rungs, 稳定 needs durability, regressions are recorded."""

from __future__ import annotations

from datetime import datetime, timedelta

from rapport.features import compute_features
from rapport.parsers import ParseOptions, parse
from rapport.schemas.profile import Profile
from rapport.schemas.report_v1 import STAGE_LADDER, Boundary, BoundaryValue, Stage, StageValue
from rapport.scoring.stage import (
    MIN_STABLE_DAYS,
    MIN_STABLE_SESSIONS,
    evaluate_transition,
    meets_stable_requirements,
)


def _features(messages: list[tuple[int, str, str]]):
    """messages: (day offset, sender A|B, text)."""
    base = datetime(2026, 7, 1, 20, 0)
    lines = []
    for day, who, text in messages:
        ts = base + timedelta(days=day)
        lines.append(f"[{ts:%Y-%m-%d %H:%M}] {who}: {text}")
    conv, _ = parse("\n".join(lines), "c1", "paste", None, ParseOptions())
    return compute_features(conv, conv.events())


def _durable(days: int = 30, sessions: int = 10) -> list[tuple[int, str, str]]:
    out: list[tuple[int, str, str]] = []
    step = max(1, days // sessions)
    for i in range(sessions):
        d = i * step
        out.append((d, "A", "今天过得怎么样"))
        out.append((d, "B", "挺好的，你呢"))
    out.append((0, "A", "我们在一起吧"))
    out.append((0, "B", "好啊，我们在一起"))
    return out


def _proposed(value: StageValue, basis: list[str] | None = None) -> Stage:
    return Stage(value=value, basis_ids=basis or ["m000001"])


# ---------------------------------------------------------------- ladder rules


def test_first_stage_has_no_transition_record():
    f = _features(_durable())
    out = evaluate_transition(None, _proposed(StageValue.getting_to_know), f, Boundary())
    assert out.value == StageValue.getting_to_know and out.change is None


def test_one_rung_advance_is_allowed_and_recorded():
    f = _features(_durable())
    out = evaluate_transition(StageValue.initial_contact.value, _proposed(StageValue.getting_to_know), f, Boundary())
    assert out.value == StageValue.getting_to_know
    assert out.change and out.change.direction == "advance"
    assert out.change.before == "initial_contact" and out.change.after == "getting_to_know"
    assert out.change.basis_ids == ["m000001"]


def test_skipping_a_rung_is_downgraded_to_the_next_one():
    f = _features(_durable())
    out = evaluate_transition(StageValue.initial_contact.value, _proposed(StageValue.mutually_confirmed), f, Boundary())
    assert out.value == StageValue.getting_to_know, "you cannot jump from 陌生 to 确定关系"
    assert out.change and "不允许跳级" in out.change.reason


def test_regression_is_recorded_rather_than_hidden():
    f = _features(_durable())
    out = evaluate_transition(StageValue.mutually_confirmed.value, _proposed(StageValue.getting_to_know), f, Boundary())
    assert out.value == StageValue.getting_to_know
    assert out.change and out.change.direction == "regress"
    assert "回退" in out.note


def test_same_stage_produces_no_change():
    f = _features(_durable())
    out = evaluate_transition(StageValue.getting_to_know.value, _proposed(StageValue.getting_to_know), f, Boundary())
    assert out.change is None


def test_off_ladder_values_do_not_crash():
    f = _features(_durable())
    for value in (StageValue.unknown, StageValue.repair):
        out = evaluate_transition(StageValue.getting_to_know.value, _proposed(value), f, Boundary())
        assert out.value == value


# ---------------------------------------------------------------- 稳定 requirements


def test_stable_needs_confirmation_sessions_span_and_no_boundary():
    f = _features(_durable())
    confirmed = _proposed(StageValue.stable)
    ok, why = meets_stable_requirements(f, Stage(value=StageValue.mutually_confirmed), Boundary())
    assert ok, why

    # not confirmed
    ok, why = meets_stable_requirements(f, Stage(value=StageValue.getting_to_know), Boundary())
    assert not ok and "确认关系" in why

    # v0.8: a boundary no longer blocks 稳定 (it is a note on the report, not a stage constraint)
    ok, _why = meets_stable_requirements(f, Stage(value=StageValue.mutually_confirmed), Boundary(value=BoundaryValue.stop_contact))
    assert ok

    # too few sessions
    small = _features(_durable(days=40, sessions=3))
    ok, why = meets_stable_requirements(small, Stage(value=StageValue.mutually_confirmed), Boundary())
    assert not ok and str(MIN_STABLE_SESSIONS) in why

    # too short a span
    short = _features(_durable(days=5, sessions=10))
    ok, why = meets_stable_requirements(short, Stage(value=StageValue.mutually_confirmed), Boundary())
    assert not ok and (str(MIN_STABLE_DAYS) in why or "跨度" in why)

    # a proposal from the ladder is accepted when everything holds
    out = evaluate_transition(StageValue.mutually_confirmed.value, confirmed, f, Boundary())
    assert out.value == StageValue.stable and out.change and out.change.direction == "advance"


def test_stable_is_downgraded_when_durability_is_missing():
    short = _features(_durable(days=5, sessions=10))
    out = evaluate_transition(StageValue.mutually_confirmed.value, _proposed(StageValue.stable), short, Boundary())
    assert out.value == StageValue.mutually_confirmed
    assert out.change and "不能称为稳定" in out.change.reason
    assert "不能称为稳定" in out.note


def test_stable_on_a_first_report_is_also_checked():
    short = _features(_durable(days=5, sessions=10))
    out = evaluate_transition(None, _proposed(StageValue.stable), short, Boundary())
    assert out.value == StageValue.mutually_confirmed


def test_ladder_order_matches_the_documented_sequence():
    assert [s.value for s in STAGE_LADDER] == [
        "initial_contact",
        "getting_to_know",
        "affectionate_signals",
        "mutually_confirmed",
        "stable",
    ]


# ---------------------------------------------------------------- integration


def test_rules_report_records_its_transition():
    from rapport.jobs.pipeline import run_rules

    conv, _ = parse(
        "\n".join(f"[2026-08-{d:02d} 20:00] {'A' if d % 2 else 'B'}: 消息{d}" for d in range(1, 20)),
        "c1",
        "paste",
        None,
        ParseOptions(),
    )
    rep = run_rules(conv, conv.events(), Profile(), previous_stage=StageValue.initial_contact.value)
    assert rep.stage.value in {s.value for s in StageValue}
    assert rep.stage.change is not None
    assert rep.stage.change.before == "initial_contact"


def test_stage_history_endpoint_lists_reports_with_transitions(app_client):
    c = app_client
    assert c.get("/api/stage/history").json()["points"] == []
    imp = c.post("/api/imports", json={"text": "\n".join(f"[2026-08-{d:02d} 20:00] {'A' if d % 2 else 'B'}: 消息{d}" for d in range(1, 20))}).json()
    c.post(f"/api/imports/{imp['id']}/confirm", json={})
    a = c.post("/api/analyses", json={"import_id": imp["id"], "mode": "rules"}).json()
    c.app_state.runner.run_pending_once()

    body = c.get("/api/stage/history").json()
    assert len(body["points"]) == 1
    point = body["points"][0]
    assert point["report_id"] == c.get(f"/api/analyses/{a['id']}").json()["report_id"]
    assert point["value"]
    assert "允许回退" in body["note"]
