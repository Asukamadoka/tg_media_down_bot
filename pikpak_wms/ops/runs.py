"""Plans that run in the background, and can be stopped (docs/wms/M8.3 §G).

Confirming a plan used to run it inside the button handler: a 2.9 GiB download
kept the message unchanged for forty minutes and invited a second press. Now
confirming only *starts* a run; this module keeps its state.

* **One run per plan.** The check is in memory (a double press in the same
  second) and in the database (``meta`` key ``running:<id>``, which survives a
  restart as a *stale* mark).
* **Progress** comes from the plan itself (``on_step``: actions done) and from
  the download (``progress``: bytes of the file being fetched); a display reads
  :class:`Run`, it is never pushed.
* **Stop** cancels the task. What was done stays done (and audited, action by
  action); a ``.part`` file stays for the next run; the plan is back to
  ``pending`` with its progress kept, and ``stopped:<id>`` says where.
* **A restart** leaves ``running:<id>`` marks behind. :meth:`Runs.recover` turns
  them into ``interrupted:<id>`` for the bot to announce, with a button to
  continue.

The plan table's status column has a fixed set of values (no schema change
since M1), so "running", "interrupted" and "stopped" live in ``meta``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..core.errors import WmsError
from ..core.models import ActionType
from ..rules.actions import Deliver
from . import downloads, outbound, plans, priority
from .context import Context
from .control import QUEUED, Control, FileTrack, SpeedMeter

log = logging.getLogger(__name__)

RUNNING, INTERRUPTED, STOPPED = "running:", "interrupted:", "stopped:"
PARALLEL, PARALLEL_DEFAULT = "parallel:", "outbound:parallel"
"""``meta`` keys: files at once for one plan, and the default for all (M9.2 §D.1)."""


@dataclass
class Run:
    plan_id: int
    total: int
    done: int
    started: float = field(default_factory=time.monotonic)
    file: str = ""
    received: int = 0
    size: int = 0
    task: asyncio.Task | None = None
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    report: plans.ApplyReport | None = None
    error: str = ""
    stopped: bool = False
    shutdown: bool = False
    """Stopped because the process is going away: left marked ``running`` so that
    the next start reports it as interrupted."""
    meter: SpeedMeter = field(default_factory=SpeedMeter)
    conns: int = 0
    """Connections open for the file being fetched, and the links they use."""
    links: str = ""
    file_started: float = field(default_factory=time.monotonic)
    control: Control = field(default_factory=Control)
    """The plan's files, one by one, and the dial for how many run at once."""

    def track_for(self, file_id: str) -> FileTrack | None:
        return self.control.track_for(file_id)

    def note_info(self, conns: int, links: str) -> None:
        self.conns, self.links = conns, links

    @property
    def average(self) -> float:
        """Bytes per second of the file so far, from its first byte."""
        span = time.monotonic() - self.file_started
        return self.received / span if span > 0 else 0.0

    def note_bytes(self, name: str, received: int, size: int) -> None:
        if name != self.file:
            self.file = name
            self.meter.clear()
            self.file_started, self.conns, self.links = time.monotonic(), 0, ""
        self.received, self.size = received, size
        self.meter.add(received)

    @property
    def speed(self) -> float:
        """Bytes per second of the file being fetched, over the last few seconds."""
        return self.meter.speed

    @property
    def eta(self) -> float | None:
        speed = self.speed
        return (self.size - self.received) / speed if speed > 0 and self.size else None

    @property
    def fraction(self) -> float | None:
        return self.received / self.size if self.size else None

    @property
    def active(self) -> bool:
        return not self.finished.is_set()


