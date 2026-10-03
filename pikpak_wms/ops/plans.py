"""Plans: store them, show them, apply them, and undo what they did.

This is the one pipeline the command line, the Mini App panel and the bot
share (docs/wms/ARCHITECTURE.md §5):

    rules → Plan → saved as pending → confirmed → apply → audit → undo

Applying is resumable and idempotent (rule 5):

* the plan row keeps ``progress``, so a run cut short by the per-run cap,
  ``--limit`` or a rate-limit refusal continues where it stopped;
* every action is checked against the index first and skipped when it has
  already happened, or when the file changed since the plan was made;
* an applied or discarded plan cannot be applied again.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any

from ..core.errors import AuthError, NotFoundError, RateLimitedError, WmsError
from ..core.models import Action, ActionType, FileNode, Plan
from ..core.redact import redact
from ..i18n import t
from ..rules.actions import BATCH_LIMIT, DUE, GONE, PRIMITIVES, Deliver, Refused, Runtime
from ..rules.units import human_size
from ..store.db import now_iso
from . import downloads, protect, taskreq
from .context import Context
from .control import CANCELLED, DONE, FAILED, PAUSED, SKIPPED, Control

log = logging.getLogger(__name__)

PENDING, PARTIAL, APPLIED, DISCARDED = "pending", "partial", "applied", "discarded"
OPEN = [PENDING, PARTIAL]


def fingerprint(plan: Plan) -> str:
    body = json.dumps([a.to_dict() for a in plan.actions], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(body.encode()).hexdigest()


async def save(ctx: Context, plan: Plan) -> int | None:
    """Store a non-empty plan as pending; an identical pending plan is reused.

    The whitelist is applied here, to every plan whatever made it (M7 §1):
    ``plan`` loses its protected actions in place, so what is shown is what
    is stored.
    """
    protect.apply_to(plan, await protect.load(ctx))
    if plan.is_empty:
        return None
    mark = fingerprint(plan)
    existing = await ctx.store.open_plan_with(plan.source, mark)
    if existing is not None:
        return existing
    return await ctx.store.save_plan(plan, fingerprint=mark)


async def get(ctx: Context, plan_id: int) -> dict[str, Any]:
    row = await ctx.store.plan_row(plan_id)
    if row is None:
        raise NotFoundError(f"no plan {plan_id}", key="plan.not_found", id=plan_id)
    return row


async def listing(ctx: Context, *, open_only: bool = True, limit: int = 20) -> list[dict]:
    return await ctx.store.plan_rows(status=OPEN if open_only else None, limit=limit)


async def discard(ctx: Context, plan_id: int) -> None:
    row = await get(ctx, plan_id)
    if row["status"] not in OPEN:
        raise WmsError(f"plan {plan_id} is {row['status']}", key="plan.closed",
                       id=plan_id, status=row["status"])
    await ctx.store.update_plan(plan_id, status=DISCARDED, progress=row["progress"],
                                result=row["result"])


def name_of(action: Action) -> str:
    return str(action.before.get("name") or str(action.before.get("path") or "").rsplit("/", 1)[-1])


def _refresh_notes(plan: Plan) -> None:
    """What the plan says it matched, made true again after files were taken out."""
    names = [name_of(a) for a in plan.actions]
    size = sum(int(a.before.get("size") or 0) for a in plan.actions)
    for note in plan.notes:
        if note.get("key") == "nl.explain.matched":
            note["args"] = {**note["args"], "count": len(names), "size": human_size(size)}
        elif note.get("key") == "nl.explain.examples":
            note["args"] = {**note["args"], "names": names[:5]}


async def remove_matching(ctx: Context, plan_id: int, fragments: list[str]) -> list[str]:
    """Take the actions whose file name contains any of ``fragments`` out of an open
    plan (「abcd00123 下过了」, docs/wms/M9.2 §C.2). Only actions not yet carried out are
    touched. Returns the names removed; an emptied plan is discarded."""
    row = await get(ctx, plan_id)
    if row["status"] not in OPEN:
        raise WmsError(f"plan {plan_id} is {row['status']}", key="plan.closed",
                       id=plan_id, status=row["status"])
    plan: Plan = row["plan"]
    wanted = [f.casefold() for f in fragments if f.strip()]
    start = row["progress"]
    kept: list[Action] = []
    gone: list[str] = []
    renumber: dict[int, int] = {}
    for index, action in enumerate(plan.actions):
        name = name_of(action)
        if index >= start and any(fragment in name.casefold() for fragment in wanted):
            gone.append(name)
            continue
        renumber[index] = len(kept)
        kept.append(action)
    if not gone:
        return []
    result = dict(row["result"] or {})
    if result.get("settled"):
        result["settled"] = {str(renumber[int(k)]): v for k, v in result["settled"].items()
                             if int(k) in renumber}
    plan.actions = kept
    _refresh_notes(plan)
    await ctx.store.rewrite_plan(plan_id, plan, fingerprint=fingerprint(plan))
    await ctx.store.update_plan(plan_id, status=row["status"], progress=start, result=result)
    if len(kept) <= start:
        await ctx.store.update_plan(plan_id, status=DISCARDED, progress=start, result=result)
    return gone


async def supersede(ctx: Context, *, prefix: str, keep: set[int]) -> list[int]:
    """Discard the open plans of a job (``source`` starting with ``prefix``)
    that its newest run did not produce again: the index moved on, and an
    old plan left open would only invite applying a stale view."""
    dropped = []
    for row in await ctx.store.plan_rows(status=OPEN, limit=1000):
        if row["id"] in keep or not str(row["source"]).startswith(prefix):
            continue
        await ctx.store.update_plan(row["id"], status=DISCARDED, progress=row["progress"],
                                    result=row["result"])
        dropped.append(row["id"])
    return dropped


# ------------------------------------------------------------------ display


def plan_totals(plan: Plan) -> tuple[int, int]:
    """(distinct files touched, their total size)."""
    sizes: dict[str, int] = {}
    for action in plan.actions:
        if action.file_id:
            sizes[action.file_id] = int(action.before.get("size") or 0)
    return len(sizes), sum(sizes.values())


def plan_lines(plan: Plan, *, plan_id: int | None = None, limit: int = 30) -> list[str]:
    """A plan for a person: header, the first ``limit`` actions, then the notes."""
    files, size = plan_totals(plan)
    lines = [
        t("plan.header", id=plan_id if plan_id is not None else "-", source=plan.source,
          actions=len(plan.actions), files=files, size=human_size(size))
    ]
    if plan.is_empty:
        lines.append(t("plan.empty"))
    for action in plan.actions[:limit]:
        lines.append("  " + action.describe())
    if len(plan.actions) > limit:
        lines.append(t("plan.more", count=len(plan.actions) - limit))
    lines.extend("  · " + line for line in plan.note_lines())
    return lines


def describe_entry(entry: dict[str, Any]) -> str:
    """One audit row, described like a plan line."""
    try:
        kind = ActionType(entry["action"])
    except ValueError:
        return str(entry["action"])
    return Action(kind, entry["file_id"], before=entry["before"], after=entry["after"]).describe()


# ------------------------------------------------------------------ applying


@dataclass
class ApplyReport:
    plan_id: int | None
    applied: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    failed: list[dict[str, Any]] = field(default_factory=list)
    remaining: int = 0
    stopped: str = ""
    """Why the run stopped early (a rate-limit or login problem), else empty."""
    outputs: list[str] = field(default_factory=list)
    """Lines to show once and never store: direct links, share links."""
    audit_ids: list[int] = field(default_factory=list)
    fetches: list[dict[str, Any]] = field(default_factory=list)
    """How each downloaded file went: ``path``, ``avg_mib_s``, ``links``, ``peak_connections``."""
    cancelled: list[dict[str, Any]] = field(default_factory=list)
    """Files stopped by hand (docs/wms/M9.2 §D.5); their ``.part`` is kept."""
    gone: list[str] = field(default_factory=list)
    """Names of the files whose source had vanished from the drive (skipped as ``gone``)."""
    settled: dict[int, str] = field(default_factory=dict)
    """Downloads that finished ahead of an unfinished one: this run's action index → how
    it ended. Saved with the plan so that a resume does not do them again."""

    def summary(self) -> str:
        text = t(
            "apply.summary",
            id=self.plan_id if self.plan_id is not None else "-",
            applied=self.applied,
            skipped=sum(self.skipped.values()),
            failed=len(self.failed),
            remaining=self.remaining,
        )
        if self.cancelled:
            text += t("apply.summary_cancelled", cancelled=len(self.cancelled))
        if self.gone:
            shown = "、".join(self.gone[:5]) + ("…" if len(self.gone) > 5 else "")
            text += "\n" + t("apply.gone", names=shown)
        return text

    def to_result(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "skipped": dict(self.skipped),
            "failed": list(self.failed),
            "stopped": self.stopped,
            "audit_ids": list(self.audit_ids),
            "fetches": list(self.fetches),
            "cancelled": list(self.cancelled),
            "gone": list(self.gone),
        }


def _check_forever(ctx: Context, actions: list[Action], allow_forever: bool) -> None:
    if not any(a.type is ActionType.DELETE_FOREVER for a in actions):
        return
    if not (allow_forever and ctx.config.runtime.allow_permanent_delete):
        raise WmsError("permanent deletion is not enabled", key="forever.refused")


Checkpoint = Callable[[int, ApplyReport], Awaitable[None]]
"""Told after a batch of actions: how many of this run's actions are handled so far,
and the report so far. The runner keeps the plan's progress current with it."""

