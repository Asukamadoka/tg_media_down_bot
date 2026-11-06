"""What a running plan shows and what can be told to it: its files (docs/wms/M9.2 §D).

A plan of downloads runs several files at once. :class:`Control` keeps one
:class:`FileTrack` per action, each with its own state, progress and a way to be
cancelled on its own, and a :class:`Gate` that says how many may be fetching
right now (changeable while the plan runs).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

WINDOW = 15.0
"""Seconds of download a file's speed is averaged over."""

class SpeedMeter:
    """Bytes per second over the last few seconds of a download."""

    def __init__(self) -> None:
        self._samples: deque[tuple[float, int]] = deque(maxlen=64)

    def clear(self) -> None:
        self._samples.clear()

    def add(self, received: int) -> None:
        self._samples.append((time.monotonic(), received))

    @property
    def speed(self) -> float:
        if len(self._samples) < 2:
            return 0.0
        end_time, end_bytes = self._samples[-1]
        start_time, start_bytes = next(
            (s for s in self._samples if end_time - s[0] <= WINDOW), self._samples[0])
        span = end_time - start_time
        return max(end_bytes - start_bytes, 0) / span if span > 0 else 0.0


QUEUED, ACTIVE, DONE, FAILED, CANCELLED, SKIPPED, PAUSED = (
    "queued", "active", "done", "failed", "cancelled", "skipped", "paused")


@dataclass
class FileTrack:
    index: int
    """The action's place in the plan."""
    file_id: str
    name: str
    size: int = 0
    priority: int = 0
    """0 normal, 1 high, 2 top (docs/wms/M9.4); may change while the file waits or runs."""
    base: int = 0
    """How many actions of the plan were done before this run: the task's number is
    ``base + index + 1``, the place it has in the plan."""
    created: str = ""
    """When the plan was made: the tie-break between plans of the same priority."""
    state: str = QUEUED
    received: int = 0
    conns: int = 0
    links: str = ""
    note: str = ""
    cancel_requested: bool = False
    pause_requested: bool = False
    paused: bool = False
    delete_partial: bool = False
    """Set with a stop: remove ``part`` and its sidecar once the file has let go."""
    logged_cancel: bool = False
    settling: bool = False
    """The download is over and is being written down; too late to pause or stop."""
    part: Path | None = None
    """Where the file is being written (``.part``), once it has started."""
    task: asyncio.Task | None = None
    resume: asyncio.Event = field(default_factory=asyncio.Event)
    started: float | None = None
    meter: SpeedMeter = field(default_factory=SpeedMeter)

    def begin(self) -> None:
        self.state, self.started = ACTIVE, time.monotonic()
        self.meter.clear()

    def bytes(self, received: int, size: int) -> None:
        self.received = received
        if size:
            self.size = size
        self.meter.add(received)

    def info(self, conns: int, links: str) -> None:
        self.conns, self.links = conns, links

    @property
    def speed(self) -> float:
        return self.meter.speed

    @property
    def average(self) -> float:
        if self.started is None:
            return 0.0
        span = time.monotonic() - self.started
        return self.received / span if span > 0 else 0.0

    @property
    def fraction(self) -> float | None:
        return self.received / self.size if self.size else None

    @property
    def number(self) -> int:
        return self.base + self.index + 1

    @property
    def order(self) -> tuple[str, int]:
        """Within one priority: older plans first, then the plan's own order."""
        return (self.created, self.base + self.index)

    @property
    def key(self) -> tuple[int, str, int]:
        """The start order: ``(-priority, plan created_at, item index)`` (M9.4 §A.2)."""
        return (-self.priority, *self.order)

    @property
    def pending(self) -> bool:
        return self.state in (QUEUED, ACTIVE, PAUSED)

    def partial_files(self) -> list[Path]:
        """The ``.part`` and ``.part.state`` this file left, those that exist."""
        if self.part is None:
            return []
        side = self.part.with_name(self.part.name + ".state")
        return [p for p in (self.part, side) if p.exists()]


