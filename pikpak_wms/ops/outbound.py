"""Outbound: take files out of the drive (docs/wms/EXTRAS.md §3).

``outbound.downloader`` picks the destination:

* ``none``: show each file's direct link, fetch nothing;
* ``aria2``: hand the link to aria2 over JSON-RPC (secret from ``ARIA2_SECRET``);
* ``local``: fetch the file into ``outbound.local_dir`` (default: the bot's
  ``MEDIA_DIR``), through a ``.part`` file renamed when complete.

Direct links expire, so a plan stores file ids only; the link is fetched at
apply time and never written to the audit.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import Config
from ..core.errors import WmsError
from ..core.models import Action, ActionType, FileNode, Plan, normalize_path
from ..core.redact import redact
from ..rules.actions import Deliver
from ..rules.units import human_size
from ..store.db import now_iso
from . import downloads, library
from . import fetch as ranged
from .context import Context
from .organize import now

Fetch = Callable[[str, Path], Awaitable[int]]
"""Download ``url`` into the given file; returns the bytes written."""

Rpc = Callable[[str, dict[str, Any]], Awaitable[Any]]
"""POST a JSON-RPC body to the URL; returns the ``result``."""

Progress = Callable[[str, int, int], None]
"""(file name, bytes received, file size): told while a ``local`` fetch runs."""

CHUNK = 1024 * 1024


async def http_rpc(url: str, body: dict[str, Any]) -> Any:  # pragma: no cover - real network
    import aiohttp

    async with aiohttp.ClientSession() as session, session.post(url, json=body) as response:
        data = await response.json(content_type=None)
    if "error" in data:
        raise WmsError(f"aria2: {data['error']}", key="outbound.aria2_error",
                       error=str(data["error"]))
    return data.get("result")


def _safe_part(part: str) -> str:
    # No way out of the destination folder, whatever a file is called.
    return part.replace("\x00", "").strip() or "_"


def local_target(base: Path, to: str, name: str) -> Path:
    parts = [_safe_part(p) for p in to.split("/") if p not in ("", ".", "..")]
    return base.joinpath(*parts, _safe_part(name).replace("/", "_"))


def unique_target(target: Path) -> Path:
    """``name (2).ext``, ``name (3).ext`` …: the first that does not exist yet.
    Nothing a person put in the library is ever overwritten (docs/wms/M9.2 §B)."""
    number = 2
    while True:
        candidate = target.with_name(f"{target.stem} ({number}){target.suffix}")
        if not candidate.exists():
            return candidate
        number += 1


def resolve_target(ctx: Context, node: FileNode, to: str, *, when: datetime | None = None
                   ) -> tuple[Path, str]:
    """Where ``node`` will be written (before any ``(2)`` is chosen) and how to show it."""
    config = ctx.config.outbound
    library_root = config.library_path
    if library_root is not None:
        relative, shown = library.destination(ctx.config, to, when=when)
        base, to = library_root, relative
    else:
        base, shown = config.local_path, ""
    if base is None:
        raise WmsError("no local folder for outbound", key="outbound.no_local_dir")
    return local_target(base, to, node.name), shown


async def present(ctx: Context, node: FileNode, to: str, *, when: datetime | None = None
                  ) -> tuple[str, str] | None:
    """Would downloading ``node`` be skipped? ``("exists", path)`` when the file is
    at its destination with the same size, ``("known", path)`` when the download log
    has it somewhere else; None when it has to be fetched."""
    target, _shown = resolve_target(ctx, node, to, when=when)
    if target.exists() and target.stat().st_size == node.size:
        return "exists", str(target)
    if ctx.config.outbound.skip_known:
        found, where = await downloads.known_elsewhere(ctx, node, target)
        if found:
            return "known", where
    return None


async def drop_present(ctx: Context, plan: Plan, *, when: datetime | None = None
                       ) -> list[FileNode]:
    """Take the downloads that would only be skipped out of ``plan`` (in place) and
    say so in its notes, so the confirm screen shows the real work (M9.2 §B.2)."""
    gone: list[FileNode] = []
    keep: list[Action] = []
    for action in plan.actions:
        via = action.after.get("via") or ctx.config.outbound.downloader
        if action.type is not ActionType.OUTBOUND or via != "local":
            keep.append(action)
            continue
        node = await ctx.store.node(action.file_id) or FileNode.from_snapshot(action.before)
        try:
            found = await present(ctx, node, str(action.after.get("to") or ""), when=when)
        except WmsError:
            found = None  # the plan will say why when it is applied
        if found is None:
            keep.append(action)
        else:
            gone.append(node)
    if gone:
        plan.actions = keep
        names = "、".join(n.name for n in gone[:3]) + ("…" if len(gone) > 3 else "")
        plan.note("outbound.skipping", count=len(gone),
                  size=human_size(sum(n.size for n in gone)), names=names)
        if not keep:
            plan.note("outbound.all_present")
    return gone


def annotate(config: Config, plan: Plan, *, when: datetime | None = None) -> None:
    """Library mode: say, in each outbound action, where it will land (as a
    person reads it), refuse a place outside the library, and note any folder
    that will be created. The date of the default folder is the day the plan is
    *applied*, so what is shown here is today's and may differ then."""
    library_root = config.outbound.library_path
    if library_root is None:
        return
    created: list[str] = []
    for action in plan.actions:
        if action.type is not ActionType.OUTBOUND:
            continue
        relative, shown = library.destination(config, str(action.after.get("to") or ""), when=when)
        action.after["shown"] = shown
        for folder in library.missing_dirs(library_root, relative):
            if library.display_path(folder) not in created:
                created.append(library.display_path(folder))
    if created:
        plan.note("outbound.will_create", dirs="、".join(created[-1:]))


