"""Shared scheduling parsing and Discord channel-topic helpers."""

import re
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

TWO_WEEKS = timedelta(days=14)
DATE_RE = re.compile(r"^\s*(\d{1,2})/(\d{1,2})\s*$")
TIME_RE = re.compile(
    r"^\s*(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*([ap]m)\s*$",
    re.IGNORECASE,
)
SERIES_TOPIC_RE = re.compile(
    r"(?:^|\n)QRLS series: "
    r"([0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})"
    r"(?:\n|$)",
    re.IGNORECASE,
)
WEEK_CHANNEL_RE = re.compile(r"^week(\d+)-", re.IGNORECASE)


def timezone_for(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Website returned an invalid schedule timezone: {name!r}") from exc


def parse_schedule_datetime(
    date_str: str,
    time_str: str,
    timezone_name: str,
    *,
    now: Optional[datetime] = None,
) -> tuple[Optional[datetime], Optional[str]]:
    dm = DATE_RE.match(date_str or "")
    if not dm:
        return None, "❌ Invalid date format. Use **M/D** (examples: `1/12`, `12/3`)."

    tm = TIME_RE.match(time_str or "")
    if not tm:
        return None, (
            "❌ Invalid time format. Use **H[:MM]am/pm** in **ET** "
            "(examples: `8pm`, `8:00pm`, `11:15am`)."
        )

    timezone = timezone_for(timezone_name)
    now_local = (now or datetime.now(timezone)).astimezone(timezone)
    month, day = int(dm.group(1)), int(dm.group(2))
    hour = int(tm.group(1)) % 12 + (12 if tm.group(3).lower() == "pm" else 0)
    minute = int(tm.group(2) or "0")

    try:
        scheduled = datetime(now_local.year, month, day, hour, minute, tzinfo=timezone)
        if scheduled <= now_local:
            next_year = scheduled.replace(year=scheduled.year + 1)
            if next_year <= now_local + TWO_WEEKS:
                scheduled = next_year
            else:
                return None, "❌ That proposed time is in the past (ET). Please choose a future time."
    except ValueError:
        return None, "❌ That date/time isn’t a valid calendar date."

    if scheduled > now_local + TWO_WEEKS:
        return None, "❌ That time is more than **2 weeks** from now. Please choose a time within the next **14 days**."
    return scheduled, None


def format_schedule_datetime(value: datetime) -> str:
    hour = value.hour
    hour12 = hour % 12 or 12
    ampm = "AM" if hour < 12 else "PM"
    return f"{value.month}/{value.day} {hour12}:{value.minute:02d}{ampm} ET"


def series_id_from_topic(topic: Optional[str]) -> Optional[str]:
    match = SERIES_TOPIC_RE.search(topic or "")
    return match.group(1).lower() if match else None


def topic_with_series_id(topic: Optional[str], series_id: str) -> str:
    marker = f"QRLS series: {series_id}"
    lines = [line for line in (topic or "").splitlines() if not line.lower().startswith("qrls series:")]
    lines.append(marker)
    return "\n".join(lines)


def week_from_channel_name(name: str) -> Optional[int]:
    match = WEEK_CHANNEL_RE.match(name)
    return int(match.group(1)) if match else None
