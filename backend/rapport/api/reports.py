"""Report endpoints: get, list, export, delete, relabel."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from rapport.api.deps import err, get_state
from rapport.db import Analysis, ReportRow
from rapport.report import refresh_labels, to_markdown
from rapport.schemas.api import FeedbackIn, ReportProfilePatch
from rapport.schemas.migrate import UnsupportedReportSchema, upgrade_report_body
from rapport.schemas.report_v1 import Report
from rapport.security import SessionDep

router = APIRouter(prefix="/reports", dependencies=[SessionDep])


def _load(st, report_id: str) -> tuple[ReportRow, Report]:
    with st.db.session() as s:
        row = s.get(ReportRow, report_id)
    if not row or row.deleted:
        raise err(404, "not_found", "报告不存在或已删除")
    try:
        body = upgrade_report_body(row.body)
    except UnsupportedReportSchema as e:
        raise err(409, "unsupported_schema", str(e), "这份报告由更新的版本生成，请升级应用后再打开。") from e
    return row, Report.model_validate(body)


@router.get("")
def list_reports(request: Request, relationship_id: str | None = None):
    st = get_state(request)
    with st.db.session() as s:
        q = s.query(ReportRow).filter(ReportRow.deleted.is_(False))
        if relationship_id:
            q = q.filter(ReportRow.relationship_id == relationship_id)
        rows = q.order_by(ReportRow.created_at.desc()).limit(100).all()
    return [
        {
            "id": r.id,
            "created_at": r.created_at.isoformat(),
            "mode": r.mode,
            "total": r.total,
            "import_id": r.import_id,
            "analysis_id": r.analysis_id,
            "relationship_id": r.relationship_id or "default",
        }
        for r in rows
    ]


@router.get("/{report_id}")
def get_report(report_id: str, request: Request):
    _, rep = _load(get_state(request), report_id)
    return rep.model_dump(mode="json")


@router.get("/{report_id}/export")
def export_report(report_id: str, request: Request, format: str = "md", include_evidence: bool = False):
    _, rep = _load(get_state(request), report_id)
    if format == "json":
        data = rep.model_dump(mode="json")
        if not include_evidence:
            data["evidence"] = {k: {"sender": v["sender"], "ts": v["ts"], "text": "[已省略]"} for k, v in data["evidence"].items()}
        data.pop("features", None)
        return data
    md = to_markdown(rep, include_evidence=include_evidence)
    return PlainTextResponse(md, media_type="text/markdown; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="rapport-{report_id}.md"'})


@router.patch("/{report_id}/profile")
def relabel(report_id: str, body: ReportProfilePatch, request: Request):
    st = get_state(request)
    row, rep = _load(st, report_id)
    before = (rep.affinity.total, rep.stage.value, [a.goal for a in rep.playbook.actions])
    rep = refresh_labels(rep, body.profile, st.prior_policy())
    assert before == (rep.affinity.total, rep.stage.value, [a.goal for a in rep.playbook.actions])
    with st.db.session() as s:
        r = s.get(ReportRow, report_id)
        r.body = rep.model_dump(mode="json")
        r.profile = body.profile.model_dump(mode="json")
    return rep.model_dump(mode="json")


@router.delete("/{report_id}")
def delete_report(report_id: str, request: Request):
    st = get_state(request)
    with st.db.session() as s:
        r = s.get(ReportRow, report_id)
        if not r:
            raise err(404, "not_found", "报告不存在")
        r.deleted = True
        r.body = {"deleted": True}
        # a deleted report must not leave a live portrait revision or a history point behind
        st.deactivate_persona_versions(report_id)
        st.drop_snapshots_for(report_id)
        st.feedback_drop_for_report(report_id)
        st.actions_drop_for_report(report_id)
        a = s.get(Analysis, r.analysis_id)
        if a:
            a.tombstone = True
    return {"ok": True}


# ---------------------------------------------------------------- feedback


@router.get("/{report_id}/feedback")
def list_feedback(report_id: str, request: Request):
    st = get_state(request)
    _load(st, report_id)
    return {"report_id": report_id, "items": st.feedback_for_report(report_id)}


@router.post("/{report_id}/feedback")
def add_feedback(report_id: str, body: FeedbackIn, request: Request):
    """Record the user's verdict on one claim. Never changes the report; exportable as eval cases."""
    st = get_state(request)
    _load(st, report_id)
    try:
        item = st.feedback_add(report_id, body.target_kind, body.target_key, body.verdict, body.note, body.correction)
    except ValueError as e:
        raise err(422, "bad_feedback", str(e)) from e
    except KeyError as e:
        raise err(404, "not_found", "报告不存在或已删除") from e
    return item
