"""Score a translator on the evaluation set (docs/wms/M6 §7).

    python -m pikpak_wms.nl.eval --backend rules
    python -m pikpak_wms.nl.eval --backend claude     # needs ANTHROPIC_API_KEY
    python -m pikpak_wms.nl.eval --backend ollama     # needs OLLAMA_URL
    python -m pikpak_wms.nl.eval --backend openai     # needs NL_OPENAI_BASE_URL, NL_OPENAI_MODEL
    python -m pikpak_wms.nl.eval --backend openai --base-url http://10.10.10.1:11434/v1
    python -m pikpak_wms.nl.eval --backend openai --with-rules   # as the bot runs it

For each case the translator either *handles* it (a Query or a question
back) or declines (``None``: not understood, for another backend). Reported:
coverage (handled / all), accuracy (right / handled), the wrong ones in
full, and the mean latency. ``rules`` must reach ≥ 70 % coverage with zero
wrong; the model backends are for Cowork to measure on the NAS.

For a model it also counts answers outside the schema twice (docs/wms/M8
§B): as they came, and after the lenient normalizing that forgives "null"
strings and Quartz cron. ``--base-url`` measures one ``openai`` host alone.

Scoring is by meaning (docs/wms/M8.2 §A): ``right`` is the same Query,
``equivalent`` the same meaning written another way (a date without a time,
``7d`` for ``1w``, a forgotten extension the kinds already cover …), ``wrong``
anything else. ``dangerous`` counts, among the wrong, the answers that would
hurt: a time pointing the wrong way for a trash or archive, ``max_size: 0``,
or a destructive query for a sentence that should have been refused.

Dates in the cases are templates (``{today}``, ``{yesterday}``,
``{week_start}``, ``{month_start}``, ``{last_month_start}``, ``{year_start}``,
``{last_year_start}``, ``{year}``, ``{month}``, ``{mm}``), filled in for
``--now``; the command defaults to the current time, in the cases' time zone.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from ..rules.schema import CATEGORIES
from ..rules.units import parse_moment
from .hosts import BOARD
from .query import Clarification, Query, Remark, period_start
from .rules_parser import RulesTranslator
from .translator import Chain, OpenAITranslator, Translator, build

DEFAULT_CASES = Path("tests/nl/cases.yaml")


def _normal(value: Any, now: datetime, tz) -> Any:
    """Order-free lists, and times compared as instants."""
    if isinstance(value, dict):
        return {k: _normal(v, now, tz) for k, v in value.items()}
    if isinstance(value, list):
        return sorted(_normal(v, now, tz) for v in value)
    if isinstance(value, str) and len(value) >= 10 and value[4] == "-":
        try:
            return parse_moment(value, now=now, tz=tz).isoformat()
        except ValueError:
            return value
    return value


# ------------------------------------------------------------- templates

_TEMPLATE = re.compile(r"\{(\w+)\}")


def template_values(now: datetime, tz: tzinfo) -> dict[str, str]:
    """What ``{today}``, ``{month_start}`` … stand for at ``now``."""
    def iso(word: str) -> str:
        moment = period_start(word, now, tz)
        assert moment is not None
        return moment.isoformat(timespec="seconds")

    local = now.astimezone(tz)
    return {
        "today": iso("today"), "yesterday": iso("yesterday"), "tomorrow": iso("tomorrow"),
        "week_start": iso("this week"), "last_week_start": iso("last week"),
        "month_start": iso("this month"), "last_month_start": iso("last month"),
        "year_start": iso("this year"), "last_year_start": iso("last year"),
        "year": f"{local.year}", "month": f"{local.month}", "mm": f"{local.month:02d}",
    }


def expand(value: Any, values: dict[str, str]) -> Any:
    """``value`` with every known ``{name}`` filled in; unknown ones stay."""
    if isinstance(value, str):
        return _TEMPLATE.sub(lambda m: values.get(m.group(1), m.group(0)), value)
    if isinstance(value, list):
        return [expand(item, values) for item in value]
    if isinstance(value, dict):
        return {key: expand(item, values) for key, item in value.items()}
    return value


def load_cases(cases_file: Path, *, now: datetime | None = None) -> tuple[
        list[dict[str, Any]], datetime, ZoneInfo]:
    """(the cases with their templates filled in, now, the time zone)."""
    data = yaml.safe_load(cases_file.read_text(encoding="utf-8"))
    tz = ZoneInfo(data["timezone"])
    moment = (now or datetime.fromisoformat(data["now"])).astimezone(tz)
    return expand(data["cases"], template_values(moment, tz)), moment, tz


# ---------------------------------------------------------- equivalence

_SPAN = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdwmy])\s*$", re.IGNORECASE)
_SPAN_DAYS = {"s": 1 / 86400, "h": 1 / 24, "d": 1, "w": 7, "y": 365}
HAS_DESTINATION = ("move", "archive", "download")
"""The intents whose ``dest`` counts; for the rest a model's ``/Trash`` is noise."""


