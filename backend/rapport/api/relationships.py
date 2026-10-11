"""关系档案 endpoints: list / create / update / delete, plus the per-relationship overview.

A relationship is the scope for evidence, history and feedback. The default one always
exists; deleting any other cascades through its imports, events, reports, snapshots,
portrait versions, feedback and call-ledger rows, and detaches it from diary scopes.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from rapport.api.deps import err, get_state
from rapport.schemas.api import RelationshipIn, RelationshipPatch
from rapport.schemas.profile import Person
from rapport.security import SessionDep
from rapport.state.relationships import RelationshipNotFound

router = APIRouter(prefix="/relationships", dependencies=[SessionDep])


@router.get("/self/profile")
def own_profile(request: Request):
    return get_state(request).self_profile()


@router.put("/self/profile")
def save_own_profile(body: Person, request: Request):
    return get_state(request).save_self_profile(body)


@router.get("/{relationship_id}/profile")
def friend_profile(relationship_id: str, request: Request):
    return get_state(request).relationship_person(relationship_id)


@router.put("/{relationship_id}/profile")
def save_friend_profile(relationship_id: str, body: Person, request: Request):
    return get_state(request).save_relationship_person(relationship_id, body)


@router.get("")
def list_relationships(request: Request, include_archived: bool = False):
    st = get_state(request)
    return {
        "relationships": st.relationship_list(include_archived=include_archived),
        "default_id": "default",
        "note": "每个对象的证据、历史曲线、画像版本与反馈互相隔离；日记按授权范围决定可用于哪些对象。",
    }


@router.post("")
def create_relationship(body: RelationshipIn, request: Request):
    st = get_state(request)
    try:
        return st.relationship_create(body.name, body.note, body.labels, relationship_id=body.id)
    except FileExistsError as e:
        raise err(409, "exists", f"对象 {body.id} 已存在") from e
    except ValueError as e:
        raise err(422, "bad_labels", str(e)) from e


@router.get("/{relationship_id}")
def get_relationship(relationship_id: str, request: Request):
    st = get_state(request)
    view = st.relationship_get(relationship_id)
    if view is None:
        raise err(404, "not_found", "对象不存在")
    return view


@router.get("/{relationship_id}/overview")
def overview(relationship_id: str, request: Request):
    """Everything the history page needs for one relationship in one call."""
    st = get_state(request)
    view = st.relationship_get(relationship_id)
    if view is None:
        raise err(404, "not_found", "对象不存在")
    return {
        "relationship": view,
        "n_eff": st.ledger_n_eff(relationship_id),
        "trend": st.trend_series(relationship_id),
        "stage_history": st.stage_history(relationship_id),
        "persona_history": st.persona_history(relationship_id),
        "feedback": st.feedback_summary(relationship_id),
        "llm": st.llm_stats(days=365, relationship_id=relationship_id)["totals"],
    }


@router.patch("/{relationship_id}")
def update_relationship(relationship_id: str, body: RelationshipPatch, request: Request):
    st = get_state(request)
    try:
        view = st.relationship_update(relationship_id, body.model_dump(exclude_none=True))
    except ValueError as e:
        raise err(422, "bad_labels", str(e)) from e
    if view is None:
        raise err(404, "not_found", "对象不存在")
    return view


@router.delete("/{relationship_id}")
def delete_relationship(relationship_id: str, request: Request):
    st = get_state(request)
    try:
        counts = st.relationship_delete(relationship_id)
    except RelationshipNotFound as e:
        raise err(404, "not_found", "对象不存在") from e
    except ValueError as e:
        raise err(409, "default_relationship", str(e)) from e
    return {"ok": True, "cleared": counts, "not_covered": ["你自己导出的报告文件", "已发送给模型供应商的数据"]}
