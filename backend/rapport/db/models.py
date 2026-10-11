"""SQLAlchemy models. app.db stores metadata, features, reports and minimal evidence — never raw chats or keys."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


DEFAULT_RELATIONSHIP_ID = "default"


class Relationship(Base):
    """One person the user is analysing chats with (关系档案).

    Everything derived from chat — events, N_eff, snapshots, portrait versions, stage history,
    reports, feedback — is scoped by `relationship_id`, so analysing two different people never
    blends their evidence. A default relationship is created on first use so the single-person
    flow needs no extra step. Labels (MBTI / zodiac / Chinese zodiac) live here as the durable
    place for them; a report's profile snapshot still records what was used at the time.
    """

    __tablename__ = "relationships"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(60), default="")
    note: Mapped[str] = mapped_column(String(255), default="")
    labels: Mapped[dict] = mapped_column(JSON, default=dict)  # {"mbti": ..., "zodiac": ..., "chinese_zodiac": ...}
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)


class Import(Base):
    __tablename__ = "imports"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    relationship_id: Mapped[str] = mapped_column(String(32), default=DEFAULT_RELATIONSHIP_ID, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)
    status: Mapped[str] = mapped_column(String(32), default="awaiting_confirmation")
    source: Mapped[str] = mapped_column(String(32))
    parser_id: Mapped[str] = mapped_column(String(32))
    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    n_messages: Mapped[int] = mapped_column(Integer, default=0)
    blob_id: Mapped[str] = mapped_column(String(32))  # encrypted message-v1 JSON
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    participants: Mapped[dict] = mapped_column(JSON, default=dict)
    range_start: Mapped[int | None] = mapped_column(Integer, nullable=True)  # seq
    range_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)


class Analysis(Base):
    __tablename__ = "analyses"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    relationship_id: Mapped[str] = mapped_column(String(32), default=DEFAULT_RELATIONSHIP_ID, index=True)
    import_id: Mapped[str] = mapped_column(ForeignKey("imports.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)
    mode: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(32), default="created")
    stage_done: Mapped[int] = mapped_column(Integer, default=0)
    stage_total: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(String(255), default="")
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True)
    profile: Mapped[dict] = mapped_column(JSON, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(128), index=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confirmed_payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    est_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    edited_lines: Mapped[dict] = mapped_column(JSON, default=dict)
    checkpoints: Mapped[dict] = mapped_column(JSON, default=dict)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    report_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    tombstone: Mapped[bool] = mapped_column(Boolean, default=False)


class ReportRow(Base):
    __tablename__ = "reports"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    relationship_id: Mapped[str] = mapped_column(String(32), default=DEFAULT_RELATIONSHIP_ID, index=True)
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analyses.id"))
    import_id: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    mode: Mapped[str] = mapped_column(String(16))
    total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    body: Mapped[dict] = mapped_column(JSON)  # report-v1
    profile: Mapped[dict] = mapped_column(JSON, default=dict)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)


class Event(Base):
    """Canonical observed event, deduplicated across imports by content fingerprint.

    Evidence rows (Phase 2) reference `fingerprint`, not `msg_id`, so re-importing the
    same chat never inflates the effective sample size.
    """

    __tablename__ = "events"
    fingerprint: Mapped[str] = mapped_column(String(40), primary_key=True)
    relationship_id: Mapped[str] = mapped_column(String(32), default=DEFAULT_RELATIONSHIP_ID, index=True)
    first_import_id: Mapped[str] = mapped_column(String(32), index=True)
    msg_id: Mapped[str] = mapped_column(String(32))
    sender: Mapped[str] = mapped_column(String(16))
    type: Mapped[str] = mapped_column(String(16))
    ts: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ts_precision: Mapped[str] = mapped_column(String(8), default="none")  # second|minute|day|none
    episode_id: Mapped[str] = mapped_column(String(64), default="")
    origin: Mapped[str] = mapped_column(String(16), default="observed_chat")
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    seen_count: Mapped[int] = mapped_column(Integer, default=1)


class Snapshot(Base):
    """One real, comparable point in a relationship's history.

    Written only when a confirmed import finishes an analysis. The unique key makes the same
    material under the same versions collapse to a single point (FR-010): no duplicate dots,
    and a rule/model change is detectable rather than silently connected.
    """

    __tablename__ = "snapshots"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    relationship_id: Mapped[str] = mapped_column(String(32), default=DEFAULT_RELATIONSHIP_ID, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    import_id: Mapped[str] = mapped_column(String(32), index=True)
    report_id: Mapped[str] = mapped_column(String(32))
    material_hash: Mapped[str] = mapped_column(String(64), index=True)
    rubric_version: Mapped[str] = mapped_column(String(32))
    features_version: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    coverage: Mapped[float] = mapped_column(Float, default=0.0)
    n_eff: Mapped[float] = mapped_column(Float, default=0.0)
    dims: Mapped[dict] = mapped_column(JSON, default=dict)
    note: Mapped[str] = mapped_column(String(255), default="")


class PersonaVersion(Base):
    """One inspectable portrait revision. Old versions keep their content and are marked inactive."""

    __tablename__ = "persona_versions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    relationship_id: Mapped[str] = mapped_column(String(32), default=DEFAULT_RELATIONSHIP_ID, index=True)
    report_id: Mapped[str] = mapped_column(String(32), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    superseded_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    tendencies: Mapped[dict] = mapped_column(JSON, default=dict)
    traits: Mapped[list] = mapped_column(JSON, default=list)
    axes: Mapped[dict] = mapped_column(JSON, default=dict)
    change_notes: Mapped[list] = mapped_column(JSON, default=list)


class DiaryRow(Base):
    """The user's own diary. Only the current revision exists; older vectors become stale.

    Kept out of reports, exports and diagnostics by design: a diary entry is the most sensitive
    thing this project stores. `allowed_relationships` is the isolation scope (empty = usable
    nowhere), and `approved_clause` is the only text strict mode may reproduce.
    """

    __tablename__ = "diary"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    text: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(16), default="fact")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    allowed_relationships: Mapped[list] = mapped_column(JSON, default=list)
    approved_clause: Mapped[str | None] = mapped_column(Text, nullable=True)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)


class LLMCall(Base):
    """One model call, metadata only (never prompt or completion text).

    This is the audit ledger behind cost and reliability numbers: how many calls a report really
    took, how many were truncated or failed, how long they waited. `prompt_hash` lets two calls be
    recognised as the same request without storing the request.
    """

    __tablename__ = "llm_calls"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    relationship_id: Mapped[str] = mapped_column(String(32), default=DEFAULT_RELATIONSHIP_ID, index=True)
    analysis_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    purpose: Mapped[str] = mapped_column(String(16), default="unknown")  # extract|synthesize|intent|reply|ping
    preset: Mapped[str] = mapped_column(String(32), default="")
    model: Mapped[str] = mapped_column(String(64), default="")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    usage_known: Mapped[bool] = mapped_column(Boolean, default=True)
    max_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="ok")  # ok|truncated|bad_json|error
    error_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cost_cny: Mapped[float | None] = mapped_column(Float, nullable=True)
    prompt_hash: Mapped[str] = mapped_column(String(64), default="")


class Feedback(Base):
    """The user's verdict on one claim in one report.

    This is how the product learns where it is wrong without any telemetry: a 👍/👎 on an intent
    reading, an observation or an action is stored locally, shown on the report, and can be
    exported (locally) as evaluation cases. It never changes a score retroactively — feedback is
    a record, not a training signal, until a human folds it into the evaluation set.
    """

    __tablename__ = "feedback"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    relationship_id: Mapped[str] = mapped_column(String(32), default=DEFAULT_RELATIONSHIP_ID, index=True)
    report_id: Mapped[str] = mapped_column(String(32), index=True)
    target_kind: Mapped[str] = mapped_column(String(24))  # intent|observation|action|landmine|stage|trait|affinity
    target_key: Mapped[str] = mapped_column(String(160), default="")  # msg id / observation id / index / statement
    verdict: Mapped[str] = mapped_column(String(12))  # agree|disagree|unsure
    note: Mapped[str] = mapped_column(String(300), default="")
    correction: Mapped[dict] = mapped_column(JSON, default=dict)  # e.g. {"pragmatic_tags": [...], "emotions": [...]}


class ActionLog(Base):
    """The user carried out one playbook option and reports what the other person did.

    A record, not a signal: nothing in `scoring/` reads it. The next synthesis receives it as
    "last time you did X and they Y" so advice can build on it instead of repeating it.
    """

    __tablename__ = "action_log"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    relationship_id: Mapped[str] = mapped_column(String(32), default=DEFAULT_RELATIONSHIP_ID, index=True)
    report_id: Mapped[str] = mapped_column(String(32), index=True)
    option_id: Mapped[str] = mapped_column(String(40))
    goal: Mapped[str] = mapped_column(String(120), default="")
    done_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    observed: Mapped[str] = mapped_column(String(16), default="other")  # accepted|alt_time|declined|no_reply|other
    note: Mapped[str] = mapped_column(String(300), default="")


class Outcome(Base):
    """Weeks-later check-in: how did it go? Local only; the only ground truth this product can have.

    Used for *direction* calibration by a human reading `scripts/outcome_direction.py` output,
    never fitted into a score.
    """

    __tablename__ = "outcomes"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    relationship_id: Mapped[str] = mapped_column(String(32), default=DEFAULT_RELATIONSHIP_ID, index=True)
    report_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    asked_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    outcome: Mapped[str] = mapped_column(String(16), default="unknown")  # better|same|worse|ended|unknown
    met_since: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str] = mapped_column(String(300), default="")


class Nudge(Base):
    """One proactive message to the user (v0.6 drive layer). Local inbox only.

    `drive_snapshot` records the three drive variables and their sources at the moment of
    sending, so "why did it say this now?" is answerable later. `verdict` is the user's 👍/👎.
    """

    __tablename__ = "nudges"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    relationship_id: Mapped[str] = mapped_column(String(32), default=DEFAULT_RELATIONSHIP_ID, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    kind: Mapped[str] = mapped_column(String(16))  # recall|timing|draft|checkin|self_care
    text: Mapped[str] = mapped_column(Text)
    evidence_ids: Mapped[list] = mapped_column(JSON, default=list)
    why: Mapped[str] = mapped_column(String(120), default="")
    drive_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(12), default="unread")  # unread|read|dismissed|acted
    verdict: Mapped[str | None] = mapped_column(String(12), nullable=True)  # agree|disagree|unsure
    actuator: Mapped[str] = mapped_column(String(16), default="inbox")
    version: Mapped[str] = mapped_column(String(40), default="")


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)


class GoldLabel(Base):
    """One human-assigned label for a dataset sample (人工金标, v0.8).

    The offline layer's evaluation numbers are agreement with AI-assisted labels; this table is
    the user's own answer for a stratified sample of their messages, so every method can be
    scored against a human reference. Only ids, labels and the AI label at labelling time are
    stored — the text stays in the encrypted dataset snapshot.
    """

    __tablename__ = "gold_labels"
    id: Mapped[str] = mapped_column(String(200), primary_key=True)  # f"{dataset_id}:{sample_id}"
    dataset_id: Mapped[str] = mapped_column(String(32), index=True)
    pool: Mapped[str] = mapped_column(String(24), default="offline_review")
    sample_id: Mapped[str] = mapped_column(String(160), index=True)
    emotion: Mapped[str | None] = mapped_column(String(16), nullable=True)
    intent: Mapped[str | None] = mapped_column(String(16), nullable=True)
    skipped: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str] = mapped_column(String(500), default="")
    weak_emotion: Mapped[str] = mapped_column(String(16), default="")  # AI label, recorded after the human answered
    split: Mapped[str] = mapped_column(String(12), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
