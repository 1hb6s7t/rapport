"""Desktop paste endpoints (v0.7): put a draft into the chat window through Windows accessibility.

Guard rails, in order: the feature is off until the user turns it on; the server only ever
listens on loopback so the request comes from this machine; Enter is pressed only when both the
per-request flag and the "allow send" setting are true; every attempt is logged without text.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from rapport.api.deps import err, get_state
from rapport.db import Setting
from rapport.desktop import available, inspect_focus, paste_into_focus
from rapport.security import SessionDep

router = APIRouter(prefix="/desktop", dependencies=[SessionDep])

FLAG = "desktop.paste"
SEND_FLAG = "desktop.allow_send"
LOG_KEY = "desktop.log"


class PasteIn(BaseModel):
    text: str = Field(max_length=4000)
    send: bool = False
    delay_s: float = Field(default=3.0, ge=0, le=15)
    source: str = Field(default="reply", max_length=32)  # reply | nudge | manual


class DesktopConfigPatch(BaseModel):
    enabled: bool | None = None
    allow_send: bool | None = None


def _log(st, entry: dict) -> None:
    with st.db.session() as s:
        row = s.get(Setting, LOG_KEY) or Setting(key=LOG_KEY, value={"items": []})
        items = list((row.value or {}).get("items") or [])
        items.append(entry)
        row.value = {"items": items[-50:]}
        s.merge(row)


@router.get("/config")
def get_config(request: Request):
    st = get_state(request)
    return {
        "available": available(),
        "enabled": st._flag(FLAG, False),
        "allow_send": st._flag(SEND_FLAG, False),
        "note": "通过 Windows 无障碍接口把草稿粘贴进你当前聚焦的聊天输入框。默认只粘贴、不回车；「允许直接发送」需要单独打开，且每次仍要你点一次。",
    }


@router.patch("/config")
def patch_config(body: DesktopConfigPatch, request: Request):
    st = get_state(request)
    if body.enabled is not None:
        st._set_flag(FLAG, body.enabled)
        if not body.enabled:
            st._set_flag(SEND_FLAG, False)
    if body.allow_send is not None:
        if body.allow_send and not st._flag(FLAG, False):
            raise err(409, "paste_disabled", "先打开「粘贴到聊天窗口」，再决定是否允许直接发送。")
        st._set_flag(SEND_FLAG, body.allow_send)
    return get_config(request)


@router.get("/focus")
def focus(request: Request):
    """What would receive a paste right now (read-only)."""
    st = get_state(request)
    if not st._flag(FLAG, False):
        raise err(409, "paste_disabled", "该功能未开启")
    return inspect_focus().as_dict()


@router.post("/paste")
def paste(body: PasteIn, request: Request):
    st = get_state(request)
    if not available():
        raise err(501, "desktop_unavailable", "当前系统不支持或未安装桌面扩展", "Windows 上运行 `uv sync --extra desktop`。")
    if not st._flag(FLAG, False):
        raise err(409, "paste_disabled", "该功能未开启，请到设置页打开「粘贴到聊天窗口」。")
    send = bool(body.send) and st._flag(SEND_FLAG, False)
    if body.send and not send:
        raise err(409, "send_not_allowed", "未允许直接发送；已改为只粘贴，请自行按回车。")
    res = paste_into_focus(body.text, send=send, delay_s=body.delay_s)
    _log(
        st,
        {
            "at": datetime.now().isoformat(timespec="seconds"),
            "ok": res.ok,
            "reason": res.reason,
            "window": res.window_title[:60],
            "control": res.control_type,
            "sent": res.sent,
            "source": body.source,
            "chars": len(body.text),
        },
    )
    return res.as_dict()


@router.get("/log")
def log(request: Request):
    st = get_state(request)
    with st.db.session() as s:
        row = s.get(Setting, LOG_KEY)
        items = list((row.value or {}).get("items") or []) if row else []
    return {"items": list(reversed(items)), "note": "只记录窗口标题、控件类型、是否回车；不记录粘贴的文字。"}
