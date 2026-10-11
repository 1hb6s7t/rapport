from types import SimpleNamespace

import pytest

from rapport.desktop import wechat_native
from rapport.errors import RapportError


def root_with(children):
    return SimpleNamespace(GetRootControl=lambda: SimpleNamespace(GetChildren=lambda: children))


def test_empty_desktop_is_not_reported_as_wechat_logged_out():
    with pytest.raises(RapportError) as error:
        wechat_native.visible_wechat_windows(root_with([]))
    assert error.value.code == 'wechat_desktop_unavailable'


def test_accessible_desktop_still_filters_by_process_identity(monkeypatch):
    controls = [SimpleNamespace(ProcessId=1), SimpleNamespace(ProcessId=2)]
    monkeypatch.setattr(wechat_native, 'process_name', lambda pid: 'weixin.exe' if pid == 2 else 'browser.exe')
    assert wechat_native.visible_wechat_windows(root_with(controls)) == [controls[1]]
