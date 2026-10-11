"""Execution-log and outcome endpoints (v0.5).

`POST /reports/{id}/actions/{option_id}/done` records that the user carried out one playbook
option and what the other person did. `POST /relationships/{rid}/outcomes` records the
weeks-later check-in. Both are records for the user and for the next synthesis; neither is
read by any scoring function.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from rapport.api.deps import err, get_state
from rapport.schemas.api import ActionDoneIn, OutcomeIn
from rapport.security import SessionDep

router = APIRouter(dependencies=[SessionDep])


@router.post("/reports/{report_id}/actions/{option_id}/done")
def action_done(report_id: str, option_id: str, body: ActionDoneIn, request: Request):
    st = get_state(request)
    try:
        return st.action_done(report_id, option_id, body.goal, body.observed, body.note)
    except ValueError as e:
        raise err(422, "bad_observed", str(e)) from e
    except KeyError as e:
        raise err(404, "not_found", "报告不存在或已删除") from e


@router.get("/reports/{report_id}/actions")
def actions_for_report(report_id: str, request: Request):
    return {"report_id": report_id, "items": get_state(request).actions_for_report(report_id)}


@router.get("/relationships/{relationship_id}/actions")
def actions_for_relationship(relationship_id: str, request: Request):
    st = get_state(request)
    st.resolve_relationship_id(relationship_id)
    return {"relationship_id": relationship_id, "items": st.actions_for_relationship(relationship_id)}


@router.post("/relationships/{relationship_id}/outcomes")
def record_outcome(relationship_id: str, body: OutcomeIn, request: Request):
    st = get_state(request)
    rid = st.resolve_relationship_id(relationship_id)
    try:
        return st.outcome_record(rid, body.report_id, body.outcome, body.met_since, body.note)
    except ValueError as e:
        raise err(422, "bad_outcome", str(e)) from e


@router.get("/relationships/{relationship_id}/outcomes")
def outcomes(relationship_id: str, request: Request):
    st = get_state(request)
    rid = st.resolve_relationship_id(relationship_id)
    return {
        "relationship_id": rid,
        "items": st.outcomes_for_relationship(rid),
        "prompt": st.outcome_prompt(rid),
        "note": "结果标签只存本机、只用于回看与方向校准，永远不进入好感度分数。",
    }