class _Waiter:
    __slots__ = ("front", "future", "key", "owner", "seq")

    def __init__(self, owner, future, front, key, seq):
        self.owner, self.future, self.front, self.key, self.seq = owner, future, front, key, seq

    def rank(self) -> tuple:
        # A promoted waiter first (the latest promoted first), then by the task's key.
        return (0, -self.seq) if self.front else (1, self.key() if self.key else (), self.seq)


class Gate:
    """At most ``limit`` holders at once; 0 means no limit. The limit may change
    while holders are inside. Waiters are served in the order of their ``key`` (priority,
    plan age, place: docs/wms/M9.4 §A.2), a promoted waiter (``front``) before all."""

    def __init__(self, limit: int = 0) -> None:
        self.limit = max(limit, 0)
        self.inside = 0
        self._waiters: list[_Waiter] = []
        self._seq = 0

    def _room(self) -> bool:
        return self.limit == 0 or self.inside < self.limit

    async def acquire(self, owner: object = None, *, front: bool = False,
                      key: Callable[[], tuple] | None = None) -> None:
        if self._room() and not self._waiters:
            self.inside += 1
            return
        self._seq += 1
        waiter = _Waiter(owner, asyncio.get_running_loop().create_future(), front, key,
                         self._seq)
        self._waiters.append(waiter)
        try:
            await waiter.future
        except BaseException:
            if waiter.future.done() and not waiter.future.cancelled():
                self._leave()
            else:
                with contextlib.suppress(ValueError):
                    self._waiters.remove(waiter)
            raise

    def release(self) -> None:
        self._leave()

    async def __aenter__(self) -> Gate:
        await self.acquire()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self._leave()

    def _leave(self) -> None:
        self.inside = max(self.inside - 1, 0)
        self._wake()

    def _wake(self) -> None:
        while self._waiters and self._room():
            waiter = min(self._waiters, key=_Waiter.rank)
            self._waiters.remove(waiter)
            if not waiter.future.done():
                self.inside += 1
                waiter.future.set_result(None)

    def promote(self, owner: object) -> bool:
        """Move ``owner``'s waiting place to the front; False when it is not waiting."""
        for waiter in self._waiters:
            if waiter.owner == owner:
                self._seq += 1
                waiter.front, waiter.seq = True, self._seq
                return True
        return False

    def set_limit(self, limit: int) -> None:
        self.limit = max(int(limit), 0)
        self._wake()


