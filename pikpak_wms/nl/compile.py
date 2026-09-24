"""A :class:`Query` → rules → a proposal a person can confirm.

Everything past this point is M2: the Query becomes ordinary rules (the
mapping of docs/wms/M6 §3), the rules engine plans over the local index,
and the plan goes through the same confirm → apply → audit pipeline. What
this module adds is the explanation: how the sentence was understood (time
zone, which timestamp 转存 means, sizes, kinds, destination) and what it
matches (count, total size, the first few names), stored as catalogue keys
so it is shown in whatever language the reader uses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from typing import Any, Literal

from ..config import Config
from ..core.models import FileNode, Plan, normalize_path, parse_time
from ..rules.engine import evaluate
from ..rules.matcher import Matcher
from ..rules.schema import Rule
from ..rules.units import human_size, parse_moment
from ..store.db import Store
from .query import Query

EXAMPLES = 5
"""How many matching names a proposal shows."""


def _k(key: str, **args: Any) -> dict[str, Any]:
    return {"key": key, "args": args}


# ------------------------------------------------------------------ rules


def name_pattern(query: Query) -> str | None:
    contains = query.filters.name_contains
    regex = query.filters.name_regex
    parts = [f"(?=.*{re.escape(text)})" for text in contains]
    if regex:
        parts.append(f"(?=.*(?:{regex}))")
    if not parts:
        return None
    if len(parts) == 1 and not regex:
        return "(?i)" + re.escape(contains[0])  # file names differ in case
    return ("(?i)" if contains else "") + "^" + "".join(parts)


def _match(query: Query) -> dict[str, Any]:
    f = query.filters
    match: dict[str, Any] = {"kind": "file"}
    if f.kinds:
        match["category"] = list(f.kinds)
    if f.extensions:
        match["extensions"] = list(f.extensions)
    if f.min_size is not None:
        match["min_size"] = f.min_size
    if f.max_size is not None:
        match["max_size"] = f.max_size
    if f.created_after:
        match["newer_than"] = f.created_after
    if f.created_before:
        match["older_than"] = f.created_before
    pattern = name_pattern(query)
    if pattern:
        match["name_regex"] = pattern
    return match


def rules_for(query: Query, config: Config, *, name: str) -> list[Rule]:
    """The M2 rules this Query means. ``list`` has none: it only reads."""
    base: dict[str, Any] = {
        "scope": query.scope.path,
        "recursive": query.scope.recursive,
        "match": _match(query),
    }
    if query.schedule is not None:
        base["schedule"] = {"cron": query.schedule.cron, "apply": False}
    intent, args, nl = query.intent, query.action_args, config.nl

    if intent == "classify":
        targets = {kind: folder for kind, folder in nl.classify.items()
                   if not query.filters.kinds or kind in query.filters.kinds}
        rules = []
        for kind, folder in targets.items():
            match = dict(base["match"], category=[kind],
                         exclude_paths=list(nl.classify.values()))
            rules.append(Rule.model_validate({
                **base, "name": f"{name}-{kind}", "match": match,
                "actions": [{"move": {"to": folder}}],
            }))
        return rules

    if intent == "download":
        to = (args.dest or nl.download_to).strip("/")
        actions: list[Any] = [{"outbound": {"to": to, "via": "local"}}]
    elif intent == "move":
        actions = [{"move": {"to": normalize_path(args.dest or "/")}}]
    elif intent == "rename":
        actions = [{"rename": {"template": args.template}}]
    elif intent == "archive":
        actions = [{"move": {"to": normalize_path(nl.archive_root) + "/{created|date:%Y-%m}"}}]
    elif intent == "trash":
        actions = ["trash"]
    else:
        return []
    return [Rule.model_validate({**base, "name": name, "actions": actions})]


# -------------------------------------------------------------- explaining


def _when(value: str, now: datetime, tz: tzinfo) -> str:
    return parse_moment(value, now=now, tz=tz).astimezone(tz).strftime("%Y-%m-%d %H:%M")


def _span(value: str) -> dict[str, Any] | None:
    found = re.fullmatch(r"(\d+(?:\.\d+)?)([smhdw])", value.strip().lower())
    if not found:
        return None
    amount, unit = found.groups()
    return _k(f"nl.span.{unit}", n=amount.rstrip("0").rstrip(".") if "." in amount else amount)


def explain(query: Query, config: Config, *, now: datetime, tz: tzinfo) -> list[dict[str, Any]]:
    """How the sentence was understood, as stored notes (keys + args)."""
    f, notes = query.filters, []
    notes.append(_k("nl.explain.intent", intent=_k(f"nl.intent.{query.intent}")))
    notes.append(_k("nl.explain.scope" if query.scope.recursive else "nl.explain.scope_flat",
                    path=query.scope.path))
    tz_name = str(tz)
    for field_name, key in (("created_after", "after"), ("created_before", "before")):
        value = getattr(f, field_name)
        if not value:
            continue
        span = _span(value)
        if span is not None:
            notes.append(_k(f"nl.explain.{key}_span", span=span))
        else:
            notes.append(_k(f"nl.explain.{key}_time", when=_when(value, now, tz), tz=tz_name))
    if f.created_after or f.created_before:
        notes.append(_k("nl.explain.time_field", tz=tz_name))
    if f.min_size is not None:
        notes.append(_k("nl.explain.min_size", size=human_size(f.min_size)))
    if f.max_size is not None:
        notes.append(_k("nl.explain.max_size", size=human_size(f.max_size)))
    if f.kinds:
        notes.append(_k("nl.explain.kinds", kinds=[_k(f"nl.kind.{kind}") for kind in f.kinds]))
    if f.extensions:
        notes.append(_k("nl.explain.extensions", extensions=", ".join(f.extensions)))
    if f.name_contains:
        notes.append(_k("nl.explain.name_contains", text="」「".join(f.name_contains)))
    if f.name_regex:
        notes.append(_k("nl.explain.name_regex", regex=f.name_regex))

    args, nl = query.action_args, config.nl
    if query.intent == "download":
        local = config.outbound.local_path
        target = "/".join(p for p in (str(local) if local else "MEDIA_DIR",
                                      (args.dest or nl.download_to).strip("/")) if p)
        notes.append(_k("nl.explain.dest_download", dest=target))
    elif query.intent == "move":
        notes.append(_k("nl.explain.dest_move", dest=normalize_path(args.dest or "/")))
    elif query.intent == "archive":
        notes.append(_k("nl.explain.dest_archive",
                        dest=normalize_path(nl.archive_root) + "/YYYY-MM"))
    elif query.intent == "classify":
        pairs = ", ".join(f"{kind} → {folder}" for kind, folder in nl.classify.items()
                          if not f.kinds or kind in f.kinds)
        notes.append(_k("nl.explain.dest_classify", pairs=pairs))
    elif query.intent == "rename":
        notes.append(_k("nl.explain.rename", template=args.template))
    elif query.intent == "trash":
        notes.append(_k("nl.explain.trash"))
    if query.schedule is not None:
        notes.append(_k("nl.explain.schedule", cron=query.schedule.cron, tz=tz_name))
    return notes


# ---------------------------------------------------------------- proposing


@dataclass
class Proposal:
    kind: Literal["plan", "listing", "rule"]
    query: Query
    notes: list[dict[str, Any]] = field(default_factory=list)
    plan: Plan | None = None
    plan_id: int | None = None
    matches: list[FileNode] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)
    translator: str = ""

    @property
    def count(self) -> int:
        return len(self.matches)

    @property
    def size(self) -> int:
        return sum(node.size for node in self.matches)


async def _matching(store: Store, query: Query, *, now: datetime, tz: tzinfo) -> list[FileNode]:
    from ..rules.schema import Match

    matcher = Matcher(Match.model_validate(_match(query)))
    nodes = await store.nodes_under(query.scope.path)
    if not query.scope.recursive:
        depth = 0 if query.scope.path == "/" else query.scope.path.count("/")
        nodes = [n for n in nodes if n.path.count("/") == depth + 1]
    found = [n for n in nodes if matcher.test(n, now=now, tz=tz) is not None]
    # Newest arrivals first: what a person asking "today's …" expects to see.
    return sorted(found, key=lambda n: parse_time(n.created_time) or now, reverse=True)


async def propose(
    store: Store, query: Query, config: Config, *, now: datetime, tz: tzinfo, name: str,
) -> Proposal:
    """Everything to show before anything happens. Touches only the index."""
    proposal = Proposal(kind="listing", query=query,
                        notes=explain(query, config, now=now, tz=tz))
    matches = await _matching(store, query, now=now, tz=tz)
    rules = rules_for(query, config, name=name)
    if query.schedule is not None:
        proposal.kind, proposal.rules = "rule", rules
    elif rules:
        proposal.kind, proposal.rules = "plan", rules
        plan = await evaluate(rules, store, now=now, tz=tz, source="nl")
        plan.notes = list(proposal.notes) + plan.notes
        proposal.plan = plan
        planned = {a.file_id for a in plan.actions if a.file_id}
        if query.intent == "classify":
            matches = [n for n in matches if n.file_id in planned]
    proposal.matches = matches
    summary = (_k("nl.explain.matched", count=proposal.count, size=human_size(proposal.size))
               if matches else _k("nl.explain.none"))
    examples = ([_k("nl.explain.examples", names=[n.name for n in matches[:EXAMPLES]])]
                if matches else [])
    proposal.notes += [summary, *examples]
    if proposal.plan is not None:
        proposal.plan.notes += [summary, *examples]
    return proposal
