from types import SimpleNamespace

import pytest

from rapport.desktop import pyweixin_navigation as nav
from rapport.desktop import wechat_native as native
from rapport.desktop.dsh_input_name import PLACEHOLDER_HINTS
from rapport.errors import RapportError


class Box:
    def __init__(self, left=100, top=200, right=900, bottom=800):
        self.left, self.top, self.right, self.bottom = left, top, right, bottom

    def width(self):
        return self.right - self.left

    def height(self):
        return self.bottom - self.top


class Node:
    def __init__(self, kind="GroupControl", cls="", aid="", name="", children=None):
        self.ControlTypeName, self.ClassName, self.AutomationId, self.Name = kind, cls, aid, name
        self.children = children or []
        self.IsOffscreen = False
        self.BoundingRectangle = Box()

    def GetChildren(self):
        return self.children


def tree(contact="指定好友"):
    title = Node(kind="TextControl", aid="title" + nav.TITLE_LABEL_SUFFIX)
    messages = Node(kind="ListControl", aid=nav.MESSAGE_ID)
    editor = Node(kind="EditControl", aid=nav.INPUT_ID, name=contact)
    root = Node(cls=nav.MAIN_CLASS, children=[title, messages, editor])
    return root, messages, editor


def test_locator_does_not_read_message_items_or_editor_value():
    root, messages, editor = tree()
    messages.GetChildren = lambda: pytest.fail("must not traverse messages")
    editor.GetChildren = lambda: pytest.fail("must not traverse editor")
    editor.GetValuePattern = lambda: pytest.fail("must not read input text for navigation")
    controls = nav.locate_chat(root, "指定 好友")
    assert controls.messages is messages and controls.editor is editor


def test_locator_prefers_name_label_over_toolbar_icons():
    root, _, _ = tree()
    name_label = root.children[0]
    root.children.extend([Node(cls=nav.TITLE_CLASS), Node(aid="content" + nav.TITLE_CONTAINER_SUFFIX)])
    assert nav.locate_chat(root, "指定好友").title is name_label
    root.children.remove(name_label)
    assert nav.locate_chat(root, "指定好友").title.AutomationId.endswith(nav.TITLE_CONTAINER_SUFFIX)


@pytest.mark.parametrize("title_name,editor_name", [("劳变淇", "劳娈淇"), ("劳娈淇", "劳变淇"), ("劳娈淇2", "劳娈淇"), ("劳娈淇", "")])
def test_accessible_contact_rejects_conflicting_or_partial_names(title_name, editor_name):
    controls = nav.ChatControls(Node(aid="title" + nav.TITLE_LABEL_SUFFIX, name=title_name), Node(), Node(name=editor_name))
    with pytest.raises(RapportError):
        nav.verify_accessible_contact(controls, "劳娈淇")


def test_missing_accessible_title_requires_ocr_fallback():
    controls = nav.ChatControls(Node(aid="title" + nav.TITLE_LABEL_SUFFIX), Node(), Node(name="劳娈淇"))
    assert nav.verify_accessible_contact(controls, "劳娈淇") is False
    controls.title.Name = "劳 娈淇"
    assert nav.verify_accessible_contact(controls, "劳娈淇") is True


@pytest.mark.parametrize("hint", PLACEHOLDER_HINTS)
def test_dsh_input_hint_works_only_with_complete_independent_title(hint):
    root, _, editor = tree("。。。\n" + hint)
    title = root.children[0]
    title.Name = "。。。"
    assert nav.locate_chat(root, "。。。").editor is editor
    for invalid_title in ("", "。。", "。。。2", "。。。 " + hint):
        title.Name = invalid_title
        with pytest.raises(RapportError):
            nav.locate_chat(root, "。。。")
    title.Name = "。。。"
    title.AutomationId = "content" + nav.TITLE_CONTAINER_SUFFIX
    with pytest.raises(RapportError):
        nav.locate_chat(root, "。。。")


@pytest.mark.parametrize("contact", ["输入消息", "小明语音输入测试", "小明 按住说话", "。。。"])
def test_contact_containing_hint_is_not_shortened(contact):
    root, _, editor = tree(contact)
    root.children[0].Name = contact
    assert nav.locate_chat(root, contact).editor is editor
    editor.Name = contact + " 输入消息"
    assert nav.locate_chat(root, contact).editor is editor


