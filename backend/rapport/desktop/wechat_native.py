"""Windows UI Automation window/input targeting plus local screenshot OCR.

No message-tree scraping, database access, injection or automatic Enter. Native controls
never leave their COM thread. The process allowlist is checked before reading window names.
"""

from __future__ import annotations

import ctypes
import hashlib
import io
import sys
import threading
import time
from contextlib import contextmanager, nullcontext
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import PureWindowsPath

from rapport.desktop import pyweixin_navigation as navigation
from rapport.desktop.wechat import context_digest, to_conversation, verify_contact
from rapport.errors import RapportError
from rapport.schemas.message_v1 import Conversation

ALLOWED_PROCESSES = {"wechat.exe", "weixin.exe"}
NATIVE_LOCK = threading.Lock()
_AUTOMATION = threading.local()


def capabilities() -> dict:
    from rapport.desktop.paste import available
    from rapport.ocr.engine import available as ocr_available

    return {"windows": sys.platform == "win32", "automation": available(), "ocr": ocr_available()}


def visible_wechat_windows(auto):
    controls = auto.GetRootControl().GetChildren()[:200]
    if not controls:
        raise RapportError(
            "wechat_desktop_unavailable", "当前服务无法访问 Windows 交互桌面。",
            "请从已登录的 Windows 桌面启动心迹，并保持微信主窗口打开；无需重新登录微信。", 409,
        )
    return [control for control in controls if process_name(control.ProcessId) in ALLOWED_PROCESSES]


def process_name(pid: int) -> str:
    if sys.platform != "win32":
        return ""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return ""
    try:
        length = wintypes.DWORD(4096)
        buffer = ctypes.create_unicode_buffer(length.value)
        if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
            return ""
        return PureWindowsPath(buffer.value).name.casefold()
    finally:
        kernel.CloseHandle(handle)


@contextmanager
def automation():
    if sys.platform != "win32":
        raise RapportError("wechat_windows_only", "微信窗口读取仅支持 Windows。", status=501)
    try:
        import uiautomation as auto
    except ImportError as exc:
        raise RapportError("wechat_desktop_missing", "Windows 无障碍扩展尚未安装。", "安装 desktop 和 ocr 扩展后重启服务。", 501) from exc
    active = getattr(_AUTOMATION, "auto", None)
    with (nullcontext() if active else auto.UIAutomationInitializerInThread()):
        if not active:
            _AUTOMATION.auto = auto
        try:
            yield auto
        except RapportError:
            raise
        except Exception as exc:
            raise RapportError("wechat_window_unavailable", "窗口操作未完成。", "请保持微信窗口打开、未最小化，并确认微信与心迹的运行权限一致。", 409) from exc
        finally:
            if not active:
                del _AUTOMATION.auto


def checked_window(auto, window_id: int, process_id: int, allow_hidden: bool = False):
    control = auto.ControlFromHandle(window_id)
    if not control or control.NativeWindowHandle != window_id or control.ProcessId != process_id:
        raise RapportError("wechat_window_changed", "所选窗口已关闭或发生变化，请重新选择。", status=409)
    if process_name(process_id) not in ALLOWED_PROCESSES:
        raise RapportError("wechat_not_wechat", "所选窗口不是受支持的微信客户端。", status=403)
    if control.IsOffscreen and not allow_hidden:
        raise RapportError("wechat_window_hidden", "请先恢复微信窗口，打开指定好友的聊天。", status=409)
    return control


def restored_window(auto, window_id: int, process_id: int):
    """The ticket's window, restored first if minimised; identity and process are checked before touching it."""
    control = checked_window(auto, window_id, process_id, True)
    if getattr(control, "IsOffscreen", False):
        navigation.restore(control, auto)
    return checked_window(auto, window_id, process_id)


