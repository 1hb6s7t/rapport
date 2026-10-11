"""Narrow pyweixin navigation adapter for Python UIAutomation.

Adapted 2026-09-22 from Hello-Mr-Crab/pywechat, commit
ebe8d4be9347816d221c38843e5b9e5e480bd8fb:
src/pyweixin/Uielements.py and WeChatTools.Tools.get_search_result/search_friend.
This module is LGPL-2.1; see LICENSE-pyweixin.txt. Changes: UIAutomation backend,
bounded traversal, exact unique contacts only, no network search or message send.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

from rapport.desktop.dsh_input_name import editor_matches_contact
from rapport.desktop.wechat import normalized_title
from rapport.errors import RapportError

# Selectors reused from pyweixin; chat_message_list and title-bar class were
# additionally verified on Weixin 4.1.13.12. Only container bounds are read.
MAIN_CLASS = "mmui::MainWindow"
LOGIN_CLASS_HINT = "Login"  # mmui::LoginWindow on Weixin 4.x
READY_IDS = {"chat_input_field", "chat_message_list", "session_list"}
TREE_WAIT_S = 10.0
SEARCH_CLASS = "mmui::XValidatorTextEdit"
SEARCH_NAMES = {"搜索", "Search", "搜尋"}
SEARCH_LIST_ID = "search_list"
SESSION_LIST_ID = "session_list"
INPUT_ID = "chat_input_field"
MESSAGE_ID = "chat_message_list"
TITLE_CLASS = "mmui::ChatTitleBarMasterView"
TITLE_LABEL_SUFFIX = ".current_chat_name_label"
TITLE_CONTAINER_SUFFIX = ".title_h_view.left_v_view"
CONTACT_CLASS = "mmui::SearchContentCellView"
CONTACT_SECTIONS = {"联系人", "聯絡人", "Contacts", "最近使用", "最常使用", "Recent used", "Most used"}
SECTION_LABELS = CONTACT_SECTIONS | {"群聊", "群組", "Groups", "服务号", "服務賬號", "Service Accounts",
    "公众号", "官方賬號", "Official Accounts", "聊天记录", "聊天記錄", "Chat History", "功能", "Features",
    "搜索网络结果", "Search web"}


def walk_containers(root):
    """Do not traverse message/session lists or read their item names/text."""
    queue = deque([(root, 0)])
    count = 0
    while queue and count < 500:
        control, depth = queue.popleft()
        count += 1
        try:
            kind = control.ControlTypeName
            yield control
            if depth < 28 and kind not in {"ListControl", "ListItemControl", "EditControl", "DocumentControl"}:
                queue.extend((child, depth + 1) for child in control.GetChildren())
        except RapportError:
            raise
        except Exception:
            continue


def visible(control):
    rect = control.BoundingRectangle
    return not control.IsOffscreen and rect.width() > 0 and rect.height() > 0


def one(controls, message):
    if len(controls) != 1:
        raise RapportError("wechat_controls_unavailable", message,
                           "请确认微信已登录且没有弹窗遮挡；程序会自动定位，无需设置区域。", 409)
    return controls[0]


@dataclass
class ChatControls:
    title: object
    messages: object
    editor: object


def verify_accessible_contact(controls, contact):
    """Prefer two exact UIA recipient names over fallible OCR of uncommon glyphs.

    A missing title name requests OCR fallback. A conflicting authoritative name
    always fails; no fuzzy matching, substring matching or character substitutions.
    """
    expected = normalized_title(contact)
    title_name = (getattr(controls.title, "Name", "") or ""
                  if getattr(controls.title, "AutomationId", "").endswith(TITLE_LABEL_SUFFIX) else "")
    if not editor_matches_contact(controls.editor.Name or "", contact, title_name):
        raise RapportError("wechat_contact_mismatch", "当前聊天不是指定好友，本次未读取。",
                           "请使用完整好友备注；心迹会自动搜索并核对会话。", 409)
    if not getattr(controls.title, "AutomationId", "").endswith(TITLE_LABEL_SUFFIX):
        return False
    title = normalized_title(title_name)
    if not title:
        return False
    if title != expected:
        raise RapportError("wechat_contact_mismatch", "聊天标题与指定好友不一致，本次已停止。",
                           "请等待微信完成会话切换后重试。", 409)
    return True


def locate_chat(root, contact):
    titles, title_containers, messages, editors = [], [], [], []
    for control in walk_containers(root):
        if not visible(control):
            continue
        aid = control.AutomationId
        if aid == SEARCH_LIST_ID:
            raise RapportError("wechat_search_open", "微信搜索面板尚未关闭，暂不读取聊天内容。", status=409)
        if aid == MESSAGE_ID and control.ControlTypeName == "ListControl":
            messages.append(control)
        elif aid == INPUT_ID and control.ControlTypeName == "EditControl":
            editors.append(control)
        elif aid.endswith(TITLE_LABEL_SUFFIX):
            titles.append(control)
        elif aid.endswith(TITLE_CONTAINER_SUFFIX):
            title_containers.append(control)
    editor = one(editors, "未能自动定位微信聊天输入框。")
    # The editor's Name identifies its recipient, not its text Value. The DSH
    # compatibility helper checks any trailing hint together with the title.
    controls = ChatControls(one(titles or title_containers, "未能自动定位聊天标题。"),
                            one(messages, "未能自动定位消息列表。"), editor)
    verify_accessible_contact(controls, contact)
    return controls


def exact_search_results(items, contact):
    """Adapted from pyweixin get_search_result; never choose first ambiguous hit.

    Only contact/recent-use sections are allowed. Group/history/web results are
    excluded, and full names are compared with whitespace normalization.
    """
    section = None
    matches = []
    for item in items:
        name = item.Name or ""
        if name in SECTION_LABELS:
            section = name
            continue
        if (section in CONTACT_SECTIONS and item.ClassName == CONTACT_CLASS
                and normalized_title(name) == normalized_title(contact) and visible(item)):
            matches.append(item)
    return matches


def assert_focus(auto, control, root):
    focused = auto.GetFocusedControl()
    if not focused or not auto.ControlsAreSame(control, focused):
        raise RapportError("wechat_focus_changed", "微信搜索焦点发生变化，本次已停止。", status=409)
    top = focused.GetTopLevelControl()
    if top.NativeWindowHandle != root.NativeWindowHandle or top.ProcessId != root.ProcessId:
        raise RapportError("wechat_focus_changed", "微信窗口发生变化，本次已停止。", status=409)


def restore(root, auto):
    pattern = root.GetWindowPattern()
    if pattern and pattern.WindowVisualState == 2:  # UIA Minimized
        pattern.SetWindowVisualState(0)
    # Qt can report SetActive success while Windows still keeps another app in
    # front. Confirm the foreground handle before treating activation as done.
    if root.SetActive():
        foreground = auto.GetForegroundControl()
        if foreground and foreground.NativeWindowHandle == root.NativeWindowHandle:
            return
    # pyweixin uses a topmost activation step. Scope it to this operation and
    # preserve the original state; no permanent pinning, movement or resizing.
    was_topmost = root.IsTopmost()
    try:
        root.SetTopmost(True, waitTime=.1)
        root.SetFocus()
        root.SetActive(waitTime=.1)
        foreground = auto.GetForegroundControl()
        if not foreground or foreground.NativeWindowHandle != root.NativeWindowHandle:
            bounds = root.BoundingRectangle
            x, y = round(bounds.width() / 2), 12
            hit = auto.ControlFromPoint(bounds.left + x, bounds.top + y)
            top = hit.GetTopLevelControl() if hit else None
            if not top or top.NativeWindowHandle != root.NativeWindowHandle:
                raise RapportError("wechat_window_obscured", "微信窗口被遮挡，未能自动激活。", status=409)
            root.Click(x=x, y=y, simulateMove=False, waitTime=.15)
        foreground = auto.GetForegroundControl()
        if not foreground or foreground.NativeWindowHandle != root.NativeWindowHandle:
            raise RapportError("wechat_window_obscured", "微信未能切到前台，请关闭遮挡窗口后重试。", status=409)
    finally:
        root.SetTopmost(was_topmost, waitTime=.1)


def search_contact(auto, root, contact):
    restore(root, auto)
    candidates = [c for c in walk_containers(root)
                  if c.ControlTypeName == "EditControl" and c.ClassName == SEARCH_CLASS
                  and c.Name in SEARCH_NAMES and visible(c)]
    search = one(candidates, "未能自动定位微信搜索框。")
    search.Click(simulateMove=False, waitTime=.15)
    search.SetFocus()
    assert_focus(auto, search, root)
    pattern = search.GetValuePattern()
    if pattern and not pattern.IsReadOnly:
        if pattern.SetValue(contact) is False:
            raise RapportError("wechat_search_failed", "未能填入好友搜索名称，请重试。", status=409)
    else:
        auto.SetClipboardText(contact)
        assert_focus(auto, search, root)
        search.SendKeys("{Ctrl}a{Ctrl}v", waitTime=.15)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        # Search results live outside the chat-message tree; no message text read.
        lists = [c for c in walk_containers(root) if c.AutomationId == SEARCH_LIST_ID and visible(c)]
        if len(lists) == 1:
            matches = exact_search_results(lists[0].GetChildren(), contact)
            if len(matches) > 1:
                raise RapportError("wechat_contact_ambiguous", "发现多个同名好友，本次没有打开任何结果。",
                                   "请在微信为该好友设置唯一备注后重试。", 409)
            if len(matches) == 1:
                # Re-check search ownership immediately before selecting a result.
                assert_focus(auto, search, root)
                matches[0].Click(simulateMove=False, waitTime=.2)
                break
        time.sleep(.15)
    else:
        raise RapportError("wechat_contact_not_found", "没有找到名称完全一致的好友。",
                           "请核对好友完整备注；不会打开聊天记录或网络搜索结果。", 404)
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        try:
            return locate_chat(root, contact)
        except RapportError:
            time.sleep(.15)
    raise RapportError("wechat_chat_not_ready", "好友聊天尚未打开，请重试。", status=409)


def choose_window(windows, contact):
    if not windows:
        raise RapportError("wechat_not_running", "没有找到正在运行的微信。", "请先打开微信并登录后重试。", 409)
    main = [w for w in windows if w.ClassName == MAIN_CLASS]
    if len(main) == 1:
        return main[0]
    if len(main) > 1:
        raise RapportError("wechat_multiple_accounts", "检测到多个微信主窗口。",
                           "请只保留要读取的微信账号窗口后重试。", 409)
    if any(LOGIN_CLASS_HINT in (w.ClassName or "") for w in windows):
        raise RapportError("wechat_login_required", "微信还没有登录。", "请先在手机上确认登录微信，再重新读取。", 409)
    matches = []
    for root in windows:
        if "Chat" not in root.ClassName:
            continue
        try:
            locate_chat(root, contact)
            matches.append(root)
        except RapportError:
            pass
    return one(matches, "未找到已登录的微信窗口，请先打开微信并登录。")


def open_recent_session(root, contact):
    """Select a uniquely named visible recent chat, including system contacts."""
    lists = [control for control in walk_containers(root)
             if control.ControlTypeName == "ListControl" and control.AutomationId == SESSION_LIST_ID]
    if len(lists) != 1:
        return None
    matches = [row for row in lists[0].GetChildren()
               if row.ClassName == "mmui::ChatSessionCell" and visible(row)
               and normalized_title((row.Name or "").splitlines()[0]) == normalized_title(contact)]
    if len(matches) > 1:
        raise RapportError("wechat_contact_ambiguous", "最近会话中有多个同名好友，未自动选择。", status=409)
    if not matches:
        return None
    matches[0].Click(simulateMove=False, waitTime=.2)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            return locate_chat(root, contact)
        except RapportError as error:
            if error.code not in {"wechat_contact_mismatch", "wechat_search_open", "wechat_controls_unavailable"}:
                raise
            time.sleep(.1)
    raise RapportError("wechat_contact_mismatch", "最近会话没有切换到指定好友，本次未读取。", status=409)


def wait_for_controls(root, timeout: float | None = None):
    """Qt builds its accessibility tree lazily: a just-restored window may expose only empty containers."""
    deadline = time.monotonic() + (TREE_WAIT_S if timeout is None else timeout)
    while not any(getattr(control, "AutomationId", "") in READY_IDS for control in walk_containers(root)):
        if time.monotonic() >= deadline:
            raise RapportError("wechat_controls_unavailable", "微信界面的无障碍信息一直为空，未能定位聊天控件。",
                               "请把微信切到前台并点一下聊天列表，稍等几秒后重试。", 409)
        time.sleep(.5)


def open_contact(auto, windows, contact):
    root = choose_window(windows, contact)
    restore(root, auto)
    wait_for_controls(root)
    try:
        controls = locate_chat(root, contact)
    except RapportError:
        if root.ClassName != MAIN_CLASS:
            raise
        controls = open_recent_session(root, contact) or search_contact(auto, root, contact)
    return root, controls
