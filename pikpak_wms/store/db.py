"""The local index, the inbound log and the audit trail, in one SQLite file.

Same approach as the bot's own database: standard-library ``sqlite3``, each
call pushed onto a worker thread, one lock serialising writes. Everything a
rule looks at is read from here, never from PikPak (rule 4).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import unicodedata
from collections.abc import Iterable
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

from ..core import requests
from ..core.models import Action, FileNode, Kind, Plan, normalize_path
from ..core.redact import redact

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


AUDIT_BACKFILLED = "downloads:audit_backfilled"

KNOWN_STATUSES = ("done", "marked", "skipped_exists")
"""A file with a row in one of these is already on the NAS (or was said to be)."""


def _backfill_audit(conn: sqlite3.Connection) -> None:
    """Once: every applied local outbound in the audit becomes a ``done`` row
    (source ``backfill``). The meta flag makes a second open do nothing."""
    if conn.execute("SELECT 1 FROM meta WHERE key = ?", (AUDIT_BACKFILLED,)).fetchone():
        return
    rows = conn.execute(
        "SELECT file_id, before, after, at, plan_id FROM audit "
        "WHERE action = 'outbound' AND dry_run = 0 ORDER BY id"
    ).fetchall()
    for row in rows:
        before, after = json.loads(row["before"] or "{}"), json.loads(row["after"] or "{}")
        path = str(after.get("path") or "")
        if not path:
            continue  # links shown, or handed to aria2: nothing landed here
        name = str(before.get("name") or path.rsplit("/", 1)[-1])
        size = int(before.get("size") or 0)
        if any(same_place(path, r["dest_path"]) for r in conn.execute(
                "SELECT dest_path FROM downloads WHERE status = 'done' AND name = ? AND size = ?",
                (name, size))):
            continue  # already logged (by a scan, or by the attempt itself)
        fetch = after.get("fetch") if isinstance(after.get("fetch"), dict) else {}
        conn.execute(
            "INSERT INTO downloads (file_id, name, size, hash, dest_path, plan_id, status, "
            "finished_at, avg_mib_s, links, peak_connections, source) "
            "VALUES (?, ?, ?, ?, ?, ?, 'done', ?, ?, ?, ?, 'backfill')",
            (row["file_id"], name, size, str(before.get("hash") or ""),
             path, row["plan_id"], row["at"], fetch.get("avg_mib_s"), fetch.get("links", ""),
             int(fetch.get("peak_connections") or 0)),
        )
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (AUDIT_BACKFILLED, "1"))


FAILED_BACKFILLED = "downloads:failed_backfilled"

_SOURCE_RANK = {"plan": 0, "manual": 1, "backfill": 2, "scan": 3}
_MERGED = ("file_id", "hash", "plan_id", "started_at", "avg_mib_s", "links", "peak_connections",
           "user_id")


def _parts(path: str) -> list[str]:
    return [p for p in unicodedata.normalize("NFC", path).replace("\\", "/").split("/") if p]


def same_place(a: str, b: str) -> bool:
    """Do two destination paths name one place? The same path however it is spelled (a
    doubled or trailing slash, NFD or NFC), or the same place seen from the container
    (``/library/…``) and from the host (``/volume…/…``): one is the end of the other, or
    they share the last four parts (the file and the three day folders). An empty path
    knows nothing, so it is never the same place here (see :func:`_dedupe_downloads`)."""
    x, y = _parts(a), _parts(b)
    if not x or not y:
        return False
    if x == y or x[-len(y):] == y or y[-len(x):] == x:
        return True
    return len(x) >= 4 and len(y) >= 4 and x[-4:] == y[-4:]


def _dedupe_downloads(conn: sqlite3.Connection) -> int:
    """Merge the ``done`` rows that say the same thing twice: one file (name and size) at one
    destination, however the path is spelled (:func:`same_place`). A row with no path joins
    the group when the file has exactly one place. The best-sourced row stays (a real
    attempt over a backfill over a scan) and takes what the others knew that it did not.
    Returns the rows removed."""
    pairs = conn.execute(
        "SELECT name, size FROM downloads WHERE status = 'done' GROUP BY name, size "
        "HAVING COUNT(*) > 1").fetchall()
    removed = 0
    for pair in pairs:
        rows = conn.execute(
            "SELECT * FROM downloads WHERE status = 'done' AND name = ? AND size = ? "
            "ORDER BY id", tuple(pair)).fetchall()
        groups: list[list[sqlite3.Row]] = []
        for row in rows:
            if row["dest_path"]:
                for group in groups:
                    if any(same_place(row["dest_path"], m["dest_path"]) for m in group):
                        group.append(row)
                        break
                else:
                    groups.append([row])
        pathless = [r for r in rows if not r["dest_path"]]
        if pathless and len(groups) == 1:
            groups[0].extend(pathless)
        elif pathless and not groups:
            groups.append(pathless)
        for group in groups:
            if len(group) < 2:
                continue
            group.sort(key=lambda r: (_SOURCE_RANK.get(r["source"], 9), r["id"]))
            keeper, others = group[0], group[1:]
            fill = {}
            for column in ("dest_path", *_MERGED):
                if keeper[column] in (None, "", 0):
                    value = next((o[column] for o in others
                                  if o[column] not in (None, "", 0)), None)
                    if value is not None:
                        fill[column] = value
            if fill:
                # Column names come from the constants above, never from input.
                conn.execute(
                    "UPDATE downloads SET " + ", ".join(f"{c} = ?" for c in fill)
                    + " WHERE id = ?", (*fill.values(), keeper["id"]))
            for other in others:
                conn.execute("DELETE FROM downloads WHERE id = ?", (other["id"],))
                removed += 1
    return removed


def _backfill_failed(conn: sqlite3.Connection) -> None:
    """Once: the files that failed in a plan's result become ``failed`` rows (source
    ``backfill``). They were never in the log, because failures were recorded in the plan
    only. A file already logged as failed for that plan is left alone."""
    if conn.execute("SELECT 1 FROM meta WHERE key = ?", (FAILED_BACKFILLED,)).fetchone():
        return
    for plan in conn.execute("SELECT id, body, result, updated_at FROM plans").fetchall():
        try:
            failed = json.loads(plan["result"] or "{}").get("failed") or []
            actions = json.loads(plan["body"] or "{}").get("actions") or []
        except ValueError:
            continue
        for item in failed:
            if item.get("action") != "outbound":
                continue
            path = str(item.get("path") or "")
            action = next((a for a in actions if a.get("type") == "outbound" and (
                (item.get("file_id") and a.get("file_id") == item["file_id"])
                or (path and (a.get("before") or {}).get("path") == path))), None)
            before = (action or {}).get("before") or {}
            name = str(before.get("name") or path.rsplit("/", 1)[-1] or "?")
            file_id = str((action or {}).get("file_id") or item.get("file_id") or "")
            if conn.execute(
                    "SELECT 1 FROM downloads WHERE plan_id = ? AND status = 'failed' AND "
                    "(name = ? OR (file_id != '' AND file_id = ?))",
                    (plan["id"], name, file_id)).fetchone():
                continue
            conn.execute(
                "INSERT INTO downloads (file_id, name, size, hash, plan_id, status, reason, "
                "finished_at, source) VALUES (?, ?, ?, ?, ?, 'failed', ?, ?, 'backfill')",
                (file_id, name, int(before.get("size") or 0), str(before.get("hash") or ""),
                 plan["id"], redact(str(item.get("error") or ""))[:300], plan["updated_at"]))
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                 (FAILED_BACKFILLED, "1"))


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
        # One connection, used from worker threads: several files downloading at once
        # read and write together, and sqlite3 does not take that from one connection.
        self._thread_lock = threading.Lock()

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
        conn.executescript(requests.SCHEMA)
        _migrate(conn)
        with conn:
            _backfill_audit(conn)
            _backfill_failed(conn)
            _dedupe_downloads(conn)
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

    def _query(self, sql: str, params: tuple) -> list[sqlite3.Row]:
        with self._thread_lock:
            return list(self.conn.execute(sql, params))

    async def _read(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return await asyncio.to_thread(self._query, sql, tuple(params))

    async def _write(self, work) -> Any:
        async with self._lock:
            return await asyncio.to_thread(self._in_transaction, work)

    def _in_transaction(self, work) -> Any:
        with self._thread_lock, self.conn:
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

    async def created_times(self) -> list[str]:
        """When each file (not folder) arrived in the drive, as recorded."""
        rows = await self._read(
            "SELECT created_time FROM files WHERE kind != 'folder' AND created_time IS NOT NULL"
        )
        return [row["created_time"] for row in rows]

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

    async def delete_meta(self, key: str) -> None:
        await self._write(lambda conn: conn.execute("DELETE FROM meta WHERE key = ?", (key,)))

    async def meta_with_prefix(self, prefix: str) -> dict[str, str]:
        rows = await self._read(
            "SELECT key, value FROM meta WHERE substr(key, 1, ?) = ?", (len(prefix), prefix))
        return {row["key"]: row["value"] for row in rows}

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

    async def rewrite_plan(self, plan_id: int, plan: Plan, *, fingerprint: str) -> None:
        """Replace a plan's actions (an exclusion removed some, M9.2 §C.2)."""
        await self._write(
            lambda conn: conn.execute(
                "UPDATE plans SET body = ?, fingerprint = ?, updated_at = ? WHERE id = ?",
                (json.dumps(plan.to_dict(), ensure_ascii=False), fingerprint, now_iso(), plan_id),
            )
        )

    # ------------------------------------------------------------- requests

    async def post_request(self, kind: str, target: str = "", arg: str = "") -> int:
        """Ask the process running a plan to do something (``wms task``)."""
        return await self._write(lambda conn: requests.post(conn, kind, target, arg))

    async def take_requests(self, kind: str, targets: set[str], *, now: float | None = None
                            ) -> list[tuple[int, str, str]]:
        """Open requests of ``kind`` aimed at ``targets``: ``(id, target, arg)``. They stay
        open until :meth:`finish_request`."""
        rows = await self._write(
            lambda conn: requests.take(conn, kind, now, targets=targets, result=None))
        return [(int(row[0]), str(row[2]), str(row[3])) for row in rows]

    async def finish_request(self, request_id: int, result: str) -> None:
        await self._write(lambda conn: requests.finish(conn, request_id, result))

    async def request_outcome(self, request_id: int) -> tuple[bool, str]:
        return await self._write(lambda conn: requests.outcome(conn, request_id))

    # ------------------------------------------------------------ downloads

    async def add_download(
        self, *, name: str, status: str, file_id: str = "", size: int = 0, hash: str = "",
        dest_path: str = "", plan_id: int | None = None, reason: str = "",
        started_at: str | None = None, finished_at: str | None = None,
        avg_mib_s: float | None = None, links: str = "", peak_connections: int = 0,
        source: str = "plan", user_id: int | None = None,
    ) -> int:
        """One outbound attempt (M9.2 §A). Returns the row id."""

        def work(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(
                "INSERT INTO downloads (file_id, name, size, hash, dest_path, plan_id, status, "
                "reason, started_at, finished_at, avg_mib_s, links, peak_connections, source, "
                "user_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (file_id, name, size, hash, dest_path, plan_id, status, reason, started_at,
                 finished_at or now_iso(), avg_mib_s, links, peak_connections, source, user_id),
            )
            return int(cursor.lastrowid or 0)

        return await self._write(work)

    async def downloads(
        self, *, since: str | None = None, name: str | None = None,
        status: list[str] | None = None, limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Newest first. ``since`` is an ISO time compared with ``finished_at``;
        ``name`` is a case-insensitive substring."""
        clauses, params = [], []
        if since:
            clauses.append("finished_at >= ?")
            params.append(since)
        if name:
            clauses.append("name LIKE ? ESCAPE '\\'")
            params.append("%" + _like_prefix(name) + "%")
        if status:
            clauses.append("status IN (" + ",".join("?" for _ in status) + ")")
            params.extend(status)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = await self._read(
            # `where` is built from fixed strings; values are parameters.
            f"SELECT * FROM downloads {where} ORDER BY finished_at DESC, id DESC LIMIT ?",
            (*params, limit),
        )
        return [dict(row) for row in rows]

    async def downloads_with(self, name: str, size: int) -> list[dict[str, Any]]:
        """Rows that say this name and size is on the NAS (done, marked, skipped)."""
        marks = ",".join("?" for _ in KNOWN_STATUSES)
        rows = await self._read(
            f"SELECT * FROM downloads WHERE name = ? AND size = ? AND status IN ({marks}) "
            "ORDER BY id DESC",
            (name, size, *KNOWN_STATUSES),
        )
        return [dict(row) for row in rows]

    async def known_downloads(self) -> tuple[set[str], set[tuple[str, int]], list[str]]:
        """What is already on the NAS, for 「还没下载过的」: the file ids, the
        (name, size) pairs, and the name fragments the owner said were downloaded
        (``marked`` rows no file in the index matched)."""
        marks = ",".join("?" for _ in KNOWN_STATUSES)
        rows = await self._read(
            f"SELECT file_id, name, size FROM downloads WHERE status IN ({marks})",
            KNOWN_STATUSES,
        )
        ids = {row["file_id"] for row in rows if row["file_id"]}
        pairs = {(row["name"], int(row["size"])) for row in rows if row["size"]}
        fragments = [row["name"].casefold() for row in rows if not row["file_id"]]
        return ids, pairs, fragments

    async def download_exists(self, dest_path: str,
                              statuses: tuple[str, ...] = ("done",)) -> bool:
        marks = ",".join("?" for _ in statuses)
        rows = await self._read(
            f"SELECT 1 FROM downloads WHERE dest_path = ? AND status IN ({marks}) LIMIT 1",
            (dest_path, *statuses),
        )
        return bool(rows)

    async def download_placed(self, dest_path: str, name: str, size: int) -> bool:
        """Is this file at this place already in the log (``done`` or ``skipped_exists``),
        however the path is spelled (:func:`same_place`)?"""
        rows = await self._read(
            "SELECT dest_path FROM downloads WHERE name = ? AND size = ? "
            "AND status IN ('done', 'skipped_exists')", (name, size))
        return any(same_place(dest_path, row["dest_path"]) for row in rows)

    async def file_identities(self) -> dict[tuple[str, int], tuple[str, str]]:
        """(name, size) → (file_id, hash) for every file in the index."""
        rows = await self._read(
            "SELECT name, size, file_id, hash FROM files WHERE kind != 'folder'")
        return {(row["name"], int(row["size"])): (row["file_id"], row["hash"]) for row in rows}

    async def files_named(self, fragment: str) -> list[FileNode]:
        """Files whose name contains ``fragment`` (any case)."""
        rows = await self._read(
            "SELECT * FROM files WHERE kind != 'folder' AND name LIKE ? ESCAPE '\\' "
            "ORDER BY path",
            ("%" + _like_prefix(fragment) + "%",),
        )
        return [_node(row) for row in rows]

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
