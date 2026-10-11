"""Startup reconcile, retention purge and wipe-all."""

from __future__ import annotations

from datetime import datetime

from rapport.db import (
    DEFAULT_RELATIONSHIP_ID,
    ActionLog,
    Analysis,
    DiaryRow,
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
from rapport.jobs.status import ACTIVE, NEEDS_MATERIAL, AnalysisStatus


class MaintenanceMixin:
    def reconcile_on_startup(self) -> dict:
        """Mark stale running analyses interrupted; purge expired raw blobs."""
        out = {"interrupted": 0, "purged": 0}
        with self.db.session() as s:
            for a in s.query(Analysis).filter(Analysis.status.in_(sorted(ACTIVE))).all():
                a.status = AnalysisStatus.interrupted.value
                a.message = "服务重启时任务被中断；可重试（已完成的分段会复用）。"
                out["interrupted"] += 1
        out["purged"] = self.purge_expired()
        return out

    def purge_expired(self) -> int:
        now = datetime.now()
        n = 0
        with self.db.session() as s:
            expired = s.query(Import).filter(Import.deleted.is_(False), Import.expires_at.isnot(None), Import.expires_at < now).all()
            for imp in expired:
                # only purge if no analysis is still waiting on it
                active = (
                    s.query(Analysis)
                    .filter(Analysis.import_id == imp.id, Analysis.status.in_(sorted(NEEDS_MATERIAL)))
                    .count()
                )
                if active:
                    continue
                self.blobs.delete(imp.blob_id)
                imp.deleted = True
                imp.status = "deleted"
                n += 1
        return n

    def wipe_all(self) -> dict:
        """Delete every import, analysis, report, ledger row, feedback and blob. Sessions revoked."""
        counts: dict[str, int] = {"imports": 0, "analyses": 0, "reports": 0, "blobs": 0}
        self.cleanup_personalization()
        with self.db.session() as s:
            for imp in s.query(Import).all():
                if self.blobs.delete(imp.blob_id):
                    counts["blobs"] += 1
                counts["imports"] += 1
            counts["analyses"] = s.query(Analysis).count()
            counts["reports"] = s.query(ReportRow).count()
            counts["snapshots"] = s.query(Snapshot).count()
            counts["persona_versions"] = s.query(PersonaVersion).count()
            counts["events"] = s.query(Event).count()
            counts["diary"] = s.query(DiaryRow).count()
            counts["llm_calls"] = s.query(LLMCall).count()
            counts["feedback"] = s.query(Feedback).count()
            counts["action_log"] = s.query(ActionLog).count()
            counts["outcomes"] = s.query(Outcome).count()
            counts["nudges"] = s.query(Nudge).count()
            counts["relationships"] = s.query(Relationship).filter(Relationship.id != DEFAULT_RELATIONSHIP_ID).count()
            # every derived store goes: bodies, history points, portrait versions, the evidence
            # ledger, the call ledger, feedback and the diary. Nothing here holds raw text except
            # the diary, which is the user's own and is cleared by the same action.
            for model in (ReportRow, Snapshot, PersonaVersion, Analysis, Event, DiaryRow, LLMCall, Feedback, ActionLog, Outcome, Nudge, Import):
                s.query(model).delete()
            # drive state lives in the settings KV under its own prefix
            for row in s.query(Setting).filter(Setting.key.like("drive.state.%")).all():
                s.delete(row)
            s.query(Relationship).filter(Relationship.id != DEFAULT_RELATIONSHIP_ID).delete()
            default = s.get(Relationship, DEFAULT_RELATIONSHIP_ID)
            if default:
                default.labels = {}
            own_profile = s.get(Setting, "profile.self.v1")
            if own_profile:
                s.delete(own_profile)
        for p in self.settings.blob_dir.glob("*.bin"):
            p.unlink(missing_ok=True)
            counts["blobs"] += 1
        self.wechat_captures.clear()
        self.wechat_history.clear()
        return counts
