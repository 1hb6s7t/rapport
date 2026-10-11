"""Raw material, canonical events and the evidence ledger."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from rapport.db import DEFAULT_RELATIONSHIP_ID, Event, Import
from rapport.schemas.message_v1 import Conversation, Message


class ImportsMixin:
    def save_import(
        self,
        conv: Conversation,
        parser_id: str,
        filename: str | None,
        relationship_id: str = DEFAULT_RELATIONSHIP_ID,
    ) -> Import:
        from rapport.state import new_id

        relationship_id = self.resolve_relationship_id(relationship_id)
        blob_id = new_id()
        self.blobs.put(blob_id, conv.model_dump_json().encode())
        imp = Import(
            id=conv.id,
            relationship_id=relationship_id,
            source=conv.source.value,
            parser_id=parser_id,
            filename=filename,
            n_messages=len(conv.messages),
            blob_id=blob_id,
            warnings=[w.model_dump() for w in conv.warnings],
            participants=conv.participants.model_dump(),
            range_start=0,
            range_end=max(0, len(conv.messages) - 1),
            expires_at=datetime.now() + timedelta(hours=self.settings.raw_retention_hours),
        )
        with self.db.session() as s:
            s.add(imp)
        self.record_events(imp, conv)
        return imp

    def load_conversation(self, imp: Import) -> Conversation | None:
        raw = self.blobs.get(imp.blob_id)
        if raw is None:
            return None
        return Conversation.model_validate_json(raw)

    def store_conversation(self, imp: Import, conv: Conversation) -> None:
        self.blobs.put(imp.blob_id, conv.model_dump_json().encode())

    @staticmethod
    def event_fingerprint(msg: Message, occurrence: int = 0, relationship_id: str = DEFAULT_RELATIONSHIP_ID) -> str:
        """Stable content fingerprint of one observed event, independent of import id.

        `occurrence` disambiguates genuinely identical messages (e.g. two real "哈哈" inside the
        same minute) without breaking dedupe across identical re-imports. A non-default
        relationship is folded in so two different people saying "早" untimed never share one
        ledger row; the default relationship keeps the pre-v0.4 fingerprints unchanged.
        """
        base = "|".join(
            [
                msg.sender.value,
                msg.ts.isoformat() if msg.ts else "",
                msg.ts_precision.value,
                msg.type.value,
                (msg.text or "").strip(),
            ]
        )
        scope = "" if relationship_id == DEFAULT_RELATIONSHIP_ID else f"|{relationship_id}"
        return hashlib.sha1(f"{base}|{occurrence}{scope}".encode()).hexdigest()[:24]

    def record_events(self, imp: Import, conv: Conversation, gap_hours: float | None = None) -> int:
        """Upsert canonical events for one import. Returns the number of NEW events.

        Re-importing the same material adds no new rows (only bumps `seen_count`).
        """
        from rapport.features.sessions import split_sessions

        relationship_id = getattr(imp, "relationship_id", None) or DEFAULT_RELATIONSHIP_ID
        events = conv.events()
        sessions = split_sessions(events, gap_hours if gap_hours is not None else self.settings.session_gap_hours)
        episode_of: dict[str, str] = {}
        for sess in sessions:
            for m in sess.messages:
                episode_of[m.id] = f"{conv.id}:s{sess.index}"
        seen: dict[str, int] = {}
        new_count = 0
        with self.db.session() as s:
            for m in events:
                base_key = f"{m.sender.value}|{m.ts.isoformat() if m.ts else ''}|{m.type.value}|{(m.text or '').strip()}"
                occ = seen.get(base_key, 0)
                seen[base_key] = occ + 1
                fp = self.event_fingerprint(m, occ, relationship_id)
                row = s.get(Event, fp)
                if row is None:
                    s.add(
                        Event(
                            fingerprint=fp,
                            relationship_id=relationship_id,
                            first_import_id=imp.id,
                            msg_id=m.id,
                            sender=m.sender.value,
                            type=m.type.value,
                            ts=m.ts,
                            ts_precision=m.ts_precision.value,
                            episode_id=episode_of.get(m.id, ""),
                            origin="observed_chat",
                            confirmed=False,
                            seen_count=1,
                        )
                    )
                    new_count += 1
                else:
                    row.seen_count += 1
        return new_count

    def rebuild_events(self, imp: Import, conv: Conversation) -> int:
        """Drop this import's first-seen events and record them again from the edited conversation.

        Events first seen by *another* import are left alone (they describe that material)."""
        with self.db.session() as s:
            s.query(Event).filter(Event.first_import_id == imp.id).delete()
        return self.record_events(imp, conv)

    def confirm_events(self, import_id: str) -> int:
        """Mark this import's canonical events as confirmed — the only path that grows N_eff."""
        with self.db.session() as s:
            rows = s.query(Event).filter(Event.first_import_id == import_id).all()
            for r in rows:
                r.confirmed = True
        return len(rows)

    def ledger_n_eff(self, relationship_id: str | None = DEFAULT_RELATIONSHIP_ID) -> float:
        """Effective evidence units for one relationship, from confirmed events only.

        `None` sums every relationship (only for diagnostics; never feed it to a report).
        """
        from rapport.scoring.evidence import n_eff_from_events

        with self.db.session() as s:
            q = s.query(Event)
            if relationship_id is not None:
                q = q.filter(Event.relationship_id == relationship_id)
            rows = q.all()
        return n_eff_from_events(rows)

    def ledger_breakdown(self, relationship_id: str | None = DEFAULT_RELATIONSHIP_ID) -> dict[str, int]:
        """Confirmed events split into countable text / unread media / other (diagnostic)."""
        from rapport.scoring.evidence import evidence_breakdown

        with self.db.session() as s:
            q = s.query(Event)
            if relationship_id is not None:
                q = q.filter(Event.relationship_id == relationship_id)
            rows = q.all()
        return evidence_breakdown(rows)

    def import_relationship(self, import_id: str) -> str:
        with self.db.session() as s:
            imp = s.get(Import, import_id)
        return (imp.relationship_id if imp else None) or DEFAULT_RELATIONSHIP_ID

    @staticmethod
    def content_hash(conv: Conversation, start: int, end: int) -> str:
        h = hashlib.sha256()
        for m in conv.messages[start : end + 1]:
            h.update(
                f"{m.id}|{m.sender}|{m.ts.isoformat() if m.ts else ''}|{m.type}|{m.text or ''}|{m.quoted_text or ''}\n".encode()
            )
        return h.hexdigest()[:32]
