"""Feedback endpoints that are not tied to one report (export, delete).

Kept on their own prefix so they cannot be shadowed by `/reports/{report_id}/...`.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from rapport.api.deps import err, get_state
from rapport.security import SessionDep

router = APIRouter(prefix="/feedback", dependencies=[SessionDep])


@router.get("/export")
def export_feedback(request: Request, relationship_id: str | None = None):
    """Intent feedback as evaluation cases. Local use only: it contains chat excerpts."""
    st = get_state(request)
    cases = st.feedback_export_eval(relationship_id)
    return {
        "cases": cases,
        "n": len(cases),
        "note": "这些案例含聊天片段，只应在本机用于个人评测集；进入 docs/EVAL.md 的金标签前必须经人工裁定。",
    }


@router.get("/summary")
def summary(request: Request, relationship_id: str | None = None):
    return get_state(request).feedback_summary(relationship_id)


@router.delete("/{feedback_id}")
def delete_feedback(feedback_id: str, request: Request):
    if not get_state(request).feedback_delete(feedback_id):
        raise err(404, "not_found", "反馈不存在")
    return {"ok": True}
