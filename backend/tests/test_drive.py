"""v0.6 drive layer: the "hormone" model is deterministic, explainable, bounded, and off by default.

Every rule the plan promised is pinned here: half-life decay, boundary pins inhibition, falling
momentum yields self-care only, daily cap and quiet hours, seeded reproducibility, hourly
idempotence, dismiss feeding inhibition, no-evidence templates producing nothing, and the
relationship cascade.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

import pytest

from rapport.db import Nudge, Setting
from rapport.drive.engine import (
    Signals,
    empty_state,
    explain,
    hour_seed,
    is_quiet,
    pick_kind,
    record_nudge,
    tick,
)
from rapport.drive.nudges import build, recall, self_care, timing
from rapport.drive.params import DEFAULT, DriveParams, drive_digest

NOON = datetime(2026, 9, 18, 12, 0)


def test_confirmed_wechat_actuator_matches_scope_text_and_does_not_repeat_uncertainty():
    from rapport.drive.actuators import REGISTRY, ConfirmedWechatActuator

    calls = []
    preview = {"status": "preview", "relationship_id": "friend", "token": "ticket", "digest": "hash",
               "items": [{"kind": "text", "text": "明确批准的文字"}]}
    nudge = {"relationship_id": "friend", "text": "明确批准的文字", "boundary_pinned": False}
    def confirm(token, digest):
        calls.append((token, digest))
        return {"status": "stopped", "results": [{"status": "uncertain"}]}

    actuator = ConfirmedWechatActuator(preview, confirm)
    assert "wechat_confirmed" not in REGISTRY
    for bad in ({**nudge, "text": "修改过的文字"}, {**nudge, "relationship_id": "another"},
                {**nudge, "boundary_pinned": True}):
        with pytest.raises(ValueError):
            actuator.deliver(bad)
    assert calls == []
    assert not actuator.deliver(nudge).delivered
    assert actuator.deliver(nudge).detail == "already_attempted"
    assert calls == [("ticket", "hash")]


def test_confirmed_wechat_actuator_requires_actual_sent_receipt():
    from rapport.drive.actuators import ConfirmedWechatActuator

    preview = {"status": "preview", "relationship_id": "friend", "token": "ticket", "digest": "hash",
               "items": [{"kind": "text", "text": "hello"}]}
    actuator = ConfirmedWechatActuator(preview, lambda *_: {"status": "complete", "results": [{"status": "sent"}]})
    assert actuator.deliver({"relationship_id": "friend", "text": "hello", "boundary_pinned": False}).delivered


def _rng(v: float):
    class R(random.Random):
        def random(self):  # type: ignore[override]
            return v

        def uniform(self, a, b):  # type: ignore[override]
            return a

    return R(0)


# ------------------------------------------------------------------ engine math


def test_decay_follows_half_life():
    st = empty_state()
    st.update(drive=0.8, inhibition=0.4, novelty=0.2, last_tick=NOON.isoformat(timespec="minutes"))
    later = NOON + timedelta(hours=DEFAULT.drive_half_life_h)
    new, _, _ = tick(st, later, Signals(), DEFAULT, rng=_rng(0.99))
    assert abs(new["drive"] - 0.4) < 1e-6
    assert new["inhibition"] < 0.4 and new["novelty"] < 0.2


def test_affinity_feeds_drive_but_falling_momentum_does_not():
    st = empty_state()
    st["last_tick"] = (NOON - timedelta(days=1)).isoformat(timespec="minutes")
    warm, _, _ = tick(dict(st), NOON, Signals(affinity_total=80, momentum="flat"), DEFAULT, rng=_rng(0.99))
    falling, _, _ = tick(dict(st), NOON, Signals(affinity_total=80, momentum="falling"), DEFAULT, rng=_rng(0.99))
    assert warm["drive"] > 0 and "affinity" in warm["last_sources"]["drive"]
    assert falling["drive"] == 0 and not falling["last_sources"]["drive"]


def test_boundary_pins_inhibition_and_blocks():
    st = empty_state()
    st.update(drive=1.0, novelty=1.0)
    for b in ("stop_contact", "safety_concern"):
        new, should, kind = tick(dict(st), NOON, Signals(boundary=b), DEFAULT, rng=_rng(0.0))
        assert new["inhibition"] == 1.0 and new["boundary_pinned"] and not should and kind is None
        assert new["last_block"] == "boundary"
        assert explain(new)["why_not"]


def test_explicit_decline_charges_once():
    st = empty_state()
    st["last_tick"] = (NOON - timedelta(hours=1)).isoformat(timespec="minutes")
    a, _, _ = tick(dict(st), NOON, Signals(boundary="explicit_decline", boundary_changed=True), DEFAULT, rng=_rng(0.99))
    b, _, _ = tick(dict(st), NOON, Signals(boundary="explicit_decline", boundary_changed=False), DEFAULT, rng=_rng(0.99))
    assert a["inhibition"] == pytest.approx(DEFAULT.explicit_decline_gain) and b["inhibition"] == 0


def test_falling_momentum_only_allows_self_care():
    st = empty_state()
    assert pick_kind(st, NOON, Signals(momentum="falling")) == "self_care"
    assert pick_kind(st, NOON, Signals(momentum="rising")) == "recall"


def test_daily_cap_and_quiet_hours_block():
    st = empty_state()
    st.update(drive=1.0, novelty=1.0)
    night = datetime(2026, 9, 18, 23, 30)
    assert is_quiet(night) and not is_quiet(NOON)
    new, should, _ = tick(dict(st), night, Signals(), DEFAULT, rng=_rng(0.0))
    assert not should and new["last_block"] == "quiet_hours"
    capped = dict(st)
    capped.update(day=f"{NOON:%Y-%m-%d}", today_count=DEFAULT.daily_cap)
    new, should, _ = tick(capped, NOON, Signals(), DEFAULT, rng=_rng(0.0))
    assert not should and new["last_block"] == "daily_cap"


def test_same_kind_cooldown():
    st = record_nudge(empty_state(), NOON, "recall")
    assert pick_kind(st, NOON + timedelta(hours=1), Signals()) == "timing"
    assert pick_kind(st, NOON + timedelta(hours=DEFAULT.same_kind_cooldown_h + 1), Signals()) == "recall"


def test_tick_is_idempotent_within_the_hour_and_seeded():
    st = empty_state()
    st.update(drive=0.9, novelty=0.9)
    a, sa, _ = tick(dict(st), NOON, Signals(), DEFAULT, relationship_id="r1")
    b, sb, _ = tick(dict(st), NOON, Signals(), DEFAULT, relationship_id="r1")
    assert (a["last_roll"], sa) == (b["last_roll"], sb)  # same seed, same decision
    again, s_again, _ = tick(a, NOON + timedelta(minutes=20), Signals(), DEFAULT, relationship_id="r1")
    assert again == a and not s_again  # second call in the same hour: nothing happens
    assert hour_seed("r1", NOON) != hour_seed("r2", NOON) and hour_seed("r1", NOON) != hour_seed("r1", NOON + timedelta(hours=1))


def test_dismiss_and_thumbs_down_raise_inhibition():
    st = empty_state()
    new, _, _ = tick(dict(st), NOON, Signals(dismissed=1, thumbs_down=1), DEFAULT, rng=_rng(0.99))
    assert new["inhibition"] == pytest.approx(DEFAULT.dismiss_gain + DEFAULT.thumbs_down_gain)


def test_digest_changes_when_a_parameter_changes():
    assert drive_digest(DEFAULT) != drive_digest(DriveParams(daily_cap=2))
    assert drive_digest(DEFAULT).startswith("drive-v1+")


# ------------------------------------------------------------------ templates


def test_templates_need_evidence():
    assert recall({}, "TA") is None and timing({}, "TA") is None
    body = {"evidence": {"m1": {"sender": "other", "ts": "2026-09-01T20:00:00", "text": "周六下午两点见"}}, "observations": []}
    r = recall(body, "小红")
    assert r and r.evidence_ids == ["m1"] and "周六下午两点见" in r.text
    sc = self_care("小红")
    assert not any(ch.isdigit() for ch in sc.text)
    assert build("draft", body=body, other_name="x", strict_drafts=[], outcome_prompt=None) is None
    assert build("checkin", body=body, other_name="x", strict_drafts=None, outcome_prompt=None) is None


# ------------------------------------------------------------------ state + API


CHAT = "\n".join(f"[2026-09-{d:02d} 20:00] {'A' if d % 2 else 'B'}: 消息{d} 周末一起吃饭吧" for d in range(1, 16))


def _report(c) -> str:
    imp = c.post("/api/imports", json={"text": CHAT}).json()
    c.post(f"/api/imports/{imp['id']}/confirm", json={})
    a = c.post("/api/analyses", json={"import_id": imp["id"], "mode": "rules"}).json()
    c.app_state.runner.run_pending_once()
    return c.get(f"/api/analyses/{a['id']}").json()["report_id"]


def test_off_by_default_tick_writes_nothing(app_client):
    c = app_client
    _report(c)
    out = c.app_state.drive_tick(NOON)
    assert out["skipped"] == "disabled" and out["ticked"] == 0
    assert c.get("/api/drive/inbox").json()["items"] == []
    cfg = c.get("/api/drive/config").json()
    assert cfg["enabled"] is False and cfg["daily_cap"] == 1


def test_enabled_tick_persists_state_and_explains(app_client):
    c = app_client
    _report(c)
    cfg = c.patch("/api/drive/config", json={"enabled": True}).json()
    assert cfg["enabled"] is True
    st = c.app_state
    out = st.drive_tick(NOON)
    assert out["ticked"] >= 1
    ex = c.get("/api/relationships/default/drive").json()
    assert ex["enabled"] and ex["last_tick"] and "drive" in ex and "sources" in ex
    with st.db.session() as s:
        assert s.get(Setting, "drive.state.default") is not None
    # an hour later the state still advances; same hour does not
    assert st.drive_tick(NOON + timedelta(minutes=5))["ticked"] == 0
    assert st.drive_tick(NOON + timedelta(hours=1))["ticked"] >= 1


def test_forced_nudge_lands_in_inbox_with_evidence_and_feeds_back(app_client, monkeypatch):
    c = app_client
    _report(c)
    st = c.app_state
    st.drive_set_config({"enabled": True})
    # force the decision: drive high, roll zero
    st._drive_save("default", {**empty_state(), "drive": 1.0, "novelty": 1.0})
    import rapport.state.drive as sd

    real_tick = sd.tick

    def forced(state, now, sig, p, rng=None, relationship_id="default"):
        return real_tick(state, now, sig, p, rng=_rng(0.0), relationship_id=relationship_id)

    monkeypatch.setattr(sd, "tick", forced)
    out = st.drive_tick(NOON)
    assert out["nudged"] == 1
    inbox = c.get("/api/drive/inbox").json()
    assert inbox["unread"] == 1
    n = inbox["items"][0]
    assert n["kind"] == "recall" and n["evidence_ids"] and n["status"] == "unread"
    assert n["drive_snapshot"]["drive"] >= 0.9 and n["version"].startswith("drive-v1+")
    # second nudge the same day is capped
    st._drive_save("default", {**st.drive_state("default"), "drive": 1.0, "novelty": 1.0})
    assert st.drive_tick(NOON + timedelta(hours=1))["nudged"] == 0
    assert st.drive_state("default")["last_block"] == "daily_cap"
    # dismiss + 👎 → inhibition on the next tick
    assert c.post(f"/api/nudges/{n['id']}/dismissed").json()["status"] == "dismissed"
    assert c.put(f"/api/nudges/{n['id']}/verdict", json={"verdict": "disagree"}).json()["verdict"] == "disagree"
    st.drive_tick(NOON + timedelta(hours=2))
    src = st.drive_state("default")["last_sources"]["inhibition"]
    assert src.get("dismissed") and src.get("thumbs_down")
    assert c.post(f"/api/nudges/{n['id']}/bogus").status_code == 422


def test_relationship_delete_and_wipe_remove_nudges(app_client):
    c = app_client
    st = c.app_state
    rel = c.post("/api/relationships", json={"name": "小蓝"}).json()
    st._nudge_save({"relationship_id": rel["id"], "kind": "self_care", "text": "x", "evidence_ids": [], "why": "", "drive_snapshot": {}, "actuator": "inbox", "version": "t"})
    st._drive_save(rel["id"], empty_state())
    c.delete(f"/api/relationships/{rel['id']}")
    with st.db.session() as s:
        assert s.query(Nudge).filter(Nudge.relationship_id == rel["id"]).count() == 0
        assert s.get(Setting, f"drive.state.{rel['id']}") is None
    st._nudge_save({"relationship_id": "default", "kind": "self_care", "text": "y", "evidence_ids": [], "why": "", "drive_snapshot": {}, "actuator": "inbox", "version": "t"})
    counts = st.wipe_all()
    assert counts.get("nudges") == 1
    with st.db.session() as s:
        assert s.query(Nudge).count() == 0


def test_scoring_never_imports_the_drive_layer():
    import importlib
    import pkgutil

    import rapport.scoring as scoring

    for m in pkgutil.iter_modules(scoring.__path__):
        mod = importlib.import_module(f"rapport.scoring.{m.name}")
        assert "rapport.drive" not in " ".join(getattr(v, "__name__", "") for v in vars(mod).values() if hasattr(v, "__name__"))