def _span_days(value: str) -> float | None:
    """A duration in days (``1w`` = 7, ``1m`` = 30, as the prompt says), or None."""
    found = _SPAN.match(value or "")
    if not found:
        return None
    amount, unit = float(found.group(1)), found.group(2).lower()
    if unit == "m":
        # The prompt says a month is 30d and offers no minutes; a bare "m" is a month.
        return amount * 30
    return amount * _SPAN_DAYS[unit]


def _moment(value: str, now: datetime, tz: tzinfo) -> str | None:
    try:
        return parse_moment(value, now=now, tz=tz).astimezone(tz).isoformat(timespec="seconds")
    except ValueError:
        return None


def _next_boundary(start: datetime) -> list[datetime]:
    """The natural ends of a period that begins at ``start``: a day, a week
    (when it starts on a Monday), a month, a year."""
    ends = [start + timedelta(days=1)]
    if start.weekday() == 0:
        ends.append(start + timedelta(days=7))
    if start.day == 1:
        months = start.month % 12 + 1
        ends.append(start.replace(year=start.year + (start.month == 12), month=months))
        if start.month == 1:
            ends.append(start.replace(year=start.year + 1))
    # 「当天 23:59:59」 is the same end, written as the last second (M8.3 §D).
    return [*ends, *(end - timedelta(seconds=1) for end in ends)]


def _reduce(query: dict[str, Any], now: datetime, tz: tzinfo) -> dict[str, Any]:
    """A Query's canonical form with the differences of wording taken out."""
    out = json.loads(json.dumps(query))  # a copy
    filters = out.get("filters") or {}
    for key in ("created_after", "created_before"):
        value = filters.get(key)
        if value is None:
            continue
        days = _span_days(value)
        filters[key] = {"days": days} if days is not None else (_moment(value, now, tz) or value)
    kinds = sorted(filters.get("kinds") or [])
    if kinds:
        covered = {e for kind in kinds for e in CATEGORIES[kind][1]} if all(
            kind in CATEGORIES for kind in kinds) else set()
        filters["extensions"] = sorted(e for e in filters.get("extensions") or []
                                       if e not in covered)
        filters["kinds"] = kinds
    for key in ("extensions", "name_contains"):
        if key in filters:
            filters[key] = sorted(filters[key])
    for key in [k for k, v in filters.items() if v in ([], None)]:
        del filters[key]
    if filters:
        out["filters"] = filters
    else:
        out.pop("filters", None)
    args = out.get("action_args") or {}
    if out.get("intent") not in HAS_DESTINATION:
        args.pop("dest", None)
    if args:
        out["action_args"] = args
    else:
        out.pop("action_args", None)
    return out


def _same_time(a: Any, b: Any) -> bool:
    if isinstance(a, dict) and isinstance(b, dict):
        return abs(a["days"] - b["days"]) <= 1  # ±1 day is the same span
    return a == b


def _natural_end(after: Any, before: Any) -> bool:
    """``before`` is exactly where the period ``after`` begins ends: the extra
    upper bound of 「今天」 or 「本月」 that changes nothing."""
    if not (isinstance(after, str) and isinstance(before, str)):
        return False
    try:
        start, end = datetime.fromisoformat(after), datetime.fromisoformat(before)
    except ValueError:
        return False
    return end in _next_boundary(start)


def equivalent(expected: dict[str, Any], got: dict[str, Any], now: datetime, tz: tzinfo) -> bool:
    """The same meaning, however it is written (docs/wms/M8.2 §A1)."""
    want, have = _reduce(expected, now, tz), _reduce(got, now, tz)
    want_f, have_f = want.get("filters", {}), have.get("filters", {})
    if ("created_before" in have_f and "created_before" not in want_f
            and _natural_end(have_f.get("created_after"), have_f["created_before"])):
        have_f = {k: v for k, v in have_f.items() if k != "created_before"}
    for key in ("created_after", "created_before"):
        if (key in want_f) != (key in have_f):
            return False
        if key in want_f and not _same_time(want_f[key], have_f[key]):
            return False
    rest = {k: v for k, v in want_f.items() if not k.startswith("created_")}
    rest_have = {k: v for k, v in have_f.items() if not k.startswith("created_")}
    if rest != rest_have:
        return False
    return ({k: v for k, v in want.items() if k != "filters"}
            == {k: v for k, v in have.items() if k != "filters"})


