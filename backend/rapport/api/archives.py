"""Explicit, relationship-scoped local archival and plaintext export."""

from __future__ import annotations

import hashlib
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from rapport.api.deps import get_state
from rapport.archives import get_service
from rapport.errors import RapportError
from rapport.security import SessionDep
from rapport.security.auth import SESSION_COOKIE


def no_store(response: Response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"


router = APIRouter(prefix="/archives", dependencies=[SessionDep, Depends(no_store)])


class CreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    import_id: str = Field(min_length=1, max_length=100)
    relationship_id: str = Field(min_length=1, max_length=100)


class ExportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    relationship_id: str = Field(min_length=1, max_length=100)
    include_media: bool = False


class ExportConfirmIn(ExportIn):
    token: str = Field(min_length=1, max_length=100)
    confirmed: bool = False


def owner(request: Request) -> str:
    return hashlib.sha256(request.cookies.get(SESSION_COOKIE, "").encode()).hexdigest()


@router.get("")
def list_archives(request: Request, relationship_id: str = Query(...)):
    return {"items": get_service(get_state(request)).list(relationship_id)}


@router.post("")
def create_archive(body: CreateIn, request: Request):
    return get_service(get_state(request)).create(body.import_id, body.relationship_id)


@router.get("/assets/{asset_id}")
def download_asset(asset_id: str, request: Request, relationship_id: str = Query(...), inline: bool = False):
    service = get_service(get_state(request))
    asset = service.get_asset(asset_id, relationship_id)
    if asset["status"] == "missing":
        raise RapportError("archive_media_missing", "此媒体原文件尚未取得", status=404)
    disposition = "inline" if inline and asset["mime"] in {"image/png", "image/jpeg", "image/gif", "image/webp"} else "attachment"
    return StreamingResponse(service.iter_asset(asset_id, relationship_id), media_type=asset["mime"], headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(asset['filename'] or asset_id)}"})


@router.get("/{archive_id}")
def get_archive(archive_id: str, request: Request, relationship_id: str = Query(...), page: int = Query(0, ge=0), page_size: int = Query(100, ge=1, le=500)):
    service = get_service(get_state(request))
    archive = service.get(archive_id, relationship_id)
    messages = service.messages(archive_id, relationship_id)
    return {"archive": archive, "items": messages[page * page_size : (page + 1) * page_size], "total": len(messages), "page": page, "page_size": page_size}


@router.get("/{archive_id}/assets")
def list_assets(archive_id: str, request: Request, relationship_id: str = Query(...)):
    return {"items": get_service(get_state(request)).assets(archive_id, relationship_id)}


@router.post("/{archive_id}/assets")
def upload_asset(archive_id: str, request: Request, file: UploadFile = File(...), relationship_id: str = Form(...), message_ref: str = Form(...), status: Literal["original", "thumbnail"] = Form("original"), kind: str | None = Form(None), caption: str = Form("")):
    return get_service(get_state(request)).add_asset(archive_id, relationship_id, message_ref, file.file, file.filename or "media", kind=kind, status=status, caption=caption)


@router.delete("/{archive_id}")
def delete_archive(archive_id: str, request: Request, relationship_id: str = Query(...)):
    state = get_state(request)
    get_service(state).delete(archive_id, relationship_id)
    from rapport.learning import LearningService

    learning = getattr(state, "learning", None) or LearningService(state.settings.data_dir, spawn_jobs=False)
    learning.invalidate_sources([f"archive:{archive_id}"])
    return {"ok": True}


@router.post("/{archive_id}/export-preview")
def preview_export(archive_id: str, body: ExportIn, request: Request):
    return get_service(get_state(request)).export_preview(archive_id, body.relationship_id, owner(request), body.include_media)


@router.post("/{archive_id}/export")
def export_archive(archive_id: str, body: ExportConfirmIn, request: Request):
    chunks = get_service(get_state(request)).export_confirm(archive_id, body.relationship_id, owner(request), body.token, body.include_media, body.confirmed)
    return StreamingResponse(chunks, media_type="application/zip", headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Content-Disposition": f'attachment; filename="rapport-archive-{archive_id}.zip"'})
