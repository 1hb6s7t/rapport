"""Archive-only media resolution and normal Windows clipboard transfer primitives.

No paths from a model or HTTP request are accepted. Original media must originate
from the scoped archive service, and checksums are rechecked immediately before use.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import io
import secrets
import struct
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from rapport.errors import RapportError

MAX_BYTES = 20 * 1024 * 1024
STATIC_MIME = {"image/png": ".png", "image/jpeg": ".jpg"}
ANIMATED_MIME = {"image/gif": ".gif"}
SENDABLE_MIME = {**STATIC_MIME, **ANIMATED_MIME}


def media_frames(data, mime, max_pixels=40_000_000):
    """Bound decode cost and keep actual animation frames (never flatten a sticker)."""
    from PIL import Image, ImageOps

    try:
        image = Image.open(io.BytesIO(data))
    except (OSError, ValueError) as error:
        raise RapportError("dispatch_asset_invalid", "媒体原件无法解码。", status=422) from error
    formats = {"image/png": "PNG", "image/jpeg": "JPEG", "image/gif": "GIF"}
    if image.format != formats.get(mime) or image.width * image.height > max_pixels:
        raise RapportError("dispatch_asset_invalid", "媒体格式或尺寸与素材记录不符。", status=422)
    count = getattr(image, "n_frames", 1)
    if count > 120 or count * image.width * image.height > 160_000_000:
        raise RapportError("dispatch_animation_too_large", "表情动画过长或分辨率过大，请选择更小的原件。", status=422)
    frames, durations = [], []
    try:
        for index in range(count):
            image.seek(index)
            frames.append(ImageOps.exif_transpose(image).convert("RGBA"))
            durations.append(max(20, int(image.info.get("duration", 100))))
    except (OSError, ValueError, EOFError) as error:
        raise RapportError("dispatch_asset_invalid", "媒体原件包含不完整帧，无法可靠核对。", status=422) from error
    return frames, durations


def resolve_asset(state, asset_id, relationship_id):
    service = getattr(state, "archives", None)
    if service is None:
        from rapport.archives import get_service

        service = get_service(state)
    asset = service.get_asset(asset_id, relationship_id)
    data = service.read_asset(asset_id, relationship_id)
    if asset.get("status") != "original" or not data or len(data) > MAX_BYTES:
        raise RapportError("dispatch_asset_unavailable", "该素材没有可发送的原件，或超过20MB限制。", status=409)
    checksum = hashlib.sha256(data).hexdigest()
    if checksum != asset.get("sha256"):
        raise RapportError("dispatch_asset_changed", "素材校验值发生变化，请重新归档并预览。", status=409)
    return asset, data


def preview_assets(state, rid, items):
    from PIL import Image, ImageOps

    out = []
    for item in items:
        row = item.model_dump(exclude_none=True)
        if item.kind != "text":
            asset, data = resolve_asset(state, item.asset_id, rid)
            if asset.get("kind") != item.kind or asset.get("mime") not in {*STATIC_MIME, "image/gif", "image/webp"}:
                raise RapportError("dispatch_asset_type", "素材类型与所选发送方式不符。", status=422)
            try:
                image = Image.open(io.BytesIO(data))
                if image.width * image.height > state.settings.max_image_pixels:
                    raise ValueError("image_size")
                image = ImageOps.exif_transpose(image).convert("RGBA")
                image.thumbnail((640, 480))
                output = io.BytesIO()
                image.save(output, format="PNG")
            except Exception as error:
                raise RapportError("dispatch_asset_invalid", "素材无法生成可核对的图片预览。", status=422) from error
            row.update({"sha256": asset["sha256"], "mime": asset["mime"], "archive_id": asset["archive_id"],
                        "caption": asset.get("caption", "")[:300],
                        "preview_url": "data:image/png;base64," + base64.b64encode(output.getvalue()).decode()})
            if asset["mime"] == "image/gif":
                frames, durations = media_frames(data, asset["mime"], state.settings.max_image_pixels)
                row.update({"preview_url": "data:image/gif;base64," + base64.b64encode(data).decode(),
                            "animated": len(frames) > 1, "frame_count": len(frames), "duration_ms": sum(durations)})
        out.append(row)
    return out


def revalidate_assets(state, rid, items):
    for item in items:
        if item["kind"] == "text":
            continue
        asset, _ = resolve_asset(state, item["asset_id"], rid)
        if any(asset.get(key) != item.get(key) for key in ("sha256", "mime", "archive_id", "kind")):
            raise RapportError("dispatch_asset_changed", "素材或归档归属已变化，剩余消息未发送。", status=409)


@contextmanager
def staged_asset(state, asset_id, rid, expected_hash):
    asset, data = resolve_asset(state, asset_id, rid)
    if asset["sha256"] != expected_hash or asset["mime"] not in SENDABLE_MIME:
        raise RapportError("dispatch_media_unsupported", "此驱动支持经过核对的 PNG/JPEG/GIF 原件。", status=501)
    parent = Path(state.settings.data_dir) / "dispatch-staging"
    parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="asset-", dir=parent) as folder:
        path = Path(folder) / ("image" + SENDABLE_MIME[asset["mime"]])
        path.write_bytes(data)
        yield path


def clipboard_file(path: Path, owner_window: int):
    """CF_HDROP per Microsoft Shell Clipboard Formats / DROPFILES documentation.

    The caller supplies a service-created, verified staging path. This is never
    exposed as an API accepting arbitrary local paths.
    """
    if sys.platform != "win32":
        raise RapportError("wechat_windows_only", "媒体传输仅支持 Windows。", status=501)
    path = path.resolve(strict=True)
    if not path.is_file() or path.stat().st_size > MAX_BYTES or "\0" in str(path):
        raise RapportError("dispatch_asset_invalid", "暂存素材不可用。", status=409)
    payload = struct.pack("<IiiII", 20, 0, 0, 0, 1) + (str(path) + "\0\0").encode("utf-16le")
    kernel, user = ctypes.WinDLL("kernel32", use_last_error=True), ctypes.WinDLL("user32", use_last_error=True)
    kernel.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel.GlobalAlloc.restype = ctypes.c_void_p
    kernel.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel.GlobalLock.restype = ctypes.c_void_p
    kernel.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel.GlobalFree.argtypes = [ctypes.c_void_p]
    user.OpenClipboard.argtypes = [ctypes.c_void_p]
    user.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    user.SetClipboardData.restype = ctypes.c_void_p
    handle = kernel.GlobalAlloc(0x0002, len(payload))  # GMEM_MOVEABLE; ownership transfers only on success
    if not handle:
        raise RapportError("dispatch_clipboard_failed", "无法创建文件剪贴板。", status=409)
    transferred, opened = False, False
    try:
        pointer = kernel.GlobalLock(handle)
        if not pointer:
            raise OSError("clipboard allocation")
        ctypes.memmove(pointer, payload, len(payload))
        kernel.GlobalUnlock(handle)
        opened = bool(user.OpenClipboard(owner_window))
        if not opened or not user.EmptyClipboard() or not user.SetClipboardData(15, handle):
            raise OSError("clipboard busy")
        transferred = True
        return user.GetClipboardSequenceNumber()
    except Exception as error:
        raise RapportError("dispatch_clipboard_failed", "文件剪贴板被占用，本项尚未粘贴。", status=409) from error
    finally:
        if opened:
            user.CloseClipboard()
        if not transferred:
            kernel.GlobalFree(handle)


def clear_owned_clipboard(sequence):
    if sys.platform != "win32":
        return
    user = ctypes.WinDLL("user32", use_last_error=True)
    if user.GetClipboardSequenceNumber() == sequence and user.OpenClipboard(None):
        try:
            if user.GetClipboardSequenceNumber() == sequence:
                user.EmptyClipboard()
        finally:
            user.CloseClipboard()


def matches_preview(source, rendered):
    """Conservative rendered-image verification, never icon/name-only matching."""
    from PIL import Image, ImageChops, ImageOps, ImageStat

    source = ImageOps.exif_transpose(source).convert("RGBA")
    # Compare only opaque source pixels; transparent GIF edges adopt WeChat's
    # current background and must not force invented RGB values into the match.
    mask = source.getchannel("A").point(lambda value: 255 if value >= 245 else 0)
    source = source.convert("RGB")
    rendered = rendered.convert("RGB")
    if min(rendered.size) < 32 or max(source.width, source.height) > 20000:
        return False
    if abs(source.width / source.height - rendered.width / rendered.height) > .035:
        return False
    # A flat colour contains too little visual information to prove identity.
    if sum(ImageStat.Stat(source.resize((32, 32))).stddev) < 12:
        return False
    expected = source.resize(rendered.size, Image.Resampling.LANCZOS)
    mask = mask.resize(rendered.size, Image.Resampling.NEAREST)
    if ImageStat.Stat(mask).mean[0] < 50:
        return False
    stat = ImageStat.Stat(ImageChops.difference(expected, rendered), mask=mask)
    return max(stat.mean) < 12 and max(stat.rms) < 22


def image_controls(container):
    """Read media container geometry only; bounded traversal, no message text scraping."""
    queue = [(container, 0)]
    found = []
    for _ in range(160):
        if not queue:
            break
        control, depth = queue.pop(0)
        if getattr(control, "IsOffscreen", True):
            continue
        rect = control.BoundingRectangle
        if control.ControlTypeName == "ImageControl" and rect.width() >= 32 and rect.height() >= 32:
            found.append(control)
        elif depth < 7:
            queue.extend((child, depth + 1) for child in control.GetChildren())
    return found


class StaticImageSender:
    """Inline image/GIF adapter, verified from pixels and accessibility each operation.

    CF_HDROP plus Ctrl+V is the standard transport also used by upstream pyweixin.
    Unlike its blind Alt+S flow, we require exactly one inline image preview,
    content matching, same recipient/context, and a new outgoing matching image.
    Dialog-based and inaccessible preview layouts stop before clicking Send.
    """

    def __init__(self, state):
        self.state = state

    def send(self, session, item):
        from rapport.desktop import wechat_native as native
        from rapport.desktop.dispatch import WindowsDispatchAdapter, editor_is_empty, read_dispatch_chat
        from rapport.desktop.wechat import context_digest

        auto, root, ticket = session.auto, session.root, session.ticket
        _, before, editor = read_dispatch_chat(auto, root, ticket, session.max_pixels)
        if context_digest(before) != context_digest(session.current) or not editor_is_empty(editor):
            raise RapportError("wechat_context_changed", "聊天或输入框已变化，本项未粘贴。", status=409)
        with staged_asset(self.state, item["asset_id"], ticket.relationship_id, item["sha256"]) as path:
            frames, _durations = media_frames(path.read_bytes(), item["mime"], session.max_pixels)
            animated = len({hashlib.sha256(frame.tobytes()).hexdigest() for frame in frames}) > 1
            controls = native.navigation.locate_chat(root, ticket.contact)
            # A matching existing visible image makes a new-image receipt ambiguous.
            if self._matching_frames(auto, root, controls.messages, frames, session.max_pixels):
                raise RapportError("dispatch_image_ambiguous", "当前可见消息中已有相同图片，无法可靠区分新发送回执。", status=409)
            editor.SetFocus()
            native.navigation.assert_focus(auto, editor, root)
            sequence = clipboard_file(path, root.NativeWindowHandle)
            try:
                native.navigation.assert_focus(auto, editor, root)
                editor.SendKeys("{Ctrl}v", waitTime=.3)
                deadline = time.monotonic() + 3
                previews = []
                while time.monotonic() < deadline:
                    native.navigation.assert_focus(auto, editor, root)
                    controls = native.navigation.locate_chat(root, ticket.contact)
                    if not auto.ControlsAreSame(editor, controls.editor):
                        raise RapportError("wechat_context_changed", "媒体预览中的输入框发生变化。", status=409)
                    previews = image_controls(editor)
                    if previews:
                        break
                    time.sleep(.1)
                if len(previews) != 1 or not self._verify_animation(session, editor, frames, animated, outgoing=False):
                    raise RapportError("dispatch_media_preview_unverified", "无法核对微信中的图片预览，本项没有点击发送。", "请在微信检查并自行处理现有草稿。", 409)
                value = native._input_value(editor)
                if value.replace("\ufffc", "").strip():
                    raise RapportError("wechat_input_changed", "图片草稿中出现了额外文字，本项没有点击发送。", status=409)
                _, latest, latest_editor = read_dispatch_chat(auto, root, ticket, session.max_pixels)
                if context_digest(latest) != context_digest(before) or not auto.ControlsAreSame(editor, latest_editor):
                    raise RapportError("wechat_context_changed", "图片发送前聊天发生变化。", status=409)
                native.navigation.assert_focus(auto, editor, root)
                button = WindowsDispatchAdapter._send_button(root, editor)
                if not button.IsEnabled:
                    raise RapportError("dispatch_send_control_changed", "图片尚未准备好，未点击发送。", status=409)
                if native._input_value(editor).replace("\ufffc", "").strip() or len(image_controls(editor)) != 1:
                    raise RapportError("wechat_input_changed", "图片草稿在发送前发生变化。", status=409)
                native.navigation.assert_focus(auto, editor, root)
                button.Click(simulateMove=False, waitTime=.25)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    controls = native.navigation.locate_chat(root, ticket.contact)
                    matches = self._matching_frames(auto, root, controls.messages, frames, session.max_pixels, outgoing=True)
                    if (matches and not image_controls(controls.editor)
                            and not native._input_value(controls.editor).strip()):
                        if not self._verify_animation(session, controls.messages, frames, animated, outgoing=True):
                            raise RapportError("dispatch_send_uncertain", "无法确认动态表情完整显示，剩余消息已停止。", status=409)
                        _, after, _ = read_dispatch_chat(auto, root, ticket, session.max_pixels)
                        if context_digest(after) != context_digest(before):
                            raise RapportError("dispatch_send_uncertain", "图片发送后可见文字上下文发生变化，剩余消息已停止。", "请核对微信中的实际结果，不会自动重发。", 409)
                        session.current = after
                        return True
                    time.sleep(.15)
                raise RapportError("dispatch_send_uncertain", "已点击图片发送，但无法可靠核对回执。", "请检查微信，本组不会自动重发。", 409)
            finally:
                clear_owned_clipboard(sequence)

    def _verify_animation(self, session, container, frames, animated, outgoing):
        from rapport.desktop import wechat_native as native

        seen = set()
        deadline = time.monotonic() + (4 if animated else .5)
        while time.monotonic() < deadline:
            native.navigation.verify_accessible_contact(native.navigation.locate_chat(session.root, session.ticket.contact), session.ticket.contact)
            matches = self._matching_frames(session.auto, session.root, container, frames, session.max_pixels, outgoing=outgoing)
            if len(matches) > 1:
                return False
            if len(matches) == 1:
                _control, frame_hashes = matches[0]
                if len(frame_hashes) == 1:
                    seen.update(frame_hashes)
            if len(seen) >= (2 if animated else 1):
                return True
            time.sleep(.08)
        return False

    @staticmethod
    def _matching(auto, root, container, source, max_pixels, outgoing=False):
        return [control for control, _ in StaticImageSender._matching_frames(auto, root, container, [source], max_pixels, outgoing)]

    @staticmethod
    def _matching_frames(auto, root, container, frames, max_pixels, outgoing=False):
        from rapport.desktop import wechat_native as native

        _, screen = native.grab_window(auto, root.NativeWindowHandle, root.ProcessId, max_pixels)
        region = container.BoundingRectangle
        result = []
        for control in image_controls(container):
            bounds = control.BoundingRectangle
            if outgoing and (bounds.left + bounds.right) / 2 < (region.left + region.right) / 2:
                continue
            if bounds.left < region.left or bounds.right > region.right or bounds.top < region.top or bounds.bottom > region.bottom:
                continue
            crop = screen.crop(native.pixel_rect(bounds, root.BoundingRectangle, screen))
            matching = {hashlib.sha256(source.tobytes()).hexdigest() for source in frames if matches_preview(source, crop)}
            if matching:
                result.append((control, matching))
        return result


class MediaCollectionAdapter:
    """User selects a visible media preview; the normal Save As dialog supplies bytes.

    Exact localized menu titles follow the already attributed pyweixin adapter's
    Uielements SaveMenuItem. No database/cache search, decryption or screenshot-as-original.
    The user must explicitly link this visible thumbnail to an archived message.
    """

    def __init__(self):
        self.lock = threading.RLock()
        self.selections = {}

    def preview(self, ticket, archive_id, message_ref, max_pixels):
        from rapport.desktop import wechat_native as native
        from rapport.desktop.wechat import context_digest

        if not native.NATIVE_LOCK.acquire(blocking=False):
            raise RapportError("wechat_busy", "另一个微信操作尚未结束。", status=409)
        try:
            with native.automation() as auto:
                root = native.restored_window(auto, ticket.window_id, ticket.process_id)
                native.navigation.restore(root, auto)
                _, conversation, _ = native.read_chat(auto, root, ticket.contact, max_pixels)
                if context_digest(conversation) != ticket.digest:
                    raise RapportError("wechat_context_changed", "可见消息已变化，请重新读取后选择媒体。", status=409)
                controls = native.navigation.locate_chat(root, ticket.contact)
                _, screenshot = native.grab_window(auto, ticket.window_id, ticket.process_id, max_pixels)
                out = []
                with self.lock:
                    self.selections = {key: row for key, row in self.selections.items() if time.monotonic() - row["created"] < 300}
                    for control in image_controls(controls.messages)[:16]:
                        bounds = control.BoundingRectangle
                        if bounds.width() < 64 or bounds.height() < 48:
                            continue  # excludes usual avatars/icons; user still selects exact item
                        box = native.pixel_rect(bounds, root.BoundingRectangle, screenshot)
                        crop = screenshot.crop(box)
                        pixels = hashlib.sha256(crop.tobytes()).hexdigest()
                        stream = io.BytesIO()
                        crop.thumbnail((480, 320))
                        crop.save(stream, format="PNG")
                        token = secrets.token_urlsafe(24)
                        self.selections[token] = {"owner": ticket.owner, "capture_token": ticket.token,
                                                  "archive_id": archive_id, "message_ref": message_ref,
                                                  "box": box, "pixels": pixels, "created": time.monotonic(), "used": False}
                        out.append({"selection_token": token, "preview": "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode(), "status": "thumbnail"})
                    if len(self.selections) > 64:
                        for key in sorted(self.selections, key=lambda key: self.selections[key]["created"])[:-64]:
                            del self.selections[key]
                if not out:
                    raise RapportError("wechat_media_selection_unsupported", "当前可见窗口未暴露可精确选择的媒体预览。", "请在微信正常另存为后上传原件；不会猜测附近图片或头像。", 501)
                return out
        finally:
            native.NATIVE_LOCK.release()

    def collect(self, state, ticket, selection_token, archive_id, message_ref):
        from rapport.desktop import wechat_native as native
        from rapport.desktop.wechat import context_digest

        with self.lock:
            selected = self.selections.get(selection_token)
            if not selected or selected["used"] or time.monotonic() - selected["created"] >= 300:
                raise RapportError("wechat_media_selection_expired", "媒体选择已使用或过期，请重新预览。", status=409)
            if (selected["owner"], selected["capture_token"], selected["archive_id"], selected["message_ref"]) != (ticket.owner, ticket.token, archive_id, message_ref):
                raise RapportError("wechat_media_selection_scope", "媒体选择与当前好友或归档消息不符。", status=403)
            selected["used"] = True  # native Save As is never blindly replayed
        if not native.NATIVE_LOCK.acquire(blocking=False):
            raise RapportError("wechat_busy", "另一个微信操作尚未结束，请重新预览。", status=409)
        try:
            with native.automation() as auto:
                root = native.restored_window(auto, ticket.window_id, ticket.process_id)
                native.navigation.restore(root, auto)
                _, current, _ = native.read_chat(auto, root, ticket.contact, state.settings.max_image_pixels)
                if context_digest(current) != ticket.digest:
                    raise RapportError("wechat_context_changed", "聊天变化，媒体尚未保存。", status=409)
                controls = native.navigation.locate_chat(root, ticket.contact)
                _, screen = native.grab_window(auto, ticket.window_id, ticket.process_id, state.settings.max_image_pixels)
                matches = []
                for control in image_controls(controls.messages):
                    box = native.pixel_rect(control.BoundingRectangle, root.BoundingRectangle, screen)
                    if tuple(box) == tuple(selected["box"]) and hashlib.sha256(screen.crop(box).tobytes()).hexdigest() == selected["pixels"]:
                        matches.append(control)
                if len(matches) != 1:
                    raise RapportError("wechat_media_changed", "所选媒体已经移动或变化，请重新预览。", status=409)
                matches[0].RightClick(simulateMove=False, waitTime=.2)
                menu = self._find(auto, ticket.process_id, "MenuItemControl", {"另存为...", "另存為...", "Save as..."})
                if len(menu) != 1:
                    raise RapportError("wechat_media_save_unsupported", "当前所选媒体没有可核对的另存为操作。", "可能尚未下载或已过期，请在微信处理后重试。", 501)
                menu[0].Click(simulateMove=False, waitTime=.25)
                return self._save_dialog(state, auto, ticket, archive_id, message_ref)
        finally:
            native.NATIVE_LOCK.release()

    @staticmethod
    def _find(auto, pid, kind, names):
        from rapport.desktop import wechat_native as native

        out = []
        for top in auto.GetRootControl().GetChildren()[:200]:
            if top.ProcessId != pid or top.IsOffscreen:
                continue
            for control in native.navigation.walk_containers(top):
                if control.ControlTypeName == kind and control.Name in names and native.navigation.visible(control):
                    out.append(control)
        return out

    def _save_dialog(self, state, auto, ticket, archive_id, message_ref):
        from rapport.desktop import wechat_native as native

        deadline = time.monotonic() + 4
        dialog = None
        while time.monotonic() < deadline:
            options = self._dialogs(auto, ticket.process_id)
            if len(options) == 1:
                dialog = options[0]
                break
            time.sleep(.1)
        if dialog is None:
            raise RapportError("wechat_media_save_unsupported", "未能核对微信的系统保存窗口。", status=501)
        controls = list(native.navigation.walk_containers(dialog))
        fields = [c for c in controls if c.ControlTypeName == "EditControl"
                  and c.Name in {"文件名(N):", "文件名:", "File name:", "檔案名稱(N):"} and native.navigation.visible(c)]
        buttons = [c for c in controls if c.ControlTypeName == "ButtonControl"
                   and c.Name in {"保存(S)", "保存", "Save", "儲存(S)"} and native.navigation.visible(c)]
        if len(fields) != 1 or len(buttons) != 1:
            raise RapportError("wechat_media_save_unsupported", "系统保存窗口的文件名或保存按钮无法唯一核对。", status=501)
        parent = Path(state.settings.data_dir) / "media-acquisition"
        parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="save-", dir=parent) as folder:
            # A unique, service-owned folder prevents overwrite and path injection.
            destination = str(Path(folder) / "original")
            field = fields[0]
            field.SetFocus()
            native.navigation.assert_focus(auto, field, dialog)
            pattern = field.GetValuePattern()
            if not pattern or pattern.IsReadOnly or pattern.SetValue(destination) is False:
                raise RapportError("wechat_media_save_unsupported", "系统保存窗口不支持可靠填写文件名。", status=501)
            if pattern.Value != destination:
                raise RapportError("wechat_media_save_unsupported", "未能核对保存位置。", status=501)
            native.navigation.assert_focus(auto, field, dialog)
            buttons[0].Click(simulateMove=False, waitTime=.2)
            deadline, last_size, stable = time.monotonic() + 20, -1, 0
            file, completed = None, False
            while time.monotonic() < deadline:
                candidates = [entry for entry in Path(folder).iterdir() if entry.is_file() and not entry.is_symlink()]
                if len(candidates) > 1:
                    raise RapportError("wechat_media_save_uncertain", "保存窗口产生了多个文件，无法确认原件。", status=409)
                if candidates:
                    file = candidates[0]
                    size = file.stat().st_size
                    stable = stable + 1 if size == last_size and size > 0 else 0
                    last_size = size
                    dialog_open = any(c.NativeWindowHandle == dialog.NativeWindowHandle for c in self._dialogs(auto, ticket.process_id))
                    if stable >= 3 and not dialog_open:
                        completed = True
                        break
                time.sleep(.25)
            if not file or not completed:
                raise RapportError("wechat_media_save_unavailable", "微信没有提供完整原文件，未将缩略图作为原件保存。", status=409)
            # add_asset performs format/hash validation and encrypted chunk storage.
            with file.open("rb") as source:
                return state.archives.add_asset(archive_id, ticket.relationship_id, message_ref, source,
                                                filename=file.name, status="original", caption="微信另存为取得的媒体文件",
                                                source_kind="wechat_save_as")

    @staticmethod
    def _dialogs(auto, process_id):
        from rapport.desktop import wechat_native as native

        result = {}
        for root in auto.GetRootControl().GetChildren()[:200]:
            if root.ProcessId != process_id or root.IsOffscreen:
                continue
            for control in native.navigation.walk_containers(root):
                if control.ClassName == "#32770" and not control.IsOffscreen:
                    result[control.NativeWindowHandle] = control
        return list(result.values())