CHECKPOINT_SKIPS = 50
"""Actions that are only skipped do not each write the plan: every this many do."""


async def _record_done(
    ctx: Context, report: ApplyReport, item: Action, extra: dict[str, Any],
    output: Any, *, plan_id: int | None, undo_of: int | None = None,
) -> None:
    """One carried-out action: its outputs, its audit row, and whether it counts as
    done or as skipped (a download that was already on the NAS)."""
    if isinstance(extra.get("fetch"), dict):
        report.fetches.append({"path": item.before.get("path"), **extra["fetch"]})
    if output:
        report.outputs.append(str(output))
    if extra.get("share_url"):
        report.outputs.append(f"{item.before.get('path')}: {extra['share_url']}")
    audit_id = await ctx.store.record(
        replace(item, after={**item.after, **extra}),
        dry_run=False, plan_id=plan_id, undo_of=undo_of,
    )
    report.audit_ids.append(audit_id)
    if extra.get("skipped"):
        report.skipped["exists"] = report.skipped.get("exists", 0) + 1
    else:
        report.applied += 1


def _skip(report: ApplyReport, state: str, action: Action) -> None:
    """One action left alone: counted by why, and named when its source has vanished."""
    report.skipped[state] = report.skipped.get(state, 0) + 1
    if state == GONE:
        report.gone.append(name_of(action))


