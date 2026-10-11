"""Visible WeChat sticker search with UIA-first labels and selected-tile OCR.

Search reads only the emoji popover. A send reopens the same query and checks the
recipient, candidate accessibility label, geometry and pixels immediately before
the single click. Search alone never sends; uncertain clicks are never retried.
"""

from __future__ import annotations

import base64
import hashlib
import io
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass

from rapport.desktop import wechat_native as native
from rapport.desktop.dispatch import editor_is_empty
from rapport.errors import RapportError

POPOVER_CLASS = "mmui::EmoticonPopover"
RESULT_CLASS = "mmui::EmoticonSearchPageBaseView"
STICKER_CLASSES = {"client-image sticker", "online-image sticker"}
SEARCH_TAB = "搜索表情"
EMOJI_BUTTON = "发送表情(Alt+E)"
TTL = 600


def _walk(root, limit=800):
    queue = deque([(root, 0)])
    for _ in range(limit):
        if not queue:
            break
        node, depth = queue.popleft()
        yield node
        if depth < 20:
            queue.extend((child, depth + 1) for child in node.GetChildren())


def _one(items, code, message):
    if len(items) != 1:
        raise RapportError(code, message, status=409)
    return items[0]


def _popover(auto, root, wait=0):
    deadline = time.monotonic() + wait
    while True:
        windows = [window for window in auto.GetRootControl().GetChildren()[:200]
                   if window.ProcessId == root.ProcessId and window.ClassName == POPOVER_CLASS
                   and native.navigation.visible(window)]
        if len(windows) == 1:
            return windows[0]
        if len(windows) > 1 or time.monotonic() >= deadline:
            raise RapportError("sticker_panel_unavailable", "没有找到唯一的微信表情面板。", status=409)
        time.sleep(.1)


def _open_search(auto, root, contact, query):
    controls = native.navigation.locate_chat(root, contact)
    if not native.navigation.verify_accessible_contact(controls, contact) or not editor_is_empty(controls.editor):
        raise RapportError("sticker_target_changed", "好友或输入框发生变化，未搜索表情。", status=409)
    panels = [window for window in auto.GetRootControl().GetChildren()[:200]
              if window.ProcessId == root.ProcessId and window.ClassName == POPOVER_CLASS
              and native.navigation.visible(window)]
    if not panels:
        native.navigation.restore(root, auto)
        editor = controls.editor.BoundingRectangle
        buttons = [node for node in native.navigation.walk_containers(root)
                   if node.ControlTypeName == "ButtonControl" and node.Name == EMOJI_BUTTON
                   and native.navigation.visible(node) and node.BoundingRectangle.left >= editor.left - 40
                   and editor.bottom <= node.BoundingRectangle.top <= root.BoundingRectangle.bottom]
        _one(buttons, "sticker_button_unavailable", "无法定位唯一的发送表情入口。").Click(
            simulateMove=False, waitTime=.2)
    panel = _popover(auto, root, wait=3)
    tabs = [node for node in _walk(panel) if node.ControlTypeName == "TabItemControl"
            and node.Name == SEARCH_TAB and native.navigation.visible(node)]
    _one(tabs, "sticker_search_unavailable", "无法定位表情搜索入口。").Click(
        simulateMove=False, waitTime=.2)
    panel = _popover(auto, root, wait=3)
    edits = [node for node in _walk(panel) if node.ControlTypeName == "EditControl"
             and node.Name in {"搜索", "Search"} and native.navigation.visible(node)]
    edit = _one(edits, "sticker_search_unavailable", "无法定位表情搜索框。")
    edit.SetFocus()
    pattern = edit.GetValuePattern()
    if not pattern or pattern.IsReadOnly or pattern.SetValue(query) is False or pattern.Value != query:
        raise RapportError("sticker_search_failed", "无法准确填写表情搜索词。", status=409)
    return panel


def _visible_candidates(panel):
    result = _one([node for node in _walk(panel) if node.ClassName == RESULT_CLASS
                   and native.navigation.visible(node)],
                  "sticker_results_unavailable", "无法定位表情搜索结果区域。")
    area = result.BoundingRectangle
    out = []
    # Qt places the result grid beside EmoticonSearchPageBaseView in the tree;
    # the latter supplies its visible viewport bounds, not the children.
    for node in _walk(panel):
        if node.ControlTypeName != "ListItemControl" or node.ClassName not in STICKER_CLASSES:
            continue
        box = node.BoundingRectangle
        # UIA reports virtual off-screen rows as visible; require full on-screen tile.
        if (node.IsOffscreen or box.left < area.left or box.right > area.right
                or box.top < area.top or box.bottom > area.bottom
                or box.width() < 48 or box.height() < 48):
            continue
        out.append(node)
    out.sort(key=lambda node: (node.BoundingRectangle.top, node.BoundingRectangle.left))
    return out[:20]