def is_dangerous(case: dict[str, Any], result: Query | Clarification | Remark | None,
                 now: datetime, tz: tzinfo) -> bool:
    """Would acting on this wrong answer hurt? (docs/wms/M8.2 §A3): a trash or
    archive with its time pointing the other way, ``max_size: 0``, or a
    destructive query where the right answer was to ask or refuse."""
    if not isinstance(result, Query):
        return False
    if case.get("remark"):
        # 「X 下过了」 made into a plan of anything: the inversion of M9.2 §C.2.
        return result.intent in ("download", "trash", "move", "archive")
    filters = result.filters
    if filters.max_size == 0:
        return True
    destructive = result.intent in ("trash", "archive")
    if case.get("reject") or case.get("clarify"):
        return destructive
    # A move with its time the wrong way round shifts the wrong files (M8.3 §E10).
    if not (destructive or result.intent == "move"):
        return False
    wanted = Query.model_validate(case["expect"]).filters
    if wanted.created_before and filters.created_after and not filters.created_before:
        return True
    return bool(wanted.created_after and filters.created_before and not filters.created_after)


def verdict(case: dict[str, Any], result: Query | Clarification | Remark | None, now, tz) -> str:
    """``right``, ``equivalent``, ``wrong`` or ``declined``."""
    if result is None:
        return "declined"
    if case.get("remark"):
        # Only a remark is right: it must never become a plan (M9.2 §C.2).
        want = case["remark"]
        ok = (isinstance(result, Remark) and result.names == want["names"]
              and result.downloaded == want["downloaded"])
        return "right" if ok else "wrong"
    if isinstance(result, Remark):
        return "wrong"
    if case.get("reject") or case.get("clarify"):
        return "right" if isinstance(result, Clarification) else "wrong"
    if isinstance(result, Clarification):
        return "wrong"
    expected = Query.model_validate(case["expect"]).canonical()
    got = result.canonical()
    if _normal(expected, now, tz) == _normal(got, now, tz):
        return "right"
    return "equivalent" if equivalent(expected, got, now, tz) else "wrong"


ERROR_KINDS = ("cut_off", "timeout", "offline", "other")
_KIND_OF_KEY = {"nl.error.cut_off": "cut_off", "nl.error.timeout": "timeout",
                "nl.error.offline": "offline"}


def error_kind(exc: BaseException) -> str:
    return _KIND_OF_KEY.get(getattr(exc, "key", ""), "other")


@dataclass
class Report:
    backend: str
    total: int = 0
    right: int = 0
    equivalent: int = 0
    wrong: list[dict[str, Any]] = field(default_factory=list)
    dangerous: list[dict[str, Any]] = field(default_factory=list)
    declined: list[str] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    """Sentences a backend failed on, each with a ``kind``: ``cut_off``, ``timeout``,
    ``offline`` or ``other`` (M8.3 §A4)."""
    seconds: list[float] = field(default_factory=list)
    invalid_before: int = 0
    invalid_after: int = 0

    @property
    def handled(self) -> int:
        return self.right + self.equivalent + len(self.wrong)

    def summary(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "cases": self.total,
            "handled": self.handled,
            "coverage": round(self.handled / self.total, 3) if self.total else 0,
            "right": self.right,
            "equivalent": self.equivalent,
            "wrong": len(self.wrong),
            "dangerous": len(self.dangerous),
            "accuracy": round(self.right / self.handled, 3) if self.handled else 0,
            "equivalent_accuracy": round((self.right + self.equivalent) / self.handled, 3)
            if self.handled else 0,
            "errors": len(self.errors),
            "errors_by_kind": {kind: sum(1 for e in self.errors if e["kind"] == kind)
                               for kind in ERROR_KINDS},
            "mean_latency_ms": round(1000 * sum(self.seconds) / len(self.seconds), 1)
            if self.seconds else 0,
            "schema_invalid_as_answered": self.invalid_before,
            "schema_invalid_after_normalizing": self.invalid_after,
        }


