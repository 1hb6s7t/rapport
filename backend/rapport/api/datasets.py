"""Paired local dataset audit/build/preview; explicit confirmed local export only."""

from __future__ import annotations

import hashlib
from typing import Literal

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from rapport.api.deps import get_state
from rapport.datasets import DatasetStore, exports
from rapport.security import SessionDep
from rapport.security.auth import SESSION_COOKIE

router = APIRouter(prefix="/datasets", dependencies=[SessionDep])
Pool = Literal["offline_review", "realtime_review", "personal_reply"]


class AuditIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_root: str | None = Field(default=None, min_length=1, max_length=2048)


class BuildIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    audit_id: str


class ExportPreviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pools: list[Pool] = Field(min_length=1, max_length=3)
    splits: list[Literal["train", "validation", "test"]] = Field(default_factory=list, max_length=3)
    session_ids: list[str] = Field(default_factory=list, max_length=10000)
    friend_ids: list[str] = Field(default_factory=list, max_length=10000)
    conversation_ids: list[str] = Field(default_factory=list, max_length=10000)
    sample_ids: list[str] = Field(default_factory=list, max_length=100000)
    eligible_only: bool = False
    max_samples: int | None = Field(default=None, ge=1, le=100000)
    target_environment: str = Field(default="local", min_length=1, max_length=200)


class ExportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=1, max_length=exports.MAX_TOKEN_BYTES)
    confirmed: bool = False
    target_environment: str = Field(default="local", min_length=1, max_length=200)


class ExportPageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=1, max_length=exports.MAX_TOKEN_BYTES)
    page: int = Field(default=0, ge=0)
    page_size: int = Field(default=20, ge=1, le=100)


def _owner(request: Request):
    return hashlib.sha256(request.cookies.get(SESSION_COOKIE, "").encode()).hexdigest()


class ReviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sample_id: str = Field(min_length=1, max_length=128)
    pool: Pool
    decision: Literal["accepted", "excluded"]
    note: str = Field(default="", max_length=2000)


def _store(request: Request, response: Response | None = None) -> DatasetStore:
    if response is not None:
        response.headers["Cache-Control"] = "no-store"
    state = get_state(request)
    return DatasetStore(state.settings.data_dir / "datasets", state.crypto)


@router.get("")
def listing(request: Request, response: Response):
    from rapport.datasets.store import default_source

    source = default_source()
    return {"items": _store(request, response).list(), "default_source": str(source) if source else None}


@router.post("/audit")
def audit(body: AuditIn, request: Request, response: Response):
    return _store(request, response).audit(body.source_root)


@router.post("/build")
def build(body: BuildIn, request: Request, response: Response):
    return _store(request, response).build(audit_id=body.audit_id)


@router.get("/{dataset_id}")
def get(dataset_id: str, request: Request, response: Response):
    return _store(request, response).get(dataset_id)


@router.get("/{dataset_id}/preview")
def preview(dataset_id: str, request: Request, response: Response, pool: Pool = "offline_review", page: int = Query(default=0, ge=0), page_size: int = Query(default=20, ge=1, le=100)):
    return _store(request, response).preview(dataset_id, pool, page, page_size)


@router.post("/{dataset_id}/export-preview")
def export_preview(dataset_id: str, body: ExportPreviewIn, request: Request, response: Response):
    selectors = body.model_dump(exclude={"pools", "target_environment"})
    return _store(request, response).export_preview(dataset_id, body.pools, selectors=selectors, target_environment=body.target_environment, owner=_owner(request))


@router.get("/{dataset_id}/export-options")
def export_options(dataset_id: str, request: Request, response: Response):
    return exports.options(_store(request, response), dataset_id)


@router.post("/{dataset_id}/export-preview/page")
def export_preview_page(dataset_id: str, body: ExportPageIn, request: Request, response: Response):
    return exports.preview_page(_store(request, response), dataset_id, body.token, owner=_owner(request), page=body.page, page_size=body.page_size)


@router.post("/{dataset_id}/reviews")
def review(dataset_id: str, body: ReviewIn, request: Request, response: Response):
    return _store(request, response).review(dataset_id, body.pool, body.sample_id, body.decision, body.note)


@router.post("/{dataset_id}/export")
def export(dataset_id: str, body: ExportIn, request: Request):
    data = _store(request).export(dataset_id, body.token, body.confirmed, target_environment=body.target_environment, owner=_owner(request))
    return Response(data, media_type="application/zip", headers={"Cache-Control": "no-store", "Content-Disposition": f'attachment; filename="rapport-dataset-{dataset_id}.zip"'})
