"""The download log: what has been taken out of the drive, and what was said to be
already on the NAS (docs/wms/M9.2 §A).

Rows are written by :mod:`pikpak_wms.ops.outbound` for every attempt and by
:func:`mark` when the owner says 「X 下过了」. :func:`scan_library` adds the files
that are already in the library but were never logged (they came before the log).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..core.models import FileNode, parse_time
from ..i18n import t
from ..rules.units import human_size
from ..store.db import now_iso
from .context import Context

SCANNED = "downloads:scanned"

current_plan: ContextVar[int | None] = ContextVar("downloads_plan", default=None)
current_user: ContextVar[int | None] = ContextVar("downloads_user", default=None)
"""Who asked for the downloads being made in this task, for the log."""
SKIPPED_NAMES = (".part", ".part.state", ".part.state.tmp")


async def log_attempt(
    ctx: Context, node: FileNode, status: str, started: str, *, plan_id: int | None = None,
    path: str = "", reason: str = "", fetch_info: dict | None = None,
) -> None:
    """One row in the download log; a failure to write it never fails the download."""
    info = fetch_info or {}
    try:
        if status == "skipped_exists" and path and await ctx.store.download_exists(
                path, ("done", "skipped_exists")):
            return  # a re-run over what was already logged
        plan = plan_id if plan_id is not None else current_plan.get()
        await ctx.store.add_download(
            name=node.name, size=node.size, file_id=node.file_id, hash=node.hash,
            dest_path=path, plan_id=plan, status=status, reason=reason[:300],
            started_at=started, finished_at=now_iso(), avg_mib_s=info.get("avg_mib_s"),
            links=info.get("links", ""), peak_connections=int(info.get("peak_connections") or 0),
            source="plan" if plan is not None else "manual", user_id=current_user.get())
    except Exception as exc:  # noqa: BLE001 - bookkeeping must not undo a download
        logging.getLogger(__name__).warning("could not write the download log: %s", exc)


def _library_roots(ctx: Context) -> list[Path]:
    """The folders the layout files downloads under: ``资源/整理`` by default."""
    config = ctx.config.outbound
    root = config.library_path
    if root is None:
        return []
    layout = config.default_layout or ""
    fixed = layout.split("{", 1)[0].strip("/")
    # ``资源/整理/{Y}`` → ``资源/整理``; a layout that starts with a placeholder → the library.
    return [root / fixed] if fixed else [root]


def _walk(base: Path) -> list[tuple[Path, int, float]]:
    found: list[tuple[Path, int, float]] = []
    for folder, _dirs, files in os.walk(base):
        for name in files:
            if name.startswith(".") or name.endswith(SKIPPED_NAMES):
                continue
            path = Path(folder) / name
            try:
                stat = path.stat()
            except OSError:
                continue
            found.append((path, stat.st_size, stat.st_mtime))
    return found


async def scan_library(ctx: Context) -> int:
    """Log the library files the index knows by name and size (``source=scan``).

    Returns how many rows were added; running it again adds none."""
    store = ctx.store
    identities = await store.file_identities()
    added = 0
    for base in _library_roots(ctx):
        if not base.is_dir():
            continue
        for path, size, mtime in await asyncio.to_thread(_walk, base):
            known = identities.get((path.name, size))
            if known is None or await store.download_exists(str(path)):
                continue
            file_id, digest = known
            when = datetime.fromtimestamp(mtime, UTC).isoformat(timespec="seconds")
            await store.add_download(
                name=path.name, size=size, file_id=file_id, hash=digest, dest_path=str(path),
                status="done", source="scan", started_at=when, finished_at=when)
            added += 1
    return added


async def scan_once(ctx: Context) -> int:
    """The first scan, at start: only once the index has something to match against."""
    if await ctx.store.get_meta(SCANNED) or await ctx.store.count_files() == 0:
        return 0
    added = await scan_library(ctx)
    await ctx.store.set_meta(SCANNED, "1")
    return added


async def mark(ctx: Context, fragments: list[str], *, user_id: int | None = None,
               plan_id: int | None = None) -> list[FileNode]:
    """The owner said these are already downloaded: a ``marked`` row for each file
    in the index whose name contains the fragment, or for the fragment itself when
    no file does (so a name the index does not know is remembered all the same).
    Returns the files that were marked."""
    marked: list[FileNode] = []
    ids, _pairs, fragments_known = await ctx.store.known_downloads()
    for fragment in dict.fromkeys(f.strip() for f in fragments if f.strip()):
        matches = await ctx.store.files_named(fragment)
        if not matches:
            if fragment.casefold() not in fragments_known:
                await ctx.store.add_download(
                    name=fragment, status="marked", reason="manual", source="manual",
                    user_id=user_id, plan_id=plan_id)
                fragments_known.append(fragment.casefold())
            continue
        for node in matches:
            if node.file_id not in ids:
                await ctx.store.add_download(
                    name=node.name, size=node.size, file_id=node.file_id, hash=node.hash,
                    status="marked", reason="manual", source="manual", user_id=user_id,
                    plan_id=plan_id)
                ids.add(node.file_id)
            marked.append(node)
    return marked


async def known_elsewhere(ctx: Context, node: FileNode, target: Path | None = None
                          ) -> tuple[bool, str]:
    """Does the log say this file (name and size) is already on the NAS somewhere
    other than ``target``? Returns ``(True, path)``; the path is empty for a file the
    owner said was downloaded. A row whose file has since gone from disk does not count."""
    for row in await ctx.store.downloads_with(node.name, node.size):
        if row["status"] == "skipped_exists":
            continue
        path = row["dest_path"]
        if target is not None and path == str(target):
            continue
        if not path:
            return True, ""
        if Path(path).exists():
            return True, path
    _ids, _pairs, fragments = await ctx.store.known_downloads()
    name = node.name.casefold()
    if any(fragment in name for fragment in fragments):
        return True, ""
    return False, ""


async def is_known(ctx: Context, node: FileNode) -> bool:
    """True when the log says this file is on the NAS: its id, its name and size,
    or a name fragment the owner said was downloaded."""
    ids, pairs, fragments = await ctx.store.known_downloads()
    return node_known(node, ids, pairs, fragments)


def node_known(node: FileNode, ids: set[str], pairs: set[tuple[str, int]],
               fragments: list[str]) -> bool:
    if node.file_id in ids or (node.name, node.size) in pairs:
        return True
    name = node.name.casefold()
    return any(fragment in name for fragment in fragments)


# ------------------------------------------------------------------ display


def _clock(value: str | None, ctx: Context) -> str:
    moment = parse_time(value) if value else None
    return moment.astimezone(ctx.config.schedule.tz).strftime("%m-%d %H:%M") if moment else "-"


def row_line(ctx: Context, row: dict[str, Any]) -> str:
    """One download as a person reads it."""
    status = t(f"downloads.status.{row['status']}")
    parts = [row["name"], human_size(row["size"]) if row["size"] else "-", status,
             _clock(row["finished_at"], ctx)]
    if row["avg_mib_s"]:
        parts.append(f"{float(row['avg_mib_s']):.1f} MiB/s")
    line = "  ".join(parts)
    where = row["dest_path"] or row["reason"]
    return f"{line}\n    {where}" if where else line


def since_for(ctx: Context, *, today: bool = False, days: int | None = None,
              since: str | None = None, now: datetime | None = None) -> str | None:
    """The ISO time a ``--today`` / ``--since`` listing starts at."""
    now = (now or datetime.now(UTC)).astimezone(ctx.config.schedule.tz)
    if today:
        return now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(
            UTC).isoformat(timespec="seconds")
    if days is not None:
        from datetime import timedelta

        return (now - timedelta(days=days)).astimezone(UTC).isoformat(timespec="seconds")
    if since:
        span = re.fullmatch(r"\s*(\d+)\s*d\s*", since)
        if span:  # 「7d」: seven days back
            return since_for(ctx, days=int(span.group(1)), now=now)
        moment = datetime.fromisoformat(since)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=ctx.config.schedule.tz)
        return moment.astimezone(UTC).isoformat(timespec="seconds")
    return None