async def evaluate(translator: Translator, cases_file: Path = DEFAULT_CASES, *,
                   now: datetime | None = None) -> Report:
    """Score ``translator``; ``now`` defaults to the cases file's own."""
    cases, now, tz = load_cases(cases_file, now=now)
    report = Report(backend=getattr(translator, "name", type(translator).__name__))
    for case in cases:
        report.total += 1
        # Every sentence starts with what the hosts really are, not with what the one
        # before it taught the board: a single failure must not cost the next ones.
        BOARD.reset()
        started = time.perf_counter()
        try:
            result = await translator.translate(case["text"], now, tz)
        except Exception as exc:  # noqa: BLE001 - a backend failing is a result
            report.errors.append({"text": case["text"], "kind": error_kind(exc),
                                  "error": f"{type(exc).__name__}: {exc}"})
            report.declined.append(case["text"])
            continue
        finally:
            report.seconds.append(time.perf_counter() - started)
        outcome = verdict(case, result, now, tz)
        if outcome == "right":
            report.right += 1
        elif outcome == "equivalent":
            report.equivalent += 1
        elif outcome == "wrong":
            item = {
                "text": case["text"],
                "expected": case.get("expect") or case.get("remark")
                or ("clarify" if case.get("clarify") else "reject"),
                "got": result.canonical() if isinstance(result, Query)
                else {"remark": result.model_dump()} if isinstance(result, Remark)
                else {"clarify": result.question},
            }
            report.wrong.append(item)
            if is_dangerous(case, result, now, tz):
                report.dangerous.append(item)
        else:
            report.declined.append(case["text"])
    for inner in getattr(translator, "translators", [translator]):
        stats = getattr(inner, "stats", None)
        if stats is not None:
            report.invalid_before += stats.invalid_before
            report.invalid_after += stats.invalid_after
    return report


def model_for(base_url: str) -> str:
    """The model NL_OPENAI_MODEL gives this host: its own when NL_OPENAI_BASE_URL
    lists it, otherwise the first (or only) one."""
    urls = [u.strip().rstrip("/") for u in os.environ.get("NL_OPENAI_BASE_URL", "").split(",")]
    models = [m.strip() for m in os.environ.get("NL_OPENAI_MODEL", "").split(",") if m.strip()]
    if not models:
        return ""
    wanted = base_url.strip().rstrip("/")
    if len(models) == len(urls) and wanted in urls:
        return models[urls.index(wanted)]
    return models[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pikpak_wms.nl.eval")
    parser.add_argument("--backend", choices=["rules", "claude", "ollama", "openai"],
                        default="rules")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--with-rules", action="store_true",
                        help="put the rules parser in front of the model, as the bot does"
                             " (default: the model alone, which is what a model score means)")
    parser.add_argument("--now", default=None,
                        help="the moment the date templates and the sentences are read at, ISO;"
                             " default: the current time")
    parser.add_argument("--base-url", default=None,
                        help="openai only: measure this one host, e.g. http://10.10.10.1:11434/v1")
    args = parser.parse_args(argv)

    if args.base_url and args.backend != "openai":
        parser.error("--base-url goes with --backend openai")
    if args.base_url:
        model = OpenAITranslator(base_url=args.base_url, model=model_for(args.base_url))
        translator: Translator = Chain([RulesTranslator(), model] if args.with_rules
                                       else [model])
    else:
        translator = build(args.backend, "none",
                           rules_first=args.backend == "rules" or args.with_rules)
    moment = datetime.fromisoformat(args.now) if args.now else datetime.now(ZoneInfo("UTC"))
    report = asyncio.run(evaluate(translator, args.cases, now=moment))
    summary = report.summary()
    if args.json:
        print(json.dumps({**summary, "wrong_cases": report.wrong,
                          "dangerous_cases": report.dangerous, "errors": report.errors},
                         ensure_ascii=False, indent=1))
    else:
        for key, value in summary.items():
            print(f"{key:>16}: {value}")
        for item in report.wrong:
            print("DANGEROUS" if item in report.dangerous else "WRONG",
                  json.dumps(item, ensure_ascii=False))
        for item in report.errors[:3]:
            print("ERROR", json.dumps(item, ensure_ascii=False))
        if len(report.errors) > 3:
            print(f"... and {len(report.errors) - 3} more errors")
    return 1 if report.wrong else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
