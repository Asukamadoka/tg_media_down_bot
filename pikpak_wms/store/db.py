"""The local index, the inbound log and the audit trail, in one SQLite file.

Same approach as the bot's own database: standard-library ``sqlite3``, each
call pushed onto a worker thread, one lock serialising writes. Everything a
rule looks at is read from here, never from PikPak (rule 4).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

from ..core.models import Action, FileNode, Kind, normalize_path


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _node(row: sqlite3.Row) -> FileNode:
    return FileNode(
        file_id=row["file_id"],
        parent_id=row["parent_id"],
        name=row["name"],
        kind=Kind(row["kind"]),
        path=row["path"],
        size=row["size"],
        mime=row["mime"],
        hash=row["hash"],
        created_time=row["created_time"],
        modified_time=row["modified_time"],
        synced_at=row["synced_at"],
    )


class Store:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ lifecycle

    async def open(self) -> Store:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await asyncio.to_thread(self._connect)
        return self

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        schema = resources.files("pikpak_wms.store").joinpath("schema.sql").read_text("utf-8")
        conn.executescript(schema)
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.commit()
        return conn

    async def close(self) -> None:
        if self._conn is not None:
            conn, self._conn = self._conn, None
            await asyncio.to_thread(conn.close)

    async def __aenter__(self) -> Store:
        return await self.open()

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Store.open() has not been awaited")
        return self._conn

    async def _read(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return await asyncio.to_thread(lambda: list(self.conn.execute(sql, tuple(params))))

    async def _write(self, work) -> Any:
        async with self._lock:
            return await asyncio.to_thread(self._in_transaction, work)

    def _in_transaction(self, work) -> Any:
        with self.conn:
            return work(self.conn)

    # ---------------------------------------------------------------- files

    async def children(self, parent_id: str) -> list[FileNode]:
        rows = await self._read(
            "SELECT * FROM files WHERE parent_id = ? ORDER BY kind DESC, name", (parent_id,)
        )
        return [_node(row) for row in rows]

    async def node(self, file_id: str) -> FileNode | None:
        rows = await self._read("SELECT * FROM files WHERE file_id = ?", (file_id,))
        return _node(rows[0]) if rows else None

    async def node_at(self, path: str) -> FileNode | None:
        rows = await self._read("SELECT * FROM files WHERE path = ?", (normalize_path(path),))
        return _node(rows[0]) if rows else None

    async def nodes_under(self, path: str, *, recursive: bool = True) -> list[FileNode]:
        """Everything below ``path`` (not ``path`` itself)."""
        base = normalize_path(path)
        if base == "/":
            rows = await self._read("SELECT * FROM files ORDER BY path")
        else:
            rows = await self._read(
                "SELECT * FROM files WHERE path LIKE ? ESCAPE '\\' ORDER BY path",
                (_like_prefix(base) + "/%",),
            )
        nodes = [_node(row) for row in rows]
        if not recursive:
            depth = 0 if base == "/" else base.count("/")
            nodes = [n for n in nodes if n.path.count("/") == depth + 1]
        return nodes

    async def count_files(self) -> int:
        rows = await self._read("SELECT COUNT(*) AS n FROM files")
        return int(rows[0]["n"])

    async def replace_children(
        self, parent_id: str, parent_path: str, nodes: list[FileNode], synced_at: str
    ) -> None:
        """Make the index's view of one folder exactly ``nodes``.

        Children that are gone are removed along with everything under them;
        a child that moved elsewhere will be re-added where its new parent is
        listed.
        """

        def work(conn: sqlite3.Connection) -> None:
            seen = {node.file_id for node in nodes}
            gone = [
                (row["file_id"], row["path"])
                for row in conn.execute(
                    "SELECT file_id, path FROM files WHERE parent_id = ?", (parent_id,)
                )
                if row["file_id"] not in seen
            ]
            for file_id, path in gone:
                conn.execute("DELETE FROM files WHERE file_id = ?", (file_id,))
                conn.execute(
                    "DELETE FROM files WHERE path LIKE ? ESCAPE '\\'",
                    (_like_prefix(path) + "/%",),
                )
            for node in nodes:
                old = conn.execute(
                    "SELECT path FROM files WHERE file_id = ?", (node.file_id,)
                ).fetchone()
                if old is not None and old["path"] != node.path and node.is_folder:
                    # Renamed or moved: re-root the paths of what it contains.
                    conn.execute(
                        "UPDATE files SET path = ? || substr(path, ?) "
                        "WHERE path LIKE ? ESCAPE '\\'",
                        (node.path, len(old["path"]) + 1, _like_prefix(old["path"]) + "/%"),
                    )
                conn.execute(
                    """
                    INSERT INTO files (file_id, parent_id, path, name, kind, size, mime, hash,
                                       created_time, modified_time, synced_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(file_id) DO UPDATE SET
                        parent_id = excluded.parent_id, path = excluded.path,
                        name = excluded.name, kind = excluded.kind, size = excluded.size,
                        mime = excluded.mime, hash = excluded.hash,
                        created_time = excluded.created_time,
                        modified_time = excluded.modified_time,
                        synced_at = excluded.synced_at
                    """,
                    (
                        node.file_id, parent_id, node.path, node.name, str(node.kind),
                        node.size, node.mime, node.hash, node.created_time,
                        node.modified_time, synced_at,
                    ),
                )

        await self._write(work)

    async def has_pending_below(self, path: str) -> bool:
        """True when a folder under ``path`` was recorded but never listed."""
        rows = await self._read(
            "SELECT 1 FROM files WHERE kind = 'folder' AND modified_time IS NULL "
            "AND path LIKE ? ESCAPE '\\' LIMIT 1",
            (_like_prefix(normalize_path(path)) + "/%",),
        )
        return bool(rows)

    async def set_modified(self, file_id: str, modified_time: str | None) -> None:
        await self._write(
            lambda conn: conn.execute(
                "UPDATE files SET modified_time = ? WHERE file_id = ?", (modified_time, file_id)
            )
        )

    async def forget(self, file_ids: list[str]) -> None:
        """Drop entries (and their subtrees) the drive no longer has."""

        def work(conn: sqlite3.Connection) -> None:
            for file_id in file_ids:
                row = conn.execute(
                    "SELECT path FROM files WHERE file_id = ?", (file_id,)
                ).fetchone()
                if row is None:
                    continue
                conn.execute("DELETE FROM files WHERE file_id = ?", (file_id,))
                conn.execute(
                    "DELETE FROM files WHERE path LIKE ? ESCAPE '\\'",
                    (_like_prefix(row["path"]) + "/%",),
                )

        await self._write(work)

    # ----------------------------------------------------------------- meta

    async def get_meta(self, key: str) -> str | None:
        rows = await self._read("SELECT value FROM meta WHERE key = ?", (key,))
        return rows[0]["value"] if rows else None

    async def set_meta(self, key: str, value: str) -> None:
        await self._write(
            lambda conn: conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        )

    # ---------------------------------------------------------------- audit

    async def record(self, action: Action, *, dry_run: bool, at: str | None = None) -> int:
        """Write one audit row, with the before snapshot (rule 5). Returns its id."""

        def work(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(
                "INSERT INTO audit (action, file_id, before, after, rule_name, dry_run, at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(action.type), action.file_id,
                    json.dumps(action.before, ensure_ascii=False),
                    json.dumps(action.after, ensure_ascii=False),
                    action.rule_name, int(dry_run), at or now_iso(),
                ),
            )
            return int(cursor.lastrowid or 0)

        return await self._write(work)

    async def audit_entry(self, audit_id: int) -> dict[str, Any] | None:
        rows = await self._read("SELECT * FROM audit WHERE id = ?", (audit_id,))
        return _audit(rows[0]) if rows else None

    async def audit_entries(self, *, limit: int = 50, applied_only: bool = False) -> list[dict]:
        where = "WHERE dry_run = 0" if applied_only else ""
        rows = await self._read(
            # `where` is one of two fixed strings, never user input.
            f"SELECT * FROM audit {where} ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [_audit(row) for row in rows]


def _audit(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "action": row["action"],
        "file_id": row["file_id"],
        "before": json.loads(row["before"] or "{}"),
        "after": json.loads(row["after"] or "{}"),
        "rule_name": row["rule_name"],
        "dry_run": bool(row["dry_run"]),
        "at": row["at"],
    }


def _like_prefix(path: str) -> str:
    """``path`` escaped for use as a LIKE prefix (``%`` and ``_`` are literal)."""
    return path.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