class Runs:
    def __init__(self, ctx: Context, lock: asyncio.Lock) -> None:
        self.ctx = ctx
        self.lock = lock
        self._runs: dict[int, Run] = {}

    # ------------------------------------------------------------ lookups

    def get(self, plan_id: int) -> Run | None:
        return self._runs.get(plan_id)

    def active(self) -> list[Run]:
        return [run for run in self._runs.values() if run.active]

    async def label(self, plan_id: int) -> str:
        """``running`` / ``interrupted`` / ``stopped`` / "" for the plan."""
        run = self._runs.get(plan_id)
        if run is not None and run.active:
            return "running"
        store = self.ctx.store
        if await store.get_meta(f"{RUNNING}{plan_id}") is not None:
            return "interrupted"
        if await store.get_meta(f"{INTERRUPTED}{plan_id}") is not None:
            return "interrupted"
        if await store.get_meta(f"{STOPPED}{plan_id}") is not None:
            return "stopped"
        return ""

    # ------------------------------------------------------------- starting

    async def parallel_for(self, plan_id: int) -> int:
        """Files at once for ``plan_id``: its own setting, else the default the owner
        chose in the bot, else ``OUTBOUND_PARALLEL_FILES`` (0 is no limit)."""
        store = self.ctx.store
        for key in (f"{PARALLEL}{plan_id}", PARALLEL_DEFAULT):
            value = await store.get_meta(key)
            if value is not None:
                with contextlib.suppress(ValueError):
                    return max(int(value), 0)
        return self.ctx.config.outbound.parallel_files

    async def set_parallel(self, plan_id: int | None, limit: int) -> None:
        """Change how many files fetch at once: for one plan (now, while it runs, and
        on its next run) or, with ``plan_id=None``, the default for every plan."""
        limit = max(int(limit), 0)
        if plan_id is None:
            await self.ctx.store.set_meta(PARALLEL_DEFAULT, str(limit))
            return
        await self.ctx.store.set_meta(f"{PARALLEL}{plan_id}", str(limit))
        run = self._runs.get(plan_id)
        if run is not None and run.active:
            run.control.set_limit(limit)

    def control_of(self, plan_id: int) -> Control | None:
        run = self._runs.get(plan_id)
        return run.control if run is not None and run.active else None

    def cancel_file(self, plan_id: int, index: int, *, delete_partial: bool = False) -> bool:
        """終止 one file of a running plan (docs/wms/M9.2 §D.5); False when it is not running."""
        control = self.control_of(plan_id)
        return bool(control and control.cancel(index, delete_partial=delete_partial))

    def pause_file(self, plan_id: int, index: int) -> bool:
        control = self.control_of(plan_id)
        return bool(control and control.pause(index))

    async def set_priority(self, plan_id: int, index: int, level: int) -> bool:
        """优先 for one task of a running plan: now, and in the plan, so a restart keeps it."""
        control = self.control_of(plan_id)
        if control is None or not control.set_priority(index, level):
            return False
        await priority.set_task(self.ctx, plan_id, control.base + index, level)
        return True

    async def set_plan_priority(self, plan_id: int, level: int) -> int:
        """整组优先: the plan and every task of it, running or not. Returns the tasks changed
        in a running plan."""
        control = self.control_of(plan_id)
        changed = control.set_plan_priority(level) if control is not None else 0
        await priority.set_plan(self.ctx, plan_id, level)
        return changed

    async def prioritize_names(self, fragments: list[str], level: int) -> list[str]:
        """「先下 X」: tasks whose file name contains a fragment, in running and in waiting
        plans. Returns the names found."""
        found = await priority.set_named(self.ctx, fragments, level)
        wanted = [f.casefold() for f in fragments if f.strip()]
        for run in self.active():
            for track in run.control.tracks.values():
                if track.pending and any(w in track.name.casefold() for w in wanted):
                    track.priority = level
                    if track.name not in found:
                        found.append(track.name)
        return found

    def queue(self) -> list[tuple[int, FileTrack]]:
        """Every queued task of every running plan, in the order they will start."""
        queued = [(run.plan_id, track) for run in self.active()
                  for track in run.control.tracks.values() if track.state == QUEUED]
        return sorted(queued, key=lambda item: item[1].key)

    def start_file(self, plan_id: int, index: int) -> bool:
        control = self.control_of(plan_id)
        return bool(control and control.start(index))

    async def start(
        self, plan_id: int, *, deliver: Deliver | None = None,
        on_bytes: Callable[[str, int, int], None] | None = None,
        make_deliver: Callable[[outbound.Progress], Deliver] | None = None,
        limit: int | None = None, user_id: int | None = None,
    ) -> Run:
        """Begin applying ``plan_id`` in the background; returns at once.

        Raises ``plan.running`` when it is already being applied, ``plan.closed``
        when it is applied or discarded.
        """
        existing = self._runs.get(plan_id)
        if existing is not None and existing.active:
            raise WmsError(f"plan {plan_id} is already running", key="plan.running", id=plan_id)
        row = await plans.get(self.ctx, plan_id)
        if row["status"] not in plans.OPEN:
            raise WmsError(f"plan {plan_id} is {row['status']}", key="plan.closed",
                           id=plan_id, status=row["status"])
        total = len(row["plan"])
        run = Run(plan_id, total=total, done=row["progress"],
                  control=Control(await self.parallel_for(plan_id)))
        # Registered before anything is awaited again: a second press finds it.
        self._runs[plan_id] = run
        try:
            store = self.ctx.store
            await store.set_meta(f"{RUNNING}{plan_id}", json.dumps(
                {"at": time.time(), "done": row["progress"], "total": total}))
            await store.delete_meta(f"{INTERRUPTED}{plan_id}")
            await store.delete_meta(f"{STOPPED}{plan_id}")
        except BaseException:
            self._runs.pop(plan_id, None)
            raise

        def seen(name: str, received: int, size: int) -> None:
            run.note_bytes(name, received, size)
            if on_bytes is not None:
                on_bytes(name, received, size)

        seen.info = run.note_info  # type: ignore[attr-defined]
        seen.track = run.track_for  # type: ignore[attr-defined]
        has_outbound = any(a.type is ActionType.OUTBOUND for a in row["plan"].actions)
        if deliver is None and has_outbound and make_deliver is not None:
            deliver = make_deliver(seen)
        # Downloads change nothing on the drive, so they do not hold the lock
        # that keeps scheduled jobs and other writers waiting.
        needs_lock = any(a.type is not ActionType.OUTBOUND for a in row["plan"].actions)
        who = downloads.current_user.set(user_id)  # the task inherits it, for the download log
        try:
            run.task = asyncio.create_task(
                self._run(run, deliver, needs_lock, limit), name=f"wms-run-{plan_id}")
        finally:
            downloads.current_user.reset(who)
        # A task cancelled before its first step never reaches the ``finally`` below.
        run.task.add_done_callback(lambda _task: run.finished.set())
        return run

    async def _run(self, run: Run, deliver: Deliver | None, needs_lock: bool,
                   limit: int | None) -> None:
        async def step(done: int, total: int) -> None:
            run.done, run.total = done, total

        try:
            if needs_lock:
                async with self.lock:
                    run.report = await plans.apply(self.ctx, run.plan_id, limit=limit,
                                                   deliver=deliver, on_step=step,
                                                   control=run.control)
            else:
                run.report = await plans.apply(self.ctx, run.plan_id, limit=limit,
                                               deliver=deliver, on_step=step,
                                               control=run.control)
        except asyncio.CancelledError:
            run.stopped = True
            if not run.shutdown:
                await asyncio.shield(self._after_stop(run))
        except WmsError as exc:
            run.error = exc.display()
            log.warning("plan %d failed: %s", run.plan_id, exc)
        except Exception as exc:
            run.error = f"{type(exc).__name__}: {exc}"[:200]
            log.exception("plan %d failed", run.plan_id)
        finally:
            try:
                if not run.shutdown:
                    await asyncio.shield(self.ctx.store.delete_meta(f"{RUNNING}{run.plan_id}"))
            except Exception as exc:  # noqa: BLE001 - bookkeeping must not undo the run
                log.warning("could not clear the running mark of plan %d: %s", run.plan_id, exc)
            run.finished.set()

    async def _after_stop(self, run: Run) -> None:
        """A stopped plan is ``pending`` again, its progress kept."""
        row = await plans.get(self.ctx, run.plan_id)
        if row["status"] in plans.OPEN:
            await self.ctx.store.update_plan(run.plan_id, status=plans.PENDING,
                                             progress=row["progress"], result=row["result"])
        await self.ctx.store.set_meta(f"{STOPPED}{run.plan_id}", json.dumps(
            {"done": row["progress"], "total": len(row["plan"])}))
        run.done = row["progress"]

    # ------------------------------------------------------------- stopping

    async def stop(self, plan_id: int) -> Run | None:
        """Stop a run and wait until it has settled; None when nothing runs."""
        run = self._runs.get(plan_id)
        if run is None or not run.active or run.task is None:
            return None
        run.task.cancel()
        await run.finished.wait()
        return run

    async def stop_all(self) -> None:
        """The process is going away: stop quietly, keeping every plan resumable."""
        for run in self.active():
            run.shutdown = True
            await self.stop(run.plan_id)

    # ------------------------------------------------------------- restarts

    async def recover(self) -> list[dict[str, Any]]:
        """Marks left by a process that died mid-run → ``interrupted``.

        Returns ``[{"id", "done", "total"}]`` for the bot to tell the admins.
        """
        store, out = self.ctx.store, []
        for key in sorted(await store.meta_with_prefix(RUNNING)):
            plan_id = int(key.removeprefix(RUNNING))
            await store.delete_meta(key)
            try:
                row = await plans.get(self.ctx, plan_id)
            except WmsError:
                continue
            if row["status"] not in plans.OPEN:
                continue
            info = {"id": plan_id, "done": row["progress"], "total": len(row["plan"])}
            await store.set_meta(f"{INTERRUPTED}{plan_id}", json.dumps(info))
            out.append(info)
        return out
