"""Human gold labels (人工金标) over the built dataset: plan, label, undo, export.

The sample text comes from the encrypted snapshot and is served only to the local session;
the AI label is never part of the sample view (it would anchor the answer).
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from rapport.api.deps import get_state
from rapport.security import SessionDep
from rapport.state.gold import SIZE_RANGE

router = APIRouter(prefix="/gold", dependencies=[SessionDep])
Pool = Literal["offline_review", "realtime_review", "personal_reply"]


class PlanIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset_id: str = Field(min_length=32, max_length=32)
    pool: Pool = "offline_review"
    size: int = Field(default=400, ge=SIZE_RANGE[0], le=SIZE_RANGE[1])
    seed: int = 20261010
    replace: bool = False


class LabelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset_id: str = Field(min_length=32, max_length=32)
    sample_id: str = Field(min_length=1, max_length=160)
    emotion: str | None = Field(default=None, max_length=16)
    intent: str | None = Field(default=None, max_length=16)
    skipped: bool = False
    note: str = Field(default="", max_length=500)


class DatasetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset_id: str = Field(min_length=32, max_length=32)


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


@router.get("/plan")
def plan(request: Request, response: Response, dataset_id: str = Query(min_length=32, max_length=32)):
    _no_store(response)
    return get_state(request).gold_progress(dataset_id)


@router.post("/plan")
def create_plan(body: PlanIn, request: Request, response: Response):
    _no_store(response)
    st = get_state(request)
    st.create_gold_plan(body.dataset_id, body.pool, body.size, body.seed, replace=body.replace)
    return st.gold_progress(body.dataset_id)


@router.get("/next")
def next_sample(request: Request, response: Response, dataset_id: str = Query(min_length=32, max_length=32)):
    _no_store(response)
    return get_state(request).gold_next(dataset_id)


@router.post("/label")
def label(body: LabelIn, request: Request, response: Response):
    _no_store(response)
    st = get_state(request)
    st.gold_label(body.dataset_id, body.sample_id, body.emotion, body.intent, skipped=body.skipped, note=body.note)
    return st.gold_next(body.dataset_id)


@router.post("/undo")
def undo(body: DatasetIn, request: Request, response: Response):
    _no_store(response)
    st = get_state(request)
    st.gold_undo(body.dataset_id)
    return st.gold_next(body.dataset_id)


@router.get("/export")
def export(request: Request, response: Response, dataset_id: str = Query(min_length=32, max_length=32)):
    _no_store(response)
    st = get_state(request)
    return {"dataset_id": dataset_id, "labels": st.gold_export(dataset_id), "progress": st.gold_progress(dataset_id)}
