"""Inbound: bring files into the drive from a magnet / URL or a PikPak share link.

Idempotent (rule 5): the ``tasks`` table has one row per source, so the same
link twice gives back the first task instead of downloading it again.
Dry-run by default like every write (rule 1).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..core.errors import WmsError
from ..core.models import Action, ActionType, join_path, normalize_path
from ..rules.actions import Runtime
from .context import Context

OFFLINE, SHARE = "offline", "share_restore"
_SHARE_LINK = re.compile(r"https?://(?:www\.)?mypikpak\.com/s/([^/?#\s]+)", re.IGNORECASE)
_OFFLINE_LINK = re.compile(r"^(magnet:\?|https?://|ftp://|ed2k://|thunder://)", re.IGNORECASE)

# PikPak phases → the tasks table's.
_PHASES = {
    "PHASE_TYPE_COMPLETE": "DONE",
    "PHASE_TYPE_ERROR": "ERROR",
    "PHASE_TYPE_RUNNING": "RUNNING",
    "PHASE_TYPE_PENDING": "PENDING",
}


def classify(source: str) -> str:
    source = source.strip()
    if _SHARE_LINK.search(source):
        return SHARE
    if _OFFLINE_LINK.match(source):
        return OFFLINE
    raise WmsError(f"not a link WMS can take in: {source}", key="inbound.unknown",
                   source=source)


@dataclass
class InboundResult:
    kind: str
    source: str
    target: str
    applied: bool = False
    existing: dict | None = None
    """The earlier task for this source, when there was one (nothing was sent)."""
    task_id: str = ""
    names: list[str] | None = None


async def inbound(
    ctx: Context, source: str, *, to: str | None = None, pass_code: str = "",
    apply_now: bool = False,
) -> InboundResult:
    source = source.strip()
    kind = classify(source)
    target = normalize_path(to or ctx.config.layout.inbox)
    result = InboundResult(kind=kind, source=source, target=target)
    result.existing = await ctx.store.task_for(kind, source)
    if result.existing is not None or not apply_now:
        return result

    if kind == OFFLINE:
        rt = Runtime(client=ctx.client, store=ctx.store)
        folder = await rt.folder_id(target, create=True)
        reply = await ctx.client.offline_download(source, folder)
        task = reply.get("task") or {}
        result.task_id = str(task.get("id") or "")
        name = str(task.get("file_name") or task.get("name") or "")
        result.names = [name] if name else []
        await ctx.store.add_task(task_id=result.task_id or f"offline:{source}", type=kind,
                                 source=source, target_path=target,
                                 file_id=str(task.get("file_id") or ""))
        file_id = str(task.get("file_id") or "")
        path = join_path(target, name) if name else target
    else:
        share_id = _SHARE_LINK.search(source).group(1)  # type: ignore[union-attr]
        info = await ctx.client.share_info(source, pass_code)
        status = info.get("share_status")
        if status and status not in ("OK", "SHARE_STATUS_OK"):
            raise WmsError(f"share link status {status}", key="inbound.share_unusable",
                           status=status)
        files = [f for f in info.get("files") or [] if f.get("id")]
        if not files:
            raise WmsError("the share link has no files", key="inbound.share_empty")
        await ctx.client.restore_share(share_id, str(info.get("pass_code_token") or ""),
                                       [str(f["id"]) for f in files])
        result.task_id = f"share:{share_id}"
        result.names = [str(f.get("name") or f["id"]) for f in files]
        # PikPak's restore call takes no destination folder: the files land
        # where PikPak puts restored shares, not in `target` (docs/HANDOFF.md).
        await ctx.store.add_task(task_id=result.task_id, type=kind, source=source,
                                 target_path="", phase="DONE")
        file_id, path = "", ", ".join(result.names)

    await ctx.store.record(
        Action(ActionType.INBOUND, file_id, after={"source": source, "path": path,
                                                   "task_id": result.task_id}),
        dry_run=False,
    )
    result.applied = True
    return result


@dataclass
class PollReport:
    checked: int = 0
    finished: int = 0
    failed: int = 0


async def poll(ctx: Context) -> PollReport:
    """Update pending offline downloads from PikPak's task list (one request)."""
    report = PollReport()
    pending = await ctx.store.task_rows(phases=["PENDING", "RUNNING"], limit=500)
    pending = [row for row in pending if row["type"] == OFFLINE]
    if not pending:
        return report
    live = {str(task.get("id")): task for task in await ctx.client.offline_tasks(page_size=200)}
    for row in pending:
        report.checked += 1
        task = live.get(row["task_id"])
        if task is None:
            continue  # older than the page PikPak returned; checked again next time
        phase = _PHASES.get(str(task.get("phase")), "RUNNING")
        if phase == "DONE":
            report.finished += 1
            await ctx.store.finish_task(row["task_id"], phase="DONE",
                                        file_id=str(task.get("file_id") or "") or None)
        elif phase == "ERROR":
            report.failed += 1
            await ctx.store.finish_task(row["task_id"], phase="ERROR",
                                        error=str(task.get("message") or "")[:500])
    return report
