"""Host/Origin guard, auth dependency, simple rate limiting."""

from __future__ import annotations

import time
from collections import deque
from urllib.parse import urlparse

from fastapi import Depends, HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware

from rapport.security.auth import CSRF_HEADER, SESSION_COOKIE, Session

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class HostOriginMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, allowed_hosts: list[str], extra_origins: list[str] | None = None):
        super().__init__(app)
        self.allowed_hosts = set(allowed_hosts)
        self.extra_origins = set(extra_origins or [])

    async def dispatch(self, request: Request, call_next):
        host = (request.headers.get("host") or "").split(":")[0]
        if host not in self.allowed_hosts:
            return _json(403, "host_not_allowed", f"Host {host!r} 不在允许列表")
        origin = request.headers.get("origin")
        if request.url.path.startswith("/api") and request.method not in SAFE_METHODS:
            if origin:
                o = urlparse(origin)
                if o.hostname not in self.allowed_hosts and origin not in self.extra_origins:
                    return _json(403, "origin_not_allowed", "跨源请求被拒绝")
            else:
                # no Origin on a state-changing request: allow only when Sec-Fetch-Site says same-origin/none
                sfs = request.headers.get("sec-fetch-site")
                if sfs and sfs not in ("same-origin", "none"):
                    return _json(403, "origin_not_allowed", "跨源请求被拒绝")
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        if not request.url.path.startswith("/api"):
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            )
        return response


def _json(status: int, code: str, msg: str):
    from fastapi.responses import JSONResponse

    return JSONResponse({"error": {"code": code, "msg": msg}}, status_code=status)


class RateLimiter:
    def __init__(self, limit: int, per_s: float):
        self.limit = limit
        self.per_s = per_s
        self.hits: deque[float] = deque()

    def allow(self) -> bool:
        now = time.time()
        while self.hits and now - self.hits[0] > self.per_s:
            self.hits.popleft()
        if len(self.hits) >= self.limit:
            return False
        self.hits.append(now)
        return True


def require_session(request: Request) -> Session:
    auth = request.app.state.auth
    s = auth.get(request.cookies.get(SESSION_COOKIE))
    if not s:
        raise HTTPException(401, {"code": "unpaired", "msg": "本机会话不存在或已过期"})
    if request.method not in SAFE_METHODS and not auth.check_csrf(s, request.headers.get(CSRF_HEADER)):
        raise HTTPException(403, {"code": "csrf", "msg": "缺少或错误的 CSRF 令牌"})
    return s


SessionDep = Depends(require_session)