def _verify_hash(part: Path, node: FileNode) -> None:
    """``OUTBOUND_VERIFY=hash``: compare PikPak's content id when it gave one
    in the form this knows (40 hex digits); otherwise there is nothing to compare."""
    if not re.fullmatch(r"[0-9A-Fa-f]{40}", node.hash or ""):
        return
    if ranged.gcid(part, node.size or None) != node.hash.upper():
        part.unlink(missing_ok=True)
        ranged.state_path(part).unlink(missing_ok=True)
        raise WmsError(f"{node.path}: content id differs", key="outbound.hash_differs",
                       path=node.path)


def make_deliver(
    ctx: Context, *, downloader: str | None = None, fetch: Fetch | None = None,
    rpc: Rpc | None = None, io: ranged.RangeIO | None = None,
    progress: Progress | None = None, clock: Callable[[], datetime] | None = None,
    pool: ranged.ConnectionPool | None = None, sleep: Callable[[float], Awaitable[None]]
    | None = None, fetch_clock: Callable[[], float] | None = None, plan_id: int | None = None,
    show_links: bool = False,
) -> Deliver:
    """One deliver function for a plan. Each action may name its own
    downloader (``via``); otherwise ``downloader``, else the config's.

    ``local`` fetches over ``outbound.connections`` Range connections
    (:mod:`pikpak_wms.ops.fetch`); ``fetch`` replaces that with one plain
    download function (tests, and anything that cannot do ranges). ``clock``
    says what day it is *now*: the default folder is the day the download runs.
    Every ``local`` attempt is written to the download log (docs/wms/M9.2 §A):
    done, skipped because it is already there, failed, or cancelled by hand.
    The links-only mode shows signed URLs only with ``show_links``; otherwise the query
    is replaced (docs/wms/M9.3 §A.7).
    ``pool`` is the connection budget shared with every other download; ``sleep`` and
    ``fetch_clock`` (tests) stand in for the pauses and the time of its retries.
    """
    config = ctx.config.outbound
    default = downloader or config.downloader
    pool = pool if pool is not None else ranged.POOL
    pool.configure(config.max_total_connections)
    # PikPak's API is asked for a few links at a time, however many files are running.
    link_calls = asyncio.Semaphore(4)

    async def links(node: FileNode, to: str) -> dict[str, Any]:
        url = await ctx.client.download_url(node.file_id)
        shown = url if show_links else redact(url)
        return {"downloader": "none", "_output": f"{node.path}\n  {shown}"}

    async def aria2(node: FileNode, to: str) -> dict[str, Any]:
        url = await ctx.client.download_url(node.file_id)
        folder = "/".join(p for p in (config.aria2.dir.rstrip("/"), to) if p)
        params: list[Any] = [[url], {"dir": folder, "out": node.name}]
        secret = os.environ.get("ARIA2_SECRET", "").strip()
        if secret:
            params.insert(0, f"token:{secret}")
        gid = await (rpc or http_rpc)(config.aria2.rpc_url, {
            "jsonrpc": "2.0", "id": "wms", "method": "aria2.addUri", "params": params,
        })
        return {"downloader": "aria2", "dir": folder, "gid": str(gid)}

    def track_of(node: FileNode):
        find = getattr(progress, "track", None)
        return find(node.file_id) if find is not None else None

    async def local(node: FileNode, to: str) -> dict[str, Any]:
        # A place was resolved when the plan was made; do it again, since this is
        # the door the files go through.
        target, shown = resolve_target(ctx, node, to, when=clock() if clock else None)
        extra = {"library_path": shown} if shown else {}
        track = track_of(node)
        if target.exists():
            same = target.stat().st_size == node.size
            if same and config.verify_mode == "hash" and re.fullmatch(
                    r"[0-9A-Fa-f]{40}", node.hash or ""):
                same = await asyncio.to_thread(
                    lambda: ranged.gcid(target, node.size or None) == node.hash.upper())
            if same:
                return {"downloader": "local", "path": str(target), "skipped": "exists", **extra}
            target = unique_target(target)  # never overwrite what is there
        elif config.skip_known:
            found, where = await downloads.known_elsewhere(ctx, node, target)
            if found:
                return {"downloader": "local", "skipped": "known", "known_at": where, **extra}
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except PermissionError as exc:
            where = library.display_path(
                str(target.relative_to(config.library_path)).split("/")[0]
            ) if config.library_path is not None and target.is_relative_to(
                config.library_path) else str(target.parent)
            raise WmsError(f"no permission to write {where}", key="outbound.no_permission",
                           path=where) from exc
        part = target.with_name(target.name + ".part")
        if track is not None:
            track.part = part

        async def url_for():
            # The web link and the origin media link, from one API call.
            async with link_calls:
                found = getattr(ctx.client, "download_links", None)
                if found is None:
                    return await ctx.client.download_url(node.file_id)
                web, origin = await found(node.file_id)
                return ranged.Links(web, origin)

        stats = ranged.FetchStats()
        note = getattr(progress, "info", None)

        def report(received: int, total: int) -> None:
            if track is not None:
                track.bytes(received, total or node.size)
                track.info(stats.connections, stats.links)
            if progress is not None:
                progress(node.name, received, total or node.size)
                if note is not None:
                    note(stats.connections, stats.links)

        if progress is not None:
            progress(node.name, 0, node.size)
        if fetch is not None:
            try:
                got = await url_for()
                written = await fetch(getattr(got, "web", got), part)
            except Exception:
                part.unlink(missing_ok=True)
                raise
        else:
            # An interrupted part stays on disk: the next run carries on from it.
            kwargs: dict[str, Any] = {}
            if sleep is not None:
                kwargs["sleep"] = sleep
            if fetch_clock is not None:
                kwargs["clock"] = fetch_clock
            written = await ranged.download(
                url_for, io or ranged.AiohttpIO(), part, connections=config.parallel,
                max_connections=config.max_parallel, progress=report, stats=stats,
                pool=pool, retry_seconds=config.retry_minutes * 60, **kwargs,
            )
        if config.verify_mode != "off" and node.size and written != node.size:
            part.unlink(missing_ok=True)
            ranged.state_path(part).unlink(missing_ok=True)
            raise WmsError(f"{node.path}: got {written} of {node.size} bytes",
                           key="outbound.short", path=node.path, got=written, size=node.size)
        if config.verify_mode == "hash":
            _verify_hash(part, node)
        part.replace(target)
        return {"downloader": "local", "path": str(target), **extra,
                **({"fetch": stats.as_dict()} if fetch is None and stats.seconds else {})}

    modes = {"none": links, "aria2": aria2, "local": local}
    if default not in modes:
        raise WmsError(f"unknown downloader {default}", key="outbound.unknown", mode=default)

    async def log(node: FileNode, status: str, started: str, *, path: str = "",
                  reason: str = "", fetch_info: dict | None = None) -> None:
        await downloads.log_attempt(ctx, node, status, started, plan_id=plan_id, path=path,
                                    reason=reason, fetch_info=fetch_info)

    async def deliver(node: FileNode, to: str, via: str | None = None) -> dict[str, Any]:
        mode = via or default
        if mode not in modes:
            raise WmsError(f"unknown downloader {mode}", key="outbound.unknown", mode=mode)
        started = now_iso()
        try:
            result = await modes[mode](node, to)
        except asyncio.CancelledError:
            track = track_of(node)
            if mode == "local" and track is not None and track.cancel_requested:
                track.logged_cancel = True
                await log(node, "cancelled", started, reason="cancelled")
            raise
        except WmsError as exc:
            if mode == "local":
                await log(node, "failed", started, reason=exc.display())
            raise
        except Exception as exc:
            # One file that cannot be fetched fails that action, not the whole plan.
            error = redact(f"{type(exc).__name__}: {exc}")[:120]
            if mode == "local":
                await log(node, "failed", started, reason=error)
            raise WmsError(f"{node.path}: {error}", key="outbound.failed",
                           path=node.path, error=error) from exc
        if mode == "local":
            skipped = result.get("skipped")
            if skipped == "exists":
                await log(node, "skipped_exists", started, path=result["path"],
                          reason="exists")
            elif skipped == "known":
                await log(node, "skipped_exists", started, path="",
                          reason=result["known_at"] or "marked")
            else:
                await log(node, "done", started, path=result["path"],
                          fetch_info=result.get("fetch"))
        return result

    return deliver


