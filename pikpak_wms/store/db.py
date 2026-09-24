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

from ..core.models import Action, FileNode, Kind, Plan, normalize_path

# Columns added after a table first shipped (red line 3: add, never rename).
_ADDED_COLUMNS = (
    ("audit", "plan_id", "INTEGER"),
    ("audit", "undo_of", "INTEGER"),
)


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, kind in _ADDED_COLUMNS:
        present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in present:
            # Names come from the constant above, never from input.
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")


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
        _migrate(conn)
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

    async def insert(self, node: FileNode, *, synced_at: str | None = None) -> None:
        """Add or overwrite one entry (after this process changed the drive)."""
        stamp = synced_at or now_iso()
        await self._write(
            lambda conn: conn.execute(
                """
                INSERT OR REPLACE INTO files (file_id, parent_id, path, name, kind, size, mime,
                                              hash, created_time, modified_time, synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    node.file_id, node.parent_id, normalize_path(node.path), node.name,
                    str(node.kind), node.size, node.mime, node.hash, node.created_time,
                    node.modified_time, stamp,
                ),
            )
        )

    async def relocate(self, file_id: str, *, parent_id: str, path: str) -> None:
        """Record a rename or a move: the entry and everything under it."""
        path = normalize_path(path)

        def work(conn: sqlite3.Connection) -> None:
            row = conn.execute("SELECT path FROM files WHERE file_id = ?", (file_id,)).fetchone()
            if row is None:
                return
            old = row["path"]
            conn.execute(
                "UPDATE files SET parent_id = ?, path = ?, name = ? WHERE file_id = ?",
                (parent_id, path, path.rsplit("/", 1)[-1], file_id),
            )
            conn.execute(
                "UPDATE files SET path = ? || substr(path, ?) WHERE path LIKE ? ESCAPE '\\'",
                (path, len(old) + 1, _like_prefix(old) + "/%"),
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

    async def record(
        self,
        action: Action,
        *,
        dry_run: bool,
        at: str | None = None,
        plan_id: int | None = None,
        undo_of: int | None = None,
    ) -> int:
        """Write one audit row, with the before snapshot (rule 5). Returns its id."""

        def work(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(
                "INSERT INTO audit (action, file_id, before, after, rule_name, dry_run, at, "
                "plan_id, undo_of) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(action.type), action.file_id,
                    json.dumps(action.before, ensure_ascii=False),
                    json.dumps(action.after, ensure_ascii=False),
                    action.rule_name, int(dry_run), at or now_iso(), plan_id, undo_of,
                ),
            )
            return int(cursor.lastrowid or 0)

        return await self._write(work)

    async def audit_entry(self, audit_id: int) -> dict[str, Any] | None:
        rows = await self._read("SELECT * FROM audit WHERE id = ?", (audit_id,))
        return _audit(rows[0]) if rows else None

    async def audit_entries(
        self, *, limit: int = 50, applied_only: bool = False, plan_id: int | None = None
    ) -> list[dict]:
        clauses, params = [], []
        if applied_only:
            clauses.append("dry_run = 0")
        if plan_id is not None:
            clauses.append("plan_id = ?")
            params.append(plan_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = await self._read(
            # `where` is built from fixed strings; values are parameters.
            f"SELECT * FROM audit {where} ORDER BY id DESC LIMIT ?",
            (*params, limit),
        )
        return [_audit(row) for row in rows]

    async def undone_by(self, audit_id: int) -> int | None:
        """The audit id of the entry that undid ``audit_id``, if any."""
        rows = await self._read("SELECT id FROM audit WHERE undo_of = ? LIMIT 1", (audit_id,))
        return int(rows[0]["id"]) if rows else None

    # ---------------------------------------------------------------- plans

    async def save_plan(self, plan: Plan, *, fingerprint: str) -> int:
        stamp = now_iso()

        def work(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(
                "INSERT INTO plans (source, fingerprint, body, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (plan.source, fingerprint, json.dumps(plan.to_dict(), ensure_ascii=False),
                 stamp, stamp),
            )
            return int(cursor.lastrowid or 0)

        return await self._write(work)

    async def plan_row(self, plan_id: int) -> dict[str, Any] | None:
        rows = await self._read("SELECT * FROM plans WHERE id = ?", (plan_id,))
        return _plan(rows[0]) if rows else None

    async def plan_rows(self, *, status: list[str] | None = None, limit: int = 20) -> list[dict]:
        if status:
            marks = ",".join("?" for _ in status)
            rows = await self._read(
                f"SELECT * FROM plans WHERE status IN ({marks}) ORDER BY id DESC LIMIT ?",
                (*status, limit),
            )
        else:
            rows = await self._read("SELECT * FROM plans ORDER BY id DESC LIMIT ?", (limit,))
        return [_plan(row) for row in rows]

    async def open_plan_with(self, source: str, fingerprint: str) -> int | None:
        """A pending plan of ``source`` with exactly these actions, if one exists."""
        rows = await self._read(
            "SELECT id FROM plans WHERE source = ? AND fingerprint = ? AND status = 'pending' "
            "ORDER BY id DESC LIMIT 1",
            (source, fingerprint),
        )
        return int(rows[0]["id"]) if rows else None

    async def update_plan(
        self, plan_id: int, *, status: str, progress: int, result: dict[str, Any]
    ) -> None:
        await self._write(
            lambda conn: conn.execute(
                "UPDATE plans SET status = ?, progress = ?, result = ?, updated_at = ? "
                "WHERE id = ?",
                (status, progress, json.dumps(result, ensure_ascii=False), now_iso(), plan_id),
            )
        )

    # ---------------------------------------------------------------- tasks

    async def add_task(
        self, *, task_id: str, type: str, source: str, target_path: str, file_id: str = "",
        phase: str = "PENDING",
    ) -> None:
        await self._write(
            lambda conn: conn.execute(
                "INSERT INTO tasks (task_id, type, source, target_path, phase, file_id, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (task_id, type, source, target_path, phase, file_id or None, now_iso()),
            )
        )

    async def task_for(self, type: str, source: str) -> dict[str, Any] | None:
        rows = await self._read(
            "SELECT * FROM tasks WHERE type = ? AND source = ?", (type, source)
        )
        return dict(rows[0]) if rows else None

    async def task_rows(self, *, phases: list[str] | None = None, limit: int = 50) -> list[dict]:
        if phases:
            marks = ",".join("?" for _ in phases)
            rows = await self._read(
                f"SELECT * FROM tasks WHERE phase IN ({marks}) ORDER BY created_at DESC LIMIT ?",
                (*phases, limit),
            )
        else:
            rows = await self._read(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
            )
        return [dict(row) for row in rows]

    async def finish_task(
        self, task_id: str, *, phase: str, file_id: str | None = None, error: str | None = None
    ) -> None:
        await self._write(
            lambda conn: conn.execute(
                "UPDATE tasks SET phase = ?, file_id = COALESCE(?, file_id), error = ?, "
                "finished_at = ? WHERE task_id = ?",
                (phase, file_id, error, now_iso(), task_id),
            )
        )


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
        "plan_id": row["plan_id"],
        "undo_of": row["undo_of"],
    }


def _plan(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "source": row["source"],
        "status": row["status"],
        "fingerprint": row["fingerprint"],
        "plan": Plan.from_dict(json.loads(row["body"])),
        "progress": row["progress"],
        "result": json.loads(row["result"] or "{}"),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _like_prefix(path: str) -> str:
    """``path`` escaped for use as a LIKE prefix (``%`` and ``_`` are literal)."""
    return path.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
