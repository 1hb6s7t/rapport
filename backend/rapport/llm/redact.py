"""Redaction before anything leaves the machine. Regex based; users can edit the preview."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from rapport.datasets.credentials import ASSIGNMENT, REDACTED, TOKEN

PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    ("credential", TOKEN, REDACTED),
    ("credential_assignment", ASSIGNMENT, REDACTED),
    ("id_card", re.compile(r"(?<!\d)\d{6}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)"), "[身份证]"),
    ("bank_card", re.compile(r"(?<!\d)(?:\d[ -]?){16,19}(?!\d)"), "[银行卡]"),
    ("phone", re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)"), "[电话]"),
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[邮箱]"),
    ("url", re.compile(r"https?://[^\s一-鿿]+|www\.[^\s一-鿿]+"), "[链接]"),
    ("wechat_id", re.compile(r"(?:微信号|微信|vx|wx|weixin)[:：\s]*[A-Za-z][A-Za-z0-9_-]{5,19}", re.I), "[微信号]"),
    ("qq", re.compile(r"(?:qq|QQ)[号:：\s]*[1-9]\d{4,10}"), "[QQ号]"),
    ("address", re.compile(r"[一-鿿]{2,8}(?:省|市|区|县)[一-鿿]{1,12}(?:路|街|道|巷|村|小区|大厦|号楼|栋|单元)[一-鿿0-9A-Za-z\-号室]{0,12}"), "[地址]"),
    ("plate", re.compile(r"[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼][A-Z][A-Z0-9]{5,6}"), "[车牌]"),
]


@dataclass
class RedactStats:
    counts: dict[str, int] = field(default_factory=dict)
    names_replaced: int = 0

    def total(self) -> int:
        return sum(self.counts.values()) + self.names_replaced


def redact_text(text: str, stats: RedactStats | None = None) -> str:
    stats = stats if stats is not None else RedactStats()
    for name, pat, repl in PATTERNS:
        text, n = pat.subn(repl, text)
        if n:
            stats.counts[name] = stats.counts.get(name, 0) + n
    return text


def redact_names(text: str, names: list[tuple[str, str]], stats: RedactStats | None = None) -> str:
    """names: [(real_name, placeholder)], longest first."""
    stats = stats if stats is not None else RedactStats()
    for real, ph in sorted(names, key=lambda x: -len(x[0])):
        if len(real) < 2:
            continue
        if real in text:
            stats.names_replaced += text.count(real)
            text = text.replace(real, ph)
    return text
