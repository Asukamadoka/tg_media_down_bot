"""Hourly traffic buckets in a SQLite file of their own.

Not the bot's main database: this one is written every minute and is safe to
delete (the meter starts again), so it stays out of the file that holds the
admin claim and the logins.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HOUR_RETENTION_DAYS = 400
HOST_RETENTION_DAYS = 90
HOST_MIN_BYTES = 1024 * 1024
"""A host is kept for the day only when it moved more than this."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS traffic_hour (
    hour_utc  INTEGER NOT NULL,
    category  TEXT NOT NULL,
    outbound  TEXT NOT NULL,
    node      TEXT NOT NULL,
    up_bytes  INTEGER NOT NULL DEFAULT 0,
    down_bytes INTEGER NOT NULL DEFAULT 0,
    cost_cny  REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (hour_utc, category, outbound, node)
);
CREATE TABLE IF NOT EXISTS traffic_host_day (
    day_local TEXT NOT NULL,
    host      TEXT NOT NULL,
    category  TEXT NOT NULL,
    outbound  TEXT NOT NULL,
    node      TEXT NOT NULL DEFAULT '',
    bytes     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day_local, host, category, outbound, node)
);
"""

HourKey = tuple[int, str, str, str]
"""``(hour_utc, category, outbound, node)``."""
HostKey = tuple[str, str, str, str, str]
"""``(day_local, host, category, outbound, node)``."""


def hour_of(moment: datetime) -> int:
    """Epoch seconds at the start of ``moment``'s UTC hour."""
    stamp = int(moment.timestamp())
    return stamp - stamp % 3600


@dataclass
class Row:
    category: str
    outbound: str
    node: str
    up: int
    down: int
    cost: float

    @property
    def bytes(self) -> int:
        return self.up + self.down


class Period:
    """A span of local days: ``[start, end)`` as UTC instants."""

    def __init__(self, name: str, start: datetime, end: datetime, tz: ZoneInfo) -> None:
        self.name = name
        self.start = start
        self.end = end
        self.tz = tz

    @property
    def days(self) -> tuple[str, str]:
        """First and last local day, as ISO dates (the last one inclusive)."""
        first = self.start.astimezone(self.tz).date()
        last = (self.end - timedelta(seconds=1)).astimezone(self.tz).date()
        return first.isoformat(), last.isoformat()


def local_day(moment: datetime, tz: ZoneInfo) -> date:
    return moment.astimezone(tz).date()


def _midnight(day: date, tz: ZoneInfo) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=tz).astimezone(UTC)


def period_for(name: str, now: datetime, tz: ZoneInfo) -> Period:
    """``today``, ``yesterday``, ``7d`` (this day and the six before) or ``month``."""
    today = local_day(now, tz)
    if name == "today":
        return Period(name, _midnight(today, tz), _midnight(today + timedelta(days=1), tz), tz)
    if name == "yesterday":
        day = today - timedelta(days=1)
        return Period(name, _midnight(day, tz), _midnight(today, tz), tz)
    if name == "7d":
        return Period(name, _midnight(today - timedelta(days=6), tz),
                      _midnight(today + timedelta(days=1), tz), tz)
    if name == "month":
        first = today.replace(day=1)
        following = (first + timedelta(days=32)).replace(day=1)
        return Period(name, _midnight(first, tz), _midnight(following, tz), tz)
    raise ValueError(f"unknown period {name!r}")


class TrafficStore:
    """Synchronous; callers that live on the event loop use ``asyncio.to_thread``."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    def open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(SCHEMA)
        conn.commit()
        self._conn = conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    @property
    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("TrafficStore.open() has not been called")
        return self._conn

    # ------------------------------------------------------------------ write

    def add_hours(self, rows: Iterable[tuple[HourKey, int, int, float]]) -> None:
        """Add ``(key, up, down, cost)`` to the buckets, creating them as needed."""
        with self._lock:
            self._db.executemany(
                """
                INSERT INTO traffic_hour (hour_utc, category, outbound, node,
                                          up_bytes, down_bytes, cost_cny)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(hour_utc, category, outbound, node) DO UPDATE SET
                    up_bytes = up_bytes + excluded.up_bytes,
                    down_bytes = down_bytes + excluded.down_bytes,
                    cost_cny = cost_cny + excluded.cost_cny
                """,
                [(*key, up, down, cost) for key, up, down, cost in rows],
            )
            self._db.commit()

    def add_hosts(self, rows: Iterable[tuple[HostKey, int]]) -> None:
        with self._lock:
            self._db.executemany(
                """
                INSERT INTO traffic_host_day (day_local, host, category, outbound, node, bytes)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(day_local, host, category, outbound, node) DO UPDATE SET
                    bytes = bytes + excluded.bytes
                """,
                [(*key, amount) for key, amount in rows],
            )
            self._db.commit()

    def prune(self, now: datetime, tz: ZoneInfo) -> None:
        hours = int((now - timedelta(days=HOUR_RETENTION_DAYS)).timestamp())
        days = (local_day(now, tz) - timedelta(days=HOST_RETENTION_DAYS)).isoformat()
        with self._lock:
            self._db.execute("DELETE FROM traffic_hour WHERE hour_utc < ?", (hours,))
            self._db.execute("DELETE FROM traffic_host_day WHERE day_local < ?", (days,))
            self._db.commit()

    # ------------------------------------------------------------------- read

    def rows(self, period: Period) -> list[Row]:
        with self._lock:
            found = self._db.execute(
                """
                SELECT category, outbound, node, SUM(up_bytes), SUM(down_bytes), SUM(cost_cny)
                FROM traffic_hour WHERE hour_utc >= ? AND hour_utc < ?
                GROUP BY category, outbound, node
                """,
                (int(period.start.timestamp()), int(period.end.timestamp())),
            ).fetchall()
        return [Row(*row) for row in found]

    def top_hosts(self, period: Period, *, outbound: str = "proxy", limit: int = 5):
        first, last = period.days
        with self._lock:
            found = self._db.execute(
                """
                SELECT host, category, node, SUM(bytes) AS total FROM traffic_host_day
                WHERE day_local >= ? AND day_local <= ? AND outbound = ?
                GROUP BY host, category, node ORDER BY total DESC LIMIT ?
                """,
                (first, last, outbound, limit),
            ).fetchall()
        return [(host, category, node, total) for host, category, node, total in found]

    def heavy_hosts(self, period: Period, *, category: str, outbound: str, minimum: int):
        first, last = period.days
        with self._lock:
            return self._db.execute(
                """
                SELECT host, SUM(bytes) AS total FROM traffic_host_day
                WHERE day_local >= ? AND day_local <= ? AND category = ? AND outbound = ?
                GROUP BY host HAVING total > ? ORDER BY total DESC
                """,
                (first, last, category, outbound, minimum),
            ).fetchall()
