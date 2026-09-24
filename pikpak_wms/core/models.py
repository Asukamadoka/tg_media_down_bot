"""Domain models. ``ops``, ``rules`` and ``store`` see these, never raw SDK dicts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

ROOT_ID = ""
"""PikPak addresses the drive root with an empty parent id."""


class Kind(StrEnum):
    FILE = "file"
    FOLDER = "folder"


class ActionType(StrEnum):
    RENAME = "rename"
    MOVE = "move"
    COPY = "copy"
    TRASH = "trash"
    DELETE_FOREVER = "delete_forever"
    STAR = "star"
    SHARE = "share"
    CREATE_FOLDER = "create_folder"
    UNTRASH = "untrash"


@dataclass(slots=True)
class FileNode:
    """One entry of the drive, as the local index holds it."""

    file_id: str
    parent_id: str
    name: str
    kind: Kind
    path: str = ""
    size: int = 0
    mime: str = ""
    hash: str = ""
    created_time: str | None = None
    modified_time: str | None = None
    synced_at: str | None = None

    @property
    def is_folder(self) -> bool:
        return self.kind is Kind.FOLDER

    @property
    def extension(self) -> str:
        stem, dot, ext = self.name.rpartition(".")
        return ext if dot and stem else ""

    @property
    def created(self) -> datetime | None:
        return parse_time(self.created_time)

    @property
    def modified(self) -> datetime | None:
        return parse_time(self.modified_time)

    @classmethod
    def from_api(cls, raw: dict[str, Any], *, parent_path: str) -> FileNode:
        """Build a node from one entry of PikPak's ``files`` listing."""
        kind = Kind.FOLDER if raw.get("kind") == "drive#folder" else Kind.FILE
        name = str(raw.get("name") or "")
        try:
            size = int(raw.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        return cls(
            file_id=str(raw.get("id") or ""),
            parent_id=str(raw.get("parent_id") or ROOT_ID),
            name=name,
            kind=kind,
            path=join_path(parent_path, name),
            size=size,
            mime=str(raw.get("mime_type") or ""),
            hash=str(raw.get("hash") or ""),
            created_time=raw.get("created_time") or None,
            modified_time=raw.get("modified_time") or None,
        )


def join_path(parent: str, name: str) -> str:
    return "/" + "/".join(part for part in (*parent.split("/"), name) if part)


def normalize_path(path: str) -> str:
    """``/a/b``: one leading slash, no trailing one, no empty parts."""
    return "/" + "/".join(part for part in (path or "").split("/") if part)


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass(slots=True)
class Quota:
    used: int
    limit: int
    in_trash: int = 0

    @property
    def free(self) -> int:
        return max(self.limit - self.used, 0)


@dataclass(slots=True)
class Action:
    """One atomic change. Every action primitive can produce this without
    touching the network, which is what dry-run rests on (rule 1)."""

    type: ActionType
    file_id: str
    before: dict[str, Any] = field(default_factory=dict)
    after: dict[str, Any] = field(default_factory=dict)
    rule_name: str = ""

    def describe(self) -> str:
        """One line for a person to read, in the WMS language."""
        from ..i18n import t  # the catalogue imports nothing from here

        before, after = self.before, self.after
        path = before.get("path") or after.get("path") or self.file_id
        if self.type is ActionType.RENAME:
            return t("action.rename", old=before.get("name"), new=after.get("name"))
        if self.type in (ActionType.MOVE, ActionType.COPY):
            return t(f"action.{self.type}", old=before.get("path"), new=after.get("path"))
        if self.type is ActionType.CREATE_FOLDER:
            return t("action.create_folder", path=after.get("path"))
        key = f"action.{self.type}"
        if self.type in (
            ActionType.TRASH,
            ActionType.UNTRASH,
            ActionType.STAR,
            ActionType.SHARE,
            ActionType.DELETE_FOREVER,
        ):
            return t(key, path=path)
        return t("action.other", action=str(self.type), path=path)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["type"] = str(self.type)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Action:
        return cls(
            type=ActionType(data["type"]),
            file_id=str(data.get("file_id", "")),
            before=dict(data.get("before") or {}),
            after=dict(data.get("after") or {}),
            rule_name=str(data.get("rule_name", "")),
        )


@dataclass(slots=True)
class Plan:
    """The result of evaluating rules: what would change, before it does.

    The CLI, the Mini App panel and the bot all show this one object, then
    apply it on confirmation. It is plain data, so it can be stored.
    """

    actions: list[Action] = field(default_factory=list)
    source: str = ""
    generated_at: str | None = None
    notes: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.actions)

    @property
    def is_empty(self) -> bool:
        return not self.actions

    def to_dict(self) -> dict[str, Any]:
        return {
            "actions": [action.to_dict() for action in self.actions],
            "source": self.source,
            "generated_at": self.generated_at,
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Plan:
        return cls(
            actions=[Action.from_dict(item) for item in data.get("actions") or []],
            source=str(data.get("source", "")),
            generated_at=data.get("generated_at"),
            notes=list(data.get("notes") or []),
        )
