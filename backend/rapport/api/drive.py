"""Drive endpoints (v0.6): the local inbox, per-relationship explanation, and the switches.

Nothing here sends anything to anyone but the user. `POST /nudges/{id}/dismiss` and a 👎 feed
back into the drive's inhibition on the next tick, which is how the user teaches it to stop.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from rapport.api.deps import err, get_state
from rapport.security import SessionDep

router = APIRouter(dependencies=[SessionDep])


class DriveConfigPatch(BaseModel):
    enabled: bool | None = None
    llm_drafts: bool | None = None
    daily_cap: int | None = Field(default=None, ge=0, le=3)
    quiet_start_h: int | None = Field(default=None, ge=0, le=23)
    quiet_end_h: int | None = Field(default=None, ge=0, le=23)
    disabled_relationships: list[str] | None = None


class NudgeVerdictIn(BaseModel):
    verdict: str | None = None  # agree | disagree | unsure | null


@router.get("/drive/config")
def get_config(request: Request):
    return get_state(request).drive_config()


@router.patch("/drive/config")
def patch_config(body: DriveConfigPatch, request: Request):
    st = get_state(request)
    out = st.drive_set_config(body.model_dump(exclude_none=True))
    if body.enabled:
        # Turning it on runs one tick right away so the user sees the state, not a blank page.
        st.drive_tick(datetime.now())
    return out


@router.get("/drive/inbox")
def inbox(request: Request, relationship_id: str | None = None, unread: bool = False, limit: int = 50):
    st = get_state(request)
    items = st.nudges_list(relationship_id, unread_only=unread, limit=max(1, min(200, limit)))
    return {
        "items": items,
        "unread": sum(1 for i in items if i["status"] == "unread") if unread else st.nudges_unread_count(),
        "note": "这些提醒只发给你自己；每条都能点开看它为什么此刻出现。",
    }


@router.post("/nudges/{nudge_id}/{status}")
def set_status(nudge_id: str, status: str, request: Request):
    st = get_state(request)
    try:
        return st.nudge_set_status(nudge_id, status)
    except ValueError as e:
        raise err(422, "bad_status", str(e)) from e
    except KeyError as e:
        raise err(404, "not_found", "提醒不存在") from e


@router.put("/nudges/{nudge_id}/verdict")
def set_verdict(nudge_id: str, body: NudgeVerdictIn, request: Request):
    st = get_state(request)
    try:
        return st.nudge_set_verdict(nudge_id, body.verdict)
    except ValueError as e:
        raise err(422, "bad_verdict", str(e)) from e
    except KeyError as e:
        raise err(404, "not_found", "提醒不存在") from e


@router.get("/relationships/{relationship_id}/drive")
def explain(relationship_id: str, request: Request):
    st = get_state(request)
    rid = st.resolve_relationship_id(relationship_id)
    return {"relationship_id": rid, **st.drive_explain(rid)}


@router.post("/drive/tick")
def tick_now(request: Request):
    """Manual tick (diagnostics). Idempotent within the hour."""
    return get_state(request).drive_tick(datetime.now())
