"""Natural-language commands (docs/wms/M6): understand, propose, confirm.

    sentence → translator → Query ─┬─ list      → what matches (no changes)
                                   ├─ schedule  → rules for the rules file
                                   └─ otherwise → a stored plan to confirm

The translator only ever produces a Query; everything that changes the
drive goes through :mod:`pikpak_wms.ops.plans` after a person confirms.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from ..core.errors import NotFoundError
from ..core.models import parse_time, render_note
from ..i18n import t
from ..nl.compile import Proposal, propose
from ..nl.hosts import BOARD
from ..nl.query import TIDY_INTENTS, Clarification, Query
from ..nl.translator import OpenAITranslator, Translator, from_environment
from ..rules.schema import Rule
from ..rules.units import human_size
from . import downloads, eventsync, organize, outbound, plans, rulesfile, tidy
from .context import Context
from .stocktake import stocktake


@dataclass
class ModelHost:
    """One NL_OPENAI_BASE_URL host, for /verify and ``wms doctor`` (M8 §A5)."""

    url: str
    model: str
    online: bool | None
    latency_ms: float | None
    error: str = ""
    name: str = ""
    """What the machine is called (``NL_OPENAI_NAMES``), shown with its model."""


async def model_hosts(*, probe: bool = True) -> list[ModelHost]:
    """Each configured model host, asked whether it is up (cached for a minute)."""
    translator = OpenAITranslator()
    reports = []
    for host in translator.hosts:
        if probe:
            await BOARD.online(host, translator._headers())  # noqa: SLF001
        state = BOARD.state(host)
        reports.append(ModelHost(host.base_url, host.model, state.online, state.latency_ms,
                                 state.error, host.display))
    return reports


async def understand(
    ctx: Context, text: str, *, translator: Translator | None = None,
    now: datetime | None = None,
) -> Query | Clarification | None:
    tz = ctx.config.schedule.tz
    return await (translator or from_environment()).translate(
        text, now or datetime.now(tz), tz
    )


def _asks_about_arrival(query: Query) -> bool:
    filters = query.filters
    return bool(filters.created_after or filters.created_before)


def _under_entry_folder(ctx: Context, path: str) -> bool:
    """The scope is, or is inside, a folder new arrivals land in."""
    entries = tidy.load_spec(ctx).inbox.folders
    return any(path == f or path.startswith(f.rstrip("/") + "/") for f in entries)


async def _freshness_note(ctx: Context) -> list[dict]:
    found = await eventsync.freshness(ctx)
    if found is None:
        return []
    clock, kind = found
    notes = [{"key": "sync.updated", "args": {
        "time": clock, "kind": {"key": f"sync.kind.{kind}", "args": {}}}}]
    if reason := await eventsync.failure(ctx):
        notes.append({"key": "sync.failed", "args": {"reason": reason}})
    return notes


async def _sync(ctx: Context, *, events: bool, scope: str) -> list[dict]:
    """Bring the index up to date for this question; the line saying when.

    A question about when files arrived, or about the entry folders, is the
    one a stale index answers wrongly, so those read the event feed first
    (docs/wms/M8.1 §B). A lost cursor is not waited on here: the incremental
    stocktake answers, and the next scheduled sync (or the daily full run)
    mends the rest. Other questions keep the incremental stocktake of their
    scope, and say nothing about it.
    """
    with contextlib.suppress(NotFoundError):
        if events:
            await eventsync.refresh_index(ctx, allow_full=False, roots=[scope])
        else:
            await stocktake(ctx.client, ctx.store, roots=[scope], full=False,
                            page_size=ctx.config.stocktake.page_size)
    return await _freshness_note(ctx) if events else []


async def _diagnose_empty(ctx: Context, now: datetime, *, with_freshness: bool) -> list[dict]:
    """Nothing matched: what arrived today and yesterday, and when the index
    was last updated, so a person can tell a wrong condition from a stale
    index (docs/wms/M8.1 §B)."""
    tz = ctx.config.schedule.tz
    start = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday = start - timedelta(days=1)
    today = earlier = 0
    for raw in await ctx.store.created_times():
        when = parse_time(raw)
        if when is None:
            continue
        when = when.astimezone(tz)
        if when >= start:
            today += 1
        elif when >= yesterday:
            earlier += 1
    notes = [{"key": "nl.explain.none_diagnosis", "args": {"today": today, "yesterday": earlier}}]
    return [*(await _freshness_note(ctx) if with_freshness else []), *notes]


async def _tidy_proposal(ctx: Context, query: Query, now: datetime) -> Proposal:
    """The M7 jobs, asked for in a sentence: planned exactly like /wms does."""
    proposal = Proposal(kind="plan", query=query)
    proposal.notes.append({"key": "nl.explain.intent",
                           "args": {"intent": {"key": f"nl.intent.{query.intent}", "args": {}}}})
    scope = query.scope.path
    if scope != "/":
        proposal.notes.append({"key": "nl.explain.scope", "args": {"path": scope}})
    # They read the whole index: the event feed keeps it current (M8.1).
    await eventsync.refresh_index(ctx, allow_full=False)
    proposal.notes += await _freshness_note(ctx)
    if query.intent == "big_report":
        proposal.kind = "report"
        proposal.big = await tidy.big_report(ctx, scope=scope, at=now)
        return proposal
    if query.intent == "organize_tree":
        parts = {query.action_args.part} if query.action_args.part else None
        planned = await tidy.organize_tree(ctx, scope=None if scope == "/" else scope,
                                           at=now, parts=parts)
    elif query.intent == "organize_inbox":
        planned = await tidy.organize_inbox(ctx, folders=None if scope == "/" else [scope],
                                            at=now)
    else:
        planned = [await organize.dedupe(ctx, scope=scope,
                                         keep_under=ctx.config.dedupe.keep_under)]
    ids = []
    for plan in planned:
        plan_id = await plans.save(ctx, plan)
        if plan_id is not None:
            ids.append(plan_id)
    kept = [p for p in planned if not p.is_empty]
    if len(ids) > 1:
        proposal.kind, proposal.plan_ids = "batch", ids
    else:
        proposal.plan = kept[0] if kept else (planned[0] if planned else None)
        proposal.plan_id = ids[0] if ids else None
        if proposal.plan is not None:
            proposal.plan.notes = list(proposal.notes) + proposal.plan.notes
    return proposal


async def make_proposal(ctx: Context, query: Query, *, now: datetime | None = None,
                        user_id: int | None = None) -> Proposal:
    """Refresh the index where the Query looks, then plan without changing anything.

    The one thing written here is the download log: names the sentence said were
    already downloaded (「X 下过了」) are remembered whether or not the plan is run."""
    tz = ctx.config.schedule.tz
    now = now or datetime.now(tz)
    if query.intent in TIDY_INTENTS:
        return await _tidy_proposal(ctx, query, now)
    fresh = _asks_about_arrival(query) or _under_entry_folder(ctx, query.scope.path)
    lead = await _sync(ctx, events=fresh, scope=query.scope.path)
    if query.marked:
        await downloads.mark(ctx, query.marked, user_id=user_id)  # after the sync: files known

    async def diagnosis() -> list[dict]:
        return await _diagnose_empty(ctx, now, with_freshness=not lead)

    proposal = await propose(ctx.store, query, ctx.config, now=now, tz=tz,
                             name=f"nl-{now:%Y%m%d-%H%M%S}", lead_notes=lead,
                             on_empty=diagnosis)
    if proposal.plan is not None and query.intent == "download":
        # What is already on the NAS is listed once and left out of the plan (M9.2 §B).
        had = len(proposal.plan.notes)
        await outbound.drop_present(ctx, proposal.plan, when=now)
        proposal.notes += proposal.plan.notes[had:]
    if proposal.plan is not None and not proposal.plan.is_empty:
        outbound.annotate(ctx.config, proposal.plan, when=now)
        proposal.plan_id = await plans.save(ctx, proposal.plan)
    return proposal


def add_rules(ctx: Context, rules: list[Rule], *, sentence: str) -> Path:
    """Write a scheduled command's rules into the rules file; returns the file."""
    path = rulesfile.target_file(ctx.config)
    rulesfile.append_rules(path, rules, comment=f"/do {sentence}")
    return path


