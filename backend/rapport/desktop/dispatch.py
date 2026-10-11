"""Explicitly confirmed, ordered desktop sends with encrypted durable receipts.

Preview is side-effect free. A receipt is persisted as uncertain BEFORE each native
operation, so process interruption and client retries can never replay that operation.
The old fill endpoint remains a separate, non-sending operation.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from contextlib import contextmanager

from rapport.db import Setting
from rapport.desktop import wechat_native as native
from rapport.desktop.wechat import context_digest, database_rows
from rapport.errors import RapportError

PREFIX = "dispatch."
TTL = 600


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


class DispatchStore:
    def __init__(self, state):
        self.state = state
        self.lock = threading.RLock()

    @staticmethod
    def key(token):
        return PREFIX + hashlib.sha256(token.encode()).hexdigest()[:48]

    def _save(self, token, receipt):
        # Do not put contact names, drafts, paths or tokens in unencrypted metadata.
        key = self.key(token)
        with self.state.db.session() as session:
            row = session.get(Setting, key)
            blob_id = row.value["blob_id"] if row else secrets.token_hex(16)
            self.state.blobs.put(blob_id, json.dumps(receipt, ensure_ascii=False).encode())
            value = {"blob_id": blob_id, "relationship_id": receipt["relationship_id"], "created_at": receipt["created_at"]}
            if row:
                row.value = value
            else:
                session.add(Setting(key=key, value=value))

    def get(self, token, owner):
        with self.lock, self.state.db.session() as session:
            row = session.get(Setting, self.key(token))
            data = self.state.blobs.get(row.value["blob_id"]) if row else None
            if not data:
                raise RapportError("dispatch_not_found", "发送预览不存在，请重新预览。", status=404)
            receipt = json.loads(data)
            if receipt["owner"] != owner:
                raise RapportError("dispatch_scope", "这份发送预览不属于当前浏览器会话。", status=403)
            if receipt["status"] == "preview" and time.time() - receipt["created_at"] >= TTL:
                raise RapportError("dispatch_expired", "发送预览已过期，请重新读取并预览。", status=409)
            return receipt

    def preview(self, owner, ticket, items):
        require_supported_items(ticket, items)
        token = secrets.token_urlsafe(24)
        binding = {"contact": ticket.contact, "relationship_id": ticket.relationship_id,
                   "import_id": ticket.import_id, "capture_token": ticket.token, "context_digest": ticket.digest,
                   "window_id": ticket.window_id, "process_id": ticket.process_id, "items": items,
                   "context_source": getattr(ticket, "context_source", "visual"),
                   "source_binding": getattr(ticket, "source_binding", None)}
        receipt = {**binding, "owner": owner, "digest": digest(binding), "created_at": time.time(),
                   "status": "preview", "results": [{"index": i, "status": "pending", "code": None} for i in range(len(items))]}
        with self.lock:
            self._save(token, receipt)
        return token, receipt

    def execute(self, token, owner, expected_digest, operation):
        with self.lock:
            receipt = self.get(token, owner)
            if not secrets.compare_digest(receipt["digest"], expected_digest):
                raise RapportError("dispatch_changed", "发送内容已变化，请重新预览。", status=409)
            if receipt["status"] != "preview":
                # Includes interrupted/running receipts. Never repeat uncertain sends.
                return receipt
            receipt["status"] = "running"
            self._save(token, receipt)

            def update(index, status, code=None):
                receipt["results"][index] = {"index": index, "status": status, "code": code}
                self._save(token, receipt)

            try:
                operation(receipt, update)
                receipt["status"] = "complete" if all(r["status"] == "sent" for r in receipt["results"]) else "stopped"
            except Exception as error:
                # No exception text: native libraries can include private draft contents.
                code = error.code if isinstance(error, RapportError) else "dispatch_interrupted"
                receipt["failure"] = {"code": code, "stage": getattr(error, "dispatch_stage", "operation"),
                                      "identity": getattr(error, "dispatch_identity", None)}
                current = next((r for r in receipt["results"] if r["status"] in {"pending", "uncertain"}), None)
                if current:
                    update(current["index"], "uncertain" if current["status"] == "uncertain" else "failed", code)
                receipt["status"] = "stopped"
            self._save(token, receipt)
            return receipt

    def clear(self, relationship_id=None):
        with self.lock, self.state.db.session() as session:
            rows = session.query(Setting).filter(Setting.key.startswith(PREFIX)).all()
            for row in rows:
                if relationship_id is None or row.value.get("relationship_id") == relationship_id:
                    self.state.blobs.delete(row.value["blob_id"])
                    session.delete(row)


def public_receipt(token, receipt):
    return {"token": token, "digest": receipt["digest"], "contact": receipt["contact"],
            "relationship_id": receipt["relationship_id"], "import_id": receipt["import_id"],
            "items": receipt["items"], "status": receipt["status"], "results": receipt["results"],
            "failure": receipt.get("failure"),
            "expires_in_s": max(0, int(TTL - (time.time() - receipt["created_at"])))}


def _comparable(text: str) -> str:
    """OCR joins the lines of one bubble and may widen punctuation: compare without whitespace, NFKC."""
    from rapport.desktop.wechat import normalized_title

    return normalized_title(text or "")


def _rows(conversation):
    from rapport.desktop.wechat import text_events

    return [(message.sender.value, _comparable(message.text)) for message in text_events(conversation)]


def editor_is_empty(editor):
    from rapport.desktop.media import image_controls

    return not native._input_value(editor).strip() and not image_controls(editor)


def _identity_diagnostic(controls, contact):
    from rapport.desktop.wechat import normalized_title

    # Only equality/availability is persisted. Never put another recipient name or
    # message content into an operational receipt when the user changed windows.
    expected = normalized_title(contact)
    title = normalized_title(getattr(controls.title, "Name", "") or "")
    editor = normalized_title(getattr(controls.editor, "Name", "") or "")
    return {"title_exact": title == expected, "editor_exact": editor == expected,
            "title_available": bool(title), "editor_available": bool(editor),
            "title_is_accessible_label": getattr(controls.title, "AutomationId", "").endswith(native.navigation.TITLE_LABEL_SUFFIX)}


def read_dispatch_chat(auto, root, ticket, max_pixels, *, state=None):
    """Sending requires both exact accessible identities, with no OCR fallback.

    The screenshot reader verifies the same identity again before/after OCR; its
    strict flag prevents a title disappearing mid-capture from switching to OCR.
    Missing or conflicting identity remains a hard stop, never a partial match.
    """
    stage, controls = "recipient_containers", None
    try:
        controls = native.navigation.locate_chat(root, ticket.contact)
        stage = "recipient_dual_accessibility"
        if not native.navigation.verify_accessible_contact(controls, ticket.contact):
            raise RapportError("dispatch_identity_unavailable", "发送前未能取得两个完整的无障碍好友名称，本组已停止。", "请等待微信界面稳定后重新读取；不会以OCR或部分姓名放行发送。", 409)
        if getattr(ticket, "context_source", "visual") == "dsh_database":
            stage = "current_chat_database"
            if state is None or not ticket.source_binding:
                raise RapportError("dsh_binding_missing", "数据库回复绑定已失效，请重新准备回复。", status=409)
            native.checked_window(auto, ticket.window_id, ticket.process_id)
            current = state.dsh.current_context(ticket.source_binding, ticket.contact, ticket.relationship_id, ticket.process_id)
            native.checked_window(auto, ticket.window_id, ticket.process_id)
            latest = recipient_controls(root, ticket)
            if not auto.ControlsAreSame(controls.editor, latest.editor):
                raise RapportError("wechat_context_changed", "读取期间聊天输入框已变化。", status=409)
            return None, current, latest.editor
        stage = "current_chat_pixels"
        return native.read_chat(auto, root, ticket.contact, max_pixels, cached_controls=controls, require_accessible_contact=True)
    except RapportError as error:
        error.dispatch_stage = stage
        if controls is not None:
            try:
                error.dispatch_identity = _identity_diagnostic(controls, ticket.contact)
            except Exception:
                error.dispatch_identity = {"snapshot_available": False}
        raise


def recipient_controls(root, ticket):
    """Accessibility-only recipient check (title and editor names both exact); no OCR, so it is fast."""
    controls = native.navigation.locate_chat(root, ticket.contact)
    if not native.navigation.verify_accessible_contact(controls, ticket.contact):
        error = RapportError("dispatch_identity_unavailable", "点击发送前未能取得两个完整的无障碍好友名称，本项未发送。", status=409)
        error.dispatch_stage = "recipient_before_click"
        raise error
    return controls


def verify_text_append(before, after, text):
    """Confirm an outgoing append using an unchanged, unambiguous overlap."""
    previous, current = _rows(before), _rows(after)
    if len(current) < 2 or current[-1] != ("me", _comparable(text)):
        return False
    overlap = current[:-1]
    if len(overlap) > len(previous) or previous[-len(overlap):] != overlap:
        return False
    # Repeated identical visible frames and duplicate suffixes cannot prove send.
    return current != previous and previous.count(overlap[-1]) == 1


def verify_database_append(before, after, text):
    """Require one new, server-assigned outgoing row in the same account/chat.

    Exact text and stable source identities distinguish repeated messages; an
    existing pending row gaining a server ID cannot count as a newly sent row.
    This verifies local WeChat's server-assigned record, not recipient reading.
    """
    if any(c.meta.get("context_source") != "dsh_database" for c in (before, after)):
        return False
    if any(not before.meta.get(k) or before.meta[k] != after.meta.get(k) for k in ("account", "contact_id")):
        return False
    previous, current = database_rows(before), database_rows(after)
    if not current or len({r[0] for r in current}) != len(current):
        return False
    message = after.messages[-1]
    try:
        assigned = int(message.meta.get("server_id") or 0) != 0
    except (ValueError, TypeError):
        return False
    if not assigned or message.sender.value != "me" or message.type.value != "text" or message.text != text or not message.meta.get("sender_verified"):
        return False
    def local_key(message):
        return message.meta.get("shard"), message.meta.get("local_id")

    if not all(local_key(message)) or local_key(message) in {local_key(m) for m in before.messages}:
        return False
    if message.id in {m.id for m in before.messages}:
        return False
    overlap = current[:-1]
    if not previous:
        return not overlap
    return bool(overlap) and len(overlap) <= len(previous) and previous[-len(overlap):] == overlap


def require_supported_items(ticket, items):
    if getattr(ticket, "context_source", "visual") == "dsh_database" and any(i["kind"] != "text" for i in items):
        raise RapportError("dsh_text_only", "当前数据库回复绑定支持文字发送。", "发送图片或表情请先选择“准备图片/表情回复（需画面）”。", 409)


def ticket_reader(auto, root, ticket, max_pixels, state):
    if getattr(ticket, "context_source", "visual") == "dsh_database":
        return read_dispatch_chat(auto, root, ticket, max_pixels, state=state)
    return read_dispatch_chat(auto, root, ticket, max_pixels)


def require_foreground(auto, root):
    foreground = auto.GetForegroundControl()
    if not foreground or foreground.NativeWindowHandle != root.NativeWindowHandle or foreground.ProcessId != root.ProcessId:
        raise RapportError("wechat_focus_changed", "微信已不在前台，本次操作已停止。", status=409)


def click_database_send(auto, root, button):
    """Hit-test only the actual send target; unrelated overlays do not matter.

    Do not retry or fall back after a click. UIA Invoke is intentionally avoided:
    Qt providers may return success without activating the send control.
    """
    require_foreground(auto, root)
    box = button.BoundingRectangle
    x, y = (box.left + box.right) // 2, (box.top + box.bottom) // 2
    if box.right <= box.left or box.bottom <= box.top or not any(m.left <= x < m.right and m.top <= y < m.bottom for m in auto.GetMonitorsRect()):
        raise RapportError("dispatch_send_control_obscured", "发送按钮不在可操作的屏幕区域。", status=409)
    hit = auto.ControlFromPoint(x, y)
    for _ in range(16):
        if hit is None:
            break
        if auto.ControlsAreSame(hit, button):
            if button.IsEnabled:
                # Absolute coordinates are exactly those whose ownership was checked.
                # Module-level Click has no simulateMove argument (Control.Click does).
                auto.Click(x, y, waitTime=.25)
                return
            break
        hit = hit.GetParentControl()
    raise RapportError("dispatch_send_control_obscured", "微信发送按钮被遮挡，本项未点击发送。", "请移开发送按钮上的遮挡后重新准备回复。", 409)


class WindowsDispatchAdapter:
    """Text and static image sender with per-operation verification.

    Reuses existing attributed pyweixin navigation; no new third-party selectors.
    A unique accessible send button within the editor region is mandatory. Missing
    or ambiguous controls fail closed instead of guessing coordinates/Enter mode.
    """

    def __init__(self, state=None):
        self.state = state

    @contextmanager
    def session(self, ticket, items, max_pixels):
        from rapport.desktop.media import SENDABLE_MIME

        require_supported_items(ticket, items)
        if any(item["kind"] == "text" and len(item["text"]) > 2000 for item in items):
            raise RapportError("dispatch_text_too_long", "单条微信消息请控制在2000字以内，请拆分后重新预览。", status=422)
        if any(item["kind"] != "text" and (self.state is None or item.get("mime") not in SENDABLE_MIME) for item in items):
            raise RapportError("dispatch_media_unsupported", "当前媒体预览驱动支持 PNG/JPEG/GIF 原件；此组包含尚不支持的素材，本组没有发送。", "只有实际归档原件可以发送，不会凭关键词猜选微信收藏表情。", 501)
        if not native.NATIVE_LOCK.acquire(blocking=False):
            raise RapportError("wechat_busy", "另一个微信操作尚未结束。", status=409)
        try:
            with native.automation() as auto:
                root = native.restored_window(auto, ticket.window_id, ticket.process_id)
                foreground = auto.GetForegroundControl()
                # Avoid unnecessarily reactivating an already active Qt window:
                # activation can rebuild accessible title controls asynchronously.
                if not foreground or foreground.NativeWindowHandle != ticket.window_id:
                    native.navigation.restore(root, auto)
                _, conversation, editor = ticket_reader(auto, root, ticket, max_pixels, self.state)
                if context_digest(conversation) != ticket.digest:
                    raise RapportError("wechat_context_changed", "当前对话已变化，请重新读取并预览。", status=409)
                if not editor_is_empty(editor):
                    raise RapportError("wechat_input_not_empty", "微信输入框已有草稿，本组没有发送。", status=409)
                self._send_button(root, editor)
                yield _WindowsTextSession(auto, root, ticket, max_pixels, conversation, self.state)
        finally:
            native.NATIVE_LOCK.release()

    @staticmethod
    def _send_button(root, editor):
        region = editor.BoundingRectangle
        controls = []
        for control in native.navigation.walk_containers(root):
            if control.ControlTypeName != "ButtonControl" or not native.navigation.visible(control):
                continue
            box = control.BoundingRectangle
            if (control.Name in {"发送", "发送(S)", "發送", "傳送", "Send", "Send (S)"}
                    and region.left <= (box.left + box.right) / 2 <= region.right
                    and region.top <= box.top <= root.BoundingRectangle.bottom):
                controls.append(control)
        if len(controls) != 1:
            raise RapportError("dispatch_send_control_unsupported", "当前微信没有可唯一核对的发送按钮，本组没有发送。", "请在微信手动发送，或使用已支持的客户端布局。", 501)
        return controls[0]


class _WindowsTextSession:
    def __init__(self, auto, root, ticket, max_pixels, conversation, state=None):
        self.auto, self.root, self.ticket, self.max_pixels = auto, root, ticket, max_pixels
        self.current = conversation
        self.state = state

    @staticmethod
    def _assert_editor_focus(auto, editor, root, stage):
        try:
            native.navigation.assert_focus(auto, editor, root)
        except RapportError as error:
            error.dispatch_stage = stage
            raise

    def send(self, item):
        require_supported_items(self.ticket, [item])
        if item["kind"] != "text":
            from rapport.desktop.media import StaticImageSender

            return StaticImageSender(self.state).send(self, item)
        auto, root, ticket = self.auto, self.root, self.ticket
        database = getattr(ticket, "context_source", "visual") == "dsh_database"
        _, before, editor = ticket_reader(auto, root, ticket, self.max_pixels, self.state)
        if context_digest(before) != context_digest(self.current) or not editor_is_empty(editor):
            raise RapportError("wechat_context_changed", "对话或输入框已变化，剩余消息未发送。", status=409)
        button = WindowsDispatchAdapter._send_button(root, editor)
        editor.SetFocus()
        self._assert_editor_focus(auto, editor, root, "text_focus_before_fill")
        # Recheck recipient/context after focus, then use a literal value (no key grammar).
        _, focused, latest_editor = ticket_reader(auto, root, ticket, self.max_pixels, self.state)
        if not auto.ControlsAreSame(editor, latest_editor) or context_digest(focused) != context_digest(before):
            raise RapportError("wechat_context_changed", "焦点或聊天内容已变化。", status=409)
        if not editor_is_empty(editor):
            raise RapportError("wechat_input_not_empty", "微信输入框已有内容。", status=409)
        self._assert_editor_focus(auto, editor, root, "text_focus_after_recheck")
        if database:
            require_foreground(auto, root)
        text = item["text"]
        pattern = editor.GetValuePattern()
        if pattern and not pattern.IsReadOnly:
            if pattern.SetValue(text) is False:
                raise RapportError("dispatch_fill_uncertain", "无法确认草稿是否填入，请检查微信。", status=409)
        else:
            auto.SetClipboardText(text)
            editor.SendKeys("{Ctrl}v", waitTime=.2)
        if native._input_value(editor).replace("\r\n", "\n").rstrip("\r\n") != text.replace("\r\n", "\n").rstrip("\r\n"):
            raise RapportError("dispatch_fill_uncertain", "微信输入内容未能与预览核对，本项未点击发送。", status=409)
        # From the fill to the click only accessibility reads run: an OCR pass here took long
        # enough for focus to move (the 2026-09 real send stopped with wechat_focus_changed).
        # The chat pixels were verified right before the fill; the receipt is verified by OCR after.
        latest = recipient_controls(root, ticket)
        if not auto.ControlsAreSame(editor, latest.editor):
            raise RapportError("wechat_context_changed", "聊天输入框已经变化，本项未点击发送。", status=409)
        latest_button = WindowsDispatchAdapter._send_button(root, editor)
        if not auto.ControlsAreSame(button, latest_button) or not latest_button.IsEnabled:
            raise RapportError("dispatch_send_control_changed", "发送按钮发生变化，本项未点击发送。", status=409)
        # The single external action follows explicit app confirmation. No Enter or guessed
        # keyboard shortcut and no retry; focus goes back only to the verified editor of the
        # verified window, and the click happens only if that focus is confirmed.
        editor.SetFocus()
        self._assert_editor_focus(auto, editor, root, "text_focus_before_click")
        if native._input_value(editor).replace("\r\n", "\n").rstrip("\r\n") != text.replace("\r\n", "\n").rstrip("\r\n"):
            raise RapportError("wechat_input_changed", "发送前草稿被修改，本项未点击发送。", status=409)
        from rapport.desktop.media import image_controls

        if image_controls(editor):
            raise RapportError("wechat_input_changed", "文字草稿中出现了未确认的图片，本项未点击发送。", status=409)
        if database:
            click_database_send(auto, root, latest_button)
        else:
            latest_button.Click(simulateMove=False, waitTime=.25)
        deadline = time.monotonic() + (10 if database else 4)
        while time.monotonic() < deadline:
            try:
                _, after, latest_editor = ticket_reader(auto, root, ticket, self.max_pixels, self.state)
            except RapportError as error:
                if database and error.code == "dsh_database_changing":
                    time.sleep(.2)
                    continue
                raise
            verified = verify_database_append(before, after, text) if database else verify_text_append(before, after, text)
            if not native._input_value(latest_editor).strip() and verified:
                self.current = after
                return True
            time.sleep(.15)
        raise RapportError("dispatch_send_uncertain", "已点击发送，但无法可靠确认回执。", "请在微信核对，本项及剩余消息不会自动重发。", 409)