def require_screen_coverage(rect, monitors):
    """Every captured pixel must lie on a monitor, including across monitor gaps."""
    uncovered = [(rect.left, rect.top, rect.right, rect.bottom)]
    for monitor in monitors:
        remaining = []
        for left, top, right, bottom in uncovered:
            x0, y0 = max(left, monitor.left), max(top, monitor.top)
            x1, y1 = min(right, monitor.right), min(bottom, monitor.bottom)
            if x1 <= x0 or y1 <= y0:
                remaining.append((left, top, right, bottom))
                continue
            remaining.extend(piece for piece in (
                (left, top, right, y0), (left, y1, right, bottom),
                (left, y0, x0, y1), (x1, y0, right, y1),
            ) if piece[0] < piece[2] and piece[1] < piece[3])
        uncovered = remaining
    if uncovered:
        raise RapportError("wechat_window_outside_screen", "微信窗口有部分位于屏幕外，本次未读取。",
                           "请把完整微信窗口移回屏幕内后重试，避免截断消息。", 409)


def require_visible_surface(auto, control):
    """Check actual overlapping windows as well as a coarse hit-test grid.

    A small floating upload button can fall between all nine grid points. Probe
    each overlapping window's intersection without reading its name or content;
    transparent windows and windows behind WeChat pass the ownership hit test.
    """
    rect = control.BoundingRectangle
    window_id = control.NativeWindowHandle
    require_screen_coverage(rect, auto.GetMonitorsRect())
    foreground = auto.GetForegroundControl()
    if not foreground or foreground.NativeWindowHandle != window_id:
        raise RapportError("wechat_window_obscured", "微信未处于前台，本次未读取。", "请在自动读取期间暂时不要切换窗口。", 409)
    points = {(round(rect.left + rect.width() * fx), round(rect.top + rect.height() * fy))
              for fx in (.1, .5, .9) for fy in (.1, .5, .9)}
    for other in auto.GetRootControl().GetChildren()[:200]:
        if other.NativeWindowHandle == window_id or other.IsOffscreen:
            continue
        bounds = other.BoundingRectangle
        left, top = max(rect.left, bounds.left), max(rect.top, bounds.top)
        right, bottom = min(rect.right, bounds.right), min(rect.bottom, bounds.bottom)
        if right <= left or bottom <= top:
            continue
        points.add(((left + right) // 2, (top + bottom) // 2))
        # Probe inside the overlap: window corners can be transparent/rounded.
        points.update((round(left + (right - left - 1) * fx), round(top + (bottom - top - 1) * fy))
                      for fx in (.25, .75) for fy in (.25, .75))
    for x, y in points:
        hit = auto.ControlFromPoint(x, y)
        top = hit.GetTopLevelControl() if hit else None
        if not top or top.NativeWindowHandle != window_id:
            raise RapportError("wechat_window_obscured", "微信窗口被其他窗口或悬浮控件遮挡，本次未读取。",
                               "请移开遮挡窗口后重试，无需调整读取区域。", 409)


def grab_window(auto, window_id: int, process_id: int, max_pixels: int):
    from PIL import ImageGrab

    control = checked_window(auto, window_id, process_id)
    rect = control.BoundingRectangle
    if rect.width() * rect.height() > max_pixels:
        raise RapportError("wechat_bad_capture_size", "微信窗口超过读取尺寸上限。", status=422)
    # Qt 4.1.13 returns black content through PrintWindow. Read the visible screen
    # at UIA's actual bounds instead, after checking foreground/occlusion.
    require_visible_surface(auto, control)
    image = ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom), all_screens=True).convert("RGB")
    checked_window(auto, window_id, process_id)
    require_visible_surface(auto, control)
    if image.width < 400 or image.height < 300 or image.width * image.height > max_pixels:
        raise RapportError("wechat_bad_capture_size", "窗口图像尺寸不适合读取。", "请使用普通大小的微信窗口。", 422)
    return control, image


def pixel_rect(bounds, window_bounds, image):
    """Map current UIA screen bounds into this window screenshot, including DPI scale."""
    if window_bounds.width() <= 0 or window_bounds.height() <= 0:
        raise RapportError("wechat_window_hidden", "微信窗口尚未恢复，请重试。", status=409)
    sx, sy = image.width / window_bounds.width(), image.height / window_bounds.height()
    values = (round((bounds.left - window_bounds.left) * sx), round((bounds.top - window_bounds.top) * sy),
              round((bounds.right - window_bounds.left) * sx), round((bounds.bottom - window_bounds.top) * sy))
    x0, y0, x1, y1 = values
    if x0 < -2 or y0 < -2 or x1 > image.width + 2 or y1 > image.height + 2 or x1 <= x0 or y1 <= y0:
        raise RapportError("wechat_layout_changed", "聊天窗口位置正在变化，请停止拖动后重试。", status=409)
    return max(0, x0), max(0, y0), min(image.width, x1), min(image.height, y1)


