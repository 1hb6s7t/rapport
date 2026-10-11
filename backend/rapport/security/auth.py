"""Pairing + session + CSRF. One local workspace, but still authenticated."""

from __future__ import annotations

import hmac
import secrets
import time
from dataclasses import dataclass, field

SESSION_COOKIE = "rapport_session"
CSRF_HEADER = "x-rapport-csrf"
SESSION_TTL_S = 12 * 3600


@dataclass
class Session:
    token: str
    csrf: str
    created: float
    last_seen: float


@dataclass
class Auth:
    pairing_code: str
    sessions: dict[str, Session] = field(default_factory=dict)
    failed_attempts: int = 0
    locked_until: float = 0.0

    @classmethod
    def create(cls, fixed_code: str | None = None, *, auto_local: bool = False) -> Auth:
        code = fixed_code or ("" if auto_local else f"{secrets.randbelow(10**6):06d}")
        return cls(pairing_code=code)

    def pair(self, code: str) -> Session | None:
        if not self.pairing_code:
            return None
        now = time.time()
        if now < self.locked_until:
            return None
        if not hmac.compare_digest(code.strip(), self.pairing_code):
            self.failed_attempts += 1
            if self.failed_attempts >= 5:
                self.locked_until = now + 60
                self.failed_attempts = 0
            return None
        self.failed_attempts = 0
        return self.new_session()

    def new_session(self) -> Session:
        now = time.time()
        self.sessions = {token: session for token, session in self.sessions.items()
                         if now - session.last_seen <= SESSION_TTL_S}
        s = Session(token=secrets.token_urlsafe(32), csrf=secrets.token_urlsafe(24), created=now, last_seen=now)
        self.sessions[s.token] = s
        return s

    def get(self, token: str | None) -> Session | None:
        if not token:
            return None
        s = self.sessions.get(token)
        if not s:
            return None
        now = time.time()
        if now - s.last_seen > SESSION_TTL_S:
            self.sessions.pop(token, None)
            return None
        s.last_seen = now
        return s

    def check_csrf(self, s: Session, header: str | None) -> bool:
        return bool(header) and hmac.compare_digest(header, s.csrf)

    def revoke_all(self) -> None:
        self.sessions.clear()
