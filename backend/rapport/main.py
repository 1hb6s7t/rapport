"""FastAPI app factory: API + same-origin static frontend + local security middleware."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from rapport.api import api_router
from rapport.config import Settings, get_settings
from rapport.errors import RapportError
from rapport.jobs.runner import JobRunner
from rapport.llm.provider import LLMProvider
from rapport.security import HostOriginMiddleware
from rapport.state import AppState

log = logging.getLogger("rapport")


def configure_logging(level: str = "INFO") -> None:
    """Structured-enough logging for a single-user tool. Never logs chat, diary or keys."""
    root = logging.getLogger()
    if root.handlers:
        return
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
STATIC_DIR = Path(__file__).parent / "static"


def create_app(settings: Settings | None = None, provider_override: LLMProvider | None = None, start_runner: bool = True) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    state = AppState(settings, provider_override=provider_override)
    runner = JobRunner(state)
    state.runner = runner

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        rec = state.reconcile_on_startup()
        from rapport.research.lifecycle import reconcile_reports

        reconcile_reports(settings.data_dir)
        if rec["interrupted"] or rec["purged"]:
            log.info("startup reconcile: %s", rec)
        if start_runner:
            runner.start()
            state.learning.reconcile()
            state.learning.start_scheduler()
        yield
        runner.stop()
        state.dsh_jobs.clear()
        state.dsh.close()
        state.desktop.close()
        state.learning.close()

    from rapport import __version__

    app = FastAPI(title="Rapport", version=__version__, docs_url=None, redoc_url=None, openapi_url="/api/openapi.json", lifespan=lifespan)
    app.state.rapport = state
    app.state.auth = state.auth
    app.add_middleware(HostOriginMiddleware, allowed_hosts=settings.allowed_hosts, extra_origins=[settings.dev_cors_origin] if settings.dev_cors_origin else [])
    if settings.dev_cors_origin:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(CORSMiddleware, allow_origins=[settings.dev_cors_origin], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

    @app.exception_handler(RapportError)
    async def _rapport_exc(request: Request, exc: RapportError):
        return JSONResponse({"error": exc.as_detail()}, status_code=exc.status)

    @app.exception_handler(HTTPException)
    async def _http_exc(request: Request, exc: HTTPException):
        detail = exc.detail if isinstance(exc.detail, dict) else {"code": "error", "msg": str(exc.detail)}
        return JSONResponse({"error": detail}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _val_exc(request: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(x) for x in first.get("loc", []))
        return JSONResponse({"error": {"code": "validation", "msg": f"{loc}: {first.get('msg', '')}"}}, status_code=422)

    app.include_router(api_router)

    if STATIC_DIR.exists() and (STATIC_DIR / "index.html").exists():
        app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa(full_path: str):
            candidate = STATIC_DIR / full_path
            if full_path and candidate.is_file() and candidate.resolve().is_relative_to(STATIC_DIR.resolve()):
                return FileResponse(candidate)
            return FileResponse(STATIC_DIR / "index.html")
    else:

        @app.get("/", include_in_schema=False)
        async def no_frontend():
            return JSONResponse({"msg": "Rapport API is running. Frontend build not found; run `npm run build` in frontend/ or use the Vite dev server."})

    return app