def _search_and_capture(auto, root, contact, query):
    from PIL import ImageGrab

    chat = native.navigation.locate_chat(root, contact)
    if not native.navigation.verify_accessible_contact(chat, contact) or not editor_is_empty(chat.editor):
        raise RapportError("sticker_target_changed", "好友或输入框发生变化，未搜索表情。", status=409)
    chat_signature = _chat_signature(chat.messages)
    panel = _open_search(auto, root, contact, query)
    latest, stable = [], 0
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        panel = _popover(auto, root)
        nodes = _visible_candidates(panel)
        signature = [(node.ClassName, node.Name, node.BoundingRectangle.left,
                      node.BoundingRectangle.top) for node in nodes]
        stable = stable + 1 if signature and signature == latest else 0
        latest = signature
        if stable >= 1:
            break
        time.sleep(.2)
    nodes = _visible_candidates(panel)
    foreground = auto.GetForegroundControl()
    if not foreground or foreground.NativeWindowHandle != panel.NativeWindowHandle:
        raise RapportError("sticker_panel_obscured", "表情面板已失去焦点，本次停止。", status=409)
    bounds = panel.BoundingRectangle
    screenshot = ImageGrab.grab(bbox=(bounds.left, bounds.top, bounds.right, bounds.bottom), all_screens=True)
    results = []
    for node in nodes:
        box = node.BoundingRectangle
        hit = auto.ControlFromPoint((box.left + box.right) // 2, (box.top + box.bottom) // 2)
        # Results are a Chromium child process; its top-level control has a
        # different HWND. Check the exact hit-tested list item instead.
        if not hit or not auto.ControlsAreSame(hit, node):
            continue
        image = screenshot.crop((box.left - bounds.left, box.top - bounds.top,
                                 box.right - bounds.left, box.bottom - bounds.top)).convert("RGB")
        output = io.BytesIO()
        image.save(output, format="PNG")
        raw = output.getvalue()
        results.append({"index": len(results), "label": node.Name or "",
                        "class_name": node.ClassName, "sha256": hashlib.sha256(raw).hexdigest(),
                        "image": raw, "chat_signature": chat_signature,
                        "rect": (box.left - bounds.left, box.top - bounds.top,
                                                box.right - bounds.left, box.bottom - bounds.top)})
    return results


@dataclass
class StickerChoice:
    token: str
    owner: str
    relationship_id: str
    capture_token: str
    query: str
    index: int
    label: str
    class_name: str
    sha256: str
    image: bytes
    chat_signature: str
    rect: tuple[int, int, int, int]
    created: float

    def public(self):
        generic = self.label.startswith("表情，来自") or not self.label
        return {"token": self.token, "index": self.index, "label": self.label,
                "needs_ocr": generic, "sha256": self.sha256,
                "preview_url": "data:image/png;base64," + base64.b64encode(self.image).decode()}


class StickerSearchStore:
    def __init__(self):
        self.lock = threading.RLock()
        self.items: dict[str, StickerChoice] = {}

    def add(self, owner, ticket, query, results):
        now = time.monotonic()
        with self.lock:
            self.items = {key: item for key, item in self.items.items() if now - item.created < TTL}
            while len(self.items) + len(results) > 80 and self.items:
                self.items.pop(min(self.items, key=lambda key: self.items[key].created))
            choices = []
            for row in results:
                choice = StickerChoice(secrets.token_urlsafe(24), owner, ticket.relationship_id,
                                       ticket.token, query, row["index"], row["label"], row["class_name"],
                                       row["sha256"], row["image"], row["chat_signature"], row["rect"], now)
                self.items[choice.token] = choice
                choices.append(choice.public())
            return choices

    def get(self, token, owner, ticket):
        with self.lock:
            choice = self.items.get(token)
            if not choice or time.monotonic() - choice.created >= TTL:
                self.items.pop(token, None)
                raise RapportError("sticker_choice_expired", "表情选择已失效，请重新搜索。", status=409)
            if (choice.owner != owner or choice.relationship_id != ticket.relationship_id
                    or choice.capture_token != ticket.token):
                raise RapportError("sticker_choice_scope", "表情选择不属于当前好友和浏览器。", status=403)
            return choice

    def clear(self, relationship_id=None):
        with self.lock:
            self.items = {key: item for key, item in self.items.items()
                          if relationship_id is not None and item.relationship_id != relationship_id}


def search_stickers(ticket, query, max_pixels):
    if not query or len(query) > 24 or any(ord(char) < 32 for char in query):
        raise RapportError("sticker_query_invalid", "搜索词需要 1–24 个可见字符。", status=422)
    if not native.NATIVE_LOCK.acquire(blocking=False):
        raise RapportError("wechat_busy", "另一个微信窗口操作尚未结束。", status=409)
    try:
        for attempt in range(2):
            try:
                with native.automation() as auto:
                    root = native.restored_window(auto, ticket.window_id, ticket.process_id)
                    return _search_and_capture(auto, root, ticket.contact, query)
            except RapportError as error:
                # Opening the non-sending Qt popover can race its Chromium child.
                # Only search itself may retry; never a candidate click/send.
                if attempt or error.code not in {"sticker_panel_unavailable", "wechat_window_unavailable"}:
                    raise
                time.sleep(.25)
    finally:
        native.NATIVE_LOCK.release()


def inspect_choice(choice):
    from PIL import Image

    from rapport.ocr.engine import run_sticker_thumbnail

    if not (choice.label.startswith("表情，来自") or not choice.label):
        return {"ocr_text": None, "ocr_used": False}
    image = Image.open(io.BytesIO(choice.image)).convert("RGB")
    boxes = run_sticker_thumbnail(image)
    return {"ocr_text": " ".join(box.text for box in boxes if box.score >= .35)[:100] or None,
            "ocr_used": True}


def _chat_signature(messages):
    """Never persist accessible message text; compare one-way digests only."""
    rows = []
    for child in messages.GetChildren()[-8:]:
        box = child.BoundingRectangle
        rows.append((child.ClassName, child.AutomationId, child.Name,
                     box.left, box.top, box.right, box.bottom))
    return hashlib.sha256(repr(rows).encode()).hexdigest()


def send_choice(ticket, choice, max_pixels, mark_uncertain):
    """Single selected sticker click after full live revalidation; no retry."""
    if not native.NATIVE_LOCK.acquire(blocking=False):
        raise RapportError("wechat_busy", "另一个微信窗口操作尚未结束。", status=409)
    try:
        with native.automation() as auto:
            root = native.restored_window(auto, ticket.window_id, ticket.process_id)
            native.navigation.restore(root, auto)
            chat = native.navigation.locate_chat(root, ticket.contact)
            if (not native.navigation.verify_accessible_contact(chat, ticket.contact)
                    or not editor_is_empty(chat.editor)
                    or _chat_signature(chat.messages) != choice.chat_signature):
                raise RapportError("sticker_context_changed", "聊天或输入框已变化，没有点击表情。", status=409)
            before = choice.chat_signature
            results = _search_and_capture(auto, root, ticket.contact, choice.query)
            if choice.index >= len(results):
                raise RapportError("sticker_candidate_changed", "搜索结果位置已变化，没有点击表情。", status=409)
            row = results[choice.index]
            if any(row[key] != getattr(choice, key) for key in ("label", "class_name", "sha256", "rect")):
                raise RapportError("sticker_candidate_changed", "表情名称或图片已变化，没有点击。", status=409)
            panel = _popover(auto, root)
            nodes = _visible_candidates(panel)
            panel_box = panel.BoundingRectangle
            matching = [node for node in nodes if node.Name == choice.label and node.ClassName == choice.class_name
                        and (node.BoundingRectangle.left - panel_box.left,
                             node.BoundingRectangle.top - panel_box.top,
                             node.BoundingRectangle.right - panel_box.left,
                             node.BoundingRectangle.bottom - panel_box.top) == choice.rect]
            selected = _one(matching, "sticker_candidate_changed", "表情控件已变化，没有点击。")
            if not native.navigation.verify_accessible_contact(native.navigation.locate_chat(root, ticket.contact), ticket.contact):
                raise RapportError("sticker_target_changed", "目标好友不再可核对，没有点击表情。", status=409)
            if not editor_is_empty(native.navigation.locate_chat(root, ticket.contact).editor):
                raise RapportError("wechat_input_not_empty", "微信输入框已有草稿，没有点击表情。", status=409)
            if _chat_signature(native.navigation.locate_chat(root, ticket.contact).messages) != before:
                raise RapportError("sticker_context_changed", "搜索期间聊天内容发生变化，没有点击。", status=409)
            foreground = auto.GetForegroundControl()
            if not foreground or foreground.NativeWindowHandle != panel.NativeWindowHandle:
                raise RapportError("sticker_panel_obscured", "表情面板不在前台，没有点击。", status=409)
            box = selected.BoundingRectangle
            hit = auto.ControlFromPoint((box.left + box.right) // 2, (box.top + box.bottom) // 2)
            if not hit or not auto.ControlsAreSame(hit, selected):
                raise RapportError("sticker_candidate_changed", "即将点击的位置不再是选中的表情。", status=409)
            # Clicking a result sends directly in this client; persist uncertain first.
            mark_uncertain()
            selected.Click(simulateMove=False, waitTime=.25)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                controls = native.navigation.locate_chat(root, ticket.contact)
                visible_items = controls.messages.GetChildren()
                latest_is_media = bool(visible_items) and any(
                    word in visible_items[-1].ClassName for word in ("Image", "Emoticon", "Bubble"))
                if (native.navigation.verify_accessible_contact(controls, ticket.contact)
                        and editor_is_empty(controls.editor)
                        and latest_is_media
                        and _chat_signature(controls.messages) != before):
                    return True
                time.sleep(.15)
            raise RapportError("dispatch_send_uncertain", "已点击表情，但无法核对微信消息回执。", status=409)
    finally:
        native.NATIVE_LOCK.release()