@pytest.mark.parametrize("label", [
    "小明2 输入消息", "小明输入消息", "小明 输入消息 请继续", "小明 输入消息 输入消息",
    "小明 语音输入其他", "输入消息", "小明 按住 Ctrl + Win", "小明 未知提示",
])
def test_dsh_compatibility_does_not_accept_partial_or_unknown_labels(label):
    root, _, _ = tree(label)
    root.children[0].Name = "小明"
    with pytest.raises(RapportError):
        nav.locate_chat(root, "小明")


def test_locator_rejects_wrong_contact_duplicate_controls_and_search_overlay():
    root, _, editor = tree()
    with pytest.raises(RapportError, match="当前聊天不是"):
        nav.locate_chat(root, "另外好友")
    root.children.append(editor)
    with pytest.raises(RapportError):
        nav.locate_chat(root, "指定好友")
    root.children.pop()
    root.children.append(Node(kind="ListControl", aid=nav.SEARCH_LIST_ID))
    with pytest.raises(RapportError, match="搜索面板"):
        nav.locate_chat(root, "指定好友")


def result(name, cls=nav.CONTACT_CLASS):
    return Node(kind="ListItemControl", cls=cls, name=name)


def test_search_uses_unique_full_contact_only_not_history_group_or_network():
    target = result("指定 好友")
    items = [result("联系人", "mmui::XTableCell"), result("指定好友2"), target,
             result("群聊", "mmui::XTableCell"), result("指定好友"),
             result("聊天记录", "mmui::XTableCell"), result("指定好友"),
             result("搜索网络结果", "mmui::XTableCell"), result("指定好友")]
    assert nav.exact_search_results(items, "指定好友") == [target]
    assert nav.exact_search_results(items, "指定") == []


def test_search_returns_duplicates_for_caller_to_reject():
    items = [result("Contacts"), result("同名好友"), result("同名好友")]
    assert len(nav.exact_search_results(items, "同名好友")) == 2


def test_pixel_bounds_follow_move_resize_and_dpi_without_percentages():
    image = SimpleNamespace(width=1600, height=1200)
    assert native.pixel_rect(Box(350, 240, 880, 530), Box(), image) == (500, 80, 1560, 660)
    with pytest.raises(RapportError):
        native.pixel_rect(Box(-100, 240, 880, 530), Box(), image)


def test_multiple_accounts_are_not_selected_arbitrarily():
    with pytest.raises(RapportError) as error:
        nav.choose_window([Node(cls=nav.MAIN_CLASS), Node(cls=nav.MAIN_CLASS)], "指定好友")
    assert error.value.code == "wechat_multiple_accounts"


def test_open_contact_uses_search_when_current_chat_differs(monkeypatch):
    root, _, _ = tree("其他好友")
    monkeypatch.setattr(nav, "restore", lambda *args: None)
    expected = object()
    calls = []

    def search(auto, window, contact):
        calls.append((window, contact))
        return expected

    monkeypatch.setattr(nav, "search_contact", search)
    assert nav.open_contact(object(), [root], "指定好友") == (root, expected)
    assert calls == [(root, "指定好友")]


def test_exact_recent_session_opens_system_contact_without_global_search(monkeypatch):
    root, _, editor = tree("其他好友")
    target = Node(kind="ListItemControl", cls="mmui::ChatSessionCell", name="文件传输助手\n最近一条预览")
    target.Click = lambda **_: setattr(editor, "Name", "文件传输助手")
    root.children.append(Node(kind="ListControl", aid=nav.SESSION_LIST_ID, children=[target]))
    monkeypatch.setattr(nav, "restore", lambda *args: None)
    monkeypatch.setattr(nav, "search_contact", lambda *args: pytest.fail("recent exact row should win"))
    _, controls = nav.open_contact(object(), [root], "文件传输助手")
    assert controls.editor is editor
    assert nav.open_recent_session(root, "文件传输") is None


def test_search_focus_guard_never_types_into_message_editor():
    root, _, editor = tree()
    auto = SimpleNamespace(GetFocusedControl=lambda: editor, ControlsAreSame=lambda a, b: a is b)
    with pytest.raises(RapportError):
        nav.assert_focus(auto, Node(kind="EditControl", cls=nav.SEARCH_CLASS), root)