def recognize_controls(image, window_bounds, controls, contact, as_page=False, require_accessible_contact=False):
    from PIL import Image, ImageOps

    from rapport.ocr import engine, regions

    title_rect = pixel_rect(controls.title.BoundingRectangle, window_bounds, image)
    message_rect = pixel_rect(controls.messages.BoundingRectangle, window_bounds, image)
    if title_rect[3] > message_rect[1] + 2:
        raise RapportError("wechat_layout_changed", "聊天标题和消息区域重叠，无法可靠读取。", status=409)
    # UIA locates containers and verifies recipient identity. Only pixels supply
    # message text. OCR the title only when its accessible Name is unavailable.
    if not navigation.verify_accessible_contact(controls, contact):
        if require_accessible_contact:
            raise RapportError("wechat_identity_unavailable", "发送前未能取得两个完整且一致的无障碍好友名称。", "请等待微信界面稳定后重新读取；不会回退到OCR姓名核验来发送。", 409)
        header = image.crop(title_rect)
        header = ImageOps.expand(header, border=8, fill=header.getpixel((0, 0)))
        if title_rect[3] - title_rect[1] < 40:
            header = header.resize((header.width * 2, header.height * 2), Image.Resampling.LANCZOS)
        verify_contact(engine.run(header), contact)
    messages = image.crop(message_rect)
    # Words inside pictures, file cards and avatars are not chat text: pixels decide, placeholders stay.
    boxes, media, removed = regions.split(messages, engine.run_chat_cached(messages))
    if not navigation.verify_accessible_contact(controls, contact) and require_accessible_contact:
        raise RapportError("wechat_identity_unavailable", "识别过程中无障碍好友名称暂时不可用，发送已停止。", status=409)
    crop = image.crop((message_rect[0], title_rect[1], message_rect[2], message_rect[3]))
    if as_page:
        from rapport.ocr.layout import Page

        page = Page("微信滚动读取", boxes, messages.width, messages.height, media=media)
        page.media_removed = removed
        page.visual_hash = hashlib.sha256(messages.tobytes()).hexdigest()
        page.image = messages  # transient: only neighboring frames retained by stitcher
        return crop, page
    conversation = to_conversation(boxes, messages.width, messages.height, contact, "right", media=media)
    return crop, conversation


def read_chat(auto, root, contact, max_pixels, as_page=False, cached_controls=None, require_accessible_contact=False):
    controls = cached_controls or navigation.locate_chat(root, contact)
    if not navigation.verify_accessible_contact(controls, contact) and require_accessible_contact:
        raise RapportError("wechat_identity_unavailable", "发送前必须核对聊天标题和输入框的完整好友名称。", status=409)
    def bounds_tuple(c):
        r = c.BoundingRectangle
        return r.left, r.top, r.right, r.bottom
    geometry = (bounds_tuple(root), bounds_tuple(controls.title), bounds_tuple(controls.messages))
    _, image = grab_window(auto, root.NativeWindowHandle, root.ProcessId, max_pixels)
    # Reacquire UIA bounds every time; moving/resizing never reuses stale ratios.
    latest = controls if cached_controls else navigation.locate_chat(root, contact)
    if not navigation.verify_accessible_contact(latest, contact) and require_accessible_contact:
        raise RapportError("wechat_identity_unavailable", "截取过程中好友名称暂时不可用，发送已停止。", status=409)
    if geometry != (bounds_tuple(root), bounds_tuple(latest.title), bounds_tuple(latest.messages)):
        raise RapportError("wechat_layout_changed", "微信窗口正在变化，请稍后重新读取。", status=409)
    crop, conversation = recognize_controls(image, root.BoundingRectangle, latest, contact, as_page=as_page,
                                           require_accessible_contact=require_accessible_contact)
    return crop, conversation, latest.editor


@dataclass
class NativeCapture:
    screenshot: bytes
    conversation: Conversation
    window_id: int
    process_id: int
    view_digest: str | None = None


