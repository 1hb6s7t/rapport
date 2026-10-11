"""User-provided profile labels. Core analysis never reads MBTI / zodiac / chinese zodiac."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MBTI_TYPES = {a + b + c + d for a in "EI" for b in "SN" for c in "TF" for d in "JP"}


class Zodiac(StrEnum):
    aries = "aries"
    taurus = "taurus"
    gemini = "gemini"
    cancer = "cancer"
    leo = "leo"
    virgo = "virgo"
    libra = "libra"
    scorpio = "scorpio"
    sagittarius = "sagittarius"
    capricorn = "capricorn"
    aquarius = "aquarius"
    pisces = "pisces"


class ChineseZodiac(StrEnum):
    rat = "rat"
    ox = "ox"
    tiger = "tiger"
    rabbit = "rabbit"
    dragon = "dragon"
    snake = "snake"
    horse = "horse"
    goat = "goat"
    monkey = "monkey"
    rooster = "rooster"
    dog = "dog"
    pig = "pig"


class Goal(StrEnum):
    understand = "understand"
    invite = "invite"
    clarify = "clarify"
    repair = "repair"
    decide = "decide"


class Birthday(BaseModel):
    """User supplied date, without inventing missing parts or calendar conversions."""

    model_config = ConfigDict(extra="forbid")
    year: int | None = Field(default=None, ge=1, le=9999)
    month: int | None = Field(default=None, ge=1, le=12)
    day: int | None = Field(default=None, ge=1, le=31)
    calendar: Literal["gregorian", "lunar"] = "gregorian"
    precision: Literal["unknown", "year", "year_month", "month_day", "day"] = "unknown"
    is_leap_month: bool | None = None

    @model_validator(mode="after")
    def valid_date(self):
        present = (self.year is not None, self.month is not None, self.day is not None)
        precisions = {
            (False, False, False): "unknown", (True, False, False): "year",
            (True, True, False): "year_month", (False, True, True): "month_day",
            (True, True, True): "day",
        }
        expected = precisions.get(present)
        if expected is None:
            raise ValueError("生日请填写年份、年月、月日或完整日期；不知道时留空")
        if "precision" in self.model_fields_set and self.precision != expected:
            raise ValueError("生日精度与已填写的日期不一致")
        self.precision = expected
        if self.year is not None and self.year > date.today().year:
            raise ValueError("出生年份不能晚于今年")
        if self.calendar == "gregorian":
            if self.is_leap_month:
                raise ValueError("公历生日没有闰月标记")
            if self.month is not None and self.day is not None:
                actual = date(self.year or 2000, self.month, self.day)
                if self.year and actual > date.today():
                    raise ValueError("出生日期不能是未来日期")
        elif self.day is not None and self.day > 30:
            raise ValueError("农历日期不能超过三十；历法转换尚未核验")
        return self


class Person(BaseModel):
    alias: str = Field(default="", max_length=120)
    mbti: str | None = None
    zodiac: Zodiac | None = None
    chinese_zodiac: ChineseZodiac | None = None
    birthday: Birthday | None = None
    source: Literal["user", "imported", "unknown"] = "unknown"
    confirmed: bool = False
    revision: int = Field(default=0, ge=0)

    @field_validator("mbti", mode="before")
    @classmethod
    def _mbti(cls, v):
        if v is None or v == "":
            return None
        v = str(v).strip().upper()
        if v not in MBTI_TYPES:
            raise ValueError(f"invalid MBTI type: {v}")
        return v


class Profile(BaseModel):
    me: Person = Field(default_factory=Person)
    other: Person = Field(default_factory=Person)
    goal: Goal = Goal.understand
    goal_text: str = Field(default="", max_length=500)
    stage_confirmed: str | None = None  # user asserts stage, e.g. "mutually_confirmed"

    def core_hash(self) -> str:
        """Hash of the parts that influence core analysis (labels excluded)."""
        core = {"goal": self.goal, "goal_text": self.goal_text, "stage_confirmed": self.stage_confirmed}
        return hashlib.sha256(json.dumps(core, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]

    def cache_hash(self) -> str:
        """Full report snapshot identity; labels still never change objective scoring."""
        data = self.model_dump(mode="json")
        return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:32]
