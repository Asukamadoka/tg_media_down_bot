"""The one thing a translator may produce: a validated :class:`Query`.

A sentence goes in, a Query comes out, and nothing else: the translator
never touches PikPak or any ops (docs/wms/M6 §2). The Query then compiles to
the M2 rule matchers and runs through the same plan → confirm → apply →
audit pipeline as everything else.

Times are ISO datetimes (a fixed moment) or durations like ``7d`` (relative
to when the rule runs, which is what a scheduled rule needs). Either way
they compare against when a file *arrived in the drive* (``created_time``),
which is what 转存 / 入库 / 下载到网盘 mean.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..rules import template as templates
from ..rules.schema import CATEGORIES
from ..rules.units import check_moment

Intent = Literal[
    "download", "move", "rename", "classify", "archive", "trash", "list", "schedule",
    # M7: the whole-drive jobs, run on request
    "organize_tree", "organize_inbox", "dedupe", "big_report",
]
TIDY_INTENTS = ("organize_tree", "organize_inbox", "dedupe", "big_report")
Part = Literal["slim", "big", "loose"]
Kind = Literal["video", "image", "audio", "document", "archive", "subtitle"]

INTENTS: tuple[str, ...] = Intent.__args__  # type: ignore[attr-defined]
KINDS: tuple[str, ...] = tuple(CATEGORIES)

_CRON = re.compile(r"^\S+ \S+ \S+ \S+ \S+$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Scope(_Strict):
    path: str = "/"
    recursive: bool = True

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        value = "/" + value.strip().strip("/")
        return value if value != "/" else "/"


class Filters(_Strict):
    created_after: str | None = None
    created_before: str | None = None
    min_size: int | None = None
    max_size: int | None = None
    kinds: list[Kind] = Field(default_factory=list)
    extensions: list[str] = Field(default_factory=list)
    name_contains: list[str] = Field(default_factory=list)
    name_regex: str | None = None

    @field_validator("created_after", "created_before")
    @classmethod
    def _moment(cls, value: str | None) -> str | None:
        return None if value is None else str(check_moment(value))

    @field_validator("min_size", "max_size")
    @classmethod
    def _size(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("a size cannot be negative")
        return value

    @field_validator("extensions")
    @classmethod
    def _extensions(cls, value: list[str]) -> list[str]:
        return [item.lower().lstrip(".") for item in value if item.strip(" .")]

    @field_validator("name_regex")
    @classmethod
    def _regex(cls, value: str | None) -> str | None:
        if value is not None:
            re.compile(value)
        return value

    @property
    def empty(self) -> bool:
        return self == Filters()


class ActionArgs(_Strict):
    dest: str | None = None
    template: str | None = None
    part: Part | None = None
    """organize_tree only: one part of it (``big``: 「把大文件单独放一起」)."""

    @field_validator("template")
    @classmethod
    def _template(cls, value: str | None) -> str | None:
        return None if value is None else templates.check(value)


class Schedule(_Strict):
    cron: str

    @field_validator("cron")
    @classmethod
    def _cron(cls, value: str) -> str:
        if not _CRON.match(value.strip()):
            raise ValueError(f"not a five-field cron expression: {value!r}")
        return value.strip()


class Query(_Strict):
    intent: Intent
    scope: Scope = Field(default_factory=Scope)
    filters: Filters = Field(default_factory=Filters)
    action_args: ActionArgs = Field(default_factory=ActionArgs)
    schedule: Schedule | None = None
    needs_clarification: str | None = None

    @model_validator(mode="after")
    def _schedule_needs_an_action(self) -> Query:
        # A schedule says *when*; the intent must still say *what*.
        if self.schedule is not None and self.intent in ("schedule", "list"):
            self.needs_clarification = self.needs_clarification or "nl.ask.schedule_what"
        # The M7 jobs take a folder at most: they already run on their own
        # schedule, and a filter would change what "tidy" means.
        if self.intent in TIDY_INTENTS and (self.schedule is not None or not self.filters.empty):
            self.needs_clarification = self.needs_clarification or "nl.ask.tidy_plain"
        return self

    def canonical(self) -> dict[str, Any]:
        """Only what is set, for comparing results (the eval) and for display."""
        data = self.model_dump(exclude_defaults=True)
        data["intent"] = self.intent
        return data


class Clarification(_Strict):
    """The sentence cannot be turned into a Query without guessing."""

    question: str
    """A catalogue key (``nl.ask.*``) or, from a model, the question itself."""

    args: dict[str, Any] = Field(default_factory=dict)


def as_result(query: Query) -> Query | Clarification:
    if query.needs_clarification:
        return Clarification(question=query.needs_clarification)
    return query


# ------------------------------------------------------------ wire schema


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}


WIRE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["intent", "scope", "filters", "action_args", "schedule", "needs_clarification"],
    "properties": {
        "intent": {"type": "string", "enum": list(INTENTS)},
        "scope": {
            "type": "object",
            "additionalProperties": False,
            "required": ["path", "recursive"],
            "properties": {"path": {"type": "string"}, "recursive": {"type": "boolean"}},
        },
        "filters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["created_after", "created_before", "min_size", "max_size", "kinds",
                         "extensions", "name_contains", "name_regex"],
            "properties": {
                "created_after": _nullable({"type": "string"}),
                "created_before": _nullable({"type": "string"}),
                "min_size": _nullable({"type": "integer"}),
                "max_size": _nullable({"type": "integer"}),
                "kinds": {"type": "array", "items": {"type": "string", "enum": list(KINDS)}},
                "extensions": {"type": "array", "items": {"type": "string"}},
                "name_contains": {"type": "array", "items": {"type": "string"}},
                "name_regex": _nullable({"type": "string"}),
            },
        },
        "action_args": {
            "type": "object",
            "additionalProperties": False,
            "required": ["dest", "template", "part"],
            "properties": {
                "dest": _nullable({"type": "string"}),
                "template": _nullable({"type": "string"}),
                "part": _nullable({"type": "string", "enum": ["slim", "big", "loose"]}),
            },
        },
        "schedule": _nullable({
            "type": "object",
            "additionalProperties": False,
            "required": ["cron"],
            "properties": {"cron": {"type": "string"}},
        }),
        "needs_clarification": _nullable({"type": "string"}),
    },
}
"""What a model backend must return: every field present (nullable where
optional), so constrained decoding can enforce it. Pydantic re-validates it."""


def wire_schema() -> dict[str, Any]:
    return copy.deepcopy(WIRE_SCHEMA)


def from_wire(data: dict[str, Any]) -> Query:
    """A model's answer → Query; nulls mean "not set"."""
    cleaned = copy.deepcopy(data)
    for section in ("scope", "filters", "action_args"):
        if isinstance(cleaned.get(section), dict):
            cleaned[section] = {k: v for k, v in cleaned[section].items() if v is not None}
    return Query.model_validate({k: v for k, v in cleaned.items() if v is not None})
