"""Execution log (攻略 "我做了") and outcome check-ins (回访) — records, never scores.

Both tables carry user-reported facts about what happened after a report. They are shown back
to the user and fed to the *next* synthesis as context, but no scoring function reads them: a
test pins that `scoring/` has no import path to these mixins.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from rapport.db import DEFAULT_RELATIONSHIP_ID, ActionLog, Outcome, ReportRow

OBSERVED = ("accepted", "alt_time", "declined", "no_reply", "other")
OUTCOMES = ("better", "same", "worse", "ended", "unknown")
OUTCOME_ASK_AFTER_DAYS = 14


class ActionsMixin:
    def action_done(self, report_id: str, option_id: str, goal: str, observed: str, note: str = "") -> dict:
        from rapport.state import new_id

        if observed not in OBSERVED:
            raise ValueError(f"未知的对方反应 {observed}")
        with self.db.session() as s:
            rep = s.get(ReportRow, report_id)
            if rep is None or rep.deleted:
                raise KeyError(report_id)
            row = (
                s.query(ActionLog)
                .filter(ActionLog.report_id == report_id, ActionLog.option_id == option_id)
                .first()
            )
            if row is None:
                row = ActionLog(id=new_id(), relationship_id=rep.relationship_id or DEFAULT_RELATIONSHIP_ID, report_id=report_id, option_id=option_id[:40])
                s.add(row)
            row.goal = goal[:120]
            row.observed = observed
            row.note = (note or "")[:300]
            row.done_at = datetime.now()
            s.flush()
            return self._action_view(row)

    @staticmethod
    def _action_view(r: ActionLog) -> dict:
        return {
            "id": r.id,
            "relationship_id": r.relationship_id,
            "report_id": r.report_id,
            "option_id": r.option_id,
            "goal": r.goal,
            "observed": r.observed,
            "note": r.note,
            "done_at": r.done_at.isoformat() if r.done_at else None,
        }

    def actions_for_report(self, report_id: str) -> list[dict]:
        with self.db.session() as s:
            rows = s.query(ActionLog).filter(ActionLog.report_id == report_id).order_by(ActionLog.done_at).all()
        return [self._action_view(r) for r in rows]

    def actions_for_relationship(self, relationship_id: str) -> list[dict]:
        with self.db.session() as s:
            rows = s.query(ActionLog).filter(ActionLog.relationship_id == relationship_id).order_by(ActionLog.done_at.desc()).all()
        return [self._action_view(r) for r in rows]

    def actions_since(self, relationship_id: str, since_report_id: str | None) -> list[dict]:
        """What the user did after the previous report — the 'last time' review for the next synthesis."""
        if not since_report_id:
            return []
        with self.db.session() as s:
            rows = s.query(ActionLog).filter(ActionLog.relationship_id == relationship_id, ActionLog.report_id == since_report_id).order_by(ActionLog.done_at).all()
        return [self._action_view(r) for r in rows]

    def actions_drop_for_report(self, report_id: str) -> int:
        with self.db.session() as s:
            return s.query(ActionLog).filter(ActionLog.report_id == report_id).delete()

    # ---------- outcomes ----------
    def outcome_record(self, relationship_id: str, report_id: str | None, outcome: str, met_since: bool = False, note: str = "") -> dict:
        from rapport.state import new_id

        if outcome not in OUTCOMES:
            raise ValueError(f"未知的结果标签 {outcome}")
        with self.db.session() as s:
            row = Outcome(
                id=new_id(),
                relationship_id=relationship_id,
                report_id=report_id,
                asked_at=datetime.now(),
                answered_at=datetime.now(),
                outcome=outcome,
                met_since=bool(met_since),
                note=(note or "")[:300],
            )
            s.add(row)
            s.flush()
            return self._outcome_view(row)

    @staticmethod
    def _outcome_view(r: Outcome) -> dict:
        return {
            "id": r.id,
            "relationship_id": r.relationship_id,
            "report_id": r.report_id,
            "answered_at": r.answered_at.isoformat() if r.answered_at else None,
            "outcome": r.outcome,
            "met_since": bool(r.met_since),
            "note": r.note,
        }

    def outcomes_for_relationship(self, relationship_id: str) -> list[dict]:
        with self.db.session() as s:
            rows = s.query(Outcome).filter(Outcome.relationship_id == relationship_id).order_by(Outcome.answered_at.desc()).all()
        return [self._outcome_view(r) for r in rows]

    def outcome_prompt(self, relationship_id: str) -> dict | None:
        """The check-in the history page should show: the latest report older than the ask window
        that has no outcome recorded yet."""
        with self.db.session() as s:
            latest = (
                s.query(ReportRow)
                .filter(ReportRow.deleted.is_(False), ReportRow.relationship_id == relationship_id)
                .order_by(ReportRow.created_at.desc())
                .first()
            )
            if latest is None:
                return None
            if datetime.now() - latest.created_at < timedelta(days=OUTCOME_ASK_AFTER_DAYS):
                return None
            answered = s.query(Outcome).filter(Outcome.report_id == latest.id).count()
        if answered:
            return None
        return {
            "report_id": latest.id,
            "report_created_at": latest.created_at.isoformat(),
            "days_since": (datetime.now() - latest.created_at).days,
            "question": "距离上次分析已过了两周多。后来怎么样了？（只存本机，不进入任何分数）",
            "options": list(OUTCOMES),
        }
