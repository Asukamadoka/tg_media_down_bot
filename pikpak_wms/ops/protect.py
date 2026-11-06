"""The whitelist (docs/wms/M7 §1): what no plan may ever touch.

Protected are the folders ``protect.paths`` names (plus any added with
``/wms protect add``, minus any removed), and, with ``protect.shared``,
everything the account ever shared: the share list is read live from
PikPak before each plan, expired shares included, and each shared
``file_id`` is resolved to its path in the index.

The filter sits in the plan layer, not in the rules: :func:`apply_to` is
called by :func:`pikpak_wms.ops.plans.save` for every plan, whatever made
it, and the executor checks again before each action (a share may have
been made after the plan). An action is dropped when

* its source or its destination is a protected folder or lies under one, or
* it would move, rename or trash a folder that *contains* protected content
  (moving the parent moves the child, so the literal path test is not enough).

A protected copy always stays in dedupe: see :func:`pikpak_wms.ops.organize.dedupe`.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..core.errors import WmsError
from ..core.models import Action, ActionType, Plan, normalize_path, parent_of
from .context import Context

log = logging.getLogger(__name__)

META_EDITS = "protect"
"""``{"added": [...], "removed": [...]}``: what /wms protect changed."""

META_SHARES = "protect:shares"
"""``{"at": ..., "roots": [...]}``: the last share list that could be read."""

SHARES_TTL = 60.0
"""Seconds one reading of the share list is reused within a process."""

_MOVES = (ActionType.MOVE, ActionType.RENAME, ActionType.TRASH, ActionType.DELETE_FOREVER)


def within(path: str, root: str) -> bool:
    """``path`` is ``root`` or lies under it."""
    path, root = normalize_path(path), normalize_path(root)
    return root == "/" or path == root or path.startswith(root + "/")


@dataclass(frozen=True)
class Protection:
    roots: tuple[str, ...] = ()
    notes: tuple[dict[str, Any], ...] = field(default=())
    """Worth saying on every plan: shares read from the cache, unresolved ones."""
    _exact: frozenset[str] = field(default=frozenset(), init=False, repr=False)
    _prefixes: tuple[str, ...] = field(default=(), init=False, repr=False)

    def __post_init__(self) -> None:
        # Asked for every node of an 80,000-entry index: one set lookup and
        # one startswith() instead of a loop over the roots.
        roots = tuple(normalize_path(root) for root in self.roots)
        object.__setattr__(self, "roots", roots)
        object.__setattr__(self, "_exact", frozenset(roots))
        object.__setattr__(self, "_prefixes", tuple(root + "/" for root in roots
                                                    if root != "/"))

    def covers(self, path: str | None) -> bool:
        if not path or not self.roots:
            return False
        if "/" in self._exact:
            return True
        return path in self._exact or path.startswith(self._prefixes)

    def holds(self, path: str | None) -> bool:
        """``path`` is a folder with protected content somewhere below it."""
        if not path or not self.roots:
            return False
        path = normalize_path(path)
        prefix = path.rstrip("/") + "/"
        return any(root != path and root.startswith(prefix) for root in self.roots)

    def touches(self, action: Action) -> bool:
        before, after = action.before, action.after
        if any(self.covers(p) for p in (before.get("path"), after.get("path"),
                                        after.get("parent_path"))):
            return True
        return action.type in _MOVES and self.holds(before.get("path"))


# ------------------------------------------------------------------ reading


async def _edits(ctx: Context) -> dict[str, list[str]]:
    raw = await ctx.store.get_meta(META_EDITS)
    data = json.loads(raw) if raw else {}
    return {"added": list(data.get("added") or []), "removed": list(data.get("removed") or [])}


async def configured(ctx: Context) -> list[str]:
    """The protected folders by name: config, plus added, minus removed."""
    edits = await _edits(ctx)
    paths = {normalize_path(p) for p in ctx.config.protect.paths}
    paths |= {normalize_path(p) for p in edits["added"]}
    paths -= {normalize_path(p) for p in edits["removed"]}
    return sorted(paths)


def _share_ids(share: dict[str, Any]) -> list[str]:
    ids = [share.get("file_id")]
    ids += list(share.get("file_ids") or [])
    ids += [f.get("id") for f in share.get("files") or [] if isinstance(f, dict)]
    return [str(i) for i in ids if i]


async def share_roots(ctx: Context) -> tuple[list[str], list[dict[str, Any]]]:
    """Paths of everything shared, and notes for the plan.

    If PikPak cannot be asked, the last list that was read is used and the
    plan says so; with no list at all, planning stops: guessing that nothing
    is shared could lose shared files, which is the one thing this is for.
    """
    memo = ctx.cache.get("protect:shares")
    if memo is not None and time.monotonic() - memo[0] < SHARES_TTL:
        return memo[1], memo[2]
    notes: list[dict[str, Any]] = []
    try:
        shares = await ctx.client.shares()
    except WmsError as exc:
        cached = await ctx.store.get_meta(META_SHARES)
        if not cached:
            raise WmsError(f"cannot read the share list: {exc}", key="protect.shares_failed",
                           error=exc.display()) from exc
        data = json.loads(cached)
        log.warning("share list unavailable (%s); using the one from %s", exc, data.get("at"))
        notes.append({"key": "protect.shares_cached", "args": {"at": data.get("at", "?")}})
        return list(data.get("roots") or []), notes

    roots: set[str] = set()
    unresolved = 0
    ids = {fid for share in shares for fid in _share_ids(share)}
    for file_id in sorted(ids):
        node = await ctx.store.node(file_id)
        if node is None:
            unresolved += 1
        else:
            roots.add(node.path)
    if unresolved:
        notes.append({"key": "protect.shares_unresolved", "args": {"count": unresolved}})
    result = sorted(roots)
    await ctx.store.set_meta(META_SHARES, json.dumps(
        {"at": datetime.now(UTC).isoformat(timespec="seconds"), "roots": result},
        ensure_ascii=False))
    ctx.cache["protect:shares"] = (time.monotonic(), result, notes)
    return result, notes


async def load(ctx: Context) -> Protection:
    roots = await configured(ctx)
    notes: list[dict[str, Any]] = []
    if ctx.config.protect.shared:
        shared, notes = await share_roots(ctx)
        roots = sorted(set(roots) | set(shared))
    return Protection(roots=tuple(roots), notes=tuple(notes))


# ----------------------------------------------------------------- filtering


def apply_to(plan: Plan, protection: Protection) -> int:
    """Drop every action touching protected content; returns how many.

    A ``create_folder`` that only served dropped moves goes too, so the
    plan does not make empty folders for nothing.
    """
    kept: list[Action] = []
    orphaned: set[str] = set()
    dropped = 0
    for action in plan.actions:
        if protection.touches(action):
            dropped += 1
            target = action.after.get("parent_path")
            while target and target != "/":
                orphaned.add(target)
                target = parent_of(target)
            continue
        kept.append(action)
    if dropped:
        needed: set[str] = set()
        for action in kept:
            target = action.after.get("parent_path")
            while target and target != "/":
                needed.add(target)
                target = parent_of(target)
        kept = [
            a for a in kept
            if not (a.type is ActionType.CREATE_FOLDER
                    and a.after.get("path") in orphaned - needed)
        ]
        plan.note("protect.skipped", count=dropped)
    plan.actions = kept
    for note in protection.notes:
        if note not in plan.notes:
            plan.notes.append(dict(note))
    return dropped


# ------------------------------------------------------------------ editing


async def add(ctx: Context, path: str) -> list[str]:
    path = normalize_path(path)
    if path == "/":
        raise WmsError("protecting / would freeze the whole drive", key="protect.root")
    edits = await _edits(ctx)
    edits["removed"] = [p for p in edits["removed"] if normalize_path(p) != path]
    if path not in {normalize_path(p) for p in ctx.config.protect.paths}:
        edits["added"] = sorted({*edits["added"], path})
    await ctx.store.set_meta(META_EDITS, json.dumps(edits, ensure_ascii=False))
    return await configured(ctx)


async def remove(ctx: Context, path: str) -> list[str]:
    path = normalize_path(path)
    if path not in await configured(ctx):
        raise WmsError(f"{path} is not protected", key="protect.not_protected", path=path)
    edits = await _edits(ctx)
    edits["added"] = [p for p in edits["added"] if normalize_path(p) != path]
    if path in {normalize_path(p) for p in ctx.config.protect.paths}:
        edits["removed"] = sorted({*edits["removed"], path})
    await ctx.store.set_meta(META_EDITS, json.dumps(edits, ensure_ascii=False))
    return await configured(ctx)


async def listing(ctx: Context) -> dict[str, Any]:
    """For /wms protect ls: the named folders, and the shared ones."""
    shared: list[str] = []
    notes: list[dict[str, Any]] = []
    if ctx.config.protect.shared:
        shared, notes = await share_roots(ctx)
    return {"paths": await configured(ctx), "shared": shared, "notes": notes,
            "shared_enabled": ctx.config.protect.shared}
