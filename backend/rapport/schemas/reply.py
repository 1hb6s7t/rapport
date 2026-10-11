"""Reply draft contract (v2 M10).

A draft is a suggestion the user may copy. It is never sent by this tool, and it may only use
personal facts that came from the user's own diary — never invented ones. Every draft therefore
carries the diary revisions it used, and the validator refuses text that reproduces a diary
sentence the user has not approved.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ReplyStyle(StrEnum):
    casual = "casual"  # 轻松
    sincere = "sincere"  # 真诚
    direct = "direct"  # 直接
    warm = "warm"  # 温柔
    humorous = "humorous"  # 幽默
    brief = "brief"  # 简洁


STYLE_LABELS = {
    "casual": "轻松",
    "sincere": "真诚",
    "direct": "直接",
    "warm": "温柔",
    "humorous": "幽默",
    "brief": "简洁",
}

DEFAULT_STYLES: tuple[str, ...] = (ReplyStyle.casual.value, ReplyStyle.sincere.value, ReplyStyle.direct.value)


class MemoryRef(BaseModel):
    id: str
    revision: int
    kind: str = "fact"


class ReplyItem(BaseModel):
    """Ordered message draft. Models refer to assets by ID, never filesystem paths."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["text", "sticker", "image"] = "text"
    text: str | None = Field(default=None, max_length=4000)
    asset_id: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_content(self):
        if self.kind == "text":
            if not self.text or not self.text.strip() or self.asset_id is not None:
                raise ValueError("文字项目必须有文字，不能有素材 ID")
        elif self.text is not None or not self.asset_id:
            raise ValueError("媒体项目必须有素材 ID，不能含文字或路径")
        return self


class ReplyDraft(BaseModel):
    style: str
    text: str
    items: list[ReplyItem] = Field(default_factory=list, max_length=12)
    ranking: dict[str, Any] = Field(default_factory=dict)
    memory_refs: list[MemoryRef] = Field(default_factory=list)
    why: str = ""  # why this draft fits the current situation
    risk_notes: list[str] = Field(default_factory=list)
    verified: bool = False  # passed the anti-fabrication check
    verification_note: str = ""
    mode: str = "free"  # free | strict_slot
    needs_review: bool = False  # a first-person experience in the text has no diary behind it
    unbacked_claims: list[str] = Field(default_factory=list)


class ReplyPack(BaseModel):
    drafts: list[ReplyDraft] = Field(default_factory=list)
    withheld_drafts: list[dict[str, str]] = Field(default_factory=list)
    memory_enabled: bool = True
    memories_used: list[MemoryRef] = Field(default_factory=list)
    dropped_memories: dict[str, int] = Field(default_factory=dict)
    style_note: str = ""
    send_note: str = "这些只是草稿：生成草稿不会代替你发送消息；只有核对接收人与整组内容并确认后，才会执行微信发送。"
    fabricated_note: str = ""
    abstain_reason: str | None = None