def _failure(item: Action, error: str, **more: Any) -> dict[str, Any]:
    return {"path": item.before.get("path") or item.after.get("path"),
            "action": str(item.type), "error": redact(error), "file_id": item.file_id, **more}


async def execute(
    ctx: Context,
    actions: list[Action],
    *,
    budget: int,
    plan_id: int | None = None,
    deliver: Deliver | None = None,
    undo_of: int | None = None,
    protection: protect.Protection | None = None,
    checkpoint: Checkpoint | None = None,
    control: Control | None = None,
    settled: dict[int, str] | None = None,
) -> tuple[int, ApplyReport]:
    """Carry out ``actions`` in order, at most ``budget`` of them.

    Returns how many were handled (applied, skipped or failed) and the report.
    With ``protection``, an action touching protected content is skipped
    (something may have been shared since the plan was made). With ``control``,
    a plan made only of downloads fetches several files at once and each file can
    be cancelled on its own (docs/wms/M9.2 §D).
    """
    if control is not None and undo_of is None and actions and all(
            a.type is ActionType.OUTBOUND for a in actions):
        return await _execute_outbound(
            ctx, actions, budget=budget, plan_id=plan_id, deliver=deliver,
            protection=protection, checkpoint=checkpoint, control=control,
            settled=settled or {})
    rt = Runtime(client=ctx.client, store=ctx.store, deliver=deliver)
    report = ApplyReport(plan_id=plan_id)
    failed_files: set[str] = set()
    index = handled = told = 0
    while index < len(actions) and handled < budget:
        if checkpoint is not None and handled > told and (
            report.applied or report.failed or handled - told >= CHECKPOINT_SKIPS
        ):
            told = handled
            await checkpoint(index, report)
        action = actions[index]
        primitive = PRIMITIVES[action.type]
        if action.file_id and action.file_id in failed_files:
            report.skipped["after_failure"] = report.skipped.get("after_failure", 0) + 1
            index += 1
            handled += 1
            continue
        state = await primitive.check(action, ctx.store)
        if state == DUE and protection is not None and protection.touches(action):
            state = "protected"
        if state != DUE:
            _skip(report, state, action)
            index += 1
            handled += 1
            continue

        batch = [action]
        key = primitive.batch_key(action)
        end = index + 1
        while key is not None and end < len(actions) and len(batch) < BATCH_LIMIT:
            if handled + len(batch) >= budget:
                break
            candidate = actions[end]
            other = PRIMITIVES[candidate.type]
            if other.batch_key(candidate) != key or candidate.file_id in failed_files:
                break
            if protection is not None and protection.touches(candidate):
                break
            if await other.check(candidate, ctx.store) != DUE:
                break
            batch.append(candidate)
            end += 1

        try:
            extras = await primitive.apply(batch, rt)
        except (RateLimitedError, AuthError) as exc:
            # Pushing on would only make it worse; stop and keep the place.
            report.stopped = exc.display()
            log.warning("apply stopped at action %d: %s", index, exc)
            break
        except WmsError as exc:
            for item in batch:
                report.failed.append(_failure(item, exc.display()))
                if item.file_id:
                    failed_files.add(item.file_id)
            index = end
            handled += len(batch)
            continue

        for item, extra in zip(batch, extras, strict=True):
            extra = dict(extra)
            output = extra.pop("_output", None)
            await _record_done(ctx, report, item, extra, output, plan_id=plan_id, undo_of=undo_of)
        index = end
        handled += len(batch)
    return index, report


