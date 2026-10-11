"""Actuators: where a nudge goes once the drive layer decides to send one.

The hourly registry contains only the local inbox. ConfirmedWechatActuator is an
explicit bridge for a recipient/text preview the user has approved; it is never
selected by the hourly loop and cannot deliver while a boundary is pinned.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Receipt:
    actuator: str
    delivered: bool
    detail: str = ""


class Actuator(Protocol):
    id: str
    audience: str  # "user" | "other"  — who ends up reading the message

    def deliver(self, nudge: dict) -> Receipt: ...


class InboxActuator:
    """Writes to the local inbox. The user reads it in the app; nobody else does."""

    id = "inbox"
    audience = "user"

    def __init__(self, save):
        self._save = save  # callable(nudge_dict) -> stored row dict

    def deliver(self, nudge: dict) -> Receipt:
        self._save(nudge)
        return Receipt(self.id, True, "written to local inbox")


REGISTRY: dict[str, type] = {"inbox": InboxActuator}


class ConfirmedWechatActuator:
    """Opt-in bridge to one exact, already-approved text dispatch preview.

    Not in REGISTRY: hourly nudges remain local. The caller supplies a preview
    approved for this recipient and text, and the existing durable confirmation
    function. Failed/uncertain execution cannot be attempted twice here either.
    """

    id = "wechat_confirmed"
    audience = "other"

    def __init__(self, preview: dict, confirm):
        from copy import deepcopy

        items = preview.get("items") or []
        if preview.get("status") != "preview" or len(items) != 1 or items[0].get("kind") != "text":
            raise ValueError("One approved text preview is required")
        self.preview = deepcopy(preview)
        self.confirm = confirm
        self.attempted = False
        self.result = None

    def deliver(self, nudge: dict) -> Receipt:
        if (nudge.get("relationship_id") != self.preview["relationship_id"]
                or nudge.get("text") != self.preview["items"][0]["text"]
                or nudge.get("boundary_pinned") is not False):
            raise ValueError("Drive message does not match the approved recipient/text or boundary gate")
        if self.attempted:
            return Receipt(self.id, False, "already_attempted")
        self.attempted = True
        self.result = self.confirm(self.preview["token"], self.preview["digest"])
        results = self.result.get("results") or []
        delivered = (self.result.get("status") == "complete" and len(results) == 1 and results[0].get("status") == "sent")
        return Receipt(self.id, delivered, self.result.get("status", "uncertain"))
