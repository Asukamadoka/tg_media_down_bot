"""Controlling the tasks of a plan from outside the process that runs it (docs/wms/M9.3 §A.5).

``wms task pause|start|stop <plan>:<n>`` leaves a row in the request table
(:mod:`pikpak_wms.core.requests`); whichever process is running that plan (the bot, or a
``wms apply`` in a terminal) looks at the table every few seconds, does what was asked
and writes the answer back. While it runs it also leaves a snapshot of its tasks under
``tasks:<plan>`` in ``meta``, which is what ``wms tasks`` shows.

``<n>`` is the action's place in the plan counted from 1; ``<plan>:all`` is every task.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from ..core.errors import WmsError
from .context import Context
from .control import ACTIVE, Control

log = logging.getLogger(__name__)

KIND = "task"
EVERY = 5.0
"""Seconds between looks at the request table (and between snapshots)."""
SNAPSHOT = "tasks:"
FRESH = 30.0
"""A snapshot older than this is from a process that has gone."""
VERBS = ("pause", "start", "stop")


def snapshot(control: Control, plan_id: int) -> dict[str, Any]:
    return {
        "plan": plan_id, "at": time.time(), "limit": control.limit,
        "tasks": [
            {"n": t.index + 1, "name": t.name, "state": t.state, "size": t.size,
             "received": t.received, "mib_s": round(t.speed / (1024 * 1024), 2) if
             t.state == ACTIVE else 0.0}
            for t in sorted(control.tracks.values(), key=lambda t: t.index)],
    }


async def publish(ctx: Context, control: Control, plan_id: int) -> None:
    await ctx.store.set_meta(f"{SNAPSHOT}{plan_id}", json.dumps(snapshot(control, plan_id)))


async def withdraw(ctx: Context, plan_id: int) -> None:
    await ctx.store.delete_meta(f"{SNAPSHOT}{plan_id}")


def _do(control: Control, plan_id: int, number: str, arg: str) -> str:
    verb, _, extra = arg.partition("+")
    if verb not in VERBS:
        return "refused: unknown request"
    if number == "all":
        if verb == "pause":
            return f"ok: {control.pause_all()}"
        if verb == "start":
            return f"ok: {control.start_all()}"
        return "refused: stop needs one task"
    try:
        index = int(number) - 1
    except ValueError:
        return "refused: not a task number"
    track = control.tracks.get(index)
    if track is None:
        return "refused: no such task"
    state = track.state
    if control.handle(verb, index, delete_partial=extra == "delete"):
        return "ok"
    return f"refused: the task is {state}"


async def serve_once(ctx: Context, control: Control, plan_id: int) -> int:
    """Take the requests for this plan's tasks and carry them out; returns how many."""
    targets = {f"{plan_id}:all", *(f"{plan_id}:{i + 1}" for i in control.tracks)}
    rows = await ctx.store.take_requests(KIND, targets)
    for rid, target, arg in rows:
        result = _do(control, plan_id, target.partition(":")[2], arg)
        await ctx.store.finish_request(rid, result)
        log.info("task request %s %s: %s", target, arg, result)
    if rows:
        await publish(ctx, control, plan_id)
    return len(rows)


async def serve(ctx: Context, control: Control, plan_id: int, every: float | None = None) -> None:
    """Run beside a plan until cancelled."""
    try:
        await publish(ctx, control, plan_id)
        while True:
            await asyncio.sleep(EVERY if every is None else every)
            try:
                await serve_once(ctx, control, plan_id)
                await publish(ctx, control, plan_id)
            except Exception as exc:  # noqa: BLE001 - controls failing must not stop the downloads
                log.warning("could not serve task requests for plan %d: %s", plan_id, exc)
    finally:
        try:
            await asyncio.shield(withdraw(ctx, plan_id))
        except Exception:  # noqa: BLE001
            log.debug("could not clear the task snapshot of plan %d", plan_id)


# ------------------------------------------------------------ the asking side


def parse_target(text: str) -> tuple[int, str]:
    """``66:3`` → ``(66, "3")``; ``66:all``."""
    plan, _, number = text.partition(":")
    if not plan.isdigit() or not (number == "all" or number.isdigit()) or number == "0":
        raise WmsError(f"not a task: {text}", key="task.bad_target", target=text)
    return int(plan), number


async def request(ctx: Context, verb: str, target: str, *, delete_partial: bool = False,
                  wait: float = 0.0, step: float = 0.5) -> tuple[bool, str]:
    """Post a request; with ``wait`` seconds, wait for the process running the plan to
    answer. Returns ``(answered, result)``."""
    if verb not in VERBS:
        raise WmsError(f"unknown verb {verb}", key="task.bad_verb", verb=verb)
    plan_id, number = parse_target(target)
    arg = "stop+delete" if verb == "stop" and delete_partial else verb
    rid = await ctx.store.post_request(KIND, f"{plan_id}:{number}", arg)
    waited = 0.0
    while True:
        done, result = await ctx.store.request_outcome(rid)
        if done or waited >= wait:
            return done, result
        await asyncio.sleep(step)
        waited += step


async def snapshots(ctx: Context, plan_id: int | None = None) -> list[dict[str, Any]]:
    """The tasks every running process reported lately (those of ``plan_id`` only, when given)."""
    out = []
    for _key, value in sorted((await ctx.store.meta_with_prefix(SNAPSHOT)).items()):
        try:
            data = json.loads(value)
        except ValueError:
            continue
        if plan_id is not None and data.get("plan") != plan_id:
            continue
        if time.time() - float(data.get("at") or 0) <= FRESH:
            out.append(data)
    return out


async def listing(ctx: Context, plan_id: int | None = None) -> list[dict[str, Any]]:
    """The tasks of every plan that is running (from its own report), or, for ``plan_id``
    when nothing runs it, as the plan row last saved them: what finished ahead of the
    first unfinished action is ``done``, the rest ``queued``."""
    found = await snapshots(ctx, plan_id)
    out = [{**snap, "live": True} for snap in found]
    if plan_id is not None and not out:
        from . import plans  # taskreq is imported by plans

        row = await plans.get(ctx, plan_id)
        settled = {int(k): v for k, v in ((row["result"] or {}).get("settled") or {}).items()}
        tasks = []
        for i, action in enumerate(row["plan"].actions):
            if action.type.value != "outbound":
                continue
            state = "done" if i < row["progress"] else settled.get(i, "queued")
            tasks.append({"n": i + 1, "name": str(action.before.get("name") or action.before.get(
                "path") or "").rsplit("/", 1)[-1], "state": state,
                "size": int(action.before.get("size") or 0), "received": 0, "mib_s": 0.0})
        if tasks:
            out.append({"plan": plan_id, "limit": None, "tasks": tasks, "live": False})
    return out
