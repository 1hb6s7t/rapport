"""Demo report: built from a bundled synthetic conversation via the rules pipeline + FakeProvider.

Always marked synthetic; never returned for user uploads.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from rapport.jobs.pipeline import ByokContext, run_byok
from rapport.llm.extract import build_payload
from rapport.llm.fake import FakeProvider
from rapport.parsers import ParseOptions, parse
from rapport.schemas.message_v1 import Source
from rapport.schemas.profile import ChineseZodiac, Person, Profile, Zodiac
from rapport.schemas.report_v1 import Mode, Report

DEMO_TXT = Path(__file__).parent / "demo_chat.txt"


@lru_cache(maxsize=1)
def demo_report() -> Report:
    conv, _ = parse(DEMO_TXT.read_text(encoding="utf-8"), "demo", "paste", None, ParseOptions())
    conv.source = Source.demo
    events = conv.events()
    profile = Profile(
        me=Person(alias="A", mbti="INFP", zodiac=Zodiac.libra, chinese_zodiac=ChineseZodiac.dragon),
        other=Person(alias="B", mbti="ENTJ", zodiac=Zodiac.aries, chinese_zodiac=ChineseZodiac.monkey),
        goal="invite",
    )
    ctx = ByokContext(provider=FakeProvider(), payload=build_payload(conv, events), versions_extra={"provider": "demo", "model": "synthetic"}, preset="ollama")
    rep = run_byok(conv, events, profile, ctx, report_id="demo")
    rep.synthetic = True
    rep.mode = Mode.demo
    rep.notes.insert(0, "这是合成演示数据生成的报告，用于展示报告结构；AI 部分由离线假模型生成，不代表真实分析质量。")
    return rep
