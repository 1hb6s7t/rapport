"""Pairing, health, settings, demo, data management."""

from __future__ import annotations

import platform
import sys
from ipaddress import ip_address

from fastapi import APIRouter, Request, Response

from rapport import __version__
from rapport.api.deps import err, get_state
from rapport.config import PROVIDER_PRESETS
from rapport.llm.provider import LLMError
from rapport.parsers import list_formats
from rapport.schemas.api import PairRequest, ProviderSettingsIn
from rapport.security import SESSION_COOKIE, SessionDep

router = APIRouter()


@router.get("/health")
def health(request: Request):
    st = get_state(request)
    return {
        "ok": True,
        "version": __version__,
        "paired": st.auth.get(request.cookies.get(SESSION_COOKIE)) is not None,
        "automatic_session": st.settings.local_auto_session,
        "listen": f"{st.settings.host}:{st.settings.port}",
        "ocr_available": _ocr_available(),
        "desktop_available": _desktop_available(),
    }


def _desktop_available() -> bool:
    from rapport.desktop import available

    return available()


def _ocr_available() -> bool:
    from rapport.ocr.engine import available

    return available()


@router.post("/session")
def local_session(request: Request, response: Response):
    """Open a browser-scoped local session, retaining CSRF and ticket ownership."""
    st = get_state(request)
    if not st.settings.local_auto_session:
        raise err(409, "local_session_disabled", "当前服务未开启本机自动连接。")
    try:
        local = bool(request.client and ip_address(request.client.host).is_loopback)
    except ValueError:
        local = False
    if not local:
        raise err(403, "local_session_only", "此入口仅接受本机连接。")
    if request.headers.get("x-rapport-local") != "1":
        raise err(403, "local_session_header", "请从本机应用页面建立连接。")
    origin = request.headers.get("origin")
    expected = f"{request.url.scheme}://{request.url.netloc}"
    if origin and origin != expected and origin != st.settings.dev_cors_origin:
        raise err(403, "origin_not_allowed", "跨源请求被拒绝")
    if request.headers.get("sec-fetch-site") not in {None, "same-origin", "none"}:
        raise err(403, "origin_not_allowed", "跨源请求被拒绝")
    session = st.auth.get(request.cookies.get(SESSION_COOKIE)) or st.auth.new_session()
    response.set_cookie(SESSION_COOKIE, session.token, httponly=True, samesite="strict", secure=False, max_age=12 * 3600, path="/")
    response.headers["Cache-Control"] = "no-store"
    return {"ok": True, "csrf": session.csrf}


@router.post("/pair")
def pair(body: PairRequest, request: Request, response: Response):
    st = get_state(request)
    if not st.auth.pairing_code:
        raise err(404, "pairing_removed", "配对码已停用，请直接打开本机应用。")
    if not st.pair_limiter.allow():
        raise err(429, "rate_limited", "配对尝试过于频繁，请稍后再试")
    s = st.auth.pair(body.code)
    if not s:
        raise err(401, "bad_code", "配对码错误（连续错误 5 次将锁定 60 秒）")
    response.set_cookie(SESSION_COOKIE, s.token, httponly=True, samesite="strict", secure=False, max_age=12 * 3600, path="/")
    return {"ok": True, "csrf": s.csrf}


@router.post("/unpair", dependencies=[SessionDep])
def unpair(request: Request, response: Response):
    get_state(request).auth.revoke_all()
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/settings", dependencies=[SessionDep])
def get_settings(request: Request):
    st = get_state(request)
    cfg = st.provider_config()
    return {
        "provider": cfg,
        "connection_mode": st.connection_mode().value,
        "effective_chain": _chain_label(st),
        "presets": [
            {
                "id": p.id,
                "label": p.label,
                "base_url": p.base_url,
                "default_model": p.default_model,
                "endpoint_region": p.endpoint_region,
                "processing_scope": p.processing_scope,
                "price": {"input_per_m": p.price.input_per_m, "output_per_m": p.price.output_per_m, "verified_on": p.price.verified_on},
                "native": p.native,
                "task_models": p.task_models,
                "profile_label": p.profile_label,
            }
            for p in PROVIDER_PRESETS.values()
        ],
        "limits": {
            "max_text_bytes": st.settings.max_text_bytes,
            "max_messages": st.settings.max_messages,
            "large_import_messages": st.settings.large_import_messages,
            "max_ai_messages": st.settings.max_ai_messages,
            "ai_warn_messages": st.settings.ai_warn_messages,
            "max_images": st.settings.max_images,
            "max_image_bytes": st.settings.max_image_bytes,
            "raw_retention_hours": st.settings.raw_retention_hours,
        },
        "formats": list_formats(),
        "ocr_available": _ocr_available(),
        "data_dir": str(st.settings.data_dir),
        "schema_changes": getattr(st.db, "schema_changes", {}),
    }