def scroll_chat(auto, root, contact, direction, ticks=1, cached_controls=None):
    controls = cached_controls or navigation.locate_chat(root, contact)
    navigation.verify_accessible_contact(controls, contact)
    foreground = auto.GetForegroundControl()
    if not foreground or foreground.NativeWindowHandle != root.NativeWindowHandle:
        raise RapportError("wechat_focus_changed", "你已切换窗口，自动滚动已停止。", status=409)
    box = controls.messages.BoundingRectangle
    hit = auto.ControlFromPoint(round((box.left + box.right) / 2), round((box.top + box.bottom) / 2))
    if not hit or hit.GetTopLevelControl().NativeWindowHandle != root.NativeWindowHandle:
        raise RapportError("wechat_window_obscured", "消息区域被遮挡，自动滚动已停止。", status=409)
    method = controls.messages.WheelUp if direction == "up" else controls.messages.WheelDown
    method(wheelTimes=ticks, waitTime=.3)


def next_ticks(ticks: int, shift, height: int) -> int:
    """Wheel notches for the next step: about 40 % of the view per step once a pixel-proven join
    has measured how far one notch moves, so neighbouring frames still share most of their rows."""
    if not shift or shift <= 0 or ticks <= 0:
        return ticks
    return max(1, min(8, int(height * .4 * ticks / shift)))


def page_text(page) -> tuple:
    """What the frame says and where (8 px rows): animated stickers change pixels, not this."""
    return tuple((box.text, round(box.y0 / 8)) for box in sorted(page.boxes, key=lambda box: (box.y0, box.x0)))


def seek_latest(auto, root, contact, max_pixels, cancel):
    controls = navigation.locate_chat(root, contact)
    last_hash, last_text, unchanged = None, None, 0
    for _ in range(60):
        if cancel.is_set():
            return None
        scroll_chat(auto, root, contact, "down", 40, cached_controls=controls)
        crop, page, _ = read_chat(auto, root, contact, max_pixels, as_page=True, cached_controls=controls)
        # Scrolling down no longer moves the list: same pixels, or (with a GIF or video
        # thumbnail animating in view) the same recognised text at the same heights.
        text = page_text(page)
        unchanged = unchanged + 1 if page.visual_hash == last_hash or (text and text == last_text) else 0
        if unchanged >= 2:
            return crop, page
        last_hash, last_text = page.visual_hash, text
    raise RapportError("wechat_latest_unconfirmed", "尚未确认到达最新消息，请重试。", status=409)


