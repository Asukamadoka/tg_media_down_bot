"""One small request table: ask a running process to do something (docs/wms/M9.3 §A.5).

A command line (``wms task pause 66:3``, ``python -m tgmd.traffic ask-probe``) cannot
reach into the bot that is running, so it leaves a row; the bot looks at the table
every few seconds and marks what it took. Both users share these functions, and
each owns its sqlite connection and its lock: every function here takes an open
connection and commits.
"""

from __future__ import annotations

import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS request (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,
    target       TEXT NOT NULL DEFAULT '',
    arg          TEXT NOT NULL DEFAULT '',
    requested_at REAL NOT NULL,
    handled_at   REAL,
    result       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_request_open ON request (kind) WHERE handled_at IS NULL;
"""

EXPIRES = 600.0
"""Seconds an unanswered request waits before it is marked ``expired``: a request for a
plan nothing is running must not fire when someone starts it next week."""


def post(conn: sqlite3.Connection, kind: str, target: str = "", arg: str = "",
         now: float | None = None) -> int:
    cursor = conn.execute(
        "INSERT INTO request (kind, target, arg, requested_at) VALUES (?, ?, ?, ?)",
        (kind, target, arg, time.time() if now is None else now))
    conn.commit()
    return int(cursor.lastrowid or 0)


def take(conn: sqlite3.Connection, kind: str, now: float | None = None, *,
         targets: set[str] | None = None, result: str | None = "ok") -> list[sqlite3.Row | tuple]:
    """The open requests of ``kind`` (those for one of ``targets``, when given), marked
    handled with ``result``, or left open for :func:`finish` when it is None. Old ones
    are marked ``expired`` and not returned."""
    now = time.time() if now is None else now
    conn.execute(
        "UPDATE request SET handled_at = ?, result = 'expired' "
        "WHERE kind = ? AND handled_at IS NULL AND requested_at < ?", (now, kind, now - EXPIRES))
    rows = conn.execute(
        "SELECT id, kind, target, arg, requested_at FROM request "
        "WHERE kind = ? AND handled_at IS NULL ORDER BY id", (kind,)).fetchall()
    if targets is not None:
        rows = [row for row in rows if row[2] in targets]
    if result is not None:
        for row in rows:
            conn.execute("UPDATE request SET handled_at = ?, result = ? WHERE id = ?",
                         (now, result, row[0]))
    conn.commit()
    return rows


def finish(conn: sqlite3.Connection, request_id: int, result: str,
           now: float | None = None) -> None:
    conn.execute("UPDATE request SET handled_at = ?, result = ? WHERE id = ?",
                 (time.time() if now is None else now, result, request_id))
    conn.commit()


def outcome(conn: sqlite3.Connection, request_id: int) -> tuple[bool, str]:
    """(handled yet?, the result text) of one request."""
    row = conn.execute("SELECT handled_at, result FROM request WHERE id = ?",
                       (request_id,)).fetchone()
    return (row is not None and row[0] is not None), (row[1] if row else "")
