"""Shared API helpers."""

from __future__ import annotations

from fastapi import HTTPException, Request

from rapport.state import AppState


def get_state(request: Request) -> AppState:
    return request.app.state.rapport


def err(status: int, code: str, msg: str, hint: str = "") -> HTTPException:
    return HTTPException(status, {"code": code, "msg": msg, "hint": hint})
