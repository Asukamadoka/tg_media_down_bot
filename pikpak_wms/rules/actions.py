"""The action primitives. Each one has two halves (rule 1):

* ``plan()`` turns a rule's step and one file into :class:`Action` records,
  reading only the local index and touching nothing;
* ``apply()`` carries out a batch of those records against PikPak and then
  updates the index to match, so a second plan straight after finds nothing
  left to do (rule 5).

Two more halves serve the executor: ``check()`` says whether an action is
still due (the file may have moved since the plan was made), and
``inverse()`` builds the action that undoes an audit entry, or refuses.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, tzinfo
from typing import Any

from ..core.client import WmsClient
from ..core.errors import NotFoundError, WmsError
from ..core.models import (
    ROOT_ID,
    Action,
    ActionType,
    FileNode,
    Kind,
    join_path,
    normalize_path,
    parent_of,
)
from ..store.db import Store
from . import template as templates
from .matcher import category_of
from .schema import CreateFolderSpec, MoveSpec, OutboundSpec, RenameSpec, ShareSpec

BATCH_LIMIT = 100
"""Most ids sent in one batch request."""


class Conflict(Exception):
    """This file cannot take this step; the plan notes why and moves on.

    Carries a catalogue key, because the reason is stored in the plan and
    translated only when shown.
    """

    def __init__(self, key: str, **kwargs: Any) -> None:
        super().__init__(key)
        self.key = key
        self.kwargs = kwargs


class Refused(WmsError):
    """An audit entry that cannot be undone automatically."""


# ------------------------------------------------------------------ planning


@dataclass
class Draft:
    """One matched file as it will be after the steps planned so far."""

    node: FileNode
    captures: dict[str, Any] = field(default_factory=dict)
    gone: bool = False

    def values(self) -> dict[str, Any]:
        node = self.node
        stem, dot, ext = node.name.rpartition(".")
        if not (dot and stem):
            stem, ext = node.name, ""
        return {
            "name": node.name,
            "stem": stem,
            "ext": ext,
            "parent": parent_of(node.path).rsplit("/", 1)[-1],
            "path": node.path,
            "size": node.size,
            "kind": str(node.kind),
            "category": category_of(node),
            "created": node.created,
            "modified": node.modified,
            **self.captures,
        }


class Planner:
    """What the drive will look like once the plan so far has run.

    It answers "is this path free?" and "does this folder exist?" from the
    index plus the plan's own effects, so two files renamed to one name, or
    a move into a folder another step creates, are handled before anything
    is sent.
    """

    def __init__(self, store: Store, *, now: datetime, tz: tzinfo) -> None:
        self.store = store
        self.now = now
        self.tz = tz
        self._claimed: dict[str, str] = {}
        self._vacated: set[str] = set()
        self._folders: set[str] = set()

    async def occupant(self, path: str) -> str | None:
        path = normalize_path(path)
        if path in self._claimed:
            return self._claimed[path]
        if path in self._vacated:
            return None
        node = await self.store.node_at(path)
        return node.file_id if node is not None else None

    async def folder_exists(self, path: str) -> bool:
        path = normalize_path(path)
        if path == "/" or path in self._folders:
            return True
        if path in self._vacated:
            return False
        node = await self.store.node_at(path)
        return node is not None and node.is_folder

    def render(self, text: str, draft: Draft) -> str:
        return templates.render(text, draft.values(), tz=self.tz)

    def claim(self, path: str, file_id: str) -> None:
        self._claimed[normalize_path(path)] = file_id

    def vacate(self, path: str) -> None:
        path = normalize_path(path)
        self._claimed.pop(path, None)
        self._vacated.add(path)

    async def need_folder(self, path: str, rule_name: str) -> list[Action]:
        """A create_folder action when ``path`` is not there yet (and will not be)."""
        if await self.folder_exists(path):
            return []
        level = normalize_path(path)
        while level != "/":
            self._folders.add(level)
            level = parent_of(level)
        return [
            Action(
                ActionType.CREATE_FOLDER, file_id="", after={"path": normalize_path(path)},
                rule_name=rule_name,
            )
        ]


# ------------------------------------------------------------------ applying


Deliver = Callable[[FileNode, str], Awaitable[dict[str, Any]]]
"""Outbound: fetch one file to the configured destination; returns audit extras."""


@dataclass
class Runtime:
    client: WmsClient
    store: Store
    deliver: Deliver | None = None

    async def folder_id(self, path: str, *, create: bool) -> str:
        path = normalize_path(path)
        if path == "/":
            return ROOT_ID
        known = await self.store.node_at(path)
        if known is not None and known.is_folder:
            return known.file_id
        if not create:
            raise WmsError(f"{path} does not exist", key="error.no_folder", path=path)
        chain = await self.client.ensure_folder_chain(path)
        parent = ROOT_ID
        for level_path, level_id in chain:
            if await self.store.node(level_id) is None:
                await self.store.insert(
                    FileNode(
                        file_id=level_id, parent_id=parent, name=level_path.rsplit("/", 1)[-1],
                        kind=Kind.FOLDER, path=level_path,
                        # Blank until a stocktake lists it (see ops.stocktake).
                        modified_time=None,
                    )
                )
            parent = level_id
        return parent


# The outcome of check(): run it, or why not.
DUE, DONE, GONE, CHANGED = "due", "done", "gone", "changed"


class Primitive:
    type: ActionType
    batch = False
    """True when PikPak takes many ids in one request."""

    def batch_key(self, action: Action) -> tuple[Any, ...] | None:
        return (self.type,) if self.batch else None

    async def plan(self, spec: Any, draft: Draft, planner: Planner, rule: str) -> list[Action]:
        raise NotImplementedError

    async def check(self, action: Action, store: Store) -> str:
        node = await store.node(action.file_id)
        if node is None:
            return GONE
        if node.path != action.before.get("path"):
            return CHANGED
        return DUE

    async def apply(self, actions: list[Action], rt: Runtime) -> list[dict[str, Any]]:
        raise NotImplementedError

    def inverse(self, entry: dict[str, Any]) -> Action:
        raise Refused(f"{self.type} cannot be undone", key="undo.refused.generic",
                      action=str(self.type))


def _snapshot(draft: Draft) -> dict[str, Any]:
    return draft.node.snapshot()


class Rename(Primitive):
    type = ActionType.RENAME

    async def plan(self, spec: RenameSpec, draft, planner, rule):
        new = planner.render(spec.template, draft).strip()
        if not new or "/" in new:
            raise Conflict("conflict.bad_name", name=new)
        node = draft.node
        if new == node.name:
            return []
        new_path = join_path(parent_of(node.path), new)
        occupant = await planner.occupant(new_path)
        if occupant not in (None, node.file_id):
            raise Conflict("conflict.taken", path=new_path)
        action = Action(self.type, node.file_id, before=_snapshot(draft),
                        after={"name": new, "path": new_path}, rule_name=rule)
        planner.vacate(node.path)
        planner.claim(new_path, node.file_id)
        draft.node = replace(node, name=new, path=new_path)
        return [action]

    async def check(self, action, store):
        node = await store.node(action.file_id)
        if node is None:
            return GONE
        if node.path == action.after.get("path"):
            return DONE
        return DUE if node.path == action.before.get("path") else CHANGED

    async def apply(self, actions, rt):
        (action,) = actions
        await rt.client.rename(action.file_id, action.after["name"])
        await rt.store.relocate(action.file_id, parent_id=action.before["parent_id"],
                                path=action.after["path"])
        return [{}]

    def inverse(self, entry):
        before, after = entry["before"], entry["after"]
        return Action(
            self.type, entry["file_id"],
            before={**before, "name": after["name"], "path": after["path"]},
            after={"name": before["name"], "path": before["path"]},
            rule_name="undo",
        )


class Move(Primitive):
    type = ActionType.MOVE
    batch = True

    def batch_key(self, action):
        return (self.type, action.after.get("parent_path"))

    async def plan(self, spec: MoveSpec, draft, planner, rule):
        node = draft.node
        dest = normalize_path(planner.render(spec.to, draft))
        if dest == parent_of(node.path):
            return []
        if dest == node.path or dest.startswith(node.path + "/"):
            raise Conflict("conflict.into_itself", path=node.path)
        new_path = join_path(dest, node.name)
        occupant = await planner.occupant(new_path)
        if occupant not in (None, node.file_id):
            raise Conflict("conflict.taken", path=new_path)
        if not spec.create_missing and not await planner.folder_exists(dest):
            raise Conflict("conflict.no_folder", path=dest)
        actions = await planner.need_folder(dest, rule)
        actions.append(
            Action(self.type, node.file_id, before=_snapshot(draft),
                   after={"path": new_path, "parent_path": dest,
                          "create_missing": spec.create_missing},
                   rule_name=rule)
        )
        planner.vacate(node.path)
        planner.claim(new_path, node.file_id)
        draft.node = replace(node, path=new_path)
        return actions

    async def check(self, action, store):
        return await Rename.check(self, action, store)  # same test: before or after path

    async def apply(self, actions, rt):
        dest = actions[0].after["parent_path"]
        folder = await rt.folder_id(dest, create=bool(actions[0].after.get("create_missing")))
        for start in range(0, len(actions), BATCH_LIMIT):
            chunk = actions[start : start + BATCH_LIMIT]
            await rt.client.move([a.file_id for a in chunk], folder)
            for action in chunk:
                await rt.store.relocate(action.file_id, parent_id=folder,
                                        path=action.after["path"])
        return [{"parent_id": folder} for _ in actions]

    def inverse(self, entry):
        before, after = entry["before"], entry["after"]
        return Action(
            self.type, entry["file_id"],
            before={**before, "path": after["path"]},
            after={"path": before["path"], "parent_path": parent_of(before["path"]),
                   "create_missing": True},
            rule_name="undo",
        )


class Copy(Primitive):
    type = ActionType.COPY
    batch = True

    def batch_key(self, action):
        return (self.type, action.after.get("parent_path"))

    async def plan(self, spec: MoveSpec, draft, planner, rule):
        node = draft.node
        dest = normalize_path(planner.render(spec.to, draft))
        new_path = join_path(dest, node.name)
        if await planner.occupant(new_path) is not None:
            return []  # a copy is already there: nothing to do (rule 5)
        if not spec.create_missing and not await planner.folder_exists(dest):
            raise Conflict("conflict.no_folder", path=dest)
        actions = await planner.need_folder(dest, rule)
        actions.append(
            Action(self.type, node.file_id, before=_snapshot(draft),
                   after={"path": new_path, "parent_path": dest,
                          "create_missing": spec.create_missing},
                   rule_name=rule)
        )
        planner.claim(new_path, f"copy-of-{node.file_id}")
        return actions

    async def check(self, action, store):
        if await store.node_at(action.after["path"]) is not None:
            return DONE
        return await super().check(action, store)

    async def apply(self, actions, rt):
        dest = actions[0].after["parent_path"]
        folder = await rt.folder_id(dest, create=bool(actions[0].after.get("create_missing")))
        for start in range(0, len(actions), BATCH_LIMIT):
            chunk = actions[start : start + BATCH_LIMIT]
            await rt.client.copy([a.file_id for a in chunk], folder)
        # The copies' ids are not returned; the next stocktake indexes them.
        return [{"parent_id": folder} for _ in actions]

    def inverse(self, entry):
        path = entry["after"].get("path")
        raise Refused(f"a copy cannot be undone automatically: trash {path} by hand",
                      key="undo.refused.copy", path=path)


class Trash(Primitive):
    type = ActionType.TRASH
    batch = True

    async def plan(self, spec, draft, planner, rule):
        planner.vacate(draft.node.path)
        draft.gone = True
        return [Action(self.type, draft.node.file_id, before=_snapshot(draft), rule_name=rule)]

    async def check(self, action, store):
        node = await store.node(action.file_id)
        if node is None:
            return DONE
        return DUE if node.path == action.before.get("path") else CHANGED

    async def apply(self, actions, rt):
        ids = [a.file_id for a in actions]
        for start in range(0, len(ids), BATCH_LIMIT):
            await rt.client.trash(ids[start : start + BATCH_LIMIT])
        await rt.store.forget(ids)
        return [{} for _ in actions]

    def inverse(self, entry):
        return Action(ActionType.UNTRASH, entry["file_id"], before=entry["before"],
                      after={"path": entry["before"].get("path")}, rule_name="undo")


class Untrash(Primitive):
    type = ActionType.UNTRASH
    batch = True

    async def check(self, action, store):
        return DONE if await store.node(action.file_id) is not None else DUE

    async def apply(self, actions, rt):
        await rt.client.untrash([a.file_id for a in actions])
        for action in actions:
            node = FileNode.from_snapshot(action.before)
            parent = await rt.store.node_at(parent_of(node.path))
            node.parent_id = parent.file_id if parent is not None else ROOT_ID
            if node.is_folder:
                # Its contents left the index with it; a blank modified_time
                # makes the next incremental stocktake list it again.
                node.modified_time = None
            await rt.store.insert(node)
        return [{} for _ in actions]

    def inverse(self, entry):
        return Action(ActionType.TRASH, entry["file_id"], before=entry["before"],
                      rule_name="undo")


class DeleteForever(Primitive):
    """Only ever from ``wms cleanup --forever``; never from a rules file."""

    type = ActionType.DELETE_FOREVER
    batch = True

    check = Trash.check

    async def apply(self, actions, rt):
        ids = [a.file_id for a in actions]
        await rt.client.delete_forever(ids)
        await rt.store.forget(ids)
        return [{} for _ in actions]

    def inverse(self, entry):
        raise Refused("a permanent deletion cannot be undone", key="undo.refused.forever")


class Star(Primitive):
    type = ActionType.STAR
    batch = True

    async def plan(self, spec, draft, planner, rule):
        return [Action(self.type, draft.node.file_id, before=_snapshot(draft), rule_name=rule)]

    async def check(self, action, store):
        # The index does not hold stars; starring twice is harmless.
        return DUE if await store.node(action.file_id) is not None else GONE

    async def apply(self, actions, rt):
        await rt.client.star([a.file_id for a in actions])
        return [{} for _ in actions]

    def inverse(self, entry):
        return Action(ActionType.UNSTAR, entry["file_id"], before=entry["before"],
                      rule_name="undo")


class Unstar(Star):
    type = ActionType.UNSTAR

    async def apply(self, actions, rt):
        await rt.client.unstar([a.file_id for a in actions])
        return [{} for _ in actions]

    def inverse(self, entry):
        return Action(ActionType.STAR, entry["file_id"], before=entry["before"],
                      rule_name="undo")


class Share(Primitive):
    type = ActionType.SHARE

    async def plan(self, spec: ShareSpec, draft, planner, rule):
        return [Action(self.type, draft.node.file_id, before=_snapshot(draft),
                       after={"need_password": spec.need_password, "days": spec.days},
                       rule_name=rule)]

    check = Star.check

    async def apply(self, actions, rt):
        (action,) = actions
        result = await rt.client.share(
            [action.file_id], need_password=bool(action.after.get("need_password")),
            days=int(action.after.get("days", -1)),
        )
        return [{"share_url": result.get("share_url", ""),
                 "pass_code": result.get("pass_code", "")}]

    def inverse(self, entry):
        raise Refused("PikPak has no call to cancel a share", key="undo.refused.share")


class CreateFolder(Primitive):
    type = ActionType.CREATE_FOLDER

    async def plan(self, spec: CreateFolderSpec, draft, planner, rule):
        return await planner.need_folder(planner.render(spec.path, draft), rule)

    async def check(self, action, store):
        node = await store.node_at(action.after["path"])
        return DONE if node is not None and node.is_folder else DUE

    async def apply(self, actions, rt):
        (action,) = actions
        path = action.after["path"]
        # The index may just be behind: find out whether this really creates
        # anything, so an undo never trashes a folder that was already there.
        try:
            await rt.client.resolve_path(path)
            existed = True
        except NotFoundError:
            existed = False
        folder = await rt.folder_id(path, create=True)
        action.file_id = folder
        return [{"existed": existed}]

    def inverse(self, entry):
        if entry["after"].get("existed"):
            raise Refused("the folder existed before", key="undo.refused.existed",
                          path=entry["after"].get("path"))
        return Action(ActionType.TRASH, entry["file_id"],
                      before={"path": entry["after"].get("path"), "kind": "folder",
                              "file_id": entry["file_id"]},
                      rule_name="undo")


class Outbound(Primitive):
    type = ActionType.OUTBOUND

    async def plan(self, spec: OutboundSpec, draft, planner, rule):
        if draft.node.is_folder:
            raise Conflict("conflict.outbound_folder", path=draft.node.path)
        to = planner.render(spec.to, draft).strip("/") if spec.to else ""
        return [Action(self.type, draft.node.file_id, before=_snapshot(draft),
                       after={"to": to}, rule_name=rule)]

    check = Star.check

    async def apply(self, actions, rt):
        (action,) = actions
        if rt.deliver is None:
            raise WmsError("no outbound destination is configured", key="error.no_outbound")
        node = await rt.store.node(action.file_id) or FileNode.from_snapshot(action.before)
        return [await rt.deliver(node, action.after.get("to", ""))]

    def inverse(self, entry):
        raise Refused("an outbound fetch cannot be undone", key="undo.refused.outbound")


class Inbound(Primitive):
    """Recorded by ops.inbound for the audit; never planned from rules."""

    type = ActionType.INBOUND

    def inverse(self, entry):
        path = entry["after"].get("path")
        raise Refused(f"trash {path} by hand to undo an inbound",
                      key="undo.refused.inbound", path=path)


PRIMITIVES: dict[ActionType, Primitive] = {
    p.type: p
    for p in (
        Rename(), Move(), Copy(), Trash(), Untrash(), DeleteForever(), Star(), Unstar(),
        Share(), CreateFolder(), Outbound(), Inbound(),
    )
}

STEP_TYPES: dict[str, ActionType] = {
    "rename": ActionType.RENAME,
    "move": ActionType.MOVE,
    "copy": ActionType.COPY,
    "trash": ActionType.TRASH,
    "star": ActionType.STAR,
    "share": ActionType.SHARE,
    "create_folder": ActionType.CREATE_FOLDER,
    "outbound": ActionType.OUTBOUND,
}
"""Rules-file verbs to primitives (see schema.SPECS)."""
