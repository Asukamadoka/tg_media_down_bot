"""Stocktake: copy the drive's folder tree into the local index.

Every rule is evaluated against the index, never against PikPak directly
(rule 4), so the index has to be complete and current.

**Full** lists every folder. **Incremental** lists a folder only when it is
new to the index, or when its ``modified_time`` or its path changed since the
last run; an unchanged folder's whole subtree is taken as unchanged. That is
the design in docs/wms/ARCHITECTURE.md §6, and it is only as good as PikPak's
habit of touching a folder's ``modified_time`` when something inside it
changes. :func:`verify` measures exactly that: it walks the whole drive
without writing and reports how the index differs, so the assumption can be
checked on a real account (see docs/HANDOFF.md, WMS M1).
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field, replace

from ..core.client import WmsClient
from ..core.models import ROOT_ID, FileNode, normalize_path
from ..i18n import t
from ..store.db import Store, now_iso


@dataclass
class StocktakeReport:
    full: bool
    folders_listed: int = 0
    folders_skipped: int = 0
    entries: int = 0
    requests: int = 0
    seconds: float = 0.0
    roots: list[str] = field(default_factory=list)

    def summary(self) -> str:
        """For a person, in the WMS language."""
        return t(
            "stocktake.full" if self.full else "stocktake.incremental",
            roots=", ".join(self.roots),
            entries=self.entries,
            listed=self.folders_listed,
            skipped=self.folders_skipped,
            requests=self.requests,
            seconds=f"{self.seconds:.1f}",
        )


async def _root(client: WmsClient, store: Store, path: str) -> tuple[str, str]:
    path = normalize_path(path)
    if path == "/":
        return ROOT_ID, "/"
    known = await store.node_at(path)
    if known is not None and known.is_folder:
        return known.file_id, path
    return await client.resolve_path(path), path


async def stocktake(
    client: WmsClient,
    store: Store,
    *,
    roots: list[str] | None = None,
    full: bool = False,
    page_size: int = 100,
) -> StocktakeReport:
    """Bring the index up to date for ``roots`` (default: the whole drive)."""
    roots = roots or ["/"]
    report = StocktakeReport(full=full, roots=[normalize_path(r) for r in roots])
    started = time.monotonic()
    requests_before = client.calls
    synced_at = now_iso()

    for root in roots:
        root_id, root_path = await _root(client, store, root)
        # (folder id, path, the modified_time to record once it is listed)
        pending: list[tuple[str, str, str | None]] = [(root_id, root_path, None)]
        while pending:
            folder_id, folder_path, live_modified = pending.pop()
            listed = [
                node
                async for node in client.list_folder(
                    folder_id, parent_path=folder_path, page_size=page_size
                )
            ]
            report.folders_listed += 1
            report.entries += len(listed)
            known = {node.file_id: node for node in await store.children(folder_id)}

            stored: list[FileNode] = []
            for node in listed:
                if not node.is_folder:
                    stored.append(node)
                    continue
                before = known.get(node.file_id)
                changed = (
                    before is None
                    or before.modified_time != node.modified_time
                    or before.path != node.path
                    # An earlier run stopped inside this subtree: finish it.
                    or await store.has_pending_below(node.path)
                )
                if full or changed:
                    pending.append((node.file_id, node.path, node.modified_time))
                    # Recorded without its modified_time until it has itself
                    # been listed. A run cut short leaves it blank, which the
                    # next run reads as changed (and finds from any ancestor
                    # through has_pending_below), so no subtree is lost.
                    stored.append(replace(node, modified_time=None))
                else:
                    report.folders_skipped += 1
                    stored.append(node)
            await store.replace_children(folder_id, folder_path, stored, synced_at)
            if folder_id != ROOT_ID:
                await store.set_modified(folder_id, live_modified)

    report.seconds = time.monotonic() - started
    report.requests = client.calls - requests_before
    await store.set_meta("last_stocktake", synced_at)
    # Numbers, not a sentence: stored values are never translated.
    await store.set_meta("last_stocktake_report", json.dumps(asdict(report)))
    return report


@dataclass
class VerifyReport:
    missing: list[str] = field(default_factory=list)
    """On the drive, not in the index."""

    stale: list[str] = field(default_factory=list)
    """In the index, gone from the drive."""

    changed: list[str] = field(default_factory=list)
    """In both, but name, size or modified time differ."""

    checked: int = 0

    @property
    def clean(self) -> bool:
        return not (self.missing or self.stale or self.changed)

    def summary(self) -> str:
        if self.clean:
            return t("verify.clean", checked=self.checked)
        return t(
            "verify.dirty",
            missing=len(self.missing),
            stale=len(self.stale),
            changed=len(self.changed),
            checked=self.checked,
        )


async def verify(
    client: WmsClient, store: Store, *, roots: list[str] | None = None, page_size: int = 100
) -> VerifyReport:
    """Walk the whole drive without writing, and compare with the index."""
    report = VerifyReport()
    live: dict[str, FileNode] = {}
    for root in roots or ["/"]:
        pending = [await _root(client, store, root)]
        while pending:
            folder_id, folder_path = pending.pop()
            async for node in client.list_folder(
                folder_id, parent_path=folder_path, page_size=page_size
            ):
                live[node.file_id] = node
                if node.is_folder:
                    pending.append((node.file_id, node.path))

    indexed: dict[str, FileNode] = {}
    for root in roots or ["/"]:
        for node in await store.nodes_under(root):
            indexed[node.file_id] = node

    report.checked = len(live)
    for file_id, node in live.items():
        before = indexed.get(file_id)
        if before is None:
            report.missing.append(node.path)
        elif (before.path, before.size, before.modified_time) != (
            node.path,
            node.size,
            node.modified_time,
        ):
            report.changed.append(node.path)
    report.stale = [node.path for file_id, node in indexed.items() if file_id not in live]
    return report
