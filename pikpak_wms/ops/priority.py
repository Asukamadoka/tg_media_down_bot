"""Download priority (docs/wms/M9.4): 0 normal, 1 high, 2 top (置顶).

A task's own priority is kept in its action (``after.priority``); a plan's is in the plan
body (``Plan.priority``) and is the default for every task without one of its own. Both are
stored with the plan, so they survive a restart and a retry plan made from the actions.
Setting a plan's priority clears the tasks' own, so 「整组优先」 means the whole group.
"""

from __future__ import annotations

from ..core.errors import WmsError
from ..core.models import Action, ActionType, Plan
from . import plans
from .context import Context

LEVELS = {"normal": 0, "high": 1, "top": 2}
NAMES = {value: name for name, value in LEVELS.items()}
WEIGHTS = {0: 1, 1: 2, 2: 4}
"""Connection grants: normal 1 : high 2 : top 4."""


def parse(text: str) -> int:
    try:
        return LEVELS[text.strip().lower()]
    except KeyError:
        raise WmsError(f"priority must be normal, high or top, not {text}",
                       key="priority.bad_level", level=text) from None


def effective(plan: Plan, action: Action) -> int:
    value = action.after.get("priority")
    return plan.priority if value is None else int(value)


def next_level(level: int) -> int:
    """The 优先 button: normal → high → top → normal."""
    return (level + 1) % 3


async def set_task(ctx: Context, plan_id: int, index: int, level: int) -> None:
    """Keep ``level`` for the action at ``index`` of the plan (its place in the plan)."""
    row = await plans.get(ctx, plan_id)
    plan: Plan = row["plan"]
    if not 0 <= index < len(plan.actions):
        raise WmsError(f"plan {plan_id} has no task {index + 1}", key="priority.no_task",
                       id=plan_id, n=index + 1)
    plan.actions[index].after["priority"] = level
    await ctx.store.rewrite_plan(plan_id, plan, fingerprint=plans.fingerprint(plan))


async def set_plan(ctx: Context, plan_id: int, level: int) -> None:
    """The whole group: the plan's priority, and no task keeps a different one."""
    row = await plans.get(ctx, plan_id)
    plan: Plan = row["plan"]
    plan.priority = level
    for action in plan.actions:
        action.after.pop("priority", None)
    await ctx.store.rewrite_plan(plan_id, plan, fingerprint=plans.fingerprint(plan))


async def set_named(ctx: Context, fragments: list[str], level: int) -> list[str]:
    """「先下 X」: every download of an open plan whose file name contains a fragment gets
    ``level``. Returns the names that matched (none: nothing queued or running has it)."""
    wanted = [f.casefold() for f in fragments if f.strip()]
    found: list[str] = []
    for summary in await plans.listing(ctx, open_only=True, limit=500):
        row = await plans.get(ctx, summary["id"])
        plan: Plan = row["plan"]
        changed = False
        for index, action in enumerate(plan.actions):
            if action.type is not ActionType.OUTBOUND or index < row["progress"]:
                continue
            name = plans.name_of(action)
            if any(fragment in name.casefold() for fragment in wanted):
                action.after["priority"] = level
                changed = True
                found.append(name)
        if changed:
            await ctx.store.rewrite_plan(summary["id"], plan, fingerprint=plans.fingerprint(plan))
    return found
