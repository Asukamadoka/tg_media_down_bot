"""The naming template: ``'{show|title}.S{s}E{e}.{ext}'``.

``{field}`` takes a value (a named group of ``name_regex``, or one of the
fields below); ``|filter`` and ``|filter:arg`` transform it, left to right.
``{{`` and ``}}`` are literal braces.

Fields every file has: ``name``, ``stem``, ``ext``, ``parent`` (the folder's
name), ``path``, ``size``, ``kind``, ``category``, ``created``, ``modified``.
Filters: ``title``, ``upper``, ``lower``, ``strip``, ``spaces`` (dots and
underscores become spaces), ``pad2``, ``date:<strftime>``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import datetime, tzinfo
from typing import Any

_FIELD = re.compile(r"\{\{|\}\}|\{([^{}]*)\}")


class TemplateError(ValueError):
    """The template names a field or filter that does not exist."""


def _date(value: Any, arg: str, tz: tzinfo | None) -> str:
    if not isinstance(value, datetime):
        raise TemplateError(f"date needs a time field, got {value!r}")
    moment = value.astimezone(tz) if tz is not None else value
    return moment.strftime(arg or "%Y-%m-%d")


def _pad2(value: Any, _arg: str, _tz: tzinfo | None) -> str:
    text = str(value)
    return text.zfill(2) if text.isdigit() else text


FILTERS: dict[str, Callable[[Any, str, tzinfo | None], str]] = {
    "title": lambda v, _a, _t: str(v).title(),
    "upper": lambda v, _a, _t: str(v).upper(),
    "lower": lambda v, _a, _t: str(v).lower(),
    "strip": lambda v, _a, _t: str(v).strip(" ._-"),
    "spaces": lambda v, _a, _t: re.sub(r"[._]+", " ", str(v)).strip(),
    "pad2": _pad2,
    "date": _date,
}


def fields_of(template: str) -> list[tuple[str, list[tuple[str, str]]]]:
    """``[(field, [(filter, arg), ...]), ...]``; raises on an unknown filter."""
    found = []
    for match in _FIELD.finditer(template):
        body = match.group(1)
        if body is None:
            continue
        field, *chain = [part.strip() for part in body.split("|")]
        if not field:
            raise TemplateError(f"empty field in {template!r}")
        filters = []
        for item in chain:
            name, _, arg = item.partition(":")
            if name not in FILTERS:
                raise TemplateError(f"unknown filter {name!r} in {template!r}")
            filters.append((name, arg))
        found.append((field, filters))
    return found


def check(template: str) -> str:
    """Validate the syntax (not the fields: those depend on the file)."""
    fields_of(template)
    stray = _FIELD.sub("", template)
    if "{" in stray or "}" in stray:
        raise TemplateError(f"unbalanced brace in {template!r}")
    return template


def render(template: str, values: Mapping[str, Any], *, tz: tzinfo | None = None) -> str:
    def one(match: re.Match[str]) -> str:
        whole, body = match.group(0), match.group(1)
        if body is None:
            return whole[0]
        field, *chain = [part.strip() for part in body.split("|")]
        if field not in values or values[field] is None:
            raise TemplateError(f"no value for {{{field}}}")
        value: Any = values[field]
        for item in chain:
            name, _, arg = item.partition(":")
            try:
                value = FILTERS[name](value, arg, tz)
            except KeyError as exc:
                raise TemplateError(f"unknown filter {name!r}") from exc
        if isinstance(value, datetime):
            value = _date(value, "", tz)
        return str(value)

    return _FIELD.sub(one, template)
