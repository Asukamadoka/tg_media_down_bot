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
from dataclasses import dataclass, field

WINDOW = 15.0
"""Seconds of download a file's speed is averaged over."""

QUEUED, ACTIVE, DONE, FAILED, CANCELLED, SKIPPED = (
    "queued", "active", "done", "failed", "cancelled", "skipped")


@dataclass
class FileTrack:
    index: int
    """The action's place in the plan."""
    file_id: str
    name: str
    size: int = 0
    state: str = QUEUED
    received: int = 0
    conns: int = 0
    links: str = ""
    note: str = ""
    cancel_requested: bool = False
    task: asyncio.Task | None = None
    started: float | None = None
    _samples: deque = field(default_factory=lambda: deque(maxlen=64))

    def begin(self) -> None:
        self.state, self.started = ACTIVE, time.monotonic()

    def bytes(self, received: int, size: int) -> None:
        self.received = received
        if size:
            self.size = size
        self._samples.append((time.monotonic(), received))

    def info(self, conns: int, links: str) -> None:
        self.conns, self.links = conns, links

    @property
    def speed(self) -> float:
        """Bytes per second over the last few seconds."""
        if len(self._samples) < 2:
            return 0.0
        end_time, end_bytes = self._samples[-1]
        start_time, start_bytes = next(
            (s for s in self._samples if end_time - s[0] <= WINDOW), self._samples[0])
        span = end_time - start_time
        return max(end_bytes - start_bytes, 0) / span if span > 0 else 0.0

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
    def pending(self) -> bool:
        return self.state in (QUEUED, ACTIVE)


class Gate:
    """At most ``limit`` holders at once; 0 means no limit. The limit may change
    while holders are inside."""

    def __init__(self, limit: int = 0) -> None:
        self.limit = max(limit, 0)
        self.inside = 0
        self._waiters: deque[asyncio.Future] = deque()

    def _room(self) -> bool:
        return self.limit == 0 or self.inside < self.limit

    async def __aenter__(self) -> Gate:
        if self._room() and not self._waiters:
            self.inside += 1
            return self
        waiter = asyncio.get_running_loop().create_future()
        self._waiters.append(waiter)
        try:
            await waiter
        except BaseException:
            if waiter.done() and not waiter.cancelled():
                self._leave()
            else:
                with contextlib.suppress(ValueError):
                    self._waiters.remove(waiter)
            raise
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self._leave()

    def _leave(self) -> None:
        self.inside = max(self.inside - 1, 0)
        self._wake()

    def _wake(self) -> None:
        while self._waiters and self._room():
            waiter = self._waiters.popleft()
            if not waiter.done():
                self.inside += 1
                waiter.set_result(None)

    def set_limit(self, limit: int) -> None:
        self.limit = max(int(limit), 0)
        self._wake()


class Control:
    """One run's files and the dial for how many are fetched at once."""

    def __init__(self, limit: int = 0) -> None:
        self.gate = Gate(limit)
        self.tracks: dict[int, FileTrack] = {}
        self.page = 0

    @property
    def limit(self) -> int:
        return self.gate.limit

    def set_limit(self, limit: int) -> None:
        self.gate.set_limit(limit)

    def add(self, index: int, file_id: str, name: str, size: int) -> FileTrack:
        track = FileTrack(index=index, file_id=file_id, name=name, size=size)
        self.tracks[index] = track
        return track

    def track_for(self, file_id: str) -> FileTrack | None:
        """The file being fetched for ``file_id`` (an active one first)."""
        found = [t for t in self.tracks.values() if t.file_id == file_id and t.pending]
        found.sort(key=lambda t: t.state != ACTIVE)
        return found[0] if found else None

    def cancel(self, index: int) -> bool:
        """Stop this file only. False when it is not queued or running."""
        track = self.tracks.get(index)
        if track is None or not track.pending or track.cancel_requested or track.task is None:
            return False
        track.cancel_requested = True
        track.task.cancel()
        return True

    def listing(self) -> list[FileTrack]:
        """The files still queued or running, active ones first, then plan order."""
        pending = [t for t in self.tracks.values() if t.pending]
        return sorted(pending, key=lambda t: (t.state != ACTIVE, t.index))

    def counts(self) -> dict[str, int]:
        out = dict.fromkeys((QUEUED, ACTIVE, DONE, FAILED, CANCELLED, SKIPPED), 0)
        for track in self.tracks.values():
            out[track.state] += 1
        return out

    @property
    def speed(self) -> float:
        return sum(t.speed for t in self.tracks.values() if t.state == ACTIVE)

    @property
    def eta(self) -> float | None:
        """Time left for what is queued and running, at the current total speed."""
        speed = self.speed
        left = sum(max(t.size - t.received, 0) for t in self.tracks.values() if t.pending)
        return left / speed if speed > 0 and left else None
