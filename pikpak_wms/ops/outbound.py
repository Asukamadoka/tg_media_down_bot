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
from ..rules.actions import Deliver
from . import fetch as ranged
from . import library
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
) -> Deliver:
    """One deliver function for a plan. Each action may name its own
    downloader (``via``); otherwise ``downloader``, else the config's.

    ``local`` fetches over ``outbound.connections`` Range connections
    (:mod:`pikpak_wms.ops.fetch`); ``fetch`` replaces that with one plain
    download function (tests, and anything that cannot do ranges). ``clock``
    says what day it is *now*: the default folder is the day the download runs.
    """
    config = ctx.config.outbound
    default = downloader or config.downloader

    async def links(node: FileNode, to: str) -> dict[str, Any]:
        url = await ctx.client.download_url(node.file_id)
        return {"downloader": "none", "_output": f"{node.path}\n  {url}"}

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

    async def local(node: FileNode, to: str) -> dict[str, Any]:
        library_root = config.library_path
        if library_root is not None:
            # A place was resolved when the plan was made; do it again, since
            # this is the door the files go through.
            relative, shown = library.destination(ctx.config, to, when=clock() if clock else None)
            base, to = library_root, relative
        else:
            base, shown = config.local_path, ""
        if base is None:
            raise WmsError("no local folder for outbound", key="outbound.no_local_dir")
        target = local_target(base, to, node.name)
        if target.exists() and target.stat().st_size == node.size:
            return {"downloader": "local", "path": str(target), "skipped": True,
                    **({"library_path": shown} if shown else {})}
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except PermissionError as exc:
            where = library.display_path(to.split("/")[0]) if library_root else str(base)
            raise WmsError(f"no permission to write {where}", key="outbound.no_permission",
                           path=where) from exc
        part = target.with_name(target.name + ".part")

        async def url_for() -> str:
            return await ctx.client.download_url(node.file_id)

        def report(received: int, total: int) -> None:
            if progress is not None:
                progress(node.name, received, total or node.size)

        if progress is not None:
            progress(node.name, 0, node.size)
        if fetch is not None:
            try:
                written = await fetch(await url_for(), part)
            except Exception:
                part.unlink(missing_ok=True)
                raise
        else:
            # An interrupted part stays on disk: the next run carries on from it.
            written = await ranged.download(
                url_for, io or ranged.AiohttpIO(), part, connections=config.parallel,
                progress=report,
            )
        if config.verify_mode != "off" and node.size and written != node.size:
            part.unlink(missing_ok=True)
            ranged.state_path(part).unlink(missing_ok=True)
            raise WmsError(f"{node.path}: got {written} of {node.size} bytes",
                           key="outbound.short", path=node.path, got=written, size=node.size)
        if config.verify_mode == "hash":
            _verify_hash(part, node)
        part.replace(target)
        return {"downloader": "local", "path": str(target),
                **({"library_path": shown} if shown else {})}

    modes = {"none": links, "aria2": aria2, "local": local}
    if default not in modes:
        raise WmsError(f"unknown downloader {default}", key="outbound.unknown", mode=default)

    async def deliver(node: FileNode, to: str, via: str | None = None) -> dict[str, Any]:
        mode = via or default
        if mode not in modes:
            raise WmsError(f"unknown downloader {mode}", key="outbound.unknown", mode=mode)
        try:
            return await modes[mode](node, to)
        except (WmsError, asyncio.CancelledError):
            raise
        except Exception as exc:
            # One file that cannot be fetched fails that action, not the whole plan.
            raise WmsError(f"{node.path}: {type(exc).__name__}: {exc}", key="outbound.failed",
                           path=node.path, error=f"{type(exc).__name__}: {exc}"[:120]) from exc

    return deliver


async def plan_paths(ctx: Context, paths: list[str], *, to: str = "") -> Plan:
    """An outbound plan for files, or every file under folders, from the index."""
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
                       after={"to": to.strip("/")}, rule_name="outbound")
            )
    annotate(ctx.config, plan)
    return plan
