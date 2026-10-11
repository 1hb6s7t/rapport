"""Reading robustness on a real desktop: not running, login window, lazy accessibility tree,
UIA faults mid-read and minimised windows. All with fakes; no WeChat automation runs here."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from rapport.desktop import pyweixin_navigation as nav
from rapport.desktop import wechat_native as native
from rapport.errors import RapportError
from tests.test_wechat_history import mock_desktop, page
from tests.test_wechat_navigation import Node, tree


def test_not_running_and_login_window_have_their_own_errors():
    with pytest.raises(RapportError) as error:
        nav.choose_window([], "指定好友")
    assert error.value.code == "wechat_not_running"
    with pytest.raises(RapportError) as error:
        nav.choose_window([Node(cls="mmui::LoginWindow")], "指定好友")
    assert error.value.code == "wechat_login_required"
    root, _, _ = tree()
    assert nav.choose_window([Node(cls="mmui::LoginWindow"), root], "指定好友") is root, "a logged-in main window wins"


def test_lazy_accessibility_tree_is_awaited_then_reported(monkeypatch):
    root = Node(cls=nav.MAIN_CLASS)
    filled, _, _ = tree()
    polls = []

    def sleep(_seconds):
        polls.append(1)
        if len(polls) == 2:
            root.children = filled.children  # Qt finished building its tree

    monkeypatch.setattr(nav.time, "sleep", sleep)
    nav.wait_for_controls(root, timeout=30)
    assert len(polls) == 2
    with pytest.raises(RapportError) as error:
        nav.wait_for_controls(Node(cls=nav.MAIN_CLASS), timeout=0)
    assert error.value.code == "wechat_controls_unavailable"


def test_uia_fault_mid_read_keeps_the_pages_already_stitched(monkeypatch):
    mock_desktop(monkeypatch, [page(985), page(980)])
    calls = []

    def read(*args, **kwargs):
        calls.append(1)
        if len(calls) == 3:
            raise OSError("COM object that has been separated from its underlying RCW")  # not a RapportError
        from PIL import Image

        return Image.new("RGB", (850, 700)), [page(985), page(980)][len(calls) - 1], None

    monkeypatch.setattr(native, "read_chat", read)
    result = native.capture_history("合成好友", 20_000_000, 1000, threading.Event(), lambda **kw: None)
    stats = result.conversation.meta["wechat_history"]
    assert stats["stop_reason"] == "wechat_window_unavailable" and stats["collected"] == 20
    assert stats["returned_to_latest"] is False, "no attempt to drive a window that just faulted"
    assert any(w.code == "history_partial" for w in result.conversation.warnings)


def test_latest_is_confirmed_by_text_when_an_animation_keeps_changing_pixels(monkeypatch):
    frames = []

    def read(*args, **kwargs):
        frame = page(990)
        frame.visual_hash = f"animated-{len(frames)}"  # a GIF in view: every screenshot differs
        frames.append(frame)
        return "crop", frame, None

    monkeypatch.setattr(native.navigation, "locate_chat", lambda *args: None)
    monkeypatch.setattr(native, "scroll_chat", lambda *args, **kwargs: None)
    monkeypatch.setattr(native, "read_chat", read)
    assert native.seek_latest(None, None, "合成好友", 1, threading.Event()) == ("crop", frames[-1])
    assert len(frames) == 3, "two unchanged reads after the first, as with identical pixels"


def test_minimised_window_is_restored_before_it_is_used(monkeypatch):
    window = SimpleNamespace(NativeWindowHandle=100, ProcessId=200, IsOffscreen=True)
    auto = SimpleNamespace(ControlFromHandle=lambda _: window)
    monkeypatch.setattr(native, "process_name", lambda _: "weixin.exe")
    restored = []

    def restore(control, _auto):
        restored.append(control)
        control.IsOffscreen = False

    monkeypatch.setattr(native.navigation, "restore", restore)
    assert native.restored_window(auto, 100, 200) is window and restored == [window]
    assert native.restored_window(auto, 100, 200) is window and len(restored) == 1, "a visible window is not re-activated"
    monkeypatch.setattr(native, "process_name", lambda _: "notepad.exe")
    window.IsOffscreen = True
    with pytest.raises(RapportError):
        native.restored_window(auto, 100, 200)
    assert len(restored) == 1, "identity is checked before anything is restored"
