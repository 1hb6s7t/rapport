"""关系档案 — the entity that scopes evidence, history, reports and feedback."""

from __future__ import annotations

from datetime import datetime

from rapport.db import (
    DEFAULT_RELATIONSHIP_ID,
    ActionLog,
    Analysis,
    Event,
    Feedback,
    Import,
    LLMCall,
    Nudge,
    Outcome,
    PersonaVersion,
    Relationship,
    ReportRow,
    Setting,
    Snapshot,
)
from rapport.errors import NotFound
from rapport.schemas.profile import Person, Profile

LABEL_KEYS = ("mbti", "zodiac", "chinese_zodiac")
SELF_PROFILE_KEY = "profile.self.v1"


class RelationshipNotFound(NotFound, KeyError):
    def __init__(self, rid: str):
        NotFound.__init__(self, "relationship_not_found", f"对象 {rid} 不存在", "请先在「历史」页创建该对象，或留空使用默认对象。")
        self.rid = rid


class RelationshipsMixin:
    def ensure_default_relationship(self) -> None:
        with self.db.session() as s:
            if s.get(Relationship, DEFAULT_RELATIONSHIP_ID) is None:
                s.add(Relationship(id=DEFAULT_RELATIONSHIP_ID, name="默认对象", note="首次使用自动创建；可改名。"))

    def resolve_relationship_id(self, relationship_id: str | None) -> str:
        """Blank means the default relationship; an unknown id is an error, never a silent new scope."""
        rid = (relationship_id or "").strip() or DEFAULT_RELATIONSHIP_ID
        with self.db.session() as s:
            row = s.get(Relationship, rid)
        if row is None:
            raise RelationshipNotFound(rid)
        return rid

    @staticmethod
    def _view(row: Relationship, counts: dict[str, int] | None = None) -> dict:
        person = RelationshipsMixin._person(row)
        return {
            "id": row.id,
            "name": row.name,
            "note": row.note,
            "labels": {k: (row.labels or {}).get(k) for k in LABEL_KEYS},
            "person": person.model_dump(mode="json"),
            "profile_revision": person.revision,
            "archived": bool(row.archived),
            "is_default": row.id == DEFAULT_RELATIONSHIP_ID,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            "counts": counts or {},
        }

    def relationship_counts(self, relationship_id: str) -> dict[str, int]:
        with self.db.session() as s:
            return {
                "imports": s.query(Import).filter(Import.relationship_id == relationship_id, Import.deleted.is_(False)).count(),
                "reports": s.query(ReportRow).filter(ReportRow.relationship_id == relationship_id, ReportRow.deleted.is_(False)).count(),
                "snapshots": s.query(Snapshot).filter(Snapshot.relationship_id == relationship_id).count(),
                "events_confirmed": s.query(Event).filter(Event.relationship_id == relationship_id, Event.confirmed.is_(True)).count(),
                "feedback": s.query(Feedback).filter(Feedback.relationship_id == relationship_id).count(),
            }

    def relationship_list(self, include_archived: bool = False) -> list[dict]:
        with self.db.session() as s:
            q = s.query(Relationship)
            if not include_archived:
                q = q.filter(Relationship.archived.is_(False))
            rows = q.order_by(Relationship.created_at).all()
        return [self._view(r, self.relationship_counts(r.id)) for r in rows]

    def relationship_get(self, relationship_id: str) -> dict | None:
        with self.db.session() as s:
            row = s.get(Relationship, relationship_id)
        if row is None:
            return None
        return self._view(row, self.relationship_counts(row.id))

    def relationship_create(self, name: str, note: str = "", labels: dict | None = None, relationship_id: str | None = None) -> dict:
        from rapport.state import new_id

        rid = (relationship_id or "").strip() or new_id()
        with self.db.session() as s:
            if s.get(Relationship, rid) is not None:
                raise FileExistsError(rid)
            s.add(Relationship(id=rid, name=name.strip()[:60], note=note.strip()[:255], labels=self._clean_labels(labels)))
        return self.relationship_get(rid)  # type: ignore[return-value]

    def relationship_update(self, relationship_id: str, patch: dict) -> dict | None:
        with self.db.session() as s:
            row = s.get(Relationship, relationship_id)
            if row is None:
                return None
            if patch.get("name") is not None:
                row.name = str(patch["name"]).strip()[:60]
            if patch.get("note") is not None:
                row.note = str(patch["note"]).strip()[:255]
            if patch.get("labels") is not None:
                old = self._person(row)
                labels = self._clean_labels(patch["labels"])
                person = Person.model_validate(old.model_dump(mode="json") | labels)
                self._store_person(row, person)
            if patch.get("archived") is not None and row.id != DEFAULT_RELATIONSHIP_ID:
                row.archived = bool(patch["archived"])
            row.updated_at = datetime.now()
        return self.relationship_get(relationship_id)

    @staticmethod
    def _person(row: Relationship) -> Person:
        labels = row.labels or {}
        saved = dict(labels.get("_profile") or {})
        # Legacy object settings remain compatible and cannot wipe birthday metadata.
        saved.update({k: labels.get(k) for k in LABEL_KEYS})
        saved.setdefault("alias", row.name)
        saved["revision"] = int(labels.get("_profile_revision", 0))
        return Person.model_validate(saved)

    @staticmethod
    def _store_person(row: Relationship, person: Person) -> Person:
        old = RelationshipsMixin._person(row)
        data = person.model_dump(mode="json", exclude={"revision"})
        revision = old.revision + int(data != old.model_dump(mode="json", exclude={"revision"}))
        saved = Person.model_validate(data | {"revision": revision})
        row.labels = {k: data[k] for k in LABEL_KEYS} | {
            "_profile": data, "_profile_revision": revision,
        }
        return saved

    def self_profile(self) -> Person:
        with self.db.session() as s:
            row = s.get(Setting, SELF_PROFILE_KEY)
            return Person.model_validate(row.value if row else {})

    def save_self_profile(self, person: Person) -> Person:
        with self.db.session() as s:
            row = s.get(Setting, SELF_PROFILE_KEY)
            old = Person.model_validate(row.value if row else {})
            self._check_revision(person, old)
            data = person.model_dump(mode="json", exclude={"revision"})
            data["revision"] = old.revision + int(data != old.model_dump(mode="json", exclude={"revision"}))
            saved = Person.model_validate(data)
            if row is None:
                s.add(Setting(key=SELF_PROFILE_KEY, value=saved.model_dump(mode="json")))
            else:
                row.value = saved.model_dump(mode="json")
            return saved

    @staticmethod
    def _check_revision(person: Person, old: Person) -> None:
        if "revision" in person.model_fields_set and person.revision != old.revision:
            from rapport.errors import RapportError

            raise RapportError("profile_revision_conflict", "人物档案已更新，请重新载入后再保存。", status=409)

    def relationship_person(self, relationship_id: str) -> Person:
        with self.db.session() as s:
            row = s.get(Relationship, relationship_id)
            if row is None:
                raise RelationshipNotFound(relationship_id)
            return self._person(row)

    def save_relationship_person(self, relationship_id: str, person: Person) -> Person:
        with self.db.session() as s:
            row = s.get(Relationship, relationship_id)
            if row is None:
                raise RelationshipNotFound(relationship_id)
            self._check_revision(person, self._person(row))
            saved = self._store_person(row, person)
            row.updated_at = datetime.now()
            return saved

    def profile_snapshot(self, relationship_id: str, supplied: Profile | None = None) -> Profile:
        """Resolve one friend explicitly; partial legacy requests retain durable metadata."""
        base = Profile(me=self.self_profile(), other=self.relationship_person(relationship_id))
        if supplied is None:
            return base
        updates = supplied.model_dump(exclude_unset=True, exclude={"me", "other"})
        for who in ("me", "other"):
            if who not in supplied.model_fields_set:
                continue
            person = getattr(supplied, who)
            durable = getattr(base, who)
            self._check_revision(person, durable)
            updates[who] = Person.model_validate(
                durable.model_dump(mode="json") | person.model_dump(mode="json", exclude_unset=True)
            )
        return base.model_copy(update=updates)

    def relationship_delete(self, relationship_id: str) -> dict:
        """Cascade: every derived row of this relationship goes; the default cannot be deleted."""
        if relationship_id == DEFAULT_RELATIONSHIP_ID:
            raise ValueError("默认对象不能删除，只能清空数据")
        counts: dict[str, int] = {}
        with self.db.session() as s:
            row = s.get(Relationship, relationship_id)
            if row is None:
                raise RelationshipNotFound(relationship_id)
            for imp in s.query(Import).filter(Import.relationship_id == relationship_id).all():
                self.blobs.delete(imp.blob_id)
            for model in (ReportRow, Snapshot, PersonaVersion, Analysis, Event, Feedback, LLMCall, ActionLog, Outcome, Nudge, Import):
                counts[model.__tablename__] = s.query(model).filter(model.relationship_id == relationship_id).delete()
            s.delete(row)
        counts["diary_detached"] = self.diary_detach_relationship(relationship_id)
        self.nudges_drop_for_relationship(relationship_id)
        self.wechat_captures.discard_relationship(relationship_id)
        self.wechat_history.clear(relationship_id)
        cleanup = getattr(self, "cleanup_personalization", None)
        if callable(cleanup):
            cleanup(relationship_id)
        return counts

    @staticmethod
    def _clean_labels(labels: dict | None) -> dict:
        if not labels:
            return dict.fromkeys(LABEL_KEYS)
        person = Person(**{k: labels.get(k) for k in LABEL_KEYS if labels.get(k)})
        return {
            "mbti": person.mbti,
            "zodiac": person.zodiac.value if person.zodiac else None,
            "chinese_zodiac": person.chinese_zodiac.value if person.chinese_zodiac else None,
        }
