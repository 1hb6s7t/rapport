"""Model-call ledger and user feedback."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func

from rapport.db import DEFAULT_RELATIONSHIP_ID, Feedback, LLMCall, ReportRow
from rapport.llm.audit import LLMCallRecord

FEEDBACK_KINDS = ("intent", "observation", "action", "landmine", "stage", "trait", "affinity", "boundary")
VERDICTS = ("agree", "disagree", "unsure")


class AuditMixin:
    # ---------- model calls ----------
    def record_llm_call(self, rec: LLMCallRecord) -> None:
        from rapport.state import new_id

        with self.db.session() as s:
            s.add(
                LLMCall(
                    id=new_id(),
                    relationship_id=rec.relationship_id or DEFAULT_RELATIONSHIP_ID,
                    analysis_id=rec.analysis_id,
                    purpose=rec.purpose,
                    preset=rec.preset,
                    model=rec.model,
                    prompt_tokens=rec.prompt_tokens,
                    completion_tokens=rec.completion_tokens,
                    usage_known=rec.usage_known,
                    max_tokens=rec.max_tokens,
                    latency_ms=rec.latency_ms,
                    status=rec.status,
                    error_code=rec.error_code,
                    cost_cny=rec.cost_cny,
                    prompt_hash=rec.prompt_hash,
                )
            )

    def llm_calls_for(self, analysis_id: str) -> list[dict]:
        with self.db.session() as s:
            rows = s.query(LLMCall).filter(LLMCall.analysis_id == analysis_id).order_by(LLMCall.created_at).all()
        return [self._call_view(r) for r in rows]

    @staticmethod
    def _call_view(r: LLMCall) -> dict:
        return {
            "id": r.id,
            "created_at": r.created_at.isoformat(),
            "purpose": r.purpose,
            "preset": r.preset,
            "model": r.model,
            "prompt_tokens": r.prompt_tokens,
            "completion_tokens": r.completion_tokens,
            "usage_known": r.usage_known,
            "max_tokens": r.max_tokens,
            "latency_ms": r.latency_ms,
            "status": r.status,
            "error_code": r.error_code,
            "cost_cny": r.cost_cny,
        }

    def llm_stats(self, days: int = 30, relationship_id: str | None = None) -> dict:
        """Aggregate ledger view: what the model layer actually cost and how often it misbehaved."""
        since = datetime.now() - timedelta(days=days)
        with self.db.session() as s:
            q = s.query(LLMCall).filter(LLMCall.created_at >= since)
            if relationship_id:
                q = q.filter(LLMCall.relationship_id == relationship_id)
            rows = q.all()
            first = s.query(func.min(LLMCall.created_at)).scalar()
        by_purpose: dict[str, dict] = {}
        totals = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost_cny": 0.0, "cost_known_calls": 0,
                  "truncated": 0, "bad_json": 0, "errors": 0, "latency_ms_sum": 0}
        for r in rows:
            p = by_purpose.setdefault(r.purpose, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost_cny": 0.0,
                                                  "truncated": 0, "bad_json": 0, "errors": 0, "latency_ms_sum": 0})
            for bucket in (p, totals):
                bucket["calls"] += 1
                bucket["prompt_tokens"] += r.prompt_tokens
                bucket["completion_tokens"] += r.completion_tokens
                bucket["latency_ms_sum"] += r.latency_ms
                if r.status == "truncated":
                    bucket["truncated"] += 1
                elif r.status == "bad_json":
                    bucket["bad_json"] += 1
                elif r.status == "error":
                    bucket["errors"] += 1
                if r.cost_cny is not None:
                    bucket["cost_cny"] += r.cost_cny
            if r.cost_cny is not None:
                totals["cost_known_calls"] += 1

        def finish(b: dict) -> dict:
            calls = b["calls"] or 1
            out = dict(b)
            out["cost_cny"] = round(b["cost_cny"], 4)
            out["avg_latency_ms"] = int(b["latency_ms_sum"] / calls)
            out["truncation_rate"] = round(b["truncated"] / calls, 3)
            out["failure_rate"] = round((b["truncated"] + b["bad_json"] + b["errors"]) / calls, 3)
            out.pop("latency_ms_sum", None)
            return out

        return {
            "days": days,
            "since": since.isoformat(),
            "ledger_since": first.isoformat() if first else None,
            "totals": finish(totals),
            "by_purpose": {k: finish(v) for k, v in sorted(by_purpose.items())},
            "note": "账本只记录用量、耗时与状态，不保存任何提示词或回复正文。费用为按预设单价的估算，以供应商账单为准。",
        }

    # ---------- feedback ----------
    def feedback_add(
        self,
        report_id: str,
        target_kind: str,
        target_key: str,
        verdict: str,
        note: str = "",
        correction: dict | None = None,
    ) -> dict:
        from rapport.state import new_id

        if target_kind not in FEEDBACK_KINDS:
            raise ValueError(f"未知的反馈对象类型 {target_kind}")
        if verdict not in VERDICTS:
            raise ValueError(f"未知的反馈结论 {verdict}")
        with self.db.session() as s:
            rep = s.get(ReportRow, report_id)
            if rep is None or rep.deleted:
                raise KeyError(report_id)
            relationship_id = rep.relationship_id or DEFAULT_RELATIONSHIP_ID
            # one verdict per (report, kind, key): a second click replaces the first
            existing = (
                s.query(Feedback)
                .filter(Feedback.report_id == report_id, Feedback.target_kind == target_kind, Feedback.target_key == target_key)
                .first()
            )
            if existing is None:
                existing = Feedback(id=new_id(), relationship_id=relationship_id, report_id=report_id, target_kind=target_kind, target_key=target_key[:160], verdict=verdict)
                s.add(existing)
            existing.verdict = verdict
            existing.note = (note or "")[:300]
            existing.correction = dict(correction or {})
            existing.created_at = datetime.now()
            s.flush()
            return self._feedback_view(existing)

    @staticmethod
    def _feedback_view(r: Feedback) -> dict:
        return {
            "id": r.id,
            "created_at": r.created_at.isoformat(),
            "relationship_id": r.relationship_id,
            "report_id": r.report_id,
            "target_kind": r.target_kind,
            "target_key": r.target_key,
            "verdict": r.verdict,
            "note": r.note,
            "correction": r.correction or {},
        }

    def feedback_for_report(self, report_id: str) -> list[dict]:
        with self.db.session() as s:
            rows = s.query(Feedback).filter(Feedback.report_id == report_id).order_by(Feedback.created_at).all()
        return [self._feedback_view(r) for r in rows]

    def feedback_delete(self, feedback_id: str) -> bool:
        with self.db.session() as s:
            row = s.get(Feedback, feedback_id)
            if row is None:
                return False
            s.delete(row)
        return True

    def feedback_drop_for_report(self, report_id: str) -> int:
        with self.db.session() as s:
            return s.query(Feedback).filter(Feedback.report_id == report_id).delete()

    def feedback_summary(self, relationship_id: str | None = None) -> dict:
        """How often the user disagreed, by claim type — the product's own error signal."""
        with self.db.session() as s:
            q = s.query(Feedback)
            if relationship_id:
                q = q.filter(Feedback.relationship_id == relationship_id)
            rows = q.all()
        out: dict[str, dict[str, int]] = {}
        for r in rows:
            b = out.setdefault(r.target_kind, {"agree": 0, "disagree": 0, "unsure": 0})
            b[r.verdict] = b.get(r.verdict, 0) + 1
        return {"by_kind": out, "total": len(rows)}

    def feedback_export_eval(self, relationship_id: str | None = None) -> list[dict]:
        """Turn intent feedback into evaluation cases (local use; contains chat excerpts).

        Each case carries the surface text, the context lines the report kept as evidence, the
        model's tags, the user's verdict and any corrected tags. This is the raw material for a
        personal held-out set — it becomes gold only after a person adjudicates it
        (docs/EVAL.md), never automatically.
        """
        with self.db.session() as s:
            q = s.query(Feedback).filter(Feedback.target_kind == "intent")
            if relationship_id:
                q = q.filter(Feedback.relationship_id == relationship_id)
            rows = q.order_by(Feedback.created_at).all()
            bodies = {r.report_id: s.get(ReportRow, r.report_id) for r in rows}
        cases: list[dict] = []
        for r in rows:
            rep = bodies.get(r.report_id)
            body = rep.body if rep is not None and isinstance(rep.body, dict) and not rep.deleted else {}
            reading = next((x for x in body.get("intent_readings", []) if x.get("msg_id") == r.target_key), None)
            if reading is None:
                continue
            evidence = body.get("evidence", {})
            ctx = [
                f"{'A' if evidence[i]['sender'] == 'me' else 'B'}: {evidence[i]['text']}"
                for i in reading.get("evidence_ids", [])
                if i in evidence and i != r.target_key
            ]
            cases.append(
                {
                    "id": f"fb:{r.id}",
                    "source": "user_feedback",
                    "report_id": r.report_id,
                    "text": reading.get("surface_text", ""),
                    "context": ctx[:6],
                    "model_pragmatic_tags": reading.get("pragmatic_tags", []),
                    "model_emotions": reading.get("emotions", []),
                    "model_ambiguous": bool(reading.get("ambiguous")),
                    "verdict": r.verdict,
                    "corrected": r.correction or {},
                    "note": r.note,
                    "adjudicated": False,
                }
            )
        return cases
