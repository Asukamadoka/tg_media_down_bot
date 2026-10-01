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
import json
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..core.errors import WmsError
from ..core.models import ActionType
from ..rules.actions import Deliver
from . import outbound, plans
from .context import Context

log = logging.getLogger(__name__)

RUNNING, INTERRUPTED, STOPPED = "running:", "interrupted:", "stopped:"
WINDOW = 15.0
"""Seconds of download the speed is averaged over."""


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
    _samples: deque = field(default_factory=lambda: deque(maxlen=64))

    def note_bytes(self, name: str, received: int, size: int) -> None:
        if name != self.file:
            self.file, self._samples = name, deque(maxlen=64)
        self.received, self.size = received, size
        self._samples.append((time.monotonic(), received))

    @property
    def speed(self) -> float:
        """Bytes per second of the file being fetched, over the last few seconds."""
        if len(self._samples) < 2:
            return 0.0
        end_time, end_bytes = self._samples[-1]
        start_time, start_bytes = next(
            (s for s in self._samples if end_time - s[0] <= WINDOW), self._samples[0])
        span = end_time - start_time
        return max(end_bytes - start_bytes, 0) / span if span > 0 else 0.0

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

    async def start(
        self, plan_id: int, *, deliver: Deliver | None = None,
        on_bytes: Callable[[str, int, int], None] | None = None,
        make_deliver: Callable[[outbound.Progress], Deliver] | None = None,
        limit: int | None = None,
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
        run = Run(plan_id, total=total, done=row["progress"])
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

        has_outbound = any(a.type is ActionType.OUTBOUND for a in row["plan"].actions)
        if deliver is None and has_outbound and make_deliver is not None:
            deliver = make_deliver(seen)
        # Downloads change nothing on the drive, so they do not hold the lock
        # that keeps scheduled jobs and other writers waiting.
        needs_lock = any(a.type is not ActionType.OUTBOUND for a in row["plan"].actions)
        run.task = asyncio.create_task(
            self._run(run, deliver, needs_lock, limit), name=f"wms-run-{plan_id}")
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
                                                   deliver=deliver, on_step=step)
            else:
                run.report = await plans.apply(self.ctx, run.plan_id, limit=limit,
                                               deliver=deliver, on_step=step)
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
