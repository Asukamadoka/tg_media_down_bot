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

import hashlib
import json
import logging
from dataclasses import dataclass, field, replace
from typing import Any

from ..core.errors import AuthError, NotFoundError, RateLimitedError, WmsError
from ..core.models import Action, ActionType, Plan
from ..i18n import t
from ..rules.actions import BATCH_LIMIT, DUE, PRIMITIVES, Deliver, Refused, Runtime
from ..rules.units import human_size
from . import protect
from .context import Context

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

    def summary(self) -> str:
        return t(
            "apply.summary",
            id=self.plan_id if self.plan_id is not None else "-",
            applied=self.applied,
            skipped=sum(self.skipped.values()),
            failed=len(self.failed),
            remaining=self.remaining,
        )

    def to_result(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "skipped": dict(self.skipped),
            "failed": list(self.failed),
            "stopped": self.stopped,
            "audit_ids": list(self.audit_ids),
        }


def _check_forever(ctx: Context, actions: list[Action], allow_forever: bool) -> None:
    if not any(a.type is ActionType.DELETE_FOREVER for a in actions):
        return
    if not (allow_forever and ctx.config.runtime.allow_permanent_delete):
        raise WmsError("permanent deletion is not enabled", key="forever.refused")


async def execute(
    ctx: Context,
    actions: list[Action],
    *,
    budget: int,
    plan_id: int | None = None,
    deliver: Deliver | None = None,
    undo_of: int | None = None,
    protection: protect.Protection | None = None,
) -> tuple[int, ApplyReport]:
    """Carry out ``actions`` in order, at most ``budget`` of them.

    Returns how many were handled (applied, skipped or failed) and the report.
    With ``protection``, an action touching protected content is skipped
    (something may have been shared since the plan was made).
    """
    rt = Runtime(client=ctx.client, store=ctx.store, deliver=deliver)
    report = ApplyReport(plan_id=plan_id)
    failed_files: set[str] = set()
    index = handled = 0
    while index < len(actions) and handled < budget:
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
            report.skipped[state] = report.skipped.get(state, 0) + 1
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
                report.failed.append(
                    {"path": item.before.get("path") or item.after.get("path"),
                     "action": str(item.type), "error": exc.display()}
                )
                if item.file_id:
                    failed_files.add(item.file_id)
            index = end
            handled += len(batch)
            continue

        for item, extra in zip(batch, extras, strict=True):
            extra = dict(extra)
            output = extra.pop("_output", None)
            if output:
                report.outputs.append(str(output))
            if extra.get("share_url"):
                report.outputs.append(f"{item.before.get('path')}: {extra['share_url']}")
            audit_id = await ctx.store.record(
                replace(item, after={**item.after, **extra}),
                dry_run=False, plan_id=plan_id, undo_of=undo_of,
            )
            report.audit_ids.append(audit_id)
        report.applied += len(batch)
        index = end
        handled += len(batch)
    return index, report


async def apply(
    ctx: Context,
    plan_id: int,
    *,
    limit: int | None = None,
    allow_forever: bool = False,
    deliver: Deliver | None = None,
) -> ApplyReport:
    """Apply a stored plan, or the next part of one (see the module docstring)."""
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
    handled, report = await execute(ctx, todo, budget=budget, plan_id=plan_id, deliver=deliver,
                                    protection=await protect.load(ctx))

    progress = start + handled
    report.remaining = len(plan.actions) - progress
    previous = row["result"] or {}
    result = report.to_result()
    result["applied"] += int(previous.get("applied", 0))
    for state, count in (previous.get("skipped") or {}).items():
        result["skipped"][state] = result["skipped"].get(state, 0) + count
    result["failed"] = list(previous.get("failed") or []) + result["failed"]
    result["audit_ids"] = list(previous.get("audit_ids") or []) + result["audit_ids"]
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
