"""Regressions for an offscreen window and an upload widget between grid points."""
from types import SimpleNamespace

import pytest

from rapport.desktop import wechat_native as native
from rapport.errors import RapportError
from tests.test_wechat_navigation import Box


@pytest.mark.parametrize("rect,monitors,valid", [
    (Box(1744, 166, 4057, 1747), [Box(0, 0, 3840, 2160)], False),
    (Box(100, 100, 900, 800), [Box(0, 0, 1920, 1080)], True),
    (Box(-500, 100, 500, 800), [Box(-1920, 0, 0, 1080), Box(0, 0, 1920, 1080)], True),
    (Box(900, 100, 1500, 800), [Box(0, 0, 1000, 1080), Box(1200, 0, 2200, 1080)], False),
    (Box(900, 100, 1500, 800), [Box(0, 0, 1000, 1080), Box(1000, 200, 2200, 1280)], False),
    (Box(), [], False),
])
def test_capture_requires_complete_monitor_coverage(rect, monitors, valid):
    if valid:
        native.require_screen_coverage(rect, monitors)
    else:
        with pytest.raises(RapportError) as error:
            native.require_screen_coverage(rect, monitors)
        assert error.value.code == "wechat_window_outside_screen"


@pytest.mark.parametrize("in_front", [True, False])
def test_small_overlay_is_detected_but_transparent_or_background_window_passes(in_front):
    root = SimpleNamespace(NativeWindowHandle=100, BoundingRectangle=Box(0, 0, 1000, 1000), IsOffscreen=False)
    overlay = SimpleNamespace(NativeWindowHandle=200, BoundingRectangle=Box(940, 420, 999, 460), IsOffscreen=False)
    calls = []

    def hit(x, y):
        calls.append((x, y))
        owner = overlay if in_front and 940 <= x < 999 and 420 <= y < 460 else root
        return SimpleNamespace(GetTopLevelControl=lambda: owner)

    auto = SimpleNamespace(GetForegroundControl=lambda: root, ControlFromPoint=hit,
                           GetMonitorsRect=lambda: [Box(0, 0, 1920, 1080)],
                           GetRootControl=lambda: SimpleNamespace(GetChildren=lambda: [root, overlay]))
    if in_front:
        with pytest.raises(RapportError) as error:
            native.require_visible_surface(auto, root)
        assert error.value.code == "wechat_window_obscured"
    else:
        native.require_visible_surface(auto, root)
    assert any(940 <= x < 999 and 420 <= y < 460 for x, y in calls)


def test_outside_screen_is_rejected_before_capturing_pixels(monkeypatch):
    from PIL import ImageGrab

    root = SimpleNamespace(NativeWindowHandle=100, BoundingRectangle=Box(900, 100, 1500, 800))
    monkeypatch.setattr(native, "checked_window", lambda *args: root)
    monkeypatch.setattr(ImageGrab, "grab", lambda **kw: pytest.fail("offscreen pixels must not be read"))
    auto = SimpleNamespace(GetMonitorsRect=lambda: [Box(0, 0, 1000, 1000)])
    with pytest.raises(RapportError):
        native.grab_window(auto, 100, 200, 20_000_000)
