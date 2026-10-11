"""Cross-import history: snapshots, portrait versions, stage history, last report.

Every query here is scoped to one relationship. Mixing two people's history into one curve
was the single biggest structural gap of v0.3, so the scope parameter is not optional in the
sense that omitting it means "the default relationship", never "everything".
"""

from __future__ import annotations

import hashlib

from rapport.db import DEFAULT_RELATIONSHIP_ID, PersonaVersion, ReportRow, Snapshot


class HistoryMixin:
    # ---------- snapshots ----------
    def save_snapshot(self, report, import_id: str, material_hash: str, n_eff: float, relationship_id: str | None = None) -> str | None:
        """Record one comparable history point. Returns the snapshot id, or None if it already existed.

        The key is (relationship, material, rubric, features, provider, model): re-running the
        same material under the same versions writes nothing, so a curve never repeats a point.
        """
        relationship_id = relationship_id or self.import_relationship(import_id)
        key = "|".join(
            [
                relationship_id if relationship_id != DEFAULT_RELATIONSHIP_ID else "",
                material_hash,
                report.versions.rubric,
                report.versions.features,
                report.versions.provider or "",
                report.versions.model or "",
            ]
        )
        snap_id = hashlib.sha1(key.encode()).hexdigest()[:32]
        with self.db.session() as s:
            if s.get(Snapshot, snap_id) is not None:
                return None
            s.add(
                Snapshot(
                    id=snap_id,
                    relationship_id=relationship_id,
                    import_id=import_id,
                    report_id=report.id,
                    material_hash=material_hash,
                    rubric_version=report.versions.rubric,
                    features_version=report.versions.features,
                    provider=report.versions.provider,
                    model=report.versions.model,
                    total=report.affinity.total,
                    coverage=report.affinity.coverage,
                    n_eff=n_eff,
                    dims={k: v.score for k, v in report.affinity.dims.items()},
                    note="boundary"
                    if report.affinity.suppressed_reason
                    else ("insufficient" if report.affinity.total is None else ""),
                )
            )
        return snap_id

    def drop_snapshots_for(self, report_id: str) -> int:
        """Remove the history points derived from one report (deleting a report deletes its numbers)."""
        with self.db.session() as s:
            rows = s.query(Snapshot).filter(Snapshot.report_id == report_id).all()
            for r in rows:
                s.delete(r)
        return len(rows)

    def trend_series(self, relationship_id: str = DEFAULT_RELATIONSHIP_ID) -> dict:
        """Ordered snapshots plus the comparability rule: a version change breaks the line."""
        with self.db.session() as s:
            rows = s.query(Snapshot).filter(Snapshot.relationship_id == relationship_id).order_by(Snapshot.created_at).all()
        points = []
        latest = (rows[-1].rubric_version, rows[-1].features_version, rows[-1].provider, rows[-1].model) if rows else None
        for r in rows:
            same_versions = (r.rubric_version, r.features_version, r.provider, r.model) == latest
            points.append(
                {
                    "id": r.id,
                    "created_at": r.created_at.isoformat(),
                    "report_id": r.report_id,
                    "total": r.total,
                    "coverage": r.coverage,
                    "n_eff": r.n_eff,
                    "dims": r.dims,
                    "comparable": same_versions,
                    "note": r.note,
                }
            )
        return {
            "relationship_id": relationship_id,
            "points": points,
            "comparable_with_latest": sum(1 for p in points if p["comparable"]),
            "note": "只有版本一致的点才可直接连线；规则或模型变更会产生断点，不会静默连成一条曲线。",
        }

    # ---------- portrait versions ----------
    def save_persona_version(self, report, relationship_id: str = DEFAULT_RELATIONSHIP_ID) -> int:
        """Append a portrait revision, carrying the reasons it differs from the last one.

        Returns the new version number, or 0 when the report has no portrait to version
        (rules mode, or insufficient material).
        """
        from rapport.report.persona_versions import diff_persona
        from rapport.schemas.report_v1 import Persona, PersonaChangeModel

        if not report.persona.available:
            return 0
        with self.db.session() as s:
            prev_row = (
                s.query(PersonaVersion)
                .filter(PersonaVersion.active.is_(True), PersonaVersion.relationship_id == relationship_id)
                .order_by(PersonaVersion.version.desc())
                .first()
            )
            previous = None
            if prev_row is not None:
                previous = Persona(
                    tendencies={k: v for k, v in (prev_row.tendencies or {}).items()},
                    traits=prev_row.traits or [],
                    inferred_axes={k: v for k, v in (prev_row.axes or {}).items()},
                    available=True,
                )
            changes = diff_persona(previous, report.persona)
            version = (prev_row.version + 1) if prev_row else 1
            from rapport.report.persona_versions import pole_stability

            recent = (
                s.query(PersonaVersion)
                .filter(PersonaVersion.relationship_id == relationship_id)
                .order_by(PersonaVersion.version.desc())
                .limit(3)
                .all()
            )
            report.persona.pole_stability = pole_stability([r.tendencies or {} for r in recent], report.persona.tendencies)
            new_id = hashlib.sha1(f"{report.id}|{version}".encode()).hexdigest()[:32]
            s.add(
                PersonaVersion(
                    id=new_id,
                    relationship_id=relationship_id,
                    report_id=report.id,
                    version=version,
                    active=True,
                    superseded_by=None,
                    tendencies={k: v.model_dump(mode="json") for k, v in report.persona.tendencies.items()},
                    traits=[t.model_dump(mode="json") for t in report.persona.traits],
                    axes={k: v.model_dump(mode="json") for k, v in report.persona.inferred_axes.items()},
                    change_notes=[c.as_dict() for c in changes],
                )
            )
            if prev_row is not None:
                prev_row.active = False
                prev_row.superseded_by = new_id
        report.persona.version = version
        report.persona.changed_from_report = prev_row.report_id if prev_row is not None else None
        report.persona.change_notes = [PersonaChangeModel(**c.as_dict()) for c in changes]
        return version

    def persona_history(self, relationship_id: str = DEFAULT_RELATIONSHIP_ID) -> dict:
        """Newest-first portrait revisions; every entry keeps its content and change notes."""
        with self.db.session() as s:
            rows = (
                s.query(PersonaVersion)
                .filter(PersonaVersion.relationship_id == relationship_id)
                .order_by(PersonaVersion.version.desc())
                .all()
            )
        return {
            "relationship_id": relationship_id,
            "versions": [
                {
                    "version": r.version,
                    "report_id": r.report_id,
                    "created_at": r.created_at.isoformat(),
                    "active": r.active,
                    "superseded_by": r.superseded_by,
                    "tendencies": {k: v.get("label", "") for k, v in (r.tendencies or {}).items()},
                    "traits": [t.get("statement", "") for t in (r.traits or [])],
                    "change_notes": r.change_notes or [],
                }
                for r in rows
            ],
            "note": "旧版本保留内容并且不再生效；删除对应报告会让该版本失效。",
        }

    def deactivate_persona_versions(self, report_id: str) -> int:
        with self.db.session() as s:
            rows = s.query(PersonaVersion).filter(PersonaVersion.report_id == report_id).all()
            for r in rows:
                r.active = False
        return len(rows)

    # ---------- reports as history ----------
    def _latest_report_row(self, s, relationship_id: str):
        return (
            s.query(ReportRow)
            .filter(ReportRow.deleted.is_(False), ReportRow.relationship_id == relationship_id)
            .order_by(ReportRow.created_at.desc())
            .first()
        )

    def last_report(self, relationship_id: str = DEFAULT_RELATIONSHIP_ID) -> tuple[str | None, dict | None]:
        """(report_id, body) of the most recent live report, for change comparisons."""
        with self.db.session() as s:
            row = self._latest_report_row(s, relationship_id)
        if row is None or not isinstance(row.body, dict):
            return None, None
        return row.id, row.body

    def last_stage(self, relationship_id: str = DEFAULT_RELATIONSHIP_ID) -> str | None:
        """The stage of the most recent report, so a new run can say how it moved."""
        _, body = self.last_report(relationship_id)
        if not body:
            return None
        return (body.get("stage") or {}).get("value")

    def stage_history(self, relationship_id: str = DEFAULT_RELATIONSHIP_ID) -> dict:
        """Every recorded stage with its transition note, newest first."""
        with self.db.session() as s:
            rows = (
                s.query(ReportRow)
                .filter(ReportRow.deleted.is_(False), ReportRow.relationship_id == relationship_id)
                .order_by(ReportRow.created_at.desc())
                .all()
            )
        points = []
        for r in rows:
            body = r.body if isinstance(r.body, dict) else {}
            stage = body.get("stage") or {}
            points.append(
                {
                    "report_id": r.id,
                    "created_at": r.created_at.isoformat(),
                    "value": stage.get("value"),
                    "change": stage.get("change"),
                }
            )
        return {
            "relationship_id": relationship_id,
            "points": points,
            "note": "阶段是描述而非等级：允许回退，且回退会被明确记录；跨版本比较请以快照的可比性为准。",
        }
