"""Scheduling zones work on Windows even before optional IANA data is installed."""

from datetime import UTC, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from rapport.errors import RapportError


def schedule_zone(name: str):
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        # Current China scheduling uses UTC+08:00; no DST since 1991. This
        # fallback is for current schedules, not historical calendar conversion.
        if name == "Asia/Shanghai":
            return timezone(timedelta(hours=8), "Asia/Shanghai")
        if name in {"UTC", "Etc/UTC"}:
            return UTC
        raise RapportError("learning_schedule_timezone", "时区不可用，请安装 tzdata 或选择 Asia/Shanghai / UTC。", status=422) from None
    except (ValueError, TypeError):
        raise RapportError("learning_schedule_timezone", "请选择有效的本地时区。", status=422) from None
