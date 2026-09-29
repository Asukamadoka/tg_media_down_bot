"""Score a translator on the evaluation set (docs/wms/M6 §7).

    python -m pikpak_wms.nl.eval --backend rules
    python -m pikpak_wms.nl.eval --backend claude     # needs ANTHROPIC_API_KEY
    python -m pikpak_wms.nl.eval --backend ollama     # needs OLLAMA_URL
    python -m pikpak_wms.nl.eval --backend openai     # needs NL_OPENAI_BASE_URL, NL_OPENAI_MODEL
    python -m pikpak_wms.nl.eval --backend openai --base-url http://<LAN_IP>:11434/v1

For each case the translator either *handles* it (a Query or a question
back) or declines (``None``: not understood, for another backend). Reported:
coverage (handled / all), accuracy (right / handled), the wrong ones in
full, and the mean latency. ``rules`` must reach ≥ 70 % coverage with zero
wrong; the model backends are for Cowork to measure on the NAS.

For a model it also counts answers outside the schema twice (docs/wms/M8
§B): as they came, and after the lenient normalizing that forgives "null"
strings and Quartz cron. ``--base-url`` measures one ``openai`` host alone.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from ..rules.units import parse_moment
from .query import Clarification, Query
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


def verdict(case: dict[str, Any], result: Query | Clarification | None, now, tz) -> str:
    """``right``, ``wrong`` or ``declined``."""
    if result is None:
        return "declined"
    if case.get("reject"):
        return "right" if isinstance(result, Clarification) else "wrong"
    if case.get("clarify"):
        return "right" if isinstance(result, Clarification) else "wrong"
    if isinstance(result, Clarification):
        return "wrong"
    expected = Query.model_validate(case["expect"]).canonical()
    got = result.canonical()
    return "right" if _normal(expected, now, tz) == _normal(got, now, tz) else "wrong"


@dataclass
class Report:
    backend: str
    total: int = 0
    right: int = 0
    wrong: list[dict[str, Any]] = field(default_factory=list)
    declined: list[str] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    seconds: list[float] = field(default_factory=list)
    invalid_before: int = 0
    invalid_after: int = 0

    @property
    def handled(self) -> int:
        return self.right + len(self.wrong)

    def summary(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "cases": self.total,
            "handled": self.handled,
            "coverage": round(self.handled / self.total, 3) if self.total else 0,
            "right": self.right,
            "wrong": len(self.wrong),
            "accuracy": round(self.right / self.handled, 3) if self.handled else 0,
            "errors": len(self.errors),
            "mean_latency_ms": round(1000 * sum(self.seconds) / len(self.seconds), 1)
            if self.seconds else 0,
            "schema_invalid_as_answered": self.invalid_before,
            "schema_invalid_after_normalizing": self.invalid_after,
        }


async def evaluate(translator: Translator, cases_file: Path = DEFAULT_CASES) -> Report:
    data = yaml.safe_load(cases_file.read_text(encoding="utf-8"))
    tz = ZoneInfo(data["timezone"])
    now = datetime.fromisoformat(data["now"]).astimezone(tz)
    report = Report(backend=getattr(translator, "name", type(translator).__name__))
    for case in data["cases"]:
        report.total += 1
        started = time.perf_counter()
        try:
            result = await translator.translate(case["text"], now, tz)
        except Exception as exc:  # noqa: BLE001 - a backend failing is a result
            report.errors.append({"text": case["text"], "error": f"{type(exc).__name__}: {exc}"})
            report.declined.append(case["text"])
            continue
        finally:
            report.seconds.append(time.perf_counter() - started)
        outcome = verdict(case, result, now, tz)
        if outcome == "right":
            report.right += 1
        elif outcome == "wrong":
            report.wrong.append({
                "text": case["text"],
                "expected": case.get("expect") or ("clarify" if case.get("clarify") else "reject"),
                "got": result.canonical() if isinstance(result, Query)
                else {"clarify": result.question},
            })
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
    parser.add_argument("--base-url", default=None,
                        help="openai only: measure this one host, e.g. http://<LAN_IP>:11434/v1")
    args = parser.parse_args(argv)

    if args.base_url and args.backend != "openai":
        parser.error("--base-url goes with --backend openai")
    if args.base_url:
        translator: Translator = Chain([OpenAITranslator(
            base_url=args.base_url, model=model_for(args.base_url))])
    else:
        translator = build(args.backend, "none", rules_first=args.backend == "rules")
    report = asyncio.run(evaluate(translator, args.cases))
    summary = report.summary()
    if args.json:
        print(json.dumps({**summary, "wrong_cases": report.wrong, "errors": report.errors},
                         ensure_ascii=False, indent=1))
    else:
        for key, value in summary.items():
            print(f"{key:>16}: {value}")
        for item in report.wrong:
            print("WRONG", json.dumps(item, ensure_ascii=False))
        for item in report.errors[:3]:
            print("ERROR", json.dumps(item, ensure_ascii=False))
        if len(report.errors) > 3:
            print(f"... and {len(report.errors) - 3} more errors")
    return 1 if report.wrong else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
