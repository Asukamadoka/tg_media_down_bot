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
from datetime import datetime, timedelta, tzinfo
from typing import Any, Literal
from zoneinfo import ZoneInfo

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
    corrections: list[str] = Field(default_factory=list, exclude=True)
    """Catalogue keys of what was put right after the model answered (the time
    direction, M8.2 §C), shown in the plan. Not part of the wire format."""

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


_NULLISH = frozenset({"null", "none", ""})

DEFAULT_TZ = "Asia/Shanghai"

# Words a model puts where a time belongs. Each stands for the START of the
# period it names, in the configured time zone: 「今天」 is today 00:00,
# 「本月」 the 1st, 「去年」 January 1st of last year. As an upper bound the same
# value reads "before that period began".
_PERIODS: dict[str, str] = {
    **dict.fromkeys(("today", "今天", "今日"), "today"),
    **dict.fromkeys(("yesterday", "昨天", "昨日"), "yesterday"),
    **dict.fromkeys(("tomorrow", "明天", "明日"), "tomorrow"),
    **dict.fromkeys(("this week", "本周", "这周", "这个星期", "本星期"), "week"),
    **dict.fromkeys(("last week", "上周", "上个星期", "上星期"), "last_week"),
    **dict.fromkeys(("this month", "本月", "这个月", "当月"), "month"),
    **dict.fromkeys(("last month", "上个月", "上月"), "last_month"),
    **dict.fromkeys(("this year", "今年", "本年"), "year"),
    **dict.fromkeys(("last year", "去年"), "last_year"),
}
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NAIVE_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?$")
_MONTHS_YEARS = re.compile(r"^(\d+(?:\.\d+)?)\s*([my])$", re.IGNORECASE)


def period_start(word: str, now: datetime, tz: tzinfo) -> datetime | None:
    """The start of the period ``word`` names (``today``, ``本周`` …), or None."""
    name = _PERIODS.get(word.strip().lower())
    if name is None:
        return None
    today = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    if name == "today":
        return today
    if name == "yesterday":
        return today - timedelta(days=1)
    if name == "tomorrow":
        return today + timedelta(days=1)
    if name == "week":
        return today - timedelta(days=today.weekday())
    if name == "last_week":
        return today - timedelta(days=today.weekday() + 7)
    month = today.replace(day=1)
    if name == "month":
        return month
    if name == "last_month":
        return (month - timedelta(days=1)).replace(day=1)
    year = today.replace(month=1, day=1)
    return year if name == "year" else year.replace(year=year.year - 1)


def normalize_time(value: Any, now: datetime, tz: tzinfo) -> Any:
    """One ``created_after`` / ``created_before`` value made to fit.

    A period word becomes that period's start as an ISO time; a bare date,
    or a time without an offset, is read in ``tz``; a month or year count
    (``1m``, ``1y``, which the prompt says to write as days) becomes days.
    Anything else is left for the schema to judge.
    """
    if not isinstance(value, str):
        return value
    text = value.strip()
    moment = period_start(text, now, tz)
    if moment is not None:
        return moment.isoformat(timespec="seconds")
    if _DATE_ONLY.match(text):
        return datetime.fromisoformat(text).replace(tzinfo=tz).isoformat(timespec="seconds")
    if _NAIVE_DATETIME.match(text):
        return (datetime.fromisoformat(text.replace(" ", "T")).replace(tzinfo=tz)
                .isoformat(timespec="seconds"))
    found = _MONTHS_YEARS.match(text)
    if found:
        amount = float(found.group(1)) * (30 if found.group(2).lower() == "m" else 365)
        return f"{int(amount)}d"
    return text


def quartz_to_cron(expression: str) -> str:
    """A Quartz expression (6 or 7 fields: seconds first, an optional year
    last) as five-field cron; ``?`` ("no specific value") becomes ``*``."""
    fields = expression.split()
    if len(fields) in (6, 7):
        fields = fields[1:6]
    return " ".join("*" if field == "?" else field for field in fields)


# Intents for which a destination means nothing: a model fills it in anyway
# (/Trash, /Recycle Bin) and the plan would show a place nothing goes to.
_NO_DESTINATION = frozenset({"trash", "list", "dedupe", "organize_tree", "organize_inbox",
                             "big_report"})


def _category_extensions(kinds: list[Any]) -> set[str]:
    return {ext for kind in kinds if kind in CATEGORIES for ext in CATEGORIES[kind][1]}


def normalize_wire(data: Any, *, now: datetime | None = None,
                   tz: tzinfo | None = None) -> Any:
    """Forgive what small models get wrong before the schema is checked
    (docs/wms/M8 §B, M8.1 §C, M8.2 §B). What it does:

    * the strings "null", "none" and "" mean null;
    * a Quartz cron becomes five-field cron;
    * ``min_size`` / ``max_size`` of 0 mean no limit (``max_size: 0`` would
      otherwise keep only empty files);
    * times: period words, bare dates, and ``1m`` / ``1y`` (:func:`normalize_time`);
    * no destination for intents that have none;
    * ``extensions`` that all belong to the given ``kinds`` are dropped, so a
      format the list forgot (``ass``, ``m2ts``) is not lost.

    Anything still wrong after this fails validation as before.
    """
    if not isinstance(data, dict):
        return data
    zone = tz or ZoneInfo(DEFAULT_TZ)
    instant = now or datetime.now(zone)

    def scalar(value: Any) -> Any:
        if isinstance(value, str) and value.strip().lower() in _NULLISH:
            return None
        return value

    cleaned: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            value = {k: scalar(v) for k, v in value.items()}
        else:
            value = scalar(value)
        cleaned[key] = value

    schedule = cleaned.get("schedule")
    if isinstance(schedule, dict):
        cron = schedule.get("cron")
        if cron is None:
            cleaned["schedule"] = None
        elif isinstance(cron, str):
            cleaned["schedule"] = {**schedule, "cron": quartz_to_cron(cron.strip())}

    filters = cleaned.get("filters")
    if isinstance(filters, dict):
        for field in ("min_size", "max_size"):
            if filters.get(field) == 0 and not isinstance(filters.get(field), bool):
                filters[field] = None
        for field in ("created_after", "created_before"):
            filters[field] = normalize_time(filters.get(field), instant, zone)
        kinds, extensions = filters.get("kinds"), filters.get("extensions")
        if isinstance(kinds, list) and kinds and isinstance(extensions, list) and extensions:
            covered = _category_extensions(kinds)
            if all(isinstance(e, str) and e.lower().lstrip(".") in covered for e in extensions):
                filters["extensions"] = []

    args = cleaned.get("action_args")
    if isinstance(args, dict) and cleaned.get("intent") in _NO_DESTINATION:
        args["dest"] = None
    return cleaned


def from_wire(data: dict[str, Any]) -> Query:
    """A model's answer → Query; nulls mean "not set"."""
    cleaned = copy.deepcopy(data)
    for section in ("scope", "filters", "action_args"):
        if isinstance(cleaned.get(section), dict):
            cleaned[section] = {k: v for k, v in cleaned[section].items() if v is not None}
    return Query.model_validate({k: v for k, v in cleaned.items() if v is not None})
