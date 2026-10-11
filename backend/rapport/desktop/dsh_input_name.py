"""WeChat 4.1.12+ input-name compatibility adapted from dsh-wechat-bot.

Source: huaqian695-sudo/dsh-wechat-bot, commit
42d672ea8aa49925637973fe11194f768bc92af6,
src/wechat_agent/infra/wechat/patch.py. Copyright (c) 2025, MIT;
see LICENSE-dsh-wechat-bot.txt.

Rapport changes: no monkeypatch or dependency import; only a complete trailing
hint with a whitespace boundary is accepted, and only with an independent,
complete title match. Never shorten an arbitrary contact name at a substring.
"""

from __future__ import annotations

from rapport.desktop.wechat import normalized_title

PLACEHOLDER_HINTS = (
    "按住 Ctrl + Win 使用语音输入文字",
    "按住 Ctrl 使用语音输入",
    "按住说话",
    "语音输入",
    "输入消息",
)


def editor_matches_contact(name: str, contact: str, title_name: str = "") -> bool:
    """Match an editor label without weakening recipient identity checks.

    An exact label takes precedence, including names containing a hint. Decorated
    labels require the separate UIA title label; OCR cannot authorize stripping.
    """
    expected = normalized_title(contact)
    if not expected:
        return False
    if normalized_title(name) == expected:
        return True
    if normalized_title(title_name) != expected:
        return False
    label = name.rstrip()
    for hint in PLACEHOLDER_HINTS:
        if not label.endswith(hint):
            continue
        prefix = label[:-len(hint)]
        if prefix and prefix[-1].isspace() and normalized_title(prefix) == expected:
            return True
    return False