def proposal_lines(proposal: Proposal, *, limit: int = 8) -> list[str]:
    """For a person: how it was understood, what it matches, what would happen."""
    lines: list[str] = []
    if proposal.kind == "plan" and proposal.plan is not None:
        files, size = plans.plan_totals(proposal.plan)
        lines.append(t("plan.header", id=proposal.plan_id or "-",
                       source=proposal.plan.source or "nl",
                       actions=len(proposal.plan), files=files, size=human_size(size)))
    lines.extend("· " + render_note(note) for note in proposal.notes)
    if proposal.kind == "plan" and proposal.plan is not None:
        actions = proposal.plan.actions
        lines.extend("  " + action.describe() for action in actions[:limit])
        if len(actions) > limit:
            lines.append(t("plan.more", count=len(actions) - limit))
        if proposal.plan.is_empty:
            lines.append(t("plan.empty"))
    elif proposal.kind == "listing":
        for node in proposal.matches[:limit * 2]:
            lines.append(f"  {node.path}  ({human_size(node.size)})")
        if len(proposal.matches) > limit * 2:
            lines.append(t("plan.more", count=len(proposal.matches) - limit * 2))
    elif proposal.kind == "report" and proposal.big is not None:
        lines.extend(proposal.big.lines())
    elif proposal.kind == "batch":
        lines.append(t("nl.batch", count=len(proposal.plan_ids)))
    elif proposal.kind == "rule":
        lines.append(t("nl.rule.header"))
        lines.extend(f"  {rule.name}  [{rule.schedule.cron if rule.schedule else ''}]"
                     for rule in proposal.rules)
    return lines


def clarification_text(result: Clarification) -> str:
    """A rules-parser question is a catalogue key; a model's is already text."""
    if result.question.startswith("nl.ask."):
        return t(result.question, **result.args)
    return result.question
