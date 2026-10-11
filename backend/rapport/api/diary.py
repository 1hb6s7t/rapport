"""Diary and memory endpoints (v2 M09).

The diary never leaves the machine and is never included in exports or diagnostics. Retrieval
always goes through the authorisation pass, so what comes back is what is *currently* usable —
not merely what the index happened to hold.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Request

from rapport.api.deps import err, get_state
from rapport.diary.authorize import StrictFactError, render_strict_reply
from rapport.schemas.diary import DiaryIn, DiaryPatch
from rapport.security import SessionDep
from rapport.state import new_id

router = APIRouter(prefix="/diary", dependencies=[SessionDep])


def _now() -> datetime:
    return datetime.now().astimezone()


@router.get("")
def list_entries(request: Request, include_deleted: bool = False):
    st = get_state(request)
    return {
        "entries": [e.to_record() for e in st.diary_list(include_deleted=include_deleted)],
        "memory_enabled_by_default": st.memory_enabled_by_default(),
        "note": "日记只存在本机；不进入报告、导出或诊断信息。",
    }


@router.post("")
def create_entry(body: DiaryIn, request: Request):
    st = get_state(request)
    from rapport.schemas.diary import DiaryEntry

    recorded = body.recorded_at or _now()
    try:
        entry = DiaryEntry(
            id=new_id(),
            text=body.text,
            kind=body.kind,
            recorded_at=recorded,
            occurred_at=body.occurred_at,
            allowed_relationships=frozenset(body.allowed_relationships),
            approved_clause=body.approved_clause,
        )
    except ValueError as e:
        raise err(422, "bad_diary", str(e)) from e
    st.diary_create(entry)
    return entry.to_record()


@router.get("/search")
def search(request: Request, q: str, relationship_id: str = "", enabled: bool | None = None, limit: int = 3, as_of: datetime | None = None):
    """Retrieve then authorise. With the switch off, the index is not consulted at all."""
    st = get_state(request)
    use = st.memory_enabled_by_default() if enabled is None else enabled
    if as_of is not None and (as_of.tzinfo is None or as_of.utcoffset() is None):
        raise err(422, "naive_time", "as_of 必须带时区")
    return st.diary_search(q, relationship_id, use, as_of=as_of, limit=limit)


@router.get("/{entry_id}")
def get_entry(entry_id: str, request: Request):
    entry = get_state(request).diary_get(entry_id)
    if entry is None:
        raise err(404, "not_found", "日记不存在或已删除")
    return entry.to_record()


@router.patch("/{entry_id}")
def patch_entry(entry_id: str, body: DiaryPatch, request: Request):
    st = get_state(request)
    patch = body.model_dump(exclude_none=True)
    if "allowed_relationships" in patch:
        patch["allowed_relationships"] = list(patch["allowed_relationships"])
    try:
        entry = st.diary_update(entry_id, patch)
    except ValueError as e:
        raise err(422, "bad_clause", str(e)) from e
    if entry is None:
        raise err(404, "not_found", "日记不存在或已删除")
    return entry.to_record()


@router.post("/{entry_id}/enabled")
def set_enabled(entry_id: str, request: Request, enabled: bool):
    if not get_state(request).diary_set_enabled(entry_id, enabled):
        raise err(404, "not_found", "日记不存在或已删除")
    return {"ok": True, "enabled": enabled}


@router.post("/{entry_id}/strict-reply")
def strict_reply(entry_id: str, request: Request, relationship_id: str = ""):
    """Strict mode: a fixed template plus a user-approved, verbatim diary sentence."""
    st = get_state(request)
    entry = st.diary_get(entry_id)
    if entry is None:
        raise err(404, "not_found", "日记不存在或已删除")
    try:
        return render_strict_reply(entry, relationship_id, _now())
    except StrictFactError as e:
        raise err(422, "strict_fact_rejected", str(e)) from e


@router.delete("/{entry_id}")
def delete_entry(entry_id: str, request: Request):
    if not get_state(request).diary_delete(entry_id):
        raise err(404, "not_found", "日记不存在")
    return {"ok": True, "note": "已删除的日记立即不可用于检索；旧向量会在下次清理时移除。"}


@router.delete("")
def clear_all(request: Request):
    n = get_state(request).diary_clear()
    return {"ok": True, "cleared": n}


@router.get("/memory/state", status_code=200)
def memory_state(request: Request):
    return {"enabled": get_state(request).memory_enabled_by_default()}


@router.put("/memory/state")
def set_memory_state(request: Request, enabled: bool):
    get_state(request).set_memory_enabled(enabled)
    return {"ok": True, "enabled": enabled}


@router.get("/cues/state", status_code=200)
def cues_state(request: Request):
    """Whether the structural-cues block is added to the intent prompt (v0.5 §3A; off by default)."""
    return {"enabled": get_state(request).cues_enabled()}


@router.put("/cues/state")
def set_cues_state(request: Request, enabled: bool):
    st = get_state(request)
    st.set_cues_enabled(enabled)
    return {"ok": True, "enabled": enabled, "note": "结构线索只是程序算出的事实（是否在回答提问、前文笑标记…），不是标签；效果见 docs/EVAL.md §15。"}


@router.get("/intent-samples/state", status_code=200)
def intent_samples_state(request: Request):
    """k sampled draws per intent judgement (v0.5 §3B). 1 = single call (default)."""
    return {"k": get_state(request).intent_samples()}


@router.put("/intent-samples/state")
def set_intent_samples_state(request: Request, k: int):
    st = get_state(request)
    st.set_intent_samples(k)
    return {"ok": True, "k": st.intent_samples(), "note": "每处语义判断调用 k 次模型并取多数；费用按 k 倍估算；k 次全一致的负向判断可不受独立片段门槛限制。"}


@router.get("/patterns/state", status_code=200)
def patterns_state(request: Request):
    """Whether the pragmatic-pattern reference layer is on (off by default; docs/EVAL.md §14)."""
    return {"enabled": get_state(request).patterns_enabled()}


@router.put("/patterns/state")
def set_patterns_state(request: Request, enabled: bool):
    st = get_state(request)
    st.set_patterns_enabled(enabled)
    return {
        "ok": True,
        "enabled": enabled,
        "note": "该层是参考规则，不是证据；留出集上开启后 macro-F1 从 0.345 降到 0.31，默认保持关闭。",
    }
