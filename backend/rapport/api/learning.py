"""Authenticated local learning control; no implicit uploads or model activation."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from rapport.api.deps import get_state
from rapport.learning import LearningService
from rapport.security import SessionDep

router = APIRouter(prefix="/learning", dependencies=[SessionDep])


def service(request: Request):
    st = get_state(request)
    if not hasattr(st, "learning"):
        st.learning = LearningService(st.settings.data_dir)
    return st.learning


class StartJob(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["dataset_build", "archive_prepare", "train", "evaluate"]
    source_mode: Literal["dataset", "archives"] = "dataset"
    relationship_ids: list[str] = Field(default_factory=list, max_length=100)
    dataset_id: str | None = None
    model_id: str | None = None
    pool: Literal["offline_review", "realtime_review", "personal_reply"] = "offline_review"
    architecture: Literal["shared", "separate", "reply_lora"] = "shared"
    max_features: int = Field(default=12000, ge=100, le=50000)
    epochs: int = Field(default=2, ge=1, le=10)
    base_model_id: str | None = None
    max_train_samples: int | None = Field(default=None, ge=20, le=50000)
    max_eval_samples: int = Field(default=64, ge=5, le=2000)


class Activate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: bool = False
    personal_trial: bool = False


class Rollback(Activate):
    model_id: str


class ReplyReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decisions: dict[str, bool]
    evaluation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class Schedule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    source_mode: Literal["dataset", "archives"] = "dataset"
    relationship_ids: list[str] = Field(default_factory=list, max_length=100)
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=100)
    dataset_id: str | None = None
    weekly_day: int = Field(default=0, ge=0, le=6)
    min_new_samples: int = Field(default=200, ge=200, le=100000)
    pool: Literal["offline_review", "realtime_review", "personal_reply"] = "offline_review"
    architecture: Literal["shared", "separate", "reply_lora"] = "shared"
    base_model_id: str | None = None


@router.get("")
def overview(request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return service(request).overview()


@router.get("/jobs")
def jobs(request: Request):
    return service(request).jobs()


@router.post("/jobs")
def create(body: StartJob, request: Request):
    return service(request).create_job(**body.model_dump())


@router.get("/jobs/{job_id}")
def job(job_id: str, request: Request):
    service(request).reconcile()
    return service(request).job(job_id)


@router.post("/jobs/{job_id}/cancel")
def cancel(job_id: str, request: Request):
    return service(request).cancel(job_id)


@router.post("/jobs/{job_id}/retry")
def retry(job_id: str, request: Request):
    return service(request).retry(job_id)


@router.get("/models")
def models(request: Request):
    return service(request).models()


@router.get("/models/compare")
def compare(request: Request, ids: str = ""):
    return {"models": [service(request).model(ident) for ident in ids.split(",")[:10] if ident]}


@router.post("/models/rollback")
def rollback(body: Rollback, request: Request):
    return service(request).activate(body.model_id, confirm=body.confirm, rollback=True, personal_trial=body.personal_trial)


@router.post("/models/{model_id}/activate")
def activate(model_id: str, body: Activate, request: Request):
    return service(request).activate(model_id, confirm=body.confirm, personal_trial=body.personal_trial)


@router.post("/models/{model_id}/deactivate")
def deactivate(model_id: str, request: Request):
    return service(request).deactivate(model_id)


@router.get("/models/{model_id}/samples")
def reply_samples(model_id: str, request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return service(request).evaluation_bundle(model_id)


@router.post("/models/{model_id}/review")
def review_replies(model_id: str, body: ReplyReview, request: Request):
    return service(request).review_replies(model_id, body.decisions, body.evaluation_hash)


@router.get("/schedules")
def schedule(request: Request):
    return service(request).schedule()


@router.put("/schedules")
def set_schedule(body: Schedule, request: Request):
    return service(request).set_schedule(body.model_dump())
