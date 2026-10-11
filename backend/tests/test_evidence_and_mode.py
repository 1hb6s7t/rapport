"""Phase 0: evidence ledger, canonical event dedupe, and the local/cloud mode guard."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from rapport.config import ConnectionMode, is_loopback_url, reset_settings
from rapport.db import Event
from rapport.llm.provider import LLMError
from rapport.parsers import ParseOptions, parse
from rapport.scoring.evidence import (
    ORIGIN_AI_DRAFT,
    ORIGIN_OBSERVED,
    ORIGIN_PUBLIC_EXAMPLE,
    Evidence,
    EvidenceError,
    count_episodes,
    effective_evidence,
)
from rapport.state import AppState

# ---------------------------------------------------------------- evidence ledger


def ev(eid: str, ep: str, kind: str = "care", quality: float = 1.0, **kw) -> Evidence:
    return Evidence(source_event_id=eid, episode_id=ep, kind=kind, quality=quality, **kw)


def test_repeat_import_and_repeat_kind_count_once():
    items = [ev("e1", "s1"), ev("e1", "s1"), ev("e1", "s1", kind="care"), ev("e1", "s1", kind="reply")]
    # (e1, care) collapses to one entry; (e1, reply) is a different observation kind
    assert effective_evidence(items) == pytest.approx(2.0)


def test_ai_draft_and_public_example_are_not_evidence():
    items = [
        ev("e1", "s1"),
        ev("e2", "s1", origin=ORIGIN_AI_DRAFT),
        ev("e3", "s1", origin=ORIGIN_PUBLIC_EXAMPLE),
        ev("e4", "s1", confirmed=False),
    ]
    assert effective_evidence(items) == pytest.approx(1.0)
    assert count_episodes(items) == 1


def test_episode_cap_limits_correlated_evidence():
    # 20 high-quality messages inside one episode cannot exceed the cap
    items = [ev(f"e{i}", "s1", kind=f"k{i}") for i in range(20)]
    assert effective_evidence(items, episode_cap=5.0) == pytest.approx(5.0)
    # a second episode adds its own capped contribution
    items += [ev(f"b{i}", "s2", kind=f"k{i}") for i in range(20)]
    assert effective_evidence(items, episode_cap=5.0) == pytest.approx(10.0)


def test_quality_is_capped_by_max_not_summed_when_sum_exceeds_cap():
    items = [ev("e1", "s1", kind="a", quality=0.6), ev("e1", "s1", kind="b", quality=0.6), ev("e1", "s1", kind="c", quality=0.6)]
    assert effective_evidence(items) == pytest.approx(1.8)
    assert effective_evidence(items, episode_cap=1.0) == pytest.approx(1.0)


def test_canonical_event_cannot_move_episodes():
    # same (event, kind) reported under two different episodes is a contradiction
    with pytest.raises(EvidenceError):
        effective_evidence([ev("e1", "s1"), ev("e1", "s2")])
    # a different observation kind in another episode is legitimate, not a conflict
    assert effective_evidence([ev("e1", "s1"), ev("e1", "s2", kind="reply")]) == pytest.approx(2.0)


def test_invalid_inputs_rejected():
    with pytest.raises(EvidenceError):
        effective_evidence([ev("e1", "s1", quality=1.5)])
    with pytest.raises(EvidenceError):
        effective_evidence([ev("e1", "s1", quality=float("nan"))])
    with pytest.raises(EvidenceError):
        effective_evidence([ev("", "s1")])
    with pytest.raises(EvidenceError):
        effective_evidence([ev("e1", "s1")], episode_cap=0)
    with pytest.raises(EvidenceError):
        effective_evidence([ev("e1", "s1")], episode_cap=float("inf"))


def test_count_episodes_respects_min_quality():
    items = [ev("e1", "s1", quality=0.9), ev("e2", "s2", quality=0.2)]
    assert count_episodes(items) == 2
    assert count_episodes(items, min_quality=0.5) == 1


# ---------------------------------------------------------------- event fingerprints


@pytest.fixture
def state():
    st = AppState(reset_settings(data_dir=Path(tempfile.mkdtemp()), pairing_code="123456"))
    yield st


SAME_CHAT = """[2026-09-01 20:00] A: 你好呀
[2026-09-01 20:05] B: 你好
[2026-09-01 20:05] B: 你好
[2026-09-01 20:30] A: 哈哈
[2026-09-01 20:30] A: 哈哈
"""


def _import(st: AppState, text: str, cid: str):
    conv, pid = parse(text, cid, "paste", None, ParseOptions())
    return st.save_import(conv, pid, None)


def test_reimporting_same_chat_adds_no_new_events(state):
    imp1 = _import(state, SAME_CHAT, "c1")
    with state.db.session() as s:
        n1 = s.query(Event).count()
    assert n1 == 5

    imp2 = _import(state, SAME_CHAT, "c2")  # same content, different import
    with state.db.session() as s:
        n2 = s.query(Event).count()
        bumped = [e.seen_count for e in s.query(Event).order_by(Event.fingerprint).all()]
    assert n2 == n1, "re-import must not create new canonical events"
    assert min(bumped) == 2, "existing events should have been seen twice"
    assert imp1.id != imp2.id


def test_identical_text_in_same_minute_stays_distinct(state):
    _import(state, SAME_CHAT, "c1")
    with state.db.session() as s:
        rows = s.query(Event).filter(Event.sender == "me").all()
    # the two real "哈哈" messages must remain two separate canonical events
    assert len(rows) == 3  # 你好呀 + 哈哈 + 哈哈
    texts = sorted(r.msg_id for r in rows)
    assert len(set(texts)) == 3


def test_events_carry_episode_ids_and_are_unconfirmed_by_default(state):
    _import(state, SAME_CHAT, "c1")
    with state.db.session() as s:
        rows = s.query(Event).all()
    assert all(r.episode_id.startswith("c1:s") for r in rows)
    assert all(r.origin == ORIGIN_OBSERVED for r in rows)
    assert all(r.confirmed is False for r in rows), "confirmation is a separate, explicit step"


# ---------------------------------------------------------------- mode guard


def test_is_loopback_url():
    assert is_loopback_url("http://127.0.0.1:11434/v1")
    assert is_loopback_url("http://localhost:11434/v1")
    assert not is_loopback_url("https://api.example.invalid/v1")
    assert not is_loopback_url(None)
    assert not is_loopback_url("not a url")


def test_local_mode_refuses_cloud_endpoint(state):
    state.save_provider_config("custom", "some-model", "https://api.example.invalid/v1", "test-key", None)
    assert state.connection_mode() == ConnectionMode.local  # local is the default
    with pytest.raises(LLMError) as e:
        state.make_provider()
    assert e.value.code == "mode_violation"
    assert "本地模式" in e.value.msg


def test_local_mode_allows_loopback_endpoint(state):
    state.save_provider_config("ollama", "qwen3:8b", "http://127.0.0.1:11434/v1", None, None)
    provider = state.make_provider()
    assert provider.info.base_url.startswith("http://127.0.0.1")


def test_local_runtimes_are_ready_without_a_key(state):
    for preset in ("ollama", "ollama-split", "ollama-lite"):
        state.save_provider_config(preset, None, None, None, None)
        cfg = state.provider_config()
        assert cfg["has_key"] is False and cfg["ready"] is True
    state.save_provider_config("custom", "some-model", "https://api.example.invalid/v1", None, None)
    assert state.provider_config()["ready"] is False


def test_switching_to_cloud_mode_allows_cloud_endpoint(state):
    state.save_provider_config("custom", "some-model", "https://api.example.invalid/v1", "test-key", None)
    with pytest.raises(LLMError):
        state.make_provider()
    state.set_connection_mode(ConnectionMode.cloud)
    provider = state.make_provider()
    assert provider.info.base_url == "https://api.example.invalid/v1"


def test_mode_survives_reload(state):
    state.set_connection_mode(ConnectionMode.cloud)
    assert state.connection_mode() == ConnectionMode.cloud
    state.set_connection_mode(ConnectionMode.local)
    assert state.connection_mode() == ConnectionMode.local


def test_rules_mode_makes_no_model_calls():
    """Rules mode must never reach a provider; the FakeProvider counts calls."""
    from rapport.jobs.pipeline import run_rules
    from rapport.llm.fake import FakeProvider
    from rapport.schemas.profile import Profile

    fake = FakeProvider()
    AppState(reset_settings(data_dir=Path(tempfile.mkdtemp())), provider_override=fake)
    conv, _ = parse(SAME_CHAT.replace("哈哈", "今天天气不错"), "c1", "paste", None, ParseOptions())
    run_rules(conv, conv.events(), Profile())
    assert fake.calls == []


# ---------------------------------------------------------------- v0.6: unread media is not evidence


def test_unread_media_does_not_raise_n_eff():
    """A thousand unread photos must not make the ledger say 'enough evidence'."""
    from rapport.scoring.evidence import evidence_breakdown, n_eff_from_events

    class Row:
        def __init__(self, i, kind, ep="e1"):
            self.fingerprint = f"f{i}"
            self.episode_id = ep
            self.type = kind
            self.ts_precision = "second"
            self.origin = ORIGIN_OBSERVED
            self.confirmed = True

    texts = [Row(i, "text", ep=f"e{i}") for i in range(3)]
    base = n_eff_from_events(texts)
    with_media = texts + [Row(1000 + i, "image", ep=f"m{i % 50}") for i in range(1000)]
    assert n_eff_from_events(with_media) == base
    bd = evidence_breakdown(with_media)
    assert bd == {"text": 3, "media_unobserved": 1000, "other": 0}


def test_transcribed_voice_counts_as_text_evidence():
    """A voice row the platform already transcribed arrives typed `text`, so it counts fully."""
    from rapport.scoring.evidence import event_quality

    assert event_quality("text", "second") == 1.0
    assert event_quality("voice", "second") == 0.0
    assert event_quality("sticker", "second") == 0.0


def test_large_import_warns_but_is_accepted(app_client):
    """Above the soft limit the import goes through with a `large_import` warning; the hard
    limit is what rejects."""
    c = app_client
    lines = [f"[2026-09-01 20:{i % 60:02d}] {'A' if i % 2 else 'B'}: 第{i}句" for i in range(5100)]
    r = c.post("/api/imports", json={"text": "\n".join(lines)})
    assert r.status_code == 200, r.text
    codes = {w["code"] for w in r.json()["warnings"]}
    assert "large_import" in codes
