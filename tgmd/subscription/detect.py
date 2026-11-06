"""Is the subscription dying? (docs/wms/M9.6 §A)

Pure functions over a :class:`Snapshot`, so every rule is testable. The node-dead rule
itself is M9.1's (``NodeManager.health``: most nodes dead for 10 minutes); it arrives here
as ``Snapshot.nodes_dead`` and is not computed twice.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from ..traffic.probe import INFO_NODE

FETCH_DEAD = "fetch_dead"
NODES_DEAD = "nodes_dead"
SENTINEL = "sentinel"
NODES_SHRUNK = "nodes_shrunk"
USERINFO_EXPIRY = "userinfo_expiry"
SIGNALS = (FETCH_DEAD, NODES_DEAD, SENTINEL, NODES_SHRUNK, USERINFO_EXPIRY)
DEAD_SIGNALS = frozenset({FETCH_DEAD, NODES_DEAD})

OK, WARNING, DEAD = "ok", "warning", "dead"

DEAD_STATUSES = frozenset({401, 403, 404})
SHRINK_PERCENT = 30
LOW_LEFT = 0.05
"""Warn when less than this share of ``total`` is left."""
DAY = 86400.0

_PSEUDO = re.compile(r"^\s*(DIRECT|REJECT|PASS|COMPATIBLE)\b", re.IGNORECASE)


def compile_sentinel(pattern: str) -> re.Pattern[str] | None:
    """``SUB_SENTINEL_REGEX``; empty (or not a valid regex) switches the signal off."""
    if not pattern.strip():
        return None
    try:
        return re.compile(pattern)
    except re.error:
        return None


def is_real_node(name: str, sentinel: re.Pattern[str] | None = None) -> bool:
    """A server, as opposed to a notice (M9.1's ``INFO_NODE``), a ``DIRECT``/``REJECT``-like
    entry or the provider's own "top up" pseudo-node."""
    return not (INFO_NODE.search(name) or _PSEUDO.match(name)
                or (sentinel is not None and sentinel.search(name)))


def real_count(names: Sequence[str], sentinel: re.Pattern[str] | None = None) -> int:
    return sum(1 for name in names if is_real_node(name, sentinel))


def parse_userinfo(header: str | None) -> dict[str, int]:
    """``upload=0; download=1; total=2; expire=3`` as numbers; anything unusable is left out."""
    out: dict[str, int] = {}
    for part in (header or "").split(";"):
        key, _, value = part.strip().partition("=")
        try:
            out[key.strip().lower()] = int(float(value))
        except ValueError:
            continue
    return out


def userinfo_signal(header: str | None, now: float, warn_days: float) -> bool:
    """``expire`` within ``warn_days``, or under 5 % of ``total`` left. An empty or odd
    header says nothing (UNVERIFIED: the provider returns none today)."""
    info = parse_userinfo(header)
    expire = info.get("expire", 0)
    if expire > 0 and expire - now <= warn_days * DAY:
        return True
    total = info.get("total", 0)
    if total > 0 and ("download" in info or "upload" in info):
        left = total - info.get("download", 0) - info.get("upload", 0)
        return left < total * LOW_LEFT
    return False


@dataclass(frozen=True)
class Snapshot:
    now: float
    names: Sequence[str] | None = None
    """Every entry of the provider; None when it could not be read."""
    nodes_dead: bool = False
    fetch_status: int | None = None
    """HTTP status of the bot's own last fetch of the stored URL; None: never, or no answer."""
    userinfo: str | None = None
    healthy_real: int | None = None
    """Real nodes in the last snapshot with state ``ok``."""


def shrunk(real: int, healthy: int | None) -> bool:
    """Real nodes dropped by at least 30 % against the last healthy snapshot."""
    return bool(healthy) and (healthy - real) * 100 >= SHRINK_PERCENT * healthy


def signals(snap: Snapshot, *, sentinel: re.Pattern[str] | None = None,
            warn_days: float = 3.0) -> list[str]:
    """The names of the signals that fire, in the fixed order of :data:`SIGNALS`. A 5xx or a
    network error is ``fetch_unknown``, which is not a signal."""
    found: set[str] = set()
    if snap.fetch_status in DEAD_STATUSES:
        found.add(FETCH_DEAD)
    if snap.nodes_dead:
        found.add(NODES_DEAD)
    if snap.names is not None:
        if sentinel is not None and any(sentinel.search(n) for n in snap.names):
            found.add(SENTINEL)
        if shrunk(real_count(snap.names, sentinel), snap.healthy_real):
            found.add(NODES_SHRUNK)
    if userinfo_signal(snap.userinfo, snap.now, warn_days):
        found.add(USERINFO_EXPIRY)
    return [s for s in SIGNALS if s in found]


def level(found: Sequence[str]) -> str:
    """``ok`` → ``warning`` (soft signals) → ``dead`` (the fetch or the nodes say so)."""
    if DEAD_SIGNALS & set(found):
        return DEAD
    return WARNING if found else OK
