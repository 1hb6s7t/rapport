"""Desktop paste actuator (Windows, optional): put a draft into the chat window the user is
looking at, through the accessibility tree — never by hooking a client or reading its database.

How it works: `uiautomation` asks Windows UI Automation for the focused control. If that control
is an editable text field in a window that is *not* this app's browser tab, the draft is placed
on the clipboard and pasted with Ctrl+V. Enter is pressed only when the caller explicitly asks
for it **and** the user has turned the "allow send" switch on; the default is paste-only, so the
user still presses Enter themselves. Every attempt is logged (window title, control type,
whether Enter was pressed) — never the text.

Every messenger has a different API; the accessibility tree is the one seam Windows gives every
app. Electron/Qt/UWP chat clients expose their input box as Edit or Document; some (older
WeChat builds) do not, in which case this reports `no_editable_focus` and the user copies.
"""

from __future__ import annotations

import sys
import time
from dataclasses import asdict, dataclass

EDITABLE = {"EditControl", "DocumentControl", "ComboBoxControl"}
OUR_TITLE_HINTS = ("心迹", "Rapport")


@dataclass
class PasteResult:
    ok: bool
    reason: str
    window_title: str = ""
    control_type: str = ""
    sent: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


def available() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import uiautomation  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


def inspect_focus() -> PasteResult:
    """What would receive a paste right now. Read-only; used by the UI to show the target."""
    if not available():
        return PasteResult(False, "unavailable")
    import uiautomation as auto

    ctrl = auto.GetFocusedControl()
    if ctrl is None:
        return PasteResult(False, "no_focus")
    top = ctrl.GetTopLevelControl()
    title = (top.Name if top else "") or ""
    ctype = ctrl.ControlTypeName
    if any(h in title for h in OUR_TITLE_HINTS):
        return PasteResult(False, "focus_is_this_app", title, ctype)
    if ctype not in EDITABLE and not ctrl.GetValuePattern():
        return PasteResult(False, "no_editable_focus", title, ctype)
    return PasteResult(True, "ready", title, ctype)


def paste_into_focus(text: str, send: bool = False, delay_s: float = 0.0) -> PasteResult:
    """Paste `text` into the focused editable control; press Enter only if `send`."""
    if not text.strip():
        return PasteResult(False, "empty_text")
    if delay_s > 0:
        time.sleep(delay_s)
    probe = inspect_focus()
    if not probe.ok:
        return probe
    import uiautomation as auto

    ctrl = auto.GetFocusedControl()
    try:
        auto.SetClipboardText(text)
        ctrl.SendKeys("{Ctrl}v", waitTime=0.2)
        if send:
            ctrl.SendKeys("{Enter}", waitTime=0.1)
        return PasteResult(True, "pasted", probe.window_title, probe.control_type, sent=bool(send))
    except Exception as e:  # noqa: BLE001
        return PasteResult(False, f"paste_failed:{type(e).__name__}", probe.window_title, probe.control_type)
