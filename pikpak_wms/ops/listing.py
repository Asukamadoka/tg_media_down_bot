"""Read-only views: a folder from the index, and the account's quota."""

from __future__ import annotations

from ..core.errors import NotFoundError
from ..core.models import FileNode, Quota, normalize_path
from .context import Context


async def ls(ctx: Context, path: str = "/") -> list[FileNode]:
    """A folder's contents, from the local index (rule 4), folders first.

    Raises NotFoundError when the index has no folder there, which may just
    mean it has not been stocktaken yet.
    """
    path = normalize_path(path)
    if path != "/":
        folder = await ctx.store.node_at(path)
        if folder is None or not folder.is_folder:
            raise NotFoundError(
                f"{path} is not a folder in the index; run a stocktake",
                key="error.not_indexed", path=path,
            )
    nodes = await ctx.store.nodes_under(path, recursive=False)
    return sorted(nodes, key=lambda node: (not node.is_folder, node.name.lower()))


async def quota(ctx: Context) -> Quota:
    return await ctx.client.quota()


async def events_raw(ctx: Context, *, limit: int = 20) -> dict:
    """PikPak's ``events`` answer, untouched (docs/wms/EXTRAS.md §5)."""
    return await ctx.client.events(page_size=limit)