class Control:
    """One run's files and the dial for how many are fetched at once."""

    def __init__(self, limit: int = 0) -> None:
        self.gate = Gate(limit)
        self.tracks: dict[int, FileTrack] = {}
        self.page = 0
        self.base = 0
        """Actions of the plan done before this run (the offset of the task numbers)."""
        self.plan_priority = 0
        self.created = ""
        """The plan's ``created_at``."""

    @property
    def limit(self) -> int:
        return self.gate.limit

    def set_limit(self, limit: int) -> None:
        self.gate.set_limit(limit)

    def add(self, index: int, file_id: str, name: str, size: int,
            priority: int | None = None) -> FileTrack:
        track = FileTrack(index=index, file_id=file_id, name=name, size=size, base=self.base,
                          created=self.created,
                          priority=self.plan_priority if priority is None else priority)
        self.tracks[index] = track
        return track

    def set_priority(self, index: int, level: int) -> bool:
        """Change one task's priority, whatever state it is in (a finished one too: it is
        only remembered). False for a task this run does not have."""
        track = self.tracks.get(index)
        if track is None:
            return False
        track.priority = level
        return True

    def set_plan_priority(self, level: int) -> int:
        """整组优先: the plan's priority, and every task of it. Returns how many tasks."""
        self.plan_priority = level
        for track in self.tracks.values():
            track.priority = level
        return len(self.tracks)

    def queued_order(self) -> list[FileTrack]:
        """The queued tasks in the order they will start."""
        return sorted((t for t in self.tracks.values() if t.state == QUEUED),
                      key=lambda t: t.key)

    def track_for(self, file_id: str) -> FileTrack | None:
        """The file being fetched for ``file_id`` (an active one first)."""
        found = [t for t in self.tracks.values() if t.file_id == file_id and t.pending]
        found.sort(key=lambda t: t.state != ACTIVE)
        return found[0] if found else None

    def cancel(self, index: int, *, delete_partial: bool = False) -> bool:
        """終止 this file only: queued, running or paused. False when it is none of
        those. Its ``.part`` stays unless ``delete_partial``."""
        track = self.tracks.get(index)
        if (track is None or not track.pending or track.cancel_requested or track.task is None
                or track.settling):
            return False
        track.cancel_requested = True
        track.delete_partial = delete_partial
        track.task.cancel()
        return True

    def pause(self, index: int) -> bool:
        """暂停: stop transferring now, keep ``.part`` and its state, give the
        connections back. False unless the file is queued or running."""
        track = self.tracks.get(index)
        if (track is None or track.state not in (QUEUED, ACTIVE) or track.task is None
                or track.pause_requested or track.cancel_requested or track.settling):
            return False
        track.pause_requested = True
        track.task.cancel()
        return True

    def start(self, index: int) -> bool:
        """开始: resume a paused file, or move a queued one to the front of the
        queue. False when it is running or finished."""
        track = self.tracks.get(index)
        if track is None or track.cancel_requested:
            return False
        if track.pause_requested:
            return False  # the pause is still on its way; press again in a moment
        if track.paused:
            track.state = QUEUED
            track.resume.set()
            return True
        if track.state == QUEUED:
            self.gate.promote(index)
            return True
        return False

    def pause_all(self) -> int:
        return sum(1 for i in list(self.tracks) if self.pause(i))

    def start_all(self) -> int:
        return sum(1 for i, t in list(self.tracks.items())
                   if t.state == PAUSED and self.start(i))

    def delete_partial(self, index: int) -> int:
        """Remove what a stopped file left behind; returns the files removed. Only
        for a file that is no longer going to be written."""
        track = self.tracks.get(index)
        if track is None or track.state != CANCELLED:
            return 0
        removed = 0
        for path in track.partial_files():
            with contextlib.suppress(OSError):
                path.unlink()
                removed += 1
        return removed

    def listing(self) -> list[FileTrack]:
        """The files still queued, running or paused, active ones first, then plan order."""
        pending = [t for t in self.tracks.values() if t.pending]
        return sorted(pending, key=lambda t: (t.state != ACTIVE, t.index))

    def rows(self) -> list[FileTrack]:
        """Every file, one line each: running, paused, queued, then those that ended
        badly, then the rest (plan order within each)."""
        rank = {ACTIVE: 0, PAUSED: 1, QUEUED: 2, FAILED: 3, CANCELLED: 4, DONE: 5, SKIPPED: 6}
        return sorted(self.tracks.values(), key=lambda t: (rank.get(t.state, 9), t.index))

    def counts(self) -> dict[str, int]:
        out = dict.fromkeys((QUEUED, ACTIVE, DONE, FAILED, CANCELLED, SKIPPED, PAUSED), 0)
        for track in self.tracks.values():
            out[track.state] += 1
        return out

    @property
    def speed(self) -> float:
        return sum(t.speed for t in self.tracks.values() if t.state == ACTIVE)

    def handle(self, verb: str, index: int, *, delete_partial: bool = False) -> bool:
        """One request from the table (``wms task pause|start|stop <plan>:<n>``)."""
        if verb == "pause":
            return self.pause(index)
        if verb == "start":
            return self.start(index)
        if verb == "stop":
            return self.cancel(index, delete_partial=delete_partial)
        return False

    @property
    def eta(self) -> float | None:
        """Time left for what is queued and running, at the current total speed."""
        speed = self.speed
        left = sum(max(t.size - t.received, 0) for t in self.tracks.values() if t.pending)
        return left / speed if speed > 0 and left else None