async def _execute_outbound(
    ctx: Context, actions: list[Action], *, budget: int, plan_id: int | None,
    deliver: Deliver | None, protection: protect.Protection | None,
    checkpoint: Checkpoint | None, control: Control, settled: dict[int, str],
) -> tuple[int, ApplyReport]:
    """The downloads of a plan, several at once.

    Every action that is due becomes a task; the control's gate lets as many run
    as it currently allows (0: all), and the connection budget shared through
    :mod:`pikpak_wms.ops.fetch` decides how many connections they get. The plan's
    progress is the first action not yet handled, so a stop or a restart carries
    on from there; files finished beyond it are skipped as already present.
    """
    rt = Runtime(client=ctx.client, store=ctx.store, deliver=deliver)
    report = ApplyReport(plan_id=plan_id)
    todo = actions[:budget]
    primitive = PRIMITIVES[ActionType.OUTBOUND]
    handled: set[int] = set()
    halt: dict[str, str] = {}
    failed_files: set[str] = set()
    last: dict[str, asyncio.Task] = {}
    tasks: list[asyncio.Task] = []
    # What the tasks inherit: who the downloads are for, in the download log.
    downloads.current_plan.set(plan_id)

    def prefix() -> int:
        return next((i for i in range(len(todo)) if i not in handled), len(todo))

    async def told() -> None:
        if checkpoint is not None:
            edge = prefix()
            report.settled = {i: control.tracks[i].state for i in handled if i > edge}
            await checkpoint(edge, report)

    async def finish(i: int, state: str) -> None:
        control.tracks[i].state = state
        handled.add(i)
        await told()

    async def run_one(i: int, action: Action, before: asyncio.Task | None) -> None:
        track = control.tracks[i]
        item = action
        try:
            if before is not None:
                await asyncio.wait({before})
            front = False
            while True:
                try:
                    if track.paused:
                        # 已暂停: nothing is held, not a slot and not a connection.
                        await track.resume.wait()
                        track.paused, front = False, True  # a started file goes first
                    await control.gate.acquire(i, front=front, key=lambda: track.key)
                    try:
                        if halt or (action.file_id and action.file_id in failed_files):
                            if not halt:
                                report.skipped["after_failure"] = report.skipped.get(
                                    "after_failure", 0) + 1
                                await finish(i, SKIPPED)
                            return  # halted: not done, the next run takes it up
                        track.begin()
                        try:
                            (extra,) = await primitive.apply([action], rt)
                        except (RateLimitedError, AuthError) as exc:
                            halt["reason"] = exc.display()
                            track.state = "queued"
                            return
                        except WmsError as exc:
                            report.failed.append(_failure(item, exc.display()))
                            if action.file_id:
                                failed_files.add(action.file_id)
                            await finish(i, FAILED)
                            return
                        track.settling = True  # too late to pause or stop: it is done
                        extra = dict(extra)
                        output = extra.pop("_output", None)
                        await _record_done(ctx, report, item, extra, output, plan_id=plan_id)
                        await finish(i, SKIPPED if extra.get("skipped") else DONE)
                        return
                    finally:
                        control.gate.release()
                except asyncio.CancelledError:
                    if not track.pause_requested or track.cancel_requested:
                        raise
                    # 暂停: this file only. Its .part and .part.state stay for 开始.
                    track.pause_requested = False
                    track.paused, track.state, track.conns = True, PAUSED, 0
                    track.resume.clear()
                    task = asyncio.current_task()
                    if task is not None:
                        task.uncancel()
        except asyncio.CancelledError:
            if not track.cancel_requested:
                raise
            # 终止, this file only: the others go on. The .part stays unless asked.
            report.cancelled.append(_failure(item, "cancelled", cancelled=True))
            if not track.logged_cancel:
                track.logged_cancel = True
                node = await ctx.store.node(action.file_id) or FileNode.from_snapshot(
                    action.before)
                await asyncio.shield(downloads.log_attempt(
                    ctx, node, "cancelled", now_iso(), plan_id=plan_id, reason="cancelled"))
            await finish(i, CANCELLED)
            if track.delete_partial:
                control.delete_partial(i)

    try:
        for i, action in enumerate(todo):
            node = await ctx.store.node(action.file_id)
            name = (node.name if node else None) or str(
                action.before.get("name") or action.before.get("path") or "").rsplit("/", 1)[-1]
            size = int((node.size if node else 0) or action.before.get("size") or 0)
            control.add(i, action.file_id, name, size,
                        priority=int(action.after.get("priority", control.plan_priority)))
            if i in settled:
                # Done, failed or cancelled before the last stop: not done again.
                control.tracks[i].state = settled[i]
                handled.add(i)
                continue
            state = await primitive.check(action, ctx.store)
            if state == DUE and protection is not None and protection.touches(action):
                state = "protected"
            if state != DUE:
                _skip(report, state, action)
                control.tracks[i].state = SKIPPED
                handled.add(i)
                continue
            task = asyncio.create_task(run_one(i, action, last.get(action.file_id)),
                                       name=f"outbound-{plan_id}-{i}")
            control.tracks[i].task = task
            if action.file_id:
                last[action.file_id] = task
            tasks.append(task)
        if tasks:
            await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        with contextlib.suppress(Exception):
            await told()
        raise
    report.stopped = halt.get("reason", "")
    return prefix(), report


