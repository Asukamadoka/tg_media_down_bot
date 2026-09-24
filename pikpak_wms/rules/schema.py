"""The rules file, validated (rule 3: behaviour lives in YAML, not in code).

A rule is a scope, a set of matchers that must all hold, and a list of
actions applied in order to every file that matches. Unknown keys are an
error rather than silently ignored: a typo in a matcher must not widen what
a rule touches.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ..config import rules_path
from ..core.errors import WmsError
from ..core.models import normalize_path
from . import template as templates
from .units import check_moment, parse_size

CATEGORIES: dict[str, tuple[str, set[str]]] = {
    # category: (mime prefix, extensions)
    "video": ("video/", {"mkv", "mp4", "avi", "mov", "wmv", "flv", "webm", "m4v", "ts",
                         "m2ts", "rmvb", "rm", "mpg", "mpeg", "3gp", "vob"}),
    "image": ("image/", {"jpg", "jpeg", "png", "gif", "webp", "bmp", "heic", "heif",
                         "tif", "tiff", "svg", "raw", "dng"}),
    "audio": ("audio/", {"mp3", "flac", "wav", "aac", "m4a", "ogg", "opus", "ape",
                         "wma", "alac"}),
    "document": ("", {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "md",
                      "epub", "mobi", "azw3", "csv", "rtf", "odt"}),
    "archive": ("", {"zip", "rar", "7z", "tar", "gz", "tgz", "bz2", "xz", "iso", "zst"}),
    "subtitle": ("", {"srt", "ass", "ssa", "vtt", "sub", "idx", "sup"}),
}


class RulesError(WmsError):
    """The rules file is missing or invalid."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _listify(value: Any) -> list[str] | None:
    if value is None:
        return None
    items = [value] if isinstance(value, str) else list(value)
    return [str(item) for item in items]


