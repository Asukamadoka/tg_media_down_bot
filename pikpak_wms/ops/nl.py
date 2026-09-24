"""Natural-language commands (docs/wms/M6): understand, propose, confirm.

    sentence → translator → Query ─┬─ list      → what matches (no changes)
                                   ├─ schedule  → rules for the rules file
                                   └─ otherwise → a stored plan to confirm

The translator only ever produces a Query; everything that changes the
drive goes through :mod:`pikpak_wms.ops.plans` after a person confirms.
"""

from __future__ import annotations

import contextlib
from datetime import datetime
from pathlib import Path

from ..core.errors import NotFoundError
from ..core.models import render_note
from ..i18n import t
from ..nl.compile import Proposal, propose
from ..nl.query import Clarification, Query
from ..nl.translator import Translator, from_environment
from ..rules.schema import Rule
from ..rules.units import human_size
from . import plans, rulesfile
from .context import Context
from .stocktake import stocktake


async def understand(
    ctx: Context, text: str, *, translator: Translator | None = None,
    now: datetime | None = None,
) -> Query | Clarification | None:
    tz = ctx.config.schedule.tz
    return await (translator or from_environment()).translate(
        text, now or datetime.now(tz), tz
    )


async def make_proposal(ctx: Context, query: Query, *, now: datetime | None = None) -> Proposal:
    """Refresh the index where the Query looks, then plan without changing anything."""
    tz = ctx.config.schedule.tz
    now = now or datetime.now(tz)
    # A folder that does not exist yet simply matches nothing.
    with contextlib.suppress(NotFoundError):
        await stocktake(ctx.client, ctx.store, roots=[query.scope.path], full=False,
                        page_size=ctx.config.stocktake.page_size)
    proposal = await propose(ctx.store, query, ctx.config, now=now, tz=tz,
                             name=f"nl-{now:%Y%m%d-%H%M%S}")
    if proposal.plan is not None and not proposal.plan.is_empty:
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
        lines.append(t("plan.header", id=proposal.plan_id or "-", source="nl",
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
