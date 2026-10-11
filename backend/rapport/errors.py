"""One error contract for the whole backend.

Every user-facing failure inherits `RapportError(code, msg, hint, status)`. `main.py` registers a
single handler that turns any of them into `{"error": {"code", "msg", "hint"}}`, so API modules
no longer translate exceptions one by one and nothing falls through as a bare 500.

`LLMError`, `ParseError`, `StrictFactError`, `ValidationFailed` and the relationship errors all
inherit from this class; their historical constructor signatures are preserved.
"""

from __future__ import annotations


class RapportError(Exception):
    status: int = 400

    def __init__(self, code: str, msg: str, hint: str = "", status: int | None = None):
        super().__init__(msg)
        self.code = code
        self.msg = msg
        self.hint = hint
        if status is not None:
            self.status = status

    def as_detail(self) -> dict:
        return {"code": self.code, "msg": self.msg, "hint": self.hint}


class NotFound(RapportError):
    status = 404


class Conflict(RapportError):
    status = 409


class Unprocessable(RapportError):
    status = 422