async def apply(
    ctx: Context,
    plan_id: int,
    *,
    limit: int | None = None,
    allow_forever: bool = False,
    deliver: Deliver | None = None,
    on_step: Callable[[int, int], Awaitable[None]] | None = None,
    control: Control | None = None,
) -> ApplyReport:
    """Apply a stored plan, or the next part of one (see the module docstring).

    The plan's progress is written as the run goes (not only at its end), so a
    run that is stopped, or cut off by a restart, resumes where it was.
    ``on_step(done, total)`` is told each time, for a progress display.
    """
    row = await get(ctx, plan_id)
    if row["status"] not in OPEN:
        raise WmsError(f"plan {plan_id} is {row['status']}", key="plan.closed",
                       id=plan_id, status=row["status"])
    plan: Plan = row["plan"]
    start = row["progress"]
    todo = plan.actions[start:]
    _check_forever(ctx, todo, allow_forever)

    budget = ctx.config.runtime.max_actions_per_run
    if limit is not None:
        budget = min(budget, max(limit, 0))
    previous = row["result"] or {}

    def merged(report: ApplyReport, index: int | None = None) -> dict[str, Any]:
        result = report.to_result()
        if index is not None and report.settled:
            result["settled"] = {str(start + i): state for i, state in report.settled.items()
                                 if i > index}
        result["applied"] += int(previous.get("applied", 0))
        for state, count in (previous.get("skipped") or {}).items():
            result["skipped"][state] = result["skipped"].get(state, 0) + count
        result["failed"] = list(previous.get("failed") or []) + result["failed"]
        result["cancelled"] = list(previous.get("cancelled") or []) + result["cancelled"]
        result["gone"] = list(previous.get("gone") or []) + result["gone"]
        result["audit_ids"] = list(previous.get("audit_ids") or []) + result["audit_ids"]
        return result

    async def checkpoint(index: int, report: ApplyReport) -> None:
        await ctx.store.update_plan(plan_id, status=PARTIAL, progress=start + index,
                                    result=merged(report, index))
        if on_step is not None:
            await on_step(start + index, len(plan.actions))

    if control is None and todo and all(a.type is ActionType.OUTBOUND for a in todo):
        control = Control(ctx.config.outbound.parallel_files)
    before = {int(k) - start: v for k, v in (previous.get("settled") or {}).items()
              if int(k) >= start}
    if control is not None:
        control.base, control.plan_priority, control.created = (
            start, plan.priority, str(row["created_at"]))
    # Whoever runs the plan also answers ``wms task`` requests for it (M9.3 §A.5).
    server = (asyncio.create_task(taskreq.serve(ctx, control, plan_id), name=f"taskreq-{plan_id}")
              if control is not None else None)
    try:
        handled, report = await execute(
            ctx, todo, budget=budget, plan_id=plan_id, deliver=deliver,
            protection=await protect.load(ctx), checkpoint=checkpoint, control=control,
            settled=before)
    finally:
        if server is not None:
            server.cancel()
            await asyncio.gather(server, return_exceptions=True)

    progress = start + handled
    report.remaining = len(plan.actions) - progress
    result = merged(report, handled)
    if on_step is not None:
        await on_step(progress, len(plan.actions))
    status = APPLIED if report.remaining == 0 else PARTIAL
    await ctx.store.update_plan(plan_id, status=status, progress=progress, result=result)
    log.info(
        "plan %d: %d applied, %d skipped, %d failed, %d remaining",
        plan_id, report.applied, sum(report.skipped.values()), len(report.failed),
        report.remaining,
    )
    return report