def capture_history(contact, max_pixels, target, cancel, report):
    from rapport.desktop.wechat_history import HistoryAssembler
    from rapport.ocr import engine

    if not engine.available():
        raise RapportError("wechat_ocr_missing", "本地 OCR 组件尚未安装。", status=501)
    if not NATIVE_LOCK.acquire(blocking=False):
        raise RapportError("wechat_busy", "另一个微信操作尚未完成。", status=409)
    try:
        with automation() as auto:
            report(phase="opening", collected=0, target=target, pages=0)
            windows = visible_wechat_windows(auto)
            root, controls = navigation.open_contact(auto, windows, contact)
            report(phase="latest", collected=0, target=target, pages=0)
            latest = seek_latest(auto, root, contact, max_pixels, cancel)
            if latest is None:
                return None
            preview, page = latest
            initial_view = to_conversation(page.boxes, page.width, page.height, contact, media=page.media)
            assembler = HistoryAssembler()
            assembler.prepend(page)
            last_hash, last_text = page.visual_hash, page_text(page)
            devices = {engine.last_device(): 1}
            unchanged = 0
            reason = "target_reached"
            started = time.monotonic()
            ticks = 2
            for _ in range(min(6000, target * 6 + 30)):
                report(phase="reading", collected=min(target, assembler.count), target=target, pages=assembler.pages,
                       device=engine.last_device())
                if assembler.count >= target:
                    break
                if cancel.is_set():
                    reason = "cancelled"
                    break
                if time.monotonic() - started > 7200:
                    reason = "time_limit"
                    break
                try:
                    scroll_chat(auto, root, contact, "up", ticks, cached_controls=controls)
                    _, page, _ = read_chat(auto, root, contact, max_pixels, as_page=True, cached_controls=controls)
                    devices[engine.last_device()] = devices.get(engine.last_device(), 0) + 1
                    text = page_text(page)
                    # Same pixels, or (a GIF or video animating in view) the same words at the same heights.
                    if page.visual_hash == last_hash or (text and text == last_text):
                        unchanged += 1
                        if unchanged >= 3:
                            reason = "no_more_loaded_history"
                            break
                        cancel.wait(.7)  # allow a delayed history load before retrying
                        continue
                    unchanged = 0
                    try:
                        assembler.prepend(page)
                    except RapportError:
                        if ticks <= 1:
                            raise
                        # Roll back to one notch above the last joined frame to recover the
                        # overlap before stopping. Never bridge an unverified gap with guesses.
                        scroll_chat(auto, root, contact, "down", ticks - 1, cached_controls=controls)
                        _, page, _ = read_chat(auto, root, contact, max_pixels, as_page=True, cached_controls=controls)
                        ticks = 1
                        assembler.prepend(page)
                    else:
                        ticks = next_ticks(ticks, assembler.last_shift, page.height)
                    last_hash, last_text = page.visual_hash, page_text(page)
                except RapportError as error:
                    reason = error.code
                    break
                except Exception:  # noqa: BLE001 - UIA/COM faults (e.g. WeChat rebuilt its tree) end the read
                    # Pages already verified and stitched are kept as a partial result.
                    reason = "wechat_window_unavailable"
                    break
            else:
                reason = "page_limit"
            if not assembler.count:
                return None
            conversation = assembler.build(contact, target, reason)
            # Restore latest for reply drafting only when the user has not stopped
            # or taken focus away. No forced focus reclamation during a long job.
            restored = False
            if not cancel.is_set() and reason in {"target_reached", "no_more_loaded_history", "time_limit", "page_limit", "wechat_history_overlap", "wechat_history_no_text"}:
                report(phase="restoring", collected=len(conversation.messages), target=target, pages=assembler.pages)
                try:
                    restored = seek_latest(auto, root, contact, max_pixels, cancel) is not None
                except Exception:  # noqa: BLE001 - returning to the latest message is a courtesy only
                    pass
            conversation.meta["wechat_history"]["returned_to_latest"] = restored
            conversation.meta["wechat_history"]["ocr_devices"] = devices
            conversation.meta["wechat_history"]["scroll_ticks"] = ticks
            output = io.BytesIO()
            preview.thumbnail((1400, 1400))
            preview.save(output, format="PNG")
            return NativeCapture(output.getvalue(), conversation, root.NativeWindowHandle, root.ProcessId, context_digest(initial_view))
    finally:
        NATIVE_LOCK.release()


def capture_contact(contact: str, max_pixels: int):
    from rapport.ocr import engine

    if not engine.available():
        raise RapportError("wechat_ocr_missing", "本地 OCR 组件尚未安装。", "安装 ocr 扩展后重启服务。", 501)
    if not NATIVE_LOCK.acquire(blocking=False):
        raise RapportError("wechat_busy", "上一次窗口操作尚未结束，请稍后重试。", status=409)
    try:
        with automation() as auto:
            windows = visible_wechat_windows(auto)
            root, _ = navigation.open_contact(auto, windows, contact)
            crop, conversation, _ = read_chat(auto, root, contact, max_pixels)
            output = io.BytesIO()
            crop.thumbnail((1400, 1400))
            crop.save(output, format="PNG")
            return NativeCapture(output.getvalue(), conversation, root.NativeWindowHandle, root.ProcessId)
    finally:
        NATIVE_LOCK.release()


def _input_value(control) -> str:
    pattern = control.GetValuePattern()
    if pattern:
        return pattern.Value or ""
    pattern = control.GetTextPattern()
    if pattern:
        return pattern.DocumentRange.GetText(-1) or ""
    raise RapportError("wechat_input_unreadable", "无法确认输入框是否为空。", "请复制草稿后手动填入。", 409)


