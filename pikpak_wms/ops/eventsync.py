"""Keep the index current from PikPak's event feed (docs/wms/M8.1 §A).

The incremental stocktake skips a folder whose ``modified_time`` has not
changed. PikPak does not touch that time when something is restored into an
existing folder, so a file transferred in today was not in the index until
the nightly full run. The event feed (``GET /drive/v1/events``) lists what
happened, so this reads it instead:

1. Read the feed from its newest end, page by page, until an event already
   handled turns up. What was not seen before is new.
2. For each file those events name, ask PikPak for it as it is *now*. Still
   there: write it to the index (a folder: list its subtree). Gone or
   trashed: drop it. The event's type name is never consulted, so a type
   this code has not heard of still reaches the index; the current state of
   the file is what counts, not what the event says happened to it.
3. Remember which events were handled (the cursor, in the meta table).

Anything that stops the feed from proving "nothing was missed" falls back:
no cursor yet, a cursor that has fallen off the end of what the feed still
holds, a feed that errors, too many events to follow one by one, an event
feed that does not look newest-first, or events that name no file at all. The
fallback is a full stocktake for a lost cursor and the old incremental one
otherwise, and the report says which and why, so a caller can tell a person.

Written without a real sample of the feed (the brief asks for one first;
this environment cannot reach an account): the field names below are the
likeliest, every one is optional, and a feed that does not fit them fails
safe into the fallback instead of into silent gaps. See docs/HANDOFF.md.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, replace
from typing import Any

from ..core.client import WmsClient
from ..core.errors import AuthError, RateLimitedError, WmsError
from ..core.models import ROOT_ID, FileNode, parse_time
from ..i18n import t
from ..store.db import Store, now_iso
from .context import Context
from .stocktake import StocktakeReport, stocktake, sync_tree

log = logging.getLogger(__name__)

CURSOR_KEY = "events_cursor"
SYNC_KEY = "last_sync"
ALERT_KEY = "events_last_alert"

KEEP_IDS = 400
"""Handled event ids remembered: enough to recognise the end of what is new."""

MAX_PAGES = 10
"""Pages of 100 read per sync. More new events than that, and the cursor is lost."""

MAX_FILES = 300
"""Distinct files followed one by one per sync."""

ALERT_EVERY = 24 * 3600
"""The same fallback reason is told to the admins at most this often."""

PHASE_DONE = "PHASE_TYPE_COMPLETE"
_ANCESTOR_DEPTH = 24


@dataclass
class EventSyncReport:
    events: int = 0
    """Events not handled before."""
    files: int = 0
    """Distinct files they named."""
    upserted: int = 0
    removed: int = 0
    folders_listed: int = 0
    unreadable: int = 0
    """Events that named no file."""
    requests: int = 0
    seconds: float = 0.0
    fallback: str = ""
    """"" | "incremental" | "full": what to run because events could not be trusted."""
    reason: str = ""
    """Why, in English, for logs; a person gets :meth:`alert`."""
    baseline: bool = False
    """First run: the cursor was only set."""
    failed: bool = False
    """The feed itself errored; the cursor was left as it was."""
    at: str = ""

    def summary(self) -> str:
        if self.fallback:
            return t("events.fallback", reason=self.reason)
        return t("events.sync", events=self.events, files=self.files, upserted=self.upserted,
                 removed=self.removed, folders=self.folders_listed, requests=self.requests,
                 seconds=f"{self.seconds:.1f}")


@dataclass
class IndexSync:
    """What :func:`refresh_index` did, for a plan line or a job summary."""

    kind: str
    """``events``, ``incremental`` or ``full``."""
    at: str
    events: EventSyncReport | None = None
    stocktake: StocktakeReport | None = None
    alert: str = ""
    """Set when a person should hear of it (a lost cursor)."""

    def summary(self) -> str:
        parts = [part.summary() for part in (self.events, self.stocktake) if part is not None]
        return "\n".join(parts)


# ------------------------------------------------------------- reading events


def _file_id(event: dict[str, Any]) -> str:
    for key in ("file_id", "reference_id"):
        value = event.get(key)
        if value:
            return str(value)
    for key in ("reference_resource", "resource", "file"):
        inner = event.get(key)
        if isinstance(inner, dict) and (inner.get("id") or inner.get("file_id")):
            return str(inner.get("id") or inner.get("file_id"))
    return ""


def _event_id(event: dict[str, Any]) -> str:
    if event.get("id"):
        return str(event["id"])
    return f"{event.get('type')}|{_file_id(event)}|{event.get('created_time')}"


def _oldest_first(events: list[dict[str, Any]]) -> bool:
    """True when the page's times climb: a feed that starts at the dawn of
    time, which reading from the head would never get to the end of."""
    times = [parse_time(str(e.get("created_time") or "")) for e in events]
    times = [when for when in times if when is not None]
    return len(times) >= 2 and times[0] < times[-1]


async def _cursor(store: Store) -> list[str] | None:
    raw = await store.get_meta(CURSOR_KEY)
    if raw is None:
        return None
    try:
        ids = json.loads(raw).get("ids")
    except (ValueError, AttributeError):
        return None
    return [str(i) for i in ids] if isinstance(ids, list) else None


async def _save_cursor(store: Store, ids: list[str], at: str) -> None:
    await store.set_meta(CURSOR_KEY, json.dumps({"ids": ids[:KEEP_IDS], "at": at}))


def _short(exc: Exception, limit: int = 120) -> str:
    text = " ".join(str(exc).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def _read_new(
    client: WmsClient, seen: set[str], page_size: int, max_pages: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str, str]:
    """(the head page, every event not in ``seen``, why the cursor was lost or "",
    why the feed failed or "").

    A first page that errors is a failed feed, not a lost cursor: the cursor
    may well still be good. Only an :class:`AuthError` is raised.
    """
    head: list[dict[str, Any]] = []
    fresh: list[dict[str, Any]] = []
    token: str | None = None
    for page_number in range(max_pages):
        try:
            page = await client.events(page_size=page_size, token=token)
        except AuthError:
            if token is None:
                raise
            return head, fresh, "the feed refused its next page", ""
        except RateLimitedError:
            if token is None:
                return [], [], "", "rate limited"
            return head, fresh, "the feed refused its next page", ""
        except WmsError as exc:
            if token is None:
                return [], [], "", _short(exc)
            return head, fresh, f"the feed refused its next page ({exc})", ""
        events = [e for e in (page or {}).get("events") or [] if isinstance(e, dict)]
        if page_number == 0:
            head = events
        new = [e for e in events if _event_id(e) not in seen]
        fresh.extend(new)
        token = (page or {}).get("next_page_token") or None
        if len(new) < len(events) or not token:
            return head, fresh, "", ""
    return head, fresh, f"more than {max_pages * page_size} events since the last sync", ""


# ------------------------------------------------------------- applying events


async def _apply(
    client: WmsClient, store: Store, file_id: str, *, page_size: int, synced_at: str,
    covered: set[str], report: EventSyncReport,
) -> None:
    """Make the index agree with PikPak about one file."""
    if file_id in covered:
        return  # an ancestor's listing already brought it up to date
    info = await client.file_info(file_id)
    if info is None or info.get("trashed") or (
        info.get("phase") and info["phase"] != PHASE_DONE
    ):
        if await store.node(file_id) is not None:
            await store.forget([file_id])
            report.removed += 1
        return

    # Walk up to the first ancestor the index knows; anything above it that it
    # does not is new, and is listed from its topmost unknown folder.
    top = info
    parent_path = "/"
    for _ in range(_ANCESTOR_DEPTH):
        parent_id = str(top.get("parent_id") or ROOT_ID)
        if parent_id == ROOT_ID:
            parent_path = "/"
            break
        parent = await store.node(parent_id)
        if parent is not None:
            parent_path = parent.path
            break
        above = await client.file_info(parent_id)
        if above is None or above.get("trashed"):
            # Inside something trashed or gone: it is not part of the drive.
            if await store.node(file_id) is not None:
                await store.forget([file_id])
                report.removed += 1
            return
        top = above
    else:
        log.info("event sync: %s is nested too deep to place; left to the next full run", file_id)
        return

    node = FileNode.from_api(top, parent_path=parent_path)
    node.parent_id = str(top.get("parent_id") or ROOT_ID)
    before = await store.node(node.file_id)
    if before is not None and before.path != node.path:
        await store.relocate(node.file_id, parent_id=node.parent_id, path=node.path)
    if not node.is_folder:
        await store.insert(node, synced_at=synced_at)
        report.upserted += 1
        return
    if before is not None and before.path == node.path \
            and before.modified_time == node.modified_time:
        return  # a folder that has not changed; its contents have events of their own
    await store.insert(replace(node, modified_time=None), synced_at=synced_at)
    listing = StocktakeReport(full=False)
    await sync_tree(client, store, node.file_id, node.path, full=False, page_size=page_size,
                    synced_at=synced_at, report=listing, live_modified=node.modified_time,
                    seen=covered)
    report.folders_listed += listing.folders_listed
    report.upserted += 1


async def sync_events(
    client: WmsClient, store: Store, *, page_size: int = 100, max_pages: int = MAX_PAGES,
    max_files: int = MAX_FILES,
) -> EventSyncReport:
    """Bring the index up to date from the event feed, or say why it cannot."""
    report = EventSyncReport()
    started = time.monotonic()
    requests_before = client.calls
    report.at = synced_at = now_iso()

    remembered = await _cursor(store)
    seen = set(remembered or [])
    head, fresh, lost, failed = await _read_new(client, seen, page_size, max_pages)

    def done() -> EventSyncReport:
        report.seconds = time.monotonic() - started
        report.requests = client.calls - requests_before
        return report

    async def rebase(reason: str, fallback: str) -> EventSyncReport:
        # Start over from the head of the feed; the fallback stocktake makes
        # the index itself whole.
        report.fallback, report.reason = fallback, reason
        await _save_cursor(store, [_event_id(e) for e in head], synced_at)
        return done()

    if failed:
        # Not a lost cursor: keep it, so the next good sync resumes from it.
        report.failed = True
        report.fallback = "incremental"
        report.reason = f"the event feed failed: {failed}"
        return done()
    if remembered is None:
        report.baseline = True
        return await rebase("no cursor yet (first run)", "incremental")
    if lost:
        return await rebase(f"the cursor is lost: {lost}", "full")
    if _oldest_first(head):
        # Reading from the head would never reach what is new.
        report.fallback = "incremental"
        report.reason = "the event feed lists oldest first, which this sync does not follow"
        return done()

    report.events = len(fresh)
    ids: list[str] = []
    for event in reversed(fresh):  # oldest first
        file_id = _file_id(event)
        if not file_id:
            report.unreadable += 1
        elif file_id not in ids:
            ids.append(file_id)
    if fresh and report.unreadable == len(fresh):
        report.fallback = "incremental"
        report.reason = "the new events name no file (their fields are not the ones expected)"
        return done()
    report.files = len(ids)
    if len(ids) > max_files:
        return await rebase(f"the cursor is lost: {len(ids)} files changed since the last sync",
                            "full")

    covered: set[str] = set()
    for file_id in ids:
        await _apply(client, store, file_id, page_size=page_size, synced_at=synced_at,
                     covered=covered, report=report)
    await _save_cursor(store, [_event_id(e) for e in fresh] + list(remembered), synced_at)
    return done()


# ------------------------------------------------------- the index, refreshed


async def _alert_once(store: Store, reason: str) -> str:
    """The text for the admins, unless the same reason was told recently."""
    now = time.time()
    try:
        last = json.loads(await store.get_meta(ALERT_KEY) or "{}")
    except ValueError:
        last = {}
    if last.get("reason") == reason and now - float(last.get("at") or 0) < ALERT_EVERY:
        return ""
    await store.set_meta(ALERT_KEY, json.dumps({"reason": reason, "at": now}))
    return t("events.alert", reason=reason)


async def refresh_index(ctx: Context, *, allow_full: bool = True,
                        roots: list[str] | None = None) -> IndexSync:
    """The index, current. Events first; a stocktake only when events cannot be trusted.

    ``allow_full`` is for callers that can wait (a scheduled job): a lost
    cursor then means a full stocktake. A person waiting on an answer gets
    the incremental one instead, and the daily full run (or the next
    scheduled sync) heals the rest.
    """
    cfg = ctx.config.stocktake
    wanted = roots or cfg.roots
    if not cfg.events:
        report = await stocktake(ctx.client, ctx.store, roots=wanted, full=False,
                                 page_size=cfg.page_size)
        result = IndexSync("incremental", now_iso(), stocktake=report)
    else:
        events = await sync_events(ctx.client, ctx.store, page_size=cfg.page_size)
        if not events.fallback:
            result = IndexSync("events", events.at, events=events)
        else:
            full = events.fallback == "full" and allow_full
            report = await stocktake(ctx.client, ctx.store, roots=cfg.roots if full else wanted,
                                     full=full, page_size=cfg.page_size)
            result = IndexSync("full" if full else "incremental", now_iso(),
                               events=events, stocktake=report)
            if not events.baseline:
                log.warning("event sync fell back to a %s stocktake: %s", result.kind,
                            events.reason)
                result.alert = await _alert_once(ctx.store, events.reason)
    await ctx.store.set_meta("last_stocktake", result.at)
    state = {"at": result.at, "kind": result.kind}
    if result.events is not None and result.events.failed:
        state["failed"] = result.events.reason
    await ctx.store.set_meta(SYNC_KEY, json.dumps(state))
    return result


async def freshness(ctx: Context) -> tuple[str, str] | None:
    """(local HH:MM, kind) of the last time the index was brought up to date."""
    raw = await ctx.store.get_meta(SYNC_KEY)
    try:
        data = json.loads(raw) if raw else None
    except ValueError:
        data = None
    if not data:
        at, kind = await ctx.store.get_meta("last_stocktake"), "incremental"
    else:
        at, kind = data.get("at"), data.get("kind") or "incremental"
    when = parse_time(at)
    if when is None:
        return None
    return when.astimezone(ctx.config.schedule.tz).strftime("%H:%M"), kind


async def failure(ctx: Context) -> str:
    """Why the last sync could not use the event feed, or "" when it could."""
    try:
        data = json.loads(await ctx.store.get_meta(SYNC_KEY) or "{}")
    except ValueError:
        return ""
    return str(data.get("failed") or "") if isinstance(data, dict) else ""


async def note_freshness(ctx: Context, plans: list[Any]) -> None:
    """Put "Index updated at 14:05 (event sync)" on each plan (M8.1 §B): a
    plan made from the entry folders is only as good as the index behind it."""
    found = await freshness(ctx)
    if found is None:
        return
    clock, kind = found
    reason = await failure(ctx)
    for plan in plans:
        plan.note("sync.updated", time=clock, kind={"key": f"sync.kind.{kind}", "args": {}})
        if reason:
            plan.note("sync.failed", reason=reason)
