"""API request/response models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from rapport.schemas.profile import Profile


class PairRequest(BaseModel):
    code: str = Field(min_length=4, max_length=12)


class ProviderSettingsIn(BaseModel):
    preset: str
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None  # None = unchanged, "" = clear
    cost_warn_cny: float | None = Field(default=None, ge=0)
    connection_mode: str | None = None  # "local" | "cloud"; None = unchanged


class ImportTextIn(BaseModel):
    text: str
    format: str = "auto"
    relationship_id: str | None = None  # None / "" = the default relationship
    filename: str | None = None
    date_order: str | None = None
    me_name: str | None = None
    timezone: str = "Asia/Shanghai"


class MessagePatch(BaseModel):
    id: str
    sender: str | None = None
    text: str | None = None
    type: str | None = None


class ImportPatch(BaseModel):
    swap_roles: bool = False
    messages: list[MessagePatch] = Field(default_factory=list)
    participants: dict[str, str] | None = None
    timezone: str | None = None


class ConfirmIn(BaseModel):
    range_start: int = 0
    range_end: int | None = None


class AnalysisIn(BaseModel):
    import_id: str
    profile: Profile = Field(default_factory=Profile)
    mode: str = "rules"  # rules | byok


class AnalysisConfirmIn(BaseModel):
    payload_hash: str
    edited_lines: dict[int, list[str]] = Field(default_factory=dict)
    accept_cost: bool = False


class ReportProfilePatch(BaseModel):
    profile: Profile


class RelationshipIn(BaseModel):
    id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,32}$")  # optional stable id (scripts / tests)
    name: str = Field(min_length=1, max_length=60)
    note: str = Field(default="", max_length=255)
    labels: dict[str, str | None] = Field(default_factory=dict)  # mbti / zodiac / chinese_zodiac


class RelationshipPatch(BaseModel):
    name: str | None = Field(default=None, max_length=60)
    note: str | None = Field(default=None, max_length=255)
    labels: dict[str, str | None] | None = None
    archived: bool | None = None


class FeedbackIn(BaseModel):
    target_kind: str  # intent | observation | action | landmine | stage | trait | affinity | boundary
    target_key: str = Field(default="", max_length=160)
    verdict: str  # agree | disagree | unsure
    note: str = Field(default="", max_length=300)
    correction: dict = Field(default_factory=dict)


class ActionDoneIn(BaseModel):
    goal: str = Field(default="", max_length=120)
    observed: str  # accepted | alt_time | declined | no_reply | other
    note: str = Field(default="", max_length=300)


class OutcomeIn(BaseModel):
    report_id: str | None = None
    outcome: str  # better | same | worse | ended | unknown
    met_since: bool = False
    note: str = Field(default="", max_length=300)


class ReplyIn(BaseModel):
    import_id: str | None = None
    relationship_id: str = ""
    styles: list[str] = Field(default_factory=list, max_length=6)
    memory_enabled: bool | None = None
    memory_limit: int = Field(default=3, ge=0, le=10)
    mode: str = "free"  # free | strict
    goal: str | None = Field(default=None, max_length=200)
    skill: Literal["off", "goutoujunshi"] = "off"


class ReplyConfirmIn(ReplyIn):
    """Free mode: the ticket and hash from `/replies/preview`. Strict mode needs neither."""

    ticket_id: str | None = None
    payload_hash: str | None = None
    accept_cost: bool = False