async def plan_paths(ctx: Context, paths: list[str], *, to: str = "",
                     downloader: str | None = None) -> Plan:
    """An outbound plan for files, or every file under folders, from the index.

    ``downloader`` is kept in each action (``after.via``), so that applying the plan
    later, from anywhere, uses the downloader it was built with (docs/wms/M9.3 §A.6)."""
    if ctx.config.outbound.library_path is not None and to.strip():
        library.resolve_user_path(to, library_dir=ctx.config.outbound.library_path)
    plan = Plan(source="outbound", generated_at=now().isoformat(timespec="seconds"))
    seen: set[str] = set()
    for raw in paths:
        path = normalize_path(raw)
        node = await ctx.store.node_at(path)
        if node is None:
            plan.note("outbound.missing", path=path)
            continue
        files = (
            [n for n in await ctx.store.nodes_under(path) if not n.is_folder]
            if node.is_folder else [node]
        )
        for item in files:
            if item.file_id in seen:
                continue
            seen.add(item.file_id)
            plan.actions.append(
                Action(ActionType.OUTBOUND, item.file_id, before=item.snapshot(),
                       after={"to": to.strip("/"), **({"via": downloader} if downloader else {})},
                       rule_name="outbound")
            )
    annotate(ctx.config, plan)
    return plan
