"""SQLite persistence: user preferences, upload cache, job history, secrets.

Everything is small and low-traffic, so the standard library's synchronous
``sqlite3`` is used and each call is pushed onto a worker thread. A single lock
serialises writes, which keeps the file consistent without WAL contention.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    mode        TEXT,
    pikpak_dir  TEXT,
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS media_cache (
    cache_key     TEXT PRIMARY KEY,
    cache_chat_id INTEGER NOT NULL,
    cache_msg_id  INTEGER NOT NULL,
    file_name     TEXT,
    file_size     INTEGER,
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    link        TEXT NOT NULL,
    mode        TEXT NOT NULL,
    status      TEXT NOT NULL,
    file_name   TEXT,
    file_size   INTEGER,
    error       TEXT,
    created_at  REAL NOT NULL,
    finished_at REAL
);

CREATE INDEX IF NOT EXISTS jobs_user_created
    ON jobs (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def cache_key(chat_id: int | str, message_id: int) -> str:
    """Stable key identifying one source message."""
    return f"{chat_id}:{message_id}"


class Database:
    """Thin async wrapper over a single SQLite file."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._conn: sqlite3.Connection | None = None

    async def connect(self) -> None:
        """Open the database and apply the schema."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await asyncio.to_thread(self._open)

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
        conn.commit()
        return conn

    async def close(self) -> None:
        if self._conn is not None:
            conn, self._conn = self._conn, None
            await asyncio.to_thread(conn.close)

    @property
    def connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Database.connect() has not been awaited")
        return self._conn

    async def _write(self, sql: str, params: Iterable[Any] = ()) -> int:
        """Run a statement that changes data, returning ``lastrowid``."""
        async with self._lock:
            return await asyncio.to_thread(self._write_sync, sql, tuple(params))

    def _write_sync(self, sql: str, params: tuple) -> int:
        cursor = self.connection.execute(sql, params)
        self.connection.commit()
        return int(cursor.lastrowid or 0)

    async def _query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return await asyncio.to_thread(self._query_sync, sql, tuple(params))

    def _query_sync(self, sql: str, params: tuple) -> list[sqlite3.Row]:
        return list(self.connection.execute(sql, params).fetchall())

    # ------------------------------------------------------------------ users

    async def get_user(self, user_id: int) -> dict[str, Any] | None:
        rows = await self._query("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return dict(rows[0]) if rows else None

    async def set_user_mode(self, user_id: int, mode: str) -> None:
        await self._write(
            """
            INSERT INTO users (user_id, mode, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET mode = excluded.mode,
                                               updated_at = excluded.updated_at
            """,
            (user_id, mode, time.time()),
        )

    async def set_user_pikpak_dir(self, user_id: int, folder: str) -> None:
        await self._write(
            """
            INSERT INTO users (user_id, pikpak_dir, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET pikpak_dir = excluded.pikpak_dir,
                                               updated_at = excluded.updated_at
            """,
            (user_id, folder, time.time()),
        )

    # ------------------------------------------------------------ media cache

    async def cache_lookup(self, key: str) -> dict[str, Any] | None:
        rows = await self._query("SELECT * FROM media_cache WHERE cache_key = ?", (key,))
        return dict(rows[0]) if rows else None

    async def cache_store(
        self,
        key: str,
        cache_chat_id: int,
        cache_msg_id: int,
        file_name: str | None,
        file_size: int | None,
    ) -> None:
        await self._write(
            """
            INSERT INTO media_cache
                (cache_key, cache_chat_id, cache_msg_id, file_name, file_size, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                cache_chat_id = excluded.cache_chat_id,
                cache_msg_id  = excluded.cache_msg_id,
                file_name     = excluded.file_name,
                file_size     = excluded.file_size,
                created_at    = excluded.created_at
            """,
            (key, cache_chat_id, cache_msg_id, file_name, file_size, time.time()),
        )

    async def cache_forget(self, key: str) -> None:
        await self._write("DELETE FROM media_cache WHERE cache_key = ?", (key,))

    # -------------------------------------------------------------- job stats

    async def record_job(self, user_id: int, link: str, mode: str) -> int:
        return await self._write(
            """
            INSERT INTO jobs (user_id, link, mode, status, created_at)
            VALUES (?, ?, ?, 'queued', ?)
            """,
            (user_id, link, mode, time.time()),
        )

    async def finish_job(
        self,
        job_id: int,
        status: str,
        file_name: str | None = None,
        file_size: int | None = None,
        error: str | None = None,
    ) -> None:
        await self._write(
            """
            UPDATE jobs
               SET status = ?, file_name = ?, file_size = ?, error = ?, finished_at = ?
             WHERE id = ?
            """,
            (status, file_name, file_size, error, time.time(), job_id),
        )

    async def user_stats(self, user_id: int) -> dict[str, Any]:
        rows = await self._query(
            """
            SELECT status, COUNT(*) AS count, COALESCE(SUM(file_size), 0) AS bytes
              FROM jobs WHERE user_id = ? GROUP BY status
            """,
            (user_id,),
        )
        return {row["status"]: {"count": row["count"], "bytes": row["bytes"]} for row in rows}

    async def recent_jobs(self, user_id: int, limit: int = 10) -> list[dict[str, Any]]:
        rows = await self._query(
            """
            SELECT id, link, mode, status, file_name, file_size, error, created_at
              FROM jobs WHERE user_id = ? ORDER BY created_at DESC LIMIT ?
            """,
            (user_id, limit),
        )
        return [dict(row) for row in rows]

    # -------------------------------------------------------------------- kv

    async def kv_get(self, key: str) -> str | None:
        rows = await self._query("SELECT value FROM kv WHERE key = ?", (key,))
        return rows[0]["value"] if rows else None

    async def kv_set(self, key: str, value: str) -> None:
        await self._write(
            """
            INSERT INTO kv (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )

    async def kv_delete(self, key: str) -> None:
        await self._write("DELETE FROM kv WHERE key = ?", (key,))

    async def kv_get_json(self, key: str) -> Any | None:
        raw = await self.kv_get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    async def kv_set_json(self, key: str, value: Any) -> None:
        await self.kv_set(key, json.dumps(value))

    async def get_or_create_secret(self, key: str = "url_signing_secret") -> str:
        """Return the persisted signing secret, generating one on first use."""
        existing = await self.kv_get(key)
        if existing:
            return existing
        secret = secrets.token_urlsafe(32)
        await self.kv_set(key, secret)
        return secret
