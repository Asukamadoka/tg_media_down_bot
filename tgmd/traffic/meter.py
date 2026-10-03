"""Turning mihomo's cumulative counters into bytes spent, per connection.

mihomo keeps a running ``upload`` and ``download`` for every live connection
and global totals, and nothing else: no history, and everything starts again
at zero when the proxy restarts. :class:`Meter` takes one snapshot at a time
and returns what was added since the previous one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .classify import Classified, classify

log = logging.getLogger(__name__)


@dataclass
class Delta:
    """Bytes one connection moved since the last snapshot."""

    conn_id: str
    info: Classified
    up: int
    down: int
    total: int
    """The connection's own running total, for the single-connection alert."""

    @property
    def bytes(self) -> int:
        return self.up + self.down


@dataclass
class Step:
    """What one snapshot added."""

    deltas: list[Delta] = field(default_factory=list)
    unattributed_up: int = 0
    unattributed_down: int = 0
    reset: bool = False
    """The proxy restarted (a counter went backwards)."""
    primed: bool = True
    """False for the first snapshot, which only sets the baseline."""
    live: dict[str, Classified] = field(default_factory=dict)


class Meter:
    """Delta accounting over successive ``/connections`` snapshots."""

    def __init__(self, model_host: tuple[str, int] | None = None) -> None:
        self._model_host = model_host
        self._seen: dict[str, tuple[int, int]] = {}
        self._totals: tuple[int, int] | None = None
        self._warned_reset = False

    def ingest(self, snapshot: dict) -> Step:
        up_total = int(snapshot.get("uploadTotal") or 0)
        down_total = int(snapshot.get("downloadTotal") or 0)
        connections = snapshot.get("connections") or []

        step = Step()
        first = self._totals is None
        reset = (
            not first
            and (up_total < self._totals[0] or down_total < self._totals[1])  # type: ignore[index]
        )
        if reset:
            # Rebaseline. Every connection is younger than the restart, so each
            # counts from zero; the global difference means nothing this time.
            if not self._warned_reset:
                log.warning("proxy counters went backwards; the proxy restarted. Rebaselining.")
                self._warned_reset = True
            self._seen = {}
            step.reset = True
        else:
            self._warned_reset = False

        attributed_up = attributed_down = 0
        seen: dict[str, tuple[int, int]] = {}
        for connection in connections:
            conn_id = str(connection.get("id") or "")
            if not conn_id:
                continue
            up = int(connection.get("upload") or 0)
            down = int(connection.get("download") or 0)
            seen[conn_id] = (up, down)
            previous = self._seen.get(conn_id)
            if previous is None:
                # A connection that existed before the first snapshot has an
                # unknown history; one that appears later started since the
                # last poll, so all of it is new.
                delta_up, delta_down = (0, 0) if first else (up, down)
            else:
                delta_up, delta_down = up - previous[0], down - previous[1]
                if delta_up < 0 or delta_down < 0:
                    delta_up, delta_down = up, down
            info = classify(connection, self._model_host)
            step.live[conn_id] = info
            if delta_up or delta_down:
                step.deltas.append(Delta(conn_id, info, delta_up, delta_down, up + down))
                attributed_up += delta_up
                attributed_down += delta_down

        if not first and not reset and self._totals is not None:
            step.unattributed_up = max(0, up_total - self._totals[0] - attributed_up)
            step.unattributed_down = max(0, down_total - self._totals[1] - attributed_down)
        step.primed = not first
        self._seen = seen
        self._totals = (up_total, down_total)
        return step
