"""What each scheduled job does. The scheduler only decides *when*.

Scheduled organize / cleanup / layout runs save their plan as pending, and
apply it only when the job says ``apply: true``. No job can delete forever:
cleanup here always plans the trash (rule 2).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from ..config import ScheduledJob
from ..core.errors import NotFoundError
from ..core.models import ActionType, Plan
from ..i18n import t
from ..rules.actions import Deliver
from . import inbound, organize, outbound, plans
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


async def run_job(ctx: Context, job: ScheduledJob, *, deliver: Deliver | None = None) -> JobResult:
    cfg = ctx.config
    if job.name in ("stocktake", "stocktake-full"):
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
