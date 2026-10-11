"""OCR import endpoint (optional extra)."""

from __future__ import annotations

from fastapi import APIRouter, File, Form, Request, UploadFile

from rapport.api.deps import err, get_state
from rapport.api.imports import _import_view
from rapport.security import SessionDep
from rapport.state import new_id

router = APIRouter(prefix="/imports/ocr", dependencies=[SessionDep])


@router.post("")
async def import_ocr(request: Request, files: list[UploadFile] = File(...), timezone: str = Form("Asia/Shanghai"), me_side: str = Form("right"), relationship_id: str | None = Form(None)):
    st = get_state(request)
    try:
        from rapport.ocr import engine, layout
    except ImportError as e:
        raise err(501, "ocr_unavailable", "OCR 组件未安装", "运行 `uv sync --extra ocr` 安装 RapidOCR 后重启。") from e
    if not engine.available():
        raise err(501, "ocr_unavailable", "OCR 组件未安装", "运行 `uv sync --extra ocr` 安装 RapidOCR 后重启。")
    if len(files) > st.settings.max_images:
        raise err(413, "too_many_images", f"最多 {st.settings.max_images} 张截图")
    images = []
    total = 0
    for f in files:
        data = await f.read(st.settings.max_image_bytes + 1)
        if len(data) > st.settings.max_image_bytes:
            raise err(413, "image_too_large", f"{f.filename} 超过 {st.settings.max_image_bytes // 1024 // 1024} MiB")
        total += len(data)
        if total > st.settings.max_images * st.settings.max_image_bytes // 2:
            raise err(413, "images_too_large", "截图总大小超过限制")
        try:
            img = engine.load_image(data, st.settings.max_image_pixels)
        except ValueError as e:
            raise err(400, "bad_image", f"{f.filename}: {e}") from e
        images.append((f.filename or "image", img))
    pages = [layout.Page(name, engine.run(img), img.width, img.height) for name, img in images]
    conv = layout.pages_to_conversation(pages, new_id(), timezone=timezone, me_side=me_side)
    from rapport.state.relationships import RelationshipNotFound

    try:
        rid = st.resolve_relationship_id(relationship_id)
    except RelationshipNotFound as e:
        raise err(404, "relationship_not_found", f"对象 {relationship_id} 不存在") from e
    imp = st.save_import(conv, "ocr", ";".join(n for n, _ in images)[:255], relationship_id=rid)
    return _import_view(imp, conv)