def _chain_label(st) -> str:
    cfg = st.provider_config()
    if st.connection_mode().value == "local":
        return f"本地（{cfg.get('base_url') or '未配置'}）"
    return f"云端（{cfg.get('base_url') or '未配置'}）"


@router.put("/settings", dependencies=[SessionDep])
def put_settings(body: ProviderSettingsIn, request: Request):
    st = get_state(request)
    if body.connection_mode is not None:
        from rapport.config import ConnectionMode

        try:
            st.set_connection_mode(ConnectionMode(body.connection_mode))
        except ValueError as e:
            raise err(400, "bad_mode", "connection_mode 只能是 local 或 cloud") from e
    if body.preset not in PROVIDER_PRESETS:
        raise err(400, "bad_preset", "未知的供应商预设")
    if body.base_url and not (body.base_url.startswith("https://") or body.base_url.startswith("http://127.0.0.1") or body.base_url.startswith("http://localhost")):
        raise err(400, "insecure_endpoint", "只允许 HTTPS 端点或本机 http 端点")
    st.save_provider_config(body.preset, body.model, body.base_url, body.api_key, body.cost_warn_cny)
    return {"ok": True, "provider": st.provider_config()}


@router.post("/settings/test-provider", dependencies=[SessionDep])
def test_provider(request: Request):
    st = get_state(request)
    try:
        p = st.make_provider()
    except LLMError as e:
        raise err(400, e.code, e.msg) from e
    ok, msg = p.ping()
    return {"ok": ok, "message": msg, "model": p.info.model, "base_url": p.info.base_url}


@router.get("/trend", dependencies=[SessionDep])
def trend(request: Request, relationship_id: str = "default"):
    """Real cross-import history. A point exists only where a confirmed import produced a report."""
    return get_state(request).trend_series(relationship_id)


@router.get("/stage/history", dependencies=[SessionDep])
def stage_history(request: Request, relationship_id: str = "default"):
    """Stage over time, with the reason each transition was recorded."""
    return get_state(request).stage_history(relationship_id)


@router.get("/persona/history", dependencies=[SessionDep])
def persona_history(request: Request, relationship_id: str = "default"):
    """Portrait revisions, newest first, each with the reasons it changed."""
    return get_state(request).persona_history(relationship_id)


@router.get("/stats", dependencies=[SessionDep])
def stats(request: Request, days: int = 30, relationship_id: str | None = None):
    """Model-call ledger totals and the user's own feedback tallies. Metadata only."""
    st = get_state(request)
    return {
        "llm": st.llm_stats(days=max(1, min(days, 3650)), relationship_id=relationship_id),
        "feedback": st.feedback_summary(relationship_id),
        "relationships": len(st.relationship_list()),
    }


@router.get("/demo")
def demo(request: Request):
    from rapport.demo import demo_report

    return demo_report().model_dump(mode="json")


@router.delete("/data", dependencies=[SessionDep])
def wipe(request: Request):
    st = get_state(request)
    counts = st.wipe_all()
    return {
        "ok": True,
        "cleared": counts,
        "not_covered": ["你自己导出的报告文件", "浏览器下载目录", "操作系统或磁盘层面的残留副本", "已发送给模型供应商的数据"],
    }


@router.get("/diagnostics", dependencies=[SessionDep])
def diagnostics(request: Request):
    st = get_state(request)
    cfg = st.provider_config()
    return {
        "app": __version__,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "provider_preset": cfg["preset"],
        "model": cfg["model"],
        "has_key": cfg["has_key"],
        "ocr_available": _ocr_available(),
        "note": "诊断信息不含聊天内容、别名、Key 或绝对路径。请自行复制到 Issue。",
    }