def bind_contact(contact, read_context):
    """DSH-style GUI recipient selection with DB context; no screenshot/OCR."""
    if not NATIVE_LOCK.acquire(blocking=False):
        raise RapportError("wechat_busy", "另一个微信操作尚未结束。", status=409)
    try:
        with automation() as auto:
            root, controls = navigation.open_contact(auto, visible_wechat_windows(auto), contact)
            if not navigation.verify_accessible_contact(controls, contact):
                raise RapportError("wechat_identity_unavailable", "未能核对完整好友名称。", status=409)
            context = read_context(root.ProcessId)
            checked_window(auto, root.NativeWindowHandle, root.ProcessId)
            if not navigation.verify_accessible_contact(navigation.locate_chat(root, contact), contact):
                raise RapportError("wechat_identity_unavailable", "核对过程中好友名称发生变化。", status=409)
            return root.NativeWindowHandle, root.ProcessId, context
    finally:
        NATIVE_LOCK.release()


def fill(ticket, text: str, max_pixels: int, before_write, *, state=None) -> dict:
    if not text.strip():
        raise RapportError("wechat_empty_draft", "回复草稿不能为空。", status=422)
    if not NATIVE_LOCK.acquire(blocking=False):
        raise RapportError("wechat_busy", "上一次窗口操作尚未结束。", status=409)
    try:
        with automation() as auto:
            root = restored_window(auto, ticket.window_id, ticket.process_id)
            navigation.restore(root, auto)
            def read():
                if getattr(ticket, "context_source", "visual") == "dsh_database":
                    from rapport.desktop.dispatch import read_dispatch_chat

                    return read_dispatch_chat(auto, root, ticket, max_pixels, state=state)
                return read_chat(auto, root, ticket.contact, max_pixels)

            _, current, control = read()
            if context_digest(current) != ticket.digest:
                raise RapportError("wechat_context_changed", "可见对话已经变化，请重新读取后再起草。", status=409)
            if _input_value(control).strip():
                raise RapportError("wechat_input_not_empty", "微信输入框已有内容，本次未覆盖。", "请先在微信处理已有草稿。", 409)
            control.SetFocus()
            time.sleep(.12)
            focused = auto.GetFocusedControl()
            if not focused or not auto.ControlsAreSame(control, focused):
                raise RapportError("wechat_focus_changed", "焦点未停留在已核对的输入框，本次未填入。", status=409)
            top = focused.GetTopLevelControl()
            if not top or top.NativeWindowHandle != ticket.window_id or top.ProcessId != ticket.process_id:
                raise RapportError("wechat_focus_changed", "微信焦点发生变化，本次未填入。", status=409)
            # Verify contact and visible context a second time after taking focus.
            _, latest, latest_control = read()
            if not auto.ControlsAreSame(control, latest_control):
                raise RapportError("wechat_focus_changed", "聊天输入框已经变化，本次未填入。", status=409)
            if context_digest(latest) != ticket.digest or _input_value(control).strip():
                raise RapportError("wechat_context_changed", "对话或输入框已变化，本次未填入。", status=409)
            if not auto.ControlsAreSame(control, auto.GetFocusedControl()):
                raise RapportError("wechat_focus_changed", "焦点已变化，本次未填入。", status=409)
            if getattr(ticket, "context_source", "visual") == "dsh_database":
                from rapport.desktop.dispatch import editor_is_empty, require_foreground

                require_foreground(auto, root)
                if not editor_is_empty(control):
                    raise RapportError("wechat_input_not_empty", "微信输入框已有内容，本次未覆盖。", status=409)
            before_write()  # single-use before the first write; uncertain writes are not retried
            try:
                value_pattern = control.GetValuePattern()
                if value_pattern and not value_pattern.IsReadOnly:
                    if value_pattern.SetValue(text) is False:
                        raise RuntimeError("value_not_set")
                else:
                    auto.SetClipboardText(text)
                    control.SendKeys("{Ctrl}v", waitTime=.2)
                if _input_value(control).replace("\r\n", "\n").rstrip("\r\n") != text.replace("\r\n", "\n").rstrip("\r\n"):
                    raise RuntimeError("value_not_verified")
            except Exception as exc:
                raise RapportError("wechat_fill_uncertain", "无法确认是否填入成功，请先查看微信输入框。", "为避免重复，本次不会自动重试。", 409) from exc
            return {"ok": True, "sent": False, "contact": ticket.contact, "message": "已填入，请在微信核对后自行发送。"}
    finally:
        NATIVE_LOCK.release()
