"""What each scheduled job does. The scheduler only decides *when*.

Scheduled organize / cleanup / layout runs save their plan as pending, and
apply it only when the job says ``apply: true``. No job can delete forever:
cleanup here always plans the trash (rule 2).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from ..config import ScheduledJob
from ..core.errors import NotFoundError
from ..core.models import ActionType, Plan
from ..i18n import t
from ..rules.actions import Deliver
from ..rules.units import human_size
from . import inbound, organize, outbound, plans, tidy
from .context import Context
from .stocktake import stocktake

log = logging.getLogger(__name__)


@dataclass
class JobResult:
    name: str
    summary: str
    plan_id: int | None = None
    plan: Plan | None = None
    report: plans.ApplyReport | None = None
    plan_ids: list[int] = field(default_factory=list)
    """Jobs that plan in batches (organize-tree: one plan per top-level folder)."""
    reports: list[plans.ApplyReport] = field(default_factory=list)
    big: Any = None
    """big-report: the :class:`pikpak_wms.ops.tidy.BigReport`."""


async def _plan_job(
    ctx: Context, job: ScheduledJob, plan: Plan, deliver: Deliver | None
) -> JobResult:
    if deliver is None and any(a.type is ActionType.OUTBOUND for a in plan.actions):
        deliver = outbound.make_deliver(ctx)
    plan_id = await plans.save(ctx, plan)
    if plan_id is None:
        return JobResult(job.name, t("job.nothing", name=job.name), plan=plan)
    if not job.apply:
        return JobResult(job.name, t("job.planned", name=job.name, id=plan_id,
                                     actions=len(plan)), plan_id, plan)
    report = await plans.apply(ctx, plan_id, deliver=deliver)
    return JobResult(job.name, report.summary(), plan_id, plan, report)


async def _refresh(ctx: Context) -> None:
    """An incremental stocktake first: M7 jobs read the whole index."""
    cfg = ctx.config
    await stocktake(ctx.client, ctx.store, roots=cfg.stocktake.roots, full=False,
                    page_size=cfg.stocktake.page_size)


async def _batch_job(ctx: Context, job: ScheduledJob, prefix: str,
                     planned: list[Plan]) -> JobResult:
    """Save each plan, retire the job's stale ones, and apply when asked,
    within one ``max_actions_per_run`` for the whole run (the rest next run)."""
    ids: list[int] = []
    for plan in planned:
        plan_id = await plans.save(ctx, plan)
        if plan_id is not None:
            ids.append(plan_id)
    await plans.supersede(ctx, prefix=prefix, keep=set(ids))
    result = JobResult(job.name, "", plan_id=ids[0] if ids else None,
                       plan=planned[0] if len(planned) == 1 else None, plan_ids=ids)
    if not ids:
        result.summary = t("job.nothing", name=job.name)
        return result
    if not job.apply:
        actions = sum(len(p) for p in planned)
        result.summary = t("job.planned_many", name=job.name, count=len(ids), actions=actions)
        return result
    budget = ctx.config.runtime.max_actions_per_run
    for plan_id in ids:
        if budget <= 0:
            break
        report = await plans.apply(ctx, plan_id, limit=budget)
        result.reports.append(report)
        budget -= report.applied + sum(report.skipped.values()) + len(report.failed)
        if report.stopped:
            break
    result.report = result.reports[0] if len(result.reports) == 1 else None
    applied = sum(r.applied for r in result.reports)
    remaining = sum(r.remaining for r in result.reports)
    result.summary = t("job.applied_many", name=job.name, applied=applied,
                       remaining=remaining)
    return result


async def run_job(ctx: Context, job: ScheduledJob, *, deliver: Deliver | None = None) -> JobResult:
    cfg = ctx.config
    if job.name in (tidy.TREE, tidy.INBOX, "dedupe", "big-report"):
        await _refresh(ctx)
    if job.name == tidy.TREE:
        result = await _batch_job(ctx, job, f"{tidy.TREE}:", await tidy.organize_tree(ctx))
    elif job.name == tidy.INBOX:
        result = await _batch_job(ctx, job, tidy.INBOX, [await tidy.organize_inbox(ctx)])
    elif job.name == "dedupe":
        plan = await organize.dedupe(ctx, scope=cfg.dedupe.scope,
                                     keep_under=cfg.dedupe.keep_under)
        result = await _batch_job(ctx, job, "dedupe", [plan])
    elif job.name == "big-report":
        report = await tidy.big_report(ctx)
        result = JobResult(job.name, t("big.summary", files=len(report.files),
                                       size=human_size(report.reclaimable)))
        result.big = report
    elif job.name in ("stocktake", "stocktake-full"):
        report = await stocktake(
            ctx.client, ctx.store, roots=cfg.stocktake.roots,
            full=job.name == "stocktake-full" or not cfg.stocktake.incremental,
            page_size=cfg.stocktake.page_size,
        )
        result = JobResult(job.name, report.summary())
    elif job.name == "inbound-poll":
        polled = await inbound.poll(ctx)
        result = JobResult(job.name, t("job.polled", checked=polled.checked,
                                       finished=polled.finished, failed=polled.failed))
    elif job.name == "layout":
        result = await _plan_job(ctx, job, await organize.layout(ctx), deliver)
    else:  # organize, cleanup
        ruleset = organize.load(ctx)
        rules = ruleset.select(stage=job.name)
        if not rules:
            return JobResult(job.name, t("job.no_rules", name=job.name))
        # Rules read the index, so bring the part they look at up to date.
        # A scope that does not exist yet (no /Inbox so far) simply has nothing
        # to organize; it must not fail the job for the other rules.
        for scope in organize.scopes(rules):
            try:
                await stocktake(ctx.client, ctx.store, roots=[scope], full=False,
                                page_size=cfg.stocktake.page_size)
            except NotFoundError:
                log.info("job %s: %s does not exist in the drive yet", job.name, scope)
        plan = await organize.plan_rules(ctx, rules, source=job.name)
        result = await _plan_job(ctx, job, plan, deliver)
    await ctx.store.set_meta(
        f"last_job:{job.name}",
        json.dumps({"plan_id": result.plan_id,
                    "applied": result.report.applied if result.report else 0}),
    )
    log.info("job %s finished (plan %s)", job.name, result.plan_id)
    return result


async def run_rule(
    ctx: Context, rule_name: str, *, deliver: Deliver | None = None
) -> JobResult:
    """A rule with its own ``schedule``: stocktake its scope, plan it alone,
    and apply only if the rule says ``apply: true`` (never permanent deletion)."""
    ruleset = organize.load(ctx)
    (rule,) = ruleset.select(names=[rule_name])
    job = ScheduledJob(name="organize", cron=rule.schedule.cron if rule.schedule else "0 0 * * *",
                       apply=bool(rule.schedule and rule.schedule.apply))
    try:
        await stocktake(ctx.client, ctx.store, roots=[rule.scope], full=False,
                        page_size=ctx.config.stocktake.page_size)
    except NotFoundError:
        log.info("rule %s: %s does not exist in the drive yet", rule_name, rule.scope)
    plan = await organize.plan_rules(ctx, [rule], source=f"rule:{rule_name}")
    result = await _plan_job(ctx, job, plan, deliver)
    result.name = f"rule:{rule_name}"
    return result
