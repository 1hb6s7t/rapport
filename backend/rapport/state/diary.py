"""Diary storage, the memory / pattern switches, and retrieve-then-authorise search."""

from __future__ import annotations

from datetime import UTC, datetime

from rapport.db import DiaryRow, Setting
from rapport.schemas.diary import DiaryEntry, DiaryKind, MemoryHit


class DiaryMixin:
    @staticmethod
    def _utc(value: datetime | None) -> datetime | None:
        """SQLite stores no timezone, so diary times are normalised to UTC on the way in and out."""
        if value is None:
            return None
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)

    @staticmethod
    def _utc_naive(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        aware = value if value.tzinfo else value.replace(tzinfo=UTC)
        return aware.astimezone(UTC).replace(tzinfo=None)

    @classmethod
    def _diary_entry(cls, row: DiaryRow) -> DiaryEntry:
        return DiaryEntry(
            id=row.id,
            revision=row.revision,
            text=row.text,
            recorded_at=cls._utc(row.recorded_at),
            occurred_at=cls._utc(row.occurred_at),
            kind=DiaryKind(row.kind),
            enabled=row.enabled,
            allowed_relationships=frozenset(row.allowed_relationships or []),
            approved_clause=row.approved_clause,
            deleted=row.deleted,
        )

    def diary_create(self, entry: DiaryEntry) -> DiaryEntry:
        with self.db.session() as s:
            s.add(
                DiaryRow(
                    id=entry.id,
                    revision=entry.revision,
                    text=entry.text,
                    kind=entry.kind.value,
                    enabled=entry.enabled,
                    recorded_at=self._utc_naive(entry.recorded_at),
                    occurred_at=self._utc_naive(entry.occurred_at),
                    allowed_relationships=sorted(entry.allowed_relationships),
                    approved_clause=entry.approved_clause,
                )
            )
        return entry

    def diary_get(self, entry_id: str, include_deleted: bool = False) -> DiaryEntry | None:
        with self.db.session() as s:
            row = s.get(DiaryRow, entry_id)
        if row is None or (row.deleted and not include_deleted):
            return None
        return self._diary_entry(row)

    def diary_list(self, include_deleted: bool = False) -> list[DiaryEntry]:
        with self.db.session() as s:
            q = s.query(DiaryRow)
            if not include_deleted:
                q = q.filter(DiaryRow.deleted.is_(False))
            rows = q.order_by(DiaryRow.recorded_at.desc()).all()
        return [self._diary_entry(r) for r in rows]

    def diary_store(self) -> dict[str, DiaryEntry]:
        return {e.id: e for e in self.diary_list(include_deleted=True)}

    def diary_update(self, entry_id: str, patch: dict) -> DiaryEntry | None:
        """Editing bumps the revision and invalidates any clause that no longer appears verbatim."""
        with self.db.session() as s:
            row = s.get(DiaryRow, entry_id)
            if row is None or row.deleted:
                return None
            if patch.get("text") is not None:
                row.text = patch["text"]
                row.revision += 1
                if row.approved_clause and row.approved_clause not in row.text:
                    row.approved_clause = None
            if patch.get("kind") is not None:
                kind = patch["kind"]
                row.kind = kind.value if isinstance(kind, DiaryKind) else DiaryKind(kind).value
            if patch.get("occurred_at") is not None:
                row.occurred_at = self._utc_naive(patch["occurred_at"])
            if patch.get("allowed_relationships") is not None:
                row.allowed_relationships = sorted(patch["allowed_relationships"])
            if patch.get("approved_clause") is not None:
                clause = patch["approved_clause"]
                if clause and clause not in row.text:
                    raise ValueError("批准的短句必须逐字出现在日记原文中")
                row.approved_clause = clause or None
            if patch.get("enabled") is not None:
                row.enabled = bool(patch["enabled"])
            row.updated_at = datetime.now()
            return self._diary_entry(row)

    def diary_set_enabled(self, entry_id: str, enabled: bool) -> bool:
        with self.db.session() as s:
            row = s.get(DiaryRow, entry_id)
            if row is None or row.deleted:
                return False
            row.enabled = enabled
            return True

    def diary_delete(self, entry_id: str) -> bool:
        """Tombstone: the entry must be unusable immediately, not merely hidden from lists."""
        with self.db.session() as s:
            row = s.get(DiaryRow, entry_id)
            if row is None:
                return False
            row.deleted = True
            row.enabled = False
            row.approved_clause = None
            row.revision += 1
        return True

    def diary_clear(self) -> int:
        with self.db.session() as s:
            rows = s.query(DiaryRow).filter(DiaryRow.deleted.is_(False)).all()
            for row in rows:
                row.deleted = True
                row.enabled = False
                row.approved_clause = None
                row.revision += 1
        return len(rows)

    def diary_detach_relationship(self, relationship_id: str) -> int:
        """Remove one relationship from every entry's scope (used when the relationship is deleted)."""
        n = 0
        with self.db.session() as s:
            for row in s.query(DiaryRow).all():
                scope = list(row.allowed_relationships or [])
                if relationship_id in scope:
                    row.allowed_relationships = [r for r in scope if r != relationship_id]
                    row.revision += 1
                    n += 1
        return n

    # ---------- switches ----------
    def _flag(self, key: str, default: bool) -> bool:
        with self.db.session() as s:
            row = s.get(Setting, key)
            if row and isinstance(row.value, dict) and "enabled" in row.value:
                return bool(row.value["enabled"])
        return default

    def _set_flag(self, key: str, enabled: bool) -> None:
        with self.db.session() as s:
            row = s.get(Setting, key) or Setting(key=key, value={})
            row.value = {"enabled": bool(enabled)}
            s.merge(row)

    def memory_enabled_by_default(self) -> bool:
        return self._flag("memory", True)

    def set_memory_enabled(self, enabled: bool) -> None:
        self._set_flag("memory", enabled)

    def intent_samples(self) -> int:
        with self.db.session() as s:
            row = s.get(Setting, "intent.samples")
            v = row.value.get("k") if row and isinstance(row.value, dict) else None
        return int(v) if isinstance(v, int) and 1 <= v <= 5 else 1

    def set_intent_samples(self, k: int) -> None:
        with self.db.session() as s:
            row = s.get(Setting, "intent.samples") or Setting(key="intent.samples", value={})
            row.value = {"k": max(1, min(5, int(k)))}
            s.merge(row)

    def cues_enabled(self) -> bool:
        return self._flag("intent.cues", False)

    def set_cues_enabled(self, enabled: bool) -> None:
        self._set_flag("intent.cues", enabled)

    def patterns_enabled(self) -> bool:
        """Pragmatic-pattern layer switch (default off; docs/EVAL.md §14). Persisted, unlike the
        env-only setting, so the UI switch survives a restart."""
        return self._flag("patterns", self.settings.use_intent_patterns)

    def set_patterns_enabled(self, enabled: bool) -> None:
        self._set_flag("patterns", enabled)

    # ---------- search ----------
    def diary_search(
        self,
        query: str,
        relationship_id: str,
        enabled: bool,
        as_of: datetime | None = None,
        limit: int = 3,
    ) -> dict:
        """Retrieve then authorise. A closed switch never touches the store's candidates."""
        from rapport.diary.authorize import authorize_memories, retrieve_if_enabled
        from rapport.knowledge.reference import char_bigrams

        def retriever():
            q = char_bigrams(query)
            out: list[MemoryHit] = []
            for entry in self.diary_list():
                target = char_bigrams(entry.text)
                if not q or not target:
                    continue
                score = len(q & target) / (len(q) ** 0.5 * len(target) ** 0.5)
                if score > 0:
                    out.append(MemoryHit(entry_id=entry.id, revision=entry.revision, similarity=score))
            out.sort(key=lambda h: (-h.similarity, h.entry_id))
            return out[: max(limit * 4, 12)]

        hits = retrieve_if_enabled(enabled, retriever)
        result = authorize_memories(
            hits, self.diary_store(), relationship_id, as_of or datetime.now().astimezone(), enabled, limit
        )
        return {
            "enabled": enabled,
            "entries": [e.to_record() for e in result.entries],
            "dropped": result.dropped,
            "note": "检索结果必须回查当前状态：已修改、已关闭、已删除、超出关系范围或来自未来的日记都不会进入草稿。",
        }
