"""DriveMixin: persisted drive state per relationship, the hourly tick, and the inbox.

State lives in the `settings` KV table under `drive.state.<rid>` so it survives restarts and is
wiped with everything else. Nudges live in their own table. The scheduler is one call per minute
from the job runner; `drive_tick(now)` is idempotent per hour, so restarts and test clocks are safe.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from rapport.db import (
    DEFAULT_RELATIONSHIP_ID,
    ActionLog,
    DiaryRow,
    Nudge,
    Outcome,
    Relationship,
    ReportRow,
    Setting,
)
from rapport.drive import DEFAULT, DriveParams, drive_digest
from rapport.drive.actuators import InboxActuator
from rapport.drive.engine import Signals, empty_state, explain, record_nudge, tick
from rapport.drive.nudges import build

log = logging.getLogger("rapport.drive")

STATUSES = ("unread", "read", "dismissed", "acted")
GLOBAL_KEY = "drive.config"
STATE_PREFIX = "drive.state."


class DriveMixin:
    params: DriveParams = DEFAULT

    # ---------- config ----------
    def drive_config(self) -> dict:
        with self.db.session() as s:
            row = s.get(Setting, GLOBAL_KEY)
            v = dict(row.value) if row and isinstance(row.value, dict) else {}
        return {
            "enabled": bool(v.get("enabled", False)),
            "llm_drafts": bool(v.get("llm_drafts", False)),
            "daily_cap": int(v.get("daily_cap", self.params.daily_cap)),
            "quiet_start_h": int(v.get("quiet_start_h", self.params.quiet_start_h)),
            "quiet_end_h": int(v.get("quiet_end_h", self.params.quiet_end_h)),
            "disabled_relationships": list(v.get("disabled_relationships", [])),
            "version": drive_digest(self._params()),
        }

    def drive_set_config(self, patch: dict) -> dict:
        allowed = {"enabled", "llm_drafts", "daily_cap", "quiet_start_h", "quiet_end_h", "disabled_relationships"}
        with self.db.session() as s:
            row = s.get(Setting, GLOBAL_KEY) or Setting(key=GLOBAL_KEY, value={})
            v = dict(row.value or {})
            for k, val in patch.items():
                if k in allowed and val is not None:
                    v[k] = val
            if "daily_cap" in v:
                v["daily_cap"] = max(0, min(3, int(v["daily_cap"])))
            row.value = v
            s.merge(row)
        return self.drive_config()

    def _params(self) -> DriveParams:
        with self.db.session() as s:
            row = s.get(Setting, GLOBAL_KEY)
            v = dict(row.value) if row and isinstance(row.value, dict) else {}
        p = self.params
        if not v:
            return p
        from dataclasses import replace

        return replace(
            p,
            daily_cap=max(0, min(3, int(v.get("daily_cap", p.daily_cap)))),
            quiet_start_h=int(v.get("quiet_start_h", p.quiet_start_h)),
            quiet_end_h=int(v.get("quiet_end_h", p.quiet_end_h)),
        )

    # ---------- state ----------
    def drive_state(self, relationship_id: str) -> dict:
        with self.db.session() as s:
            row = s.get(Setting, STATE_PREFIX + relationship_id)
            return dict(row.value) if row and isinstance(row.value, dict) else empty_state()

    def _drive_save(self, relationship_id: str, st: dict) -> None:
        with self.db.session() as s:
            row = s.get(Setting, STATE_PREFIX + relationship_id) or Setting(key=STATE_PREFIX + relationship_id, value={})
            row.value = st
            s.merge(row)

    def drive_explain(self, relationship_id: str) -> dict:
        cfg = self.drive_config()
        out = explain(self.drive_state(relationship_id), self._params())
        out["enabled"] = cfg["enabled"] and relationship_id not in cfg["disabled_relationships"]
        out["version"] = cfg["version"]
        return out

    # ---------- signals ----------
    def _drive_signals(self, relationship_id: str, st: dict, now: datetime) -> tuple[Signals, dict]:
        """Read everything the engine may react to, since the last tick. Never chat text."""
        since = datetime.fromisoformat(st["last_tick"]) if st.get("last_tick") else now - timedelta(days=3650)
        sig = Signals()
        latest_body: dict | None = None
        with self.db.session() as s:
            rep = (
                s.query(ReportRow)
                .filter(ReportRow.deleted.is_(False), ReportRow.relationship_id == relationship_id)
                .order_by(ReportRow.created_at.desc())
                .first()
            )
            if rep is not None and isinstance(rep.body, dict):
                latest_body = rep.body
                aff = rep.body.get("affinity") or {}
                sig.affinity_total = aff.get("total")
                sig.momentum = (rep.body.get("trend") or {}).get("momentum") or "unknown"
                sig.boundary = (rep.body.get("boundary") or {}).get("value") or "none"
                sig.new_report = rep.created_at > since
                sig.days_since_report = (now - rep.created_at).total_seconds() / 86400.0
                sig.boundary_changed = sig.boundary != st.get("last_boundary")
            sig.new_diary_entries = (
                s.query(DiaryRow).filter(DiaryRow.deleted.is_(False), DiaryRow.recorded_at > since).count()
                if hasattr(DiaryRow, "deleted")
                else s.query(DiaryRow).filter(DiaryRow.recorded_at > since).count()
            )
            sig.new_actions = s.query(ActionLog).filter(ActionLog.relationship_id == relationship_id, ActionLog.done_at > since).count()
            oc = (
                s.query(Outcome)
                .filter(Outcome.relationship_id == relationship_id, Outcome.answered_at.isnot(None), Outcome.answered_at > since)
                .order_by(Outcome.answered_at.desc())
                .first()
            )
            sig.new_outcome = oc.outcome if oc else None
            sig.dismissed = s.query(Nudge).filter(Nudge.relationship_id == relationship_id, Nudge.status == "dismissed", Nudge.updated_at > since).count()
            sig.thumbs_down = s.query(Nudge).filter(Nudge.relationship_id == relationship_id, Nudge.verdict == "disagree", Nudge.updated_at > since).count()
        return sig, latest_body or {}

    # ---------- tick ----------
    def drive_tick(self, now: datetime | None = None) -> dict:
        """Advance every enabled relationship one step. Safe to call any time; acts once per hour."""
        now = now or datetime.now()
        cfg = self.drive_config()
        out = {"ticked": 0, "nudged": 0, "skipped": "disabled" if not cfg["enabled"] else None}
        if not cfg["enabled"]:
            return out
        params = self._params()
        with self.db.session() as s:
            rids = [r.id for r in s.query(Relationship).all()] or [DEFAULT_RELATIONSHIP_ID]
        for rid in rids:
            if rid in cfg["disabled_relationships"]:
                continue
            st = self.drive_state(rid)
            if st.get("last_tick") and st["last_tick"][:13] == now.isoformat()[:13]:
                continue
            sig, body = self._drive_signals(rid, st, now)
            new_st, should, kind = tick(st, now, sig, params, relationship_id=rid)
            new_st["last_boundary"] = sig.boundary
            out["ticked"] += 1
            if should and kind:
                nudge = self._compose(rid, kind, body, new_st)
                if nudge is not None:
                    InboxActuator(self._nudge_save).deliver(nudge)
                    new_st = record_nudge(new_st, now, kind)
                    out["nudged"] += 1
                else:
                    new_st["last_block"] = "no_evidence_for_kind"
            self._drive_save(rid, new_st)
        return out

    def _compose(self, rid: str, kind: str, body: dict, st: dict) -> dict | None:
        other = "对方"
        with self.db.session() as s:
            rel = s.get(Relationship, rid)
            if rel is not None and rel.name:
                other = rel.name
        strict: list[dict] = []
        if kind == "draft":
            try:
                from rapport.llm.reply import strict_pack

                entries = [e for e in self.diary_list() if e.enabled and rid in e.allowed_relationships and e.approved_clause]
                pack = strict_pack(entries[:3], rid, datetime.now().astimezone())
                strict = [d.model_dump(mode="json") for d in pack.drafts]
            except Exception:  # noqa: BLE001 - a draft that cannot be built is simply not offered
                strict = []
        prompt = self.outcome_prompt(rid) if kind == "checkin" else None
        d = build(kind, body=body, other_name=other, strict_drafts=strict, outcome_prompt=prompt)
        if d is None or not d.text:
            return None
        return {
            "relationship_id": rid,
            "kind": d.kind,
            "text": d.text,
            "evidence_ids": d.evidence_ids,
            "why": d.why,
            "drive_snapshot": {k: round(float(st.get(k, 0.0)), 3) for k in ("drive", "inhibition", "novelty")} | {"p": st.get("last_p"), "sources": st.get("last_sources")},
            "actuator": "inbox",
            "version": drive_digest(self._params()),
        }

    # ---------- inbox ----------
    def _nudge_save(self, n: dict) -> dict:
        from rapport.state import new_id

        with self.db.session() as s:
            row = Nudge(
                id=new_id(),
                relationship_id=n["relationship_id"],
                kind=n["kind"],
                text=n["text"],
                evidence_ids=list(n.get("evidence_ids") or []),
                why=n.get("why", ""),
                drive_snapshot=n.get("drive_snapshot") or {},
                actuator=n.get("actuator", "inbox"),
                version=n.get("version", ""),
                status="unread",
            )
            s.add(row)
            s.flush()
            return self._nudge_view(row)

    @staticmethod
    def _nudge_view(r: Nudge) -> dict:
        return {
            "id": r.id,
            "relationship_id": r.relationship_id,
            "created_at": r.created_at.isoformat(),
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            "kind": r.kind,
            "text": r.text,
            "evidence_ids": list(r.evidence_ids or []),
            "why": r.why,
            "drive_snapshot": dict(r.drive_snapshot or {}),
            "status": r.status,
            "verdict": r.verdict,
            "actuator": r.actuator,
            "version": r.version,
        }

    def nudges_list(self, relationship_id: str | None = None, unread_only: bool = False, limit: int = 50) -> list[dict]:
        with self.db.session() as s:
            q = s.query(Nudge)
            if relationship_id:
                q = q.filter(Nudge.relationship_id == relationship_id)
            if unread_only:
                q = q.filter(Nudge.status == "unread")
            rows = q.order_by(Nudge.created_at.desc()).limit(limit).all()
            return [self._nudge_view(r) for r in rows]

    def nudges_unread_count(self) -> int:
        with self.db.session() as s:
            return s.query(Nudge).filter(Nudge.status == "unread").count()

    def nudge_set_status(self, nudge_id: str, status: str) -> dict:
        if status not in STATUSES:
            raise ValueError(f"未知状态 {status}")
        with self.db.session() as s:
            row = s.get(Nudge, nudge_id)
            if row is None:
                raise KeyError(nudge_id)
            row.status = status
            row.updated_at = datetime.now()
            s.flush()
            return self._nudge_view(row)

    def nudge_set_verdict(self, nudge_id: str, verdict: str | None) -> dict:
        if verdict not in (None, "agree", "disagree", "unsure"):
            raise ValueError(f"未知的反馈 {verdict}")
        with self.db.session() as s:
            row = s.get(Nudge, nudge_id)
            if row is None:
                raise KeyError(nudge_id)
            row.verdict = verdict
            row.updated_at = datetime.now()
            s.flush()
            return self._nudge_view(row)

    def nudges_drop_for_relationship(self, relationship_id: str) -> int:
        with self.db.session() as s:
            n = s.query(Nudge).filter(Nudge.relationship_id == relationship_id).delete()
            st = s.get(Setting, STATE_PREFIX + relationship_id)
            if st is not None:
                s.delete(st)
        return n
