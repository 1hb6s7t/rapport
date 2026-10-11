"""Outbound gate: the one place that decides what leaves the machine and proves it was shown.

The analysis path has had this discipline since v0.1 (preview → hash → confirm → send exactly
that). v0.4.1 makes it a reusable object so the reply-draft path — which sends **diary text**,
the most sensitive thing in the store — goes through the same three steps instead of calling
the model directly.

A gate ticket is short-lived, single-use, bound to (lines, destination, model, prompts, diary
revisions) and stored in memory only; nothing about it is written to disk.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from dataclasses import dataclass, field

from rapport.config import PROMPTS_VERSION
from rapport.errors import Conflict, NotFound

TICKET_TTL_S = 15 * 60


@dataclass
class OutboundTicket:
    id: str
    purpose: str  # analysis | reply
    payload_hash: str
    destination: dict
    lines: list[str]
    diary_refs: list[dict]  # [{id, revision, kind, chars}]
    cost: dict
    created_at: float = field(default_factory=time.time)
    extra: dict = field(default_factory=dict)

    @property
    def expired(self) -> bool:
        return time.time() - self.created_at > TICKET_TTL_S

    def view(self) -> dict:
        return {
            "ticket_id": self.id,
            "purpose": self.purpose,
            "payload_hash": self.payload_hash,
            "destination": self.destination,
            "lines": self.lines,
            "n_lines": len(self.lines),
            "diary_refs": self.diary_refs,
            "n_diary_chars": sum(int(r.get("chars", 0)) for r in self.diary_refs),
            "cost": self.cost,
            "prompts_version": PROMPTS_VERSION,
            "expires_in_s": max(0, int(TICKET_TTL_S - (time.time() - self.created_at))),
            **self.extra,
        }


def payload_hash(lines: list[str] | list[list[str]], cfg: dict, price_date: str, diary_refs: list[dict] | None = None) -> str:
    """Hash of exactly what will be sent, so a confirmation cannot drift from the preview."""
    blob = {
        "lines": lines,
        "provider": cfg.get("preset"),
        "model": cfg.get("model"),
        "base_url": cfg.get("base_url"),
        "prompts": PROMPTS_VERSION,
        "price_date": price_date,
        "diary": [(r.get("id"), r.get("revision")) for r in (diary_refs or [])],
    }
    return hashlib.sha256(json.dumps(blob, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:32]


class OutboundGate:
    """In-memory ticket store. One process, one user; a restart simply invalidates open previews."""

    def __init__(self) -> None:
        self._tickets: dict[str, OutboundTicket] = {}
        self._lock = threading.Lock()

    def issue(self, purpose: str, lines: list[str], destination: dict, cost: dict, diary_refs: list[dict] | None = None, extra: dict | None = None) -> OutboundTicket:
        refs = list(diary_refs or [])
        t = OutboundTicket(
            id=secrets.token_hex(8),
            purpose=purpose,
            payload_hash=payload_hash(lines, destination, str(destination.get("price_date", "")), refs),
            destination=destination,
            lines=list(lines),
            diary_refs=refs,
            cost=dict(cost),
            extra=dict(extra or {}),
        )
        with self._lock:
            self._sweep()
            self._tickets[t.id] = t
        return t

    def redeem(self, ticket_id: str, presented_hash: str) -> OutboundTicket:
        """Consume a ticket. The hash the client presents must match what was previewed."""
        with self._lock:
            self._sweep()
            t = self._tickets.get(ticket_id)
            if t is None:
                raise NotFound("ticket_unknown", "预览已失效或不存在，请重新预览", "预览 15 分钟内有效，且只能确认一次。")
            if t.payload_hash != presented_hash:
                raise Conflict("hash_mismatch", "预览内容已变化，请重新预览后确认")
            del self._tickets[ticket_id]
            return t

    def _sweep(self) -> None:
        dead = [k for k, v in self._tickets.items() if v.expired]
        for k in dead:
            del self._tickets[k]