@pytest.mark.parametrize("obscured", [False, True])
def test_activation_restores_original_topmost_state_even_on_failure(obscured):
    changes, clicks = [], []
    foreground = [SimpleNamespace(NativeWindowHandle=999)]

    def click(**_):
        clicks.append(True)
        foreground[0] = SimpleNamespace(NativeWindowHandle=100)

    root = SimpleNamespace(NativeWindowHandle=100, BoundingRectangle=Box(),
                           GetWindowPattern=lambda: None, SetActive=lambda **_: False,
                           IsTopmost=lambda: False, SetFocus=lambda: True,
                           SetTopmost=lambda value, **_: changes.append(value),
                           Click=click)
    top = SimpleNamespace(NativeWindowHandle=999 if obscured else 100)
    auto = SimpleNamespace(ControlFromPoint=lambda *args: SimpleNamespace(GetTopLevelControl=lambda: top),
                           GetForegroundControl=lambda: foreground[0])
    if obscured:
        with pytest.raises(RapportError):
            nav.restore(root, auto)
        assert not clicks
    else:
        nav.restore(root, auto)
        assert clicks == [True]
    assert changes == [True, False]


def test_setactive_success_does_not_bypass_foreground_check():
    changes = []
    root = SimpleNamespace(NativeWindowHandle=100, BoundingRectangle=Box(),
                           GetWindowPattern=lambda: None, SetActive=lambda **_: True,
                           IsTopmost=lambda: False, SetFocus=lambda: True,
                           SetTopmost=lambda value, **_: changes.append(value))
    other = SimpleNamespace(NativeWindowHandle=999)
    auto = SimpleNamespace(GetForegroundControl=lambda: other,
                           ControlFromPoint=lambda *args: SimpleNamespace(GetTopLevelControl=lambda: other))
    with pytest.raises(RapportError, match="遮挡"):
        nav.restore(root, auto)
    assert changes == [True, False]


def test_setactive_success_but_wrong_foreground_uses_verified_title_click():
    changes, clicks = [], []
    foreground = [SimpleNamespace(NativeWindowHandle=999)]

    def click(**_):
        clicks.append(True)
        foreground[0] = SimpleNamespace(NativeWindowHandle=100)

    root = SimpleNamespace(NativeWindowHandle=100, BoundingRectangle=Box(),
                           GetWindowPattern=lambda: None, SetActive=lambda **_: True,
                           IsTopmost=lambda: False, SetFocus=lambda: True,
                           SetTopmost=lambda value, **_: changes.append(value), Click=click)
    auto = SimpleNamespace(GetForegroundControl=lambda: foreground[0],
                           ControlFromPoint=lambda *args: SimpleNamespace(GetTopLevelControl=lambda: root))
    nav.restore(root, auto)
    assert clicks == [True] and changes == [True, False]


def test_screen_capture_uses_actual_bounds_and_stops_on_occlusion(monkeypatch):
    from PIL import Image, ImageGrab

    root = SimpleNamespace(NativeWindowHandle=100, BoundingRectangle=Box())
    monkeypatch.setattr(native, "checked_window", lambda *args: root)
    grabs = []

    def grab(**kwargs):
        grabs.append(kwargs)
        return Image.new("RGB", (800, 600))

    monkeypatch.setattr(ImageGrab, "grab", grab)
    auto = SimpleNamespace(GetForegroundControl=lambda: root,
                           GetMonitorsRect=lambda: [Box(0, 0, 1920, 1080)],
                           GetRootControl=lambda: SimpleNamespace(GetChildren=lambda: []),
                           ControlFromPoint=lambda *args: SimpleNamespace(GetTopLevelControl=lambda: root))
    native.grab_window(auto, 100, 200, 20_000_000)
    assert grabs == [{"bbox": (100, 200, 900, 800), "all_screens": True}]
    auto.ControlFromPoint = lambda *args: SimpleNamespace(GetTopLevelControl=lambda: SimpleNamespace(NativeWindowHandle=999))
    with pytest.raises(RapportError):
        native.grab_window(auto, 100, 200, 20_000_000)
    assert len(grabs) == 1