class Match(_Strict):
    """Every matcher given must hold; none given matches everything in scope."""

    kind: Literal["file", "folder"] | None = None
    name_regex: str | None = None
    path_glob: list[str] | None = None
    mime: list[str] | None = None
    min_size: int | None = None
    max_size: int | None = None
    older_than: str | int | datetime | None = None
    newer_than: str | int | datetime | None = None
    # Beyond the original eight:
    extensions: list[str] | None = None
    category: list[str] | None = None
    empty: bool | None = None
    exclude_paths: list[str] | None = None
    """Skip anything at or under these folders (e.g. where a classify rule files things)."""
    time_field: Literal["created", "modified"] = "created"
    """Which drive timestamp older_than / newer_than / {created} compare.
    ``created`` is when the file arrived in the drive (saved, restored or
    finished downloading)."""

    @field_validator("name_regex")
    @classmethod
    def _regex(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError(f"name_regex does not compile: {exc}") from exc
        return value

    @field_validator("exclude_paths", mode="before")
    @classmethod
    def _folders(cls, value: Any) -> list[str] | None:
        items = _listify(value)
        return None if items is None else [normalize_path(item) for item in items]

    @field_validator("path_glob", "mime", mode="before")
    @classmethod
    def _many(cls, value: Any) -> list[str] | None:
        return _listify(value)

    @field_validator("extensions", mode="before")
    @classmethod
    def _extensions(cls, value: Any) -> list[str] | None:
        items = _listify(value)
        return None if items is None else [item.lower().lstrip(".") for item in items]

    @field_validator("category", mode="before")
    @classmethod
    def _category(cls, value: Any) -> list[str] | None:
        items = _listify(value)
        for item in items or []:
            if item not in CATEGORIES:
                raise ValueError(
                    f"unknown category {item!r}; use one of {', '.join(CATEGORIES)}"
                )
        return items

    @field_validator("min_size", "max_size", mode="before")
    @classmethod
    def _size(cls, value: Any) -> int | None:
        return None if value is None else parse_size(value)

    @field_validator("older_than", "newer_than", mode="before")
    @classmethod
    def _moment(cls, value: Any) -> Any:
        return None if value is None else check_moment(value)


class RenameSpec(_Strict):
    template: str

    @field_validator("template")
    @classmethod
    def _template(cls, value: str) -> str:
        if "/" in value:
            raise ValueError("a rename template makes a name, not a path; use move for folders")
        return templates.check(value)


class MoveSpec(_Strict):
    to: str
    create_missing: bool = True

    @field_validator("to")
    @classmethod
    def _to(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError(f"'to' must be an absolute drive path, got {value!r}")
        return templates.check(value)


class NoArgs(_Strict):
    pass


class ShareSpec(_Strict):
    need_password: bool = False
    days: int = -1
    """-1 means the link never expires."""


class CreateFolderSpec(_Strict):
    path: str

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        return templates.check(normalize_path(value))


class OutboundSpec(_Strict):
    to: str = ""
    """A sub-folder under the outbound destination; a template."""

    via: Literal["none", "aria2", "local"] | None = None
    """Override ``outbound.downloader`` for this rule (M6's 下载 means ``local``)."""

    @field_validator("to")
    @classmethod
    def _to(cls, value: str) -> str:
        return templates.check(value.strip("/"))


SPECS: dict[str, type[BaseModel]] = {
    "rename": RenameSpec,
    "move": MoveSpec,
    "copy": MoveSpec,
    "trash": NoArgs,
    "star": NoArgs,
    "share": ShareSpec,
    "create_folder": CreateFolderSpec,
    "outbound": OutboundSpec,
}
"""The action primitives a rule may use. There is deliberately no
``delete_forever``: permanent deletion never comes from a rules file (rule 2)."""


class Step(BaseModel):
    """One entry of ``actions:``, written ``- move: {to: /x}``."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    op: str
    spec: Any

    @model_validator(mode="before")
    @classmethod
    def _one_key(cls, value: Any) -> Any:
        if isinstance(value, Step):
            return value
        if isinstance(value, str):
            value = {value: {}}
        if not isinstance(value, dict) or len(value) != 1:
            raise ValueError("each action is one key, like `- trash: {}` or `- move: {to: /x}`")
        (op, args), = value.items()
        if op not in SPECS:
            raise ValueError(f"unknown action {op!r}; use one of {', '.join(SPECS)}")
        return {"op": op, "spec": SPECS[op].model_validate(args or {})}


class RuleSchedule(_Strict):
    cron: str
    """Five-field cron in ``schedule.timezone``."""

    apply: bool = False
    """False: each run saves a plan to confirm (rule 1). Never permanent deletion."""

    @field_validator("cron")
    @classmethod
    def _cron(cls, value: str) -> str:
        if len(value.split()) != 5:
            raise ValueError(f"not a five-field cron expression: {value!r}")
        return value.strip()


class Rule(_Strict):
    name: str
    enabled: bool = True
    schedule: RuleSchedule | None = None
    """Run this rule on its own cron, besides the organize / cleanup jobs."""
    stage: Literal["organize", "cleanup"] = "organize"
    """``wms organize`` runs organize rules, ``wms cleanup`` cleanup rules."""
    scope: str = "/"
    recursive: bool = True
    match: Match = Field(default_factory=Match)
    actions: list[Step] = Field(min_length=1)

    @field_validator("scope")
    @classmethod
    def _scope(cls, value: str) -> str:
        return normalize_path(value)


class RuleSet(_Strict):
    version: int = 1
    rules: list[Rule] = Field(default_factory=list)

    @field_validator("rules")
    @classmethod
    def _unique(cls, rules: list[Rule]) -> list[Rule]:
        seen: set[str] = set()
        for rule in rules:
            if rule.name in seen:
                raise ValueError(f"two rules are called {rule.name!r}; names must be unique")
            seen.add(rule.name)
        return rules

    def select(
        self, *, stage: str | None = None, names: list[str] | None = None
    ) -> list[Rule]:
        """Enabled rules of one stage, or exactly the rules named (enabled or not)."""
        if names:
            known = {rule.name: rule for rule in self.rules}
            missing = [name for name in names if name not in known]
            if missing:
                raise RulesError(f"no rule called {', '.join(missing)}")
            return [known[name] for name in names]
        return [r for r in self.rules if r.enabled and (stage is None or r.stage == stage)]


def parse_rules(data: Any, *, origin: str = "rules") -> RuleSet:
    try:
        return RuleSet.model_validate(data or {})
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in error['loc'])}: {error['msg']}" for error in exc.errors()
        )
        raise RulesError(f"{origin} is not valid: {problems}") from exc


def load_rules(path: Path | None = None) -> RuleSet:
    target = path or rules_path()
    if not target.exists():
        raise RulesError(
            f"{target} does not exist; copy config/rules.example.yaml there and edit it"
        )
    try:
        data = yaml.safe_load(target.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise RulesError(f"{target} is not valid YAML: {exc}") from exc
    return parse_rules(data, origin=str(target))
