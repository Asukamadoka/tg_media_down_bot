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

import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ..core.errors import WmsError
from ..core.models import Action, ActionType, FileNode, Plan, normalize_path
from ..rules.actions import Deliver
from .context import Context
from .organize import now

Fetch = Callable[[str, Path], Awaitable[int]]
"""Download ``url`` into the given file; returns the bytes written."""

Rpc = Callable[[str, dict[str, Any]], Awaitable[Any]]
"""POST a JSON-RPC body to the URL; returns the ``result``."""

CHUNK = 1024 * 1024


async def http_fetch(url: str, target: Path) -> int:  # pragma: no cover - real network
    import aiohttp

    written = 0
    timeout = aiohttp.ClientTimeout(total=None, sock_read=300)
    async with aiohttp.ClientSession(timeout=timeout) as session, session.get(url) as response:
        response.raise_for_status()
        with target.open("wb") as handle:
            async for chunk in response.content.iter_chunked(CHUNK):
                handle.write(chunk)
                written += len(chunk)
    return written


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


def make_deliver(
    ctx: Context, *, downloader: str | None = None, fetch: Fetch | None = None,
    rpc: Rpc | None = None,
) -> Deliver:
    config = ctx.config.outbound
    mode = downloader or config.downloader

    if mode == "none":
        async def links(node: FileNode, to: str) -> dict[str, Any]:
            url = await ctx.client.download_url(node.file_id)
            return {"downloader": "none", "_output": f"{node.path}\n  {url}"}

        return links

    if mode == "aria2":
        call = rpc or http_rpc

        async def aria2(node: FileNode, to: str) -> dict[str, Any]:
            url = await ctx.client.download_url(node.file_id)
            folder = "/".join(p for p in (config.aria2.dir.rstrip("/"), to) if p)
            params: list[Any] = [[url], {"dir": folder, "out": node.name}]
            secret = os.environ.get("ARIA2_SECRET", "").strip()
            if secret:
                params.insert(0, f"token:{secret}")
            gid = await call(config.aria2.rpc_url, {
                "jsonrpc": "2.0", "id": "wms", "method": "aria2.addUri", "params": params,
            })
            return {"downloader": "aria2", "dir": folder, "gid": str(gid)}

        return aria2

    if mode == "local":
        base = config.local_path
        if base is None:
            raise WmsError("no local folder for outbound", key="outbound.no_local_dir")
        get = fetch or http_fetch

        async def local(node: FileNode, to: str) -> dict[str, Any]:
            target = local_target(base, to, node.name)
            if target.exists() and target.stat().st_size == node.size:
                return {"downloader": "local", "path": str(target), "skipped": True}
            target.parent.mkdir(parents=True, exist_ok=True)
            part = target.with_name(target.name + ".part")
            url = await ctx.client.download_url(node.file_id)
            try:
                written = await get(url, part)
            except Exception:
                part.unlink(missing_ok=True)
                raise
            if node.size and written != node.size:
                part.unlink(missing_ok=True)
                raise WmsError(f"{node.path}: got {written} of {node.size} bytes",
                               key="outbound.short", path=node.path, got=written,
                               size=node.size)
            part.replace(target)
            return {"downloader": "local", "path": str(target)}

        return local

    raise WmsError(f"unknown downloader {mode}", key="outbound.unknown", mode=mode)


async def plan_paths(ctx: Context, paths: list[str], *, to: str = "") -> Plan:
    """An outbound plan for files, or every file under folders, from the index."""
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
    return plan