async def save_and_apply(
    ctx: Context,
    plan: Plan,
    *,
    limit: int | None = None,
    allow_forever: bool = False,
    deliver: Deliver | None = None,
) -> tuple[int | None, ApplyReport | None]:
    plan_id = await save(ctx, plan)
    if plan_id is None:
        return None, None
    report = await apply(ctx, plan_id, limit=limit, allow_forever=allow_forever,
                         deliver=deliver)
    return plan_id, report


# ------------------------------------------------------------------- undo


@dataclass
class UndoOutcome:
    audit_id: int
    action: Action
    applied: bool = False
    new_audit_id: int | None = None


async def undo(ctx: Context, audit_id: int, *, apply_now: bool) -> UndoOutcome:
    """Build (and with ``apply_now``, run) the action that reverses one audit entry."""
    entry = await ctx.store.audit_entry(audit_id)
    if entry is None:
        raise NotFoundError(f"no audit entry {audit_id}", key="undo.no_entry", id=audit_id)
    if entry["dry_run"]:
        raise Refused("a dry-run entry changed nothing", key="undo.refused.dry_run", id=audit_id)
    later = await ctx.store.undone_by(audit_id)
    if later is not None:
        raise Refused(f"already undone by {later}", key="undo.refused.already",
                      id=audit_id, by=later)
    try:
        kind = ActionType(entry["action"])
    except ValueError as exc:
        raise Refused(f"unknown action {entry['action']}", key="undo.refused.generic",
                      action=entry["action"]) from exc
    inverse = PRIMITIVES[kind].inverse(entry)
    if kind is ActionType.CREATE_FOLDER and await ctx.store.children(entry["file_id"]):
        # Trashing it would take whatever was put there since; undo that first.
        raise Refused("the folder is not empty", key="undo.refused.not_empty",
                      path=entry["after"].get("path"))
    state = await PRIMITIVES[inverse.type].check(inverse, ctx.store)
    if state != DUE:
        raise Refused(f"the file changed since audit entry {audit_id} ({state})",
                      key="undo.refused.changed", id=audit_id, state=state)
    outcome = UndoOutcome(audit_id=audit_id, action=inverse)
    if not apply_now:
        return outcome
    _, report = await execute(ctx, [inverse], budget=1, undo_of=audit_id)
    if report.stopped or report.failed:
        reason = report.stopped or report.failed[0]["error"]
        raise WmsError(f"undo failed: {reason}", key="undo.failed", error=reason)
    outcome.applied = True
    outcome.new_audit_id = report.audit_ids[0] if report.audit_ids else None
    return outcome
