from fastapi import APIRouter

from rapport.api import (
    actions,
    analyses,
    archives,
    core,
    datasets,
    desktop,
    diary,
    dispatch,
    drive,
    feedback,
    gold,
    imports,
    learning,
    models,
    ocr,
    preferences,
    relationships,
    replies,
    reports,
    wechat,
    wechat_dsh,
)

api_router = APIRouter(prefix="/api")
api_router.include_router(core.router)
api_router.include_router(imports.router)
api_router.include_router(ocr.router)
api_router.include_router(analyses.router)
api_router.include_router(reports.router)
api_router.include_router(diary.router)
api_router.include_router(replies.router)
api_router.include_router(relationships.router)
api_router.include_router(feedback.router)
api_router.include_router(actions.router)
api_router.include_router(drive.router)
api_router.include_router(preferences.router)
api_router.include_router(gold.router)
api_router.include_router(desktop.router)
api_router.include_router(dispatch.router)
api_router.include_router(datasets.router)
api_router.include_router(archives.router)
api_router.include_router(learning.router)
api_router.include_router(models.router)
api_router.include_router(wechat.router)
api_router.include_router(wechat_dsh.router)

__all__ = ["api_router"]
