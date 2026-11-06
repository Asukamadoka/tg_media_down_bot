"""Sizes, durations and points in time, as a rules file writes them.

``100MB``, ``1.5 GiB``, ``30d``, ``2w``, ``2026-09-01``. Sizes are binary
(``1GB`` is 1024³ bytes), which is what the drive and a NAS show.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, tzinfo

_SIZE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgt]?)i?b?\s*$", re.IGNORECASE)
_SIZE_POWERS = {"": 0, "k": 1, "m": 2, "g": 3, "t": 4}

_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdw])\s*$", re.IGNORECASE)
_DURATION_UNITS = {
    "s": timedelta(seconds=1),
    "m": timedelta(minutes=1),
    "h": timedelta(hours=1),
    "d": timedelta(days=1),
    "w": timedelta(weeks=1),
}


def parse_size(value: int | float | str) -> int:
    """Bytes from ``1024``, ``100MB``, ``1.5GiB`` or ``2 g``."""
    if isinstance(value, bool):
        raise ValueError(f"not a size: {value!r}")
    if isinstance(value, int | float):
        if value < 0:
            raise ValueError(f"a size cannot be negative: {value!r}")
        return int(value)
    match = _SIZE.match(str(value))
    if not match:
        raise ValueError(f"not a size: {value!r} (write it like 100MB or 1.5GB)")
    number, unit = match.groups()
    return int(float(number) * 1024 ** _SIZE_POWERS[unit.lower()])


def parse_duration(value: str | int) -> timedelta:
    """A span from ``30d``, ``12h``, ``2w``, ``90m``; a bare number is days."""
    if isinstance(value, bool):
        raise ValueError(f"not a duration: {value!r}")
    if isinstance(value, int | float):
        return timedelta(days=value)
    match = _DURATION.match(str(value))
    if not match:
        raise ValueError(f"not a duration: {value!r} (write it like 30d, 12h or 2w)")
    number, unit = match.groups()
    return float(number) * _DURATION_UNITS[unit.lower()]


def parse_moment(value: str | int | datetime, *, now: datetime, tz: tzinfo) -> datetime:
    """A point in time: a duration back from ``now`` (``30d``), or a date / datetime.

    A date or a datetime without an offset is read in ``tz``, the configured
    time zone, never the container's.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=tz)
    text = str(value).strip()
    try:
        return now - parse_duration(text)
    except ValueError:
        pass
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"not a duration or a date: {value!r} (write it like 30d or 2026-09-01)"
        ) from exc
    return moment if moment.tzinfo else moment.replace(tzinfo=tz)


def check_moment(value: str | int | datetime) -> str | int | datetime:
    """Validate a moment without knowing ``now`` yet (for loading rules)."""
    if isinstance(value, datetime | int) and not isinstance(value, bool):
        return value
    text = str(value).strip()
    if _DURATION.match(text):
        return text
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"not a duration or a date: {value!r} (write it like 30d or 2026-09-01)"
        ) from exc
    return text


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"  # pragma: no cover
